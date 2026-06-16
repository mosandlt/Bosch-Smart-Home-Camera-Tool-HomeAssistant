"""Regression: a transient live-snapshot failure on the proactive refresh tick
must NOT replace a good (live) cached frame with a stale event snapshot.

Bug (reported 2026-06-11, privacy OFF): the user saw the current live snapshot,
then "after some time" the card flipped to an ancient image from an old motion
event. Root cause: ``_async_trigger_image_refresh`` fell back to
``async_fetch_fresh_event_snapshot`` whenever the live fetch failed and
overwrote ``_cached_image`` with the "latest event" image — which is days old
when ``last_event`` is frozen (no new motion / FCM stale).

Fix: only seed from the event image on a genuine cold start (no real frame yet —
the 1×1 placeholder does not count); never replace a real live frame with it,
and back off a full interval on a transient failure.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-2222-3333-4444-555555555555"

_LIVE_FRAME = b"\xff\xd8\xff\xe0LIVE-FRAME-CURRENT" + b"\x00" * 64
_OLD_EVENT = b"\xff\xd8\xff\xe0ANCIENT-EVENT-IMAGE" + b"\x11" * 64


def _make_camera(cached: bytes | None):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        _live_connections={},
        _shc_state_cache={},  # privacy OFF
        _image_entities={},
        async_fetch_live_snapshot=AsyncMock(return_value=None),  # live FAILS
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        async_fetch_fresh_event_snapshot=AsyncMock(return_value=_OLD_EVENT),
    )
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._display_name = "Bosch Terrasse"
    cam._cached_image = cached
    cam._force_image_refresh = False
    cam._last_image_fetch = 0.0
    cam._was_streaming = False
    cam._refresh_inflight = (
        False  # synchronous in-flight guard (replaces _refresh_lock)
    )
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(data={}, async_create_task=MagicMock())
    return cam, coord


@pytest.mark.asyncio
async def test_live_failure_keeps_good_frame_not_stale_event() -> None:
    """A real cached live frame survives a failed live fetch — no flip to event."""
    cam, coord = _make_camera(cached=_LIVE_FRAME)

    with patch("custom_components.bosch_shc_camera.camera.save_snapshot", AsyncMock()):
        await cam._async_trigger_image_refresh(delay=0)

    # The good live frame is preserved …
    assert cam._cached_image == _LIVE_FRAME
    # … and the stale event snapshot was never even fetched.
    coord.async_fetch_fresh_event_snapshot.assert_not_awaited()
    # Backoff: _last_image_fetch bumped so it does not hammer every tick.
    assert cam._last_image_fetch > 0.0


@pytest.mark.asyncio
async def test_cold_start_still_seeds_from_event_snapshot() -> None:
    """With only the placeholder (no real frame), seeding from the event is kept."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    cam, coord = _make_camera(cached=BoschCamera._PLACEHOLDER_JPEG)

    with patch("custom_components.bosch_shc_camera.camera.save_snapshot", AsyncMock()):
        await cam._async_trigger_image_refresh(delay=0)

    # Cold start: the event image is used as a seed (better than a black tile).
    coord.async_fetch_fresh_event_snapshot.assert_awaited_once()
    assert cam._cached_image == _OLD_EVENT


@pytest.mark.asyncio
async def test_live_success_updates_frame() -> None:
    """Sanity: when the live fetch succeeds, the live frame is cached as before."""
    cam, coord = _make_camera(cached=_LIVE_FRAME)
    new_live = b"\xff\xd8\xff\xe0NEW-LIVE" + b"\x00" * 32
    coord.async_fetch_live_snapshot = AsyncMock(return_value=new_live)

    with patch("custom_components.bosch_shc_camera.camera.save_snapshot", AsyncMock()):
        await cam._async_trigger_image_refresh(delay=0)

    assert cam._cached_image == new_live
    coord.async_fetch_fresh_event_snapshot.assert_not_awaited()
