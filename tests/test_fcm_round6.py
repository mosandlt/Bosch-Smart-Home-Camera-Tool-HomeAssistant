"""Sprint E: fcm.py coverage — noise filter, start early-exits, handle push, mark-read.

Covers missing lines in custom_components/bosch_shc_camera/fcm.py:
  Lines 117-122: _install_fcm_noise_filter idempotent branch
  Lines 143-160: async_start_fcm_push early exits
  Lines 385-392: async_handle_fcm_push — no token → return
  Lines 393-558: async_handle_fcm_push — HTTP non-200, empty events, dedup,
                 new event (bus fire + alert task), person upgrade, notification switch
  Lines 941-969: async_mark_events_read — all branches
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
MODULE = "custom_components.bosch_shc_camera.fcm"


# ── shared helpers ────────────────────────────────────────────────────────────

def _resp_cm(status: int, json_data=None, text: str = ""):
    """Return an async context-manager mock for aiohttp session.get / session.put."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_coord(**overrides):
    """Return a minimal coordinator stub for async_handle_fcm_push tests."""
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock()
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-A",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        _last_event_ids={},
        _alert_sent_ids={},
        _camera_entities={},
        _cached_events={},
        _bg_tasks=set(),
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _one_event(event_id="new-event-id", event_type="MOVEMENT", event_tags=None):
    return [{
        "id": event_id,
        "eventType": event_type,
        "eventTags": event_tags or [],
        "timestamp": "2026-05-07T10:00:00Z",
        "imageUrl": "",
        "videoClipUrl": "",
        "videoClipUploadStatus": "",
    }]


# ── 1. _install_fcm_noise_filter — idempotent branch ─────────────────────────

class TestInstallFcmNoiseFilterIdempotent:
    """Lines 117-122: second call must not add a second filter instance."""

    def _get_filter_count(self, logger_name: str) -> int:
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        fcm_logger = logging.getLogger(logger_name)
        return sum(1 for f in fcm_logger.filters if isinstance(f, _FCMNoiseFilter))

    def setup_method(self):
        """Clear any pre-existing filters before each test."""
        fcm_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        fcm_logger.filters = [f for f in fcm_logger.filters
                               if not isinstance(f, _FCMNoiseFilter)]

    def test_first_call_installs_one_filter(self):
        from custom_components.bosch_shc_camera.fcm import _install_fcm_noise_filter
        _install_fcm_noise_filter()
        count = self._get_filter_count("firebase_messaging.fcmpushclient")
        assert count == 1, "first _install_fcm_noise_filter call must add exactly one filter"

    def test_second_call_is_idempotent(self):
        from custom_components.bosch_shc_camera.fcm import _install_fcm_noise_filter
        _install_fcm_noise_filter()
        _install_fcm_noise_filter()
        count = self._get_filter_count("firebase_messaging.fcmpushclient")
        assert count == 1, "second _install_fcm_noise_filter call must not add a duplicate filter"

    def test_many_calls_stay_at_one(self):
        from custom_components.bosch_shc_camera.fcm import _install_fcm_noise_filter
        for _ in range(5):
            _install_fcm_noise_filter()
        count = self._get_filter_count("firebase_messaging.fcmpushclient")
        assert count == 1, "repeated _install_fcm_noise_filter calls must keep exactly one filter"


# ── 2. async_start_fcm_push — early exits ────────────────────────────────────

class TestAsyncStartFcmPushEarlyExits:
    """Lines 143-160: three early-exit branches before Firebase is imported."""

    def _stub(self, **overrides):
        base = dict(
            _fcm_running=False,
            options={"enable_fcm_push": True},
            hass=MagicMock(),
            _entry=SimpleNamespace(data={}),
            data={},
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    @pytest.mark.asyncio
    async def test_already_running_returns_immediately(self):
        """_fcm_running=True → function must return without touching options."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push
        coord = self._stub(_fcm_running=True)
        # options intentionally absent so any options read would KeyError
        del coord.options
        # Must not raise
        await async_start_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_fcm_push_disabled_returns_early(self):
        """enable_fcm_push=False → debug log + return, no Firebase import."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push
        coord = self._stub(options={"enable_fcm_push": False})
        # Ensure firebase_messaging is NOT importable so we'd know if it tried
        with patch.dict(sys.modules, {"firebase_messaging": None}):
            await async_start_fcm_push(coord)
        # No exception = early exit worked
        assert not coord._fcm_running, "FCM must not be marked running after early exit"

    @pytest.mark.asyncio
    async def test_import_error_returns_with_warning(self):
        """firebase_messaging ImportError → log warning + return."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push
        coord = self._stub()

        # Remove firebase_messaging from sys.modules to force ImportError
        saved = sys.modules.pop("firebase_messaging", None)
        # Also block the submodule that FCM tries
        sys.modules["firebase_messaging"] = None  # causes ImportError on 'from ... import'
        try:
            await async_start_fcm_push(coord)
        finally:
            if saved is not None:
                sys.modules["firebase_messaging"] = saved
            else:
                sys.modules.pop("firebase_messaging", None)

        assert not coord._fcm_running, "FCM must not be marked running after ImportError"

    @pytest.mark.asyncio
    async def test_fcm_disabled_default_false(self):
        """options with no 'enable_fcm_push' key → defaults to False → early exit."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push
        coord = self._stub(options={})  # key absent → .get(..., False) = False
        await async_start_fcm_push(coord)
        assert not coord._fcm_running, "missing enable_fcm_push must default to False and exit early"


# ── 3. async_handle_fcm_push — no token ──────────────────────────────────────

class TestAsyncHandleFcmPushNoToken:
    """Lines 385-387: token is falsy → return immediately, no HTTP call."""

    @pytest.mark.asyncio
    async def test_no_token_returns_without_http(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(token="")
        session = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        session.get.assert_not_called(), "no HTTP request must be made when token is empty"

    @pytest.mark.asyncio
    async def test_none_token_returns_without_http(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(token=None)
        session = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        session.get.assert_not_called(), "no HTTP request must be made when token is None"

    @pytest.mark.asyncio
    async def test_no_token_does_not_update_listeners(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(token="")
        await async_handle_fcm_push(coord)
        coord.async_update_listeners.assert_not_called(), \
            "async_update_listeners must not be called when token is absent"


# ── 4. async_handle_fcm_push — HTTP non-200 and empty events ─────────────────

class TestAsyncHandleFcmPushHttpBranches:
    """Lines 397-402: non-200 → continue; empty events list → continue."""

    @pytest.mark.asyncio
    async def test_http_404_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        coord.async_update_listeners.assert_not_called(), \
            "non-200 response must not trigger listener update"

    @pytest.mark.asyncio
    async def test_http_500_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        coord.async_update_listeners.assert_not_called(), \
            "HTTP 500 must not trigger listener update"

    @pytest.mark.asyncio
    async def test_empty_events_list_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[]))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        coord.async_update_listeners.assert_not_called(), \
            "empty events list must not trigger listener update"

    @pytest.mark.asyncio
    async def test_http_401_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(401))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        coord.hass.bus.async_fire.assert_not_called(), \
            "HTTP 401 must not fire any HA events"


# ── 5. async_handle_fcm_push — dedup ─────────────────────────────────────────

class TestAsyncHandleFcmPushDedup:
    """Lines 413-418: newest_id already in _alert_sent_ids within 60s → skip."""

    @pytest.mark.asyncio
    async def test_recent_sent_id_skips_alert(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        recent_ts = time.monotonic() - 10.0  # 10 s ago → within 60 s window
        coord = _make_coord(
            _last_event_ids={CAM_ID: "old-event-id"},
            _alert_sent_ids={"new-event-id": recent_ts},
        )
        events = _one_event("new-event-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        coord.hass.bus.async_fire.assert_not_called(), \
            "event already sent within 60s must be deduped — no bus fire"
        coord.async_update_listeners.assert_not_called(), \
            "event already sent within 60s must be deduped — no listener update"

    @pytest.mark.asyncio
    async def test_stale_sent_id_beyond_60s_not_deduped(self):
        """If the same event_id was sent >60s ago it is NOT deduped (window expired)."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        old_ts = time.monotonic() - 70.0  # 70 s ago → outside 60 s window
        coord = _make_coord(
            _last_event_ids={CAM_ID: "old-event-id"},
            _alert_sent_ids={"new-event-id": old_ts},
        )
        events = _one_event("new-event-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        coord.hass.bus.async_fire.assert_called(), \
            "event sent >60s ago must not be deduped — bus fire expected"

    @pytest.mark.asyncio
    async def test_old_entries_evicted_from_sent_cache(self):
        """_alert_sent_ids entries older than 120s are evicted on each call."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        very_old_ts = time.monotonic() - 130.0
        coord = _make_coord(
            _last_event_ids={CAM_ID: "old-event-id"},
            _alert_sent_ids={"ancient-id": very_old_ts, "new-event-id": very_old_ts - 5},
        )
        events = _one_event("new-event-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        assert "ancient-id" not in coord._alert_sent_ids, \
            "entries older than 120s must be evicted from _alert_sent_ids"


# ── 6. async_handle_fcm_push — new event ─────────────────────────────────────

class TestAsyncHandleFcmPushNewEvent:
    """Lines 430-553: prev_id != newest_id → fire bus + create alert task.
    elif newest_id (prev_id=None) → only update last_event_ids.
    """

    def _coord_with_prev(self, prev_id=None):
        coord = _make_coord(
            _last_event_ids={CAM_ID: prev_id} if prev_id else {},
            options={},
        )
        return coord

    @pytest.mark.asyncio
    async def test_new_event_fires_motion_bus_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_motion" in fired, \
            "MOVEMENT event must fire bosch_shc_camera_motion on the HA bus"

    @pytest.mark.asyncio
    async def test_new_event_creates_alert_task(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        coord.hass.async_create_task.assert_called(), \
            "new event must schedule an async_send_alert task via async_create_task"

    @pytest.mark.asyncio
    async def test_new_event_updates_last_event_id(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        assert coord._last_event_ids[CAM_ID] == "new-id", \
            "_last_event_ids must be updated to newest_id on new event"

    @pytest.mark.asyncio
    async def test_new_event_calls_async_update_listeners(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        coord.async_update_listeners.assert_called_once(), \
            "new event must call async_update_listeners to refresh binary sensors"

    @pytest.mark.asyncio
    async def test_elif_newest_id_only_updates_last_event_id(self):
        """prev_id is None (first push) → elif branch: just store newest_id, no bus fire."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={})  # no prev_id
        events = _one_event("first-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        coord.hass.bus.async_fire.assert_not_called(), \
            "elif branch (no prev_id) must not fire HA events — only record the id"
        assert coord._last_event_ids.get(CAM_ID) == "first-id", \
            "elif branch must store newest_id in _last_event_ids"

    @pytest.mark.asyncio
    async def test_same_event_id_as_prev_does_not_fire(self):
        """newest_id == prev_id → neither if nor elif branch → no bus fire."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "same-id"})
        events = _one_event("same-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_handle_fcm_push(coord)
        coord.hass.bus.async_fire.assert_not_called(), \
            "same event id as prev must not fire HA events"

    @pytest.mark.asyncio
    async def test_audio_alarm_fires_audio_alarm_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id", "AUDIO_ALARM")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_audio_alarm" in fired, \
            "AUDIO_ALARM event must fire bosch_shc_camera_audio_alarm on the HA bus"


# ── 7. async_handle_fcm_push — mark_events_read gate ─────────────────────────

class TestAsyncHandleFcmPushMarkEventsRead:
    """Lines 546-550: mark_events_read option gates the call."""

    @pytest.mark.asyncio
    async def test_mark_events_read_true_calls_mark(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(
            _last_event_ids={CAM_ID: "old-id"},
            options={"mark_events_read": True},
        )
        events = _one_event("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        mock_mark = AsyncMock(return_value=True)
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with patch(f"{MODULE}.async_mark_events_read", mock_mark):
                    await async_handle_fcm_push(coord)
        mock_mark.assert_awaited_once(), \
            "mark_events_read=True must call async_mark_events_read"
        args = mock_mark.call_args.args
        assert "new-id" in args[1], \
            "async_mark_events_read must be called with the new event id"

    @pytest.mark.asyncio
    async def test_mark_events_read_false_skips_mark(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(
            _last_event_ids={CAM_ID: "old-id"},
            options={"mark_events_read": False},
        )
        events = _one_event("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        mock_mark = AsyncMock(return_value=True)
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with patch(f"{MODULE}.async_mark_events_read", mock_mark):
                    await async_handle_fcm_push(coord)
        mock_mark.assert_not_awaited(), \
            "mark_events_read=False must not call async_mark_events_read"

    @pytest.mark.asyncio
    async def test_mark_events_read_absent_skips_mark(self):
        """mark_events_read key absent (default) → option.get returns False → skip."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(
            _last_event_ids={CAM_ID: "old-id"},
            options={},  # key absent
        )
        events = _one_event("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        mock_mark = AsyncMock(return_value=True)
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with patch(f"{MODULE}.async_mark_events_read", mock_mark):
                    await async_handle_fcm_push(coord)
        mock_mark.assert_not_awaited(), \
            "absent mark_events_read must default to False and not call async_mark_events_read"


# ── 8. async_handle_fcm_push — PERSON upgrade ────────────────────────────────

class TestAsyncHandleFcmPushPersonUpgrade:
    """Lines 446-447: MOVEMENT + PERSON tag → upgraded to PERSON event type."""

    @pytest.mark.asyncio
    async def test_movement_with_person_tag_fires_person_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id", "MOVEMENT", ["PERSON"])
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_person" in fired, \
            "MOVEMENT + PERSON tag must fire bosch_shc_camera_person (not motion)"
        assert "bosch_shc_camera_motion" not in fired, \
            "MOVEMENT + PERSON tag must NOT also fire bosch_shc_camera_motion"

    @pytest.mark.asyncio
    async def test_movement_without_person_tag_fires_motion_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id", "MOVEMENT", [])  # no PERSON tag
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_motion" in fired, \
            "MOVEMENT without PERSON tag must fire bosch_shc_camera_motion"
        assert "bosch_shc_camera_person" not in fired, \
            "MOVEMENT without PERSON tag must NOT fire bosch_shc_camera_person"

    @pytest.mark.asyncio
    async def test_pure_person_event_fires_person_event(self):
        """eventType=PERSON (rare, but possible) fires person without upgrade path."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id", "PERSON", [])
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_person" in fired, \
            "eventType=PERSON must fire bosch_shc_camera_person"

    @pytest.mark.asyncio
    async def test_person_tag_on_non_movement_not_upgraded(self):
        """PERSON tag only upgrades MOVEMENT, not AUDIO_ALARM etc."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        events = _one_event("new-id", "AUDIO_ALARM", ["PERSON"])
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_audio_alarm" in fired, \
            "AUDIO_ALARM + PERSON tag must fire audio_alarm (upgrade only for MOVEMENT)"
        assert "bosch_shc_camera_person" not in fired, \
            "AUDIO_ALARM + PERSON tag must not fire person event"


# ── 9. async_handle_fcm_push — notification switch ───────────────────────────

class TestAsyncHandleFcmPushNotificationSwitch:
    """Lines 484-527: master switch OFF → alert blocked → no async_create_task for alert."""

    @pytest.mark.asyncio
    async def test_master_switch_off_blocks_alert(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        master_state = MagicMock()
        master_state.state = "off"
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        # Make states.get return OFF for the master switch
        coord.hass.states.get = MagicMock(return_value=master_state)
        events = _one_event("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock) as mock_alert:
                await async_handle_fcm_push(coord)
        # Bus still fires (event still logged) but no alert task
        mock_alert.assert_not_awaited(), \
            "master notifications switch OFF must prevent async_send_alert call"

    @pytest.mark.asyncio
    async def test_master_switch_on_allows_alert(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        master_state = MagicMock()
        master_state.state = "on"
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        coord.hass.states.get = MagicMock(return_value=master_state)
        events = _one_event("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        coord.hass.async_create_task.assert_called(), \
            "master notifications switch ON must allow async_send_alert task creation"

    @pytest.mark.asyncio
    async def test_no_switch_state_allows_alert(self):
        """states.get returns None (switch not found) → no blocking → alert allowed."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        coord.hass.states.get = MagicMock(return_value=None)  # switch not found
        events = _one_event("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        coord.hass.async_create_task.assert_called(), \
            "absent notification switch must default to allowed (None → not off)"

    @pytest.mark.asyncio
    async def test_type_specific_switch_off_blocks_alert(self):
        """Master ON but type-specific switch OFF → alert blocked."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push
        master_on = MagicMock()
        master_on.state = "on"
        type_off = MagicMock()
        type_off.state = "off"

        def _states_get(eid):
            if "movement_notifications" in eid:
                return type_off
            if "_notifications" in eid:
                return master_on
            return None

        coord = _make_coord(_last_event_ids={CAM_ID: "old-id"})
        coord.hass.states.get = MagicMock(side_effect=_states_get)
        events = _one_event("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=events))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock) as mock_alert:
                await async_handle_fcm_push(coord)
        mock_alert.assert_not_awaited(), \
            "type-specific switch OFF must block alert even when master switch is ON"


# ── 10. async_mark_events_read — all branches ────────────────────────────────

class TestAsyncMarkEventsRead:
    """Lines 941-969: empty list → True, no token → False, HTTP 200 → True, all fail → False."""

    @pytest.mark.asyncio
    async def test_empty_list_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord(token="tok")
        result = await async_mark_events_read(coord, [])
        assert result is True, "empty event_ids list must return True (nothing to do)"

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord(token="")
        result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "no token must return False (cannot authenticate)"

    @pytest.mark.asyncio
    async def test_none_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord(token=None)
        result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "None token must return False"

    @pytest.mark.asyncio
    async def test_http_200_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(200))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is True, "HTTP 200 PUT response must return True"

    @pytest.mark.asyncio
    async def test_http_201_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(201))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is True, "HTTP 201 PUT response must return True"

    @pytest.mark.asyncio
    async def test_http_204_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(204))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is True, "HTTP 204 PUT response must return True"

    @pytest.mark.asyncio
    async def test_http_500_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(500))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "HTTP 500 response must return False (all failed)"

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()
        session = MagicMock()
        session.put = MagicMock(side_effect=Exception("network error"))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "exception during PUT must return False"

    @pytest.mark.asyncio
    async def test_partial_success_returns_true(self):
        """Multiple events: one fails, one succeeds → any success → True."""
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()
        call_count = 0

        @asynccontextmanager
        async def _put(*args, **kw):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status = 200 if call_count == 2 else 500
            yield resp

        session = MagicMock()
        session.put = _put
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await async_mark_events_read(coord, ["fail-id", "ok-id"])
        assert result is True, "partial success (at least one 200) must return True"

    @pytest.mark.asyncio
    async def test_all_fail_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()

        @asynccontextmanager
        async def _put(*args, **kw):
            resp = MagicMock()
            resp.status = 403
            yield resp

        session = MagicMock()
        session.put = _put
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await async_mark_events_read(coord, ["e1", "e2", "e3"])
        assert result is False, "all-fail responses must return False"

    @pytest.mark.asyncio
    async def test_put_sends_correct_payload(self):
        """PUT body must include id + isRead=True."""
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read
        coord = _make_coord()
        captured = {}

        @asynccontextmanager
        async def _put(*args, **kw):
            captured["json"] = kw.get("json", {})
            resp = MagicMock()
            resp.status = 200
            yield resp

        session = MagicMock()
        session.put = _put
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await async_mark_events_read(coord, ["event-xyz"])
        assert captured["json"].get("id") == "event-xyz", \
            "PUT payload must include the event id"
        assert captured["json"].get("isRead") is True, \
            "PUT payload must set isRead=True"
