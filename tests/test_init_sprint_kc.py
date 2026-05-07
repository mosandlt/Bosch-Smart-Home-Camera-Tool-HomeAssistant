"""Sprint KC tests — targeting _async_update_data lines 1624-2181 of __init__.py.

Coverage targets (in order):
  - Lines 1629-1734: Event processing + dedup logic in the cam_ids loop
  - Lines 1746-1798: Privacy mode from cloud API + external toggle detection
  - Lines 1885-2055: Slow tier — wifiinfo / audio / motion parallel fetch
  - Lines 2107-2115: SHC states supplementary update gate
  - Lines 2176-2181: Outer exception handlers (TimeoutError, ClientError)

All tests run without a running HA instance: SimpleNamespace stubs the coordinator,
AsyncMock / MagicMock stub collaborators. Unbound method calls use the pattern
    BoschCameraCoordinator.method_name(coord, *args)
so the real code path executes on our lightweight stub.
"""
from __future__ import annotations

import asyncio
import threading
import time as time_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── Shared helpers ─────────────────────────────────────────────────────────────


def _make_coord_full(cam_id: str = CAM_A, **overrides):
    """Coordinator stub for _async_update_data tests.

    Pre-seeds all fields needed so the method reaches the section under test
    without hitting AttributeError on missing attributes.
    """

    def _create_task(c):
        try:
            c.close()
        except Exception:
            pass
        return MagicMock()

    base = dict(
        token="tok-A",
        refresh_token="rfr-B",
        options={},
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
            entry_id="01KM38DHZ525S61HPENAT7NHC0",
        ),
        _feature_flags={"x": 1},       # pre-populated → skip FF fetch
        _protocol_checked=True,         # pre-done → skip
        _first_tick_done=True,          # skip fast-first-tick guard → process events+slow
        _fcm_lock=threading.Lock(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_client=None,
        _last_status=time_mod.monotonic(),    # recent → skip status
        _last_events=float('-inf'),     # stale → run events
        _last_slow=time_mod.monotonic(),      # recent → skip slow by default
        _last_smb_cleanup=time_mod.monotonic(),
        _last_nvr_cleanup=time_mod.monotonic(),
        _last_smb_disk_check=time_mod.monotonic(),
        _hw_version={cam_id: "HOME_Eyes_Outdoor"},
        _cached_status={cam_id: "ONLINE"},
        _cached_events={},
        _offline_since={},
        _per_cam_status_at={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_error_at={},
        _live_connections={},
        _local_promote_at={},
        _lan_tcp_reachable={},
        _rcp_lan_ip_cache={},
        _local_creds_cache={},
        _shc_state_cache={cam_id: {
            "device_id": None,
            "camera_light": None,
            "front_light": None,
            "wallwasher": None,
            "front_light_intensity": None,
            "privacy_mode": False,
            "has_light": False,
            "notifications_status": None,
        }},
        _wifiinfo_cache={},
        _ambient_light_cache={},
        _lighting_switch_cache={},
        _pan_cache={},
        _notif_set_at={},
        _privacy_set_at={},
        _light_set_at={},
        _audio_alarm_set_at={},
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        _arming_set_at={},
        _last_event_ids={},
        _event_dedup_cache={},
        _alert_sent_ids={},
        _audio_alarm_cache={},
        _firmware_cache={},
        _unread_events_cache={},
        _privacy_sound_cache={},
        _commissioned_cache={},
        _timestamp_cache={},
        _ledlights_cache={},
        _lens_elevation_cache={},
        _audio_cache={},
        _motion_light_cache={},
        _ambient_lighting_cache={},
        _global_lighting_cache={},
        _notifications_cache={},
        _rules_cache={},
        _cloud_zones_cache={},
        _cloud_privacy_masks_cache={},
        _lighting_options_cache={},
        _intrusion_config_cache={},
        _alarm_settings_cache={},
        _alarm_status_cache={},
        _arming_cache={},
        _icon_led_brightness_cache={},
        _gen2_zones_cache={},
        _gen2_private_areas_cache={},
        _camera_entities={},
        _integration_version="11.0.10",
        _OFFLINE_EXTENDED_INTERVAL=900,
        _WRITE_LOCK_SECS=30.0,
        shc_ready=False,
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _should_check_status=MagicMock(return_value=False),   # skip status
        _ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        async_mark_events_read=AsyncMock(),
        async_handle_fcm_push=AsyncMock(),
        _tear_down_live_stream=AsyncMock(),
        _is_write_locked=MagicMock(return_value=False),
        _cleanup_stale_devices=MagicMock(),
        get_model_config=lambda cid: SimpleNamespace(generation=2),
        get_quality_params=lambda cid: (True, {}),
        is_camera_online=lambda cid: True,
        _async_send_alert=AsyncMock(),
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_create_background_task=MagicMock(side_effect=_create_task),
            async_add_executor_job=AsyncMock(),
            data={},
            bus=SimpleNamespace(async_fire=MagicMock()),
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp"),
        ),
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    return ns


def _make_cam_entry(cam_id: str = CAM_A, **fields) -> dict:
    """Minimal camera dict as returned by /v11/video_inputs."""
    base = {
        "id": cam_id,
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "title": "Terrasse",
        "privacyMode": "OFF",
        "featureSupport": {"light": False, "panLimit": 0},
        "featureStatus": {},
        "status": {"isCommissioned": True, "isConnected": True},
    }
    base.update(fields)
    return base


def _url_session(url_map: dict, default_json=None):
    """aiohttp session mock routing GET by URL substring (longest pattern wins).

    url_map: {substring: json_value_or_(json_value, status_code)}
    Patterns are tested longest-first so specific sub-paths win over generic ones.
    """

    def _make_resp(json_val, status: int = 200):
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

    # Sort by pattern length descending — longer patterns are more specific
    _sorted_patterns = sorted(url_map.keys(), key=len, reverse=True)

    def _get(url, **kwargs):
        url_str = str(url)
        for pattern in _sorted_patterns:
            if pattern in url_str:
                val = url_map[pattern]
                if isinstance(val, tuple):
                    return _make_resp(val[0], val[1])
                return _make_resp(val)
        return _make_resp(default_json or [])

    session.get = _get
    session.put = AsyncMock()
    return session


def _session_for_cam(cam_entry: dict, events: list | None = None) -> MagicMock:
    """Convenience: session returning cam_entry from /v11/video_inputs and events from /events."""
    cam_id = cam_entry["id"]
    return _url_session({
        # Specific sub-paths must come before the generic /v11/video_inputs
        # (sorted longest-first, so these win over the base path)
        f"/v11/video_inputs/{cam_id}/last_event": ({"id": ""}, 404),  # force full fetch
        f"/v11/video_inputs/{cam_id}/lighting/switch": ({}, 200),
        f"/v11/video_inputs/{cam_id}/ping": "ONLINE",
        f"/v11/video_inputs/{cam_id}/commissioned": {"connected": True, "commissioned": True},
        f"/v11/events?videoInputId={cam_id}": events or [],
        "/v11/video_inputs": [cam_entry],  # base list — matched last (shortest)
    })


# ── Section 1: Event processing + dedup ───────────────────────────────────────


class TestEventProcessing:
    """_async_update_data lines 1629-1734: event loop logic.

    WHY: The event-detection path is the primary motion-alert mechanism.
    We verify new-event detection, dedup, skip-when-disabled, and timestamp
    update — without these the automation chain (bosch_shc_camera_motion) is
    silently broken.
    """

    @pytest.mark.asyncio
    async def test_new_event_fires_alert(self):
        """do_events=True + new event → hass.bus.async_fire called for MOVEMENT.

        Pins the core alert path: a new event_id that was never seen before
        must trigger the HA bus event and async_send_alert.
        The session must return the event from /v11/events (the fetch overwrites
        _cached_events, so pre-seeding the cache alone is not enough).
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-001"
        events = [{"id": event_id, "eventType": "MOVEMENT", "eventTags": [], "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord_full(
            _last_events=float('-inf'),    # force do_events=True
            _cached_events={},
            _last_event_ids={CAM_A: "OLD-ID"},  # different from EVT-001 → new event
            _alert_sent_ids={},
        )

        # Session must return events from /v11/events so _cached_events is populated
        session = _session_for_cam(cam_entry, events=events)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        coord.hass.bus.async_fire.assert_called(), (
            "hass.bus.async_fire must be called when a new MOVEMENT event is detected"
        )
        fired_events = [call.args[0] for call in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_motion" in fired_events, (
            f"Expected 'bosch_shc_camera_motion' in fired events, got: {fired_events}"
        )

    @pytest.mark.asyncio
    async def test_event_dedup_suppresses_duplicate_alert(self):
        """_alert_sent_ids already contains newest event_id → no bus event fired.

        The dedup guard (line 1663) prevents a duplicate alert when FCM already
        dispatched the same event_id within the last 60 seconds.
        The session must return the event so _cached_events is populated before
        the dedup check runs.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        event_id = "EVT-ALREADY-SENT"
        events = [{"id": event_id, "eventType": "MOVEMENT", "eventTags": [], "timestamp": "2026-01-01T00:00:00Z"}]
        cam_entry = _make_cam_entry(CAM_A)

        # Mark as already sent 10 seconds ago → within the 60s dedup window
        coord = _make_coord_full(
            _cached_events={},
            _last_events=0.0,  # force do_events=True
            _last_event_ids={CAM_A: "PREV-DIFFERENT"},  # triggers new-event branch
            _alert_sent_ids={event_id: time_mod.monotonic() - 10.0},  # recently sent
        )

        session = _session_for_cam(cam_entry, events=events)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        fired_events = [call.args[0] for call in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_motion" not in fired_events, (
            "Duplicate alert must be suppressed when event_id is in _alert_sent_ids "
            f"within 60s. Fired events: {fired_events}"
        )

    @pytest.mark.asyncio
    async def test_do_events_false_skips_event_loop(self):
        """do_events=False → event loop skipped, no bus event ever fired.

        When FCM is healthy the polling interval is 300s; on the fast first tick
        do_events is always False. No events must be processed in that case.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        events = [{"id": "EVT-002", "eventType": "MOVEMENT", "eventTags": [], "timestamp": "t"}]
        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord_full(
            # Force do_events=False: _last_events very recent + long interval via fcm_healthy
            _last_events=time_mod.monotonic(),  # recent → do_events=False
            _cached_events={CAM_A: events},
            _last_event_ids={CAM_A: "PREV"},
            _alert_sent_ids={},
        )

        session = _session_for_cam(cam_entry)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        fired_events = [call.args[0] for call in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_motion" not in fired_events, (
            "No events must be fired when do_events=False (fast first tick or FCM healthy)"
        )

    @pytest.mark.asyncio
    async def test_timestamps_updated_after_do_events(self):
        """After a successful run with do_events=True, _last_events is updated.

        Line 1738: `if do_events: self._last_events = now`
        Pins that the timestamp sentinel advances so the next tick correctly
        computes whether it's time to poll events again.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A)
        initial_last_events = float('-inf')

        coord = _make_coord_full(
            _last_events=initial_last_events,  # stale → do_events=True
            _cached_events={CAM_A: []},
        )

        session = _session_for_cam(cam_entry)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._last_events > initial_last_events, (
            f"_last_events ({coord._last_events}) must be greater than "
            f"initial ({initial_last_events}) after a do_events=True run"
        )


# ── Section 2: Privacy mode from cloud API ────────────────────────────────────


class TestPrivacyModeFromCloud:
    """_async_update_data lines 1746-1798: privacy detection from cloud API.

    WHY: External privacy toggles (physical button, Bosch app) must be detected
    and acted upon even when our own privacy switch didn't change. If not caught,
    the TLS proxy enters an endless reconnect loop against a blocked camera
    (ECONNREFUSED 113) while the HA switch stays "streaming" — misleading the user.
    """

    @pytest.mark.asyncio
    async def test_external_privacy_on_tears_down_stream(self):
        """privacyMode=ON in cloud + active stream + cache was OFF → _tear_down_live_stream called.

        Lines 1786-1797: hardware/external privacy trigger detection. When privacy
        transitions OFF→ON and there is an active live connection, we must call
        _tear_down_live_stream so go2rtc and the TLS proxy stop cleanly.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A, privacyMode="ON")

        coord = _make_coord_full(
            _cached_events={CAM_A: []},
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},  # active stream
            _shc_state_cache={CAM_A: {
                "device_id": None,
                "camera_light": None,
                "front_light": None,
                "wallwasher": None,
                "front_light_intensity": None,
                "privacy_mode": False,   # was OFF before this tick
                "has_light": False,
                "notifications_status": None,
            }},
            _privacy_set_at={},  # no write lock
        )

        session = _session_for_cam(cam_entry)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._tear_down_live_stream.assert_called(), (
            "_tear_down_live_stream must be scheduled when external privacyMode=ON "
            "is detected and there is an active live connection"
        )
        call_args = coord._tear_down_live_stream.call_args_list
        cam_ids_called = [a[0][0] for a in call_args]
        assert CAM_A in cam_ids_called, (
            f"_tear_down_live_stream must be called for {CAM_A}, called for: {cam_ids_called}"
        )

    @pytest.mark.asyncio
    async def test_privacy_write_lock_prevents_cloud_override(self):
        """Write lock active (_privacy_set_at recent) → cloud privacyMode must not override cache.

        Lines 1767-1770: if a write happened within _WRITE_LOCK_SECS, skip the
        cloud update. This prevents a stale cloud value from reverting the user's
        own privacy toggle during the ~20s Bosch propagation window.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A, privacyMode="ON")

        coord = _make_coord_full(
            _cached_events={CAM_A: []},
            _live_connections={},  # no active stream
            _shc_state_cache={CAM_A: {
                "device_id": None,
                "camera_light": None,
                "front_light": None,
                "wallwasher": None,
                "front_light_intensity": None,
                "privacy_mode": False,   # cache says OFF
                "has_light": False,
                "notifications_status": None,
            }},
            # Write lock: our switch turned privacy OFF 5s ago → cloud still says ON
            _privacy_set_at={CAM_A: time_mod.monotonic() - 5.0},
        )

        session = _session_for_cam(cam_entry)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        # Cache must still say False — the write lock blocked the cloud override
        privacy_in_cache = coord._shc_state_cache[CAM_A]["privacy_mode"]
        assert privacy_in_cache is False, (
            f"Write lock must prevent cloud privacyMode=ON from overriding cache. "
            f"Cache says: {privacy_in_cache}"
        )
        # And _tear_down_live_stream must NOT have been called
        coord._tear_down_live_stream.assert_not_called(), (
            "_tear_down_live_stream must not be called when write lock is active"
        )


# ── Section 3: Outer exception handlers ───────────────────────────────────────


class TestOuterExceptionHandlers:
    """_async_update_data lines 2176-2181: outer try/except block.

    WHY: The coordinator must translate raw network errors into UpdateFailed so
    HA shows the "Integration unavailable" banner instead of a Python traceback
    in the logs. These exception handlers are the last line of defense.
    """

    @pytest.mark.asyncio
    async def test_timeout_error_raises_update_failed(self):
        """asyncio.TimeoutError during camera list GET → raises UpdateFailed.

        Line 2178-2179: `except asyncio.TimeoutError: raise UpdateFailed(...)`.
        Confirms the outer handler maps the timeout to a user-readable error.
        """
        from homeassistant.helpers.update_coordinator import UpdateFailed
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_full()

        # Session that raises TimeoutError on the very first GET
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        # Make the context manager for session.get raise TimeoutError
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=cm)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            with pytest.raises(UpdateFailed) as exc_info:
                await BoschCameraCoordinator._async_update_data(coord)

        assert "Timeout" in str(exc_info.value) or "timeout" in str(exc_info.value).lower(), (
            f"UpdateFailed message must mention timeout. Got: {exc_info.value}"
        )

    @pytest.mark.asyncio
    async def test_client_error_raises_update_failed(self):
        """aiohttp.ClientError during camera list GET → raises UpdateFailed(f'Network error: {err}').

        Lines 2180-2181: `except aiohttp.ClientError as err: raise UpdateFailed(f'Network error: ...')`.
        """
        import aiohttp
        from homeassistant.helpers.update_coordinator import UpdateFailed
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_full()

        # Session that raises ClientConnectionError on GET
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientConnectionError("connection refused"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=cm)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            with pytest.raises(UpdateFailed) as exc_info:
                await BoschCameraCoordinator._async_update_data(coord)

        assert "Network error" in str(exc_info.value), (
            f"UpdateFailed message must contain 'Network error'. Got: {exc_info.value}"
        )


# ── Section 4: Slow tier — wifiinfo / audio / motion ──────────────────────────


class TestSlowTier:
    """_async_update_data lines 1885-2055: slow-tier parallel endpoint fetch.

    WHY: The slow tier fetches wifiinfo, motion settings, audioAlarm, firmware, etc.
    These are expensive (~13 endpoints × 5s timeout). We verify the fetch runs
    when do_slow=True + camera ONLINE, and is skipped when do_slow=False — and
    that the wifiinfo result is stored in _wifiinfo_cache.
    """

    @pytest.mark.asyncio
    async def test_slow_tier_wifiinfo_populated(self):
        """do_slow=True + camera ONLINE → wifiinfo fetched and stored in _wifiinfo_cache.

        Lines 1961-1962: `if ep == 'wifiinfo': self._wifiinfo_cache[cam_id_key] = ep_data`.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A, hardwareVersion="HOME_Eyes_Outdoor")
        wifiinfo_data = {"signalStrength": -55, "ssid": "my-network"}

        coord = _make_coord_full(
            _last_slow=float('-inf'),  # stale → do_slow=True
            _last_events=time_mod.monotonic(),   # recent → skip events
            _cached_events={CAM_A: []},
            _cached_status={CAM_A: "ONLINE"},
            _wifiinfo_cache={},
        )

        # Session: video_inputs returns our cam, wifiinfo returns wifiinfo_data.
        # Use full paths (longest-match wins) so specific sub-paths don't get
        # swallowed by the generic "/v11/video_inputs" entry.
        session = _url_session({
            f"/v11/video_inputs/{CAM_A}/last_event": ({"id": ""}, 404),
            f"/v11/video_inputs/{CAM_A}/lighting/switch": ({}, 200),
            f"/v11/video_inputs/{CAM_A}/lighting/motion": {},
            f"/v11/video_inputs/{CAM_A}/lighting/ambient": {},
            f"/v11/video_inputs/{CAM_A}/lighting": {},
            f"/v11/video_inputs/{CAM_A}/intrusionDetectionConfig": {},
            f"/v11/video_inputs/{CAM_A}/ambient_light_sensor_level": {"ambientLightSensorLevel": 0.5},
            f"/v11/video_inputs/{CAM_A}/recording_options": {},
            f"/v11/video_inputs/{CAM_A}/unread_events_count": {"count": 0},
            f"/v11/video_inputs/{CAM_A}/privacy_sound_override": {"result": False},
            f"/v11/video_inputs/{CAM_A}/commissioned": {"connected": True, "commissioned": True},
            f"/v11/video_inputs/{CAM_A}/autofollow": {},
            f"/v11/video_inputs/{CAM_A}/notifications": {},
            f"/v11/video_inputs/{CAM_A}/privateAreas": [],
            f"/v11/video_inputs/{CAM_A}/timestamp": {"result": True},
            f"/v11/video_inputs/{CAM_A}/audioAlarm": {"sensitivity": "medium"},
            f"/v11/video_inputs/{CAM_A}/firmware": {"version": "9.40.25"},
            f"/v11/video_inputs/{CAM_A}/wifiinfo": wifiinfo_data,
            f"/v11/video_inputs/{CAM_A}/motion": {"enabled": True},
            f"/v11/video_inputs/{CAM_A}/ledlights": {"state": "OFF"},
            f"/v11/video_inputs/{CAM_A}/lens_elevation": {"elevation": 0.0},
            f"/v11/video_inputs/{CAM_A}/audio": {},
            f"/v11/video_inputs/{CAM_A}/rules": [],
            f"/v11/video_inputs/{CAM_A}/zones": [],
            f"/v11/video_inputs/{CAM_A}/ping": "ONLINE",
            f"/v11/events?videoInputId={CAM_A}": [],
            f"/v11/video_inputs/{CAM_A}/connection": ({"urls": []}, 200),
            "/v11/video_inputs": [cam_entry],   # base list — matched last
        })

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ), patch("aiohttp.TCPConnector"):
            await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A in coord._wifiinfo_cache, (
            "_wifiinfo_cache must contain the cam_id entry after do_slow=True + ONLINE"
        )
        assert coord._wifiinfo_cache[CAM_A] == wifiinfo_data, (
            f"_wifiinfo_cache must store the API response. Got: {coord._wifiinfo_cache[CAM_A]}"
        )

    @pytest.mark.asyncio
    async def test_slow_tier_skipped_when_do_slow_false(self):
        """do_slow=False → slow-tier fetch skipped, _wifiinfo_cache stays empty.

        Lines 1885-1887: `if do_slow and is_online:` gate prevents expensive
        endpoint fetches when the slow-tier interval hasn't elapsed.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord_full(
            _last_slow=time_mod.monotonic(),   # recent → do_slow=False
            _last_events=time_mod.monotonic(), # recent → do_events=False
            _cached_events={CAM_A: []},
            _cached_status={CAM_A: "ONLINE"},
            _wifiinfo_cache={},
        )

        # Track whether slow-tier endpoint was called
        wifiinfo_called = []

        def _get(url, **kwargs):
            r = MagicMock()
            r.status = 200
            r.__aenter__ = AsyncMock(return_value=r)
            r.__aexit__ = AsyncMock(return_value=None)
            if "/wifiinfo" in str(url):
                wifiinfo_called.append(url)
                r.json = AsyncMock(return_value={"ssid": "test"})
            elif "/v11/video_inputs" in str(url) and "video_inputs/" not in str(url):
                r.json = AsyncMock(return_value=[cam_entry])
            elif "/lighting/switch" in str(url):
                r.json = AsyncMock(return_value={})
            else:
                r.json = AsyncMock(return_value=[])
            r.text = AsyncMock(return_value="")
            return r

        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        session.get = _get
        session.put = AsyncMock()

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A not in coord._wifiinfo_cache, (
            "_wifiinfo_cache must remain empty when do_slow=False"
        )
        assert wifiinfo_called == [], (
            f"wifiinfo endpoint must not be called when do_slow=False. Called URLs: {wifiinfo_called}"
        )


# ── Section 5: SHC states update ──────────────────────────────────────────────


class TestShcStatesUpdate:
    """_async_update_data lines 2107-2115: SHC supplementary state fetch gate.

    WHY: The SHC local API is authoritative for camera light state and serves
    as fallback when the cloud is unreachable. But calling it when shc_ready=False
    (SHC not configured or unavailable) would cause an AttributeError or hang.
    We pin the gate logic so a future refactor can't accidentally call
    _async_update_shc_states when SHC is not ready.
    """

    @pytest.mark.asyncio
    async def test_shc_ready_true_calls_update_shc_states(self):
        """shc_ready=True → _async_update_shc_states called once.

        Line 2110: `if self.shc_ready: await self._async_update_shc_states(data)`.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord_full(
            shc_ready=True,
            _last_events=time_mod.monotonic(),
            _cached_events={CAM_A: []},
        )

        session = _session_for_cam(cam_entry)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_update_shc_states.assert_called_once(), (
            "_async_update_shc_states must be called exactly once when shc_ready=True"
        )

    @pytest.mark.asyncio
    async def test_shc_ready_false_skips_update_shc_states(self):
        """shc_ready=False → _async_update_shc_states NOT called.

        Guards against calling the SHC API when the SHC controller is not
        configured or is offline (would hang or raise).
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_entry = _make_cam_entry(CAM_A)

        coord = _make_coord_full(
            shc_ready=False,
            _last_events=time_mod.monotonic(),
            _cached_events={CAM_A: []},
        )

        session = _session_for_cam(cam_entry)

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_update_shc_states.assert_not_called(), (
            "_async_update_shc_states must NOT be called when shc_ready=False"
        )
