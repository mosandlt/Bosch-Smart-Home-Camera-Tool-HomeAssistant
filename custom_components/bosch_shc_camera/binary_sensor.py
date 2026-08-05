"""Bosch Smart Home Camera — Binary Sensor Platform.

Creates binary sensor entities per camera:
  • {Name} Motion           — ON when a MOVEMENT event was detected within the configurable active window (default 90 s)
  • {Name} Audio Alarm      — ON when an AUDIO_ALARM event was detected within the configurable active window (default 90 s)
  • {Name} Person Detected  — ON when a PERSON event was detected within the configurable active window (default 90 s)

All sensors are disabled by default (entity_registry_enabled_default = False).
Enable them in Settings → Entities if you want to trigger automations from motion/audio/person events.

Event data is read from coordinator.data[cam_id]["events"] (the most recent event list).
The sensors go ON when the most-recent event matches the type AND its timestamp is within
the configurable active window (default 90 s); otherwise they are OFF.

Device class:
  motion binary sensor  → BinarySensorDeviceClass.MOTION
  audio  binary sensor  → BinarySensorDeviceClass.SOUND
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, BoschCameraCoordinator, get_options  # type: ignore[attr-defined]
from .const import (
    CONF_AI_ANALYSIS_ENABLED,
    DEFAULT_MOTION_ACTIVE_WINDOW,
    MOTION_ACTIVE_WINDOW_MAX,
    MOTION_ACTIVE_WINDOW_MIN,
)
from .dynamic_devices import register_dynamic_camera_listener
from .time_utils import parse_bosch_timestamp

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = (
    0  # coordinator handles all updates; no per-entity parallelism needed
)

# Module-level fallback — keeps tests and external code that reference
# EVENT_ACTIVE_WINDOW directly working unchanged.  Production code reads
# the per-entry option via `_motion_active_window` (see below).
EVENT_ACTIVE_WINDOW = DEFAULT_MOTION_ACTIVE_WINDOW


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities for each camera."""
    coordinator: BoschCameraCoordinator = config_entry.runtime_data
    opts = get_options(config_entry)

    def _build_entities_for_cam(cam_id: str) -> list[Any]:
        cam_info = coordinator.data.get(cam_id, {}).get("info", {})
        has_sound = cam_info.get("featureSupport", {}).get("sound", False)
        cam_entities: list[Any] = [
            BoschMotionBinarySensor(coordinator, cam_id, config_entry),
            BoschPersonDetectedBinarySensor(coordinator, cam_id, config_entry),
            BoschLanReachableBinarySensor(coordinator, cam_id, config_entry),
        ]
        if has_sound:
            cam_entities.append(
                BoschAudioAlarmBinarySensor(coordinator, cam_id, config_entry)
            )
        # AI Camera Analysis — only when the master option is enabled (same
        # gate as the sensor.py AI-analysis sensors).
        if opts.get(CONF_AI_ANALYSIS_ENABLED, False):
            cam_entities.append(
                BoschAiRecentAlertBinarySensor(coordinator, cam_id, config_entry)
            )
        return cam_entities

    known_cam_ids: set[str] = set(coordinator.data)
    entities: list[Any] = []
    for cam_id in known_cam_ids:
        entities.extend(_build_entities_for_cam(cam_id))
    async_add_entities(entities, update_before_add=False)

    # Quality-Scale Gold `dynamic-devices`.
    config_entry.async_on_unload(
        register_dynamic_camera_listener(
            coordinator, known_cam_ids, async_add_entities, _build_entities_for_cam
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
class _BoschBinarySensorBase(CoordinatorEntity, BinarySensorEntity):  # type: ignore[misc]
    """Shared base for Bosch camera binary sensors."""

    # Disabled by default — enable explicitly in entity registry if desired
    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
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

    def _get_latest_event_of_type(self, event_type: str) -> dict[str, Any] | None:
        """Return the most recent event matching event_type, or None."""
        events = self._cam_data.get("events", [])
        for ev in events:
            if ev.get("eventType", "") == event_type:
                return ev  # type: ignore[no-any-return]
        return None

    def _get_latest_person_event(self) -> dict[str, Any] | None:
        """Return the most recent event that represents a detected person.

        Gen2 cameras (Outdoor II / Indoor II, DualRadar) report a human as
        ``eventType="MOVEMENT"`` with ``eventTags=["PERSON"]`` rather than a bare
        ``PERSON`` type. The coordinator only upgrades a *local* variable to
        PERSON when firing the HA bus event — the raw event dict kept in
        ``coordinator.data[...]["events"]`` is never rewritten, so matching on
        ``eventType=="PERSON"`` alone left the Person sensor stuck OFF on Gen2.
        Accept either the explicit PERSON type or a MOVEMENT event tagged
        PERSON, whichever is newer in the (newest-first) event list.
        """
        events = self._cam_data.get("events", [])
        for ev in events:
            event_type = ev.get("eventType", "")
            if event_type == "PERSON":
                return ev  # type: ignore[no-any-return]
            if event_type == "MOVEMENT" and "PERSON" in (ev.get("eventTags") or []):
                return ev  # type: ignore[no-any-return]
        return None

    @property
    def _motion_active_window(self) -> int:
        """Return the configured active-window duration in seconds.

        Reads `motion_active_window` from the config-entry options, falling
        back to DEFAULT_MOTION_ACTIVE_WINDOW (90 s) when the key is absent
        (legacy entries without the option).  The value is clamped to the
        valid range [MOTION_ACTIVE_WINDOW_MIN, MOTION_ACTIVE_WINDOW_MAX] so
        persisted out-of-range values (e.g. from a corrupted config) never
        cause surprising behaviour.
        """
        raw: Any = self._entry.options.get(
            "motion_active_window", DEFAULT_MOTION_ACTIVE_WINDOW
        )
        try:
            value: int = int(raw)
        except (TypeError, ValueError):
            value = DEFAULT_MOTION_ACTIVE_WINDOW
        return max(MOTION_ACTIVE_WINDOW_MIN, min(MOTION_ACTIVE_WINDOW_MAX, value))

    def _event_within_window(self, event: dict[str, Any]) -> bool:
        """Return True if the event timestamp is within the active window seconds of now.

        Bosch /v11/events timestamps carry an explicit timezone designator —
        currently an offset, e.g. ``"2026-06-18T06:06:30.499+02:00[Europe/Berlin]"``,
        historically a ``Z`` suffix. The instant MUST be derived by honoring
        that designator (`parse_bosch_timestamp`), never by truncating it away:
        ``ts_str[:19]`` + ``replace(tzinfo=UTC)`` re-labelled the local
        wall-clock reading as UTC, so a fresh event appeared ~2h in the future
        (negative age → window stuck on) in CEST. Parsing the offset restores
        the true instant.

        The window duration is taken from `_motion_active_window` which reads
        the `motion_active_window` config-entry option (default 90 s, range
        10-300 s, configurable via Settings → Integrations → Configure).
        """
        dt_utc = parse_bosch_timestamp(event.get("timestamp"))
        if dt_utc is None:
            return False
        age = datetime.now(tz=UTC) - dt_utc
        return age <= timedelta(seconds=self._motion_active_window)


# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, kw_only=True)
class BoschBinarySensorEntityDescription(BinarySensorEntityDescription):  # type: ignore[misc]
    """Describes a Bosch camera binary sensor.

    Mirrors the `EntityDescription`-driven pattern used by Platinum-tier core
    integrations (e.g. `reolink`): structurally-identical entities share one
    generic entity class (`BoschBinarySensorEntity` below) parametrized by a
    description instance, instead of one hand-written subclass per entity.

    `unique_id_prefix`/`unique_id_suffix` express the historical (and in one
    case quirky — see `BoschPersonDetectedBinarySensor`'s `bosch_shc_cam_`
    prefix) unique_id scheme; preserved verbatim so existing users' entities
    are never orphaned by this refactor.

    `event_lookup_fn`, when set, is the shared "find the most-recent matching
    event" behavior the event-based sensors (motion/audio_alarm/person) all
    share — `is_on`/`extra_state_attributes` are ON only if the found event's
    timestamp is within the configurable active window. Sensors with
    genuinely different logic (LAN-reachable, AI-recent-alert) leave this
    `None` and override `is_on`/`extra_state_attributes`/`available` directly
    in their own thin subclass, same as Reolink does for its own outliers.
    """

    unique_id_prefix: str = "bosch_shc_camera"
    unique_id_suffix: str = ""
    event_lookup_fn: (
        Callable[[BoschBinarySensorEntity], dict[str, Any] | None] | None
    ) = None


class BoschBinarySensorEntity(_BoschBinarySensorBase):
    """Generic Bosch camera binary sensor driven by a `BoschBinarySensorEntityDescription`.

    Implements the shared "most-recent-event-of-type within the active
    window" behavior for `event_lookup_fn`-based descriptions. Subclasses
    with different semantics (LAN-reachable, AI-recent-alert) override
    `is_on`/`extra_state_attributes`/`available` and simply don't rely on
    `event_lookup_fn`.
    """

    entity_description: BoschBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
        description: BoschBinarySensorEntityDescription,
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self.entity_description = description
        self._attr_unique_id = (
            f"{description.unique_id_prefix}_{cam_id}_{description.unique_id_suffix}"
        )
        self._attr_translation_key = description.translation_key
        if description.device_class is not None:
            self._attr_device_class = description.device_class
        if description.entity_category is not None:
            self._attr_entity_category = description.entity_category
        self._attr_entity_registry_enabled_default = (
            description.entity_registry_enabled_default
        )

    @property
    def is_on(self) -> bool | None:
        if self.entity_description.event_lookup_fn is None:
            raise NotImplementedError
        event = self.entity_description.event_lookup_fn(self)
        if event is None:
            return False
        return self._event_within_window(event)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.entity_description.event_lookup_fn is None:
            return {}
        event = self.entity_description.event_lookup_fn(self)
        if not event:
            return {}
        return {
            "event_id": event.get("id", ""),
            "timestamp": event.get("timestamp", ""),
        }


MOTION_DESCRIPTION = BoschBinarySensorEntityDescription(
    key="motion",
    device_class=BinarySensorDeviceClass.MOTION,
    translation_key="motion",
    entity_registry_enabled_default=False,
    unique_id_suffix="motion_binary",
    event_lookup_fn=lambda entity: entity._get_latest_event_of_type("MOVEMENT"),
)

AUDIO_ALARM_DESCRIPTION = BoschBinarySensorEntityDescription(
    key="audio_alarm",
    device_class=BinarySensorDeviceClass.SOUND,
    translation_key="audio_alarm_binary",
    entity_registry_enabled_default=False,
    unique_id_suffix="audio_alarm_binary",
    event_lookup_fn=lambda entity: entity._get_latest_event_of_type("AUDIO_ALARM"),
)

PERSON_DESCRIPTION = BoschBinarySensorEntityDescription(
    key="person_detected",
    device_class=BinarySensorDeviceClass.OCCUPANCY,
    translation_key="person_detected",
    entity_registry_enabled_default=False,
    unique_id_prefix="bosch_shc_cam",
    unique_id_suffix="person_detected",
    event_lookup_fn=lambda entity: entity._get_latest_person_event(),
)


# ─────────────────────────────────────────────────────────────────────────────
class BoschMotionBinarySensor(BoschBinarySensorEntity):
    """Binary sensor: ON when a MOVEMENT event occurred within the configurable active window (default 90 s)."""

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry, MOTION_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
class BoschAudioAlarmBinarySensor(BoschBinarySensorEntity):
    """Binary sensor: ON when an AUDIO_ALARM event occurred within the configurable active window (default 90 s)."""

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry, AUDIO_ALARM_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
class BoschPersonDetectedBinarySensor(BoschBinarySensorEntity):
    """Binary sensor: ON when a PERSON event occurred within the configurable active window (default 90 s)."""

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry, PERSON_DESCRIPTION)


LAN_REACHABLE_DESCRIPTION = BoschBinarySensorEntityDescription(
    key="lan_reachable",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="lan_reachable",
    entity_registry_enabled_default=True,
    unique_id_suffix="lan_reachable",
    # No event_lookup_fn — TCP-reachability logic, not event-based. is_on /
    # extra_state_attributes / available are all overridden below.
)


# ─────────────────────────────────────────────────────────────────────────────
class BoschLanReachableBinarySensor(BoschBinarySensorEntity):
    """Reports whether the camera answers a TCP connect on port 443.

    Always available — useful precisely when the Bosch cloud is unreachable.
    Honors the post-write grace period so a transient blip right after a
    privacy/light toggle does not flip the state to off (the camera briefly
    tears down its HTTPS endpoint while Digest creds rotate).
    """

    # Both freshness fields are monotonic-derived → they change on every
    # coordinator tick while the on/off state stays put. Recording them spawns
    # a new `state_attributes` row each tick and bloats the DB. Keep them
    # visible live, but never historize them.
    _unrecorded_attributes = frozenset(
        {"last_check_seconds_ago", "write_grace_seconds_left"}
    )

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry, LAN_REACHABLE_DESCRIPTION)
        # Use HA's auto-naming via translation_key + device_info — no `name`
        # override here, otherwise the device-name prefix gets duplicated
        # into the slug, producing entity_ids like
        # `binary_sensor.bosch_<title>_bosch_<title>_lan_reachable`.

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool | None:
        is_lan_reachable = getattr(self.coordinator, "is_lan_reachable", None)
        if is_lan_reachable is None:
            return None
        result = is_lan_reachable(self._cam_id)
        return None if result is None else bool(result)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry = self.coordinator.lan_tcp_reachable.get(self._cam_id)
        attrs: dict[str, Any] = {"camera_id": self._cam_id}
        if entry is not None:
            _reachable, ts = entry
            attrs["last_check_seconds_ago"] = round(_time.monotonic() - ts)
        last_write = (
            self.coordinator.local_write_at.get(self._cam_id, float("-inf"))
            if hasattr(self.coordinator, "local_write_at")
            else float("-inf")
        )
        if last_write != float("-inf"):
            grace_left = self.coordinator.LOCAL_WRITE_GRACE_S - (
                _time.monotonic() - last_write
            )
            if grace_left > 0:
                attrs["write_grace_seconds_left"] = round(grace_left)
        return attrs


# ─────────────────────────────────────────────────────────────────────────────
# Deliberately a FIXED window, not a reuse of the `ai_analysis_repeat_context_minutes`
# option: that option tunes a PROMPT heuristic (how long AI-provided context
# should mention recent activity to the model) — a UI "recent activity"
# indicator is a different concern, and coupling the two would mean changing
# the prompt-context window silently also changes how long this binary
# sensor stays on. See task spec / ai-camera-analysis plan.
AI_RECENT_ALERT_WINDOW_MINUTES = 10

AI_RECENT_ALERT_DESCRIPTION = BoschBinarySensorEntityDescription(
    key="ai_recent_alert",
    translation_key="ai_recent_alert",
    entity_registry_enabled_default=True,
    unique_id_suffix="ai_recent_alert",
    # No event_lookup_fn — reads coordinator.ai_analysis_recent, not
    # coordinator.data[...]["events"]. is_on / extra_state_attributes /
    # available are all overridden below.
)


class BoschAiRecentAlertBinarySensor(BoschBinarySensorEntity):
    """ON for `AI_RECENT_ALERT_WINDOW_MINUTES` minutes after the most recent
    AI Camera Analysis alert for this camera (`ai_analysis.py`).

    Backed by the in-memory `coordinator.ai_analysis_recent[cam_id]` cache
    (see `ai_alert_store.py`), same source `BoschAiAlerts24hSensor` reads.

    `last_score`/`last_short` only change when a NEW alert lands (same
    cadence as the on/off state itself), so they're safe to record. The
    "how long until this turns off" value changes every coordinator tick
    while on/off stays put — same recorder-DB-bloat shape
    `BoschLanReachableBinarySensor` guards against elsewhere in this file —
    so it's excluded via `_unrecorded_attributes`, same discipline.
    """

    _unrecorded_attributes = frozenset({"seconds_since_last_alert"})

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator, cam_id, entry, AI_RECENT_ALERT_DESCRIPTION)

    def _latest_alert(self) -> tuple[str, int] | None:
        entries = self.coordinator.ai_analysis_recent.get(self._cam_id, [])
        return entries[-1] if entries else None

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        latest = self._latest_alert()
        if latest is None:
            return False
        generated_at, _score = latest
        try:
            gen_dt = datetime.fromisoformat(generated_at)
        except (TypeError, ValueError):
            return False
        if gen_dt.tzinfo is None:
            gen_dt = gen_dt.replace(tzinfo=UTC)
        return (datetime.now(UTC) - gen_dt) <= timedelta(
            minutes=AI_RECENT_ALERT_WINDOW_MINUTES
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        latest = self._latest_alert()
        if latest is None:
            return {}
        generated_at, score = latest
        attrs: dict[str, Any] = {"last_score": score, "generated_at": generated_at}
        try:
            gen_dt = datetime.fromisoformat(generated_at)
            if gen_dt.tzinfo is None:
                gen_dt = gen_dt.replace(tzinfo=UTC)
            attrs["seconds_since_last_alert"] = round(
                (datetime.now(UTC) - gen_dt).total_seconds()
            )
        except (TypeError, ValueError):
            pass
        return attrs
