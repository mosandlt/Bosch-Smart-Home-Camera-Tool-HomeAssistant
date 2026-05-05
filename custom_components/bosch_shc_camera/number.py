"""Bosch Smart Home Camera — Number Platform.

Creates number entities per camera:
  • {Name} Pan Position     — pan the 360 camera left/right (-120° to +120°).
    Only available for cameras with featureSupport.panLimit > 0 (CAMERA_360).
    Uses cloud API: PUT /v11/video_inputs/{id}/pan
    State is read from GET /v11/video_inputs/{id}/pan (polled each coordinator tick).

  • {Name} Audio Threshold  — audio alarm trigger threshold in dB (0–100).
    Available for all cameras.
    Reads from coordinator.audio_alarm_settings(cam_id)["threshold"].
    Writes via PUT /v11/video_inputs/{id}/audioAlarm {"threshold": value, "enabled": true}.
    Disabled by default.
"""

import asyncio
import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, CLOUD_API

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = []
    for cam_id in coordinator.data:
        cam_info = coordinator.data[cam_id].get("info", {})
        pan_limit = cam_info.get("featureSupport", {}).get("panLimit", 0)
        if pan_limit:
            entities.append(BoschPanNumber(coordinator, cam_id, config_entry, pan_limit))
        entities.append(BoschAudioThresholdNumber(coordinator, cam_id, config_entry))
        entities.append(BoschSpeakerLevelNumber(coordinator, cam_id, config_entry))
        has_light = cam_info.get("featureSupport", {}).get("light", False)
        if has_light:
            entities.append(BoschFrontLightIntensityNumber(coordinator, cam_id, config_entry))
        # Gen2-only entities
        from .models import get_model_config
        hw = cam_info.get("hardwareVersion", "CAMERA")
        if get_model_config(hw).generation >= 2:
            # lens_elevation works on both Indoor II and Outdoor II
            # (Indoor II slow-tier returns 200 on this endpoint, confirmed 2026-04-11)
            entities.append(BoschLensElevationNumber(coordinator, cam_id, config_entry))
            entities.append(BoschMicrophoneLevelNumber(coordinator, cam_id, config_entry))
            # Light-related entities only for cameras that actually expose Gen2 lighting
            # (Indoor II has no RGB/wallwasher lights — only Power-LED via iconLedBrightness).
            if hw not in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
                entities.append(BoschWhiteBalanceNumber(coordinator, cam_id, config_entry))
                entities.append(BoschTopLedBrightnessNumber(coordinator, cam_id, config_entry))
                entities.append(BoschBottomLedBrightnessNumber(coordinator, cam_id, config_entry))
                entities.append(BoschMotionLightSensitivityNumber(coordinator, cam_id, config_entry))
                entities.append(BoschDarknessThresholdNumber(coordinator, cam_id, config_entry))
        # Gen2 Indoor II — alarm delays + power-LED brightness + audio alarm sensitivity
        if hw in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
            entities.append(BoschPowerLedBrightnessNumber(coordinator, cam_id, config_entry))
            entities.append(BoschAlarmDelayNumber(coordinator, cam_id, config_entry))
            entities.append(BoschAlarmActivationDelayNumber(coordinator, cam_id, config_entry))
            entities.append(BoschPreAlarmDelayNumber(coordinator, cam_id, config_entry))
            entities.append(BoschAudioAlarmSensitivityNumber(coordinator, cam_id, config_entry))
    async_add_entities(entities, update_before_add=False)


class BoschPanNumber(CoordinatorEntity, NumberEntity):
    """Number entity to control the pan position of the 360 camera."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry, pan_limit: int) -> None:
        super().__init__(coordinator)
        self._cam_id    = cam_id
        self._entry     = entry
        self._pan_limit = pan_limit

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name             = f"Bosch {self._cam_title} Pan Position"
        self._attr_unique_id        = f"bosch_shc_pan_{cam_id.lower()}"
        self._attr_native_min_value = -pan_limit
        self._attr_native_max_value =  pan_limit
        self._attr_native_step      = 1
        self._attr_mode             = NumberMode.SLIDER
        self._attr_native_unit_of_measurement = "°"
        self._attr_icon             = "mdi:pan-horizontal"
        self._attr_translation_key  = "pan_position"
        self._attr_entity_category  = EntityCategory.CONFIG

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model_name,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }

    def _rotation_180(self) -> bool:
        """Return True if the camera is configured as ceiling-mounted (image
        rotated 180°). When True, the slider sign is inverted so that "right"
        on the slider stays "right" on the user's screen.
        """
        return bool(
            getattr(self.coordinator, "_image_rotation_180", {}).get(self._cam_id)
        )

    @property
    def native_value(self) -> float | None:
        raw = self.coordinator._pan_cache.get(self._cam_id)
        if raw is None:
            return None
        return -raw if self._rotation_180() else raw

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._pan_cache.get(self._cam_id) is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        # Invert sign when the camera is ceiling-mounted so the user-visible
        # direction matches the camera-physical pan direction.
        actual = -int(value) if self._rotation_180() else int(value)
        await self.coordinator.async_cloud_set_pan(self._cam_id, actual)


# ─────────────────────────────────────────────────────────────────────────────
class BoschAudioThresholdNumber(CoordinatorEntity, NumberEntity):
    """Number entity to set the audio alarm trigger threshold (dB).

    Range: 0–100 dB, step 1.
    Reads from coordinator.audio_alarm_settings(cam_id)["threshold"].
    Writes via PUT /v11/video_inputs/{id}/audioAlarm {"threshold": value, "enabled": true}.
    Disabled by default — enable in Settings → Entities.
    """

    _attr_icon                        = "mdi:volume-high"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 100
    _attr_native_step                 = 1
    _attr_mode                        = NumberMode.BOX
    _attr_native_unit_of_measurement  = "dB"
    _attr_has_entity_name             = True
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Audio Threshold"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_audio_threshold"
        self._attr_translation_key = "audio_threshold"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model_name,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }

    @property
    def native_value(self) -> float | None:
        """Return the current audio alarm threshold in dB."""
        settings = self.coordinator.audio_alarm_settings(self._cam_id)
        val = settings.get("threshold")
        if val is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available only when audio alarm settings have been fetched (slow tier)."""
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator.audio_alarm_settings(self._cam_id))
        )

    async def async_set_native_value(self, value: float) -> None:
        """Write the new threshold to the camera via cloud API.

        Preserves sensitivity + audioAlarmConfiguration fields — the Bosch app
        sends the full body (capture 2026-04-11):
            {"sensitivity":0,"threshold":72,"enabled":true,"audioAlarmConfiguration":"CUSTOM"}
        """
        from .switch import _is_gen2_indoor, _warn_if_privacy_on
        if _is_gen2_indoor(self) and await _warn_if_privacy_on(self, "Audio-Schwellenwert"):
            return
        threshold = int(round(value))
        current   = dict(self.coordinator.audio_alarm_settings(self._cam_id) or {})
        current["threshold"] = threshold
        current.setdefault("enabled", True)
        current.setdefault("sensitivity", 0)
        current.setdefault("audioAlarmConfiguration", "CUSTOM")
        success = await self.coordinator.async_put_camera(
            self._cam_id, "audioAlarm", current
        )
        if success:
            # Optimistically update coordinator data so UI reflects immediately
            if self._cam_id in self.coordinator.data:
                self.coordinator.data[self._cam_id]["audioAlarm"] = current
            _LOGGER.debug("Audio threshold set to %d dB for %s", threshold, self._cam_id)
        else:
            _LOGGER.warning("Failed to set audio threshold for %s", self._cam_id)
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschSpeakerLevelNumber(CoordinatorEntity, NumberEntity):
    """Number entity to control the intercom speaker volume (0–100).

    Writes via PUT /v11/video_inputs/{id}/audio {"SpeakerLevel": value}.
    Disabled by default — enable in Settings -> Entities.
    """

    _attr_icon                        = "mdi:volume-medium"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 100
    _attr_native_step                 = 1
    _attr_mode                        = NumberMode.SLIDER
    _attr_native_unit_of_measurement  = "%"
    _attr_has_entity_name             = True
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry
        self._current_level: float = 50  # default speaker level

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Speaker Level"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_speaker_level"
        self._attr_translation_key = "speaker_level"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model_name,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }

    @property
    def native_value(self) -> float:
        return self._current_level

    async def async_set_native_value(self, value: float) -> None:
        """Write the new speaker level to the camera via cloud API."""
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        level = int(round(value))
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {self.coordinator.token}",
            "Content-Type": "application/json",
        }
        body = {"SpeakerLevel": level}
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/audio",
                    headers=headers,
                    json=body,
                ) as resp:
                    if resp.status in (200, 204):
                        self._current_level = float(level)
                        _LOGGER.debug("Speaker level set to %d for %s", level, self._cam_id)
                    else:
                        _LOGGER.warning(
                            "Failed to set speaker level for %s: HTTP %d", self._cam_id, resp.status
                        )
        except Exception as err:
            _LOGGER.warning("Speaker level error for %s: %s", self._cam_id, err)
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschFrontLightIntensityNumber(CoordinatorEntity, NumberEntity):
    """Number entity: front light brightness (0–100%).

    Maps to frontLightIntensity (0.0–1.0) in PUT /v11/video_inputs/{id}/lighting_override.
    Only for cameras with featureSupport.light = True (outdoor cameras).
    Disabled by default — enable in Settings → Entities.
    """

    _attr_icon                        = "mdi:brightness-6"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 100
    _attr_native_step                 = 5
    _attr_mode                        = NumberMode.SLIDER
    _attr_native_unit_of_measurement  = "%"
    _attr_has_entity_name             = True
    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Front Light Intensity"
        self._attr_unique_id = f"bosch_shc_front_light_intensity_{cam_id.lower()}"
        self._attr_translation_key = "front_light_intensity"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model_name,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }

    @property
    def native_value(self) -> float | None:
        val = self.coordinator._shc_state_cache.get(self._cam_id, {}).get("front_light_intensity")
        if val is not None:
            return round(val * 100)
        return None

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_set_native_value(self, value: float) -> None:
        """Set front light intensity (0-100% → 0.0-1.0 API value)."""
        intensity = round(value / 100, 2)
        await self.coordinator.async_cloud_set_light_component(
            self._cam_id, "intensity", intensity
        )


# ─────────────────────────────────────────────────────────────────────────────
class _BoschGen2NumberBase(CoordinatorEntity, NumberEntity):
    """Base class for Gen2-only number entities."""

    _attr_entity_registry_enabled_default = True
    _attr_has_entity_name = True

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry
        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model_name,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }


class BoschLensElevationNumber(_BoschGen2NumberBase):
    """Number entity: lens mounting height in meters (Gen2 only).

    Reads from GET /v11/video_inputs/{id}/lens_elevation → {"elevation": 2.0}
    Writes via PUT /v11/video_inputs/{id}/lens_elevation → {"elevation": value}
    Used by camera for perspective correction in person detection.
    """

    _attr_icon                        = "mdi:arrow-up-down"
    _attr_native_min_value            = 0.5
    _attr_native_max_value            = 5.0
    _attr_native_step                 = 0.05
    _attr_mode                        = NumberMode.SLIDER
    _attr_native_unit_of_measurement  = "m"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Lens Elevation"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_lens_elevation"
        self._attr_translation_key = "lens_elevation"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float | None:
        return self.coordinator._lens_elevation_cache.get(self._cam_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._lens_elevation_cache.get(self._cam_id) is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_put_camera(
            self._cam_id, "lens_elevation", {"elevation": round(value, 2)}
        )
        self.coordinator._lens_elevation_cache[self._cam_id] = value
        self.async_write_ha_state()


class BoschMicrophoneLevelNumber(_BoschGen2NumberBase):
    """Number entity: microphone recording level 0-100% (Gen2 only).

    Reads from GET /v11/video_inputs/{id}/audio → {"microphoneLevel": 60, ...}
    Writes via PUT /v11/video_inputs/{id}/audio → full body with updated microphoneLevel.
    """

    _attr_icon                        = "mdi:microphone"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 100
    _attr_native_step                 = 5
    _attr_mode                        = NumberMode.SLIDER
    _attr_native_unit_of_measurement  = "%"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Microphone Level"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_mic_level"
        self._attr_translation_key = "microphone_level"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float | None:
        audio = self.coordinator._audio_cache.get(self._cam_id, {})
        val = audio.get("microphoneLevel")
        return float(val) if val is not None else None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator._audio_cache.get(self._cam_id))
        )

    async def async_set_native_value(self, value: float) -> None:
        from .switch import _is_gen2_indoor, _warn_if_privacy_on
        if _is_gen2_indoor(self) and await _warn_if_privacy_on(self, "Mikrofon-Lautstärke"):
            return
        audio = dict(self.coordinator._audio_cache.get(self._cam_id, {}))
        audio["microphoneLevel"] = int(round(value))
        await self.coordinator.async_put_camera(self._cam_id, "audio", audio)
        self.coordinator._audio_cache[self._cam_id] = audio
        self.async_write_ha_state()


class BoschWhiteBalanceNumber(_BoschGen2NumberBase):
    """Number entity: front light color temperature -1.0 to 1.0 (Gen2 only).

    -1.0 = cool/blue, 0.0 = neutral, 1.0 = warm/orange.
    Only applies to front light (top/bottom LEDs use RGB color instead).
    Reads from GET /v11/video_inputs/{id}/lighting/switch → frontLightSettings.whiteBalance
    Writes via PUT /lighting/switch with frontLightSettings only.
    """

    _attr_icon                        = "mdi:thermometer-lines"
    _attr_native_min_value            = -1.0
    _attr_native_max_value            = 1.0
    _attr_native_step                 = 0.05
    _attr_mode                        = NumberMode.SLIDER

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Farbtemperatur Frontlicht"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_white_balance"
        self._wb_value: float | None = None
        self._attr_translation_key = "white_balance"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float | None:
        cached = self.coordinator._lighting_switch_cache.get(self._cam_id, {})
        front = cached.get("frontLightSettings", {})
        wb = front.get("whiteBalance")
        if wb is not None:
            self._wb_value = wb
        return self._wb_value

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    async def async_set_native_value(self, value: float) -> None:
        """Set white balance for front light — sends FULL body (API requirement)."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        wb = round(value, 2)
        cached = self.coordinator._lighting_switch_cache.get(self._cam_id, {})
        body = {
            "frontLightSettings": cached.get("frontLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
            "topLedLightSettings": cached.get("topLedLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
            "bottomLedLightSettings": cached.get("bottomLedLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
        }
        body["frontLightSettings"] = {**body["frontLightSettings"], "whiteBalance": wb, "color": None}
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {self.coordinator.token}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/lighting/switch",
                    headers=headers, json=body,
                ) as resp:
                    if resp.status in (200, 201, 204):
                        self._wb_value = wb
                        try:
                            self.coordinator._lighting_switch_cache[self._cam_id] = await resp.json()
                        except Exception:
                            pass
                        _LOGGER.debug("White balance set to %.2f for %s", wb, self._cam_id[:8])
                    else:
                        _LOGGER.warning("White balance HTTP %d for %s", resp.status, self._cam_id[:8])
        except Exception as err:
            _LOGGER.warning("White balance error for %s: %s", self._cam_id[:8], err)
        self.async_write_ha_state()


class _BoschLedBrightnessBase(_BoschGen2NumberBase):
    """Base for Top/Bottom LED brightness (0-100%, Gen2 only)."""

    _attr_icon                        = "mdi:brightness-6"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 100
    _attr_native_step                 = 5
    _attr_mode                        = NumberMode.SLIDER
    _attr_native_unit_of_measurement  = "%"
    _led_key: str = ""  # override in subclass

    @property
    def native_value(self) -> float | None:
        cached = self.coordinator._lighting_switch_cache.get(self._cam_id, {})
        led = cached.get(self._led_key, {})
        val = led.get("brightness")
        if val is not None:
            self._brightness = float(val)
        return self._brightness

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._brightness: float | None = None

    async def async_set_native_value(self, value: float) -> None:
        """Set brightness — sends FULL body with all 3 groups (API requirement)."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        brightness = int(round(value))
        # Read current state from cache, update only our group
        cached = self.coordinator._lighting_switch_cache.get(self._cam_id, {})
        body = {
            "frontLightSettings": cached.get("frontLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
            "topLedLightSettings": cached.get("topLedLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
            "bottomLedLightSettings": cached.get("bottomLedLightSettings", {"brightness": 0, "color": None, "whiteBalance": 0.0}),
        }
        body[self._led_key] = {**body[self._led_key], "brightness": brightness}
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {self.coordinator.token}",
            "Content-Type": "application/json",
        }
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/lighting/switch",
                    headers=headers, json=body,
                ) as resp:
                    if resp.status in (200, 201, 204):
                        self._brightness = float(brightness)
                        try:
                            self.coordinator._lighting_switch_cache[self._cam_id] = await resp.json()
                        except Exception:
                            pass
                        _LOGGER.debug("%s brightness set to %d for %s", self._led_key, brightness, self._cam_id[:8])
                    else:
                        _LOGGER.warning("%s brightness HTTP %d for %s", self._led_key, resp.status, self._cam_id[:8])
        except Exception as err:
            _LOGGER.warning("%s brightness error for %s: %s", self._led_key, self._cam_id[:8], err)
        self.async_write_ha_state()


class BoschTopLedBrightnessNumber(_BoschLedBrightnessBase):
    """Number entity: top LED brightness 0-100% (Gen2, oberes Licht)."""
    _led_key = "topLedLightSettings"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Helligkeit Oberes Licht"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_top_led_brightness"
        self._attr_icon      = "mdi:arrow-up-bold"
        self._attr_translation_key = "top_led_brightness"
        self._attr_entity_category = EntityCategory.CONFIG


class BoschBottomLedBrightnessNumber(_BoschLedBrightnessBase):
    """Number entity: bottom LED brightness 0-100% (Gen2, unteres Licht)."""
    _led_key = "bottomLedLightSettings"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Helligkeit Unteres Licht"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_bottom_led_brightness"
        self._attr_icon      = "mdi:arrow-down-bold"
        self._attr_translation_key = "bottom_led_brightness"
        self._attr_entity_category = EntityCategory.CONFIG


class BoschMotionLightSensitivityNumber(_BoschGen2NumberBase):
    """Number entity: motion-triggered light sensitivity 1-5 (Gen2 only).

    Reads from GET /v11/video_inputs/{id}/lighting/motion → motionLightSensitivity
    Writes via PUT /v11/video_inputs/{id}/lighting/motion with full body.
    1 = low sensitivity, 5 = high sensitivity.
    """

    _attr_icon                        = "mdi:motion-sensor"
    _attr_native_min_value            = 1
    _attr_native_max_value            = 5
    _attr_native_step                 = 1
    _attr_mode                        = NumberMode.SLIDER

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Bewegungslicht Empfindlichkeit"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_motion_light_sensitivity"
        self._attr_translation_key = "motion_light_sensitivity"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float | None:
        cache = self.coordinator._motion_light_cache.get(self._cam_id, {})
        val = cache.get("motionLightSensitivity")
        return float(val) if val is not None else None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator._motion_light_cache.get(self._cam_id))
        )

    async def async_set_native_value(self, value: float) -> None:
        cache = dict(self.coordinator._motion_light_cache.get(self._cam_id, {}))
        if not cache:
            return
        cache["motionLightSensitivity"] = int(round(value))
        success = await self.coordinator.async_put_camera(
            self._cam_id, "lighting/motion", cache
        )
        if success:
            self.coordinator._motion_light_cache[self._cam_id] = cache
        self.async_write_ha_state()


class BoschDarknessThresholdNumber(_BoschGen2NumberBase):
    """Number entity: darkness threshold 0-100% (Gen2 only).

    Controls when the camera switches from day to night lighting mode.
    0 = always day, 100 = always night.
    Reads from GET /v11/video_inputs/{id}/lighting → {"darknessThreshold": 0.47, "softLightFading": bool}
    Writes via PUT /v11/video_inputs/{id}/lighting with full body.
    """

    _attr_icon                        = "mdi:weather-night"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 100
    _attr_native_step                 = 1
    _attr_mode                        = NumberMode.SLIDER
    _attr_native_unit_of_measurement  = "%"
    _attr_entity_category             = EntityCategory.CONFIG

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Dunkelheitsschwelle"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_darkness_threshold"
        self._attr_translation_key = "darkness_threshold"

    @property
    def native_value(self) -> float | None:
        cache = self.coordinator._global_lighting_cache.get(self._cam_id, {})
        val = cache.get("darknessThreshold")
        return round(float(val) * 100, 0) if val is not None else None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator._global_lighting_cache.get(self._cam_id))
        )

    async def async_set_native_value(self, value: float) -> None:
        cache = self.coordinator._global_lighting_cache.get(self._cam_id, {})
        soft_fading = cache.get("softLightFading", True)
        body = {"darknessThreshold": round(value / 100, 4), "softLightFading": soft_fading}
        success = await self.coordinator.async_put_camera(
            self._cam_id, "lighting", body
        )
        if success:
            self.coordinator._global_lighting_cache[self._cam_id] = body
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
# Gen2 Indoor II — Power-LED brightness + Alarm delays + Audio alarm sensitivity
# ─────────────────────────────────────────────────────────────────────────────
class BoschPowerLedBrightnessNumber(_BoschGen2NumberBase):
    """Number: Power-LED brightness (0-4, 5 discrete steps) — white LED showing camera is powered.

    Maps to "Power-LED" slider in iOS app → Kamera-Funktionen.
    Distinct from Status-LED (red, recording indicator, BoschStatusLedSwitch).
    PUT /v11/video_inputs/{id}/iconLedBrightness  body: {"value": 0-4}
    Confirmed by direct API test 2026-04-11: writing value=5 → HTTP 400
    "must be less than or equal to 4". The iOS app shows this as a percent
    slider but internally maps to 5 discrete positions (0 = off, 4 = max).
    """

    _attr_icon                        = "mdi:led-on"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 4
    _attr_native_step                 = 1
    _attr_mode                        = NumberMode.SLIDER
    _attr_entity_category             = EntityCategory.CONFIG

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Power-LED"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_power_led_brightness"
        self._attr_translation_key = "power_led_brightness"

    @property
    def native_value(self) -> float | None:
        return self.coordinator._icon_led_brightness_cache.get(self._cam_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._icon_led_brightness_cache.get(self._cam_id) is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        val = int(round(max(0, min(4, value))))
        success = await self.coordinator.async_put_camera(
            self._cam_id, "iconLedBrightness", {"value": val}
        )
        if success:
            self.coordinator._icon_led_brightness_cache[self._cam_id] = val
        self.async_write_ha_state()


class _BoschAlarmDelayBase(_BoschGen2NumberBase):
    """Shared base for alarm_settings integer fields."""

    _field: str = ""
    _attr_native_step                 = 1
    _attr_mode                        = NumberMode.BOX
    _attr_native_unit_of_measurement  = "s"
    _attr_entity_category             = EntityCategory.CONFIG

    @property
    def _settings(self) -> dict:
        return self.coordinator._alarm_settings_cache.get(self._cam_id, {})

    @property
    def native_value(self) -> float | None:
        val = self._settings.get(self._field)
        return float(val) if val is not None else None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and bool(self._settings)
        )

    async def async_set_native_value(self, value: float) -> None:
        cfg = dict(self._settings)
        if not cfg:
            return
        cfg[self._field] = int(round(value))
        success = await self.coordinator.async_put_camera(
            self._cam_id, "alarm_settings", cfg
        )
        if success:
            self.coordinator._alarm_settings_cache[self._cam_id] = cfg
        self.async_write_ha_state()


class BoschAlarmDelayNumber(_BoschAlarmDelayBase):
    """Number: siren duration (alarm_settings.alarmDelayInSeconds).

    How long the 75 dB siren stays active when triggered.
    Observed range from capture: 52–76s.
    """

    _field                  = "alarmDelayInSeconds"
    _attr_icon              = "mdi:timer-alert"
    _attr_native_min_value  = 10
    _attr_native_max_value  = 300

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Sirenen-Dauer"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_alarm_delay"
        self._attr_translation_key = "alarm_delay"


class BoschAlarmActivationDelayNumber(_BoschAlarmDelayBase):
    """Number: siren activation delay (alarm_settings.alarmActivationDelaySeconds).

    Time between detection and siren activation. Observed: 1–180s.
    """

    _field                  = "alarmActivationDelaySeconds"
    _attr_icon              = "mdi:timer-sand"
    _attr_native_min_value  = 0
    _attr_native_max_value  = 600

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Alarm-Verzögerung"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_alarm_activation_delay"
        self._attr_translation_key = "alarm_activation_delay"


class BoschPreAlarmDelayNumber(_BoschAlarmDelayBase):
    """Number: pre-alarm duration (alarm_settings.preAlarmDelayInSeconds).

    How long the LED warning stays active before the siren fires.
    Observed: 30–38s.
    """

    _field                  = "preAlarmDelayInSeconds"
    _attr_icon              = "mdi:led-on"
    _attr_native_min_value  = 0
    _attr_native_max_value  = 300

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Pre-Alarm Dauer"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_prealarm_delay"
        self._attr_translation_key = "pre_alarm_delay"


class BoschAudioAlarmSensitivityNumber(_BoschGen2NumberBase):
    """Number: audio alarm sensitivity (audioAlarm.sensitivity, 0-10).

    Write-only — `GET /audioAlarm` returns only {threshold, enabled, audioAlarmConfiguration}
    (confirmed by direct API test 2026-04-11). The sensitivity field is sent in PUT bodies
    but not echoed back. We track the last-written value optimistically and default to 0.
    Disabled by default since there's no read-side value to show.
    """

    _attr_icon                        = "mdi:microphone"
    _attr_native_min_value            = 0
    _attr_native_max_value            = 10
    _attr_native_step                 = 1
    _attr_mode                        = NumberMode.SLIDER
    _attr_entity_category             = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Geraeusch Empfindlichkeit"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_audio_alarm_sensitivity"
        self._last_written: int = 0
        self._attr_translation_key = "audio_alarm_sensitivity"

    @property
    def _settings(self) -> dict:
        return self.coordinator.audio_alarm_settings(self._cam_id) or {}

    @property
    def native_value(self) -> float | None:
        # GET response doesn't echo sensitivity — return last-written value (default 0)
        val = self._settings.get("sensitivity", self._last_written)
        return float(val)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and bool(self._settings)

    async def async_set_native_value(self, value: float) -> None:
        from .switch import _is_gen2_indoor, _warn_if_privacy_on
        if _is_gen2_indoor(self) and await _warn_if_privacy_on(self, "Geräusch-Empfindlichkeit"):
            return
        current = dict(self._settings)
        if not current:
            return
        new_val = int(round(value))
        current["sensitivity"] = new_val
        current.setdefault("enabled", True)
        current.setdefault("threshold", 54)
        current.setdefault("audioAlarmConfiguration", "CUSTOM")
        success = await self.coordinator.async_put_camera(
            self._cam_id, "audioAlarm", current
        )
        if success:
            self._last_written = new_val
            cam = self.coordinator.data.get(self._cam_id)
            if cam is not None:
                cam["audioAlarm"] = current
        self.async_write_ha_state()
