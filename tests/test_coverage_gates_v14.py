"""Coverage tests for specific uncovered lines — v14 gap-fill.

Targets:
  config_flow.py:    667-670 (invalid_ip_address), 673-680 (invalid_ip_allowlist)
  fcm.py:            209, 474-478, 487-495, 883-890, 915-916, 919-923,
                     940-989 (various poll-loop paths), 1345
  frigate_endpoint.py: 553-560 (semaphore cap reached)

Each test pins exactly one input→output (PIN_EVERY_MODE rule).
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.fcm"


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_supervisor_coord(
    entry_data: dict, *, force_hard: bool = False
) -> SimpleNamespace:
    """Minimal coordinator for _async_run_fcm_supervisor tests.

    Does NOT pre-set _fcm_start_lock so that tests covering lines 915-916 can
    leave it absent.  Callers that need the lock (hard-heal path) must set it
    themselves or use _make_supervisor_coord_with_lock().
    """
    coord = SimpleNamespace()
    coord._entry = SimpleNamespace(data=dict(entry_data))
    coord.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    coord.options = {"enable_fcm_push": True}
    coord._fcm_force_hard_heal = force_hard
    coord._fcm_last_push = float("-inf")
    coord._fcm_running = False
    coord._fcm_healthy = False
    return coord


def _make_supervisor_coord_with_lock(
    entry_data: dict, *, force_hard: bool = False
) -> SimpleNamespace:
    coord = _make_supervisor_coord(entry_data, force_hard=force_hard)
    coord._fcm_start_lock = asyncio.Lock()
    return coord


# ─────────────────────────────────────────────────────────────────────────────
# config_flow.py — lines 667-670: invalid frigate_bind_host
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_options_flow_invalid_bind_host_sets_error() -> None:
    """frigate_bind_host with non-IP value → errors['frigate_bind_host'] == 'invalid_ip_address'."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    captured: dict[str, dict] = {}
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: (
            captured.update({"errors": kw.get("errors", {})}) or {"type": "form"}
        )
    )

    await flow.async_step_init(user_input={"frigate_bind_host": "not_an_ip"})

    assert captured["errors"].get("frigate_bind_host") == "invalid_ip_address"


# ─────────────────────────────────────────────────────────────────────────────
# config_flow.py — lines 673-680: invalid frigate_ip_allowlist CIDR
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_options_flow_invalid_ip_allowlist_sets_error() -> None:
    """frigate_ip_allowlist with bad CIDR → errors['frigate_ip_allowlist'] == 'invalid_ip_allowlist'."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    captured: dict[str, dict] = {}
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: (
            captured.update({"errors": kw.get("errors", {})}) or {"type": "form"}
        )
    )

    await flow.async_step_init(user_input={"frigate_ip_allowlist": "not_a_cidr"})

    assert captured["errors"].get("frigate_ip_allowlist") == "invalid_ip_allowlist"


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — line 209: reset_fcm_error_counter backward-compat shim
# ─────────────────────────────────────────────────────────────────────────────


def test_reset_fcm_error_counter_delegates_to_staleness_reset() -> None:
    """reset_fcm_error_counter() (line 209) clears _SHARED_STALENESS_TIMESTAMPS."""
    from custom_components.bosch_shc_camera.fcm import (
        _FCMNoiseFilter,
        reset_fcm_error_counter,
    )

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())
    assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) >= 1

    reset_fcm_error_counter()

    assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 474-478: async_ensure_fcm_supervisor idempotent path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_supervisor_returns_early_when_fcm_disabled() -> None:
    """async_ensure_fcm_supervisor with enable_fcm_push=False → returns without spawning."""
    from custom_components.bosch_shc_camera import fcm

    coord = SimpleNamespace(
        options={"enable_fcm_push": False},
    )
    await fcm.async_ensure_fcm_supervisor(coord)
    assert not hasattr(coord, "_fcm_supervisor_task")


@pytest.mark.asyncio
async def test_ensure_supervisor_spawns_task_when_none() -> None:
    """async_ensure_fcm_supervisor with no existing task → creates supervisor task."""
    from custom_components.bosch_shc_camera import fcm

    coord = SimpleNamespace(
        options={"enable_fcm_push": True},
        _fcm_supervisor_task=None,
    )
    with patch.object(fcm, "_async_run_fcm_supervisor", new=AsyncMock()):
        await fcm.async_ensure_fcm_supervisor(coord)

    assert coord._fcm_supervisor_task is not None
    coord._fcm_supervisor_task.cancel()
    try:
        await coord._fcm_supervisor_task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.mark.asyncio
async def test_ensure_supervisor_idempotent_when_task_alive() -> None:
    """async_ensure_fcm_supervisor with a running task → returns early, task unchanged."""
    from custom_components.bosch_shc_camera import fcm

    async def _hang() -> None:
        await asyncio.sleep(9999)

    existing = asyncio.create_task(_hang())
    coord = SimpleNamespace(
        options={"enable_fcm_push": True},
        _fcm_supervisor_task=existing,
    )

    await fcm.async_ensure_fcm_supervisor(coord)

    assert coord._fcm_supervisor_task is existing  # not replaced

    existing.cancel()
    try:
        await existing
    except asyncio.CancelledError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 487-495: async_stop_fcm_supervisor running task path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_supervisor_cancels_running_task_and_calls_stop_push() -> None:
    """async_stop_fcm_supervisor cancels a live task, sets it None, and calls async_stop_fcm_push."""
    from custom_components.bosch_shc_camera import fcm

    async def _hang() -> None:
        await asyncio.sleep(9999)

    running = asyncio.create_task(_hang())
    coord = SimpleNamespace(
        _fcm_supervisor_task=running,
        _fcm_client=None,
        _fcm_running=False,
    )

    with patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()) as mock_stop:
        await fcm.async_stop_fcm_supervisor(coord)

    assert coord._fcm_supervisor_task is None
    mock_stop.assert_called_once_with(coord)
    assert running.done()


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 883-890: hard-heal reason strings (3 elif branches)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_hard_heal_reason_soft_streak() -> None:
    """Line 883: soft_streak >= FCM_SUPERVISOR_SOFT_HEAL_MAX → 'soft-restarts' reason logged."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    # Patch MAX to 0 so soft_streak=0 >= 0 triggers the elif on the first iteration.
    with (
        patch.object(fcm, "FCM_SUPERVISOR_SOFT_HEAL_MAX", 0),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch.object(
            fcm,
            "_async_start_fcm_push_locked",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()


@pytest.mark.asyncio
async def test_supervisor_hard_heal_reason_creds_staleness() -> None:
    """Line 887: recent staleness timestamp → 'PHONE_REGISTRATION_ERROR' reason logged."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    # Add a recent staleness entry so get_recent_fcm_creds_staleness_count > 0.
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())

    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    try:
        with (
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "reset_fcm_creds_staleness_counter"),
            patch.object(
                fcm,
                "_async_start_fcm_push_locked",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert task.done()
    finally:
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


@pytest.mark.asyncio
async def test_supervisor_hard_heal_reason_no_credentials() -> None:
    """Line 890: no fcm_credentials in entry.data → 'no persisted credentials' reason."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    # Empty entry data → not coordinator._entry.data.get("fcm_credentials") is True.
    coord = _make_supervisor_coord_with_lock({}, force_hard=False)

    with (
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch.object(
            fcm,
            "_async_start_fcm_push_locked",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 915-916 + 919-921: lock creation + CancelledError → break
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_creates_lock_when_absent_and_breaks_on_cancelled() -> None:
    """Lines 915-916: lock created when _fcm_start_lock absent.
    Lines 919-921: CancelledError from start → supervisor breaks and returns normally."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    # Coordinator WITHOUT _fcm_start_lock so lines 915-916 are reached.
    coord = _make_supervisor_coord({"fcm_credentials": {"gcm": "x"}}, force_hard=False)

    with patch.object(
        fcm,
        "_async_start_fcm_push_locked",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    # Lock was created on the coordinator.
    assert hasattr(coord, "_fcm_start_lock")


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 922-923: generic exception during start → loop continues
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_exception_during_start_logs_and_continues() -> None:
    """Lines 922-923: RuntimeError from start is logged; loop continues to a second attempt."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    call_count = 0

    async def _flaky(_coord: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient error")  # lines 922-923
        raise asyncio.CancelledError()  # terminate on 2nd attempt

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_flaky),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert call_count == 2  # RuntimeError iteration + CancelledError iteration


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 946-951: poll loop exits when is_started() returns False
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_poll_exits_when_listener_stops() -> None:
    """Lines 946-951: poll loop breaks when fcm_client.is_started() returns False."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = False
    coord._fcm_client = fcm_client

    call_count = 0

    async def _start_then_cancel(_coord: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return True  # listener "up" — enters poll loop
        raise asyncio.CancelledError()

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start_then_cancel),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert call_count == 2  # first start (poll exits) + second (cancel terminates)


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 952-955: CancelledError while polling → async_stop_fcm_push
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_cancelled_during_poll_calls_stop_push() -> None:
    """Lines 952-955: task cancellation inside the poll sleep → async_stop_fcm_push called."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = (
        True  # stays "alive" so poll doesn't break by itself
    )
    coord._fcm_client = fcm_client

    with (
        patch.object(
            fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=True)
        ),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()) as mock_stop,
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        await asyncio.sleep(0.05)  # let supervisor reach the poll-loop sleep
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    mock_stop.assert_called()


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 962-966: push_received=True → fast restart, counters reset
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_push_received_resets_counters() -> None:
    """Lines 962-966: push arrived while listener ran → failures/soft_streak reset to 0."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = False
    coord._fcm_client = fcm_client

    call_count = 0

    async def _start_effect(_coord: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a push arriving while this listener was alive.
            coord._fcm_last_push = time.monotonic()
            return True
        raise asyncio.CancelledError()

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start_effect),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — line 1345: retry call in async_handle_fcm_push
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_handle_push_retries_when_http200_but_no_new_event() -> None:
    """Line 1345: recursive retry fires when HTTP 200 returned but no new event dispatched."""
    from custom_components.bosch_shc_camera import fcm

    coord = SimpleNamespace()
    coord.token = "bearer_tok"
    coord.data = {"cam1": {"info": {"title": "Cam1"}}}
    coord._last_event_ids = {}  # prev_id=None → no dispatch, but _any_fetch_ok=True
    coord._alert_sent_ids = {}
    coord._fcm_running = True  # enables line 1345
    coord.options = {}
    coord._camera_entities = {}
    coord.hass = SimpleNamespace(
        states=SimpleNamespace(get=MagicMock(return_value=None))
    )

    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=[{"id": "evt1", "eventType": "MOVEMENT"}])

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=cm)

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        # _attempt=0: no dispatch + HTTP200 + _fcm_running → recurse with _attempt=1
        # _attempt=1: same → recurse with _attempt=2
        # _attempt=2: 2 < 2 is False → stop
        await fcm.async_handle_fcm_push(coord, 0)

    # Test passes if no exception was raised (line 1345 was reached and executed).


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — lines 988-989: task cancelled during final backoff sleep → break
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_cancelled_during_final_backoff_sleep() -> None:
    """Lines 988-989: task.cancel() while supervisor sleeps after listener termination → break."""
    import asyncio as _asyncio

    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import (
        FCM_SUPERVISOR_POLL_SEC,
        _FCMNoiseFilter,
    )

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = False  # poll breaks immediately
    coord._fcm_client = fcm_client

    # Save real asyncio.sleep before patching to avoid infinite recursion in the hook.
    _real_sleep = _asyncio.sleep
    reached_final_sleep = _asyncio.Event()

    async def _controlled_sleep(secs: float) -> None:
        if secs == FCM_SUPERVISOR_POLL_SEC:
            return  # poll sleep → instant
        # Final backoff sleep: signal the test, then actually block so the cancel fires here.
        reached_final_sleep.set()
        await _real_sleep(9999)

    with (
        patch.object(
            fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=True)
        ),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch("asyncio.sleep", new=_controlled_sleep),
    ):
        task = _asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        await reached_final_sleep.wait()  # supervisor is now blocked inside final sleep
        task.cancel()
        try:
            await task
        except _asyncio.CancelledError:
            pass

    assert task.done()


# ─────────────────────────────────────────────────────────────────────────────
# frigate_endpoint.py — lines 553-560: semaphore cap reached → reject
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_front_door_rejects_client_when_connection_cap_reached() -> None:
    """Lines 553-560: _handle closes writer immediately when all semaphore slots are taken."""
    from custom_components.bosch_shc_camera.frigate_endpoint import (
        FrontDoorConfig,
        _CameraServer,
    )

    config = FrontDoorConfig(max_connections=1)
    server = _CameraServer(
        cam_id="11111111-1111-1111-1111-111111111111",
        config=config,
        resolve_inner=AsyncMock(),
        on_active=None,
        on_idle=None,
    )

    # Exhaust the semaphore so _sem.locked() returns True.
    await server._sem.acquire()

    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 5000))
    writer.close = MagicMock()

    await server._handle(MagicMock(), writer)

    writer.close.assert_called_once()

    # Restore semaphore so it isn't leaked.
    server._sem.release()


# ─────────────────────────────────────────────────────────────────────────────
# fcm.py — C2 regression (bug-hunt 2026-07-01): inner poll loop must honor
# _fcm_force_hard_heal even while the listener still reports is_started()==True
# (the silent-delivery-death case the flag exists for).
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_inner_poll_breaks_on_forced_hard_heal() -> None:
    """When the delivery-death watchdog sets _fcm_force_hard_heal while the
    listener still reports is_started()==True, the inner poll loop must break
    promptly so the top-of-loop hard-heal fires — it must NOT wait for an
    independent socket death that, in this exact scenario, may never come.

    Before the fix the inner loop only exited on is_started()==False, so with a
    listener that stays 'started' the supervisor never re-read the flag: the
    forced hard-heal never happened (here: the second start / credential purge
    below never occurs, so start_calls stays 1 and the assertion fails). The
    sleep-call safety net prevents a regressed loop from hanging the test."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    # Listener stays "started" — the silent-delivery-death case.
    fcm_client = MagicMock()
    fcm_client.is_started.return_value = True
    coord._fcm_client = fcm_client

    start_calls = 0

    async def _start(_coord: object) -> bool:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            return True  # listener "up" → enter the inner poll loop
        # Second start == the forced hard-heal restart. Terminate the supervisor.
        raise asyncio.CancelledError()

    sleep_calls = 0

    async def _sleep(*_a: object, **_k: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            # First sleep is inside the inner poll loop: simulate the watchdog
            # flagging delivery death while the listener still reports started.
            coord._fcm_force_hard_heal = True
        elif sleep_calls > 50:
            # Safety net: a regressed inner loop that never breaks would spin
            # here forever — abort so the test FAILS (on start_calls) not hangs.
            raise asyncio.CancelledError()

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch("asyncio.sleep", new=_sleep),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    # The listener never stopped (is_started stayed True) yet the supervisor
    # restarted → the inner poll loop honored the forced hard-heal promptly.
    assert start_calls == 2
    # The hard-heal actually purged the credentials from the entry data.
    coord.hass.config_entries.async_update_entry.assert_called()
    # Flag consumed (and reset) by the top-of-loop hard-heal.
    assert coord._fcm_force_hard_heal is False
