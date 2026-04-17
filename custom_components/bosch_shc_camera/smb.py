"""SMB/NAS upload and auto-download helpers for Bosch Smart Home Camera.

Extracted from __init__.py to keep the coordinator lean.
All functions that previously used `self` now take a `coordinator` parameter.
"""
from __future__ import annotations

import logging
import os
import re
import socket
import time
from urllib.parse import urlparse

_LOGGER = logging.getLogger(__name__)


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


def _safe_name(name: str) -> str:
    """Sanitize a camera name for use as a directory/file name component.

    Removes path traversal sequences and non-safe characters, truncates to 64 chars.
    """
    return re.sub(r"[^\w\-. ]", "_", name.replace("..", "_"))[:64]


# ── Auto-download (runs in executor thread) ───────────────────────────────────

def sync_download(coordinator, data: dict, token: str, download_path: str) -> None:
    """Download new event files to download_path/{camera_name}/."""
    import requests
    import urllib3
    urllib3.disable_warnings()

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    session.verify = False

    for cam_id, cam_data in data.items():
        cam_name = cam_data["info"].get("title", cam_id)
        folder = os.path.join(download_path, cam_name)
        os.makedirs(folder, exist_ok=True)

        for ev in cam_data.get("events", []):
            _download_one(session, ev, folder, "jpg", ev.get("imageUrl"))
            if ev.get("videoClipUploadStatus") == "Done":
                _download_one(session, ev, folder, "mp4", ev.get("videoClipUrl"))


def _download_one(
    session, ev: dict, folder: str, ext: str, url: str | None
) -> None:
    if not url:
        return
    ts = ev.get("timestamp", "")[:19].replace(":", "-").replace("T", "_")
    etype = ev.get("eventType", "EVENT")
    ev_id = ev.get("id", "")[:8]
    path = os.path.join(folder, f"{ts}_{etype}_{ev_id}.{ext}")
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


# ── SMB/NAS upload (runs in executor thread) ──────────────────────────────────

def sync_smb_upload(coordinator, data: dict, token: str) -> None:
    """Upload new event files to SMB/NAS share.

    Folder structure: {smb_base_path}/{year}/{month}/{camera_name}_{date}_{time}_{type}.{ext}
    Uses smbprotocol for cross-platform SMB access.
    """
    import requests as req
    import urllib3
    urllib3.disable_warnings()

    opts = coordinator.options
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
        socket.setdefaulttimeout(10)
        try:
            register_session(server, username=username, password=password)
        finally:
            socket.setdefaulttimeout(None)
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
                smb_makedirs(smb_folder, server, share, base_path, folder_parts)
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


def smb_makedirs(full_path: str, server: str, share: str, base_path: str, folder_parts: str) -> None:
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


# ── SMB retention cleanup (runs in executor thread, once per day) ─────────────

def sync_smb_cleanup(coordinator) -> None:
    """Delete files on the SMB share that are older than smb_retention_days."""
    try:
        from smbclient import register_session, scandir, remove, stat as smb_stat
    except ImportError:
        return

    opts = coordinator.options
    server = opts.get("smb_server", "").strip()
    share = opts.get("smb_share", "").strip()
    username = opts.get("smb_username", "").strip()
    password = opts.get("smb_password", "")
    base_path = opts.get("smb_base_path", "Bosch-Kameras").strip()
    retention_days = int(opts.get("smb_retention_days", 180))

    if not server or not share or retention_days <= 0:
        return

    try:
        socket.setdefaulttimeout(10)
        try:
            register_session(server, username=username, password=password)
        finally:
            socket.setdefaulttimeout(None)
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


# ── SMB disk-free check (runs in executor thread, once per hour) ──────────────

def sync_smb_disk_check(coordinator) -> None:
    """Check free space on the SMB share and fire an HA alert if low."""
    try:
        from smbclient import register_session
        import smbclient._io as _smb_io  # noqa: F401 — ensure smbclient loaded
    except ImportError:
        return

    opts = coordinator.options
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
        socket.setdefaulttimeout(10)
        try:
            register_session(server, username=username, password=password)
        finally:
            socket.setdefaulttimeout(None)
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
        coordinator.hass.loop.call_soon_threadsafe(
            coordinator.hass.async_create_task,
            async_smb_disk_alert(coordinator, msg, notify_service),
        )


async def async_smb_disk_alert(coordinator, message: str, notify_service: str) -> None:
    """Send disk-full warning via notify service or HA persistent notification."""
    services = [s.strip() for s in notify_service.split(",") if s.strip()]
    sent = False
    for svc in services:
        domain, _, name = svc.partition(".")
        if coordinator.hass.services.has_service(domain, name):
            try:
                await coordinator.hass.services.async_call(
                    domain, name,
                    {"message": message, "title": "Bosch Kamera — Speicherwarnung"},
                )
                sent = True
            except Exception as err:
                _LOGGER.debug("SMB disk alert via %s failed: %s", svc, err)
    if not sent:
        # Fall back to HA persistent notification
        await coordinator.hass.services.async_call(
            "persistent_notification", "create",
            {
                "title": "Bosch Kamera — Speicherwarnung",
                "message": message,
                "notification_id": "bosch_smb_disk_warn",
            },
        )
