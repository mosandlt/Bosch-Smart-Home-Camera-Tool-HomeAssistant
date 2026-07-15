"""Tests for camera_status.py — parallel per-camera status-check pass
(Phase 2 step 4 of the coordinator rewrite). Direct unit tests in
isolation; the existing integration-level tests exercising the full
_async_update_data (test_init.py) already cover end-to-end wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.camera_status import (
    _check_one_camera_status,
    poll_statuses,
)

CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-2222-2222-2222-222222222222"
HEADERS = {"Authorization": "Bearer tok", "Accept": "application/json"}
NOW = 1000.0
INTERVAL = 60


def _make_resp(status: int, json_data=None, text_data: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session(url_responses: dict):
    state = {
        k: list(v) if isinstance(v, list) else [v] for k, v in url_responses.items()
    }

    def _get(url, **kwargs):
        for pattern, queue in state.items():
            if pattern in url and queue:
                return queue.pop(0)
        return _make_resp(200, {})

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _make_coord(**overrides):
    def _create_task(coro, **kwargs):
        coro.close()
        return MagicMock()

    def spawn_tracked(coro, **kwargs):
        # Mirrors BoschCameraCoordinator.spawn_tracked closely enough for
        # these direct-module unit tests: routes through
        # hass.async_create_task (assertable via coord.hass.async_create_task)
        # and closes the coroutine to avoid "never awaited" warnings, without
        # needing a real bg_tasks set on this bare SimpleNamespace stub.
        return coord.hass.async_create_task(coro, **kwargs)

    base = dict(
        hass=SimpleNamespace(async_create_task=MagicMock(side_effect=_create_task)),
        spawn_tracked=spawn_tracked,
        should_check_status=MagicMock(return_value=True),
        cached_status={},
        async_local_tcp_ping=AsyncMock(return_value=False),
        per_cam_status_at={},
        offline_since={},
        stream_fell_back={},
        stream_error_count={},
        stream_error_at={},
        live_connections={},
        local_promote_at={},
        commissioned_cache={},
        entry=SimpleNamespace(options={}),
        promote_to_local=AsyncMock(),
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    return coord


class TestCheckOneCameraStatusGating:
    @pytest.mark.asyncio
    async def test_gate_closed_returns_cached_status(self):
        coord = _make_coord(
            should_check_status=MagicMock(return_value=False),
            cached_status={CAM_A: "ONLINE"},
        )
        session = _make_session({})

        cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert cam_id == CAM_A
        assert status == "ONLINE"
        session.get.assert_not_called()
        coord.async_local_tcp_ping.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_closed_unknown_cam_defaults_unknown(self):
        coord = _make_coord(should_check_status=MagicMock(return_value=False))
        session = _make_session({})

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "UNKNOWN"


class TestCheckOneCameraStatusLocalPing:
    @pytest.mark.asyncio
    async def test_local_ping_ok_returns_online_without_cloud_call(self):
        coord = _make_coord(async_local_tcp_ping=AsyncMock(return_value=True))
        session = _make_session({})

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "ONLINE"
        session.get.assert_not_called()
        assert coord.per_cam_status_at[CAM_A] == NOW
        assert CAM_A not in coord.offline_since

    @pytest.mark.asyncio
    async def test_local_ping_ok_no_fallback_flag_skips_promotion_logic(self):
        coord = _make_coord(async_local_tcp_ping=AsyncMock(return_value=True))
        session = _make_session({})

        await _check_one_camera_status(coord, CAM_A, session, HEADERS, NOW, INTERVAL)

        coord.promote_to_local.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_ping_ok_auto_mode_active_remote_promotes(self):
        """Fallback flag set + AUTO mode + an active REMOTE stream + cooldown
        elapsed → schedule a local-promotion task."""
        coord = _make_coord(
            async_local_tcp_ping=AsyncMock(return_value=True),
            stream_fell_back={CAM_A: True},
            entry=SimpleNamespace(options={"stream_connection_type": "auto"}),
            live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            local_promote_at={},
        )
        session = _make_session({})

        await _check_one_camera_status(coord, CAM_A, session, HEADERS, NOW, INTERVAL)

        coord.promote_to_local.assert_called_once_with(CAM_A)
        assert CAM_A not in coord.stream_fell_back
        assert CAM_A not in coord.stream_error_count

    @pytest.mark.asyncio
    async def test_local_ping_ok_auto_mode_cooldown_active_skips_promotion(self):
        coord = _make_coord(
            async_local_tcp_ping=AsyncMock(return_value=True),
            stream_fell_back={CAM_A: True},
            entry=SimpleNamespace(options={"stream_connection_type": "auto"}),
            live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            local_promote_at={CAM_A: NOW - 10},  # promoted 10s ago, cooldown=300s
        )
        session = _make_session({})

        await _check_one_camera_status(coord, CAM_A, session, HEADERS, NOW, INTERVAL)

        coord.promote_to_local.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_ping_ok_manual_mode_clears_flag_no_promotion(self):
        """Fallback flag set but stream_connection_type is NOT 'auto' — must
        not clear the fallback flag or attempt promotion at all."""
        coord = _make_coord(
            async_local_tcp_ping=AsyncMock(return_value=True),
            stream_fell_back={CAM_A: True},
            entry=SimpleNamespace(options={"stream_connection_type": "local"}),
        )
        session = _make_session({})

        await _check_one_camera_status(coord, CAM_A, session, HEADERS, NOW, INTERVAL)

        coord.promote_to_local.assert_not_called()
        assert coord.stream_fell_back[CAM_A] is True

    @pytest.mark.asyncio
    async def test_local_ping_ok_auto_mode_no_active_remote_no_promotion(self):
        """Fallback flag + AUTO mode, but no live_connections entry (no
        active stream at all) — clears the fallback bookkeeping but does
        not attempt promotion (nothing to promote)."""
        coord = _make_coord(
            async_local_tcp_ping=AsyncMock(return_value=True),
            stream_fell_back={CAM_A: True},
            entry=SimpleNamespace(options={"stream_connection_type": "auto"}),
        )
        session = _make_session({})

        await _check_one_camera_status(coord, CAM_A, session, HEADERS, NOW, INTERVAL)

        coord.promote_to_local.assert_not_called()
        assert CAM_A not in coord.stream_fell_back


class TestCheckOneCameraStatusCloudPath:
    @pytest.mark.asyncio
    async def test_ping_200_online(self):
        coord = _make_coord()
        session = _make_session({"/ping": _make_resp(200, text_data='"ONLINE"')})

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "ONLINE"
        assert coord.per_cam_status_at[CAM_A] == NOW

    @pytest.mark.asyncio
    async def test_ping_200_updating_status_mapped(self):
        coord = _make_coord()
        session = _make_session(
            {"/ping": _make_resp(200, text_data='"UPDATING_FIRMWARE"')}
        )

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "UPDATING"

    @pytest.mark.asyncio
    async def test_ping_444_session_limit_fires_quota_handler(self):
        coord = _make_coord(_async_handle_session_quota_hit=AsyncMock())
        session = _make_session({"/ping": _make_resp(444)})

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "SESSION_LIMIT"
        coord.hass.async_create_task.assert_called_once()
        # SESSION_LIMIT must not count as an offline-tracking transition
        assert CAM_A not in coord.offline_since

    @pytest.mark.asyncio
    async def test_ping_444_missing_quota_handler_is_a_noop(self):
        coord = _make_coord()  # no _async_handle_session_quota_hit attribute
        session = _make_session({"/ping": _make_resp(444)})

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "SESSION_LIMIT"
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_ping_error_falls_back_to_commissioned(self):
        coord = _make_coord()
        ping_resp = MagicMock()
        ping_resp.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
        ping_resp.__aexit__ = AsyncMock(return_value=None)
        session = _make_session(
            {
                "/ping": ping_resp,
                "/commissioned": _make_resp(
                    200, {"connected": True, "commissioned": True}
                ),
            }
        )

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "ONLINE"
        assert coord.commissioned_cache[CAM_A] == {
            "connected": True,
            "commissioned": True,
        }

    @pytest.mark.asyncio
    async def test_ping_444_does_not_fall_back_to_commissioned(self):
        """ping_ok=True is set even for SESSION_LIMIT — the commissioned
        fallback must NOT also run (it's a fallback for a FAILED ping, not
        for a quota hit)."""
        coord = _make_coord()
        session = _make_session({"/ping": _make_resp(444)})

        await _check_one_camera_status(coord, CAM_A, session, HEADERS, NOW, INTERVAL)

        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_commissioned_configured_but_not_connected_offline(self):
        coord = _make_coord()
        session = _make_session(
            {
                "/ping": _make_resp(500),
                "/commissioned": _make_resp(200, {"configured": True}),
            }
        )

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "OFFLINE"
        assert coord.offline_since[CAM_A] == NOW

    @pytest.mark.asyncio
    async def test_commissioned_200_neither_connected_nor_configured_stays_unknown(
        self,
    ):
        """A 200 commissioned response that's neither "connected+commissioned"
        nor "configured" must leave status at its UNKNOWN default — found as
        an untested branch (100% line coverage hid it) by a bug-hunt agent."""
        coord = _make_coord()
        session = _make_session(
            {
                "/ping": _make_resp(500),
                "/commissioned": _make_resp(200, {}),
            }
        )

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_commissioned_unexpected_status_code_stays_unknown(self):
        """A commissioned response with a status other than 200/444 (e.g. a
        plain 500) must leave status at UNKNOWN, not raise — found as an
        untested branch (distinct from the exception-based swallow test) by
        a bug-hunt agent."""
        coord = _make_coord()
        session = _make_session(
            {
                "/ping": _make_resp(500),
                "/commissioned": _make_resp(500),
            }
        )

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_offline_since_not_overwritten_on_repeated_offline(self):
        coord = _make_coord(offline_since={CAM_A: NOW - 500})
        session = _make_session(
            {
                "/ping": _make_resp(500),
                "/commissioned": _make_resp(200, {"configured": True}),
            }
        )

        await _check_one_camera_status(coord, CAM_A, session, HEADERS, NOW, INTERVAL)

        assert coord.offline_since[CAM_A] == NOW - 500

    @pytest.mark.asyncio
    async def test_commissioned_444_session_limit(self):
        coord = _make_coord(_async_handle_session_quota_hit=AsyncMock())
        session = _make_session(
            {"/ping": _make_resp(500), "/commissioned": _make_resp(444)}
        )

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "SESSION_LIMIT"

    @pytest.mark.asyncio
    async def test_commissioned_fetch_error_is_swallowed(self):
        coord = _make_coord()
        comm_resp = MagicMock()
        comm_resp.__aenter__ = AsyncMock(side_effect=RuntimeError("boom"))
        comm_resp.__aexit__ = AsyncMock(return_value=None)
        session = _make_session({"/ping": _make_resp(500), "/commissioned": comm_resp})

        _cam_id, status = await _check_one_camera_status(
            coord, CAM_A, session, HEADERS, NOW, INTERVAL
        )

        assert status == "UNKNOWN"


class TestPollStatuses:
    @pytest.mark.asyncio
    async def test_status_cached_for_each_camera(self):
        coord = _make_coord()
        session = _make_session(
            {
                "/ping": [
                    _make_resp(200, text_data='"ONLINE"'),
                    _make_resp(200, text_data='"ONLINE"'),
                ]
            }
        )

        result = await poll_statuses(coord, [CAM_A, CAM_B], session, HEADERS, NOW, {})

        assert result is True
        assert coord.cached_status[CAM_A] == "ONLINE"
        assert coord.cached_status[CAM_B] == "ONLINE"

    @pytest.mark.asyncio
    async def test_any_status_checked_true_even_for_cached_result(self):
        """Per the module's documented semantics: any_status_checked is
        True for every non-exception result, even a gate-skipped one that
        just returned a cached status (NOT only on a fresh fetch)."""
        coord = _make_coord(should_check_status=MagicMock(return_value=False))
        session = _make_session({})

        result = await poll_statuses(coord, [CAM_A], session, HEADERS, NOW, {})

        assert result is True

    @pytest.mark.asyncio
    async def test_one_camera_exception_does_not_abort_others(self):
        coord = _make_coord()

        async def _flaky_check(coordinator, cam_id, session, headers, now, interval):
            if cam_id == CAM_A:
                raise asyncio.CancelledError()
            return (cam_id, "ONLINE")

        import custom_components.bosch_shc_camera.camera_status as cs

        original = cs._check_one_camera_status
        cs._check_one_camera_status = _flaky_check
        try:
            result = await poll_statuses(
                coord, [CAM_A, CAM_B], MagicMock(), HEADERS, NOW, {}
            )
        finally:
            cs._check_one_camera_status = original

        assert result is True
        assert coord.cached_status[CAM_B] == "ONLINE"
        assert CAM_A not in coord.cached_status

    @pytest.mark.asyncio
    async def test_interval_status_read_from_opts_with_default(self):
        coord = _make_coord()
        session = _make_session({})

        await poll_statuses(coord, [CAM_A], session, HEADERS, NOW, {})

        coord.should_check_status.assert_called_once_with(CAM_A, NOW, 60)

    @pytest.mark.asyncio
    async def test_interval_status_custom_value_from_opts(self):
        coord = _make_coord()
        session = _make_session({})

        await poll_statuses(
            coord, [CAM_A], session, HEADERS, NOW, {"interval_status": 120}
        )

        coord.should_check_status.assert_called_once_with(CAM_A, NOW, 120)

    @pytest.mark.asyncio
    async def test_empty_cam_ids_returns_false(self):
        coord = _make_coord()
        session = _make_session({})

        result = await poll_statuses(coord, [], session, HEADERS, NOW, {})

        assert result is False
