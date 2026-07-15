"""Tests for event_dispatch.py — per-camera data-dict build + new-event
dispatch (Phase 2 step 5 of the coordinator rewrite). Direct unit tests
in isolation; the existing integration-level tests exercising the full
_async_update_data (test_init.py) already cover end-to-end wiring."""

from __future__ import annotations

import asyncio
import contextlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.event_dispatch import build_data_and_dispatch

CAM_A = "11111111-1111-1111-1111-111111111111"
NOW = 1000.0


def _make_coord(**overrides):
    """SimpleNamespace, NOT MagicMock — the module under test relies on
    `getattr(coordinator, "fcm_last_push", float("-inf"))` defaulting when
    the attribute is absent, which a plain MagicMock defeats (it
    auto-vivifies any attribute access instead of raising AttributeError)."""
    coord = SimpleNamespace(
        cached_status=overrides.pop("cached_status", {}),
        cached_events=overrides.pop("cached_events", {}),
        last_event_ids=overrides.pop("last_event_ids", {}),
        alert_sent_ids=overrides.pop("alert_sent_ids", {}),
        live_connections=overrides.pop("live_connections", {}),
        camera_entities=overrides.pop("camera_entities", {}),
        fcm_lock=contextlib.nullcontext(),
        options=overrides.pop("options", {}),
        hass=MagicMock(),
        async_mark_events_read=AsyncMock(),
        async_send_alert=AsyncMock(),
    )
    # `spawn_tracked` mirrors BoschCameraCoordinator.spawn_tracked closely
    # enough for these direct-module unit tests: routes through
    # hass.async_create_task (already asserted on directly below) instead of
    # needing a real bg_tasks set on this bare SimpleNamespace stub.
    coord.spawn_tracked = lambda coro, **kw: coord.hass.async_create_task(coro, **kw)
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _cam_by_id(cam_id=CAM_A, title="Terrasse"):
    return {cam_id: {"id": cam_id, "title": title}}


class TestNoEventsOrDoEventsFalse:
    @pytest.mark.asyncio
    async def test_do_events_false_still_builds_data(self):
        coord = _make_coord(
            cached_status={CAM_A: "ONLINE"},
            cached_events={CAM_A: [{"id": "ev1"}]},
        )
        data = await build_data_and_dispatch(
            coord, [CAM_A], _cam_by_id(), NOW, do_events=False
        )
        assert data[CAM_A]["status"] == "ONLINE"
        assert data[CAM_A]["events"] == [{"id": "ev1"}]
        coord.hass.bus.async_fire.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_events_builds_data_without_dispatch(self):
        coord = _make_coord(cached_status={CAM_A: "OFFLINE"})
        data = await build_data_and_dispatch(
            coord, [CAM_A], _cam_by_id(), NOW, do_events=True
        )
        assert data[CAM_A]["events"] == []
        coord.hass.bus.async_fire.assert_not_called()


class TestFirstSeenBootstrap:
    @pytest.mark.asyncio
    async def test_prev_id_none_seeds_last_event_ids(self):
        coord = _make_coord(cached_events={CAM_A: [{"id": "ev1", "isRead": True}]})
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.last_event_ids[CAM_A] == "ev1"

    @pytest.mark.asyncio
    async def test_prev_id_none_empty_newest_id_skips_bootstrap(self):
        """Branch coverage: events[0] with no "id" → newest_id="" → the
        bootstrap `if newest_id:` guard must NOT set last_event_ids."""
        coord = _make_coord(cached_events={CAM_A: [{"isRead": True}]})
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert CAM_A not in coord.last_event_ids

    @pytest.mark.asyncio
    async def test_prev_id_none_marks_unread_events_when_configured(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev1", "isRead": False}]},
            options={"mark_events_read": True},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        coord.async_mark_events_read.assert_awaited_once_with(["ev1"])

    @pytest.mark.asyncio
    async def test_prev_id_none_mark_read_disabled_skips_call(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev1", "isRead": False}]},
            options={"mark_events_read": False},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        coord.async_mark_events_read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_prev_id_none_mark_read_exception_swallowed(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev1", "isRead": False}]},
            options={"mark_events_read": True},
        )
        coord.async_mark_events_read = AsyncMock(side_effect=RuntimeError("boom"))
        data = await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.last_event_ids[CAM_A] == "ev1"
        assert data[CAM_A]["info"]["id"] == CAM_A

    @pytest.mark.asyncio
    async def test_prev_id_none_mark_read_cancelled_propagates(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev1", "isRead": False}]},
            options={"mark_events_read": True},
        )
        coord.async_mark_events_read = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)


class TestNewEventDispatch:
    @pytest.mark.asyncio
    async def test_new_event_fires_motion(self):
        coord = _make_coord(
            cached_events={
                CAM_A: [{"id": "ev2", "eventType": "MOVEMENT", "timestamp": "t"}]
            },
            last_event_ids={CAM_A: "ev1"},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        coord.hass.bus.async_fire.assert_called_once()
        assert coord.hass.bus.async_fire.call_args[0][0] == "bosch_shc_camera_motion"
        assert coord.last_event_ids[CAM_A] == "ev2"
        coord.hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_new_event_fires_audio_alarm(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "AUDIO_ALARM"}]},
            last_event_ids={CAM_A: "ev1"},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert (
            coord.hass.bus.async_fire.call_args[0][0] == "bosch_shc_camera_audio_alarm"
        )

    @pytest.mark.asyncio
    async def test_new_event_fires_person(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "PERSON"}]},
            last_event_ids={CAM_A: "ev1"},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.hass.bus.async_fire.call_args[0][0] == "bosch_shc_camera_person"

    @pytest.mark.asyncio
    async def test_movement_with_person_tag_upgrades_to_person(self):
        coord = _make_coord(
            cached_events={
                CAM_A: [
                    {
                        "id": "ev2",
                        "eventType": "MOVEMENT",
                        "eventTags": ["PERSON"],
                    }
                ]
            },
            last_event_ids={CAM_A: "ev1"},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.hass.bus.async_fire.call_args[0][0] == "bosch_shc_camera_person"

    @pytest.mark.asyncio
    async def test_unknown_event_type_fires_no_bus_event(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "SOMETHING_ELSE"}]},
            last_event_ids={CAM_A: "ev1"},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        coord.hass.bus.async_fire.assert_not_called()
        # send_alert is still scheduled regardless of bus-event type
        coord.hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_new_event_triggers_camera_entity_refresh(self):
        entity = MagicMock()
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            camera_entities={CAM_A: entity},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        entity.async_trigger_image_refresh.assert_called_once_with(delay=2)

    @pytest.mark.asyncio
    async def test_no_camera_entity_registered_no_refresh_crash(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
        )
        data = await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert data[CAM_A]["events"][0]["id"] == "ev2"

    @pytest.mark.asyncio
    async def test_new_event_marks_read_when_configured(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            options={"mark_events_read": True},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        coord.async_mark_events_read.assert_awaited_once_with(["ev2"])

    @pytest.mark.asyncio
    async def test_new_event_mark_read_exception_swallowed(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            options={"mark_events_read": True},
        )
        coord.async_mark_events_read = AsyncMock(side_effect=RuntimeError("boom"))
        data = await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert data[CAM_A]["status"] == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_new_event_mark_read_cancelled_propagates(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            options={"mark_events_read": True},
        )
        coord.async_mark_events_read = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)


class TestDedupSkip:
    @pytest.mark.asyncio
    async def test_recent_alert_sent_id_skips_dispatch(self):
        now_mono = time.monotonic()
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            alert_sent_ids={"ev2": now_mono},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        coord.hass.bus.async_fire.assert_not_called()
        # last_event_ids still advances even on dedup skip
        assert coord.last_event_ids[CAM_A] == "ev2"

    @pytest.mark.asyncio
    async def test_alert_sent_ids_pruned_above_64(self):
        now_mono = time.monotonic()
        stale = {f"old{i}": now_mono - 200.0 for i in range(70)}
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            alert_sent_ids=stale,
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert all(k.startswith("old") is False for k in ["ev2"])
        assert "ev2" in coord.alert_sent_ids
        assert len(coord.alert_sent_ids) < 70 + 1


class TestFcmDeliveryDeathWatchdog:
    @pytest.mark.asyncio
    async def test_dead_delivery_flags_unhealthy_and_forces_hard_heal(self):
        now_mono = time.monotonic()
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            options={"enable_fcm_push": True},
            fcm_running=True,
            fcm_healthy=True,
            fcm_last_push=now_mono - 1000.0,
            fcm_started_at=now_mono - 2000.0,
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.fcm_healthy is False
        assert coord.fcm_force_hard_heal is True

    @pytest.mark.asyncio
    async def test_healthy_recent_push_not_flagged(self):
        now_mono = time.monotonic()
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            options={"enable_fcm_push": True},
            fcm_running=True,
            fcm_healthy=True,
            fcm_last_push=now_mono - 5.0,
            fcm_started_at=now_mono - 2000.0,
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.fcm_healthy is True

    @pytest.mark.asyncio
    async def test_fcm_disabled_never_flagged(self):
        now_mono = time.monotonic()
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            options={"enable_fcm_push": False},
            fcm_running=False,
            fcm_healthy=False,
            fcm_last_push=now_mono - 5000.0,
            fcm_started_at=now_mono - 5000.0,
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.fcm_healthy is False  # unchanged, never explicitly re-flagged

    @pytest.mark.asyncio
    async def test_never_pushed_ago_label(self):
        """_last_push stays float('-inf') (never pushed) — covers the "never"
        branch of the _ago log-string ternary."""
        now_mono = time.monotonic()
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev2", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
            options={"enable_fcm_push": True},
            fcm_running=True,
            fcm_healthy=True,
            fcm_last_push=float("-inf"),
            fcm_started_at=now_mono - 2000.0,
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.fcm_healthy is False


class TestUnchangedEventNoOp:
    @pytest.mark.asyncio
    async def test_newest_id_equals_prev_id_no_dispatch(self):
        coord = _make_coord(
            cached_events={CAM_A: [{"id": "ev1", "eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
        )
        await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        coord.hass.bus.async_fire.assert_not_called()
        assert coord.last_event_ids[CAM_A] == "ev1"

    @pytest.mark.asyncio
    async def test_prev_id_set_empty_newest_id_falls_through_no_update(self):
        """Branch coverage: prev_id already known, events[0] lacks "id" (empty
        newest_id) → the final `elif newest_id:` guard must stay False and
        last_event_ids must NOT be touched."""
        coord = _make_coord(
            cached_events={CAM_A: [{"eventType": "MOVEMENT"}]},
            last_event_ids={CAM_A: "ev1"},
        )
        data = await build_data_and_dispatch(coord, [CAM_A], _cam_by_id(), NOW, True)
        assert coord.last_event_ids[CAM_A] == "ev1"
        assert data[CAM_A]["info"]["id"] == CAM_A


class TestDataDictShape:
    @pytest.mark.asyncio
    async def test_data_dict_includes_live_connection(self):
        coord = _make_coord(
            cached_status={CAM_A: "ONLINE"},
            live_connections={CAM_A: {"_connection_type": "LOCAL"}},
        )
        data = await build_data_and_dispatch(
            coord, [CAM_A], _cam_by_id(), NOW, do_events=False
        )
        assert data[CAM_A]["live"] == {"_connection_type": "LOCAL"}

    @pytest.mark.asyncio
    async def test_multiple_cameras_independent(self):
        cam_b = "22222222-2222-2222-2222-222222222222"
        coord = _make_coord(cached_status={CAM_A: "ONLINE", cam_b: "OFFLINE"})
        by_id = {**_cam_by_id(), cam_b: {"id": cam_b, "title": "Innenbereich"}}
        data = await build_data_and_dispatch(
            coord, [CAM_A, cam_b], by_id, NOW, do_events=False
        )
        assert data[CAM_A]["status"] == "ONLINE"
        assert data[cam_b]["status"] == "OFFLINE"
