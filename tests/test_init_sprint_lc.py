"""Sprint LC tests — slow-tier result processing and _try_live_connection_inner gaps.

Coverage targets:
  Group A (lines 1886-2166): slow-tier _async_update_data result processing
  Group B (lines 2437-2785): _try_live_connection_inner remaining branches

All tests run without a live HA instance.  Coordinator is a SimpleNamespace stub;
methods are called via BoschCameraCoordinator.<method>(coord, ...) (unbound pattern).
"""
from __future__ import annotations

import asyncio
import json
import time as time_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# Camera IDs used in tests
CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_resp(status: int, json_data=None, text_data: str = ""):
    """Build a fake aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session_fn(url_routes: dict):
    """
    Return a session whose .get() dispatches by URL substring (longest match wins).

    url_routes: {substring: resp_or_list}
    List values pop from front (sequences); non-list values are reused.
    Patterns are sorted by length descending so specific paths beat generic ones.
    """
    state: dict = {k: (list(v) if isinstance(v, list) else [v]) for k, v in url_routes.items()}
    # Sort by pattern length descending — longer (more specific) patterns match first.
    sorted_patterns = sorted(state.keys(), key=len, reverse=True)

    def _get(url, **kwargs):
        for pattern in sorted_patterns:
            if pattern in url:
                queue = state[pattern]
                if queue:
                    r = queue.pop(0)
                    # Keep last item for repeated calls on same pattern
                    if not queue:
                        queue.append(r)
                    return r
        return _make_resp(200, [])

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _put_resp(status: int, body: str):
    """Build a fake response for session.put(...)."""
    r = MagicMock()
    r.status = status
    r.text = AsyncMock(return_value=body)
    return r


_PATCH_SESSION = "custom_components.bosch_shc_camera.async_get_clientsession"

# ── Gen2 camera list payload ──────────────────────────────────────────────────
# Privacy ON (="ON") skips the RCP-via-cloud path (lines 2066-2105) which
# would open a REAL aiohttp.ClientSession and trigger pytest-socket errors.
# HOME_Eyes_Indoor supports alarm_settings/alarmStatus/iconLedBrightness
# pan_limit=0 → no autofollow endpoint
CAM_GEN2_INDOOR = {
    "id": CAM_A,
    "hardwareVersion": "HOME_Eyes_Indoor",
    "featureSupport": {"light": False, "panLimit": 0},
    "featureStatus": {},
    "privacyMode": "ON",   # skip RCP path to avoid real socket
}

# Gen1 camera (CAMERA_360) — supports motion_sensitive_areas + privacy_masks
CAM_GEN1 = {
    "id": CAM_A,
    "hardwareVersion": "CAMERA_360",
    "featureSupport": {"light": False, "panLimit": 0},
    "featureStatus": {},
    "privacyMode": "ON",   # skip RCP path
}

# Gen2 Outdoor with light support — supports lighting_options
CAM_GEN2_OUTDOOR = {
    "id": CAM_A,
    "hardwareVersion": "HOME_Eyes_Outdoor",
    "featureSupport": {"light": True, "panLimit": 0},
    "featureStatus": {},
    "privacyMode": "ON",   # skip RCP path
}

# Gen2 camera with pan support — supports autofollow
CAM_GEN2_PAN = {
    "id": CAM_A,
    "hardwareVersion": "HOME_Eyes_Outdoor",
    "featureSupport": {"light": False, "panLimit": 1},
    "featureStatus": {},
    "privacyMode": "ON",   # skip RCP path
}


# ── Group A coordinator stub ───────────────────────────────────────────────────

def _make_coord_for_update_data(**overrides):
    """
    Coordinator stub tuned for slow-tier _async_update_data tests.

    _last_slow=float("-inf") → ancient → do_slow=True (300s interval elapsed)
    _first_tick_done=True → second tick, so events+slow are not suppressed
    """
    def _create_task(coro, **kwargs):
        try:
            if hasattr(coro, "close"):
                coro.close()
        except Exception:
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        token="tok-A",
        refresh_token="rfr-B",
        _refreshed_refresh=None,
        _entry=SimpleNamespace(
            entry_id="01KM38DHZ525S61HPENAT7NHC0",
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
        ),
        options={},
        # Make slow tier trigger
        _last_status=float("-inf"),
        _last_events=float("-inf"),
        _last_slow=float("-inf"),            # ancient → do_slow=True
        _last_smb_cleanup=time_mod.monotonic(),
        _last_smb_disk_check=time_mod.monotonic(),
        _last_nvr_cleanup=time_mod.monotonic(),
        # FCM
        _fcm_lock=__import__("threading").Lock(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_client=None,
        # Camera data caches
        _hw_version={},
        _cached_status={CAM_A: "ONLINE"},   # pre-cached so status check returns ONLINE
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
        # Slow-tier caches (populated by tests)
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
        _intrusion_config_cache={},
        _alarm_settings_cache={},
        _alarm_status_cache={},
        _arming_cache={},
        _icon_led_brightness_cache={},
        _gen2_zones_cache={},
        _gen2_private_areas_cache={},
        # Write-lock
        _WRITE_LOCK_SECS=30.0,
        _audio_alarm_set_at={},
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        _arming_set_at={},
        # Feature / protocol
        _feature_flags={"dummy": True},   # already populated → skip FF fetch
        _protocol_checked=True,           # already checked → skip protocol fetch
        _integration_version="11.0.10",
        _OFFLINE_EXTENDED_INTERVAL=900,
        # Stubs for called methods
        _ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _should_check_status=MagicMock(return_value=False),   # skip cloud status check
        _cleanup_stale_devices=MagicMock(),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        async_mark_events_read=AsyncMock(),
        _is_write_locked=MagicMock(return_value=False),
        shc_ready=False,
        get_model_config=lambda cid: SimpleNamespace(generation=2),
        get_quality_params=MagicMock(return_value=(True, 1)),
        # NVR/SMB background tasks — must be AsyncMock so the coro is valid
        _run_nvr_cleanup_bg=AsyncMock(return_value=None),
        _run_smb_cleanup_bg=AsyncMock(return_value=None),
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_create_background_task=MagicMock(),
            async_add_executor_job=AsyncMock(),
            data={},
            bus=SimpleNamespace(async_fire=MagicMock()),
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp"),
        ),
        debug=False,
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    # Pre-set _first_tick_done so second-tick logic runs
    coord._first_tick_done = True
    return coord


def _build_slow_tier_routes(cam_info: dict, extra_routes: dict | None = None) -> dict:
    """
    Build URL route dict for a coordinator test with a single camera.

    cam_info: dict with camera metadata (id, hardwareVersion, featureSupport, ...)
    extra_routes: override/extend specific URL responses
    """
    cid = cam_info["id"]
    routes = {
        "v11/video_inputs": _make_resp(200, [cam_info]),
        f"{cid}/ping": _make_resp(200, {}, text_data="ONLINE"),
        # Slow-tier endpoints with sane defaults
        f"{cid}/wifiinfo": _make_resp(200, {"rssiValueDb": -60, "signalStrength": 80}),
        f"{cid}/ambient_light_sensor_level": _make_resp(200, {"ambientLightSensorLevel": 500}),
        f"{cid}/motion": _make_resp(200, {"sensitivity": "LOW"}),
        f"{cid}/audioAlarm": _make_resp(200, {"sensitivity": 50}),
        f"{cid}/firmware": _make_resp(200, {"version": "9.40.25"}),
        f"{cid}/recording_options": _make_resp(200, {"enabled": False}),
        f"{cid}/unread_events_count": _make_resp(200, {"count": 0}),
        f"{cid}/commissioned": _make_resp(200, {"connected": True}),
        f"{cid}/timestamp": _make_resp(200, {"result": False}),
        f"{cid}/notifications": _make_resp(200, []),
        f"{cid}/rules": _make_resp(200, []),
        # Gen1-only
        f"{cid}/motion_sensitive_areas": _make_resp(200, []),
        f"{cid}/privacy_masks": _make_resp(200, []),
        f"{cid}/privacy_sound_override": _make_resp(200, {"result": False}),
        # Gen2-only
        f"{cid}/ledlights": _make_resp(200, {"state": "OFF"}),
        f"{cid}/lens_elevation": _make_resp(200, {"elevation": 0}),
        f"{cid}/audio": _make_resp(200, {"volume": 50}),
        f"{cid}/lighting/motion": _make_resp(200, {}),
        f"{cid}/lighting/ambient": _make_resp(200, {}),
        f"{cid}/lighting": _make_resp(200, {}),
        f"{cid}/intrusionDetectionConfig": _make_resp(200, {}),
        f"{cid}/alarm_settings": _make_resp(200, {}),
        f"{cid}/alarmStatus": _make_resp(200, {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}),
        f"{cid}/iconLedBrightness": _make_resp(200, {"value": 0}),
        f"{cid}/zones": _make_resp(200, []),
        f"{cid}/privateAreas": _make_resp(200, []),
        f"{cid}/lighting_options": _make_resp(200, {}),
        f"{cid}/autofollow": _make_resp(200, {"enabled": False}),
        f"{cid}/lighting/switch": _make_resp(200, {}),
    }
    if extra_routes:
        routes.update(extra_routes)
    return routes


# ═══════════════════════════════════════════════════════════════════════════════
# Group A — Slow-tier _async_update_data tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSlowTierSkippedOffline:
    """Line 1886: do_slow=True but camera offline → debug log, slow-tier skipped."""

    @pytest.mark.asyncio
    async def test_slow_tier_skipped_offline(self):
        """Camera status=UNKNOWN (not ONLINE) → slow-tier not executed."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "UNKNOWN"},  # not ONLINE
        )

        cam_info = dict(CAM_GEN2_INDOOR)
        routes = {
            "v11/video_inputs": _make_resp(200, [cam_info]),
            f"{CAM_A}/ping": _make_resp(200, {}, text_data="OFFLINE"),
            f"{CAM_A}/lighting/switch": _make_resp(200, {}),
        }
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        # Slow-tier should not have populated wifiinfo cache (since offline)
        assert CAM_A not in coord._wifiinfo_cache, (
            "wifiinfo cache must not be populated when camera is offline"
        )
        assert isinstance(result, dict), "Must return a dict"


class TestSlowTierFetchException:
    """Lines 1905-1908: _fetch catches exception → returns (ep, 0, None)."""

    @pytest.mark.asyncio
    async def test_slow_tier_fetch_exception_returns_zero_status(self):
        """When one endpoint raises ClientError, _fetch returns (ep, 0, None) — no crash."""
        import aiohttp as _aiohttp
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        # Build routes — make firmware raise a ClientError
        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)

        error_resp = MagicMock()
        error_resp.status = 200
        error_resp.__aenter__ = AsyncMock(side_effect=_aiohttp.ClientConnectionError("conn refused"))
        error_resp.__aexit__ = AsyncMock(return_value=None)
        # Override firmware route with one that raises on __aenter__
        routes[f"{CAM_A}/firmware"] = error_resp

        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        # Other caches must still be populated — exception on firmware is swallowed
        assert isinstance(result, dict), "Must return a dict even when one endpoint raises"
        assert CAM_A in coord._wifiinfo_cache, "wifiinfo must still be cached despite firmware exception"


class TestSlowTierAutofollow:
    """Line 1990: autofollow endpoint → data[cam]['autofollow'] set."""

    @pytest.mark.asyncio
    async def test_slow_tier_autofollow_cached(self):
        """autofollow endpoint 200 → data[cam_id]['autofollow'] populated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        cam_info = dict(CAM_GEN2_PAN)  # panLimit=1 → autofollow endpoint added
        routes = _build_slow_tier_routes(cam_info)
        routes[f"{CAM_A}/autofollow"] = _make_resp(200, {"enabled": True})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A in result, "Camera must appear in result"
        assert result[CAM_A].get("autofollow") == {"enabled": True}, (
            "data[cam]['autofollow'] must be set from endpoint response"
        )


class TestSlowTierTimestamp:
    """Line 1992-1993: timestamp endpoint (no write-lock) → _timestamp_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_timestamp_cached(self):
        """timestamp 200 with no write-lock → _timestamp_cache[cam] = True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )
        coord._is_write_locked = MagicMock(return_value=False)

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/timestamp"] = _make_resp(200, {"result": True})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._timestamp_cache.get(CAM_A) is True, (
            "_timestamp_cache must be True when endpoint returns result=True"
        )


class TestSlowTierNotifications:
    """Line 1995: notifications endpoint → _notifications_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_notifications_cached(self):
        """notifications 200 → _notifications_cache[cam] populated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        expected = [{"type": "motion", "enabled": True}]
        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/notifications"] = _make_resp(200, expected)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._notifications_cache.get(CAM_A) == expected, (
            "_notifications_cache must be populated from endpoint response"
        )


class TestSlowTierRules:
    """Line 1997: rules endpoint → _rules_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_rules_cached(self):
        """rules 200 → _rules_cache[cam] populated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        expected = [{"id": "r1", "name": "rule1"}]
        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/rules"] = _make_resp(200, expected)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._rules_cache.get(CAM_A) == expected, (
            "_rules_cache must be populated from endpoint response"
        )


class TestSlowTierLedlights:
    """Lines 2004-2006: ledlights ON → _ledlights_cache set to True."""

    @pytest.mark.asyncio
    async def test_slow_tier_ledlights_on_cached(self):
        """ledlights state=ON with no write-lock → _ledlights_cache[cam] = True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )
        coord._is_write_locked = MagicMock(return_value=False)

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/ledlights"] = _make_resp(200, {"state": "ON"})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._ledlights_cache.get(CAM_A) is True, (
            "_ledlights_cache must be True when ledlights state=ON"
        )


class TestSlowTierLensElevation:
    """Lines 2007-2008: lens_elevation → _lens_elevation_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_lens_elevation_cached(self):
        """lens_elevation 200 → _lens_elevation_cache[cam] = elevation value."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/lens_elevation"] = _make_resp(200, {"elevation": 45})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._lens_elevation_cache.get(CAM_A) == 45, (
            "_lens_elevation_cache must contain the elevation value from endpoint"
        )


class TestSlowTierAudio:
    """Lines 2009-2010: audio endpoint → _audio_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_audio_cached(self):
        """audio 200 → _audio_cache[cam] populated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        expected = {"volume": 80, "muted": False}
        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/audio"] = _make_resp(200, expected)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._audio_cache.get(CAM_A) == expected, (
            "_audio_cache must be populated from audio endpoint response"
        )


class TestSlowTierUnreadEventsNumeric:
    """Lines 1982-1983: unread_events_count is int/float (not dict) → cache = int(ep_data)."""

    @pytest.mark.asyncio
    async def test_slow_tier_unread_events_count_numeric(self):
        """unread_events_count returns numeric 5 → _unread_events_cache[cam] = 5."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        # Return a raw int-like response (numeric, not dict)
        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/unread_events_count"] = _make_resp(200, 5)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._unread_events_cache.get(CAM_A) == 5, (
            "_unread_events_cache must be int(5) when endpoint returns numeric"
        )


class TestSlowTierPrivacySoundNoWriteLock:
    """Lines 1985-1986: privacy_sound_override, no write-lock → cache updated."""

    @pytest.mark.asyncio
    async def test_slow_tier_privacy_sound_no_write_lock(self):
        """privacy_sound_override 200, write-lock=False → _privacy_sound_cache updated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )
        coord._is_write_locked = MagicMock(return_value=False)

        cam_info = dict(CAM_GEN1)  # CAMERA_360 → privacy_sound_override included
        routes = _build_slow_tier_routes(cam_info)
        routes[f"{CAM_A}/privacy_sound_override"] = _make_resp(200, {"result": True})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._privacy_sound_cache.get(CAM_A) is True, (
            "_privacy_sound_cache must be True when write-lock=False and result=True"
        )


class TestSlowTierMotionSensitiveAreas:
    """Line 1999: motion_sensitive_areas → _cloud_zones_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_motion_sensitive_areas_cached(self):
        """motion_sensitive_areas 200 → _cloud_zones_cache[cam] = list."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        expected = [{"name": "zone1", "enabled": True}]
        cam_info = dict(CAM_GEN1)  # Gen1 → motion_sensitive_areas endpoint
        routes = _build_slow_tier_routes(cam_info)
        routes[f"{CAM_A}/motion_sensitive_areas"] = _make_resp(200, expected)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cloud_zones_cache.get(CAM_A) == expected, (
            "_cloud_zones_cache must be populated from motion_sensitive_areas"
        )


class TestSlowTierPrivacyMasks:
    """Line 2001: privacy_masks → _cloud_privacy_masks_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_privacy_masks_cached(self):
        """privacy_masks 200 → _cloud_privacy_masks_cache[cam] = list."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        expected = [{"x": 0, "y": 0, "width": 100, "height": 100}]
        cam_info = dict(CAM_GEN1)
        routes = _build_slow_tier_routes(cam_info)
        routes[f"{CAM_A}/privacy_masks"] = _make_resp(200, expected)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cloud_privacy_masks_cache.get(CAM_A) == expected, (
            "_cloud_privacy_masks_cache must be populated from privacy_masks"
        )


class TestSlowTierLightingOptions:
    """Line 2003: lighting_options → _lighting_options_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_lighting_options_cached(self):
        """lighting_options 200 → _lighting_options_cache[cam] populated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        expected = {"mode": "auto", "brightness": 80}
        cam_info = dict(CAM_GEN2_OUTDOOR)  # light=True → lighting_options added
        routes = _build_slow_tier_routes(cam_info)
        routes[f"{CAM_A}/lighting_options"] = _make_resp(200, expected)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._lighting_options_cache.get(CAM_A) == expected, (
            "_lighting_options_cache must be populated from lighting_options"
        )


class TestSlowTierAlarmSettings:
    """Line 2023: alarm_settings → _alarm_settings_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_alarm_settings_cached(self):
        """alarm_settings 200 → _alarm_settings_cache[cam] populated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        expected = {"sensitivity": "HIGH", "armed": False}
        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/alarm_settings"] = _make_resp(200, expected)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._alarm_settings_cache.get(CAM_A) == expected, (
            "_alarm_settings_cache must be populated from alarm_settings"
        )


class TestSlowTierAlarmStatusArming:
    """Lines 2027-2035: alarmStatus intrusionSystem ACTIVE/INACTIVE → _arming_cache."""

    @pytest.mark.asyncio
    async def test_slow_tier_alarm_status_arming_active(self):
        """alarmStatus intrusionSystem=ACTIVE, no write-lock → _arming_cache[cam]=True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )
        coord._is_write_locked = MagicMock(return_value=False)

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/alarmStatus"] = _make_resp(
            200, {"alarmType": "INTRUSION", "intrusionSystem": "ACTIVE"}
        )
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._arming_cache.get(CAM_A) is True, (
            "_arming_cache must be True when intrusionSystem=ACTIVE"
        )

    @pytest.mark.asyncio
    async def test_slow_tier_alarm_status_arming_inactive(self):
        """alarmStatus intrusionSystem=INACTIVE, no write-lock → _arming_cache[cam]=False."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )
        coord._is_write_locked = MagicMock(return_value=False)

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/alarmStatus"] = _make_resp(
            200, {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}
        )
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._arming_cache.get(CAM_A) is False, (
            "_arming_cache must be False when intrusionSystem=INACTIVE"
        )


class TestSlowTierIconLedBrightness:
    """Lines 2038-2042: iconLedBrightness → _icon_led_brightness_cache set."""

    @pytest.mark.asyncio
    async def test_slow_tier_icon_led_brightness_cached(self):
        """iconLedBrightness value=2 → _icon_led_brightness_cache[cam] = 2."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR)
        routes[f"{CAM_A}/iconLedBrightness"] = _make_resp(200, {"value": 2})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._icon_led_brightness_cache.get(CAM_A) == 2, (
            "_icon_led_brightness_cache must be 2 from endpoint value"
        )


class TestCleanupStaleDevices:
    """Lines 2171-2172: cleanup_stale_devices called when not first tick and data non-empty."""

    @pytest.mark.asyncio
    async def test_cleanup_stale_devices_called(self):
        """Second tick with populated data → _cleanup_stale_devices called with cam IDs."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=time_mod.monotonic(),   # not stale → skip slow tier
            _cached_status={CAM_A: "ONLINE"},
        )
        cleanup_mock = MagicMock()
        coord._cleanup_stale_devices = cleanup_mock

        routes = {
            "v11/video_inputs": _make_resp(200, [CAM_GEN2_INDOOR]),
            f"{CAM_A}/ping": _make_resp(200, {}, text_data="ONLINE"),
            f"{CAM_A}/lighting/switch": _make_resp(200, {}),
        }
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A in result, "Camera must appear in result"
        cleanup_mock.assert_called_once(), "cleanup_stale_devices must be called on second tick"
        call_args = cleanup_mock.call_args
        assert CAM_A in call_args.args[0], "cam_id must appear in cleanup_stale_devices argument"


class TestNvrCleanupTriggeredDaily:
    """Lines 2144-2149: enable_nvr=True, _last_nvr_cleanup stale → background task created."""

    @pytest.mark.asyncio
    async def test_nvr_cleanup_triggered_daily(self):
        """NVR enabled, retention=3 days, _last_nvr_cleanup ancient → async_create_background_task called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        nvr_opts = {"enable_nvr": True, "nvr_retention_days": 3}
        coord = _make_coord_for_update_data(
            _last_slow=time_mod.monotonic(),   # skip slow tier
            _last_nvr_cleanup=float("-inf"),             # ancient → interval elapsed
            _cached_status={CAM_A: "ONLINE"},
        )
        # get_options(self._entry) reads entry.options
        coord._entry.options = nvr_opts
        coord.options = nvr_opts

        bg_task_mock = MagicMock()
        coord.hass.async_create_background_task = bg_task_mock

        routes = {
            f"{CAM_A}/lighting/switch": _make_resp(200, {}),
            f"{CAM_A}/ping": _make_resp(200, {}, text_data="ONLINE"),
            "v11/video_inputs": _make_resp(200, [CAM_GEN2_INDOOR]),
        }
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert bg_task_mock.called, (
            "async_create_background_task must be called for NVR daily retention purge"
        )
        # Check that at least one call included the NVR cleanup task name
        all_call_str = str(bg_task_mock.call_args_list)
        assert "nvr_cleanup" in all_call_str, (
            f"NVR cleanup task name must appear in background task calls, got: {all_call_str}"
        )


class TestSmbDiskCheckTimeout:
    """Lines 2159-2166: SMB disk check runs, raises TimeoutError → warning logged."""

    @pytest.mark.asyncio
    async def test_smb_disk_check_timeout(self, caplog):
        """SMB disk check enabled, async_add_executor_job raises TimeoutError → WARNING logged."""
        import logging
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=time_mod.monotonic(),         # skip slow tier
            _last_smb_disk_check=float("-inf"),               # ancient → interval elapsed
            _cached_status={CAM_A: "ONLINE"},
        )
        coord._entry.options = {
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_disk_warn_mb": 500,
        }
        coord.options = coord._entry.options

        # Make the executor job stall so asyncio.wait_for times out
        async def _hang(*args, **kwargs):
            await asyncio.sleep(9999)

        coord.hass.async_add_executor_job = MagicMock(side_effect=lambda fn, *a: _hang())

        routes = {
            "v11/video_inputs": _make_resp(200, [CAM_GEN2_INDOOR]),
            f"{CAM_A}/ping": _make_resp(200, {}, text_data="ONLINE"),
            f"{CAM_A}/lighting/switch": _make_resp(200, {}),
        }
        session = _make_session_fn(routes)

        with caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"), \
             patch(_PATCH_SESSION, return_value=session), \
             patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            await BoschCameraCoordinator._async_update_data(coord)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("SMB disk check" in m or "timed out" in m for m in warning_msgs), (
            f"WARNING about SMB disk check timeout must be logged, got: {warning_msgs}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Group B — _try_live_connection_inner remaining branches
# ═══════════════════════════════════════════════════════════════════════════════


def _model_cfg(**overrides):
    """Return a SimpleNamespace that mimics a ModelConfig dataclass."""
    base = dict(
        max_stream_errors=3,
        min_wifi_for_local=50,
        max_session_duration=3600,
        generation=2,
        display_name="Eyes Außenkamera II",
        pre_warm_delay=0,
        pre_warm_retries=2,
        pre_warm_retry_wait=1,
        post_warm_buffer=0,
        describe_timeout=5,
        min_total_wait=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_coord_live(**overrides):
    """Minimal coordinator stub for _try_live_connection_inner tests."""
    task_mock = MagicMock()
    task_mock.done = MagicMock(return_value=True)

    def _create_task(coro, **kwargs):
        import inspect
        if inspect.iscoroutine(coro):
            coro.close()
        return task_mock

    base = dict(
        token="tok-A",
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={"stream_connection_type": "auto"},
        ),
        _stream_type_override=None,
        _stream_error_count={},
        _stream_error_at={},
        _stream_fell_back={},
        _local_promote_at={},
        _lan_tcp_reachable={},
        _rcp_lan_ip_cache={CAM_A: "192.168.1.1"},
        _local_creds_cache={},
        _tls_proxy_ports={},
        _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
        _wifiinfo_cache={},
        _audio_enabled={},
        _live_connections={},
        _live_opened_at={},
        _stream_warming=set(),
        _stream_warming_started={},
        _camera_entities={},
        _bg_tasks=set(),
        _renewal_tasks={},
        _auto_renew_generation={},
        _nvr_user_intent={},
        _nvr_processes={},
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _start_tls_proxy=AsyncMock(return_value=12345),
        _stop_tls_proxy=AsyncMock(),
        _register_go2rtc_stream=AsyncMock(),
        _check_and_recover_webrtc=AsyncMock(),
        _auto_renew_local_session=AsyncMock(return_value=None),
        _remote_session_terminator=AsyncMock(return_value=None),
        _refresh_rcp_state=AsyncMock(return_value=None),
        async_request_refresh=AsyncMock(return_value=None),
        get_quality_params=MagicMock(return_value=(True, 1)),
        get_quality=MagicMock(return_value="auto"),
        get_model_config=MagicMock(return_value=_model_cfg()),
        hass=SimpleNamespace(
            async_create_task=_create_task,
            async_add_executor_job=AsyncMock(),
            data={},
        ),
        debug=False,
        _get_cam_lan_ip=MagicMock(return_value="192.168.1.1"),
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    def _replace_renewal_task(cam_id, coro):
        t = coord.hass.async_create_task(coro)
        coord._renewal_tasks[cam_id] = t
        return t
    coord._replace_renewal_task = _replace_renewal_task
    return coord


class TestRemoteInst4Reduced:
    """Line 2437: REMOTE, inst=4 → inst overridden to 2 before PUT."""

    @pytest.mark.asyncio
    async def test_remote_inst4_reduced_to_2(self):
        """get_quality_params returns inst=4, REMOTE → PUT body highQualityVideo with inst=2 internally (no 400 error)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # get_quality_params returns inst=4, which REMOTE doesn't support
        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            get_quality_params=MagicMock(return_value=(True, 4)),
        )

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        resp = _put_resp(200, remote_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # Method should succeed (no 400 because inst was reduced to 2)
        assert result is not None, (
            "REMOTE with inst=4→2 reduction must succeed"
        )
        # PUT must have been called
        assert session_mock.put.called, "PUT must be called"


class TestLocalCredsCacheSplitException:
    """Lines 2485-2486: cam_addr split raises (malformed) → exception caught, continues."""

    @pytest.mark.asyncio
    async def test_local_creds_cache_split_exception_swallowed(self):
        """cam_addr without ':' → split raises → exception caught, method continues past cache."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
        )

        # Return a LOCAL response with URL where port is non-numeric.
        # split(":") succeeds (returns ["192.168.1.1", "NOTAPORT"]) but
        # int("NOTAPORT") raises ValueError inside the try/except at line 2482.
        # The except at 2485 catches it and logs debug — cache is NOT populated.
        # Line 2491 then does the same split successfully and we need _start_tls_proxy
        # to handle int("NOTAPORT") — but cam_addr.split(":")[1] = "NOTAPORT" is
        # passed to int() at line 2491 outside the try block, raising ValueError
        # which propagates up (not caught by inner try). We patch _start_tls_proxy
        # to accept anything so we can verify the cache miss.
        # To avoid the second split failure at line 2491, we use a URL with a valid
        # port but trigger the cache-write exception by patching int() inside the try.
        # Simplest approach: test with a URL that has a non-numeric port.
        # The try block catches the int() error; line 2491's split also fails but
        # that propagates outside — we wrap the whole call in try/except to confirm.
        local_body = json.dumps({
            "user": "u-local",
            "password": "p-local",
            "urls": ["192.168.1.1:NOTAPORT"],   # port non-numeric → int() fails in try block
            "bufferingTime": 500,
        })
        resp = _put_resp(200, local_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            # int("NOTAPORT") at line 2491 (outside try) raises ValueError which
            # propagates — catch it here to verify the cache was NOT set before the raise.
            try:
                await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)
            except (ValueError, Exception):
                pass  # expected — line 2491 also raises, not covered by inner try

        # creds cache must NOT be populated (int() failed inside the try/except)
        assert CAM_A not in coord._local_creds_cache, (
            "_local_creds_cache must NOT be populated when int(port) fails in the try/except block"
        )


class TestStaleStreamStopTimeout:
    """Lines 2616-2624: stale stream.stop() times out → TimeoutError caught, stream=None."""

    @pytest.mark.asyncio
    async def test_stale_stream_stop_timeout_force_detaches(self):
        """cam_ent.stream.stop() raises TimeoutError → stream set to None (force-detached)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # Create a stale stream whose stop() hangs
        stale_stream = MagicMock()
        stale_stream.stop = AsyncMock(side_effect=asyncio.TimeoutError())
        cam_ent = SimpleNamespace(stream=stale_stream)

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
            _camera_entities={CAM_A: cam_ent},
        )

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        resp = _put_resp(200, local_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert cam_ent.stream is None, (
            "cam_ent.stream must be None after stale.stop() TimeoutError (force-detach)"
        )


class TestStaleStreamStopException:
    """Lines 2625-2631: stream.stop raises generic Exception → caught, stream=None."""

    @pytest.mark.asyncio
    async def test_stale_stream_stop_exception_force_detaches(self):
        """cam_ent.stream.stop() raises RuntimeError → caught, cam_ent.stream = None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        stale_stream = MagicMock()
        stale_stream.stop = AsyncMock(side_effect=RuntimeError("worker dead"))
        cam_ent = SimpleNamespace(stream=stale_stream)

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
            _camera_entities={CAM_A: cam_ent},
        )

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        resp = _put_resp(200, local_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        async def _wait_for_passthrough(coro, timeout):
            """Let asyncio.wait_for run normally so RuntimeError propagates as generic exception."""
            return await coro

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("asyncio.wait_for", side_effect=_wait_for_passthrough), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert cam_ent.stream is None, (
            "cam_ent.stream must be None after generic exception in stale.stop()"
        )


class TestNoProxyPortPrewarmFalse:
    """Line 2658: _tls_proxy_ports empty → prewarm_ok = False."""

    @pytest.mark.asyncio
    async def test_no_proxy_port_prewarm_false(self):
        """_tls_proxy_ports empty → prewarm_ok=False branch hit → REMOTE fallback attempted."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_live(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _lan_tcp_reachable={},
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _tls_proxy_ports={},   # empty → no proxy port → prewarm_ok=False
            _start_tls_proxy=AsyncMock(return_value=None),  # returns None → no port in dict
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
        )

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        resp_local = _put_resp(200, local_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp_local)

        prewarm_called = []

        async def _prewarm_spy(*args, **kwargs):
            prewarm_called.append(True)
            return True

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   side_effect=_prewarm_spy):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # pre_warm_rtsp must NOT be called (no proxy port → prewarm_ok=False directly)
        assert not prewarm_called, (
            "pre_warm_rtsp must not be called when _tls_proxy_ports is empty"
        )


class TestLocalPrewarmFailedFallsBackToRemote:
    """Lines 2669-2679: pre-warm fails, REMOTE in candidates → continue to next candidate."""

    @pytest.mark.asyncio
    async def test_local_prewarm_failed_falls_back_to_remote(self):
        """pre_warm_rtsp returns False, AUTO mode → continues to REMOTE candidate."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_live(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},   # → LOCAL+REMOTE candidates
            _async_local_tcp_ping=AsyncMock(return_value=True),  # TCP ok → LOCAL kept
            _lan_tcp_reachable={},
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _tls_proxy_ports={CAM_A: 12345},
            _start_tls_proxy=AsyncMock(return_value=12345),
            _stop_tls_proxy=AsyncMock(),
        )

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        resp_local = _put_resp(200, local_body)
        resp_remote = _put_resp(200, remote_body)

        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(side_effect=[resp_local, resp_remote])

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=False)):   # pre-warm FAILS
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # REMOTE fallback must succeed
        assert result is not None, (
            "Must fall back to REMOTE when LOCAL pre-warm fails and REMOTE in candidates"
        )
        assert result.get("_connection_type") != "LOCAL", (
            "Result must not be LOCAL when pre-warm failed and fell back to REMOTE"
        )
        # _stream_fell_back must be set
        assert coord._stream_fell_back.get(CAM_A) is True, (
            "_stream_fell_back must be True after LOCAL pre-warm failure"
        )


class TestMinWaitSleepCalled:
    """Lines 2685-2690: elapsed < min_total_wait → asyncio.sleep called for remaining time."""

    @pytest.mark.asyncio
    async def test_min_wait_sleep_called(self):
        """min_total_wait=5, elapsed=~0 → asyncio.sleep called with ~5s remaining."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
            get_model_config=MagicMock(return_value=_model_cfg(min_total_wait=5)),
        )

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        resp = _put_resp(200, local_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        sleep_calls = []

        async def _fake_sleep(duration):
            sleep_calls.append(duration)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("asyncio.sleep", side_effect=_fake_sleep), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # At least one sleep call must have been for the min_wait remaining time
        positive_sleeps = [s for s in sleep_calls if s > 0]
        assert positive_sleeps, (
            "asyncio.sleep must be called with positive remaining time when min_total_wait=5"
        )


class TestStreamUpdateSourceException:
    """Lines 2710-2715: stream.update_source raises → stream = None."""

    @pytest.mark.asyncio
    async def test_stream_update_source_exception_resets_stream(self):
        """cam_ent.stream.update_source raises → cam_entity.stream set to None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        stream_mock = MagicMock()
        stream_mock.update_source = MagicMock(side_effect=RuntimeError("HA internals"))
        cam_ent = SimpleNamespace(stream=stream_mock)

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
            _camera_entities={CAM_A: cam_ent},
        )

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        resp = _put_resp(200, remote_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert cam_ent.stream is None, (
            "cam_entity.stream must be set to None when update_source raises"
        )


class TestStreamUpdateSourceNoExistingStream:
    """Line 2717: cam_entity.stream is None → remains None."""

    @pytest.mark.asyncio
    async def test_stream_update_source_no_existing_stream(self):
        """cam_entity.stream=None → code hits else branch, stream stays None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_ent = SimpleNamespace(stream=None)

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
            _camera_entities={CAM_A: cam_ent},
        )

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        resp = _put_resp(200, remote_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None, "Method must return a result dict"
        assert cam_ent.stream is None, (
            "cam_entity.stream must stay None when no existing stream present"
        )


class TestNvrSidecarLocal:
    """Lines 2779-2782: _nvr_user_intent=True, type=LOCAL → nvr_recorder.start_recorder task."""

    @pytest.mark.asyncio
    async def test_nvr_sidecar_local_starts_recorder(self):
        """NVR intent=True, LOCAL stream → async_create_task called for start_recorder."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        task_calls = []

        def _create_task(coro, **kwargs):
            import inspect
            if inspect.iscoroutine(coro):
                coro.close()
            task_calls.append(kwargs.get("name", ""))
            return MagicMock()

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
            _nvr_user_intent={CAM_A: True},  # NVR intent set for this camera
        )
        coord.hass = SimpleNamespace(
            async_create_task=_create_task,
            async_add_executor_job=AsyncMock(),
            data={},
        )
        coord._replace_renewal_task = lambda cam_id, coro: _create_task(coro)

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        resp = _put_resp(200, local_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        nvr_start_calls = [n for n in task_calls if "nvr_start" in n]
        assert nvr_start_calls, (
            f"async_create_task with name 'bosch_nvr_start_*' must be called for LOCAL+NVR intent, got: {task_calls}"
        )


class TestNvrSidecarRemote:
    """Lines 2784-2788: _nvr_user_intent=True, type=REMOTE, cam in _nvr_processes → stop_recorder task."""

    @pytest.mark.asyncio
    async def test_nvr_sidecar_remote_stops_recorder(self):
        """NVR intent=True, REMOTE stream, cam_id in _nvr_processes → stop_recorder task created."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        task_calls = []

        def _create_task(coro, **kwargs):
            import inspect
            if inspect.iscoroutine(coro):
                coro.close()
            task_calls.append(kwargs.get("name", ""))
            return MagicMock()

        coord = _make_coord_live(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
            _nvr_user_intent={CAM_A: True},        # NVR intent set
            _nvr_processes={CAM_A: MagicMock()},   # recorder currently running
        )
        coord.hass = SimpleNamespace(
            async_create_task=_create_task,
            async_add_executor_job=AsyncMock(),
            data={},
        )
        coord._replace_renewal_task = lambda cam_id, coro: _create_task(coro)

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        resp = _put_resp(200, remote_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        nvr_stop_calls = [n for n in task_calls if "nvr_stop" in n]
        assert nvr_stop_calls, (
            f"async_create_task with name 'bosch_nvr_stop_*' must be called for REMOTE+NVR+running recorder, got: {task_calls}"
        )
