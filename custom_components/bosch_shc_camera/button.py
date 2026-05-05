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
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, get_options

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


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

    coordinator = config_entry.runtime_data
    entities = []
    for cam_id in coordinator.data:
        entities.append(BoschRefreshSnapshotButton(coordinator, cam_id, config_entry))
        # Acoustic alarm (siren) — available for all cameras (disabled by default).
        # If the camera doesn't support it, the API returns HTTP 442 which is handled gracefully.
        entities.append(BoschAcousticAlarmButton(coordinator, cam_id, config_entry))
    async_add_entities(entities, update_before_add=False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschRefreshSnapshotButton(CoordinatorEntity, ButtonEntity):
    """Button: force an immediate coordinator refresh.

    Fetches latest camera info, status, and events from the Bosch Cloud API
    right now — without waiting for the next scheduled interval.
    Useful after motion events or when you want a fresh snapshot immediately.
    """

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

        self._attr_name            = f"Bosch {self._cam_title} Refresh Snapshot"
        self._attr_unique_id       = f"bosch_shc_refresh_{cam_id.lower()}"
        self._attr_icon            = "mdi:camera-refresh"
        self._attr_translation_key = "refresh_snapshot"

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

    async def async_press(self) -> None:
        """Force an immediate data refresh and image update for this camera."""
        _LOGGER.debug("Snapshot refresh triggered for %s", self._cam_title)
        # Fire coordinator refresh in background — do NOT await it.
        # async_request_refresh() awaits the full coordinator tick (can take 6-22 s);
        # blocking here makes the button feel frozen in the browser/card.
        self.hass.async_create_task(self.coordinator.async_request_refresh())
        # Refresh the camera image immediately (parallel, faster than coordinator tick)
        cam = self.coordinator._camera_entities.get(self._cam_id)
        if cam:
            self.hass.async_create_task(cam._async_trigger_image_refresh(delay=0))


# ─────────────────────────────────────────────────────────────────────────────
class BoschAcousticAlarmButton(CoordinatorEntity, ButtonEntity):
    """Button: trigger the camera siren (acoustic alarm).

    Sends PUT /v11/video_inputs/{id}/acoustic_alarm to activate the built-in siren.
    Discovered from iOS app analysis (GetAcousticAlarmActivationUrl).
    Created for all cameras — if a model doesn't support it, the API returns HTTP 442
    which is handled gracefully. Disabled by default to avoid UI clutter.
    """

    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_translation_key = "acoustic_alarm"
    _attr_entity_category = EntityCategory.CONFIG

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

        self._attr_name            = f"Bosch {self._cam_title} Siren"
        self._attr_unique_id       = f"bosch_shc_siren_{cam_id.lower()}"
        self._attr_icon            = "mdi:alarm-light"

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

    async def async_press(self) -> None:
        """Trigger the acoustic alarm (siren) on the camera."""
        _LOGGER.info("Triggering acoustic alarm (siren) for %s", self._cam_title)
        try:
            success = await self.coordinator.async_put_camera(
                self._cam_id, "acoustic_alarm", {"enabled": True}
            )
            if success:
                _LOGGER.info("Siren activated for %s", self._cam_title)
            else:
                _LOGGER.warning(
                    "Siren activation returned non-success for %s — "
                    "endpoint may not be supported or payload format differs",
                    self._cam_title,
                )
        except Exception as err:
            _LOGGER.error("Siren activation failed for %s: %s", self._cam_title, err)
