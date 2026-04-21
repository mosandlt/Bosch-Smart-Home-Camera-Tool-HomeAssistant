"""Constants for the Bosch Smart Home Camera integration."""

DOMAIN = "bosch_shc_camera"
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
    "enable_auto_download":   False,
    "download_path":          "",
    "shc_ip":        "",
    "shc_cert_path": "",
    "shc_key_path":  "",
    "high_quality_video": False,
    "stream_connection_type": "auto",
    "enable_binary_sensors": True,
    "enable_fcm_push": False,
    "alert_notify_service": "",
    "alert_notify_system": "",
    "alert_notify_information": "",
    "alert_notify_screenshot": "",
    "alert_notify_video": "",
    "alert_save_snapshots": False,
    "alert_delete_after_send": True,
    "fcm_push_mode": "auto",
    "audio_default_on": True,
    "enable_intercom": False,
    "enable_smb_upload": False,
    "smb_server": "",
    "smb_share": "",
    "smb_username": "",
    "smb_password": "",
    "smb_base_path": "Bosch-Kameras",
    "smb_folder_pattern": "{year}/{month}",
    "smb_file_pattern": "{camera}_{date}_{time}_{type}_{id}",
    "smb_retention_days": 180,
    "smb_disk_warn_mb": 5120,
    "debug_logging": False,
    "enable_go2rtc": True,
}
