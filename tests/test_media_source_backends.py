"""Tests for `media_source.py` backends + helpers (Round 3).

`test_media_source_helpers.py` covers the small pure helpers
(`_safe_join`, `_is_macos_junk`, `_parse_filename`, `_enabled_sources`).
This file goes after the bigger units:

  - `_LocalBackend` — list_cameras / list_dates / list_events / resolve
    against a real tmp_path (no mocks needed for filesystem reads).
  - `_NvrBackend` — same pattern for Mini-NVR continuous recordings
    (Camera/YYYY-MM-DD/HH-MM.mp4 layout).
  - `_format_event_title` — pure string formatter.
  - `_node` — the BrowseMediaSource builder used everywhere in
    `BoschCameraMediaSource._browse`.
  - `_entry_title` — config entry title resolver.
  - `BoschCameraMediaSource.async_resolve_media` — URL builder for
    play requests.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── _LocalBackend ────────────────────────────────────────────────────────


class TestLocalBackendListCameras:
    def test_empty_dir_returns_empty(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path))
        assert b.list_cameras() == []

    def test_missing_dir_returns_empty(self, tmp_path):
        """Backend constructed with a path that doesn't exist must
        return [], not crash. Defensive against user typos in
        download_path."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path / "does-not-exist"))
        assert b.list_cameras() == []

    def test_lists_cameras_alphabetically_case_insensitive(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "Zebra").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / "Beta").mkdir()
        b = _LocalBackend(str(tmp_path))
        # Case-insensitive sort
        assert b.list_cameras() == ["alpha", "Beta", "Zebra"]

    def test_skips_macos_junk(self, tmp_path):
        """`._.DS_Store` and similar macOS metadata dirs must not
        appear as fake camera entries."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "Real-Cam").mkdir()
        (tmp_path / ".DS_Store").mkdir()
        (tmp_path / "._Real-Cam").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_cameras() == ["Real-Cam"]

    def test_skips_files_only_dirs(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "loose-file.txt").write_text("x")
        b = _LocalBackend(str(tmp_path))
        assert b.list_cameras() == []


class TestLocalBackendListDates:
    def test_groups_files_by_date(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = tmp_path / "Terrasse"
        cam.mkdir()
        # Filename pattern: <Camera>_<YYYY-MM-DD>_<HH-MM-SS>_<EventType>.<ext>
        (cam / "Terrasse_2026-05-04_10-30-00_MOVEMENT_A1.jpg").write_text("x")
        (cam / "Terrasse_2026-05-04_10-31-00_MOVEMENT_A2.mp4").write_text("x")
        (cam / "Terrasse_2026-05-03_09-00-00_AUDIO_A3.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        # Reverse-sorted by date (newest first)
        assert b.list_dates("Terrasse") == ["2026-05-04", "2026-05-03"]

    def test_unknown_camera_returns_empty(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates("NonExistent") == []

    def test_skips_unparseable_filenames(self, tmp_path):
        """Loose / hand-named files in the camera dir don't break the
        date listing — they're silently skipped."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "random-file.jpg").write_text("x")
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates("Cam") == ["2026-05-04"]

    def test_traversal_camera_name_returns_empty(self, tmp_path):
        """`../etc` style camera name must not escape the base dir
        — `_safe_join` gates this."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates("../../etc") == []


class TestLocalBackendListEvents:
    def test_groups_jpg_and_mp4_into_one_event(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.mp4").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        # One event tuple, video preferred as primary, jpg as thumbnail
        assert len(events) == 1
        preferred, image, parsed = events[0]
        assert preferred.endswith(".mp4")
        assert image.endswith(".jpg")
        assert parsed["date"] == "2026-05-04"

    def test_image_only_event(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_AUDIO_C1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        preferred, image, _ = events[0]
        assert preferred.endswith(".jpg")
        assert image == preferred

    def test_video_only_event_image_none(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_AUDIO_C2.mp4").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        preferred, image, _ = events[0]
        assert preferred.endswith(".mp4")
        assert image is None

    def test_filters_other_dates(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        (cam / "Cam_2026-05-03_10-00-00_MOVEMENT_D1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        assert len(events) == 1
        # Only the date=2026-05-04 entry came through
        assert events[0][2]["date"] == "2026-05-04"

    def test_sorted_newest_first(self, tmp_path):
        """Within a date, events appear newest-first (reverse stem sort
        works because the timestamp is in the stem)."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg").write_text("x")
        (cam / "Cam_2026-05-04_15-30-00_AUDIO_E1.jpg").write_text("x")
        (cam / "Cam_2026-05-04_08-00-00_MOVEMENT_F1.jpg").write_text("x")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events("Cam", "2026-05-04")
        # Sort by stem reverse → 15:30 first, then 10:00, then 08:00
        assert "15-30-00" in events[0][0]
        assert "10-00-00" in events[1][0]
        assert "08-00-00" in events[2][0]


class TestLocalBackendResolve:
    def test_resolve_existing_file(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "Cam").mkdir()
        target = tmp_path / "Cam" / "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg"
        target.write_text("x")
        b = _LocalBackend(str(tmp_path))
        out = b.resolve("Cam", "Cam_2026-05-04_10-00-00_MOVEMENT_B1.jpg")
        assert out == target

    def test_resolve_traversal_blocked(self, tmp_path):
        """Path traversal via `..` must be blocked even when the target
        file exists outside the base."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path / "base"))
        (tmp_path / "base").mkdir()
        # Try to escape the base dir
        out = b.resolve("..", "etc", "passwd")
        assert out is None

    def test_resolve_nonexistent_file_returns_none(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path))
        out = b.resolve("Cam", "missing.jpg")
        assert out is None

    def test_resolve_directory_returns_none(self, tmp_path):
        """Resolve must only return file paths — directory targets
        return None (caller wants to play a media file)."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "Cam").mkdir()
        b = _LocalBackend(str(tmp_path))
        # "Cam" exists but is a dir
        out = b.resolve("Cam")
        assert out is None


# ── _NvrBackend ──────────────────────────────────────────────────────────


class TestNvrBackend:
    def test_list_cameras_sorted(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _NvrBackend
        (tmp_path / "Garten").mkdir()
        (tmp_path / "Terrasse").mkdir()
        (tmp_path / ".DS_Store").mkdir()
        b = _NvrBackend(str(tmp_path))
        assert b.list_cameras() == ["Garten", "Terrasse"]

    def test_list_dates_only_yyyy_mm_dd_dirs(self, tmp_path):
        """Only `YYYY-MM-DD` named dirs are date entries — random
        sub-dirs (e.g. `_staging`, `_failed`) must be excluded."""
        from custom_components.bosch_shc_camera.media_source import _NvrBackend
        cam = tmp_path / "Cam"
        cam.mkdir()
        (cam / "2026-05-04").mkdir()
        (cam / "2026-05-03").mkdir()
        (cam / "_staging").mkdir()  # NVR scratch dir
        (cam / "_failed").mkdir()
        b = _NvrBackend(str(tmp_path))
        # Reverse-sorted, junk excluded
        assert b.list_dates("Cam") == ["2026-05-04", "2026-05-03"]

    def test_list_dates_unknown_camera_returns_empty(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _NvrBackend
        b = _NvrBackend(str(tmp_path))
        assert b.list_dates("NoCam") == []

    def test_list_segments_returns_filename_and_human_label(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _NvrBackend
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        # NVR segment naming: HH-MM.mp4
        (date / "10-30.mp4").write_text("x")
        (date / "11-00.mp4").write_text("x")
        b = _NvrBackend(str(tmp_path))
        out = b.list_segments("Cam", "2026-05-04")
        # Reverse-sorted, label is HH:MM (not HH-MM)
        assert out == [
            ("11-00.mp4", "11:00"),
            ("10-30.mp4", "10:30"),
        ]

    def test_list_segments_skips_non_matching_files(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _NvrBackend
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        (date / "10-30.mp4").write_text("x")
        (date / "random.txt").write_text("x")
        (date / "10-30.tmp").write_text("x")  # ffmpeg in-progress
        b = _NvrBackend(str(tmp_path))
        out = b.list_segments("Cam", "2026-05-04")
        assert out == [("10-30.mp4", "10:30")]

    def test_resolve_validates_date_and_filename(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _NvrBackend
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        (date / "10-30.mp4").write_text("x")
        b = _NvrBackend(str(tmp_path))
        out = b.resolve("Cam", "2026-05-04", "10-30.mp4")
        assert out is not None
        # Bad date format rejected
        assert b.resolve("Cam", "2026/05/04", "10-30.mp4") is None
        # Bad filename rejected
        assert b.resolve("Cam", "2026-05-04", "evil.exe") is None
        # Traversal rejected
        assert b.resolve("..", "2026-05-04", "10-30.mp4") is None

    def test_resolve_missing_file_returns_none(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _NvrBackend
        cam = tmp_path / "Cam"
        date = cam / "2026-05-04"
        date.mkdir(parents=True)
        b = _NvrBackend(str(tmp_path))
        out = b.resolve("Cam", "2026-05-04", "10-30.mp4")
        assert out is None


# ── _format_event_title ──────────────────────────────────────────────────


class TestFormatEventTitle:
    def test_movement_event(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title
        parsed = {"date": "2026-05-04", "time": "10-30-15", "etype": "MOVEMENT", "camera": "Terrasse"}
        out = _format_event_title(parsed)
        # Format must include human-readable time + event type + camera
        assert "MOVEMENT" in out
        assert "10:30:15" in out
        assert "Terrasse" in out

    def test_audio_event(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title
        parsed = {"date": "2026-05-04", "time": "10-30-15", "etype": "AUDIO", "camera": "Terrasse"}
        out = _format_event_title(parsed)
        assert "AUDIO" in out

    def test_unknown_type_passes_through(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title
        parsed = {"date": "2026-05-04", "time": "10-30-15", "etype": "UNKNOWN_EVT", "camera": "Terrasse"}
        # Must not crash — just include the literal type
        out = _format_event_title(parsed)
        assert isinstance(out, str)
        assert "UNKNOWN_EVT" in out


# ── _entry_title ─────────────────────────────────────────────────────────


class TestEntryTitle:
    def test_returns_entry_title_when_loaded(self):
        from custom_components.bosch_shc_camera.media_source import _entry_title
        entry = SimpleNamespace(entry_id="01ABC", title="My Bosch")
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_entry=MagicMock(return_value=entry),
            ),
        )
        assert _entry_title(hass, "01ABC") == "My Bosch"

    def test_falls_back_to_entry_id_short_when_missing(self):
        from custom_components.bosch_shc_camera.media_source import _entry_title
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_get_entry=MagicMock(return_value=None),
            ),
        )
        out = _entry_title(hass, "01ABCDEFGHJKLMNOPQRSTUV0")
        # Some short form of the entry_id — pin only that it's a string
        assert isinstance(out, str)
        assert len(out) > 0


# ── _node ────────────────────────────────────────────────────────────────


class TestNode:
    def test_default_directory_node(self):
        from custom_components.bosch_shc_camera.media_source import _node
        out = _node(identifier="root", title="Root")
        assert out.identifier == "root"
        assert out.title == "Root"
        assert out.can_play is False
        assert out.can_expand is True

    def test_playable_leaf(self):
        from custom_components.bosch_shc_camera.media_source import _node
        from homeassistant.components.media_player import MediaClass
        out = _node(
            identifier="L:01ENT/Cam/2026-05-04/file.mp4",
            title="10:30",
            media_class=MediaClass.VIDEO,
            media_content_type="video/mp4",
            can_play=True,
            can_expand=False,
        )
        assert out.can_play is True
        assert out.can_expand is False
        assert out.media_content_type == "video/mp4"

    def test_thumbnail_propagated(self):
        from custom_components.bosch_shc_camera.media_source import _node
        out = _node(
            identifier="x", title="x",
            thumbnail="https://example/thumb.jpg",
        )
        assert out.thumbnail == "https://example/thumb.jpg"


# ── BoschCameraMediaSource.async_resolve_media ───────────────────────────


class TestAsyncResolveMedia:
    @pytest.mark.asyncio
    async def test_root_unresolvable(self):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        from homeassistant.components.media_source.error import Unresolvable
        src = BoschCameraMediaSource(SimpleNamespace())
        item = SimpleNamespace(identifier=None)
        with pytest.raises(Unresolvable):
            await src.async_resolve_media(item)

    @pytest.mark.asyncio
    async def test_resolves_to_view_url_with_mime(self):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        src = BoschCameraMediaSource(SimpleNamespace())
        item = SimpleNamespace(identifier="L:01ENT/Cam/2026-05-04/file.mp4")
        out = await src.async_resolve_media(item)
        # MIME inferred from extension
        assert out.mime_type == "video/mp4"
        assert "L:01ENT/Cam/2026-05-04/file.mp4" in out.url

    @pytest.mark.asyncio
    async def test_unknown_extension_falls_back_to_octet_stream(self):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        src = BoschCameraMediaSource(SimpleNamespace())
        item = SimpleNamespace(identifier="L:01ENT/Cam/file.unknownext")
        out = await src.async_resolve_media(item)
        assert out.mime_type == "application/octet-stream"
