"""Unit tests for mjpeg_snapshot.py — fetch_mjpeg_snapshot helper.

Tests cover:
  - Success: FFmpeg returns JPEG bytes
  - Timeout: subprocess hangs → None + warning
  - Non-zero return code: FFmpeg crashed → None + warning
  - Empty stdout: FFmpeg exited 0 but no output → None + warning
  - Missing JPEG magic: output does not start with 0xFF 0xD8 → None + warning
  - Missing required params: host/user/password empty → None (no subprocess)
  - FileNotFoundError: ffmpeg not found → None + error
  - OSError on spawn: generic OS error → None + warning
"""

from __future__ import annotations

import asyncio
from asyncio import subprocess as aiosubprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

FAKE_JPEG = (
    b"\xff\xd8\xff\xe0" + b"\x00" * 16 + b"\xff\xd9"
)  # minimal valid-looking JPEG
CAM_HOST = "10.0.0.149"
CAM_PORT = 443
USER = "cbs-ABCDEF12"
PASS = "supersecret"


def _mock_proc(
    returncode: int = 0, stdout: bytes = FAKE_JPEG, stderr: bytes = b""
) -> MagicMock:
    """Build a mock asyncio subprocess with communicate() returning (stdout, stderr)."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    return proc


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestFetchMjpegSnapshot:
    @pytest.mark.asyncio
    async def test_success_returns_jpeg_bytes(self):
        """Happy path: FFmpeg exits 0, stdout is a JPEG → bytes returned."""
        proc = _mock_proc(returncode=0, stdout=FAKE_JPEG)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS)
        assert result == FAKE_JPEG

    @pytest.mark.asyncio
    async def test_timeout_returns_none_and_kills_proc(self):
        """Subprocess hangs past timeout → None, proc.kill() called."""
        proc = _mock_proc()
        # Make communicate() hang by raising TimeoutError at wait_for level
        proc.communicate = AsyncMock(side_effect=TimeoutError())
        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", side_effect=TimeoutError()),
        ):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(
                CAM_HOST, CAM_PORT, USER, PASS, timeout=0.001
            )
        assert result is None
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_kill_raises_process_lookup_error_returns_none(self):
        """Race: process exits between timeout detection and kill() call.
        kill() raises ProcessLookupError → swallowed, None still returned (line 144)."""
        proc = _mock_proc()
        proc.kill = MagicMock(side_effect=ProcessLookupError("already gone"))
        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", side_effect=TimeoutError()),
        ):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(
                CAM_HOST, CAM_PORT, USER, PASS, timeout=0.001
            )
        assert result is None
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_nonzero_returncode_returns_none(self):
        """FFmpeg exits with code != 0 → None + warning logged."""
        proc = _mock_proc(returncode=1, stdout=b"", stderr=b"some error")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_stdout_returns_none(self):
        """FFmpeg exits 0 but stdout is empty → None + warning logged."""
        proc = _mock_proc(returncode=0, stdout=b"")
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS)
        assert result is None

    @pytest.mark.asyncio
    async def test_bad_magic_returns_none(self):
        """Output does not start with JPEG magic (0xFF 0xD8) → None + warning."""
        not_jpeg = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # PNG bytes
        proc = _mock_proc(returncode=0, stdout=not_jpeg)
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_host_returns_none_without_subprocess(self):
        """Missing cam_host → None immediately, no subprocess spawned."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot("", CAM_PORT, USER, PASS)
        assert result is None
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_user_returns_none_without_subprocess(self):
        """Missing user → None immediately, no subprocess spawned."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, "", PASS)
        assert result is None
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_password_returns_none_without_subprocess(self):
        """Missing password → None immediately, no subprocess spawned."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, "")
        assert result is None
        mock_exec.assert_not_called()

    @pytest.mark.asyncio
    async def test_ffmpeg_not_found_returns_none(self):
        """ffmpeg binary not found (FileNotFoundError) → None + error logged."""
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=FileNotFoundError("ffmpeg not found")),
        ):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS)
        assert result is None

    @pytest.mark.asyncio
    async def test_os_error_on_spawn_returns_none(self):
        """Generic OSError on create_subprocess_exec → None + warning logged."""
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("spawn failed")),
        ):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            result = await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS)
        assert result is None

    @pytest.mark.asyncio
    async def test_rtsp_url_contains_correct_inst(self):
        """RTSP URL passed to FFmpeg must contain inst=3 (MJPEG stream instance)."""
        proc = _mock_proc(returncode=0, stdout=FAKE_JPEG)
        captured_args: list[str] = []

        async def fake_exec(*args: str, **kwargs: object) -> MagicMock:
            captured_args.extend(args)
            return proc

        with patch("asyncio.create_subprocess_exec", new=fake_exec):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS)

        # The RTSP URL argument must contain inst=3 and the camera host.
        # Scheme is `rtsps://` (FFmpeg negotiates TLS to port 443). User+password
        # are URL-encoded — Bosch cbs Digest passwords contain @ : / { | etc.
        rtsp_url = next((a for a in captured_args if a.startswith("rtsps://")), "")
        assert "inst=3" in rtsp_url, f"Expected inst=3 in RTSP URL, got: {rtsp_url}"
        assert CAM_HOST in rtsp_url
        assert USER in rtsp_url

    @pytest.mark.asyncio
    async def test_custom_timeout_passed_to_wait_for(self):
        """Custom timeout value is forwarded to asyncio.wait_for."""
        proc = _mock_proc(returncode=0, stdout=FAKE_JPEG)
        recorded_timeout: list[float] = []

        original_wait_for = asyncio.wait_for

        async def fake_wait_for(coro: object, timeout: float) -> object:
            recorded_timeout.append(timeout)
            return await original_wait_for(coro, timeout=10)  # type: ignore[arg-type]

        with (
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
            patch("asyncio.wait_for", new=fake_wait_for),
        ):
            from custom_components.bosch_shc_camera.mjpeg_snapshot import (
                fetch_mjpeg_snapshot,
            )

            await fetch_mjpeg_snapshot(CAM_HOST, CAM_PORT, USER, PASS, timeout=3.5)

        assert recorded_timeout and recorded_timeout[0] == 3.5
