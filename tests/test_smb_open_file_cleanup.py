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
    return _SmbBackend(
        hass,
        {
            "smb_server": "nas",
            "smb_share": "M",
            "smb_username": "u",
            "smb_password": "p",
            "smb_base_path": "",
        },
    )


def _install_failing_smbclient(
    stat_raises: Exception | None = None, open_raises: Exception | None = None
) -> MagicMock:
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
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(OSError, match="NtStatus 0xc0000043"):
                backend.open_file(
                    "Terrasse",
                    "2026",
                    "05",
                    "19",
                    "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4",
                )
        close_spy.assert_called_once()

    def test_open_flat_file_closes_session_on_smb_error(self):
        """Same contract for the flat-layout variant. Pins L511-513."""
        backend = _backend()
        _install_failing_smbclient(open_raises=OSError("simulated SMB blowup"))
        with patch.object(
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(OSError, match="simulated SMB blowup"):
                backend.open_flat_file(
                    "Terrasse", "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4"
                )
        close_spy.assert_called_once()

    def test_open_file_closes_session_on_stat_error(self):
        """`stat()` raising before `open_file()` also runs cleanup."""
        backend = _backend()
        _install_failing_smbclient(stat_raises=PermissionError("EACCES"))
        with patch.object(
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(PermissionError):
                backend.open_file(
                    "Terrasse",
                    "2026",
                    "05",
                    "19",
                    "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4",
                )
        close_spy.assert_called_once()


class TestSmbPathTraversal:
    """Regression (bug-hunt 2026-07-03): `_path()` string-joined every
    segment into the UNC path with ZERO validation — unlike `filename`,
    which every caller already re-validates before calling `_path()`.
    Camera titles come from the Bosch cloud account (in principle
    attacker-influenceable) and media_content_id segments are reachable via
    any media_source.resolve_media call, not just this integration's own
    browse UI, so a crafted `camera` segment containing "..\\" could escape
    `{share}\\{base}\\{camera}\\...` and read/list outside the intended
    NAS tree.
    """

    def test_path_rejects_backslash_traversal_segment(self) -> None:
        backend = _backend()
        with pytest.raises(FileNotFoundError):
            backend._path("..\\..\\Windows\\System32", "file.mp4")

    def test_path_rejects_dotdot_segment(self) -> None:
        backend = _backend()
        with pytest.raises(FileNotFoundError):
            backend._path("..", "file.mp4")

    def test_path_rejects_forward_slash_segment(self) -> None:
        backend = _backend()
        with pytest.raises(FileNotFoundError):
            backend._path("../etc/passwd", "file.mp4")

    def test_path_accepts_normal_segments(self) -> None:
        """No regression: a legitimate camera/date tree still builds the
        expected UNC path."""
        backend = _backend()
        path = backend._path("Terrasse", "2026", "05", "19", "file.mp4")
        assert path == "\\\\nas\\M\\Terrasse\\2026\\05\\19\\file.mp4"

    def test_path_skips_empty_segment(self) -> None:
        """An empty-string segment (e.g. a double-slash/trailing-empty split
        artifact) must be silently skipped, not raise and not appear in the
        joined path — pins the `if not seg: continue` guard."""
        backend = _backend()
        path = backend._path("Terrasse", "", "file.mp4")
        assert path == "\\\\nas\\M\\Terrasse\\file.mp4"

    def test_open_file_rejects_malicious_camera_before_touching_smbclient(
        self,
    ) -> None:
        """A malicious `camera` value must be rejected before smb_stat()/
        open_file() are ever called with the traversal path — proving the
        traversal never actually reaches the network layer."""
        backend = _backend()
        mod = _install_failing_smbclient()
        with patch.object(
            backend,
            "_close_session_cache",
            wraps=backend._close_session_cache,
        ) as close_spy:
            with pytest.raises(FileNotFoundError):
                backend.open_file(
                    "..\\..\\Windows\\System32",
                    "2026",
                    "05",
                    "19",
                    "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4",
                )
        mod.stat.assert_not_called()
        mod.open_file.assert_not_called()
        close_spy.assert_called_once()

    def test_open_flat_file_rejects_malicious_camera(self) -> None:
        backend = _backend()
        mod = _install_failing_smbclient()
        with pytest.raises(FileNotFoundError):
            backend.open_flat_file(
                "../etc", "Terrasse_2026-05-19_12-30-45_MOTION_ABC123.mp4"
            )
        mod.stat.assert_not_called()
        mod.open_file.assert_not_called()
