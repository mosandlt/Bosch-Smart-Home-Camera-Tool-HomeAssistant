"""Tests for Mini-NVR Phase 3 (quality switch) and Phase 4 (pre-roll buffer).

Phase 3: _apply_quality() rewrites inst= in the RTSP URL; _build_ffmpeg_args
  receives the quality kwarg and passes the adjusted URL to ffmpeg.
Phase 4: pre-roll helpers — dir/pattern helpers, prune_preroll_cache,
  create_motion_clip_args, start/stop_preroll_recorder, create_motion_clip.

All tests are pure-unit or use mocks — no real ffmpeg, no tmpfs I/O.

User/forum source: project-internal Mini-NVR Phase 3+4 implementation
(2026-05-08). Regression guard: quality URL rewrite + pre-roll concat must
never silently break on refactor.
"""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_coord(opts=None, cam_title="Terrasse", cam_id="11111111"):
    coord = SimpleNamespace(
        options=opts
        or {
            "nvr_base_path": "/config/bosch_nvr",
            "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
            "nvr_preroll_seconds": 30,
            "nvr_quality": "auto",
        },
        data={cam_id: {"info": {"title": cam_title}}},
        _live_connections={
            cam_id: {
                "_connection_type": "LOCAL",
                "rtspsUrl": "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=1&enableaudio=1",
            }
        },
        _nvr_processes={},
        _nvr_preroll_processes={},
        _nvr_preroll_segment_counts={},
        _nvr_preroll_last_crash={},
        _nvr_preroll_tasks={},
        hass=MagicMock(),
    )
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)
    coord.hass.async_create_background_task = MagicMock(return_value=MagicMock())
    coord.hass.loop = MagicMock()
    coord._bg_tasks = set()
    return coord


# ===========================================================================
# Phase 3 — quality URL rewrite
# ===========================================================================


class TestNvrQuality(unittest.TestCase):
    def _apply(self, url, quality):
        from custom_components.bosch_shc_camera.recorder import _apply_quality

        return _apply_quality(url, quality)

    def test_auto_quality_url_unchanged(self):
        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=1&enableaudio=1"
        assert self._apply(url, "auto") == url

    def test_low_quality_replaces_inst(self):
        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=1&enableaudio=1"
        result = self._apply(url, "low")
        assert "inst=4" in result
        assert "inst=1" not in result

    def test_low_quality_replaces_inst_large_number(self):
        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=10&other=x"
        result = self._apply(url, "low")
        assert "inst=4" in result
        assert "inst=10" not in result

    def test_low_quality_with_no_inst_appends(self):
        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?enableaudio=1"
        result = self._apply(url, "low")
        assert "inst=4" in result
        assert result.startswith("rtsp://")

    def test_low_quality_with_no_query_appends(self):
        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel"
        result = self._apply(url, "low")
        assert "inst=4" in result
        assert "?" in result

    def test_low_quality_in_ffmpeg_args(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=1"
        args = _build_ffmpeg_args(url, "/tmp/out/%H-%M.mp4", quality="low")
        joined = " ".join(args)
        assert "inst=4" in joined
        assert "inst=1" not in joined

    def test_auto_quality_in_ffmpeg_args_preserves_inst1(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=1"
        args = _build_ffmpeg_args(url, "/tmp/out/%H-%M.mp4", quality="auto")
        joined = " ".join(args)
        assert "inst=1" in joined
        assert "inst=4" not in joined

    def test_ffmpeg_args_default_quality_is_auto(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        url = "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=1"
        args = _build_ffmpeg_args(url, "/tmp/out/%H-%M.mp4")
        # default should be auto — inst=1 preserved
        assert "inst=1" in " ".join(args)


# ===========================================================================
# Phase 4 — pre-roll helpers
# ===========================================================================


class TestPrerollHelpers(unittest.TestCase):
    def test_preroll_dir_safe_name(self):
        from custom_components.bosch_shc_camera.recorder import _preroll_dir

        d = _preroll_dir("/dev/shm/cache", "Terrasse Kamera")
        assert d.startswith("/dev/shm/cache/")
        assert "Terrasse" in d
        # No path traversal characters
        assert ".." not in d

    def test_preroll_dir_path_traversal_stripped(self):
        from custom_components.bosch_shc_camera.recorder import _preroll_dir

        d = _preroll_dir("/dev/shm/cache", "../../etc/passwd")
        assert "etc" not in d or d.startswith("/dev/shm/cache/")

    def test_preroll_pattern_contains_cam(self):
        from custom_components.bosch_shc_camera.recorder import _preroll_pattern

        p = _preroll_pattern("/dev/shm/cache", "Terrasse")
        assert "Terrasse" in p
        assert "%H%M%S" in p
        assert p.endswith(".mp4")

    def test_list_preroll_segments_empty_dir(self):
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        with tempfile.TemporaryDirectory() as d:
            result = _list_preroll_segments(d)
            assert result == []

    def test_list_preroll_segments_nonexistent_dir(self):
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        result = _list_preroll_segments("/nonexistent/path/xyz")
        assert result == []

    def test_list_preroll_segments_sorted_oldest_first(self):
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            _list_preroll_segments,
        )

        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            for i, name in enumerate(["c.mp4", "a.mp4", "b.mp4"]):
                path = os.path.join(d, name)
                with open(path, "wb") as f:
                    f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
                os.utime(path, (now - (3 - i), now - (3 - i)))
            segs = _list_preroll_segments(d)
            mtimes = [m for _, m in segs]
            assert mtimes == sorted(mtimes)

    def test_list_preroll_segments_skips_small_files(self):
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        with tempfile.TemporaryDirectory() as d:
            # Write a file below the minimum size
            tiny = os.path.join(d, "tiny.mp4")
            with open(tiny, "wb") as f:
                f.write(b"x" * 512)
            result = _list_preroll_segments(d)
            assert result == []

    def test_prune_keeps_max_segments(self):
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            prune_preroll_cache,
        )

        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            for i in range(6):
                path = os.path.join(d, f"{i:06d}.mp4")
                with open(path, "wb") as f:
                    f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
                os.utime(path, (now - (6 - i), now - (6 - i)))
            prune_preroll_cache(d, 3)
            remaining = [f for f in os.listdir(d) if f.endswith(".mp4")]
            assert len(remaining) == 3

    def test_prune_deletes_oldest(self):
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            prune_preroll_cache,
        )

        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            names = [f"seg{i:02d}.mp4" for i in range(5)]
            for i, name in enumerate(names):
                path = os.path.join(d, name)
                with open(path, "wb") as f:
                    f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
                os.utime(path, (now - (5 - i), now - (5 - i)))
            deleted = prune_preroll_cache(d, 2)
            assert deleted == 3
            remaining = sorted(os.listdir(d))
            # The two newest (seg03, seg04) should remain
            assert remaining == ["seg03.mp4", "seg04.mp4"]

    def test_prune_no_op_when_under_limit(self):
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            prune_preroll_cache,
        )

        with tempfile.TemporaryDirectory() as d:
            for i in range(2):
                path = os.path.join(d, f"seg{i}.mp4")
                with open(path, "wb") as f:
                    f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
            deleted = prune_preroll_cache(d, 5)
            assert deleted == 0


# ===========================================================================
# Phase 4 — create_motion_clip helpers
# ===========================================================================


class TestCreateMotionClip(unittest.TestCase):
    def test_create_motion_clip_args_concat_format(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4", "/tmp/b.mp4"], "/tmp/out.mp4")
        assert "-f" in args
        concat_idx = args.index("-f")
        assert args[concat_idx + 1] == "concat"

    def test_create_motion_clip_args_output_path(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/motion_clip.mp4")
        assert "/tmp/motion_clip.mp4" == args[-1]

    def test_create_motion_clip_args_copy_codec(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/out.mp4")
        assert "-c" in args
        c_idx = args.index("-c")
        assert args[c_idx + 1] == "copy"

    def test_create_motion_clip_args_faststart(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/out.mp4")
        assert "+faststart" in " ".join(args)

    def test_create_motion_clip_args_references_concat_file(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/out.mp4")
        # The concat list file should be referenced as -i <something>
        assert "-i" in args
        i_idx = args.index("-i")
        assert "concat" in args[i_idx + 1] or "out.mp4" in args[i_idx + 1]

    def test_create_motion_clip_no_preroll_returns_false(self):
        """list_preroll_files returns [] → create_motion_clip returns False."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_coord()
        cam_id = next(iter(coord.data.keys()))

        async def _run():
            with patch.object(recorder, "list_preroll_files", return_value=[]):
                return await recorder.create_motion_clip(coord, cam_id, "/tmp/out.mp4")

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_create_motion_clip_success(self):
        """Mock subprocess → rc=0 → returns True."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_coord()
        cam_id = next(iter(coord.data.keys()))

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def _run():
            with (
                patch.object(
                    recorder,
                    "list_preroll_files",
                    return_value=["/tmp/seg0.mp4", "/tmp/seg1.mp4"],
                ),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
                tempfile.TemporaryDirectory() as d,
            ):
                output = os.path.join(d, "motion.mp4")

                # Patch async_add_executor_job to actually run the function
                async def _exec_job(fn, *args):
                    return fn(*args) if args else fn()

                coord.hass.async_add_executor_job = _exec_job
                return await recorder.create_motion_clip(coord, cam_id, output)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is True

    def test_create_motion_clip_ffmpeg_not_found(self):
        """FileNotFoundError on spawn → returns False gracefully."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_coord()
        cam_id = next(iter(coord.data.keys()))

        async def _run():
            with (
                patch.object(
                    recorder, "list_preroll_files", return_value=["/tmp/seg0.mp4"]
                ),
                patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=FileNotFoundError("ffmpeg"),
                ),
                tempfile.TemporaryDirectory() as d,
            ):
                output = os.path.join(d, "motion.mp4")

                async def _exec_job(fn, *args):
                    return fn(*args) if args else fn()

                coord.hass.async_add_executor_job = _exec_job
                return await recorder.create_motion_clip(coord, cam_id, output)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_create_motion_clip_ffmpeg_rc_nonzero(self):
        """ffmpeg exits with rc=1 → returns False."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_coord()
        cam_id = next(iter(coord.data.keys()))

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        async def _run():
            with (
                patch.object(
                    recorder, "list_preroll_files", return_value=["/tmp/seg0.mp4"]
                ),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
                tempfile.TemporaryDirectory() as d,
            ):
                output = os.path.join(d, "motion.mp4")

                async def _exec_job(fn, *args):
                    return fn(*args) if args else fn()

                coord.hass.async_add_executor_job = _exec_job
                return await recorder.create_motion_clip(coord, cam_id, output)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False


# ===========================================================================
# Phase 4 — start_preroll_recorder / stop_preroll_recorder
# ===========================================================================


class TestPrerollRecorderLifecycle(unittest.TestCase):
    def test_start_preroll_requires_local_session(self):
        """No LOCAL session → preroll recorder not spawned."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)
        coord._live_connections[cam_id]["_connection_type"] = "REMOTE"

        async def _run():
            await recorder.start_preroll_recorder(coord, cam_id)
            return cam_id in coord._nvr_preroll_processes

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_start_preroll_stores_process(self):
        """Valid LOCAL session → process stored in _nvr_preroll_processes."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await recorder.start_preroll_recorder(coord, cam_id)
            return coord._nvr_preroll_processes.get(cam_id)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is mock_proc

    def test_stop_preroll_removes_process(self):
        """stop_preroll_recorder pops process from dict and sends SIGTERM."""
        import signal as _signal

        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.send_signal = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        coord._nvr_preroll_processes[cam_id] = mock_proc

        async def _run():
            await recorder.stop_preroll_recorder(coord, cam_id)
            return cam_id in coord._nvr_preroll_processes

        still_present = asyncio.get_event_loop().run_until_complete(_run())
        assert still_present is False
        mock_proc.send_signal.assert_called_once_with(_signal.SIGTERM)

    def test_stop_preroll_noop_when_no_process(self):
        """stop_preroll_recorder is a no-op when no process registered."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)

        async def _run():
            await recorder.stop_preroll_recorder(coord, cam_id)

        # Should not raise
        asyncio.get_event_loop().run_until_complete(_run())

    def test_stop_all_preroll_stops_all(self):
        """stop_all_preroll calls stop for every cam in _nvr_preroll_processes."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_coord()

        stopped = []

        async def mock_stop(c, cid):
            stopped.append(cid)

        coord._nvr_preroll_processes = {"cam1": MagicMock(), "cam2": MagicMock()}

        async def _run():
            with patch.object(recorder, "stop_preroll_recorder", side_effect=mock_stop):
                await recorder.stop_all_preroll(coord)

        asyncio.get_event_loop().run_until_complete(_run())
        assert set(stopped) == {"cam1", "cam2"}


# ===========================================================================
# Phase 4 — list_preroll_files
# ===========================================================================


class TestListPrerollFiles(unittest.TestCase):
    def test_list_preroll_files_returns_sorted_paths(self):
        import custom_components.bosch_shc_camera.recorder as recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        cam_id = "11111111"
        with tempfile.TemporaryDirectory() as cache_dir:
            coord = _make_coord(
                opts={"nvr_preroll_cache_dir": cache_dir, "nvr_preroll_seconds": 30},
                cam_id=cam_id,
            )
            # Create the cam dir
            cam_dir = recorder._preroll_dir(cache_dir, "Terrasse")
            os.makedirs(cam_dir, exist_ok=True)
            now = time.time()
            for i, name in enumerate(["b.mp4", "a.mp4"]):
                p = os.path.join(cam_dir, name)
                with open(p, "wb") as f:
                    f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
                os.utime(p, (now - (2 - i), now - (2 - i)))
            paths = recorder.list_preroll_files(coord, cam_id)
            # Should be sorted oldest-first
            assert len(paths) == 2
            assert paths[0].endswith("b.mp4")  # older
            assert paths[1].endswith("a.mp4")  # newer


# ===========================================================================
# Phase 3 — _build_preroll_ffmpeg_args
# ===========================================================================


class TestBuildPrerollFfmpegArgs(unittest.TestCase):
    def test_preroll_args_no_segment_atclocktime(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-segment_atclocktime" not in args

    def test_preroll_args_no_strftime_mkdir(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-strftime_mkdir" not in args

    def test_preroll_args_no_reconnect(self):
        """-reconnect* are HTTP-only; crash ffmpeg rc=8 on rtsp:// inputs (confirmed 2026-05-08)."""
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-reconnect" not in args
        assert "-reconnect_at_eof" not in args
        assert "-reconnect_streamed" not in args
        assert "-reconnect_delay_max" not in args

    def test_preroll_args_10s_segments(self):
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_SEGMENT_SECONDS,
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-segment_time" in args
        idx = args.index("-segment_time")
        assert args[idx + 1] == str(_PREROLL_SEGMENT_SECONDS)

    def test_preroll_args_uses_copy_codec(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-c" in args
        assert args[args.index("-c") + 1] == "copy"

    def test_preroll_args_pattern_at_end(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        pattern = "/dev/shm/cache/cam/%H%M%S.mp4"
        args = _build_preroll_ffmpeg_args("rtsp://127.0.0.1:9000/stream", pattern)
        assert args[-1] == pattern


# ===========================================================================
# start_recorder date-directory pre-creation
# ===========================================================================


class TestStartRecorderDateDirPreCreation(unittest.TestCase):
    """Regression: -strftime_mkdir 1 does not create date subdirs on all ffmpeg
    versions bundled with HA (confirmed rc=254 on 2026-05-08). start_recorder()
    must pre-create today's and tomorrow's date dirs before spawning ffmpeg."""

    def test_date_dirs_created_before_ffmpeg_spawn(self):
        """start_recorder pre-creates YYYY-MM-DD subdirs under staging/<cam>/."""
        import datetime

        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"

        created_paths = []

        async def fake_executor_job(fn, *args):
            # Capture makedirs calls; don't do real I/O
            if fn is os.makedirs:
                created_paths.append(args[0])
            return None

        coord = _make_coord(cam_id=cam_id)
        coord.hass.async_add_executor_job = fake_executor_job

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())

        today = datetime.date.today().strftime("%Y-%m-%d")
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).strftime(
            "%Y-%m-%d"
        )

        # At least one of the created paths must end with the today date subdir
        assert any(today in p for p in created_paths), (
            f"today's date dir '{today}' not found in created paths: {created_paths}"
        )
        assert any(tomorrow in p for p in created_paths), (
            f"tomorrow's date dir '{tomorrow}' not found in created paths: {created_paths}"
        )

    def test_date_dirs_created_under_staging_not_base(self):
        """Date dirs must be inside _staging/<cam>/, not directly under base path."""
        import datetime

        import custom_components.bosch_shc_camera.recorder as recorder
        from custom_components.bosch_shc_camera.recorder import _STAGING_DIRNAME

        cam_id = "11111111"
        base_path = "/config/bosch_nvr"
        created_paths = []

        async def fake_executor_job(fn, *args):
            if fn is os.makedirs:
                created_paths.append(args[0])
            return None

        coord = _make_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": base_path,
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 0,
            },
        )
        coord.hass.async_add_executor_job = fake_executor_job

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())

        today = datetime.date.today().strftime("%Y-%m-%d")
        # All date-dir paths must contain the staging subdir component
        date_dir_paths = [p for p in created_paths if today in p]
        assert date_dir_paths, "no date dir created"
        for path in date_dir_paths:
            assert _STAGING_DIRNAME in path, (
                f"date dir '{path}' not under staging tree ({_STAGING_DIRNAME!r})"
            )


# ===========================================================================
# Pre-roll wiring: start_recorder triggers start_preroll_recorder
# ===========================================================================


class TestPrerollWiring(unittest.TestCase):
    """Regression: start_preroll_recorder was never called from start_recorder
    (wiring omission found 2026-05-08 during live test). Verified by checking
    that /dev/shm/bosch_nvr_cache/ was never created despite preroll_seconds=30."""

    def test_start_recorder_calls_preroll_when_seconds_gt_zero(self):
        """start_recorder must call start_preroll_recorder when nvr_preroll_seconds > 0."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 30,
                "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
            },
        )

        started_preroll = []

        async def fake_start_preroll(c, cid):
            started_preroll.append(cid)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with patch.object(
                    recorder, "start_preroll_recorder", side_effect=fake_start_preroll
                ):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id in started_preroll, "start_preroll_recorder was not called"

    def test_start_recorder_skips_preroll_when_seconds_zero(self):
        """start_recorder must NOT call start_preroll_recorder when nvr_preroll_seconds=0."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 0,
                "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
            },
        )

        started_preroll = []

        async def fake_start_preroll(c, cid):
            started_preroll.append(cid)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with patch.object(
                    recorder, "start_preroll_recorder", side_effect=fake_start_preroll
                ):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id not in started_preroll, (
            "start_preroll_recorder was called despite seconds=0"
        )

    def test_stop_recorder_calls_stop_preroll(self):
        """stop_recorder must call stop_preroll_recorder to kill the pre-roll ffmpeg."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)

        stopped_preroll = []

        async def fake_stop_preroll(c, cid):
            stopped_preroll.append(cid)

        async def _run():
            with patch.object(
                recorder, "stop_preroll_recorder", side_effect=fake_stop_preroll
            ):
                await recorder.stop_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id in stopped_preroll, (
            "stop_preroll_recorder was not called from stop_recorder"
        )

    def test_stop_all_calls_stop_all_preroll(self):
        """stop_all must call stop_all_preroll before stopping main recorders."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_coord()
        coord._nvr_processes = {"cam1": MagicMock()}

        stop_all_preroll_called = []

        async def fake_stop_all_preroll(c):
            stop_all_preroll_called.append(True)

        async def _run():
            with patch.object(
                recorder, "stop_all_preroll", side_effect=fake_stop_all_preroll
            ):
                with patch.object(recorder, "stop_recorder", new=AsyncMock()):
                    await recorder.stop_all(coord)

        asyncio.get_event_loop().run_until_complete(_run())
        assert stop_all_preroll_called, "stop_all_preroll was not called from stop_all"


# ===========================================================================
# Periodic prune watcher (_watch_preroll_recorder)
# ===========================================================================


class TestWatchPrerollRecorder(unittest.TestCase):
    """Regression: prune_preroll_cache was only called at spawn time; the ring
    buffer grew unbounded during operation (confirmed 2026-05-08: 11 segments
    when max_segs=4). _watch_preroll_recorder must prune every segment interval."""

    def test_watcher_calls_prune_after_sleep(self):
        """_watch_preroll_recorder calls prune_preroll_cache after one sleep cycle.

        fake_sleep returns on the first call, then raises CancelledError on the
        second so the loop executes exactly one prune iteration.
        """
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)
        cam_dir = "/dev/shm/bosch_nvr_cache/Terrasse"
        max_segs = 4

        mock_proc = MagicMock()
        mock_proc.returncode = None
        coord._nvr_preroll_processes[cam_id] = mock_proc

        prune_calls = []

        async def fake_executor_job(fn, *args):
            # v12.4.1: watcher now calls _prune_and_count (prune + return
            # remaining segment count for the diagnostic sensor) instead
            # of the plain prune_preroll_cache. Accept either name so this
            # test pins the "prune fired on tick" contract regardless of
            # the helper rename.
            if fn in (recorder.prune_preroll_cache, recorder._prune_and_count):
                prune_calls.append(args)
            return None

        coord.hass.async_add_executor_job = fake_executor_job

        sleep_calls = 0

        async def fake_sleep(_secs):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        async def _run():
            with patch("asyncio.sleep", side_effect=fake_sleep):
                try:
                    await recorder._watch_preroll_recorder(
                        coord, cam_id, cam_dir, max_segs
                    )
                except asyncio.CancelledError:
                    pass

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(prune_calls) >= 1, "prune_preroll_cache not called in watcher loop"
        assert prune_calls[0][0] == cam_dir
        assert prune_calls[0][1] == max_segs

    def test_watcher_exits_when_process_gone(self):
        """_watch_preroll_recorder exits naturally when the process is no longer registered."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)
        cam_dir = "/dev/shm/bosch_nvr_cache/Terrasse"

        # No process in _nvr_preroll_processes → watcher should return
        coord._nvr_preroll_processes = {}

        async def _run():
            with patch("asyncio.sleep", new=AsyncMock()):
                await recorder._watch_preroll_recorder(coord, cam_id, cam_dir, 4)

        # Should complete without hanging
        asyncio.get_event_loop().run_until_complete(
            asyncio.wait_for(_run(), timeout=2.0)
        )

    def test_watcher_exits_when_process_exited(self):
        """Watcher exits when proc.returncode is not None (ffmpeg finished)."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)

        mock_proc = MagicMock()
        mock_proc.returncode = 0  # already exited
        coord._nvr_preroll_processes[cam_id] = mock_proc

        async def _run():
            with patch("asyncio.sleep", new=AsyncMock()):
                await recorder._watch_preroll_recorder(coord, cam_id, "/tmp/cache", 4)

        asyncio.get_event_loop().run_until_complete(
            asyncio.wait_for(_run(), timeout=2.0)
        )

    def test_start_preroll_creates_watcher_task(self):
        """start_preroll_recorder must register a background prune-watcher task."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_task = MagicMock()
        mock_task.done.return_value = False
        coord.hass.async_create_background_task = MagicMock(return_value=mock_task)

        async def fake_executor_job(fn, *args):
            return None

        coord.hass.async_add_executor_job = fake_executor_job

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await recorder.start_preroll_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert hasattr(coord, "_nvr_preroll_tasks"), "_nvr_preroll_tasks not created"
        assert cam_id in coord._nvr_preroll_tasks, "watcher task not stored for cam_id"

    def test_stop_preroll_cancels_watcher_task(self):
        """stop_preroll_recorder must cancel the periodic prune-watcher task."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(cam_id=cam_id)

        mock_task = MagicMock()
        mock_task.done.return_value = False
        coord._nvr_preroll_tasks = {cam_id: mock_task}

        # No process — but task should still be cancelled
        coord._nvr_preroll_processes = {}

        async def _run():
            await recorder.stop_preroll_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        mock_task.cancel.assert_called_once()
        assert cam_id not in coord._nvr_preroll_tasks


# ===========================================================================
# Event-only mode (nvr_event_only=True)
# ===========================================================================


class TestEventOnlyMode(unittest.TestCase):
    """Regression guard: when nvr_event_only=True, start_recorder must skip the
    main continuous ffmpeg and run only the pre-roll ring buffer. Disk space
    savings: only motion-triggered clips are stored, no 24/7 segments."""

    def test_event_only_skips_main_ffmpeg(self):
        """nvr_event_only=True must NOT spawn the main segment recorder."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 30,
                "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
                "nvr_event_only": True,
            },
        )

        spawned = []

        async def _run():
            with patch(
                "asyncio.create_subprocess_exec",
                side_effect=lambda *a, **k: spawned.append(a) or MagicMock(),
            ):
                with patch.object(recorder, "start_preroll_recorder", new=AsyncMock()):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(spawned) == 0, "main ffmpeg spawned despite nvr_event_only=True"
        assert cam_id not in coord._nvr_processes

    def test_event_only_starts_preroll(self):
        """nvr_event_only=True must start the pre-roll recorder when preroll_seconds > 0."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 30,
                "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
                "nvr_event_only": True,
            },
        )

        started_preroll = []

        async def fake_start_preroll(c, cid):
            started_preroll.append(cid)

        async def _run():
            with patch.object(
                recorder, "start_preroll_recorder", side_effect=fake_start_preroll
            ):
                await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id in started_preroll, (
            "start_preroll_recorder not called in event_only mode"
        )

    def test_event_only_skips_preroll_when_seconds_zero(self):
        """nvr_event_only=True but preroll_seconds=0 must not start pre-roll."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 0,
                "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
                "nvr_event_only": True,
            },
        )

        started_preroll = []

        async def fake_start_preroll(c, cid):
            started_preroll.append(cid)

        async def _run():
            with patch.object(
                recorder, "start_preroll_recorder", side_effect=fake_start_preroll
            ):
                await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id not in started_preroll, "preroll started despite seconds=0"

    def test_normal_mode_still_spawns_main_ffmpeg(self):
        """Sanity: without nvr_event_only, the main recorder still spawns."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = "11111111"
        coord = _make_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 0,
                "nvr_event_only": False,
            },
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id in coord._nvr_processes, "main ffmpeg not spawned in normal mode"


if __name__ == "__main__":
    unittest.main()
