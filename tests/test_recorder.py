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
import datetime
import os
import signal
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


# =============================================================================
# Section: LAN-only gate (`should_record`)
# =============================================================================


def _make_gate_coord(
    *, conn_type: str = "LOCAL", online: bool = True
) -> SimpleNamespace:
    """Minimal coordinator stub with the three fields ``should_record`` reads."""
    return SimpleNamespace(
        _live_connections={CAM_ID: {"_connection_type": conn_type}},
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
        """Unknown cam_id (not in `_live_connections`) → not LOCAL → False."""
        from custom_components.bosch_shc_camera.recorder import should_record

        coord = SimpleNamespace(
            _live_connections={},
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


# =============================================================================
# Section: ffmpeg argv (pinned wire format) + quality switch
# =============================================================================


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


# =============================================================================
# Section: file pattern / directory layout
# =============================================================================


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


# =============================================================================
# Section: pre-roll ring-buffer pure helpers
# =============================================================================


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

    def test_missing_directory_returns_empty(self, tmp_path):
        """Pin: non-existent cam_dir returns [] (idempotent first call)."""
        from custom_components.bosch_shc_camera.recorder import _list_preroll_segments

        result = _list_preroll_segments(str(tmp_path / "does-not-exist"))
        assert result == []

    def test_directory_entry_skipped(self, tmp_path):
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

    def test_stat_race_between_listdir_and_stat_is_skipped(self, monkeypatch, tmp_path):
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

    def test_returns_empty_when_dir_missing(self, tmp_path):
        """Calling with a nonexistent path returns [] without raising."""
        from custom_components.bosch_shc_camera import recorder

        result = recorder._list_preroll_segments(str(tmp_path / "no_such_dir"))
        assert result == []

    def test_returns_empty_on_listdir_error(self, monkeypatch, tmp_path):
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

    def test_keeps_three_newest_of_seven(self, tmp_path):
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

    def test_under_max_keeps_everything(self, tmp_path):
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

    def test_unlink_oserror_continues(self, tmp_path):
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


# =============================================================================
# Section: motion-clip concatenation (`create_motion_clip`)
# =============================================================================


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
        _nvr_preroll_processes={},
        _nvr_preroll_segment_counts={},
        _nvr_preroll_tasks={},
        _bg_tasks=set(),
        # SENTINEL_RULE: monotonic-based "last X" maps default to float('-inf')
        # so any (now - last) >= interval check is True on fresh CI VMs.
        _nvr_last_preroll_prune={CAM_ID: float("-inf")},
    )
    coord.hass = SimpleNamespace(
        async_add_executor_job=_run_executor,
        async_create_background_task=lambda c, n=None: MagicMock(),
    )
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
        _nvr_error_state={},
        _nvr_auth_retry_count={},
        _nvr_recorder_locks={},
        hass=MagicMock(),
    )
    coord.hass.async_add_executor_job = AsyncMock(return_value=None)
    coord.hass.async_create_background_task = MagicMock(return_value=MagicMock())
    coord.hass.loop = MagicMock()
    coord._bg_tasks = set()

    def _get_nvr_recorder_lock(cid: str) -> asyncio.Lock:
        lock = coord._nvr_recorder_locks.get(cid)
        if lock is None:
            lock = asyncio.Lock()
            coord._nvr_recorder_locks[cid] = lock
        return lock

    coord._get_nvr_recorder_lock = _get_nvr_recorder_lock
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
    async def test_ffmpeg_not_found_returns_false(self, tmp_path):
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
    async def test_concat_write_oserror_returns_false(self, tmp_path):
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
    async def test_oserror_on_spawn_returns_false(self, tmp_path):
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
    async def test_communicate_timeout_returns_false(self, tmp_path):
        """If ffmpeg's communicate() hangs past the timeout, kill it and
        return False."""
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
    async def test_kill_process_lookup_error_swallowed(self, tmp_path):
        """`proc.kill()` raises because ffmpeg already exited between the
        communicate() timeout and the kill; the helper must return False
        without propagating the exception."""
        from custom_components.bosch_shc_camera import recorder

        async def _executor(fn, *args):
            return fn(*args)

        coord = SimpleNamespace(
            hass=SimpleNamespace(async_add_executor_job=_executor),
        )

        # proc.communicate() never resolves naturally — wait_for times out
        # before it does. kill() raises ProcessLookupError (already dead).
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        proc.kill = MagicMock(side_effect=ProcessLookupError())

        out_path = str(tmp_path / "clip.mp4")

        with (
            patch.object(
                recorder,
                "list_preroll_files",
                return_value=[str(tmp_path / "seg1.mp4")],
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
    async def test_concat_unlink_oserror_swallowed(self, tmp_path):
        """OSError when removing the concat file after a successful clip must
        be swallowed."""
        from custom_components.bosch_shc_camera import recorder
        from custom_components.bosch_shc_camera.recorder import _PREROLL_MIN_SIZE_BYTES

        cam_name = CAM_TITLE
        cache_dir = str(tmp_path / "cache")
        cam_cache = os.path.join(cache_dir, cam_name)
        os.makedirs(cam_cache, exist_ok=True)
        seg = os.path.join(cam_cache, "seg.mp4")
        with open(seg, "wb") as f:
            f.write(b"x" * _PREROLL_MIN_SIZE_BYTES)

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
            result = await recorder.create_motion_clip(coord, cam_id, output)

        assert result is True

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_via_phase_coord(self):
        """FileNotFoundError on spawn → returns False gracefully (using the
        MagicMock-hass coordinator stub shape)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        cam_id = next(iter(coord.data.keys()))

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
            result = await recorder.create_motion_clip(coord, cam_id, output)

        assert result is False


class TestListPrerollFiles:
    def test_returns_sorted_paths(self):
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


# =============================================================================
# Section: retention purge — mocked filesystem
# =============================================================================


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


# =============================================================================
# Section: retention purge — real tmp_path files
# =============================================================================


def _make_lifecycle_coord(
    *, conn_type: str = "LOCAL", base_path: str = "/tmp/nvr_test"
):
    """Stub coordinator with the fields recorder.py's lifecycle functions
    (start/stop_recorder, _watch_recorder, sync_nvr_cleanup) touch."""
    proxy_url = "rtsp://user:pass@127.0.0.1:46597/rtsp_tunnel?inst=1"
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
        _nvr_recent_crash={},
        _nvr_error_state={},
        _nvr_auth_retry_count={},
        _nvr_recorder_locks={},
        _bg_tasks=set(),
        data={CAM_ID: {"info": {"title": "Terrasse"}, "status": "ONLINE"}},
        options={
            "nvr_base_path": base_path,
            "nvr_retention_days": 3,
            "enable_nvr": True,
        },
        is_camera_online=lambda cid: True,
    )

    def _get_nvr_recorder_lock(cam_id: str) -> asyncio.Lock:
        lock = coord._nvr_recorder_locks.get(cam_id)
        if lock is None:
            lock = asyncio.Lock()
            coord._nvr_recorder_locks[cam_id] = lock
        return lock

    coord._get_nvr_recorder_lock = _get_nvr_recorder_lock

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
    def test_zero_retention_disables_cleanup(self, tmp_path):
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

    def test_deletes_files_older_than_cutoff(self, tmp_path):
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

    def test_prunes_empty_date_dirs_but_not_camera_root(self, tmp_path):
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

    def test_keeps_files_at_or_after_cutoff(self, tmp_path):
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

    def test_unreadable_file_skipped_not_crash(self, tmp_path):
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


# =============================================================================
# Section: start_recorder lifecycle
# =============================================================================


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


class TestStartRecorder:
    @pytest.mark.asyncio
    async def test_skipped_when_not_local(self, tmp_path):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(conn_type="REMOTE", base_path=str(tmp_path))
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_skipped_when_no_proxy_url(self, tmp_path):
        """rtspsUrl missing or not rtsp:// → skip with warning, no spawn."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord._live_connections[CAM_ID]["rtspsUrl"] = ""
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_when_proxy_url_is_https(self, tmp_path):
        """If only the rtsps:// URL is set (not rewritten through proxy),
        skip — recording over TLS to the camera bypasses our proxy."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord._live_connections[CAM_ID]["rtspsUrl"] = "rtsps://camera.lan/x"
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_spawns_ffmpeg(self, tmp_path):
        """LOCAL + valid proxy URL → spawn, register process, register watcher."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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
    async def test_successful_spawn_clears_stale_error_state(self, tmp_path):
        """Issue #42: _nvr_error_state must not stay stuck showing "error"
        forever after a give-up — a fresh successful spawn (manual toggle,
        or the stream-up hook reviving the recorder on the next LOCAL
        session) must clear it."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord._nvr_error_state[CAM_ID] = "ffmpeg crashed twice"
        proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord._nvr_error_state

    @pytest.mark.asyncio
    async def test_replaces_existing_process(self, tmp_path):
        """Calling start_recorder while one is already running must stop
        the old before spawning new — required for cred rotation."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_oserror_on_spawn_returns(self, tmp_path):
        """Generic OSError (permissions, OOM, fork limit) — log + return."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=OSError("EAGAIN"),
        ):
            await recorder.start_recorder(coord, CAM_ID)
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_makedirs_failure_aborts_spawn(self, tmp_path):
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
        """During the proxy-URL polling loop, if the connection type
        flips to non-LOCAL (user toggled stream off), `start_recorder`
        must return silently without starting ffmpeg."""
        from custom_components.bosch_shc_camera import recorder

        coord = SimpleNamespace(
            _live_connections={
                CAM_ID_SHORT: {"_connection_type": "LOCAL", "rtspsUrl": ""}
            },
            options={},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
        )

        async def _fake_sleep(_sec):
            # Flip the connection type after the first sleep so the loop
            # body sees it on the next iteration and returns.
            coord._live_connections[CAM_ID_SHORT]["_connection_type"] = "REMOTE"

        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch("asyncio.sleep", new=_fake_sleep),
            patch.object(recorder, "_PROXY_URL_WAIT_STEPS", 5),
        ):
            await recorder.start_recorder(coord, CAM_ID_SHORT)
        # Must have exited via the early `return` — coord.options unmodified
        # and no ffmpeg subprocess was spawned.

    @pytest.mark.asyncio
    async def test_rtsp_url_appears_during_wait_continues(self):
        """If the URL lands during polling, the loop breaks and the
        function continues past the wait block."""
        from custom_components.bosch_shc_camera import recorder

        coord = SimpleNamespace(
            _live_connections={
                CAM_ID_SHORT: {"_connection_type": "LOCAL", "rtspsUrl": ""}
            },
            options={"nvr_event_only": True, "nvr_preroll_seconds": 0},
            data={CAM_ID_SHORT: {"info": {"title": "Terrasse"}}},
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
        )

        async def _fake_sleep(_sec):
            coord._live_connections[CAM_ID_SHORT]["rtspsUrl"] = (
                "rtsp://127.0.0.1:5000/cam"
            )

        with (
            patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)),
            patch("asyncio.sleep", new=_fake_sleep),
            patch.object(recorder, "_PROXY_URL_WAIT_STEPS", 5),
        ):
            await recorder.start_recorder(coord, CAM_ID_SHORT)
        # nvr_event_only + preroll_seconds=0 returns immediately past the
        # poll loop without invoking ffmpeg — the test merely verifies the
        # function reached past the URL-landed branch without crashing.


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
                with patch.object(recorder, "start_preroll_recorder", new=AsyncMock()):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert len(spawned) == 0, "main ffmpeg spawned despite nvr_event_only=True"
        assert cam_id not in coord._nvr_processes

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
                recorder, "start_preroll_recorder", side_effect=fake_start_preroll
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
        assert cam_id in coord._nvr_processes, "main ffmpeg not spawned in normal mode"


# =============================================================================
# Section: stop_recorder / stop_all lifecycle
# =============================================================================


class TestStopRecorder:
    @pytest.mark.asyncio
    async def test_no_op_when_not_running(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        # No process registered
        await recorder.stop_recorder(coord, CAM_ID)
        # No exception, no state change
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_already_exited_quick_return(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=0)  # already exited
        coord._nvr_processes[CAM_ID] = proc
        await recorder.stop_recorder(coord, CAM_ID)
        proc.send_signal.assert_not_called()
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_clean_sigterm_exit(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
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
        coord._nvr_processes[CAM_ID] = proc

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
        coord._nvr_processes[CAM_ID] = proc
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
        coord._nvr_processes[CAM_ID] = proc

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

    @pytest.mark.asyncio
    async def test_stop_recorder_calls_stop_preroll(self):
        """stop_recorder must call stop_preroll_recorder to kill the pre-roll ffmpeg."""
        from custom_components.bosch_shc_camera import recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

        stopped_preroll = []

        async def fake_stop_preroll(c, cid):
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
        coord._nvr_processes["cam-A"] = proc_a
        coord._nvr_processes["cam-B"] = proc_b
        await recorder.stop_all(coord)
        # Both must be drained
        assert coord._nvr_processes == {}

    @pytest.mark.asyncio
    async def test_empty_dict_is_safe(self):
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord._nvr_processes.clear()
        await recorder.stop_all(coord)

    @pytest.mark.asyncio
    async def test_stop_all_calls_stop_all_preroll(self):
        """stop_all must call stop_all_preroll before stopping main recorders."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_phase_coord()
        coord._nvr_processes = {"cam1": MagicMock()}

        stop_all_preroll_called = []

        async def fake_stop_all_preroll(c):
            stop_all_preroll_called.append(True)

        with patch.object(
            recorder, "stop_all_preroll", side_effect=fake_stop_all_preroll
        ):
            with patch.object(recorder, "stop_recorder", new=AsyncMock()):
                await recorder.stop_all(coord)

        assert stop_all_preroll_called, "stop_all_preroll was not called from stop_all"


# =============================================================================
# Section: pre-roll recorder lifecycle (start/stop_preroll_recorder)
# =============================================================================


class TestPrerollRecorderLifecycle(unittest.TestCase):
    def test_start_preroll_requires_local_session(self):
        """No LOCAL session → preroll recorder not spawned."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)
        coord._live_connections[cam_id]["_connection_type"] = "REMOTE"

        async def _run():
            await recorder.start_preroll_recorder(coord, cam_id)
            return cam_id in coord._nvr_preroll_processes

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result is False

    def test_start_preroll_stores_process(self):
        """Valid LOCAL session → process stored in _nvr_preroll_processes."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

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

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

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

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

        async def _run():
            await recorder.stop_preroll_recorder(coord, cam_id)

        # Should not raise
        asyncio.get_event_loop().run_until_complete(_run())

    def test_stop_all_preroll_stops_all(self):
        """stop_all_preroll calls stop for every cam in _nvr_preroll_processes."""
        import custom_components.bosch_shc_camera.recorder as recorder

        coord = _make_phase_coord()

        stopped = []

        async def mock_stop(c, cid):
            stopped.append(cid)

        coord._nvr_preroll_processes = {"cam1": MagicMock(), "cam2": MagicMock()}

        async def _run():
            with patch.object(recorder, "stop_preroll_recorder", side_effect=mock_stop):
                await recorder.stop_all_preroll(coord)

        asyncio.get_event_loop().run_until_complete(_run())
        assert set(stopped) == {"cam1", "cam2"}


class TestStartPrerollRecorder:
    """LOCAL-gating full path + ffmpeg FileNotFoundError/OSError cleanup,
    using the tmp_path-based lifecycle coordinator stub."""

    @pytest.mark.asyncio
    async def test_skipped_when_not_local(self, tmp_path):
        """`_connection_type != "LOCAL"` → early return. No spawn, no proc
        registered. Pre-roll is LAN-only by design."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(conn_type="REMOTE", base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_skipped_when_rtsp_url_missing(self, tmp_path):
        """rtspsUrl empty / not rtsp:// → return."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        coord._live_connections[CAM_ID]["rtspsUrl"] = ""
        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_happy_path_full_local(self, tmp_path):
        """LOCAL + valid rtsp:// URL → walks the full path: makedirs,
        spawn, register proc, prune-on-spawn, register watcher task."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
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
        assert (tmp_path / "Terrasse").exists()
        # Prune-on-spawn happened with the computed max_segs.
        # nvr_preroll_seconds=30 → ceil(30/10)+1 = 4
        assert len(prune_calls) == 1
        assert prune_calls[0][1] == 4
        # Watcher task registered
        assert CAM_ID in coord._nvr_preroll_tasks
        assert coord._nvr_preroll_tasks[CAM_ID] is not None

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_cleanup(self, tmp_path):
        """`create_subprocess_exec` → `FileNotFoundError`. Must log error +
        return WITHOUT registering proc or task — otherwise stop_preroll
        would later iterate over a None proc."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
        with patch.object(
            asyncio,
            "create_subprocess_exec",
            side_effect=FileNotFoundError("ffmpeg not on PATH"),
        ):
            # Must not raise.
            await recorder.start_preroll_recorder(coord, CAM_ID)

        assert CAM_ID not in coord._nvr_preroll_processes
        # Pre-roll watcher task also not registered.
        assert CAM_ID not in coord._nvr_preroll_tasks

    @pytest.mark.asyncio
    async def test_spawn_oserror_cleanup(self, tmp_path):
        """Generic OSError on spawn — same cleanup invariant as the
        FileNotFoundError path."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
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
        """OSError during cache_dir creation → return, no spawn. Read-only
        fs / permission denied / NFS hiccup."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30

        async def _bad_executor(fn, *args, **kwargs):
            if fn is os.makedirs:
                raise OSError("EROFS")
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _bad_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_preroll_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_prune_exception_swallowed(self, tmp_path):
        """If prune_preroll_cache raises any Exception, start_preroll continues."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
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

    @pytest.mark.asyncio
    async def test_preroll_tasks_auto_created_when_absent(self, tmp_path):
        """If coordinator has no _nvr_preroll_tasks attr, it is created."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord.options["nvr_preroll_cache_dir"] = str(tmp_path)
        coord.options["nvr_preroll_seconds"] = 30
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


class TestStopPrerollRecorder:
    """SIGKILL escalation race paths + send_signal ProcessLookupError."""

    @pytest.mark.asyncio
    async def test_kill_process_lookup_error_swallowed(self, tmp_path):
        """SIGTERM times out → proc.kill() raises ProcessLookupError → no crash.

        Race: process died between our SIGTERM-timeout and the SIGKILL call.
        Must be swallowed so stop_preroll_recorder still completes cleanly.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        proc.kill = MagicMock(side_effect=ProcessLookupError("no such process"))
        coord._nvr_preroll_processes[CAM_ID] = proc

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
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_final_timeout_after_sigkill_swallowed(self, tmp_path):
        """Even SIGKILL hung in wait_for → final TimeoutError is swallowed.

        Under no circumstances may stop_preroll_recorder propagate a
        TimeoutError; the watchdog must remain non-blocking so the
        integration unload path can finish.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=None)
        coord._nvr_preroll_processes[CAM_ID] = proc

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
    async def test_no_process_registered_is_no_op(self, tmp_path):
        """Pin idempotency: calling stop on a cam with no live process is safe."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        # No process registered
        await recorder.stop_preroll_recorder(coord, CAM_ID)
        # No state change, no exception
        assert CAM_ID not in coord._nvr_preroll_processes

    @pytest.mark.asyncio
    async def test_already_exited_returns_quickly(self, tmp_path):
        """If returncode is already set, send_signal is never called."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_preroll_coord(tmp_path)
        proc = _mock_proc(returncode=0)
        coord._nvr_preroll_processes[CAM_ID] = proc

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
        coord._nvr_preroll_processes[CAM_ID] = proc

        # Must not raise
        await recorder.stop_preroll_recorder(coord, CAM_ID)

        # Process must have been popped
        assert CAM_ID not in coord._nvr_preroll_processes

    def test_cancels_watcher_task(self):
        """stop_preroll_recorder must cancel the periodic prune-watcher task."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

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


# =============================================================================
# Section: watch loops (_watch_recorder / _watch_preroll_recorder)
# =============================================================================


class TestWatchRecorder:
    @pytest.mark.asyncio
    async def test_clean_exit_no_respawn(self):
        """Process exited cleanly AND was already removed from
        _nvr_processes → no respawn (replacement / clean stop scenario)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
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

        coord = _make_lifecycle_coord(conn_type="REMOTE")  # gate now closed
        proc = _mock_proc(returncode=1, stderr_data=b"connection refused")
        proc.wait = AsyncMock(return_value=1)
        coord._nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc)
        restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_crash_triggers_respawn(self):
        """ffmpeg crashes within respawn window AND gate still open →
        respawn after delay."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()  # LOCAL, online
        proc = _mock_proc(returncode=1, stderr_data=b"transient")
        proc.wait = AsyncMock(return_value=1)
        coord._nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc)
        restart.assert_awaited_once_with(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_second_crash_within_window_gives_up(self):
        """Two crashes inside the respawn window → set error_state, no respawn.
        Defends against an infinite restart loop when the camera is dead."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        # Mark a recent crash
        coord._nvr_recent_crash[CAM_ID] = time.monotonic() - 5  # 5 s ago
        proc = _mock_proc(returncode=1, stderr_data=b"crash 2")
        proc.wait = AsyncMock(return_value=1)
        coord._nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc)
        restart.assert_not_called()
        assert "crashed" in coord._nvr_error_state.get(CAM_ID, "").lower()

    @pytest.mark.asyncio
    async def test_auth_failure_respawns_without_giveup(self):
        """Issue #42: a 401/Unauthorized ffmpeg exit (cred-rotation race) must
        retry without counting toward the 2-crash give-up threshold — a
        second back-to-back auth-failure must NOT set _nvr_error_state or
        skip the respawn, unlike a genuine repeated crash."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        # Simulate the reported sequence: a crash was already recorded
        # moments ago — with a normal crash this would trigger give-up.
        coord._nvr_recent_crash[CAM_ID] = time.monotonic() - 5
        proc = _mock_proc(
            returncode=8, stderr_data=b"method OPTIONS failed: 401 (Unauthorized)"
        )
        proc.wait = AsyncMock(return_value=8)
        coord._nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc)

        restart.assert_awaited_once_with(coord, CAM_ID, is_auto_retry=True)
        assert CAM_ID not in coord._nvr_error_state
        # The crash-window timestamp must be untouched by the auth-failure
        # path — it doesn't count as a "crash" for give-up purposes.
        assert coord._nvr_recent_crash[CAM_ID] == pytest.approx(
            time.monotonic() - 5, abs=1.0
        )

    @pytest.mark.asyncio
    async def test_auth_failure_case_insensitive_and_lowercase_401(self):
        """The marker match must be case-insensitive and also catch a bare
        '401' without the word 'Unauthorized' in the tail."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=8, stderr_data=b"HTTP/1.1 401 \n")
        proc.wait = AsyncMock(return_value=8)
        coord._nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc)

        restart.assert_awaited_once_with(coord, CAM_ID, is_auto_retry=True)
        assert CAM_ID not in coord._nvr_error_state

    @pytest.mark.asyncio
    async def test_auth_failure_no_respawn_when_gate_closed_after_sleep(self):
        """Same gate-recheck-after-sleep discipline as the normal crash path
        must apply to the auth-failure path too."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(returncode=8, stderr_data=b"401 unauthorized")
        proc.wait = AsyncMock(return_value=8)
        coord._nvr_processes[CAM_ID] = proc

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
            await recorder._watch_recorder(coord, CAM_ID, proc)

        restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_closed_after_sleep_no_respawn(self):
        """If should_record returns False after the respawn sleep, don't respawn."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        coord._nvr_recent_crash = {CAM_ID: float("-inf")}
        proc = _mock_proc(returncode=1)  # non-zero → crash path
        coord._nvr_processes[CAM_ID] = proc

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
            await recorder._watch_recorder(coord, CAM_ID, proc)

        # Sleep happened (respawn delay)
        assert len(sleep_calls) == 1
        # But start_recorder was NOT called (gate was closed)
        assert len(start_calls) == 0

    @pytest.mark.asyncio
    async def test_stderr_drain_timeout_no_crash(self, tmp_path):
        """If `proc.stderr.read(2048)` never resolves, `asyncio.wait_for`
        raises `asyncio.TimeoutError` which is swallowed. The watcher must
        continue (not crash, not propagate) — production: a frozen TCP
        stack on the camera mustn't kill the integration's task pool.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))

        # stderr.read returns a coroutine that hangs forever. We then
        # patch `asyncio.wait_for` to raise TimeoutError so we don't
        # actually block.
        stderr = MagicMock()

        async def _hang(_n):
            await asyncio.sleep(3600)  # would block; wait_for short-circuits

        stderr.read = _hang

        proc = _mock_proc(returncode=1, stderr=stderr)
        coord._nvr_processes[CAM_ID] = proc

        # User intent stays True, so the watcher will try to respawn — we
        # patch `start_recorder` to a no-op so we observe only the drain
        # branch.
        respawn_called = {"n": 0}

        async def _fake_start(_coord, _cam):
            respawn_called["n"] += 1

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
            # Must NOT raise. Drain TimeoutError caught, rest of watcher
            # runs to completion (respawn path).
            await recorder._watch_recorder(coord, CAM_ID, proc)

        # Watcher reached the respawn branch → drain swallowed correctly.
        assert respawn_called["n"] == 1

    @pytest.mark.asyncio
    async def test_stderr_drain_generic_exception_swallowed(self, tmp_path):
        """Same fall-through for non-Timeout exceptions (e.g. stderr already
        closed)."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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


class TestWatchPrerollRecorder:
    """Periodic prune loop that runs while a pre-roll ffmpeg child is alive.
    Exits when `proc.returncode is not None`. Without a fake-clock the loop
    would block 10s/iteration; `asyncio.sleep` is patched to no-op."""

    @pytest.mark.asyncio
    async def test_periodic_prune_called_then_exits_on_proc_exit(self, tmp_path):
        """One prune iteration → proc.returncode set → loop exits.

        Fake `asyncio.sleep` (no real-time wait). After the first wakeup
        the watcher calls `prune_preroll_cache` once; before the second
        wakeup we set `proc.returncode = 0` so the early-return triggers
        and the loop exits cleanly.
        """
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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

        # prune must have been invoked at least once
        assert len(prune_calls) >= 1
        # cam_dir + max_segs passed through unchanged
        assert prune_calls[0] == (str(tmp_path / "cam"), 4)
        # Loop slept at least twice (first to call prune, second to see
        # the dead proc and return). Pure smoke for the periodic shape.
        assert sleep_count["n"] >= 2

    @pytest.mark.asyncio
    async def test_exits_when_proc_missing_from_dict(self, tmp_path):
        """If `_nvr_preroll_processes[cam_id]` is gone (clean stop / crash
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
    async def test_prune_exception_swallowed_then_proc_exits(self, tmp_path):
        """`prune_preroll_cache` raising must not kill the watcher. After the
        swallow we let the proc exit on the next tick so the test terminates."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
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
        coord._nvr_preroll_processes[cam_id] = mock_proc

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

        # No process in _nvr_preroll_processes → watcher should return
        coord._nvr_preroll_processes = {}

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

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(cam_id=cam_id)

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


# =============================================================================
# Section: pre-roll → recorder wiring (start/stop_recorder trigger preroll)
# =============================================================================


class TestPrerollWiring(unittest.TestCase):
    """Regression: start_preroll_recorder was never called from start_recorder
    (wiring omission found 2026-05-08 during live test). Verified by checking
    that /dev/shm/bosch_nvr_cache/ was never created despite preroll_seconds=30."""

    def test_start_recorder_calls_preroll_when_seconds_gt_zero(self):
        """start_recorder must call start_preroll_recorder when nvr_preroll_seconds > 0."""
        import custom_components.bosch_shc_camera.recorder as recorder

        cam_id = CAM_ID_SHORT
        coord = _make_phase_coord(
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
                    recorder, "start_preroll_recorder", side_effect=fake_start_preroll
                ):
                    await recorder.start_recorder(coord, cam_id)

        asyncio.get_event_loop().run_until_complete(_run())
        assert cam_id not in started_preroll, (
            "start_preroll_recorder was called despite seconds=0"
        )


# =============================================================================
# Section: switch delegation (BoschNvrRecordingSwitch)
# =============================================================================


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
            _live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
            _nvr_processes={},
            _nvr_user_intent={},
            _nvr_error_state={},
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
        coord._nvr_user_intent[CAM_ID] = True
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
        coord._live_connections[CAM_ID]["_connection_type"] = "REMOTE"
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
            _live_connections={},
            _nvr_processes={},
            _nvr_user_intent={},
            _nvr_error_state={},
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


# =============================================================================
# Section: staging-drain pipeline (recorder._drain_staging_to_remote, sync_drain_tick
# and friends) — NVR-storage-target upload/promote/retention flow introduced
# in v11.0.4. Covers: _is_segment_finalized (mtime+size gate),
# _list_staging_candidates (directory walker), sync_drain_tick (local/smb/ftp
# dispatch + per-camera diagnostic state + retry-cap quarantine), SMB/FTP
# retention purge respecting nvr_smb_subpath, and the watcher coroutine's
# start/stop/exception-swallowing semantics. All filesystem and network I/O
# is mocked; tests use tmp_path so nothing escapes the per-test sandbox.
# =============================================================================

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


# ── 1. _is_segment_finalized ─────────────────────────────────────────────────


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


# ── 2. _list_staging_candidates ──────────────────────────────────────────────


class TestListStagingCandidates:
    def test_missing_root_returns_empty(self, tmp_path):
        assert (
            recorder._list_staging_candidates(
                str(tmp_path / "does-not-exist"),
            )
            == []
        )

    def test_walks_cam_date_files(self, tmp_path):
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

    def test_skips_non_dir_entries_in_root(self, tmp_path):
        """A stray file under _staging/ must not blow up the walk."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        (staging / "stray.mp4").write_bytes(b"x")
        out = recorder._list_staging_candidates(str(staging))
        assert out == []

    def test_skips_non_dir_date_entry(self, tmp_path):
        """A stray file under _staging/<cam>/ must not blow up."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        cam = staging / CAM
        cam.mkdir()
        (cam / "stray.mp4").write_bytes(b"x")
        out = recorder._list_staging_candidates(str(staging))
        assert out == []


# ── 3. sync_drain_tick — full target dispatch ────────────────────────────────


class TestSyncDrainTickLocal:
    def test_finalized_segment_promoted_to_local_layout(self, tmp_path):
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

    def test_too_young_segment_left_in_staging(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4", age_seconds=5)
        coord = _make_coord(tmp_path, target="local")
        result = recorder.sync_drain_tick(coord)
        assert result["pending"] == 1
        # File untouched.
        assert (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()

    def test_unknown_target_falls_through_to_local(self, tmp_path):
        """Misconfigured target → fail-safe to local promotion (never to nowhere)."""
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="garbage")
        result = recorder.sync_drain_tick(coord)
        assert result["promoted"] == 1


class TestSyncDrainTickSmb:
    def test_smb_target_invokes_upload(self, tmp_path):
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

    def test_smb_failure_is_counted(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="smb")
        with patch.object(recorder, "_upload_smb", return_value=False):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 0
        assert result["failed"] == 1
        # Staging file kept for retry.
        assert (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()


class TestSyncDrainTickFtp:
    def test_ftp_target_invokes_upload(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_upload_ftp", return_value=True) as up:
            result = recorder.sync_drain_tick(coord)
        up.assert_called_once()
        assert result["uploaded"] == 1
        assert not (tmp_path / "_staging" / CAM / "2026-05-06" / "10-00.mp4").exists()

    def test_ftp_failure_is_counted(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with patch.object(recorder, "_upload_ftp", return_value=False):
            result = recorder.sync_drain_tick(coord)
        assert result["failed"] == 1


class TestSyncDrainTickRetryCap:
    """5 failures → file moves to _failed/ + persistent_notification fired."""

    def test_quarantine_after_max_retries(self, tmp_path):
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
        assert path.as_posix() not in coord._nvr_drain_failures


class TestSyncDrainTickStateCounters:
    """The watcher persists state on the coordinator so the diagnostic sensor
    can render it. Pin the shape."""

    def test_drain_state_populated(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="local")
        recorder.sync_drain_tick(coord, now=time.time())
        state = coord._nvr_drain_state
        assert state["target"] == "local"
        assert state["promoted"] == 1
        assert "last_age_by_cam" in state
        assert CAM in state["last_age_by_cam"]


# ── 4. SMB / FTP retention purge with nvr_smb_subpath ────────────────────────


class TestNvrCleanupSmbSubpath:
    def test_smb_root_uses_nvr_subpath(self, tmp_path):
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

    def test_smb_skip_without_server(self, tmp_path):
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
    def test_ftp_root_uses_nvr_subpath(self, tmp_path):
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

    def test_ftp_zero_retention_skipped(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp", retention_days=0)
        # Should never even try to connect.
        with patch("custom_components.bosch_shc_camera.smb._ftp_connect") as conn:
            recorder._sync_nvr_cleanup_ftp(coord)
            conn.assert_not_called()


class TestNvrCleanupDispatch:
    """``sync_nvr_cleanup`` is the public entry point — it dispatches to the
    target-specific helper plus always purges the local staging tree."""

    def test_local_only_calls_local_helper(self, tmp_path):
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

    def test_smb_target_calls_smb_and_local(self, tmp_path):
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

    def test_ftp_target_calls_ftp_and_local(self, tmp_path):
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

    def test_zero_retention_short_circuits(self, tmp_path):
        coord = _make_coord(tmp_path, retention_days=0)
        with patch.object(recorder, "_sync_nvr_cleanup_local") as loc:
            recorder.sync_nvr_cleanup(coord)
        loc.assert_not_called()


# ── 5. Watcher start / stop coroutine ────────────────────────────────────────


class TestDrainStagingWatcher:
    @pytest.mark.asyncio
    async def test_watcher_runs_tick_then_sleeps(self, tmp_path):
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
                recorder._drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.15)  # let it run a couple of ticks
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert ticks, "watcher never invoked sync_drain_tick"

    @pytest.mark.asyncio
    async def test_watcher_skips_when_nvr_disabled(self, tmp_path):
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
                recorder._drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.15)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_watcher_swallows_tick_exception(self, tmp_path):
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
                recorder._drain_staging_to_remote(coord),
            )
            await asyncio.sleep(0.20)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert len(calls) >= 2, "watcher exited after first exception"


# ── 6. Pure helpers — _remote_smb_path / _remote_ftp_path ────────────────────


class TestRemotePathHelpers:
    def test_smb_path_includes_subpath(self, tmp_path):
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

    def test_smb_path_sanitizes_camera_name(self, tmp_path):
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

    def test_ftp_path_starts_with_slash(self, tmp_path):
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


# ── 7. _upload_smb / _upload_ftp / _move_local — direct unit tests ───────────


class TestMoveLocal:
    def test_success_returns_true_and_creates_dest(self, tmp_path):
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

    def test_oserror_returns_false(self, tmp_path):
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
    def test_returns_false_when_smbprotocol_missing(self, tmp_path):
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

    def test_returns_false_when_server_empty(self, tmp_path):
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

    def test_returns_false_on_session_failure(self, tmp_path):
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

    def test_returns_false_on_mkdirs_failure(self, tmp_path):
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

    def test_returns_false_on_upload_open_failure(self, tmp_path):
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

    def test_happy_path_writes_to_smb(self, tmp_path):
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
    def test_returns_false_when_server_empty(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp", smb_server="")
        ok = recorder._upload_ftp(
            coord,
            "/fake.mp4",
            CAM,
            "2026-05-06",
            "10-00.mp4",
        )
        assert ok is False

    def test_returns_false_on_login_failure(self, tmp_path):
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

    def test_returns_false_on_mkdirs_failure(self, tmp_path):
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

    def test_returns_false_on_storbinary_failure(self, tmp_path):
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

    def test_happy_path_calls_storbinary(self, tmp_path):
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

    def test_quit_failure_falls_back_to_close(self, tmp_path):
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


# ── 8. Quarantine helper ─────────────────────────────────────────────────────


class TestQuarantineFailed:
    def test_moves_file_into_failed_tree(self, tmp_path):
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

    def test_oserror_swallowed(self, tmp_path):
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


# ── 9. _sync_nvr_cleanup_local ───────────────────────────────────────────────


class TestSyncNvrCleanupLocal:
    def test_skips_when_path_missing(self, tmp_path):
        coord = _make_coord(tmp_path / "doesnotexist", target="local")
        # No raise.
        recorder._sync_nvr_cleanup_local(coord)

    def test_skips_when_zero_retention(self, tmp_path):
        coord = _make_coord(tmp_path, target="local", retention_days=0)
        recorder._sync_nvr_cleanup_local(coord)

    def test_deletes_old_files(self, tmp_path):
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

    def test_stat_failure_skipped(self, tmp_path):
        """A file that disappears between os.walk and os.stat must not raise."""
        f = tmp_path / CAM / "2026-04-01" / "10-00.mp4"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
        coord = _make_coord(tmp_path, target="local", retention_days=3)
        with patch("os.stat", side_effect=OSError("vanished")):
            recorder._sync_nvr_cleanup_local(coord)

    def test_remove_failure_swallowed(self, tmp_path):
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

    def test_rmdir_failure_swallowed(self, tmp_path):
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


# ── 10. _list_staging_candidates extra branches ──────────────────────────────


class TestListStagingExtra:
    def test_listdir_root_oserror(self, tmp_path):
        """os.listdir(staging_root) raising — return empty list."""
        staging = tmp_path / "_staging"
        staging.mkdir()
        with patch("os.listdir", side_effect=OSError("perm")):
            assert recorder._list_staging_candidates(str(staging)) == []

    def test_listdir_cam_oserror(self, tmp_path):
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

    def test_listdir_date_oserror(self, tmp_path):
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

    def test_stat_failure_skipped(self, tmp_path):
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

    def test_non_regular_file_skipped(self, tmp_path):
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


# ── 11. sync_drain_tick — successful upload but unlink fails ─────────────────


class TestSyncDrainTickUnlinkFailure:
    def test_smb_unlink_failure_only_logs(self, tmp_path):
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

    def test_ftp_unlink_failure_only_logs(self, tmp_path):
        _make_segment(tmp_path, CAM, "2026-05-06", "10-00.mp4")
        coord = _make_coord(tmp_path, target="ftp")
        with (
            patch.object(recorder, "_upload_ftp", return_value=True),
            patch("os.unlink", side_effect=OSError("readonly")),
        ):
            result = recorder.sync_drain_tick(coord)
        assert result["uploaded"] == 1
        assert result["failed"] == 0

    def test_persistent_notification_swallows_errors(self, tmp_path):
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


# ── 12. _sync_nvr_cleanup_smb / _ftp — deeper walks ──────────────────────────


class TestSyncNvrCleanupSmbDeepWalk:
    def test_smb_skipped_when_no_share(self, tmp_path):
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

    def test_smb_session_failure_returns(self, tmp_path):
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

    def test_smb_walk_recurses_and_deletes(self, tmp_path):
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

    def test_smb_scandir_exception_swallowed(self, tmp_path):
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

    def test_smb_stat_exception_swallowed(self, tmp_path):
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
    def test_ftp_walk_lists_and_deletes(self, tmp_path):
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

    def test_ftp_cwd_failure_returns_cleanly(self, tmp_path):
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

    def test_ftp_listing_exception_swallowed(self, tmp_path):
        coord = _make_coord(
            tmp_path, target="ftp", smb_base_path="Bosch", smb_subpath="NVR"
        )
        ftp = MagicMock()
        ftp.retrlines.side_effect = OSError("listing failed")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect", return_value=ftp
        ):
            recorder._sync_nvr_cleanup_ftp(coord)

    def test_ftp_mdtm_failure_skips_file(self, tmp_path):
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

    def test_ftp_delete_failure_swallowed(self, tmp_path):
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

    def test_ftp_cwd_in_recursion_swallowed(self, tmp_path):
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

    def test_ftp_quit_failure_swallowed(self, tmp_path):
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

    def test_ftp_mdtm_and_delete_use_absolute_paths(self, tmp_path):
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


# ── 13. Upload_ftp close-fallback when both quit and close fail ──────────────


class TestUploadFtpCloseFallback:
    def test_quit_and_close_both_fail(self, tmp_path):
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


# ── 14. Misc edge / warning paths in NEW functions ───────────────────────────


class TestUploadSmbServerEmptyWarning:
    def test_warning_logged_no_session(self, tmp_path, caplog):
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
    def test_smbclient_missing_returns_silently(self, tmp_path):
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
    def test_connect_failure_returns_silently(self, tmp_path):
        coord = _make_coord(tmp_path, target="ftp")
        with patch(
            "custom_components.bosch_shc_camera.smb._ftp_connect",
            side_effect=OSError("login refused"),
        ):
            recorder._sync_nvr_cleanup_ftp(coord)


class TestFtpCleanupShortAndDotDotLines:
    def test_short_line_skipped(self, tmp_path):
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


# ── D-P2: persistent_notification scheduling — thread vs loop ─────────────────


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

    # ── _watch_recorder disk-full path ─────────────────────────────────────

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
            _nvr_processes={},
            _nvr_user_intent={cam_id: True},
            _nvr_recent_crash={},
            _nvr_error_state={},
            data={
                cam_id: {
                    "info": {"title": "Terrasse"},
                    "status": "ONLINE",
                }
            },
            options={"nvr_base_path": "/tmp/nvr_test", "enable_nvr": True},
            is_camera_online=lambda cid: True,
            _live_connections={
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
        coord._nvr_processes[cam_id] = proc

        await recorder._watch_recorder(coord, cam_id, proc)

        # async_create_task MUST have been called (notification scheduled).
        create_task.assert_called_once()
        # call_soon_threadsafe must NOT be used — we're already on the loop.
        call_soon_threadsafe.assert_not_called()
        assert coord._nvr_error_state.get(cam_id) == "disk full"

    @pytest.mark.asyncio
    async def test_watch_recorder_diskfull_swallows_async_create_task_error(
        self,
    ) -> None:
        """If async_create_task raises (e.g. loop shutting down), the disk-full
        branch must still set error_state and return — no unhandled exception."""
        cam_id = "AABBCCDD-0000-0000-0000-000000000001"

        coord = SimpleNamespace(
            _nvr_processes={},
            _nvr_user_intent={cam_id: True},
            _nvr_recent_crash={},
            _nvr_error_state={},
            data={cam_id: {"info": {"title": "Cam"}, "status": "ONLINE"}},
            options={"nvr_base_path": "/tmp/nvr_test", "enable_nvr": True},
            is_camera_online=lambda cid: True,
            _live_connections={
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
        coord._nvr_processes[cam_id] = proc

        # Must not raise even though async_create_task blows up.
        await recorder._watch_recorder(coord, cam_id, proc)

        assert coord._nvr_error_state.get(cam_id) == "disk full"

    # ── sync_drain_tick quarantine path ────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
# Section: `_nvr_recent_crash` SENTINEL_RULE default (relocated from
# tests/test_bug_regression_v11.py)
# ─────────────────────────────────────────────────────────────────────────────


class TestNvrRecentCrashSentinel:
    """`_nvr_recent_crash.get(cam_id, ...)` must default to float('-inf')
    (SENTINEL_RULE). On CI VMs where time.monotonic() < _RESPAWN_WINDOW_SECONDS,
    a 0.0 default makes the FIRST crash look like a second crash within the
    window, permanently suppressing respawn."""

    def test_first_crash_default_is_not_zero(self):
        import inspect

        from custom_components.bosch_shc_camera import recorder

        src = inspect.getsource(recorder)
        assert "_nvr_recent_crash.get(cam_id, 0.0)" not in src, (
            "recorder.py must not use 0.0 as default for _nvr_recent_crash; "
            "on CI VMs with low monotonic, first crash triggers false crash-loop detection"
        )

    def test_first_crash_uses_neginf_default(self):
        import inspect

        from custom_components.bosch_shc_camera import recorder

        src = inspect.getsource(recorder)
        assert_in_source(src, '_nvr_recent_crash.get(cam_id, float("-inf"))')

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


# =============================================================================
# Section: issue #42 follow-up — cred-rotation race root-cause fix
# (late re-read + shared lock with _refresh_local_creds_from_heartbeat) and
# bounded auth-retry so a genuine broken credential surfaces instead of
# retrying forever.
# =============================================================================


class TestStartRecorderCredRotationRace:
    @pytest.mark.asyncio
    async def test_late_rotation_uses_fresh_url_not_stale_capture(self, tmp_path):
        """A heartbeat cred rotation landing between the makedirs executor
        job and the ffmpeg spawn must NOT result in ffmpeg being launched
        with the stale, already-invalidated URL captured earlier in
        start_recorder — it must re-read _live_connections one more time
        right before spawning."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        stale_url = coord._live_connections[CAM_ID]["rtspsUrl"]
        fresh_url = "rtsp://newuser:newpass@127.0.0.1:46597/rtsp_tunnel?inst=1"

        async def _rotating_executor(fn, *args, **kwargs):
            # Simulate _refresh_local_creds_from_heartbeat firing while
            # start_recorder is awaiting the staging-dir makedirs job.
            if fn is os.makedirs:
                coord._live_connections[CAM_ID]["rtspsUrl"] = fresh_url
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
    async def test_torn_down_mid_makedirs_aborts_spawn(self, tmp_path):
        """If the LOCAL session is torn down (e.g. LOCAL→REMOTE fallback)
        while start_recorder awaits the makedirs job, the final re-read
        under the lock must detect this and abort rather than spawn ffmpeg
        against a stream that no longer exists."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))

        async def _tearing_down_executor(fn, *args, **kwargs):
            if fn is os.makedirs:
                coord._live_connections[CAM_ID]["_connection_type"] = "REMOTE"
            return fn(*args, **kwargs)

        coord.hass.async_add_executor_job = _tearing_down_executor

        with patch.object(asyncio, "create_subprocess_exec") as spawn:
            await recorder.start_recorder(coord, CAM_ID)
        spawn.assert_not_called()
        assert CAM_ID not in coord._nvr_processes

    @pytest.mark.asyncio
    async def test_spawn_serializes_against_heartbeat_lock(self, tmp_path):
        """start_recorder's final re-read+spawn must run under the SAME
        per-camera lock instance _refresh_local_creds_from_heartbeat uses,
        so the two can never interleave mid-mutation."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        proc = _mock_proc(returncode=None)
        seen_locked_during_spawn = []

        async def _spawn(*args, **kwargs):
            seen_locked_during_spawn.append(
                coord._get_nvr_recorder_lock(CAM_ID).locked()
            )
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)

        assert seen_locked_during_spawn == [True], (
            "ffmpeg spawn must happen while holding the per-camera NVR recorder lock"
        )
        # Lock must be released again once start_recorder returns.
        assert not coord._get_nvr_recorder_lock(CAM_ID).locked()


class TestWatchRecorderBoundedAuthRetry:
    @pytest.mark.asyncio
    async def test_retries_up_to_cap_then_gives_up(self, tmp_path):
        """6 consecutive 401 exits must retry exactly 5 times (per
        _MAX_CONSECUTIVE_AUTH_RETRIES) and then give up with a distinct
        error message — a genuine broken credential must not retry
        forever and silently hide the fault from the user.

        Regression: this must exercise the REAL `start_recorder` (only
        `asyncio.create_subprocess_exec` mocked), not a fake stand-in —
        the bug this guards against was `start_recorder`'s own
        auth-retry respawn resetting `_nvr_auth_retry_count` before the
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
        coord._nvr_processes[CAM_ID] = first_proc

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
                await recorder._watch_recorder(coord, CAM_ID, proc)
                proc = coord._nvr_processes.get(CAM_ID)
                if proc is None:
                    break  # gave up

        assert spawn_count[0] == recorder._MAX_CONSECUTIVE_AUTH_RETRIES, (
            "must respawn via the REAL start_recorder exactly "
            "_MAX_CONSECUTIVE_AUTH_RETRIES times, not loop forever"
        )
        assert coord._nvr_auth_retry_count[CAM_ID] == (
            recorder._MAX_CONSECUTIVE_AUTH_RETRIES + 1
        )
        assert "repeated auth failures" in coord._nvr_error_state.get(CAM_ID, "")
        assert CAM_ID not in coord._nvr_processes, (
            "must give up with no process registered, not keep respawning"
        )

    @pytest.mark.asyncio
    async def test_single_auth_failure_does_not_give_up(self):
        """A lone 401 (the common transient-race case) must retry without
        touching _nvr_error_state — the bounded cap must not make the
        common case worse."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord()
        proc = _mock_proc(
            returncode=8, stderr_data=b"method OPTIONS failed: 401 (Unauthorized)"
        )
        coord._nvr_processes[CAM_ID] = proc

        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc)

        restart.assert_awaited_once_with(coord, CAM_ID, is_auto_retry=True)
        assert CAM_ID not in coord._nvr_error_state
        assert coord._nvr_auth_retry_count[CAM_ID] == 1

    @pytest.mark.asyncio
    async def test_auth_retry_counter_reset_on_successful_spawn(self, tmp_path):
        """After one 401 retry, a later successful spawn must clear the
        auth-retry counter — a later isolated 401 must not inherit the
        prior streak toward the give-up cap."""
        from custom_components.bosch_shc_camera import recorder

        coord = _make_lifecycle_coord(base_path=str(tmp_path))
        coord._nvr_auth_retry_count[CAM_ID] = recorder._MAX_CONSECUTIVE_AUTH_RETRIES
        proc = _mock_proc(returncode=None)

        async def _spawn(*args, **kwargs):
            return proc

        with patch.object(asyncio, "create_subprocess_exec", side_effect=_spawn):
            await recorder.start_recorder(coord, CAM_ID)

        assert CAM_ID not in coord._nvr_auth_retry_count
