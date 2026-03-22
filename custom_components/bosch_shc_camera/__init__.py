"""Bosch Smart Home Camera — Home Assistant Custom Integration.

Provides camera, sensor and button entities for all Bosch Smart Home cameras
via the Bosch Cloud API (residential.cbs.boschsecurity.com).

Features (all toggleable in Options):
  • Camera snapshot entities  — latest motion-triggered JPEG per camera
  • Status + event sensors    — ONLINE/OFFLINE, last event timestamp, events-today count
  • Snapshot trigger buttons  — force immediate refresh; "Open Live Stream" button
  • Auto-download             — background download of all event files to a local folder
  • Live stream               — full 30fps H.264 1920×1080 + AAC audio via rtsps://:443
                                 ConnectionType "REMOTE" → proxy-NN:443/{hash}/rtsp_tunnel

Installation:
  1. Copy bosch_shc_camera/ to /config/custom_components/
  2. Restart Home Assistant
  3. Settings → Integrations → Add → "Bosch Smart Home Camera"
  4. Enter Bearer token

No user data is hardcoded. All configuration via the HA UI.
"""

import asyncio
import logging
import os
import time
from datetime import timedelta

import aiohttp
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession, async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

DOMAIN     = "bosch_shc_camera"
CLOUD_API  = "https://residential.cbs.boschsecurity.com"

ALL_PLATFORMS = ["camera", "sensor", "button", "switch", "number", "select"]

# ConnectionType enum — confirmed working value: "REMOTE"
# REMOTE → cloud proxy, fast (~1.5s), no credentials, works from anywhere
# LOCAL  → LAN direct, returns Digest user/password, slow (~15s)
LIVE_TYPE_CANDIDATES = ["REMOTE", "LOCAL"]  # REMOTE (cloud) first, LOCAL (LAN) as fallback
LIVE_SESSION_TTL = 55  # seconds — proxy sessions last ~60s, expire 5s early to be safe

DEFAULT_OPTIONS = {
    "scan_interval":      60,    # coordinator tick interval (seconds)
    "interval_status":   300,   # ping camera status every 5 minutes
    "interval_events":   300,   # fetch new events every 5 minutes
    "snapshot_interval": 1800,  # how often to fetch a fresh cloud snapshot (seconds, 30 min)
    "enable_snapshots":       True,
    "enable_sensors":         True,
    "enable_snapshot_button": True,
    "enable_auto_download":   False,
    "download_path":          "",
    # SHC local API — for camera light + privacy mode control
    "shc_ip":        "",   # e.g. 192.168.20.4
    "shc_cert_path": "",   # path to client cert PEM (e.g. /config/claude_cert.pem)
    "shc_key_path":  "",   # path to client key PEM  (e.g. /config/claude_key.pem)
    "high_quality_video": False,  # True = highQualityVideo: True in PUT /connection
}


def get_options(entry: ConfigEntry) -> dict:
    """Return entry options merged with defaults."""
    opts = dict(DEFAULT_OPTIONS)
    opts.update(entry.options)
    return opts


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraCoordinator(DataUpdateCoordinator):
    """
    Shared coordinator — fetches all camera data once per scan_interval.
    All entity types (camera, sensor, button) read from coordinator.data
    rather than making independent API calls.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        opts = get_options(entry)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=int(opts.get("scan_interval", 60))),
        )
        # Live-stream proxy info — keyed by cam_id, cleared after LIVE_SESSION_TTL seconds
        self._live_connections: dict[str, dict] = {}
        self._live_opened_at:   dict[str, float] = {}   # timestamp when session was opened
        # Per-camera audio setting — True = audio on (default), False = muted
        self._audio_enabled:    dict[str, bool]  = {}
        # Camera entity references — registered on entity setup, used by button/service
        self._camera_entities: dict = {}
        # Per-type last-fetched timestamps (0 = never → fetch immediately)
        self._last_status: float = 0.0
        self._last_events: float = 0.0
        self._last_slow:   float = 0.0   # wifiinfo / ambient / RCP / motion / audio / recording
        # Cached data for types that are not re-fetched this tick
        self._cached_status: dict[str, str] = {}
        self._cached_events: dict[str, list] = {}
        # SHC local API state cache — keyed by cam_id
        # Each entry: {"device_id": str, "camera_light": bool|None, "privacy_mode": bool|None}
        self._shc_state_cache: dict[str, dict] = {}
        self._shc_devices_raw: list = []       # cached GET /smarthome/devices response
        self._last_shc_fetch: float = 0.0      # last time SHC devices were fetched
        # Pan position cache — keyed by cam_id, only populated for cameras with panLimit > 0
        self._pan_cache: dict[str, int | None] = {}
        # WiFi info cache — keyed by cam_id, populated from GET /wifiinfo
        self._wifiinfo_cache: dict[str, dict] = {}
        # Ambient light sensor cache — keyed by cam_id, populated from GET /ambient_light_sensor_level
        self._ambient_light_cache: dict[str, float | None] = {}
        # RCP data caches — keyed by cam_id, populated via RCP protocol over cloud proxy
        self._rcp_dimmer_cache: dict[str, int | None] = {}    # LED dimmer value 0–100
        self._rcp_privacy_cache: dict[str, int | None] = {}   # privacy mask byte[1] (1=ON)
        self._rcp_clock_offset_cache: dict[str, float | None] = {}  # camera clock offset vs server (seconds)
        self._rcp_lan_ip_cache: dict[str, str | None] = {}          # camera LAN IP via RCP 0x0a36
        self._rcp_product_name_cache: dict[str, str | None] = {}    # camera product name via RCP 0x0aea
        self._rcp_bitrate_cache: dict[str, list[int]] = {}          # bitrate ladder kbps from 0x0c81
        # Video quality preference — keyed by cam_id, runtime only (not persisted)
        # Values: "auto" | "high" | "low"
        self._quality_preference: dict[str, str] = {}

    # ── Properties ────────────────────────────────────────────────────────────
    @property
    def token(self) -> str:
        return self._entry.data.get("bearer_token", "")

    @property
    def refresh_token(self) -> str:
        return self._entry.data.get("refresh_token", "")

    @property
    def options(self) -> dict:
        return get_options(self._entry)

    # ── Token renewal ─────────────────────────────────────────────────────────
    async def _ensure_valid_token(self) -> str:
        """
        Return a valid bearer token.
        If the current token is expired (401), try silent renewal via refresh_token.
        Updates the config entry data with the new tokens if renewed.
        """
        from .config_flow import _do_refresh
        token = self.token
        refresh = self.refresh_token
        if not token and not refresh:
            raise UpdateFailed("Not authenticated — add the integration again to log in")
        if not refresh:
            return token  # No refresh token — use whatever we have
        # Try to renew silently
        session = async_get_clientsession(self.hass, verify_ssl=False)
        tokens = await _do_refresh(session, refresh)
        if tokens:
            new_access  = tokens.get("access_token", token)
            new_refresh = tokens.get("refresh_token", refresh)
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={
                    **self._entry.data,
                    "bearer_token":  new_access,
                    "refresh_token": new_refresh,
                },
            )
            _LOGGER.debug("Bearer token renewed silently via refresh_token")
            return new_access
        _LOGGER.warning("Silent token renewal failed — using existing token")
        return token

    # ── Main update ───────────────────────────────────────────────────────────
    async def _async_update_data(self) -> dict:
        """
        Coordinator tick — runs every scan_interval seconds.
        Each data type (status, events) is only re-fetched when its own
        interval has elapsed, reducing unnecessary API traffic.

        Returns dict keyed by cam_id:
          {
            "info":   {...},    # from GET /v11/video_inputs (every tick)
            "status": "ONLINE", # from ping — only when interval_status elapsed
            "events": [...],    # from events API — only when interval_events elapsed
            "live":   {...},    # cached proxy info from PUT /connection
          }
        """
        token = self.token
        if not token and not self.refresh_token:
            raise UpdateFailed("Not authenticated — re-add the integration to log in")

        opts    = self.options
        now     = time.monotonic()

        do_status = (now - self._last_status) >= int(opts.get("interval_status", 60))
        do_events = (now - self._last_events) >= int(opts.get("interval_events", 60))
        # Slow tier — wifiinfo, ambient light, RCP, motion, audio alarm, recording (every 5 min)
        do_slow   = (now - self._last_slow)   >= int(opts.get("interval_slow", 300))

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            # ── 1. List cameras (every tick — lightweight, needed for entity list) ──
            async with async_timeout.timeout(15):
                async with session.get(
                    f"{CLOUD_API}/v11/video_inputs", headers=headers
                ) as resp:
                    if resp.status == 401:
                        _LOGGER.info("Token expired (401) — attempting silent renewal")
                        token = await self._ensure_valid_token()
                        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
                    elif resp.status != 200:
                        raise UpdateFailed(f"Camera list returned HTTP {resp.status}")
                    else:
                        cam_list = await resp.json()

            # Retry after renewal if we got a 401
            if resp.status == 401:
                async with async_timeout.timeout(15):
                    async with session.get(
                        f"{CLOUD_API}/v11/video_inputs", headers=headers
                    ) as resp2:
                        if resp2.status == 401:
                            raise UpdateFailed(
                                "Token expired and renewal failed — go to Settings → Integrations → "
                                "Bosch Smart Home Camera → Configure → Force new browser login"
                            )
                        if resp2.status != 200:
                            raise UpdateFailed(f"Camera list returned HTTP {resp2.status}")
                        cam_list = await resp2.json()

            data: dict = {}

            for cam in cam_list:
                cam_id = cam.get("id", "")
                if not cam_id:
                    continue

                # ── 2. Ping status — only when interval_status elapsed ─────────
                if do_status:
                    status = "UNKNOWN"
                    try:
                        async with async_timeout.timeout(8):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id}/ping",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    status = (await r.text()).strip().strip('"')
                    except Exception as err:
                        _LOGGER.debug("Ping error for %s: %s", cam_id, err)
                    self._cached_status[cam_id] = status
                else:
                    status = self._cached_status.get(cam_id, "UNKNOWN")

                # ── 3. Events — only when interval_events elapsed ─────────────
                if do_events:
                    events: list = []
                    try:
                        url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=20"
                        async with async_timeout.timeout(15):
                            async with session.get(url, headers=headers) as r:
                                if r.status == 200:
                                    events = await r.json()
                    except Exception as err:
                        _LOGGER.debug("Events fetch error for %s: %s", cam_id, err)
                    self._cached_events[cam_id] = events
                else:
                    events = self._cached_events.get(cam_id, [])

                data[cam_id] = {
                    "info":   cam,
                    "status": status,
                    "events": events,
                    "live":   self._live_connections.get(cam_id, {}),
                }

            # Update timestamps only after successful fetches
            if do_status:
                self._last_status = now
            if do_events:
                self._last_events = now
            if do_slow:
                self._last_slow = now

            # ── 4. Read privacy mode + light support from cloud API response ─────
            # privacyMode and featureSupport are already in the /v11/video_inputs
            # response — no extra request needed. Populate _shc_state_cache from
            # cloud data so the privacy switch works without SHC configured.
            for cam_id_key, cam_entry in data.items():
                cam_raw = cam_entry.get("info", {})
                privacy_str  = cam_raw.get("privacyMode", "")
                feat_support = cam_raw.get("featureSupport", {})
                has_light    = feat_support.get("light", False)
                feat_status  = cam_raw.get("featureStatus", {})
                light_on     = feat_status.get("frontIlluminatorInGeneralLightOn")

                cache = self._shc_state_cache.setdefault(cam_id_key, {
                    "device_id":           None,
                    "camera_light":        None,
                    "privacy_mode":        None,
                    "has_light":           False,
                    "notifications_status": None,
                })
                # Always update privacy from cloud API (authoritative, fast, no SHC needed)
                if privacy_str:
                    cache["privacy_mode"] = (privacy_str.upper() == "ON")
                cache["has_light"] = has_light
                # Use cloud featureStatus for light state only if SHC hasn't set it yet
                # (SHC CameraLight service is more accurate for the current LED state)
                if light_on is not None and cache.get("camera_light") is None:
                    cache["camera_light"] = light_on
                # Read notifications status from cloud API response
                notif_status = cam_raw.get("notificationsEnabledStatus", "")
                if notif_status:
                    cache["notifications_status"] = notif_status

                # Fetch pan position for cameras that support it
                pan_limit = cam_raw.get("featureSupport", {}).get("panLimit", 0)
                if pan_limit:
                    try:
                        async with async_timeout.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/pan",
                                headers=headers,
                            ) as pan_resp:
                                if pan_resp.status == 200:
                                    pan_data = await pan_resp.json()
                                    self._pan_cache[cam_id_key] = pan_data.get("currentAbsolutePosition")
                    except Exception as err:
                        _LOGGER.debug("Pan fetch error for %s: %s", cam_id_key, err)

                # ── Slow tier: wifiinfo, ambient light, motion, audio, recording ──
                # Only fetched every interval_slow seconds (default 5 min).
                # These values change rarely — fetching every tick wastes bandwidth.
                if do_slow:
                    # WiFi info (signal strength, IP, SSID)
                    try:
                        async with async_timeout.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/wifiinfo",
                                headers=headers,
                            ) as wifi_resp:
                                if wifi_resp.status == 200:
                                    self._wifiinfo_cache[cam_id_key] = await wifi_resp.json()
                                else:
                                    _LOGGER.debug(
                                        "wifiinfo HTTP %d for %s", wifi_resp.status, cam_id_key
                                    )
                    except Exception as err:
                        _LOGGER.debug("WiFi info fetch error for %s: %s", cam_id_key, err)

                    # Ambient light sensor level
                    try:
                        async with async_timeout.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/ambient_light_sensor_level",
                                headers=headers,
                            ) as al_resp:
                                if al_resp.status == 200:
                                    al_data = await al_resp.json()
                                    self._ambient_light_cache[cam_id_key] = al_data.get(
                                        "ambientLightSensorLevel"
                                    )
                                else:
                                    _LOGGER.debug(
                                        "ambient_light_sensor_level HTTP %d for %s",
                                        al_resp.status, cam_id_key,
                                    )
                    except Exception as err:
                        _LOGGER.debug("Ambient light fetch error for %s: %s", cam_id_key, err)

                    # Motion detection settings
                    try:
                        async with async_timeout.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/motion",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    data[cam_id_key]["motion"] = await r.json()
                    except Exception as err:
                        _LOGGER.debug("Motion fetch error for %s: %s", cam_id_key, err)

                    # Audio alarm settings
                    try:
                        async with async_timeout.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/audioAlarm",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    data[cam_id_key]["audioAlarm"] = await r.json()
                    except Exception as err:
                        _LOGGER.debug("AudioAlarm fetch error for %s: %s", cam_id_key, err)

                    # Recording options
                    try:
                        async with async_timeout.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/recording_options",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    data[cam_id_key]["recordingOptions"] = await r.json()
                    except Exception as err:
                        _LOGGER.debug("Recording options fetch error for %s: %s", cam_id_key, err)

                # ── RCP data via cloud proxy (slow tier — every 5 min) ────────
                # Opens a proxy connection and reads multiple RCP values.
                # Only when camera is ONLINE and slow-tier interval elapsed.
                cam_status = self._cached_status.get(cam_id_key, "UNKNOWN")
                if cam_status == "ONLINE" and do_slow:
                    try:
                        rcp_connector = aiohttp.TCPConnector(ssl=False)
                        rcp_session   = aiohttp.ClientSession(connector=rcp_connector)
                        rcp_headers   = {
                            "Authorization": f"Bearer {token}",
                            "Content-Type":  "application/json",
                            "Accept":        "application/json",
                        }
                        try:
                            async with async_timeout.timeout(10):
                                async with rcp_session.put(
                                    f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/connection",
                                    json={"type": "REMOTE", "highQualityVideo": self.get_quality_params(cam_id_key)[0]},
                                    headers=rcp_headers,
                                ) as conn_resp:
                                    if conn_resp.status in (200, 201):
                                        import json as _json
                                        conn_data = _json.loads(await conn_resp.text())
                                        urls = conn_data.get("urls", [])
                                        if urls:
                                            # urls[0] = "proxy-NN.live.cbs.boschsecurity.com:42090/{hash}"
                                            parts = urls[0].split("/", 1)
                                            if len(parts) == 2:
                                                proxy_host = parts[0]  # "proxy-NN:42090"
                                                proxy_hash = parts[1]  # "{hash}"
                                                await self._async_update_rcp_data(
                                                    cam_id_key, proxy_host, proxy_hash
                                                )
                                    else:
                                        _LOGGER.debug(
                                            "RCP proxy connection HTTP %d for %s",
                                            conn_resp.status, cam_id_key,
                                        )
                        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                            _LOGGER.debug("RCP proxy connect error for %s: %s", cam_id_key, err)
                        finally:
                            await rcp_session.close()
                            await rcp_connector.close()
                    except Exception as err:
                        _LOGGER.debug("RCP update skipped for %s: %s", cam_id_key, err)

            # ── 5. SHC states (camera light current state) ────────────────────
            # Privacy mode is now read from cloud API above (step 4).
            # SHC is only used for camera light state + control (no cloud API found).
            if opts.get("shc_ip", "").strip():
                try:
                    await self._async_update_shc_states(data)
                except Exception as err:
                    _LOGGER.debug("SHC state update error: %s", err)

            # ── 6. Auto-download new event files ──────────────────────────────
            if do_events and opts.get("enable_auto_download") and opts.get("download_path"):
                await self.hass.async_add_executor_job(
                    self._sync_download, data, token, opts["download_path"]
                )

            return data

        except UpdateFailed:
            raise
        except asyncio.TimeoutError:
            raise UpdateFailed("Timeout fetching camera data from Bosch cloud")
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Network error: {err}")

    # ── Live stream ───────────────────────────────────────────────────────────
    async def try_live_connection(self, cam_id: str) -> dict | None:
        """
        Open a live proxy connection via PUT /v11/video_inputs/{id}/connection.
        Uses "REMOTE" (confirmed working) → cloud proxy, fast (~1.5s).
        On success stores:
          - proxyUrl:  https://proxy-NN:42090/{hash}/snap.jpg  (current image, no auth)
          - rtspsUrl:  rtsps://proxy-NN:443/{hash}/rtsp_tunnel?... (30fps H.264+AAC audio)
        Returns the enriched response dict, or None on failure.
        """
        token = self.token
        if not token:
            _LOGGER.warning("try_live_connection: no token available")
            return None

        # Use a dedicated session with SSL verification disabled.
        # async_get_clientsession(verify_ssl=False) shares a session but the
        # verify_ssl flag may not apply to all requests in newer HA versions.
        # async_create_clientsession creates a fresh session we fully control.
        connector = aiohttp.TCPConnector(ssl=False)
        session   = aiohttp.ClientSession(connector=connector)

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        try:
            hq, inst = self.get_quality_params(cam_id)
            for type_val in LIVE_TYPE_CANDIDATES:
                try:
                    async with async_timeout.timeout(10):
                        async with session.put(
                            url,
                            json={"type": type_val, "highQualityVideo": hq},
                            headers=headers,
                        ) as resp:
                            body = await resp.text()
                            _LOGGER.debug(
                                "PUT /connection type=%s → HTTP %d: %s",
                                type_val, resp.status, body[:200],
                            )
                            if resp.status in (200, 201):
                                import json as _json
                                result = _json.loads(body)
                                _LOGGER.info(
                                    "Live connection opened! type=%s → %s", type_val, result
                                )
                                # Build URLs from the 'urls' array in the response
                                # urls[0] = "proxy-NN.live.cbs.boschsecurity.com:42090/{hash}"
                                urls = result.get("urls", [])
                                if urls:
                                    proxy_host_path = urls[0]
                                    result["proxyUrl"] = f"https://{proxy_host_path}/snap.jpg"
                                    rtsps_host_path   = proxy_host_path.replace(":42090", ":443")
                                    audio_param = "&enableaudio=1" if self._audio_enabled.get(cam_id, False) else ""
                                    result["rtspsUrl"] = (
                                        f"rtsps://{rtsps_host_path}/rtsp_tunnel"
                                        f"?inst={inst}{audio_param}&fmtp=1&maxSessionDuration=3600"
                                    )
                                    result["rtspUrl"] = result["rtspsUrl"]
                                elif result.get("hash"):
                                    h  = result["hash"]
                                    ph = result.get("proxyHost", "proxy-01.live.cbs.boschsecurity.com")
                                    pp = result.get("proxyPort", 42090)
                                    result["proxyUrl"] = f"https://{ph}:{pp}/{h}/snap.jpg"
                                    audio_param = "&enableaudio=1" if self._audio_enabled.get(cam_id, False) else ""
                                    result["rtspsUrl"] = (
                                        f"rtsps://{ph}:443/{h}/rtsp_tunnel"
                                        f"?inst={inst}{audio_param}&fmtp=1&maxSessionDuration=3600"
                                    )
                                    result["rtspUrl"] = result["rtspsUrl"]
                                self._live_connections[cam_id] = result
                                self._live_opened_at[cam_id]   = time.monotonic()
                                # Register stream in go2rtc with TLS verification
                                # disabled so HA's camera card can show live video+audio.
                                rtsps_url = result.get("rtspsUrl", "")
                                if rtsps_url:
                                    await self._register_go2rtc_stream(cam_id, rtsps_url)
                                await self.async_request_refresh()
                                return result
                            elif resp.status == 401:
                                _LOGGER.warning(
                                    "try_live_connection: token expired (401) for %s", cam_id
                                )
                                return None
                            else:
                                _LOGGER.warning(
                                    "try_live_connection: HTTP %d for type=%s: %s",
                                    resp.status, type_val, body[:200],
                                )
                except asyncio.TimeoutError:
                    _LOGGER.warning("try_live_connection: timeout for type=%s", type_val)
                except aiohttp.ClientError as err:
                    _LOGGER.warning("try_live_connection: connection error for type=%s: %s", type_val, err)
        finally:
            await session.close()

        _LOGGER.warning("Could not open live connection for %s — all types failed", cam_id)
        return None

    # ── go2rtc integration ────────────────────────────────────────────────────
    async def async_fetch_live_snapshot(self, cam_id: str) -> bytes | None:
        """Open a temporary REMOTE live connection to fetch a fresh snap.jpg.

        Does NOT register the connection in _live_connections — the live stream
        switch stays OFF. Used by background image refresh so cameras always
        show a current image rather than a (possibly expired) event snapshot.
        """
        token = self.token
        if not token:
            return None

        connector = aiohttp.TCPConnector(ssl=False)
        session   = aiohttp.ClientSession(connector=connector)
        headers   = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        try:
            async with async_timeout.timeout(10):
                async with session.put(
                    url,
                    json={"type": "REMOTE", "highQualityVideo": self.get_quality_params(cam_id)[0]},
                    headers=headers,
                ) as resp:
                    if resp.status not in (200, 201):
                        _LOGGER.debug(
                            "fetch_live_snapshot: PUT /connection → HTTP %d for %s",
                            resp.status, cam_id,
                        )
                        return None
                    import json as _json
                    result = _json.loads(await resp.text())
                    urls = result.get("urls", [])
                    if not urls:
                        return None
                    proxy_url = f"https://{urls[0]}/snap.jpg"

            async with async_timeout.timeout(10):
                async with session.get(proxy_url) as snap_resp:
                    ct = snap_resp.headers.get("Content-Type", "")
                    if snap_resp.status == 404:
                        # Proxy URL expired — re-request connection and retry once
                        _LOGGER.debug(
                            "fetch_live_snapshot: snap.jpg 404 for %s — proxy URL expired, retrying",
                            cam_id,
                        )
                        async with async_timeout.timeout(10):
                            async with session.put(
                                url,
                                json={"type": "REMOTE", "highQualityVideo": self.get_quality_params(cam_id)[0]},
                                headers=headers,
                            ) as resp2:
                                if resp2.status not in (200, 201):
                                    return None
                                result2 = _json.loads(await resp2.text())
                                urls2 = result2.get("urls", [])
                                if not urls2:
                                    return None
                                proxy_url = f"https://{urls2[0]}/snap.jpg"
                        async with async_timeout.timeout(10):
                            async with session.get(proxy_url) as snap_resp2:
                                ct2 = snap_resp2.headers.get("Content-Type", "")
                                if snap_resp2.status == 200 and "image" in ct2:
                                    data = await snap_resp2.read()
                                    if data:
                                        return data
                        return None
                    if snap_resp.status == 200 and "image" in ct:
                        data = await snap_resp.read()
                        # Bosch returns HTTP 200 with 0 bytes when privacy mode is ON
                        if not data:
                            _LOGGER.debug(
                                "fetch_live_snapshot: %s → empty response (privacy mode ON?)",
                                cam_id,
                            )
                            return None
                        _LOGGER.debug(
                            "fetch_live_snapshot: %s → %d bytes", cam_id, len(data)
                        )
                        return data
                    _LOGGER.debug(
                        "fetch_live_snapshot: snap.jpg → HTTP %d for %s",
                        snap_resp.status, cam_id,
                    )
                    return None

        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("fetch_live_snapshot error for %s: %s", cam_id, err)
            return None
        finally:
            await session.close()

    async def async_fetch_fresh_event_snapshot(self, cam_id: str) -> bytes | None:
        """Fetch fresh events from Bosch API and return the latest event JPEG.

        Used as fallback for cameras whose snap.jpg returns 401 (e.g. CAMERA_360).
        Bypasses the coordinator's cached event list — always hits Bosch API directly
        so the returned imageUrl is always fresh (not expired).
        """
        token = self.token
        if not token:
            return None

        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session  = async_get_clientsession(self.hass, verify_ssl=False)
        headers  = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        events_url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}"

        try:
            async with async_timeout.timeout(15):
                async with session.get(events_url, headers=headers) as resp:
                    if resp.status != 200:
                        _LOGGER.debug(
                            "fetch_fresh_event_snapshot: events HTTP %d for %s",
                            resp.status, cam_id,
                        )
                        return None
                    import json as _json
                    events = _json.loads(await resp.text())

            if not events:
                return None

            # Try each event URL from newest to oldest
            img_headers = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
            for ev in events:
                img_url = ev.get("imageUrl")
                if not img_url:
                    continue
                try:
                    async with async_timeout.timeout(20):
                        async with session.get(img_url, headers=img_headers) as snap_resp:
                            if snap_resp.status == 200:
                                data = await snap_resp.read()
                                if data:
                                    _LOGGER.debug(
                                        "fetch_fresh_event_snapshot: %s → %d bytes @ %s",
                                        cam_id, len(data), ev.get("timestamp", "")[:19],
                                    )
                                    return data
                except (asyncio.TimeoutError, aiohttp.ClientError):
                    continue

        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("fetch_fresh_event_snapshot error for %s: %s", cam_id, err)

        return None

    async def async_fetch_live_snapshot_local(self, cam_id: str) -> bytes | None:
        """Fetch a live snapshot via LOCAL connection using HTTP Digest auth.

        For cameras like CAMERA_360 whose REMOTE snap.jpg returns 401,
        this opens a LOCAL connection to get Digest credentials and fetches
        snap.jpg directly from the camera's LAN IP.

        Runs in an executor thread since requests (sync) is used for Digest auth.
        """
        token = self.token
        if not token:
            return None

        connector = aiohttp.TCPConnector(ssl=False)
        session   = aiohttp.ClientSession(connector=connector)
        headers   = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        result = None
        try:
            async with async_timeout.timeout(15):
                async with session.put(
                    url, json={"type": "LOCAL", "highQualityVideo": self.get_quality_params(cam_id)[0]}, headers=headers
                ) as resp:
                    if resp.status not in (200, 201):
                        _LOGGER.debug(
                            "fetch_live_snapshot_local: PUT LOCAL → HTTP %d for %s",
                            resp.status, cam_id,
                        )
                        return None
                    import json as _json
                    result = _json.loads(await resp.text())
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("fetch_live_snapshot_local: PUT error for %s: %s", cam_id, err)
            return None
        finally:
            await session.close()

        user     = result.get("user")
        password = result.get("password")
        urls     = result.get("urls", [])
        if not user or not password or not urls:
            _LOGGER.debug(
                "fetch_live_snapshot_local: missing credentials/urls for %s: %s",
                cam_id, result,
            )
            return None

        camera_host = urls[0]  # e.g. "192.168.20.21:443"
        snap_url    = f"https://{camera_host}/snap.jpg"

        def _fetch_digest() -> bytes | None:
            import requests as req
            import urllib3
            urllib3.disable_warnings()
            try:
                r = req.get(
                    snap_url,
                    auth=req.auth.HTTPDigestAuth(user, password),
                    verify=False,
                    timeout=10,
                )
                if r.status_code == 200 and "image" in r.headers.get("Content-Type", ""):
                    _LOGGER.debug(
                        "fetch_live_snapshot_local: %s → %d bytes via Digest",
                        cam_id, len(r.content),
                    )
                    return r.content
                _LOGGER.debug(
                    "fetch_live_snapshot_local: Digest snap.jpg → HTTP %d for %s",
                    r.status_code, cam_id,
                )
            except Exception as err:
                _LOGGER.debug("fetch_live_snapshot_local: requests error for %s: %s", cam_id, err)
            return None

        return await self.hass.async_add_executor_job(_fetch_digest)

    async def _register_go2rtc_stream(self, cam_id: str, rtsps_url: str) -> None:
        """Register the Bosch rtsps:// stream in go2rtc with TLS verify disabled.

        go2rtc is HA's built-in RTSP→WebRTC bridge (port 1984 locally).
        Appending #insecure to the URL tells go2rtc to skip TLS verification,
        which is required for Bosch's private CA certificate.

        Once registered, HA's camera card can display live 30fps H.264 + AAC audio
        via WebRTC or HLS directly from the go2rtc bridge.

        The stream is registered under the camera entity unique_id so HA's stream
        component can find it automatically.
        """
        # go2rtc stream name must match what HA's stream component uses.
        # HA registers streams under the camera entity unique_id.
        stream_name = f"bosch_shc_cam_{cam_id.lower()}"
        # Append #insecure to skip TLS certificate verification
        go2rtc_src = f"{rtsps_url}#insecure=1"

        try:
            async with async_timeout.timeout(5):
                async with aiohttp.ClientSession() as s:
                    # go2rtc API: PUT /api/streams?src=URL&name=STREAM_NAME
                    resp = await s.put(
                        f"http://localhost:1984/api/streams",
                        params={"src": go2rtc_src, "name": stream_name},
                    )
                    _LOGGER.debug(
                        "go2rtc stream '%s' registered → HTTP %d (src: %s)",
                        stream_name, resp.status, go2rtc_src[:80],
                    )
        except asyncio.TimeoutError:
            _LOGGER.debug("go2rtc API not reachable (timeout) — live stream only via snap.jpg")
        except aiohttp.ClientError as err:
            _LOGGER.debug("go2rtc API not reachable (%s) — live stream only via snap.jpg", err)

    async def _unregister_go2rtc_stream(self, cam_id: str) -> None:
        """Remove the camera stream from go2rtc when the live session ends."""
        stream_name = f"bosch_shc_cam_{cam_id.lower()}"
        try:
            async with async_timeout.timeout(3):
                async with aiohttp.ClientSession() as s:
                    await s.delete(
                        f"http://localhost:1984/api/streams",
                        params={"name": stream_name},
                    )
                    _LOGGER.debug("go2rtc stream '%s' removed", stream_name)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            pass  # go2rtc may not be running — silently ignore

    # ── RCP protocol (Bosch Remote Configuration Protocol via cloud proxy) ──────
    async def _rcp_session(self, proxy_host: str, proxy_hash: str) -> str | None:
        """Open an RCP session via the cloud proxy and return the sessionid, or None on failure.

        The RCP handshake consists of two steps:
          1. WRITE command 0xff0c with a fixed payload → extract <sessionid> from XML response
          2. WRITE command 0xff0d with the sessionid → ACK (confirms the session)

        Auth=3 (anonymous via URL hash) provides read-only access.
        The proxy_host should be in the form "proxy-NN.live.cbs.boschsecurity.com:42090".
        """
        base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"
        init_payload = "0x0102004000000000040000000000000000010000000000000001000000000000"

        connector = aiohttp.TCPConnector(ssl=False)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                # Step 1: open session
                params1 = {
                    "command":   "0xff0c",
                    "direction": "WRITE",
                    "type":      "P_OCTET",
                    "payload":   init_payload,
                }
                try:
                    async with async_timeout.timeout(8):
                        async with session.get(base, params=params1) as resp:
                            if resp.status != 200:
                                _LOGGER.debug(
                                    "_rcp_session: step1 HTTP %d for %s", resp.status, proxy_host
                                )
                                return None
                            text = await resp.text()
                except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                    _LOGGER.debug("_rcp_session: step1 error for %s: %s", proxy_host, err)
                    return None

                # Parse <sessionid> from XML response
                import re as _re
                m = _re.search(r"<sessionid>(\S+)</sessionid>", text, _re.IGNORECASE)
                if not m:
                    _LOGGER.debug(
                        "_rcp_session: no <sessionid> in response for %s: %s",
                        proxy_host, text[:200],
                    )
                    return None
                session_id = m.group(1)

                # Step 2: ACK the session
                params2 = {
                    "command":   "0xff0d",
                    "direction": "WRITE",
                    "type":      "P_OCTET",
                    "sessionid": session_id,
                }
                try:
                    async with async_timeout.timeout(8):
                        async with session.get(base, params=params2) as resp2:
                            _LOGGER.debug(
                                "_rcp_session: ACK HTTP %d for %s (sessionid=%s)",
                                resp2.status, proxy_host, session_id,
                            )
                except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                    _LOGGER.debug("_rcp_session: step2 error for %s: %s", proxy_host, err)
                    # Session may still be valid — return it anyway

                return session_id
        finally:
            await connector.close()

    async def _rcp_read(
        self,
        rcp_base: str,
        command: str,
        sessionid: str,
        type_: str = "P_OCTET",
        num: int = 0,
    ) -> bytes | None:
        """READ an RCP command and return the raw payload bytes, or None on failure.

        Args:
            rcp_base:  Full RCP base URL, e.g. "https://proxy-NN:42090/{hash}/rcp.xml"
            command:   Hex command string, e.g. "0x0c22"
            sessionid: Session ID from _rcp_session()
            type_:     RCP type string, e.g. "P_OCTET" or "T_WORD"
            num:       num parameter (0 = omit)
        """
        params: dict = {
            "command":   command,
            "direction": "READ",
            "type":      type_,
            "sessionid": sessionid,
        }
        if num:
            params["num"] = str(num)

        connector = aiohttp.TCPConnector(ssl=False)
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                try:
                    async with async_timeout.timeout(8):
                        async with session.get(rcp_base, params=params) as resp:
                            if resp.status != 200:
                                _LOGGER.debug(
                                    "_rcp_read: command=%s HTTP %d", command, resp.status
                                )
                                return None
                            return await resp.read()
                except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                    _LOGGER.debug("_rcp_read: command=%s error: %s", command, err)
                    return None
        finally:
            await connector.close()

    async def _async_update_rcp_data(self, cam_id: str, proxy_host: str, proxy_hash: str) -> None:
        """Fetch RCP data (LED dimmer, privacy state) for a camera via cloud proxy.

        Opens a fresh RCP session, reads 0x0c22 (LED dimmer) and 0x0d00 (privacy mask),
        and caches the results. Gracefully skips on any failure — RCP is read-only
        supplementary data and must never block the main coordinator update.
        """
        session_id = await self._rcp_session(proxy_host, proxy_hash)
        if not session_id:
            _LOGGER.debug("_async_update_rcp_data: could not open RCP session for %s", cam_id)
            return

        rcp_base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"

        # Read LED dimmer (0x0c22) — T_WORD, num=1 → integer 0–100
        try:
            raw = await self._rcp_read(rcp_base, "0x0c22", session_id, type_="T_WORD", num=1)
            if raw and len(raw) >= 2:
                import struct as _struct
                dimmer_val = _struct.unpack(">H", raw[:2])[0]
                self._rcp_dimmer_cache[cam_id] = int(dimmer_val)
                _LOGGER.debug("RCP LED dimmer for %s: %d%%", cam_id, dimmer_val)
        except Exception as err:
            _LOGGER.debug("RCP dimmer read error for %s: %s", cam_id, err)

        # Read privacy mask (0x0d00) — P_OCTET 4B → byte[1]=1 means ON
        try:
            raw = await self._rcp_read(rcp_base, "0x0d00", session_id, type_="P_OCTET")
            if raw and len(raw) >= 2:
                self._rcp_privacy_cache[cam_id] = int(raw[1])
                _LOGGER.debug(
                    "RCP privacy mask for %s: byte[1]=%d", cam_id, raw[1]
                )
        except Exception as err:
            _LOGGER.debug("RCP privacy read error for %s: %s", cam_id, err)

        # Read camera clock (0x0a0f) — 8 bytes → compute offset vs server time
        try:
            import datetime as _dt
            raw = await self._rcp_read(rcp_base, "0x0a0f", session_id, type_="P_OCTET")
            if raw and len(raw) >= 8:
                # RCP clock format: year(2B big-endian) month(1B) day(1B) hour(1B) min(1B) sec(1B) weekday(1B)
                import struct as _struct2
                year, month, day, hour, minute, second, _ = _struct2.unpack(">HBBBBBB", raw[:8])
                cam_dt = _dt.datetime(year, month, day, hour, minute, second, tzinfo=_dt.timezone.utc)
                server_dt = _dt.datetime.now(_dt.timezone.utc)
                offset = (cam_dt - server_dt).total_seconds()
                self._rcp_clock_offset_cache[cam_id] = round(offset, 1)
                _LOGGER.debug("RCP clock offset for %s: %.1fs", cam_id, offset)
        except Exception as err:
            _LOGGER.debug("RCP clock read error for %s: %s", cam_id, err)

        # Read LAN IP via RCP (0x0a36) — 4 bytes IPv4 or ASCII string
        try:
            raw = await self._rcp_read(rcp_base, "0x0a36", session_id, type_="P_OCTET")
            if raw:
                if len(raw) == 4:
                    ip_str = ".".join(str(b) for b in raw)
                else:
                    ip_str = raw.rstrip(b"\x00").decode("ascii", errors="replace")
                self._rcp_lan_ip_cache[cam_id] = ip_str
                _LOGGER.debug("RCP LAN IP for %s: %s", cam_id, ip_str)
        except Exception as err:
            _LOGGER.debug("RCP LAN IP read error for %s: %s", cam_id, err)

        # Read product name via RCP (0x0aea) — null-terminated ASCII
        try:
            raw = await self._rcp_read(rcp_base, "0x0aea", session_id, type_="P_OCTET")
            if raw:
                name_str = raw.rstrip(b"\x00").decode("ascii", errors="replace")
                self._rcp_product_name_cache[cam_id] = name_str
                _LOGGER.debug("RCP product name for %s: %s", cam_id, name_str)
        except Exception as err:
            _LOGGER.debug("RCP product name read error for %s: %s", cam_id, err)

        # Read bitrate ladder (0x0c81) — series of big-endian uint32 kbps values
        try:
            import struct as _struct3
            raw = await self._rcp_read(rcp_base, "0x0c81", session_id, type_="P_OCTET")
            if raw and len(raw) >= 4:
                n = len(raw) // 4
                ladder = [_struct3.unpack(">I", raw[i*4:(i+1)*4])[0] for i in range(n)]
                self._rcp_bitrate_cache[cam_id] = ladder
                _LOGGER.debug("RCP bitrate ladder for %s: %s", cam_id, ladder)
        except Exception as err:
            _LOGGER.debug("RCP bitrate read error for %s: %s", cam_id, err)

    def clock_offset(self, cam_id: str) -> float | None:
        """Return clock offset in seconds (camera time − server time), or None."""
        return self._rcp_clock_offset_cache.get(cam_id)

    def rcp_lan_ip(self, cam_id: str) -> str | None:
        """Return camera LAN IP from RCP 0x0a36, or None."""
        return self._rcp_lan_ip_cache.get(cam_id)

    def rcp_product_name(self, cam_id: str) -> str | None:
        """Return camera product name from RCP 0x0aea, or None."""
        return self._rcp_product_name_cache.get(cam_id)

    def rcp_bitrate_ladder(self, cam_id: str) -> list[int]:
        """Return bitrate ladder (kbps) from RCP 0x0c81, or empty list."""
        return self._rcp_bitrate_cache.get(cam_id, [])

    def get_quality(self, cam_id: str) -> str:
        """Return current quality preference: 'auto', 'high', or 'low'."""
        return self._quality_preference.get(cam_id, "auto")

    def set_quality(self, cam_id: str, quality: str) -> None:
        """Set quality preference. quality must be 'auto', 'high', or 'low'."""
        self._quality_preference[cam_id] = quality

    def get_quality_params(self, cam_id: str) -> tuple[bool, int]:
        """Return (highQualityVideo: bool, inst: int) for current quality preference."""
        q = self.get_quality(cam_id)
        if q == "high":
            return True, 1    # primary encoder, max quality (~30 Mbps)
        elif q == "low":
            return False, 4   # low-bandwidth stream (~1.9 Mbps)
        else:  # "auto"
            return False, 2   # iOS default, balanced (~7.5 Mbps)

    def motion_settings(self, cam_id: str) -> dict:
        """Return motion detection settings dict, or empty dict."""
        return self.data.get(cam_id, {}).get("motion", {})

    def audio_alarm_settings(self, cam_id: str) -> dict:
        """Return audio alarm settings dict, or empty dict."""
        return self.data.get(cam_id, {}).get("audioAlarm", {})

    def recording_options(self, cam_id: str) -> dict:
        """Return recording options dict, or empty dict."""
        return self.data.get(cam_id, {}).get("recordingOptions", {})

    async def async_put_camera(self, cam_id: str, endpoint: str, payload: dict) -> bool:
        """PUT to /v11/video_inputs/{cam_id}/{endpoint} with payload. Returns True on success."""
        token = self.token
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.put(
                    f"{CLOUD_API}/v11/video_inputs/{cam_id}/{endpoint}",
                    headers=headers, json=payload,
                ) as resp:
                    return resp.status in (200, 204)
        except Exception as err:
            _LOGGER.warning("async_put_camera %s/%s error: %s", cam_id, endpoint, err)
            return False

    # ── SHC local API (camera light + privacy mode) ───────────────────────────
    async def _async_shc_request(
        self, method: str, path: str, body: dict | None = None
    ) -> dict | list | None:
        """Make a request to the SHC local API using mutual TLS.

        Returns parsed JSON on success, None on failure.
        Requires shc_ip, shc_cert_path, shc_key_path in options.
        """
        import ssl
        opts      = self.options
        shc_ip    = opts.get("shc_ip", "").strip()
        cert_path = opts.get("shc_cert_path", "").strip()
        key_path  = opts.get("shc_key_path", "").strip()
        if not shc_ip or not cert_path or not key_path:
            return None
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode    = ssl.CERT_NONE
            ctx.load_cert_chain(cert_path, key_path)
        except Exception as err:
            _LOGGER.warning("SHC TLS setup failed (check cert/key paths): %s", err)
            return None

        url     = f"https://{shc_ip}:8444/smarthome{path}"
        headers = {"api-version": "3.2", "Content-Type": "application/json"}
        try:
            connector = aiohttp.TCPConnector(ssl=ctx)
            async with aiohttp.ClientSession(connector=connector) as s:
                async with async_timeout.timeout(10):
                    if method == "GET":
                        async with s.get(url, headers=headers) as r:
                            if r.status == 200:
                                return await r.json()
                            _LOGGER.debug("SHC GET %s → HTTP %d", path, r.status)
                    elif method == "PUT":
                        async with s.put(url, json=body, headers=headers) as r:
                            _LOGGER.debug("SHC PUT %s → HTTP %d", path, r.status)
                            return {"status": r.status, "ok": r.status in (200, 201, 204)}
        except asyncio.TimeoutError:
            _LOGGER.debug("SHC request timeout: %s %s", method, path)
        except aiohttp.ClientError as err:
            _LOGGER.debug("SHC request error %s %s: %s", method, path, err)
        except Exception as err:
            _LOGGER.debug("SHC unexpected error %s %s: %s", method, path, err)
        return None

    async def _async_update_shc_states(self, data: dict) -> None:
        """Fetch CameraLight and PrivacyMode states from SHC for each camera.

        Matches SHC devices to cloud cameras by device name (title).
        Refreshes the SHC device list at most once per 60 seconds.
        """
        opts = self.options
        if not opts.get("shc_ip", "").strip():
            return

        # Re-fetch device list at most once per 60 s
        now = time.monotonic()
        if now - self._last_shc_fetch >= 60 or not self._shc_devices_raw:
            devices = await self._async_shc_request("GET", "/devices")
            if isinstance(devices, list):
                self._shc_devices_raw = devices
                self._last_shc_fetch  = now

        shc_devices = self._shc_devices_raw
        if not shc_devices:
            return

        for cam_id, cam in data.items():
            title = cam.get("info", {}).get("title", "").lower().strip()

            # Match SHC device by name (case-insensitive)
            device_id = None
            for dev in shc_devices:
                if dev.get("name", "").lower().strip() == title:
                    device_id = dev.get("id")
                    break
            if not device_id:
                _LOGGER.debug("SHC: no device found matching camera title '%s'", title)
                continue

            entry = self._shc_state_cache.setdefault(cam_id, {
                "device_id":    device_id,
                "camera_light": None,
                "privacy_mode": None,
            })
            entry["device_id"] = device_id

            # Fetch CameraLight service state
            svc = await self._async_shc_request(
                "GET", f"/devices/{device_id}/services/CameraLight"
            )
            if isinstance(svc, dict):
                val = svc.get("state", {}).get("value", "")
                entry["camera_light"] = (val.upper() == "ON")

            # Fetch PrivacyMode service state
            svc = await self._async_shc_request(
                "GET", f"/devices/{device_id}/services/PrivacyMode"
            )
            if isinstance(svc, dict):
                val = svc.get("state", {}).get("value", "")
                entry["privacy_mode"] = (val.upper() == "ENABLED")

    async def async_shc_set_camera_light(self, cam_id: str, on: bool) -> bool:
        """Turn the camera indicator LED on (True) or off (False) via SHC API."""
        device_id = self._shc_state_cache.get(cam_id, {}).get("device_id")
        if not device_id:
            _LOGGER.warning("SHC: no device_id cached for %s — cannot control light", cam_id)
            return False
        result = await self._async_shc_request(
            "PUT",
            f"/devices/{device_id}/services/CameraLight/state",
            {"@type": "cameraLightState", "value": "ON" if on else "OFF"},
        )
        if result and result.get("ok", result.get("status", 0) in (200, 201, 204)):
            self._shc_state_cache[cam_id]["camera_light"] = on
            await self.async_request_refresh()
            return True
        return False

    async def async_shc_set_privacy_mode(self, cam_id: str, enabled: bool) -> bool:
        """Enable (True) or disable (False) privacy mode via SHC API (legacy fallback)."""
        device_id = self._shc_state_cache.get(cam_id, {}).get("device_id")
        if not device_id:
            _LOGGER.warning("SHC: no device_id cached for %s — cannot set privacy mode", cam_id)
            return False
        result = await self._async_shc_request(
            "PUT",
            f"/devices/{device_id}/services/PrivacyMode/state",
            {"@type": "privacyModeState", "value": "ENABLED" if enabled else "DISABLED"},
        )
        if result and result.get("ok", result.get("status", 0) in (200, 201, 204)):
            self._shc_state_cache[cam_id]["privacy_mode"] = enabled
            await self.async_request_refresh()
            return True
        return False

    async def async_cloud_set_privacy_mode(self, cam_id: str, enabled: bool) -> bool:
        """Enable (True) or disable (False) privacy mode via Bosch cloud API.

        Uses PUT /v11/video_inputs/{id}/privacy — no SHC local API needed.
        Falls back to SHC API if cloud call fails and SHC is configured.

        Discovery: GET/PUT /v11/video_inputs/{id}/privacy
          Body: {"privacyMode": "ON"/"OFF", "durationInSeconds": null}
          Response: HTTP 204 on success.
        """
        token = self.token
        if not token:
            _LOGGER.warning("cloud_set_privacy_mode: no token for %s", cam_id)
            return False

        connector = aiohttp.TCPConnector(ssl=False)
        session   = aiohttp.ClientSession(connector=connector)
        headers   = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url  = f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy"
        body = {"privacyMode": "ON" if enabled else "OFF", "durationInSeconds": None}

        try:
            async with async_timeout.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    if resp.status in (200, 201, 204):
                        self._shc_state_cache.setdefault(cam_id, {})["privacy_mode"] = enabled
                        _LOGGER.debug(
                            "cloud_set_privacy_mode: %s → %s (HTTP %d)",
                            cam_id, "ON" if enabled else "OFF", resp.status,
                        )
                        await self.async_request_refresh()
                        return True
                    _LOGGER.warning(
                        "cloud_set_privacy_mode: HTTP %d for %s", resp.status, cam_id
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_privacy_mode error for %s: %s", cam_id, err)
        finally:
            await session.close()

        # Fallback to SHC if cloud API fails and SHC is configured
        if self.options.get("shc_ip", "").strip():
            _LOGGER.debug("cloud_set_privacy_mode: cloud failed, falling back to SHC for %s", cam_id)
            return await self.async_shc_set_privacy_mode(cam_id, enabled)
        return False

    async def async_cloud_set_camera_light(self, cam_id: str, on: bool) -> bool:
        """Turn the camera light on (True) or off (False) via Bosch cloud API.

        Uses PUT /v11/video_inputs/{id}/lighting_override — no SHC local API needed.
        Discovered 2026-03-21 via mitmproxy capture.
        ON:  {"frontLightOn": true, "wallwasherOn": true, "frontLightIntensity": 1.0}
        OFF: {"frontLightOn": false, "wallwasherOn": false}
        Response: HTTP 204 on success.
        """
        token = self.token
        if not token:
            _LOGGER.warning("cloud_set_camera_light: no token for %s", cam_id)
            return False

        connector = aiohttp.TCPConnector(ssl=False)
        session   = aiohttp.ClientSession(connector=connector)
        headers   = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url  = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_override"
        body = (
            {"frontLightOn": True, "wallwasherOn": True, "frontLightIntensity": 1.0}
            if on else
            {"frontLightOn": False, "wallwasherOn": False}
        )

        try:
            async with async_timeout.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    if resp.status in (200, 201, 204):
                        self._shc_state_cache.setdefault(cam_id, {})["camera_light"] = on
                        _LOGGER.debug(
                            "cloud_set_camera_light: %s → %s (HTTP %d)",
                            cam_id, "ON" if on else "OFF", resp.status,
                        )
                        await self.async_request_refresh()
                        return True
                    _LOGGER.warning(
                        "cloud_set_camera_light: HTTP %d for %s", resp.status, cam_id
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_camera_light error for %s: %s", cam_id, err)
        finally:
            await session.close()
        return False

    async def async_cloud_set_notifications(self, cam_id: str, enabled: bool) -> bool:
        """Enable (FOLLOW_CAMERA_SCHEDULE) or disable (ALWAYS_OFF) notifications via cloud API.

        Uses PUT /v11/video_inputs/{id}/enable_notifications.
        Discovered 2026-03-21 via mitmproxy capture.
        Response: HTTP 204 on success.
        """
        token = self.token
        if not token:
            _LOGGER.warning("cloud_set_notifications: no token for %s", cam_id)
            return False

        connector = aiohttp.TCPConnector(ssl=False)
        session   = aiohttp.ClientSession(connector=connector)
        headers   = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url    = f"{CLOUD_API}/v11/video_inputs/{cam_id}/enable_notifications"
        status = "FOLLOW_CAMERA_SCHEDULE" if enabled else "ALWAYS_OFF"
        body   = {"enabledNotificationsStatus": status}

        try:
            async with async_timeout.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    if resp.status in (200, 201, 204):
                        self._shc_state_cache.setdefault(cam_id, {})["notifications_status"] = status
                        _LOGGER.debug(
                            "cloud_set_notifications: %s → %s (HTTP %d)",
                            cam_id, status, resp.status,
                        )
                        await self.async_request_refresh()
                        return True
                    _LOGGER.warning(
                        "cloud_set_notifications: HTTP %d for %s", resp.status, cam_id
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_notifications error for %s: %s", cam_id, err)
        finally:
            await session.close()
        return False

    async def async_cloud_set_pan(self, cam_id: str, position: int) -> bool:
        """Pan the 360 camera to an absolute position (-120 to +120 degrees).

        Uses PUT /v11/video_inputs/{id}/pan — no SHC local API needed.
        Discovered 2026-03-21 via mitmproxy capture.
        Response: {"currentAbsolutePosition": N, "cameraStoppedAtLimit": false,
                   "estimatedTimeToCompletion": 970}
        """
        token = self.token
        if not token:
            _LOGGER.warning("cloud_set_pan: no token for %s", cam_id)
            return False

        connector = aiohttp.TCPConnector(ssl=False)
        session   = aiohttp.ClientSession(connector=connector)
        headers   = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/pan"

        try:
            async with async_timeout.timeout(10):
                async with session.put(url, json={"absolutePosition": position}, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        actual = data.get("currentAbsolutePosition", position)
                        self._pan_cache[cam_id] = actual
                        _LOGGER.debug(
                            "cloud_set_pan: %s → %d° (HTTP %d, ETA %dms)",
                            cam_id, actual, resp.status,
                            data.get("estimatedTimeToCompletion", 0),
                        )
                        await self.async_request_refresh()
                        return True
                    _LOGGER.warning("cloud_set_pan: HTTP %d for %s", resp.status, cam_id)
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_pan error for %s: %s", cam_id, err)
        finally:
            await session.close()
        return False

    # ── Auto-download (runs in executor thread) ───────────────────────────────
    def _sync_download(self, data: dict, token: str, download_path: str) -> None:
        """Download new event files to download_path/{camera_name}/."""
        import requests  # sync requests — only used in executor
        import urllib3
        urllib3.disable_warnings()

        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {token}"
        session.verify = False

        for cam_id, cam_data in data.items():
            cam_name = cam_data["info"].get("title", cam_id)
            folder   = os.path.join(download_path, cam_name)
            os.makedirs(folder, exist_ok=True)

            for ev in cam_data.get("events", []):
                self._download_one(session, ev, folder, "jpg", ev.get("imageUrl"))
                if ev.get("videoClipUploadStatus") == "Done":
                    self._download_one(session, ev, folder, "mp4", ev.get("videoClipUrl"))

    @staticmethod
    def _download_one(
        session, ev: dict, folder: str, ext: str, url: str | None
    ) -> None:
        if not url:
            return
        ts    = ev.get("timestamp", "")[:19].replace(":", "-").replace("T", "_")
        etype = ev.get("eventType", "EVENT")
        ev_id = ev.get("id", "")[:8]
        path  = os.path.join(folder, f"{ts}_{etype}_{ev_id}.{ext}")
        if os.path.exists(path):
            return
        try:
            r = session.get(url, timeout=60, stream=True)
            if r.status_code == 200:
                with open(path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                _LOGGER.debug("Downloaded: %s", os.path.basename(path))
        except Exception as err:
            _LOGGER.warning("Download failed for %s: %s", os.path.basename(path), err)


# ─────────────────────────────────────────────────────────────────────────────
async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    coordinator = BoschCameraCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, ALL_PLATFORMS)

    # Reload integration when options change (e.g. scan_interval updated)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Register services (idempotent)
    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, ALL_PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when options are changed."""
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register HA services (skip if already registered)."""

    async def handle_trigger_snapshot(call: ServiceCall) -> None:
        """Force an immediate refresh for all cameras (data + images)."""
        for edata in hass.data.get(DOMAIN, {}).values():
            if coord := edata.get("coordinator"):
                await coord.async_request_refresh()
                for cam_id, cam in coord._camera_entities.items():
                    hass.async_create_task(cam._async_trigger_image_refresh(delay=1))

    async def handle_open_live_connection(call: ServiceCall) -> None:
        """Try to open a live proxy connection for a specific camera."""
        cam_id = call.data.get("camera_id", "")
        for edata in hass.data.get(DOMAIN, {}).values():
            if coord := edata.get("coordinator"):
                result = await coord.try_live_connection(cam_id)
                if result:
                    _LOGGER.info("Live connection established: %s", result)

    if not hass.services.has_service(DOMAIN, "trigger_snapshot"):
        hass.services.async_register(DOMAIN, "trigger_snapshot", handle_trigger_snapshot)
    if not hass.services.has_service(DOMAIN, "open_live_connection"):
        hass.services.async_register(DOMAIN, "open_live_connection", handle_open_live_connection)
