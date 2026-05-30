"""Tests for event-driven snapshot refresh (v12.2.0).

Covers Path A (live-snap refresh on FCM event arrival) and Path B
(Bosch cloud event image pushed into camera entity cache).

Path A: MOVEMENT/PERSON/etc → cam._async_trigger_image_refresh(delay=0) scheduled.
Path B: alert step-2 imageUrl bytes → cam._cached_image updated, save_snapshot
        awaited, image_entity.async_notify_refreshed called.

Source: feature implementation 2026-05-15.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera.fcm"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"

# A minimal JPEG-like byte sequence that passes save_snapshot's > 100 B size guard.
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x42" * 400  # 404 B — real-looking snapshot
JPEG_BYTES_ALT = b"\xff\xd8\xff\xe0" + b"\x99" * 400  # different content, same length


# ── helpers ─────────────────────────────────────────────────────────────────


def _resp_cm(
    status: int,
    body: bytes = b"",
    content_type: str = "image/jpeg",
    json_data: Any = None,
) -> Any:
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.json = AsyncMock(return_value=json_data if json_data is not None else [])
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _one_event(
    event_id: str = "new-evt",
    event_type: str = "MOVEMENT",
    tags: list[str] | None = None,
    image: str = "",
) -> list[dict[str, Any]]:
    return [
        {
            "id": event_id,
            "eventType": event_type,
            "eventTags": tags or [],
            "timestamp": "2026-05-15T10:00:00Z",
            "imageUrl": image,
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }
    ]


def _make_push_coord(**overrides: Any) -> Any:
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
        _last_event_ids={},
        _alert_sent_ids={},
        _camera_entities={},
        _image_entities={},
        _shc_state_cache={},
        _cached_events={},
        _bg_tasks=set(),
        _hw_version={CAM_ID: "HOME_Eyes_Outdoor"},  # Gen2 Outdoor → delay=0
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_alert_coord(options: dict[str, Any] | None = None, **overrides: Any) -> Any:
    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha-snap"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    base_opts: dict[str, Any] = {
        "alert_notify_service": "notify.test",
        "alert_notify_information": "",
        "alert_notify_screenshot": "",
        "alert_notify_video": "",
        "alert_notify_system": "",
        "alert_save_snapshots": False,
        "alert_delete_after_send": True,
        "mark_events_read": False,
        "enable_smb_upload": False,
        "enable_local_save": False,
        "download_path": "",
    }
    if options:
        base_opts.update(options)

    coord = SimpleNamespace(
        token="tok-alert",
        hass=hass,
        options=base_opts,
        data={
            CAM_ID: {"info": {"title": "Terrasse"}, "events": []},
        },
        _last_event_ids={CAM_ID: "prior-event-id"},
        _camera_entities={},
        _image_entities={},
        _shc_state_cache={},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


async def _run_alert(
    coord: Any,
    event_type: str = "MOVEMENT",
    image_url: str = "",
    clip_url: str = "",
    clip_status: str = "",
    cam_name: str = "Terrasse",
    timestamp: str = "2026-05-15T10:00:00.000Z",
    session_override: Any = None,
) -> None:
    from custom_components.bosch_shc_camera.fcm import async_send_alert

    session = session_override or MagicMock(get=MagicMock(return_value=_resp_cm(404)))
    with patch(f"{MODULE}.async_get_clientsession", return_value=session):
        with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
            with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                    await async_send_alert(
                        coord,
                        cam_name,
                        event_type,
                        timestamp,
                        image_url,
                        clip_url,
                        clip_status,
                    )


# ═══════════════════════════════════════════════════════════════════════════════
# Path A — live-snap refresh on FCM event arrival
# ═══════════════════════════════════════════════════════════════════════════════


class TestPathAMovement:
    """MOVEMENT event → _async_trigger_image_refresh called exactly once."""

    @pytest.mark.asyncio
    async def test_movement_triggers_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="MOVEMENT")
            )
        )

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        # async_create_task must have been called at least once
        coord.hass.async_create_task.assert_called()
        # The cam entity's refresh must have been invoked
        cam_entity._async_trigger_image_refresh.assert_called_once_with(delay=0)


class TestPathAPersonEvent:
    """PERSON event → _async_trigger_image_refresh called exactly once."""

    @pytest.mark.asyncio
    async def test_person_triggers_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        # PERSON via eventTags upgrade path
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200,
                json_data=_one_event("new-evt", event_type="MOVEMENT", tags=["PERSON"]),
            )
        )

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity._async_trigger_image_refresh.assert_called_once_with(delay=0)


class TestPathAGen1Delay:
    """Gen1 camera (e.g. CAMERA_360 Indoor) → delay=1.5 s.

    The per-model event_refresh_delay field gives slower Gen1 hardware time to
    capture the post-trigger frame before snap.jpg is fetched. Without this
    delay the live-snap on Gen1 can return the pre-motion frame.
    """

    @pytest.mark.asyncio
    async def test_gen1_indoor_uses_15s_delay(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
            _hw_version={CAM_ID: "INDOOR"},  # Gen1 360 Innenkamera
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="MOVEMENT")
            )
        )

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity._async_trigger_image_refresh.assert_called_once_with(delay=1.5)


class TestPathAStatusOnlyEvent:
    """Status-only event type → _async_trigger_image_refresh NOT called."""

    @pytest.mark.asyncio
    async def test_trouble_connect_does_not_trigger_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="TROUBLE_CONNECT")
            )
        )

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity._async_trigger_image_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_trouble_disconnect_does_not_trigger_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="TROUBLE_DISCONNECT")
            )
        )

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity._async_trigger_image_refresh.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Path B — Bosch cloud event image pushed into camera entity cache
# ═══════════════════════════════════════════════════════════════════════════════


class TestPathBValidJpeg:
    """Valid JPEG bytes from imageUrl → cache updated, save_snapshot called, notify fired."""

    @pytest.mark.asyncio
    async def test_path_b_updates_cache_and_notifies(self) -> None:
        cam_entity = MagicMock()
        cam_entity._cached_image = None  # no existing cache
        cam_entity._last_image_fetch = float("-inf")

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord(
            _camera_entities={CAM_ID: cam_entity},
            _image_entities={CAM_ID: image_entity},
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        assert cam_entity._cached_image == JPEG_BYTES, (
            "cache must hold the event image bytes"
        )
        mock_save.assert_awaited_once_with(coord.hass, CAM_ID, JPEG_BYTES)
        image_entity.async_notify_refreshed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_path_b_updates_last_image_fetch(self) -> None:
        """_last_image_fetch must be set to current monotonic time."""
        cam_entity = MagicMock()
        cam_entity._cached_image = None
        cam_entity._last_image_fetch = float("-inf")

        coord = _make_alert_coord(
            _camera_entities={CAM_ID: cam_entity},
            _image_entities={},
            _shc_state_cache={},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        before = time.monotonic()
        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock):
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )
        after = time.monotonic()

        fetch_ts: float = cam_entity._last_image_fetch
        assert before <= fetch_ts <= after, (
            f"_last_image_fetch must be updated to current monotonic; "
            f"got {fetch_ts!r}, expected [{before:.3f}, {after:.3f}]"
        )


class TestPathBPrivacyModeBlocked:
    """Privacy mode ON → no cache update, no save, no notify."""

    @pytest.mark.asyncio
    async def test_path_b_blocked_by_privacy_mode(self) -> None:
        cam_entity = MagicMock()
        cam_entity._cached_image = None
        cam_entity._last_image_fetch = float("-inf")

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord(
            _camera_entities={CAM_ID: cam_entity},
            _image_entities={CAM_ID: image_entity},
            _shc_state_cache={CAM_ID: {"privacy_mode": True}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        assert cam_entity._cached_image is None, (
            "cache must NOT be updated when privacy is ON"
        )
        mock_save.assert_not_awaited()
        image_entity.async_notify_refreshed.assert_not_awaited()


class TestPathBDeduplication:
    """Same byte-length in cache → no save, no notify (deduplication)."""

    @pytest.mark.asyncio
    async def test_path_b_skipped_on_identical_length(self) -> None:
        # Pre-fill with same-length bytes (simulates duplicate push)
        existing = b"\xff\xd8\xff\xe0" + b"\xaa" * 400  # same length as JPEG_BYTES
        assert len(existing) == len(JPEG_BYTES)

        cam_entity = MagicMock()
        cam_entity._cached_image = existing
        cam_entity._last_image_fetch = time.monotonic()

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord(
            _camera_entities={CAM_ID: cam_entity},
            _image_entities={CAM_ID: image_entity},
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        mock_save.assert_not_awaited()
        image_entity.async_notify_refreshed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_path_b_fires_when_length_differs(self) -> None:
        """Different length → NOT deduplicated → update fires."""
        short_existing = b"\xff\xd8\xff\xe0" + b"\xaa" * 100  # shorter than JPEG_BYTES
        assert len(short_existing) != len(JPEG_BYTES)

        cam_entity = MagicMock()
        cam_entity._cached_image = short_existing
        cam_entity._last_image_fetch = time.monotonic()

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord(
            _camera_entities={CAM_ID: cam_entity},
            _image_entities={CAM_ID: image_entity},
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        mock_save.assert_awaited_once()
        image_entity.async_notify_refreshed.assert_awaited_once()


class TestPathBNoCameraEntity:
    """No camera entity registered → no error, no crash."""

    @pytest.mark.asyncio
    async def test_path_b_no_camera_entity_is_silent(self) -> None:
        coord = _make_alert_coord(
            _camera_entities={},
            _image_entities={},
            _shc_state_cache={},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            # Must complete without raising
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        mock_save.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════════════════
# Combined A + B lifecycle ordering
# ═══════════════════════════════════════════════════════════════════════════════


class TestPathAandBOrdering:
    """Path A fires on FCM push; Path B fires later when alert pipeline downloads imageUrl."""

    @pytest.mark.asyncio
    async def test_path_a_fires_before_path_b(self) -> None:
        """Verify ordering: FCM push → Path A (immediate), alert pipeline → Path B (delayed).

        We can't observe wall-clock ordering in unit tests, but we can verify:
        - Path A: async_create_task is called during async_handle_fcm_push
        - Path B: save_snapshot is called during async_send_alert (the alert pipeline)
        Both are independent coroutines; the test confirms both fire for a single event.
        """
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        # Set up a cam entity for Path A
        cam_entity = MagicMock()
        cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)
        # Set it up so Path B can also update it (no existing cache → update will fire)
        cam_entity._cached_image = None
        cam_entity._last_image_fetch = float("-inf")

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        task_stub = MagicMock(add_done_callback=MagicMock())

        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
            _image_entities={CAM_ID: image_entity},
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)
        # Alert send must be patchable — replace with no-op so we can test push handler alone
        path_a_fired = False

        original_create_task = coord.hass.async_create_task

        def _track_create_task(coro: Any) -> Any:
            nonlocal path_a_fired
            # If the cam entity refresh was scheduled, Path A fired
            if cam_entity._async_trigger_image_refresh.called:
                path_a_fired = True
            return task_stub

        coord.hass.async_create_task = _track_create_task

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="MOVEMENT")
            )
        )

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                    await async_handle_fcm_push(coord)

        # Path A: refresh must have been called
        cam_entity._async_trigger_image_refresh.assert_called_once_with(delay=0)

        # Path B: simulate the alert pipeline completing with imageUrl bytes
        alert_coord = _make_alert_coord(
            _camera_entities={CAM_ID: cam_entity},
            _image_entities={CAM_ID: image_entity},
            _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        img_session = MagicMock()
        img_session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                alert_coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=img_session,
            )

        # Path B: save_snapshot must have fired
        mock_save.assert_awaited_once_with(alert_coord.hass, CAM_ID, JPEG_BYTES)
        image_entity.async_notify_refreshed.assert_awaited_once()
