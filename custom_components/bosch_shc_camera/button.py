"""Bosch Smart Home Camera — Button Platform.

Creates two button entities per camera:
  • {Name} Refresh Snapshot  — forces an immediate coordinator refresh
  • {Name} Open Live Stream  — tries PUT /connection with all known enum values;
                               on success the camera entity's stream_source becomes available
                               and HA can render the RTSP stream live.
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

    entities = []
    for cam_id in coordinator.data:
        entities.extend([
            BoschRefreshSnapshotButton(coordinator, cam_id, config_entry),
            BoschOpenLiveStreamButton(coordinator, cam_id, config_entry),
        ])
    async_add_entities(entities, update_before_add=False)


# ─────────────────────────────────────────────────────────────────────────────
class _BoschButtonBase(CoordinatorEntity, ButtonEntity):
    """Shared base for Bosch camera button entities."""

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

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


# ─────────────────────────────────────────────────────────────────────────────
class BoschRefreshSnapshotButton(_BoschButtonBase):
    """Button: force an immediate snapshot refresh (triggers coordinator update)."""

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Refresh Snapshot"
        self._attr_unique_id = f"bosch_shc_refresh_{cam_id.lower()}"
        self._attr_icon      = "mdi:camera-refresh"

    async def async_press(self) -> None:
        """Force an immediate coordinator refresh for all cameras."""
        _LOGGER.debug("Snapshot refresh triggered for %s", self._cam_title)
        await self.coordinator.async_request_refresh()


# ─────────────────────────────────────────────────────────────────────────────
class BoschOpenLiveStreamButton(_BoschButtonBase):
    """Button: try to open a live proxy connection for this camera.

    On success:
      • The camera entity's stream_source property returns the RTSP URL
      • HA's stream component handles HLS conversion for the Lovelace card
      • The RTSP URL is also shown in the camera entity's extra_state_attributes

    On failure (ConnectionType enum still unknown):
      • A warning is logged with mitmproxy capture instructions
      • No error is raised — the button is safe to press at any time
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Open Live Stream"
        self._attr_unique_id = f"bosch_shc_live_{cam_id.lower()}"
        self._attr_icon      = "mdi:video-wireless"

    async def async_press(self) -> None:
        """Try PUT /connection — cycles through all known ConnectionType enum values."""
        _LOGGER.info(
            "Attempting live stream connection for %s (%s)",
            self._cam_title,
            self._cam_id,
        )
        result = await self.coordinator.try_live_connection(self._cam_id)
        if result:
            rtsp = result.get("rtspUrl", "")
            proxy = result.get("proxyUrl", "")
            _LOGGER.info(
                "Live stream established for %s — RTSP: %s  Proxy: %s",
                self._cam_title,
                rtsp,
                proxy,
            )
        else:
            _LOGGER.warning(
                "Live stream unavailable for %s. "
                "To find the ConnectionType enum: run mitmproxy → open Bosch Smart Home "
                "Camera app → tap Live View → look for '📤 Request JSON: {\"type\": \"...\"}' "
                "in the terminal → update LIVE_TYPE_CANDIDATES in __init__.py.",
                self._cam_title,
            )
