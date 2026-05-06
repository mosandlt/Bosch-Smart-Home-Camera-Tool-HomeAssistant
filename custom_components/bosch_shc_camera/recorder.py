"""Mini-NVR — local-only continuous recording sidecar.

Phase 1 MVP: spawn one ffmpeg child per LOCAL-streaming camera that reads from
the existing TLS-proxy RTSP URL (`rtsp://user:pass@127.0.0.1:NNN/...`) and
segments the stream into 5-min wall-aligned MP4 files on local disk.

Constraint (LAN-only):
    The recorder is allowed to run only when the camera's live session is in
    LOCAL mode AND the camera reports ONLINE.  If either flips off (e.g. the
    LOCAL→REMOTE fallback fires, or the camera goes OFFLINE) the recorder
    stops cleanly — no fallback to the cloud relay path.  See
    `docs/mini-nvr-concept.md` §2.

Architecture choice (`docs/mini-nvr-concept.md` §10): in-integration via
`asyncio.create_subprocess_exec`.  HA Add-on path is deferred to Phase 2 if
4-cam Pi 4 setups choke.  `-c copy` only — no transcoding.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import time
from typing import TYPE_CHECKING

from .smb import _safe_name

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


# Defaults — also exposed as config-flow options (`nvr_*`).
DEFAULT_BASE_PATH = "/config/bosch_nvr"
DEFAULT_RETENTION_DAYS = 3
DEFAULT_SEGMENT_SECONDS = 300  # 5 minutes, wall-aligned
# Crash-loop guard: if ffmpeg exits twice within this window we give up.
_RESPAWN_WINDOW_SECONDS = 30.0
_RESPAWN_DELAY_SECONDS = 5.0
# Stop timeout — give ffmpeg time to flush the trailing moov atom on SIGTERM.
_STOP_GRACE_SECONDS = 5.0

# ── Staging-drain watcher tunables ───────────────────────────────────────────
# ffmpeg writes EVERY segment locally first ("staging") so a half-flushed file
# is never uploaded. Once a segment file's mtime stops changing AND it has a
# reasonable size we treat it as finalized and move it to the configured
# storage target.
_DRAIN_TICK_SECONDS = 30.0          # how often the watcher sweeps staging
_DRAIN_FINALIZE_AGE_SECONDS = 60.0  # mtime must be older than this
_DRAIN_MIN_SIZE_BYTES = 10 * 1024   # < 10 KB → still being written / corrupt
_DRAIN_MAX_RETRIES = 5              # quarantine after this many failed uploads
_STAGING_DIRNAME = "_staging"
_FAILED_DIRNAME = "_failed"


# ── pure helpers (testable without spawning ffmpeg or touching disk) ─────────

def _segment_dir(base_path: str, cam_name: str) -> str:
    """Return ``{base_path}/{sanitized_cam_name}``.

    Camera names are user-controlled (Bosch app title), so we run them through
    the same `_safe_name()` used by the SMB upload pipeline to strip path
    traversal and shell metacharacters.  Test: `tests/test_recorder.py`.
    """
    return os.path.join(base_path, _safe_name(cam_name))


def _segment_pattern(base_path: str, cam_name: str) -> str:
    """Return the strftime pattern for the *promoted* (post-drain) segments.

    Layout: ``{base}/{cam}/YYYY-MM-DD/HH-MM.mp4``. This is where files end up
    when ``nvr_storage_target == "local"``. Wall-aligned 5 min slices make
    timeline scrubbing intuitive — "show me 14:35" doesn't fall inside a
    segment that started at 14:32. Used by the daily retention purge for the
    LOCAL target and as the canonical browse path for Media Source.
    """
    cam_dir = _segment_dir(base_path, cam_name)
    return os.path.join(cam_dir, "%Y-%m-%d", "%H-%M.mp4")


def _staging_dir(base_path: str, cam_name: str) -> str:
    """Return the per-camera staging dir under ``{base}/_staging/{cam}/``.

    ffmpeg always writes here regardless of ``nvr_storage_target``. Defends
    against partial-writes during segment rotation: an upload that happens
    mid-flush would otherwise produce a truncated MP4 with a missing moov
    atom. The drain watcher (``_drain_staging_to_remote``) picks up files
    only after their mtime has stopped moving, guaranteeing they are complete.
    """
    return os.path.join(base_path, _STAGING_DIRNAME, _safe_name(cam_name))


def _staging_pattern(base_path: str, cam_name: str) -> str:
    """ffmpeg ``-strftime`` output template inside the staging tree."""
    return os.path.join(
        _staging_dir(base_path, cam_name),
        "%Y-%m-%d", "%H-%M.mp4",
    )


def _failed_dir(base_path: str, cam_name: str) -> str:
    """Quarantine dir for files that exceeded the upload retry cap."""
    return os.path.join(base_path, _FAILED_DIRNAME, _safe_name(cam_name))


def _remote_smb_path(opts: dict, cam_name: str, date: str, fname: str) -> str:
    """Build the SMB destination path for one finalized segment.

    Layout: ``\\\\{server}\\{share}\\{smb_base_path}\\{nvr_smb_subpath}\\{cam}\\{date}\\{fname}``.
    Pure helper — no I/O. Called from the drain watcher per file.
    """
    server = (opts.get("smb_server") or "").strip()
    share = (opts.get("smb_share") or "").strip()
    base = (opts.get("smb_base_path") or "Bosch-Kameras").strip()
    sub = (opts.get("nvr_smb_subpath") or "NVR").strip()
    cam = _safe_name(cam_name)
    return f"\\\\{server}\\{share}\\{base}\\{sub}\\{cam}\\{date}\\{fname}".replace("/", "\\")


def _remote_ftp_path(opts: dict, cam_name: str, date: str, fname: str) -> str:
    """Build the FTP destination path for one finalized segment.

    Layout: ``/{smb_base_path}/{nvr_smb_subpath}/{cam}/{date}/{fname}`` — FTP
    has no shares, paths are relative to the FTP login root.
    """
    base = (opts.get("smb_base_path") or "Bosch-Kameras").strip().strip("/")
    sub = (opts.get("nvr_smb_subpath") or "NVR").strip().strip("/")
    cam = _safe_name(cam_name)
    return f"/{base}/{sub}/{cam}/{date}/{fname}".replace("//", "/")


def _build_ffmpeg_args(
    rtsp_url: str,
    segment_pattern: str,
    *,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
) -> list[str]:
    """Return the exact ffmpeg argv used to record the stream.

    Pure function so tests can pin the wire format without spawning ffmpeg.
    Pattern is fed to ``-f segment`` via ``-strftime 1`` — ffmpeg substitutes
    ``%Y/%m/%d/%H/%M`` from the wall clock and creates parent directories
    implicitly via ``-segment_format mp4`` + ``-strftime_mkdir 1``.
    """
    return [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel", "warning",
        # Force TCP — RTP-over-UDP through the loopback proxy is fragile and
        # the TLS proxy already rewrites SETUP to TCP-interleaved anyway.
        "-rtsp_transport", "tcp",
        # Reconnect on transient TCP drops; without this a TLS-proxy renewal
        # gap kills the recorder permanently.
        "-reconnect", "1",
        "-reconnect_at_eof", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", rtsp_url,
        "-map", "0",  # include all streams (video + AAC audio) — MVP keeps audio per concept §10
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-segment_format", "mp4",
        "-segment_atclocktime", "1",
        "-reset_timestamps", "1",
        "-strftime", "1",
        "-strftime_mkdir", "1",
        "-movflags", "+faststart",
        segment_pattern,
    ]


def should_record(
    coordinator: "BoschCameraCoordinator",
    cam_id: str,
    *,
    switch_on: bool,
) -> bool:
    """LAN-only gate. Returns True iff all three conditions hold:

    1. ``switch_on`` — user has toggled the per-camera NVR switch ON.
    2. The live session is LOCAL (NOT cloud relay).
    3. The camera is reachable (last status == ONLINE).

    Pure helper so tests can hit every combination without HA running.
    """
    if not switch_on:
        return False
    live = coordinator._live_connections.get(cam_id, {})
    if live.get("_connection_type") != "LOCAL":
        return False
    if not coordinator.is_camera_online(cam_id):
        return False
    return True


# ── recorder lifecycle (per-camera ffmpeg child) ─────────────────────────────

async def start_recorder(coordinator: "BoschCameraCoordinator", cam_id: str) -> None:
    """Spawn (or replace) the ffmpeg recorder for one camera.

    Idempotent: if a recorder is already running for ``cam_id`` it is stopped
    first so the new one picks up fresh creds (heartbeat-cred rotation hook).
    Caller is responsible for the LAN-only check (`should_record`).
    """
    # Replace any pre-existing recorder (cred rotation, switch re-toggle).
    await stop_recorder(coordinator, cam_id)

    live = coordinator._live_connections.get(cam_id, {})
    if live.get("_connection_type") != "LOCAL":
        _LOGGER.debug(
            "NVR start skipped for %s — not LOCAL (gate should have caught this)",
            cam_id[:8],
        )
        return
    rtsp_url = live.get("rtspsUrl") or live.get("rtspUrl") or ""
    if not rtsp_url.startswith("rtsp://"):
        _LOGGER.warning(
            "NVR start skipped for %s — TLS-proxy URL not ready (got %r)",
            cam_id[:8], rtsp_url[:30],
        )
        return

    opts = coordinator.options
    base_path = (opts.get("nvr_base_path") or DEFAULT_BASE_PATH).strip()
    cam_name = (
        coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    )
    # ffmpeg ALWAYS writes to a staging tree first — defends against
    # partial-writes during segment rotation. The drain watcher promotes
    # finalized files to either the local layout or to SMB / FTP, depending
    # on `nvr_storage_target`.
    pattern = _staging_pattern(base_path, cam_name)

    # Make the staging camera dir up-front so ffmpeg never trips on a missing
    # parent (strftime_mkdir creates only the strftime-derived leaf, not the
    # full chain).
    try:
        await coordinator.hass.async_add_executor_job(
            os.makedirs, _staging_dir(base_path, cam_name), 0o755, True,
        )
    except OSError as err:
        _LOGGER.warning(
            "NVR cannot create staging dir for %s: %s", cam_name, err,
        )
        return

    args = _build_ffmpeg_args(rtsp_url, pattern)

    _LOGGER.info(
        "NVR starting recorder for %s -> %s",
        cam_name, pattern,
    )
    _LOGGER.debug("NVR ffmpeg argv for %s: %s", cam_name, " ".join(args))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _LOGGER.error(
            "NVR cannot start — ffmpeg binary not found on PATH. "
            "Install ffmpeg or disable the NVR option.",
        )
        return
    except OSError as err:
        _LOGGER.warning("NVR ffmpeg spawn failed for %s: %s", cam_name, err)
        return

    coordinator._nvr_processes[cam_id] = proc
    # Watcher coroutine restarts ffmpeg once on transient crash and gives up
    # if it crashes again within _RESPAWN_WINDOW_SECONDS.
    task = coordinator.hass.async_create_background_task(
        _watch_recorder(coordinator, cam_id, proc),
        f"bosch_nvr_watch_{cam_id[:8]}",
    )
    coordinator._bg_tasks.add(task)
    task.add_done_callback(coordinator._bg_tasks.discard)


async def stop_recorder(coordinator: "BoschCameraCoordinator", cam_id: str) -> None:
    """Stop the recorder for one camera, giving ffmpeg up to 5 s to flush MP4."""
    proc = coordinator._nvr_processes.pop(cam_id, None)
    if proc is None:
        return
    if proc.returncode is not None:
        _LOGGER.debug(
            "NVR stop_recorder: ffmpeg already exited for %s (rc=%d)",
            cam_id[:8], proc.returncode,
        )
        return
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
        _LOGGER.debug(
            "NVR stop_recorder: ffmpeg cleanly exited for %s (rc=%d)",
            cam_id[:8], proc.returncode,
        )
    except asyncio.TimeoutError:
        _LOGGER.warning(
            "NVR stop_recorder: ffmpeg did not exit within %.0fs for %s — escalating to SIGKILL",
            _STOP_GRACE_SECONDS, cam_id[:8],
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "NVR stop_recorder: ffmpeg still alive after SIGKILL for %s",
                cam_id[:8],
            )


async def stop_all(coordinator: "BoschCameraCoordinator") -> None:
    """Stop every recorder — called on integration unload / HA stop."""
    for cam_id in list(coordinator._nvr_processes.keys()):
        await stop_recorder(coordinator, cam_id)


async def _watch_recorder(
    coordinator: "BoschCameraCoordinator",
    cam_id: str,
    proc: asyncio.subprocess.Process,
) -> None:
    """Watch one ffmpeg child, retry-once-then-give-up.

    HA already owns the LOCAL→REMOTE fallback decision; the recorder just
    follows it.  When ffmpeg exits with a non-zero rc and the LAN-only gate
    is still True we treat it as a transient failure (camera blip, TLS-proxy
    cred rotation, network glitch) and respawn after _RESPAWN_DELAY_SECONDS.
    A second crash inside _RESPAWN_WINDOW_SECONDS = give up; the user has to
    toggle the switch off+on to retry.
    """
    started_at = time.monotonic()
    rc = await proc.wait()
    # If somebody already removed the proc from _nvr_processes (clean stop /
    # replacement) we're done — nothing to respawn.
    if coordinator._nvr_processes.get(cam_id) is not proc:
        return
    coordinator._nvr_processes.pop(cam_id, None)

    # Drain stderr for the first crash to surface ffmpeg's reason.
    err_tail = ""
    if proc.stderr is not None:
        try:
            err_bytes = await asyncio.wait_for(proc.stderr.read(2048), timeout=1.0)
            err_tail = err_bytes.decode("utf-8", errors="replace").strip()
        except (asyncio.TimeoutError, Exception):
            pass

    elapsed = time.monotonic() - started_at
    _LOGGER.warning(
        "NVR ffmpeg exited rc=%s after %.0fs for %s. Tail: %s",
        rc, elapsed, cam_id[:8], err_tail[-500:] if err_tail else "(no stderr)",
    )

    # Quick re-check: only respawn if we still want to record.
    last = getattr(coordinator, "_nvr_user_intent", {}).get(cam_id, False)
    if not should_record(coordinator, cam_id, switch_on=last):
        _LOGGER.info("NVR not respawning for %s — gate now closed", cam_id[:8])
        return

    # Crash-loop guard.
    if elapsed < _RESPAWN_WINDOW_SECONDS:
        prev_crash = coordinator._nvr_recent_crash.get(cam_id, 0.0)
        now = time.monotonic()
        if (now - prev_crash) < _RESPAWN_WINDOW_SECONDS:
            _LOGGER.error(
                "NVR ffmpeg crashed twice within %.0fs for %s — giving up. "
                "Toggle the recording switch off+on to retry.",
                _RESPAWN_WINDOW_SECONDS, cam_id[:8],
            )
            coordinator._nvr_error_state[cam_id] = "ffmpeg crashed twice"
            return
        coordinator._nvr_recent_crash[cam_id] = now

    await asyncio.sleep(_RESPAWN_DELAY_SECONDS)
    if not should_record(coordinator, cam_id, switch_on=last):
        return
    _LOGGER.info("NVR respawning ffmpeg for %s after transient crash", cam_id[:8])
    await start_recorder(coordinator, cam_id)


# ── staging-drain watcher (per-coordinator background task) ──────────────────

def _list_staging_candidates(staging_root: str) -> list[tuple[str, str, str, float, int]]:
    """Walk the staging tree and return ``(full_path, cam, date, mtime, size)``
    tuples for every regular file. Pure helper so the watcher is testable
    without spinning up an event loop.
    """
    out: list[tuple[str, str, str, float, int]] = []
    if not os.path.isdir(staging_root):
        return out
    # Layout: {staging_root}/{cam}/{date}/{file}.mp4
    try:
        cams = os.listdir(staging_root)
    except OSError:
        return out
    for cam in cams:
        cam_dir = os.path.join(staging_root, cam)
        if not os.path.isdir(cam_dir):
            continue
        try:
            dates = os.listdir(cam_dir)
        except OSError:
            continue
        for date in dates:
            date_dir = os.path.join(cam_dir, date)
            if not os.path.isdir(date_dir):
                continue
            try:
                files = os.listdir(date_dir)
            except OSError:
                continue
            for fname in files:
                full = os.path.join(date_dir, fname)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                # Pin only regular files
                if not os.path.isfile(full):
                    continue
                out.append((full, cam, date, st.st_mtime, st.st_size))
    return out


def _is_segment_finalized(mtime: float, size: int, *, now: float | None = None) -> bool:
    """Return True iff the segment is old enough AND big enough to upload.

    Both thresholds together avoid uploading a half-written ffmpeg segment
    mid-rotation. Pure function for testability.
    """
    now_ts = now if now is not None else time.time()
    return (
        size >= _DRAIN_MIN_SIZE_BYTES
        and (now_ts - mtime) >= _DRAIN_FINALIZE_AGE_SECONDS
    )


def _move_local(coordinator: "BoschCameraCoordinator",
                full: str, base_path: str, cam: str, date: str, fname: str) -> bool:
    """target=local: rename staging file into ``{base}/{cam}/{date}/{fname}``.

    Returns True on success. The promoted layout is what Media Source / the
    retention purge already understand. Synchronous — runs inside the
    executor job that wraps the watcher tick.
    """
    dest_dir = os.path.join(base_path, cam, date)
    dest = os.path.join(dest_dir, fname)
    try:
        os.makedirs(dest_dir, mode=0o755, exist_ok=True)
        # ``shutil.move`` falls back to copy+unlink across filesystems
        # (e.g. if the user mounted a NAS at the base path).
        shutil.move(full, dest)
        return True
    except OSError as err:
        _LOGGER.debug("NVR drain (local): move %s -> %s failed: %s",
                      full, dest, err)
        return False


def _upload_smb(coordinator: "BoschCameraCoordinator",
                full: str, cam: str, date: str, fname: str) -> bool:
    """target=smb: upload one finalized segment via smbclient.

    Reuses the session-register pattern from ``smb.py`` but writes only to
    the NVR subtree (``{smb_base_path}/{nvr_smb_subpath}``) so cloud-event
    uploads stay in their own branch.
    """
    try:
        from smbclient import register_session, open_file
    except ImportError:
        _LOGGER.warning(
            "NVR drain (smb): smbprotocol not installed — install or set "
            "nvr_storage_target=local"
        )
        return False
    opts = coordinator.options
    server = (opts.get("smb_server") or "").strip()
    username = (opts.get("smb_username") or "").strip()
    password = opts.get("smb_password") or ""
    if not server:
        _LOGGER.debug("NVR drain (smb): smb_server is empty — skip")
        return False
    try:
        register_session(server, username=username, password=password)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("NVR drain (smb): session to %s failed: %s", server, err)
        return False

    # Build remote path + ensure the per-date folder exists.
    from .smb import smb_makedirs
    base = (opts.get("smb_base_path") or "Bosch-Kameras").strip()
    sub = (opts.get("nvr_smb_subpath") or "NVR").strip()
    server_share = f"\\\\{server}\\{(opts.get('smb_share') or '').strip()}"
    folder_parts = f"{sub}/{cam}/{date}"
    smb_folder = f"{server_share}\\{base}\\{folder_parts}".replace("/", "\\")
    try:
        smb_makedirs(smb_folder, server,
                     (opts.get("smb_share") or "").strip(),
                     base, folder_parts)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("NVR drain (smb): mkdir %s failed: %s", smb_folder, err)
        return False

    dest = _remote_smb_path(opts, cam, date, fname)
    try:
        with open(full, "rb") as src, open_file(dest, mode="wb") as dst:
            for chunk in iter(lambda: src.read(65536), b""):
                dst.write(chunk)
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("NVR drain (smb): upload %s -> %s failed: %s",
                        full, dest, err)
        return False


def _upload_ftp(coordinator: "BoschCameraCoordinator",
                full: str, cam: str, date: str, fname: str) -> bool:
    """target=ftp: upload one finalized segment via ftplib."""
    from .smb import _ftp_connect, _ftp_makedirs
    opts = coordinator.options
    server = (opts.get("smb_server") or "").strip()
    username = (opts.get("smb_username") or "").strip()
    password = opts.get("smb_password") or ""
    if not server:
        _LOGGER.debug("NVR drain (ftp): smb_server is empty — skip")
        return False
    try:
        ftp = _ftp_connect(server, username, password)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("NVR drain (ftp): login to %s failed: %s", server, err)
        return False
    try:
        base = (opts.get("smb_base_path") or "Bosch-Kameras").strip().strip("/")
        sub = (opts.get("nvr_smb_subpath") or "NVR").strip().strip("/")
        cam_safe = _safe_name(cam)
        ftp_dir = f"/{base}/{sub}/{cam_safe}/{date}".replace("//", "/").rstrip("/")
        try:
            _ftp_makedirs(ftp, ftp_dir)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("NVR drain (ftp): mkdir %s failed: %s", ftp_dir, err)
            return False
        dest = f"{ftp_dir}/{fname}"
        try:
            with open(full, "rb") as src:
                ftp.storbinary(f"STOR {dest}", src)
            return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("NVR drain (ftp): upload %s -> %s failed: %s",
                            full, dest, err)
            return False
    finally:
        try:
            ftp.quit()
        except Exception:
            try:
                ftp.close()
            except Exception:
                pass


def _quarantine_failed(base_path: str, full: str, cam: str, date: str, fname: str) -> None:
    """Move a file that exceeded the retry cap into ``{base}/_failed/{cam}/...``.

    Keeps the user's recording around for inspection without endlessly
    spamming upload retries each tick.
    """
    dest_dir = os.path.join(_failed_dir(base_path, cam), date)
    try:
        os.makedirs(dest_dir, mode=0o755, exist_ok=True)
        shutil.move(full, os.path.join(dest_dir, fname))
    except OSError as err:
        _LOGGER.debug("NVR drain: quarantine of %s failed: %s", full, err)


def sync_drain_tick(coordinator: "BoschCameraCoordinator", *,
                    now: float | None = None) -> dict[str, int]:
    """One synchronous drain pass over the staging tree.

    Pure-ish helper (touches disk + may do network I/O via the upload
    callbacks) — runs inside an executor job. Returns a counters dict the
    caller (the async watcher) can fold into the per-camera state used by
    ``BoschNvrStateSensor``.
    """
    opts = coordinator.options
    base_path = (opts.get("nvr_base_path") or DEFAULT_BASE_PATH).strip()
    target = (opts.get("nvr_storage_target") or "local").lower()
    staging_root = os.path.join(base_path, _STAGING_DIRNAME)

    # Per-camera retry counter survives across ticks via the coordinator.
    if not hasattr(coordinator, "_nvr_drain_failures"):
        coordinator._nvr_drain_failures = {}
    failures: dict[str, int] = coordinator._nvr_drain_failures

    promoted = uploaded = failed = 0
    pending = 0
    last_age: dict[str, float] = {}
    now_ts = now if now is not None else time.time()

    candidates = _list_staging_candidates(staging_root)
    for full, cam, date, mtime, size in candidates:
        # Always update the age stat so the sensor shows "fresh segment seen
        # but waiting to finalize" even before a successful drain.
        last_age[cam] = now_ts - mtime
        if not _is_segment_finalized(mtime, size, now=now_ts):
            pending += 1
            continue

        ok = False
        if target == "local":
            ok = _move_local(coordinator, full, base_path, cam, date, os.path.basename(full))
            if ok:
                promoted += 1
        elif target == "smb":
            ok = _upload_smb(coordinator, full, cam, date, os.path.basename(full))
            if ok:
                uploaded += 1
                try:
                    os.unlink(full)
                except OSError as err:
                    _LOGGER.debug("NVR drain: unlink %s after smb upload "
                                  "failed: %s", full, err)
        elif target == "ftp":
            ok = _upload_ftp(coordinator, full, cam, date, os.path.basename(full))
            if ok:
                uploaded += 1
                try:
                    os.unlink(full)
                except OSError as err:
                    _LOGGER.debug("NVR drain: unlink %s after ftp upload "
                                  "failed: %s", full, err)
        else:
            _LOGGER.debug("NVR drain: unknown target %r — treating as local", target)
            ok = _move_local(coordinator, full, base_path, cam, date, os.path.basename(full))
            if ok:
                promoted += 1

        if ok:
            failures.pop(full, None)
            continue

        failed += 1
        failures[full] = failures.get(full, 0) + 1
        if failures[full] >= _DRAIN_MAX_RETRIES:
            _LOGGER.error(
                "NVR drain: %s exceeded %d retries — quarantining to _failed/",
                full, _DRAIN_MAX_RETRIES,
            )
            _quarantine_failed(base_path, full, cam, date, os.path.basename(full))
            failures.pop(full, None)
            # Best-effort persistent notification — surface to the user.
            try:
                hass = getattr(coordinator, "hass", None)
                if hass is not None:
                    hass.loop.call_soon_threadsafe(
                        hass.async_create_task,
                        hass.services.async_call(
                            "persistent_notification", "create",
                            {
                                "title": "Bosch Mini-NVR — Upload failed",
                                "message": (
                                    f"Failed to drain {os.path.basename(full)} "
                                    f"after {_DRAIN_MAX_RETRIES} attempts. "
                                    f"File moved to {_failed_dir(base_path, cam)}."
                                ),
                                "notification_id": f"bosch_nvr_drain_failed_{cam}",
                            },
                        ),
                    )
            except Exception:
                pass

    # Persist the latest drain stats on the coordinator so the sensor can
    # render them. ``_nvr_drain_state`` is created on first tick.
    state: dict = getattr(coordinator, "_nvr_drain_state", None) or {}
    state["target"] = target
    state["pending"] = pending
    state["promoted"] = promoted
    state["uploaded"] = uploaded
    state["failed"] = failed
    state["last_age_by_cam"] = last_age
    state["last_tick_ts"] = now_ts
    coordinator._nvr_drain_state = state

    return {
        "promoted": promoted,
        "uploaded": uploaded,
        "failed": failed,
        "pending": pending,
    }


async def _drain_staging_to_remote(coordinator: "BoschCameraCoordinator") -> None:
    """Long-running watcher coroutine — one per coordinator (NOT per camera).

    Drives ``sync_drain_tick`` on a 30 s schedule via the HA executor pool so
    the synchronous SMB / FTP I/O never blocks the event loop. Cancellation
    is the supported stop path; ``async_unload_entry`` arranges that.
    """
    while True:
        try:
            opts = coordinator.options
            if opts.get("enable_nvr", False):
                try:
                    await coordinator.hass.async_add_executor_job(
                        sync_drain_tick, coordinator,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("NVR drain tick raised: %s", err)
            await asyncio.sleep(_DRAIN_TICK_SECONDS)
        except asyncio.CancelledError:
            _LOGGER.debug("NVR drain watcher cancelled — exiting")
            raise


# ── retention purge (runs in executor thread, once per day) ──────────────────

def sync_nvr_cleanup(coordinator: "BoschCameraCoordinator") -> None:
    """Delete NVR segments older than ``nvr_retention_days``.

    Dispatches based on ``nvr_storage_target``:
      * ``local`` → walk the on-disk tree under ``nvr_base_path`` (mirrors
        ``sync_smb_cleanup``: os.walk + cutoff math).
      * ``smb``   → walk only the NVR subtree
        ``{smb_base_path}/{nvr_smb_subpath}`` via smbclient.scandir.
      * ``ftp``   → walk only ``/{smb_base_path}/{nvr_smb_subpath}`` via
        ftplib LIST + MDTM.

    Always also purges the local ``_staging`` and ``_failed`` trees because
    those live under ``nvr_base_path`` regardless of the target. Same daily
    schedule as ``sync_smb_cleanup`` (called from ``_run_nvr_cleanup_bg``).
    """
    opts = coordinator.options
    retention_days = int(opts.get("nvr_retention_days", DEFAULT_RETENTION_DAYS))
    if retention_days <= 0:
        return
    target = (opts.get("nvr_storage_target") or "local").lower()
    if target == "smb":
        _sync_nvr_cleanup_smb(coordinator)
    elif target == "ftp":
        _sync_nvr_cleanup_ftp(coordinator)
    _sync_nvr_cleanup_local(coordinator)


def _sync_nvr_cleanup_local(coordinator: "BoschCameraCoordinator") -> None:
    """Local-disk retention purge — covers ``local`` target plus the staging /
    failed dirs (which exist no matter the target).
    """
    opts = coordinator.options
    base_path = (opts.get("nvr_base_path") or DEFAULT_BASE_PATH).strip()
    retention_days = int(opts.get("nvr_retention_days", DEFAULT_RETENTION_DAYS))
    if retention_days <= 0 or not base_path or not os.path.isdir(base_path):
        return

    cutoff = time.time() - retention_days * 86400
    deleted = 0
    for root, dirs, files in os.walk(base_path):
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                try:
                    os.remove(full)
                    deleted += 1
                except OSError as err:
                    _LOGGER.debug("NVR cleanup: cannot remove %s: %s", full, err)
    # Second pass: prune empty date folders (but never the camera dir or
    # base_path itself).
    for root, dirs, files in os.walk(base_path, topdown=False):
        if root == base_path:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass
    if deleted:
        _LOGGER.info(
            "NVR cleanup (local): deleted %d file(s) older than %d days from %s",
            deleted, retention_days, base_path,
        )


def _sync_nvr_cleanup_smb(coordinator: "BoschCameraCoordinator") -> None:
    """Walk only the NVR subtree on the SMB share and unlink old files."""
    try:
        from smbclient import register_session, scandir, remove, stat as smb_stat
    except ImportError:
        return
    opts = coordinator.options
    server = (opts.get("smb_server") or "").strip()
    share = (opts.get("smb_share") or "").strip()
    username = (opts.get("smb_username") or "").strip()
    password = opts.get("smb_password") or ""
    base_path = (opts.get("smb_base_path") or "Bosch-Kameras").strip()
    sub = (opts.get("nvr_smb_subpath") or "NVR").strip()
    retention_days = int(opts.get("nvr_retention_days", DEFAULT_RETENTION_DAYS))
    if not server or not share or retention_days <= 0:
        return
    try:
        register_session(server, username=username, password=password)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("NVR cleanup (smb): session to %s failed: %s", server, err)
        return

    cutoff = time.time() - retention_days * 86400
    root = f"\\\\{server}\\{share}\\{base_path}\\{sub}"
    deleted = 0

    def _walk_and_delete(path: str) -> None:
        nonlocal deleted
        try:
            entries = list(scandir(path))
        except Exception:  # noqa: BLE001
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
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("NVR cleanup (smb): error on %s: %s",
                                  entry.name, err)

    _walk_and_delete(root)
    if deleted:
        _LOGGER.info(
            "NVR cleanup (smb): deleted %d file(s) older than %d days from %s",
            deleted, retention_days, root,
        )


def _sync_nvr_cleanup_ftp(coordinator: "BoschCameraCoordinator") -> None:
    """Walk only the NVR subtree on the FTP server and unlink old files."""
    import ftplib
    from datetime import datetime, timezone
    from .smb import _ftp_connect

    opts = coordinator.options
    server = (opts.get("smb_server") or "").strip()
    username = (opts.get("smb_username") or "").strip()
    password = opts.get("smb_password") or ""
    base_path = (opts.get("smb_base_path") or "Bosch-Kameras").strip().strip("/")
    sub = (opts.get("nvr_smb_subpath") or "NVR").strip().strip("/")
    retention_days = int(opts.get("nvr_retention_days", DEFAULT_RETENTION_DAYS))
    if not server or retention_days <= 0:
        return
    try:
        ftp = _ftp_connect(server, username, password)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("NVR cleanup (ftp): login to %s failed: %s", server, err)
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
        except Exception:  # noqa: BLE001
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
                resp = ftp.sendcmd(f"MDTM {name}")
                ts_str = resp.split()[-1]
                mt = datetime.strptime(ts_str[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
            except Exception:  # noqa: BLE001
                continue
            if mt < cutoff:
                try:
                    ftp.delete(name)
                    deleted += 1
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("NVR cleanup (ftp): delete %s failed: %s",
                                  name, err)
        for sd in subdirs:
            _walk_and_delete(f"{path}/{sd}")
            try:
                ftp.cwd(path)
            except Exception:  # noqa: BLE001
                pass

    try:
        root = f"/{base_path}/{sub}"
        _walk_and_delete(root)
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: BLE001
            pass
    if deleted:
        _LOGGER.info(
            "NVR cleanup (ftp): deleted %d file(s) older than %d days from %s",
            deleted, retention_days, f"{server}/{base_path}/{sub}",
        )
