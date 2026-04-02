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

from .fcm import (
    fetch_firebase_config as _fcm_fetch_firebase_config,
    async_start_fcm_push as _fcm_async_start_fcm_push,
    register_fcm_with_bosch as _fcm_register_fcm_with_bosch,
    async_stop_fcm_push as _fcm_async_stop_fcm_push,
    async_handle_fcm_push as _fcm_async_handle_fcm_push,
    async_send_alert as _fcm_async_send_alert,
    async_mark_events_read as _fcm_async_mark_events_read,
    get_alert_services as _fcm_get_alert_services,
    build_notify_data as _fcm_build_notify_data,
    _write_file as _fcm_write_file,
)
from .smb import (
    sync_download,
    sync_smb_upload,
    sync_smb_cleanup,
    sync_smb_disk_check,
    async_smb_disk_alert,
)
from .tls_proxy import pre_warm_rtsp, start_tls_proxy, stop_tls_proxy
from . import shc as shc_mod
from .rcp import async_update_rcp_data, get_cached_rcp_session

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

DOMAIN     = "bosch_shc_camera"
CLOUD_API  = "https://residential.cbs.boschsecurity.com"

ALL_PLATFORMS = ["binary_sensor", "camera", "sensor", "button", "switch", "number", "select", "update"]

# ConnectionType enum — confirmed working value: "REMOTE"
LIVE_TYPE_CANDIDATES = ["REMOTE", "LOCAL"]
LIVE_SESSION_TTL = 55  # seconds — proxy sessions last ~60s, expire 5s early


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
    "shc_ip":        "",
    "shc_cert_path": "",
    "shc_key_path":  "",
    "high_quality_video": False,
    "stream_connection_type": "auto",   # "remote", "local", or "auto" (local first, fallback remote)
    "enable_binary_sensors": True,
    "enable_fcm_push": False,  # FCM push notifications for near-instant event detection (opt-in)
    "alert_notify_service": "",   # notify service for alerts (e.g. "notify.signal_messenger"), empty = disabled
    # Per-type notification routing (empty = falls back to alert_notify_service for backward compat)
    "alert_notify_system": "",      # System alerts (token failure, disk warning) — empty = uses alert_notify_service
    "alert_notify_information": "", # Step 1: text event notification — empty = uses alert_notify_service
    "alert_notify_screenshot": "",  # Step 2: snapshot image — empty = uses alert_notify_service
    "alert_notify_video": "",       # Step 3: video clip — empty = uses alert_notify_service
    "alert_save_snapshots": False, # save event snapshots locally to /config/www/bosch_alerts/
    "alert_delete_after_send": True, # delete local snapshot after sending (only when alert_save_snapshots=False)
    "fcm_push_mode": "auto",  # "auto" (ios→android→polling fallback), "android", "ios", "polling"
    "audio_default_on": True,  # Audio default ON when starting a live stream
    "enable_intercom": False,  # Two-way audio (intercom) switch — disabled by default
    "enable_smb_upload": False,  # Upload events to SMB/NAS share (opt-in)
    "smb_server": "",            # SMB server IP/hostname (e.g. 192.168.1.1 for FRITZ!Box)
    "smb_share": "",             # Share name (e.g. "FRITZ.NAS")
    "smb_username": "",          # SMB username
    "smb_password": "",          # SMB password
    "smb_base_path": "Bosch-Kameras",  # Base folder on the share
    "smb_folder_pattern": "{year}/{month}",  # Subfolder pattern (default: YYYY/MM)
    "smb_file_pattern": "{camera}_{date}_{time}_{type}_{id}",  # File name pattern
    "smb_retention_days": 180,  # Delete files older than N days (0 = keep forever)
    "smb_disk_warn_mb": 5120,   # Alert when free space on SMB share falls below N MB (0 = disable)
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
        # In-memory stream type override — changed by BoschStreamModeSwitch without reload.
        # None = use options setting; "local" / "auto" / "remote" = override.
        self._stream_type_override: str | None = None
        # Per-camera audio setting — True = audio+video on (default), False = snapshot-only
        self._audio_enabled:    dict[str, bool]  = {}
        # Camera entity references — registered on entity setup, used by button/service
        self._camera_entities: dict = {}
        # Per-type last-fetched timestamps (-inf = never → always fetch on first tick)
        self._last_status: float = -86400.0  # force status check on first tick
        self._last_events: float = -86400.0  # force event check on first tick
        self._last_slow:   float = -86400.0  # force slow check on first tick
        # Cached data for types that are not re-fetched this tick
        self._cached_status: dict[str, str] = {}
        self._cached_events: dict[str, list] = {}
        # SHC local API state cache — keyed by cam_id
        # Each entry: {"device_id": str, "camera_light": bool|None, "privacy_mode": bool|None}
        self._shc_state_cache: dict[str, dict] = {}
        self._shc_devices_raw: list = []       # cached GET /smarthome/devices response
        self._last_shc_fetch: float = 0.0      # last time SHC devices were fetched
        # SHC health tracking — skip SHC calls when offline to avoid latency
        self._shc_available: bool = True        # assume available until proven otherwise
        self._shc_fail_count: int = 0           # consecutive failures
        self._shc_last_check: float = 0.0       # last time we probed SHC after it went offline
        _SHC_MAX_FAILS = 3                      # mark offline after this many consecutive failures
        _SHC_RETRY_INTERVAL = 120               # seconds — retry SHC after this long when offline
        self._SHC_MAX_FAILS = _SHC_MAX_FAILS
        self._SHC_RETRY_INTERVAL = _SHC_RETRY_INTERVAL
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
        # Phase 2 RCP caches
        self._rcp_alarm_catalog_cache: dict[str, list[dict]] = {}  # alarm types from 0x0c38
        self._rcp_motion_zones_cache: dict[str, list[dict]] = {}   # motion zones from 0x0c00
        self._rcp_motion_coords_cache: dict[str, list[dict]] = {}  # zone coords from 0x0c0a
        self._rcp_tls_cert_cache: dict[str, dict] = {}             # TLS cert info from 0x0b91
        self._rcp_network_services_cache: dict[str, list[str]] = {} # network services from 0x0c62
        self._rcp_iva_catalog_cache: dict[str, list[dict]] = {}    # IVA analytics from 0x0b60
        # Video quality preference — keyed by cam_id, runtime only (not persisted)
        # Values: "auto" | "high" | "low"
        self._quality_preference: dict[str, str] = {}
        # RCP session ID cache — keyed by proxy_hash, value (session_id, expires_monotonic)
        # Avoids 2 round-trip RCP handshake on every thumbnail/data fetch
        self._rcp_session_cache: dict[str, tuple[str, float]] = {}
        # Proxy URL cache — keyed by cam_id, value (urls[0], expires_monotonic)
        # Proxy leases last ~60s; cache for 50s to skip PUT /connection on warm refreshes
        self._proxy_url_cache: dict[str, tuple[str, float]] = {}
        # Last-seen event IDs per camera — used to detect new events for snapshot refresh
        self._last_event_ids: dict[str, str] = {}
        # FCM push client — near-instant event detection via Firebase Cloud Messaging
        self._fcm_client = None        # FcmPushClient instance (or None if disabled)
        self._fcm_token: str = ""      # FCM registration token
        self._fcm_running: bool = False
        self._fcm_last_push: float = 0.0  # monotonic time of last received push
        self._fcm_healthy: bool = False   # True when FCM is connected and receiving
        self._fcm_push_mode: str = "unknown"  # active FCM mode: "android", "ios", "auto", or "unknown"
        # Unread events count cache — keyed by cam_id, populated from GET /unread_events_count
        self._unread_events_cache: dict[str, int] = {}
        # Privacy sound override cache — keyed by cam_id, populated from GET /privacy_sound_override
        self._privacy_sound_cache: dict[str, bool | None] = {}
        # Commissioned status cache — keyed by cam_id, populated from GET /commissioned
        self._commissioned_cache: dict[str, dict] = {}
        # Feature flags — populated once from GET /v11/feature_flags
        self._feature_flags: dict[str, bool] = {}
        # Firmware update status cache — keyed by cam_id, from GET /firmware
        self._firmware_cache: dict[str, dict] = {}
        # SMB maintenance — last run timestamps (monotonic)
        self._last_smb_cleanup: float = 0.0     # last daily cleanup run
        self._last_smb_disk_check: float = 0.0  # last disk-free check
        # Token refresh failure tracking — alert once, not every 80s
        self._token_alert_sent: bool = False     # True after first alert sent
        self._token_fail_count: int = 0          # consecutive refresh failures
        # Timestamp overlay cache — keyed by cam_id, from GET /timestamp
        self._timestamp_cache: dict[str, bool | None] = {}
        # Notification type toggles cache — keyed by cam_id, from GET /notifications
        self._notifications_cache: dict[str, dict] = {}
        # Rules cache — keyed by cam_id, from GET /rules
        self._rules_cache: dict[str, list] = {}
        # Write-lock timestamps — prevent coordinator from overwriting optimistic state
        # with stale cloud data in the seconds after a successful API write.
        # Keyed by cam_id, value is monotonic time of last successful write.
        self._light_set_at:   dict[str, float] = {}      # lighting_override write timestamp
        self._notif_set_at:   dict[str, float] = {}      # enable_notifications write timestamp
        self._privacy_set_at: dict[str, float] = {}      # privacy write timestamp
        _WRITE_LOCK_SECS = 8.0                           # seconds to hold write lock
        self._WRITE_LOCK_SECS = _WRITE_LOCK_SECS
        # TLS proxy for LOCAL RTSPS streams — keyed by cam_id
        # FFmpeg can't handle RTSPS + Digest auth with self-signed certs.
        # The proxy accepts plain TCP and forwards to camera over TLS.
        self._tls_proxy_ports: dict[str, int] = {}  # cam_id → local port
        # Pre-create SSL context for TLS proxy (blocking call — must not run in event loop)
        import ssl
        # SSL context created lazily on first use (ssl.create_default_context
        # is blocking I/O — must not run in the event loop)
        self._tls_ssl_ctx = None
        # Offline tracking — per camera, monotonic timestamp when first detected offline.
        # Used to extend status check intervals for persistently offline cameras.
        self._offline_since: dict[str, float] = {}
        # Extended offline interval: cameras offline for >15 min are checked every 15 min
        # instead of the normal interval_status (5 min), reducing unnecessary cloud calls.
        _OFFLINE_EXTENDED_INTERVAL = 900  # 15 minutes
        self._OFFLINE_EXTENDED_INTERVAL = _OFFLINE_EXTENDED_INTERVAL
        # Per-camera status check timestamps (for extended offline intervals)
        self._per_cam_status_at: dict[str, float] = {}

    # ── Properties ────────────────────────────────────────────────────────────
    @property
    def token(self) -> str:
        # Prefer in-memory refreshed token over config entry (avoids stale reads)
        return getattr(self, "_refreshed_token", None) or self._entry.data.get("bearer_token", "")

    @property
    def refresh_token(self) -> str:
        return getattr(self, "_refreshed_refresh", None) or self._entry.data.get("refresh_token", "")

    @property
    def options(self) -> dict:
        return get_options(self._entry)

    # ── Token renewal ─────────────────────────────────────────────────────────
    async def _ensure_valid_token(self) -> str:
        """
        Return a valid bearer token.
        Called ONLY when we get a 401 — not on every tick.
        Refreshes via refresh_token with retry logic:
          - 3 attempts with 2s delay between retries
          - Persists new refresh token to config entry data (non-reloading)
          - Only alerts after 3 consecutive complete failures
        """
        from .config_flow import _do_refresh
        refresh = getattr(self, "_refreshed_refresh", None) or self.refresh_token
        if not refresh:
            await self._async_token_failure_alert(
                "Kein Refresh-Token vorhanden — bitte unter Einstellungen → Integrationen → "
                "Bosch Smart Home Camera → Konfigurieren → Erneut anmelden."
            )
            raise UpdateFailed("No refresh token — go to Settings → Integrations → Configure → Force new login")
        session = async_get_clientsession(self.hass, verify_ssl=False)
        # Retry up to 3 times with 2s delay
        tokens = None
        for attempt in range(3):
            tokens = await _do_refresh(session, refresh)
            if tokens:
                break
            if attempt < 2:
                _LOGGER.debug("Token refresh attempt %d failed, retrying in 2s...", attempt + 1)
                await asyncio.sleep(2)
        if tokens:
            self._refreshed_token = tokens.get("access_token", "")
            new_refresh = tokens.get("refresh_token", refresh)
            self._refreshed_refresh = new_refresh
            _LOGGER.info("Bearer token renewed silently via refresh_token")
            # Always persist both tokens to config entry so they survive reloads/restarts.
            # Previously only saved when refresh_token changed — but Keycloak offline_access
            # keeps the same refresh_token, so the new bearer_token was never persisted.
            new_data = dict(self._entry.data)
            needs_update = False
            if new_refresh != self._entry.data.get("refresh_token", ""):
                new_data["refresh_token"] = new_refresh
                needs_update = True
            if self._refreshed_token != self._entry.data.get("bearer_token", ""):
                new_data["bearer_token"] = self._refreshed_token
                needs_update = True
            if needs_update:
                self.hass.config_entries.async_update_entry(self._entry, data=new_data)
                _LOGGER.debug("Persisted refreshed tokens to config entry")
            # Schedule next proactive refresh before this token expires
            self._schedule_token_refresh()
            # Reset failure tracking on success
            if self._token_fail_count > 0:
                _LOGGER.info("Token refresh recovered after %d failures", self._token_fail_count)
            self._token_fail_count = 0
            self._token_alert_sent = False
            return self._refreshed_token
        self._token_fail_count += 1
        _LOGGER.warning("Silent token renewal failed (attempt %d)", self._token_fail_count)
        # Only alert after 3 consecutive complete failures
        if self._token_fail_count >= 3:
            await self._async_token_failure_alert(
                "Token-Erneuerung fehlgeschlagen — bitte unter Einstellungen → Integrationen → "
                "Bosch Smart Home Camera → Konfigurieren → Erneut anmelden."
            )
        raise UpdateFailed("Token refresh failed — check network or re-login")

    async def _async_token_failure_alert(self, message: str) -> None:
        """Send a one-time alert when token refresh fails (notify + persistent notification)."""
        if self._token_alert_sent:
            return
        self._token_alert_sent = True
        title = "⚠️ Bosch Kamera — Token abgelaufen"
        # HA persistent notification (always — visible in sidebar)
        try:
            await self.hass.services.async_call(
                "persistent_notification", "create",
                {"title": title, "message": message, "notification_id": "bosch_token_expired"},
            )
        except Exception as err:
            _LOGGER.debug("Persistent notification failed: %s", err)
        # Notify service (Signal, mobile_app, etc.) — uses system services
        for svc in self._get_alert_services("system"):
            domain, _, name = svc.partition(".")
            if self.hass.services.has_service(domain, name):
                try:
                    await self.hass.services.async_call(
                        domain, name, {"message": message, "title": title},
                    )
                    _LOGGER.info("Token failure alert sent via %s", svc)
                except Exception as err:
                    _LOGGER.debug("Token failure alert via %s failed: %s", svc, err)

    # ── Proactive background token refresh ───────────────────────────────────

    def _schedule_token_refresh(self) -> None:
        """Schedule a proactive token refresh 5 minutes before the JWT expires.

        Called after every successful token acquisition (startup + renewals).
        Ensures the token is always valid when automations or action methods run,
        eliminating the ~60s race window between token expiry and the next
        coordinator tick that previously triggered reactive 401 handling.
        """
        import base64 as _b64
        import json as _json
        import time as _time
        token = self.token
        if not token:
            return
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return
            # JWT payload is URL-safe base64 (no padding)
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = _json.loads(_b64.urlsafe_b64decode(payload_b64))
            exp = payload.get("exp", 0)
            remaining = exp - _time.time()
            # Refresh 5 minutes before expiry; at minimum 10s to avoid tight loops
            refresh_in = max(remaining - 300, 10)
            _LOGGER.debug(
                "Token expires in %.0fs — proactive refresh scheduled in %.0fs",
                remaining, refresh_in,
            )
            self.hass.loop.call_later(
                refresh_in,
                lambda: self.hass.async_create_task(self._proactive_refresh()),
            )
        except Exception as err:
            _LOGGER.debug("_schedule_token_refresh: cannot parse token expiry: %s", err)

    async def _proactive_refresh(self) -> None:
        """Background task: refresh the token before it expires."""
        _LOGGER.debug("Proactive token refresh triggered")
        try:
            await self._ensure_valid_token()
            # _ensure_valid_token calls _schedule_token_refresh on success,
            # so the next refresh is automatically rescheduled.
        except Exception as err:
            _LOGGER.warning(
                "Proactive token refresh failed: %s — will retry via reactive 401 handling",
                err,
            )

    # ── Local health check ────────────────────────────────────────────────────
    async def _async_local_tcp_ping(self, cam_id: str, timeout: float = 1.5) -> bool:
        """Quick TCP connect to camera port 443 on LAN — returns True if reachable.

        Uses cached LAN IP from RCP (0x0a36) or known network config.
        Much faster than cloud /commissioned check (~5ms vs ~200ms).
        """
        cam_ip = self._rcp_lan_ip_cache.get(cam_id)
        if not cam_ip:
            return False  # no known LAN IP — can't ping locally
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(cam_ip, 443),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    def _should_check_status(self, cam_id: str, now: float, interval_status: int) -> bool:
        """Determine if this camera needs a status check this tick.

        - Normal cameras: check every interval_status seconds.
        - Persistently offline cameras (>15 min): check every _OFFLINE_EXTENDED_INTERVAL.
        """
        last = self._last_status
        offline_since = self._offline_since.get(cam_id)
        if offline_since and (now - offline_since) > self._OFFLINE_EXTENDED_INTERVAL:
            # Camera has been offline for a while — use extended interval
            per_cam_last = self._per_cam_status_at.get(cam_id, -86400.0)
            return (now - per_cam_last) >= self._OFFLINE_EXTENDED_INTERVAL
        return (now - last) >= interval_status

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
        # Event polling interval: when FCM push is healthy, extend to interval_events (5 min)
        # as a safety net. When FCM is down/disabled, poll at scan_interval (60s) for faster detection.
        if self._fcm_healthy:
            event_interval = int(opts.get("interval_events", 300))
        else:
            event_interval = int(opts.get("interval_events", 60))
        do_events = (now - self._last_events) >= event_interval
        # Slow tier — wifiinfo, ambient light, RCP, motion, audio alarm, recording (every 5 min)
        do_slow   = (now - self._last_slow)   >= int(opts.get("interval_slow", 300))

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            # ── 1. List cameras (every tick — lightweight, needed for entity list) ──
            async with asyncio.timeout(15):
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
                async with asyncio.timeout(15):
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

            # ── Feature flags (fetch once — rarely changes) ────────────────
            if not self._feature_flags:
                try:
                    async with asyncio.timeout(5):
                        async with session.get(
                            f"{CLOUD_API}/v11/feature_flags", headers=headers
                        ) as ff_resp:
                            if ff_resp.status == 200:
                                self._feature_flags = await ff_resp.json()
                                _LOGGER.debug("Feature flags: %s", self._feature_flags)
                except Exception:
                    pass

            data: dict = {}

            # ── Build camera ID list ─────────────────────────────────────────
            cam_ids = []
            cam_by_id: dict[str, dict] = {}
            for cam in cam_list:
                cid = cam.get("id", "")
                if cid:
                    cam_ids.append(cid)
                    cam_by_id[cid] = cam

            # ── 2. Status — parallel across all cameras ──────────────────────
            # Local TCP ping + cloud /commissioned run in parallel for all cameras.
            # Local ping (~5ms) can skip the cloud call (~200ms) when camera is reachable.
            interval_status = int(opts.get("interval_status", 60))

            async def _check_status(cam_id: str) -> tuple[str, str]:
                """Check single camera status. Returns (cam_id, status)."""
                if not self._should_check_status(cam_id, now, interval_status):
                    return (cam_id, self._cached_status.get(cam_id, "UNKNOWN"))

                # Fast path: local TCP ping — if camera is reachable on LAN,
                # it's definitely ONLINE (skip cloud /commissioned call).
                if await self._async_local_tcp_ping(cam_id):
                    self._per_cam_status_at[cam_id] = now
                    self._offline_since.pop(cam_id, None)  # clear offline tracking
                    _LOGGER.debug("Local TCP ping OK for %s — ONLINE (cloud check skipped)", cam_id[:8])
                    return (cam_id, "ONLINE")

                # Cloud path: /commissioned (primary) + /ping (fallback)
                status = "UNKNOWN"
                comm_ok = False
                try:
                    async with asyncio.timeout(8):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/commissioned",
                            headers=headers,
                        ) as r:
                            if r.status == 200:
                                comm = await r.json()
                                self._commissioned_cache[cam_id] = comm
                                comm_ok = True
                                if comm.get("connected") and comm.get("commissioned"):
                                    status = "ONLINE"
                                elif comm.get("configured"):
                                    status = "OFFLINE"
                            elif r.status == 444:
                                status = "OFFLINE"
                                comm_ok = True
                except Exception as err:
                    _LOGGER.debug("Commissioned check error for %s: %s", cam_id, err)
                if not comm_ok:
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id}/ping",
                                headers=headers,
                            ) as pr:
                                if pr.status == 200:
                                    status = (await pr.text()).strip().strip('"')
                    except Exception as err:
                        _LOGGER.debug("Ping fallback error for %s: %s", cam_id, err)

                self._per_cam_status_at[cam_id] = now
                # Track offline duration for extended interval
                if status == "OFFLINE":
                    if cam_id not in self._offline_since:
                        self._offline_since[cam_id] = now
                else:
                    self._offline_since.pop(cam_id, None)
                return (cam_id, status)

            # Run all status checks in parallel
            status_results = await asyncio.gather(
                *[_check_status(cid) for cid in cam_ids],
                return_exceptions=True,
            )
            any_status_checked = False
            for result in status_results:
                if isinstance(result, Exception):
                    continue
                cid, status = result
                self._cached_status[cid] = status
                if self._should_check_status(cid, now, interval_status):
                    any_status_checked = True

            # ── 3. Events — parallel across all cameras ──────────────────────
            async def _fetch_events(cam_id: str) -> tuple[str, list]:
                """Fetch events for single camera. Returns (cam_id, events)."""
                events: list = []
                skip_full_fetch = False
                try:
                    async with asyncio.timeout(5):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/last_event",
                            headers=headers,
                        ) as le_resp:
                            if le_resp.status == 200:
                                last_ev = await le_resp.json()
                                last_ev_id = last_ev.get("id", "")
                                if last_ev_id and last_ev_id == self._last_event_ids.get(cam_id):
                                    skip_full_fetch = True
                                    events = self._cached_events.get(cam_id, [])
                                    _LOGGER.debug(
                                        "last_event unchanged for %s (id=%s) — skipping full fetch",
                                        cam_id, last_ev_id[:8],
                                    )
                except Exception as err:
                    _LOGGER.debug("last_event check error for %s: %s — falling back to full fetch", cam_id, err)

                if not skip_full_fetch:
                    try:
                        url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=20"
                        async with asyncio.timeout(15):
                            async with session.get(url, headers=headers) as r:
                                if r.status == 200:
                                    events = await r.json()
                    except Exception as err:
                        _LOGGER.debug("Events fetch error for %s: %s", cam_id, err)
                return (cam_id, events)

            if do_events:
                # Run all event fetches in parallel
                event_results = await asyncio.gather(
                    *[_fetch_events(cid) for cid in cam_ids],
                    return_exceptions=True,
                )
                for result in event_results:
                    if isinstance(result, Exception):
                        continue
                    cid, events = result
                    self._cached_events[cid] = events

            # ── Build data dict + process new events (must be sequential) ─────
            for cam_id in cam_ids:
                cam = cam_by_id[cam_id]
                status = self._cached_status.get(cam_id, "UNKNOWN")
                events = self._cached_events.get(cam_id, [])

                if do_events and events:
                    newest_id = events[0].get("id", "")
                    prev_id   = self._last_event_ids.get(cam_id)
                    if prev_id is None:
                        unread_ids = [
                            ev.get("id") for ev in events
                            if ev.get("id") and not ev.get("isRead", False)
                        ]
                        if unread_ids:
                            _LOGGER.debug(
                                "Startup: marking %d unread event(s) as read for %s",
                                len(unread_ids), cam_id,
                            )
                            try:
                                await self.async_mark_events_read(unread_ids)
                            except Exception:
                                pass
                    elif newest_id and newest_id != prev_id:
                        self._last_event_ids[cam_id] = newest_id
                        _LOGGER.debug(
                            "New event detected for %s (id=%s) — triggering snapshot refresh",
                            cam_id, newest_id,
                        )
                        cam_entity = self._camera_entities.get(cam_id)
                        if cam_entity:
                            self.hass.async_create_task(
                                cam_entity._async_trigger_image_refresh(delay=2)
                            )
                        newest_event  = events[0]
                        event_type    = newest_event.get("eventType", "")
                        cam_name      = cam.get("title", cam_id)
                        event_payload = {
                            "camera_id":   cam_id,
                            "camera_name": cam_name,
                            "timestamp":   newest_event.get("timestamp", ""),
                            "image_url":   newest_event.get("imageUrl", ""),
                            "event_id":    newest_id,
                        }
                        if event_type == "MOVEMENT":
                            self.hass.bus.async_fire(
                                "bosch_shc_camera_motion", event_payload
                            )
                        elif event_type == "AUDIO_ALARM":
                            self.hass.bus.async_fire(
                                "bosch_shc_camera_audio_alarm", event_payload
                            )
                        elif event_type == "PERSON":
                            self.hass.bus.async_fire(
                                "bosch_shc_camera_person", event_payload
                            )
                        self.hass.async_create_task(
                            self._async_send_alert(
                                cam_name, event_type,
                                newest_event.get("timestamp", ""),
                                newest_event.get("imageUrl", ""),
                                newest_event.get("videoClipUrl", ""),
                                newest_event.get("videoClipUploadStatus", ""),
                            )
                        )
                        try:
                            await self.async_mark_events_read([newest_id])
                        except Exception:
                            pass
                    elif newest_id:
                        self._last_event_ids[cam_id] = newest_id

                data[cam_id] = {
                    "info":   cam,
                    "status": status,
                    "events": events,
                    "live":   self._live_connections.get(cam_id, {}),
                }

            # Update timestamps only after successful fetches
            if any_status_checked:
                self._last_status = now
            if do_events:
                self._last_events = now
            if do_slow:
                self._last_slow = now

            # ── 4. Read privacy mode + light from cloud API response (primary) ──
            # Cloud API is ~10x faster than SHC local API (113ms vs 1122ms).
            # privacyMode and featureSupport are already in /v11/video_inputs —
            # no extra request needed. SHC (step 5) supplements as fallback.
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
                # Cloud is authoritative for privacy (fast, always available).
                # Skip overwrite if a write happened within _WRITE_LOCK_SECS — same
                # propagation-delay race as camera light.
                privacy_locked = (
                    cam_id_key in self._privacy_set_at
                    and (time.monotonic() - self._privacy_set_at[cam_id_key]) < self._WRITE_LOCK_SECS
                )
                if privacy_str and not privacy_locked:
                    cache["privacy_mode"] = (privacy_str.upper() == "ON")
                cache["has_light"] = has_light
                # Use cloud featureStatus for light state; SHC supplements if available.
                # Skip overwrite if a write happened within _WRITE_LOCK_SECS — the cloud
                # API returns stale data briefly after a PUT /lighting_override, which
                # would flip the switch back to OFF right after the user turned it ON.
                light_locked = (
                    cam_id_key in self._light_set_at
                    and (time.monotonic() - self._light_set_at[cam_id_key]) < self._WRITE_LOCK_SECS
                )
                if light_on is not None and not light_locked:
                    cache["camera_light"] = light_on
                elif cache.get("camera_light") is None:
                    cache["camera_light"] = None
                # Read notifications status from cloud API response.
                # Skip overwrite if written recently (same propagation-delay race as light).
                notif_status = cam_raw.get("notificationsEnabledStatus", "")
                notif_locked = (
                    cam_id_key in self._notif_set_at
                    and (time.monotonic() - self._notif_set_at[cam_id_key]) < self._WRITE_LOCK_SECS
                )
                if notif_status and not notif_locked:
                    cache["notifications_status"] = notif_status

                # Camera online check — skip expensive API calls for offline cameras
                cam_status = data[cam_id_key].get("status", "UNKNOWN")
                is_online = cam_status == "ONLINE"

                # Fetch pan position for cameras that support it (skip if offline)
                pan_limit = cam_raw.get("featureSupport", {}).get("panLimit", 0)
                if pan_limit and is_online:
                    try:
                        async with asyncio.timeout(5):
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
                # Skipped entirely when camera is offline — all endpoints return 444.
                if do_slow and not is_online:
                    _LOGGER.debug("Slow-tier skipped for %s (offline)", cam_id_key)
                if do_slow and is_online:
                    # ── Parallel slow-tier fetch ──────────────────────────────
                    # All endpoints are independent — fetch in parallel with
                    # asyncio.gather() instead of sequentially.
                    # Reduces slow-tier from ~13×5s = 65s to ~5s (single timeout).
                    hw = cam_raw.get("hardwareVersion", "")
                    pan_limit = cam_raw.get("featureSupport", {}).get("panLimit", 0)

                    async def _fetch(endpoint: str) -> tuple[str, int, any]:
                        """Fetch a single slow-tier endpoint. Returns (endpoint, status, data)."""
                        try:
                            async with asyncio.timeout(8):
                                async with session.get(
                                    f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/{endpoint}",
                                    headers=headers,
                                ) as r:
                                    if r.status == 200:
                                        return (endpoint, 200, await r.json())
                                    return (endpoint, r.status, None)
                        except Exception as err:
                            _LOGGER.debug("%s fetch error for %s: %s", endpoint, cam_id_key, err)
                            return (endpoint, 0, None)

                    # Build task list (skip endpoints not applicable to this camera)
                    endpoints = [
                        "wifiinfo", "ambient_light_sensor_level", "motion",
                        "audioAlarm", "firmware", "recording_options",
                        "unread_events_count", "commissioned", "timestamp",
                        "notifications", "rules",
                    ]
                    if hw in ("INDOOR", "CAMERA_360"):
                        endpoints.append("privacy_sound_override")
                    if pan_limit:
                        endpoints.append("autofollow")

                    results = await asyncio.gather(
                        *[_fetch(ep) for ep in endpoints],
                        return_exceptions=True,
                    )

                    # Process results
                    for result in results:
                        if isinstance(result, Exception):
                            continue
                        ep, status, ep_data = result
                        if status != 200 or ep_data is None:
                            continue
                        if ep == "wifiinfo":
                            self._wifiinfo_cache[cam_id_key] = ep_data
                        elif ep == "ambient_light_sensor_level":
                            self._ambient_light_cache[cam_id_key] = ep_data.get("ambientLightSensorLevel")
                        elif ep == "motion":
                            data[cam_id_key]["motion"] = ep_data
                        elif ep == "audioAlarm":
                            data[cam_id_key]["audioAlarm"] = ep_data
                        elif ep == "firmware":
                            self._firmware_cache[cam_id_key] = ep_data
                        elif ep == "recording_options":
                            data[cam_id_key]["recordingOptions"] = ep_data
                        elif ep == "unread_events_count":
                            if isinstance(ep_data, dict):
                                self._unread_events_cache[cam_id_key] = int(ep_data.get("count", ep_data.get("result", 0)))
                            elif isinstance(ep_data, (int, float)):
                                self._unread_events_cache[cam_id_key] = int(ep_data)
                        elif ep == "privacy_sound_override":
                            self._privacy_sound_cache[cam_id_key] = ep_data.get("result", False)
                        elif ep == "commissioned":
                            self._commissioned_cache[cam_id_key] = ep_data
                        elif ep == "autofollow":
                            data[cam_id_key]["autofollow"] = ep_data
                        elif ep == "timestamp":
                            self._timestamp_cache[cam_id_key] = ep_data.get("result", False)
                        elif ep == "notifications":
                            self._notifications_cache[cam_id_key] = ep_data
                        elif ep == "rules":
                            self._rules_cache[cam_id_key] = ep_data

                # ── RCP data via cloud proxy (slow tier — every 5 min) ────────
                # Opens a proxy connection and reads multiple RCP values.
                # Only when camera is ONLINE and slow-tier interval elapsed.
                # Skip RCP data fetch if a LOCAL stream is active — the RCP fetch
                # opens a REMOTE PUT /connection which would overwrite the LOCAL
                # session and kill the go2rtc stream.
                local_stream_active = (
                    cam_id_key in self._live_connections
                    and self._live_connections[cam_id_key].get("_connection_type") == "LOCAL"
                )
                if is_online and do_slow and not local_stream_active:
                    try:
                        rcp_connector = aiohttp.TCPConnector(ssl=False)
                        rcp_session   = aiohttp.ClientSession(connector=rcp_connector)
                        rcp_headers   = {
                            "Authorization": f"Bearer {token}",
                            "Content-Type":  "application/json",
                            "Accept":        "application/json",
                        }
                        try:
                            async with asyncio.timeout(10):
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

            # ── 5. SHC states (supplementary + offline fallback) ────────────────
            # Cloud is primary (step 4, ~113ms). SHC supplements with camera
            # light state and serves as fallback when cloud is unreachable.
            if self.shc_ready:
                try:
                    await self._async_update_shc_states(data)
                except Exception as err:
                    _LOGGER.debug("SHC state update error: %s", err)

            # ── 6. Auto-download new event files ──────────────────────────────
            if do_events and opts.get("enable_auto_download") and opts.get("download_path"):
                await self.hass.async_add_executor_job(
                    sync_download, self, data, token, opts["download_path"]
                )
                # Mark all downloaded events as read
                dl_event_ids = []
                for cam_id_dl, cam_data_dl in data.items():
                    for ev_dl in cam_data_dl.get("events", []):
                        eid = ev_dl.get("id")
                        if eid:
                            dl_event_ids.append(eid)
                if dl_event_ids:
                    try:
                        await self.async_mark_events_read(dl_event_ids)
                    except Exception:
                        pass

            # ── 7. SMB/NAS upload ─────────────────────────────────────────────
            if do_events and opts.get("enable_smb_upload") and opts.get("smb_server"):
                await self.hass.async_add_executor_job(
                    sync_smb_upload, self, data, token
                )

            # ── 8. SMB daily cleanup (retention) ──────────────────────────────
            _SMB_CLEANUP_INTERVAL = 86400  # once per day
            if (
                opts.get("enable_smb_upload")
                and opts.get("smb_server")
                and opts.get("smb_retention_days", 180) > 0
                and (time.monotonic() - self._last_smb_cleanup) >= _SMB_CLEANUP_INTERVAL
            ):
                self._last_smb_cleanup = time.monotonic()
                await self.hass.async_add_executor_job(sync_smb_cleanup, self)

            # ── 9. SMB disk-free check (hourly) ───────────────────────────────
            _SMB_DISK_CHECK_INTERVAL = 3600  # once per hour
            if (
                opts.get("enable_smb_upload")
                and opts.get("smb_server")
                and opts.get("smb_disk_warn_mb", 500) > 0
                and (time.monotonic() - self._last_smb_disk_check) >= _SMB_DISK_CHECK_INTERVAL
            ):
                self._last_smb_disk_check = time.monotonic()
                await self.hass.async_add_executor_job(sync_smb_disk_check, self)

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
            opts = get_options(self._entry)
            conn_type_pref = self._stream_type_override or opts.get("stream_connection_type", "auto")
            if conn_type_pref == "local":
                candidates = ["LOCAL"]
            elif conn_type_pref == "auto":
                candidates = ["LOCAL", "REMOTE"]
            else:
                candidates = ["REMOTE"]

            for type_val in candidates:
                # LOCAL: default to best quality (no bandwidth limit on LAN)
                # unless user explicitly chose a lower quality setting
                if type_val == "LOCAL" and self.get_quality(cam_id) == "auto":
                    hq, inst = True, 1
                try:
                    async with asyncio.timeout(10):
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
                                audio_param = "&enableaudio=1" if self._audio_enabled.get(cam_id, True) else ""
                                # LOCAL response: {"user": "...", "password": "...", "urls": ["192.168.x.x:443"]}
                                # Embed credentials in RTSP URL for direct LAN access.
                                local_user = result.get("user", "")
                                local_pass = result.get("password", "")
                                if type_val == "LOCAL" and local_user and local_pass:
                                    result["_connection_type"] = "LOCAL"
                                    result["_local_user"]     = local_user
                                    result["_local_password"] = local_pass
                                    urls = result.get("urls", [])
                                    img_scheme = result.get("imageUrlScheme", "https://{url}/snap.jpg")
                                    if urls:
                                        from urllib.parse import quote as _q
                                        cam_addr = urls[0]  # "192.168.x.x:443"
                                        result["proxyUrl"] = img_scheme.replace("{url}", cam_addr)
                                        # Start TLS proxy — FFmpeg can't handle RTSPS + Digest
                                        # auth with self-signed certs. Proxy unwraps TLS only.
                                        cam_host, cam_port = cam_addr.split(":")
                                        proxy_port = await self._start_tls_proxy(
                                            cam_id, cam_host, int(cam_port)
                                        )
                                        eu = _q(local_user, safe="")
                                        ep = _q(local_pass, safe="")
                                        # Plain RTSP through local proxy → TLS to camera
                                        result["rtspsUrl"] = (
                                            f"rtsp://{eu}:{ep}@127.0.0.1:{proxy_port}"
                                            f"/rtsp_tunnel?inst={inst}{audio_param}&fmtp=1&maxSessionDuration=3600"
                                        )
                                        result["rtspUrl"] = result["rtspsUrl"]
                                else:
                                    # REMOTE response: {"urls": ["proxy-NN:42090/{hash}"]}
                                    urls = result.get("urls", [])
                                    if urls:
                                        proxy_host_path = urls[0]
                                        result["proxyUrl"] = f"https://{proxy_host_path}/snap.jpg"
                                        rtsps_host_path   = proxy_host_path.replace(":42090", ":443")
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
                                        result["rtspsUrl"] = (
                                            f"rtsps://{ph}:443/{h}/rtsp_tunnel"
                                            f"?inst={inst}{audio_param}&fmtp=1&maxSessionDuration=3600"
                                        )
                                        result["rtspUrl"] = result["rtspsUrl"]
                                self._live_connections[cam_id] = result
                                self._live_opened_at[cam_id]   = time.monotonic()
                                # Run go2rtc registration and pre-warm in parallel
                                # — both are independent and best-effort.
                                parallel_tasks: list[asyncio.Task] = []
                                rtsps_url = result.get("rtspsUrl", "")
                                if rtsps_url:
                                    parallel_tasks.append(
                                        asyncio.create_task(self._register_go2rtc_stream(cam_id, rtsps_url))
                                    )
                                if type_val == "LOCAL" and local_user and local_pass:
                                    proxy_port_val = self._tls_proxy_ports.get(cam_id)
                                    if proxy_port_val:
                                        parallel_tasks.append(
                                            asyncio.create_task(
                                                pre_warm_rtsp(proxy_port_val, local_user, local_pass, cam_addr.split(":")[0])
                                            )
                                        )
                                if parallel_tasks:
                                    await asyncio.gather(*parallel_tasks, return_exceptions=True)
                                # LOCAL: camera respects maxSessionDuration=3600 in our URL
                                # (tested: stream survives 70s+ without renewal).
                                # Auto-renewal DISABLED — each PUT /connection invalidates
                                # the previous session's credentials, causing 401 errors.
                                self.hass.async_create_task(self.async_request_refresh())
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

        Proxy URL caching: PUT /connection takes ~1.5s. The resulting proxy lease
        lasts ~60s. We cache urls[0] for 50s and skip PUT /connection on warm
        refreshes, reducing latency from ~3s → ~0.5s per card refresh cycle.
        """
        import json as _json

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
        conn_url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        async def _get_proxy_url_entry() -> str | None:
            """Return a valid urls[0] string, using cache when possible."""
            now = time.monotonic()
            cached = self._proxy_url_cache.get(cam_id)
            if cached:
                url_entry, expires_at = cached
                if now < expires_at:
                    _LOGGER.debug(
                        "fetch_live_snapshot: proxy cache HIT for %s (%.0fs remaining)",
                        cam_id, expires_at - now,
                    )
                    return url_entry
                del self._proxy_url_cache[cam_id]

            # Cache miss — call PUT /connection
            async with asyncio.timeout(10):
                async with session.put(
                    conn_url,
                    json={"type": "REMOTE", "highQualityVideo": self.get_quality_params(cam_id)[0]},
                    headers=headers,
                ) as resp:
                    if resp.status not in (200, 201):
                        _LOGGER.debug(
                            "fetch_live_snapshot: PUT /connection → HTTP %d for %s",
                            resp.status, cam_id,
                        )
                        return None
                    result = _json.loads(await resp.text())
                    urls = result.get("urls", [])
                    if not urls:
                        return None
                    self._proxy_url_cache[cam_id] = (urls[0], now + 50.0)  # 50s TTL
                    _LOGGER.debug(
                        "fetch_live_snapshot: proxy cache MISS for %s — PUT /connection done",
                        cam_id,
                    )
                    return urls[0]

        try:
            url_entry = await _get_proxy_url_entry()
            if not url_entry:
                return None

            # ── RCP 0x099e: 320×180 JPEG (faster and lower bandwidth than snap.jpg) ──
            # 0x0a88 READ confirms the camera's snapshot resolution is 320×180.
            # 0x099e returns a JPEG at that resolution via the proxy RCP endpoint.
            # Falls back to snap.jpg below if RCP session or read fails.
            parts = url_entry.split("/", 1)
            if len(parts) == 2:
                proxy_host_rcp, proxy_hash_rcp = parts[0], parts[1]
                rcp_base = f"https://{proxy_host_rcp}/{proxy_hash_rcp}/rcp.xml"
                try:
                    session_id = await self._get_cached_rcp_session(proxy_host_rcp, proxy_hash_rcp)
                    if session_id:
                        raw = await self._rcp_read(rcp_base, "0x099e", session_id)
                        if raw and raw[:2] == b"\xff\xd8":
                            _LOGGER.debug(
                                "fetch_live_snapshot: RCP 0x099e → %d bytes (320×180 JPEG) for %s",
                                len(raw), cam_id,
                            )
                            return raw
                        _LOGGER.debug(
                            "fetch_live_snapshot: RCP 0x099e unavailable for %s — using snap.jpg",
                            cam_id,
                        )
                except Exception as _rcp_err:  # noqa: BLE001
                    _LOGGER.debug(
                        "fetch_live_snapshot: RCP error for %s: %s — using snap.jpg",
                        cam_id, _rcp_err,
                    )

            proxy_url = f"https://{url_entry}/snap.jpg"
            async with asyncio.timeout(10):
                async with session.get(proxy_url) as snap_resp:
                    ct = snap_resp.headers.get("Content-Type", "")
                    if snap_resp.status == 404:
                        # Proxy URL expired — invalidate cache and retry once with a fresh lease
                        _LOGGER.debug(
                            "fetch_live_snapshot: snap.jpg 404 for %s — proxy URL expired, retrying",
                            cam_id,
                        )
                        self._proxy_url_cache.pop(cam_id, None)
                        url_entry2 = await _get_proxy_url_entry()
                        if not url_entry2:
                            return None
                        proxy_url2 = f"https://{url_entry2}/snap.jpg"
                        async with asyncio.timeout(10):
                            async with session.get(proxy_url2) as snap_resp2:
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
            async with asyncio.timeout(15):
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
                    async with asyncio.timeout(20):
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
            async with asyncio.timeout(15):
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

        camera_host = urls[0]  # e.g. "192.168.x.x:443"
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
            async with asyncio.timeout(5):
                async with aiohttp.ClientSession() as s:
                    # go2rtc API: PUT /api/streams?src=URL&name=STREAM_NAME
                    resp = await s.put(
                        f"http://localhost:1984/api/streams",
                        params={"src": go2rtc_src, "name": stream_name},
                    )
                    _LOGGER.info(
                        "go2rtc stream '%s' registered → HTTP %d (src: %s)",
                        stream_name, resp.status, go2rtc_src[:80],
                    )
        except asyncio.TimeoutError:
            _LOGGER.debug("go2rtc API not reachable (timeout) — using TLS proxy instead")
        except aiohttp.ClientError as err:
            _LOGGER.debug("go2rtc API not reachable (%s) — using TLS proxy instead", err)

    async def _unregister_go2rtc_stream(self, cam_id: str) -> None:
        """Remove the camera stream from go2rtc when the live session ends."""
        stream_name = f"bosch_shc_cam_{cam_id.lower()}"
        try:
            async with asyncio.timeout(3):
                async with aiohttp.ClientSession() as s:
                    await s.delete(
                        f"http://localhost:1984/api/streams",
                        params={"name": stream_name},
                    )
                    _LOGGER.debug("go2rtc stream '%s' removed", stream_name)
        except (asyncio.TimeoutError, aiohttp.ClientError):
            pass  # go2rtc may not be running — silently ignore

    async def _start_tls_proxy(self, cam_id: str, cam_host: str, cam_port: int) -> int:
        """Start a local TCP→TLS proxy for a LOCAL RTSPS stream."""
        # Lazy-init SSL context in executor (blocking I/O, must not run in event loop)
        if self._tls_ssl_ctx is None:
            self._tls_ssl_ctx = await self.hass.async_add_executor_job(self._create_ssl_ctx)
        return start_tls_proxy(
            self._tls_ssl_ctx, cam_id, cam_host, cam_port, self._tls_proxy_ports
        )

    @staticmethod
    def _create_ssl_ctx():
        """Create SSL context for TLS proxy (blocking — runs in executor)."""
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _stop_tls_proxy(self, cam_id: str) -> None:
        """Stop the TLS proxy for a camera."""
        stop_tls_proxy(cam_id, self._tls_proxy_ports)

    async def _auto_renew_local_session(self, cam_id: str) -> None:
        """Renew a LOCAL live session before the 60s camera timeout.

        Scheduled via async_call_later(50, ...) after every LOCAL connection open.
        Gets fresh Digest credentials from Bosch cloud and re-registers go2rtc
        with the new URL so the RTSP session continues seamlessly.
        Does nothing if the stream has been turned off or switched to REMOTE.
        """
        conn = self._live_connections.get(cam_id)
        if not conn or conn.get("_connection_type") != "LOCAL":
            return  # stream turned off or switched to REMOTE — nothing to do
        _LOGGER.debug("Auto-renewing LOCAL session for %s", cam_id)
        await self.try_live_connection(cam_id)

    # ── FCM push notifications — delegated to fcm.py ─────────────────────────
    async def _fetch_firebase_config(self) -> dict:
        """Fetch Firebase config (delegated to fcm.py)."""
        return await _fcm_fetch_firebase_config(self.hass)

    async def async_start_fcm_push(self) -> None:
        """Start the FCM push listener (delegated to fcm.py)."""
        return await _fcm_async_start_fcm_push(self)

    async def _register_fcm_with_bosch(self) -> bool:
        """Register FCM token with Bosch CBS (delegated to fcm.py)."""
        return await _fcm_register_fcm_with_bosch(self)

    async def async_stop_fcm_push(self) -> None:
        """Stop the FCM push listener (delegated to fcm.py)."""
        return await _fcm_async_stop_fcm_push(self)

    async def _async_handle_fcm_push(self) -> None:
        """Handle an FCM push (delegated to fcm.py)."""
        return await _fcm_async_handle_fcm_push(self)

    def _get_alert_services(self, type_key: str) -> list[str]:
        """Return notify services for a given alert type (delegated to fcm.py)."""
        return _fcm_get_alert_services(self, type_key)

    @staticmethod
    def _build_notify_data(
        svc: str, message: str, file_path: str | None = None, title: str | None = None,
    ) -> dict:
        """Build notify service call data (delegated to fcm.py)."""
        return _fcm_build_notify_data(svc, message, file_path, title)

    async def _async_send_alert(
        self, cam_name: str, event_type: str, timestamp: str,
        image_url: str, clip_url: str = "", clip_status: str = "",
    ) -> None:
        """Send a 3-step alert (delegated to fcm.py)."""
        return await _fcm_async_send_alert(
            self, cam_name, event_type, timestamp, image_url, clip_url, clip_status,
        )

    async def async_mark_events_read(self, event_ids: list[str]) -> bool:
        """Mark events as read on the Bosch cloud (delegated to fcm.py)."""
        return await _fcm_async_mark_events_read(self, event_ids)

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        """Write binary data to file (delegated to fcm.py)."""
        _fcm_write_file(path, data)

    # ── RCP protocol (Bosch Remote Configuration Protocol via cloud proxy) ──────
    async def _get_cached_rcp_session(self, proxy_host: str, proxy_hash: str) -> str | None:
        """Return a cached RCP session ID, opening a new one if missing or expired.

        Caches valid session IDs for 5 minutes (TTL 300 s) to avoid the 2-step
        RCP handshake (0xff0c + 0xff0d) on every thumbnail or data fetch.
        """
        now = time.monotonic()
        cached = self._rcp_session_cache.get(proxy_hash)
        if cached:
            session_id, expires_at = cached
            if now < expires_at:
                return session_id
            del self._rcp_session_cache[proxy_hash]

        session_id = await self._rcp_session(proxy_host, proxy_hash)
        if session_id:
            self._rcp_session_cache[proxy_hash] = (session_id, now + 300.0)  # 5-min TTL
        return session_id

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
                    async with asyncio.timeout(8):
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
                    async with asyncio.timeout(8):
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

        Uses the HA shared session (verify_ssl=False) to avoid creating a new
        connector+session per RCP command (prevents socket exhaustion).
        """
        params: dict = {
            "command":   command,
            "direction": "READ",
            "type":      type_,
            "sessionid": sessionid,
        }
        if num:
            params["num"] = str(num)

        session = async_get_clientsession(self.hass, verify_ssl=False)
        try:
            async with asyncio.timeout(8):
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

    async def _async_update_rcp_data(self, cam_id: str, proxy_host: str, proxy_hash: str) -> None:
        """Fetch RCP data (LED dimmer, privacy state) for a camera via cloud proxy.

        Opens a fresh RCP session, reads 0x0c22 (LED dimmer) and 0x0d00 (privacy mask),
        and caches the results. Gracefully skips on any failure — RCP is read-only
        supplementary data and must never block the main coordinator update.
        """
        session_id = await self._get_cached_rcp_session(proxy_host, proxy_hash)
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
        """Return current quality preference: 'auto', 'high', or 'low'.

        Priority:
          1. Runtime override set by BoschVideoQualitySelect (session-only)
          2. Options setting 'high_quality_video' (persistent default)
          3. 'auto' (balanced, ~7.5 Mbps)
        """
        if cam_id in self._quality_preference:
            return self._quality_preference[cam_id]
        if get_options(self._entry).get("high_quality_video"):
            return "high"
        return "auto"

    def set_quality(self, cam_id: str, quality: str) -> None:
        """Set quality preference. quality must be 'auto', 'high', or 'low'."""
        self._quality_preference[cam_id] = quality
        # Invalidate proxy URL cache so next fetch uses a fresh PUT /connection
        # with the updated highQualityVideo flag
        self._proxy_url_cache.pop(cam_id, None)

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
        session = async_get_clientsession(self.hass, verify_ssl=False)
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/{endpoint}"
        try:
            async with asyncio.timeout(10):
                async with session.put(url, headers=headers, json=payload) as resp:
                    if resp.status == 401:
                        # Token expired — refresh and retry once
                        _LOGGER.info("async_put_camera %s/%s: 401 — refreshing token", cam_id, endpoint)
                        try:
                            token = await self._ensure_valid_token()
                            headers["Authorization"] = f"Bearer {token}"
                        except Exception:
                            return False
                        async with asyncio.timeout(10):
                            async with session.put(url, headers=headers, json=payload) as resp2:
                                return resp2.status in (200, 204)
                    return resp.status in (200, 204)
        except Exception as err:
            _LOGGER.warning("async_put_camera %s/%s error: %s", cam_id, endpoint, err)
            return False

    # ── SHC local API + Cloud API setters ────────────────────────────────────
    # Implementation lives in shc.py — these are thin delegation wrappers.

    @property
    def shc_configured(self) -> bool:
        """True if SHC local API is fully configured (IP + certs)."""
        return shc_mod.shc_configured(self)

    @property
    def shc_ready(self) -> bool:
        """True if SHC is configured AND currently considered available."""
        return shc_mod.shc_ready(self)

    def _shc_mark_success(self) -> None:
        shc_mod._shc_mark_success(self)

    def _shc_mark_failure(self) -> None:
        shc_mod._shc_mark_failure(self)

    async def _async_shc_request(
        self, method: str, path: str, body: dict | None = None
    ) -> dict | list | None:
        return await shc_mod.async_shc_request(self, method, path, body)

    async def _async_update_shc_states(self, data: dict) -> None:
        return await shc_mod.async_update_shc_states(self, data)

    async def async_shc_set_camera_light(self, cam_id: str, on: bool) -> bool:
        return await shc_mod.async_shc_set_camera_light(self, cam_id, on)

    async def async_shc_set_privacy_mode(self, cam_id: str, enabled: bool) -> bool:
        return await shc_mod.async_shc_set_privacy_mode(self, cam_id, enabled)

    async def async_cloud_set_privacy_mode(self, cam_id: str, enabled: bool) -> bool:
        return await shc_mod.async_cloud_set_privacy_mode(self, cam_id, enabled)

    async def async_cloud_set_camera_light(self, cam_id: str, on: bool) -> bool:
        return await shc_mod.async_cloud_set_camera_light(self, cam_id, on)

    async def async_cloud_set_notifications(self, cam_id: str, enabled: bool) -> bool:
        return await shc_mod.async_cloud_set_notifications(self, cam_id, enabled)

    async def async_cloud_set_pan(self, cam_id: str, position: int) -> bool:
        return await shc_mod.async_cloud_set_pan(self, cam_id, position)

    # SMB/NAS upload, download, cleanup, and disk-check functions are in smb.py

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})
    # Register services at domain level — ensures they are available even when
    # the config entry is in setup_retry (e.g. token expired).
    # Without this, the Lovelace card shows "action not found" errors.
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})

    coordinator = BoschCameraCoordinator(hass, entry)

    await coordinator.async_config_entry_first_refresh()

    # Start proactive background token refresh (5 min before JWT expiry)
    coordinator._schedule_token_refresh()

    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}

    opts = get_options(entry)
    platforms = [p for p in ALL_PLATFORMS if p != "binary_sensor"]
    if opts.get("enable_binary_sensors", True):
        platforms = ["binary_sensor"] + platforms

    await hass.config_entries.async_forward_entry_setups(entry, platforms)

    # Reload integration when options change (e.g. scan_interval updated)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Start FCM push listener (runs in background, non-blocking)
    if opts.get("enable_fcm_push", False):
        hass.async_create_task(coordinator.async_start_fcm_push())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Stop FCM push listener before unloading
    edata = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if coord := edata.get("coordinator"):
        await coord.async_stop_fcm_push()

    unloaded = await hass.config_entries.async_unload_platforms(entry, ALL_PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when user changes options in the UI.

    This listener fires on any config entry update (data OR options).
    We only reload if options actually changed — data-only updates
    (e.g. persisting a refreshed token) should NOT trigger a reload.
    """
    edata = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coord = edata.get("coordinator")
    if coord:
        prev_opts = coord.options
        new_opts = get_options(entry)
        if prev_opts == new_opts:
            _LOGGER.debug("Config entry updated (data only) — skipping reload")
            return
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register HA services (skip if already registered)."""

    async def handle_trigger_snapshot(call: ServiceCall) -> None:
        """Force an immediate refresh for all cameras (data + images)."""
        for edata in hass.data.get(DOMAIN, {}).values():
            if coord := edata.get("coordinator"):
                # Fire coordinator refresh in background — do NOT await it.
                # async_request_refresh() awaits the full coordinator tick which can
                # take 6-22 s; blocking here freezes the card until the tick finishes.
                hass.async_create_task(coord.async_request_refresh())
                for cam_id, cam in coord._camera_entities.items():
                    hass.async_create_task(cam._async_trigger_image_refresh(delay=0))

    async def handle_open_live_connection(call: ServiceCall) -> None:
        """Try to open a live proxy connection for a specific camera."""
        cam_id = call.data.get("camera_id", "")
        for edata in hass.data.get(DOMAIN, {}).values():
            if coord := edata.get("coordinator"):
                result = await coord.try_live_connection(cam_id)
                if result:
                    _LOGGER.info("Live connection established: %s", result)

    async def handle_create_rule(call: ServiceCall) -> None:
        """Create a cloud-side schedule rule for a camera."""
        cam_id = call.data.get("camera_id", "")
        name = call.data.get("name", "HA Rule")
        start_time = call.data.get("start_time", "00:00:00")
        end_time = call.data.get("end_time", "23:59:00")
        weekdays = call.data.get("weekdays", [0, 1, 2, 3, 4, 5, 6])
        is_active = call.data.get("is_active", True)
        for edata in hass.data.get(DOMAIN, {}).values():
            if coord := edata.get("coordinator"):
                payload = {
                    "id": None, "name": name, "isActive": is_active,
                    "startTime": start_time, "endTime": end_time,
                    "weekdays": weekdays,
                }
                session = async_get_clientsession(hass, verify_ssl=False)
                headers = {"Authorization": f"Bearer {coord.token}", "Content-Type": "application/json"}
                try:
                    async with asyncio.timeout(10):
                        async with session.post(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules",
                            headers=headers, json=payload,
                        ) as resp:
                            if resp.status in (200, 201):
                                result = await resp.json()
                                _LOGGER.info("Rule created: %s", result)
                            else:
                                _LOGGER.warning("Create rule failed: HTTP %d", resp.status)
                except Exception as err:
                    _LOGGER.warning("Create rule error: %s", err)
                break

    async def handle_delete_rule(call: ServiceCall) -> None:
        """Delete a cloud-side schedule rule."""
        cam_id = call.data.get("camera_id", "")
        rule_id = call.data.get("rule_id", "")
        for edata in hass.data.get(DOMAIN, {}).values():
            if coord := edata.get("coordinator"):
                session = async_get_clientsession(hass, verify_ssl=False)
                headers = {"Authorization": f"Bearer {coord.token}"}
                try:
                    async with asyncio.timeout(10):
                        async with session.delete(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules/{rule_id}",
                            headers=headers,
                        ) as resp:
                            if resp.status == 204:
                                _LOGGER.info("Rule %s deleted", rule_id)
                            else:
                                _LOGGER.warning("Delete rule failed: HTTP %d", resp.status)
                except Exception as err:
                    _LOGGER.warning("Delete rule error: %s", err)
                break

    if not hass.services.has_service(DOMAIN, "trigger_snapshot"):
        hass.services.async_register(DOMAIN, "trigger_snapshot", handle_trigger_snapshot)
    if not hass.services.has_service(DOMAIN, "open_live_connection"):
        hass.services.async_register(DOMAIN, "open_live_connection", handle_open_live_connection)
    if not hass.services.has_service(DOMAIN, "create_rule"):
        hass.services.async_register(DOMAIN, "create_rule", handle_create_rule)
    if not hass.services.has_service(DOMAIN, "delete_rule"):
        hass.services.async_register(DOMAIN, "delete_rule", handle_delete_rule)
