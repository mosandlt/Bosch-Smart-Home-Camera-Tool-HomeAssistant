"""Regression test for the play_stream-during-prewarm race.

Observed 2026-05-17 05:16:14 UTC:
    [homeassistant.components.camera] ERROR Error requesting stream:
    camera.bosch_innenbereich does not support play stream service

Root cause:
    __init__.py:2621 sets self._live_connections[cam_id] = result BEFORE the
    LOCAL pre-warm completes. The dict has no `rtspsUrl` key yet (it's set at
    line 2749 only after pre-warm). During this window:

      - async_create_stream()'s gate check (camera.py:490) sees
        _live_connections is populated → skips auto-open path
      - calls super().async_create_stream() → reads stream_source()
      - stream_source() returns None because rtspsUrl is missing (intentional
        per camera.py:519-523)
      - HA core raises HomeAssistantError "does not support play stream service"

Fix: in async_create_stream, if cam_id ∈ _stream_warming, poll-wait until
warming completes (up to cfg.min_total_wait + 5s grace), then delegate to super.
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
async def test_prewarm_in_progress_waits_for_completion():
    """While _stream_warming contains cam_id, async_create_stream must wait
    for warming to complete before delegating to super — otherwise stream_source
    returns None and HA logs 'does not support play stream service'."""
    coord = _make_coord(
        _live_connections={CAM_ID: {"proxyUrl": "https://x/snap.jpg"}},
        _stream_warming={CAM_ID},
    )
    cam = _make_camera(coord)

    fake_stream = object()
    # Schedule background task to finish pre-warm after 0.3s
    async def _finish_prewarm():
        await asyncio.sleep(0.3)
        coord._live_connections[CAM_ID]["rtspsUrl"] = "rtsp://127.0.0.1:36107/x"
        coord._stream_warming.discard(CAM_ID)

    asyncio.create_task(_finish_prewarm())

    with patch(
        "homeassistant.components.camera.Camera.async_create_stream",
        new=AsyncMock(return_value=fake_stream),
    ):
        result = await cam.async_create_stream()

    # super().async_create_stream must have been awaited AFTER warming cleared
    assert result is fake_stream
    assert CAM_ID not in coord._stream_warming
    # And try_live_connection must NOT have been called — connection already
    # existed, we just had to wait for the URL to be populated.
    coord.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_prewarm_timeout_returns_none():
    """If pre-warm never completes within min_total_wait + grace, return None
    rather than blocking forever. Caller (HA) treats None as 'no stream'."""
    coord = _make_coord(
        _live_connections={CAM_ID: {"proxyUrl": "https://x/snap.jpg"}},
        _stream_warming={CAM_ID},
        # Tight deadline so the test completes quickly: 0 + 5s grace = 5s.
        # We patch asyncio.sleep to short-circuit the wait.
        get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
    )
    cam = _make_camera(coord)

    # Replace sleep with a no-op so the deadline loop exits in microseconds
    # rather than waiting 5 real seconds.
    with patch("custom_components.bosch_shc_camera.camera.asyncio.sleep",
               new=AsyncMock(return_value=None)):
        result = await cam.async_create_stream()

    assert result is None


@pytest.mark.asyncio
async def test_no_warming_delegates_immediately():
    """If pre-warm is not in progress, no waiting — just delegate to super.
    Backwards-compatible with the existing happy path."""
    coord = _make_coord(
        _live_connections={CAM_ID: {"rtspsUrl": "rtsp://127.0.0.1:36107/x"}},
        _stream_warming=set(),  # NOT warming
    )
    cam = _make_camera(coord)

    fake_stream = object()
    with patch(
        "homeassistant.components.camera.Camera.async_create_stream",
        new=AsyncMock(return_value=fake_stream),
    ):
        result = await cam.async_create_stream()

    assert result is fake_stream
    coord.try_live_connection.assert_not_awaited()
