"""Constants for the Bosch Smart Home Camera integration."""

DOMAIN = "bosch_shc_camera"

# Lovelace card version — must match CARD_VERSION in src/bosch-camera-card.js.
# Bumped here alongside every card release so the auto-registered resource URL
# changes and browsers fetch the new file (HA serves www/ with max-age=31 days).
CARD_VERSION = "13.5.6"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

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
]

LIVE_SESSION_TTL = 55  # seconds — proxy sessions last ~60s, expire 5s early

# ── Network timeouts (seconds) ────────────────────────────────────────────────
# Centralised so snap + PUT /connection paths stay consistent across the
# integration and match the Python CLI (bosch_camera.py). Other endpoints
# still use inline literals — only the hot paths below were previously
# inconsistent (CLI 5/15s vs. integration 10s).
TIMEOUT_SNAP = 10  # GET on signed image / imageUrl
TIMEOUT_PUT_CONNECTION = 10  # PUT /v11/video_inputs/{id}/connection

# Subprocess-lifecycle timeouts (recorder.py). Grace = SIGTERM→SIGKILL window;
# kill_wait = post-SIGKILL wait_for; stderr_drain = drain pipe before close;
# ffmpeg_init = NVR FFmpeg process init wait.
TIMEOUT_RECORDER_GRACE = 5.0
TIMEOUT_RECORDER_KILL_WAIT = 2.0
TIMEOUT_RECORDER_STDERR_DRAIN = 1.0
TIMEOUT_RECORDER_FFMPEG_INIT = 30.0

# tls_proxy.py — TCP connect to camera + RTSP pre-warm DESCRIBE response wait.
TIMEOUT_TLS_PROXY_CONNECT = 10
TIMEOUT_TLS_PROXY_RTSP_READ = 5

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
# use the stream are unaffected. See __init__.py _idle_session_reaper.
STREAM_IDLE_REAP_SEC = 180  # no-consumer grace before tearing a session down
STREAM_IDLE_REAP_CHECK_SEC = 30  # reaper poll interval
# An HLS stream counts as actively watched if a playlist/segment was fetched
# within this window (clients refetch every few seconds). Used instead of HA's
# unreliable Stream.available (which stays True for the whole session). See
# cf_unbuffer.hls_access_age + __init__.py _has_active_consumer.
STREAM_HLS_FRESH_SEC = 30

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
    # staging dir first; the watcher in recorder._drain_staging_to_remote moves
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
    "enable_go2rtc": True,
    # Green IT (power/bandwidth saving). Currently: the idle-session reaper tears
    # a camera's live session down once nobody is watching it for
    # STREAM_IDLE_REAP_SEC, so the camera stops encoding+streaming video to no
    # one (saves WLAN bandwidth + camera power/heat, turns the live LED off,
    # frees Bosch's per-camera 60-min session slot). Umbrella flag — future
    # power-saving behaviours hang off the same toggle. Default ON.
    "enable_green_it": True,
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
}

# v2.16.0 dropped the historical "confirm" value (popup dialog) in favour
# of an inline tap-to-reveal overlay. Stale "confirm" values from v12.8.0
# collapse to "lan" at the read site in camera.py.
AUTO_PLAY_DEFAULT_VALUES = ("lan", "always", "never")

# ── Webhook delivery ──────────────────────────────────────────────────────────
CONF_ENABLE_WEBHOOK_DELIVERY = "enable_webhook_delivery"
CONF_WEBHOOK_URL = "webhook_url"

# ── PTZ controls (pan presets) ────────────────────────────────────────────────
CONF_ENABLE_PTZ_CONTROLS = "enable_ptz_controls"
