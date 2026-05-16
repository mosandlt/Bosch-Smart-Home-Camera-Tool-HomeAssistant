"""Coverage push on recorder.py pre-roll + motion-clip + stop_preroll paths.

These tests target the lowest-covered branches in `recorder.py` that
previous test rounds did not reach:

  1. `_list_preroll_segments` + `prune_preroll_cache` (lines 215-250)
     - listdir OSError → empty list, no crash.
     - Segments below `_PREROLL_MIN_SIZE_BYTES` (1024) are filtered out
       so a half-flushed/corrupt segment cannot be served by the clip
       builder (would otherwise produce a broken first-second of MP4).
     - Sort+delete keeps the newest `max_segments` (here 3 of 7) so the
       ring-buffer stays bounded — the central LAN-side defense against
       /dev/shm filling.

  2. `create_motion_clip` (lines 436-471)
     - `asyncio.create_subprocess_exec` raising `FileNotFoundError`
       (ffmpeg not on PATH) → returns False, logs error, no crash.
     - `_write_concat` raising `OSError` (read-only fs) → returns False
       before any spawn attempt.
     - Argv contains `-f concat -safe 0` exactly — pinning the unsafe
       path flag is critical because ffmpeg refuses absolute paths in
       the concat-list without `-safe 0`.

  3. `stop_preroll_recorder` SIGKILL escalation final timeout (lines
     388-396)
     - `proc.kill()` raising `ProcessLookupError` is swallowed (race
       where the process died between SIGTERM-timeout and SIGKILL).
     - Final `asyncio.TimeoutError` after SIGKILL is also swallowed —
       under no circumstances may stop_preroll_recorder raise.

User/forum source: project-internal Mini-NVR Phase 4 (pre-roll buffer)
and Phase 7 (motion-clip) — bugs in these paths leak ffmpeg processes
or corrupt clips, both highly user-visible.

SENTINEL_RULE: every `time.monotonic()` default in this file uses
`float('-inf')`, not `0.0`, so the assertions hold on fresh CI VMs.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_TITLE = "Terrasse"


def _make_coord(tmp_path, *, cam_title: str = CAM_TITLE):
    """Stub coordinator with the fields the pre-roll/clip helpers read."""
    async def _run_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": cam_title}, "status": "ONLINE"}},
        options={
            "nvr_preroll_cache_dir": str(tmp_path),
            "nvr_preroll_seconds": 30,
        },
        _nvr_preroll_processes={}, _nvr_preroll_segment_counts={},
        _nvr_preroll_tasks={},
        _bg_tasks=set(),
        # SENTINEL_RULE: monotonic-based "last X" maps default to float('-inf')
        # so any (now - last) >= interval check is True on fresh CI VMs.
        _nvr_last_preroll_prune={CAM_ID: float('-inf')},
    )
    coord.hass = SimpleNamespace(
        async_add_executor_job=_run_executor,
        async_create_background_task=lambda c, n=None: MagicMock(),
    )
    return coord


# ── 1. _list_preroll_segments + prune_preroll_cache ────────────────────────


class TestListPrerollSegments:
    """Pin _list_preroll_segments behavior under three failure modes."""

    def test_listdir_oserror_returns_empty(self, tmp_path):
        """If `os.listdir` raises (permissions / vanished dir), return [].

        Pin: caller (prune / list_files) must never crash on a transient
        OSError — pre-roll is best-effort.
        """
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        # Patch listdir to raise OSError only for our path
        real_listdir = os.listdir

        def _flaky(path, *args, **kwargs):
            if str(path) == str(cam_dir):
                raise OSError("EACCES")
            return real_listdir(path, *args, **kwargs)

        with patch.object(os, "listdir", side_effect=_flaky):
            result = _list_preroll_segments(str(cam_dir))
        assert result == []

    def test_undersized_segments_filtered(self, tmp_path):
        """Segment smaller than `_PREROLL_MIN_SIZE_BYTES` is dropped.

        Pin: half-flushed corrupt segments must never reach the motion-clip
        concatenator — ffmpeg would otherwise fail mid-stream with an
        invalid-moov error.
        """
        from custom_components.bosch_shc_camera.recorder import (
            _list_preroll_segments,
            _PREROLL_MIN_SIZE_BYTES,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        tiny = cam_dir / "000000.mp4"
        tiny.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES - 1))
        ok = cam_dir / "000010.mp4"
        ok.write_bytes(b"y" * (_PREROLL_MIN_SIZE_BYTES + 100))

        result = _list_preroll_segments(str(cam_dir))
        paths = [p for p, _ in result]
        assert str(tiny) not in paths
        assert str(ok) in paths

    def test_missing_directory_returns_empty(self, tmp_path):
        """Pin: non-existent cam_dir returns [] (idempotent first call)."""
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        result = _list_preroll_segments(str(tmp_path / "does-not-exist"))
        assert result == []


class TestPrunePrerollCache:
    """Pin prune_preroll_cache deletes oldest, keeps the newest `max`."""

    def test_keeps_three_newest_of_seven(self, tmp_path):
        """7 segments, max=3 → 4 deleted, 3 kept; the 3 newest by mtime.

        Pin: ring buffer must be bounded by mtime so wall-clock skews
        don't keep stale files alive. Critical defense against the
        /dev/shm cache growing without limit.
        """
        from custom_components.bosch_shc_camera.recorder import (
            prune_preroll_cache,
            _PREROLL_MIN_SIZE_BYTES,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        # Seven segments with increasing mtimes; smallest mtime = oldest.
        files = []
        now = time.time()
        for i in range(7):
            f = cam_dir / f"{i:06d}.mp4"
            f.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 10))
            mtime = now - (7 - i) * 10  # i=0 oldest, i=6 newest
            os.utime(f, (mtime, mtime))
            files.append(f)

        deleted = prune_preroll_cache(str(cam_dir), max_segments=3)
        assert deleted == 4
        # Oldest 4 (i=0..3) gone, newest 3 (i=4,5,6) survive.
        for i in range(4):
            assert not files[i].exists(), f"oldest seg {i} must be pruned"
        for i in range(4, 7):
            assert files[i].exists(), f"newest seg {i} must survive"

    def test_under_max_keeps_everything(self, tmp_path):
        """When count ≤ max_segments, no deletes."""
        from custom_components.bosch_shc_camera.recorder import (
            prune_preroll_cache,
            _PREROLL_MIN_SIZE_BYTES,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        for i in range(2):
            f = cam_dir / f"{i:06d}.mp4"
            f.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 1))

        deleted = prune_preroll_cache(str(cam_dir), max_segments=5)
        assert deleted == 0

    def test_unlink_oserror_continues(self, tmp_path):
        """If unlink raises mid-prune, the loop continues — file might
        have already vanished due to a parallel sweep."""
        from custom_components.bosch_shc_camera.recorder import (
            prune_preroll_cache,
            _PREROLL_MIN_SIZE_BYTES,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        # 4 segments, max=1 → 3 to delete
        for i in range(4):
            f = cam_dir / f"{i:06d}.mp4"
            f.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 1))
            mtime = time.time() - (4 - i) * 10
            os.utime(f, (mtime, mtime))

        with patch.object(os, "unlink", side_effect=OSError("ENOENT")):
            deleted = prune_preroll_cache(str(cam_dir), max_segments=1)
        # All unlinks raised → deleted counter stays 0, no exception escapes.
        assert deleted == 0


# ── 2. create_motion_clip — concat-write + ffmpeg spawn paths ──────────────


class TestCreateMotionClip:
    """Pin create_motion_clip failure paths + argv shape."""

    def _seed_preroll(self, tmp_path, count: int = 2):
        """Create `count` valid pre-roll segments in tmp_path/<CAM_TITLE>/."""
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        for i in range(count):
            f = cam_dir / f"{i:06d}.mp4"
            f.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 50))
        return cam_dir

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_returns_false(self, tmp_path):
        """ffmpeg missing on PATH → log error + return False, no crash.

        Pin: a misconfigured host (no ffmpeg) must not break the motion
        pipeline for other cameras; this entry just returns False.
        """
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(tmp_path)
        self._seed_preroll(tmp_path)

        out_path = str(tmp_path / "clip.mp4")
        with patch.object(
            asyncio, "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            result = await recorder.create_motion_clip(coord, CAM_ID, out_path)
        assert result is False

    @pytest.mark.asyncio
    async def test_concat_write_oserror_returns_false(self, tmp_path):
        """`_write_concat` raising OSError (read-only fs) → return False
        before ffmpeg is even spawned.

        Pin: pre-flight write failure bypasses the spawn so we don't
        leak an orphaned ffmpeg trying to read a missing concat file.
        """
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(tmp_path)
        self._seed_preroll(tmp_path)

        out_path = str(tmp_path / "clip.mp4")

        # Make executor-job dispatch raise OSError only on the concat write
        # (not on the unlink call at the end).
        async def _bad_executor(fn, *args, **kwargs):
            if callable(fn) and "_write_concat" in getattr(fn, "__qualname__", ""):
                raise OSError("EROFS")
            return fn(*args, **kwargs)
        coord.hass.async_add_executor_job = _bad_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            result = await recorder.create_motion_clip(coord, CAM_ID, out_path)
        assert result is False
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_oserror_on_spawn_returns_false(self, tmp_path):
        """Generic OSError (e.g. EAGAIN fork-limit) during spawn → False.

        Pin: branch 469-471 — non-FileNotFoundError OSError path covered.
        """
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(tmp_path)
        self._seed_preroll(tmp_path)

        out_path = str(tmp_path / "clip.mp4")
        with patch.object(
            asyncio, "create_subprocess_exec",
            side_effect=OSError("EAGAIN"),
        ):
            result = await recorder.create_motion_clip(coord, CAM_ID, out_path)
        assert result is False

    def test_motion_clip_args_have_concat_safe(self):
        """Pin ffmpeg argv: `-f concat -safe 0 -i <concat_file>`.

        ffmpeg refuses absolute paths in concat-list files without
        `-safe 0`; dropping that flag would silently break clips on
        every install where `/config/bosch_nvr/...` paths are absolute
        (i.e. all of them).
        """
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        argv = create_motion_clip_args(
            ["/tmp/a.mp4", "/tmp/b.mp4"], "/tmp/out.mp4"
        )
        # Find the -f concat -safe 0 sequence
        assert "-f" in argv
        f_idx = argv.index("-f")
        assert argv[f_idx + 1] == "concat"
        assert "-safe" in argv
        s_idx = argv.index("-safe")
        assert argv[s_idx + 1] == "0"
        # Output is last
        assert argv[-1] == "/tmp/out.mp4"
        # -y forces overwrite — critical because the same path may already
        # exist from a previous (failed) attempt.
        assert "-y" in argv


# ── 3. stop_preroll_recorder — SIGKILL escalation race paths ───────────────


def _mock_proc(returncode=None):
    """Build a mock asyncio.subprocess.Process for stop tests."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    final_rc = returncode if returncode is not None else -9
    async def _wait():
        proc.returncode = final_rc
        return final_rc
    proc.wait = _wait
    return proc


class TestStopPrerollSigkillRace:
    """Pin race-safe behavior in stop_preroll_recorder lines 388-396."""

    @pytest.mark.asyncio
    async def test_kill_process_lookup_error_swallowed(self, tmp_path):
        """SIGTERM times out → proc.kill() raises ProcessLookupError → no crash.

        Race: process died between our SIGTERM-timeout and the SIGKILL call.
        Must be swallowed so stop_preroll_recorder still completes cleanly.
        """
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        proc.kill = MagicMock(side_effect=ProcessLookupError("no such process"))
        coord._nvr_preroll_processes[CAM_ID] = proc

        # First wait_for (SIGTERM grace) → TimeoutError; second (post-SIGKILL) → resolves.
        with patch.object(asyncio, "wait_for", side_effect=[
            asyncio.TimeoutError(),
            -9,
        ]):
            # Must not raise
            await recorder.stop_preroll_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once()
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_final_timeout_after_sigkill_swallowed(self, tmp_path):
        """Even SIGKILL hung in wait_for → final TimeoutError is swallowed.

        Pin: branch 393-396 — under no circumstances may stop_preroll_recorder
        propagate a TimeoutError; the watchdog must remain non-blocking so
        the integration unload path can finish.
        """
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        coord._nvr_preroll_processes[CAM_ID] = proc

        # Both wait_for calls time out — SIGKILL didn't take either.
        with patch.object(asyncio, "wait_for", side_effect=[
            asyncio.TimeoutError(),
            asyncio.TimeoutError(),
        ]):
            await recorder.stop_preroll_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_process_registered_is_no_op(self, tmp_path):
        """Pin idempotency: calling stop on a cam with no live process is safe."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(tmp_path)
        # No process registered
        await recorder.stop_preroll_recorder(coord, CAM_ID)
        # No state change, no exception
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_already_exited_returns_quickly(self, tmp_path):
        """If returncode is already set, send_signal is never called."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(tmp_path)
        proc = _mock_proc(returncode=0)
        coord._nvr_preroll_processes[CAM_ID] = proc

        await recorder.stop_preroll_recorder(coord, CAM_ID)
        proc.send_signal.assert_not_called()
        proc.kill.assert_not_called()
