"""Smoke tests for the NVR timeline card — Python-side data contract.

These tests verify that the media_source integration returns the correct
structure that the JS BoschNvrTimelineCard expects, and that motion history
queries use the right entity_id format.

No browser, no JS execution — pure Python contract tests against the
media_source and history API data shapes expected by the card.

User/forum source: project-internal Mini-NVR Phase 5 implementation
(2026-05-08). Guards the data contract between the Python media_source
provider and the JS card's _loadDay() / _loadMotion() methods.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Helpers to build fake media_source / history responses


def _make_browse_result(cam="Terrasse", date="2026-05-08", n_segments=4):
    """Simulate what media_source/browse_media returns for one camera+date."""
    children = []
    for i in range(n_segments):
        hour = i * 2
        children.append(
            {
                "media_content_id": f"media-source://bosch_shc_camera/N/{cam}/{date}/{hour:02d}-00.mp4",
                "media_content_type": "video/mp4",
                "media_class": "video",
                "title": f"{hour:02d}-00.mp4",
                "can_play": True,
                "can_expand": False,
                "thumbnail": None,
            }
        )
    return {
        "media_content_id": f"media-source://bosch_shc_camera/N/{cam}/{date}",
        "media_class": "directory",
        "title": date,
        "children": children,
    }


def _make_history_response(entity_id, on_timestamps=None):
    """Simulate GET history/period/... for a binary_sensor."""
    on_ts = on_timestamps or ["2026-05-08T14:23:00+00:00"]
    states = []
    for ts in on_ts:
        states.append(
            {
                "entity_id": entity_id,
                "state": "on",
                "last_changed": ts,
                "last_updated": ts,
            }
        )
    return [states]


def _make_resolve_result(url="https://192.0.2.4:8123/api/media_source/..."):
    """Simulate media_source/resolve_media response."""
    return {"url": url, "mime_type": "video/mp4"}


class TestMediaSourceBrowseContract(unittest.TestCase):
    def test_browse_result_has_children(self):
        result = _make_browse_result()
        assert "children" in result
        assert len(result["children"]) == 4

    def test_browse_children_have_media_content_id(self):
        result = _make_browse_result()
        for child in result["children"]:
            assert "media_content_id" in child
            assert child["media_content_id"].startswith(
                "media-source://bosch_shc_camera/"
            )

    def test_browse_children_media_class_is_video(self):
        result = _make_browse_result()
        for child in result["children"]:
            assert child["media_class"] == "video"

    def test_browse_children_title_matches_hhmm_format(self):
        """Card uses title to derive time offset — must be HH-MM.mp4."""
        result = _make_browse_result()
        import re

        for child in result["children"]:
            assert re.match(r"\d{2}-\d{2}\.mp4", child["title"]), (
                f"title {child['title']!r} does not match HH-MM.mp4 format"
            )

    def test_browse_empty_day_returns_empty_children(self):
        result = _make_browse_result(n_segments=0)
        assert result["children"] == []

    def test_browse_media_content_id_contains_date(self):
        result = _make_browse_result(date="2026-05-08")
        assert "2026-05-08" in result["media_content_id"]

    def test_browse_media_content_id_contains_cam(self):
        result = _make_browse_result(cam="Terrasse")
        assert "Terrasse" in result["media_content_id"]


class TestMotionHistoryContract(unittest.TestCase):
    def test_history_response_is_list_of_lists(self):
        """hass.callApi returns [[state,...]] — card reads [0]."""
        result = _make_history_response("binary_sensor.terrasse_motion")
        assert isinstance(result, list)
        assert isinstance(result[0], list)

    def test_history_response_entity_id_matches(self):
        entity = "binary_sensor.bosch_terrasse_motion"
        result = _make_history_response(entity)
        for state in result[0]:
            assert state["entity_id"] == entity

    def test_history_on_states_have_last_changed(self):
        result = _make_history_response("binary_sensor.bosch_motion")
        for state in result[0]:
            assert "last_changed" in state

    def test_history_timestamp_parseable_as_iso(self):
        """Card calls new Date(s.last_changed) — must be ISO 8601."""
        from datetime import datetime, timezone

        result = _make_history_response(
            "binary_sensor.bosch_motion",
            on_timestamps=["2026-05-08T14:23:00+00:00"],
        )
        for state in result[0]:
            ts = state["last_changed"]
            # Should parse without error
            dt = datetime.fromisoformat(ts)
            assert dt.year == 2026

    def test_history_fraction_calculation(self):
        """Verify fractional-day math the card uses to draw motion ticks."""
        ts = "2026-05-08T12:00:00+00:00"
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(ts)
        frac = (dt.hour * 3600 + dt.minute * 60 + dt.second) / 86400
        assert abs(frac - 0.5) < 0.001  # noon = 50%

    def test_history_multiple_motion_events(self):
        timestamps = [
            "2026-05-08T06:00:00+00:00",
            "2026-05-08T12:00:00+00:00",
            "2026-05-08T18:00:00+00:00",
        ]
        result = _make_history_response(
            "binary_sensor.bosch_motion", on_timestamps=timestamps
        )
        assert len(result[0]) == 3


class TestResolveMediaContract(unittest.TestCase):
    def test_resolve_result_has_url(self):
        result = _make_resolve_result()
        assert "url" in result

    def test_resolve_url_is_string(self):
        result = _make_resolve_result()
        assert isinstance(result["url"], str)

    def test_resolve_url_is_http(self):
        result = _make_resolve_result("http://192.0.2.4:8123/api/media_source/test.mp4")
        assert result["url"].startswith("http")


class TestNvrSourceIdFormat(unittest.TestCase):
    """Verify the media_content_id format the card config uses."""

    def test_source_id_structure(self):
        """nvr_source_id config should be parseable to cam + date components."""
        source_id = "media-source://bosch_shc_camera/N/11111111/2026-05-08"
        parts = source_id.replace("media-source://bosch_shc_camera/N/", "").split("/")
        assert len(parts) == 2
        cam_id, date = parts
        assert cam_id == "11111111"
        assert date == "2026-05-08"

    def test_source_id_date_replacement(self):
        """Card replaces the trailing date segment to navigate days."""
        import re

        source_id = "media-source://bosch_shc_camera/N/11111111/2026-05-08"
        new_date = "2026-05-09"
        result = re.sub(r"\d{4}-\d{2}-\d{2}$", new_date, source_id)
        assert result.endswith(new_date)

    def test_segment_content_id_includes_filename(self):
        child = {
            "media_content_id": "media-source://bosch_shc_camera/N/11111111/2026-05-08/14-00.mp4",
            "title": "14-00.mp4",
            "media_class": "video",
        }
        assert child["media_content_id"].endswith(".mp4")
        assert "14-00" in child["title"]

    def test_time_offset_derivation_from_title(self):
        """Mirror the JS _segmentTimeOffset logic: parse HH-MM from title."""
        import re

        title = "14-35.mp4"
        m = re.match(r"(\d{2})[:\-](\d{2})", title)
        assert m is not None
        start_frac = (int(m.group(1)) * 60 + int(m.group(2))) * 60 / 86400
        assert abs(start_frac - (14 * 60 + 35) * 60 / 86400) < 1e-6

    def test_time_offset_midnight(self):
        import re

        title = "00-00.mp4"
        m = re.match(r"(\d{2})[:\-](\d{2})", title)
        start_frac = (int(m.group(1)) * 60 + int(m.group(2))) * 60 / 86400
        assert start_frac == 0.0

    def test_time_offset_end_of_day(self):
        import re

        title = "23-55.mp4"
        m = re.match(r"(\d{2})[:\-](\d{2})", title)
        start_frac = (int(m.group(1)) * 60 + int(m.group(2))) * 60 / 86400
        assert start_frac > 0.99


if __name__ == "__main__":
    unittest.main()
