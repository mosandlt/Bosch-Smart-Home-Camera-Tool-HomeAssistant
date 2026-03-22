"""Select entities for Bosch Smart Home Camera integration.

Provides:
  - BoschVideoQualitySelect: dropdown to choose streaming quality
  - BoschMotionSensitivitySelect: dropdown to set motion detection sensitivity
    (SUPER_HIGH / HIGH / MEDIUM / MEDIUM_LOW / LOW)
    Reads from coordinator.motion_settings(cam_id)["motionAlarmConfiguration"].
    Writes via PUT /v11/video_inputs/{id}/motion.
    Disabled by default.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschCameraCoordinator, DOMAIN

_LOGGER = logging.getLogger(__name__)

QUALITY_OPTIONS = ["Auto", "Hoch (30 Mbps)", "Niedrig (1.9 Mbps)"]
QUALITY_MAP = {
    "Auto":              "auto",
    "Hoch (30 Mbps)":   "high",
    "Niedrig (1.9 Mbps)": "low",
}
QUALITY_MAP_REVERSE = {v: k for k, v in QUALITY_MAP.items()}

MOTION_SENSITIVITY_OPTIONS = ["SUPER_HIGH", "HIGH", "MEDIUM", "MEDIUM_LOW", "LOW"]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BoschCameraCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = []
    for cam_id in coordinator.data:
        entities.append(BoschVideoQualitySelect(coordinator, cam_id, config_entry))
        entities.append(BoschMotionSensitivitySelect(coordinator, cam_id, config_entry))
    async_add_entities(entities)


class BoschVideoQualitySelect(CoordinatorEntity, SelectEntity):
    """Select entity to choose the RTSPS stream quality (inst + highQualityVideo)."""

    _attr_icon = "mdi:video-high-definition"
    _attr_options = QUALITY_OPTIONS

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        cam_data = coordinator.data.get(cam_id, {})
        cam_info = cam_data.get("info", {})
        self._cam_title = cam_info.get("title", cam_id)
        self._entry = entry
        self._attr_name      = f"Bosch {self._cam_title} Video Quality"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_video_quality"

    @property
    def device_info(self):
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model": cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version": cam_info.get("firmwareVersion", ""),
        }

    @property
    def current_option(self) -> str:
        """Return the current quality label."""
        quality_key = self.coordinator.get_quality(self._cam_id)
        return QUALITY_MAP_REVERSE.get(quality_key, "Auto")

    async def async_select_option(self, option: str) -> None:
        """Handle quality selection — update coordinator preference and reconnect stream."""
        quality_key = QUALITY_MAP.get(option, "auto")
        self.coordinator.set_quality(self._cam_id, quality_key)
        # If stream is currently active, reconnect with new quality
        live = self.coordinator.data.get(self._cam_id, {}).get("live", {})
        if live.get("rtspsUrl") or live.get("proxyUrl"):
            try:
                new_live = await self.coordinator.try_live_connection(self._cam_id)
                if new_live:
                    self.coordinator.data[self._cam_id]["live"] = new_live
                    await self.coordinator._register_go2rtc_stream(
                        self._cam_id, new_live.get("rtspsUrl", "")
                    )
            except Exception:
                pass
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschMotionSensitivitySelect(CoordinatorEntity, SelectEntity):
    """Select entity to set motion detection sensitivity for a camera.

    Options: SUPER_HIGH / HIGH / MEDIUM / MEDIUM_LOW / LOW
    Reads from coordinator.motion_settings(cam_id)["motionAlarmConfiguration"].
    Writes via PUT /v11/video_inputs/{id}/motion {"enabled": true, "motionAlarmConfiguration": value}.
    Disabled by default — enable in Settings → Entities.
    """

    _attr_icon    = "mdi:motion-sensor"
    _attr_options = MOTION_SENSITIVITY_OPTIONS
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        cam_data = coordinator.data.get(cam_id, {})
        cam_info = cam_data.get("info", {})
        self._cam_title = cam_info.get("title", cam_id)

        self._attr_name      = f"Bosch {self._cam_title} Motion Sensitivity"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_motion_sensitivity_select"

    @property
    def device_info(self) -> dict:
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name":        f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":       cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version":  cam_info.get("firmwareVersion", ""),
        }

    @property
    def current_option(self) -> str | None:
        """Return the current motion sensitivity level."""
        settings = self.coordinator.motion_settings(self._cam_id)
        val = settings.get("motionAlarmConfiguration")
        if val in MOTION_SENSITIVITY_OPTIONS:
            return val
        return None

    @property
    def available(self) -> bool:
        """Available only when motion settings have been fetched (slow tier)."""
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator.motion_settings(self._cam_id))
        )

    async def async_select_option(self, option: str) -> None:
        """Write the new sensitivity level to the camera via cloud API."""
        if option not in MOTION_SENSITIVITY_OPTIONS:
            _LOGGER.warning("Invalid motion sensitivity option: %s", option)
            return
        # Read current enabled state (preserve it; default True if unknown)
        settings = self.coordinator.motion_settings(self._cam_id)
        enabled  = settings.get("enabled", True)
        success  = await self.coordinator.async_put_camera(
            self._cam_id,
            "motion",
            {"enabled": enabled, "motionAlarmConfiguration": option},
        )
        if success:
            # Optimistically update coordinator data so UI reflects immediately
            motion_data = self.coordinator.data.get(self._cam_id, {}).get("motion", {})
            motion_data["motionAlarmConfiguration"] = option
            if self._cam_id in self.coordinator.data:
                self.coordinator.data[self._cam_id]["motion"] = motion_data
            _LOGGER.debug("Motion sensitivity set to %s for %s", option, self._cam_id)
        else:
            _LOGGER.warning("Failed to set motion sensitivity for %s", self._cam_id)
        self.async_write_ha_state()
