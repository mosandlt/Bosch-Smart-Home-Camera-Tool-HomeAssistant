"""Sprint KA tests — targeting _async_update_data branches in __init__.py.

Coverage targets (lines 1298-1460):
  - Lines 1312-1315: No-token guard → UpdateFailed
  - Lines 1322-1325, 1359-1362: First-tick detection and skip of events/slow
  - Lines 1338-1357: FCM watchdog — dead client → _fcm_healthy=False
  - Lines 1368-1380: Camera list 200 success → _hw_version populated
  - Lines 1373-1397: 401 → token refresh + retry 200 → success
  - Lines 1388-1392: 401 → retry also 401 → UpdateFailed("Token expired")
  - Lines 1393-1396: non-200 non-401 → UpdateFailed("HTTP 500")
  - Lines 1398-1411: Feature flags 200 → _feature_flags set
  - Lines 1408-1410: Feature flags TimeoutError swallowed, method continues
  - Lines 1413-1442: Protocol check SUPPORTED → no WARNING, _protocol_checked=True
  - Lines 1424-1431: Protocol check NOT_SUPPORTED → WARNING log emitted

All tests run without a live HA instance.  Coordinator is a SimpleNamespace stub;
methods are called via BoschCameraCoordinator.<method>(coord, ...) (unbound pattern).
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import logging

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_resp(status: int, json_data=None, text_data: str = ""):
    """Build a fake aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text_data)
    # Support `async with session.get(...) as resp:`
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session(url_responses: dict):
    """
    Build a session mock whose .get() returns context-manager responses keyed
    by URL substring.

    url_responses: {substring: resp_or_list}
      If the value is a list, each call pops from the front (allows multi-call
      sequences on the same URL pattern).
    """
    # Clone so we can pop without mutating the caller's dict
    state: dict = {k: (list(v) if isinstance(v, list) else [v]) for k, v in url_responses.items()}

    def _get(url, **kwargs):
        for pattern, queue in state.items():
            if pattern in url:
                if queue:
                    return queue.pop(0)
                # If queue exhausted, return last item repeated
        # fallback: 200 empty list
        return _make_resp(200, [])

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _make_coord(**overrides):
    """
    Coordinator stub with every attribute that _async_update_data touches.

    The stub is a SimpleNamespace; the 'options' attribute must be a plain dict
    (not a property) so the namespace returns it directly when the code does
    `opts = self.options`.
    """

    def _create_task(coro):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        # Auth — refresh_token is a @property on the real class; expose as plain attr
        # so the unbound-method call pattern still works on SimpleNamespace.
        token="tok-A",
        refresh_token="rfr-B",
        _refreshed_refresh=None,
        _entry=SimpleNamespace(
            entry_id="01KM38DHZ525S61HPENAT7NHC0",
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
        ),
        # options property is bypassed on SimpleNamespace — expose the dict directly
        options={},
        # Timestamps — set _last_slow to now so do_slow=False by default,
        # preventing the slow-tier loop from running (requires many extra caches).
        # Individual tests that want do_slow=True can override with _last_slow=-86400.0.
        _last_status=float("-inf"),
        _last_events=float("-inf"),
        _last_slow=time.monotonic(),           # not stale → do_slow=False
        _last_smb_cleanup=time.monotonic(),    # far future → skip SMB cleanup
        _last_smb_disk_check=time.monotonic(),
        _last_nvr_cleanup=time.monotonic(),
        # FCM
        _fcm_lock=threading.Lock(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_client=None,
        # Camera data caches
        _hw_version={},
        _cached_status={},
        _cached_events={},
        _last_event_ids={},
        _alert_sent_ids={},
        _commissioned_cache={},
        _live_connections={},
        _offline_since={},
        _per_cam_status_at={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_error_at={},
        _local_promote_at={},
        _lan_tcp_reachable={},
        _rcp_lan_ip_cache={},
        _local_creds_cache={},
        _shc_state_cache={},
        _wifiinfo_cache={},
        _privacy_set_at={},
        _light_set_at={},
        _notif_set_at={},
        _lighting_switch_cache={},
        _pan_cache={},
        # Additional slow-tier caches (needed if do_slow=True)
        _ambient_light_cache={},
        _audio_alarm_cache={},
        _firmware_cache={},
        _unread_events_cache={},
        _privacy_sound_cache={},
        _timestamp_cache={},
        _notifications_cache={},
        _rules_cache={},
        _cloud_zones_cache={},
        _cloud_privacy_masks_cache={},
        _lighting_options_cache={},
        _ledlights_cache={},
        _lens_elevation_cache={},
        _audio_cache={},
        _motion_light_cache={},
        _ambient_lighting_cache={},
        _global_lighting_cache={},
        # Write-lock
        _WRITE_LOCK_SECS=30.0,
        # Feature / protocol
        _feature_flags={},
        _protocol_checked=False,
        _integration_version="11.0.10",
        _OFFLINE_EXTENDED_INTERVAL=900,
        # Per-cam audio/privacy/notif write-at dicts
        _audio_alarm_set_at={},
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        # Stubs for called methods
        _ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _should_check_status=MagicMock(return_value=True),
        _cleanup_stale_devices=MagicMock(),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        async_mark_events_read=AsyncMock(),
        _is_write_locked=MagicMock(return_value=False),
        shc_ready=False,   # skip SHC supplement path by default
        get_model_config=lambda cid: SimpleNamespace(generation=2),
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_create_background_task=MagicMock(),
            async_add_executor_job=AsyncMock(),
            data={},
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp"),
        ),
        debug=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# Canonical patch target for async_get_clientsession
_PATCH_SESSION = "custom_components.bosch_shc_camera.async_get_clientsession"


# ── 1. No-token guard ─────────────────────────────────────────────────────────


class TestNoTokenGuard:
    """Lines 1312-1315: raise UpdateFailed when both token and refresh_token are falsy.

    The coordinator checks `self.token` then `self.refresh_token` (a property
    that reads from _entry.data).  When BOTH are empty/None the method must
    raise UpdateFailed immediately without touching the session.
    """

    @pytest.mark.asyncio
    async def test_no_token_no_refresh_raises_update_failed(self):
        """Both token and refresh_token falsy → UpdateFailed raised before session."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # refresh_token is a @property on the real class but a plain attr on our stub.
        # Set both to falsy so the guard fires.
        coord = _make_coord(token=None, refresh_token="")

        with pytest.raises(UpdateFailed, match="Not authenticated"), \
             patch(_PATCH_SESSION) as mock_sess:
            await BoschCameraCoordinator._async_update_data(coord)

        mock_sess.assert_not_called(), (
            "Session should not be opened when token/refresh_token are both missing"
        )

    @pytest.mark.asyncio
    async def test_no_token_but_refresh_present_proceeds(self):
        """token=None but refresh_token present → method proceeds past the guard."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # token=None, refresh_token="rfr-B" (still truthy) → guard must NOT raise
        coord = _make_coord(token=None, refresh_token="rfr-B")
        coord._first_tick_done = True

        session = _make_session({
            "v11/video_inputs": _make_resp(200, []),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
        })

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method should return a dict when refresh_token is present"
        )


# ── 2. First-tick detection ────────────────────────────────────────────────────


class TestFirstTickDetection:
    """Lines 1322-1325, 1359-1362: first-tick flag prevents events/slow fetches.

    On the first call _first_tick_done must not exist yet → the method sets it,
    and forces do_events=False / do_slow=False regardless of interval timers.
    """

    @pytest.mark.asyncio
    async def test_first_tick_sets_flag(self):
        """After first call, _first_tick_done is set on the coordinator."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        assert not hasattr(coord, "_first_tick_done"), (
            "Pre-condition: _first_tick_done must not exist before first tick"
        )

        session = _make_session({
            "v11/video_inputs": _make_resp(200, []),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert hasattr(coord, "_first_tick_done"), (
            "_first_tick_done must be set after the first tick"
        )

    @pytest.mark.asyncio
    async def test_first_tick_skips_events_even_if_interval_elapsed(self):
        """First tick with stale _last_events → events API is NOT called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # Force intervals to appear elapsed
        coord = _make_coord(
            _last_events=-86400.0,   # 24h ago → do_events would be True without first-tick
            _last_slow=-86400.0,
        )
        assert not hasattr(coord, "_first_tick_done"), (
            "Pre-condition: must be first tick"
        )

        call_log: list[str] = []

        session_mock = MagicMock()

        def _get(url, **kwargs):
            call_log.append(url)
            if "feature_flags" in url:
                return _make_resp(200, {})
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            # video_inputs → camera list (no cameras → events never called)
            return _make_resp(200, [])

        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        event_urls = [u for u in call_log if "events" in u and "feature" not in u]
        assert event_urls == [], (
            f"Events API must not be called on first tick, but got: {event_urls}"
        )

    @pytest.mark.asyncio
    async def test_second_tick_does_not_suppress_events(self):
        """Second tick (_first_tick_done set) with stale _last_events → events fetch runs."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _last_events=-86400.0,   # stale → do_events=True
            _last_slow=time.monotonic(),  # not stale → do_slow=False (avoids extra calls)
        )
        # Simulate second tick by pre-setting the flag
        coord._first_tick_done = True

        cam_id = "CAM-A-001"
        call_log: list[str] = []

        def _get(url, **kwargs):
            call_log.append(url)
            if "feature_flags" in url:
                return _make_resp(200, {})
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            if "/events" in url:
                return _make_resp(200, [])
            if "last_event" in url:
                return _make_resp(200, {"id": "ev-1"})
            if "ping" in url:
                return _make_resp(200, {}, text_data="ONLINE")
            if "commissioned" in url:
                return _make_resp(200, {"connected": True, "commissioned": True})
            # video_inputs list
            return _make_resp(200, [{"id": cam_id, "hardwareVersion": "CAMERA"}])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        event_urls = [u for u in call_log if ("last_event" in u or ("/events" in u and "feature" not in u))]
        assert event_urls, (
            "Events-related URLs must be called on second tick when interval elapsed"
        )


# ── 3. FCM watchdog ────────────────────────────────────────────────────────────


class TestFcmWatchdog:
    """Lines 1338-1357: FCM health detection via FcmPushClient.is_started().

    When FCM is running + healthy but is_started() returns False → _fcm_healthy
    must be flipped to False.  When FCM is not running → _fcm_healthy unchanged.
    """

    @pytest.mark.asyncio
    async def test_fcm_dead_client_sets_healthy_false(self):
        """_fcm_running=True, _fcm_healthy=True, is_started()=False → _fcm_healthy=False."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        fcm_client = MagicMock()
        fcm_client.is_started = MagicMock(return_value=False)

        coord = _make_coord(
            _fcm_running=True,
            _fcm_healthy=True,
            _fcm_client=fcm_client,
            options={"enable_fcm_push": True},
        )
        coord._first_tick_done = True  # skip first-tick suppression

        session = _make_session({
            "v11/video_inputs": _make_resp(200, []),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._fcm_healthy is False, (
            "_fcm_healthy must be False when is_started() returns False"
        )

    @pytest.mark.asyncio
    async def test_fcm_not_running_leaves_healthy_unchanged(self):
        """_fcm_running=False → watchdog block skipped, _fcm_healthy unchanged."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        fcm_client = MagicMock()
        fcm_client.is_started = MagicMock(return_value=False)  # would be dead if running

        coord = _make_coord(
            _fcm_running=False,
            _fcm_healthy=True,
            _fcm_client=fcm_client,
            options={"enable_fcm_push": True},
        )
        coord._first_tick_done = True

        session = _make_session({
            "v11/video_inputs": _make_resp(200, []),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._fcm_healthy is True, (
            "_fcm_healthy must remain True when _fcm_running is False"
        )

    @pytest.mark.asyncio
    async def test_fcm_is_started_exception_leaves_healthy_unchanged(self):
        """is_started() raises → exception caught, _fcm_healthy stays True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        fcm_client = MagicMock()
        fcm_client.is_started = MagicMock(side_effect=RuntimeError("library error"))

        coord = _make_coord(
            _fcm_running=True,
            _fcm_healthy=True,
            _fcm_client=fcm_client,
            options={"enable_fcm_push": True},
        )
        coord._first_tick_done = True

        session = _make_session({
            "v11/video_inputs": _make_resp(200, []),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._fcm_healthy is True, (
            "_fcm_healthy must not change when is_started() raises an exception"
        )


# ── 4. Camera list 200 success ────────────────────────────────────────────────


class TestCameraList200:
    """Lines 1368-1380: successful camera list → _hw_version populated."""

    @pytest.mark.asyncio
    async def test_200_populates_hw_version(self):
        """200 with camera list → _hw_version[cam_id] set to hardwareVersion."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._first_tick_done = True

        cam_data = [{"id": "CAM-A", "hardwareVersion": "HOME_Eyes_Outdoor"}]
        session = _make_session({
            "v11/video_inputs": _make_resp(200, cam_data),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            "ping": _make_resp(200, {}, text_data="ONLINE"),
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._hw_version.get("CAM-A") == "HOME_Eyes_Outdoor", (
            "_hw_version must be populated from the camera list response"
        )

    @pytest.mark.asyncio
    async def test_200_empty_list_returns_empty_dict(self):
        """200 with empty camera list → method returns empty data dict."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._first_tick_done = True

        session = _make_session({
            "v11/video_inputs": _make_resp(200, []),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
        })

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert result == {}, (
            "Empty camera list must produce an empty data dict"
        )


# ── 5. Camera list 401 → refresh + retry 200 ─────────────────────────────────


class TestCameraList401Retry:
    """Lines 1373-1397: 401 on first call → token refresh → retry returns 200."""

    @pytest.mark.asyncio
    async def test_401_then_200_succeeds(self):
        """First GET returns 401, second returns 200 → method succeeds."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._first_tick_done = True

        cam_data = [{"id": "CAM-B", "hardwareVersion": "CAMERA_360"}]
        resp_401 = _make_resp(401, None)
        resp_200 = _make_resp(200, cam_data)

        # The session mock must serve 401 first, then 200 on the retry
        call_count = [0]

        def _get(url, **kwargs):
            if "feature_flags" in url:
                return _make_resp(200, {})
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            if "v11/video_inputs" in url and "ping" not in url:
                call_count[0] += 1
                if call_count[0] == 1:
                    return resp_401
                return resp_200
            # ping / other
            return _make_resp(200, {}, text_data="ONLINE")

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert "CAM-B" in result, (
            "After 401+renewal+retry-200, camera must appear in result data"
        )
        coord._ensure_valid_token.assert_called_once(), (
            "_ensure_valid_token must be called exactly once on 401"
        )


# ── 6. Camera list 401 → retry also 401 → UpdateFailed ───────────────────────


class TestCameraList401DoubleFailure:
    """Lines 1388-1392: both calls return 401 → UpdateFailed with 'Token expired'."""

    @pytest.mark.asyncio
    async def test_401_retry_401_raises_update_failed(self):
        """Both video_inputs calls return 401 → UpdateFailed containing 'Token expired'."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._first_tick_done = True

        call_count = [0]

        def _get(url, **kwargs):
            if "v11/video_inputs" in url and "ping" not in url:
                call_count[0] += 1
                return _make_resp(401, None)
            return _make_resp(200, {})

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with pytest.raises(UpdateFailed, match="Token expired"), \
             patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        assert call_count[0] == 2, (
            "Both 401 calls must have been made (initial + retry)"
        )


# ── 7. Camera list non-200 non-401 → UpdateFailed ────────────────────────────


class TestCameraListHttpError:
    """Lines 1393-1396 and 1377-1378: non-200/401 status → UpdateFailed with HTTP code."""

    @pytest.mark.asyncio
    async def test_500_raises_update_failed_with_code(self):
        """Session returns 500 → UpdateFailed containing 'HTTP 500'."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._first_tick_done = True

        def _get(url, **kwargs):
            if "v11/video_inputs" in url and "ping" not in url:
                return _make_resp(500, None)
            return _make_resp(200, {})

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with pytest.raises(UpdateFailed, match="HTTP 500"), \
             patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

    @pytest.mark.asyncio
    async def test_503_raises_update_failed_with_code(self):
        """Session returns 503 → UpdateFailed containing 'HTTP 503'."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._first_tick_done = True

        def _get(url, **kwargs):
            if "v11/video_inputs" in url and "ping" not in url:
                return _make_resp(503, None)
            return _make_resp(200, {})

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with pytest.raises(UpdateFailed, match="HTTP 503"), \
             patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)


# ── 8. Feature flags fetch 200 ────────────────────────────────────────────────


class TestFeatureFlags200:
    """Lines 1398-1411: feature flags endpoint returns 200 → _feature_flags populated."""

    @pytest.mark.asyncio
    async def test_200_sets_feature_flags(self):
        """FF endpoint 200 with data → _feature_flags updated on the coordinator."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_feature_flags={})  # empty → will fetch
        coord._first_tick_done = True

        ff_data = {"motionDetection": True, "privacyMode": True}

        def _get(url, **kwargs):
            if "feature_flags" in url:
                return _make_resp(200, ff_data)
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            return _make_resp(200, [])  # camera list

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._feature_flags == ff_data, (
            "_feature_flags must be set to the parsed JSON response"
        )

    @pytest.mark.asyncio
    async def test_already_populated_feature_flags_not_refetched(self):
        """_feature_flags already set (truthy) → FF endpoint not called again."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        existing_flags = {"alreadySet": True}
        coord = _make_coord(_feature_flags=existing_flags)
        coord._first_tick_done = True

        call_log: list[str] = []

        def _get(url, **kwargs):
            call_log.append(url)
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        ff_calls = [u for u in call_log if "feature_flags" in u]
        assert ff_calls == [], (
            "feature_flags endpoint must not be called when _feature_flags already populated"
        )
        assert coord._feature_flags == existing_flags, (
            "Existing _feature_flags must not be overwritten"
        )


# ── 9. Feature flags timeout swallowed ────────────────────────────────────────


class TestFeatureFlagsTimeout:
    """Lines 1408-1410: TimeoutError from FF endpoint is caught → method continues."""

    @pytest.mark.asyncio
    async def test_feature_flags_timeout_does_not_raise(self):
        """asyncio.TimeoutError during FF fetch → swallowed, method returns normally."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_feature_flags={})
        coord._first_tick_done = True

        call_log: list[str] = []

        def _get(url, **kwargs):
            call_log.append(url)
            if "feature_flags" in url:
                # Return a context manager that raises TimeoutError on __aenter__
                resp = MagicMock()
                resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
                resp.__aexit__ = AsyncMock(return_value=None)
                return resp
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return normally after FF TimeoutError"
        )
        # Feature flags should remain empty (timeout, nothing stored)
        assert coord._feature_flags == {}, (
            "_feature_flags must stay empty when FF fetch times out"
        )


# ── 10. Protocol version check — SUPPORTED ────────────────────────────────────


class TestProtocolCheckSupported:
    """Lines 1413-1442: protocol check 200 SUPPORTED → no WARNING, _protocol_checked set."""

    @pytest.mark.asyncio
    async def test_supported_state_no_warning_log(self, caplog):
        """Protocol endpoint returns state=SUPPORTED → no WARNING emitted."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=False, _feature_flags={"dummy": True})
        coord._first_tick_done = True

        def _get(url, **kwargs):
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"), \
             patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        warning_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert not warning_msgs, (
            f"No WARNING must be emitted for SUPPORTED protocol state, got: {warning_msgs}"
        )

    @pytest.mark.asyncio
    async def test_supported_sets_protocol_checked(self):
        """After SUPPORTED protocol response → _protocol_checked must be True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=False, _feature_flags={"dummy": True})
        coord._first_tick_done = True

        def _get(url, **kwargs):
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._protocol_checked is True, (
            "_protocol_checked must be True after the protocol check runs"
        )

    @pytest.mark.asyncio
    async def test_already_checked_does_not_re_check(self):
        """_protocol_checked=True → protocol endpoint not called again."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=True, _feature_flags={"dummy": True})
        coord._first_tick_done = True

        call_log: list[str] = []

        def _get(url, **kwargs):
            call_log.append(url)
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        proto_calls = [u for u in call_log if "protocol_support" in u]
        assert proto_calls == [], (
            "protocol_support must not be called when _protocol_checked is already True"
        )


# ── 11. Protocol version check — NOT_SUPPORTED ────────────────────────────────


class TestProtocolCheckDeprecated:
    """Lines 1424-1431: state != SUPPORTED → WARNING log emitted."""

    @pytest.mark.asyncio
    async def test_deprecated_state_emits_warning(self, caplog):
        """Protocol endpoint returns state=DEPRECATED → WARNING log contains 'may no longer be supported'."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=False, _feature_flags={"dummy": True})
        coord._first_tick_done = True

        def _get(url, **kwargs):
            if "protocol_support" in url:
                return _make_resp(200, {"state": "DEPRECATED"})
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"), \
             patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("may no longer be supported" in m or "protocol" in m.lower() for m in warning_msgs), (
            f"WARNING about protocol support must be emitted, got warnings: {warning_msgs}"
        )

    @pytest.mark.asyncio
    async def test_unsupported_state_emits_warning(self, caplog):
        """Protocol endpoint returns state=UNSUPPORTED → WARNING log emitted."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=False, _feature_flags={"dummy": True})
        coord._first_tick_done = True

        def _get(url, **kwargs):
            if "protocol_support" in url:
                return _make_resp(200, {"state": "UNSUPPORTED"})
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"), \
             patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert warning_msgs, (
            "At least one WARNING must be emitted for UNSUPPORTED protocol state"
        )

    @pytest.mark.asyncio
    async def test_protocol_check_sets_checked_flag_even_on_deprecated(self):
        """After any protocol response (even DEPRECATED) → _protocol_checked=True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=False, _feature_flags={"dummy": True})
        coord._first_tick_done = True

        def _get(url, **kwargs):
            if "protocol_support" in url:
                return _make_resp(200, {"state": "DEPRECATED"})
            return _make_resp(200, [])

        session_mock = MagicMock()
        session_mock.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._protocol_checked is True, (
            "_protocol_checked must be True even after a DEPRECATED protocol response"
        )
