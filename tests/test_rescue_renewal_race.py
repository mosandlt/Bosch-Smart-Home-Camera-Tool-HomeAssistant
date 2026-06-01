"""Regression tests for the rescue↔renewal TLS-proxy race (2026-06-01).

Root cause: the 401 rescue (`_handle_stream_worker_error`) and the proxy-died
rebuild (`_on_tls_proxy_died`) tore the old TLS proxy down with an external
`await self._stop_tls_proxy(cam_id)` BEFORE calling `try_live_connection(...)`.
That stop ran OUTSIDE the per-cam stream lock. A concurrent renewal/heartbeat
(`try_live_connection(is_renewal=True)`) holds that lock across the whole
proxy rebuild (PUT /connection + pre-warm, several seconds of awaits). If the
rescue's external stop landed while the renewal held the lock mid-rebuild, it
killed the port the renewal had just started — and the rescue's own
`try_live_connection(is_renewal=False)` then SKIPPED (lock held), leaving
go2rtc + HA Stream pinned to the dead proxy port → frozen image.

Fix: the teardown moved INSIDE `_try_live_connection_inner`, guarded by a new
`force_reset` flag, so the stop-old-proxy + rebuild are atomic under the lock.
The recovery callers pass `force_reset=True`, which also makes the public
`try_live_connection` WAIT for the lock instead of skipping.

These tests pin:
  1. `force_reset=True` tears down (pop live + discard warming + stop proxy)
     at the top of the inner builder.
  2. `force_reset=False` does NOT tear anything down (opportunistic callers).
  3. The lock-skip guard exempts `force_reset` (so recovery waits, not skips).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _inner_coord() -> SimpleNamespace:
    """Minimal stub for `_try_live_connection_inner` up to the token gate.

    `token=None` makes the inner return None immediately AFTER the force_reset
    teardown block, so the test isolates exactly that teardown.
    """
    return SimpleNamespace(
        _live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
        _stream_warming={CAM_ID},
        _stream_warming_started={CAM_ID: 123.0},
        _stop_tls_proxy=AsyncMock(return_value=None),
        token=None,
    )


@pytest.mark.asyncio
async def test_force_reset_tears_down_under_inner():
    """force_reset=True → old session + proxy torn down inside the inner
    (i.e. under the stream lock), before any rebuild work."""
    c = _inner_coord()
    result = await BoschCameraCoordinator._try_live_connection_inner(
        c, CAM_ID, is_renewal=False, force_reset=True
    )
    assert result is None  # no token → returns after teardown
    c._stop_tls_proxy.assert_awaited_once_with(CAM_ID)
    assert CAM_ID not in c._live_connections, "force_reset must drop live session"
    assert CAM_ID not in c._stream_warming, "force_reset must clear warming flag"
    assert CAM_ID not in c._stream_warming_started


@pytest.mark.asyncio
async def test_no_force_reset_no_teardown():
    """force_reset=False → inner does NOT tear down (opportunistic / renewal
    callers manage their own session state)."""
    c = _inner_coord()
    result = await BoschCameraCoordinator._try_live_connection_inner(
        c, CAM_ID, is_renewal=False, force_reset=False
    )
    assert result is None
    c._stop_tls_proxy.assert_not_awaited()
    assert CAM_ID in c._live_connections, (
        "REGRESSION: inner tore down the live session without force_reset — "
        "an opportunistic call would now kill an active stream."
    )
    assert CAM_ID in c._stream_warming


@pytest.mark.asyncio
async def test_force_reset_bypasses_lock_skip_guard():
    """When the stream lock is already held, a force_reset recovery call must
    WAIT for it (not skip) — otherwise the rescue would no-op while a renewal
    holds the lock and the 401 would never be rescued."""
    import asyncio

    lock = asyncio.Lock()
    await lock.acquire()  # simulate a renewal holding the lock

    inner_called = {"n": 0}

    async def _fake_inner(cam_id, is_renewal=False, force_reset=False):
        inner_called["n"] += 1
        return {"_connection_type": "LOCAL"}

    c = SimpleNamespace(
        _shc_state_cache={},
        _get_stream_lock=lambda cid: lock,
        _ensure_go2rtc_schemes_fresh=AsyncMock(),
        _try_live_connection_inner=_fake_inner,
    )

    task = asyncio.ensure_future(
        BoschCameraCoordinator.try_live_connection(c, CAM_ID, force_reset=True)
    )
    await asyncio.sleep(0)  # let it reach `async with lock` and block (not skip)
    assert inner_called["n"] == 0, "force_reset call should be WAITING on the lock"
    assert not task.done(), (
        "REGRESSION: force_reset call returned early (skipped) while the lock "
        "was held — recovery must wait for the lock, never skip."
    )
    lock.release()  # renewal finished
    result = await task
    assert result == {"_connection_type": "LOCAL"}
    assert inner_called["n"] == 1


@pytest.mark.asyncio
async def test_opportunistic_call_still_skips_when_locked():
    """A plain (non-renewal, non-force) call still SKIPS when the lock is held
    — unchanged behavior, pinned so the guard relaxation didn't over-reach."""
    import asyncio

    lock = asyncio.Lock()
    await lock.acquire()

    c = SimpleNamespace(
        _shc_state_cache={},
        _get_stream_lock=lambda cid: lock,
        _ensure_go2rtc_schemes_fresh=AsyncMock(),
        _try_live_connection_inner=AsyncMock(return_value={"x": 1}),
    )
    result = await BoschCameraCoordinator.try_live_connection(c, CAM_ID)
    assert result is None  # skipped
    c._try_live_connection_inner.assert_not_awaited()
    lock.release()
