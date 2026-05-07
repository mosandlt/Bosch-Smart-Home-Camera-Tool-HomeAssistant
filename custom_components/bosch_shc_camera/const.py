"""Constants for the Bosch Smart Home Camera integration."""

DOMAIN = "bosch_shc_camera"

# Lovelace card version — must match CARD_VERSION in src/bosch-camera-card.js.
# Bumped here alongside every card release so the auto-registered resource URL
# changes and browsers fetch the new file (HA serves www/ with max-age=31 days).
CARD_VERSION = "2.11.7"
CLOUD_API = "https://residential.cbs.boschsecurity.com"

ALL_PLATFORMS = [
    "binary_sensor", "camera", "sensor", "button",
    "switch", "number", "select", "update", "light",
]

LIVE_TYPE_CANDIDATES = ["REMOTE", "LOCAL"]
LIVE_SESSION_TTL = 55  # seconds — proxy sessions last ~60s, expire 5s early

# ── Network timeouts (seconds) ────────────────────────────────────────────────
# Centralised so snap + PUT /connection paths stay consistent across the
# integration and match the Python CLI (bosch_camera.py). Other endpoints
# still use inline literals — only the hot paths below were previously
# inconsistent (CLI 5/15s vs. integration 10s).
TIMEOUT_SNAP = 10             # GET on signed image / imageUrl
TIMEOUT_PUT_CONNECTION = 10   # PUT /v11/video_inputs/{id}/connection

DEFAULT_OPTIONS = {
    "scan_interval":      60,
    "interval_status":   300,
    "interval_events":   300,
    "snapshot_interval": 1800,
    "enable_snapshots":       True,
    "enable_sensors":         True,
    "enable_snapshot_button": True,
    "enable_local_save":      False,
    "download_path":          "/config/bosch_events",
    "high_quality_video": False,
    "stream_connection_type": "auto",
    # HLS player buffer profile applied by the Lovelace card (hls.js).
    # "latency"  → small buffer, ~4-6s lag, may stutter on Wi-Fi jitter
    # "balanced" → default, ~8-10s lag, robust against typical Wi-Fi hiccups
    # "stable"   → large buffer, ~12-15s lag, no stutter even on weak links
    "live_buffer_mode": "balanced",
    "enable_binary_sensors": True,
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
    "audio_default_on": True,
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
    "debug_logging": False,
    "enable_go2rtc": True,
}
