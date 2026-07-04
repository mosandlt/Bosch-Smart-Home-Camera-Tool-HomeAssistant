"""Regression test for the teardown↔renewal TLS-proxy race (2026-07-04).

Live incident: Innenbereich (Eyes Indoor II) HA log, 2026-07-04 05:16-05:20 —
HA's stream worker got `404 Not Found` on the old local-proxy session, then
repeated `Connection refused` against a DIFFERENT port/session for 4+ minutes,
with our own `camera.stream.stop() ... timed out after 5s — force-detaching`
warning logged in between. No REMOTE-fallback escalation ever fired; only a
manual HA restart recovered the camera.

Root cause: `_tear_down_live_stream` (idle reaper / external-privacy-detect /
frigate-idle-timeout / REMOTE-lifetime terminator — none of them go through
`try_live_connection`) ran WITHOUT the per-cam stream lock
(`_get_stream_lock`), while `try_live_connection`/`_try_live_connection_inner`
hold that lock across the WHOLE rebuild (PUT /connection + TLS proxy start +
pre-warm + `Stream.update_source()`). An unlocked teardown could interleave
mid-rebuild: the renewal publishes a brand-new proxy port into HA's `Stream`
via `update_source()`, and a beat later the racing teardown closes that same
port and pops `_live_connections[cam_id]` — leaving the new session dead with
zero error-counting (`record_stream_error` only counts when
`_connection_type == "LOCAL"`, which the teardown just cleared) and therefore
no LOCAL→REMOTE escalation. Same class of race the 2026-06-01 fix already
closed for the 401-rescue/threshold-escalation path (see
test_rescue_renewal_race.py) — but `_tear_down_live_stream`'s OWN callers were
never given the same lock.

Fix: `_tear_down_live_stream` now runs its entire body under
`self._get_stream_lock(cam_id)`, so it can no longer interleave with an
in-flight `try_live_connection` rebuild — whichever started first runs to
completion before the other begins.

Follow-up (same day, adversarial verification round): locking closed the old
race but opened a narrower one — three callers (idle reaper,
frigate-idle-timeout, REMOTE-lifetime terminator) read stale state, DECIDE to
tear down, then queue a call that can now block on the lock for the whole
duration of a concurrent rebuild. By the time it wakes, the original decision
may no longer apply to whatever session exists now (a fresh, healthy,
unrelated-to-the-original-reason session) — teardown ran unconditionally
regardless. Fixed by adding `expected_generation` to `_tear_down_live_stream`:
callers pass the `_auto_renew_generation` value they observed at decision
time; teardown re-checks it FIRST thing under the lock and no-ops if a newer
rebuild has since superseded the stale trigger.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord():
    """Minimal coordinator stub with everything `_tear_down_live_stream` touches."""
    coord = SimpleNamespace(
        _stream_locks={},
        _auto_renew_generation={CAM_ID: 1},
        _live_connections={
            CAM_ID: {"_connection_type": "LOCAL", "rtspsUrl": "rtsps://old"}
        },
        _user_intent_streams={CAM_ID},
        _live_opened_at={CAM_ID: 100.0},
        _stream_error_count={CAM_ID: 0},
        _stream_error_at={},
        _stream_fell_back={},
        _local_rescue_attempts={},
        _local_rescue_at={},
        _stream_warming=set(),
        _stream_warming_started={},
        _renewal_tasks={},
        _reaper_tasks={},
        _session_idle_since={},
        _camera_entities={},
        _live_stream_entities={},
        _stop_tls_proxy=AsyncMock(),
        _unregister_go2rtc_stream=AsyncMock(),
        _nvr_processes={},
        _nvr_user_intent={},
        stop_recorder=AsyncMock(),
    )
    coord._get_stream_lock = lambda cam_id: coord._stream_locks.setdefault(
        cam_id, asyncio.Lock()
    )
    return coord


@pytest.mark.asyncio
async def test_teardown_waits_for_renewal_holding_the_lock():
    """A concurrent renewal holding the stream lock must block teardown —
    not race it — so teardown can never pop `_live_connections`/close the
    proxy while a rebuild is actively publishing a new session."""
    coord = _make_coord()
    lock = asyncio.Lock()
    coord._stream_locks[CAM_ID] = lock
    coord._get_stream_lock = lambda cam_id: coord._stream_locks[cam_id]

    await lock.acquire()  # simulate an in-flight renewal holding the lock

    task = asyncio.ensure_future(
        BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
    )
    await asyncio.sleep(0)  # let it reach `async with lock` and block

    assert not task.done(), (
        "REGRESSION: teardown must block on the stream lock while a renewal "
        "holds it, not run concurrently."
    )
    assert CAM_ID in coord._live_connections, (
        "REGRESSION: teardown popped _live_connections while the lock was "
        "still held by a concurrent renewal — this is the exact race that "
        "left the Innenbereich camera stuck on 2026-07-04."
    )
    coord._stop_tls_proxy.assert_not_awaited()

    lock.release()  # renewal finished
    await task

    assert CAM_ID not in coord._live_connections
    coord._stop_tls_proxy.assert_awaited_once_with(CAM_ID)
    coord._unregister_go2rtc_stream.assert_awaited_once_with(CAM_ID)


@pytest.mark.asyncio
async def test_lock_spans_the_awaited_cleanup_not_just_the_pops():
    """The lock must stay held across `_stop_tls_proxy`'s await, not just the
    synchronous dict pops at the top — otherwise a concurrent rebuild could
    still slip in between the pop and the proxy-stop."""
    coord = _make_coord()
    lock = asyncio.Lock()
    coord._stream_locks[CAM_ID] = lock
    coord._get_stream_lock = lambda cam_id: coord._stream_locks[cam_id]

    reached_proxy_stop = asyncio.Event()
    release_proxy_stop = asyncio.Event()

    async def _slow_stop_tls_proxy(cam_id):
        reached_proxy_stop.set()
        await release_proxy_stop.wait()

    coord._stop_tls_proxy = AsyncMock(side_effect=_slow_stop_tls_proxy)

    task = asyncio.ensure_future(
        BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
    )
    await reached_proxy_stop.wait()  # teardown is now inside _stop_tls_proxy's await

    assert lock.locked(), (
        "REGRESSION: the stream lock was released before the awaited "
        "_stop_tls_proxy cleanup finished — a concurrent try_live_connection "
        "could acquire it and race the in-progress teardown."
    )

    release_proxy_stop.set()
    await task
    assert not lock.locked()


@pytest.mark.asyncio
async def test_teardown_skips_when_session_generation_changed_since_decision():
    """A stale-intent caller (idle reaper / frigate-idle / REMOTE terminator)
    read state, decided to tear down, then queued a call that blocked on the
    lock while a concurrent rebuild ran to completion and bumped the
    generation. By the time teardown gets the lock, the ORIGINAL reason no
    longer applies to whatever session exists now — it must no-op rather
    than destroy the fresh, healthy, unrelated session."""
    coord = _make_coord()
    coord._auto_renew_generation[CAM_ID] = 2  # a rebuild already superseded gen=1
    coord._live_connections[CAM_ID] = {
        "_connection_type": "LOCAL",
        "rtspsUrl": "rtsps://fresh-healthy-session",
    }

    await BoschCameraCoordinator._tear_down_live_stream(
        coord, CAM_ID, expected_generation=1
    )

    assert CAM_ID in coord._live_connections, (
        "REGRESSION: teardown destroyed a session from a newer generation "
        "than the stale trigger observed — it must no-op instead."
    )
    coord._stop_tls_proxy.assert_not_awaited()
    coord._unregister_go2rtc_stream.assert_not_awaited()


@pytest.mark.asyncio
async def test_teardown_proceeds_when_generation_matches():
    """Sanity check: the generation guard doesn't just always skip — it
    tears down normally when the expected generation still matches."""
    coord = _make_coord()
    await BoschCameraCoordinator._tear_down_live_stream(
        coord, CAM_ID, expected_generation=1
    )
    assert CAM_ID not in coord._live_connections
    coord._stop_tls_proxy.assert_awaited_once_with(CAM_ID)
