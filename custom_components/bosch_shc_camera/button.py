"""Bosch Smart Home Camera — Button Platform.

Creates two button entities per camera:
  • {Name} Refresh Snapshot  — forces an immediate coordinator refresh
  • {Name} Open Live Stream  — opens PUT /connection with type "REMOTE";
                               on success the camera entity's stream_source becomes
                               rtsps://proxy-NN:443/{hash}/rtsp_tunnel (30fps H.264+AAC)
                               and HA can render a live video stream.
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
    """Button: open a live proxy connection for this camera.

    On success:
      • The camera entity's stream_source returns the rtsps:// URL
      • Stream: H.264 1920×1080 30fps + AAC-LC audio
      • HA's stream component handles HLS conversion for the Lovelace card
      • The rtsps:// URL is also shown in the camera entity's extra_state_attributes

    Note: HA's stream component must support rtsps:// (RTSP/1.0 over TLS with
    TLS verification disabled for Bosch's private CA).
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Open Live Stream"
        self._attr_unique_id = f"bosch_shc_live_{cam_id.lower()}"
        self._attr_icon      = "mdi:video-wireless"

    async def async_press(self) -> None:
        """Open PUT /connection REMOTE → get proxy URL + rtsps:// stream URL."""
        _LOGGER.info(
            "Attempting live stream connection for %s (%s)",
            self._cam_title,
            self._cam_id,
        )
        result = await self.coordinator.try_live_connection(self._cam_id)
        if result:
            rtsps = result.get("rtspsUrl", result.get("rtspUrl", ""))
            proxy = result.get("proxyUrl", "")
            _LOGGER.info(
                "Live stream established for %s — rtsps: %s  snap: %s",
                self._cam_title,
                rtsps,
                proxy,
            )
        else:
            _LOGGER.warning(
                "Live stream unavailable for %s. Check token validity.",
                self._cam_title,
            )
