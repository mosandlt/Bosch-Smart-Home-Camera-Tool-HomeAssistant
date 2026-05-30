"""Coverage push for recorder.py watch loops + pre-roll spawn (MEDIUM).

Targets three async paths still uncovered after the LOW round:

  1. `_watch_preroll_recorder` (lines 287-300) — periodic-prune loop
     that runs while a pre-roll ffmpeg child is alive. Exits when
     `proc.returncode is not None`. Without a fake-clock the loop
     would block 10 s/iteration; we patch `asyncio.sleep` to no-op.

  2. `_watch_recorder` stderr-drain TimeoutError fall-through
     (lines 706-707) — the existing tests cover the happy drain
     and stderr=None branches; this one forces `stderr.read` to
     hang past 1 s so `asyncio.wait_for` raises `asyncio.TimeoutError`
     and the watcher must keep going (no crash, no respawn-skip).

  3. `start_preroll_recorder` LOCAL-gating full path + ffmpeg
     `FileNotFoundError` cleanup (lines 308-343). LOCAL + valid
     `rtspsUrl` arms the full code path; `FileNotFoundError`
     during `create_subprocess_exec` must leave
     `_nvr_preroll_processes[cam_id]` unset.

SENTINEL_RULE compliance: every monotonic-based default in this file
uses `float('-inf')` so `(now - last)` ≥ interval checks are True on
fresh CI VMs (~200 s uptime). Same rule as `feedback_test_sentinel.md`.

User/forum source: project-internal Mini-NVR Phase 4 (pre-roll buffer)
+ regression hardening before the v11.2.x release line — leaks in the
watch loops are silent-but-fatal (ffmpeg orphan + tmpfs fill).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_TITLE = "Terrasse"


# ── shared coordinator stub ─────────────────────────────────────────────


def _make_coord(tmp_path, *, conn_type: str = "LOCAL"):
    """Stub coordinator with the fields recorder.py touches in the
    pre-roll + watch_recorder paths."""
    proxy_url = "rtsp://user:pass@127.0.0.1:46597/rtsp_tunnel?inst=1"

    async def _run_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def _create_bg_task(coro, name=None):
        # Close the coroutine to silence "never awaited" warnings —
        # we don't actually run the watcher task here.
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
        # SENTINEL_RULE: float('-inf') so monotonic-window checks pass on
        # fresh CI VMs (uptime ~200 s).
        _nvr_recent_crash={CAM_ID: float("-inf")},
        _nvr_error_state={},
        _bg_tasks=set(),
        data={CAM_ID: {"info": {"title": CAM_TITLE}, "status": "ONLINE"}},
        options={
            "nvr_preroll_cache_dir": str(tmp_path),
            "nvr_preroll_seconds": 30,
            "nvr_base_path": str(tmp_path / "nvr"),
            "nvr_retention_days": 3,
            "enable_nvr": True,
        },
        is_camera_online=lambda cid: True,
    )
    coord.hass = SimpleNamespace(
        async_add_executor_job=_run_executor,
        async_create_background_task=_create_bg_task,
    )
    return coord


def _mock_proc(returncode=None, *, stderr=None):
    """Build a minimal asyncio.subprocess.Process stub."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.send_signal = MagicMock()
    proc.kill = MagicMock()
    proc.stderr = stderr

    final_rc = returncode if returncode is not None else 0

    async def _wait():
        proc.returncode = final_rc
        return final_rc

    proc.wait = _wait
    return proc


# ── 1. _watch_preroll_recorder ───────────────────────────────────────────


class TestWatchPrerollRecorder:
    """Lines 287-300 — periodic prune loop, exits on process death."""

    @pytest.mark.asyncio
    async def test_periodic_prune_called_then_exits_on_proc_exit(self, tmp_path):
        """One prune iteration → proc.returncode set → loop exits.

        Fake `asyncio.sleep` (no real-time wait). After the first wakeup
        the watcher calls `prune_preroll_cache` once; before the second
        wakeup we set `proc.returncode = 0` so the early-return triggers
        and the loop exits cleanly.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        coord._nvr_preroll_processes[CAM_ID] = proc

        prune_calls: list[tuple[str, int]] = []

        def _fake_prune(cam_dir, max_segs):
            prune_calls.append((cam_dir, max_segs))
            # After 1 prune call, mark the proc as exited so the next
            # iteration returns. Otherwise the loop is infinite.
            if len(prune_calls) >= 1:
                proc.returncode = 0
            return 0

        sleep_count = {"n": 0}

        async def _fake_sleep(_secs):
            sleep_count["n"] += 1
            return None

        with (
            patch.object(recorder, "prune_preroll_cache", _fake_prune),
            patch("asyncio.sleep", _fake_sleep),
        ):
            await recorder._watch_preroll_recorder(
                coord,
                CAM_ID,
                str(tmp_path / "cam"),
                max_segs=4,
            )

        # prune must have been invoked at least once (line 296-298)
        assert len(prune_calls) >= 1
        # cam_dir + max_segs passed through unchanged
        assert prune_calls[0] == (str(tmp_path / "cam"), 4)
        # Loop slept at least twice (first to call prune, second to see
        # the dead proc and return). Pure smoke for the periodic shape.
        assert sleep_count["n"] >= 2

    @pytest.mark.asyncio
    async def test_exits_when_proc_missing_from_dict(self, tmp_path):
        """If `_nvr_preroll_processes[cam_id]` is gone (clean stop /
        crash race), the watcher must exit on the next tick — line 293.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        # Note: NO proc registered for CAM_ID

        async def _fake_sleep(_secs):
            return None

        with (
            patch.object(recorder, "prune_preroll_cache") as prune,
            patch("asyncio.sleep", _fake_sleep),
        ):
            await recorder._watch_preroll_recorder(
                coord,
                CAM_ID,
                str(tmp_path / "cam"),
                max_segs=4,
            )

        # No prune attempted because we bailed before line 296.
        prune.assert_not_called()

    @pytest.mark.asyncio
    async def test_prune_exception_swallowed_then_proc_exits(self, tmp_path):
        """Lines 299-300 — `prune_preroll_cache` raising must not kill
        the watcher. After the swallow we let the proc exit on the next
        tick so the test terminates."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        coord._nvr_preroll_processes[CAM_ID] = proc

        call_count = {"n": 0}

        def _bad_prune(cam_dir, max_segs):
            call_count["n"] += 1
            # First call raises — must be swallowed.
            # On the second call, mark proc dead so the loop exits.
            if call_count["n"] >= 1:
                proc.returncode = 0
            raise RuntimeError("disk gone")

        async def _fake_sleep(_secs):
            return None

        with (
            patch.object(recorder, "prune_preroll_cache", _bad_prune),
            patch("asyncio.sleep", _fake_sleep),
        ):
            # Must NOT raise — exception path 299-300 swallows.
            await recorder._watch_preroll_recorder(
                coord,
                CAM_ID,
                str(tmp_path / "cam"),
                max_segs=4,
            )

        assert call_count["n"] >= 1


# ── 2. _watch_recorder stderr-drain TimeoutError fall-through ────────────


class TestWatchRecorderDrainTimeout:
    """Lines 706-707 — drain hangs > 1 s → asyncio.TimeoutError swallowed."""

    @pytest.mark.asyncio
    async def test_drain_timeout_no_crash(self, tmp_path):
        """If `proc.stderr.read(2048)` never resolves, `asyncio.wait_for`
        raises `asyncio.TimeoutError` which the `except` on line 706
        swallows. The watcher must continue (not crash, not propagate)
        — production: a frozen TCP stack on the camera mustn't kill
        the integration's task pool.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)

        # stderr.read returns a coroutine that hangs forever. We then
        # patch `asyncio.wait_for` to raise TimeoutError so we don't
        # actually block.
        stderr = MagicMock()

        async def _hang(_n):
            await asyncio.sleep(3600)  # would block; wait_for short-circuits

        stderr.read = _hang

        proc = _mock_proc(returncode=1, stderr=stderr)
        coord._nvr_processes[CAM_ID] = proc

        # User intent stays True, so the watcher will try to respawn —
        # we patch `start_recorder` to a no-op so we observe only the
        # drain branch.
        respawn_called = {"n": 0}

        async def _fake_start(_coord, _cam):
            respawn_called["n"] += 1

        # Patch wait_for so the drain hits TimeoutError instantly,
        # and the respawn sleep (line 735) returns immediately.
        original_wait_for = asyncio.wait_for

        async def _fake_wait_for(coro, timeout):
            # Coroutine cleanup: close so we don't warn.
            if asyncio.iscoroutine(coro):
                coro.close()
            raise TimeoutError()

        async def _fake_sleep(_secs):
            return None

        with (
            patch.object(recorder, "start_recorder", _fake_start),
            patch("asyncio.wait_for", _fake_wait_for),
            patch("asyncio.sleep", _fake_sleep),
        ):
            # Must NOT raise. Drain TimeoutError caught at line 706,
            # rest of watcher runs to completion (respawn path).
            await recorder._watch_recorder(coord, CAM_ID, proc)

        # Watcher reached the respawn branch → drain swallowed correctly.
        assert respawn_called["n"] == 1

    @pytest.mark.asyncio
    async def test_drain_generic_exception_swallowed(self, tmp_path):
        """Same fall-through for non-Timeout exceptions (`except (...,
        Exception)` on line 706) — e.g. stderr already closed."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        stderr = MagicMock()
        stderr.read = AsyncMock(side_effect=ValueError("stream closed"))
        proc = _mock_proc(returncode=1, stderr=stderr)
        coord._nvr_processes[CAM_ID] = proc

        async def _fake_start(_coord, _cam):
            pass

        async def _fake_sleep(_secs):
            return None

        with (
            patch.object(recorder, "start_recorder", _fake_start),
            patch("asyncio.sleep", _fake_sleep),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc)
        # No assertion needed beyond "did not raise".


# ── 3. start_preroll_recorder LOCAL-gating + FileNotFoundError ───────────


class TestStartPrerollRecorder:
    """Lines 308-343 — LOCAL gate + ffmpeg-not-found cleanup."""

    @pytest.mark.asyncio
    async def test_skipped_when_not_local(self, tmp_path):
        """`_connection_type != "LOCAL"` → early return (line 309).
        No spawn, no proc registered. Pre-roll is LAN-only by design."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path, conn_type="REMOTE")
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_skipped_when_rtsp_url_missing(self, tmp_path):
        """rtspsUrl empty / not rtsp:// → return (lines 310-312)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        coord._live_connections[CAM_ID]["rtspsUrl"] = ""
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_happy_path_full_local(self, tmp_path):
        """LOCAL + valid rtsp:// URL → walks the full path: makedirs,
        spawn, register proc, prune-on-spawn, register watcher task.
        Covers lines 308-366."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        proc = _mock_proc(returncode=None)

        async def _spawn(*_args, **_kwargs):
            return proc

        prune_calls: list[tuple[str, int]] = []

        def _fake_prune(cam_dir, max_segs):
            prune_calls.append((cam_dir, max_segs))
            return 0

        with (
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn),
            patch.object(recorder, "prune_preroll_cache", _fake_prune),
        ):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        # Process registered
        assert coord._nvr_preroll_processes[CAM_ID] is proc
        # Cache dir created under tmp_path / Terrasse
        assert (tmp_path / CAM_TITLE).exists()
        # Prune-on-spawn happened with the computed max_segs.
        # nvr_preroll_seconds=30 → ceil(30/10)+1 = 4
        assert len(prune_calls) == 1
        assert prune_calls[0][1] == 4
        # Watcher task registered
        assert CAM_ID in coord._nvr_preroll_tasks
        assert coord._nvr_preroll_tasks[CAM_ID] is not None

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_cleanup(self, tmp_path):
        """`create_subprocess_exec` → `FileNotFoundError` (line 338-340).
        Must log error + return WITHOUT registering proc or task —
        otherwise stop_preroll would later iterate over a None proc."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            # Must not raise.
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert CAM_ID not in coord._nvr_preroll_processes
        # Pre-roll watcher task also not registered (line 360 not reached).
        assert CAM_ID not in coord._nvr_preroll_tasks

    @pytest.mark.asyncio
    async def test_spawn_oserror_cleanup(self, tmp_path):
        """Generic OSError on spawn (line 341-343) — same cleanup
        invariant as the FileNotFoundError path."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("EAGAIN — fork limit"),
        ):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert CAM_ID not in coord._nvr_preroll_processes
        assert CAM_ID not in coord._nvr_preroll_tasks

    @pytest.mark.asyncio
    async def test_makedirs_failure_aborts(self, tmp_path):
        """OSError during cache_dir creation (line 324-326) → return,
        no spawn. Read-only fs / permission denied / NFS hiccup."""
        import os as _os

        from custom_components.bosch_shc_camera import recorder

        coord = _make_coord(tmp_path)

        async def _bad_executor(fn, *args, **kwargs):
            if fn is _os.makedirs:
                raise OSError("EROFS")
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _bad_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_preroll_processes
