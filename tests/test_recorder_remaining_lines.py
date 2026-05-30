"""Targeted coverage tests for recorder.py remaining uncovered lines.

Targets:
  Line 227   — _list_preroll_segments: non-file entry (dir/symlink) → continue
  Lines 230-231 — _list_preroll_segments: os.stat raises OSError → continue
  Lines 354-355 — start_preroll_recorder: prune_preroll_cache raises Exception → pass
  Line 359   — start_preroll_recorder: _nvr_preroll_tasks attr absent → auto-create {}
  Lines 384-385 — stop_preroll_recorder: send_signal raises ProcessLookupError → return
  Lines 475-481 — create_motion_clip: communicate() times out → kill + return False
  Lines 486-487 — create_motion_clip: os.unlink(concat_file) raises OSError → pass (swallowed)
  Lines 660-661 — stop_recorder: proc.kill() raises ProcessLookupError after SIGTERM timeout
  Lines 664-665 — stop_recorder: second wait_for also times out after SIGKILL
  Line 737   — _watch_recorder: gate closed after sleep → silent return (no respawn)

SENTINEL_RULE: float('-inf') for all monotonic-based "last done" defaults.
"""

from __future__ import annotations

import asyncio
import os
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_TITLE = "Terrasse"


# ── shared stubs ─────────────────────────────────────────────────────────────


def _make_coord(tmp_path=None, *, conn_type: str = "LOCAL"):
    """Full-featured stub coordinator."""
    proxy_url = "rtsp://user:pass@127.0.0.1:46597/rtsp_tunnel?inst=1"
    base = str(tmp_path) if tmp_path else "/tmp/nvr_test_remaining"

    async def _run_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _create_bg_task(coro, name=None):
        coro.close()
        task = MagicMock()
        task.add_done_callback = MagicMock()
        return task

    coord = SimpleNamespace(
        _live_connections={
            CAM_ID: {
                "_connection_type": conn_type,
                "rtspsUrl": proxy_url,
            }
        },
        _nvr_processes={},
        _nvr_preroll_processes={},
        _nvr_preroll_segment_counts={},
        _nvr_preroll_tasks={},
        _nvr_user_intent={CAM_ID: True},
        _nvr_recent_crash={CAM_ID: float("-inf")},
        _nvr_error_state={},
        _bg_tasks=set(),
        data={CAM_ID: {"info": {"title": CAM_TITLE}, "status": "ONLINE"}},
        options={
            "nvr_base_path": base,
            "nvr_retention_days": 3,
            "enable_nvr": True,
            "nvr_preroll_cache_dir": base + "/preroll_cache",
            "nvr_preroll_seconds": 30,
        },
        is_camera_online=lambda cid: True,
    )
    coord.hass = SimpleNamespace(
        async_add_executor_job=_run_executor,
        async_create_background_task=_create_bg_task,
    )
    return coord


def _mock_proc(returncode=None):
    proc = MagicMock()
    proc.returncode = returncode
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    final_rc = returncode if returncode is not None else 0

    async def _wait():
        proc.returncode = final_rc
        return final_rc

    proc.wait = _wait
    proc.stderr = None
    return proc


# ── Line 227: non-file entry in _list_preroll_segments → continue ─────────


class TestListPrerollSegmentsNonFile:
    def test_directory_entry_skipped(self, tmp_path):
        """A subdirectory inside cam_dir must be skipped (line 227 continue)."""
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            _list_preroll_segments,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        # Create a subdirectory — os.path.isfile returns False → continue
        (cam_dir / "subdir").mkdir()
        # Also create one valid segment so the function returns something
        seg = cam_dir / "seg.mp4"
        seg.write_bytes(b"x" * _PREROLL_MIN_SIZE_BYTES)

        result = _list_preroll_segments(str(cam_dir))
        paths = [r[0] for r in result]
        assert str(seg) in paths
        assert str(cam_dir / "subdir") not in paths


# ── Lines 230-231: os.stat raises OSError → continue ─────────────────────


class TestListPrerollSegmentsStatError:
    def test_stat_oserror_is_skipped(self, tmp_path):
        """If os.stat raises OSError (file vanished after listdir), skip it."""
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            _list_preroll_segments,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        ghost = cam_dir / "ghost.mp4"
        ghost.write_bytes(b"x" * _PREROLL_MIN_SIZE_BYTES)
        good = cam_dir / "good.mp4"
        good.write_bytes(b"x" * _PREROLL_MIN_SIZE_BYTES)

        real_stat = os.stat

        def _flaky_stat(path, *args, **kwargs):
            if str(path) == str(ghost):
                raise OSError("vanished")
            return real_stat(path, *args, **kwargs)

        with patch("os.stat", side_effect=_flaky_stat):
            result = _list_preroll_segments(str(cam_dir))

        paths = [r[0] for r in result]
        assert str(good) in paths
        assert str(ghost) not in paths


# ── Lines 354-355: prune raises Exception in start_preroll_recorder → pass ──


class TestStartPrerollPruneException:
    @pytest.mark.asyncio
    async def test_prune_exception_swallowed(self, tmp_path):
        """If prune_preroll_cache raises any Exception, start_preroll continues."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        proc = _mock_proc(returncode=None)

        async def _spawn(*a, **kw):
            return proc

        with (
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn),
            patch.object(
                recorder, "prune_preroll_cache", side_effect=RuntimeError("disk full")
            ),
        ):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        # Process must still be registered despite the prune failure
        assert coord._nvr_preroll_processes.get(CAM_ID) is proc


# ── Line 359: _nvr_preroll_tasks absent → auto-created ───────────────────


class TestStartPrerollTasksAutoCreate:
    @pytest.mark.asyncio
    async def test_preroll_tasks_auto_created_when_absent(self, tmp_path):
        """If coordinator has no _nvr_preroll_tasks attr, it is created (line 359)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        # Remove the attribute to trigger the hasattr branch
        del coord._nvr_preroll_tasks

        proc = _mock_proc(returncode=None)

        async def _spawn(*a, **kw):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        # The attribute must now exist and contain the registered task
        assert hasattr(coord, "_nvr_preroll_tasks")
        assert CAM_ID in coord._nvr_preroll_tasks


# ── Lines 384-385: stop_preroll_recorder send_signal raises ProcessLookupError


class TestStopPrerollSendSignalProcessLookupError:
    @pytest.mark.asyncio
    async def test_send_signal_process_lookup_error_returns(self):
        """ProcessLookupError from send_signal means process is already gone — return."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock(side_effect=ProcessLookupError)
        coord._nvr_preroll_processes[CAM_ID] = proc

        # Must not raise
        await recorder.stop_preroll_recorder(coord, CAM_ID)

        # Process must have been popped
        assert CAM_ID not in coord._nvr_preroll_processes


# ── Lines 475-481: create_motion_clip communicate() timeout ──────────────


class TestCreateMotionClipTimeout:
    @pytest.mark.asyncio
    async def test_communicate_timeout_returns_false(self, tmp_path):
        """If ffmpeg's communicate() hangs > 30 s, kill it and return False."""
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        # Write a real pre-roll segment so list_preroll_files returns something
        cam_name = CAM_TITLE
        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cam_cache = os.path.join(cache_dir, cam_name)
        os.makedirs(cam_cache, exist_ok=True)
        seg = os.path.join(cam_cache, "seg.mp4")
        with open(seg, "wb") as f:
            f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)

        coord = _make_coord(tmp_path)
        coord.options["nvr_preroll_cache_dir"] = cache_dir
        output = str(tmp_path / "out.mp4")

        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()

        async def _hanging_communicate():
            await asyncio.sleep(9999)

        proc.communicate = _hanging_communicate

        async def _spawn(*a, **kw):
            return proc

        with (
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn),
            patch("asyncio.wait_for", side_effect=asyncio.TimeoutError),
        ):
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        assert result is False
        proc.kill.assert_called_once()


# ── Lines 486-487: concat file os.unlink raises OSError → pass ───────────


class TestCreateMotionClipUnlinkError:
    @pytest.mark.asyncio
    async def test_concat_unlink_oserror_swallowed(self, tmp_path):
        """OSError when removing concat file must be swallowed (line 486-487)."""
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        cam_name = CAM_TITLE
        cache_dir = str(tmp_path / "cache")
        cam_cache = os.path.join(cache_dir, cam_name)
        os.makedirs(cam_cache, exist_ok=True)
        seg = os.path.join(cam_cache, "seg.mp4")
        with open(seg, "wb") as f:
            f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)

        coord = _make_coord(tmp_path)
        coord.options["nvr_preroll_cache_dir"] = cache_dir
        output = str(tmp_path / "out.mp4")

        proc = MagicMock()
        proc.returncode = 0

        async def _communicate():
            return (b"", b"")

        proc.communicate = _communicate

        async def _spawn(*a, **kw):
            return proc

        real_executor = coord.hass.async_add_executor_job

        async def _executor_with_unlink_error(fn, *args, **kwargs):
            # Make os.unlink fail only for the concat file
            if fn is os.unlink and args and ".concat.txt" in str(args[0]):
                raise OSError("read-only")
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _executor_with_unlink_error

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        # Result depends on returncode=0; the OSError must not propagate
        assert result is True


# ── Lines 660-661: stop_recorder proc.kill raises ProcessLookupError ─────


class TestStopRecorderKillProcessLookupError:
    @pytest.mark.asyncio
    async def test_kill_process_lookup_error_swallowed(self):
        """If proc.kill() raises ProcessLookupError after SIGTERM timeout, swallow it."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock()
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        coord._nvr_processes[CAM_ID] = proc

        # Both wait_for calls time out: SIGTERM window then SIGKILL window
        call_count = 0

        async def _always_timeout(coro, timeout):
            try:
                coro.close()
            except Exception:
                pass
            call_count_ref[0] += 1
            raise TimeoutError

        call_count_ref = [0]

        with patch("asyncio.wait_for", side_effect=_always_timeout):
            await recorder.stop_recorder(coord, CAM_ID)

        # kill was attempted (and swallowed ProcessLookupError)
        proc.kill.assert_called_once()
        # Must have called wait_for twice (SIGTERM + SIGKILL windows)
        assert call_count_ref[0] == 2


# ── Lines 664-665: stop_recorder second wait_for also times out ───────────


class TestStopRecorderDoubleTimeout:
    @pytest.mark.asyncio
    async def test_second_wait_timeout_logs_warning_no_raise(self):
        """After SIGKILL, if wait_for times out again, log warning but don't raise."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock()
        proc.kill = MagicMock()  # succeeds
        coord._nvr_processes[CAM_ID] = proc

        calls = []

        async def _timeout_twice(coro, timeout):
            try:
                coro.close()
            except Exception:
                pass
            calls.append(timeout)
            raise TimeoutError

        with patch("asyncio.wait_for", side_effect=_timeout_twice):
            await recorder.stop_recorder(coord, CAM_ID)

        # Must not raise; two timeouts (SIGTERM + post-SIGKILL)
        assert len(calls) == 2
        proc.kill.assert_called_once()


# ── Line 737: _watch_recorder gate closed after sleep → silent return ─────


class TestWatchRecorderGateClosedAfterSleep:
    @pytest.mark.asyncio
    async def test_gate_closed_after_sleep_no_respawn(self):
        """If should_record returns False after the respawn sleep, don't respawn."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord()
        proc = _mock_proc(returncode=1)  # non-zero → crash path
        coord._nvr_processes[CAM_ID] = proc

        # Make should_record return True first time (pre-sleep check at line 717)
        # then False after sleep (line 736), so we never call start_recorder.
        # We toggle the flag via a call counter.
        call_count = [0]
        real_should_record = recorder.should_record

        def _toggling_should_record(c, cid, *, switch_on):
            call_count[0] += 1
            if call_count[0] == 1:
                return True  # line 717: gate open → proceed to sleep
            return False  # line 736: gate now closed → return without respawn

        sleep_calls = []

        async def _no_sleep(secs):
            sleep_calls.append(secs)

        start_calls = []

        async def _mock_start(c, cid):
            start_calls.append(cid)

        with (
            patch.object(
                recorder, "should_record", side_effect=_toggling_should_record
            ),
            patch("asyncio.sleep", side_effect=_no_sleep),
            patch.object(recorder, "start_recorder", side_effect=_mock_start),
        ):
            # Need elapsed >= _RESPAWN_WINDOW_SECONDS to skip crash-loop guard
            import time

            with patch("time.monotonic", return_value=9999.0):
                await recorder._watch_recorder(coord, CAM_ID, proc)

        # Sleep happened (respawn delay)
        assert len(sleep_calls) == 1
        # But start_recorder was NOT called (gate was closed)
        assert len(start_calls) == 0
