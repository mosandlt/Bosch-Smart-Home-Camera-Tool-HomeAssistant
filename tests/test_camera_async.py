"""Tests for camera.py async methods + lifecycle hooks (Round 2).

Existing `test_camera.py` + `test_camera_extra.py` cover the synchronous
properties (~42 tests). This file targets the async lifecycle and the
image-refresh state machine that those skipped:

  - `async_setup_entry`  — entity creation gated by `enable_snapshots`
  - `async_added_to_hass` / `async_will_remove_from_hass` — register
    + unregister with the coordinator's `_camera_entities` map
  - `_handle_coordinator_update` — streaming→idle transition + 30-min
    proactive refresh schedule
  - `_async_trigger_image_refresh` — the 4-step fallback chain
    (event-snapshot quick-seed → live REMOTE → live LOCAL Digest →
    fresh-event last-resort) with privacy-mode short-circuit
  - `async_enable_motion_detection` / `async_disable_motion_detection`
    — Bosch v11 PUT path with sensitivity preservation
  - `_async_rcp_thumbnail` — RCP+ over cloud proxy fallback for
    cameras whose REMOTE snap.jpg returns 401

These run without HA runtime; SimpleNamespace stubs the coordinator
+ camera entity. The few HA-framework calls (`async_write_ha_state`,
`super().async_added_to_hass`) are stubbed via class-method binding
that skips the parent chain.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _make_coord(**overrides):
    """Coordinator stub with the dicts camera.py reads."""
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:00:00:00:00:01",  # synthetic test MAC
                },
                "events": [],
                "live": {},
            }
        },
        _live_connections={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        _shc_state_cache={},
        _stream_warming=set(),
        last_update_success=True,
        motion_settings=lambda cid: {},
        is_stream_warming=lambda cid: False,
        async_request_refresh=AsyncMock(),
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        async_fetch_fresh_event_snapshot=AsyncMock(return_value=None),
        async_put_camera=AsyncMock(return_value=True),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_entry(**overrides):
    base = dict(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera(coord=None, entry=None, **camera_overrides):
    """Build a BoschCamera stub.

    Bypasses CoordinatorEntity / Camera __init__ so the entity is
    callable in pure-Python tests without the HA framework. We attach
    only the attributes the methods-under-test read.
    """
    from custom_components.bosch_shc_camera.camera import BoschCamera
    coord = coord or _make_coord()
    entry = entry or _make_entry()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = entry
    cam._attr_name = "Bosch Terrasse"
    cam._cached_image = None
    cam._force_image_refresh = False
    cam._last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor"
    cam._hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "64:00:00:00:00:01"
    # HA framework calls camera.py uses
    cam.async_write_ha_state = MagicMock()
    # Default async_create_task closes the coroutine to avoid the
    # "coroutine never awaited" warning. Tests that need to capture
    # the scheduled coroutine override this with their own collector.
    def _create_task(coro):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock()
    cam.hass = SimpleNamespace(async_create_task=MagicMock(side_effect=_create_task))
    for k, v in camera_overrides.items():
        setattr(cam, k, v)
    return cam


# ── async_setup_entry (74-90) ────────────────────────────────────────────


class TestAsyncSetupEntry:
    """Per-cam entity creation, gated by `enable_snapshots` option."""

    @pytest.mark.asyncio
    async def test_skip_when_snapshots_disabled(self):
        from custom_components.bosch_shc_camera.camera import async_setup_entry
        coord = _make_coord()
        entry = _make_entry(options={"enable_snapshots": False})
        entry.runtime_data = coord
        async_add = MagicMock()
        await async_setup_entry(MagicMock(), entry, async_add)
        async_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_one_entity_per_cam(self):
        from custom_components.bosch_shc_camera.camera import async_setup_entry
        coord = _make_coord()
        coord.data = {CAM_ID: {"info": {"title": "Terrasse"}}, "OTHER-ID": {"info": {"title": "Garten"}}}
        entry = _make_entry(options={})  # default enable_snapshots=True
        entry.runtime_data = coord
        async_add = MagicMock()
        await async_setup_entry(MagicMock(), entry, async_add)
        async_add.assert_called_once()
        # First positional arg = list of entities
        entities = async_add.call_args[0][0]
        assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_no_entities_when_no_cams_discovered(self):
        from custom_components.bosch_shc_camera.camera import async_setup_entry
        coord = _make_coord()
        coord.data = {}
        entry = _make_entry(options={})
        entry.runtime_data = coord
        async_add = MagicMock()
        await async_setup_entry(MagicMock(), entry, async_add)
        async_add.assert_called_once()
        entities = async_add.call_args[0][0]
        assert entities == []


# ── Lifecycle hooks ──────────────────────────────────────────────────────


class TestLifecycleHooks:
    """Pin the entity registration contract.

    `async_added_to_hass` must register self in `coordinator._camera_entities`
    so the heartbeat NVR-restart hook + service handlers can find this
    instance. `async_will_remove_from_hass` must unregister so the dict
    doesn't accumulate dead refs across reloads."""

    @pytest.mark.asyncio
    async def test_added_to_hass_registers_with_coordinator(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        # Patch CoordinatorEntity's parent to be a no-op so we don't need
        # the HA dispatcher to fire.
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_added_to_hass(cam)
        assert coord._camera_entities[CAM_ID] is cam

    @pytest.mark.asyncio
    async def test_added_to_hass_schedules_image_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        # Make hass.async_create_task close the coroutine so it doesn't leak
        def _create_task(coro):
            try:
                coro.close()
            except (AttributeError, RuntimeError):
                pass
            return MagicMock()
        cam.hass.async_create_task = MagicMock(side_effect=_create_task)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_added_to_hass(cam)
        cam.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_will_remove_unregisters_from_coordinator(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        coord._camera_entities[CAM_ID] = cam
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_will_remove_from_hass(cam)
        assert CAM_ID not in coord._camera_entities

    @pytest.mark.asyncio
    async def test_will_remove_when_not_registered_no_crash(self):
        """User edge case — `async_will_remove_from_hass` may fire after
        a reload that already cleared the dict. Must not raise."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        # Ensure dict is empty
        coord._camera_entities.clear()
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            # Must not raise
            await BoschCamera.async_will_remove_from_hass(cam)


# ── _handle_coordinator_update (153-171) ────────────────────────────────


class TestHandleCoordinatorUpdate:
    """The main state-machine hook fired on every coordinator tick.

    Two transitions of interest:
      1. streaming → idle: trigger immediate (delay=2s) refresh so the
         card replaces the now-paused stream tile with a fresh snapshot.
      2. still idle → idle, but proactive interval elapsed: kick off a
         background refresh so the snapshot stays current even when no
         user is looking.

    Must NOT trigger refresh when:
      - Still streaming (no transition)
      - Was streaming and now still streaming
      - Idle but interval not elapsed
    """

    def _create_task_collector(self, cam):
        tasks = []
        def _create_task(coro):
            tasks.append(coro)
            try:
                coro.close()
            except (AttributeError, RuntimeError):
                pass
            return MagicMock()
        cam.hass.async_create_task = MagicMock(side_effect=_create_task)
        return tasks

    def test_streaming_to_idle_triggers_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()  # _live_connections empty → not streaming
        cam = _make_camera(coord=coord, _was_streaming=True)
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert len(tasks) == 1
        # _was_streaming flipped
        assert cam._was_streaming is False

    def test_idle_to_idle_within_interval_no_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(
            coord=coord,
            _was_streaming=False,
            _last_image_fetch=time.monotonic() - 100,  # 100s ago, < 1800s default
        )
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert tasks == []

    def test_idle_to_idle_after_interval_triggers_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(
            coord=coord,
            _was_streaming=False,
            _last_image_fetch=time.monotonic() - 2000,  # > 1800s
        )
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert len(tasks) == 1

    def test_streaming_no_action(self):
        """Was streaming, still streaming → no refresh, no transition."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord(_live_connections={CAM_ID: {}})
        cam = _make_camera(coord=coord, _was_streaming=True)
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert tasks == []
        assert cam._was_streaming is True

    def test_custom_snapshot_interval_respected(self):
        """User-set `snapshot_interval` option must override default 1800s."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        entry = _make_entry(options={"snapshot_interval": 60})  # 1 min
        cam = _make_camera(
            coord=coord, entry=entry,
            _was_streaming=False,
            _last_image_fetch=time.monotonic() - 90,  # 90s ago > 60s
        )
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert len(tasks) == 1


# ── _async_trigger_image_refresh (173-242) ───────────────────────────────


class TestAsyncTriggerImageRefresh:
    """The 4-step image-refresh state machine, the largest method in
    camera.py and the one most user-visible bugs cluster around."""

    @pytest.mark.asyncio
    async def test_privacy_mode_short_circuit(self):
        """When SHC says privacy is ON, skip the refresh entirely — the
        camera blocks the image and any fetch returns 0 bytes (or worse,
        a stale event still). Pinned because the missing short-circuit
        in earlier versions caused dozens of empty PUT /connection
        round-trips per minute (2026-04-23 forum thread)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord(_shc_state_cache={CAM_ID: {"privacy_mode": True}})
        cam = _make_camera(coord=coord)
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_not_awaited()
        coord.async_fetch_fresh_event_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_refresh_flag_set_then_cleared(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        # finally clause clears the flag
        assert cam._force_image_refresh is False

    @pytest.mark.asyncio
    async def test_uses_live_snapshot_when_idle(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8live")
        cam = _make_camera(coord=coord)
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        assert cam._cached_image == b"\xff\xd8live"
        assert cam._last_image_fetch > 0
        cam.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_local_when_remote_returns_none(self):
        """REMOTE snap.jpg may 401 on CAMERA_360 — try LOCAL Digest path."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=b"\xff\xd8local")
        cam = _make_camera(coord=coord)
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        coord.async_fetch_live_snapshot_local.assert_awaited_once_with(CAM_ID)
        assert cam._cached_image == b"\xff\xd8local"

    @pytest.mark.asyncio
    async def test_falls_back_to_fresh_event_when_live_paths_fail(self):
        """When both REMOTE+LOCAL live snap paths return None, dig into
        fresh events as a last resort. Bosch sometimes returns a 0-byte
        snap.jpg right after a privacy-mode flip; the fresh event grab
        is the safety net."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(return_value=b"\xff\xd8event")
        cam = _make_camera(coord=coord)
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_fresh_event_snapshot.assert_awaited_once_with(CAM_ID)
        assert cam._cached_image == b"\xff\xd8event"

    @pytest.mark.asyncio
    async def test_skips_live_snapshot_when_streaming(self):
        """Opening PUT /connection while a stream is live tears down
        the active RTSP session. Skip both live paths when streaming;
        only the quick-seed (event) path runs."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord(_live_connections={CAM_ID: {}})  # → is_streaming True
        cam = _make_camera(coord=coord)
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_not_awaited()
        coord.async_fetch_live_snapshot_local.assert_not_awaited()
        # And NOT the fresh-event fallback either (line 228 check)
        coord.async_fetch_fresh_event_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quick_event_seed_when_no_cached_image(self):
        """First-mount path: _cached_image is None (haven't seeded the
        placeholder yet via __init__). Use async_camera_image to grab
        a quick event snapshot so the card has something within 1 s."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord, _cached_image=None)
        cam.async_camera_image = AsyncMock(return_value=b"\xff\xd8seed")
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        cam.async_camera_image.assert_awaited_once()
        assert cam._cached_image == b"\xff\xd8seed"

    @pytest.mark.asyncio
    async def test_exception_swallowed(self):
        """Network/HTTP errors must not propagate — the user sees a
        blank state otherwise. Pinned because earlier versions surfaced
        these as red toasts on every coordinator tick when the WAN was
        down."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(side_effect=RuntimeError("oops"))
        cam = _make_camera(coord=coord)
        # Must NOT raise
        await BoschCamera._async_trigger_image_refresh(cam, delay=0)
        # And the flag was still cleared (finally)
        assert cam._force_image_refresh is False

    @pytest.mark.asyncio
    async def test_delay_zero_skips_sleep(self):
        """delay=0 must not call asyncio.sleep — pin so a refactor can't
        accidentally add a 0-second sleep that schedules a context switch."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            await BoschCamera._async_trigger_image_refresh(cam, delay=0)
            sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delay_nonzero_sleeps(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            await BoschCamera._async_trigger_image_refresh(cam, delay=2)
            sleep.assert_awaited_once_with(2)


# ── async_enable/disable_motion_detection (276-294) ──────────────────────


class TestMotionDetectionToggle:
    """The standard HA `camera.enable_motion_detection` /
    `camera.disable_motion_detection` services. Bosch wants both
    `enabled` and `motionAlarmConfiguration` (sensitivity) in every
    PUT — preserving the existing sensitivity is critical, otherwise
    the user's tuning resets to HIGH every time they toggle."""

    @pytest.mark.asyncio
    async def test_enable_sends_enabled_true(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": False, "motionAlarmConfiguration": "MEDIUM",
        }
        cam = _make_camera(coord=coord)
        # Make hass.async_create_task close the coro to avoid leak warnings
        cam.hass.async_create_task = MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1])
        await BoschCamera.async_enable_motion_detection(cam)
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "motion",
            {"enabled": True, "motionAlarmConfiguration": "MEDIUM"},
        )

    @pytest.mark.asyncio
    async def test_disable_sends_enabled_false_keeps_sensitivity(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": True, "motionAlarmConfiguration": "LOW",
        }
        cam = _make_camera(coord=coord)
        cam.hass.async_create_task = MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1])
        await BoschCamera.async_disable_motion_detection(cam)
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "motion",
            {"enabled": False, "motionAlarmConfiguration": "LOW"},
        )

    @pytest.mark.asyncio
    async def test_enable_defaults_to_high_when_no_settings(self):
        """When motion_settings returns empty (cam not yet refreshed),
        default sensitivity to HIGH so the PUT doesn't drop the field."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.motion_settings = lambda cid: {}
        cam = _make_camera(coord=coord)
        cam.hass.async_create_task = MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1])
        await BoschCamera.async_enable_motion_detection(cam)
        payload = coord.async_put_camera.await_args[0][2]
        assert payload["motionAlarmConfiguration"] == "HIGH"
        assert payload["enabled"] is True

    @pytest.mark.asyncio
    async def test_enable_triggers_coordinator_refresh(self):
        """After PUT, fire a coordinator refresh in background so the
        `motion_detection_enabled` property reflects the new state."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        cam.hass.async_create_task = MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1])
        await BoschCamera.async_enable_motion_detection(cam)
        cam.hass.async_create_task.assert_called_once()


# ── stream_source advanced cases (406-435) ───────────────────────────────


class TestStreamSourceEdgeCases:
    """Additional stream_source cases not covered in test_camera_extra.py."""

    @pytest.mark.asyncio
    async def test_returns_none_when_url_missing(self):
        """Live conn entry exists but has no rtsps/rtsp URL — return None.
        Edge case during the connect-handshake window."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord(_live_connections={CAM_ID: {"_connection_type": "LOCAL"}})
        coord._audio_enabled = {}
        cam = _make_camera(coord=coord)
        url = await BoschCamera.stream_source(cam)
        assert url is None

    @pytest.mark.asyncio
    async def test_falls_back_from_rtsps_to_rtsp(self):
        """Some legacy code paths set `rtspUrl` (no s); stream_source
        accepts either. Pin so a refactor doesn't drop the fallback."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord(_live_connections={
            CAM_ID: {
                "_connection_type": "LOCAL",
                "rtspUrl": "rtsp://x:y@127.0.0.1:5000/rtsp_tunnel",
            },
        })
        coord._audio_enabled = {CAM_ID: True}
        cam = _make_camera(coord=coord)
        url = await BoschCamera.stream_source(cam)
        assert url and "127.0.0.1:5000" in url


# ── is_recording always False (260-262) ──────────────────────────────────


class TestIsRecording:
    """HA Camera.is_recording — we don't track recording at the entity
    level (Mini-NVR has its own switch entity). Pin to False."""

    def test_returns_false(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        assert BoschCamera.is_recording.fget(cam) is False


# ── _token property (319-321) ────────────────────────────────────────────


class TestTokenProperty:
    def test_returns_bearer_token_from_entry_data(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        entry = _make_entry(data={"bearer_token": "TOK-X"})
        cam = _make_camera(coord=coord, entry=entry)
        assert BoschCamera._token.fget(cam) == "TOK-X"

    def test_returns_empty_when_no_token(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        entry = _make_entry(data={})
        cam = _make_camera(coord=coord, entry=entry)
        assert BoschCamera._token.fget(cam) == ""


# ── _cam_data property ───────────────────────────────────────────────────


class TestCamDataProperty:
    def test_returns_coordinator_cam_dict(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        out = BoschCamera._cam_data.fget(cam)
        assert out["info"]["title"] == "Terrasse"

    def test_returns_empty_dict_for_unknown_cam(self):
        """If the cam disappears from coordinator.data (e.g. after a
        device removal), _cam_data must return {} rather than KeyError
        — every attribute consumer trusts the {} contract."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        coord.data = {}  # cam not in data
        cam = _make_camera(coord=coord)
        assert BoschCamera._cam_data.fget(cam) == {}


# ── _PLACEHOLDER_JPEG class constant ─────────────────────────────────────


class TestPlaceholderJpeg:
    """The 1×1 black JPEG used while the first real snapshot is fetching.
    Without it, HA's camera proxy returns HTTP 500 to the card and the
    user sees a broken-image icon."""

    def test_placeholder_is_valid_jpeg(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        ph = BoschCamera._PLACEHOLDER_JPEG
        assert ph.startswith(b"\xff\xd8")  # JPEG SOI
        assert ph.endswith(b"\xff\xd9")    # JPEG EOI
        # Reasonable size — not a multi-MB photo by accident
        assert 100 < len(ph) < 1000

    def test_placeholder_decodes_via_pil(self):
        """Pin that PIL can actually decode it — a corrupt placeholder
        would crash the rotation path (_rotate_jpeg_180 is the only
        consumer that hits PIL)."""
        from PIL import Image
        from io import BytesIO
        from custom_components.bosch_shc_camera.camera import BoschCamera
        img = Image.open(BytesIO(BoschCamera._PLACEHOLDER_JPEG))
        assert img.size == (1, 1)
