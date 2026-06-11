"""Cover the asyncio.CancelledError re-raise paths inside
`_async_update_data`'s event-processing branch.

Two re-raise points:
- L1926: `async_mark_events_read(unread_ids)` raises during startup
  bootstrap (first observation of a camera's event list)
- L2003: `async_mark_events_read([newest_id])` raises during the
  "new event since last tick" alert path

Both must propagate CancelledError so HA's coordinator can shut down
cleanly — without the explicit `raise`, the generic `except Exception`
arm below would swallow the cancellation signal and the coordinator
task would refuse to die during config-entry reload.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

from .test_init_sprint_ka import _PATCH_SESSION, _make_coord, _make_resp

_PATCH_CLOUD_SESSION = "custom_components.bosch_shc_camera.async_get_bosch_cloud_session"

CAM_A = "11111111-1111-1111-1111-111111111111"


def _make_session_with_events(
    events_payload: list[dict], cam_list: list[dict] | None = None
):
    """Session that serves a cam list + last_event + events list.

    `events_payload` is the list returned for the /v11/events endpoint.
    """
    cam_list = cam_list or [
        {"id": CAM_A, "title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"}
    ]
    from unittest.mock import MagicMock

    def _get(url, **_kw):
        if "/last_event" in url:
            # Force fall-through to full fetch by returning a different id
            return _make_resp(200, {"id": "stale-id"})
        if "/events" in url:
            return _make_resp(200, events_payload)
        if "feature_flags" in url:
            return _make_resp(200, {})
        if "protocol_support" in url:
            return _make_resp(200, {"state": "SUPPORTED"})
        if "ping" in url:
            return _make_resp(200, {}, text_data="ONLINE")
        # default: cam list
        return _make_resp(200, cam_list)

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


@pytest.mark.asyncio
class TestMarkEventsReadCancellation:
    async def test_startup_mark_read_cancellation_propagates(self):
        """First observation of a cam (no entry in _last_event_ids) → the
        bootstrap mark-read fires. If it raises CancelledError, the
        coordinator must NOT swallow it. Pins __init__.py L1926."""
        coord = _make_coord(
            _first_tick_done=True,
            _last_events=-86400.0,  # stale → do_events=True
            _last_slow=time.monotonic(),  # fresh → do_slow=False
            options={"mark_events_read": True},
        )
        coord.async_mark_events_read = AsyncMock(side_effect=asyncio.CancelledError)
        # Make _check_status return a sensible ONLINE so the events branch
        # is the only thing that can crash.
        coord._check_status = AsyncMock(return_value=(CAM_A, "ONLINE"))

        events = [{"id": "ev-1", "isRead": False}]
        session = _make_session_with_events(events)

        with (
            patch(_PATCH_SESSION, return_value=session),
            patch(_PATCH_CLOUD_SESSION, new=AsyncMock(return_value=session)),
            pytest.raises(asyncio.CancelledError),
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        # async_mark_events_read was invoked exactly once with our unread id.
        coord.async_mark_events_read.assert_awaited_once_with(["ev-1"])

    async def test_new_event_mark_read_cancellation_propagates(self):
        """Subsequent ticks where prev_id exists but `newest_id` is newer
        → the per-event mark-read fires inside the alert path. If it
        raises CancelledError, the coordinator must NOT swallow it.
        Pins __init__.py L2003."""
        coord = _make_coord(
            _first_tick_done=True,
            _last_events=-86400.0,
            _last_slow=time.monotonic(),
            options={"mark_events_read": True},
            # Already seen "old-ev" — incoming "ev-2" is newer.
            _last_event_ids={CAM_A: "old-ev"},
            _camera_entities={},
            _alert_sent_ids={},
        )
        coord._async_send_alert = AsyncMock(return_value=None)
        coord.async_mark_events_read = AsyncMock(side_effect=asyncio.CancelledError)
        coord._check_status = AsyncMock(return_value=(CAM_A, "ONLINE"))

        events = [
            {
                "id": "ev-2",
                "isRead": False,
                "timestamp": "t",
                "imageUrl": "",
                "videoClipUrl": "",
                "videoClipUploadStatus": "",
                "type": "motion",
            }
        ]
        session = _make_session_with_events(events)

        with (
            patch(_PATCH_SESSION, return_value=session),
            patch(_PATCH_CLOUD_SESSION, new=AsyncMock(return_value=session)),
            pytest.raises(asyncio.CancelledError),
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        coord.async_mark_events_read.assert_awaited_once_with(["ev-2"])
