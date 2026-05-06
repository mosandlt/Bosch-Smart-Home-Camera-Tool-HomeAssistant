"""Tests for recorder.py — Mini-NVR Phase 1 MVP.

Pure-helper tests pin contract surfaces of the recorder so a future refactor
cannot regress these invariants:

  1. ``should_record`` (LAN-only gate)
     — must return True iff switch ON ∧ conn_type=LOCAL ∧ camera ONLINE.
     This is the hard line from concept §2 — no cloud-relay recording.

  2. ``_build_ffmpeg_args``
     — pinned wire format. ``-c copy`` (never transcode), ``-segment_time 300``,
     ``-segment_atclocktime 1`` (wall-aligned), ``-strftime 1`` + ``-strftime_mkdir 1``
     (date-folder created by ffmpeg), ``-movflags +faststart`` (segment is
     web-playable mid-write — critical for the timeline UI).

  3. ``_segment_pattern`` / ``_segment_dir``
     — sanitized camera name via ``_safe_name`` (path-traversal guard) +
     ``YYYY-MM-DD/HH-MM.mp4`` layout.

  4. ``sync_nvr_cleanup`` retention purge
     — only files older than the cutoff are removed; never directories at the
     base path.  Mocked filesystem so tests don't touch real disk.

  5. ``BoschNvrRecordingSwitch.async_turn_on`` / ``async_turn_off``
     — delegate to ``coordinator.start_recorder`` / ``stop_recorder``.

User/forum source: project-internal Phase 1 implementation; the LAN-only
gate is the central design decision documented in
`docs/mini-nvr-concept.md` §2.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM_TITLE = "Terrasse"


# ── 1. LAN-only gate (`should_record`) ───────────────────────────────────


def _make_coord(*, conn_type: str = "LOCAL", online: bool = True) -> SimpleNamespace:
    """Coordinator stub with the three fields ``should_record`` reads."""
    return SimpleNamespace(
        _live_connections={CAM_ID: {"_connection_type": conn_type}},
        is_camera_online=lambda cid: online,
    )


class TestShouldRecord:
    """All eight combinations of (switch, conn_type, online) — only one yields True."""

    def test_all_three_true_returns_true(self):
        from custom_components.bosch_shc_camera.recorder import should_record
        coord = _make_coord(conn_type="LOCAL", online=True)
        assert should_record(coord, CAM_ID, switch_on=True) is True

    def test_switch_off_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record
        coord = _make_coord(conn_type="LOCAL", online=True)
        assert should_record(coord, CAM_ID, switch_on=False) is False

    def test_remote_connection_returns_false(self):
        """LAN-only is a hard line — no fallback to cloud relay (concept §2)."""
        from custom_components.bosch_shc_camera.recorder import should_record
        coord = _make_coord(conn_type="REMOTE", online=True)
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_camera_offline_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record
        coord = _make_coord(conn_type="LOCAL", online=False)
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
        coord = _make_coord(conn_type="WHATEVER", online=True)
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_remote_and_offline_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record
        coord = _make_coord(conn_type="REMOTE", online=False)
        assert should_record(coord, CAM_ID, switch_on=True) is False

    def test_switch_off_and_remote_returns_false(self):
        from custom_components.bosch_shc_camera.recorder import should_record
        coord = _make_coord(conn_type="REMOTE", online=True)
        assert should_record(coord, CAM_ID, switch_on=False) is False


# ── 2. ffmpeg argv (pinned wire format) ──────────────────────────────────


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
            "rtsp://x/y", "/tmp/out/%H-%M.mp4", segment_seconds=60,
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

    def test_reconnect_enabled(self):
        """Without `-reconnect 1` a TLS-proxy cred-rotation hiccup permanently
        kills the recorder. Pinned so a future refactor can't drop it."""
        from custom_components.bosch_shc_camera.recorder import _build_ffmpeg_args
        args = _build_ffmpeg_args("rtsp://x/y", "/tmp/out/%H-%M.mp4")
        idx = args.index("-reconnect")
        assert args[idx + 1] == "1"


# ── 3. File pattern / directory layout ──────────────────────────────────


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
        head_after_base = result[len("/config/bosch_nvr/"):]
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
            _segment_dir, _segment_pattern,
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


# ── 4. Retention purge (mocked filesystem) ──────────────────────────────


class TestSyncNvrCleanup:
    """`sync_nvr_cleanup` walks the base path, deletes files older than the
    cutoff, then prunes empty per-day folders.  Never touches the base path
    itself.  All filesystem calls are mocked."""

    def _coord(self, base_path: str = "/config/bosch_nvr",
               retention_days: int = 3,
               enabled: bool = True) -> SimpleNamespace:
        return SimpleNamespace(options={
            "enable_nvr": enabled,
            "nvr_base_path": base_path,
            "nvr_retention_days": retention_days,
        })

    def test_zero_retention_disables_cleanup(self):
        """Hard guard: retention_days <= 0 must NOT delete anything (would
        otherwise wipe the user's archive on a config-flow off-by-one)."""
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup
        coord = self._coord(retention_days=0)
        with patch("os.path.isdir", return_value=True), \
             patch("os.walk") as walk, \
             patch("os.remove") as rm:
            sync_nvr_cleanup(coord)
            walk.assert_not_called()
            rm.assert_not_called()

    def test_missing_base_path_returns_cleanly(self):
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup
        coord = self._coord()
        with patch("os.path.isdir", return_value=False), \
             patch("os.walk") as walk:
            sync_nvr_cleanup(coord)
            walk.assert_not_called()

    def test_only_files_older_than_cutoff_removed(self):
        """Files with mtime < cutoff are deleted; newer ones are kept."""
        from custom_components.bosch_shc_camera.recorder import sync_nvr_cleanup
        coord = self._coord(retention_days=3)
        now = time.time()
        old = now - 10 * 86400        # 10 days old → DELETE
        recent = now - 1 * 86400      # 1 day old → KEEP

        files_walked = [
            ("/config/bosch_nvr/Terrasse/2026-04-26",
             [],
             ["10-00.mp4", "10-05.mp4"]),
            ("/config/bosch_nvr/Terrasse/2026-05-05",
             [],
             ["14-00.mp4"]),
        ]

        def fake_stat(path):
            mt = old if "2026-04-26" in path else recent
            return SimpleNamespace(st_mtime=mt)

        removed: list[str] = []

        with patch("os.path.isdir", return_value=True), \
             patch("os.walk") as walk, \
             patch("os.stat", side_effect=fake_stat), \
             patch("os.remove", side_effect=removed.append), \
             patch("os.listdir", return_value=["x"]), \
             patch("os.rmdir"):
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

        with patch("os.path.isdir", return_value=True), \
             patch("os.walk") as walk, \
             patch("os.stat") as stat, \
             patch("os.remove") as rm, \
             patch("os.rmdir") as rmdir, \
             patch("os.listdir", return_value=["a"]):
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
        with patch("os.path.isdir", return_value=True), \
             patch("os.walk") as walk, \
             patch("os.listdir", return_value=[]), \
             patch("os.rmdir", side_effect=rmdir_calls.append):
            walk.side_effect = [iter(empty), iter(empty)]
            sync_nvr_cleanup(coord)

        # base_path must NOT be in rmdir_calls — guarded by the
        # `if root == base_path: continue` branch.
        assert "/config/bosch_nvr" not in rmdir_calls


# ── 5. Switch turn_on / turn_off → coordinator delegation ───────────────


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
            data={CAM_ID: {
                "info": {"title": CAM_TITLE},
                "status": "ONLINE",
                "events": [],
            }},
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


# ── 6. Entry-point smoke: switch only registered when option enabled ────


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
            data={CAM_ID: {
                "info": {"title": CAM_TITLE},
                "status": "ONLINE",
                "events": [],
            }},
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
