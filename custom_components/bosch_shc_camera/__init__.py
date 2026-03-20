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

ALL_PLATFORMS = ["camera", "sensor", "button", "switch"]

# ConnectionType enum — confirmed working value: "REMOTE"
# REMOTE → cloud proxy, fast (~1.5s), no credentials, works from anywhere
# LOCAL  → LAN direct, returns Digest user/password, slow (~15s)
LIVE_TYPE_CANDIDATES = ["REMOTE", "LOCAL"]
LIVE_SESSION_TTL = 55  # seconds — proxy sessions last ~60s, expire 5s early to be safe

DEFAULT_OPTIONS = {
    "scan_interval":    60,    # coordinator tick interval (seconds)
    "interval_status":  300,   # ping camera status every 5 minutes
    "interval_events":  300,   # fetch new events every 5 minutes
    "enable_snapshots":       True,
    "enable_sensors":         True,
    "enable_snapshot_button": True,
    "enable_auto_download":   False,
    "download_path":          "",
    # SHC local API — for camera light + privacy mode control
    "shc_ip":        "",   # e.g. 192.168.20.4
    "shc_cert_path": "",   # path to client cert PEM (e.g. /config/claude_cert.pem)
    "shc_key_path":  "",   # path to client key PEM  (e.g. /config/claude_key.pem)
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
        # Cached data for types that are not re-fetched this tick
        self._cached_status: dict[str, str] = {}
        self._cached_events: dict[str, list] = {}
        # SHC local API state cache — keyed by cam_id
        # Each entry: {"device_id": str, "camera_light": bool|None, "privacy_mode": bool|None}
        self._shc_state_cache: dict[str, dict] = {}
        self._shc_devices_raw: list = []       # cached GET /smarthome/devices response
        self._last_shc_fetch: float = 0.0      # last time SHC devices were fetched

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

            # ── 4. SHC states (camera light + privacy mode) ───────────────────
            if opts.get("shc_ip", "").strip():
                try:
                    await self._async_update_shc_states(data)
                except Exception as err:
                    _LOGGER.debug("SHC state update error: %s", err)

            # ── 5. Auto-download new event files ──────────────────────────────
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
            for type_val in LIVE_TYPE_CANDIDATES:
                try:
                    async with async_timeout.timeout(10):
                        async with session.put(
                            url, json={"type": type_val}, headers=headers
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
                                        f"?inst=1{audio_param}&fmtp=1&maxSessionDuration=60"
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
                                        f"?inst=1{audio_param}&fmtp=1&maxSessionDuration=60"
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
                    url, json={"type": "REMOTE"}, headers=headers
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
                    if snap_resp.status == 200 and "image" in ct:
                        data = await snap_resp.read()
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
                    url, json={"type": "LOCAL"}, headers=headers
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
        """Enable (True) or disable (False) privacy mode via SHC API."""
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
