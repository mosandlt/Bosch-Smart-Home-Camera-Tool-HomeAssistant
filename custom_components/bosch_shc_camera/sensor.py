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

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .time_utils import parse_bosch_timestamp


def _event_is_today_local(ts_str: str | None) -> bool:
    """True if a Bosch event timestamp falls on today's *local* calendar date.

    Buckets by the local date of the event's true instant (offset honored),
    not by a naive string prefix — see time_utils. A Bosch
    timestamp already carries the local offset, so its instant maps to the
    correct local day even across the UTC midnight boundary.
    """
    dt_utc = parse_bosch_timestamp(ts_str)
    if dt_utc is None:
        return False
    local_dt: datetime = dt_util.as_local(dt_utc)
    now_local: datetime = dt_util.now()
    return local_dt.date() == now_local.date()


from . import BoschCameraCoordinator, get_options
from .const import CONF_AI_ANALYSIS_ENABLED, CONF_ENABLE_AI_DESCRIPTION, DOMAIN
from .dynamic_devices import register_dynamic_camera_listener

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = (
    0  # coordinator handles all updates; no per-entity parallelism needed
)


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

    coordinator = config_entry.runtime_data

    def _build_entities_for_cam(cam_id: str) -> list[Any]:
        """Every per-camera sensor entity — the SAME logic used for the
        initial setup pass and the dynamic-add listener (Quality-Scale
        Gold `dynamic-devices`). Deliberately excludes the account-level
        entities below (FCM push status / cloud maintenance / feature
        flags) — those are one-per-integration, not one-per-camera, and
        must only ever be added once during the initial pass.
        """
        cam_entities: list[Any] = [
            BoschCameraStatusSensor(coordinator, cam_id, config_entry),
            BoschCameraLastEventSensor(coordinator, cam_id, config_entry),
            BoschCameraEventsTodaySensor(coordinator, cam_id, config_entry),
            BoschWifiSignalSensor(coordinator, cam_id, config_entry),
            BoschFirmwareVersionSensor(coordinator, cam_id, config_entry),
            BoschAmbientLightSensor(coordinator, cam_id, config_entry),
            BoschClockOffsetSensor(coordinator, cam_id, config_entry),
            BoschMotionSensitivitySensor(coordinator, cam_id, config_entry),
            BoschLastEventTypeSensor(coordinator, cam_id, config_entry),
            BoschMovementEventsTodaySensor(coordinator, cam_id, config_entry),
            BoschAudioEventsTodaySensor(coordinator, cam_id, config_entry),
            BoschUnreadEventsCountSensor(coordinator, cam_id, config_entry),
            BoschStreamStatusSensor(coordinator, cam_id, config_entry),
        ]
        # LED Dimmer via RCP — only for cameras with a physical light (featureSupport.light)
        cam_info = coordinator.data.get(cam_id, {}).get("info", {})
        has_light = cam_info.get("featureSupport", {}).get("light", False)
        if has_light:
            cam_entities.append(BoschLedDimmerSensor(coordinator, cam_id, config_entry))
        # Commissioned status (diagnostic, disabled by default)
        cam_entities.append(BoschCommissionedSensor(coordinator, cam_id, config_entry))
        # Cloud rules count (diagnostic, disabled by default)
        cam_entities.append(BoschRulesCountSensor(coordinator, cam_id, config_entry))
        # RCP deep-dive sensors (diagnostic, disabled by default)
        cam_entities.append(BoschAlarmCatalogSensor(coordinator, cam_id, config_entry))
        cam_entities.append(BoschMotionZonesSensor(coordinator, cam_id, config_entry))
        cam_entities.append(BoschPrivateAreasSensor(coordinator, cam_id, config_entry))
        cam_entities.append(BoschTlsCertSensor(coordinator, cam_id, config_entry))
        cam_entities.append(
            BoschNetworkServicesSensor(coordinator, cam_id, config_entry)
        )
        cam_entities.append(BoschIvaCatalogSensor(coordinator, cam_id, config_entry))
        # Gen2-only sensors
        from .models import get_model_config as _gmc_setup

        hw_setup = cam_info.get("hardwareVersion", "")
        if _gmc_setup(hw_setup).generation >= 2:
            # Ambient-light schedule is Outdoor-only (Indoor II has no RGB lights)
            if hw_setup not in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
                cam_entities.append(
                    BoschAmbientLightScheduleSensor(coordinator, cam_id, config_entry)
                )
        # Gen2 Indoor II — alarm state sensor
        if hw_setup in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
            cam_entities.append(
                BoschAlarmStateSensor(coordinator, cam_id, config_entry)
            )
        # F4: ONVIF scopes sensor (LAN, disabled by default)
        cam_entities.append(BoschOnvifScopesSensor(coordinator, cam_id, config_entry))
        # F6: RCP protocol version sensor (LAN, disabled by default)
        cam_entities.append(BoschRcpVersionSensor(coordinator, cam_id, config_entry))
        # Mini-NVR diagnostic sensor — disabled by default. One per camera.
        if opts.get("enable_nvr", False):
            cam_entities.append(BoschNvrStateSensor(coordinator, cam_id, config_entry))
        # AI Snapshot Description sensor — only when option is enabled
        if opts.get(CONF_ENABLE_AI_DESCRIPTION, False):
            cam_entities.append(
                BoschCameraAiDescriptionSensor(coordinator, cam_id, config_entry)
            )
        # AI Camera Analysis sensors — only when the master option is enabled
        if opts.get(CONF_AI_ANALYSIS_ENABLED, False):
            cam_entities.append(
                BoschAiAlertScoreSensor(coordinator, cam_id, config_entry)
            )
            cam_entities.append(
                BoschAiAlerts24hSensor(coordinator, cam_id, config_entry)
            )
        # External stream URL sensors (main + sub). Per-camera, always
        # registered so the BoschExternalStreamSwitch can toggle their
        # value without dynamic entity (re-)registration. Disabled in
        # entity registry by default; surfaced only when the user enables
        # them for a specific camera.
        cam_entities.append(BoschStreamUrlSensor(coordinator, cam_id, config_entry))
        cam_entities.append(BoschStreamUrlSubSensor(coordinator, cam_id, config_entry))
        cam_entities.append(
            BoschFrigateUrlHighSensor(coordinator, cam_id, config_entry)
        )
        cam_entities.append(BoschFrigateUrlLowSensor(coordinator, cam_id, config_entry))
        return cam_entities

    known_cam_ids: set[str] = set(coordinator.data)
    entities: list[Any] = []
    for cam_id in known_cam_ids:
        entities.extend(_build_entities_for_cam(cam_id))
    # Integration-level sensor: FCM push status (one per integration, not per camera)
    first_cam_id = next(iter(coordinator.data), None)
    if first_cam_id:
        entities.append(
            BoschFcmPushStatusSensor(coordinator, first_cam_id, config_entry)
        )
        # Bosch community-RSS-derived maintenance window (one per integration).
        # Stays available even when the cloud is unreachable — that is the
        # scenario it exists for.
        entities.append(
            BoschCloudMaintenanceSensor(coordinator, first_cam_id, config_entry)
        )
        # F13: Cloud feature-flags sensor (account-level, one per integration, disabled by default)
        entities.append(
            BoschCloudFeatureFlagsSensor(coordinator, first_cam_id, config_entry)
        )
    async_add_entities(entities, update_before_add=False)

    # Quality-Scale Gold `dynamic-devices`: a camera added to the Bosch
    # account after HA startup gets its per-camera sensors added
    # automatically on the next coordinator tick, instead of requiring an
    # integration reload. Account-level entities (FCM push status / cloud
    # maintenance / feature flags, added above) are deliberately NOT part
    # of `_build_entities_for_cam` and so never get re-added here.
    config_entry.async_on_unload(
        register_dynamic_camera_listener(
            coordinator, known_cam_ids, async_add_entities, _build_entities_for_cam
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
class _BoschSensorBase(CoordinatorEntity, SensorEntity):  # type: ignore[misc]
    """Shared base for all Bosch camera sensors."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
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

    @property
    def _cam_data(self) -> dict[str, Any]:
        return self.coordinator.data.get(self._cam_id, {})  # type: ignore[no-any-return]

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model": self._model_name,
            "sw_version": self._fw,
            "connections": {("mac", self._mac)} if self._mac else set(),
        }


# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, kw_only=True)
class BoschSensorEntityDescription(SensorEntityDescription):  # type: ignore[misc]
    """Describes a Bosch camera sensor.

    Mirrors the `EntityDescription`-driven pattern already used by
    `binary_sensor.py`'s `BoschBinarySensorEntityDescription` (see that
    file's pilot commit for the full rationale): structurally-identical
    sensors — single-field cache lookups, formulaic `available` checks,
    small `extra_state_attributes` dicts — share one generic entity class
    (`BoschSensorEntity` below) parametrized by a description instance,
    instead of one hand-written subclass per entity. Sensors with genuinely
    distinct logic (multi-source aggregation like the motion-zone/privacy-
    mask priority merging, complex multi-branch state derivation like the
    camera-status/stream-status sensors, or entities that already share a
    dedicated base class of their own like the stream-URL/Frigate-URL
    sensors) stay as their own subclasses outside this pattern — the same
    hybrid judgment call the binary_sensor pilot made, not a mechanical
    one-size-fits-all collapse.

    `unique_id_fn` takes the camera id and returns the full unique_id —
    kept as a function (not a prefix/suffix pair) because the pre-existing
    unique_id schemes in this file are genuinely inconsistent (some
    lowercase the cam_id, some don't; prefixes vary between
    `bosch_shc_camera_`, `bosch_shc_status_`, `bosch_shc_last_event_`, and a
    few account-level sensors ignore cam_id entirely) — preserved verbatim
    so existing users' entities are never orphaned by this refactor.

    `value_fn`/`extra_attrs_fn`/`available_fn` are optional callables run
    against the entity instance (so they can reach `self._cam_id`,
    `self.coordinator`, etc., exactly like the concrete subclasses did
    before this refactor). `available_fn=None` falls back to the
    `CoordinatorEntity` default (`coordinator.last_update_success`) —
    identical to the sensors that never overrode `available` at all.
    `extra_attrs_fn=None` means "no extra_state_attributes", matching the
    sensors that never defined that property.
    """

    unique_id_fn: Callable[[str], str]
    value_fn: Callable[[BoschSensorEntity], Any] | None = None
    extra_attrs_fn: Callable[[BoschSensorEntity], dict[str, Any]] | None = None
    available_fn: Callable[[BoschSensorEntity], bool] | None = None


class BoschSensorEntity(_BoschSensorBase):
    """Generic Bosch camera sensor driven by a `BoschSensorEntityDescription`.

    See `BoschSensorEntityDescription` above for the full design rationale.
    """

    entity_description: BoschSensorEntityDescription

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
        description: BoschSensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self.entity_description = description
        self._attr_unique_id = description.unique_id_fn(cam_id)
        if description.translation_key is not None:
            self._attr_translation_key = description.translation_key
        if description.device_class is not None:
            self._attr_device_class = description.device_class
        if description.entity_category is not None:
            self._attr_entity_category = description.entity_category
        if description.native_unit_of_measurement is not None:
            self._attr_native_unit_of_measurement = (
                description.native_unit_of_measurement
            )
        if description.state_class is not None:
            self._attr_state_class = description.state_class
        if description.options is not None:
            self._attr_options = description.options
        self._attr_entity_registry_enabled_default = (
            description.entity_registry_enabled_default
        )

    @property
    def native_value(self) -> Any:
        if self.entity_description.value_fn is None:
            raise NotImplementedError
        return self.entity_description.value_fn(self)

    @property
    def available(self) -> bool:
        if self.entity_description.available_fn is None:
            return self.coordinator.last_update_success  # type: ignore[no-any-return]
        return self.entity_description.available_fn(self)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.entity_description.extra_attrs_fn is None:
            return {}
        return self.entity_description.extra_attrs_fn(self)


# ─────────────────────────────────────────────────────────────────────────────
_STATUS_SENSOR_OPTIONS: list[str] = [
    "online",
    "offline",
    "updating",
    "session_limit",
    "unknown",
]


class BoschCameraStatusSensor(_BoschSensorBase):
    """Sensor: online / offline / updating / unknown.

    `updating` takes precedence over online/offline because the camera
    reboots during a firmware install and any cloud "online" reading is
    cached from before the reboot. Dashboard auto-entities and automations
    can use this single sensor to drive both visibility and alerting.
    """

    _attr_options: ClassVar[list[str]] = _STATUS_SENSOR_OPTIONS
    _attr_device_class = SensorDeviceClass.ENUM

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_status_{cam_id.lower()}"
        self._attr_translation_key = "status"

    @property
    def native_value(self) -> str:
        # Firmware install in progress trumps the cloud-cached status —
        # the camera is rebooting and dependent entities should reflect that.
        is_updating = getattr(self.coordinator, "is_updating", None)
        if is_updating is not None and is_updating(self._cam_id):
            return "updating"
        raw = str(self._cam_data.get("status", "UNKNOWN")).lower()
        if raw == "online":
            events = self._cam_data.get("events", [])
            if (
                events
                and str(events[0].get("eventType", "")).upper() == "TROUBLE_DISCONNECT"
            ):
                return "offline"
        # session_limit: HTTP 444 — not offline, just too many concurrent sessions
        if raw == "session_limit":
            return "session_limit"
        return raw if raw in _STATUS_SENSOR_OPTIONS else "unknown"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = self._cam_data.get("info", {})
        comm = self.coordinator.commissioned_cache.get(self._cam_id, {})
        fw = self.coordinator.firmware_cache.get(self._cam_id, {})
        attrs: dict[str, Any] = {
            "camera_id": self._cam_id,
            "model": info.get("hardwareVersion", ""),
            "firmware": info.get("firmwareVersion", ""),
            "mac": info.get("macAddress", ""),
        }
        if comm:
            attrs["configured"] = comm.get("configured")
            attrs["connected"] = comm.get("connected")
            attrs["commissioned"] = comm.get("commissioned")
        if fw:
            attrs["firmware_updating"] = fw.get("updating", False)
            attrs["firmware_update_status"] = fw.get("status", "")
            attrs["firmware_up_to_date"] = fw.get("upToDate", True)
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraLastEventSensor(_BoschSensorBase):
    """Sensor: datetime of the most recent motion event."""

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_last_event_{cam_id.lower()}"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_translation_key = "last_event"

    @property
    def native_value(self) -> datetime | None:
        events = self._cam_data.get("events", [])
        if not events:
            return None
        ts_str = events[0].get("timestamp", "")
        if not ts_str:
            return None
        # Honor the offset Bosch sends ("+02:00" or "Z"); do NOT truncate it
        # away and re-label as UTC — that shifted the value +2h in CEST (#34).
        dt_utc = parse_bosch_timestamp(ts_str)
        if dt_utc is None:
            return None
        local_dt: datetime = dt_util.as_local(dt_utc)
        return local_dt

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        events = self._cam_data.get("events", [])
        latest = events[0] if events else {}
        return {
            "event_type": latest.get("eventType", ""),
            "event_id": latest.get("id", "")[:8],
            "has_image": bool(latest.get("imageUrl")),
            "has_clip": bool(latest.get("videoClipUrl")),
            "clip_status": latest.get("videoClipUploadStatus", ""),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraEventsTodaySensor(_BoschSensorBase):
    """Sensor: count of motion events that occurred today."""

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_events_today_{cam_id.lower()}"
        self._attr_native_unit_of_measurement = "events"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_translation_key = "events_today"

    @property
    def native_value(self) -> int:
        events = self._cam_data.get("events", [])
        return sum(1 for ev in events if _event_is_today_local(ev.get("timestamp")))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        events = self._cam_data.get("events", [])
        today_events = [
            ev for ev in events if _event_is_today_local(ev.get("timestamp"))
        ]
        return {
            "events_in_feed": len(events),
            "latest_timestamps": [
                ev.get("timestamp", "")[:19] for ev in today_events[:5]
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
def _wifi_signal_value(entity: BoschSensorEntity) -> int | None:
    wifi = entity.coordinator.wifiinfo_cache.get(entity._cam_id)
    if wifi is None:
        return None
    signal = wifi.get("signalStrength")
    if signal is None:
        return None
    return int(signal)


def _wifi_signal_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.wifiinfo_cache.get(entity._cam_id) is not None
    )


def _wifi_signal_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    wifi = entity.coordinator.wifiinfo_cache.get(entity._cam_id, {})
    attrs: dict[str, Any] = {
        "ssid": wifi.get("ssid", ""),
        "ip_address": wifi.get("ipAddress", ""),
        "mac_address": wifi.get("macAddress", ""),
    }
    lan_ip_rcp = entity.coordinator.rcp_lan_ip(entity._cam_id)
    if lan_ip_rcp:
        attrs["lan_ip_rcp"] = lan_ip_rcp
    ladder = entity.coordinator.rcp_bitrate_ladder(entity._cam_id)
    if ladder:
        attrs["bitrate_ladder_kbps"] = ladder
        attrs["max_bitrate_kbps"] = max(ladder)
    return attrs


WIFI_SIGNAL_DESCRIPTION = BoschSensorEntityDescription(
    key="wifi_signal",
    translation_key="wifi_signal",
    entity_category=EntityCategory.DIAGNOSTIC,
    # No device_class — Bosch API returns percentage (0-100), not dBm
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="%",
    unique_id_fn=lambda cam_id: f"bosch_shc_wifi_signal_{cam_id.lower()}",
    value_fn=_wifi_signal_value,
    available_fn=_wifi_signal_available,
    extra_attrs_fn=_wifi_signal_attrs,
)


class BoschWifiSignalSensor(BoschSensorEntity):
    """Sensor: WiFi signal strength in percent.

    Data source: GET /v11/video_inputs/{id}/wifiinfo (fetched by coordinator).
    Attributes: ssid, ip_address, mac_address.
    """

    entity_description = WIFI_SIGNAL_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, WIFI_SIGNAL_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _firmware_version_value(entity: BoschSensorEntity) -> str | None:
    info = entity._cam_data.get("info", {})
    fw = info.get("firmwareVersion", "")
    return fw if fw else None


def _firmware_version_available(entity: BoschSensorEntity) -> bool:
    return entity.coordinator.last_update_success and bool(
        entity._cam_data.get("info", {}).get("firmwareVersion", "")
    )


def _firmware_version_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    info = entity._cam_data.get("info", {})
    # upToDate may be a top-level field or inside featureSupport
    up_to_date = info.get("upToDate")
    if up_to_date is None:
        up_to_date = info.get("featureSupport", {}).get("upToDate")
    attrs: dict[str, Any] = {
        "up_to_date": up_to_date,
        "hardware_version": info.get("hardwareVersion", ""),
    }
    product_name = entity.coordinator.rcp_product_name(entity._cam_id)
    if product_name:
        attrs["product_name_rcp"] = product_name
    return attrs


FIRMWARE_VERSION_DESCRIPTION = BoschSensorEntityDescription(
    key="firmware_version",
    translation_key="firmware_version",
    entity_category=EntityCategory.DIAGNOSTIC,
    unique_id_fn=lambda cam_id: f"bosch_shc_firmware_{cam_id.lower()}",
    value_fn=_firmware_version_value,
    available_fn=_firmware_version_available,
    extra_attrs_fn=_firmware_version_attrs,
)


class BoschFirmwareVersionSensor(BoschSensorEntity):
    """Sensor: firmware version string.

    Data source: firmwareVersion field from GET /v11/video_inputs (already in coordinator data).
    Attributes: up_to_date (bool from featureSupport.upToDate or similar field).
    """

    entity_description = FIRMWARE_VERSION_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, FIRMWARE_VERSION_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _ambient_light_value(entity: BoschSensorEntity) -> int | None:
    level = entity.coordinator.ambient_light_cache.get(entity._cam_id)
    if level is None:
        return None
    # Convert 0.0–1.0 float to 0–100 integer percentage
    return round(float(level) * 100)


def _ambient_light_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.ambient_light_cache.get(entity._cam_id) is not None
    )


AMBIENT_LIGHT_DESCRIPTION = BoschSensorEntityDescription(
    key="ambient_light",
    translation_key="ambient_light",
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="%",
    unique_id_fn=lambda cam_id: f"bosch_shc_ambient_light_{cam_id.lower()}",
    value_fn=_ambient_light_value,
    available_fn=_ambient_light_available,
)


class BoschAmbientLightSensor(BoschSensorEntity):
    """Sensor: ambient light level as a percentage (0–100%).

    Data source: GET /v11/video_inputs/{id}/ambient_light_sensor_level (fetched by coordinator).
    The API returns a float 0.0–1.0 which is converted to 0–100%.
    """

    entity_description = AMBIENT_LIGHT_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, AMBIENT_LIGHT_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _led_dimmer_value(entity: BoschSensorEntity) -> int | None:
    return entity.coordinator.rcp_dimmer_cache.get(entity._cam_id)  # type: ignore[no-any-return]


def _led_dimmer_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rcp_dimmer_cache.get(entity._cam_id) is not None
    )


LED_DIMMER_DESCRIPTION = BoschSensorEntityDescription(
    key="led_dimmer",
    translation_key="led_dimmer",
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="%",
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_led_dimmer_{cam_id.lower()}",
    value_fn=_led_dimmer_value,
    available_fn=_led_dimmer_available,
)


class BoschLedDimmerSensor(BoschSensorEntity):
    """Sensor: LED dimmer value 0–100% read via RCP protocol (command 0x0c22).

    Data source: RCP command 0x0c22 (T_WORD) via cloud proxy (rcp.xml).
    Only registered for cameras with featureSupport.light = True.
    State is None (unavailable) when RCP session could not be established.
    """

    entity_description = LED_DIMMER_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, LED_DIMMER_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _clock_offset_value(entity: BoschSensorEntity) -> float | None:
    return entity.coordinator.clock_offset(entity._cam_id)  # type: ignore[no-any-return]


def _clock_offset_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.clock_offset(entity._cam_id) is not None
    )


def _clock_offset_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    val = entity.coordinator.clock_offset(entity._cam_id)
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


CLOCK_OFFSET_DESCRIPTION = BoschSensorEntityDescription(
    key="clock_offset",
    translation_key="clock_offset",
    native_unit_of_measurement="s",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_clock_offset",
    value_fn=_clock_offset_value,
    available_fn=_clock_offset_available,
    extra_attrs_fn=_clock_offset_attrs,
)


class BoschClockOffsetSensor(BoschSensorEntity):
    """Clock offset between camera internal clock and HA server (seconds)."""

    entity_description = CLOCK_OFFSET_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, CLOCK_OFFSET_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _motion_sensitivity_value(entity: BoschSensorEntity) -> str | None:
    settings = entity.coordinator.motion_settings(entity._cam_id)
    if not settings:
        return None
    enabled = settings.get("enabled", False)
    if not enabled:
        return "disabled"
    return (
        str(settings.get("motionAlarmConfiguration", "UNKNOWN"))
        .lower()
        .replace("_", " ")
    )


def _motion_sensitivity_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    settings = entity.coordinator.motion_settings(entity._cam_id)
    if not settings:
        return {}
    return {
        "enabled": settings.get("enabled"),
        "sensitivity": settings.get("motionAlarmConfiguration"),
    }


MOTION_SENSITIVITY_DESCRIPTION = BoschSensorEntityDescription(
    key="motion_sensitivity",
    translation_key="motion_sensitivity",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_motion_sensitivity",
    value_fn=_motion_sensitivity_value,
    extra_attrs_fn=_motion_sensitivity_attrs,
)


class BoschMotionSensitivitySensor(BoschSensorEntity):
    """Shows motion detection enabled state and sensitivity level."""

    entity_description = MOTION_SENSITIVITY_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, MOTION_SENSITIVITY_DESCRIPTION)

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Motion Sensitivity"


# ─────────────────────────────────────────────────────────────────────────────
_LAST_EVENT_TYPE_OPTIONS: list[str] = [
    "movement",
    "person",
    "audio_alarm",
    "trouble",
    "trouble_disconnect",
    "trouble_reconnect",
    "trouble_connect",
    "none",
]


def _last_event_type_value(entity: BoschSensorEntity) -> str:
    events = entity.coordinator.data.get(entity._cam_id, {}).get("events", [])
    if not events:
        return "none"
    event_type = str(events[0].get("eventType", "")).lower()
    # ENUM device_class rejects any value outside _attr_options (HA logs a
    # state-validation warning and drops the state), so map a missing or
    # unrecognised event shape onto the "none" catch-all instead.
    return event_type if event_type in _LAST_EVENT_TYPE_OPTIONS else "none"


def _last_event_type_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    events = entity.coordinator.data.get(entity._cam_id, {}).get("events", [])
    if not events:
        return {}
    latest = events[0]
    return {
        "event_type": latest.get("eventType"),
        "timestamp": latest.get("timestamp"),
        "event_id": latest.get("id"),
    }


LAST_EVENT_TYPE_DESCRIPTION = BoschSensorEntityDescription(
    key="last_event_type",
    translation_key="last_event_type",
    options=_LAST_EVENT_TYPE_OPTIONS,
    device_class=SensorDeviceClass.ENUM,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_last_event_type",
    value_fn=_last_event_type_value,
    extra_attrs_fn=_last_event_type_attrs,
)


class BoschLastEventTypeSensor(BoschSensorEntity):
    """Shows the type of the most recent camera event."""

    entity_description = LAST_EVENT_TYPE_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, LAST_EVENT_TYPE_DESCRIPTION)

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Last Event Type"


# ─────────────────────────────────────────────────────────────────────────────
def _movement_events_today_value(entity: BoschSensorEntity) -> int:
    events = entity.coordinator.data.get(entity._cam_id, {}).get("events", [])
    return sum(
        1
        for e in events
        if e.get("eventType") == "MOVEMENT"
        and _event_is_today_local(e.get("timestamp"))
    )


MOVEMENT_EVENTS_TODAY_DESCRIPTION = BoschSensorEntityDescription(
    key="movement_events_today",
    translation_key="movement_events_today",
    native_unit_of_measurement="events",
    state_class=SensorStateClass.MEASUREMENT,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_movement_events_today",
    value_fn=_movement_events_today_value,
)


class BoschMovementEventsTodaySensor(BoschSensorEntity):
    """Number of MOVEMENT events today."""

    entity_description = MOVEMENT_EVENTS_TODAY_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, MOVEMENT_EVENTS_TODAY_DESCRIPTION)

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Movement Events Today"


# ─────────────────────────────────────────────────────────────────────────────
def _audio_events_today_value(entity: BoschSensorEntity) -> int:
    events = entity.coordinator.data.get(entity._cam_id, {}).get("events", [])
    return sum(
        1
        for e in events
        if e.get("eventType") == "AUDIO_ALARM"
        and _event_is_today_local(e.get("timestamp"))
    )


AUDIO_EVENTS_TODAY_DESCRIPTION = BoschSensorEntityDescription(
    key="audio_events_today",
    translation_key="audio_events_today",
    native_unit_of_measurement="events",
    state_class=SensorStateClass.MEASUREMENT,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_audio_events_today",
    value_fn=_audio_events_today_value,
)


class BoschAudioEventsTodaySensor(BoschSensorEntity):
    """Number of AUDIO_ALARM events today."""

    entity_description = AUDIO_EVENTS_TODAY_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, AUDIO_EVENTS_TODAY_DESCRIPTION)

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Audio Events Today"


# ─────────────────────────────────────────────────────────────────────────────
def _fcm_push_status_value(entity: BoschSensorEntity) -> str:
    if not entity.coordinator.options.get("enable_fcm_push", False):
        return "disabled"
    if entity.coordinator.fcm_healthy:
        return "fcm_push"
    return "polling"


def _fcm_push_status_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    import time as _time

    attrs: dict[str, Any] = {
        "fcm_enabled": entity.coordinator.options.get("enable_fcm_push", False),
        "fcm_running": entity.coordinator.fcm_running,
        "fcm_healthy": entity.coordinator.fcm_healthy,
        "fcm_push_mode": entity.coordinator.fcm_push_mode,
        "fcm_push_mode_config": entity.coordinator.options.get("fcm_push_mode", "auto"),
    }
    if entity.coordinator.fcm_last_push != float("-inf"):
        age = _time.monotonic() - entity.coordinator.fcm_last_push
        attrs["last_push_seconds_ago"] = round(age)
    return attrs


FCM_PUSH_STATUS_DESCRIPTION = BoschSensorEntityDescription(
    key="push_status",
    translation_key="push_status",
    entity_category=EntityCategory.DIAGNOSTIC,
    options=["fcm_push", "polling", "disabled"],
    device_class=SensorDeviceClass.ENUM,
    unique_id_fn=lambda cam_id: "bosch_shc_camera_fcm_push_status",
    value_fn=_fcm_push_status_value,
    extra_attrs_fn=_fcm_push_status_attrs,
)


class BoschFcmPushStatusSensor(BoschSensorEntity):
    """Shows the event detection method: FCM push (instant) or polling (fallback).

    States:
      - "fcm_push"  — FCM connected and receiving pushes (~2s event detection)
      - "polling"   — FCM disabled or failed, using interval-based polling
      - "disabled"  — FCM push not enabled in options
    """

    # `last_push_seconds_ago` is recomputed from a monotonic clock on every
    # property read, so it changes on every coordinator tick even while the
    # state stays "fcm_push". Recording it spawns a fresh `state_attributes`
    # row each tick and bloats the DB (HA#39). Keep it visible live, but never
    # historize it. See https://developers.home-assistant.io/blog/2023/09/20/
    # excluding-state-attributes-from-recording/.
    _unrecorded_attributes = frozenset({"last_push_seconds_ago"})

    entity_description = FCM_PUSH_STATUS_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, FCM_PUSH_STATUS_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _cloud_maintenance_value(entity: BoschSensorEntity) -> str:
    window = entity.coordinator.maintenance_cache
    return window.state() if window else "idle"


def _cloud_maintenance_available(entity: BoschSensorEntity) -> bool:
    # Intentionally always True: the sensor must remain readable while the
    # Bosch cloud is down, since that is precisely when users look at it.
    return True


def _cloud_maintenance_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    import time as _time

    window = entity.coordinator.maintenance_cache
    attrs: dict[str, Any] = {}
    if window is not None:
        attrs.update(window.as_dict())
    last_fetch = entity.coordinator.maintenance_last_fetch
    if last_fetch != float("-inf"):
        attrs["last_fetched_seconds_ago"] = round(_time.monotonic() - last_fetch)
    return attrs


CLOUD_MAINTENANCE_DESCRIPTION = BoschSensorEntityDescription(
    key="cloud_maintenance",
    translation_key="cloud_maintenance",
    entity_category=EntityCategory.DIAGNOSTIC,
    options=["active", "scheduled", "past", "recent", "unknown", "idle"],
    device_class=SensorDeviceClass.ENUM,
    unique_id_fn=lambda cam_id: "bosch_shc_camera_cloud_maintenance",
    value_fn=_cloud_maintenance_value,
    available_fn=_cloud_maintenance_available,
    extra_attrs_fn=_cloud_maintenance_attrs,
)


class BoschCloudMaintenanceSensor(BoschSensorEntity):
    """Surfaces Bosch's announced maintenance / incident state for the cloud.

    Data source: community.bosch-smarthome.com Wartungsarbeiten + Statusmeldungen
    RSS feeds, fetched by the coordinator (see `maintenance.py`). One per
    integration. Stays available even when the Bosch cloud itself is down —
    that is the entire point: the user needs a stable place to see WHY their
    cameras are unavailable.
    """

    # `last_fetched_seconds_ago` is monotonic-derived → changes every tick.
    # Keep it live but unrecorded so it does not bloat `state_attributes`
    # (HA#39). The stable window fields (title/link/dates) stay recorded.
    _unrecorded_attributes = frozenset({"last_fetched_seconds_ago"})

    entity_description = CLOUD_MAINTENANCE_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, CLOUD_MAINTENANCE_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _unread_events_value(entity: BoschSensorEntity) -> int | None:
    return entity.coordinator.unread_events_cache.get(entity._cam_id)  # type: ignore[no-any-return]


def _unread_events_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.unread_events_cache.get(entity._cam_id) is not None
    )


UNREAD_EVENTS_DESCRIPTION = BoschSensorEntityDescription(
    key="unread_events",
    translation_key="unread_events",
    native_unit_of_measurement="events",
    state_class=SensorStateClass.MEASUREMENT,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_unread_events",
    value_fn=_unread_events_value,
    available_fn=_unread_events_available,
)


class BoschUnreadEventsCountSensor(BoschSensorEntity):
    """Sensor: number of unread events for this camera.

    Data source: GET /v11/video_inputs/{id}/unread_events_count (fetched by coordinator, slow tier).
    Disabled by default — enable in HA entity settings if needed.
    """

    entity_description = UNREAD_EVENTS_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, UNREAD_EVENTS_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _commissioned_value(entity: BoschSensorEntity) -> str | None:
    data = entity.coordinator.commissioned_cache.get(entity._cam_id)
    if data is None:
        return None
    if not data.get("connected", False):
        return "not_connected"
    if data.get("commissioned", False):
        return "commissioned"
    return "not_commissioned"


def _commissioned_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.commissioned_cache.get(entity._cam_id) is not None
    )


def _commissioned_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    data = entity.coordinator.commissioned_cache.get(entity._cam_id)
    if not data:
        return {}
    return {
        "configured": data.get("configured"),
        "connected": data.get("connected"),
        "commissioned": data.get("commissioned"),
    }


COMMISSIONED_DESCRIPTION = BoschSensorEntityDescription(
    key="commissioned",
    translation_key="commissioned",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    options=["commissioned", "not_commissioned", "not_connected"],
    device_class=SensorDeviceClass.ENUM,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_commissioned",
    value_fn=_commissioned_value,
    available_fn=_commissioned_available,
    extra_attrs_fn=_commissioned_attrs,
)


class BoschCommissionedSensor(BoschSensorEntity):
    """Sensor: commissioned status from GET /v11/video_inputs/{id}/commissioned.

    Response: {"configured": true, "connected": true, "commissioned": true}
    Displays: "Commissioned" / "Not commissioned" / "Not connected"
    Diagnostic, disabled by default.
    """

    entity_description = COMMISSIONED_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, COMMISSIONED_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _rules_count_value(entity: BoschSensorEntity) -> int | None:
    rules = entity.coordinator.rules_cache.get(entity._cam_id)
    if rules is None:
        return None
    return len(rules)


def _rules_count_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rules_cache.get(entity._cam_id) is not None
    )


def _rules_count_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    rules = entity.coordinator.rules_cache.get(entity._cam_id, [])
    return {
        "rules": [
            {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "active": r.get("isActive", False),
                "start": r.get("startTime", ""),
                "end": r.get("endTime", ""),
                "weekdays": r.get("weekdays", []),
            }
            for r in rules
        ],
    }


RULES_COUNT_DESCRIPTION = BoschSensorEntityDescription(
    key="schedule_rules",
    translation_key="schedule_rules",
    native_unit_of_measurement="rules",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_rules_count",
    value_fn=_rules_count_value,
    available_fn=_rules_count_available,
    extra_attrs_fn=_rules_count_attrs,
)


class BoschRulesCountSensor(BoschSensorEntity):
    """Sensor: number of cloud-side schedule rules for this camera.

    Data source: GET /v11/video_inputs/{id}/rules (fetched by coordinator, slow tier).
    Attributes: list of rule names and active status.
    """

    # `rules` is a list of rule dicts purely for card display; recording it
    # spends a large `state_attributes` blob with zero history value (HA#39).
    _unrecorded_attributes = frozenset({"rules"})

    entity_description = RULES_COUNT_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, RULES_COUNT_DESCRIPTION)


# ── RCP Deep Dive Sensors ───────────────────────────────────────────────────


def _alarm_catalog_value(entity: BoschSensorEntity) -> int | None:
    alarms = entity.coordinator.rcp_alarm_catalog_cache.get(entity._cam_id)
    if alarms is None:
        return None
    return len(alarms)


def _alarm_catalog_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rcp_alarm_catalog_cache.get(entity._cam_id) is not None
    )


def _alarm_catalog_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    alarms = entity.coordinator.rcp_alarm_catalog_cache.get(entity._cam_id, [])
    return {
        "alarm_types": [a["name"] for a in alarms],
        "alarm_details": alarms,
        "categories": list({a["type"] for a in alarms}),
    }


ALARM_CATALOG_DESCRIPTION = BoschSensorEntityDescription(
    key="alarm_catalog",
    translation_key="alarm_catalog",
    native_unit_of_measurement="types",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_alarm_catalog",
    value_fn=_alarm_catalog_value,
    available_fn=_alarm_catalog_available,
    extra_attrs_fn=_alarm_catalog_attrs,
)


class BoschAlarmCatalogSensor(BoschSensorEntity):
    """Sensor: alarm types supported by camera firmware (RCP 0x0c38).

    Displays count of supported alarm types. Attributes list all types
    with name and category (virtual, flame, smoke, audio, motion, etc.).
    """

    # `alarm_details` duplicates the full RCP catalog as a big list; keep the
    # small `alarm_types`/`categories` recorded but never the blob (HA#39).
    _unrecorded_attributes = frozenset({"alarm_details"})

    entity_description = ALARM_CATALOG_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, ALARM_CATALOG_DESCRIPTION)


class BoschMotionZonesSensor(_BoschSensorBase):
    """Sensor: motion detection zones (Cloud API + RCP + Gen2 polygon zones).

    Displays total number of zones across all sources.
    Attributes contain zone data for overlay visualization:
      - cloud_zones: Gen1 rectangular zones (x/y/w/h normalized 0.0–1.0)
      - gen2_zones: Gen2 polygon zones (points array, trigger, color)
      - zones/coordinates: RCP firmware data (fallback)
    """

    _attr_native_unit_of_measurement = "zones"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Coordinate lists for card overlay only — never historize the blobs
    # (HA#39). The *_count fields stay recorded.
    _unrecorded_attributes = frozenset(
        {"zones", "coordinates", "cloud_zones", "gen2_zones"}
    )
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_motion_zones"
        self._attr_translation_key = "motion_zones"

    @property
    def native_value(self) -> int | None:
        # Unlike every sibling diagnostic sensor in this file
        # (BoschRulesCountSensor, BoschAlarmCatalogSensor, etc.), a naive
        # `[]` default for every cache lookup would report a confirmed
        # "0 zones" state (with the misleading "No motion zones configured"
        # attribute note) instead of unknown/unavailable during the window
        # before the first successful fetch, or on a camera where it never
        # succeeds. Distinguish "not yet fetched" (None) from "fetched,
        # zero zones" ([]) per source.
        gen2_zones = self.coordinator.gen2_zones_cache.get(self._cam_id)
        cloud_zones = self.coordinator.cloud_zones_cache.get(self._cam_id)
        zones = self.coordinator.rcp_motion_zones_cache.get(self._cam_id)
        if gen2_zones is None and cloud_zones is None and zones is None:
            return None
        # Gen2 polygon zones take priority
        if gen2_zones:
            return len(gen2_zones)
        # Then cloud zones (Gen1 rectangles)
        if cloud_zones:
            return len(cloud_zones)
        # Fallback to RCP
        return len(zones or [])

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and (
            self.coordinator.gen2_zones_cache.get(self._cam_id) is not None
            or self.coordinator.cloud_zones_cache.get(self._cam_id) is not None
            or self.coordinator.rcp_motion_zones_cache.get(self._cam_id) is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        zones = self.coordinator.rcp_motion_zones_cache.get(self._cam_id, [])
        coords = self.coordinator.rcp_motion_coords_cache.get(self._cam_id, [])
        cloud_zones = self.coordinator.cloud_zones_cache.get(self._cam_id, [])
        gen2_zones = self.coordinator.gen2_zones_cache.get(self._cam_id, [])
        attrs: dict[str, Any] = {
            "zones": zones,
            "coordinates": coords,
            "coordinate_count": len(coords),
            "cloud_zones": cloud_zones,
            "cloud_zone_count": len(cloud_zones),
            "gen2_zones": gen2_zones,
            "gen2_zone_count": len(gen2_zones),
        }
        total = len(gen2_zones) or len(cloud_zones) or len(zones)
        if total == 0:
            attrs["note"] = (
                "No motion zones configured — use the Bosch app to set up zones"
            )
        return attrs


def _tls_cert_value(entity: BoschSensorEntity) -> datetime | None:
    cert = entity.coordinator.rcp_tls_cert_cache.get(entity._cam_id)
    if not cert or "not_after" not in cert:
        return None
    try:
        dt = datetime.fromisoformat(cert["not_after"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _tls_cert_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rcp_tls_cert_cache.get(entity._cam_id) is not None
    )


def _tls_cert_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    cert = entity.coordinator.rcp_tls_cert_cache.get(entity._cam_id, {})
    return {
        "issuer": cert.get("issuer", ""),
        "subject": cert.get("subject", ""),
        "key_size": cert.get("key_size"),
        "serial": cert.get("serial", ""),
        "not_before": cert.get("not_before", ""),
        "not_after": cert.get("not_after", ""),
        "signature_algorithm": cert.get("signature_algorithm", ""),
    }


TLS_CERT_DESCRIPTION = BoschSensorEntityDescription(
    key="tls_cert",
    translation_key="tls_cert",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    device_class=SensorDeviceClass.TIMESTAMP,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_tls_cert",
    value_fn=_tls_cert_value,
    available_fn=_tls_cert_available,
    extra_attrs_fn=_tls_cert_attrs,
)


class BoschTlsCertSensor(BoschSensorEntity):
    """Sensor: TLS certificate info from camera (RCP 0x0b91).

    Displays certificate expiry date. Attributes contain issuer, subject,
    key size, and serial number.
    """

    entity_description = TLS_CERT_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, TLS_CERT_DESCRIPTION)


def _network_services_value(entity: BoschSensorEntity) -> int | None:
    services = entity.coordinator.rcp_network_services_cache.get(entity._cam_id)
    if services is None:
        return None
    return len(services)


def _network_services_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rcp_network_services_cache.get(entity._cam_id)
        is not None
    )


def _network_services_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    services = entity.coordinator.rcp_network_services_cache.get(entity._cam_id, [])
    return {"services": services}


NETWORK_SERVICES_DESCRIPTION = BoschSensorEntityDescription(
    key="network_services",
    translation_key="network_services",
    native_unit_of_measurement="services",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_network_services",
    value_fn=_network_services_value,
    available_fn=_network_services_available,
    extra_attrs_fn=_network_services_attrs,
)


class BoschNetworkServicesSensor(BoschSensorEntity):
    """Sensor: network services running on camera (RCP 0x0c62).

    Displays count of active services. Attributes list all services
    (HTTP, HTTPS, RTSP, SNMP, UPnP, NTP, ONVIF, etc.).
    """

    entity_description = NETWORK_SERVICES_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, NETWORK_SERVICES_DESCRIPTION)


def _iva_catalog_value(entity: BoschSensorEntity) -> int | None:
    modules = entity.coordinator.rcp_iva_catalog_cache.get(entity._cam_id)
    if modules is None:
        return None
    return len(modules)


def _iva_catalog_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rcp_iva_catalog_cache.get(entity._cam_id) is not None
    )


def _iva_catalog_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    modules = entity.coordinator.rcp_iva_catalog_cache.get(entity._cam_id, [])
    active = [m for m in modules if m.get("active")]
    return {
        "modules": modules,
        "active_count": len(active),
        "active_modules": active,
    }


IVA_CATALOG_DESCRIPTION = BoschSensorEntityDescription(
    key="iva_analytics",
    translation_key="iva_analytics",
    native_unit_of_measurement="modules",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_iva_catalog",
    value_fn=_iva_catalog_value,
    available_fn=_iva_catalog_available,
    extra_attrs_fn=_iva_catalog_attrs,
)


class BoschIvaCatalogSensor(BoschSensorEntity):
    """Sensor: IVA analytics modules from camera firmware (RCP 0x0b60).

    Displays count of analytics modules. Attributes list all modules with
    ID, version, flags, and active state.
    """

    # Module lists for card display only — never historize the blobs (HA#39).
    # `active_count` stays recorded.
    _unrecorded_attributes = frozenset({"modules", "active_modules"})

    entity_description = IVA_CATALOG_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, IVA_CATALOG_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
class BoschPrivateAreasSensor(_BoschSensorBase):
    """Sensor: privacy mask areas (Gen1 rectangles + Gen2 polygons).

    Displays number of privacy masks. Attributes contain mask data
    for overlay visualization on the camera image.
      - cloud_privacy_masks: Gen1 rectangular masks (x/y/w/h normalized 0.0–1.0)
      - gen2_private_areas: Gen2 polygon masks (points array, color)
    """

    _attr_native_unit_of_measurement = "masks"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    # Mask coordinate lists for card overlay only — never historize the blobs
    # (HA#39). The *_count fields stay recorded.
    _unrecorded_attributes = frozenset({"cloud_privacy_masks", "gen2_private_areas"})

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_privacy_masks"
        self._attr_translation_key = "privacy_masks"

    @property
    def native_value(self) -> int | None:
        # See BoschMotionZonesSensor above — a naive `[]` default for both
        # cache lookups would return a confirmed "0 masks" (with a
        # misleading "no masks configured" attribute note) even before
        # either source had ever been fetched.
        gen2_areas = self.coordinator.gen2_private_areas_cache.get(self._cam_id)
        cloud_masks = self.coordinator.cloud_privacy_masks_cache.get(self._cam_id)
        if gen2_areas is None and cloud_masks is None:
            return None
        # Gen2 polygon private areas take priority
        if gen2_areas:
            return len(gen2_areas)
        # Gen1 cloud privacy masks
        return len(cloud_masks or [])

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success and (
            self.coordinator.gen2_private_areas_cache.get(self._cam_id) is not None
            or self.coordinator.cloud_privacy_masks_cache.get(self._cam_id) is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cloud_masks = self.coordinator.cloud_privacy_masks_cache.get(self._cam_id, [])
        gen2_areas = self.coordinator.gen2_private_areas_cache.get(self._cam_id, [])
        attrs: dict[str, Any] = {
            "cloud_privacy_masks": cloud_masks,
            "cloud_mask_count": len(cloud_masks),
            "gen2_private_areas": gen2_areas,
            "gen2_area_count": len(gen2_areas),
        }
        total = len(gen2_areas) or len(cloud_masks)
        if total == 0:
            attrs["note"] = (
                "No privacy masks configured — use the Bosch app to set up masks"
            )
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
class BoschAmbientLightScheduleSensor(_BoschSensorBase):
    """Sensor: ambient light schedule details (Gen2 only).

    Shows the schedule mode (ENVIRONMENT = dusk-to-dawn, or manual times).
    Attributes contain the full schedule config: enabled state, schedule type,
    manual start/end times, and per-light-group brightness/whiteBalance settings.
    Data source: GET /v11/video_inputs/{id}/lighting/ambient (fetched by coordinator, slow tier).
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_ambient_schedule"
        self._attr_translation_key = "ambient_schedule"
        self._attr_options = ["disabled", "dusk_to_dawn", "manual"]
        self._attr_device_class = SensorDeviceClass.ENUM

    @property
    def native_value(self) -> str | None:
        cache = self.coordinator.ambient_lighting_cache.get(self._cam_id)
        if not cache:
            return None
        enabled = cache.get("ambientLightEnabled", False)
        if not enabled:
            return "disabled"
        schedule = cache.get("ambientLightSchedule", {})
        # Schedule can be a string ("ENVIRONMENT") or dict ({"type": "ENVIRONMENT", ...})
        schedule_type = (
            schedule.get("type", schedule) if isinstance(schedule, dict) else schedule
        )
        if schedule_type == "ENVIRONMENT":
            return "dusk_to_dawn"
        return "manual"

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.ambient_lighting_cache.get(self._cam_id) is not None
            and len(self.coordinator.ambient_lighting_cache.get(self._cam_id, {})) > 0
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cache = self.coordinator.ambient_lighting_cache.get(self._cam_id, {})
        if not cache:
            return {}
        schedule = cache.get("ambientLightSchedule", "ENVIRONMENT")
        if isinstance(schedule, dict):
            schedule_str = schedule.get("type", "ENVIRONMENT")
        else:
            schedule_str = schedule
        attrs: dict[str, Any] = {
            "enabled": cache.get("ambientLightEnabled", False),
            "schedule_type": schedule_str,
        }
        if isinstance(schedule, dict):
            if schedule.get("lightOnTime"):
                attrs["schedule_on_time"] = schedule["lightOnTime"]
            if schedule.get("lightOffTime"):
                attrs["schedule_off_time"] = schedule["lightOffTime"]
        # Manual schedule times (if set)
        start = cache.get("ambientLightManualStartTime")
        end = cache.get("ambientLightManualEndTime")
        if start:
            attrs["manual_start_time"] = start
        if end:
            attrs["manual_end_time"] = end
        # Per-light-group brightness settings
        for group_key in (
            "frontLightSettings",
            "topLedLightSettings",
            "bottomLedLightSettings",
        ):
            group = cache.get(group_key)
            if group and isinstance(group, dict):
                prefix = (
                    group_key.replace("Settings", "")
                    .replace("Light", "_light")
                    .replace("Led", "_led")
                )
                attrs[f"{prefix}_brightness"] = group.get("brightness")
                wb = group.get("whiteBalance")
                if wb is not None:
                    attrs[f"{prefix}_white_balance"] = wb
                color = group.get("color")
                if color is not None:
                    attrs[f"{prefix}_color"] = color
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
class BoschAlarmStateSensor(_BoschSensorBase):
    """Sensor: alarm state (Gen2 Indoor II only).

    Actual API response (confirmed live):
        GET /v11/video_inputs/{id}/alarmStatus
        → {"alarmType": "NONE" | ..., "intrusionSystem": "INACTIVE" | "ACTIVE" | ...}

    Sensor state = intrusionSystem field (INACTIVE = disarmed, ACTIVE = armed).
    `alarm_type` in attributes exposes what kind of alarm last fired (NONE when idle).
    """

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_alarm_state"
        self._attr_translation_key = "alarm_state"
        self._attr_options = [
            "active",
            "inactive",
            "unknown",
            "system_managed_armed",
            "system_managed_disarmed",
            "armed_away",
            "armed_stay",
            "disarmed",
        ]
        self._attr_device_class = SensorDeviceClass.ENUM

    @property
    def native_value(self) -> str:
        status = self.coordinator.alarm_status_cache.get(self._cam_id, {})
        if status:
            # Guard the ENUM: an unmapped intrusionSystem value (e.g. new
            # firmware) would make HA discard the state and show "unknown"
            # anyway — map it explicitly so alarm automations get a defined
            # value, not a dropped one (same pattern as BoschLastEventTypeSensor).
            val = str(status.get("intrusionSystem", "unknown")).lower()
            opts = getattr(self, "_attr_options", None)
            return val if (not opts or val in opts) else "unknown"
        armed = self.coordinator.arming_cache.get(self._cam_id)
        if armed is True:
            return "active"
        if armed is False:
            return "inactive"
        return "unknown"

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success  # type: ignore[no-any-return]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        settings = self.coordinator.alarm_settings_cache.get(self._cam_id, {})
        status = self.coordinator.alarm_status_cache.get(self._cam_id, {})
        return {
            "alarm_mode": settings.get("alarmMode"),
            "pre_alarm_mode": settings.get("preAlarmMode"),
            "siren_duration_s": settings.get("alarmDelayInSeconds"),
            "activation_delay_s": settings.get("alarmActivationDelaySeconds"),
            "pre_alarm_duration_s": settings.get("preAlarmDelayInSeconds"),
            "alarm_type": status.get("alarmType"),
            "intrusion_system": status.get("intrusionSystem"),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschStreamStatusSensor(_BoschSensorBase):
    """Sensor: live stream state — idle / warming_up / connecting / streaming / streaming_remote."""

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_stream_status_{cam_id.lower()}"
        self._attr_translation_key = "stream_status"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_options = [
            "idle",
            "warming_up",
            "connecting",
            "streaming",
            "streaming_remote",
        ]
        self._attr_device_class = SensorDeviceClass.ENUM

    @property
    def native_value(self) -> str:
        fell_back = self.coordinator.stream_fell_back.get(self._cam_id, False)
        if self.coordinator.is_stream_warming(self._cam_id):
            return "warming_up"
        live = self.coordinator.live_connections.get(self._cam_id, {})
        rtsps = live.get("rtspsUrl") or live.get("rtspUrl")
        if rtsps:
            # stream_source is set → FFmpeg is (or will be) playing
            if fell_back:
                return "streaming_remote"
            return "streaming"
        if self._cam_id in self.coordinator.live_connections:
            return "connecting"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        live = self.coordinator.live_connections.get(self._cam_id, {})
        return {
            "connection_type": live.get("_connection_type", ""),
            "stream_errors": self.coordinator.stream_error_count.get(self._cam_id, 0),
            "fell_back": self.coordinator.stream_fell_back.get(self._cam_id, False),
        }


# ─────────────────────────────────────────────────────────────────────────────
class BoschNvrStateSensor(_BoschSensorBase):
    """Diagnostic sensor surfacing the Mini-NVR drain-watcher state per camera.

    Helps users answer "is recording actually reaching the target?". Reads
    from ``coordinator.nvr_drain_state`` (populated by
    ``recorder.sync_drain_tick``) and ``coordinator.nvr_user_intent`` /
    ``coordinator.nvr_processes`` (populated by the recorder lifecycle
    plumbing). Pure properties — no I/O. Disabled by default in the entity
    registry to avoid surprise entities.

    States:
      * ``recording`` — ffmpeg child is alive AND user-intent flag is set
      * ``idle``      — no recorder is running for this camera
      * ``error``     — the crash-loop guard tripped

    Attributes:
      * ``target``             — current ``nvr_storage_target`` (local/smb/ftp)
      * ``pending_uploads``    — files in the staging tree not yet finalized
      * ``failed_uploads``     — failed-this-tick upload count
      * ``last_segment_age_s`` — seconds since last seen segment for this cam
    """

    _attr_entity_registry_enabled_default = False
    # These fields are recomputed every 30 s drain tick while recording, so
    # they churn the recorder with no history value (HA#39). Keep them live;
    # never historize them. `target`/`error`/`user_intent` stay recorded.
    _unrecorded_attributes = frozenset(
        {"last_segment_age_s", "last_tick_ts", "pending_uploads", "failed_uploads"}
    )

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_nvr_state_{cam_id.lower()}"
        self._attr_translation_key = "nvr_state"
        self._attr_options = ["idle", "recording", "error"]
        self._attr_device_class = SensorDeviceClass.ENUM

    @property
    def native_value(self) -> str:
        if self.coordinator.nvr_error_state.get(self._cam_id):
            return "error"
        proc = self.coordinator.nvr_processes.get(self._cam_id)
        if proc is not None and self.coordinator.nvr_user_intent.get(self._cam_id):
            return "recording"
        return "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = getattr(self.coordinator, "nvr_drain_state", {}) or {}
        # Camera title is used as the staging-folder key (sanitized via
        # _safe_name in recorder._staging_dir). Read with the same sanitization
        # so the per-camera age lookup stays consistent.
        from .smb import _safe_name

        info = self.coordinator.data.get(self._cam_id, {}).get("info", {})
        cam_key = _safe_name(info.get("title", self._cam_id))
        last_age = (state.get("last_age_by_cam") or {}).get(cam_key)
        preroll_count = self.coordinator.nvr_preroll_segment_counts.get(self._cam_id, 0)
        return {
            "target": state.get("target", "local"),
            "pending_uploads": int(state.get("pending", 0)),
            "failed_uploads": int(state.get("failed", 0)),
            "last_segment_age_s": float(last_age) if last_age is not None else None,
            "last_tick_ts": state.get("last_tick_ts"),
            "user_intent": bool(
                self.coordinator.nvr_user_intent.get(self._cam_id, False)
            ),
            "error": self.coordinator.nvr_error_state.get(self._cam_id, ""),
            "preroll_segments": preroll_count,
            "preroll_running": bool(
                self.coordinator.nvr_preroll_processes.get(self._cam_id)
            ),
        }


def _ai_description_value(entity: BoschSensorEntity) -> str | None:
    """Return last description, truncated to 255 chars (HA state limit)."""
    text: str | None = entity._cam_data.get("ai_description", {}).get("text")
    if text is None:
        return None
    return text[:255]


def _ai_description_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    """Expose full description + metadata."""
    ai: dict[str, str | None] = entity._cam_data.get("ai_description", {})
    return {
        "description": ai.get("text"),
        "generated_at": ai.get("generated_at"),
        "ai_task_entity": ai.get("ai_task_entity"),
    }


AI_DESCRIPTION_DESCRIPTION = BoschSensorEntityDescription(
    key="ai_description",
    translation_key="ai_description",
    unique_id_fn=lambda cam_id: f"bosch_shc_ai_description_{cam_id.lower()}",
    value_fn=_ai_description_value,
    extra_attrs_fn=_ai_description_attrs,
)


class BoschCameraAiDescriptionSensor(BoschSensorEntity):
    """Sensor: last AI-generated snapshot description for this camera.

    Only created when the ``enable_ai_description`` integration option is
    enabled.  The state is the description text, truncated to 255 chars
    (HA state hard limit).  The full text is available in
    ``extra_state_attributes["description"]``.

    Updated via coordinator push whenever :func:`handle_describe_snapshot`
    stores a new result in ``coordinator.data[cam_id]["ai_description"]``.
    """

    entity_description = AI_DESCRIPTION_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, AI_DESCRIPTION_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _ai_alert_score_value(entity: BoschSensorEntity) -> int | None:
    score = entity._cam_data.get("ai_analysis", {}).get("score")
    if score is None:
        return None
    try:
        return int(score)
    except (TypeError, ValueError):
        return None


def _ai_alert_score_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    ai: dict[str, Any] = entity._cam_data.get("ai_analysis", {})
    if not ai:
        return {}
    return {
        "short": ai.get("short"),
        "detail": ai.get("detail"),
        "direction": ai.get("direction"),
        "carrying": ai.get("carrying"),
        "activity": ai.get("activity"),
        "gate_state": ai.get("gate_state"),
        "gate_risk": ai.get("gate_risk"),
        "known_person": ai.get("known_person"),
        "image_path": ai.get("image_path"),
        "generated_at": ai.get("generated_at"),
    }


AI_ALERT_SCORE_DESCRIPTION = BoschSensorEntityDescription(
    key="ai_alert_score",
    translation_key="ai_alert_score",
    state_class=SensorStateClass.MEASUREMENT,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_ai_alert_score",
    value_fn=_ai_alert_score_value,
    extra_attrs_fn=_ai_alert_score_attrs,
)


class BoschAiAlertScoreSensor(BoschSensorEntity):
    """Sensor: last AI Camera Analysis suspicion score (1-10) for this
    camera — sibling to `BoschCameraAiDescriptionSensor` (free-text), but
    for the STRUCTURED analysis feature (`ai_analysis.py`).

    Only created when the `ai_analysis_enabled` integration option is
    enabled (mirrors the sibling feature's own `enable_ai_description`
    gate). State is `None` until the first analysis has ever run for this
    camera. Updated via coordinator push whenever
    `ai_analysis.async_generate_ai_analysis` stores a new result in
    `coordinator.data[cam_id]["ai_analysis"]`.
    """

    entity_description = AI_ALERT_SCORE_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, AI_ALERT_SCORE_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
def _ai_alerts_24h_value(entity: BoschSensorEntity) -> int:
    entries = entity.coordinator.ai_analysis_recent.get(entity._cam_id, [])
    if not entries:
        return 0
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    count = 0
    for generated_at, _score in entries:
        try:
            gen_dt = datetime.fromisoformat(generated_at)
        except (TypeError, ValueError):
            continue
        if gen_dt >= cutoff:
            count += 1
    return count


AI_ALERTS_24H_DESCRIPTION = BoschSensorEntityDescription(
    key="ai_alerts_24h",
    translation_key="ai_alerts_24h",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_ai_alerts_24h",
    value_fn=_ai_alerts_24h_value,
)


class BoschAiAlerts24hSensor(BoschSensorEntity):
    """Sensor: rolling count of AI Camera Analysis alerts in the last 24h
    for this camera. Computed from the in-memory
    `coordinator.ai_analysis_recent[cam_id]` cache (see `ai_alert_store.py`'s
    `recent_alerts`/`async_load_recent_alerts` — populated on every stored
    alert and rebuilt from each camera's `alerts.jsonl` tail on startup, so
    the count survives an HA restart).

    Only created when the `ai_analysis_enabled` integration option is
    enabled — same gate as `BoschAiAlertScoreSensor`.
    """

    entity_description = AI_ALERTS_24H_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, AI_ALERTS_24H_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
import re as _re_inst


def _swap_inst(url: str, new_inst: int) -> str:
    """Return ``url`` with its ``inst=N`` query parameter rewritten to ``new_inst``.

    The Bosch RTSP URL always contains exactly one ``inst=N`` token in the
    query string (e.g. ``?inst=1&enableaudio=1``). This helper is the only
    place that knows that invariant — kept tiny so it's trivial to test.
    """
    return _re_inst.sub(r"inst=\d+", f"inst={new_inst}", url, count=1)


class _BoschStreamUrlSensorBase(_BoschSensorBase):
    """Shared base for the main + sub external-stream-URL sensors.

    Subclasses set ``_inst`` (1 for main, 2 for sub) and a translation key.
    Returns ``None`` when:
      - the BoschExternalStreamSwitch is OFF for this camera (default), OR
      - no live session is open yet (rtspsUrl empty).

    The URL is read straight from ``coordinator.live_connections[cam_id]``
    so it always reflects whatever quality/transport the integration picked
    (LOCAL TLS proxy, REMOTE TLS proxy, or direct rtsps fallback).
    """

    _attr_entity_registry_enabled_default = False
    _inst: int = 1

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.external_stream_enabled.get(self._cam_id, False):
            return None
        live = self.coordinator.live_connections.get(self._cam_id) or {}
        url = live.get("rtspsUrl") or live.get("rtspUrl") or ""
        if not url:
            return None
        # The integration always picks one ``inst=N`` per session; for the
        # sub-stream sensor we substitute it with 2. For the main sensor we
        # leave it untouched (whatever the user picked in options is fine —
        # typically inst=1 for max quality on LOCAL).
        if self._inst == 2:
            return _swap_inst(url, 2)
        return url


class BoschStreamUrlSensor(_BoschStreamUrlSensorBase):
    """Main RTSP stream URL (whatever inst= the current session uses).

    Default quality is inst=1 (LOCAL ~30 Mbps full-HD); selectable via the
    integration's stream-connection options. Same URL the camera entity uses
    internally — exposing it here lets users paste it into Frigate / BlueIris
    without digging through HA's internals.
    """

    _inst = 1

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_stream_url_{cam_id.lower()}"
        self._attr_translation_key = "stream_url"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC


class BoschStreamUrlSubSensor(_BoschStreamUrlSensorBase):
    """Sub-stream RTSP URL (inst=2 — balanced quality, ~7.5 Mbps LOCAL).

    Derived from the main URL by substituting ``inst=N`` → ``inst=2``. Same
    Bosch session, same TLS proxy, no extra cloud-API quota cost — RTSP is
    pull-based, so the camera only sends the sub-stream when an external
    client actually connects.
    """

    _inst = 2

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_stream_url_sub_{cam_id.lower()}"
        self._attr_translation_key = "stream_url_sub"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC


class _BoschFrigateUrlSensorBase(_BoschSensorBase):
    """Credential-free always-on RTSP URL for an external recorder (Frigate).

    Returns None unless the global ``frigate_endpoints_enabled`` option is on,
    the matching per-camera High/Low switch is on, and the front-door is bound.
    The URL needs no ``user:pass@`` — the front-door injects Digest auth toward
    the camera. Paste it straight into Frigate's go2rtc / ffmpeg input.
    """

    _attr_entity_registry_enabled_default = False
    _quality: str = "high"

    @property
    def native_value(self) -> str | None:
        url: str | None = self.coordinator.frigate_endpoint_url(
            self._cam_id, self._quality
        )
        return url


class BoschFrigateUrlHighSensor(_BoschFrigateUrlSensorBase):
    """Frigate persistent endpoint URL — High quality (inst=1)."""

    _quality = "high"

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_frigate_url_high_{cam_id.lower()}"
        self._attr_translation_key = "frigate_url_high"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC


class BoschFrigateUrlLowSensor(_BoschFrigateUrlSensorBase):
    """Frigate persistent endpoint URL — Low quality (inst=2)."""

    _quality = "low"

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_unique_id = f"bosch_shc_frigate_url_low_{cam_id.lower()}"
        self._attr_translation_key = "frigate_url_low"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC


# ─────────────────────────────────────────────────────────────────────────────
# F4: ONVIF Scopes Sensor
# ─────────────────────────────────────────────────────────────────────────────


def _onvif_scopes_value(entity: BoschSensorEntity) -> str | None:
    scopes = entity.coordinator.rcp_onvif_scopes_cache.get(entity._cam_id)
    if not scopes:
        return None
    return "supported"


def _onvif_scopes_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rcp_onvif_scopes_cache.get(entity._cam_id) is not None
    )


def _onvif_scopes_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    scopes = entity.coordinator.rcp_onvif_scopes_cache.get(entity._cam_id, {})
    return {
        "name": scopes.get("name", ""),
        "hardware": scopes.get("hardware", ""),
        "profiles": scopes.get("profiles", []),
        "raw_scopes": scopes.get("raw_scopes", []),
    }


ONVIF_SCOPES_DESCRIPTION = BoschSensorEntityDescription(
    key="onvif_scopes",
    translation_key="onvif_scopes",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    options=["supported"],
    device_class=SensorDeviceClass.ENUM,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_onvif_scopes",
    value_fn=_onvif_scopes_value,
    available_fn=_onvif_scopes_available,
    extra_attrs_fn=_onvif_scopes_attrs,
)


class BoschOnvifScopesSensor(BoschSensorEntity):
    """Sensor: ONVIF scope advertisement from camera firmware (RCP 0x0a98 via LAN).

    State: "ONVIF supported" when the camera answered the LAN RCP read, else
    the entity stays unavailable. Attributes contain the parsed scope dict
    (camera name, hardware model, advertised ONVIF profiles).

    Data source: RCP command 0x0a98 read directly from cam:443 over HTTPS
    with Digest auth (cbs credentials from local_creds_cache). Slow-tier
    (300 s) — cbs creds rotate on every PUT /connection so the RCP read
    is always authenticated with fresh credentials from the last LAN session.

    Disabled by default — enable in HA entity settings.
    """

    entity_description = ONVIF_SCOPES_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, ONVIF_SCOPES_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# F6: RCP Version Sensor
# ─────────────────────────────────────────────────────────────────────────────


def _rcp_version_value(entity: BoschSensorEntity) -> str | None:
    return entity.coordinator.rcp_version_cache.get(entity._cam_id)  # type: ignore[no-any-return]


def _rcp_version_available(entity: BoschSensorEntity) -> bool:
    return (
        entity.coordinator.last_update_success
        and entity.coordinator.rcp_version_cache.get(entity._cam_id) is not None
    )


def _rcp_version_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    ver = entity.coordinator.rcp_version_cache.get(entity._cam_id, "")
    if not ver:
        return {}
    parts = ver.split(".")
    return {
        "major": parts[0] if len(parts) > 0 else "",
        "minor": parts[1] if len(parts) > 1 else "",
        "patch": parts[2] if len(parts) > 2 else "",
        "build": parts[3] if len(parts) > 3 else "",
    }


RCP_VERSION_DESCRIPTION = BoschSensorEntityDescription(
    key="rcp_version",
    translation_key="rcp_version",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_rcp_version",
    value_fn=_rcp_version_value,
    available_fn=_rcp_version_available,
    extra_attrs_fn=_rcp_version_attrs,
)


class BoschRcpVersionSensor(BoschSensorEntity):
    """Sensor: RCP protocol version from camera firmware (RCP 0xff00 via LAN).

    State: version string "major.minor.patch.build" (e.g. "1.2.38.150").
    Gen1 cameras report ~1.2.9.225; Gen2 FW 9.40.102 reports 1.2.38.150.

    Data source: RCP command 0xff00 read directly from cam:443 over HTTPS
    with Digest auth (cbs credentials from local_creds_cache). Slow-tier
    (300 s). Returns 4 bytes which map to the four version components.

    Disabled by default — enable in HA entity settings.
    """

    entity_description = RCP_VERSION_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, RCP_VERSION_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# F13: Cloud Feature Flags Sensor
# ─────────────────────────────────────────────────────────────────────────────


def _cloud_feature_flags_value(entity: BoschSensorEntity) -> str | None:
    flags = entity.coordinator.feature_flags
    if not flags:
        return None
    enabled = sorted(k for k, v in flags.items() if v)
    result = ", ".join(enabled) if enabled else "none"
    return result[:255]


def _cloud_feature_flags_available(entity: BoschSensorEntity) -> bool:
    return entity.coordinator.last_update_success and bool(
        entity.coordinator.feature_flags
    )


def _cloud_feature_flags_attrs(entity: BoschSensorEntity) -> dict[str, Any]:
    flags = entity.coordinator.feature_flags
    if not flags:
        return {}
    return dict(flags)


CLOUD_FEATURE_FLAGS_DESCRIPTION = BoschSensorEntityDescription(
    key="cloud_feature_flags",
    translation_key="cloud_feature_flags",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    # Account-level unique_id — not per camera
    unique_id_fn=lambda cam_id: "bosch_shc_camera_cloud_feature_flags",
    value_fn=_cloud_feature_flags_value,
    available_fn=_cloud_feature_flags_available,
    extra_attrs_fn=_cloud_feature_flags_attrs,
)


class BoschCloudFeatureFlagsSensor(BoschSensorEntity):
    """Sensor: Bosch cloud feature flags for this account (GET /v11/feature_flags).

    State: comma-separated list of enabled flag names (those with value=True).
    Attributes: full dict of all flags with their boolean values.

    Data source: GET /v11/feature_flags — fetched once at startup and cached
    in coordinator.feature_flags. Rarely changes (account-level server-side
    config). Account-level entity — one per integration, not per camera.

    Disabled by default — enable in HA entity settings.
    """

    entity_description = CLOUD_FEATURE_FLAGS_DESCRIPTION

    def __init__(
        self, coordinator: BoschCameraCoordinator, cam_id: str, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, cam_id, entry, CLOUD_FEATURE_FLAGS_DESCRIPTION)
