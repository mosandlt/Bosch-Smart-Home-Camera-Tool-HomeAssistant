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
from datetime import timedelta

import aiohttp
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

DOMAIN     = "bosch_shc_camera"
CLOUD_API  = "https://residential.cbs.boschsecurity.com"

ALL_PLATFORMS = ["camera", "sensor", "button"]

# ConnectionType enum — confirmed working value: "REMOTE"
# REMOTE → cloud proxy, fast (~1.5s), no credentials, works from anywhere
# LOCAL  → LAN direct, returns Digest user/password, slow (~15s)
LIVE_TYPE_CANDIDATES = ["REMOTE", "LOCAL"]

DEFAULT_OPTIONS = {
    "scan_interval":          30,
    "enable_snapshots":       True,
    "enable_sensors":         True,
    "enable_snapshot_button": True,
    "enable_auto_download":   False,
    "download_path":          "",
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
            update_interval=timedelta(seconds=int(opts.get("scan_interval", 30))),
        )
        # Persists live-stream proxy info between coordinator refreshes
        self._live_connections: dict[str, dict] = {}

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
        Returns dict keyed by cam_id:
          {
            "info":   {...},        # from GET /v11/video_inputs
            "status": "ONLINE",     # from GET /v11/video_inputs/{id}/ping
            "events": [...],        # from GET /v11/events?videoInputId={id}&limit=20
            "live":   {...},        # cached proxy info from PUT /connection
          }
        """
        token = self.token
        if not token and not self.refresh_token:
            raise UpdateFailed("Not authenticated — re-add the integration to log in")

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            # ── 1. List cameras ───────────────────────────────────────────────
            async with async_timeout.timeout(15):
                async with session.get(
                    f"{CLOUD_API}/v11/video_inputs", headers=headers
                ) as resp:
                    if resp.status == 401:
                        # Token expired — try silent renewal
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

                # ── 2. Ping status ────────────────────────────────────────────
                status = "UNKNOWN"
                try:
                    async with async_timeout.timeout(8):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/ping",
                            headers=headers,
                        ) as r:
                            if r.status == 200:
                                status = (await r.text()).strip().strip('"')
                except Exception:
                    pass

                # ── 3. Latest events ──────────────────────────────────────────
                events: list = []
                try:
                    url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=20"
                    async with async_timeout.timeout(15):
                        async with session.get(url, headers=headers) as r:
                            if r.status == 200:
                                events = await r.json()
                except Exception as err:
                    _LOGGER.debug("Events fetch error for %s: %s", cam_id, err)

                data[cam_id] = {
                    "info":   cam,
                    "status": status,
                    "events": events,
                    "live":   self._live_connections.get(cam_id, {}),
                }

            # ── 4. Auto-download new event files ──────────────────────────────
            opts = self.options
            if opts.get("enable_auto_download") and opts.get("download_path"):
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
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        for type_val in LIVE_TYPE_CANDIDATES:
            try:
                async with async_timeout.timeout(10):
                    async with session.put(
                        url, json={"type": type_val}, headers=headers
                    ) as resp:
                        if resp.status in (200, 201):
                            result = await resp.json()
                            _LOGGER.info(
                                "Live connection opened! type=%s → %s", type_val, result
                            )
                            # Build URLs from the 'urls' array in the response
                            # urls[0] = "proxy-NN.live.cbs.boschsecurity.com:42090/{hash}"
                            urls = result.get("urls", [])
                            if urls:
                                proxy_host_path = urls[0]  # e.g. "proxy-20.live.cbs.boschsecurity.com:42090/abc123"
                                # snap.jpg on port 42090 — no auth needed
                                result["proxyUrl"] = f"https://{proxy_host_path}/snap.jpg"
                                # rtsps:// on port 443 — full 30fps H.264 + AAC audio
                                # Replace :42090 with :443 and use rtsps:// scheme
                                rtsps_host_path = proxy_host_path.replace(":42090", ":443")
                                result["rtspsUrl"] = (
                                    f"rtsps://{rtsps_host_path}/rtsp_tunnel"
                                    "?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60"
                                )
                                # Keep rtspUrl for backwards compatibility (points to rtsps)
                                result["rtspUrl"] = result["rtspsUrl"]
                            elif result.get("hash"):
                                # Fallback: reconstruct from hash field
                                h  = result["hash"]
                                ph = result.get("proxyHost", "proxy-01.live.cbs.boschsecurity.com")
                                pp = result.get("proxyPort", 42090)
                                result["proxyUrl"] = f"https://{ph}:{pp}/{h}/snap.jpg"
                                result["rtspsUrl"] = (
                                    f"rtsps://{ph}:443/{h}/rtsp_tunnel"
                                    "?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60"
                                )
                                result["rtspUrl"] = result["rtspsUrl"]
                            self._live_connections[cam_id] = result
                            await self.async_request_refresh()
                            return result
                        elif resp.status == 401:
                            _LOGGER.warning("Token expired during live connection attempt")
                            return None
            except (asyncio.TimeoutError, aiohttp.ClientError):
                pass

        _LOGGER.warning("Could not open live connection for %s", cam_id)
        return None

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
        """Force an immediate refresh for all cameras."""
        for edata in hass.data.get(DOMAIN, {}).values():
            if coord := edata.get("coordinator"):
                await coord.async_request_refresh()

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
