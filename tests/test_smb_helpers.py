"""Tests for smb.py helper functions.

Pure functions, no I/O, no SMB protocol mocking needed:
  - `_safe_name` — sanitizes camera names for directory paths (path-traversal guard)
  - `_is_safe_bosch_url` — duplicate of __init__ SSRF guard, must enforce same contract
  - `sync_local_save` — FCM-triggered local file save (replaces coordinator auto-download)
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── _safe_name (path-traversal sanitization) ────────────────────────────


class TestSafeName:
    def test_normal_name_passes_through(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("Terrasse") == "Terrasse"

    def test_spaces_and_hyphens_allowed(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("Bosch Terrasse-Kamera") == "Bosch Terrasse-Kamera"

    def test_dots_allowed(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("Cam.Front") == "Cam.Front"

    def test_double_dot_replaced(self):
        """Path-traversal sequence must be defanged."""
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("../etc/passwd")
        assert ".." not in result
        # Must not contain a path separator
        assert "/" not in result

    def test_slashes_replaced(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("evil/path/here")
        assert "/" not in result

    def test_backslash_replaced(self):
        """Windows path separator must also be sanitized."""
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("evil\\path")
        assert "\\" not in result

    def test_special_chars_replaced(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("name<with>special|chars*")
        for ch in "<>|*":
            assert ch not in result

    def test_unicode_replaced(self):
        """Non-word characters become `_` — keeps fs-safe."""
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("Außenkamera")
        # `ß` is `\w` in Python regex so it stays — both fine for filesystem
        assert len(result) > 0

    def test_truncates_to_64_chars(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        long_name = "x" * 100
        assert len(_safe_name(long_name)) == 64

    def test_empty_string_returns_empty(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        assert _safe_name("") == ""

    def test_only_unsafe_chars_yields_underscores(self):
        from custom_components.bosch_shc_camera.smb import _safe_name
        result = _safe_name("///***")
        assert all(c == "_" for c in result)


# ── sync_local_save (FCM-triggered local file save) ──────────────────────
# Regression suite for the v11.0.8 change that replaced the coordinator-based
# `enable_auto_download` polling with FCM-triggered `sync_local_save`.
# User-reported: media browser empty despite `enable_auto_download` checked
# (forum.simon42.com PN from geotie, 2026-05-06). Root causes:
#   1. Coordinator pulled ALL events from Bosch Cloud periodically (not allowed).
#   2. _download_one saved files as {date}_{time}_{type}_{id}.ext — no camera
#      prefix — so _FILE_RE in media_source.py never matched → dates list empty.
# Fix: FCM-triggered save only, filename includes camera prefix so _FILE_RE matches.

_FILE_RE = re.compile(
    r"^(?P<camera>.+?)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})"
    r"_(?P<etype>[A-Z_]+)_[0-9A-F]+\.(?P<ext>jpg|jpeg|mp4)$",
    re.IGNORECASE,
)


def _make_coordinator(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(options={"download_path": str(tmp_path)})


def _make_ev(
    timestamp: str = "2026-05-06T17:57:28.000Z",
    event_type: str = "MOVEMENT",
    ev_id: str = "EE30D727-0000-0000-0000-000000000000",
    image_url: str = "https://residential.cbs.boschsecurity.com/snap.jpg",
    clip_url: str = "https://residential.cbs.boschsecurity.com/clip.mp4",
    clip_status: str = "Done",
) -> dict:
    return {
        "timestamp": timestamp,
        "eventType": event_type,
        "id": ev_id,
        "imageUrl": image_url,
        "videoClipUrl": clip_url,
        "videoClipUploadStatus": clip_status,
    }


class TestSyncLocalSave:
    def test_filename_matches_file_re(self, tmp_path):
        """v11.0.8: saved filename must match _FILE_RE so media_source can list it.

        The old _download_one saved {date}_{time}_{type}_{id}.ext — missing the
        camera prefix — causing list_dates() to silently return [] and the Media
        Browser to appear empty.
        """
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = _make_coordinator(tmp_path)
        ev = _make_ev()
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content = lambda chunk_size: [b"FAKE"]
        with patch("requests.Session.get", return_value=resp):
            sync_local_save(coord, ev, "tok", "Innenbereich")
        saved = list((tmp_path / "Innenbereich").rglob("*.*"))
        assert saved, "no files saved"
        for f in saved:
            assert _FILE_RE.match(f.name), f"filename {f.name!r} does not match _FILE_RE"

    def test_camera_subdir_created(self, tmp_path):
        """Camera name subdirectory is created inside download_path."""
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = _make_coordinator(tmp_path)
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content = lambda chunk_size: [b"FAKE"]
        with patch("requests.Session.get", return_value=resp):
            sync_local_save(coord, _make_ev(), "tok", "Terrasse")
        assert (tmp_path / "Terrasse").is_dir()

    def test_clip_skipped_when_status_not_done(self, tmp_path):
        """MP4 not saved when videoClipUploadStatus != Done."""
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = _make_coordinator(tmp_path)
        ev = _make_ev(clip_status="Pending")
        resp = MagicMock()
        resp.status_code = 200
        resp.iter_content = lambda chunk_size: [b"FAKE"]
        with patch("requests.Session.get", return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")
        saved = list((tmp_path / "Terrasse").rglob("*.mp4"))
        assert saved == [], "MP4 must not be saved when clip status is not Done"

    def test_unsafe_url_not_fetched(self, tmp_path):
        """SSRF guard: URLs not on bosch domains must be skipped."""
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = _make_coordinator(tmp_path)
        ev = _make_ev(image_url="https://attacker.com/evil.jpg", clip_url="")
        with patch("requests.Session.get") as mock_get:
            sync_local_save(coord, ev, "tok", "Terrasse")
            mock_get.assert_not_called()

    def test_empty_download_path_is_noop(self, tmp_path):
        """Empty download_path → function returns immediately, no files saved."""
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = SimpleNamespace(options={"download_path": ""})
        with patch("requests.Session.get") as mock_get:
            sync_local_save(coord, _make_ev(), "tok", "Terrasse")
            mock_get.assert_not_called()

    def test_existing_file_not_redownloaded(self, tmp_path):
        """Files that already exist are skipped (idempotent on FCM duplicates)."""
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = _make_coordinator(tmp_path)
        ev = _make_ev()
        # Pre-create the expected JPEG in the nested year/month/day folder
        stem = "Terrasse_2026-05-06_17-57-28_MOVEMENT_EE30D727"
        nested_dir = tmp_path / "Terrasse" / "2026" / "05" / "06"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / f"{stem}.jpg").write_bytes(b"existing")
        with patch("requests.Session.get") as mock_get:
            sync_local_save(coord, ev, "tok", "Terrasse")
            for call in mock_get.call_args_list:
                url = call.args[0] if call.args else call.kwargs.get("url", "")
                assert "snap.jpg" not in url, "JPEG must not be re-fetched"


# ── _is_safe_bosch_url (smb copy) ───────────────────────────────────────


class TestSmbSafeBoschUrl:
    @pytest.mark.parametrize("url", [
        "https://residential.cbs.boschsecurity.com/event/snap.jpg",
        "https://api.bosch.com/x",
    ])
    def test_legit_urls_allowed(self, url):
        from custom_components.bosch_shc_camera.smb import _is_safe_bosch_url
        assert _is_safe_bosch_url(url) is True

    @pytest.mark.parametrize("url", [
        "http://residential.cbs.boschsecurity.com/x",  # not HTTPS
        "https://attacker.com/x",
        "https://127.0.0.1/x",
        "",
    ])
    def test_unsafe_urls_rejected(self, url):
        from custom_components.bosch_shc_camera.smb import _is_safe_bosch_url
        assert _is_safe_bosch_url(url) is False
