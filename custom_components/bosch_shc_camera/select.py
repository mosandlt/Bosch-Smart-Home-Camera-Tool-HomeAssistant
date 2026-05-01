"""Select entities for Bosch Smart Home Camera integration.

Provides:
  - BoschVideoQualitySelect: dropdown to choose streaming quality
  - BoschMotionSensitivitySelect: dropdown to set motion detection sensitivity
    (SUPER_HIGH / HIGH / MEDIUM / MEDIUM_LOW / LOW)
    Reads from coordinator.motion_settings(cam_id)["motionAlarmConfiguration"].
    Writes via PUT /v11/video_inputs/{id}/motion.
    Disabled by default.
"""
from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from . import BoschCameraCoordinator, DOMAIN, get_options

_LOGGER = logging.getLogger(__name__)

STREAM_MODE_OPTIONS = ["auto", "local", "remote"]

QUALITY_OPTIONS = ["auto", "high", "low"]

MOTION_SENSITIVITY_OPTIONS = ["super_high", "high", "medium_high", "medium_low", "low", "off"]
SENSITIVITY_TO_API = {k: k.upper() for k in MOTION_SENSITIVITY_OPTIONS}

DETECTION_MODE_OPTIONS = ["all_motions", "only_humans", "zones"]
DETECTION_TO_API = {k: k.upper() for k in DETECTION_MODE_OPTIONS}

FCM_PUSH_MODE_OPTIONS = ["auto", "android", "ios", "polling"]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BoschCameraCoordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = []
    for cam_id in coordinator.data:
        entities.append(BoschVideoQualitySelect(coordinator, cam_id, config_entry))
        entities.append(BoschMotionSensitivitySelect(coordinator, cam_id, config_entry))
        # Gen2-only: detection mode select
        cam_info = coordinator.data[cam_id].get("info", {})
        hw = cam_info.get("hardwareVersion", "CAMERA")
        from .models import get_model_config
        if get_model_config(hw).generation >= 2:
            entities.append(BoschDetectionModeSelect(coordinator, cam_id, config_entry))
    # Integration-level selects (one per integration, not per camera)
    first_cam_id = next(iter(coordinator.data), None)
    if first_cam_id:
        entities.append(BoschFcmPushModeSelect(coordinator, first_cam_id, config_entry))
        entities.append(BoschStreamModeSelect(coordinator, first_cam_id, config_entry))
    async_add_entities(entities)


class BoschVideoQualitySelect(CoordinatorEntity, SelectEntity, RestoreEntity):
    """Select entity to choose the RTSPS stream quality (inst + highQualityVideo)."""

    _attr_icon = "mdi:video-high-definition"
    _attr_options = QUALITY_OPTIONS

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        cam_data = coordinator.data.get(cam_id, {})
        cam_info = cam_data.get("info", {})
        self._cam_title = cam_info.get("title", cam_id)
        self._entry = entry
        self._attr_name            = f"Bosch {self._cam_title} Video Quality"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_video_quality"
        self._attr_translation_key = "video_quality"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        """Restore last quality selection after HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state:
            saved = last_state.state
            # Backward compat: old states were display text like "Auto"
            _LEGACY_MAP = {"Auto": "auto", "Hoch (30 Mbps)": "high", "Niedrig (1.9 Mbps)": "low"}
            quality_key = _LEGACY_MAP.get(saved, saved if saved in QUALITY_OPTIONS else None)
            if quality_key:
                self.coordinator.set_quality(self._cam_id, quality_key)
                _LOGGER.debug("Restored quality %s for %s", quality_key, self._cam_id)

    @property
    def device_info(self):
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model": cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version": cam_info.get("firmwareVersion", ""),
        }

    @property
    def current_option(self) -> str:
        """Return the current quality key."""
        quality_key = self.coordinator.get_quality(self._cam_id)
        return quality_key if quality_key in QUALITY_OPTIONS else "auto"

    async def async_select_option(self, option: str) -> None:
        """Handle quality selection — update coordinator preference and reconnect stream."""
        self.coordinator.set_quality(self._cam_id, option)
        # If stream is currently active, reconnect with new quality
        live = self.coordinator.data.get(self._cam_id, {}).get("live", {})
        if live.get("rtspsUrl") or live.get("proxyUrl"):
            try:
                new_live = await self.coordinator.try_live_connection(self._cam_id)
                if new_live:
                    self.coordinator.data[self._cam_id]["live"] = new_live
                    await self.coordinator._register_go2rtc_stream(
                        self._cam_id, new_live.get("rtspsUrl", "")
                    )
            except Exception:
                pass
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschMotionSensitivitySelect(CoordinatorEntity, SelectEntity):
    """Select entity to set motion detection sensitivity for a camera.

    Options: SUPER_HIGH / HIGH / MEDIUM / MEDIUM_LOW / LOW
    Reads from coordinator.motion_settings(cam_id)["motionAlarmConfiguration"].
    Writes via PUT /v11/video_inputs/{id}/motion {"enabled": true, "motionAlarmConfiguration": value}.
    Disabled by default — enable in Settings → Entities.
    """

    _attr_icon    = "mdi:motion-sensor"
    _attr_options = MOTION_SENSITIVITY_OPTIONS
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

        cam_data = coordinator.data.get(cam_id, {})
        cam_info = cam_data.get("info", {})
        self._cam_title = cam_info.get("title", cam_id)

        self._attr_name            = f"Bosch {self._cam_title} Motion Sensitivity"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_motion_sensitivity_select"
        self._attr_translation_key = "motion_sensitivity"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self) -> dict:
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name":        f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":       cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version":  cam_info.get("firmwareVersion", ""),
        }

    @property
    def current_option(self) -> str | None:
        """Return the current motion sensitivity level."""
        settings = self.coordinator.motion_settings(self._cam_id)
        val = settings.get("motionAlarmConfiguration")
        if val:
            lower = val.lower()
            if lower in MOTION_SENSITIVITY_OPTIONS:
                return lower
        return None

    @property
    def available(self) -> bool:
        """Available only when motion settings have been fetched (slow tier)."""
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator.motion_settings(self._cam_id))
        )

    async def async_select_option(self, option: str) -> None:
        """Write the new sensitivity level to the camera via cloud API."""
        if option not in MOTION_SENSITIVITY_OPTIONS:
            _LOGGER.warning("Invalid motion sensitivity option: %s", option)
            return
        from .switch import _is_gen2_indoor, _warn_if_privacy_on
        if _is_gen2_indoor(self) and await _warn_if_privacy_on(self, "Bewegungsempfindlichkeit"):
            return
        api_value = SENSITIVITY_TO_API[option]
        settings = self.coordinator.motion_settings(self._cam_id)
        enabled  = settings.get("enabled", True)
        success  = await self.coordinator.async_put_camera(
            self._cam_id,
            "motion",
            {"enabled": enabled, "motionAlarmConfiguration": api_value},
        )
        if success:
            motion_data = self.coordinator.data.get(self._cam_id, {}).get("motion", {})
            motion_data["motionAlarmConfiguration"] = api_value
            if self._cam_id in self.coordinator.data:
                self.coordinator.data[self._cam_id]["motion"] = motion_data
            _LOGGER.debug("Motion sensitivity set to %s for %s", api_value, self._cam_id)
        else:
            _LOGGER.warning("Failed to set motion sensitivity for %s", self._cam_id)
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschFcmPushModeSelect(CoordinatorEntity, SelectEntity):
    """Select entity to choose the FCM push notification mode.

    Options: Auto (ios→android→polling fallback), Android, iOS, Polling.
    When changed: restarts FCM with the new mode.
    One per integration (not per camera).
    """

    _attr_icon    = "mdi:cellphone-arrow-down"
    _attr_options = FCM_PUSH_MODE_OPTIONS
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry
        self._attr_name            = "Bosch Camera FCM Push Mode"
        self._attr_unique_id       = "bosch_shc_camera_fcm_push_mode"
        self._attr_translation_key = "fcm_push_mode"

    @property
    def device_info(self) -> dict:
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        cam_title = cam_info.get("title", self._cam_id)
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": f"Bosch {cam_title}",
            "manufacturer": "Bosch",
            "model": cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version": cam_info.get("firmwareVersion", ""),
        }

    @property
    def available(self) -> bool:
        # Gating: dropdown is wirkungslos solange Master-Switch enable_fcm_push aus ist.
        # Unavailable signalisiert dem User explizit dass erst die Integration-Option
        # gesetzt werden muss, bevor der Push-Mode irgendetwas tut.
        if not super().available:
            return False
        return bool(self.coordinator.options.get("enable_fcm_push", False))

    @property
    def current_option(self) -> str:
        mode = get_options(self._entry).get("fcm_push_mode", "auto")
        return mode if mode in FCM_PUSH_MODE_OPTIONS else "auto"

    async def async_select_option(self, option: str) -> None:
        """Handle push mode selection — update options and restart FCM."""
        # Update the integration options
        new_options = dict(self._entry.options)
        new_options["fcm_push_mode"] = option
        self.hass.config_entries.async_update_entry(
            self._entry, options=new_options,
        )
        # Restart FCM with new mode
        await self.coordinator.async_stop_fcm_push()
        self.coordinator._fcm_push_mode = "unknown"
        if self.coordinator.options.get("enable_fcm_push", False):
            self.hass.async_create_task(self.coordinator.async_start_fcm_push())
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschStreamModeSelect(CoordinatorEntity, SelectEntity):
    """Select entity to choose the live stream connection mode.

    Options:
      "Auto (Lokal → Cloud)" — try LOCAL first, fall back to REMOTE cloud proxy
      "Nur Lokal"            — direct LAN only (no internet required)
      "Nur Cloud"            — cloud proxy only (always REMOTE)

    Changes _stream_type_override in-memory — no integration reload needed.
    Takes effect on the next live stream activation.
    One per integration (not per camera).
    """

    _attr_icon             = "mdi:home-network"
    _attr_options          = STREAM_MODE_OPTIONS
    _attr_entity_category  = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry
        cam_info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = cam_info.get("title", cam_id)
        self._attr_name            = "Bosch Camera Stream Modus"
        self._attr_unique_id       = "bosch_shc_camera_stream_mode"
        self._attr_translation_key = "stream_mode"

    @property
    def device_info(self) -> dict:
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name":        f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":       cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version":  cam_info.get("firmwareVersion", ""),
        }

    @property
    def current_option(self) -> str:
        """Return the current stream mode key."""
        mode = self.coordinator._stream_type_override
        if mode is None:
            mode = get_options(self._entry).get("stream_connection_type", "auto")
        return mode if mode in STREAM_MODE_OPTIONS else "auto"

    async def async_select_option(self, option: str) -> None:
        """Handle stream mode selection — update in-memory preference immediately."""
        self.coordinator._stream_type_override = option
        _LOGGER.info("Stream mode set to %s", option)
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschDetectionModeSelect(CoordinatorEntity, SelectEntity):
    """Select entity: intrusion detection mode (Gen2 only).

    Options: ALL_MOTIONS / PERSON_DETECTION
    Reads from coordinator._intrusion_config_cache[cam_id]["detectionMode"].
    Writes via PUT /v11/video_inputs/{id}/intrusionDetectionConfig.
    """

    _attr_icon    = "mdi:shield-home-outline"
    _attr_options = DETECTION_MODE_OPTIONS

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry
        cam_data = coordinator.data.get(cam_id, {})
        cam_info = cam_data.get("info", {})
        self._cam_title = cam_info.get("title", cam_id)
        self._attr_name            = f"Bosch {self._cam_title} Erkennungsmodus"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_detection_mode"
        self._attr_translation_key = "detection_mode"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def device_info(self) -> dict:
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name":        f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":       cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version":  cam_info.get("firmwareVersion", ""),
        }

    @property
    def current_option(self) -> str | None:
        cfg = self.coordinator._intrusion_config_cache.get(self._cam_id, {})
        val = cfg.get("detectionMode")
        if val:
            lower = val.lower()
            if lower in DETECTION_MODE_OPTIONS:
                return lower
        return None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator._intrusion_config_cache.get(self._cam_id))
        )

    async def async_select_option(self, option: str) -> None:
        if option not in DETECTION_MODE_OPTIONS:
            return
        from .switch import _warn_if_privacy_on
        if await _warn_if_privacy_on(self, "Erkennungsmodus"):
            return
        api_value = DETECTION_TO_API[option]
        cfg = dict(self.coordinator._intrusion_config_cache.get(self._cam_id, {}))
        if not cfg:
            return
        cfg["detectionMode"] = api_value
        success = await self.coordinator.async_put_camera(
            self._cam_id, "intrusionDetectionConfig", cfg
        )
        if success:
            self.coordinator._intrusion_config_cache[self._cam_id] = cfg
            _LOGGER.debug("Detection mode set to %s for %s", api_value, self._cam_id[:8])
        else:
            _LOGGER.warning("Failed to set detection mode for %s", self._cam_id[:8])
        self.async_write_ha_state()
