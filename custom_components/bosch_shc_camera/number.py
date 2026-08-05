"""Bosch Smart Home Camera — Number Platform.

Creates number entities per camera:
  • {Name} Pan Position     — pan the 360 camera left/right (-120° to +120°).
    Only available for cameras with featureSupport.panLimit > 0 (CAMERA_360).
    Uses cloud API: PUT /v11/video_inputs/{id}/pan
    State is read from GET /v11/video_inputs/{id}/pan (polled each coordinator tick).

  • {Name} Intrusion Sensitivity  — intrusion detection sensitivity 0-7 (Gen2 only).
    Reads from coordinator.intrusion_config_cache[cam_id]["sensitivity"].
    Writes via PUT /v11/video_inputs/{id}/intrusionDetectionConfig — full body preserved.
    FW 9.40+ supports range 0-7 (confirmed live: sensitivity=3, max=7).

  • {Name} Intrusion Distance  — detection range in metres 1-8 (Gen2 only).
    Reads from coordinator.intrusion_config_cache[cam_id]["distance"].
    Writes via PUT /v11/video_inputs/{id}/intrusionDetectionConfig — full body preserved.
    API rejects distance > 8 with HTTP 400 (verified FW 9.40.102). Max clamped to 8.

Every structurally-similar number entity (single cache field read +
single-endpoint PUT write, with an optional per-camera lock / privacy guard
/ write-lock timestamp) is driven by one generic `BoschNumberEntity`
parametrized by a `BoschNumberEntityDescription` — matching the pattern used
by Platinum-tier core integrations like `reolink`, instead of one
hand-written subclass per entity. `BoschPanNumber` stays a genuine outlier
(its min/max range depends on a per-camera `pan_limit` value that a single
shared description object cannot express) and overrides `native_value`/
`available`/`async_set_native_value` directly — a legitimate hybrid, not a
mechanical one-size-fits-all collapse.
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base import _BoschEntityBase
from .dynamic_devices import register_dynamic_camera_listener
from .guards import _get_cam_lock, _is_gen2_indoor, _warn_if_privacy_on

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data

    def _build_entities_for_cam(cam_id: str) -> list[Any]:
        cam_info = coordinator.data.get(cam_id, {}).get("info", {})
        cam_entities: list[Any] = []
        pan_limit = cam_info.get("featureSupport", {}).get("panLimit", 0)
        if pan_limit:
            cam_entities.append(
                BoschPanNumber(coordinator, cam_id, config_entry, pan_limit)
            )
        cam_entities.append(BoschSpeakerLevelNumber(coordinator, cam_id, config_entry))
        # Card playback volume — paired with the audio switch (registered for
        # every camera), the automatable source of truth for the card's volume.
        cam_entities.append(BoschAudioVolumeNumber(coordinator, cam_id, config_entry))
        has_light = cam_info.get("featureSupport", {}).get("light", False)
        if has_light:
            cam_entities.append(
                BoschFrontLightIntensityNumber(coordinator, cam_id, config_entry)
            )
        # Gen2-only entities
        from .models import get_model_config

        hw = cam_info.get("hardwareVersion", "CAMERA")
        if get_model_config(hw).generation >= 2:
            # lens_elevation works on both Indoor II and Outdoor II
            # (Indoor II slow-tier returns 200 on this endpoint, confirmed live)
            cam_entities.append(
                BoschLensElevationNumber(coordinator, cam_id, config_entry)
            )
            cam_entities.append(
                BoschMicrophoneLevelNumber(coordinator, cam_id, config_entry)
            )
            # Intrusion detection tuning — available on both Indoor II and Outdoor II.
            cam_entities.append(
                BoschIntrusionSensitivityNumber(coordinator, cam_id, config_entry)
            )
            cam_entities.append(
                BoschIntrusionDistanceNumber(coordinator, cam_id, config_entry)
            )
            # Light-related entities only for cameras that actually expose Gen2 lighting
            # (Indoor II has no RGB/wallwasher lights — only Power-LED via iconLedBrightness).
            if hw not in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
                cam_entities.append(
                    BoschWhiteBalanceNumber(coordinator, cam_id, config_entry)
                )
                cam_entities.append(
                    BoschTopLedBrightnessNumber(coordinator, cam_id, config_entry)
                )
                cam_entities.append(
                    BoschBottomLedBrightnessNumber(coordinator, cam_id, config_entry)
                )
                cam_entities.append(
                    BoschMotionLightSensitivityNumber(coordinator, cam_id, config_entry)
                )
                cam_entities.append(
                    BoschDarknessThresholdNumber(coordinator, cam_id, config_entry)
                )
        # Gen2 Indoor II — alarm delays + power-LED brightness
        if hw in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
            cam_entities.append(
                BoschPowerLedBrightnessNumber(coordinator, cam_id, config_entry)
            )
            cam_entities.append(
                BoschAlarmDelayNumber(coordinator, cam_id, config_entry)
            )
            cam_entities.append(
                BoschAlarmActivationDelayNumber(coordinator, cam_id, config_entry)
            )
            cam_entities.append(
                BoschPreAlarmDelayNumber(coordinator, cam_id, config_entry)
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
@dataclass(frozen=True, kw_only=True)
class BoschNumberEntityDescription(NumberEntityDescription):  # type: ignore[misc]
    """Describes a Bosch camera number entity.

    Mirrors the `EntityDescription`-driven pattern used by Platinum-tier core
    integrations (e.g. `reolink`): structurally-similar entities (read one
    field from a coordinator cache, write it back via one cloud PUT) share
    one generic entity class (`BoschNumberEntity` below) parametrized by a
    description instance, instead of one hand-written subclass per entity.

    `unique_id_fn` builds the entity's `unique_id` from `cam_id` — kept as a
    function rather than a fixed prefix/suffix template because the historical
    unique_id schemes genuinely differ between entities (some use
    `bosch_shc_camera_{cam_id}_{suffix}`, others `bosch_shc_{name}_{cam_id.lower()}`)
    and must be preserved verbatim so existing users' entities are never
    orphaned by this refactor.

    `value_fn`/`available_fn`/`set_value_fn` are the per-entity read/gate/write
    logic. All three are `None` for `BoschPanNumber` (its min/max range is
    per-camera dynamic, not expressible in a single shared description) —
    that entity overrides `native_value`/`available`/`async_set_native_value`
    directly instead, same as `binary_sensor.py`'s LanReachable/AiRecentAlert
    outliers still carrying a description for static metadata only.
    """

    unique_id_fn: Callable[[str], str]
    value_fn: Callable[[BoschNumberEntity], float | None] | None = None
    available_fn: Callable[[BoschNumberEntity], bool] | None = None
    set_value_fn: (
        Callable[[BoschNumberEntity, float], Coroutine[Any, Any, None]] | None
    ) = None


class BoschNumberEntity(_BoschEntityBase, NumberEntity):  # type: ignore[misc]
    """Generic Bosch camera number entity driven by a `BoschNumberEntityDescription`.

    Implements the shared "read one cache field / write it back via one PUT"
    behavior for `value_fn`/`available_fn`/`set_value_fn`-based descriptions.
    `BoschPanNumber` is the one outlier that leaves all three `None` and
    overrides the corresponding property/method directly.

    `_led_key`/`_field`/`_brightness`/`_wb_value` are declared here (not only
    set in `__init__`) as class-level defaults, and declared again as class
    attributes on the concrete subclasses that need a non-default value, so
    that unit tests which bypass `__init__` via `cls.__new__(cls)` (used
    throughout `tests/test_number.py` to unit-test `native_value`/`available`/
    `async_set_native_value` without exercising `CoordinatorEntity.__init__`)
    still resolve the correct per-entity field name via ordinary class
    attribute lookup.
    """

    entity_description: BoschNumberEntityDescription
    _attr_has_entity_name = True
    _led_key: str = ""
    _field: str = ""
    _brightness: float | None = None
    _wb_value: float | None = None

    def __init__(
        self,
        coordinator: Any,
        cam_id: str,
        entry: ConfigEntry,
        description: BoschNumberEntityDescription,
    ) -> None:
        super().__init__(coordinator, cam_id, entry)
        self.entity_description = description
        self._attr_unique_id = description.unique_id_fn(cam_id)
        self._attr_translation_key = description.translation_key
        self._attr_native_min_value = description.native_min_value
        self._attr_native_max_value = description.native_max_value
        self._attr_native_step = description.native_step
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        if description.mode is not None:
            self._attr_mode = description.mode
        if description.entity_category is not None:
            self._attr_entity_category = description.entity_category
        self._attr_entity_registry_enabled_default = (
            description.entity_registry_enabled_default
        )

    @property
    def native_value(self) -> float | None:
        if self.entity_description.value_fn is None:
            raise NotImplementedError
        return self.entity_description.value_fn(self)

    @property
    def available(self) -> bool:
        if self.entity_description.available_fn is None:
            raise NotImplementedError
        return self.entity_description.available_fn(self)

    async def async_set_native_value(self, value: float) -> None:
        if self.entity_description.set_value_fn is None:
            raise NotImplementedError
        await self.entity_description.set_value_fn(self, value)


# ─────────────────────────────────────────────────────────────────────────────
PAN_DESCRIPTION = BoschNumberEntityDescription(
    key="pan_position",
    translation_key="pan_position",
    native_step=1,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="°",
    entity_category=EntityCategory.CONFIG,
    unique_id_fn=lambda cam_id: f"bosch_shc_pan_{cam_id.lower()}",
)


class BoschPanNumber(BoschNumberEntity):
    """Number entity to control the pan position of the 360 camera.

    Genuine outlier: `native_min_value`/`native_max_value` depend on the
    per-camera `pan_limit` (only known at `async_setup_entry` time, not a
    static description field), so this entity overrides `native_value`/
    `available`/`async_set_native_value` directly instead of using
    `value_fn`/`available_fn`/`set_value_fn`.
    """

    entity_description = PAN_DESCRIPTION

    def __init__(
        self, coordinator: Any, cam_id: str, entry: ConfigEntry, pan_limit: int
    ) -> None:
        super().__init__(coordinator, cam_id, entry, PAN_DESCRIPTION)
        self._pan_limit = pan_limit
        self._attr_native_min_value = -pan_limit
        self._attr_native_max_value = pan_limit

    def _rotation_180(self) -> bool:
        """Return True if the camera is configured as ceiling-mounted (image
        rotated 180°). When True, the slider sign is inverted so that "right"
        on the slider stays "right" on the user's screen.
        """
        return bool(
            getattr(self.coordinator, "image_rotation_180", {}).get(self._cam_id)
        )

    @property
    def native_value(self) -> float | None:
        raw = self.coordinator.pan_cache.get(self._cam_id)
        if raw is None:
            return None
        return float(-raw if self._rotation_180() else raw)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.pan_cache.get(self._cam_id) is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        # Invert sign when the camera is ceiling-mounted so the user-visible
        # direction matches the camera-physical pan direction.
        actual = -int(value) if self._rotation_180() else int(value)
        await self.coordinator.async_cloud_set_pan(self._cam_id, actual)


# ─────────────────────────────────────────────────────────────────────────────
# Speaker level (intercom volume, 0–100%)
#
# Reads from coordinator.audio_cache[cam_id]["speakerLevel"].
# Writes via PUT /v11/video_inputs/{id}/audio with full body preserved —
# same pattern as microphone level so audioEnabled is not clobbered.
# Body shape: {"audioEnabled":true,"microphoneLevel":60,"speakerLevel":80}.
# Disabled by default — enable in Settings -> Entities.
# Serialized on a per-camera lock shared with microphone level and
# BoschIntercomSwitch (same /audio endpoint, same audio_cache) so a
# concurrent write to a sibling field can't be clobbered by a stale snapshot
# taken before the lock.
def _speaker_level_value(entity: BoschNumberEntity) -> float | None:
    audio = entity.coordinator.audio_cache.get(entity._cam_id, {})
    val = audio.get("speakerLevel")
    return float(val) if val is not None else None


def _speaker_level_available(entity: BoschNumberEntity) -> bool:
    return entity.coordinator.last_update_success and (
        entity.coordinator.audio_cache.get(entity._cam_id) is not None
    )


async def _speaker_level_set(entity: BoschNumberEntity, value: float) -> None:
    level = round(value)
    lock = _get_cam_lock(entity.coordinator, "_audio_config_locks", entity._cam_id)
    async with lock:
        audio = dict(entity.coordinator.audio_cache.get(entity._cam_id, {}))
        audio["speakerLevel"] = level
        success = await entity.coordinator.async_put_camera(
            entity._cam_id, "audio", audio
        )
        if success:
            # Merge only the changed field so a concurrent microphone write
            # isn't clobbered by our stale snapshot.
            entity.coordinator.audio_cache.setdefault(entity._cam_id, {})[
                "speakerLevel"
            ] = level
            _LOGGER.debug("Speaker level set to %d for %s", level, entity._cam_id)
        else:
            _LOGGER.warning(
                "Failed to set speaker level for %s: HTTP error", entity._cam_id
            )
    entity.async_write_ha_state()


SPEAKER_LEVEL_DESCRIPTION = BoschNumberEntityDescription(
    key="speaker_level",
    translation_key="speaker_level",
    native_min_value=0,
    native_max_value=100,
    native_step=1,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=False,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_speaker_level",
    value_fn=_speaker_level_value,
    available_fn=_speaker_level_available,
    set_value_fn=_speaker_level_set,
)


class BoschSpeakerLevelNumber(BoschNumberEntity):
    """Number entity to control the intercom speaker volume (0–100)."""

    entity_description = SPEAKER_LEVEL_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, SPEAKER_LEVEL_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# Card playback volume — virtual preference, no Bosch API (loudness is a
# browser property). Automatable, cross-session source of truth the Lovelace
# card applies to its <video> element: the card reads it and writes it back
# via number.set_value, and HA pushes the change to every open card. No
# effect on iOS (Safari makes video.volume read-only). Paired with
# switch.<cam>_audio (the on/off master).
_AUDIO_VOLUME_DEFAULT = 50


def _audio_volume_value(entity: BoschNumberEntity) -> float:
    return float(
        entity.coordinator.audio_volume.get(entity._cam_id, _AUDIO_VOLUME_DEFAULT)
    )


def _audio_volume_available(entity: BoschNumberEntity) -> bool:
    # Grey out together with the camera's other controls when it is offline,
    # rather than staying settable on its own (the audio switch greys too).
    return bool(entity.coordinator.last_update_success) and bool(
        entity.coordinator.is_camera_online(entity._cam_id)
    )


async def _audio_volume_set(entity: BoschNumberEntity, value: float) -> None:
    """Store the new playback volume — no Bosch API call (browser-side level).

    The card reads this state to set video.volume; HA pushes the change to
    every open card automatically.
    """
    entity.coordinator.audio_volume[entity._cam_id] = round(value)
    entity.async_write_ha_state()


AUDIO_VOLUME_DESCRIPTION = BoschNumberEntityDescription(
    key="audio_volume",
    translation_key="audio_volume",
    native_min_value=0,
    native_max_value=100,
    native_step=5,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.CONFIG,
    unique_id_fn=lambda cam_id: f"bosch_shc_audio_volume_{cam_id.lower()}",
    value_fn=_audio_volume_value,
    available_fn=_audio_volume_available,
    set_value_fn=_audio_volume_set,
)


class BoschAudioVolumeNumber(BoschNumberEntity):
    """Card playback volume (0–100 %) for this camera's live audio."""

    entity_description = AUDIO_VOLUME_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, AUDIO_VOLUME_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# Front light brightness (0–100%).
#
# Maps to frontLightIntensity (0.0–1.0) in PUT /v11/video_inputs/{id}/lighting_override.
# Only for cameras with featureSupport.light = True (outdoor cameras).
def _front_light_intensity_value(entity: BoschNumberEntity) -> float | None:
    val = entity.coordinator.shc_state_cache.get(entity._cam_id, {}).get(
        "front_light_intensity"
    )
    if val is not None:
        return float(round(float(val) * 100))
    return None


def _front_light_intensity_available(entity: BoschNumberEntity) -> bool:
    # Gate on cache presence like the other number entities — otherwise a
    # cache-miss reports "unknown" (available + native_value None) instead of
    # "unavailable", and automations reading the level see an undefined value.
    return bool(entity.coordinator.last_update_success) and (
        entity.coordinator.shc_state_cache.get(entity._cam_id, {}).get(
            "front_light_intensity"
        )
        is not None
    )


async def _front_light_intensity_set(entity: BoschNumberEntity, value: float) -> None:
    """Set front light intensity (0-100% → 0.0-1.0 API value)."""
    intensity = round(value / 100, 2)
    success = await entity.coordinator.async_cloud_set_light_component(
        entity._cam_id, "intensity", intensity
    )
    if not success:
        # The setter (shc.py) never raises — see BoschPrivacyModeSwitch's
        # matching fix for why: a total failure across every fallback path
        # used to be invisible (state just reverted).
        _LOGGER.warning(
            "Front light intensity set to %.0f%% failed on all paths for %s "
            "— state unchanged",
            value,
            entity._cam_id[:8],
        )


FRONT_LIGHT_INTENSITY_DESCRIPTION = BoschNumberEntityDescription(
    key="front_light_intensity",
    translation_key="front_light_intensity",
    native_min_value=0,
    native_max_value=100,
    native_step=5,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.CONFIG,
    unique_id_fn=lambda cam_id: f"bosch_shc_front_light_intensity_{cam_id.lower()}",
    value_fn=_front_light_intensity_value,
    available_fn=_front_light_intensity_available,
    set_value_fn=_front_light_intensity_set,
)


class BoschFrontLightIntensityNumber(BoschNumberEntity):
    """Number entity: front light brightness (0–100%)."""

    entity_description = FRONT_LIGHT_INTENSITY_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, FRONT_LIGHT_INTENSITY_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# Lens elevation — mounting height in metres (Gen2 only).
#
# Reads from GET /v11/video_inputs/{id}/lens_elevation → {"elevation": 2.0}
# Writes via PUT /v11/video_inputs/{id}/lens_elevation → {"elevation": value}
# Used by camera for perspective correction in person detection.
def _lens_elevation_value(entity: BoschNumberEntity) -> float | None:
    val = entity.coordinator.lens_elevation_cache.get(entity._cam_id)
    return float(val) if val is not None else None


def _lens_elevation_available(entity: BoschNumberEntity) -> bool:
    return (
        bool(entity.coordinator.last_update_success)
        and entity.coordinator.lens_elevation_cache.get(entity._cam_id) is not None
    )


async def _lens_elevation_set(entity: BoschNumberEntity, value: float) -> None:
    success = await entity.coordinator.async_put_camera(
        entity._cam_id, "lens_elevation", {"elevation": round(value, 2)}
    )
    if success:
        entity.coordinator.lens_elevation_cache[entity._cam_id] = value
    entity.async_write_ha_state()


LENS_ELEVATION_DESCRIPTION = BoschNumberEntityDescription(
    key="lens_elevation",
    translation_key="lens_elevation",
    native_min_value=0.5,
    native_max_value=5.0,
    native_step=0.05,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="m",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_lens_elevation",
    value_fn=_lens_elevation_value,
    available_fn=_lens_elevation_available,
    set_value_fn=_lens_elevation_set,
)


class BoschLensElevationNumber(BoschNumberEntity):
    """Number entity: lens mounting height in meters (Gen2 only)."""

    entity_description = LENS_ELEVATION_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, LENS_ELEVATION_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# Microphone recording level 0-100% (Gen2 only).
#
# Reads from GET /v11/video_inputs/{id}/audio → {"microphoneLevel": 60, ...}
# Writes via PUT /v11/video_inputs/{id}/audio → full body with updated microphoneLevel.
def _microphone_level_value(entity: BoschNumberEntity) -> float | None:
    audio = entity.coordinator.audio_cache.get(entity._cam_id, {})
    val = audio.get("microphoneLevel")
    return float(val) if val is not None else None


def _microphone_level_available(entity: BoschNumberEntity) -> bool:
    return entity.coordinator.last_update_success and (
        entity.coordinator.audio_cache.get(entity._cam_id) is not None
    )


async def _microphone_level_set(entity: BoschNumberEntity, value: float) -> None:
    if _is_gen2_indoor(entity) and await _warn_if_privacy_on(
        entity, "Mikrofon-Lautstärke"
    ):
        return
    level = round(value)
    # Serialized on the same per-camera lock as speaker level and
    # BoschIntercomSwitch — see that description's comment.
    lock = _get_cam_lock(entity.coordinator, "_audio_config_locks", entity._cam_id)
    async with lock:
        audio = dict(entity.coordinator.audio_cache.get(entity._cam_id, {}))
        audio["microphoneLevel"] = level
        success = await entity.coordinator.async_put_camera(
            entity._cam_id, "audio", audio
        )
        if success:
            # Merge only the changed field (see speaker-level note).
            entity.coordinator.audio_cache.setdefault(entity._cam_id, {})[
                "microphoneLevel"
            ] = level
    entity.async_write_ha_state()


MICROPHONE_LEVEL_DESCRIPTION = BoschNumberEntityDescription(
    key="microphone_level",
    translation_key="microphone_level",
    native_min_value=0,
    native_max_value=100,
    native_step=5,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_mic_level",
    value_fn=_microphone_level_value,
    available_fn=_microphone_level_available,
    set_value_fn=_microphone_level_set,
)


class BoschMicrophoneLevelNumber(BoschNumberEntity):
    """Number entity: microphone recording level 0-100% (Gen2 only)."""

    entity_description = MICROPHONE_LEVEL_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, MICROPHONE_LEVEL_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
_LIGHT_SW_DEFAULT: dict[str, Any] = {
    "brightness": 0,
    "color": None,
    "whiteBalance": 0.0,
}


def _lighting_switch_body(cached: dict[str, Any]) -> dict[str, Any]:
    """Build full lighting/switch PUT body from cache (API requires all 3 groups)."""
    return {
        k: cached.get(k, _LIGHT_SW_DEFAULT)
        for k in ("frontLightSettings", "topLedLightSettings", "bottomLedLightSettings")
    }


# White balance — front light color temperature -1.0 to 1.0 (Gen2 only).
#
# -1.0 = cool/blue, 0.0 = neutral, 1.0 = warm/orange.
# Only applies to front light (top/bottom LEDs use RGB color instead).
# Reads from GET /v11/video_inputs/{id}/lighting/switch → frontLightSettings.whiteBalance
# Writes via PUT /lighting/switch with frontLightSettings only.
def _white_balance_value(entity: BoschNumberEntity) -> float | None:
    cached = entity.coordinator.lighting_switch_cache.get(entity._cam_id, {})
    front = cached.get("frontLightSettings", {})
    wb = front.get("whiteBalance")
    if wb is not None:
        entity._wb_value = wb
    return entity._wb_value


def _white_balance_available(entity: BoschNumberEntity) -> bool:
    # Gate on the lighting cache being populated — a write during the
    # pre-populate / failed-sub-fetch window would PUT zero-defaults and
    # clobber the camera's real light settings.
    return bool(entity.coordinator.last_update_success) and (
        entity.coordinator.lighting_switch_cache.get(entity._cam_id, {}).get(
            "frontLightSettings"
        )
        is not None
    )


async def _white_balance_set(entity: BoschNumberEntity, value: float) -> None:
    """Set white balance for front light — sends FULL body (API requirement)."""
    wb = round(value, 2)
    cached = entity.coordinator.lighting_switch_cache.get(entity._cam_id, {})
    body = _lighting_switch_body(cached)
    body["frontLightSettings"] = {
        **body["frontLightSettings"],
        "whiteBalance": wb,
        "color": None,
    }
    # Route through the coordinator's universal writer, which handles a 401
    # via token-refresh + retry.
    ok = await entity.coordinator.async_put_camera(
        entity._cam_id, "lighting/switch", body
    )
    if ok:
        entity._wb_value = wb
        # Merge ONLY the group we changed into the live cache (not the whole
        # snapshot) so a concurrent sibling write to a different light group
        # isn't clobbered by our stale snapshot.
        cur = entity.coordinator.lighting_switch_cache.setdefault(entity._cam_id, {})
        cur["frontLightSettings"] = body["frontLightSettings"]
        _LOGGER.debug("White balance set to %.2f for %s", wb, entity._cam_id[:8])
    else:
        _LOGGER.warning("White balance write failed for %s", entity._cam_id[:8])
    entity.async_write_ha_state()


WHITE_BALANCE_DESCRIPTION = BoschNumberEntityDescription(
    key="white_balance",
    translation_key="white_balance",
    native_min_value=-1.0,
    native_max_value=1.0,
    native_step=0.05,
    mode=NumberMode.SLIDER,
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_white_balance",
    value_fn=_white_balance_value,
    available_fn=_white_balance_available,
    set_value_fn=_white_balance_set,
)


class BoschWhiteBalanceNumber(BoschNumberEntity):
    """Number entity: front light color temperature -1.0 to 1.0 (Gen2 only)."""

    entity_description = WHITE_BALANCE_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, WHITE_BALANCE_DESCRIPTION)
        self._wb_value = None


# ─────────────────────────────────────────────────────────────────────────────
# Top/Bottom LED brightness 0-100% (Gen2 only) — shared shape, `_led_key`
# selects which lighting_switch_cache group to read/write.
def _led_brightness_value(entity: BoschNumberEntity) -> float | None:
    cached = entity.coordinator.lighting_switch_cache.get(entity._cam_id, {})
    led = cached.get(entity._led_key, {})
    val = led.get("brightness")
    if val is not None:
        entity._brightness = float(val)
    return entity._brightness


def _led_brightness_available(entity: BoschNumberEntity) -> bool:
    # Gate on the lighting cache (see white-balance note) — avoids writing
    # zero-defaults that clobber real settings before the cache is populated.
    return bool(entity.coordinator.last_update_success) and (
        entity.coordinator.lighting_switch_cache.get(entity._cam_id, {}).get(
            entity._led_key
        )
        is not None
    )


async def _led_brightness_set(entity: BoschNumberEntity, value: float) -> None:
    """Set brightness — sends FULL body with all 3 groups (API requirement)."""
    brightness = round(value)
    cached = entity.coordinator.lighting_switch_cache.get(entity._cam_id, {})
    body = _lighting_switch_body(cached)
    body[entity._led_key] = {**body[entity._led_key], "brightness": brightness}
    # Route through the coordinator's universal writer (401 → token-refresh +
    # retry).
    ok = await entity.coordinator.async_put_camera(
        entity._cam_id, "lighting/switch", body
    )
    if ok:
        entity._brightness = float(brightness)
        # Merge only the changed LED group (see white-balance note above).
        cur = entity.coordinator.lighting_switch_cache.setdefault(entity._cam_id, {})
        cur[entity._led_key] = body[entity._led_key]
        _LOGGER.debug(
            "%s brightness set to %d for %s",
            entity._led_key,
            brightness,
            entity._cam_id[:8],
        )
    else:
        _LOGGER.warning(
            "%s brightness write failed for %s",
            entity._led_key,
            entity._cam_id[:8],
        )
    entity.async_write_ha_state()


TOP_LED_BRIGHTNESS_DESCRIPTION = BoschNumberEntityDescription(
    key="top_led_brightness",
    translation_key="top_led_brightness",
    native_min_value=0,
    native_max_value=100,
    native_step=5,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_top_led_brightness",
    value_fn=_led_brightness_value,
    available_fn=_led_brightness_available,
    set_value_fn=_led_brightness_set,
)

BOTTOM_LED_BRIGHTNESS_DESCRIPTION = BoschNumberEntityDescription(
    key="bottom_led_brightness",
    translation_key="bottom_led_brightness",
    native_min_value=0,
    native_max_value=100,
    native_step=5,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_bottom_led_brightness",
    value_fn=_led_brightness_value,
    available_fn=_led_brightness_available,
    set_value_fn=_led_brightness_set,
)


class BoschTopLedBrightnessNumber(BoschNumberEntity):
    """Number entity: top LED brightness 0-100% (Gen2, oberes Licht)."""

    entity_description = TOP_LED_BRIGHTNESS_DESCRIPTION
    _led_key = "topLedLightSettings"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, TOP_LED_BRIGHTNESS_DESCRIPTION)
        self._led_key = "topLedLightSettings"
        self._brightness = None


class BoschBottomLedBrightnessNumber(BoschNumberEntity):
    """Number entity: bottom LED brightness 0-100% (Gen2, unteres Licht)."""

    entity_description = BOTTOM_LED_BRIGHTNESS_DESCRIPTION
    _led_key = "bottomLedLightSettings"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, BOTTOM_LED_BRIGHTNESS_DESCRIPTION)
        self._led_key = "bottomLedLightSettings"
        self._brightness = None


# ─────────────────────────────────────────────────────────────────────────────
# Motion-triggered light sensitivity 1-5 (Gen2 only).
#
# Reads from GET /v11/video_inputs/{id}/lighting/motion → motionLightSensitivity
# Writes via PUT /v11/video_inputs/{id}/lighting/motion with full body.
# 1 = low sensitivity, 5 = high sensitivity.
def _motion_light_sensitivity_value(entity: BoschNumberEntity) -> float | None:
    cache = entity.coordinator.motion_light_cache.get(entity._cam_id, {})
    val = cache.get("motionLightSensitivity")
    return float(val) if val is not None else None


def _motion_light_sensitivity_available(entity: BoschNumberEntity) -> bool:
    return entity.coordinator.last_update_success and bool(
        entity.coordinator.motion_light_cache.get(entity._cam_id)
    )


async def _motion_light_sensitivity_set(
    entity: BoschNumberEntity, value: float
) -> None:
    cache = dict(entity.coordinator.motion_light_cache.get(entity._cam_id, {}))
    if not cache:
        return
    cache["motionLightSensitivity"] = round(value)
    success = await entity.coordinator.async_put_camera(
        entity._cam_id, "lighting/motion", cache
    )
    if success:
        entity.coordinator.motion_light_cache[entity._cam_id] = cache
    entity.async_write_ha_state()


MOTION_LIGHT_SENSITIVITY_DESCRIPTION = BoschNumberEntityDescription(
    key="motion_light_sensitivity",
    translation_key="motion_light_sensitivity",
    native_min_value=1,
    native_max_value=5,
    native_step=1,
    mode=NumberMode.SLIDER,
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_motion_light_sensitivity",
    value_fn=_motion_light_sensitivity_value,
    available_fn=_motion_light_sensitivity_available,
    set_value_fn=_motion_light_sensitivity_set,
)


class BoschMotionLightSensitivityNumber(BoschNumberEntity):
    """Number entity: motion-triggered light sensitivity 1-5 (Gen2 only)."""

    entity_description = MOTION_LIGHT_SENSITIVITY_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator, cam_id, entry, MOTION_LIGHT_SENSITIVITY_DESCRIPTION
        )


# ─────────────────────────────────────────────────────────────────────────────
# Darkness threshold 0-100% (Gen2 only).
#
# Controls when the camera switches from day to night lighting mode.
# 0 = always day, 100 = always night.
# Reads from GET /v11/video_inputs/{id}/lighting → {"darknessThreshold": 0.47, "softLightFading": bool}
# Writes via PUT /v11/video_inputs/{id}/lighting with full body.
def _darkness_threshold_value(entity: BoschNumberEntity) -> float | None:
    cache = entity.coordinator.global_lighting_cache.get(entity._cam_id, {})
    val = cache.get("darknessThreshold")
    return round(float(val) * 100, 0) if val is not None else None


def _darkness_threshold_available(entity: BoschNumberEntity) -> bool:
    return entity.coordinator.last_update_success and bool(
        entity.coordinator.global_lighting_cache.get(entity._cam_id)
    )


async def _darkness_threshold_set(entity: BoschNumberEntity, value: float) -> None:
    cache = entity.coordinator.global_lighting_cache.get(entity._cam_id, {})
    soft_fading = cache.get("softLightFading", True)
    body = {
        "darknessThreshold": round(value / 100, 4),
        "softLightFading": soft_fading,
    }
    success = await entity.coordinator.async_put_camera(
        entity._cam_id, "lighting", body
    )
    if success:
        entity.coordinator.global_lighting_cache[entity._cam_id] = body
    entity.async_write_ha_state()


DARKNESS_THRESHOLD_DESCRIPTION = BoschNumberEntityDescription(
    key="darkness_threshold",
    translation_key="darkness_threshold",
    native_min_value=0,
    native_max_value=100,
    native_step=1,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="%",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_darkness_threshold",
    value_fn=_darkness_threshold_value,
    available_fn=_darkness_threshold_available,
    set_value_fn=_darkness_threshold_set,
)


class BoschDarknessThresholdNumber(BoschNumberEntity):
    """Number entity: darkness threshold 0-100% (Gen2 only)."""

    entity_description = DARKNESS_THRESHOLD_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, DARKNESS_THRESHOLD_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# Gen2 Indoor II — Power-LED brightness + Alarm delays + Intrusion detection
# ─────────────────────────────────────────────────────────────────────────────
# Power-LED brightness (0-4, 5 discrete steps) — white LED showing camera is powered.
#
# Maps to "Power-LED" slider in iOS app → Kamera-Funktionen.
# Distinct from Status-LED (red, recording indicator, BoschStatusLedSwitch).
# PUT /v11/video_inputs/{id}/iconLedBrightness  body: {"value": 0-4}
# Confirmed live: writing value=5 → HTTP 400
# "must be less than or equal to 4". The iOS app shows this as a percent
# slider but internally maps to 5 discrete positions (0 = off, 4 = max).
def _power_led_brightness_value(entity: BoschNumberEntity) -> float | None:
    val = entity.coordinator.icon_led_brightness_cache.get(entity._cam_id)
    return float(val) if val is not None else None


def _power_led_brightness_available(entity: BoschNumberEntity) -> bool:
    return (
        bool(entity.coordinator.last_update_success)
        and entity.coordinator.icon_led_brightness_cache.get(entity._cam_id) is not None
    )


async def _power_led_brightness_set(entity: BoschNumberEntity, value: float) -> None:
    val = round(max(0, min(4, value)))
    success = await entity.coordinator.async_put_camera(
        entity._cam_id, "iconLedBrightness", {"value": val}
    )
    if success:
        entity.coordinator.icon_led_brightness_cache[entity._cam_id] = val
    entity.async_write_ha_state()


POWER_LED_BRIGHTNESS_DESCRIPTION = BoschNumberEntityDescription(
    key="power_led_brightness",
    translation_key="power_led_brightness",
    native_min_value=0,
    native_max_value=4,
    native_step=1,
    mode=NumberMode.SLIDER,
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_power_led_brightness",
    value_fn=_power_led_brightness_value,
    available_fn=_power_led_brightness_available,
    set_value_fn=_power_led_brightness_set,
)


class BoschPowerLedBrightnessNumber(BoschNumberEntity):
    """Number: Power-LED brightness (0-4, 5 discrete steps)."""

    entity_description = POWER_LED_BRIGHTNESS_DESCRIPTION

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, POWER_LED_BRIGHTNESS_DESCRIPTION)


# ─────────────────────────────────────────────────────────────────────────────
# Alarm delays (alarm_settings integer fields) — shared shape, `_field`
# selects which alarm_settings_cache key to read/write. Privacy mode blocks
# /alarm_settings PUT with HTTP 443 on Gen2 Indoor cameras — without this
# guard the write silently fails, the cache isn't updated, and native_value
# re-reads the old value while HA's verify-timeout fires.
def _alarm_delay_value(entity: BoschNumberEntity) -> float | None:
    val = entity.coordinator.alarm_settings_cache.get(entity._cam_id, {}).get(
        entity._field
    )
    return float(val) if val is not None else None


def _alarm_delay_available(entity: BoschNumberEntity) -> bool:
    return entity.coordinator.last_update_success and bool(
        entity.coordinator.alarm_settings_cache.get(entity._cam_id, {})
    )


async def _alarm_delay_set(entity: BoschNumberEntity, value: float) -> None:
    cfg = dict(entity.coordinator.alarm_settings_cache.get(entity._cam_id, {}))
    if not cfg:
        return
    if _is_gen2_indoor(entity) and await _warn_if_privacy_on(entity, "Alarm Settings"):
        return
    cfg[entity._field] = round(value)
    success = await entity.coordinator.async_put_camera(
        entity._cam_id, "alarm_settings", cfg
    )
    if success:
        entity.coordinator.alarm_settings_cache[entity._cam_id] = cfg
        # Write-lock so the slow-tier poll doesn't revert this before the
        # cloud reflects it (mirrors the intrusion-config pattern).
        entity.coordinator.alarm_settings_set_at[entity._cam_id] = _time.monotonic()
    entity.async_write_ha_state()


ALARM_DELAY_DESCRIPTION = BoschNumberEntityDescription(
    key="alarm_delay",
    translation_key="alarm_delay",
    native_min_value=10,
    native_max_value=300,
    native_step=1,
    mode=NumberMode.BOX,
    native_unit_of_measurement="s",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_alarm_delay",
    value_fn=_alarm_delay_value,
    available_fn=_alarm_delay_available,
    set_value_fn=_alarm_delay_set,
)

ALARM_ACTIVATION_DELAY_DESCRIPTION = BoschNumberEntityDescription(
    key="alarm_activation_delay",
    translation_key="alarm_activation_delay",
    native_min_value=0,
    native_max_value=600,
    native_step=1,
    mode=NumberMode.BOX,
    native_unit_of_measurement="s",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_alarm_activation_delay",
    value_fn=_alarm_delay_value,
    available_fn=_alarm_delay_available,
    set_value_fn=_alarm_delay_set,
)

PRE_ALARM_DELAY_DESCRIPTION = BoschNumberEntityDescription(
    key="pre_alarm_delay",
    translation_key="pre_alarm_delay",
    native_min_value=0,
    native_max_value=300,
    native_step=1,
    mode=NumberMode.BOX,
    native_unit_of_measurement="s",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_prealarm_delay",
    value_fn=_alarm_delay_value,
    available_fn=_alarm_delay_available,
    set_value_fn=_alarm_delay_set,
)


class BoschAlarmDelayNumber(BoschNumberEntity):
    """Number: siren duration (alarm_settings.alarmDelayInSeconds).

    How long the 75 dB siren stays active when triggered.
    Observed range from capture: 52–76s.
    """

    entity_description = ALARM_DELAY_DESCRIPTION
    _field = "alarmDelayInSeconds"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, ALARM_DELAY_DESCRIPTION)
        self._field = "alarmDelayInSeconds"


class BoschAlarmActivationDelayNumber(BoschNumberEntity):
    """Number: siren activation delay (alarm_settings.alarmActivationDelaySeconds).

    Time between detection and siren activation. Observed: 1–180s.
    """

    entity_description = ALARM_ACTIVATION_DELAY_DESCRIPTION
    _field = "alarmActivationDelaySeconds"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, ALARM_ACTIVATION_DELAY_DESCRIPTION)
        self._field = "alarmActivationDelaySeconds"


class BoschPreAlarmDelayNumber(BoschNumberEntity):
    """Number: pre-alarm duration (alarm_settings.preAlarmDelayInSeconds).

    How long the LED warning stays active before the siren fires.
    Observed: 30–38s.
    """

    entity_description = PRE_ALARM_DELAY_DESCRIPTION
    _field = "preAlarmDelayInSeconds"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, PRE_ALARM_DELAY_DESCRIPTION)
        self._field = "preAlarmDelayInSeconds"


# ─────────────────────────────────────────────────────────────────────────────
# Gen2 — Intrusion Detection Number Entities
#
# Shared shape (like the alarm-delay group above): `_field` selects which
# intrusion_config_cache key to read/write. Clamp bounds are taken from the
# entity's own description (native_min_value/native_max_value) so they can
# never drift from the declared range. Write-lock timestamp
# intrusion_config_set_at is set after successful PUT to prevent the
# slow-tier poll from reverting the optimistic cache update. Available for
# both Gen2 Indoor II (HOME_Eyes_Indoor) and Gen2 Outdoor II
# (HOME_Eyes_Outdoor) — intrusion detection is present on both hardware
# variants.
def _intrusion_value(entity: BoschNumberEntity) -> float | None:
    cfg = entity.coordinator.intrusion_config_cache.get(entity._cam_id, {})
    val = cfg.get(entity._field)
    return float(val) if val is not None else None


def _intrusion_available(entity: BoschNumberEntity) -> bool:
    return entity.coordinator.last_update_success and bool(
        entity.coordinator.intrusion_config_cache.get(entity._cam_id)
    )


async def _intrusion_set(entity: BoschNumberEntity, value: float) -> None:
    cfg = dict(entity.coordinator.intrusion_config_cache.get(entity._cam_id, {}))
    if not cfg:
        return
    lo = entity.entity_description.native_min_value
    hi = entity.entity_description.native_max_value
    assert lo is not None and hi is not None
    cfg[entity._field] = round(max(lo, min(hi, value)))
    success = await entity.coordinator.async_put_camera(
        entity._cam_id, "intrusionDetectionConfig", cfg
    )
    if success:
        entity.coordinator.intrusion_config_cache[entity._cam_id] = cfg
        entity.coordinator.intrusion_config_set_at[entity._cam_id] = _time.monotonic()
        _LOGGER.debug(
            "Intrusion %s set to %s for %s",
            entity._field,
            cfg[entity._field],
            entity._cam_id[:8],
        )
    else:
        _LOGGER.warning(
            "Failed to set intrusion %s for %s", entity._field, entity._cam_id[:8]
        )
    entity.async_write_ha_state()


INTRUSION_SENSITIVITY_DESCRIPTION = BoschNumberEntityDescription(
    key="intrusion_sensitivity",
    translation_key="intrusion_sensitivity",
    native_min_value=0,
    native_max_value=7,
    native_step=1,
    mode=NumberMode.SLIDER,
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_intrusion_sensitivity",
    value_fn=_intrusion_value,
    available_fn=_intrusion_available,
    set_value_fn=_intrusion_set,
)

INTRUSION_DISTANCE_DESCRIPTION = BoschNumberEntityDescription(
    key="intrusion_distance",
    translation_key="intrusion_distance",
    native_min_value=1,
    native_max_value=8,
    native_step=1,
    mode=NumberMode.SLIDER,
    native_unit_of_measurement="m",
    entity_category=EntityCategory.CONFIG,
    entity_registry_enabled_default=True,
    unique_id_fn=lambda cam_id: f"bosch_shc_camera_{cam_id}_intrusion_distance",
    value_fn=_intrusion_value,
    available_fn=_intrusion_available,
    set_value_fn=_intrusion_set,
)


class BoschIntrusionSensitivityNumber(BoschNumberEntity):
    """Number: intrusion detection sensitivity 0–7 (Gen2 only).

    FW 9.40+ raised the range from 0–5 to 0–7 (confirmed live: value=3 seen).
    """

    entity_description = INTRUSION_SENSITIVITY_DESCRIPTION
    _field = "sensitivity"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, INTRUSION_SENSITIVITY_DESCRIPTION)
        self._field = "sensitivity"


class BoschIntrusionDistanceNumber(BoschNumberEntity):
    """Number: intrusion detection range in metres 1–8 (Gen2 only).

    API rejects distance > 8 with HTTP 400 (verified live on FW 9.40.102).
    """

    entity_description = INTRUSION_DISTANCE_DESCRIPTION
    _field = "distance"

    def __init__(self, coordinator: Any, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry, INTRUSION_DISTANCE_DESCRIPTION)
        self._field = "distance"
