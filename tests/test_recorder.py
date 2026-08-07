"""Tests for recorder.py — Mini-NVR (continuous + event-driven local recording).

Consolidated single flat test module (platinum convention: one
tests/test_<module>.py per source module) covering every test surface of
``custom_components/bosch_shc_camera/recorder.py``:

  1. ``should_record`` (LAN-only gate)
     — must return True iff switch ON ∧ conn_type=LOCAL ∧ camera ONLINE.
     This is the hard line from concept §2 — no cloud-relay recording.

  2. ``_build_ffmpeg_args`` / ``_apply_quality``
     — pinned wire format. ``-c copy`` (never transcode), ``-segment_time 300``,
     ``-segment_atclocktime 1`` (wall-aligned), ``-strftime 1`` + ``-strftime_mkdir 1``
     (date-folder created by ffmpeg), ``-movflags +faststart`` (segment is
     web-playable mid-write — critical for the timeline UI). Quality switch
     rewrites ``inst=`` for the low-bandwidth LOCAL-only mode.

  3. Pre-roll ring buffer helpers (``_preroll_dir``/``_list_preroll_segments``/
     ``prune_preroll_cache``/``_build_preroll_ffmpeg_args``) and the
     motion-clip concatenator (``create_motion_clip``/``create_motion_clip_args``).

  4. ``_segment_pattern`` / ``_segment_dir``
     — sanitized camera name via ``_safe_name`` (path-traversal guard) +
     ``YYYY-MM-DD/HH-MM.mp4`` layout.

  5. ``sync_nvr_cleanup`` retention purge
     — only files older than the cutoff are removed; never directories at the
     base path. Covered both with a mocked filesystem and with real tmp_path
     files.

  6. Recorder lifecycle — ``start_recorder``/``stop_recorder``/``stop_all``,
     the pre-roll counterparts, and the ``_watch_recorder`` /
     ``_watch_preroll_recorder`` background watchers (respawn-on-crash,
     crash-loop give-up, disk-full detection, SIGTERM→SIGKILL escalation).

  7. ``BoschNvrRecordingSwitch.async_turn_on`` / ``async_turn_off``
     — delegate to ``coordinator.start_recorder`` / ``stop_recorder``.

User/forum source: project-internal Mini-NVR implementation (continuous
recording MVP, quality switch, pre-roll ring buffer, motion-clip concat,
event-only mode). The LAN-only gate is the central design decision
documented in `docs/mini-nvr-concept.md` §2.

SENTINEL_RULE: every `time.monotonic()` default in this file uses
`float('-inf')`, not `0.0`, so the assertions hold on fresh CI VMs
(~200s monotonic uptime).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import recorder
from tests.source_match import assert_in_source

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_TITLE = "Terrasse"

# Some legacy tests (originally test_nvr_phases34.py /
# test_recorder_motion_clip_timeout.py) used a short-form camera id and a
# MagicMock-based coordinator stub shape. Kept distinct from CAM_ID/
# _make_lifecycle_coord (below) rather than force-merged, since the two
# stub shapes have different default field values and merging them would
# risk silently changing what each test actually exercises.
CAM_ID_SHORT = "11111111"


def _make_gate_coord(
    *, conn_type: str = "LOCAL", online: bool = True
) -> SimpleNamespace:
    """Minimal coordinator stub with the three fields ``should_record`` reads."""
    return SimpleNamespace(
        live_connections={CAM_ID: {"_connection_type": conn_type}},
        is_camera_online=lambda cid: online,
    )


class TestShouldRecord:
    """All eight combinations of (switch, conn_type, online) — only one yields True."""

    def test_all_three_true_returns_true(self):
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = _make_gate_coord(conn_type="LOCAL", online=True)
        assert should_record(coord, CAM_ID, switch_on=True) is True

    def test_switch_off_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = _make_gate_coord(conn_type="LOCAL", online=True)
        assert should_record(coord, CAM_ID, switch_on=False) is False

    def test_remote_connection_returns_false(self):
        """LAN-only is a hard line — no fallback to cloud relay (concept §2)."""
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = _make_gate_coord(conn_type="REMOTE", online=True)
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_camera_offline_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = _make_gate_coord(conn_type="LOCAL", online=False)
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_no_live_connection_returns_false(self):
        """Unknown cam_id (not in `live_connections`) → not LOCAL → False."""
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = SimpleNamespace(
            live_connections={},
            is_camera_online=lambda cid: True,
        )
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_unknown_connection_type_returns_false(self):
        """A connection_type the gate doesn't know about must NOT enable
        recording — fail-closed."""
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = _make_gate_coord(conn_type="WHATEVER", online=True)
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_remote_and_offline_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = _make_gate_coord(conn_type="REMOTE", online=False)
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_switch_off_and_remote_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = _make_gate_coord(conn_type="REMOTE", online=True)
        assert should_record(coord, CAM_ID, switch_on=False) is False


class TestBuildFfmpegArgs:
    """The exact ffmpeg argv is the contract surface against ffmpeg upstream
    behavior; pinning it catches accidental regressions like dropping
    ``-segment_atclocktime 1`` (segments would no longer wall-align)."""

    def test_argv_starts_with_ffmpeg_binary(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%Y-%m-%d/%H-%M.mp4")
        assert args[0] == "ffmpeg"

    def test_uses_c_copy_no_transcode(self):
        """Concept §3.2 / §9 — `-c copy` is non-negotiable. Re-encoding on a Pi
        would be lossy and burn CPU; Bosch already encodes 1080p H.264."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%Y-%m-%d/%H-%M.mp4")
        # `-c copy` must appear as a contiguous pair somewhere in argv
        for i in range(len(args) - 1):
            if args[i] == "-c" and args[i + 1] == "copy":
                return
        pytest.fail(f"-c copy missing from argv: {args}")

    def test_uses_rtsp_transport_tcp(self):
        """The TLS proxy uses TCP-interleaved RTSP; UDP-RTP through the loopback
        proxy is fragile. Force TCP."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%Y-%m-%d/%H-%M.mp4")
        idx = args.index("-rtsp_transport")
        assert args[idx + 1] == "tcp"

    def test_default_segment_time_is_300(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%Y-%m-%d/%H-%M.mp4")
        idx = args.index("-segment_time")
        assert args[idx + 1] == "300"

    def test_segment_time_override(self):
        """Caller can override segment length (used in tests / future shorter
        segments). Pinned so the kwarg name doesn't drift."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args(
            "rtsp://x/y",
            "/tmp/out/%H-%M.mp4",
            segment_seconds=60,
        )
        idx = args.index("-segment_time")
        assert args[idx + 1] == "60"

    def test_segment_atclocktime_enabled(self):
        """Wall-clock alignment — concept §3.2: `show me 14:35` doesn't fall
        mid-segment."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-segment_atclocktime")
        assert args[idx + 1] == "1"

    def test_segment_format_mp4(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-segment_format")
        assert args[idx + 1] == "mp4"

    def test_strftime_enabled(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-strftime")
        assert args[idx + 1] == "1"

    def test_strftime_mkdir_enabled(self):
        """ffmpeg auto-creates the per-day folder from the strftime path —
        otherwise the recorder would 404 on the first segment of every day."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-strftime_mkdir")
        assert args[idx + 1] == "1"

    def test_movflags_faststart(self):
        """`+faststart` lets the segment be browser-playable while still being
        written — required for the timeline UI's "play latest" affordance."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-movflags")
        assert args[idx + 1] == "+faststart"

    def test_input_url_is_passed_with_dash_i(self):
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        url = "rtsp://user:pass@127.0.0.1:34567/?inst=4"
        args = _build_ffmpeg_args(url, "/tmp/out/%H-%M.mp4")
        idx = args.index("-i")
        assert args[idx + 1] == url

    def test_output_pattern_is_last_arg(self):
        """ffmpeg expects the output spec at the end of argv after `-f segment`."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        pattern = "/tmp/out/%Y-%m-%d/%H-%M.mp4"
        args = _build_ffmpeg_args("rtsp://x/y", pattern)
        assert args[-1] == pattern

    def test_includes_all_streams_with_map_0(self):
        """Concept §10 decision 3: include audio in MVP. `-map 0` selects all
        streams from the input, which keeps both video and AAC audio."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-map")
        assert args[idx + 1] == "0"

    def test_reset_timestamps_enabled(self):
        """Each segment must start at PTS 0 — otherwise mp4 duration math is
        wrong and HA's media player can mis-render the seekbar."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-reset_timestamps")
        assert args[idx + 1] == "1"

    def test_reconnect_not_present(self):
        """-reconnect is an HTTP-only flag that crashes ffmpeg on rtsp:// inputs
        (rc=8, 'Option reconnect not found'). Confirmed on live HA 2026-05-08.
        Watcher (_watch_recorder) handles respawn instead."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args

        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        assert "-reconnect" not in args


class TestQualitySwitch(unittest.TestCase):
    """`_apply_quality` rewrites `inst=` in the RTSP URL for the low-bandwidth
    LOCAL-only recording mode; `_build_ffmpeg_args` wires the `quality` kwarg
    through to it."""

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


class TestSegmentPattern:
    def test_basic_layout(self):
        """`{base}/{cam}/%Y-%m-%d/%H-%M.mp4` — pinned per concept §4.1."""
        from custom_components.bosch_shc_camera.recorder import _segment_pattern

        result = _segment_pattern("/config/bosch_nvr", "Terrasse")
        # Must include the camera name as a path component
        assert "Terrasse" in result
        # Date strftime token, in folder form
        assert "%Y-%m-%d" in result
        # Time strftime token, as filename without seconds
        assert result.endswith("%H-%M.mp4")

    def test_camera_name_sanitized_via_safe_name(self):
        """Camera names are user-controlled (Bosch app title); a `../` traversal
        attempt must be defanged via `_safe_name`."""
        from custom_components.bosch_shc_camera.recorder import _segment_pattern

        result = _segment_pattern("/config/bosch_nvr", "../../etc/passwd")
        # `..` must not survive as an actual path traversal segment
        # (i.e. no `/../` in the rendered pattern other than legit base path)
        # Easiest assert: the rendered path must START with the base path and
        # not contain `..` at all.
        assert result.startswith("/config/bosch_nvr/")
        # `_safe_name` replaces `..` with `_` and `/` with `_`, so the
        # would-be traversal collapses into one safe path component.
        head_after_base = result[len("/config/bosch_nvr/") :]
        # The first path component (the cam-name slot) must NOT contain `..`
        cam_component = head_after_base.split("/", 1)[0]
        assert ".." not in cam_component
        assert "/" not in cam_component  # already covered by split — sanity

    def test_camera_name_with_spaces_preserved(self):
        """`_safe_name` keeps spaces (per smb tests). User-readable folders."""
        from custom_components.bosch_shc_camera.recorder import _segment_pattern

        result = _segment_pattern("/config/bosch_nvr", "Bosch Eingang")
        assert "Bosch Eingang" in result

    def test_segment_dir_is_prefix_of_pattern(self):
        """`_segment_dir(b, c)` must be a strict prefix of `_segment_pattern(b, c)`."""
        from custom_components.bosch_shc_camera.recorder import (
            _segment_dir,
            _segment_pattern,
        )

        sd = _segment_dir("/config/bosch_nvr", "Terrasse")
        sp = _segment_pattern("/config/bosch_nvr", "Terrasse")
        assert sp.startswith(sd + "/") or sp.startswith(sd + os.sep)

    def test_pattern_renders_valid_path_via_strftime(self):
        """Spot-check: ``time.strftime`` over the pattern must produce a
        sensible YYYY-MM-DD/HH-MM.mp4 path."""
        from custom_components.bosch_shc_camera.recorder import _segment_pattern

        pattern = _segment_pattern("/config/bosch_nvr", "Terrasse")
        # Pin to a fixed timestamp: 2026-05-06 14:35:00 UTC.
        # Use ``time.gmtime`` so the test result is timezone-independent.
        rendered = time.strftime(pattern, time.gmtime(1778078100))
        assert rendered.endswith("/2026-05-06/14-35.mp4")


class TestPrerollDirAndPattern(unittest.TestCase):
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


class TestListPrerollSegments:
    """Pin _list_preroll_segments behavior under several failure modes."""

    def test_listdir_oserror_returns_empty(self, tmp_path: Path):
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

    def test_undersized_segments_filtered(self, tmp_path: Path):
        """Segment smaller than `_PREROLL_MIN_SIZE_BYTES` is dropped.

        Pin: half-flushed corrupt segments must never reach the motion-clip
        concatenator — ffmpeg would otherwise fail mid-stream with an
        invalid-moov error.
        """
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            _list_preroll_segments,
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

    def test_missing_directory_returns_empty(self, tmp_path: Path):
        """Pin: non-existent cam_dir returns [] (idempotent first call)."""
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        result = _list_preroll_segments(str(tmp_path / "does-not-exist"))
        assert result == []

    def test_directory_entry_skipped(self, tmp_path: Path):
        """A subdirectory inside cam_dir must be skipped (non-file entry)."""
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

    def test_stat_oserror_is_skipped(self, tmp_path: Path):
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

    def test_stat_race_between_listdir_and_stat_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """A file that vanishes between `os.listdir` and `os.stat` must be
        skipped silently (race-condition tolerance)."""
        from custom_components.bosch_shc_camera import recorder

        good = tmp_path / "000100.mp4"
        bad = tmp_path / "000200.mp4"
        # Both files must be ≥ _PREROLL_MIN_SIZE_BYTES so the size filter doesn't
        # swallow them before reaching the OSError branch.
        payload = b"x" * (recorder._PREROLL_MIN_SIZE_BYTES + 1024)
        good.write_bytes(payload)
        bad.write_bytes(payload)

        real_stat = os.stat

        def _stat(path, *a, **kw):
            # Simulate the bad file being unlinked between listdir + stat.
            if isinstance(path, (str, os.PathLike)) and str(path).endswith(
                "000200.mp4"
            ):
                raise OSError("simulated race — file vanished")
            return real_stat(path, *a, **kw)

        # `os.path.isfile` ALSO calls `os.stat` internally — patching the bare
        # stat raises in isfile too, so the loop body never reaches the explicit
        # `st = os.stat(full)` line we want to cover. Force isfile to True so
        # control reaches the real branch under test.
        monkeypatch.setattr(recorder.os.path, "isfile", lambda _p: True)
        monkeypatch.setattr(recorder.os, "stat", _stat)

        result = recorder._list_preroll_segments(str(tmp_path))
        paths = [p for p, _mt in result]
        assert any(p.endswith("000100.mp4") for p in paths)
        assert not any(p.endswith("000200.mp4") for p in paths)

    def test_returns_empty_when_dir_missing(self, tmp_path: Path):
        """Calling with a nonexistent path returns [] without raising."""
        from custom_components.bosch_shc_camera import recorder

        result = recorder._list_preroll_segments(str(tmp_path / "no_such_dir"))
        assert result == []

    def test_returns_empty_on_listdir_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        """`OSError` from `os.listdir` (e.g. EACCES) is swallowed — return []."""
        from custom_components.bosch_shc_camera import recorder

        def _bad_listdir(_p):
            raise OSError("EACCES")

        monkeypatch.setattr(recorder.os, "listdir", _bad_listdir)
        result = recorder._list_preroll_segments(str(tmp_path))
        assert result == []

    def test_empty_dir_returns_empty(self):
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        with tempfile.TemporaryDirectory() as d:
            result = _list_preroll_segments(d)
            assert result == []

    def test_sorted_oldest_first(self):
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

    def test_skips_small_files(self):
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        with tempfile.TemporaryDirectory() as d:
            # Write a file below the minimum size
            tiny = os.path.join(d, "tiny.mp4")
            with open(tiny, "wb") as f:
                f.write(b"x" * 512)
            result = _list_preroll_segments(d)
            assert result == []


class TestPrunePrerollCache:
    """Pin prune_preroll_cache deletes oldest, keeps the newest `max`."""

    def test_keeps_three_newest_of_seven(self, tmp_path: Path):
        """7 segments, max=3 → 4 deleted, 3 kept; the 3 newest by mtime.

        Pin: ring buffer must be bounded by mtime so wall-clock skews
        don't keep stale files alive. Critical defense against the
        /dev/shm cache growing without limit.
        """
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            prune_preroll_cache,
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

    def test_under_max_keeps_everything(self, tmp_path: Path):
        """When count ≤ max_segments, no deletes."""
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            prune_preroll_cache,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        for i in range(2):
            f = cam_dir / f"{i:06d}.mp4"
            f.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 1))

        deleted = prune_preroll_cache(str(cam_dir), max_segments=5)
        assert deleted == 0

    def test_unlink_oserror_continues(self, tmp_path: Path):
        """If unlink raises mid-prune, the loop continues — file might
        have already vanished due to a parallel sweep."""
        from custom_components.bosch_shc_camera.recorder import (
            _PREROLL_MIN_SIZE_BYTES,
            prune_preroll_cache,
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

    def test_keeps_max_segments(self):
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

    def test_deletes_oldest_by_name(self):
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

    def test_no_op_when_under_limit(self):
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


class TestBuildPrerollFfmpegArgs(unittest.TestCase):
    def test_no_segment_atclocktime(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-segment_atclocktime" not in args

    def test_no_strftime_mkdir(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-strftime_mkdir" not in args

    def test_no_reconnect(self):
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

    def test_10s_segments(self):
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

    def test_uses_copy_codec(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        assert "-c" in args
        assert args[args.index("-c") + 1] == "copy"

    def test_pattern_at_end(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        pattern = "/dev/shm/cache/cam/%H%M%S.mp4"
        args = _build_preroll_ffmpeg_args("rtsp://127.0.0.1:9000/stream", pattern)
        assert args[-1] == pattern

    def test_analyzeduration_and_probesize_bumped(self):
        """GitHub #64: rc=234 'unspecified size' on the ring's own RTSP
        session -- ffmpeg needs a bigger probe window than the 5s/5MB
        default to find SPS/PPS."""
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream", "/tmp/%H%M%S.mp4"
        )
        i_index = args.index("-i")
        assert args[args.index("-analyzeduration") + 1] == "10M"
        assert args.index("-analyzeduration") < i_index
        assert args[args.index("-probesize") + 1] == "10M"
        assert args.index("-probesize") < i_index

    def test_low_quality_rewrites_url(self):
        """GitHub #64 follow-up: the ring previously ignored nvr_quality
        entirely and always requested the full inst=1 stream, unlike the
        continuous recorder."""
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        args = _build_preroll_ffmpeg_args(
            "rtsp://user:pass@127.0.0.1:9000/stream?inst=1",
            "/tmp/%H%M%S.mp4",
            quality="low",
        )
        assert (
            args[args.index("-i") + 1]
            == "rtsp://user:pass@127.0.0.1:9000/stream?inst=4"
        )

    def test_default_quality_is_auto_unchanged_url(self):
        from custom_components.bosch_shc_camera.recorder import (
            _build_preroll_ffmpeg_args,
        )

        url = "rtsp://user:pass@127.0.0.1:9000/stream?inst=1"
        args = _build_preroll_ffmpeg_args(url, "/tmp/%H%M%S.mp4")
        assert args[args.index("-i") + 1] == url


def _make_preroll_coord(tmp_path, *, cam_title: str = CAM_TITLE) -> SimpleNamespace:
    """Stub coordinator with the fields the pre-roll/clip helpers read."""

    async def _run_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": cam_title}, "status": "ONLINE"}},
        options={
            "nvr_preroll_cache_dir": str(tmp_path),
            "nvr_preroll_seconds": 30,
        },
        nvr_preroll_processes={},
        nvr_preroll_segment_counts={},
        nvr_preroll_tasks={},
        bg_tasks=set(),
        # SENTINEL_RULE: monotonic-based "last X" maps default to float('-inf')
        # so any (now - last) >= interval check is True on fresh CI VMs.
        _nvr_last_preroll_prune={CAM_ID: float("-inf")},
        _nvr_preroll_last_crash={},
        _nvr_recorder_locks={},
    )
    coord.hass = SimpleNamespace(
        async_add_executor_job=_run_executor,
        async_create_background_task=lambda c, n=None: MagicMock(),
    )

    def get_nvr_recorder_lock(cid: str) -> asyncio.Lock:
        lock = coord._nvr_recorder_locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            coord._nvr_recorder_locks[cid] = lock
        return lock

    coord.get_nvr_recorder_lock = get_nvr_recorder_lock
    return coord


def _make_phase_coord(opts=None, cam_title="Terrasse", cam_id=CAM_ID_SHORT):
    """MagicMock-hass coordinator stub (legacy `asyncio.get_event_loop()`
    style tests originally in test_nvr_phases34.py)."""
    coord = SimpleNamespace(
        options=opts
        or {
            "nvr_base_path": "/config/bosch_nvr",
            "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
            "nvr_preroll_seconds": 30,
            "nvr_quality": "auto",
        },
        data={cam_id: {"info": {"title": cam_title}}},
        live_connections={
            cam_id: {
                "_connection_type": "LOCAL",
                "rtspsUrl": "rtsp://user:pass@127.0.0.1:9000/rtsp_tunnel?inst=1&enableaudio=1",
            }
        },
        nvr_processes={},
        nvr_preroll_processes={},
        nvr_preroll_segment_counts={},
        _nvr_preroll_last_crash={},
        nvr_preroll_tasks={},
        nvr_error_state={},
        nvr_auth_retry_count={},
        _nvr_recorder_locks={},
        _nvr_preroll_zero_warned=set(),
        hass=MagicMock(),
        async_update_listeners=MagicMock(),
    )
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)
    coord.hass.async_create_background_task = MagicMock(return_value=MagicMock())
    coord.hass.loop = MagicMock()
    coord.bg_tasks = set()

    def get_nvr_recorder_lock(cid: str) -> asyncio.Lock:
        lock = coord._nvr_recorder_locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            coord._nvr_recorder_locks[cid] = lock
        return lock

    coord.get_nvr_recorder_lock = get_nvr_recorder_lock

    # get_nvr_mode: mirrors the REAL coordinator method exactly — per-camera
    # override first (GitHub #43), else fall back to the global nvr_event_only
    # option. Bug-hunt finding (2026-07-11): an earlier version of this stub
    # only mirrored the fallback half, silently ignoring any override a test
    # set via coord._nvr_mode_preference — meaning no test using this factory
    # could ever exercise the per-camera-override-differs-from-global path
    # that is the entire point of the feature.
    coord._nvr_mode_preference = {}
    coord.get_nvr_mode = lambda cid: (
        coord._nvr_mode_preference[cid]
        if coord._nvr_mode_preference.get(cid) in ("continuous", "event_buffered")
        else (
            "event_buffered"
            if coord.options.get("nvr_event_only", False)
            else "continuous"
        )
    )
    return coord


class TestCreateMotionClipArgs:
    """Pin `create_motion_clip_args` ffmpeg argv shape."""

    def test_concat_format(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4", "/tmp/b.mp4"], "/tmp/out.mp4")
        assert "-f" in args
        concat_idx = args.index("-f")
        assert args[concat_idx + 1] == "concat"

    def test_output_path_is_last(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/motion_clip.mp4")
        assert "/tmp/motion_clip.mp4" == args[-1]

    def test_copy_codec(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/out.mp4")
        assert "-c" in args
        c_idx = args.index("-c")
        assert args[c_idx + 1] == "copy"

    def test_faststart(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/out.mp4")
        assert "+faststart" in " ".join(args)

    def test_references_concat_file(self):
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        args = create_motion_clip_args(["/tmp/a.mp4"], "/tmp/out.mp4")
        # The concat list file should be referenced as -i <something>
        assert "-i" in args
        i_idx = args.index("-i")
        assert "concat" in args[i_idx + 1] or "out.mp4" in args[i_idx + 1]

    def test_has_concat_safe_0(self):
        """ffmpeg refuses absolute paths in concat-list files without
        `-safe 0`; dropping that flag would silently break clips on
        every install where `/config/bosch_nvr/...` paths are absolute
        (i.e. all of them)."""
        from custom_components.bosch_shc_camera.recorder import create_motion_clip_args

        argv = create_motion_clip_args(["/tmp/a.mp4", "/tmp/b.mp4"], "/tmp/out.mp4")
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


class TestCreateMotionClip:
    """Pin create_motion_clip happy path + every failure branch."""

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
    async def test_ffmpeg_not_found_returns_false(self, tmp_path: Path):
        """ffmpeg missing on PATH → log error + return False, no crash.

        Pin: a misconfigured host (no ffmpeg) must not break the motion
        pipeline for other cameras; this entry just returns False.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        self._seed_preroll(tmp_path)

        out_path = str(tmp_path / "clip.mp4")
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            result = await recorder.create_motion_clip(coord, CAM_ID, out_path)
        assert result is False

    @pytest.mark.asyncio
    async def test_concat_write_oserror_returns_false(self, tmp_path: Path):
        """`_write_concat` raising OSError (read-only fs) → return False
        before ffmpeg is even spawned.

        Pin: pre-flight write failure bypasses the spawn so we don't
        leak an orphaned ffmpeg trying to read a missing concat file.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
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
    async def test_oserror_on_spawn_returns_false(self, tmp_path: Path):
        """Generic OSError (e.g. EAGAIN fork-limit) during spawn → False."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        self._seed_preroll(tmp_path)

        out_path = str(tmp_path / "clip.mp4")
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("EAGAIN"),
        ):
            result = await recorder.create_motion_clip(coord, CAM_ID, out_path)
        assert result is False

    @pytest.mark.asyncio
    async def test_communicate_timeout_returns_false(self, tmp_path: Path):
        """If ffmpeg's communicate() hangs past the timeout, kill it and
        return False."""
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        # Write two real pre-roll segments so list_preroll_files returns
        # something — it always drops the newest (possibly still being
        # written by the ring), so a single segment alone would yield [].
        cam_name = CAM_TITLE
        cache_dir = str(tmp_path / "cache")
        os.makedirs(cache_dir, exist_ok=True)
        cam_cache = os.path.join(cache_dir, cam_name)
        os.makedirs(cam_cache, exist_ok=True)
        now = time.time()
        for i, name in enumerate(["seg0.mp4", "seg1.mp4"]):
            p = os.path.join(cam_cache, name)
            with open(p, "wb") as f:
                f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
            os.utime(p, (now - (1 - i), now - (1 - i)))

        coord = _make_preroll_coord(tmp_path)
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

    @pytest.mark.asyncio
    async def test_kill_process_lookup_error_swallowed(self, tmp_path: Path):
        """`proc.kill()` raises because ffmpeg already exited between the
        communicate() timeout and the kill; the helper must return False
        without propagating the exception."""
        from custom_components.bosch_shc_camera import recorder

        async def _executor(fn, *args):
            return fn(*args)

        coord = SimpleNamespace(
            hass=SimpleNamespace(async_add_executor_job=_executor),
            _nvr_recorder_locks={},
        )

        def get_nvr_recorder_lock(cid: str) -> asyncio.Lock:
            lock = coord._nvr_recorder_locks.get(cid)
            if lock is None:
                lock = asyncio.Lock()
                coord._nvr_recorder_locks[cid] = lock
            return lock

        coord.get_nvr_recorder_lock = get_nvr_recorder_lock

        # proc.communicate() never resolves naturally — wait_for times out
        # before it does. kill() raises ProcessLookupError (already dead).
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock(side_effect=ProcessLookupError())

        out_path = str(tmp_path / "clip.mp4")
        # GitHub #51: staging hardlinks the listed segment, so it must be a
        # real file — a nonexistent mocked path would yield zero staged
        # segments and short-circuit before ever reaching the ffmpeg spawn
        # this test exercises.
        seg1 = tmp_path / "seg1.mp4"
        seg1.write_bytes(b"x" * 2048)

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(seg1)],
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ),
            patch(
                "asyncio.wait_for",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            ok = await recorder.create_motion_clip(coord, CAM_ID_SHORT, out_path)
        assert ok is False
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_concat_unlink_oserror_swallowed(self, tmp_path: Path):
        """OSError when removing the concat file after a successful clip must
        be swallowed."""
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        cam_name = CAM_TITLE
        cache_dir = str(tmp_path / "cache")
        cam_cache = os.path.join(cache_dir, cam_name)
        os.makedirs(cam_cache, exist_ok=True)
        # Two segments — list_preroll_files always drops the newest.
        now = time.time()
        for i, name in enumerate(["seg0.mp4", "seg1.mp4"]):
            p = os.path.join(cam_cache, name)
            with open(p, "wb") as f:
                f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
            os.utime(p, (now - (1 - i), now - (1 - i)))

        coord = _make_preroll_coord(tmp_path)
        coord.options["nvr_preroll_cache_dir"] = cache_dir
        output = str(tmp_path / "out.mp4")

        proc = MagicMock()
        proc.returncode = 0

        async def _communicate():
            return (b"", b"")

        proc.communicate = _communicate

        async def _spawn(*a, **kw):
            return proc

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

    @pytest.mark.asyncio
    async def test_no_preroll_segments_returns_false(self):
        """list_preroll_files returns [] → create_motion_clip returns False."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

        with patch.object(recorder, "list_preroll_files", return_value=[]):
            result = await recorder.create_motion_clip(coord, cam_id, "/tmp/out.mp4")
        assert result is False

    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        """Mock subprocess → rc=0 → returns True."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with tempfile.TemporaryDirectory() as d:
            # GitHub #51: the new staging step hardlinks every listed
            # segment, so a mocked-but-nonexistent path would silently
            # yield zero staged segments (and a false-negative "False"
            # result for the wrong reason) — segments must be real files.
            seg0 = os.path.join(d, "seg0.mp4")
            seg1 = os.path.join(d, "seg1.mp4")
            for p in (seg0, seg1):
                with open(p, "wb") as f:
                    f.write(b"x" * 2048)

            with (
                patch.object(
                    recorder,
                    "list_preroll_files",
                    return_value=[seg0, seg1],
                ),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            ):
                output = os.path.join(d, "motion.mp4")

                # Patch async_add_executor_job to actually run the function
                async def _exec_job(fn, *args):
                    return fn(*args) if args else fn()

                coord.hass.async_add_executor_job = _exec_job
                result = await recorder.create_motion_clip(coord, cam_id, output)

        assert result is True

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_via_phase_coord(self):
        """FileNotFoundError on spawn → returns False gracefully (using the
        MagicMock-hass coordinator stub shape)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

        with tempfile.TemporaryDirectory() as d:
            seg0 = os.path.join(d, "seg0.mp4")
            with open(seg0, "wb") as f:
                f.write(b"x" * 2048)

            with (
                patch.object(recorder, "list_preroll_files", return_value=[seg0]),
                patch(
                    "asyncio.create_subprocess_exec",
                    side_effect=FileNotFoundError("ffmpeg"),
                ),
            ):
                output = os.path.join(d, "motion.mp4")

                async def _exec_job(fn, *args):
                    return fn(*args) if args else fn()

                coord.hass.async_add_executor_job = _exec_job
                result = await recorder.create_motion_clip(coord, cam_id, output)

        assert result is False

    @pytest.mark.asyncio
    async def test_ffmpeg_rc_nonzero_returns_false(self):
        """ffmpeg exits with rc=1 → returns False."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_proc.returncode = 1

        with tempfile.TemporaryDirectory() as d:
            seg0 = os.path.join(d, "seg0.mp4")
            with open(seg0, "wb") as f:
                f.write(b"x" * 2048)

            with (
                patch.object(recorder, "list_preroll_files", return_value=[seg0]),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            ):
                output = os.path.join(d, "motion.mp4")

                async def _exec_job(fn, *args):
                    return fn(*args) if args else fn()

                coord.hass.async_add_executor_job = _exec_job
                result = await recorder.create_motion_clip(coord, cam_id, output)

        assert result is False


class TestStageSegmentsForConcat:
    """GitHub #51: `_stage_segments_for_concat` hardlinks listed segments
    into a private dir so a concurrent prune/rotate can't yank one out from
    under ffmpeg's concat demuxer after it was already selected."""

    def test_hardlinks_every_segment(self, tmp_path: Path):
        from custom_components.bosch_shc_camera.recorder import (
            _stage_segments_for_concat,
        )

        src_dir = tmp_path / "ring"
        src_dir.mkdir()
        seg0 = src_dir / "000000.mp4"
        seg1 = src_dir / "000001.mp4"
        seg0.write_bytes(b"a" * 2048)
        seg1.write_bytes(b"b" * 2048)

        stage_dir = tmp_path / "_stage" / "event"
        staged = _stage_segments_for_concat([str(seg0), str(seg1)], str(stage_dir))

        assert len(staged) == 2
        for p in staged:
            assert os.path.isfile(p)
        # Real hardlinks (same inode), not copies.
        assert os.stat(staged[0]).st_ino == os.stat(seg0).st_ino
        assert os.stat(staged[1]).st_ino == os.stat(seg1).st_ino

    def test_survives_original_deleted_after_staging(self, tmp_path: Path):
        """The whole point: once staged, deleting the original must NOT
        affect the staged copy's content (tmpfs hardlink semantics)."""
        from custom_components.bosch_shc_camera.recorder import (
            _stage_segments_for_concat,
        )

        src_dir = tmp_path / "ring"
        src_dir.mkdir()
        seg0 = src_dir / "000000.mp4"
        seg0.write_bytes(b"payload" * 100)

        stage_dir = tmp_path / "_stage" / "event"
        staged = _stage_segments_for_concat([str(seg0)], str(stage_dir))
        assert len(staged) == 1

        # Simulate the ring pruning/rotating the original right after staging.
        os.unlink(seg0)

        assert os.path.isfile(staged[0])
        assert Path(staged[0]).read_bytes() == b"payload" * 100

    def test_missing_segment_skipped_not_raised(self, tmp_path: Path):
        """A segment already gone by the time we try to link it (the far
        tighter residual race) is silently skipped — the caller still gets
        the segments that DID survive, instead of an exception aborting the
        whole clip."""
        from custom_components.bosch_shc_camera.recorder import (
            _stage_segments_for_concat,
        )

        src_dir = tmp_path / "ring"
        src_dir.mkdir()
        seg0 = src_dir / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)
        missing = src_dir / "000001.mp4"  # never created

        stage_dir = tmp_path / "_stage" / "event"
        staged = _stage_segments_for_concat([str(seg0), str(missing)], str(stage_dir))

        assert len(staged) == 1
        assert os.path.isfile(staged[0])

    def test_empty_input_returns_empty(self, tmp_path: Path):
        from custom_components.bosch_shc_camera.recorder import (
            _stage_segments_for_concat,
        )

        staged = _stage_segments_for_concat([], str(tmp_path / "_stage" / "event"))
        assert staged == []

    def test_unwritable_stage_dir_returns_empty(self, tmp_path: Path):
        """`os.makedirs` failing (e.g. permission denied) must degrade to
        "nothing staged", not raise into the caller."""
        from custom_components.bosch_shc_camera.recorder import (
            _stage_segments_for_concat,
        )

        seg0 = tmp_path / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)

        with patch("os.makedirs", side_effect=OSError("permission denied")):
            staged = _stage_segments_for_concat(
                [str(seg0)], str(tmp_path / "_stage" / "event")
            )
        assert staged == []


class TestSweepOrphanedStageDirs:
    """GitHub #51 bug-hunt follow-up: `_stage/<clip>` dirs left behind by a
    hard-killed process must eventually be reclaimed, but an in-flight
    concurrent assembly's own fresh stage dir must never be touched."""

    def test_removes_old_orphan(self, tmp_path: Path):
        from custom_components.bosch_shc_camera.recorder import (
            _STAGE_ORPHAN_MAX_AGE_SECONDS,
            _sweep_orphaned_stage_dirs,
        )

        cam_dir = tmp_path / "cam"
        stage = cam_dir / "_stage" / "old-clip"
        stage.mkdir(parents=True)
        (stage / "0000_seg.mp4").write_bytes(b"x")
        old_time = time.time() - _STAGE_ORPHAN_MAX_AGE_SECONDS - 60
        os.utime(stage, (old_time, old_time))

        _sweep_orphaned_stage_dirs(str(cam_dir))

        assert not stage.exists()

    def test_leaves_fresh_stage_dir_untouched(self, tmp_path: Path):
        """A concurrent in-flight assembly's stage dir is only seconds old
        — must survive a sweep triggered by an unrelated ring respawn."""
        from custom_components.bosch_shc_camera.recorder import (
            _sweep_orphaned_stage_dirs,
        )

        cam_dir = tmp_path / "cam"
        stage = cam_dir / "_stage" / "fresh-clip"
        stage.mkdir(parents=True)
        (stage / "0000_seg.mp4").write_bytes(b"x")

        _sweep_orphaned_stage_dirs(str(cam_dir))

        assert stage.exists()
        assert (stage / "0000_seg.mp4").exists()

    def test_no_stage_root_is_a_noop(self, tmp_path: Path):
        from custom_components.bosch_shc_camera.recorder import (
            _sweep_orphaned_stage_dirs,
        )

        cam_dir = tmp_path / "cam"
        cam_dir.mkdir()
        # No _stage/ subdirectory at all — must not raise.
        _sweep_orphaned_stage_dirs(str(cam_dir))

    def test_ignores_non_directory_entries(self, tmp_path: Path):
        """A stray file directly under `_stage/` (not a clip subdirectory)
        must be skipped, not raise."""
        from custom_components.bosch_shc_camera.recorder import (
            _STAGE_ORPHAN_MAX_AGE_SECONDS,
            _sweep_orphaned_stage_dirs,
        )

        cam_dir = tmp_path / "cam"
        stage_root = cam_dir / "_stage"
        stage_root.mkdir(parents=True)
        stray = stage_root / "not_a_dir.txt"
        stray.write_bytes(b"x")
        old_time = time.time() - _STAGE_ORPHAN_MAX_AGE_SECONDS - 60
        os.utime(stray, (old_time, old_time))

        _sweep_orphaned_stage_dirs(str(cam_dir))

        assert stray.exists()

    def test_stat_race_swallowed(self, tmp_path: Path):
        """A directory that vanishes between the `os.path.isdir` check and
        the EXPLICIT `os.stat(path).st_mtime` call right after it (e.g. a
        concurrent cleanup) must be skipped, not raise — this is the exact
        TOCTOU window the function's own try/except guards against.

        Note: `os.path.isdir` itself calls `os.stat` internally and
        swallows any OSError (returning False), so a naive "always raise
        for this path" patch never reaches the code under test — it has to
        let the FIRST (isdir's internal) stat succeed and only fail the
        SECOND (explicit) one.
        """
        from custom_components.bosch_shc_camera import recorder

        cam_dir = tmp_path / "cam"
        stage = cam_dir / "_stage" / "racy-clip"
        stage.mkdir(parents=True)

        real_stat = os.stat
        call_count = 0

        def _flaky_stat(path, *a, **kw):
            nonlocal call_count
            if str(path) == str(stage):
                call_count += 1
                if call_count > 1:  # first call is os.path.isdir's own check
                    raise OSError("no such file or directory (raced away)")
            return real_stat(path, *a, **kw)

        with patch.object(os, "stat", side_effect=_flaky_stat):
            recorder._sweep_orphaned_stage_dirs(str(cam_dir))
        # Must not raise — the racy entry is simply skipped this sweep.


class TestCleanupStageDir:
    """`_cleanup_stage_dir` — best-effort hardlink+dir removal, called from
    both `create_motion_clip`'s `finally` and `_sweep_orphaned_stage_dirs`.
    Previously zero direct test coverage (only exercised indirectly via the
    happy path in staging-race tests)."""

    def test_removes_files_and_dir(self, tmp_path: Path):
        from custom_components.bosch_shc_camera.recorder import _cleanup_stage_dir

        stage = tmp_path / "_stage" / "clip"
        stage.mkdir(parents=True)
        (stage / "0000_seg.mp4").write_bytes(b"x")
        (stage / "0001_seg.mp4").write_bytes(b"y")

        _cleanup_stage_dir(str(stage))

        assert not stage.exists()

    def test_nonexistent_dir_swallowed(self, tmp_path: Path):
        from custom_components.bosch_shc_camera.recorder import _cleanup_stage_dir

        # os.listdir on a nonexistent path raises inside the outer try —
        # must not propagate.
        _cleanup_stage_dir(str(tmp_path / "never-created"))

    def test_per_file_unlink_failure_swallowed_rmdir_still_attempted(
        self, tmp_path: Path
    ):
        """One file failing to unlink must not stop the others from being
        removed, and `os.rmdir` is still attempted afterward (it will fail
        too, on the still-present file, but that failure is ALSO
        swallowed by the outer except)."""
        from custom_components.bosch_shc_camera.recorder import _cleanup_stage_dir

        stage = tmp_path / "_stage" / "clip"
        stage.mkdir(parents=True)
        good = stage / "0000_good.mp4"
        bad = stage / "0001_bad.mp4"
        good.write_bytes(b"x")
        bad.write_bytes(b"y")

        real_unlink = os.unlink

        def _flaky_unlink(path, *a, **kw):
            if str(path) == str(bad):
                raise OSError("permission denied")
            return real_unlink(path, *a, **kw)

        with patch.object(os, "unlink", side_effect=_flaky_unlink):
            _cleanup_stage_dir(str(stage))  # must not raise

        assert not good.exists()
        assert bad.exists()  # the one that failed to unlink is still there


class TestCreateMotionClipStagingRace:
    """GitHub #51 integration-level coverage: the exact race the reporter
    hit — a segment that `list_preroll_files()` selected gets pruned before
    ffmpeg's concat demuxer opens it — must no longer lose the whole clip,
    and the list+stage step must run under the per-camera recorder lock."""

    @pytest.mark.asyncio
    async def test_segment_pruned_between_list_and_open_still_ships_clip(
        self, tmp_path: Path
    ):
        """Simulates the reported mechanism directly: by the time
        `create_motion_clip` gets the segment list, one of the two listed
        files is deleted (as if a concurrent ring prune won the race)
        BEFORE staging runs. The surviving segment must still ship instead
        of the whole clip aborting."""
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg1 = cam_dir / "000001.mp4"
        seg0.write_bytes(b"x" * 2048)
        seg1.write_bytes(b"x" * 2048)

        # list_preroll_files "saw" both segments a moment ago, but seg1 was
        # pruned/rotated out from under us by the time staging runs.
        def _list_with_race(_coord, _cam_id):
            os.unlink(seg1)
            return [str(seg0), str(seg1)]

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(recorder, "list_preroll_files", side_effect=_list_with_race),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            output = str(tmp_path / "motion.mp4")
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        # Old behavior: ffmpeg would be handed seg1's now-missing path and
        # exit non-zero ("Impossible to open"), losing the whole clip. New
        # behavior: only the surviving segment is staged and concatenated.
        assert result is True

    @pytest.mark.asyncio
    async def test_all_listed_segments_vanish_before_staging_cleans_up_stage_dir(
        self, tmp_path: Path
    ):
        """Every listed segment vanishing before staging (not just one) —
        `staged_paths` ends up empty despite `preroll_paths` being
        non-empty, so `stage_dir` was already created. Must still return
        False cleanly AND clean up the now-empty stage dir, not leak it."""
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg1 = cam_dir / "000001.mp4"
        seg0.write_bytes(b"x" * 2048)
        seg1.write_bytes(b"x" * 2048)

        def _list_with_total_race(_coord, _cam_id):
            os.unlink(seg0)
            os.unlink(seg1)
            return [str(seg0), str(seg1)]

        with patch.object(
            recorder, "list_preroll_files", side_effect=_list_with_total_race
        ):
            output = str(tmp_path / "motion.mp4")
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        assert result is False
        assert not (cam_dir / "_stage").exists() or not list(
            (cam_dir / "_stage").rglob("*")
        )

    @pytest.mark.asyncio
    async def test_list_and_stage_runs_under_recorder_lock(self, tmp_path: Path):
        """A concurrent holder of `get_nvr_recorder_lock` (e.g. the prune
        watcher or a ring respawn) must block `create_motion_clip`'s
        list+stage step until it releases — not run concurrently with it."""
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)

        lock = coord.get_nvr_recorder_lock(CAM_ID)
        await lock.acquire()

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(recorder, "list_preroll_files", return_value=[str(seg0)]),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            output = str(tmp_path / "motion.mp4")
            task = asyncio.ensure_future(
                recorder.create_motion_clip(coord, CAM_ID, output)
            )
            await asyncio.sleep(0.05)
            assert not task.done(), (
                "create_motion_clip must block on the recorder lock instead "
                "of proceeding while a concurrent holder has it"
            )
            lock.release()
            result = await task

        assert result is True

    @pytest.mark.asyncio
    async def test_stage_dir_cleaned_up_on_success(self, tmp_path: Path):
        """The `_stage/<clip>` dir created for one event must not linger
        after a successful clip — else it accumulates forever."""
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(recorder, "list_preroll_files", return_value=[str(seg0)]),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            output = str(tmp_path / "motion.mp4")
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        assert result is True
        assert not (cam_dir / "_stage").exists() or not list(
            (cam_dir / "_stage").rglob("*")
        )

    @pytest.mark.asyncio
    async def test_stage_dir_cleaned_up_on_ffmpeg_failure(self, tmp_path: Path):
        """Same cleanup guarantee on the failure path (rc!=0) — the
        `finally` must run regardless of outcome."""
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"boom"))
        mock_proc.returncode = 1

        with (
            patch.object(recorder, "list_preroll_files", return_value=[str(seg0)]),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            output = str(tmp_path / "motion.mp4")
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        assert result is False
        assert not (cam_dir / "_stage").exists() or not list(
            (cam_dir / "_stage").rglob("*")
        )

    @pytest.mark.asyncio
    async def test_concat_txt_cleaned_up_on_spawn_failure(self, tmp_path: Path):
        """Bug-hunt finding: the `.concat.txt` staging file used to leak on
        the spawn-failure path (only cleaned up after a successful
        `communicate()`). Must be removed regardless of outcome."""
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)

        output = str(tmp_path / "motion.mp4")
        with (
            patch.object(recorder, "list_preroll_files", return_value=[str(seg0)]),
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=OSError("EAGAIN"),
            ),
        ):
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        assert result is False
        assert not os.path.exists(output + ".concat.txt")

    @pytest.mark.asyncio
    async def test_concat_txt_cleaned_up_on_timeout(self, tmp_path: Path):
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)

        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock()

        output = str(tmp_path / "motion.mp4")
        with (
            patch.object(recorder, "list_preroll_files", return_value=[str(seg0)]),
            patch("asyncio.create_subprocess_exec", return_value=proc),
        ):
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        assert result is False
        assert not os.path.exists(output + ".concat.txt")

    @pytest.mark.asyncio
    async def test_rc_zero_with_discontinuity_marker_still_ships_but_warns(
        self, tmp_path: Path, caplog
    ):
        """A concat that reports rc=0 but logged a non-monotonic-DTS style
        warning in stderr must still ship (usually still watchable) — just
        surfaced via a WARNING log instead of being silently invisible."""
        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg0 = cam_dir / "000000.mp4"
        seg0.write_bytes(b"x" * 2048)

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"", b"Non-monotonic DTS in output stream")
        )
        mock_proc.returncode = 0

        with (
            patch.object(recorder, "list_preroll_files", return_value=[str(seg0)]),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            caplog.at_level("WARNING"),
        ):
            output = str(tmp_path / "motion.mp4")
            result = await recorder.create_motion_clip(coord, CAM_ID, output)

        assert result is True
        assert any("discontinuity" in rec.message.lower() for rec in caplog.records)


class TestCreateMotionClipExtraSegments:
    """`extra_segments` (post-roll capture, issue #43 follow-up) must be
    appended AFTER the pre-roll segments in the concat list, and the
    function must still work when there are no pre-roll segments at all
    (post-roll-only clip, e.g. nvr_preroll_seconds=0 < nvr_postroll_seconds)."""

    @pytest.mark.asyncio
    async def test_extra_segments_appended_after_preroll(self):
        """Concat file content lists preroll paths first, then extras, in order."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        captured_concat: dict[str, str] = {}
        real_unlink = os.unlink

        def _spy_unlink(path, *a, **kw):
            if str(path).endswith(".concat.txt"):
                with open(path, encoding="utf-8") as f:
                    captured_concat["content"] = f.read()
            return real_unlink(path, *a, **kw)

        with tempfile.TemporaryDirectory() as d:
            # GitHub #51/#54 follow-up: staging hardlinks every listed
            # segment — pre-roll AND extra_segments alike (the post-roll
            # tail is now read straight out of the live ring directory, so
            # it needs the exact same prune-race protection) — so all of
            # these must be real files.
            pre0 = os.path.join(d, "pre0.mp4")
            pre1 = os.path.join(d, "pre1.mp4")
            post0 = os.path.join(d, "post0.mp4")
            for p in (pre0, pre1, post0):
                with open(p, "wb") as f:
                    f.write(b"x" * 2048)

            with (
                patch.object(
                    recorder,
                    "list_preroll_files",
                    return_value=[pre0, pre1],
                ),
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
                patch("os.unlink", side_effect=_spy_unlink),
            ):
                output = os.path.join(d, "motion.mp4")

                async def _exec_job(fn, *args):
                    return fn(*args) if args else fn()

                coord.hass.async_add_executor_job = _exec_job
                result = await recorder.create_motion_clip(
                    coord, cam_id, output, extra_segments=[post0]
                )

        assert result is True
        lines = captured_concat["content"].splitlines()
        # All three entries are the STAGED hardlink paths (not the original
        # names) — GitHub #51's whole point is that the concat demuxer
        # opens stable, private copies, not the originals.
        assert len(lines) == 3
        assert lines[0].endswith("pre0.mp4'") and "/_stage/" in lines[0]
        assert lines[1].endswith("pre1.mp4'") and "/_stage/" in lines[1]
        assert lines[2].endswith("post0.mp4'") and "/_stage/" in lines[2]

    @pytest.mark.asyncio
    async def test_extra_segments_only_no_preroll(self):
        """Zero pre-roll segments + extra_segments present → still assembles
        (post-roll-only clip)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(recorder, "list_preroll_files", return_value=[]),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            tempfile.TemporaryDirectory() as d,
        ):
            output = os.path.join(d, "motion.mp4")
            post0 = os.path.join(d, "post0.mp4")
            with open(post0, "wb") as f:
                f.write(b"x" * 2048)

            async def _exec_job(fn, *args):
                return fn(*args) if args else fn()

            coord.hass.async_add_executor_job = _exec_job
            result = await recorder.create_motion_clip(
                coord, cam_id, output, extra_segments=[post0]
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_no_preroll_no_extra_returns_false(self):
        """Neither pre-roll nor extra_segments → nothing to concat, False."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

        with patch.object(recorder, "list_preroll_files", return_value=[]):
            result = await recorder.create_motion_clip(
                coord, cam_id, "/tmp/out.mp4", extra_segments=None
            )
        assert result is False


class TestListPrerollFiles:
    """`list_preroll_files` always drops the newest segment — the ring
    writer's ffmpeg `-f segment` process keeps exactly one file open at a
    time, so the newest file on disk may still be mid-write with no
    finalized moov atom (issue #43 follow-up: realKim-dotcom's own local
    event→clip patch hit exactly this and had to stop the ring writer
    first before concatenating)."""

    def test_returns_sorted_paths_minus_newest(self):
        import custom_components.bosch_shc_camera.recorder as recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        cam_id = CAM_ID_SHORT
        with tempfile.TemporaryDirectory() as cache_dir:
            coord = _make_phase_coord(
                opts={"nvr_preroll_cache_dir": cache_dir, "nvr_preroll_seconds": 30},
                cam_id=cam_id,
            )
            # Create the cam dir
            cam_dir = recorder._preroll_dir(cache_dir, "Terrasse")
            os.makedirs(cam_dir, exist_ok=True)
            now = time.time()
            for i, name in enumerate(["c.mp4", "b.mp4", "a.mp4"]):
                p = os.path.join(cam_dir, name)
                with open(p, "wb") as f:
                    f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
                os.utime(p, (now - (3 - i), now - (3 - i)))
            paths = recorder.list_preroll_files(coord, cam_id)
            # Sorted oldest-first, newest ("a.mp4" — possibly still being
            # written by the ring writer) excluded.
            assert len(paths) == 2
            assert paths[0].endswith("c.mp4")  # oldest
            assert paths[1].endswith("b.mp4")  # middle
            assert not any(p.endswith("a.mp4") for p in paths)

    def test_single_segment_excluded_returns_empty(self):
        """Only one segment on disk — it's almost certainly the one
        actively being written, so it must NOT be returned (no reliable
        pre-roll yet, not a partial/corrupt clip)."""
        import custom_components.bosch_shc_camera.recorder as recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        cam_id = CAM_ID_SHORT
        with tempfile.TemporaryDirectory() as cache_dir:
            coord = _make_phase_coord(
                opts={"nvr_preroll_cache_dir": cache_dir, "nvr_preroll_seconds": 30},
                cam_id=cam_id,
            )
            cam_dir = recorder._preroll_dir(cache_dir, "Terrasse")
            os.makedirs(cam_dir, exist_ok=True)
            p = os.path.join(cam_dir, "only.mp4")
            with open(p, "wb") as f:
                f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
            paths = recorder.list_preroll_files(coord, cam_id)
            assert paths == []

    def test_no_segments_returns_empty(self):
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        with tempfile.TemporaryDirectory() as cache_dir:
            coord = _make_phase_coord(
                opts={"nvr_preroll_cache_dir": cache_dir, "nvr_preroll_seconds": 30},
                cam_id=cam_id,
            )
            paths = recorder.list_preroll_files(coord, cam_id)
            assert paths == []


class TestNewestPrerollPath:
    """`_newest_preroll_path` — direct unit coverage of the real filesystem
    scan (every other test patches it out with a mock, so the function body
    itself needs its own dedicated coverage)."""

    def test_returns_newest_by_mtime(self):
        import custom_components.bosch_shc_camera.recorder as recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        with tempfile.TemporaryDirectory() as cam_dir:
            now = time.time()
            for i, name in enumerate(["c.mp4", "b.mp4", "a.mp4"]):
                p = os.path.join(cam_dir, name)
                with open(p, "wb") as f:
                    f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)
                os.utime(p, (now - (3 - i), now - (3 - i)))
            result = recorder._newest_preroll_path(cam_dir)
            assert result is not None
            assert result.endswith("a.mp4")

    def test_empty_dir_returns_none(self):
        import custom_components.bosch_shc_camera.recorder as recorder

        with tempfile.TemporaryDirectory() as cam_dir:
            assert recorder._newest_preroll_path(cam_dir) is None

    def test_missing_dir_returns_none(self):
        import custom_components.bosch_shc_camera.recorder as recorder

        assert recorder._newest_preroll_path("/nonexistent/path/xyz") is None


class TestSyncNvrCleanup:
    """`sync_nvr_cleanup` walks the base path, deletes files older than the
    cutoff, then prunes empty per-day folders. Never touches the base path
    itself. All filesystem calls are mocked."""

    def _coord(
        self,
        base_path: str = "/config/bosch_nvr",
        retention_days: int = 3,
        enabled: bool = True,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            options={
                "enable_nvr": enabled,
                "nvr_base_path": base_path,
                "nvr_retention_days": retention_days,
            }
        )

    def test_zero_retention_disables_cleanup(self):
        """Hard guard: retention_days <= 0 must NOT delete anything (would
        otherwise wipe the user's archive on a config-flow off-by-one)."""
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup

        coord = self._coord(retention_days=0)
        with (
            patch("os.path.isdir", return_value=True),
            patch("os.walk") as walk,
            patch("os.remove") as rm,
        ):
            sync_nvr_cleanup(coord)
            walk.assert_not_called()
            rm.assert_not_called()

    def test_missing_base_path_returns_cleanly(self):
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup

        coord = self._coord()
        with patch("os.path.isdir", return_value=False), patch("os.walk") as walk:
            sync_nvr_cleanup(coord)
            walk.assert_not_called()

    def test_only_files_older_than_cutoff_removed(self):
        """Files with mtime < cutoff are deleted; newer ones are kept."""
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup

        coord = self._coord(retention_days=3)
        now = time.time()
        old = now - 10 * 86400  # 10 days old → DELETE
        recent = now - 1 * 86400  # 1 day old → KEEP

        files_walked = [
            ("/config/bosch_nvr/Terrasse/2026-04-26", [], ["10-00.mp4", "10-05.mp4"]),
            ("/config/bosch_nvr/Terrasse/2026-05-05", [], ["14-00.mp4"]),
        ]

        def fake_stat(path):
            mt = old if "2026-04-26" in path else recent
            return SimpleNamespace(st_mtime=mt)

        removed: list[str] = []

        with (
            patch("os.path.isdir", return_value=True),
            patch("os.walk") as walk,
            patch("os.stat", side_effect=fake_stat),
            patch("os.remove", side_effect=removed.append),
            patch("os.listdir", return_value=["x"]),
            patch("os.rmdir"),
        ):
            # First call (delete pass) returns full tree; second call (rmdir
            # pass, topdown=False) also returns full tree — same files but
            # they're already removed by then. Use ``side_effect`` list to
            # serve both.
            walk.side_effect = [iter(files_walked), iter(files_walked)]
            sync_nvr_cleanup(coord)

        # Only the two old files should be removed.
        assert len(removed) == 2
        for p in removed:
            assert "2026-04-26" in p
        for p in removed:
            assert "2026-05-05" not in p

    def test_never_removes_directories_in_first_pass(self):
        """First pass touches only files; directory removal happens in a
        separate `rmdir` pass that respects "directory must be empty"."""
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup

        coord = self._coord(retention_days=3)

        with (
            patch("os.path.isdir", return_value=True),
            patch("os.walk") as walk,
            patch("os.stat") as stat,
            patch("os.remove") as rm,
            patch("os.rmdir") as rmdir,
            patch("os.listdir", return_value=["a"]),
        ):
            walk.side_effect = [iter([]), iter([])]
            sync_nvr_cleanup(coord)
            # No files = no remove + no empty-dir prune (listdir returned non-empty).
            rm.assert_not_called()
            rmdir.assert_not_called()

    def test_base_path_itself_never_pruned(self):
        """Even if the user's base path is empty, ``sync_nvr_cleanup`` must
        NOT rmdir the base path itself — that would break the next start_recorder
        which expects the dir to exist."""
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup

        coord = self._coord(base_path="/config/bosch_nvr", retention_days=3)

        # Walk yields ONLY the base path as an empty dir (no children).
        # Second pass (topdown=False) yields the same.
        empty = [("/config/bosch_nvr", [], [])]

        rmdir_calls: list[str] = []
        with (
            patch("os.path.isdir", return_value=True),
            patch("os.walk") as walk,
            patch("os.listdir", return_value=[]),
            patch("os.rmdir", side_effect=rmdir_calls.append),
        ):
            walk.side_effect = [iter(empty), iter(empty)]
            sync_nvr_cleanup(coord)

        # base_path must NOT be in rmdir_calls — guarded by the
        # `if root == base_path: continue` branch.
        assert "/config/bosch_nvr" not in rmdir_calls


def _make_lifecycle_coord(
    *, conn_type: str = "LOCAL", base_path: str = "/tmp/nvr_test"
):
    """Stub coordinator with the fields recorder.py's lifecycle functions
    (start/stop_recorder, _watch_recorder, sync_nvr_cleanup) touch."""
    proxy_url = "rtsp://user:pass@127.0.0.1:46597/rtsp_tunnel?inst=1"
    coord = SimpleNamespace(
        live_connections={
            CAM_ID: {
                "_connection_type": conn_type,
                "rtspsUrl": proxy_url,
            }
        },
        nvr_processes={},
        nvr_preroll_processes={},
        nvr_preroll_segment_counts={},
        nvr_preroll_tasks={},
        nvr_user_intent={CAM_ID: True},
        nvr_recent_crash={},
        _nvr_preroll_last_crash={},
        nvr_error_state={},
        nvr_auth_retry_count={},
        _nvr_recorder_locks={},
        bg_tasks=set(),
        data={CAM_ID: {"info": {"title": "Terrasse"}, "status": "ONLINE"}},
        options={
            "nvr_base_path": base_path,
            "nvr_retention_days": 3,
            "enable_nvr": True,
        },
        is_camera_online=lambda cid: True,
        async_update_listeners=MagicMock(),
        nvr_shutting_down=False,
    )

    def get_nvr_recorder_lock(cam_id: str) -> asyncio.Lock:
        lock = coord._nvr_recorder_locks.get(cam_id)
        if lock is None:
            lock = asyncio.Lock()
            coord._nvr_recorder_locks[cam_id] = lock
        return lock

    coord.get_nvr_recorder_lock = get_nvr_recorder_lock

    # get_session: real CameraSessionState instances (lazily created, one per
    # cam_id) so stream_ready_event is a real, independent asyncio.Event per
    # camera — matches the production get_or_create_session backing store.
    # A camera that already has a usable rtspsUrl (the common case for this
    # fixture's default) never touches this — start_recorder's fast path
    # checks the URL first and only reads get_session().stream_ready_event
    # when it needs to wait.
    from custom_components.bosch_shc_camera.session_state import (
        get_or_create_session as _get_or_create_session,
    )

    coord._sessions = {}
    coord.get_session = lambda cid: _get_or_create_session(coord._sessions, cid)
    # get_nvr_mode: mirrors the REAL coordinator method exactly — per-camera
    # override first (GitHub #43), else fall back to the global nvr_event_only
    # option. Bug-hunt finding (2026-07-11): an earlier version of this stub
    # only mirrored the fallback half, silently ignoring any override a test
    # set via coord._nvr_mode_preference — meaning no test using this factory
    # could ever exercise the per-camera-override-differs-from-global path
    # that is the entire point of the feature.
    coord._nvr_mode_preference = {}
    coord.get_nvr_mode = lambda cid: (
        coord._nvr_mode_preference[cid]
        if coord._nvr_mode_preference.get(cid) in ("continuous", "event_buffered")
        else (
            "event_buffered"
            if coord.options.get("nvr_event_only", False)
            else "continuous"
        )
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


class TestNvrCleanupRealFiles:
    def test_zero_retention_disables_cleanup(self, tmp_path: Path):
        """retention_days <= 0 → skip entirely. Hard rule: never delete
        all files just because user fat-fingered the option."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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

        coord = _make_lifecycle_coord(base_path="/nonexistent/path/that/does/not/exist")
        # Must not raise
        recorder.sync_nvr_cleanup(coord)

    def test_deletes_files_older_than_cutoff(self, tmp_path: Path):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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

    def test_prunes_empty_date_dirs_but_not_camera_root(self, tmp_path: Path):
        """After deleting files, empty date folders are removed. Camera
        root + base_path itself must NEVER be removed."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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

    def test_keeps_files_at_or_after_cutoff(self, tmp_path: Path):
        """Boundary: file with mtime == cutoff must NOT be deleted
        (condition is `<`, not `<=`)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_retention_days"] = 7

        cam_dir = tmp_path / "Cam" / "2026-04-29"
        cam_dir.mkdir(parents=True)
        edge_file = cam_dir / "12-00.mp4"
        edge_file.write_bytes(b"x")
        cutoff_ts = time.time() - 7 * 86400 + 60  # 7d - 1min ago = within cutoff
        os.utime(edge_file, (cutoff_ts, cutoff_ts))

        recorder.sync_nvr_cleanup(coord)
        assert edge_file.exists()

    def test_unreadable_file_skipped_not_crash(self, tmp_path: Path):
        """File that os.stat fails on (race: file disappeared mid-walk) must
        be silently skipped, not crash the cleanup loop."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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


def _mock_proc(
    returncode: int | None = None,
    *,
    stderr_data: bytes = b"",
    stderr: MagicMock | None = None,
) -> MagicMock:
    """Build a mock asyncio.subprocess.Process for stop/watch tests."""
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
    if stderr is not None:
        proc.stderr = stderr
    elif stderr_data:
        s = MagicMock()
        s.read = AsyncMock(return_value=stderr_data)
        proc.stderr = s
    else:
        proc.stderr = None
    return proc


def _tail_for(proc: MagicMock) -> recorder._StderrTail:
    """Build a `recorder._StderrTail` pre-populated with whatever a live
    `_drain_stderr_live` task would have collected by the time a (mocked)
    process exits — since `_watch_recorder`/`_watch_preroll_health` no
    longer read `proc.stderr` themselves post-exit (GitHub #64 fix), every
    test that constructs its own `proc` double must construct the matching
    tail explicitly instead. Only understands the simple
    `AsyncMock(return_value=...)` shape `_mock_proc` builds — tests that
    exercise `_drain_stderr_live` itself (timeouts/exceptions/large output)
    build a `_StderrTail` directly, they don't use this helper.
    """
    data = b""
    stderr = getattr(proc, "stderr", None)
    if stderr is not None:
        read_fn = getattr(stderr, "read", None)
        ret = getattr(read_fn, "return_value", None)
        if isinstance(ret, bytes):
            data = ret
    return recorder._StderrTail(data=data)


class TestStartRecorder:
    @pytest.mark.asyncio
    async def test_skipped_when_not_local(self, tmp_path: Path):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(conn_type="REMOTE", base_path=str(tmp_path))
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_skipped_when_no_proxy_url(self, tmp_path: Path):
        """rtspsUrl missing or not rtsp:// → skip with warning, no spawn.

        The readiness-wait is now event-based (GitHub #49 redesign): it
        awaits stream_ready_event with a model-derived timeout instead of
        polling. Patch asyncio.wait_for to raise TimeoutError immediately so
        this test still exercises the full "never becomes ready" exhaustion
        path without any real wall-clock delay.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.live_connections[CAM_ID]["rtspsUrl"] = ""

        async def _timeout_immediately(*_a, **_kw):
            raise TimeoutError

        with (
            patch.object(asyncio, "create_subprocess_exec") as spawn,
            patch.object(asyncio, "wait_for", new=_timeout_immediately),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_proxy_url_is_https(self, tmp_path: Path):
        """If only the rtsps:// URL is set (not rewritten through proxy),
        skip — recording over TLS to the camera bypasses our proxy."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.live_connections[CAM_ID]["rtspsUrl"] = "rtsps://camera.lan/x"

        async def _timeout_immediately(*_a, **_kw):
            raise TimeoutError

        with (
            patch.object(asyncio, "create_subprocess_exec") as spawn,
            patch.object(asyncio, "wait_for", new=_timeout_immediately),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_spawns_ffmpeg(self, tmp_path: Path):
        """LOCAL + valid proxy URL → spawn, register process, register watcher."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)
        assert coord.nvr_processes[CAM_ID] is proc
        # Segment dir was created — under the staging tree as of v11.0.4
        # NVR-storage-target refactor (ffmpeg always writes to _staging first).
        assert (tmp_path / "_staging" / "Terrasse").exists()

    @pytest.mark.asyncio
    async def test_happy_path_wires_live_stderr_drain(self, tmp_path: Path):
        """GitHub #64 regression guard: proves the WIRING, not just that
        `_drain_stderr_live` works in isolation. A mutation deleting the
        `_spawn_stderr_drain_task(...)` call at the real `_start_recorder_
        locked` call site (or reverting to the old post-exit-only read)
        left every other test in this file passing — nothing asserted the
        drain is actually attached to the real spawned process. This
        directly patches `_spawn_stderr_drain_task` and asserts it fires
        exactly once, with the process just spawned and this camera's id,
        so deleting/breaking that call site is caught here."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return proc

        with (
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn),
            patch.object(recorder, "_spawn_stderr_drain_task") as drain_spawn,
        ):
            await recorder.start_recorder(coord, CAM_ID)

        drain_spawn.assert_called_once()
        call_coord, call_cam_id, call_proc = drain_spawn.call_args.args[:3]
        assert call_coord is coord
        assert call_cam_id == CAM_ID
        assert call_proc is proc
        assert drain_spawn.call_args.kwargs["name_prefix"] == "bosch_nvr_stderr_drain"

    @pytest.mark.asyncio
    async def test_successful_spawn_clears_stale_error_state(self, tmp_path: Path):
        """Issue #42: nvr_error_state must not stay stuck showing "error"
        forever after a give-up — a fresh successful spawn (manual toggle,
        or the stream-up hook reviving the recorder on the next LOCAL
        session) must clear it."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.nvr_error_state[CAM_ID] = "ffmpeg crashed twice"
        proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord.nvr_error_state

    @pytest.mark.asyncio
    async def test_replaces_existing_process(self, tmp_path: Path):
        """Calling start_recorder while one is already running must stop
        the old before spawning new — required for cred rotation."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        old_proc = _mock_proc(returncode=None)
        coord.nvr_processes[CAM_ID] = old_proc
        new_proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return new_proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)
        # Old got SIGTERM
        old_proc.send_signal.assert_called_once_with(signal.SIGTERM)
        # New is now registered
        assert coord.nvr_processes[CAM_ID] is new_proc

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_fails_silently(self, tmp_path: Path):
        """Missing ffmpeg binary must not crash HA — log error + return."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_oserror_on_spawn_returns(self, tmp_path: Path):
        """Generic OSError (permissions, OOM, fork limit) — log + return."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("EAGAIN"),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_makedirs_failure_aborts_spawn(self, tmp_path: Path):
        """If we can't create the segment dir (read-only fs, no perms),
        skip the spawn — ffmpeg would just fail later anyway."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))

        async def _bad_executor(fn, *args, **kwargs):
            if fn is os.makedirs:
                raise OSError("EROFS")
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _bad_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_torn_down_during_proxy_url_wait_returns_early(self):
        """During the event-based readiness wait, if the connection type
        flips to non-LOCAL (user toggled stream off, or teardown fired —
        which also clears stream_ready_event, see stream_lifecycle.py),
        `start_recorder` must return silently without starting ffmpeg."""
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.session_state import (
            get_or_create_session,
        )

        coord = SimpleNamespace(
            live_connections={
                CAM_ID_SHORT: {"_connection_type": "LOCAL", "rtspsUrl": ""}
            },
            options={},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
            async_update_listeners=MagicMock(),
            _sessions={},
        )
        coord.get_session = lambda cid: get_or_create_session(coord._sessions, cid)
        coord._nvr_recorder_locks = {}
        coord.get_nvr_recorder_lock = lambda cid: coord._nvr_recorder_locks.setdefault(
            cid, asyncio.Lock()
        )

        async def _wait_for_then_teardown(*_a, **_kw):
            # Simulate: by the time the wait resolves (event set or timeout,
            # doesn't matter which), the stream has already been torn down —
            # matches the real teardown path clearing _connection_type.
            coord.live_connections[CAM_ID_SHORT]["_connection_type"] = "REMOTE"

        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch.object(asyncio, "wait_for", new=_wait_for_then_teardown),
        ):
            await recorder.start_recorder(coord, CAM_ID_SHORT)
        # Must have exited via the early `return` — coord.options unmodified
        # and no ffmpeg subprocess was spawned.

    @pytest.mark.asyncio
    async def test_rtsp_url_appears_during_wait_continues(self):
        """If the URL lands (stream_ready_event fires) before the wait
        times out, start_recorder continues past the wait block."""
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.session_state import (
            get_or_create_session,
        )

        coord = SimpleNamespace(
            live_connections={
                CAM_ID_SHORT: {"_connection_type": "LOCAL", "rtspsUrl": ""}
            },
            options={"nvr_event_only": True, "nvr_preroll_seconds": 0},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
            async_update_listeners=MagicMock(),
            _sessions={},
            _nvr_preroll_zero_warned=set(),
        )
        coord.get_session = lambda cid: get_or_create_session(coord._sessions, cid)
        coord._nvr_recorder_locks = {}
        coord.get_nvr_recorder_lock = lambda cid: coord._nvr_recorder_locks.setdefault(
            cid, asyncio.Lock()
        )
        coord.get_nvr_mode = lambda cid: "event_buffered"

        async def _wait_for_then_url_lands(*_a, **_kw):
            # Simulate the event firing: rtspsUrl lands, as
            # live_connection.py would have set it right before .set().
            coord.live_connections[CAM_ID_SHORT]["rtspsUrl"] = (
                "rtsp://127.0.0.1:5000/cam"
            )

        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch.object(asyncio, "wait_for", new=_wait_for_then_url_lands),
        ):
            await recorder.start_recorder(coord, CAM_ID_SHORT)
        # nvr_event_only + preroll_seconds=0 returns immediately past the
        # poll loop without invoking ffmpeg — the test merely verifies the
        # function reached past the URL-landed branch without crashing.

    @pytest.mark.asyncio
    async def test_wait_timeout_matches_slow_model_min_total_wait(self):
        """GitHub #49 regression (realKim-dotcom, 2026-07-15): Gen1 Outdoor
        (OUTDOOR/CAMERA_EYES, min_total_wait=35s) never got its NVR recorder
        started because the readiness wait used a flat 12s window tuned only
        for Gen2 ("~3-10s"). Pin that the computed wait_for timeout is now
        derived from the camera's own model config, not the old flat
        constant — this is the actual root cause, independent of whether
        the event happens to fire in time in any single test run.
        """
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.models import MODELS
        from custom_components.bosch_shc_camera.session_state import (
            get_or_create_session,
        )

        coord = SimpleNamespace(
            live_connections={
                CAM_ID_SHORT: {"_connection_type": "LOCAL", "rtspsUrl": ""}
            },
            options={},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
            async_update_listeners=MagicMock(),
            _sessions={},
            hw_version={CAM_ID_SHORT: "OUTDOOR"},
        )
        coord.get_session = lambda cid: get_or_create_session(coord._sessions, cid)
        coord._nvr_recorder_locks = {}
        coord.get_nvr_recorder_lock = lambda cid: coord._nvr_recorder_locks.setdefault(
            cid, asyncio.Lock()
        )

        captured_timeout = None

        async def _capture_timeout(_coro, *, timeout=None):
            nonlocal captured_timeout
            captured_timeout = timeout
            raise TimeoutError

        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch.object(asyncio, "wait_for", new=_capture_timeout),
        ):
            await recorder.start_recorder(coord, CAM_ID_SHORT)

        assert captured_timeout is not None
        assert captured_timeout >= MODELS["OUTDOOR"].min_total_wait
        # The old flat constant was 12s (24 steps x 500ms) — this model's
        # min_total_wait (35s) must dominate, proving the fix actually
        # changed behavior for exactly the affected model.
        assert captured_timeout >= 35


class TestStartRecorderDateDirPreCreation(unittest.TestCase):
    """Regression: -strftime_mkdir 1 does not create date subdirs on all ffmpeg
    versions bundled with HA (confirmed rc=254 on 2026-05-08). start_recorder()
    must pre-create today's and tomorrow's date dirs before spawning ffmpeg."""

    def test_date_dirs_created_before_ffmpeg_spawn(self):
        """start_recorder pre-creates YYYY-MM-DD subdirs under staging/<cam>/."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT

        created_paths = []

        async def fake_executor_job(fn, *args):
            # Capture makedirs calls; don't do real I/O
            if fn is os.makedirs:
                created_paths.append(args[0])
            return None

        coord = _make_phase_coord(cam_id=cam_id)
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
        import custom_components.bosch_shc_camera.recorder as recorder
        from custom_components.bosch_shc_camera.recorder import _STAGING_DIRNAME

        cam_id = CAM_ID_SHORT
        base_path = "/config/bosch_nvr"
        created_paths = []

        async def fake_executor_job(fn, *args):
            if fn is os.makedirs:
                created_paths.append(args[0])
            return None

        coord = _make_phase_coord(
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


class TestEventOnlyMode(unittest.TestCase):
    """Regression guard: when nvr_event_only=True, start_recorder must skip the
    main continuous ffmpeg and run only the pre-roll ring buffer. Disk space
    savings: only motion-triggered clips are stored, no 24/7 segments."""

    def test_event_only_skips_main_ffmpeg(self):
        """nvr_event_only=True must NOT spawn the main segment recorder."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
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
                with patch.object(
                    recorder, "_spawn_preroll_recorder_locked", new=AsyncMock()
                ):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(spawned) == 0, "main ffmpeg spawned despite nvr_event_only=True"
        assert cam_id not in coord.nvr_processes

    def test_event_only_starts_preroll(self):
        """nvr_event_only=True must start the pre-roll recorder when preroll_seconds > 0."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
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
                recorder,
                "_spawn_preroll_recorder_locked",
                side_effect=fake_start_preroll,
            ):
                await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id in started_preroll, (
            "_spawn_preroll_recorder_locked not called in event_only mode"
        )

    def test_event_only_skips_preroll_when_seconds_zero(self):
        """nvr_event_only=True but preroll_seconds=0 must not start pre-roll."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
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
                recorder,
                "_spawn_preroll_recorder_locked",
                side_effect=fake_start_preroll,
            ):
                await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id not in started_preroll, "preroll started despite seconds=0"

    def test_normal_mode_still_spawns_main_ffmpeg(self):
        """Sanity: without nvr_event_only, the main recorder still spawns."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
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
        assert cam_id in coord.nvr_processes, "main ffmpeg not spawned in normal mode"


class TestPerCameraNvrModeOverrideMixedFleet:
    """GitHub #43 — the actual use case the feature exists for: a mixed fleet
    where one camera needs continuous-while-armed recording (glass-facing,
    PIR can't fire through glass) while another wants the lightweight
    event-buffered pre-roll ring, in the SAME install, diverging from
    whatever the global nvr_event_only default is set to.

    Bug-hunt finding (2026-07-11): the shared test-coordinator factories'
    get_nvr_mode stubs originally ignored per-camera overrides entirely, so
    this exact scenario was completely untested at the recorder-integration
    level despite being the feature's whole reason to exist. Fixed alongside
    this test.
    """

    def test_override_continuous_beats_global_event_only(self):
        """Global nvr_event_only=True (event-buffered default) but CAM_A has
        a 'continuous' override → CAM_A must still spawn the main ffmpeg
        recorder, not just a pre-roll ring."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_a = "AAAAAAAA"
        coord = _make_phase_coord(
            cam_id=cam_a,
            cam_title="Glasfassade",
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 0,
                "nvr_event_only": True,  # global default: event-buffered
            },
        )
        coord._nvr_mode_preference[cam_a] = "continuous"  # per-cam override

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await recorder.start_recorder(coord, cam_a)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_a in coord.nvr_processes, (
            "override='continuous' must spawn the main ffmpeg recorder even "
            "though the global option says event-only"
        )

    def test_override_event_buffered_beats_global_continuous(self):
        """Global nvr_event_only=False (continuous default) but CAM_B has an
        'event_buffered' override → CAM_B must run only the pre-roll ring,
        no main ffmpeg recorder."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_b = "BBBBBBBB"
        coord = _make_phase_coord(
            cam_id=cam_b,
            cam_title="Grundstueck",
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 20,
                "nvr_event_only": False,  # global default: continuous
            },
        )
        coord._nvr_mode_preference[cam_b] = "event_buffered"  # per-cam override

        started_preroll = []

        async def fake_start_preroll(c, cid):
            started_preroll.append(cid)

        async def _run():
            with patch.object(
                recorder,
                "_spawn_preroll_recorder_locked",
                side_effect=fake_start_preroll,
            ):
                await recorder.start_recorder(coord, cam_b)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_b not in coord.nvr_processes, (
            "override='event_buffered' must NOT spawn the main ffmpeg "
            "recorder even though the global option says continuous"
        )
        assert cam_b in started_preroll, (
            "override='event_buffered' must start the pre-roll ring"
        )

    def test_two_cameras_same_coordinator_diverge_independently(self):
        """The real mixed-fleet scenario: ONE coordinator serving both
        cameras, each resolving to a DIFFERENT effective mode at the same
        time — proving the override is genuinely per-camera, not accidentally
        shared/global state."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_glass, cam_premises = "AAAAAAAA", "BBBBBBBB"
        coord = _make_phase_coord(
            cam_id=cam_glass,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 20,
                "nvr_event_only": False,  # global default: continuous
            },
        )
        # Add the second camera to the same coordinator instance.
        coord.data[cam_premises] = {"info": {"title": "Grundstueck"}}
        coord.live_connections[cam_premises] = {
            "_connection_type": "LOCAL",
            "rtspsUrl": "rtsp://user:pass@127.0.0.1:9001/rtsp_tunnel?inst=1",
        }
        # Only the premises camera opts into event-buffered; the glass camera
        # stays on the (here: continuous) global default.
        coord._nvr_mode_preference[cam_premises] = "event_buffered"

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        started_preroll = []

        async def fake_start_preroll(c, cid):
            started_preroll.append(cid)

        async def _run():
            with (
                patch("asyncio.create_subprocess_exec", return_value=mock_proc),
                patch.object(
                    recorder,
                    "_spawn_preroll_recorder_locked",
                    side_effect=fake_start_preroll,
                ),
            ):
                await recorder.start_recorder(coord, cam_glass)
                await recorder.start_recorder(coord, cam_premises)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_glass in coord.nvr_processes, "glass cam must run continuous"
        assert cam_premises not in coord.nvr_processes, (
            "premises cam must NOT run continuous"
        )
        assert cam_premises in started_preroll, "premises cam must run pre-roll"


class TestStopRecorder:
    @pytest.mark.asyncio
    async def test_no_op_when_not_running(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        # No process registered
        await recorder.stop_recorder(coord, CAM_ID)
        # No exception, no state change
        assert CAM_ID not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_already_exited_quick_return(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=0)  # already exited
        coord.nvr_processes[CAM_ID] = proc
        await recorder.stop_recorder(coord, CAM_ID)
        proc.send_signal.assert_not_called()
        assert CAM_ID not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_clean_sigterm_exit(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=None)
        proc.wait = AsyncMock(return_value=0)
        coord.nvr_processes[CAM_ID] = proc
        await recorder.stop_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_not_called()  # didn't escalate
        assert CAM_ID not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_sigkill_escalation_on_timeout(self):
        """If ffmpeg ignores SIGTERM for 5 s, escalate to SIGKILL."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=None)

        # First wait (after SIGTERM): timeout. Second wait (after SIGKILL): success.
        wait_calls = [TimeoutError(), 137]

        async def _wait():
            r = wait_calls.pop(0)
            if isinstance(r, BaseException):
                raise r
            return r

        proc.wait = _wait
        coord.nvr_processes[CAM_ID] = proc

        with patch.object(
            asyncio,
            "wait_for",
            side_effect=[
                TimeoutError(),
                137,
            ],
        ):
            await recorder.stop_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_lookup_error_on_sigterm_safely_returns(self):
        """If the process died between our check and SIGTERM (race), the
        ProcessLookupError must be swallowed."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock(side_effect=ProcessLookupError())
        coord.nvr_processes[CAM_ID] = proc
        # Must not raise
        await recorder.stop_recorder(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_kill_process_lookup_error_swallowed(self):
        """If proc.kill() raises ProcessLookupError after SIGTERM timeout, swallow it."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock()
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        coord.nvr_processes[CAM_ID] = proc

        call_count_ref = [0]

        async def _always_timeout(coro, timeout):
            try:
                coro.close()
            except Exception:
                pass
            call_count_ref[0] += 1
            raise TimeoutError

        with patch("asyncio.wait_for", side_effect=_always_timeout):
            await recorder.stop_recorder(coord, CAM_ID)

        # kill was attempted (and swallowed ProcessLookupError)
        proc.kill.assert_called_once()
        # Must have called wait_for twice (SIGTERM + SIGKILL windows)
        assert call_count_ref[0] == 2

    @pytest.mark.asyncio
    async def test_second_wait_timeout_logs_warning_no_raise(self):
        """After SIGKILL, if wait_for times out again, log warning but don't raise."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock()
        proc.kill = MagicMock()  # succeeds
        coord.nvr_processes[CAM_ID] = proc

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

    @pytest.mark.asyncio
    async def test_stop_recorder_calls_stop_preroll(self):
        """stop_recorder must call stop_preroll_recorder to kill the pre-roll ffmpeg."""
        from custom_components.bosch_shc_camera import recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

        stopped_preroll = []

        async def fake_stop_preroll(c, cid, **kwargs):
            stopped_preroll.append(cid)

        with patch.object(
            recorder, "stop_preroll_recorder", side_effect=fake_stop_preroll
        ):
            await recorder.stop_recorder(coord, cam_id)

        assert cam_id in stopped_preroll, (
            "stop_preroll_recorder was not called from stop_recorder"
        )


class TestStopAll:
    @pytest.mark.asyncio
    async def test_stops_every_running_recorder(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc_a = _mock_proc(returncode=0)
        proc_b = _mock_proc(returncode=0)
        coord.nvr_processes["cam-A"] = proc_a
        coord.nvr_processes["cam-B"] = proc_b
        await recorder.stop_all(coord)
        # Both must be drained
        assert coord.nvr_processes == {}

    @pytest.mark.asyncio
    async def test_empty_dict_is_safe(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord.nvr_processes.clear()
        await recorder.stop_all(coord)

    @pytest.mark.asyncio
    async def test_stop_all_calls_stop_all_preroll(self):
        """stop_all must call stop_all_preroll before stopping main recorders."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        coord.nvr_processes = {"cam1": MagicMock()}

        stop_all_preroll_called = []

        async def fake_stop_all_preroll(c):
            stop_all_preroll_called.append(True)

        with patch.object(
            recorder, "stop_all_preroll", side_effect=fake_stop_all_preroll
        ):
            with patch.object(recorder, "stop_recorder", new=AsyncMock()):
                await recorder.stop_all(coord)

        assert stop_all_preroll_called, "stop_all_preroll was not called from stop_all"


class TestNvrShutdownRace:
    """issue #47: a recorder/ring ffmpeg spawn still in flight when a
    config-entry unload/reload begins must not survive as an orphaned,
    never-killed process."""

    @pytest.mark.asyncio
    async def test_start_recorder_refuses_to_spawn_when_shutting_down(self):
        from custom_components.bosch_shc_camera import recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        coord.nvr_shutting_down = True

        with patch("asyncio.create_subprocess_exec") as spawn_mock:
            await recorder.start_recorder(coord, cam_id)

        spawn_mock.assert_not_called()
        assert cam_id not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_spawn_preroll_refuses_when_shutting_down(self):
        from custom_components.bosch_shc_camera import recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        coord._nvr_mode_preference[cam_id] = "event_buffered"
        coord.nvr_shutting_down = True

        with patch("asyncio.create_subprocess_exec") as spawn_mock:
            await recorder.start_preroll_recorder(coord, cam_id)

        spawn_mock.assert_not_called()
        assert cam_id not in coord.nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_shutting_down_defaults_false_on_bare_stub(self):
        """A coordinator stub predating this fix (no `nvr_shutting_down`
        attribute at all) must behave exactly as before — spawn allowed."""
        from custom_components.bosch_shc_camera import recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        assert not hasattr(coord, "nvr_shutting_down")

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await recorder.start_recorder(coord, cam_id)

        assert coord.nvr_processes.get(cam_id) is mock_proc

    @pytest.mark.asyncio
    async def test_stop_all_sweeps_cameras_known_only_via_camera_entities(self):
        """A camera with no tracked process yet (e.g. its start_recorder
        call is still in flight) but present in `camera_entities` must
        still have its per-cam lock acquired by the unload sweep — closes
        the exact gap a stale `list(nvr_processes.keys())` snapshot had."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord.camera_entities = {"cam-new": MagicMock()}
        locked_cams = []
        real_get_lock = coord.get_nvr_recorder_lock

        def _tracking_get_lock(cam_id):
            locked_cams.append(cam_id)
            return real_get_lock(cam_id)

        coord.get_nvr_recorder_lock = _tracking_get_lock
        await recorder.stop_all(coord)
        assert "cam-new" in locked_cams

    @pytest.mark.asyncio
    async def test_stop_all_serializes_on_per_cam_lock(self):
        """stop_all must not touch a camera's process until it can acquire
        that camera's `get_nvr_recorder_lock` — proves it cannot race a
        concurrent in-flight start_recorder holding the same lock."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=None)
        coord.nvr_processes[CAM_ID] = proc

        lock = coord.get_nvr_recorder_lock(CAM_ID)
        await lock.acquire()
        try:
            task = asyncio.ensure_future(recorder.stop_all(coord))
            await asyncio.sleep(0)
            # stop_all is blocked waiting for the lock we hold — the
            # process must be untouched while blocked.
            assert CAM_ID in coord.nvr_processes
        finally:
            lock.release()
        await task
        assert CAM_ID not in coord.nvr_processes


class TestPrerollRecorderLifecycle(unittest.TestCase):
    def test_start_preroll_requires_local_session(self):
        """No LOCAL session → preroll recorder not spawned."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        coord.live_connections[cam_id]["_connection_type"] = "REMOTE"

        async def _run():
            await recorder.start_preroll_recorder(coord, cam_id)
            return cam_id in coord.nvr_preroll_processes

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_start_preroll_stores_process(self):
        """Valid LOCAL session → process stored in nvr_preroll_processes."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        coord._nvr_mode_preference[cam_id] = "event_buffered"

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                await recorder.start_preroll_recorder(coord, cam_id)
            return coord.nvr_preroll_processes.get(cam_id)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is mock_proc

    def test_stop_preroll_removes_process(self):
        """stop_preroll_recorder pops process from dict and sends SIGTERM."""
        import signal as _signal

        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.send_signal = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        coord.nvr_preroll_processes[cam_id] = mock_proc

        async def _run():
            await recorder.stop_preroll_recorder(coord, cam_id)
            return cam_id in coord.nvr_preroll_processes

        still_present = asyncio.get_event_loop().run_until_complete(_run())
        assert still_present is False
        mock_proc.send_signal.assert_called_once_with(_signal.SIGTERM)

    def test_stop_preroll_noop_when_no_process(self):
        """stop_preroll_recorder is a no-op when no process registered."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

        async def _run():
            await recorder.stop_preroll_recorder(coord, cam_id)

        # Should not raise
        asyncio.get_event_loop().run_until_complete(_run())

    def test_stop_all_preroll_stops_all(self):
        """stop_all_preroll calls stop for every cam in nvr_preroll_processes."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_phase_coord()

        stopped = []

        async def mock_stop(c, cid):
            stopped.append(cid)

        coord.nvr_preroll_processes = {"cam1": MagicMock(), "cam2": MagicMock()}

        async def _run():
            with patch.object(recorder, "stop_preroll_recorder", side_effect=mock_stop):
                await recorder.stop_all_preroll(coord)

        asyncio.get_event_loop().run_until_complete(_run())
        assert set(stopped) == {"cam1", "cam2"}


class TestStartPrerollRecorderSerialization:
    """issue #44 (realKim-dotcom): concurrent start_preroll_recorder callers
    for the same camera (switch turn-on, stream-up hook, mode select can
    all reach this) must not race — the second must wait for the first to
    finish (serialized on the same per-camera lock `start_recorder`'s main
    spawn uses), not leak a second untracked ffmpeg ring writer that
    interleaves segments with the first."""

    @pytest.mark.asyncio
    async def test_concurrent_calls_do_not_overlap_spawns(self, tmp_path: Path):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"

        active = 0
        max_active = 0

        async def _spawn(*_a, **_kw):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return _mock_proc(returncode=None)

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await asyncio.gather(
                recorder.start_preroll_recorder(coord, CAM_ID),
                recorder.start_preroll_recorder(coord, CAM_ID),
            )

        assert max_active == 1, (
            "two ffmpeg ring writers spawned concurrently — the race is not serialized"
        )

    @pytest.mark.asyncio
    async def test_concurrent_calls_leave_exactly_one_tracked_process(
        self, tmp_path: Path
    ):
        """After two concurrent calls settle, exactly the LAST spawn is
        tracked — the first caller's process was cleanly stopped by the
        second caller's leading stop_preroll_recorder(), not silently
        overwritten while still running (the actual leak in #44)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"

        procs: list[MagicMock] = []

        async def _spawn(*_a, **_kw):
            proc = _mock_proc(returncode=None)
            procs.append(proc)
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await asyncio.gather(
                recorder.start_preroll_recorder(coord, CAM_ID),
                recorder.start_preroll_recorder(coord, CAM_ID),
            )

        assert len(procs) == 2
        assert coord.nvr_preroll_processes[CAM_ID] is procs[-1]
        # The first caller's process must have been asked to stop (not
        # leaked as an untracked orphan) — the second call's leading
        # stop_preroll_recorder() SIGTERMs whatever is currently tracked.
        procs[0].send_signal.assert_called_once_with(signal.SIGTERM)


class TestStartPrerollRecorder:
    """LOCAL-gating full path + ffmpeg FileNotFoundError/OSError cleanup,
    using the tmp_path-based lifecycle coordinator stub."""

    @pytest.mark.asyncio
    async def test_skipped_when_not_local(self, tmp_path: Path):
        """`_connection_type != "LOCAL"` → early return. No spawn, no proc
        registered. Pre-roll is LAN-only by design."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(conn_type="REMOTE", base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord.nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_skipped_when_rtsp_url_missing(self, tmp_path: Path):
        """rtspsUrl empty / not rtsp:// → return."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        coord.live_connections[CAM_ID]["rtspsUrl"] = ""
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord.nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_happy_path_full_local(self, tmp_path: Path):
        """LOCAL + valid rtsp:// URL → walks the full path: makedirs,
        spawn, register proc, prune-on-spawn, register watcher task."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
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
        assert coord.nvr_preroll_processes[CAM_ID] is proc
        # Cache dir created under tmp_path / Terrasse
        assert (tmp_path / "Terrasse").exists()
        # start_preroll_recorder's own leading stop_preroll_recorder() call
        # is a respawn, not a genuine stop — it passes prune_cache=False so
        # the ring buffer's accumulated context survives a restart (issue
        # #43 follow-up bug-hunt finding: an earlier version wiped the ring
        # on every LOCAL-session renewal). Only prune-on-spawn (max_segs
        # from nvr_preroll_seconds=30 → ceil(30/10)+1 = 4) fires here.
        assert len(prune_calls) == 1
        assert prune_calls[0][1] == 4

    @pytest.mark.asyncio
    async def test_happy_path_wires_live_stderr_drain(self, tmp_path: Path):
        """GitHub #64 regression guard for the pre-roll ring spawn path —
        same rationale as the main recorder's sibling test above: proves
        `_spawn_stderr_drain_task` is actually wired to the real spawned
        preroll process at `_spawn_preroll_recorder_locked`'s real call
        site, not just that the drain function works when called directly."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        proc = _mock_proc(returncode=None)

        async def _spawn(*_args, **_kwargs):
            return proc

        with (
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn),
            patch.object(recorder, "_spawn_stderr_drain_task") as drain_spawn,
        ):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        drain_spawn.assert_called_once()
        call_coord, call_cam_id, call_proc = drain_spawn.call_args.args[:3]
        assert call_coord is coord
        assert call_cam_id == CAM_ID
        assert call_proc is proc
        assert (
            drain_spawn.call_args.kwargs["name_prefix"]
            == "bosch_nvr_preroll_stderr_drain"
        )

    @pytest.mark.asyncio
    async def test_nvr_quality_option_reaches_spawned_argv(self, tmp_path: Path):
        """GitHub #64 follow-up: the ring previously always requested the
        full inst=1 stream regardless of the user's nvr_quality option
        (unlike the continuous recorder). Proves the actual spawned argv
        carries the option, not just that the pure builder accepts it."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord.options["nvr_quality"] = "low"
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        coord.live_connections[CAM_ID]["rtspsUrl"] = "rtsp://cam/stream?inst=1"
        proc = _mock_proc(returncode=None)
        spawn_args: list[tuple] = []

        async def _spawn(*args, **_kwargs):
            spawn_args.append(args)
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert len(spawn_args) == 1
        args = spawn_args[0]
        assert args[args.index("-i") + 1] == "rtsp://cam/stream?inst=4"

    @pytest.mark.asyncio
    async def test_orphan_sweep_failure_swallowed_spawn_still_succeeds(
        self, tmp_path: Path
    ):
        """The orphan `_stage/*` sweep on ring spawn is best-effort — a
        failure there (e.g. permission denied) must not abort the spawn
        itself."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        proc = _mock_proc(returncode=None)

        async def _spawn(*_args, **_kwargs):
            return proc

        async def _flaky_executor(fn, *args, **kwargs):
            if fn is recorder._sweep_orphaned_stage_dirs:
                raise OSError("permission denied")
            return fn(*args, **kwargs) if callable(fn) else None

        coord.hass.async_add_executor_job = _flaky_executor

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert coord.nvr_preroll_processes[CAM_ID] is proc
        # Watcher task registered
        assert CAM_ID in coord.nvr_preroll_tasks
        assert coord.nvr_preroll_tasks[CAM_ID] is not None

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_cleanup(self, tmp_path: Path):
        """`create_subprocess_exec` → `FileNotFoundError`. Must log error +
        return WITHOUT registering proc or task — otherwise stop_preroll
        would later iterate over a None proc."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            # Must not raise.
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert CAM_ID not in coord.nvr_preroll_processes
        # Pre-roll watcher task also not registered.
        assert CAM_ID not in coord.nvr_preroll_tasks

    @pytest.mark.asyncio
    async def test_spawn_oserror_cleanup(self, tmp_path: Path):
        """Generic OSError on spawn — same cleanup invariant as the
        FileNotFoundError path."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("EAGAIN — fork limit"),
        ):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert CAM_ID not in coord.nvr_preroll_processes
        assert CAM_ID not in coord.nvr_preroll_tasks

    @pytest.mark.asyncio
    async def test_makedirs_failure_aborts(self, tmp_path: Path):
        """OSError during cache_dir creation → return, no spawn. Read-only
        fs / permission denied / NFS hiccup."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"

        async def _bad_executor(fn, *args, **kwargs):
            if fn is os.makedirs:
                raise OSError("EROFS")
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _bad_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord.nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_prune_exception_swallowed(self, tmp_path: Path):
        """If prune_preroll_cache raises any Exception, start_preroll continues."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
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
        assert coord.nvr_preroll_processes.get(CAM_ID) is proc

    @pytest.mark.asyncio
    async def test_preroll_tasks_auto_created_when_absent(self, tmp_path: Path):
        """If coordinator has no nvr_preroll_tasks attr, it is created."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        # Remove the attribute to trigger the hasattr branch
        del coord.nvr_preroll_tasks

        proc = _mock_proc(returncode=None)

        async def _spawn(*a, **kw):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        # The attribute must now exist and contain the registered task
        assert hasattr(coord, "nvr_preroll_tasks")
        assert CAM_ID in coord.nvr_preroll_tasks


class TestSpawnPrerollRecorderIdempotency:
    """2026-07-19 bug-hunt finding: `_spawn_preroll_recorder_locked`'s own
    per-camera lock only prevents two callers racing to spawn AT THE SAME
    instant — it does NOT prevent a double-spawn across two SEPARATE lock
    acquisitions with a gap in between (assemble_and_ship_motion_clip
    releases and re-acquires the lock three times with a live, unlocked
    postroll capture in between). An unrelated trigger (heartbeat cred-
    rotation restart, a LOCAL session renewal, a rapid switch re-toggle)
    could spawn its own ring via start_recorder in one of those gaps, and
    the finalize/restart bracket would then spawn a SECOND ring writer on
    top, leaking the first — same class of bug as #44, different trigger
    pair. Fix: never spawn while a ring writer is already alive for that
    camera, regardless of which trigger pair races."""

    @pytest.mark.asyncio
    async def test_skips_spawn_when_a_ring_writer_is_already_alive(
        self, tmp_path: Path
    ):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        existing_proc = _mock_proc(returncode=None)  # still running
        coord.nvr_preroll_processes[CAM_ID] = existing_proc

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder._spawn_preroll_recorder_locked(coord, CAM_ID)

        spawn.assert_not_called()
        # The existing process must be left untouched — not replaced.
        assert coord.nvr_preroll_processes[CAM_ID] is existing_proc

    @pytest.mark.asyncio
    async def test_spawns_when_the_registered_process_has_already_exited(
        self, tmp_path: Path
    ):
        """A registered process that already exited (returncode set, e.g.
        crashed and not yet cleaned up) must NOT block a genuine respawn —
        this guard is specifically about an ALIVE process, not any entry."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        dead_proc = _mock_proc(returncode=1)  # already exited
        coord.nvr_preroll_processes[CAM_ID] = dead_proc
        new_proc = _mock_proc(returncode=None)

        async def _spawn(*_a, **_kw):
            return new_proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder._spawn_preroll_recorder_locked(coord, CAM_ID)

        assert coord.nvr_preroll_processes[CAM_ID] is new_proc

    @pytest.mark.asyncio
    async def test_no_prior_process_spawns_normally(self, tmp_path: Path):
        """Control: no entry at all → the guard must not block a genuine
        first spawn."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._nvr_mode_preference[CAM_ID] = "event_buffered"
        assert CAM_ID not in coord.nvr_preroll_processes
        proc = _mock_proc(returncode=None)

        async def _spawn(*_a, **_kw):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder._spawn_preroll_recorder_locked(coord, CAM_ID)

        assert coord.nvr_preroll_processes[CAM_ID] is proc


class TestStopPrerollRecorder:
    """SIGKILL escalation race paths + send_signal ProcessLookupError."""

    @pytest.mark.asyncio
    async def test_kill_process_lookup_error_swallowed(self, tmp_path: Path):
        """SIGTERM times out → proc.kill() raises ProcessLookupError → no crash.

        Race: process died between our SIGTERM-timeout and the SIGKILL call.
        Must be swallowed so stop_preroll_recorder still completes cleanly.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        proc.kill = MagicMock(side_effect=ProcessLookupError("no such process"))
        coord.nvr_preroll_processes[CAM_ID] = proc

        # First wait_for (SIGTERM grace) → TimeoutError; second (post-SIGKILL) → resolves.
        with patch.object(
            asyncio,
            "wait_for",
            side_effect=[
                TimeoutError(),
                -9,
            ],
        ):
            # Must not raise
            await recorder.stop_preroll_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once()
        assert CAM_ID not in coord.nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_final_timeout_after_sigkill_swallowed(self, tmp_path: Path):
        """Even SIGKILL hung in wait_for → final TimeoutError is swallowed.

        Under no circumstances may stop_preroll_recorder propagate a
        TimeoutError; the watchdog must remain non-blocking so the
        integration unload path can finish.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID] = proc

        # Both wait_for calls time out — SIGKILL didn't take either.
        with patch.object(
            asyncio,
            "wait_for",
            side_effect=[
                TimeoutError(),
                TimeoutError(),
            ],
        ):
            await recorder.stop_preroll_recorder(coord, CAM_ID)
        proc.send_signal.assert_called_once_with(signal.SIGTERM)
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_process_registered_is_no_op(self, tmp_path: Path):
        """Pin idempotency: calling stop on a cam with no live process is safe."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        # No process registered
        await recorder.stop_preroll_recorder(coord, CAM_ID)
        # No state change, no exception
        assert CAM_ID not in coord.nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_already_exited_returns_quickly(self, tmp_path: Path):
        """If returncode is already set, send_signal is never called."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=0)
        coord.nvr_preroll_processes[CAM_ID] = proc

        await recorder.stop_preroll_recorder(coord, CAM_ID)
        proc.send_signal.assert_not_called()
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_signal_process_lookup_error_returns(self):
        """ProcessLookupError from send_signal means process is already gone — return."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=None)
        proc.send_signal = MagicMock(side_effect=ProcessLookupError)
        coord.nvr_preroll_processes[CAM_ID] = proc

        # Must not raise
        await recorder.stop_preroll_recorder(coord, CAM_ID)

        # Process must have been popped
        assert CAM_ID not in coord.nvr_preroll_processes

    def test_cancels_watcher_task(self):
        """stop_preroll_recorder must cancel the periodic prune-watcher task."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

        mock_task = MagicMock()
        mock_task.done.return_value = False
        coord.nvr_preroll_tasks = {cam_id: mock_task}

        # No process — but task should still be cancelled
        coord.nvr_preroll_processes = {}

        async def _run():
            await recorder.stop_preroll_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        mock_task.cancel.assert_called_once()
        assert cam_id not in coord.nvr_preroll_tasks

    @pytest.mark.asyncio
    async def test_returns_true_on_clean_sigterm_exit(self, tmp_path: Path):
        """stop_preroll_recorder now returns True iff SIGTERM exit was
        clean within the grace window — stop_and_finalize_preroll_recorder
        depends on this to know whether the newest segment's moov atom can
        be trusted (issue #43 follow-up feature)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID] = proc

        with patch.object(asyncio, "wait_for", return_value=None):
            result = await recorder.stop_preroll_recorder(coord, CAM_ID)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_hard_kill(self, tmp_path: Path):
        """A SIGTERM timeout forcing a hard kill must return False — no
        moov-atom guarantee on the segment that was mid-write."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID] = proc

        with patch.object(asyncio, "wait_for", side_effect=[TimeoutError(), None]):
            result = await recorder.stop_preroll_recorder(coord, CAM_ID)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_nothing_to_stop(self, tmp_path: Path):
        """No active process registered → nothing was stopped → False."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        result = await recorder.stop_preroll_recorder(coord, CAM_ID)
        assert result is False


class TestStopAndFinalizePrerollRecorder:
    """Opt-in stop-then-finalize assembly mode (issue #43 follow-up feature
    request, realKim-dotcom's fork): recovers the freshest ring segment for
    FCM-triggered clips instead of always dropping it.

    GitHub #50 (realKim-dotcom, 2026-07-15) redesign: this function now
    only stops the ring and returns a stable, ring-genuinely-stopped
    segment list — it no longer restarts the ring itself (see
    `restart_preroll_recorder_after_finalize`, tested separately below) nor
    relocates the finalized file to a sibling directory (no longer needed:
    the caller uses the returned list directly instead of re-scanning via
    `list_preroll_files()`, so there's no risk of the restarted ring's own
    scan picking the same file back up).
    """

    @pytest.mark.asyncio
    async def test_no_active_process_returns_false_empty(self):
        """Ring not running for this camera yet — nothing to finalize."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        # nvr_preroll_processes has no entry for CAM_ID_SHORT.
        result = await recorder.stop_and_finalize_preroll_recorder(coord, CAM_ID_SHORT)
        assert result == (False, [])

    @pytest.mark.asyncio
    async def test_dead_process_returns_false_empty(self):
        """A process handle that already exited must be treated as
        nothing-to-finalize, same as no handle at all."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        proc = _mock_proc(returncode=0)
        coord.nvr_preroll_processes[CAM_ID_SHORT] = proc
        result = await recorder.stop_and_finalize_preroll_recorder(coord, CAM_ID_SHORT)
        assert result == (False, [])

    @pytest.mark.asyncio
    async def test_empty_ring_returns_false_without_stopping(self):
        """The ring writer is alive but has produced no segments yet
        (`_newest_preroll_path` returns None) — must not stop a perfectly
        healthy ring writer for no benefit."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        coord.hass.async_add_executor_job = AsyncMock(
            side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        )
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID_SHORT] = proc

        with (
            patch.object(recorder, "_newest_preroll_path", return_value=None),
            patch.object(recorder, "stop_preroll_recorder") as mock_stop,
        ):
            result = await recorder.stop_and_finalize_preroll_recorder(
                coord, CAM_ID_SHORT
            )

        assert result == (False, [])
        mock_stop.assert_not_called()

    @pytest.mark.asyncio
    async def test_clean_exit_returns_all_segments_including_newest(
        self, tmp_path: Path
    ):
        """The common path: an active ring with a newest segment, clean
        SIGTERM exit → returns True plus EVERY segment still on disk
        (including the newest, since a clean exit guarantees its moov atom)
        — does NOT restart the ring (that's the caller's job, after it has
        built the clip from this list)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord(
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_preroll_cache_dir": str(tmp_path),
                "nvr_preroll_seconds": 30,
                "nvr_quality": "auto",
            }
        )
        coord.hass.async_add_executor_job = AsyncMock(
            side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        )
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID_SHORT] = proc
        cam_dir = recorder._preroll_dir(str(tmp_path), "Terrasse")
        os.makedirs(cam_dir)
        older = os.path.join(cam_dir, "115950.mp4")
        newest = os.path.join(cam_dir, "120000.mp4")
        for p in (older, newest):
            with open(p, "wb") as f:
                f.write(b"x" * 2048)
        # Distinct mtimes so _list_preroll_segments' oldest-first sort is
        # deterministic regardless of filesystem timestamp resolution.
        os.utime(older, (1000, 1000))
        os.utime(newest, (2000, 2000))

        with (
            patch.object(recorder, "_newest_preroll_path", return_value=newest),
            patch.object(
                recorder, "stop_preroll_recorder", new=AsyncMock(return_value=True)
            ) as mock_stop,
        ):
            ring_was_running, paths = await recorder.stop_and_finalize_preroll_recorder(
                coord, CAM_ID_SHORT
            )

        assert ring_was_running is True
        assert paths == [older, newest]
        # Clean exit — nothing discarded, both files still on disk.
        assert os.path.isfile(older)
        assert os.path.isfile(newest)
        mock_stop.assert_awaited_once_with(coord, CAM_ID_SHORT, prune_cache=False)

    @pytest.mark.asyncio
    async def test_hard_kill_excludes_and_deletes_newest_only(self, tmp_path: Path):
        """A forced kill (no moov-atom guarantee) must exclude JUST the
        untrustworthy newest segment from the returned list (and delete it)
        — every OTHER already-complete segment is still returned, since the
        ring itself is genuinely stopped either way. This is the actual
        GitHub #50 fix: the old implementation fell back to a second
        drop-newest scan here, losing a SECOND segment on top of the
        untrustworthy one."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord(
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_preroll_cache_dir": str(tmp_path),
                "nvr_preroll_seconds": 30,
                "nvr_quality": "auto",
            }
        )
        coord.hass.async_add_executor_job = AsyncMock(
            side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        )
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID_SHORT] = proc
        cam_dir = recorder._preroll_dir(str(tmp_path), "Terrasse")
        os.makedirs(cam_dir)
        older = os.path.join(cam_dir, "115950.mp4")
        newest = os.path.join(cam_dir, "120000.mp4")
        for p in (older, newest):
            with open(p, "wb") as f:
                f.write(b"x" * 2048)
        os.utime(older, (1000, 1000))
        os.utime(newest, (2000, 2000))

        with (
            patch.object(recorder, "_newest_preroll_path", return_value=newest),
            patch.object(
                recorder, "stop_preroll_recorder", new=AsyncMock(return_value=False)
            ),
        ):
            ring_was_running, paths = await recorder.stop_and_finalize_preroll_recorder(
                coord, CAM_ID_SHORT
            )

        assert ring_was_running is True
        assert paths == [older]
        assert os.path.isfile(older)  # the real, complete segment survives
        assert not os.path.exists(newest)  # untrustworthy segment deleted

    @pytest.mark.asyncio
    async def test_hard_kill_cleanup_unlink_failure_swallowed(self, tmp_path: Path):
        """If the untrustworthy newest file can't even be unlinked (e.g.
        it's already gone, or a permissions race), that must not raise into
        the caller — best-effort cleanup only, same discipline as every
        other cache-prune path in this module."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord(
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_preroll_cache_dir": str(tmp_path),
                "nvr_preroll_seconds": 30,
                "nvr_quality": "auto",
            }
        )
        coord.hass.async_add_executor_job = AsyncMock(
            side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        )
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID_SHORT] = proc
        cam_dir = recorder._preroll_dir(str(tmp_path), "Terrasse")
        os.makedirs(cam_dir)
        newest = os.path.join(cam_dir, "120000.mp4")
        with open(newest, "wb") as f:
            f.write(b"x" * 2048)

        with (
            patch.object(recorder, "_newest_preroll_path", return_value=newest),
            patch.object(
                recorder, "stop_preroll_recorder", new=AsyncMock(return_value=False)
            ),
            patch("os.unlink", side_effect=OSError("already gone")),
        ):
            ring_was_running, paths = await recorder.stop_and_finalize_preroll_recorder(
                coord, CAM_ID_SHORT
            )

        assert ring_was_running is True
        assert paths == []  # newest still excluded even though unlink failed

    @pytest.mark.asyncio
    async def test_holds_recorder_lock_across_stop(self):
        """Same serialization discipline as issue #44's fix: the per-camera
        recorder lock must stay held for the whole stop sequence so a
        concurrent start_preroll_recorder/switch-toggle can't race in
        between and leak a second untracked ring writer."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        coord.hass.async_add_executor_job = AsyncMock(
            side_effect=lambda fn, *a, **kw: fn(*a, **kw)
        )
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID_SHORT] = proc
        lock = coord.get_nvr_recorder_lock(CAM_ID_SHORT)
        observed_locked_during_stop = False

        async def _fake_stop(_coord, _cam_id, *, prune_cache):
            nonlocal observed_locked_during_stop
            observed_locked_during_stop = lock.locked()
            return True

        with (
            patch.object(recorder, "_newest_preroll_path", return_value="/x/y.mp4"),
            patch.object(recorder, "stop_preroll_recorder", side_effect=_fake_stop),
            patch.object(recorder, "_list_preroll_segments", return_value=[]),
        ):
            await recorder.stop_and_finalize_preroll_recorder(coord, CAM_ID_SHORT)

        assert observed_locked_during_stop is True
        assert not lock.locked()


class TestRestartPrerollRecorderAfterFinalize:
    """GitHub #50: the restart step is now its own function, deliberately
    called only after the caller has built the motion clip — see
    `TestStopAndFinalizePrerollRecorder`'s class docstring."""

    @pytest.mark.asyncio
    async def test_spawns_under_lock(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        lock = coord.get_nvr_recorder_lock(CAM_ID_SHORT)
        observed_locked = False

        async def _fake_spawn(_coord, _cam_id):
            nonlocal observed_locked
            observed_locked = lock.locked()

        with patch.object(
            recorder, "_spawn_preroll_recorder_locked", side_effect=_fake_spawn
        ) as mock_spawn:
            await recorder.restart_preroll_recorder_after_finalize(coord, CAM_ID_SHORT)

        mock_spawn.assert_awaited_once_with(coord, CAM_ID_SHORT)
        assert observed_locked is True
        assert not lock.locked()


class TestWatchRecorder:
    @pytest.mark.asyncio
    async def test_clean_exit_no_respawn(self):
        """Process exited cleanly AND was already removed from
        nvr_processes → no respawn (replacement / clean stop scenario)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=0)
        proc.wait = AsyncMock(return_value=0)
        # Not registered → already replaced/stopped
        with patch.object(recorder, "start_recorder", new=AsyncMock()) as restart:
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))
        restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_respawn_when_gate_closed(self):
        """ffmpeg crashed but should_record now False (cam offline / switch
        toggled off / went REMOTE) → don't respawn."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(conn_type="REMOTE")  # gate now closed
        proc = _mock_proc(returncode=1, stderr_data=b"connection refused")
        proc.wait = AsyncMock(return_value=1)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))
        restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_crash_triggers_respawn(self):
        """ffmpeg crashes within respawn window AND gate still open →
        respawn after delay."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()  # LOCAL, online
        proc = _mock_proc(returncode=1, stderr_data=b"transient")
        proc.wait = AsyncMock(return_value=1)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))
        restart.assert_awaited_once_with(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_second_crash_within_window_gives_up(self):
        """Two crashes inside the respawn window → set error_state, no respawn.
        Defends against an infinite restart loop when the camera is dead."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        # Mark a recent crash
        coord.nvr_recent_crash[CAM_ID] = time.monotonic() - 5  # 5 s ago
        proc = _mock_proc(returncode=1, stderr_data=b"crash 2")
        proc.wait = AsyncMock(return_value=1)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))
        restart.assert_not_called()
        assert "crashed" in coord.nvr_error_state.get(CAM_ID, "").lower()

    @pytest.mark.asyncio
    async def test_auth_failure_respawns_without_giveup(self):
        """Issue #42: a 401/Unauthorized ffmpeg exit (cred-rotation race) must
        retry without counting toward the 2-crash give-up threshold — a
        second back-to-back auth-failure must NOT set nvr_error_state or
        skip the respawn, unlike a genuine repeated crash."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        # Simulate the reported sequence: a crash was already recorded
        # moments ago — with a normal crash this would trigger give-up.
        coord.nvr_recent_crash[CAM_ID] = time.monotonic() - 5
        proc = _mock_proc(
            returncode=8, stderr_data=b"method OPTIONS failed: 401 (Unauthorized)"
        )
        proc.wait = AsyncMock(return_value=8)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        restart.assert_awaited_once_with(coord, CAM_ID, is_auto_retry=True)
        assert CAM_ID not in coord.nvr_error_state
        # The crash-window timestamp must be untouched by the auth-failure
        # path — it doesn't count as a "crash" for give-up purposes.
        assert coord.nvr_recent_crash[CAM_ID] == pytest.approx(
            time.monotonic() - 5, abs=1.0
        )

    @pytest.mark.asyncio
    async def test_respawn_raising_unexpectedly_sets_error_state_transient_path(self):
        """Maintenance-round bug-hunt finding, 2026-07-17: an unexpected
        exception from start_recorder's respawn call used to kill this
        background watcher task silently — no nvr_error_state, no listener
        push, leaving recording permanently stopped with zero user-visible
        signal (same "external trigger never fires again" shape as the
        fixed #51 bug). Must be caught, logged, and surfaced."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=1, stderr_data=b"transient")
        proc.wait = AsyncMock(return_value=1)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(
                recorder,
                "start_recorder",
                new=AsyncMock(side_effect=OSError("port bind failed")),
            ),
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            # Must NOT raise.
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        assert "respawn" in coord.nvr_error_state.get(CAM_ID, "").lower()
        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_respawn_raising_unexpectedly_sets_error_state_auth_retry_path(self):
        """Same fix, exercised via the auth-retry respawn call instead of
        the transient-crash one."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord.nvr_recent_crash[CAM_ID] = time.monotonic() - 5
        proc = _mock_proc(
            returncode=8, stderr_data=b"method OPTIONS failed: 401 (Unauthorized)"
        )
        proc.wait = AsyncMock(return_value=8)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(
                recorder,
                "start_recorder",
                new=AsyncMock(side_effect=OSError("port bind failed")),
            ),
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        assert "respawn" in coord.nvr_error_state.get(CAM_ID, "").lower()
        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_auth_failure_case_insensitive_and_lowercase_401(self):
        """The marker match must be case-insensitive and also catch a bare
        '401' without the word 'Unauthorized' in the tail."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=8, stderr_data=b"HTTP/1.1 401 \n")
        proc.wait = AsyncMock(return_value=8)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        restart.assert_awaited_once_with(coord, CAM_ID, is_auto_retry=True)
        assert CAM_ID not in coord.nvr_error_state

    @pytest.mark.asyncio
    async def test_auth_failure_no_respawn_when_gate_closed_after_sleep(self):
        """Same gate-recheck-after-sleep discipline as the normal crash path
        must apply to the auth-failure path too."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=8, stderr_data=b"401 unauthorized")
        proc.wait = AsyncMock(return_value=8)
        coord.nvr_processes[CAM_ID] = proc

        call_count = [0]

        def _toggling_should_record(c, cid, *, switch_on):
            call_count[0] += 1
            return call_count[0] == 1

        with (
            patch.object(
                recorder, "should_record", side_effect=_toggling_should_record
            ),
            patch.object(asyncio, "sleep", new=AsyncMock()),
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_closed_after_sleep_no_respawn(self):
        """If should_record returns False after the respawn sleep, don't respawn."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord.nvr_recent_crash = {CAM_ID: float("-inf")}
        proc = _mock_proc(returncode=1)  # non-zero → crash path
        coord.nvr_processes[CAM_ID] = proc

        # Make should_record return True first time (pre-sleep check) then
        # False after sleep, so we never call start_recorder. We toggle the
        # flag via a call counter.
        call_count = [0]

        def _toggling_should_record(c, cid, *, switch_on):
            call_count[0] += 1
            if call_count[0] == 1:
                return True  # gate open → proceed to sleep
            return False  # gate now closed → return without respawn

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
            patch("time.monotonic", return_value=9999.0),
        ):
            # Need elapsed >= _RESPAWN_WINDOW_SECONDS to skip crash-loop guard
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        # Sleep happened (respawn delay)
        assert len(sleep_calls) == 1
        # But start_recorder was NOT called (gate was closed)
        assert len(start_calls) == 0

    @pytest.mark.asyncio
    async def test_watch_recorder_uses_tail_populated_before_exit(self):
        """`_watch_recorder` must read its stderr tail from the
        `_StderrTail` object passed in (populated by a live
        `_drain_stderr_live` task, GitHub #64) — NOT from `proc.stderr`
        itself post-exit. Simulate a tail already containing data at the
        moment the process exits (exactly what a live drain leaves behind)
        and confirm it reaches the respawn-gating marker logic (ENOSPC)
        even though `proc.stderr.read` is never called at all."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=1)  # no stderr mock at all
        proc.wait = AsyncMock(return_value=1)
        coord.nvr_processes[CAM_ID] = proc
        tail = recorder._StderrTail(data=b"no space left on device")

        async def _fake_sleep(_secs):
            return None

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()),
            patch("asyncio.sleep", _fake_sleep),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, tail)

        # ENOSPC marker in the pre-populated tail must have been detected —
        # proves the tail buffer, not proc.stderr, is the diagnostic source.
        assert coord.nvr_error_state.get(CAM_ID) == "disk full"

    @pytest.mark.asyncio
    async def test_logs_stderr_tail_on_crash(self, caplog):
        """Sibling to `_watch_preroll_health`'s identical test — the main
        recorder's crash WARNING must actually contain real stderr content
        pulled from the live-drained tail, not just fire some log line.
        Mutation-tested gap: this assertion did not previously exist for
        the main recorder path (only the preroll ring had it)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=-11)
        proc.wait = AsyncMock(return_value=-11)
        coord.nvr_processes[CAM_ID] = proc
        tail = recorder._StderrTail(data=b"Non-monotonic DTS in output stream")

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()),
            patch.object(asyncio, "sleep", new=AsyncMock()),
            caplog.at_level("WARNING"),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, tail)

        assert any("exited rc=" in rec.message for rec in caplog.records)
        assert any("Non-monotonic DTS" in rec.message for rec in caplog.records)


class TestDrainStderrLive:
    """`_drain_stderr_live` is the actual fix for GitHub #64: without a live
    reader, ffmpeg's own write() to a full stderr pipe blocks forever and
    `proc.wait()` never returns — a completely silent hang, exactly the
    reported symptom (cache dir stays empty forever, no crash, no log)."""

    @pytest.mark.asyncio
    async def test_drains_large_output_without_hanging(self):
        """Proves the real deadlock scenario against a REAL OS pipe: spawn a
        python3 subprocess that writes more than the ~64KB default pipe
        buffer to stderr in one burst, THEN exits. Without a live reader,
        `proc.wait()` never returns (write() blocks on the full pipe) —
        this test asserts the whole drain+wait sequence completes well
        within a bounded timeout, i.e. it does NOT hang."""
        from custom_components.bosch_shc_camera import recorder

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * 200000); sys.stderr.flush()",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        tail = recorder._StderrTail()
        drain_task = asyncio.ensure_future(
            recorder._drain_stderr_live(proc.stderr, tail)
        )
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=10.0)
        finally:
            if not drain_task.done():
                drain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_task

        assert rc == 0
        # Tail is bounded to the configured max, not the full 200000 bytes.
        assert len(tail.data) == recorder._STDERR_TAIL_MAX_BYTES
        assert tail.data == b"x" * recorder._STDERR_TAIL_MAX_BYTES

    @pytest.mark.asyncio
    async def test_without_live_drain_large_output_hangs(self):
        """Negative control proving the ORIGINAL bug was real: the exact
        same subprocess, but with nothing draining stderr while it runs —
        only `proc.wait()`, mirroring the pre-fix `_watch_recorder`/
        `_watch_preroll_health` structure. Must NOT complete within a short
        deadline (the child blocks on its own stderr write()), demonstrating
        this is a genuine OS-level pipe deadlock, not a hypothetical."""
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * 200000); sys.stderr.flush()",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
        finally:
            # Cleanup: kill + drain now so the test doesn't leak a hung
            # child process/pipe past this test.
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)

    @pytest.mark.asyncio
    async def test_returns_on_none_stderr(self):
        """`proc.stderr` can be None (e.g. never piped) — must return
        immediately, not raise."""
        from custom_components.bosch_shc_camera import recorder

        tail = recorder._StderrTail()
        await asyncio.wait_for(recorder._drain_stderr_live(None, tail), timeout=1.0)
        assert tail.data == b""

    @pytest.mark.asyncio
    async def test_returns_on_eof(self):
        """A closed/exhausted stream (read() returns b"") ends the loop
        without raising."""
        from custom_components.bosch_shc_camera import recorder

        stderr = AsyncMock()
        stderr.read = AsyncMock(return_value=b"")
        tail = recorder._StderrTail()
        await asyncio.wait_for(recorder._drain_stderr_live(stderr, tail), timeout=1.0)
        assert tail.data == b""

    @pytest.mark.asyncio
    async def test_swallows_value_error(self):
        """Reading from an already-broken/closed pipe raises ValueError in
        practice — must be swallowed, not propagated (matches the old
        post-exit drain's broad exception tolerance for this exact case)."""
        from custom_components.bosch_shc_camera import recorder

        stderr = AsyncMock()
        stderr.read = AsyncMock(side_effect=ValueError("stream closed"))
        tail = recorder._StderrTail()
        await asyncio.wait_for(recorder._drain_stderr_live(stderr, tail), timeout=1.0)
        assert tail.data == b""

    @pytest.mark.asyncio
    async def test_swallows_connection_error(self):
        """A broken pipe on the read side raises ConnectionError — also
        swallowed."""
        from custom_components.bosch_shc_camera import recorder

        stderr = AsyncMock()
        stderr.read = AsyncMock(side_effect=ConnectionResetError("broken pipe"))
        tail = recorder._StderrTail()
        await asyncio.wait_for(recorder._drain_stderr_live(stderr, tail), timeout=1.0)
        assert tail.data == b""

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        """`asyncio.CancelledError` must NOT be swallowed — this task is
        tracked in `bg_tasks` and must be cleanly cancellable on integration
        unload like every other tracked background task."""
        from custom_components.bosch_shc_camera import recorder

        stderr = AsyncMock()

        async def _hang(_n):
            await asyncio.sleep(3600)

        stderr.read = _hang
        tail = recorder._StderrTail()
        task = asyncio.ensure_future(recorder._drain_stderr_live(stderr, tail))
        await asyncio.sleep(0)  # let it start waiting on read()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_tail_rolls_over_bound(self):
        """Multiple small chunks accumulating past `_STDERR_TAIL_MAX_BYTES`
        must roll — keep only the newest bytes, not grow unbounded (a
        long-lived continuous recorder with lots of warnings must not leak
        memory)."""
        from custom_components.bosch_shc_camera import recorder

        chunks = [b"a" * 1500, b"b" * 1500]  # 3000 total > 2048 max

        async def _fake_read(_n):
            if chunks:
                return chunks.pop(0)
            return b""

        stderr = AsyncMock()
        stderr.read = _fake_read
        tail = recorder._StderrTail()
        await asyncio.wait_for(recorder._drain_stderr_live(stderr, tail), timeout=1.0)
        assert len(tail.data) == recorder._STDERR_TAIL_MAX_BYTES
        # Newest bytes (the "b"s) must be what's kept.
        assert tail.data.endswith(b"b" * 1500)
        assert tail.data.startswith(b"a" * (recorder._STDERR_TAIL_MAX_BYTES - 1500))


class TestWatchPrerollRecorder:
    """Periodic prune loop that runs while a pre-roll ffmpeg child is alive.
    Exits when `proc.returncode is not None`. Without a fake-clock the loop
    would block 10s/iteration; `asyncio.sleep` is patched to no-op."""

    @pytest.mark.asyncio
    async def test_periodic_prune_called_then_exits_on_proc_exit(self, tmp_path: Path):
        """One prune iteration → proc.returncode set → loop exits.

        Fake `asyncio.sleep` (no real-time wait). After the first wakeup
        the watcher calls `prune_preroll_cache` once; before the second
        wakeup we set `proc.returncode = 0` so the early-return triggers
        and the loop exits cleanly.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID] = proc

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

        # prune must have been invoked at least once
        assert len(prune_calls) >= 1
        # cam_dir + max_segs passed through unchanged
        assert prune_calls[0] == (str(tmp_path / "cam"), 4)
        # Loop slept at least twice (first to call prune, second to see
        # the dead proc and return). Pure smoke for the periodic shape.
        assert sleep_count["n"] >= 2

    @pytest.mark.asyncio
    async def test_exits_when_proc_missing_from_dict(self, tmp_path: Path):
        """If `nvr_preroll_processes[cam_id]` is gone (clean stop / crash
        race), the watcher must exit on the next tick."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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

        # No prune attempted because we bailed before reaching that call.
        prune.assert_not_called()

    @pytest.mark.asyncio
    async def test_prune_exception_swallowed_then_proc_exits(self, tmp_path: Path):
        """`prune_preroll_cache` raising must not kill the watcher. After the
        swallow we let the proc exit on the next tick so the test terminates."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)
        coord.nvr_preroll_processes[CAM_ID] = proc

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
            # Must NOT raise — exception path swallows.
            await recorder._watch_preroll_recorder(
                coord,
                CAM_ID,
                str(tmp_path / "cam"),
                max_segs=4,
            )

        assert call_count["n"] >= 1

    def test_calls_prune_after_sleep_short_cam_id(self):
        """`_watch_preroll_recorder` calls prune_preroll_cache after one
        sleep cycle (short-form cam_id / MagicMock-hass coordinator variant).

        fake_sleep returns on the first call, then raises CancelledError on
        the second so the loop executes exactly one prune iteration.
        """
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        cam_dir = "/dev/shm/bosch_nvr_cache/Terrasse"
        max_segs = 4

        mock_proc = MagicMock()
        mock_proc.returncode = None
        coord.nvr_preroll_processes[cam_id] = mock_proc

        prune_calls = []

        async def fake_executor_job(fn, *args):
            # v12.4.1: watcher calls _prune_and_count (prune + return
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

    def test_exits_when_process_gone_short_cam_id(self):
        """`_watch_preroll_recorder` exits naturally when the process is no
        longer registered (short-form cam_id variant)."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        cam_dir = "/dev/shm/bosch_nvr_cache/Terrasse"

        # No process in nvr_preroll_processes → watcher should return
        coord.nvr_preroll_processes = {}

        async def _run():
            with patch("asyncio.sleep", new=AsyncMock()):
                await recorder._watch_preroll_recorder(coord, cam_id, cam_dir, 4)

        # Should complete without hanging
        asyncio.get_event_loop().run_until_complete(
            asyncio.wait_for(_run(), timeout=2.0)
        )

    def test_exits_when_process_exited_short_cam_id(self):
        """Watcher exits when proc.returncode is not None (ffmpeg finished)."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

        mock_proc = MagicMock()
        mock_proc.returncode = 0  # already exited
        coord.nvr_preroll_processes[cam_id] = mock_proc

        async def _run():
            with patch("asyncio.sleep", new=AsyncMock()):
                await recorder._watch_preroll_recorder(coord, cam_id, "/tmp/cache", 4)

        asyncio.get_event_loop().run_until_complete(
            asyncio.wait_for(_run(), timeout=2.0)
        )

    def test_start_preroll_creates_watcher_task(self):
        """start_preroll_recorder must register a background prune-watcher task."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        coord._nvr_mode_preference[cam_id] = "event_buffered"

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
        assert hasattr(coord, "nvr_preroll_tasks"), "nvr_preroll_tasks not created"
        assert cam_id in coord.nvr_preroll_tasks, "watcher task not stored for cam_id"


class TestWatchPrerollHealth:
    """GitHub #51 bug-hunt finding: unlike `_watch_recorder` (the main
    recorder's crash watchdog), the pre-roll ring previously had NO
    crash-respawn path at all — a ring that died mid-idle stayed dead
    indefinitely. `_watch_preroll_health` closes that gap; these tests pin
    its respawn/backoff/give-up behavior against the dedicated
    `_nvr_preroll_last_crash` tracker (kept separate from the main
    recorder's `nvr_recent_crash` so the two watchdogs can't clobber each
    other's crash-window state)."""

    @pytest.mark.asyncio
    async def test_intentional_stop_no_respawn(self):
        """If the process was already popped/replaced (an intentional stop
        or a fresh respawn already happened) by the time we wake, this is
        NOT a crash — must be a silent no-op."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=0)
        # Deliberately NOT registered in nvr_preroll_processes — simulates
        # stop_preroll_recorder having already popped it before this exit
        # was observed.

        with patch.object(
            recorder, "start_preroll_recorder", new=AsyncMock()
        ) as respawn:
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        respawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_crash_respawns_after_delay(self):
        """A genuine unexpected exit (process still the tracked one) with
        the gate open and no recent prior crash → respawn after the normal
        backoff delay."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc

        with (
            patch.object(
                recorder, "start_preroll_recorder", new=AsyncMock()
            ) as respawn,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        respawn.assert_awaited_once_with(coord, CAM_ID)
        # The dead process handle must not linger (GitHub #51 bug-hunt
        # finding: this used to keep `preroll_running` sensor attribute
        # misleadingly True after a crash).
        assert CAM_ID not in coord.nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_second_crash_within_window_gives_up(self):
        """Two crashes within `_RESPAWN_WINDOW_SECONDS` → the second one
        does NOT respawn (crash-loop guard), matching `_watch_recorder`'s
        discipline for the main recorder."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord._nvr_preroll_last_crash[CAM_ID] = time.monotonic()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc

        with (
            patch.object(
                recorder, "start_preroll_recorder", new=AsyncMock()
            ) as respawn,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        respawn.assert_not_awaited()
        # Maintenance-round bug-hunt finding, 2026-07-17: unlike
        # _watch_recorder's equivalent give-up paths, this branch used to
        # only log — no nvr_error_state, no listener push — so the
        # mini_nvr_state sensor's `error` attribute and the recording
        # switch's `last_error` attribute stayed blank with the ring
        # permanently dead and no UI signal.
        assert "pre-roll" in coord.nvr_error_state.get(CAM_ID, "").lower()
        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_respawn_raising_unexpectedly_sets_error_state(self):
        """Same class of bug as _watch_recorder's respawn-raises fix,
        applied to the pre-roll ring's own health watcher — ironic since
        this function exists specifically to close the 'external trigger
        never fires again' gap from #51. Must be caught, logged, and
        surfaced, not silently kill this background task."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc

        with (
            patch.object(
                recorder,
                "start_preroll_recorder",
                new=AsyncMock(side_effect=OSError("port bind failed")),
            ),
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            # Must NOT raise.
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        assert "respawn" in coord.nvr_error_state.get(CAM_ID, "").lower()
        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_no_respawn_when_gate_closed(self):
        """`should_record` False (switch off, camera offline, or gone
        REMOTE) → no respawn, even though this was a genuine crash."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord.nvr_user_intent[CAM_ID] = False
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc

        with patch.object(
            recorder, "start_preroll_recorder", new=AsyncMock()
        ) as respawn:
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        respawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_respawn_when_shutting_down(self):
        """Config-entry unload/HA-stop in progress → never respawn (would
        race `stop_all_preroll`'s sweep, same discipline as
        `_spawn_preroll_recorder_locked`'s own `nvr_shutting_down` guard)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord.nvr_shutting_down = True
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc

        with patch.object(
            recorder, "start_preroll_recorder", new=AsyncMock()
        ) as respawn:
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        respawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_gate_closes_during_backoff_sleep_no_respawn(self):
        """The gate is re-checked AFTER the backoff sleep too — a switch
        toggled off while we were waiting to respawn must still be honored,
        not just the check taken before the sleep."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc

        async def _close_gate_during_sleep(*_a, **_kw):
            coord.nvr_user_intent[CAM_ID] = False

        with (
            patch.object(
                recorder, "start_preroll_recorder", new=AsyncMock()
            ) as respawn,
            patch.object(
                asyncio, "sleep", new=AsyncMock(side_effect=_close_gate_during_sleep)
            ),
        ):
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        respawn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_logs_stderr_tail_on_crash(self, caplog):
        """The crash must be loud (WARNING log with the stderr tail) —
        this was the core of the reported gap: previously totally silent."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        stderr_reader = AsyncMock()
        stderr_reader.read = AsyncMock(
            return_value=b"Non-monotonic DTS in output stream"
        )
        proc.stderr = stderr_reader
        coord.nvr_preroll_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_preroll_recorder", new=AsyncMock()),
            patch.object(asyncio, "sleep", new=AsyncMock()),
            caplog.at_level("WARNING"),
        ):
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        assert any("exited unexpectedly" in rec.message for rec in caplog.records)
        assert any("Non-monotonic DTS" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_empty_stderr_tail_logs_no_stderr_placeholder(self, caplog):
        """An empty `_StderrTail` (e.g. the process crashed before the live
        drain task ever read any bytes, or stderr was never piped) must
        still proceed with the "(no stderr)" placeholder rather than
        failing the whole health-check — same diagnostic-value guarantee
        the old post-exit-read-with-timeout path gave, now sourced from the
        live tail buffer instead."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc
        tail = recorder._StderrTail()  # nothing collected yet

        with (
            patch.object(
                recorder, "start_preroll_recorder", new=AsyncMock()
            ) as respawn,
            patch.object(asyncio, "sleep", new=AsyncMock()),
            caplog.at_level("WARNING"),
        ):
            await recorder._watch_preroll_health(coord, CAM_ID, proc, tail)

        assert any("(no stderr)" in rec.message for rec in caplog.records)
        respawn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_shutting_down_during_backoff_sleep_no_respawn(self):
        """`nvr_shutting_down` is re-checked AFTER the backoff sleep too —
        a config-entry unload starting DURING the wait must still be
        honored, not just a pre-sleep snapshot (same discipline already
        verified for the switch-intent re-read)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = MagicMock()
        proc.wait = AsyncMock(return_value=-11)
        proc.stderr = None
        coord.nvr_preroll_processes[CAM_ID] = proc

        async def _shutdown_during_sleep(*_a, **_kw):
            coord.nvr_shutting_down = True

        with (
            patch.object(
                recorder, "start_preroll_recorder", new=AsyncMock()
            ) as respawn,
            patch.object(
                asyncio,
                "sleep",
                new=AsyncMock(side_effect=_shutdown_during_sleep),
            ),
        ):
            await recorder._watch_preroll_health(coord, CAM_ID, proc, _tail_for(proc))

        respawn.assert_not_awaited()


class TestPrerollWiring(unittest.TestCase):
    """Regression: start_preroll_recorder was never called from start_recorder
    (wiring omission found 2026-05-08 during live test). Verified by checking
    that /dev/shm/bosch_nvr_cache/ was never created despite preroll_seconds=30."""

    def test_start_recorder_calls_preroll_when_seconds_gt_zero(self):
        """start_recorder must call start_preroll_recorder when
        nvr_preroll_seconds > 0 AND mode is 'event_buffered' — the ring's
        output is only ever consumed by event_buffered's motion-clip
        assembly (GitHub #54)."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
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

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def _run():
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                with patch.object(
                    recorder,
                    "_spawn_preroll_recorder_locked",
                    side_effect=fake_start_preroll,
                ):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id in started_preroll, "start_preroll_recorder was not called"

    def test_start_recorder_skips_preroll_in_continuous_mode(self):
        """GitHub #54 (realKim-dotcom): while mode is 'continuous', the
        main ffmpeg recorder already captures everything the ring would —
        and motion-clip assembly (the ring's only consumer) is gated to
        'event_buffered'. Spawning the ring here is a second full-bandwidth
        ffmpeg consumer whose output nothing ever reads; on a
        bandwidth-constrained WiFi link the reporter measured this actively
        degrading the continuous recorder's own footage during the event
        burst. The ring must NOT spawn here even with
        nvr_preroll_seconds > 0."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
            cam_id=cam_id,
            opts={
                "nvr_base_path": "/config/bosch_nvr",
                "nvr_quality": "auto",
                "nvr_preroll_seconds": 30,
                "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
                "nvr_event_only": False,  # global default: continuous
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
                    recorder,
                    "_spawn_preroll_recorder_locked",
                    side_effect=fake_start_preroll,
                ):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id in coord.nvr_processes, "continuous recorder must still spawn"
        assert started_preroll == [], (
            "pre-roll ring must NOT spawn while mode is continuous — its "
            "output is never consumed and it only doubles bandwidth load"
        )

    def test_start_recorder_skips_preroll_when_seconds_zero(self):
        """start_recorder must NOT call start_preroll_recorder when nvr_preroll_seconds=0."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
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
                    recorder,
                    "_spawn_preroll_recorder_locked",
                    side_effect=fake_start_preroll,
                ):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id not in started_preroll, (
            "start_preroll_recorder was called despite seconds=0"
        )


class TestGitHub64PrerollRingGenuineEndToEnd(unittest.TestCase):
    """GitHub #64 (Lawyer82): Event Buffered (Preroll) mode,
    nvr_preroll_seconds=10 (nonzero — the zero-seconds case is a distinct,
    already-fixed bug per `TestPrerollWiring`), nvr_postroll_seconds=20,
    Live Stream + Mini-NVR Recording switches ON — reporter found
    /dev/shm/bosch_nvr_cache stayed completely empty (zero files ever
    written), checked repeatedly including after runtime switch
    re-toggles, on v16.1.5-beta-2 AND v16.1.5-beta-6.

    Every pre-existing "event_buffered + nonzero preroll -> ring starts"
    test (`TestPrerollWiring` above) mocked out
    `_spawn_preroll_recorder_locked` itself via `patch.object`, so none of
    them ever exercised the real function body — the LOCAL/rtsp_url gates,
    `os.makedirs`, or the real `asyncio.create_subprocess_exec` call. This
    test deliberately does NOT mock `_spawn_preroll_recorder_locked` — it
    only fakes the ffmpeg subprocess boundary (same
    `patch("asyncio.create_subprocess_exec", ...)` convention every other
    real-path recorder test in this file uses) and drives real
    `os.makedirs`/directory checks against `tmp_path` via
    `_make_lifecycle_coord`'s inline executor stub, so it actually proves
    (or disproves) that the ring reaches a real ffmpeg argv targeting the
    configured cache dir.
    """

    def test_ring_actually_spawns_for_event_buffered_nonzero_preroll(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = os.path.join(tmp, "bosch_nvr_cache")
            coord = _make_lifecycle_coord(base_path=os.path.join(tmp, "bosch_nvr"))
            coord.options["nvr_preroll_cache_dir"] = cache_dir
            coord.options["nvr_preroll_seconds"] = 10
            coord.options["nvr_postroll_seconds"] = 20
            coord._nvr_mode_preference[CAM_ID] = "event_buffered"
            coord._nvr_preroll_zero_warned = set()

            mock_proc = MagicMock()
            mock_proc.returncode = None
            mock_proc.pid = 4242
            mock_proc.wait = AsyncMock(return_value=0)
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"")

            captured: dict[str, list[str]] = {}

            async def _fake_exec(*args, **_kwargs):
                captured["args"] = list(args)
                return mock_proc

            async def _run():
                with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
                    await recorder.start_recorder(coord, CAM_ID)

            asyncio.get_event_loop().run_until_complete(_run())

            # No continuous-mode ffmpeg — only the pre-roll ring.
            assert CAM_ID not in coord.nvr_processes
            assert CAM_ID in coord.nvr_preroll_processes, (
                "the pre-roll ring must be tracked as running — this is "
                "exactly the state GitHub #64's reporter never observed "
                "(cache dir stayed empty, nothing ever spawned)"
            )
            assert "args" in captured, (
                "asyncio.create_subprocess_exec was never called — the ring "
                "ffmpeg never actually spawned"
            )
            argv = captured["args"]
            assert argv[0] == "ffmpeg"
            target = argv[-1]
            assert cache_dir in target, (
                f"ffmpeg target pattern {target!r} does not point into the "
                f"configured cache dir {cache_dir!r}"
            )
            assert "Terrasse" in target
            # The real os.makedirs call (via _make_lifecycle_coord's inline
            # executor) must have actually created the per-camera cache dir
            # — the exact directory GitHub #64's reporter found empty.
            assert os.path.isdir(os.path.join(cache_dir, "Terrasse")), (
                "pre-roll cache dir for the camera was never created on disk"
            )


class TestNvrSwitchTurnOnOff:
    """The switch is a thin shim over `coordinator.start_recorder` /
    `stop_recorder`. Pin that shape so a refactor can't introduce a third
    state-machine path that bypasses the coordinator."""

    def _stub_entry(self):
        return SimpleNamespace(
            entry_id="01ENTRY",
            data={"bearer_token": "x"},
            options={"enable_nvr": True},
        )

    def _stub_coord(self):
        return SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {"title": CAM_TITLE},
                    "status": "ONLINE",
                    "events": [],
                }
            },
            live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
            nvr_processes={},
            nvr_user_intent={},
            nvr_error_state={},
            last_update_success=True,
            options={"enable_nvr": True},
            is_camera_online=lambda cid: True,
            is_session_stale=lambda cid: False,
            is_stream_warming=lambda cid: False,
            start_recorder=AsyncMock(),
            stop_recorder=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_async_turn_on_calls_start_recorder(self):
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.start_recorder.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_async_turn_off_calls_stop_recorder(self):
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.stop_recorder.assert_awaited_once_with(CAM_ID)

    def test_unique_id_matches_concept_doc(self):
        """`bosch_shc_nvr_recording_<lowercased-cam-id>` — pinned so users'
        dashboards / automations don't break across versions."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        assert sw.unique_id == f"bosch_shc_nvr_recording_{CAM_ID.lower()}"

    def test_translation_key_set(self):
        """Single source of truth for UI strings — `nvr_recording`."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        assert sw.translation_key == "nvr_recording"

    def test_entity_disabled_by_default(self):
        """Opt-in feature — must not auto-add to the entity registry as enabled."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        assert sw.entity_registry_enabled_default is False

    def test_is_on_reflects_user_intent(self):
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        assert sw.is_on is False
        coord.nvr_user_intent[CAM_ID] = True
        assert sw.is_on is True

    def test_available_only_when_local(self):
        """Available iff: last_update_success ∧ camera ONLINE ∧ conn_type LOCAL.
        Same gate as `should_record` minus the user-intent check (the switch
        widget itself stays interactive even when the underlying conditions
        aren't met for recording)."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        # Baseline = LOCAL + ONLINE + last_update_success → available.
        assert sw.available is True
        # Flip to REMOTE → unavailable.
        coord.live_connections[CAM_ID]["_connection_type"] = "REMOTE"
        assert sw.available is False

    def test_available_false_when_camera_offline(self):
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = self._stub_coord()
        coord.is_camera_online = lambda cid: False
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, self._stub_entry())
        assert sw.available is False


class TestSwitchSetupGate:
    """`enable_nvr` is the explicit opt-in switch in config_flow. Verify the
    setup function only adds the per-camera NVR switch when the option is
    True — otherwise existing users see a surprise new entity per camera."""

    def test_switch_class_constructible_with_option_enabled(self):
        """Smoke: a stub coordinator with `enable_nvr: True` lets the entity
        be constructed without raising. (Full setup_entry is too async-heavy
        for a unit test; this catches the most common breakage — mismatched
        constructor signature after a refactor.)"""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
        )

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {"title": CAM_TITLE},
                    "status": "ONLINE",
                    "events": [],
                }
            },
            live_connections={},
            nvr_processes={},
            nvr_user_intent={},
            nvr_error_state={},
            last_update_success=True,
            options={"enable_nvr": True},
            is_camera_online=lambda cid: True,
            is_session_stale=lambda cid: False,
            is_stream_warming=lambda cid: False,
        )
        entry = SimpleNamespace(
            entry_id="01ENTRY",
            data={"bearer_token": "x"},
            options={"enable_nvr": True},
        )
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, entry)
        # Object is alive + carries the expected unique_id prefix.
        assert sw.unique_id.startswith("bosch_shc_nvr_recording_")


# staging-drain pipeline (recorder.drain_staging_to_remote, sync_drain_tick
# and friends) — NVR-storage-target upload/promote/retention flow introduced
# in v11.0.4. Covers: _is_segment_finalized (mtime+size gate),
# _list_staging_candidates (directory walker), sync_drain_tick (local/smb/ftp
# dispatch + per-camera diagnostic state + retry-cap quarantine), SMB/FTP
# retention purge respecting nvr_smb_subpath, and the watcher coroutine's
# start/stop/exception-swallowing semantics. All filesystem and network I/O
# is mocked; tests use tmp_path so nothing escapes the per-test sandbox.

CAM = "Terrasse"


def _make_coord(
    tmp_path: Path,
    *,
    target: str = "local",
    smb_subpath: str = "NVR",
    smb_server: str = "fritz.box",
    smb_share: str = "FRITZ.NAS",
    smb_base_path: str = "Bosch-Kameras",
    retention_days: int = 3,
):
    """Coordinator stub with everything the drain helpers read."""
    return SimpleNamespace(
        options={
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path),
            "nvr_storage_target": target,
            "nvr_smb_subpath": smb_subpath,
            "nvr_retention_days": retention_days,
            "smb_server": smb_server,
            "smb_share": smb_share,
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": smb_base_path,
        },
        hass=SimpleNamespace(
            async_add_executor_job=MagicMock(),
            loop=SimpleNamespace(call_soon_threadsafe=MagicMock()),
            async_create_task=MagicMock(),
            services=SimpleNamespace(async_call=MagicMock()),
        ),
    )


def _make_segment(
    tmp_path: Path,
    cam: str,
    date: str,
    name: str,
    *,
    age_seconds: float = 120,
    size_kb: int = 100,
) -> Path:
    """Create a fake staging segment with a known mtime + size."""
    cam_dir = tmp_path / "_staging" / cam / date
    cam_dir.mkdir(parents=True, exist_ok=True)
    p = cam_dir / name
    p.write_bytes(b"x" * size_kb * 1024)
    mtime = time.time() - age_seconds
    os.utime(p, (mtime, mtime))
    return p


class TestIsSegmentFinalized:
    def test_too_young_returns_false(self):
        assert (
            recorder._is_segment_finalized(
                mtime=time.time() - 10,
                size=100_000,
            )
            is False
        )

    def test_too_small_returns_false(self):
        assert (
            recorder._is_segment_finalized(
                mtime=time.time() - 120,
                size=100,
            )
            is False
        )

    def test_old_enough_and_big_enough_returns_true(self):
        assert (
            recorder._is_segment_finalized(
                mtime=time.time() - 120,
                size=100_000,
            )
            is True
        )

    def test_explicit_now_arg(self):
        """`now` lets tests pin the current time without monkeypatching ``time.time``."""
        ref_now = 1_000_000.0
        assert (
            recorder._is_segment_finalized(
                mtime=ref_now - 120,
                size=100_000,
                now=ref_now,
            )
            is True
        )
        assert (
            recorder._is_segment_finalized(
                mtime=ref_now - 5,
                size=100_000,
                now=ref_now,
            )
            is False
        )


class TestListStagingCandidates:
    def test_missing_root_returns_empty(self, tmp_path: Path):
        assert (
            recorder._list_staging_candidates(
                str(tmp_path / "does-not-exist"),
            )
            == []
        )

    def test_walks_cam_date_files(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        _make_segment(tmp_path, CAM, "2026-05-06", "10-05.mp4")
        _make_segment(tmp_path, "Innen", "2026-05-06", "11-00.mp4")
        out = recorder._list_staging_candidates(
            str(tmp_path / "_staging"),
        )
        cams = {entry[1] for entry in out}
        assert cams == {CAM, "Innen"}
        # Three files total
        assert len(out) == 3

    def test_skips_non_dir_entries_in_root(self, tmp_path: Path):
        """A stray file under _staging/ must not blow up the walk."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        (staging / "stray.mp4").write_bytes(b"x")
        out = recorder._list_staging_candidates(str(staging))
        assert out == []

    def test_skips_non_dir_date_entry(self, tmp_path: Path):
        """A stray file under _staging/<cam>/ must not blow up."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        cam = staging / CAM
        cam.mkdir()
        (cam / "stray.mp4").write_bytes(b"x")
        out = recorder._list_staging_candidates(str(staging))
        assert out == []


class TestSyncDrainTickLocal:
    def test_finalized_segment_promoted_to_local_layout(self, tmp_path: Path):
        """target=local → file moves from _staging tree to the canonical
        ``{base}/{cam}/{date}/...`` layout the Media Source already browses."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="local")
        result = recorder.sync_drain_tick(coord)
        promoted_path = tmp_path / CAM / "2026-05-06" / "10-00.mp4"
        assert promoted_path.exists()
        # Staging file removed by ``shutil.move``.
        assert not (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert result["promoted"] == 1
        assert result["uploaded"] == 0
        assert result["failed"] == 0
        assert result["pending"] == 0

    def test_too_young_segment_left_in_staging(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4", age_seconds=5)
        coord = _make_coord(tmp_path, target="local")
        result = recorder.sync_drain_tick(coord)
        assert result["pending"] == 1
        # File untouched.
        assert (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()

    def test_unknown_target_falls_through_to_local(self, tmp_path: Path):
        """Misconfigured target → fail-safe to local promotion (never to nowhere)."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="garbage")
        result = recorder.sync_drain_tick(coord)
        assert result["promoted"] == 1


class TestSyncDrainTickSmb:
    def test_smb_target_invokes_upload(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb", return_value=True) as up:
            result = recorder.sync_drain_tick(coord)
        up.assert_called_once()
        cam_arg = up.call_args.args[2]
        assert cam_arg == CAM
        # Successful upload → staging file is unlinked.
        assert not (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert result["uploaded"] == 1
        assert result["failed"] == 0

    def test_smb_failure_is_counted(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb", return_value=False):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 0
        assert result["failed"] == 1
        # Staging file kept for retry.
        assert (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()


class TestSyncDrainTickFtp:
    def test_ftp_target_invokes_upload(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_upload_ftp", return_value=True) as up:
            result = recorder.sync_drain_tick(coord)
        up.assert_called_once()
        assert result["uploaded"] == 1
        assert not (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()

    def test_ftp_failure_is_counted(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_upload_ftp", return_value=False):
            result = recorder.sync_drain_tick(coord)
        assert result["failed"] == 1


class TestSyncDrainTickRetryCap:
    """5 failures → file moves to _failed/ + persistent_notification fired."""

    def test_quarantine_after_max_retries(self, tmp_path: Path):
        path = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb", return_value=False):
            for _ in range(recorder._DRAIN_MAX_RETRIES):
                recorder.sync_drain_tick(coord)
        # File is now under _failed/, not _staging/
        assert not path.exists()
        failed_path = tmp_path / "_failed" / CAM / "2026-05-06" / "10-00.mp4"
        assert failed_path.exists()
        # Counter is cleared once the file is quarantined.
        assert path.as_posix() not in coord.nvr_drain_failures


class TestSyncDrainTickStateCounters:
    """The watcher persists state on the coordinator so the diagnostic sensor
    can render it. Pin the shape."""

    def test_drain_state_populated(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="local")
        recorder.sync_drain_tick(coord, now=time.time())
        state = coord.nvr_drain_state
        assert state["target"] == "local"
        assert state["promoted"] == 1
        assert "last_age_by_cam" in state
        assert CAM in state["last_age_by_cam"]


class TestNvrCleanupSmbSubpath:
    def test_smb_root_uses_nvr_subpath(self, tmp_path: Path):
        """``_sync_nvr_cleanup_smb`` must walk ONLY the NVR subtree, not the
        entire share — otherwise it would delete cloud-event uploads too."""
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )
        seen: list[str] = []

        def fake_scandir(path):
            seen.append(path)
            return iter([])  # empty → walk terminates

        # smbclient is imported lazily inside the helper — patch its API.
        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=MagicMock(),
                    scandir=fake_scandir,
                    remove=MagicMock(),
                    stat=MagicMock(),
                ),
            },
        ):
            recorder._sync_nvr_cleanup_smb(coord)

        # Walked path must end with the NVR subtree, not the bare share.
        assert seen, "_sync_nvr_cleanup_smb did not invoke scandir"
        assert seen[0].endswith("\\Bosch\\NVR")

    def test_smb_skip_without_server(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="smb", smb_server="")
        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=MagicMock(),
                    scandir=MagicMock(),
                    remove=MagicMock(),
                    stat=MagicMock(),
                ),
            },
        ):
            # Should be a no-op — no scandir call.
            recorder._sync_nvr_cleanup_smb(coord)


class TestNvrCleanupFtpSubpath:
    def test_ftp_root_uses_nvr_subpath(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        cwd_calls: list[str] = []
        ftp = MagicMock()

        def cwd(p):
            cwd_calls.append(p)

        ftp.cwd.side_effect = cwd
        ftp.retrlines.side_effect = lambda cmd, cb: None  # empty listing
        ftp.quit.return_value = None

        with (
            patch.object(recorder, "_ftp_connect", create=True),
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
            ),
        ):
            recorder._sync_nvr_cleanup_ftp(coord)
        assert cwd_calls
        # First cwd targets the NVR subtree.
        assert cwd_calls[0] == "/Bosch/NVR"

    def test_ftp_zero_retention_skipped(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="ftp", retention_days=0)
        # Should never even try to connect.
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect") as conn:
            recorder._sync_nvr_cleanup_ftp(coord)
            conn.assert_not_called()


class TestNvrCleanupDispatch:
    """``sync_nvr_cleanup`` is the public entry point — it dispatches to the
    target-specific helper plus always purges the local staging tree."""

    def test_local_only_calls_local_helper(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="local")
        with (
            patch.object(recorder, "_sync_nvr_cleanup_local") as loc,
            patch.object(recorder, "_sync_nvr_cleanup_smb") as smb,
            patch.object(recorder, "_sync_nvr_cleanup_ftp") as ftp,
        ):
            recorder.sync_nvr_cleanup(coord)
        loc.assert_called_once_with(coord)
        smb.assert_not_called()
        ftp.assert_not_called()

    def test_smb_target_calls_smb_and_local(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="smb")
        with (
            patch.object(recorder, "_sync_nvr_cleanup_local") as loc,
            patch.object(recorder, "_sync_nvr_cleanup_smb") as smb,
            patch.object(recorder, "_sync_nvr_cleanup_ftp") as ftp,
        ):
            recorder.sync_nvr_cleanup(coord)
        loc.assert_called_once_with(coord)
        smb.assert_called_once_with(coord)
        ftp.assert_not_called()

    def test_ftp_target_calls_ftp_and_local(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="ftp")
        with (
            patch.object(recorder, "_sync_nvr_cleanup_local") as loc,
            patch.object(recorder, "_sync_nvr_cleanup_smb") as smb,
            patch.object(recorder, "_sync_nvr_cleanup_ftp") as ftp,
        ):
            recorder.sync_nvr_cleanup(coord)
        loc.assert_called_once_with(coord)
        smb.assert_not_called()
        ftp.assert_called_once_with(coord)

    def test_zero_retention_short_circuits(self, tmp_path: Path):
        coord = _make_coord(tmp_path, retention_days=0)
        with patch.object(recorder, "_sync_nvr_cleanup_local") as loc:
            recorder.sync_nvr_cleanup(coord)
        loc.assert_not_called()


class TestDrainStagingWatcher:
    @pytest.mark.asyncio
    async def test_watcher_runs_tick_then_sleeps(self, tmp_path: Path):
        """One tick on enable_nvr=True; sleep is what gets cancelled."""
        coord = _make_coord(tmp_path, target="local")

        # Provide an awaitable executor stub.
        async def _exec(fn, c):
            return fn(c)

        coord.hass.async_add_executor_job = _exec

        ticks: list[int] = []
        original_tick = recorder.sync_drain_tick

        def counting_tick(coordinator, **kwargs):
            ticks.append(1)
            return original_tick(coordinator, **kwargs)

        with (
            patch.object(recorder, "sync_drain_tick", counting_tick),
            patch.object(recorder, "_DRAIN_TICK_SECONDS", 0.05),
        ):
            task = asyncio.create_task(
                recorder.drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.15)  # let it run a couple of ticks
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert ticks, "watcher never invoked sync_drain_tick"

    @pytest.mark.asyncio
    async def test_watcher_skips_when_nvr_disabled(self, tmp_path: Path):
        coord = _make_coord(tmp_path)
        coord.options["enable_nvr"] = False

        async def _exec(fn, c):
            return fn(c)

        coord.hass.async_add_executor_job = _exec

        with (
            patch.object(recorder, "sync_drain_tick") as tick,
            patch.object(recorder, "_DRAIN_TICK_SECONDS", 0.05),
        ):
            task = asyncio.create_task(
                recorder.drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_watcher_swallows_tick_exception(self, tmp_path: Path):
        """A raising tick must not kill the watcher loop."""
        coord = _make_coord(tmp_path, target="local")

        async def _exec(fn, c):
            return fn(c)

        coord.hass.async_add_executor_job = _exec

        calls = []

        def boom(coordinator, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("simulated")

        with (
            patch.object(recorder, "sync_drain_tick", boom),
            patch.object(recorder, "_DRAIN_TICK_SECONDS", 0.05),
        ):
            task = asyncio.create_task(
                recorder.drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.20)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(calls) >= 2, "watcher exited after first exception"


class TestRemotePathHelpers:
    def test_smb_path_includes_subpath(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )
        path = recorder._remote_smb_path(
            coord.options,
            CAM,
            "2026-05-06",
            "10-00.mp4",
        )
        assert path == r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\2026-05-06\10-00.mp4"

    def test_smb_path_sanitizes_camera_name(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )
        path = recorder._remote_smb_path(
            coord.options,
            "../../etc",
            "2026-05-06",
            "10-00.mp4",
        )
        # ``..`` collapsed by _safe_name → no traversal in the rendered path.
        head_after_root = path.split("\\NVR\\", 1)[1]
        cam_component = head_after_root.split("\\", 1)[0]
        assert ".." not in cam_component

    def test_ftp_path_starts_with_slash(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        path = recorder._remote_ftp_path(
            coord.options,
            CAM,
            "2026-05-06",
            "10-00.mp4",
        )
        assert path == "/Bosch/NVR/Terrasse/2026-05-06/10-00.mp4"


class TestMoveLocal:
    def test_success_returns_true_and_creates_dest(self, tmp_path: Path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="local")
        ok = recorder._move_local(
            coord,
            str(src),
            str(tmp_path),
            CAM,
            "2026-05-06",
            "10-00.mp4",
        )
        assert ok is True
        assert (tmp_path / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert not src.exists()

    def test_oserror_returns_false(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="local")
        with patch.object(recorder.shutil, "move", side_effect=OSError("nope")):
            ok = recorder._move_local(
                coord,
                "/missing/x.mp4",
                str(tmp_path),
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False


class TestUploadSmb:
    def test_returns_false_when_smbprotocol_missing(self, tmp_path: Path):
        """``ImportError`` path — test environment has smbprotocol installed
        but the helper must still tolerate its absence on user systems."""
        coord = _make_coord(tmp_path, target="smb")
        # Make ``import smbclient`` raise ImportError inside the function.
        import sys

        with patch.dict(sys.modules, {"smbclient": None}):
            ok = recorder._upload_smb(
                coord,
                "/fake.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_when_server_empty(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="smb", smb_server="")
        # smbclient is a real module; we only need to short-circuit before it
        # gets used.
        ok = recorder._upload_smb(
            coord,
            "/fake.mp4",
            CAM,
            "2026-05-06",
            "10-00.mp4",
        )
        assert ok is False

    def test_returns_false_on_session_failure(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="smb")
        import sys

        smb_mock = MagicMock(
            register_session=MagicMock(side_effect=OSError("boom")),
            open_file=MagicMock(),
        )
        with patch.dict(sys.modules, {"smbclient": smb_mock}):
            ok = recorder._upload_smb(
                coord,
                "/fake.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_mkdirs_failure(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="smb")
        import sys

        smb_mock = MagicMock(register_session=MagicMock(), open_file=MagicMock())
        with (
            patch.dict(sys.modules, {"smbclient": smb_mock}),
            patch(
                "custom_components.bosch_shc_camera.smb.smb_makedirs",
                side_effect=OSError("mkdir failed"),
            ),
        ):
            ok = recorder._upload_smb(
                coord,
                "/fake.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_upload_open_failure(self, tmp_path: Path):
        """File-open or upload itself raising → caught and returns False."""
        coord = _make_coord(tmp_path, target="smb")
        import sys

        smb_mock = MagicMock(
            register_session=MagicMock(),
            open_file=MagicMock(side_effect=OSError("write failed")),
        )
        with (
            patch.dict(sys.modules, {"smbclient": smb_mock}),
            patch("custom_components.bosch_shc_camera.smb.smb_makedirs"),
        ):
            ok = recorder._upload_smb(
                coord,
                "/missing-file.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False

    def test_happy_path_writes_to_smb(self, tmp_path: Path):
        """Successful write — smbclient.open_file gets the bytes."""
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        import sys
        from io import BytesIO

        smb_dst = BytesIO()
        smb_dst.close = lambda: None  # so the with-block doesn't blow up

        class _OpenFileCtx:
            def __enter__(self_inner):
                return smb_dst

            def __exit__(self_inner, *exc):
                return False

        smb_mock = MagicMock(
            register_session=MagicMock(),
            open_file=MagicMock(return_value=_OpenFileCtx()),
        )
        with (
            patch.dict(sys.modules, {"smbclient": smb_mock}),
            patch("custom_components.bosch_shc_camera.smb.smb_makedirs"),
        ):
            ok = recorder._upload_smb(
                coord,
                str(src),
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is True
        assert smb_dst.getvalue()  # got bytes


class TestUploadFtp:
    def test_returns_false_when_server_empty(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="ftp", smb_server="")
        ok = recorder._upload_ftp(
            coord,
            "/fake.mp4",
            CAM,
            "2026-05-06",
            "10-00.mp4",
        )
        assert ok is False

    def test_returns_false_on_login_failure(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="ftp")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect",
            side_effect=OSError("login refused"),
        ):
            ok = recorder._upload_ftp(
                coord,
                "/fake.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_mkdirs_failure(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
            ),
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_makedirs",
                side_effect=OSError("mkdir refused"),
            ),
        ):
            ok = recorder._upload_ftp(
                coord,
                "/fake.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False

    def test_returns_false_on_storbinary_failure(self, tmp_path: Path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        ftp.storbinary.side_effect = OSError("transfer aborted")
        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
            ),
            patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"),
        ):
            ok = recorder._upload_ftp(
                coord,
                str(src),
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False

    def test_happy_path_calls_storbinary(self, tmp_path: Path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
            ),
            patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"),
        ):
            ok = recorder._upload_ftp(
                coord,
                str(src),
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is True
        ftp.storbinary.assert_called_once()
        # Quit attempt happens in finally block.
        ftp.quit.assert_called_once()

    def test_quit_failure_falls_back_to_close(self, tmp_path: Path):
        """ftp.quit() raising in the finally-block must fall through to close."""
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        ftp.quit.side_effect = OSError("connection broken")
        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
            ),
            patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"),
        ):
            recorder._upload_ftp(
                coord,
                str(src),
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        ftp.close.assert_called_once()


class TestQuarantineFailed:
    def test_moves_file_into_failed_tree(self, tmp_path: Path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        recorder._quarantine_failed(
            str(tmp_path),
            str(src),
            CAM,
            "2026-05-06",
            "10-00.mp4",
        )
        assert (tmp_path / "_failed" / CAM / "2026-05-06" / "10-00.mp4").exists()
        assert not src.exists()

    def test_oserror_swallowed(self, tmp_path: Path):
        """A move-failure must not raise — the watcher is best-effort."""
        with patch.object(
            recorder.shutil, "move", side_effect=OSError("permission denied")
        ):
            recorder._quarantine_failed(
                str(tmp_path),
                "/missing.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )


class TestSyncNvrCleanupLocal:
    def test_skips_when_path_missing(self, tmp_path: Path):
        coord = _make_coord(tmp_path / "doesnotexist", target="local")
        # No raise.
        recorder._sync_nvr_cleanup_local(coord)

    def test_skips_when_zero_retention(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="local", retention_days=0)
        recorder._sync_nvr_cleanup_local(coord)

    def test_deletes_old_files(self, tmp_path: Path):
        # Old file
        old = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x")
        old_mtime = time.time() - 10 * 86400
        os.utime(old, (old_mtime, old_mtime))
        # Recent file
        recent = tmp_path / CAM / "2026-05-06" / "11-00.mp4"
        recent.parent.mkdir(parents=True)
        recent.write_bytes(b"y")

        coord = _make_coord(tmp_path, target="local", retention_days=3)
        recorder._sync_nvr_cleanup_local(coord)
        assert not old.exists()
        assert recent.exists()

    def test_stat_failure_skipped(self, tmp_path: Path):
        """A file that disappears between os.walk and os.stat must not raise."""
        f = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        coord = _make_coord(tmp_path, target="local", retention_days=3)
        with patch("os.stat", side_effect=OSError("vanished")):
            recorder._sync_nvr_cleanup_local(coord)

    def test_remove_failure_swallowed(self, tmp_path: Path):
        """An unlink failure must NOT bubble up — best-effort cleanup."""
        f = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        old_mtime = time.time() - 10 * 86400
        os.utime(f, (old_mtime, old_mtime))
        coord = _make_coord(tmp_path, target="local", retention_days=3)
        real_remove = os.remove

        def selective(p):
            if str(p) == str(f):
                raise OSError("readonly")
            return real_remove(p)

        with patch("os.remove", side_effect=selective):
            recorder._sync_nvr_cleanup_local(coord)
        # File still exists (remove failed silently).
        assert f.exists()

    def test_rmdir_failure_swallowed(self, tmp_path: Path):
        """An rmdir/listdir failure during the empty-folder prune pass must
        not raise — second pass is best-effort."""
        # Create one old file (will be deleted) leaving an empty per-day dir.
        old = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        old.parent.mkdir(parents=True)
        old.write_bytes(b"x")
        old_mtime = time.time() - 10 * 86400
        os.utime(old, (old_mtime, old_mtime))

        coord = _make_coord(tmp_path, target="local", retention_days=3)
        # Patch listdir during the second (rmdir) pass to raise. The first
        # walk call uses os.walk which uses scandir internally — listdir
        # is only used inside the rmdir prune block.
        real_listdir = os.listdir

        def selective(p):
            # Only fail for the per-day dir we just emptied.
            if str(p) == str(old.parent):
                raise OSError("perm")
            return real_listdir(p)

        with patch("os.listdir", side_effect=selective):
            recorder._sync_nvr_cleanup_local(coord)
        # File was still removed successfully in pass 1.
        assert not old.exists()


class TestListStagingExtra:
    def test_listdir_root_oserror(self, tmp_path: Path):
        """os.listdir(staging_root) raising — return empty list."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        with patch("os.listdir", side_effect=OSError("perm")):
            assert recorder._list_staging_candidates(str(staging)) == []

    def test_listdir_cam_oserror(self, tmp_path: Path):
        """os.listdir on the cam-dir raising — skip that camera, continue."""
        staging = tmp_path / "_staging"
        cam = staging / CAM
        cam.mkdir(parents=True)
        # Real listdir on root works (returns ["Terrasse"]); fail only on cam.
        real_listdir = os.listdir

        def selective(p):
            if str(p) == str(cam):
                raise OSError("perm")
            return real_listdir(p)

        with patch("os.listdir", side_effect=selective):
            out = recorder._list_staging_candidates(str(staging))
        assert out == []

    def test_listdir_date_oserror(self, tmp_path: Path):
        """os.listdir on the date-dir raising — skip that date."""
        staging = tmp_path / "_staging"
        date = staging / CAM / "2026-05-06"
        date.mkdir(parents=True)
        real_listdir = os.listdir

        def selective(p):
            if str(p) == str(date):
                raise OSError("perm")
            return real_listdir(p)

        with patch("os.listdir", side_effect=selective):
            out = recorder._list_staging_candidates(str(staging))
        assert out == []

    def test_stat_failure_skipped(self, tmp_path: Path):
        """A file vanishing between os.listdir and os.stat must not raise."""
        seg = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        real_stat = os.stat

        def selective(p, *a, **kw):
            if str(p) == str(seg):
                raise OSError("vanished")
            return real_stat(p, *a, **kw)

        with patch("os.stat", side_effect=selective):
            out = recorder._list_staging_candidates(
                str(tmp_path / "_staging"),
            )
        assert out == []

    def test_non_regular_file_skipped(self, tmp_path: Path):
        """A directory disguised as a file (broken layout) is skipped."""
        staging = tmp_path / "_staging"
        date_dir = staging / CAM / "2026-05-06"
        date_dir.mkdir(parents=True)
        # Make a sub-dir at the file slot.
        bogus = date_dir / "10-00.mp4"
        bogus.mkdir()
        out = recorder._list_staging_candidates(str(staging))
        # The directory entry is not a regular file → skipped.
        assert out == []


class TestSyncDrainTickUnlinkFailure:
    def test_smb_unlink_failure_only_logs(self, tmp_path: Path):
        """A successful upload + failed unlink must NOT bump ``failed``."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with (
            patch.object(recorder, "_upload_smb", return_value=True),
            patch("os.unlink", side_effect=OSError("readonly")),
        ):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 1
        assert result["failed"] == 0

    def test_ftp_unlink_failure_only_logs(self, tmp_path: Path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with (
            patch.object(recorder, "_upload_ftp", return_value=True),
            patch("os.unlink", side_effect=OSError("readonly")),
        ):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 1
        assert result["failed"] == 0

    def test_persistent_notification_swallows_errors(self, tmp_path: Path):
        """If services.async_call raises, the watcher must not crash."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        # Make services.async_call itself blow up to exercise the except.
        coord.hass.loop.call_soon_threadsafe = MagicMock(
            side_effect=RuntimeError("loop is closed"),
        )
        with patch.object(recorder, "_upload_smb", return_value=False):
            for _ in range(recorder._DRAIN_MAX_RETRIES):
                recorder.sync_drain_tick(coord)
        # Quarantine still happened despite notification path failing.
        assert (tmp_path / "_failed" / CAM / "2026-05-06" / "10-00.mp4").exists()


class TestSyncNvrCleanupSmbDeepWalk:
    def test_smb_skipped_when_no_share(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="smb", smb_share="")
        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=MagicMock(),
                    scandir=MagicMock(),
                    remove=MagicMock(),
                    stat=MagicMock(),
                ),
            },
        ):
            recorder._sync_nvr_cleanup_smb(coord)

    def test_smb_session_failure_returns(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="smb")
        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=MagicMock(side_effect=OSError("auth")),
                    scandir=MagicMock(),
                    remove=MagicMock(),
                    stat=MagicMock(),
                ),
            },
        ):
            recorder._sync_nvr_cleanup_smb(coord)

    def test_smb_walk_recurses_and_deletes(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )

        # Build a fake tree with one old file and one fresh file.
        class Entry:
            def __init__(self, name, is_dir):
                self.name = name
                self._is_dir = is_dir

            def is_dir(self):
                return self._is_dir

        old_st = SimpleNamespace(st_mtime=time.time() - 10 * 86400)
        new_st = SimpleNamespace(st_mtime=time.time())

        layout = {
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR": [Entry("Terrasse", True)],
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse": [
                Entry("old.mp4", False),
                Entry("new.mp4", False),
            ],
        }
        stats = {
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\old.mp4": old_st,
            r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\new.mp4": new_st,
        }

        def fake_scandir(path):
            return iter(layout.get(path, []))

        def fake_stat(path):
            return stats[path]

        removed: list[str] = []

        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=MagicMock(),
                    scandir=fake_scandir,
                    remove=removed.append,
                    stat=fake_stat,
                ),
            },
        ):
            recorder._sync_nvr_cleanup_smb(coord)
        assert removed == [r"\\fritz.box\FRITZ.NAS\Bosch\NVR\Terrasse\old.mp4"]

    def test_smb_walk_stops_at_deadline_instead_of_hanging_forever(
        self, tmp_path: Path
    ):
        """A hung/unreachable share must not block the cleanup job forever.

        Simulates a stalled scandir() by advancing a fake monotonic clock
        past _NVR_CLEANUP_MAX_SECONDS on every call — the walk must give up
        and return instead of looping/recursing indefinitely. Regression for
        docs/stream-perf-stability-refactor-plan.md Phase 2 point 9 (recorder.py
        ~1953-2060: `_walk_and_delete` had no per-call deadline).
        """
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )

        class Entry:
            def __init__(self, name, is_dir):
                self.name = name
                self._is_dir = is_dir

            def is_dir(self):
                return self._is_dir

        # An infinitely-deep tree: every directory contains one more
        # subdirectory plus one file. Without a deadline this recurses
        # forever; the test fails via a hang/RecursionError if the deadline
        # check regresses.
        scandir_calls = {"n": 0}

        def fake_scandir(path):
            scandir_calls["n"] += 1
            return iter([Entry("child", True), Entry("leaf.mp4", False)])

        def fake_stat(path):
            return SimpleNamespace(st_mtime=time.time() - 10 * 86400)

        # Fake monotonic clock that's already past the deadline on the very
        # first check, so the walk must stop immediately instead of
        # recursing into the infinite tree.
        fake_now = {"t": 0.0}

        def fake_monotonic():
            fake_now["t"] += recorder._NVR_CLEANUP_MAX_SECONDS + 1.0
            return fake_now["t"]

        with (
            patch.dict(
                "sys.modules",
                {
                    "smbclient": MagicMock(
                        register_session=MagicMock(),
                        scandir=fake_scandir,
                        remove=MagicMock(),
                        stat=fake_stat,
                    ),
                },
            ),
            patch.object(recorder.time, "monotonic", fake_monotonic),
        ):
            recorder._sync_nvr_cleanup_smb(coord)

        # The deadline fires on the very first check (before the initial
        # scandir), so the walk must never have recursed into the tree.
        assert scandir_calls["n"] == 0

    def test_smb_walk_stops_mid_loop_when_deadline_expires_between_entries(
        self, tmp_path: Path
    ):
        """The deadline check ALSO runs inside the per-entry loop (not just
        once at function entry) — a share that responds fine at first but
        stalls partway through a large directory listing must still bail out
        instead of grinding through remaining entries past the deadline.
        Regression for the mid-loop check at recorder.py's
        `_walk_and_delete` (SMB), distinct from the top-of-function check
        already covered by test_smb_walk_stops_at_deadline_instead_of_hanging_forever.
        """
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )

        class Entry:
            def __init__(self, name):
                self.name = name

            def is_dir(self):
                return False

        root = r"\\fritz.box\FRITZ.NAS\Bosch\NVR"

        def fake_scandir(path):
            assert path == root
            return iter([Entry("a.mp4"), Entry("b.mp4")])

        removed: list[str] = []

        def fake_stat(path):
            return SimpleNamespace(st_mtime=time.time() - 10 * 86400)

        # monotonic() call order: (1) deadline = now + MAX, (2) top-of-function
        # check for root — both still under deadline, (3) per-entry check for
        # "a.mp4" — still under deadline, entry is processed, (4) per-entry
        # check for "b.mp4" — now past deadline, must stop before processing it.
        calls = iter([0.0, 1.0, 2.0, recorder._NVR_CLEANUP_MAX_SECONDS + 1.0])

        def fake_monotonic():
            return next(calls, recorder._NVR_CLEANUP_MAX_SECONDS + 1.0)

        with (
            patch.dict(
                "sys.modules",
                {
                    "smbclient": MagicMock(
                        register_session=MagicMock(),
                        scandir=fake_scandir,
                        remove=removed.append,
                        stat=fake_stat,
                    ),
                },
            ),
            patch.object(recorder.time, "monotonic", fake_monotonic),
        ):
            recorder._sync_nvr_cleanup_smb(coord)

        assert removed == [f"{root}\\a.mp4"]

    def test_smb_scandir_exception_swallowed(self, tmp_path: Path):
        """A scandir failure deep in the tree must not propagate."""
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )
        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=MagicMock(),
                    scandir=MagicMock(side_effect=OSError("scandir failed")),
                    remove=MagicMock(),
                    stat=MagicMock(),
                ),
            },
        ):
            # Should not raise.
            recorder._sync_nvr_cleanup_smb(coord)

    def test_smb_stat_exception_swallowed(self, tmp_path: Path):
        """smb_stat raising on a leaf file must not bubble up."""
        coord = _make_coord(
            tmp_path, target="smb", smb_base_path="Bosch", smb_subpath="NVR"
        )

        class Entry:
            name = "boom.mp4"

            def is_dir(self):
                return False

        def fake_scandir(path):
            if path.endswith("\\NVR"):
                return iter([Entry()])
            return iter([])

        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=MagicMock(),
                    scandir=fake_scandir,
                    remove=MagicMock(),
                    stat=MagicMock(side_effect=OSError("stat failed")),
                ),
            },
        ):
            recorder._sync_nvr_cleanup_smb(coord)


class TestSyncNvrCleanupFtpDeepWalk:
    def test_ftp_walk_lists_and_deletes(self, tmp_path: Path):
        """End-to-end walk: cwd → LIST → MDTM → DELE for old files only."""
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        # Build a LIST output: one subdir, one old file, one fresh file.
        listings = {
            "/Bosch/NVR": [
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 Terrasse",
            ],
            "/Bosch/NVR/Terrasse": [
                "-rw-r--r--  1 user grp  1024 Apr 01 10:00 old.mp4",
                "-rw-r--r--  1 user grp  1024 May 09 10:00 new.mp4",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines

        # MDTM responses — new.mp4 uses current time so the test never drifts
        # past the retention boundary as calendar days advance.
        # B13-6: MDTM and DELETE must use absolute paths (position-independent).
        def sendcmd(cmd):
            import datetime

            if cmd == "MDTM /Bosch/NVR/Terrasse/old.mp4":
                return "213 20260101010000"
            return datetime.datetime.utcnow().strftime("213 %Y%m%d%H%M%S")

        ftp.sendcmd.side_effect = sendcmd
        ftp.cwd.return_value = None
        ftp.delete.return_value = None

        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)
        # B13-6 regression pin: delete must use the absolute path, not just the filename.
        ftp.delete.assert_called_once_with("/Bosch/NVR/Terrasse/old.mp4")

    def test_ftp_walk_stops_at_deadline_instead_of_hanging_forever(
        self, tmp_path: Path
    ):
        """A hung/unreachable FTP server must not block cleanup forever.

        Mirrors the SMB deadline test: fake_monotonic is already past
        _NVR_CLEANUP_MAX_SECONDS on the first check inside _walk_and_delete,
        so ftp.cwd() (the first blocking call per directory) must never be
        reached. Regression for docs/stream-perf-stability-refactor-plan.md
        Phase 2 point 9.
        """
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()

        # Fake monotonic clock that jumps past the deadline between the
        # initial `deadline = time.monotonic() + _NVR_CLEANUP_MAX_SECONDS`
        # call and the very first in-walk check.
        fake_now = {"t": 0.0}

        def fake_monotonic():
            fake_now["t"] += recorder._NVR_CLEANUP_MAX_SECONDS + 1.0
            return fake_now["t"]

        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect",
                return_value=ftp,
            ),
            patch.object(recorder.time, "monotonic", fake_monotonic),
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

        ftp.cwd.assert_not_called()
        ftp.delete.assert_not_called()

    def test_ftp_walk_stops_mid_loop_when_deadline_expires_between_files(
        self, tmp_path: Path
    ):
        """Mirrors test_smb_walk_stops_mid_loop_when_deadline_expires_between_entries
        for the FTP `_walk_and_delete`'s per-file deadline check (distinct
        from the top-of-function check already covered by
        test_ftp_walk_stops_at_deadline_instead_of_hanging_forever): a
        server that lists fine but stalls partway through MDTM/DELETE calls
        for a large file list must still bail out.
        """
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()

        listing = [
            "-rw-r--r-- 1 owner group 100 Jan 01 00:00 a.mp4",
            "-rw-r--r-- 1 owner group 100 Jan 01 00:00 b.mp4",
        ]

        def fake_retrlines(cmd, cb):
            for line in listing:
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        ftp.cwd.return_value = None
        ftp.sendcmd.return_value = "213 20260101010000"

        # monotonic() call order: (1) deadline = now + MAX, (2) top-of-function
        # check for the root path — both under deadline, (3) per-file check for
        # "a.mp4" — still under deadline, MDTM+DELETE proceed, (4) per-file
        # check for "b.mp4" — now past deadline, must stop before it.
        calls = iter([0.0, 1.0, 2.0, recorder._NVR_CLEANUP_MAX_SECONDS + 1.0])

        def fake_monotonic():
            return next(calls, recorder._NVR_CLEANUP_MAX_SECONDS + 1.0)

        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect",
                return_value=ftp,
            ),
            patch.object(recorder.time, "monotonic", fake_monotonic),
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

        ftp.delete.assert_called_once_with("/Bosch/NVR/a.mp4")

    def test_ftp_cwd_failure_returns_cleanly(self, tmp_path: Path):
        """ftp.cwd raising error_perm — entire walk returns early w/o delete."""
        import ftplib

        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        ftp.cwd.side_effect = ftplib.error_perm("550 not found")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)
        ftp.delete.assert_not_called()

    def test_ftp_listing_exception_swallowed(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        ftp.retrlines.side_effect = OSError("listing failed")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_mdtm_failure_skips_file(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "-rw-r--r--  1 user grp 1024 Apr 01 10:00 weird.mp4",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        ftp.sendcmd.side_effect = OSError("MDTM unsupported")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)
        ftp.delete.assert_not_called()

    def test_ftp_delete_failure_swallowed(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "-rw-r--r--  1 user grp 1024 Apr 01 10:00 old.mp4",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        ftp.sendcmd.return_value = "213 20260101010000"
        ftp.delete.side_effect = OSError("permission denied")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_cwd_in_recursion_swallowed(self, tmp_path: Path):
        """``cwd`` failure when popping back up the tree must not raise."""
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 Terrasse",
            ],
            "/Bosch/NVR/Terrasse": [],
        }

        cwd_call_count = {"n": 0}

        def cwd(p):
            cwd_call_count["n"] += 1
            # First call (entering /Bosch/NVR): ok.
            # Second (entering Terrasse): ok.
            # Third (cwd back to /Bosch/NVR): raise.
            if cwd_call_count["n"] >= 3:
                raise OSError("cwd back failed")

        ftp.cwd.side_effect = cwd

        def fake_retrlines(cmd, cb):
            # Determine current path from the latest cwd call.
            args, _ = ftp.cwd.call_args
            current = args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_quit_failure_swallowed(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        ftp.retrlines.side_effect = lambda cmd, cb: None  # empty
        ftp.quit.side_effect = OSError("connection lost")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_mdtm_and_delete_use_absolute_paths(self, tmp_path: Path):
        """B13-6 regression: MDTM and DELETE must use absolute paths so that
        the commands are position-independent after recursive _walk_and_delete
        calls leave the FTP cwd pointing at a subdirectory."""
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        # Single file in the root NVR dir — old enough to be deleted.
        listings = {
            "/Bosch/NVR": [
                "-rw-r--r--  1 user grp 1024 Apr 01 10:00 clip.mp4",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        # Return an old timestamp for any MDTM call.
        ftp.sendcmd.return_value = "213 20260101010000"
        ftp.cwd.return_value = None
        ftp.delete.return_value = None

        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

        # MDTM must have been called with the absolute path, not just "clip.mp4".
        mdtm_calls = [str(c) for c in ftp.sendcmd.call_args_list]
        assert any("/Bosch/NVR/clip.mp4" in c for c in mdtm_calls), (
            f"MDTM must use absolute path; calls were: {mdtm_calls}"
        )
        # DELETE must also use the absolute path.
        ftp.delete.assert_called_once_with("/Bosch/NVR/clip.mp4")


class TestUploadFtpCloseFallback:
    def test_quit_and_close_both_fail(self, tmp_path: Path):
        src = _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        ftp = MagicMock()
        ftp.quit.side_effect = OSError("a")
        ftp.close.side_effect = OSError("b")
        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
            ),
            patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"),
        ):
            # Exception from close() in the inner finally must not propagate.
            recorder._upload_ftp(
                coord,
                str(src),
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )


class TestUploadSmbServerEmptyWarning:
    def test_warning_logged_no_session(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """``smb_server`` empty must short-circuit (no register_session call)."""
        coord = _make_coord(tmp_path, target="smb", smb_server="")
        # Provide a real-ish smbclient that would crash if invoked — proves
        # the helper short-circuits before importing it.
        register = MagicMock()
        with patch.dict(
            "sys.modules",
            {
                "smbclient": MagicMock(
                    register_session=register, open_file=MagicMock()
                ),
            },
        ):
            ok = recorder._upload_smb(
                coord,
                "/x.mp4",
                CAM,
                "2026-05-06",
                "10-00.mp4",
            )
        assert ok is False
        register.assert_not_called()


class TestSmbCleanupImportErrorBranch:
    def test_smbclient_missing_returns_silently(self, tmp_path: Path):
        """Production environments without smbprotocol must not raise."""
        import builtins
        import sys

        coord = _make_coord(tmp_path, target="smb")
        real_import = builtins.__import__

        def selective(name, *a, **kw):
            if name == "smbclient":
                raise ImportError("smbprotocol not installed")
            return real_import(name, *a, **kw)

        # Pop any cached smbclient first so the import path runs fresh.
        sys.modules.pop("smbclient", None)
        with patch("builtins.__import__", side_effect=selective):
            recorder._sync_nvr_cleanup_smb(coord)


class TestFtpCleanupConnectFailure:
    def test_connect_failure_returns_silently(self, tmp_path: Path):
        coord = _make_coord(tmp_path, target="ftp")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect",
            side_effect=OSError("login refused"),
        ):
            recorder._sync_nvr_cleanup_ftp(coord)


class TestFtpCleanupShortAndDotDotLines:
    def test_short_line_skipped(self, tmp_path: Path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        listings = {
            "/Bosch/NVR": [
                "short line",  # too few fields → skipped (line 896-897)
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 .",  # dot → skipped (line 899-900)
                "drwxr-xr-x  2 user grp 4096 May 06 10:00 ..",
            ],
        }

        def fake_retrlines(cmd, cb):
            current = ftp.cwd.call_args.args[0]
            for line in listings.get(current, []):
                cb(line)

        ftp.retrlines.side_effect = fake_retrlines
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)
        ftp.delete.assert_not_called()


class TestPersistentNotificationScheduling:
    """D-P2 regression: verify the correct scheduling primitive is used in
    each caller context.

    * ``_watch_recorder`` (async def, runs on the event loop):
      must call ``hass.async_create_task`` DIRECTLY — never via
      ``hass.loop.call_soon_threadsafe``.  Eager-creating a coroutine and
      passing it through ``call_soon_threadsafe(async_create_task, coro)``
      from inside an async function is unnecessary and could produce a
      "coroutine was never awaited" warning if the outer except fires first.

    * ``sync_drain_tick`` (plain def, runs in executor thread):
      must use ``hass.loop.call_soon_threadsafe`` to cross from the thread
      back onto the event loop — direct ``async_create_task`` is not
      thread-safe.
    """

    @pytest.mark.asyncio
    async def test_watch_recorder_diskfull_uses_async_create_task_not_threadsafe(
        self,
    ) -> None:
        """Disk-full branch in _watch_recorder must call async_create_task
        directly (already on loop), never call_soon_threadsafe."""
        cam_id = "AABBCCDD-0000-0000-0000-000000000000"

        # Stub coordinator with the minimal fields _watch_recorder reads.
        create_task = MagicMock()
        call_soon_threadsafe = MagicMock()
        # MagicMock (not AsyncMock): the coroutine goes to create_task which
        # is also a MagicMock — it would never be awaited and would produce a
        # "coroutine was never awaited" RuntimeWarning.  We only care that
        # async_create_task was called, not that the coro was actually run.
        services_async_call = MagicMock()

        coord = SimpleNamespace(
            nvr_processes={},
            nvr_user_intent={cam_id: True},
            nvr_recent_crash={},
            nvr_error_state={},
            data={
                cam_id: {
                    "info": {"title": "Terrasse"},
                    "status": "ONLINE",
                }
            },
            options={"nvr_base_path": "/tmp/nvr_test", "enable_nvr": True},
            is_camera_online=lambda cid: True,
            async_update_listeners=MagicMock(),
            live_connections={
                cam_id: {
                    "_connection_type": "LOCAL",
                    "rtspsUrl": "rtsp://u:p@127.0.0.1:9999/rtsp_tunnel?inst=1",
                }
            },
            hass=SimpleNamespace(
                async_create_task=create_task,
                loop=SimpleNamespace(call_soon_threadsafe=call_soon_threadsafe),
                services=SimpleNamespace(async_call=services_async_call),
            ),
        )

        # Proc registered so the guard in _watch_recorder doesn't exit early.
        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"no space left on device")

        async def _wait() -> int:
            proc.returncode = 1
            return 1

        proc.wait = _wait
        coord.nvr_processes[cam_id] = proc

        await recorder._watch_recorder(coord, cam_id, proc, _tail_for(proc))

        # async_create_task MUST have been called (notification scheduled).
        create_task.assert_called_once()
        # call_soon_threadsafe must NOT be used — we're already on the loop.
        call_soon_threadsafe.assert_not_called()
        assert coord.nvr_error_state.get(cam_id) == "disk full"

    @pytest.mark.asyncio
    async def test_watch_recorder_diskfull_swallows_async_create_task_error(
        self,
    ) -> None:
        """If async_create_task raises (e.g. loop shutting down), the disk-full
        branch must still set error_state and return — no unhandled exception."""
        cam_id = "AABBCCDD-0000-0000-0000-000000000001"

        coord = SimpleNamespace(
            nvr_processes={},
            nvr_user_intent={cam_id: True},
            nvr_recent_crash={},
            nvr_error_state={},
            data={cam_id: {"info": {"title": "Cam"}, "status": "ONLINE"}},
            options={"nvr_base_path": "/tmp/nvr_test", "enable_nvr": True},
            is_camera_online=lambda cid: True,
            async_update_listeners=MagicMock(),
            live_connections={
                cam_id: {
                    "_connection_type": "LOCAL",
                    "rtspsUrl": "rtsp://u:p@127.0.0.1:9999/rtsp_tunnel?inst=1",
                }
            },
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=RuntimeError("loop closed")),
                loop=SimpleNamespace(call_soon_threadsafe=MagicMock()),
                # MagicMock (not AsyncMock) so no coroutine object is created;
                # async_create_task raises before it could consume the return
                # value anyway — using AsyncMock would produce a "never awaited"
                # warning here because the coroutine never reaches the task loop.
                services=SimpleNamespace(async_call=MagicMock()),
            ),
        )

        proc = MagicMock()
        proc.returncode = 1
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"enospc")

        async def _wait() -> int:
            proc.returncode = 1
            return 1

        proc.wait = _wait
        coord.nvr_processes[cam_id] = proc

        # Must not raise even though async_create_task blows up.
        await recorder._watch_recorder(coord, cam_id, proc, _tail_for(proc))

        assert coord.nvr_error_state.get(cam_id) == "disk full"

    def test_drain_tick_quarantine_uses_call_soon_threadsafe(
        self, tmp_path: Path
    ) -> None:
        """sync_drain_tick runs in an executor thread; its persistent-
        notification call MUST go through call_soon_threadsafe (not direct
        async_create_task which is not thread-safe)."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")

        call_soon_threadsafe = MagicMock()
        create_task = MagicMock()
        coord.hass.loop.call_soon_threadsafe = call_soon_threadsafe
        coord.hass.async_create_task = create_task

        with patch.object(recorder, "_upload_smb", return_value=False):
            for _ in range(recorder._DRAIN_MAX_RETRIES):
                recorder.sync_drain_tick(coord)

        # Thread path must use call_soon_threadsafe.
        call_soon_threadsafe.assert_called()
        # Quarantine happened.
        assert (tmp_path / "_failed" / CAM / "2026-05-06" / "10-00.mp4").exists()


# `nvr_recent_crash` SENTINEL_RULE default (relocated from
# tests/test_bug_regression_v11.py)


class TestNvrRecentCrashSentinel:
    """`nvr_recent_crash.get(cam_id, ...)` must default to float('-inf')
    (SENTINEL_RULE). On CI VMs where time.monotonic() < _RESPAWN_WINDOW_SECONDS,
    a 0.0 default makes the FIRST crash look like a second crash within the
    window, permanently suppressing respawn."""

    def test_first_crash_default_is_not_zero(self):
        import inspect

        from custom_components.bosch_shc_camera import recorder

        src = inspect.getsource(recorder)
        assert "nvr_recent_crash.get(cam_id, 0.0)" not in src, (
            "recorder.py must not use 0.0 as default for nvr_recent_crash; "
            "on CI VMs with low monotonic, first crash triggers false crash-loop detection"
        )

    def test_first_crash_uses_neginf_default(self):
        import inspect

        from custom_components.bosch_shc_camera import recorder

        src = inspect.getsource(recorder)
        assert_in_source(src, 'nvr_recent_crash.get(cam_id, float("-inf"))')

    def test_first_crash_does_not_suppress_respawn_at_low_monotonic(self):
        """With monotonic=30s and _RESPAWN_WINDOW_SECONDS=60s, the first
        crash must not suppress respawn.

        Before the fix: prev_crash=0.0 → (30 - 0.0) = 30 < 60 → crash-loop
        guard fires → respawn suppressed on the FIRST crash. After the fix:
        prev_crash=float('-inf') → (30 - (-inf)) = inf >= 60 → respawn allowed.
        """
        from custom_components.bosch_shc_camera.recorder import _RESPAWN_WINDOW_SECONDS

        RESPAWN_WINDOW = _RESPAWN_WINDOW_SECONDS
        low_monotonic_now = RESPAWN_WINDOW * 0.5

        prev_crash_old_default = 0.0
        prev_crash_new_default = float("-inf")

        old_behavior = (low_monotonic_now - prev_crash_old_default) < RESPAWN_WINDOW
        new_behavior = (low_monotonic_now - prev_crash_new_default) < RESPAWN_WINDOW

        assert old_behavior is True, (
            "0.0 default causes crash-loop guard to fire at low monotonic"
        )
        assert new_behavior is False, (
            "float('-inf') default must not trigger crash-loop guard on first crash"
        )


# issue #42 follow-up — cred-rotation race root-cause fix
# (late re-read + shared lock with refresh_local_creds_from_heartbeat) and
# bounded auth-retry so a genuine broken credential surfaces instead of
# retrying forever.


class TestStartRecorderCredRotationRace:
    @pytest.mark.asyncio
    async def test_late_rotation_uses_fresh_url_not_stale_capture(self, tmp_path: Path):
        """A heartbeat cred rotation landing between the makedirs executor
        job and the ffmpeg spawn must NOT result in ffmpeg being launched
        with the stale, already-invalidated URL captured earlier in
        start_recorder — it must re-read live_connections one more time
        right before spawning."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        stale_url = coord.live_connections[CAM_ID]["rtspsUrl"]
        fresh_url = "rtsp://newuser:newpass@127.0.0.1:46597/rtsp_tunnel?inst=1"

        async def _rotating_executor(fn, *args, **kwargs):
            # Simulate refresh_local_creds_from_heartbeat firing while
            # start_recorder is awaiting the staging-dir makedirs job.
            if fn is os.makedirs:
                coord.live_connections[CAM_ID]["rtspsUrl"] = fresh_url
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _rotating_executor

        proc = _mock_proc(returncode=None)
        captured_args = []

        async def _spawn(*args, **kwargs):
            captured_args.append(args)
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)

        assert captured_args, "ffmpeg was never spawned"
        argv = captured_args[0]
        assert not any(stale_url in a for a in argv if isinstance(a, str)), (
            "ffmpeg must not be spawned with the stale, already-rotated URL"
        )
        assert any("newuser:newpass" in a for a in argv if isinstance(a, str)), (
            "ffmpeg must be spawned with the freshly-rotated creds"
        )

    @pytest.mark.asyncio
    async def test_torn_down_mid_makedirs_aborts_spawn(self, tmp_path: Path):
        """If the LOCAL session is torn down (e.g. LOCAL→REMOTE fallback)
        while start_recorder awaits the makedirs job, the final re-read
        under the lock must detect this and abort rather than spawn ffmpeg
        against a stream that no longer exists."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))

        async def _tearing_down_executor(fn, *args, **kwargs):
            if fn is os.makedirs:
                coord.live_connections[CAM_ID]["_connection_type"] = "REMOTE"
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _tearing_down_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord.nvr_processes

    @pytest.mark.asyncio
    async def test_spawn_serializes_against_heartbeat_lock(self, tmp_path: Path):
        """start_recorder's final re-read+spawn must run under the SAME
        per-camera lock instance refresh_local_creds_from_heartbeat uses,
        so the two can never interleave mid-mutation."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)
        seen_locked_during_spawn = []

        async def _spawn(*args, **kwargs):
            seen_locked_during_spawn.append(
                coord.get_nvr_recorder_lock(CAM_ID).locked()
            )
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)

        assert seen_locked_during_spawn == [True], (
            "ffmpeg spawn must happen while holding the per-camera NVR recorder lock"
        )
        # Lock must be released again once start_recorder returns.
        assert not coord.get_nvr_recorder_lock(CAM_ID).locked()

    @pytest.mark.asyncio
    async def test_concurrent_start_recorder_calls_never_double_spawn(
        self, tmp_path: Path
    ):
        """GitHub #49 secondary finding (realKim-dotcom, 2026-07-15,
        pre-existing on v15.0.2 AND v16.0.0 -- not a v16 regression): two
        concurrent start_recorder calls for the SAME camera (e.g. a switch
        toggle racing a coordinator-tick auto-heal) must never both spawn
        a live ffmpeg process writing to the same staging segment file.

        Before the fix, only the tail-end spawn was lock-protected -- the
        leading `stop_recorder` call ran unlocked, so two callers could
        each pass it, then each independently (serially, not exclusively
        against each other's decision to spawn) acquire the lock and spawn
        their own ffmpeg, leaving two live processes both writing the same
        %H-%M.mp4 segment (confirmed via /proc/PID/fd in the report).

        With the fix (this function's entire body -- stop AND spawn -- runs
        under one lock acquisition), the second caller's own leading
        stop_recorder call runs AFTER the first caller's spawn has already
        completed and released the lock, so it correctly finds and
        terminates the first caller's process before spawning its own.
        Net result: exactly one live process at the end, and the first
        caller's process was actually torn down (SIGTERM sent), not
        silently orphaned.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))

        spawned_procs = []

        def _make_new_proc():
            proc = MagicMock()
            proc.returncode = None
            proc.wait = AsyncMock(return_value=0)
            proc.pid = 1000 + len(spawned_procs)
            proc.stderr = MagicMock()
            proc.stderr.read = AsyncMock(return_value=b"")
            spawned_procs.append(proc)
            return proc

        async def _spawn(*args, **kwargs):
            # Yield control so the two concurrent start_recorder calls
            # genuinely have a chance to interleave around this point,
            # not just run back-to-back synchronously.
            await asyncio.sleep(0)
            return _make_new_proc()

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await asyncio.gather(
                recorder.start_recorder(coord, CAM_ID),
                recorder.start_recorder(coord, CAM_ID),
            )

        assert len(spawned_procs) == 2, (
            "expected both calls to eventually spawn (one respawns after "
            "the other), not that only one call attempted a spawn at all"
        )
        # The critical property: only ONE process is left tracked as live —
        # the other must have been torn down (SIGTERM), not orphaned as a
        # second, untracked ffmpeg still writing to the same segment file.
        assert coord.nvr_processes[CAM_ID] is spawned_procs[-1], (
            "the coordinator must track only the LATEST process"
        )
        first_proc = spawned_procs[0]
        assert first_proc.wait.await_count >= 1 or first_proc.send_signal.called, (
            "the first spawned process must have been actively stopped "
            "(SIGTERM + wait), not left running untracked in the background"
        )


class TestWatchRecorderBoundedAuthRetry:
    @pytest.mark.asyncio
    async def test_retries_up_to_cap_then_gives_up(self, tmp_path: Path):
        """6 consecutive 401 exits must retry exactly 5 times (per
        _MAX_CONSECUTIVE_AUTH_RETRIES) and then give up with a distinct
        error message — a genuine broken credential must not retry
        forever and silently hide the fault from the user.

        Regression: this must exercise the REAL `start_recorder` (only
        `asyncio.create_subprocess_exec` mocked), not a fake stand-in —
        the bug this guards against was `start_recorder`'s own
        auth-retry respawn resetting `nvr_auth_retry_count` before the
        next 401 could ever accumulate past 1, which a fake respawn that
        skips `start_recorder` entirely cannot catch."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        spawn_count = [0]

        async def _spawn(*args, **kwargs):
            spawn_count[0] += 1
            # Every spawned ffmpeg immediately 401s (genuinely broken cred).
            return _mock_proc(
                returncode=8,
                stderr_data=b"method OPTIONS failed: 401 (Unauthorized)",
            )

        first_proc = _mock_proc(
            returncode=8,
            stderr_data=b"method OPTIONS failed: 401 (Unauthorized)",
        )
        coord.nvr_processes[CAM_ID] = first_proc

        with (
            patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn),
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            # _make_lifecycle_coord's async_create_background_task stub
            # just closes the scheduled watcher coroutine instead of
            # running it, so drive the respawn chain explicitly here —
            # each iteration is what the (unrun) background task would
            # have done on its own.
            proc = first_proc
            for _ in range(recorder._MAX_CONSECUTIVE_AUTH_RETRIES + 1):
                await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))
                proc = coord.nvr_processes.get(CAM_ID)
                if proc is None:
                    break  # gave up

        assert spawn_count[0] == recorder._MAX_CONSECUTIVE_AUTH_RETRIES, (
            "must respawn via the REAL start_recorder exactly "
            "_MAX_CONSECUTIVE_AUTH_RETRIES times, not loop forever"
        )
        assert coord.nvr_auth_retry_count[CAM_ID] == (
            recorder._MAX_CONSECUTIVE_AUTH_RETRIES + 1
        )
        assert "repeated auth failures" in coord.nvr_error_state.get(CAM_ID, "")
        assert CAM_ID not in coord.nvr_processes, (
            "must give up with no process registered, not keep respawning"
        )

    @pytest.mark.asyncio
    async def test_single_auth_failure_does_not_give_up(self):
        """A lone 401 (the common transient-race case) must retry without
        touching nvr_error_state — the bounded cap must not make the
        common case worse."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(
            returncode=8, stderr_data=b"method OPTIONS failed: 401 (Unauthorized)"
        )
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        restart.assert_awaited_once_with(coord, CAM_ID, is_auto_retry=True)
        assert CAM_ID not in coord.nvr_error_state
        assert coord.nvr_auth_retry_count[CAM_ID] == 1

    @pytest.mark.asyncio
    async def test_auth_retry_counter_reset_on_successful_spawn(self, tmp_path: Path):
        """After one 401 retry, a later successful spawn must clear the
        auth-retry counter — a later isolated 401 must not inherit the
        prior streak toward the give-up cap."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.nvr_auth_retry_count[CAM_ID] = recorder._MAX_CONSECUTIVE_AUTH_RETRIES
        proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)

        assert CAM_ID not in coord.nvr_auth_retry_count


class TestNvrStateChangePushesImmediateUpdate:
    """Issue #42 follow-up (realKim-dotcom, 2026-07-10): the `mini_nvr_state`
    sensor reads `nvr_processes`/`nvr_error_state` live (no I/O), but
    nothing told HA to re-render those entities when those dicts changed —
    so the sensor only refreshed on the next ~60s coordinator tick, lagging
    up to 20s behind a real start and 1-2 min behind a real stop. Every
    recorder-lifecycle transition must now call
    `coordinator.async_update_listeners()` immediately."""

    @pytest.mark.asyncio
    async def test_successful_spawn_pushes_update(self, tmp_path: Path):
        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)

        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_stop_recorder_pushes_update_when_process_was_running(
        self, tmp_path: Path
    ):
        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.nvr_processes[CAM_ID] = _mock_proc(returncode=None)

        await recorder.stop_recorder(coord, CAM_ID)

        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_stop_recorder_no_push_when_nothing_was_running(self, tmp_path: Path):
        """No process registered → nothing actually changed → no spurious push."""
        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        assert CAM_ID not in coord.nvr_processes

        await recorder.stop_recorder(coord, CAM_ID)

        coord.async_update_listeners.assert_not_called()

    @pytest.mark.asyncio
    async def test_unexpected_crash_pushes_update(self, tmp_path: Path):
        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=1)
        coord.nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()),
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_disk_full_give_up_pushes_update(self, tmp_path: Path):
        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(
            returncode=1, stderr_data=b"Error writing trailer: No space left on device"
        )
        coord.nvr_processes[CAM_ID] = proc

        await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        assert coord.nvr_error_state[CAM_ID] == "disk full"
        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_auth_retry_give_up_pushes_update(self, tmp_path: Path):
        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.nvr_auth_retry_count[CAM_ID] = recorder._MAX_CONSECUTIVE_AUTH_RETRIES
        proc = _mock_proc(
            returncode=8, stderr_data=b"method OPTIONS failed: 401 (Unauthorized)"
        )
        coord.nvr_processes[CAM_ID] = proc

        await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        assert "repeated auth failures" in coord.nvr_error_state[CAM_ID]
        coord.async_update_listeners.assert_called()

    @pytest.mark.asyncio
    async def test_crash_twice_give_up_pushes_update(self, tmp_path: Path):
        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.nvr_recent_crash[CAM_ID] = time.monotonic()
        proc = _mock_proc(returncode=1, stderr_data=b"some other ffmpeg error")
        coord.nvr_processes[CAM_ID] = proc

        with patch.object(asyncio, "sleep", new=AsyncMock()):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))

        assert coord.nvr_error_state[CAM_ID] == "ffmpeg crashed twice"
        coord.async_update_listeners.assert_called()


# Phase 5 — post-roll capture + event→clip assembly (issue #43)


def _write_ring_segment(
    cache_dir: str, cam_title: str, hhmmss: str, mtime: float, *, size: int = 2048
) -> str:
    """Write a fake pre-roll ring segment file with an explicit mtime —
    simulates the ring writer producing a new segment during a test's fake
    `asyncio.sleep`."""
    cam_dir = recorder._preroll_dir(cache_dir, cam_title)
    os.makedirs(cam_dir, exist_ok=True)
    path = os.path.join(cam_dir, f"{hhmmss}.mp4")
    with open(path, "wb") as f:
        f.write(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def _make_assembly_coord(
    tmp_path,
    *,
    postroll_seconds: int = 0,
    finalize_ring_on_event: bool = False,
    event_clip_enabled: bool = True,
    ring_running: bool = True,
):
    """Stub coordinator for `assemble_and_ship_motion_clip` tests.

    ``ring_running`` (GitHub #54 follow-up): the post-roll tail is now
    derived from the pre-roll ring's own `nvr_preroll_processes` entry —
    False simulates `nvr_preroll_seconds=0` (no ring spawned at all).
    """

    async def _run_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": CAM_TITLE}, "status": "ONLINE"}},
        options={
            "nvr_base_path": str(tmp_path / "nvr"),
            "nvr_preroll_cache_dir": str(tmp_path / "cache"),
            "nvr_preroll_seconds": 30,
            "nvr_postroll_seconds": postroll_seconds,
            "nvr_finalize_ring_on_event": finalize_ring_on_event,
        },
        live_connections={
            CAM_ID: {
                "_connection_type": "LOCAL",
                "rtspsUrl": "rtsp://127.0.0.1:9000/x",
            }
        },
        _nvr_clip_assembly_locks={},
        _nvr_event_clip_enabled={CAM_ID: event_clip_enabled},
        _nvr_recorder_locks={},
        nvr_preroll_processes=({CAM_ID: MagicMock()} if ring_running else {}),
    )
    coord.hass = SimpleNamespace(async_add_executor_job=_run_executor)

    def _get_lock(cam_id: str) -> asyncio.Lock:
        lock = coord._nvr_clip_assembly_locks.get(cam_id)
        if lock is None:
            lock = asyncio.Lock()
            coord._nvr_clip_assembly_locks[cam_id] = lock
        return lock

    coord.get_nvr_clip_assembly_lock = _get_lock
    coord.get_nvr_event_clip_enabled = lambda cam_id: coord._nvr_event_clip_enabled.get(
        cam_id, True
    )

    def _get_recorder_lock(cam_id: str) -> asyncio.Lock:
        lock = coord._nvr_recorder_locks.get(cam_id)
        if lock is None:
            lock = asyncio.Lock()
            coord._nvr_recorder_locks[cam_id] = lock
        return lock

    coord.get_nvr_recorder_lock = _get_recorder_lock
    return coord


class TestAssembleAndShipMotionClip:
    """Orchestrator wiring FCM events -> create_motion_clip -> NVR staging."""

    @pytest.mark.asyncio
    async def test_lock_already_held_skips(self, tmp_path: Path):
        """A concurrent assembly in progress for the same camera → skip,
        don't queue (issue #43 follow-up: bursty motion events must not
        pile up overlapping ffmpeg concats)."""
        coord = _make_assembly_coord(tmp_path)
        lock = coord.get_nvr_clip_assembly_lock(CAM_ID)
        await lock.acquire()
        try:
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)
        finally:
            lock.release()
        assert result is False

    @pytest.mark.asyncio
    async def test_staging_dir_makedirs_oserror_returns_false(self, tmp_path: Path):
        """Cannot create the staging dest dir — must return False rather
        than leaving a partial clip behind or crashing."""
        coord = _make_assembly_coord(tmp_path, postroll_seconds=5)

        _real_makedirs = os.makedirs
        staging_root = str(tmp_path / "nvr")

        def _makedirs(path, *args, **kwargs):
            if path.startswith(staging_root):
                raise OSError("disk full")
            return _real_makedirs(path, *args, **kwargs)

        async def _fake_sleep(_seconds):
            return None

        with (
            patch.object(recorder, "list_preroll_files", return_value=[]),
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch("os.makedirs", side_effect=_makedirs),
        ):
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is False

    @pytest.mark.asyncio
    async def test_preroll_only_writes_into_staging_tree(self, tmp_path: Path):
        """No post-roll configured: assembled clip lands under
        {base}/_staging/{cam}/{date}/HH-MM-SS_motion.mp4 — the exact tree
        the existing drain watcher already scans."""
        coord = _make_assembly_coord(tmp_path, postroll_seconds=0)

        with patch.object(
            recorder,
            "list_preroll_files",
            return_value=[str(tmp_path / "pre0.mp4")],
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)

            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0

            # Mocked ffmpeg doesn't actually write anything — touch the
            # output path ourselves so the staging-tree assertions below
            # exercise the real dest_dir/fname the orchestrator computed,
            # matching what a real ffmpeg -y invocation would leave behind.
            async def _spawn(*args, **_kwargs):
                output_path = args[-1]
                assert output_path.endswith("_motion.mp4")
                with open(output_path, "wb") as f:
                    f.write(b"x" * 4096)
                return mock_proc

            with patch("asyncio.create_subprocess_exec", side_effect=_spawn):
                result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True
        staging_root = os.path.join(str(tmp_path / "nvr"), recorder._STAGING_DIRNAME)
        cam_dir = os.path.join(staging_root, CAM_TITLE)
        assert os.path.isdir(cam_dir)
        # Exactly one date dir, one clip file ending in _motion.mp4.
        dates = os.listdir(cam_dir)
        assert len(dates) == 1
        files = os.listdir(os.path.join(cam_dir, dates[0]))
        assert len(files) == 1
        assert files[0].endswith("_motion.mp4")

    @pytest.mark.asyncio
    async def test_postroll_segments_appended_after_preroll(self, tmp_path: Path):
        """postroll_seconds>0 + the ring keeps running through the wait
        (GitHub #54 follow-up): new ring segments written after the event
        are appended, in order, after the pre-roll segments — no separate
        capture file, no second RTSP session."""
        coord = _make_assembly_coord(tmp_path, postroll_seconds=5)
        cache_dir = str(tmp_path / "cache")
        event_time = time.time()
        seen_paths: dict[str, list[str]] = {}

        def _fake_create_motion_clip_args(preroll_paths, output_path):
            seen_paths["paths"] = list(preroll_paths)
            return ["ffmpeg", "-y", output_path]

        async def _fake_sleep(seconds):
            assert seconds == 5 + recorder._PREROLL_SEGMENT_SECONDS
            _write_ring_segment(
                cache_dir, CAM_TITLE, "120010", event_time + 1, size=2048
            )
            # A second, newest segment simulates the ring's currently-
            # writing file — dropped by the same "always drop newest" rule
            # list_preroll_files() uses, so it must NOT show up below.
            _write_ring_segment(
                cache_dir, CAM_TITLE, "120020", event_time + 2, size=2048
            )

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(tmp_path / "pre0.mp4")],
            ),
            patch(
                "custom_components.bosch_shc_camera.recorder.time.time",
                return_value=event_time,
            ),
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch.object(
                recorder,
                "create_motion_clip_args",
                side_effect=_fake_create_motion_clip_args,
            ),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True
        # pre0.mp4 (staged) then exactly ONE post-roll segment (the newer,
        # still-being-written one is excluded).
        assert len(seen_paths["paths"]) == 2
        assert seen_paths["paths"][0].endswith("pre0.mp4")
        assert seen_paths["paths"][1].endswith("120010.mp4")

    @pytest.mark.asyncio
    async def test_postroll_includes_provably_finalized_newest_segment(
        self, tmp_path: Path
    ):
        """GitHub #54 follow-up (realKim-dotcom): if the newest ring segment
        is provably finalized (ffprobe can read a duration — moov atom
        already written), it must be included in the post-roll tail instead
        of always being dropped."""
        coord = _make_assembly_coord(tmp_path, postroll_seconds=5)
        cache_dir = str(tmp_path / "cache")
        event_time = time.time()
        seen_paths: dict[str, list[str]] = {}

        def _fake_create_motion_clip_args(preroll_paths, output_path):
            seen_paths["paths"] = list(preroll_paths)
            return ["ffmpeg", "-y", output_path]

        async def _fake_sleep(seconds):
            assert seconds == 5 + recorder._PREROLL_SEGMENT_SECONDS
            _write_ring_segment(
                cache_dir, CAM_TITLE, "120010", event_time + 1, size=2048
            )
            # Ring already rotated past this one too — a real ffprobe would
            # find a valid moov atom on it, unlike the mid-write case in
            # test_postroll_segments_appended_after_preroll above.
            _write_ring_segment(
                cache_dir, CAM_TITLE, "120020", event_time + 2, size=2048
            )

        ffmpeg_proc = MagicMock()
        ffmpeg_proc.communicate = AsyncMock(return_value=(b"", b""))
        ffmpeg_proc.returncode = 0

        ffprobe_proc = MagicMock()
        ffprobe_proc.communicate = AsyncMock(return_value=(b"9.98\n", b""))
        ffprobe_proc.returncode = 0

        async def _fake_subprocess_exec(*args, **_kwargs):
            if args and args[0] == "ffprobe":
                return ffprobe_proc
            return ffmpeg_proc

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(tmp_path / "pre0.mp4")],
            ),
            patch(
                "custom_components.bosch_shc_camera.recorder.time.time",
                return_value=event_time,
            ),
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch.object(
                recorder,
                "create_motion_clip_args",
                side_effect=_fake_create_motion_clip_args,
            ),
            patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess_exec),
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True
        # pre0.mp4 (staged) then BOTH post-roll segments — the newest one
        # is kept because ffprobe proved it's already finalized.
        assert len(seen_paths["paths"]) == 3
        assert seen_paths["paths"][0].endswith("pre0.mp4")
        assert seen_paths["paths"][1].endswith("120010.mp4")
        assert seen_paths["paths"][2].endswith("120020.mp4")

    @pytest.mark.asyncio
    async def test_postroll_ring_not_running_skips_postroll(self, tmp_path: Path):
        """nvr_postroll_seconds>0 but nvr_preroll_seconds=0 (no ring
        spawned): there's nothing to derive a tail from — skip post-roll
        and ship pre-roll-only, without ever waiting."""
        coord = _make_assembly_coord(tmp_path, postroll_seconds=5, ring_running=False)

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(tmp_path / "pre0.mp4")],
            ),
            patch("asyncio.sleep") as mock_sleep,
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_postroll_no_new_segment_falls_back_to_preroll_only(
        self, tmp_path: Path
    ):
        """The ring is running but produces no new segment within the wait
        window (e.g. the LOCAL session dropped) — must not abort the clip,
        just ship pre-roll segments alone."""
        coord = _make_assembly_coord(tmp_path, postroll_seconds=5)

        async def _fake_sleep(_seconds):
            return None  # no new ring segment appears

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(tmp_path / "pre0.mp4")],
            ),
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True

    @pytest.mark.asyncio
    async def test_no_preroll_no_postroll_output_still_attempted(self, tmp_path: Path):
        """Both empty (e.g. options changed mid-flight): create_motion_clip
        itself returns False (nothing to concat) and that propagates."""
        coord = _make_assembly_coord(tmp_path, postroll_seconds=0)

        with patch.object(recorder, "list_preroll_files", return_value=[]):
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is False

    @pytest.mark.asyncio
    async def test_event_clip_switch_off_skips_entirely(self, tmp_path: Path):
        """Feature request (realKim-dotcom, issue #43 follow-up): the
        per-camera nvr_event_clip switch OFF must skip native assembly
        entirely, without ever touching the assembly lock, pre-roll list,
        or spawning ffmpeg — the ring buffer stays untouched for other
        consumers (e.g. a fork's own clip-saving service)."""
        coord = _make_assembly_coord(tmp_path, event_clip_enabled=False)

        with (
            patch.object(recorder, "list_preroll_files") as mock_list,
            patch("asyncio.create_subprocess_exec") as mock_spawn,
        ):
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is False
        mock_list.assert_not_called()
        mock_spawn.assert_not_called()
        assert not coord.get_nvr_clip_assembly_lock(CAM_ID).locked()

    @pytest.mark.asyncio
    async def test_event_clip_switch_on_by_default(self, tmp_path: Path):
        """Backward compatibility: with no explicit switch state, native
        assembly proceeds exactly as before this feature existed."""
        coord = _make_assembly_coord(tmp_path)

        with patch.object(
            recorder, "list_preroll_files", return_value=[str(tmp_path / "pre0.mp4")]
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True

    @pytest.mark.asyncio
    async def test_finalize_ring_on_event_disabled_by_default(self, tmp_path: Path):
        """nvr_finalize_ring_on_event defaults to off: the assembly must
        never call stop_and_finalize_preroll_recorder unless the option
        is explicitly turned on."""
        coord = _make_assembly_coord(tmp_path)

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(tmp_path / "pre0.mp4")],
            ),
            patch.object(
                recorder, "stop_and_finalize_preroll_recorder"
            ) as mock_finalize,
            patch.object(
                recorder, "restart_preroll_recorder_after_finalize"
            ) as mock_restart,
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True
        mock_finalize.assert_not_called()
        mock_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_ring_on_event_uses_stable_segment_list(
        self, tmp_path: Path
    ):
        """nvr_finalize_ring_on_event=True: the ring-stopped segment list
        from stop_and_finalize_preroll_recorder is used DIRECTLY (bypassing
        list_preroll_files entirely — GitHub #50), with the ring-derived
        post-roll tail still appended after (GitHub #54 follow-up). Unlike
        the pre-#54 cold-capture design, the ring is restarted BEFORE the
        post-roll wait (so it's actually recording through it), not after
        the clip is built — exactly once, not twice."""
        coord = _make_assembly_coord(
            tmp_path, postroll_seconds=5, finalize_ring_on_event=True
        )
        cache_dir = str(tmp_path / "cache")
        stable_segments = [str(tmp_path / "pre0.mp4"), str(tmp_path / "pre1.mp4")]
        for p in stable_segments:
            with open(p, "wb") as f:
                f.write(b"x" * 2048)

        event_time = time.time()
        seen_paths: dict[str, list[str]] = {}

        def _fake_create_motion_clip_args(preroll_paths, output_path):
            seen_paths["paths"] = list(preroll_paths)
            return ["ffmpeg", "-y", output_path]

        restart_before_sleep: list[bool] = []

        async def _fake_sleep(_seconds):
            # The ring must already have been restarted by the time we get
            # here — this is the whole point of GitHub #54's reordering.
            restart_before_sleep.append(mock_restart.await_count == 1)
            _write_ring_segment(cache_dir, CAM_TITLE, "120010", event_time + 1)
            _write_ring_segment(cache_dir, CAM_TITLE, "120020", event_time + 2)

        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(recorder, "list_preroll_files") as mock_list_preroll_files,
            patch.object(
                recorder,
                "stop_and_finalize_preroll_recorder",
                new=AsyncMock(return_value=(True, stable_segments)),
            ) as mock_finalize,
            patch.object(
                recorder,
                "restart_preroll_recorder_after_finalize",
                new=AsyncMock(),
            ) as mock_restart,
            patch(
                "custom_components.bosch_shc_camera.recorder.time.time",
                return_value=event_time,
            ),
            patch("asyncio.sleep", side_effect=_fake_sleep),
            patch.object(
                recorder,
                "create_motion_clip_args",
                side_effect=_fake_create_motion_clip_args,
            ),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True
        mock_finalize.assert_awaited_once_with(coord, CAM_ID)
        # list_preroll_files must NOT be consulted — the stable, already-
        # correct list from stop_and_finalize_preroll_recorder is used as
        # its basis (GitHub #51: staged into hardlinks before use, so the
        # paths actually opened by ffmpeg differ from the originals — that's
        # the point, not a regression).
        mock_list_preroll_files.assert_not_called()
        assert restart_before_sleep == [True]
        assert len(seen_paths["paths"]) == 3
        assert seen_paths["paths"][0].endswith("pre0.mp4") and (
            "/_stage/" in seen_paths["paths"][0]
        )
        assert seen_paths["paths"][1].endswith("pre1.mp4") and (
            "/_stage/" in seen_paths["paths"][1]
        )
        # post-roll (3rd) comes after the finalized pre-roll segments, and
        # (GitHub #54 follow-up) is staged into the same hardlink dir too —
        # it's now read straight out of the live ring, exposed to the same
        # prune race the pre-roll segments already were.
        assert seen_paths["paths"][2].endswith("120010.mp4") and (
            "/_stage/" in seen_paths["paths"][2]
        )
        # Restarted exactly once (before the wait) — not a second time in
        # the `finally` block.
        mock_restart.assert_awaited_once_with(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_finalize_ring_on_event_nothing_to_finalize_falls_back(
        self, tmp_path: Path
    ):
        """stop_and_finalize_preroll_recorder returning (False, []) — ring
        wasn't running / had nothing to finalize — must fall back to the
        normal list_preroll_files() path, same as before this feature, and
        must NOT attempt a restart (nothing was stopped)."""
        coord = _make_assembly_coord(tmp_path, finalize_ring_on_event=True)

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(tmp_path / "pre0.mp4")],
            ),
            patch.object(
                recorder,
                "stop_and_finalize_preroll_recorder",
                new=AsyncMock(return_value=(False, [])),
            ),
            patch.object(
                recorder, "restart_preroll_recorder_after_finalize"
            ) as mock_restart,
        ):
            (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
                result = await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        assert result is True
        mock_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_ring_on_event_restarts_even_if_clip_build_fails(
        self, tmp_path: Path
    ):
        """GitHub #50: the ring must be restarted even if create_motion_clip
        fails/raises after the ring was already stopped — otherwise a
        transient ffmpeg failure would leave the pre-roll ring permanently
        down until something unrelated happens to restart it."""
        coord = _make_assembly_coord(tmp_path, finalize_ring_on_event=True)
        stable_segments = [str(tmp_path / "pre0.mp4")]
        (tmp_path / "pre0.mp4").write_bytes(b"x" * 2048)

        with (
            patch.object(
                recorder,
                "stop_and_finalize_preroll_recorder",
                new=AsyncMock(return_value=(True, stable_segments)),
            ),
            patch.object(
                recorder,
                "restart_preroll_recorder_after_finalize",
                new=AsyncMock(),
            ) as mock_restart,
            patch.object(
                recorder,
                "create_motion_clip",
                new=AsyncMock(side_effect=RuntimeError("simulated ffmpeg crash")),
            ),
        ):
            with pytest.raises(RuntimeError):
                await recorder.assemble_and_ship_motion_clip(coord, CAM_ID)

        mock_restart.assert_awaited_once_with(coord, CAM_ID)


class TestNewestSegmentIsFinalized:
    """GitHub #54 follow-up (realKim-dotcom): `_newest_segment_is_finalized`
    must only report True on an actual proven-readable container, and fall
    back to False (→ drop-newest, the pre-existing safe behavior) on every
    failure/ambiguous mode — never risk shipping a moov-less segment."""

    @pytest.mark.asyncio
    async def test_valid_duration_returns_true(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"9.98\n", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is True

    @pytest.mark.asyncio
    async def test_nonzero_returncode_returns_false(self):
        """ffprobe failing to parse (no moov atom yet) must be treated as
        still-mid-write, matching the pre-existing drop-newest default."""
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b"moov atom not found"))
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False

    @pytest.mark.asyncio
    async def test_unparseable_stdout_returns_false(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"N/A\n", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False

    @pytest.mark.asyncio
    async def test_zero_duration_returns_false(self):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"0.0\n", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False

    @pytest.mark.asyncio
    async def test_ffprobe_not_found_returns_false(self):
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("ffprobe"),
        ):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False

    @pytest.mark.asyncio
    async def test_spawn_oserror_returns_false(self):
        with patch("asyncio.create_subprocess_exec", side_effect=OSError("no procs")):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_returns_false(self):
        """After a kill on timeout, the process must also be reaped
        (`proc.wait()`) — matching `stop_preroll_recorder`'s established
        kill+wait pattern — so a probe timeout never leaves a zombie
        child process behind."""
        proc = MagicMock()

        async def _hang():
            await asyncio.sleep(999)

        proc.communicate = AsyncMock(side_effect=_hang)
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=None)
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch(
                "custom_components.bosch_shc_camera.recorder."
                "TIMEOUT_RECORDER_SEGMENT_PROBE",
                0.01,
            ),
        ):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_kill_on_already_dead_process_is_swallowed(self):
        """proc.kill() on a process that already exited between the timeout
        firing and the kill call raises ProcessLookupError — must not
        propagate."""
        proc = MagicMock()

        async def _hang():
            await asyncio.sleep(999)

        proc.communicate = AsyncMock(side_effect=_hang)
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        proc.wait = AsyncMock(return_value=None)
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch(
                "custom_components.bosch_shc_camera.recorder."
                "TIMEOUT_RECORDER_SEGMENT_PROBE",
                0.01,
            ),
        ):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False

    @pytest.mark.asyncio
    async def test_wait_after_kill_also_times_out_is_swallowed(self):
        """A killed process that still doesn't reap within
        TIMEOUT_RECORDER_KILL_WAIT (e.g. stuck in uninterruptible I/O) must
        not raise — matching `stop_preroll_recorder`'s best-effort reap."""
        proc = MagicMock()

        async def _hang():
            await asyncio.sleep(999)

        proc.communicate = AsyncMock(side_effect=_hang)
        proc.kill = MagicMock()
        proc.wait = AsyncMock(side_effect=_hang)
        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch(
                "custom_components.bosch_shc_camera.recorder."
                "TIMEOUT_RECORDER_SEGMENT_PROBE",
                0.01,
            ),
            patch(
                "custom_components.bosch_shc_camera.recorder."
                "TIMEOUT_RECORDER_KILL_WAIT",
                0.01,
            ),
        ):
            assert await recorder._newest_segment_is_finalized("/x/seg.mp4") is False


class TestStopPrerollRecorderCachePrune:
    """issue #43 follow-up: leftover ring segments must not survive stop()."""

    @pytest.mark.asyncio
    async def test_leftover_segments_removed_on_stop(self, tmp_path: Path):
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg = cam_dir / "120000.mp4"
        seg.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 10))

        proc = _mock_proc(returncode=0)
        coord.nvr_preroll_processes[CAM_ID] = proc

        await recorder.stop_preroll_recorder(coord, CAM_ID)

        assert not seg.exists()

    @pytest.mark.asyncio
    async def test_no_process_still_prunes_cache(self, tmp_path: Path):
        """Even with no live process (e.g. crashed before this call), a
        stale ring file left from a prior run must still be cleaned up."""
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg = cam_dir / "120000.mp4"
        seg.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 10))

        await recorder.stop_preroll_recorder(coord, CAM_ID)

        assert not seg.exists()

    @pytest.mark.asyncio
    async def test_prune_error_swallowed(self, tmp_path: Path):
        """A raising executor job (e.g. cache dir vanished mid-prune) must
        not propagate — best-effort cleanup, non-fatal."""
        coord = _make_preroll_coord(tmp_path)

        async def _raising_executor(fn, *args, **kwargs):
            raise OSError("cache dir vanished")

        coord.hass.async_add_executor_job = _raising_executor

        # Must not raise.
        await recorder.stop_preroll_recorder(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_prune_cache_false_keeps_leftover_segments(self, tmp_path: Path):
        """Regression (bug-hunt finding, issue #43 follow-up): a RESPAWN
        (`prune_cache=False`) must NOT wipe the ring — only a genuine stop
        (the default) does. Without this, every LOCAL-session/cred-rotation
        renewal wiped the pre-roll buffer to empty, defeating its purpose."""
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        coord = _make_preroll_coord(tmp_path)
        cam_dir = tmp_path / CAM_TITLE
        cam_dir.mkdir()
        seg = cam_dir / "120000.mp4"
        seg.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 10))

        proc = _mock_proc(returncode=0)
        coord.nvr_preroll_processes[CAM_ID] = proc

        await recorder.stop_preroll_recorder(coord, CAM_ID, prune_cache=False)

        assert seg.exists()

    @pytest.mark.asyncio
    async def test_start_preroll_recorder_respawn_preserves_ring(self, tmp_path: Path):
        """End-to-end: calling `start_preroll_recorder` again for a camera
        with an existing ring (its own leading stop_preroll_recorder call)
        must not wipe the pre-existing segments — only the periodic
        prune-to-max_segs watcher/prune-on-spawn should ever trim them."""
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        seg = cam_dir / "120000.mp4"
        seg.write_bytes(b"x" * (_PREROLL_MIN_SIZE_BYTES + 10))

        proc = _mock_proc(returncode=None)

        async def _spawn(*_a, **_kw):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert seg.exists()


class TestStartRecorderEventBufferedPushesUpdate:
    """issue #43 follow-up: mini_nvr_state's preroll_running/preroll_segments
    attributes must refresh immediately when the ring spawns in
    event_buffered mode, not wait for the next coordinator tick."""

    @pytest.mark.asyncio
    async def test_async_update_listeners_called_after_preroll_start(self):
        coord = SimpleNamespace(
            live_connections={
                CAM_ID_SHORT: {
                    "_connection_type": "LOCAL",
                    "rtspsUrl": "rtsp://127.0.0.1:5000/cam",
                }
            },
            options={"nvr_preroll_seconds": 30},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
            async_update_listeners=MagicMock(),
        )
        coord.get_nvr_mode = lambda cid: "event_buffered"
        coord._nvr_recorder_locks = {}
        coord.get_nvr_recorder_lock = lambda cid: coord._nvr_recorder_locks.setdefault(
            cid, asyncio.Lock()
        )
        coord._nvr_preroll_zero_warned = set()

        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch.object(
                recorder,
                "_spawn_preroll_recorder_locked",
                new=AsyncMock(return_value=None),
            ),
        ):
            await recorder.start_recorder(coord, CAM_ID_SHORT)

        coord.async_update_listeners.assert_called_once()

    @pytest.mark.asyncio
    async def test_preroll_seconds_zero_no_update_pushed(self):
        """No ring is started (preroll_seconds=0) -> nothing changed, no
        spurious listener push."""
        coord = SimpleNamespace(
            live_connections={
                CAM_ID_SHORT: {
                    "_connection_type": "LOCAL",
                    "rtspsUrl": "rtsp://127.0.0.1:5000/cam",
                }
            },
            options={"nvr_preroll_seconds": 0},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
            async_update_listeners=MagicMock(),
        )
        coord.get_nvr_mode = lambda cid: "event_buffered"
        coord._nvr_recorder_locks = {}
        coord.get_nvr_recorder_lock = lambda cid: coord._nvr_recorder_locks.setdefault(
            cid, asyncio.Lock()
        )
        coord._nvr_preroll_zero_warned = set()

        with patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)):
            await recorder.start_recorder(coord, CAM_ID_SHORT)

        coord.async_update_listeners.assert_not_called()

    @pytest.mark.asyncio
    async def test_preroll_seconds_zero_logs_warning_once(self):
        """GitHub #64 (Lawyer82): mode='event_buffered' with the global
        nvr_preroll_seconds still at its 0 default must WARN once per camera
        instead of silently never spawning the ring — and must not repeat
        the WARN on a second call (e.g. a session renewal) while the
        condition is unchanged. Also proves the flag is genuinely
        per-camera, not a global/shared one — a second camera in the
        SAME coordinator hitting the identical condition must still get
        its own WARN."""
        cam_a, cam_b = CAM_ID_SHORT, "BBBBBBBB"
        coord = SimpleNamespace(
            live_connections={
                cam_a: {
                    "_connection_type": "LOCAL",
                    "rtspsUrl": "rtsp://127.0.0.1:5000/cam",
                },
                cam_b: {
                    "_connection_type": "LOCAL",
                    "rtspsUrl": "rtsp://127.0.0.1:5001/cam",
                },
            },
            options={"nvr_preroll_seconds": 0},
            data={
                cam_a: {"info": {"title": "Terrasse"}},
                cam_b: {"info": {"title": "Garten"}},
            },
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
            async_update_listeners=MagicMock(),
        )
        coord.get_nvr_mode = lambda cid: "event_buffered"
        coord._nvr_recorder_locks = {}
        coord.get_nvr_recorder_lock = lambda cid: coord._nvr_recorder_locks.setdefault(
            cid, asyncio.Lock()
        )
        coord._nvr_preroll_zero_warned = set()

        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch.object(recorder._LOGGER, "warning") as mock_warn,
        ):
            await recorder.start_recorder(coord, cam_a)
            await recorder.start_recorder(coord, cam_a)
            await recorder.start_recorder(coord, cam_b)

        assert mock_warn.call_count == 2, (
            "WARN must fire exactly once PER CAMERA, not once globally and "
            "not on every start_recorder call for an already-warned camera"
        )
        assert cam_a in coord._nvr_preroll_zero_warned
        assert cam_b in coord._nvr_preroll_zero_warned

    @pytest.mark.asyncio
    async def test_preroll_seconds_zero_warning_clears_when_set_positive(self):
        """Once nvr_preroll_seconds is fixed (>0), the one-time WARN flag
        must clear so it can re-fire if the option is ever reset to 0
        again."""
        coord = SimpleNamespace(
            live_connections={
                CAM_ID_SHORT: {
                    "_connection_type": "LOCAL",
                    "rtspsUrl": "rtsp://127.0.0.1:5000/cam",
                }
            },
            options={"nvr_preroll_seconds": 0},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
            async_update_listeners=MagicMock(),
        )
        coord.get_nvr_mode = lambda cid: "event_buffered"
        coord._nvr_recorder_locks = {}
        coord.get_nvr_recorder_lock = lambda cid: coord._nvr_recorder_locks.setdefault(
            cid, asyncio.Lock()
        )
        coord._nvr_preroll_zero_warned = set()

        with patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)):
            await recorder.start_recorder(coord, CAM_ID_SHORT)
        assert CAM_ID_SHORT in coord._nvr_preroll_zero_warned

        coord.options["nvr_preroll_seconds"] = 30
        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch.object(
                recorder,
                "_spawn_preroll_recorder_locked",
                new=AsyncMock(return_value=None),
            ),
        ):
            await recorder.start_recorder(coord, CAM_ID_SHORT)
        assert CAM_ID_SHORT not in coord._nvr_preroll_zero_warned


class TestSyncNvrCleanupLocalStagingExclusion:
    """Bug-hunt 2026-07-20: the empty-dir prune pass used to walk the
    ENTIRE base_path, including _staging/{cam}/{date}/ — start_recorder
    deliberately pre-creates TODAY's and TOMORROW's staging date-dir on
    start because ffmpeg's -strftime_mkdir is unreliable on some bundled
    builds (rc=254 "Failed to open segment"). Tomorrow's dir is empty by
    construction and stays empty until midnight rollover, so this daily
    cleanup (same cadence as the pre-creation) would almost always delete
    it, silently undoing the exact workaround it exists for.
    """

    def test_empty_tomorrow_staging_dir_survives_cleanup(self, tmp_path: Path):
        base = tmp_path / "nvr"
        staging_tomorrow = base / "_staging" / "Terrasse" / "2026-07-21"
        staging_tomorrow.mkdir(parents=True)

        # A genuinely old, non-staging file must still be cleaned up
        # normally, and its now-empty parent dir pruned as before.
        old_dir = base / "Terrasse" / "2026-07-01"
        old_dir.mkdir(parents=True)
        old_file = old_dir / "10-00.mp4"
        old_file.write_bytes(b"x")
        old_mtime = time.time() - 10 * 86400
        os.utime(old_file, (old_mtime, old_mtime))

        coord = SimpleNamespace(
            options={
                "nvr_base_path": str(base),
                "nvr_retention_days": 3,
            }
        )

        recorder._sync_nvr_cleanup_local(coord)

        assert staging_tomorrow.is_dir(), (
            "the pre-created, still-empty tomorrow staging dir must survive "
            "the daily cleanup — deleting it reintroduces the midnight-"
            "rollover rc=254 bug the pre-creation exists to prevent"
        )
        assert not old_file.exists(), "the genuinely old file must still be deleted"
        assert not old_dir.exists(), (
            "the now-empty non-staging date dir must still be pruned as before"
        )
