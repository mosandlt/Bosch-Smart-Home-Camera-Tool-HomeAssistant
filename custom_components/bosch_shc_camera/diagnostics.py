"""Diagnostics support for Bosch Smart Home Camera (Quality-Scale Gold).

Returns a redacted JSON snapshot of the integration state when the user
clicks "Download diagnostics" in Settings → Devices & Services. Replaces
the manual log-collection workflow for bug reports.

Sensitive fields (bearer / refresh tokens, FCM IDs, SMB credentials, MAC
addresses, cloud IDs) are redacted via homeassistant.diagnostics.async_redact_data.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

TO_REDACT = {
    # Tokens / OAuth credentials
    "bearer_token",
    "refresh_token",
    "access_token",
    "id_token",
    # FCM / Firebase secrets — async_redact_data walks dicts recursively, so
    # these top-level keys cover the nested fcm_credentials.* substructures.
    "fcm_token",
    "fcm_config",
    "fcm_credentials",
    "api_key",
    "vapid_key",
    "auth",
    "endpoint",
    "fid",
    "private",
    "public",
    "secret",
    "p256dh",
    "android_id",
    "security_token",
    "token",
    # SMB / NAS credentials
    "smb_password",
    "smb_username",
    "smb_server",
    # Stream / RTSP URLs (contain proxy session credentials)
    "rtspsUrl",
    "rtsps_url",
    "live_rtsps",
    "live_proxy",
    # Network identifiers (PII)
    "mac",
    "cloud_id",
    "videoInputId",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a Bosch SHC Camera config entry."""
    coord = getattr(entry, "runtime_data", None)

    # Per-camera summary — only fields safe to share, no secrets, no rtsps URLs
    cameras: list[dict[str, Any]] = []
    if coord is not None and coord.data:
        for cam_id, cdata in coord.data.items():
            info = cdata.get("info", {})
            live = cdata.get("live", {})
            cameras.append(
                {
                    "cam_id_prefix": cam_id[:8],
                    "title": info.get("title"),
                    "model": info.get("hardwareVersion"),
                    "firmware": info.get("firmwareVersion"),
                    "status": cdata.get("status"),
                    "online": cdata.get("online"),
                    "privacy_mode": cdata.get("privacy_mode"),
                    "events_today_count": len(cdata.get("events", [])),
                    "live_connection_type": live.get("connectionType"),
                    "live_age_seconds": live.get("age_seconds"),
                }
            )

    return {
        "entry": {
            "title": entry.title,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator": {
            "running": coord is not None,
            "last_update_success": getattr(coord, "last_update_success", None),
            "fcm_running": getattr(coord, "_fcm_running", None),
            "fcm_healthy": getattr(coord, "_fcm_healthy", None),
            "auth_outage_count": getattr(coord, "_auth_outage_count", None),
            "scan_interval": getattr(getattr(coord, "update_interval", None), "total_seconds", lambda: None)(),
        },
        "cameras": cameras,
    }
