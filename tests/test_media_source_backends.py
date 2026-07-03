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

from tests.source_match import assert_in_source

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

    def test_skips_underscore_dirs(self, tmp_path):
        """B13-5 regression: _staging / _failed NVR scratch dirs must not appear
        as camera tiles in the Media Browser for _LocalBackend."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        (tmp_path / "Terrasse").mkdir()
        (tmp_path / "Innenbereich").mkdir()
        (tmp_path / "_staging").mkdir()
        (tmp_path / "_failed").mkdir()
        b = _LocalBackend(str(tmp_path))
        result = b.list_cameras()
        assert "_staging" not in result, "_staging must be filtered from camera list"
        assert "_failed" not in result, "_failed must be filtered from camera list"
        assert result == ["Innenbereich", "Terrasse"]

    def test_skips_files_only_dirs(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        (tmp_path / "loose-file.txt").write_text("x")
        b = _LocalBackend(str(tmp_path))
        assert b.list_cameras() == []

    def test_year_first_folders_appear_in_camera_list(self, tmp_path):
        """Year-first folders (e.g. "2026/") must appear in list_cameras() alongside
        real camera folders so users can browse legacy recordings without restructuring.

        Regression fix (simon42 / Andreas74 2026-05-08): previously these were
        filtered out and reported as hidden, leaving legacy recordings inaccessible
        in the Media Browser.
        """
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        (tmp_path / "Terrasse").mkdir()
        (tmp_path / "Innenbereich").mkdir()
        (tmp_path / "2026").mkdir()
        (tmp_path / "2025").mkdir()
        b = _LocalBackend(str(tmp_path))
        cameras = b.list_cameras()
        assert "Terrasse" in cameras, "real camera must appear"
        assert "Innenbereich" in cameras, "real camera must appear"
        assert "2026" in cameras, (
            "year-first folder must appear — browseable as 2026→month→day→events"
        )
        assert "2025" in cameras, "year-first folder must appear"

    def test_list_year_first_months(self, tmp_path):
        """list_year_first_months returns 2-digit month dirs under base/YYYY/."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        year_dir = tmp_path / "2026"
        (year_dir / "03").mkdir(parents=True)
        (year_dir / "04").mkdir()
        (year_dir / "junk").mkdir()  # non-month dir must be excluded
        (year_dir / "file.mp4").write_text("x")  # file must be excluded
        b = _LocalBackend(str(tmp_path))
        months = b.list_year_first_months("2026")
        assert months == ["04", "03"], f"expected newest-first months, got {months}"

    def test_list_year_first_days(self, tmp_path):
        """list_year_first_days returns 2-digit day dirs under base/YYYY/MM/."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        day_dir = tmp_path / "2026" / "03"
        (day_dir / "25").mkdir(parents=True)
        (day_dir / "26").mkdir()
        (day_dir / "notaday").mkdir()
        b = _LocalBackend(str(tmp_path))
        days = b.list_year_first_days("2026", "03")
        assert days == ["26", "25"], f"expected newest-first days, got {days}"

    def test_list_year_first_events(self, tmp_path):
        """list_year_first_events returns (filename, image, parsed) tuples from base/YYYY/MM/DD/."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        day = tmp_path / "2026" / "03" / "25"
        day.mkdir(parents=True)
        (day / "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4").write_text("x")
        (day / "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.jpg").write_text("x")
        (day / "._macjunk").write_text("x")  # must be filtered
        b = _LocalBackend(str(tmp_path))
        events = b.list_year_first_events("2026", "03", "25")
        assert len(events) == 1, f"expected 1 event, got {len(events)}"
        fname, image, parsed = events[0]
        assert fname.endswith(".mp4"), "video preferred over image"
        assert image is not None, "jpg thumbnail must be linked"
        assert parsed["camera"] == "Garten"


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

    def test_resolve_year_first_4_part_path(self, tmp_path):
        """resolve(year, month, day, filename) must return the file for year-first layout.

        _serve_local accepts len(tail)==4, which maps to (year, month, day, filename) —
        the year-first path where the year dir sits directly at the NAS/local root
        with no camera prefix.  Without len(tail)==4 in the allow-list the handler
        raises HTTPNotFound for every year-first playback attempt.
        Fix: v11.0.19 (simon42/Andreas74 2026-05-08).
        """
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        year_dir = tmp_path / "2026" / "03" / "25"
        year_dir.mkdir(parents=True)
        fname = "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4"
        (year_dir / fname).write_text("x")
        b = _LocalBackend(str(tmp_path))
        result = b.resolve("2026", "03", "25", fname)
        assert result is not None, (
            "4-part year-first resolve must return a Path, not None"
        )
        assert result.is_file(), "resolved 4-part path must point at a real file"


# ── _NvrBackend ──────────────────────────────────────────────────────────


class TestNvrBackend:
    def test_list_cameras_sorted(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _NvrBackend

        (tmp_path / "Garten").mkdir()
        (tmp_path / "Terrasse").mkdir()
        (tmp_path / ".DS_Store").mkdir()
        b = _NvrBackend(str(tmp_path))
        assert b.list_cameras() == ["Garten", "Terrasse"]

    def test_list_cameras_skips_underscore_dirs(self, tmp_path):
        """B13-5 regression: _staging / _failed NVR internal dirs must not
        appear as camera tiles in the Media Browser for _NvrBackend."""
        from custom_components.bosch_shc_camera.media_source import _NvrBackend

        (tmp_path / "Garten").mkdir()
        (tmp_path / "Terrasse").mkdir()
        (tmp_path / "_staging").mkdir()
        (tmp_path / "_failed").mkdir()
        b = _NvrBackend(str(tmp_path))
        result = b.list_cameras()
        assert "_staging" not in result, "_staging must be filtered"
        assert "_failed" not in result, "_failed must be filtered"
        assert result == ["Garten", "Terrasse"]

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

        parsed = {
            "date": "2026-05-04",
            "time": "10-30-15",
            "etype": "MOVEMENT",
            "camera": "Terrasse",
        }
        out = _format_event_title(parsed)
        # Format must include human-readable time + event type + camera
        assert "MOVEMENT" in out
        assert "10:30:15" in out
        assert "Terrasse" in out

    def test_audio_event(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title

        parsed = {
            "date": "2026-05-04",
            "time": "10-30-15",
            "etype": "AUDIO",
            "camera": "Terrasse",
        }
        out = _format_event_title(parsed)
        assert "AUDIO" in out

    def test_unknown_type_passes_through(self):
        from custom_components.bosch_shc_camera.media_source import _format_event_title

        parsed = {
            "date": "2026-05-04",
            "time": "10-30-15",
            "etype": "UNKNOWN_EVT",
            "camera": "Terrasse",
        }
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
        from homeassistant.components.media_player import MediaClass

        from custom_components.bosch_shc_camera.media_source import _node

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
            identifier="x",
            title="x",
            thumbnail="https://example/thumb.jpg",
        )
        assert out.thumbnail == "https://example/thumb.jpg"


# ── BoschCameraMediaSource.async_resolve_media ───────────────────────────


class TestAsyncResolveMedia:
    @pytest.mark.asyncio
    async def test_root_unresolvable(self):
        from homeassistant.components.media_source.error import Unresolvable

        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )

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


# ── LocalBackend camera_first year/month/day tree ────────────────────────────


class TestLocalBackendCameraFirst:
    """_LocalBackend with folder_pattern starting with {camera} → year/month/day tree.

    Regression: reported by Georg (simon42, 2026-05-08): files saved via
    sync_local_save land in camera/2026/05/08/ but the serve view routed
    camera/year/… paths to the SMB backend (kind="S"), returning 404 for
    every playback attempt. Fix: prefer Local when no SMB source is configured.
    """

    def test_list_years_returns_four_digit_dirs(self, tmp_path):
        """list_years must return only dirs matching ^\\d{4}$ (not full YYYY-MM-DD names)."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        cam = tmp_path / "Terrasse"
        (cam / "2026").mkdir(parents=True)
        (cam / "2025").mkdir()
        (cam / "2026-05-08").mkdir()  # must NOT appear as a year
        b = _LocalBackend(str(tmp_path))
        years = b.list_years("Terrasse")
        assert years == ["2026", "2025"], (
            f"Expected only 4-digit year dirs, got {years}"
        )

    def test_list_months_returns_two_digit_dirs(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        cam = tmp_path / "Terrasse"
        year_dir = cam / "2026"
        (year_dir / "05").mkdir(parents=True)
        (year_dir / "04").mkdir()
        (year_dir / "not-a-month").mkdir()
        b = _LocalBackend(str(tmp_path))
        months = b.list_months("Terrasse", "2026")
        assert months == ["05", "04"], f"Expected two-digit month dirs, got {months}"

    def test_list_days_returns_two_digit_dirs(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        cam = tmp_path / "Terrasse"
        month_dir = cam / "2026" / "05"
        (month_dir / "08").mkdir(parents=True)
        (month_dir / "07").mkdir()
        b = _LocalBackend(str(tmp_path))
        days = b.list_days("Terrasse", "2026", "05")
        assert days == ["08", "07"], f"Expected two-digit day dirs, got {days}"

    def test_list_events_dated_reads_files_from_day_dir(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        cam = tmp_path / "Terrasse"
        day_dir = cam / "2026" / "05" / "08"
        day_dir.mkdir(parents=True)
        (day_dir / "Terrasse_2026-05-08_10-30-00_MOVEMENT_ABCD1234.jpg").write_bytes(
            b"\xff\xd8"
        )
        b = _LocalBackend(str(tmp_path))
        events = b.list_events_dated("Terrasse", "2026", "05", "08")
        assert len(events) == 1, "Expected 1 event in the day directory"
        fname, _thumb, parsed = events[0]
        assert "MOVEMENT" in fname, (
            f"Event filename should contain event type, got {fname}"
        )
        assert parsed["date"] == "2026-05-08", f"Parsed date wrong: {parsed['date']}"

    def test_resolve_camera_first_path(self, tmp_path):
        """resolve(camera, year, month, day, filename) must return the correct file path."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        cam = tmp_path / "Terrasse"
        day_dir = cam / "2026" / "05" / "08"
        day_dir.mkdir(parents=True)
        fname = "Terrasse_2026-05-08_10-30-00_MOVEMENT_ABCD1234.jpg"
        (day_dir / fname).write_bytes(b"\xff\xd8")
        b = _LocalBackend(str(tmp_path))
        resolved = b.resolve("Terrasse", "2026", "05", "08", fname)
        assert resolved is not None, (
            "resolve() must return a Path for a camera-first file"
        )
        assert resolved.is_file(), "Resolved path must be an actual file"

    def test_camera_first_property_true_for_default_pattern(self):
        """Default folder_pattern={camera}/{year}/{month}/{day} → camera_first=True."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        b = _LocalBackend("/tmp")  # default pattern
        assert b.camera_first is True, (
            "Default folder_pattern must make camera_first=True; "
            "sync_local_save uses the same default and creates camera/year/month/day/"
        )


class TestViewRoutingCameraFirstLocal:
    """Pin the routing fix: local camera-first paths must NOT be sent to SMB.

    Before the fix (pre-v11.0.18): parts[1] matching ^\\d{4}$ always set kind='S',
    so _find_source(entry_id, 'S') returned None for users without SMB → HTTP 404.
    After the fix: kind falls through to 'L' when no SMB source is configured.
    """

    def test_source_routing_prefers_smb_only_when_smb_configured(self):
        """When parts[1] is a year AND SMB is not configured, routing must pick Local.

        This is a structural pin of the fix at BoschCameraMediaView.get — reads the
        source code and asserts the disambiguation logic is present.
        """
        import inspect

        from custom_components.bosch_shc_camera.media_source import BoschCameraMediaView

        src = inspect.getsource(BoschCameraMediaView.get)
        # The fix must check for an SMB source before defaulting to "S"
        assert_in_source(
            src, "_find_source", '"S"', '"L"'
        )  # BoschCameraMediaView.get must disambiguate Local vs SMB camera-first paths via _find_source — without this, Local camera-first files (camera/year/month/day/file) are incorrectly routed to SMB and return HTTP 404 (georg, simon42, 2026-05-08)
        # Specifically, the SMB preference expression must exist (not just hardcode "S").
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job
        # (it does blocking Path.exists()/mkdir()/is_dir() internally) instead of
        # being called directly on the event loop.
        assert_in_source(  # Routing must use _find_source (via the executor) to check if SMB is configured before choosing kind='S'
            src,
            '"S" if await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "S")',
            "'S' if await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, 'S')",
            any_of=True,
        )

    def test_legacy_flat_path_routes_to_local_when_local_configured(self):
        """Legacy flat identifier {camera}/{filename} routes to kind='L' when Local exists.

        A camera name like 'Terrasse' never matches _YEAR_RE, so the path falls through
        all year/NVR heuristics to the else branch. The else branch now checks for a
        Local source first, so users with a local download_path get kind='L'.
        The else branch must NOT unconditionally hardcode kind='S'.
        """
        import inspect

        from custom_components.bosch_shc_camera.media_source import BoschCameraMediaView

        src = inspect.getsource(BoschCameraMediaView.get)
        # The else branch must prefer Local via _find_source (not hardcode 'L' or 'S').
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job.
        assert_in_source(  # The else-branch must check _find_source for Local before choosing kind. Hardcoding kind='L' would break SMB-only users; hardcoding kind='S' would break Local-only users.
            src,
            'await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "L")',
        )
        # Must also NOT unconditionally hardcode kind='S' in the else branch
        lines = src.splitlines()
        in_else = False
        for line in lines:
            if line.strip().startswith("else:"):
                in_else = True
            if in_else and 'kind = "S"' in line and "_find_source" not in line:
                assert False, (
                    "else-branch must not unconditionally set kind='S' — that would break Local users"
                )

    def test_camera_first_and_legacy_flat_coexist(self, tmp_path):
        """_LocalBackend must serve BOTH old flat files AND new year/month/day files
        from the same base directory (Georg's mixed-layout scenario).

        Old files are flat in camera/. New files are in camera/2026/05/08/.
        Both must be resolvable via backend.resolve().
        """
        from custom_components.bosch_shc_camera.media_source import _LocalBackend

        cam = tmp_path / "Terrasse"
        # Old flat file
        flat_fname = "Terrasse_2026-05-07_09-00-00_MOVEMENT_OLD00001.jpg"
        cam.mkdir()
        (cam / flat_fname).write_bytes(b"\xff\xd8")
        # New camera-first file
        day_dir = cam / "2026" / "05" / "08"
        day_dir.mkdir(parents=True)
        new_fname = "Terrasse_2026-05-08_10-30-00_MOVEMENT_ABCD1234.jpg"
        (day_dir / new_fname).write_bytes(b"\xff\xd8")

        b = _LocalBackend(str(tmp_path))
        # Flat file → resolve(camera, filename)
        flat_resolved = b.resolve("Terrasse", flat_fname)
        assert flat_resolved is not None and flat_resolved.is_file(), (
            "resolve(camera, flat_filename) must work for legacy flat files"
        )
        # Camera-first file → resolve(camera, year, month, day, filename)
        new_resolved = b.resolve("Terrasse", "2026", "05", "08", new_fname)
        assert new_resolved is not None and new_resolved.is_file(), (
            "resolve(camera, year, month, day, filename) must work for camera-first files"
        )

    def test_smb_date_first_single_source_still_routes_to_smb(self):
        """When parts[0] is a year (SMB date-first single-source), kind must still be 'S'.

        The disambiguation fix must NOT change how SMB date-first paths are routed.
        These paths have parts[0] = '2026' (a 4-digit year), which triggers the
        EARLIER heuristic before the camera-first disambiguation branch is reached.
        """
        import inspect

        from custom_components.bosch_shc_camera.media_source import BoschCameraMediaView

        src = inspect.getsource(BoschCameraMediaView.get)
        # The _YEAR_RE.match(head) branch must still unconditionally set kind='S'
        lines = src.splitlines()
        year_first_block = False
        for line in lines:
            stripped = line.strip()
            if "_YEAR_RE.match(head)" in stripped:
                year_first_block = True
            if (
                year_first_block
                and 'kind = "S"' in stripped
                and "_find_source" not in stripped
            ):
                break  # found the unconditional S assignment for date-first SMB
        else:
            assert False, (
                "The SMB date-first path (_YEAR_RE.match(head)) must still unconditionally "
                "set kind='S' — the disambiguation fix must only apply to camera/year/… paths"
            )

    def test_smb_camera_first_with_smb_configured_routes_to_smb(self):
        """camera/year/month/day/filename must route to SMB when an SMB source exists.

        FTP uploads land on the same NAS share and are browsed via SMB. The camera-first
        disambiguation must pick 'S' when _find_source finds an SMB backend, so FTP
        and SMB camera-first files are served correctly.
        """
        import inspect

        from custom_components.bosch_shc_camera.media_source import BoschCameraMediaView

        src = inspect.getsource(BoschCameraMediaView.get)
        # After the fix: the camera/year path picks 'S' when SMB is present.
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job.
        assert_in_source(  # camera-first disambiguation must choose kind='S' when _find_source returns SMB, so FTP-uploaded / SMB camera-first files are served correctly
            src,
            'kind = ("S" if await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "S") is not None else "L")',
        )

    def test_smb_flat_single_source_routes_to_smb_when_no_local(self):
        """Flat SMB file {camera}/{filename} must route to kind='S' when no Local source exists.

        Users with only a NAS share (no local download_path) and old flat files directly
        in the camera/ folder on the NAS would get HTTP 404 if the else-branch always
        hardcoded kind='L'. Fix: prefer Local when it exists, fall back to SMB.
        """
        import inspect

        from custom_components.bosch_shc_camera.media_source import BoschCameraMediaView

        src = inspect.getsource(BoschCameraMediaView.get)
        # The else branch must use _find_source to choose between L and S.
        # bug-hunt 2026-07-03: _find_source now runs via async_add_executor_job.
        assert_in_source(  # The else-branch (flat file fallback) must check for a Local source before defaulting to kind='L', so SMB-only users with flat NAS files are served
            src,
            'await self.hass.async_add_executor_job(_find_source, self.hass, entry_id, "L")',
        )

    def test_nvr_path_routes_to_nvr(self):
        """camera/YYYY-MM-DD/HH-MM.mp4 must always route to kind='N' (NVR).

        NVR paths use the full ISO-date format (2026-05-08) in parts[1], which matches
        _NVR_DATE_DIR_RE but NOT _YEAR_RE (has dashes). The NVR branch must fire before
        the else branch so that continuous-recording segments are served correctly.
        """
        import inspect

        from custom_components.bosch_shc_camera.media_source import BoschCameraMediaView

        src = inspect.getsource(BoschCameraMediaView.get)
        assert_in_source(  # BoschCameraMediaView.get must have an NVR branch checking _NVR_DATE_DIR_RE before the flat-file fallback, so NVR segments route to kind='N'
            src, "_NVR_DATE_DIR_RE.match(parts[1])"
        )
        # NVR must set kind='N' unconditionally (not via _find_source heuristic)
        lines = [line.strip() for line in src.splitlines()]
        nvr_block = False
        for line in lines:
            if "_NVR_DATE_DIR_RE.match(parts[1])" in line:
                nvr_block = True
            if nvr_block and 'kind = "N"' in line:
                break
        else:
            assert False, "NVR branch must set kind='N' after matching _NVR_DATE_DIR_RE"

    def test_explicit_kind_tokens_bypass_all_heuristics(self):
        """When the path starts with L, S, or N (multi-source), heuristics are skipped.

        This is the normal path for multi-source entries (both Local + SMB configured).
        Explicit tokens are never ambiguous, so no _find_source lookup is needed there.
        """
        import inspect

        from custom_components.bosch_shc_camera.media_source import BoschCameraMediaView

        src = inspect.getsource(BoschCameraMediaView.get)
        # The very first if-branch must handle explicit tokens without calling _find_source
        lines = src.splitlines()
        token_block = False
        for line in lines:
            stripped = line.strip()
            if 'head in ("L", "S", "N")' in stripped:
                token_block = True
            if token_block and "tail = parts[1:]" in stripped:
                break  # found the token branch — correctly peels the token and moves on
            if token_block and "_find_source" in stripped:
                assert False, (
                    "Explicit kind token branch must NOT call _find_source — "
                    "L/S/N tokens are unambiguous by design"
                )


# ── _SmbBackend year-first browse ────────────────────────────────────────


class TestSmbBackendYearFirst:
    """_SmbBackend year-first browse methods — mocked at _scandir_filtered boundary.

    Regression fix v11.0.19 (simon42/Andreas74 2026-05-08): year-first folders
    ('2026', '2025') were not browseable via SMB.  Fix: remove _YEAR_RE filter
    from list_cameras(); add list_year_first_months/days/events().
    """

    def _make_backend(self):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend

        hass = SimpleNamespace(data={})
        opts = {
            "smb_server": "nas.local",
            "smb_share": "Events",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "",
            "upload_protocol": "smb",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
        }
        return _SmbBackend(hass, opts)

    def test_list_cameras_includes_year_first_folders(self):
        """list_cameras() must return ALL dirs — including 4-digit year dirs.

        Previously _YEAR_RE filtered these out, leaving legacy recordings
        inaccessible for SMB/FTP users (same bug as for _LocalBackend).
        """
        from unittest.mock import patch

        b = self._make_backend()
        dirs = ["Terrasse", "2026", "Innenbereich", "2025"]
        with patch.object(b, "_scandir_filtered", return_value=iter(dirs)):
            result = b.list_cameras()
        assert "2026" in result, "year-first folder must appear in SMB list_cameras()"
        assert "Terrasse" in result, (
            "normal camera folder must appear in SMB list_cameras()"
        )
        assert result == sorted(dirs, key=str.casefold), (
            "SMB list_cameras() must be sorted case-insensitive"
        )

    def test_list_year_first_months_filters_by_date_dir_re(self):
        """list_year_first_months('2026') filters to 2-digit dirs only, newest-first."""
        from unittest.mock import patch

        b = self._make_backend()
        raw = ["03", "04", "junk", "not-a-month"]
        with patch.object(b, "_scandir_filtered", return_value=iter(raw)) as mock_scan:
            result = b.list_year_first_months("2026")
        mock_scan.assert_called_once_with("2026", want_dirs=True)
        assert result == ["04", "03"], (
            f"SMB list_year_first_months must return ['04','03'] newest-first, got {result}"
        )
        assert "junk" not in result, "non-month dir must be excluded"

    def test_list_year_first_days_filters_and_sorts(self):
        """list_year_first_days('2026', '03') returns 2-digit day dirs, newest-first."""
        from unittest.mock import patch

        b = self._make_backend()
        raw = ["25", "26", "notaday"]
        with patch.object(b, "_scandir_filtered", return_value=iter(raw)) as mock_scan:
            result = b.list_year_first_days("2026", "03")
        mock_scan.assert_called_once_with("2026", "03", want_dirs=True)
        assert result == ["26", "25"], (
            f"SMB list_year_first_days must return ['26','25'] newest-first, got {result}"
        )

    def test_list_year_first_events_groups_mp4_and_jpg(self):
        """list_year_first_events groups mp4+jpg into one event, video preferred."""
        from unittest.mock import patch

        b = self._make_backend()
        raw = [
            "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4",
            "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.jpg",
            "unparseable_random_name.txt",  # must be silently skipped
        ]
        with patch.object(b, "_scandir_filtered", return_value=iter(raw)) as mock_scan:
            result = b.list_year_first_events("2026", "03", "25")
        mock_scan.assert_called_once_with("2026", "03", "25", want_dirs=False)
        assert len(result) == 1, (
            f"SMB list_year_first_events must return 1 event (random.txt not parsed), got {len(result)}"
        )
        fname, image, parsed = result[0]
        assert fname.endswith(".mp4"), (
            "video must be preferred over image in SMB year-first events"
        )
        assert image is not None and image.endswith(".jpg"), (
            "jpg must be included as thumbnail"
        )
        assert parsed["camera"] == "Garten", f"parsed camera wrong: {parsed['camera']}"


# ── Browse handler year-first routing (structural) ────────────────────────


class TestBrowseYearFirstRouting:
    """Pin the browse handler's year-first detection in async_browse_media.

    Fix v11.0.19: camera=2026 must route to list_year_first_months, not
    list_years('2026'), which would return [] (no nested year dirs inside 2026/).
    """

    def test_browse_handler_calls_year_first_methods(self):
        """_browse_smb/_browse_local source must contain all three year-first method calls."""
        import inspect

        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )

        src_smb = inspect.getsource(BoschCameraMediaSource._browse_smb)
        src_local = inspect.getsource(BoschCameraMediaSource._browse_local)
        assert (
            "list_year_first_months" in src_smb or "list_year_first_months" in src_local
        ), (
            "_browse_smb or _browse_local must call list_year_first_months for '2026 → month' browsing"
        )
        assert (
            "list_year_first_days" in src_smb or "list_year_first_days" in src_local
        ), (
            "_browse_smb or _browse_local must call list_year_first_days for '2026 → month → day' browsing"
        )
        assert (
            "list_year_first_events" in src_smb or "list_year_first_events" in src_local
        ), (
            "_browse_smb or _browse_local must call list_year_first_events for year-first events"
        )

    def test_browse_handler_detects_year_with_year_re(self):
        """browse handler must use _YEAR_RE.match(camera) to detect year-first folders."""
        import inspect

        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )

        src_smb = inspect.getsource(BoschCameraMediaSource._browse_smb)
        src_local = inspect.getsource(BoschCameraMediaSource._browse_local)
        assert_in_source(  # browse handler (_browse_smb or _browse_local) must call _YEAR_RE.match(camera) to detect year-first folders at len(rest)==1/2/3 inside the camera_first block
            src_smb + src_local, "_YEAR_RE.match(camera)"
        )
