"""Bosch Smart Home Camera — Camera Platform.

Each camera discovered via /v11/video_inputs becomes a HA camera entity.
Images are the latest motion-triggered event snapshots from the cloud API.

If a live proxy connection has been opened (via the "Open Live Stream" button
or the bosch_shc_camera.open_live_connection service), the entity exposes
a stream_source (rtsps:// URL on port 443) for full 30fps H.264 + AAC audio.

Stream URL format:
  rtsps://proxy-NN.live.cbs.boschsecurity.com:443/{hash}/rtsp_tunnel
    ?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60

Note: HA's stream component must support rtsps:// (RTSP over TLS).
The stream requires -tls_verify 0 / insecure TLS (Bosch private CA).
If HA cannot open rtsps://, use ffplay from the Python CLI tool instead.
"""

import asyncio
import logging

import aiohttp
import async_timeout

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, CLOUD_API, get_options

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities — one per discovered Bosch camera."""
    opts = get_options(config_entry)
    if not opts.get("enable_snapshots", True):
        _LOGGER.debug("Camera snapshots disabled in options — skipping camera platform")
        return

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    entities = [
        BoschSHCCamera(coordinator, cam_id, config_entry)
        for cam_id in coordinator.data
    ]
    async_add_entities(entities, update_before_add=False)


class BoschSHCCamera(CoordinatorEntity, Camera):
    """Represents a single Bosch Smart Home camera in Home Assistant.

    • Shows the latest motion-triggered JPEG snapshot (refreshed every scan_interval)
    • Exposes stream_source (RTSP) once a live connection has been established
    • Device groups with sensor and button entities on the same HA device
    • Camera state is "streaming" when live proxy is active, "idle" otherwise
    """

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)

        self._cam_id = cam_id
        self._entry  = entry
        self._cached_image: bytes | None = None

        info = coordinator.data.get(cam_id, {}).get("info", {})
        title = info.get("title", cam_id)

        self._attr_name      = f"Bosch {title}"
        self._attr_unique_id = f"bosch_shc_cam_{cam_id.lower()}"
        self._model = info.get("hardwareVersion", "CAMERA")
        self._fw    = info.get("firmwareVersion", "")
        self._mac   = info.get("macAddress", "")

    # ── Helpers ───────────────────────────────────────────────────────────────
    @property
    def _cam_data(self) -> dict:
        return self.coordinator.data.get(self._cam_id, {})

    # ── Streaming state ───────────────────────────────────────────────────────
    @property
    def is_streaming(self) -> bool:
        """True when a live proxy connection is active.

        Controls the HA camera state: True → "streaming", False → "idle".
        This reflects whether the live stream switch is ON and the proxy
        session is still valid (not expired).
        """
        return self._cam_id in self.coordinator._live_connections

    @property
    def is_recording(self) -> bool:
        return False

    @property
    def _token(self) -> str:
        return self._entry.data.get("bearer_token", "")

    # ── HA metadata ───────────────────────────────────────────────────────────
    @property
    def brand(self) -> str:
        return "Bosch"

    @property
    def model(self) -> str:
        return self._model

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    @property
    def device_info(self) -> dict:
        return {
            "identifiers":  {(DOMAIN, self._cam_id)},
            "name":         self._attr_name,
            "manufacturer": "Bosch",
            "model":        self._model,
            "sw_version":   self._fw,
            "connections":  {("mac", self._mac)} if self._mac else set(),
        }

    @property
    def extra_state_attributes(self) -> dict:
        cam_data = self._cam_data
        events   = cam_data.get("events", [])
        latest   = events[0] if events else {}
        live     = cam_data.get("live", {})
        rtsps_url = live.get("rtspsUrl", live.get("rtspUrl", ""))
        return {
            "camera_id":       self._cam_id,
            "status":          cam_data.get("status", "UNKNOWN"),
            "streaming_state": "active" if self.is_streaming else "idle",
            "last_event":      latest.get("timestamp", "")[:19],
            "event_type":      latest.get("eventType", ""),
            "model":           self._model,
            "firmware":        self._fw,
            "mac":             self._mac,
            "live_rtsps":      rtsps_url,
            "live_proxy":      live.get("proxyUrl", ""),
        }

    # ── Live stream ───────────────────────────────────────────────────────────
    @property
    def stream_source(self) -> str | None:
        """
        Return rtsps:// URL if a live proxy connection has been opened.
        Stream: H.264 1920×1080 30fps + AAC-LC 16kHz mono audio.
        URL format: rtsps://proxy-NN.live.cbs.boschsecurity.com:443/{hash}/rtsp_tunnel
                      ?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60

        To open: press the "Open Live Stream" button or call the service
        bosch_shc_camera.open_live_connection with the camera_id.

        Note: HA's stream component must be able to open rtsps:// with TLS verify disabled.
        If HA's ffmpeg cannot do this, the stream will not appear in the Lovelace card.
        """
        live = self._cam_data.get("live", {})
        return live.get("rtspsUrl") or live.get("rtspUrl") or None

    # ── Snapshot image ────────────────────────────────────────────────────────
    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """
        Return the best available JPEG snapshot, tried in order:

        1. Cloud proxy live snap  — if a live connection has been opened
           (proxy-NN.live.cbs.boschsecurity.com snap.jpg, no auth needed)
           Updated every coordinator tick while live switch is ON.
        2. Latest event snapshot  — most recent motion-triggered image (cloud events API)
           Always available, but only refreshes on motion.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        token   = self._token
        headers_bearer = {"Authorization": f"Bearer {token}", "Accept": "*/*"}

        # ── 1. Cloud proxy live snapshot ─────────────────────────────────────
        live = self.coordinator._live_connections.get(self._cam_id, {})
        proxy_url = live.get("proxyUrl", "")
        if proxy_url:
            try:
                async with async_timeout.timeout(10):
                    async with session.get(proxy_url) as resp:
                        ct = resp.headers.get("Content-Type", "")
                        if resp.status == 200 and "image" in ct:
                            self._cached_image = await resp.read()
                            _LOGGER.debug(
                                "%s: live proxy snapshot %d bytes",
                                self._attr_name, len(self._cached_image),
                            )
                            return self._cached_image
                        elif resp.status in (401, 403, 404):
                            # Proxy session expired — clear it so switch turns OFF
                            _LOGGER.debug(
                                "%s: proxy snapshot %d — clearing live connection",
                                self._attr_name, resp.status,
                            )
                            self.coordinator._live_connections.pop(self._cam_id, None)
                            self.coordinator._live_opened_at.pop(self._cam_id, None)
            except (asyncio.TimeoutError, aiohttp.ClientError):
                pass

        # ── 2. Latest event snapshot (motion-triggered) ───────────────────────
        events = self._cam_data.get("events", [])
        for ev in events:
            img_url = ev.get("imageUrl")
            if not img_url:
                continue
            try:
                async with async_timeout.timeout(20):
                    async with session.get(img_url, headers=headers_bearer) as resp:
                        if resp.status == 200:
                            self._cached_image = await resp.read()
                            _LOGGER.debug(
                                "%s: event snapshot %d bytes @ %s",
                                self._attr_name,
                                len(self._cached_image),
                                ev.get("timestamp", "")[:19],
                            )
                            return self._cached_image
                        elif resp.status == 401:
                            _LOGGER.warning(
                                "%s: token expired — update via integration options",
                                self._attr_name,
                            )
                            return self._cached_image
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug("%s: event snapshot error: %s", self._attr_name, err)
            break  # only try the first event

        # Return last cached image if all methods failed
        return self._cached_image
