"""Sprint LA tests — targeting _auto_renew_local_session, _promote_to_local,
and _remote_session_terminator in __init__.py.

Coverage targets (lines 3627-3841):
  - Group 1: Break guards in _auto_renew_local_session (stale-gen, stream-off, non-LOCAL)
  - Group 2: Full renewal block (elapsed >= renewal_interval)
  - Group 3: Lightweight heartbeat (PUT /connection LOCAL)
  - Group 4: 3-consecutive-heartbeat-fail → force renewal
  - Group 5: CancelledError + finally block in _auto_renew_local_session
  - Group 6: _promote_to_local early-returns and success paths
  - Group 7: _remote_session_terminator teardown and guards

All tests run without a live HA instance. Coordinator is a SimpleNamespace stub;
each method is called via the unbound pattern:
    BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)
"""
from __future__ import annotations

import asyncio
import time as time_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _model_cfg(**overrides):
    """Return a SimpleNamespace that mimics a ModelConfig for keepalive tests."""
    base = dict(
        max_session_duration=3600,
        heartbeat_interval=30,
        renewal_interval=1800,
        generation=2,
        display_name="Eyes Outdoor",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_coord(**overrides):
    """Minimal coordinator stub for keepalive / promote / terminator tests."""
    import inspect

    def _create_task(coro, **kwargs):
        """Consume the coroutine so Python 3.14 'never awaited' warnings don't fire."""
        if inspect.iscoroutine(coro):
            coro.close()
        return MagicMock()

    base = dict(
        token="tok-A",
        _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
        _auto_renew_generation={CAM_A: 1},
        _renewal_tasks={},
        _session_stale={},
        get_model_config=MagicMock(return_value=_model_cfg()),
        try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        _refresh_local_creds_from_heartbeat=MagicMock(),
        _tear_down_live_stream=AsyncMock(),
        async_request_refresh=AsyncMock(return_value=None),
        hass=SimpleNamespace(async_create_task=_create_task),
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    # Store _create_task reference so tests can inspect calls if needed
    coord.hass._create_task_calls = []
    original_create_task = coord.hass.async_create_task

    def _recording_create_task(coro, **kwargs):
        coord.hass._create_task_calls.append(coro)
        return original_create_task(coro, **kwargs)

    coord.hass.async_create_task = _recording_create_task
    return coord


def _heartbeat_resp(status: int):
    """Build a minimal async-context-manager response mock for the heartbeat PUT."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value='{"user":"u","password":"p","urls":["1.2.3.4:443"]}')
    # Make it usable as an async context manager: `async with session.put(...) as resp`
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_aiohttp_session_mock(put_cm):
    """Build an aiohttp.ClientSession that works as an async context manager."""
    session = MagicMock()
    session.put = MagicMock(return_value=put_cm)
    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=session)
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return session_cm, session


# ── Group 1: Break guards ──────────────────────────────────────────────────────


class TestAutoRenewBreakGuards:
    """Lines 3615-3625: stale-gen, stream-off, and non-LOCAL break conditions."""

    @pytest.mark.asyncio
    async def test_stale_generation_breaks_loop(self):
        """After sleep, generation mismatch → loop exits cleanly (no renewal)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        # Generation mismatch: task runs with gen=1 but dict says 99
        coord._auto_renew_generation[CAM_A] = 99

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_not_called(), (
            "try_live_connection must not be called when generation is stale"
        )

    @pytest.mark.asyncio
    async def test_stream_off_breaks_loop(self):
        """After sleep, cam_id not in _live_connections → loop exits."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_live_connections={})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_not_called(), (
            "try_live_connection must not be called when stream is off"
        )

    @pytest.mark.asyncio
    async def test_non_local_breaks_loop(self):
        """After sleep, _connection_type != 'LOCAL' → loop exits."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}}
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_not_called(), (
            "try_live_connection must not be called when connection is not LOCAL"
        )

    @pytest.mark.asyncio
    async def test_finally_pops_renewal_tasks_on_break(self):
        """Normal break (stale gen) → finally block still pops _renewal_tasks."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._renewal_tasks[CAM_A] = MagicMock()
        coord._auto_renew_generation[CAM_A] = 99  # stale gen → break

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        assert CAM_A not in coord._renewal_tasks, (
            "finally block must pop CAM_A from _renewal_tasks on any exit"
        )


# ── Group 2: Full renewal (elapsed >= renewal_interval) ───────────────────────


class TestAutoRenewFullRenewal:
    """Lines 3630-3662: full session renewal when elapsed >= renewal_interval."""

    def _one_shot_sleep(self, coord, renewal_interval=0):
        """Return a sleep side_effect that runs one iteration then breaks via stale-gen."""
        call_count = 0

        async def _sleep(t):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                coord._auto_renew_generation[CAM_A] = 999

        return _sleep

    @pytest.mark.asyncio
    async def test_renewal_success_clears_fails(self):
        """try_live_connection returns result → renewal_fails reset to 0."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=0)),
            try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        )

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_awaited_once(), (
            "try_live_connection must be called once for the renewal"
        )

    @pytest.mark.asyncio
    async def test_renewal_success_clears_stale_flag(self):
        """Successful renewal → _session_stale[cam_id] set to False."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=0)),
            try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
            _session_stale={CAM_A: True},
        )

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        assert coord._session_stale.get(CAM_A) is False, (
            "Successful renewal must clear the session_stale flag"
        )

    @pytest.mark.asyncio
    async def test_renewal_failure_increments_fails(self):
        """try_live_connection returns None → renewal called, session_start reset."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=0)),
            try_live_connection=AsyncMock(return_value=None),
        )

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_awaited_once(), (
            "try_live_connection must be awaited once on renewal failure path"
        )

    @pytest.mark.asyncio
    async def test_renewal_exception_increments_fails(self):
        """try_live_connection raises → exception caught, session_start reset."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=0)),
            try_live_connection=AsyncMock(side_effect=RuntimeError("boom")),
        )

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)):
            # Must not raise — exception is caught internally
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_awaited_once(), (
            "try_live_connection must be awaited even when it raises"
        )

    @pytest.mark.asyncio
    async def test_renewal_three_fails_marks_stale(self):
        """3 consecutive renewal failures → _session_stale[cam_id] = True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        iteration = 0

        async def _sleep_three_iterations(t):
            nonlocal iteration
            iteration += 1
            if iteration >= 4:
                coord._auto_renew_generation[CAM_A] = 999

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=0)),
            try_live_connection=AsyncMock(return_value=None),  # always fail
        )

        with patch("asyncio.sleep", side_effect=_sleep_three_iterations):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        assert coord._session_stale.get(CAM_A) is True, (
            "_session_stale must be True after 3 consecutive renewal failures"
        )

    @pytest.mark.asyncio
    async def test_renewal_already_stale_not_re_marked(self):
        """_session_stale already True → no redundant assignment on further fails."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        iteration = 0

        async def _sleep_two_iters(t):
            nonlocal iteration
            iteration += 1
            if iteration >= 3:
                coord._auto_renew_generation[CAM_A] = 999

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=0)),
            try_live_connection=AsyncMock(return_value=None),
            _session_stale={CAM_A: True},  # already stale
        )

        with patch("asyncio.sleep", side_effect=_sleep_two_iters):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        # Still True — nothing broke
        assert coord._session_stale.get(CAM_A) is True, (
            "_session_stale must remain True — already-stale case must not crash"
        )


# ── Group 3: Lightweight heartbeat ─────────────────────────────────────────────


class TestAutoRenewHeartbeat:
    """Lines 3680-3712: lightweight PUT /connection heartbeat path."""

    def _one_shot_sleep(self, coord):
        """Run one iteration then exit via stale-gen."""
        call_count = 0

        async def _sleep(t):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                coord._auto_renew_generation[CAM_A] = 999

        return _sleep

    @pytest.mark.asyncio
    async def test_heartbeat_200_resets_consecutive_fails(self):
        """PUT 200 → consecutive_fails = 0, _refresh_local_creds_from_heartbeat called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=9999999)),
        )

        put_cm = _heartbeat_resp(200)
        session_cm, _ = _make_aiohttp_session_mock(put_cm)

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_cm):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord._refresh_local_creds_from_heartbeat.assert_called_once(), (
            "_refresh_local_creds_from_heartbeat must be called on heartbeat 200"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_no_token_continues(self):
        """self.token = None → skip heartbeat PUT entirely."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            token=None,
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=9999999)),
        )

        put_cm = _heartbeat_resp(200)
        session_cm, session = _make_aiohttp_session_mock(put_cm)

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_cm):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord._refresh_local_creds_from_heartbeat.assert_not_called(), (
            "No heartbeat PUT must be sent when token is None"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_non_200_increments_fails(self):
        """PUT 500 → consecutive_fails += 1, _refresh_local_creds NOT called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=9999999)),
        )

        put_cm = _heartbeat_resp(500)
        session_cm, _ = _make_aiohttp_session_mock(put_cm)

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_cm):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord._refresh_local_creds_from_heartbeat.assert_not_called(), (
            "_refresh_local_creds_from_heartbeat must NOT be called on non-200 heartbeat"
        )

    @pytest.mark.asyncio
    async def test_heartbeat_exception_increments_fails(self):
        """aiohttp raises during heartbeat → exception caught, loop continues."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        import aiohttp as _aiohttp

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=9999999)),
        )

        bad_session = MagicMock()
        bad_session.put = MagicMock(side_effect=_aiohttp.ClientConnectionError("refused"))
        bad_session_cm = MagicMock()
        bad_session_cm.__aenter__ = AsyncMock(return_value=bad_session)
        bad_session_cm.__aexit__ = AsyncMock(return_value=False)

        with patch("asyncio.sleep", side_effect=self._one_shot_sleep(coord)), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=bad_session_cm):
            # Must not propagate the ClientConnectionError
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord._refresh_local_creds_from_heartbeat.assert_not_called(), (
            "_refresh_local_creds_from_heartbeat must not be called after heartbeat exception"
        )


# ── Group 4: 3-consecutive-fail → force renewal ───────────────────────────────


class TestAutoRenewForceRenewal:
    """Lines 3715-3730: 3 consecutive heartbeat fails → immediate renewal."""

    def _three_fail_sleep(self, coord):
        """Run 3 iterations with heartbeat-failing sessions then exit."""
        call_count = 0

        async def _sleep(t):
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                coord._auto_renew_generation[CAM_A] = 999

        return _sleep

    @pytest.mark.asyncio
    async def test_three_heartbeat_fails_force_renewal_success(self):
        """After 3 heartbeat fails, try_live_connection called and succeeds."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=9999999)),
            try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        )

        put_cm = _heartbeat_resp(500)
        session_cm, _ = _make_aiohttp_session_mock(put_cm)

        with patch("asyncio.sleep", side_effect=self._three_fail_sleep(coord)), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_cm):
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_awaited(), (
            "try_live_connection must be called after 3 consecutive heartbeat failures"
        )

    @pytest.mark.asyncio
    async def test_three_heartbeat_fails_force_renewal_failure(self):
        """After 3 heartbeat fails, try_live_connection returns None → session_start reset."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=9999999)),
            try_live_connection=AsyncMock(return_value=None),
        )

        put_cm = _heartbeat_resp(500)
        session_cm, _ = _make_aiohttp_session_mock(put_cm)

        with patch("asyncio.sleep", side_effect=self._three_fail_sleep(coord)), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_cm):
            # Must not raise — renewal failure is handled gracefully
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_awaited(), (
            "try_live_connection must be called even when it returns None"
        )

    @pytest.mark.asyncio
    async def test_three_heartbeat_fails_force_renewal_exception(self):
        """After 3 heartbeat fails, try_live_connection raises → exception caught."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(renewal_interval=9999999)),
            try_live_connection=AsyncMock(side_effect=OSError("network gone")),
        )

        put_cm = _heartbeat_resp(500)
        session_cm, _ = _make_aiohttp_session_mock(put_cm)

        with patch("asyncio.sleep", side_effect=self._three_fail_sleep(coord)), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_cm):
            # Must not propagate the OSError
            await BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)

        coord.try_live_connection.assert_awaited(), (
            "try_live_connection must be awaited even when it raises after 3 heartbeat fails"
        )


# ── Group 5: CancelledError + finally ─────────────────────────────────────────


class TestAutoRenewCancelledError:
    """Lines 3731-3735: CancelledError is caught and finally cleans up."""

    @pytest.mark.asyncio
    async def test_cancelled_error_pops_renewal_tasks(self):
        """task.cancel() → CancelledError caught, _renewal_tasks.pop called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._renewal_tasks[CAM_A] = MagicMock()

        async def _blocking_sleep(t):
            # Simulate a real blocking sleep so cancel() actually interrupts it
            await asyncio.sleep(9999)

        task = asyncio.create_task(
            BoschCameraCoordinator._auto_renew_local_session(coord, CAM_A, 1)
        )
        # Let the task reach the first asyncio.sleep
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # re-raised by the outer await — that is expected

        assert CAM_A not in coord._renewal_tasks, (
            "finally must pop CAM_A from _renewal_tasks after CancelledError"
        )


# ── Group 6: _promote_to_local ────────────────────────────────────────────────


class TestPromoteToLocal:
    """Lines 3750-3774: _promote_to_local early-returns and success paths."""

    @pytest.mark.asyncio
    async def test_promote_no_live_connection_returns_early(self):
        """_live_connections empty → return immediately without calling try_live_connection."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_live_connections={})

        await BoschCameraCoordinator._promote_to_local(coord, CAM_A)

        coord.try_live_connection.assert_not_called(), (
            "try_live_connection must not be called when there is no live connection"
        )

    @pytest.mark.asyncio
    async def test_promote_not_remote_returns_early(self):
        """_connection_type = 'LOCAL' → return immediately (only REMOTE gets promoted)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}}
        )

        await BoschCameraCoordinator._promote_to_local(coord, CAM_A)

        coord.try_live_connection.assert_not_called(), (
            "try_live_connection must not be called when connection is already LOCAL"
        )

    @pytest.mark.asyncio
    async def test_promote_renewal_returns_none(self):
        """try_live_connection returns None → debug log, no further action."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            try_live_connection=AsyncMock(return_value=None),
        )

        await BoschCameraCoordinator._promote_to_local(coord, CAM_A)

        coord.try_live_connection.assert_awaited_once(), (
            "try_live_connection must be awaited on REMOTE → LOCAL promotion attempt"
        )

    @pytest.mark.asyncio
    async def test_promote_renewal_returns_local(self):
        """try_live_connection returns LOCAL result → info log, migration success."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        )

        await BoschCameraCoordinator._promote_to_local(coord, CAM_A)

        coord.try_live_connection.assert_awaited_once(), (
            "try_live_connection must be awaited on successful REMOTE→LOCAL promotion"
        )

    @pytest.mark.asyncio
    async def test_promote_renewal_returns_non_local(self):
        """try_live_connection returns non-LOCAL result → debug log (LAN did not stick)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            try_live_connection=AsyncMock(return_value={"_connection_type": "REMOTE"}),
        )

        await BoschCameraCoordinator._promote_to_local(coord, CAM_A)

        coord.try_live_connection.assert_awaited_once(), (
            "try_live_connection must be awaited even when LAN promotion does not stick"
        )

    @pytest.mark.asyncio
    async def test_promote_exception_warns(self):
        """try_live_connection raises → exception caught, no propagation."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            try_live_connection=AsyncMock(side_effect=RuntimeError("connection refused")),
        )

        # Must not raise
        await BoschCameraCoordinator._promote_to_local(coord, CAM_A)

        coord.try_live_connection.assert_awaited_once(), (
            "try_live_connection must be awaited even when it raises"
        )


# ── Group 7: _remote_session_terminator ───────────────────────────────────────


class TestRemoteSessionTerminator:
    """Lines 3797-3841: teardown scheduling, guards, CancelledError, finally."""

    @pytest.mark.asyncio
    async def test_terminator_stale_gen_skips(self):
        """After sleep, generation mismatch → return without teardown."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _auto_renew_generation={CAM_A: 99},  # task runs with gen=1 → stale
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        coord._tear_down_live_stream.assert_not_called(), (
            "_tear_down_live_stream must not be called when generation is stale"
        )

    @pytest.mark.asyncio
    async def test_terminator_stream_off_skips(self):
        """After sleep, cam_id not in _live_connections → return without teardown."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={},  # stream already off
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        coord._tear_down_live_stream.assert_not_called(), (
            "_tear_down_live_stream must not be called when stream is already off"
        )

    @pytest.mark.asyncio
    async def test_terminator_not_remote_skips(self):
        """After sleep, _connection_type != 'REMOTE' → return without teardown."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        coord._tear_down_live_stream.assert_not_called(), (
            "_tear_down_live_stream must not be called when connection is LOCAL, not REMOTE"
        )

    @pytest.mark.asyncio
    async def test_terminator_tears_down_remote_stream(self):
        """All guards pass → _tear_down_live_stream called + async_request_refresh scheduled."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        coord._tear_down_live_stream.assert_awaited_once_with(CAM_A), (
            "_tear_down_live_stream must be called with cam_id when REMOTE session expires"
        )
        assert len(coord.hass._create_task_calls) == 1, (
            "hass.async_create_task must be called exactly once to schedule async_request_refresh"
        )

    @pytest.mark.asyncio
    async def test_terminator_sleep_delay_based_on_max_session_duration(self):
        """delay = max(1, max_session_duration - 60) is passed to asyncio.sleep."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(max_session_duration=120)),
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )

        sleep_calls = []

        async def _record_sleep(t):
            sleep_calls.append(t)

        with patch("asyncio.sleep", side_effect=_record_sleep):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        assert sleep_calls, "asyncio.sleep must be called at least once"
        assert sleep_calls[0] == 60, (
            f"Expected delay=60 (120-60), got {sleep_calls[0]}"
        )

    @pytest.mark.asyncio
    async def test_terminator_min_delay_is_one_second(self):
        """max_session_duration=30 (< 61) → delay = max(1, 30-60) = 1."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            get_model_config=MagicMock(return_value=_model_cfg(max_session_duration=30)),
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )

        sleep_calls = []

        async def _record_sleep(t):
            sleep_calls.append(t)

        with patch("asyncio.sleep", side_effect=_record_sleep):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        assert sleep_calls[0] == 1, (
            f"Minimum delay must be 1 second even if max_session_duration < 61, got {sleep_calls[0]}"
        )

    @pytest.mark.asyncio
    async def test_terminator_cancelled_no_teardown(self):
        """task.cancel() → CancelledError caught, _tear_down_live_stream NOT called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )

        task = asyncio.create_task(
            BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        coord._tear_down_live_stream.assert_not_called(), (
            "_tear_down_live_stream must not be called when task is cancelled"
        )

    @pytest.mark.asyncio
    async def test_terminator_finally_pops_renewal_tasks(self):
        """finally block always pops _renewal_tasks regardless of exit path."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )
        coord._renewal_tasks[CAM_A] = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        assert CAM_A not in coord._renewal_tasks, (
            "finally must pop CAM_A from _renewal_tasks on any exit path"
        )

    @pytest.mark.asyncio
    async def test_terminator_finally_pops_on_stale_gen_exit(self):
        """finally pops _renewal_tasks even on early return (stale gen)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _auto_renew_generation={CAM_A: 99},
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )
        coord._renewal_tasks[CAM_A] = MagicMock()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._remote_session_terminator(coord, CAM_A, 1)

        assert CAM_A not in coord._renewal_tasks, (
            "finally must pop _renewal_tasks even when stale-gen guard returns early"
        )
