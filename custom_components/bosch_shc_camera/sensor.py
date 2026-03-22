"""Bosch Smart Home Camera — Sensor Platform.

Creates sensor entities per camera:
  • {Name} Status              — ONLINE / OFFLINE / UNKNOWN
  • {Name} Last Event          — timestamp of the most recent motion event (device class: timestamp)
  • {Name} Events Today        — count of motion events today
  • {Name} WiFi Signal         — WiFi signal strength as percentage (device_class: signal_strength)
                                  attributes: ssid, ip_address, mac_address
  • {Name} Firmware Version    — firmware version string from /v11/video_inputs
                                  attributes: up_to_date
  • {Name} Ambient Light Level — ambient light sensor level (0.0–1.0) as percentage
                                  from GET /v11/video_inputs/{id}/ambient_light_sensor_level
"""

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
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
            BoschWifiSignalSensor(coordinator, cam_id, config_entry),
            BoschFirmwareVersionSensor(coordinator, cam_id, config_entry),
            BoschAmbientLightSensor(coordinator, cam_id, config_entry),
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


# ─────────────────────────────────────────────────────────────────────────────
class BoschWifiSignalSensor(_BoschSensorBase):
    """Sensor: WiFi signal strength in percent.

    Data source: GET /v11/video_inputs/{id}/wifiinfo (fetched by coordinator).
    Attributes: ssid, ip_address, mac_address.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name                       = f"Bosch {self._cam_title} WiFi Signal"
        self._attr_unique_id                  = f"bosch_shc_wifi_signal_{cam_id.lower()}"
        self._attr_device_class               = SensorDeviceClass.SIGNAL_STRENGTH
        self._attr_state_class                = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon                       = "mdi:wifi"

    @property
    def native_value(self) -> int | None:
        wifi = self.coordinator._wifiinfo_cache.get(self._cam_id)
        if wifi is None:
            return None
        signal = wifi.get("signalStrength")
        if signal is None:
            return None
        return int(signal)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._wifiinfo_cache.get(self._cam_id) is not None
        )

    @property
    def extra_state_attributes(self) -> dict:
        wifi = self.coordinator._wifiinfo_cache.get(self._cam_id, {})
        return {
            "ssid":        wifi.get("ssid", ""),
            "ip_address":  wifi.get("ipAddress", ""),
            "mac_address": wifi.get("macAddress", ""),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschFirmwareVersionSensor(_BoschSensorBase):
    """Sensor: firmware version string.

    Data source: firmwareVersion field from GET /v11/video_inputs (already in coordinator data).
    Attributes: up_to_date (bool from featureSupport.upToDate or similar field).
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Firmware Version"
        self._attr_unique_id = f"bosch_shc_firmware_{cam_id.lower()}"
        self._attr_icon      = "mdi:chip"

    @property
    def native_value(self) -> str | None:
        info = self._cam_data.get("info", {})
        fw = info.get("firmwareVersion", "")
        return fw if fw else None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and bool(self._cam_data.get("info", {}).get("firmwareVersion", ""))
        )

    @property
    def extra_state_attributes(self) -> dict:
        info = self._cam_data.get("info", {})
        # upToDate may be a top-level field or inside featureSupport
        up_to_date = info.get("upToDate")
        if up_to_date is None:
            up_to_date = info.get("featureSupport", {}).get("upToDate")
        return {
            "up_to_date": up_to_date,
            "hardware_version": info.get("hardwareVersion", ""),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschAmbientLightSensor(_BoschSensorBase):
    """Sensor: ambient light level as a percentage (0–100%).

    Data source: GET /v11/video_inputs/{id}/ambient_light_sensor_level (fetched by coordinator).
    The API returns a float 0.0–1.0 which is converted to 0–100%.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name                       = f"Bosch {self._cam_title} Ambient Light"
        self._attr_unique_id                  = f"bosch_shc_ambient_light_{cam_id.lower()}"
        self._attr_state_class                = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon                       = "mdi:brightness-6"

    @property
    def native_value(self) -> int | None:
        level = self.coordinator._ambient_light_cache.get(self._cam_id)
        if level is None:
            return None
        # Convert 0.0–1.0 float to 0–100 integer percentage
        return round(float(level) * 100)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._ambient_light_cache.get(self._cam_id) is not None
        )
