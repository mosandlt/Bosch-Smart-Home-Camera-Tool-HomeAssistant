"""Bosch Smart Home Camera — Light Platform (Gen2 only).

Creates native HA light entities for Gen2 cameras (Eyes Außenkamera II):
  - Top LED Light   — RGB color + brightness (oberes Licht, "tausende Farben")
  - Bottom LED Light — RGB color + brightness (unteres Licht, "tausende Farben")
  - Front Light     — color temperature + brightness (Frontlicht, kaltweiß↔warmweiß)

Gen2 lighting API: PUT /v11/video_inputs/{id}/lighting/switch
Each light group uses EITHER color (HEX #RRGGBB) OR whiteBalance (-1.0 to 1.0), never both.
When color is set, whiteBalance becomes null (color mode).
When whiteBalance is set, color becomes null (temperature mode).

Gen1 cameras use a different API (lighting_override) and are handled by switch.py instead.
"""

import asyncio
import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, CLOUD_API

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = []
    for cam_id in coordinator.data:
        cam_info = coordinator.data[cam_id].get("info", {})
        hw = cam_info.get("hardwareVersion", "CAMERA")
        from .models import get_model_config
        if get_model_config(hw).generation >= 2:
            has_light = cam_info.get("featureSupport", {}).get("light", False)
            if has_light:
                entities.append(BoschTopLedLight(coordinator, cam_id, config_entry))
                entities.append(BoschBottomLedLight(coordinator, cam_id, config_entry))
                entities.append(BoschFrontLight(coordinator, cam_id, config_entry))
    async_add_entities(entities, update_before_add=False)


class _BoschLightBase(CoordinatorEntity, LightEntity):
    """Base class for Gen2 light entities."""

    _led_key: str = ""  # "frontLightSettings", "topLedLightSettings", "bottomLedLightSettings"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry = entry
        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw = info.get("firmwareVersion", "")
        self._mac = info.get("macAddress", "")

        # Local state cache
        self._brightness: int = 0
        self._last_brightness: int = 100  # remember last non-zero brightness for restore on turn_on
        self._color_hex: str | None = None
        self._last_color_hex: str | None = None  # remember last color for restore
        self._white_balance: float | None = None
        self._last_white_balance: float | None = 0.0
        self._is_on: bool = False
        self._state_loaded: bool = False  # True after first load from cache

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model": self._model_name,
            "sw_version": self._fw,
            "connections": {("mac", self._mac)} if self._mac else set(),
        }

    @property
    def is_on(self) -> bool:
        self._load_state_from_cache()
        return self._is_on

    @property
    def brightness(self) -> int | None:
        """HA brightness is 0-255, API brightness is 0-100."""
        self._load_state_from_cache()
        return int(self._brightness * 255 / 100) if self._brightness else 0

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def _load_state_from_cache(self) -> None:
        """Load initial state from coordinator lighting/switch cache."""
        if self._state_loaded:
            return
        lsc = self.coordinator._lighting_switch_cache.get(self._cam_id, {})
        if not lsc:
            return
        led = lsc.get(self._led_key, {})
        bri = led.get("brightness", 0)
        color = led.get("color")
        wb = led.get("whiteBalance")
        self._brightness = bri
        self._is_on = bri > 0
        if bri > 0:
            self._last_brightness = bri
        if color:
            self._color_hex = color
            self._last_color_hex = color
            self._white_balance = None
        elif wb is not None:
            self._white_balance = wb
            self._last_white_balance = wb
            self._color_hex = None
        self._state_loaded = True

    def _get_current_state(self) -> dict:
        """Get the current lighting/switch state from coordinator cache."""
        cached = self.coordinator._lighting_switch_cache.get(self._cam_id, {})
        # Default fallback if cache is empty
        return {
            "frontLightSettings": cached.get("frontLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
            "topLedLightSettings": cached.get("topLedLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
            "bottomLedLightSettings": cached.get("bottomLedLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
        }

    async def _put_lighting_switch(self, updates: dict) -> bool:
        """Send PUT /lighting/switch — ALWAYS sends full body with all 3 groups.

        The Bosch API requires all 3 light groups in every PUT request.
        `updates` contains only the keys to change; the rest is read from cache.
        """
        token = self.coordinator.token
        if not token:
            return False
        # Build full body: start with current state, then apply updates
        body = self._get_current_state()
        for key, val in updates.items():
            if key in body:
                body[key] = {**body[key], **val}  # merge, not replace
            else:
                body[key] = val
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/lighting/switch",
                    headers=headers, json=body,
                ) as resp:
                    if resp.status in (200, 201, 204):
                        # Update cache with response
                        try:
                            rsp = await resp.json()
                            self.coordinator._lighting_switch_cache[self._cam_id] = rsp
                        except Exception:
                            pass
                        return True
                    _LOGGER.warning("lighting/switch HTTP %d for %s", resp.status, self._cam_id[:8])
        except Exception as err:
            _LOGGER.warning("lighting/switch error for %s: %s", self._cam_id[:8], err)
        return False

    async def _put_switch_endpoint(self, endpoint: str, enabled: bool) -> bool:
        """Send PUT /lighting/switch/front or /topdown."""
        token = self.coordinator.token
        if not token:
            return False
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/lighting/switch/{endpoint}",
                    headers=headers, json={"enabled": enabled},
                ) as resp:
                    return resp.status in (200, 201, 204)
        except Exception as err:
            _LOGGER.warning("lighting/switch/%s error: %s", endpoint, err)
        return False


# ─────────────────────────────────────────────────────────────────────────────
class _BoschRgbLedLight(_BoschLightBase):
    """Base for Top/Bottom LED light — RGB color + brightness.

    Remembers last brightness and color for restore on turn_on.
    """

    _led_key = ""
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        self._load_state_from_cache()
        color = self._color_hex or self._last_color_hex
        if color:
            h = color.lstrip("#")
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._load_state_from_cache()
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        rgb = kwargs.get(ATTR_RGB_COLOR)

        # Restore last brightness if not specified
        api_brightness = int(brightness * 100 / 255) if brightness else (self._last_brightness or 100)

        if rgb:
            color_hex = f"#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}"
            self._color_hex = color_hex
            self._last_color_hex = color_hex
            self._white_balance = None
        else:
            # Restore last color
            color_hex = self._color_hex or self._last_color_hex

        if color_hex:
            body = {self._led_key: {"brightness": api_brightness, "color": color_hex, "whiteBalance": None}}
        else:
            body = {self._led_key: {"brightness": api_brightness, "color": None, "whiteBalance": 0.0}}

        self._brightness = api_brightness
        self._last_brightness = api_brightness
        self._is_on = True

        if await self._put_lighting_switch(body):
            await self._put_switch_endpoint("topdown", True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Remember current settings before turning off
        if self._brightness > 0:
            self._last_brightness = self._brightness
        if self._color_hex:
            self._last_color_hex = self._color_hex
        body = {self._led_key: {"brightness": 0}}
        self._is_on = False
        self._brightness = 0
        await self._put_lighting_switch(body)
        self.async_write_ha_state()


class BoschTopLedLight(_BoschRgbLedLight):
    """Light entity: Top LED (oberes Licht) — RGB color + brightness."""

    _led_key = "topLedLightSettings"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name = f"Bosch {self._cam_title} Oberes Licht"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_top_led_light"
        self._attr_icon = "mdi:arrow-up-bold-circle"


# ─────────────────────────────────────────────────────────────────────────────
class BoschBottomLedLight(_BoschRgbLedLight):
    """Light entity: Bottom LED (unteres Licht) — RGB color + brightness."""

    _led_key = "bottomLedLightSettings"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name = f"Bosch {self._cam_title} Unteres Licht"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_bottom_led_light"
        self._attr_icon = "mdi:arrow-down-bold-circle"


# ─────────────────────────────────────────────────────────────────────────────
class BoschFrontLight(_BoschLightBase):
    """Light entity: Front spotlight — color temperature + brightness.

    Front light only supports white with color temperature (whiteBalance -1.0 to 1.0),
    NOT RGB colors. -1.0 = cool/blue, 0.0 = neutral, 1.0 = warm/orange.
    Mapped to HA color temp: 2000K (warm) to 6500K (cool).
    """

    _led_key = "frontLightSettings"
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = 2000
    _attr_max_color_temp_kelvin = 6500

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name = f"Bosch {self._cam_title} Frontlicht"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_front_light_entity"
        self._attr_icon = "mdi:spotlight-beam"
        self._white_balance = 0.0

    @property
    def color_temp_kelvin(self) -> int | None:
        """Convert whiteBalance (-1.0 to 1.0) to Kelvin (6500 to 2000)."""
        if self._white_balance is None:
            return 4250  # neutral
        # -1.0 (cool) = 6500K, 1.0 (warm) = 2000K
        return int(4250 - self._white_balance * 2250)

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        color_temp_k = kwargs.get(ATTR_COLOR_TEMP_KELVIN)

        api_brightness = int(brightness * 100 / 255) if brightness else (self._brightness or 100)

        if color_temp_k:
            # Convert Kelvin to whiteBalance: 6500K = -1.0, 2000K = 1.0
            wb = round((4250 - color_temp_k) / 2250, 2)
            wb = max(-1.0, min(1.0, wb))
            self._white_balance = wb
        else:
            wb = self._white_balance if self._white_balance is not None else 0.0

        body = {self._led_key: {"brightness": api_brightness, "color": None, "whiteBalance": wb}}
        self._brightness = api_brightness
        self._is_on = True

        if await self._put_lighting_switch(body):
            await self._put_switch_endpoint("front", True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        self._brightness = 0
        await self._put_switch_endpoint("front", False)
        self.async_write_ha_state()
