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
  • {Name} LED Dimmer          — LED dimmer value 0–100% via RCP protocol (0x0c22)
                                  only for cameras with featureSupport.light = True
"""

import logging
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.util import dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
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
            BoschClockOffsetSensor(coordinator, cam_id, config_entry),
            BoschMotionSensitivitySensor(coordinator, cam_id, config_entry),
            BoschAudioAlarmSensor(coordinator, cam_id, config_entry),
            BoschLastEventTypeSensor(coordinator, cam_id, config_entry),
            BoschMovementEventsTodaySensor(coordinator, cam_id, config_entry),
            BoschAudioEventsTodaySensor(coordinator, cam_id, config_entry),
            BoschUnreadEventsCountSensor(coordinator, cam_id, config_entry),
        ])
        # LED Dimmer via RCP — only for cameras with a physical light (featureSupport.light)
        cam_info = coordinator.data[cam_id].get("info", {})
        has_light = cam_info.get("featureSupport", {}).get("light", False)
        if has_light:
            entities.append(BoschLedDimmerSensor(coordinator, cam_id, config_entry))
    # Integration-level sensor: FCM push status (one per integration, not per camera)
    first_cam_id = next(iter(coordinator.data), None)
    if first_cam_id:
        entities.append(BoschFcmPushStatusSensor(coordinator, first_cam_id, config_entry))
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
        today  = dt_util.now().strftime("%Y-%m-%d")
        return sum(1 for ev in events if ev.get("timestamp", "").startswith(today))

    @property
    def extra_state_attributes(self) -> dict:
        events = self._cam_data.get("events", [])
        today  = dt_util.now().strftime("%Y-%m-%d")
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
        attrs = {
            "ssid":        wifi.get("ssid", ""),
            "ip_address":  wifi.get("ipAddress", ""),
            "mac_address": wifi.get("macAddress", ""),
        }
        lan_ip_rcp = self.coordinator.rcp_lan_ip(self._cam_id)
        if lan_ip_rcp:
            attrs["lan_ip_rcp"] = lan_ip_rcp
        ladder = self.coordinator.rcp_bitrate_ladder(self._cam_id)
        if ladder:
            attrs["bitrate_ladder_kbps"] = ladder
            attrs["max_bitrate_kbps"] = max(ladder)
        return attrs


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
        attrs = {
            "up_to_date": up_to_date,
            "hardware_version": info.get("hardwareVersion", ""),
        }
        product_name = self.coordinator.rcp_product_name(self._cam_id)
        if product_name:
            attrs["product_name_rcp"] = product_name
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
class BoschAmbientLightSensor(_BoschSensorBase):
    """Sensor: ambient light level as a percentage (0–100%).
    Disabled by default — enable in HA entity settings if needed.

    Data source: GET /v11/video_inputs/{id}/ambient_light_sensor_level (fetched by coordinator).
    The API returns a float 0.0–1.0 which is converted to 0–100%.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name                              = f"Bosch {self._cam_title} Ambient Light"
        self._attr_unique_id                         = f"bosch_shc_ambient_light_{cam_id.lower()}"
        self._attr_state_class                       = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement        = "%"
        self._attr_icon                              = "mdi:brightness-6"
        self._attr_entity_registry_enabled_default   = False

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


# ─────────────────────────────────────────────────────────────────────────────
class BoschLedDimmerSensor(_BoschSensorBase):
    """Sensor: LED dimmer value 0–100% read via RCP protocol (command 0x0c22).

    Data source: RCP command 0x0c22 (T_WORD) via cloud proxy (rcp.xml).
    Only registered for cameras with featureSupport.light = True.
    State is None (unavailable) when RCP session could not be established.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name                              = f"Bosch {self._cam_title} LED Dimmer"
        self._attr_unique_id                         = f"bosch_shc_led_dimmer_{cam_id.lower()}"
        self._attr_state_class                       = SensorStateClass.MEASUREMENT
        self._attr_native_unit_of_measurement        = "%"
        self._attr_icon                              = "mdi:brightness-6"
        self._attr_entity_registry_enabled_default   = False

    @property
    def native_value(self) -> int | None:
        return self.coordinator._rcp_dimmer_cache.get(self._cam_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._rcp_dimmer_cache.get(self._cam_id) is not None
        )


# ─────────────────────────────────────────────────────────────────────────────
class BoschClockOffsetSensor(_BoschSensorBase):
    """Clock offset between camera internal clock and HA server (seconds)."""

    _attr_icon = "mdi:clock-alert-outline"
    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Clock Offset"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_clock_offset"

    @property
    def native_value(self):
        return self.coordinator.clock_offset(self._cam_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.clock_offset(self._cam_id) is not None
        )

    @property
    def extra_state_attributes(self) -> dict:
        val = self.coordinator.clock_offset(self._cam_id)
        if val is None:
            return {}
        abs_offset = abs(val)
        if abs_offset < 5:
            status = "in_sync"
        elif abs_offset < 60:
            status = "minor_drift"
        else:
            status = "out_of_sync"
        return {
            "offset_seconds": val,
            "status": status,
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschMotionSensitivitySensor(_BoschSensorBase):
    """Shows motion detection enabled state and sensitivity level."""

    _attr_icon = "mdi:motion-sensor"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Motion Sensitivity"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_motion_sensitivity"

    @property
    def native_value(self):
        settings = self.coordinator.motion_settings(self._cam_id)
        if not settings:
            return None
        enabled = settings.get("enabled", False)
        if not enabled:
            return "disabled"
        return settings.get("motionAlarmConfiguration", "UNKNOWN").lower().replace("_", " ")

    @property
    def extra_state_attributes(self) -> dict:
        settings = self.coordinator.motion_settings(self._cam_id)
        if not settings:
            return {}
        return {
            "enabled": settings.get("enabled"),
            "sensitivity": settings.get("motionAlarmConfiguration"),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschAudioAlarmSensor(_BoschSensorBase):
    """Shows audio alarm enabled state and detection threshold."""

    _attr_icon = "mdi:volume-high"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Audio Alarm"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_audio_alarm"

    @property
    def native_value(self):
        settings = self.coordinator.audio_alarm_settings(self._cam_id)
        if not settings:
            return None
        return "enabled" if settings.get("enabled", False) else "disabled"

    @property
    def extra_state_attributes(self) -> dict:
        settings = self.coordinator.audio_alarm_settings(self._cam_id)
        if not settings:
            return {}
        return {
            "enabled": settings.get("enabled"),
            "threshold": settings.get("threshold"),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschLastEventTypeSensor(_BoschSensorBase):
    """Shows the type of the most recent camera event."""

    _attr_icon = "mdi:alert-circle-outline"

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Last Event Type"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_last_event_type"

    @property
    def native_value(self):
        events = self.coordinator.data.get(self._cam_id, {}).get("events", [])
        if not events:
            return "none"
        latest = events[0]
        return latest.get("eventType", "unknown").lower().replace("_", " ")

    @property
    def extra_state_attributes(self) -> dict:
        events = self.coordinator.data.get(self._cam_id, {}).get("events", [])
        if not events:
            return {}
        latest = events[0]
        return {
            "event_type": latest.get("eventType"),
            "timestamp": latest.get("timestamp"),
            "event_id": latest.get("id"),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschMovementEventsTodaySensor(_BoschSensorBase):
    """Number of MOVEMENT events today."""

    _attr_icon = "mdi:run"
    _attr_native_unit_of_measurement = "events"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Movement Events Today"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_movement_events_today"

    @property
    def native_value(self):
        today = dt_util.now().strftime("%Y-%m-%d")
        events = self.coordinator.data.get(self._cam_id, {}).get("events", [])
        return sum(
            1 for e in events
            if e.get("eventType") == "MOVEMENT"
            and (e.get("timestamp") or "").startswith(today)
        )


# ─────────────────────────────────────────────────────────────────────────────
class BoschAudioEventsTodaySensor(_BoschSensorBase):
    """Number of AUDIO_ALARM events today."""

    _attr_icon = "mdi:volume-vibrate"
    _attr_native_unit_of_measurement = "events"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Audio Events Today"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_audio_events_today"

    @property
    def native_value(self):
        today = dt_util.now().strftime("%Y-%m-%d")
        events = self.coordinator.data.get(self._cam_id, {}).get("events", [])
        return sum(
            1 for e in events
            if e.get("eventType") == "AUDIO_ALARM"
            and (e.get("timestamp") or "").startswith(today)
        )


# ─────────────────────────────────────────────────────────────────────────────
class BoschFcmPushStatusSensor(_BoschSensorBase):
    """Shows the event detection method: FCM push (instant) or polling (fallback).

    States:
      - "fcm_push"  — FCM connected and receiving pushes (~2s event detection)
      - "polling"   — FCM disabled or failed, using interval-based polling
      - "disabled"  — FCM push not enabled in options
    """

    _attr_icon = "mdi:bell-ring-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def name(self) -> str:
        return "Bosch Camera Event Detection"

    @property
    def unique_id(self) -> str:
        return "bosch_shc_camera_fcm_push_status"

    @property
    def native_value(self) -> str:
        if not self.coordinator.options.get("enable_fcm_push", False):
            return "disabled"
        if self.coordinator._fcm_healthy:
            return "fcm_push"
        return "polling"

    @property
    def icon(self) -> str:
        val = self.native_value
        if val == "fcm_push":
            return "mdi:bell-ring"
        if val == "polling":
            return "mdi:timer-sand"
        return "mdi:bell-off"

    @property
    def extra_state_attributes(self) -> dict:
        import time as _time
        attrs = {
            "fcm_enabled": self.coordinator.options.get("enable_fcm_push", False),
            "fcm_running": self.coordinator._fcm_running,
            "fcm_healthy": self.coordinator._fcm_healthy,
            "fcm_push_mode": self.coordinator._fcm_push_mode,
            "fcm_push_mode_config": self.coordinator.options.get("fcm_push_mode", "auto"),
        }
        if self.coordinator._fcm_last_push > 0:
            age = _time.monotonic() - self.coordinator._fcm_last_push
            attrs["last_push_seconds_ago"] = round(age)
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
class BoschUnreadEventsCountSensor(_BoschSensorBase):
    """Sensor: number of unread events for this camera.

    Data source: GET /v11/video_inputs/{id}/unread_events_count (fetched by coordinator, slow tier).
    Disabled by default — enable in HA entity settings if needed.
    """

    _attr_icon = "mdi:email-alert"
    _attr_native_unit_of_measurement = "events"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Unread Events"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_unread_events"

    @property
    def native_value(self) -> int | None:
        return self.coordinator._unread_events_cache.get(self._cam_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._unread_events_cache.get(self._cam_id) is not None
        )
