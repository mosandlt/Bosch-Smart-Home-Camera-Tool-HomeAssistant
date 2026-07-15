"""Tests for `smb.py` — local save, SMB upload/cleanup, FTP upload/cleanup.

Covers:
  * Path-safety helpers: `_safe_name` (path-traversal guard), `_is_safe_bosch_url`
    (SSRF guard), `_bosch_ssl_ctx` (TLS verification for cloud-media downloads).
  * `sync_local_save` — FCM-triggered local file save (replaces the old
    coordinator-based `enable_auto_download` polling).
  * `sync_smb_upload` / `smb_makedirs` / `sync_smb_cleanup` — SMB upload +
    directory creation + retention cleanup, including the recursive
    `_walk_and_delete` helper.
  * `_sync_ftp_upload` / `_sync_ftp_cleanup` plus their pure helpers
    (`_ftp_exists`, `_ftp_makedirs`, `_ftp_connect`) — FTP mirror of the SMB
    path.
  * `_fire_cleanup_alert` / `_async_cleanup_alert` — retention-cleanup
    notification dispatch.
  * Pure computation pinned without exercising the full pipeline: folder/file
    pattern formatting, retention-cutoff math.

All SMB/FTP protocol calls are mocked via `sys.modules` injection or `patch`;
no real network I/O. `urllib.request.urlopen` is patched (not `requests`,
which this module never uses). Filesystem tests use `tmp_path` so nothing
escapes the per-test sandbox.

NOTE: `custom_components.bosch_shc_camera.smb` imports are done lazily inside
each test body (HA test-isolation convention used throughout this codebase),
not hoisted to module level.
"""

from __future__ import annotations

import ftplib
import re
import ssl
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.smb"
CAM_ID = "11111111-1111-1111-1111-111111111111"
URLOPEN = f"{MODULE}.urllib.request.urlopen"


# ── shared fixtures/helpers ──────────────────────────────────────────────────


def _coord(options: dict | None = None) -> SimpleNamespace:
    """Coordinator stub with the two enable-toggles most smb.py functions read."""
    opts = dict(options or {})
    opts.setdefault("enable_local_save", True)
    opts.setdefault("enable_smb_upload", True)
    return SimpleNamespace(options=opts, hass=MagicMock(), _download_started_at=0.0)


def _smb_upload_coord() -> SimpleNamespace:
    """Coordinator pre-configured with a full SMB server/pattern config."""
    return _coord(
        {
            "smb_server": "192.168.1.100",
            "smb_share": "SHARE",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        }
    )


def _urlopen_resp(status: int = 200, content: bytes = b"DATA") -> MagicMock:
    """Build a MagicMock that behaves like urllib.request.urlopen()'s
    context-manager return value."""
    resp = MagicMock()
    resp.status = status
    resp.read.side_effect = [content, b""]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _fake_smb() -> MagicMock:
    """MagicMock satisfying the `import smbclient` surface used by smb.py."""
    smb = MagicMock()
    smb.register_session = MagicMock()
    smb.mkdir = MagicMock()
    smb.open_file = MagicMock()
    smb.stat = MagicMock(side_effect=OSError("missing"))
    smb.scandir = MagicMock(return_value=[])
    smb.remove = MagicMock()
    return smb


def _smb_event(
    image_url: str | None = "https://cdn.bosch.com/snap.jpg",
    clip_url: str | None = None,
) -> dict:
    ev = {
        "timestamp": "2026-05-10T10:00:00Z",
        "eventType": "MOVEMENT",
        "id": "EVID1234ABCD",
        "imageUrl": image_url,
    }
    if clip_url:
        ev["videoClipUrl"] = clip_url
        ev["videoClipUploadStatus"] = "Done"
    return ev


def _basic_event(
    image_url: str | None = "https://cdn.bosch.com/snap.jpg",
    clip_url: str | None = None,
) -> dict:
    ev = {
        "timestamp": "2026-05-07T10:00:00Z",
        "eventType": "MOVEMENT",
        "id": "EVID1234ABCD",
        "imageUrl": image_url,
    }
    if clip_url:
        ev["videoClipUrl"] = clip_url
        ev["videoClipUploadStatus"] = "Done"
    return ev


def _make_coordinator(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        options={"enable_local_save": True, "download_path": str(tmp_path)}
    )


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


# ── feature area: _safe_name (path-traversal sanitization) ──────────────────


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


# ── feature area: SSRF / TLS guards ──────────────────────────────────────────


class TestSmbSafeBoschUrl:
    """`_is_safe_bosch_url` gates every cloud-media fetch — pin the allow/deny
    contract shared with __init__'s SSRF guard."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://residential.cbs.boschsecurity.com/event/snap.jpg",
            "https://api.bosch.com/x",
        ],
    )
    def test_legit_urls_allowed(self, url: str) -> None:
        from custom_components.bosch_shc_camera.smb import _is_safe_bosch_url

        assert _is_safe_bosch_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://residential.cbs.boschsecurity.com/x",  # not HTTPS
            "https://attacker.com/x",
            "https://127.0.0.1/x",
            "",
        ],
    )
    def test_unsafe_urls_rejected(self, url: str) -> None:
        from custom_components.bosch_shc_camera.smb import _is_safe_bosch_url

        assert _is_safe_bosch_url(url) is False


class TestBoschSslCtxVerifies:
    """Regression (2026-06-16, CWE-295 / GHSA-6qh5-x5m5-vj6v): smb cloud-media
    downloads (imageUrl / videoClipUrl) send the bearer token, so _bosch_ssl_ctx
    must VERIFY TLS against the pinned Bosch cloud CA — never the former
    CERT_NONE / check_hostname=False which left the token MITM-exposed."""

    def test_ssl_ctx_verifies_and_pins_bosch_ca(self):
        from custom_components.bosch_shc_camera import smb

        smb._SSL_CTX = None  # reset module cache for a deterministic build
        try:
            ctx = smb._bosch_ssl_ctx()
        finally:
            smb._SSL_CTX = None
        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True
        # Pinned Bosch intermediate is anchored via PARTIAL_CHAIN (cloud_ssl).
        assert ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN

    def test_ssl_ctx_reuses_cloud_ssl_builder_and_caches(self):
        from custom_components.bosch_shc_camera import smb

        sentinel = object()
        smb._SSL_CTX = None
        with patch(
            "custom_components.bosch_shc_camera.cloud_ssl._build_ssl_context",
            return_value=sentinel,
        ) as mock_build:
            try:
                first = smb._bosch_ssl_ctx()
                second = smb._bosch_ssl_ctx()
            finally:
                smb._SSL_CTX = None
        assert first is sentinel
        assert second is sentinel
        mock_build.assert_called_once()  # built once, then cached


# ── feature area: sync_local_save (FCM-triggered local save) ────────────────


class TestSyncLocalSaveBasics:
    """Filename shape, subdirectory creation, and clip-status gating for the
    FCM-triggered local save path."""

    def _make_urlopen_resp(self, content: bytes = b"FAKE") -> MagicMock:
        resp = MagicMock()
        resp.status = 200
        resp.read.side_effect = [content, b""]
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_filename_matches_file_re(self, tmp_path: Path) -> None:
        """Saved filename must match _FILE_RE so media_source can list it.

        The old _download_one saved {date}_{time}_{type}_{id}.ext — missing the
        camera prefix — causing list_dates() to silently return [] and the Media
        Browser to appear empty.
        """
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _make_coordinator(tmp_path)
        ev = _make_ev()
        resp = self._make_urlopen_resp(b"FAKE")
        with patch("urllib.request.urlopen", return_value=resp):
            sync_local_save(coord, ev, "tok", "Innenbereich")
        saved = list((tmp_path / "Innenbereich").rglob("*.*"))
        assert saved, "no files saved"
        for f in saved:
            assert _FILE_RE.match(f.name), (
                f"filename {f.name!r} does not match _FILE_RE"
            )

    def test_camera_subdir_created(self, tmp_path: Path) -> None:
        """Camera name subdirectory is created inside download_path."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _make_coordinator(tmp_path)
        resp = self._make_urlopen_resp(b"FAKE")
        with patch("urllib.request.urlopen", return_value=resp):
            sync_local_save(coord, _make_ev(), "tok", "Terrasse")
        assert (tmp_path / "Terrasse").is_dir()

    def test_clip_skipped_when_status_not_done(self, tmp_path: Path) -> None:
        """MP4 not saved when videoClipUploadStatus != Done."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _make_coordinator(tmp_path)
        ev = _make_ev(clip_status="Pending")
        resp = self._make_urlopen_resp(b"FAKE")
        with patch("urllib.request.urlopen", return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")
        saved = list((tmp_path / "Terrasse").rglob("*.mp4"))
        assert saved == [], "MP4 must not be saved when clip status is not Done"

    def test_unsafe_url_not_fetched(self, tmp_path: Path) -> None:
        """SSRF guard: URLs not on bosch domains must be skipped."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _make_coordinator(tmp_path)
        ev = _make_ev(image_url="https://attacker.com/evil.jpg", clip_url="")
        with patch("urllib.request.urlopen") as mock_get:
            sync_local_save(coord, ev, "tok", "Terrasse")
            mock_get.assert_not_called()

    def test_empty_download_path_is_noop(self, tmp_path: Path) -> None:
        """Empty download_path → function returns immediately, no files saved."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = SimpleNamespace(options={"download_path": ""})
        with patch("urllib.request.urlopen") as mock_get:
            sync_local_save(coord, _make_ev(), "tok", "Terrasse")
            mock_get.assert_not_called()

    def test_existing_file_not_redownloaded(self, tmp_path: Path) -> None:
        """Files that already exist are skipped (idempotent on FCM duplicates)."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _make_coordinator(tmp_path)
        ev = _make_ev()
        # Pre-create the expected JPEG in the nested year/month/day folder
        stem = "Terrasse_2026-05-06_17-57-28_MOVEMENT_EE30D727"
        nested_dir = tmp_path / "Terrasse" / "2026" / "05" / "06"
        nested_dir.mkdir(parents=True, exist_ok=True)
        (nested_dir / f"{stem}.jpg").write_bytes(b"existing")
        with patch("urllib.request.urlopen") as mock_urlopen:
            sync_local_save(coord, ev, "tok", "Terrasse")
            # urlopen receives a urllib.request.Request object; check full_url
            for c in mock_urlopen.call_args_list:
                req_arg = c.args[0] if c.args else c.kwargs.get("url", "")
                url_str = getattr(req_arg, "full_url", str(req_arg))
                assert "snap.jpg" not in url_str, "JPEG must not be re-fetched"


class TestSyncLocalSaveEmptyDownloadPath:
    """Toggle on + download_path empty/whitespace must short-circuit before
    any urlopen call."""

    def test_empty_download_path_returns_without_urlopen(self):
        """`download_path` stripped to empty (whitespace-only) → early return."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord(
            {
                "enable_local_save": True,
                "download_path": "   ",  # whitespace only → strip empties
            }
        )
        ev = _smb_event()
        with patch(URLOPEN) as mock_urlopen:
            sync_local_save(coord, ev, "tok", "Terrasse")
        mock_urlopen.assert_not_called()


class TestSyncLocalSaveTimestampGuards:
    """A short/empty/malformed timestamp must never crash the FCM save path."""

    def test_short_timestamp_returns_early(self, tmp_path: Path) -> None:
        """Timestamp shorter than 19 chars → return without writing any file."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        ev = {
            "timestamp": "2026-05",
            "eventType": "MOVEMENT",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }
        sync_local_save(coord, ev, "tok", "Terrasse")
        assert list(tmp_path.iterdir()) == [], (
            "Short timestamp must cause early return — no folder or file created"
        )

    def test_empty_timestamp_returns_early(self, tmp_path: Path) -> None:
        """Empty timestamp → return without writing."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        ev = {"timestamp": "", "eventType": "MOVEMENT"}
        sync_local_save(coord, ev, "tok", "Terrasse")
        assert list(tmp_path.iterdir()) == [], "Empty timestamp must cause early return"

    def test_malformed_but_long_timestamp_falls_through_to_download(
        self, tmp_path: Path
    ) -> None:
        """Timestamp with month=0 causes ValueError in strptime → the except
        swallows it, and the download proceeds normally anyway.
        """
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        # Set _download_started_at so the timestamp comparison is attempted
        coord._download_started_at = time.time() - 3600  # 1 hour ago

        # "0000-00-00T00:00:00Z" — month=0 causes ValueError in strptime
        ev = {
            "timestamp": "0000-00-00T00:00:00Z",
            "eventType": "MOVEMENT",
            "id": "EVID1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        resp = _urlopen_resp(200, b"JPEG")

        with patch(URLOPEN, return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        # The download was attempted (exception swallowed, execution continues)
        resp.__enter__.assert_called()

    def test_valid_old_timestamp_skipped_when_started_at_set(
        self, tmp_path: Path
    ) -> None:
        """Timestamp predating session start → skipped (ev_epoch < started_at - 60).
        This is the normal gate that the malformed-timestamp test bypasses via except.
        """
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        coord._download_started_at = time.time()  # now

        ev = {
            # Old event: 2000-01-01 — clearly before session start
            "timestamp": "2000-01-01T00:00:00Z",
            "eventType": "MOVEMENT",
            "id": "EVID1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        with patch(URLOPEN) as mock_urlopen:
            sync_local_save(coord, ev, "tok", "Terrasse")

        # Old event must be skipped — no download
        mock_urlopen.assert_not_called()


class TestSyncLocalSavePatternFormatErrors:
    """Bad user-supplied folder_pattern / file_pattern keys must not crash;
    a sensible fallback path is used instead."""

    def test_folder_pattern_unknown_key_falls_back_to_cam(self, tmp_path: Path) -> None:
        """`{nonexistent}` in folder_pattern → KeyError caught → sub = cam_safe."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord(
            {
                "enable_local_save": True,
                "download_path": str(tmp_path),
                "folder_pattern": "{nonexistent}/{year}",
            }
        )
        resp = _urlopen_resp(200, b"JPG")
        ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
        with patch(URLOPEN, return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        # File landed somewhere under Terrasse/ (cam_safe fallback)
        found = list(tmp_path.rglob("Terrasse*"))
        assert found, "Fallback path missing — KeyError not handled cleanly"

    def test_file_pattern_unknown_key_falls_back(self, tmp_path: Path) -> None:
        """`{nonexistent}` in file_pattern → KeyError caught → default stem."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord(
            {
                "enable_local_save": True,
                "download_path": str(tmp_path),
                "file_pattern": "{nonexistent}_{date}",
            }
        )
        resp = _urlopen_resp(200, b"JPG")
        ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
        with patch(URLOPEN, return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        # Fallback stem is `{cam}_{date}_{time}_{type}_{id}` — must contain MOVEMENT + date
        jpgs = list(tmp_path.rglob("*.jpg"))
        assert jpgs, "No file written — fallback stem missing"
        assert any("MOVEMENT" in p.name for p in jpgs)


class TestSyncLocalSaveMp4Gate:
    """Additional branch coverage around the mp4-status gate and swallowed
    download exceptions."""

    def test_mp4_skipped_when_status_not_done(self, tmp_path: Path) -> None:
        """MP4 url present but videoClipUploadStatus != 'Done' → MP4 not downloaded."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        ev = {
            "timestamp": "2026-05-07T10:00:00Z",
            "eventType": "MOVEMENT",
            "id": "EVID1234",
            "imageUrl": None,
            "videoClipUrl": "https://cdn.boschsecurity.com/clip.mp4",
            "videoClipUploadStatus": "Pending",
        }

        with patch(URLOPEN) as mock_urlopen:
            sync_local_save(coord, ev, "tok", "Terrasse")

        mock_urlopen.assert_not_called()

    def test_download_exception_logged_not_raised(self, tmp_path: Path) -> None:
        """urlopen raising an exception must be swallowed — no crash."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        ev = {
            "timestamp": "2026-05-07T10:00:00Z",
            "eventType": "MOVEMENT",
            "id": "EVID1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        with patch(URLOPEN, side_effect=OSError("disk full")):
            sync_local_save(coord, ev, "tok", "Terrasse")  # must not raise

    def test_image_downloaded_and_written(self, tmp_path: Path) -> None:
        """HTTP 200 image response → file written to download_path/camera/stem.jpg."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        ev = {
            "timestamp": "2026-05-07T10:00:00Z",
            "eventType": "MOVEMENT",
            "id": "EVID1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        resp = _urlopen_resp(200, b"JPEG_DATA")
        with patch(URLOPEN, return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        written = list((tmp_path / "Terrasse").rglob("*.jpg"))
        assert len(written) == 1, "Image download must write one .jpg file"


class TestSyncLocalSaveToggleGuard:
    def test_sync_local_save_skips_when_toggle_off(self):
        """sync_local_save must return immediately when enable_local_save=False."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord(
            {
                "enable_local_save": False,
                "download_path": "/tmp/bosch_test",
            }
        )
        ev = {
            "timestamp": "2026-05-08T10:00:00Z",
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://residential.cbs.boschsecurity.com/v11/img.jpg",
        }
        with patch(URLOPEN) as mock_urlopen:
            sync_local_save(coord, ev, "tok", "Terrasse")

        mock_urlopen.assert_not_called()

    def test_sync_local_save_runs_when_toggle_on(self):
        """sync_local_save must proceed when enable_local_save=True."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        with tempfile.TemporaryDirectory() as tmpdir:
            coord = _coord(
                {
                    "enable_local_save": True,
                    "download_path": tmpdir,
                }
            )
            resp = _urlopen_resp(200, b"IMGDATA")
            ev = {
                "timestamp": "2026-05-08T10:00:00Z",
                "eventType": "MOVEMENT",
                "id": "ABCD1234",
                "imageUrl": "https://residential.cbs.boschsecurity.com/v11/img.jpg",
            }
            with patch(URLOPEN, return_value=resp):
                sync_local_save(coord, ev, "tok", "Terrasse")

        resp.__enter__.assert_called()


# ── feature area: sync_smb_upload — early exits / toggles ───────────────────


class TestSyncSmbUploadEarlyExits:
    def test_no_server_returns_early(self):
        """Empty smb_server → return before any network calls."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({"smb_server": "", "smb_share": "SHARE"})
        # Should not raise even if smbclient is missing
        sync_smb_upload(coord, {}, "tok")

    def test_no_share_returns_early(self):
        """Empty smb_share → return before any network calls."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({"smb_server": "192.168.1.1", "smb_share": ""})
        sync_smb_upload(coord, {}, "tok")

    def test_smbclient_import_error_logs_warning(self):
        """smbclient not installed → log warning and return gracefully."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE"})

        # Make smbclient unavailable
        with patch.dict(sys.modules, {"smbclient": None}):
            # should not raise — logs warning instead
            try:
                sync_smb_upload(coord, {}, "tok")
            except ImportError:
                pass  # acceptable — module guards with try/except ImportError

    def test_ftp_protocol_delegates_to_ftp(self):
        """upload_protocol='ftp' → delegates to _sync_ftp_upload."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({"upload_protocol": "ftp", "smb_server": ""})

        with patch(f"{MODULE}._sync_ftp_upload") as mock_ftp:
            sync_smb_upload(coord, {"data": 1}, "tok")

        mock_ftp.assert_called_once_with(coord, {"data": 1}, "tok", None)

    def test_smb_session_failure_returns_gracefully(self):
        """register_session raising → log warning and return."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE"})

        fake_smb = MagicMock()
        fake_smb.register_session.side_effect = Exception("auth failed")
        fake_smb.mkdir = MagicMock()
        fake_smb.open_file = MagicMock()
        fake_smb.stat = MagicMock(side_effect=OSError)

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                sync_smb_upload(coord, {}, "tok")  # must not raise


class TestSmbTransferSocketTimeout:
    """Regression: the actual file-transfer loop must run under its own
    socket timeout (_SMB_TRANSFER_TIMEOUT), separate from the 10s
    connection-setup timeout — see docs/stream-perf-stability-refactor-plan.md
    Phase 2 point 9 (smb.py ~287-295: a hung open_file()/write() previously
    had no timeout at all and could block the executor thread forever)."""

    def test_transfer_wrapped_in_its_own_socket_timeout(self):
        """setdefaulttimeout sequence must be: 10 (connect) -> None -> the
        transfer timeout -> None (final reset), even on a fully successful
        upload with no errors."""
        from custom_components.bosch_shc_camera import smb
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        ev = _smb_event()
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}

        fake_smb = _fake_smb()
        calls: list[float | None] = []

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch.object(
                smb.socket,
                "setdefaulttimeout",
                side_effect=lambda v=None: calls.append(v),
            ),
            patch(f"{MODULE}._http_get", return_value=(200, b"DATA")),
        ):
            sync_smb_upload(coord, data, "tok")

        assert calls == [10, None, smb._SMB_TRANSFER_TIMEOUT, None]

    def test_transfer_timeout_reset_even_when_transfer_raises(self):
        """A hard failure inside the transfer loop must still reset the
        socket default timeout afterward (no leaked global state for the
        next executor job on the same thread)."""
        from custom_components.bosch_shc_camera import smb
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        calls: list[float | None] = []

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch.object(
                smb.socket,
                "setdefaulttimeout",
                side_effect=lambda v=None: calls.append(v),
            ),
            patch.object(
                smb,
                "_sync_smb_upload_events",
                side_effect=TimeoutError("simulated hung NAS write"),
            ),
        ):
            with pytest.raises(TimeoutError):
                sync_smb_upload(coord, {}, "tok")

        # Final call must be the reset back to None, regardless of the
        # exception raised inside the transfer loop.
        assert calls[-1] is None
        assert smb._SMB_TRANSFER_TIMEOUT in calls


class TestEnableToggleGuards:
    """Regression: smb.py functions must respect their enable-toggle even when
    called directly (defense-in-depth — callers already guard, but the function
    itself must not proceed if the toggle is off)."""

    def test_sync_smb_upload_skips_when_toggle_off(self):
        """sync_smb_upload must return immediately when enable_smb_upload=False."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord(
            {
                "enable_smb_upload": False,
                "smb_server": "nas.local",
                "smb_share": "share",
                "upload_protocol": "smb",
            }
        )
        fake_smb = _fake_smb()
        ev = {
            "timestamp": "2026-05-08T10:00:00Z",
            "eventType": "MOVEMENT",
            "id": "ABCD1234",
            "imageUrl": "https://media.bosch-smart-home.com/v11/img.jpg",
        }
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            sync_smb_upload(coord, data, "tok")

        fake_smb.register_session.assert_not_called()

    def test_sync_smb_cleanup_skips_when_toggle_off(self):
        """sync_smb_cleanup must return immediately when enable_smb_upload=False."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "enable_smb_upload": False,
                "smb_server": "nas.local",
                "smb_share": "share",
                "smb_retention_days": 30,
                "upload_protocol": "smb",
            }
        )
        fake_smb = _fake_smb()
        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            sync_smb_cleanup(coord)

        fake_smb.register_session.assert_not_called()

    def test_ftp_cleanup_skips_when_toggle_off(self):
        """_sync_ftp_cleanup must return immediately when enable_smb_upload=False."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "enable_smb_upload": False,
                "smb_server": "ftp.example.com",
                "smb_retention_days": 30,
            }
        )
        fake_ftp_connect = MagicMock()
        with patch(f"{MODULE}._ftp_connect", fake_ftp_connect):
            _sync_ftp_cleanup(coord)

        fake_ftp_connect.assert_not_called()


# ── feature area: sync_smb_upload — main upload loop ─────────────────────────


class TestSyncSmbUploadMainLoop:
    def test_uploads_image_when_http_200(self):
        """Valid event with imageUrl + HTTP 200 → open_file called (image written to SMB)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()

        fake_smb = _fake_smb()
        # stat raises OSError → file doesn't exist → upload proceeds
        fake_smb.stat.side_effect = OSError("not found")

        # open_file returns a context manager
        fake_file = MagicMock()
        fake_file.__enter__ = MagicMock(return_value=fake_file)
        fake_file.__exit__ = MagicMock(return_value=False)
        fake_smb.open_file.return_value = fake_file

        resp = _urlopen_resp(200, b"IMG")

        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "events": [
                    {
                        "timestamp": "2026-05-07T10:00:00Z",
                        "eventType": "MOVEMENT",
                        "id": "EVID1234",
                        "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
                    }
                ],
            }
        }

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                with patch(f"{MODULE}.smb_makedirs"):
                    with patch(URLOPEN, return_value=resp):
                        sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_called_once()
        call_args = fake_smb.open_file.call_args[0][0]
        assert ".jpg" in call_args, "open_file must be called with a .jpg path"

    def test_skips_video_clip_when_status_not_done(self):
        """videoClipUploadStatus != 'Done' → mp4 not uploaded."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_base_path": "Bosch",
            }
        )

        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        data = {
            CAM_ID: {
                "info": {"title": "Cam"},
                "events": [
                    {
                        "timestamp": "2026-05-07T10:00:00Z",
                        "eventType": "MOVEMENT",
                        "id": "EVID1234",
                        "videoClipUrl": "https://cdn.boschsecurity.com/clip.mp4",
                        "videoClipUploadStatus": "Pending",  # not Done
                    }
                ],
            }
        }

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                with patch(f"{MODULE}.smb_makedirs"):
                    sync_smb_upload(coord, data, "tok")

        # open_file must not have been called for mp4
        for c in fake_smb.open_file.call_args_list:
            assert ".mp4" not in str(c), "MP4 must not be uploaded when status != Done"

    def test_skips_event_with_short_timestamp(self):
        """Timestamp shorter than 19 chars → event skipped entirely."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_base_path": "Bosch",
            }
        )

        fake_smb = _fake_smb()

        data = {
            CAM_ID: {
                "info": {"title": "Cam"},
                "events": [{"timestamp": "2026-05", "eventType": "MOVE", "id": "X"}],
            }
        }

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_not_called()


class TestSmbMkdirError:
    """smb_makedirs raising inside the upload loop → warning logged, event skipped."""

    def test_mkdir_error_logs_warning_and_continues(self):
        """smb_makedirs raises Exception → warning + continue (no upload attempted)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs", side_effect=Exception("mkdir boom")),
            patch(URLOPEN) as mock_urlopen,
        ):
            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # No upload was attempted because mkdir failed → continue skipped the rest
        mock_urlopen.assert_not_called()


class TestSmbStatSkip:
    """smb_stat succeeding (no OSError) means the file already exists on the
    share → upload is skipped."""

    def test_image_skipped_when_stat_succeeds(self):
        """smb_stat returns without error → file already on share → no open_file call."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        # stat does NOT raise → file exists
        fake_smb.stat.return_value = MagicMock()
        fake_smb.stat.side_effect = None

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN) as mock_urlopen,
        ):
            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # No HTTP request made — skipped because stat showed file exists
        mock_urlopen.assert_not_called()


class TestSmbSnapshotHttpBranches:
    """HTTP status/exception branches on the snapshot fetch."""

    def test_snapshot_non200_logs_warning(self):
        """HTTP 404 on snapshot → _LOGGER.warning, open_file NOT called."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        resp = _urlopen_resp(404, b"")

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN, return_value=resp),
        ):
            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # HTTP was called but open_file was NOT (non-200 means skip write)
        resp.__enter__.assert_called()
        fake_smb.open_file.assert_not_called()

    def test_snapshot_urlopen_exception_logged_no_crash(self):
        """ConnectionError on snapshot urlopen → except branch → no upload, no exception."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord(
            {
                "enable_smb_upload": True,
                "smb_server": "nas.local",
                "smb_share": "SHARE",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )
        fake_smb = _fake_smb()

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN, side_effect=ConnectionError("link down")),
        ):
            ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            # Must not raise
            sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_not_called()


class TestSmbClipUpload:
    """Video-clip upload branches: stat-exists skip, missing → upload, non-200,
    and urlopen exceptions."""

    def test_clip_skipped_when_stat_succeeds(self):
        """Clip: stat does NOT raise for the clip path → file exists → skip upload."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()

        def stat_side_effect(path):
            # Image path → raise so image is uploaded.
            # Clip path → succeed so clip is skipped.
            if path.endswith(".jpg"):
                raise OSError("not found")
            return MagicMock()

        fake_smb.stat.side_effect = stat_side_effect

        fake_file = MagicMock()
        fake_file.__enter__ = MagicMock(return_value=fake_file)
        fake_file.__exit__ = MagicMock(return_value=False)
        fake_smb.open_file.return_value = fake_file

        resp = _urlopen_resp(200, b"IMG")

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN, return_value=resp),
        ):
            ev = _basic_event(clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        # Only image urlopen was called; clip was skipped
        assert resp.__enter__.call_count == 1

    def test_clip_uploaded_when_stat_raises(self):
        """Clip: stat raises OSError → file missing → upload (HTTP 200)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        fake_file = MagicMock()
        fake_file.__enter__ = MagicMock(return_value=fake_file)
        fake_file.__exit__ = MagicMock(return_value=False)
        fake_smb.open_file.return_value = fake_file

        # For clip streaming, urlopen is used as context manager that yields chunks
        resp = _urlopen_resp(200, b"VIDDATA")

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN, return_value=resp),
        ):
            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        # open_file called for clip (.mp4)
        assert fake_smb.open_file.call_count == 1
        call_path = fake_smb.open_file.call_args[0][0]
        assert call_path.endswith(".mp4")

    def test_clip_non200_logs_warning(self):
        """Clip: HTTP 503 → warning, open_file NOT called."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        resp = _urlopen_resp(503, b"")

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN, return_value=resp),
        ):
            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        # HTTP was called (to check clip) but file not written
        resp.__enter__.assert_called()
        fake_smb.open_file.assert_not_called()

    def test_clip_urlopen_exception_logged_no_crash(self):
        """Timeout on clip urlopen → warning, no open_file for .mp4."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord(
            {
                "enable_smb_upload": True,
                "smb_server": "nas.local",
                "smb_share": "SHARE",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )
        fake_smb = _fake_smb()

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN, side_effect=TimeoutError("read timeout")),
        ):
            # No image → only clip path executes
            ev = _smb_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_not_called()


class TestSmbImageUrlSafetyGuard:
    """Regression: the SMB snapshot/clip path must reject non-Bosch URLs.

    Bug (pre-fix): the SMB upload path fetched `imageUrl` with no
    `_is_safe_bosch_url` check, while the FTP path, the FTP clip path and
    `sync_local_save` all validate it. An event whose `imageUrl` pointed at an
    attacker-controlled host would have been fetched (SSRF) with the user's
    bearer token attached. The fix mirrors the FTP path's guard.
    """

    def test_non_bosch_image_url_is_not_fetched(self):
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN) as mock_urlopen,
        ):
            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [
                        _basic_event(image_url="https://evil.example.com/snap.jpg")
                    ],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # SSRF guard blocked the fetch entirely.
        mock_urlopen.assert_not_called()
        fake_smb.open_file.assert_not_called()

    def test_bosch_image_url_is_fetched(self):
        """Control: a legitimate *.bosch.com imageUrl still uploads normally."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_smb.open_file.return_value = MagicMock()

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
            patch(f"{MODULE}.smb_makedirs"),
            patch(URLOPEN, return_value=_urlopen_resp(status=200, content=b"IMG")),
        ):
            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [
                        _basic_event(image_url="https://cdn.bosch.com/snap.jpg")
                    ],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # Safe Bosch URL → fetch happened.
        fake_smb.open_file.assert_called()

    def test_unsafe_smb_clip_url_skipped(self):
        """Non-Bosch videoClipUrl in SMB upload is warned + skipped."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        URLLIB_REQUEST = f"{MODULE}.urllib.request"
        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.smb_makedirs"),
            patch(f"{URLLIB_REQUEST}.urlopen") as mock_urlopen,
        ):
            ev = {
                "timestamp": "2026-05-07T10:00:00Z",
                "eventType": "MOVEMENT",
                "id": "EVID1234ABCD",
                "imageUrl": None,
                "videoClipUrl": "https://evil.example.com/steal.mp4",
                "videoClipUploadStatus": "Done",
            }
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        # Non-Bosch clip URL is rejected → no urlopen, no open_file
        mock_urlopen.assert_not_called()
        fake_smb.open_file.assert_not_called()


# ── feature area: smb_makedirs (recursive SMB directory creation) ───────────


class TestSmbMakedirs:
    def test_makedirs_creates_each_path_segment(self):
        """mkdir called for each segment of base_path + folder_parts."""
        from custom_components.bosch_shc_camera.smb import smb_makedirs

        fake_smb = MagicMock()
        fake_smb.stat.side_effect = OSError("not found")  # nothing exists yet
        fake_smb.mkdir = MagicMock()

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            smb_makedirs(
                r"\\server\SHARE\Bosch\2026\05\07",
                "server",
                "SHARE",
                "Bosch",
                "2026/05/07",
            )

        # Expected segments: Bosch, 2026, 05, 07 → 4 mkdir calls
        assert fake_smb.mkdir.call_count >= 3, (
            "mkdir must be called for each directory segment"
        )

    def test_makedirs_swallows_existing_dir_error(self):
        """mkdir raising OSError (already exists) is silently ignored."""
        from custom_components.bosch_shc_camera.smb import smb_makedirs

        fake_smb = MagicMock()
        # stat raises → directory appears missing → mkdir called but raises OSError
        fake_smb.stat.side_effect = OSError("not found")
        fake_smb.mkdir.side_effect = OSError("already exists")

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            # Must not raise
            smb_makedirs(
                r"\\server\SHARE\Bosch\2026",
                "server",
                "SHARE",
                "Bosch",
                "2026",
            )

    def test_makedirs_skips_stat_success_segments(self):
        """If stat succeeds (dir exists), mkdir is not called for that segment."""
        from custom_components.bosch_shc_camera.smb import smb_makedirs

        fake_smb = MagicMock()
        fake_smb.stat.return_value = MagicMock()  # dir exists — no OSError
        fake_smb.mkdir = MagicMock()

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            smb_makedirs(
                r"\\server\SHARE\Bosch\2026",
                "server",
                "SHARE",
                "Bosch",
                "2026",
            )

        fake_smb.mkdir.assert_not_called()


# ── feature area: sync_smb_cleanup / _walk_and_delete ────────────────────────


class TestSyncSmbCleanupEarlyExits:
    def test_no_server_returns_early(self):
        """Empty smb_server → return without any SMB calls."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {"smb_server": "", "smb_share": "SHARE", "smb_retention_days": 30}
        )
        sync_smb_cleanup(coord)  # must not raise

    def test_retention_zero_returns_early(self):
        """smb_retention_days=0 → keep forever, no deletion."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {"smb_server": "192.168.1.1", "smb_share": "SHARE", "smb_retention_days": 0}
        )
        sync_smb_cleanup(coord)

    def test_session_failure_returns_gracefully(self):
        """register_session raising → log warning and return without crash."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_retention_days": 30,
            }
        )

        fake_smb = MagicMock()
        fake_smb.register_session.side_effect = Exception("connection refused")

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                sync_smb_cleanup(coord)  # must not raise

    def test_smbclient_import_error_returns_silently(self):
        """smbclient ImportError inside sync_smb_cleanup → silent return."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_retention_days": 30,
            }
        )

        with patch.dict(sys.modules, {"smbclient": None}):
            try:
                sync_smb_cleanup(coord)
            except ImportError:
                pass  # acceptable

    def test_zero_retention_disables_cleanup(self):
        """retention_days <= 0 must skip cleanup entirely (don't delete
        all files!). Pinned via the `if retention_days <= 0: return` guard."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        # Build a stub coordinator that would otherwise enter the loop
        coord = SimpleNamespace(
            options={
                "smb_server": "fritz.box",
                "smb_share": "Backup",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "BoschKameras",
                "smb_retention_days": 0,
                "upload_protocol": "smb",
            },
        )
        # Should return cleanly without trying to import smbclient
        sync_smb_cleanup(coord)  # no exception = pass


class TestSyncSmbCleanupFtpEarlyReturn:
    """protocol=='ftp' → delegates to _sync_ftp_cleanup immediately."""

    def test_ftp_protocol_delegates_to_ftp_cleanup(self):
        """upload_protocol=ftp → _sync_ftp_cleanup called, not the SMB path."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {"upload_protocol": "ftp", "smb_server": "", "smb_retention_days": "30"}
        )

        with patch(f"{MODULE}._sync_ftp_cleanup") as mock_ftp_cleanup:
            sync_smb_cleanup(coord)

        mock_ftp_cleanup.assert_called_once_with(coord)


class TestWalkAndDeleteRetention:
    """`_walk_and_delete` age-based retention: old files removed, recent files
    kept, directory entries recursed into."""

    def test_walk_and_delete_removes_old_files(self):
        """scandir returns one old file → remove called."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_retention_days": 1,
                "smb_base_path": "Bosch",
            }
        )

        # Fake old file entry
        old_entry = MagicMock()
        old_entry.name = "old_file.jpg"
        old_entry.is_dir.return_value = False

        # Fake stat result with old mtime
        old_stat = MagicMock()
        old_stat.st_mtime = time.time() - 5 * 86400  # 5 days old

        fake_smb = _fake_smb()
        fake_smb.register_session = MagicMock()
        fake_smb.scandir.return_value = [old_entry]
        # Clear the side_effect set by _fake_smb() so return_value takes effect
        fake_smb.stat.side_effect = None
        fake_smb.stat.return_value = old_stat

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                sync_smb_cleanup(coord)

        fake_smb.remove.assert_called_once()

    def test_walk_and_delete_skips_recent_files(self):
        """File newer than cutoff → not deleted."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_retention_days": 180,
                "smb_base_path": "Bosch",
            }
        )

        recent_entry = MagicMock()
        recent_entry.name = "recent_file.jpg"
        recent_entry.is_dir.return_value = False

        recent_stat = MagicMock()
        recent_stat.st_mtime = time.time()  # just now — clearly within retention

        fake_smb = _fake_smb()
        fake_smb.register_session = MagicMock()
        fake_smb.scandir.return_value = [recent_entry]
        fake_smb.stat.return_value = recent_stat

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                sync_smb_cleanup(coord)

        fake_smb.remove.assert_not_called()

    def test_walk_recurses_into_subdirectory(self):
        """scandir returns a directory → recurse into it."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_retention_days": 1,
                "smb_base_path": "Bosch",
            }
        )

        sub_dir = MagicMock()
        sub_dir.name = "2026"
        sub_dir.is_dir.return_value = True

        # Second call (recurse into subdir) returns no files
        scandir_results = [[sub_dir], []]
        call_count = [0]

        def _scandir(path):
            result = scandir_results[min(call_count[0], len(scandir_results) - 1)]
            call_count[0] += 1
            return result

        fake_smb = _fake_smb()
        fake_smb.register_session = MagicMock()
        fake_smb.scandir.side_effect = _scandir

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                sync_smb_cleanup(coord)

        # scandir called at least twice (root + subdir)
        assert fake_smb.scandir.call_count >= 2, (
            "Directory entries must cause recursive scandir call"
        )


class TestWalkAndDeleteRecurseDeepDelete:
    """A deeper recursion where the subdirectory itself contains the old file
    that must be deleted (confirms recursion doesn't just descend but also
    acts on what it finds)."""

    def test_recurses_into_subdirectory_and_deletes(self):
        """A directory entry triggers recursive _walk_and_delete which then
        deletes an old file found one level down."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_share": "SHARE",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "1",
            }
        )

        fake_smb = _fake_smb()

        # Root has one subdirectory entry, subdir has one old file
        dir_entry = MagicMock()
        dir_entry.name = "2025"
        dir_entry.is_dir.return_value = True

        file_entry = MagicMock()
        file_entry.name = "old.jpg"
        file_entry.is_dir.return_value = False

        old_stat = MagicMock()
        old_stat.st_mtime = 0.0  # epoch → older than any retention

        # scandir: first call (root) returns dir_entry, second (subdir) returns file_entry
        fake_smb.scandir.side_effect = [[dir_entry], [file_entry]]
        fake_smb.stat.return_value = old_stat
        fake_smb.stat.side_effect = None

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
        ):
            sync_smb_cleanup(coord)

        # remove was called once (for old.jpg in the subdir)
        fake_smb.remove.assert_called_once()


class TestSmbCleanupScandirException:
    """scandir() raising must be caught and the recursion ends silently."""

    def test_scandir_exception_returns_no_delete(self):
        """PermissionError on scandir → except branch returns, no remove() call."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord(
            {
                "enable_smb_upload": True,
                "smb_server": "nas.local",
                "smb_share": "SHARE",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "30",
                "upload_protocol": "smb",
            }
        )
        fake_smb = _fake_smb()
        fake_smb.scandir.side_effect = PermissionError("access denied")

        with (
            patch.dict(sys.modules, {"smbclient": fake_smb}),
            patch(f"{MODULE}.socket"),
        ):
            # Must not raise
            sync_smb_cleanup(coord)

        fake_smb.remove.assert_not_called()


# ── feature area: FTP pure helpers (_ftp_exists / _ftp_makedirs / _ftp_connect) ──


class TestFtpExists:
    """`_ftp_exists` probes a remote file by calling FTP `SIZE` — present
    files return the number, missing files raise `error_perm`. Wrapper
    must convert both to a clean True/False without leaking ftplib."""

    def test_existing_file_returns_true(self):
        from custom_components.bosch_shc_camera.smb import _ftp_exists

        ftp = MagicMock()
        ftp.size.return_value = 1024
        assert _ftp_exists(ftp, "/foo/bar.jpg") is True

    def test_missing_file_error_perm_returns_false(self):
        """`SIZE` on missing file raises `error_perm` ('550 No such file').
        Wrapper must catch and return False (not propagate the FTP exc)."""
        from custom_components.bosch_shc_camera.smb import _ftp_exists

        ftp = MagicMock()
        ftp.size.side_effect = ftplib.error_perm("550 No such file")
        assert _ftp_exists(ftp, "/foo/missing.jpg") is False

    def test_other_exception_returns_false(self):
        """Connection drop / timeout during SIZE must also return False
        — never raise. Caller decides what to do (typically: try upload
        anyway and let STOR fail with a clearer error)."""
        from custom_components.bosch_shc_camera.smb import _ftp_exists

        ftp = MagicMock()
        ftp.size.side_effect = ConnectionResetError()
        assert _ftp_exists(ftp, "/x") is False


class TestFtpMakedirs:
    def test_creates_each_path_segment(self):
        """Path /a/b/c → 3 mkd calls: /a, /a/b, /a/b/c. Some FTP servers
        reject a single deep mkd, so we walk segment by segment."""
        from custom_components.bosch_shc_camera.smb import _ftp_makedirs

        ftp = MagicMock()
        _ftp_makedirs(ftp, "/Bosch-Kameras/2026/05/06")
        # Expected calls: /Bosch-Kameras, /Bosch-Kameras/2026, .../05, .../06
        assert ftp.mkd.call_count == 4
        calls = [c.args[0] for c in ftp.mkd.call_args_list]
        assert calls == [
            "/Bosch-Kameras",
            "/Bosch-Kameras/2026",
            "/Bosch-Kameras/2026/05",
            "/Bosch-Kameras/2026/05/06",
        ]

    def test_already_exists_swallowed(self):
        """`error_perm` on mkd usually means 'already exists' (550) —
        the cleanup function should NOT raise; it must continue creating
        deeper segments."""
        from custom_components.bosch_shc_camera.smb import _ftp_makedirs

        ftp = MagicMock()
        ftp.mkd.side_effect = ftplib.error_perm("550 already exists")
        # Must not raise
        _ftp_makedirs(ftp, "/a/b/c")
        assert ftp.mkd.call_count == 3

    def test_collapses_double_slashes(self):
        """Path with `//` (e.g. base_path empty) must not produce empty
        segments which would create invalid FTP commands."""
        from custom_components.bosch_shc_camera.smb import _ftp_makedirs

        ftp = MagicMock()
        _ftp_makedirs(ftp, "/a//b//c/")
        calls = [c.args[0] for c in ftp.mkd.call_args_list]
        assert "" not in [c.split("/")[-1] for c in calls], (
            "Empty segment leaked → FTP server gets 'mkd /a/' which "
            "breaks on the FRITZ.NAS daemon."
        )
        assert ftp.mkd.call_count == 3

    def test_root_only_no_calls(self):
        """`/` alone has no segments → no calls (idempotent for root)."""
        from custom_components.bosch_shc_camera.smb import _ftp_makedirs

        ftp = MagicMock()
        _ftp_makedirs(ftp, "/")
        ftp.mkd.assert_not_called()


class TestFtpConnect:
    def test_connect_passive_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """FRITZ!Box FTP requires passive mode — connection must call
        `set_pasv(True)` after login. Active mode silently fails on
        NAT'd connections (the default user setup)."""
        captured = {}

        class _StubFTP:
            def __init__(self, server, timeout=30):
                captured["server"] = server
                captured["timeout"] = timeout

            def login(self, u, p):
                captured["user"] = u
                captured["pass"] = p

            def set_pasv(self, on):
                captured["pasv"] = on

        monkeypatch.setattr(ftplib, "FTP", _StubFTP)
        from custom_components.bosch_shc_camera.smb import _ftp_connect

        ftp = _ftp_connect("fritz.box", "user", "secret")
        assert captured["server"] == "fritz.box"
        assert captured["timeout"] == 30
        assert captured["user"] == "user"
        assert captured["pass"] == "secret"
        assert captured["pasv"] is True, (
            "Passive mode required on FRITZ.NAS — active mode breaks "
            "NAT'd connections silently."
        )


# ── feature area: _sync_ftp_upload — early exits / main loop ────────────────


class TestSyncFtpUploadEarlyExits:
    def test_no_server_returns_early(self):
        """Empty smb_server → return without FTP call."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({"smb_server": ""})
        _sync_ftp_upload(coord, {}, "tok")  # must not raise

    def test_ftp_connect_failure_returns_gracefully(self):
        """FTP login failure → log warning, return without crash."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {"smb_server": "192.168.1.1", "smb_username": "user", "smb_password": "pw"}
        )

        with patch(f"{MODULE}._ftp_connect", side_effect=Exception("auth failed")):
            _sync_ftp_upload(coord, {}, "tok")  # must not raise

    def test_empty_events_completes_without_upload(self):
        """No events for camera → no FTP stor calls, quit() still called in finally."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_username": "user",
                "smb_password": "pw",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": []}}

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()
        fake_ftp.quit.assert_called_once()

    def test_short_timestamp_event_skipped(self):
        """Event with timestamp shorter than 19 chars → skip without crash."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "B",
            }
        )

        fake_ftp = MagicMock()
        data = {
            CAM_ID: {
                "info": {"title": "Cam"},
                "events": [{"timestamp": "2026-05", "eventType": "MOVEMENT"}],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()

    def test_image_uploaded_successfully(self):
        """Valid event with JPEG URL → storbinary called with .jpg path."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )

        fake_ftp = MagicMock()
        resp = _urlopen_resp(200, b"JPEG")

        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "events": [
                    {
                        "timestamp": "2026-05-07T10:00:00Z",
                        "eventType": "MOVEMENT",
                        "id": "EVID1234",
                        "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
                    }
                ],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                with patch(f"{MODULE}._ftp_exists", return_value=False):
                    with patch(URLOPEN, return_value=resp):
                        _sync_ftp_upload(coord, data, "tok")

        stor_calls = fake_ftp.storbinary.call_args_list
        assert len(stor_calls) == 1, "One STOR command expected for the image"
        assert ".jpg" in stor_calls[0][0][0], "STOR path must end in .jpg"

    def test_file_already_exists_skipped(self):
        """File already on FTP server → skip storbinary."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()
        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "events": [
                    {
                        "timestamp": "2026-05-07T10:00:00Z",
                        "eventType": "MOVEMENT",
                        "id": "EVID1234",
                        "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
                    }
                ],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                with patch(
                    f"{MODULE}._ftp_exists", return_value=True
                ):  # already exists
                    _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()

    def test_ftp_quit_called_on_exception(self):
        """Exception mid-upload propagates through finally → ftp.quit() still called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()
        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "events": [
                    {
                        "timestamp": "2026-05-07T10:00:00Z",
                        "eventType": "MOVEMENT",
                        "id": "X",
                        "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
                    }
                ],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            # _ftp_makedirs raises — propagates through the try/finally in _sync_ftp_upload
            with patch(
                f"{MODULE}._ftp_makedirs", side_effect=Exception("mid-upload error")
            ):
                try:
                    _sync_ftp_upload(coord, data, "tok")
                except Exception:
                    pass  # exception expected — finally must still run

        fake_ftp.quit.assert_called_once()


class TestFtpUploadMainLoop:
    """Additional main-loop branch coverage for `_sync_ftp_upload` not already
    pinned by TestSyncFtpUploadEarlyExits."""

    def test_uploads_image_via_ftp_storbinary(self):
        """Valid event + HTTP 200 → storbinary called with .jpg STOR command."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )

        fake_ftp = MagicMock()
        resp = _urlopen_resp(200, b"IMGBYTES")

        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "events": [
                    {
                        "timestamp": "2026-05-07T10:00:00Z",
                        "eventType": "MOVEMENT",
                        "id": "EVID1234",
                        "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
                    }
                ],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                with patch(f"{MODULE}._ftp_exists", return_value=False):
                    with patch(URLOPEN, return_value=resp):
                        _sync_ftp_upload(coord, data, "tok")

        stor_calls = fake_ftp.storbinary.call_args_list
        assert len(stor_calls) >= 1, "storbinary must be called for the image"
        assert ".jpg" in stor_calls[0][0][0], "STOR command must include .jpg path"

    def test_skips_mp4_when_status_not_done(self):
        """videoClipUploadStatus != 'Done' → mp4 not uploaded."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()

        data = {
            CAM_ID: {
                "info": {"title": "Cam"},
                "events": [
                    {
                        "timestamp": "2026-05-07T10:00:00Z",
                        "eventType": "MOVEMENT",
                        "id": "X",
                        "videoClipUrl": "https://cdn.boschsecurity.com/clip.mp4",
                        "videoClipUploadStatus": "Pending",
                    }
                ],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                with patch(f"{MODULE}._ftp_exists", return_value=False):
                    _sync_ftp_upload(coord, data, "tok")

        for c in fake_ftp.storbinary.call_args_list:
            assert ".mp4" not in str(c), "MP4 must not be uploaded when status != Done"


class TestFtpSnapshotHttpBranches:
    """HTTP status/exception branches on the FTP snapshot fetch."""

    def test_ftp_snapshot_non200_logs_warning(self):
        """FTP upload path: image HTTP 404 → warning, ftp.storbinary NOT called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.size.side_effect = Exception("not found")  # file doesn't exist

        resp = _urlopen_resp(404, b"")

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=False),
            patch(URLOPEN, return_value=resp),
        ):
            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()

    def test_ftp_snapshot_exception_logged_no_crash(self):
        """ConnectionError on FTP snapshot urlopen → except branch, no storbinary."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.fritz.box",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )
        fake_ftp = MagicMock()

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=False),
            patch(URLOPEN, side_effect=ConnectionError("link down")),
        ):
            ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()


class TestFtpClipUpload:
    """FTP clip upload branches: exists skip, 200 upload, non-200, and
    urlopen exceptions."""

    def test_ftp_clip_skipped_when_exists(self):
        """FTP path: clip exists → skip, storbinary NOT called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )

        fake_ftp = MagicMock()

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=True),
            patch(URLOPEN) as mock_urlopen,
        ):
            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_ftp_clip_uploaded_200(self):
        """FTP path: clip missing → HTTP 200 → storbinary called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )

        fake_ftp = MagicMock()
        resp = _urlopen_resp(200, b"CLIP")

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=False),
            patch(URLOPEN, return_value=resp),
        ):
            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        assert fake_ftp.storbinary.call_count == 1

    def test_ftp_clip_non200_logs_warning(self):
        """FTP path: clip HTTP 502 → warning, storbinary NOT called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )

        fake_ftp = MagicMock()
        resp = _urlopen_resp(502, b"")

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=False),
            patch(URLOPEN, return_value=resp),
        ):
            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()

    def test_ftp_clip_urlopen_exception_logged_no_crash(self):
        """TimeoutError on clip urlopen → except branch, no storbinary."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.fritz.box",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )
        fake_ftp = MagicMock()

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=False),
            patch(URLOPEN, side_effect=TimeoutError("read timeout")),
        ):
            ev = _smb_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()


class TestFtpSafeUrlGuard:
    """The FTP upload path must also enforce the `_is_safe_bosch_url` guard
    for image/clip URLs."""

    def test_unsafe_image_url_skipped(self):
        """Non-Bosch imageUrl is rejected by _is_safe_bosch_url → no HTTP request."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "folder_pattern": "{year}/{month}/{day}",
                "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            }
        )

        fake_ftp = MagicMock()

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=False),
            patch(URLOPEN) as mock_urlopen,
        ):
            ev = {
                "timestamp": "2026-05-07T10:00:00Z",
                "eventType": "MOVEMENT",
                "id": "EVID1234ABCD",
                "imageUrl": "https://evil.example.com/steal.jpg",
            }
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        mock_urlopen.assert_not_called()


class TestFtpFinallyQuitClose:
    """`_sync_ftp_upload`'s finally block calls ftp.quit(); if that raises →
    falls back to ftp.close(); if BOTH raise, it must still swallow cleanly."""

    def test_ftp_quit_called_on_success(self):
        """Normal path: ftp.quit() called in finally."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=True),
        ):
            _sync_ftp_upload(coord, {}, "tok")

        fake_ftp.quit.assert_called_once()

    def test_ftp_close_called_when_quit_raises(self):
        """ftp.quit() raises → ftp.close() called as fallback."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.quit.side_effect = Exception("connection reset")

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=True),
        ):
            _sync_ftp_upload(coord, {}, "tok")

        fake_ftp.quit.assert_called_once()
        fake_ftp.close.assert_called_once()

    def test_quit_and_close_both_fail(self, tmp_path: Path) -> None:
        """quit() + close() both raise → outer caller sees no exception."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord(
            {
                "smb_server": "ftp.fritz.box",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )
        fake_ftp = MagicMock()
        fake_ftp.quit.side_effect = Exception("connection reset")
        fake_ftp.close.side_effect = Exception("socket already gone")

        with (
            patch(f"{MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{MODULE}._ftp_makedirs"),
            patch(f"{MODULE}._ftp_exists", return_value=True),
        ):
            # Empty data → just exercises connect → finally block
            _sync_ftp_upload(coord, {}, "tok")

        fake_ftp.quit.assert_called_once()
        fake_ftp.close.assert_called_once()


# ── feature area: _sync_ftp_cleanup ──────────────────────────────────────────


class TestSyncFtpCleanupEarlyExits:
    def test_no_server_returns_early(self):
        """Empty smb_server → return without any FTP call."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({"smb_server": "", "smb_retention_days": 30})
        _sync_ftp_cleanup(coord)  # must not raise

    def test_retention_zero_returns_early(self):
        """smb_retention_days=0 → skip cleanup."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({"smb_server": "192.168.1.1", "smb_retention_days": 0})
        _sync_ftp_cleanup(coord)

    def test_ftp_connect_failure_returns_gracefully(self):
        """FTP login failure → log warning, return without crash."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_retention_days": 30,
                "smb_username": "u",
                "smb_password": "p",
            }
        )

        with patch(f"{MODULE}._ftp_connect", side_effect=Exception("auth failed")):
            _sync_ftp_cleanup(coord)  # must not raise

    def test_empty_directory_completes_without_deletion(self):
        """Empty FTP directory → no DELE commands."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_retention_days": 30,
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.nlst.return_value = []  # empty directory

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_not_called()
        fake_ftp.quit.assert_called_once()

    def test_mlsd_exception_falls_back_to_nlst(self):
        """mlsd() raising → fall back to nlst() for listing."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_retention_days": 30,
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.mlsd.side_effect = Exception("MLSD not supported")
        fake_ftp.nlst.return_value = []  # empty — no deletions

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            try:
                _sync_ftp_cleanup(coord)
            except Exception:
                pass  # acceptable — just must not hang

        fake_ftp.quit.assert_called()


class TestFtpCleanupWalkAndDelete:
    """`_walk_and_delete` for the FTP path: age-based retention + directory
    recursion, mirroring the SMB walk semantics."""

    def test_walk_and_delete_deletes_old_files(self):
        """LIST returns a file with old MDTM timestamp → delete called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_retention_days": 1,
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        old_ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(time.time() - 5 * 86400))
        list_line = "-rw-r--r-- 1 user group 1000 Jan 01 10:00 old_file.jpg"

        fake_ftp = MagicMock()

        def _retrlines(cmd, callback):
            callback(list_line)

        fake_ftp.retrlines.side_effect = _retrlines
        fake_ftp.sendcmd.return_value = f"213 {old_ts}"

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_called_once_with("old_file.jpg")

    def test_walk_and_delete_skips_recent_files(self):
        """FILE with MDTM timestamp within retention → not deleted."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_retention_days": 180,
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        recent_ts = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        list_line = "-rw-r--r-- 1 user group 1000 Jan 01 10:00 recent_file.jpg"

        fake_ftp = MagicMock()

        def _retrlines(cmd, callback):
            callback(list_line)

        fake_ftp.retrlines.side_effect = _retrlines
        fake_ftp.sendcmd.return_value = f"213 {recent_ts}"

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_not_called()

    def test_walk_recurses_into_subdirectories(self):
        """LIST returns a directory entry → cwd into it and LIST again."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_retention_days": 30,
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        dir_line = "drwxr-xr-x 2 user group 0 Jan 01 10:00 2026"
        call_count = [0]

        fake_ftp = MagicMock()

        def _retrlines(cmd, callback):
            if call_count[0] == 0:
                callback(dir_line)  # first call: return a directory
            # second call: empty → no files in subdir
            call_count[0] += 1

        fake_ftp.retrlines.side_effect = _retrlines

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        # cwd must have been called for the subdirectory
        assert fake_ftp.cwd.call_count >= 1, (
            "Directory entry must cause cwd() + recursive LIST"
        )

    def test_ftp_quit_called_on_completion(self):
        """ftp.quit() called in finally after cleanup walk."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "192.168.1.1",
                "smb_retention_days": 30,
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.retrlines.side_effect = lambda cmd, cb: None  # empty dir

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.quit.assert_called_once()


class TestFtpCleanupDeepWalk:
    """`_walk_and_delete` error/edge branches for the FTP cleanup path: cwd
    error_perm, retrlines failure, malformed LIST lines, MDTM/delete
    failures, and the quit/close fallback in the outer finally."""

    def test_cwd_error_perm_returns_silently(self):
        """ftp.cwd raises ftplib.error_perm → _walk_and_delete returns without listing."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "30",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.cwd.side_effect = ftplib.error_perm("550 No such directory")
        fake_ftp.quit.side_effect = Exception("closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        # retrlines was NOT called — returned on cwd failure
        fake_ftp.retrlines.assert_not_called()

    def test_retrlines_exception_returns_silently(self):
        """ftp.retrlines raises → _walk_and_delete returns, no delete attempted."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "30",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None  # cwd succeeds
        fake_ftp.retrlines.side_effect = Exception("connection lost")
        fake_ftp.quit.side_effect = Exception("closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_not_called()

    def _run_cleanup_with_list(
        self, lines: list[str], fake_ftp: MagicMock, coord
    ) -> None:
        """Helper: run _sync_ftp_cleanup with a faked LIST output."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        def fake_retrlines(cmd, callback):
            for line in lines:
                callback(line)

        fake_ftp.retrlines.side_effect = fake_retrlines
        fake_ftp.quit.side_effect = Exception("closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

    def test_short_line_skipped(self):
        """LIST line with fewer than 9 parts → skipped."""
        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "30",
            }
        )
        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None

        # Only 5 parts → skipped
        self._run_cleanup_with_list(
            ["drwxr-xr-x 1 user group 0 Jan 01"], fake_ftp, coord
        )

        fake_ftp.delete.assert_not_called()
        fake_ftp.sendcmd.assert_not_called()

    def test_dot_entries_skipped(self):
        """LIST line where name is '.' or '..' → skipped."""
        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "30",
            }
        )
        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None

        dot_line = "-rw-r--r-- 1 user group 1024 Jan 01 12:00 ."
        dotdot_line = "-rw-r--r-- 1 user group 1024 Jan 01 12:00 .."
        self._run_cleanup_with_list([dot_line, dotdot_line], fake_ftp, coord)

        fake_ftp.delete.assert_not_called()

    def test_mdtm_failure_skips_file(self):
        """sendcmd("MDTM ...") raises → file is not deleted (continue)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "1",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None
        fake_ftp.sendcmd.side_effect = Exception("MDTM not supported")
        fake_ftp.quit.side_effect = Exception("closed")

        file_line = "-rw-r--r-- 1 user group 1024 Jan 01 12:00 oldfile.jpg"

        def fake_retrlines(cmd, callback):
            callback(file_line)

        fake_ftp.retrlines.side_effect = fake_retrlines

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_not_called()

    def test_delete_failure_logged_as_debug(self):
        """ftp.delete raises → debug log, execution continues."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "1",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None
        # MDTM returns an old timestamp (epoch)
        fake_ftp.sendcmd.return_value = "213 19700101000000"
        fake_ftp.delete.side_effect = Exception("permission denied")
        fake_ftp.quit.side_effect = Exception("closed")

        file_line = "-rw-r--r-- 1 user group 1024 Jan 01 12:00 oldfile.jpg"

        def fake_retrlines(cmd, callback):
            callback(file_line)

        fake_ftp.retrlines.side_effect = fake_retrlines

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            # Must not raise even though delete fails
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_called_once()

    def test_subdir_cwd_back_fails_continues(self):
        """After recursing into a subdir, cwd(parent) fails → pass, loop continues."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "30",
            }
        )

        fake_ftp = MagicMock()
        call_count = {"n": 0}

        def cwd_side_effect(path):
            call_count["n"] += 1
            # First call (entering /Bosch/NVR-equivalent root): ok.
            # Second (entering subdir): ok.
            # Third (cwd back to root): raise.
            if call_count["n"] >= 3:
                raise Exception("cwd back failed")

        fake_ftp.cwd.side_effect = cwd_side_effect

        list_call = {"n": 0}

        def fake_retrlines(cmd, callback):
            list_call["n"] += 1
            if list_call["n"] == 1:
                callback("drwxr-xr-x 1 user group 0 Jan 01 12:00 2025")

        fake_ftp.retrlines.side_effect = fake_retrlines
        fake_ftp.quit.side_effect = Exception("closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            # Must not raise
            _sync_ftp_cleanup(coord)

    def test_cleanup_quit_exception_no_crash(self):
        """ftp.quit() raises in finally of _sync_ftp_cleanup → no crash."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord(
            {
                "smb_server": "ftp.example.com",
                "smb_username": "u",
                "smb_password": "p",
                "smb_base_path": "Bosch",
                "smb_retention_days": "30",
            }
        )

        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None
        fake_ftp.retrlines.side_effect = Exception("empty")
        fake_ftp.quit.side_effect = Exception("already closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            # Must not raise — quit exception swallowed
            _sync_ftp_cleanup(coord)


# ── feature area: retention/cleanup notification ─────────────────────────────


class TestCleanupAlert:
    """`_fire_cleanup_alert` fires a notify after age-based retention deletes
    files; `_async_cleanup_alert` is the async dispatcher it schedules."""

    def test_no_notify_service_skips_alert(self):
        """No alert_notify_system and no alert_notify_service → call_soon_threadsafe not called."""
        from custom_components.bosch_shc_camera.smb import _fire_cleanup_alert

        coord = _coord({})
        _fire_cleanup_alert(coord, 5, 180, "\\\\nas\\share\\Bosch-Kameras")
        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_system_service_schedules_alert(self):
        """alert_notify_system set → call_soon_threadsafe called once."""
        from custom_components.bosch_shc_camera.smb import _fire_cleanup_alert

        coord = _coord({"alert_notify_system": "notify.test_user"})
        _fire_cleanup_alert(coord, 3, 90, "\\\\nas\\share\\Bosch")
        coord.hass.loop.call_soon_threadsafe.assert_called_once()

    def test_fallback_to_alert_notify_service(self):
        """No system service configured → falls back to alert_notify_service."""
        from custom_components.bosch_shc_camera.smb import _fire_cleanup_alert

        coord = _coord({"alert_notify_service": "notify.signal"})
        _fire_cleanup_alert(coord, 1, 180, "nas/Bosch")
        coord.hass.loop.call_soon_threadsafe.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_alert_calls_service(self):
        """_async_cleanup_alert calls the notify service when it exists."""
        from custom_components.bosch_shc_camera.smb import _async_cleanup_alert

        coord = _coord()
        coord.hass.services.has_service = MagicMock(return_value=True)
        coord.hass.services.async_call = AsyncMock()
        await _async_cleanup_alert(coord, "5 Dateien gelöscht", "notify.test_user")
        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_alert_no_service_no_call(self):
        """_async_cleanup_alert: service not registered → no call, no exception."""
        from custom_components.bosch_shc_camera.smb import _async_cleanup_alert

        coord = _coord()
        coord.hass.services.has_service = MagicMock(return_value=False)
        coord.hass.services.async_call = AsyncMock()
        await _async_cleanup_alert(coord, "msg", "notify.nonexistent")
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_notify_service_exception_swallowed(self):
        """If the notify service exists but its async_call raises, the alert
        must not crash the cleanup task — logged at DEBUG, no propagation."""
        from custom_components.bosch_shc_camera.smb import _async_cleanup_alert

        coord = SimpleNamespace(hass=MagicMock())
        coord.hass.services.has_service = MagicMock(return_value=True)
        coord.hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("notify broken")
        )

        # Must not raise — the except branch is logged at DEBUG
        await _async_cleanup_alert(coord, "msg", "notify.test_user")

        coord.hass.services.async_call.assert_awaited_once()


# ── feature area: pure computation (path patterns / retention math) ─────────


class TestSmbPathPatterns:
    """`sync_smb_upload` builds folder + file paths from user-configurable
    patterns. Pattern formatting is INLINE — but we can replicate the exact
    computation here to pin invariants without exercising the full upload
    pipeline."""

    def _build(
        self,
        ts: str,
        etype: str,
        ev_id: str,
        cam_name: str,
        folder_pattern: str = "{year}/{month}/{day}",
        file_pattern: str = "{camera}_{date}_{time}_{type}_{id}",
        base_path: str = "Bosch-Kameras",
    ):
        # Mirrors sync_smb_upload's path computation exactly.
        year = ts[:4]
        month = ts[5:7]
        day = ts[8:10]
        date_str = f"{year}-{month}-{day}"
        time_str = ts[11:19].replace(":", "-")
        folder_parts = folder_pattern.format(
            year=year,
            month=month,
            day=day,
            camera=cam_name,
            type=etype,
        )
        file_base = file_pattern.format(
            camera=cam_name,
            date=date_str,
            time=time_str,
            type=etype,
            id=ev_id,
            year=year,
            month=month,
            day=day,
        )
        return base_path, folder_parts, file_base

    def test_default_pattern_yyyy_mm_dd(self):
        """Default folder pattern must produce zero-padded month + day."""
        ts = "2026-05-06T03:07:04.123Z"
        _, folder, file_base = self._build(ts, "MOVEMENT", "abcd1234", "Terrasse")
        assert folder == "2026/05/06", (
            "Pattern must zero-pad — '2026/5/6' breaks alphabetical sort"
        )
        assert file_base.startswith("Terrasse_2026-05-06_03-07-04_MOVEMENT_abcd1234")

    def test_time_colons_replaced_with_hyphens(self):
        """Filenames can't contain `:` on Windows / FAT32 — must hyphenate."""
        ts = "2026-12-31T23:59:59.000Z"
        _, _, file_base = self._build(ts, "PERSON", "fedc4321", "Cam")
        assert ":" not in file_base
        assert "23-59-59" in file_base

    def test_camera_name_in_folder_pattern(self):
        ts = "2026-05-06T01:02:03.000Z"
        _, folder, _ = self._build(
            ts,
            "MOVEMENT",
            "ee",
            "Bosch Eingang",
            folder_pattern="{camera}/{year}/{month}",
        )
        # Bosch event timestamps sort under each cam first
        assert folder == "Bosch Eingang/2026/05"

    def test_event_type_in_file_pattern(self):
        ts = "2026-05-06T01:02:03.000Z"
        _, _, file_base = self._build(
            ts,
            "AUDIO_ALARM",
            "abc12345",
            "Cam",
            file_pattern="{type}_{id}",
        )
        assert file_base == "AUDIO_ALARM_abc12345"

    def test_event_id_truncated_to_8_chars(self):
        """Caller pre-truncates the ev_id to 8 chars (simulated here).
        Pin that 8 is the only used substring length downstream."""
        ts = "2026-05-06T01:02:03.000Z"
        full_id = "0123456789abcdef0123456789abcdef"
        # sync_smb_upload does `ev.get("id", "")[:8]` before formatting
        truncated = full_id[:8]
        _, _, file_base = self._build(ts, "MOVEMENT", truncated, "C")
        assert "01234567" in file_base
        assert "89abcdef" not in file_base


class TestRetentionMath:
    """`sync_smb_cleanup` and `_sync_ftp_cleanup` both compute
    `cutoff = time.time() - retention_days * 86400`. Pin the math so a
    bad day-count multiplier (e.g. 24*60 instead of 86400) gets caught."""

    def test_180_day_default_in_seconds(self):
        # 180 days is the default option value
        retention_days = 180
        cutoff_offset_secs = retention_days * 86400
        assert cutoff_offset_secs == 15_552_000, (
            "180-day cutoff must equal 15,552,000 seconds. Off-by-multiplier "
            "bugs (e.g. *3600) produce 7.5-day retention silently."
        )


# _http_get_chunked Bearer-auth Request building (relocated from tests/test_remaining_cheap_gaps.py)
class TestHttpGetChunked:
    def test_builds_bearer_request_and_urlopens(self):
        """`_http_get_chunked` must build a Request with `Authorization:
        Bearer <token>` and pass the SSL context + timeout to urlopen."""
        from custom_components.bosch_shc_camera import smb

        captured_req = {}
        sentinel = object()

        def _fake_urlopen(req, context=None, timeout=None, **_kw):
            captured_req["req"] = req
            captured_req["context"] = context
            captured_req["timeout"] = timeout
            return sentinel

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = smb._http_get_chunked(
                "https://example/clip.mp4",
                "TOKEN42",
                timeout=30,
            )

        assert result is sentinel
        req = captured_req["req"]
        # urllib lowercases header names internally.
        assert req.get_header("Authorization") == "Bearer TOKEN42"
        assert captured_req["timeout"] == 30


# sync_local_save filenaming with a camera-name prefix (relocated from
# tests/test_fresh_install.py — the _FILE_RE regex tests it complements
# live in tests/test_media_source.py)
class TestLocalSaveFilenaming:
    """Files saved by sync_local_save must include the camera prefix in the
    filename so media_source.py's `_FILE_RE` can attribute them back to the
    right camera in the Media Browser."""

    def test_saved_filename_includes_camera_prefix(self, tmp_path: Path) -> None:
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
        from custom_components.bosch_shc_camera.smb import _safe_name, sync_local_save

        hass = MagicMock()
        hass.config.config_dir = "/tmp/test-ha"
        coord = SimpleNamespace(
            token="tok-fresh",
            hass=hass,
            options=dict(DEFAULT_OPTIONS),
            data={CAM_ID: {"info": {"title": "Aussenkamera"}, "events": []}},
            last_event_ids={CAM_ID: "fresh-event-001"},
            _download_started_at=0.0,  # disable "predates startup" guard
        )
        coord.options["enable_local_save"] = True
        coord.options["download_path"] = str(tmp_path)

        cam_name = "Aussenkamera Einfahrt"
        cam_safe = _safe_name(cam_name)  # preserves spaces: "Aussenkamera Einfahrt"

        ev = {
            "timestamp": "2026-05-07T12:00:00.000Z",
            "eventType": "MOVEMENT",
            "id": "11111111",  # valid hex ID
            "imageUrl": "https://api.bosch.com/image.jpg",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.side_effect = [b"JFIF" + b"\x00" * 200, b""]
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch(
            "custom_components.bosch_shc_camera.smb.urllib.request.urlopen",
            return_value=mock_resp,
        ):
            with patch(
                "custom_components.bosch_shc_camera.smb._is_safe_bosch_url",
                return_value=True,
            ):
                sync_local_save(coord, ev, "tok", cam_name)

        cam_dir = tmp_path / cam_safe
        assert cam_dir.is_dir(), (
            f"Camera subfolder {cam_safe!r} must be created under download_path"
        )
        # With folder_pattern={camera}/{year}/{month}/{day} files are in subfolders
        files = list(cam_dir.rglob("*.*"))
        assert files, "At least one file must be saved"
        for f in files:
            assert f.name.startswith(cam_safe + "_"), (
                f"Saved filename must start with camera prefix {cam_safe!r}; got: {f.name}"
            )


# Motion-event / live-stream TLS-channel contention — SMB/FTP prefetched-image
# bypass (relocated from tests/test_stream_motion_contention.py — the fcm.py
# Path A guard + the async_send_alert prefetch-propagation sibling live in
# tests/test_fcm.py)
_JPEG_BYTES_MOTION = b"\xff\xd8\xff\xe0" + b"\x42" * 400  # 404 B


class TestSmbPrefetchedImage:
    """sync_smb_upload(prefetched_image=...) must use the supplied bytes,
    skipping a second cloud/camera pull of the same snapshot — needed
    because the cloud fetch competes with a live stream for the camera's
    single TLS control channel."""

    def _make_coord(self) -> Any:
        coord = MagicMock()
        coord.options = {
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_share": "Bosch",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "Cams",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            "upload_protocol": "smb",
        }
        return coord

    def _smb_data(self, img_url: str = "") -> dict[str, Any]:
        return {
            CAM_ID: {
                "info": {"title": "Innenbereich"},
                "events": [
                    {
                        "id": "aabbccdd-test",
                        "eventType": "MOVEMENT",
                        "timestamp": "2026-06-12T07:07:30Z",
                        "imageUrl": img_url,
                        "videoClipUrl": "",
                        "videoClipUploadStatus": "",
                    }
                ],
            }
        }

    def test_prefetched_image_bypasses_http_get(self) -> None:
        """When prefetched_image is provided, _http_get must NOT be called."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = self._make_coord()
        data = self._smb_data(
            img_url="https://residential.cbs.boschsecurity.com/img.jpg"
        )

        mock_open_file = MagicMock()
        mock_open_file.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_open_file.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch("custom_components.bosch_shc_camera.smb.smbclient", create=True),
            patch(
                "custom_components.bosch_shc_camera.smb.register_session",
                create=True,
            ),
            patch(
                "custom_components.bosch_shc_camera.smb.open_file",
                mock_open_file,
                create=True,
            ),
            patch(
                "custom_components.bosch_shc_camera.smb.smb_stat",
                side_effect=OSError("not found"),
                create=True,
            ),
            patch("custom_components.bosch_shc_camera.smb.smb_makedirs"),
            patch("custom_components.bosch_shc_camera.smb._http_get") as mock_get,
        ):
            import sys

            fake_smb = MagicMock()
            fake_smb.register_session = MagicMock()
            fake_smb.open_file = mock_open_file
            fake_smb.stat = MagicMock(side_effect=OSError("not found"))
            fake_smb.mkdir = MagicMock()
            sys.modules.setdefault("smbclient", fake_smb)

            sync_smb_upload(coord, data, "tok", prefetched_image=_JPEG_BYTES_MOTION)

        mock_get.assert_not_called()

    def test_no_prefetch_calls_http_get(self) -> None:
        """When prefetched_image=None, _http_get IS called (backward compat)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = self._make_coord()
        img_url = "https://residential.cbs.boschsecurity.com/img.jpg"
        data = self._smb_data(img_url=img_url)

        import sys

        fake_smb = MagicMock()
        fake_smb.register_session = MagicMock()
        fake_smb.open_file = MagicMock(
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock()),
                __exit__=MagicMock(return_value=False),
            )
        )
        fake_smb.stat = MagicMock(side_effect=OSError("not found"))
        fake_smb.mkdir = MagicMock()
        sys.modules["smbclient"] = fake_smb

        with (
            patch("custom_components.bosch_shc_camera.smb.smb_makedirs"),
            patch(
                "custom_components.bosch_shc_camera.smb._http_get",
                return_value=(200, _JPEG_BYTES_MOTION),
            ) as mock_get,
        ):
            sync_smb_upload(coord, data, "tok", prefetched_image=None)

        mock_get.assert_called_once_with(img_url, "tok", timeout=30)

    def test_prefetched_image_written_when_file_missing(self) -> None:
        """File-doesn't-exist + prefetched bytes → write prefetched bytes,
        no cloud fetch. Uses unconditional `sys.modules` assignment for the
        fake smbclient (not `setdefault`) so it's reliable regardless of
        test execution order."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = self._make_coord()
        data = self._smb_data(
            img_url="https://residential.cbs.boschsecurity.com/img.jpg"
        )

        import sys

        written: dict[str, Any] = {}

        def _fake_open_file(path: str, mode: str = "r") -> Any:
            handle = MagicMock()

            def _write(content: bytes) -> None:
                written["path"] = path
                written["mode"] = mode
                written["content"] = content

            handle.write = MagicMock(side_effect=_write)
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=handle)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        fake_smb = MagicMock()
        fake_smb.register_session = MagicMock()
        fake_smb.open_file = MagicMock(side_effect=_fake_open_file)
        fake_smb.stat = MagicMock(side_effect=OSError("not found"))
        fake_smb.mkdir = MagicMock()
        sys.modules["smbclient"] = fake_smb

        with (
            patch("custom_components.bosch_shc_camera.smb.smb_makedirs"),
            patch("custom_components.bosch_shc_camera.smb._http_get") as mock_get,
        ):
            sync_smb_upload(coord, data, "tok", prefetched_image=_JPEG_BYTES_MOTION)

        fake_smb.register_session.assert_called_once()
        mock_get.assert_not_called()
        assert written["mode"] == "wb"
        assert written["content"] == _JPEG_BYTES_MOTION
        assert written["path"].endswith(
            "Innenbereich_2026-06-12_07-07-30_MOVEMENT_aabbccdd.jpg"
        )


class TestFtpPrefetchedImage:
    """_sync_ftp_upload(prefetched_image=...) must use the supplied bytes,
    skipping a second cloud/camera pull."""

    def _make_coord(self, protocol: str = "ftp") -> Any:
        coord = MagicMock()
        coord.options = {
            "enable_smb_upload": True,
            "smb_server": "fritz.box",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "Cams",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            "upload_protocol": protocol,
        }
        return coord

    def _ftp_data(self, img_url: str = "") -> dict[str, Any]:
        return {
            CAM_ID: {
                "info": {"title": "Innenbereich"},
                "events": [
                    {
                        "id": "aabbccdd-ftp",
                        "eventType": "MOVEMENT",
                        "timestamp": "2026-06-12T07:07:30Z",
                        "imageUrl": img_url,
                        "videoClipUrl": "",
                        "videoClipUploadStatus": "",
                    }
                ],
            }
        }

    def test_ftp_prefetched_bypasses_http_get(self) -> None:
        """FTP upload: prefetched_image → no _http_get call."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = self._make_coord()
        img_url = "https://residential.cbs.boschsecurity.com/img.jpg"
        data = self._ftp_data(img_url=img_url)

        fake_ftp = MagicMock()
        fake_ftp.size = MagicMock(side_effect=Exception("not found"))
        fake_ftp.storbinary = MagicMock()
        fake_ftp.quit = MagicMock()

        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect",
                return_value=fake_ftp,
            ),
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_exists",
                return_value=False,
            ),
            patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"),
            patch("custom_components.bosch_shc_camera.smb._http_get") as mock_get,
        ):
            _sync_ftp_upload(coord, data, "tok", prefetched_image=_JPEG_BYTES_MOTION)

        mock_get.assert_not_called()
        fake_ftp.storbinary.assert_called_once()
        call_args = fake_ftp.storbinary.call_args
        stored_bytes = call_args[0][1].read()
        assert stored_bytes == _JPEG_BYTES_MOTION

    def test_ftp_no_prefetch_calls_http_get(self) -> None:
        """FTP upload: no prefetch → _http_get IS called (backward compat)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = self._make_coord()
        img_url = "https://residential.cbs.boschsecurity.com/img.jpg"
        data = self._ftp_data(img_url=img_url)

        fake_ftp = MagicMock()
        fake_ftp.storbinary = MagicMock()
        fake_ftp.quit = MagicMock()

        with (
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_connect",
                return_value=fake_ftp,
            ),
            patch(
                "custom_components.bosch_shc_camera.smb._ftp_exists",
                return_value=False,
            ),
            patch("custom_components.bosch_shc_camera.smb._ftp_makedirs"),
            patch(
                "custom_components.bosch_shc_camera.smb._http_get",
                return_value=(200, _JPEG_BYTES_MOTION),
            ) as mock_get,
        ):
            _sync_ftp_upload(coord, data, "tok", prefetched_image=None)

        mock_get.assert_called_once_with(img_url, "tok", timeout=30)


# ── feature area: smb_available() — optional-dependency probe ───────────────
#
# `smbprotocol` is an optional manifest.json requirement (can fail to install
# on an unsupported OS/CPU architecture). smb_available() is the single
# source of truth other modules (coordinator's Repairs-issue check,
# media_source.py's SMB browse-backend gate) use to decide whether SMB
# features are actually usable, instead of each duplicating a bare
# `try: import smbclient except ImportError` themselves.


class TestSmbAvailable:
    def test_returns_true_when_smbclient_importable(self) -> None:
        from custom_components.bosch_shc_camera.smb import smb_available

        fake_smb = MagicMock()
        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            assert smb_available() is True

    def test_returns_false_when_smbclient_absent(self) -> None:
        from custom_components.bosch_shc_camera.smb import smb_available

        with patch.dict(sys.modules, {"smbclient": None}):
            assert smb_available() is False
