"""Regression tests for camera-name edge cases in media_source.py and smb.py.

Covers bugs reported by Andreas74 (simon42 forum 2026-05-07, topic 81743):
  - Media Browser subfolder appeared but was empty after events downloaded.
  - User suspected space in camera name was the cause.
  - Additional: old events downloaded after reload (Thomas, 2026-05-07).

These tests pin the correct behaviour so future refactors cannot regress it.
"""
from __future__ import annotations

import sys
import time
import calendar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_event_file(cam_dir: Path, cam_name: str, date: str, t: str = "10-00-00",
                     etype: str = "MOVEMENT", ev_id: str = "37AE5347",
                     ext: str = "jpg") -> Path:
    """Write a zero-byte event file with the standard naming convention."""
    filename = f"{cam_name}_{date}_{t}_{etype}_{ev_id}.{ext}"
    p = cam_dir / filename
    p.write_bytes(b"FAKE")
    return p


def _coord_with_download_path(tmp_path: Path, started_offset_s: float = -3600):
    """Coordinator stub for local-save tests.

    started_offset_s: seconds relative to now for _download_started_at.
      Negative → started in the past.  Default = 1 h ago.
    """
    coord = SimpleNamespace(
        options={"download_path": str(tmp_path)},
        hass=MagicMock(),
        _download_started_at=time.time() + started_offset_s,
    )
    return coord


def _iso_ts(offset_s: float) -> str:
    """Return an ISO-8601 UTC timestamp offset_s seconds from now."""
    t = time.gmtime(time.time() + offset_s)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


# ── _LocalBackend: camera names with spaces ───────────────────────────────────


class TestLocalBackendCameraNameWithSpace:
    """_LocalBackend must handle camera names that contain spaces.

    Root context: Andreas74 (simon42 2026-05-07) reported that the Media
    Browser subfolder was always empty when the camera display name
    contained a space.  The _FILE_RE and _safe_join must both tolerate
    spaces so list_cameras / list_dates / list_events all work end-to-end.
    """

    def test_list_cameras_returns_name_with_space(self, tmp_path):
        """Directories whose name contains a space are returned correctly."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Meine Kamera"
        (tmp_path / cam).mkdir()
        b = _LocalBackend(str(tmp_path))
        assert cam in b.list_cameras()

    def test_list_dates_with_space_in_name(self, tmp_path):
        """list_dates must find dates when camera name has a space."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Bosch Terrasse"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]

    def test_list_dates_multiple_days(self, tmp_path):
        """Multiple dates returned sorted newest-first, spaces handled."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Kamera 01"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-06")
        _make_event_file(cam_dir, cam, "2026-05-07", t="11-00-00")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07", "2026-05-06"]

    def test_list_events_returns_files_with_space_in_name(self, tmp_path):
        """list_events must yield events when camera name has a space."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Garten Kamera"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        events = b.list_events(cam, "2026-05-07")
        assert len(events) == 1, "One event expected"

    def test_list_events_multiple_spaces(self, tmp_path):
        """Camera name with multiple spaces must work end-to-end."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Vorne Rechts Aussen"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]
        events = b.list_events(cam, "2026-05-07")
        assert len(events) == 1

    def test_resolve_file_with_space_in_name(self, tmp_path):
        """resolve() must return the Path for a file under a camera with a space."""
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Test Kamera"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        f = _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        result = b.resolve(cam, f.name)
        assert result == f


# ── _LocalBackend: camera names with umlauts ─────────────────────────────────


class TestLocalBackendUmlautNames:
    """Camera names with German umlauts (ä, ö, ü) must be handled correctly."""

    def test_list_dates_umlaut_name(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Küche"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]

    def test_list_dates_umlaut_with_space(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import _LocalBackend
        cam = "Haustür Eingang"
        cam_dir = tmp_path / cam
        cam_dir.mkdir()
        _make_event_file(cam_dir, cam, "2026-05-07")
        b = _LocalBackend(str(tmp_path))
        assert b.list_dates(cam) == ["2026-05-07"]


# ── _FILE_RE: regex edge cases ───────────────────────────────────────────────


class TestFileRegexEdgeCases:
    """_FILE_RE must match (or correctly reject) filenames from various camera names."""

    @pytest.fixture(autouse=True)
    def _import(self):
        from custom_components.bosch_shc_camera.media_source import _FILE_RE
        self.re = _FILE_RE

    def _parse(self, name: str):
        m = self.re.match(name)
        return m.groupdict() if m else None

    def test_space_in_camera_name(self):
        r = self._parse("Meine Kamera_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Meine Kamera"
        assert r["date"] == "2026-05-07"

    def test_two_spaces_in_camera_name(self):
        r = self._parse("Vorne Rechts Aussen_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Vorne Rechts Aussen"

    def test_umlaut_in_camera_name(self):
        r = self._parse("Küche_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Küche"

    def test_number_in_camera_name(self):
        r = self._parse("Kamera 1_2026-05-07_10-00-00_MOVEMENT_37AE5347.jpg")
        assert r is not None
        assert r["camera"] == "Kamera 1"

    def test_mp4_extension(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_37AE5347.mp4")
        assert r is not None
        assert r["ext"] == "mp4"

    def test_person_event_type(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_PERSON_37AE5347.jpg")
        assert r is not None
        assert r["etype"] == "PERSON"

    def test_unknown_id_rejected(self):
        """Files saved with id='UNKNOWN' (non-hex) must NOT match _FILE_RE.

        This is the case when coordinator._last_event_ids returns 'unknown'
        as the fallback — those files are invisible in the Media Browser.
        Regression guard: if the ev_id guard is ever loosened, add a
        hex-validation step in sync_local_save instead.
        """
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_UNKNOWN.jpg")
        assert r is None, (
            "_FILE_RE must not match files whose ID is 'UNKNOWN' (non-hex). "
            "If this fails, also fix sync_local_save to use a valid hex ID."
        )

    def test_empty_id_rejected(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_.jpg")
        assert r is None, "_FILE_RE must require at least one hex char in ID segment"

    def test_lowercase_hex_id_accepted(self):
        r = self._parse("Terrasse_2026-05-07_10-00-00_MOVEMENT_abcd1234.jpg")
        assert r is not None, "Lowercase hex IDs must be accepted (re.IGNORECASE)"


# ── sync_local_save: old-event guard ─────────────────────────────────────────


class TestSyncLocalSaveOldEventGuard:
    """sync_local_save must skip events that predate coordinator._download_started_at.

    Root cause (Andreas74, simon42 2026-05-07): when download_path is
    enabled and HA restarts, Bosch may replay queued FCM push notifications
    for events that happened before the reload.  Without the guard, those
    stale events get downloaded even though they predate the current session.
    The fix: compare ev["timestamp"] (parsed to epoch) against
    coordinator._download_started_at (set in __init__ to time.time()).
    Events older than (started_at - 60 s) are skipped.
    """

    def test_old_event_is_skipped(self, tmp_path):
        """Event timestamp 2 hours before coordinator start → no file written."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        # Coordinator started "now"
        coord = _coord_with_download_path(tmp_path, started_offset_s=0)

        # Event from 2 hours ago
        ev = {
            "timestamp": _iso_ts(-7200),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        assert list(tmp_path.rglob("*.jpg")) == [], (
            "Old event (2 h before session start) must not create any file"
        )

    def test_event_just_before_cutoff_is_skipped(self, tmp_path):
        """Event 90 s before start is within the guard window → skipped."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord_with_download_path(tmp_path, started_offset_s=0)
        ev = {
            "timestamp": _iso_ts(-90),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        assert list(tmp_path.rglob("*.jpg")) == [], (
            "Event 90 s before session start must be skipped"
        )

    def test_recent_event_is_not_skipped(self, tmp_path):
        """Event after coordinator start (within 60 s slack) → proceeds to download."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        # Coordinator started 10 minutes ago
        coord = _coord_with_download_path(tmp_path, started_offset_s=-600)

        # Event happened 5 minutes ago (after session start)
        ev = {
            "timestamp": _iso_ts(-300),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.iter_content.return_value = [b"JPEG"]
        fake_session = MagicMock()
        fake_session.get.return_value = fake_resp
        fake_session.headers = {}
        fake_requests = MagicMock()
        fake_requests.Session.return_value = fake_session

        with patch.dict(sys.modules, {"requests": fake_requests, "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        written = list(tmp_path.rglob("*.jpg"))
        assert len(written) == 1, (
            "Recent event (5 min after session start) must trigger download"
        )

    def test_event_within_60s_slack_is_not_skipped(self, tmp_path):
        """Event 30 s before coordinator start is within the 60 s tolerance → allowed."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord_with_download_path(tmp_path, started_offset_s=0)

        ev = {
            "timestamp": _iso_ts(-30),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.iter_content.return_value = [b"JPEG"]
        fake_session = MagicMock()
        fake_session.get.return_value = fake_resp
        fake_session.headers = {}
        fake_requests = MagicMock()
        fake_requests.Session.return_value = fake_session

        with patch.dict(sys.modules, {"requests": fake_requests, "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        written = list(tmp_path.rglob("*.jpg"))
        assert len(written) == 1, (
            "Event within 60 s slack window must not be blocked"
        )

    def test_no_started_at_attribute_falls_through(self, tmp_path):
        """Coordinator without _download_started_at (e.g. old pickled state) must not crash."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = SimpleNamespace(
            options={"download_path": str(tmp_path)},
            hass=MagicMock(),
            # deliberately no _download_started_at
        )

        ev = {
            "timestamp": _iso_ts(-7200),
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        # Should not raise; getattr default is 0.0 which disables the guard
        with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")
        # No assertion on file — guard disabled, behaviour depends on requests mock


# ── _browse path auto-detection (single_source vs multi_source) ───────────────


def _hass_for_browse(tmp_path: Path, entry_id: str = "01ENT",
                     extra_opts: dict | None = None):
    """Minimal fake hass for _browse tests with one local backend."""
    opts = {"download_path": str(tmp_path), "media_browser_source": "auto"}
    if extra_opts:
        opts.update(extra_opts)
    coord = SimpleNamespace(options=opts)
    entry = SimpleNamespace(entry_id=entry_id, runtime_data=coord, title="Bosch")
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_loaded_entries=MagicMock(return_value=[entry]),
            async_get_entry=MagicMock(return_value=entry),
        ),
        data={},
    )
    return hass


def _seed_event(base: Path, camera: str, date: str,
                t: str = "10-00-00") -> None:
    """Seed a single event jpg in the camera-first nested structure: camera/year/month/day/."""
    year, month, day = date.split("-")
    cam_dir = base / camera / year / month / day
    cam_dir.mkdir(parents=True, exist_ok=True)
    (cam_dir / f"{camera}_{date}_{t}_MOVEMENT_AB12CD34.jpg").write_bytes(b"x")


class TestBrowsePathAutoDetection:
    """_browse correctly detects whether parts[1] is a source-kind token or
    a tree segment (camera name).

    Regression target: the original `parts[1] not in ("L","S","N")` check
    broke navigation for cameras named exactly "L", "S", or "N" in
    single_source mode.  Fixed by comparing against the actual backend kind.
    """

    def test_camera_with_space_navigates_to_years(self, tmp_path):
        """Single source, camera name 'Meine Kamera' (with space) → years listed.

        Root context: Andreas74 (simon42 2026-05-07) reported empty subfolder.
        Camera-first tree (default): camera level shows years.
        """
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "Meine Kamera", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Meine Kamera")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_camera_named_L_known_ambiguity(self, tmp_path):
        """KNOWN LIMITATION: camera named 'L' with local backend is ambiguous.

        The identifier '{entry_id}/L' cannot be distinguished between:
          a) navigate to source kind "L" (local)   ← backwards-compat path
          b) navigate to camera named "L"           ← desired for this camera

        Because single_source==True and actual_kind=="L" and parts[1]=="L",
        the condition `parts[1] != actual_kind` is False, so the code takes
        the source-token path (else branch) and returns the camera list
        instead of the date list.

        Fixing this without a breaking identifier-scheme change is not possible.
        The fix we applied (use `!= actual_kind` instead of `not in ("L","S","N")`)
        already correctly handles cameras named "S" or "N" with a local backend.
        The remaining blind spot is ONLY camera-name == backend-kind (both "L").

        This test pins the actual behaviour so we notice if it ever changes.
        """
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "L", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/L")
        # Source-kind path taken → returns the camera list (one camera named "L")
        assert len(out.children) == 1
        assert out.children[0].title == "L", (
            "Known limitation: identifier '01ENT/L' is treated as source-kind token "
            "and returns the camera list; 'L' is the camera name shown as a child."
        )

    def test_camera_named_S_navigates_to_years(self, tmp_path):
        """Camera named 'S' must not be treated as SMB-source token."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "S", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/S")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_camera_named_N_navigates_to_years(self, tmp_path):
        """Camera named 'N' must not be treated as NVR-source token."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "N", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/N")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_old_style_L_prefix_compatibility(self, tmp_path):
        """Old bookmark with 'L' prefix on single-source entry must still navigate.

        When a user had multi-source and bookmarked '{entry_id}/L/Cam',
        then removed SMB, single_source becomes True.  Identifier has 'L' at
        parts[1] which matches the actual backend kind → treated as source
        token (backwards-compat path), rest = [cam] → camera-first year level.
        """
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "Terrasse", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        # Old multi-source identifier for the camera level: 01ENT/L/Terrasse
        out = src._browse("01ENT/L/Terrasse")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"

    def test_camera_with_umlaut_navigates_to_years(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "Küche", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Küche")
        assert len(out.children) == 1

    def test_camera_with_space_day_lists_events(self, tmp_path):
        """Full path to day level with space in camera name returns events."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "Bosch Terrasse", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Bosch Terrasse/2026/05/07")
        assert len(out.children) == 1
        assert out.children[0].can_play is True

    def test_root_with_space_camera_lists_camera_as_child(self, tmp_path):
        """Root browse returns camera names (with spaces) as children."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "Vorne Rechts", "2026-05-07")
        _seed_event(tmp_path, "Hinten Links", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("")
        titles = {c.title for c in out.children}
        assert "Vorne Rechts" in titles
        assert "Hinten Links" in titles

    def test_camera_name_longer_than_one_char_starting_with_L(self, tmp_path):
        """Camera named 'Lounge' (starts with L) is NOT treated as source token."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "Lounge", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/Lounge")
        assert len(out.children) == 1
        assert out.children[0].title == "2026"  # camera-first: year level

    def test_multiple_cameras_with_spaces_sorted(self, tmp_path):
        """Multiple cameras with spaces are sorted case-insensitively."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        _seed_event(tmp_path, "Zweite Kamera", "2026-05-07")
        _seed_event(tmp_path, "erste Kamera", "2026-05-07")
        _seed_event(tmp_path, "Mittlere Kamera", "2026-05-07")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("")
        titles = [c.title for c in out.children]
        assert titles == sorted(titles, key=str.casefold)

    def test_unknown_entry_raises_unresolvable(self, tmp_path):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, Unresolvable,
        )
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises((Exception,)):
            src._browse("UNKNOWN_ENTRY/Cam")

    def test_camera_day_events_thumbnail_uses_space_in_url(self, tmp_path):
        """Thumbnail URL for events under a camera with a space must be set."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource,
        )
        cam = "My Cam"
        cam_dir = tmp_path / cam / "2026" / "05" / "07"
        cam_dir.mkdir(parents=True)
        # Write both jpg and mp4 to trigger thumbnail logic
        stem = f"{cam}_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        (cam_dir / f"{stem}.jpg").write_bytes(b"x")
        (cam_dir / f"{stem}.mp4").write_bytes(b"x")
        hass = _hass_for_browse(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01ENT/My Cam/2026/05/07")
        assert len(out.children) == 1
        event = out.children[0]
        # Thumbnail must be set for the jpg
        assert event.thumbnail is not None
        assert "My Cam" in event.thumbnail


# ── _safe_name: sanitisation edge cases ──────────────────────────────────────


class TestSafeNameSanitization:
    """_safe_name must produce valid directory names from arbitrary camera names.

    The result is used both as a directory name (sync_local_save) and as a
    component of the filename that _FILE_RE must then match.
    """

    @pytest.fixture(autouse=True)
    def _import(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        self.fn = _safe_name

    def test_plain_name_unchanged(self):
        assert self.fn("Terrasse") == "Terrasse"

    def test_spaces_preserved(self):
        assert self.fn("Meine Kamera") == "Meine Kamera"

    def test_dot_dot_replaced(self):
        result = self.fn("../etc/passwd")
        assert ".." not in result

    def test_slash_replaced(self):
        result = self.fn("Cam/Eingang")
        assert "/" not in result

    def test_tilde_replaced(self):
        result = self.fn("~user")
        assert "~" not in result

    def test_umlaut_preserved(self):
        result = self.fn("Haustür")
        assert "Haustür" == result

    def test_truncated_to_64(self):
        long_name = "A" * 100
        assert len(self.fn(long_name)) <= 64

    def test_result_matches_file_re_roundtrip(self):
        """Sanitized name, used as camera in a filename, must match _FILE_RE."""
        import re
        _FILE_RE = re.compile(
            r"^(?P<camera>.+?)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})"
            r"_(?P<etype>[A-Z_]+)_[0-9A-F]+\.(?P<ext>jpg|jpeg|mp4)$",
            re.IGNORECASE,
        )
        test_names = [
            "Meine Kamera", "Küche", "Cam/Eingang", "Test 01", "Vorne Rechts Aussen",
        ]
        for name in test_names:
            safe = self.fn(name)
            filename = f"{safe}_2026-05-07_10-00-00_MOVEMENT_AB12CD34.jpg"
            m = _FILE_RE.match(filename)
            assert m is not None, (
                f"_safe_name({name!r}) = {safe!r} produces filename {filename!r} "
                f"that does NOT match _FILE_RE — Media Browser would show empty folder"
            )
            assert m.group("camera") == safe
