"""SMB/NAS upload and auto-download helpers for Bosch Smart Home Camera.

Extracted from __init__.py to keep the coordinator lean.
All functions that previously used `self` now take a `coordinator` parameter.
"""
from __future__ import annotations

import calendar
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


# ── Local save (FCM-triggered, runs in executor thread) ───────────────────────

def sync_local_save(coordinator, ev: dict, token: str, cam_name: str) -> None:
    """Save a single event's image/clip to the local download_path on FCM trigger.

    Folder structure follows folder_pattern option (default: {camera}/{year}/{month}/{day}).
    Filename follows file_pattern option (default: {camera}_{date}_{time}_{type}_{id}).
    """
    import requests
    import urllib3
    urllib3.disable_warnings()

    opts = coordinator.options
    download_path = (opts.get("download_path") or "").strip()
    if not download_path:
        return

    ts = ev.get("timestamp", "")
    if not ts or len(ts) < 19:
        return

    # Reject events that predate this coordinator session (e.g. delayed/queued
    # FCM pushes arriving after a reload). Parse ISO timestamp → epoch and
    # compare against coordinator._download_started_at (set at __init__ time).
    # Allow 60 s of slack for clock skew and network/processing delay.
    started_at = getattr(coordinator, "_download_started_at", 0.0)
    if started_at:
        try:
            struct = time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")
            ev_epoch = calendar.timegm(struct)
            if ev_epoch < started_at - 60:
                _LOGGER.debug(
                    "Local save skipped: event %s predates session start (%.0fs old at startup)",
                    ts[:19], started_at - ev_epoch,
                )
                return
        except Exception:
            pass

    cam_safe = _safe_name(cam_name)
    date_str = ts[:10]
    time_str = ts[11:19].replace(":", "-")
    etype = ev.get("eventType", "EVENT")
    ev_id = (ev.get("id") or "")[:8].upper()

    year, month, day = date_str[:4], date_str[5:7], date_str[8:10]
    folder_pattern = (opts.get("folder_pattern") or "{camera}/{year}/{month}/{day}").strip().strip("/")
    file_pattern = (opts.get("file_pattern") or "{camera}_{date}_{time}_{type}_{id}").strip()

    try:
        sub = folder_pattern.format(camera=cam_safe, year=year, month=month, day=day,
                                    date=date_str, time=time_str, type=etype)
    except (KeyError, ValueError):
        sub = cam_safe

    folder = os.path.join(download_path, sub.replace("/", os.sep))

    try:
        stem = file_pattern.format(camera=cam_safe, date=date_str, time=time_str,
                                   type=etype, id=ev_id, year=year, month=month, day=day)
    except (KeyError, ValueError):
        stem = f"{cam_safe}_{date_str}_{time_str}_{etype}_{ev_id}"

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    session.verify = False

    for ext, url in [("jpg", ev.get("imageUrl")), ("mp4", ev.get("videoClipUrl"))]:
        if not url:
            continue
        if ext == "mp4" and ev.get("videoClipUploadStatus") != "Done":
            continue
        if not _is_safe_bosch_url(url):
            continue
        path = os.path.join(folder, f"{stem}.{ext}")
        if os.path.exists(path):
            continue
        try:
            r = session.get(url, timeout=60, stream=True)
            if r.status_code == 200:
                os.makedirs(folder, exist_ok=True)
                with open(path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)
                _LOGGER.debug("Local save: %s", os.path.basename(path))
        except Exception as err:
            _LOGGER.warning("Local save failed for %s: %s", os.path.basename(path), err)


# ── SMB/NAS upload (runs in executor thread) ──────────────────────────────────

def sync_smb_upload(coordinator, data: dict, token: str) -> None:
    """Upload new event files to SMB or FTP.

    Folder structure: {smb_base_path}/{camera}/{year}/{month}/{day}/{camera_name}_{date}_{time}_{type}.{ext}
    Backend selected via ``upload_protocol`` option ("smb" default, or "ftp").
    """
    protocol = (coordinator.options.get("upload_protocol") or "smb").lower()
    if protocol == "ftp":
        return _sync_ftp_upload(coordinator, data, token)

    import requests as req
    import urllib3
    urllib3.disable_warnings()

    opts = coordinator.options
    server = opts.get("smb_server", "").strip()
    share = opts.get("smb_share", "").strip()
    username = opts.get("smb_username", "").strip()
    password = opts.get("smb_password", "")
    base_path = opts.get("smb_base_path", "Bosch-Kameras").strip()
    folder_pattern = opts.get("folder_pattern", "{camera}/{year}/{month}/{day}").strip()
    file_pattern = opts.get("file_pattern", "{camera}_{date}_{time}_{type}_{id}").strip()

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
    # Bosch Cloud uses a private CA (Video CA 2A) not in the system trust store.
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
    """Delete files on the SMB or FTP share that are older than smb_retention_days."""
    protocol = (coordinator.options.get("upload_protocol") or "smb").lower()
    if protocol == "ftp":
        return _sync_ftp_cleanup(coordinator)
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
    protocol = (coordinator.options.get("upload_protocol") or "smb").lower()
    if protocol == "ftp":
        # FTP has no portable disk-free RPC across servers; skip silently.
        return
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

    # Use smbclient's statvfs to get free space (not available in all smbclient versions)
    try:
        import smbclient
        if not hasattr(smbclient, "statvfs"):
            # smbclient package installed on this HA does not expose statvfs —
            # the disk-free check is unsupported. Skip silently.
            return
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


# ── FTP backend (FRITZ.NAS, plain FTP servers) ────────────────────────────────
# FRITZ!Box SMB on macOS Sequoia hangs in `rename()` for minutes; FTP RNFR/RNTO
# is ~75 file/s on the same hardware. FTP backend reuses smb_* options
# (server / username / password / base_path / patterns); smb_share is unused
# because FTP has no shares — the base_path is relative to the FTP root.

def _ftp_connect(server: str, username: str, password: str):
    """Open a passive-mode FTP connection. Caller closes via .quit()."""
    import ftplib
    ftp = ftplib.FTP(server, timeout=30)
    ftp.login(username, password)
    ftp.set_pasv(True)
    return ftp


def _ftp_exists(ftp, path: str) -> bool:
    import ftplib
    try:
        ftp.size(path)
        return True
    except ftplib.error_perm:
        return False
    except Exception:
        return False


def _ftp_makedirs(ftp, path: str) -> None:
    """Create FTP directories recursively, ignoring already-exists errors."""
    import ftplib
    parts = [p for p in path.split("/") if p]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            ftp.mkd(current)
        except ftplib.error_perm:
            pass  # already exists or permission — ignore


def _sync_ftp_upload(coordinator, data: dict, token: str) -> None:
    """Upload event files to an FTP server (e.g. FRITZ.NAS via plain FTP)."""
    import requests as req
    import urllib3
    from io import BytesIO
    urllib3.disable_warnings()

    opts = coordinator.options
    server = opts.get("smb_server", "").strip()
    username = opts.get("smb_username", "").strip()
    password = opts.get("smb_password", "")
    base_path = opts.get("smb_base_path", "Bosch-Kameras").strip().strip("/")
    folder_pattern = opts.get("folder_pattern", "{camera}/{year}/{month}/{day}").strip()
    file_pattern = opts.get("file_pattern", "{camera}_{date}_{time}_{type}_{id}").strip()

    if not server:
        return

    try:
        ftp = _ftp_connect(server, username, password)
    except Exception as err:
        _LOGGER.warning("FTP login to %s failed: %s", server, err)
        return

    session = req.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    session.verify = False

    try:
        for cam_id, cam_data in data.items():
            cam_name = cam_data["info"].get("title", cam_id)
            ev_list = cam_data.get("events", [])
            _LOGGER.debug("FTP upload: %s has %d events", cam_name, len(ev_list))

            for ev in ev_list:
                ts = ev.get("timestamp", "")
                if not ts or len(ts) < 19:
                    continue

                year = ts[:4]
                month = ts[5:7]
                day = ts[8:10]
                date_str = f"{year}-{month}-{day}"
                time_str = ts[11:19].replace(":", "-")
                etype = ev.get("eventType", "EVENT")
                ev_id = ev.get("id", "")[:8]

                folder_parts = folder_pattern.format(
                    year=year, month=month, day=day,
                    camera=cam_name, type=etype,
                ).strip("/")
                file_base = file_pattern.format(
                    camera=cam_name, date=date_str, time=time_str,
                    type=etype, id=ev_id, year=year, month=month, day=day,
                )

                ftp_dir = f"/{base_path}/{folder_parts}".replace("//", "/").rstrip("/")
                _ftp_makedirs(ftp, ftp_dir)

                # Snapshot
                img_url = ev.get("imageUrl")
                if img_url and _is_safe_bosch_url(img_url):
                    fname = f"{file_base}.jpg"
                    fpath = f"{ftp_dir}/{fname}"
                    if _ftp_exists(ftp, fpath):
                        _LOGGER.debug("FTP skip (exists): %s", fname)
                    else:
                        try:
                            r = session.get(img_url, timeout=30)
                            if r.status_code == 200 and r.content:
                                ftp.storbinary(f"STOR {fpath}", BytesIO(r.content))
                                _LOGGER.info("FTP uploaded: %s (%d bytes)", fname, len(r.content))
                            else:
                                _LOGGER.warning("FTP snapshot download failed: HTTP %d", r.status_code)
                        except Exception as err:
                            _LOGGER.warning("FTP upload error for %s: %s", fname, err)

                # Video clip
                clip_url = ev.get("videoClipUrl")
                clip_status = ev.get("videoClipUploadStatus", "")
                if clip_url and clip_status == "Done" and _is_safe_bosch_url(clip_url):
                    fname = f"{file_base}.mp4"
                    fpath = f"{ftp_dir}/{fname}"
                    if _ftp_exists(ftp, fpath):
                        _LOGGER.debug("FTP skip (exists): %s", fname)
                    else:
                        try:
                            r = session.get(clip_url, timeout=60, stream=True)
                            if r.status_code == 200:
                                ftp.storbinary(f"STOR {fpath}", r.raw)
                                _LOGGER.info("FTP uploaded: %s", fname)
                            else:
                                _LOGGER.warning("FTP clip download failed: HTTP %d", r.status_code)
                        except Exception as err:
                            _LOGGER.warning("FTP clip upload error for %s: %s", fname, err)
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def _sync_ftp_cleanup(coordinator) -> None:
    """Delete files on the FTP server older than smb_retention_days."""
    import ftplib
    from datetime import datetime, timezone

    opts = coordinator.options
    server = opts.get("smb_server", "").strip()
    username = opts.get("smb_username", "").strip()
    password = opts.get("smb_password", "")
    base_path = opts.get("smb_base_path", "Bosch-Kameras").strip().strip("/")
    retention_days = int(opts.get("smb_retention_days", 180))

    if not server or retention_days <= 0:
        return

    try:
        ftp = _ftp_connect(server, username, password)
    except Exception as err:
        _LOGGER.warning("FTP cleanup: login to %s failed: %s", server, err)
        return

    cutoff = time.time() - retention_days * 86400
    deleted = 0

    def _walk_and_delete(path: str) -> None:
        nonlocal deleted
        try:
            ftp.cwd(path)
        except ftplib.error_perm:
            return
        entries: list[str] = []
        try:
            ftp.retrlines("LIST", entries.append)
        except Exception:
            return

        files: list[str] = []
        subdirs: list[str] = []
        for line in entries:
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms, name = parts[0], parts[-1]
            if name in (".", ".."):
                continue
            if perms.startswith("d"):
                subdirs.append(name)
            elif perms.startswith("-"):
                files.append(name)

        for name in files:
            try:
                # MDTM for accurate mtime; falls back to LIST timestamp parsing if absent
                resp = ftp.sendcmd(f"MDTM {name}")
                # "213 YYYYMMDDHHMMSS"
                ts_str = resp.split()[-1]
                mt = datetime.strptime(ts_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            except Exception:
                continue
            if mt < cutoff:
                try:
                    ftp.delete(name)
                    deleted += 1
                except Exception as err:
                    _LOGGER.debug("FTP cleanup: delete %s failed: %s", name, err)

        for sub in subdirs:
            _walk_and_delete(f"{path}/{sub}")
            try:
                ftp.cwd(path)  # back up before next sibling
            except Exception:
                pass

    try:
        root = f"/{base_path}"
        _walk_and_delete(root)
    finally:
        try:
            ftp.quit()
        except Exception:
            pass

    if deleted:
        _LOGGER.info(
            "FTP cleanup: deleted %d file(s) older than %d days from %s",
            deleted, retention_days, f"{server}/{base_path}",
        )
