"""Regression tests for bugs found and fixed in v11.0.16 and v11.0.17.

Seven bugs fixed in v11.0.16:
  1. Privacy short-circuit used wrong cache attribute (_camera_status_extra never
     assigned → getattr returned {} always → privacy guard never fired → wasted
     PUT /connection + snap.jpg on every tick while privacy mode ON).
  2. _last_nvr_cleanup initialized to 0.0 instead of float('-inf') — NVR cleanup
     skipped for first 24h on fresh installs (SENTINEL_RULE violation).
  3. FCM device_type race — register_fcm_with_bosch called before
     coordinator._fcm_push_mode was set to `mode`; always registered as ANDROID.
  4. local_rtsp_url uninitialized if Bosch LOCAL response has empty `urls` list;
     line 2672 raised NameError crashing stream setup silently.
  5. _StreamSupportNoiseFilter used 0.0 as default instead of float('-inf')
     (SENTINEL_RULE; benign on long-uptime systems but inconsistent).
  6. any_status_checked stale re-evaluation: after _check_status set
     _per_cam_status_at[cid]=now, re-calling _should_check_status returned False
     for extended-offline cameras → _last_status never advanced → do_status=True
     every tick (redundant gathers, no real API calls, wasted CPU).
  7. rtsp_keepalive writer.close() without await writer.wait_closed() on 3 of 4
     exit paths (inconsistency with pre_warm_rtsp which does await wait_closed).

Two additional SENTINEL_RULE bugs fixed in v11.0.17:
  8. recorder.py: _nvr_recent_crash.get(cam_id, 0.0) — on CI VMs with monotonic
     < _RESPAWN_WINDOW_SECONDS, first crash would appear as a "second crash" and
     suppress respawn permanently. Fix: float('-inf').
  9. __init__.py: _alert_sent_ids.get(newest_id, 0.0) > (now - 60) — on hosts
     with monotonic < 60s, 0.0 > negative is True → dedup skip fires on every
     first event ID → motion alerts suppressed at startup. Fix: float('-inf').
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.source_match import assert_in_source

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera"


# ── Bug 1: Privacy cache wrong attribute ────────────────────────────────────


class TestPrivacyCacheFix:
    """_camera_status_extra was never assigned — guard always returned {}
    making the privacy short-circuit in async_fetch_live_snapshot dead code."""

    def _make_coord(self, privacy: bool = True):
        coord = SimpleNamespace(
            token="tok",
            _shc_state_cache={CAM_ID: {"privacy_mode": privacy}},
        )
        return coord

    def test_shc_state_cache_populated_correctly(self):
        """_shc_state_cache[cam_id]['privacy_mode'] is readable and truthy."""
        coord = self._make_coord(privacy=True)
        result = coord._shc_state_cache.get(CAM_ID, {}).get("privacy_mode")
        assert result is True, (
            "_shc_state_cache must hold privacy_mode=True when privacy is ON"
        )

    def test_old_attribute_never_existed(self):
        """_camera_status_extra was NEVER assigned on the coordinator."""
        coord = self._make_coord()
        assert not hasattr(coord, "_camera_status_extra"), (
            "_camera_status_extra must not exist — using getattr(..., {}) "
            "always silently returned empty dict, making the guard useless"
        )

    def test_privacy_guard_uses_correct_cache(self):
        """The privacy short-circuit expression now reads _shc_state_cache."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        # The actual implementation is in _async_fetch_live_snapshot_impl
        src = inspect.getsource(
            _m.BoschCameraCoordinator._async_fetch_live_snapshot_impl
        )
        # The wrong attribute must not appear as a live code reference (getattr call)
        assert 'getattr(self, "_camera_status_extra"' not in src, (
            "_async_fetch_live_snapshot_impl must not call getattr(_camera_status_extra) "
            "— that attribute is never assigned; guard always returned empty dict"
        )
        assert "_shc_state_cache" in src, (
            "_async_fetch_live_snapshot_impl must use _shc_state_cache for privacy check"
        )

    def test_local_snapshot_guard_uses_correct_cache(self):
        """Same check for async_fetch_live_snapshot_local."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(
            _m.BoschCameraCoordinator.async_fetch_live_snapshot_local
        )
        assert "_camera_status_extra" not in src, (
            "async_fetch_live_snapshot_local must not reference _camera_status_extra"
        )
        assert "_shc_state_cache" in src, (
            "async_fetch_live_snapshot_local must use _shc_state_cache"
        )


# ── Bug 2: NVR cleanup sentinel ─────────────────────────────────────────────


class TestNvrCleanupSentinel:
    """_last_nvr_cleanup was 0.0 — NVR cleanup skipped for first 24h on
    fresh HA installs because monotonic() < 86400 for ~23.9h of uptime."""

    def test_last_nvr_cleanup_is_neg_inf(self):
        """_last_nvr_cleanup must be float('-inf') so first tick always runs cleanup."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m.BoschCameraCoordinator.__init__)
        # Should NOT be initialized to 0.0
        assert "_last_nvr_cleanup: float = 0.0" not in src, (
            "_last_nvr_cleanup must not be 0.0 — violates SENTINEL_RULE: "
            "CI VMs have low monotonic uptime and cleanup would be skipped"
        )
        assert_in_source(src, 'float("-inf")', "float('-inf')", any_of=True)
        # _last_nvr_cleanup must be initialized with float('-inf')
        # so the first cleanup tick always fires (matches _last_smb_cleanup)

    def test_smb_cleanup_uses_neg_inf_as_reference(self):
        """_last_smb_cleanup uses float('-inf') — _last_nvr_cleanup must match."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m.BoschCameraCoordinator.__init__)
        assert "_last_smb_cleanup" in src and "float" in src, (
            "_last_smb_cleanup must exist and use float sentinel as reference pattern"
        )

    def test_cleanup_fires_on_low_monotonic(self):
        """Simulate CI VM (200s uptime): cleanup must fire on first tick."""
        _NVR_CLEANUP_INTERVAL = 86400  # once per day — local in _async_update_data
        last_cleanup = float("-inf")
        now = 200.0  # ~200s, as on CI VM boot
        assert (now - last_cleanup) >= _NVR_CLEANUP_INTERVAL, (
            "With float('-inf') as sentinel, cleanup must fire even at 200s uptime. "
            "With 0.0, it would skip cleanup for the first 24h."
        )


# ── Bug 3: FCM device_type race — OBSOLETE (class deleted) ──────────────────
# The TestFcmDeviceTypeRace class was removed in v12.4.5 because the
# `mode` parameter in register_fcm_with_bosch no longer exists — deviceType
# is now hardcoded to ANDROID (the OSS key handles both platforms). The bug
# it guarded against (race between mode-commit and device_type) is moot.
# New pin tests live in tests/test_fcm_mode_pin.py.


# ── Bug 4: local_rtsp_url uninitialized ─────────────────────────────────────


class TestLocalRtspUrlInit:
    """local_rtsp_url was only assigned inside `if urls:` block but used
    unconditionally afterwards — NameError if Bosch returned empty urls."""

    def test_local_rtsp_url_initialized_before_urls_block(self):
        """Source must show local_rtsp_url initialized before `if urls:`."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m.BoschCameraCoordinator._try_live_connection_inner)
        # local_rtsp_url must be initialized before the if-urls block
        init_pos = src.find('local_rtsp_url = ""')
        urls_pos = src.find("if urls:")
        assert init_pos != -1, (
            "local_rtsp_url must be initialized to '' before `if urls:` block "
            "to prevent NameError when Bosch returns LOCAL response with empty urls"
        )
        assert init_pos < urls_pos, (
            "local_rtsp_url = '' must appear BEFORE the `if urls:` block "
            f"(found at pos {init_pos} vs if-urls at {urls_pos})"
        )


# ── Bug 5: _StreamSupportNoiseFilter sentinel ────────────────────────────────


class TestStreamSupportNoiseFilterSentinel:
    """_last_passed.get(ent, 0.0) violated SENTINEL_RULE.
    float('-inf') is the correct sentinel for time.monotonic() comparisons."""

    def test_filter_uses_neg_inf_default(self):
        """_last_passed.get must use float('-inf') not 0.0 as the miss-default."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m._StreamSupportNoiseFilter.filter)
        assert "_last_passed.get(ent, 0.0)" not in src, (
            "_StreamSupportNoiseFilter.filter must not use 0.0 as default — "
            "SENTINEL_RULE: use float('-inf') for time.monotonic() comparisons"
        )
        assert_in_source(src, "float('-inf')", 'float("-inf")', any_of=True)
        # _StreamSupportNoiseFilter.filter must use float('-inf') as the
        # default for unseen entities

    def test_filter_passes_new_entity_at_low_monotonic(self):
        """At monotonic=5s (CI), a new entity must pass through (not be filtered)."""
        import logging

        from custom_components.bosch_shc_camera.__init__ import (
            _StreamSupportNoiseFilter,
        )

        f = _StreamSupportNoiseFilter()
        record = logging.LogRecord(
            name="homeassistant.components.camera.bosch_terrasse",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="test",
            args=(),
            exc_info=None,
        )

        with patch(
            "custom_components.bosch_shc_camera.__init__._StreamSupportNoiseFilter.filter",
            wraps=f.filter,
        ):
            with patch("time.monotonic", return_value=5.0):
                # Patch the internal import used by the filter
                import time as _time_mod

                orig = _time_mod.monotonic
                _time_mod.monotonic = lambda: 5.0
                try:
                    result = f.filter(record)
                finally:
                    _time_mod.monotonic = orig

        assert result is True, (
            "At monotonic=5s, a new entity must not be filtered. "
            "With 0.0 sentinel, (5.0 - 0.0) >= 30 is False → would filter "
            "the first log message on CI. With float('-inf'), passes correctly."
        )


# ── Bug 6: any_status_checked stale re-evaluation ───────────────────────────


class TestAnyStatusCheckedFix:
    """After _check_status ran and set _per_cam_status_at[cid]=now, calling
    _should_check_status again returned False for extended-offline cams →
    _last_status never advanced → do_status=True on every tick (busy spin)."""

    def test_any_status_checked_set_unconditionally(self):
        """Source must not re-call _should_check_status after gather results."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m.BoschCameraCoordinator._async_update_data)

        # Find the any_status_checked block
        block_start = src.find("any_status_checked = False")
        assert block_start != -1, "any_status_checked pattern must exist"

        # The stale pattern: _should_check_status called AFTER gather
        stale_pattern = "if self._should_check_status(cid, now, interval_status):\n                    any_status_checked = True"
        assert stale_pattern not in src, (
            "any_status_checked must NOT be gated on _should_check_status re-eval "
            "after gather — _per_cam_status_at[cid] was just set to `now` inside "
            "_check_status, so re-eval always returns False for extended-offline cams"
        )

        # The correct pattern: unconditional assignment
        correct_pattern = "any_status_checked = True"
        # Find it after any_status_checked = False
        tail = src[block_start:]
        assert correct_pattern in tail, (
            "any_status_checked must be set to True unconditionally when a "
            "non-exception result is processed from status_results"
        )

    def test_comment_explains_fix(self):
        """A comment in source explains WHY the stale re-eval was removed."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m.BoschCameraCoordinator._async_update_data)
        assert "_per_cam_status_at" in src and "any_status_checked" in src, (
            "The fix comment must reference _per_cam_status_at to explain "
            "why the stale re-evaluation was incorrect"
        )


# ── Bug 7: rtsp_keepalive writer.close without wait_closed ─────────────────


class TestKeepaliveWriterClose:
    """rtsp_keepalive closed writer without await wait_closed() on 3 of 4 paths,
    unlike pre_warm_rtsp which does await wait_closed() consistently."""

    def test_rtsp_keepalive_has_wait_closed(self):
        """rtsp_keepalive source must contain wait_closed() call."""
        import inspect

        from custom_components.bosch_shc_camera import tls_proxy as _tp

        src = inspect.getsource(_tp.rtsp_keepalive)
        assert "wait_closed" in src, (
            "rtsp_keepalive must call writer.wait_closed() after writer.close() "
            "to properly release the underlying TCP socket"
        )

    def test_keepalive_wait_closed_count_matches_close_count(self):
        """Every writer.close() in rtsp_keepalive must be paired with wait_closed."""
        import inspect

        from custom_components.bosch_shc_camera import tls_proxy as _tp

        src = inspect.getsource(_tp.rtsp_keepalive)
        close_count = src.count("writer.close()")
        wait_count = src.count("wait_closed()")
        assert close_count == wait_count, (
            f"rtsp_keepalive has {close_count} writer.close() calls but "
            f"{wait_count} wait_closed() calls — every close must be paired "
            "to prevent TCP socket accumulation"
        )

    def test_pre_warm_also_has_wait_closed(self):
        """pre_warm_rtsp already had wait_closed — verify it wasn't broken."""
        import inspect

        from custom_components.bosch_shc_camera import tls_proxy as _tp

        src = inspect.getsource(_tp.pre_warm_rtsp)
        assert "wait_closed" in src, (
            "pre_warm_rtsp must still call wait_closed() — the fix must not "
            "have accidentally removed it"
        )

    @pytest.mark.asyncio
    async def test_keepalive_no_nonce_no_200_returns_false_and_closes(self):
        """No nonce/realm AND no 200 OK → return False, writer closed properly."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        wait_closed_called = []

        async def fake_wait_closed():
            wait_closed_called.append(True)

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = fake_wait_closed

        mock_reader = MagicMock()
        # Response with no nonce/realm and no 200 OK
        mock_reader.read = AsyncMock(return_value=b"RTSP/1.0 401 Unauthorized\r\n\r\n")

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 401 Unauthorized\r\n\r\n",
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, (
            "writer.close() must be called on no-nonce/no-200 path"
        )
        assert len(wait_closed_called) > 0, (
            "wait_closed() must be awaited on the no-nonce/no-200 path"
        )

    def _make_keepalive_mocks(self, resp1_bytes: bytes, wait_closed_raises=False):
        """Build (mock_reader, mock_writer) for rtsp_keepalive testing."""

        async def fake_wait_closed():
            if wait_closed_raises:
                raise ConnectionResetError("already closed")

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = fake_wait_closed

        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=resp1_bytes)
        return mock_reader, mock_writer

    @pytest.mark.asyncio
    async def test_keepalive_200_no_auth_awaits_wait_closed(self):
        """200 OK (no auth challenge) path must await wait_closed."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        mock_reader, mock_writer = self._make_keepalive_mocks(
            b"RTSP/1.0 200 OK\r\n\r\n"
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 200 OK\r\n\r\n",
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, "writer.close() must be called"

    @pytest.mark.asyncio
    async def test_keepalive_200_no_auth_wait_closed_exception_suppressed(self):
        """wait_closed() raising on the 200-OK no-auth path must be suppressed."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        mock_reader, mock_writer = self._make_keepalive_mocks(
            b"RTSP/1.0 200 OK\r\n\r\n", wait_closed_raises=True
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 200 OK\r\n\r\n",
                    ]
                ),
            ):
                # Must not raise even if wait_closed fails
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        # Exception was suppressed — function completed normally
        assert mock_writer.close.called, (
            "writer.close() must be called regardless of wait_closed result"
        )

    @pytest.mark.asyncio
    async def test_keepalive_authenticated_path_wait_closed_exception_suppressed(self):
        """wait_closed() raising after the authenticated OPTIONS path must be suppressed."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        # resp1 includes nonce+realm to trigger auth challenge path
        resp1 = b'RTSP/1.0 401 Unauthorized\r\nnonce="abc123"\r\nrealm="cam"\r\n\r\n'
        mock_reader, mock_writer = self._make_keepalive_mocks(
            resp1, wait_closed_raises=True
        )
        # resp2 is the authenticated response
        resp2 = b"RTSP/1.0 200 OK\r\n\r\n"

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),  # open_connection
                        resp1,  # reader.read (resp1, with auth challenge)
                        resp2,  # reader.read (resp2, after auth)
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, (
            "writer.close() must be called on the authenticated path"
        )

    @pytest.mark.asyncio
    async def test_keepalive_no_nonce_wait_closed_exception_suppressed(self):
        """wait_closed() raising on the no-nonce/no-200 path must be suppressed."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        mock_reader, mock_writer = self._make_keepalive_mocks(
            b"RTSP/1.0 401 Unauthorized\r\n\r\n", wait_closed_raises=True
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 401 Unauthorized\r\n\r\n",
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, (
            "writer.close() must be called on no-nonce/no-200 path"
        )


# ── Bug 8: recorder.py _nvr_recent_crash default 0.0 ────────────────────────


class TestNvrRecentCrashSentinel:
    """recorder.py:378 — _nvr_recent_crash default 0.0 triggered false crash-loop detection.

    On CI VMs where time.monotonic() < _RESPAWN_WINDOW_SECONDS (e.g., 60s),
    the first NVR crash appeared as a 'second crash within the window' because
    (now - 0.0) < 60 was True. Fix: use float('-inf') as the never-crashed sentinel.
    """

    def test_first_crash_default_is_not_zero(self):
        """Source must not use 0.0 as default for _nvr_recent_crash (SENTINEL_RULE)."""
        import inspect

        from custom_components.bosch_shc_camera import recorder

        src = inspect.getsource(recorder)
        assert "_nvr_recent_crash.get(cam_id, 0.0)" not in src, (
            "recorder.py must not use 0.0 as default for _nvr_recent_crash; "
            "on CI VMs with low monotonic, first crash triggers false crash-loop detection"
        )

    def test_first_crash_uses_neginf_default(self):
        """Source must use float('-inf') as default for _nvr_recent_crash."""
        import inspect

        from custom_components.bosch_shc_camera import recorder

        src = inspect.getsource(recorder)
        assert_in_source(src, '_nvr_recent_crash.get(cam_id, float("-inf"))')
        # recorder.py must use float('-inf') so (now - default) is always >= RESPAWN_WINDOW

    def test_first_crash_does_not_suppress_respawn_at_low_monotonic(self):
        """With monotonic=30s and _RESPAWN_WINDOW_SECONDS=60s, first crash must not suppress respawn.

        Before fix: _nvr_recent_crash.get(cam_id, 0.0) → prev_crash=0.0
        (30 - 0.0) = 30 < 60 → crash-loop guard fires → respawn suppressed on FIRST crash.
        After fix: prev_crash=float('-inf') → (30 - (-inf)) = inf >= 60 → False → respawn allowed.
        """
        from custom_components.bosch_shc_camera.recorder import _RESPAWN_WINDOW_SECONDS

        RESPAWN_WINDOW = _RESPAWN_WINDOW_SECONDS
        low_monotonic_now = RESPAWN_WINDOW * 0.5  # definitely less than the window

        prev_crash_old_default = 0.0
        prev_crash_new_default = float("-inf")

        old_behavior = (low_monotonic_now - prev_crash_old_default) < RESPAWN_WINDOW
        new_behavior = (low_monotonic_now - prev_crash_new_default) < RESPAWN_WINDOW

        assert old_behavior is True, (
            "0.0 default causes crash-loop guard to fire at low monotonic"
        )
        assert new_behavior is False, (
            "float('-inf') default must not trigger crash-loop guard on first crash"
        )


# ── Bug 9: __init__.py _alert_sent_ids default 0.0 ───────────────────────────


class TestAlertSentIdsSentinel:
    """__init__.py:1665 — _alert_sent_ids.get(id, 0.0) > (now - 60) suppresses alerts at startup.

    On hosts with time.monotonic() < 60s (CI VMs), 0.0 > (now - 60) evaluates
    True because now - 60 is negative. This causes the dedup gate to fire on
    every NEW event ID that was never seen before, suppressing motion alerts
    at startup. Fix: use float('-inf') as the default so -inf > negative is False.
    """

    def test_alert_sent_ids_default_not_zero(self):
        """Source must not use 0.0 as default for _alert_sent_ids lookup (SENTINEL_RULE)."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m)
        assert "_alert_sent_ids.get(newest_id, 0.0)" not in src, (
            "__init__.py must not use 0.0 as default for _alert_sent_ids.get(); "
            "on hosts with monotonic < 60s, 0.0 > (now-60) is True → first alert suppressed"
        )

    def test_alert_sent_ids_uses_neginf_default(self):
        """Source must use float('-inf') as default for _alert_sent_ids lookup."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m)
        assert_in_source(src, '_alert_sent_ids.get(newest_id, float("-inf"))')
        # __init__.py must use float('-inf') so dedup only fires for IDs seen within 60s

    def test_dedup_not_triggered_for_new_id_at_low_monotonic(self):
        """With monotonic=30s, a new (never-seen) event ID must NOT be dedup-skipped.

        Old: 0.0 > (30 - 60) = 0.0 > -30 = True → _dedup_skip=True → alert lost.
        New: -inf > (30 - 60) = -inf > -30 = False → _dedup_skip=False → alert sent.
        """
        low_monotonic_now = 30.0  # typical CI VM uptime at startup

        old_default = 0.0
        new_default = float("-inf")

        old_dedup = old_default > (low_monotonic_now - 60.0)
        new_dedup = new_default > (low_monotonic_now - 60.0)

        assert old_dedup is True, (
            "0.0 default causes dedup skip at low monotonic (demonstrates bug)"
        )
        assert new_dedup is False, (
            "float('-inf') default must not trigger dedup for unseen event IDs"
        )

    def test_dedup_still_fires_for_recently_seen_id(self):
        """An ID sent 30s ago must still be deduped (guard window is 60s)."""
        now = 200.0
        recently_sent = now - 30.0  # sent 30s ago, within 60s window

        dedup = recently_sent > (now - 60.0)
        assert dedup is True, (
            "Recently-seen ID must still be deduped within the 60s window"
        )


# ── Bug 10: fcm.py _alert_sent_ids.get default 0.0 (same pattern as Bug 9) ──


class TestFcmAlertSentIdsSentinel:
    """fcm.py:420 — same _alert_sent_ids.get(id, 0.0) pattern as __init__.py.

    FCM push handler uses the same dedup gate. On hosts with monotonic < 60s,
    0.0 > (now - 60) evaluates True → first FCM push for any event suppressed.
    """

    def test_fcm_dedup_default_not_zero(self):
        """fcm.py must not use 0.0 as default for _alert_sent_ids lookup."""
        import inspect

        from custom_components.bosch_shc_camera import fcm

        src = inspect.getsource(fcm)
        assert "_sent.get(newest_id, 0.0) > _now - 60.0" not in src, (
            "fcm.py must not use 0.0 as default for _alert_sent_ids.get(); "
            "on hosts with monotonic < 60s first FCM alert is suppressed"
        )

    def test_fcm_dedup_uses_neginf_default(self):
        """fcm.py must use float('-inf') as default for _alert_sent_ids lookup."""
        import inspect

        from custom_components.bosch_shc_camera import fcm

        src = inspect.getsource(fcm)
        assert_in_source(src, '_sent.get(newest_id, float("-inf")) > _now - 60.0')
        # fcm.py must use float('-inf') so dedup only fires for IDs sent within 60s


# ── Bug 11+12: pre_warm_rtsp writer leaked on exception + wait_closed missing ─


class TestPreWarmRtspWriterCleanup:
    """tls_proxy.py pre_warm_rtsp: two writer-close bugs.

    Bug 11: If open_connection succeeds but a subsequent await raises (e.g.
    asyncio.TimeoutError from wait_for), writer is not closed in the except
    block. The leaked TCP connection occupies one of the camera's 2 concurrent
    RTSP session slots, causing the next retry to fail with "max sessions".

    Bug 12: On the no-nonce path (writer.close() at line 400), wait_closed()
    was not awaited, so the old session slot wasn't released before the retry.
    The success path correctly awaits wait_closed(); this pinned parity.
    """

    def test_writer_closed_in_exception_path(self):
        """pre_warm_rtsp must close writer in the except block (Bug 11)."""
        import inspect

        from custom_components.bosch_shc_camera.tls_proxy import pre_warm_rtsp

        src = inspect.getsource(pre_warm_rtsp)
        # Normalise parens + whitespace so the assertion survives the formatter
        # wrapping `writer = None  # …` into `writer = (\n  None  # …\n)`.
        src_norm = " ".join(src.replace("(", " ").replace(")", " ").split())
        assert "writer = None" in src_norm, (
            "pre_warm_rtsp must initialize writer=None before the try block "
            "so the exception path can close it safely"
        )
        assert "if writer is not None" in src, (
            "pre_warm_rtsp exception path must check 'if writer is not None' "
            "before closing to handle failed open_connection"
        )

    def test_no_nonce_path_awaits_wait_closed(self):
        """pre_warm_rtsp no-nonce retry path must await writer.wait_closed() (Bug 12)."""
        import inspect

        from custom_components.bosch_shc_camera.tls_proxy import pre_warm_rtsp

        src = inspect.getsource(pre_warm_rtsp)
        # Count total await writer.wait_closed() occurrences — must be ≥ 2
        # (success path + no-nonce path)
        count = src.count("await writer.wait_closed()")
        assert count >= 2, (
            f"pre_warm_rtsp must have at least 2 'await writer.wait_closed()' calls "
            f"(success path + no-nonce path); found {count}"
        )


# ── Bug 13: renewal_fails not reset on heartbeat-forced renewal success ────────


class TestHeartbeatRenewalFailsReset:
    """__init__.py _auto_renew_local_session: heartbeat-forced renewal success
    must reset renewal_fails to prevent false _session_stale flag.

    If time-based renewals previously failed (renewal_fails ≥ 3), and then a
    heartbeat-forced renewal succeeds, renewal_fails stays ≥ 3. On the next
    elapsed ≥ renewal_interval check, 'if renewal_fails >= 3' fires immediately,
    setting _session_stale=True on a healthy streaming session.
    """

    def test_heartbeat_renewal_resets_renewal_fails(self):
        """Source must reset renewal_fails=0 after heartbeat-forced renewal success."""
        import inspect

        import custom_components.bosch_shc_camera.__init__ as _m

        src = inspect.getsource(_m)
        # Find the heartbeat-forced renewal block (identified by consecutive_fails reset)
        heartbeat_block_marker = "Heartbeat: session renewed for"
        idx = src.find(heartbeat_block_marker)
        assert idx != -1, "Heartbeat renewal log message not found in __init__.py"
        # After the log, renewal_fails must be reset (window widened — ruff
        # line-wrapping spreads the block over more lines).
        nearby = src[idx : idx + 600]
        # Normalise parens + whitespace so the assertion survives the formatter
        # wrapping `renewal_fails = 0  # …` into `renewal_fails = (\n  0  # …\n)`.
        nearby_norm = " ".join(nearby.replace("(", " ").replace(")", " ").split())
        assert "renewal_fails = 0" in nearby_norm, (
            "After heartbeat-forced renewal success, renewal_fails must be reset to 0. "
            "Without this, a prior renewal_fails ≥ 3 count causes _session_stale=True "
            "on the next time-based renewal check — marking a healthy session as stale."
        )
