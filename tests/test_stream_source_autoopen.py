"""stream_source() opens the live session when none is active (GitHub #74).

HomeKit asks camera.async_get_stream_source() cold — no play_stream or WebRTC
entry point opens the Bosch session first, so a None source aborted the
stream with "Camera has no stream source". Source for the call path:
homeassistant/components/homekit/type_cameras.py `_async_get_stream_source`.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_shc_camera.camera import BoschCamera
from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED

CAM_ID = "11111111-1111-1111-1111-111111111111"
URL = "rtsps://127.0.0.1:46597/rtsp_tunnel"


def _cam(
    *, privacy: bool = False, result: object = {"ok": True}, populate: bool = True
) -> BoschCamera:
    coord = SimpleNamespace(
        live_connections={},
        shc_state_cache={CAM_ID: {"privacy_mode": privacy}},
        stream_warming=set(),
        async_update_listeners=lambda: None,
        get_model_config=lambda _cam_id: SimpleNamespace(min_total_wait=1),
    )

    async def _open(_cam_id: str) -> object:
        if result is not None and populate:
            # A coalesced start (STREAM_START_SKIPPED) is falsy but the other
            # start still populates the session.
            coord.live_connections[CAM_ID] = {
                "rtspsUrl": URL,
                "_connection_type": "LOCAL",
            }
        return result

    coord.try_live_connection = AsyncMock(side_effect=_open)
    cam = object.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._display_name = "Test"
    cam.stream_options = {}
    return cam


@pytest.mark.asyncio
async def test_opens_live_connection_when_none_active() -> None:
    cam = _cam()
    assert await cam.stream_source() == URL
    cam.coordinator.try_live_connection.assert_awaited_once_with(CAM_ID)
    assert cam.stream_options == {"rtsp_transport": "tcp"}


@pytest.mark.asyncio
async def test_existing_session_is_not_reopened() -> None:
    cam = _cam()
    cam.coordinator.live_connections[CAM_ID] = {"rtspsUrl": URL}
    assert await cam.stream_source() == URL
    cam.coordinator.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_privacy_mode_returns_none_without_opening() -> None:
    cam = _cam(privacy=True)
    assert await cam.stream_source() is None
    cam.coordinator.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_start_returns_none() -> None:
    cam = _cam(result=None)
    assert await cam.stream_source() is None


@pytest.mark.asyncio
async def test_coalesced_start_waits_for_the_other_start() -> None:
    cam = _cam(result=STREAM_START_SKIPPED)
    assert await cam.stream_source() == URL


@pytest.mark.asyncio
async def test_coalesced_start_that_never_populates_returns_none() -> None:
    cam = _cam(result=STREAM_START_SKIPPED, populate=False)
    assert await cam.stream_source() is None


@pytest.mark.asyncio
async def test_prewarm_timeout_returns_none() -> None:
    cam = _cam()
    cam._wait_for_prewarm = AsyncMock(return_value=False)
    assert await cam.stream_source() is None
    cam._wait_for_prewarm.assert_awaited_once_with("stream_source")


@pytest.mark.asyncio
async def test_provider_probe_does_not_open_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA core probes providers via stream_source() on every entity add and
    go2rtc (re)load (camera/webrtc.py `async_get_supported_provider`); that
    must stay passive or every idle camera opens a Bosch session at startup."""
    cam = _cam()
    seen: list[str | None] = []

    async def _probe(_self: object, *, write_state: bool = True) -> None:
        seen.append(await cam.stream_source())

    monkeypatch.setattr(
        "homeassistant.components.camera.Camera.async_refresh_providers", _probe
    )
    await cam.async_refresh_providers()

    assert seen == [None]
    cam.coordinator.try_live_connection.assert_not_awaited()
    assert cam._probing_providers is False
    assert await cam.stream_source() == URL
