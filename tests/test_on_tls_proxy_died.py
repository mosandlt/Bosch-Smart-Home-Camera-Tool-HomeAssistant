"""Coverage tests for `BoschCameraCoordinator._on_tls_proxy_died`.

Auto-rebuild flow invoked by the TLS-proxy circuit breaker. Five branches:
1. Backoff skip (recent rebuild)
2. Stream no longer active
3. Active connection is not LOCAL
4. Successful rebuild
5. Rebuild raises / returns None
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator


def _coord(
    *,
    live: dict | None = None,
    last_rebuild: float = float("-inf"),
    try_result=None,
    try_raises: Exception | None = None,
) -> SimpleNamespace:
    c = SimpleNamespace()
    c._tls_proxy_rebuild_last = (
        {} if last_rebuild == float("-inf") else {"C": last_rebuild}
    )
    c._live_connections = {"C": live} if live else {}
    c._stream_warming = set()
    c._stream_warming_started = {}
    c._stop_tls_proxy = AsyncMock(return_value=None)
    if try_raises is not None:
        c.try_live_connection = AsyncMock(side_effect=try_raises)
    else:
        c.try_live_connection = AsyncMock(return_value=try_result)
    return c


@pytest.fixture
def no_sleep(monkeypatch):
    """Skip the 5s pre-rebuild settle wait."""

    async def _fast(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _fast)


@pytest.mark.asyncio
class TestOnTlsProxyDied:
    async def test_backoff_skips_when_recent(self, monkeypatch, no_sleep):
        # last rebuild 1s ago, threshold = 30s → skip
        from custom_components.bosch_shc_camera import time as _m_time

        monkeypatch.setattr(_m_time, "monotonic", lambda: 100.0)
        c = _coord(last_rebuild=99.0)
        await BoschCameraCoordinator._on_tls_proxy_died(c, "C")
        # try_live_connection must NOT have been called.
        c.try_live_connection.assert_not_awaited()
        c._stop_tls_proxy.assert_not_awaited()

    async def test_stream_no_longer_active(self, monkeypatch, no_sleep):
        from custom_components.bosch_shc_camera import time as _m_time

        monkeypatch.setattr(_m_time, "monotonic", lambda: 1000.0)
        c = _coord(live=None)  # no live connection
        await BoschCameraCoordinator._on_tls_proxy_died(c, "C")
        c.try_live_connection.assert_not_awaited()
        c._stop_tls_proxy.assert_not_awaited()

    async def test_skip_when_not_local(self, monkeypatch, no_sleep):
        from custom_components.bosch_shc_camera import time as _m_time

        monkeypatch.setattr(_m_time, "monotonic", lambda: 1000.0)
        c = _coord(live={"_connection_type": "REMOTE"})
        await BoschCameraCoordinator._on_tls_proxy_died(c, "C")
        c.try_live_connection.assert_not_awaited()
        # _stop_tls_proxy must NOT be called for a non-LOCAL flow.
        c._stop_tls_proxy.assert_not_awaited()

    async def test_successful_rebuild(self, monkeypatch, no_sleep):
        from custom_components.bosch_shc_camera import time as _m_time

        monkeypatch.setattr(_m_time, "monotonic", lambda: 1000.0)
        c = _coord(
            live={"_connection_type": "LOCAL"}, try_result={"_connection_type": "LOCAL"}
        )
        await BoschCameraCoordinator._on_tls_proxy_died(c, "C")
        c._stop_tls_proxy.assert_awaited_once_with("C")
        c.try_live_connection.assert_awaited_once_with("C")

    async def test_rebuild_returns_none(self, monkeypatch, no_sleep):
        from custom_components.bosch_shc_camera import time as _m_time

        monkeypatch.setattr(_m_time, "monotonic", lambda: 1000.0)
        # try_live_connection returns falsy → "returned no result" warning branch (L4324).
        c = _coord(live={"_connection_type": "LOCAL"}, try_result=None)
        await BoschCameraCoordinator._on_tls_proxy_died(c, "C")
        c.try_live_connection.assert_awaited_once_with("C")

    async def test_rebuild_raises(self, monkeypatch, no_sleep):
        from custom_components.bosch_shc_camera import time as _m_time

        monkeypatch.setattr(_m_time, "monotonic", lambda: 1000.0)
        # try_live_connection raises → except branch (L4328-L4329).
        c = _coord(
            live={"_connection_type": "LOCAL"},
            try_raises=RuntimeError("transient WiFi drop"),
        )
        # Must NOT propagate — the wrapper swallows so the camera task
        # survives until the next heartbeat retry.
        await BoschCameraCoordinator._on_tls_proxy_died(c, "C")
        c.try_live_connection.assert_awaited_once_with("C")
