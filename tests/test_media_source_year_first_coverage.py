"""Tests for media_source.py year-first coverage gaps.

Pins the remaining uncovered lines in `_LocalBackend` year-first helpers
(lines 135, 141, 144, 150, 153, 156) and the SMB year-first browse branches
in `_browse_smb` (lines 924-928, 937-942, 972-985).

Bucket A of the coverage round. The year-first folder layout is:
    base/YYYY/MM/DD/<camera>_<date>_<time>_<type>_<id>.{mp4,jpg}

Helpers must return `[]` for path-traversal inputs (defense-in-depth via
`_safe_join`). The browse handler must dispatch to year-first methods
when `_YEAR_RE.match(camera)` and emit the correct children.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── _LocalBackend year-first None-branches ───────────────────────────────────


class TestLocalListYearFirstMonthsNoneBranches:
    """Pin line 135: `_safe_join(base, year) is None` → return []."""

    def test_path_traversal_year_returns_empty(self, tmp_path):
        """Year arg with `..` → _safe_join returns None → caller returns []."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_months("../../etc") == []

    def test_year_dir_not_a_dir_returns_empty(self, tmp_path):
        """Year is a file, not a directory → second arm of `not d.is_dir()` → []."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "2026").write_bytes(b"x")
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_months("2026") == []


class TestLocalListYearFirstDaysNoneBranches:
    """Pin lines 141 and 144: year-traversal None → []; month-traversal None → []."""

    def test_year_traversal_returns_empty(self, tmp_path):
        """Line 141: year is `../../etc` → _safe_join returns None → []."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_days("../../etc", "05") == []

    def test_month_traversal_inside_year_returns_empty(self, tmp_path):
        """Line 144: year ok but month is traversal → second _safe_join None → []."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "2026").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_days("2026", "../../etc") == []

    def test_month_dir_missing_returns_empty(self, tmp_path):
        """Line 144 (`not d.is_dir()`): month name valid but dir doesn't exist."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "2026").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_days("2026", "05") == []


class TestLocalListYearFirstEventsNoneBranches:
    """Pin lines 150, 153, 156: 3 traversal-None branches for events helper."""

    def test_year_traversal_returns_empty(self, tmp_path):
        """Line 150: year is `..` → first _safe_join None → []."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("../../etc", "05", "07") == []

    def test_month_traversal_returns_empty(self, tmp_path):
        """Line 153: year ok, month traversal → second _safe_join None → []."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "2026").mkdir()
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("2026", "../../etc", "07") == []

    def test_day_traversal_returns_empty(self, tmp_path):
        """Line 156: year+month ok, day traversal → third _safe_join None → []."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "2026" / "05").mkdir(parents=True)
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("2026", "05", "../../etc") == []

    def test_day_dir_missing_returns_empty(self, tmp_path):
        """Line 156 (`not d.is_dir()`): day name valid but day dir absent."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        (tmp_path / "2026" / "05").mkdir(parents=True)
        b = _LocalBackend(str(tmp_path))
        assert b.list_year_first_events("2026", "05", "07") == []


# ── SMB year-first browse branches (lines 924-928, 937-942, 972-985) ────────


def _make_media_source(tmp_path):
    """Build a BoschCameraMediaSource bound to a single SMB source whose
    year-first methods are backed by tmp_path scaffolding.

    Mocks `_enabled_sources` so the dispatcher reaches `_browse_smb` with a
    `_SmbBackend` instance.
    """
    from custom_components.bosch_shc_camera.media_source import (
        BoschCameraMediaSource,
        _SmbBackend,
        _Source,
    )

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_get_entry=MagicMock(
                return_value=SimpleNamespace(entry_id="01ENT", title="Bosch")
            )
        ),
    )

    backend = _SmbBackend(
        hass,
        {
            "smb_server": "nas.local",
            "smb_share": "Events",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "",
            "upload_protocol": "smb",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
        },
    )
    src = _Source(entry_id="01ENT", kind="S", label="NAS")
    media = BoschCameraMediaSource(hass)
    return media, src, backend


class TestBrowseSmbYearFirstMonths:
    """Pin lines 922-928: `_YEAR_RE.match(camera)` → list_year_first_months path.

    Reached by `_browse_smb` with rest=['2026'] and a year-named root folder.
    """

    def test_year_at_camera_level_lists_months(self, tmp_path):
        media, src, backend = _make_media_source(tmp_path)
        with patch.object(backend, "list_year_first_months", return_value=["04", "03"]):
            node = media._browse_smb(src, backend, ["2026"], single_source=True)
        titles = [c.title for c in node.children]
        assert titles == ["04", "03"], (
            f"year-first months must be rendered as direct children, got {titles}"
        )
        # identifier of children must include the year segment
        for child in node.children:
            assert "2026/" in child.identifier, (
                f"child identifier must embed the year, got {child.identifier}"
            )


class TestBrowseSmbYearFirstDays:
    """Pin lines 935-942: `_YEAR_RE.match(camera) and _DATE_DIR_RE.match(year)`
    → list_year_first_days path.

    Reached with rest=['2026','05'] where the first segment is the year.
    """

    def test_year_month_lists_days(self, tmp_path):
        media, src, backend = _make_media_source(tmp_path)
        with patch.object(backend, "list_year_first_days", return_value=["08", "07"]):
            node = media._browse_smb(src, backend, ["2026", "05"], single_source=True)
        titles = [c.title for c in node.children]
        assert titles == ["08", "07"], (
            f"year-first days must be rendered, got {titles}"
        )
        assert node.title == "2026-05", (
            f"month node title must combine year+month, got {node.title}"
        )


class TestBrowseSmbYearFirstEvents:
    """Pin lines 970-989: 3-segment year-first events branch.

    rest=['2026','05','08'] with all three matching their respective regexes →
    dispatch to `list_year_first_events`, build VIDEO/IMAGE children, embed
    `year/month/day/file` in identifiers + thumbnails.
    """

    def test_year_month_day_lists_events_with_thumbnail(self, tmp_path):
        from homeassistant.components.media_player import MediaClass
        media, src, backend = _make_media_source(tmp_path)
        events = [
            (
                "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.mp4",
                "Garten_2026-03-25_10-33-11_MOVEMENT_ABC123.jpg",
                {
                    "camera": "Garten",
                    "date": "2026-03-25",
                    "time": "10-33-11",
                    "etype": "MOVEMENT",
                    "ext": "mp4",
                },
            ),
        ]
        with patch.object(backend, "list_year_first_events", return_value=events):
            node = media._browse_smb(
                src, backend, ["2026", "03", "25"], single_source=True
            )
        assert node.title == "2026-03-25", (
            f"event-list node title must be YYYY-MM-DD, got {node.title}"
        )
        assert len(node.children) == 1
        child = node.children[0]
        # Video event → VIDEO media class + can_play
        assert child.media_class == MediaClass.VIDEO
        assert child.can_play is True
        assert child.can_expand is False
        assert child.media_content_type == "video/mp4"
        # Identifier embeds year/month/day/file (no camera prefix in year-first)
        assert "2026/03/25/" in child.identifier
        assert child.identifier.endswith(".mp4")
        # Thumbnail URL uses the jpg sibling
        assert child.thumbnail is not None
        assert child.thumbnail.endswith(".jpg"), (
            f"thumbnail must point at the jpg sibling, got {child.thumbnail}"
        )

    def test_year_month_day_image_only_event(self, tmp_path):
        """Image-only event (no mp4 sibling) → IMAGE class + content-type image/jpeg."""
        from homeassistant.components.media_player import MediaClass
        media, src, backend = _make_media_source(tmp_path)
        events = [
            (
                "Garten_2026-03-25_10-33-11_AUDIO_AAAA.jpg",
                None,
                {
                    "camera": "Garten",
                    "date": "2026-03-25",
                    "time": "10-33-11",
                    "etype": "AUDIO",
                    "ext": "jpg",
                },
            ),
        ]
        with patch.object(backend, "list_year_first_events", return_value=events):
            node = media._browse_smb(
                src, backend, ["2026", "03", "25"], single_source=True
            )
        assert len(node.children) == 1
        child = node.children[0]
        assert child.media_class == MediaClass.IMAGE
        assert child.media_content_type == "image/jpeg"
        # No jpg sibling → thumbnail must be None (no preview)
        assert child.thumbnail is None
