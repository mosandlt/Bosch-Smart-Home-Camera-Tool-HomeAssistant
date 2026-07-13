"""Session-State-Facade Slice 4 (docs/stream-perf-stability-refactor-plan.md)
— dedicated regression tests for the lock-dict migration onto
`CameraSessionState.stream_lock`/`.nvr_recorder_lock`/`.snapshot_fetch_lock`/
`.go2rtc_reregister_lock`/`.nvr_clip_assembly_lock`.

Slice 4 is the plan's highest-risk slice ("Locks — höchstes Risiko,
timing-kritisch"): `asyncio.Lock` objects have IDENTITY — two different
`Lock()` instances are NEVER interchangeable even if both are unlocked. A
facade view that minted a NEW lock on every access instead of returning the
one existing instance would silently destroy every mutual-exclusion
guarantee built on top of it (two callers each believe they hold exclusive
access while actually holding two different, independently-unlocked Lock
objects — no deadlock, just silently lost exclusivity).

This file's `TestLockIdentityPreservedAcrossCacheFieldView` class is the
mandatory pre-migration gate the task calls for: it must pass BEFORE any
production call site is switched from a bare `dict[str, asyncio.Lock]` to a
`CacheFieldView[asyncio.Lock]`, using the existing `lock_utils.
get_or_create_lock` helper exactly as `__init__.py`'s five lock getters do.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.bosch_shc_camera.lock_utils import get_or_create_lock
from custom_components.bosch_shc_camera.session_state import (
    CacheFieldView,
    CameraSessionState,
)

CAM_A = "cam-a"
CAM_B = "cam-b"


class TestLockIdentityPreservedAcrossCacheFieldView:
    """The single most critical test in this migration: two successive
    `get_or_create_lock(view, cam_id)` calls for the SAME cam_id must return
    the exact same `asyncio.Lock` OBJECT (`is`, not just `==` — `asyncio.
    Lock` has no custom `__eq__`, so `==` would trivially pass even for two
    different instances via default identity-based equality; only `is`
    actually proves the object graph is shared)."""

    async def test_get_or_create_lock_returns_same_object_twice(self) -> None:
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(sessions, "stream_lock")

        lock1 = get_or_create_lock(view, CAM_A)
        lock2 = get_or_create_lock(view, CAM_A)
        assert lock1 is lock2

    async def test_get_or_create_lock_returns_same_object_many_times(self) -> None:
        """Guards against any accidental per-call materialization — not just
        the second call, but every subsequent call must see the same
        instance."""
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(
            sessions, "nvr_recorder_lock"
        )

        first = get_or_create_lock(view, CAM_A)
        for _ in range(10):
            assert get_or_create_lock(view, CAM_A) is first

    async def test_different_cam_ids_get_different_lock_objects(self) -> None:
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(
            sessions, "snapshot_fetch_lock"
        )

        lock_a = get_or_create_lock(view, CAM_A)
        lock_b = get_or_create_lock(view, CAM_B)
        assert lock_a is not lock_b

    async def test_lock_object_survives_direct_view_getitem_after_creation(
        self,
    ) -> None:
        """Once created via the helper, plain `view[cam_id]` (not just
        another `get_or_create_lock` call) must also see the same object —
        proves `CacheFieldView.__getitem__` itself is not creating copies."""
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(
            sessions, "go2rtc_reregister_lock"
        )

        created = get_or_create_lock(view, CAM_A)
        assert view[CAM_A] is created
        assert sessions[CAM_A].go2rtc_reregister_lock is created

    async def test_lock_identity_preserved_across_mutual_exclusion(self) -> None:
        """End-to-end proof that the identity guarantee actually delivers
        real mutual exclusion: two "concurrent" coroutines fetching the lock
        for the same cam_id and acquiring it must genuinely serialize (the
        second's `async with` blocks until the first releases) — this would
        NOT hold if each fetched a different Lock instance."""
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(
            sessions, "nvr_clip_assembly_lock"
        )
        order: list[str] = []

        async def holder() -> None:
            lock = get_or_create_lock(view, CAM_A)
            async with lock:
                order.append("holder-enter")
                await asyncio.sleep(0.01)
                order.append("holder-exit")

        async def waiter() -> None:
            await asyncio.sleep(0)  # let holder acquire first
            lock = get_or_create_lock(view, CAM_A)
            async with lock:
                order.append("waiter-enter")

        await asyncio.gather(holder(), waiter())
        assert order == ["holder-enter", "holder-exit", "waiter-enter"]

    async def test_setitem_then_get_or_create_returns_the_set_object(self) -> None:
        """Mirrors `lock_utils.get_or_create_lock`'s own `store[key] = lock`
        write path once more from the caller side — an externally
        pre-seeded lock (as several test fixtures across the suite do) must
        also be respected, not silently replaced."""
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(sessions, "stream_lock")
        preseeded = asyncio.Lock()
        view[CAM_A] = preseeded

        assert get_or_create_lock(view, CAM_A) is preseeded

    async def test_lock_not_replaced_while_held(self) -> None:
        """A lock currently `locked()` (held elsewhere) must never be
        swapped out by a concurrent `get_or_create_lock` call for the same
        cam_id — `lock_utils.get_or_create_lock`'s check-then-insert has no
        `await` between `.get()` and `store[key] = lock`, so this is
        structurally guaranteed, but verify the end-to-end behavior anyway
        rather than just trusting the reasoning."""
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(sessions, "stream_lock")

        lock = get_or_create_lock(view, CAM_A)
        async with lock:
            assert lock.locked()
            # A second, independent "get" while the first is held must
            # still observe the SAME (held) lock object, not a fresh
            # unlocked one.
            same_lock = get_or_create_lock(view, CAM_A)
            assert same_lock is lock
            assert same_lock.locked()


class TestCacheFieldViewGenericGetSemanticsForLocks:
    """`get_or_create_lock` relies on `store.get(key)` returning `None` for
    an absent key (matching plain-`dict.get()`, the contract the helper was
    originally written against) — verify `CacheFieldView`'s inherited
    `MutableMapping.get()` mixin actually provides this for a fresh/empty
    store, since Slice 2/3 never exercised `.get()` with no default for a
    genuinely-missing key on a lock-typed field before."""

    def test_get_missing_key_returns_none_by_default(self) -> None:
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(sessions, "stream_lock")
        assert view.get(CAM_A) is None

    def test_get_missing_key_on_existing_session_returns_none(self) -> None:
        """Session exists (another field already populated) but this
        particular lock field was never set — must still behave like a
        missing dict key, not like a stored falsy value."""
        sessions = {CAM_A: CameraSessionState(generation=5)}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(sessions, "stream_lock")
        assert view.get(CAM_A) is None
        assert sessions[CAM_A].generation == 5


@pytest.mark.parametrize(
    "field_name",
    [
        "stream_lock",
        "nvr_recorder_lock",
        "snapshot_fetch_lock",
        "go2rtc_reregister_lock",
        "nvr_clip_assembly_lock",
        "fresh_snap_lock",
    ],
)
class TestAllFiveSlice4LockFieldsIndividually:
    """Parametrized identity + independence check across all six Slice 4
    lock fields (not just the one field spot-checked above per scenario) —
    each must independently preserve lock identity and stay isolated from
    the other five fields on the same `CameraSessionState` instance.

    Includes `fresh_snap_lock` — the 6th field found via this migration's
    own systematic re-audit (see the module docstring in `session_state.py`
    for how it was discovered) — a second-round bug-hunt sub-agent caught
    this parametrized class initially omitting it despite the rest of the
    file (and the purge-completeness test) correctly covering all 6,
    ironically repeating the exact class of miss the docstring warns about.
    """

    async def test_identity_preserved(self, field_name: str) -> None:
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[asyncio.Lock] = CacheFieldView(sessions, field_name)
        first = get_or_create_lock(view, CAM_A)
        assert get_or_create_lock(view, CAM_A) is first

    async def test_independent_of_other_lock_fields_on_same_session(
        self, field_name: str
    ) -> None:
        all_fields = [
            "stream_lock",
            "nvr_recorder_lock",
            "snapshot_fetch_lock",
            "go2rtc_reregister_lock",
            "nvr_clip_assembly_lock",
            "fresh_snap_lock",
        ]
        sessions: dict[str, CameraSessionState] = {}
        views = {
            name: CacheFieldView[asyncio.Lock](sessions, name) for name in all_fields
        }

        this_lock = get_or_create_lock(views[field_name], CAM_A)
        for other_name in all_fields:
            if other_name == field_name:
                continue
            assert CAM_A not in views[other_name], (
                f"creating {field_name}'s lock must not spuriously populate "
                f"{other_name} on the same CameraSessionState instance"
            )

        # Now populate every OTHER field too and confirm this one is untouched.
        other_locks = {
            other_name: get_or_create_lock(views[other_name], CAM_A)
            for other_name in all_fields
            if other_name != field_name
        }
        assert get_or_create_lock(views[field_name], CAM_A) is this_lock
        for other_lock in other_locks.values():
            assert other_lock is not this_lock
