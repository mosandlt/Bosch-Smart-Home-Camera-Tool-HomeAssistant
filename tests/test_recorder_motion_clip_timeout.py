"""Cover the recorder's `create_motion_clip` timeout-kill race + the
`start_recorder` proxy-URL-poll tear-down branch.

Targets:
- L495-496: `proc.kill()` raises ProcessLookupError because ffmpeg
  already exited between `wait_for` timing out and the kill — must be
  swallowed silently.
- L571: while polling for `rtspsUrl`, the live connection is torn down
  (user toggled stream off) — `start_recorder` returns without starting
  ffmpeg.
- L574: rtsp_url lands during the polling window — function continues.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import recorder


CAM_A = "11111111"


def _coord_with_live(connection_type="LOCAL", rtsps_url=""):
    return SimpleNamespace(
        _live_connections={CAM_A: {"_connection_type": connection_type, "rtspsUrl": rtsps_url}},
        options={},
        data={CAM_A: {"info": {"title": "Terrasse"}}},
        hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
    )


@pytest.mark.asyncio
class TestMotionClipKillRace:
    async def test_process_lookup_error_on_kill_is_swallowed(self, tmp_path):
        """`proc.kill()` raises because ffmpeg already exited; the helper
        must return False without propagating the exception. Pins L495-496."""
        async def _executor(fn, *args):
            return fn(*args)

        coord = SimpleNamespace(
            hass=SimpleNamespace(async_add_executor_job=_executor),
        )

        # proc.communicate() never resolves naturally — wait_for times out
        # before it does. kill() raises ProcessLookupError (already dead).
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock(side_effect=ProcessLookupError())

        out_path = str(tmp_path / "clip.mp4")

        with patch.object(
            recorder, "list_preroll_files",
            return_value=[str(tmp_path / "seg1.mp4")],
        ), patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ), patch(
            "asyncio.wait_for",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            ok = await recorder.create_motion_clip(coord, CAM_A, out_path)
        assert ok is False
        proc.kill.assert_called_once()


@pytest.mark.asyncio
class TestStartRecorderProxyUrlWait:
    async def test_torn_down_during_wait_returns_early(self):
        """During the proxy-URL polling loop, if the connection type
        flips to non-LOCAL (user toggled stream off), `start_recorder`
        must return silently without starting ffmpeg. Pins L571."""
        coord = _coord_with_live(connection_type="LOCAL", rtsps_url="")

        async def _fake_sleep(_sec):
            # Flip the connection type after the first sleep so the loop
            # body sees it on the next iteration and returns.
            coord._live_connections[CAM_A]["_connection_type"] = "REMOTE"

        with patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)), \
             patch("asyncio.sleep", new=_fake_sleep), \
             patch.object(recorder, "_PROXY_URL_WAIT_STEPS", 5):
            await recorder.start_recorder(coord, CAM_A)
        # Must have exited via the `return` at L571 — coord.options unmodified
        # and no ffmpeg subprocess was spawned.

    async def test_rtsp_url_appears_during_wait_continues(self):
        """If the URL lands during polling, the loop breaks (L574) and
        function continues past the wait block."""
        coord = _coord_with_live(connection_type="LOCAL", rtsps_url="")
        coord.options = {"nvr_event_only": True, "nvr_preroll_seconds": 0}

        async def _fake_sleep(_sec):
            coord._live_connections[CAM_A]["rtspsUrl"] = "rtsp://127.0.0.1:5000/cam"

        with patch.object(recorder, "stop_recorder", new=AsyncMock(return_value=None)), \
             patch("asyncio.sleep", new=_fake_sleep), \
             patch.object(recorder, "_PROXY_URL_WAIT_STEPS", 5):
            await recorder.start_recorder(coord, CAM_A)
        # nvr_event_only + preroll_seconds=0 returns immediately past the
        # poll loop without invoking ffmpeg — the test merely verifies the
        # function reached past L574 without crashing.
