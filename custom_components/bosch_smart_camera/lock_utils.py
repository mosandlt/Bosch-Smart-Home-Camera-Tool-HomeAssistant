"""Get-or-create per-key ``asyncio.Lock`` helper.

Used by every per-camera/per-session locking need across the coordinator
(``get_stream_lock``, ``_get_rcp_session_lock``, ``get_nvr_recorder_lock``,
``get_nvr_clip_assembly_lock``, snapshot-fetch lock lookups, etc.) so each
now delegates to one function instead of duplicating the same
"get-or-create a lock for this key in my dict" pattern.

Deliberately NOT applied to the go2rtc setup lock in ``async_setup_entry``
(``hass.data.setdefault(f"{DOMAIN}_go2rtc_init_lock", asyncio.Lock())``) —
that one is a single fixed-key lock scoped to ``hass.data`` for
cross-config-entry serialization, not a per-key registry the coordinator
owns, so forcing it through this helper would be a shape mismatch rather
than real deduplication.

``store`` accepts any ``MutableMapping`` (not just a plain ``dict``) since
several per-cam_id coordinator lock dicts are backed by
``session_state.CacheFieldView`` instead of a plain dict — a full
``MutableMapping`` whose ``.get()``/``__setitem__`` behave identically to a
plain dict's for this helper's purposes, including that a lock's IDENTITY
survives across repeated calls. Test fixtures across the suite still pass
plain ``dict[str, asyncio.Lock]`` stand-ins directly, which continue to
work unchanged since ``dict`` satisfies ``MutableMapping`` too.
"""

from __future__ import annotations

import asyncio
from collections.abc import MutableMapping


def get_or_create_lock(
    store: MutableMapping[str, asyncio.Lock], key: str
) -> asyncio.Lock:
    """Return the ``asyncio.Lock`` for ``key`` in ``store``, creating it if absent.

    Safe under asyncio: check-then-insert has no ``await`` between the two
    steps, so concurrent coroutines cannot interleave here.
    """
    lock = store.get(key)
    if lock is None:
        lock = asyncio.Lock()
        store[key] = lock
    return lock
