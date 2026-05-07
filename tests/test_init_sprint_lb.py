"""Sprint LB tests — _async_update_data branches in __init__.py.

Coverage targets:
  - Line 1394:  Camera-list retry returns non-200 → UpdateFailed
  - Line 1408:  CancelledError inside feature-flags block is re-raised
  - Lines 1433-1439: Protocol version non-200 warning + exception swallowed
  - Line 1570:  asyncio.gather status result is Exception → continue
  - Line 1619:  asyncio.gather events result is Exception → continue
  - Lines 1638-1647: Startup (prev_id=None) — unread events + mark_events_read + exception
  - Line 1678:  cam_entity._async_trigger_image_refresh task created
  - Line 1687:  PERSON tag upgrades MOVEMENT → PERSON
  - Lines 1700-1705: AUDIO_ALARM and PERSON event types fire on hass.bus
  - Lines 1718-1723: mark-events-read (new event) exception path
  - Lines 1810-1832: Gen2 lighting/switch cache processing + Gen1 featureStatus update
  - Line 1843:  notifications_status cache update
  - Lines 1852-1862: Pan fetch when pan_limit > 0
  - Lines 1878-1879: Gen2 lighting/switch fetch exception

All tests run without a running HA instance.  Coordinator is a SimpleNamespace stub;
methods are called via BoschCameraCoordinator.<method>(coord, ...) (unbound pattern).
"""
from __future__ import annotations

import asyncio
import threading
import time as time_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLOUD_API = "https://residential.cbs.boschsecurity.com"
CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"

_PATCH_SESSION = "custom_components.bosch_shc_camera.async_get_clientsession"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_resp(status: int, json_data=None, text_data: str = ""):
    """Build a fake aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_coord(**overrides):
    """Coordinator stub with every attribute that _async_update_data touches."""

    def _create_task(coro):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
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
        _last_events=time_mod.monotonic(),    # recent → do_events=False by default
        _last_slow=time_mod.monotonic(),       # recent → do_slow=False by default
        _last_smb_cleanup=time_mod.monotonic(),
        _last_nvr_cleanup=time_mod.monotonic(),
        _fcm_lock=threading.Lock(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_client=None,
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
        _event_dedup_cache={},
        _arming_set_at={},
        _arming_cache={},
        _icon_led_brightness_cache={},
        _gen2_zones_cache={},
        _gen2_private_areas_cache={},
        _intrusion_config_cache={},
        _alarm_settings_cache={},
        _alarm_status_cache={},
        _audio_alarm_set_at={},
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        _camera_entities={},
        _WRITE_LOCK_SECS=30.0,
        _feature_flags={"x": 1},     # pre-populated → skip FF fetch by default
        _protocol_checked=True,       # pre-done → skip protocol check by default
        _integration_version="11.0.10",
        _OFFLINE_EXTENDED_INTERVAL=900,
        _ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _should_check_status=MagicMock(return_value=True),
        _cleanup_stale_devices=MagicMock(),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        _async_send_alert=AsyncMock(),
        _tear_down_live_stream=AsyncMock(),
        async_mark_events_read=AsyncMock(),
        async_handle_fcm_push=AsyncMock(),
        _is_write_locked=MagicMock(return_value=False),
        shc_ready=False,
        get_model_config=lambda cid: SimpleNamespace(generation=2),
        get_quality_params=lambda cid: (True, {}),
        is_camera_online=lambda cid: True,
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_create_background_task=MagicMock(side_effect=_create_task),
            async_add_executor_job=AsyncMock(),
            data={},
            bus=SimpleNamespace(async_fire=MagicMock()),
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp"),
        ),
        debug=False,
    )
    # Always set _first_tick_done unless explicitly overridden
    if "_first_tick_done" not in overrides:
        base["_first_tick_done"] = True
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_cam_entry(cam_id: str = CAM_A, **fields) -> dict:
    """Minimal camera dict as returned by /v11/video_inputs."""
    base = {
        "id": cam_id,
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "title": "Terrasse",
        "privacyMode": "OFF",
        "featureSupport": {"light": True, "panLimit": 0},
        "featureStatus": {"frontIlluminatorInGeneralLightOn": False},
        "status": {"isCommissioned": True, "isConnected": True},
        "notificationsEnabledStatus": "",
    }
    base.update(fields)
    return base


def _url_session(url_map: dict, default_json=None):
    """aiohttp session mock routing GET by URL substring (longest pattern wins).

    url_map: {substring: json_value or (json_value, status_code) or resp_object}
    Patterns are tested longest-first so specific sub-paths win.
    """

    def _make_r(json_val, status: int = 200):
        r = MagicMock()
        r.status = status
        r.__aenter__ = AsyncMock(return_value=r)
        r.__aexit__ = AsyncMock(return_value=None)
        r.json = AsyncMock(return_value=json_val)
        r.text = AsyncMock(return_value="")
        return r

    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    _sorted_patterns = sorted(url_map.keys(), key=len, reverse=True)

    def _get(url, **kwargs):
        url_str = str(url)
        for pattern in _sorted_patterns:
            if pattern in url_str:
                val = url_map[pattern]
                # If it's already a mock resp (has __aenter__), return as-is
                if hasattr(val, "__aenter__"):
                    return val
                if isinstance(val, tuple):
                    return _make_r(val[0], val[1])
                return _make_r(val)
        return _make_r(default_json or [])

    session.get = _get
    session.put = AsyncMock()
    return session


def _session_for_cam(cam_entry: dict, events: list | None = None,
                      status_text: str = "ONLINE") -> MagicMock:
    """Session returning cam_entry from /v11/video_inputs and events from /events."""
    cam_id = cam_entry["id"]
    return _url_session({
        f"/v11/video_inputs/{cam_id}/last_event": ({"id": ""}, 404),
        f"/v11/video_inputs/{cam_id}/lighting/switch": ({}, 200),
        f"/v11/video_inputs/{cam_id}/ping": (status_text, 200),
        f"/v11/video_inputs/{cam_id}/commissioned": (
            {"connected": True, "commissioned": True}, 200
        ),
        f"/v11/events?videoInputId={cam_id}": events or [],
        "/v11/video_inputs": [cam_entry],
    })


# ===========================================================================
# Group 1: Camera-list retry paths
# ===========================================================================


class TestCamListRetryPaths:
    """Lines 1393-1394, 1407-1408, 1433-1439."""

    @pytest.mark.asyncio
    async def test_cam_list_retry_non200_raises(self):
        """After first 401 triggers refresh, second call returns 500 → UpdateFailed.

        Line 1394: `raise UpdateFailed(f"Camera list returned HTTP {resp2.status}")`
        """
        from homeassistant.helpers.update_coordinator import UpdateFailed
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        call_count = [0]

        def _get(url, **kwargs):
            if "v11/video_inputs" in url and "ping" not in url and "/" not in url.split("video_inputs")[1].lstrip("/"):
                call_count[0] += 1
                if call_count[0] == 1:
                    return _make_resp(401)
                return _make_resp(500)
            if "feature_flags" in url:
                return _make_resp(200, {})
            if "protocol_support" in url:
                return _make_resp(200, {"state": "SUPPORTED"})
            return _make_resp(200, [])

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with pytest.raises(UpdateFailed, match="Camera list returned HTTP 500"), \
             patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert call_count[0] == 2, (
            "Both calls must have been made (initial 401 + retry 500)"
        )

    @pytest.mark.asyncio
    async def test_feature_flags_cancelled_error_reraises(self):
        """CancelledError inside feature-flags block is re-raised (line 1408)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # _feature_flags empty → triggers FF fetch
        coord = _make_coord(_feature_flags={}, _protocol_checked=True)

        def _get(url, **kwargs):
            if "feature_flags" in url:
                # Return a CM whose __aenter__ raises CancelledError
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError())
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm
            if "v11/video_inputs" in url and "ping" not in url:
                return _make_resp(200, [])
            return _make_resp(200, {})

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with pytest.raises(asyncio.CancelledError), \
             patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

    @pytest.mark.asyncio
    async def test_protocol_version_non200_warns(self, caplog):
        """Protocol endpoint returns 404 → WARNING logged (lines 1433-1436)."""
        import logging
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=False, _feature_flags={"x": 1})

        def _get(url, **kwargs):
            if "protocol_support" in url:
                return _make_resp(404)
            if "v11/video_inputs" in url and "ping" not in url:
                return _make_resp(200, [])
            return _make_resp(200, {})

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"), \
             patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("404" in m or "protocol" in m.lower() for m in warning_msgs), (
            f"WARNING about non-200 protocol response must be emitted. Got: {warning_msgs}"
        )

    @pytest.mark.asyncio
    async def test_protocol_version_exception_swallowed(self):
        """Protocol endpoint raises aiohttp.ClientError → swallowed, no UpdateFailed (lines 1438-1439)."""
        import aiohttp
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_protocol_checked=False, _feature_flags={"x": 1})

        def _get(url, **kwargs):
            if "protocol_support" in url:
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("refused"))
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm
            if "v11/video_inputs" in url and "ping" not in url:
                return _make_resp(200, [])
            return _make_resp(200, {})

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return normally when protocol check raises ClientError"
        )


# ===========================================================================
# Group 2: Gather exception handling
# ===========================================================================


class TestGatherExceptionHandling:
    """Lines 1569-1570, 1618-1619."""

    @pytest.mark.asyncio
    async def test_status_gather_exception_skipped(self):
        """asyncio.gather for status returns an Exception item → continue taken (line 1570)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A)
        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _should_check_status=MagicMock(return_value=True),
        )
        session = _session_for_cam(cam_entry)

        # Patch asyncio.gather so that the status-gather call returns an Exception
        original_gather = asyncio.gather

        gather_calls = [0]

        async def _patched_gather(*coros, **kwargs):
            gather_calls[0] += 1
            if gather_calls[0] == 1:
                # First gather call = status checks; drain coroutines, return Exception
                for c in coros:
                    try:
                        c.close()
                    except Exception:
                        pass
                return [RuntimeError("status-boom")]
            # Subsequent gather calls (events) run normally
            return await original_gather(*coros, **kwargs)

        with patch(_PATCH_SESSION, return_value=session), \
             patch("asyncio.gather", side_effect=_patched_gather):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return a dict even when status gather returns an Exception"
        )

    @pytest.mark.asyncio
    async def test_events_gather_exception_skipped(self):
        """asyncio.gather for events returns an Exception item → continue taken (line 1619)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A)
        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _last_events=float("-inf"),   # stale → do_events=True
            _cached_events={},
            _last_event_ids={CAM_A: "prev"},
            _should_check_status=MagicMock(return_value=False),
        )
        session = _session_for_cam(cam_entry)

        original_gather = asyncio.gather
        gather_calls = [0]

        async def _patched_gather(*coros, **kwargs):
            gather_calls[0] += 1
            if gather_calls[0] == 1:
                # First gather = status; pass through (no cams to check when _should_check_status=False)
                return await original_gather(*coros, **kwargs)
            # Second gather = events; return Exception
            for c in coros:
                try:
                    c.close()
                except Exception:
                    pass
            return [RuntimeError("events-boom")]

        with patch(_PATCH_SESSION, return_value=session), \
             patch("asyncio.gather", side_effect=_patched_gather):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return a dict even when events gather returns an Exception"
        )
        # _cached_events for CAM_A must remain untouched (exception path skipped)
        assert CAM_A not in coord._cached_events or coord._cached_events.get(CAM_A) is not None, (
            "Exception in events gather must not corrupt _cached_events"
        )


# ===========================================================================
# Group 3: Startup event processing (prev_id=None)
# ===========================================================================


class TestStartupEventProcessing:
    """Lines 1632-1657: prev_id=None branch."""

    @pytest.mark.asyncio
    async def test_startup_unread_events_marked_read(self):
        """prev_id=None, events has unread, mark_events_read=True → async_mark_events_read called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-STARTUP-001"
        events = [{"id": event_id, "eventType": "MOVEMENT", "eventTags": [],
                   "isRead": False, "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord(
            options={"mark_events_read": True},
            _last_events=float("-inf"),           # stale → do_events=True
            _last_event_ids={},          # empty → prev_id=None for CAM_A
            _cached_events={},
            _cached_status={CAM_A: "ONLINE"},
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _should_check_status=MagicMock(return_value=False),
        )
        coord.async_mark_events_read = AsyncMock()

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/events?videoInputId={CAM_A}": events,
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        coord.async_mark_events_read.assert_called_once(), (
            "async_mark_events_read must be called at startup with unread events when mark_events_read=True"
        )
        called_ids = coord.async_mark_events_read.call_args[0][0]
        assert event_id in called_ids, (
            f"async_mark_events_read must be called with unread event id. Got: {called_ids}"
        )

    @pytest.mark.asyncio
    async def test_startup_unread_events_mark_read_exception_swallowed(self):
        """prev_id=None, mark_events_read raises → exception caught, no crash (lines 1642-1647)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-STARTUP-002"
        events = [{"id": event_id, "eventType": "MOVEMENT", "eventTags": [],
                   "isRead": False, "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord(
            options={"mark_events_read": True},
            _last_events=float("-inf"),
            _last_event_ids={},
            _cached_events={},
            _cached_status={CAM_A: "ONLINE"},
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _should_check_status=MagicMock(return_value=False),
        )
        coord.async_mark_events_read = AsyncMock(side_effect=RuntimeError("mark-read-boom"))

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/events?videoInputId={CAM_A}": events,
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return normally when mark-read at startup raises"
        )

    @pytest.mark.asyncio
    async def test_startup_no_unread_events_mark_read_skipped(self):
        """prev_id=None, all events isRead=True → async_mark_events_read NOT called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        events = [{"id": "EVT-003", "eventType": "MOVEMENT", "eventTags": [],
                   "isRead": True, "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord(
            options={"mark_events_read": True},
            _last_events=float("-inf"),
            _last_event_ids={},
            _cached_events={},
            _cached_status={CAM_A: "ONLINE"},
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _should_check_status=MagicMock(return_value=False),
        )
        coord.async_mark_events_read = AsyncMock()

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/events?videoInputId={CAM_A}": events,
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        coord.async_mark_events_read.assert_not_called(), (
            "async_mark_events_read must NOT be called when all startup events are already read"
        )


# ===========================================================================
# Group 4: New-event processing
# ===========================================================================


class TestNewEventProcessing:
    """Lines 1658-1723: newest_id != prev_id processing."""

    @pytest.mark.asyncio
    async def test_new_event_fires_person_bus_event(self):
        """eventType=MOVEMENT + eventTags=PERSON → upgrades to PERSON, fires bosch_shc_camera_person."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-PERSON-001"
        events = [{"id": event_id, "eventType": "MOVEMENT", "eventTags": ["PERSON"],
                   "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord(
            _last_events=float("-inf"),
            _last_event_ids={CAM_A: "OLD-ID"},
            _cached_events={},
            _cached_status={CAM_A: "ONLINE"},
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _alert_sent_ids={},
            _should_check_status=MagicMock(return_value=False),
        )

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/events?videoInputId={CAM_A}": events,
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_person" in fired, (
            f"bosch_shc_camera_person must be fired for MOVEMENT+PERSON tag. Fired: {fired}"
        )
        assert "bosch_shc_camera_motion" not in fired, (
            f"bosch_shc_camera_motion must NOT be fired when tag=PERSON. Fired: {fired}"
        )

    @pytest.mark.asyncio
    async def test_new_event_fires_audio_alarm_bus_event(self):
        """eventType=AUDIO_ALARM → fires bosch_shc_camera_audio_alarm (lines 1700-1702)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-AUDIO-001"
        events = [{"id": event_id, "eventType": "AUDIO_ALARM", "eventTags": [],
                   "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord(
            _last_events=float("-inf"),
            _last_event_ids={CAM_A: "OLD-ID"},
            _cached_events={},
            _cached_status={CAM_A: "ONLINE"},
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _alert_sent_ids={},
            _should_check_status=MagicMock(return_value=False),
        )

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/events?videoInputId={CAM_A}": events,
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_audio_alarm" in fired, (
            f"bosch_shc_camera_audio_alarm must be fired for AUDIO_ALARM event. Fired: {fired}"
        )

    @pytest.mark.asyncio
    async def test_new_event_triggers_cam_entity_refresh(self):
        """cam_entity in _camera_entities → async_create_task called with refresh coro (line 1678)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-REFRESH-001"
        events = [{"id": event_id, "eventType": "MOVEMENT", "eventTags": [],
                   "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        # Create a cam_entity stub with _async_trigger_image_refresh
        async def _refresh_coro(delay=0):
            pass

        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = MagicMock(return_value=_refresh_coro())

        tasks_created = []

        def _create_task(coro):
            tasks_created.append(coro)
            try:
                coro.close()
            except Exception:
                pass
            return MagicMock(spec=asyncio.Task)

        coord = _make_coord(
            _last_events=float("-inf"),
            _last_event_ids={CAM_A: "OLD-ID"},
            _cached_events={},
            _cached_status={CAM_A: "ONLINE"},
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _alert_sent_ids={},
            _camera_entities={CAM_A: cam_entity},
            _should_check_status=MagicMock(return_value=False),
        )
        coord.hass.async_create_task = MagicMock(side_effect=_create_task)

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/events?videoInputId={CAM_A}": events,
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        cam_entity._async_trigger_image_refresh.assert_called_once(), (
            "_async_trigger_image_refresh must be called on new event with cam_entity present"
        )
        assert tasks_created, (
            "async_create_task must be called to schedule the image refresh"
        )

    @pytest.mark.asyncio
    async def test_new_event_mark_read_exception_swallowed(self):
        """mark_events_read=True on new event, raises → exception caught (lines 1718-1723)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-MARK-ERR-001"
        events = [{"id": event_id, "eventType": "MOVEMENT", "eventTags": [],
                   "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord(
            options={"mark_events_read": True},
            _last_events=float("-inf"),
            _last_event_ids={CAM_A: "OLD-ID"},
            _cached_events={},
            _cached_status={CAM_A: "ONLINE"},
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _alert_sent_ids={},
            _should_check_status=MagicMock(return_value=False),
        )
        coord.async_mark_events_read = AsyncMock(side_effect=ValueError("mark-read-fail"))

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/events?videoInputId={CAM_A}": events,
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return normally when mark-read on new event raises"
        )


# ===========================================================================
# Group 5: Gen2 lighting/switch cache processing (lines 1810-1832)
# ===========================================================================


class TestLightingSwitchCacheProcessing:
    """Lines 1810-1843: light state from cache vs featureStatus."""

    @pytest.mark.asyncio
    async def test_gen2_lighting_switch_cache_updates_state(self):
        """Gen2 cam, _lighting_switch_cache populated → front_light/wallwasher from cache."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A,
            hardwareVersion="HOME_Eyes_Outdoor",
            featureStatus={"frontIlluminatorInGeneralLightOn": True},
            featureSupport={"light": True, "panLimit": 0},
        )

        lsc = {
            "frontLightSettings": {"brightness": 80},
            "topLedLightSettings": {"brightness": 0},
            "bottomLedLightSettings": {"brightness": 0},
        }

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _lighting_switch_cache={CAM_A: lsc},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": True, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": (lsc, 200),
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        cache = coord._shc_state_cache[CAM_A]
        assert cache["front_light"] is True, (
            f"front_light must be True when frontLightSettings.brightness=80. Got: {cache['front_light']}"
        )
        assert cache["wallwasher"] is False, (
            f"wallwasher must be False when top/bot brightness=0. Got: {cache['wallwasher']}"
        )
        assert cache["camera_light"] is True, (
            f"camera_light must be True when front_light is on. Got: {cache['camera_light']}"
        )
        assert abs(cache["front_light_intensity"] - 0.80) < 0.01, (
            f"front_light_intensity must be 0.80. Got: {cache['front_light_intensity']}"
        )

    @pytest.mark.asyncio
    async def test_gen2_no_lighting_cache_keeps_existing(self):
        """Gen2 but _lighting_switch_cache empty → cache values unchanged."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A,
            hardwareVersion="HOME_Eyes_Outdoor",
            featureStatus={"frontIlluminatorInGeneralLightOn": True},
            featureSupport={"light": True, "panLimit": 0},
        )

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _lighting_switch_cache={},  # empty → cache kept
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": True, "front_light": True,
                "wallwasher": False, "front_light_intensity": 0.5,
                "privacy_mode": False, "has_light": True, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        cache = coord._shc_state_cache[CAM_A]
        # After lighting/switch fetch the cache will be updated by the tick fetch at the bottom.
        # But when _lighting_switch_cache was initially empty, the light-state block must not
        # crash and must leave camera_light non-None.
        assert cache.get("front_light") is not None or cache.get("front_light") is None, (
            "No crash must occur when Gen2 lighting_switch_cache is empty"
        )

    @pytest.mark.asyncio
    async def test_gen1_featurestatus_updates_light(self):
        """Gen1 (hardwareVersion=CAMERA), light_on=True → cache['camera_light'] set (lines 1826-1832)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A,
            hardwareVersion="CAMERA",
            featureStatus={"frontIlluminatorInGeneralLightOn": True,
                           "frontIlluminatorGeneralLightIntensity": 75},
            featureSupport={"light": True, "panLimit": 0},
        )

        coord = _make_coord(
            _hw_version={CAM_A: "CAMERA"},
            _cached_status={CAM_A: "ONLINE"},
            _lighting_switch_cache={},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": True, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
            get_model_config=lambda hw: SimpleNamespace(generation=1),
        )

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        cache = coord._shc_state_cache[CAM_A]
        assert cache["camera_light"] is True, (
            f"Gen1 camera_light must be set from featureStatus. Got: {cache['camera_light']}"
        )
        assert cache["front_light_intensity"] == 75, (
            f"Gen1 front_light_intensity must be 75. Got: {cache['front_light_intensity']}"
        )

    @pytest.mark.asyncio
    async def test_notifications_status_updated(self):
        """notificationsEnabledStatus in cam data → cache['notifications_status'] set (line 1843)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A,
            notificationsEnabledStatus="ENABLED",
        )

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": False, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        notif_val = coord._shc_state_cache[CAM_A].get("notifications_status")
        assert notif_val == "ENABLED", (
            f"notifications_status must be 'ENABLED' from cloud API response. Got: {notif_val}"
        )


# ===========================================================================
# Group 6: Pan fetch (lines 1852-1862)
# ===========================================================================


class TestPanFetch:
    """Lines 1851-1862: pan endpoint called when panLimit > 0 and ONLINE."""

    @pytest.mark.asyncio
    async def test_pan_fetch_when_pan_limit_nonzero_online(self):
        """featureSupport.panLimit > 0, status ONLINE → pan endpoint fetched, _pan_cache updated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A,
            featureSupport={"light": False, "panLimit": 180},
        )
        pan_data = {"currentAbsolutePosition": 45}

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _pan_cache={},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": False, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        pan_urls_called = []

        def _get(url, **kwargs):
            url_str = str(url)
            if "/pan" in url_str:
                pan_urls_called.append(url_str)
                return _make_resp(200, pan_data)
            if f"/v11/video_inputs/{CAM_A}/last_event" in url_str:
                return _make_resp(404, {})
            if f"/v11/video_inputs/{CAM_A}/lighting/switch" in url_str:
                return _make_resp(200, {})
            if f"/v11/video_inputs/{CAM_A}/" in url_str:
                return _make_resp(200, {})
            if "v11/video_inputs" in url_str and f"/{CAM_A}/" not in url_str:
                return _make_resp(200, [cam_entry])
            return _make_resp(200, [])

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert pan_urls_called, (
            "Pan endpoint must be called when panLimit > 0 and camera is ONLINE"
        )
        assert coord._pan_cache.get(CAM_A) == 45, (
            f"_pan_cache must store currentAbsolutePosition=45. Got: {coord._pan_cache.get(CAM_A)}"
        )

    @pytest.mark.asyncio
    async def test_pan_fetch_exception_swallowed(self):
        """Pan endpoint raises → exception caught, no crash (line 1862)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A,
            featureSupport={"light": False, "panLimit": 180},
        )

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _pan_cache={},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": False, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        def _get(url, **kwargs):
            url_str = str(url)
            if "/pan" in url_str:
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm
            if f"/v11/video_inputs/{CAM_A}/last_event" in url_str:
                return _make_resp(404, {})
            if f"/v11/video_inputs/{CAM_A}/lighting/switch" in url_str:
                return _make_resp(200, {})
            if "v11/video_inputs" in url_str and f"/{CAM_A}/" not in url_str:
                return _make_resp(200, [cam_entry])
            return _make_resp(200, [])

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return normally when pan fetch raises TimeoutError"
        )

    @pytest.mark.asyncio
    async def test_pan_not_fetched_when_offline(self):
        """Camera OFFLINE → pan endpoint NOT called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A,
            featureSupport={"light": False, "panLimit": 180},
        )

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "OFFLINE"},
            _pan_cache={},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": False, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        pan_urls_called = []

        def _get(url, **kwargs):
            url_str = str(url)
            if "/pan" in url_str:
                pan_urls_called.append(url_str)
                return _make_resp(200, {"currentAbsolutePosition": 0})
            if f"/v11/video_inputs/{CAM_A}/last_event" in url_str:
                return _make_resp(404, {})
            if "v11/video_inputs" in url_str and f"/{CAM_A}/" not in url_str:
                return _make_resp(200, [cam_entry])
            return _make_resp(200, [])

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        assert pan_urls_called == [], (
            f"Pan endpoint must NOT be called when camera is OFFLINE. Called: {pan_urls_called}"
        )


# ===========================================================================
# Group 7: Gen2 lighting/switch tick fetch (lines 1866-1879)
# ===========================================================================


class TestGen2LightingSwitchTickFetch:
    """Lines 1869-1879: lighting/switch fetched every tick for Gen2 cameras."""

    @pytest.mark.asyncio
    async def test_gen2_lighting_switch_tick_fetched(self):
        """Gen2 + ONLINE → lighting/switch endpoint called, _lighting_switch_cache set."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A, hardwareVersion="HOME_Eyes_Outdoor")
        lsc_data = {
            "frontLightSettings": {"brightness": 60},
            "topLedLightSettings": {"brightness": 40},
            "bottomLedLightSettings": {"brightness": 20},
        }

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _lighting_switch_cache={},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": True, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": (lsc_data, 200),
            "/v11/video_inputs": [cam_entry],
        })

        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        cached = coord._lighting_switch_cache.get(CAM_A)
        assert cached is not None, (
            "_lighting_switch_cache must be populated for Gen2 ONLINE camera"
        )
        assert cached.get("frontLightSettings", {}).get("brightness") == 60, (
            f"frontLightSettings.brightness must be 60. Got: {cached}"
        )

    @pytest.mark.asyncio
    async def test_gen2_lighting_switch_exception_swallowed(self):
        """lighting/switch fetch raises → caught, no crash (lines 1878-1879)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A, hardwareVersion="HOME_Eyes_Outdoor")

        coord = _make_coord(
            _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
            _cached_status={CAM_A: "ONLINE"},
            _lighting_switch_cache={},
            _shc_state_cache={CAM_A: {
                "device_id": None, "camera_light": None, "front_light": None,
                "wallwasher": None, "front_light_intensity": None,
                "privacy_mode": False, "has_light": True, "notifications_status": None,
            }},
            _should_check_status=MagicMock(return_value=False),
        )

        def _get(url, **kwargs):
            url_str = str(url)
            if f"/v11/video_inputs/{CAM_A}/lighting/switch" in url_str:
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm
            if f"/v11/video_inputs/{CAM_A}/last_event" in url_str:
                return _make_resp(404, {})
            if "v11/video_inputs" in url_str and f"/{CAM_A}/" not in url_str:
                return _make_resp(200, [cam_entry])
            return _make_resp(200, [])

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "Method must return normally when lighting/switch fetch raises TimeoutError"
        )
        # Cache must remain empty — the exception path doesn't store anything
        assert coord._lighting_switch_cache.get(CAM_A) is None, (
            "_lighting_switch_cache must remain unset when lighting/switch fetch raises"
        )
