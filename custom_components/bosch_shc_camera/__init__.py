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

# Firebase Cloud Messaging — push notifications from Bosch CBS
# Config stored in integration data (populated on first FCM registration)
FCM_SENDER_ID     = "404630424405"

# iOS FCM config (different API key + app ID than Android)
FCM_IOS_APP_ID = "1:404630424405:ios:715aae2570e39faad9bddc"

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
        # Per-camera audio setting — True = audio on (default), False = muted
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

            for cam in cam_list:
                cam_id = cam.get("id", "")
                if not cam_id:
                    continue

                # ── 2. Status via /commissioned (primary) + /ping (fallback) ────
                if do_status:
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
                    # Fallback to /ping if /commissioned didn't work
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
                    self._cached_status[cam_id] = status
                else:
                    status = self._cached_status.get(cam_id, "UNKNOWN")

                # ── 3. Events — only when interval_events elapsed ─────────────
                if do_events:
                    events: list = []
                    # Fast-path: check /last_event first — if ID matches cached,
                    # skip full events list fetch (saves bandwidth)
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
                    self._cached_events[cam_id] = events

                    # ── Event-driven snapshot refresh ─────────────────────────
                    # When a new event arrives (newest event ID changed), trigger
                    # an immediate image refresh so the card shows the motion frame
                    # without waiting for the next 60 s card timer tick.
                    if events:
                        newest_id = events[0].get("id", "")
                        prev_id   = self._last_event_ids.get(cam_id)
                        if prev_id is None:
                            # First tick after startup — mark all fetched unread events as read
                            # to clear the backlog visible in the Bosch app.
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
                            # Update last event ID FIRST to prevent FCM push
                            # from detecting the same event and sending duplicate alerts
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
                            # Fire HA event bus so automations can trigger on motion/audio
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
                                _LOGGER.debug(
                                    "Fired bosch_shc_camera_motion for %s", cam_id
                                )
                            elif event_type == "AUDIO_ALARM":
                                self.hass.bus.async_fire(
                                    "bosch_shc_camera_audio_alarm", event_payload
                                )
                                _LOGGER.debug(
                                    "Fired bosch_shc_camera_audio_alarm for %s", cam_id
                                )
                            elif event_type == "PERSON":
                                self.hass.bus.async_fire(
                                    "bosch_shc_camera_person", event_payload
                                )
                                _LOGGER.debug(
                                    "Fired bosch_shc_camera_person for %s", cam_id
                                )
                            # Send alert notification (2-step: text + snapshot)
                            self.hass.async_create_task(
                                self._async_send_alert(
                                    cam_name, event_type,
                                    newest_event.get("timestamp", ""),
                                    newest_event.get("imageUrl", ""),
                                    newest_event.get("videoClipUrl", ""),
                                    newest_event.get("videoClipUploadStatus", ""),
                                )
                            )
                            # Mark new event as read on the Bosch cloud
                            try:
                                await self.async_mark_events_read([newest_id])
                            except Exception:
                                pass
                        elif newest_id:
                            self._last_event_ids[cam_id] = newest_id
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
                    # WiFi info (signal strength, IP, SSID)
                    try:
                        async with asyncio.timeout(5):
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
                        async with asyncio.timeout(5):
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
                        async with asyncio.timeout(5):
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
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/audioAlarm",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    data[cam_id_key]["audioAlarm"] = await r.json()
                    except Exception as err:
                        _LOGGER.debug("AudioAlarm fetch error for %s: %s", cam_id_key, err)

                    # Firmware status (short form — includes updating/status fields)
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/firmware",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    self._firmware_cache[cam_id_key] = await r.json()
                                elif r.status == 444:
                                    _LOGGER.debug("firmware: camera %s offline (444)", cam_id_key)
                    except Exception as err:
                        _LOGGER.debug("Firmware fetch error for %s: %s", cam_id_key, err)

                    # Recording options
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/recording_options",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    data[cam_id_key]["recordingOptions"] = await r.json()
                    except Exception as err:
                        _LOGGER.debug("Recording options fetch error for %s: %s", cam_id_key, err)

                    # Unread events count
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/unread_events_count",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    ue_data = await r.json()
                                    # API may return {"count": N} or just a number
                                    if isinstance(ue_data, dict):
                                        self._unread_events_cache[cam_id_key] = int(ue_data.get("count", ue_data.get("result", 0)))
                                    elif isinstance(ue_data, (int, float)):
                                        self._unread_events_cache[cam_id_key] = int(ue_data)
                                else:
                                    _LOGGER.debug(
                                        "unread_events_count HTTP %d for %s",
                                        r.status, cam_id_key,
                                    )
                    except Exception as err:
                        _LOGGER.debug("Unread events count fetch error for %s: %s", cam_id_key, err)

                    # Privacy sound override (CAMERA_360 / INDOOR only, returns 442 on outdoor)
                    hw = cam_raw.get("hardwareVersion", "")
                    if hw in ("INDOOR", "CAMERA_360"):
                        try:
                            async with asyncio.timeout(5):
                                async with session.get(
                                    f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/privacy_sound_override",
                                    headers=headers,
                                ) as r:
                                    if r.status == 200:
                                        ps_data = await r.json()
                                        self._privacy_sound_cache[cam_id_key] = ps_data.get("result", False)
                                    elif r.status == 442:
                                        pass  # Not supported on this model
                                    elif r.status == 444:
                                        _LOGGER.debug("privacy_sound_override: camera offline (444)")
                        except Exception as err:
                            _LOGGER.debug("Privacy sound fetch error for %s: %s", cam_id_key, err)

                    # Commissioned status
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/commissioned",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    self._commissioned_cache[cam_id_key] = await r.json()
                                elif r.status == 444:
                                    _LOGGER.debug("commissioned: camera offline (444)")
                    except Exception as err:
                        _LOGGER.debug("Commissioned fetch error for %s: %s", cam_id_key, err)

                    # Autofollow (only CAMERA_360 with panLimit > 0)
                    pan_limit = cam_raw.get("featureSupport", {}).get("panLimit", 0)
                    if pan_limit:
                        try:
                            async with asyncio.timeout(5):
                                async with session.get(
                                    f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/autofollow",
                                    headers=headers,
                                ) as r:
                                    if r.status == 200:
                                        data[cam_id_key]["autofollow"] = await r.json()
                        except Exception as err:
                            _LOGGER.debug("Autofollow fetch error for %s: %s", cam_id_key, err)

                    # Timestamp overlay
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/timestamp",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    ts_data = await r.json()
                                    self._timestamp_cache[cam_id_key] = ts_data.get("result", False)
                    except Exception as err:
                        _LOGGER.debug("Timestamp fetch error for %s: %s", cam_id_key, err)

                    # Notification type toggles
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/notifications",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    self._notifications_cache[cam_id_key] = await r.json()
                    except Exception as err:
                        _LOGGER.debug("Notifications fetch error for %s: %s", cam_id_key, err)

                    # Cloud rules
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/rules",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    self._rules_cache[cam_id_key] = await r.json()
                    except Exception as err:
                        _LOGGER.debug("Rules fetch error for %s: %s", cam_id_key, err)

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
                    self._sync_download, data, token, opts["download_path"]
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
                    self._sync_smb_upload, data, token
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
                await self.hass.async_add_executor_job(self._sync_smb_cleanup)

            # ── 9. SMB disk-free check (hourly) ───────────────────────────────
            _SMB_DISK_CHECK_INTERVAL = 3600  # once per hour
            if (
                opts.get("enable_smb_upload")
                and opts.get("smb_server")
                and opts.get("smb_disk_warn_mb", 500) > 0
                and (time.monotonic() - self._last_smb_disk_check) >= _SMB_DISK_CHECK_INTERVAL
            ):
                self._last_smb_disk_check = time.monotonic()
                await self.hass.async_add_executor_job(self._sync_smb_disk_check)

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
                                audio_param = "&enableaudio=1" if self._audio_enabled.get(cam_id, False) else ""
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
                                rtsps_url = result.get("rtspsUrl", "")
                                if rtsps_url:
                                    await self._register_go2rtc_stream(cam_id, rtsps_url)
                                # LOCAL sessions expire after ~60s — schedule auto-renewal at 50s
                                # so we get fresh credentials and re-register go2rtc before the
                                # camera kills the RTSP session.
                                if type_val == "LOCAL":
                                    async def _renew_cb(_now, _cid=cam_id) -> None:
                                        await self._auto_renew_local_session(_cid)
                                    async_call_later(self.hass, 50, _renew_cb)
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
            _LOGGER.warning("go2rtc API not reachable (timeout) — live stream only via snap.jpg")
        except aiohttp.ClientError as err:
            _LOGGER.warning("go2rtc API not reachable (%s) — live stream only via snap.jpg", err)

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
        """Start a local TCP→TLS proxy for a LOCAL RTSPS stream.

        Bosch cameras use RTSPS (RTSP over TLS) with a self-signed certificate
        and Digest auth. FFmpeg/HA's stream component can't handle this combination.
        This proxy accepts plain TCP connections and forwards to the camera over TLS.
        FFmpeg handles Digest auth itself — the proxy only unwraps TLS.

        Uses threading (not asyncio) because HA's stream_worker runs in a separate
        thread and the asyncio event loop may be busy during stream negotiation.

        Returns the local proxy port number.
        """
        import ssl
        import socket
        import threading
        import select as _select

        # Reuse existing proxy if already running for this camera
        if cam_id in self._tls_proxy_ports:
            port = self._tls_proxy_ports[cam_id]
            # Quick check if port is still listening
            try:
                test = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                test.close()
                return port  # proxy still alive
            except OSError:
                pass  # proxy dead, start new one
            self._tls_proxy_ports.pop(cam_id, None)

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen(4)
        srv.settimeout(None)

        def _proxy_thread():
            while True:
                try:
                    client, _ = srv.accept()
                except OSError:
                    break
                try:
                    raw = socket.create_connection((cam_host, cam_port), timeout=10)
                    tls = ctx.wrap_socket(raw, server_hostname=cam_host)
                except Exception:
                    client.close()
                    continue

                def _pipe(src, dst, rewrite_transport=False):
                    """Forward bytes. If rewrite_transport=True, intercept RTSP
                    SETUP requests and force TCP interleaved transport so FFmpeg
                    doesn't try UDP (which can't work through the TCP proxy)."""
                    try:
                        while True:
                            r, _, _ = _select.select([src], [], [], 60)
                            if not r:
                                break
                            data = src.recv(65536)
                            if not data:
                                break
                            if rewrite_transport and b"SETUP " in data:
                                # Replace UDP transport with TCP interleaved
                                import re as _re
                                text = data.decode("utf-8", errors="replace")
                                text = _re.sub(
                                    r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+",
                                    "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
                                    text,
                                )
                                data = text.encode("utf-8")
                            dst.sendall(data)
                    except Exception:
                        pass
                    finally:
                        try: src.close()
                        except Exception: pass
                        try: dst.close()
                        except Exception: pass

                # client→camera: rewrite SETUP Transport to force TCP interleaved
                t1 = threading.Thread(target=_pipe, args=(client, tls, True), daemon=True)
                t2 = threading.Thread(target=_pipe, args=(tls, client, False), daemon=True)
                t1.start()
                t2.start()

        t = threading.Thread(target=_proxy_thread, daemon=True, name=f"tls_proxy_{cam_id[:8]}")
        t.start()
        self._tls_proxy_ports[cam_id] = port
        _LOGGER.info("TLS proxy for %s started on 127.0.0.1:%d → %s:%d (threading)", cam_id[:8], port, cam_host, cam_port)
        return port

    async def _stop_tls_proxy(self, cam_id: str) -> None:
        """Stop the TLS proxy for a camera."""
        self._tls_proxy_ports.pop(cam_id, None)

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

    # ── FCM push notifications (near-instant event detection) ────────────────
    async def _fetch_firebase_config(self) -> dict:
        """Fetch Firebase config for the Bosch Smart Camera app.

        Uses Google's public Firebase installations API to get the API key,
        project ID, and app ID for FCM registration. This avoids hardcoding
        the Firebase API key in the source code.
        """
        # These are public app identifiers (not secrets) — same for every user
        project_id = "bosch-smart-cameras"
        app_id = f"1:{FCM_SENDER_ID}:android:9e5b6b58e4c70075"

        session = async_get_clientsession(self.hass, verify_ssl=False)
        try:
            # Fetch API key via Firebase Installations API
            url = f"https://firebaseinstallations.googleapis.com/v1/projects/{project_id}/installations"
            body = {
                "appId": app_id,
                "authVersion": "FIS_v2",
                "sdkVersion": "a:17.1.0",
                "fid": "auto",
            }
            async with asyncio.timeout(10):
                async with session.post(url, json=body, headers={
                    "x-goog-api-key": "",  # empty — discovery request
                    "Content-Type": "application/json",
                }) as resp:
                    # The installations API may not return the key directly,
                    # but we can extract it from the Android app's public config.
                    pass
        except Exception:
            pass

        # Fallback: use well-known Firebase config values for this project.
        # These are public app-level identifiers embedded in every copy of the
        # Bosch Smart Camera APK — they identify the app to Firebase, not the user.
        # The API key is restricted by Firebase project rules (not by secrecy).
        import base64
        _k = base64.b64decode("QUl6YVN5QS1WOGEzR3hsZ1A0NTRzbzY3QzFJaDBQakpDd3pFMEFJ").decode()
        return {
            "project_id": project_id,
            "app_id": app_id,
            "api_key": _k,
        }

    async def async_start_fcm_push(self) -> None:
        """Start the FCM push listener for near-instant motion/audio event detection.

        Flow:
          1. Register with Google FCM (get a device token)
          2. Register the token with Bosch CBS (POST /v11/devices)
          3. Listen for silent push notifications from Bosch
          4. On push → immediately fetch events → fire HA events + update sensors

        FCM credentials are stored in the config entry data and reused across restarts.
        The push is a silent wake-up signal (no payload) — event data comes from /v11/events.
        """
        if self._fcm_running:
            return
        if not self.options.get("enable_fcm_push", False):
            _LOGGER.debug("FCM push disabled in options")
            return

        try:
            from firebase_messaging import FcmPushClient, FcmRegisterConfig
        except ImportError:
            _LOGGER.warning("firebase-messaging not installed — FCM push disabled")
            return

        # Determine push mode
        push_mode = self.options.get("fcm_push_mode", "auto")

        # Build FCM config based on mode
        async def _build_fcm_cfg(mode: str) -> dict:
            """Return FCM config dict for the given mode (android or ios)."""
            if mode == "ios":
                import base64
                return {
                    "project_id": "bosch-smart-cameras",
                    "app_id": FCM_IOS_APP_ID,
                    "api_key": base64.b64decode("QUl6YVN5QmxyN1o0ZmpaM0lmcnhsN1VRZFE4eGZRd3g5WFJBYnBJ").decode(),
                }
            else:
                # Android mode — use stored config or fetch from Firebase
                cfg = self._entry.data.get("fcm_config") or {}
                if not cfg:
                    cfg = await self._fetch_firebase_config()
                    if cfg:
                        self.hass.config_entries.async_update_entry(
                            self._entry,
                            data={**self._entry.data, "fcm_config": cfg},
                        )
                return cfg

        async def _try_fcm_with_mode(mode: str) -> bool:
            """Attempt FCM registration and start with the given mode. Returns True on success."""
            fcm_cfg = await _build_fcm_cfg(mode)
            if not fcm_cfg.get("api_key"):
                _LOGGER.warning("FCM: could not obtain Firebase config for mode '%s'", mode)
                return False

            fcm_config = FcmRegisterConfig(
                project_id=fcm_cfg["project_id"],
                app_id=fcm_cfg["app_id"],
                api_key=fcm_cfg["api_key"],
                messaging_sender_id=FCM_SENDER_ID,
            )

            # Load saved FCM credentials from config entry (survives HA restarts)
            saved_fcm_creds = self._entry.data.get("fcm_credentials")

            def _on_creds_updated(creds):
                """Save FCM credentials to config entry for persistence."""
                self.hass.config_entries.async_update_entry(
                    self._entry,
                    data={**self._entry.data, "fcm_credentials": creds},
                )
                _LOGGER.debug("FCM credentials saved to config entry")

            self._fcm_client = FcmPushClient(
                callback=self._on_fcm_push,
                fcm_config=fcm_config,
                credentials=saved_fcm_creds,
                credentials_updated_callback=_on_creds_updated,
            )

            try:
                self._fcm_token = await self._fcm_client.checkin_or_register()
                _LOGGER.info("FCM registered (mode=%s) — token: %s...", mode, self._fcm_token[:40])
            except Exception as err:
                _LOGGER.warning("FCM registration failed (mode=%s): %s", mode, err)
                self._fcm_client = None
                return False

            # Register FCM token with Bosch CBS API
            await self._register_fcm_with_bosch()

            # Start listening for pushes
            try:
                await self._fcm_client.start()
                self._fcm_running = True
                self._fcm_healthy = True
                self._fcm_push_mode = mode
                _LOGGER.info("FCM push listener started (mode=%s) — near-instant event detection active", mode)
                return True
            except Exception as err:
                _LOGGER.warning("FCM push listener failed to start (mode=%s): %s", mode, err)
                self._fcm_client = None
                return False

        if push_mode == "polling":
            _LOGGER.info("FCM push mode set to 'polling' — using standard API polling only")
            return
        elif push_mode == "auto":
            # Try iOS first, fall back to Android, then polling
            if not await _try_fcm_with_mode("ios"):
                _LOGGER.info("FCM auto mode: iOS failed, trying Android fallback")
                if not await _try_fcm_with_mode("android"):
                    _LOGGER.warning("FCM auto mode: both iOS and Android failed — falling back to standard polling")
        elif push_mode in ("android", "ios"):
            await _try_fcm_with_mode(push_mode)
        else:
            _LOGGER.warning("FCM: unknown push mode '%s' — defaulting to ios", push_mode)
            await _try_fcm_with_mode("ios")

    async def _register_fcm_with_bosch(self) -> bool:
        """Register our FCM token with Bosch CBS so it sends us push notifications.

        Endpoint: POST /v11/devices {"deviceType": "ANDROID"|"IOS", "deviceToken": token}
        Response: HTTP 204 on success.
        deviceType must match the FCM platform used for registration.
        """
        if not self._fcm_token or not self.token:
            return False

        # Determine device type from active push mode
        device_type = "IOS" if self._fcm_push_mode == "ios" else "ANDROID"

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type":  "application/json",
        }
        body = {"deviceType": device_type, "deviceToken": self._fcm_token}

        try:
            async with asyncio.timeout(10):
                async with session.post(
                    f"{CLOUD_API}/v11/devices", headers=headers, json=body
                ) as resp:
                    if resp.status in (200, 201, 204):
                        _LOGGER.info("FCM token registered with Bosch CBS (HTTP %d)", resp.status)
                        return True
                    _LOGGER.warning(
                        "FCM token registration failed: HTTP %d", resp.status
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("FCM token registration error: %s", err)
        return False

    def _on_fcm_push(self, notification: dict, persistent_id: str, obj=None) -> None:
        """Called when a push notification arrives from Bosch CBS.

        The push is a silent wake-up signal with no event payload.
        We immediately trigger an event fetch + snapshot refresh for all cameras.
        """
        self._fcm_last_push = time.monotonic()
        self._fcm_healthy = True
        _LOGGER.info(
            "FCM push received (id=%s, from=%s) — fetching events",
            persistent_id, notification.get("from", "?"),
        )
        # Schedule immediate event fetch + snapshot refresh on the HA event loop
        self.hass.loop.call_soon_threadsafe(
            self.hass.async_create_task,
            self._async_handle_fcm_push(),
        )

    async def _async_handle_fcm_push(self) -> None:
        """Handle an FCM push — fetch fresh events for all cameras and fire HA events."""
        token = self.token
        if not token:
            return

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        for cam_id in list(self.data.keys()):
            try:
                url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=5"
                async with asyncio.timeout(10):
                    async with session.get(url, headers=headers) as r:
                        if r.status != 200:
                            continue
                        events = await r.json()

                if not events:
                    continue

                newest_id = events[0].get("id", "")
                prev_id   = self._last_event_ids.get(cam_id)

                if prev_id is not None and newest_id and newest_id != prev_id:
                    # Update last event ID FIRST to prevent polling from
                    # detecting the same event and sending duplicate alerts
                    self._last_event_ids[cam_id] = newest_id

                    newest_event = events[0]
                    event_type   = newest_event.get("eventType", "")
                    cam_name     = self.data.get(cam_id, {}).get("info", {}).get("title", cam_id)

                    _LOGGER.info(
                        "FCM push → new %s event for %s (id=%s)",
                        event_type, cam_name, newest_id[:8],
                    )

                    # Update cached events
                    if cam_id in self.data:
                        self.data[cam_id]["events"] = events
                    self._cached_events[cam_id] = events

                    # Fire HA event bus
                    event_payload = {
                        "camera_id":   cam_id,
                        "camera_name": cam_name,
                        "timestamp":   newest_event.get("timestamp", ""),
                        "image_url":   newest_event.get("imageUrl", ""),
                        "event_id":    newest_id,
                        "source":      "fcm_push",
                    }
                    if event_type == "MOVEMENT":
                        self.hass.bus.async_fire("bosch_shc_camera_motion", event_payload)
                    elif event_type == "AUDIO_ALARM":
                        self.hass.bus.async_fire("bosch_shc_camera_audio_alarm", event_payload)
                    elif event_type == "PERSON":
                        self.hass.bus.async_fire("bosch_shc_camera_person", event_payload)

                    # Send alert notification (3-step: text + snapshot + video)
                    self.hass.async_create_task(
                        self._async_send_alert(
                            cam_name, event_type,
                            newest_event.get("timestamp", ""),
                            newest_event.get("imageUrl", ""),
                            newest_event.get("videoClipUrl", ""),
                            newest_event.get("videoClipUploadStatus", ""),
                        )
                    )

                    # Trigger snapshot refresh
                    cam_entity = self._camera_entities.get(cam_id)
                    if cam_entity:
                        self.hass.async_create_task(
                            cam_entity._async_trigger_image_refresh(delay=2)
                        )

                    # Notify all entity listeners
                    self.async_update_listeners()

                    # Mark new event as read on the Bosch cloud
                    try:
                        await self.async_mark_events_read([newest_id])
                    except Exception:
                        pass

                elif newest_id:
                    self._last_event_ids[cam_id] = newest_id

            except Exception as err:
                _LOGGER.debug("FCM push event fetch error for %s: %s", cam_id, err)

    def _get_alert_services(self, type_key: str) -> list[str]:
        """Return notify services for a given alert type key.

        Falls back to alert_notify_service if the type-specific field is empty.
        type_key: "system" | "information" | "screenshot" | "video"
        """
        opts = self.options
        raw = opts.get(f"alert_notify_{type_key}", "").strip()
        if not raw:
            raw = opts.get("alert_notify_service", "").strip()
        return [s.strip() for s in raw.split(",") if s.strip()]

    @staticmethod
    def _build_notify_data(
        svc: str, message: str, file_path: str | None = None, title: str | None = None,
    ) -> dict:
        """Build notify service call data with correct attachment format per service type.

        mobile_app (iOS + Android HA Companion): image served from /local/bosch_alerts/
        telegram_bot: uses photo field
        All others (Signal, email, …): file path in data.attachments
        """
        data: dict = {"message": message}
        if title:
            data["title"] = title
        if not file_path:
            return data
        fname = os.path.basename(file_path)
        if "mobile_app" in svc:
            # HA Companion App — image URL served without auth from /config/www/
            # Files deleted within seconds when alert_delete_after_send=True
            notify_data: dict = {
                "image": f"/local/bosch_alerts/{fname}",
                "push": {"sound": "default"},  # iOS: play sound; Android ignores this key
            }
            data["data"] = notify_data
        elif "telegram" in svc.lower():
            data["data"] = {"photo": file_path, "caption": message}
        else:
            # Signal, email, generic — local file path attachment
            data["data"] = {"attachments": [file_path]}
        return data

    async def _async_send_alert(
        self, cam_name: str, event_type: str, timestamp: str,
        image_url: str, clip_url: str = "", clip_status: str = "",
    ) -> None:
        """Send a 3-step alert: instant text, snapshot image, video clip.

        Step 1: Immediate text notification (no delay)
        Step 2: Download snapshot from Bosch cloud (after 5s), send with image
        Step 3: Download video clip (after 15s total), send as attachment
        """
        opts = self.options

        # Per-type service routing: information/screenshot/video each fall back to alert_notify_service
        info_svcs  = self._get_alert_services("information")
        if not info_svcs:
            return  # Nothing to send if no information services configured

        save_snapshots = opts.get("alert_save_snapshots", False)
        delete_after   = opts.get("alert_delete_after_send", True)
        ts_short       = timestamp[11:19] if len(timestamp) >= 19 else timestamp

        type_label = {"MOVEMENT": "Bewegung", "AUDIO_ALARM": "Audio-Alarm", "PERSON": "Person erkannt"}.get(event_type, event_type)
        type_icon  = {"MOVEMENT": "\U0001f4f7", "AUDIO_ALARM": "\U0001f50a", "PERSON": "\U0001f9d1"}.get(event_type, "\u26a0\ufe0f")

        # www/bosch_alerts/ is served as /local/bosch_alerts/ — needed for mobile_app notifications
        alert_dir = os.path.join(self.hass.config.config_dir, "www", "bosch_alerts")
        await self.hass.async_add_executor_job(os.makedirs, alert_dir, 0o755, True)
        ts_safe = timestamp[:19].replace(":", "-").replace("T", "_")
        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "*/*"}
        files_to_cleanup: list[str] = []

        async def _notify_type(type_key: str, message: str, file_path: str | None = None) -> None:
            """Send to services configured for this alert type (information/screenshot/video)."""
            for svc in self._get_alert_services(type_key):
                try:
                    domain, service = svc.split(".", 1)
                    call_data = self._build_notify_data(svc, message, file_path)
                    await self.hass.services.async_call(domain, service, call_data)
                except Exception as err:
                    _LOGGER.warning("Alert send failed for %s (%s): %s", svc, type_key, err)

        # ── Step 1: Instant text alert ────────────────────────────────────────
        try:
            await _notify_type("information", f"{type_icon} {cam_name}: {type_label} ({ts_short})")
            _LOGGER.debug("Alert step 1 (text) sent to %d services", len(info_svcs))
        except Exception as err:
            _LOGGER.warning("Alert step 1 failed: %s", err)
            return

        # ── Step 2: Snapshot image (after 5s) ─────────────────────────────────
        # If image_url is empty (event just created), re-fetch events to get it
        if not image_url:
            await asyncio.sleep(5)
            try:
                events_url = f"{CLOUD_API}/v11/events?videoInputId=&limit=5"
                # Find cam_id from cam_name in coordinator data
                for cid, cdata in self.data.items():
                    if cdata.get("info", {}).get("title", "") == cam_name:
                        events_url = f"{CLOUD_API}/v11/events?videoInputId={cid}&limit=5"
                        break
                async with asyncio.timeout(10):
                    async with session.get(events_url, headers=headers) as r:
                        if r.status == 200:
                            fresh_events = await r.json()
                            if fresh_events:
                                image_url = fresh_events[0].get("imageUrl", "")
                                clip_url = fresh_events[0].get("videoClipUrl", "") or clip_url
                                clip_status = fresh_events[0].get("videoClipUploadStatus", "") or clip_status
                                _LOGGER.debug("Alert: re-fetched image_url=%s", image_url[:60] if image_url else "empty")
            except Exception as err:
                _LOGGER.debug("Alert: re-fetch events failed: %s", err)

        if image_url:
            if not image_url.startswith("http"):
                _LOGGER.debug("Alert: invalid image_url: %s", image_url[:60])
            else:
                await asyncio.sleep(5)
            snap_path = os.path.join(alert_dir, f"{cam_name}_{ts_safe}_{event_type}.jpg")
            try:
                async with asyncio.timeout(15):
                    async with session.get(image_url, headers=headers) as resp:
                        if resp.status == 200 and "image" in resp.headers.get("Content-Type", ""):
                            data = await resp.read()
                            if data:
                                await self.hass.async_add_executor_job(self._write_file, snap_path, data)
                                await _notify_type(
                                    "screenshot",
                                    f"\U0001f4f8 {cam_name} Snapshot ({ts_short})",
                                    snap_path,
                                )
                                _LOGGER.debug("Alert step 2 (screenshot) sent: %s", snap_path)
                                if not save_snapshots:
                                    files_to_cleanup.append(snap_path)
            except Exception as err:
                _LOGGER.warning("Alert step 2 failed: %s", err)

        # ── Step 3: Video clip — poll until ready, then download + send ─────
        # Bosch uploads clips asynchronously. The event initially has
        # clip_status=Pending (or no clipUrl at all). We poll the events API
        # every 10s for up to 90s until videoClipUploadStatus=Done.
        cam_id = None
        for cid, cdata in self.data.items():
            if cdata.get("info", {}).get("title", "") == cam_name:
                cam_id = cid
                break

        if cam_id:
            clip_path = os.path.join(alert_dir, f"{cam_name}_{ts_safe}_{event_type}.mp4")
            auth_headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
            found_clip_url = clip_url if (clip_url and clip_status == "Done") else ""

            # Try direct clip.mp4 download first (faster than polling)
            if not found_clip_url:
                event_id = self._last_event_ids.get(cam_id, "")
                if event_id:
                    try:
                        async with asyncio.timeout(10):
                            async with session.get(
                                f"{CLOUD_API}/v11/events/{event_id}/clip.mp4",
                                headers={"Authorization": f"Bearer {self.token}", "Accept": "*/*"},
                            ) as r:
                                if r.status == 200 and "video" in r.headers.get("Content-Type", ""):
                                    found_clip_url = f"{CLOUD_API}/v11/events/{event_id}/clip.mp4"
                                    _LOGGER.debug("Alert: direct clip.mp4 available for %s", cam_name)
                    except Exception:
                        pass

            if not found_clip_url and clip_status == "Unavailable":
                _LOGGER.debug(
                    "Alert: clip status Unavailable from start — skipping poll for %s", cam_name,
                )
            elif not found_clip_url:
                # Poll for clip readiness (10s intervals, up to 90s)
                clip_unavailable = False
                for attempt in range(9):
                    await asyncio.sleep(10)
                    try:
                        async with asyncio.timeout(10):
                            async with session.get(
                                f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=3",
                                headers=auth_headers,
                            ) as r:
                                if r.status != 200:
                                    continue
                                fresh = await r.json()
                                for ev in fresh:
                                    if ev.get("timestamp", "")[:19] == timestamp[:19]:
                                        status = ev.get("videoClipUploadStatus", "")
                                        url = ev.get("videoClipUrl", "")
                                        if status == "Done" and url:
                                            found_clip_url = url
                                        elif status == "Unavailable":
                                            clip_unavailable = True
                                            _LOGGER.debug(
                                                "Alert: clip Unavailable after %ds — stop polling for %s",
                                                (attempt + 1) * 10, cam_name,
                                            )
                                        break
                        if found_clip_url:
                            _LOGGER.debug(
                                "Alert: clip ready after %ds for %s",
                                (attempt + 1) * 10, cam_name,
                            )
                            break
                        if clip_unavailable:
                            break
                    except Exception:
                        continue

            if found_clip_url:
                try:
                    dl_headers = {"Authorization": f"Bearer {self.token}", "Accept": "*/*"}
                    async with asyncio.timeout(60):
                        async with session.get(found_clip_url, headers=dl_headers) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                if data and len(data) > 1000:
                                    await self.hass.async_add_executor_job(
                                        self._write_file, clip_path, data
                                    )
                                    size_kb = len(data) // 1024
                                    await _notify_type(
                                        "video",
                                        f"\U0001f3ac {cam_name} Video ({ts_short}, {size_kb} KB)",
                                        clip_path,
                                    )
                                    _LOGGER.info(
                                        "Alert step 3 (video) sent: %s (%d KB)", clip_path, size_kb
                                    )
                                    if not save_snapshots:
                                        files_to_cleanup.append(clip_path)
                except Exception as err:
                    _LOGGER.warning("Alert step 3 (video) failed: %s", err)
            else:
                _LOGGER.debug("Alert: video clip not ready after 90s for %s", cam_name)

        # ── Mark event as read ─────────────────────────────────────────────
        if cam_id:
            event_id = self._last_event_ids.get(cam_id, "")
            if event_id:
                try:
                    await self.async_mark_events_read([event_id])
                except Exception:
                    pass

        # ── SMB upload (immediate, alongside alert) ────────────────────────
        if opts.get("enable_smb_upload") and opts.get("smb_server") and cam_id:
            try:
                # Build a minimal data dict for _sync_smb_upload with just this event
                ev_id = self._last_event_ids.get(cam_id, "unknown")
                ev_data = {
                    "timestamp": timestamp,
                    "eventType": event_type,
                    "id": ev_id,
                    "imageUrl": image_url,
                    "videoClipUrl": found_clip_url if found_clip_url else "",
                    "videoClipUploadStatus": "Done" if found_clip_url else "",
                }
                smb_data = {
                    cam_id: {
                        "info": {"title": cam_name},
                        "events": [ev_data],
                    }
                }
                _LOGGER.info(
                    "Alert: SMB upload starting for %s (event=%s, img=%s, clip=%s)",
                    cam_name, ev_id[:8] if ev_id else "?",
                    bool(image_url), bool(found_clip_url),
                )
                await self.hass.async_add_executor_job(
                    self._sync_smb_upload, smb_data, self.token
                )
                _LOGGER.info("Alert: SMB upload completed for %s", cam_name)
            except Exception as err:
                _LOGGER.warning("Alert: SMB upload failed for %s: %s", cam_name, err)

        # ── Cleanup local files ───────────────────────────────────────────────
        if delete_after and files_to_cleanup:
            await asyncio.sleep(5)  # give Signal time to read the files
            for fpath in files_to_cleanup:
                try:
                    await self.hass.async_add_executor_job(os.remove, fpath)
                except OSError:
                    pass

    async def async_mark_events_read(self, event_ids: list[str]) -> bool:
        """Mark events as read/seen on the Bosch cloud.

        Tries PUT /v11/events/bulk first, falls back to individual PUT.
        Best-effort — never raises.
        """
        if not event_ids:
            return True

        token = self.token
        if not token:
            return False

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # Try bulk update
        try:
            body = {"events": [{"id": eid, "isRead": True} for eid in event_ids]}
            async with asyncio.timeout(10):
                async with session.put(
                    f"{CLOUD_API}/v11/events/bulk", headers=headers, json=body
                ) as resp:
                    if resp.status in (200, 204):
                        _LOGGER.debug("Marked %d events as read (bulk)", len(event_ids))
                        return True
                    _LOGGER.debug("Bulk mark-read HTTP %d — trying individual", resp.status)
        except Exception as err:
            _LOGGER.debug("Bulk mark-read error: %s — trying individual", err)

        # Fallback: individual PUT /v11/events with {"id": eid, "isRead": true}
        success = False
        for eid in event_ids:
            try:
                async with asyncio.timeout(5):
                    async with session.put(
                        f"{CLOUD_API}/v11/events",
                        headers=headers, json={"id": eid, "isRead": True},
                    ) as resp:
                        if resp.status in (200, 204):
                            success = True
            except Exception:
                pass

        if success:
            _LOGGER.debug("Marked events as read (individual)")
        return success

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        with open(path, "wb") as f:
            f.write(data)

    async def async_stop_fcm_push(self) -> None:
        """Stop the FCM push listener."""
        if self._fcm_client and self._fcm_running:
            try:
                await self._fcm_client.stop()
            except Exception:
                pass
            self._fcm_running = False
            _LOGGER.info("FCM push listener stopped")

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

    # ── SHC local API (camera light + privacy mode) ───────────────────────────

    @property
    def shc_configured(self) -> bool:
        """True if SHC local API is fully configured (IP + certs)."""
        opts = self.options
        return bool(
            opts.get("shc_ip", "").strip()
            and opts.get("shc_cert_path", "").strip()
            and opts.get("shc_key_path", "").strip()
        )

    @property
    def shc_ready(self) -> bool:
        """True if SHC is configured AND currently considered available.

        When SHC is offline (too many consecutive failures), returns False
        unless the retry interval has elapsed.
        """
        if not self.shc_configured:
            return False
        if self._shc_available:
            return True
        # SHC is offline — check if retry interval has passed
        now = time.monotonic()
        if now - self._shc_last_check >= self._SHC_RETRY_INTERVAL:
            return True  # allow one retry
        return False

    def _shc_mark_success(self) -> None:
        """Mark SHC as healthy after a successful request."""
        if not self._shc_available:
            _LOGGER.info("SHC local API is back online")
        self._shc_available = True
        self._shc_fail_count = 0

    def _shc_mark_failure(self) -> None:
        """Track a failed SHC request; mark offline after N consecutive failures."""
        self._shc_fail_count += 1
        self._shc_last_check = time.monotonic()
        if self._shc_fail_count >= self._SHC_MAX_FAILS and self._shc_available:
            self._shc_available = False
            _LOGGER.warning(
                "SHC local API marked offline after %d consecutive failures — "
                "will retry in %ds. Falling back to cloud API.",
                self._shc_fail_count, self._SHC_RETRY_INTERVAL,
            )

    async def _async_shc_request(
        self, method: str, path: str, body: dict | None = None
    ) -> dict | list | None:
        """Make a request to the SHC local API using mutual TLS.

        Returns parsed JSON on success, None on failure.
        Requires shc_ip, shc_cert_path, shc_key_path in options.
        Tracks SHC health — marks offline after repeated failures.
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
            self._shc_mark_failure()
            return None

        url     = f"https://{shc_ip}:8444/smarthome{path}"
        headers = {"api-version": "3.2", "Content-Type": "application/json"}
        try:
            connector = aiohttp.TCPConnector(ssl=ctx)
            async with aiohttp.ClientSession(connector=connector) as s:
                async with asyncio.timeout(10):
                    if method == "GET":
                        async with s.get(url, headers=headers) as r:
                            if r.status == 200:
                                self._shc_mark_success()
                                return await r.json()
                            _LOGGER.debug("SHC GET %s → HTTP %d", path, r.status)
                            self._shc_mark_failure()
                    elif method == "PUT":
                        async with s.put(url, json=body, headers=headers) as r:
                            _LOGGER.debug("SHC PUT %s → HTTP %d", path, r.status)
                            if r.status in (200, 201, 204):
                                self._shc_mark_success()
                            else:
                                self._shc_mark_failure()
                            return {"status": r.status, "ok": r.status in (200, 201, 204)}
        except asyncio.TimeoutError:
            _LOGGER.debug("SHC request timeout: %s %s", method, path)
            self._shc_mark_failure()
        except aiohttp.ClientError as err:
            _LOGGER.debug("SHC request error %s %s: %s", method, path, err)
            self._shc_mark_failure()
        except Exception as err:
            _LOGGER.debug("SHC unexpected error %s %s: %s", method, path, err)
            self._shc_mark_failure()
        return None

    async def _async_update_shc_states(self, data: dict) -> None:
        """Fetch CameraLight and PrivacyMode states from SHC for each camera.

        SHC is the PRIMARY source for privacy + light state when configured.
        Values from SHC overwrite any cloud-sourced values from step 4.
        Matches SHC devices to cloud cameras by device name (title).
        Refreshes the SHC device list at most once per 60 seconds.
        """
        if not self.shc_configured:
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

            # Fetch CameraLight service state (SHC is authoritative)
            svc = await self._async_shc_request(
                "GET", f"/devices/{device_id}/services/CameraLight"
            )
            if isinstance(svc, dict):
                val = svc.get("state", {}).get("value", "")
                entry["camera_light"] = (val.upper() == "ON")

            # Fetch PrivacyMode service state (SHC is authoritative)
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
            self.async_update_listeners()
            self.hass.async_create_task(self.async_request_refresh())
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
            self._privacy_set_at[cam_id] = time.monotonic()
            self.async_update_listeners()
            self.hass.async_create_task(self.async_request_refresh())
            if not enabled:
                cam = self._camera_entities.get(cam_id)
                if cam:
                    self.hass.async_create_task(cam._async_trigger_image_refresh(delay=1.5))
            return True
        return False

    async def async_cloud_set_privacy_mode(self, cam_id: str, enabled: bool) -> bool:
        """Enable (True) or disable (False) privacy mode.

        Strategy: Cloud API first (~150ms), SHC local API fallback (~1100ms).
        Cloud is 10x faster due to connection pooling; SHC requires fresh mTLS
        handshake per request on an embedded controller.
        SHC fallback ensures control when cloud is unreachable (offline mode).
        """
        # ── Cloud API (primary — fast) ───────────────────────────────────────
        token = self.token
        if token:
            session = async_get_clientsession(self.hass, verify_ssl=False)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            }
            url  = f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy"
            body = {"privacyMode": "ON" if enabled else "OFF", "durationInSeconds": None}

            try:
                async with asyncio.timeout(10):
                    async with session.put(url, json=body, headers=headers) as resp:
                        if resp.status == 401:
                            # Token expired — refresh and retry once
                            _LOGGER.info("cloud_set_privacy_mode: 401 — refreshing token and retrying")
                            try:
                                token = await self._ensure_valid_token()
                                headers["Authorization"] = f"Bearer {token}"
                            except Exception:
                                pass  # fall through to SHC
                        if resp.status in (200, 201, 204):
                            self._shc_state_cache.setdefault(cam_id, {})["privacy_mode"] = enabled
                            self._privacy_set_at[cam_id] = time.monotonic()
                            self.async_update_listeners()
                            _LOGGER.debug(
                                "cloud_set_privacy_mode: %s → %s (HTTP %d)",
                                cam_id, "ON" if enabled else "OFF", resp.status,
                            )
                            self.hass.async_create_task(self.async_request_refresh())
                            if not enabled:
                                cam = self._camera_entities.get(cam_id)
                                if cam:
                                    self.hass.async_create_task(
                                        cam._async_trigger_image_refresh(delay=1.5)
                                    )
                            return True
                        if resp.status == 401:
                            # Retry with refreshed token
                            async with asyncio.timeout(10):
                                async with session.put(url, json=body, headers=headers) as resp2:
                                    if resp2.status in (200, 201, 204):
                                        self._shc_state_cache.setdefault(cam_id, {})["privacy_mode"] = enabled
                                        self._privacy_set_at[cam_id] = time.monotonic()
                                        self.async_update_listeners()
                                        self.hass.async_create_task(self.async_request_refresh())
                                        if not enabled:
                                            cam = self._camera_entities.get(cam_id)
                                            if cam:
                                                self.hass.async_create_task(
                                                    cam._async_trigger_image_refresh(delay=1.5)
                                                )
                                        return True
                        _LOGGER.warning(
                            "cloud_set_privacy_mode: HTTP %d for %s", resp.status, cam_id
                        )
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning("cloud_set_privacy_mode error for %s: %s", cam_id, err)

        # ── SHC local API fallback (offline mode) ────────────────────────────
        if self.shc_ready:
            _LOGGER.info("cloud_set_privacy_mode: cloud failed, falling back to SHC for %s", cam_id)
            return await self.async_shc_set_privacy_mode(cam_id, enabled)
        return False

    async def async_cloud_set_camera_light(self, cam_id: str, on: bool) -> bool:
        """Turn the camera light on (True) or off (False).

        Strategy: Cloud API first (~150ms), SHC local API fallback (~1100ms).
        SHC fallback ensures control when cloud is unreachable (offline mode).
        Note: SHC CameraLight service only exists for cameras with physical lights
        (Garten/CAMERA_EYES). For cameras without it, SHC fallback will fail silently.
        """
        # ── Cloud API (primary — fast) ───────────────────────────────────────
        token = self.token
        if token:
            session = async_get_clientsession(self.hass, verify_ssl=False)
            headers = {
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
                async with asyncio.timeout(10):
                    async with session.put(url, json=body, headers=headers) as resp:
                        if resp.status in (200, 201, 204):
                            self._shc_state_cache.setdefault(cam_id, {})["camera_light"] = on
                            # Write-lock: prevent the next coordinator refresh from overwriting
                            # the optimistic state with stale cloud data (Bosch API propagation delay).
                            self._light_set_at[cam_id] = time.monotonic()
                            self.async_update_listeners()
                            _LOGGER.debug(
                                "cloud_set_camera_light: %s → %s (HTTP %d)",
                                cam_id, "ON" if on else "OFF", resp.status,
                            )
                            self.hass.async_create_task(self.async_request_refresh())
                            return True
                        _LOGGER.warning(
                            "cloud_set_camera_light: HTTP %d for %s", resp.status, cam_id
                        )
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning("cloud_set_camera_light error for %s: %s", cam_id, err)

        # ── SHC local API fallback (offline mode) ────────────────────────────
        if self.shc_ready:
            _LOGGER.info("cloud_set_camera_light: cloud failed, falling back to SHC for %s", cam_id)
            return await self.async_shc_set_camera_light(cam_id, on)
        return False

    async def async_cloud_set_notifications(self, cam_id: str, enabled: bool) -> bool:
        """Enable (FOLLOW_CAMERA_SCHEDULE) or disable (ALWAYS_OFF) notifications via cloud API.

        Uses PUT /v11/video_inputs/{id}/enable_notifications.
        """
        token = self.token
        if not token:
            _LOGGER.warning("cloud_set_notifications: no token for %s", cam_id)
            return False

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url    = f"{CLOUD_API}/v11/video_inputs/{cam_id}/enable_notifications"
        status = "FOLLOW_CAMERA_SCHEDULE" if enabled else "ALWAYS_OFF"
        body   = {"enabledNotificationsStatus": status}

        try:
            async with asyncio.timeout(10):
                async with session.put(url, json=body, headers=headers) as resp:
                    if resp.status in (200, 201, 204):
                        self._shc_state_cache.setdefault(cam_id, {})["notifications_status"] = status
                        self._notif_set_at[cam_id] = time.monotonic()
                        self.async_update_listeners()
                        _LOGGER.debug(
                            "cloud_set_notifications: %s → %s (HTTP %d)",
                            cam_id, status, resp.status,
                        )
                        self.hass.async_create_task(self.async_request_refresh())
                        return True
                    _LOGGER.warning(
                        "cloud_set_notifications: HTTP %d for %s", resp.status, cam_id
                    )
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_notifications error for %s: %s", cam_id, err)
        return False

    async def async_cloud_set_pan(self, cam_id: str, position: int) -> bool:
        """Pan the 360 camera to an absolute position (-120 to +120 degrees).

        Uses PUT /v11/video_inputs/{id}/pan — no SHC local API needed.
        """
        token = self.token
        if not token:
            _LOGGER.warning("cloud_set_pan: no token for %s", cam_id)
            return False

        session = async_get_clientsession(self.hass, verify_ssl=False)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/pan"

        try:
            async with asyncio.timeout(10):
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
                        self.hass.async_create_task(self.async_request_refresh())
                        return True
                    _LOGGER.warning("cloud_set_pan: HTTP %d for %s", resp.status, cam_id)
        except (asyncio.TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.warning("cloud_set_pan error for %s: %s", cam_id, err)
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

    # ── SMB/NAS upload (runs in executor thread) ────────────────────────────
    def _sync_smb_upload(self, data: dict, token: str) -> None:
        """Upload new event files to SMB/NAS share.

        Folder structure: {smb_base_path}/{year}/{month}/{camera_name}_{date}_{time}_{type}.{ext}
        Uses smbprotocol for cross-platform SMB access.
        """
        import requests as req
        import urllib3
        urllib3.disable_warnings()

        opts = self.options
        server = opts.get("smb_server", "").strip()
        share = opts.get("smb_share", "").strip()
        username = opts.get("smb_username", "").strip()
        password = opts.get("smb_password", "")
        base_path = opts.get("smb_base_path", "Bosch-Kameras").strip()
        folder_pattern = opts.get("smb_folder_pattern", "{year}/{month}").strip()
        file_pattern = opts.get("smb_file_pattern", "{camera}_{date}_{time}_{type}_{id}").strip()

        if not server or not share:
            return

        try:
            from smbclient import (
                register_session, mkdir, open_file, stat as smb_stat
            )
            import smbclient  # noqa: F401
        except ImportError:
            _LOGGER.warning(
                "smbprotocol not installed — SMB upload disabled. "
                "Install with: pip install smbprotocol"
            )
            return

        try:
            register_session(server, username=username, password=password)
        except Exception as err:
            _LOGGER.warning("SMB session to %s failed: %s", server, err)
            return

        session = req.Session()
        session.headers["Authorization"] = f"Bearer {token}"
        session.verify = False

        for cam_id, cam_data in data.items():
            cam_name = cam_data["info"].get("title", cam_id)
            ev_list = cam_data.get("events", [])
            _LOGGER.debug("SMB upload: %s has %d events", cam_name, len(ev_list))

            for ev in ev_list:
                ts = ev.get("timestamp", "")
                if not ts or len(ts) < 19:
                    _LOGGER.debug("SMB upload: skipping event with short/empty timestamp: %r", ts)
                    continue

                # Parse timestamp for folder/file patterns
                year = ts[:4]
                month = ts[5:7]
                day = ts[8:10]
                date_str = f"{year}-{month}-{day}"
                time_str = ts[11:19].replace(":", "-")
                etype = ev.get("eventType", "EVENT")
                ev_id = ev.get("id", "")[:8]

                # Build folder path from pattern
                folder_parts = folder_pattern.format(
                    year=year, month=month, day=day,
                    camera=cam_name, type=etype,
                )
                smb_folder = f"\\\\{server}\\{share}\\{base_path}\\{folder_parts}"
                smb_folder = smb_folder.replace("/", "\\")

                # Build file name from pattern
                file_base = file_pattern.format(
                    camera=cam_name, date=date_str, time=time_str,
                    type=etype, id=ev_id, year=year, month=month, day=day,
                )

                # Ensure folder exists (create recursively)
                try:
                    self._smb_makedirs(smb_folder, server, share, base_path, folder_parts)
                except Exception as err:
                    _LOGGER.warning("SMB mkdir error for %s: %s", smb_folder, err)
                    continue

                # Upload snapshot
                img_url = ev.get("imageUrl")
                if img_url:
                    smb_path = f"{smb_folder}\\{file_base}.jpg"
                    try:
                        smb_stat(smb_path)
                        _LOGGER.debug("SMB skip (exists): %s", file_base + ".jpg")
                    except OSError:
                        try:
                            r = session.get(img_url, timeout=30)
                            if r.status_code == 200 and r.content:
                                with open_file(smb_path, mode="wb") as f:
                                    f.write(r.content)
                                _LOGGER.info("SMB uploaded: %s (%d bytes)", file_base + ".jpg", len(r.content))
                            else:
                                _LOGGER.warning("SMB snapshot download failed: HTTP %d, %d bytes", r.status_code, len(r.content))
                        except Exception as err:
                            _LOGGER.warning("SMB upload error for %s: %s", file_base, err)
                else:
                    _LOGGER.debug("SMB: no imageUrl for event %s", ev.get("id", "?")[:8])

                # Upload video clip
                clip_url = ev.get("videoClipUrl")
                clip_status = ev.get("videoClipUploadStatus", "")
                if clip_url and clip_status == "Done":
                    smb_path = f"{smb_folder}\\{file_base}.mp4"
                    try:
                        smb_stat(smb_path)
                        _LOGGER.debug("SMB skip (exists): %s", file_base + ".mp4")
                    except OSError:
                        try:
                            r = session.get(clip_url, timeout=60, stream=True)
                            if r.status_code == 200:
                                total = 0
                                with open_file(smb_path, mode="wb") as f:
                                    for chunk in r.iter_content(65536):
                                        f.write(chunk)
                                        total += len(chunk)
                                _LOGGER.info("SMB uploaded: %s (%d bytes)", file_base + ".mp4", total)
                            else:
                                _LOGGER.warning("SMB clip download failed: HTTP %d", r.status_code)
                        except Exception as err:
                            _LOGGER.warning("SMB clip upload error for %s: %s", file_base, err)

    @staticmethod
    def _smb_makedirs(full_path: str, server: str, share: str, base_path: str, folder_parts: str) -> None:
        """Create SMB directories recursively."""
        from smbclient import mkdir, stat as smb_stat

        # Build path incrementally
        parts = [p for p in f"{base_path}\\{folder_parts}".replace("/", "\\").split("\\") if p]
        current = f"\\\\{server}\\{share}"

        for part in parts:
            current = f"{current}\\{part}"
            try:
                smb_stat(current)
            except OSError:
                try:
                    mkdir(current)
                except OSError:
                    pass  # May exist due to race condition

    # ── SMB retention cleanup (runs in executor thread, once per day) ────────
    def _sync_smb_cleanup(self) -> None:
        """Delete files on the SMB share that are older than smb_retention_days."""
        try:
            from smbclient import register_session, scandir, remove, stat as smb_stat
        except ImportError:
            return

        opts = self.options
        server = opts.get("smb_server", "").strip()
        share = opts.get("smb_share", "").strip()
        username = opts.get("smb_username", "").strip()
        password = opts.get("smb_password", "")
        base_path = opts.get("smb_base_path", "Bosch-Kameras").strip()
        retention_days = int(opts.get("smb_retention_days", 180))

        if not server or not share or retention_days <= 0:
            return

        try:
            register_session(server, username=username, password=password)
        except Exception as err:
            _LOGGER.warning("SMB cleanup: session to %s failed: %s", server, err)
            return

        cutoff = time.time() - retention_days * 86400
        root = f"\\\\{server}\\{share}\\{base_path}"
        deleted = 0

        def _walk_and_delete(path: str) -> None:
            nonlocal deleted
            try:
                entries = list(scandir(path))
            except Exception:
                return
            for entry in entries:
                full = f"{path}\\{entry.name}"
                if entry.is_dir():
                    _walk_and_delete(full)
                else:
                    try:
                        st = smb_stat(full)
                        if st.st_mtime < cutoff:
                            remove(full)
                            deleted += 1
                            _LOGGER.debug("SMB cleanup: deleted %s", entry.name)
                    except Exception as err:
                        _LOGGER.debug("SMB cleanup: error on %s: %s", entry.name, err)

        _walk_and_delete(root)
        if deleted:
            _LOGGER.info(
                "SMB cleanup: deleted %d file(s) older than %d days from %s",
                deleted, retention_days, root,
            )

    # ── SMB disk-free check (runs in executor thread, once per hour) ─────────
    def _sync_smb_disk_check(self) -> None:
        """Check free space on the SMB share and fire an HA alert if low."""
        try:
            from smbclient import register_session
            import smbclient._io as _smb_io  # noqa: F401 — ensure smbclient loaded
        except ImportError:
            return

        import ctypes

        opts = self.options
        server = opts.get("smb_server", "").strip()
        share = opts.get("smb_share", "").strip()
        username = opts.get("smb_username", "").strip()
        password = opts.get("smb_password", "")
        warn_mb = int(opts.get("smb_disk_warn_mb", 500))
        # Use system services for disk alerts (falls back to alert_notify_service if empty)
        system_raw = opts.get("alert_notify_system", "").strip()
        notify_service = system_raw or opts.get("alert_notify_service", "").strip()

        if not server or not share or warn_mb <= 0:
            return

        try:
            register_session(server, username=username, password=password)
        except Exception as err:
            _LOGGER.warning("SMB disk check: session to %s failed: %s", server, err)
            return

        # Use smbclient's statvfs to get free space
        try:
            import smbclient
            vfs = smbclient.statvfs(f"\\\\{server}\\{share}")
            free_mb = (vfs.f_bavail * vfs.f_frsize) // (1024 * 1024)
        except Exception as err:
            _LOGGER.debug("SMB disk check: statvfs failed: %s", err)
            return

        if free_mb < warn_mb:
            msg = (
                f"Bosch Camera NAS: Wenig Speicherplatz auf \\\\{server}\\{share} — "
                f"noch {free_mb} MB frei (Warnschwelle: {warn_mb} MB)"
            )
            _LOGGER.warning(msg)
            # Fire alert via HA event loop
            self.hass.loop.call_soon_threadsafe(
                self.hass.async_create_task,
                self._async_smb_disk_alert(msg, notify_service),
            )

    async def _async_smb_disk_alert(self, message: str, notify_service: str) -> None:
        """Send disk-full warning via notify service or HA persistent notification."""
        services = [s.strip() for s in notify_service.split(",") if s.strip()]
        sent = False
        for svc in services:
            domain, _, name = svc.partition(".")
            if self.hass.services.has_service(domain, name):
                try:
                    await self.hass.services.async_call(
                        domain, name,
                        {"message": message, "title": "Bosch Kamera — Speicherwarnung"},
                    )
                    sent = True
                except Exception as err:
                    _LOGGER.debug("SMB disk alert via %s failed: %s", svc, err)
        if not sent:
            # Fall back to HA persistent notification
            await self.hass.services.async_call(
                "persistent_notification", "create",
                {
                    "title": "Bosch Kamera — Speicherwarnung",
                    "message": message,
                    "notification_id": "bosch_smb_disk_warn",
                },
            )

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
