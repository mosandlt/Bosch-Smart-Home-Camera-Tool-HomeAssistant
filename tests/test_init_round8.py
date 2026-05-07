"""Sprint B: __init__.py keepalive, WebRTC watchdog, lifecycle, services.

Covers missing lines in:
  _refresh_rcp_state (3334-3335, 3338)
  _check_and_recover_webrtc (3341-3370, 3390-3436)
  _auto_renew_local_session early-exit branches (3625-3759)
  _async_cancel_coordinator_tasks (4444-4497)
  async_unload_entry (4501-4505)
  _async_options_updated (4515-4522)
  _register_services handlers (4530-5113)
"""

from __future__ import annotations

import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _resp_cm(status: int, text: str = "", body: bytes = b"",
             headers: dict | None = None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=body or text.encode())
    resp.json = AsyncMock(return_value={})
    resp.headers = headers or {}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _stub_coord(**kwargs):
    hass = MagicMock()
    coord = SimpleNamespace(
        token="test-token",
        hass=hass,
        _entry=SimpleNamespace(entry_id="01ENTRY"),
        _live_connections={},
        _rcp_state_cache={},
        _camera_entities={},
        _auto_renew_generation={},
        _session_stale={},
        _renewal_tasks={},
        _auto_renew_tasks={},
        _last_schemes_refresh=float('-inf'),
        _last_go2rtc_reload=float('-inf'),
        _proxy_url_cache={},
        _options_snapshot={},
    )
    coord.get_model_config = MagicMock()
    coord.try_live_connection = AsyncMock(return_value=None)
    coord._refresh_local_creds_from_heartbeat = MagicMock()
    coord._ensure_go2rtc_schemes_fresh = AsyncMock()
    for k, v in kwargs.items():
        setattr(coord, k, v)
    return coord


# ── _refresh_rcp_state ───────────────────────────────────────────────────────


class TestRefreshRcpState:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._refresh_rcp_state = types.MethodType(
            BoschCameraCoordinator._refresh_rcp_state, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_empty_cache_no_update(self):
        """Empty _rcp_state_cache → setdefault returns {} → if cache: skipped."""
        coord = self._bind(_stub_coord())
        await coord._refresh_rcp_state(CAM_ID)
        # cache stays empty (setdefault returns {}, falsy → no assignment)
        assert coord._rcp_state_cache.get(CAM_ID) == {}

    @pytest.mark.asyncio
    async def test_non_empty_cache_updated(self):
        """Pre-populated cache → source + fetched_at are written."""
        coord = self._bind(_stub_coord(
            _rcp_state_cache={CAM_ID: {"some_key": "val"}},
            _live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
        ))
        await coord._refresh_rcp_state(CAM_ID)
        cache = coord._rcp_state_cache[CAM_ID]
        assert cache["source"] == "local"
        assert "fetched_at" in cache

    @pytest.mark.asyncio
    async def test_no_live_connection_uses_question_mark(self):
        """No active connection → source derived from '?' → stored as '?'."""
        coord = self._bind(_stub_coord(
            _rcp_state_cache={CAM_ID: {"old": True}},
            _live_connections={},
        ))
        await coord._refresh_rcp_state(CAM_ID)
        assert coord._rcp_state_cache[CAM_ID]["source"] == "?"


# ── _check_and_recover_webrtc ────────────────────────────────────────────────


class TestCheckAndRecoverWebrtc:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._check_and_recover_webrtc = types.MethodType(
            BoschCameraCoordinator._check_and_recover_webrtc, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_no_cam_entity_returns_early(self):
        coord = self._bind(_stub_coord(_camera_entities={}))
        with patch("asyncio.sleep", AsyncMock()):
            await coord._check_and_recover_webrtc(CAM_ID)
        coord._ensure_go2rtc_schemes_fresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_feature_not_supported_returns_early(self):
        from homeassistant.components.camera import CameraEntityFeature
        cam_entity = MagicMock()
        cam_entity.supported_features = MagicMock()
        # CameraEntityFeature.STREAM not in supported_features
        cam_entity.supported_features.__contains__ = MagicMock(return_value=False)
        coord = self._bind(_stub_coord(_camera_entities={CAM_ID: cam_entity}))
        with patch("asyncio.sleep", AsyncMock()):
            await coord._check_and_recover_webrtc(CAM_ID)
        coord._ensure_go2rtc_schemes_fresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_webrtc_already_present_returns_early(self):
        from homeassistant.components.camera import CameraEntityFeature, StreamType
        cam_entity = MagicMock()
        cam_entity.supported_features.__contains__ = MagicMock(return_value=True)
        caps = MagicMock()
        caps.frontend_stream_types = {StreamType.WEB_RTC}
        cam_entity.camera_capabilities = caps
        coord = self._bind(_stub_coord(_camera_entities={CAM_ID: cam_entity}))
        with patch("asyncio.sleep", AsyncMock()):
            await coord._check_and_recover_webrtc(CAM_ID)
        coord._ensure_go2rtc_schemes_fresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_schemes_refresh_restores_webrtc(self):
        """Direct refresh fixes the issue → no go2rtc reload needed."""
        from homeassistant.components.camera import CameraEntityFeature, StreamType
        cam_entity = MagicMock()
        cam_entity.supported_features.__contains__ = MagicMock(return_value=True)
        # First caps check: WEB_RTC missing; after refresh: WEB_RTC present
        caps_bad = MagicMock()
        caps_bad.frontend_stream_types = {StreamType.HLS}
        caps_good = MagicMock()
        caps_good.frontend_stream_types = {StreamType.WEB_RTC}
        cam_entity.camera_capabilities = caps_bad

        def _side_effect():
            cam_entity.camera_capabilities = caps_good
        coord = self._bind(_stub_coord(_camera_entities={CAM_ID: cam_entity}))
        coord._ensure_go2rtc_schemes_fresh = AsyncMock(side_effect=_side_effect)

        with patch("asyncio.sleep", AsyncMock()):
            await coord._check_and_recover_webrtc(CAM_ID)

        coord._ensure_go2rtc_schemes_fresh.assert_awaited_once()
        coord.hass.config_entries.async_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_throttled_if_recently_reloaded(self):
        """Already reloaded within 3600s → skip go2rtc reload."""
        import time
        from homeassistant.components.camera import CameraEntityFeature, StreamType
        cam_entity = MagicMock()
        cam_entity.supported_features.__contains__ = MagicMock(return_value=True)
        caps = MagicMock()
        caps.frontend_stream_types = {StreamType.HLS}
        cam_entity.camera_capabilities = caps
        coord = self._bind(_stub_coord(
            _camera_entities={CAM_ID: cam_entity},
            _last_go2rtc_reload=time.monotonic() - 60,  # reloaded 60s ago
        ))
        with patch("asyncio.sleep", AsyncMock()):
            await coord._check_and_recover_webrtc(CAM_ID)
        coord.hass.config_entries.async_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_go2rtc_entries_skips_reload(self):
        """No loaded go2rtc config entries → nothing to reload."""
        from homeassistant.components.camera import CameraEntityFeature, StreamType
        cam_entity = MagicMock()
        cam_entity.supported_features.__contains__ = MagicMock(return_value=True)
        caps = MagicMock()
        caps.frontend_stream_types = {StreamType.HLS}
        cam_entity.camera_capabilities = caps
        coord = self._bind(_stub_coord(_camera_entities={CAM_ID: cam_entity}))
        coord.hass.config_entries.async_entries.return_value = []
        with patch("asyncio.sleep", AsyncMock()):
            await coord._check_and_recover_webrtc(CAM_ID)
        coord.hass.config_entries.async_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_go2rtc_reload_called(self):
        """WEB_RTC missing + no recent reload + entry present → reload called."""
        import time
        from homeassistant.components.camera import CameraEntityFeature, StreamType
        from homeassistant.config_entries import ConfigEntryState
        cam_entity = MagicMock()
        cam_entity.supported_features.__contains__ = MagicMock(return_value=True)
        caps = MagicMock()
        caps.frontend_stream_types = {StreamType.HLS}
        cam_entity.camera_capabilities = caps
        cam_entity.async_refresh_providers = AsyncMock()

        go2rtc_entry = MagicMock()
        go2rtc_entry.state = ConfigEntryState.LOADED
        go2rtc_entry.entry_id = "go2rtc-01"

        coord = self._bind(_stub_coord(
            _camera_entities={CAM_ID: cam_entity},
            _last_go2rtc_reload=float('-inf'),
        ))
        coord.hass.config_entries.async_entries.return_value = [go2rtc_entry]
        coord.hass.config_entries.async_reload = AsyncMock()

        with patch("asyncio.sleep", AsyncMock()):
            await coord._check_and_recover_webrtc(CAM_ID)

        coord.hass.config_entries.async_reload.assert_called_once_with("go2rtc-01")


# ── _auto_renew_local_session early exits ───────────────────────────────────


class TestAutoRenewLocalSession:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._auto_renew_local_session = types.MethodType(
            BoschCameraCoordinator._auto_renew_local_session, coord
        )
        return coord

    def _model_cfg(self, heartbeat=3600, renewal=86400):
        cfg = MagicMock()
        cfg.heartbeat_interval = heartbeat
        cfg.renewal_interval = renewal
        return cfg

    @pytest.mark.asyncio
    async def test_cancelled_error_exits_gracefully(self):
        """CancelledError during sleep → logs + cleanup, no re-raise."""
        coord = self._bind(_stub_coord(
            _auto_renew_generation={CAM_ID: 1},
            _live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
            _renewal_tasks={CAM_ID: MagicMock()},
        ))
        coord.get_model_config.return_value = self._model_cfg()
        with patch("asyncio.sleep", AsyncMock(side_effect=asyncio.CancelledError())):
            await coord._auto_renew_local_session(CAM_ID, generation=1)
        # Must not raise; renewal_tasks cleaned up
        assert CAM_ID not in coord._renewal_tasks

    @pytest.mark.asyncio
    async def test_stale_generation_breaks_loop(self):
        """Generation mismatch → loop exits after first iteration."""
        coord = self._bind(_stub_coord(
            _auto_renew_generation={CAM_ID: 2},  # current gen=2, task gen=1
            _live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
            _renewal_tasks={},
        ))
        coord.get_model_config.return_value = self._model_cfg()
        with patch("asyncio.sleep", AsyncMock()):
            await coord._auto_renew_local_session(CAM_ID, generation=1)

    @pytest.mark.asyncio
    async def test_no_live_connection_breaks_loop(self):
        """cam_id not in _live_connections → break."""
        coord = self._bind(_stub_coord(
            _auto_renew_generation={CAM_ID: 1},
            _live_connections={},  # cam not in live_connections
            _renewal_tasks={},
        ))
        coord.get_model_config.return_value = self._model_cfg()
        with patch("asyncio.sleep", AsyncMock()):
            await coord._auto_renew_local_session(CAM_ID, generation=1)

    @pytest.mark.asyncio
    async def test_not_local_type_breaks_loop(self):
        """connection_type != LOCAL → break."""
        coord = self._bind(_stub_coord(
            _auto_renew_generation={CAM_ID: 1},
            _live_connections={CAM_ID: {"_connection_type": "REMOTE"}},
            _renewal_tasks={},
        ))
        coord.get_model_config.return_value = self._model_cfg()
        with patch("asyncio.sleep", AsyncMock()):
            await coord._auto_renew_local_session(CAM_ID, generation=1)


# ── _async_cancel_coordinator_tasks ─────────────────────────────────────────


class TestAsyncCancelCoordinatorTasks:
    def _make_coord(self):
        task1 = MagicMock()
        task1.done.return_value = False
        task1.cancel = MagicMock()
        bg_task = MagicMock()
        bg_task.done.return_value = False
        bg_task.cancel = MagicMock()
        coord = SimpleNamespace(
            async_stop_fcm_push=AsyncMock(),
            _token_refresh_handle=None,
            _renewal_tasks={"cam1": task1},
            _bg_tasks={bg_task},
            _nvr_drain_task=None,
            _tls_proxy_ports={},
            _stream_log_listener=None,
        )
        return coord, task1, bg_task

    @pytest.mark.asyncio
    async def test_renewal_tasks_cancelled(self):
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks
        coord, task1, bg_task = self._make_coord()
        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            await _async_cancel_coordinator_tasks(coord)
        task1.cancel.assert_called_once()
        assert coord._renewal_tasks == {}

    @pytest.mark.asyncio
    async def test_bg_tasks_cancelled(self):
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks
        coord, task1, bg_task = self._make_coord()
        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            await _async_cancel_coordinator_tasks(coord)
        bg_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_token_refresh_handle_cancelled(self):
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks
        handle = MagicMock()
        coord, task1, bg_task = self._make_coord()
        coord._token_refresh_handle = handle
        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            await _async_cancel_coordinator_tasks(coord)
        handle.cancel.assert_called_once()
        assert coord._token_refresh_handle is None

    @pytest.mark.asyncio
    async def test_nvr_drain_task_cancelled(self):
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks
        drain_task = AsyncMock(side_effect=asyncio.CancelledError())
        drain_task.done = MagicMock(return_value=False)
        drain_task.cancel = MagicMock()
        coord, _, _ = self._make_coord()
        coord._nvr_drain_task = drain_task
        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            await _async_cancel_coordinator_tasks(coord)
        drain_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_listener_removed(self):
        import logging
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks
        listener = MagicMock()
        coord, _, _ = self._make_coord()
        coord._stream_log_listener = listener
        stream_logger = MagicMock()
        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])), \
             patch("logging.getLogger", return_value=stream_logger):
            await _async_cancel_coordinator_tasks(coord)
        stream_logger.removeHandler.assert_called_once_with(listener)
        assert coord._stream_log_listener is None

    @pytest.mark.asyncio
    async def test_stop_all_proxies_called(self):
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks
        coord, _, _ = self._make_coord()
        coord._tls_proxy_ports = {"cam1": 12345}
        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies") as mock_stop, \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            await _async_cancel_coordinator_tasks(coord)
        mock_stop.assert_called_once_with({"cam1": 12345})


# ── async_unload_entry ───────────────────────────────────────────────────────


class TestAsyncUnloadEntry:
    @pytest.mark.asyncio
    async def test_with_coord_cancels_tasks_and_unloads(self):
        from custom_components.bosch_shc_camera import async_unload_entry
        coord = MagicMock()
        entry = MagicMock()
        entry.runtime_data = coord
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        with patch(f"{MODULE}._async_cancel_coordinator_tasks", AsyncMock()) as mock_cancel:
            result = await async_unload_entry(hass, entry)
        mock_cancel.assert_awaited_once_with(coord)
        assert result is True

    @pytest.mark.asyncio
    async def test_without_coord_still_unloads(self):
        from custom_components.bosch_shc_camera import async_unload_entry
        entry = MagicMock()
        entry.runtime_data = None
        hass = MagicMock()
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        with patch(f"{MODULE}._async_cancel_coordinator_tasks", AsyncMock()) as mock_cancel:
            result = await async_unload_entry(hass, entry)
        mock_cancel.assert_not_awaited()
        assert result is True


# ── _async_options_updated ───────────────────────────────────────────────────


class TestAsyncOptionsUpdated:
    @pytest.mark.asyncio
    async def test_options_unchanged_skips_reload(self):
        from custom_components.bosch_shc_camera import _async_options_updated
        opts = {"scan_interval": 60}
        coord = SimpleNamespace(_options_snapshot=opts)
        entry = MagicMock()
        entry.runtime_data = coord
        entry.data = {}
        entry.options = opts
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        with patch(f"{MODULE}.get_options", return_value=opts):
            await _async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_options_changed_triggers_reload(self):
        from custom_components.bosch_shc_camera import _async_options_updated
        coord = SimpleNamespace(_options_snapshot={"scan_interval": 60})
        entry = MagicMock()
        entry.runtime_data = coord
        entry.data = {}
        entry.options = {"scan_interval": 30}  # different
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        await _async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_coord_triggers_reload(self):
        from custom_components.bosch_shc_camera import _async_options_updated
        entry = MagicMock()
        entry.runtime_data = None
        hass = MagicMock()
        hass.config_entries.async_reload = AsyncMock()
        await _async_options_updated(hass, entry)
        hass.config_entries.async_reload.assert_awaited_once()


# ── _register_services ───────────────────────────────────────────────────────


def _make_hass_for_services(already_registered=False):
    hass = MagicMock()
    hass.services.has_service.return_value = already_registered
    hass.services.async_register = MagicMock()
    hass.config_entries.async_loaded_entries.return_value = []
    hass.async_create_task = MagicMock()
    return hass


def _get_handlers(hass):
    """Extract {service_name: handler} from async_register call history."""
    return {
        c.args[1]: c.args[2]
        for c in hass.services.async_register.call_args_list
    }


class TestRegisterServicesGuard:
    def test_already_registered_skips(self):
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass_for_services(already_registered=True)
        _register_services(hass)
        hass.services.async_register.assert_not_called()

    def test_not_registered_registers_all(self):
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass_for_services(already_registered=False)
        _register_services(hass)
        assert hass.services.async_register.call_count >= 5


class TestHandleTriggerSnapshot:
    @pytest.mark.asyncio
    async def test_no_entries_does_nothing(self):
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass_for_services()
        _register_services(hass)
        handler = _get_handlers(hass)["trigger_snapshot"]
        call_mock = MagicMock()
        await handler(call_mock)
        hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_entries_with_coord_schedules_refresh(self):
        from custom_components.bosch_shc_camera import _register_services
        coord = MagicMock()
        coord.async_request_refresh = AsyncMock()
        coord._camera_entities = {}
        entry = MagicMock()
        entry.runtime_data = coord
        hass = _make_hass_for_services()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        tasks_created = []
        hass.async_create_task = lambda coro: tasks_created.append(coro)
        _register_services(hass)
        handler = _get_handlers(hass)["trigger_snapshot"]
        await handler(MagicMock())
        # clean up any created coroutines to avoid ResourceWarning
        for coro in tasks_created:
            if asyncio.iscoroutine(coro):
                coro.close()


class TestHandleOpenLiveConnection:
    @pytest.mark.asyncio
    async def test_missing_camera_id_raises_service_validation_error(self):
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass_for_services()
        _register_services(hass)
        handler = _get_handlers(hass)["open_live_connection"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": ""}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)

    @pytest.mark.asyncio
    async def test_no_entries_raises_ha_error(self):
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass_for_services()
        _register_services(hass)
        handler = _get_handlers(hass)["open_live_connection"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": CAM_ID}
        with pytest.raises(HomeAssistantError):
            await handler(call_mock)

    @pytest.mark.asyncio
    async def test_conn_success_returns_without_error(self):
        from custom_components.bosch_shc_camera import _register_services
        coord = MagicMock()
        coord.try_live_connection = AsyncMock(return_value={"urls": ["proxy/hash"]})
        entry = MagicMock()
        entry.runtime_data = coord
        hass = _make_hass_for_services()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        _register_services(hass)
        handler = _get_handlers(hass)["open_live_connection"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": CAM_ID, "renewal": False}
        await handler(call_mock)  # must not raise


class TestHandleCreateRule:
    @pytest.mark.asyncio
    async def test_http_200_logs_and_returns(self):
        from custom_components.bosch_shc_camera import _register_services
        coord = MagicMock()
        coord.token = "tok"
        entry = MagicMock()
        entry.runtime_data = coord
        hass = _make_hass_for_services()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(201, text='{"id":"r1"}'))
        resp_mock = session.post.return_value.__aenter__.return_value
        resp_mock.json = AsyncMock(return_value={"id": "r1"})

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["create_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "name": "Test", "start_time": "08:00:00",
                              "end_time": "20:00:00", "weekdays": [0, 6], "is_active": True}
            await handler(call_mock)

    @pytest.mark.asyncio
    async def test_http_error_raises_ha_error(self):
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        coord = MagicMock()
        coord.token = "tok"
        entry = MagicMock()
        entry.runtime_data = coord
        hass = _make_hass_for_services()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(500))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["create_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


class TestHandleDeleteRule:
    @pytest.mark.asyncio
    async def test_http_204_succeeds(self):
        from custom_components.bosch_shc_camera import _register_services
        coord = MagicMock()
        coord.token = "tok"
        entry = MagicMock()
        entry.runtime_data = coord
        hass = _make_hass_for_services()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(return_value=_resp_cm(204))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "rule-1"}
            await handler(call_mock)  # must not raise

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        coord = MagicMock()
        coord.token = "tok"
        entry = MagicMock()
        entry.runtime_data = coord
        hass = _make_hass_for_services()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(return_value=_resp_cm(404))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "rule-1"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


class TestHandleUpdateRule:
    @pytest.mark.asyncio
    async def test_missing_ids_raises_service_validation_error(self):
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass_for_services()
        _register_services(hass)
        handler = _get_handlers(hass)["update_rule"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": "", "rule_id": ""}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)

    @pytest.mark.asyncio
    async def test_rule_in_cache_puts_update(self):
        from custom_components.bosch_shc_camera import _register_services
        existing_rule = {"id": "rule-1", "name": "Old", "isActive": True,
                         "startTime": "08:00:00", "endTime": "20:00:00", "weekdays": [0]}
        coord = MagicMock()
        coord.token = "tok"
        coord._rules_cache = {CAM_ID: [existing_rule]}
        coord.async_request_refresh = AsyncMock()
        entry = MagicMock()
        entry.runtime_data = coord
        hass = _make_hass_for_services()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(200, text='{"id":"rule-1"}'))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["update_rule"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID, "rule_id": "rule-1",
                "is_active": False,
            }
            await handler(call_mock)
        session.put.assert_called_once()
