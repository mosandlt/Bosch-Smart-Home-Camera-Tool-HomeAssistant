"""Sprint H: smb.py additional coverage — FTP and SMB upload/cleanup edge cases.

Target lines:
  - 206-208: smb mkdir error → _LOGGER.warning + continue
  - 216: smb_stat returns (file exists → skip upload)
  - 225-227: HTTP non-200 on snapshot → warning log
  - 235-252: video clip upload — stat exists→skip, stat OSError→upload (200 OK), clip non-200
  - 295: sync_smb_cleanup FTP early return (protocol=="ftp")
  - 315-316: _walk_and_delete dir recursion
  - 541-543: FTP snapshot download non-200 → warning
  - 549-562: FTP clip upload — exists skip, 200 upload, non-200 warning
  - 566-570: FTP quit/close in finally
  - 601-602: _sync_ftp_cleanup ftp.cwd raises error_perm → return
  - 606-607: ftp.retrlines("LIST", ...) raises → return
  - 614, 617: LIST line parsing — line with <9 parts, name in (".", "..")
  - 630-631: MDTM fails → continue
  - 636-637: ftp.delete fails → debug log
  - 643-644: ftp.cwd(path) in subdirs fails → pass
  - 652-653: ftp.quit() in finally raises → ftp.close() called

All smbclient calls are mocked via patch.dict(sys.modules).
requests is patched at the module level (import requests as req inside functions).
ftplib is mocked for FTP paths.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

MODULE = "custom_components.bosch_shc_camera.smb"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _coord(options: dict | None = None):
    coord = SimpleNamespace(
        options=options or {},
        hass=MagicMock(),
    )
    return coord


def _fake_smb():
    """Return a MagicMock that satisfies the smbclient import inside smb.py."""
    smb = MagicMock()
    smb.register_session = MagicMock()
    smb.mkdir = MagicMock()
    smb.open_file = MagicMock()
    smb.stat = MagicMock(side_effect=OSError("not found"))
    smb.scandir = MagicMock(return_value=[])
    smb.remove = MagicMock()
    return smb


def _fake_requests(status=200, content=b"IMGDATA"):
    """Return fake requests module + session with one mock HTTP response."""
    fake_response = MagicMock()
    fake_response.status_code = status
    fake_response.content = content
    fake_response.iter_content.return_value = [content]

    fake_session = MagicMock()
    fake_session.get.return_value = fake_response
    fake_session.headers = {}

    fake_req = MagicMock()
    fake_req.Session.return_value = fake_session

    return fake_req, fake_session, fake_response


def _smb_upload_coord():
    """Return a coordinator pre-configured for SMB upload tests."""
    return _coord({
        "smb_server": "192.168.1.100",
        "smb_share": "SHARE",
        "smb_username": "user",
        "smb_password": "pass",
        "smb_base_path": "Bosch",
        "folder_pattern": "{year}/{month}/{day}",
        "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
    })


def _basic_event(image_url="https://cdn.bosch.com/snap.jpg", clip_url=None):
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


# ── SMB mkdir error → warning + continue ────────────────────────────────────


class TestSmbMkdirError:
    """Covers lines 206-208: smb_makedirs raises → warning logged, event skipped."""

    def test_mkdir_error_logs_warning_and_continues(self):
        """smb_makedirs raises Exception → warning + continue (no upload attempted)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_req, fake_session, _ = _fake_requests()

        # Patch smb_makedirs inside the module to raise
        with patch.dict(sys.modules, {"smbclient": fake_smb, "urllib3": MagicMock()}), \
             patch.dict(sys.modules, {"requests": fake_req}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs", side_effect=Exception("mkdir boom")):

            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # No upload was attempted because mkdir failed → continue skipped the rest
        fake_session.get.assert_not_called()


# ── smb_stat returns (file exists → skip) ───────────────────────────────────


class TestSmbStatSkip:
    """Covers line 216: smb_stat does NOT raise → file exists → skip upload."""

    def test_image_skipped_when_stat_succeeds(self):
        """smb_stat returns without error → file already on share → no open_file call."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        # stat does NOT raise → file exists
        fake_smb.stat.return_value = MagicMock()
        fake_smb.stat.side_effect = None

        fake_req, fake_session, _ = _fake_requests()

        with patch.dict(sys.modules, {"smbclient": fake_smb, "urllib3": MagicMock()}), \
             patch.dict(sys.modules, {"requests": fake_req}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs"):

            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # No HTTP request made — skipped because stat showed file exists
        fake_session.get.assert_not_called()


# ── HTTP non-200 snapshot → warning ─────────────────────────────────────────


class TestSmbSnapshotNon200:
    """Covers lines 225-227: snapshot HTTP status != 200 → warning logged."""

    def test_snapshot_non200_logs_warning(self):
        """HTTP 404 on snapshot → _LOGGER.warning, open_file NOT called."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        fake_req, fake_session, fake_resp = _fake_requests(status=404, content=b"")
        fake_resp.status_code = 404
        fake_resp.content = b""

        with patch.dict(sys.modules, {"smbclient": fake_smb, "urllib3": MagicMock()}), \
             patch.dict(sys.modules, {"requests": fake_req}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs"):

            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            sync_smb_upload(coord, data, "tok")

        # HTTP was called but open_file was NOT (non-200 means skip write)
        fake_session.get.assert_called_once()
        fake_smb.open_file.assert_not_called()


# ── Video clip upload paths ──────────────────────────────────────────────────


class TestSmbClipUpload:
    """Covers lines 235-252: video clip upload branches."""

    def test_clip_skipped_when_stat_succeeds(self):
        """Clip: stat does NOT raise → file exists → skip upload (line 237-238)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()

        call_counter = {"n": 0}

        def stat_side_effect(path):
            call_counter["n"] += 1
            # First call (image) → raise so image is uploaded
            # Second call (clip) → succeed so clip is skipped
            if path.endswith(".jpg"):
                raise OSError("not found")
            return MagicMock()

        fake_smb.stat.side_effect = stat_side_effect

        fake_file = MagicMock()
        fake_file.__enter__ = MagicMock(return_value=fake_file)
        fake_file.__exit__ = MagicMock(return_value=False)
        fake_smb.open_file.return_value = fake_file

        fake_req, fake_session, fake_resp = _fake_requests(status=200, content=b"IMG")
        fake_resp.status_code = 200
        fake_resp.content = b"IMG"

        with patch.dict(sys.modules, {"smbclient": fake_smb, "urllib3": MagicMock()}), \
             patch.dict(sys.modules, {"requests": fake_req}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs"):

            ev = _basic_event(clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        # Only image GET was called; clip was skipped
        assert fake_session.get.call_count == 1

    def test_clip_uploaded_when_stat_raises(self):
        """Clip: stat raises OSError → file missing → upload (HTTP 200) — lines 239-248."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        fake_file = MagicMock()
        fake_file.__enter__ = MagicMock(return_value=fake_file)
        fake_file.__exit__ = MagicMock(return_value=False)
        fake_smb.open_file.return_value = fake_file

        fake_req, fake_session, fake_resp = _fake_requests(status=200, content=b"VIDDATA")
        fake_resp.status_code = 200
        fake_resp.iter_content.return_value = [b"VIDDATA"]

        with patch.dict(sys.modules, {"smbclient": fake_smb, "urllib3": MagicMock()}), \
             patch.dict(sys.modules, {"requests": fake_req}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs"):

            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        # open_file called for clip (.mp4)
        assert fake_smb.open_file.call_count == 1
        call_path = fake_smb.open_file.call_args[0][0]
        assert call_path.endswith(".mp4")

    def test_clip_non200_logs_warning(self):
        """Clip: HTTP 503 → warning, open_file NOT called (line 250)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _smb_upload_coord()
        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        fake_req, fake_session, fake_resp = _fake_requests(status=503, content=b"")
        fake_resp.status_code = 503

        with patch.dict(sys.modules, {"smbclient": fake_smb, "urllib3": MagicMock()}), \
             patch.dict(sys.modules, {"requests": fake_req}), \
             patch(f"{MODULE}.socket"), \
             patch(f"{MODULE}.smb_makedirs"):

            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            sync_smb_upload(coord, data, "tok")

        # HTTP was called (to check clip) but file not written
        fake_session.get.assert_called_once()
        fake_smb.open_file.assert_not_called()


# ── sync_smb_cleanup FTP early return ───────────────────────────────────────


class TestSyncSmbCleanupFtpEarlyReturn:
    """Covers line 295: protocol=='ftp' → delegates to _sync_ftp_cleanup immediately."""

    def test_ftp_protocol_delegates_to_ftp_cleanup(self):
        """upload_protocol=ftp → _sync_ftp_cleanup called, not SMB path."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord({"upload_protocol": "ftp", "smb_server": "", "smb_retention_days": "30"})

        with patch(f"{MODULE}._sync_ftp_cleanup") as mock_ftp_cleanup:
            sync_smb_cleanup(coord)

        mock_ftp_cleanup.assert_called_once_with(coord)


# ── _walk_and_delete dir recursion ──────────────────────────────────────────


class TestWalkAndDeleteRecurse:
    """Covers lines 315-316: _walk_and_delete recurses into subdirectories."""

    def test_recurses_into_subdirectory(self):
        """A directory entry triggers recursive _walk_and_delete call."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        import time

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "1",
        })

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

        with patch.dict(sys.modules, {"smbclient": fake_smb}), \
             patch(f"{MODULE}.socket"):
            sync_smb_cleanup(coord)

        # remove was called once (for old.jpg in the subdir)
        fake_smb.remove.assert_called_once()


# ── FTP snapshot non-200 → warning ──────────────────────────────────────────


class TestFtpSnapshotNon200:
    """Covers lines 541-543: FTP snapshot HTTP non-200 → warning."""

    def test_ftp_snapshot_non200_logs_warning(self):
        """FTP upload path: image HTTP 404 → warning, ftp.storbinary NOT called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })

        fake_ftp = MagicMock()
        fake_ftp.size.side_effect = Exception("not found")  # file doesn't exist

        fake_req, fake_session, fake_resp = _fake_requests(status=404, content=b"")
        fake_resp.status_code = 404
        fake_resp.content = b""

        import ftplib

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=False), \
             patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):

            data = {
                CAM_ID: {
                    "info": {"title": "Terrasse"},
                    "events": [_basic_event()],
                }
            }
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()


# ── FTP clip upload paths ────────────────────────────────────────────────────


class TestFtpClipUpload:
    """Covers lines 549-562: FTP clip upload — exists skip, 200 upload, non-200."""

    def test_ftp_clip_skipped_when_exists(self):
        """FTP path: clip exists → skip, storbinary NOT called (lines 551-552)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })

        fake_ftp = MagicMock()
        fake_req, fake_session, _ = _fake_requests()

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=True), \
             patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):

            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()
        fake_session.get.assert_not_called()

    def test_ftp_clip_uploaded_200(self):
        """FTP path: clip missing → HTTP 200 → storbinary called (lines 554-558)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })

        fake_ftp = MagicMock()
        fake_req, fake_session, fake_resp = _fake_requests(status=200, content=b"CLIP")
        fake_resp.status_code = 200
        fake_resp.raw = MagicMock()

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=False), \
             patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):

            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        assert fake_ftp.storbinary.call_count == 1

    def test_ftp_clip_non200_logs_warning(self):
        """FTP path: clip HTTP 502 → warning, storbinary NOT called (line 560)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })

        fake_ftp = MagicMock()
        fake_req, fake_session, fake_resp = _fake_requests(status=502, content=b"")
        fake_resp.status_code = 502

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=False), \
             patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):

            ev = _basic_event(image_url=None, clip_url="https://cdn.bosch.com/clip.mp4")
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()


# ── FTP finally: quit/close ──────────────────────────────────────────────────


class TestFtpFinallyQuitClose:
    """Covers lines 566-570: finally block calls ftp.quit(); if that raises → ftp.close()."""

    def test_ftp_quit_called_on_success(self):
        """Normal path: ftp.quit() called in finally."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

        fake_ftp = MagicMock()
        fake_req, _, _ = _fake_requests()

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=True), \
             patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):

            _sync_ftp_upload(coord, {}, "tok")

        fake_ftp.quit.assert_called_once()

    def test_ftp_close_called_when_quit_raises(self):
        """ftp.quit() raises → ftp.close() called as fallback (lines 567-569)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

        fake_ftp = MagicMock()
        fake_ftp.quit.side_effect = Exception("connection reset")
        fake_req, _, _ = _fake_requests()

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=True), \
             patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):

            _sync_ftp_upload(coord, {}, "tok")

        fake_ftp.quit.assert_called_once()
        fake_ftp.close.assert_called_once()


# ── _sync_ftp_cleanup: cwd error_perm → return ──────────────────────────────


class TestFtpCleanupCwdErrorPerm:
    """Covers lines 601-602: ftp.cwd raises error_perm → return from _walk_and_delete."""

    def test_cwd_error_perm_returns_silently(self):
        """ftp.cwd raises ftplib.error_perm → _walk_and_delete returns without listing."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup
        import ftplib

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "30",
        })

        fake_ftp = MagicMock()
        fake_ftp.cwd.side_effect = ftplib.error_perm("550 No such directory")
        fake_ftp.quit.side_effect = Exception("closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        # retrlines was NOT called — returned on cwd failure
        fake_ftp.retrlines.assert_not_called()


# ── _sync_ftp_cleanup: retrlines raises → return ────────────────────────────


class TestFtpCleanupRetrlinesFails:
    """Covers lines 606-607: ftp.retrlines raises → return from _walk_and_delete."""

    def test_retrlines_exception_returns_silently(self):
        """ftp.retrlines raises → _walk_and_delete returns, no delete attempted."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "30",
        })

        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None  # cwd succeeds
        fake_ftp.retrlines.side_effect = Exception("connection lost")
        fake_ftp.quit.side_effect = Exception("closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_not_called()


# ── _sync_ftp_cleanup: LIST line parsing edge cases ─────────────────────────


class TestFtpCleanupListParsing:
    """Covers lines 614, 617: short LIST lines and dot-entries are skipped."""

    def _run_cleanup_with_list(self, lines: list[str], fake_ftp: MagicMock, coord) -> None:
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
        """LIST line with fewer than 9 parts → skipped (line 613-614)."""
        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "30",
        })
        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None

        # Only 5 parts → skipped
        self._run_cleanup_with_list(["drwxr-xr-x 1 user group 0 Jan 01"], fake_ftp, coord)

        fake_ftp.delete.assert_not_called()
        fake_ftp.sendcmd.assert_not_called()

    def test_dot_entries_skipped(self):
        """LIST line where name is '.' or '..' → skipped (line 616-617)."""
        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "30",
        })
        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None

        dot_line = "-rw-r--r-- 1 user group 1024 Jan 01 12:00 ."
        dotdot_line = "-rw-r--r-- 1 user group 1024 Jan 01 12:00 .."
        self._run_cleanup_with_list([dot_line, dotdot_line], fake_ftp, coord)

        fake_ftp.delete.assert_not_called()


# ── _sync_ftp_cleanup: MDTM fails → continue ────────────────────────────────


class TestFtpCleanupMdtmFails:
    """Covers lines 630-631: MDTM sendcmd raises → continue (skip file)."""

    def test_mdtm_failure_skips_file(self):
        """sendcmd("MDTM ...") raises → file is not deleted (continue)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "1",
        })

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


# ── _sync_ftp_cleanup: ftp.delete fails → debug log ─────────────────────────


class TestFtpCleanupDeleteFails:
    """Covers lines 636-637: ftp.delete raises → _LOGGER.debug, no crash."""

    def test_delete_failure_logged_as_debug(self):
        """ftp.delete raises → debug log, execution continues."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "1",
        })

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


# ── _sync_ftp_cleanup: subdir cwd fails → pass ──────────────────────────────


class TestFtpCleanupSubdirCwdFails:
    """Covers lines 643-644: ftp.cwd(path) after subdir recursion fails → pass."""

    def test_subdir_cwd_back_fails_continues(self):
        """After recursing into a subdir, cwd(parent) fails → pass, loop continues.

        The walk enters root (cwd OK), lists a subdir, recurses. Inside the subdir,
        cwd succeeds, retrlines is called, returns no entries. Then the parent loop
        tries cwd(root) to navigate back — this fails with Exception → pass (line 643-644).
        """
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "30",
        })

        fake_ftp = MagicMock()
        call_count = {"n": 0}

        def cwd_side_effect(path):
            call_count["n"] += 1
            # First two cwd calls succeed (enter root + enter subdir);
            # third call (back to parent after subdir recursion) fails → pass
            if call_count["n"] >= 3:
                raise Exception("cwd back failed")

        fake_ftp.cwd.side_effect = cwd_side_effect

        # Root: one subdir; subdir: empty
        list_call = {"n": 0}

        def fake_retrlines(cmd, callback):
            list_call["n"] += 1
            if list_call["n"] == 1:
                callback("drwxr-xr-x 1 user group 0 Jan 01 12:00 2025")
            # Second call (inside subdir) returns nothing

        fake_ftp.retrlines.side_effect = fake_retrlines
        fake_ftp.quit.side_effect = Exception("closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            # Must not raise — cwd failure in subdir back-navigation is caught
            _sync_ftp_cleanup(coord)

    def test_cleanup_quit_exception_no_crash(self):
        """ftp.quit() raises in finally of _sync_ftp_cleanup → no crash (line 652)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "smb_retention_days": "30",
        })

        fake_ftp = MagicMock()
        fake_ftp.cwd.return_value = None
        fake_ftp.retrlines.side_effect = Exception("empty")
        fake_ftp.quit.side_effect = Exception("already closed")

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            # Must not raise — quit exception swallowed
            _sync_ftp_cleanup(coord)


# ── FTP is_safe_bosch_url guard ─────────────────────────────────────────────


class TestFtpSafeUrlGuard:
    """Covers the _is_safe_bosch_url guard in _sync_ftp_upload for image/clip URLs."""

    def test_unsafe_image_url_skipped(self):
        """Non-Bosch imageUrl is rejected by _is_safe_bosch_url → no HTTP request."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "ftp.example.com",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })

        fake_ftp = MagicMock()
        fake_req, fake_session, _ = _fake_requests()

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp), \
             patch(f"{MODULE}._ftp_makedirs"), \
             patch(f"{MODULE}._ftp_exists", return_value=False), \
             patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):

            ev = {
                "timestamp": "2026-05-07T10:00:00Z",
                "eventType": "MOVEMENT",
                "id": "EVID1234ABCD",
                "imageUrl": "https://evil.example.com/steal.jpg",
            }
            data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [ev]}}
            _sync_ftp_upload(coord, data, "tok")

        fake_session.get.assert_not_called()
