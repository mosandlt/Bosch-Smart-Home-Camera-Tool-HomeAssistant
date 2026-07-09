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
import re as _re_mod
import socket
import ssl
import threading
import time
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .maintenance import MaintenanceWindow
from urllib.parse import urlparse

import aiohttp

# ── URL allowlist for image/video downloads (SSRF prevention) ────────────────
_SAFE_DOMAINS = frozenset({".boschsecurity.com", ".bosch.com"})


def _is_safe_bosch_url(url: str) -> bool:
    """Validate that a URL points to a known Bosch domain (HTTPS only)."""
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and any(parsed.hostname.endswith(d) for d in _SAFE_DOMAINS)
    )


from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import recorder as nvr_recorder
from . import shc as shc_mod
from .auth_utils import async_digest_request
from .cloud_ssl import (
    async_bosch_cloud_session_cm,
    async_get_bosch_cloud_session,
    async_get_bosch_cloud_ssl_context,
)
from .fcm import (
    _write_file as _fcm_write_file,
)
from .fcm import (
    async_ensure_fcm_supervisor as _fcm_async_ensure_supervisor,
)
from .fcm import (
    async_handle_fcm_push as _fcm_async_handle_fcm_push,
)
from .fcm import (
    async_mark_events_read as _fcm_async_mark_events_read,
)
from .fcm import (
    async_send_alert as _fcm_async_send_alert,
)
from .fcm import (
    async_stop_fcm_supervisor as _fcm_async_stop_supervisor,
)
from .fcm import (
    build_notify_data as _fcm_build_notify_data,
)
from .fcm import (
    fetch_firebase_config as _fcm_fetch_firebase_config,
)
from .fcm import (
    get_alert_services as _fcm_get_alert_services,
)
from .fcm import (
    register_fcm_with_bosch as _fcm_register_fcm_with_bosch,
)
from .frigate_endpoint import (
    QUALITY_HIGH,
    FrontDoorConfig,
    FrontDoorRunner,
    InnerTarget,
    build_public_url,
)
from .rcp import async_update_rcp_data
from .rcp import (
    get_cached_rcp_session as get_cached_rcp_session,  # re-export: mypy --no-implicit-reexport
)
from .smb import (
    sync_smb_cleanup,
)
from .smb import (
    sync_smb_upload as sync_smb_upload,  # re-export: mypy --no-implicit-reexport
)
from .tls_proxy import pre_warm_rtsp, start_tls_proxy, stop_all_proxies, stop_tls_proxy

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
# moment it lands, so the binary sensor can never turn ON (issue #36). When
# push is not delivering we therefore poll at this fast cadence instead — bounded
# below the smallest motion window (MOTION_ACTIVE_WINDOW_MIN/DEFAULT) so a
# polled event is always seen while still "fresh". A user who explicitly set a
# lower `interval_events` keeps it (min() below).
FCM_DOWN_EVENT_POLL_SEC = 60.0

# Delivery-death detection (issue #36). When the periodic /v11/events poll finds
# a genuinely NEW event while FCM is enabled+running+"healthy" yet no real push
# has arrived in this window, push delivery is dead at the cloud/Google layer
# even though the socket reports is_started()=True (the exact silent-death case
# the fcm.py module docstring describes). The poll is ground truth that push
# missed a real event, so we flip _fcm_healthy=False and force a HARD heal
# (purge + fresh registration) — which also re-POSTs to Bosch /v11/devices,
# healing a server-side-dropped device registration. 10 min is wide enough that
# a push arriving just before the poll (race) keeps _fcm_last_push recent and
# suppresses a false positive.
FCM_DELIVERY_DEAD_AFTER_SEC = 600.0

# Grace before a camera's online→offline transition is ANNOUNCED (push/notify).
# Cameras on a Wi-Fi repeater/mesh briefly drop during a repeater restart or a
# DFS channel change and recover within a minute or two; firing an "offline /
# live + snapshots unavailable" notification on the first failed status check is
# noise. Only announce offline once the camera has stayed offline continuously
# for this long. A recovery within the window produces no notification at all.
# The camera ENTITY availability still flips immediately — only the notification
# is debounced.
CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC = 300.0  # 5 min

# Bounded slow-tier defer (stream-contention gate, see the slow-tier block in
# _async_update_data): while a live stream is active the slow-tier diagnostic
# read is deferred to avoid TLS-channel contention.  On a *continuously* active
# stream (e.g. a dashboard left on live view) that deferral would otherwise
# never end, freezing the diagnostic sensors indefinitely.  After this many
# seconds of unbroken deferral we force one read even while streaming —
# accepting a rare brief contention over permanently stale diagnostics — then
# the defer cycle restarts.  Bounds worst-case staleness to ~this + one slow
# interval.
SLOW_TIER_MAX_DEFER_SEC = 1800.0

# Module-level debounce dict for auto-describe-on-motion — keyed by cam_id.
# Must live at module level (not inside async_setup_entry) so it survives
# integration reloads: re-entering async_setup_entry would otherwise reset
# the dict and allow a burst of back-to-back AI calls across the reload gap.
_AI_MOTION_DEBOUNCE: dict[str, float] = {}
_AI_MOTION_DEBOUNCE_SEC = 30.0

# Read integration version once at import time (sync I/O at module level is fine — import
# happens in the executor during HA startup, not inside the event loop).
try:
    import json as _json
    import pathlib as _pathlib

    _INTEGRATION_VERSION: str = _json.loads(
        (_pathlib.Path(__file__).parent / "manifest.json").read_text()
    )["version"]
except Exception:  # pragma: no cover — manifest.json ships with the package; only fires on a corrupted install
    _INTEGRATION_VERSION = "unknown"


class _StreamSupportNoiseFilter(logging.Filter):
    """Rate-limit HA camera-component log spam during stream pre-warm.

    Handles two recurring burst patterns from HA's camera component:

    1. "does not support play stream service" — fired when stream_source()
       returns None during LOCAL pre-warm (~25 s window). Multiple tabs /
       Companion app / card HLS fallback can produce 9 of these in 15 s.
       Rate-limited to 1 per 30 s *per entity_id* (bosch_* only).

    2. "Camera not found" — fired when the browser requests WebRTC for a
       camera not yet registered in go2rtc (startup race: browser reconnects
       and sees cached "streaming" state before the coordinator has finished
       re-registering the stream). Rate-limited to 1 per 60 s globally
       (message carries no entity_id so per-entity tracking isn't possible).

    A real "stream truly broken" issue still surfaces because one ERROR per
    window is always passed through. Other camera integrations are unaffected.
    """

    _MAX_TRACKED = 32  # max entity IDs to track — prevents unbounded growth
    _NOT_FOUND_KEY = "__camera_not_found__"
    _NOT_FOUND_WINDOW = 60.0

    def __init__(self) -> None:
        super().__init__()
        self._last_passed: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)

        # ── "Camera not found" burst (startup race, no entity_id in message) ──
        if "Camera not found" in msg and "Error requesting stream" in msg:
            import time as _t

            now = _t.monotonic()
            last = self._last_passed.get(self._NOT_FOUND_KEY, float("-inf"))
            if (now - last) < self._NOT_FOUND_WINDOW:
                return False
            self._last_passed[self._NOT_FOUND_KEY] = now
            return True

        # ── "does not support play stream service" burst (pre-warm window) ──
        if "does not support play stream service" not in msg:
            return True
        # Extract entity_id from "Error requesting stream: camera.<id> ..."
        ent = ""
        prefix = "camera."
        idx = msg.find(prefix)
        if idx != -1:
            tail = msg[idx + len(prefix) :]
            ent = tail.split(" ", 1)[0]
        if not ent.startswith("bosch_"):
            return True  # not us, leave alone
        import time as _t

        now = _t.monotonic()
        last = self._last_passed.get(ent, float("-inf"))
        if (now - last) < 30.0:
            return False
        # Prune oldest entry when dict grows too large
        if len(self._last_passed) >= self._MAX_TRACKED:
            oldest = min(self._last_passed, key=self._last_passed.__getitem__)
            del self._last_passed[oldest]
        self._last_passed[ent] = now
        return True


def _install_stream_support_noise_filter() -> None:
    """Install the Bosch-side filter on HA's camera component logger once."""
    cam_logger = logging.getLogger("homeassistant.components.camera")
    for f in cam_logger.filters:
        if isinstance(f, _StreamSupportNoiseFilter):
            return
    cam_logger.addFilter(_StreamSupportNoiseFilter())


class _StreamWorkerErrorListener(logging.Handler):
    """Intercept `Error from stream worker` log records from HA's stream
    component and route each one to the coordinator's stream-error handler.

    HA's stream component runs an auto-restart loop on worker crashes
    (`stream.__init__.Stream._run_worker`): worker fails → `_set_state(False)`
    (yellow in the card) → backoff wait → `_set_state(True)` (briefly blue) →
    retry. This produces a continuous yellow→blue→yellow cycle that our own
    polling watchdog misses when its 60 s tick happens to land during a brief
    "available" window. Instead of polling, we listen to HA's own error log:
    every "Error from stream worker" on a logger named
    `homeassistant.components.stream.stream.camera.<entity_id>` increments the
    coordinator's per-camera counter, and once the threshold is reached the
    coordinator forces REMOTE on the next `try_live_connection` — escaping
    the cycle deterministically on N consecutive stream-worker errors rather
    than hoping the 60 s tick catches a failing state.
    """

    def __init__(self, coordinator: "BoschCameraCoordinator") -> None:
        super().__init__(logging.ERROR)
        self._coordinator: BoschCameraCoordinator | None = coordinator

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._coordinator is None:
                return
            if record.levelno < logging.ERROR:
                return
            # Only interested in HA's stream worker errors. Other errors on
            # the same parent logger (e.g. RecorderBuildError, HLS output
            # failures) aren't our concern.
            msg = record.getMessage()
            if "Error from stream worker" not in msg:
                return
            # Logger name shape:
            # homeassistant.components.stream.stream.camera.bosch_<slug>
            name = record.name
            marker = ".stream.camera."
            if marker not in name:
                return
            entity_id = "camera." + name.rsplit(marker, 1)[1]
            # Resolve cam_id from entity_id via the coordinator's entity map.
            # `emit` runs in the logging thread — defer the async work.
            cam_id = None
            for cid, entity in self._coordinator._camera_entities.items():
                if getattr(entity, "entity_id", None) == entity_id:
                    cam_id = cid
                    break
            if not cam_id:
                return
            loop = self._coordinator.hass.loop
            loop.call_soon_threadsafe(
                self._coordinator._schedule_stream_worker_error, cam_id, msg
            )
        except Exception:  # noqa: S110 # logging.emit handler must never raise; exception would recurse into logging itself
            # Never let the log handler crash the event loop or the logger.
            # Intentionally broad: this runs inside logging.emit and any
            # exception here would be routed back to logging's own error path.
            pass


def _looks_like_uuid_name(n: str) -> bool:
    """True if `n` looks like a `Bosch <UUID>` placeholder name.

    Detects names a previous cloud-degraded startup leaked into the device
    registry when `coordinator.data[cam_id].info.title` was empty and the
    code fell back to using the cam_id (UUID-style) as the title.
    """
    return len(n) >= 36 and n.upper().count("-") >= 4


def _rehydrate_cams_from_registry(
    hass: HomeAssistant,
    entry_id: str,
) -> tuple[set[str], dict[str, str]]:
    """Discover known cam_ids + human-readable titles from the HA registries.

    Used by `async_setup_entry` when the first cloud refresh raises
    `ConfigEntryNotReady` — without this rehydration, no entities would
    materialise on a cold start during a cloud outage, even though privacy
    / light / LAN-ping all work without the cloud.

    Returns `(cam_ids, cam_titles)`. `cam_titles` is keyed by cam_id.
    Title-resolution order:
      1. `device.name_by_user` — manual rename always wins.
      2. `device.name` if it is NOT a `Bosch <UUID>` placeholder (which we
         repair on the way out).
      3. derived from the camera entity_id slug (`camera.bosch_terrasse` →
         `Terrasse`).
      4. fall back to the cam_id itself.

    If a stale `Bosch <UUID>` placeholder is detected in the device
    registry, the device name is repaired in place so newly-registered
    entities pick up the correct slug.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    ereg = er.async_get(hass)
    dreg = dr.async_get(hass)
    cam_ids: set[str] = set()
    for ent in er.async_entries_for_config_entry(ereg, entry_id):
        # Unique IDs in this integration consistently embed the UUID-style
        # cam_id; the first match yields the canonical set.
        for part in ent.unique_id.split("_"):
            if len(part) == 36 and part.count("-") == 4:
                cam_ids.add(part.upper())
                break
    cam_titles: dict[str, str] = {}
    for cid in cam_ids:
        device = dreg.async_get_device(identifiers={(DOMAIN, cid)})
        title: str | None = None
        if device and device.name_by_user:
            t = device.name_by_user
            title = t[6:] if t.startswith("Bosch ") else t
        elif device and device.name and not _looks_like_uuid_name(device.name):
            t = device.name
            title = t[6:] if t.startswith("Bosch ") else t
        else:
            cam_eid = ereg.async_get_entity_id(
                "camera",
                DOMAIN,
                f"bosch_shc_cam_{cid.lower()}",
            )
            if cam_eid and cam_eid.startswith("camera.bosch_"):
                slug = cam_eid[len("camera.bosch_") :]
                title = slug.replace("_", " ").title()
        if title:
            cam_titles[cid] = title
            # Repair the device name in the registry if it was a broken
            # `Bosch <UUID>` placeholder from a prior degraded startup.
            # Sticky-name damage compounds across restarts otherwise.
            if device and device.name and _looks_like_uuid_name(device.name):
                dreg.async_update_device(device.id, name=f"Bosch {title}")
                _LOGGER.info(
                    "Repaired device name for %s: 'Bosch %s' (was a UUID placeholder)",
                    cid[:8],
                    title,
                )
    return cam_ids, cam_titles


def _redact_creds(d: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of a dict with the `password` field redacted for safe logging.

    The camera-issued Digest password is ephemeral (rotates on camera reboot)
    but still a credential — replacing it with a short prefix + length keeps
    the log line useful for diagnostics without exposing the secret.
    """
    return {
        k: (
            f"{v[:3]}***({len(v)} chars)"
            if k == "password" and isinstance(v, str)
            else v
        )
        for k, v in d.items()
    }


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


from .const import (
    ALL_PLATFORMS,
    CLOUD_API,
    DEFAULT_OPTIONS,
    DOMAIN,
    LIVE_SESSION_TTL,  # noqa: F401  # re-exported for tests
    SHC_MAX_FAILS,
    SHC_RETRY_INTERVAL,
    STREAM_HLS_FRESH_SEC,
    STREAM_IDLE_REAP_CHECK_SEC,
    STREAM_IDLE_REAP_SEC,
    STREAM_START_SKIPPED,
    TIMEOUT_PUT_CONNECTION,
    TIMEOUT_SNAP,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# hass.data key holding the per-entry options snapshot used by
# _async_options_updated to tell a real options edit apart from the frequent
# data-only writes (token refresh, FCM token/credential persistence). Kept in
# hass.data (not only on the coordinator) so the comparison survives the brief
# `entry.runtime_data is None` window during a reload — see _async_options_updated.
OPTIONS_SNAPSHOT_KEY = f"{DOMAIN}_options_snapshot"


def get_options(entry: ConfigEntry) -> dict[str, Any]:
    """Return entry options merged with defaults."""
    opts: dict[str, Any] = dict(DEFAULT_OPTIONS)
    opts.update(entry.options)
    return opts


# ─────────────────────────────────────────────────────────────────────────────
class BoschCameraCoordinator(DataUpdateCoordinator):  # type: ignore[misc]
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
    # `coordinator._SHC_MAX_FAILS` without per-instance assignment.
    _SHC_MAX_FAILS: int = SHC_MAX_FAILS
    _SHC_RETRY_INTERVAL: int = SHC_RETRY_INTERVAL

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._entry = entry
        # Advanced diagnostic escape hatch (set via the manual-login/relogin
        # "Advanced" field) — NEVER defaulted to any specific host. Only ever
        # non-empty if a user explicitly typed a Bosch-confirmed alternate
        # camera-API base URL in to test whether their account is registered
        # there instead of production (2026-07-06 SebastianHarder investigation).
        cloud_api_override = entry.data.get("cloud_api_override", "")
        self._cloud_api = cloud_api_override or CLOUD_API
        if cloud_api_override:
            _LOGGER.warning(
                "Using diagnostic camera-API override %s instead of the "
                "default — this should only be set for troubleshooting a "
                "specific account issue with Bosch support's guidance",
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
            name=DOMAIN,
            update_interval=timedelta(seconds=int(opts.get("scan_interval", 60))),
        )
        # Live-stream proxy info — keyed by cam_id, cleared after LIVE_SESSION_TTL seconds
        self._live_connections: dict[str, dict[str, Any]] = {}
        self._live_opened_at: dict[str, float] = {}  # timestamp when session was opened
        # Local-RCP+ state cache: per-cam {"privacy_mode": bool, "led_dimmer": int, "fetched_at": float, "source": "local"|"remote"}
        # Refreshed opportunistically after each successful PUT /connection.
        # Used as a refinement source for SHC-cache values when SHC is offline /
        # not configured. Persists past session-end (last-known is better than nothing).
        self._rcp_state_cache: dict[str, dict[str, Any]] = {}
        # In-memory stream type override — changed by BoschStreamModeSwitch without reload.
        # None = use options setting; "local" / "auto" / "remote" = override.
        self._stream_type_override: str | None = None
        # Per-camera audio setting — True = audio+video on (default), False = snapshot-only
        self._audio_enabled: dict[str, bool] = {}
        # Per-camera card playback volume 0-100 — the automatable, cross-session
        # source of truth the Lovelace card applies to its <video> (browser has
        # no backend volume knob; this is a virtual preference). Mirrors the
        # _audio_enabled pattern: in-memory, seeded to a default per camera.
        self._audio_volume: dict[str, int] = {}
        # Auto-renewal tasks and generation counters per camera.
        # The generation counter increments on every new stream start,
        # allowing stale renewal loops to detect they belong to an old session.
        # Legacy task dict — kept for backwards-compat with any external code
        # that inspects it, but never populated now (use _renewal_tasks).
        self._auto_renew_tasks: dict[str, asyncio.Task[None]] = {}
        self._renewal_tasks: dict[str, asyncio.Task[None]] = {}
        self._auto_renew_generation: dict[str, int] = {}
        # Idle-session reaper tasks (one per LOCAL session, generation-tracked
        # like _renewal_tasks) + the monotonic timestamp at which a switch-OFF
        # session was first observed with no consumer. See _idle_session_reaper.
        self._reaper_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_idle_since: dict[str, float] = {}
        # Camera entity references — registered on entity setup, used by button/service
        self._camera_entities: dict[str, Any] = {}
        # Live-stream switch entity references — registered by
        # BoschLiveStreamSwitch.async_added_to_hass. _tear_down_live_stream
        # uses this to push the cleared "off" state to HA immediately, so the
        # UI does not show a stale "on" until the next coordinator refresh
        # tick. Reported by Thomas 2026-05-19: privacy toggle left the
        # live-stream switch visibly on.
        self._live_stream_entities: dict[str, Any] = {}
        # User-intent tracking for the live-stream switch. Decouples the
        # switch state from `_live_connections`: HA Core opens streams via
        # `async_create_stream` (Lovelace card preload, Cast, play_stream
        # service), each of which populates `_live_connections` and would
        # otherwise flip the switch to "on" even though the user never
        # toggled it. The set is keyed by cam_id and only mutated by
        # explicit `BoschLiveStreamSwitch.async_turn_on/off` calls plus
        # external teardowns (`_tear_down_live_stream` resets it because a
        # privacy-on / health-watchdog escalation cancels user intent too).
        # Bug 2026-05-20.
        self._user_intent_streams: set[str] = set()
        # Image entity references — registered on image platform setup
        # Keyed by cam_id; image entities call async_notify_refreshed() after
        # each disk-persist so WKWebView gets a fresh signed URL.
        self._image_entities: dict[str, Any] = {}
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
        self._slow_tier_deferred: set[str] = set()
        # Per-cam monotonic timestamp of when the *current* unbroken deferral
        # started, so a continuously-active stream cannot starve diagnostics
        # forever: once now - start >= SLOW_TIER_MAX_DEFER_SEC we force one read
        # despite the stream. Entry cleared whenever the deferred fetch runs.
        self._slow_tier_defer_since: dict[str, float] = {}
        # Cached data for types that are not re-fetched this tick
        self._cached_status: dict[str, str] = {}
        # Per-cam time (monotonic) the cloud last returned HTTP 444 (session
        # quota / not-ready, e.g. a freshly re-paired camera). For a short window
        # after, WRITE paths skip the cloud and go straight to the LAN/SHC
        # fallback instead of re-hitting the cloud for another 444. -inf = never.
        self._cloud_444_at: dict[str, float] = {}
        self._cached_events: dict[str, list[Any]] = {}
        # SHC local API state cache — keyed by cam_id
        # Each entry: {"device_id": str, "camera_light": bool|None, "privacy_mode": bool|None}
        self._shc_state_cache: dict[str, dict[str, Any]] = {}
        self._shc_devices_raw: list[Any] = []  # cached GET /smarthome/devices response
        self._last_shc_fetch: float = float(
            "-inf"
        )  # last SHC fetch (time.monotonic); -inf = never (SENTINEL_RULE)
        # SHC health tracking — skip SHC calls when offline to avoid latency
        self._shc_available: bool = True  # assume available until proven otherwise
        self._shc_fail_count: int = 0  # consecutive failures
        self._shc_last_check: float = float(
            "-inf"
        )  # last SHC probe (time.monotonic); -inf = never (SENTINEL_RULE)
        # _SHC_MAX_FAILS + _SHC_RETRY_INTERVAL are class-level constants
        # mirrored from const.py — see top-of-class declaration.
        # Pan position cache — keyed by cam_id, only populated for cameras with panLimit > 0
        self._pan_cache: dict[str, int | None] = {}
        # WiFi info cache — keyed by cam_id, populated from GET /wifiinfo
        self._wifiinfo_cache: dict[str, dict[str, Any]] = {}
        # Ambient light sensor cache — keyed by cam_id, populated from GET /ambient_light_sensor_level
        self._ambient_light_cache: dict[str, float | None] = {}
        # RCP data caches — keyed by cam_id, populated via RCP protocol over cloud proxy
        self._rcp_dimmer_cache: dict[str, int | None] = {}  # LED dimmer value 0–100
        self._rcp_privacy_cache: dict[
            str, int | None
        ] = {}  # privacy mask byte[1] (1=ON)
        self._rcp_clock_offset_cache: dict[
            str, float | None
        ] = {}  # camera clock offset vs server (seconds)
        self._rcp_lan_ip_cache: dict[
            str, str | None
        ] = {}  # camera LAN IP via RCP 0x0a36
        self._rcp_product_name_cache: dict[
            str, str | None
        ] = {}  # camera product name via RCP 0x0aea
        self._rcp_bitrate_cache: dict[
            str, list[int]
        ] = {}  # bitrate ladder kbps from 0x0c81
        # Phase 2 RCP caches
        self._rcp_alarm_catalog_cache: dict[
            str, list[dict[str, Any]]
        ] = {}  # alarm types from 0x0c38
        self._rcp_motion_zones_cache: dict[
            str, list[dict[str, Any]]
        ] = {}  # motion zones from 0x0c00
        self._rcp_motion_coords_cache: dict[
            str, list[dict[str, Any]]
        ] = {}  # zone coords from 0x0c0a
        self._rcp_tls_cert_cache: dict[
            str, dict[str, Any]
        ] = {}  # TLS cert info from 0x0b91
        self._rcp_network_services_cache: dict[
            str, list[str]
        ] = {}  # network services from 0x0c62
        self._rcp_iva_catalog_cache: dict[
            str, list[dict[str, Any]]
        ] = {}  # IVA analytics from 0x0b60
        # F4: ONVIF scopes cache — keyed by cam_id, from RCP 0x0a98 via LAN cbs-auth (300s slow-tier)
        self._rcp_onvif_scopes_cache: dict[str, dict[str, Any]] = {}
        # F6: RCP protocol version cache — keyed by cam_id, from RCP 0xff00+0xff04 via LAN (300s slow-tier)
        self._rcp_version_cache: dict[str, str | None] = {}
        # Commands that consistently return error=0x90 (not supported via proxy).
        # Key: cam_id, value: set of command hex strings. After 3 consecutive
        # failures the command is skipped for the rest of the session.
        self._rcp_cmd_failures: dict[
            str, dict[str, int]
        ] = {}  # cam_id → {cmd → fail_count}
        # Video quality preference — keyed by cam_id, runtime only (not persisted)
        # Values: "auto" | "high" | "low"
        self._quality_preference: dict[str, str] = {}
        # RCP session ID cache — keyed by proxy_hash, value (session_id, expires_monotonic)
        # Avoids 2 round-trip RCP handshake on every thumbnail/data fetch
        self._rcp_session_cache: dict[str, tuple[str, float]] = {}
        # Per-proxy_hash lock serializing RCP session opens. Bosch's cloud RCP
        # proxy only tolerates one live session per proxy_hash — two concurrent
        # openers (e.g. a privacy-mode toggle's snapshot trigger racing the
        # coordinator's RCP data refresh) each fire their own 0xff0c/0xff0d
        # handshake, and the proxy rejects whichever loses the race with
        # sessionid 0x00000000 ("proxy rejected"), seen live 2026-07-08.
        # Serializing on this lock makes the second caller await the first's
        # in-flight open and then read the now-populated cache instead.
        self._rcp_session_locks: dict[str, asyncio.Lock] = {}
        # Proxy URL cache — keyed by cam_id, value (urls[0], expires_monotonic)
        # Proxy leases last ~60s; cache for 50s to skip PUT /connection on warm refreshes
        self._proxy_url_cache: dict[str, tuple[str, float]] = {}
        # Per-camera lock serializing async_fetch_live_snapshot calls.
        # Prevents duplicate PUT /connection when first-load + proactive refresh
        # overlap, or when a user rapid-triggers snapshots.
        self._snapshot_fetch_locks: dict[str, asyncio.Lock] = {}
        # Per-camera lock serializing try_live_connection(). Initialised here
        # (not lazily) so _get_stream_lock stays a plain dict lookup.
        self._stream_locks: dict[str, asyncio.Lock] = {}
        # Short-lived cache for async_fetch_fresh_event_snapshot.
        # After an FCM push, async_update_listeners() wakes all HA consumers
        # simultaneously; each calls async_image() → async_fetch_fresh_event_snapshot.
        # Without coalescing this fires 8+ identical cloud round-trips in ~200 ms.
        # The lock (created lazily per cam_id) serialises concurrent callers:
        # the first one fetches and stores the result; the rest acquire the lock
        # after it releases, find the cache hit, and return without a network call.
        # TTL=8s covers the burst window while staying well inside the 60s scan cycle.
        self._fresh_snap_cache: dict[str, tuple[bytes, float]] = {}
        self._fresh_snap_locks: dict[str, asyncio.Lock] = {}
        # AI snapshot-description rate limiter (F3): per-camera cooldown +
        # global daily budget. monotonic sentinel = -inf (SENTINEL_RULE: CI VMs
        # boot ~200s monotonic, 0.0 would falsely satisfy the cooldown).
        self._ai_last_call: dict[str, float] = {}
        self._ai_day_count: int = 0
        self._ai_day_stamp: str = ""
        self._ai_in_flight: int = 0
        self._ai_budget_logged_day: str = ""
        # Persistent storage for the daily AI budget counter (survives restart/reload).
        self._ai_budget_store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}_ai_budget"
        )
        # Last-seen event IDs per camera — used to detect new events for snapshot refresh
        self._last_event_ids: dict[str, str] = {}
        # Epoch timestamp of coordinator start — used to reject event downloads for
        # events that predate this session (e.g. queued FCM pushes arriving after reload).
        self._download_started_at: float = time.time()
        # Alert-sent cache keyed by event_id → monotonic timestamp. Bosch can
        # send two FCM pushes ~10 s apart for the same MOVEMENT event (once at
        # detection start, again when the clip is finalized), and concurrent
        # push handlers race on `_last_event_ids` before either commits. This
        # cache blocks the second alert dispatch when the ID was already
        # alerted within 60 s. Pruned to the 32 most recent entries to bound
        # memory.
        self._alert_sent_ids: dict[str, float] = {}
        # FCM push client — near-instant event detection via Firebase Cloud Messaging
        self._fcm_client = None  # FcmPushClient instance (or None if disabled)
        self._fcm_token: str = ""  # FCM registration token
        self._fcm_running: bool = False
        self._fcm_last_push: float = float(
            "-inf"
        )  # monotonic time of last received push
        # Monotonic time the FCM listener last started successfully. Used by the
        # delivery-death watchdog (issue #36) as the grace reference when no push
        # has ever arrived: push delivery is only judged "dead" once the listener
        # has been up for FCM_DELIVERY_DEAD_AFTER_SEC, so a still-warming-up start
        # is never falsely condemned, while a genuinely dead-from-start Bosch
        # registration is still caught once the grace elapses.
        self._fcm_started_at: float = float("-inf")
        self._fcm_healthy: bool = False  # True when FCM is connected and receiving
        # Set True by the event-poll path when it detects a new event that FCM
        # push never delivered (issue #36 silent-delivery-death). The supervisor
        # checks this flag at the top of each iteration and does a hard-heal
        # (purge + re-register) when it is set. Cleared by the supervisor.
        self._fcm_force_hard_heal: bool = False
        # The supervisor asyncio.Task that keeps the FCM listener alive. Created
        # by async_ensure_fcm_supervisor; cancelled by async_stop_fcm_supervisor.
        self._fcm_supervisor_task: asyncio.Task[None] | None = None
        # Serialises every FCM start/stop/self-heal so the setup-time start
        # and the watchdog's self-heal can't run concurrently. Live bug
        # 2026-05-21: without the lock the initial async_start_fcm_push from
        # async_setup_entry ran in parallel with the first coordinator tick's
        # self-heal — two checkin_or_register() calls registered two device
        # tokens in 2 s; the first listener died with NoneType-in-_login
        # (orphaned client whose credentials were overwritten by the second).
        self._fcm_start_lock: asyncio.Lock = asyncio.Lock()
        self._fcm_push_mode: str = (
            "unknown"  # "auto" once FCM listener is up, else "unknown"
        )
        # Lock serializing cross-thread FCM state writes.
        # _on_fcm_push fires in a Firebase thread; the event loop reads these fields.
        self._fcm_lock: threading.Lock = threading.Lock()
        # Unread events count cache — keyed by cam_id, populated from GET /unread_events_count
        self._unread_events_cache: dict[str, int] = {}
        # Privacy sound override cache — keyed by cam_id, populated from GET /privacy_sound_override
        self._privacy_sound_cache: dict[str, bool | None] = {}
        # Commissioned status cache — keyed by cam_id, populated from GET /commissioned
        self._commissioned_cache: dict[str, dict[str, Any]] = {}
        # Feature flags — populated once from GET /v11/feature_flags
        self._feature_flags: dict[str, bool] = {}
        # Protocol version check — run once at startup
        self._protocol_checked: bool = False
        self._integration_version = _INTEGRATION_VERSION
        # Firmware update status cache — keyed by cam_id, from GET /firmware
        self._firmware_cache: dict[str, dict[str, Any]] = {}
        # SMB maintenance — last run timestamps (monotonic)
        self._last_smb_cleanup: float = float(
            "-inf"
        )  # float('-inf') → runs on first tick
        # Token refresh failure tracking — alert once, not every 80s
        self._token_alert_sent: bool = False  # True after first alert sent
        self._token_fail_count: int = 0  # consecutive refresh failures
        # Bosch auth-server outage tracking — distinct from hard failures.
        # 5xx from Keycloak = Bosch infrastructure problem, NOT user/config issue:
        # no reauth trigger, no escalation, just back off and retry.
        self._auth_outage_count: int = 0  # consecutive 5xx responses
        self._auth_outage_alert_sent: bool = False
        self._auth_outage_next_retry_ts: float = float("-inf")  # monotonic time gate
        # Cached LOCAL Digest credentials per camera — survives live-connection
        # teardown. Populated on every successful PUT /connection LOCAL and used
        # as a fallback path (snap.jpg, Gen2 RCP privacy writes) when the Bosch
        # cloud is unreachable. Creds are ephemeral (camera rotates them on
        # reboot) but usually stable for minutes to hours.
        # {cam_id: {"user": str, "password": str, "host": str, "port": int, "ts": monotonic}}
        self._local_creds_cache: dict[str, dict[str, Any]] = {}
        # Serializes _ensure_valid_token so concurrent refreshes don't race
        # (Keycloak rotates refresh_token and invalidates the previous one —
        # two parallel POSTs with the same token → first wins, second gets
        # invalid_grant and permanently breaks the loop).
        self._token_refresh_lock: asyncio.Lock = asyncio.Lock()
        # TimerHandle for the next scheduled proactive token refresh.
        # Held so async_unload_entry can cancel it — otherwise a config
        # reload leaks timers that still fire against a dead coordinator.
        self._token_refresh_handle: asyncio.TimerHandle | None = None
        # Strong references to fire-and-forget background tasks so the GC
        # does not cancel them mid-flight. Self-removing via done_callback.
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        # Per-camera flag: set True after 3 consecutive session-renewal
        # failures (LOCAL auto-renew loop). Flipped back to False after
        # a successful renewal. Exposed via is_session_stale().
        self._session_stale: dict[str, bool] = {}
        # Timestamp overlay cache — keyed by cam_id, from GET /timestamp
        self._timestamp_cache: dict[str, bool | None] = {}
        # Status LED cache — keyed by cam_id, from GET /ledlights (Gen2 only)
        self._ledlights_cache: dict[str, bool | None] = {}
        # Lens elevation cache — keyed by cam_id, from GET /lens_elevation (Gen2 only)
        self._lens_elevation_cache: dict[str, float | None] = {}
        # Audio settings cache — keyed by cam_id, from GET /audio (Gen2 only)
        self._audio_cache: dict[str, dict[str, Any]] = {}
        # Motion light cache — keyed by cam_id, from GET /lighting/motion (Gen2 only)
        self._motion_light_cache: dict[str, dict[str, Any]] = {}
        # Image rotation 180° flag — keyed by cam_id, indoor cameras only.
        # No API call — purely a client-side display flag for ceiling-mounted cams.
        # Read by camera.async_camera_image (rotates JPEG via PIL) and by the
        # Pan number entity (inverts sign so "right" stays "right" on screen).
        # State is owned by BoschImageRotation180Switch (RestoreEntity).
        self._image_rotation_180: dict[str, bool] = {}
        # External stream URL exposure flag — keyed by cam_id, default False.
        # Owned by BoschExternalStreamSwitch (RestoreEntity). When True, the
        # per-camera BoschStreamUrlSensor + BoschStreamUrlSubSensor expose the
        # current LOCAL/REMOTE rtspsUrl (inst=1) and a derived inst=2 sub-stream
        # URL so users can paste them into Frigate / BlueIris configs.
        # Default OFF — opt-in per camera, avoids entity-spam.
        self._external_stream_enabled: dict[str, bool] = {}
        # Ambient lighting config cache — keyed by cam_id, from GET /lighting/ambient (Gen2 only)
        self._ambient_lighting_cache: dict[str, dict[str, Any]] = {}
        # Lighting switch cache — keyed by cam_id, from GET /lighting/switch (Gen2 only)
        self._lighting_switch_cache: dict[str, dict[str, Any]] = {}
        # Global lighting config cache — keyed by cam_id, from GET /lighting (Gen2 only)
        # Contains: darknessThreshold (0.0-1.0), softLightFading (bool)
        self._global_lighting_cache: dict[str, dict[str, Any]] = {}
        # Notification type toggles cache — keyed by cam_id, from GET /notifications
        self._notifications_cache: dict[str, dict[str, Any]] = {}
        # Rules cache — keyed by cam_id, from GET /rules
        self._rules_cache: dict[str, list[Any]] = {}
        # Cloud motion zones cache — keyed by cam_id, from GET /motion_sensitive_areas
        self._cloud_zones_cache: dict[str, list[Any]] = {}
        # Cloud privacy masks cache — keyed by cam_id, from GET /privacy_masks
        self._cloud_privacy_masks_cache: dict[str, list[Any]] = {}
        # Lighting options cache — keyed by cam_id, from GET /lighting_options
        self._lighting_options_cache: dict[str, dict[str, Any]] = {}
        # Intrusion detection config cache — keyed by cam_id, from GET /intrusionDetectionConfig (Gen2 only)
        self._intrusion_config_cache: dict[str, dict[str, Any]] = {}
        # Audio detection config cache — keyed by cam_id, from GET /audioDetectionConfig
        # (Gen2 Audio-Plus). Contains: detectGlassBreak, detectFireAlarm (both bool).
        self._audio_detection_cache: dict[str, dict[str, Any]] = {}
        # Alarm settings cache — from GET /alarm_settings (Gen2 Indoor II only).
        # Contains: alarmMode, alarmDelayInSeconds, alarmActivationDelaySeconds,
        #          preAlarmMode, preAlarmDelayInSeconds
        self._alarm_settings_cache: dict[str, dict[str, Any]] = {}
        # Alarm status cache — from GET /alarmStatus (Gen2 Indoor II only).
        self._alarm_status_cache: dict[str, dict[str, Any]] = {}
        # Last observed alarmType per cam — for rising-edge detection of intrusion
        # events. Fires `bosch_shc_camera_intrusion` when alarmType transitions
        # from NONE/empty to a real alarm type (e.g. INTRUSION_DETECTED).
        self._last_alarm_type: dict[str, str] = {}
        # Intrusion system arming cache — derived from alarmStatus (armed/disarmed).
        # Set by BoschAlarmSystemArmSwitch on successful PUT /intrusionSystem/arming.
        self._arming_cache: dict[str, bool] = {}
        # Status LED brightness cache (Gen2 Indoor II) — from GET /iconLedBrightness.
        # Value range: 0-4 (0 = off, 4 = max).
        self._icon_led_brightness_cache: dict[str, int] = {}
        # Gen2 polygon zones cache — keyed by cam_id, from GET /zones (Gen2 only)
        # Contains polygon zones with trigger: "PERSON", maskType, color fields
        self._gen2_zones_cache: dict[str, list[Any]] = {}
        # Gen2 private areas cache — keyed by cam_id, from GET /privateAreas (Gen2 only)
        # Contains privacy mask polygons with color: "#000000"
        self._gen2_private_areas_cache: dict[str, list[Any]] = {}
        # userToken cache — keyed by cam_id, from GET /credentials
        self._user_token_cache: dict[str, str] = {}
        # Separate timer for lighting/switch — polled every tick (60s) instead of slow tier (300s)
        # Bosch app polls this every ~40s; slow tier (300s) is too slow for responsive light state
        self._last_lighting_switch: float = float("-inf")
        # Write-lock timestamps — prevent coordinator from overwriting optimistic state
        # with stale cloud data in the seconds after a successful API write.
        # Keyed by cam_id, value is monotonic time of last successful write.
        self._light_set_at: dict[str, float] = {}  # lighting_override write timestamp
        self._notif_set_at: dict[
            str, float
        ] = {}  # enable_notifications write timestamp
        # Tracks cam_ids for which a "notifications disabled" WARN has been logged.
        # Cleared when the camera re-enables notifications so the WARN re-fires if
        # they are disabled again later.
        self._notif_disabled_logged: set[str] = set()
        # Tracks cam_ids for which a "firmware update available" INFO has been
        # logged. Cleared once the update installs (upToDate flips back to True)
        # so the INFO re-fires for the next update.
        self._fw_update_alerted: set[str] = set()
        self._privacy_set_at: dict[str, float] = {}  # privacy write timestamp
        self._privacy_sound_set_at: dict[
            str, float
        ] = {}  # privacy_sound_override write
        self._timestamp_set_at: dict[str, float] = {}  # timestamp overlay write
        self._ledlights_set_at: dict[str, float] = {}  # status LED write
        self._arming_set_at: dict[str, float] = {}  # alarm system arm/disarm write
        self._intrusion_config_set_at: dict[
            str, float
        ] = {}  # intrusionDetectionConfig write
        self._audio_detection_set_at: dict[
            str, float
        ] = {}  # audioDetectionConfig write (glass-break / fire-alarm)
        self._motion_set_at: dict[str, float] = {}  # motion sensitivity write
        self._alarm_settings_set_at: dict[str, float] = {}  # alarm_settings write
        # firmware install-trigger write — held just long enough for the
        # optimistic `updating=True` (set by BoschFirmwareUpdate.async_install)
        # to survive one slow-tier poll cycle before Bosch's own backend
        # reports the real in-progress state.
        self._firmware_set_at: dict[str, float] = {}
        self._WRITE_LOCK_SECS = (
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
        self._hw_version: dict[str, str] = {}
        # TLS proxy for LOCAL RTSPS streams — keyed by cam_id
        # FFmpeg can't handle RTSPS + Digest auth with self-signed certs.
        # The proxy accepts plain TCP and forwards to camera over TLS.
        self._tls_proxy_ports: dict[str, int] = {}  # cam_id → local port
        # ── Frigate / external-recorder persistent RTSP front-doors ───────────
        # Per-camera always-on credential-free RTSP endpoint (frigate_endpoint.py).
        # Owned per-camera by the High/Low BoschFrigate*Switch (RestoreEntity);
        # the front-door runner binds a sticky port and opens the Bosch session
        # lazily on the first recorder connect. Default OFF (opt-in).
        self._frigate_runner: FrontDoorRunner | None = None
        self._frigate_high_enabled: dict[str, bool] = {}
        self._frigate_low_enabled: dict[str, bool] = {}
        self._frigate_sticky_port: dict[
            str, int
        ] = {}  # cam_id → stable front-door port
        # Auto-rebuild backoff: monotonic ts of last _on_tls_proxy_died rebuild.
        # Prevents a rebuild storm when the new proxy also immediately dies
        # because the camera is still flapping (WiFi jitter, brief Bosch FW glitch).
        self._tls_proxy_rebuild_last: dict[str, float] = {}
        # Stream error tracking — consecutive FFmpeg failures per camera.
        # After max_stream_errors, auto-fallback from LOCAL → REMOTE.
        # `_stream_error_at` records monotonic ts of the last record_stream_error
        # tick so AUTO mode can time-decay the counter (cf. _STREAM_ERROR_TTL_SEC
        # in _try_live_connection_inner). Without decay a one-off LAN blip
        # (router reboot, transient WLAN dropout) pins the cam to REMOTE forever
        # because record_stream_success only fires on a successful LOCAL stream
        # and AUTO has already stopped attempting LOCAL.
        self._stream_error_count: dict[str, int] = {}
        self._stream_error_at: dict[str, float] = {}
        self._stream_fell_back: dict[
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
        self._local_rescue_attempts: dict[str, int] = {}
        self._local_rescue_at: dict[
            str, float
        ] = {}  # cam_id → monotonic ts of last rescue
        # TCP reachability cache — (reachable, monotonic_ts). TTL 60s.
        # Populated by _async_local_tcp_ping (status loop) and stream pre-check.
        self._lan_tcp_reachable: dict[str, tuple[bool, float]] = {}
        # Monotonic timestamp of the last successful local-RCP write per cam.
        # The camera briefly tears down its cloud session when Digest creds
        # rotate after an RCP write; we use this to suppress LAN-offline
        # false positives during that ~30 s window. Default `float('-inf')`
        # per SENTINEL_RULE so "never written" never satisfies the grace check.
        self._local_write_at: dict[str, float] = {}
        # During a cloud outage we kick a periodic ping of every known cam IP
        # so the card / switches have a recent reachability signal even though
        # the cloud-driven status loop is blocked. Tracks last outage-ping
        # tick to throttle to once per ~30 s.
        self._last_outage_ping_at: float = float("-inf")
        # Active LOCAL-promotion cooldown: monotonic ts of last attempt to lift
        # an active REMOTE-fallback stream onto LOCAL via Stream.update_source.
        # Prevents ping-pong if LAN is flapping in/out of reachability.
        self._local_promote_at: dict[str, float] = {}
        # SSL context created lazily on first use (ssl.create_default_context
        # is blocking I/O — must not run in the event loop)
        self._tls_ssl_ctx: ssl.SSLContext | None = None
        # Offline tracking — per camera, monotonic timestamp when first detected offline.
        # Used to extend status check intervals for persistently offline cameras.
        self._offline_since: dict[str, float] = {}
        # Extended offline interval: cameras offline for >15 min are checked every 15 min
        # instead of the normal interval_status (5 min), reducing unnecessary cloud calls.
        self._OFFLINE_EXTENDED_INTERVAL = 900  # 15 minutes
        # Per-camera status check timestamps (for extended offline intervals)
        self._per_cam_status_at: dict[str, float] = {}
        # Stream warm-up state — eagerly initialised so clear_stream_warming() and
        # is_stream_warming() never need hasattr guards. Lazy init (hasattr) caused
        # clear_stream_warming() calls before first is_stream_warming() to silently
        # no-op, leaving the entity badge stuck on "warming" after stream start.
        self._stream_warming: set[str] = set()
        self._stream_warming_started: dict[str, float] = {}
        # Bosch community RSS-derived maintenance announcement. Periodic refresh
        # every _MAINTENANCE_INTERVAL_S; reactive refresh on cloud 5xx (rate-
        # limited by _MAINTENANCE_REACTIVE_COOLDOWN_S). Cleared explicitly only
        # when the fetcher returns a fresh window — transient community-site
        # outages leave the previous value in place so the sensor stays stable.
        from .maintenance import (
            MaintenanceWindow,
        )  # local import: avoid module-load order issues

        self._maintenance_cache: MaintenanceWindow | None = None
        self._maintenance_last_fetch: float = float("-inf")
        self._MAINTENANCE_INTERVAL_S: float = 3600.0
        self._MAINTENANCE_REACTIVE_COOLDOWN_S: float = 300.0
        # (link, state) of the last user-facing notification we sent for a
        # maintenance window. Dedupes so the same window announces at most
        # three times (scheduled / active / past). In-memory only — a HA
        # restart inside a maintenance window may re-announce, accepted as a
        # v1 trade-off vs. persistence overhead.
        self._maintenance_notified_key: tuple[str, str] | None = None
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
        self._offline_seen_at: dict[str, float] = {}
        # Bosch cloud reachability tracker. Fires user notifications on the
        # transitions (healthy → outage) and (outage → recovered). One-tick
        # blips are suppressed by requiring _CLOUD_OUTAGE_NOTIFY_AFTER_S of
        # continuous failure before announcing the outage. The recovery
        # notification fires immediately when the next tick succeeds. While
        # an RSS-announced maintenance window is `active` we stay silent —
        # the maintenance lifecycle notifier already told the user.
        self._cloud_outage_started_at: float | None = None
        self._cloud_outage_notified: bool = False
        self._CLOUD_OUTAGE_NOTIFY_AFTER_S: float = 60.0
        # ── Session-quota (HTTP 444) tracker ─────────────────────────────────
        # Timestamps of recent 444 hits per camera (monotonic). Entries older
        # than _SESSION_QUOTA_WINDOW_S are pruned on each new hit. When ≥3
        # hits occur within the window a persistent notification is shown.
        self._session_quota_hits: dict[str, list[float]] = {}
        self._SESSION_QUOTA_WINDOW_S: float = 300.0  # 5 minutes
        self._SESSION_QUOTA_NOTIFY_THRESHOLD: int = 3
        # ── Mini-NVR (Phase 1 MVP) — see custom_components/.../recorder.py ───
        # _nvr_processes:  cam_id → live ffmpeg subprocess (one per recording).
        # _nvr_user_intent: persisted switch state (True = user wants to record).
        # _nvr_error_state: cam_id → human-readable error after crash-loop guard.
        # _nvr_recent_crash: monotonic ts of last ffmpeg exit (crash-window math).
        # _last_nvr_cleanup: last daily retention purge (monotonic).
        # The recorder is a third consumer of the existing TLS proxy — it does
        # NOT open a new RTSP session against the camera (Bosch caps concurrent
        # sessions at 2-3). LAN-only: only runs when _connection_type=LOCAL +
        # camera ONLINE. See `docs/mini-nvr-concept.md` §2.
        self._nvr_processes: dict[str, asyncio.subprocess.Process] = {}
        self._nvr_user_intent: dict[str, bool] = {}
        self._nvr_error_state: dict[str, str] = {}
        self._nvr_recent_crash: dict[str, float] = {}
        self._last_nvr_cleanup: float = float(
            "-inf"
        )  # float('-inf') → runs on first tick
        # Phase 4: pre-roll buffer — one short-segment ffmpeg per camera writing to tmpfs.
        # Keyed by cam_id, lifecycle mirrors _nvr_processes but independently controlled.
        self._nvr_preroll_processes: dict[str, asyncio.subprocess.Process] = {}
        self._nvr_preroll_last_crash: dict[str, float] = {}
        self._nvr_preroll_segment_counts: dict[str, int] = {}
        self._nvr_preroll_tasks: dict[str, asyncio.Task[Any]] = {}
        # Drain watcher state — populated by recorder.sync_drain_tick. Used by
        # BoschNvrStateSensor to render `target` / `pending_uploads` /
        # `failed_uploads` / `last_segment_age_s` attributes without coupling
        # the sensor to the watcher.
        self._nvr_drain_state: dict[str, Any] = {}
        self._nvr_drain_failures: dict[str, int] = {}
        # Per-coordinator drain watcher task. Started in async_setup_entry,
        # cancelled in async_unload_entry. NOT per-camera — one watcher serves
        # the entire integration.
        self._nvr_drain_task: asyncio.Task[None] | None = None

    def get_model_config(self, cam_id: str) -> Any:
        """Return CameraModelConfig for a camera (from models.py)."""
        from .models import get_model_config

        hw = self._hw_version.get(cam_id, "CAMERA")
        return get_model_config(hw)

    @staticmethod
    def _err_str(err: BaseException) -> str:
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

    def _is_write_locked(self, cam_id: str, set_at_dict: dict[str, float]) -> bool:
        """Return True if a fresh user-write is still inside the eventual-consistency window.

        Used by every coordinator slow-tier endpoint handler that polls a
        cloud field also writable from a switch entity. Without this guard,
        a poll within `_WRITE_LOCK_SECS` of the user toggle can revert the
        cache to the stale cloud value before it has caught up — the bug
        shape that bit privacy_mode + camera_light in v11.0.x. Keep the
        whole pattern in one helper so future cache fields can opt in with
        a one-liner.
        """
        ts = set_at_dict.get(cam_id)
        return ts is not None and (time.monotonic() - ts) < self._WRITE_LOCK_SECS

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

        Set by `_auto_renew_local_session` after 3 consecutive full-renewal
        failures; cleared on the first successful renewal. Entities can use
        this in their `available` property to avoid showing a frozen stream
        as if it were healthy.
        """
        return bool(self._session_stale.get(cam_id, False))

    def _refresh_local_creds_from_heartbeat(
        self,
        cam_id: str,
        resp_text: str,
        generation: int,
        elapsed: float,
    ) -> None:
        """Cache fresh LOCAL creds from a heartbeat PUT response and rebuild
        the cached rtspsUrl so the next stream-worker restart picks them up.

        Bosch's PUT /v11/video_inputs/{id}/connection LOCAL returns a fresh
        digest user/password pair on every call; the previous pair stops
        accepting NEW RTSP connects within ~60 s (the maxSessionDuration
        default Bosch announces). The active RTSP connection survives, but
        without this refresh a reconnect after idle would fail with HTTP 401
        and trip the LOCAL→REMOTE fallback. Capture analysis in
        captures/api-findings.md §1 shows the iOS app handles this by
        firing PUT at ~5 Hz during live view; we settle for one PUT per
        heartbeat_interval (30 s on Indoor) which is more than enough.

        The handler is best-effort: any parse / state error is swallowed,
        the heartbeat keeps running, and the reactive 401 rescue in
        _handle_stream_worker_error remains as a safety net.
        """
        try:
            import json as _json
            from urllib.parse import quote as _q

            rj = _json.loads(resp_text or "{}")
            new_user = rj.get("user")
            new_pass = rj.get("password")
            if not (new_user and new_pass):
                return  # response without creds — nothing to refresh
            live = self._live_connections.get(cam_id)
            if not live or live.get("_connection_type") != "LOCAL":
                return  # session torn down or already on REMOTE
            old_user = live.get("_local_user")
            old_pass = live.get("_local_password")
            if old_user == new_user and old_pass == new_pass:
                return  # creds unchanged (rare but possible — skip noisy update)
            proxy_port = self._tls_proxy_ports.get(cam_id)
            if not proxy_port:
                return  # TLS proxy not running — nothing to point the URL at
            old_url = live.get("rtspsUrl") or live.get("rtspUrl") or ""
            inst_val = 1
            qs = old_url.split("?", 1)[-1] if "?" in old_url else ""
            for tok in qs.split("&"):
                if tok.startswith("inst="):
                    try:
                        inst_val = int(tok.split("=", 1)[1])
                    except ValueError:
                        pass
                    break
            # Audio track is ALWAYS requested. switch.<cam>_audio is now a
            # lightweight synced MUTE preference applied card-side (video.muted),
            # so toggling it no longer re-opens the stream (AAC ≈ negligible
            # bandwidth) — this is what makes mute/unmute sync instantly across
            # devices and fixes the audio-toggle reconnect jank (#22). 2026-06-01.
            audio_param = "&enableaudio=1"
            mcfg = self.get_model_config(cam_id)
            new_url = (
                f"rtsp://{_q(new_user, safe='')}:{_q(new_pass, safe='')}@"
                f"127.0.0.1:{proxy_port}/rtsp_tunnel?inst={inst_val}"
                f"{audio_param}&fmtp=1&maxSessionDuration={mcfg.max_session_duration}"
            )
            live["_local_user"] = new_user
            live["_local_password"] = new_pass
            live["rtspsUrl"] = new_url
            live["rtspUrl"] = new_url
            cache = self._local_creds_cache.get(cam_id, {})
            cache.update(
                {
                    "user": new_user,
                    "password": new_pass,
                    "ts": time.monotonic(),
                }
            )
            self._local_creds_cache[cam_id] = cache
            cam_entity = self._camera_entities.get(cam_id)
            stream = getattr(cam_entity, "stream", None) if cam_entity else None
            if stream is not None:
                try:
                    stream.update_source(new_url)
                except Exception as err:
                    _LOGGER.debug(
                        "Heartbeat: Stream.update_source for %s failed (will heal at next worker restart): %s",
                        cam_id[:8],
                        err,
                    )
            # go2rtc (WebRTC) holds the proxy URL with the OLD embedded creds
            # too. Re-register go2rtc with the fresh URL so WebRTC-only viewers
            # (those never opening an HLS stream) don't 401 → silent video freeze
            # once the camera rotates the old creds out (~60 s grace). This must
            # run UNCONDITIONALLY — i.e. regardless of whether an HA HLS stream
            # object exists — because a pure WebRTC viewer never opens one.
            # HA Stream (HLS) was updated above (stream is not None path); go2rtc
            # is always updated here. Tracked bg task (sync method — can't await).
            # 2026-06-01, decoupled 2026-06-22 (B2 fix: WebRTC-only stale creds).
            go2rtc_task = self.hass.async_create_task(
                self._register_go2rtc_stream(cam_id, new_url),
                name=f"bosch_go2rtc_reregister_{cam_id[:8]}",
            )
            self._bg_tasks.add(go2rtc_task)
            go2rtc_task.add_done_callback(self._bg_tasks.discard)
            # NVR sidecar: unlike a fresh connect, the ESTABLISHED ffmpeg RTSP
            # session survives cred rotation (see docstring above) — no
            # restart needed here. A proactive restart on every heartbeat used
            # to run unconditionally, which on fast-rotating Gen1 cameras
            # (15 s heartbeat) killed and respawned ffmpeg ~4x/minute,
            # truncating every recorded segment to a few seconds (GitHub
            # issue #41). Genuine ffmpeg failures (the connection actually
            # dying, e.g. once creds truly go stale past the ~60 s grace) are
            # already recovered by `_watch_recorder`, which respawns with the
            # freshly-cached `rtspsUrl` set above.
            _LOGGER.debug(
                "Heartbeat refreshed creds for %s (gen=%d, %.0fs into session, user=%s)",
                cam_id[:8],
                generation,
                elapsed,
                new_user,
            )
        except Exception as err:
            _LOGGER.debug(
                "Heartbeat cred-refresh skipped for %s: %s",
                cam_id[:8],
                err,
            )

    def record_stream_error(self, cam_id: str) -> None:
        """Record a stream error. After max_stream_errors, next stream start uses REMOTE."""
        # The counter exists to suppress LOCAL after consecutive LAN failures.
        # Only count errors on a CONFIRMED-LOCAL session: REMOTE errors are
        # unrelated to LAN health, and a None type (no session / torn down, e.g.
        # a worker error firing after _tear_down_live_stream cleared the dict)
        # is not a LAN-health signal either. Counting those would pin the cam to
        # REMOTE after an unrelated hiccup even when LAN works fine again.
        live = self._live_connections.get(cam_id, {})
        if live.get("_connection_type") != "LOCAL":
            return
        count = self._stream_error_count.get(cam_id, 0) + 1
        self._stream_error_count[cam_id] = count
        self._stream_error_at[cam_id] = time.monotonic()
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
        if self._stream_error_count.get(cam_id, 0) > 0:
            _LOGGER.info(
                "Stream recovered for %s — resetting error counter", cam_id[:8]
            )
        self._stream_error_count[cam_id] = 0
        self._stream_error_at.pop(cam_id, None)
        self._stream_fell_back[cam_id] = False
        self._local_rescue_attempts.pop(cam_id, None)
        self._local_rescue_at.pop(cam_id, None)

    async def _tear_down_live_stream(
        self, cam_id: str, expected_generation: int | None = None
    ) -> None:
        """Stop an active LOCAL/REMOTE live stream cleanly.

        Shared teardown for:
          * `BoschLiveStreamSwitch.async_turn_off` (user pressed stop).
          * `BoschPrivacyModeSwitch.async_turn_on` (camera shutter closes, any
            streaming session must also end — the TLS proxy's camera-side
            socket is dead anyway once privacy engages).
          * The stream-worker-error listener and health watchdog (when they
            force a REMOTE fallback).

        Steps:
          1. Cancel the LOCAL keepalive task (tracked in `_renewal_tasks`;
             the legacy `_auto_renew_tasks` dict is never populated).
          2. Clear the per-cam session state (`_live_connections`,
             `_live_opened_at`).
          3. Stop the TLS proxy server socket — closing TCP is enough for
             the camera to detect disconnect and drop its RTSP session
             (LED off). Do NOT send PUT /connection here; that starts a
             NEW session and keeps the camera streaming.
          4. Unregister from go2rtc so the shared RTSP→WebRTC endpoint
             stops serving a dead URL.
          5. Stop HA's `Stream` object on the camera entity. Without this
             the stream_worker keeps its cached URL and auto-restarts
             against the (now-dead) TLS proxy forever — that's what
             produced the yellow→blue→yellow cycle reported in #6 when
             Privacy was flipped while a stream was running, and what our
             own `_StreamWorkerErrorListener` would then try to "fix" by
             falling back to REMOTE — which also fails since the camera
             returns HTTP 443 sh:camera.in.privacy.mode.

        Runs entirely under the per-cam stream lock (`_get_stream_lock`) —
        the SAME lock `try_live_connection`/`_try_live_connection_inner` hold
        for the whole build/rebuild. Without this, an unlocked call here
        (idle reaper, external-privacy detection, frigate-idle-timeout,
        REMOTE-lifetime terminator — none of them go through
        `try_live_connection`) could race a concurrent renewal: the renewal
        publishes a brand-new proxy port + `Stream.update_source()` first,
        then this teardown runs a beat later and pops `_live_connections`
        (line below) — which also silently defeats `record_stream_error`'s
        LOCAL-only counting — and closes the port the renewal just
        published, leaving the new session dead with no error escalation
        and no automatic recovery (live incident 2026-07-04, Innenbereich:
        stream-worker looped on "Connection refused" against a rotated
        session for 4+ minutes until a manual HA restart).

        `expected_generation`: pass the `_auto_renew_generation` value the
        caller observed when it DECIDED to tear down (idle reaper,
        frigate-idle-timeout, REMOTE-lifetime terminator — all watchdogs that
        read stale state, then queue this call). Locking closed the old race
        but opened a new one: this call can now block for the whole duration
        of a concurrent rebuild, and a rebuild bumps the generation — so by
        the time this call finally gets the lock, the stale "tear it down"
        decision may no longer apply to whatever session exists NOW (a fresh,
        healthy, unrelated-to-the-original-reason session). Re-checking the
        generation under the lock — before touching any state — lets us bail
        out instead of destroying a session the caller never actually meant
        to kill. Callers with a hard, still-true-regardless-of-session fact
        (privacy ON, user pressed stop) pass `None` (default) — always apply.
        """
        async with self._get_stream_lock(cam_id):
            if (
                expected_generation is not None
                and self._auto_renew_generation.get(cam_id, 0) != expected_generation
            ):
                _LOGGER.debug(
                    "Teardown for %s skipped — session generation changed "
                    "(%s) since the caller decided to tear down (expected %s); "
                    "a newer rebuild superseded the stale trigger",
                    cam_id[:8],
                    self._auto_renew_generation.get(cam_id, 0),
                    expected_generation,
                )
                return
            task = self._renewal_tasks.pop(cam_id, None)
            if task and not task.done():
                task.cancel()
            # Cancel the idle reaper too. When the reaper itself triggers teardown
            # it has already returned (it schedules teardown as a separate task), so
            # this cancel is a no-op in that path; for all other teardown triggers
            # (switch off, privacy on, REMOTE fallback) it stops the reaper loop.
            reaper = self._reaper_tasks.pop(cam_id, None)
            if reaper and not reaper.done():
                reaper.cancel()
            # Step 1 — clear visible state FIRST. BoschLiveStreamSwitch.is_on
            # reads from `_user_intent_streams`; if anything below raises (NVR
            # child gone, file lock, ...) the user-visible switch would otherwise
            # stay stuck on "on". Reported by Thomas 2026-05-19 (Mini-NVR BETA).
            # Reset user intent too — privacy-on, health-watchdog REMOTE-escalation
            # and external teardowns all genuinely end the user's "live stream
            # wanted" intent.
            self._user_intent_streams.discard(cam_id)
            self._live_connections.pop(cam_id, None)
            self._live_opened_at.pop(cam_id, None)
            self._session_idle_since.pop(cam_id, None)
            # Clear the warm-up flag proactively. is_stream_warming() would lazily
            # clear it (Scenario 1: no live conn), but a privacy-ON teardown is
            # immediately followed by a privacy cooldown check that calls
            # is_stream_warming — leaving it set risks blocking the very next
            # privacy toggle. Discard here so the toggle is never spuriously gated.
            self._stream_warming.discard(cam_id)
            self._stream_warming_started.pop(cam_id, None)
            self._stream_error_count.pop(cam_id, None)
            self._stream_error_at.pop(cam_id, None)
            self._stream_fell_back.pop(cam_id, None)
            self._local_rescue_attempts.pop(cam_id, None)
            self._local_rescue_at.pop(cam_id, None)
            # Push the cleared state to HA immediately so the UI flips to "off"
            # without waiting for the next coordinator refresh tick.
            ls_entity = self._live_stream_entities.get(cam_id)
            if ls_entity is not None and getattr(ls_entity, "hass", None) is not None:
                try:
                    ls_entity.async_write_ha_state()
                except Exception as exc:  # pragma: no cover — defensive: HA state write
                    _LOGGER.debug(
                        "live-stream switch state write for %s skipped: %s",
                        cam_id[:8],
                        exc,
                    )
            # Step 2 — stop the NVR sidecar best-effort. Ordering: BEFORE
            # _stop_tls_proxy so ffmpeg gets a chance to flush MP4 cleanly,
            # but AFTER the visible state is corrected. Keep user-intent set
            # so the recorder auto-restarts when the LAN session comes back.
            # Concept §2.
            if cam_id in self._nvr_processes:
                try:
                    await self.stop_recorder(cam_id, clear_intent=False)
                except Exception as exc:
                    _LOGGER.warning(
                        "stop_recorder for %s raised %s during teardown — "
                        "switch state already cleared, continuing with proxy/stream cleanup",
                        cam_id[:8],
                        exc,
                    )
            await self._stop_tls_proxy(cam_id)
            await self._unregister_go2rtc_stream(cam_id)
            cam_entity = self._camera_entities.get(cam_id)
            if cam_entity is not None:
                stream = getattr(cam_entity, "stream", None)
                if stream is not None:
                    # Hard 5 s timeout: HA's Stream.stop() awaits the worker to
                    # exit, which never happens if the worker is stuck in an
                    # FFmpeg reconnect-loop against a dead URL (e.g. an expired
                    # REMOTE TLS-proxy port from a session that the relay has
                    # already capped). Without the timeout the entire teardown
                    # blocks the next stream-on for >5 min and pre-warm appears
                    # to "never start" (the stuck-warming watchdog clears it at
                    # 300 s). Setting `cam_entity.stream = None` synchronously
                    # afterwards is sufficient — HA's internal cleanup runs in
                    # a background task once the reference is dropped.
                    try:
                        await asyncio.wait_for(stream.stop(), timeout=5)
                    except TimeoutError:
                        _LOGGER.warning(
                            "camera.stream.stop() for %s timed out after 5s — "
                            "force-detaching, worker will be GC'd",
                            cam_id[:8],
                        )
                    except Exception as exc:
                        _LOGGER.debug(
                            "camera.stream.stop() for %s failed: %s", cam_id[:8], exc
                        )
                    cam_entity.stream = None

    def _schedule_stream_worker_error(self, cam_id: str, msg: str) -> None:
        """Thread-safe entry point from the log listener. Coalesces identical
        worker-error bursts and dispatches the async handler."""
        # Coalesce: skip if an unhandled dispatch for this cam is already
        # in flight. Prevents a flood of identical restart attempts when
        # HA's auto-restart loop fires 5-6 times per minute.
        pending = getattr(self, "_stream_worker_dispatch_pending", None)
        if pending is None:
            self._stream_worker_dispatch_pending = pending = set()
        if cam_id in pending:
            return
        pending.add(cam_id)
        self.hass.async_create_task(self._handle_stream_worker_error(cam_id, msg))

    async def _handle_stream_worker_error(self, cam_id: str, msg: str) -> None:
        """React to an HA stream-worker error for one camera.

        The primary failure mode this targets is the cycle reported in
        issue #6: the stream briefly becomes available (~2 s), FFmpeg fails,
        HA auto-restarts after a backoff, briefly becomes available again —
        forever. Each worker crash logs "Error from stream worker" exactly
        once, so our counter increments once per cycle.

        After `max_stream_errors` cycles we escalate: if the active connection
        is LOCAL we force a REMOTE restart (matches the watchdog's escalation
        path). If the active connection is already REMOTE there's no fallback
        left, so we just keep counting and let HA's internal backoff keep
        retrying — the error entries in the HA log are the diagnostic trail
        for any future debugging.

        Exception: HTTP 401 errors trigger the LOCAL rescue path *immediately*
        without waiting for the threshold. 401 is an unambiguous "Bosch
        rotated the session creds" signal — there is no value in burning
        4 additional retry cycles before re-issuing PUT /connection. Each
        retry just hits 401 again, and HA's stream component coalesces
        repeated identical errors so the counter may never reach the
        threshold (live bug 2026-05-27, Indoor Gen2: 4 errors, threshold 5,
        rescue never fired, frozen image until manual restart).
        """
        pending = getattr(self, "_stream_worker_dispatch_pending", None)
        try:
            self.record_stream_error(cam_id)
            cfg = self.get_model_config(cam_id)
            live = self._live_connections.get(cam_id, {})
            conn_type = live.get("_connection_type")

            # ── LOCAL rescue: HTTP 401 means Bosch rotated session creds ──
            # When the HLS consumer disconnects (browser tab closed) and HA
            # later reconnects, the camera has silently invalidated the
            # per-session digest creds and answers 401. The fix is not to
            # fall back to REMOTE — the LAN is fine — but to issue a fresh
            # PUT /connection LOCAL and use the new creds. We allow this
            # rescue once per failure burst (reset on stream success); if the
            # rescue itself fails or the next session also gets 401, the
            # counter blocks a second attempt and the normal REMOTE fallback
            # below takes over — preventing a re-issue loop on real LAN faults.
            is_auth_error = (
                "401" in msg or "Unauthorized" in msg or "authorization failed" in msg
            )

            # Threshold guard — but 401 bypasses it (see docstring).
            if (
                not is_auth_error
                and self._stream_error_count.get(cam_id, 0) < cfg.max_stream_errors
            ):
                return  # below threshold — let HA's auto-restart keep trying
            # Time-decay the rescue counter: rescues older than 5 min belong
            # to a previous failure burst. Without this the counter sticks at
            # 1 (record_stream_success never fires when no HLS consumer is
            # connected) and the next legitimate 401 burst — typically 8–14
            # min later when Bosch rotates again — skips straight to REMOTE.
            _LOCAL_RESCUE_TTL_SEC = 300
            now_mono = time.monotonic()
            last_rescue = self._local_rescue_at.get(cam_id, float("-inf"))
            if (
                last_rescue > float("-inf")
                and (now_mono - last_rescue) > _LOCAL_RESCUE_TTL_SEC
            ):
                self._local_rescue_attempts.pop(cam_id, None)
                self._local_rescue_at.pop(cam_id, None)
            if (
                conn_type == "LOCAL"
                and is_auth_error
                and self._local_rescue_attempts.get(cam_id, 0) < 1
            ):
                # Claim the rescue burst (one per cam at a time; decays after
                # _LOCAL_RESCUE_TTL_SEC). The burst itself retries internally.
                self._local_rescue_attempts[cam_id] = 1
                self._local_rescue_at[cam_id] = now_mono
                _LOGGER.warning(
                    "Stream worker auth-failed for %s on LOCAL — Bosch session "
                    "creds rotated; re-issuing fresh LOCAL session (LAN preserved)",
                    cam_id[:8],
                )
                # Reset error counter so try_live_connection picks LOCAL again
                # (it filters LOCAL out once the counter is saturated).
                self._stream_error_count[cam_id] = 0
                self._stream_fell_back.pop(cam_id, None)
                self._live_connections.pop(cam_id, None)
                # Resilient rescue: a fresh PUT /connection can briefly race the
                # camera's own session teardown — observed live 2026-05-31 (Indoor
                # Gen2): the new TLS proxy got "SSL UNEXPECTED_EOF" / "Connection
                # reset by peer" on the first re-issue while the camera was
                # mid-rotation. A SINGLE attempt then left go2rtc + HA Stream
                # pinned to the dead proxy port, so consumers saw "connection
                # refused" / "wrong user/pass" → frozen image until a manual
                # integration reload. Because the rescue tears the stream down,
                # no NEW stream-worker error fires to retrigger us — so the burst
                # must self-retry with backoff instead of relying on another
                # error to drive attempt 2.
                _LOCAL_RESCUE_MAX_ATTEMPTS = 3
                _LOCAL_RESCUE_RETRY_DELAY = 5
                result = None
                for rescue_try in range(1, _LOCAL_RESCUE_MAX_ATTEMPTS + 1):
                    # force_reset: stop-old-proxy + rebuild happen atomically
                    # under the stream lock (no external _stop_tls_proxy that
                    # could race a concurrent renewal — 2026-06-01).
                    result = await self.try_live_connection(cam_id, force_reset=True)
                    if result:
                        _LOGGER.info(
                            "LOCAL rescue: %s restarted as %s (attempt %d/%d)",
                            cam_id[:8],
                            result.get("_connection_type", "?"),
                            rescue_try,
                            _LOCAL_RESCUE_MAX_ATTEMPTS,
                        )
                        break
                    if rescue_try < _LOCAL_RESCUE_MAX_ATTEMPTS:
                        _LOGGER.warning(
                            "LOCAL rescue attempt %d/%d failed for %s — camera "
                            "transiently unreachable; retrying in %ds",
                            rescue_try,
                            _LOCAL_RESCUE_MAX_ATTEMPTS,
                            cam_id[:8],
                            _LOCAL_RESCUE_RETRY_DELAY,
                        )
                        await asyncio.sleep(_LOCAL_RESCUE_RETRY_DELAY)
                    else:
                        _LOGGER.warning(
                            "LOCAL rescue exhausted %d attempts for %s — leaving "
                            "to health watchdog / next failure burst",
                            _LOCAL_RESCUE_MAX_ATTEMPTS,
                            cam_id[:8],
                        )
                return  # whatever try_live_connection produced is the new state

            if conn_type != "LOCAL":
                # Already on REMOTE (or no live session) — nothing to escalate
                # to. Counter stays saturated so a future LOCAL attempt would
                # skip straight to REMOTE.
                _LOGGER.warning(
                    "Stream worker errors still occurring for %s on %s — "
                    "HA backoff continues, no further fallback available",
                    cam_id[:8],
                    conn_type or "(no session)",
                )
                return
            _LOGGER.warning(
                "Stream worker errors exceed threshold for %s on LOCAL — "
                "tearing down and retrying (REMOTE will be selected)",
                cam_id[:8],
            )
            # Mark fallback BEFORE the rebuild so try_live_connection picks
            # REMOTE. force_reset stops the dead LOCAL proxy + clears live state
            # INSIDE the stream lock — same atomic teardown as the 401 rescue, so
            # this escalation can't race a concurrent renewal either (2026-06-01).
            self._stream_fell_back[cam_id] = True
            result = await self.try_live_connection(cam_id, force_reset=True)
            if result:
                _LOGGER.info(
                    "Stream worker error recovery: %s restarted as %s",
                    cam_id[:8],
                    result.get("_connection_type", "?"),
                )
        finally:
            if pending is not None:
                pending.discard(cam_id)

    def _replace_renewal_task(
        self, cam_id: str, coro: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Cancel any existing renewal task for cam_id, then create and track the new one.

        Uses async_create_background_task: the keepalive coroutines run as
        `while True` loops that only return on stream-off. Tracked-task API
        (async_create_task) makes HA's startup-wait phase block on these
        loops, which never end — surfaces as a 5-minute "Something is
        blocking Home Assistant from wrapping up the start up phase" warning.
        """
        old = self._renewal_tasks.get(cam_id)
        if old and not old.done():
            old.cancel()
        task = self.hass.async_create_background_task(
            coro, f"bosch_shc_camera_renewal_{cam_id[:8]}"
        )
        self._renewal_tasks[cam_id] = task
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task  # type: ignore[no-any-return]

    def _replace_reaper_task(
        self, cam_id: str, coro: Coroutine[Any, Any, None]
    ) -> asyncio.Task[None]:
        """Cancel any existing idle reaper for cam_id, then create and track the new one.

        Mirrors `_replace_renewal_task`: the reaper is a `while True` loop that
        only returns on stream-off / teardown, so it must be a background task
        (otherwise HA's startup-wait phase blocks on it).
        """
        old = self._reaper_tasks.get(cam_id)
        if old and not old.done():
            old.cancel()
        task = self.hass.async_create_background_task(
            coro, f"bosch_shc_camera_reaper_{cam_id[:8]}"
        )
        self._reaper_tasks[cam_id] = task
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task  # type: ignore[no-any-return]

    async def _go2rtc_consumer_count(self, cam_id: str) -> int | None:
        """Best-effort count of active go2rtc consumers for this camera's stream.

        go2rtc tracks every reader (WebRTC, RTSP, MSE) of a registered stream
        in `consumers`. Returns the count, or None when go2rtc cannot be
        reached on any known port (HA-bundled 11984 / legacy 1984) — None means
        "unknown", which the idle reaper treats as "no confirmed consumer".
        """
        cam_entity = self._camera_entities.get(cam_id)
        if cam_entity is not None and cam_entity.entity_id:
            stream_name = cam_entity.entity_id
        else:
            stream_name = f"bosch_shc_cam_{cam_id.lower()}"
        for url in (
            "http://localhost:11984/api/streams",
            "http://localhost:1984/api/streams",
        ):
            try:
                async with asyncio.timeout(3):
                    async with aiohttp.ClientSession() as s:
                        async with s.get(url, params={"src": stream_name}) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError):
                continue
            consumers = data.get("consumers") if isinstance(data, dict) else None
            return len(consumers) if isinstance(consumers, list) else 0
        return None

    async def _has_active_consumer(self, cam_id: str) -> bool:
        """True if anything is actively consuming the live stream.

        Three signals, in cheap-to-expensive order:
          1. An active Mini-NVR recorder — it reads the TLS proxy DIRECTLY (not
             via HLS/go2rtc, see _nvr_processes), so it must be checked
             explicitly or the reaper would tear a recording's session down.
          2. A live HLS viewer — a playlist/segment was fetched within
             STREAM_HLS_FRESH_SEC (clients refetch every few seconds; tracked by
             cf_unbuffer). HA's `Stream.available` is deliberately NOT used: it
             means "can serve", not "is serving", and stays True for the whole
             session once HLS was ever requested — which pinned a long-abandoned
             session as "watched" and stopped the reaper from ever firing (live
             bug found 2026-06-03, HLS/mobile session never reaped).
          3. go2rtc reporting ≥1 consumer (WebRTC / RTSP / MSE).

        Used by the idle reaper to avoid tearing down a session that someone —
        a viewer or an automation — is still using.
        """
        from .cf_unbuffer import hls_access_age

        if cam_id in self._nvr_processes:
            return True
        cam_entity = self._camera_entities.get(cam_id)
        stream = getattr(cam_entity, "stream", None) if cam_entity is not None else None
        token = getattr(stream, "access_token", None) if stream is not None else None
        if token:
            age = hls_access_age(token)
            if age is not None and age < STREAM_HLS_FRESH_SEC:
                return True
        # An external recorder (Frigate/BlueIris) connected to the persistent
        # front-door is a real consumer the reaper must not tear down.
        if (
            self._frigate_runner is not None
            and self._frigate_runner.active_count(cam_id) > 0
        ):
            return True
        count = await self._go2rtc_consumer_count(cam_id)
        # None == go2rtc could not be reached on ANY known port (11984/1984) →
        # we CANNOT confirm the session is idle. Treating that "unknown" as
        # "no consumer" tore down LIVE viewers on any setup where go2rtc answers
        # on a different port — the WebRTC consumer is real but invisible to us,
        # so the reaper killed the stream every grace window (the user's "stream
        # just dies"). A lingering ghost while go2rtc is unreachable is far less
        # harmful than reaping an active live view, so unknown ⇒ keep alive.
        # Only a CONFIRMED count of 0 permits reaping. 2026-06-03 reaper fix.
        if count is None:
            return True
        return count > 0

    async def _idle_session_reaper(self, cam_id: str, generation: int) -> None:
        """Tear down a LOCAL session once nobody is consuming it.

        A live session — opened by a card view, a Cast, camera.play_stream,
        camera.record or a media-browser preview — keeps the camera's LOCAL
        RTSP session alive through the keepalive loop until the
        maxSessionDuration recycle (effectively forever). When the consumer
        goes away (browser tab closed / navigated away / Cast stopped) nothing
        ends it: the live-stream switch stays where it was and the camera stays
        occupied — the "ghost" session. This reaper polls every
        STREAM_IDLE_REAP_CHECK_SEC and, once there has been no consumer for
        STREAM_IDLE_REAP_SEC, runs the shared teardown so the camera drops its
        session (LED off) and the switch flips OFF.

        Reaping is driven purely by consumer presence, NOT by the switch state.
        Anything actually using the stream — a viewer (HLS/WebRTC) or an
        automation (Mini-NVR recording, Cast) — counts as a consumer
        (`_has_active_consumer`) and keeps the session alive, so automations
        that rely on the stream are unaffected. A switch that is ON but that
        nobody is watching is itself the ghost and gets reaped. Generation-
        tracked exactly like `_auto_renew_local_session`: an OFF→ON cycle or
        full renewal bumps the generation and this loop exits.
        """
        self._session_idle_since.pop(cam_id, None)
        try:
            while True:
                await asyncio.sleep(STREAM_IDLE_REAP_CHECK_SEC)
                if self._auto_renew_generation.get(cam_id, 0) != generation:
                    return  # OFF→ON / renewal started a newer session
                live = self._live_connections.get(cam_id)
                if not live or live.get("_connection_type") != "LOCAL":
                    return  # session gone or no longer LOCAL — nothing to reap
                if await self._has_active_consumer(cam_id):
                    if cam_id in self._session_idle_since:
                        _LOGGER.debug(
                            "Idle reaper: %s — consumer back, idle timer reset",
                            cam_id[:8],
                        )
                    self._session_idle_since.pop(cam_id, None)
                    continue
                now = time.monotonic()
                since = self._session_idle_since.get(cam_id)
                if since is None:
                    self._session_idle_since[cam_id] = now
                    _LOGGER.debug(
                        "Idle reaper: %s — no consumer, arming idle timer (%ds grace)",
                        cam_id[:8],
                        STREAM_IDLE_REAP_SEC,
                    )
                    continue
                _LOGGER.debug(
                    "Idle reaper: %s — still no consumer (%.0fs/%ds)",
                    cam_id[:8],
                    now - since,
                    STREAM_IDLE_REAP_SEC,
                )
                if now - since >= STREAM_IDLE_REAP_SEC:
                    _LOGGER.info(
                        "Idle reaper: %s — no stream consumer for %.0fs — "
                        "tearing down LOCAL session",
                        cam_id[:8],
                        now - since,
                    )
                    self._session_idle_since.pop(cam_id, None)
                    # Schedule teardown in its own task: _tear_down_live_stream
                    # cancels _reaper_tasks[cam_id] (i.e. THIS task), so awaiting
                    # it directly would deliver CancelledError mid-teardown. A
                    # fresh task runs teardown to completion; cancelling this
                    # (already-returning) reaper is then a no-op.
                    self.hass.async_create_task(
                        self._tear_down_live_stream(
                            cam_id, expected_generation=generation
                        ),
                        f"bosch_shc_camera_reap_teardown_{cam_id[:8]}",
                    )
                    return
        finally:
            self._session_idle_since.pop(cam_id, None)

    # ── Properties ────────────────────────────────────────────────────────────
    @property
    def token(self) -> str:
        # Prefer in-memory refreshed token over config entry (avoids stale reads)
        return getattr(self, "_refreshed_token", None) or self._entry.data.get(  # type: ignore[no-any-return]  # value is correct at runtime; HA/external source is Any-typed
            "bearer_token", ""
        )

    @property
    def refresh_token(self) -> str:
        return getattr(self, "_refreshed_refresh", None) or self._entry.data.get(  # type: ignore[no-any-return]  # value is correct at runtime; HA/external source is Any-typed
            "refresh_token", ""
        )

    @property
    def options(self) -> dict[str, Any]:
        # [S7] _options_snapshot is valid for this coordinator's lifetime:
        # _async_options_updated always calls async_reload on real option changes,
        # rebuilding the coordinator with fresh options. No stale-read risk.
        return self._options_snapshot

    # ── Token renewal ─────────────────────────────────────────────────────────
    def _token_still_valid(self, min_remaining: int = 60) -> bool:
        """Return True if the in-memory bearer token is valid for >= min_remaining seconds.

        Used to skip unnecessary refreshes when a concurrent caller already
        refreshed the token while we were waiting on the lock.
        """
        import base64 as _b64
        import json as _json
        import time as _time

        token = self.token
        if not token:
            return False
        try:
            parts = token.split(".")
            if len(parts) < 2:
                return False
            payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
            payload = _json.loads(_b64.urlsafe_b64decode(payload_b64))
            return bool((payload.get("exp", 0) - _time.time()) >= min_remaining)
        except (ValueError, TypeError):
            # JWT payload was not base64-decodable or not JSON — treat as expired.
            return False

    async def _ensure_valid_token(self, observed_token: str | None = None) -> str:
        """
        Return a valid bearer token.
        Called ONLY when we get a 401 — not on every tick.
        Refreshes via refresh_token with retry logic:
          - Serialized via self._token_refresh_lock so two concurrent
            callers never race on the same refresh_token (Keycloak
            rotates and invalidates the previous token on success —
            the loser of the race would get invalid_grant forever).
          - Skip-if-already-refreshed: after acquiring the lock, compare
            against `observed_token` (the token the caller saw fail);
            if it no longer matches `self.token`, another caller already
            refreshed it while we waited — reuse that instead of a redundant
            POST. Callers should always pass the token they observed to be
            bad. Bosch can reject a token (rotate/early-invalidate it) well
            before its own JWT `exp` claim says so — trusting `exp` alone
            (as this used to) meant a token rejected for any other reason
            was never actually refreshed: it still "looked" valid, so every
            retry just resent the same dead token forever (2026-07-06,
            SebastianHarder community report — a manual-login token was
            rejected immediately, and the integration never recovered).
          - Always re-read the freshest refresh_token from the
            config entry under the lock so we never send a stale
            token that was already rotated and persisted by the
            previous caller.
          - 3 attempts with 2s delay between retries
          - Persists new refresh token to config entry data (non-reloading)
          - Only alerts after 3 consecutive complete failures
        """
        async with self._token_refresh_lock:
            return await self._refresh_token_locked(observed_token)

    async def _refresh_token_locked(self, observed_token: str | None = None) -> str:
        from .config_flow import (
            AuthServerOutageError,
            RefreshTokenInvalidError,
            _do_refresh,
        )

        # Another caller may have just refreshed the token while we were
        # waiting on the lock — if so, skip the POST entirely. Prefer
        # comparing against the token the caller actually observed to be
        # bad (authoritative: a 401 already proves it); only fall back to
        # the JWT-expiry heuristic when no observed token was given.
        if observed_token is not None:
            if self.token != observed_token:
                return self.token
        elif self._token_still_valid(min_remaining=60):
            return self.token
        # If we're in a Bosch auth-server outage, skip the POST entirely
        # until the back-off gate opens — avoids hammering a server that
        # is already known to be down.
        import time as _time

        now_m = _time.monotonic()
        if self._auth_outage_count > 0 and now_m < self._auth_outage_next_retry_ts:
            remaining = int(self._auth_outage_next_retry_ts - now_m)
            raise UpdateFailed(
                f"Bosch auth server outage — next retry in {remaining}s "
                f"(outage count: {self._auth_outage_count})"
            )
        # Always prefer the freshest refresh_token from the config entry
        # (persisted by previous successful refresh) over our in-memory
        # copy, which could be stale in edge cases (e.g. entry reload).
        refresh = (
            self._entry.data.get("refresh_token", "")
            or getattr(self, "_refreshed_refresh", None)
            or ""
        )
        if not refresh:
            # No refresh token at all — trigger the built-in HA reauth button
            # (shows "Reconfigure" on the integration card, runs our auto-login).
            raise ConfigEntryAuthFailed("No refresh token — re-authentication required")
        session = await async_get_bosch_cloud_session(self.hass)
        # Retry up to 3 times with 2s delay on TRANSIENT errors only.
        # Hard auth errors (invalid_grant) raise RefreshTokenInvalidError
        # which we convert to ConfigEntryAuthFailed immediately — retrying
        # a rejected refresh token is pointless and just extends the user's
        # broken state.
        # Server outage (5xx) raises AuthServerOutageError — we back off
        # and retry later without triggering reauth (nothing for the user to fix).
        tokens = None
        try:
            for attempt in range(3):
                tokens = await _do_refresh(session, refresh)
                if tokens:
                    break
                if attempt < 2:
                    _LOGGER.debug(
                        "Token refresh attempt %d failed (transient), retrying in 2s...",
                        attempt + 1,
                    )
                    await asyncio.sleep(2)
        except RefreshTokenInvalidError:
            # Do not log the exception body — Keycloak error responses can echo
            # token material back in the payload.
            _LOGGER.error(
                "Refresh token rejected by Keycloak (invalid_grant) — triggering reauth flow"
            )
            raise ConfigEntryAuthFailed(
                "Refresh token invalid — please re-authenticate via the "
                "Reconfigure button on the integration card."
            ) from None
        except AuthServerOutageError as err:
            self._auth_outage_count += 1
            # Exponential back-off: 60s, 120s, 240s, 480s, capped at 600s (10 min)
            backoff = min(60 * (2 ** (self._auth_outage_count - 1)), 600)
            self._auth_outage_next_retry_ts = now_m + backoff
            _LOGGER.warning(
                "Bosch Keycloak auth server outage (%s) — NOT triggering reauth "
                "(server-side problem, refresh token is probably still valid). "
                "Backing off %ds before next attempt (outage #%d).",
                err,
                backoff,
                self._auth_outage_count,
            )
            # One-time repair issue after 3 consecutive outages so the user
            # sees a clear explanation in Settings → Repairs while entities
            # are unavailable. Quality-Scale Gold rule `repair-issues`.
            if self._auth_outage_count >= 3 and not self._auth_outage_alert_sent:
                self._auth_outage_alert_sent = True
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    "auth_server_outage",
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="auth_server_outage",
                    translation_placeholders={"error": str(err)},
                )
            raise UpdateFailed(
                f"Bosch auth server outage — will retry in {backoff}s"
            ) from err
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
                _LOGGER.info(
                    "Token refresh recovered after %d failures", self._token_fail_count
                )
            self._token_fail_count = 0
            if self._token_alert_sent:
                self._token_alert_sent = False
                ir.async_delete_issue(self.hass, DOMAIN, "token_expired")
            # Clear auth-server outage state + dismiss the outage notification
            if self._auth_outage_count > 0:
                _LOGGER.info(
                    "Bosch auth server recovered after %d outage cycles",
                    self._auth_outage_count,
                )
                self._auth_outage_count = 0
                self._auth_outage_next_retry_ts = float("-inf")
                if self._auth_outage_alert_sent:
                    self._auth_outage_alert_sent = False
                    ir.async_delete_issue(self.hass, DOMAIN, "auth_server_outage")
            return self._refreshed_token  # type: ignore[no-any-return]
        self._token_fail_count += 1
        _LOGGER.warning(
            "Silent token renewal failed (attempt %d)", self._token_fail_count
        )
        # After 3 consecutive complete failures the refresh token is very
        # likely invalidated on Keycloak's side (invalid_grant). Trigger the
        # built-in HA reauth flow — a "Reconfigure" button appears on the
        # integration card, which runs the same auto-login flow and updates
        # the existing entry in place (keeps options, entities, automations).
        if self._token_fail_count >= 3:
            raise ConfigEntryAuthFailed(
                "Token refresh failed repeatedly — please re-authenticate via "
                "the Reconfigure button on the integration card."
            )
        raise UpdateFailed("Token refresh failed — will retry")

    async def _async_token_failure_alert(self, message: str) -> None:
        """Send a one-time alert when token refresh fails (repair issue + notify)."""
        if self._token_alert_sent:
            return
        self._token_alert_sent = True
        title = "⚠️ Bosch Kamera — Token abgelaufen"
        # Repair issue — stays visible under Settings → Repairs until resolved.
        # Quality-Scale Gold rule `repair-issues` (replaces persistent_notification).
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            "token_expired",
            is_fixable=False,
            severity=ir.IssueSeverity.CRITICAL,
            translation_key="token_expired",
            translation_placeholders={"message": message},
        )
        # Notify service (Signal, mobile_app, etc.) — uses system services
        for svc in self._get_alert_services("system"):
            domain, _, name = svc.partition(".")
            if self.hass.services.has_service(domain, name):
                try:
                    await self.hass.services.async_call(
                        domain,
                        name,
                        {"message": message, "title": title},
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
                remaining,
                refresh_in,
            )
            # Cancel any previously scheduled handle so reloads/reschedules
            # don't stack multiple timers that all fire the same refresh.
            prev = self._token_refresh_handle
            if prev is not None:
                try:
                    prev.cancel()
                except (AttributeError, RuntimeError) as err:
                    _LOGGER.debug(
                        "Could not cancel prior token-refresh handle: %s", err
                    )

            def _schedule_proactive_refresh() -> None:
                if self.hass.is_stopping:
                    return
                t = self.hass.async_create_task(self._proactive_refresh())
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)

            self._token_refresh_handle = self.hass.loop.call_later(
                refresh_in,
                _schedule_proactive_refresh,
            )
        except Exception as err:
            _LOGGER.debug("_schedule_token_refresh: cannot parse token expiry: %s", err)

    async def _proactive_refresh(self) -> None:
        """Background task: refresh the token before it expires."""
        if self.hass.is_stopping:
            return
        _LOGGER.debug("Proactive token refresh triggered")
        try:
            # Pass the current token as `observed_token` so the JWT-expiry
            # fallback (which would always say "still valid" here — this
            # fires 5 minutes BEFORE expiry by design) can't skip the
            # refresh; only a concurrent caller who already refreshed since
            # this timer fired should short-circuit it.
            await self._ensure_valid_token(self.token)
            # _ensure_valid_token calls _schedule_token_refresh on success,
            # so the next refresh is automatically rescheduled.
        except Exception as err:
            _LOGGER.warning(
                "Proactive token refresh failed: %s — will retry via reactive 401 handling",
                err,
            )

    # ── Local health check ────────────────────────────────────────────────────
    # Grace period after a local RCP write during which LAN-ping failures are
    # treated as still-reachable: the camera rotates Digest creds + tears down
    # its cloud TLS session after each write, and the LAN HTTPS endpoint is
    # briefly unresponsive (~5–15 s observed). 30 s leaves margin without
    # masking a real network outage.
    _LOCAL_WRITE_GRACE_S: float = 30.0

    def _in_local_write_grace(self, cam_id: str, now: float | None = None) -> bool:
        """True if this cam was written to via local RCP within _LOCAL_WRITE_GRACE_S."""
        moment = now if now is not None else time.monotonic()
        last = self._local_write_at.get(cam_id, float("-inf"))
        return (moment - last) < self._LOCAL_WRITE_GRACE_S

    def is_lan_reachable(self, cam_id: str) -> bool | None:
        """Most recent LAN-TCP reachability for `cam_id`, or None if unknown.

        Honors `_local_write_at` grace period — during the post-write window
        we report the last *positive* reachability (or True if none recorded)
        so the UI does not flip to offline for a few seconds after every
        privacy/light toggle.
        """
        entry = self._lan_tcp_reachable.get(cam_id)
        if entry is None:
            return True if self._in_local_write_grace(cam_id) else None
        reachable, _ts = entry
        if not reachable and self._in_local_write_grace(cam_id):
            return True
        return reachable

    def is_updating(self, cam_id: str) -> bool:
        """True while firmware install is in progress for `cam_id`.

        Reads `_firmware_cache[cam_id]['updating']` populated by the slow-tier
        firmware poll. The camera reboots during the install (typically 3–7 min)
        so dependent entities should flip to unavailable for that window. The
        camera-status sensor surfaces the same state as the enum value
        ``"updating"``.
        """
        return bool(self._firmware_cache.get(cam_id, {}).get("updating", False))

    async def _async_local_tcp_ping(self, cam_id: str, timeout: float = 1.5) -> bool:
        """Quick TCP connect to camera port 443 on LAN — returns True if reachable.

        Tries _rcp_lan_ip_cache first, falls back to _local_creds_cache.
        Result is written to _lan_tcp_reachable for stream pre-check reuse.
        Much faster than cloud /commissioned check (~5ms vs ~200ms).
        """
        cam_ip = self._get_cam_lan_ip(cam_id)
        if not cam_ip:
            return False  # no known LAN IP — can't ping locally
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
        self._lan_tcp_reachable[cam_id] = (result, time.monotonic())
        return result

    async def _async_outage_ping_all(self) -> None:
        """Ping every known camera concurrently during a cloud outage.

        Called from the UpdateFailed paths in `_async_update_data`. Throttled
        to once per 30 s so a flapping cloud does not hammer the LAN. Result
        feeds `_lan_tcp_reachable`, which the switch/light entity
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
        for cid in self._rcp_lan_ip_cache:
            if cid not in cam_ids:
                cam_ids.append(cid)
        if not cam_ids:
            return
        results = await asyncio.gather(
            *(self._async_local_tcp_ping(cid) for cid in cam_ids),
            return_exceptions=True,
        )
        _ok = sum(1 for r in results if r is True)
        _LOGGER.info(
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

    def _get_cam_lan_ip(self, cam_id: str) -> str | None:
        """Return the best known LAN IP for a camera, or None if not yet discovered."""
        ip = self._rcp_lan_ip_cache.get(cam_id)
        if ip:
            return ip
        creds = self._local_creds_cache.get(cam_id)
        return creds.get("host") if creds else None

    def _should_check_status(
        self, cam_id: str, now: float, interval_status: int
    ) -> bool:
        """Determine if this camera needs a status check this tick.

        - Normal cameras: check every interval_status seconds.
        - Persistently offline cameras (>15 min): check every _OFFLINE_EXTENDED_INTERVAL.

        Uses per-camera timestamps (_per_cam_status_at) instead of the global
        _last_status so that the check interval is independent of scan_interval.
        With _last_status, setting scan_interval < interval_status caused _last_status
        to advance every tick, making (now - _last_status) always < interval_status
        and status checks never firing after the first tick.
        """
        per_cam_last = self._per_cam_status_at.get(cam_id, float("-inf"))
        offline_since = self._offline_since.get(cam_id)
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
        token = self.token
        if not token and not self.refresh_token:
            raise UpdateFailed("Not authenticated — re-add the integration to log in")

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
        with self._fcm_lock:
            _fcm_healthy = self._fcm_healthy
        if opts.get("enable_fcm_push", False):
            sup = getattr(self, "_fcm_supervisor_task", None)
            if sup is None or sup.done():
                self.hass.async_create_task(_fcm_async_ensure_supervisor(self))
        if _fcm_healthy:
            event_interval = int(opts.get("interval_events", 300))
        else:
            # FCM is not delivering (disabled or flagged unhealthy): the poll IS
            # the detection path now, so it must run faster than the motion
            # window or polled events age out before the binary sensor can see
            # them (issue #36). Cap at FCM_DOWN_EVENT_POLL_SEC; honour a user's
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
        # getattr handles stub coordinators in tests that predate the
        # diagnostic cloud_api_override field (real instances always set
        # self._cloud_api in __init__).
        cloud_api = getattr(self, "_cloud_api", CLOUD_API)

        try:
            # ── 1. List cameras (every tick — lightweight, needed for entity list) ──
            async with asyncio.timeout(15):
                async with session.get(
                    f"{cloud_api}/v11/video_inputs", headers=headers
                ) as resp:
                    if resp.status == 401:
                        _LOGGER.info("Token expired (401) — attempting silent renewal")
                        _LOGGER.debug(
                            "video_inputs 401 body against %s (diagnostic, no token "
                            "material): %s",
                            cloud_api,
                            (await resp.text())[:300],
                        )
                        token = await self._ensure_valid_token(token)
                        headers = {
                            "Authorization": f"Bearer {token}",
                            "Accept": "application/json",
                        }
                    elif resp.status != 200:
                        # Cloud 5xx often coincides with announced maintenance —
                        # kick off a background fetch of the community RSS so
                        # the UI can show a specific reason instead of generic
                        # "unavailable". Rate-limited inside the helper.
                        # getattr handles stub coordinators in tests.
                        _maint = getattr(self, "_async_refresh_maintenance", None)
                        if _maint is not None:
                            self.hass.async_create_task(_maint(reactive=True))
                        # And kick a LAN-ping sweep so the switch/light entities
                        # have a fresh reachability signal even though the
                        # cloud-driven status loop won't run this tick.
                        _outage_ping = getattr(self, "_async_outage_ping_all", None)
                        if _outage_ping is not None:
                            self.hass.async_create_task(_outage_ping())
                        raise UpdateFailed(f"Camera list returned HTTP {resp.status}")
                    else:
                        cam_list = await resp.json()

            # Retry after renewal if we got a 401
            if resp.status == 401:
                async with asyncio.timeout(15):
                    async with session.get(
                        f"{cloud_api}/v11/video_inputs", headers=headers
                    ) as resp2:
                        if resp2.status == 401:
                            import json as _json

                            body_text = (await resp2.text())[:300]
                            _LOGGER.debug(
                                "video_inputs retry still 401 after renewal against "
                                "%s — Bosch response body (diagnostic, no token "
                                "material): %s",
                                cloud_api,
                                body_text,
                            )
                            try:
                                body_json = _json.loads(body_text)
                            except ValueError:
                                body_json = {}
                            # A fresh, successfully-renewed token still being 401'd
                            # is not a token problem at all — Bosch is telling us
                            # the account itself lacks camera-API access (e.g. a
                            # shared-user registration that never completed).
                            # Re-authenticating cannot fix this; say so instead of
                            # sending the user in an endless, pointless relogin loop
                            # (2026-07-06 SebastianHarder community report — debug
                            # logging above finally surfaced the real reason).
                            if body_json.get("error") == "sh:authorization.failed":
                                # The official Bosch Camera App performs a separate,
                                # one-time "registration/check" step against the camera
                                # backend after SingleKey ID login (name/marketing-consent/
                                # T&C acceptance, distinct from login itself) — an account
                                # that never went through that screen (e.g. reached camera
                                # access via a beta/invite path rather than the normal
                                # in-app first run) gets a permanently valid login but a
                                # permanently rejected camera-API token. Re-authenticating
                                # only repeats the login step, so it cannot fix this.
                                raise UpdateFailed(
                                    "Bosch rejected the camera API with "
                                    f"'sh:authorization.failed' ({body_json.get('message', 'no detail')}) "
                                    "— this is an account/permission issue on Bosch's side, not a "
                                    "login problem. Re-authenticating will not fix it. Open the "
                                    "official Bosch Smart Camera App and complete any registration "
                                    "or terms-of-service screen it shows on next login — this "
                                    "integration only logs in and does not perform that separate "
                                    "camera-account registration step. If no such screen appears, "
                                    "contact Bosch support."
                                )
                            raise UpdateFailed(
                                "Token expired and renewal failed — go to Settings → Integrations → "
                                "Bosch Smart Home Camera → Configure → Force new browser login"
                            )
                        if resp2.status != 200:
                            raise UpdateFailed(
                                f"Camera list returned HTTP {resp2.status}"
                            )
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
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, aiohttp.ClientError) as err:
                    _LOGGER.debug("Feature flags fetch failed: %s", err)

            # ── Protocol version check (once at startup) ──────────────────
            if not self._protocol_checked:
                self._protocol_checked = True
                try:
                    _version = self._integration_version
                    async with asyncio.timeout(5):
                        async with session.get(
                            f"{CLOUD_API}/protocol_support?protocol=11&client=haV{_version}",
                            headers=headers,
                        ) as proto_resp:
                            if proto_resp.status == 200:
                                proto_data = await proto_resp.json()
                                if proto_data.get("state") != "SUPPORTED":
                                    _LOGGER.warning(
                                        "Bosch API protocol version 11 may no longer be supported "
                                        "(state=%s) — consider updating the integration",
                                        proto_data.get("state"),
                                    )
                                else:
                                    _LOGGER.debug(
                                        "Protocol v11 supported: %s", proto_data
                                    )
                            else:
                                _LOGGER.warning(
                                    "Bosch API protocol version check returned HTTP %s "
                                    "— consider updating the integration",
                                    proto_resp.status,
                                )
                except Exception as exc:
                    _LOGGER.debug("Protocol version check failed: %s", exc)

            data: dict[str, Any] = {}

            # ── Build camera ID list ─────────────────────────────────────────
            cam_ids: list[str] = []
            cam_by_id: dict[str, dict[str, Any]] = {}
            for cam in cam_list:
                cid = cam.get("id", "")
                if cid:
                    cam_ids.append(cid)
                    cam_by_id[cid] = cam
                    # Cache hardware version for model-specific behavior
                    self._hw_version[cid] = cam.get("hardwareVersion", "CAMERA")

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
                    _LOGGER.debug(
                        "Local TCP ping OK for %s — ONLINE (cloud check skipped)",
                        cam_id[:8],
                    )
                    # Active LOCAL promotion: if this cam is currently pinned to
                    # REMOTE because of a past LAN-fail burst (auto-mode
                    # fallback) and the LAN is reachable again, clear the
                    # error counter + fallback flag so the next stream-on
                    # gets to attempt LOCAL again. If a stream is *currently*
                    # running on REMOTE-fallback we additionally schedule a
                    # background `try_live_connection(is_renewal=True)` —
                    # the inner already runs `Stream.update_source()` after
                    # pre-warm, so the live HLS session swaps from Cloud to
                    # LAN with a brief (~2-3 s) re-buffer instead of
                    # waiting for the user to re-toggle. Cooldown 5 min
                    # prevents ping-pong if LAN flaps in/out.
                    if self._stream_fell_back.get(cam_id):
                        # Read options fresh here: this branch only runs for a
                        # camera that has fallen back to REMOTE (rare), so the
                        # cost is negligible, and reading `self._entry.options`
                        # directly avoids coupling to the enclosing-scope `opts`
                        # snapshot (which a caller/test may set independently of
                        # the entry). Reverted a micro-opt that broke that
                        # contract. 2026-06-18.
                        _check_opts = get_options(self._entry)
                        if _check_opts.get("stream_connection_type", "local") == "auto":
                            err_count_was = self._stream_error_count.get(cam_id, 0)
                            _LOGGER.info(
                                "AUTO mode: %s LAN reachable again — clearing "
                                "REMOTE fallback flag (counter=%d)",
                                cam_id[:8],
                                err_count_was,
                            )
                            self._stream_error_count.pop(cam_id, None)
                            self._stream_error_at.pop(cam_id, None)
                            self._stream_fell_back.pop(cam_id, None)
                            # Active promotion path: only when a stream is
                            # actively running on REMOTE-fallback.
                            live = self._live_connections.get(cam_id, {})
                            if live.get("_connection_type") == "REMOTE":
                                last_promote = self._local_promote_at.get(
                                    cam_id, float("-inf")
                                )
                                _LOCAL_PROMOTE_COOLDOWN_S = 300
                                if (now - last_promote) > _LOCAL_PROMOTE_COOLDOWN_S:
                                    self._local_promote_at[cam_id] = now
                                    _LOGGER.info(
                                        "AUTO mode: %s active REMOTE stream — "
                                        "attempting live LOCAL promotion via renewal",
                                        cam_id[:8],
                                    )
                                    self.hass.async_create_task(
                                        self._promote_to_local(cam_id)
                                    )
                    return (cam_id, "ONLINE")

                # Cloud path: /ping (primary, 8 bytes) + /commissioned (fallback)
                status = "UNKNOWN"
                ping_ok = False
                try:
                    async with asyncio.timeout(5):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/ping",
                            headers=headers,
                        ) as pr:
                            if pr.status == 200:
                                ping_result = (await pr.text()).strip().strip('"')
                                # Map firmware update statuses to UPDATING
                                if ping_result.startswith("UPDATING"):
                                    status = "UPDATING"
                                else:
                                    status = ping_result  # "ONLINE" or "OFFLINE"
                                ping_ok = True
                            elif pr.status == 444:
                                _LOGGER.warning(
                                    "Bosch session-quota hit for %s — too many simultaneous"
                                    " live sessions across all clients (HA, Bosch App, ioBroker, etc.)."
                                    " Close unused sessions to recover.",
                                    cam_id[:8],
                                )
                                status = "SESSION_LIMIT"
                                ping_ok = True
                except Exception as err:
                    _LOGGER.debug("Ping check error for %s: %s", cam_id, err)
                if not ping_ok:
                    try:
                        async with asyncio.timeout(8):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id}/commissioned",
                                headers=headers,
                            ) as r:
                                if r.status == 200:
                                    comm = await r.json()
                                    self._commissioned_cache[cam_id] = comm
                                    if comm.get("connected") and comm.get(
                                        "commissioned"
                                    ):
                                        status = "ONLINE"
                                    elif comm.get("configured"):
                                        status = "OFFLINE"
                                elif r.status == 444:
                                    _LOGGER.warning(
                                        "Bosch session-quota hit for %s (commissioned fallback)"
                                        " — too many simultaneous live sessions across all clients.",
                                        cam_id[:8],
                                    )
                                    status = "SESSION_LIMIT"
                    except Exception as err:
                        _LOGGER.debug(
                            "Commissioned fallback error for %s: %s", cam_id, err
                        )

                self._per_cam_status_at[cam_id] = now
                # Track offline duration for extended interval.
                # SESSION_LIMIT is NOT a connectivity failure — do not add to _offline_since
                # so the camera does not get penalised with extended check intervals.
                if status in ("OFFLINE", "UPDATING"):
                    if cam_id not in self._offline_since:
                        self._offline_since[cam_id] = now
                else:
                    self._offline_since.pop(cam_id, None)
                # Fire persistent notification if 444 hits accumulate
                if status == "SESSION_LIMIT":
                    _handle_quota = getattr(
                        self, "_async_handle_session_quota_hit", None
                    )
                    if _handle_quota is not None:
                        self.hass.async_create_task(_handle_quota(cam_id))
                return (cam_id, status)

            # Run all status checks in parallel
            status_results = await asyncio.gather(
                *[_check_status(cid) for cid in cam_ids],
                return_exceptions=True,
            )
            any_status_checked = False
            for result in status_results:
                if isinstance(result, BaseException):
                    continue
                cid, status = result
                self._cached_status[cid] = status
                # Mark as checked unconditionally — _per_cam_status_at[cid] was
                # just updated inside _check_status, so re-calling _should_check_status
                # would always return False for extended-offline cams (their per-cam
                # timestamp was just set to `now`). That caused _last_status to stall
                # forever when all cameras were persistently offline, making do_status=True
                # on every subsequent tick even though no real API calls ran.
                any_status_checked = True

            # ── 3. Events — parallel across all cameras ──────────────────────
            async def _fetch_events(cam_id: str) -> tuple[str, list[Any], bool]:
                """Fetch events for single camera.

                Returns ``(cam_id, events, ok)``. ``ok`` is True only when the
                cloud gave a definitive answer (last_event matched the cached id,
                or the full events list came back 200). On a transient failure
                ``ok`` is False so the caller keeps the previously-cached events
                instead of blanking them, and does not advance ``_last_events``
                (which would defer the next retry by a full poll interval — up to
                300 s while FCM is healthy). Mirrors the cross-version ioBroker fix.
                """
                events: list[Any] = []
                skip_full_fetch = False
                ok = False
                try:
                    async with asyncio.timeout(5):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/last_event",
                            headers=headers,
                        ) as le_resp:
                            if le_resp.status == 200:
                                last_ev = await le_resp.json()
                                last_ev_id = last_ev.get("id", "")
                                if (
                                    last_ev_id
                                    and last_ev_id == self._last_event_ids.get(cam_id)
                                ):
                                    skip_full_fetch = True
                                    ok = True
                                    events = self._cached_events.get(cam_id, [])
                                    _LOGGER.debug(
                                        "last_event unchanged for %s (id=%s) — skipping full fetch",
                                        cam_id,
                                        last_ev_id[:8],
                                    )
                except Exception as err:
                    _LOGGER.debug(
                        "last_event check error for %s: %s — falling back to full fetch",
                        cam_id,
                        err,
                    )

                if not skip_full_fetch:
                    try:
                        url = f"{CLOUD_API}/v11/events?videoInputId={cam_id}&limit=20"
                        async with asyncio.timeout(15):
                            async with session.get(url, headers=headers) as r:
                                if r.status == 200:
                                    events = await r.json()
                                    ok = True
                    except Exception as err:
                        _LOGGER.debug(
                            "Events fetch error for %s: %s",
                            cam_id,
                            BoschCameraCoordinator._err_str(err),
                        )
                return (cam_id, events, ok)

            any_events_fetched = False
            if do_events:
                # Run all event fetches in parallel
                event_results = await asyncio.gather(
                    *[_fetch_events(cid) for cid in cam_ids],
                    return_exceptions=True,
                )
                for ev_result in event_results:
                    if isinstance(ev_result, BaseException):
                        continue
                    cid, ev_list, ev_ok = ev_result
                    # Only overwrite the cache on a definitive fetch — a transient
                    # failure must not blank a camera's events (and its
                    # events-today count) until the next successful poll.
                    if ev_ok:
                        self._cached_events[cid] = ev_list
                        any_events_fetched = True

            # ── Build data dict + process new events (must be sequential) ─────
            for cam_id in cam_ids:
                cam = cam_by_id[cam_id]
                status = self._cached_status.get(cam_id, "UNKNOWN")
                events = self._cached_events.get(cam_id, [])

                if do_events and events:
                    newest_id = events[0].get("id", "")
                    prev_id = self._last_event_ids.get(cam_id)
                    if prev_id is None:
                        unread_ids = [
                            ev.get("id")
                            for ev in events
                            if ev.get("id") and not ev.get("isRead", False)
                        ]
                        if unread_ids and self.options.get("mark_events_read", False):
                            _LOGGER.debug(
                                "Startup: marking %d unread event(s) as read for %s",
                                len(unread_ids),
                                cam_id,
                            )
                            try:
                                await self.async_mark_events_read(unread_ids)
                            except asyncio.CancelledError:
                                raise
                            except Exception as err:
                                _LOGGER.debug(
                                    "Mark-read (startup) failed for %s: %s", cam_id, err
                                )
                        # Bootstrap _last_event_ids so the next polling tick can
                        # detect newer events. Without this seed, prev_id stays
                        # None forever in polling-only mode (no FCM) — every
                        # tick re-enters this branch, alert chain (`elif newest_id
                        # and newest_id != prev_id`) is never reached, and
                        # automations on `bosch_shc_camera_motion` never fire
                        # after a restart. (Forum: geotie 2026 — "Automation
                        # funktioniert, wird aber oft nicht ausgelöst".)
                        if newest_id:
                            self._last_event_ids[cam_id] = newest_id
                    elif newest_id and newest_id != prev_id:
                        # Per-event-ID dedup shared with fcm.async_handle_fcm_push.
                        # Guards against a polling tick firing an alert that the
                        # FCM handler already dispatched for the same event ID.
                        _now_mono = time.monotonic()
                        # Delivery-death detection (issue #36): this poll found a
                        # genuinely new event. If FCM push is enabled+running+
                        # "healthy" yet no real push has arrived in the last
                        # FCM_DELIVERY_DEAD_AFTER_SEC, push delivery is dead at the
                        # cloud/Google layer even though the socket reports
                        # is_started()=True — the poll just proved push missed a
                        # real event. Flag unhealthy + force a HARD heal (purge +
                        # fresh registration, which re-POSTs to Bosch /v11/devices).
                        # A push arriving just before this poll keeps _fcm_last_push
                        # recent → no false positive.
                        with self._fcm_lock:
                            _last_push = getattr(self, "_fcm_last_push", float("-inf"))
                            _started_at = getattr(
                                self, "_fcm_started_at", float("-inf")
                            )
                            # Grace reference: the later of "last real push" and
                            # "listener start". A still-warming-up listener (no
                            # push yet, started <window ago) is never condemned;
                            # a dead-from-start registration IS caught once the
                            # listener has been up for the window with no push.
                            _ref = max(_last_push, _started_at)
                            _push_age = _now_mono - _ref
                            if (
                                self.options.get("enable_fcm_push", False)
                                and getattr(self, "_fcm_running", False)
                                and getattr(self, "_fcm_healthy", False)
                                and _ref != float("-inf")
                                and _push_age > FCM_DELIVERY_DEAD_AFTER_SEC
                            ):
                                self._fcm_healthy = False
                                self._fcm_force_hard_heal = True
                                _ago = (
                                    "never"
                                    if _last_push == float("-inf")
                                    else f"{_push_age / 60.0:.0f} min ago"
                                )
                                _LOGGER.warning(
                                    "FCM delivery watchdog: polling found a new event "
                                    "(%s) that push never delivered (last push %s) — "
                                    "delivery is dead despite a live socket; forcing "
                                    "fresh registration with Bosch CBS",
                                    newest_id,
                                    _ago,
                                )
                        _dedup_skip = (
                            self._alert_sent_ids.get(newest_id, float("-inf"))
                            > _now_mono - 60.0
                        )
                        self._last_event_ids[cam_id] = newest_id
                        if _dedup_skip:
                            _LOGGER.debug(
                                "Polling dedup: skipping duplicate alert for %s id=%s",
                                cam_id,
                                newest_id,
                            )
                        else:
                            self._alert_sent_ids[newest_id] = _now_mono
                            # Bound memory: the FCM handler prunes this dedup map
                            # too, but it never runs when FCM is disabled, so the
                            # polling path must prune here as well — otherwise it
                            # grows one entry per event forever. Drop entries older
                            # than 2× the 60s dedup window.
                            if len(self._alert_sent_ids) > 64:
                                # Mutate in place — a dict-comprehension rebind
                                # (self._alert_sent_ids = {...}) would detach
                                # any alias another concurrent call already
                                # holds. fcm.py's async_handle_fcm_push does
                                # exactly that: it captures `_sent =
                                # coordinator._alert_sent_ids` once, then
                                # writes to it later after an await. If this
                                # rebind ran in between, that later write would
                                # land in the orphaned old dict — invisible to
                                # any later reader of self._alert_sent_ids —
                                # allowing a duplicate alert for the same event
                                # (bug-hunt 2026-07-03).
                                _cutoff = _now_mono - 120.0
                                for _k in [
                                    k
                                    for k, v in self._alert_sent_ids.items()
                                    if v < _cutoff
                                ]:
                                    del self._alert_sent_ids[_k]
                            _LOGGER.debug(
                                "New event detected for %s (id=%s) — triggering snapshot refresh",
                                cam_id,
                                newest_id,
                            )
                            cam_entity = self._camera_entities.get(cam_id)
                            if cam_entity:
                                self.hass.async_create_task(
                                    cam_entity._async_trigger_image_refresh(delay=2)
                                )
                            newest_event = events[0]
                            event_type = newest_event.get("eventType", "")
                            event_tags = newest_event.get("eventTags", []) or []
                            # Gen2 DualRadar fires eventType=MOVEMENT w/ eventTags=["PERSON"]
                            # when a human is detected — the tag is more specific, so upgrade.
                            if "PERSON" in event_tags and event_type == "MOVEMENT":
                                event_type = "PERSON"
                            cam_name = cam.get("title", cam_id)
                            event_payload = {
                                "camera_id": cam_id,
                                "camera_name": cam_name,
                                "timestamp": newest_event.get("timestamp", ""),
                                "image_url": newest_event.get("imageUrl", ""),
                                "event_id": newest_id,
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
                                    cam_name,
                                    event_type,
                                    newest_event.get("timestamp", ""),
                                    newest_event.get("imageUrl", ""),
                                    newest_event.get("videoClipUrl", ""),
                                    newest_event.get("videoClipUploadStatus", ""),
                                    event_id=newest_id,
                                )
                            )
                            if self.options.get("mark_events_read", False):
                                try:
                                    await self.async_mark_events_read([newest_id])
                                except asyncio.CancelledError:
                                    raise
                                except Exception as err:
                                    _LOGGER.debug(
                                        "Mark-read (new event) failed for %s: %s",
                                        cam_id,
                                        err,
                                    )
                    elif newest_id:
                        self._last_event_ids[cam_id] = newest_id

                data[cam_id] = {
                    "info": cam,
                    "status": status,
                    "events": events,
                    "live": self._live_connections.get(cam_id, {}),
                }

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
                privacy_str = cam_raw.get("privacyMode", "")
                feat_support = cam_raw.get("featureSupport", {})
                has_light = feat_support.get("light", False)
                feat_status = cam_raw.get("featureStatus", {})
                light_on = feat_status.get("frontIlluminatorInGeneralLightOn")

                cache = self._shc_state_cache.setdefault(
                    cam_id_key,
                    {
                        "device_id": None,
                        "camera_light": None,
                        "front_light": None,
                        "wallwasher": None,
                        "front_light_intensity": None,
                        "privacy_mode": None,
                        "has_light": False,
                        "notifications_status": None,
                    },
                )
                # Cloud is authoritative for privacy (fast, always available).
                # Skip overwrite if a write happened within _WRITE_LOCK_SECS — same
                # propagation-delay race as camera light.
                privacy_locked = (
                    cam_id_key in self._privacy_set_at
                    and (time.monotonic() - self._privacy_set_at[cam_id_key])
                    < self._WRITE_LOCK_SECS
                )
                if privacy_str and not privacy_locked:
                    new_privacy = privacy_str.upper() == "ON"
                    old_privacy = cache.get("privacy_mode")
                    cache["privacy_mode"] = new_privacy
                    # Hardware/external privacy trigger detection.
                    # When the cam's physical privacy button is pressed (or someone
                    # toggles privacy in the Bosch app), the cloud reports
                    # privacyMode=ON but our `BoschPrivacyModeSwitch.async_turn_on`
                    # — which is the only path that calls `_tear_down_live_stream`
                    # — never runs. The result is a stuck `state: streaming`,
                    # the live-stream switch frozen on `on`, and the TLS proxy
                    # entering an endless reconnect loop against the now-gone
                    # camera (Errno 113 Host unreachable). We detect the
                    # OFF→ON transition here and schedule the same teardown
                    # the user-toggle path uses.
                    if (
                        new_privacy is True
                        and old_privacy is not True
                        and self._live_connections.get(cam_id_key)
                    ):
                        _LOGGER.info(
                            "Privacy ON detected externally for %s — tearing down active stream",
                            cam_id_key[:8],
                        )
                        self.hass.async_create_task(
                            self._tear_down_live_stream(cam_id_key)
                        )
                cache["has_light"] = has_light
                # Use cloud featureStatus for light state; SHC supplements if available.
                # Skip overwrite if a write happened within _WRITE_LOCK_SECS — the cloud
                # API returns stale data briefly after a PUT /lighting_override, which
                # would flip the switch back to OFF right after the user turned it ON.
                light_locked = (
                    cam_id_key in self._light_set_at
                    and (time.monotonic() - self._light_set_at[cam_id_key])
                    < self._WRITE_LOCK_SECS
                )
                if light_on is not None and not light_locked:
                    # Gen2: Use lighting/switch cache for actual light state
                    # (featureStatus reports config state, not physical on/off)
                    from .models import get_model_config as _gmc_light

                    _hw = cam_raw.get("hardwareVersion", "CAMERA")
                    if _gmc_light(_hw).generation >= 2:
                        # Gen2: Only update light state from lighting/switch cache
                        # Do NOT use featureStatus (reports config, not physical state)
                        # If cache not yet populated, keep current state (don't overwrite)
                        lsc = self._lighting_switch_cache.get(cam_id_key)
                        if lsc:
                            front_bri = lsc.get("frontLightSettings", {}).get(
                                "brightness", 0
                            )
                            top_bri = lsc.get("topLedLightSettings", {}).get(
                                "brightness", 0
                            )
                            bot_bri = lsc.get("bottomLedLightSettings", {}).get(
                                "brightness", 0
                            )
                            cache["front_light"] = front_bri > 0
                            cache["wallwasher"] = top_bri > 0 or bot_bri > 0
                            cache["camera_light"] = (
                                front_bri > 0 or top_bri > 0 or bot_bri > 0
                            )
                            cache["front_light_intensity"] = (
                                front_bri / 100.0 if front_bri else 0.0
                            )
                        # else: keep current cache values, don't overwrite from featureStatus
                    else:
                        cache["camera_light"] = light_on
                        cache["front_light"] = feat_status.get(
                            "frontIlluminatorInGeneralLightOn"
                        )
                        cache["wallwasher"] = feat_status.get(
                            "wallwasherInGeneralLightOn"
                        )
                        intensity = feat_status.get(
                            "frontIlluminatorGeneralLightIntensity"
                        )
                        if intensity is not None:
                            cache["front_light_intensity"] = intensity
                elif cache.get("camera_light") is None:
                    cache["camera_light"] = None
                # Read notifications status from cloud API response.
                # Skip overwrite if written recently (same propagation-delay race as light).
                notif_status = cam_raw.get("notificationsEnabledStatus", "")
                notif_locked = (
                    cam_id_key in self._notif_set_at
                    and (time.monotonic() - self._notif_set_at[cam_id_key])
                    < self._WRITE_LOCK_SECS
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
                                    self._pan_cache[cam_id_key] = pan_data.get(
                                        "currentAbsolutePosition"
                                    )
                    except Exception as err:
                        _LOGGER.debug(
                            "Pan fetch error for %s: %s",
                            cam_id_key,
                            BoschCameraCoordinator._err_str(err),
                        )

                # ── Gen2 lighting/switch — fetched every tick (60s) ──
                # Bosch app polls this every ~40s. Slow tier (300s) is too slow
                # for responsive light state sync when lights are changed via the app.
                from .models import get_model_config as _gmc_tick

                hw_tick = cam_raw.get("hardwareVersion", "")
                if is_online and _gmc_tick(hw_tick).generation >= 2:
                    try:
                        async with asyncio.timeout(5):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/lighting/switch",
                                headers=headers,
                            ) as ls_resp:
                                if ls_resp.status == 200:
                                    self._lighting_switch_cache[
                                        cam_id_key
                                    ] = await ls_resp.json()
                    except Exception as err:
                        _LOGGER.debug(
                            "lighting/switch fetch error for %s: %s",
                            cam_id_key,
                            BoschCameraCoordinator._err_str(err),
                        )

                # ── Slow tier: wifiinfo, ambient light, motion, audio, recording ──
                # Only fetched every interval_slow seconds (default 5 min).
                # These values change rarely — fetching every tick wastes bandwidth.
                # Skipped when camera is offline or session-quota hit (444) — endpoints
                # would return 444 too, and the camera isn't truly unreachable.
                #
                # Stream-contention gate (Root-Cause: stream-freeze-on-motion-event-
                # contention.md, 2026-06-12): the Gen2 camera exposes ONE TLS control
                # channel shared by the RTSP keepalive AND every RCP / cloud slow-tier
                # read.  When both compete, OPTIONS RTT grows from ~1 s to ~21 s against
                # a 30-s RTSP session timeout → go2rtc EOF → 5-10 s video freeze.
                # Fix: defer (NOT drop) the slow-tier fetch for a camera while its live
                # stream is active.  The next coordinator tick where the stream is idle
                # picks it up via _slow_tier_deferred.  A *continuously* active stream
                # (dashboard left on live view) never goes idle, so the deferral is
                # bounded by SLOW_TIER_MAX_DEFER_SEC: once exceeded we force one read
                # despite the stream, then restart the cycle — no permanent staleness.
                # Partial coordinator stubs in unit tests bypass __init__ and may
                # lack these attributes; the real coordinator always sets them in
                # __init__.  Lazy-init keeps the gate robust without forcing every
                # stub to mirror the fields.
                if not hasattr(self, "_slow_tier_deferred"):
                    self._slow_tier_deferred = set()
                if not hasattr(self, "_slow_tier_defer_since"):
                    self._slow_tier_defer_since = {}
                stream_active = cam_id_key in self._live_connections
                # Option: defer slow-tier when stream is active (default ON).
                # When OFF, slow-tier runs regardless — diagnostic sensors stay
                # current during streaming at the cost of potential TLS contention.
                _defer_diag = bool(
                    opts.get(
                        "defer_diag_during_stream",
                        True,
                    )
                )
                # Bounded defer: a deferral that has lasted ≥ the cap must yield one
                # read even while the stream is live, else diagnostics freeze forever.
                _defer_started = self._slow_tier_defer_since.get(cam_id_key)
                defer_bound_reached = (
                    _defer_started is not None
                    and time.monotonic() - _defer_started >= SLOW_TIER_MAX_DEFER_SEC
                )
                # per-camera slow flag: True on the normal interval, OR when a deferred
                # fetch is now safe to run (stream gone idle since last deferral).
                do_slow_cam = (
                    do_slow
                    or (cam_id_key in self._slow_tier_deferred and not stream_active)
                    or defer_bound_reached
                )
                if (
                    _defer_diag
                    and do_slow_cam
                    and stream_active
                    and not defer_bound_reached
                ):
                    # Defer: stream is live — adding to deferred set instead of running.
                    self._slow_tier_deferred.add(cam_id_key)
                    self._slow_tier_defer_since.setdefault(cam_id_key, time.monotonic())
                    _LOGGER.debug(
                        "Slow-tier deferred for %s (live stream active)",
                        cam_id_key,
                    )
                    do_slow_cam = False  # skip the fetch blocks below for this camera
                elif do_slow_cam and cam_id_key in self._slow_tier_deferred:
                    # Deferred fetch now safe: stream gone idle, defer disabled, or the
                    # defer bound was reached (forced read despite an active stream).
                    self._slow_tier_deferred.discard(cam_id_key)
                    self._slow_tier_defer_since.pop(cam_id_key, None)
                    _LOGGER.debug(
                        "Slow-tier running deferred fetch for %s (%s)",
                        cam_id_key,
                        "defer bound reached, stream still active"
                        if defer_bound_reached and stream_active
                        else "stream now idle",
                    )
                if do_slow_cam and not is_online:
                    _LOGGER.debug(
                        "Slow-tier skipped for %s (%s)", cam_id_key, cam_status.lower()
                    )
                if do_slow_cam and is_online:
                    # ── Parallel slow-tier fetch ──────────────────────────────
                    # All endpoints are independent — fetch in parallel with
                    # asyncio.gather() instead of sequentially.
                    # Reduces slow-tier from ~13×5s = 65s to ~5s (single timeout).
                    hw = cam_raw.get("hardwareVersion", "")
                    pan_limit = cam_raw.get("featureSupport", {}).get("panLimit", 0)

                    async def _fetch(
                        endpoint: str,
                    ) -> tuple[str, int, dict[str, Any] | None]:
                        """Fetch a single slow-tier endpoint. Returns (endpoint, status, data)."""
                        try:
                            async with asyncio.timeout(8):
                                async with session.get(
                                    f"{CLOUD_API}/v11/video_inputs/{cam_id_key}/{endpoint}",  # noqa: B023 # closure awaited within the same loop iteration
                                    headers=headers,
                                ) as r:
                                    if r.status == 200:
                                        return (endpoint, 200, await r.json())
                                    return (endpoint, r.status, None)
                        except Exception as err:
                            _LOGGER.debug(
                                "%s fetch error for %s: %s",
                                endpoint,
                                cam_id_key,  # noqa: B023 # closure awaited within the same loop iteration
                                BoschCameraCoordinator._err_str(err),
                            )
                            return (endpoint, 0, None)

                    # Build task list (skip endpoints not applicable to this camera)
                    from .models import get_model_config as _gmc2

                    is_gen2 = _gmc2(hw).generation >= 2
                    endpoints = [
                        "wifiinfo",
                        "ambient_light_sensor_level",
                        "motion",
                        "firmware",
                        "recording_options",
                        "unread_events_count",
                        "commissioned",
                        "timestamp",
                        "notifications",
                        "rules",
                    ]
                    # Gen1 uses motion_sensitive_areas + privacy_masks (rectangles)
                    # Gen2 Outdoor II uses zones + privateAreas (polygons) — different endpoints!
                    # Gen2 Indoor II returns 442 ("hardware not supported") on privateAreas
                    # — confirmed by direct API test 2026-04-11. Only poll zones.
                    if is_gen2:
                        endpoints.append("zones")
                        if hw not in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
                            endpoints.append("privateAreas")
                    else:
                        endpoints.extend(["motion_sensitive_areas", "privacy_masks"])
                    if hw in (
                        "INDOOR",
                        "CAMERA_360",
                        "HOME_Eyes_Indoor",
                        "CAMERA_INDOOR_GEN2",
                    ):
                        endpoints.append("privacy_sound_override")
                    if pan_limit:
                        endpoints.append("autofollow")
                    has_light = cam_raw.get("featureSupport", {}).get("light", False)
                    if has_light:
                        endpoints.append("lighting_options")

                    # Gen2-only endpoints
                    if is_gen2:
                        endpoints.extend(
                            [
                                "ledlights",
                                "lens_elevation",
                                "audio",
                                "lighting/motion",
                                "lighting/ambient",
                                "lighting",
                                "intrusionDetectionConfig",
                                "audioDetectionConfig",
                            ]
                        )
                    # Gen2 Indoor II-only endpoints (alarm system + power-LED).
                    # privacy_sound_override is added above (same as Gen1 Indoor).
                    if hw in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
                        endpoints.extend(
                            [
                                "alarm_settings",
                                "alarmStatus",
                                "iconLedBrightness",
                            ]
                        )

                    results = await asyncio.gather(
                        *[_fetch(ep) for ep in endpoints],
                        return_exceptions=True,
                    )

                    # Process results
                    for fetch_result in results:
                        if isinstance(fetch_result, BaseException):
                            continue
                        ep, ep_status, ep_data = fetch_result
                        if ep_status != 200 or ep_data is None:
                            continue
                        if ep == "wifiinfo":
                            self._wifiinfo_cache[cam_id_key] = ep_data
                        elif ep == "ambient_light_sensor_level":
                            self._ambient_light_cache[cam_id_key] = ep_data.get(
                                "ambientLightSensorLevel"
                            )
                        elif ep == "motion":
                            # Skip within the write-lock window so a poll that
                            # lands before the cloud reflects the user's
                            # sensitivity change doesn't revert the UI.
                            if not self._is_write_locked(
                                cam_id_key, self._motion_set_at
                            ):
                                data[cam_id_key]["motion"] = ep_data
                        elif ep == "firmware":
                            # Write-locked like motion/privacy_sound_override above —
                            # otherwise a poll landing right after async_install()'s
                            # optimistic updating=True (before Bosch's backend has
                            # actually flagged the install) reverts it to stale
                            # "not updating" and a second install PUT could fire.
                            if not self._is_write_locked(
                                cam_id_key, self._firmware_set_at
                            ):
                                self._firmware_cache[cam_id_key] = ep_data
                        elif ep == "recording_options":
                            data[cam_id_key]["recordingOptions"] = ep_data
                        elif ep == "unread_events_count":
                            if isinstance(ep_data, dict):
                                self._unread_events_cache[cam_id_key] = int(
                                    ep_data.get("count", ep_data.get("result", 0)) or 0
                                )
                            elif isinstance(ep_data, (int, float)):
                                self._unread_events_cache[cam_id_key] = int(ep_data)
                        elif ep == "privacy_sound_override":
                            if not self._is_write_locked(
                                cam_id_key, self._privacy_sound_set_at
                            ):
                                self._privacy_sound_cache[cam_id_key] = ep_data.get(
                                    "result", False
                                )
                        elif ep == "commissioned":
                            self._commissioned_cache[cam_id_key] = ep_data
                        elif ep == "autofollow":
                            data[cam_id_key]["autofollow"] = ep_data
                        elif ep == "timestamp":
                            if not self._is_write_locked(
                                cam_id_key, self._timestamp_set_at
                            ):
                                self._timestamp_cache[cam_id_key] = ep_data.get(
                                    "result", False
                                )
                        elif ep == "notifications":
                            self._notifications_cache[cam_id_key] = ep_data
                        elif ep == "rules":
                            self._rules_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, list) else []
                            )
                        elif ep == "motion_sensitive_areas":
                            self._cloud_zones_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, list) else []
                            )
                        elif ep == "privacy_masks":
                            self._cloud_privacy_masks_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, list) else []
                            )
                        elif ep == "lighting_options":
                            self._lighting_options_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, dict) else {}
                            )
                        elif ep == "ledlights":
                            if not self._is_write_locked(
                                cam_id_key, self._ledlights_set_at
                            ):
                                self._ledlights_cache[cam_id_key] = (
                                    ep_data.get("state") == "ON"
                                    if isinstance(ep_data, dict)
                                    else None
                                )
                        elif ep == "lens_elevation":
                            self._lens_elevation_cache[cam_id_key] = (
                                ep_data.get("elevation")
                                if isinstance(ep_data, dict)
                                else None
                            )
                        elif ep == "audio":
                            self._audio_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, dict) else {}
                            )
                        elif ep == "lighting/motion":
                            self._motion_light_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, dict) else {}
                            )
                            # MotionLightSwitch state is synced via switch._is_on
                            # on its next update — nothing to do here.
                        elif ep == "lighting/ambient":
                            self._ambient_lighting_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, dict) else {}
                            )
                        elif ep == "lighting":
                            self._global_lighting_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, dict) else {}
                            )
                        elif ep == "intrusionDetectionConfig":
                            # Skip cache overwrite within the write-lock window —
                            # otherwise a slow-tier poll hitting before the cloud
                            # has caught up to the user's toggle reverts the
                            # switch back to the stale enabled value.
                            if not self._is_write_locked(
                                cam_id_key, self._intrusion_config_set_at
                            ):
                                self._intrusion_config_cache[cam_id_key] = (
                                    ep_data if isinstance(ep_data, dict) else {}
                                )
                        elif ep == "audioDetectionConfig":
                            # Glass-break / fire-alarm sound detection (Gen2
                            # Audio-Plus). Skip cache overwrite within the
                            # write-lock window so an optimistic toggle isn't
                            # reverted by a slow-tier poll before cloud catches up.
                            if not self._is_write_locked(
                                cam_id_key, self._audio_detection_set_at
                            ):
                                self._audio_detection_cache[cam_id_key] = (
                                    ep_data if isinstance(ep_data, dict) else {}
                                )
                        elif ep == "alarm_settings":
                            # Skip within the write-lock window (cloud
                            # propagation) so the optimistic cache isn't reverted.
                            if not self._is_write_locked(
                                cam_id_key, self._alarm_settings_set_at
                            ):
                                self._alarm_settings_cache[cam_id_key] = (
                                    ep_data if isinstance(ep_data, dict) else {}
                                )
                        elif ep == "alarmStatus":
                            # Actual response format confirmed 2026-04-11:
                            #   {"alarmType": "NONE" | ..., "intrusionSystem": "INACTIVE" | "ACTIVE" | ...}
                            self._alarm_status_cache[cam_id_key] = (
                                ep_data if isinstance(ep_data, dict) else {}
                            )
                            if isinstance(ep_data, dict) and not self._is_write_locked(
                                cam_id_key, self._arming_set_at
                            ):
                                intrusion = str(
                                    ep_data.get("intrusionSystem", "")
                                ).upper()
                                if intrusion == "ACTIVE":
                                    self._arming_cache[cam_id_key] = True
                                elif intrusion == "INACTIVE":
                                    self._arming_cache[cam_id_key] = False
                            if isinstance(ep_data, dict):
                                # Class-method dispatch (not bound `self.`) because
                                # existing test fixtures inject `SimpleNamespace`
                                # stubs as `self`. Those lack instance methods, so
                                # the bound-method lookup raises AttributeError —
                                # the explicit `Class.method(self, ...)` form binds
                                # at call-site instead. The helper itself is
                                # defensive (lazy-inits `_last_alarm_type`).
                                BoschCameraCoordinator._maybe_fire_intrusion_event(
                                    self,
                                    cam_id_key,
                                    cam_raw.get("title", cam_id_key),
                                    ep_data,
                                )
                        elif ep == "iconLedBrightness":
                            # Power-LED brightness 0-4 (5 discrete steps: off + 4 levels)
                            try:
                                val = (
                                    int(ep_data.get("value", 0))
                                    if isinstance(ep_data, dict)
                                    else 0
                                )
                                self._icon_led_brightness_cache[cam_id_key] = max(
                                    0, min(4, val)
                                )
                            except (TypeError, ValueError):
                                self._icon_led_brightness_cache[cam_id_key] = 0
                        elif ep == "zones":
                            zones_data: list[Any] = (
                                ep_data if isinstance(ep_data, list) else []
                            )
                            self._gen2_zones_cache[cam_id_key] = zones_data
                            _LOGGER.debug(
                                "Gen2 zones for %s: %d zones fetched",
                                cam_id_key[:8],
                                len(zones_data),
                            )
                        elif ep == "privateAreas":
                            areas_data: list[Any] = (
                                ep_data if isinstance(ep_data, list) else []
                            )
                            self._gen2_private_areas_cache[cam_id_key] = areas_data
                            _LOGGER.debug(
                                "Gen2 privateAreas for %s: %d areas fetched",
                                cam_id_key[:8],
                                len(areas_data),
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
                local_stream_active = (
                    cam_id_key in self._live_connections
                    and self._live_connections[cam_id_key].get("_connection_type")
                    == "LOCAL"
                )
                privacy_on = cam_raw.get("privacyMode", "").upper() == "ON"
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
                        rcp_connector = aiohttp.TCPConnector(
                            ssl=await async_get_bosch_cloud_ssl_context(self.hass)
                        )
                        rcp_headers = {
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        }
                        async with aiohttp.ClientSession(
                            connector=rcp_connector
                        ) as rcp_session:
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
                                            if urls:
                                                # urls[0] = "proxy-NN.live.cbs.boschsecurity.com:42090/{hash}"
                                                parts = urls[0].split("/", 1)
                                                if len(parts) == 2:
                                                    proxy_host = parts[
                                                        0
                                                    ]  # "proxy-NN:42090"
                                                    proxy_hash = parts[1]  # "{hash}"
                                                    await self._async_update_rcp_data(
                                                        cam_id_key,
                                                        proxy_host,
                                                        proxy_hash,
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
                    and self._get_cam_lan_ip(cam_id_key)
                    and self._local_creds_cache.get(cam_id_key)
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

            # ── 7. SMB/NAS upload — triggered by FCM push only (not coordinator) ──
            # Removed from coordinator tick: the full event scan took ~90s on
            # startup (checking hundreds of existing files via SMB). New events
            # are uploaded immediately when FCM push triggers alert processing.

            # ── 8. SMB daily cleanup (retention) ──────────────────────────────
            _SMB_CLEANUP_INTERVAL = 86400  # once per day
            if (
                opts.get("enable_smb_upload")
                and opts.get("smb_server")
                and opts.get("smb_retention_days", 180) > 0
                and (time.monotonic() - self._last_smb_cleanup) >= _SMB_CLEANUP_INTERVAL
            ):
                self._last_smb_cleanup = time.monotonic()
                # Fire-and-forget: cleanup walks the entire share and can take
                # minutes on large datasets. Don't block the coordinator tick.
                # Errors land in the executor future and are logged from smb.py.
                self.hass.async_create_background_task(
                    self._run_smb_cleanup_bg(),
                    "bosch_shc_camera_smb_cleanup",
                )

            # ── 8b. NVR daily retention purge ─────────────────────────────────
            _NVR_CLEANUP_INTERVAL = 86400  # once per day
            if (
                opts.get("enable_nvr", False)
                and int(opts.get("nvr_retention_days", 3)) > 0
                and (time.monotonic() - self._last_nvr_cleanup) >= _NVR_CLEANUP_INTERVAL
            ):
                self._last_nvr_cleanup = time.monotonic()
                self.hass.async_create_background_task(
                    self._run_nvr_cleanup_bg(),
                    "bosch_shc_camera_nvr_cleanup",
                )

            # Quality-Scale Gold (stale-devices): remove devices for cameras that
            # no longer exist in the Bosch cloud account. Skip on the fast first
            # tick so we don't race the device-registry creation in async_setup_entry.
            if not is_first_tick and data:
                self._cleanup_stale_devices(set(data.keys()))

            # Per-camera availability transition notifier — fires when a cam
            # flips between online and offline. First tick is silent (records
            # baseline). Skipped on the fast first tick because the cache is
            # still being populated and could carry a stale "online" from a
            # restore_state load. Defensive getattr handles stub coordinators
            # in unit tests that bypass __init__ (~80 fixtures across the
            # test suite).
            _announce = getattr(self, "_async_maybe_announce_camera_status", None)
            _compute = getattr(self, "_compute_status_for", None)
            if (
                not is_first_tick
                and data
                and _announce is not None
                and _compute is not None
            ):
                for _cam_id, _cam_data in data.items():
                    new_status = _compute(_cam_id, _cam_data)
                    self.hass.async_create_task(_announce(_cam_id, new_status))

            # Persist LAN IPs (cam_id → IP) so the next cloud-degraded
            # startup can ping cameras without first needing a cloud call.
            # Throttle: only write if the mapping actually changed.
            _store = getattr(self, "_lan_ips_store", None)
            if _store is not None and self._rcp_lan_ip_cache:
                _snapshot = {k: v for k, v in self._rcp_lan_ip_cache.items() if v}
                _prev = getattr(self, "_lan_ips_snapshot", None)
                if _snapshot and _snapshot != _prev:
                    self._lan_ips_snapshot = _snapshot
                    self.hass.async_create_task(_store.async_save(_snapshot))

            # Persist hardware versions (cam_id → hw_version) for the same
            # cloud-degraded-startup reason — without this, _is_gen2() defaults
            # to Gen1 after a cold start during a cloud outage, which makes
            # the LAN-fallback gate on the privacy / front-light switches
            # report "unavailable" even though the LAN RCP path would work.
            _hw_store = getattr(self, "_hw_version_store", None)
            if _hw_store is not None and self._hw_version:
                _hw_snapshot = {k: v for k, v in self._hw_version.items() if v}
                _hw_prev = getattr(self, "_hw_version_snapshot", None)
                if _hw_snapshot and _hw_snapshot != _hw_prev:
                    self._hw_version_snapshot = _hw_snapshot
                    self.hass.async_create_task(_hw_store.async_save(_hw_snapshot))

            # Persist LOCAL Digest creds so LAN-fallback privacy / light
            # writes survive HA restarts during a Bosch cloud outage. Without
            # this, every cold restart while the cloud is 503 leaves the cred
            # cache empty and the LAN RCP write returns <err> "no auth".
            _creds_store = getattr(self, "_local_creds_store", None)
            if _creds_store is not None and self._local_creds_cache:
                _cred_snapshot = {
                    k: {
                        "user": v["user"],
                        "password": v["password"],
                        "host": v["host"],
                        "port": v.get("port", 443),
                    }
                    for k, v in self._local_creds_cache.items()
                    if v.get("user") and v.get("password") and v.get("host")
                }
                _cred_prev = getattr(self, "_local_creds_snapshot", None)
                if _cred_snapshot and _cred_snapshot != _cred_prev:
                    self._local_creds_snapshot = _cred_snapshot
                    self.hass.async_create_task(_creds_store.async_save(_cred_snapshot))

            # Periodic background refresh of the Bosch community maintenance
            # feed — once per hour on a healthy coordinator tick. Reactive
            # refresh on 503 is handled inside the cloud-call branch.
            # getattr defaults handle stub coordinators in tests that bypass __init__.
            _maint_last = getattr(self, "_maintenance_last_fetch", float("-inf"))
            _maint_interval = getattr(self, "_MAINTENANCE_INTERVAL_S", 3600.0)
            _maint_refresh = getattr(self, "_async_refresh_maintenance", None)
            if _maint_refresh is not None and (now - _maint_last) >= _maint_interval:
                self.hass.async_create_task(_maint_refresh(reactive=False))

            # Cloud-state transition notifier (v12.4.11). Schedule as a
            # background task so the coordinator main loop is not delayed by
            # a slow notify service. getattr handles stub coordinators in
            # tests that bypass __init__.
            _cloud_alert = getattr(self, "_async_maybe_announce_cloud_state", None)
            if _cloud_alert is not None:
                self.hass.async_create_task(_cloud_alert(True))

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

            return data

        except UpdateFailed:
            _cloud_alert = getattr(self, "_async_maybe_announce_cloud_state", None)
            if _cloud_alert is not None:
                self.hass.async_create_task(_cloud_alert(False))
            raise
        except TimeoutError:
            _maint = getattr(self, "_async_refresh_maintenance", None)
            if _maint is not None:
                self.hass.async_create_task(_maint(reactive=True))
            _outage_ping = getattr(self, "_async_outage_ping_all", None)
            if _outage_ping is not None:
                self.hass.async_create_task(_outage_ping())
            _cloud_alert = getattr(self, "_async_maybe_announce_cloud_state", None)
            if _cloud_alert is not None:
                self.hass.async_create_task(_cloud_alert(False))
            raise UpdateFailed(
                "Timeout fetching camera data from Bosch cloud"
            ) from None
        except aiohttp.ClientError as err:
            _cloud_alert = getattr(self, "_async_maybe_announce_cloud_state", None)
            if _cloud_alert is not None:
                self.hass.async_create_task(_cloud_alert(False))
            raise UpdateFailed(f"Network error: {err}") from err

    def _refresh_notifications_disabled_issues(self) -> None:
        """Create or clear Repairs issues for cameras with disabled movement/person notifications.

        Called once per coordinator tick (inside _async_update_data) AFTER data is
        built.  Idempotent — safe to call every tick.

        A camera is only processed when its notifications dict is non-empty
        (i.e. the endpoint has been fetched at least once).  Cameras with no
        notification data yet are skipped entirely to avoid false-positive
        issues on startup.
        """
        for cam_id, notif in self._notifications_cache.items():
            if not notif:
                # No data fetched yet — skip to avoid false positives.
                continue

            disabled = [t for t in ("movement", "person") if notif.get(t) is False]

            if disabled:
                cam_title: str = (
                    (self.data or {})
                    .get(cam_id, {})
                    .get("info", {})
                    .get("title", cam_id)
                )
                types_str = " + ".join(t.capitalize() for t in disabled)
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    f"notifications_disabled_{cam_id}",
                    is_fixable=False,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="notifications_disabled",
                    translation_placeholders={
                        "camera": cam_title,
                        "types": types_str,
                    },
                )
                if cam_id not in self._notif_disabled_logged:
                    self._notif_disabled_logged.add(cam_id)
                    _LOGGER.warning(
                        "Camera %r has %s cloud notification(s) disabled — "
                        "the corresponding binary sensor(s) will stay 'Clear'. "
                        "Enable the notification switch(es) in Home Assistant or "
                        "the Bosch Smart Home app.",
                        cam_title,
                        types_str,
                    )
            else:
                ir.async_delete_issue(
                    self.hass,
                    DOMAIN,
                    f"notifications_disabled_{cam_id}",
                )
                self._notif_disabled_logged.discard(cam_id)

    def _refresh_firmware_update_issues(self) -> None:
        """Create or clear Repairs issues for cameras with a firmware update available.

        Called once per coordinator tick (inside _async_update_data) AFTER data is
        built. Idempotent — safe to call every tick. Mirrors
        _refresh_notifications_disabled_issues (same Repairs-issue pattern):
        previously a firmware update becoming available had NO user-visible
        signal from the integration at all — only HA core's own generic
        Settings → Updates panel, easy to miss (Thomas report 2026-07-07,
        "just had a firmware update, got no alert").

        A camera is only processed once its firmware endpoint has been fetched
        at least once (`_firmware_cache[cam_id]['upToDate']` present) to avoid
        a false-positive "issue cleared" transition on startup.
        """
        for cam_id, fw in self._firmware_cache.items():
            if not fw:
                # No data fetched yet — skip to avoid false positives.
                continue

            up_to_date = fw.get("upToDate")
            if up_to_date is None:
                continue

            issue_id = f"firmware_update_available_{cam_id}"

            if not up_to_date:
                cam_title: str = (
                    (self.data or {})
                    .get(cam_id, {})
                    .get("info", {})
                    .get("title", cam_id)
                )
                current = fw.get("current") or "?"
                latest = fw.get("update") or "?"
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=True,
                    is_persistent=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="firmware_update_available",
                    translation_placeholders={
                        "camera": cam_title,
                        "current": current,
                        "latest": latest,
                    },
                    data={"cam_id": cam_id},
                )
                if cam_id not in self._fw_update_alerted:
                    self._fw_update_alerted.add(cam_id)
                    _LOGGER.info(
                        "Firmware update available for %r: %s -> %s",
                        cam_title,
                        current,
                        latest,
                    )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                self._fw_update_alerted.discard(cam_id)

    async def async_install_firmware(self, cam_id: str) -> None:
        """Install the pending firmware update for `cam_id` right now.

        Shared by two entry points: the `update` entity's Install button
        (update.py, BoschFirmwareUpdate.async_install) and the "Fix" action on
        the `firmware_update_available` Repairs issue (repairs.py) — one
        implementation so both stay in sync instead of duplicating the
        guard/write-lock logic.

        PUTs the same endpoint/payload the official Bosch app's "Update now"
        button uses (research/apk_2.12.0 decompile: FirmwareBackendService.
        UpdateCameraFirmware — {"id": <update field>} to the same URL this
        integration already GETs for status).
        """
        fw: dict[str, Any] = self._firmware_cache.get(cam_id, {})
        if fw.get("updating"):
            raise HomeAssistantError("Firmware install is already in progress")
        target = fw.get("update")
        if not target:
            raise HomeAssistantError(
                "No firmware update is currently available to install"
            )
        ok = await self.async_put_camera(cam_id, "firmware", {"id": target})
        if not ok:
            raise HomeAssistantError(
                f"Bosch cloud rejected the firmware install request for {target}"
            )
        fw["updating"] = True
        self._firmware_cache[cam_id] = fw
        self._firmware_set_at[cam_id] = time.monotonic()

    async def async_soft_reset_camera(self, cam_id: str) -> None:
        """Reboot the camera (soft reset).

        PUTs the same bodyless endpoint the official Bosch app's camera
        "Restart" action uses (research/apk_2.12.0 decompile:
        BackendUrlProviderService.GetCameraSoftResetUrl → PUT
        video_inputs/{id}/soft_reset). The camera briefly drops offline
        while it reboots; no local state to update here — the next
        status poll picks up the new online/offline state naturally.

        Live-tested 2026-07-08 against a real online camera: Bosch's
        cloud returned HTTP 404 sh:entity.notfound despite the request
        matching the app byte-for-byte — the button entity is disabled
        by default (button.py) for this reason.
        """
        ok = await self.async_put_camera(cam_id, "soft_reset", None)
        if not ok:
            raise HomeAssistantError(
                "Bosch cloud rejected the soft-reset (restart) request"
            )

    async def async_hard_reset_camera(self, cam_id: str) -> None:
        """Factory-reset the camera (hard reset).

        PUTs the same bodyless endpoint the official Bosch app's camera
        "Factory Reset" action uses (research/apk_2.12.0 decompile:
        BackendUrlProviderService.GetCameraHardResetUrl → PUT
        video_inputs/{id}/hard_reset). Unlike soft reset, this is
        destructive — the camera loses its Bosch account pairing and
        must be re-commissioned from scratch via the Bosch app before it
        will work with this integration again. The button entity is
        disabled by default for exactly this reason (button.py).
        """
        ok = await self.async_put_camera(cam_id, "hard_reset", None)
        if not ok:
            raise HomeAssistantError(
                "Bosch cloud rejected the hard-reset (factory reset) request"
            )

    async def _async_refresh_maintenance(self, *, reactive: bool) -> None:
        """Fetch the Bosch community maintenance announcement in the background.

        Reactive calls (triggered by cloud 5xx/timeout) are rate-limited so a
        flapping cloud does not hammer the community site. Periodic calls run
        once per _MAINTENANCE_INTERVAL_S regardless of cloud health.

        Failure is silent — the previous cache value is retained so the sensor
        does not flap on a transient community-site outage.
        """
        from .maintenance import async_fetch_maintenance

        now = time.monotonic()
        if (
            reactive
            and (now - self._maintenance_last_fetch)
            < self._MAINTENANCE_REACTIVE_COOLDOWN_S
        ):
            return
        self._maintenance_last_fetch = now
        try:
            session = async_get_clientsession(self.hass)
            result = await async_fetch_maintenance(session)
        except Exception as exc:
            _LOGGER.debug("Maintenance fetch raised: %s", exc)
            return
        if result is not None:
            self._maintenance_cache = result
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

        Triggers on state in {scheduled, active, past}, deduped by (link,
        state) so each window announces at most three times: scheduled when
        first seen, active when the window opens, past when it closes. The
        `past` announcement only fires if we previously announced `active`
        for the same link — otherwise an old past window discovered mid-feed
        would spam users with stale "wartung beendet" messages.

        Recent/unknown/idle states stay silent (no actionable info). Service
        routing: get_alert_services(coordinator, "system") — falls back to
        `alert_notify_service`, matching the existing TROUBLE event plumbing.

        Failure is non-fatal — a notify service can be misconfigured by the
        user, but maintenance discovery itself must keep working.
        """
        if not mw.camera_relevant:
            return
        state = mw.state()
        if state not in ("scheduled", "active", "past"):
            return
        # `past` only announces when we already announced `active` for this
        # same window (same link). Suppresses stale past-window discovery.
        if state == "past":
            prior = self._maintenance_notified_key
            if prior is None or prior[0] != mw.link or prior[1] != "active":
                self._maintenance_notified_key = (mw.link, state)
                getattr(self, "_persist_maint_notified_key", lambda: None)()
                return
        notify_key = (mw.link, state)
        if self._maintenance_notified_key == notify_key:
            return
        from .fcm import build_notify_data, get_alert_services

        services = get_alert_services(self, "system")
        if not services:
            _LOGGER.debug("Maintenance announce skipped: no notify service configured")
            self._maintenance_notified_key = notify_key
            getattr(self, "_persist_maint_notified_key", lambda: None)()
            return
        from zoneinfo import ZoneInfo

        when = ""
        if mw.scheduled_start and mw.scheduled_end:
            tz = ZoneInfo("Europe/Berlin")
            start = mw.scheduled_start.astimezone(tz)
            end = mw.scheduled_end.astimezone(tz)
            when = f"{start.strftime('%a %d.%m. %H:%M')}–{end.strftime('%H:%M')}"
        verb_map = {"scheduled": "geplant", "active": "läuft", "past": "beendet"}
        verb = verb_map[state]
        title = f"Bosch Cloud-Wartung {verb}"
        body_lines = [mw.title or "Wartungsmeldung"]
        if when:
            body_lines.append(when)
        if state == "active":
            body_lines.append("Live-Bild und Snapshots ggf. eingeschränkt.")
        elif state == "past":
            body_lines.append("Cloud-Dienste sollten wieder normal funktionieren.")
        if mw.link:
            body_lines.append(mw.link)
        message = "\n".join(body_lines)
        for svc in services:
            try:
                data = build_notify_data(svc, message, title=title)
                # `alert_notify_service` option stores entries like `notify.<svc>`
                # OR bare service names `<svc>`. Mirror the FCM-side split so
                # `hass.services.async_call("notify", "<svc>", ...)` resolves
                # correctly. Pre-fix: hardcoded "notify" + svc="notify.<svc>"
                # produced `notify.notify.<svc>` and silently failed.
                _domain, _service = svc.split(".", 1) if "." in svc else ("notify", svc)
                await self.hass.services.async_call(
                    _domain, _service, data, blocking=False
                )
                _LOGGER.info(
                    "Maintenance announce sent via notify.%s (state=%s, window=%s)",
                    svc,
                    state,
                    when or "(no window)",
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Maintenance announce via notify.%s failed: %s",
                    svc,
                    exc,
                )
        self._maintenance_notified_key = notify_key
        getattr(self, "_persist_maint_notified_key", lambda: None)()

    def _persist_maint_notified_key(self) -> None:
        """Write `_maintenance_notified_key` to disk so HA restarts mid-
        window do not re-fire the active-state announcement on the next
        coordinator tick. Bug 2026-05-20: ~20 duplicate alerts during a
        single Bosch maintenance window because every restart wiped the
        in-memory dedup key.
        """
        key = self._maintenance_notified_key
        store = getattr(self, "_maint_notified_store", None)
        if store is None or key is None:
            return
        self.hass.async_create_task(store.async_save({"link": key[0], "state": key[1]}))

    def _persist_cloud_outage_flag(self) -> None:
        """Mirror the maintenance-key persistence for the cloud-state
        dedup flag, so a restart mid-outage doesn't re-fire "Cloud nicht
        erreichbar"."""
        store = getattr(self, "_cloud_alert_store", None)
        if store is None:
            return
        self.hass.async_create_task(
            store.async_save({"outage_notified": bool(self._cloud_outage_notified)})
        )

    async def _async_maybe_announce_camera_status(
        self,
        cam_id: str,
        new_status: str,
    ) -> None:
        """Fire a notification when a camera flips between online and offline.

        The first observation per camera is silent — we record the baseline
        without notifying so a HA restart while a camera is offline does not
        re-announce the existing state. Only `online → offline` and
        `offline → online` transitions notify; `unknown` is treated as a
        non-event (camera info is just temporarily missing, not a real
        availability change).

        Routing matches the maintenance path: `alert_notify_system` falls
        back to `alert_notify_service`. Notify failures are swallowed.
        """
        # Lazy-init for SimpleNamespace test stubs that bypass __init__.
        if not hasattr(self, "_offline_seen_at"):
            self._offline_seen_at = {}
        last = self._last_camera_status.get(cam_id)
        if last is None:
            # First tick after startup — record baseline silently.
            self._last_camera_status[cam_id] = new_status
            return
        # Whenever the camera is currently online, drop any pending offline-grace
        # timer (covers recovery within the grace window AND the no-op
        # online→online tick below).
        if new_status == "online":
            self._offline_seen_at.pop(cam_id, None)
        if new_status == last:
            return
        # Skip transitions involving "unknown" — coordinator hickups can flap
        # status to UNKNOWN for one tick during cloud transients; do not
        # convert that into spam.
        if new_status == "unknown" or last == "unknown":
            self._last_camera_status[cam_id] = new_status
            return
        # Offline-announce grace: a camera on a Wi-Fi repeater/mesh briefly drops
        # during a repeater restart or DFS channel change and recovers within a
        # minute or two. Only announce offline once it has stayed offline for
        # CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC; a recovery within the window is
        # silent. We hold the baseline at "online" (don't commit the flip) until
        # the grace elapses, so the eventual recovery doesn't emit a spurious
        # "online" notification either.
        if new_status == "offline":
            seen = self._offline_seen_at.get(cam_id)
            now_mono = time.monotonic()
            if seen is None:
                self._offline_seen_at[cam_id] = now_mono
                return
            if (now_mono - seen) < CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC:
                return
        self._last_camera_status[cam_id] = new_status
        from .fcm import build_notify_data, get_alert_services

        services = get_alert_services(self, "system")
        cam_info = self.data.get(cam_id, {}).get("info", {})
        cam_name = cam_info.get("title") or cam_id[:8]
        if not services:
            _LOGGER.debug(
                "Camera status announce skipped for %s (%s→%s): no notify service configured",
                cam_name,
                last,
                new_status,
            )
            return
        if new_status == "offline":
            title = f"Bosch Kamera {cam_name} offline"
            message = (
                f"Bosch Kamera {cam_name} ist offline. "
                "Live-Bild und Snapshots sind bis zur Wiederverbindung nicht verfügbar."
            )
        else:
            title = f"Bosch Kamera {cam_name} wieder online"
            message = f"Bosch Kamera {cam_name} ist wieder erreichbar."
        for svc in services:
            try:
                data = build_notify_data(svc, message, title=title)
                # `alert_notify_service` option stores entries like `notify.<svc>`
                # OR bare service names `<svc>`. Mirror the FCM-side split so
                # `hass.services.async_call("notify", "<svc>", ...)` resolves
                # correctly. Pre-fix: hardcoded "notify" + svc="notify.<svc>"
                # produced `notify.notify.<svc>` and silently failed.
                _domain, _service = svc.split(".", 1) if "." in svc else ("notify", svc)
                await self.hass.services.async_call(
                    _domain, _service, data, blocking=False
                )
                _LOGGER.info(
                    "Camera status announce sent via notify.%s for %s (%s→%s)",
                    svc,
                    cam_name,
                    last,
                    new_status,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Camera status announce via notify.%s for %s failed: %s",
                    svc,
                    cam_name,
                    exc,
                )

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

        Outage path: when ``success=False`` for at least
        ``_CLOUD_OUTAGE_NOTIFY_AFTER_S`` seconds in a row, fire a one-shot
        "Bosch Cloud nicht erreichbar" notification. Recovery path: when the
        next ``success=True`` arrives after an outage was announced, fire
        "Bosch Cloud wieder erreichbar". One-tick failure blips never get
        announced — they self-clear on the next success.

        Suppressed while an RSS-announced maintenance window is `active`
        because the maintenance lifecycle notifier (v12.4.8) already told
        the user. We still record state transitions internally so we are
        able to announce a recovery once the window closes if needed.

        Routing: `alert_notify_system` → falls back to
        `alert_notify_service`, same path as TROUBLE_DISCONNECT and the
        maintenance announcements. Notify failures are swallowed.
        """
        now = time.monotonic()
        # Active-maintenance check — if Bosch announced this exact outage as
        # planned, stay silent.
        in_maintenance = False
        mw = self._maintenance_cache
        if mw is not None and mw.camera_relevant and mw.state() == "active":
            in_maintenance = True
        if success:
            if not self._cloud_outage_notified:
                # Was either healthy already or in a sub-grace blip — just
                # reset the tracker so the next outage starts a fresh window.
                self._cloud_outage_started_at = None
                return
            # We previously announced an outage — announce recovery now.
            self._cloud_outage_notified = False
            self._cloud_outage_started_at = None
            getattr(self, "_persist_cloud_outage_flag", lambda: None)()
            if in_maintenance:
                _LOGGER.debug(
                    "Cloud recovered during active maintenance — staying silent"
                )
                return
            await self._async_dispatch_cloud_alert(recovered=True)
            return
        # success=False
        if self._cloud_outage_started_at is None:
            self._cloud_outage_started_at = now
            return
        if self._cloud_outage_notified:
            return
        if (now - self._cloud_outage_started_at) < self._CLOUD_OUTAGE_NOTIFY_AFTER_S:
            return
        # Outage has persisted long enough → announce, but stay silent during
        # known maintenance.
        self._cloud_outage_notified = True
        getattr(self, "_persist_cloud_outage_flag", lambda: None)()
        if in_maintenance:
            _LOGGER.debug("Cloud outage suppressed: known active maintenance window")
            return
        await self._async_dispatch_cloud_alert(recovered=False)

    async def _async_dispatch_cloud_alert(self, *, recovered: bool) -> None:
        """Send the actual notification through the integration's alert pipeline."""
        from .fcm import build_notify_data, get_alert_services

        services = get_alert_services(self, "system")
        if not services:
            _LOGGER.debug(
                "Cloud-state alert skipped (recovered=%s) — no notify service configured",
                recovered,
            )
            return
        if recovered:
            title = "Bosch Cloud wieder erreichbar"
            message = (
                "Die Bosch-Cloud antwortet wieder. "
                "Snapshots und Stream-Anfragen laufen normal."
            )
        else:
            title = "Bosch Cloud nicht erreichbar"
            message = (
                "Die Bosch-Cloud antwortet nicht mehr (HTTP 5xx / Timeout). "
                "Privacy- und Licht-Schalter gehen weiter über LAN, "
                "Snapshots und Stream-Anfragen sind eingeschränkt."
            )
        for svc in services:
            try:
                data = build_notify_data(svc, message, title=title)
                # `alert_notify_service` option stores entries like `notify.<svc>`
                # OR bare service names `<svc>`. Mirror the FCM-side split so
                # `hass.services.async_call("notify", "<svc>", ...)` resolves
                # correctly. Pre-fix: hardcoded "notify" + svc="notify.<svc>"
                # produced `notify.notify.<svc>` and silently failed.
                _domain, _service = svc.split(".", 1) if "." in svc else ("notify", svc)
                await self.hass.services.async_call(
                    _domain, _service, data, blocking=False
                )
                _LOGGER.info(
                    "Cloud-state alert sent via notify.%s (recovered=%s)",
                    svc,
                    recovered,
                )
            except Exception as exc:
                _LOGGER.warning(
                    "Cloud-state alert via notify.%s failed: %s",
                    svc,
                    exc,
                )

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

    def _cleanup_stale_devices(self, current_cam_ids: set[str]) -> None:
        """Remove devices for cameras no longer in the Bosch cloud account.

        Quality-Scale Gold rule `stale-devices`. Compares the device registry
        against the freshly-fetched camera list — anything tied to our domain
        with a cam_id that disappeared gets removed (entities + device entry).
        Without this, a camera removed from the Bosch app stays visible in HA
        as `unavailable` forever.
        """
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(dev_reg, self._entry.entry_id):
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

    # ── Live stream safety guards ────────────────────────────────────────────
    # Prevents concurrent stream setup, privacy toggles during warm-up, etc.
    # _stream_setup_lock: per-camera asyncio.Lock to serialize stream operations
    # _stream_warming: set of cam_ids currently in warm-up phase (blocks privacy toggles)

    def _get_stream_lock(self, cam_id: str) -> asyncio.Lock:
        """Get or create per-camera stream setup lock.

        Safe under asyncio: check-then-insert has no `await` between the
        two steps, so concurrent coroutines cannot interleave here.
        """
        lock = self._stream_locks.get(cam_id)
        if lock is None:
            lock = asyncio.Lock()
            self._stream_locks[cam_id] = lock
        return lock

    def _get_rcp_session_lock(self, proxy_hash: str) -> asyncio.Lock:
        """Get or create per-proxy_hash RCP session-open lock.

        Safe under asyncio: check-then-insert has no `await` between the
        two steps, so concurrent coroutines cannot interleave here.
        """
        lock = self._rcp_session_locks.get(proxy_hash)
        if lock is None:
            lock = asyncio.Lock()
            self._rcp_session_locks[proxy_hash] = lock
        return lock

    def clear_stream_warming(self, cam_id: str) -> None:
        """Force-clear the stream-warming flag for a camera.

        Used by is_stream_warming() when the flag is stale (live_connections
        no longer has the cam_id, so the warm-up must have completed or
        errored out without resetting the flag).
        """
        self._stream_warming.discard(cam_id)

    def is_stream_warming(self, cam_id: str) -> bool:
        """True if this camera is currently in the warm-up phase.

        Auto-clears stale flags in three scenarios:
          1. cam_id in `_stream_warming` but NOT in `_live_connections` — the
             previous warm-up errored out without resetting the flag (fix
             2026-04-11).
          2. cam_id in `_stream_warming` AND `_live_connections[cam_id]` has
             a non-empty `rtspsUrl` — pre-warm actually completed, the flag
             just wasn't discarded (race in `_try_live_connection_inner` exit
             paths). Observed 2026-04-27 on Gen1 Outdoor + Gen1 Indoor cams
             during simultaneous 4-camera toggle: state stuck at
             `warming_up` with `live_rtsps=null` for >7 min while keepalive
             was already running (gen=2, 480s into session).
          3. cam_id in `_stream_warming` for >300 s — hard timeout. Pre-warm
             worst case is ~120 s (CAMERA_EYES outdoor 8 retries × 15 s).
             Anything longer is stuck — clear and let the next toggle reset
             cleanly rather than blocking privacy/snapshot UI forever.
        """
        import time as _time

        if cam_id not in self._stream_warming:
            return False
        # Scenario 1: warming flag without _live_connections entry
        if cam_id not in self._live_connections:
            _LOGGER.debug(
                "Clearing stale stream-warming flag for %s (no live conn)", cam_id[:8]
            )
            self._stream_warming.discard(cam_id)
            self._stream_warming_started.pop(cam_id, None)
            return False
        live = self._live_connections.get(cam_id, {})
        # Scenario 2: warming flag but pre-warm actually finished (URL set)
        if live.get("rtspsUrl") or live.get("rtspUrl"):
            _LOGGER.debug(
                "Clearing stale stream-warming flag for %s (rtspsUrl already set — race)",
                cam_id[:8],
            )
            self._stream_warming.discard(cam_id)
            self._stream_warming_started.pop(cam_id, None)
            return False
        # Scenario 3: warming for >180 s — hard timeout. Pre-warm worst case is
        # ~150 s (CAMERA_EYES outdoor: 8 retries × 13 s + 35 s min_total_wait +
        # buffer). 180 s leaves a small safety margin without holding the
        # privacy toggle hostage for 5 minutes on a stuck warm-up.
        # -inf (not 0) as the missing-key default (SENTINEL_RULE): an entry in
        # _stream_warming with no start timestamp is an inconsistent state — treat
        # it as stuck and clear it rather than holding the privacy toggle hostage
        # forever (a `0` default is falsy and would skip the failsafe entirely).
        started = self._stream_warming_started.get(cam_id, float("-inf"))
        elapsed = _time.monotonic() - started
        if elapsed > 180:
            _LOGGER.warning(
                "Clearing stuck stream-warming flag for %s (warming for %s)",
                cam_id[:8],
                f"{elapsed:.0f}s" if started != float("-inf") else "unknown duration",
            )
            self._stream_warming.discard(cam_id)
            self._stream_warming_started.pop(cam_id, None)
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
        if bool(self._shc_state_cache.get(cam_id, {}).get("privacy_mode")):
            _LOGGER.info(
                "try_live_connection: privacy mode active for %s — stream blocked",
                cam_id[:8],
            )
            return None
        lock = self._get_stream_lock(cam_id)
        # A recovery rebuild (force_reset) must WAIT for the lock, never skip:
        # the teardown of the old proxy now happens INSIDE the lock (see
        # _try_live_connection_inner) so a concurrent renewal/heartbeat can't
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
            return await self._try_live_connection_inner(
                cam_id, is_renewal, force_reset
            )

    async def _try_live_connection_inner(
        self, cam_id: str, is_renewal: bool = False, force_reset: bool = False
    ) -> dict[str, Any] | None:
        """Inner implementation of try_live_connection (called under lock)."""
        if force_reset:
            # Recovery rebuild (401 rescue / proxy-died): tear the old session
            # and proxy down HERE, under the per-cam stream lock, so the stop is
            # serialized against any concurrent renewal/heartbeat that is also
            # holding the lock to (re)build the proxy. Doing the stop in the
            # caller (outside the lock) let a renewal publish Stream/go2rtc
            # against the port we'd just killed → frozen image (race 2026-06-01).
            self._live_connections.pop(cam_id, None)
            self._stream_warming.discard(cam_id)
            self._stream_warming_started.pop(cam_id, None)
            await self._stop_tls_proxy(cam_id)
        token = self.token
        if not token:
            _LOGGER.warning("try_live_connection: no token available")
            return None

        # Use a dedicated session with SSL verification disabled.
        # Bosch Cloud API uses a private CA (Video CA 2A).
        # NOTE: explicit try/finally around session.close() (rather than
        # `async with aiohttp.ClientSession(...)`) is deliberate here —
        # this method spans ~270 lines of stream-setup logic and the extra
        # indent level is a readability liability. ClientSession's default
        # connector_owner=True makes session.close() also close the connector,
        # so the finally block below is leak-free.
        connector = aiohttp.TCPConnector(
            ssl=await async_get_bosch_cloud_ssl_context(self.hass)
        )
        session = aiohttp.ClientSession(connector=connector)

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        try:
            hq, inst = self.get_quality_params(cam_id)
            # [S7] Direct key read — opts only used for this one key; avoids full dict copy
            conn_type_pref = self._stream_type_override or self._entry.options.get(
                "stream_connection_type", "local"
            )
            if conn_type_pref == "local":
                candidates = ["LOCAL"]
            elif conn_type_pref == "auto":
                cfg = self.get_model_config(cam_id)
                # Time-decay the error counter: failures older than the TTL
                # belong to a previous network state (router reboot, transient
                # WLAN dropout). Without decay the cam stays pinned on REMOTE
                # forever because record_stream_success only fires on a
                # successful LOCAL stream — and AUTO has stopped attempting
                # LOCAL by then. Cf. _LOCAL_RESCUE_TTL_SEC / _local_rescue_at.
                # TTL is shorter when LAN is currently reachable (the burst is
                # almost certainly stale) and longer otherwise (LAN may still
                # be flaky — keep the cam on REMOTE a bit longer to avoid
                # ping-pong fallback loops).
                lan_ok = self._lan_tcp_reachable.get(cam_id, (False, 0))[0]
                _STREAM_ERROR_TTL_SEC = 300 if lan_ok else 1800
                err_ts = self._stream_error_at.get(cam_id, 0)
                if err_ts and (time.monotonic() - err_ts) > _STREAM_ERROR_TTL_SEC:
                    if self._stream_error_count.get(cam_id, 0) > 0:
                        _LOGGER.info(
                            "AUTO mode: %s stream-error counter aged out "
                            "(%.0fs since last error, LAN=%s) — re-attempting LOCAL",
                            cam_id[:8],
                            time.monotonic() - err_ts,
                            "ok" if lan_ok else "unknown",
                        )
                    self._stream_error_count.pop(cam_id, None)
                    self._stream_error_at.pop(cam_id, None)
                    self._stream_fell_back.pop(cam_id, None)
                # Check if LOCAL should be skipped:
                # 1. Too many consecutive stream errors → fall back to REMOTE
                err_count = self._stream_error_count.get(cam_id, 0)
                if err_count >= cfg.max_stream_errors:
                    _LOGGER.warning(
                        "AUTO mode: %s had %d consecutive LOCAL errors — falling back to REMOTE",
                        cam_id[:8],
                        err_count,
                    )
                    self._stream_fell_back[cam_id] = True
                    candidates = ["REMOTE"]
                else:
                    # 2. WiFi signal too weak → prefer REMOTE
                    wifi = self._wifiinfo_cache.get(cam_id, {}).get(
                        "signalStrength", 100
                    )
                    if isinstance(wifi, (int, float)) and wifi < cfg.min_wifi_for_local:
                        _LOGGER.info(
                            "AUTO mode: %s WiFi %d%% < %d%% threshold — using REMOTE",
                            cam_id[:8],
                            wifi,
                            cfg.min_wifi_for_local,
                        )
                        candidates = [
                            "REMOTE",
                            "LOCAL",
                        ]  # prefer REMOTE but try LOCAL as fallback
                    else:
                        candidates = ["LOCAL", "REMOTE"]
                    self._stream_fell_back[cam_id] = False
            else:
                candidates = ["REMOTE"]

            # ── TCP pre-check: skip LOCAL if camera is LAN-unreachable ──────
            # When AUTO mode has both LOCAL and REMOTE as candidates and we
            # know the camera's LAN IP, a 1.5s TCP ping decides immediately —
            # saving 45–100s of pre-warm timeout for cameras on a different
            # network/VLAN or that are powered off. Result is cached 60s so
            # repeated stream starts don't each trigger a fresh ping.
            if "LOCAL" in candidates and "REMOTE" in candidates:
                lan_ip = self._get_cam_lan_ip(cam_id)
                if lan_ip:
                    _TCP_TTL = 60.0
                    cached_tcp = self._lan_tcp_reachable.get(cam_id)
                    now_tcp = time.monotonic()
                    if cached_tcp and (now_tcp - cached_tcp[1]) < _TCP_TTL:
                        tcp_ok = cached_tcp[0]
                        _LOGGER.debug(
                            "TCP pre-check cache HIT for %s (%s): %s",
                            cam_id[:8],
                            lan_ip,
                            "reachable" if tcp_ok else "unreachable",
                        )
                    else:
                        tcp_ok = await self._async_local_tcp_ping(cam_id)
                        _LOGGER.debug(
                            "TCP pre-check for %s (%s): %s",
                            cam_id[:8],
                            lan_ip,
                            "reachable" if tcp_ok else "unreachable",
                        )
                    if not tcp_ok:
                        _LOGGER.info(
                            "TCP pre-check: %s LAN unreachable — skipping LOCAL, using REMOTE",
                            cam_id[:8],
                        )
                        candidates = ["REMOTE"]
                        self._stream_fell_back[cam_id] = True

            # A 401 on PUT /connection means the bearer token was rotated /
            # early-invalidated. _ensure_valid_token() is built for exactly this
            # ("Called ONLY when we get a 401"); refresh once per call and retry
            # the PUT rather than silently failing the whole connection.
            token_refreshed = False
            for type_val in candidates:
                # Reset quality params for each candidate — LOCAL override
                # must not leak into the REMOTE fallback.
                hq, inst = self.get_quality_params(cam_id)
                if type_val == "LOCAL" and self.get_quality(cam_id) == "auto":
                    # LOCAL: default to best quality (no bandwidth limit on LAN)
                    hq, inst = True, 1
                elif type_val == "REMOTE" and inst == 4:
                    # REMOTE proxy doesn't support inst=4 (returns 400).
                    # Fall back to inst=2 (balanced, ~7.5 Mbps).
                    inst = 2
                try:
                    # Timeout covers only the HTTP call — pre-warm runs after.
                    async with asyncio.timeout(TIMEOUT_PUT_CONNECTION):
                        resp = await session.put(
                            url,
                            json={"type": type_val, "highQualityVideo": hq},
                            headers=headers,
                        )
                        body = await resp.text()
                        # Recoverable token expiry: refresh once and retry the
                        # same candidate's PUT in-place (covered by this timeout).
                        if resp.status == 401 and not token_refreshed:
                            token_refreshed = True
                            try:
                                token = await self._ensure_valid_token(token)
                            except Exception as err:
                                _LOGGER.warning(
                                    "try_live_connection: token refresh after 401 "
                                    "failed for %s: %s",
                                    cam_id,
                                    err,
                                )
                            else:
                                headers["Authorization"] = f"Bearer {token}"
                                _LOGGER.info(
                                    "try_live_connection: token expired (401) for "
                                    "%s — refreshed, retrying PUT (%s)",
                                    cam_id,
                                    type_val,
                                )
                                resp = await session.put(
                                    url,
                                    json={
                                        "type": type_val,
                                        "highQualityVideo": hq,
                                    },
                                    headers=headers,
                                )
                                body = await resp.text()
                    _LOGGER.debug(
                        "PUT /connection type=%s → HTTP %d (%d bytes)",
                        type_val,
                        resp.status,
                        len(body),
                    )
                    if resp.status in (200, 201):
                        import json as _json

                        result: dict[str, Any] = _json.loads(body)
                        _LOGGER.info(
                            "Live connection opened! type=%s → %s",
                            type_val,
                            _redact_creds(result),
                        )
                        # Always request audio — switch.<cam>_audio is a synced
                        # card-side mute now, not a stream-track toggle (see the
                        # cred-rotation site above). 2026-06-01.
                        audio_param = "&enableaudio=1"
                        # Extract bufferingTime for FFmpeg tuning (LOCAL=500ms, REMOTE=1000ms)
                        buffering_ms = result.get("bufferingTime", 1000)
                        result["_bufferingTime"] = buffering_ms
                        # LOCAL response: {"user": "...", "password": "...", "urls": ["192.168.x.x:443"]}
                        local_user = result.get("user", "")
                        local_pass = result.get("password", "")
                        local_rtsp_url = ""  # guard: set inside `if urls:` below; "" means pre-warm skipped
                        if type_val == "LOCAL" and local_user and local_pass:
                            result["_connection_type"] = "LOCAL"
                            result["_local_user"] = local_user
                            result["_local_password"] = local_pass
                            urls = result.get("urls", [])
                            img_scheme = result.get(
                                "imageUrlScheme", "https://{url}/snap.jpg"
                            )
                            if urls:
                                from urllib.parse import quote as _q

                                cam_addr = urls[0]  # "192.168.x.x:443"
                                # Cache LOCAL creds for cloud-outage fallback paths.
                                # Stays populated after the live connection is torn down.
                                try:
                                    _host, _port = cam_addr.split(":")
                                    self._local_creds_cache[cam_id] = {
                                        "user": local_user,
                                        "password": local_pass,
                                        "host": _host,
                                        "port": int(_port),
                                        "ts": time.monotonic(),
                                    }
                                except Exception as _e:
                                    _LOGGER.debug(
                                        "LOCAL creds cache skip for %s: %s",
                                        cam_id[:8],
                                        _e,
                                    )
                                _snap_url = img_scheme.replace("{url}", cam_addr)
                                if "JpegSize=" not in _snap_url:
                                    _snap_url += (
                                        "&" if "?" in _snap_url else "?"
                                    ) + "JpegSize=1206"
                                result["proxyUrl"] = _snap_url
                                cam_host, cam_port = cam_addr.split(":")
                                proxy_port = await self._start_tls_proxy(
                                    cam_id,
                                    cam_host,
                                    int(cam_port),
                                    is_renewal=is_renewal,
                                )
                                eu = _q(local_user, safe="")
                                ep = _q(local_pass, safe="")
                                from .models import get_model_config as _gmc

                                _mcfg = _gmc(self._hw_version.get(cam_id, "CAMERA"))
                                local_rtsp_url = (
                                    f"rtsp://{eu}:{ep}@127.0.0.1:{proxy_port}"
                                    f"/rtsp_tunnel?inst={inst}{audio_param}&fmtp=1&maxSessionDuration={_mcfg.max_session_duration}"
                                )
                                # Don't set rtspsUrl yet — pre-warm must complete first
                                # so stream_source() returns None until encoder is ready.
                                # rtspsUrl/rtspUrl will be set after pre-warm below.
                        else:
                            # REMOTE response: {"urls": ["proxy-NN:42090/{hash}"]}
                            urls = result.get("urls", [])
                            cloud_rtsps_url = None
                            if urls:
                                proxy_host_path = urls[0]
                                result["proxyUrl"] = (
                                    f"https://{proxy_host_path}/snap.jpg?JpegSize=1206"
                                )
                                rtsps_host_path = proxy_host_path.replace(
                                    ":42090", ":443"
                                )
                                cloud_rtsps_url = (
                                    f"rtsps://{rtsps_host_path}/rtsp_tunnel"
                                    f"?inst={inst}{audio_param}&fmtp=1&maxSessionDuration=3600"
                                )
                            elif result.get("hash"):
                                h = result["hash"]
                                ph = result.get(
                                    "proxyHost", "proxy-01.live.cbs.boschsecurity.com"
                                )
                                pp = result.get("proxyPort", 42090)
                                result["proxyUrl"] = (
                                    f"https://{ph}:{pp}/{h}/snap.jpg?JpegSize=1206"
                                )
                                cloud_rtsps_url = (
                                    f"rtsps://{ph}:443/{h}/rtsp_tunnel"
                                    f"?inst={inst}{audio_param}&fmtp=1&maxSessionDuration=3600"
                                )
                            if cloud_rtsps_url:
                                # Run a Python TLS proxy for REMOTE too, symmetric to LOCAL.
                                # Bosch Cloud serves session URLs on hosts like
                                # proxy-NN.live.cbs.boschsecurity.com but the cert SAN list
                                # only covers *.residential.connect.boschsecurity.com —
                                # go2rtc's Go RTSP client refuses with `tls: failed to
                                # verify certificate`. The proxy terminates TLS in Python
                                # (verify_mode=CERT_NONE, check_hostname=False) and re-
                                # exports as plain RTSP on 127.0.0.1:N — both FFmpeg (HLS
                                # path) and go2rtc (WebRTC path) consume without scheme
                                # tricks. Falls back to direct rtsps:// if proxy startup
                                # fails (HLS still works that way; WebRTC still cert-
                                # blocked, identical to v10.3.24 behavior).
                                try:
                                    from urllib.parse import urlparse as _up

                                    parsed = _up(cloud_rtsps_url)
                                    pq = parsed.path + (
                                        f"?{parsed.query}" if parsed.query else ""
                                    )
                                    proxy_port = await self._start_tls_proxy(
                                        cam_id,
                                        parsed.hostname or "",
                                        parsed.port or 443,
                                    )
                                    local_rtsp_url = (
                                        f"rtsp://127.0.0.1:{proxy_port}{pq}"
                                    )
                                    result["rtspsUrl"] = local_rtsp_url
                                    result["rtspUrl"] = local_rtsp_url
                                    result["_remote_origin_url"] = cloud_rtsps_url
                                    _LOGGER.debug(
                                        "REMOTE TLS proxy %s: %s → %s",
                                        cam_id[:8],
                                        parsed.hostname,
                                        local_rtsp_url[:80],
                                    )
                                except Exception as err:
                                    _LOGGER.warning(
                                        "REMOTE TLS proxy start failed for %s — falling back "
                                        "to direct rtsps:// (HLS works, WebRTC will cert-fail): %s",
                                        cam_id[:8],
                                        err,
                                    )
                                    result["rtspsUrl"] = cloud_rtsps_url
                                    result["rtspUrl"] = cloud_rtsps_url
                        self._live_connections[cam_id] = result
                        self._live_opened_at[cam_id] = time.monotonic()

                        # ── LOCAL encoder warm-up (model-specific) ────────
                        # Camera needs time after PUT /connection before the
                        # RTSP encoder produces valid H.264 frames. Timing
                        # varies by model: CAMERA_360 (indoor) ~5s, CAMERA_EYES
                        # (outdoor) ~25s. Pre-warm sends DESCRIBE until the
                        # camera responds, plus a safety buffer. The RTSP URL
                        # is withheld from stream_source() until ready.
                        if type_val == "LOCAL" and local_user and local_pass:
                            self._stream_warming.add(cam_id)
                            self._stream_warming_started[cam_id] = time.monotonic()
                            # Stop HA's existing Stream now — the PUT above
                            # just rotated creds and the TLS proxy just
                            # switched ports, so FFmpeg's cached URL is dead.
                            # Without stopping here, FFmpeg keeps retrying
                            # the stale URL during the pre-warm wait (up to
                            # min_total_wait seconds), racks up
                            # max_stream_errors, and trips the worker-error
                            # listener into a REMOTE fallback before we ever
                            # call update_source() with the new URL.
                            #
                            # Applies to BOTH renewals AND fresh user-toggles.
                            # On a fresh toggle, a stale Stream object can
                            # still be present if a previous session was torn
                            # down at the URL level but HA's internal Stream
                            # cache held on to the worker. Reusing that worker
                            # via update_source() risks serving a *different
                            # camera's* cached buffer to this entity (observed
                            # 2026-04-27: two camera cards on one dashboard
                            # showed the same video for whichever camera was
                            # toggled last — Stream-Object Reuse-Race). Always
                            # forcing a fresh Stream eliminates that class of
                            # bugs at the cost of one extra FFmpeg cold-start
                            # per stream-on (negligible — the pre-warm already
                            # dominates the activation time).
                            cam_ent = self._camera_entities.get(cam_id)
                            if cam_ent is not None:
                                stale = getattr(cam_ent, "stream", None)
                                if stale is not None:
                                    # Hard 5 s timeout: if HA's stream_worker
                                    # is stuck in a reconnect-loop against an
                                    # expired URL (e.g. a prior REMOTE session
                                    # whose relay-side cap already passed),
                                    # `stale.stop()` awaits the worker to exit
                                    # and never returns — pinning this whole
                                    # coroutine and the per-cam stream lock,
                                    # which makes every subsequent switch-ON
                                    # fail with "already in progress" until
                                    # the integration is reloaded. Force-
                                    # detach instead; HA will GC the worker.
                                    try:
                                        await asyncio.wait_for(stale.stop(), timeout=5)
                                    except TimeoutError:
                                        _LOGGER.warning(
                                            "%s: stale Stream.stop() for %s timed out — "
                                            "force-detaching",
                                            "Renewal" if is_renewal else "Fresh toggle",
                                            cam_id[:8],
                                        )
                                    except Exception as _exc:
                                        _LOGGER.debug(
                                            "%s: stale Stream.stop() for %s failed: %s",
                                            "Renewal" if is_renewal else "Fresh toggle",
                                            cam_id[:8],
                                            _exc,
                                        )
                                    cam_ent.stream = None
                                    _LOGGER.debug(
                                        "%s: invalidated stale Stream for %s before pre-warm",
                                        "Renewal" if is_renewal else "Fresh toggle",
                                        cam_id[:8],
                                    )
                            cfg = self.get_model_config(cam_id)
                            hw = self._hw_version.get(cam_id, "?")
                            put_time = time.monotonic()
                            proxy_port_val = self._tls_proxy_ports.get(cam_id)
                            if proxy_port_val:
                                _LOGGER.debug(
                                    "LOCAL pre-warm for %s (%s, hw=%s): delay=%ds, retries=%d, wait=%ds, buffer=%ds, min_total=%ds",
                                    cam_id[:8],
                                    cfg.display_name,
                                    hw,
                                    cfg.pre_warm_delay,
                                    cfg.pre_warm_retries,
                                    cfg.pre_warm_retry_wait,
                                    cfg.post_warm_buffer,
                                    cfg.min_total_wait,
                                )
                                await asyncio.sleep(cfg.pre_warm_delay)
                                prewarm_ok = await pre_warm_rtsp(
                                    proxy_port_val,
                                    local_user,
                                    local_pass,
                                    cam_addr.split(":")[0],
                                    max_attempts=cfg.pre_warm_retries,
                                    retry_wait=cfg.pre_warm_retry_wait,
                                    post_success_wait=cfg.post_warm_buffer,
                                    describe_timeout=cfg.describe_timeout,
                                    max_session_duration=cfg.max_session_duration,
                                )
                            else:
                                prewarm_ok = False
                            # If pre-warm failed AND auto mode has REMOTE as a
                            # later candidate, abandon this LOCAL attempt and
                            # fall through to the next candidate. Without this
                            # the integration would pin the user on a dead
                            # LOCAL URL (camera LAN unreachable, firewalled
                            # subnet, different VLAN, etc.) and HA's stream
                            # worker would cycle yellow→blue→yellow forever.
                            # In "local" mode there's nothing to fall back to,
                            # so keep the LOCAL URL so the user can see the
                            # actual failure mode.
                            if (
                                not prewarm_ok
                                and "REMOTE" in candidates
                                and type_val == "LOCAL"
                            ):
                                _LOGGER.warning(
                                    "LOCAL pre-warm failed for %s — camera LAN unreachable? "
                                    "Falling back to REMOTE.",
                                    cam_id[:8],
                                )
                                self._stream_warming.discard(cam_id)
                                self._stream_warming_started.pop(cam_id, None)
                                self._live_connections.pop(cam_id, None)
                                await self._stop_tls_proxy(cam_id)
                                self._stream_fell_back[cam_id] = True
                                continue  # try next candidate (REMOTE)
                            if not prewarm_ok:
                                # LOCAL-only mode: no REMOTE candidate to fall back to.
                                # Pre-warm failed (camera rejected TLS handshakes or
                                # RTSP DESCRIBE), so the URL we are about to expose
                                # won't actually stream. Clear the warm-up flag now
                                # rather than holding it through the min_total_wait
                                # sleep — otherwise the user gets locked out of the
                                # privacy toggle for ~25s on Indoor / ~100s on
                                # Outdoor while the encoder warm-up they paid for
                                # has already definitively failed. Regression
                                # 2026-05-19 (Innenbereich): TLS proxy reset 3×,
                                # warm-up held the privacy switch hostage.
                                _LOGGER.warning(
                                    "LOCAL pre-warm failed for %s without REMOTE fallback — "
                                    "clearing warm-up flag so privacy toggle stays responsive",
                                    cam_id[:8],
                                )
                                self._stream_warming.discard(cam_id)
                                self._stream_warming_started.pop(cam_id, None)
                            # Ensure minimum total time from PUT /connection.
                            # Renewals use 2/3 of this (camera encoder already warm).
                            min_wait = (
                                (cfg.min_total_wait * 2 // 3)
                                if is_renewal
                                else cfg.min_total_wait
                            )
                            elapsed = time.monotonic() - put_time
                            remaining = min_wait - elapsed
                            if remaining > 0:
                                _LOGGER.debug(
                                    "LOCAL %s: waiting %.0fs more (%.0fs elapsed, min %ds)",
                                    cam_id[:8],
                                    remaining,
                                    elapsed,
                                    cfg.min_total_wait,
                                )
                                await asyncio.sleep(remaining)
                            # Set URL — encoder should be ready now
                            result["rtspsUrl"] = local_rtsp_url
                            result["rtspUrl"] = local_rtsp_url
                            self._live_connections[cam_id] = result  # update with URL
                            self._stream_warming.discard(cam_id)

                        rtsps_url = result.get("rtspsUrl", "")

                        # ── Update HA's stream with new URL ──────────────
                        # AFTER pre-warm so FFmpeg connects to a ready encoder.
                        cam_entity = self._camera_entities.get(cam_id)
                        if cam_entity is not None and rtsps_url:
                            if (
                                hasattr(cam_entity, "stream")
                                and cam_entity.stream is not None
                            ):
                                try:
                                    cam_entity.stream.update_source(rtsps_url)
                                    # Never log the RTSPS URL or anything derived
                                    # from it — it embeds Digest credentials
                                    # (user:password@) and the proxy session-hash
                                    # token. The cam id alone identifies the event.
                                    _LOGGER.debug(
                                        "Stream.update_source() applied for %s",
                                        cam_id[:8],
                                    )
                                except Exception as err:
                                    _LOGGER.debug(
                                        "Stream.update_source() failed for %s — forcing stream rebuild: %s",
                                        cam_id[:8],
                                        err,
                                    )
                                    cam_entity.stream = None
                            else:
                                cam_entity.stream = None

                        # ── Register with go2rtc (AFTER pre-warm) ────────
                        if rtsps_url:
                            await self._register_go2rtc_stream(cam_id, rtsps_url)
                            # Synchronously push provider discovery on the cam
                            # entity NOW so `frontend_stream_types` includes
                            # WEB_RTC by the time the next state-write fires.
                            # Otherwise HA's auto-refresh runs async ~100 ms-4 s
                            # later and the card's `camera/webrtc/offer`
                            # races against it — the card sends the offer
                            # before the provider was wired, HA's
                            # `require_webrtc_support` decorator rejects with
                            # `Camera does not support WebRTC,
                            # frontend_stream_types={HLS}`, card falls to HLS
                            # for the whole session. Explicit refresh here
                            # eliminates the race.
                            cam_ent = self._camera_entities.get(cam_id)
                            if cam_ent is not None:
                                try:
                                    await cam_ent.async_refresh_providers()
                                except Exception as err:
                                    _LOGGER.debug(
                                        "post-register refresh_providers failed for %s: %s",
                                        cam_id[:8],
                                        err,
                                    )
                            # NOTE: An earlier patch (2026-05-27) auto-stopped
                            # HA's FFmpeg Stream after a successful go2rtc
                            # register to avoid double-subscription on the
                            # camera. Reverted: Lovelace cards may still use
                            # LL-HLS (not WebRTC) — killing the Stream object
                            # leaves the HLS pipeline dead, producing 404 on
                            # /api/hls/.../playlist.m3u8?_HLS_part=N requests.
                            # If double-subscription becomes a recurring
                            # problem on weak setups, gate this behind an
                            # opt-in option (e.g. "stream_mode=webrtc_only").

                        # ── LOCAL session auto-renewal ───────────────────
                        if type_val == "LOCAL" and local_user and local_pass:
                            gen = self._auto_renew_generation.get(cam_id, 0) + 1
                            self._auto_renew_generation[cam_id] = gen
                            self._replace_renewal_task(
                                cam_id, self._auto_renew_local_session(cam_id, gen)
                            )
                            # Green IT idle reaper: tears the session down once
                            # nobody consumes it (tab closed / navigated away /
                            # Cast stopped), regardless of switch state. An active
                            # viewer or recorder counts as a consumer.
                            # OPT-IN / default OFF (under development): WebRTC
                            # viewers don't reliably surface as go2rtc consumers,
                            # so the reaper can false-negative and kill a watched
                            # stream — parked until detection is reworked. Match
                            # the const.py DEFAULT_OPTIONS default (False).
                            if self._entry.options.get("enable_green_it", False):
                                self._replace_reaper_task(
                                    cam_id, self._idle_session_reaper(cam_id, gen)
                                )
                        # ── REMOTE session lifetime watchdog ─────────────
                        # The relay drops the RTSP TCP at the URL's
                        # maxSessionDuration boundary with a hard reset, which
                        # leaves HA's stream_worker in a tight reconnect loop
                        # against a dead URL until HLS times out. We schedule a
                        # clean teardown ~60s before that boundary so the switch
                        # flips OFF gracefully and the user sees a defined
                        # state instead of a buffering spinner.
                        elif type_val == "REMOTE":
                            gen = self._auto_renew_generation.get(cam_id, 0) + 1
                            self._auto_renew_generation[cam_id] = gen
                            self._replace_renewal_task(
                                cam_id, self._remote_session_terminator(cam_id, gen)
                            )
                        # Full coordinator refresh re-evaluates ALL cameras (and
                        # re-touches their go2rtc registration). On a transparent
                        # session RENEWAL nothing user-visible changes — the stream
                        # stays up, this cam was already re-registered per-cam above
                        # with its rotated creds — so skip the cross-camera refresh
                        # on renewal (the regular ≤60 s tick confirms state). A fresh
                        # toggle still refreshes so provider/state propagate now.
                        # NOTE: the per-cam _register_go2rtc_stream above is NOT
                        # skipped on renewal — LOCAL creds rotate, so go2rtc needs
                        # the new rtsps_url or the renewed stream breaks. 2026-05-29.
                        if not is_renewal:
                            self.hass.async_create_task(self.async_request_refresh())
                        self.hass.async_create_task(
                            self._check_and_recover_webrtc(cam_id)
                        )
                        # Opportunistic RCP+ state pull: refresh privacy + LED-dimmer
                        # via lokales / Cloud-Proxy RCP+ on the freshly opened session.
                        # No-op silently on failure; fallback paths (SHC + cloud
                        # /v11/video_inputs) keep their primacy.
                        self.hass.async_create_task(self._refresh_rcp_state(cam_id))
                        # ── NVR sidecar reaction ─────────────────────────
                        # The recorder follows the live-session connection
                        # type. On a fresh LOCAL session, start (or re-start
                        # with new creds) ffmpeg if user-intent is set. On a
                        # REMOTE session, stop any running recorder cleanly —
                        # LAN-only is a hard line per concept §2.
                        if self._nvr_user_intent.get(cam_id):
                            if type_val == "LOCAL":
                                self.hass.async_create_task(
                                    nvr_recorder.start_recorder(self, cam_id),
                                    name=f"bosch_nvr_start_{cam_id[:8]}",
                                )
                            elif type_val == "REMOTE" and cam_id in self._nvr_processes:
                                self.hass.async_create_task(
                                    nvr_recorder.stop_recorder(self, cam_id),
                                    name=f"bosch_nvr_stop_{cam_id[:8]}",
                                )
                        return result
                    if resp.status == 401:
                        # Still 401 after the in-place token refresh for THIS
                        # candidate. Don't abort the whole call — in AUTO mode
                        # (candidates=[LOCAL, REMOTE]) a LOCAL 401 (camera
                        # unreachable / wrong network) must still let REMOTE be
                        # tried. Fall through to the next candidate; the loop's
                        # post-amble returns None only when ALL types failed.
                        _LOGGER.warning(
                            "try_live_connection: still 401 for %s (type=%s) "
                            "after token refresh — skipping this candidate",
                            cam_id,
                            type_val,
                        )
                    else:
                        _LOGGER.warning(
                            "try_live_connection: HTTP %d for type=%s: %s",
                            resp.status,
                            type_val,
                            body[:200],
                        )
                except TimeoutError:
                    _LOGGER.warning(
                        "try_live_connection: timeout for type=%s", type_val
                    )
                except aiohttp.ClientError as err:
                    _LOGGER.warning(
                        "try_live_connection: connection error for type=%s: %s",
                        type_val,
                        err,
                    )
        finally:
            await session.close()

        _LOGGER.warning(
            "Could not open live connection for %s — all types failed", cam_id
        )
        return None

    async def _run_smb_cleanup_bg(self) -> None:
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
        self._nvr_user_intent[cam_id] = True
        if not nvr_recorder.should_record(self, cam_id, switch_on=True):
            _LOGGER.debug(
                "NVR start_recorder skipped for %s — gate closed (LOCAL=%s online=%s)",
                cam_id[:8],
                self._live_connections.get(cam_id, {}).get("_connection_type"),
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
            self._nvr_user_intent.pop(cam_id, None)
        await nvr_recorder.stop_recorder(self, cam_id)

    async def _run_nvr_cleanup_bg(self) -> None:
        """Run NVR retention purge in an executor thread (called once per day)."""
        try:
            await self.hass.async_add_executor_job(nvr_recorder.sync_nvr_cleanup, self)
        except Exception as err:
            _LOGGER.debug("NVR cleanup background task error: %s", err)

    # ── go2rtc integration ────────────────────────────────────────────────────
    async def async_fetch_live_snapshot(self, cam_id: str) -> bytes | None:
        """Open a temporary REMOTE live connection to fetch a fresh snap.jpg.

        Does NOT register the connection in _live_connections — the live stream
        switch stays OFF. Used by background image refresh so cameras always
        show a current image rather than a (possibly expired) event snapshot.

        Proxy URL caching: PUT /connection takes ~1.5s. The resulting proxy lease
        lasts ~60s. We cache urls[0] for 50s and skip PUT /connection on warm
        refreshes, reducing latency from ~3s → ~0.5s per card refresh cycle.

        Per-camera lock: concurrent callers (first-load + proactive refresh,
        Lovelace double-firing) are serialized so only one PUT /connection
        runs per camera at a time. The second caller finds the warm cache.
        """
        lock = self._snapshot_fetch_locks.get(cam_id)
        if lock is None:
            lock = asyncio.Lock()
            self._snapshot_fetch_locks[cam_id] = lock
        async with lock:
            return await self._async_fetch_live_snapshot_impl(cam_id)

    async def _async_fetch_live_snapshot_impl(self, cam_id: str) -> bytes | None:
        import json as _json

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
        # _shc_state_cache is always initialized to {} in __init__ (line 300),
        # so the old getattr() guard for AttributeError is no longer needed.
        # Previous hotfix used _camera_status_extra (wrong attr — never assigned),
        # so the privacy short-circuit never fired; fixed here.
        if self._shc_state_cache.get(cam_id, {}).get("privacy_mode"):
            return None

        # Reuse the pooled, application-lifetime Bosch cloud session instead of
        # opening a fresh TCP+TLS connection on every snapshot poll (~5–8 calls/
        # min across 4 cameras). Connection pooling removes a full TLS handshake
        # per tick. The CM does NOT close the shared session. 2026-06-18 (perf).
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

                # ── RCP 0x099e: 320×180 JPEG (Gen1 only) ──
                # Gen1 (INDOOR/OUTDOOR/CAMERA_360) returns a JPEG via the proxy RCP
                # endpoint. Gen2 (HOME_Eyes_*) responds with non-JPEG payload —
                # 0x0a88 only reports the *configured* snapshot resolution, not that
                # 0x099e delivers bytes. Skip on Gen2 to silence log noise; snap.jpg
                # works uniformly.
                # Defensive getattr — _hw_version is a real-coordinator attribute
                # set in __init__, but tests use ``SimpleNamespace`` stubs that
                # don't auto-populate dicts. Without the fallback every snapshot
                # test (~14 cases across test_init_round7, test_init_sprint_*)
                # raises AttributeError before reaching the gate logic.
                hw_gen2 = getattr(self, "_hw_version", {}).get(cam_id, "") in (
                    "HOME_Eyes_Indoor",
                    "HOME_Eyes_Outdoor",
                )
                parts = url_entry.split("/", 1)
                if len(parts) == 2 and not hw_gen2:
                    proxy_host_rcp, proxy_hash_rcp = parts[0], parts[1]
                    rcp_base = f"https://{proxy_host_rcp}/{proxy_hash_rcp}/rcp.xml"
                    try:
                        session_id = await self._get_cached_rcp_session(
                            proxy_host_rcp, proxy_hash_rcp
                        )
                        if session_id:
                            raw = await self._rcp_read(rcp_base, "0x099e", session_id)
                            if raw and raw[:2] == b"\xff\xd8":
                                _LOGGER.debug(
                                    "fetch_live_snapshot: RCP 0x099e → %d bytes (320×180 JPEG) for %s",
                                    len(raw),
                                    cam_id,
                                )
                                return raw
                            _LOGGER.debug(
                                "fetch_live_snapshot: RCP 0x099e unavailable for %s — using snap.jpg",
                                cam_id,
                            )
                    except Exception as _rcp_err:
                        _LOGGER.debug(
                            "fetch_live_snapshot: RCP error for %s: %s — using snap.jpg",
                            cam_id,
                            _rcp_err,
                        )

                proxy_url = f"https://{url_entry}/snap.jpg?JpegSize=1206"
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
                            proxy_url2 = f"https://{url_entry2}/snap.jpg?JpegSize=1206"
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
                            # F2 (2026-05-25): cross-check the camera's "privacy is on"
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
                                    self.hass.async_create_task(
                                        self.async_request_refresh()
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
        if max_per_day and (used + self._ai_in_flight) >= max_per_day:
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

    def _ai_record_call(self, cam_id: str) -> None:
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
        if self._shc_state_cache.get(cam_id, {}).get("privacy_mode"):
            return None
        if not force and not self._ai_window_allowed():
            return None
        if not force and not self._ai_rate_allowed(cam_id):
            # Reuse cached description only if not stale and not from a privacy era
            cached_entry = self.data.get(cam_id, {}).get("ai_description", {})
            cached_text: str | None = cached_entry.get("text")
            if cached_text and not self._shc_state_cache.get(cam_id, {}).get(
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
        cam_entity = getattr(self, "_camera_entities", {}).get(cam_id)
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
        self._ai_in_flight += 1
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
                    # Record the call while _ai_in_flight is still 1 so the
                    # budget counter reflects in-progress work correctly.
                    self._ai_record_call(cam_id)
        except TimeoutError:
            _LOGGER.debug("AI description timed out (20s) for %s", cam_id[:8])
        except Exception as err:
            _LOGGER.debug("AI description generate failed for %s: %s", cam_id[:8], err)
        finally:
            self._ai_in_flight -= 1
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
        lock = self._fresh_snap_locks.setdefault(cam_id, asyncio.Lock())
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
                            async with session.get(
                                img_url, headers=img_headers
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

    async def async_fetch_live_snapshot_local(self, cam_id: str) -> bytes | None:
        """Fetch a live snapshot via LOCAL connection using HTTP Digest auth.

        For cameras like CAMERA_360 whose REMOTE snap.jpg returns 401,
        this opens a LOCAL connection to get Digest credentials and fetches
        snap.jpg directly from the camera's LAN IP.

        Uses auth_utils.async_digest_request (aiohttp) for non-blocking Digest auth.
        """
        token = self.token
        if not token:
            return None
        # Same privacy short-circuit as the REMOTE fetch — the LAN snap.jpg
        # also returns 0 bytes when privacy mode is ON. _shc_state_cache is
        # always initialized to {} in __init__, no getattr guard needed.
        if self._shc_state_cache.get(cam_id, {}).get("privacy_mode"):
            return None

        connector = aiohttp.TCPConnector(
            ssl=await async_get_bosch_cloud_ssl_context(self.hass)
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"

        result = None
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
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
        snap_url = f"https://{camera_host}/snap.jpg?JpegSize=1206"

        from homeassistant.helpers.aiohttp_client import async_get_clientsession

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
        hash) based on `_live_connections[cam_id]._connection_type`. Returns the
        parsed value (int / bytes / str depending on `type_`) or None when no
        session is active or the read fails. Never raises.

        Designed for opportunistic reads — never triggers a fresh PUT /connection
        (would cred-rotate on Gen2 Outdoor and break the running stream).
        """
        live = self._live_connections.get(cam_id, {})
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
            from .local_rcp import rcp_read_local_sync

            return await self.hass.async_add_executor_job(
                rcp_read_local_sync, host, user, pwd, command, type_
            )
        if conn_type == "REMOTE":
            urls = live.get("urls", [])
            if not urls:
                return None
            # Cloud-Proxy URL form: "proxy-XX.live.cbs.boschsecurity.com:42090/{hash}"
            proxy_with_hash = urls[0]
            from .local_rcp import rcp_read_remote_sync

            return await self.hass.async_add_executor_job(
                rcp_read_remote_sync, proxy_with_hash, command, type_
            )
        return None

    async def _refresh_rcp_state(self, cam_id: str) -> None:
        """Hook fired after a successful stream start. Currently a no-op marker.

        Earlier versions (v10.4.8) read RCP `0x0d00` and `0x0c22` here and
        interpreted them as privacy-mode and LED-dimmer state. A/B testing
        proved both interpretations were wrong (0x0d00 byte[1] stayed 1
        independent of the privacy toggle, so it is NOT the mode flag), so
        the reads were removed in v10.4.9. The hook itself is kept as a
        cheap extension point for future verified RCP+ uses.
        """
        live = self._live_connections.get(cam_id, {})
        source = live.get("_connection_type", "?").lower()
        cache = self._rcp_state_cache.setdefault(cam_id, {})

        # NOTE: 0x0d00 P_OCTET (4 bytes) was previously read here as
        # "privacy_mode" via byte[1]==1, but A/B testing 2026-04-27 proved
        # this byte is NOT the privacy-mode toggle — it stays at 1 even
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

    async def _check_and_recover_webrtc(self, cam_id: str) -> None:
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
        cam_entity = self._camera_entities.get(cam_id)
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
            self._last_schemes_refresh = float(
                "-inf"
            )  # force next _ensure_go2rtc_schemes_fresh past the 600s throttle
            await self._ensure_go2rtc_schemes_fresh()
            cam_entity._invalidate_camera_capabilities_cache()
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
        # Filter on `_live_connections` for the same reason as the
        # schemes-fresh loop below: refreshing providers on an idle cam
        # triggers `stream_source()` → `try_live_connection()` and opens
        # an unwanted LOCAL session.
        for cam_id_x, cam_ent in list(self._camera_entities.items()):
            if cam_id_x not in self._live_connections:
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
        the right scheme set. Avoids the race where the card asks for
        capabilities before the post-stream watchdog had a chance to fire.

        Direct-refresh (private-API hack) instead of full config-entry reload,
        because reload was found to not actually populate the schemes set in
        time before camera state writes happen — the bundled go2rtc binary
        may not yet be answering `/api/schemes` when the new provider's
        `initialize()` runs during reload, so the fresh provider also caches
        an empty set. Calling `provider._rest_client.schemes.list()` directly
        on the existing instance bypasses the reload churn and pulls the
        current scheme list now that go2rtc is ready.
        """
        if not hasattr(self, "_last_schemes_refresh"):
            self._last_schemes_refresh = float("-inf")
        now = time.monotonic()
        if now - self._last_schemes_refresh < 600:
            return
        try:
            from homeassistant.components.camera.webrtc import DATA_WEBRTC_PROVIDERS
        except ImportError:
            return
        providers = self.hass.data.get(DATA_WEBRTC_PROVIDERS, set())
        if not providers:
            return
        self._last_schemes_refresh = now
        refreshed = False
        for provider in providers:
            if not hasattr(provider, "_rest_client") or not hasattr(
                provider, "_supported_schemes"
            ):
                continue  # not the bundled go2rtc provider
            try:
                fresh = await provider._rest_client.schemes.list()
                if fresh:
                    old_count = len(provider._supported_schemes)
                    provider._supported_schemes = fresh
                    refreshed = True
                    _LOGGER.info(
                        "webrtc-watchdog: refreshed go2rtc provider _supported_schemes "
                        "(was %d schemes, now %d)",
                        old_count,
                        len(fresh),
                    )
            except Exception as err:
                _LOGGER.debug("webrtc-watchdog: scheme-refresh failed: %s", err)
        # Push the now-fresh provider to every camera entity that has STREAM
        # in supported_features. Without this, cams that ran async_refresh_providers
        # against a stale scheme set keep `_webrtc_provider = None` cached, and
        # the next `camera/capabilities` query advertises only HLS — even though
        # the provider's schemes are now fresh. The auto-fire only triggers on
        # `supported_features & STREAM` flips, but our streams may already be up.
        if refreshed:
            from homeassistant.components.camera import CameraEntityFeature

            for cam_id_x, cam_ent in list(self._camera_entities.items()):
                # Only touch cameras that already have an active session.
                # HA Core's `async_refresh_providers` calls `stream_source()`
                # on the entity, which our implementation answers with
                # `try_live_connection()` — opening a fresh LOCAL stream on
                # idle cams the user never asked to view. Bug 2026-05-20:
                # Innenbereich woke up streaming after this loop ran on a
                # Terrasse stream-open. Guard added so the watchdog stays
                # scoped to the cam that triggered it.
                if cam_id_x not in self._live_connections:
                    continue
                try:
                    if CameraEntityFeature.STREAM in cam_ent.supported_features:
                        await cam_ent.async_refresh_providers()
                        _LOGGER.debug(
                            "webrtc-watchdog: refreshed providers on %s",
                            getattr(cam_ent, "entity_id", "?"),
                        )
                except Exception as err:
                    _LOGGER.debug(
                        "webrtc-watchdog: cam refresh-providers failed for %s: %s",
                        getattr(cam_ent, "entity_id", "?"),
                        err,
                    )

    # ── Frigate / external-recorder persistent RTSP front-doors ───────────────

    def _frigate_config(self) -> FrontDoorConfig:
        """Build the front-door config from the integration options."""
        opts = self.options
        raw_allow = str(opts.get("frigate_ip_allowlist", "") or "")
        allowlist = frozenset(p.strip() for p in raw_allow.split(",") if p.strip())
        # NOT `opts.get(..., 60) or 60` — the options-flow schema allows 0
        # (vol.Range(min=0, max=3600)) as an explicit "close immediately"
        # value (frigate_endpoint.py: "if idle_timeout > 0: ... # 0 = close
        # immediately"). `or 60` treats 0 as falsy and silently substitutes
        # the default, making that documented value unreachable — the same
        # class of bug as the previously-dead-until-v14.4.0
        # frigate_idle_timeout option (bug-hunt 2026-07-03).
        idle_timeout_opt = opts.get("frigate_idle_timeout", 60)
        idle_timeout = 60 if idle_timeout_opt is None else idle_timeout_opt
        return FrontDoorConfig(
            bind_host=str(opts.get("frigate_bind_host", "127.0.0.1")),
            ip_allowlist=allowlist,
            auth_mode=str(opts.get("frigate_auth_mode", "none")),
            token=str(opts.get("frigate_token", "") or ""),
            basic_user=str(opts.get("frigate_basic_user", "frigate") or "frigate"),
            idle_timeout=float(idle_timeout),
            max_connections=int(opts.get("frigate_max_connections", 8) or 8),
        )

    def _frigate_url_host(self, bind_host: str) -> str:
        """Host to embed in the published URL.

        - A specific interface IP (e.g. 192.168.1.50) or 127.0.0.1 is routable
          as-is and used verbatim.
        - An all-interfaces bind (0.0.0.0 / :: / empty) isn't routable, so we
          detect the HA host's primary outbound IPv4 (no packet is sent) instead.
        """
        if bind_host not in ("0.0.0.0", "::", ""):  # noqa: S104 # all-interfaces sentinels
            return bind_host
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return str(sock.getsockname()[0])
        except OSError:
            return "127.0.0.1"
        finally:
            sock.close()

    async def _frigate_resolve_inner(self, cam_id: str) -> InnerTarget | None:
        """Lazily ensure a LOCAL live session + inner TLS proxy for a recorder.

        Returns the inner proxy port + the session's rotating Digest creds, or
        None when no LOCAL session is available (e.g. REMOTE-only fallback,
        privacy mode, camera offline) so the front-door drops the client.
        Credential-free injection only works on the LOCAL path.

        Only opens a new session when there is no active LOCAL session — calling
        try_live_connection() unconditionally would issue a PUT /connection on
        Gen2 FW 9.40.25+, rotating Digest credentials and destroying the running
        TLS proxy port every time a recorder reconnects (HA#37 stream-drop loop).
        """
        live = self._live_connections.get(cam_id, {})
        if live.get("_connection_type") != "LOCAL":
            await self.try_live_connection(cam_id)
            live = self._live_connections.get(cam_id, {})
        if live.get("_connection_type") != "LOCAL":
            return None
        port = self._tls_proxy_ports.get(cam_id)
        user = live.get("_local_user")
        pwd = live.get("_local_password")
        if not (port and user and pwd):
            return None
        return InnerTarget(port=port, digest_user=str(user), digest_password=str(pwd))

    def _frigate_wanted(self, cam_id: str) -> bool:
        """True if the feature is enabled AND a High/Low switch is on for cam."""
        if not self.options.get("frigate_endpoints_enabled", False):
            return False
        return bool(
            self._frigate_high_enabled.get(cam_id)
            or self._frigate_low_enabled.get(cam_id)
        )

    def _frigate_on_idle(self, cam_id: str) -> None:
        """Front-door for cam_id has had zero recorder clients for the configured
        frigate_idle_timeout. Tear down the on-demand LOCAL session it opened —
        but ONLY if no OTHER consumer (a live card view, Cast, Mini-NVR) is still
        using it; otherwise do nothing and let the generic idle reaper handle
        teardown when everyone leaves. Runs as a background task because on_idle
        is a synchronous loop callback. (bug-hunt 2026-07-01 — wires the
        previously-dead frigate_idle_timeout option.)
        """

        async def _maybe_teardown() -> None:
            if await self._has_active_consumer(cam_id):
                return  # a live view / recording still needs the session
            live = self._live_connections.get(cam_id)
            if not live or live.get("_connection_type") != "LOCAL":
                return  # nothing LOCAL to tear down
            # Capture the generation at decision time: `_tear_down_live_stream`
            # can now block on the stream lock for the duration of a
            # concurrent rebuild, so by the time it runs, this stale "LOCAL,
            # no consumer" read may no longer describe the current session.
            gen = self._auto_renew_generation.get(cam_id, 0)
            _LOGGER.info(
                "frigate front-door %s idle for frigate_idle_timeout — tearing "
                "down on-demand LOCAL session",
                cam_id[:8],
            )
            await self._tear_down_live_stream(cam_id, expected_generation=gen)

        task = self.hass.async_create_task(
            _maybe_teardown(), f"bosch_shc_camera_frigate_idle_{cam_id[:8]}"
        )
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def async_sync_frigate_endpoint(self, cam_id: str) -> None:
        """Start or stop the front-door for a camera per current switch state."""
        wanted = self._frigate_wanted(cam_id)
        if not wanted:
            if self._frigate_runner is not None and self._frigate_runner.has_server(
                cam_id
            ):
                self._frigate_runner.stop_server(cam_id)
            return
        if self._frigate_runner is None:
            self._frigate_runner = FrontDoorRunner()
        config = self._frigate_config()
        base_port = int(self.options.get("frigate_bind_port", 0))
        if base_port > 0:
            # Fixed-port mode: compute a stable per-camera port from the sorted
            # list of ALL known cam_ids (sorted → adding a new camera doesn't
            # shift existing cameras' ports). First cam → base, second → base+1, …
            sorted_cams = sorted(self.data.keys()) if self.data else [cam_id]
            idx = sorted_cams.index(cam_id) if cam_id in sorted_cams else 0
            preferred_port = base_port + idx
            try:
                port = await self._frigate_runner.start_server(
                    cam_id,
                    config,
                    self._frigate_resolve_inner,
                    preferred_port=preferred_port,
                    on_idle=lambda: self._frigate_on_idle(cam_id),
                )
                self._frigate_sticky_port[cam_id] = port
            except OSError as err:
                _LOGGER.error(
                    "frigate front-door %s: fixed port %d unavailable (%s) — "
                    "set frigate_bind_port to 0 or pick a free port",
                    cam_id[:8],
                    preferred_port,
                    err,
                )
                return
        else:
            # Ephemeral mode: use in-session sticky port; fall back on collision.
            try:
                port = await self._frigate_runner.start_server(
                    cam_id,
                    config,
                    self._frigate_resolve_inner,
                    preferred_port=self._frigate_sticky_port.get(cam_id, 0),
                    on_idle=lambda: self._frigate_on_idle(cam_id),
                )
                self._frigate_sticky_port[cam_id] = port
            except OSError as err:
                # Sticky port taken (e.g. after a reload) — retry on an ephemeral port.
                _LOGGER.warning(
                    "frigate front-door %s: bind on sticky port failed (%s) — using ephemeral",
                    cam_id[:8],
                    err,
                )
                self._frigate_sticky_port.pop(cam_id, None)
                try:
                    port = await self._frigate_runner.start_server(
                        cam_id,
                        config,
                        self._frigate_resolve_inner,
                        on_idle=lambda: self._frigate_on_idle(cam_id),
                    )
                except OSError as err2:
                    # The first OSError assumed "port taken", but an
                    # ephemeral (port=0) bind still uses frigate_bind_host —
                    # if THAT is the problem (unbindable/nonexistent
                    # interface, bad IPv6 literal, etc.) the retry fails
                    # with the same error, previously uncaught. Since
                    # async_added_to_hass calls this on every HA restart for
                    # a RestoreEntity-restored "on" switch, a bad
                    # frigate_bind_host used to break entity setup with a
                    # traceback on every restart instead of a clear log
                    # (bug-hunt 2026-07-03).
                    _LOGGER.error(
                        "frigate front-door %s: could not bind even an "
                        "ephemeral port (%s) — check frigate_bind_host",
                        cam_id[:8],
                        err2,
                    )
                    return
                self._frigate_sticky_port[cam_id] = port
        # Sensors read the new port/state.
        self.async_update_listeners()

    def frigate_endpoint_url(self, cam_id: str, quality: str) -> str | None:
        """Published credential-free RTSP URL for a camera+quality, or None.

        None when the feature is off, the matching High/Low switch is off, or
        the front-door is not currently bound.
        """
        if not self.options.get("frigate_endpoints_enabled", False):
            return None
        enabled = (
            self._frigate_high_enabled
            if quality == QUALITY_HIGH
            else self._frigate_low_enabled
        )
        if not enabled.get(cam_id):
            return None
        if self._frigate_runner is None or not self._frigate_runner.has_server(cam_id):
            return None
        port = self._frigate_runner.port(cam_id)
        if not port:
            return None
        config = self._frigate_config()
        # Use the camera model's session duration (3600s), not the 60s default —
        # a too-low value can arm a server-side timer that kills the stream.
        msd = int(self.get_model_config(cam_id).max_session_duration)
        return build_public_url(
            self._frigate_url_host(config.bind_host),
            port,
            quality,
            config,
            max_session_duration=msd,
        )

    def async_stop_frigate_endpoints(self) -> None:
        """Tear down all front-doors (integration unload / shutdown)."""
        if self._frigate_runner is not None:
            try:
                self._frigate_runner.stop_all()
            except Exception as err:  # broad: teardown must never block unload
                _LOGGER.debug("frigate front-doors stop_all raised: %s", err)
            self._frigate_runner = None

    async def _register_go2rtc_stream(self, cam_id: str, rtsps_url: str) -> bool:
        """Register the Bosch RTSP stream in go2rtc for WebRTC support.

        go2rtc is HA's built-in RTSP→WebRTC bridge. Once registered, HA's
        camera card can display live 30fps H.264 + AAC audio via WebRTC
        (~2s latency) or HLS (~12s latency) directly from go2rtc.

        The stream is registered under the camera entity unique_id so HA's
        stream component can find it automatically.

        go2rtc API endpoints (tried in order):
        1. Unix socket (HA 2024+): /config/go2rtc.sock or /homeassistant/go2rtc.sock
        2. Port 11984 (HA 2024+ internal)
        3. Port 1984 (legacy / standalone go2rtc)
        """
        # HA's bundled go2rtc provider (homeassistant/components/go2rtc/__init__.py
        # line ~380) registers streams lazily under `camera.entity_id` when a
        # WebRTC offer or snapshot request arrives. To have our pre-registration
        # actually benefit HA's WebRTC / snapshot paths, we must use the same
        # name — otherwise we create a parallel stream go2rtc knows about but
        # HA never looks at. Falls back to the legacy internal name when the
        # camera entity hasn't been added yet (first registration race).
        cam_entity = self._camera_entities.get(cam_id)
        if cam_entity is not None and cam_entity.entity_id:
            stream_name = cam_entity.entity_id
        else:
            stream_name = f"bosch_shc_cam_{cam_id.lower()}"
        go2rtc_src = rtsps_url

        # The rtspx:// scheme skips TLS verification in go2rtc. Bosch Cloud's
        # RTSPS proxy returns a cert for *.residential.connect.boschsecurity.com
        # but serves session URLs on proxy-NN.live.cbs.boschsecurity.com hosts —
        # go2rtc's native Go RTSP client refuses the mismatch with `tls: failed
        # to verify certificate`. Without the rewrite, registration succeeds but
        # the first consumer request 500s and HA never consumes from go2rtc.
        # Default behavior since v10.3.23 (was Beta-gated v10.3.21–v10.3.22).
        # See: https://github.com/AlexxIT/go2rtc/blob/master/internal/rtsp/README.md
        if go2rtc_src.startswith("rtsps://"):
            go2rtc_src = "rtspx://" + go2rtc_src[len("rtsps://") :]

        # Try multiple go2rtc API endpoints
        endpoints = [
            "http://localhost:11984/api/streams",
            "http://localhost:1984/api/streams",
        ]
        # Also try Unix socket if available
        config_dir = self.hass.config.config_dir
        sock_path: str | None = None
        for _candidate in (
            os.path.join(config_dir, "go2rtc.sock") if config_dir else None,
            "/homeassistant/go2rtc.sock",
        ):
            if _candidate and os.path.exists(_candidate):
                sock_path = _candidate
                break

        for url in endpoints:
            try:
                async with asyncio.timeout(3):
                    connector = None
                    if sock_path and url == endpoints[0]:
                        # Try Unix socket first
                        try:
                            connector = aiohttp.UnixConnector(path=sock_path)
                        except (OSError, RuntimeError) as err:
                            _LOGGER.debug(
                                "go2rtc Unix socket connector unavailable: %s", err
                            )
                    async with aiohttp.ClientSession(connector=connector) as s:
                        put_url = (
                            url if not connector else "http://localhost/api/streams"
                        )
                        resp = await s.put(
                            put_url,
                            params={"src": go2rtc_src, "name": stream_name},
                        )
                        body = await resp.text()
                        # go2rtc bundled with HA writes the stream to its in-memory
                        # registry via URL query params, THEN tries to persist to
                        # /config/go2rtc.yaml. The YAML-persist step fails on HA
                        # (minimal go2rtc.yaml not meant for writes) and returns
                        # HTTP 400 with body `yaml: ... did not find expected key`
                        # — but the in-memory stream is registered. Verified live
                        # (go2rtc 1.9.12) + documented at
                        # https://github.com/AlexxIT/go2rtc/issues/1386.
                        is_yaml_persist_warning = (
                            resp.status == 400 and body.startswith("yaml:")
                        )
                        if resp.status in (200, 201, 204) or is_yaml_persist_warning:
                            # Verify by probing /api/streams?src=<name> — returns
                            # producers/consumers JSON when registered, 404 when
                            # not. This catches any silent mis-registration.
                            verified = False
                            try:
                                async with s.get(
                                    put_url, params={"src": stream_name}
                                ) as check_resp:
                                    if check_resp.status == 200:
                                        verified = True
                            except (TimeoutError, aiohttp.ClientError):
                                pass
                            if verified:
                                _LOGGER.info(
                                    "go2rtc stream '%s' registered via %s (HTTP %d%s)",
                                    stream_name,
                                    "unix socket" if connector else url,
                                    resp.status,
                                    ", yaml-persist warn ignored"
                                    if is_yaml_persist_warning
                                    else "",
                                )
                                return True  # verified-registered success
                            _LOGGER.debug(
                                "go2rtc PUT returned %d via %s but verify GET missed '%s' — trying next endpoint",
                                resp.status,
                                "unix socket" if connector else url,
                                stream_name,
                            )
                            continue
                        _LOGGER.debug(
                            "go2rtc stream '%s' → HTTP %d via %s (body: %s)",
                            stream_name,
                            resp.status,
                            "unix socket" if connector else url,
                            body[:80],
                        )
                        continue
            except (TimeoutError, aiohttp.ClientError, OSError):
                continue

        _LOGGER.debug(
            "go2rtc API not reachable on any endpoint — using TLS proxy + HLS"
        )
        return False

    async def _unregister_go2rtc_stream(self, cam_id: str) -> None:
        """Remove the camera stream from go2rtc when the live session ends.

        Name must match _register_go2rtc_stream — prefer camera.entity_id
        (HA's bundled go2rtc provider uses this) and fall back to the legacy
        internal name when the entity is unavailable.
        """
        cam_entity = self._camera_entities.get(cam_id)
        if cam_entity is not None and cam_entity.entity_id:
            stream_name = cam_entity.entity_id
        else:
            stream_name = f"bosch_shc_cam_{cam_id.lower()}"
        # Try same endpoints as _register_go2rtc_stream — DELETE must reach the
        # port where the stream was actually registered (11984 on HA 2024+, 1984 legacy).
        endpoints = [
            "http://localhost:11984/api/streams",
            "http://localhost:1984/api/streams",
        ]
        for url in endpoints:
            try:
                async with asyncio.timeout(3):
                    async with aiohttp.ClientSession() as s:
                        resp = await s.delete(url, params={"name": stream_name})
                        # Only a real removal (200/204) ends the loop. aiohttp
                        # does not raise on 4xx/5xx, so an unconditional break
                        # would stop on a 404 (stream registered on the OTHER
                        # port) or a 500 and never reach the endpoint where the
                        # stream actually lives — defeating the documented
                        # multi-endpoint retry and leaking a stale stream (with
                        # its dead proxy port) in go2rtc.
                        if resp.status in (200, 204):
                            _LOGGER.debug(
                                "go2rtc stream '%s' removed via %s (HTTP %d)",
                                stream_name,
                                url,
                                resp.status,
                            )
                            break
                        _LOGGER.debug(
                            "go2rtc DELETE '%s' via %s → HTTP %d — trying next endpoint",
                            stream_name,
                            url,
                            resp.status,
                        )
            except (TimeoutError, aiohttp.ClientError):
                pass  # go2rtc may not be running on this port — try next

    async def _start_tls_proxy(
        self, cam_id: str, cam_host: str, cam_port: int, is_renewal: bool = False
    ) -> int:
        """Start a local TCP→TLS proxy for a LOCAL RTSPS stream."""
        # Lazy-init SSL context in executor (blocking I/O, must not run in event loop)
        if self._tls_ssl_ctx is None:
            self._tls_ssl_ctx = await self.hass.async_add_executor_job(
                self._create_ssl_ctx
            )
        ssl_ctx: ssl.SSLContext = self._tls_ssl_ctx

        # Hop from the proxy daemon thread back to the HA event loop and
        # schedule the rebuild coroutine. The circuit breaker fires on
        # transient WiFi jitter; without this signal the stream stays dead
        # until the next heartbeat (up to 3600s for Indoor Gen2).
        def _died_callback() -> None:
            def _on_loop() -> None:
                if self.hass.is_stopping:
                    return
                t = self.hass.async_create_task(self._on_tls_proxy_died(cam_id))
                self._bg_tasks.add(t)
                t.add_done_callback(self._bg_tasks.discard)

            try:
                self.hass.loop.call_soon_threadsafe(_on_loop)
            except RuntimeError:
                pass  # event loop closed (HA shutting down)

        return start_tls_proxy(
            ssl_ctx,
            cam_id,
            cam_host,
            cam_port,
            self._tls_proxy_ports,
            is_renewal=is_renewal,
            on_proxy_died=_died_callback,
        )

    async def _on_tls_proxy_died(self, cam_id: str) -> None:
        """Auto-rebuild the LOCAL session after the TLS proxy circuit breaker fires.

        Triggered by start_tls_proxy's on_proxy_died callback when the proxy
        closes its server socket after 5 consecutive connect failures (WiFi
        jitter, brief camera reboot, Bosch FW glitch).

        Backoff: skip if another rebuild ran within _TLS_PROXY_REBUILD_MIN_INTERVAL
        seconds — prevents a storm when the new proxy also dies immediately
        because the camera is still flapping.
        """
        _TLS_PROXY_REBUILD_MIN_INTERVAL = 30.0
        _PRE_WAIT = 5.0  # give the camera a moment to actually recover

        now = time.monotonic()
        last = self._tls_proxy_rebuild_last.get(cam_id, float("-inf"))
        if (now - last) < _TLS_PROXY_REBUILD_MIN_INTERVAL:
            _LOGGER.debug(
                "TLS proxy rebuild for %s skipped — last rebuild %.0fs ago (< %.0fs)",
                cam_id[:8],
                now - last,
                _TLS_PROXY_REBUILD_MIN_INTERVAL,
            )
            return
        self._tls_proxy_rebuild_last[cam_id] = now

        await asyncio.sleep(_PRE_WAIT)

        # Re-check state AFTER the wait — user may have toggled off,
        # or another flow may have already rebuilt.
        live = self._live_connections.get(cam_id)
        if not live:
            _LOGGER.debug(
                "TLS proxy rebuild for %s skipped — stream no longer active",
                cam_id[:8],
            )
            return
        if live.get("_connection_type") != "LOCAL":
            _LOGGER.debug(
                "TLS proxy rebuild for %s skipped — active connection is %s, "
                "not LOCAL (another recovery flow owns it)",
                cam_id[:8],
                live.get("_connection_type"),
            )
            return

        _LOGGER.warning(
            "TLS proxy for %s died (circuit breaker) — rebuilding LOCAL session",
            cam_id[:8],
        )
        # force_reset clears stale state (live-session, warm-up flags) and stops
        # the dead proxy INSIDE the stream lock, so the teardown can't race a
        # concurrent renewal/heartbeat rebuild. The camera was demonstrably
        # unreachable for ~30 s, so the privacy toggle deserves to be reactive
        # again (warm-up reset) and a fresh PUT /connection runs end-to-end.
        try:
            result = await self.try_live_connection(cam_id, force_reset=True)
            if result:
                _LOGGER.info(
                    "TLS proxy rebuild for %s succeeded (%s)",
                    cam_id[:8],
                    result.get("_connection_type", "?"),
                )
            else:
                _LOGGER.warning(
                    "TLS proxy rebuild for %s returned no result — next "
                    "heartbeat/renewal will retry",
                    cam_id[:8],
                )
        except Exception as exc:
            _LOGGER.warning(
                "TLS proxy rebuild for %s failed: %s — next heartbeat/renewal will retry",
                cam_id[:8],
                exc,
            )

    @staticmethod
    def _create_ssl_ctx() -> ssl.SSLContext:
        """Create SSL context for TLS proxy (blocking — runs in executor)."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _stop_tls_proxy(self, cam_id: str) -> None:
        """Stop the TLS proxy for a camera."""
        stop_tls_proxy(cam_id, self._tls_proxy_ports)

    async def _auto_renew_local_session(self, cam_id: str, generation: int) -> None:
        """Keep LOCAL RTSP session alive via heartbeats + periodic full renewal.

        Two mechanisms, both model-specific (from CameraModelConfig):

        1. Cloud heartbeat (every cfg.heartbeat_interval seconds):
           PUT /connection LOCAL — refreshes the cloud-side credential lease.
           Lightweight, does NOT restart TLS proxy or FFmpeg.

        2. Full session renewal (every cfg.renewal_interval seconds):
           Complete session restart — new PUT /connection, new credentials,
           new TLS proxy, Stream.update_source(). Required because some cameras
           (especially outdoor CAMERA_EYES) kill the RTSP TCP connection after
           a few minutes regardless of cloud heartbeats.

        The Bosch app sends PUT /connection every ~1s as heartbeat.
        Indoor cameras are stable for 3500s+, outdoor cameras drop after 2-10 min.
        """
        cfg = self.get_model_config(cam_id)
        heartbeat_interval = cfg.heartbeat_interval
        renewal_interval = cfg.renewal_interval
        _LOGGER.debug(
            "Session keepalive started for %s (gen=%d, heartbeat=%ds, renewal=%ds)",
            cam_id[:8],
            generation,
            heartbeat_interval,
            renewal_interval,
        )
        consecutive_fails = 0
        renewal_fails = 0  # consecutive full-renewal failures (for session_stale)
        session_start = time.monotonic()
        try:
            while True:
                await asyncio.sleep(heartbeat_interval)
                # Stop if a newer generation was started (OFF→ON cycle)
                if self._auto_renew_generation.get(cam_id, 0) != generation:
                    _LOGGER.debug(
                        "Keepalive: stale gen=%d for %s — stopping",
                        generation,
                        cam_id[:8],
                    )
                    break
                # Stop if stream was turned off
                if cam_id not in self._live_connections:
                    _LOGGER.debug("Keepalive: stream off for %s — stopping", cam_id[:8])
                    break
                live = self._live_connections.get(cam_id, {})
                if live.get("_connection_type") != "LOCAL":
                    _LOGGER.debug("Keepalive: not LOCAL for %s — stopping", cam_id[:8])
                    break

                elapsed = time.monotonic() - session_start

                # ── Full session renewal (proactive, time-based) ─────────
                if elapsed >= renewal_interval:
                    _LOGGER.info(
                        "Session renewal for %s after %.0fs (interval=%ds)",
                        cam_id[:8],
                        elapsed,
                        renewal_interval,
                    )
                    try:
                        result = await self.try_live_connection(cam_id, is_renewal=True)
                        if result:
                            _LOGGER.info("Session renewed for %s", cam_id[:8])
                            renewal_fails = 0
                            if self._session_stale.get(cam_id):
                                self._session_stale[cam_id] = False
                                _LOGGER.info(
                                    "Session recovered for %s — stale flag cleared",
                                    cam_id[:8],
                                )
                        else:
                            renewal_fails += 1
                            _LOGGER.warning(
                                "Session renewal failed for %s — retrying next cycle",
                                cam_id[:8],
                            )
                            session_start = time.monotonic()  # reset to avoid spamming
                    except Exception as exc:
                        renewal_fails += 1
                        _LOGGER.warning(
                            "Session renewal error for %s: %s", cam_id[:8], exc
                        )
                        session_start = time.monotonic()
                    # Mark session stale after 3 consecutive renewal failures so
                    # entities can surface "unavailable" instead of silently
                    # showing a frozen picture.
                    if renewal_fails >= 3 and not self._session_stale.get(cam_id):
                        self._session_stale[cam_id] = True
                        _LOGGER.warning(
                            "Session renewal persistently failing for %s (%d consecutive)",
                            cam_id[:8],
                            renewal_fails,
                        )
                    # try_live_connection creates a NEW heartbeat task with new generation,
                    # so this loop will exit at the stale-gen check above.
                    continue

                # ── Lightweight cloud heartbeat ───────────────────────────
                # Bosch rotates the per-session digest creds on EVERY successful
                # PUT /connection LOCAL (verified across all captures, see
                # captures/api-findings.md §1). The original creds remain valid
                # for the active RTSP connection as long as FFmpeg keeps the
                # session alive — but a reconnect after RTSP idle (HLS consumer
                # disconnect) gets HTTP 401 because the ~14-min-old creds were
                # rotated out long ago by 28+ subsequent heartbeats.
                #
                # We parse the response, cache the new creds in the live-session
                # state, rebuild the rtspsUrl with fresh creds, and call
                # Stream.update_source(). HA's stream component changes the
                # source for the next worker restart only — the running worker
                # is not disturbed, so there is no glitch in the live view. When
                # the worker eventually restarts (idle reconnect, crash) it
                # picks up the fresh URL automatically and avoids the 401.
                try:
                    token = self.token
                    if not token:
                        continue
                    # Pooled shared session — a heartbeat fires every ~30 s per
                    # camera; a fresh TCP+TLS handshake each time was pure
                    # overhead. The CM does NOT close the shared session.
                    # 2026-06-18 (perf).
                    async with async_bosch_cloud_session_cm(self.hass) as session:
                        url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/connection"
                        async with asyncio.timeout(TIMEOUT_PUT_CONNECTION):
                            async with session.put(
                                url,
                                json={"type": "LOCAL", "highQualityVideo": True},
                                headers={
                                    "Authorization": f"Bearer {token}",
                                    "Content-Type": "application/json",
                                },
                            ) as resp:
                                resp_text = (
                                    await resp.text()
                                    if resp.status in (200, 201)
                                    else ""
                                )
                                if resp.status in (200, 201):
                                    consecutive_fails = 0
                                    self._refresh_local_creds_from_heartbeat(
                                        cam_id,
                                        resp_text,
                                        generation,
                                        elapsed,
                                    )
                                else:
                                    consecutive_fails += 1
                                    _LOGGER.warning(
                                        "Heartbeat HTTP %d for %s (fail %d)",
                                        resp.status,
                                        cam_id[:8],
                                        consecutive_fails,
                                    )
                except Exception as exc:
                    consecutive_fails += 1
                    _LOGGER.warning(
                        "Heartbeat error for %s: %s (fail %d)",
                        cam_id[:8],
                        exc,
                        consecutive_fails,
                    )

                # After 3 consecutive heartbeat failures, force immediate renewal
                if consecutive_fails >= 3:
                    _LOGGER.warning(
                        "Heartbeat: %d consecutive failures for %s — forcing renewal",
                        consecutive_fails,
                        cam_id[:8],
                    )
                    consecutive_fails = 0
                    try:
                        result = await self.try_live_connection(cam_id, is_renewal=True)
                        if result:
                            _LOGGER.info(
                                "Heartbeat: session renewed for %s", cam_id[:8]
                            )
                            renewal_fails = (
                                0  # prevent stale flag misfiring after heartbeat rescue
                            )
                        else:
                            _LOGGER.warning(
                                "Heartbeat: renewal failed for %s", cam_id[:8]
                            )
                            session_start = time.monotonic()
                    except Exception as exc:
                        _LOGGER.warning(
                            "Heartbeat: renewal error for %s: %s", cam_id[:8], exc
                        )
                        session_start = time.monotonic()
        except asyncio.CancelledError:
            _LOGGER.debug("Keepalive cancelled for %s (gen=%d)", cam_id[:8], generation)
        finally:
            self._renewal_tasks.pop(cam_id, None)
            _LOGGER.debug(
                "Keepalive loop ended for %s (gen=%d)", cam_id[:8], generation
            )

    async def _promote_to_local(self, cam_id: str) -> None:
        """Lift an active REMOTE-fallback stream onto LOCAL via a renewal.

        Triggered from the status loop when the cam's TCP-ping cache flips
        from unreachable → reachable while a stream is currently running on
        REMOTE-fallback. Calls `try_live_connection(is_renewal=True)` which
        — with `_stream_fell_back` already cleared by the caller — runs the
        AUTO candidate list (LOCAL first, REMOTE as fallback) and on a
        successful LOCAL pre-warm invokes `Stream.update_source()` so the
        live HLS session swaps from Cloud to LAN with a brief re-buffer.
        Falls back to REMOTE again on LAN failure (no harm — the stream
        keeps running, just on the original path).
        """
        try:
            live = self._live_connections.get(cam_id, {})
            if not live or live.get("_connection_type") != "REMOTE":
                return
            result = await self.try_live_connection(cam_id, is_renewal=True)
            if not result:
                _LOGGER.debug(
                    "Active LOCAL promotion: %s renewal returned None — "
                    "stream stays on REMOTE",
                    cam_id[:8],
                )
                return
            new_type = result.get("_connection_type")
            if new_type == "LOCAL":
                _LOGGER.info(
                    "Active LOCAL promotion: %s migrated REMOTE → LOCAL",
                    cam_id[:8],
                )
            else:
                _LOGGER.debug(
                    "Active LOCAL promotion: %s renewal landed on %s "
                    "(LAN attempt did not stick)",
                    cam_id[:8],
                    new_type,
                )
        except Exception as err:
            _LOGGER.warning(
                "Active LOCAL promotion failed for %s: %s",
                cam_id[:8],
                err,
            )

    async def _remote_session_terminator(self, cam_id: str, generation: int) -> None:
        """Schedule a clean teardown of a REMOTE live session before the
        relay-side lifetime cap.

        Background: when the session reaches `maxSessionDuration` the upstream
        relay drops the RTSP TCP with a hard reset. HA's stream_worker then
        enters a tight reconnect loop against the dead URL until the HLS
        consumer's read timeout fires — anywhere from 30 s (browser) to
        several minutes of buffering spinner depending on the consumer. By
        tearing the stream down ourselves shortly before the cap, the switch
        flips OFF cleanly and the user sees a defined state. A re-toggle
        starts a fresh REMOTE session at full lifetime.

        We do not auto-renew REMOTE: the relay only mints a brand-new URL on
        each PUT /connection, so renewal would force a 30+ s pre-warm window
        every ~58 min — worse UX than a clean stop.

        Generation-tracked the same way as `_auto_renew_local_session`: any
        OFF→ON cycle bumps `_auto_renew_generation`, this loop's generation
        check then exits without action.
        """
        cfg = self.get_model_config(cam_id)
        # Tear down 60 s before the URL's maxSessionDuration so the camera
        # never hits the relay-side cap; if the user has shortened the cap
        # via the model config (<=60), still give ourselves 1 s.
        delay = max(1, cfg.max_session_duration - 60)
        _LOGGER.debug(
            "REMOTE session terminator scheduled for %s (gen=%d, %ds until teardown)",
            cam_id[:8],
            generation,
            delay,
        )
        try:
            await asyncio.sleep(delay)
            # Stop if a newer generation was started (OFF→ON cycle, or a
            # subsequent LOCAL upgrade replaced the REMOTE session).
            if self._auto_renew_generation.get(cam_id, 0) != generation:
                _LOGGER.debug(
                    "REMOTE terminator: stale gen=%d for %s — skipping",
                    generation,
                    cam_id[:8],
                )
                return
            # Stop if the stream was already turned off.
            if cam_id not in self._live_connections:
                _LOGGER.debug(
                    "REMOTE terminator: stream already off for %s — skipping",
                    cam_id[:8],
                )
                return
            live = self._live_connections.get(cam_id, {})
            if live.get("_connection_type") != "REMOTE":
                _LOGGER.debug(
                    "REMOTE terminator: %s is %s now — skipping",
                    cam_id[:8],
                    live.get("_connection_type"),
                )
                return
            _LOGGER.info(
                "REMOTE session lifetime reached for %s — tearing down cleanly",
                cam_id[:8],
            )
            # Schedule teardown in its OWN task rather than awaiting it here:
            # this terminator is itself registered in `_renewal_tasks[cam_id]`
            # (`_replace_renewal_task`), and `_tear_down_live_stream`'s first
            # action pops+cancels that entry — i.e. it would cancel ITSELF
            # mid-teardown, potentially aborting cleanup after the TLS proxy
            # stops but before go2rtc unregister / `stream.stop()` run. Same
            # trap the idle reaper already avoids (see its comment above).
            self.hass.async_create_task(
                self._tear_down_live_stream(cam_id, expected_generation=generation),
                f"bosch_shc_camera_remote_terminate_{cam_id[:8]}",
            )
            self.hass.async_create_task(self.async_request_refresh())
        except asyncio.CancelledError:
            _LOGGER.debug(
                "REMOTE terminator cancelled for %s (gen=%d)",
                cam_id[:8],
                generation,
            )
        finally:
            self._renewal_tasks.pop(cam_id, None)

    # ── FCM push notifications — delegated to fcm.py ─────────────────────────
    async def _fetch_firebase_config(self) -> dict[str, Any]:
        """Fetch Firebase config (delegated to fcm.py)."""
        return await _fcm_fetch_firebase_config(self.hass)

    async def async_start_fcm_push(self) -> None:
        """Start the FCM supervisor (delegated to fcm.py)."""
        return await _fcm_async_ensure_supervisor(self)

    async def _register_fcm_with_bosch(self) -> bool:
        """Register FCM token with Bosch CBS (delegated to fcm.py)."""
        return await _fcm_register_fcm_with_bosch(self)

    async def async_stop_fcm_push(self) -> None:
        """Stop the FCM supervisor and push listener (delegated to fcm.py)."""
        return await _fcm_async_stop_supervisor(self)

    async def _async_handle_fcm_push(self) -> None:
        """Handle an FCM push (delegated to fcm.py)."""
        return await _fcm_async_handle_fcm_push(self)

    def _get_alert_services(self, type_key: str) -> list[str]:
        """Return notify services for a given alert type (delegated to fcm.py)."""
        return _fcm_get_alert_services(self, type_key)

    @staticmethod
    def _build_notify_data(
        svc: str,
        message: str,
        file_path: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Build notify service call data (delegated to fcm.py)."""
        return _fcm_build_notify_data(svc, message, file_path, title)

    async def _async_send_alert(
        self,
        cam_name: str,
        event_type: str,
        timestamp: str,
        image_url: str,
        clip_url: str = "",
        clip_status: str = "",
        event_id: str = "",
    ) -> None:
        """Send a 3-step alert (delegated to fcm.py)."""
        return await _fcm_async_send_alert(
            self,
            cam_name,
            event_type,
            timestamp,
            image_url,
            clip_url,
            clip_status,
            event_id=event_id,
        )

    async def async_mark_events_read(self, event_ids: list[str]) -> bool:
        """Mark events as read on the Bosch cloud (delegated to fcm.py)."""
        return await _fcm_async_mark_events_read(self, event_ids)

    @staticmethod
    def _write_file(path: str, data: bytes) -> None:
        """Write binary data to file (delegated to fcm.py)."""
        _fcm_write_file(path, data)

    # ── RCP protocol (Bosch Remote Configuration Protocol via cloud proxy) ──────
    def _invalidate_rcp_session(self, proxy_hash: str) -> None:
        """Drop a cached RCP session so the next call reopens the handshake.

        Call this when a downstream RCP read returns HTTP 401 (auth dropped),
        HTTP 403 (session expired), or RCP error 0x0c0d (session closed).
        Without invalidation the cache would keep serving the dead ID for
        its full 5-min TTL — readers would see None until the entry expired.
        """
        if self._rcp_session_cache.pop(proxy_hash, None) is not None:
            _LOGGER.debug("RCP session cache invalidated for %s", proxy_hash[:8])

    async def _get_cached_rcp_session(
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
            cached = self._rcp_session_cache.get(proxy_hash)
            if cached:
                session_id, expires_at = cached
                if now < expires_at:
                    return session_id
                del self._rcp_session_cache[proxy_hash]

            new_session_id: str | None = await self._rcp_session(proxy_host, proxy_hash)
            if new_session_id:
                self._rcp_session_cache[proxy_hash] = (
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
        """
        base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"
        init_payload = (
            "0x0102004000000000040000000000000000010000000000000001000000000000"
        )

        connector = aiohttp.TCPConnector(
            ssl=await async_get_bosch_cloud_ssl_context(self.hass)
        )
        try:
            async with aiohttp.ClientSession(connector=connector) as session:
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
                    _LOGGER.debug(
                        "_rcp_session: step1 error for %s: %s", proxy_host, err
                    )
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
                    _LOGGER.debug(
                        "_rcp_session: step2 error for %s: %s", proxy_host, err
                    )
                    # Session may still be valid — return it anyway

                return session_id
        finally:
            await connector.close()

    @staticmethod
    def _proxy_hash_from_rcp_base(rcp_base: str) -> str | None:
        """Extract proxy_hash from `https://host:port/{hash}/rcp.xml`."""
        parts = rcp_base.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-1] == "rcp.xml":
            return parts[-2]
        return None

    async def _rcp_read(
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
                            "_rcp_read: command=%s HTTP %d", command, resp.status
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
            _LOGGER.debug("_rcp_read: command=%s error: %s", command, err)
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

        Uses the cached LOCAL session credentials (``_local_creds_cache``) which
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
        if self._is_rcp_lan_denied(cam_id, opcode_hex):
            return None
        ip = self._get_cam_lan_ip(cam_id)
        if not ip:
            return None
        creds = self._local_creds_cache.get(cam_id)
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
                self._rcp_onvif_scopes_cache[cam_id] = scopes_dict
                _LOGGER.debug("ONVIF scopes for %s: %s", cam_id[:8], scopes_dict)
        except Exception as err:
            _LOGGER.debug(
                "ONVIF scopes fetch error for %s: %s",
                cam_id[:8],
                BoschCameraCoordinator._err_str(err),
            )

        # F6: RCP protocol versions via 0xff00 (primary) + 0xff04 (secondary)
        try:
            raw_ver = await self._fetch_rcp_lan(cam_id, "0xff00")
            if raw_ver and len(raw_ver) >= 4:
                version_str = f"{raw_ver[0]}.{raw_ver[1]}.{raw_ver[2]}.{raw_ver[3]}"
                self._rcp_version_cache[cam_id] = version_str
                _LOGGER.debug("RCP version for %s: %s", cam_id[:8], version_str)
        except Exception as err:
            _LOGGER.debug(
                "RCP version fetch error for %s: %s",
                cam_id[:8],
                BoschCameraCoordinator._err_str(err),
            )

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
          2. 'auto' (LAN streams are always forced to hq=True, inst=1 regardless)
        """
        if cam_id in self._quality_preference:
            return self._quality_preference[cam_id]
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
            return True, 1  # primary encoder, max quality (~30 Mbps)
        if q == "low":
            return False, 4  # low-bandwidth stream (~1.9 Mbps)
        return False, 2  # "auto" — iOS default, balanced (~7.5 Mbps)

    def motion_settings(self, cam_id: str) -> dict[str, Any]:
        """Return motion detection settings dict, or empty dict."""
        return self.data.get(cam_id, {}).get("motion", {})  # type: ignore[no-any-return]

    def recording_options(self, cam_id: str) -> dict[str, Any]:
        """Return recording options dict, or empty dict."""
        return self.data.get(cam_id, {}).get("recordingOptions", {})  # type: ignore[no-any-return]

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
                            token = await self._ensure_valid_token(token)
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
                                ok2 = resp2.status in (200, 204)
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
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[Any] | None:
        return await shc_mod.async_shc_request(self, method, path, body)

    async def _async_update_shc_states(self, data: dict[str, Any]) -> None:
        return await shc_mod.async_update_shc_states(self, data)

    async def async_shc_set_camera_light(self, cam_id: str, on: bool) -> bool:
        return await shc_mod.async_shc_set_camera_light(self, cam_id, on)

    async def async_cloud_set_light_component(
        self, cam_id: str, component: str, value: Any
    ) -> bool:
        return await shc_mod.async_cloud_set_light_component(
            self, cam_id, component, value
        )

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


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    # Register services at domain level — ensures they are available even when
    # the config entry is in setup_retry (e.g. token expired).
    # Without this, the Lovelace card shows "action not found" errors.
    _register_services(hass)

    # Serve the bundled card JS files via HA's static path handler.
    # cache_headers=False → no-store so browsers always revalidate.
    from pathlib import Path as _Path

    from homeassistant.components.http import StaticPathConfig as _StaticPathConfig
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

    from .const import CARD_VERSION

    _www = _Path(__file__).parent / "www"
    await hass.http.async_register_static_paths(
        [
            _StaticPathConfig(
                f"/{DOMAIN}/bosch-camera-card.js",
                str(_www / "bosch-camera-card.js"),
                False,
            ),
            _StaticPathConfig(
                f"/{DOMAIN}/bosch-camera-autoplay-fix.js",
                str(_www / "bosch-camera-autoplay-fix.js"),
                False,
            ),
        ]
    )

    async def _register_lovelace_resources() -> None:
        """Write card URLs into Lovelace resource storage (appears in UI)."""
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            _LOGGER.warning(
                "%s: Lovelace not available — card not auto-registered", DOMAIN
            )
            return
        resources = lovelace.resources
        await resources.async_load()

        # Remove legacy /local/ entries left over from pre-v10.3.19 installs.
        # Having both old and new entries causes the card to load twice, which
        # triggers a "custom element already defined" error and the older cached
        # version wins.
        # Also remove the bosch-camera-autoplay-fix.js resource (ANY path):
        # deprecated as of v13.3.0 — the watchdog it contained is a no-op now
        # (the card self-heals per-instance), and its old index-paired HLS
        # injection could disrupt the wrong camera. We stop registering it
        # (loop below) and delete any previously auto-registered entry here. The
        # static path still serves the no-op stub, so cached/manual references
        # resolve harmlessly instead of 404-ing.
        _remove_prefixes = (
            "/local/bosch-camera-card",
            "/local/bosch-camera-autoplay-fix",
            f"/{DOMAIN}/bosch-camera-autoplay-fix",
        )
        for item in list(resources.async_items()):
            if item.get("url", "").startswith(_remove_prefixes):
                await resources.async_delete_item(item["id"])
                _LOGGER.debug(
                    "%s: Removed deprecated Lovelace resource: %s", DOMAIN, item["url"]
                )

        for card_path in (f"/{DOMAIN}/bosch-camera-card.js",):
            versioned = f"{card_path}?v={CARD_VERSION}"
            existing_id = None
            already_current = False
            for item in resources.async_items():
                item_url = item.get("url", "")
                if item_url.startswith(card_path):
                    already_current = item_url == versioned
                    existing_id = item["id"]
                    break
            if already_current:
                _LOGGER.debug(
                    "%s: Lovelace resource already current: %s", DOMAIN, versioned
                )
                continue
            if existing_id:
                await resources.async_update_item(
                    existing_id, {"res_type": "module", "url": versioned}
                )
                _LOGGER.debug("%s: Updated Lovelace resource: %s", DOMAIN, versioned)
            else:
                await resources.async_create_item(
                    {"res_type": "module", "url": versioned}
                )
                _LOGGER.debug("%s: Registered Lovelace resource: %s", DOMAIN, versioned)

    if hass.is_running:
        # Integration reloaded while HA is already up
        await _register_lovelace_resources()
    else:
        from homeassistant.core import Event as _Event
        from homeassistant.core import callback as _callback

        @_callback  # type: ignore[untyped-decorator]
        def _on_ha_started(_event: _Event) -> None:
            hass.async_create_task(_register_lovelace_resources())

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)

    return True


# Regex for the v11.0.0 doubled-prefix bug. A buggy entity_id looks like
# `button.bosch_est_bosch_est_refresh_snapshot`: domain, dot, two identical
# `bosch_<slug>_` runs, then the suffix. The backreference `\2` makes the
# match require the slug to literally repeat, so single-prefix entities
# (e.g. `switch.bosch_est_live_stream`) are never touched.
_DOUBLED_PREFIX_RE = _re_mod.compile(
    r"^(button|number|select|update|binary_sensor|light)"
    r"\.bosch_([a-z0-9_]+?)_bosch_\2_(.+)$"
)


async def _migrate_doubled_prefix_entity_ids(
    hass: HomeAssistant, config_entry_id: str
) -> int:
    """Rename entity_ids carrying the v11.0.0 doubled-prefix bug.

    v11.0.0 Gold-Compliance migration added `_attr_has_entity_name = True`
    to 30+ entity classes without removing the device-name prefix from
    their `_attr_name`, so HA prepended the device name a second time and
    the buggy entity_id stuck in the registry. v12.3.0 fixes the source;
    this helper renames the surviving entries so they match what the
    corrected code now produces.

    Reported in forum 998974/15 (Andrew75, 2026-05-15).
    """
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    renamed: list[tuple[str, str]] = []

    def _cb(reg_entry: er.RegistryEntry) -> dict[str, Any] | None:
        m = _DOUBLED_PREFIX_RE.match(reg_entry.entity_id)
        if not m:
            return None
        domain_part, slug, rest = m.group(1), m.group(2), m.group(3)
        new_eid = f"{domain_part}.bosch_{slug}_{rest}"
        # Skip if the new entity_id is already taken — avoid the ValueError
        # async_update_entity would raise. Shouldn't happen in practice (the
        # old entity owned the unique_id), but guard anyway.
        if ent_reg.async_get(new_eid):
            return None
        renamed.append((reg_entry.entity_id, new_eid))
        return {"new_entity_id": new_eid}

    await er.async_migrate_entries(hass, config_entry_id, _cb)

    if renamed:
        _LOGGER.warning(
            "Migrated %d entity_id(s) with the v11.0.0 doubled-prefix bug. "
            "Update automations/scripts/Lovelace dashboards that reference: %s",
            len(renamed),
            "; ".join(f"{old} → {new}" for old, new in renamed),
        )
        examples = ", ".join(f"`{old}` → `{new}`" for old, new in renamed[:5])
        if len(renamed) > 5:
            examples += ", …"
        ir.async_create_issue(
            hass,
            DOMAIN,
            "doubled_prefix_entity_ids_migrated",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="doubled_prefix_entity_ids_migrated",
            translation_placeholders={
                "count": str(len(renamed)),
                "examples": examples,
            },
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, "doubled_prefix_entity_ids_migrated")

    return len(renamed)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to the current schema version.

    v1 → v2 (2026-05-17, v12.4.3): DEFAULT_OPTIONS['stream_connection_type']
    flipped from 'auto' to 'local'. Entries that never explicitly set the
    option silently relied on the auto default; without this migration they
    would switch to local-only on first start after upgrade and lose their
    REMOTE-fallback safety net. Persist 'auto' explicitly so existing
    installs keep their current behaviour. New installs (created after the
    bump) get 'local' via DEFAULT_OPTIONS.

    v2 → v3 (2026-05-18, v12.4.5): fcm_push_mode is now binary — 'auto' (use
    OSS FCM key with automatic polling fallback) or 'polling' (skip FCM
    entirely). Legacy 'ios' / 'android' values from earlier versions get
    coerced to 'auto'; the OSS-sanctioned Android Firebase app handles both
    platforms transparently.

    Additionally, when the mode is FCM-bound ('ios', 'android', or the legacy
    'auto' which used an iOS-first chain), fcm_credentials and
    fcm_registered_token are cleared from entry.data so that
    register_fcm_with_bosch detects a missing token and forces re-registration
    with deviceType=ANDROID against Bosch CBS. Without this clearance,
    register_fcm_with_bosch sees "token unchanged" and skips re-registration,
    leaving Bosch CBS with deviceType=IOS while the HA client registers
    platform=ANDROID at Firebase — silently breaking push routing for every
    upgrader on a legacy FCM mode.
    """
    if entry.version < 2:
        new_options = dict(entry.options)
        if "stream_connection_type" not in new_options:
            new_options["stream_connection_type"] = "auto"
            _LOGGER.info(
                "Migration v1→v2: preserved stream_connection_type=auto for entry %s",
                entry.entry_id,
            )
        hass.config_entries.async_update_entry(entry, options=new_options, version=2)
    if entry.version < 3:
        new_options = dict(entry.options)
        new_data = dict(entry.data)
        fcm_mode = new_options.get("fcm_push_mode")
        if fcm_mode in ("ios", "android"):
            new_options["fcm_push_mode"] = "auto"
        if fcm_mode in ("ios", "android", "auto"):
            # Clear stale FCM registration so register_fcm_with_bosch forces
            # re-registration with deviceType=ANDROID on next startup.
            # 'auto' in v2 used an iOS-first Bosch registration path; that token
            # is equally stale after switching to the OSS Android Firebase key.
            new_data.pop("fcm_credentials", None)
            new_data.pop("fcm_registered_token", None)
            _LOGGER.info(
                "Migration v2→v3: rewrote fcm_push_mode to 'auto' + cleared FCM "
                "creds + token for re-registration with deviceType=ANDROID for "
                "entry %s",
                entry.entry_id,
            )
        hass.config_entries.async_update_entry(
            entry, options=new_options, data=new_data, version=3
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = BoschCameraCoordinator(hass, entry)

    # Post-update feedback prompt — one-time per integration version. When the
    # user updates to a new version we file a persistent notification pointing
    # to GitHub Discussions so feedback channels are discoverable from the HA
    # UI itself, not buried in the README. Stored per-version in entry.options;
    # we only fire when the persisted "feedback_hint_version" != current.
    # Multi-lang: picks message text per `hass.config.language`; falls back to
    # English when the language isn't in the small inline dict below (we keep
    # this inline rather than in translations/ because persistent_notification
    # doesn't go through the entity-translation pipeline).
    try:
        last_hint_version = entry.options.get("feedback_hint_version", "")
        if _INTEGRATION_VERSION not in (last_hint_version, "unknown"):
            _disc_url = "https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/discussions"
            _iss_url = "https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant/issues"
            _lang_messages: dict[str, tuple[str, str]] = {
                "de": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Update auf **v{_INTEGRATION_VERSION}** abgeschlossen. "
                    f"Feedback, Fragen, Ideen? Nutze die neuen "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Bug-Reports weiter via [Issues]({_iss_url}).",
                ),
                "en": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Updated to **v{_INTEGRATION_VERSION}**. "
                    f"Feedback, questions, ideas? Use the new "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Bug reports still on [Issues]({_iss_url}).",
                ),
                "fr": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Mise à jour vers **v{_INTEGRATION_VERSION}** terminée. "
                    f"Commentaires, questions, idées ? Utilisez les nouvelles "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Rapports de bugs toujours via [Issues]({_iss_url}).",
                ),
                "es": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Actualización a **v{_INTEGRATION_VERSION}** completada. "
                    f"¿Comentarios, preguntas, ideas? Usa las nuevas "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Informes de errores siguen en [Issues]({_iss_url}).",
                ),
                "it": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Aggiornamento a **v{_INTEGRATION_VERSION}** completato. "
                    f"Feedback, domande, idee? Usa le nuove "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Segnalazioni di bug ancora su [Issues]({_iss_url}).",
                ),
                "nl": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Bijgewerkt naar **v{_INTEGRATION_VERSION}**. "
                    f"Feedback, vragen, ideeën? Gebruik de nieuwe "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Bugmeldingen nog steeds via [Issues]({_iss_url}).",
                ),
                "pl": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Aktualizacja do **v{_INTEGRATION_VERSION}** zakończona. "
                    f"Opinie, pytania, pomysły? Skorzystaj z nowych "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Zgłoszenia błędów nadal przez [Issues]({_iss_url}).",
                ),
                "pt": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Atualização para **v{_INTEGRATION_VERSION}** concluída. "
                    f"Feedback, perguntas, ideias? Use as novas "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Relatórios de bugs ainda via [Issues]({_iss_url}).",
                ),
                "ru": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Обновление до **v{_INTEGRATION_VERSION}** завершено. "
                    f"Отзывы, вопросы, идеи? Используйте новые "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Сообщения об ошибках по-прежнему в [Issues]({_iss_url}).",
                ),
                "uk": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"Оновлення до **v{_INTEGRATION_VERSION}** завершено. "
                    f"Відгуки, питання, ідеї? Використовуйте нові "
                    f"[GitHub Discussions]({_disc_url}). "
                    f"Звіти про помилки досі через [Issues]({_iss_url}).",
                ),
                "zh-Hans": (
                    f"Bosch Smart Home Camera v{_INTEGRATION_VERSION}",
                    f"已更新至 **v{_INTEGRATION_VERSION}**。"
                    f"反馈、问题、建议？请使用新的 "
                    f"[GitHub Discussions]({_disc_url})。"
                    f"错误报告请继续通过 [Issues]({_iss_url}) 提交。",
                ),
            }
            _lang_raw = (hass.config.language or "en").lower()
            # zh-CN / zh-Hans normalisation
            if _lang_raw.startswith("zh"):
                _lang_key = "zh-Hans"
            else:
                _lang_key = _lang_raw.split("-", 1)[0]
            _title, _message = _lang_messages.get(_lang_key, _lang_messages["en"])
            hass.async_create_task(
                hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "notification_id": f"{DOMAIN}_feedback_v{_INTEGRATION_VERSION}",
                        "title": _title,
                        "message": _message,
                    },
                    blocking=False,
                )
            )
            # Persist version so the prompt won't fire again until next update
            new_opts = dict(entry.options)
            new_opts["feedback_hint_version"] = _INTEGRATION_VERSION
            hass.config_entries.async_update_entry(entry, options=new_opts)
    except Exception as _fb_err:
        _LOGGER.debug("feedback-hint suppressed: %s", _fb_err)

    # Load the persistent maintenance-notification dedup key so a restart
    # mid-window does not re-fire the "Wartung läuft" alert. Stored as
    # `[link, state]`. Bug 2026-05-20: Thomas received the same active-
    # maintenance announcement ~20 times because every HA restart wiped
    # `_maintenance_notified_key` and the next coordinator tick re-fired
    # the active-state notify.
    _maint_key_store: Store[dict[str, str]] = Store(
        hass, version=1, key=f"{DOMAIN}_maint_notified"
    )
    coordinator._maint_notified_store = _maint_key_store
    _persisted_maint_key = await _maint_key_store.async_load() or None
    if isinstance(_persisted_maint_key, dict):
        _link = _persisted_maint_key.get("link")
        _state = _persisted_maint_key.get("state")
        if isinstance(_link, str) and isinstance(_state, str):
            coordinator._maintenance_notified_key = (_link, _state)
            _LOGGER.info(
                "Loaded persisted maintenance-notify dedup key: %s for %s",
                _state,
                _link[:60],
            )

    # Same problem for cloud-state-alert: `_cloud_outage_notified` lived only
    # in memory, so a restart during an outage could re-fire "Cloud nicht
    # erreichbar". Persist a tiny boolean so restarts honour the dedup.
    _cloud_alert_store: Store = Store(
        hass, version=1, key=f"{DOMAIN}_cloud_alert_state"
    )
    coordinator._cloud_alert_store = _cloud_alert_store
    _persisted_cloud_alert = await _cloud_alert_store.async_load() or {}
    if isinstance(_persisted_cloud_alert, dict):
        if _persisted_cloud_alert.get("outage_notified") is True:
            coordinator._cloud_outage_notified = True
            _LOGGER.info(
                "Loaded persisted cloud-outage-notified flag (was True at last save)",
            )

    # Load the persistent LAN-IP map (cam_id → IP) so the LAN-ping helpers
    # have something to work with on a cloud-degraded startup. Written below
    # on every successful coordinator refresh.
    _lan_ips_store: Store = Store(hass, version=1, key=f"{DOMAIN}_lan_ips")
    coordinator._lan_ips_store = _lan_ips_store
    _persisted_ips = await _lan_ips_store.async_load() or {}
    if isinstance(_persisted_ips, dict):
        for _cid, _ip in _persisted_ips.items():
            if isinstance(_cid, str) and isinstance(_ip, str):
                coordinator._rcp_lan_ip_cache[_cid.upper()] = _ip
        if _persisted_ips:
            _LOGGER.info(
                "Loaded %d persisted LAN IP(s) for cloud-degraded LAN ping",
                len(_persisted_ips),
            )

    # Load the persistent hardware-version map (cam_id → hw_version). Without
    # this, a cold start during a Bosch cloud 5xx leaves `_hw_version` empty
    # and `_is_gen2()` returns False for every camera — which in turn makes
    # the privacy / front-light switches unavailable even though the LAN
    # RCP path would work. v12.4.10 added the LAN-fallback availability gate
    # but missed this persistence; 2026-05-20 maintenance window exposed the
    # gap (cloud 503 for 30+ minutes, switches grey, no toggle).
    _hw_version_store: Store = Store(hass, version=1, key=f"{DOMAIN}_hw_versions")
    coordinator._hw_version_store = _hw_version_store
    _persisted_hw = await _hw_version_store.async_load() or {}
    if isinstance(_persisted_hw, dict):
        for _cid, _hw in _persisted_hw.items():
            if isinstance(_cid, str) and isinstance(_hw, str):
                coordinator._hw_version[_cid.upper()] = _hw
        if _persisted_hw:
            _LOGGER.info(
                "Loaded %d persisted hardware version(s) for cloud-degraded LAN fallback",
                len(_persisted_hw),
            )

    # Load persisted LOCAL Digest creds (cam_id → {user, password, host, port}).
    # Bosch cycles these creds on every PUT /connection LOCAL — typically valid
    # for the lifetime of a session, occasionally beyond. Persisting lets the
    # LAN-fallback privacy / light writes work across HA restarts during a
    # multi-hour cloud outage; without this the in-memory cache is empty on
    # cold start and every RCP write returns <err> from the camera.
    # Security note: stored in HA's .storage (same protection level as the
    # cloud bearer token). LAN-only effective scope (camera not internet-exposed).
    _creds_store: Store = Store(hass, version=1, key=f"{DOMAIN}_local_creds")
    coordinator._local_creds_store = _creds_store
    _persisted_creds = await _creds_store.async_load() or {}
    if isinstance(_persisted_creds, dict):
        _loaded_creds = 0
        for _cid, _payload in _persisted_creds.items():
            if not (isinstance(_cid, str) and isinstance(_payload, dict)):
                continue
            if "user" in _payload and "password" in _payload and "host" in _payload:
                coordinator._local_creds_cache[_cid.upper()] = {
                    "user": _payload["user"],
                    "password": _payload["password"],
                    "host": _payload["host"],
                    "port": int(_payload.get("port", 443)),
                    "ts": time.monotonic(),
                }
                _loaded_creds += 1
        if _loaded_creds:
            _LOGGER.info(
                "Loaded %d persisted LOCAL Digest cred(s) for LAN-fallback writes",
                _loaded_creds,
            )

    # Belt-and-suspenders: if the persistent store was empty (first start of
    # the integration since this feature shipped, or store cleared), back-fill
    # from the device registry. Device `model` is set by camera.py:device_info
    # to the human-readable display name from models.py. Reverse-map it back
    # to the canonical hardwareVersion string so `_is_gen2()` works.
    # Wrapped in try/except: HA test fixtures sometimes hand back a partially-
    # initialised DeviceRegistry mock; rehydrate is best-effort.
    try:
        from homeassistant.helpers import device_registry as dr

        from .models import MODELS

        _dreg = dr.async_get(hass)
        _display_to_hw: dict[str, str] = {}
        for _hw_key, _cfg in MODELS.items():
            # First key wins per display name — keeps canonical Gen2 mapping
            # ("HOME_Eyes_Outdoor") instead of the "CAMERA_OUTDOOR_GEN2" alias.
            _display_to_hw.setdefault(_cfg.display_name, _hw_key)
        for _device in dr.async_entries_for_config_entry(_dreg, entry.entry_id):
            for _domain, _cid in _device.identifiers:
                if _domain != DOMAIN:
                    continue
                if _cid.upper() in coordinator._hw_version:
                    continue  # already populated
                _hw_from_model = _display_to_hw.get(_device.model or "")
                if _hw_from_model:
                    coordinator._hw_version[_cid.upper()] = _hw_from_model
                    _LOGGER.info(
                        "Recovered hardware version for %s from device registry: %s (%s)",
                        _cid[:8],
                        _hw_from_model,
                        _device.model,
                    )
    except Exception as exc:
        _LOGGER.debug("Device-registry hw_version rehydrate skipped: %s", exc)

    # First refresh — tolerate a cloud-side 5xx so the integration can still
    # set up entities for known cameras (loaded from the entity registry)
    # and the LAN-fallback paths can take over. Before v12.4.10 the bare
    # `async_config_entry_first_refresh()` raised `ConfigEntryNotReady` on
    # any cloud failure, which left the user with no usable entities for as
    # long as Bosch was down — even though privacy / light / LAN-ping all
    # work without the cloud. Now: try once, if it fails, fall back to
    # registry-derived cam_ids; the coordinator keeps retrying in the
    # background.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady as exc:
        _LOGGER.warning(
            "Bosch cloud unreachable on startup (%s) — bringing up integration "
            "with LAN-only entities; cloud-driven data will arrive on next refresh",
            exc,
        )
        cam_ids, cam_titles = _rehydrate_cams_from_registry(hass, entry.entry_id)
        if cam_ids:
            coordinator.data = {
                cid: {
                    "info": {"title": cam_titles.get(cid, cid)},
                    "status": "UNKNOWN",
                    "events": [],
                }
                for cid in cam_ids
            }
            coordinator.last_update_success = False
            _LOGGER.info(
                "Bosch cloud-degraded startup: rehydrated %d camera(s) from entity registry: %s",
                len(cam_ids),
                ", ".join(sorted(c[:8] for c in cam_ids)),
            )
            # Kick an immediate LAN ping so the LAN-reachable sensors and
            # switch fallbacks have a useful state right away.
            hass.async_create_task(coordinator._async_outage_ping_all())
        else:
            # Truly first-time install with no registry → preserve the original
            # behaviour and bail out so HA shows the standard setup-failed UI.
            raise

    # v12.3.0 migration — rename entity_ids carrying the v11.0.0 doubled-prefix
    # bug BEFORE forwarding platforms, so entities re-attach to the renamed
    # registry entries instead of re-creating with the buggy id. No-op on
    # clean / new installs and on installs that have already been migrated.
    await _migrate_doubled_prefix_entity_ids(hass, entry.entry_id)

    # v12.4.10 migration — the first BoschLanReachableBinarySensor build
    # overrode `name()` which doubled the device-name prefix into the
    # entity_id (`binary_sensor.bosch_<X>_bosch_<X>_lan_reachable`). Delete
    # any such stale entries so platform setup re-creates them with the
    # canonical `binary_sensor.bosch_<X>_lan_reachable` slug derived from
    # the translation key. No-op on clean installs.
    from homeassistant.helpers import entity_registry as er

    _ereg = er.async_get(hass)
    _stale_lan_ids = [
        e.entity_id
        for e in er.async_entries_for_config_entry(_ereg, entry.entry_id)
        if e.entity_id.endswith("_lan_reachable")
        and e.entity_id.count("_bosch_") >= 1
        and e.entity_id.startswith("binary_sensor.bosch_")
    ]
    for _stale_id in _stale_lan_ids:
        _LOGGER.info("v12.4.10 migration: removing stale entity_id %s", _stale_id)
        _ereg.async_remove(_stale_id)

    # v12.5.1 migration — Eyes Indoor II has no controllable light hardware
    # (only IR night-vision LEDs which the camera firmware manages itself).
    # v12.5.0 mistakenly created a `BoschFrontLight` entity for Indoor II
    # plus three stale `number.*_helligkeit_*` / `*_farbtemperatur_*`
    # entities had been left in the registry from an even older codepath.
    # All four were always `unavailable`. Remove them so the dashboard
    # doesn't show greyed-out entries that can never work. Per-cam scoped:
    # only entities whose unique_id contains an Indoor II cam_id are removed.
    _indoor_ii_cam_ids: set[str] = set()
    for _cam_id, _hw in (coordinator._hw_version or {}).items():
        if _hw in ("HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"):
            _indoor_ii_cam_ids.add(_cam_id.lower())
    _orphan_uid_suffixes = (
        "_front_light_entity",  # BoschFrontLight (v12.5.0 mistake)
        "_top_led_brightness",  # BoschTopLedBrightnessNumber (Outdoor-only)
        "_bottom_led_brightness",  # BoschBottomLedBrightnessNumber (Outdoor-only)
        "_white_balance",  # BoschWhiteBalanceNumber (Outdoor-only)
    )
    _stale_indoor_ids: list[str] = []
    for _ent in er.async_entries_for_config_entry(_ereg, entry.entry_id):
        if not any(_ent.unique_id.lower().endswith(s) for s in _orphan_uid_suffixes):
            continue
        if not any(_cid in _ent.unique_id.lower() for _cid in _indoor_ii_cam_ids):
            continue
        _stale_indoor_ids.append(_ent.entity_id)
    for _stale_id in _stale_indoor_ids:
        _LOGGER.info(
            "v12.5.1 migration: removing Indoor II orphan entity %s (no light hardware)",
            _stale_id,
        )
        _ereg.async_remove(_stale_id)

    # Restore persisted daily AI budget so the cap survives restart/reload.
    await coordinator.async_load_ai_budget()

    # Quality-Scale Bronze (runtime-data): store on entry.runtime_data, not hass.data[DOMAIN].
    # HA clears runtime_data automatically on unload — no manual cleanup needed.
    entry.runtime_data = coordinator

    # Coord-independent options snapshot for _async_options_updated. Stored in
    # hass.data so the "did options change?" comparison survives the brief
    # runtime_data=None window during a reload — a data-only write (token / FCM)
    # landing in that window must not trigger a full reload. NOT cleared on
    # unload (would empty it inside the very window we protect); it is simply
    # overwritten by the next setup.
    hass.data.setdefault(OPTIONS_SNAPSHOT_KEY, {})[entry.entry_id] = get_options(entry)

    opts = get_options(entry)
    platforms = [p for p in ALL_PLATFORMS if p != "binary_sensor"]
    if opts.get("enable_binary_sensors", True):
        platforms = ["binary_sensor", *platforms]

    await hass.config_entries.async_forward_entry_setups(entry, platforms)

    # Start proactive background token refresh (5 min before JWT expiry).
    # Deliberately scheduled AFTER the awaits above succeed: arming this
    # timer earlier meant a failure in async_load_ai_budget() or
    # async_forward_entry_setups() aborted async_setup_entry with the timer
    # already live — HA never calls async_unload_entry (or fires
    # EVENT_HOMEASSISTANT_STOP, registered further below) for a setup that
    # never completed, so the handle had no cancellation path and fired
    # _proactive_refresh() later against an orphaned coordinator. Each failed
    # setup retry (HA retries on ConfigEntryNotReady) armed one more zombie
    # timer with no bound on how many could accumulate (bug-hunt 2026-07-03).
    coordinator._schedule_token_refresh()

    # Quench the camera-component log spam during stream pre-warm (idempotent).
    # See _StreamSupportNoiseFilter docstring for context.
    _install_stream_support_noise_filter()

    # Cloudflare-Tunnel HLS-buffering workaround (idempotent). Rewrites the
    # Content-Type on /api/hls/* responses so cloudflared switches to
    # streaming mode instead of buffering each segment at the edge — fixes
    # the iOS Companion App on cellular ("HLS wird geladen…" hang).
    # See cf_unbuffer.py docstring + knowledge-base/cloudflared-tunnel-hls-buffering.md
    from . import cf_unbuffer

    cf_unbuffer.register(hass)

    # Listen on HA's stream component logger for worker-error events. This
    # catches the auto-restart cycle from Stream._run_worker() — which our
    # own polling watchdog can miss when its tick lands during a brief
    # "available" window. See _StreamWorkerErrorListener for the full
    # reasoning. Only installs once per process regardless of reloads.
    stream_logger = logging.getLogger("homeassistant.components.stream")
    if not any(
        isinstance(h, _StreamWorkerErrorListener) for h in stream_logger.handlers
    ):
        listener = _StreamWorkerErrorListener(coordinator)
        stream_logger.addHandler(listener)
        coordinator._stream_log_listener = listener
    else:
        # Rebind the existing listener to the current coordinator so a
        # config reload doesn't leave it pointing at the old coordinator.
        existing = next(
            h
            for h in stream_logger.handlers
            if isinstance(h, _StreamWorkerErrorListener)
        )
        existing._coordinator = coordinator
        coordinator._stream_log_listener = existing

    # v8.0.2 migration: auto-enable front light / wallwasher / intensity entities
    # that were initially created with disabled_by=integration in earlier builds.
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    for uid_suffix in ("front_light_", "wallwasher_", "front_light_intensity_"):
        for cam_id in coordinator.data:
            uid = f"bosch_shc_{uid_suffix}{cam_id.lower()}"
            ent = ent_reg.async_get_entity_id(
                "switch" if "intensity" not in uid_suffix else "number", DOMAIN, uid
            )
            if ent:
                entry_obj = ent_reg.async_get(ent)
                if (
                    entry_obj
                    and entry_obj.disabled_by == er.RegistryEntryDisabler.INTEGRATION
                ):
                    ent_reg.async_update_entity(ent, disabled_by=None)
                    _LOGGER.info("v8.0.2 migration: enabled %s", ent)

    # Auto-setup go2rtc integration for WebRTC streaming (opt-out via options).
    # WHY the lock: if two config entries set up in parallel (e.g. after HA
    # restart with multiple accounts), both check "no go2rtc entry exists"
    # simultaneously and both fire async_init → duplicate go2rtc entries.
    # The domain-scoped asyncio.Lock serializes the check-and-create.
    # Stored on hass.data under a distinct key (not hass.data[DOMAIN]) so
    # it doesn't pollute the per-entry iteration in service handlers.
    if opts.get("enable_go2rtc", True):
        go2rtc_lock = hass.data.setdefault(f"{DOMAIN}_go2rtc_init_lock", asyncio.Lock())
        async with go2rtc_lock:
            go2rtc_entries = hass.config_entries.async_entries("go2rtc")
            if not go2rtc_entries:
                try:
                    result = await hass.config_entries.flow.async_init(
                        "go2rtc",
                        context={"source": "system"},
                        data={},
                    )
                    if result.get("type") == "create_entry":
                        _LOGGER.info(
                            "go2rtc integration auto-created for WebRTC streaming support"
                        )
                    else:
                        _LOGGER.debug(
                            "go2rtc setup result: %s", result.get("type", "unknown")
                        )
                except Exception as err:
                    _LOGGER.debug("go2rtc auto-setup skipped: %s", err)
            else:
                _LOGGER.debug(
                    "go2rtc integration already active (entry: %s)",
                    go2rtc_entries[0].entry_id,
                )

    # Reload integration when options change (e.g. scan_interval updated)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Cancel our long-running background tasks on HA shutdown. Without this
    # `async_unload_entry` does not run on HA stop (it only runs on config
    # entry unload/reload), so `_auto_renew_local_session` would still be
    # pending at HA's "final writes" shutdown stage and HA emits the
    # "was still running after final writes shutdown stage" warning plus a
    # 30 s close-event timeout. `async_listen_once` auto-unregisters after
    # firing, so there's no stale handler after a restart.
    async def _on_ha_stop(_event: Any) -> None:
        await _async_cancel_coordinator_tasks(coordinator)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_ha_stop)
    )

    # Start FCM supervisor (runs in background, non-blocking)
    if opts.get("enable_fcm_push", False):
        hass.async_create_task(coordinator.async_start_fcm_push())

    # Mini-NVR drain watcher — promotes finalized staging segments to the
    # configured storage target (local / smb / ftp). One watcher per
    # coordinator; serves all cameras. Cancelled in async_unload_entry.
    if opts.get("enable_nvr", False):
        coordinator._nvr_drain_task = hass.async_create_background_task(
            nvr_recorder._drain_staging_to_remote(coordinator),
            "bosch_nvr_drain_watcher",
        )

    # ── Webhook delivery ─────────────────────────────────────────────────────
    # Listen on all four HA event bus topics fired by the coordinator and
    # re-deliver them via HTTP POST to the user-configured URL.
    # Default OFF — both enable_webhook_delivery AND webhook_url must be set.
    _WEBHOOK_EVENT_TYPES = (
        "bosch_shc_camera_motion",
        "bosch_shc_camera_audio_alarm",
        "bosch_shc_camera_person",
        "bosch_shc_camera_intrusion",
    )

    async def _async_deliver_webhook(event: Any) -> None:
        """POST event payload to the configured webhook URL."""
        from .const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL

        cur_opts = get_options(entry)
        if not cur_opts.get(CONF_ENABLE_WEBHOOK_DELIVERY, False):
            return
        url = cur_opts.get(CONF_WEBHOOK_URL, "").strip()
        if not url:
            _LOGGER.warning(
                "Webhook delivery enabled but webhook_url is empty — skipping"
            )
            return
        # Only allow http(s) — refuse file://, gopher://, etc. that could be
        # smuggled in via the user option and abused through the shared session.
        if not url.lower().startswith(("http://", "https://")):
            _LOGGER.warning("Webhook URL rejected — only http(s) schemes are allowed")
            return
        payload: dict[str, Any] = {
            "event_type": event.event_type,
            "camera": event.data.get("camera_name", event.data.get("camera_id", "")),
            "camera_id": event.data.get("camera_id", ""),
            "timestamp": event.data.get("timestamp", ""),
            "extra": {
                k: v
                for k, v in event.data.items()
                if k not in ("camera_name", "camera_id", "timestamp")
            },
        }
        session = async_get_clientsession(hass)
        try:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.warning(
                        "Webhook POST returned HTTP %d for event %s",
                        resp.status,
                        event.event_type,
                    )
                else:
                    _LOGGER.debug(
                        "Webhook POST %s → HTTP %d", event.event_type, resp.status
                    )
        except aiohttp.ClientError as err:
            _LOGGER.error("Webhook delivery failed for %s: %s", event.event_type, err)

    for _evt_type in _WEBHOOK_EVENT_TYPES:
        entry.async_on_unload(hass.bus.async_listen(_evt_type, _async_deliver_webhook))

    # describe_snapshot service — ask HA ai_task to describe a camera snapshot
    async def handle_describe_snapshot(call: ServiceCall) -> dict[str, Any]:
        """Ask HA's ai_task to describe the current camera snapshot."""
        import datetime as _dt_mod

        camera_id: str = call.data.get("camera_id", "").strip()
        entity_id_arg: str = call.data.get("entity_id", "").strip()
        instructions: str = call.data.get("instructions", "").strip()
        ai_task_entity_arg: str = call.data.get("ai_task_entity", "").strip()

        if not camera_id and not entity_id_arg:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id or entity_id"},
            )

        loaded = list(hass.config_entries.async_loaded_entries(DOMAIN))
        if not loaded:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unexpected_error",
                translation_placeholders={
                    "action": "describe_snapshot",
                    "error": "no loaded entries",
                },
            )
        resolved_entity_id: str = ""
        resolved_cam_id: str = ""
        coord: Any = None
        cur_opts: dict[str, Any] = {}
        for entry_inst in loaded:
            _coord = entry_inst.runtime_data
            if not _coord:
                continue
            if camera_id:
                cam_entity = getattr(_coord, "_camera_entities", {}).get(camera_id)
                if cam_entity:
                    coord = _coord
                    cur_opts = get_options(entry_inst)
                    resolved_entity_id = cam_entity.entity_id
                    resolved_cam_id = camera_id
                    break
            elif entity_id_arg:
                for cid, cent in getattr(_coord, "_camera_entities", {}).items():
                    if cent.entity_id == entity_id_arg:
                        coord = _coord
                        cur_opts = get_options(entry_inst)
                        resolved_entity_id = entity_id_arg
                        resolved_cam_id = cid
                        break
                if coord:
                    break
        if coord is None:
            # Fallback to first available coordinator for options
            for _fb_entry in loaded:
                if _fb_entry.runtime_data:
                    coord = _fb_entry.runtime_data
                    cur_opts = get_options(_fb_entry)
                    break
        if coord is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="unexpected_error",
                translation_placeholders={
                    "action": "describe_snapshot",
                    "error": "no active coordinator",
                },
            )

        # Privacy guard: do not analyze a blank/privacy frame via the manual service
        if resolved_cam_id and coord._shc_state_cache.get(resolved_cam_id, {}).get(
            "privacy_mode"
        ):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="privacy_active",
            )

        if not resolved_entity_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_found",
                translation_placeholders={
                    "kind": "camera entity",
                    "id": camera_id or entity_id_arg,
                },
            )

        prompt = instructions or cur_opts.get(
            "ai_describe_prompt",
            "Du bist eine Überwachungskamera-Assistenz. Melde NUR"
            " sicherheitsrelevante Beobachtungen: Personen (auch nur teilweise"
            " sichtbar: Beine, Arme, Silhouette, Schatten), Fahrzeuge, Tiere,"
            " Pakete oder ungewöhnliche Aktivität. Beschreibe NICHT die"
            " Umgebung, Räume, Möbel, Architektur oder Bildqualität und benenne"
            " KEINE Orte. Rate nicht: Fußmatten, Teppiche, Bodenfliesen und"
            " Schatten sind kein Paket. Wenn nichts Sicherheitsrelevantes"
            " erkennbar ist, sage das kurz, z. B.: Keine"
            " sicherheitsrelevanten Beobachtungen.",
        )
        # Language resolution: per-call override → option → fallback "Deutsch"
        language: str = (
            call.data.get("language", "").strip()
            or (cur_opts.get("ai_describe_language") or "").strip()
            or "Deutsch"
        )
        # Append bilingual language directive so the model replies in the chosen
        # language regardless of its training defaults.
        full_instructions: str = f"{prompt}\n\nRespond only in {language}. Antworte ausschließlich auf {language}."
        ai_task_entity_used: str = (
            ai_task_entity_arg or (cur_opts.get("ai_task_entity") or "").strip()
        )

        ai_call_data: dict[str, Any] = {
            "task_name": "Bosch camera snapshot",
            "instructions": full_instructions,
            "attachments": [
                {
                    "media_content_id": f"media-source://camera/{resolved_entity_id}",
                    "media_content_type": "image/jpeg",
                }
            ],
        }
        if ai_task_entity_used:
            ai_call_data["entity_id"] = ai_task_entity_used

        # Count this manual call as in-flight so a concurrent AUTO describe
        # (whose budget gate reads ``used + _ai_in_flight``) sees the work and
        # does not push the daily total over the cap. Service-path itself has no
        # budget gate (manual = always allowed), but it must stay visible.
        _track_in_flight = hasattr(coord, "_ai_in_flight")
        if _track_in_flight:
            coord._ai_in_flight += 1
        try:
            async with asyncio.timeout(20):
                resp = await hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    ai_call_data,
                    blocking=True,
                    return_response=True,
                )
        except TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ai_task_unavailable",
                translation_placeholders={"error": "timed out (20s)"},
            ) from err
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ai_task_unavailable",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            if _track_in_flight:
                coord._ai_in_flight -= 1

        text: str = (
            str(resp.get("data", "")) if isinstance(resp, dict) else str(resp or "")
        ).strip()
        if not text:
            return {"description": ""}
        if resolved_cam_id:
            coord._ai_record_call(resolved_cam_id)
        generated_at = _dt_mod.datetime.now(_dt_mod.UTC).isoformat()
        if resolved_cam_id and resolved_cam_id in coord.data:
            coord.data[resolved_cam_id]["ai_description"] = {
                "text": text,
                "generated_at": generated_at,
                "ai_task_entity": ai_task_entity_used or "default",
            }
            coord.async_set_updated_data(coord.data)
        hass.bus.async_fire(
            "bosch_shc_camera_ai_description",
            {
                "camera_id": resolved_cam_id,
                "entity_id": resolved_entity_id,
                "description": text,
                "generated_at": generated_at,
            },
        )
        return {"description": text}

    # ── Auto-describe on motion (opt-in) ─────────────────────────────────────
    # _AI_MOTION_DEBOUNCE / _AI_MOTION_DEBOUNCE_SEC are module-level so the
    # debounce state survives integration reloads — see definition near the top.

    async def _async_auto_describe(event: Any) -> None:
        """Auto-call describe_snapshot on motion/person events (debounced)."""
        cam_id_evt: str = event.data.get("camera_id", "")
        now_ts = hass.loop.time()
        last = _AI_MOTION_DEBOUNCE.get(cam_id_evt, float("-inf"))
        if now_ts - last < _AI_MOTION_DEBOUNCE_SEC:
            return
        loaded_entries = list(hass.config_entries.async_loaded_entries(DOMAIN))
        if not loaded_entries:
            return
        # Resolve the correct coordinator for this camera before reading options.
        found_coord: Any = None
        for _entry in loaded_entries:
            coord_inst = _entry.runtime_data
            if coord_inst:
                cam_entity_obj = getattr(coord_inst, "_camera_entities", {}).get(
                    cam_id_evt
                )
                if cam_entity_obj:
                    found_coord = coord_inst
                    break
        if found_coord is None:
            _LOGGER.debug("auto-describe: no entity found for cam_id %s", cam_id_evt)
            return
        ai_opts = get_options(found_coord._entry)
        if not ai_opts.get("ai_describe_on_motion", False):
            return
        # Update debounce timestamp only after confirming the option is enabled —
        # writing it before the check would suppress the first real describe call
        # if the user enables the option within the debounce window.
        _AI_MOTION_DEBOUNCE[cam_id_evt] = now_ts
        try:
            await found_coord.async_generate_ai_description(cam_id_evt, force=False)
        except Exception as err:
            _LOGGER.debug("auto-describe failed for %s: %s", cam_id_evt, err)

    for _motion_evt in ("bosch_shc_camera_motion", "bosch_shc_camera_person"):
        entry.async_on_unload(hass.bus.async_listen(_motion_evt, _async_auto_describe))

    if not hass.services.has_service(DOMAIN, "describe_snapshot"):
        hass.services.async_register(
            DOMAIN,
            "describe_snapshot",
            handle_describe_snapshot,
            supports_response=SupportsResponse.OPTIONAL,
        )

    # send_event_webhook service — test/manual trigger
    # Uses live-entry iteration so the handler always reads the current options
    # even after an integration reload — no stale closure over a setup-time entry.
    async def handle_send_event_webhook(call: ServiceCall) -> None:
        """Manually fire a webhook POST for testing."""
        import datetime as _dt

        from .const import CONF_ENABLE_WEBHOOK_DELIVERY, CONF_WEBHOOK_URL

        loaded = list(hass.config_entries.async_loaded_entries(DOMAIN))
        if not loaded:
            _LOGGER.warning(
                "send_event_webhook: no loaded entries for domain %s", DOMAIN
            )
            return
        cur_opts = get_options(loaded[0])
        if not cur_opts.get(CONF_ENABLE_WEBHOOK_DELIVERY, False):
            _LOGGER.warning(
                "send_event_webhook: webhook delivery is disabled in options"
            )
            return
        url = cur_opts.get(CONF_WEBHOOK_URL, "").strip()
        if not url:
            _LOGGER.warning("send_event_webhook: webhook_url is not configured")
            return
        if not url.lower().startswith(("http://", "https://")):
            _LOGGER.warning(
                "send_event_webhook: webhook_url %r has invalid scheme — only http/https allowed",
                url[:50],
            )
            return
        event_type_val: str = call.data.get("event_type", "MOVEMENT")
        entity_id_val: str = call.data.get("entity_id", "")
        # Resolve camera name from entity_id if given
        cam_name = entity_id_val
        if entity_id_val:
            state = hass.states.get(entity_id_val)
            if state:
                cam_name = state.attributes.get("friendly_name", entity_id_val)
        payload: dict[str, Any] = {
            "event_type": event_type_val,
            "camera": cam_name,
            "camera_id": "",
            "timestamp": _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z"),
            "extra": {"source": "manual"},
        }
        session = async_get_clientsession(hass)
        try:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                _LOGGER.info("send_event_webhook: POST %s → HTTP %d", url, resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.error("send_event_webhook: POST failed: %s", err)

    if not hass.services.has_service(DOMAIN, "send_event_webhook"):
        hass.services.async_register(
            DOMAIN, "send_event_webhook", handle_send_event_webhook
        )

    return True


async def _async_cancel_coordinator_tasks(coord: "BoschCameraCoordinator") -> None:
    """Shared teardown for both config-entry unload and HA stop.

    Called from `async_unload_entry` (integration reload / removal) and from
    the `EVENT_HOMEASSISTANT_STOP` listener registered in `async_setup_entry`.
    Without the stop listener, `_auto_renew_local_session` would still be
    running at HA's "final writes" shutdown stage and trigger the
    "was still running after final writes shutdown stage" warning — because
    `async_unload_entry` is not invoked on full HA shutdown, only on entry
    unload/reload.
    """
    # async_stop_fcm_push explicitly re-raises asyncio.CancelledError (it has
    # its own awaits on FCM client shutdown). If this whole teardown
    # coroutine is cancelled (e.g. HA's shutdown deadline cancelling a slow
    # unload) while sitting on THIS specific await — the only unguarded one
    # in this function, every step below already has its own try/except —
    # the CancelledError used to propagate immediately and skip every
    # remaining cleanup step: token-refresh handle, renewal/reaper tasks,
    # remaining _bg_tasks, the NVR drain watcher, NVR recorders, live-stream
    # teardown, Frigate endpoints, and stop_all_proxies. Catch it, finish the
    # rest of the cleanup, then re-raise at the end so the cancellation still
    # ultimately surfaces to the caller (bug-hunt 2026-07-03).
    _cancelled_during_cleanup: asyncio.CancelledError | None = None
    try:
        await coord.async_stop_fcm_push()
    except asyncio.CancelledError as err:
        _cancelled_during_cleanup = err
        _LOGGER.debug("FCM stop cancelled mid-teardown — continuing remaining cleanup")
    # Cancel scheduled proactive token refresh — otherwise a reload leaves
    # a stale TimerHandle that fires against the dead coordinator.
    handle = getattr(coord, "_token_refresh_handle", None)
    if handle is not None:
        try:
            handle.cancel()
        except (AttributeError, RuntimeError) as err:
            _LOGGER.debug("Cancel of token-refresh handle raised: %s", err)
        coord._token_refresh_handle = None
    # Cancel all LOCAL session auto-renewal tasks. The task dicts also
    # register in _bg_tasks (via _replace_renewal_task), so the gather
    # below actually waits for cancellation to propagate.
    for task in coord._renewal_tasks.values():
        if not task.done():
            task.cancel()
    coord._renewal_tasks.clear()
    # Idle reaper tasks (same lifecycle as the renewal tasks above).
    for task in coord._reaper_tasks.values():
        if not task.done():
            task.cancel()
    coord._reaper_tasks.clear()
    # Cancel tracked fire-and-forget background tasks (snapshot refreshes
    # from FCM pushes, renewal tasks registered above, go2rtc registration,
    # etc.). Await them so cancellation actually propagates before HA
    # enters its own final-writes shutdown stage.
    bg = list(coord._bg_tasks)
    for t in bg:
        if not t.done():
            t.cancel()
    if bg:
        await asyncio.gather(*bg, return_exceptions=True)
    coord._bg_tasks.clear()
    # Stop the NVR drain watcher BEFORE the recorders. The watcher is a
    # long-running coroutine; cancelling it is the supported stop path.
    drain_task = getattr(coord, "_nvr_drain_task", None)
    if drain_task is not None and not drain_task.done():
        drain_task.cancel()
        try:
            await drain_task
        except (asyncio.CancelledError, Exception):  # noqa: S110 # drain_task cancelled intentionally on shutdown; any residual error is non-actionable
            pass
        coord._nvr_drain_task = None
    # Stop all NVR recorders BEFORE the TLS proxies — once the proxies are
    # gone the ffmpeg children would die anyway, but we want a clean SIGTERM
    # so the trailing MP4 moov atom is flushed and the in-progress segment
    # stays playable.
    try:
        await nvr_recorder.stop_all(coord)
    except Exception as err:
        _LOGGER.debug("NVR stop_all on unload raised: %s", err)
    # Tear down every active LOCAL/REMOTE live stream cleanly BEFORE
    # stop_all_proxies. Without this, integration reload leaves stale state
    # behind: go2rtc keeps the producer URL with the now-dead proxy port,
    # and HA's Stream object on the camera entity keeps the dead URL —
    # the browser then polls a 404 m3u8 until the user hard-refreshes the
    # card. _tear_down_live_stream handles per-cam: unregister go2rtc,
    # stop_tls_proxy, stream.stop() + cam_entity.stream = None.
    # Symptom hit 2026-05-26 after two mjpeg-test reloads back-to-back left
    # a stale `cbs-76512325@127.0.0.1:32987` Terrasse entry in go2rtc that
    # had to be cleaned manually.
    # `getattr(..., {})` keeps minimal SimpleNamespace test fixtures working —
    # they often don't populate every coordinator attribute.
    for cam_id in list(getattr(coord, "_live_connections", {}).keys()):
        teardown = getattr(coord, "_tear_down_live_stream", None)
        if teardown is None:
            break
        try:
            await teardown(cam_id)
        except Exception as err:
            _LOGGER.debug(
                "teardown live stream for %s on unload raised: %s",
                cam_id[:8],
                err,
            )
    # Stop all Frigate front-doors (closes listeners + the shared bg loop).
    stop_frigate = getattr(coord, "async_stop_frigate_endpoints", None)
    if stop_frigate is not None:
        stop_frigate()  # self-guarded — never raises
    # Stop all TLS proxies (closes server sockets, terminates threads).
    # Idempotent — _tear_down_live_stream already stopped per-cam proxies,
    # this catches anything left in the port_cache (defensive).
    stop_all_proxies(coord._tls_proxy_ports)
    # Remove the stream-worker log listener so the handler doesn't outlive
    # the coordinator and keep a reference to a dead object.
    listener = getattr(coord, "_stream_log_listener", None)
    if listener is not None:
        logging.getLogger("homeassistant.components.stream").removeHandler(listener)
        # Nullify the coordinator reference so any in-flight emit() calls
        # during the reload gap bail out early instead of accessing a dead object.
        listener._coordinator = None
        coord._stream_log_listener = None

    if _cancelled_during_cleanup is not None:
        # Cleanup finished despite the cancellation — now let it surface to
        # the caller, matching standard asyncio cancellation etiquette.
        raise _cancelled_during_cleanup


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coord = getattr(entry, "runtime_data", None)
    if coord:
        await _async_cancel_coordinator_tasks(coord)

    return bool(await hass.config_entries.async_unload_platforms(entry, ALL_PLATFORMS))


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry only when the *options* actually change.

    This listener fires on ANY config-entry update — including the frequent
    data-only writes (token refresh at L1560, plus five FCM `data=` writes in
    fcm.py). A data-only write must NEVER reload: a reload tears down every
    camera's live stream (go2rtc unregister + TLS-proxy stop). Incident
    2026-05-29: toggling privacy on one camera persisted a refreshed token, this
    listener fired while `entry.runtime_data` was briefly None, the old
    `if coord:` guard fell through straight to async_reload, and an unrelated
    camera's WebRTC source vanished from go2rtc (DESCRIBE 404 → 30 s-delayed HLS).

    The reload decision must depend ONLY on whether options changed — never on
    whether the coordinator happens to be present. The previous-options snapshot
    therefore lives in hass.data (keyed by entry_id) so it survives the
    `runtime_data is None` reload/startup window; the coordinator snapshot is a
    fallback for the first push before hass.data is populated. See
    OPTIONS_SNAPSHOT_KEY + the snapshot write in async_setup_entry.
    """
    new_opts = get_options(entry)
    prev_opts: dict[str, Any] | None = None
    snapshots = hass.data.get(OPTIONS_SNAPSHOT_KEY)
    if isinstance(snapshots, dict):
        stored = snapshots.get(entry.entry_id)
        if isinstance(stored, dict):
            prev_opts = stored
    if prev_opts is None:
        # Fallback for the first update before async_setup_entry stored the
        # hass.data snapshot (and for tests that only populate runtime_data).
        coord = getattr(entry, "runtime_data", None)
        coord_snap = (
            getattr(coord, "_options_snapshot", None) if coord is not None else None
        )
        if isinstance(coord_snap, dict):
            prev_opts = coord_snap
    if prev_opts is not None and prev_opts == new_opts:
        _LOGGER.debug(
            "Config entry updated (options unchanged — data-only write) — skipping reload"
        )
        return
    # Real options change (or previous options unknown → safest to reload).
    # Record the new options before reloading so the fresh setup compares
    # against them rather than re-reloading in a loop.
    if isinstance(snapshots, dict):
        snapshots[entry.entry_id] = new_opts
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(hass: HomeAssistant) -> None:
    """Register HA services (skip if already registered)."""

    async def handle_trigger_snapshot(call: ServiceCall) -> None:
        """Force an immediate refresh.

        Without `entity_id`: refresh all cameras (data + images). With an
        `entity_id`: refresh only that camera's image — and skip the full
        coordinator tick. The Lovelace card passes its own camera on mount /
        tab-switch / its 60 s timer, so a dashboard with N cameras would
        otherwise fire N×(all cameras) image refreshes (and N coordinator ticks)
        and hammer Bosch's ~3-concurrent-session budget. The periodic coordinator
        poll keeps event/state data fresh independently.
        """
        target = call.data.get("entity_id")
        # entity_id may arrive as a list (HA target selector) or a bare string.
        if isinstance(target, list):
            target = target[0] if target else None
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if not coord:
                continue
            if target:
                for _cam_id, cam in coord._camera_entities.items():
                    if getattr(cam, "entity_id", None) == target:
                        hass.async_create_task(
                            cam._async_trigger_image_refresh(delay=0)
                        )
                continue
            # Fire coordinator refresh in background — do NOT await it.
            # async_request_refresh() awaits the full coordinator tick which can
            # take 6-22 s; blocking here freezes the card until the tick finishes.
            hass.async_create_task(coord.async_request_refresh())
            for _cam_id, cam in coord._camera_entities.items():
                hass.async_create_task(cam._async_trigger_image_refresh(delay=0))

    async def handle_open_live_connection(call: ServiceCall) -> None:
        """Try to open a live proxy connection for a specific camera."""
        cam_id = call.data.get("camera_id", "")
        if not cam_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id"},
            )
        is_renewal = bool(call.data.get("renewal", False))
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                result = await coord.try_live_connection(cam_id, is_renewal=is_renewal)
                if result:
                    _LOGGER.info(
                        "Live connection established: %s", _redact_creds(result)
                    )
                    return
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="live_connection_failed",
            translation_placeholders={"cam_id_prefix": cam_id[:8]},
        )

    async def handle_create_rule(call: ServiceCall) -> None:
        """Create a cloud-side schedule rule for a camera."""
        cam_id = call.data.get("camera_id", "")
        if not cam_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id"},
            )
        name = call.data.get("name", "HA Rule")
        start_time = call.data.get("start_time", "00:00:00")
        end_time = call.data.get("end_time", "23:59:00")
        weekdays = call.data.get("weekdays", [0, 1, 2, 3, 4, 5, 6])
        is_active = call.data.get("is_active", True)
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                payload = {
                    "id": None,
                    "name": name,
                    "isActive": is_active,
                    "startTime": start_time,
                    "endTime": end_time,
                    "weekdays": weekdays,
                }
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                try:
                    async with asyncio.timeout(10):
                        async with session.post(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules",
                            headers=headers,
                            json=payload,
                        ) as resp:
                            if resp.status in (200, 201):
                                result = await resp.json()
                                _LOGGER.info("Rule created: %s", result)
                            else:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error",
                                    translation_placeholders={
                                        "action": "Create rule",
                                        "status": str(resp.status),
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Create rule",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_delete_rule(call: ServiceCall) -> None:
        """Delete a cloud-side schedule rule."""
        cam_id = call.data.get("camera_id", "")
        rule_id = call.data.get("rule_id", "")
        if not cam_id or not rule_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id and rule_id"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
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
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error",
                                    translation_placeholders={
                                        "action": "Delete rule",
                                        "status": str(resp.status),
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Delete rule",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_update_rule(call: ServiceCall) -> None:
        """Update a cloud-side schedule rule (activate/deactivate, change times)."""
        cam_id = call.data.get("camera_id", "")
        rule_id = call.data.get("rule_id", "")
        if not cam_id or not rule_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id and rule_id"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                # Fetch current rule from cache or API (API needs all fields for PUT)
                existing = None
                for rule in coord._rules_cache.get(cam_id, []):
                    if rule.get("id") == rule_id:
                        existing = dict(rule)
                        break
                if not existing:
                    # Fetch from API if not in cache
                    try:
                        async with asyncio.timeout(10):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules",
                                headers=headers,
                            ) as resp:
                                if resp.status == 200:
                                    rules = await resp.json()
                                    for rule in rules:
                                        if rule.get("id") == rule_id:
                                            existing = dict(rule)
                                            break
                    except Exception as err:
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key="unexpected_error",
                            translation_placeholders={
                                "action": "Fetch rules for update",
                                "error": str(err),
                            },
                        ) from err
                if not existing:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="not_found",
                        translation_placeholders={"kind": "Rule", "id": rule_id},
                    )
                # Overlay provided fields
                if "name" in call.data:
                    existing["name"] = call.data["name"]
                if "is_active" in call.data:
                    existing["isActive"] = call.data["is_active"]
                if "start_time" in call.data:
                    existing["startTime"] = call.data["start_time"]
                if "end_time" in call.data:
                    existing["endTime"] = call.data["end_time"]
                if "weekdays" in call.data:
                    existing["weekdays"] = call.data["weekdays"]
                try:
                    async with asyncio.timeout(10):
                        async with session.put(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/rules/{rule_id}",
                            headers=headers,
                            json=existing,
                        ) as resp:
                            if resp.status in (200, 201, 204):
                                _LOGGER.info("Rule %s updated", rule_id)
                                await coord.async_request_refresh()
                            else:
                                body = await resp.text()
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error_with_body",
                                    translation_placeholders={
                                        "action": "Update rule",
                                        "status": str(resp.status),
                                        "body": body[:200],
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Update rule",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_set_motion_zones(call: ServiceCall) -> None:
        """Set motion detection zones for a camera (normalized coordinates 0.0–1.0)."""
        cam_id = call.data.get("camera_id", "")
        zones = call.data.get("zones", [])
        if not cam_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id"},
            )
        if not isinstance(zones, list):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_must_be_list",
                translation_placeholders={"argument": "zones"},
            )
        # Validate zone coordinates
        for i, z in enumerate(zones):
            for key in ("x", "y", "w", "h"):
                if key not in z:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="missing_field",
                        translation_placeholders={
                            "kind": "zone",
                            "index": str(i),
                            "field": key,
                        },
                    )
                try:
                    val = float(z[key])
                except (TypeError, ValueError) as err:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="value_out_of_range",
                        translation_placeholders={
                            "kind": "zone",
                            "index": str(i),
                            "field": key,
                            "value": str(z[key]),
                        },
                    ) from err
                if val < 0.0 or val > 1.0:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="value_out_of_range",
                        translation_placeholders={
                            "kind": "zone",
                            "index": str(i),
                            "field": key,
                            "value": f"{val:.3f}",
                        },
                    )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                try:
                    async with asyncio.timeout(10):
                        async with session.post(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion_sensitive_areas",
                            headers=headers,
                            json=zones,
                        ) as resp:
                            if resp.status in (200, 201, 204):
                                _LOGGER.info(
                                    "Motion zones set for %s (%d zones)",
                                    cam_id[:8],
                                    len(zones),
                                )
                                await coord.async_request_refresh()
                            elif resp.status == 443:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="privacy_blocked",
                                    translation_placeholders={
                                        "action": "Set motion zones"
                                    },
                                )
                            else:
                                body = await resp.text()
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error_with_body",
                                    translation_placeholders={
                                        "action": "Set motion zones",
                                        "status": str(resp.status),
                                        "body": body[:200],
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Set motion zones",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_get_motion_zones(call: ServiceCall) -> None:
        """Read current motion detection zones and show as persistent notification."""
        cam_id = call.data.get("camera_id", "")
        if not cam_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {"Authorization": f"Bearer {coord.token}"}
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion_sensitive_areas",
                            headers=headers,
                        ) as resp:
                            if resp.status == 200:
                                zones = await resp.json()
                                if not zones:
                                    msg = "Keine Motion-Zonen konfiguriert."
                                else:
                                    lines = [f"{len(zones)} Motion-Zone(n):"]
                                    for i, z in enumerate(zones):
                                        lines.append(
                                            f"• Zone {i + 1}: x={z.get('x', 0):.3f} y={z.get('y', 0):.3f} w={z.get('w', 0):.3f} h={z.get('h', 0):.3f}"
                                        )
                                    msg = "\n".join(lines)
                                _LOGGER.info("Motion zones for %s: %s", cam_id[:8], msg)
                                await hass.services.async_call(
                                    "persistent_notification",
                                    "create",
                                    {
                                        "title": "Motion-Zonen",
                                        "message": msg,
                                        "notification_id": "bosch_motion_zones",
                                    },
                                )
                            elif resp.status == 443:
                                msg = "Motion-Zonen nicht verfügbar (HTTP 443). Mögliche Ursache: Privacy-Mode ist aktiv."
                                await hass.services.async_call(
                                    "persistent_notification",
                                    "create",
                                    {
                                        "title": "Motion-Zonen",
                                        "message": msg,
                                        "notification_id": "bosch_motion_zones",
                                    },
                                )
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="privacy_blocked",
                                    translation_placeholders={
                                        "action": "get_motion_zones"
                                    },
                                )
                            else:
                                body = await resp.text()
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error_with_body",
                                    translation_placeholders={
                                        "action": "Get motion zones",
                                        "status": str(resp.status),
                                        "body": body[:200],
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Get motion zones",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_share_camera(call: ServiceCall) -> None:
        """Share one or more cameras with a friend (time-limited)."""
        friend_id = call.data.get("friend_id", "")
        camera_ids = call.data.get("camera_ids", [])
        days = call.data.get("days", 30)
        if not friend_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "friend_id"},
            )
        if not camera_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_ids"},
            )
        if isinstance(camera_ids, str):
            camera_ids = [camera_ids]
        from datetime import datetime, timedelta

        now = datetime.now(UTC)
        end = now + timedelta(days=int(days))
        shares = [
            {
                "videoInputId": cid,
                "shareTime": {
                    "start": now.isoformat(),
                    "end": end.isoformat(),
                },
            }
            for cid in camera_ids
        ]
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                try:
                    async with asyncio.timeout(10):
                        async with session.put(
                            f"{CLOUD_API}/v11/friends/{friend_id}/share",
                            headers=headers,
                            json=shares,
                        ) as resp:
                            if resp.status in (200, 201, 204):
                                _LOGGER.info(
                                    "Shared %d camera(s) with friend %s for %d days",
                                    len(camera_ids),
                                    friend_id[:8],
                                    days,
                                )
                                await hass.services.async_call(
                                    "persistent_notification",
                                    "create",
                                    {
                                        "title": "Kamera-Freigabe",
                                        "message": f"{len(camera_ids)} Kamera(s) für {days} Tage geteilt.",
                                    },
                                )
                            else:
                                body = await resp.text()
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error_with_body",
                                    translation_placeholders={
                                        "action": "Share camera",
                                        "status": str(resp.status),
                                        "body": body[:200],
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Share camera",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_get_privacy_masks(call: ServiceCall) -> None:
        """Read current privacy masks and show as persistent notification."""
        cam_id = call.data.get("camera_id", "")
        if not cam_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {"Authorization": f"Bearer {coord.token}"}
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy_masks",
                            headers=headers,
                        ) as resp:
                            if resp.status == 200:
                                masks = await resp.json()
                                if not masks:
                                    msg = "Keine Privacy-Masken konfiguriert."
                                else:
                                    lines = [f"{len(masks)} Privacy-Maske(n):"]
                                    for i, m in enumerate(masks):
                                        lines.append(
                                            f"• Maske {i + 1}: x={m.get('x', 0):.3f} y={m.get('y', 0):.3f} w={m.get('w', 0):.3f} h={m.get('h', 0):.3f}"
                                        )
                                    msg = "\n".join(lines)
                                _LOGGER.info(
                                    "Privacy masks for %s: %s", cam_id[:8], msg
                                )
                                await hass.services.async_call(
                                    "persistent_notification",
                                    "create",
                                    {
                                        "title": "Privacy-Masken",
                                        "message": msg,
                                        "notification_id": "bosch_privacy_masks",
                                    },
                                )
                            elif resp.status == 443:
                                msg = "Privacy-Masken nicht verfügbar (HTTP 443). Mögliche Ursache: Privacy-Mode ist aktiv."
                                await hass.services.async_call(
                                    "persistent_notification",
                                    "create",
                                    {
                                        "title": "Privacy-Masken",
                                        "message": msg,
                                        "notification_id": "bosch_privacy_masks",
                                    },
                                )
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="privacy_blocked",
                                    translation_placeholders={
                                        "action": "get_privacy_masks"
                                    },
                                )
                            else:
                                body = await resp.text()
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error_with_body",
                                    translation_placeholders={
                                        "action": "Get privacy masks",
                                        "status": str(resp.status),
                                        "body": body[:200],
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Get privacy masks",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_set_privacy_masks(call: ServiceCall) -> None:
        """Set privacy mask zones for a camera (normalized coordinates 0.0–1.0)."""
        cam_id = call.data.get("camera_id", "")
        masks = call.data.get("masks", [])
        if not cam_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id"},
            )
        if not isinstance(masks, list):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_must_be_list",
                translation_placeholders={"argument": "masks"},
            )
        for i, m in enumerate(masks):
            for key in ("x", "y", "w", "h"):
                if key not in m:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="missing_field",
                        translation_placeholders={
                            "kind": "mask",
                            "index": str(i),
                            "field": key,
                        },
                    )
                try:
                    val = float(m[key])
                except (TypeError, ValueError) as err:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="value_out_of_range",
                        translation_placeholders={
                            "kind": "mask",
                            "index": str(i),
                            "field": key,
                            "value": str(m[key]),
                        },
                    ) from err
                if val < 0.0 or val > 1.0:
                    raise ServiceValidationError(
                        translation_domain=DOMAIN,
                        translation_key="value_out_of_range",
                        translation_placeholders={
                            "kind": "mask",
                            "index": str(i),
                            "field": key,
                            "value": f"{val:.3f}",
                        },
                    )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                try:
                    async with asyncio.timeout(10):
                        async with session.post(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/privacy_masks",
                            headers=headers,
                            json=masks,
                        ) as resp:
                            if resp.status in (200, 201, 204):
                                _LOGGER.info(
                                    "Privacy masks set for %s (%d masks)",
                                    cam_id[:8],
                                    len(masks),
                                )
                                await coord.async_request_refresh()
                            elif resp.status == 443:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="privacy_blocked",
                                    translation_placeholders={
                                        "action": "Set privacy masks"
                                    },
                                )
                            else:
                                body = await resp.text()
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error_with_body",
                                    translation_placeholders={
                                        "action": "Set privacy masks",
                                        "status": str(resp.status),
                                        "body": body[:200],
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Set privacy masks",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_delete_motion_zone(call: ServiceCall) -> None:
        """Delete a single motion detection zone by index."""
        cam_id = call.data.get("camera_id", "")
        zone_index = call.data.get("zone_index", -1)
        if not cam_id or zone_index < 0:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id and zone_index"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                # Fetch current zones, remove the one at index, re-POST
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion_sensitive_areas",
                            headers=headers,
                        ) as resp:
                            if resp.status != 200:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error",
                                    translation_placeholders={
                                        "action": "delete_motion_zone fetch",
                                        "status": str(resp.status),
                                    },
                                )
                            zones = await resp.json()
                    if zone_index >= len(zones):
                        raise ServiceValidationError(
                            translation_domain=DOMAIN,
                            translation_key="index_out_of_range",
                            translation_placeholders={
                                "index": str(zone_index),
                                "count": str(len(zones)),
                            },
                        )
                    removed = zones.pop(zone_index)
                    _LOGGER.info("Removing zone %d: %s", zone_index, removed)
                    async with asyncio.timeout(10):
                        async with session.post(
                            f"{CLOUD_API}/v11/video_inputs/{cam_id}/motion_sensitive_areas",
                            headers=headers,
                            json=zones,
                        ) as resp:
                            if resp.status in (200, 201, 204):
                                _LOGGER.info(
                                    "Zone %d deleted, %d zones remaining",
                                    zone_index,
                                    len(zones),
                                )
                                await coord.async_request_refresh()
                            else:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error",
                                    translation_placeholders={
                                        "action": "delete_motion_zone POST",
                                        "status": str(resp.status),
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "delete_motion_zone",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_get_lighting_schedule(call: ServiceCall) -> None:
        """Read the full lighting schedule and show as persistent notification."""
        cam_id = call.data.get("camera_id", "")
        if not cam_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                try:
                    cached = getattr(coord, "_lighting_options_cache", {}).get(cam_id)
                    if cached:
                        data = cached
                    else:
                        session = await async_get_bosch_cloud_session(hass)
                        headers = {"Authorization": f"Bearer {coord.token}"}
                        async with asyncio.timeout(10):
                            async with session.get(
                                f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_options",
                                headers=headers,
                            ) as resp:
                                if resp.status == 200:
                                    data = await resp.json()
                                else:
                                    raise HomeAssistantError(
                                        translation_domain=DOMAIN,
                                        translation_key="http_error",
                                        translation_placeholders={
                                            "action": "get_lighting_schedule",
                                            "status": str(resp.status),
                                        },
                                    )
                    sched = data.get("scheduleStatus", "?")
                    on_time = data.get("generalLightOnTime", "?")
                    off_time = data.get("generalLightOffTime", "?")
                    threshold = data.get("darknessThreshold", "?")
                    motion = data.get("lightOnMotion", False)
                    followup = data.get("lightOnMotionFollowUpTimeSeconds", 0)
                    front = data.get("frontIlluminatorInGeneralLightOn", False)
                    wall = data.get("wallwasherInGeneralLightOn", False)
                    intensity = data.get("frontIlluminatorGeneralLightIntensity", 1.0)
                    msg = (
                        f"Modus: {sched}\n"
                        f"Zeitplan: {on_time} → {off_time}\n"
                        f"Dunkelheits-Schwelle: {threshold}\n"
                        f"Licht bei Bewegung: {'Ja' if motion else 'Nein'} ({followup}s Nachlauf)\n"
                        f"Frontlicht: {'An' if front else 'Aus'} (Intensität: {intensity})\n"
                        f"Wallwasher: {'An' if wall else 'Aus'}"
                    )
                    _LOGGER.info("Lighting schedule for %s: %s", cam_id[:8], msg)
                    await hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Licht-Zeitplan",
                            "message": msg,
                            "notification_id": "bosch_lighting",
                        },
                    )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "get_lighting_schedule",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_rename_camera(call: ServiceCall) -> None:
        """Rename a camera via the Bosch cloud API."""
        cam_id = call.data.get("camera_id", "")
        new_name = call.data.get("new_name", "")
        if not cam_id or not new_name:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "camera_id and new_name"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                try:
                    async with asyncio.timeout(10):
                        async with session.put(
                            f"{CLOUD_API}/v11/video_inputs",
                            headers=headers,
                            json={
                                "videoInputId": cam_id,
                                "title": new_name,
                                "timeZone": hass.config.time_zone,
                            },
                        ) as resp:
                            if resp.status in (200, 201, 204):
                                _LOGGER.info(
                                    "Camera %s renamed to '%s'", cam_id[:8], new_name
                                )
                                await coord.async_request_refresh()
                            else:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error",
                                    translation_placeholders={
                                        "action": "Rename",
                                        "status": str(resp.status),
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Rename",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_invite_friend(call: ServiceCall) -> None:
        """Invite a friend for camera sharing."""
        email = call.data.get("email", "")
        if not email:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "email"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {
                    "Authorization": f"Bearer {coord.token}",
                    "Content-Type": "application/json",
                }
                try:
                    async with asyncio.timeout(10):
                        async with session.post(
                            f"{CLOUD_API}/v11/friends",
                            headers=headers,
                            json={"invitationEmail": email, "nickName": email},
                        ) as resp:
                            if resp.status in (200, 201):
                                data = await resp.json()
                                email_domain = (
                                    email.split("@")[-1]
                                    if "@" in email
                                    else "[no-domain]"
                                )
                                _LOGGER.info(
                                    "Friend invited: *@%s (ID: %s)",
                                    email_domain,
                                    data.get("id", "?"),
                                )
                                await hass.services.async_call(
                                    "persistent_notification",
                                    "create",
                                    {
                                        "title": "Kamera-Freigabe",
                                        "message": f"Einladung an {email} gesendet. Friend-ID: {data.get('id', '?')}",
                                    },
                                )
                            else:
                                body = await resp.text()
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error_with_body",
                                    translation_placeholders={
                                        "action": "Invite",
                                        "status": str(resp.status),
                                        "body": body[:200],
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Invite",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_list_friends(call: ServiceCall) -> None:
        """List all friends and camera shares."""
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {"Authorization": f"Bearer {coord.token}"}
                try:
                    async with asyncio.timeout(10):
                        async with session.get(
                            f"{CLOUD_API}/v11/friends", headers=headers
                        ) as resp:
                            if resp.status == 200:
                                friends = await resp.json()
                                if not friends:
                                    msg = "Keine Freunde / Kamera-Freigaben."
                                else:
                                    lines = [f"{len(friends)} Freund(e):"]
                                    for f in friends:
                                        email = f.get(
                                            "email", f.get("invitationEmail", "?")
                                        )
                                        status = f.get(
                                            "status", f.get("invitationStatus", "?")
                                        )
                                        fid = f.get("id", "?")
                                        shares = f.get("sharedVideoInputs", [])
                                        lines.append(
                                            f"• {email} (Status: {status}, ID: {fid}, Kameras: {len(shares)})"
                                        )
                                    msg = "\n".join(lines)
                                _LOGGER.info("Friends: %s", msg)
                                await hass.services.async_call(
                                    "persistent_notification",
                                    "create",
                                    {
                                        "title": "Kamera-Freigaben",
                                        "message": msg,
                                        "notification_id": "bosch_friends_list",
                                    },
                                )
                            else:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error",
                                    translation_placeholders={
                                        "action": "List friends",
                                        "status": str(resp.status),
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "List friends",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_remove_friend(call: ServiceCall) -> None:
        """Remove a friend and revoke camera shares."""
        friend_id = call.data.get("friend_id", "")
        if not friend_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "friend_id"},
            )
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if coord:
                session = await async_get_bosch_cloud_session(hass)
                headers = {"Authorization": f"Bearer {coord.token}"}
                try:
                    async with asyncio.timeout(10):
                        async with session.delete(
                            f"{CLOUD_API}/v11/friends/{friend_id}", headers=headers
                        ) as resp:
                            if resp.status in (200, 201, 204):
                                _LOGGER.info("Friend %s removed", friend_id)
                            else:
                                raise HomeAssistantError(
                                    translation_domain=DOMAIN,
                                    translation_key="http_error",
                                    translation_placeholders={
                                        "action": "Remove friend",
                                        "status": str(resp.status),
                                    },
                                )
                except HomeAssistantError:
                    raise
                except Exception as err:
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key="unexpected_error",
                        translation_placeholders={
                            "action": "Remove friend",
                            "error": str(err),
                        },
                    ) from err
                break

    async def handle_migrate_flat_events(call: ServiceCall) -> None:
        """Move flat event files (camera/file) into date hierarchy (camera/year/month/day/file)."""
        import re as _re
        import shutil
        from pathlib import Path

        _PAT = _re.compile(
            r"^(?:(?P<camera>.+?)_)?(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})_"
            r"(?P<etype>[A-Z_]+)_[0-9A-F]+\.(?P<ext>jpg|jpeg|mp4)$",
            _re.IGNORECASE,
        )
        total = 0
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if not coord:
                continue
            opts = coord.options
            base_str = (opts.get("download_path") or "").strip()
            if not base_str:
                continue
            base = Path(base_str)
            if not base.is_dir():
                continue

            def _migrate(base: Path) -> int:
                moved = 0
                for cam_dir in base.iterdir():
                    if not cam_dir.is_dir():
                        continue
                    for f in list(cam_dir.iterdir()):
                        if not f.is_file():
                            continue
                        m = _PAT.match(f.name)
                        if not m:
                            continue
                        y, mo, d = m.group("date").split("-")
                        dest_dir = cam_dir / y / mo / d
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest = dest_dir / f.name
                        if dest.exists():
                            _LOGGER.warning(
                                "migrate_flat_events: skip %s — dest exists", f.name
                            )
                            continue
                        shutil.move(str(f), str(dest))
                        moved += 1
                return moved

            n = await hass.async_add_executor_job(_migrate, base)
            total += n
            _LOGGER.info("migrate_flat_events: moved %d file(s) in %s", n, base)

        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Bosch Kamera",
                "message": f"Flat-file migration complete: {total} file(s) moved to date hierarchy.",
                "notification_id": "bosch_migrate",
            },
        )

    async def handle_delete_event(call: ServiceCall) -> None:
        """Delete a local event file by file path or by camera + date."""
        from pathlib import Path

        file_path = (call.data.get("file_path") or "").strip()
        camera = (call.data.get("camera") or "").strip()
        date = (call.data.get("date") or "").strip()

        if not file_path and not camera:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="argument_required",
                translation_placeholders={"argument": "file_path or camera"},
            )

        deleted = 0
        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coord = entry.runtime_data
            if not coord:
                continue
            opts = coord.options
            base_str = (opts.get("download_path") or "").strip()
            if not base_str:
                continue
            base = Path(base_str).resolve()

            def _delete(base: Path, file_path: str, camera: str, date: str) -> int:
                import re as _re2

                _PAT2 = _re2.compile(
                    r"^(?:(?P<cam>.+?)_)?(?P<date>\d{4}-\d{2}-\d{2})_",
                    _re2.IGNORECASE,
                )
                count = 0
                if file_path:
                    target = Path(file_path).resolve()
                    try:
                        target.relative_to(base)
                    except ValueError:
                        _LOGGER.warning(
                            "delete_event: path %s outside base %s — rejected",
                            target,
                            base,
                        )
                        return 0
                    if target.is_file():
                        target.unlink()
                        count += 1
                    return count
                # camera + optional date filter. Unreachable defensively: the
                # service raises argument_required when file_path AND camera are
                # both empty, so with file_path empty here camera is always set.
                if not camera:  # pragma: no cover
                    return 0
                cam_dir = (base / camera).resolve()
                try:
                    cam_dir.relative_to(base)
                except ValueError:
                    return 0
                if not cam_dir.is_dir():
                    return 0
                for f in cam_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    m = _PAT2.match(f.name)
                    if not m:
                        continue
                    if date and m.group("date") != date:
                        continue
                    f.unlink()
                    count += 1
                # clean up empty dirs
                for d in sorted(cam_dir.rglob("*"), reverse=True):
                    if d.is_dir():
                        try:
                            d.rmdir()
                        except OSError:
                            pass
                return count

            n = await hass.async_add_executor_job(
                _delete, base, file_path, camera, date
            )
            deleted += n
            break

        _LOGGER.info("delete_event: deleted %d file(s)", deleted)
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "Bosch Kamera",
                "message": f"Deleted {deleted} event file(s).",
                "notification_id": "bosch_delete_event",
            },
        )

    if not hass.services.has_service(DOMAIN, "trigger_snapshot"):
        hass.services.async_register(
            DOMAIN, "trigger_snapshot", handle_trigger_snapshot
        )
    if not hass.services.has_service(DOMAIN, "open_live_connection"):
        hass.services.async_register(
            DOMAIN, "open_live_connection", handle_open_live_connection
        )
    if not hass.services.has_service(DOMAIN, "create_rule"):
        hass.services.async_register(DOMAIN, "create_rule", handle_create_rule)
    if not hass.services.has_service(DOMAIN, "delete_rule"):
        hass.services.async_register(DOMAIN, "delete_rule", handle_delete_rule)
    if not hass.services.has_service(DOMAIN, "delete_motion_zone"):
        hass.services.async_register(
            DOMAIN, "delete_motion_zone", handle_delete_motion_zone
        )
    if not hass.services.has_service(DOMAIN, "get_lighting_schedule"):
        hass.services.async_register(
            DOMAIN, "get_lighting_schedule", handle_get_lighting_schedule
        )
    if not hass.services.has_service(DOMAIN, "get_privacy_masks"):
        hass.services.async_register(
            DOMAIN, "get_privacy_masks", handle_get_privacy_masks
        )
    if not hass.services.has_service(DOMAIN, "set_privacy_masks"):
        hass.services.async_register(
            DOMAIN, "set_privacy_masks", handle_set_privacy_masks
        )
    if not hass.services.has_service(DOMAIN, "update_rule"):
        hass.services.async_register(DOMAIN, "update_rule", handle_update_rule)
    if not hass.services.has_service(DOMAIN, "set_motion_zones"):
        hass.services.async_register(
            DOMAIN, "set_motion_zones", handle_set_motion_zones
        )
    if not hass.services.has_service(DOMAIN, "get_motion_zones"):
        hass.services.async_register(
            DOMAIN, "get_motion_zones", handle_get_motion_zones
        )
    if not hass.services.has_service(DOMAIN, "share_camera"):
        hass.services.async_register(DOMAIN, "share_camera", handle_share_camera)
    if not hass.services.has_service(DOMAIN, "rename_camera"):
        hass.services.async_register(DOMAIN, "rename_camera", handle_rename_camera)
    if not hass.services.has_service(DOMAIN, "invite_friend"):
        hass.services.async_register(DOMAIN, "invite_friend", handle_invite_friend)
    if not hass.services.has_service(DOMAIN, "list_friends"):
        hass.services.async_register(DOMAIN, "list_friends", handle_list_friends)
    if not hass.services.has_service(DOMAIN, "remove_friend"):
        hass.services.async_register(DOMAIN, "remove_friend", handle_remove_friend)
    if not hass.services.has_service(DOMAIN, "migrate_flat_events"):
        hass.services.async_register(
            DOMAIN, "migrate_flat_events", handle_migrate_flat_events
        )
    if not hass.services.has_service(DOMAIN, "delete_event"):
        hass.services.async_register(DOMAIN, "delete_event", handle_delete_event)
