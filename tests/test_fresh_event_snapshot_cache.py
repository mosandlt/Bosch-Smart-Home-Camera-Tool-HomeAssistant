"""Coverage tests for `async_fetch_fresh_event_snapshot` cache fast-path.

Exercises the lock-free cache hit branch (L3725) so a hot-path FCM-burst
doesn't take the lock on every concurrent caller.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator


def _coord(cache: dict[str, tuple[bytes, float]]) -> SimpleNamespace:
    c = SimpleNamespace()
    c._fresh_snap_cache = cache
    c._fresh_snap_locks = {}
    c.token = "TOKEN"
    c.hass = SimpleNamespace()
    return c


@pytest.mark.asyncio
class TestFreshSnapshotCacheFastPath:
    async def test_returns_cached_when_unexpired(self):
        """Fast-path: cache hit with future expiry returns immediately."""
        # Expiry far in the future relative to current monotonic clock.
        cache = {"C": (b"JPEG_DATA", float("inf"))}
        coord = _coord(cache)
        result = await BoschCameraCoordinator.async_fetch_fresh_event_snapshot(
            coord, "C"
        )
        assert result == b"JPEG_DATA"

    async def test_returns_none_on_expired_cache_without_token(self):
        """Stale cache + no token → fast-path falls through, slow-path bails
        on missing token. Exercises the post-fast-path token guard."""
        cache = {"C": (b"JPEG_DATA", float("-inf"))}
        coord = _coord(cache)
        coord.token = None  # forces the early-return after the fast path
        result = await BoschCameraCoordinator.async_fetch_fresh_event_snapshot(
            coord, "C"
        )
        assert result is None

    async def test_lock_path_recheck_hit(self):
        """Slow-path: a concurrent caller that queued on the lock must see
        the cache populated by the winner and return without another fetch.
        Simulated by stuffing the cache between the fast-path miss and the
        in-lock re-check via an awaitable lock that lets us write first."""
        cache: dict[str, tuple[bytes, float]] = {}
        coord = _coord(cache)

        original_lock = asyncio.Lock()

        # When acquiring the lock, populate the cache so the in-lock re-check
        # picks it up and short-circuits without any HTTP call.
        class _PopulatingLock:
            def __init__(self, target: dict[str, tuple[bytes, float]]):
                self._inner = original_lock
                self._target = target

            async def __aenter__(self):
                await self._inner.__aenter__()
                self._target["C"] = (b"CONCURRENT_DATA", float("inf"))
                return self

            async def __aexit__(self, *a):
                return await self._inner.__aexit__(*a)

        coord._fresh_snap_locks["C"] = _PopulatingLock(cache)
        result = await BoschCameraCoordinator.async_fetch_fresh_event_snapshot(
            coord, "C"
        )
        assert result == b"CONCURRENT_DATA"
