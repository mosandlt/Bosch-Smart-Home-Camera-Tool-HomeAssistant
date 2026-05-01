"""Bosch Smart Home Camera — Binary Sensor Platform.

Creates binary sensor entities per camera:
  • {Name} Motion           — ON when a MOVEMENT event was detected within the last 30 seconds
  • {Name} Audio Alarm      — ON when an AUDIO_ALARM event was detected within the last 30 seconds
  • {Name} Person Detected  — ON when a PERSON event was detected within the last 30 seconds

All sensors are disabled by default (entity_registry_enabled_default = False).
Enable them in Settings → Entities if you want to trigger automations from motion/audio/person events.

Event data is read from coordinator.data[cam_id]["events"] (the most recent event list).
The sensors go ON when the most-recent event matches the type AND its timestamp is within
the last 30 seconds; otherwise they are OFF.

Device class:
  motion binary sensor  → BinarySensorDeviceClass.MOTION
  audio  binary sensor  → BinarySensorDeviceClass.SOUND
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import DOMAIN, BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

# How long (seconds) a motion/audio event keeps the binary sensor ON.
# 90 s covers the polling-only fallback (coordinator scan_interval is 60 s, so
# an event could be up to 60 s old when first seen by data[]); 30 s would
# systematically miss events in that path and only fire when an FCM push
# happens to land between two ticks.
EVENT_ACTIVE_WINDOW = 90


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities for each camera."""
    coordinator: BoschCameraCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = []
    for cam_id in coordinator.data:
        cam_info = coordinator.data[cam_id].get("info", {})
        has_sound = cam_info.get("featureSupport", {}).get("sound", False)
        entities.append(BoschMotionBinarySensor(coordinator, cam_id, config_entry))
        entities.append(BoschPersonDetectedBinarySensor(coordinator, cam_id, config_entry))
        if has_sound:
            entities.append(BoschAudioAlarmBinarySensor(coordinator, cam_id, config_entry))
    async_add_entities(entities, update_before_add=False)


# ─────────────────────────────────────────────────────────────────────────────
class _BoschBinarySensorBase(CoordinatorEntity, BinarySensorEntity):
    """Shared base for Bosch camera binary sensors."""

    # Disabled by default — enable explicitly in entity registry if desired
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

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

    @property
    def _cam_data(self) -> dict:
        return self.coordinator.data.get(self._cam_id, {})

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

    def _get_latest_event_of_type(self, event_type: str) -> dict | None:
        """Return the most recent event matching event_type, or None."""
        events = self._cam_data.get("events", [])
        for ev in events:
            if ev.get("eventType", "") == event_type:
                return ev
        return None

    def _event_within_window(self, event: dict) -> bool:
        """Return True if the event timestamp is within EVENT_ACTIVE_WINDOW seconds of now."""
        ts_str = event.get("timestamp", "")
        if not ts_str:
            return False
        try:
            # Strip to 19 chars: "2026-03-22T14:30:00" (API may append ".000Z")
            ts_clean = ts_str[:19]
            dt = datetime.fromisoformat(ts_clean)
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            dt_local = dt.replace(tzinfo=local_tz or timezone.utc)
            now_local = datetime.now(tz=local_tz or timezone.utc)
            return (now_local - dt_local) <= timedelta(seconds=EVENT_ACTIVE_WINDOW)
        except (ValueError, TypeError):
            return False


# ─────────────────────────────────────────────────────────────────────────────
class BoschMotionBinarySensor(_BoschBinarySensorBase):
    """Binary sensor: ON when a MOVEMENT event occurred within the last 30 seconds."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_icon         = "mdi:motion-sensor"

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Motion"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_motion_binary"
        self._attr_translation_key = "motion"

    @property
    def is_on(self) -> bool:
        event = self._get_latest_event_of_type("MOVEMENT")
        if event is None:
            return False
        return self._event_within_window(event)

    @property
    def extra_state_attributes(self) -> dict:
        event = self._get_latest_event_of_type("MOVEMENT")
        if not event:
            return {}
        return {
            "event_id":  event.get("id", ""),
            "timestamp": event.get("timestamp", ""),
            "image_url": event.get("imageUrl", ""),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschAudioAlarmBinarySensor(_BoschBinarySensorBase):
    """Binary sensor: ON when an AUDIO_ALARM event occurred within the last 30 seconds."""

    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_icon         = "mdi:volume-high"

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Audio Alarm"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_audio_alarm_binary"
        self._attr_translation_key = "audio_alarm_binary"

    @property
    def is_on(self) -> bool:
        event = self._get_latest_event_of_type("AUDIO_ALARM")
        if event is None:
            return False
        return self._event_within_window(event)

    @property
    def extra_state_attributes(self) -> dict:
        event = self._get_latest_event_of_type("AUDIO_ALARM")
        if not event:
            return {}
        return {
            "event_id":  event.get("id", ""),
            "timestamp": event.get("timestamp", ""),
            "image_url": event.get("imageUrl", ""),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschPersonDetectedBinarySensor(_BoschBinarySensorBase):
    """Binary sensor: ON when a PERSON event occurred within the last 30 seconds."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_icon         = "mdi:account-alert"

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Person Detected"
        self._attr_unique_id       = f"bosch_shc_cam_{cam_id}_person_detected"
        self._attr_translation_key = "person_detected"

    @property
    def is_on(self) -> bool:
        event = self._get_latest_event_of_type("PERSON")
        if event is None:
            return False
        return self._event_within_window(event)

    @property
    def extra_state_attributes(self) -> dict:
        event = self._get_latest_event_of_type("PERSON")
        if not event:
            return {}
        return {
            "event_id":  event.get("id", ""),
            "timestamp": event.get("timestamp", ""),
            "image_url": event.get("imageUrl", ""),
        }
