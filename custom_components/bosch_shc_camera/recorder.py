"""Mini-NVR — local-only continuous recording sidecar.

Phase 1 MVP: spawn one ffmpeg child per LOCAL-streaming camera that reads from
the existing published RTSP URL (`live_connections[cam_id]["rtspsUrl"]` —
since viewing_front_door.py, this is normally the credential-free stable-port
viewing front-door URL for LOCAL sessions, `rtsp://127.0.0.1:NNN/...`, falling
back to the raw `rtsp://user:pass@127.0.0.1:NNN/...` TLS-proxy URL only if the
front-door failed to bind) and segments the stream into 5-min wall-aligned MP4
files on local disk.

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
import datetime
import logging
import math
import os
import shutil
import signal
import time
from datetime import UTC
from typing import TYPE_CHECKING, Any

from bosch_shc_camera_client.mini_nvr import apply_quality as _lib_apply_quality
from bosch_shc_camera_client.mini_nvr import build_ffmpeg_args as _lib_build_ffmpeg_args
from bosch_shc_camera_client.mini_nvr import (
    build_preroll_ffmpeg_args as _lib_build_preroll_ffmpeg_args,
)
from bosch_shc_camera_client.mini_nvr import (
    create_motion_clip_args as _lib_create_motion_clip_args,
)
from bosch_shc_camera_client.mini_nvr import (
    list_preroll_segments as _lib_list_preroll_segments,
)
from bosch_shc_camera_client.mini_nvr import (
    newest_preroll_path as _lib_newest_preroll_path,
)
from bosch_shc_camera_client.mini_nvr import (
    newest_segment_is_finalized as _lib_newest_segment_is_finalized,
)
from bosch_shc_camera_client.mini_nvr import (
    prune_preroll_cache as _lib_prune_preroll_cache,
)

from .const import (
    TIMEOUT_RECORDER_FFMPEG_INIT,
    TIMEOUT_RECORDER_GRACE,
    TIMEOUT_RECORDER_KILL_WAIT,
    TIMEOUT_RECORDER_SEGMENT_PROBE,
)
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
# Per-call deadline for the NVR SMB/FTP cleanup walk (_sync_nvr_cleanup_smb /
# _sync_nvr_cleanup_ftp): a hung/unreachable NAS share has no other backstop
# — these run in an executor thread with no depth limit and no per-op
# timeout of their own, so a stalled scandir()/LIST call could otherwise
# block the whole cleanup job (and the executor thread) indefinitely. Checked
# with time.monotonic() (SENTINEL_RULE — never 0.0) at the top of every
# recursive call; on expiry the walk unwinds without deleting further files
# rather than raising, so files already found within the deadline are still
# removed.
_NVR_CLEANUP_MAX_SECONDS = 60.0
# Auth-retry guard: a single 401 is almost always a
# transient heartbeat cred-rotation race and is retried without counting
# toward the crash-window give-up above — but a GENUINE broken credential
# would otherwise retry silently forever. Cap consecutive 401s so a real
# fault still surfaces instead of looping forever.
_MAX_CONSECUTIVE_AUTH_RETRIES = 5
# Stop timeout — give ffmpeg time to flush the trailing moov atom on SIGTERM.
# Centralized in const.py so the SIGTERM/SIGKILL/stderr timing is tunable
# without touching the recorder.
_STOP_GRACE_SECONDS = TIMEOUT_RECORDER_GRACE

# When the NVR switch is toggled on right after Live Stream ON, the TLS
# proxy URL is still empty until the RTSP DESCRIBE handshake completes
# (~3–10 s on Gen2). Poll for it before giving up.
_PROXY_URL_WAIT_STEPS = 24
_PROXY_URL_WAIT_INTERVAL = 0.5

# ── Phase 4: pre-roll buffer tunables ────────────────────────────────────────
_PREROLL_SEGMENT_SECONDS = 10  # short segments for fine-grained pre-roll
_PREROLL_MAX_SEGMENTS = 5  # keep last 5 × 10 s = 50 s max in tmpfs
_PREROLL_MIN_SIZE_BYTES = 1024  # discard sub-1 KB corrupt segments
# ffmpeg's concat demuxer can exit rc=0 while having stitched together
# segments with inconsistent timing (e.g. a mid-ring RTSP reconnect on a
# flaky camera) — not a failure worth discarding the clip over, but worth a
# loud log line instead of being silently invisible.
_CONCAT_DISCONTINUITY_MARKERS = (
    "non-monotonic",
    "non monotonically increasing dts",
    "discontinuity",
)
# A hard-killed HA process (SIGKILL/OOM) can leave a `_stage/<clip>`
# hardlink dir behind between `_stage_segments_for_concat`
# and its own `finally` cleanup. Swept on ring spawn (below), age-gated well
# past any realistic assembly duration (postroll capture ≤60s + concat well
# under its own timeout) so an in-flight concurrent assembly's own
# seconds-old stage dir is never touched.
_STAGE_ORPHAN_MAX_AGE_SECONDS = 300.0

# ── Staging-drain watcher tunables ───────────────────────────────────────────
# ffmpeg writes EVERY segment locally first ("staging") so a half-flushed file
# is never uploaded. Once a segment file's mtime stops changing AND it has a
# reasonable size we treat it as finalized and move it to the configured
# storage target.
_DRAIN_TICK_SECONDS = 30.0  # how often the watcher sweeps staging
_DRAIN_FINALIZE_AGE_SECONDS = 60.0  # mtime must be older than this
_DRAIN_MIN_SIZE_BYTES = 10 * 1024  # < 10 KB → still being written / corrupt
_DRAIN_MAX_RETRIES = 5  # quarantine after this many failed uploads
_STAGING_DIRNAME = "_staging"
_FAILED_DIRNAME = "_failed"


# ── live stderr drain (GitHub #64) ───────────────────────────────────────────
# ffmpeg is spawned with stderr=PIPE but nothing used to read it until AFTER
# `proc.wait()` returned. On a flaky/bandwidth-constrained RTSP source
# (-loglevel warning still emits frequent non-monotonic-DTS / packet-loss
# lines), enough unread output fills the OS pipe buffer (~64KB on Linux) and
# ffmpeg's own write() to its stderr blocks forever — the process never
# exits, `proc.wait()` never returns, and the recorder hangs completely
# silently: no crash, no log line, registered as "running" while the output
# directory stays empty forever. Exactly the reported symptom. Fix: drain
# stderr continuously for the life of the process via a dedicated background
# task, keeping only a small rolling tail for diagnostics.
_STDERR_TAIL_MAX_BYTES = 2048  # matches the old single post-exit read() size
_STDERR_DRAIN_CHUNK_BYTES = 4096


class _StderrTail:
    """Bounded rolling tail of a live-drained ffmpeg stderr stream.

    Mutated only by `_drain_stderr_live` (single writer); read only by the
    owning process's crash-watcher after the process has exited. Both run on
    the same asyncio event loop with no `await` between the watcher's read
    and its use, so no lock is needed.
    """

    __slots__ = ("data",)

    def __init__(self, data: bytes = b"") -> None:
        self.data = data


async def _drain_stderr_live(
    stderr: asyncio.StreamReader | None, tail: _StderrTail
) -> None:
    """Continuously read `stderr` for the life of the ffmpeg child.

    Keeps only the last `_STDERR_TAIL_MAX_BYTES` bytes — unbounded buffering
    here would itself be a memory leak on a long-lived continuous recorder
    with lots of warnings. Returns on EOF (process exited, pipe closed) or on
    a broken/closed-stream error — both are ordinary end-of-life, not bugs.
    `asyncio.CancelledError` is intentionally NOT caught here; it must
    propagate so this task can be cancelled cleanly on shutdown like any
    other tracked `bg_tasks` entry.
    """
    if stderr is None:
        return
    try:
        while True:
            chunk = await stderr.read(_STDERR_DRAIN_CHUNK_BYTES)
            if not chunk:
                return
            tail.data = (tail.data + chunk)[-_STDERR_TAIL_MAX_BYTES:]
    except (ValueError, OSError):
        # Reading from an already-closed/broken pipe (ConnectionError and
        # rarer OSError variants like EIO alike) — nothing more to drain.
        return


def _spawn_stderr_drain_task(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    proc: asyncio.subprocess.Process,
    tail: _StderrTail,
    *,
    name_prefix: str,
) -> None:
    """Spawn + track the live stderr-drain task for one ffmpeg child.

    Tracked in `coordinator.bg_tasks` (same discipline as every other
    fire-and-forget task in this module) so integration unload's
    cancel-and-gather sweep covers it too — it otherwise self-terminates on
    EOF the moment the process exits (SIGTERM/SIGKILL close its stderr pipe),
    so explicit cancellation on a normal stop is not required.
    """
    task = coordinator.hass.async_create_background_task(
        _drain_stderr_live(proc.stderr, tail),
        f"{name_prefix}_{cam_id[:8]}",
    )
    coordinator.bg_tasks.add(task)
    task.add_done_callback(coordinator.bg_tasks.discard)


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
    atom. The drain watcher (``drain_staging_to_remote``) picks up files
    only after their mtime has stopped moving, guaranteeing they are complete.
    """
    return os.path.join(base_path, _STAGING_DIRNAME, _safe_name(cam_name))


def _staging_pattern(base_path: str, cam_name: str) -> str:
    """ffmpeg ``-strftime`` output template inside the staging tree."""
    return os.path.join(
        _staging_dir(base_path, cam_name),
        "%Y-%m-%d",
        "%H-%M.mp4",
    )


def _failed_dir(base_path: str, cam_name: str) -> str:
    """Quarantine dir for files that exceeded the upload retry cap."""
    return os.path.join(base_path, _FAILED_DIRNAME, _safe_name(cam_name))


def _remote_smb_path(opts: dict[str, Any], cam_name: str, date: str, fname: str) -> str:
    """Build the SMB destination path for one finalized segment.

    Layout: ``\\\\{server}\\{share}\\{smb_base_path}\\{nvr_smb_subpath}\\{cam}\\{date}\\{fname}``.
    Pure helper — no I/O. Called from the drain watcher per file.
    """
    server = (opts.get("smb_server") or "").strip()
    share = (opts.get("smb_share") or "").strip()
    base = (opts.get("smb_base_path") or "Bosch-Kameras").strip()
    sub = (opts.get("nvr_smb_subpath") or "NVR").strip()
    cam = _safe_name(cam_name)
    return f"\\\\{server}\\{share}\\{base}\\{sub}\\{cam}\\{date}\\{fname}".replace(
        "/", "\\"
    )


def _remote_ftp_path(opts: dict[str, Any], cam_name: str, date: str, fname: str) -> str:
    """Build the FTP destination path for one finalized segment.

    Layout: ``/{smb_base_path}/{nvr_smb_subpath}/{cam}/{date}/{fname}`` — FTP
    has no shares, paths are relative to the FTP login root.
    """
    base = (opts.get("smb_base_path") or "Bosch-Kameras").strip().strip("/")
    sub = (opts.get("nvr_smb_subpath") or "NVR").strip().strip("/")
    cam = _safe_name(cam_name)
    return f"/{base}/{sub}/{cam}/{date}/{fname}".replace("//", "/")


def _apply_quality(rtsp_url: str, quality: str) -> str:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.apply_quality."""
    return _lib_apply_quality(rtsp_url, quality)


def _build_ffmpeg_args(
    rtsp_url: str,
    segment_pattern: str,
    *,
    segment_seconds: int = DEFAULT_SEGMENT_SECONDS,
    quality: str = "auto",
) -> list[str]:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.build_ffmpeg_args."""
    return _lib_build_ffmpeg_args(
        rtsp_url,
        segment_pattern,
        segment_seconds=segment_seconds,
        quality=quality,
    )


# ── Phase 4: pre-roll helpers ─────────────────────────────────────────────────


def _preroll_dir(cache_dir: str, cam_name: str) -> str:
    """Return {cache_dir}/{safe_cam_name}/"""
    return os.path.join(cache_dir, _safe_name(cam_name))


def _preroll_cam_dir(coordinator: BoschCameraCoordinator, cam_id: str) -> str:
    """Resolve one camera's pre-roll cache dir from coordinator options/data.

    Shared by every pre-roll reader/writer (`list_preroll_files`,
    `start_preroll_recorder`, `stop_preroll_recorder`,
    `stop_and_finalize_preroll_recorder`) so the cache_dir/cam_name
    resolution logic lives in exactly one place.
    """
    opts = coordinator.options
    cache_dir = (
        opts.get("nvr_preroll_cache_dir") or "/dev/shm/bosch_nvr_cache"  # noqa: S108 # tmpfs NVR cache default, user-overridable via options
    ).strip()
    cam_name = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    return _preroll_dir(cache_dir, cam_name)


def _preroll_pattern(cache_dir: str, cam_name: str) -> str:
    """strftime pattern for pre-roll 10 s segments in tmpfs."""
    return os.path.join(_preroll_dir(cache_dir, cam_name), "%H%M%S.mp4")


def _newest_preroll_path(cam_dir: str) -> str | None:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.newest_preroll_path.

    Runs inside an executor job (filesystem I/O). Used by
    `stop_and_finalize_preroll_recorder` to identify the ring's actively-
    written segment *before* stopping it, so the now-finalized file can be
    handed back to the caller once the stop confirms a clean ffmpeg exit.
    """
    return _lib_newest_preroll_path(cam_dir)


def _list_preroll_segments(cam_dir: str) -> list[tuple[str, float]]:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.list_preroll_segments."""
    return _lib_list_preroll_segments(cam_dir)


def prune_preroll_cache(cam_dir: str, max_segments: int) -> int:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.prune_preroll_cache."""
    return _lib_prune_preroll_cache(cam_dir, max_segments)


def _prune_and_count(cam_dir: str, max_segments: int) -> int:
    """Prune then return remaining segment count. Runs inside an executor job.

    Deliberately calls this module's own `prune_preroll_cache`/
    `_list_preroll_segments` wrappers rather than the library's
    `prune_and_count` directly — several tests patch
    `recorder.prune_preroll_cache` to intercept pruning, which only works
    if the call stays routed through this module's own namespace.
    """
    prune_preroll_cache(cam_dir, max_segments)
    return len(_list_preroll_segments(cam_dir))


def _build_preroll_ffmpeg_args(
    rtsp_url: str, pattern: str, *, quality: str = "auto"
) -> list[str]:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.build_preroll_ffmpeg_args."""
    return _lib_build_preroll_ffmpeg_args(
        rtsp_url,
        pattern,
        segment_seconds=_PREROLL_SEGMENT_SECONDS,
        quality=quality,
    )


async def _watch_preroll_recorder(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    cam_dir: str,
    max_segs: int,
) -> None:
    """Periodic prune loop — keeps the pre-roll ring buffer bounded while running.

    Fires every _PREROLL_SEGMENT_SECONDS (10 s) and discards the oldest
    segments so the buffer never grows past max_segs × 10 s. Exits cleanly
    when the process exits or is cancelled.

    The prune executor job runs under the same per-camera
    ``get_nvr_recorder_lock`` that ``create_motion_clip``
    holds while listing+staging segments for a clip. `asyncio.Task.cancel()`
    (used by `stop_preroll_recorder`) cannot abort a prune job already
    dispatched to the executor thread pool — only the lock makes the two
    genuinely mutually exclusive, closing the race where a listed segment
    got pruned out from under ffmpeg's concat demuxer moments later.
    """
    while True:
        try:
            await asyncio.sleep(_PREROLL_SEGMENT_SECONDS)
        except asyncio.CancelledError:
            raise
        proc = coordinator.nvr_preroll_processes.get(cam_id)
        if proc is None or proc.returncode is not None:
            return
        try:
            async with coordinator.get_nvr_recorder_lock(cam_id):
                remaining = await coordinator.hass.async_add_executor_job(
                    _prune_and_count,
                    cam_dir,
                    max_segs,
                )
                coordinator.nvr_preroll_segment_counts[cam_id] = remaining
        except Exception:  # best-effort prune-on-stop; non-fatal if cache dir missing  # noqa: S110 # best-effort cache prune, non-fatal if dir missing
            pass


async def _watch_preroll_health(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    proc: asyncio.subprocess.Process,
    stderr_tail: _StderrTail,
) -> None:
    """Detect an unexpected pre-roll ring ffmpeg exit and respawn it.

    Unlike the main recorder (`_watch_recorder`), `_watch_preroll_recorder`'s
    periodic prune loop only checks `proc.returncode` once every
    `_PREROLL_SEGMENT_SECONDS` and silently stops pruning (never popping the
    dead process handle, never logging, never respawning) once it notices
    the process is gone — so without this watcher a ring that dies mid-idle
    (e.g. a non-monotonic-DTS abort from a flaky camera) stays dead
    indefinitely until something UNRELATED happens to respawn it (a motion
    event, a mode-select toggle, a session renewal).

    Mirrors `_watch_recorder`'s crash-window/backoff discipline (same
    `_RESPAWN_DELAY_SECONDS`/`_RESPAWN_WINDOW_SECONDS`) but tracks its own
    crash timestamps in the dedicated `_nvr_preroll_last_crash` field so the
    ring's crash-loop tracking can never clobber the main recorder's
    `nvr_recent_crash`.

    Uses the same "am I still the tracked process?" identity check as
    `_watch_recorder` to distinguish an intentional stop/replacement (no
    action) from a genuine crash — `stop_preroll_recorder` always pops the
    process from `nvr_preroll_processes` BEFORE signalling it, so by the
    time any exit is observed here the dict entry has either already moved
    on (intentional) or still points at this exact `proc` (crash).
    """
    rc = await proc.wait()
    if coordinator.nvr_preroll_processes.get(cam_id) is not proc:
        return
    coordinator.nvr_preroll_processes.pop(cam_id, None)
    coordinator.nvr_preroll_segment_counts.pop(cam_id, None)

    # `stderr_tail` was populated live by `_drain_stderr_live` for the whole
    # life of the process (see GitHub #64) — by the time we get here the
    # pipe may already be closed/empty, so a post-exit read is no longer the
    # right source of diagnostic data.
    err_tail = stderr_tail.data.decode("utf-8", errors="replace").strip()

    _LOGGER.warning(
        "NVR pre-roll ring ffmpeg exited unexpectedly rc=%s for %s — pre-roll "
        "coverage stopped accumulating. Tail: %s",
        rc,
        cam_id[:8],
        err_tail[-500:] if err_tail else "(no stderr)",
    )

    if getattr(coordinator, "nvr_shutting_down", False):
        return

    last = getattr(coordinator, "nvr_user_intent", {}).get(cam_id, False)
    if not should_record(coordinator, cam_id, switch_on=last):
        _LOGGER.info("NVR pre-roll not respawning for %s — gate now closed", cam_id[:8])
        return

    now = time.monotonic()
    prev_crash = coordinator._nvr_preroll_last_crash.get(
        cam_id, float("-inf")
    )  # coordinator-owned per-cam crash tracker, recorder module is its only writer
    if (now - prev_crash) < _RESPAWN_WINDOW_SECONDS:
        # Neither a motion event nor an NVR-mode change
        # actually revives a fully-dead ring — `assemble_and_ship_motion_clip`
        # only restarts the ring after a *live* finalize, and the mode-select
        # respawn (`select.py`) only refreshes an *already-running* recorder.
        # The only automatic recovery is a LOCAL session (re)establishing —
        # don't tell the operator to wait on paths that won't help.
        _LOGGER.error(
            "NVR pre-roll ring crashed twice within %.0fs for %s — giving up "
            "automatic respawn for now. It will resume once the camera's "
            "LOCAL session is (re)established (e.g. a reconnect); toggle "
            "the recording switch off+on to retry immediately.",
            _RESPAWN_WINDOW_SECONDS,
            cam_id[:8],
        )
        # Mirror the main recorder's give-up discipline (nvr_error_state +
        # listener push) so this is visible in the UI, not log-only —
        # otherwise the mini_nvr_state sensor's `error` attribute and the
        # recording switch's `last_error` attribute stay blank with the
        # ring permanently dead.
        coordinator.nvr_error_state[cam_id] = "pre-roll ring crashed twice"
        coordinator.async_update_listeners()
        return
    coordinator._nvr_preroll_last_crash[cam_id] = (
        now  # coordinator-owned per-cam crash tracker, recorder module is its only writer
    )

    await asyncio.sleep(_RESPAWN_DELAY_SECONDS)
    if getattr(coordinator, "nvr_shutting_down", False):
        return
    # Re-read intent — `last` above was captured before the sleep; a switch
    # toggled off DURING the backoff must still be honored, not just a
    # stale pre-sleep snapshot (bug caught by this fix's own test suite).
    last = getattr(coordinator, "nvr_user_intent", {}).get(cam_id, False)
    if not should_record(coordinator, cam_id, switch_on=last):
        return
    _LOGGER.info(
        "NVR pre-roll ring respawning for %s after transient crash", cam_id[:8]
    )
    try:
        await start_preroll_recorder(coordinator, cam_id)
    except Exception as respawn_err:
        # An unexpected exception here would otherwise kill this
        # health-watch task silently, permanently disabling the pre-roll
        # ring's own crash recovery (ironic: this function exists
        # specifically to close that class of gap).
        _LOGGER.error(
            "NVR pre-roll respawn raised unexpectedly for %s: %s",
            cam_id[:8],
            respawn_err,
        )
        coordinator.nvr_error_state[cam_id] = "pre-roll respawn failed unexpectedly"
        coordinator.async_update_listeners()


async def _spawn_preroll_recorder_locked(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Spawn the pre-roll ffmpeg ring writer for one camera to tmpfs.

    Callers MUST already hold ``coordinator.get_nvr_recorder_lock(cam_id)``
    — factored out of `start_preroll_recorder` so other already-locked
    callers (`start_recorder`'s own locked body,
    `restart_preroll_recorder_after_finalize`) can respawn the ring without
    releasing the lock between their own stop/decision and this spawn
    (releasing it would reopen a double-spawn race).

    The lock alone only prevents two callers from racing to spawn AT THE
    SAME instant — it does NOT prevent a double-spawn across two SEPARATE
    lock acquisitions with a gap in between. `assemble_and_ship_motion_clip`
    releases and re-acquires this same lock three times (finalize → an
    unlocked live postroll capture that can run for the full configured
    duration → concat → restart), and nothing else this function's
    docstring assumes holds true across that whole window prevents an
    UNRELATED trigger (heartbeat cred-rotation restart, a LOCAL session
    renewal, a rapid switch re-toggle) from acquiring the lock in one of
    those gaps and spawning its own ring via `start_recorder`. This
    idempotency guard is the actual belt-and-suspenders fix: regardless of
    which trigger pair races, never spawn a second ring writer while one is
    still alive for this camera.
    """
    existing = coordinator.nvr_preroll_processes.get(cam_id)
    if existing is not None and existing.returncode is None:
        _LOGGER.debug(
            "NVR pre-roll spawn skipped for %s — a ring writer is already "
            "running (pid=%s)",
            cam_id[:8],
            existing.pid,
        )
        return
    if coordinator.get_nvr_mode(cam_id) != "event_buffered":
        # `_start_recorder_locked` doesn't spawn the ring in continuous
        # mode, but this function has two OTHER callers that don't go
        # through that gate — `_watch_preroll_health`'s crash-respawn
        # (after its `_RESPAWN_DELAY_SECONDS` sleep) and
        # `restart_preroll_recorder_after_finalize` (after a possibly
        # long-running postroll capture) — either of which can fire after
        # the camera's mode already flipped to `continuous` mid-wait,
        # resurrecting the "second full-bandwidth ffmpeg consumer" the mode
        # gate exists to prevent, just via a race instead of
        # unconditionally. Re-check the mode here, the single choke point
        # both callers share with the normal path.
        _LOGGER.debug(
            "NVR pre-roll spawn skipped for %s — mode is not event_buffered",
            cam_id[:8],
        )
        return
    if getattr(coordinator, "nvr_shutting_down", False):
        # Config-entry unload/HA-stop is tearing this coordinator down —
        # refuse to spawn a new ring writer that stop_all_preroll()'s
        # sweep, running concurrently, might not see.
        _LOGGER.debug(
            "NVR pre-roll spawn skipped for %s — coordinator shutting down",
            cam_id[:8],
        )
        return
    live = coordinator.live_connections.get(cam_id, {})
    conn_type = live.get("_connection_type")
    if conn_type != "LOCAL":
        # GitHub #64: this re-read is a SEPARATE snapshot of
        # `coordinator.live_connections` from whatever the caller already
        # checked — `_start_recorder_locked`'s own LOCAL/rtsp_url gate runs
        # under this same `get_nvr_recorder_lock`, but `live_connection.py`'s
        # writers (`try_live_connection_inner`) mutate `live_connections`
        # WITHOUT holding that lock, so the connection can genuinely have
        # flipped (or never been LOCAL to begin with, e.g. an AUTO-mode
        # REMOTE fallback) by the time this function's two OTHER callers
        # (`_watch_preroll_health`'s crash-respawn after its backoff sleep,
        # `restart_preroll_recorder_after_finalize` after a possibly
        # long-running live postroll capture) reach here. Both of those
        # callers have a genuine await gap before this read; the direct
        # `_start_recorder_locked` path does not (single uninterrupted
        # synchronous stretch under the lock). Previously silent — this was
        # the single biggest blind spot reported in #64 (cache dir stayed
        # completely empty with zero log trace of why the ring never spawned).
        _LOGGER.debug(
            "NVR pre-roll spawn skipped for %s: connection type is %r, expected LOCAL",
            cam_id[:8],
            conn_type,
        )
        return
    rtsp_url = live.get("rtspsUrl") or live.get("rtspUrl") or ""
    if not rtsp_url.startswith("rtsp://"):
        _LOGGER.debug(
            "NVR pre-roll spawn skipped for %s: no valid rtsp URL (got %r)",
            cam_id[:8],
            rtsp_url,
        )
        return

    opts = coordinator.options
    cache_dir = (
        opts.get("nvr_preroll_cache_dir") or "/dev/shm/bosch_nvr_cache"  # noqa: S108 # tmpfs NVR cache default, user-overridable via options
    ).strip()
    cam_name = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    cam_dir = _preroll_dir(cache_dir, cam_name)
    try:
        await coordinator.hass.async_add_executor_job(
            os.makedirs,
            cam_dir,
            0o755,
            True,
        )
    except OSError as err:
        _LOGGER.warning(
            "NVR pre-roll: cannot create cache dir for %s: %s", cam_name, err
        )
        return

    pattern = _preroll_pattern(cache_dir, cam_name)
    quality = (opts.get("nvr_quality") or "auto").strip().lower()
    args = _build_preroll_ffmpeg_args(rtsp_url, pattern, quality=quality)
    _LOGGER.debug("NVR pre-roll starting for %s -> %s", cam_name, pattern)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        _LOGGER.error("NVR pre-roll: ffmpeg not found on PATH")
        return
    except OSError as err:
        _LOGGER.warning("NVR pre-roll ffmpeg spawn failed for %s: %s", cam_name, err)
        return

    coordinator.nvr_preroll_processes[cam_id] = proc
    stderr_tail = _StderrTail()
    _spawn_stderr_drain_task(
        coordinator,
        cam_id,
        proc,
        stderr_tail,
        name_prefix="bosch_nvr_preroll_stderr_drain",
    )
    # Compute max_segs once; used for prune-on-spawn and periodic watcher.
    preroll_secs = int(opts.get("nvr_preroll_seconds", 0))
    max_segs = max(2, math.ceil(preroll_secs / _PREROLL_SEGMENT_SECONDS) + 1)
    # Prune on spawn so stale segments from a previous session don't inflate the buffer.
    try:
        remaining = await coordinator.hass.async_add_executor_job(
            _prune_and_count,
            cam_dir,
            max_segs,
        )
        coordinator.nvr_preroll_segment_counts[cam_id] = remaining
    except Exception:  # best-effort prune-on-spawn; non-fatal if cache dir missing  # noqa: S110 # best-effort cache prune, non-fatal if dir missing
        pass
    # Sweep any orphaned clip-assembly stage dirs left by a hard-killed
    # process — same cadence as the prune above, harmless no-op when
    # there's nothing to sweep.
    try:
        await coordinator.hass.async_add_executor_job(
            _sweep_orphaned_stage_dirs, cam_dir
        )
    except Exception:  # best-effort orphan sweep; non-fatal if cache dir missing  # noqa: S110 # best-effort orphan sweep, non-fatal if dir missing
        pass

    # Start periodic prune watcher — keeps the ring buffer bounded while running.
    if not hasattr(coordinator, "nvr_preroll_tasks"):
        coordinator.nvr_preroll_tasks = {}
    task = coordinator.hass.async_create_background_task(
        _watch_preroll_recorder(coordinator, cam_id, cam_dir, max_segs),
        f"bosch_nvr_preroll_watch_{cam_id[:8]}",
    )
    coordinator.bg_tasks.add(task)
    task.add_done_callback(coordinator.bg_tasks.discard)
    coordinator.nvr_preroll_tasks[cam_id] = task

    # Start crash-detect/respawn watcher — one per spawned proc, not
    # tracked in a dict (nothing needs to look it up/cancel it later: its
    # own "am I still the tracked process?" identity check on wake is what
    # makes an intentional stop a no-op, same discipline as `_watch_recorder`
    # for the main recorder).
    health_task = coordinator.hass.async_create_background_task(
        _watch_preroll_health(coordinator, cam_id, proc, stderr_tail),
        f"bosch_nvr_preroll_health_{cam_id[:8]}",
    )
    coordinator.bg_tasks.add(health_task)
    health_task.add_done_callback(coordinator.bg_tasks.discard)


async def start_preroll_recorder(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Spawn parallel pre-roll ffmpeg for one camera to tmpfs.

    Serialized on the same per-camera lock the main recorder spawn uses —
    unlike `start_recorder`'s spawn, without it two concurrent callers
    (switch turn-on, the stream-up hook, and the NVR mode select can all
    reach this for the same camera) could each pass the leading
    stop-then-spawn sequence, and the loser's process handle would get
    overwritten in `nvr_preroll_processes`, leaking an untracked second
    ffmpeg ring writer that interleaves segments with the first.
    `start_recorder` releases this lock before calling here, so holding it
    for this whole function cannot deadlock.
    """
    async with coordinator.get_nvr_recorder_lock(cam_id):
        # This is a respawn (fresh creds / restart), not a genuine stop —
        # keep the accumulated ring buffer instead of wiping it (see
        # prune_cache docstring on stop_preroll_recorder).
        await stop_preroll_recorder(coordinator, cam_id, prune_cache=False)
        await _spawn_preroll_recorder_locked(coordinator, cam_id)


async def stop_preroll_recorder(
    coordinator: BoschCameraCoordinator, cam_id: str, *, prune_cache: bool = True
) -> bool:
    """Stop pre-roll recorder for one camera and clear its tmpfs ring cache.

    Leftover segments from the just-stopped ring buffer are unlinked so they
    don't sit in ``/dev/shm`` until the next ``start_preroll_recorder()``
    happens to overwrite them.

    ``prune_cache=False`` is used by ``start_preroll_recorder``'s own leading
    self-call (a respawn, e.g. LOCAL session/cred-rotation renewal) so the
    ring buffer keeps its accumulated context across a restart instead of
    being wiped to empty every renewal — an unconditional wipe here would
    fire on every renewal (via ``start_recorder``'s own leading
    ``stop_recorder`` call), not just genuine stops, defeating the
    pre-roll buffer's purpose.

    Returns True iff a running process exited on SIGTERM within the grace
    window (i.e. ffmpeg finalized its own output cleanly, moov atom
    included). Returns False if there was nothing to stop, the process was
    already dead, or it had to be force-killed — `hard-kill` gives no
    guarantee the last-open segment file has a valid moov atom, so callers
    (`stop_and_finalize_preroll_recorder`) must treat False as "don't
    trust the newest segment file".
    """
    # Cancel the periodic prune watcher first.
    tasks = getattr(coordinator, "nvr_preroll_tasks", {})
    watcher = tasks.pop(cam_id, None)
    if watcher is not None and not watcher.done():
        watcher.cancel()

    coordinator.nvr_preroll_segment_counts.pop(cam_id, None)
    proc = coordinator.nvr_preroll_processes.pop(cam_id, None)
    clean_exit = False
    if proc is not None and proc.returncode is None:
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            proc = None
        else:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_STOP_GRACE_SECONDS)
                clean_exit = True
            except TimeoutError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(
                        proc.wait(), timeout=TIMEOUT_RECORDER_KILL_WAIT
                    )
                except TimeoutError:
                    pass

    if not prune_cache:
        return clean_exit

    cam_dir = _preroll_cam_dir(coordinator, cam_id)
    try:
        await coordinator.hass.async_add_executor_job(prune_preroll_cache, cam_dir, 0)
    except Exception:  # best-effort cleanup; non-fatal if cache dir missing  # noqa: S110 # best-effort ring cleanup on stop, non-fatal if dir missing
        pass
    return clean_exit


def _known_cam_ids_for_shutdown(coordinator: BoschCameraCoordinator) -> set[str]:
    """All camera IDs that could plausibly have (or soon get) an NVR/ring
    ffmpeg process — used by the unload-time sweeps below.

    A plain ``list(coordinator.nvr_processes.keys())`` snapshot misses a
    camera whose ``start_recorder``/``_spawn_preroll_recorder_locked`` call
    is still in flight and hasn't registered its process yet at snapshot
    time, leaking its ffmpeg past shutdown. Including every
    currently-configured camera (not just
    ones with an already-tracked process) means the per-cam
    ``get_nvr_recorder_lock`` acquire in ``stop_all``/``stop_all_preroll``
    below will still serialize against — and thus catch — that in-flight
    spawn once it finishes registering.
    """
    cam_ids = set(coordinator.nvr_processes) | set(coordinator.nvr_preroll_processes)
    cam_ids |= set(getattr(coordinator, "camera_entities", {}) or {})
    return cam_ids


async def stop_all_preroll(coordinator: BoschCameraCoordinator) -> None:
    """Stop all pre-roll recorders — called on integration unload.

    Serializes each camera on `get_nvr_recorder_lock` so a
    `start_preroll_recorder`/`_spawn_preroll_recorder_locked` call that is
    still in flight when unload begins cannot race this sweep: it either
    hasn't acquired the lock yet (and will observe `nvr_shutting_down` and
    bail once it does), or already holds the lock and finishes registering
    into `nvr_preroll_processes` before this loop's own acquire for that
    camera unblocks and sees the freshly-spawned process.
    """
    for cam_id in _known_cam_ids_for_shutdown(coordinator):
        async with coordinator.get_nvr_recorder_lock(cam_id):
            await stop_preroll_recorder(coordinator, cam_id)


async def stop_and_finalize_preroll_recorder(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> tuple[bool, list[str]]:
    """Stop the ring for a motion-clip assembly, returning every segment
    that's safe to concatenate. Does NOT restart the ring — see
    `restart_preroll_recorder_after_finalize`, called by the caller only
    AFTER the clip has been built from this function's returned list.

    `list_preroll_files()`'s "always drop the newest file" heuristic
    exists because the ring's
    actively-written segment may lack a moov atom yet — correct while the
    ring is running, but wrong once it's genuinely stopped: everything on
    disk at that point is either complete, or (if we had to hard-kill) the
    one specific file that was open at kill time, which we already know
    the path of. Two real bugs this replaces:

    1. On a hard-kill, the old implementation returned `None` and the
       caller fell all the way back to `list_preroll_files()`'s normal
       drop-newest scan against the (still ring-stopped!) directory — that
       scan doesn't know the ring already stopped, so it dropped a SECOND,
       perfectly good segment on top of the untrustworthy one, doubling the
       footage loss (the report's ~26 s bimodal case).
    2. Even on a CLEAN exit, the old implementation restarted the ring
       (`_spawn_preroll_recorder_locked`) *before* the caller's own
       `list_preroll_files()` scan ran moments later for the clip assembly.
       That scan's drop-newest heuristic would then drop whatever the
       freshly-restarted ring had already written (often nothing yet — its
       first file was still under `_PREROLL_MIN_SIZE_BYTES` and thus
       invisible to the scan entirely), so it dropped a real, older,
       already-complete segment instead of the throwaway new one — costing
       a segment even on the "successful" path.

    Both are fixed by the same restructuring: this function returns a
    stable, already-correct list with the ring genuinely stopped, and the
    caller uses that list directly (no second scan, no drop-newest against
    a directory whose "actively written" file no longer exists) before
    restarting the ring itself once the clip is done.

    Runs under the same per-camera lock `start_preroll_recorder`/
    `stop_preroll_recorder` use so no other caller can race in between. If
    the ring writer isn't running
    (never started, or already crashed) returns `(False, [])` — nothing to
    finalize, caller should fall back to whatever pre-roll segments already
    exist via the normal `list_preroll_files()` path.

    Returns `(ring_was_running, safe_segment_paths)`. When `ring_was_running`
    is True, `safe_segment_paths` is the authoritative, oldest-first list
    to hand to `create_motion_clip`'s `preroll_paths_override` — already
    complete, no further filtering needed.
    """
    async with coordinator.get_nvr_recorder_lock(cam_id):
        proc = coordinator.nvr_preroll_processes.get(cam_id)
        if proc is None or proc.returncode is not None:
            return False, []
        cam_dir = _preroll_cam_dir(coordinator, cam_id)
        newest = await coordinator.hass.async_add_executor_job(
            _newest_preroll_path, cam_dir
        )
        if newest is None:
            # Ring is alive but hasn't produced any segment yet — nothing
            # to gain from stopping+restarting a perfectly healthy writer.
            return False, []
        clean_exit = await stop_preroll_recorder(coordinator, cam_id, prune_cache=False)

        # Ring is now genuinely stopped — every remaining file is complete
        # EXCEPT possibly `newest`, which lacks a moov-atom guarantee if we
        # had to hard-kill instead of a clean SIGTERM exit.
        segs = await coordinator.hass.async_add_executor_job(
            _list_preroll_segments, cam_dir
        )
        paths = [p for p, _ in segs]
        if newest is not None and not clean_exit:
            cam_name = (
                coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
            )
            _LOGGER.debug(
                "NVR pre-roll finalize: hard-killed for %s, discarding untrusted segment %s",
                cam_name,
                newest,
            )
            paths = [p for p in paths if p != newest]
            try:
                await coordinator.hass.async_add_executor_job(os.unlink, newest)
            except OSError:
                pass
        return True, paths


async def restart_preroll_recorder_after_finalize(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Respawn the pre-roll ring after `stop_and_finalize_preroll_recorder`.

    Deliberately its own step — called only after the caller
    has already built the motion clip from the stable, ring-stopped segment
    list, so a freshly-restarted ring's own in-flight segment can never
    shadow that scan.
    """
    async with coordinator.get_nvr_recorder_lock(cam_id):
        await _spawn_preroll_recorder_locked(coordinator, cam_id)


def list_preroll_files(coordinator: BoschCameraCoordinator, cam_id: str) -> list[str]:
    """Return list of pre-roll segment paths for cam_id, sorted oldest-first,
    safe to hand to `create_motion_clip`'s concat demuxer.

    The ring writer's ffmpeg `-f segment` process keeps exactly one file
    open at a time — the newest file on disk may still be mid-write with no
    finalized moov atom yet (it reaches the size threshold almost
    immediately after rotation, well before the 10 s segment period ends).
    Concatenating it produces a corrupt/failing clip — always drop the
    newest entry here rather than risk shipping a broken assembled clip.
    Costs at most one ~10 s segment of the freshest pre-roll footage.

    This "always drop newest" heuristic is only correct while the ring is
    still actively writing. `stop_and_finalize_preroll_recorder` genuinely
    stops the ring first, so it bypasses this function
    entirely and reads `_list_preroll_segments` directly — applying this
    drop-newest logic to an already-stopped ring's directory would drop a
    real, complete segment for no reason.
    """
    cam_dir = _preroll_cam_dir(coordinator, cam_id)
    paths = [path for path, _ in _list_preroll_segments(cam_dir)]
    return paths[:-1] if paths else paths


async def _newest_segment_is_finalized(path: str) -> bool:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.newest_segment_is_finalized.

    The post-roll scan always drops the newest ring segment on the
    assumption it might still be mid-write (concatenating a moov-less file
    produces a broken clip) unless this proves the segment already has a
    finalized moov atom.
    """
    return await _lib_newest_segment_is_finalized(
        path,
        timeout_probe=TIMEOUT_RECORDER_SEGMENT_PROBE,
        timeout_kill_wait=TIMEOUT_RECORDER_KILL_WAIT,
    )


def create_motion_clip_args(preroll_paths: list[str], output_path: str) -> list[str]:
    """Thin wrapper around bosch_shc_camera_client.mini_nvr.create_motion_clip_args.

    `preroll_paths` is unused here (kept for backward compatibility with
    existing call sites/tests) — the actual concat-list file contents are
    built by `create_motion_clip` before this returns its ffmpeg argv, same
    as before this was extracted.
    """
    del preroll_paths
    return _lib_create_motion_clip_args(output_path)


def _stage_segments_for_concat(paths: list[str], stage_dir: str) -> list[str]:
    """Hardlink each segment into a private ``stage_dir`` before concat.

    Listing segments and later opening those
    exact paths in ffmpeg's concat demuxer are two separate moments in time;
    a concurrent ring prune/rotate/respawn could unlink one of them in
    between, aborting the whole clip (rc=254 "Impossible to open"). A
    hardlink is effectively free on tmpfs and keeps the data alive under the
    staged name even if the original directory entry is deleted moments
    later — so the concat step is immune to whatever happens to the
    originals after this point.

    Runs inside an executor job (filesystem I/O). A segment that has
    already vanished by the time we try to link it (a far tighter race than
    the one this closes — listing and staging are consecutive lines of
    code) is silently skipped rather than aborting the whole clip; the
    caller ships whatever segments survived instead of losing the entire
    event over one missing 10 s slice.
    """
    try:
        os.makedirs(stage_dir, exist_ok=True)
    except OSError:
        return []
    staged: list[str] = []
    for i, src in enumerate(paths):
        dst = os.path.join(stage_dir, f"{i:04d}_{os.path.basename(src)}")
        try:
            os.link(src, dst)
        except OSError:
            continue
        staged.append(dst)
    return staged


def _cleanup_stage_dir(stage_dir: str) -> None:
    """Remove every hardlink plus the directory `_stage_segments_for_concat`
    created. Runs inside an executor job; best-effort, never raises."""
    try:
        for name in os.listdir(stage_dir):
            try:
                os.unlink(os.path.join(stage_dir, name))
            except OSError:
                pass
        os.rmdir(stage_dir)
    except OSError:
        pass


def _sweep_orphaned_stage_dirs(cam_dir: str) -> None:
    """Remove `_stage/<clip>` subdirectories left behind by a process that
    was hard-killed between `_stage_segments_for_concat` staging a clip's
    segments and its own `finally` block running `_cleanup_stage_dir`
    (that `finally` covers every normal exit path, but not SIGKILL/OOM).
    tmpfs only clears these on a full OS reboot, not an HA restart, so left
    unswept they'd accumulate forever
    across repeated hard kills.

    Age-gated at `_STAGE_ORPHAN_MAX_AGE_SECONDS` — well past any realistic
    assembly duration — so an in-flight concurrent event's own seconds-old
    stage dir (a session renewal can respawn the ring while another
    camera's — or even this one's — assembly is still mid-flight) is never
    touched. Runs inside an executor job at ring spawn time (same cadence
    as the pre-existing prune-on-spawn step); best-effort, never raises.
    """
    stage_root = os.path.join(cam_dir, "_stage")
    try:
        entries = os.listdir(stage_root)
    except OSError:
        return
    now = time.time()
    for name in entries:
        path = os.path.join(stage_root, name)
        try:
            if not os.path.isdir(path):
                continue
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if age < _STAGE_ORPHAN_MAX_AGE_SECONDS:
            continue
        _cleanup_stage_dir(path)


async def create_motion_clip(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    output_path: str,
    *,
    extra_segments: list[str] | None = None,
    preroll_paths_override: list[str] | None = None,
) -> bool:
    """Concatenate available pre-roll (+post-roll) segments into output_path.

    ``extra_segments`` (optional) are appended, in order, after the pre-roll
    segments — the post-roll tail derived from the still-running pre-roll
    ring, so a single clip covers both sides of the event without a second
    ffmpeg pass or a second RTSP session. Returns True on success.

    ``preroll_paths_override`` (optional): when the caller already has an
    authoritative, ring-genuinely-stopped segment list (from
    `stop_and_finalize_preroll_recorder`), use it directly instead of
    re-scanning via `list_preroll_files()` — that scan's "always drop the
    newest file" heuristic is only correct while the ring is still actively
    writing; applying it again to an already-finalized list would drop a
    real, complete segment for no reason.

    Obtaining the segment list (either path above) and staging it into a
    private hardlink dir both run under ``get_nvr_recorder_lock`` — the
    same lock the periodic prune watcher and any ring stop/respawn use —
    so nothing can prune/rotate/wipe a segment between "we decided to use
    this file" and "it's safely hardlinked". ``extra_segments`` is staged
    under the SAME lock for the same reason: now that the post-roll tail
    is read straight out of the live ring
    directory instead of a private capture file, it's exposed to the same
    prune-race the pre-roll segments already were. This also covers the
    override path: a finalized list is only stable at the moment it was
    built, not for however long the caller takes to reach this function
    (e.g. an integration unload racing in in between would otherwise wipe
    the whole cache dir, `stop_all_preroll` → `prune_cache`). The lock is
    released again before the (potentially slower) ffmpeg concat itself
    runs.
    """
    async with coordinator.get_nvr_recorder_lock(cam_id):
        if preroll_paths_override is not None:
            preroll_paths = list(preroll_paths_override)
        else:
            preroll_paths = await coordinator.hass.async_add_executor_job(
                list_preroll_files,
                coordinator,
                cam_id,
            )

        source_paths = [*(preroll_paths or []), *(extra_segments or [])]
        staged_paths: list[str] = []
        stage_dir: str | None = None
        if source_paths:
            stage_dir = os.path.join(
                os.path.dirname(source_paths[0]),
                "_stage",
                os.path.splitext(os.path.basename(output_path))[0],
            )
            staged_paths = await coordinator.hass.async_add_executor_job(
                _stage_segments_for_concat,
                source_paths,
                stage_dir,
            )
            if len(staged_paths) < len(source_paths):
                _LOGGER.debug(
                    "NVR motion clip: %d/%d segment(s) vanished before "
                    "staging for %s (ring pruned/rotated concurrently) — "
                    "continuing with the rest",
                    len(source_paths) - len(staged_paths),
                    len(source_paths),
                    cam_id[:8],
                )

    paths = staged_paths
    if not paths:
        _LOGGER.debug("NVR motion clip: no pre-roll segments for %s", cam_id[:8])
        if stage_dir is not None:
            await coordinator.hass.async_add_executor_job(_cleanup_stage_dir, stage_dir)
        return False

    concat_file = output_path + ".concat.txt"
    concat_content = "\n".join(f"file '{p}'" for p in paths) + "\n"

    def _write_concat() -> None:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(concat_file, "w", encoding="utf-8") as f:
            f.write(concat_content)

    try:
        try:
            await coordinator.hass.async_add_executor_job(_write_concat)
        except OSError as err:
            _LOGGER.warning("NVR motion clip: cannot write concat file: %s", err)
            return False

        args = create_motion_clip_args(paths, output_path)
        _LOGGER.debug(
            "NVR motion clip for %s: %d segments -> %s",
            cam_id[:8],
            len(paths),
            output_path,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            _LOGGER.error("NVR motion clip: ffmpeg not found on PATH")
            return False
        except OSError as err:
            _LOGGER.warning("NVR motion clip: ffmpeg spawn failed: %s", err)
            return False

        try:
            _, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=TIMEOUT_RECORDER_FFMPEG_INIT
            )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            _LOGGER.warning("NVR motion clip: ffmpeg timed out for %s", cam_id[:8])
            return False

        if proc.returncode != 0:
            tail = (
                (stderr_bytes or b"").decode("utf-8", errors="replace").strip()[-300:]
            )
            _LOGGER.warning(
                "NVR motion clip: ffmpeg rc=%d for %s. Tail: %s",
                proc.returncode,
                cam_id[:8],
                tail,
            )
            return False

        # rc=0 doesn't guarantee clean timing — a
        # mid-ring reconnect can still leave a visible glitch at a segment
        # boundary. Not treated as a failure (the clip is normally still
        # watchable), just surfaced instead of being silently invisible.
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").lower()
        if any(marker in stderr_text for marker in _CONCAT_DISCONTINUITY_MARKERS):
            _LOGGER.warning(
                "NVR motion clip shipped for %s but ffmpeg reported a "
                "timing discontinuity while concatenating (possible glitch "
                "at a segment boundary): %s",
                cam_id[:8],
                (stderr_bytes or b"").decode("utf-8", errors="replace").strip()[-300:],
            )
        return True
    finally:
        try:
            await coordinator.hass.async_add_executor_job(os.unlink, concat_file)
        except OSError:
            pass
        if stage_dir is not None:
            await coordinator.hass.async_add_executor_job(_cleanup_stage_dir, stage_dir)


# ── Phase 5: post-roll capture + event→clip assembly ────────────────────────


async def assemble_and_ship_motion_clip(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> bool:
    """On an FCM motion/person event, assemble a pre-roll(+post-roll) clip
    for a camera running in `event_buffered` Mini-NVR mode and drop it into
    the staging tree so the existing drain watcher promotes/uploads it like
    any continuous-mode segment.

    Guarded by a per-camera lock so overlapping FCM events don't race the
    concat-file write for the same camera; if an assembly is already in
    flight for this camera the new one is skipped rather than queued (a
    burst of events during one ongoing motion episode should not pile up
    redundant, mostly-overlapping clips). Returns True iff a clip was
    written to staging.

    No-ops (returns False without touching the ring buffer at all) if the
    per-camera ``nvr_event_clip`` switch has been turned off — an opt-out
    for installs that orchestrate their own clip-saving externally (e.g.
    via HA automations) and don't want a second, native clip produced on
    every event on top of their own. The underlying pre-roll ring keeps
    running for such installs' own consumers; only this native assembly is
    skipped.
    """
    if not coordinator.get_nvr_event_clip_enabled(cam_id):
        _LOGGER.debug(
            "NVR motion clip: native event-clip assembly disabled for %s, skipping",
            cam_id[:8],
        )
        return False

    lock = coordinator.get_nvr_clip_assembly_lock(cam_id)
    if lock.locked():
        _LOGGER.debug(
            "NVR motion clip: assembly already in progress for %s, skipping",
            cam_id[:8],
        )
        return False

    async with lock:
        opts = coordinator.options
        base_path = (opts.get("nvr_base_path") or DEFAULT_BASE_PATH).strip()
        cam_name = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
        # Local (system/container) time, NOT UTC — continuous-mode's
        # segment writer uses ffmpeg -strftime, which
        # always renders in the local system timezone. Naming this clip in
        # UTC put it hours away from the continuous-mode segments in the
        # same dated folder, sorting out of order relative to the actual
        # event. `start_recorder`'s own date-dir pre-creation already uses
        # `datetime.date.today()` (local) for the same reason — matching it
        # here.
        now = datetime.datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        # Microsecond precision (not just HH-MM-SS): two motion events for
        # the same camera within the same wall-clock second would otherwise
        # collide on the output filename and the second ffmpeg -y silently
        # overwrites the first clip.
        fname = now.strftime("%H-%M-%S-%f") + "_motion.mp4"
        staging_cam = _staging_dir(base_path, cam_name)
        dest_dir = os.path.join(staging_cam, date_str)
        output_path = os.path.join(dest_dir, fname)

        # Opt-in recovery of the freshest ring segment: normally
        # `list_preroll_files()` drops it because it may still be
        # mid-write. Finalizing it costs a small gap in ring coverage on
        # every event (ring is stopped, then restarted after the clip is
        # built), so it's gated behind its own option rather than on by
        # default.
        #
        # The ring is stopped HERE and only restarted after
        # the clip has been assembled (`finally` below) — see
        # `stop_and_finalize_preroll_recorder`'s docstring for why the old
        # stop-then-immediately-restart-then-scan ordering silently dropped
        # a real segment (or two, on a hard-kill) on every finalized event.
        preroll_override: list[str] | None = None
        ring_stopped_for_finalize = False
        if opts.get("nvr_finalize_ring_on_event", False):
            (
                ring_stopped_for_finalize,
                preroll_override,
            ) = await stop_and_finalize_preroll_recorder(coordinator, cam_id)

        extra_segments: list[str] = []
        postroll_secs = int(opts.get("nvr_postroll_seconds") or 0)
        postroll_attached = False
        ring_restarted = False
        # Post-roll tail derivation: instead of a second cold RTSP capture
        # (a fresh connection at the worst possible moment inherits every
        # transport pathology the already-flowing ring is immune to, and
        # costs a session Gen1 hardware can't spare), wait for the
        # still-running pre-roll ring to record past the event and then
        # take the newly-written segments as the post-roll tail. If the
        # ring was stopped above for `nvr_finalize_ring_on_event`, it must
        # be restarted BEFORE the wait so it's actually recording through
        # the post-roll window rather than only after the whole clip has
        # been built.
        event_wall_time = time.time()
        if postroll_secs > 0:
            if ring_stopped_for_finalize:
                await restart_preroll_recorder_after_finalize(coordinator, cam_id)
                ring_restarted = True
            if cam_id in coordinator.nvr_preroll_processes:
                # `+ _PREROLL_SEGMENT_SECONDS`: `list_preroll_files()`'s
                # "always drop the newest segment" rule (it may still be
                # mid-write) also applies to the scan below, so the last
                # partial segment of a bare `postroll_secs` wait would be
                # excluded — this extra margin guarantees at least
                # `postroll_secs` of footage lands in a *finalized* segment
                # before the scan, at zero extra session/resource cost
                # (the ring keeps running for other consumers regardless).
                await asyncio.sleep(postroll_secs + _PREROLL_SEGMENT_SECONDS)
                cam_dir = _preroll_cam_dir(coordinator, cam_id)
                segs = await coordinator.hass.async_add_executor_job(
                    _list_preroll_segments, cam_dir
                )
                # Only drop the
                # newest segment if it's not yet provably finalized — see
                # `_newest_segment_is_finalized`'s docstring. Proven-closed
                # segments are kept, pushing the tail closer to
                # postroll_secs + _PREROLL_SEGMENT_SECONDS instead of always
                # paying for one discarded segment.
                if segs and await _newest_segment_is_finalized(segs[-1][0]):
                    usable = segs
                else:
                    usable = segs[:-1] if segs else segs
                extra_segments = [
                    path for path, mtime in usable if mtime > event_wall_time
                ]
                postroll_attached = bool(extra_segments)
                if not postroll_attached:
                    _LOGGER.debug(
                        "NVR motion clip: post-roll requested for %s but no "
                        "new ring segment appeared within %ds",
                        cam_name,
                        postroll_secs,
                    )
            else:
                _LOGGER.warning(
                    "NVR motion clip: nvr_postroll_seconds is set for %s "
                    "but the pre-roll ring isn't running (requires "
                    "nvr_preroll_seconds > 0 — the post-roll tail is "
                    "derived from the ring itself, not a separate live "
                    "capture) — skipping post-roll for this event",
                    cam_name,
                )

        try:
            try:
                await coordinator.hass.async_add_executor_job(
                    os.makedirs, dest_dir, 0o755, True
                )
            except OSError as err:
                _LOGGER.warning(
                    "NVR motion clip: cannot create staging dir for %s: %s",
                    cam_name,
                    err,
                )
                return False

            shipped = await create_motion_clip(
                coordinator,
                cam_id,
                output_path,
                extra_segments=extra_segments,
                preroll_paths_override=(
                    preroll_override if ring_stopped_for_finalize else None
                ),
            )
        finally:
            if ring_stopped_for_finalize and not ring_restarted:
                # Restart even if clip assembly failed/raised above — the
                # ring must not stay down until some unrelated event (e.g.
                # a LOCAL session renewal) happens to restart it (GitHub
                # #50). Already restarted above when post-roll was
                # requested (`ring_restarted`) — avoid a double restart.
                await restart_preroll_recorder_after_finalize(coordinator, cam_id)

        if shipped:
            _LOGGER.info(
                "NVR motion clip assembled for %s -> %s (postroll=%ds, "
                "finalize_ring_segments=%s)",
                cam_name,
                output_path,
                postroll_secs if postroll_attached else 0,
                len(preroll_override)
                if ring_stopped_for_finalize and preroll_override
                else 0,
            )
        return shipped


def should_record(
    coordinator: BoschCameraCoordinator,
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
    live = coordinator.live_connections.get(cam_id, {})
    if live.get("_connection_type") != "LOCAL":
        return False
    if not coordinator.is_camera_online(cam_id):
        return False
    return True


# ── recorder lifecycle (per-camera ffmpeg child) ─────────────────────────────


async def start_recorder(
    coordinator: BoschCameraCoordinator, cam_id: str, *, is_auto_retry: bool = False
) -> None:
    """Spawn (or replace) the ffmpeg recorder for one camera.

    Idempotent: if a recorder is already running for ``cam_id`` it is stopped
    first so the new one picks up fresh creds (heartbeat-cred rotation hook).
    Caller is responsible for the LAN-only check (`should_record`).

    ``is_auto_retry`` must be True ONLY when `_watch_recorder`'s own
    auth-failure branch calls this to respawn after a 401. A successful
    ffmpeg *spawn* is not proof the credential is actually valid — the RTSP
    DESCRIBE that would reveal a genuinely broken credential still happens
    after this returns. Resetting ``nvr_auth_retry_count`` on every spawn
    (as this used to do unconditionally) made the give-up cap in
    `_watch_recorder` unreachable for a persistent auth fault: each retry's
    respawn immediately zeroed the counter the retry loop had just
    incremented, so it could never exceed 1. Every OTHER caller (switch
    toggle, coordinator tick, the non-auth crash-respawn path) still resets
    it, since those are legitimate "give this a fresh budget" moments.

    Serialized on ``get_nvr_recorder_lock`` for its ENTIRE body, not just the
    tail-end spawn: locking only the final "read fresh URL + spawn"
    section, while the LEADING ``stop_recorder`` call ran unlocked, would
    let two concurrent callers for the same camera (e.g. a switch toggle
    racing a coordinator-tick auto-heal) each pass the unlocked stop step,
    then each independently — serially, not exclusively against EACH
    OTHER's decision to spawn — acquire the lock and spawn their own
    ffmpeg, both writing to the same staging ``%H-%M.mp4`` segment file and
    mutually truncating it. This is the exact same shape already fixed for
    the pre-roll ring path (see ``_spawn_preroll_recorder_locked``'s
    docstring) — applying the identical pattern here: this function
    acquires the lock once, and its entire stop+wait+spawn sequence runs
    inside it.
    """
    async with coordinator.get_nvr_recorder_lock(cam_id):
        await _start_recorder_locked(coordinator, cam_id, is_auto_retry=is_auto_retry)


async def _start_recorder_locked(
    coordinator: BoschCameraCoordinator, cam_id: str, *, is_auto_retry: bool = False
) -> None:
    """Body of `start_recorder`, run under its caller's `get_nvr_recorder_lock`.

    Callers MUST already hold ``coordinator.get_nvr_recorder_lock(cam_id)`` —
    see `start_recorder`'s docstring. Calls `_spawn_preroll_recorder_locked`
    directly (not the public `start_preroll_recorder`, which itself acquires
    this same lock — re-entering a non-reentrant `asyncio.Lock` would
    deadlock) for the same reason `stop_and_finalize_preroll_recorder`
    does.
    """
    # Replace any pre-existing recorder (cred rotation, switch re-toggle).
    # This is a respawn, not a genuine stop — keep the pre-roll ring buffer
    # instead of wiping it.
    await stop_recorder(coordinator, cam_id, prune_preroll_cache=False)

    live = coordinator.live_connections.get(cam_id, {})
    if live.get("_connection_type") != "LOCAL":
        _LOGGER.debug(
            "NVR start skipped for %s — not LOCAL (gate should have caught this)",
            cam_id[:8],
        )
        return
    # Wait for the TLS-proxy URL: when the NVR switch is toggled on right
    # after the Live Stream switch, the RTSP DESCRIBE pre-warm handshake may
    # still be in flight and ``live_connections[cam_id].rtspsUrl`` is still
    # empty. The coordinator tick would eventually retry, but the immediate
    # UI toggle would record an unwarranted WARNING every tick until the URL
    # lands.
    #
    # Redesigned as an event wait, not a fixed-duration poll. A flat 12 s
    # (24 x 500ms) poll, a constant independently guessed here, would never
    # stay in sync with the REAL per-model pacing already computed and
    # enforced in live_connection.py (``min_total_wait`` — 35 s for Gen1
    # Outdoor on a weak WiFi link, vs. a stale "~3-10s on Gen2" assumption)
    # — so the recorder would give up on every single coordinator tick for
    # slower-encoder cameras and the NVR recorder would never start.
    # Rather than duplicate that timing knowledge as a second guessed
    # constant, wait on ``stream_ready_event`` — the single authoritative
    # signal live_connection.py sets at the exact moment it actually
    # publishes a usable rtspsUrl (see session_state.py). This structurally
    # removes the class of bug where two independent timing constants
    # exist for one real-world event and one of them silently drifts
    # stale, instead of just widening the old constant.
    # The model's own ``min_total_wait`` is still used, but only as an
    # outer safety-net ceiling ("something is genuinely stuck, give up") —
    # not as the primary correctness mechanism.
    rtsp_url = live.get("rtspsUrl") or live.get("rtspUrl") or ""
    if not rtsp_url.startswith("rtsp://"):
        from .models import get_model_config as _get_model_config

        _hw_version_cache = getattr(coordinator, "hw_version", None) or {}
        _model_cfg = _get_model_config(_hw_version_cache.get(cam_id, "CAMERA"))
        _timeout = max(
            _PROXY_URL_WAIT_STEPS * _PROXY_URL_WAIT_INTERVAL, _model_cfg.min_total_wait
        )
        _event = coordinator.get_session(cam_id).stream_ready_event
        try:
            await asyncio.wait_for(_event.wait(), timeout=_timeout)
        except TimeoutError:
            pass
        live = coordinator.live_connections.get(cam_id, {})
        if live.get("_connection_type") != "LOCAL":
            return  # stream torn down while we were waiting
        rtsp_url = live.get("rtspsUrl") or live.get("rtspUrl") or ""
        if not rtsp_url.startswith("rtsp://"):
            _LOGGER.warning(
                "NVR start skipped for %s — TLS-proxy URL not ready after %d s "
                "(stream warm-up too slow); next coordinator tick will retry",
                cam_id[:8],
                round(_timeout),
            )
            return

    opts = coordinator.options

    # Event-only mode: skip continuous recording, run only the pre-roll ring
    # buffer. Motion events can still create clips from cached segments.
    # Resolved per-camera with fallback to the global option —
    # lets a mixed fleet run continuous-while-armed on cameras where PIR
    # can't fire (e.g. shooting through glass) while others stay event-only.
    if coordinator.get_nvr_mode(cam_id) == "event_buffered":
        preroll_secs = int(opts.get("nvr_preroll_seconds") or 0)
        if preroll_secs > 0:
            coordinator._nvr_preroll_zero_warned.discard(cam_id)
            await _spawn_preroll_recorder_locked(coordinator, cam_id)
            # Push an immediate entity update so `mini_nvr_state`'s
            # preroll_running/preroll_segments attributes reflect reality the
            # instant the ring spawns, instead of waiting for the next
            # coordinator tick.
            coordinator.async_update_listeners()
        elif cam_id not in coordinator._nvr_preroll_zero_warned:
            # Mode is per-camera, but the seconds knob is a
            # single global option — a user can pick "Event Buffered
            # (Preroll)" without ever touching it, leaving it at its 0
            # default. Nothing else in this path logs anything, so the
            # ring silently never spawns and /dev/shm/bosch_nvr_cache stays
            # empty forever with zero signal. One-time WARN per camera
            # (cleared above once the option is set >0) instead of every
            # stream-up/session-renewal call to this function.
            _LOGGER.warning(
                "Mini-NVR mode for %s is 'Event Buffered (Preroll)' but the "
                "global 'Preroll seconds' option is 0 — the pre-roll ring "
                "will never start. Set nvr_preroll_seconds > 0 in the "
                "integration's Options to fix this.",
                cam_id[:8],
            )
            coordinator._nvr_preroll_zero_warned.add(cam_id)
        return

    base_path = (opts.get("nvr_base_path") or DEFAULT_BASE_PATH).strip()
    cam_name = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    # ffmpeg ALWAYS writes to a staging tree first — defends against
    # partial-writes during segment rotation. The drain watcher promotes
    # finalized files to either the local layout or to SMB / FTP, depending
    # on `nvr_storage_target`.
    pattern = _staging_pattern(base_path, cam_name)

    # Pre-create the staging camera dir AND today's/tomorrow's date subdir.
    # -strftime_mkdir 1 is unreliable on some ffmpeg versions bundled with HA
    # (confirmed via rc=254 "Failed to open segment"). We create
    # the next 2 days so a recording that starts just before midnight doesn't
    # fail when ffmpeg rolls over to a new date subdirectory.
    staging_cam = _staging_dir(base_path, cam_name)
    try:
        for day_offset in range(2):
            day = (
                datetime.date.today() + datetime.timedelta(days=day_offset)
            ).strftime("%Y-%m-%d")
            await coordinator.hass.async_add_executor_job(
                os.makedirs,
                os.path.join(staging_cam, day),
                0o755,
                True,
            )
    except OSError as err:
        _LOGGER.warning(
            "NVR cannot create staging dir for %s: %s",
            cam_name,
            err,
        )
        return

    quality = (opts.get("nvr_quality") or "auto").strip().lower()

    _LOGGER.info(
        "NVR starting recorder for %s -> %s (quality=%s)",
        cam_name,
        pattern,
        quality,
    )
    # The makedirs step above awaits an executor job,
    # long enough for a Bosch heartbeat to rotate LOCAL creds out from under
    # the `rtsp_url` captured earlier -- ffmpeg would then connect with an
    # already-invalid cred pair and 401 on its first DESCRIBE. Re-read the
    # live URL right before spawning (closing the window to a few
    # microseconds) -- still under the SAME get_nvr_recorder_lock this whole
    # function has held since start_recorder acquired it,
    # the same lock `refresh_local_creds_from_heartbeat` uses while mutating
    # `live_connections`, so the two can't interleave.
    if getattr(coordinator, "nvr_shutting_down", False):
        # Config-entry unload/HA-stop started while we were creating
        # staging dirs — refuse to spawn a process that
        # stop_all()'s concurrent sweep, serialized on this same lock,
        # might already have passed for this camera.
        _LOGGER.debug(
            "NVR start skipped for %s — coordinator shutting down",
            cam_id[:8],
        )
        return
    live = coordinator.live_connections.get(cam_id, {})
    if live.get("_connection_type") != "LOCAL":
        return  # stream torn down while we were creating staging dirs
    fresh_rtsp_url = live.get("rtspsUrl") or live.get("rtspUrl") or rtsp_url
    args = _build_ffmpeg_args(fresh_rtsp_url, pattern, quality=quality)
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

    coordinator.nvr_processes[cam_id] = proc
    stderr_tail = _StderrTail()
    _spawn_stderr_drain_task(
        coordinator, cam_id, proc, stderr_tail, name_prefix="bosch_nvr_stderr_drain"
    )
    # A fresh spawn is underway — clear any stale give-up/error state from a
    # prior crash-loop so the sensor doesn't keep showing "error" forever
    # after a successful restart.
    coordinator.nvr_error_state.pop(cam_id, None)
    # Do NOT reset the auth-retry counter when THIS spawn is itself an
    # auto-retry from the auth-failure branch below — a successful
    # subprocess *spawn* is not proof the credential is valid (the RTSP
    # DESCRIBE that would reveal a persistent auth fault happens after
    # this returns). Resetting here unconditionally made the give-up cap
    # unreachable for a genuinely broken credential: each retry's own
    # respawn zeroed the counter the retry loop had just incremented.
    if not is_auto_retry:
        coordinator.nvr_auth_retry_count.pop(cam_id, None)
    # Push an immediate entity update so `mini_nvr_state` (and anything else
    # reading these dicts) reflects "recording" the instant ffmpeg actually
    # spawns, instead of waiting for the next ~60s coordinator tick (which
    # could otherwise leave the sensor reading "idle" up to 20s after the
    # process was already up).
    coordinator.async_update_listeners()
    # Watcher coroutine restarts ffmpeg once on transient crash and gives up
    # if it crashes again within _RESPAWN_WINDOW_SECONDS.
    task = coordinator.hass.async_create_background_task(
        _watch_recorder(coordinator, cam_id, proc, stderr_tail),
        f"bosch_nvr_watch_{cam_id[:8]}",
    )
    coordinator.bg_tasks.add(task)
    task.add_done_callback(coordinator.bg_tasks.discard)

    # Do NOT also run the pre-roll ring while the continuous recorder is
    # active for this camera: the ring's output is only ever consumed by
    # motion-clip assembly, which is gated to `event_buffered` mode (see
    # the early-return above) — the continuous recorder already captures
    # everything, so a concurrently running ring is a second
    # full-bandwidth ffmpeg consumer whose output nothing reads, actively
    # degrading footage on a bandwidth-constrained link.
    # `stop_recorder`'s leading call above already stopped any ring left
    # over from a prior `event_buffered` stint; mode flipping back to
    # `event_buffered` re-triggers `start_recorder` (switch.py /
    # select.py) which respawns it fresh — an accepted pre-roll-refill
    # gap traded for avoiding the bandwidth contention.


async def stop_recorder(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    *,
    prune_preroll_cache: bool = True,
) -> None:
    """Stop the recorder for one camera, giving ffmpeg up to 5 s to flush MP4.

    ``prune_preroll_cache=False`` passes through to ``stop_preroll_recorder``
    — used by ``start_recorder``'s own leading self-call (a respawn, not a
    genuine stop) so a LOCAL session/cred-rotation renewal doesn't wipe the
    pre-roll ring buffer every time.
    """
    await stop_preroll_recorder(coordinator, cam_id, prune_cache=prune_preroll_cache)
    proc = coordinator.nvr_processes.pop(cam_id, None)
    if proc is None:
        return
    # Push immediately — `nvr_processes` (the sensor's source of truth) is
    # already popped above, so "recording" flips to "idle" right now
    # regardless of how long the graceful-stop/SIGKILL sequence below takes,
    # instead of leaving the sensor reading "recording" for up to 1-2
    # minutes while it waits for the next coordinator tick.
    coordinator.async_update_listeners()
    if proc.returncode is not None:
        _LOGGER.debug(
            "NVR stop_recorder: ffmpeg already exited for %s (rc=%d)",
            cam_id[:8],
            proc.returncode,
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
            cam_id[:8],
            proc.returncode,
        )
    except TimeoutError:
        _LOGGER.warning(
            "NVR stop_recorder: ffmpeg did not exit within %.0fs for %s — escalating to SIGKILL",
            _STOP_GRACE_SECONDS,
            cam_id[:8],
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=TIMEOUT_RECORDER_KILL_WAIT)
        except TimeoutError:
            _LOGGER.warning(
                "NVR stop_recorder: ffmpeg still alive after SIGKILL for %s",
                cam_id[:8],
            )


async def stop_all(coordinator: BoschCameraCoordinator) -> None:
    """Stop every recorder — called on integration unload / HA stop.

    See `stop_all_preroll`'s docstring: sweeps every known
    camera (not just ones with an already-tracked process) under
    `get_nvr_recorder_lock`, so an in-flight `start_recorder` call cannot
    leave an untracked, never-killed ffmpeg process behind.
    """
    await stop_all_preroll(coordinator)
    for cam_id in _known_cam_ids_for_shutdown(coordinator):
        async with coordinator.get_nvr_recorder_lock(cam_id):
            await stop_recorder(coordinator, cam_id)


async def _watch_recorder(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    proc: asyncio.subprocess.Process,
    stderr_tail: _StderrTail,
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
    # If somebody already removed the proc from nvr_processes (clean stop /
    # replacement) we're done — nothing to respawn.
    if coordinator.nvr_processes.get(cam_id) is not proc:
        return
    coordinator.nvr_processes.pop(cam_id, None)
    # Push immediately — an unexpected ffmpeg exit is a real "recording"→
    # "idle" transition the instant it's detected, not something that should
    # wait for the next coordinator tick (same reasoning as stop_recorder
    # above).
    coordinator.async_update_listeners()

    # `stderr_tail` was populated live by `_drain_stderr_live` for the whole
    # life of the process (see GitHub #64) — by the time we get here the
    # pipe may already be closed/empty, so a post-exit read is no longer the
    # right source of diagnostic data.
    err_tail = stderr_tail.data.decode("utf-8", errors="replace").strip()

    elapsed = time.monotonic() - started_at
    _LOGGER.warning(
        "NVR ffmpeg exited rc=%s after %.0fs for %s. Tail: %s",
        rc,
        elapsed,
        cam_id[:8],
        err_tail[-500:] if err_tail else "(no stderr)",
    )

    # Quick re-check: only respawn if we still want to record.
    last = getattr(coordinator, "nvr_user_intent", {}).get(cam_id, False)
    if not should_record(coordinator, cam_id, switch_on=last):
        _LOGGER.info("NVR not respawning for %s — gate now closed", cam_id[:8])
        return

    # B13-4: Detect disk-full — ENOSPC causes ffmpeg rc=1 with a specific
    # stderr message.  Raise a persistent HA notification and skip respawn
    # (the drive is still full, retrying immediately loops forever).
    _ENOSPC_MARKERS = ("no space left", "enospc", "disk quota exceeded")
    err_lower = err_tail.lower()
    if any(marker in err_lower for marker in _ENOSPC_MARKERS):
        _LOGGER.error(
            "NVR ffmpeg exited due to disk-full for %s — not respawning. "
            "Free space under %s and toggle the switch off+on to retry.",
            cam_id[:8],
            (coordinator.options.get("nvr_base_path") or DEFAULT_BASE_PATH),
        )
        coordinator.nvr_error_state[cam_id] = "disk full"
        coordinator.async_update_listeners()
        try:
            hass = getattr(coordinator, "hass", None)
            if hass is not None:
                # Already in the async event loop (_watch_recorder is an async def),
                # so schedule the task directly — no call_soon_threadsafe needed.
                # The coroutine is created INSIDE async_create_task to avoid an
                # eager-create / never-awaited coroutine object if the outer except
                # fires before the task is scheduled.
                hass.async_create_task(
                    hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Bosch Mini-NVR — Disk full",
                            "message": (
                                f"Recording stopped for camera {cam_id[:8]}: "
                                "no space left on device. "
                                f"Free space under "
                                f"{coordinator.options.get('nvr_base_path') or DEFAULT_BASE_PATH} "
                                "and toggle the NVR switch off+on to resume."
                            ),
                            "notification_id": f"bosch_nvr_diskfull_{cam_id[:8]}",
                        },
                    )
                )
        except Exception:  # noqa: S110 # best-effort UI notification; error already logged
            pass
        return

    # A 401/Unauthorized ffmpeg exit means it raced a Bosch
    # credential rotation — a known-transient condition (the next heartbeat
    # tick, or this very respawn once it lands after the rotation settles,
    # will pick up fresh creds), not a persistent fault. Counting it toward
    # the give-up threshold let two back-to-back cred-rotation races
    # permanently kill the recorder — observed as a ~1h recording gap when
    # the NVR switch was toggled on shortly after a LOCAL session opened.
    # Keep retrying on the normal delay/backoff without touching the crash
    # counter or the give-up error state.
    _AUTH_MARKERS = ("401", "unauthorized")
    if any(marker in err_lower for marker in _AUTH_MARKERS):
        retries = coordinator.nvr_auth_retry_count.get(cam_id, 0) + 1
        coordinator.nvr_auth_retry_count[cam_id] = retries
        if retries > _MAX_CONSECUTIVE_AUTH_RETRIES:
            _LOGGER.error(
                "NVR ffmpeg rejected with auth failures %d times in a row for "
                "%s — this is no longer consistent with a transient "
                "cred-rotation race. Toggle the recording switch off+on to "
                "retry.",
                retries,
                cam_id[:8],
            )
            coordinator.nvr_error_state[cam_id] = (
                "repeated auth failures — not a rotation race"
            )
            coordinator.async_update_listeners()
            return
        _LOGGER.warning(
            "NVR ffmpeg hit an auth failure for %s (cred-rotation race, "
            "%d/%d consecutive) — retrying without counting toward the "
            "crash-window give-up limit",
            cam_id[:8],
            retries,
            _MAX_CONSECUTIVE_AUTH_RETRIES,
        )
        await asyncio.sleep(_RESPAWN_DELAY_SECONDS)
        if not should_record(coordinator, cam_id, switch_on=last):
            return
        try:
            await start_recorder(coordinator, cam_id, is_auto_retry=True)
        except Exception as respawn_err:
            # An unexpected exception here would otherwise kill this
            # background watcher task silently, with no nvr_error_state and
            # no listener push, leaving recording permanently stopped with
            # zero user-visible signal.
            _LOGGER.error(
                "NVR respawn (auth-retry path) raised unexpectedly for %s: %s",
                cam_id[:8],
                respawn_err,
            )
            coordinator.nvr_error_state[cam_id] = "respawn failed unexpectedly"
            coordinator.async_update_listeners()
        return

    # B13-2: Always record the crash timestamp (not only for short-lived
    # crashes).  This closes the hole where a camera that crashes every 45 s
    # (i.e. elapsed > _RESPAWN_WINDOW_SECONDS) is never counted and respawns
    # forever because nvr_recent_crash is never written.
    # The *give-up* gate still requires two crashes within the window.
    now = time.monotonic()
    prev_crash = coordinator.nvr_recent_crash.get(cam_id, float("-inf"))
    if (now - prev_crash) < _RESPAWN_WINDOW_SECONDS:
        _LOGGER.error(
            "NVR ffmpeg crashed twice within %.0fs for %s — giving up. "
            "Toggle the recording switch off+on to retry.",
            _RESPAWN_WINDOW_SECONDS,
            cam_id[:8],
        )
        coordinator.nvr_error_state[cam_id] = "ffmpeg crashed twice"
        coordinator.async_update_listeners()
        return
    coordinator.nvr_recent_crash[cam_id] = now

    await asyncio.sleep(_RESPAWN_DELAY_SECONDS)
    if not should_record(coordinator, cam_id, switch_on=last):
        return
    _LOGGER.info("NVR respawning ffmpeg for %s after transient crash", cam_id[:8])
    try:
        await start_recorder(coordinator, cam_id)
    except Exception as respawn_err:
        # See the matching comment on the auth-retry respawn above — same
        # fix, same reasoning.
        _LOGGER.error(
            "NVR respawn (transient-crash path) raised unexpectedly for %s: %s",
            cam_id[:8],
            respawn_err,
        )
        coordinator.nvr_error_state[cam_id] = "respawn failed unexpectedly"
        coordinator.async_update_listeners()


# ── staging-drain watcher (per-coordinator background task) ──────────────────


def _list_staging_candidates(
    staging_root: str,
) -> list[tuple[str, str, str, float, int]]:
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


def _move_local(
    coordinator: BoschCameraCoordinator,
    full: str,
    base_path: str,
    cam: str,
    date: str,
    fname: str,
) -> bool:
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
        _LOGGER.debug("NVR drain (local): move %s -> %s failed: %s", full, dest, err)
        return False


def _upload_smb(
    coordinator: BoschCameraCoordinator, full: str, cam: str, date: str, fname: str
) -> bool:
    """target=smb: upload one finalized segment via smbclient.

    Reuses the session-register pattern from ``smb.py`` but writes only to
    the NVR subtree (``{smb_base_path}/{nvr_smb_subpath}``) so cloud-event
    uploads stay in their own branch.
    """
    try:
        from smbclient import (
            open_file,
            register_session,
        )
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
    except Exception as err:
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
        smb_makedirs(
            smb_folder,
            server,
            (opts.get("smb_share") or "").strip(),
            base,
            folder_parts,
        )
    except Exception as err:
        _LOGGER.debug("NVR drain (smb): mkdir %s failed: %s", smb_folder, err)
        return False

    dest = _remote_smb_path(opts, cam, date, fname)
    try:
        with open(full, "rb") as src, open_file(dest, mode="wb") as dst:
            for chunk in iter(lambda: src.read(65536), b""):
                dst.write(chunk)
        return True
    except Exception as err:
        _LOGGER.warning("NVR drain (smb): upload %s -> %s failed: %s", full, dest, err)
        return False


def _upload_ftp(
    coordinator: BoschCameraCoordinator, full: str, cam: str, date: str, fname: str
) -> bool:
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
    except Exception as err:
        _LOGGER.warning("NVR drain (ftp): login to %s failed: %s", server, err)
        return False
    try:
        base = (opts.get("smb_base_path") or "Bosch-Kameras").strip().strip("/")
        sub = (opts.get("nvr_smb_subpath") or "NVR").strip().strip("/")
        cam_safe = _safe_name(cam)
        ftp_dir = f"/{base}/{sub}/{cam_safe}/{date}".replace("//", "/").rstrip("/")
        try:
            _ftp_makedirs(ftp, ftp_dir)
        except Exception as err:
            _LOGGER.debug("NVR drain (ftp): mkdir %s failed: %s", ftp_dir, err)
            return False
        dest = f"{ftp_dir}/{fname}"
        try:
            with open(full, "rb") as src:
                ftp.storbinary(f"STOR {dest}", src)
            return True
        except Exception as err:
            _LOGGER.warning(
                "NVR drain (ftp): upload %s -> %s failed: %s", full, dest, err
            )
            return False
    finally:
        try:
            ftp.quit()
        except Exception:  # best-effort graceful FTP quit; fallback to close below
            try:
                ftp.close()
            except (  # noqa: S110 # best-effort FTP teardown, failure non-actionable
                Exception
            ):  # best-effort FTP socket close on teardown, failure non-actionable
                pass


def _quarantine_failed(
    base_path: str, full: str, cam: str, date: str, fname: str
) -> None:
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


def sync_drain_tick(
    coordinator: BoschCameraCoordinator, *, now: float | None = None
) -> dict[str, int]:
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
    if not hasattr(coordinator, "nvr_drain_failures"):
        coordinator.nvr_drain_failures = {}
    failures: dict[str, int] = coordinator.nvr_drain_failures

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
            ok = _move_local(
                coordinator, full, base_path, cam, date, os.path.basename(full)
            )
            if ok:
                promoted += 1
        elif target == "smb":
            ok = _upload_smb(coordinator, full, cam, date, os.path.basename(full))
            if ok:
                uploaded += 1
                try:
                    os.unlink(full)
                except OSError as err:
                    _LOGGER.debug(
                        "NVR drain: unlink %s after smb upload failed: %s", full, err
                    )
        elif target == "ftp":
            ok = _upload_ftp(coordinator, full, cam, date, os.path.basename(full))
            if ok:
                uploaded += 1
                try:
                    os.unlink(full)
                except OSError as err:
                    _LOGGER.debug(
                        "NVR drain: unlink %s after ftp upload failed: %s", full, err
                    )
        else:
            _LOGGER.debug("NVR drain: unknown target %r — treating as local", target)
            ok = _move_local(
                coordinator, full, base_path, cam, date, os.path.basename(full)
            )
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
                full,
                _DRAIN_MAX_RETRIES,
            )
            _quarantine_failed(base_path, full, cam, date, os.path.basename(full))
            failures.pop(full, None)
            # Best-effort persistent notification — surface to the user.
            try:
                hass = getattr(coordinator, "hass", None)
                if hass is not None:
                    # sync_drain_tick runs in an executor thread, so we need
                    # call_soon_threadsafe to schedule onto the event loop.
                    # The coroutine is created INSIDE the lambda so it is only
                    # constructed on the loop thread — never an eager-create /
                    # never-awaited object sitting on a foreign thread.
                    _msg = (
                        f"Failed to drain {os.path.basename(full)} "
                        f"after {_DRAIN_MAX_RETRIES} attempts. "
                        f"File moved to {_failed_dir(base_path, cam)}."
                    )
                    _nid = f"bosch_nvr_drain_failed_{cam}"
                    hass.loop.call_soon_threadsafe(
                        hass.async_create_task,
                        hass.services.async_call(
                            "persistent_notification",
                            "create",
                            {
                                "title": "Bosch Mini-NVR — Upload failed",
                                "message": _msg,
                                "notification_id": _nid,
                            },
                        ),
                    )
            except (  # noqa: S110 # best-effort UI notify; quarantine + error already logged
                Exception
            ):  # best-effort UI notification; quarantine + error log already done above
                pass

    # Persist the latest drain stats on the coordinator so the sensor can
    # render them. ``nvr_drain_state`` is created on first tick.
    state: dict[str, Any] = getattr(coordinator, "nvr_drain_state", None) or {}
    state["target"] = target
    state["pending"] = pending
    state["promoted"] = promoted
    state["uploaded"] = uploaded
    state["failed"] = failed
    state["last_age_by_cam"] = last_age
    state["last_tick_ts"] = now_ts
    coordinator.nvr_drain_state = state

    return {
        "promoted": promoted,
        "uploaded": uploaded,
        "failed": failed,
        "pending": pending,
    }


async def drain_staging_to_remote(coordinator: BoschCameraCoordinator) -> None:
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
                        sync_drain_tick,
                        coordinator,
                    )
                except Exception as err:
                    _LOGGER.warning("NVR drain tick raised: %s", err)
            await asyncio.sleep(_DRAIN_TICK_SECONDS)
        except asyncio.CancelledError:
            _LOGGER.debug("NVR drain watcher cancelled — exiting")
            raise


# ── retention purge (runs in executor thread, once per day) ──────────────────


def sync_nvr_cleanup(coordinator: BoschCameraCoordinator) -> None:
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
    schedule as ``sync_smb_cleanup`` (called from ``run_nvr_cleanup_bg``).
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


def _sync_nvr_cleanup_local(coordinator: BoschCameraCoordinator) -> None:
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
    for root, _dirs, files in os.walk(base_path):
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
    # Second pass: prune empty date folders (but never the camera dir,
    # base_path itself, or anything under the _staging tree).
    #
    # This walk must exclude _staging/{cam}/{date}/ — start_recorder
    # deliberately pre-creates TODAY's and TOMORROW's staging date-dir on
    # start because ffmpeg's `-strftime_mkdir 1` is unreliable on some
    # bundled builds (confirmed via rc=254 "Failed to open segment", see
    # start_recorder's own comment). Tomorrow's dir is empty by
    # construction and stays empty until midnight rollover — this daily
    # cleanup runs on essentially the same cadence as that pre-creation,
    # so it would almost always find tomorrow's staging date-dir still
    # empty and delete it, silently undoing the exact workaround it exists
    # for: at midnight, ffmpeg's own -strftime_mkdir then fails again on
    # affected builds, dropping that segment (the recurring gap this was
    # meant to prevent).
    staging_root = os.path.join(base_path, _STAGING_DIRNAME)
    for root, _dirs, _files in os.walk(base_path, topdown=False):
        if root == base_path:
            continue
        if root == staging_root or root.startswith(staging_root + os.sep):
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass
    if deleted:
        _LOGGER.info(
            "NVR cleanup (local): deleted %d file(s) older than %d days from %s",
            deleted,
            retention_days,
            base_path,
        )


def _sync_nvr_cleanup_smb(coordinator: BoschCameraCoordinator) -> None:
    """Walk only the NVR subtree on the SMB share and unlink old files."""
    try:
        from smbclient import register_session, remove, scandir
        from smbclient import stat as smb_stat
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
    except Exception as err:
        _LOGGER.warning("NVR cleanup (smb): session to %s failed: %s", server, err)
        return

    cutoff = time.time() - retention_days * 86400
    root = f"\\\\{server}\\{share}\\{base_path}\\{sub}"
    deleted = 0
    deadline = time.monotonic() + _NVR_CLEANUP_MAX_SECONDS
    deadline_hit = False

    def _walk_and_delete(path: str) -> None:
        nonlocal deleted, deadline_hit
        if time.monotonic() > deadline:
            deadline_hit = True
            return
        try:
            entries = list(scandir(path))
        except Exception:
            return
        for entry in entries:
            if time.monotonic() > deadline:
                deadline_hit = True
                return
            full = f"{path}\\{entry.name}"
            if entry.is_dir():
                _walk_and_delete(full)
            else:
                try:
                    st = smb_stat(full)
                    if st.st_mtime < cutoff:
                        remove(full)
                        deleted += 1
                except Exception as err:
                    _LOGGER.debug("NVR cleanup (smb): error on %s: %s", entry.name, err)

    _walk_and_delete(root)
    if deadline_hit:
        _LOGGER.warning(
            "NVR cleanup (smb): deadline (%.0fs) exceeded, walk stopped early — "
            "some old files under %s may remain until the next cleanup run",
            _NVR_CLEANUP_MAX_SECONDS,
            root,
        )
    if deleted:
        _LOGGER.info(
            "NVR cleanup (smb): deleted %d file(s) older than %d days from %s",
            deleted,
            retention_days,
            root,
        )


def _sync_nvr_cleanup_ftp(coordinator: BoschCameraCoordinator) -> None:
    """Walk only the NVR subtree on the FTP server and unlink old files."""
    import ftplib
    from datetime import datetime

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
    except Exception as err:
        _LOGGER.warning("NVR cleanup (ftp): login to %s failed: %s", server, err)
        return

    cutoff = time.time() - retention_days * 86400
    deleted = 0
    deadline = time.monotonic() + _NVR_CLEANUP_MAX_SECONDS
    deadline_hit = False

    def _walk_and_delete(path: str) -> None:
        nonlocal deleted, deadline_hit
        if time.monotonic() > deadline:
            deadline_hit = True
            return
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
            if time.monotonic() > deadline:
                deadline_hit = True
                return
            # B13-6: use absolute paths for MDTM and DELETE so the commands are
            # position-independent even if a recursive _walk_and_delete call
            # left the FTP working-directory pointing at a subdirectory.
            abs_name = f"{path}/{name}"
            try:
                resp = ftp.sendcmd(f"MDTM {abs_name}")
                ts_str = resp.split()[-1]
                mt = (
                    datetime.strptime(ts_str[:14], "%Y%m%d%H%M%S")
                    .replace(tzinfo=UTC)
                    .timestamp()
                )
            except Exception:  # skip file if MDTM unavailable; resilient FTP walk loop  # noqa: S112 # skip file if MDTM unavailable, resilient walk
                continue
            if mt < cutoff:
                try:
                    ftp.delete(abs_name)
                    deleted += 1
                except Exception as err:
                    _LOGGER.debug(
                        "NVR cleanup (ftp): delete %s failed: %s", abs_name, err
                    )
        for sd in subdirs:
            _walk_and_delete(f"{path}/{sd}")
            try:
                ftp.cwd(path)
            except (  # noqa: S110 # best-effort cwd restore, sibling loop continues
                Exception
            ):  # best-effort cwd back to parent after subdir walk; non-fatal
                pass

    try:
        root = f"/{base_path}/{sub}"
        _walk_and_delete(root)
    finally:
        try:
            ftp.quit()
        except (  # noqa: S110 # best-effort FTP teardown, failure non-actionable
            Exception
        ):  # best-effort FTP quit on cleanup teardown, failure non-actionable
            pass
    if deadline_hit:
        _LOGGER.warning(
            "NVR cleanup (ftp): deadline (%.0fs) exceeded, walk stopped early — "
            "some old files under %s may remain until the next cleanup run",
            _NVR_CLEANUP_MAX_SECONDS,
            f"{server}/{base_path}/{sub}",
        )
    if deleted:
        _LOGGER.info(
            "NVR cleanup (ftp): deleted %d file(s) older than %d days from %s",
            deleted,
            retention_days,
            f"{server}/{base_path}/{sub}",
        )
