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

STREAM_MODE_OPTIONS = ["Auto (Lokal → Cloud)", "Nur Lokal", "Nur Cloud"]
STREAM_MODE_MAP     = {
    "Auto (Lokal → Cloud)": "auto",
    "Nur Lokal":            "local",
    "Nur Cloud":            "remote",
}
STREAM_MODE_MAP_REVERSE = {v: k for k, v in STREAM_MODE_MAP.items()}

QUALITY_OPTIONS = ["Auto", "Hoch (30 Mbps)", "Niedrig (1.9 Mbps)"]
QUALITY_MAP = {
    "Auto":              "auto",
    "Hoch (30 Mbps)":   "high",
    "Niedrig (1.9 Mbps)": "low",
}
QUALITY_MAP_REVERSE = {v: k for k, v in QUALITY_MAP.items()}

MOTION_SENSITIVITY_OPTIONS = ["SUPER_HIGH", "HIGH", "MEDIUM_HIGH", "MEDIUM_LOW", "LOW", "OFF"]

DETECTION_MODE_OPTIONS = ["ALL_MOTIONS", "ONLY_HUMANS", "ZONES"]

FCM_PUSH_MODE_OPTIONS = ["Auto", "Android", "iOS", "Polling"]
FCM_PUSH_MODE_MAP = {"Auto": "auto", "Android": "android", "iOS": "ios", "Polling": "polling"}
FCM_PUSH_MODE_MAP_REVERSE = {v: k for k, v in FCM_PUSH_MODE_MAP.items()}


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
        self._attr_name      = f"Bosch {self._cam_title} Video Quality"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_video_quality"

    async def async_added_to_hass(self) -> None:
        """Restore last quality selection after HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in QUALITY_MAP:
            quality_key = QUALITY_MAP[last_state.state]
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
        """Return the current quality label."""
        quality_key = self.coordinator.get_quality(self._cam_id)
        return QUALITY_MAP_REVERSE.get(quality_key, "Auto")

    async def async_select_option(self, option: str) -> None:
        """Handle quality selection — update coordinator preference and reconnect stream."""
        quality_key = QUALITY_MAP.get(option, "auto")
        self.coordinator.set_quality(self._cam_id, quality_key)
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

        self._attr_name      = f"Bosch {self._cam_title} Motion Sensitivity"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_motion_sensitivity_select"

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
        if val in MOTION_SENSITIVITY_OPTIONS:
            return val
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
        # Read current enabled state (preserve it; default True if unknown)
        settings = self.coordinator.motion_settings(self._cam_id)
        enabled  = settings.get("enabled", True)
        success  = await self.coordinator.async_put_camera(
            self._cam_id,
            "motion",
            {"enabled": enabled, "motionAlarmConfiguration": option},
        )
        if success:
            # Optimistically update coordinator data so UI reflects immediately
            motion_data = self.coordinator.data.get(self._cam_id, {}).get("motion", {})
            motion_data["motionAlarmConfiguration"] = option
            if self._cam_id in self.coordinator.data:
                self.coordinator.data[self._cam_id]["motion"] = motion_data
            _LOGGER.debug("Motion sensitivity set to %s for %s", option, self._cam_id)
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
        self._attr_name      = "Bosch Camera FCM Push Mode"
        self._attr_unique_id = "bosch_shc_camera_fcm_push_mode"

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
    def current_option(self) -> str:
        mode = get_options(self._entry).get("fcm_push_mode", "auto")
        return FCM_PUSH_MODE_MAP_REVERSE.get(mode, "Auto")

    async def async_select_option(self, option: str) -> None:
        """Handle push mode selection — update options and restart FCM."""
        mode_key = FCM_PUSH_MODE_MAP.get(option, "auto")
        # Update the integration options
        new_options = dict(self._entry.options)
        new_options["fcm_push_mode"] = mode_key
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
        self._attr_name      = "Bosch Camera Stream Modus"
        self._attr_unique_id = "bosch_shc_camera_stream_mode"

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
        """Return the current stream mode label."""
        # In-memory override takes priority; fall back to options default
        mode = self.coordinator._stream_type_override
        if mode is None:
            mode = get_options(self._entry).get("stream_connection_type", "auto")
        return STREAM_MODE_MAP_REVERSE.get(mode, "Auto (Lokal → Cloud)")

    async def async_select_option(self, option: str) -> None:
        """Handle stream mode selection — update in-memory preference immediately."""
        mode = STREAM_MODE_MAP.get(option, "auto")
        self.coordinator._stream_type_override = mode
        _LOGGER.info("Stream mode set to %s (%s)", option, mode)
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
        self._attr_name      = f"Bosch {self._cam_title} Erkennungsmodus"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_detection_mode"

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
        if val in DETECTION_MODE_OPTIONS:
            return val
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
        cfg = dict(self.coordinator._intrusion_config_cache.get(self._cam_id, {}))
        if not cfg:
            return
        cfg["detectionMode"] = option
        success = await self.coordinator.async_put_camera(
            self._cam_id, "intrusionDetectionConfig", cfg
        )
        if success:
            self.coordinator._intrusion_config_cache[self._cam_id] = cfg
            _LOGGER.debug("Detection mode set to %s for %s", option, self._cam_id[:8])
        else:
            _LOGGER.warning("Failed to set detection mode for %s", self._cam_id[:8])
        self.async_write_ha_state()
