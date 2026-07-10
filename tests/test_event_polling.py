"""Tests for event_polling.py — parallel per-camera events-fetch pass
(Phase 2 step 4 of the coordinator rewrite). Direct unit tests in
isolation; the existing integration-level tests exercising the full
_async_update_data (test_init.py) already cover end-to-end wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.event_polling import (
    _fetch_one_camera_events,
    poll_events,
)

CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-2222-2222-2222-222222222222"
HEADERS = {"Authorization": "Bearer tok", "Accept": "application/json"}


def _make_resp(status: int, json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session(url_responses: dict):
    """Session whose .get() returns a response keyed by URL substring."""
    state = {
        k: list(v) if isinstance(v, list) else [v] for k, v in url_responses.items()
    }

    def _get(url, **kwargs):
        for pattern, queue in state.items():
            if pattern in url and queue:
                return queue.pop(0)
        return _make_resp(200, [])

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _make_coord(**overrides):
    base = dict(
        _last_event_ids={},
        _cached_events={},
        _err_str=staticmethod(lambda err: str(err) or repr(err)),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestFetchOneCameraEvents:
    @pytest.mark.asyncio
    async def test_last_event_unchanged_skips_full_fetch(self):
        """If last_event's id matches the cached id, skip the full fetch
        and return the already-cached events list."""
        coord = _make_coord(
            _last_event_ids={CAM_A: "evt-1"},
            _cached_events={CAM_A: [{"id": "evt-1"}]},
        )
        session = _make_session({"last_event": _make_resp(200, {"id": "evt-1"})})

        _cam_id, events, ok = await _fetch_one_camera_events(
            coord, CAM_A, session, HEADERS
        )

        assert _cam_id == CAM_A
        assert events == [{"id": "evt-1"}]
        assert ok is True
        # Only the last_event URL should have been called, not the full events URL
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_last_event_changed_triggers_full_fetch(self):
        coord = _make_coord(_last_event_ids={CAM_A: "evt-old"})
        session = _make_session(
            {
                "last_event": _make_resp(200, {"id": "evt-new"}),
                "v11/events": _make_resp(200, [{"id": "evt-new"}, {"id": "evt-old"}]),
            }
        )

        _cam_id, events, ok = await _fetch_one_camera_events(
            coord, CAM_A, session, HEADERS
        )

        assert events == [{"id": "evt-new"}, {"id": "evt-old"}]
        assert ok is True
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_no_prior_last_event_id_triggers_full_fetch(self):
        """cam_id absent from _last_event_ids (fresh camera) must fall
        through to the full fetch, not crash on a None comparison."""
        coord = _make_coord()
        session = _make_session(
            {
                "last_event": _make_resp(200, {"id": "evt-1"}),
                "v11/events": _make_resp(200, [{"id": "evt-1"}]),
            }
        )

        _cam_id, _events, ok = await _fetch_one_camera_events(
            coord, CAM_A, session, HEADERS
        )

        assert ok is True
        assert session.get.call_count == 2

    @pytest.mark.asyncio
    async def test_last_event_check_error_falls_back_to_full_fetch(self):
        """A failure in the last_event optimization must not prevent the
        full fetch fallback from running."""
        coord = _make_coord()
        last_event_resp = MagicMock()
        last_event_resp.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
        last_event_resp.__aexit__ = AsyncMock(return_value=None)
        session = _make_session(
            {
                "last_event": last_event_resp,
                "v11/events": _make_resp(200, [{"id": "evt-1"}]),
            }
        )

        _cam_id, events, ok = await _fetch_one_camera_events(
            coord, CAM_A, session, HEADERS
        )

        assert ok is True
        assert events == [{"id": "evt-1"}]

    @pytest.mark.asyncio
    async def test_full_fetch_failure_returns_not_ok_empty_events(self):
        """A transient failure must return ok=False and NOT return stale
        cached events under a different key — caller decides whether to
        keep its own previously-cached events."""
        coord = _make_coord()
        events_resp = MagicMock()
        events_resp.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
        events_resp.__aexit__ = AsyncMock(return_value=None)
        session = _make_session(
            {
                "last_event": _make_resp(500),
                "v11/events": events_resp,
            }
        )

        _cam_id, events, ok = await _fetch_one_camera_events(
            coord, CAM_A, session, HEADERS
        )

        assert ok is False
        assert events == []

    @pytest.mark.asyncio
    async def test_full_fetch_non_200_returns_not_ok(self):
        coord = _make_coord()
        session = _make_session(
            {"last_event": _make_resp(500), "v11/events": _make_resp(503)}
        )

        _cam_id, events, ok = await _fetch_one_camera_events(
            coord, CAM_A, session, HEADERS
        )

        assert ok is False
        assert events == []


class TestPollEvents:
    @pytest.mark.asyncio
    async def test_do_events_false_is_a_noop(self):
        coord = _make_coord()
        session = _make_session({})

        result = await poll_events(coord, [CAM_A], session, HEADERS, do_events=False)

        assert result is False
        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_fetch_caches_events_and_returns_true(self):
        coord = _make_coord()
        session = _make_session(
            {
                "last_event": _make_resp(500),
                "v11/events": _make_resp(200, [{"id": "evt-1"}]),
            }
        )

        result = await poll_events(coord, [CAM_A], session, HEADERS, do_events=True)

        assert result is True
        assert coord._cached_events[CAM_A] == [{"id": "evt-1"}]

    @pytest.mark.asyncio
    async def test_failed_fetch_does_not_overwrite_cache_or_report_fetched(self):
        coord = _make_coord(_cached_events={CAM_A: [{"id": "stale"}]})
        session = _make_session(
            {"last_event": _make_resp(500), "v11/events": _make_resp(503)}
        )

        result = await poll_events(coord, [CAM_A], session, HEADERS, do_events=True)

        assert result is False
        assert coord._cached_events[CAM_A] == [{"id": "stale"}]

    @pytest.mark.asyncio
    async def test_one_camera_exception_does_not_abort_others(self):
        """gather(..., return_exceptions=True) — an exception escaping one
        camera's fetch coroutine must not prevent the other camera's
        result from being processed.

        Uses asyncio.CancelledError, not a generic RuntimeError: a bug-hunt
        agent correctly pointed out that _fetch_one_camera_events's own two
        try/except blocks already catch plain Exception internally, so a
        bare RuntimeError could never actually escape the real function —
        CancelledError (a BaseException, not caught by `except Exception`)
        is the one realistic way an exception reaches this gather at all,
        e.g. HA unloading/reloading the integration mid-poll."""
        coord = _make_coord()

        call_count = [0]

        async def _flaky_fetch(coordinator, cam_id, session, headers):
            call_count[0] += 1
            if cam_id == CAM_A:
                raise asyncio.CancelledError()
            return (cam_id, [{"id": "evt-b"}], True)

        import custom_components.bosch_shc_camera.event_polling as ep

        original = ep._fetch_one_camera_events
        ep._fetch_one_camera_events = _flaky_fetch
        try:
            result = await poll_events(
                coord, [CAM_A, CAM_B], MagicMock(), HEADERS, do_events=True
            )
        finally:
            ep._fetch_one_camera_events = original

        assert result is True
        assert CAM_B in coord._cached_events
        assert CAM_A not in coord._cached_events
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_multiple_cameras_all_cached_independently(self):
        coord = _make_coord()
        session = _make_session(
            {
                "last_event": _make_resp(500),
                "v11/events": [
                    _make_resp(200, [{"id": "evt-a"}]),
                    _make_resp(200, [{"id": "evt-b"}]),
                ],
            }
        )

        result = await poll_events(
            coord, [CAM_A, CAM_B], session, HEADERS, do_events=True
        )

        assert result is True
        assert CAM_A in coord._cached_events
        assert CAM_B in coord._cached_events
