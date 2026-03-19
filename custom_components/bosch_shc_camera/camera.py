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

from homeassistant.components.camera import Camera
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
    """

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
            "camera_id":   self._cam_id,
            "status":      cam_data.get("status", "UNKNOWN"),
            "last_event":  latest.get("timestamp", "")[:19],
            "event_type":  latest.get("eventType", ""),
            "model":       self._model,
            "firmware":    self._fw,
            "mac":         self._mac,
            "live_rtsps":  rtsps_url,
            "live_proxy":  live.get("proxyUrl", ""),
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
           (proxy-NN.live.cbs.boschsecurity.com snap.jpg, requires HcsoB cookie)
        2. Local camera snap.jpg  — if local_ip + credentials are configured
           (HTTP Digest auth, 1920×1080, only when camera is on local LAN)
        3. Latest event snapshot  — most recent motion-triggered image (cloud events API)
           Always available, but only refreshes on motion.
        """
        session = async_get_clientsession(self.hass, verify_ssl=False)
        token   = self._token
        headers_bearer = {"Authorization": f"Bearer {token}", "Accept": "*/*"}

        # ── 1. Cloud proxy live snapshot ─────────────────────────────────────
        live = self._cam_data.get("live", {})
        proxy_url = live.get("proxyUrl", "")
        if proxy_url:
            cookie = live.get("cookie", "")
            hdrs   = dict(headers_bearer)
            if cookie:
                hdrs["Cookie"] = f"HcsoB={cookie}" if "=" not in cookie else cookie
            try:
                async with async_timeout.timeout(10):
                    async with session.get(proxy_url, headers=hdrs) as resp:
                        ct = resp.headers.get("Content-Type", "")
                        if resp.status == 200 and "image" in ct:
                            self._cached_image = await resp.read()
                            _LOGGER.debug(
                                "%s: live proxy snapshot %d bytes",
                                self._attr_name, len(self._cached_image),
                            )
                            return self._cached_image
                        elif resp.status in (401, 403, 404):
                            # Proxy session expired — clear it
                            _LOGGER.debug(
                                "%s: proxy snapshot %d — clearing live connection",
                                self._attr_name, resp.status,
                            )
                            self.coordinator._live_connections.pop(self._cam_id, None)
            except (asyncio.TimeoutError, aiohttp.ClientError):
                pass

        # ── 2. Local camera snap.jpg (Digest auth) ────────────────────────────
        # Credentials stored in coordinator options or passed via extra config.
        # Only attempted if local_ip is configured.
        local_ip   = self._cam_data.get("info", {}).get("localIp", "")
        local_user = self._cam_data.get("info", {}).get("localUsername", "")
        local_pass = self._cam_data.get("info", {}).get("localPassword", "")
        if local_ip and local_user and local_pass:
            local_url = f"https://{local_ip}/snap.jpg"
            try:
                # Digest auth requires sync requests — run in executor
                img_data = await self.hass.async_add_executor_job(
                    _fetch_local_snap, local_url, local_user, local_pass
                )
                if img_data:
                    self._cached_image = img_data
                    _LOGGER.debug(
                        "%s: local snap %d bytes from %s",
                        self._attr_name, len(img_data), local_ip,
                    )
                    return self._cached_image
            except Exception as err:
                _LOGGER.debug("%s: local snap error: %s", self._attr_name, err)

        # ── 3. Latest event snapshot (motion-triggered) ───────────────────────
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


def _fetch_local_snap(url: str, username: str, password: str) -> bytes | None:
    """Synchronous helper for local camera Digest auth (runs in executor)."""
    import requests as _requests
    from requests.auth import HTTPDigestAuth
    import urllib3
    urllib3.disable_warnings()
    try:
        r = _requests.get(
            url,
            auth=HTTPDigestAuth(username, password),
            timeout=8,
            verify=False,
        )
        if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image"):
            return r.content
    except Exception:
        pass
    return None
