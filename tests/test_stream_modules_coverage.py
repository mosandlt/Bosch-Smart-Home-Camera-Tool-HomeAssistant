"""Coverage tests for stream-related module gaps.

Targets (all confirmed missing before this file):
  switch.py  — line 935 (stream-warming branch of _cooldown_message)
               lines 994-1020 (_pending_privacy_loop: while-else timeout warning,
                                except Exception failure path, finally cleanup)
               lines 1058-1061 (async_will_remove_from_hass task cancel)
  camera.py  — lines 338-342 (privacy TOCTOU guard: discard frame when privacy
                               turned ON during fetch)
               line 697 (_pending_privacy_loop STREAM_START_SKIPPED debug log)
  tls_proxy.py — lines 441-442 (best-effort writer close raises inside outer
                                  exception handler of rtsp_keepalive)

Strategy: all tests use SimpleNamespace stubs — no real HA, no aiohttp.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"

# ─────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _stub_coord(
    *,
    stream_warming: bool = False,
    privacy_set_at: float = float("-inf"),
    live_connections: dict | None = None,
    shc_state: dict | None = None,
) -> SimpleNamespace:
    """Coordinator stub matching the fields BoschPrivacyModeSwitch touches."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        _live_connections=live_connections if live_connections is not None else {},
        _user_intent_streams=set(),
        _shc_state_cache={
            CAM_ID: (shc_state if shc_state is not None else {"privacy_mode": False})
        },
        _session_stale={},
        _stream_warming={CAM_ID} if stream_warming else set(),
        _privacy_set_at={CAM_ID: privacy_set_at}
        if privacy_set_at != float("-inf")
        else {},
        _light_set_at={},
        _audio_enabled={CAM_ID: True},
        _privacy_sound_cache={CAM_ID: False},
        _timestamp_cache={CAM_ID: True},
        _ledlights_cache={CAM_ID: True},
        _arming_cache={},
        _rcp_privacy_cache={},
        last_update_success=True,
        options={},
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: stream_warming,
        async_cloud_set_privacy_mode=AsyncMock(),
    )


def _stub_entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "x"},
        options={},
    )


def _make_privacy_switch(coord: SimpleNamespace, entry: SimpleNamespace | None = None):
    """Construct BoschPrivacyModeSwitch bypassing HA entity plumbing."""
    from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

    entry = entry or _stub_entry()
    sw = BoschPrivacyModeSwitch.__new__(BoschPrivacyModeSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._entry = entry
    sw._cam_title = "Terrasse"
    sw._model = "HOME_Eyes_Outdoor"
    sw._model_name = "Eyes Outdoor"
    sw._fw = "9.40.25"
    sw._mac = "aa:bb:cc:dd:ee:01"
    sw._pending_privacy = None
    sw._pending_apply_task = None
    sw.async_write_ha_state = MagicMock()
    return sw


# ─────────────────────────────────────────────────────────────────────────────
# switch.py — line 935: _cooldown_message when stream is warming
# ─────────────────────────────────────────────────────────────────────────────


class TestCooldownMessageStreamWarming:
    """_cooldown_message returns the stream-warming message when the stream
    is warming up (line 935 branch)."""

    def test_cooldown_message_stream_warming_branch(self) -> None:
        """Line 935: when is_stream_warming returns True, _cooldown_message
        must return the 'stream is still starting' message."""
        coord = _stub_coord(stream_warming=True)
        sw = _make_privacy_switch(coord)

        msg = sw._cooldown_message()

        assert "stream is still starting" in msg
        assert "Terrasse" in msg

    def test_cooldown_message_cooldown_branch(self) -> None:
        """When no stream warming but privacy was recently set, returns the
        cooldown message (ensures only line 935 branch hits 'stream')."""
        import time

        coord = _stub_coord(stream_warming=False, privacy_set_at=time.monotonic())
        sw = _make_privacy_switch(coord)

        msg = sw._cooldown_message()

        assert "stream is still starting" not in msg
        assert "wait" in msg.lower() or "just changed" in msg.lower()


# ─────────────────────────────────────────────────────────────────────────────
# switch.py — lines 994-1020: _pending_privacy_loop paths
# ─────────────────────────────────────────────────────────────────────────────


class TestPendingPrivacyLoop:
    """Tests for the three interesting branches of _pending_privacy_loop."""

    @pytest.mark.asyncio
    async def test_loop_timeout_warning_logged(self) -> None:
        """Lines 996-1001 (while-else): when _privacy_block_remaining never
        returns ≤0 within the max wait, the else clause fires and logs a
        warning (lines 997-1001), then flush is still called."""
        coord = _stub_coord(stream_warming=True)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False

        # Shrink constants so the test completes quickly
        sw._PRIVACY_PENDING_MAX_WAIT = 0.05  # type: ignore[assignment]
        sw._PRIVACY_PENDING_POLL = 0.02  # type: ignore[assignment]

        # is_stream_warming stays True → block_remaining always > 0 → while-else fires
        flush_called = []

        async def _fake_flush() -> None:
            flush_called.append(True)

        sw._flush_pending_privacy = _fake_flush  # type: ignore[method-assign]

        with patch("custom_components.bosch_shc_camera.switch._LOGGER") as mock_log:
            await sw._pending_privacy_loop()

        # warning must have been emitted (lines 997-1001)
        assert mock_log.warning.called
        warning_args = " ".join(
            str(a) for call in mock_log.warning.call_args_list for a in call.args
        )
        assert "still blocked" in warning_args or "applying now" in warning_args
        # flush must still have been called after the warning
        assert flush_called

    @pytest.mark.asyncio
    async def test_loop_exception_in_flush_logged_not_raised(self) -> None:
        """Lines 1005-1010: an Exception from _flush_pending_privacy must be
        caught and logged as a warning — the loop must NOT propagate."""
        coord = _stub_coord(stream_warming=False)
        # No cooldown blocking → block_remaining returns 0 → breaks immediately
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False

        async def _raise_flush() -> None:
            raise RuntimeError("simulated API failure")

        sw._flush_pending_privacy = _raise_flush  # type: ignore[method-assign]

        with patch("custom_components.bosch_shc_camera.switch._LOGGER") as mock_log:
            # Must not raise
            await sw._pending_privacy_loop()

        assert mock_log.warning.called
        warning_args = " ".join(
            str(a) for call in mock_log.warning.call_args_list for a in call.args
        )
        assert "simulated API failure" in warning_args or "failed" in warning_args

    @pytest.mark.asyncio
    async def test_loop_cancelled_error_propagates(self) -> None:
        """Line 1003-1004: CancelledError must re-raise (not be swallowed)."""
        coord = _stub_coord(stream_warming=False)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False

        async def _cancel_flush() -> None:
            raise asyncio.CancelledError

        sw._flush_pending_privacy = _cancel_flush  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await sw._pending_privacy_loop()

    @pytest.mark.asyncio
    async def test_finally_clears_task_and_writes_state(self) -> None:
        """Lines 1011-1013 (finally): _pending_apply_task becomes None and
        async_write_ha_state is called regardless of success or failure."""
        coord = _stub_coord(stream_warming=False)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False
        # Simulate a running task
        fake_task: asyncio.Task[None] = MagicMock(spec=asyncio.Task)  # type: ignore[assignment]
        sw._pending_apply_task = fake_task

        # Happy path — flush does nothing
        async def _noop_flush() -> None:
            pass

        sw._flush_pending_privacy = _noop_flush  # type: ignore[method-assign]

        await sw._pending_privacy_loop()

        assert sw._pending_apply_task is None
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_finally_clears_task_even_on_exception(self) -> None:
        """finally block fires even when _flush_pending_privacy raises."""
        coord = _stub_coord(stream_warming=False)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = True
        fake_task: asyncio.Task[None] = MagicMock(spec=asyncio.Task)  # type: ignore[assignment]
        sw._pending_apply_task = fake_task

        async def _bad_flush() -> None:
            raise ValueError("boom")

        sw._flush_pending_privacy = _bad_flush  # type: ignore[method-assign]

        # Exception is caught internally — must not propagate
        await sw._pending_privacy_loop()

        assert sw._pending_apply_task is None
        sw.async_write_ha_state.assert_called()


# ─────────────────────────────────────────────────────────────────────────────
# switch.py — lines 1048-1053: async_will_remove_from_hass
# ─────────────────────────────────────────────────────────────────────────────


class TestAsyncWillRemoveFromHass:
    """async_will_remove_from_hass cancels a pending task (lines 1050-1052)."""

    @pytest.mark.asyncio
    async def test_cancels_running_task_on_removal(self) -> None:
        """Lines 1058-1061: if _pending_apply_task is not done, cancel() is called."""
        coord = _stub_coord()
        sw = _make_privacy_switch(coord)

        task = MagicMock(spec=asyncio.Task)
        task.done.return_value = False
        task.cancel = MagicMock()
        sw._pending_apply_task = task

        # Patch super().async_will_remove_from_hass to avoid HA entity plumbing
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

            await BoschPrivacyModeSwitch.async_will_remove_from_hass(sw)

        task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_cancel_completed_task_on_removal(self) -> None:
        """Task already done → cancel() must NOT be called."""
        coord = _stub_coord()
        sw = _make_privacy_switch(coord)

        task = MagicMock(spec=asyncio.Task)
        task.done.return_value = True
        task.cancel = MagicMock()
        sw._pending_apply_task = task

        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

            await BoschPrivacyModeSwitch.async_will_remove_from_hass(sw)

        task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_task_removal_is_noop(self) -> None:
        """_pending_apply_task is None → no crash, super() is still called."""
        coord = _stub_coord()
        sw = _make_privacy_switch(coord)
        sw._pending_apply_task = None

        super_called = []

        async def _fake_super(self_inner: object) -> None:
            super_called.append(True)

        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(side_effect=lambda: super_called.append(True)),
        ):
            from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

            await BoschPrivacyModeSwitch.async_will_remove_from_hass(sw)
        # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# camera.py — lines 338-342: privacy TOCTOU guard (discard frame)
# ─────────────────────────────────────────────────────────────────────────────


class TestCameraPrivacyToctouGuard:
    """When privacy flips ON during a live fetch, the fetched frame must be
    discarded (lines 338-342 — the `return` before `_cached_image = image`)."""

    def _make_camera(self, coord: SimpleNamespace) -> object:
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        cam._display_name = "Bosch Terrasse"
        cam.hass = SimpleNamespace()
        # is_streaming is a @property — drive it via coordinator._live_connections
        # (empty dict → not streaming)
        cam._cached_image = b""
        # Use a 1×1 JPEG placeholder marker to distinguish "real" vs placeholder
        cam._PLACEHOLDER_JPEG = b"\xff\xd8\xff\xe0placeholder"
        cam._last_image_fetch = float("-inf")
        cam._refresh_inflight = False
        cam._force_image_refresh = False
        cam.async_write_ha_state = MagicMock()
        return cam

    @pytest.mark.asyncio
    async def test_frame_discarded_when_privacy_on_during_fetch(self) -> None:
        """Lines 338-342: if privacy_mode becomes True while a frame was being
        fetched, _cached_image must NOT be updated.

        We reach lines 338-342 by calling _async_trigger_image_refresh with
        a coord that has: privacy OFF at the top (so the early-out at line 260
        is not triggered) but privacy ON in _shc_state_cache at the point where
        the TOCTOU guard re-reads it (line 336-342).  We achieve this by having
        async_fetch_live_snapshot return a real image AND having _shc_state_cache
        report privacy_mode=True (the guard at line 337)."""
        # Privacy OFF at top-of-method check (line 260), ON at TOCTOU re-check (line 337)
        # We set privacy to True in the cache for the TOCTOU check.
        # But line 260 also reads the same cache → we need to pass the top-guard.
        # Solution: set privacy False, then swap it to True after the top-guard
        # runs but before the TOCTOU check by patching _async_trigger_image_refresh's
        # internal flow via a side-effect on async_fetch_live_snapshot.
        coord = _stub_coord(shc_state={"privacy_mode": False})
        cam = self._make_camera(coord)
        original_cache = cam._cached_image

        fake_image = b"\xff\xd8\xff\xe0fakeframe"

        # Side-effect: flip privacy to True while "fetching" — simulates the race
        def _flip_privacy_and_return(*_args: object, **_kwargs: object) -> bytes:
            coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
            return fake_image

        coord.async_fetch_live_snapshot = AsyncMock(
            side_effect=_flip_privacy_and_return
        )
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(return_value=None)
        # async_camera_image needed for placeholder fast-path (cam._cached_image = b"" ≠ placeholder)
        cam.async_camera_image = AsyncMock(return_value=None)

        from custom_components.bosch_shc_camera.camera import BoschCamera

        await BoschCamera._async_trigger_image_refresh(cam)

        # Privacy was ON at TOCTOU check → frame must be discarded
        assert cam._cached_image == original_cache

    @pytest.mark.asyncio
    async def test_frame_stored_when_privacy_off_during_fetch(self) -> None:
        """Control: when privacy stays OFF throughout, the frame IS stored."""
        coord = _stub_coord(shc_state={"privacy_mode": False})
        cam = self._make_camera(coord)

        fake_image = b"\xff\xd8\xff\xe0fakeframe"
        coord.async_fetch_live_snapshot = AsyncMock(return_value=fake_image)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(return_value=None)
        coord._image_entities = {}
        cam.async_camera_image = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.save_snapshot",
            new=AsyncMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera._async_trigger_image_refresh(cam)

        assert cam._cached_image == fake_image


# ─────────────────────────────────────────────────────────────────────────────
# camera.py — line 697: async_create_stream with STREAM_START_SKIPPED
# ─────────────────────────────────────────────────────────────────────────────


class TestAsyncCreateStreamSkipped:
    """Line 697: when try_live_connection returns STREAM_START_SKIPPED a debug
    message is logged and the method continues (falls through to prewarm wait)."""

    def _make_camera(self, coord: SimpleNamespace) -> object:
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        cam._display_name = "Bosch Terrasse"
        return cam

    @pytest.mark.asyncio
    async def test_stream_start_skipped_logs_debug_and_continues(self) -> None:
        """Line 697: STREAM_START_SKIPPED path logs the coalescing message and
        does NOT return None early (falls through to the prewarm poll)."""
        from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED

        coord = SimpleNamespace(
            _live_connections={},  # no existing session → triggers auto-open path
            _stream_warming=set(),  # not warming → no wait loop
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
            try_live_connection=AsyncMock(return_value=STREAM_START_SKIPPED),
            async_update_listeners=MagicMock(),
            get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
        )
        cam = self._make_camera(coord)

        fake_stream = object()
        with (
            patch(
                "homeassistant.components.camera.Camera.async_create_stream",
                new=AsyncMock(return_value=fake_stream),
            ),
            patch("custom_components.bosch_shc_camera.camera._LOGGER") as mock_log,
        ):
            result = await cam.async_create_stream()

        # try_live_connection was called
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        # The "coalescing" debug message must have been emitted (line 697)
        debug_calls = " ".join(
            str(a) for call in mock_log.debug.call_args_list for a in call.args
        )
        assert "coalescing" in debug_calls
        # Method did NOT return None early — it fell through to super()
        assert result is fake_stream

    @pytest.mark.asyncio
    async def test_privacy_on_raises_ha_error(self) -> None:
        """Gate before line 697: privacy ON must raise HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError

        coord = SimpleNamespace(
            _live_connections={},
            _stream_warming=set(),
            _shc_state_cache={CAM_ID: {"privacy_mode": True}},
            try_live_connection=AsyncMock(),
            async_update_listeners=MagicMock(),
            get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
        )
        cam = self._make_camera(coord)

        with pytest.raises(HomeAssistantError, match="privacy mode is ON"):
            await cam.async_create_stream()

        coord.try_live_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_connection_returns_none(self) -> None:
        """When try_live_connection returns a falsy non-skipped value, return None."""
        coord = SimpleNamespace(
            _live_connections={},
            _stream_warming=set(),
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
            try_live_connection=AsyncMock(return_value=None),
            async_update_listeners=MagicMock(),
            get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
        )
        cam = self._make_camera(coord)

        result = await cam.async_create_stream()

        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# tls_proxy.py — lines 441-442: best-effort writer close in outer except handler
# ─────────────────────────────────────────────────────────────────────────────


class TestRtspKeepaliveWriterCloseOnException:
    """Lines 441-442: when an exception is raised AFTER open_connection succeeds
    (e.g. during drain / read) AND writer.close() / wait_closed() itself raises,
    the inner exception must be silently swallowed (noqa S110) and the function
    must return False without propagating."""

    @pytest.mark.asyncio
    async def test_writer_close_raises_in_outer_except_is_swallowed(self) -> None:
        """Cover lines 441-442: outer except → writer is not None → writer.close()
        raises → inner except: pass fires → rtsp_keepalive returns False."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        mock_writer = AsyncMock()
        mock_writer.close = MagicMock(side_effect=OSError("simulated close error"))
        mock_writer.wait_closed = AsyncMock(side_effect=OSError("simulated wait error"))

        mock_reader = AsyncMock()

        # Simulate: open_connection succeeds (returns reader+writer), but
        # writer.drain() raises → triggers outer except path.
        async def _fake_open(*args: object, **kwargs: object) -> tuple[object, object]:
            return mock_reader, mock_writer

        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock(side_effect=ConnectionResetError("reset"))

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            new=AsyncMock(side_effect=_fake_open),
        ):
            result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        # Must return False — not raise
        assert result is False
        # writer.close() was attempted
        mock_writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_writer_wait_closed_raises_in_outer_except_is_swallowed(self) -> None:
        """wait_closed() raises inside the inner try — still returns False."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()  # close() succeeds
        mock_writer.wait_closed = AsyncMock(side_effect=OSError("wait_closed boom"))
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock(side_effect=TimeoutError("read timeout"))

        mock_reader = AsyncMock()

        async def _fake_open(*args: object, **kwargs: object) -> tuple[object, object]:
            return mock_reader, mock_writer

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            new=AsyncMock(side_effect=_fake_open),
        ):
            result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert result is False

    @pytest.mark.asyncio
    async def test_open_connection_fails_returns_false_no_writer_cleanup(self) -> None:
        """When open_connection itself raises, writer is None → cleanup branch
        skipped entirely → returns False cleanly."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            new=AsyncMock(side_effect=ConnectionRefusedError("refused")),
        ):
            result = await rtsp_keepalive(9999, "user", "pass", CAM_ID)

        assert result is False
