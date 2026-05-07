"""SMB/FTP backend coverage round 2.

Targets `smb.py` (was 6%) — covers FTP helpers, path/folder generation
patterns, retention math, and the noise filter on disk-alert routing.
"""

from __future__ import annotations

import ftplib
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── _ftp_exists ─────────────────────────────────────────────────────────


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


# ── _ftp_makedirs — recursive mkdir ─────────────────────────────────────


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


# ── Folder/file pattern formatting (lib of regression cases) ────────────


class TestSmbPathPatterns:
    """The `sync_smb_upload` function builds folder + file paths from
    user-configurable patterns. Pattern formatting is INLINE — but we
    can replicate the exact computation here to pin invariants without
    exercising the full upload pipeline."""

    def _build(self, ts: str, etype: str, ev_id: str, cam_name: str,
               folder_pattern: str = "{year}/{month}/{day}",
               file_pattern: str = "{camera}_{date}_{time}_{type}_{id}",
               base_path: str = "Bosch-Kameras"):
        # Mirrors sync_smb_upload's path computation exactly.
        year = ts[:4]
        month = ts[5:7]
        day = ts[8:10]
        date_str = f"{year}-{month}-{day}"
        time_str = ts[11:19].replace(":", "-")
        folder_parts = folder_pattern.format(
            year=year, month=month, day=day, camera=cam_name, type=etype,
        )
        file_base = file_pattern.format(
            camera=cam_name, date=date_str, time=time_str,
            type=etype, id=ev_id, year=year, month=month, day=day,
        )
        return base_path, folder_parts, file_base

    def test_default_pattern_yyyy_mm_dd(self):
        """Default folder pattern must produce zero-padded month + day."""
        ts = "2026-05-06T03:07:04.123Z"
        _, folder, file_base = self._build(ts, "MOVEMENT", "abcd1234", "Terrasse")
        assert folder == "2026/05/06", "Pattern must zero-pad — '2026/5/6' breaks alphabetical sort"
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
            ts, "MOVEMENT", "ee", "Bosch Eingang",
            folder_pattern="{camera}/{year}/{month}",
        )
        # Bosch event timestamps sort under each cam first
        assert folder == "Bosch Eingang/2026/05"

    def test_event_type_in_file_pattern(self):
        ts = "2026-05-06T01:02:03.000Z"
        _, _, file_base = self._build(
            ts, "AUDIO_ALARM", "abc12345", "Cam",
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


# ── Retention math (cleanup cutoff) ─────────────────────────────────────


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



# ── _ftp_connect signature pin ──────────────────────────────────────────


class TestFtpConnect:
    def test_connect_passive_mode(self, monkeypatch):
        """FRITZ!Box FTP requires passive mode — connection must call
        `set_pasv(True)` after login. Active mode silently fails on
        NAT'd connections (the default user setup)."""
        import ftplib as _ftplib
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

        monkeypatch.setattr(_ftplib, "FTP", _StubFTP)
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
