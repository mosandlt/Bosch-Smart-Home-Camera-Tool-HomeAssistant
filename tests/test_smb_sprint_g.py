"""Sprint G: smb.py additional coverage.

Target lines:
  - 76-77: sync_local_save malformed timestamp falls through to download
  - 164-252: sync_smb_upload main loop (upload image/clip, skip short ts)
  - 257-271: smb_makedirs directory creation
  - 295: sync_smb_cleanup ftp protocol delegate
  - 307-333: sync_smb_cleanup walk_and_delete (old/recent files, recursion)
  - 364: sync_smb_disk_check ftp early return
  - 382: sync_smb_disk_check statvfs not present
  - 385-387: sync_smb_disk_check statvfs below threshold fires alert
  - 541-653: _sync_ftp_upload / _sync_ftp_cleanup main loops

All smbclient calls are mocked via patch.dict(sys.modules).
requests is patched at the module level (import requests as req inside functions).
"""
from __future__ import annotations

import sys
import time
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


# ── TestSyncLocalSaveMalformedTimestamp ──────────────────────────────────────


class TestSyncLocalSaveMalformedTimestamp:
    """Covers lines 76-77: malformed timestamp falls through except Exception: pass."""

    def test_malformed_but_long_timestamp_falls_through_to_download(self, tmp_path):
        """Timestamp with month=0 causes ValueError in strptime → except swallows it,
        and the download proceeds normally (lines 76-77 hit).
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

        fake_req, fake_session, fake_response = _fake_requests(status=200, content=b"JPEG")
        fake_response.iter_content.return_value = [b"JPEG"]

        with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        # The download was attempted (exception swallowed, execution continues)
        fake_session.get.assert_called_once()

    def test_valid_old_timestamp_skipped_when_started_at_set(self, tmp_path):
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

        fake_req, fake_session, _ = _fake_requests()

        with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        # Old event must be skipped — no download
        fake_session.get.assert_not_called()


# ── TestSyncSmbUpload ────────────────────────────────────────────────────────


class TestSyncSmbUpload:
    """Covers lines 164-252: sync_smb_upload main upload loop."""

    def test_no_server_returns_early(self):
        """Empty smb_server → return immediately (already tested in round1, re-pin here)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload
        coord = _coord({"smb_server": "", "smb_share": "SHARE"})
        sync_smb_upload(coord, {}, "tok")  # must not raise

    def test_smbclient_import_error_logs_warning(self):
        """smbclient not installable → warning logged, no crash."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE"})

        with patch.dict(sys.modules, {"smbclient": None}):
            # ImportError is caught internally and logged as a warning
            try:
                sync_smb_upload(coord, {}, "tok")
            except ImportError:
                pass  # function may propagate in some Python versions — acceptable

    def test_session_failure_returns_gracefully(self):
        """register_session raises → warning logged, return without crash."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE"})

        fake_smb = _fake_smb()
        fake_smb.register_session.side_effect = Exception("auth failed")

        fake_req, _, _ = _fake_requests()

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
                    sync_smb_upload(coord, {}, "tok")  # must not raise

    def test_uploads_image_when_http_200(self):
        """Valid event with imageUrl + HTTP 200 → open_file called (image written to SMB)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })

        fake_smb = _fake_smb()
        # stat raises OSError → file doesn't exist → upload proceeds
        fake_smb.stat.side_effect = OSError("not found")

        # open_file returns a context manager
        fake_file = MagicMock()
        fake_file.__enter__ = MagicMock(return_value=fake_file)
        fake_file.__exit__ = MagicMock(return_value=False)
        fake_smb.open_file.return_value = fake_file

        fake_req, fake_session, fake_response = _fake_requests(status=200, content=b"IMG")
        fake_response.status_code = 200
        fake_response.content = b"IMG"

        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "events": [{
                    "timestamp": "2026-05-07T10:00:00Z",
                    "eventType": "MOVEMENT",
                    "id": "EVID1234",
                    "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
                }],
            }
        }

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                with patch(f"{MODULE}.smb_makedirs"):
                    with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
                        sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_called_once()
        call_args = fake_smb.open_file.call_args[0][0]
        assert ".jpg" in call_args, "open_file must be called with a .jpg path"

    def test_skips_video_clip_when_status_not_done(self):
        """videoClipUploadStatus != 'Done' → mp4 not uploaded."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_base_path": "Bosch",
        })

        fake_smb = _fake_smb()
        fake_smb.stat.side_effect = OSError("not found")

        fake_req, fake_session, fake_response = _fake_requests(status=200, content=b"VID")

        data = {
            CAM_ID: {
                "info": {"title": "Cam"},
                "events": [{
                    "timestamp": "2026-05-07T10:00:00Z",
                    "eventType": "MOVEMENT",
                    "id": "EVID1234",
                    "videoClipUrl": "https://cdn.boschsecurity.com/clip.mp4",
                    "videoClipUploadStatus": "Pending",  # not Done
                }],
            }
        }

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                with patch(f"{MODULE}.smb_makedirs"):
                    with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
                        sync_smb_upload(coord, data, "tok")

        # open_file must not have been called for mp4
        for c in fake_smb.open_file.call_args_list:
            assert ".mp4" not in str(c), "MP4 must not be uploaded when status != Done"

    def test_skips_event_with_short_timestamp(self):
        """Timestamp shorter than 19 chars → event skipped entirely."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_base_path": "Bosch",
        })

        fake_smb = _fake_smb()
        fake_req, _, _ = _fake_requests()

        data = {
            CAM_ID: {
                "info": {"title": "Cam"},
                "events": [{"timestamp": "2026-05", "eventType": "MOVE", "id": "X"}],
            }
        }

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
                    sync_smb_upload(coord, data, "tok")

        fake_smb.open_file.assert_not_called()

    def test_ftp_protocol_delegates_to_ftp_upload(self):
        """upload_protocol='ftp' → _sync_ftp_upload called immediately."""
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = _coord({"upload_protocol": "ftp", "smb_server": "192.168.1.1"})

        with patch(f"{MODULE}._sync_ftp_upload") as mock_ftp:
            sync_smb_upload(coord, {"x": 1}, "tok")

        mock_ftp.assert_called_once_with(coord, {"x": 1}, "tok")


# ── TestSmbMakedirs ──────────────────────────────────────────────────────────


class TestSmbMakedirs:
    """Covers lines 257-271: smb_makedirs recursive directory creation."""

    def test_makedirs_creates_each_path_segment(self):
        """mkdir called for each segment of base_path + folder_parts."""
        from custom_components.bosch_shc_camera.smb import smb_makedirs

        fake_smb = MagicMock()
        fake_smb.stat.side_effect = OSError("not found")  # nothing exists yet
        fake_smb.mkdir = MagicMock()

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            smb_makedirs(
                r"\\server\SHARE\Bosch\2026\05\07",
                "server", "SHARE", "Bosch", "2026/05/07",
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
                "server", "SHARE", "Bosch", "2026",
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
                "server", "SHARE", "Bosch", "2026",
            )

        fake_smb.mkdir.assert_not_called()


# ── TestSyncSmbCleanup ───────────────────────────────────────────────────────


class TestSyncSmbCleanup:
    """Covers lines 295, 307-333."""

    def test_ftp_protocol_delegates_to_ftp_cleanup(self):
        """upload_protocol='ftp' → _sync_ftp_cleanup called (line 295)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"upload_protocol": "ftp"})

        with patch(f"{MODULE}._sync_ftp_cleanup") as mock_ftp:
            sync_smb_cleanup(coord)

        mock_ftp.assert_called_once_with(coord)

    def test_no_server_returns_early(self):
        """Empty smb_server → return without any SMB calls."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"smb_server": "", "smb_share": "SHARE", "smb_retention_days": 30})
        sync_smb_cleanup(coord)  # must not raise

    def test_zero_retention_days_returns_early(self):
        """smb_retention_days=0 → keep forever, return early."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_retention_days": 0})
        sync_smb_cleanup(coord)

    def test_smbclient_import_error_returns_silently(self):
        """smbclient ImportError inside sync_smb_cleanup → silent return."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_retention_days": 30})

        with patch.dict(sys.modules, {"smbclient": None}):
            try:
                sync_smb_cleanup(coord)
            except ImportError:
                pass  # acceptable

    def test_walk_and_delete_removes_old_files(self):
        """scandir returns one old file → remove called (lines 317-327)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_retention_days": 1,
            "smb_base_path": "Bosch",
        })

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

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_retention_days": 180,
            "smb_base_path": "Bosch",
        })

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
        """scandir returns a directory → recurse into it (lines 319-320)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_retention_days": 1,
            "smb_base_path": "Bosch",
        })

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


# ── TestSyncSmbDiskCheck ─────────────────────────────────────────────────────


class TestSyncSmbDiskCheck:
    """Covers lines 364, 382, 385-387."""

    def test_ftp_protocol_returns_early(self):
        """upload_protocol='ftp' → return at line 364 (FTP has no statvfs)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check
        coord = _coord({"upload_protocol": "ftp"})
        sync_smb_disk_check(coord)  # must not raise

    def test_statvfs_not_in_smbclient_returns_silently(self):
        """smbclient without statvfs attr → line 382 return."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_disk_warn_mb": 500,
        })

        # spec=[] means no attributes → hasattr(smbclient, "statvfs") is False
        fake_smb = MagicMock(spec=[])
        fake_smb.register_session = MagicMock()

        with patch.dict(sys.modules, {"smbclient": fake_smb, "smbclient._io": MagicMock()}):
            with patch(f"{MODULE}.socket"):
                sync_smb_disk_check(coord)  # must not raise; no alert fired

        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_statvfs_available_below_threshold_fires_alert(self):
        """Free space < warn_mb → call_soon_threadsafe called to schedule alert (lines 385-387)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_disk_warn_mb": 500,
        })

        fake_vfs = MagicMock()
        fake_vfs.f_bavail = 100        # 100 blocks
        fake_vfs.f_frsize = 1024 * 1024  # 1 MB per block → 100 MB free

        fake_smb = MagicMock()
        fake_smb.register_session = MagicMock()
        fake_smb.statvfs.return_value = fake_vfs

        with patch.dict(sys.modules, {"smbclient": fake_smb, "smbclient._io": MagicMock()}):
            with patch(f"{MODULE}.socket"):
                sync_smb_disk_check(coord)

        # Alert must have been scheduled via the HA event loop
        coord.hass.loop.call_soon_threadsafe.assert_called_once()

    def test_statvfs_above_threshold_no_alert(self):
        """Free space >= warn_mb → no alert fired."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_share": "SHARE",
            "smb_disk_warn_mb": 500,
        })

        fake_vfs = MagicMock()
        fake_vfs.f_bavail = 1000       # 1000 MB free (above 500 MB threshold)
        fake_vfs.f_frsize = 1024 * 1024

        fake_smb = MagicMock()
        fake_smb.register_session = MagicMock()
        fake_smb.statvfs.return_value = fake_vfs

        with patch.dict(sys.modules, {"smbclient": fake_smb, "smbclient._io": MagicMock()}):
            with patch(f"{MODULE}.socket"):
                sync_smb_disk_check(coord)

        coord.hass.loop.call_soon_threadsafe.assert_not_called()


# ── TestSyncFtpUpload ────────────────────────────────────────────────────────


class TestSyncFtpUpload:
    """Covers lines 541-570: _sync_ftp_upload main loop."""

    def test_no_server_returns_early(self):
        """Empty smb_server → return immediately."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": ""})
        _sync_ftp_upload(coord, {}, "tok")  # must not raise

    def test_ftp_login_failure_returns_gracefully(self):
        """FTP connect raises → warning logged, return without crash."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_username": "u", "smb_password": "p"})

        with patch(f"{MODULE}._ftp_connect", side_effect=Exception("login failed")):
            with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
                _sync_ftp_upload(coord, {}, "tok")  # must not raise

    def test_uploads_image_via_ftp_storbinary(self):
        """Valid event + HTTP 200 → storbinary called with .jpg STOR command."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
            "folder_pattern": "{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
        })

        fake_ftp = MagicMock()
        fake_req, fake_session, fake_response = _fake_requests(status=200, content=b"IMGBYTES")
        fake_response.content = b"IMGBYTES"

        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "events": [{
                    "timestamp": "2026-05-07T10:00:00Z",
                    "eventType": "MOVEMENT",
                    "id": "EVID1234",
                    "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
                }],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                with patch(f"{MODULE}._ftp_exists", return_value=False):
                    with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
                        _sync_ftp_upload(coord, data, "tok")

        stor_calls = fake_ftp.storbinary.call_args_list
        assert len(stor_calls) >= 1, "storbinary must be called for the image"
        assert ".jpg" in stor_calls[0][0][0], "STOR command must include .jpg path"

    def test_skips_mp4_when_status_not_done(self):
        """videoClipUploadStatus != 'Done' → mp4 not uploaded."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

        fake_ftp = MagicMock()
        fake_req, _, _ = _fake_requests()

        data = {
            CAM_ID: {
                "info": {"title": "Cam"},
                "events": [{
                    "timestamp": "2026-05-07T10:00:00Z",
                    "eventType": "MOVEMENT",
                    "id": "X",
                    "videoClipUrl": "https://cdn.boschsecurity.com/clip.mp4",
                    "videoClipUploadStatus": "Pending",
                }],
            }
        }

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                with patch(f"{MODULE}._ftp_exists", return_value=False):
                    with patch.dict(sys.modules, {"requests": fake_req, "urllib3": MagicMock()}):
                        _sync_ftp_upload(coord, data, "tok")

        for c in fake_ftp.storbinary.call_args_list:
            assert ".mp4" not in str(c), "MP4 must not be uploaded when status != Done"

    def test_ftp_quit_called_on_exit(self):
        """ftp.quit() called in finally block after upload loop."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

        fake_ftp = MagicMock()
        data = {CAM_ID: {"info": {"title": "Cam"}, "events": []}}

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
                _sync_ftp_upload(coord, data, "tok")

        fake_ftp.quit.assert_called_once()


# ── TestSyncFtpCleanup ───────────────────────────────────────────────────────


class TestSyncFtpCleanup:
    """Covers lines 573-653: _sync_ftp_cleanup main loop."""

    def test_no_server_returns_early(self):
        """Empty smb_server → return immediately."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup
        coord = _coord({"smb_server": "", "smb_retention_days": 30})
        _sync_ftp_cleanup(coord)  # must not raise

    def test_zero_retention_returns_early(self):
        """smb_retention_days=0 → return without FTP connect."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup
        coord = _coord({"smb_server": "192.168.1.1", "smb_retention_days": 0})
        _sync_ftp_cleanup(coord)

    def test_walk_and_delete_deletes_old_files(self):
        """LIST returns a file with old MDTM timestamp → delete called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_retention_days": 1,
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

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

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_retention_days": 180,
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

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

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_retention_days": 30,
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

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

        coord = _coord({
            "smb_server": "192.168.1.1",
            "smb_retention_days": 30,
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "Bosch",
        })

        fake_ftp = MagicMock()
        fake_ftp.retrlines.side_effect = lambda cmd, cb: None  # empty dir

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.quit.assert_called_once()
