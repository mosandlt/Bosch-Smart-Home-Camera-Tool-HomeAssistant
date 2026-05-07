"""Sprint MC tests — covering lines missed by Sprint LC.

Coverage targets in __init__.py:
  1905       – _fetch returns non-200 status (not exception, not 200)
  1957       – asyncio.gather returns Exception object → isinstance branch → continue
  2015       – lighting/motion for-loop body (pass) — hass.data entity_platform populated
  2041-2042  – iconLedBrightness try-block (int(value) succeeds; also TypeError branch)
  2069-2105  – RCP-via-cloud PUT path (privacyMode OFF, not local stream)
  2103       – RCP TimeoutError/ClientError branch
  2111-2114  – shc_ready=True → _async_update_shc_states called; exception caught
  2129-2133  – SMB cleanup background task triggered (ancient _last_smb_cleanup)
  2706       – stream.update_source() succeeds → _LOGGER.debug on the success path
  2974-2991  – fetch_live_snapshot RCP 0x099e unavailable (non-JPEG raw) + Exception
  3002-3018  – fetch_live_snapshot snap.jpg 404 → retry second proxy URL → return None

All tests use unbound-method pattern: BoschCameraCoordinator.method(coord, ...).
No live HA runtime required.
"""
from __future__ import annotations

import asyncio
import json
import time as time_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CLOUD_API = "https://residential.cbs.boschsecurity.com"
DOMAIN = "bosch_shc_camera"

# ── Shared response helpers ────────────────────────────────────────────────────


def _make_resp(status: int, json_data=None, text_data: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session_fn(url_routes: dict):
    """Route session.get() by URL substring (longest match first)."""
    state: dict = {k: (list(v) if isinstance(v, list) else [v]) for k, v in url_routes.items()}
    sorted_patterns = sorted(state.keys(), key=len, reverse=True)

    def _get(url, **kwargs):
        for pattern in sorted_patterns:
            if pattern in url:
                queue = state[pattern]
                if queue:
                    r = queue.pop(0)
                    if not queue:
                        queue.append(r)
                    return r
        return _make_resp(200, [])

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _put_resp(status: int, body: str):
    r = MagicMock()
    r.status = status
    r.text = AsyncMock(return_value=body)
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=None)
    return r


_PATCH_SESSION = "custom_components.bosch_shc_camera.async_get_clientsession"

# Camera with privacyMode OFF (needed for RCP path) — Gen2 Indoor
CAM_GEN2_INDOOR_PRIV_OFF = {
    "id": CAM_A,
    "hardwareVersion": "HOME_Eyes_Indoor",
    "featureSupport": {"light": False, "panLimit": 0},
    "featureStatus": {},
    "privacyMode": "OFF",
}

# Gen2 Indoor with privacyMode ON (for other tests)
CAM_GEN2_INDOOR_PRIV_ON = {
    "id": CAM_A,
    "hardwareVersion": "HOME_Eyes_Indoor",
    "featureSupport": {"light": False, "panLimit": 0},
    "featureStatus": {},
    "privacyMode": "ON",
}


def _make_coord_for_update_data(**overrides):
    """Coordinator stub for _async_update_data tests (same base as Sprint LC)."""

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
        _last_status=float("-inf"),
        _last_events=float("-inf"),
        _last_slow=float("-inf"),
        _last_smb_cleanup=time_mod.monotonic(),    # not stale by default
        _last_smb_disk_check=time_mod.monotonic(),
        _last_nvr_cleanup=time_mod.monotonic(),
        _fcm_lock=__import__("threading").Lock(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_client=None,
        _hw_version={},
        _cached_status={CAM_A: "ONLINE"},
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
        _WRITE_LOCK_SECS=30.0,
        _audio_alarm_set_at={},
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        _arming_set_at={},
        _feature_flags={"dummy": True},
        _protocol_checked=True,
        _integration_version="11.0.10",
        _OFFLINE_EXTENDED_INTERVAL=900,
        _ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _should_check_status=MagicMock(return_value=False),
        _cleanup_stale_devices=MagicMock(),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        async_mark_events_read=AsyncMock(),
        _is_write_locked=MagicMock(return_value=False),
        shc_ready=False,
        get_model_config=lambda cid: SimpleNamespace(generation=2),
        get_quality_params=MagicMock(return_value=(True, 1)),
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
    coord._first_tick_done = True
    return coord


def _build_slow_tier_routes(cam_info: dict, extra_routes: dict | None = None) -> dict:
    """Build URL route dict for a coordinator test with a single camera."""
    cid = cam_info["id"]
    routes = {
        "v11/video_inputs": _make_resp(200, [cam_info]),
        f"{cid}/ping": _make_resp(200, {}, text_data="ONLINE"),
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
        f"{cid}/motion_sensitive_areas": _make_resp(200, []),
        f"{cid}/privacy_masks": _make_resp(200, []),
        f"{cid}/privacy_sound_override": _make_resp(200, {"result": False}),
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
# Line 1905 — _fetch returns non-200 status (not exception, not 200)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFetchNon200Status:
    """Line 1905: _fetch receives HTTP non-200 (e.g. 403) → returns (ep, status, None)."""

    @pytest.mark.asyncio
    async def test_fetch_non200_returns_status_none(self):
        """firmware endpoint returns HTTP 403 → _fetch returns (ep, 403, None); other caches still set."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_ON)
        # firmware returns 403 — __aenter__ succeeds, status != 200 → line 1905 hit
        routes[f"{CAM_A}/firmware"] = _make_resp(403)

        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        # firmware cache must NOT be set (status != 200 → ep_data = None → skipped)
        assert CAM_A not in coord._firmware_cache, (
            "_firmware_cache must not be set when firmware endpoint returns 403"
        )
        # wifiinfo must still be cached (other endpoints unaffected)
        assert CAM_A in coord._wifiinfo_cache, (
            "_wifiinfo_cache must still be populated despite firmware 403"
        )
        assert isinstance(result, dict)


# ═══════════════════════════════════════════════════════════════════════════════
# Line 1957 — asyncio.gather returns Exception object → isinstance → continue
# ═══════════════════════════════════════════════════════════════════════════════


class TestGatherExceptionContinue:
    """Line 1957: gather result is an Exception instance → isinstance branch → continue."""

    @pytest.mark.asyncio
    async def test_gather_exception_result_skipped(self):
        """Patch asyncio.gather to return a list with one Exception → line 1957 (continue) hit."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_ON)
        session = _make_session_fn(routes)

        original_gather = asyncio.gather

        async def _gather_with_exception(*coros, **kwargs):
            """Run gather normally but inject one Exception at the start of results."""
            real_results = await original_gather(*coros, **kwargs)
            # Prepend a bare Exception so isinstance(result, Exception) is True on first item
            return [RuntimeError("injected")] + list(real_results)

        with patch(_PATCH_SESSION, return_value=session), \
             patch("asyncio.gather", side_effect=_gather_with_exception):
            result = await BoschCameraCoordinator._async_update_data(coord)

        # Method must not crash and must return a dict
        assert isinstance(result, dict), "Must return dict even when gather result has Exception"
        # wifiinfo should still be cached (from the real results)
        assert CAM_A in coord._wifiinfo_cache, (
            "_wifiinfo_cache must be populated from normal gather results after Exception is skipped"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Line 2015 — lighting/motion for-loop body (pass) with non-empty entity list
# ═══════════════════════════════════════════════════════════════════════════════


class TestMotionLightForLoopBody:
    """Line 2015: lighting/motion result → for-loop over entity_platform iterates ≥1 item."""

    @pytest.mark.asyncio
    async def test_motion_light_for_loop_body_hit(self):
        """hass.data entity_platform has ≥1 switch entity → for-loop body (pass) executed."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )
        # Populate entity_platform so the for-loop has something to iterate
        coord.hass.data = {
            "entity_platform": {
                f"{DOMAIN}.switch": [MagicMock()],
            }
        }

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_ON)
        routes[f"{CAM_A}/lighting/motion"] = _make_resp(200, {"trigger": "motion", "enabled": True})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._motion_light_cache.get(CAM_A) == {"trigger": "motion", "enabled": True}, (
            "_motion_light_cache must be populated from lighting/motion endpoint"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Lines 2041-2042 — iconLedBrightness TypeError/ValueError branch
# ═══════════════════════════════════════════════════════════════════════════════


class TestIconLedBrightnessTypeError:
    """Lines 2041-2042: iconLedBrightness value triggers TypeError/ValueError → cache = 0."""

    @pytest.mark.asyncio
    async def test_icon_led_brightness_typeerror_sets_zero(self):
        """iconLedBrightness returns non-numeric value → int() raises TypeError → cache = 0."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_ON)
        # Return a dict with a non-integer "value" → int(ep_data.get("value", 0)) fails
        # Actually ep_data.get("value", 0) returns "not-a-number", then int("not-a-number")
        # raises ValueError — hits line 2042 (the except branch)
        routes[f"{CAM_A}/iconLedBrightness"] = _make_resp(200, {"value": "not-a-number"})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._icon_led_brightness_cache.get(CAM_A) == 0, (
            "_icon_led_brightness_cache must be 0 when int() raises ValueError"
        )

    @pytest.mark.asyncio
    async def test_icon_led_brightness_none_value_sets_zero(self):
        """iconLedBrightness returns {"value": None} → int(None) raises TypeError → cache = 0."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_ON)
        # int(None) → TypeError
        routes[f"{CAM_A}/iconLedBrightness"] = _make_resp(200, {"value": None})
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._icon_led_brightness_cache.get(CAM_A) == 0, (
            "_icon_led_brightness_cache must be 0 when int(None) raises TypeError"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Lines 2084-2098 — RCP-via-cloud PUT 200 path (privacyMode OFF)
# ═══════════════════════════════════════════════════════════════════════════════


class TestRcpViaCloudPut200:
    """Lines 2084-2098: privacyMode=OFF, is_online=True, do_slow=True → RCP PUT path entered."""

    @pytest.mark.asyncio
    async def test_rcp_put_200_calls_async_update_rcp_data(self):
        """PUT /connection returns 200 with urls → _async_update_rcp_data called."""
        import aiohttp as _aiohttp
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        # Session for the main coordinator call (fast-tier endpoints)
        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_OFF)
        main_session = _make_session_fn(routes)

        # RCP session mock: PUT returns 200 with proxy URL
        rcp_put_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/abc123hash"],
            "bufferingTime": 1000,
        })
        rcp_put_resp = _put_resp(200, rcp_put_body)
        rcp_session_mock = MagicMock()
        rcp_session_mock.__aenter__ = AsyncMock(return_value=rcp_session_mock)
        rcp_session_mock.__aexit__ = AsyncMock(return_value=None)
        rcp_session_mock.put = MagicMock(return_value=rcp_put_resp)

        rcp_connector_mock = MagicMock()

        with patch(_PATCH_SESSION, return_value=main_session), \
             patch("aiohttp.TCPConnector", return_value=rcp_connector_mock), \
             patch("aiohttp.ClientSession", return_value=rcp_session_mock):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_update_rcp_data.assert_awaited_once()
        call_args = coord._async_update_rcp_data.call_args
        assert call_args.args[0] == CAM_A
        assert "proxy-01.live.cbs.boschsecurity.com:42090" in call_args.args[1]
        assert "abc123hash" in call_args.args[2]

    @pytest.mark.asyncio
    async def test_rcp_put_non200_logs_debug(self, caplog):
        """PUT /connection returns 403 → else branch (line 2098) → debug logged, no crash."""
        import logging
        import aiohttp as _aiohttp
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_OFF)
        main_session = _make_session_fn(routes)

        rcp_put_resp = _put_resp(403, "")
        rcp_session_mock = MagicMock()
        rcp_session_mock.__aenter__ = AsyncMock(return_value=rcp_session_mock)
        rcp_session_mock.__aexit__ = AsyncMock(return_value=None)
        rcp_session_mock.put = MagicMock(return_value=rcp_put_resp)

        with patch(_PATCH_SESSION, return_value=main_session), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=rcp_session_mock), \
             caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_update_rcp_data.assert_not_awaited()
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("RCP proxy connection HTTP 403" in m or "HTTP 403" in m for m in debug_msgs), (
            f"DEBUG about RCP 403 must be logged, got: {debug_msgs}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Line 2103 — RCP TimeoutError/ClientError inside the PUT attempt
# ═══════════════════════════════════════════════════════════════════════════════


class TestRcpConnectError:
    """Line 2103: asyncio.TimeoutError or aiohttp.ClientError in RCP PUT → debug logged."""

    @pytest.mark.asyncio
    async def test_rcp_timeout_error_caught(self, caplog):
        """asyncio.timeout fires during PUT /connection → TimeoutError caught at line 2103."""
        import logging
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=float("-inf"),
            _cached_status={CAM_A: "ONLINE"},
        )

        routes = _build_slow_tier_routes(CAM_GEN2_INDOOR_PRIV_OFF)
        main_session = _make_session_fn(routes)

        rcp_session_mock = MagicMock()
        rcp_session_mock.__aenter__ = AsyncMock(return_value=rcp_session_mock)
        rcp_session_mock.__aexit__ = AsyncMock(return_value=None)
        # Make put raise asyncio.TimeoutError
        rcp_put_resp = MagicMock()
        rcp_put_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        rcp_put_resp.__aexit__ = AsyncMock(return_value=None)
        rcp_session_mock.put = MagicMock(return_value=rcp_put_resp)

        with patch(_PATCH_SESSION, return_value=main_session), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=rcp_session_mock), \
             caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), "Must return dict after RCP TimeoutError"
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("RCP proxy connect error" in m for m in debug_msgs), (
            f"DEBUG 'RCP proxy connect error' must be logged on timeout, got: {debug_msgs}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Lines 2111-2114 — shc_ready=True → _async_update_shc_states; exception caught
# ═══════════════════════════════════════════════════════════════════════════════


class TestShcReadyStatesUpdate:
    """Lines 2111-2114: shc_ready=True → _async_update_shc_states called; exception → debug."""

    @pytest.mark.asyncio
    async def test_shc_states_called_when_ready(self):
        """shc_ready=True → _async_update_shc_states is awaited."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=time_mod.monotonic(),   # skip slow tier
            _cached_status={CAM_A: "ONLINE"},
            shc_ready=True,
        )

        routes = {
            "v11/video_inputs": _make_resp(200, [CAM_GEN2_INDOOR_PRIV_ON]),
            f"{CAM_A}/ping": _make_resp(200, {}, text_data="ONLINE"),
            f"{CAM_A}/lighting/switch": _make_resp(200, {}),
        }
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_update_shc_states.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shc_states_exception_caught(self, caplog):
        """shc_ready=True, _async_update_shc_states raises → exception caught, debug logged."""
        import logging
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_for_update_data(
            _last_slow=time_mod.monotonic(),
            _cached_status={CAM_A: "ONLINE"},
            shc_ready=True,
        )
        coord._async_update_shc_states = AsyncMock(
            side_effect=RuntimeError("SHC connection refused")
        )

        routes = {
            "v11/video_inputs": _make_resp(200, [CAM_GEN2_INDOOR_PRIV_ON]),
            f"{CAM_A}/ping": _make_resp(200, {}, text_data="ONLINE"),
            f"{CAM_A}/lighting/switch": _make_resp(200, {}),
        }
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session), \
             caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), "Must return dict even when SHC update raises"
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("SHC state update error" in m for m in debug_msgs), (
            f"DEBUG 'SHC state update error' must be logged, got: {debug_msgs}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Lines 2129-2133 — SMB cleanup background task (ancient _last_smb_cleanup)
# ═══════════════════════════════════════════════════════════════════════════════


class TestSmbCleanupBackgroundTask:
    """Lines 2129-2133: SMB cleanup enabled + ancient _last_smb_cleanup → background task fired."""

    @pytest.mark.asyncio
    async def test_smb_cleanup_background_task_created(self):
        """SMB upload enabled, _last_smb_cleanup ancient → async_create_background_task called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        smb_opts = {
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_retention_days": 30,
        }
        coord = _make_coord_for_update_data(
            _last_slow=time_mod.monotonic(),          # skip slow tier
            _last_smb_cleanup=float("-inf"),          # ancient → interval elapsed
            _cached_status={CAM_A: "ONLINE"},
        )
        coord._entry.options = smb_opts
        coord.options = smb_opts

        bg_task_mock = MagicMock()
        coord.hass.async_create_background_task = bg_task_mock

        routes = {
            "v11/video_inputs": _make_resp(200, [CAM_GEN2_INDOOR_PRIV_ON]),
            f"{CAM_A}/ping": _make_resp(200, {}, text_data="ONLINE"),
            f"{CAM_A}/lighting/switch": _make_resp(200, {}),
        }
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert bg_task_mock.called, (
            "async_create_background_task must be called for SMB daily retention cleanup"
        )
        all_call_str = str(bg_task_mock.call_args_list)
        assert "smb_cleanup" in all_call_str, (
            f"SMB cleanup task name must appear in background task calls, got: {all_call_str}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Line 2706 — stream.update_source() success → _LOGGER.debug
# ═══════════════════════════════════════════════════════════════════════════════


def _model_cfg(**overrides):
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


class TestStreamUpdateSourceSuccess:
    """Line 2706: stream.update_source() succeeds → _LOGGER.debug on success path."""

    @pytest.mark.asyncio
    async def test_stream_update_source_success_logs_debug(self, caplog):
        """cam_ent.stream.update_source() succeeds → debug logged for 'Stream.update_source()'."""
        import logging
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        stream_mock = MagicMock()
        stream_mock.update_source = MagicMock(return_value=None)  # success
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
             patch("aiohttp.ClientSession", return_value=session_mock), \
             caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        stream_mock.update_source.assert_called_once()
        assert result is not None
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Stream.update_source()" in m for m in debug_msgs), (
            f"DEBUG 'Stream.update_source()' success log must be emitted, got: {debug_msgs}"
        )
        # stream must remain set (not cleared on success)
        assert cam_ent.stream is stream_mock, (
            "cam_ent.stream must remain the stream object after successful update_source()"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Lines 2974-2991 — fetch_live_snapshot RCP 0x099e non-JPEG + Exception path
# ═══════════════════════════════════════════════════════════════════════════════


def _make_snapshot_coord(**overrides):
    """Minimal coordinator stub for _async_fetch_live_snapshot_impl tests."""
    base = dict(
        token="tok-A",
        _proxy_url_cache={},
        _snapshot_fetch_locks={},
        _camera_status_extra={},
        get_quality_params=MagicMock(return_value=(True, {})),
        _get_cached_rcp_session=AsyncMock(return_value=None),
        _rcp_read=AsyncMock(return_value=None),
        hass=SimpleNamespace(
            async_add_executor_job=AsyncMock(),
            data={},
        ),
        debug=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestFetchLiveSnapshotRcpUnavailable:
    """Lines 2986-2989: RCP 0x099e returns non-JPEG raw → falls through to snap.jpg."""

    @pytest.mark.asyncio
    async def test_rcp_non_jpeg_falls_through_to_snap(self):
        """_rcp_read returns bytes that don't start with \\xff\\xd8 → debug logged, snap.jpg tried."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_snapshot_coord(
            # Pre-populate cache so PUT /connection is skipped
            _proxy_url_cache={
                CAM_A: ("proxy-01.example.com:42090/testhash", time_mod.monotonic() + 50)
            },
            # session_id returned → RCP path entered; raw = b"\x00\x01" (not JPEG)
            _get_cached_rcp_session=AsyncMock(return_value="sess-123"),
            _rcp_read=AsyncMock(return_value=b"\x00\x01\x02"),  # not b"\xff\xd8"
        )

        snap_resp = MagicMock()
        snap_resp.status = 200
        snap_resp.headers = {"Content-Type": "image/jpeg"}
        snap_resp.read = AsyncMock(return_value=b"\xff\xd8snap")
        snap_resp.__aenter__ = AsyncMock(return_value=snap_resp)
        snap_resp.__aexit__ = AsyncMock(return_value=None)

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.get = MagicMock(return_value=snap_resp)
        session_mock.put = MagicMock()

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(
                coord, CAM_A
            )

        # Should fall through to snap.jpg which returns data
        assert result == b"\xff\xd8snap", (
            "Must fall through to snap.jpg when RCP raw is not JPEG"
        )


class TestFetchLiveSnapshotRcpException:
    """Lines 2990-2994: _rcp_read raises exception → caught, falls through to snap.jpg."""

    @pytest.mark.asyncio
    async def test_rcp_exception_falls_through_to_snap(self):
        """_rcp_read raises RuntimeError → caught at line 2990, snap.jpg tried."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_snapshot_coord(
            _proxy_url_cache={
                CAM_A: ("proxy-01.example.com:42090/testhash", time_mod.monotonic() + 50)
            },
            _get_cached_rcp_session=AsyncMock(return_value="sess-123"),
            _rcp_read=AsyncMock(side_effect=RuntimeError("RCP timeout")),
        )

        snap_resp = MagicMock()
        snap_resp.status = 200
        snap_resp.headers = {"Content-Type": "image/jpeg"}
        snap_resp.read = AsyncMock(return_value=b"\xff\xd8snap")
        snap_resp.__aenter__ = AsyncMock(return_value=snap_resp)
        snap_resp.__aexit__ = AsyncMock(return_value=None)

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.get = MagicMock(return_value=snap_resp)
        session_mock.put = MagicMock()

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(
                coord, CAM_A
            )

        assert result == b"\xff\xd8snap", (
            "Must fall through to snap.jpg when _rcp_read raises"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# Lines 3002-3018 — snap.jpg 404 → retry second proxy URL → return None
# ═══════════════════════════════════════════════════════════════════════════════


class TestFetchLiveSnapshot404RetryReturnsNone:
    """Lines 3002-3018: snap.jpg returns 404 → retry with second proxy URL → return None."""

    @pytest.mark.asyncio
    async def test_snap_404_retry_second_proxy_returns_none(self):
        """snap.jpg 404 → evict cache, _get_proxy_url_entry() returns None → return None (line 3009)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_snapshot_coord(
            _proxy_url_cache={
                CAM_A: ("proxy-01.example.com:42090/testhash", time_mod.monotonic() + 50)
            },
            _get_cached_rcp_session=AsyncMock(return_value=None),
        )

        snap_404_resp = MagicMock()
        snap_404_resp.status = 404
        snap_404_resp.headers = {"Content-Type": "text/plain"}
        snap_404_resp.read = AsyncMock(return_value=b"")
        snap_404_resp.__aenter__ = AsyncMock(return_value=snap_404_resp)
        snap_404_resp.__aexit__ = AsyncMock(return_value=None)

        # PUT /connection returns no urls → _get_proxy_url_entry returns None
        put_resp_no_urls = MagicMock()
        put_resp_no_urls.status = 200
        put_resp_no_urls.text = AsyncMock(return_value='{"urls": []}')
        put_resp_no_urls.__aenter__ = AsyncMock(return_value=put_resp_no_urls)
        put_resp_no_urls.__aexit__ = AsyncMock(return_value=None)

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.get = MagicMock(return_value=snap_404_resp)
        session_mock.put = MagicMock(return_value=put_resp_no_urls)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(
                coord, CAM_A
            )

        assert result is None, (
            "Must return None when snap.jpg 404 and second proxy URL returns empty urls"
        )

    @pytest.mark.asyncio
    async def test_snap_404_retry_second_url_returns_none_after_empty_snap(self):
        """snap.jpg 404 → retry with second URL → snap also returns data → line 3018 hit."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_snapshot_coord(
            _proxy_url_cache={
                CAM_A: ("proxy-01.example.com:42090/testhash", time_mod.monotonic() + 50)
            },
            _get_cached_rcp_session=AsyncMock(return_value=None),
        )

        # First snap.jpg call → 404
        snap_404_resp = MagicMock()
        snap_404_resp.status = 404
        snap_404_resp.headers = {"Content-Type": "text/plain"}
        snap_404_resp.read = AsyncMock(return_value=b"")
        snap_404_resp.__aenter__ = AsyncMock(return_value=snap_404_resp)
        snap_404_resp.__aexit__ = AsyncMock(return_value=None)

        # Second snap.jpg call → 200 with empty body (no data)
        snap_200_empty_resp = MagicMock()
        snap_200_empty_resp.status = 200
        snap_200_empty_resp.headers = {"Content-Type": "image/jpeg"}
        snap_200_empty_resp.read = AsyncMock(return_value=b"")  # empty → if data: False
        snap_200_empty_resp.__aenter__ = AsyncMock(return_value=snap_200_empty_resp)
        snap_200_empty_resp.__aexit__ = AsyncMock(return_value=None)

        # PUT /connection → fresh URL
        put_body = json.dumps({"urls": ["proxy-02.example.com:42090/newhash"]})
        put_resp_fresh = MagicMock()
        put_resp_fresh.status = 200
        put_resp_fresh.text = AsyncMock(return_value=put_body)
        put_resp_fresh.__aenter__ = AsyncMock(return_value=put_resp_fresh)
        put_resp_fresh.__aexit__ = AsyncMock(return_value=None)

        get_call_count = [0]

        def _get_side_effect(url, **kwargs):
            get_call_count[0] += 1
            if get_call_count[0] == 1:
                return snap_404_resp
            return snap_200_empty_resp

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.get = MagicMock(side_effect=_get_side_effect)
        session_mock.put = MagicMock(return_value=put_resp_fresh)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(
                coord, CAM_A
            )

        # Second snap returns empty data → if data: is False → falls through to return None (3018)
        assert result is None, (
            "Must return None when second proxy snap.jpg returns empty body"
        )
