"""Bosch Smart Home Camera — Switch Platform.

Creates switch entities per camera:
  • {Name} Live Stream  — ON = live stream active, OFF = stopped
                          Turning ON: opens PUT /connection REMOTE, sets stream_source
                          to rtsps://:443 (30fps H.264 + AAC audio).
                          Stays ON until manually turned OFF.
                          Turning OFF clears the session immediately.
                          Default: OFF (no live stream on startup).

  • {Name} Audio        — ON = stream includes audio (AAC), OFF = video-only
                          Affects the rtsps:// URL used by go2rtc / WebRTC.
                          If live stream is active, re-opens the connection.
                          Default: OFF (silent stream; avoids unexpected audio).

  • {Name} Privacy Mode — ON = privacy mode active (camera off / lens covered).
                          Uses Bosch cloud API: PUT /v11/video_inputs/{id}/privacy.
                          No SHC local API needed — works without SHC configured.

  • {Name} Camera Light — ON = camera indicator LED on, OFF = LED off.
                          Only available if camera supports light (featureSupport.light).
                          Uses Bosch cloud API: PUT /v11/video_inputs/{id}/lighting_override.
                          No SHC local API needed — works without SHC configured.

  • {Name} Notifications — ON = notifications enabled (FOLLOW_CAMERA_SCHEDULE or ON_CAMERA_SCHEDULE),
                           OFF = ALWAYS_OFF.
                           Uses Bosch cloud API: PUT /v11/video_inputs/{id}/enable_notifications.
                           State is read from /v11/video_inputs (notificationsEnabledStatus field).
                           Three-state aware: both FOLLOW_CAMERA_SCHEDULE and ON_CAMERA_SCHEDULE
                           are treated as ON. Turning ON always sends FOLLOW_CAMERA_SCHEDULE.
                           No SHC local API needed.
"""

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, get_options, CLOUD_API

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
class _BoschSwitchBase(CoordinatorEntity, SwitchEntity):
    """Shared base for Bosch camera switch entities."""

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
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities for each camera."""
    opts = get_options(config_entry)
    if not opts.get("enable_snapshot_button", True):
        return

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = []
    for cam_id in coordinator.data:
        cam_info = coordinator.data[cam_id].get("info", {})
        entities.append(BoschLiveStreamSwitch(coordinator, cam_id, config_entry))
        entities.append(BoschAudioSwitch(coordinator, cam_id, config_entry))
        # Privacy mode — always available via cloud API (no SHC needed)
        entities.append(BoschPrivacyModeSwitch(coordinator, cam_id, config_entry))
        # Camera light — only if cloud API reports featureSupport.light = True.
        # Do NOT fall back to "SHC configured" — cameras without a physical light
        # (e.g. CAMERA_360 indoor) would otherwise get a spurious light switch.
        has_light = cam_info.get("featureSupport", {}).get("light", False)
        if has_light:
            entities.append(BoschCameraLightSwitch(coordinator, cam_id, config_entry))
        # Notifications — available for all cameras via cloud API
        entities.append(BoschNotificationsSwitch(coordinator, cam_id, config_entry))
        # Motion detection toggle — available for all cameras via cloud API
        entities.append(BoschMotionEnabledSwitch(coordinator, cam_id, config_entry))
        # Record sound toggle — available for all cameras via cloud API
        entities.append(BoschRecordSoundSwitch(coordinator, cam_id, config_entry))
        # Auto-follow — only for cameras with panLimit > 0 (CAMERA_360)
        pan_limit = cam_info.get("featureSupport", {}).get("panLimit", 0)
        if pan_limit:
            entities.append(BoschAutoFollowSwitch(coordinator, cam_id, config_entry))
        # Intercom (two-way audio) — disabled by default
        entities.append(BoschIntercomSwitch(coordinator, cam_id, config_entry))
        # Privacy sound — only for cameras where the endpoint returns 200 (not 442)
        # Indoor CAMERA_360 supports it, outdoor CAMERA_EYES returns 442
        hw_version = cam_info.get("hardwareVersion", "")
        if hw_version == "CAMERA_360":
            entities.append(BoschPrivacySoundSwitch(coordinator, cam_id, config_entry))
        # Timestamp overlay — available for all cameras
        entities.append(BoschTimestampSwitch(coordinator, cam_id, config_entry))
        # Notification type toggles — per-type on/off for movement, person, audio, trouble, cameraAlarm
        for ntype in ("movement", "person", "audio", "trouble", "cameraAlarm"):
            entities.append(BoschNotificationTypeSwitch(coordinator, cam_id, config_entry, ntype))
    async_add_entities(entities, update_before_add=False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschLiveStreamSwitch(_BoschSwitchBase):
    """Switch: ON = live stream active, OFF = stopped.

    State is driven by the coordinator's _live_connections dict.
    Stays ON until manually turned OFF or HA restarts.
    Default state on HA startup: OFF.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Live Stream"
        self._attr_unique_id = f"bosch_shc_live_{cam_id.lower()}"

    @property
    def is_on(self) -> bool:
        """True if a live session is currently active."""
        return self._cam_id in self.coordinator._live_connections

    @property
    def icon(self) -> str:
        return "mdi:video-wireless" if self.is_on else "mdi:video-wireless-outline"

    @property
    def extra_state_attributes(self) -> dict:
        live = self.coordinator._live_connections.get(self._cam_id, {})
        conn_type = live.get("_connection_type", "REMOTE") if live else ""
        return {
            "connection_type":  conn_type,
            "rtsps_url":        live.get("rtspsUrl", ""),
            "proxy_snap_url":   live.get("proxyUrl", ""),
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Open a new live proxy connection."""
        _LOGGER.info("Live stream ON for %s", self._cam_title)
        result = await self.coordinator.try_live_connection(self._cam_id)
        if result:
            _LOGGER.info(
                "Live stream active for %s — %s",
                self._cam_title, result.get("rtspsUrl", ""),
            )
        else:
            _LOGGER.warning("Live stream failed for %s — check HA logs", self._cam_title)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Clear the live session immediately."""
        _LOGGER.info("Live stream OFF for %s", self._cam_title)
        self.coordinator._live_connections.pop(self._cam_id, None)
        self.coordinator._live_opened_at.pop(self._cam_id, None)
        # Update state immediately so the UI reflects OFF without waiting for
        # the go2rtc unregister (up to 3s) + coordinator refresh.
        self.async_write_ha_state()
        await self.coordinator._unregister_go2rtc_stream(self._cam_id)
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ─────────────────────────────────────────────────────────────────────────────
class BoschAudioSwitch(_BoschSwitchBase):
    """Switch: ON = live stream includes audio (AAC), OFF = video-only.

    Default: OFF — silent stream. Turn ON to enable AAC-LC 16kHz mono audio.
    If the live stream is currently active, toggling re-opens the connection
    so the new audio setting takes effect immediately.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Audio"
        self._attr_unique_id = f"bosch_shc_audio_{cam_id.lower()}"
        # Default: audio OFF (silent stream)
        coordinator._audio_enabled.setdefault(cam_id, False)

    @property
    def is_on(self) -> bool:
        return self.coordinator._audio_enabled.get(self._cam_id, False)

    @property
    def icon(self) -> str:
        return "mdi:volume-high" if self.is_on else "mdi:volume-off"

    async def async_turn_on(self, **kwargs) -> None:
        """Enable audio on the live stream."""
        _LOGGER.info("Audio ON for %s", self._cam_title)
        self.coordinator._audio_enabled[self._cam_id] = True
        await self._apply_audio_change()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable audio on the live stream (video-only)."""
        _LOGGER.info("Audio OFF for %s", self._cam_title)
        self.coordinator._audio_enabled[self._cam_id] = False
        await self._apply_audio_change()

    async def _apply_audio_change(self) -> None:
        """Re-open the live connection if active so the audio change takes effect."""
        if self._cam_id in self.coordinator._live_connections:
            _LOGGER.info(
                "Re-opening live connection for %s to apply audio change", self._cam_title
            )
            await self.coordinator.try_live_connection(self._cam_id)
        else:
            self.hass.async_create_task(self.coordinator.async_request_refresh())


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraLightSwitch(_BoschSwitchBase):
    """Switch: ON = camera indicator LED on, OFF = LED off.

    Only registered for cameras with featureSupport.light = True (from cloud API).
    State is read from cloud API featureStatus (frontIlluminatorInGeneralLightOn).
    Write (turn on/off) uses Bosch cloud API: PUT /v11/video_inputs/{id}/lighting_override.
    No SHC local API needed — works without SHC configured.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Camera Light"
        self._attr_unique_id = f"bosch_shc_light_{cam_id.lower()}"
        self._attr_icon      = "mdi:led-on"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._shc_state_cache.get(self._cam_id, {}).get("camera_light")

    @property
    def available(self) -> bool:
        """Available when coordinator is running and camera has light support.

        Light state comes from cloud API featureStatus (no SHC needed).
        Control uses cloud API (PUT /v11/video_inputs/{id}/lighting_override).
        """
        return self.coordinator.last_update_success

    @property
    def icon(self) -> str:
        return "mdi:led-on" if self.is_on else "mdi:led-off"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_camera_light(self._cam_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_camera_light(self._cam_id, False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschPrivacyModeSwitch(_BoschSwitchBase):
    """Switch: ON = privacy mode active (camera off / shutter closed), OFF = camera active.

    Uses the Bosch cloud API: PUT /v11/video_inputs/{id}/privacy
    No SHC local API required — works without SHC configured.
    State is read from the /v11/video_inputs response (privacyMode field).
    Falls back to SHC API if cloud call fails and SHC is configured.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Privacy Mode"
        self._attr_unique_id = f"bosch_shc_privacy_{cam_id.lower()}"

    @property
    def is_on(self) -> bool | None:
        """True when privacy mode is ON (camera blocked/shuttered).

        Read from cloud API response (privacyMode field in /v11/video_inputs).
        Available immediately without SHC configured.
        """
        return self.coordinator._shc_state_cache.get(self._cam_id, {}).get("privacy_mode")

    @property
    def available(self) -> bool:
        """Available as soon as the coordinator has fetched camera data.

        Unlike camera light (which needs SHC), privacy state comes from the
        cloud API response — no SHC configuration needed.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator._shc_state_cache.get(self._cam_id, {}).get("privacy_mode") is not None
        )

    @property
    def icon(self) -> str:
        return "mdi:eye-off" if self.is_on else "mdi:eye"

    @property
    def extra_state_attributes(self) -> dict:
        """Extra attributes including RCP-sourced privacy state for cross-validation.

        rcp_state: privacy mask byte[1] from RCP command 0x0d00 (1=ON, 0=OFF, None=unavailable).
        This supplements the REST API privacy state with a direct camera-side reading.
        The switch logic (is_on) remains driven by the REST API only.
        """
        rcp_raw = self.coordinator._rcp_privacy_cache.get(self._cam_id)
        return {
            "rcp_state": rcp_raw,
        }

    async def async_turn_on(self, **kwargs) -> None:
        """Enable privacy mode — camera turns off / shutter closes."""
        await self.coordinator.async_cloud_set_privacy_mode(self._cam_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable privacy mode — camera turns back on."""
        await self.coordinator.async_cloud_set_privacy_mode(self._cam_id, False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschNotificationsSwitch(_BoschSwitchBase):
    """Switch: ON = notifications enabled (FOLLOW_CAMERA_SCHEDULE or ON_CAMERA_SCHEDULE), OFF = ALWAYS_OFF.

    Three-state aware: the API can return FOLLOW_CAMERA_SCHEDULE, ON_CAMERA_SCHEDULE, or ALWAYS_OFF.
    Both "ON" variants are treated as switch state = True.
    Turning ON always sends FOLLOW_CAMERA_SCHEDULE.

    Uses Bosch cloud API: PUT /v11/video_inputs/{id}/enable_notifications.
    State is read from the /v11/video_inputs response (notificationsEnabledStatus field).
    No SHC local API required.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Notifications"
        self._attr_unique_id = f"bosch_shc_notifications_{cam_id.lower()}"

    # Values that map to ON state (notifications active in some form)
    _NOTIFICATIONS_ON_STATES = {"FOLLOW_CAMERA_SCHEDULE", "ON_CAMERA_SCHEDULE"}

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator._shc_state_cache.get(self._cam_id, {}).get("notifications_status")
        if status is None:
            return None
        return status in self._NOTIFICATIONS_ON_STATES

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._shc_state_cache.get(self._cam_id, {}).get("notifications_status") is not None
        )

    @property
    def icon(self) -> str:
        return "mdi:bell" if self.is_on else "mdi:bell-off"

    async def async_turn_on(self, **kwargs) -> None:
        """Enable notifications (follow camera schedule)."""
        await self.coordinator.async_cloud_set_notifications(self._cam_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable notifications (always off)."""
        await self.coordinator.async_cloud_set_notifications(self._cam_id, False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschMotionEnabledSwitch(_BoschSwitchBase):
    """Toggle motion detection on/off.

    KNOWN LIMITATION: The camera firmware has an internal IVA rules engine that
    enforces motion detection settings independently. Changes via this switch
    (cloud API PUT /motion) are accepted but may be reverted within ~1 second
    by the camera's on-device automation rules. Settings controlled via the SHC
    (privacy mode, camera light) are NOT affected by this issue.
    See: GET /v11/video_inputs/{id}/rules (returns [] — rules stored on-device).
    """

    _attr_icon = "mdi:motion-sensor"
    _attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Motion Detection"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_motion_enabled"

    @property
    def is_on(self) -> bool | None:
        settings = self.coordinator.motion_settings(self._cam_id)
        if not settings:
            return None
        return settings.get("enabled", False)

    async def async_turn_on(self, **kwargs):
        settings = self.coordinator.motion_settings(self._cam_id)
        sensitivity = settings.get("motionAlarmConfiguration", "HIGH") if settings else "HIGH"
        await self.coordinator.async_put_camera(
            self._cam_id,
            "motion",
            {"enabled": True, "motionAlarmConfiguration": sensitivity},
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_off(self, **kwargs):
        settings = self.coordinator.motion_settings(self._cam_id)
        sensitivity = settings.get("motionAlarmConfiguration", "HIGH") if settings else "HIGH"
        await self.coordinator.async_put_camera(
            self._cam_id,
            "motion",
            {"enabled": False, "motionAlarmConfiguration": sensitivity},
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ─────────────────────────────────────────────────────────────────────────────
class BoschRecordSoundSwitch(_BoschSwitchBase):
    """Toggle audio in cloud event recordings."""

    _attr_icon = "mdi:record-rec"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Record Sound"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_record_sound"

    @property
    def is_on(self) -> bool | None:
        opts = self.coordinator.recording_options(self._cam_id)
        if not opts:
            return None
        return opts.get("recordSound", False)

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "recording_options", {"recordSound": True}
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "recording_options", {"recordSound": False}
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ─────────────────────────────────────────────────────────────────────────────
class BoschAutoFollowSwitch(_BoschSwitchBase):
    """Toggle auto-follow (camera automatically pans to track motion).

    Only available on CAMERA_360 (indoor) — cameras with panLimit > 0.
    Uses cloud API: GET/PUT /v11/video_inputs/{id}/autofollow
    Body: {"result": true/false}
    Response: HTTP 204 on success.
    """

    _attr_icon = "mdi:target-account"
    _attr_entity_registry_enabled_default = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Auto Follow"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_autofollow"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data.get(self._cam_id, {}).get("autofollow")
        if data is None:
            return None
        return data.get("result", False)

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "autofollow", {"result": True}
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "autofollow", {"result": False}
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ─────────────────────────────────────────────────────────────────────────────
class BoschIntercomSwitch(_BoschSwitchBase):
    """Switch: ON = intercom (two-way audio) active, OFF = intercom off.

    When turned ON: enables speaker via PUT /v11/video_inputs/{id}/audio
    with {"audioEnabled": True, "SpeakerLevel": 50}.
    When turned OFF: disables speaker with {"audioEnabled": False}.
    Disabled by default — enable in Settings -> Entities.
    """

    _attr_icon = "mdi:microphone"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._is_on: bool = False

    @property
    def name(self) -> str:
        return f"Bosch {self._cam_title} Intercom"

    @property
    def unique_id(self) -> str:
        return f"bosch_shc_camera_{self._cam_id}_intercom"

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def icon(self) -> str:
        return "mdi:microphone" if self._is_on else "mdi:microphone-off"

    async def async_turn_on(self, **kwargs):
        """Enable intercom (two-way audio) with speaker level 50."""
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {self.coordinator.token}",
            "Content-Type": "application/json",
        }
        body = {"audioEnabled": True, "SpeakerLevel": 50}
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/audio",
                    headers=headers,
                    json=body,
                ) as resp:
                    if resp.status in (200, 204):
                        self._is_on = True
                        _LOGGER.info("Intercom ON for %s", self._cam_title)
                    else:
                        _LOGGER.warning(
                            "Intercom ON failed for %s: HTTP %d", self._cam_title, resp.status
                        )
        except Exception as err:
            _LOGGER.warning("Intercom ON error for %s: %s", self._cam_title, err)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        """Disable intercom (two-way audio)."""
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {self.coordinator.token}",
            "Content-Type": "application/json",
        }
        body = {"audioEnabled": False}
        try:
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{self._cam_id}/audio",
                    headers=headers,
                    json=body,
                ) as resp:
                    if resp.status in (200, 204):
                        self._is_on = False
                        _LOGGER.info("Intercom OFF for %s", self._cam_title)
                    else:
                        _LOGGER.warning(
                            "Intercom OFF failed for %s: HTTP %d", self._cam_title, resp.status
                        )
        except Exception as err:
            _LOGGER.warning("Intercom OFF error for %s: %s", self._cam_title, err)
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschPrivacySoundSwitch(_BoschSwitchBase):
    """Switch: ON = privacy sound override active, OFF = privacy sound off.

    When enabled, the camera plays an audible tone when privacy mode changes.
    Uses cloud API: GET/PUT /v11/video_inputs/{id}/privacy_sound_override
    Body: {"result": true/false}
    Only available on CAMERA_360 (indoor) — outdoor cameras return 442.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Privacy Sound"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_privacy_sound"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data.get(self._cam_id, {}).get("privacy_sound_override")
        if data is None:
            return None
        return data.get("result", False)

    @property
    def icon(self) -> str:
        return "mdi:volume-high" if self.is_on else "mdi:volume-off"

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "privacy_sound_override", {"result": True}
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "privacy_sound_override", {"result": False}
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())


# ─────────────────────────────────────────────────────────────────────────────
class BoschTimestampSwitch(_BoschSwitchBase):
    """Switch: ON = time/date overlay visible on video, OFF = hidden.

    Uses cloud API: GET/PUT /v11/video_inputs/{id}/timestamp
    Body: {"result": true/false}
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Timestamp Overlay"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_timestamp"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._timestamp_cache.get(self._cam_id)

    @property
    def icon(self) -> str:
        return "mdi:clock-outline" if self.is_on else "mdi:clock-remove-outline"

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator._timestamp_cache.get(self._cam_id) is not None
        )

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "timestamp", {"result": True}
        )
        self.coordinator._timestamp_cache[self._cam_id] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "timestamp", {"result": False}
        )
        self.coordinator._timestamp_cache[self._cam_id] = False
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
_NOTIF_TYPE_ICONS = {
    "movement":    "mdi:motion-sensor",
    "person":      "mdi:account-eye",
    "audio":       "mdi:volume-high",
    "trouble":     "mdi:alert-circle",
    "cameraAlarm": "mdi:alarm-light",
}

_NOTIF_TYPE_LABELS = {
    "movement":    "Movement Notifications",
    "person":      "Person Notifications",
    "audio":       "Audio Notifications",
    "trouble":     "Trouble Notifications",
    "cameraAlarm": "Camera Alarm Notifications",
}


class BoschNotificationTypeSwitch(_BoschSwitchBase):
    """Per-type notification toggle (movement, person, audio, trouble, cameraAlarm).

    Reads from GET /v11/video_inputs/{id}/notifications.
    Writes via PUT /v11/video_inputs/{id}/notifications with all toggles.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry, ntype: str) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._ntype = ntype
        label = _NOTIF_TYPE_LABELS.get(ntype, ntype)
        self._attr_name      = f"Bosch {self._cam_title} {label}"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_notif_{ntype}"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator._notifications_cache.get(self._cam_id, {})
        if not data:
            return None
        return data.get(self._ntype, False)

    @property
    def icon(self) -> str:
        return _NOTIF_TYPE_ICONS.get(self._ntype, "mdi:bell")

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and bool(self.coordinator._notifications_cache.get(self._cam_id))
        )

    async def _set_type(self, value: bool):
        """Write updated notification toggles (preserving other types)."""
        current = dict(self.coordinator._notifications_cache.get(self._cam_id, {}))
        current[self._ntype] = value
        success = await self.coordinator.async_put_camera(
            self._cam_id, "notifications", current
        )
        if success:
            self.coordinator._notifications_cache[self._cam_id] = current
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set_type(True)

    async def async_turn_off(self, **kwargs):
        await self._set_type(False)
