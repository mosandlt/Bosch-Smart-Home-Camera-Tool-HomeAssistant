"""Tests for lock_utils.get_or_create_lock — the get-or-create per-key
asyncio.Lock helper that collapsed the coordinator's several duplicated
copies of this pattern (_get_stream_lock/_get_rcp_session_lock/
_get_nvr_recorder_lock/_get_nvr_clip_assembly_lock/_snapshot_fetch_locks/
_go2rtc_reregister_locks/_fresh_snap_locks). See
tests/test_session_state_facade_slice4.py for the lock-IDENTITY-preserving
CacheFieldView integration these coordinator dicts now use in production."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.bosch_shc_camera.lock_utils import get_or_create_lock


class TestGetOrCreateLock:
    def test_creates_new_lock_for_unknown_key(self):
        """First call for a key creates and stores an asyncio.Lock."""
        store: dict[str, asyncio.Lock] = {}

        lock = get_or_create_lock(store, "a")

        assert isinstance(lock, asyncio.Lock)
        assert store["a"] is lock

    def test_returns_same_lock_on_second_call(self):
        """Repeated calls for the same key return the identical lock object."""
        store: dict[str, asyncio.Lock] = {}

        lock1 = get_or_create_lock(store, "a")
        lock2 = get_or_create_lock(store, "a")

        assert lock1 is lock2

    def test_different_keys_get_different_locks(self):
        """Two distinct keys get distinct asyncio.Lock instances."""
        store: dict[str, asyncio.Lock] = {}

        lock_a = get_or_create_lock(store, "a")
        lock_b = get_or_create_lock(store, "b")

        assert lock_a is not lock_b

    def test_does_not_overwrite_existing_lock(self):
        """A pre-populated lock in the store must be returned as-is, not
        replaced by a fresh one."""
        store: dict[str, asyncio.Lock] = {}
        preexisting = asyncio.Lock()
        store["a"] = preexisting

        lock = get_or_create_lock(store, "a")

        assert lock is preexisting

    @pytest.mark.asyncio
    async def test_lock_is_actually_usable_for_serialization(self):
        """Sanity check the returned object behaves as a real asyncio.Lock
        (not just an isinstance check) — acquire/release works."""
        store: dict[str, asyncio.Lock] = {}
        lock = get_or_create_lock(store, "a")

        assert not lock.locked()
        async with lock:
            assert lock.locked()
        assert not lock.locked()
