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

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN

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
        pan_limit = cam_info.get("featureSupport", {}).get("panLimit", 0)
        if pan_limit:
            entities.append(BoschPanNumber(coordinator, cam_id, config_entry, pan_limit))
        entities.append(BoschAudioThresholdNumber(coordinator, cam_id, config_entry))
    async_add_entities(entities, update_before_add=False)


class BoschPanNumber(CoordinatorEntity, NumberEntity):
    """Number entity to control the pan position of the 360 camera."""

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry, pan_limit: int) -> None:
        super().__init__(coordinator)
        self._cam_id    = cam_id
        self._entry     = entry
        self._pan_limit = pan_limit

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
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

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }

    @property
    def native_value(self) -> float | None:
        return self.coordinator._pan_cache.get(self._cam_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._pan_cache.get(self._cam_id) is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_cloud_set_pan(self._cam_id, int(value))


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
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Audio Threshold"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_audio_threshold"

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model,
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
        """Write the new threshold to the camera via cloud API."""
        threshold = int(round(value))
        # Read current enabled state (preserve it; default True if unknown)
        settings = self.coordinator.audio_alarm_settings(self._cam_id)
        enabled  = settings.get("enabled", True)
        success  = await self.coordinator.async_put_camera(
            self._cam_id,
            "audioAlarm",
            {"threshold": threshold, "enabled": enabled},
        )
        if success:
            # Optimistically update coordinator data so UI reflects immediately
            audio_data = self.coordinator.data.get(self._cam_id, {}).get("audioAlarm", {})
            audio_data["threshold"] = threshold
            if self._cam_id in self.coordinator.data:
                self.coordinator.data[self._cam_id]["audioAlarm"] = audio_data
            _LOGGER.debug("Audio threshold set to %d dB for %s", threshold, self._cam_id)
        else:
            _LOGGER.warning("Failed to set audio threshold for %s", self._cam_id)
        self.async_write_ha_state()
