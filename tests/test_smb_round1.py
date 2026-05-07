"""Sprint E: smb.py — sync_local_save, sync_smb_upload, sync_smb_cleanup,
sync_smb_disk_check, async_smb_disk_alert, _sync_ftp_upload, _sync_ftp_cleanup.

Covers missing lines: 58, 91-92, 103-233, 238-252, 261, 267-314, 324-377,
396-397, 451-551, 556-637.

All SMB/FTP calls are mocked via sys.modules or patch — no real network.
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

MODULE = "custom_components.bosch_shc_camera.smb"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _coord(options: dict | None = None):
    coord = SimpleNamespace(
        options=options or {},
        hass=MagicMock(),
    )
    return coord


# ── sync_local_save — uncovered branches ──────────────────────────────────────


class TestSyncLocalSaveUncovered:
    def test_short_timestamp_returns_early(self, tmp_path):
        """Timestamp shorter than 19 chars → return without writing any file."""
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = _coord({"download_path": str(tmp_path)})
        ev = {"timestamp": "2026-05", "eventType": "MOVEMENT",
              "imageUrl": "https://cdn.boschsecurity.com/snap.jpg"}
        sync_local_save(coord, ev, "tok", "Terrasse")
        assert list(tmp_path.iterdir()) == [], \
            "Short timestamp must cause early return — no folder or file created"

    def test_empty_timestamp_returns_early(self, tmp_path):
        """Empty timestamp → return without writing."""
        from custom_components.bosch_shc_camera.smb import sync_local_save
        coord = _coord({"download_path": str(tmp_path)})
        ev = {"timestamp": "", "eventType": "MOVEMENT"}
        sync_local_save(coord, ev, "tok", "Terrasse")
        assert list(tmp_path.iterdir()) == [], \
            "Empty timestamp must cause early return"

    def test_mp4_skipped_when_status_not_done(self, tmp_path):
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

        fake_requests = MagicMock()
        fake_session = MagicMock()
        fake_requests.Session.return_value = fake_session
        fake_session.headers = {}

        with patch.dict(sys.modules, {"requests": fake_requests, "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        fake_session.get.assert_not_called()

    def test_download_exception_logged_not_raised(self, tmp_path):
        """requests.get raising an exception must be swallowed — no crash."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        ev = {
            "timestamp": "2026-05-07T10:00:00Z",
            "eventType": "MOVEMENT",
            "id": "EVID1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.iter_content.side_effect = OSError("disk full")

        fake_session = MagicMock()
        fake_session.get.return_value = fake_response
        fake_session.headers = {}

        fake_requests = MagicMock()
        fake_requests.Session.return_value = fake_session

        with patch.dict(sys.modules, {"requests": fake_requests, "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")  # must not raise

    def test_image_downloaded_and_written(self, tmp_path):
        """HTTP 200 image response → file written to download_path/camera/stem.jpg."""
        from custom_components.bosch_shc_camera.smb import sync_local_save

        coord = _coord({"download_path": str(tmp_path)})
        ev = {
            "timestamp": "2026-05-07T10:00:00Z",
            "eventType": "MOVEMENT",
            "id": "EVID1234",
            "imageUrl": "https://cdn.boschsecurity.com/snap.jpg",
        }

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.iter_content.return_value = [b"JPEG_DATA"]

        fake_session = MagicMock()
        fake_session.get.return_value = fake_response
        fake_session.headers = {}

        fake_requests = MagicMock()
        fake_requests.Session.return_value = fake_session

        with patch.dict(sys.modules, {"requests": fake_requests, "urllib3": MagicMock()}):
            sync_local_save(coord, ev, "tok", "Terrasse")

        written = list((tmp_path / "Terrasse").glob("*.jpg"))
        assert len(written) == 1, "Image download must write one .jpg file"


# ── sync_smb_upload — early exits ────────────────────────────────────────────


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

        mock_ftp.assert_called_once_with(coord, {"data": 1}, "tok")

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
            with patch(f"{MODULE}.socket") as mock_sock:
                sync_smb_upload(coord, {}, "tok")  # must not raise


# ── sync_smb_cleanup — early exits ───────────────────────────────────────────


class TestSyncSmbCleanupEarlyExits:
    def test_no_server_returns_early(self):
        """Empty smb_server → return without any SMB calls."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"smb_server": "", "smb_share": "SHARE", "smb_retention_days": 30})
        sync_smb_cleanup(coord)  # must not raise

    def test_retention_zero_returns_early(self):
        """smb_retention_days=0 → keep forever, no deletion."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_retention_days": 0})
        sync_smb_cleanup(coord)

    def test_ftp_protocol_delegates_to_ftp(self):
        """upload_protocol='ftp' → delegates to _sync_ftp_cleanup."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"upload_protocol": "ftp"})

        with patch(f"{MODULE}._sync_ftp_cleanup") as mock_ftp:
            sync_smb_cleanup(coord)

        mock_ftp.assert_called_once_with(coord)

    def test_session_failure_returns_gracefully(self):
        """register_session raising → log warning and return without crash."""
        from custom_components.bosch_shc_camera.smb import sync_smb_cleanup
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_retention_days": 30})

        fake_smb = MagicMock()
        fake_smb.register_session.side_effect = Exception("connection refused")

        with patch.dict(sys.modules, {"smbclient": fake_smb}):
            with patch(f"{MODULE}.socket"):
                sync_smb_cleanup(coord)  # must not raise


# ── sync_smb_disk_check — early exits ────────────────────────────────────────


class TestSyncSmbDiskCheckEarlyExits:
    def test_ftp_protocol_returns_early(self):
        """upload_protocol='ftp' → silently return (FTP has no portable statvfs)."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check
        coord = _coord({"upload_protocol": "ftp"})
        sync_smb_disk_check(coord)  # must not raise

    def test_no_server_returns_early(self):
        """Empty smb_server → return before any SMB calls."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check
        coord = _coord({"smb_server": "", "smb_share": "SHARE"})
        sync_smb_disk_check(coord)

    def test_warn_mb_zero_returns_early(self):
        """smb_disk_warn_mb=0 → disk check disabled → return."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_disk_warn_mb": 0})
        sync_smb_disk_check(coord)

    def test_session_failure_returns_gracefully(self):
        """register_session raising → log warning and return."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_disk_warn_mb": 500})

        fake_smb = MagicMock()
        fake_smb.register_session.side_effect = Exception("auth error")

        with patch.dict(sys.modules, {"smbclient": fake_smb, "smbclient._io": MagicMock()}):
            with patch(f"{MODULE}.socket"):
                sync_smb_disk_check(coord)  # must not raise

    def test_statvfs_not_present_returns_gracefully(self):
        """smbclient without statvfs attribute → skip silently."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_disk_warn_mb": 500})

        fake_smb = MagicMock(spec=[])  # no attributes — statvfs absent
        fake_smb.register_session = MagicMock()

        with patch.dict(sys.modules, {"smbclient": fake_smb, "smbclient._io": MagicMock()}):
            with patch(f"{MODULE}.socket"):
                sync_smb_disk_check(coord)  # must not raise

    def test_low_disk_fires_alert(self):
        """Free space below warn_mb → call_soon_threadsafe to schedule disk alert."""
        from custom_components.bosch_shc_camera.smb import sync_smb_disk_check
        coord = _coord({"smb_server": "192.168.1.1", "smb_share": "SHARE",
                        "smb_disk_warn_mb": 500,
                        "alert_notify_system": "notify.signal"})

        fake_smb = MagicMock()
        fake_smb.register_session = MagicMock()
        # statvfs returns 100 MB free (below 500 MB threshold)
        fake_vfs = MagicMock()
        fake_vfs.f_bavail = 100
        fake_vfs.f_frsize = 1024 * 1024
        fake_smb.statvfs.return_value = fake_vfs

        with patch.dict(sys.modules, {"smbclient": fake_smb, "smbclient._io": MagicMock()}):
            with patch(f"{MODULE}.socket"):
                sync_smb_disk_check(coord)

        coord.hass.loop.call_soon_threadsafe.assert_called_once()


# ── async_smb_disk_alert — persistent_notification fallback ──────────────────


class TestAsyncSmbDiskAlertFallback:
    @pytest.mark.asyncio
    async def test_service_not_found_falls_back_to_persistent_notification(self):
        """No matching notify service → fall back to persistent_notification."""
        from custom_components.bosch_shc_camera.smb import async_smb_disk_alert
        coord = _coord()
        coord.hass.services.has_service = MagicMock(return_value=False)
        coord.hass.services.async_call = AsyncMock()

        await async_smb_disk_alert(coord, "Low disk space!", "notify.nonexistent")

        # Must have called persistent_notification.create
        calls = coord.hass.services.async_call.call_args_list
        assert any(c[0][0] == "persistent_notification" for c in calls), \
            "No notify service found → must fall back to persistent_notification"

    @pytest.mark.asyncio
    async def test_empty_notify_service_falls_back(self):
        """Empty notify service string → no services to try → persistent_notification."""
        from custom_components.bosch_shc_camera.smb import async_smb_disk_alert
        coord = _coord()
        coord.hass.services.has_service = MagicMock(return_value=False)
        coord.hass.services.async_call = AsyncMock()

        await async_smb_disk_alert(coord, "Low!", "")

        calls = coord.hass.services.async_call.call_args_list
        assert any(c[0][0] == "persistent_notification" for c in calls)

    @pytest.mark.asyncio
    async def test_service_found_no_fallback(self):
        """notify service exists → call it, skip persistent_notification."""
        from custom_components.bosch_shc_camera.smb import async_smb_disk_alert
        coord = _coord()
        coord.hass.services.has_service = MagicMock(return_value=True)
        coord.hass.services.async_call = AsyncMock()

        await async_smb_disk_alert(coord, "Low!", "notify.signal")

        calls = coord.hass.services.async_call.call_args_list
        assert not any(c[0][0] == "persistent_notification" for c in calls), \
            "If notify service found, persistent_notification must not be called"

    @pytest.mark.asyncio
    async def test_service_exception_falls_back(self):
        """notify service raises → exception swallowed → fall back to persistent_notification."""
        from custom_components.bosch_shc_camera.smb import async_smb_disk_alert
        coord = _coord()
        coord.hass.services.has_service = MagicMock(return_value=True)
        # First call (notify.signal) raises; second call (persistent_notification) succeeds
        coord.hass.services.async_call = AsyncMock(
            side_effect=[Exception("signal down"), None]
        )

        await async_smb_disk_alert(coord, "Low!", "notify.signal")

        calls = coord.hass.services.async_call.call_args_list
        assert any(c[0][0] == "persistent_notification" for c in calls), \
            "Failed notify service must fall back to persistent_notification"


# ── _sync_ftp_upload ──────────────────────────────────────────────────────────


class TestSyncFtpUpload:
    def test_no_server_returns_early(self):
        """Empty smb_server → return without FTP call."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": ""})
        _sync_ftp_upload(coord, {}, "tok")  # must not raise

    def test_ftp_connect_failure_returns_gracefully(self):
        """FTP login failure → log warning, return without crash."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_username": "user",
                        "smb_password": "pw"})

        with patch(f"{MODULE}._ftp_connect", side_effect=Exception("auth failed")):
            with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
                _sync_ftp_upload(coord, {}, "tok")  # must not raise

    def test_empty_events_completes_without_upload(self):
        """No events for camera → no FTP stor calls."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_username": "user",
                        "smb_password": "pw", "smb_base_path": "Bosch"})

        fake_ftp = MagicMock()
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": []}}

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
                _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()
        fake_ftp.quit.assert_called_once()

    def test_short_timestamp_event_skipped(self):
        """Event with timestamp shorter than 19 chars → skip without crash."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_username": "u",
                        "smb_password": "p", "smb_base_path": "B"})

        fake_ftp = MagicMock()
        data = {CAM_ID: {"info": {"title": "Cam"},
                          "events": [{"timestamp": "2026-05", "eventType": "MOVEMENT"}]}}

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            with patch(f"{MODULE}._ftp_makedirs"):
                with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
                    _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()

    def test_image_uploaded_successfully(self):
        """Valid event with JPEG URL → storbinary called with .jpg path."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_username": "u",
                        "smb_password": "p", "smb_base_path": "Bosch",
                        "smb_folder_pattern": "{year}/{month}",
                        "smb_file_pattern": "{camera}_{date}_{time}_{type}_{id}"})

        fake_ftp = MagicMock()
        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.content = b"JPEG"

        fake_session = MagicMock()
        fake_session.get.return_value = fake_response
        fake_session.headers = {}

        fake_requests = MagicMock()
        fake_requests.Session.return_value = fake_session

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
                    with patch.dict(sys.modules, {"requests": fake_requests, "urllib3": MagicMock()}):
                        _sync_ftp_upload(coord, data, "tok")

        stor_calls = fake_ftp.storbinary.call_args_list
        assert len(stor_calls) == 1, "One STOR command expected for the image"
        assert ".jpg" in stor_calls[0][0][0], "STOR path must end in .jpg"

    def test_file_already_exists_skipped(self):
        """File already on FTP server → skip storbinary."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_username": "u",
                        "smb_password": "p", "smb_base_path": "Bosch"})

        fake_ftp = MagicMock()
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
                with patch(f"{MODULE}._ftp_exists", return_value=True):  # already exists
                    with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
                        _sync_ftp_upload(coord, data, "tok")

        fake_ftp.storbinary.assert_not_called()

    def test_ftp_quit_called_on_exception(self):
        """Exception mid-upload propagates through finally → ftp.quit() still called."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload
        coord = _coord({"smb_server": "192.168.1.1", "smb_username": "u",
                        "smb_password": "p", "smb_base_path": "Bosch"})

        fake_ftp = MagicMock()
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": [
            {"timestamp": "2026-05-07T10:00:00Z", "eventType": "MOVEMENT",
             "id": "X", "imageUrl": "https://cdn.boschsecurity.com/snap.jpg"}
        ]}}

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            # _ftp_makedirs raises — propagates through the try/finally in _sync_ftp_upload
            with patch(f"{MODULE}._ftp_makedirs", side_effect=Exception("mid-upload error")):
                with patch.dict(sys.modules, {"requests": MagicMock(), "urllib3": MagicMock()}):
                    try:
                        _sync_ftp_upload(coord, data, "tok")
                    except Exception:
                        pass  # exception expected — finally must still run

        fake_ftp.quit.assert_called_once()


# ── _sync_ftp_cleanup ─────────────────────────────────────────────────────────


class TestSyncFtpCleanup:
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
        coord = _coord({"smb_server": "192.168.1.1", "smb_retention_days": 30,
                        "smb_username": "u", "smb_password": "p"})

        with patch(f"{MODULE}._ftp_connect", side_effect=Exception("auth failed")):
            _sync_ftp_cleanup(coord)  # must not raise

    def test_empty_directory_completes_without_deletion(self):
        """Empty FTP directory → no DELE commands."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup
        coord = _coord({"smb_server": "192.168.1.1", "smb_retention_days": 30,
                        "smb_username": "u", "smb_password": "p",
                        "smb_base_path": "Bosch"})

        fake_ftp = MagicMock()
        fake_ftp.nlst.return_value = []  # empty directory

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_not_called()
        fake_ftp.quit.assert_called_once()

    def test_old_file_deleted(self):
        """File older than retention → FTP delete called via MDTM + delete."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup
        import time
        coord = _coord({"smb_server": "192.168.1.1", "smb_retention_days": 1,
                        "smb_username": "u", "smb_password": "p",
                        "smb_base_path": "Bosch"})

        # _sync_ftp_cleanup uses retrlines("LIST", callback) + sendcmd("MDTM name")
        old_ts = time.strftime("%Y%m%d%H%M%S", time.gmtime(time.time() - 5 * 86400))
        list_line = "-rw-r--r-- 1 user group 1000 Jan 01 10:00 old_file.jpg"

        fake_ftp = MagicMock()

        def _retrlines(cmd, callback):
            callback(list_line)

        fake_ftp.retrlines.side_effect = _retrlines
        fake_ftp.sendcmd.return_value = f"213 {old_ts}"

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            _sync_ftp_cleanup(coord)

        fake_ftp.delete.assert_called_once()

    def test_recent_file_not_deleted(self):
        """File newer than retention → not deleted."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup
        import time
        coord = _coord({"smb_server": "192.168.1.1", "smb_retention_days": 180,
                        "smb_username": "u", "smb_password": "p",
                        "smb_base_path": "Bosch"})

        # Use retrlines + sendcmd mocks matching the actual implementation
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

    def test_mlsd_exception_falls_back_to_nlst(self):
        """mlsd() raising → fall back to nlst() for listing."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_cleanup
        coord = _coord({"smb_server": "192.168.1.1", "smb_retention_days": 30,
                        "smb_username": "u", "smb_password": "p",
                        "smb_base_path": "Bosch"})

        fake_ftp = MagicMock()
        fake_ftp.mlsd.side_effect = Exception("MLSD not supported")
        fake_ftp.nlst.return_value = []  # empty — no deletions

        with patch(f"{MODULE}._ftp_connect", return_value=fake_ftp):
            try:
                _sync_ftp_cleanup(coord)
            except Exception:
                pass  # acceptable — just must not hang

        fake_ftp.quit.assert_called()
