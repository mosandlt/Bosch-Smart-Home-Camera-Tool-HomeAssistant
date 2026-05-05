"""Bosch Smart Home Camera — Update Platform.

Shows firmware update status using the native HA update entity.
Data source: GET /v11/video_inputs/{id}/firmware (short form).
Response: {current, upToDate, update, updating, status}
"""

import logging

from homeassistant.components.update import UpdateEntity, UpdateDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN

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
        entities.append(BoschFirmwareUpdate(coordinator, cam_id, config_entry))
    async_add_entities(entities, update_before_add=False)


class BoschFirmwareUpdate(CoordinatorEntity, UpdateEntity):
    """Update entity showing camera firmware status."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_has_entity_name = True

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

        self._attr_name            = f"Bosch {self._cam_title} Firmware"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_firmware_update"
        self._attr_translation_key = "firmware_update"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

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
    def installed_version(self) -> str | None:
        fw = self.coordinator._firmware_cache.get(self._cam_id, {})
        return fw.get("current") or self._fw or None

    @property
    def latest_version(self) -> str | None:
        fw = self.coordinator._firmware_cache.get(self._cam_id, {})
        if not fw:
            return self.installed_version
        if fw.get("upToDate", True):
            return self.installed_version
        update_ver = fw.get("update")
        if update_ver:
            return update_ver
        # Not up to date but no update version specified
        return "update available"

    @property
    def in_progress(self) -> bool:
        fw = self.coordinator._firmware_cache.get(self._cam_id, {})
        return fw.get("updating", False)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict:
        fw = self.coordinator._firmware_cache.get(self._cam_id, {})
        return {
            "up_to_date": fw.get("upToDate"),
            "updating": fw.get("updating", False),
            "status": fw.get("status", ""),
        }
