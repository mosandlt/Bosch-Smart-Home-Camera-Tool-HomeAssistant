"""Coverage push on recorder.py async paths.

`tests/test_recorder.py` (from the agent) covers the pure-function
helpers (`should_record`, `_build_ffmpeg_args`, path generation,
retention cutoff math). This file covers the async lifecycle:
  - `start_recorder` — LAN gate, dir creation, subprocess spawn,
    watcher registration, ffmpeg-not-found, OSError on spawn.
  - `stop_recorder` — SIGTERM, clean exit, timeout → SIGKILL,
    process-already-dead idempotency.
  - `stop_all` — iterates over all running cams.
  - `_watch_recorder` — single-respawn after transient crash,
    crash-loop give-up, gate-closed-during-wait.
  - `sync_nvr_cleanup` — disabled-when-retention-zero, missing
    base_path, mtime ≥ cutoff (keep), empty-dir prune.

These are the highest-leverage paths because they cover error
handling — the parts most likely to bite in production when
ffmpeg crashes / disk fills / network blips.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _make_coord(*, conn_type: str = "LOCAL", base_path: str = "/tmp/nvr_test"):
    """Stub coordinator with the fields recorder.py touches."""
    proxy_url = "rtsp://user:pass@127.0.0.1:46597/rtsp_tunnel?inst=1"
    coord = SimpleNamespace(
        _live_connections={
            CAM_ID: {
                "_connection_type": conn_type,
                "rtspsUrl": proxy_url,
            }
        },
        _nvr_processes={},
        _nvr_user_intent={CAM_ID: True},
        _nvr_recent_crash={},
        _nvr_error_state={},
        _bg_tasks=set(),
        data={CAM_ID: {"info": {"title": "Terrasse"}, "status": "ONLINE"}},
        options={
            "nvr_base_path": base_path,
            "nvr_retention_days": 3,
            "enable_nvr": True,
        },
        is_camera_online=lambda cid: True,
    )

    # Build a hass stub. async_add_executor_job runs the function in-thread for
    # the test (no actual executor needed). async_create_background_task swallows
    # the coro so we don't have to await unstarted watchers.
    async def _run_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _create_bg_task(coro, name=None):
        # Close the coroutine to silence "never awaited" warnings.
        coro.close()
        task = MagicMock()
        task.add_done_callback = MagicMock()
        return task

    coord.hass = SimpleNamespace(
        async_add_executor_job=_run_executor,
        async_create_background_task=_create_bg_task,
    )
    return coord


def _mock_proc(returncode=None, stderr_data: bytes = b""):
    """Build a mock asyncio.subprocess.Process."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    # Real subprocess sets `returncode` after `wait()` resolves; mirror
    # that so debug-log statements like `%d` % proc.returncode don't trip.
    final_rc = returncode if returncode is not None else 0
    async def _wait():
        proc.returncode = final_rc
        return final_rc
    proc.wait = _wait
    if stderr_data:
        stderr = MagicMock()
        stderr.read = AsyncMock(return_value=stderr_data)
        proc.stderr = stderr
    else:
        proc.stderr = None
    return proc


# ── start_recorder ──────────────────────────────────────────────────────


class TestStartRecorder:
    @pytest.mark.asyncio
    async def test_skipped_when_not_local(self, tmp_path):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(conn_type="REMOTE", base_path=str(tmp_path))
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_skipped_when_no_proxy_url(self, tmp_path):
        """rtspsUrl missing or not rtsp:// → skip with warning, no spawn."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        coord._live_connections[CAM_ID]["rtspsUrl"] = ""
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_proxy_url_is_https(self, tmp_path):
        """If only the rtsps:// URL is set (not rewritten through proxy),
        skip — recording over TLS to the camera bypasses our proxy."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        coord._live_connections[CAM_ID]["rtspsUrl"] = "rtsps://camera.lan/x"
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_spawns_ffmpeg(self, tmp_path):
        """LOCAL + valid proxy URL → spawn, register process, register watcher."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)
        async def _spawn(*args, **kwargs):
            return proc
        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)
        assert coord._nvr_processes[CAM_ID] is proc
        # Segment dir was created — under the staging tree as of v11.0.4
        # NVR-storage-target refactor (ffmpeg always writes to _staging first).
        assert (tmp_path / "_staging" / "Terrasse").exists()

    @pytest.mark.asyncio
    async def test_replaces_existing_process(self, tmp_path):
        """Calling start_recorder while one is already running must stop
        the old before spawning new — required for cred rotation."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        old_proc = _mock_proc(returncode=None)
        coord._nvr_processes[CAM_ID] = old_proc
        new_proc = _mock_proc(returncode=None)
        async def _spawn(*args, **kwargs):
            return new_proc
        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)
        # Old got SIGTERM
        old_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        # New is now registered
        assert coord._nvr_processes[CAM_ID] is new_proc

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_fails_silently(self, tmp_path):
        """Missing ffmpeg binary must not crash HA — log error + return."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        with patch.object(
            asyncio, "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_oserror_on_spawn_returns(self, tmp_path):
        """Generic OSError (permissions, OOM, fork limit) — log + return."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        with patch.object(
            asyncio, "create_subprocess_exec",
            side_effect=OSError("EAGAIN"),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_makedirs_failure_aborts_spawn(self, tmp_path):
        """If we can't create the segment dir (read-only fs, no perms),
        skip the spawn — ffmpeg would just fail later anyway."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))

        async def _bad_executor(fn, *args, **kwargs):
            if fn is os.makedirs:
                raise OSError("EROFS")
            return fn(*args, **kwargs)
        coord.hass.async_add_executor_job = _bad_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()


# ── stop_recorder ───────────────────────────────────────────────────────


class TestStopRecorder:
    @pytest.mark.asyncio
    async def test_no_op_when_not_running(self):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        # No process registered
        await recorder.stop_recorder(coord, CAM_ID)
        # No exception, no state change
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_already_exited_quick_return(self):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        proc = _mock_proc(returncode=0)  # already exited
        coord._nvr_processes[CAM_ID] = proc
        await recorder.stop_recorder(coord, CAM_ID)
        proc.send_signal.assert_not_called()
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_clean_sigterm_exit(self):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        proc = _mock_proc(returncode=None)
        proc.wait = AsyncMock(return_value=0)
        coord._nvr_processes[CAM_ID] = proc
        await recorder.stop_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_not_called()  # didn't escalate
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_sigkill_escalation_on_timeout(self):
        """If ffmpeg ignores SIGTERM for 5 s, escalate to SIGKILL."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        proc = _mock_proc(returncode=None)

        # First wait (after SIGTERM): timeout. Second wait (after SIGKILL): success.
        wait_calls = [asyncio.TimeoutError(), 137]
        async def _wait():
            r = wait_calls.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r
        proc.wait = _wait
        coord._nvr_processes[CAM_ID] = proc

        with patch.object(asyncio, "wait_for", side_effect=[
            asyncio.TimeoutError(), 137,
        ]):
            await recorder.stop_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_lookup_error_on_sigterm_safely_returns(self):
        """If the process died between our check and SIGTERM (race), the
        ProcessLookupError must be swallowed."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock(side_effect=ProcessLookupError())
        coord._nvr_processes[CAM_ID] = proc
        # Must not raise
        await recorder.stop_recorder(coord, CAM_ID)


# ── stop_all ────────────────────────────────────────────────────────────


class TestStopAll:
    @pytest.mark.asyncio
    async def test_stops_every_running_recorder(self):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        proc_a = _mock_proc(returncode=0)
        proc_b = _mock_proc(returncode=0)
        coord._nvr_processes["cam-A"] = proc_a
        coord._nvr_processes["cam-B"] = proc_b
        await recorder.stop_all(coord)
        # Both must be drained
        assert coord._nvr_processes == {}

    @pytest.mark.asyncio
    async def test_empty_dict_is_safe(self):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        coord._nvr_processes.clear()
        await recorder.stop_all(coord)


# ── _watch_recorder ─────────────────────────────────────────────────────


class TestWatchRecorder:
    @pytest.mark.asyncio
    async def test_clean_exit_no_respawn(self):
        """Process exited cleanly AND was already removed from
        _nvr_processes → no respawn (replacement / clean stop scenario)."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        proc = _mock_proc(returncode=0)
        proc.wait = AsyncMock(return_value=0)
        # Not registered → already replaced/stopped
        with patch.object(recorder, "start_recorder", new=AsyncMock()) as restart:
            await recorder._watch_recorder(coord, CAM_ID, proc)
        restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_respawn_when_gate_closed(self):
        """ffmpeg crashed but should_record now False (cam offline / switch
        toggled off / went REMOTE) → don't respawn."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(conn_type="REMOTE")  # gate now closed
        proc = _mock_proc(returncode=1, stderr_data=b"connection refused")
        proc.wait = AsyncMock(return_value=1)
        coord._nvr_processes[CAM_ID] = proc

        with patch.object(recorder, "start_recorder", new=AsyncMock()) as restart, \
             patch.object(asyncio, "sleep", new=AsyncMock()):
            await recorder._watch_recorder(coord, CAM_ID, proc)
        restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_crash_triggers_respawn(self):
        """ffmpeg crashes within respawn window AND gate still open →
        respawn after delay."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()  # LOCAL, online
        proc = _mock_proc(returncode=1, stderr_data=b"transient")
        proc.wait = AsyncMock(return_value=1)
        coord._nvr_processes[CAM_ID] = proc

        with patch.object(recorder, "start_recorder", new=AsyncMock()) as restart, \
             patch.object(asyncio, "sleep", new=AsyncMock()):
            await recorder._watch_recorder(coord, CAM_ID, proc)
        restart.assert_awaited_once_with(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_second_crash_within_window_gives_up(self):
        """Two crashes inside the respawn window → set error_state, no respawn.
        Defends against an infinite restart loop when the camera is dead."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord()
        # Mark a recent crash
        coord._nvr_recent_crash[CAM_ID] = time.monotonic() - 5  # 5 s ago
        proc = _mock_proc(returncode=1, stderr_data=b"crash 2")
        proc.wait = AsyncMock(return_value=1)
        coord._nvr_processes[CAM_ID] = proc

        with patch.object(recorder, "start_recorder", new=AsyncMock()) as restart, \
             patch.object(asyncio, "sleep", new=AsyncMock()):
            await recorder._watch_recorder(coord, CAM_ID, proc)
        restart.assert_not_called()
        assert "crashed" in coord._nvr_error_state.get(CAM_ID, "").lower()


# ── sync_nvr_cleanup ────────────────────────────────────────────────────


class TestNvrCleanup:
    def test_zero_retention_disables_cleanup(self, tmp_path):
        """retention_days <= 0 → skip entirely. Hard rule: never delete
        all files just because user fat-fingered the option."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        coord.options["nvr_retention_days"] = 0
        # Drop a file that would otherwise be deleted
        old_file = tmp_path / "old.mp4"
        old_file.write_bytes(b"x")
        old_mtime = time.time() - 365 * 86400
        os.utime(old_file, (old_mtime, old_mtime))

        recorder.sync_nvr_cleanup(coord)
        assert old_file.exists(), (
            "retention=0 must skip cleanup — otherwise a typo in the "
            "option deletes a year of recordings."
        )

    def test_missing_base_path_no_op(self):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path="/nonexistent/path/that/does/not/exist")
        # Must not raise
        recorder.sync_nvr_cleanup(coord)

    def test_deletes_files_older_than_cutoff(self, tmp_path):
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        coord.options["nvr_retention_days"] = 7
        # Camera dir + date dir
        cam_dir = tmp_path / "Cam" / "2026-01-01"
        cam_dir.mkdir(parents=True)
        old_file = cam_dir / "00-00.mp4"
        old_file.write_bytes(b"x")
        old_mtime = time.time() - 30 * 86400  # 30 days old
        os.utime(old_file, (old_mtime, old_mtime))

        recent_file = cam_dir / "23-55.mp4"
        recent_file.write_bytes(b"y")
        # Default mtime ≈ now → keeps

        recorder.sync_nvr_cleanup(coord)
        assert not old_file.exists()
        assert recent_file.exists()

    def test_prunes_empty_date_dirs_but_not_camera_root(self, tmp_path):
        """After deleting files, empty date folders are removed. Camera
        root + base_path itself must NEVER be removed."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        coord.options["nvr_retention_days"] = 7
        cam_dir = tmp_path / "Cam"
        date_dir = cam_dir / "2026-01-01"
        date_dir.mkdir(parents=True)
        old_file = date_dir / "00-00.mp4"
        old_file.write_bytes(b"x")
        os.utime(old_file, (time.time() - 30 * 86400, time.time() - 30 * 86400))

        recorder.sync_nvr_cleanup(coord)
        # Date dir gone (empty after deletion)
        assert not date_dir.exists()
        # Camera dir gone too (it became empty after date dir went)
        # But base_path stays
        assert tmp_path.exists()

    def test_keeps_files_at_or_after_cutoff(self, tmp_path):
        """Boundary: file with mtime == cutoff must NOT be deleted
        (condition is `<`, not `<=`)."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        coord.options["nvr_retention_days"] = 7

        cam_dir = tmp_path / "Cam" / "2026-04-29"
        cam_dir.mkdir(parents=True)
        edge_file = cam_dir / "12-00.mp4"
        edge_file.write_bytes(b"x")
        cutoff_ts = time.time() - 7 * 86400 + 60  # 7d - 1min ago = within cutoff
        os.utime(edge_file, (cutoff_ts, cutoff_ts))

        recorder.sync_nvr_cleanup(coord)
        assert edge_file.exists()

    def test_unreadable_file_skipped_not_crash(self, tmp_path):
        """File that os.stat fails on (race: file disappeared mid-walk) must
        be silently skipped, not crash the cleanup loop."""
        from custom_components.bosch_shc_camera import recorder
        coord = _make_coord(base_path=str(tmp_path))
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        good = cam_dir / "good.mp4"
        good.write_bytes(b"x")

        # Patch os.stat to fail for one file
        real_stat = os.stat
        def _flaky_stat(path, *args, **kwargs):
            if path.endswith("good.mp4"):
                raise OSError("file vanished")
            return real_stat(path, *args, **kwargs)

        with patch.object(os, "stat", side_effect=_flaky_stat):
            recorder.sync_nvr_cleanup(coord)
        # Must not have raised; the file is still there because we didn't
        # get to the unlink call.
        assert good.exists()
