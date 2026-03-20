"""Bosch Smart Home Camera — Sensor Platform.

Creates three sensor entities per camera:
  • {Name} Status         — ONLINE / OFFLINE / UNKNOWN
  • {Name} Last Event     — timestamp of the most recent motion event (device class: timestamp)
  • {Name} Events Today   — count of motion events today
"""

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.util import dt as dt_util
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
    """Set up sensor entities for each camera."""
    opts = get_options(config_entry)
    if not opts.get("enable_sensors", True):
        _LOGGER.debug("Sensors disabled in options — skipping sensor platform")
        return

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    entities = []
    for cam_id in coordinator.data:
        entities.extend([
            BoschCameraStatusSensor(coordinator, cam_id, config_entry),
            BoschCameraLastEventSensor(coordinator, cam_id, config_entry),
            BoschCameraEventsTodaySensor(coordinator, cam_id, config_entry),
        ])
    async_add_entities(entities, update_before_add=False)


# ─────────────────────────────────────────────────────────────────────────────
class _BoschSensorBase(CoordinatorEntity, SensorEntity):
    """Shared base for all Bosch camera sensors."""

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
    def _cam_data(self) -> dict:
        return self.coordinator.data.get(self._cam_id, {})

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
class BoschCameraStatusSensor(_BoschSensorBase):
    """Sensor: ONLINE / OFFLINE / UNKNOWN."""

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Status"
        self._attr_unique_id = f"bosch_shc_status_{cam_id.lower()}"

    @property
    def native_value(self) -> str:
        return self._cam_data.get("status", "UNKNOWN")

    @property
    def icon(self) -> str:
        return "mdi:camera" if self.native_value == "ONLINE" else "mdi:camera-off"

    @property
    def extra_state_attributes(self) -> dict:
        info = self._cam_data.get("info", {})
        return {
            "camera_id": self._cam_id,
            "model":     info.get("hardwareVersion", ""),
            "firmware":  info.get("firmwareVersion", ""),
            "mac":       info.get("macAddress", ""),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraLastEventSensor(_BoschSensorBase):
    """Sensor: datetime of the most recent motion event."""

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name        = f"Bosch {self._cam_title} Last Event"
        self._attr_unique_id   = f"bosch_shc_last_event_{cam_id.lower()}"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon        = "mdi:motion-sensor"

    @property
    def native_value(self) -> datetime | None:
        events = self._cam_data.get("events", [])
        if not events:
            return None
        ts_str = events[0].get("timestamp", "")
        if not ts_str:
            return None
        try:
            # API returns e.g. "2026-03-19T09:32:08.000Z" or "2026-03-19T09:32:08"
            # Despite the Z suffix, Bosch timestamps are in local time —
            # treating as UTC causes a 1-hour offset in CET/CEST timezones.
            ts_clean = ts_str[:19]  # "2026-03-19T09:32:08"
            dt = datetime.fromisoformat(ts_clean)
            local_tz = dt_util.get_time_zone(self.hass.config.time_zone)
            return dt.replace(tzinfo=local_tz or timezone.utc)
        except ValueError:
            return None

    @property
    def extra_state_attributes(self) -> dict:
        events = self._cam_data.get("events", [])
        latest = events[0] if events else {}
        return {
            "event_type": latest.get("eventType", ""),
            "event_id":   latest.get("id", "")[:8],
            "has_image":  bool(latest.get("imageUrl")),
            "has_clip":   bool(latest.get("videoClipUrl")),
            "clip_status": latest.get("videoClipUploadStatus", ""),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraEventsTodaySensor(_BoschSensorBase):
    """Sensor: count of motion events that occurred today."""

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name                        = f"Bosch {self._cam_title} Events Today"
        self._attr_unique_id                   = f"bosch_shc_events_today_{cam_id.lower()}"
        self._attr_icon                        = "mdi:counter"
        self._attr_native_unit_of_measurement  = "events"
        self._attr_state_class                 = "total"

    @property
    def native_value(self) -> int:
        events = self._cam_data.get("events", [])
        today  = datetime.now().strftime("%Y-%m-%d")
        return sum(1 for ev in events if ev.get("timestamp", "").startswith(today))

    @property
    def extra_state_attributes(self) -> dict:
        events = self._cam_data.get("events", [])
        today  = datetime.now().strftime("%Y-%m-%d")
        today_events = [ev for ev in events if ev.get("timestamp", "").startswith(today)]
        return {
            "events_in_feed": len(events),
            "latest_timestamps": [
                ev.get("timestamp", "")[:19] for ev in today_events[:5]
            ],
        }
