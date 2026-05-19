"""Coverage tests for `_SmbBackend.open_file` + `open_flat_file` cleanup.

When `smbclient.open_file` raises (closed FRITZ.NAS connection, EACCES,
NtStatus) the SMB session cache must be torn down before the exception
propagates — otherwise it leaks until the media-source background sweeper
catches it. Pins media_source.py L456-458 + L511-513.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.media_source import _SmbBackend


def _backend() -> _SmbBackend:
    hass = MagicMock()
    hass.data = {}
    return _SmbBackend(hass, {
        "smb_server": "nas",
        "smb_share": "M",
        "smb_username": "u",
        "smb_password": "p",
        "smb_base_path": "",
    })


def _install_failing_smbclient(stat_raises: Exception | None = None,
                               open_raises: Exception | None = None) -> MagicMock:
    """Inject a fake `smbclient` into sys.modules whose stat/open_file raise."""
    mod = MagicMock()
    mod.register_session = MagicMock()
    fake_stat = MagicMock()
    fake_stat.st_size = 1024
    if stat_raises is not None:
        mod.stat = MagicMock(side_effect=stat_raises)
    else:
        mod.stat = MagicMock(return_value=fake_stat)
    if open_raises is not None:
        mod.open_file = MagicMock(side_effect=open_raises)
    else:
        mod.open_file = MagicMock(return_value=MagicMock())
    sys.modules["smbclient"] = mod
    return mod


class TestOpenFileExceptionCleanup:
    def test_open_file_closes_session_on_smb_error(self):
        """`open_file()` raises → `_close_session_cache(cache)` runs +
        exception propagates. Pins L456-458 of media_source.py."""
        backend = _backend()
        _install_failing_smbclient(open_raises=OSError("NtStatus 0xc0000043"))
        with patch.object(
            backend, "_close_session_cache", wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(OSError, match="NtStatus 0xc0000043"):
                backend.open_file("Terrasse", "2026", "05", "19", "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4")
        close_spy.assert_called_once()

    def test_open_flat_file_closes_session_on_smb_error(self):
        """Same contract for the flat-layout variant. Pins L511-513."""
        backend = _backend()
        _install_failing_smbclient(open_raises=OSError("simulated SMB blowup"))
        with patch.object(
            backend, "_close_session_cache", wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(OSError, match="simulated SMB blowup"):
                backend.open_flat_file("Terrasse", "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4")
        close_spy.assert_called_once()

    def test_open_file_closes_session_on_stat_error(self):
        """`stat()` raising before `open_file()` also runs cleanup."""
        backend = _backend()
        _install_failing_smbclient(stat_raises=PermissionError("EACCES"))
        with patch.object(
            backend, "_close_session_cache", wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(PermissionError):
                backend.open_file("Terrasse", "2026", "05", "19", "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4")
        close_spy.assert_called_once()
