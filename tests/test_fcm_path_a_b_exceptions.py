"""Cover the exception-swallow paths in FCM Path A + Path B.

`async_handle_fcm_push` schedules:
- Path A — live-snap refresh on event arrival (L899-900: warn + swallow)
- Path B — alert step-2 imageUrl bytes pushed into camera cache
  (L1169-1170: warn + swallow)

Both arms catch generic `Exception` so a transient internal bug
(e.g. `get_model_config` raises, or `_cached_image` write fails) doesn't
kill the FCM listener task. These tests pin the swallow behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera.fcm"


def _resp_cm(status: int, json_data: Any = None) -> Any:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else [])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _one_event(
    event_id: str = "new-evt", event_type: str = "MOVEMENT"
) -> list[dict[str, Any]]:
    return [
        {
            "id": event_id,
            "eventType": event_type,
            "eventTags": [],
            "timestamp": "2026-05-15T10:00:00Z",
            "imageUrl": "",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }
    ]


def _make_push_coord(**overrides) -> Any:
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock(
        return_value=MagicMock(add_done_callback=MagicMock())
    )
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-test",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        _last_event_ids={CAM_ID: "old-evt"},
        _alert_sent_ids={},
        _camera_entities={},
        _image_entities={},
        _shc_state_cache={},
        _cached_events={},
        _bg_tasks=set(),
        _hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


@pytest.mark.asyncio
class TestPathAExceptionSwallow:
    async def test_get_model_config_raise_is_swallowed(self):
        """If `get_model_config` raises mid-flight (e.g. unexpected hw
        string), Path A logs a warning and continues — no propagation.
        Pins fcm.py L899-900."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        coord = _make_push_coord(_camera_entities={CAM_ID: cam_entity})

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200,
                json_data=_one_event("new-evt", event_type="MOVEMENT"),
            )
        )

        with (
            patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)),
            patch(
                "custom_components.bosch_shc_camera.models.get_model_config",
                side_effect=RuntimeError("simulated unknown hw"),
            ),
        ):
            # Must NOT raise — the warn-and-continue arm runs.
            await async_handle_fcm_push(coord)


@pytest.mark.asyncio
class TestStopFcmPushCancellation:
    async def test_wait_for_cancellation_propagates(self):
        """If the `asyncio.wait_for(asyncio.gather(...))` block in
        `async_stop_fcm_push` is cancelled during HA shutdown, the
        CancelledError must propagate (not get swallowed by the broad
        `except Exception` arm above). Pins fcm.py L635-636."""
        import asyncio
        import threading

        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        async def _dummy():
            return None

        coord = SimpleNamespace(
            _fcm_lock=threading.Lock(),
            _fcm_client=MagicMock(
                stop=AsyncMock(return_value=None),
                tasks=[_dummy()],
            ),
            _fcm_running=True,
        )

        with patch(
            "asyncio.wait_for", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            with pytest.raises(asyncio.CancelledError):
                await async_stop_fcm_push(coord)


@pytest.mark.asyncio
class TestPathBExceptionSwallow:
    async def test_save_snapshot_raise_is_swallowed(self, tmp_path):
        """If `save_snapshot` raises mid-flight while persisting the
        event-image bytes from the cloud, Path B logs a warning and
        continues — no propagation, no FCM listener crash.
        Pins fcm.py L1169-1170."""
        import asyncio

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        cam_b = MagicMock(_cached_image=None, _last_image_fetch=0.0)
        coord = _make_push_coord(
            _camera_entities={CAM_ID: cam_b},
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )
        coord.data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": []}}
        coord.options = {
            "alert_notify_service": "notify.test",
            "alert_notify_screenshot": "notify.test",
            "alert_save_snapshots": False,
            "alert_delete_after_send": True,
            "enable_smb_upload": False,
            "enable_local_save": False,
            "download_path": "",
        }
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)
        coord.hass.services.async_call = AsyncMock(return_value=None)
        coord.hass.config = MagicMock(config_dir=str(tmp_path))

        # Step 2 fetches image_url. Mock session.get to return JPEG bytes
        # with image content-type so the function reaches the save_snapshot
        # invocation that we want to make raise.
        img_resp = MagicMock()
        img_resp.status = 200
        img_resp.read = AsyncMock(return_value=b"\xff\xd8\xff\xe0" + b"\x99" * 500)
        img_resp.headers = {"Content-Type": "image/jpeg"}
        img_cm = MagicMock()
        img_cm.__aenter__ = AsyncMock(return_value=img_resp)
        img_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=img_cm)

        # Domain must end with .boschsecurity.com for _is_safe_bosch_url.
        image_url = "https://residential.cbs.boschsecurity.com/img.jpg"

        async def _fast_sleep(_secs):
            return None

        with (
            patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)),
            patch(
                f"{MODULE}.save_snapshot",
                new=AsyncMock(side_effect=RuntimeError("disk full")),
            ),
            patch("asyncio.sleep", new=_fast_sleep),
        ):
            # Must NOT raise — Path B's inner try/except swallows the disk error.
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-19T10:00:00Z",
                image_url,
                "",
                "",
                event_id="ev-1",
            )
