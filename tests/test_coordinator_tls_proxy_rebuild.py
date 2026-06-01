"""Regression tests for BoschCameraCoordinator._on_tls_proxy_died.

Bug 2026-05-18 (Innenbereich WiFi-jitter): TLS proxy circuit breaker closed
the server socket but the coordinator never noticed → stream stayed dead
until manual switch toggle. See test_tls_proxy_died_callback.py for the
proxy-side fix; this module tests the coordinator-side handler.

Pinned behavior of _on_tls_proxy_died(cam_id):
  - Backoff: second invocation within 30s is suppressed (prevents rebuild
    storm if the new proxy also dies immediately because camera still down)
  - Skip if cam_id no longer in _live_connections (stream was turned off)
  - Skip if active connection_type != "LOCAL" (REMOTE has no TLS proxy
    to rebuild; another flow owns recovery)
  - On rebuild, tears down stale state and calls try_live_connection()
  - Uses float('-inf') sentinel for "never rebuilt" (SENTINEL_RULE — CI VMs
    have low monotonic uptime so 0.0 default would race with first real ts)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_A = "AAAA1111-BBBB-2222-CCCC-3333DDDD4444"


def _make_coord(*, live_conn: dict | None = None):
    """Build a minimal coord stub with just what _on_tls_proxy_died touches."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    coord = SimpleNamespace()
    coord._live_connections = {CAM_A: live_conn} if live_conn else {}
    coord._tls_proxy_rebuild_last = {}
    # Warm-up state — _on_tls_proxy_died now clears these too so the privacy
    # toggle stays responsive after the breaker fires (regression 2026-05-19,
    # Innenbereich incident).
    coord._stream_warming = set()
    coord._stream_warming_started = {}
    coord._stop_tls_proxy = AsyncMock(return_value=None)
    coord.try_live_connection = AsyncMock(return_value={"_connection_type": "LOCAL"})
    coord._on_tls_proxy_died = BoschCameraCoordinator._on_tls_proxy_died.__get__(coord)
    return coord


# ── Happy path: rebuild fires after pre-wait ──────────────────────────────


@pytest.mark.asyncio
async def test_rebuild_when_local_stream_active(monkeypatch) -> None:
    """When a LOCAL stream is active and proxy dies, rebuild via try_live_connection."""
    coord = _make_coord(live_conn={"_connection_type": "LOCAL"})

    # Skip the pre-wait (5s) to keep tests fast
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

    await coord._on_tls_proxy_died(CAM_A)

    # Teardown (stop proxy + clear stale _live_connections / warming) now runs
    # INSIDE try_live_connection under the per-cam stream lock, via force_reset.
    # The proxy-died flow therefore makes NO external _stop_tls_proxy call that
    # could race a concurrent renewal (rescue↔renewal proxy race, 2026-06-01),
    # and force_reset=True makes the call WAIT for the lock instead of early-
    # returning on a stale lock.
    coord._stop_tls_proxy.assert_not_awaited()
    coord.try_live_connection.assert_awaited_once_with(CAM_A, force_reset=True)


# ── Skip conditions ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skip_when_no_live_connection(monkeypatch) -> None:
    """No active stream → nothing to rebuild (switch was turned off)."""
    coord = _make_coord(live_conn=None)
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

    await coord._on_tls_proxy_died(CAM_A)

    coord.try_live_connection.assert_not_awaited()
    coord._stop_tls_proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_skip_when_active_connection_is_remote(monkeypatch) -> None:
    """REMOTE stream has no TLS proxy to rebuild — another flow owns recovery."""
    coord = _make_coord(live_conn={"_connection_type": "REMOTE"})
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

    await coord._on_tls_proxy_died(CAM_A)

    coord.try_live_connection.assert_not_awaited()


# ── Backoff: suppress rebuild storm ────────────────────────────────────────


@pytest.mark.asyncio
async def test_backoff_suppresses_second_call_within_window(monkeypatch) -> None:
    """Two callbacks within 30s → only one rebuild fires.

    Prevents storm if the new proxy also immediately dies because camera
    is still flapping. Coordinator can still rebuild on next renewal.
    """
    coord = _make_coord(live_conn={"_connection_type": "LOCAL"})
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

    # First call: rebuild fires
    await coord._on_tls_proxy_died(CAM_A)
    assert coord.try_live_connection.await_count == 1

    # Reset live_connections (rebuild cleared it; simulate try_live_connection re-populating)
    coord._live_connections[CAM_A] = {"_connection_type": "LOCAL"}

    # Second call within window: must be suppressed
    await coord._on_tls_proxy_died(CAM_A)
    assert coord.try_live_connection.await_count == 1, (
        "Second _on_tls_proxy_died within backoff window must NOT trigger "
        "a second rebuild — prevents storm when camera is still down"
    )


@pytest.mark.asyncio
async def test_backoff_allows_second_call_after_window(monkeypatch) -> None:
    """After backoff window elapses, a second rebuild is allowed."""
    coord = _make_coord(live_conn={"_connection_type": "LOCAL"})
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

    await coord._on_tls_proxy_died(CAM_A)
    assert coord.try_live_connection.await_count == 1

    # Simulate elapsed time by rewinding the timestamp past the window
    coord._tls_proxy_rebuild_last[CAM_A] -= 120.0
    coord._live_connections[CAM_A] = {"_connection_type": "LOCAL"}

    await coord._on_tls_proxy_died(CAM_A)
    assert coord.try_live_connection.await_count == 2, (
        "After backoff window, a fresh callback must trigger a rebuild — "
        "otherwise persistent WiFi flapping leaves the stream dead forever"
    )


# ── SENTINEL_RULE: float('-inf') default not 0.0 ──────────────────────────


@pytest.mark.asyncio
async def test_first_call_not_blocked_by_zero_sentinel(monkeypatch) -> None:
    """First-ever rebuild must fire even on a freshly-booted CI VM.

    Bug shape: if default were 0.0 and time.monotonic() < 30s (boot), the
    backoff check `(now - 0.0) < 30` would suppress the very first rebuild.
    Implementation MUST use float('-inf').
    """
    coord = _make_coord(live_conn={"_connection_type": "LOCAL"})
    monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

    # No entry in _tls_proxy_rebuild_last — simulates first-ever call
    assert CAM_A not in coord._tls_proxy_rebuild_last

    await coord._on_tls_proxy_died(CAM_A)
    assert coord.try_live_connection.await_count == 1, (
        "First rebuild must fire regardless of monotonic clock value — "
        "default must be float('-inf'), not 0.0 (SENTINEL_RULE)"
    )


def test_sentinel_uses_float_inf_in_source() -> None:
    """Pin source: float('-inf') sentinel for _tls_proxy_rebuild_last default."""
    from pathlib import Path

    src = (
        Path(__file__).parent.parent
        / "custom_components"
        / "bosch_shc_camera"
        / "__init__.py"
    ).read_text()
    # The .get() call for _tls_proxy_rebuild_last must use float('-inf') default
    assert (
        "_tls_proxy_rebuild_last.get(cam_id, float('-inf'))" in src
        or '_tls_proxy_rebuild_last.get(cam_id, float("-inf"))' in src
    ), (
        "_on_tls_proxy_died must use float('-inf') as 'never rebuilt' sentinel "
        "(SENTINEL_RULE — 0.0 races with fresh-VM monotonic clock)"
    )


# ── _start_tls_proxy wires the callback ───────────────────────────────────


def test_start_tls_proxy_passes_on_proxy_died_callback() -> None:
    """_start_tls_proxy must wire on_proxy_died → _on_tls_proxy_died."""
    from pathlib import Path

    src = (
        Path(__file__).parent.parent
        / "custom_components"
        / "bosch_shc_camera"
        / "__init__.py"
    ).read_text()
    assert "on_proxy_died=" in src, (
        "_start_tls_proxy must pass on_proxy_died= to start_tls_proxy() — "
        "otherwise the circuit breaker has no way to signal the coordinator"
    )
    assert "_on_tls_proxy_died" in src, (
        "Coordinator must expose _on_tls_proxy_died method as the callback target"
    )
