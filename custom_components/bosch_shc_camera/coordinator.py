"""BoschCameraCoordinator — the shared DataUpdateCoordinator subclass.

Matches Core/reolink convention: `__init__.py` handles only config-entry
setup/unload/migrate/platform-forwarding/service-registration, while the
coordinator class itself lives in its own module and delegates to sibling
free-function modules (stream_lifecycle, session_renewal, go2rtc_client,
tls_proxy_wiring, slow_tier, tick_bootstrap, tick_housekeeping, tick_failure,
camera_status, event_polling, event_dispatch, fcm, shc, rcp, smb, etc.).
"""

import asyncio
import ipaddress
import logging
import ssl
import threading
import time
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
from bosch_shc_camera_client.media_transfer import (
    is_safe_bosch_url as _lib_is_safe_bosch_url,
)

if TYPE_CHECKING:
    from .maintenance import MaintenanceWindow

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import announcements, device_actions, quality_prefs, repairs
from . import recorder as nvr_recorder
from .camera_list import fetch_camera_list
from .camera_status import poll_statuses
from .cloud_ssl import async_bosch_cloud_session_cm
from .const import (
    CLOUD_API,
    DEFAULT_OPTIONS,
    DOMAIN,
    JPEG_SIZE_FULL,
    RCP_099E_PROBE_FAILURE_MEMO_SEC,
    SHC_MAX_FAILS,
    SHC_RETRY_INTERVAL,
    STREAM_START_SKIPPED,
    TIMEOUT_PUT_CONNECTION,
    TIMEOUT_RCP_099E_PROBE,
    TIMEOUT_SNAP,
)
from .event_dispatch import build_data_and_dispatch
from .event_polling import poll_events
from .fcm import (
    FCMCoordinatorMixin,
)
from .fcm import (
    async_ensure_fcm_supervisor as _fcm_async_ensure_supervisor,
)
from .frigate_endpoint import (
    FrigateCoordinatorMixin,
    FrontDoorRunner,
)
from .go2rtc_client import (
    ensure_go2rtc_schemes_fresh,
    unregister_go2rtc_stream,
)
from .live_connection import try_live_connection_inner
from .lock_utils import get_or_create_lock
from .rcp import async_update_rcp_data
from .remote_viewing_front_door import (
    start_remote_viewing_front_door,
    stop_remote_viewing_front_door,
)
from .session_renewal import (
    auto_renew_local_session,
    promote_to_local,
    refresh_local_creds_from_heartbeat,
    remote_session_terminator,
)
from .session_state import (
    BoolFieldView,
    CacheFieldView,
    CameraSessionState,
    FloatFieldView,
    LiveOpenedAtView,
    StreamWarmingView,
    get_or_create_session,
)
from .shc import SHCCoordinatorMixin
from .slow_tier import (
    _compute_cam_context,
    _poll_cam_control,
    _poll_cam_info_caches,
    _poll_slow_tier_endpoints,
)
from .smb import sync_smb_cleanup
from .stream_lifecycle import (
    go2rtc_consumer_count,
    handle_stream_worker_error,
    has_active_consumer,
    idle_session_reaper,
    schedule_stream_worker_error,
    tear_down_live_stream,
)
from .tick_bootstrap import ensure_feature_flags, ensure_protocol_checked
from .tick_failure import (
    dispatch_client_error,
    dispatch_timeout,
    dispatch_update_failed,
)
from .tick_housekeeping import run_housekeeping
from .tls_proxy_wiring import (
    create_ssl_ctx,
    on_tls_proxy_died,
    start_tls_proxy_wiring,
    stop_tls_proxy_wiring,
)
from .token_auth import TokenAuthCoordinatorMixin
from .viewing_front_door import (
    start_viewing_front_door,
    stop_viewing_front_door,
)

_LOGGER = logging.getLogger(__name__)

# Coalesce concurrent async_fetch_fresh_event_snapshot calls for the same camera.
# After an FCM push all HA consumers wake simultaneously and each requests the latest
# event thumbnail. 8 s covers the burst window; the 60 s scan cycle always gets fresh data.
_FRESH_SNAP_TTL = 8.0

# Event-poll cadence while FCM push is NOT delivering (disabled, or watchdog
# flagged unhealthy). The relaxed `interval_events` (default 300 s) assumes
# push carries the near-instant detection and the poll is only a safety net —
# but with push dead the poll IS the detection path, and a 300 s poll behind a
# 90 s motion window means a polled event is already older than the window the
# moment it lands, so the binary sensor can never turn ON. When
# push is not delivering we therefore poll at this fast cadence instead — bounded
# below the smallest motion window (MOTION_ACTIVE_WINDOW_MIN/DEFAULT) so a
# polled event is always seen while still "fresh". A user who explicitly set a
# lower `interval_events` keeps it (min() below).
FCM_DOWN_EVENT_POLL_SEC = 60.0

# Grace before a camera's online→offline transition is ANNOUNCED (push/notify).
# Cameras on a Wi-Fi repeater/mesh briefly drop during a repeater restart or a
# DFS channel change and recover within a minute or two; firing an "offline /
# live + snapshots unavailable" notification on the first failed status check is
# noise. Only announce offline once the camera has stayed offline continuously
# for this long. A recovery within the window produces no notification at all.
# The camera ENTITY availability still flips immediately — only the notification
# is debounced.
CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC = 300.0  # 5 min

# Read integration version once at import time (sync I/O at module level is fine — import
# happens in the executor during HA startup, not inside the event loop).
# Duplicated from __init__.py (both modules need it — __init__.py for the
# feedback-hint/persistent_notification code in async_setup_entry, this module
# for BoschCameraCoordinator.__init__'s self.integration_version — this is a
# self-contained leaf computation, not worth a shared-module round-trip for).
try:
    import json as _json
    import pathlib as _pathlib

    _INTEGRATION_VERSION: str = _json.loads(
        (_pathlib.Path(__file__).parent / "manifest.json").read_text()
    )["version"]
except Exception:  # pragma: no cover — manifest.json ships with the package; only fires on a corrupted install
    _INTEGRATION_VERSION = "unknown"

# ── URL allowlist for image/video downloads (SSRF prevention) ────────────────
_SAFE_DOMAINS = frozenset({".boschsecurity.com", ".bosch.com"})

# `_is_safe_bosch_url` was previously a byte-identical copy duplicated across
# fcm.py/smb.py/coordinator.py — now a single shared implementation in
# bosch_shc_camera_client.media_transfer, aliased here to keep the private
# name every call site in this file already uses. `_SAFE_DOMAINS` stays
# local since `_is_safe_bosch_host` below still uses it directly.
_is_safe_bosch_url = _lib_is_safe_bosch_url


def _is_safe_bosch_host(host_and_port: str) -> bool:
    """Validate a bare ``host[:port]`` string (no scheme) against the Bosch allowlist.

    Used for the RCP proxy host/hash pair Bosch's cloud PUT /connection
    response hands back (e.g. "proxy-01.live.cbs.boschsecurity.com:42090")
    before it is used to build a request URL for the RCP client library —
    an unvalidated value here is an SSRF path. Parsed via ``urlparse`` (not a
    naive ``rsplit(":", 1)``) so this extracts the same authority a real
    HTTP client would connect to — a value like
    "proxy.boschsecurity.com:443@attacker.example" splits to an
    allowlisted-looking "proxy.boschsecurity.com" on the last colon, but an
    HTTP client parses it as userinfo and connects to attacker.example.
    Any "@" is rejected outright (a legitimate Bosch value never has one;
    aiohttp would otherwise turn userinfo into a Basic-Auth header sent to
    Bosch's real proxy), and a ``ValueError`` from ``urlparse`` on malformed
    input fails closed rather than propagating past callers' narrower
    exception handlers.
    """
    if "@" in host_and_port:
        return False
    try:
        hostname = urlparse(f"https://{host_and_port}").hostname
    except ValueError:
        return False
    return hostname is not None and any(hostname.endswith(d) for d in _SAFE_DOMAINS)


def _parse_safe_rcp_proxy_url(url_entry: str, cam_id: str) -> tuple[str, str] | None:
    """Split a Bosch ``urls[0]`` proxy entry into ``(host, hash)``, validated.

    Returns None (logging a warning) for a malformed entry or one whose host
    fails `_is_safe_bosch_host` — never hands back an unvalidated host to a
    caller that will use it to build a request URL.
    """
    parts = url_entry.split("/", 1)
    if len(parts) != 2 or not _is_safe_bosch_host(parts[0]):
        _LOGGER.warning(
            "Rejected unsafe/malformed RCP proxy entry for %s: %s",
            cam_id,
            url_entry[:60],
        )
        return None
    return parts[0], parts[1]


def _is_safe_local_camera_host(host_and_port: str) -> bool:
    """Validate a Bosch-issued LOCAL camera ``host:port`` before use.

    Unlike the RCP proxy host (validated against a Bosch-domain allowlist,
    since that value legitimately points at Bosch's own cloud
    infrastructure), a LOCAL session's host is expected to be the physical
    camera's own private LAN address. Accepting an arbitrary/public host
    here would let a compromised or malicious PUT /connection response
    redirect the snapshot request — made with TLS verification disabled —
    to an arbitrary host, and that same host is cached for later outage
    fallback, extending the exposure window. Link-local, loopback, and
    unspecified addresses are explicitly excluded even though Python's
    `is_private` counts all three as private — 169.254.169.254 is the
    well-known cloud-metadata SSRF target, 127.0.0.1/0.0.0.0 would make
    Home Assistant connect back to itself, and a physical camera's LOCAL
    address is never any of these in practice.
    """
    host, _, port_str = host_and_port.partition(":")
    if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        and not addr.is_link_local
        and not addr.is_loopback
        and not addr.is_unspecified
    )


def _parse_onvif_scopes(raw: bytes) -> dict[str, Any]:
    """Parse ONVIF scope TLV payload from RCP 0x0a98 (ASCII, ~720 bytes).

    The payload is a series of null-terminated ASCII strings, each of which
    may be an ONVIF scope URI of the form:
        onvif://www.onvif.org/name/Bosch%20Smart%20Home%20Camera
        onvif://www.onvif.org/hardware/HOME_Eyes_Outdoor
        onvif://www.onvif.org/Profile/Streaming

    Returns a dict with parsed fields and the raw scope list:
        {
            "raw_scopes": [...],
            "name": "Bosch Smart Home Camera",
            "hardware": "HOME_Eyes_Outdoor",
            "profiles": ["Streaming", ...],
            "supported": True,
        }

    Returns {"supported": True, "raw_scopes": [], "name": "", "hardware": "", "profiles": []}
    on parse error (non-None raw means camera answered, so ONVIF is supported).
    """
    import re as _re_onvif
    from urllib.parse import unquote as _unquote

    result: dict[str, Any] = {
        "supported": True,
        "raw_scopes": [],
        "name": "",
        "hardware": "",
        "profiles": [],
    }
    try:
        # Null-terminated or newline-separated ASCII strings
        text = raw.decode("ascii", errors="replace")
        # Split on null bytes, newlines, or whitespace runs
        scopes = [s.strip() for s in _re_onvif.split(r"[\x00\n\r]+", text) if s.strip()]
        result["raw_scopes"] = scopes
        for scope in scopes:
            if not scope.startswith("onvif://www.onvif.org/"):
                continue
            path = scope[len("onvif://www.onvif.org/") :]
            if "/" not in path:
                continue
            key, _, val = path.partition("/")
            val_decoded = _unquote(val).replace("+", " ")
            if key == "name":
                result["name"] = val_decoded
            elif key == "hardware":
                result["hardware"] = val_decoded
            elif key == "Profile":
                profiles: list[str] = result["profiles"]
                profiles.append(val_decoded)
    except Exception:  # noqa: S110 # pragma: no cover — defensive parse of raw camera bytes; partial result still returned
        pass
    return result


def get_options(entry: ConfigEntry) -> dict[str, Any]:
    """Return entry options merged with defaults."""
    opts: dict[str, Any] = dict(DEFAULT_OPTIONS)
    opts.update(entry.options)
    return opts


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraCoordinator(
    DataUpdateCoordinator,  # type: ignore[misc]
    FCMCoordinatorMixin,
    FrigateCoordinatorMixin,
    SHCCoordinatorMixin,
    TokenAuthCoordinatorMixin,
):
    """
    Shared coordinator — fetches all camera data once per scan_interval.
    All entity types (camera, sensor, button) read from coordinator.data
    rather than making independent API calls.
    """

    # How long a (cam_id, opcode_hex) entry stays in the RCP-LAN denied cache
    # after a 401. 24 h is short enough that a real permission grant recovers
    # the same day, long enough that a wrong CBS user does not respawn log
    # noise every 5 min.
    _RCP_LAN_DENIED_TTL: float = 86400.0

    # SHC local-API circuit-breaker thresholds, mirrored from const.py.
    # Exposed as class attrs so shc.py + existing tests can read them as
    # `coordinator.SHC_MAX_FAILS` without per-instance assignment.
    SHC_MAX_FAILS: int = SHC_MAX_FAILS
    SHC_RETRY_INTERVAL: int = SHC_RETRY_INTERVAL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        # Advanced diagnostic escape hatch (set via the manual-login/relogin
        # "Advanced" field) — NEVER defaulted to any specific host. Only ever
        # non-empty if a user explicitly typed a Bosch-confirmed alternate
        # camera-API base URL in to test whether their account is registered
        # there instead of production. Deliberately narrow-scope (CAUTION:
        # never expand this to redirect real camera-cloud traffic — the
        # point is to probe ONE endpoint, not to run the integration
        # against non-production Bosch infrastructure): only
        # `camera_list.fetch_camera_list` reads `self._cloud_api` — every
        # other request (status/events/snapshots/RCP/writes) always uses
        # the module-level `CLOUD_API`, on purpose.
        cloud_api_override = entry.data.get("cloud_api_override", "")
        # Re-validated here (not just at config-flow input time) against the
        # same Bosch-domain allowlist as image/video URLs — every request
        # built from `self._cloud_api` attaches the real bearer token, so a
        # stale/tampered entry.data value could otherwise exfiltrate it to
        # an arbitrary host.
        if cloud_api_override and not _is_safe_bosch_url(cloud_api_override):
            _LOGGER.warning(
                "Ignoring cloud_api_override %s — not a recognized Bosch "
                "domain, falling back to the default camera API",
                cloud_api_override,
            )
            cloud_api_override = ""
        self._cloud_api = cloud_api_override or CLOUD_API
        if cloud_api_override:
            _LOGGER.warning(
                "Using diagnostic camera-API override %s for the initial "
                "camera-list probe only (not for status/events/snapshots/"
                "writes, which always use the production API) — this "
                "should only be set for troubleshooting a specific account "
                "issue with Bosch support's guidance",
                cloud_api_override,
            )
        opts = get_options(entry)
        # Snapshot of options at coordinator creation — used by _async_options_updated
        # to distinguish real options edits from data-only updates (e.g. token refresh).
        # Must be a deep-ish copy so later entry.options mutations don't silently update it.
        self._options_snapshot: dict[str, Any] = dict(opts)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=int(opts.get("scan_interval", 60))),
        )
        # Per-camera session bookkeeping (generation counter for the TOCTOU
        # guard, idle-reaper/stream-warmup timestamps, warming flag) — see
        # session_state.py. Declared before live_opened_at/stream_warming
        # below since those are thin facades backed by this same dict.
        # Accessed via get_session().
        self._sessions: dict[str, CameraSessionState] = {}
        # Live-stream proxy info — keyed by cam_id, cleared after LIVE_SESSION_TTL seconds
        # Session-State-Facade Slice 3: CacheFieldView over self._sessions
        # (see session_state.py) — preserves the exact `dict[str, dict[str,
        # Any]]` get/setitem/pop/in contract external callers use.
        self.live_connections: CacheFieldView[dict[str, Any]] = CacheFieldView(
            self._sessions, "live_connection"
        )
        # timestamp when session was opened — dict-like facade over
        # CameraSessionState.opened_at (external readers in camera.py use
        # .get()/.pop(), preserved via LiveOpenedAtView; see session_state.py)
        self.live_opened_at = LiveOpenedAtView(self._sessions)
        # Local-RCP+ state cache: per-cam {"privacy_mode": bool, "led_dimmer": int, "fetched_at": float, "source": "local"|"remote"}
        # Refreshed opportunistically after each successful PUT /connection.
        # Used as a refinement source for SHC-cache values when SHC is offline /
        # not configured. Persists past session-end (last-known is better than nothing).
        # Session-State-Facade Slice 2: CacheFieldView over self._sessions
        # (see session_state.py) — preserves the exact `dict[str, dict[str,
        # Any]]` get/setitem/pop/in/len contract external callers use.
        self._rcp_state_cache: CacheFieldView[dict[str, Any]] = CacheFieldView(
            self._sessions, "rcp_state_cache"
        )
        # In-memory stream type override — changed by BoschStreamModeSwitch without reload.
        # None = use options setting; "local" / "auto" / "remote" = override.
        self.stream_type_override: str | None = None
        # Per-camera audio setting — True = audio+video on (default), False = snapshot-only
        self.audio_enabled: dict[str, bool] = {}
        # Per-camera card playback volume 0-100 — the automatable, cross-session
        # source of truth the Lovelace card applies to its <video> (browser has
        # no backend volume knob; this is a virtual preference). Mirrors the
        # audio_enabled pattern: in-memory, seeded to a default per camera.
        self.audio_volume: dict[str, int] = {}
        # Auto-renewal tasks and generation counters per camera.
        # The generation counter increments on every new stream start,
        # allowing stale renewal loops to detect they belong to an old session.
        # Legacy task dict — kept for backwards-compat with any external code
        # that inspects it, but never populated now (use renewal_tasks).
        self._auto_renew_tasks: dict[str, asyncio.Task[None]] = {}
        self.renewal_tasks: dict[str, asyncio.Task[None]] = {}
        # Idle-session reaper tasks (one per LOCAL session, generation-tracked
        # like renewal_tasks). See idle_session_reaper.
        self.reaper_tasks: dict[str, asyncio.Task[None]] = {}
        # Camera entity references — registered on entity setup, used by button/service
        self.camera_entities: dict[str, Any] = {}
        # Diagnostic counter — how many times a stale HA Stream's
        # `.stop()` has timed out (5s) for this cam_id, leaving its
        # underlying `stream_worker` thread running unobserved (HA's
        # `Stream` class exposes no public cancel API for a stuck worker
        # thread — see live_connection.py's stale-Stream handling). Used
        # only for WARNING-log context on repeat occurrences; never reset.
        self.zombie_stream_worker_count: dict[str, int] = {}
        # Live-stream switch entity references — registered by
        # BoschLiveStreamSwitch.async_added_to_hass. tear_down_live_stream
        # uses this to push the cleared "off" state to HA immediately, so the
        # UI does not show a stale "on" until the next coordinator refresh
        # tick — otherwise a privacy toggle can leave the live-stream switch
        # visibly on.
        self.live_stream_entities: dict[str, Any] = {}
        # User-intent tracking for the live-stream switch. Decouples the
        # switch state from `live_connections`: HA Core opens streams via
        # `async_create_stream` (Lovelace card preload, Cast, play_stream
        # service), each of which populates `live_connections` and would
        # otherwise flip the switch to "on" even though the user never
        # toggled it. The set is keyed by cam_id and only mutated by
        # explicit `BoschLiveStreamSwitch.async_turn_on/off` calls plus
        # external teardowns (`tear_down_live_stream` resets it because a
        # privacy-on / health-watchdog escalation cancels user intent too).
        # Session-State-Facade Slice 3: BoolFieldView over self._sessions
        # (see session_state.py) — preserves the exact `set[str]` in/.add()/
        # .discard() contract external callers use.
        self.user_intent_streams = BoolFieldView(self._sessions, "user_intent_stream")
        # Image entity references — registered on image platform setup
        # Keyed by cam_id; image entities call async_notify_refreshed() after
        # each disk-persist so WKWebView gets a fresh signed URL.
        self.image_entities: dict[str, Any] = {}
        # Per-type last-fetched timestamps (-inf = never → always fetch on first tick)
        self._last_status: float = float("-inf")  # force status check on first tick
        self._last_events: float = float("-inf")  # force event check on first tick
        self._last_slow: float = float("-inf")  # force slow check on first tick
        # Per-camera set of cam_ids whose slow-tier diagnostic fetch was deferred
        # because a live stream was active on that tick.  When the stream goes idle
        # the next coordinator tick picks these up (do_slow_cam becomes True even
        # if the global do_slow interval has not elapsed yet).
        # Invariant: an entry is removed as soon as the deferred fetch actually runs.
        # SENTINEL_RULE: never use 0.0 / float('inf') here — set membership is the flag.
        # Session-State-Facade Slice 1: BoolFieldView over self._sessions (see
        # session_state.py) — preserves the exact `set[str]` in/add/discard
        # contract slow_tier.py already uses.
        self.slow_tier_deferred = BoolFieldView(self._sessions, "slow_tier_deferred")
        # Per-cam monotonic timestamp of when the *current* unbroken deferral
        # started, so a continuously-active stream cannot starve diagnostics
        # forever: once now - start >= SLOW_TIER_MAX_DEFER_SEC we force one read
        # despite the stream. Entry cleared whenever the deferred fetch runs.
        self.slow_tier_defer_since: dict[str, float] = {}
        # Cached data for types that are not re-fetched this tick
        self.cached_status: dict[str, str] = {}
        # Per-cam time (monotonic) the cloud last returned HTTP 444 (session
        # quota / not-ready, e.g. a freshly re-paired camera). For a short window
        # after, WRITE paths skip the cloud and go straight to the LAN/SHC
        # fallback instead of re-hitting the cloud for another 444. -inf = never.
        self.cloud_444_at: dict[str, float] = {}
        self.cached_events: dict[str, list[Any]] = {}
        # SHC local API state cache — keyed by cam_id
        # Each entry: {"device_id": str, "camera_light": bool|None, "privacy_mode": bool|None}
        # Session-State-Facade Slice 2: CacheFieldView over self._sessions.
        self.shc_state_cache: CacheFieldView[dict[str, Any]] = CacheFieldView(
            self._sessions, "shc_state_cache"
        )
        self.shc_devices_raw: list[Any] = []  # cached GET /smarthome/devices response
        self.last_shc_fetch: float = float(
            "-inf"
        )  # last SHC fetch (time.monotonic); -inf = never (SENTINEL_RULE)
        # SHC health tracking — skip SHC calls when offline to avoid latency
        self.shc_available: bool = True  # assume available until proven otherwise
        self.shc_fail_count: int = 0  # consecutive failures
        self.shc_last_check: float = float(
            "-inf"
        )  # last SHC probe (time.monotonic); -inf = never (SENTINEL_RULE)
        # SHC_MAX_FAILS + SHC_RETRY_INTERVAL are class-level constants
        # mirrored from const.py — see top-of-class declaration.
        # Pan position cache — keyed by cam_id, only populated for cameras with panLimit > 0
        # Session-State-Facade Slice 2: CacheFieldView over self._sessions
        # (see session_state.py) for every _rcp_*_cache/pan_cache/etc.
        # below — preserves the exact `dict[str, T]` get/setitem/pop/in/len
        # contract external callers already use.
        self.pan_cache: CacheFieldView[int | None] = CacheFieldView(
            self._sessions, "pan_cache"
        )
        # WiFi info cache — keyed by cam_id, populated from GET /wifiinfo
        self.wifiinfo_cache: dict[str, dict[str, Any]] = {}
        # Ambient light sensor cache — keyed by cam_id, populated from GET /ambient_light_sensor_level
        self.ambient_light_cache: dict[str, float | None] = {}
        # RCP data caches — keyed by cam_id, populated via RCP protocol over cloud proxy
        self.rcp_dimmer_cache: CacheFieldView[int | None] = CacheFieldView(
            self._sessions, "rcp_dimmer_cache"
        )  # LED dimmer value 0–100
        self.rcp_privacy_cache: CacheFieldView[int | None] = CacheFieldView(
            self._sessions, "rcp_privacy_cache"
        )  # privacy mask byte[1] (1=ON)
        self.rcp_clock_offset_cache: CacheFieldView[float | None] = CacheFieldView(
            self._sessions, "rcp_clock_offset_cache"
        )  # camera clock offset vs server (seconds)
        self.rcp_lan_ip_cache: CacheFieldView[str | None] = CacheFieldView(
            self._sessions, "rcp_lan_ip_cache"
        )  # camera LAN IP via RCP 0x0a36
        self.rcp_product_name_cache: CacheFieldView[str | None] = CacheFieldView(
            self._sessions, "rcp_product_name_cache"
        )  # camera product name via RCP 0x0aea
        self.rcp_bitrate_cache: CacheFieldView[list[int]] = CacheFieldView(
            self._sessions, "rcp_bitrate_cache"
        )  # bitrate ladder kbps from 0x0c81
        self.rcp_alarm_catalog_cache: CacheFieldView[list[dict[str, Any]]] = (
            CacheFieldView(self._sessions, "rcp_alarm_catalog_cache")
        )  # alarm types from 0x0c38
        self.rcp_motion_zones_cache: CacheFieldView[list[dict[str, Any]]] = (
            CacheFieldView(self._sessions, "rcp_motion_zones_cache")
        )  # motion zones from 0x0c00
        self.rcp_motion_coords_cache: CacheFieldView[list[dict[str, Any]]] = (
            CacheFieldView(self._sessions, "rcp_motion_coords_cache")
        )  # zone coords from 0x0c0a
        self.rcp_tls_cert_cache: CacheFieldView[dict[str, Any]] = CacheFieldView(
            self._sessions, "rcp_tls_cert_cache"
        )  # TLS cert info from 0x0b91
        self.rcp_network_services_cache: CacheFieldView[list[str]] = CacheFieldView(
            self._sessions, "rcp_network_services_cache"
        )  # network services from 0x0c62
        self.rcp_iva_catalog_cache: CacheFieldView[list[dict[str, Any]]] = (
            CacheFieldView(self._sessions, "rcp_iva_catalog_cache")
        )  # IVA analytics from 0x0b60
        # F4: ONVIF scopes cache — keyed by cam_id, from RCP 0x0a98 via LAN cbs-auth (300s slow-tier)
        self.rcp_onvif_scopes_cache: CacheFieldView[dict[str, Any]] = CacheFieldView(
            self._sessions, "rcp_onvif_scopes_cache"
        )
        # F6: RCP protocol version cache — keyed by cam_id, from RCP 0xff00+0xff04 via LAN (300s slow-tier)
        self.rcp_version_cache: CacheFieldView[str | None] = CacheFieldView(
            self._sessions, "rcp_version_cache"
        )
        # Commands that consistently return error=0x90 (not supported via proxy).
        # Key: cam_id, value: set of command hex strings. After 3 consecutive
        # failures the command is skipped for the rest of the session.
        self._rcp_cmd_failures: dict[
            str, dict[str, int]
        ] = {}  # cam_id → {cmd → fail_count}
        # Video quality preference — keyed by cam_id, runtime only (not persisted)
        # Values: "auto" | "high" | "low"
        self._quality_preference: dict[str, str] = {}
        # Effective `inst` value actually sent on the last successful PUT
        # /connection — set by live_connection.py. Needed because REMOTE
        # sessions clamp a "low" preference's inst=4 to inst=2 (Bosch's
        # REMOTE proxy rejects inst=4 with a 400), so the raw preference in
        # _quality_preference can silently diverge from what's actually
        # streaming; the select entity surfaces this via extra_state_attributes.
        self._quality_effective_inst: dict[str, int] = {}
        # Per-camera Mini-NVR mode override — keyed by cam_id, restored from
        # RestoreEntity on startup (BoschNvrModeSelect), same in-memory
        # pattern as _quality_preference. Values: "continuous" | "event_buffered".
        # Absent = fall back to the global nvr_event_only option.
        # Session-State-Facade Slice 2: CacheFieldView over self._sessions.
        self._nvr_mode_preference: CacheFieldView[str] = CacheFieldView(
            self._sessions, "nvr_mode_preference"
        )
        # RCP session ID cache — keyed by proxy_hash, value (session_id, expires_monotonic)
        # Avoids 2 round-trip RCP handshake on every thumbnail/data fetch
        self.rcp_session_cache: dict[str, tuple[str, float]] = {}
        # Per-proxy_hash lock serializing RCP session opens. Bosch's cloud RCP
        # proxy only tolerates one live session per proxy_hash — two concurrent
        # openers (e.g. a privacy-mode toggle's snapshot trigger racing the
        # coordinator's RCP data refresh) each fire their own 0xff0c/0xff0d
        # handshake, and the proxy rejects whichever loses the race with
        # sessionid 0x00000000 ("proxy rejected"), seen live in production.
        # Serializing on this lock makes the second caller await the first's
        # in-flight open and then read the now-populated cache instead.
        self.rcp_session_locks: dict[str, asyncio.Lock] = {}
        # Proxy URL cache — keyed by cam_id, value (urls[0], expires_monotonic)
        # Proxy leases last ~60s; cache for 50s to skip PUT /connection on warm refreshes
        self._proxy_url_cache: dict[str, tuple[str, float]] = {}
        # cam_id → monotonic timestamp until which the RCP 0x099e thumbnail
        # probe is skipped, after it timed out/failed/errored. A
        # camera that can never satisfy 0x099e (observed on some Gen1 units)
        # would otherwise pay the probe's full 2-8s cost on every single
        # snapshot fetch, forever, starving the snap.jpg leg's timeout budget.
        # Re-probed hourly so a firmware update can re-enable the fast path;
        # cleared immediately on any success.
        self._rcp_099e_probe_failed_until: dict[str, float] = {}
        # Per-camera lock serializing async_fetch_live_snapshot calls.
        # Prevents duplicate PUT /connection when first-load + proactive refresh
        # overlap, or when a user rapid-triggers snapshots.
        # Session-State-Facade Slice 4: CacheFieldView over self._sessions
        # (see session_state.py) — preserves the exact `dict[str,
        # asyncio.Lock]` get/setitem/in contract get_or_create_lock relies
        # on, with lock IDENTITY preserved (verified in
        # tests/test_session_state_facade_slice4.py before this migration).
        self._snapshot_fetch_locks: CacheFieldView[asyncio.Lock] = CacheFieldView(
            self._sessions, "snapshot_fetch_lock"
        )
        # Per-camera lock serializing try_live_connection(). Initialised here
        # (not lazily) so get_stream_lock stays a plain dict lookup.
        self._stream_locks: CacheFieldView[asyncio.Lock] = CacheFieldView(
            self._sessions, "stream_lock"
        )
        # Short-lived cache for async_fetch_fresh_event_snapshot.
        # After an FCM push, async_update_listeners() wakes all HA consumers
        # simultaneously; each calls async_image() → async_fetch_fresh_event_snapshot.
        # Without coalescing this fires 8+ identical cloud round-trips in ~200 ms.
        # The lock (created lazily per cam_id) serialises concurrent callers:
        # the first one fetches and stores the result; the rest acquire the lock
        # after it releases, find the cache hit, and return without a network call.
        # TTL=8s covers the burst window while staying well inside the 60s scan cycle.
        self._fresh_snap_cache: dict[str, tuple[bytes, float]] = {}
        # Session-State-Facade Slice 4: CacheFieldView over self._sessions
        # (see session_state.py) — found during the mandatory systematic
        # re-audit of every per-cam_id `dict[str, asyncio.Lock]` attribute
        # (not just the 5 originally named); same lock-identity-preserving
        # migration as _snapshot_fetch_locks/_stream_locks above.
        self._fresh_snap_locks: CacheFieldView[asyncio.Lock] = CacheFieldView(
            self._sessions, "fresh_snap_lock"
        )
        # AI snapshot-description rate limiter (F3): per-camera cooldown +
        # global daily budget. monotonic sentinel = -inf (SENTINEL_RULE: CI VMs
        # boot ~200s monotonic, 0.0 would falsely satisfy the cooldown).
        self._ai_last_call: dict[str, float] = {}
        self._ai_day_count: int = 0
        self._ai_day_stamp: str = ""
        self.ai_in_flight: int = 0
        self._ai_budget_logged_day: str = ""
        # Persistent storage for the daily AI budget counter (survives restart/reload).
        self._ai_budget_store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}_ai_budget"
        )
        # AI Camera Analysis (structured motion scoring, ai_analysis.py) — its
        # OWN cooldown/budget state, deliberately separate from the AI
        # Snapshot Description fields above so the two features don't
        # compete for the same daily allowance (each has its own
        # ai_*_max_per_day option). Same monotonic sentinel discipline.
        self._ai_analysis_last_call: dict[str, float] = {}
        self._ai_analysis_day_count: int = 0
        self._ai_analysis_day_stamp: str = ""
        self.ai_analysis_in_flight: int = 0
        self._ai_analysis_budget_logged_day: str = ""
        self._ai_analysis_budget_store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}_ai_analysis_budget"
        )
        # ai_alert_store.py's in-memory recent-alerts index: cam_id → list of
        # (generated_at ISO string, score) tuples, newest last, pruned to a
        # bounded window. Read synchronously by ai_analysis.py's prompt
        # builder (repeat-context hint) — deliberately NOT a disk read on the
        # event loop; the JSONL file is the durable log, this is just a fast
        # recent-window cache rebuilt from it on setup.
        self.ai_analysis_recent: dict[str, list[tuple[str, int]]] = {}
        # switch.<cam>_ai_analysis / text.<cam>_ai_scene_context backing
        # stores — plain per-cam_id dicts (Session-State-Facade slice
        # pending; small enough to defer, see coordinator.py's own v15.0.0
        # migration-in-progress note).
        self.ai_analysis_camera_enabled: dict[str, bool] = {}
        self.ai_analysis_scene_context: dict[str, str] = {}
        # Last-seen event IDs per camera — used to detect new events for snapshot refresh
        self.last_event_ids: dict[str, str] = {}
        # Epoch timestamp of coordinator start — used to reject event downloads for
        # events that predate this session (e.g. queued FCM pushes arriving after reload).
        self._download_started_at: float = time.time()
        # Alert-sent cache keyed by event_id → monotonic timestamp. Bosch can
        # send two FCM pushes ~10 s apart for the same MOVEMENT event (once at
        # detection start, again when the clip is finalized), and concurrent
        # push handlers race on `last_event_ids` before either commits. This
        # cache blocks the second alert dispatch when the ID was already
        # alerted within 60 s. Pruned to the 32 most recent entries to bound
        # memory.
        self.alert_sent_ids: dict[str, float] = {}
        # FCM push client — near-instant event detection via Firebase Cloud Messaging
        self.fcm_client = None  # FcmPushClient instance (or None if disabled)
        self.fcm_token: str = ""  # FCM registration token
        self.fcm_running: bool = False
        self.fcm_last_push: float = float(
            "-inf"
        )  # monotonic time of last received push
        # Monotonic time the FCM listener last started successfully. Used by the
        # delivery-death watchdog as the grace reference when no push
        # has ever arrived: push delivery is only judged "dead" once the listener
        # has been up for FCM_DELIVERY_DEAD_AFTER_SEC, so a still-warming-up start
        # is never falsely condemned, while a genuinely dead-from-start Bosch
        # registration is still caught once the grace elapses.
        self.fcm_started_at: float = float("-inf")
        self.fcm_healthy: bool = False  # True when FCM is connected and receiving
        # Set True by the event-poll path when it detects a new event that FCM
        # push never delivered (silent-delivery-death). The supervisor
        # checks this flag at the top of each iteration and does a hard-heal
        # (purge + re-register) when it is set. Cleared by the supervisor.
        self.fcm_force_hard_heal: bool = False
        # The supervisor asyncio.Task that keeps the FCM listener alive. Created
        # by async_ensure_fcm_supervisor; cancelled by async_stop_fcm_supervisor.
        self.fcm_supervisor_task: asyncio.Task[None] | None = None
        # Serialises every FCM start/stop/self-heal so the setup-time start
        # and the watchdog's self-heal can't run concurrently. Without the
        # lock the initial async_start_fcm_push from
        # async_setup_entry ran in parallel with the first coordinator tick's
        # self-heal — two checkin_or_register() calls registered two device
        # tokens in 2 s; the first listener died with NoneType-in-_login
        # (orphaned client whose credentials were overwritten by the second).
        self.fcm_start_lock: asyncio.Lock = asyncio.Lock()
        self.fcm_push_mode: str = (
            "unknown"  # "auto" once FCM listener is up, else "unknown"
        )
        # Lock serializing cross-thread FCM state writes.
        # _on_fcm_push fires in a Firebase thread; the event loop reads these fields.
        self.fcm_lock: threading.Lock = threading.Lock()
        # Unread events count cache — keyed by cam_id, populated from GET /unread_events_count
        self.unread_events_cache: dict[str, int] = {}
        # Privacy sound override cache — keyed by cam_id, populated from GET /privacy_sound_override
        self.privacy_sound_cache: dict[str, bool | None] = {}
        # Commissioned status cache — keyed by cam_id, populated from GET /commissioned
        self.commissioned_cache: dict[str, dict[str, Any]] = {}
        # Feature flags — populated once from GET /v11/feature_flags
        self.feature_flags: dict[str, bool] = {}
        # Protocol version check — run once at startup
        self.protocol_checked: bool = False
        self.integration_version = _INTEGRATION_VERSION
        # Firmware update status cache — keyed by cam_id, from GET /firmware
        self.firmware_cache: dict[str, dict[str, Any]] = {}
        # Per-camera lock serializing async_install_firmware()'s
        # check-then-PUT-then-set sequence. Without
        # this, the Update entity's Install button and the Repairs "Fix"
        # action (both call the same method) could race — both read
        # firmware_cache[cam_id]["updating"] as False before either await
        # completes and sets it, sending two overlapping install PUTs to
        # Bosch's cloud for the same camera.
        self._firmware_install_locks: dict[str, asyncio.Lock] = {}
        # SMB maintenance — last run timestamps (monotonic)
        self.last_smb_cleanup: float = float(
            "-inf"
        )  # float('-inf') → runs on first tick
        # True once the "smbprotocol not installed" Repairs issue's WARNING
        # log line has fired — avoids re-logging every coordinator tick while
        # the issue stays open. Reset to False once the issue clears.
        self._smb_unavailable_logged: bool = False
        # Token refresh failure tracking — alert once, not every 80s
        self._token_alert_sent: bool = False  # True after first alert sent
        self._token_fail_count: int = 0  # consecutive refresh failures
        # Consecutive refresh-timeout failures — tracked separately from
        # _token_fail_count. A timeout proves nothing about the refresh
        # token's validity, so it must never contribute toward the
        # reauth-escalation count.
        self._token_timeout_fail_count: int = 0
        # Bosch auth-server outage tracking — distinct from hard failures.
        # 5xx from Keycloak = Bosch infrastructure problem, NOT user/config issue:
        # no reauth trigger, no escalation, just back off and retry.
        self.auth_outage_count: int = 0  # consecutive 5xx responses
        self._auth_outage_next_retry_ts: float = float("-inf")  # monotonic time gate
        # Cached LOCAL Digest credentials per camera — survives live-connection
        # teardown. Populated on every successful PUT /connection LOCAL and used
        # as a fallback path (snap.jpg, Gen2 RCP privacy writes) when the Bosch
        # cloud is unreachable. Creds are ephemeral (camera rotates them on
        # reboot) but usually stable for minutes to hours.
        # {cam_id: {"user": str, "password": str, "host": str, "port": int, "ts": monotonic}}
        # Session-State-Facade Slice 2: CacheFieldView over self._sessions.
        self.local_creds_cache: CacheFieldView[dict[str, Any]] = CacheFieldView(
            self._sessions, "local_creds_cache"
        )
        # Serializes ensure_valid_token so concurrent refreshes don't race
        # (Keycloak rotates refresh_token and invalidates the previous one —
        # two parallel POSTs with the same token → first wins, second gets
        # invalid_grant and permanently breaks the loop).
        self._token_refresh_lock: asyncio.Lock = asyncio.Lock()
        # TimerHandle for the next scheduled proactive token refresh.
        # Held so async_unload_entry can cancel it — otherwise a config
        # reload leaks timers that still fire against a dead coordinator.
        self.token_refresh_handle: asyncio.TimerHandle | None = None
        # Strong references to fire-and-forget background tasks so the GC
        # does not cancel them mid-flight. Self-removing via done_callback.
        self.bg_tasks: set[asyncio.Task[Any]] = set()
        # Per-camera flag: set True after 3 consecutive session-renewal
        # failures (LOCAL auto-renew loop). Flipped back to False after
        # a successful renewal. Exposed via is_session_stale().
        self.session_stale: dict[str, bool] = {}
        # Timestamp overlay cache — keyed by cam_id, from GET /timestamp
        self.timestamp_cache: dict[str, bool | None] = {}
        # Status LED cache — keyed by cam_id, from GET /ledlights (Gen2 only)
        self.ledlights_cache: dict[str, bool | None] = {}
        # Lens elevation cache — keyed by cam_id, from GET /lens_elevation (Gen2 only)
        self.lens_elevation_cache: dict[str, float | None] = {}
        # Audio settings cache — keyed by cam_id, from GET /audio (Gen2 only)
        # Session-State-Facade Slice 2: CacheFieldView over self._sessions.
        self.audio_cache: CacheFieldView[dict[str, Any]] = CacheFieldView(
            self._sessions, "audio_cache"
        )
        # Motion light cache — keyed by cam_id, from GET /lighting/motion (Gen2 only)
        self.motion_light_cache: dict[str, dict[str, Any]] = {}
        # Image rotation 180° flag — keyed by cam_id, indoor cameras only.
        # No API call — purely a client-side display flag for ceiling-mounted cams.
        # Read by camera.async_camera_image (rotates JPEG via PIL) and by the
        # Pan number entity (inverts sign so "right" stays "right" on screen).
        # State is owned by BoschImageRotation180Switch (RestoreEntity).
        self.image_rotation_180: dict[str, bool] = {}
        # External stream URL exposure flag — keyed by cam_id, default False.
        # Owned by BoschExternalStreamSwitch (RestoreEntity). When True, the
        # per-camera BoschStreamUrlSensor + BoschStreamUrlSubSensor expose the
        # current LOCAL/REMOTE rtspsUrl (inst=1) and a derived inst=2 sub-stream
        # URL so users can paste them into Frigate / BlueIris configs.
        # Default OFF — opt-in per camera, avoids entity-spam.
        self.external_stream_enabled: dict[str, bool] = {}
        # Ambient lighting config cache — keyed by cam_id, from GET /lighting/ambient (Gen2 only)
        self.ambient_lighting_cache: dict[str, dict[str, Any]] = {}
        # Lighting switch cache — keyed by cam_id, from GET /lighting/switch (Gen2 only)
        self.lighting_switch_cache: dict[str, dict[str, Any]] = {}
        # Global lighting config cache — keyed by cam_id, from GET /lighting (Gen2 only)
        # Contains: darknessThreshold (0.0-1.0), softLightFading (bool)
        self.global_lighting_cache: dict[str, dict[str, Any]] = {}
        # Notification type toggles cache — keyed by cam_id, from GET /notifications
        self.notifications_cache: dict[str, dict[str, Any]] = {}
        # Rules cache — keyed by cam_id, from GET /rules
        self.rules_cache: dict[str, list[Any]] = {}
        # Cloud motion zones cache — keyed by cam_id, from GET /motion_sensitive_areas
        self.cloud_zones_cache: dict[str, list[Any]] = {}
        # Cloud privacy masks cache — keyed by cam_id, from GET /privacy_masks
        self.cloud_privacy_masks_cache: dict[str, list[Any]] = {}
        # Lighting options cache — keyed by cam_id, from GET /lighting_options
        self.lighting_options_cache: dict[str, dict[str, Any]] = {}
        # Intrusion detection config cache — keyed by cam_id, from GET /intrusionDetectionConfig (Gen2 only)
        self.intrusion_config_cache: dict[str, dict[str, Any]] = {}
        # Audio detection config cache — keyed by cam_id, from GET /audioDetectionConfig
        # (Gen2 Audio-Plus). Contains: detectGlassBreak, detectFireAlarm (both bool).
        self.audio_detection_cache: dict[str, dict[str, Any]] = {}
        # Alarm settings cache — from GET /alarm_settings (Gen2 Indoor II only).
        # Contains: alarmMode, alarmDelayInSeconds, alarmActivationDelaySeconds,
        #          preAlarmMode, preAlarmDelayInSeconds
        self.alarm_settings_cache: dict[str, dict[str, Any]] = {}
        # Alarm status cache — from GET /alarmStatus (Gen2 Indoor II only).
        self.alarm_status_cache: dict[str, dict[str, Any]] = {}
        # Last observed alarmType per cam — for rising-edge detection of intrusion
        # events. Fires `bosch_shc_camera_intrusion` when alarmType transitions
        # from NONE/empty to a real alarm type (e.g. INTRUSION_DETECTED).
        self._last_alarm_type: dict[str, str] = {}
        # Intrusion system arming cache — derived from alarmStatus (armed/disarmed).
        # Set by BoschAlarmSystemArmSwitch on successful PUT /intrusionSystem/arming.
        self.arming_cache: dict[str, bool] = {}
        # Status LED brightness cache (Gen2 Indoor II) — from GET /iconLedBrightness.
        # Value range: 0-4 (0 = off, 4 = max).
        self.icon_led_brightness_cache: dict[str, int] = {}
        # Gen2 polygon zones cache — keyed by cam_id, from GET /zones (Gen2 only)
        # Contains polygon zones with trigger: "PERSON", maskType, color fields
        self.gen2_zones_cache: dict[str, list[Any]] = {}
        # Gen2 private areas cache — keyed by cam_id, from GET /privateAreas (Gen2 only)
        # Contains privacy mask polygons with color: "#000000"
        self.gen2_private_areas_cache: dict[str, list[Any]] = {}
        # userToken cache — keyed by cam_id, from GET /credentials
        self._user_token_cache: dict[str, str] = {}
        # Separate timer for lighting/switch — polled every tick (60s) instead of slow tier (300s)
        # Bosch app polls this every ~40s; slow tier (300s) is too slow for responsive light state
        self._last_lighting_switch: float = float("-inf")
        # Write-lock timestamps — prevent coordinator from overwriting optimistic state
        # with stale cloud data in the seconds after a successful API write.
        # Keyed by cam_id, value is monotonic time of last successful write.
        # Session-State-Facade Slice 1 (docs/stream-perf-stability-refactor-plan.md):
        # each of these is a `FloatFieldView` over `self._sessions`, not a bare
        # dict — preserves the exact `dict[str, float]` `.get()`/`[cam_id]=`/`in`
        # contract every external call site (shc.py/switch.py/select.py/light.py/
        # number.py/services.py/slow_tier.py) already uses, per session_state.py.
        self.light_set_at = FloatFieldView(
            self._sessions, "light_set_at"
        )  # lighting_override write timestamp
        self.notif_set_at = FloatFieldView(
            self._sessions, "notif_set_at"
        )  # enable_notifications write timestamp
        # Tracks cam_ids for which a "notifications disabled" WARN has been logged.
        # Cleared when the camera re-enables notifications so the WARN re-fires if
        # they are disabled again later.
        self._notif_disabled_logged = BoolFieldView(
            self._sessions, "notif_disabled_logged"
        )
        # Tracks cam_ids for which a "firmware update available" INFO has been
        # logged. Cleared once the update installs (upToDate flips back to True)
        # so the INFO re-fires for the next update.
        self._fw_update_alerted = BoolFieldView(self._sessions, "fw_update_alerted")
        # Tracks cam_ids for which a "event_buffered mode but preroll seconds
        # is 0" WARN has been logged (the ring silently never
        # spawns in this case). Cleared once nvr_preroll_seconds is set > 0
        # so the WARN re-fires if it's ever reset back to 0.
        self._nvr_preroll_zero_warned = BoolFieldView(
            self._sessions, "nvr_preroll_zero_warned"
        )
        self.privacy_set_at = FloatFieldView(
            self._sessions, "privacy_set_at"
        )  # privacy write timestamp
        self.privacy_sound_set_at = FloatFieldView(
            self._sessions, "privacy_sound_set_at"
        )  # privacy_sound_override write
        self.timestamp_set_at = FloatFieldView(
            self._sessions, "timestamp_set_at"
        )  # timestamp overlay write
        self.ledlights_set_at = FloatFieldView(
            self._sessions, "ledlights_set_at"
        )  # status LED write
        self.arming_set_at = FloatFieldView(
            self._sessions, "arming_set_at"
        )  # alarm system arm/disarm write
        self.intrusion_config_set_at = FloatFieldView(
            self._sessions, "intrusion_config_set_at"
        )  # intrusionDetectionConfig write
        self.audio_detection_set_at = FloatFieldView(
            self._sessions, "audio_detection_set_at"
        )  # audioDetectionConfig write (glass-break / fire-alarm)
        self.motion_set_at = FloatFieldView(
            self._sessions, "motion_set_at"
        )  # motion sensitivity write
        self.alarm_settings_set_at = FloatFieldView(
            self._sessions, "alarm_settings_set_at"
        )  # alarm_settings write
        self.lighting_options_set_at = FloatFieldView(
            self._sessions, "lighting_options_set_at"
        )  # lighting schedule write
        # firmware install-trigger write — held just long enough for the
        # optimistic `updating=True` (set by BoschFirmwareUpdate.async_install)
        # to survive one slow-tier poll cycle before Bosch's own backend
        # reports the real in-progress state.
        self.firmware_set_at = FloatFieldView(self._sessions, "firmware_set_at")
        self.WRITE_LOCK_SECS = (
            30.0  # seconds to hold write lock (Bosch cloud propagation can take 20s+)
        )
        # RCP-LAN denied-cache: (cam_id, opcode_hex) → monotonic timestamp when
        # the 401 was observed. CBS users lack permission for some opcodes
        # (e.g. 0x0a98 iconLedBrightness); without this throttle, each slow-tier
        # cycle (~5 min) re-issues the same 401 forever. After 24 h we try
        # once more in case permissions changed. See _fetch_rcp_lan.
        self._rcp_lan_denied_until: dict[tuple[str, str], float] = {}
        # Camera hardware version cache — keyed by cam_id, e.g. "CAMERA_360", "CAMERA_EYES"
        # Used for model-specific timing (encoder warm-up) and feature gating.
        self.hw_version: dict[str, str] = {}
        # TLS proxy for LOCAL RTSPS streams — keyed by cam_id
        # FFmpeg can't handle RTSPS + Digest auth with self-signed certs.
        # The proxy accepts plain TCP and forwards to camera over TLS.
        self.tls_proxy_ports: dict[str, int] = {}  # cam_id → local port
        # asyncio.Server objects backing each proxy (tls_proxy.py is
        # asyncio-native — no module-level socket state). Coordinator-owned
        # so unload can close them deterministically per config entry.
        self.tls_proxy_servers: dict[str, asyncio.base_events.Server] = {}
        # Set right before the defensive `stop_all_proxies` sweep in
        # `_async_cancel_coordinator_tasks` (mirrors `go2rtc_teardown_done`)
        # so a straggler `start_tls_proxy_wiring` call racing that sweep
        # (a queued task, a manual switch/service call landing in the gap
        # between per-cam teardown and platform unload) can't start a fresh
        # proxy that `stop_all_proxies`'s already-taken snapshot will never
        # see — orphaning it past config-entry unload.
        self.tls_proxy_teardown_done = False
        # ── Frigate / external-recorder persistent RTSP front-doors ───────────
        # Per-camera always-on credential-free RTSP endpoint (frigate_endpoint.py).
        # Owned per-camera by the High/Low BoschFrigate*Switch (RestoreEntity);
        # the front-door runner binds a sticky port and opens the Bosch session
        # lazily on the first recorder connect. Default OFF (opt-in).
        self.frigate_runner: FrontDoorRunner | None = None
        self.frigate_high_enabled: dict[str, bool] = {}
        self.frigate_low_enabled: dict[str, bool] = {}
        self._frigate_sticky_port: dict[
            str, int
        ] = {}  # cam_id → stable front-door port
        # ── Main-viewing-path credential-free RTSP front-door ──────────────
        # Always-reused (not opt-in like Frigate's) counterpart to the
        # Frigate front-door above, published via stream_source() for LOCAL
        # sessions — see viewing_front_door.py's module docstring for the
        # go2rtc native-registration-leak rationale.
        self.viewing_front_door_runner: FrontDoorRunner | None = None
        self.viewing_sticky_port: dict[
            str, int
        ] = {}  # cam_id → stable viewing front-door port
        # ── REMOTE viewing-path front-door (remote_viewing_front_door.py) ──
        # Separate runner/sticky-port state from the LOCAL one above —
        # deliberately NOT shared, see remote_viewing_front_door.py's module
        # docstring for the cross-type-reuse hazard this avoids (a
        # REMOTE<->LOCAL transition that bypasses tear_down_live_stream,
        # e.g. session_renewal.promote_to_local, could otherwise leave a
        # shared runner's listener bound with the wrong relay type).
        self.remote_viewing_front_door_runner: FrontDoorRunner | None = None
        self.remote_viewing_sticky_port: dict[
            str, int
        ] = {}  # cam_id → stable REMOTE viewing front-door port
        # Auto-rebuild backoff: monotonic ts of last on_tls_proxy_died rebuild.
        # Prevents a rebuild storm when the new proxy also immediately dies
        # because the camera is still flapping (WiFi jitter, brief Bosch FW glitch).
        self.tls_proxy_rebuild_last: dict[str, float] = {}
        # Stream error tracking — consecutive FFmpeg failures per camera.
        # After max_stream_errors, auto-fallback from LOCAL → REMOTE.
        # `stream_error_at` records monotonic ts of the last record_stream_error
        # tick so AUTO mode can time-decay the counter (cf. _STREAM_ERROR_TTL_SEC
        # in try_live_connection_inner). Without decay a one-off LAN blip
        # (router reboot, transient WLAN dropout) pins the cam to REMOTE forever
        # because record_stream_success only fires on a successful LOCAL stream
        # and AUTO has already stopped attempting LOCAL.
        self.stream_error_count: dict[str, int] = {}
        self.stream_error_at: dict[str, float] = {}
        self.stream_fell_back: dict[
            str, bool
        ] = {}  # True = currently using REMOTE fallback
        # LOCAL session-cred rescue counter. When the HLS consumer goes idle
        # the camera quietly invalidates the per-session digest creds; a later
        # reconnect on the same TLS proxy gets HTTP 401. Re-issuing PUT
        # /connection LOCAL produces fresh creds and keeps us on LAN — falling
        # back to REMOTE in that case is a regression. Counter is bumped on
        # each rescue attempt and reset by record_stream_success(); a non-zero
        # value blocks further rescue attempts in the same failure burst so we
        # can't get stuck in a re-issue loop if the LAN is genuinely broken.
        # Rescues older than _LOCAL_RESCUE_TTL_SEC are treated as "different
        # failure burst" and time-decayed back to 0 — the watchdog's
        # record_stream_success() never fires when no HLS consumer is
        # connected, so without time decay the counter would stick at 1 after
        # the first rescue and the next 401 burst (typically 8–14 min later)
        # would skip straight to REMOTE.
        self.local_rescue_attempts: dict[str, int] = {}
        self.local_rescue_at: dict[
            str, float
        ] = {}  # cam_id → monotonic ts of last rescue
        # TCP reachability cache — (reachable, monotonic_ts). TTL 60s.
        # Populated by async_local_tcp_ping (status loop) and stream pre-check.
        self.lan_tcp_reachable: dict[str, tuple[bool, float]] = {}
        # Monotonic ts of the last time the AUTO-mode TCP
        # pre-check's "unreachable" verdict was deliberately overridden to
        # force a real LOCAL attempt anyway (chicken-and-egg breaker — see
        # LAN_RECHECK_FORCE_INTERVAL_SEC in const.py / live_connection.py).
        self.lan_recheck_forced_at: dict[str, float] = {}
        # Monotonic timestamp of the last successful local-RCP write per cam.
        # The camera briefly tears down its cloud session when Digest creds
        # rotate after an RCP write; we use this to suppress LAN-offline
        # false positives during that ~30 s window. Default `float('-inf')`
        # per SENTINEL_RULE so "never written" never satisfies the grace check.
        self.local_write_at: dict[str, float] = {}
        # During a cloud outage we kick a periodic ping of every known cam IP
        # so the card / switches have a recent reachability signal even though
        # the cloud-driven status loop is blocked. Tracks last outage-ping
        # tick to throttle to once per ~30 s.
        self._last_outage_ping_at: float = float("-inf")
        # Active LOCAL-promotion cooldown: monotonic ts of last attempt to lift
        # an active REMOTE-fallback stream onto LOCAL via Stream.update_source.
        # Prevents ping-pong if LAN is flapping in/out of reachability.
        self.local_promote_at: dict[str, float] = {}
        # SSL context created lazily on first use (ssl.create_default_context
        # is blocking I/O — must not run in the event loop)
        self.tls_ssl_ctx: ssl.SSLContext | None = None
        # Shared, lazily-created plain-HTTP session for the localhost go2rtc
        # API (register/unregister/consumer-count). A completely different
        # trust domain from the Bosch-cloud TLS session in cloud_ssl.py, so
        # it gets its own pool rather than reusing that one. Was previously
        # a fresh aiohttp.ClientSession() per call on all three go2rtc call
        # sites. Closed once in _async_cancel_coordinator_tasks on
        # unload/HA-stop.
        self.go2rtc_session: aiohttp.ClientSession | None = None
        self.go2rtc_session_lock = asyncio.Lock()
        # Set True once _async_cancel_coordinator_tasks has closed the
        # session for good — guards _get_go2rtc_session against lazily
        # re-creating (and leaking) a new session for a stray post-teardown
        # caller. See _get_go2rtc_session's docstring/comment.
        self.go2rtc_teardown_done = False
        # Offline tracking — per camera, monotonic timestamp when first detected offline.
        # Used to extend status check intervals for persistently offline cameras.
        self.offline_since: dict[str, float] = {}
        # Extended offline interval: cameras offline for >15 min are checked every 15 min
        # instead of the normal interval_status (5 min), reducing unnecessary cloud calls.
        self._OFFLINE_EXTENDED_INTERVAL = 900  # 15 minutes
        # Per-camera status check timestamps (for extended offline intervals)
        self.per_cam_status_at: dict[str, float] = {}
        # Stream warm-up state — eagerly initialised so clear_stream_warming() and
        # is_stream_warming() never need hasattr guards. Lazy init (hasattr) caused
        # clear_stream_warming() calls before first is_stream_warming() to silently
        # no-op, leaving the entity badge stuck on "warming" after stream start.
        # set-like facade over CameraSessionState.warming (external readers
        # in camera.py use `in`/`not in`, preserved via StreamWarmingView).
        # warming_started timestamp lives in the same CameraSessionState.
        self.stream_warming = StreamWarmingView(self._sessions)
        # Bosch community RSS-derived maintenance announcement. Periodic refresh
        # every _MAINTENANCE_INTERVAL_S; reactive refresh on cloud 5xx (rate-
        # limited by _MAINTENANCE_REACTIVE_COOLDOWN_S). Cleared explicitly only
        # when the fetcher returns a fresh window — transient community-site
        # outages leave the previous value in place so the sensor stays stable.
        from .maintenance import (
            MaintenanceWindow,
        )  # local import: avoid module-load order issues

        self.maintenance_cache: MaintenanceWindow | None = None
        self.maintenance_last_fetch: float = float("-inf")
        self._MAINTENANCE_INTERVAL_S: float = 3600.0
        self._MAINTENANCE_REACTIVE_COOLDOWN_S: float = 300.0
        # (link, state) of the last user-facing notification we sent for a
        # maintenance window. Dedupes so the same window announces at most
        # three times (scheduled / active / past). In-memory only — a HA
        # restart inside a maintenance window may re-announce, accepted as a
        # v1 trade-off vs. persistence overhead.
        self.maintenance_notified_key: tuple[str, str] | None = None
        # Per-camera last observed availability ("online" / "offline" /
        # "unknown"). First observation is silent so a HA restart while a
        # camera is offline does not re-announce. Transitions involving
        # "unknown" are also silent — those are coordinator transient flaps,
        # not real availability changes.
        self._last_camera_status: dict[str, str] = {}
        # Monotonic ts a camera was first observed offline (for the announce
        # grace window — CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC). Cleared as soon as
        # the camera is seen online again, so a brief repeater/Wi-Fi blip never
        # produces an offline notification.
        # Session-State-Facade Slice 1: FloatFieldView over self._sessions (see
        # session_state.py) — preserves the exact `dict[str, float]` `.get()`/
        # `[cam_id]=`/`.pop()` contract `_async_maybe_announce_camera_status`
        # below already uses.
        self._offline_seen_at = FloatFieldView(self._sessions, "offline_seen_at")
        # Bosch cloud reachability tracker. Fires user notifications on the
        # transitions (healthy → outage) and (outage → recovered). One-tick
        # blips are suppressed by requiring _CLOUD_OUTAGE_NOTIFY_AFTER_S of
        # continuous failure before announcing the outage. The recovery
        # notification fires immediately when the next tick succeeds. While
        # an RSS-announced maintenance window is `active` we stay silent —
        # the maintenance lifecycle notifier already told the user.
        self._cloud_outage_started_at: float | None = None
        self.cloud_outage_notified: bool = False
        self._CLOUD_OUTAGE_NOTIFY_AFTER_S: float = 60.0
        # ── Session-quota (HTTP 444) tracker ─────────────────────────────────
        # Timestamps of recent 444 hits per camera (monotonic). Entries older
        # than _SESSION_QUOTA_WINDOW_S are pruned on each new hit. When ≥3
        # hits occur within the window a persistent notification is shown.
        self._session_quota_hits: dict[str, list[float]] = {}
        self._SESSION_QUOTA_WINDOW_S: float = 300.0  # 5 minutes
        self._SESSION_QUOTA_NOTIFY_THRESHOLD: int = 3
        # ── Mini-NVR — see custom_components/.../recorder.py ───
        # nvr_processes:  cam_id → live ffmpeg subprocess (one per recording).
        # nvr_user_intent: persisted switch state (True = user wants to record).
        # nvr_error_state: cam_id → human-readable error after crash-loop guard.
        # nvr_recent_crash: monotonic ts of last ffmpeg exit (crash-window math).
        # last_nvr_cleanup: last daily retention purge (monotonic).
        # The recorder is a third consumer of the existing TLS proxy — it does
        # NOT open a new RTSP session against the camera (Bosch caps concurrent
        # sessions at 2-3). LAN-only: only runs when _connection_type=LOCAL +
        # camera ONLINE. See `docs/mini-nvr-concept.md` §2.
        self.nvr_processes: dict[str, asyncio.subprocess.Process] = {}
        # Set True as the very first step of config-entry unload/HA-stop
        # teardown (_async_cancel_coordinator_tasks), BEFORE stop_all()/
        # stop_all_preroll() run. Checked by start_recorder/
        # _spawn_preroll_recorder_locked (under the same per-cam
        # `get_nvr_recorder_lock`) so a spawn that is still in flight when
        # unload begins either (a) hasn't acquired the lock yet and bails
        # out without spawning, or (b) already holds the lock and finishes
        # registering into nvr_processes/nvr_preroll_processes before
        # unload's stop_all — which now takes the SAME per-cam lock — can
        # observe and kill it. Closes an orphaned-ffmpeg race (up to 5
        # stray recorder/ring processes could survive a reload, including
        # concurrent writers to the same output file).
        self.nvr_shutting_down: bool = False
        # Session-State-Facade Slice 2: CacheFieldView over self._sessions
        # (see session_state.py) for the plain per-cam Mini-NVR status
        # dicts below — nvr_processes above stays a plain dict (live
        # subprocess handles, deliberately excluded from this slice, see
        # the session_state.py module docstring).
        self.nvr_user_intent: CacheFieldView[bool] = CacheFieldView(
            self._sessions, "nvr_user_intent"
        )
        self.nvr_error_state: CacheFieldView[str] = CacheFieldView(
            self._sessions, "nvr_error_state"
        )
        self.nvr_recent_crash: CacheFieldView[float] = CacheFieldView(
            self._sessions, "nvr_recent_crash"
        )
        # nvr_auth_retry_count: consecutive 401/Unauthorized ffmpeg exits per
        # camera. A single 401 is almost always a
        # transient heartbeat cred-rotation race and is retried without
        # counting toward the crash-window give-up — but retrying forever
        # would hide a GENUINE broken-credential fault. Capped separately
        # in recorder._watch_recorder.
        self.nvr_auth_retry_count: CacheFieldView[int] = CacheFieldView(
            self._sessions, "nvr_auth_retry_count"
        )
        # _nvr_recorder_locks: per-camera lock serializing the tail of
        # recorder.start_recorder (final creds re-read → ffmpeg spawn)
        # against refresh_local_creds_from_heartbeat's in-place mutation of
        # live_connections[cam_id] — closes the remaining cred-rotation race
        # window rather than only tolerating its 401 symptom.
        # Session-State-Facade Slice 4: CacheFieldView over self._sessions
        # (see session_state.py) — same lock-identity-preserving migration
        # as _snapshot_fetch_locks/_stream_locks above.
        self._nvr_recorder_locks: CacheFieldView[asyncio.Lock] = CacheFieldView(
            self._sessions, "nvr_recorder_lock"
        )
        # _nvr_clip_assembly_locks: per-camera lock guarding
        # recorder.assemble_and_ship_motion_clip — prevents overlapping FCM
        # events for the same camera from racing the concat-file write
        # (event_buffered mode).
        self._nvr_clip_assembly_locks: CacheFieldView[asyncio.Lock] = CacheFieldView(
            self._sessions, "nvr_clip_assembly_lock"
        )
        # _nvr_event_clip_enabled: per-camera switch state for the native
        # FCM-triggered event→clip assembly (default True, backward
        # compatible). Installs that orchestrate their own clip-saving
        # externally can turn this off per camera while the underlying
        # pre-roll ring keeps running for their own consumers.
        self._nvr_event_clip_enabled: CacheFieldView[bool] = CacheFieldView(
            self._sessions, "nvr_event_clip_enabled"
        )
        self.last_nvr_cleanup: float = float(
            "-inf"
        )  # float('-inf') → runs on first tick
        # Pre-roll buffer — one short-segment ffmpeg per camera writing to tmpfs.
        # Keyed by cam_id, lifecycle mirrors nvr_processes but independently controlled.
        self.nvr_preroll_processes: dict[str, asyncio.subprocess.Process] = {}
        self._nvr_preroll_last_crash: CacheFieldView[float] = CacheFieldView(
            self._sessions, "nvr_preroll_last_crash"
        )
        self.nvr_preroll_segment_counts: CacheFieldView[int] = CacheFieldView(
            self._sessions, "nvr_preroll_segment_counts"
        )
        self.nvr_preroll_tasks: dict[str, asyncio.Task[Any]] = {}
        # Drain watcher state — populated by recorder.sync_drain_tick. Used by
        # BoschNvrStateSensor to render `target` / `pending_uploads` /
        # `failed_uploads` / `last_segment_age_s` attributes without coupling
        # the sensor to the watcher.
        self.nvr_drain_state: dict[str, Any] = {}
        self.nvr_drain_failures: dict[str, int] = {}
        # Per-coordinator drain watcher task. Started in async_setup_entry,
        # cancelled in async_unload_entry. NOT per-camera — one watcher serves
        # the entire integration.
        self.nvr_drain_task: asyncio.Task[None] | None = None

    def get_model_config(self, cam_id: str) -> Any:
        """Return CameraModelConfig for a camera (from models.py)."""
        from .models import get_model_config

        hw = self.hw_version.get(cam_id, "CAMERA")
        return get_model_config(hw)

    @staticmethod
    def err_str(err: BaseException) -> str:
        """Format an exception so empty-message types (TimeoutError, some
        aiohttp errors) still produce meaningful log output. Falls back to
        repr(err) when str(err) is empty — the original "fetch error: "
        empty-tail bug shipped for months before this helper."""
        s = str(err)
        return s if s else repr(err)

    def _is_rcp_lan_denied(self, cam_id: str, opcode_hex: str) -> bool:
        """Return True if this (cam, opcode) is currently denied (24 h cache).

        Defensive against minimal test-fixture coordinators (no `__init__`)
        that don't have the `_rcp_lan_denied_until` attribute — treat absence
        as "not denied" rather than raising.
        """
        cache: dict[tuple[str, str], float] | None = getattr(
            self, "_rcp_lan_denied_until", None
        )
        if not cache:
            return False
        ts = cache.get((cam_id, opcode_hex))
        if ts is None:
            return False
        return bool((time.monotonic() - ts) < self._RCP_LAN_DENIED_TTL)

    def _mark_rcp_lan_denied(self, cam_id: str, opcode_hex: str) -> None:
        """Record a 401 for this (cam, opcode). Future calls skip for 24 h."""
        if not hasattr(self, "_rcp_lan_denied_until"):
            self._rcp_lan_denied_until = {}
        self._rcp_lan_denied_until[(cam_id, opcode_hex)] = time.monotonic()

    def _clear_rcp_lan_denied(self, cam_id: str, opcode_hex: str) -> None:
        """Clear a denied entry after a successful 200 — permissions may have
        changed (firmware upgrade, CBS user re-provision)."""
        cache = getattr(self, "_rcp_lan_denied_until", None)
        if cache is not None:
            cache.pop((cam_id, opcode_hex), None)

    def _maybe_fire_intrusion_event(
        self, cam_id: str, cam_name: str, alarm_status: dict[str, Any]
    ) -> None:
        """Fire `bosch_shc_camera_intrusion` on rising edge of `alarmType`.

        Bosch /v11/video_inputs/{id}/alarmStatus returns
        `{"alarmType": "NONE" | "INTRUSION_DETECTED" | ..., "intrusionSystem": "ACTIVE" | "INACTIVE" | ...}`.
        Real intrusion → alarmType transitions from "NONE"/empty to something
        else. We fire once per rising edge; identical repeats and falling
        edges do not fire (those would either spam or be misleading).

        Without this, the event type was registered as a webhook target and
        exposed via send_event_webhook but never auto-fired — webhook users
        only got the manual test event.

        Defensive against SimpleNamespace test stubs that lack
        `_last_alarm_type` — lazy-init on first call.
        """
        if not alarm_status:
            return
        raw = alarm_status.get("alarmType")
        if raw is None:
            return
        if not hasattr(self, "_last_alarm_type"):
            self._last_alarm_type = {}
        new_type = str(raw).strip().upper()
        prev_type = self._last_alarm_type.get(cam_id, "NONE").strip().upper()
        was_idle = prev_type in ("", "NONE")
        is_idle = new_type in ("", "NONE")
        if was_idle and not is_idle:
            self.hass.bus.async_fire(
                "bosch_shc_camera_intrusion",
                {
                    "camera_id": cam_id,
                    "camera_name": cam_name,
                    "alarm_type": new_type,
                    "intrusion_system": str(
                        alarm_status.get("intrusionSystem", "")
                    ).upper(),
                    "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                },
            )
        self._last_alarm_type[cam_id] = new_type

    def is_write_locked(
        self, cam_id: str, set_at_dict: dict[str, float] | FloatFieldView
    ) -> bool:
        """Return True if a fresh user-write is still inside the eventual-consistency window.

        Used by every coordinator slow-tier endpoint handler that polls a
        cloud field also writable from a switch entity. Without this guard,
        a poll within `WRITE_LOCK_SECS` of the user toggle can revert the
        cache to the stale cloud value before it has caught up — the bug
        shape that bit privacy_mode + camera_light in v11.0.x. Keep the
        whole pattern in one helper so future cache fields can opt in with
        a one-liner.

        `set_at_dict` accepts either a bare `dict` (legacy call sites / test
        stubs) or a `FloatFieldView` (Session-State-Facade Slice 1 — see
        session_state.py) — both support the `.get(cam_id)` lookup this
        method relies on.
        """
        ts = set_at_dict.get(cam_id)
        return ts is not None and (time.monotonic() - ts) < self.WRITE_LOCK_SECS

    def is_camera_online(self, cam_id: str) -> bool:
        """Return True if this camera's last known status is ONLINE.

        Used by switch/sensor entities to gate availability — prevents commands
        from firing at offline cameras where they cannot be executed.
        Cloud-only switches (Privacy, Notifications) bypass this check since
        those API calls succeed regardless of camera reachability.
        """
        return bool(self.data.get(cam_id, {}).get("status", "UNKNOWN") == "ONLINE")

    def is_session_stale(self, cam_id: str) -> bool:
        """Return True if the LOCAL keepalive loop has given up on this camera.

        Set by `auto_renew_local_session` after 3 consecutive full-renewal
        failures; cleared on the first successful renewal. Entities can use
        this in their `available` property to avoid showing a frozen stream
        as if it were healthy.
        """
        return bool(self.session_stale.get(cam_id, False))

    async def refresh_local_creds_from_heartbeat(
        self,
        cam_id: str,
        resp_text: str,
        generation: int,
        elapsed: float,
    ) -> None:
        """Cache fresh LOCAL creds from a heartbeat PUT response and rebuild
        the cached rtspsUrl so the next stream-worker restart picks them up.

        Thin dispatch to `session_renewal.refresh_local_creds_from_heartbeat`
        — kept as a bound
        method because it is called from within `auto_renew_local_session`
        below and patched directly in tests via `AsyncMock()` /
        `BoschCameraCoordinator.refresh_local_creds_from_heartbeat(coord,
        ...)` unbound-style calls. See `session_renewal.py` for the full
        docstring (cred-rotation window, go2rtc re-registration, NVR
        sidecar survival) — unchanged by this move.
        """
        await refresh_local_creds_from_heartbeat(
            self, cam_id, resp_text, generation, elapsed
        )

    def record_stream_error(self, cam_id: str) -> None:
        """Record a stream error. After max_stream_errors, next stream start uses REMOTE."""
        # The counter exists to suppress LOCAL after consecutive LAN failures.
        # Only count errors on a CONFIRMED-LOCAL session: REMOTE errors are
        # unrelated to LAN health, and a None type (no session / torn down, e.g.
        # a worker error firing after tear_down_live_stream cleared the dict)
        # is not a LAN-health signal either. Counting those would pin the cam to
        # REMOTE after an unrelated hiccup even when LAN works fine again.
        live = self.live_connections.get(cam_id, {})
        if live.get("_connection_type") != "LOCAL":
            return
        count = self.stream_error_count.get(cam_id, 0) + 1
        self.stream_error_count[cam_id] = count
        self.stream_error_at[cam_id] = time.monotonic()
        cfg = self.get_model_config(cam_id)
        # Log only on the transition to threshold — not every subsequent tick while still failing
        if count == cfg.max_stream_errors:
            _LOGGER.warning(
                "Stream error %d/%d for %s — will fall back to REMOTE on next start",
                count,
                cfg.max_stream_errors,
                cam_id[:8],
            )
        elif count > cfg.max_stream_errors:
            _LOGGER.debug(
                "Stream error %d/%d for %s (repeat)",
                count,
                cfg.max_stream_errors,
                cam_id[:8],
            )

    def record_stream_success(self, cam_id: str) -> None:
        """Reset error counter on successful stream."""
        if self.stream_error_count.get(cam_id, 0) > 0:
            _LOGGER.info(
                "Stream recovered for %s — resetting error counter", cam_id[:8]
            )
        self.stream_error_count[cam_id] = 0
        self.stream_error_at.pop(cam_id, None)
        self.stream_fell_back[cam_id] = False
        self.local_rescue_attempts.pop(cam_id, None)
        self.local_rescue_at.pop(cam_id, None)

    async def tear_down_live_stream(
        self, cam_id: str, expected_generation: int | None = None
    ) -> None:
        """Stop an active LOCAL/REMOTE live stream cleanly.

        Thin dispatch to `stream_lifecycle.tear_down_live_stream` — kept as
        a bound method because this is called extensively from other
        coordinator-facing modules (switch.py, slow_tier.py,
        frigate_endpoint.py's FrigateCoordinatorMixin, live_connection.py)
        and the shutdown path (`async_unload_entry`'s
        `getattr(coord, "tear_down_live_stream", None)` duck-typed
        dispatch) as bound `coordinator.tear_down_live_stream(...)` calls,
        and patched directly in tests via `AsyncMock()` /
        `BoschCameraCoordinator.tear_down_live_stream(coord, ...)`
        unbound-style calls. See `stream_lifecycle.py` for the full
        docstring (session-generation race, NVR/proxy/go2rtc/Stream
        teardown order, live-incident history) — unchanged by this move.
        """
        await tear_down_live_stream(self, cam_id, expected_generation)

    def schedule_stream_worker_error(self, cam_id: str, msg: str) -> None:
        """Thread-safe entry point from the log listener.

        Thin dispatch to `stream_lifecycle.schedule_stream_worker_error` —
        kept as a bound method because `_StreamWorkerErrorListener.emit`
        passes `self._coordinator.schedule_stream_worker_error` itself
        (not its call result) to `loop.call_soon_threadsafe`.
        """
        schedule_stream_worker_error(self, cam_id, msg)

    async def handle_stream_worker_error(self, cam_id: str, msg: str) -> None:
        """React to an HA stream-worker error for one camera.

        Thin dispatch to `stream_lifecycle.handle_stream_worker_error` —
        kept as a bound method for the same reasons as
        `tear_down_live_stream` above (external callers, test patching).
        See `stream_lifecycle.py` for the full docstring (401 LOCAL-rescue
        path, REMOTE escalation, threshold semantics) — unchanged by this
        move.
        """
        await handle_stream_worker_error(self, cam_id, msg)

    def replace_renewal_task(
        self, cam_id: str, coro: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Cancel any existing renewal task for cam_id, then create and track the new one.

        Uses async_create_background_task: the keepalive coroutines run as
        `while True` loops that only return on stream-off. Tracked-task API
        (async_create_task) makes HA's startup-wait phase block on these
        loops, which never end — surfaces as a 5-minute "Something is
        blocking Home Assistant from wrapping up the start up phase" warning.
        """
        old = self.renewal_tasks.get(cam_id)
        if old and not old.done():
            old.cancel()
        task = self.hass.async_create_background_task(
            coro, f"bosch_shc_camera_renewal_{cam_id[:8]}"
        )
        self.renewal_tasks[cam_id] = task
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
        return task  # type: ignore[no-any-return]

    def replace_reaper_task(
        self, cam_id: str, coro: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Cancel any existing idle reaper for cam_id, then create and track the new one.

        Mirrors `replace_renewal_task`: the reaper is a `while True` loop that
        only returns on stream-off / teardown, so it must be a background task
        (otherwise HA's startup-wait phase blocks on it).
        """
        old = self.reaper_tasks.get(cam_id)
        if old and not old.done():
            old.cancel()
        task = self.hass.async_create_background_task(
            coro, f"bosch_shc_camera_reaper_{cam_id[:8]}"
        )
        self.reaper_tasks[cam_id] = task
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
        return task  # type: ignore[no-any-return]

    def spawn_tracked(
        self, coro: Coroutine[Any, Any, Any], *, name: str
    ) -> asyncio.Task[Any]:
        """Fire-and-forget a coroutine as a tracked task in `bg_tasks`.

        Several one-shot
        `hass.async_create_task(...)` call sites in the split-out tick
        modules (event_dispatch.py / tick_failure.py / tick_housekeeping.py
        / camera_status.py) were never registered in `bg_tasks`, unlike
        the disciplined stream/FCM task pattern elsewhere in this class
        (`replace_renewal_task` / `replace_reaper_task` / the go2rtc
        re-register spawn). An untracked task is not awaited or cancelled
        by `_async_cancel_coordinator_tasks` on unload/HA-stop — it either
        gets silently orphaned or, on a fast reload, can still be running
        against an already-torn-down coordinator. This is a thin wrapper
        so every future one-shot spawn from those modules goes through the
        same tracked path without duplicating the add/discard boilerplate.
        Not for `while True` loops — those must use
        `async_create_background_task` (see `replace_renewal_task`'s
        docstring for why).
        """
        task = self.hass.async_create_task(coro, name=name)
        self.bg_tasks.add(task)
        task.add_done_callback(self.bg_tasks.discard)
        return task  # type: ignore[no-any-return]

    async def go2rtc_consumer_count(self, cam_id: str) -> int | None:
        """Best-effort count of active go2rtc consumers for this camera's stream.

        Thin dispatch to `stream_lifecycle.go2rtc_consumer_count` — kept as
        a bound method for the same reasons as `tear_down_live_stream`
        above (test patching via `AsyncMock()`, called from
        `has_active_consumer` below). See `stream_lifecycle.py` for the
        full docstring — unchanged by this move.
        """
        return await go2rtc_consumer_count(self, cam_id)

    async def has_active_consumer(self, cam_id: str) -> bool:
        """True if anything is actively consuming the live stream.

        Thin dispatch to `stream_lifecycle.has_active_consumer` — kept as a
        bound method because this is called from `frigate_endpoint.py`'s
        `FrigateCoordinatorMixin` (`self.has_active_consumer(cam_id)`,
        where `self` is the coordinator instance) and patched directly in
        tests via `AsyncMock()`. See `stream_lifecycle.py` for the full
        docstring (three consumer signals, cheap-to-expensive order,
        go2rtc-unreachable semantics) — unchanged by this move.
        """
        return await has_active_consumer(self, cam_id)

    async def idle_session_reaper(self, cam_id: str, generation: int) -> None:
        """Tear down a LOCAL session once nobody is consuming it.

        Thin dispatch to `stream_lifecycle.idle_session_reaper` — kept as a
        bound method because this is called from `live_connection.py` as
        `coordinator.idle_session_reaper(cam_id, gen)` (wrapped in
        `replace_reaper_task`) and patched directly in tests via
        `AsyncMock()` / `BoschCameraCoordinator.idle_session_reaper(c, ...)`
        unbound-style calls. See `stream_lifecycle.py` for the full
        docstring (idle-reap timing, generation tracking) — unchanged by
        this move.
        """
        await idle_session_reaper(self, cam_id, generation)

    # ── Local health check ────────────────────────────────────────────────────
    # Grace period after a local RCP write during which LAN-ping failures are
    # treated as still-reachable: the camera rotates Digest creds + tears down
    # its cloud TLS session after each write, and the LAN HTTPS endpoint is
    # briefly unresponsive (~5–15 s observed). 30 s leaves margin without
    # masking a real network outage.
    LOCAL_WRITE_GRACE_S: float = 30.0

    def _in_local_write_grace(self, cam_id: str, now: float | None = None) -> bool:
        """True if this cam was written to via local RCP within LOCAL_WRITE_GRACE_S."""
        moment = now if now is not None else time.monotonic()
        last = self.local_write_at.get(cam_id, float("-inf"))
        return (moment - last) < self.LOCAL_WRITE_GRACE_S

    def is_lan_reachable(self, cam_id: str) -> bool | None:
        """Most recent LAN-TCP reachability for `cam_id`, or None if unknown.

        Honors `local_write_at` grace period — during the post-write window
        we report the last *positive* reachability (or True if none recorded)
        so the UI does not flip to offline for a few seconds after every
        privacy/light toggle.
        """
        entry = self.lan_tcp_reachable.get(cam_id)
        if entry is None:
            return True if self._in_local_write_grace(cam_id) else None
        reachable, _ts = entry
        if not reachable and self._in_local_write_grace(cam_id):
            return True
        return reachable

    def is_updating(self, cam_id: str) -> bool:
        """True while firmware install is in progress for `cam_id`.

        Reads `firmware_cache[cam_id]['updating']` populated by the slow-tier
        firmware poll. The camera reboots during the install (typically 3–7 min)
        so dependent entities should flip to unavailable for that window. The
        camera-status sensor surfaces the same state as the enum value
        ``"updating"``.
        """
        return bool(self.firmware_cache.get(cam_id, {}).get("updating", False))

    async def async_local_tcp_ping(self, cam_id: str, timeout: float = 1.5) -> bool:
        """Quick TCP connect to camera port 443 on LAN — returns True if reachable.

        Tries rcp_lan_ip_cache first, falls back to local_creds_cache.
        Result is written to lan_tcp_reachable for stream pre-check reuse.
        Much faster than cloud /commissioned check (~5ms vs ~200ms).
        """
        cam_ip = self.get_cam_lan_ip(cam_id)
        if not cam_ip:
            return False  # no known LAN IP — can't ping locally
        # rcp_lan_ip_cache is populated from RCP data returned via the cloud
        # proxy and restored unvalidated from storage — unlike
        # local_creds_cache, whose host is validated at every write site —
        # reject anything that isn't a private LAN address before opening a
        # TCP connection to it.
        if not _is_safe_local_camera_host(f"{cam_ip}:443"):
            return False
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(cam_ip, 443),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            result = True
        except (TimeoutError, OSError):
            result = False
        self.lan_tcp_reachable[cam_id] = (result, time.monotonic())
        return result

    async def async_outage_ping_all(self) -> None:
        """Ping every known camera concurrently during a cloud outage.

        Called from all three outer failure paths in `_async_update_data`
        (`UpdateFailed`/`TimeoutError`/`aiohttp.ClientError`, via
        tick_failure.py's dispatch_* helpers). Throttled
        to once per 30 s so a flapping cloud does not hammer the LAN. Result
        feeds `lan_tcp_reachable`, which the switch/light entity
        `available` checks and the card LAN-tile renderer consult.
        """
        now = time.monotonic()
        if (now - self._last_outage_ping_at) < 30.0:
            return
        self._last_outage_ping_at = now
        cam_ids: list[str] = []
        if self.data:
            cam_ids.extend(self.data.keys())
        # Also include cams known only via LAN IP cache (rare — coordinator
        # data not yet populated after a fresh start mid-outage).
        for cid in self.rcp_lan_ip_cache:
            if cid not in cam_ids:
                cam_ids.append(cid)
        if not cam_ids:
            return
        results = await asyncio.gather(
            *(self.async_local_tcp_ping(cid) for cid in cam_ids),
            return_exceptions=True,
        )
        _ok = sum(1 for r in results if r is True)
        # DEBUG not INFO: throttled to once per 30s but only
        # while the cloud is down — a sustained outage (minutes to hours)
        # would otherwise spam INFO every 30s for the whole duration.
        _LOGGER.debug(
            "Outage LAN-ping: %d/%d cam(s) reachable (%s)",
            _ok,
            len(cam_ids),
            ", ".join(
                f"{cid[:8]}={'on' if r is True else 'off' if r is False else 'err'}"
                for cid, r in zip(cam_ids, results, strict=False)
            ),
        )
        # Notify dependent entities (binary_sensor.*_lan_reachable, privacy/light
        # switch `available` checks) so the new ping result reflects in the UI
        # without waiting for the next coordinator tick.
        self.async_update_listeners()

    def get_cam_lan_ip(self, cam_id: str) -> str | None:
        """Return the best known LAN IP for a camera, or None if not yet discovered."""
        ip = self.rcp_lan_ip_cache.get(cam_id)
        if ip:
            return ip
        creds = self.local_creds_cache.get(cam_id)
        return creds.get("host") if creds else None

    def should_check_status(
        self, cam_id: str, now: float, interval_status: int
    ) -> bool:
        """Determine if this camera needs a status check this tick.

        - Normal cameras: check every interval_status seconds.
        - Persistently offline cameras (>15 min): check every _OFFLINE_EXTENDED_INTERVAL.

        Uses per-camera timestamps (per_cam_status_at) instead of the global
        _last_status so that the check interval is independent of scan_interval.
        With _last_status, setting scan_interval < interval_status caused _last_status
        to advance every tick, making (now - _last_status) always < interval_status
        and status checks never firing after the first tick.
        """
        per_cam_last = self.per_cam_status_at.get(cam_id, float("-inf"))
        offline_since = self.offline_since.get(cam_id)
        if offline_since and (now - offline_since) > self._OFFLINE_EXTENDED_INTERVAL:
            # Camera has been offline for a while — use extended interval
            return (now - per_cam_last) >= self._OFFLINE_EXTENDED_INTERVAL
        return (now - per_cam_last) >= interval_status

    # ── Main update ───────────────────────────────────────────────────────────
    async def _async_update_data(self) -> dict[str, Any]:
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
        # Local import (not top-level): keeps unittest.mock.patch(
        # "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
        # ...) working the same way it did before BoschCameraCoordinator
        # moved out of __init__.py — those patches target the package's own
        # namespace, matching the pattern already used in live_connection.py.
        from . import (
            async_get_bosch_cloud_session as async_get_bosch_cloud_session,
        )

        token = self.token
        if not token and not self.refresh_token:
            # No token at all is not a transient condition UpdateFailed would
            # imply (endless SETUP_RETRY) — it means re-authentication is
            # required, so start the reauth flow instead.
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="not_authenticated",
            )

        opts = self.options
        now = time.monotonic()

        # Fast first tick: on startup, only fetch camera list + basic status.
        # Skip events + slow-tier to reduce startup from ~2 min to ~15s.
        # Full data loads on the second tick (60s later).
        is_first_tick = not hasattr(self, "_first_tick_done")
        if is_first_tick:
            self._first_tick_done = True

        # FCM supervisor heartbeat: the supervisor task manages all restart/retry
        # logic internally (exponential backoff, soft vs hard-heal). This tick
        # only checks that the supervisor task is still alive and restarts it if
        # it somehow died (should never happen — the supervisor loops forever).
        with self.fcm_lock:
            fcm_healthy = self.fcm_healthy
        if opts.get("enable_fcm_push", False):
            sup = getattr(self, "fcm_supervisor_task", None)
            if sup is None or sup.done():
                self.hass.async_create_task(_fcm_async_ensure_supervisor(self))
        if fcm_healthy:
            event_interval = int(opts.get("interval_events", 300))
        else:
            # FCM is not delivering (disabled or flagged unhealthy): the poll IS
            # the detection path now, so it must run faster than the motion
            # window or polled events age out before the binary sensor can see
            # them. Cap at FCM_DOWN_EVENT_POLL_SEC; honour a user's
            # explicitly-lower interval_events via min().
            event_interval = min(
                int(opts.get("interval_events", 300)), int(FCM_DOWN_EVENT_POLL_SEC)
            )
        do_events = (now - self._last_events) >= event_interval
        do_slow = (now - self._last_slow) >= int(opts.get("interval_slow", 300))

        # First tick: skip heavy operations
        if is_first_tick:
            do_events = False
            do_slow = False
            _LOGGER.info(
                "Fast first tick — skipping events + slow-tier for quick startup"
            )

        session = await async_get_bosch_cloud_session(self.hass)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        try:
            # ── 1. List cameras (every tick — lightweight, needed for entity list) ──
            cam_list, token, headers = await fetch_camera_list(
                self, session, headers, token
            )

            # ── Feature flags (fetch once — rarely changes) ────────────────
            await ensure_feature_flags(self, session, headers)

            # ── Protocol version check (once at startup) ──────────────────
            await ensure_protocol_checked(self, session, headers)

            # ── Build camera ID list ─────────────────────────────────────────
            cam_ids: list[str] = []
            cam_by_id: dict[str, dict[str, Any]] = {}
            for cam in cam_list:
                cid = cam.get("id", "")
                if cid:
                    cam_ids.append(cid)
                    cam_by_id[cid] = cam
                    # Cache hardware version for model-specific behavior
                    self.hw_version[cid] = cam.get("hardwareVersion", "CAMERA")

            # ── 2. Status ─ parallel across all cameras ────────────────────────
            any_status_checked = await poll_statuses(
                self, cam_ids, session, headers, now, opts
            )

            # ── 3. Events — parallel across all cameras ──────────────────────
            any_events_fetched = await poll_events(
                self, cam_ids, session, headers, do_events
            )

            # ── Build data dict + process new events (must be sequential) ─────
            data = await build_data_and_dispatch(
                self, cam_ids, cam_by_id, now, do_events
            )

            # Update timestamps only after successful fetches
            if any_status_checked:
                self._last_status = now
            # Advance the events timestamp only when at least one camera returned
            # a definitive result. If every fetch failed (cloud blip), leave
            # _last_events so do_events stays True next tick and the poll retries
            # promptly instead of backing off a full interval (up to 300 s while
            # FCM is healthy). Cross-version parity with the ioBroker fix.
            if do_events and any_events_fetched:
                self._last_events = now
            if do_slow:
                self._last_slow = now

            # ── 4. Read privacy mode + light from cloud API response (primary) ──
            # Cloud API is ~10x faster than SHC local API (113ms vs 1122ms).
            # privacyMode and featureSupport are already in /v11/video_inputs —
            # no extra request needed. SHC (step 5) supplements as fallback.
            for cam_id_key, cam_entry in data.items():
                cam_raw = cam_entry.get("info", {})
                _poll_cam_info_caches(self, cam_id_key, cam_raw)

                # ── Per-camera context: hw/is_gen2/is_online/stream state/
                # slow-tier defer gate — computed once, shared by every
                # slow-tier sub-block below (replaces several redundant
                # re-derivations the original inline loop had at different
                # points) ──────────────────────────────────────────────────
                ctx = _compute_cam_context(
                    self, cam_id_key, cam_raw, data, opts, do_slow
                )
                is_online = ctx.is_online
                do_slow_cam = ctx.do_slow_cam

                # Pan position + Gen2 lighting/switch — both polled every
                # tick (not slow-tier-gated), only gated on is_online.
                await _poll_cam_control(self, cam_id_key, ctx, session, headers)

                # ── Slow tier: wifiinfo, ambient light, motion, audio, recording ──
                # Only fetched every interval_slow seconds (default 5 min).
                await _poll_slow_tier_endpoints(
                    self,
                    cam_id_key,
                    cam_raw,
                    ctx,
                    data,
                    session,
                    headers,
                    lambda cid, title, ep_data: (
                        BoschCameraCoordinator._maybe_fire_intrusion_event(
                            self, cid, title, ep_data
                        )
                    ),
                )

                # ── RCP data via cloud proxy (slow tier — every 5 min) ────────
                # Opens a proxy connection and reads multiple RCP values.
                # Only when camera is ONLINE and slow-tier interval elapsed.
                # Skip RCP data fetch if a LOCAL stream is active — the RCP fetch
                # opens a REMOTE PUT /connection which would overwrite the LOCAL
                # session and kill the go2rtc stream.
                # Skip when Privacy is ON — the cloud proxy rejects RCP session
                # handshakes (invalid session 0x00000000) while privacy blocks the
                # camera's RCP endpoint. Avoids noisy debug logs every 5 min.
                local_stream_active = ctx.local_stream_active
                privacy_on = ctx.privacy_on
                if is_online and do_slow_cam and privacy_on:
                    _LOGGER.debug(
                        "RCP slow-tier skipped for %s (privacy ON)", cam_id_key
                    )
                if (
                    is_online
                    and do_slow_cam
                    and not local_stream_active
                    and not privacy_on
                ):
                    try:
                        # Pooled Bosch-cloud session (cloud_ssl.py) — this
                        # slow-tier RCP fetch used to open a fresh
                        # TCPConnector+ClientSession per camera on every tick.
                        # Must NOT be closed here: it's process-wide, shared
                        # with every other Bosch-cloud call, and closed
                        # exactly once on EVENT_HOMEASSISTANT_STOP.
                        rcp_session = await async_get_bosch_cloud_session(self.hass)
                        rcp_headers = {
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        }
                        try:
                            async with asyncio.timeout(TIMEOUT_PUT_CONNECTION):
                                async with rcp_session.put(
                                    f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/connection",
                                    json={
                                        "type": "REMOTE",
                                        "highQualityVideo": self.get_quality_params(
                                            cam_id_key
                                        )[0],
                                    },
                                    headers=rcp_headers,
                                ) as conn_resp:
                                    if conn_resp.status in (200, 201):
                                        conn_data = await conn_resp.json(
                                            content_type=None
                                        )
                                        urls = conn_data.get("urls", [])
                                        # urls[0] = "proxy-NN.live.cbs.boschsecurity.com:42090/{hash}"
                                        # _parse_safe_rcp_proxy_url rejects a
                                        # malformed/unsafe entry by returning
                                        # None (logs internally) — an
                                        # unvalidated proxy host here is an
                                        # SSRF path.
                                        parsed_proxy = (
                                            _parse_safe_rcp_proxy_url(
                                                urls[0], cam_id_key
                                            )
                                            if urls
                                            else None
                                        )
                                        if parsed_proxy:
                                            proxy_host, proxy_hash = parsed_proxy
                                            await self._async_update_rcp_data(
                                                cam_id_key, proxy_host, proxy_hash
                                            )
                                    else:
                                        _LOGGER.debug(
                                            "RCP proxy connection HTTP %d for %s",
                                            conn_resp.status,
                                            cam_id_key,
                                        )
                        except (TimeoutError, aiohttp.ClientError) as err:
                            _LOGGER.debug(
                                "RCP proxy connect error for %s: %s",
                                cam_id_key,
                                err,
                            )
                    except Exception as err:
                        _LOGGER.debug("RCP update skipped for %s: %s", cam_id_key, err)

                # ── F4/F6 LAN diagnostic sensors (slow tier) ─────────────────
                # Reads ONVIF scopes (0x0a98) and RCP version (0xff00) directly
                # from camera HTTPS LAN endpoint using cached cbs Digest creds.
                # Only runs when LAN IP and cbs creds are available — fully
                # non-blocking (errors are swallowed, sensor stays unavailable).
                if (
                    is_online
                    and do_slow_cam
                    and self.get_cam_lan_ip(cam_id_key)
                    and self.local_creds_cache.get(cam_id_key)
                ):
                    try:
                        await self._async_update_lan_diagnostic_sensors(cam_id_key)
                    except Exception as err:
                        _LOGGER.debug(
                            "LAN diagnostic sensors skipped for %s: %s", cam_id_key, err
                        )

            # ── 5. SHC states (supplementary + offline fallback) ────────────────
            # Cloud is primary (step 4, ~113ms). SHC supplements with camera
            # light state and serves as fallback when cloud is unreachable.
            if self.shc_ready:
                try:
                    await self._async_update_shc_states(data)
                except Exception as err:
                    _LOGGER.debug("SHC state update error: %s", err)

            # ── 7/8. Housekeeping: SMB/NVR cleanup, stale devices, availability
            # notify, LAN-IP/hw-version/local-creds persistence, maintenance feed,
            # cloud-state notify ────────────────────────────────────────────────
            await run_housekeeping(self, data, opts, now, is_first_tick)

            # Raise a Repairs issue when movement/person notifications are
            # disabled on a camera — without them the binary sensors are
            # permanently "Clear" with no error shown to the user.
            try:
                self._refresh_notifications_disabled_issues()
            except Exception:
                _LOGGER.debug(
                    "Notifications-disabled Repairs check failed (non-fatal)",
                    exc_info=True,
                )

            # Raise a Repairs issue when a firmware update is available for a
            # camera — see _refresh_firmware_update_issues docstring.
            try:
                self._refresh_firmware_update_issues()
            except Exception:
                _LOGGER.debug(
                    "Firmware-update Repairs check failed (non-fatal)",
                    exc_info=True,
                )

            # Raise a Repairs issue when an SMB-dependent feature is
            # configured but the optional smbprotocol package isn't
            # installed — see _refresh_smb_unavailable_issue docstring.
            try:
                self._refresh_smb_unavailable_issue()
            except Exception:
                _LOGGER.debug(
                    "SMB-unavailable Repairs check failed (non-fatal)",
                    exc_info=True,
                )

            return data

        except UpdateFailed:
            await dispatch_update_failed(self)
            raise
        except TimeoutError:
            raise await dispatch_timeout(self) from None
        except aiohttp.ClientError as err:
            raise await dispatch_client_error(self, err) from err

    def _refresh_notifications_disabled_issues(self) -> None:
        """Create or clear Repairs issues for cameras with disabled movement/person notifications.

        Thin delegator — logic lives in repairs.py (style audit,
        2026-08-04: repairs-issue lifecycle doesn't belong inline on the
        coordinator). Kept as a real bound method (not removed) so
        _async_update_data's own call site below and the test suite's
        unbound-method-call pattern (`BoschCameraCoordinator.
        _refresh_notifications_disabled_issues(coord)`) keep working
        unchanged.
        """
        repairs.refresh_notifications_disabled_issues(self)

    def _refresh_firmware_update_issues(self) -> None:
        """Create or clear Repairs issues for cameras with a firmware update available.

        Thin delegator — see _refresh_notifications_disabled_issues docstring.
        """
        repairs.refresh_firmware_update_issues(self)

    def _refresh_smb_unavailable_issue(self) -> None:
        """Create or clear a Repairs issue when smbprotocol is missing but needed.

        Thin delegator — see _refresh_notifications_disabled_issues docstring.
        """
        repairs.refresh_smb_unavailable_issue(self)

    async def async_install_firmware(self, cam_id: str) -> None:
        """Install the pending firmware update for `cam_id` right now.

        Thin delegator — logic lives in device_actions.py (style audit,
        2026-08-04: device-action triggers don't belong inline on the
        coordinator). Kept as a real bound method (not removed) so
        update.py's Install button, repairs.py's Fix-flow, and the test
        suite's attribute-mocking/unbound-method-call patterns keep
        working unchanged.
        """
        await device_actions.async_install_firmware(self, cam_id)

    async def async_soft_reset_camera(self, cam_id: str) -> None:
        """Reboot the camera (soft reset).

        Thin delegator — see async_install_firmware docstring.
        """
        await device_actions.async_soft_reset_camera(self, cam_id)

    async def async_hard_reset_camera(self, cam_id: str) -> None:
        """Factory-reset the camera (hard reset).

        Thin delegator — see async_install_firmware docstring.
        """
        await device_actions.async_hard_reset_camera(self, cam_id)

    async def _async_refresh_maintenance(self, *, reactive: bool) -> None:
        """Fetch the Bosch community maintenance announcement in the background.

        Reactive calls (triggered by cloud 5xx/timeout) are rate-limited so a
        flapping cloud does not hammer the community site. Periodic calls run
        once per _MAINTENANCE_INTERVAL_S regardless of cloud health.

        Failure is silent — the previous cache value is retained so the sensor
        does not flap on a transient community-site outage.
        """
        # Local import (not top-level): keeps unittest.mock.patch(
        # "custom_components.bosch_shc_camera.async_get_clientsession", ...)
        # working the same way it did before BoschCameraCoordinator moved
        # out of __init__.py — matches the live_connection.py pattern.
        from . import (
            async_get_clientsession as async_get_clientsession,
        )
        from .maintenance import async_fetch_maintenance

        now = time.monotonic()
        if (
            reactive
            and (now - self.maintenance_last_fetch)
            < self._MAINTENANCE_REACTIVE_COOLDOWN_S
        ):
            return
        self.maintenance_last_fetch = now
        try:
            session = async_get_clientsession(self.hass)
            result = await async_fetch_maintenance(session)
        except Exception as exc:
            _LOGGER.debug("Maintenance fetch raised: %s", exc)
            return
        if result is not None:
            self.maintenance_cache = result
            _LOGGER.debug(
                "Maintenance: %s state=%s window=%s..%s",
                result.title[:60],
                result.state(),
                result.scheduled_start,
                result.scheduled_end,
            )
            await self._async_maybe_announce_maintenance(result)

    async def _async_maybe_announce_maintenance(self, mw: "MaintenanceWindow") -> None:
        """Fire a user notification for a maintenance-window state transition.

        Thin delegator — logic lives in announcements.py (style audit,
        2026-08-04: notification-orchestration business logic doesn't
        belong inline on the coordinator). Kept as a real bound method
        (not removed) so the test suite's unbound-method-call and
        attribute-mocking patterns keep working unchanged.
        """
        await announcements.maybe_announce_maintenance(self, mw)

    def _persist_maint_notified_key(self) -> None:
        """Write `maintenance_notified_key` to disk so HA restarts mid-
        window do not re-fire the active-state announcement on the next
        coordinator tick — otherwise every restart wipes the in-memory
        dedup key and a single maintenance window can produce dozens of
        duplicate alerts.
        """
        key = self.maintenance_notified_key
        store = getattr(self, "maint_notified_store", None)
        if store is None or key is None:
            return
        self.hass.async_create_task(store.async_save({"link": key[0], "state": key[1]}))

    def _persist_cloud_outage_flag(self) -> None:
        """Mirror the maintenance-key persistence for the cloud-state
        dedup flag, so a restart mid-outage doesn't re-fire "Cloud nicht
        erreichbar"."""
        store = getattr(self, "cloud_alert_store", None)
        if store is None:
            return
        # Tracked (not a bare hass.async_create_task) — an untracked save
        # can still complete after config-entry removal deletes the Store,
        # recreating integration-owned state on disk after removal and
        # bypassing the teardown behavior spawn_tracked() documents.
        self.spawn_tracked(
            store.async_save({"outage_notified": bool(self.cloud_outage_notified)}),
            name="bosch_shc_camera_persist_cloud_outage_flag",
        )

    async def _async_maybe_announce_camera_status(
        self,
        cam_id: str,
        new_status: str,
    ) -> None:
        """Fire a notification when a camera flips between online and offline.

        Thin delegator — see _async_maybe_announce_maintenance docstring.
        Kept as a real bound method specifically because tick_housekeeping.
        run_housekeeping reaches this via
        `getattr(coordinator, "_async_maybe_announce_camera_status", None)`
        to stay a safe no-op for stub coordinators that don't define it
        (tests/test_tick_housekeeping.py::
        test_stub_coordinator_without_announce_helpers_no_crash pins this).
        """
        await announcements.maybe_announce_camera_status(self, cam_id, new_status)

    async def _async_handle_session_quota_hit(self, cam_id: str) -> None:
        """Track HTTP 444 hits per camera and fire a persistent notification if repeated.

        After _SESSION_QUOTA_NOTIFY_THRESHOLD (3) hits within _SESSION_QUOTA_WINDOW_S (5 min)
        a HA persistent_notification is created advising the user to close other clients.
        Non-fatal — any failure is swallowed so the caller's status update is unaffected.
        """
        try:
            now = time.monotonic()
            hits = self._session_quota_hits.setdefault(cam_id, [])
            # Prune hits outside the window
            hits[:] = [t for t in hits if (now - t) < self._SESSION_QUOTA_WINDOW_S]
            hits.append(now)

            if len(hits) >= self._SESSION_QUOTA_NOTIFY_THRESHOLD:
                cam_info = (
                    self.data.get(cam_id, {}).get("info", {}) if self.data else {}
                )
                cam_name = cam_info.get("title") or cam_id[:8]
                notification_id = f"bosch_session_quota_{cam_id[:8].lower()}"
                title = f"Bosch Kamera {cam_name}: Sitzungslimit erreicht"
                message = (
                    f"Kamera {cam_name} meldet HTTP 444 (Session-Quota). "
                    "Zu viele gleichzeitige Live-Verbindungen im Bosch-Konto. "
                    "Bitte schließen Sie die Bosch App auf weiteren Geräten "
                    "oder deaktivieren Sie parallele Integrationen (ioBroker, Python CLI). "
                    "Die Integration wiederholt den Verbindungsaufbau automatisch."
                )
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": title,
                        "message": message,
                        "notification_id": notification_id,
                    },
                    blocking=False,
                )
                _LOGGER.warning(
                    "Session-quota persistent notification created for %s (%d hits in %.0fs)",
                    cam_id[:8],
                    len(hits),
                    self._SESSION_QUOTA_WINDOW_S,
                )
        except Exception as exc:
            _LOGGER.debug("Session-quota notification failed (non-fatal): %s", exc)

    async def _async_maybe_announce_cloud_state(self, success: bool) -> None:
        """Fire a user notification on cloud-reachability transitions.

        Thin delegator — see _async_maybe_announce_maintenance docstring.
        Kept as a real bound method specifically because tick_failure.
        dispatch_* and tick_housekeeping.run_housekeeping reach this via
        `getattr(coordinator, "_async_maybe_announce_cloud_state", None)`
        to stay a safe no-op for stub coordinators that don't define it
        (tests/test_tick_failure.py::test_missing_announce_method_is_a_noop
        and tests/test_tick_housekeeping.py::
        test_stub_coordinator_without_notifier_no_crash pin this).
        """
        await announcements.maybe_announce_cloud_state(self, success)

    def _compute_status_for(
        self,
        cam_id: str,
        cam_data: dict[str, Any] | None = None,
    ) -> str:
        """Re-uses the BoschCameraStatusSensor logic so the announce path and
        the sensor never drift apart.

        Mirror of `sensor.BoschCameraStatusSensor.native_value`: cloud ONLINE
        + latest event TROUBLE_DISCONNECT → offline; otherwise the cloud
        status verbatim. The `cam_data` argument lets the update-loop pass
        the fresh data dict before `self.data` has been swapped by the
        parent class (`_async_update_data` returns after the per-cam
        transition check fires).
        """
        if cam_data is None:
            cam_data = self.data.get(cam_id, {}) if self.data else {}
        raw = str(cam_data.get("status", "UNKNOWN")).lower()
        if raw == "online":
            events = cam_data.get("events", [])
            if (
                events
                and str(events[0].get("eventType", "")).upper() == "TROUBLE_DISCONNECT"
            ):
                return "offline"
        return raw

    # ── Per-cam_id dict/set purge ──────────────────────────
    # `cleanup_stale_devices` below only removed the device-registry entry
    # for a camera that disappeared from the Bosch cloud account — none of
    # the ~100 per-cam_id-keyed coordinator dict/set attributes accumulated
    # over this coordinator instance's lifetime were ever cleared. On a
    # camera swap/rename (new cam_id, old one gone for good) those entries
    # just sit there forever, growing unbounded over the coordinator's
    # lifetime (never restarted except on HA restart/reload). This list is
    # audited against `BoschCameraCoordinator.__init__` — every attribute
    # there whose declared comment/usage confirms it is keyed by the plain
    # cam_id string lives in one of the two tuples below; anything NOT
    # listed here was deliberately excluded (see the comment block at the
    # end) and should stay that way unless its keying changes.
    #
    # Plain `dict[str, ...]` attributes keyed directly by cam_id → `.pop()`.
    _PURGE_CAM_DICT_ATTRS: tuple[str, ...] = (
        "_sessions",  # also backs the live_opened_at / stream_warming views
        # (and, since Slice 2, every _rcp_*_cache / shc_state_cache /
        # pan_cache / audio_cache / local_creds_cache / _nvr_mode_preference /
        # plain _nvr_* status CacheFieldView below, plus, since Slice 3,
        # live_connections / user_intent_streams — see the excluded-list
        # comment block at the end of this tuple)
        "audio_enabled",
        "audio_volume",
        "_auto_renew_tasks",  # legacy, kept for backwards-compat, never populated
        "renewal_tasks",
        "reaper_tasks",
        "camera_entities",
        "zombie_stream_worker_count",
        "live_stream_entities",
        "image_entities",
        "slow_tier_defer_since",
        "cached_status",
        "cloud_444_at",
        "cached_events",
        "wifiinfo_cache",
        "ambient_light_cache",
        "_rcp_cmd_failures",
        "_quality_preference",
        "_quality_effective_inst",
        "_proxy_url_cache",
        "_rcp_099e_probe_failed_until",
        "_fresh_snap_cache",
        "_ai_last_call",
        "_ai_analysis_last_call",
        "ai_analysis_recent",
        "ai_analysis_camera_enabled",
        "ai_analysis_scene_context",
        "last_event_ids",
        "unread_events_cache",
        "privacy_sound_cache",
        "commissioned_cache",
        "firmware_cache",
        "_firmware_install_locks",
        "session_stale",
        "timestamp_cache",
        "ledlights_cache",
        "lens_elevation_cache",
        "motion_light_cache",
        "image_rotation_180",
        "external_stream_enabled",
        "ambient_lighting_cache",
        "lighting_switch_cache",
        "global_lighting_cache",
        "notifications_cache",
        "rules_cache",
        "cloud_zones_cache",
        "cloud_privacy_masks_cache",
        "lighting_options_cache",
        "intrusion_config_cache",
        "audio_detection_cache",
        "alarm_settings_cache",
        "alarm_status_cache",
        "_last_alarm_type",
        "arming_cache",
        "icon_led_brightness_cache",
        "gen2_zones_cache",
        "gen2_private_areas_cache",
        "_user_token_cache",
        "hw_version",
        "tls_proxy_ports",
        "tls_proxy_servers",
        "frigate_high_enabled",
        "frigate_low_enabled",
        "_frigate_sticky_port",
        "viewing_sticky_port",
        "remote_viewing_sticky_port",
        "tls_proxy_rebuild_last",
        "stream_error_count",
        "stream_error_at",
        "stream_fell_back",
        "local_rescue_attempts",
        "local_rescue_at",
        "lan_tcp_reachable",
        "lan_recheck_forced_at",
        "local_write_at",
        "local_promote_at",
        "offline_since",
        "per_cam_status_at",
        "_last_camera_status",
        "_session_quota_hits",
        "nvr_processes",
        "nvr_preroll_processes",
        "nvr_preroll_tasks",
        "nvr_drain_state",
        "nvr_drain_failures",
    )
    # `set[str]` attributes whose members are cam_id → `.discard()`.
    # Empty since Slice 3: `user_intent_streams` (the last member) is now a
    # BoolFieldView facade over `_sessions` — see the excluded-list comment
    # block below.
    _PURGE_CAM_SET_ATTRS: tuple[str, ...] = ()
    # Deliberately EXCLUDED (audited, not an oversight):
    #   rcp_session_cache / rcp_session_locks — keyed by proxy_hash, not cam_id.
    #   alert_sent_ids — keyed by event_id, not cam_id.
    #   feature_flags — account-level (GET /v11/feature_flags once), not per-cam.
    #   live_opened_at / stream_warming / _offline_seen_at / light_set_at /
    #       notif_set_at / privacy_set_at / privacy_sound_set_at /
    #       timestamp_set_at / ledlights_set_at / arming_set_at /
    #       intrusion_config_set_at / audio_detection_set_at / motion_set_at /
    #       alarm_settings_set_at / lighting_options_set_at / firmware_set_at /
    #       slow_tier_deferred / _notif_disabled_logged / _fw_update_alerted /
    #       _nvr_preroll_zero_warned —
    #       thin FloatFieldView/BoolFieldView facades over _sessions (Session-
    #       State-Facade Slice 1, see session_state.py); purging _sessions
    #       purges these automatically, and they are no longer `dict`/`set`
    #       instances so `test_cam_id_purge_completeness.py`'s `vars(coord)`
    #       auto-discovery no longer even sees them as candidates.
    #   _rcp_state_cache / shc_state_cache / pan_cache / rcp_dimmer_cache /
    #       rcp_privacy_cache / rcp_clock_offset_cache / rcp_lan_ip_cache /
    #       rcp_product_name_cache / rcp_bitrate_cache /
    #       rcp_alarm_catalog_cache / rcp_motion_zones_cache /
    #       rcp_motion_coords_cache / rcp_tls_cert_cache /
    #       rcp_network_services_cache / rcp_iva_catalog_cache /
    #       rcp_onvif_scopes_cache / rcp_version_cache / _nvr_mode_preference /
    #       local_creds_cache / audio_cache / nvr_user_intent /
    #       nvr_error_state / nvr_recent_crash / nvr_auth_retry_count /
    #       _nvr_event_clip_enabled / _nvr_preroll_last_crash /
    #       nvr_preroll_segment_counts — thin `CacheFieldView` facades over
    #       _sessions (Session-State-Facade Slice 2, see session_state.py);
    #       same "_sessions purge covers it, no longer a bare dict instance"
    #       reasoning as the Slice 1 list above.
    #   nvr_drain_state / nvr_drain_failures — NOT actually cam_id-keyed
    #       despite the `dict[str, ...]` type hint (audited during Slice 2,
    #       see session_state.py module docstring): nvr_drain_state is a
    #       single flat dict with fixed keys ("target"/"pending"/etc.)
    #       replaced wholesale every drain tick, and nvr_drain_failures is
    #       keyed by staging file path, not cam_id. Left in this tuple as a
    #       harmless no-op (a cam_id never matches those keys) rather than
    #       moved, to avoid a second churn on this list for a non-bug.
    #   live_connections — thin `CacheFieldView` facade over _sessions
    #       (Session-State-Facade Slice 3, see session_state.py); same
    #       "_sessions purge covers it" reasoning as the Slice 2 list above.
    #   user_intent_streams — thin `BoolFieldView` facade over _sessions
    #       (Session-State-Facade Slice 3, see session_state.py); no longer
    #       a `set` instance so `test_cam_id_purge_completeness.py`'s
    #       `vars(coord)` auto-discovery no longer sees it as a candidate,
    #       same reasoning as the Slice 1 bool-flag list above. This is why
    #       `_PURGE_CAM_SET_ATTRS` above is now empty.
    #   _snapshot_fetch_locks / _stream_locks / _go2rtc_reregister_locks /
    #       _nvr_recorder_locks / _nvr_clip_assembly_locks / _fresh_snap_locks
    #       (the last found via systematic re-audit, not originally named) —
    #       thin `CacheFieldView` facades over _sessions (Session-State-
    #       Facade Slice 4, see session_state.py); same "_sessions purge
    #       covers it, no longer a bare dict instance" reasoning as the
    #       Slice 2/3 lists above. Popping the whole `_sessions[cam_id]`
    #       entry only ever happens once a camera is confirmed gone from the
    #       Bosch cloud account (see `_purge_cam_id`'s docstring) — never
    #       mid-operation while one of that camera's locks could be held, so
    #       this is safe for lock-typed fields too, not just plain data.
    #   Everything else in __init__ not listed above is a genuinely global/
    #   account-level attribute (counters, constants, locks keyed by
    #   proxy_hash, single Task/Store handles, etc.) — not per-cam.
    # `_rcp_lan_denied_until` is handled separately below: it is keyed by a
    # (cam_id, opcode_hex) TUPLE, not a plain cam_id string.

    def _purge_cam_id(self, cam_id: str) -> None:
        """Purge every per-cam_id coordinator dict/set entry for `cam_id`.

        Called from `cleanup_stale_devices` once a camera has been
        confirmed gone from the Bosch cloud account (device-registry entry
        already removed) — never mid-operation, so popping locks with no
        in-flight waiters is safe. See `_PURGE_CAM_DICT_ATTRS` /
        `_PURGE_CAM_SET_ATTRS` above for the audited attribute list.

        `tls_proxy_servers` is popped explicitly, BEFORE the generic loop
        below (which also lists it, purely so the auto-discovery
        completeness test in `tests/test_cam_id_purge_completeness.py`
        confirms it's gone by the time this method returns): unlike every
        other entry in that list (plain ints/dicts/caches), its value is a
        live `asyncio.Server` — a bare `.pop()` with the reference
        discarded would drop it without closing the listening socket,
        leaking it for the rest of the HA process lifetime. The dict
        removal itself stays synchronous (so callers/tests see it gone
        immediately); only the actual socket-close I/O is deferred to a
        tracked background task.
        """
        server = self.tls_proxy_servers.pop(cam_id, None)
        if server is not None:

            async def _close_leftover_proxy() -> None:
                try:
                    server.close()
                    server.close_clients()
                    await server.wait_closed()
                except Exception as exc:
                    _LOGGER.debug(
                        "TLS proxy for %s: close during camera-removal purge raised — %s",
                        cam_id[:8],
                        exc,
                    )

            t = self.hass.async_create_task(_close_leftover_proxy())
            self.bg_tasks.add(t)
            t.add_done_callback(self.bg_tasks.discard)

        # Same rationale as the `tls_proxy_servers` block above: the
        # viewing front-door's *listener* lives inside
        # `viewing_front_door_runner` (a single shared object across all
        # cameras), not in `viewing_sticky_port` (which is just the plain
        # int port number, safe for the generic pop-loop below). If a
        # camera is removed while its front-door is still bound, it must be
        # explicitly stopped here or the listener leaks for the rest of the
        # HA process lifetime, same as an un-stopped `tls_proxy_servers`
        # entry would.
        if cam_id in self.viewing_sticky_port:

            async def _close_leftover_viewing_front_door() -> None:
                # Bug-hunt finding: every OTHER mutator of the viewing
                # front-door state (start_viewing_front_door via
                # try_live_connection_inner, stop_viewing_front_door via
                # tear_down_live_stream) runs under the per-cam stream lock
                # — this purge-triggered stop is the sole caller that used
                # to run unlocked. Without the lock, a concurrent renewal
                # racing this purge could re-insert a fresh listener +
                # `viewing_sticky_port[cam_id]` entry for a camera that was
                # just confirmed gone from the Bosch cloud account (TOCTOU:
                # purge synchronously popped `viewing_sticky_port` above/
                # below this block, then the concurrent renewal's
                # `start_viewing_front_door` re-adds it AFTER this stop
                # already ran) — orphaning a bound socket for the rest of
                # the HA process lifetime with nothing left to ever purge
                # it again. Taking the lock here serializes against that
                # renewal exactly like `tear_down_live_stream` already does.
                try:
                    async with self.get_stream_lock(cam_id):
                        await self.stop_viewing_front_door(cam_id)
                        # Re-pop defensively: if a renewal was mid-flight
                        # and re-inserted this entry while we waited for
                        # the lock, it must not survive a confirmed purge.
                        self.viewing_sticky_port.pop(cam_id, None)
                except Exception as exc:
                    _LOGGER.debug(
                        "Viewing front-door for %s: stop during camera-removal purge raised — %s",
                        cam_id[:8],
                        exc,
                    )

            t2 = self.hass.async_create_task(_close_leftover_viewing_front_door())
            self.bg_tasks.add(t2)
            t2.add_done_callback(self.bg_tasks.discard)

        # Same again for the REMOTE viewing front-door's listener — separate
        # runner (`remote_viewing_front_door_runner`), same leak-if-
        # unstopped / lock-against-a-racing-renewal reasoning as the LOCAL
        # block immediately above.
        if cam_id in self.remote_viewing_sticky_port:

            async def _close_leftover_remote_viewing_front_door() -> None:
                try:
                    async with self.get_stream_lock(cam_id):
                        await self.stop_remote_viewing_front_door(cam_id)
                        self.remote_viewing_sticky_port.pop(cam_id, None)
                except Exception as exc:
                    _LOGGER.debug(
                        "REMOTE viewing front-door for %s: stop during "
                        "camera-removal purge raised — %s",
                        cam_id[:8],
                        exc,
                    )

            t3 = self.hass.async_create_task(
                _close_leftover_remote_viewing_front_door()
            )
            self.bg_tasks.add(t3)
            t3.add_done_callback(self.bg_tasks.discard)

        for attr_name in self._PURGE_CAM_DICT_ATTRS:
            attr = getattr(self, attr_name)
            attr.pop(cam_id, None)
        for attr_name in self._PURGE_CAM_SET_ATTRS:
            attr = getattr(self, attr_name)
            attr.discard(cam_id)
        # Tuple-keyed by (cam_id, opcode_hex) — filter on the cam_id half.
        stale_lan_denied_keys = [
            key for key in self._rcp_lan_denied_until if key[0] == cam_id
        ]
        for key in stale_lan_denied_keys:
            self._rcp_lan_denied_until.pop(key, None)

    def cleanup_stale_devices(self, current_cam_ids: set[str]) -> None:
        """Remove devices for cameras no longer in the Bosch cloud account.

        Quality-Scale Gold rule `stale-devices`. Compares the device registry
        against the freshly-fetched camera list — anything tied to our domain
        with a cam_id that disappeared gets removed (entities + device entry).
        Without this, a camera removed from the Bosch app stays visible in HA
        as `unavailable` forever. Also purges every per-cam_id coordinator
        dict/set entry for the removed camera (see `_purge_cam_id`) so those
        do not grow unbounded across camera swaps/renames over the lifetime
        of this coordinator instance.
        """
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(dev_reg, self.entry.entry_id):
            cam_id = next(
                (ident[1] for ident in device.identifiers if ident[0] == DOMAIN),
                None,
            )
            if cam_id and cam_id not in current_cam_ids:
                _LOGGER.info(
                    "Removing stale device for camera %s (no longer in Bosch cloud account)",
                    cam_id[:8],
                )
                dev_reg.async_remove_device(device.id)
                self._purge_cam_id(cam_id)

    # ── Live stream safety guards ────────────────────────────────────────────
    # Prevents concurrent stream setup, privacy toggles during warm-up, etc.
    # _stream_setup_lock: per-camera asyncio.Lock to serialize stream operations
    # stream_warming: set of cam_ids currently in warm-up phase (blocks privacy toggles)

    def get_stream_lock(self, cam_id: str) -> asyncio.Lock:
        """Get or create per-camera stream setup lock."""
        return get_or_create_lock(self._stream_locks, cam_id)

    def _get_rcp_session_lock(self, proxy_hash: str) -> asyncio.Lock:
        """Get or create per-proxy_hash RCP session-open lock."""
        return get_or_create_lock(self.rcp_session_locks, proxy_hash)

    def get_nvr_recorder_lock(self, cam_id: str) -> asyncio.Lock:
        """Get or create per-camera Mini-NVR recorder-spawn lock."""
        return get_or_create_lock(self._nvr_recorder_locks, cam_id)

    def get_nvr_clip_assembly_lock(self, cam_id: str) -> asyncio.Lock:
        """Get or create per-camera Mini-NVR motion-clip-assembly lock."""
        return get_or_create_lock(self._nvr_clip_assembly_locks, cam_id)

    def get_session(self, cam_id: str) -> CameraSessionState:
        """Get or create per-camera session bookkeeping (generation counter,
        idle-reaper timestamp, stream-warmup timestamp — see session_state.py)."""
        return get_or_create_session(self._sessions, cam_id)

    def clear_stream_warming(self, cam_id: str) -> None:
        """Force-clear the stream-warming flag for a camera.

        Used by is_stream_warming() when the flag is stale (live_connections
        no longer has the cam_id, so the warm-up must have completed or
        errored out without resetting the flag).
        """
        self.stream_warming.discard(cam_id)

    def is_stream_warming(self, cam_id: str) -> bool:
        """True if this camera is currently in the warm-up phase.

        Auto-clears stale flags in three scenarios:
          1. cam_id in `stream_warming` but NOT in `live_connections` — the
             previous warm-up errored out without resetting the flag.
          2. cam_id in `stream_warming` AND `live_connections[cam_id]` has
             a non-empty `rtspsUrl` — pre-warm actually completed, the flag
             just wasn't discarded (race in `try_live_connection_inner` exit
             paths). Observed on Gen1 Outdoor + Gen1 Indoor cams
             during simultaneous 4-camera toggle: state stuck at
             `warming_up` with `live_rtsps=null` for >7 min while keepalive
             was already running (gen=2, 480s into session).
          3. cam_id in `stream_warming` for >300 s — hard timeout. Pre-warm
             worst case is ~120 s (CAMERA_EYES outdoor 8 retries × 15 s).
             Anything longer is stuck — clear and let the next toggle reset
             cleanly rather than blocking privacy/snapshot UI forever.
        """
        import time as _time

        if cam_id not in self.stream_warming:
            return False
        # Scenario 1: warming flag without live_connections entry
        if cam_id not in self.live_connections:
            _LOGGER.debug(
                "Clearing stale stream-warming flag for %s (no live conn)", cam_id[:8]
            )
            self.stream_warming.discard(cam_id)
            self.get_session(cam_id).warming_started = float("-inf")
            return False
        live = self.live_connections.get(cam_id, {})
        # Scenario 2: warming flag but pre-warm actually finished (URL set)
        if live.get("rtspsUrl") or live.get("rtspUrl"):
            _LOGGER.debug(
                "Clearing stale stream-warming flag for %s (rtspsUrl already set — race)",
                cam_id[:8],
            )
            self.stream_warming.discard(cam_id)
            self.get_session(cam_id).warming_started = float("-inf")
            return False
        # Scenario 3: warming for >180 s — hard timeout. Pre-warm worst case is
        # ~150 s (CAMERA_EYES outdoor: 8 retries × 13 s + 35 s min_total_wait +
        # buffer). 180 s leaves a small safety margin without holding the
        # privacy toggle hostage for 5 minutes on a stuck warm-up.
        # -inf (not 0) as the missing-key default (SENTINEL_RULE): an entry in
        # stream_warming with no start timestamp is an inconsistent state — treat
        # it as stuck and clear it rather than holding the privacy toggle hostage
        # forever (a `0` default is falsy and would skip the failsafe entirely).
        started = self.get_session(cam_id).warming_started
        elapsed = _time.monotonic() - started
        if elapsed > 180:
            _LOGGER.warning(
                "Clearing stuck stream-warming flag for %s (warming for %s)",
                cam_id[:8],
                f"{elapsed:.0f}s" if started != float("-inf") else "unknown duration",
            )
            self.stream_warming.discard(cam_id)
            self.get_session(cam_id).warming_started = float("-inf")
            return False
        return True

    # ── Live stream ───────────────────────────────────────────────────────────
    async def try_live_connection(
        self, cam_id: str, is_renewal: bool = False, force_reset: bool = False
    ) -> dict[str, Any] | None:
        """
        Open a live proxy connection via PUT /v11/video_inputs/{id}/connection.
        Uses "REMOTE" (confirmed working) → cloud proxy, fast (~1.5s).
        On success stores:
          - proxyUrl:  https://proxy-NN:42090/{hash}/snap.jpg  (current image, no auth)
          - rtspsUrl:  rtsps://proxy-NN:443/{hash}/rtsp_tunnel?... (30fps H.264+AAC audio)
        Returns the enriched response dict, or None on failure.
        Serialized per camera via asyncio.Lock to prevent concurrent setup.
        """
        # Privacy guard — fail-open if cache not yet populated at boot
        if bool(self.shc_state_cache.get(cam_id, {}).get("privacy_mode")):
            _LOGGER.info(
                "try_live_connection: privacy mode active for %s — stream blocked",
                cam_id[:8],
            )
            return None
        lock = self.get_stream_lock(cam_id)
        # A recovery rebuild (force_reset) must WAIT for the lock, never skip:
        # the teardown of the old proxy now happens INSIDE the lock (see
        # try_live_connection_inner) so a concurrent renewal/heartbeat can't
        # publish Stream/go2rtc against a port the recovery is about to kill.
        # is_renewal already waits. Only opportunistic (non-recovery) calls skip.
        if lock.locked() and not is_renewal and not force_reset:
            # Opportunistic de-dup: a non-renewal start for this camera is
            # already in flight (e.g. a second card, a Lovelace auto-open, or
            # the user toggling the switch while a play_stream is mid-setup).
            # Return the dedicated STREAM_START_SKIPPED sentinel — NOT None —
            # so the switch consumer does not mistake the skip for a real
            # failure and log "Live stream failed", drop the user's stream
            # intent, or record a (false) stream error that would wrongly
            # nudge the camera toward REMOTE fallback. The in-flight start
            # publishes the session. (Demoted to debug: this is normal under
            # concurrent access and was previously a spurious WARNING.)
            _LOGGER.debug(
                "try_live_connection: start already in progress for %s — "
                "coalescing into it",
                cam_id[:8],
            )
            return STREAM_START_SKIPPED
        # Pre-emptive: if go2rtc's `_supported_schemes` is stale (HA Core bug),
        # the post-stream watchdog reload would race against the card's caps
        # query and the card chooses HLS forever. Reload BEFORE pre-warm so by
        # the time HA's `async_refresh_providers` runs (on STREAM-feature flip)
        # the schemes are fresh. Throttled to once per hour.
        if not is_renewal:
            await self._ensure_go2rtc_schemes_fresh()
        async with lock:
            return await try_live_connection_inner(
                self, cam_id, is_renewal, force_reset
            )

    async def run_smb_cleanup_bg(self) -> None:
        """Run the SMB retention cleanup in the background without blocking the coordinator tick."""
        try:
            await self.hass.async_add_executor_job(sync_smb_cleanup, self)
        except Exception as err:
            _LOGGER.debug("SMB cleanup background task error: %s", err)

    # ── Mini-NVR plumbing (delegate to recorder.py) ──────────────────────────
    async def start_recorder(self, cam_id: str) -> None:
        """Spawn the per-camera ffmpeg recorder if the LAN-only gate is open.

        Called by `BoschNvrRecordingSwitch.async_turn_on` and from the
        connection-type/cred-rotation hooks below. Idempotent — replaces an
        existing recorder so a fresh URL is picked up.
        """
        # User-intent flag (consulted by the watcher's respawn check).
        self.nvr_user_intent[cam_id] = True
        if not nvr_recorder.should_record(self, cam_id, switch_on=True):
            conn_type = self.live_connections.get(cam_id, {}).get("_connection_type")
            if conn_type == "REMOTE":
                # Recording wants LOCAL, but the live session is
                # currently REMOTE (e.g. AUTO mode fell back because of a
                # stale LAN IP / weak WiFi / consecutive stream errors) —
                # this is the single most likely reason a user sees the
                # Mini-NVR switch/sensor go silently "unavailable" with no
                # explanation. Previously only a DEBUG log, easy to miss
                # entirely; make it discoverable.
                _LOGGER.warning(
                    "NVR start_recorder skipped for %s — live session is "
                    "REMOTE, not LOCAL (LAN-only by design). Recording will "
                    "resume automatically once the session is LOCAL again",
                    cam_id[:8],
                )
            else:
                _LOGGER.debug(
                    "NVR start_recorder skipped for %s — gate closed (LOCAL=%s online=%s)",
                    cam_id[:8],
                    conn_type,
                    self.is_camera_online(cam_id),
                )
            return
        await nvr_recorder.start_recorder(self, cam_id)

    async def stop_recorder(self, cam_id: str, *, clear_intent: bool = True) -> None:
        """Stop the per-camera ffmpeg recorder.

        ``clear_intent=False`` is used when the LAN drops out: we stop the
        running ffmpeg but keep the user-intent flag so the recorder restarts
        automatically when the LAN comes back.
        """
        if clear_intent:
            self.nvr_user_intent.pop(cam_id, None)
        await nvr_recorder.stop_recorder(self, cam_id)

    async def run_nvr_cleanup_bg(self) -> None:
        """Run NVR retention purge in an executor thread (called once per day)."""
        try:
            await self.hass.async_add_executor_job(nvr_recorder.sync_nvr_cleanup, self)
        except Exception as err:
            _LOGGER.debug("NVR cleanup background task error: %s", err)

    # ── go2rtc integration ────────────────────────────────────────────────────
    async def async_fetch_live_snapshot(
        self, cam_id: str, jpeg_size: int | None = None
    ) -> bytes | None:
        """Open a temporary REMOTE live connection to fetch a fresh snap.jpg.

        Does NOT register the connection in live_connections — the live stream
        switch stays OFF. Used by background image refresh so cameras always
        show a current image rather than a (possibly expired) event snapshot.

        ``jpeg_size`` overrides snap.jpg's resolution for callers that only need
        a preview (see const.jpeg_size_for_width). The default of ``None`` keeps
        JPEG_SIZE_FULL, so the background refresh, the image.* entity and the
        AI-analysis fetch — everything that persists or analyses the frame —
        are unaffected.

        Proxy URL caching: PUT /connection takes ~1.5s. The resulting proxy lease
        lasts ~60s. We cache urls[0] for 50s and skip PUT /connection on warm
        refreshes, reducing latency from ~3s → ~0.5s per card refresh cycle.

        Per-camera lock: concurrent callers (first-load + proactive refresh,
        Lovelace double-firing) are serialized so only one PUT /connection
        runs per camera at a time. The second caller finds the warm cache.
        """
        lock = get_or_create_lock(self._snapshot_fetch_locks, cam_id)
        async with lock:
            return await self._async_fetch_live_snapshot_impl(cam_id, jpeg_size)

    async def _async_fetch_live_snapshot_impl(
        self, cam_id: str, jpeg_size: int | None = None
    ) -> bytes | None:
        import json as _json

        snap_jpeg_size = jpeg_size or JPEG_SIZE_FULL

        token = self.token
        if not token:
            return None
        # Privacy short-circuit: when privacy mode is ON, the camera returns
        # snap.jpg with HTTP 200 and 0 bytes (camera blocks live frames while
        # the shutter / privacy mask is engaged). Skip the network call entirely
        # rather than burning a PUT /connection + snap.jpg round-trip every
        # coordinator tick (~5–8 calls per minute across 4 cameras) just to
        # log "empty response (privacy mode ON?)" each time. The camera entity
        # falls back to its cached frame or _PLACEHOLDER_JPEG. Detected via the
        # cached `privacy_mode` boolean populated in the same /v11/video_inputs
        # response (line 1386) — no extra request needed.
        # shc_state_cache is always initialized to {} in __init__ (line 300),
        # so the old getattr() guard for AttributeError is no longer needed.
        # Previous hotfix used _camera_status_extra (wrong attr — never assigned),
        # so the privacy short-circuit never fired; fixed here.
        if self.shc_state_cache.get(cam_id, {}).get("privacy_mode"):
            return None

        # Reuse the pooled, application-lifetime Bosch cloud session instead of
        # opening a fresh TCP+TLS connection on every snapshot poll (~5–8 calls/
        # min across 4 cameras). Connection pooling removes a full TLS handshake
        # per tick. The CM does NOT close the shared session.
        async with async_bosch_cloud_session_cm(self.hass) as session:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
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
                            cam_id,
                            expires_at - now,
                        )
                        return url_entry
                    del self._proxy_url_cache[cam_id]

                # Cache miss — call PUT /connection
                async with asyncio.timeout(TIMEOUT_PUT_CONNECTION):
                    async with session.put(
                        conn_url,
                        json={
                            "type": "REMOTE",
                            "highQualityVideo": self.get_quality_params(cam_id)[0],
                        },
                        headers=headers,
                    ) as resp:
                        if resp.status not in (200, 201):
                            _LOGGER.debug(
                                "fetch_live_snapshot: PUT /connection → HTTP %d for %s",
                                resp.status,
                                cam_id,
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
                        return str(urls[0])

            try:
                url_entry = await _get_proxy_url_entry()
                if not url_entry:
                    return None
                # Validate the host portion before it's used to build any
                # request URL (RCP 0x099e below, or the snap.jpg fetch
                # further down) — an unvalidated proxy host from Bosch's
                # own PUT /connection response is an SSRF path.
                _entry_host = url_entry.split("/", 1)[0]
                if not _is_safe_bosch_host(_entry_host):
                    _LOGGER.warning(
                        "Rejected unsafe RCP proxy host for %s: %s",
                        cam_id,
                        _entry_host[:60],
                    )
                    return None

                # ── RCP 0x099e: 320×180 JPEG (Gen1 only) ──
                # Gen1 (INDOOR/OUTDOOR/CAMERA_360) returns a JPEG via the proxy RCP
                # endpoint. Gen2 (HOME_Eyes_*) responds with non-JPEG payload —
                # 0x0a88 only reports the *configured* snapshot resolution, not that
                # 0x099e delivers bytes. Skip on Gen2 to silence log noise; snap.jpg
                # works uniformly.
                # Defensive getattr — hw_version is a real-coordinator attribute
                # set in __init__, but tests use ``SimpleNamespace`` stubs that
                # don't auto-populate dicts. Without the fallback every snapshot
                # test (~14 cases across test_init_round7, test_init_sprint_*)
                # raises AttributeError before reaching the gate logic.
                hw_gen2 = getattr(self, "hw_version", {}).get(cam_id, "") in (
                    "HOME_Eyes_Indoor",
                    "HOME_Eyes_Outdoor",
                )
                parts = url_entry.split("/", 1)
                probe_failed_until = self._rcp_099e_probe_failed_until.get(
                    cam_id, float("-inf")
                )
                probe_memoized_failed = time.monotonic() < probe_failed_until
                # RCP 0x099e only ever returns a fixed 320×180 JPEG — must
                # never be used to satisfy a full-resolution request (the
                # default jpeg_size=None), or camera.py would cache this
                # thumbnail as the shared full-res frame.
                if (
                    jpeg_size is not None
                    and len(parts) == 2
                    and not hw_gen2
                    and not probe_memoized_failed
                ):
                    proxy_host_rcp, proxy_hash_rcp = parts[0], parts[1]
                    rcp_base = f"https://{proxy_host_rcp}/{proxy_hash_rcp}/rcp.xml"
                    try:
                        # Hard-cap the probe — on cameras where it
                        # always fails (observed 2-8s per attempt, no timeout
                        # of its own), an unbudgeted probe starved the
                        # snap.jpg leg below of the time it needed to succeed.
                        async with asyncio.timeout(TIMEOUT_RCP_099E_PROBE):
                            session_id = await self.get_cached_rcp_session(
                                proxy_host_rcp, proxy_hash_rcp
                            )
                            raw = (
                                await self.rcp_read(rcp_base, "0x099e", session_id)
                                if session_id
                                else None
                            )
                        if raw and raw[:2] == b"\xff\xd8":
                            _LOGGER.debug(
                                "fetch_live_snapshot: RCP 0x099e → %d bytes (320×180 JPEG) for %s",
                                len(raw),
                                cam_id,
                            )
                            self._rcp_099e_probe_failed_until.pop(cam_id, None)
                            return raw
                        _LOGGER.debug(
                            "fetch_live_snapshot: RCP 0x099e unavailable for %s — using snap.jpg",
                            cam_id,
                        )
                        if session_id:
                            # Only memoize when we actually got a session and
                            # read a real (non-JPEG) response — a genuine
                            # signal this camera can't satisfy 0x099e. A
                            # missing session_id can be a transient
                            # session-acquisition hiccup (e.g. concurrent
                            # LOCAL-session contention) unrelated to hardware
                            # capability, so it must not disable the fast
                            # path for a full hour on a one-off blip.
                            self._rcp_099e_probe_failed_until[cam_id] = (
                                time.monotonic() + RCP_099E_PROBE_FAILURE_MEMO_SEC
                            )
                    except Exception as _rcp_err:
                        _LOGGER.debug(
                            "fetch_live_snapshot: RCP error for %s: %s — using snap.jpg",
                            cam_id,
                            _rcp_err,
                        )
                        self._rcp_099e_probe_failed_until[cam_id] = (
                            time.monotonic() + RCP_099E_PROBE_FAILURE_MEMO_SEC
                        )
                elif len(parts) == 2 and not hw_gen2 and probe_memoized_failed:
                    _LOGGER.debug(
                        "fetch_live_snapshot: RCP 0x099e memoized-failed for %s — skipping probe, using snap.jpg",
                        cam_id,
                    )

                proxy_url = f"https://{url_entry}/snap.jpg?JpegSize={snap_jpeg_size}"
                async with asyncio.timeout(TIMEOUT_SNAP):
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
                            if not _is_safe_bosch_host(url_entry2.split("/", 1)[0]):
                                _LOGGER.warning(
                                    "Rejected unsafe RCP proxy host for %s (retry)",
                                    cam_id,
                                )
                                return None
                            proxy_url2 = f"https://{url_entry2}/snap.jpg?JpegSize={snap_jpeg_size}"
                            async with asyncio.timeout(TIMEOUT_SNAP):
                                async with session.get(proxy_url2) as snap_resp2:
                                    ct2 = snap_resp2.headers.get("Content-Type", "")
                                    if snap_resp2.status == 200 and "image" in ct2:
                                        data2: bytes = await snap_resp2.read()
                                        if data2:
                                            return data2
                            return None
                        if snap_resp.status == 200 and "image" in ct:
                            data: bytes = await snap_resp.read()
                            # Bosch returns HTTP 200 with 0 bytes when privacy mode is ON.
                            # Cross-check the camera's "privacy is on"
                            # signal against HA's cached privacy state — if HA still thinks
                            # privacy is OFF, we have a state drift (toggled in the Bosch
                            # app, not yet reflected via cloud poll) and emit a WARNING.
                            if not data:
                                cam_raw = self.data.get(cam_id, {})
                                ha_privacy_on = (
                                    str(cam_raw.get("privacyMode", "")).upper() == "ON"
                                )
                                if ha_privacy_on:
                                    _LOGGER.debug(
                                        "fetch_live_snapshot: %s → empty response (privacy mode ON, HA agrees)",
                                        cam_id,
                                    )
                                else:
                                    _LOGGER.warning(
                                        "fetch_live_snapshot: %s → empty response but HA "
                                        "privacy state is OFF — state drift (likely toggled "
                                        "via Bosch app, cloud poll lag). Forcing refresh.",
                                        cam_id,
                                    )
                                    # Actually force the refresh the message
                                    # promises: pull fresh privacy state from the
                                    # cloud now instead of waiting up to a full
                                    # poll interval. Without this the switch stays
                                    # visually wrong and this WARNING repeats on
                                    # every snapshot until the next poll. The
                                    # coordinator debouncer coalesces repeats.
                                    # Tracked (not a bare hass.async_create_task)
                                    # — otherwise this can outlive config-entry
                                    # unload and keep running against an
                                    # already-torn-down coordinator.
                                    self.spawn_tracked(
                                        self.async_request_refresh(),
                                        name="bosch_shc_camera_privacy_drift_refresh",
                                    )
                                return None
                            _LOGGER.debug(
                                "fetch_live_snapshot: %s → %d bytes", cam_id, len(data)
                            )
                            return data
                        _LOGGER.debug(
                            "fetch_live_snapshot: snap.jpg → HTTP %d for %s",
                            snap_resp.status,
                            cam_id,
                        )
                        return None

            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug("fetch_live_snapshot error for %s: %s", cam_id, err)
                return None

    def _ai_window_allowed(self) -> bool:
        """Time-window + condition-entity gate for AUTO AI analyses.

        Returns True if the current moment is within the configured activation
        window AND the condition entity (if any) is in the expected state.
        When neither gate is configured, always returns True.
        Manual force=True callers MUST bypass this — callers are responsible.
        """
        opts = self.options
        time_start_raw: str = (opts.get("ai_active_time_start") or "").strip()
        time_end_raw: str = (opts.get("ai_active_time_end") or "").strip()
        condition_entity_id: str = (
            opts.get("ai_active_condition_entity") or ""
        ).strip()
        condition_state: str = (
            opts.get("ai_active_condition_state") or "not_home"
        ).strip()

        time_gate_active = bool(time_start_raw and time_end_raw)
        if bool(time_start_raw) != bool(time_end_raw):
            _LOGGER.warning(
                "AI activation window: only one of start/end time is configured"
                " (start=%r end=%r) — time gate disabled. Set both or neither.",
                time_start_raw,
                time_end_raw,
            )
        condition_gate_active = bool(condition_entity_id)

        if not time_gate_active and not condition_gate_active:
            return True

        time_allowed = True
        if time_gate_active:
            try:
                from datetime import time as _dt_time

                def _parse_t(s: str) -> _dt_time:
                    parts = s.split(":")
                    h, m = int(parts[0]), int(parts[1])
                    sec = int(parts[2]) if len(parts) > 2 else 0
                    return _dt_time(h, m, sec)

                t_start = _parse_t(time_start_raw)
                t_end = _parse_t(time_end_raw)
                now_t = dt_util.now().time().replace(microsecond=0)
                if t_end >= t_start:
                    # Normal window: e.g. 08:00–22:00. start==end is a zero-width
                    # window (allowed only at that exact second) — matches live.
                    time_allowed = t_start <= now_t <= t_end
                else:
                    # Overnight window: e.g. 22:00–06:00
                    time_allowed = now_t >= t_start or now_t <= t_end
            except Exception:
                _LOGGER.debug(
                    "AI activation window: malformed time value (start=%r end=%r)"
                    " — treating as no time gate",
                    time_start_raw,
                    time_end_raw,
                )
                time_allowed = True  # malformed → allow (fail-open)

        condition_allowed = True
        if condition_gate_active:
            state_obj = self.hass.states.get(condition_entity_id)
            if state_obj is None or state_obj.state in ("unknown", "unavailable"):
                condition_allowed = False  # conservative: don't burn credits
                _LOGGER.debug(
                    "AI activation window: condition entity %s is %s — blocking AI",
                    condition_entity_id,
                    state_obj.state if state_obj else "missing",
                )
            else:
                condition_allowed = state_obj.state == condition_state

        return time_allowed and condition_allowed

    def ai_budget_state(self) -> tuple[int, int]:
        """Return (used_today, max_per_day) for the AI-analysis daily budget.

        Rolls the counter over when the local calendar date changes.
        max_per_day == 0 means unlimited.
        """
        opts = self.options
        try:
            max_per_day = int(opts.get("ai_max_per_day", 100) or 0)
        except (TypeError, ValueError):
            max_per_day = 100
        today = dt_util.now().date().isoformat()
        if self._ai_day_stamp != today:
            self._ai_day_stamp = today
            self._ai_day_count = 0
            self.hass.async_create_task(self._async_save_ai_budget())
        return self._ai_day_count, max_per_day

    async def async_load_ai_budget(self) -> None:
        """Load persisted daily AI budget from storage (called on setup)."""
        try:
            stored = await self._ai_budget_store.async_load()
        except Exception as err:
            _LOGGER.debug("AI budget store load failed: %s", err)
            stored = None
        if isinstance(stored, dict):
            stored_date: str = stored.get("date", "")
            today = dt_util.now().date().isoformat()
            if stored_date == today:
                try:
                    self._ai_day_count = int(stored.get("count", 0))
                    self._ai_day_stamp = stored_date
                except (TypeError, ValueError):
                    pass
            # else: stored day != today → counter stays at 0 (already reset for new day)

    async def _async_save_ai_budget(self) -> None:
        """Persist daily AI budget count to storage."""
        try:
            await self._ai_budget_store.async_save(
                {
                    "date": self._ai_day_stamp,
                    "count": self._ai_day_count,
                }
            )
        except Exception as err:
            _LOGGER.debug("AI budget store save failed: %s", err)

    def _ai_rate_allowed(self, cam_id: str) -> bool:
        """Cooldown + daily-budget gate for AUTO AI analyses."""
        opts = self.options
        try:
            cooldown = float(opts.get("ai_cooldown_seconds", 60) or 0)
        except (TypeError, ValueError):
            cooldown = 60.0
        used, max_per_day = self.ai_budget_state()
        if max_per_day and (used + self.ai_in_flight) >= max_per_day:
            # Use the SAME local-date source as ai_budget_state() above so the
            # one-shot "budget reached" log re-arms in lockstep with the daily
            # counter reset (a UTC date here would suppress the log for the
            # hours between local and UTC midnight). Lesson: events-today UTC bug.
            today = dt_util.now().date().isoformat()
            if self._ai_budget_logged_day != today:
                self._ai_budget_logged_day = today
                _LOGGER.info(
                    "AI analysis daily budget of %d reached — skipping until tomorrow",
                    max_per_day,
                )
            return False
        last = self._ai_last_call.get(cam_id, float("-inf"))
        return (time.monotonic() - last) >= cooldown

    def ai_record_call(self, cam_id: str) -> None:
        """Record an AI analysis for cooldown + daily-budget accounting."""
        self.ai_budget_state()  # ensure the day-rollover runs first
        self._ai_last_call[cam_id] = time.monotonic()
        self._ai_day_count += 1
        self.hass.async_create_task(self._async_save_ai_budget())

    async def async_generate_ai_description(
        self, cam_id: str, *, force: bool = False
    ) -> str | None:
        """Generate an AI description of a camera's current snapshot via ai_task.

        Shared by the notify-include path (F2) and the on-motion auto path.
        Returns the description text, or None when skipped (rate-limited,
        camera unknown, ai_task unavailable, or empty result). Auto callers
        pass force=False so the cooldown + daily budget apply; manual/service
        callers pass force=True to bypass the cooldown (still counts toward
        the daily budget). Never raises — failures return None so the calling
        notification/event path is never broken.
        """
        if not self.options.get("enable_ai_description", False):
            return None
        if self.shc_state_cache.get(cam_id, {}).get("privacy_mode"):
            return None
        if not force and not self._ai_window_allowed():
            return None
        if not force and not self._ai_rate_allowed(cam_id):
            # Reuse cached description only if not stale and not from a privacy era
            cached_entry = self.data.get(cam_id, {}).get("ai_description", {})
            cached_text: str | None = cached_entry.get("text")
            if cached_text and not self.shc_state_cache.get(cam_id, {}).get(
                "privacy_mode"
            ):
                # Reject cache if generated_at is older than cooldown window or 300s cap
                try:
                    opts_cs = self.options
                    cooldown_secs = float(opts_cs.get("ai_cooldown_seconds", 60) or 0)
                    max_age = min(cooldown_secs, 300.0)
                    gen_at_str: str | None = cached_entry.get("generated_at")
                    if gen_at_str:
                        gen_dt = datetime.fromisoformat(gen_at_str)
                        age_secs = (datetime.now(UTC) - gen_dt).total_seconds()
                        if max_age > 0 and age_secs <= max_age:
                            return cached_text
                except Exception as _cache_err:
                    _LOGGER.debug("AI cache staleness check failed: %s", _cache_err)
            return None
        cam_entity = getattr(self, "camera_entities", {}).get(cam_id)
        if cam_entity is None:
            return None
        entity_id = cam_entity.entity_id
        opts = self.options
        prompt = opts.get("ai_describe_prompt") or (
            "Du bist eine Überwachungskamera-Assistenz. Melde NUR"
            " sicherheitsrelevante Beobachtungen: Personen (auch nur teilweise"
            " sichtbar: Beine, Arme, Silhouette, Schatten), Fahrzeuge, Tiere,"
            " Pakete oder ungewöhnliche Aktivität. Beschreibe NICHT die"
            " Umgebung, Räume, Möbel, Architektur oder Bildqualität und benenne"
            " KEINE Orte. Rate nicht: Fußmatten, Teppiche, Bodenfliesen und"
            " Schatten sind kein Paket. Wenn nichts Sicherheitsrelevantes"
            " erkennbar ist, sage das kurz, z. B.: Keine"
            " sicherheitsrelevanten Beobachtungen."
        )
        language = (opts.get("ai_describe_language") or "").strip() or "Deutsch"
        full_instructions = (
            f"{prompt}\n\nRespond only in {language}."
            f" Antworte ausschließlich auf {language}."
        )
        ai_task_entity = (opts.get("ai_task_entity") or "").strip()
        ai_call_data: dict[str, Any] = {
            "task_name": "Bosch camera snapshot",
            "instructions": full_instructions,
            "attachments": [
                {
                    "media_content_id": f"media-source://camera/{entity_id}",
                    "media_content_type": "image/jpeg",
                }
            ],
        }
        if ai_task_entity:
            ai_call_data["entity_id"] = ai_task_entity
        self.ai_in_flight += 1
        _ai_resp: Any = None
        _text_result: str | None = None
        try:
            async with asyncio.timeout(20):
                _ai_resp = await self.hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    ai_call_data,
                    blocking=True,
                    return_response=True,
                )
            if _ai_resp is not None:
                _text_candidate = (
                    str(_ai_resp.get("data", ""))
                    if isinstance(_ai_resp, dict)
                    else str(_ai_resp or "")
                ).strip()
                if _text_candidate:
                    _text_result = _text_candidate
                    # Record the call while ai_in_flight is still 1 so the
                    # budget counter reflects in-progress work correctly.
                    self.ai_record_call(cam_id)
        except TimeoutError:
            _LOGGER.debug("AI description timed out (20s) for %s", cam_id[:8])
        except Exception as err:
            _LOGGER.debug("AI description generate failed for %s: %s", cam_id[:8], err)
        finally:
            self.ai_in_flight -= 1
        if _text_result is None:
            return None
        text = _text_result
        generated_at = datetime.now(UTC).isoformat()
        if cam_id in self.data:
            self.data[cam_id]["ai_description"] = {
                "text": text,
                "generated_at": generated_at,
                "ai_task_entity": ai_task_entity or "default",
            }
            self.async_set_updated_data(self.data)
        self.hass.bus.async_fire(
            "bosch_shc_camera_ai_description",
            {
                "camera_id": cam_id,
                "entity_id": entity_id,
                "description": text,
                "generated_at": generated_at,
            },
        )
        return text

    async def async_fetch_fresh_event_snapshot(self, cam_id: str) -> bytes | None:
        """Fetch fresh events from Bosch API and return the latest event JPEG.

        Used as fallback for cameras whose snap.jpg returns 401 (e.g. CAMERA_360).
        Bypasses the coordinator's cached event list — always hits Bosch API directly
        so the returned imageUrl is always fresh (not expired).

        Concurrent callers for the same cam_id are coalesced: the first caller
        acquires the per-camera lock, fetches, and stores the result in
        `_fresh_snap_cache`; subsequent callers that arrive while the first is
        in-flight wait on the lock and then return the cached result without an
        additional network round-trip. This prevents 8+ duplicate cloud requests
        after an FCM push wakes all HA consumers simultaneously.
        """
        # Local import (not top-level): keeps unittest.mock.patch(
        # "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
        # ...) working the same way it did before BoschCameraCoordinator
        # moved out of __init__.py — those patches target the package's own
        # namespace, matching the pattern already used in live_connection.py.
        from . import (
            async_get_bosch_cloud_session as async_get_bosch_cloud_session,
        )

        # Fast path: cache hit without acquiring the lock (hot path after first fetch)
        cached = self._fresh_snap_cache.get(cam_id)
        if cached:
            data, expiry = cached
            if time.monotonic() < expiry:
                return data

        token = self.token
        if not token:
            return None

        # Slow path: serialise concurrent fetches for the same camera
        lock = get_or_create_lock(self._fresh_snap_locks, cam_id)
        async with lock:
            # Re-check cache now that we hold the lock — a concurrent caller that
            # raced through the fast-path miss and waited here may have already
            # populated the cache while we were queued.
            cached = self._fresh_snap_cache.get(cam_id)
            if cached:
                data, expiry = cached
                if time.monotonic() < expiry:
                    return data

            session = await async_get_bosch_cloud_session(self.hass)
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            events_url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}"

            try:
                async with asyncio.timeout(15):
                    async with session.get(events_url, headers=headers) as resp:
                        if resp.status != 200:
                            _LOGGER.debug(
                                "fetch_fresh_event_snapshot: events HTTP %d for %s",
                                resp.status,
                                cam_id,
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
                    if not _is_safe_bosch_url(img_url):
                        _LOGGER.warning("Unsafe imageUrl rejected: %s", img_url[:60])
                        continue
                    try:
                        async with asyncio.timeout(20):
                            # allow_redirects=False: _is_safe_bosch_url only
                            # validates img_url itself — aiohttp follows
                            # redirects by default, so a validated URL could
                            # still redirect to an arbitrary internal host
                            # (same fix already applied to camera.py's
                            # equivalent event-snapshot fetch).
                            async with session.get(
                                img_url, headers=img_headers, allow_redirects=False
                            ) as snap_resp:
                                if snap_resp.status == 200:
                                    evdata: bytes = await snap_resp.read()
                                    if evdata:
                                        _LOGGER.debug(
                                            "fetch_fresh_event_snapshot: %s → %d bytes @ %s",
                                            cam_id,
                                            len(evdata),
                                            ev.get("timestamp", "")[:19],
                                        )
                                        self._fresh_snap_cache[cam_id] = (
                                            evdata,
                                            time.monotonic() + _FRESH_SNAP_TTL,
                                        )
                                        return evdata
                    except (TimeoutError, aiohttp.ClientError):
                        continue

            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug(
                    "fetch_fresh_event_snapshot error for %s: %s", cam_id, err
                )

            return None

    async def async_fetch_live_snapshot_local(
        self, cam_id: str, jpeg_size: int | None = None
    ) -> bytes | None:
        """Fetch a live snapshot via LOCAL connection using HTTP Digest auth.

        For cameras like CAMERA_360 whose REMOTE snap.jpg returns 401,
        this opens a LOCAL connection to get Digest credentials and fetches
        snap.jpg directly from the camera's LAN IP.

        Uses auth_utils.async_digest_request (aiohttp) for non-blocking Digest auth.

        ``jpeg_size`` behaves as in async_fetch_live_snapshot: ``None`` keeps
        the full-resolution frame the persisting callers expect.
        """
        token = self.token
        if not token:
            return None
        # Same privacy short-circuit as the REMOTE fetch — the LAN snap.jpg
        # also returns 0 bytes when privacy mode is ON. shc_state_cache is
        # always initialized to {} in __init__, no getattr guard needed.
        if self.shc_state_cache.get(cam_id, {}).get("privacy_mode"):
            return None

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        result = None
        try:
            # Reuse the pooled cloud session instead of opening a fresh
            # connector/TLS handshake on every LOCAL-bootstrap attempt.
            async with async_bosch_cloud_session_cm(self.hass) as session:
                async with asyncio.timeout(15):
                    async with session.put(
                        url,
                        json={
                            "type": "LOCAL",
                            "highQualityVideo": self.get_quality_params(cam_id)[0],
                        },
                        headers=headers,
                    ) as resp:
                        if resp.status not in (200, 201):
                            _LOGGER.debug(
                                "fetch_live_snapshot_local: PUT LOCAL → HTTP %d for %s",
                                resp.status,
                                cam_id,
                            )
                            return None
                        import json as _json

                        result = _json.loads(await resp.text())
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug(
                "fetch_live_snapshot_local: PUT error for %s: %s", cam_id, err
            )
            return None

        user = result.get("user")
        password = result.get("password")
        urls = result.get("urls", [])
        if not user or not password or not urls:
            _LOGGER.debug(
                "fetch_live_snapshot_local: missing credentials/urls for %s "
                "(has_user=%s, has_password=%s, urls=%d)",
                cam_id,
                bool(user),
                bool(password),
                len(urls),
            )
            return None

        camera_host = urls[0]  # e.g. "192.168.x.x:443"
        if not _is_safe_local_camera_host(camera_host):
            _LOGGER.warning(
                "Rejected unsafe/malformed LOCAL camera host for %s: %s",
                cam_id,
                camera_host[:60],
            )
            return None
        snap_url = (
            f"https://{camera_host}/snap.jpg?JpegSize={jpeg_size or JPEG_SIZE_FULL}"
        )

        # This snapshot-fallback-specific PUT /connection LOCAL is a
        # separate credential issuance from the streaming LOCAL-session
        # paths (live_connection.py/session_renewal.py) — cache these too,
        # or a cloud outage right after using only this fallback (never a
        # live stream) leaves local_creds_cache empty.
        _host, _, _port_str = camera_host.partition(":")
        self.local_creds_cache[cam_id.upper()] = {
            "user": user,
            "password": password,
            "host": _host,
            "port": int(_port_str) if _port_str.isdigit() else 443,
            "ts": time.monotonic(),
        }

        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        # Local import (not top-level): keeps unittest.mock.patch(
        # "custom_components.bosch_shc_camera.async_digest_request", ...)
        # working the same way it did before BoschCameraCoordinator moved
        # out of __init__.py, matching _fetch_rcp_lan's identical pattern
        # below and the live_connection.py precedent — matters here even
        # though only one call site remains, so a future third caller isn't
        # tempted to reintroduce the top-level-only inconsistency.
        from . import (
            async_digest_request as async_digest_request,
        )

        session = async_get_clientsession(self.hass, verify_ssl=False)
        try:
            async with asyncio.timeout(12):
                async with await async_digest_request(
                    session,
                    "GET",
                    snap_url,
                    user,
                    password,
                    timeout=10.0,
                    ssl=False,
                ) as resp:
                    if resp.status == 200 and "image" in resp.headers.get(
                        "Content-Type", ""
                    ):
                        content: bytes = await resp.read()
                        _LOGGER.debug(
                            "fetch_live_snapshot_local: %s → %d bytes via Digest",
                            cam_id,
                            len(content),
                        )
                        return content
                    _LOGGER.debug(
                        "fetch_live_snapshot_local: Digest snap.jpg → HTTP %d for %s",
                        resp.status,
                        cam_id,
                    )
        except (TimeoutError, aiohttp.ClientError, ValueError) as err:
            # ValueError: malformed/missing WWW-Authenticate (cam Digest state
            # may be half-rotated during FCM flap). Forum 998974/15 (Andrew75).
            _LOGGER.debug(
                "fetch_live_snapshot_local: aiohttp error for %s: %s", cam_id, err
            )
        return None

    # ── Local / Cloud-Proxy RCP+ READ helpers ────────────────────────────────
    async def _rcp_read_active(self, cam_id: str, command: str, type_: str) -> Any:
        """Read an RCP+ field via the currently active stream session.

        Dispatches LOCAL (digest auth + cam IP) vs REMOTE (basic-empty + proxy
        hash) based on `live_connections[cam_id]._connection_type`. Returns the
        parsed value (int / bytes / str depending on `type_`) or None when no
        session is active or the read fails. Never raises.

        Designed for opportunistic reads — never triggers a fresh PUT /connection
        (would cred-rotate on Gen2 Outdoor and break the running stream).
        """
        live = self.live_connections.get(cam_id, {})
        if not live:
            return None
        conn_type = live.get("_connection_type")
        if conn_type == "LOCAL":
            user = live.get("_local_user")
            pwd = live.get("_local_password")
            urls = live.get("urls", [])
            if not user or not pwd or not urls:
                return None
            host = urls[0]  # "192.168.x.x:443"
            from bosch_shc_camera_client.local_rcp import rcp_read_local_sync

            return await self.hass.async_add_executor_job(
                rcp_read_local_sync, host, user, pwd, command, type_
            )
        if conn_type == "REMOTE":
            urls = live.get("urls", [])
            if not urls:
                return None
            # Cloud-Proxy URL form: "proxy-XX.live.cbs.boschsecurity.com:42090/{hash}"
            proxy_with_hash = urls[0]
            from bosch_shc_camera_client.local_rcp import rcp_read_remote_sync

            return await self.hass.async_add_executor_job(
                rcp_read_remote_sync, proxy_with_hash, command, type_
            )
        return None

    async def refresh_rcp_state(self, cam_id: str) -> None:
        """Hook fired after a successful stream start. Currently a no-op marker.

        Earlier versions (v10.4.8) read RCP `0x0d00` and `0x0c22` here and
        interpreted them as privacy-mode and LED-dimmer state. A/B testing
        proved both interpretations were wrong (0x0d00 byte[1] stayed 1
        independent of the privacy toggle, so it is NOT the mode flag), so
        the reads were removed in v10.4.9. The hook itself is kept as a
        cheap extension point for future verified RCP+ uses.
        """
        live = self.live_connections.get(cam_id, {})
        source = live.get("_connection_type", "?").lower()
        cache = self._rcp_state_cache.setdefault(cam_id, {})

        # NOTE: 0x0d00 P_OCTET (4 bytes) was previously read here as
        # "privacy_mode" via byte[1]==1, but A/B testing against the real
        # camera proved this byte is NOT the privacy-mode toggle — it stays at 1 even
        # when privacy is OFF, so it likely reflects a static mask-config
        # or some other always-on indicator. The Bosch cloud
        # `/v11/video_inputs.privacyMode` field is the correct source of
        # truth and was never the lie I thought it was. Reverted in v10.4.9.
        #
        # 0x0c22 T_WORD was likewise read as "led_dimmer 0-100" but its
        # semantics are unverified vs. the actual user-facing dimmer.
        # Pulled out until properly mapped against ground-truth.
        if cache:
            cache["source"] = source
            cache["fetched_at"] = time.monotonic()

    async def check_and_recover_webrtc(self, cam_id: str) -> None:
        """Watchdog for HA's bundled go2rtc WebRTCProvider stale-schemes bug.

        HA's `WebRTCProvider.initialize()` runs once at config-entry-load and
        caches `_supported_schemes` from `/api/schemes`. The bundled go2rtc
        binary can be respawned by HA's watchdog (server.py) when it crashes
        or its API stops responding — but the Python provider instance keeps
        running with whatever schemes it had at boot. If `initialize()` ever
        returned an empty set (race during boot), the camera entity is stuck
        advertising only HLS forever, even though go2rtc itself is fine.

        Symptom: `camera.camera_capabilities.frontend_stream_types == {HLS}`
        instead of `{HLS, WEB_RTC}` while a stream is active. WebRTC offers
        from the card get rejected with `webrtc_offer_failed: Camera does not
        support WebRTC`.

        Recovery: reload the bundled go2rtc config entry — `async_setup_entry`
        re-runs `provider.initialize()` and refreshes `_supported_schemes`.

        Throttled to once per hour per integration entry to avoid reload-spam
        if go2rtc is genuinely broken (e.g. binary won't start). Skipped when
        a recent reload already happened.
        """
        await asyncio.sleep(2)  # let async_refresh_providers settle first
        cam_entity = self.camera_entities.get(cam_id)
        if cam_entity is None:
            return
        from homeassistant.components.camera import CameraEntityFeature, StreamType

        if CameraEntityFeature.STREAM not in cam_entity.supported_features:
            return  # stream_source not yet ready, nothing to check
        try:
            caps = cam_entity.camera_capabilities
            if StreamType.WEB_RTC in caps.frontend_stream_types:
                return  # all good
        except Exception as err:
            _LOGGER.debug("webrtc-watchdog: capabilities probe failed: %s", err)
            return
        # First-line recovery: direct-refresh `_supported_schemes` on the
        # existing provider + push refresh_providers to all streaming cams.
        # This is much cheaper than reloading the whole config entry and
        # usually does the job (the schemes are already populated, the cams
        # just need to re-query the providers).
        try:
            self.last_schemes_refresh = float(
                "-inf"
            )  # force next _ensure_go2rtc_schemes_fresh past the 600s throttle
            await self._ensure_go2rtc_schemes_fresh()
            cam_entity.invalidate_camera_capabilities_cache()
            caps2 = cam_entity.camera_capabilities
            if StreamType.WEB_RTC in caps2.frontend_stream_types:
                _LOGGER.info(
                    "webrtc-watchdog: WEB_RTC restored for %s via direct schemes-refresh",
                    cam_id[:8],
                )
                return
        except Exception as err:
            _LOGGER.debug("webrtc-watchdog: direct refresh failed: %s", err)
        now = time.monotonic()
        if not hasattr(self, "_last_go2rtc_reload"):
            self._last_go2rtc_reload = float("-inf")
        if now - self._last_go2rtc_reload < 3600:
            return  # already reloaded recently — don't spam
        from homeassistant.config_entries import ConfigEntryState

        go2rtc_entries = [
            e
            for e in self.hass.config_entries.async_entries("go2rtc")
            if e.state == ConfigEntryState.LOADED
        ]
        if not go2rtc_entries:
            _LOGGER.debug("webrtc-watchdog: no loaded go2rtc entry to reload")
            return
        self._last_go2rtc_reload = now
        for entry in go2rtc_entries:
            _LOGGER.warning(
                "webrtc-watchdog: WebRTC capability missing for %s while stream is active — "
                "reloading bundled go2rtc entry %s to refresh stale _supported_schemes "
                "(HA Core bug; reload runs WebRTCProvider.initialize() again)",
                cam_id[:8],
                entry.entry_id,
            )
            try:
                await self.hass.config_entries.async_reload(entry.entry_id)
            except Exception as err:
                _LOGGER.warning("webrtc-watchdog: go2rtc reload failed: %s", err)
        # After reload, the cam entity's `_webrtc_provider` is still None — HA
        # only auto-refreshes on `supported_features & STREAM` flips, but our
        # stream is already up. Push the refresh manually so the next
        # `camera/capabilities` query returns the fresh `[web_rtc, hls]`.
        # Filter on `live_connections` for the same reason as the
        # schemes-fresh loop below: refreshing providers on an idle cam
        # triggers `stream_source()` → `try_live_connection()` and opens
        # an unwanted LOCAL session.
        for cam_id_x, cam_ent in list(self.camera_entities.items()):
            if cam_id_x not in self.live_connections:
                continue
            try:
                if CameraEntityFeature.STREAM in cam_ent.supported_features:
                    await cam_ent.async_refresh_providers()
            except Exception as err:
                _LOGGER.debug(
                    "webrtc-watchdog: async_refresh_providers failed for %s: %s",
                    getattr(cam_ent, "entity_id", "?"),
                    err,
                )

    async def _ensure_go2rtc_schemes_fresh(self) -> None:
        """Pre-emptive: re-fetch `_supported_schemes` directly on the existing
        WebRTCProvider instance(s) so the very first stream activation finds
        the right scheme set.

        Thin dispatch to `go2rtc_client.ensure_go2rtc_schemes_fresh` — kept
        as a bound method because it is patched directly in tests via `AsyncMock()` /
        `BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)`
        unbound-style calls. See `go2rtc_client.py` for the full docstring
        (private-API hack rationale, watchdog scoping) — unchanged by this
        move.
        """
        await ensure_go2rtc_schemes_fresh(self)

    async def unregister_go2rtc_stream(self, cam_id: str) -> None:
        """Remove the camera stream from go2rtc when the live session ends.

        Thin dispatch to `go2rtc_client.unregister_go2rtc_stream` — kept as
        a bound method because it is called from `stream_lifecycle.py` as
        `coordinator.unregister_go2rtc_stream(cam_id)` and patched
        directly in tests via `AsyncMock()` /
        `BoschCameraCoordinator.unregister_go2rtc_stream(coord, ...)`
        unbound-style calls. See `go2rtc_client.py` for the full docstring
        (endpoint retry order) — unchanged by this move.
        """
        await unregister_go2rtc_stream(self, cam_id)

    async def start_tls_proxy(
        self, cam_id: str, cam_host: str, cam_port: int, is_renewal: bool = False
    ) -> int:
        """Start a local TCP→TLS proxy for a LOCAL RTSPS stream.

        Thin dispatch to `tls_proxy_wiring.start_tls_proxy_wiring` — kept
        as a bound method because it is called from other coordinator-facing modules
        (live_connection.py) as `coordinator.start_tls_proxy(...)` and
        patched directly in tests via `AsyncMock()` /
        `BoschCameraCoordinator.start_tls_proxy(coord, ...)` unbound-style
        calls. See `tls_proxy_wiring.py` for the full docstring (died-callback
        thread→event-loop hop, lazy SSL context init) — unchanged by this
        move.
        """
        return await start_tls_proxy_wiring(
            self, cam_id, cam_host, cam_port, is_renewal=is_renewal
        )

    async def on_tls_proxy_died(self, cam_id: str) -> None:
        """Auto-rebuild the LOCAL session after the TLS proxy circuit breaker fires.

        Thin dispatch to `tls_proxy_wiring.on_tls_proxy_died` — kept as a
        bound method because it is scheduled as a task from within
        `start_tls_proxy`'s died-callback and patched directly in tests
        via `AsyncMock()` / `BoschCameraCoordinator.on_tls_proxy_died(
        coord, ...)` unbound-style calls. See `tls_proxy_wiring.py` for the
        full docstring (backoff mechanism, force_reset rebuild) — unchanged
        by this move.
        """
        await on_tls_proxy_died(self, cam_id)

    @staticmethod
    def create_ssl_ctx() -> ssl.SSLContext:
        """Create SSL context for TLS proxy (blocking — runs in executor).

        Thin dispatch to `tls_proxy_wiring.create_ssl_ctx` — kept as a
        staticmethod on the class because `start_tls_proxy` calls it via
        `self.create_ssl_ctx` (patchable per-instance in tests) and tests
        also call `BoschCameraCoordinator.create_ssl_ctx()` directly. See
        `tls_proxy_wiring.py` for the full docstring — unchanged by this
        move.
        """
        return create_ssl_ctx()

    async def stop_tls_proxy(self, cam_id: str) -> None:
        """Stop the TLS proxy for a camera.

        Thin dispatch to `tls_proxy_wiring.stop_tls_proxy_wiring` — kept as
        a bound method because it is called from other coordinator-facing
        modules (live_connection.py, stream_lifecycle.py, switch.py) as
        `coordinator.stop_tls_proxy(cam_id)` and patched directly in
        tests via `AsyncMock()` / `BoschCameraCoordinator.stop_tls_proxy(
        coord, ...)` unbound-style calls. See `tls_proxy_wiring.py` for the
        full docstring — unchanged by this move.
        """
        await stop_tls_proxy_wiring(self, cam_id)

    async def start_viewing_front_door(
        self, cam_id: str, *, inst: int, audio_param: str, max_session_duration: int
    ) -> str | None:
        """Start (or reuse) the credential-free front-door for the main viewing path.

        Thin dispatch to `viewing_front_door.start_viewing_front_door` — kept
        as a bound method because it is called from other coordinator-facing
        modules (live_connection.py) as
        `coordinator.start_viewing_front_door(...)` and patched directly in
        tests via `AsyncMock()` /
        `BoschCameraCoordinator.start_viewing_front_door(coord, ...)`
        unbound-style calls. See `viewing_front_door.py` for the full
        docstring — unchanged by this move.
        """
        return await start_viewing_front_door(
            self,
            cam_id,
            inst=inst,
            audio_param=audio_param,
            max_session_duration=max_session_duration,
        )

    async def stop_viewing_front_door(self, cam_id: str) -> None:
        """Stop the credential-free front-door for the main viewing path.

        Thin dispatch to `viewing_front_door.stop_viewing_front_door` — kept
        as a bound method because it is called from other coordinator-facing
        modules (stream_lifecycle.py) as
        `coordinator.stop_viewing_front_door(cam_id)` and patched directly
        in tests via `AsyncMock()` /
        `BoschCameraCoordinator.stop_viewing_front_door(coord, ...)`
        unbound-style calls. See `viewing_front_door.py` for the full
        docstring — unchanged by this move.
        """
        await stop_viewing_front_door(self, cam_id)

    async def start_remote_viewing_front_door(
        self, cam_id: str, *, inst: int, audio_param: str, max_session_duration: int
    ) -> str | None:
        """Start (or reuse) the stable-URL front-door for the REMOTE viewing path.

        Thin dispatch to
        `remote_viewing_front_door.start_remote_viewing_front_door` — kept as
        a bound method for the same call-site/test-patching reasons as
        `start_viewing_front_door` above. See `remote_viewing_front_door.py`
        for the full docstring.
        """
        return await start_remote_viewing_front_door(
            self,
            cam_id,
            inst=inst,
            audio_param=audio_param,
            max_session_duration=max_session_duration,
        )

    async def stop_remote_viewing_front_door(self, cam_id: str) -> None:
        """Stop the stable-URL front-door for the REMOTE viewing path.

        Thin dispatch to
        `remote_viewing_front_door.stop_remote_viewing_front_door` — kept as
        a bound method for the same call-site/test-patching reasons as
        `stop_viewing_front_door` above.
        """
        await stop_remote_viewing_front_door(self, cam_id)

    async def auto_renew_local_session(self, cam_id: str, generation: int) -> None:
        """Keep LOCAL RTSP session alive via heartbeats + periodic full renewal.

        Thin dispatch to `session_renewal.auto_renew_local_session` — kept
        as a bound method because this is called from `live_connection.py` as
        `coordinator.auto_renew_local_session(cam_id, gen)` (wrapped in
        `replace_renewal_task`) and patched directly in tests via
        `AsyncMock()` / `BoschCameraCoordinator.auto_renew_local_session(
        coord, ...)` unbound-style calls. See `session_renewal.py` for the
        full docstring (heartbeat vs. full-renewal cadence, model-specific
        intervals) — unchanged by this move.
        """
        await auto_renew_local_session(self, cam_id, generation)

    async def promote_to_local(self, cam_id: str) -> None:
        """Lift an active REMOTE-fallback stream onto LOCAL via a renewal.

        Thin dispatch to `session_renewal.promote_to_local` — kept as a
        bound method because this is called from `camera_status.py` as
        `coordinator.promote_to_local(cam_id)` and patched directly in
        tests via `AsyncMock()` / `BoschCameraCoordinator.promote_to_local(
        coord, ...)` unbound-style calls. See `session_renewal.py` for the
        full docstring (LAN-recovery trigger, REMOTE→LOCAL migration) —
        unchanged by this move.
        """
        await promote_to_local(self, cam_id)

    async def remote_session_terminator(self, cam_id: str, generation: int) -> None:
        """Schedule a clean teardown of a REMOTE live session before the
        relay-side lifetime cap.

        Thin dispatch to `session_renewal.remote_session_terminator` — kept
        as a bound method because this is called from `live_connection.py`
        as `coordinator.remote_session_terminator(cam_id, gen)` (wrapped
        in `replace_renewal_task`) and patched directly in tests via
        `AsyncMock()` / `BoschCameraCoordinator.remote_session_terminator(
        coord, ...)` unbound-style calls. See `session_renewal.py` for the
        full docstring (relay lifetime cap, generation tracking, self-
        cancel avoidance) — unchanged by this move.
        """
        await remote_session_terminator(self, cam_id, generation)

    # ── RCP protocol (Bosch Remote Configuration Protocol via cloud proxy) ──────
    def _invalidate_rcp_session(self, proxy_hash: str) -> None:
        """Drop a cached RCP session so the next call reopens the handshake.

        Call this when a downstream RCP read returns HTTP 401 (auth dropped),
        HTTP 403 (session expired), or RCP error 0x0c0d (session closed).
        Without invalidation the cache would keep serving the dead ID for
        its full 5-min TTL — readers would see None until the entry expired.
        """
        if self.rcp_session_cache.pop(proxy_hash, None) is not None:
            _LOGGER.debug("RCP session cache invalidated for %s", proxy_hash[:8])

    async def get_cached_rcp_session(
        self, proxy_host: str, proxy_hash: str
    ) -> str | None:
        """Return a cached RCP session ID, opening a new one if missing or expired.

        Caches valid session IDs for 5 minutes (TTL 300 s) to avoid the 2-step
        RCP handshake (0xff0c + 0xff0d) on every thumbnail or data fetch.

        Serialized per proxy_hash via `_get_rcp_session_lock` — Bosch's proxy
        only tolerates one live session per proxy_hash, so two callers racing
        an empty/expired cache would otherwise each open their own session and
        one gets rejected (sessionid 0x00000000).
        """
        async with self._get_rcp_session_lock(proxy_hash):
            now = time.monotonic()
            cached = self.rcp_session_cache.get(proxy_hash)
            if cached:
                session_id, expires_at = cached
                if now < expires_at:
                    return session_id
                del self.rcp_session_cache[proxy_hash]

            new_session_id: str | None = await self._rcp_session(proxy_host, proxy_hash)
            if new_session_id:
                self.rcp_session_cache[proxy_hash] = (
                    new_session_id,
                    now + 300.0,
                )  # 5-min TTL
            return new_session_id

    async def _rcp_session(self, proxy_host: str, proxy_hash: str) -> str | None:
        """Open an RCP session via the cloud proxy and return the sessionid, or None on failure.

        The RCP handshake consists of two steps:
          1. WRITE command 0xff0c with a fixed payload → extract <sessionid> from XML response
          2. WRITE command 0xff0d with the sessionid → ACK (confirms the session)

        Auth=3 (anonymous via URL hash) provides read-only access.
        The proxy_host should be in the form "proxy-NN.live.cbs.boschsecurity.com:42090".

        Uses the shared, HA-lifecycle-managed Bosch-cloud session
        (`async_bosch_cloud_session_cm` / `cloud_ssl.py`) instead of opening a
        fresh ``aiohttp.ClientSession`` + ``TCPConnector`` per call — this
        proxy host is the exact same Bosch-pinned-CA TLS trust domain
        (`async_get_bosch_cloud_ssl_context`) the shared session already
        uses, so there is no separate trust reason to keep a private
        connector here.
        """
        base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"
        init_payload = (
            "0x0102004000000000040000000000000000010000000000000001000000000000"
        )

        async with async_bosch_cloud_session_cm(self.hass) as session:
            # Step 1: open session
            params1 = {
                "command": "0xff0c",
                "direction": "WRITE",
                "type": "P_OCTET",
                "payload": init_payload,
            }
            try:
                async with asyncio.timeout(8):
                    async with session.get(base, params=params1) as resp:
                        if resp.status != 200:
                            _LOGGER.debug(
                                "_rcp_session: step1 HTTP %d for %s",
                                resp.status,
                                proxy_host,
                            )
                            return None
                        text = await resp.text()
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug("_rcp_session: step1 error for %s: %s", proxy_host, err)
                return None

            # Parse <sessionid> from XML response
            import re as _re

            m = _re.search(r"<sessionid>(\S+)</sessionid>", text, _re.IGNORECASE)
            if not m:
                _LOGGER.debug(
                    "_rcp_session: no <sessionid> in response for %s: %s",
                    proxy_host,
                    text[:200],
                )
                return None
            session_id = m.group(1)

            # Step 2: ACK the session
            params2 = {
                "command": "0xff0d",
                "direction": "WRITE",
                "type": "P_OCTET",
                "sessionid": session_id,
            }
            try:
                async with asyncio.timeout(8):
                    async with session.get(base, params=params2) as resp2:
                        _LOGGER.debug(
                            "_rcp_session: ACK HTTP %d for %s (sessionid=%s)",
                            resp2.status,
                            proxy_host,
                            session_id,
                        )
            except (TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug("_rcp_session: step2 error for %s: %s", proxy_host, err)
                # Session may still be valid — return it anyway

            return session_id

    @staticmethod
    def _proxy_hash_from_rcp_base(rcp_base: str) -> str | None:
        """Extract proxy_hash from `https://host:port/{hash}/rcp.xml`."""
        parts = rcp_base.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-1] == "rcp.xml":
            return parts[-2]
        return None

    async def rcp_read(
        self,
        rcp_base: str,
        command: str,
        sessionid: str,
        type_: str = "P_OCTET",
        num: int = 0,
    ) -> bytes | None:
        """READ an RCP command and return the raw payload bytes, or None on failure.

        Uses the HA shared session to avoid creating a new
        connector+session per RCP command (prevents socket exhaustion).
        Invalidates the session cache on HTTP 401/403 or RCP <err>0x0c0d</err>
        (session closed) — the dead ID would otherwise block reads until TTL.
        """
        # Local import (not top-level): keeps unittest.mock.patch(
        # "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
        # ...) working the same way it did before BoschCameraCoordinator
        # moved out of __init__.py — those patches target the package's own
        # namespace, matching the pattern already used in live_connection.py.
        from . import (
            async_get_bosch_cloud_session as async_get_bosch_cloud_session,
        )

        params: dict[str, str] = {
            "command": command,
            "direction": "READ",
            "type": type_,
            "sessionid": sessionid,
        }
        if num:
            params["num"] = str(num)

        session = await async_get_bosch_cloud_session(self.hass)
        try:
            async with asyncio.timeout(8):
                async with session.get(rcp_base, params=params) as resp:
                    if resp.status != 200:
                        _LOGGER.debug(
                            "rcp_read: command=%s HTTP %d", command, resp.status
                        )
                        if resp.status in (401, 403):
                            proxy_hash = self._proxy_hash_from_rcp_base(rcp_base)
                            if proxy_hash:
                                self._invalidate_rcp_session(proxy_hash)
                        return None
                    raw = await resp.read()
                    # RCP session-closed response: <err>0x0c0d</err>. Drop the
                    # cached session so the next read reopens the handshake.
                    if b"0x0c0d" in raw and b"<err>" in raw:
                        proxy_hash = self._proxy_hash_from_rcp_base(rcp_base)
                        if proxy_hash:
                            self._invalidate_rcp_session(proxy_hash)
                        return None
                    return bytes(raw)
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("rcp_read: command=%s error: %s", command, err)
            return None

    async def _async_update_rcp_data(
        self, cam_id: str, proxy_host: str, proxy_hash: str
    ) -> None:
        """Fetch all RCP data for a camera via cloud proxy.

        Delegates to rcp.py's async_update_rcp_data() which reads:
          Phase 1: LED dimmer, privacy mask, clock, LAN IP, product name, bitrate
          Phase 2: alarm catalog, motion zones/coords, TLS cert, network services, IVA catalog
        """
        await async_update_rcp_data(self, cam_id, proxy_host, proxy_hash)

    async def _fetch_rcp_lan(
        self,
        cam_id: str,
        opcode_hex: str,
    ) -> bytes | None:
        """Read an RCP value directly from the camera's LAN HTTPS endpoint (cbs Digest auth).

        Uses the cached LOCAL session credentials (``local_creds_cache``) which
        are populated on every successful PUT /connection LOCAL. The camera's
        ``rcp.xml`` endpoint on port 443 requires HTTP Digest auth with the
        rotating cbs-XXXXXXXX user/password pair.

        Returns the decoded payload bytes on success, None on any error
        (no LAN IP, no creds, network error, auth failure, RCP error).

        IMPORTANT: Do NOT call this from the event loop for opcodes that would
        rotate cbs creds (i.e. never issue PUT /connection LOCAL here — use
        the existing slow-tier RCP proxy path for writes). This helper is
        READ-ONLY and purely supplementary to the cloud-proxy path.
        """
        # Local import (not top-level): keeps unittest.mock.patch(
        # "custom_components.bosch_shc_camera.async_get_clientsession", ...)
        # working the same way it did before BoschCameraCoordinator moved
        # out of __init__.py — matches the live_connection.py pattern.
        from . import (
            async_digest_request as async_digest_request,
        )
        from . import (
            async_get_clientsession as async_get_clientsession,
        )

        if self._is_rcp_lan_denied(cam_id, opcode_hex):
            return None
        ip = self.get_cam_lan_ip(cam_id)
        if not ip:
            return None
        creds = self.local_creds_cache.get(cam_id)
        if not creds:
            return None
        user: str = creds.get("user", "")
        password: str = creds.get("password", "")
        if not (user and password):
            return None
        port: int = creds.get("port", 443)
        base = f"https://{ip}:{port}/rcp.xml"
        params: dict[str, str] = {
            "command": opcode_hex,
            "direction": "READ",
            "type": "P_OCTET",
            "num": "1",
        }
        from urllib.parse import urlencode

        url = f"{base}?{urlencode(params)}"
        try:
            import re as _re_lan

            async with await async_digest_request(
                async_get_clientsession(self.hass, verify_ssl=False),
                "GET",
                url,
                user,
                password,
                timeout=8.0,
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "_fetch_rcp_lan: %s@%s HTTP %d", opcode_hex, ip, resp.status
                    )
                    if resp.status == 401:
                        # CBS user lacks permission for this opcode — stop hammering
                        # the camera every 5 min. Retry once the TTL expires.
                        self._mark_rcp_lan_denied(cam_id, opcode_hex)
                    return None
                self._clear_rcp_lan_denied(cam_id, opcode_hex)
                raw = await resp.read()
                # Check for RCP-level error
                if b"<err>" in raw.lower():
                    _LOGGER.debug(
                        "_fetch_rcp_lan: %s@%s RCP error: %s", opcode_hex, ip, raw[:120]
                    )
                    return None
                # Extract payload from <str>HEXDATA</str>
                m = _re_lan.search(
                    rb"<str>([0-9a-fA-F]+)</str>", raw, _re_lan.IGNORECASE
                )
                if m:
                    return bytes.fromhex(m.group(1).decode("ascii"))
                # Fallback: raw bytes if not XML envelope
                if raw and not raw.lstrip(b"\n\r\t ").startswith(b"<"):
                    return bytes(raw)
                return None
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("_fetch_rcp_lan: %s@%s %s", opcode_hex, ip, err)
            return None
        except Exception as err:  # pragma: no cover
            _LOGGER.debug("_fetch_rcp_lan: %s@%s unexpected: %s", opcode_hex, ip, err)
            return None

    async def _async_update_lan_diagnostic_sensors(self, cam_id: str) -> None:
        """Fetch F4 (ONVIF scopes) and F6 (RCP version) for a single camera via LAN.

        Called on slow-tier when the camera is ONLINE, LAN IP is known, and
        cbs creds are cached. Failures are non-fatal: caches keep their last
        known value or remain absent (sensor shows unavailable).
        """
        # F4: ONVIF scopes via RCP 0x0a98 — ~720 B ASCII TLV
        try:
            raw_onvif = await self._fetch_rcp_lan(cam_id, "0x0a98")
            if raw_onvif:
                scopes_dict = _parse_onvif_scopes(raw_onvif)
                self.rcp_onvif_scopes_cache[cam_id] = scopes_dict
                _LOGGER.debug("ONVIF scopes for %s: %s", cam_id[:8], scopes_dict)
        except Exception as err:
            _LOGGER.debug(
                "ONVIF scopes fetch error for %s: %s",
                cam_id[:8],
                BoschCameraCoordinator.err_str(err),
            )

        # F6: RCP protocol versions via 0xff00 (primary) + 0xff04 (secondary)
        try:
            raw_ver = await self._fetch_rcp_lan(cam_id, "0xff00")
            if raw_ver and len(raw_ver) >= 4:
                version_str = f"{raw_ver[0]}.{raw_ver[1]}.{raw_ver[2]}.{raw_ver[3]}"
                self.rcp_version_cache[cam_id] = version_str
                _LOGGER.debug("RCP version for %s: %s", cam_id[:8], version_str)
        except Exception as err:
            _LOGGER.debug(
                "RCP version fetch error for %s: %s",
                cam_id[:8],
                BoschCameraCoordinator.err_str(err),
            )

    def clock_offset(self, cam_id: str) -> float | None:
        """Return clock offset in seconds (camera time − server time), or None."""
        return self.rcp_clock_offset_cache.get(cam_id)

    def rcp_lan_ip(self, cam_id: str) -> str | None:
        """Return camera LAN IP from RCP 0x0a36, or None."""
        return self.rcp_lan_ip_cache.get(cam_id)

    def rcp_product_name(self, cam_id: str) -> str | None:
        """Return camera product name from RCP 0x0aea, or None."""
        return self.rcp_product_name_cache.get(cam_id)

    def rcp_bitrate_ladder(self, cam_id: str) -> list[int]:
        """Return bitrate ladder (kbps) from RCP 0x0c81, or empty list."""
        return self.rcp_bitrate_cache.get(cam_id, [])

    def get_quality(self, cam_id: str) -> str:
        """Return current quality preference: 'auto', 'high', or 'low'.

        Thin delegator — logic lives in quality_prefs.py (style audit,
        same pattern as device_actions.py's async_install_firmware).
        """
        return quality_prefs.get_quality(self, cam_id)

    def set_quality(self, cam_id: str, quality: str) -> None:
        """Set quality preference. quality must be 'auto', 'high', or 'low'.

        Thin delegator — see get_quality's docstring.
        """
        quality_prefs.set_quality(self, cam_id, quality)

    def get_quality_params(self, cam_id: str) -> tuple[bool, int]:
        """Return (highQualityVideo: bool, inst: int) for current quality preference.

        Thin delegator — see get_quality's docstring.
        """
        return quality_prefs.get_quality_params(self, cam_id)

    def get_quality_remote_fallback_active(self, cam_id: str) -> bool:
        """True if the "low" preference is set but the last successful
        connection actually used inst=2 instead of inst=4 (REMOTE clamp).

        Thin delegator — see get_quality's docstring.
        """
        return quality_prefs.get_quality_remote_fallback_active(self, cam_id)

    def get_nvr_mode(self, cam_id: str) -> str:
        """Return effective Mini-NVR mode for this camera.

        Thin delegator — see get_quality's docstring.
        """
        return quality_prefs.get_nvr_mode(self, cam_id)

    def set_nvr_mode(self, cam_id: str, mode: str) -> None:
        """Set the per-camera Mini-NVR mode override.

        Thin delegator — see get_quality's docstring.
        """
        quality_prefs.set_nvr_mode(self, cam_id, mode)

    def get_nvr_event_clip_enabled(self, cam_id: str) -> bool:
        """Return whether native FCM-triggered event→clip assembly is on for this camera.

        Thin delegator — see get_quality's docstring.
        """
        return quality_prefs.get_nvr_event_clip_enabled(self, cam_id)

    def set_nvr_event_clip_enabled(self, cam_id: str, enabled: bool) -> None:
        """Set whether native FCM-triggered event→clip assembly runs for this camera.

        Thin delegator — see get_quality's docstring.
        """
        quality_prefs.set_nvr_event_clip_enabled(self, cam_id, enabled)

    def motion_settings(self, cam_id: str) -> dict[str, Any]:
        """Return motion detection settings dict, or empty dict.

        Thin delegator — see get_quality's docstring.
        """
        return quality_prefs.motion_settings(self, cam_id)

    def recording_options(self, cam_id: str) -> dict[str, Any]:
        """Return recording options dict, or empty dict.

        Thin delegator — see get_quality's docstring.
        """
        return quality_prefs.recording_options(self, cam_id)

    async def async_put_camera(
        self, cam_id: str, endpoint: str, payload: dict[str, Any] | None
    ) -> bool:
        """PUT to /v11/video_inputs/{cam_id}/{endpoint} with payload. Returns True on success.

        payload=None sends a truly empty body (no bytes, not even "{}") —
        required for soft_reset/hard_reset. Verified from the decompiled
        Bosch app (research/apk_2.12.0): UpdateSoftReset/UpdateHardReset
        call the 2-arg PutStringAsync(url, accessToken) overload, whose
        argsAsJson parameter defaults to "" — StringContent("", ...,
        "application/json") is Content-Length: 0, not the 2-byte "{}"
        aiohttp's `json={}` would send. Every other endpoint this method
        is used for sends a real payload dict, so this only changes
        behavior for the two reset endpoints.
        """
        # Local import (not top-level): keeps unittest.mock.patch(
        # "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
        # ...) working the same way it did before BoschCameraCoordinator
        # moved out of __init__.py — those patches target the package's own
        # namespace, matching the pattern already used in live_connection.py.
        from . import (
            async_get_bosch_cloud_session as async_get_bosch_cloud_session,
        )

        token = self.token
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        put_kwargs: dict[str, Any] = (
            {"data": ""} if payload is None else {"json": payload}
        )
        session = await async_get_bosch_cloud_session(self.hass)
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/{endpoint}"
        try:
            async with asyncio.timeout(10):
                async with session.put(url, headers=headers, **put_kwargs) as resp:
                    if resp.status == 401:
                        # Token expired — refresh and retry once
                        _LOGGER.info(
                            "async_put_camera %s/%s: 401 — refreshing token",
                            cam_id,
                            endpoint,
                        )
                        try:
                            token = await self.ensure_valid_token(token)
                            headers["Authorization"] = f"Bearer {token}"
                        except asyncio.CancelledError:
                            raise
                        except Exception as err:
                            _LOGGER.debug(
                                "async_put_camera token refresh failed: %s", err
                            )
                            return False
                        async with asyncio.timeout(10):
                            async with session.put(
                                url, headers=headers, **put_kwargs
                            ) as resp2:
                                # Must accept the same status set as the
                                # initial attempt below (200/201/204) — this
                                # is the identical write, retried after a
                                # token refresh, not a different semantic
                                # operation.
                                ok2 = resp2.status in (200, 201, 204)
                                if not ok2:
                                    body2 = await resp2.text()
                                    _LOGGER.debug(
                                        "async_put_camera %s/%s: retry HTTP %d — %s",
                                        cam_id,
                                        endpoint,
                                        resp2.status,
                                        body2[:200],
                                    )
                                return ok2
                    ok = resp.status in (200, 201, 204)
                    if not ok:
                        body = await resp.text()
                        _LOGGER.debug(
                            "async_put_camera %s/%s: HTTP %d — %s",
                            cam_id,
                            endpoint,
                            resp.status,
                            body[:200],
                        )
                    return ok
        except Exception as err:
            _LOGGER.warning("async_put_camera %s/%s error: %s", cam_id, endpoint, err)
            return False

    # SMB/NAS upload, download, cleanup, and disk-check functions are in smb.py
