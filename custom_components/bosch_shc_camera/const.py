"""Constants for the Bosch Smart Home Camera integration."""

import re

DOMAIN = "bosch_shc_camera"

# Lovelace card version — must match CARD_VERSION in src/bosch-camera-card.js.
# Bumped here alongside every card release so the auto-registered resource URL
# changes and browsers fetch the new file (HA serves www/ with max-age=31 days).
CARD_VERSION = "14.1.20"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

# Delivery-death detection (issue #36). When the periodic /v11/events poll finds
# a genuinely NEW event while FCM is enabled+running+"healthy" yet no real push
# has arrived in this window, push delivery is dead at the cloud/Google layer
# even though the socket reports is_started()=True (the exact silent-death case
# the fcm.py module docstring describes). The poll is ground truth that push
# missed a real event, so we flip fcm_healthy=False and force a HARD heal
# (purge + fresh registration) — which also re-POSTs to Bosch /v11/devices,
# healing a server-side-dropped device registration. 10 min is wide enough that
# a push arriving just before the poll (race) keeps fcm_last_push recent and
# suppresses a false positive.
FCM_DELIVERY_DEAD_AFTER_SEC = 600.0

# Bounded slow-tier defer (stream-contention gate, see slow_tier.py). While a
# live stream is active the slow-tier diagnostic read is deferred to avoid
# TLS-channel contention. On a *continuously* active stream (e.g. a dashboard
# left on live view) that deferral would otherwise never end, freezing the
# diagnostic sensors indefinitely. After this many seconds of unbroken
# deferral we force one read even while streaming — accepting a rare brief
# contention over permanently stale diagnostics — then the defer cycle
# restarts. Bounds worst-case staleness to ~this + one slow interval.
SLOW_TIER_MAX_DEFER_SEC = 1800.0

ALL_PLATFORMS = [
    "binary_sensor",
    "camera",
    "image",
    "sensor",
    "button",
    "switch",
    "number",
    "select",
    "update",
    "light",
    "text",
]

LIVE_SESSION_TTL = 55  # seconds — proxy sessions last ~60s, expire 5s early


class _StreamStartSkipped(dict):  # type: ignore[type-arg]
    """Sentinel returned by ``try_live_connection`` when it declined to open a
    new session because a non-renewal start for the same camera was already in
    flight (opportunistic de-dup: ``lock.locked() and not is_renewal and not
    force_reset``).

    This is **not** a failure — the in-flight start will publish the session.

    It subclasses ``dict`` (and stays empty) on purpose:
      * it is **falsy**, so the many existing ``if result:`` / ``if not
        result:`` consumers keep treating it exactly like the old ``None``
        return — no behaviour change for them;
      * it is structurally a ``dict[str, Any]``, so ``try_live_connection``
        keeps its ``dict | None`` return type and the renewal/recovery
        callers that do ``result.get(...)`` stay type-safe (and would no-op
        rather than crash even in the impossible case they received it).

    Only consumers that would otherwise emit a *false* failure side-effect
    (log "Live stream failed", discard the user's stream intent, or record a
    stream error that wrongly nudges the camera to REMOTE) compare with
    ``is STREAM_START_SKIPPED`` and treat it as a benign no-op. Never mutate
    the singleton — ``__bool__`` is pinned False as a belt-and-braces guard.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False


# Singleton instance — compare with ``is STREAM_START_SKIPPED``.
STREAM_START_SKIPPED = _StreamStartSkipped()

# ── snap.jpg resolution (`JpegSize`) ──────────────────────────────────────────
# Every snap.jpg call site used to hardcode JpegSize=1206 (full resolution),
# including the ones serving the Lovelace card — which asks HA for width=315.
# Measured payloads for the same frame on one Gen1 outdoor camera:
#
#     JpegSize=1206  ≈ 500 KB      JpegSize=640  ≈ 220 KB      JpegSize=320  ≈ 65 KB
#
# On a bandwidth-constrained link the full-res body alone can take longer than
# HA's CAMERA_IMAGE_TIMEOUT (10 s), so a preview request fails outright and the
# card shows a stale frame. Deriving the size from the width HA passed to
# async_camera_image() costs nothing for callers that want full resolution:
# jpeg_size_for_width() returns None (= "leave it at JPEG_SIZE_FULL") whenever
# no width was requested, which is what the background image refresh, the
# image.* entity and the AI-analysis fetch do.
JPEG_SIZE_FULL = 1206
JPEG_SIZE_THUMB = 320
JPEG_SIZE_MEDIUM = 640

_JPEG_SIZE_RE = re.compile(r"JpegSize=\d+")


def jpeg_size_for_width(width: int | None) -> int | None:
    """Map the width HA requested onto a camera ``JpegSize`` value.

    Returns ``None`` when the caller did not ask for a thumbnail-sized image,
    meaning the snap.jpg URL should keep its full-resolution ``JpegSize``.
    """
    if width is None or width <= 0 or width > JPEG_SIZE_MEDIUM:
        return None
    if width <= JPEG_SIZE_THUMB:
        return JPEG_SIZE_THUMB
    return JPEG_SIZE_MEDIUM


def with_jpeg_size(url: str, size: int | None) -> str:
    """Return ``url`` with its ``JpegSize`` query parameter set to ``size``.

    A ``size`` of ``None`` (no width requested) returns the URL unchanged, so
    full-resolution callers keep exactly the URL they had before.
    """
    if not url or not size:
        return url
    if "JpegSize=" in url:
        return _JPEG_SIZE_RE.sub(f"JpegSize={int(size)}", url)
    return f"{url}{'&' if '?' in url else '?'}JpegSize={int(size)}"


# ── Network timeouts (seconds) ────────────────────────────────────────────────
# Centralised so snap + PUT /connection paths stay consistent across the
# integration and match the Python CLI (bosch_camera.py). Other endpoints
# still use inline literals — only the hot paths below were previously
# inconsistent (CLI 5/15s vs. integration 10s).
TIMEOUT_SNAP = 10  # GET on signed image / imageUrl
TIMEOUT_PUT_CONNECTION = 10  # PUT /v11/video_inputs/{id}/connection
TIMEOUT_RCP_099E_PROBE = 2.5  # RCP 0x099e thumbnail probe (GitHub #56 — must not eat the snap.jpg leg's budget)
RCP_099E_PROBE_FAILURE_MEMO_SEC = (
    3600  # skip the probe for this long per-cam_id after a failure/timeout
)

# GitHub #55: LOCAL snap.jpg's inline Digest request is capped at 6s to stay
# under HA's outer 10s CAMERA_IMAGE_TIMEOUT — too tight for cameras whose TLS
# handshake alone runs 2.5-6.9s, and a handshake killed mid-flight never gets
# pooled, so every request starts cold and hits the same wall. On a timeout,
# a background attempt gets this much more generous budget to complete the
# handshake once and leave a warm pooled connection for subsequent inline
# requests, rate-limited so a persistently-slow camera isn't hammered.
LOCAL_SNAP_WARMUP_TIMEOUT_SEC = 25.0
LOCAL_SNAP_WARMUP_MIN_INTERVAL_SEC = 30.0

# GET /v11/video_inputs (camera_list.py) — first call of every coordinator
# tick, gating everything else that tick. A bare timeout here used to fail
# the WHOLE tick immediately (community report, forum thread, 2026-07-23: a
# brief Bosch-cloud blip caused two consecutive failed ticks, self-recovered
# on the third) — one quick in-tick retry absorbs a momentary hiccup instead
# of costing users a full failed update + stale/offline-looking cameras over
# what a few seconds' grace would have covered. A persistent outage still
# fails after the retry, same as before.
TIMEOUT_VIDEO_INPUTS = 15.0
VIDEO_INPUTS_RETRY_DELAY_SEC = 3.0

# issue #47: AUTO-mode TCP pre-check chicken-and-egg breaker. When the
# camera's cached LAN IP is stale (DHCP re-lease after a mesh flap/reboot),
# every pre-check ping against it fails forever, which would otherwise skip
# LOCAL — and only the LOCAL PUT itself can teach us the camera's *current*
# IP. At most once per this interval, ignore a failing pre-check and let
# LOCAL be attempted for real so a fresh IP has a chance to be learned; the
# existing pre-warm-failure fallback still demotes to REMOTE gracefully if
# the camera really is unreachable.
LAN_RECHECK_FORCE_INTERVAL_SEC = 600.0

# Subprocess-lifecycle timeouts (recorder.py). Grace = SIGTERM→SIGKILL window;
# kill_wait = post-SIGKILL wait_for; stderr_drain = drain pipe before close;
# ffmpeg_init = NVR FFmpeg process init wait.
TIMEOUT_RECORDER_GRACE = 20.0
TIMEOUT_RECORDER_KILL_WAIT = 2.0
TIMEOUT_RECORDER_STDERR_DRAIN = 1.0
TIMEOUT_RECORDER_FFMPEG_INIT = 30.0
TIMEOUT_RECORDER_SEGMENT_PROBE = 5.0


# SHC local-API fallback retry policy. Used by shc.py's circuit breaker
# (offline mode). Centralized so the values are not buried as instance
# attributes inside the coordinator.
SHC_MAX_FAILS = 3  # mark SHC offline after this many consecutive failures
SHC_RETRY_INTERVAL = 120  # seconds — retry SHC after this long while offline

DEFAULT_MOTION_ACTIVE_WINDOW = 90  # seconds — see binary_sensor.py for rationale
MOTION_ACTIVE_WINDOW_MIN = 10  # seconds
MOTION_ACTIVE_WINDOW_MAX = 300  # seconds

# Idle-session reaper. A LOCAL session (card view, Cast, camera.play_stream,
# camera.record, media-browser preview) is torn down after STREAM_IDLE_REAP_SEC
# with no consumer, freeing the camera's LOCAL RTSP session (Bosch caps LOCAL
# sessions at 60 min) instead of lingering until the maxSessionDuration recycle.
# Reaping is driven by consumer presence, not by the switch: an active viewer or
# Mini-NVR recorder counts as a consumer and is never reaped, so automations that
# use the stream are unaffected. See __init__.py idle_session_reaper.
STREAM_IDLE_REAP_SEC = 180  # no-consumer grace before tearing a session down
STREAM_IDLE_REAP_CHECK_SEC = 30  # reaper poll interval
# An HLS stream counts as actively watched if a playlist/segment was fetched
# within this window (clients refetch every few seconds). Used instead of HA's
# unreliable Stream.available (which stays True for the whole session). See
# cf_unbuffer.hls_access_age + __init__.py has_active_consumer.
STREAM_HLS_FRESH_SEC = 30

DEFAULT_AI_DESCRIBE_PROMPT = "Du bist eine Überwachungskamera-Assistenz. Melde NUR sicherheitsrelevante Beobachtungen: Personen (auch nur teilweise sichtbar: Beine, Arme, Silhouette, Schatten), Fahrzeuge, Tiere, Pakete oder ungewöhnliche Aktivität. Beschreibe NICHT die Umgebung, Räume, Möbel, Architektur oder Bildqualität und benenne KEINE Orte. Rate nicht: Fußmatten, Teppiche, Bodenfliesen und Schatten sind kein Paket. Wenn nichts Sicherheitsrelevantes erkennbar ist, sage das kurz, z. B.: Keine sicherheitsrelevanten Beobachtungen."
DEFAULT_AI_DESCRIBE_LANGUAGE = "Deutsch"

DEFAULT_OPTIONS = {
    "scan_interval": 60,
    "interval_status": 300,
    "interval_events": 300,
    "snapshot_interval": 1800,
    "enable_snapshots": True,
    "enable_sensors": True,
    "enable_snapshot_button": True,
    "enable_local_save": False,
    "download_path": "/config/bosch_events",
    "stream_connection_type": "local",
    # HLS player buffer profile applied by the Lovelace card (hls.js).
    # "latency"  → small buffer, ~4-6s lag, may stutter on Wi-Fi jitter
    # "balanced" → default, ~8-10s lag, robust against typical Wi-Fi hiccups
    # "stable"   → large buffer, ~12-15s lag, no stutter even on weak links
    "live_buffer_mode": "balanced",
    "enable_binary_sensors": True,
    "motion_active_window": DEFAULT_MOTION_ACTIVE_WINDOW,
    "enable_fcm_push": False,
    "alert_notify_service": "",
    "alert_notify_system": "",
    "alert_notify_information": "",
    "alert_notify_screenshot": "",
    "alert_notify_video": "",
    "alert_save_snapshots": False,
    "alert_delete_after_send": True,
    "mark_events_read": False,
    "fcm_push_mode": "auto",
    "enable_intercom": False,
    "enable_smb_upload": False,
    "upload_protocol": "smb",
    "smb_server": "",
    "smb_share": "",
    "smb_username": "",
    "smb_password": "",
    "smb_base_path": "Bosch-Kameras",
    "folder_pattern": "{camera}/{year}/{month}/{day}",
    "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
    "smb_retention_days": 180,
    # ── Mini-NVR (continuous LAN-only recording) — Phase 1 MVP ──────────────
    # Disabled by default; opt-in via integration options. See
    # `docs/mini-nvr-concept.md` for the full design.
    "enable_nvr": False,
    "nvr_base_path": "/config/bosch_nvr",
    "nvr_retention_days": 3,
    # NVR storage target: "local" (default — segments stay under nvr_base_path),
    # "smb" (drain finalized segments to the same SMB share used for events),
    # "ffp" / "ftp" (drain to FTP server). ffmpeg ALWAYS writes to a local
    # staging dir first; the watcher in recorder.drain_staging_to_remote moves
    # finalized files to the remote target.
    "nvr_storage_target": "local",
    # Subfolder under smb_base_path / FTP base_path to keep NVR segments
    # separate from the cloud-event upload tree. Default "NVR".
    "nvr_smb_subpath": "NVR",
    # Phase 3: quality — "auto" = inst=1 (max ~30 Mbps), "low" = inst=4 (~1.9 Mbps, LOCAL only)
    "nvr_quality": "auto",
    # Phase 4: pre-roll buffer — 0 = disabled; 10-60 s = keep rolling cache in tmpfs
    "nvr_preroll_seconds": 0,
    "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",  # noqa: S108 # default tmpfs cache dir, overridable via config options
    "nvr_postroll_seconds": 0,
    # Opt-in: stop-finalize the ring's actively-written segment (SIGTERM,
    # wait for moov atom) and re-attach it before dropping the newest
    # segment on an FCM event, instead of always discarding it. Costs a
    # small (~1s) ring gap per event. Default OFF — issue #43 follow-up
    # feature request, realKim-dotcom.
    "nvr_finalize_ring_on_event": False,
    "enable_go2rtc": True,
    # Green IT (power/bandwidth saving). Currently: the idle-session reaper tears
    # a camera's live session down once nobody is watching it for
    # STREAM_IDLE_REAP_SEC, so the camera stops encoding+streaming video to no
    # one (saves WLAN bandwidth + camera power/heat, turns the live LED off,
    # frees Bosch's per-camera 60-min session slot). Umbrella flag — future
    # power-saving behaviours hang off the same toggle.
    # DEFAULT OFF / UNDER DEVELOPMENT (2026-06-03): the idle reaper's consumer
    # detection cannot reliably see a live WebRTC viewer — HA's go2rtc-backed
    # WebRTC session does not surface as a go2rtc `consumers` entry on every
    # setup (verified live: real WebRTC viewer → consumers:null), so the reaper
    # false-negatives and tears down a stream someone is actively watching.
    # Parked behind an opt-in, off by default, until viewer-presence detection
    # is reworked. The 60-min Bosch session recycle still bounds any ghost.
    "enable_green_it": False,
    "enable_webhook_delivery": False,
    "webhook_url": "",
    # PTZ controls (pan presets) — opt-in. CAMERA_360 indoor only; default off
    # so non-PTZ users do not see a stray select entity in their dashboard.
    "enable_ptz_controls": False,
    # Card auto-play default — exposed as camera entity attribute so the
    # Lovelace card can read it. Per-card YAML `auto_play` overrides this.
    # Values:
    #   "lan"    — auto-reveal on LAN, tap-to-reveal overlay on remote (default)
    #   "always" — auto-reveal in every session
    #   "never"  — tap-to-reveal overlay in every session
    # The card pre-initializes the backend stream while the overlay is
    # showing so video is warm by the time the user taps.
    "auto_play_default": "lan",
    # MJPEG inst=3 snapshot source (Gen2 cameras only).
    # When True: async_camera_image() tries to fetch one JPEG frame directly
    # from the camera's LAN RTSP inst=3 stream via FFmpeg subprocess before
    # falling back to the normal cloud-proxy / snap.jpg path. Bypasses the
    # H.264-transcode overhead for snapshot requests (~150-300 ms vs ~500 ms
    # cloud-proxy round-trip on a healthy LAN).
    # KNOWN ISSUE (2026-05-25): FFmpeg's built-in TLS stack does not negotiate
    # cleanly with Bosch's RTSPS server on port 443 — returns "Invalid data
    # found when processing input" (FFmpeg code 183) even with `-tls_verify 0`.
    # The reliable path is to route FFmpeg through our existing tls_proxy.py
    # (plain RTSP on 127.0.0.1:<port>), but that requires non-trivial setup-
    # tearing per snapshot which would defeat the speed benefit. Until that's
    # implemented, opt-in only — keeps the code path available for testing
    # and skips it for normal users so warn-spam stays out of the logs.
    "use_mjpeg_snapshot": False,
    # Defer slow-tier diagnostic cloud reads while a live stream is active.
    # Default ON: prevents TLS-channel contention → stream freeze on motion burst.
    # See stream-freeze-on-motion-event-contention.md.
    "defer_diag_during_stream": True,
    # ── AI Snapshot Description ───────────────────────────────────────────────
    # Opt-in: when enabled, exposes a describe_snapshot service and a per-camera
    # sensor holding the last AI-generated description.
    "enable_ai_description": False,
    "ai_task_entity": "",  # empty → HA chooses the preferred ai_task entity
    "ai_describe_prompt": DEFAULT_AI_DESCRIBE_PROMPT,
    "ai_describe_language": DEFAULT_AI_DESCRIBE_LANGUAGE,
    "ai_describe_on_motion": False,
    # Append the AI snapshot description to the event push notification
    # ("Stage 2" snapshot alert). Opt-in (default off). Rate-limited by the
    # two budget knobs below so frequent motion events do not burn AI credits.
    "ai_notify_include_description": False,
    # Per-camera minimum seconds between AUTO analyses (notify-include +
    # on-motion). Manual describe_snapshot bypasses the cooldown.
    "ai_cooldown_seconds": 60,
    # Global daily budget across all cameras (resets at local midnight). When
    # exceeded, auto analyses are skipped until the next day. 0 = unlimited.
    "ai_max_per_day": 100,
    # Activation time window for AUTO AI analyses (on-motion + notify-include).
    # "HH:MM" or "HH:MM:SS". Both empty = no time gate (always active).
    # If end < start the window spans midnight (e.g. 22:00–06:00).
    "ai_active_time_start": "",
    "ai_active_time_end": "",
    # Condition-entity gate: entity_id whose state must match ai_active_condition_state
    # before AUTO analyses are allowed. Empty = no condition gate.
    "ai_active_condition_entity": "",
    "ai_active_condition_state": "not_home",
    # ── AI Camera Analysis (motion-triggered structured scoring) ──────────────
    # Sibling to AI Snapshot Description above (free-text): this requests a
    # STRUCTURED 1-10 suspicion score + fields from ai_task.generate_data,
    # with its own gating/budget so the two features don't compete for the
    # same daily AI-call allowance. Opt-in, default off. Design inspired by
    # concepts from github.com/simpleaddins/HomeAssistantAICameraCentre (MIT)
    # per Thomas's request, independently reimplemented against this
    # integration's own coordinator/entity model — see README credit line.
    "ai_analysis_enabled": False,
    "ai_analysis_task_entity": "",  # empty → falls back to ai_task_entity above
    "ai_analysis_snapshot_count": 3,
    "ai_analysis_snapshot_interval_ms": 800,
    "ai_analysis_cooldown_seconds": 30,
    "ai_analysis_max_per_day": 200,
    "ai_analysis_retention_days": 30,
    "ai_analysis_repeat_context_minutes": 30,
    "ai_analysis_alarm_panel_entity": "",
    "ai_analysis_alarmo_enabled": False,
    "ai_analysis_alarm_trigger_service": "",
    "ai_analysis_alarm_trigger_score": 7,
    # ── Frigate / external-recorder persistent RTSP endpoints ─────────────────
    # Opt-in always-on, credential-free RTSP front-door per camera (see
    # frigate_endpoint.py). Master switch; per-camera High/Low switches gate
    # which quality URLs are published. Default OFF.
    "frigate_endpoints_enabled": False,
    # Bind host for the front-door listener. "127.0.0.1" = localhost-only
    # (default, safest); "0.0.0.0" = reachable from the whole LAN (needed when
    # the recorder runs on another host — credential-free, so pair with the
    # IP allowlist and/or a token).
    "frigate_bind_host": "127.0.0.1",
    # Comma-separated client IPs / CIDRs allowed to connect. Empty = allow any.
    "frigate_ip_allowlist": "",
    # Gate auth: "none" | "path_token" | "basic". With a token set, path_token
    # serves rtsp://host:port/<token>/rtsp_tunnel?…, basic serves
    # rtsp://user:pass@host:port/… . Empty token disables the gate.
    "frigate_auth_mode": "none",
    "frigate_token": "",
    "frigate_basic_user": "frigate",
    # Seconds a session lingers after the last recorder disconnects.
    "frigate_idle_timeout": 60,
    # Fixed RTSP bind port (0 = OS-assigned ephemeral, changes on restart).
    # Set to e.g. 8556 to keep the sensor URL stable across HA restarts and
    # settings changes. Multiple cameras use port, port+1, … (sorted cam-ID order).
    "frigate_bind_port": 0,
    # Max simultaneous recorder clients per camera front-door (anti-flood).
    # Default 8 covers typical Frigate multi-sub-stream setups with headroom.
    "frigate_max_connections": 8,
}

# v2.16.0 dropped the historical "confirm" value (popup dialog) in favour
# of an inline tap-to-reveal overlay. Stale "confirm" values from v12.8.0
# collapse to "lan" at the read site in camera.py.
AUTO_PLAY_DEFAULT_VALUES = ("lan", "always", "never")

# ── Frigate / external-recorder persistent RTSP endpoints ─────────────────────
CONF_FRIGATE_ENDPOINTS_ENABLED = "frigate_endpoints_enabled"
CONF_FRIGATE_BIND_HOST = "frigate_bind_host"
CONF_FRIGATE_BIND_PORT = "frigate_bind_port"
CONF_FRIGATE_IP_ALLOWLIST = "frigate_ip_allowlist"
CONF_FRIGATE_AUTH_MODE = "frigate_auth_mode"
CONF_FRIGATE_TOKEN = "frigate_token"  # noqa: S105 # option key name, not a credential
CONF_FRIGATE_BASIC_USER = "frigate_basic_user"
CONF_FRIGATE_IDLE_TIMEOUT = "frigate_idle_timeout"
CONF_FRIGATE_MAX_CONNECTIONS = "frigate_max_connections"
FRIGATE_BIND_VALUES = ("127.0.0.1", "0.0.0.0")  # noqa: S104 # 0.0.0.0 is an explicit opt-in LAN-exposure choice
FRIGATE_AUTH_VALUES = ("none", "path_token", "basic")

# ── Webhook delivery ──────────────────────────────────────────────────────────
CONF_ENABLE_WEBHOOK_DELIVERY = "enable_webhook_delivery"
CONF_WEBHOOK_URL = "webhook_url"

# ── PTZ controls (pan presets) ────────────────────────────────────────────────
CONF_ENABLE_PTZ_CONTROLS = "enable_ptz_controls"

# ── Slow-tier defer during live stream ────────────────────────────────────────
# When True (default), slow-tier diagnostic cloud reads are deferred while a
# camera's live stream is active, preventing TLS-channel contention that could
# cause go2rtc EOF → 5-10 s stream freeze on motion bursts.  Set to False only
# if diagnostic sensors must stay current even while a stream is running.
CONF_DEFER_DIAG_DURING_STREAM = "defer_diag_during_stream"
DEFAULT_DEFER_DIAG_DURING_STREAM = True

# ── AI Snapshot Description ───────────────────────────────────────────────────
CONF_ENABLE_AI_DESCRIPTION = "enable_ai_description"
CONF_AI_TASK_ENTITY = "ai_task_entity"
CONF_AI_DESCRIBE_PROMPT = "ai_describe_prompt"
CONF_AI_DESCRIBE_LANGUAGE = "ai_describe_language"
CONF_AI_DESCRIBE_ON_MOTION = "ai_describe_on_motion"
CONF_AI_NOTIFY_INCLUDE_DESCRIPTION = "ai_notify_include_description"
CONF_AI_COOLDOWN_SECONDS = "ai_cooldown_seconds"
CONF_AI_MAX_PER_DAY = "ai_max_per_day"
CONF_AI_ACTIVE_TIME_START = "ai_active_time_start"
CONF_AI_ACTIVE_TIME_END = "ai_active_time_end"
CONF_AI_ACTIVE_CONDITION_ENTITY = "ai_active_condition_entity"
CONF_AI_ACTIVE_CONDITION_STATE = "ai_active_condition_state"

# ── AI Camera Analysis (motion-triggered structured scoring) ──────────────────
CONF_AI_ANALYSIS_ENABLED = "ai_analysis_enabled"
CONF_AI_ANALYSIS_TASK_ENTITY = "ai_analysis_task_entity"
CONF_AI_ANALYSIS_SNAPSHOT_COUNT = "ai_analysis_snapshot_count"
CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS = "ai_analysis_snapshot_interval_ms"
CONF_AI_ANALYSIS_COOLDOWN_SECONDS = "ai_analysis_cooldown_seconds"
CONF_AI_ANALYSIS_MAX_PER_DAY = "ai_analysis_max_per_day"
CONF_AI_ANALYSIS_RETENTION_DAYS = "ai_analysis_retention_days"
CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES = "ai_analysis_repeat_context_minutes"
CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY = "ai_analysis_alarm_panel_entity"
CONF_AI_ANALYSIS_ALARMO_ENABLED = "ai_analysis_alarmo_enabled"
CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE = "ai_analysis_alarm_trigger_service"
CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE = "ai_analysis_alarm_trigger_score"
# Alert-target subentry field names (config_entries subentry data, not a flat option)
CONF_AI_TARGET_NAME = "target_name"
CONF_AI_TARGET_NOTIFY_SERVICE = "notify_service"
CONF_AI_TARGET_MIN_SCORE = "min_score"
CONF_AI_TARGET_CONDITION = "condition"
CONF_AI_TARGET_CAMERA_FILTER = "camera_filter"
AI_TARGET_CONDITIONS = ("always", "away", "armed", "away_or_armed")
# Known-visitor subentry field names
CONF_AI_VISITOR_NAME = "visitor_name"
CONF_AI_VISITOR_DESCRIPTION = "visitor_description"
# NOTE: no "camera-scope" subentry — per-camera AI-analysis enable/disable
# lives solely on switch.<cam>_ai_analysis (switch.py), matching every other
# per-camera toggle in this integration. An earlier draft added a redundant
# ai_camera_scope subentry with no backend consumer; removed before release.
