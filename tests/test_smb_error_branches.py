"""SMB/FTP error-branch coverage for `smb.py` (Bucket A).

Pins the executor-side Exception swallows that no other test file covers:
  - L58  : sync_local_save bails out when download_path is empty (toggle on).
  - L95-96  : sync_local_save folder_pattern raises KeyError/ValueError → falls back to cam_safe.
  - L103-104: sync_local_save file_pattern raises KeyError/ValueError → falls back to "{cam}_{date}_{time}_{type}_{id}".
  - L244-245: sync_smb_upload urlopen for snapshot raises → warning, no open_file.
  - L269-270: sync_smb_upload urlopen for clip raises → warning, no open_file.
  - L333-334: sync_smb_cleanup _walk_and_delete scandir raises → recursion returns silently.
  - L388-389: _async_cleanup_alert services.async_call raises → debug log, no crash.
  - L505-506: _sync_ftp_upload urlopen for snapshot raises → warning, no storbinary.
  - L524-525: _sync_ftp_upload urlopen for clip raises → warning, no storbinary.
  - L532-533: _sync_ftp_upload ftp.quit + ftp.close BOTH raise → finally swallows, no crash.

All sockets/HTTP are mocked. No filesystem writes (sync_local_save uses
tmp_path) and no live HA event loop is required for the executor functions.
urllib.request.urlopen is patched instead of requests.Session (removed).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.smb"
CAM_ID = "11111111-1111-1111-1111-111111111111"
URLOPEN = f"{MODULE}.urllib.request.urlopen"


def _coord(options: dict | None = None):
    opts = dict(options or {})
    opts.setdefault("enable_local_save", True)
    opts.setdefault("enable_smb_upload", True)
    return SimpleNamespace(options=opts, hass=MagicMock(), _download_started_at=0.0)


def _urlopen_resp(status: int = 200, content: bytes = b"X",
                  raises: Exception | None = None) -> MagicMock:
    """Build a fake urlopen context-manager response."""
    resp = MagicMock()
    resp.status = status
    resp.read.side_effect = [content, b""]
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _fake_smb():
    smb = MagicMock()
    smb.register_session = MagicMock()
    smb.mkdir = MagicMock()
    smb.open_file = MagicMock()
    smb.stat = MagicMock(side_effect=OSError("missing"))
    smb.scandir = MagicMock(return_value=[])
    smb.remove = MagicMock()
    return smb


def _smb_event(image_url="https://cdn.bosch.com/snap.jpg", clip_url=None):
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


# ── L58 — sync_local_save: empty download_path returns ─────────────────────


class TestLocalSaveEmptyDownloadPath:
    """Toggle on + download_path empty must short-circuit before urlopen call."""

    def test_empty_download_path_returns_without_urlopen(self):
        """`download_path=""` after strip → early return (line 58)."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({
            "enable_local_save": True,
            "download_path": "   ",  # whitespace only → strip empties
        })
        ev = _smb_event()
        with patch(URLOPEN) as mock_urlopen:
            sync_local_save(coord, ev, "tok", "Terrasse")
        mock_urlopen.assert_not_called()


# ── L95-96 / L103-104 — pattern format errors ──────────────────────────────


class TestLocalSavePatternFormatErrors:
    """Bad user-supplied folder_pattern / file_pattern keys must not crash;
    a sensible fallback path is used instead."""

    def test_folder_pattern_unknown_key_falls_back_to_cam(self, tmp_path):
        """`{nonexistent}` in folder_pattern → KeyError caught → sub = cam_safe (L95-96)."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({
            "enable_local_save": True,
            "download_path": str(tmp_path),
            "folder_pattern": "{nonexistent}/{year}",
        })
        resp = _urlopen_resp(200, b"JPG")
        ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
        with patch(URLOPEN, return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        # File landed somewhere under Terrasse/ (cam_safe fallback)
        found = list(tmp_path.rglob("Terrasse*"))
        assert found, "Fallback path missing — KeyError not handled cleanly"

    def test_file_pattern_unknown_key_falls_back(self, tmp_path):
        """`{nonexistent}` in file_pattern → KeyError caught → default stem (L103-104)."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({
            "enable_local_save": True,
            "download_path": str(tmp_path),
            "file_pattern": "{nonexistent}_{date}",
        })
        resp = _urlopen_resp(200, b"JPG")
        ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
        with patch(URLOPEN, return_value=resp):
            sync_local_save(coord, ev, "tok", "Terrasse")

        # Fallback stem is `{cam}_{date}_{time}_{type}_{id}` — must contain MOVEMENT + date
        jpgs = list(tmp_path.rglob("*.jpg"))
        assert jpgs, "No file written — fallback stem missing"
        assert any("MOVEMENT" in p.name for p in jpgs)


# ── L244-245 — SMB snapshot upload exception swallow ───────────────────────


class TestSmbSnapshotUploadException:
    """urlopen() raising during snapshot fetch must not crash the worker;
    a warning is logged and execution continues (L244-245)."""

    def test_snapshot_urlopen_exception_logged_no_crash(self):
        """ConnectionError on snapshot urlopen → except branch (L244-245) → no upload, no exception."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_share": "SHARE",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })
        fake_smb = _fake_smb()

        with patch.dict(sys.modules, {"smbclient": fake_smb}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs"), \
             patch(URLOPEN, side_effect=ConnectionError("link down")):
            ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            # Must not raise
            sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_not_called()


# ── L269-270 — SMB clip upload exception swallow ───────────────────────────


class TestSmbClipUploadException:
    """urlopen() raising during clip fetch must not crash (L269-270)."""

    def test_clip_urlopen_exception_logged_no_crash(self):
        """Timeout on clip urlopen → warning, no open_file for .mp4."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_share": "SHARE",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })
        fake_smb = _fake_smb()

        with patch.dict(sys.modules, {"smbclient": fake_smb}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs"), \
             patch(URLOPEN, side_effect=TimeoutError("read timeout")):
            # No image → only clip path executes
            ev = _smb_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_not_called()


# ── L333-334 — SMB cleanup scandir exception swallow ───────────────────────


class TestSmbCleanupScandirException:
    """scandir() raising must be caught and the recursion ends silently (L333-334)."""

    def test_scandir_exception_returns_no_delete(self):
        """PermissionError on scandir → except branch returns, no remove() call."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord({
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_share": "SHARE",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "30",
            "upload_protocol": "smb",
        })
        fake_smb = _fake_smb()
        fake_smb.scandir.side_effect = PermissionError("access denied")

        with patch.dict(sys.modules, {"smbclient": fake_smb}), \
             patch(f"{MODULE}.socket"):
            # Must not raise
            sync_smb_cleanup(coord)

        fake_smb.remove.assert_not_called()


# ── L388-389 — _async_cleanup_alert services.async_call exception ──────────


class TestAsyncCleanupAlertException:
    """If the notify service exists but its async_call raises, the alert must
    not crash the cleanup task (L388-389)."""

    @pytest.mark.asyncio
    async def test_notify_service_exception_swallowed(self):
        """`services.async_call` raising → debug log, no propagation."""
        from custom_components.bosch_shc_camera.smb import _async_cleanup_alert

        coord = SimpleNamespace(hass=MagicMock())
        coord.hass.services.has_service = MagicMock(return_value=True)
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("notify broken"))

        # Must not raise — the except branch is logged at DEBUG
        await _async_cleanup_alert(coord, "msg", "notify.test_user")

        coord.hass.services.async_call.assert_awaited_once()


# ── L505-506 — FTP snapshot request exception ──────────────────────────────


class TestFtpSnapshotUploadException:
    """urlopen for FTP snapshot raising must log + continue (L505-506)."""

    def test_ftp_snapshot_exception_logged_no_crash(self):
        """ConnectionError on FTP snapshot urlopen → except branch, no storbinary."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.fritz.box",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })
        fake_ftp = MagicMock()

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=False), \
             patch(URLOPEN, side_effect=ConnectionError("link down")):
            ev = _smb_event(image_url="https://cdn.bosch.com/snap.jpg")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()


# ── L524-525 — FTP clip request exception ──────────────────────────────────


class TestFtpClipUploadException:
    """urlopen for FTP clip raising must log + continue (L524-525)."""

    def test_ftp_clip_exception_logged_no_crash(self):
        """TimeoutError on FTP clip urlopen → except branch, no storbinary."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.fritz.box",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })
        fake_ftp = MagicMock()

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=False), \
             patch(URLOPEN, side_effect=TimeoutError("read timeout")):
            ev = _smb_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()


# ── L532-533 — finally: quit raises + close raises ─────────────────────────


class TestFtpFinallyBothQuitAndCloseRaise:
    """Bug: if ftp.quit() raises AND ftp.close() raises the function must still
    return cleanly (the outer `try: ftp.close() except: pass` swallows it).
    L532-533 = the inner `except Exception: pass` around close()."""

    def test_quit_raises_then_close_raises_swallowed(self):
        """quit() + close() both raise → outer caller sees no exception."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.fritz.box",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })
        fake_ftp = MagicMock()
        fake_ftp.quit.side_effect = Exception("connection reset")
        fake_ftp.close.side_effect = Exception("socket already gone")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=True):
            # Empty data → just exercises connect → finally block
            _sync_ftp_upload(coord, {}, "tok")

        fake_ftp.quit.assert_called_once()
        fake_ftp.close.assert_called_once()
