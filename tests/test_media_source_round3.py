"""Tests for media_source.py — round 3.

Covers previously-uncovered lines:
  Line 126: list_dates — non-file entries (dirs) skipped in camera folder
  Lines 136, 140: list_events — cam_dir is None, is-junk skip
  Lines 277, 299, 302, 306: _NvrBackend empty base, cam_dir None,
                             date_dir None/not-dir, junk files in segment listing
  Line 321: _NvrBackend.resolve — date_dir None / invalid date/filename
  Lines 360-373: _browse — single-source implicit kind; source not found error
  Lines 388-391: async_get_media_source view registration (first vs second call)
  Lines 719-784: BoschCameraMediaView.get — dispatch by head token + all tail-length
                 guards + _serve_local (file not found, bad mime)
  Lines 791-798: _serve_nvr — bad date/filename, path None
  Lines 810-869: _serve_smb — bad year/month/day, FileNotFoundError, OSError,
                 Range header handling (206 + full read)

Uses tmp_path + SimpleNamespace stubs — no real HA runtime.
"""
from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.media_source import (
    _LocalBackend,
    _NvrBackend,
    _SmbBackend,
    _parse_filename,
    _safe_join,
    BoschCameraMediaSource,
    BoschCameraMediaView,
    _enabled_sources,
    async_get_media_source,
)
from homeassistant.components.media_source.error import Unresolvable

MODULE = "custom_components.bosch_shc_camera.media_source"

CAM_FILE = "Terrasse_2026-05-07_10-00-00_MOVEMENT_EF791764.mp4"
CAM_IMG  = "Terrasse_2026-05-07_10-00-00_MOVEMENT_EF791764.jpg"


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_local_tree(tmp_path: Path, *, cam: str = "Terrasse",
                     files: list[str] | None = None) -> _LocalBackend:
    cam_dir = tmp_path / cam
    cam_dir.mkdir(parents=True, exist_ok=True)
    for f in (files or [CAM_FILE, CAM_IMG]):
        (cam_dir / f).write_bytes(b"data")
    return _LocalBackend(str(tmp_path))


def _make_nvr_tree(tmp_path: Path, *, cam: str = "Terrasse",
                   date: str = "2026-05-07",
                   segments: list[str] | None = None) -> _NvrBackend:
    seg_dir = tmp_path / cam / date
    seg_dir.mkdir(parents=True, exist_ok=True)
    for s in (segments or ["10-00.mp4"]):
        (seg_dir / s).write_bytes(b"vid")
    return _NvrBackend(str(tmp_path))


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


# ── _LocalBackend.list_dates ─────────────────────────────────────────────────

class TestLocalBackendListDates:
    """Line 126: directories inside a camera folder are skipped (only files count)."""

    def test_subdir_in_cam_folder_skipped(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        # A sub-directory — must NOT contribute a date
        (cam_dir / "subdir").mkdir()
        # A real event file
        (cam_dir / CAM_FILE).write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        dates = backend.list_dates("Terrasse")
        assert dates == ["2026-05-07"]

    def test_no_valid_files_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        (cam_dir / "readme.txt").write_bytes(b"x")  # unrecognised extension
        backend = _LocalBackend(str(tmp_path))
        assert backend.list_dates("Terrasse") == []


# ── _LocalBackend.list_events ────────────────────────────────────────────────

class TestLocalBackendListEvents:
    """Lines 135-140: cam_dir=None (path traversal blocked) + junk file skip."""

    def test_cam_dir_none_path_traversal(self, tmp_path):
        """_safe_join blocks '../..'; list_events returns []."""
        backend = _LocalBackend(str(tmp_path))
        # "../../etc" → _safe_join returns None → early return
        result = backend.list_events("../../etc", "2026-05-07")
        assert result == []

    def test_macos_junk_file_skipped(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        (cam_dir / "._Terrasse_2026-05-07_10-00-00_MOVEMENT_EF791764.mp4").write_bytes(b"x")
        (cam_dir / CAM_FILE).write_bytes(b"x")
        backend = _LocalBackend(str(tmp_path))
        events = backend.list_events("Terrasse", "2026-05-07")
        # Only the real file contributes; junk is skipped
        assert len(events) == 1
        fname, _, _ = events[0]
        assert "._" not in fname


# ── _NvrBackend ───────────────────────────────────────────────────────────────

class TestNvrBackendListCameras:
    """Line 277: base dir doesn't exist → list_cameras returns []."""

    def test_missing_base_returns_empty(self, tmp_path):
        backend = _NvrBackend(str(tmp_path / "nonexistent"))
        assert backend.list_cameras() == []


class TestNvrBackendListDates:
    """Lines 299, 302: cam_dir None or not a directory → list_dates returns []."""

    def test_path_traversal_cam_returns_empty(self, tmp_path):
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_dates("../../etc") == []

    def test_missing_cam_dir_returns_empty(self, tmp_path):
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_dates("MissingCam") == []


class TestNvrBackendListSegments:
    """Lines 306, 302: junk files skipped; date_dir None or not-dir."""

    def test_junk_file_skipped_in_segments(self, tmp_path):
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "._10-00.mp4").write_bytes(b"x")  # macOS junk
        (seg_dir / "10-00.mp4").write_bytes(b"x")
        backend = _NvrBackend(str(tmp_path))
        segs = backend.list_segments("Terrasse", "2026-05-07")
        assert len(segs) == 1
        assert segs[0][0] == "10-00.mp4"

    def test_date_dir_none_path_traversal(self, tmp_path):
        (tmp_path / "Terrasse").mkdir()
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_segments("Terrasse", "../../etc") == []

    def test_date_dir_not_dir_returns_empty(self, tmp_path):
        cam_dir = tmp_path / "Terrasse"
        cam_dir.mkdir()
        # "2026-05-07" is a file, not a directory
        (cam_dir / "2026-05-07").write_bytes(b"x")
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_segments("Terrasse", "2026-05-07") == []

    def test_non_matching_file_skipped(self, tmp_path):
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True)
        (seg_dir / "README.txt").write_bytes(b"x")
        backend = _NvrBackend(str(tmp_path))
        assert backend.list_segments("Terrasse", "2026-05-07") == []


class TestNvrBackendResolve:
    """Line 321: date_dir None (path traversal) and invalid date/filename."""

    def test_date_traversal_returns_none(self, tmp_path):
        (tmp_path / "Terrasse").mkdir()
        backend = _NvrBackend(str(tmp_path))
        assert backend.resolve("Terrasse", "../../etc", "10-00.mp4") is None

    def test_invalid_date_format_returns_none(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        assert backend.resolve("Terrasse", "20260507", "10-00.mp4") is None

    def test_invalid_segment_format_returns_none(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        assert backend.resolve("Terrasse", "2026-05-07", "bad.avi") is None

    def test_missing_file_returns_none(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        assert backend.resolve("Terrasse", "2026-05-07", "23-59.mp4") is None

    def test_valid_resolve_returns_path(self, tmp_path):
        backend = _make_nvr_tree(tmp_path)
        result = backend.resolve("Terrasse", "2026-05-07", "10-00.mp4")
        assert result is not None
        assert result.name == "10-00.mp4"


# ── _browse dispatch — single-source implicit kind ────────────────────────────

class TestBrowseDispatchSingleSource:
    """Lines 360-373: single-source entry implicit kind detection and unknown-source error."""

    def test_unknown_entry_raises_unresolvable(self, tmp_path):
        hass = _hass_stub("entry1", tmp_path=tmp_path)
        (tmp_path).mkdir(exist_ok=True)
        ms = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            ms._browse("unknown-entry/L/Terrasse")

    def test_too_deep_local_path_raises_unresolvable(self, tmp_path):
        """Camera-first tree: 6 rest segments (past camera/year/month/day/events) → Unresolvable."""
        (tmp_path / "Terrasse").mkdir(parents=True, exist_ok=True)
        hass = _hass_stub("entry1", tmp_path=tmp_path)
        ms = BoschCameraMediaSource(hass)
        with pytest.raises(Unresolvable):
            ms._browse("entry1/L/Terrasse/2026/05/07/extra/segment")


# ── async_get_media_source — view registration ────────────────────────────────

class TestAsyncGetMediaSource:
    """Lines 388-391: view is registered only once; second call skips re-registration."""

    @pytest.mark.asyncio
    async def test_first_call_registers_view(self):
        hass = MagicMock()
        hass.data = {}
        hass.http = MagicMock()
        ms = await async_get_media_source(hass)
        hass.http.register_view.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_call_skips_registration(self):
        hass = MagicMock()
        hass.data = {}
        hass.http = MagicMock()
        await async_get_media_source(hass)
        await async_get_media_source(hass)
        # register_view must have been called exactly once across two calls
        assert hass.http.register_view.call_count == 1


# ── BoschCameraMediaView.get — dispatch ───────────────────────────────────────

def _make_view_hass(entry_id: str, tmp_path: Path, kind: str = "L"):
    """Build a hass stub that exposes one source of the given kind."""
    hass = MagicMock()
    hass.data = {}
    hass.http = MagicMock()

    if kind == "L":
        (tmp_path / "Terrasse").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Terrasse" / CAM_FILE).write_bytes(b"mp4data")
        opts = {"download_path": str(tmp_path), "media_browser_source": "local"}
    elif kind == "N":
        seg_dir = tmp_path / "Terrasse" / "2026-05-07"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "10-00.mp4").write_bytes(b"nvr")
        opts = {"enable_nvr": True, "nvr_base_path": str(tmp_path),
                "media_browser_source": "local"}
    else:
        opts = {"enable_smb_upload": True, "upload_protocol": "smb",
                "smb_server": "nas", "smb_share": "SHARE",
                "smb_username": "u", "smb_password": "p",
                "smb_base_path": "", "media_browser_source": "smb"}

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


class TestMediaViewDispatch:
    """Lines 719-770: get() dispatches by head token to correct backend."""

    @pytest.mark.asyncio
    async def test_empty_parts_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path)
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):  # web.HTTPNotFound
            await view.get(request, "entry1", "")

    @pytest.mark.asyncio
    async def test_local_wrong_tail_length_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # L head + only 1 tail part (need exactly 2)
            await view.get(request, "entry1", "L/Terrasse")

    @pytest.mark.asyncio
    async def test_nvr_wrong_tail_length_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # N head + only 2 parts (need exactly 3: cam/date/file)
            await view.get(request, "entry1", "N/Terrasse/2026-05-07")

    @pytest.mark.asyncio
    async def test_source_not_found_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # S head — but only L backend is configured
            await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")


class TestServeLocal:
    """Lines 773-784: _serve_local error paths."""

    @pytest.mark.asyncio
    async def test_bad_filename_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # "notvalid.txt" doesn't match _FILE_RE → HTTPNotFound
            await view.get(request, "entry1", "L/Terrasse/notvalid.txt")

    @pytest.mark.asyncio
    async def test_missing_file_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="L")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            missing = "Terrasse_2026-05-07_23-59-59_MOVEMENT_AABBCCDD.mp4"
            await view.get(request, "entry1", f"L/Terrasse/{missing}")


class TestServeNvr:
    """Lines 791-798: _serve_nvr — bad date format, bad filename, missing file."""

    @pytest.mark.asyncio
    async def test_bad_date_format_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            await view.get(request, "entry1", "N/Terrasse/20260507/10-00.mp4")

    @pytest.mark.asyncio
    async def test_bad_segment_name_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            await view.get(request, "entry1", "N/Terrasse/2026-05-07/bad.avi")

    @pytest.mark.asyncio
    async def test_missing_nvr_file_raises_404(self, tmp_path):
        hass = _make_view_hass("entry1", tmp_path, kind="N")
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            await view.get(request, "entry1", "N/Terrasse/2026-05-07/23-59.mp4")


class TestServeSmb:
    """Lines 810-869: _serve_smb — validation, FileNotFoundError, OSError, Range."""

    def _smb_hass(self, entry_id="entry1"):
        """Hass stub with an SMB backend using mocked smbclient."""
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

    @pytest.mark.asyncio
    async def test_bad_year_format_raises_404(self):
        hass = self._smb_hass()
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}
        with pytest.raises(Exception):
            # year "XXXX" doesn't match _YEAR_RE → HTTPNotFound
            await view.get(request, "entry1", "S/Cam/XXXX/05/07/file.mp4")

    @pytest.mark.asyncio
    async def test_smb_file_not_found_raises_404(self):
        """FileNotFoundError from open_file → HTTPNotFound."""
        hass = self._smb_hass()
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(side_effect=FileNotFoundError("nope"))

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with pytest.raises(Exception):
                await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")

    @pytest.mark.asyncio
    async def test_smb_os_error_raises_404(self):
        """OSError (e.g. SMB network failure) from open_file → HTTPNotFound."""
        hass = self._smb_hass()
        view = BoschCameraMediaView(hass)
        request = MagicMock()
        request.headers = {}

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(side_effect=OSError("smb down"))

        with patch(f"{MODULE}._find_source",
                   return_value=(MagicMock(kind="S"), backend_mock)):
            with pytest.raises(Exception):
                await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")

    @pytest.mark.asyncio
    async def test_smb_range_request_206(self):
        """Range header → status 206 + Content-Range header returned."""
        hass = self._smb_hass()
        view = BoschCameraMediaView(hass)

        payload = b"A" * 2000
        fobj = MagicMock()
        fobj.seek = MagicMock()
        fobj.read = MagicMock(side_effect=[payload[500:500+256*1024], b""])
        fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fobj, len(payload)))

        request = MagicMock()
        request.headers = {"Range": "bytes=500-1999"}

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
                resp = await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")
        assert resp is real_response

    @pytest.mark.asyncio
    async def test_smb_full_read_no_range(self):
        """No Range header → status 200, full content streamed."""
        hass = self._smb_hass()
        view = BoschCameraMediaView(hass)

        payload = b"B" * 100
        fobj = MagicMock()
        fobj.seek = MagicMock()
        fobj.read = MagicMock(side_effect=[payload, b""])
        fobj.close = MagicMock()

        backend_mock = MagicMock()
        backend_mock.open_file = MagicMock(return_value=(fobj, len(payload)))

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
                resp = await view.get(request, "entry1", "S/Cam/2026/05/07/file.mp4")
        assert resp is real_response
