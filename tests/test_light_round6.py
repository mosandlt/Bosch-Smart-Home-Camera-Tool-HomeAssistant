"""Tests for light.py — BoschFrontLight async_turn_on/off and color_temp_kelvin.

Sprint C coverage target: lines 47-59 (async_setup_entry), 147 (available),
170 (brightness when off), 228 (color_temp_kelvin), 245-246 (preconfigure path),
313-354 (BoschFrontLight.async_turn_on), 358-373 (BoschFrontLight.async_turn_off),
432-483 (BoschTopLedLight/BoschBottomLedLight async_turn_on/off).

Covers: async_setup_entry gating, BoschFrontLight color_temp_kelvin conversion,
preconfigure-while-off behavior, async_turn_on with color_temp / brightness,
async_turn_off, _BoschRgbLedLight turn_on/off.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _stub_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                    "featureSupport": {"light": True},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        _lighting_switch_cache={},
        _shc_state_cache={CAM_ID: {}},
        _light_set_at={},
        last_update_success=True,
        token="tok-A",
        async_update_listeners=MagicMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord():
    return _stub_coord()


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── async_setup_entry gating ──────────────────────────────────────────────────

class TestLightAsyncSetupEntry:
    def test_entities_added_for_gen2_with_light(self):
        """Three light entities (top/bottom/front) must be added for Gen2 Outdoor with light."""
        from custom_components.bosch_shc_camera.light import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = True
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschTopLedLight" in entity_classes, "BoschTopLedLight must be added for Gen2 Outdoor"
        assert "BoschBottomLedLight" in entity_classes, "BoschBottomLedLight must be added for Gen2 Outdoor"
        assert "BoschFrontLight" in entity_classes, "BoschFrontLight must be added for Gen2 Outdoor"

    def test_no_entities_for_gen1_cameras(self):
        """No light entities for Gen1 (non-Gen2) cameras — light.py is Gen2-only."""
        from custom_components.bosch_shc_camera.light import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"  # Gen1
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert added == [], "No light entities must be registered for Gen1 camera"

    def test_no_entities_when_has_light_false(self):
        """No light entities for Gen2 cameras without featureSupport.light."""
        from custom_components.bosch_shc_camera.light import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert added == [], "No light entities when featureSupport.light=False"


# ── BoschFrontLight.color_temp_kelvin ─────────────────────────────────────────

class TestFrontLightColorTempKelvin:
    def _make_front_light(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight
        entity = BoschFrontLight(coord, CAM_ID, entry)
        return entity

    def test_cool_white_balance_gives_high_kelvin(self, stub_coord, stub_entry):
        """whiteBalance=-1.0 (coolest) must map to 6500K."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._white_balance = -1.0
        entity._is_on = True
        entity._brightness = 80
        # Bypass _load_state_from_cache by clearing cache
        stub_coord._lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k == 6500, "whiteBalance=-1.0 must map to 6500K (cool)"

    def test_warm_white_balance_gives_low_kelvin(self, stub_coord, stub_entry):
        """whiteBalance=1.0 (warmest) must map to 2000K."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._white_balance = 1.0
        entity._is_on = True
        entity._brightness = 80
        stub_coord._lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k == 2000, "whiteBalance=1.0 must map to 2000K (warm)"

    def test_neutral_white_balance_gives_mid_kelvin(self, stub_coord, stub_entry):
        """whiteBalance=0.0 must map to 4250K (midpoint)."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._white_balance = 0.0
        entity._is_on = True
        entity._brightness = 80
        stub_coord._lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k == 4250, "whiteBalance=0.0 must map to 4250K (neutral)"

    def test_returns_value_when_off_for_ui_slider(self, stub_coord, stub_entry):
        """Must return a non-None Kelvin value even when light is off (UI slider position)."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = False
        entity._white_balance = None
        entity._last_white_balance = -1.0
        stub_coord._lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k is not None, "color_temp_kelvin must return a value even when off"


# ── BoschFrontLight.async_turn_on ─────────────────────────────────────────────

class TestFrontLightTurnOn:
    def _make_front_light(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight
        entity = BoschFrontLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        return entity

    @pytest.mark.asyncio
    async def test_turn_on_without_kwargs_uses_last_brightness(self, stub_coord, stub_entry):
        """Turn ON with no kwargs must use remembered brightness (not 0)."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = False
        entity._last_brightness = 75
        entity._white_balance = -0.5
        await entity.async_turn_on()
        call_args = entity._put_lighting_switch.call_args[0][0]
        assert call_args["frontLightSettings"]["brightness"] == 75, \
            "Turn ON must restore last brightness (75) when no explicit brightness given"

    @pytest.mark.asyncio
    async def test_turn_on_with_color_temp_stores_wb(self, stub_coord, stub_entry):
        """Turn ON with ATTR_COLOR_TEMP_KELVIN must convert to whiteBalance and store it."""
        from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 6500})
        # 6500K → wb = (4250-6500)/2250 = -1.0
        assert entity._white_balance == -1.0, "6500K must map to whiteBalance=-1.0"
        assert entity._last_white_balance == -1.0, "last_white_balance must also be updated"

    @pytest.mark.asyncio
    async def test_turn_on_while_off_with_brightness_only_preconfigures(self, stub_coord, stub_entry):
        """When light is off and only brightness is given: store locally, don't PUT API."""
        from homeassistant.components.light import ATTR_BRIGHTNESS
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = False
        await entity.async_turn_on(**{ATTR_BRIGHTNESS: 128})
        entity._put_lighting_switch.assert_not_called()
        assert entity._last_brightness == 50, \
            "128/255*100 = 50% must be stored as last_brightness in preconfigure mode"

    @pytest.mark.asyncio
    async def test_turn_on_sends_put_and_enables_front_switch(self, stub_coord, stub_entry):
        """Turn ON from on-state must PUT lighting/switch and enable front switch endpoint."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = True
        entity._last_brightness = 60
        await entity.async_turn_on()
        entity._put_lighting_switch.assert_called_once()
        entity._put_switch_endpoint.assert_called_once_with("front", True)

    @pytest.mark.asyncio
    async def test_turn_on_sets_is_on_true(self, stub_coord, stub_entry):
        """Entity must be is_on=True after turn_on."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on()
        assert entity._is_on is True, "Entity must be on after turn_on"


# ── BoschFrontLight.async_turn_off ────────────────────────────────────────────

class TestFrontLightTurnOff:
    def _make_front_light(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight
        entity = BoschFrontLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        return entity

    @pytest.mark.asyncio
    async def test_turn_off_sets_is_on_false(self, stub_coord, stub_entry):
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = True
        entity._brightness = 80
        await entity.async_turn_off()
        assert entity._is_on is False, "Entity must be off after turn_off"
        assert entity._brightness == 0, "Brightness must be 0 after turn_off"

    @pytest.mark.asyncio
    async def test_turn_off_sends_brightness_zero_and_disables_endpoint(self, stub_coord, stub_entry):
        """Turn OFF must PUT brightness=0 AND disable the front switch endpoint."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = True
        entity._brightness = 80
        entity._white_balance = -0.5
        await entity.async_turn_off()
        put_call = entity._put_lighting_switch.call_args[0][0]
        assert put_call["frontLightSettings"]["brightness"] == 0, \
            "Must send brightness=0 to keep cache consistent with camera state"
        entity._put_switch_endpoint.assert_called_once_with("front", False)

    @pytest.mark.asyncio
    async def test_turn_off_preserves_white_balance_in_put(self, stub_coord, stub_entry):
        """Turn OFF must preserve whiteBalance in PUT body so subsequent top/bottom PUTs don't accidentally re-enable front."""
        entity = self._make_front_light(stub_coord, stub_entry)
        entity._is_on = True
        entity._brightness = 80
        entity._white_balance = 0.5
        await entity.async_turn_off()
        put_call = entity._put_lighting_switch.call_args[0][0]
        assert put_call["frontLightSettings"]["whiteBalance"] == 0.5, \
            "whiteBalance must be preserved in turn_off PUT (prevents accidental re-enable)"


# ── BoschTopLedLight.async_turn_on / async_turn_off ──────────────────────────

class TestTopLedLightTurnOn:
    def _make_top_led(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        entity = BoschTopLedLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        entity._sync_wallwasher_cache = MagicMock()
        return entity

    @pytest.mark.asyncio
    async def test_turn_on_with_rgb_sends_color_hex(self, stub_coord, stub_entry):
        """Turn ON with ATTR_RGB_COLOR must convert to #RRGGBB and include in PUT body."""
        from homeassistant.components.light import ATTR_RGB_COLOR
        entity = self._make_top_led(stub_coord, stub_entry)
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on(**{ATTR_RGB_COLOR: (255, 0, 128)})
        call_args = entity._put_lighting_switch.call_args[0][0]
        assert call_args["topLedLightSettings"]["color"] == "#FF0080", \
            "RGB (255,0,128) must be sent as #FF0080"
        assert call_args["topLedLightSettings"]["whiteBalance"] is None, \
            "whiteBalance must be None when color is set (API requires mutual exclusion)"

    @pytest.mark.asyncio
    async def test_turn_on_enables_topdown_endpoint(self, stub_coord, stub_entry):
        """Turn ON must also enable the topdown lighting endpoint (ambient mode)."""
        entity = self._make_top_led(stub_coord, stub_entry)
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on()
        entity._put_switch_endpoint.assert_called_with("topdown", True)

    @pytest.mark.asyncio
    async def test_preconfigure_while_off_with_rgb(self, stub_coord, stub_entry):
        """Color change while off must store color locally without calling API."""
        from homeassistant.components.light import ATTR_RGB_COLOR
        entity = self._make_top_led(stub_coord, stub_entry)
        entity._is_on = False
        await entity.async_turn_on(**{ATTR_RGB_COLOR: (0, 255, 0)})
        entity._put_lighting_switch.assert_not_called()
        assert entity._last_color_hex == "#00FF00", \
            "RGB color must be stored as last_color_hex in preconfigure mode"

    @pytest.mark.asyncio
    async def test_turn_off_sends_brightness_zero(self, stub_coord, stub_entry):
        entity = self._make_top_led(stub_coord, stub_entry)
        entity._is_on = True
        entity._brightness = 70
        stub_coord._lighting_switch_cache[CAM_ID] = {
            "topLedLightSettings": {"brightness": 0},
            "bottomLedLightSettings": {"brightness": 0},
        }
        await entity.async_turn_off()
        put_call = entity._put_lighting_switch.call_args[0][0]
        assert put_call["topLedLightSettings"]["brightness"] == 0, \
            "Turn off must send brightness=0 for topLedLightSettings"
        assert entity._is_on is False, "Entity must be off after turn_off"

    @pytest.mark.asyncio
    async def test_turn_off_disables_topdown_when_both_leds_off(self, stub_coord, stub_entry):
        """Topdown endpoint must be disabled when both Top and Bottom brightness reach 0."""
        entity = self._make_top_led(stub_coord, stub_entry)
        entity._is_on = True
        entity._brightness = 70
        # Both LEDs at 0 after PUT
        stub_coord._lighting_switch_cache[CAM_ID] = {
            "topLedLightSettings": {"brightness": 0},
            "bottomLedLightSettings": {"brightness": 0},
        }
        await entity.async_turn_off()
        entity._put_switch_endpoint.assert_called_with("topdown", False)


# ── _BoschLightBase.brightness property (off-state) ──────────────────────────

class TestLightBaseAvailableAndBrightness:
    def test_available_requires_only_coordinator_success(self, stub_coord, stub_entry):
        """Light entities must be available when coordinator succeeded (no camera-online gate)."""
        from custom_components.bosch_shc_camera.light import BoschFrontLight
        entity = BoschFrontLight(stub_coord, CAM_ID, stub_entry)
        stub_coord.last_update_success = True
        assert entity.available is True, "Light entity must be available when coordinator succeeded"

    def test_brightness_returns_last_brightness_when_off(self, stub_coord, stub_entry):
        """brightness property must return last_brightness (scaled to 0-255) when light is off."""
        from custom_components.bosch_shc_camera.light import BoschFrontLight
        entity = BoschFrontLight(stub_coord, CAM_ID, stub_entry)
        entity._is_on = False
        entity._brightness = 0
        entity._last_brightness = 80
        stub_coord._lighting_switch_cache = {}  # ensure _load_state_from_cache is a no-op
        b = entity.brightness
        assert b == round(80 * 255 / 100), \
            "Brightness when off must return last_brightness scaled to HA 0-255"
