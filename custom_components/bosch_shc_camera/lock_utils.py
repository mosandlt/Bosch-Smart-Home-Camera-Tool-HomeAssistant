"""Get-or-create per-key ``asyncio.Lock`` helper.

The coordinator (``__init__.py``) independently grew five near-identical
copies of the same "get-or-create a lock for this key in my dict" pattern —
``_get_stream_lock``, ``_get_rcp_session_lock``, ``_get_nvr_recorder_lock``,
``async_fetch_live_snapshot``'s ``_snapshot_fetch_locks`` lookup, and an
inline ``_fresh_snap_locks.setdefault(key, asyncio.Lock())`` variant — as new
per-camera/per-session locking needs were bolted on release after release.
This collapses all five into one function each of them now delegates to.

Deliberately NOT applied to the go2rtc setup lock in ``async_setup_entry``
(``hass.data.setdefault(f"{DOMAIN}_go2rtc_init_lock", asyncio.Lock())``) —
that one is a single fixed-key lock scoped to ``hass.data`` for
cross-config-entry serialization, not a per-key registry the coordinator
owns, so forcing it through this helper would be a shape mismatch rather
than real deduplication.
"""

from __future__ import annotations

import asyncio


def get_or_create_lock(store: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
    """Return the ``asyncio.Lock`` for ``key`` in ``store``, creating it if absent.

    Safe under asyncio: check-then-insert has no ``await`` between the two
    steps, so concurrent coroutines cannot interleave here.
    """
    lock = store.get(key)
    if lock is None:
        lock = asyncio.Lock()
        store[key] = lock
    return lock
