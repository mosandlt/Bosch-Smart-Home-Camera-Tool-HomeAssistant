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
                          Uses SHC local API for write; reads state from cloud API.
                          Requires shc_ip + cert/key configured in options for control.
"""

import logging

from homeassistant.components.switch import SwitchEntity
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
        # Camera light — only if camera supports light (from cloud featureSupport)
        # or if SHC is configured (SHC will tell us the state; cloud data may not be ready yet)
        has_light = cam_info.get("featureSupport", {}).get("light", False)
        if has_light or opts.get("shc_ip", "").strip():
            entities.append(BoschCameraLightSwitch(coordinator, cam_id, config_entry))
    async_add_entities(entities, update_before_add=False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschLiveStreamSwitch(CoordinatorEntity, SwitchEntity):
    """Switch: ON = live stream active, OFF = stopped.

    State is driven by the coordinator's _live_connections dict.
    Stays ON until manually turned OFF or HA restarts.
    Default state on HA startup: OFF.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Live Stream"
        self._attr_unique_id = f"bosch_shc_live_{cam_id.lower()}"

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
        return {
            "rtsps_url":      live.get("rtspsUrl", ""),
            "proxy_snap_url": live.get("proxyUrl", ""),
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

    async def async_turn_off(self, **kwargs) -> None:
        """Clear the live session immediately."""
        _LOGGER.info("Live stream OFF for %s", self._cam_title)
        self.coordinator._live_connections.pop(self._cam_id, None)
        self.coordinator._live_opened_at.pop(self._cam_id, None)
        await self.coordinator._unregister_go2rtc_stream(self._cam_id)
        await self.coordinator.async_request_refresh()


# ─────────────────────────────────────────────────────────────────────────────
class BoschAudioSwitch(CoordinatorEntity, SwitchEntity):
    """Switch: ON = live stream includes audio (AAC), OFF = video-only.

    Default: OFF — silent stream. Turn ON to enable AAC-LC 16kHz mono audio.
    If the live stream is currently active, toggling re-opens the connection
    so the new audio setting takes effect immediately.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Audio"
        self._attr_unique_id = f"bosch_shc_audio_{cam_id.lower()}"

        # Default: audio OFF (silent stream)
        coordinator._audio_enabled.setdefault(cam_id, False)

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
            await self.coordinator.async_request_refresh()


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraLightSwitch(CoordinatorEntity, SwitchEntity):
    """Switch: ON = camera indicator LED on, OFF = LED off.

    Only registered for cameras with featureSupport.light = True (from cloud API).
    State is read from cloud API featureStatus or SHC CameraLight service.
    Write (turn on/off) requires SHC local API (shc_ip + cert/key in options).
    If SHC is not configured, the entity is shown but unavailable for control.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Camera Light"
        self._attr_unique_id = f"bosch_shc_light_{cam_id.lower()}"
        self._attr_icon      = "mdi:led-on"

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

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._shc_state_cache.get(self._cam_id, {}).get("camera_light")

    @property
    def available(self) -> bool:
        """Available when coordinator is running and we have a light state.

        Light state comes from SHC (if configured) or cloud API featureStatus.
        The entity shows state without SHC, but control requires SHC to be configured.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator._shc_state_cache.get(self._cam_id, {}).get("camera_light") is not None
        )

    @property
    def icon(self) -> str:
        return "mdi:led-on" if self.is_on else "mdi:led-off"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_shc_set_camera_light(self._cam_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_shc_set_camera_light(self._cam_id, False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschPrivacyModeSwitch(CoordinatorEntity, SwitchEntity):
    """Switch: ON = privacy mode active (camera off / shutter closed), OFF = camera active.

    Uses the Bosch cloud API: PUT /v11/video_inputs/{id}/privacy
    No SHC local API required — works without SHC configured.
    State is read from the /v11/video_inputs response (privacyMode field).
    Falls back to SHC API if cloud call fails and SHC is configured.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        self._entry  = entry

        info = coordinator.data.get(cam_id, {}).get("info", {})
        self._cam_title = info.get("title", cam_id)
        self._model     = info.get("hardwareVersion", "CAMERA")
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

        self._attr_name      = f"Bosch {self._cam_title} Privacy Mode"
        self._attr_unique_id = f"bosch_shc_privacy_{cam_id.lower()}"

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

    async def async_turn_on(self, **kwargs) -> None:
        """Enable privacy mode — camera turns off / shutter closes."""
        await self.coordinator.async_cloud_set_privacy_mode(self._cam_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable privacy mode — camera turns back on."""
        await self.coordinator.async_cloud_set_privacy_mode(self._cam_id, False)
