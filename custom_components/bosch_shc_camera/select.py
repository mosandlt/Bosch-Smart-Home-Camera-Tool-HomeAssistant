"""Select entities for Bosch Smart Home Camera integration.

Provides:
  - BoschVideoQualitySelect: dropdown to choose streaming quality
"""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschCameraCoordinator, DOMAIN

QUALITY_OPTIONS = ["Auto", "Hoch (30 Mbps)", "Niedrig (1.9 Mbps)"]
QUALITY_MAP = {
    "Auto":              "auto",
    "Hoch (30 Mbps)":   "high",
    "Niedrig (1.9 Mbps)": "low",
}
QUALITY_MAP_REVERSE = {v: k for k, v in QUALITY_MAP.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BoschCameraCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = []
    for cam_id in coordinator.data:
        entities.append(BoschVideoQualitySelect(coordinator, cam_id, config_entry))
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
        raw_name = cam_info.get("name") or cam_info.get("id", cam_id)
        self._cam_title = raw_name.replace("_", " ").title()
        self._entry = entry
        self._attr_name      = f"Bosch {self._cam_title} Video Quality"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_video_quality"

    @property
    def device_info(self):
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": cam_info.get("name", self._cam_id),
            "manufacturer": "Bosch",
            "model": cam_info.get("deviceType", "Smart Home Camera"),
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
