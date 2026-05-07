"""Sprint J3: targeted line coverage for __init__.py uncovered branches.

Targets:
  - async_setup (line 4213): _register_services(hass) call, HA-not-running path
  - _async_cancel_coordinator_tasks: handle.cancel() raises RuntimeError (4427-4428)
  - _async_cancel_coordinator_tasks: nvr_recorder.stop_all raises (4464-4465)
  - handle_trigger_snapshot (4513-4514): camera entity loop creates tasks
  - handle_create_rule exception path (4562-4563)
  - handle_delete_rule exception path (4587-4588)
  - handle_update_rule PUT exception (4651-4652)
  - handle_get_motion_zones exception (4738-4739)
  - handle_share_camera exception (4788-4789)
  - handle_get_privacy_masks exception (4834-4835)
  - handle_set_privacy_masks exception (4871-4872)
  - handle_delete_motion_zone exception (4912-4913)
  - handle_get_lighting_schedule exception (4964-4965)
  - handle_rename_camera exception (4993-4994)
  - handle_invite_friend exception (5026-5027)
  - handle_list_friends exception (5062-5063)
  - handle_remove_friend exception (5087-5088)

Pattern: identical to test_services_round1.py — mock hass, extract closures
via _get_handlers(), patch async_get_clientsession, assert HomeAssistantError.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
FRIEND_ID = "friend-abc-123"


# ── Shared helpers ────────────────────────────────────────────────────────────


def _resp_cm(status: int, text: str = "", json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_hass(already_registered=False):
    hass = MagicMock()
    hass.services.has_service.return_value = already_registered
    hass.services.async_register = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config_entries.async_loaded_entries.return_value = []
    hass.async_create_task = MagicMock()
    return hass


def _get_handlers(hass):
    """Return {service_name: handler} from async_register call list."""
    return {c.args[1]: c.args[2] for c in hass.services.async_register.call_args_list}


def _entry_with_coord(**coord_kwargs):
    coord = MagicMock()
    coord.token = "tok-A"
    coord.async_request_refresh = AsyncMock()
    coord._rules_cache = {}
    for k, v in coord_kwargs.items():
        setattr(coord, k, v)
    entry = MagicMock()
    entry.runtime_data = coord
    return entry, coord


def _make_cancel_coord(**overrides):
    """Minimal coord stub for _async_cancel_coordinator_tasks tests."""
    task = MagicMock()
    task.done.return_value = False
    task.cancel = MagicMock()
    coord = SimpleNamespace(
        async_stop_fcm_push=AsyncMock(),
        _token_refresh_handle=None,
        _renewal_tasks={},
        _bg_tasks=set(),
        _nvr_drain_task=None,
        _tls_proxy_ports={},
        _stream_log_listener=None,
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


# ── async_setup ───────────────────────────────────────────────────────────────


class TestAsyncSetup:
    def _make_lovelace_hass(self, is_running: bool = True):
        """Build a minimal hass mock that satisfies async_setup's Lovelace path."""
        hass = MagicMock()
        hass.is_running = is_running
        hass.http = MagicMock()
        hass.http.async_register_static_paths = AsyncMock()
        hass.bus = MagicMock()
        hass.bus.async_listen_once = MagicMock()
        hass.async_create_task = MagicMock()
        hass.services = MagicMock()
        hass.services.has_service = MagicMock(return_value=True)  # skip re-register

        resources = MagicMock()
        resources.async_load = AsyncMock()
        resources.async_items = MagicMock(return_value=[])
        resources.async_create_item = AsyncMock()
        resources.async_update_item = AsyncMock()
        resources.async_delete_item = AsyncMock()
        lovelace = MagicMock()
        lovelace.resources = resources
        hass.data = {"lovelace": lovelace}
        return hass

    @pytest.mark.asyncio
    async def test_async_setup_calls_register_services(self):
        """async_setup must call _register_services(hass) — line 4213."""
        from custom_components.bosch_shc_camera import async_setup

        hass = self._make_lovelace_hass(is_running=True)

        with patch(f"{MODULE}._register_services") as mock_reg:
            result = await async_setup(hass, {})

        mock_reg.assert_called_once_with(hass)
        assert result is True

    @pytest.mark.asyncio
    async def test_async_setup_not_running_registers_listener(self):
        """When HA is not yet running, async_setup registers an EVENT_HOMEASSISTANT_STARTED
        listener via hass.bus.async_listen_once — not an immediate await."""
        from custom_components.bosch_shc_camera import async_setup

        hass = self._make_lovelace_hass(is_running=False)

        with patch(f"{MODULE}._register_services"):
            result = await async_setup(hass, {})

        hass.bus.async_listen_once.assert_called_once()
        assert result is True

    @pytest.mark.asyncio
    async def test_async_setup_is_running_no_listener(self):
        """When HA is already running, async_listen_once must NOT be called."""
        from custom_components.bosch_shc_camera import async_setup

        hass = self._make_lovelace_hass(is_running=True)

        with patch(f"{MODULE}._register_services"):
            await async_setup(hass, {})

        hass.bus.async_listen_once.assert_not_called()


# ── _async_cancel_coordinator_tasks — exception branches ─────────────────────


class TestCancelCoordinatorTasksExceptions:
    @pytest.mark.asyncio
    async def test_token_refresh_handle_cancel_raises_runtime_error(self):
        """handle.cancel() raising RuntimeError must be swallowed and logged (4427-4428)."""
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks

        handle = MagicMock()
        handle.cancel.side_effect = RuntimeError("already cancelled")
        coord = _make_cancel_coord(_token_refresh_handle=handle)

        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            # Must NOT propagate the RuntimeError
            await _async_cancel_coordinator_tasks(coord)

        handle.cancel.assert_called_once()
        # After exception, handle must still be set to None
        assert coord._token_refresh_handle is None

    @pytest.mark.asyncio
    async def test_token_refresh_handle_cancel_raises_attribute_error(self):
        """handle.cancel() raising AttributeError must also be swallowed (4427-4428)."""
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks

        handle = MagicMock()
        handle.cancel.side_effect = AttributeError("no cancel method")
        coord = _make_cancel_coord(_token_refresh_handle=handle)

        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock()), \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            await _async_cancel_coordinator_tasks(coord)

        handle.cancel.assert_called_once()
        assert coord._token_refresh_handle is None

    @pytest.mark.asyncio
    async def test_nvr_stop_all_exception_swallowed(self):
        """nvr_recorder.stop_all raising must be caught and debug-logged (4464-4465)."""
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks

        coord = _make_cancel_coord()

        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock(side_effect=RuntimeError("nvr crash"))) as mock_stop, \
             patch(f"{MODULE}.stop_all_proxies"), \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            # Must NOT propagate
            await _async_cancel_coordinator_tasks(coord)

        mock_stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_nvr_stop_all_exception_does_not_block_proxy_stop(self):
        """Even when nvr stop_all raises, stop_all_proxies must still be called."""
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks

        coord = _make_cancel_coord(_tls_proxy_ports={"cam1": 9999})

        with patch(f"{MODULE}.nvr_recorder.stop_all", AsyncMock(side_effect=OSError("disk full"))), \
             patch(f"{MODULE}.stop_all_proxies") as mock_proxy, \
             patch("asyncio.gather", AsyncMock(return_value=[])):
            await _async_cancel_coordinator_tasks(coord)

        mock_proxy.assert_called_once_with({"cam1": 9999})


# ── handle_trigger_snapshot — camera entity loop ──────────────────────────────


class TestHandleTriggerSnapshot:
    @pytest.mark.asyncio
    async def test_creates_task_per_camera_entity(self):
        """For each camera in coord._camera_entities, async_create_task must be called
        (line 4514). Also calls async_request_refresh per entry."""
        from custom_components.bosch_shc_camera import _register_services

        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)

        coord = MagicMock()
        coord.async_request_refresh = AsyncMock()
        coord._camera_entities = {"cam-a": cam_entity, "cam-b": MagicMock()}
        for ent in coord._camera_entities.values():
            if hasattr(ent, "_async_trigger_image_refresh"):
                continue
            ent._async_trigger_image_refresh = AsyncMock(return_value=None)

        entry = MagicMock()
        entry.runtime_data = coord

        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        _register_services(hass)
        handler = _get_handlers(hass)["trigger_snapshot"]
        call_mock = MagicMock()
        call_mock.data = {}
        await handler(call_mock)

        # async_create_task: 1 for async_request_refresh + 2 for camera entities
        assert hass.async_create_task.call_count >= 2

    @pytest.mark.asyncio
    async def test_no_entries_no_tasks(self):
        """With no loaded entries, async_create_task must not be called."""
        from custom_components.bosch_shc_camera import _register_services

        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = []

        _register_services(hass)
        handler = _get_handlers(hass)["trigger_snapshot"]
        call_mock = MagicMock()
        call_mock.data = {}
        await handler(call_mock)

        hass.async_create_task.assert_not_called()


# ── handle_create_rule — exception path ──────────────────────────────────────


class TestHandleCreateRuleException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """Non-HomeAssistantError from session.post → HomeAssistantError (4562-4563)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(side_effect=OSError("network error"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["create_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "name": "Test Rule"}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert "Create rule" in str(exc_info.value) or exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_timeout_error_raises_homeassistant_error(self):
        """asyncio.TimeoutError from session.post → wrapped HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(side_effect=TimeoutError("timed out"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["create_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_delete_rule — exception path ──────────────────────────────────────


class TestHandleDeleteRuleException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """Non-HomeAssistantError from session.delete → HomeAssistantError (4587-4588)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(side_effect=OSError("connection reset"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "rule-42"}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_http_error_response_raises_homeassistant_error(self):
        """HTTP 500 from delete → HomeAssistantError with http_error key."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(return_value=_resp_cm(500, "server error"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "rule-42"}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "http_error"


# ── handle_update_rule — PUT exception path ───────────────────────────────────


class TestHandleUpdateRulePutException:
    @pytest.mark.asyncio
    async def test_put_network_error_raises_homeassistant_error(self):
        """session.put raising OSError during update → HomeAssistantError (4651-4652)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        existing = {"id": "r1", "name": "Old", "isActive": True,
                    "startTime": "08:00:00", "endTime": "20:00:00", "weekdays": [0]}
        entry, coord = _entry_with_coord(_rules_cache={CAM_ID: [existing]})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(side_effect=OSError("broken pipe"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["update_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "r1", "name": "New"}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"


# ── handle_get_motion_zones — exception path ──────────────────────────────────


class TestHandleGetMotionZonesException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.get raising → HomeAssistantError (4738-4739)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(side_effect=OSError("timeout"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_missing_cam_id_raises_service_validation_error(self):
        """Empty camera_id → ServiceValidationError before HTTP call."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services

        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["get_motion_zones"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": ""}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)


# ── handle_share_camera — exception path ─────────────────────────────────────


class TestHandleShareCameraException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.put raising → HomeAssistantError (4788-4789)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(side_effect=OSError("network unreachable"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["share_camera"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": FRIEND_ID, "camera_ids": [CAM_ID], "days": 7}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_missing_friend_id_raises_service_validation_error(self):
        """Empty friend_id → ServiceValidationError."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services

        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["share_camera"]
        call_mock = MagicMock()
        call_mock.data = {"friend_id": "", "camera_ids": [CAM_ID]}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)


# ── handle_get_privacy_masks — exception path ─────────────────────────────────


class TestHandleGetPrivacyMasksException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.get raising → HomeAssistantError (4834-4835)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(side_effect=OSError("ssl error"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_http_500_raises_homeassistant_error(self):
        """HTTP 500 from GET privacy_masks → HomeAssistantError with http_error_with_body."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500, "internal server error"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "http_error_with_body"


# ── handle_set_privacy_masks — exception path ─────────────────────────────────


class TestHandleSetPrivacyMasksException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.post raising → HomeAssistantError (4871-4872)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(side_effect=OSError("connection refused"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "masks": [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}],
            }
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_missing_mask_field_raises_service_validation_error(self):
        """Mask missing required field 'h' → ServiceValidationError with missing_field."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services

        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["set_privacy_masks"]
        call_mock = MagicMock()
        call_mock.data = {
            "camera_id": CAM_ID,
            "masks": [{"x": 0.1, "y": 0.2, "w": 0.3}],  # missing "h"
        }
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(call_mock)
        assert exc_info.value.translation_key == "missing_field"


# ── handle_delete_motion_zone — exception path ────────────────────────────────


class TestHandleDeleteMotionZoneException:
    @pytest.mark.asyncio
    async def test_fetch_network_error_raises_homeassistant_error(self):
        """session.get raising → HomeAssistantError (4912-4913)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(side_effect=OSError("network reset"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_motion_zone"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "zone_index": 0}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_missing_args_raises_service_validation_error(self):
        """Empty camera_id + negative zone_index → ServiceValidationError."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services

        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["delete_motion_zone"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": "", "zone_index": -1}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)


# ── handle_get_lighting_schedule — exception path ────────────────────────────


class TestHandleGetLightingScheduleException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.get raising → HomeAssistantError (4964-4965)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        coord._lighting_options_cache = {}
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(side_effect=OSError("ssl handshake failed"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_lighting_schedule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_cached_data_sends_notification_without_http(self):
        """When _lighting_options_cache has data, no HTTP call is made."""
        from custom_components.bosch_shc_camera import _register_services

        cached = {
            "scheduleStatus": "scheduled",
            "generalLightOnTime": "18:00:00",
            "generalLightOffTime": "06:00:00",
            "darknessThreshold": 50,
            "lightOnMotion": True,
            "lightOnMotionFollowUpTimeSeconds": 30,
            "frontIlluminatorInGeneralLightOn": True,
            "wallwasherInGeneralLightOn": False,
            "frontIlluminatorGeneralLightIntensity": 0.8,
        }
        entry, coord = _entry_with_coord()
        coord._lighting_options_cache = {CAM_ID: cached}
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.services.async_call = AsyncMock()

        session = MagicMock()
        session.get = MagicMock()  # must NOT be called

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_lighting_schedule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        session.get.assert_not_called()
        hass.services.async_call.assert_awaited_once()


# ── handle_rename_camera — exception path ─────────────────────────────────────


class TestHandleRenameCameraException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.put raising → HomeAssistantError (4993-4994)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(side_effect=OSError("connection refused"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["rename_camera"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "new_name": "Garage"}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_missing_args_raises_service_validation_error(self):
        """Missing camera_id/new_name → ServiceValidationError."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services

        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["rename_camera"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": CAM_ID, "new_name": ""}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)


# ── handle_invite_friend — exception path ─────────────────────────────────────


class TestHandleInviteFriendException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.post raising → HomeAssistantError (5026-5027)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(side_effect=OSError("dns resolution failed"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["invite_friend"]
            call_mock = MagicMock()
            call_mock.data = {"email": "test@example.com"}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_http_error_response_raises_homeassistant_error(self):
        """HTTP 400 → HomeAssistantError with http_error_with_body key."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(400, "bad request"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["invite_friend"]
            call_mock = MagicMock()
            call_mock.data = {"email": "bad@example.com"}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "http_error_with_body"


# ── handle_list_friends — exception path ──────────────────────────────────────


class TestHandleListFriendsException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.get raising → HomeAssistantError (5062-5063)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(side_effect=OSError("connection reset by peer"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["list_friends"]
            call_mock = MagicMock()
            call_mock.data = {}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_http_error_response_raises_homeassistant_error(self):
        """HTTP 503 → HomeAssistantError with http_error key."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(503))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["list_friends"]
            call_mock = MagicMock()
            call_mock.data = {}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "http_error"


# ── handle_remove_friend — exception path ─────────────────────────────────────


class TestHandleRemoveFriendException:
    @pytest.mark.asyncio
    async def test_network_error_raises_homeassistant_error(self):
        """session.delete raising → HomeAssistantError (5087-5088)."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(side_effect=OSError("broken pipe"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["remove_friend"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": FRIEND_ID}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "unexpected_error"

    @pytest.mark.asyncio
    async def test_http_error_response_raises_homeassistant_error(self):
        """HTTP 404 → HomeAssistantError with http_error key."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services

        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(return_value=_resp_cm(404))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["remove_friend"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": FRIEND_ID}
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)

        assert exc_info.value.translation_key == "http_error"

    @pytest.mark.asyncio
    async def test_missing_friend_id_raises_service_validation_error(self):
        """Empty friend_id → ServiceValidationError before HTTP call."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services

        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["remove_friend"]
        call_mock = MagicMock()
        call_mock.data = {"friend_id": ""}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)
