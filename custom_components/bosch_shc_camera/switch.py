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
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, get_options, CLOUD_API

_LOGGER = logging.getLogger(__name__)


_GEN2_INDOOR_HW = {"HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"}
_INDOOR_HW = {"INDOOR", "CAMERA_360", "HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"}


def _is_gen2_indoor(entity) -> bool:
    """Return True if the entity's camera is a Gen2 Indoor model."""
    hw = entity.coordinator.data.get(entity._cam_id, {}).get(
        "info", {}
    ).get("hardwareVersion", "")
    return hw in _GEN2_INDOOR_HW


async def _warn_if_privacy_on(entity, feature_name: str) -> bool:
    """Show a persistent notification when the user tries to change a
    privacy-gated setting while privacy mode is ON. Returns True if the
    write was blocked.

    The Bosch cloud API returns HTTP 443 "sh:camera.in.privacy.mode" on
    reads and writes to /intrusionDetectionConfig, /zones, /privateAreas,
    /motion, and some lighting endpoints while the camera is in privacy
    mode. Without a guard the write silently fails in the logs; with this
    guard the user sees a clear notification explaining why.
    """
    coordinator = entity.coordinator
    cam_id = entity._cam_id
    cache = coordinator._shc_state_cache.get(cam_id, {})
    privacy_on = bool(cache.get("privacy_mode"))
    if not privacy_on:
        return False
    cam_title = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    _LOGGER.warning(
        "%s write blocked for %s — camera is in privacy mode (HTTP 443 would follow).",
        feature_name, cam_title,
    )
    try:
        await entity.hass.services.async_call(
            "persistent_notification", "create",
            {
                "title": f"{feature_name} — Kamera im Privacy-Mode",
                "message": (
                    f"Die Einstellung **{feature_name}** für **{cam_title}** kann nicht "
                    f"geändert werden, solange der Privacy-Mode aktiv ist.\n\n"
                    f"Die Kamera liefert in diesem Zustand `HTTP 443 sh:camera.in.privacy.mode` "
                    f"auf Schreibzugriffe. Schalte zuerst den Privacy-Mode aus "
                    f"(`switch.bosch_{cam_title.lower()}_privacy_mode`) und versuche es erneut."
                ),
                "notification_id": f"bosch_privacy_blocked_{cam_id}",
            },
            blocking=False,
        )
    except Exception as err:
        _LOGGER.debug("persistent_notification create failed: %s", err)
    return True


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
        from .models import get_display_name
        self._model_name = get_display_name(self._model)
        self._fw        = info.get("firmwareVersion", "")
        self._mac       = info.get("macAddress", "")

    @property
    def available(self) -> bool:
        """Base availability: coordinator running AND camera is ONLINE.

        Prevents automation triggers and service calls from reaching cameras
        that are currently offline or unreachable. Cloud-only switches
        (BoschPrivacyModeSwitch, BoschNotificationsSwitch, notification type
        switches) override this to skip the per-camera online check, since
        those API calls go through the Bosch cloud and succeed even when
        the camera itself is unreachable.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
        )

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model":        self._model_name,
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
            entities.append(BoschFrontLightSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschWallwasherSwitch(coordinator, cam_id, config_entry))
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
        # Indoor CAMERA_360 (Gen1) + HOME_Eyes_Indoor (Gen2) support it; outdoor returns 442.
        hw_version = cam_info.get("hardwareVersion", "")
        if hw_version in ("CAMERA_360", "INDOOR", "HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
            entities.append(BoschPrivacySoundSwitch(coordinator, cam_id, config_entry))
        # Timestamp overlay — available for all cameras
        entities.append(BoschTimestampSwitch(coordinator, cam_id, config_entry))
        # Status LED — Gen2 cameras only
        from .models import get_model_config
        if get_model_config(hw_version).generation >= 2:
            entities.append(BoschStatusLedSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschMotionLightSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschAmbientLightSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschSoftLightFadingSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschIntrusionDetectionSwitch(coordinator, cam_id, config_entry))
        # Notification type toggles — person is cloud AI (all cameras);
        # audio gated on featureSupport.sound (API-reported, not hardcoded by model).
        has_sound = cam_info.get("featureSupport", {}).get("sound", False)
        for ntype in ("movement", "person", "trouble", "cameraAlarm", "troubleEmail"):
            entities.append(BoschNotificationTypeSwitch(coordinator, cam_id, config_entry, ntype))
        if has_sound:
            entities.append(BoschNotificationTypeSwitch(coordinator, cam_id, config_entry, "audio"))
        # Gen2 Indoor II — alarm system (integrated 75 dB siren) + audio alarm (glass/smoke/CO)
        if hw_version in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
            entities.append(BoschAlarmSystemArmSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschAlarmModeSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschPreAlarmSwitch(coordinator, cam_id, config_entry))
            entities.append(BoschAudioAlarmSwitch(coordinator, cam_id, config_entry))
        # Image rotation 180° — only for indoor cameras (Gen1 360 + Gen2 Indoor II).
        # Outdoor cameras have a fixed mounting orientation by design and don't
        # need this. The switch is purely client-side display state — the card
        # applies CSS transform, the snapshot path applies PIL rotation, and
        # (for Gen1 360) the pan slider sign is inverted.
        if hw_version in _INDOOR_HW:
            entities.append(BoschImageRotation180Switch(coordinator, cam_id, config_entry))
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
        self._attr_name            = f"Bosch {self._cam_title} Live Stream"
        self._attr_unique_id       = f"bosch_shc_live_{cam_id.lower()}"
        self._attr_translation_key = "live_stream"

    @property
    def is_on(self) -> bool:
        """True if a live session is currently active."""
        return self._cam_id in self.coordinator._live_connections

    @property
    def available(self) -> bool:
        """Unavailable while privacy mode is active or the LOCAL keepalive loop has stalled."""
        if not super().available:
            return False
        if bool(self.coordinator._shc_state_cache.get(self._cam_id, {}).get("privacy_mode")):
            return False
        return not self.coordinator.is_session_stale(self._cam_id)

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

    # Minimum seconds between stream ON attempts per camera.
    _STREAM_COOLDOWN = 5

    async def async_turn_on(self, **kwargs) -> None:
        """Open a new live proxy connection."""
        import time
        # Block stream start if privacy mode is active (camera shutter is closed)
        if bool(self.coordinator._shc_state_cache.get(self._cam_id, {}).get("privacy_mode")):
            raise ServiceValidationError(
                f"Cannot start stream for {self._cam_title} — privacy mode is active. "
                "Turn off privacy mode first.",
            )
        last_off = getattr(self, "_last_stream_off", 0)
        elapsed = time.monotonic() - last_off
        if last_off > 0 and elapsed < self._STREAM_COOLDOWN:
            _LOGGER.warning(
                "Stream ON for %s blocked — cooldown %.0fs remaining",
                self._cam_title, self._STREAM_COOLDOWN - elapsed,
            )
            return
        _LOGGER.info("Live stream ON for %s", self._cam_title)
        # No explicit cleanup needed — try_live_connection() sends a new
        # PUT /connection which automatically replaces any stale session.
        result = await self.coordinator.try_live_connection(self._cam_id)
        if result:
            conn_type = result.get("_connection_type", "REMOTE")
            _LOGGER.info(
                "Live stream active for %s (%s) — %s",
                self._cam_title, conn_type, result.get("rtspsUrl", ""),
            )
            # Schedule health check — if the LOCAL stream isn't actually
            # producing HLS segments after ~60s and still not after ~120s,
            # record errors and restart. After enough errors the next
            # try_live_connection() falls through to REMOTE automatically
            # via the max_stream_errors gate.
            # Track the task on the coordinator so async_unload_entry cancels
            # it during integration reload; otherwise a stale check from a
            # previous session can fire against a fresh coordinator and start
            # a second renewal loop alongside the user-triggered one.
            if conn_type == "LOCAL":
                hc_task = self.hass.async_create_task(
                    self._stream_health_watchdog(self._cam_id)
                )
                self.coordinator._bg_tasks.add(hc_task)
                hc_task.add_done_callback(self.coordinator._bg_tasks.discard)
        else:
            _LOGGER.warning("Live stream failed for %s — check HA logs", self._cam_title)
            self.coordinator.record_stream_error(self._cam_id)
        self.async_write_ha_state()

    async def _stream_health_watchdog(self, cam_id: str) -> None:
        """Watchdog for a LOCAL stream: verify HA's stream component is
        actually producing HLS output.

        Runs two checks (60s, 120s after the live URL was exposed). At each
        tick:
          * stop early if the stream was turned off or already switched to
            REMOTE — nothing to watch.
          * ask HA's `Stream` object whether it's `available` — that flag
            flips True only when the FFmpeg worker has produced its first
            segment. A Stream object that exists but whose `available` is
            False means FFmpeg started and then died, which is exactly the
            failure mode reported in issue #6 (yellow → brief blue → yellow
            cycle).

        On a healthy tick the watchdog clears the coordinator error counter
        and exits. On a failing tick it records a stream error, tears the
        LOCAL session down, and calls try_live_connection() again — which
        will go directly to REMOTE once `max_stream_errors` is reached.
        Two failed ticks in a row therefore escalate to Cloud within ~2 min
        without any hard-coded time gate.
        """
        import asyncio

        def _is_local_active() -> bool:
            live = self.coordinator._live_connections.get(cam_id, {})
            return bool(live) and live.get("_connection_type") == "LOCAL"

        def _stream_health_state() -> str:
            # Three-state classifier so we don't conflate "no consumer yet"
            # with "stream object exists but is unhealthy". Returns:
            #   "no_consumer" — cam_entity.stream is None (frontend never
            #     asked for HLS, FFmpeg never started). Restarting the LOCAL
            #     session does NOT help here; nobody is reading bytes.
            #   "healthy"     — Stream.available is True (worker producing).
            #   "unhealthy"   — Stream object exists but available is False
            #     (FFmpeg started and died, or never produced first segment).
            cam_entity = self.coordinator._camera_entities.get(cam_id)
            if not cam_entity:
                return "no_consumer"
            stream = getattr(cam_entity, "stream", None)
            if stream is None:
                return "no_consumer"
            return "healthy" if bool(getattr(stream, "available", False)) else "unhealthy"

        for idx, delay in enumerate((60, 60)):  # 60s, then another 60s → ~2 min total
            await asyncio.sleep(delay)
            if not _is_local_active():
                return
            state = _stream_health_state()
            if state == "healthy":
                self.coordinator.record_stream_success(cam_id)
                return
            if state == "no_consumer":
                # No HLS consumer asked for the stream — FFmpeg never started,
                # so there's nothing to restart. Leaving the LOCAL session up
                # so a future consumer (browser tab opens) gets it instantly.
                _LOGGER.debug(
                    "Stream health watchdog: %s LOCAL session up but no HLS "
                    "consumer connected — skipping health check (frontend "
                    "card may be unmounted)",
                    cam_id[:8],
                )
                return
            # On the second consecutive failure (~2 min with no healthy
            # output), escalate: saturate the error counter so the next
            # try_live_connection() is forced to REMOTE regardless of the
            # per-model threshold. Single failure still follows the normal
            # gradual-escalation path via record_stream_error().
            is_final = idx == 1
            if is_final:
                cfg = self.coordinator.get_model_config(cam_id)
                self.coordinator._stream_error_count[cam_id] = cfg.max_stream_errors
                _LOGGER.warning(
                    "Stream health watchdog: %s LOCAL stream still not healthy "
                    "after ~2 min — forcing REMOTE fallback",
                    cam_id[:8],
                )
            else:
                self.coordinator.record_stream_error(cam_id)
                _LOGGER.warning(
                    "Stream health watchdog: %s LOCAL stream not healthy at %ds — "
                    "recording error and restarting",
                    cam_id[:8], delay,
                )
            self.coordinator._live_connections.pop(cam_id, None)
            await self.coordinator._stop_tls_proxy(cam_id)
            result = await self.coordinator.try_live_connection(cam_id)
            if result:
                _LOGGER.info(
                    "Stream health watchdog: %s restarted as %s",
                    cam_id[:8], result.get("_connection_type", "?"),
                )
                # If we fell back to REMOTE, stop watching — REMOTE has no
                # pre-warm dependency and no LOCAL-specific failure modes.
                if result.get("_connection_type") != "LOCAL":
                    self.async_write_ha_state()
                    return
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Clear the live session and stop the TLS proxy."""
        import time
        self._last_stream_off = time.monotonic()
        _LOGGER.info("Live stream OFF for %s", self._cam_title)
        # Shared teardown: cancels renewal task, pops _live_connections,
        # stops TLS proxy + go2rtc, stops HA's camera.stream so
        # stream_worker can't auto-restart against the dead proxy.
        await self.coordinator._tear_down_live_stream(self._cam_id)
        # Update state immediately so the UI reflects OFF without waiting
        # for the coordinator refresh that follows.
        self.async_write_ha_state()
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
        self._attr_name            = f"Bosch {self._cam_title} Audio"
        self._attr_unique_id       = f"bosch_shc_audio_{cam_id.lower()}"
        self._attr_translation_key = "audio"
        self._attr_entity_category = EntityCategory.CONFIG
        # Default from options (configurable in integration settings)
        opts = coordinator.options
        audio_default = opts.get("audio_default_on", True)
        coordinator._audio_enabled.setdefault(cam_id, audio_default)

    @property
    def is_on(self) -> bool:
        return self.coordinator._audio_enabled.get(self._cam_id, True)

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
        if bool(self.coordinator._shc_state_cache.get(self._cam_id, {}).get("privacy_mode")):
            _LOGGER.warning(
                "Audio change for %s skipped — privacy mode is active", self._cam_title
            )
            return
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
        self._attr_name            = f"Bosch {self._cam_title} Camera Light"
        self._attr_unique_id       = f"bosch_shc_light_{cam_id.lower()}"
        self._attr_icon            = "mdi:led-on"
        self._attr_translation_key = "camera_light"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._shc_state_cache.get(self._cam_id, {}).get("camera_light")

    @property
    def available(self) -> bool:
        """Available when coordinator is running, camera online, and light support present.

        Control uses cloud API (PUT /v11/video_inputs/{id}/lighting_override).
        Requires camera ONLINE: light control needs camera to respond.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
        )

    @property
    def icon(self) -> str:
        return "mdi:led-on" if self.is_on else "mdi:led-off"

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_camera_light(self._cam_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_camera_light(self._cam_id, False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschFrontLightSwitch(_BoschSwitchBase):
    """Switch: front spotlight on/off (independent of wallwasher).

    Uses cloud API: PUT /v11/video_inputs/{id}/lighting_override
    Only registered for cameras with featureSupport.light = True.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Front Light"
        self._attr_unique_id       = f"bosch_shc_front_light_{cam_id.lower()}"
        self._attr_icon            = "mdi:spotlight-beam"
        self._attr_translation_key = "front_light"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._shc_state_cache.get(self._cam_id, {}).get("front_light")

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_light_component(self._cam_id, "front", True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_light_component(self._cam_id, "front", False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschWallwasherSwitch(_BoschSwitchBase):
    """Switch: top + bottom ambient lights on/off (independent of front light).

    Gen1: Uses cloud API: PUT /v11/video_inputs/{id}/lighting_override (wallwasherOn)
    Gen2: Uses cloud API: PUT /v11/video_inputs/{id}/lighting/switch/topdown (enabled)
    Only registered for cameras with featureSupport.light = True.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        from .models import get_model_config
        hw = coordinator.data.get(cam_id, {}).get("info", {}).get("hardwareVersion", "CAMERA")
        is_gen2 = get_model_config(hw).generation >= 2
        label = "Oberes + Unteres Licht" if is_gen2 else "Wallwasher"
        self._attr_name            = f"Bosch {self._cam_title} {label}"
        self._attr_unique_id       = f"bosch_shc_wallwasher_{cam_id.lower()}"
        self._attr_icon            = "mdi:wall-sconce-flat"
        self._attr_translation_key = "wallwasher"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._shc_state_cache.get(self._cam_id, {}).get("wallwasher")

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_light_component(self._cam_id, "wallwasher", True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_cloud_set_light_component(self._cam_id, "wallwasher", False)


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
        self._attr_name            = f"Bosch {self._cam_title} Privacy Mode"
        self._attr_unique_id       = f"bosch_shc_privacy_{cam_id.lower()}"
        self._attr_translation_key = "privacy_mode"

    @property
    def is_on(self) -> bool | None:
        """True when privacy mode is ON (camera blocked/shuttered).

        Read from cloud API response (privacyMode field in /v11/video_inputs).
        Available immediately without SHC configured.
        """
        return self.coordinator._shc_state_cache.get(self._cam_id, {}).get("privacy_mode")

    @property
    def available(self) -> bool:
        """Cloud-only: available without camera being ONLINE.

        Privacy state comes from the cloud API response — the camera does not
        need to be locally reachable for this to work. Overrides the base class
        is_camera_online() guard intentionally.
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

    # Minimum seconds between privacy mode changes per camera.
    # Rapid toggling can stress the camera firmware (red LED / reboot).
    _PRIVACY_COOLDOWN = 10

    async def _check_cooldown(self) -> bool:
        """Return True if cooldown period has passed, False if too soon."""
        import time
        # Block during stream warm-up (TLS proxy + encoder init)
        if self.coordinator.is_stream_warming(self._cam_id):
            _LOGGER.warning(
                "Privacy toggle for %s blocked — stream is warming up",
                self._cam_title,
            )
            return False
        # Block rapid toggles
        last = self.coordinator._privacy_set_at.get(self._cam_id, 0)
        elapsed = time.monotonic() - last
        if elapsed < self._PRIVACY_COOLDOWN:
            remaining = self._PRIVACY_COOLDOWN - elapsed
            _LOGGER.warning(
                "Privacy toggle for %s blocked — cooldown %.0fs remaining (prevents camera stress)",
                self._cam_title, remaining,
            )
            return False
        return True

    async def async_turn_on(self, **kwargs) -> None:
        """Enable privacy mode — camera turns off / shutter closes.

        Also stops any active live stream since the camera can't stream
        while privacy mode is active (shutter closed). Uses the coordinator's
        shared stream-teardown so the renewal task is cancelled, HA's
        camera.stream is stopped, and the stream_worker doesn't enter its
        auto-restart loop against the now-dead TLS proxy — which was the
        side-effect noticed by Thomas: flipping Privacy ON while streaming
        made the stream switch look still-on, renewal task kept firing,
        and the stream_worker-error listener would uselessly try a REMOTE
        fallback against a camera that's returning HTTP 443 privacy-gated.
        """
        if not await self._check_cooldown():
            return
        if self._cam_id in self.coordinator._live_connections:
            _LOGGER.info("Privacy ON for %s — stopping active live stream", self._cam_title)
            await self.coordinator._tear_down_live_stream(self._cam_id)
        await self.coordinator.async_cloud_set_privacy_mode(self._cam_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        """Disable privacy mode — camera turns back on."""
        if not await self._check_cooldown():
            return
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
        self._attr_name            = f"Bosch {self._cam_title} Notifications"
        self._attr_unique_id       = f"bosch_shc_notifications_{cam_id.lower()}"
        self._attr_translation_key = "notifications"
        self._attr_entity_category = EntityCategory.CONFIG

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
        """Cloud-only: available without camera being ONLINE.

        Notification state comes from the cloud API — overrides base class
        is_camera_online() guard intentionally.
        """
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
    _attr_translation_key = "motion_detection"
    _attr_entity_category = EntityCategory.CONFIG

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
        if _is_gen2_indoor(self) and await _warn_if_privacy_on(self, "Bewegungserkennung"):
            return
        settings = self.coordinator.motion_settings(self._cam_id)
        sensitivity = settings.get("motionAlarmConfiguration", "HIGH") if settings else "HIGH"
        await self.coordinator.async_put_camera(
            self._cam_id,
            "motion",
            {"enabled": True, "motionAlarmConfiguration": sensitivity},
        )
        self.hass.async_create_task(self.coordinator.async_request_refresh())

    async def async_turn_off(self, **kwargs):
        if _is_gen2_indoor(self) and await _warn_if_privacy_on(self, "Bewegungserkennung"):
            return
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
    _attr_translation_key = "record_sound"

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
    _attr_translation_key = "auto_follow"
    _attr_entity_category = EntityCategory.CONFIG

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
    _attr_translation_key = "intercom"
    _attr_entity_category = EntityCategory.CONFIG

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

    Maps to the iOS app "Ton" toggle under Kamera-Funktionen — when enabled,
    the camera plays an audible tone when privacy mode changes.
    Uses cloud API: GET/PUT /v11/video_inputs/{id}/privacy_sound_override
    Body: {"result": true/false}
    Supported: CAMERA_360 (Gen1 Indoor), HOME_Eyes_Indoor (Gen2 Indoor II).
    Outdoor cameras return 442.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "privacy_sound"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Privacy Sound"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_privacy_sound"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._privacy_sound_cache.get(self._cam_id)

    @property
    def icon(self) -> str:
        return "mdi:volume-high" if self.is_on else "mdi:volume-off"

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
            and self.coordinator._privacy_sound_cache.get(self._cam_id) is not None
        )

    async def async_turn_on(self, **kwargs):
        success = await self.coordinator.async_put_camera(
            self._cam_id, "privacy_sound_override", {"result": True}
        )
        if success:
            self.coordinator._privacy_sound_cache[self._cam_id] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        success = await self.coordinator.async_put_camera(
            self._cam_id, "privacy_sound_override", {"result": False}
        )
        if success:
            self.coordinator._privacy_sound_cache[self._cam_id] = False
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschTimestampSwitch(_BoschSwitchBase):
    """Switch: ON = time/date overlay visible on video, OFF = hidden.

    Uses cloud API: GET/PUT /v11/video_inputs/{id}/timestamp
    Body: {"result": true/false}
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "timestamp_overlay"

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
            and self.coordinator.is_camera_online(self._cam_id)
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
class BoschStatusLedSwitch(_BoschSwitchBase):
    """Switch: status LED on/off (Gen2 cameras only).

    Uses cloud API: GET/PUT /v11/video_inputs/{id}/ledlights
    Body: {"state": "ON"/"OFF"}
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "status_led"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Status LED"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_ledlights"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._ledlights_cache.get(self._cam_id)

    @property
    def icon(self) -> str:
        return "mdi:led-on" if self.is_on else "mdi:led-off"

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
            and self.coordinator._ledlights_cache.get(self._cam_id) is not None
        )

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "ledlights", {"state": "ON"}
        )
        self.coordinator._ledlights_cache[self._cam_id] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_put_camera(
            self._cam_id, "ledlights", {"state": "OFF"}
        )
        self.coordinator._ledlights_cache[self._cam_id] = False
        self.async_write_ha_state()


# ─────────────────────────────────────────────────────────────────────────────
class BoschMotionLightSwitch(_BoschSwitchBase):
    """Switch: motion-triggered lighting on/off (Gen2 only).

    When ON, camera lights turn on automatically when motion is detected.
    Uses cloud API: GET/PUT /v11/video_inputs/{id}/lighting/motion
    Toggles lightOnMotionEnabled field, preserves all other settings.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Licht bei Bewegung"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_motion_light"
        self._attr_icon            = "mdi:motion-sensor"
        self._attr_translation_key = "motion_light"
        self._attr_entity_category = EntityCategory.CONFIG
        self._is_on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        # Read from coordinator cache if local state not yet set
        if self._is_on is None:
            cache = self.coordinator._motion_light_cache.get(self._cam_id, {})
            if cache:
                self._is_on = cache.get("lightOnMotionEnabled", False)
        return self._is_on

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
        )

    async def _set_motion_light(self, enabled: bool) -> None:
        """Read current motion light config, toggle enabled, write back."""
        # Read current config from cache or API
        cache = self.coordinator._motion_light_cache.get(self._cam_id, {})
        if not cache:
            # Fetch fresh if cache empty
            import aiohttp, asyncio
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            token = self.coordinator.token
            if not token:
                return
            session = async_get_clientsession(self.hass, verify_ssl=False)
            try:
                async with asyncio.timeout(10):
                    async with session.get(
                        f"https://residential.cbs.boschsecurity.com/v11/video_inputs/{self._cam_id}/lighting/motion",
                        headers={"Authorization": f"Bearer {token}"},
                    ) as resp:
                        if resp.status == 200:
                            cache = await resp.json()
                        else:
                            _LOGGER.warning("Motion light GET HTTP %d for %s", resp.status, self._cam_id[:8])
                            return
            except Exception as err:
                _LOGGER.warning("Motion light GET error for %s: %s", self._cam_id[:8], err)
                return
        # Update the enabled flag and write back
        data = dict(cache)
        data["lightOnMotionEnabled"] = enabled
        success = await self.coordinator.async_put_camera(
            self._cam_id, "lighting/motion", data
        )
        if success:
            self._is_on = enabled
            self.coordinator._motion_light_cache[self._cam_id] = data
            _LOGGER.info("Motion light %s for %s", "ON" if enabled else "OFF", self._cam_id[:8])
        else:
            _LOGGER.warning("Motion light PUT failed for %s", self._cam_id[:8])
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set_motion_light(True)

    async def async_turn_off(self, **kwargs):
        await self._set_motion_light(False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschAmbientLightSwitch(_BoschSwitchBase):
    """Switch: ambient/permanent lighting on/off (Gen2 only).

    When ON, camera lights stay on according to schedule (dusk-to-dawn or manual times).
    Uses cloud API: GET/PUT /v11/video_inputs/{id}/lighting/ambient
    Toggles ambientLightEnabled field, preserves schedule and brightness settings.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Dauerlicht"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_ambient_light"
        self._attr_icon            = "mdi:lightbulb-auto"
        self._attr_translation_key = "ambient_light"
        self._attr_entity_category = EntityCategory.CONFIG
        self._is_on: bool | None = None

    @property
    def is_on(self) -> bool | None:
        if self._is_on is None:
            cache = self.coordinator._ambient_lighting_cache.get(self._cam_id, {})
            if cache:
                self._is_on = cache.get("ambientLightEnabled", False)
        return self._is_on

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
        )

    async def _set_ambient_light(self, enabled: bool) -> None:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        token = self.coordinator.token
        if not token:
            return
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"https://residential.cbs.boschsecurity.com/v11/video_inputs/{self._cam_id}/lighting/ambient"
        import asyncio
        try:
            async with asyncio.timeout(10):
                async with session.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
            data["ambientLightEnabled"] = enabled
            async with asyncio.timeout(10):
                async with session.put(url, headers=headers, json=data) as resp:
                    if resp.status in (200, 204):
                        self._is_on = enabled
        except Exception as err:
            _LOGGER.warning("Ambient light error for %s: %s", self._cam_id[:8], err)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set_ambient_light(True)

    async def async_turn_off(self, **kwargs):
        await self._set_ambient_light(False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschSoftLightFadingSwitch(_BoschSwitchBase):
    """Switch: soft light fading (Gen2 only).

    When ON, lights fade smoothly instead of snapping on/off.
    Uses cloud API: GET/PUT /v11/video_inputs/{id}/lighting
    Body: {"darknessThreshold": float, "softLightFading": bool}
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "soft_light_fading"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name      = f"Bosch {self._cam_title} Weiches Lichtfading"
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_soft_light_fading"
        self._attr_icon      = "mdi:transition"

    @property
    def is_on(self) -> bool | None:
        cache = self.coordinator._global_lighting_cache.get(self._cam_id, {})
        return cache.get("softLightFading") if cache else None

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
            and bool(self.coordinator._global_lighting_cache.get(self._cam_id))
        )

    async def _put_global_lighting(self, enabled: bool) -> None:
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        token = self.coordinator.token
        if not token:
            return
        cache = self.coordinator._global_lighting_cache.get(self._cam_id, {})
        # Preserve existing darknessThreshold
        threshold = cache.get("darknessThreshold", 0.5)
        body = {"darknessThreshold": threshold, "softLightFading": enabled}
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        url = f"https://residential.cbs.boschsecurity.com/v11/video_inputs/{self._cam_id}/lighting"
        try:
            async with asyncio.timeout(10):
                async with session.put(url, headers=headers, json=body) as resp:
                    if resp.status in (200, 204):
                        # Update cache
                        try:
                            rsp = await resp.json()
                            if isinstance(rsp, dict):
                                self.coordinator._global_lighting_cache[self._cam_id] = rsp
                            else:
                                self.coordinator._global_lighting_cache[self._cam_id] = body
                        except Exception:
                            self.coordinator._global_lighting_cache[self._cam_id] = body
        except Exception as err:
            _LOGGER.warning("Soft fading error for %s: %s", self._cam_id[:8], err)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._put_global_lighting(True)

    async def async_turn_off(self, **kwargs):
        await self._put_global_lighting(False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschIntrusionDetectionSwitch(_BoschSwitchBase):
    """Switch: intrusion detection on/off (Gen2 only).

    DualRadar 180° 3D motion detection with person recognition.
    Uses cloud API: GET/PUT /v11/video_inputs/{id}/intrusionDetectionConfig
    Toggles enabled field, preserves sensitivity/detectionMode/distance.
    Extra attributes: sensitivity (1-5), detectionMode, distance (meters).
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Einbrucherkennung"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_intrusion_detection"
        self._attr_icon            = "mdi:shield-home"
        self._attr_translation_key = "intrusion_detection"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def _config(self) -> dict:
        return self.coordinator._intrusion_config_cache.get(self._cam_id, {})

    @property
    def is_on(self) -> bool | None:
        return self._config.get("enabled")

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
            and bool(self._config)
        )

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "sensitivity": self._config.get("sensitivity"),
            "detection_mode": self._config.get("detectionMode"),
            "distance_meters": self._config.get("distance"),
        }

    async def _set_intrusion(self, enabled: bool) -> None:
        # Write-guard: /intrusionDetectionConfig returns HTTP 443
        # "sh:camera.in.privacy.mode" while privacy is ON. Warn the user
        # visibly instead of failing silently in the logs.
        if await _warn_if_privacy_on(self, "Einbrucherkennung"):
            return
        cfg = dict(self._config)
        if not cfg:
            return
        cfg["enabled"] = enabled
        success = await self.coordinator.async_put_camera(
            self._cam_id, "intrusionDetectionConfig", cfg
        )
        if success:
            self.coordinator._intrusion_config_cache[self._cam_id] = cfg
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set_intrusion(True)

    async def async_turn_off(self, **kwargs):
        await self._set_intrusion(False)


# ─────────────────────────────────────────────────────────────────────────────
_NOTIF_TYPE_ICONS = {
    "movement":     "mdi:motion-sensor",
    "person":       "mdi:account-eye",
    "audio":        "mdi:volume-high",
    "trouble":      "mdi:alert-circle",
    "cameraAlarm":  "mdi:alarm-light",
    "troubleEmail": "mdi:email-alert",
}

_NOTIF_TYPE_LABELS = {
    "movement":     "Movement Notifications",
    "person":       "Person Notifications",
    "audio":        "Audio Notifications",
    "trouble":      "Trouble Notifications (Push)",
    "cameraAlarm":  "Camera Alarm Notifications",
    "troubleEmail": "Trouble Notifications (Email)",
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
        self._attr_name            = f"Bosch {self._cam_title} {label}"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_notif_{ntype}"
        self._attr_translation_key = f"notification_type_{ntype}"

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
        """Cloud-only: available without camera being ONLINE.

        Notification type toggles go through the Bosch cloud API — overrides
        base class is_camera_online() guard intentionally.
        """
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


# ─────────────────────────────────────────────────────────────────────────────
# Gen2 Indoor II — Alarm System (integrated 75 dB siren)
# ─────────────────────────────────────────────────────────────────────────────
class BoschAlarmSystemArmSwitch(_BoschSwitchBase):
    """Switch: scharf/unscharf (armed / disarmed) for the integrated alarm system.

    PUT /v11/video_inputs/{id}/intrusionSystem/arming  body: {"arm": true/false}
    State is derived from GET /v11/video_inputs/{id}/alarmStatus polling +
    optimistic update on successful PUT.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Alarmanlage"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_alarm_arm"
        self._attr_translation_key = "alarm_system_arm"

    @property
    def icon(self) -> str:
        return "mdi:shield-lock" if self.is_on else "mdi:shield-off-outline"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator._arming_cache.get(self._cam_id)

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
        )

    @property
    def extra_state_attributes(self) -> dict:
        status = self.coordinator._alarm_status_cache.get(self._cam_id, {})
        return {
            "alarm_type":       status.get("alarmType"),
            "intrusion_system": status.get("intrusionSystem"),
        }

    async def _set_arm(self, arm: bool) -> None:
        success = await self.coordinator.async_put_camera(
            self._cam_id, "intrusionSystem/arming", {"arm": arm}
        )
        if success:
            self.coordinator._arming_cache[self._cam_id] = arm
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set_arm(True)

    async def async_turn_off(self, **kwargs):
        await self._set_arm(False)


class _BoschAlarmSettingsSwitchBase(_BoschSwitchBase):
    """Shared base for alarm_settings boolean toggles (alarmMode / preAlarmMode)."""

    _field: str = ""   # field to toggle (alarmMode / preAlarmMode)

    @property
    def _settings(self) -> dict:
        return self.coordinator._alarm_settings_cache.get(self._cam_id, {})

    @property
    def is_on(self) -> bool | None:
        val = self._settings.get(self._field)
        if val is None:
            return None
        return str(val).upper() == "ON"

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
            and bool(self._settings)
        )

    async def _set(self, enabled: bool) -> None:
        cfg = dict(self._settings)
        if not cfg:
            return
        cfg[self._field] = "ON" if enabled else "OFF"
        success = await self.coordinator.async_put_camera(
            self._cam_id, "alarm_settings", cfg
        )
        if success:
            self.coordinator._alarm_settings_cache[self._cam_id] = cfg
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class BoschAlarmModeSwitch(_BoschAlarmSettingsSwitchBase):
    """Switch: main alarm (75 dB siren) ON/OFF — alarm_settings.alarmMode."""

    _field = "alarmMode"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Sirene"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_alarm_mode"
        self._attr_icon            = "mdi:alarm-light"
        self._attr_translation_key = "alarm_mode"
        self._attr_entity_category = EntityCategory.CONFIG


class BoschPreAlarmSwitch(_BoschAlarmSettingsSwitchBase):
    """Switch: Pre-Alarm (LED warning before siren) ON/OFF — alarm_settings.preAlarmMode."""

    _field = "preAlarmMode"

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Pre-Alarm"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_prealarm"
        self._attr_icon            = "mdi:led-on"
        self._attr_translation_key = "pre_alarm"
        self._attr_entity_category = EntityCategory.CONFIG


class BoschAudioAlarmSwitch(_BoschSwitchBase):
    """Switch: basic sound/noise detection ON/OFF (free tier, not Audio+ premium).

    Maps to the "Geräusche" toggle in the iOS app under Ereignisse → Audio.
    This is the FREE sound-threshold detection that triggers an event when the
    ambient noise level exceeds the configured threshold.

    Do NOT confuse with Audio+ (paid subscription) which adds glass-break / smoke /
    CO detection — Audio+ uses a different `audioAlarmConfiguration` value and is
    gated behind a /v11/purchases check. When this switch is ON, the camera's
    audioAlarmConfiguration is "CUSTOM" (free threshold-based detection); "OFF"
    fully disables sound detection.

    PUT /v11/video_inputs/{id}/audioAlarm  body preserves sensitivity/threshold/config,
    only toggles the enabled field.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Geraeusch-Erkennung"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_audio_alarm"
        self._attr_icon            = "mdi:ear-hearing"
        self._attr_translation_key = "audio_alarm"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def _settings(self) -> dict:
        return self.coordinator.audio_alarm_settings(self._cam_id) or {}

    @property
    def is_on(self) -> bool | None:
        s = self._settings
        if not s:
            return None
        return bool(s.get("enabled", False))

    @property
    def available(self) -> bool:
        return (
            self.coordinator.last_update_success
            and self.coordinator.is_camera_online(self._cam_id)
            and bool(self._settings)
        )

    @property
    def extra_state_attributes(self) -> dict:
        s = self._settings
        return {
            "sensitivity":  s.get("sensitivity"),
            "threshold":    s.get("threshold"),
            "configuration": s.get("audioAlarmConfiguration"),
        }

    async def _set(self, enabled: bool) -> None:
        if _is_gen2_indoor(self) and await _warn_if_privacy_on(self, "Geräusch-Erkennung"):
            return
        current = dict(self._settings)
        if not current:
            return
        current["enabled"] = enabled
        # Preserve all other fields — capture shows full body with sensitivity/threshold/config
        success = await self.coordinator.async_put_camera(
            self._cam_id, "audioAlarm", current
        )
        if success:
            cam_data = self.coordinator.data.get(self._cam_id)
            if cam_data is not None:
                cam_data["audioAlarm"] = current
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


# ─────────────────────────────────────────────────────────────────────────────
class BoschImageRotation180Switch(_BoschSwitchBase, RestoreEntity):
    """Switch: ON = display the camera image rotated 180° (ceiling mount).

    Indoor-only — outdoor cameras have a fixed mounting orientation. Bosch's
    Cloud API does not expose any image-rotation field; this switch is a
    pure client-side display flag with three effects:

      1. `camera.async_camera_image()` rotates the snapshot JPEG via PIL
         before serving it (so push notifications, NAS clips, the dashboard
         snapshot, and any other consumer of /api/camera_proxy/ see the
         right-way-up image).
      2. The Lovelace card applies `transform: rotate(180deg)` to its
         <video> element only — the <img> already comes pre-rotated from
         (1), so rotating it again in CSS would cancel out and leave the
         dashboard snapshot looking upside-down.
      3. For PTZ cameras (Gen1 360), `BoschPanNumber` inverts the sign of
         the pan value so "right" on the slider stays "right" on screen
         even when the camera is upside-down.

    State persists across restarts via RestoreEntity. Default: OFF.
    """

    def __init__(self, coordinator, cam_id: str, entry: ConfigEntry) -> None:
        super().__init__(coordinator, cam_id, entry)
        self._attr_name            = f"Bosch {self._cam_title} Bild 180° drehen"
        self._attr_unique_id       = f"bosch_shc_camera_{cam_id}_image_rotation_180"
        self._attr_icon            = "mdi:image-auto-adjust"
        self._attr_translation_key = "image_rotation_180"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator._image_rotation_180.get(self._cam_id, False))

    @property
    def available(self) -> bool:
        # Always available — pure client-side flag, no API dependency
        return self.coordinator.last_update_success

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self.coordinator._image_rotation_180[self._cam_id] = True
            _LOGGER.debug(
                "image_rotation_180: restored ON for %s from previous state",
                self._cam_id[:8],
            )

    async def async_turn_on(self, **kwargs):
        self.coordinator._image_rotation_180[self._cam_id] = True
        self.async_write_ha_state()
        # Notify pan number entity to refresh display value (sign flips).
        self.coordinator.async_update_listeners()

    async def async_turn_off(self, **kwargs):
        self.coordinator._image_rotation_180[self._cam_id] = False
        self.async_write_ha_state()
        self.coordinator.async_update_listeners()
