"""Tests for the new privacy-guard `return` branches added on 2026-05-28.

Covers the four sites where `_warn_if_privacy_on` was added:
  1. light.py  _BoschRgbLedLight.async_turn_on   (line 370 — return when blocked)
  2. light.py  BoschFrontLight.async_turn_on      (line 502 — return when blocked)
  3. switch.py BoschPanicAlarmSwitch._set         (line 1793 — return when blocked)
                                                   (line 1804 — warning on failed PUT)
  4. number.py _BoschAlarmDelayBase.async_set_native_value (line 762 — return when blocked)

Strategy: patch `_warn_if_privacy_on` directly at import site so we can force
True (blocked) or False (pass-through) without needing a real coordinator cache.
For the panic-alarm failed-PUT warning (line 1804): use `async_put_camera=False`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "00000000-0000-0000-0000-000000000001"


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _stub_coord_with_privacy(privacy_on: bool = False, hw: str = "HOME_Eyes_Indoor"):
    """Coordinator stub that _warn_if_privacy_on can actually interrogate."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                },
            }
        },
        _shc_state_cache={CAM_ID: {"privacy_mode": privacy_on}},
        _panic_alarm_cache={},
        _alarm_settings_cache={},
        _lighting_switch_cache={},
        _light_set_at={},
        last_update_success=True,
        token="tok-A",
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
        async_update_listeners=MagicMock(),
    )


def _hass_stub():
    """Minimal hass stub with services.async_call that won't crash."""
    svc = SimpleNamespace(async_call=AsyncMock())
    return SimpleNamespace(services=svc)


# ─────────────────────────────────────────────────────────────────────────────
# 1. light.py — _BoschRgbLedLight.async_turn_on  (BoschTopLedLight)
# ─────────────────────────────────────────────────────────────────────────────


class TestRgbLedLightPrivacyGuard:
    """When privacy is ON the turn_on must abort and NOT call the API."""

    def _make_top_led(self, coord):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entry = SimpleNamespace(data={}, options={})
        entity = BoschTopLedLight.__new__(BoschTopLedLight)
        entity.coordinator = coord
        entity._cam_id = CAM_ID
        entity._entry = entry
        entity._cam_title = "Innenbereich"
        entity._model = "HOME_Eyes_Indoor"
        entity._model_name = "Eyes Indoor"
        entity._fw = "9.40.25"
        entity._mac = "aa:bb:cc:dd:ee:02"
        entity._brightness = 0
        entity._last_brightness = 80
        entity._color_hex = None
        entity._last_color_hex = None
        entity._white_balance = None
        entity._last_white_balance = -1.0
        entity._is_on = True
        entity._led_key = "topLedLightSettings"
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        entity._sync_wallwasher_cache = MagicMock()
        entity.hass = _hass_stub()
        return entity

    @pytest.mark.asyncio
    async def test_turn_on_blocked_when_privacy_on(self):
        """When _warn_if_privacy_on returns True the API PUT must NOT be called."""
        coord = _stub_coord_with_privacy(privacy_on=True)
        entity = self._make_top_led(coord)

        with patch(
            "custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
            new=AsyncMock(return_value=True),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_not_called()
        entity._put_switch_endpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_proceeds_when_privacy_off(self):
        """When _warn_if_privacy_on returns False the turn_on must proceed normally."""
        coord = _stub_coord_with_privacy(privacy_on=False)
        entity = self._make_top_led(coord)

        with patch(
            "custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
            new=AsyncMock(return_value=False),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 2. light.py — BoschFrontLight.async_turn_on
# ─────────────────────────────────────────────────────────────────────────────


class TestFrontLightPrivacyGuard:
    """When privacy is ON BoschFrontLight.async_turn_on must abort early."""

    def _make_front_light(self, coord):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entry = SimpleNamespace(data={}, options={})
        entity = BoschFrontLight.__new__(BoschFrontLight)
        entity.coordinator = coord
        entity._cam_id = CAM_ID
        entity._entry = entry
        entity._cam_title = "Innenbereich"
        entity._model = "HOME_Eyes_Indoor"
        entity._model_name = "Eyes Indoor"
        entity._fw = "9.40.25"
        entity._mac = "aa:bb:cc:dd:ee:02"
        entity._brightness = 0
        entity._last_brightness = 80
        entity._color_hex = None
        entity._last_color_hex = None
        entity._white_balance = -1.0
        entity._last_white_balance = -1.0
        entity._is_on = True
        entity._led_key = "frontLightSettings"
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        entity.hass = _hass_stub()
        return entity

    @pytest.mark.asyncio
    async def test_front_light_blocked_when_privacy_on(self):
        """When _warn_if_privacy_on returns True the API PUT must NOT be called."""
        coord = _stub_coord_with_privacy(privacy_on=True)
        entity = self._make_front_light(coord)

        with patch(
            "custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
            new=AsyncMock(return_value=True),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_not_called()
        entity._put_switch_endpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_front_light_proceeds_when_privacy_off(self):
        """When _warn_if_privacy_on returns False the turn_on must proceed."""
        coord = _stub_coord_with_privacy(privacy_on=False)
        entity = self._make_front_light(coord)

        with patch(
            "custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
            new=AsyncMock(return_value=False),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 3. switch.py — BoschPanicAlarmSwitch._set
# ─────────────────────────────────────────────────────────────────────────────


class TestPanicAlarmPrivacyGuardAndFailedPut:
    """Covers lines 1793 (return when blocked) and 1804 (warning on failed PUT)."""

    def _make_switch(self, coord):
        from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        entity = BoschPanicAlarmSwitch(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity.hass = _hass_stub()
        return entity

    @pytest.mark.asyncio
    async def test_set_blocked_when_privacy_on(self):
        """_set(True) with privacy ON must not call async_put_camera."""
        coord = _stub_coord_with_privacy(privacy_on=True)
        entity = self._make_switch(coord)

        # Confirm not called
        await entity._set(True)

        # privacy guard should have fired — async_put_camera not called
        coord.async_put_camera.assert_not_called()
        # cache also not updated
        assert coord._panic_alarm_cache.get(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_set_false_skips_privacy_guard(self):
        """_set(False) must skip the privacy guard (guard only fires for `enabled=True`)."""
        coord = _stub_coord_with_privacy(privacy_on=True)
        entity = self._make_switch(coord)

        await entity._set(False)

        # async_put_camera called (no privacy guard for turn-off)
        coord.async_put_camera.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_put_logs_warning_and_does_not_set_cache(self):
        """When async_put_camera returns False a warning is logged (line 1804)
        and the cache must NOT be set to the new state."""
        coord = _stub_coord_with_privacy(privacy_on=False)
        coord.async_put_camera = AsyncMock(return_value=False)
        entity = self._make_switch(coord)

        await entity._set(True)

        # PUT was called
        coord.async_put_camera.assert_called_once()
        # Cache must NOT be True (PUT failed)
        assert coord._panic_alarm_cache.get(CAM_ID) is not True

    @pytest.mark.asyncio
    async def test_successful_put_sets_cache(self):
        """When async_put_camera returns True the cache is updated."""
        coord = _stub_coord_with_privacy(privacy_on=False)
        entity = self._make_switch(coord)

        await entity._set(True)

        assert coord._panic_alarm_cache[CAM_ID] is True


# ─────────────────────────────────────────────────────────────────────────────
# 4. number.py — _BoschAlarmDelayBase.async_set_native_value
# ─────────────────────────────────────────────────────────────────────────────


class TestAlarmDelayPrivacyGuard:
    """Line 762: when camera is Gen2 Indoor AND privacy is ON the setter must abort."""

    def _make_entity(self, coord, klass_name="BoschAlarmDelayNumber"):
        import importlib

        mod = importlib.import_module("custom_components.bosch_shc_camera.number")
        klass = getattr(mod, klass_name)
        entry = SimpleNamespace(data={}, options={})
        entity = klass.__new__(klass)
        entity.coordinator = coord
        entity._cam_id = CAM_ID
        entity._entry = entry
        entity._cam_title = "Innenbereich"
        entity._model = "HOME_Eyes_Indoor"
        entity._model_name = "Eyes Indoor"
        entity._fw = "9.40.25"
        entity._mac = "aa:bb:cc:dd:ee:02"
        entity._field = "alarmDelayInSeconds"
        # _settings is a property reading from coordinator._alarm_settings_cache
        coord._alarm_settings_cache[CAM_ID] = {
            "alarmDelayInSeconds": 10,
            "sirenDurationInSeconds": 30,
        }
        entity.async_write_ha_state = MagicMock()
        entity.hass = _hass_stub()
        return entity

    @pytest.mark.asyncio
    async def test_set_value_blocked_for_gen2_indoor_with_privacy_on(self):
        """Gen2 Indoor + privacy ON → async_put_camera must NOT be called."""
        coord = _stub_coord_with_privacy(privacy_on=True, hw="HOME_Eyes_Indoor")
        entity = self._make_entity(coord)
        # Record original cache value
        original_delay = coord._alarm_settings_cache[CAM_ID]["alarmDelayInSeconds"]

        await entity.async_set_native_value(15.0)

        coord.async_put_camera.assert_not_called()
        # Cache not updated — value must still be the original (10), not the new value (15)
        assert (
            coord._alarm_settings_cache[CAM_ID]["alarmDelayInSeconds"] == original_delay
        )

    @pytest.mark.asyncio
    async def test_set_value_proceeds_for_gen2_indoor_with_privacy_off(self):
        """Gen2 Indoor + privacy OFF → async_put_camera IS called."""
        coord = _stub_coord_with_privacy(privacy_on=False, hw="HOME_Eyes_Indoor")
        entity = self._make_entity(coord)

        await entity.async_set_native_value(15.0)

        coord.async_put_camera.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_value_proceeds_for_gen2_outdoor_regardless_of_privacy(self):
        """Gen2 Outdoor is NOT in _GEN2_INDOOR_HW → guard does NOT fire even
        when the _shc_state_cache says privacy_mode=True."""
        coord = _stub_coord_with_privacy(privacy_on=True, hw="HOME_Eyes_Outdoor")
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        entity = self._make_entity(coord)

        await entity.async_set_native_value(15.0)

        # No privacy guard for Outdoor cameras — PUT proceeds
        coord.async_put_camera.assert_called_once()
