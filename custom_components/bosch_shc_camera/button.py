"""Bosch Smart Home Camera — Button Platform.

Creates one button entity per camera:
  • {Name} Refresh Snapshot — forces an immediate coordinator refresh (data + image)

The Live Stream is controlled by the switch platform (switch.py):
  switch.bosch_garten_live_stream  →  ON = open live proxy, OFF = close
"""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, get_options

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities for each camera."""
    opts = get_options(config_entry)
    if not opts.get("enable_snapshot_button", True):
        _LOGGER.debug("Buttons disabled in options — skipping button platform")
        return

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = [
        BoschRefreshSnapshotButton(coordinator, cam_id, config_entry)
        for cam_id in coordinator.data
    ]
    async_add_entities(entities, update_before_add=False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschRefreshSnapshotButton(CoordinatorEntity, ButtonEntity):
    """Button: force an immediate coordinator refresh.

    Fetches latest camera info, status, and events from the Bosch Cloud API
    right now — without waiting for the next scheduled interval.
    Useful after motion events or when you want a fresh snapshot immediately.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Refresh Snapshot"
        self._attr_unique_id = f"bosch_shc_refresh_{cam_id.lower()}"
        self._attr_icon      = "mdi:camera-refresh"

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

    async def async_press(self) -> None:
        """Force an immediate data refresh for this camera."""
        _LOGGER.debug("Snapshot refresh triggered for %s", self._cam_title)
        await self.coordinator.async_request_refresh()
