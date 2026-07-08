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
from typing import Any
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


# ── Regression: GitHub issue #40 ────────────────────────────────────────────
#
# "Failed to start WebRTC stream: Camera does not support WebRTC" — introduced
# by the async_handle_async_webrtc_offer() override added above (v14.4.11).
#
# HA core's Camera.__init__ computes _supports_native_async_webrtc=True purely
# from *overriding* the method (identity check vs Camera.async_handle_async_
# webrtc_offer), regardless of what the override does. That flag then makes
# async_refresh_providers() SKIP go2rtc provider detection entirely
# (_webrtc_provider stays None forever), while camera_capabilities()
# unconditionally advertises StreamType.WEB_RTC anyway — so the frontend
# always offers WebRTC, but core's own super().async_handle_async_webrtc_offer
# (called at the end of our override) checks `if self._webrtc_provider`, finds
# None, and raises HomeAssistantError("Camera does not support WebRTC") on
# every single offer, for every camera (Gen1 and Gen2 alike).
#
# Fix: force self._supports_native_async_webrtc = False back in __init__ right
# after Camera.__init__(self) — our override still runs on every offer via
# normal polymorphic dispatch (the flag only gates capability/provider
# bookkeeping, not method dispatch), so this restores go2rtc provider
# detection without losing the pre-warm wait.


def _make_real_camera() -> Any:
    """Construct BoschCamera via its real __init__ (not __new__ bypass) so the
    HA-core Camera.__init__ bookkeeping this bug lives in actually runs."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "CAMERA_EYES",
                    "firmwareVersion": "7.91.56",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "events": [],
                "live": {},
            }
        },
        _live_connections={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_warming=set(),
        _shc_state_cache={},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )
    entry = SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )
    return BoschCamera(coord, CAM_ID, entry)


def test_camera_does_not_report_native_webrtc_support() -> None:
    """Overriding async_handle_async_webrtc_offer() for the pre-warm wait must
    NOT flip HA core's _supports_native_async_webrtc bookkeeping flag — that
    flag must stay False so async_refresh_providers() still runs go2rtc
    provider detection instead of leaving _webrtc_provider permanently None."""
    cam = _make_real_camera()
    assert cam._supports_native_async_webrtc is False


@pytest.mark.asyncio
async def test_webrtc_offer_delegates_to_registered_provider_not_hard_error() -> None:
    """With a go2rtc provider registered, an offer must reach the provider —
    not fall through to core's `raise HomeAssistantError("Camera does not
    support WebRTC")`, which is exactly issue #40's user-visible symptom."""
    cam = _make_real_camera()
    provider = MagicMock()
    provider.async_handle_async_webrtc_offer = AsyncMock(return_value=None)
    cam._webrtc_provider = provider

    send_message = MagicMock()
    await cam.async_handle_async_webrtc_offer("sdp-offer", "session-1", send_message)

    provider.async_handle_async_webrtc_offer.assert_awaited_once_with(
        cam, "sdp-offer", "session-1", send_message
    )
