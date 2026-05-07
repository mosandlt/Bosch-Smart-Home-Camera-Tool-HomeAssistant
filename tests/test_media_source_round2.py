"""Round-2 coverage for media_source.py and smb.sync_local_save.

Targets uncovered lines from the coverage report:
  - smb.py 76-77, 93-111   → sync_local_save download guards + HTTP flow
  - media_source.py 166-261 → _SmbBackend unit API
  - media_source.py 653-706 → _browse_smb tree navigation
  - media_source.py 336-372 → _enabled_sources filter paths
  - media_source.py 499-512 → _browse_entry_root NVR/SMB single-source dispatch
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import sys

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_SAFE_IMAGE_URL = "https://media.boschsecurity.com/snapshot.jpg"
_SAFE_VIDEO_URL = "https://media.boschsecurity.com/clip.mp4"


def _iso_now(offset_s: float = 0) -> str:
    t = time.gmtime(time.time() + offset_s)
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", t)


def _coord(tmp_path: Path, *, started_offset_s: float = -3600, extra_opts: dict | None = None):
    opts = {"download_path": str(tmp_path)}
    if extra_opts:
        opts.update(extra_opts)
    return SimpleNamespace(options=opts, _download_started_at=time.time() + started_offset_s)


def _ev(**kwargs) -> dict:
    base = {
        "timestamp": _iso_now(),
        "eventType": "MOVEMENT",
        "id": "AABBCCDD",
        "imageUrl": _SAFE_IMAGE_URL,
        "videoClipUrl": _SAFE_VIDEO_URL,
        "videoClipUploadStatus": "Done",
    }
    base.update(kwargs)
    return base


def _mock_response(status=200, content=b"FAKEDATA"):
    r = MagicMock()
    r.status_code = status
    r.iter_content = lambda chunk_size: iter([content])
    return r


# ─────────────────────────────────────────────────────────────────────────────
# sync_local_save — download flow
# ─────────────────────────────────────────────────────────────────────────────

class TestSyncLocalSaveDownload:
    """Cover the actual HTTP download logic (lines 79-111)."""

    def _call(self, coord, ev, mock_session, cam_name="Terrasse"):
        from custom_components.bosch_shc_camera.smb import sync_local_save
        # requests is imported locally inside sync_local_save, so patch at the
        # global requests module level (already in sys.modules after first import).
        with patch("requests.Session", return_value=mock_session):
            sync_local_save(coord, ev, "TOKEN", cam_name)

    def test_jpg_downloaded_on_200(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, b"JPEG")
        self._call(coord, _ev(videoClipUrl=None), sess)
        files = list((tmp_path / "Terrasse").iterdir())
        assert len(files) == 1
        assert files[0].name.endswith(".jpg")
        assert files[0].read_bytes() == b"JPEG"

    def test_mp4_and_jpg_both_downloaded(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, b"DATA")
        self._call(coord, _ev(), sess)
        cam_dir = tmp_path / "Terrasse"
        exts = {f.suffix for f in cam_dir.iterdir()}
        assert ".jpg" in exts
        assert ".mp4" in exts

    def test_mp4_skipped_when_status_not_done(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, b"DATA")
        self._call(coord, _ev(videoClipUploadStatus="Pending"), sess)
        cam_dir = tmp_path / "Terrasse"
        exts = {f.suffix for f in cam_dir.iterdir()}
        assert ".jpg" in exts
        assert ".mp4" not in exts

    def test_mp4_skipped_when_status_missing(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, b"DATA")
        ev = _ev()
        del ev["videoClipUploadStatus"]
        self._call(coord, ev, sess)
        exts = {f.suffix for f in (tmp_path / "Terrasse").iterdir()}
        assert ".mp4" not in exts

    def test_unsafe_url_skipped(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        self._call(coord, _ev(imageUrl="https://evil.example.com/x.jpg", videoClipUrl=None), sess)
        assert not (tmp_path / "Terrasse").exists() or not list((tmp_path / "Terrasse").iterdir())
        sess.get.assert_not_called()

    def test_missing_image_url_no_jpg(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, b"DATA")
        self._call(coord, _ev(imageUrl=None), sess)
        exts = {f.suffix for f in (tmp_path / "Terrasse").iterdir()}
        assert ".jpg" not in exts

    def test_http_non_200_no_file_written(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(403)
        self._call(coord, _ev(videoClipUrl=None), sess)
        assert not list((tmp_path / "Terrasse").iterdir())

    def test_http_exception_does_not_crash(self, tmp_path):
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.side_effect = OSError("network gone")
        self._call(coord, _ev(videoClipUrl=None), sess)
        assert not list((tmp_path / "Terrasse").iterdir())

    def test_file_already_exists_skips_http(self, tmp_path):
        """If the file is already on disk, no HTTP request must be made."""
        coord = _coord(tmp_path)
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        ev = _ev(videoClipUrl=None)
        ts = ev["timestamp"]
        date_str = ts[:10]
        time_str = ts[11:19].replace(":", "-")
        stem = f"Terrasse_{date_str}_{time_str}_MOVEMENT_AABBCCDD"
        (cam_dir / f"{stem}.jpg").write_bytes(b"OLD")
        sess = MagicMock()
        self._call(coord, ev, sess)
        sess.get.assert_not_called()

    def test_stem_uses_empty_id_when_none(self, tmp_path):
        """id=None must not crash; stem ends with empty id suffix."""
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, b"X")
        self._call(coord, _ev(id=None, videoClipUrl=None), sess)
        files = list((tmp_path / "Terrasse").iterdir())
        assert len(files) == 1
        assert files[0].stem.endswith("_MOVEMENT_")

    def test_short_timestamp_returns_early(self, tmp_path):
        """Events with timestamp shorter than 19 chars must be ignored."""
        coord = _coord(tmp_path)
        sess = MagicMock()
        self._call(coord, _ev(timestamp="2026-05"), sess, cam_name="Cam")
        sess.get.assert_not_called()

    def test_no_download_path_returns_early(self, tmp_path):
        """Empty download_path must be a no-op."""
        coord = SimpleNamespace(options={"download_path": ""}, _download_started_at=time.time() - 3600)
        sess = MagicMock()
        self._call(coord, _ev(), sess, cam_name="Cam")
        sess.get.assert_not_called()

    def test_camera_name_with_space_creates_dir(self, tmp_path):
        """Camera name containing a space must produce the right directory."""
        coord = _coord(tmp_path)
        sess = MagicMock()
        sess.get.return_value = _mock_response(200, b"X")
        self._call(coord, _ev(videoClipUrl=None), sess, cam_name="Außen Kamera")
        assert (tmp_path / "Außen Kamera").is_dir()


# ─────────────────────────────────────────────────────────────────────────────
# _SmbBackend unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestSmbBackendProperties:

    def _make(self, **opts):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend
        hass = MagicMock()
        hass.data = {}
        base = {
            "smb_server": "nas.local",
            "smb_share": "Media",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "Bosch/Cams",
        }
        base.update(opts)
        return _SmbBackend(hass, base)

    def test_configured_true_when_server_and_share(self):
        assert self._make().configured is True

    def test_configured_false_when_no_server(self):
        assert self._make(smb_server="").configured is False

    def test_configured_false_when_no_share(self):
        assert self._make(smb_share="").configured is False

    def test_label_contains_server_and_share(self):
        b = self._make()
        assert "nas.local" in b.label
        assert "Media" in b.label

    def test_path_builds_unc(self):
        b = self._make()
        p = b._path("2026", "05", "07")
        assert p.startswith("\\\\nas.local\\Media\\")
        assert "2026" in p
        assert "07" in p

    def test_path_without_extra_segments(self):
        b = self._make(smb_base_path="")
        p = b._path()
        assert p == "\\\\nas.local\\Media"


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


class TestSmbBackendScandir:
    """Tests for list_years / list_months / list_days / list_events via mocked smbclient."""

    def _make(self):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend
        hass = MagicMock()
        hass.data = {}
        return _SmbBackend(hass, {
            "smb_server": "nas", "smb_share": "M", "smb_username": "u",
            "smb_password": "p", "smb_base_path": "",
        })

    def test_list_years_filters_non_year(self):
        b = self._make()
        entries = [_dir_entry("2025"), _dir_entry("2026"), _dir_entry("random")]
        fake = _fake_smbclient(entries)
        with patch.dict(sys.modules, {"smbclient": fake}):
            years = b.list_years()
        assert years == ["2026", "2025"]

    def test_list_years_skips_macos_junk(self):
        b = self._make()
        entries = [_dir_entry("2026"), _dir_entry(".DS_Store")]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            years = b.list_years()
        assert ".DS_Store" not in years

    def test_list_months_filters_non_numeric(self):
        b = self._make()
        entries = [_dir_entry("05"), _dir_entry("12"), _dir_entry("junk")]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            months = b.list_months("2026")
        assert "junk" not in months
        assert months == ["12", "05"]

    def test_list_days_sorted_newest_first(self):
        b = self._make()
        entries = [_dir_entry("03"), _dir_entry("22"), _dir_entry("07")]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            days = b.list_days("2026", "05")
        assert days == ["22", "07", "03"]

    def test_list_events_groups_jpg_and_mp4(self):
        b = self._make()
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        entries = [
            _dir_entry(f"{stem}.jpg", is_dir=False, is_file=True),
            _dir_entry(f"{stem}.mp4", is_dir=False, is_file=True),
        ]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            events = b.list_events("2026", "05", "07")
        assert len(events) == 1
        preferred, image, parsed = events[0]
        assert preferred.endswith(".mp4")
        assert image.endswith(".jpg")

    def test_list_events_skips_unparseable_filenames(self):
        b = self._make()
        entries = [_dir_entry("not_a_valid_event.txt", is_dir=False, is_file=True)]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            events = b.list_events("2026", "05", "07")
        assert events == []

    def test_list_events_image_only(self):
        b = self._make()
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        entries = [_dir_entry(f"{stem}.jpg", is_dir=False, is_file=True)]
        with patch.dict(sys.modules, {"smbclient": _fake_smbclient(entries)}):
            events = b.list_events("2026", "05", "07")
        assert len(events) == 1
        preferred, image, _ = events[0]
        assert preferred.endswith(".jpg")
        assert image.endswith(".jpg")


class TestSmbBackendOpenFile:

    def _make(self):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend
        hass = MagicMock()
        hass.data = {}
        return _SmbBackend(hass, {"smb_server": "nas", "smb_share": "M",
                                  "smb_username": "u", "smb_password": "p",
                                  "smb_base_path": ""})

    def test_traversal_in_filename_raises(self):
        b = self._make()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_file("2026", "05", "07", "../secret.jpg")

    def test_backslash_in_filename_raises(self):
        b = self._make()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_file("2026", "05", "07", "a\\b.jpg")

    def test_unparseable_filename_raises(self):
        b = self._make()
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            with pytest.raises(FileNotFoundError):
                b.open_file("2026", "05", "07", "not_valid_UNKNOWN.jpg")

    def test_valid_filename_delegates_to_smbclient(self):
        b = self._make()
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        fake_fobj = MagicMock()
        fake = _fake_smbclient(fobj=fake_fobj, stat_size=1234)
        with patch.dict(sys.modules, {"smbclient": fake}):
            fobj, size = b.open_file("2026", "05", "07", f"{stem}.jpg")
        assert size == 1234
        assert fobj is fake_fobj


# ─────────────────────────────────────────────────────────────────────────────
# _browse_smb — tree navigation
# ─────────────────────────────────────────────────────────────────────────────

def _hass_smb(list_years=None, list_months=None, list_days=None, list_events=None):
    """Build a mock hass that returns a single SMB-backed source."""
    from custom_components.bosch_shc_camera.media_source import _SmbBackend, _Source

    backend = MagicMock(spec=_SmbBackend)
    backend.list_years.return_value = list_years or []
    backend.list_months.return_value = list_months or []
    backend.list_days.return_value = list_days or []
    backend.list_events.return_value = list_events or []

    src = _Source(entry_id="01ENT", kind="S", label="NAS")

    entry = MagicMock()
    entry.entry_id = "01ENT"
    entry.runtime_data = SimpleNamespace(options={
        "enable_smb_upload": True,
        "upload_protocol": "smb",
        "smb_server": "nas",
        "smb_share": "M",
    })

    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_loaded_entries.return_value = [entry]

    from custom_components.bosch_shc_camera import media_source as ms
    with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
        hass._smb_src = src
        hass._smb_backend = backend
        hass._smb_patch_target = ms
    return hass, src, backend


class TestBrowseSmb:

    def _browse(self, identifier, years=None, months=None, days=None, events=None):
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _Source, _SmbBackend,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        backend = MagicMock(spec=_SmbBackend)
        backend.list_years.return_value = years or []
        backend.list_months.return_value = months or []
        backend.list_days.return_value = days or []
        backend.list_events.return_value = events or []

        src = _Source(entry_id="01ENT", kind="S", label="NAS \\\\nas\\M")

        hass = MagicMock()
        hass.data = {}

        src_obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            return src_obj._browse(identifier)

    def test_root_lists_years(self):
        out = self._browse("", years=["2026", "2025"])
        assert len(out.children) == 2
        assert out.children[0].title == "2026"

    def test_year_lists_months(self):
        out = self._browse("01ENT/2026", months=["05", "04"])
        assert len(out.children) == 2
        assert out.children[0].title == "05"

    def test_year_month_lists_days(self):
        out = self._browse("01ENT/2026/05", days=["22", "07"])
        assert len(out.children) == 2
        assert "2026-05-22" in out.children[0].title

    def test_year_month_day_lists_events(self):
        stem = "Terrasse_2026-05-07_10-00-00_MOVEMENT_AB12CD34"
        evs = [(f"{stem}.mp4", f"{stem}.jpg",
                {"camera": "Terrasse", "date": "2026-05-07", "time": "10-00-00",
                 "etype": "MOVEMENT"})]
        out = self._browse("01ENT/2026/05/07", events=evs)
        assert len(out.children) == 1
        assert out.children[0].can_play is True

    def test_event_thumbnail_set_when_image_present(self):
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        evs = [(f"{stem}.mp4", f"{stem}.jpg",
                {"camera": "Cam", "date": "2026-05-07", "time": "08-00-00",
                 "etype": "MOVEMENT"})]
        out = self._browse("01ENT/2026/05/07", events=evs)
        assert out.children[0].thumbnail is not None

    def test_event_no_thumbnail_when_image_none(self):
        stem = "Cam_2026-05-07_08-00-00_MOVEMENT_DEADBEEF"
        evs = [(f"{stem}.mp4", None,
                {"camera": "Cam", "date": "2026-05-07", "time": "08-00-00",
                 "etype": "MOVEMENT"})]
        out = self._browse("01ENT/2026/05/07", events=evs)
        assert out.children[0].thumbnail is None

    def test_too_deep_raises_unresolvable(self):
        from homeassistant.components.media_source.error import Unresolvable
        with pytest.raises(Unresolvable):
            self._browse("01ENT/2026/05/07/file.mp4/extra")

    def test_single_source_skips_kind_token_for_year(self):
        """Single SMB source: '01ENT/2026' directly navigates to months."""
        out = self._browse("01ENT/2026", months=["05"])
        assert out.children[0].title == "05"


# ─────────────────────────────────────────────────────────────────────────────
# _enabled_sources — filter paths
# ─────────────────────────────────────────────────────────────────────────────

class TestEnabledSourcesFilters:

    def _entry(self, opts: dict):
        entry = MagicMock()
        entry.entry_id = "01ENT"
        entry.runtime_data = SimpleNamespace(options=opts)
        return entry

    def _call(self, entry):
        from custom_components.bosch_shc_camera import media_source as ms
        hass = MagicMock()
        hass.config_entries.async_loaded_entries.return_value = [entry]
        hass.data = {}
        return ms._enabled_sources(hass)

    def test_none_filter_excludes_entry(self):
        entry = self._entry({"media_browser_source": "none", "download_path": "/tmp"})
        assert self._call(entry) == []

    def test_smb_filter_excludes_local_backend(self, tmp_path):
        """media_browser_source='smb' must not include the local backend."""
        entry = self._entry({
            "media_browser_source": "smb",
            "download_path": str(tmp_path),
            "enable_smb_upload": False,
        })
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "L" not in kinds

    def test_local_filter_excludes_smb_backend(self, tmp_path):
        """media_browser_source='local' must not include the SMB backend."""
        entry = self._entry({
            "media_browser_source": "local",
            "download_path": str(tmp_path),
            "enable_smb_upload": True,
            "upload_protocol": "smb",
            "smb_server": "nas",
            "smb_share": "M",
            "smb_username": "u",
            "smb_password": "p",
        })
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "S" not in kinds

    def test_nvr_backend_added_when_enabled(self, tmp_path):
        """enable_nvr=True with an existing dir must produce an NVR source."""
        nvr_base = tmp_path / "nvr"
        nvr_base.mkdir()
        entry = self._entry({
            "enable_nvr": True,
            "nvr_base_path": str(nvr_base),
            "download_path": "",
        })
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "N" in kinds

    def test_nvr_skipped_when_dir_missing(self, tmp_path):
        entry = self._entry({
            "enable_nvr": True,
            "nvr_base_path": str(tmp_path / "no-such-dir"),
            "download_path": "",
        })
        sources = self._call(entry)
        kinds = [src.kind for src, _ in sources]
        assert "N" not in kinds

    def test_download_path_dir_created_if_missing(self, tmp_path):
        """Missing download_path directory must be created by _enabled_sources."""
        new_dir = tmp_path / "auto_created"
        assert not new_dir.exists()
        entry = self._entry({"download_path": str(new_dir)})
        self._call(entry)
        assert new_dir.is_dir()


# ─────────────────────────────────────────────────────────────────────────────
# _browse_entry_root — NVR / SMB single-source dispatch
# ─────────────────────────────────────────────────────────────────────────────

class TestBrowseEntryRootDispatch:

    def test_nvr_single_source_lists_cameras(self, tmp_path):
        """Single NVR source: root browse goes straight to camera list."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _NvrBackend, _Source,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        nvr_base = tmp_path / "nvr"
        (nvr_base / "Terrasse").mkdir(parents=True)
        backend = _NvrBackend(str(nvr_base))
        src = _Source(entry_id="01ENT", kind="N", label="Aufnahmen")

        hass = MagicMock()
        hass.data = {}
        obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = obj._browse("")
        cameras = [c.title for c in out.children]
        assert "Terrasse" in cameras

    def test_smb_single_source_root_shows_years(self):
        """Single SMB source: root browse shows year folders."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _SmbBackend, _Source,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        backend = MagicMock(spec=_SmbBackend)
        backend.list_years.return_value = ["2026"]
        src = _Source(entry_id="01ENT", kind="S", label="NAS")

        hass = MagicMock()
        hass.data = {}
        obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src, backend)]):
            out = obj._browse("")
        assert out.children[0].title == "2026"

    def test_multi_source_entry_root_shows_chooser(self, tmp_path):
        """Two backends on same entry: root shows source chooser."""
        from custom_components.bosch_shc_camera.media_source import (
            BoschCameraMediaSource, _LocalBackend, _NvrBackend, _Source,
        )
        from custom_components.bosch_shc_camera import media_source as ms

        local = _LocalBackend(str(tmp_path))
        nvr_base = tmp_path / "nvr"
        nvr_base.mkdir()
        nvr = _NvrBackend(str(nvr_base))

        src_l = _Source("01ENT", "L", "Lokal")
        src_n = _Source("01ENT", "N", "Aufnahmen")

        hass = MagicMock()
        hass.data = {}
        obj = BoschCameraMediaSource(hass)
        with patch.object(ms, "_enabled_sources", return_value=[(src_l, local), (src_n, nvr)]):
            out = obj._browse("01ENT")
        kinds = {c.identifier.split("/")[-1] for c in out.children}
        assert "L" in kinds
        assert "N" in kinds


# ─────────────────────────────────────────────────────────────────────────────
# _SmbBackend._ensure_session — caching
# ─────────────────────────────────────────────────────────────────────────────

class TestSmbBackendSessionCache:

    def test_session_registered_only_once(self):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend
        hass = MagicMock()
        hass.data = {}
        b = _SmbBackend(hass, {
            "smb_server": "nas", "smb_share": "M",
            "smb_username": "u", "smb_password": "p", "smb_base_path": "",
        })
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            b._ensure_session()
            b._ensure_session()
        fake.register_session.assert_called_once()

    def test_session_key_stored_in_hass_data(self):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend, SMB_SESSION_KEY
        hass = MagicMock()
        hass.data = {}
        b = _SmbBackend(hass, {
            "smb_server": "nas2", "smb_share": "M2",
            "smb_username": "bob", "smb_password": "x", "smb_base_path": "",
        })
        fake = _fake_smbclient()
        with patch.dict(sys.modules, {"smbclient": fake}):
            b._ensure_session()
        assert ("nas2", "bob") in hass.data[SMB_SESSION_KEY]
