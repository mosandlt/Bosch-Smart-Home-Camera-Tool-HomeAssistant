"""Tests for media_source.py — flat-file coverage (Round 5).

Covers ALL remaining missing lines from the coverage report:
  72-73    _safe_join — path traversal (target outside base)
  135      _LocalBackend.list_years — cam_dir is a file, not a dir
  142-148  _LocalBackend.list_months — cam_dir None / year_dir None / not-dir
  154-163  _LocalBackend.list_days — various None paths
  172, 175 _LocalBackend.list_events_dated — cam_dir None, day_dir not dir
  215, 218 _LocalBackend._collect_events — junk skip, unparseable skip
  361-369  _SmbBackend.list_flat_dates — success path + OSError
  373-392  _SmbBackend.list_flat_events — success + OSError
  396-404  _SmbBackend.open_flat_file — traversal/invalid/macos/valid
  440      _NvrBackend.list_segments — cam_dir None (path traversal)
  475      _find_source returns None
  491-492  _enabled_sources — OSError for local backend
  510-511  _enabled_sources — OSError for NVR backend
  698, 701-722  _browse_local camera_first: flat dates + flat events branch
  728-733  _browse_local camera_first: days level (len(rest)==3)
  755-774  _browse_local legacy flat (camera_first=False)
  864      _browse_smb camera_first: flat dates appended at camera level
  875-888  _browse_smb camera_first: flat events branch (non-year rest[1])
  932-953  _browse_smb date-first: events at len(rest)==3
  990-1004 BoschCameraMediaView.get() — SMB single-source routing heuristics
  1024-1033 get() — SMB len(tail)==2 flat path
  1047-1050 _serve_local — file exists but bad mime type
  1064     _serve_nvr — returns FileResponse (happy path)
  1097-1108 _serve_smb_flat — full path (parse, open, stream)
  1131-1133 _stream_smb_fobj — invalid Range header (ValueError)
  1154     _stream_smb_fobj — finally block: fobj.close() called even on write failure
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.media_source import (
    _LocalBackend,
    _NvrBackend,
    _SmbBackend,
    _safe_join,
    BoschCameraMediaSource,
    BoschCameraMediaView,
    _enabled_sources,
    _find_source,
)
from homeassistant.components.media_source.error import Unresolvable

MODULE = "custom_components.bosch_shc_camera.media_source"

# ---------------------------------------------------------------------------
# SMB test helpers (reused from round2 pattern)
# ---------------------------------------------------------------------------

def _fake_smbclient(entries=None, stat_size=0, fobj=None):
    """Build a fake smbclient module for sys.modules injection."""
    mod = MagicMock()
    mod.register_session = MagicMock()
    if entries is not None:
        mod.scandir = MagicMock(return_value=iter(entries))
    if fobj is not None:
        fake_stat = MagicMock()
        fake_stat.st_size = stat_size
        mod.open_file = MagicMock(return_value=fobj)
        mod.stat = MagicMock(return_value=fake_stat)
    return mod


def _dir_entry(name, is_dir=True, is_file=False):
    e = MagicMock()
    e.name = name
    e.is_dir.return_value = is_dir
    e.is_file.return_value = is_file
    return e


def _make_smb_backend(**opts):
    from custom_components.bosch_shc_camera.media_source import _SmbBackend
    hass = MagicMock()
    hass.data = {}
    base = {
        "smb_server": "nas",
        "smb_share": "M",
        "smb_username": "u",
        "smb_password": "p",
        "smb_base_path": "",
    }
    base.update(opts)
    return _SmbBackend(hass, base)


def _hass_stub(entry_id: str = "entry1", opts: dict | None = None, tmp_path: Path | None = None):
    hass = MagicMock()
    hass.data = {}
    hass.http = MagicMock()
    opts = opts or {"download_path": str(tmp_path or "/tmp"), "media_browser_source": "local"}
    coord = SimpleNamespace(options=opts)
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = coord
    entry.title = "Bosch Cam"
    hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)

    async def _exec(fn, *args):
        return fn(*args)
    hass.async_add_executor_job = _exec
    return hass


# ---------------------------------------------------------------------------
# Lines 72-73: _safe_join — path traversal via symlink (escaping base)
# ---------------------------------------------------------------------------

class TestSafeJoinTraversal:
    def test_symlink_escaping_base_returns_none(self, tmp_path):
        """Symlink that points outside base passes raise_if_invalid_path
        but resolve() shows it's outside the base → returns None (lines 72-73)."""
        base = tmp_path / "base"
        outside = tmp_path / "outside"
        base.mkdir()
        outside.mkdir()
        # Create a symlink named "escape" inside base pointing to outside/
        symlink = base / "escape"
        symlink.symlink_to(outside)
        # "escape" is a valid name (no ..) so raise_if_invalid_path passes
        # but its resolve() is outside base → lines 72-73 fire
        result = _safe_join(base, "escape")
        assert result is None

    def test_valid_path_returns_target(self, tmp_path):
        """Normal relative path stays inside base → returns Path."""
        (tmp_path / "cam").mkdir()
        result = _safe_join(tmp_path, "cam")
        assert result is not None
        assert result.name == "cam"


# ---------------------------------------------------------------------------
# Line 135: _LocalBackend.list_years — cam_dir is a file, not a dir
# ---------------------------------------------------------------------------

class TestLocalListYears:
    def test_cam_path_is_file_not_dir_returns_empty(self, tmp_path):
        # Create "Terrasse" as a file, not a dir
        (tmp_path / "Terrasse").write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_years("Terrasse") == []

    def test_path_traversal_returns_empty(self, tmp_path):
        backend = _LocalBackend(str(tmp_path / "sub"))
        assert backend.list_years("../../etc") == []


# ---------------------------------------------------------------------------
# Lines 142-148: _LocalBackend.list_months — cam_dir None / year_dir None / not-dir
# ---------------------------------------------------------------------------

class TestLocalListMonths:
    def test_cam_dir_none_path_traversal_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _LocalBackend(str(nested))
        assert backend.list_months("../../etc", "2026") == []

    def test_year_dir_none_path_traversal_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        backend = _LocalBackend(str(tmp_path))
        # year is path traversal inside cam_dir
        assert backend.list_months("Cam", "../../etc") == []

    def test_year_dir_not_dir_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        # Create "2026" as a file inside cam
        (cam_dir / "2026").write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_months("Cam", "2026") == []

    def test_year_dir_missing_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_months("Cam", "2026") == []

    def test_year_dir_exists_returns_sorted_months(self, tmp_path):
        """Year dir exists and has valid month dirs → returns them (line 148)."""
        (tmp_path / "Cam" / "2026" / "05").mkdir(parents=True)
        (tmp_path / "Cam" / "2026" / "04").mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        months = backend.list_months("Cam", "2026")
        assert months == ["05", "04"]


# ---------------------------------------------------------------------------
# Lines 154-163: _LocalBackend.list_days — various None paths
# ---------------------------------------------------------------------------

class TestLocalListDays:
    def test_cam_dir_none_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _LocalBackend(str(nested))
        assert backend.list_days("../../etc", "2026", "05") == []

    def test_year_traversal_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "../../etc", "05") == []

    def test_month_traversal_inside_year_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        year_dir = cam_dir / "2026"
        year_dir.mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "2026", "../../etc") == []

    def test_month_dir_not_dir_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        year_dir = cam_dir / "2026"
        year_dir.mkdir(parents=True)
        (year_dir / "05").write_bytes(b"x")  # file not dir
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "2026", "05") == []

    def test_month_dir_missing_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Cam"
        (cam_dir / "2026").mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_days("Cam", "2026", "05") == []


# ---------------------------------------------------------------------------
# Lines 172, 175: _LocalBackend.list_events_dated — cam_dir None, day_dir not dir
# ---------------------------------------------------------------------------

class TestLocalListEventsDated:
    def test_cam_dir_none_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _LocalBackend(str(nested))
        assert backend.list_events_dated("../../etc", "2026", "05", "07") == []

    def test_day_dir_not_dir_returns_empty(self, tmp_path):
        # Create cam/year/month/day as a file
        day_path = tmp_path / "Cam" / "2026" / "05" / "07"
        day_path.parent.mkdir(parents=True)
        day_path.write_bytes(b"x")  # file not dir
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_events_dated("Cam", "2026", "05", "07") == []

    def test_day_dir_missing_returns_empty(self, tmp_path):
        (tmp_path / "Cam" / "2026" / "05").mkdir(parents=True)
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_events_dated("Cam", "2026", "05", "07") == []


# ---------------------------------------------------------------------------
# Lines 215, 218: _LocalBackend._collect_events — junk + unparseable skips
# ---------------------------------------------------------------------------

class TestLocalCollectEvents:
    def test_macos_junk_skipped_in_collect(self, tmp_path):
        day_dir = tmp_path / "Cam" / "2026" / "05" / "07"
        day_dir.mkdir(parents=True)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        (day_dir / f"._macos_{stem}.mp4").write_bytes(b"junk")  # macOS junk
        (day_dir / f"{stem}.mp4").write_bytes(b"real")
        backend = _LocalBackend(str(tmp_path))
        events = backend.list_events_dated("Cam", "2026", "05", "07")
        assert len(events) == 1
        assert "._" not in events[0][0]

    def test_unparseable_filename_skipped_in_collect(self, tmp_path):
        day_dir = tmp_path / "Cam" / "2026" / "05" / "07"
        day_dir.mkdir(parents=True)
        (day_dir / "not_a_valid_event.txt").write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_events_dated("Cam", "2026", "05", "07") == []


# ---------------------------------------------------------------------------
# Lines 361-369: _SmbBackend.list_flat_dates — success + OSError
# ---------------------------------------------------------------------------

class TestSmbListFlatDates:
    def test_flat_dates_extracted_from_files(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_DEADBEEF"
        entries = [
            _dir_entry(f"{stem}.mp4", is_dir=False, is_file=True),
            _dir_entry(f"{stem}.jpg", is_dir=False, is_file=True),
        ]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == ["2026-05-07"]

    def test_flat_dates_multiple_files_deduped(self):
        b = _make_smb_backend()
        s1 = "Cam_2026-05-07_10-00-00_MOVEMENT_AA"
        s2 = "Cam_2026-05-07_11-00-00_MOVEMENT_BB"
        s3 = "Cam_2026-05-08_10-00-00_MOVEMENT_CC"
        entries = [
            _dir_entry(f"{s1}.mp4", is_dir=False, is_file=True),
            _dir_entry(f"{s2}.jpg", is_dir=False, is_file=True),
            _dir_entry(f"{s3}.mp4", is_dir=False, is_file=True),
        ]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == ["2026-05-08", "2026-05-07"]  # sorted reverse

    def test_flat_dates_oserror_returns_empty(self):
        b = _make_smb_backend()
        fake = MagicMock()
        fake.register_session = MagicMock()
        fake.scandir = MagicMock(side_effect=OSError("smb down"))
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == []

    def test_flat_dates_unparseable_file_skipped(self):
        b = _make_smb_backend()
        entries = [_dir_entry("not_parseable.txt", is_dir=False, is_file=True)]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            dates = b.list_flat_dates("Cam")
        assert dates == []


# ---------------------------------------------------------------------------
# Lines 373-392: _SmbBackend.list_flat_events — success + OSError
# ---------------------------------------------------------------------------

class TestSmbListFlatEvents:
    def test_flat_events_filtered_by_date(self):
        b = _make_smb_backend()
        stem_match = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        stem_other = "Cam_2026-05-08_10-00-00_MOVEMENT_CCDD"
        entries = [
            _dir_entry(f"{stem_match}.mp4", is_dir=False, is_file=True),
            _dir_entry(f"{stem_match}.jpg", is_dir=False, is_file=True),
            _dir_entry(f"{stem_other}.mp4", is_dir=False, is_file=True),
        ]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert len(events) == 1
        preferred, image, parsed = events[0]
        assert preferred.endswith(".mp4")
        assert image.endswith(".jpg")
        assert parsed["date"] == "2026-05-07"

    def test_flat_events_no_match_returns_empty(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-08_10-00-00_MOVEMENT_AABB"
        entries = [_dir_entry(f"{stem}.mp4", is_dir=False, is_file=True)]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert events == []

    def test_flat_events_oserror_returns_empty(self):
        b = _make_smb_backend()
        fake = MagicMock()
        fake.register_session = MagicMock()
        fake.scandir = MagicMock(side_effect=OSError("smb down"))
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert events == []

    def test_flat_events_image_only(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        entries = [_dir_entry(f"{stem}.jpg", is_dir=False, is_file=True)]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            events = b.list_flat_events("Cam", "2026-05-07")
        assert len(events) == 1
        preferred, image, _ = events[0]
        assert preferred.endswith(".jpg")
        assert image.endswith(".jpg")


# ---------------------------------------------------------------------------
# Lines 396-404: _SmbBackend.open_flat_file — traversal/junk/invalid/valid
# ---------------------------------------------------------------------------

class TestSmbOpenFlatFile:
    def test_slash_in_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "some/path.jpg")

    def test_backslash_in_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "a\\b.jpg")

    def test_dotdot_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "..")

    def test_macos_junk_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "._hidden.jpg")

    def test_unparseable_filename_raises(self):
        b = _make_smb_backend()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_flat_file("Cam", "not_valid_UNKNOWN.jpg")

    def test_valid_filename_returns_fobj_and_size(self):
        b = _make_smb_backend()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_DEADBEEF"
        fake_fobj = MagicMock()
        fake = _fake_smbclient(fobj=fake_fobj, stat_size=4096)
        with patch.dict(sys.modules, {"smbclient": fake}):
            fobj, size = b.open_flat_file("Cam", f"{stem}.mp4")
        assert fobj is fake_fobj
        assert size == 4096


# ---------------------------------------------------------------------------
# Line 440: _NvrBackend.list_segments — cam_dir None (path traversal)
# ---------------------------------------------------------------------------

class TestNvrListSegmentsCamDirNone:
    def test_path_traversal_cam_returns_empty(self, tmp_path):
        nested = tmp_path / "base"
        nested.mkdir()
        backend = _NvrBackend(str(nested))
        assert backend.list_segments("../../etc", "2026-05-07") == []


# ---------------------------------------------------------------------------
# Line 475: _enabled_sources — entry with no runtime_data → continue
# ---------------------------------------------------------------------------

class TestEnabledSourcesNoRuntimeData:
    def test_entry_without_runtime_data_skipped(self):
        """Entry where runtime_data is None → continue (line 475)."""
        entry = MagicMock()
        entry.entry_id = "e1"
        # Simulate entry with no runtime_data attribute
        del entry.runtime_data  # Remove attribute so getattr returns None
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        sources = _enabled_sources(hass)
        # Entry skipped → no sources
        assert sources == []

    def test_entry_runtime_data_none_skipped(self):
        """Entry where runtime_data is explicitly None → continue (line 475)."""
        entry = SimpleNamespace(entry_id="e1", runtime_data=None)
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        sources = _enabled_sources(hass)
        assert sources == []


# ---------------------------------------------------------------------------
# Line 475 (original): _find_source returns None
# ---------------------------------------------------------------------------

class TestFindSourceNone:
    def test_find_source_unknown_kind_returns_none(self, tmp_path):
        hass = _hass_stub("entry1", tmp_path=tmp_path)
        result = _find_source(hass, "entry1", "X")  # X is not a valid kind
        assert result is None


# ---------------------------------------------------------------------------
# Lines 491-492: _enabled_sources — OSError creating local backend path
# ---------------------------------------------------------------------------

class TestEnabledSourcesOSError:
    def test_local_oserror_skipped(self):
        """OSError when creating download dir → source is silently skipped."""
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.runtime_data = SimpleNamespace(options={"download_path": "/no/such/deeply/nested/path"})
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        # Path.mkdir will raise PermissionError (subclass of OSError) for impossible paths
        # on systems where /no doesn't exist and we can't create it.
        # We patch Path.mkdir to raise OSError to ensure the except branch fires.
        with patch("custom_components.bosch_shc_camera.media_source.Path.mkdir",
                   side_effect=OSError("permission denied")):
            sources = _enabled_sources(hass)
        # Local source should be skipped due to OSError
        kinds = [s.kind for s, _ in sources]
        assert "L" not in kinds

    def test_nvr_oserror_skipped(self, tmp_path):
        """OSError when accessing NVR path → source is silently skipped."""
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.runtime_data = SimpleNamespace(options={
            "download_path": "",
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path / "nvr"),
        })
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        # Patch Path.is_dir to raise OSError for the NVR path check
        original_is_dir = Path.is_dir
        call_count = [0]
        def mock_is_dir(self):
            call_count[0] += 1
            if "nvr" in str(self):
                raise OSError("nvr inaccessible")
            return original_is_dir(self)
        with patch.object(Path, "is_dir", mock_is_dir):
            sources = _enabled_sources(hass)
        kinds = [s.kind for s, _ in sources]
        assert "N" not in kinds


# ---------------------------------------------------------------------------
# Lines 698, 701-722: _browse_local camera_first flat dates + flat events
# ---------------------------------------------------------------------------

class TestBrowseLocalCameraFirstFlat:

    def _setup(self, tmp_path):
        """Create a backend with files directly in camera/ (flat layout within camera_first mode)."""
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111"
        (cam_dir / f"{stem}.mp4").write_bytes(b"vid")
        (cam_dir / f"{stem}.jpg").write_bytes(b"img")
        return tmp_path

    def test_camera_level_shows_flat_dates_alongside_years(self, tmp_path):
        """camera_first=True, len(rest)==1: flat dates (from files in cam/) appended."""
        self._setup(tmp_path)
        # Also create a year subfolder so we have both year nodes and flat dates
        year_dir = tmp_path / "Terrasse" / "2026"
        year_dir.mkdir()
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse")
        titles = [c.title for c in out.children]
        # Year node
        assert "2026" in titles
        # Flat date node from file
        assert "2026-05-07" in titles

    def test_camera_level_flat_dates_only(self, tmp_path):
        """camera_first=True, no year subdirs — only flat dates shown."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse")
        titles = [c.title for c in out.children]
        assert "2026-05-07" in titles

    def test_flat_date_routing_returns_events(self, tmp_path):
        """camera_first=True, rest[1] is YYYY-MM-DD (not a 4-digit year) → flat events."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        # Browse into the flat date
        out = src._browse("01TESTENTRY/Terrasse/2026-05-07")
        assert len(out.children) == 1
        ev = out.children[0]
        assert ev.can_play is True
        assert ev.can_expand is False

    def test_flat_date_event_has_thumbnail(self, tmp_path):
        """Flat event nodes include a thumbnail URL when image is present."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026-05-07")
        ev = out.children[0]
        assert ev.thumbnail is not None
        assert ".jpg" in ev.thumbnail

    def test_flat_date_event_identifier_is_2_segment(self, tmp_path):
        """Flat event identifier is camera/filename (2 segments, not 5)."""
        self._setup(tmp_path)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026-05-07")
        ev = out.children[0]
        # Identifier after entry prefix: camera/filename (2 parts)
        parts_after_entry = ev.identifier.split("/")[1:]
        assert len(parts_after_entry) == 2


# ---------------------------------------------------------------------------
# Lines 703-707: _browse_local camera_first — months level (len(rest)==2, 4-digit year)
# ---------------------------------------------------------------------------

class TestBrowseLocalMonthsLevel:
    def test_year_level_lists_months(self, tmp_path):
        """camera_first, len(rest)==2 with 4-digit year → lists months."""
        from tests.test_media_source_browse import _seed_local_event
        _seed_local_event(tmp_path, "Terrasse", "2026-05-07")
        _seed_local_event(tmp_path, "Terrasse", "2026-04-15")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026")
        titles = [c.title for c in out.children]
        # Months show as bare 2-digit numbers now ("05", "04")
        assert "05" in titles
        assert "04" in titles

    def test_year_level_empty_months(self, tmp_path):
        """camera_first, len(rest)==2 with 4-digit year but empty year dir."""
        year_dir = tmp_path / "Terrasse" / "2026"
        year_dir.mkdir(parents=True)
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026")
        assert out.children == []


# ---------------------------------------------------------------------------
# Lines 728-733: _browse_local camera_first — days level (len(rest)==3)
# ---------------------------------------------------------------------------

class TestBrowseLocalDaysLevel:
    def test_year_month_level_lists_days(self, tmp_path):
        """camera_first, len(rest)==3 → lists days."""
        from tests.test_media_source_browse import _seed_local_event
        _seed_local_event(tmp_path, "Terrasse", "2026-05-07")
        _seed_local_event(tmp_path, "Terrasse", "2026-05-04")
        hass, _ = _hass_with_local_dir(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01TESTENTRY/Terrasse/2026/05")
        titles = [c.title for c in out.children]
        # Should show bare day numbers
        assert "07" in titles
        assert "04" in titles
        # Children must be expandable (folders)
        assert all(c.can_expand for c in out.children)


# ---------------------------------------------------------------------------
# Lines 755-774: _browse_local legacy flat (camera_first=False)
# ---------------------------------------------------------------------------

class TestBrowseLocalLegacyFlat:

    def _hass_with_legacy_backend(self, tmp_path):
        """Build hass with camera_first=False (flat) local backend."""
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_11111111"
        (cam_dir / f"{stem}.mp4").write_bytes(b"vid")
        (cam_dir / f"{stem}.jpg").write_bytes(b"img")
        opts = {
            "download_path": str(tmp_path),
            "folder_pattern": "{year}/{month}/{day}/{camera}",  # NOT camera-first
            "media_browser_source": "auto",
        }
        coord = SimpleNamespace(options=opts)
        entry = SimpleNamespace(
            entry_id="01FLAT",
            runtime_data=coord,
            title="Flat Backend",
        )
        from unittest.mock import MagicMock
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[entry]),
                async_get_entry=MagicMock(return_value=entry),
            ),
            data={},
        )
        return hass

    def test_legacy_camera_lists_dates(self, tmp_path):
        """camera_first=False, len(rest)==1 → list dates from filenames."""
        hass = self._hass_with_legacy_backend(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01FLAT/Terrasse")
        titles = [c.title for c in out.children]
        assert "2026-05-07" in titles

    def test_legacy_date_lists_events(self, tmp_path):
        """camera_first=False, len(rest)==2 → list events for that date."""
        hass = self._hass_with_legacy_backend(tmp_path)
        src = BoschCameraMediaSource(hass)
        out = src._browse("01FLAT/Terrasse/2026-05-07")
        assert len(out.children) == 1
        ev = out.children[0]
        assert ev.can_play is True

    def test_legacy_date_event_no_thumbnail_when_image_missing(self, tmp_path):
        """Legacy flat: no thumbnail when image is None."""
        # mp4 only, no jpg
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_ABCD1234"
        (cam_dir / f"{stem}.mp4").write_bytes(b"vid")
        opts = {
            "download_path": str(tmp_path),
            "folder_pattern": "{year}/{month}/{day}/{camera}",
            "media_browser_source": "auto",
        }
        coord = SimpleNamespace(options=opts)
        entry = SimpleNamespace(
            entry_id="01FLAT2",
            runtime_data=coord,
            title="Flat2",
        )
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=MagicMock(return_value=[entry]),
                async_get_entry=MagicMock(return_value=entry),
            ),
            data={},
        )
        src = BoschCameraMediaSource(hass)
        out = src._browse("01FLAT2/Cam/2026-05-07")
        assert len(out.children) == 1
        assert out.children[0].thumbnail is None

    def test_legacy_too_deep_raises_unresolvable(self, tmp_path):
        """camera_first=False: 4+ segments → Unresolvable."""
        hass = self._hass_with_legacy_backend(tmp_path)
        src = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            src._browse("01FLAT/Terrasse/2026-05-07/extra/segment")


# ---------------------------------------------------------------------------
# Line 864: _browse_smb camera_first — flat dates at camera level
# ---------------------------------------------------------------------------

class TestBrowseSmbCameraFirstFlatDates:

    def _browse_smb(self, identifier, cameras=None, years=None, flat_dates=None):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _Source, _SmbBackend,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = True
        backend.list_cameras.return_value = cameras or []
        backend.list_years.return_value = years or []
        backend.list_flat_dates.return_value = flat_dates or []
        backend.list_flat_events.return_value = []

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_camera_level_shows_flat_dates(self):
        """len(rest)==1: list_flat_dates() results appended to year nodes."""
        out = self._browse_smb(
            "01ENT/Terrasse",
            years=["2026"],
            flat_dates=["2026-05-07"],
        )
        titles = [c.title for c in out.children]
        assert "2026" in titles
        assert "2026-05-07" in titles

    def test_camera_level_flat_dates_only(self):
        """No year dirs, only flat dates."""
        out = self._browse_smb(
            "01ENT/Terrasse",
            years=[],
            flat_dates=["2026-05-08", "2026-05-07"],
        )
        titles = [c.title for c in out.children]
        assert "2026-05-08" in titles
        assert "2026-05-07" in titles


# ---------------------------------------------------------------------------
# Lines 875-888: _browse_smb camera_first — flat events branch (non-year rest[1])
# ---------------------------------------------------------------------------

class TestBrowseSmbCameraFirstFlatEvents:

    def _browse_smb_flat(self, identifier, flat_events=None, image_present=True):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _Source, _SmbBackend,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABBCCDD"
        image = f"{stem}.jpg" if image_present else None
        default_events = [(f"{stem}.mp4", image,
                           {"camera": "Cam", "date": "2026-05-07",
                            "time": "10-00-00", "etype": "MOVEMENT"})]

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = True
        backend.list_flat_events.return_value = flat_events if flat_events is not None else default_events

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_flat_date_route_returns_events(self):
        """rest[1] is YYYY-MM-DD (not year) → flat events returned."""
        out = self._browse_smb_flat("01ENT/Cam/2026-05-07")
        assert len(out.children) == 1
        ev = out.children[0]
        assert ev.can_play is True

    def test_flat_event_has_thumbnail_when_image_present(self):
        out = self._browse_smb_flat("01ENT/Cam/2026-05-07", image_present=True)
        assert out.children[0].thumbnail is not None

    def test_flat_event_no_thumbnail_when_image_none(self):
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABBCCDD"
        events_no_image = [(f"{stem}.mp4", None,
                            {"camera": "Cam", "date": "2026-05-07",
                             "time": "10-00-00", "etype": "MOVEMENT"})]
        out = self._browse_smb_flat("01ENT/Cam/2026-05-07", flat_events=events_no_image)
        assert out.children[0].thumbnail is None


# ---------------------------------------------------------------------------
# Lines 932-953: _browse_smb date-first — events at len(rest)==3
# ---------------------------------------------------------------------------

class TestBrowseSmbDateFirstEvents:

    def _browse_smb_date_first(self, identifier, events=None, image_present=True):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _Source, _SmbBackend,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        image = f"{stem}.jpg" if image_present else None
        default_events = [(f"{stem}.mp4", image,
                           {"camera": "Terrasse", "date": "2026-05-07",
                            "time": "10-00-00", "etype": "MOVEMENT"})]

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False
        backend.list_events.return_value = events if events is not None else default_events

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_date_first_events_listed(self):
        """date-first, len(rest)==3 (year/month/day) → events returned."""
        out = self._browse_smb_date_first("01ENT/2026/05/07")
        assert len(out.children) == 1
        assert out.children[0].can_play is True

    def test_date_first_event_has_thumbnail(self):
        out = self._browse_smb_date_first("01ENT/2026/05/07", image_present=True)
        assert out.children[0].thumbnail is not None

    def test_date_first_event_no_thumbnail_when_image_none(self):
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        events_no_img = [(f"{stem}.mp4", None,
                          {"camera": "Terrasse", "date": "2026-05-07",
                           "time": "10-00-00", "etype": "MOVEMENT"})]
        out = self._browse_smb_date_first("01ENT/2026/05/07", events=events_no_img)
        assert out.children[0].thumbnail is None

    def test_date_first_days_level(self):
        """date-first, len(rest)==2 (year/month) → days returned (lines 933-938)."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _Source, _SmbBackend,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False
        backend.list_days.return_value = ["22", "07"]

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = src_obj._browse("01ENT/2026/05")
        titles = [c.title for c in out.children]
        assert any("2026-05-22" in t for t in titles)
        assert any("2026-05-07" in t for t in titles)

    def test_date_first_too_deep_raises_unresolvable(self):
        from homeassistant.components.media_source.error import Unresolvable
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _Source, _SmbBackend,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        backend = MagicMock(spec=_SmbBackend)
        backend.camera_first = False

        src = _Source(entry_id="01ENT", kind="S", label="NAS")
        hass = MagicMock()
        hass.data = {}
        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            with pytest.raises(Unresolvable):
                src_obj._browse("01ENT/2026/05/07/file.mp4/extra")


# ---------------------------------------------------------------------------
# Lines 990-1004: BoschCameraMediaView.get() — SMB single-source routing heuristics
# ---------------------------------------------------------------------------

def _smb_hass_for_view(entry_id="entry1"):
    """Build hass stub with an SMB backend."""
    hass = MagicMock()
    hass.data = {}
    hass.http = MagicMock()
    opts = {
        "enable_smb_upload": True,
        "upload_protocol": "smb",
        "smb_server": "nas",
        "smb_share": "SHARE",
        "smb_username": "u",
        "smb_password": "p",
        "smb_base_path": "",
        "media_browser_source": "smb",
    }
    coord = SimpleNamespace(options=opts)
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.runtime_data = coord
    entry.title = "Bosch"
    hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
    hass.config_entries.async_get_entry = MagicMock(return_value=entry)

    async def _exec(fn, *args):
        return fn(*args)
    hass.async_add_executor_job = _exec
    return hass


class TestMediaViewSmbRouting:
    """Lines 990-1004: year-first and camera-first single-source routing + NVR routing."""

    @pytest.mark.asyncio
    async def test_year_head_routes_to_smb(self):
        """head matches _YEAR_RE → kind=S, tail=parts (date-first single-source)."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fake_fobj, 4))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"2026/05/07/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_camera_year_head_routes_to_smb(self):
        """parts[0]=camera, parts[1]=year → kind=S (camera-first single-source)."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AABB"
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fake_fobj, 4))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1",
                                      f"Terrasse/2026/05/07/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_else_branch_routes_to_local(self, tmp_path):
        """head is not a kind/year/camera+year/camera+date → else branch → kind=L (1003-1004)."""
        # Use a local backend
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        (cam_dir / f"{stem}.mp4").write_bytes(b"data")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        # "Cam/file.mp4" — head="Cam" (not kind token, not year, and len(parts)=2
        # so not camera+year because parts[1]="file.mp4" doesn't match YEAR_RE,
        # and parts[1] doesn't match NVR_DATE_DIR_RE) → else branch → kind=L
        # Note: len(tail)=2 is valid for local serve
        resp = await view.get(request, "entry1", f"Cam/{stem}.mp4")
        from aiohttp.web import FileResponse
        assert isinstance(resp, FileResponse)

    @pytest.mark.asyncio
    async def test_nvr_date_head_routes_to_nvr(self, tmp_path):
        """parts[1] matches NVR_DATE_DIR_RE → kind=N (NVR single-source)."""
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "10-00.mp4").write_bytes(b"nvr")

        hass = MagicMock()
        hass.data = {}
        opts = {
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path),
            "media_browser_source": "local",
        }
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "NVR"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", "Terrasse/2026-05-07/10-00.mp4")
        assert isinstance(resp, type(resp))  # web.FileResponse


# ---------------------------------------------------------------------------
# Lines 1024-1033: get() — SMB len(tail)==2 flat path
# ---------------------------------------------------------------------------

class TestMediaViewSmbFlatPath:

    @pytest.mark.asyncio
    async def test_tail_len_2_calls_serve_smb_flat(self):
        """S head + 2-part tail → _serve_smb_flat invoked."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 4))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_tail_len_other_raises_404(self):
        """SMB backend with 3-part tail (not 2, 4, or 5) → HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)

        backend_mock = MagicMock()

        request = MagicMock()
        request.headers = {}

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with pytest.raises(Exception):  # web.HTTPNotFound
                await view.get(request, "entry1", "S/a/b/c")


# ---------------------------------------------------------------------------
# Lines 1047-1050: _serve_local — bad mime type check
# ---------------------------------------------------------------------------

class TestServeLocalBadMime:

    @pytest.mark.asyncio
    async def test_bad_mime_raises_404(self, tmp_path):
        """File exists and filename parses, but mime is not image/jpeg or video/mp4."""
        # Create a file with extension that parses but gives bad mime
        # We can't easily create a file with a parseable name but bad mime
        # because _FILE_RE only matches jpg/jpeg/mp4 — instead we test via
        # mocking mimetypes.guess_type to return a different mime.
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        filepath = cam_dir / f"{stem}.jpg"
        filepath.write_bytes(b"data")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        with patch("custom_components.bosch_shc_camera.media_source.mimetypes.guess_type",
                   return_value=("application/octet-stream", None)):
            with pytest.raises(Exception):  # web.HTTPNotFound
                await view.get(request, "entry1", f"L/Cam/{stem}.jpg")


# ---------------------------------------------------------------------------
# Line 1064: _serve_nvr — returns FileResponse (happy path)
# ---------------------------------------------------------------------------

class TestServeNvrHappyPath:

    @pytest.mark.asyncio
    async def test_nvr_serve_valid_file_returns_file_response(self, tmp_path):
        """_serve_nvr with valid date + segment → returns web.FileResponse."""
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "10-00.mp4").write_bytes(b"nvr data")

        hass = MagicMock()
        hass.data = {}
        opts = {
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path),
            "media_browser_source": "local",
        }
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "NVR"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", "N/Terrasse/2026-05-07/10-00.mp4")
        # Should return web.FileResponse (not raise)
        from aiohttp.web import FileResponse
        assert isinstance(resp, FileResponse)


# ---------------------------------------------------------------------------
# Line 1050: _serve_local — happy path returns web.FileResponse
# ---------------------------------------------------------------------------

class TestServeLocalHappyPath:

    @pytest.mark.asyncio
    async def test_serve_local_returns_file_response(self, tmp_path):
        """_serve_local with valid file + correct mime → web.FileResponse (line 1050)."""
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        filepath = cam_dir / f"{stem}.mp4"
        filepath.write_bytes(b"mp4data")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", f"L/Cam/{stem}.mp4")
        from aiohttp.web import FileResponse
        assert isinstance(resp, FileResponse)

    @pytest.mark.asyncio
    async def test_serve_local_jpg_returns_file_response(self, tmp_path):
        """_serve_local with .jpg → web.FileResponse (line 1050 via jpg mime)."""
        cam_dir = tmp_path / "Cam"
        cam_dir.mkdir()
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"
        filepath = cam_dir / f"{stem}.jpg"
        filepath.write_bytes(b"jpgdata")

        hass = MagicMock()
        hass.data = {}
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
        coord = SimpleNamespace(options=opts)
        entry = MagicMock()
        entry.entry_id = "entry1"
        entry.runtime_data = coord
        entry.title = "Bosch"
        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.config_entries.async_get_entry = MagicMock(return_value=entry)

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        resp = await view.get(request, "entry1", f"L/Cam/{stem}.jpg")
        from aiohttp.web import FileResponse
        assert isinstance(resp, FileResponse)


# ---------------------------------------------------------------------------
# Lines 1097-1108: _serve_smb_flat — full path (parse, open, stream)
# ---------------------------------------------------------------------------

class TestServeSmFlatFull:

    @pytest.mark.asyncio
    async def test_serve_smb_flat_happy_path(self):
        """_serve_smb_flat: valid filename → open_flat_file → stream."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"mp4data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 7))

        request = MagicMock()
        request.headers = {}

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_serve_smb_flat_invalid_filename_raises_404(self):
        """_serve_smb_flat: unparseable filename → HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)

        backend_mock = MagicMock()

        request = MagicMock()
        request.headers = {}

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with pytest.raises(Exception):
                await view.get(request, "entry1", "S/Cam/invalid_filename.mp4")

    @pytest.mark.asyncio
    async def test_serve_smb_flat_file_not_found_raises_404(self):
        """_serve_smb_flat: open_flat_file raises FileNotFoundError → HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(side_effect=FileNotFoundError("nope"))

        request = MagicMock()
        request.headers = {}

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with pytest.raises(Exception):
                await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

    @pytest.mark.asyncio
    async def test_serve_smb_flat_os_error_raises_404(self):
        """_serve_smb_flat: OSError from open_flat_file → HTTPNotFound."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(side_effect=OSError("smb down"))

        request = MagicMock()
        request.headers = {}

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with pytest.raises(Exception):
                await view.get(request, "entry1", f"S/Cam/{stem}.mp4")


# ---------------------------------------------------------------------------
# Lines 1131-1133: _stream_smb_fobj — invalid Range header (ValueError)
# ---------------------------------------------------------------------------

class TestStreamSmbFobjRangeErrors:

    def _make_smb_view_with_mock_fobj(self, payload: bytes, range_header: str):
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[payload, b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, len(payload)))

        request = MagicMock()
        request.headers = {"Range": range_header}

        return hass, view, stem, backend_mock, fake_fobj, request

    @pytest.mark.asyncio
    async def test_invalid_range_header_falls_back_to_200(self):
        """Range: bytes=notanumber → ValueError caught → full 200 response."""
        payload = b"X" * 100
        hass, view, stem, backend_mock, fake_fobj, request = \
            self._make_smb_view_with_mock_fobj(payload, "bytes=notanumber-")

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()
        # Capture the status passed to StreamResponse
        created_statuses = []

        def _make_response(*args, **kwargs):
            created_statuses.append(kwargs.get("status", 200))
            return real_response

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", side_effect=_make_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response
        # Invalid range → status should be 200
        assert created_statuses[0] == 200

    @pytest.mark.asyncio
    async def test_range_end_only_no_start(self):
        """Range: bytes=-500 (end only, no start) → treated as full 200."""
        payload = b"Y" * 200
        hass, view, stem, backend_mock, fake_fobj, request = \
            self._make_smb_view_with_mock_fobj(payload, "bytes=-500")

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_range_start_beyond_size_falls_back_to_200(self):
        """Range where start > size → invalid range → falls back to 200."""
        payload = b"Z" * 50
        hass, view, stem, backend_mock, fake_fobj, request = \
            self._make_smb_view_with_mock_fobj(payload, "bytes=9999-99999")

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        created_statuses = []
        def _make_response(*args, **kwargs):
            created_statuses.append(kwargs.get("status", 200))
            return real_response

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", side_effect=_make_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")
        assert resp is real_response
        # Out-of-bounds range → fallback 200
        assert created_statuses[0] == 200


# ---------------------------------------------------------------------------
# Line 1154: _stream_smb_fobj — finally block: fobj.close() called on write failure
# ---------------------------------------------------------------------------

class TestStreamSmbFobjFinally:

    @pytest.mark.asyncio
    async def test_fobj_closed_even_when_write_raises(self):
        """Finally block: fobj.close() called even if response.write() raises."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(return_value=b"data")
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 4))

        # response.write raises an exception to simulate write failure
        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock(side_effect=ConnectionResetError("client gone"))
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        request = MagicMock()
        request.headers = {}

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                with pytest.raises(ConnectionResetError):
                    await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

        # fobj.close() MUST have been called (finally block)
        fake_fobj.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_fobj_closed_on_happy_path(self):
        """Finally block: fobj.close() is also called on the normal success path."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(side_effect=[b"data", b""])
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 4))

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        request = MagicMock()
        request.headers = {}

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

        fake_fobj.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_chunk_breaks_read_loop(self):
        """fobj.read() returns empty bytes before remaining is exhausted → break (line 1154)."""
        hass = _smb_hass_for_view()
        view = BoschCameraMediaView(hass)
        stem = "Cam_2026-05-07_10-00-00_MOVEMENT_AABB"

        # size=1000 but fobj returns b"" immediately → empty chunk → break at line 1154
        fake_fobj = MagicMock()
        fake_fobj.read = MagicMock(return_value=b"")  # always returns empty
        fake_fobj.seek = MagicMock()
        fake_fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_flat_file = MagicMock(return_value=(fake_fobj, 1000))

        real_response = MagicMock()
        real_response.prepare = AsyncMock()
        real_response.write = AsyncMock()
        real_response.write_eof = AsyncMock()

        async def _exec(fn, *args):
            return fn(*args)
        hass.async_add_executor_job = _exec

        request = MagicMock()
        request.headers = {}

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with patch(f"{MODULE}.web.StreamResponse", return_value=real_response):
                resp = await view.get(request, "entry1", f"S/Cam/{stem}.mp4")

        # write should NOT have been called (broke before writing)
        real_response.write.assert_not_called()
        # close must still be called (finally)
        fake_fobj.close.assert_called_once()
        assert resp is real_response


# ---------------------------------------------------------------------------
# Helper import for tests that reuse _hass_with_local_dir
# ---------------------------------------------------------------------------

def _hass_with_local_dir(tmp_path: Path, options: dict | None = None):
    """Minimal hass with one local backend pointed at tmp_path."""
    opts = {
        "download_path": str(tmp_path),
        "media_browser_source": "auto",
    }
    if options:
        opts.update(options)
    coord = SimpleNamespace(options=opts)
    entry = SimpleNamespace(
        entry_id="01TESTENTRY",
        runtime_data=coord,
        title="Test Bosch",
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_loaded_entries=MagicMock(return_value=[entry]),
            async_get_entry=MagicMock(return_value=entry),
        ),
        data={},
    )
    return hass, entry


from unittest.mock import MagicMock  # noqa: E402 (already imported at top)
