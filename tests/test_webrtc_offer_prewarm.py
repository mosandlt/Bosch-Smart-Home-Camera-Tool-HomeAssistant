"""Regression test for MOBILE_BACKLOG item: native WebRTC offer during pre-warm.

Symptom: idle→ONLINE LOCAL stream in the native HA more-info view (Companion
app) showed ~25-35s of black video with no retry. async_create_stream() (the
HLS/Cast path) already polls until LOCAL pre-warm clears before reading
stream_source() — but async_handle_async_webrtc_offer() (the native
app/go2rtc path) delegated straight to the base class, which reads
stream_source() immediately and gets None while _stream_warming still
contains the cam_id, failing instead of waiting like the card's own JS
retry (_waitForStreamReady) already does.

Fix: factor the pre-warm poll out into _wait_for_prewarm() and call it from
both async_create_stream() and a new async_handle_async_webrtc_offer()
override before delegating to super().
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "22222222-2222-2222-2222-222222222222"


def _make_coord(**overrides):
    base = dict(
        _live_connections={},
        _stream_warming=set(),
        _shc_state_cache={},
        try_live_connection=AsyncMock(return_value={"rtspsUrl": "rtsp://x"}),
        async_update_listeners=MagicMock(),
        get_model_config=lambda cid: SimpleNamespace(min_total_wait=2),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera(coord):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._display_name = "Bosch Innenbereich"
    return cam


@pytest.mark.asyncio
async def test_webrtc_offer_waits_for_prewarm_before_delegating():
    """A WebRTC offer arriving mid-warm-up must wait for pre-warm to clear
    before super().async_handle_async_webrtc_offer() is awaited — otherwise
    go2rtc reads stream_source()==None and the native view stays black."""
    coord = _make_coord(_stream_warming={CAM_ID})
    cam = _make_camera(coord)

    async def _finish_prewarm():
        await asyncio.sleep(0.3)
        coord._stream_warming.discard(CAM_ID)

    asyncio.create_task(_finish_prewarm())  # noqa: RUF006  # fire-and-forget in test

    send_message = MagicMock()
    with patch(
        "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
        new=AsyncMock(return_value=None),
    ) as mock_super:
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    assert CAM_ID not in coord._stream_warming
    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)


@pytest.mark.asyncio
async def test_webrtc_offer_no_warming_delegates_immediately():
    """No pre-warm in progress → no waiting, straight delegation (happy path)."""
    coord = _make_coord(_stream_warming=set())
    cam = _make_camera(coord)

    send_message = MagicMock()
    with patch(
        "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
        new=AsyncMock(return_value=None),
    ) as mock_super:
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)


@pytest.mark.asyncio
async def test_webrtc_offer_prewarm_timeout_still_delegates():
    """If pre-warm never clears within the deadline, _wait_for_prewarm logs a
    warning and returns False — but the offer still delegates to super() so
    HA surfaces its own "no stream" handling rather than silently hanging."""
    coord = _make_coord(
        _stream_warming={CAM_ID},
        get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
    )
    cam = _make_camera(coord)

    send_message = MagicMock()
    with (
        patch(
            "custom_components.bosch_shc_camera.camera.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
            new=AsyncMock(return_value=None),
        ) as mock_super,
    ):
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)
