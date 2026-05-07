"""Sprint D: __init__.py service handler happy-paths and error branches.

Covers missing lines 4607–5086: handle_update_rule (cache-miss API fetch,
field overlay, PUT 200/204/error, rule-not-found), handle_set_motion_zones
(zone coord validation: missing field, out-of-range; HTTP 200/443/500),
handle_get_motion_zones (empty/non-empty, 443, error), handle_share_camera
(string→list normalization, 204, error), handle_get_privacy_masks/masks
(empty/non-empty, 443, missing-field validation), handle_delete_motion_zone
(index OOB, fetch+delete cycle, fetch error), handle_get_lighting_schedule
(cache hit, API fetch 200, HTTP error), handle_rename_camera (missing args,
204, error), handle_invite_friend (201+notification, error), handle_list_friends
(empty, non-empty, error), handle_remove_friend (204, error).

Pattern identical to test_init_round8.py — mock hass, extract closures,
patch async_get_clientsession to avoid real network calls.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _resp_cm(status: int, text: str = "", json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_data or {})
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


# ── handle_update_rule — uncovered branches ───────────────────────────────────


class TestHandleUpdateRuleRemaining:
    @pytest.mark.asyncio
    async def test_rule_not_in_cache_fetches_from_api(self):
        """When rule is not in _rules_cache, handler fetches from API via GET."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord(_rules_cache={CAM_ID: []})
        coord.async_request_refresh = AsyncMock()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        rules_resp = _resp_cm(200, json_data=[{"id": "rule-1", "name": "Old",
                                               "isActive": True, "startTime": "08:00:00",
                                               "endTime": "20:00:00", "weekdays": [0]}])
        put_resp = _resp_cm(204)
        session = MagicMock()
        session.get = MagicMock(return_value=rules_resp)
        session.put = MagicMock(return_value=put_resp)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["update_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "rule-1", "name": "New Name"}
            await handler(call_mock)

        session.get.assert_called_once()
        session.put.assert_called_once()
        coord.async_request_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rule_not_found_in_api_raises_service_validation_error(self):
        """Rule missing in both cache and API → ServiceValidationError with 'not_found'."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord(_rules_cache={CAM_ID: []})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        rules_resp = _resp_cm(200, json_data=[])  # empty list — rule not there
        session = MagicMock()
        session.get = MagicMock(return_value=rules_resp)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["update_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "missing-rule"}
            with pytest.raises(ServiceValidationError) as exc_info:
                await handler(call_mock)
        assert exc_info.value.translation_key == "not_found", \
            "Missing rule must raise not_found ServiceValidationError"

    @pytest.mark.asyncio
    async def test_field_overlay_applied_to_existing(self):
        """All overlay fields (name, is_active, start_time, end_time, weekdays) are applied."""
        from custom_components.bosch_shc_camera import _register_services
        existing = {"id": "r1", "name": "Old", "isActive": True,
                    "startTime": "08:00:00", "endTime": "20:00:00", "weekdays": [0, 6]}
        entry, coord = _entry_with_coord(_rules_cache={CAM_ID: [existing]})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        captured_put = []

        async def fake_put(*args, **kwargs):
            captured_put.append(kwargs.get("json", {}))
            return _resp_cm(200).__aenter__.return_value

        put_cm = MagicMock()
        put_cm.__aenter__ = AsyncMock(side_effect=lambda: captured_put.append(True) or _resp_cm(200).__aenter__.return_value)
        put_cm.__aexit__ = AsyncMock(return_value=None)

        # Simpler: just track that put was called with new data via assert on session
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(200))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["update_rule"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID, "rule_id": "r1",
                "name": "New Name", "is_active": False,
                "start_time": "09:00:00", "end_time": "21:00:00",
                "weekdays": [1, 2, 3],
            }
            await handler(call_mock)

        put_call_kwargs = session.put.call_args
        sent_json = put_call_kwargs[1]["json"]
        assert sent_json["name"] == "New Name", "name must be overlaid"
        assert sent_json["isActive"] is False, "isActive must be overlaid"
        assert sent_json["startTime"] == "09:00:00", "startTime must be overlaid"
        assert sent_json["endTime"] == "21:00:00", "endTime must be overlaid"
        assert sent_json["weekdays"] == [1, 2, 3], "weekdays must be overlaid"

    @pytest.mark.asyncio
    async def test_put_error_raises_ha_error(self):
        """PUT returning non-2xx must raise HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        existing = {"id": "r1", "name": "X", "isActive": True,
                    "startTime": "08:00:00", "endTime": "20:00:00", "weekdays": []}
        entry, coord = _entry_with_coord(_rules_cache={CAM_ID: [existing]})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(422, text="Unprocessable"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["update_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "r1"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

    @pytest.mark.asyncio
    async def test_api_fetch_exception_raises_ha_error(self):
        """Exception during GET for rules → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord(_rules_cache={CAM_ID: []})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get.side_effect = Exception("network error")

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["update_rule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "rule_id": "r1"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_set_motion_zones ───────────────────────────────────────────────────


class TestHandleSetMotionZones:
    @pytest.mark.asyncio
    async def test_zone_missing_field_raises_validation_error(self):
        """Zone dict missing required key → ServiceValidationError with 'missing_field'."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["set_motion_zones"]
        call_mock = MagicMock()
        call_mock.data = {
            "camera_id": CAM_ID,
            "zones": [{"x": 0.1, "y": 0.1, "w": 0.5}],  # 'h' missing
        }
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(call_mock)
        assert exc_info.value.translation_key == "missing_field", \
            "Missing coordinate key must raise missing_field"

    @pytest.mark.asyncio
    async def test_zone_value_out_of_range_raises_validation_error(self):
        """Zone coordinate > 1.0 → ServiceValidationError with 'value_out_of_range'."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["set_motion_zones"]
        call_mock = MagicMock()
        call_mock.data = {
            "camera_id": CAM_ID,
            "zones": [{"x": 0.1, "y": 0.1, "w": 1.5, "h": 0.5}],  # w > 1.0
        }
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(call_mock)
        assert exc_info.value.translation_key == "value_out_of_range", \
            "Coordinate > 1.0 must raise value_out_of_range"

    @pytest.mark.asyncio
    async def test_negative_zone_value_raises_validation_error(self):
        """Zone coordinate < 0.0 → ServiceValidationError."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["set_motion_zones"]
        call_mock = MagicMock()
        call_mock.data = {
            "camera_id": CAM_ID,
            "zones": [{"x": -0.1, "y": 0.0, "w": 0.5, "h": 0.5}],
        }
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)

    @pytest.mark.asyncio
    async def test_http_200_refreshes_coordinator(self):
        """HTTP 200 → coordinator refresh requested."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(200))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "zones": [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}],
            }
            await handler(call_mock)

        coord.async_request_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_443_raises_privacy_blocked(self):
        """HTTP 443 → HomeAssistantError with translation_key='privacy_blocked'."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(443))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "zones": [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}],
            }
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)
        assert exc_info.value.translation_key == "privacy_blocked", \
            "HTTP 443 from motion zones endpoint must raise privacy_blocked"

    @pytest.mark.asyncio
    async def test_http_500_raises_ha_error(self):
        """HTTP 500 → HomeAssistantError with body text."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(500, text="Internal Server Error"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "zones": [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}],
            }
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

    @pytest.mark.asyncio
    async def test_exception_wraps_to_ha_error(self):
        """aiohttp exception → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post.side_effect = Exception("connection refused")

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "zones": [{"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}],
            }
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_get_motion_zones ───────────────────────────────────────────────────


class TestHandleGetMotionZones:
    @pytest.mark.asyncio
    async def test_empty_zones_creates_notification(self):
        """Empty zone list → persistent notification with 'Keine Motion-Zonen' message."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[]))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        hass.services.async_call.assert_awaited_once()
        call_args = hass.services.async_call.call_args
        assert "Keine Motion-Zonen" in call_args[0][2]["message"], \
            "Empty zone list must show 'Keine Motion-Zonen' message"

    @pytest.mark.asyncio
    async def test_non_empty_zones_lists_them(self):
        """Non-empty zones → notification lists each zone with coordinates."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        zones = [{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}]
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=zones))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        call_args = hass.services.async_call.call_args
        msg = call_args[0][2]["message"]
        assert "Zone 1" in msg, "Zone listing must include 'Zone 1'"
        assert "0.100" in msg, "Zone coordinates must appear in notification"

    @pytest.mark.asyncio
    async def test_http_443_creates_notification_and_raises(self):
        """HTTP 443 → persistent notification + HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(443))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_500_raises_ha_error(self):
        """HTTP 500 → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500, text="err"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_motion_zones"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_share_camera ───────────────────────────────────────────────────────


class TestHandleShareCamera:
    @pytest.mark.asyncio
    async def test_string_camera_id_converted_to_list(self):
        """camera_ids given as string → converted to [string] before PUT."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(204))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["share_camera"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": "friend-1", "camera_ids": CAM_ID, "days": 7}
            await handler(call_mock)

        put_json = session.put.call_args[1]["json"]
        assert isinstance(put_json, list), "camera_ids string must be wrapped in list"
        assert put_json[0]["videoInputId"] == CAM_ID

    @pytest.mark.asyncio
    async def test_http_204_sends_notification(self):
        """HTTP 204 → persistent notification sent."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(204))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["share_camera"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": "friend-1", "camera_ids": [CAM_ID], "days": 30}
            await handler(call_mock)

        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        """HTTP 403 → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(403, text="Forbidden"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["share_camera"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": "friend-1", "camera_ids": [CAM_ID], "days": 30}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

    @pytest.mark.asyncio
    async def test_share_time_uses_days_param(self):
        """Share time end must be ~`days` days from now."""
        from custom_components.bosch_shc_camera import _register_services
        import datetime
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(200))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["share_camera"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": "f1", "camera_ids": [CAM_ID], "days": 14}
            await handler(call_mock)

        put_json = session.put.call_args[1]["json"]
        end_str = put_json[0]["shareTime"]["end"]
        end_dt = datetime.datetime.fromisoformat(end_str)
        now = datetime.datetime.now(datetime.timezone.utc)
        diff = (end_dt - now).total_seconds()
        assert 13 * 86400 < diff < 15 * 86400, \
            "Share end time must be ~14 days from now"


# ── handle_get_privacy_masks ──────────────────────────────────────────────────


class TestHandleGetPrivacyMasks:
    @pytest.mark.asyncio
    async def test_empty_masks_shows_keine_message(self):
        """Empty mask list → 'Keine Privacy-Masken' in notification."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[]))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        call_args = hass.services.async_call.call_args
        assert "Keine Privacy-Masken" in call_args[0][2]["message"]

    @pytest.mark.asyncio
    async def test_non_empty_masks_lists_them(self):
        """Non-empty mask list → coordinates appear in notification."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        masks = [{"x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}]
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=masks))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        msg = hass.services.async_call.call_args[0][2]["message"]
        assert "Maske 1" in msg, "Non-empty mask list must enumerate masks"

    @pytest.mark.asyncio
    async def test_http_443_creates_notification_and_raises(self):
        """HTTP 443 → notification + HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(443))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_500_raises_ha_error(self):
        """HTTP 500 → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500, text="err"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_set_privacy_masks ──────────────────────────────────────────────────


class TestHandleSetPrivacyMasks:
    @pytest.mark.asyncio
    async def test_mask_missing_field_raises_validation_error(self):
        """Mask dict missing required key → ServiceValidationError with 'missing_field'."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["set_privacy_masks"]
        call_mock = MagicMock()
        call_mock.data = {
            "camera_id": CAM_ID,
            "masks": [{"x": 0.1, "y": 0.2, "w": 0.3}],  # 'h' missing
        }
        with pytest.raises(ServiceValidationError) as exc_info:
            await handler(call_mock)
        assert exc_info.value.translation_key == "missing_field"

    @pytest.mark.asyncio
    async def test_http_200_refreshes_coordinator(self):
        """HTTP 200 → coordinator refresh."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(200))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "masks": [{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}],
            }
            await handler(call_mock)

        coord.async_request_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_443_raises_privacy_blocked(self):
        """HTTP 443 → privacy_blocked HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(443))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "masks": [{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}],
            }
            with pytest.raises(HomeAssistantError) as exc_info:
                await handler(call_mock)
        assert exc_info.value.translation_key == "privacy_blocked"

    @pytest.mark.asyncio
    async def test_http_error_raises_ha_error(self):
        """HTTP 500 → HomeAssistantError with http_error_with_body."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(500, text="Server Error"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["set_privacy_masks"]
            call_mock = MagicMock()
            call_mock.data = {
                "camera_id": CAM_ID,
                "masks": [{"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}],
            }
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_delete_motion_zone ─────────────────────────────────────────────────


class TestHandleDeleteMotionZone:
    @pytest.mark.asyncio
    async def test_index_out_of_range_raises_validation_error(self):
        """zone_index >= len(zones) → ServiceValidationError."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        zones = [{"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}]  # only 1 zone
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=zones))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_motion_zone"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "zone_index": 5}  # OOB
            with pytest.raises(ServiceValidationError) as exc_info:
                await handler(call_mock)
        assert exc_info.value.translation_key == "index_out_of_range"

    @pytest.mark.asyncio
    async def test_valid_index_fetches_and_reposts(self):
        """Valid index → zone is removed, remaining zones are re-POSTed."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        zone_a = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
        zone_b = {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[zone_a, zone_b]))
        session.post = MagicMock(return_value=_resp_cm(204))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_motion_zone"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "zone_index": 0}
            await handler(call_mock)

        post_json = session.post.call_args[1]["json"]
        assert post_json == [zone_b], \
            "After deleting zone 0, only zone_b must be re-POSTed"
        coord.async_request_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fetch_error_raises_ha_error(self):
        """GET fetch returning non-200 → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["delete_motion_zone"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "zone_index": 0}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_get_lighting_schedule ──────────────────────────────────────────────


class TestHandleGetLightingSchedule:
    @pytest.mark.asyncio
    async def test_cache_hit_skips_api_call(self):
        """Cached lighting options must be used without an API call."""
        from custom_components.bosch_shc_camera import _register_services
        cached = {
            "scheduleStatus": "MANUAL", "generalLightOnTime": "18:00:00",
            "generalLightOffTime": "06:00:00", "darknessThreshold": 5,
            "lightOnMotion": True, "lightOnMotionFollowUpTimeSeconds": 30,
            "frontIlluminatorInGeneralLightOn": True,
            "wallwasherInGeneralLightOn": False,
            "frontIlluminatorGeneralLightIntensity": 0.8,
        }
        entry, coord = _entry_with_coord(_lighting_options_cache={CAM_ID: cached})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_lighting_schedule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        session.get.assert_not_called()
        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_miss_fetches_from_api(self):
        """No cache → GET request made, notification sent."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord(_lighting_options_cache={})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        api_data = {
            "scheduleStatus": "DUSK_TO_DAWN", "generalLightOnTime": "00:00:00",
            "generalLightOffTime": "00:00:00", "darknessThreshold": 3,
            "lightOnMotion": False, "lightOnMotionFollowUpTimeSeconds": 0,
            "frontIlluminatorInGeneralLightOn": False, "wallwasherInGeneralLightOn": False,
            "frontIlluminatorGeneralLightIntensity": 1.0,
        }
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=api_data))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_lighting_schedule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        session.get.assert_called_once()
        hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_miss_http_error_raises(self):
        """GET returns non-200 → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord(_lighting_options_cache={})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(503))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["get_lighting_schedule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

    @pytest.mark.asyncio
    async def test_notification_shows_schedule_details(self):
        """Notification message must contain schedule mode and time fields."""
        from custom_components.bosch_shc_camera import _register_services
        cached = {
            "scheduleStatus": "MANUAL", "generalLightOnTime": "18:30:00",
            "generalLightOffTime": "05:30:00", "darknessThreshold": 7,
            "lightOnMotion": True, "lightOnMotionFollowUpTimeSeconds": 60,
            "frontIlluminatorInGeneralLightOn": True, "wallwasherInGeneralLightOn": True,
            "frontIlluminatorGeneralLightIntensity": 0.5,
        }
        entry, coord = _entry_with_coord(_lighting_options_cache={CAM_ID: cached})
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
            _register_services(hass)
            handler = _get_handlers(hass)["get_lighting_schedule"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID}
            await handler(call_mock)

        msg = hass.services.async_call.call_args[0][2]["message"]
        assert "MANUAL" in msg, "Schedule mode must appear in notification"
        assert "18:30:00" in msg, "Light-on time must appear in notification"


# ── handle_rename_camera ──────────────────────────────────────────────────────


class TestHandleRenameCamera:
    @pytest.mark.asyncio
    async def test_missing_new_name_raises_validation_error(self):
        """empty new_name → ServiceValidationError."""
        from homeassistant.exceptions import ServiceValidationError
        from custom_components.bosch_shc_camera import _register_services
        hass = _make_hass()
        _register_services(hass)
        handler = _get_handlers(hass)["rename_camera"]
        call_mock = MagicMock()
        call_mock.data = {"camera_id": CAM_ID, "new_name": ""}
        with pytest.raises(ServiceValidationError):
            await handler(call_mock)

    @pytest.mark.asyncio
    async def test_http_204_refreshes_coordinator(self):
        """HTTP 204 → coordinator refresh requested."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(204))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["rename_camera"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "new_name": "Front Door"}
            await handler(call_mock)

        coord.async_request_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_http_error_raises_ha_error(self):
        """HTTP 422 → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(422))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["rename_camera"]
            call_mock = MagicMock()
            call_mock.data = {"camera_id": CAM_ID, "new_name": "Front Door"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_invite_friend ──────────────────────────────────────────────────────


class TestHandleInviteFriend:
    @pytest.mark.asyncio
    async def test_http_201_sends_notification_with_friend_id(self):
        """HTTP 201 → persistent notification contains friend ID."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(201, json_data={"id": "friend-xyz"}))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["invite_friend"]
            call_mock = MagicMock()
            call_mock.data = {"email": "friend@example.com"}
            await handler(call_mock)

        hass.services.async_call.assert_awaited_once()
        msg = hass.services.async_call.call_args[0][2]["message"]
        assert "friend-xyz" in msg, "Notification must include the new friend ID"

    @pytest.mark.asyncio
    async def test_http_error_raises_ha_error(self):
        """HTTP 409 (already invited) → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post = MagicMock(return_value=_resp_cm(409, text="Already invited"))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["invite_friend"]
            call_mock = MagicMock()
            call_mock.data = {"email": "friend@example.com"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

    @pytest.mark.asyncio
    async def test_exception_wraps_to_ha_error(self):
        """Network error → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.post.side_effect = OSError("timeout")

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["invite_friend"]
            call_mock = MagicMock()
            call_mock.data = {"email": "friend@example.com"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)


# ── handle_list_friends ───────────────────────────────────────────────────────


class TestHandleListFriends:
    @pytest.mark.asyncio
    async def test_no_friends_shows_keine_message(self):
        """Empty friends list → 'Keine Freunde' in notification."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[]))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["list_friends"]
            await handler(MagicMock())

        msg = hass.services.async_call.call_args[0][2]["message"]
        assert "Keine Freunde" in msg

    @pytest.mark.asyncio
    async def test_friends_listed_in_notification(self):
        """Non-empty friends → email + status + ID appear in notification."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        friends = [{"email": "bob@example.com", "status": "ACCEPTED",
                    "id": "f-001", "sharedVideoInputs": [CAM_ID]}]
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=friends))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["list_friends"]
            await handler(MagicMock())

        msg = hass.services.async_call.call_args[0][2]["message"]
        assert "bob@example.com" in msg, "Friend email must appear in notification"
        assert "ACCEPTED" in msg, "Friend status must appear in notification"
        assert "f-001" in msg, "Friend ID must appear in notification"

    @pytest.mark.asyncio
    async def test_http_error_raises_ha_error(self):
        """HTTP 500 → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["list_friends"]
            with pytest.raises(HomeAssistantError):
                await handler(MagicMock())


# ── handle_remove_friend ──────────────────────────────────────────────────────


class TestHandleRemoveFriend:
    @pytest.mark.asyncio
    async def test_http_204_succeeds(self):
        """HTTP 204 → success, no error."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(return_value=_resp_cm(204))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["remove_friend"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": "f-001"}
            await handler(call_mock)

        session.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_http_404_raises_ha_error(self):
        """HTTP 404 (friend not found) → HomeAssistantError."""
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
            call_mock.data = {"friend_id": "f-999"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

    @pytest.mark.asyncio
    async def test_exception_wraps_to_ha_error(self):
        """Network exception → HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete.side_effect = OSError("network down")

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["remove_friend"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": "f-001"}
            with pytest.raises(HomeAssistantError):
                await handler(call_mock)

    @pytest.mark.asyncio
    async def test_http_200_also_succeeds(self):
        """HTTP 200 is also a valid success response."""
        from custom_components.bosch_shc_camera import _register_services
        entry, coord = _entry_with_coord()
        hass = _make_hass()
        hass.config_entries.async_loaded_entries.return_value = [entry]

        session = MagicMock()
        session.delete = MagicMock(return_value=_resp_cm(200))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            _register_services(hass)
            handler = _get_handlers(hass)["remove_friend"]
            call_mock = MagicMock()
            call_mock.data = {"friend_id": "f-001"}
            await handler(call_mock)  # must not raise
