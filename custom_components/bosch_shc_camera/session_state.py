"""Per-camera live-session bookkeeping.

Consolidates per-camera coordinator state into one `CameraSessionState`
dataclass per camera, accessed either directly or through thin dict/set-like
"view" facades (`StreamWarmingView`, `LiveOpenedAtView`, `FloatFieldView`,
`BoolFieldView`, `CacheFieldView`) that preserve the exact `dict`/`set`
contract external call sites (`camera.py`, `switch.py`, `shc.py`,
`select.py`, `light.py`, `number.py`, `services.py`, `slow_tier.py`,
`stream_lifecycle.py`, `live_connection.py`, etc.) already rely on — so
those call sites keep using `.get()`/`[cam_id] = ...`/`in`/`.add()`/
`.discard()`/`.pop()` unchanged.

``generation`` is the TOCTOU guard central to this design: a caller that
decides to tear down or renew a session captures the generation at
decision time, and re-checks it after any ``await`` (lock acquisition,
sleep) before acting — a generation mismatch means a newer session has
since superseded the stale decision.

Cache fields default to the `_UNSET` sentinel (not `None`), since several
of them are themselves `Optional`-valued caches (e.g. `rcp_privacy_cache:
int | None`) where a stored `None` is a legitimate cached value ("queried,
camera reported nothing") that must stay distinguishable from "never
queried" for `in`/`.get()` callers.

`CacheFieldView.__getitem__`/`.get()` return the SAME stored object
reference (not a copy) for mutable dict/list/Lock values, so nested
in-place mutation (`cache[cam_id]["key"] = value`, used by
`session_renewal.py`'s credential-rotation path) and lock identity
(repeated `get_or_create_lock()` calls for the same cam_id returning the
SAME `asyncio.Lock`) both work exactly as they did against the old bare
per-field dicts — verified explicitly, including a real two-coroutine
`async with lock:` mutual-exclusion test with a same-object check
performed while the lock is held (`tests/test_session_state_facade_slice4.py`).
An `asyncio.Lock` is a stateful, identity-bearing object: two different
`Lock()` instances are never interchangeable even both-unlocked, so this
identity guarantee is what makes the shared-lock pattern safe to move onto
this facade at all.

`CacheFieldView.__iter__` materializes eagerly (not a lazy generator) so a
concurrent `get_or_create_session()` call elsewhere growing the shared
`_sessions` dict mid-loop cannot raise "dictionary changed size during
iteration" on a caller iterating a view (e.g. `rcp_lan_ip_cache` in
`__init__.py`'s outage-ping loop).

Deliberately NOT folded into this dataclass — each has a genuinely
different shape that would make folding it in incorrect, not merely
incomplete:
  * `live_connections` (before it was folded in as `live_connection`),
    `nvr_processes`/`nvr_preroll_processes` (live `asyncio.subprocess.
    Process` handles) and `nvr_preroll_tasks` (`asyncio.Task` handles) are
    not simple cached data.
  * `nvr_drain_state` is a single flat dict with fixed string keys
    ("target"/"pending"/"promoted"/"uploaded"/"failed"/"last_age_by_cam"/
    "last_tick_ts") replaced wholesale every drain tick
    (`recorder.py::sync_drain_tick`), not cam_id-keyed at all; and
    `nvr_drain_failures` is keyed by staging **file path**
    (`recorder.py`, `failures[full] = failures.get(full, 0) + 1`), not
    cam_id. Migrating either into a per-cam field would be actively wrong.
  * `rcp_session_locks` is keyed by `proxy_hash`, not `cam_id` — wrong
    shape for a per-camera facade field.
  * `guards.py::_get_cam_lock`'s per-cam lock registry (e.g.
    `_audio_config_locks`) is never pre-declared in
    `BoschCameraCoordinator.__init__` at all — lazily materialized via
    `getattr`/`setattr` on first use.

A lock is never dropped while held: `_purge_cam_id` (the only code path
that ever removes a `CameraSessionState`, via `self._sessions.pop(cam_id,
None)`) runs only after `cleanup_stale_devices` has confirmed a camera is
gone from the Bosch cloud account entirely — never mid-operation while one
of that camera's locks could be `locked()` by an in-flight coroutine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

_T = TypeVar("_T")


class _Unset:
    """Sentinel type for an unpopulated `CameraSessionState` cache field.

    A dedicated type (not `None`) because several cache fields are
    themselves `Optional`-valued caches (e.g. `rcp_privacy_cache: int |
    None`) — a stored `None` there is a legitimate cached value that must
    stay distinguishable from "never queried".
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


_UNSET = _Unset()


@dataclass
class CameraSessionState:
    """Per-camera live-session bookkeeping.

    ``idle_since``/``opened_at`` use ``None``, ``warming_started`` uses
    ``float('-inf')``, as their "not currently set" sentinel — matching the
    exact semantics of the dict/set lookups they replace (SENTINEL_RULE:
    never ``0.0`` for "never done", since CI/production hosts boot with a
    nonzero monotonic clock already).

    The float write-lock timestamp fields below likewise use ``None`` as
    their "not set" sentinel (preserving the exact `dict.get(cam_id,
    default)` semantics of the dicts they replace — callers supply their
    own default, most commonly ``float('-inf')`` per SENTINEL_RULE), and
    the bool "already logged/deferred" flags use ``False`` (matching the
    `not in a_set` semantics of the sets they replace).
    """

    generation: int = 0
    idle_since: float | None = None
    warming_started: float = float("-inf")
    warming: bool = False
    opened_at: float | None = None
    # Set exactly when live_connection.py actually publishes a usable
    # rtspsUrl for a LOCAL session — replaces a duplicate,
    # independently-guessed timeout constant in recorder.py that had
    # silently drifted out of sync with the real per-model min_total_wait
    # pacing enforced here, causing the NVR recorder to give up on every
    # coordinator tick for slower-encoder/weaker-WiFi cameras.
    # Cleared at the start of every fresh LOCAL warm-up attempt so a stale
    # "ready" signal from a previous session can never leak into a new one;
    # default_factory gives every camera its own Event instance (a bare
    # `= asyncio.Event()` default would share ONE Event across all cameras).
    stream_ready_event: asyncio.Event = field(default_factory=asyncio.Event)
    # ── Diagnostic/write-lock timestamps ────────────────────────────────
    offline_seen_at: float | None = None
    light_set_at: float | None = None
    notif_set_at: float | None = None
    privacy_set_at: float | None = None
    privacy_sound_set_at: float | None = None
    timestamp_set_at: float | None = None
    ledlights_set_at: float | None = None
    arming_set_at: float | None = None
    intrusion_config_set_at: float | None = None
    audio_detection_set_at: float | None = None
    motion_set_at: float | None = None
    alarm_settings_set_at: float | None = None
    lighting_options_set_at: float | None = None
    firmware_set_at: float | None = None
    # ── "Already logged/deferred this cycle" flags ──────────────────────
    notif_disabled_logged: bool = False
    fw_update_alerted: bool = False
    slow_tier_deferred: bool = False
    nvr_preroll_zero_warned: bool = False
    # ── Per-cam caches without cross-camera access ──────────────────────
    # All default to the `_UNSET` sentinel (see `_Unset` above) — matches
    # the `cam_id not in old_dict` semantics of the dicts they replace.
    rcp_state_cache: dict[str, Any] | _Unset = _UNSET
    shc_state_cache: dict[str, Any] | _Unset = _UNSET
    pan_cache: int | None | _Unset = _UNSET
    rcp_dimmer_cache: int | None | _Unset = _UNSET
    rcp_privacy_cache: int | None | _Unset = _UNSET
    rcp_clock_offset_cache: float | None | _Unset = _UNSET
    rcp_lan_ip_cache: str | None | _Unset = _UNSET
    rcp_product_name_cache: str | None | _Unset = _UNSET
    rcp_bitrate_cache: list[int] | _Unset = _UNSET
    rcp_alarm_catalog_cache: list[dict[str, Any]] | _Unset = _UNSET
    rcp_motion_zones_cache: list[dict[str, Any]] | _Unset = _UNSET
    rcp_motion_coords_cache: list[dict[str, Any]] | _Unset = _UNSET
    rcp_tls_cert_cache: dict[str, Any] | _Unset = _UNSET
    rcp_network_services_cache: list[str] | _Unset = _UNSET
    rcp_iva_catalog_cache: list[dict[str, Any]] | _Unset = _UNSET
    rcp_onvif_scopes_cache: dict[str, Any] | _Unset = _UNSET
    rcp_version_cache: str | None | _Unset = _UNSET
    nvr_mode_preference: str | _Unset = _UNSET
    local_creds_cache: dict[str, Any] | _Unset = _UNSET
    audio_cache: dict[str, Any] | _Unset = _UNSET
    nvr_user_intent: bool | _Unset = _UNSET
    nvr_error_state: str | _Unset = _UNSET
    nvr_recent_crash: float | _Unset = _UNSET
    nvr_auth_retry_count: int | _Unset = _UNSET
    nvr_event_clip_enabled: bool | _Unset = _UNSET
    nvr_preroll_last_crash: float | _Unset = _UNSET
    nvr_preroll_segment_counts: int | _Unset = _UNSET
    # ── Session/stream state ─────────────────────────────────────────────
    live_connection: dict[str, Any] | _Unset = _UNSET
    user_intent_stream: bool = False
    # ── Per-camera locks ─────────────────────────────────────────────────
    # All default to `_UNSET` (see `_Unset` above) — matches the
    # `cam_id not in old_dict` semantics of the `dict[str, asyncio.Lock]`
    # attributes they replace, so `lock_utils.get_or_create_lock`'s
    # `store.get(key)` check keeps returning `None` (not a spuriously
    # pre-created Lock) for a camera that never needed this lock yet.
    stream_lock: asyncio.Lock | _Unset = _UNSET
    nvr_recorder_lock: asyncio.Lock | _Unset = _UNSET
    snapshot_fetch_lock: asyncio.Lock | _Unset = _UNSET
    go2rtc_reregister_lock: asyncio.Lock | _Unset = _UNSET
    nvr_clip_assembly_lock: asyncio.Lock | _Unset = _UNSET
    fresh_snap_lock: asyncio.Lock | _Unset = _UNSET


def get_or_create_session(
    store: dict[str, CameraSessionState], cam_id: str
) -> CameraSessionState:
    """Return the `CameraSessionState` for `cam_id` in `store`, creating it if absent.

    Safe under asyncio: check-then-insert has no `await` between the two
    steps, so concurrent coroutines cannot interleave here (same idiom as
    `lock_utils.get_or_create_lock`).
    """
    session = store.get(cam_id)
    if session is None:
        session = CameraSessionState()
        store[cam_id] = session
    return session


class StreamWarmingView:
    """Set-like facade over `CameraSessionState.warming`.

    Preserves the `stream_warming: set[str]` contract external callers
    (`camera.py`: `in`/`not in`) rely on, without them needing to change.
    """

    def __init__(self, sessions: dict[str, CameraSessionState]) -> None:
        self._sessions = sessions

    def __contains__(self, cam_id: str) -> bool:
        session = self._sessions.get(cam_id)
        return session is not None and session.warming

    def add(self, cam_id: str) -> None:
        get_or_create_session(self._sessions, cam_id).warming = True

    def discard(self, cam_id: str) -> None:
        session = self._sessions.get(cam_id)
        if session is not None:
            session.warming = False

    def __len__(self) -> int:
        return sum(1 for session in self._sessions.values() if session.warming)


class LiveOpenedAtView:
    """Dict-like facade over `CameraSessionState.opened_at`.

    Preserves the `live_opened_at: dict[str, float]` contract external
    callers (`camera.py`: `.get()`/`.pop()`) rely on, without them needing
    to change.
    """

    def __init__(self, sessions: dict[str, CameraSessionState]) -> None:
        self._sessions = sessions

    def get(self, cam_id: str, default: float | None = None) -> float | None:
        session = self._sessions.get(cam_id)
        if session is None or session.opened_at is None:
            return default
        return session.opened_at

    def pop(self, cam_id: str, default: float | None = None) -> float | None:
        session = self._sessions.get(cam_id)
        if session is None or session.opened_at is None:
            return default
        val = session.opened_at
        session.opened_at = None
        return val

    def __setitem__(self, cam_id: str, value: float) -> None:
        get_or_create_session(self._sessions, cam_id).opened_at = value

    def __len__(self) -> int:
        return sum(
            1 for session in self._sessions.values() if session.opened_at is not None
        )


class FloatFieldView:
    """Dict-like facade over a named ``float | None`` field of `CameraSessionState`.

    Generalizes `LiveOpenedAtView` for reuse across the write-lock
    timestamp fields (``offline_seen_at``/every ``*_set_at``) — one
    instance per field, parameterized by `field_name`, instead of one
    hand-rolled class per field. Preserves the exact `dict[str, float]`
    contract (`.get()`/`[cam_id] = ...`/`.pop()`/`in`) external callers
    already rely on.
    """

    def __init__(
        self, sessions: dict[str, CameraSessionState], field_name: str
    ) -> None:
        self._sessions = sessions
        self._field_name = field_name

    def get(self, cam_id: str, default: float | None = None) -> float | None:
        session = self._sessions.get(cam_id)
        if session is None:
            return default
        value: float | None = getattr(session, self._field_name)
        return default if value is None else value

    def __setitem__(self, cam_id: str, value: float) -> None:
        setattr(get_or_create_session(self._sessions, cam_id), self._field_name, value)

    def __contains__(self, cam_id: str) -> bool:
        session = self._sessions.get(cam_id)
        return session is not None and getattr(session, self._field_name) is not None

    def __getitem__(self, cam_id: str) -> float:
        """Raise `KeyError` if unset — matches `dict[str, float][cam_id]` semantics
        for the `cam_id in view and view[cam_id]` call-site pattern."""
        session = self._sessions.get(cam_id)
        value: float | None = (
            None if session is None else getattr(session, self._field_name)
        )
        if value is None:
            raise KeyError(cam_id)
        return value

    def pop(self, cam_id: str, default: float | None = None) -> float | None:
        session = self._sessions.get(cam_id)
        if session is None:
            return default
        value: float | None = getattr(session, self._field_name)
        if value is None:
            return default
        setattr(session, self._field_name, None)
        return value

    def __len__(self) -> int:
        return sum(
            1
            for session in self._sessions.values()
            if getattr(session, self._field_name) is not None
        )


class BoolFieldView:
    """Set-like facade over a named `bool` field of `CameraSessionState`.

    Generalizes `StreamWarmingView` for reuse across the "already
    logged/deferred this cycle" flags (``notif_disabled_logged``,
    ``fw_update_alerted``, ``slow_tier_deferred``) — one instance per
    field, parameterized by `field_name`. Preserves the exact `set[str]`
    contract (`in`/`.add()`/`.discard()`) external callers already rely on.
    """

    def __init__(
        self, sessions: dict[str, CameraSessionState], field_name: str
    ) -> None:
        self._sessions = sessions
        self._field_name = field_name

    def __contains__(self, cam_id: str) -> bool:
        session = self._sessions.get(cam_id)
        return session is not None and bool(getattr(session, self._field_name))

    def add(self, cam_id: str) -> None:
        setattr(get_or_create_session(self._sessions, cam_id), self._field_name, True)

    def discard(self, cam_id: str) -> None:
        session = self._sessions.get(cam_id)
        if session is not None:
            setattr(session, self._field_name, False)

    def __len__(self) -> int:
        return sum(
            1
            for session in self._sessions.values()
            if getattr(session, self._field_name)
        )


class CacheFieldView(MutableMapping[str, _T]):
    """`MutableMapping[str, _T]` facade over a named per-camera cache field
    of `CameraSessionState`.

    Generalizes `FloatFieldView`/`BoolFieldView` for the heterogeneous
    cache value types (dict/list/int/str/float/bool, several of them
    themselves `Optional`) — one instance per field, parameterized by
    `field_name`, built on `collections.abc.MutableMapping` rather than
    hand-writing every dict method: only `__getitem__`/`__setitem__`/
    `__delitem__`/`__iter__`/`__len__` are implemented below, and the mixin
    supplies `.get()`/`.pop()`/`.setdefault()`/`.update()`/`.clear()`/
    `.items()`/`.values()`/`.keys()`/`in`/`==`/`bool()` on top — including
    the two whole-dict-iteration call sites these caches have
    (`rcp_lan_ip_cache` in `__init__.py`'s outage-ping loop and
    `tick_housekeeping.py`'s persisted-snapshot comprehension;
    `local_creds_cache` in the same `tick_housekeeping.py` snapshot path)
    and the nested-subscript-write pattern several `shc_state_cache`
    writers use (`cache[cam_id]["key"] = value`) — `__getitem__` returns
    the SAME stored object reference (not a copy), so in-place mutation of
    a returned dict/list persists correctly.

    Uses the `_UNSET` sentinel (not `None`) for "no cached value for this
    cam_id yet" — see `_Unset` above.
    """

    def __init__(
        self, sessions: dict[str, CameraSessionState], field_name: str
    ) -> None:
        self._sessions = sessions
        self._field_name = field_name

    def __getitem__(self, cam_id: str) -> _T:
        session = self._sessions.get(cam_id)
        value: object = (
            _UNSET if session is None else getattr(session, self._field_name)
        )
        if value is _UNSET:
            raise KeyError(cam_id)
        return cast("_T", value)

    def __setitem__(self, cam_id: str, value: _T) -> None:
        setattr(get_or_create_session(self._sessions, cam_id), self._field_name, value)

    def __delitem__(self, cam_id: str) -> None:
        session = self._sessions.get(cam_id)
        value: object = (
            _UNSET if session is None else getattr(session, self._field_name)
        )
        if value is _UNSET:
            raise KeyError(cam_id)
        setattr(session, self._field_name, _UNSET)

    def __iter__(self) -> Iterator[str]:
        # Materialized eagerly (not a lazy generator) so that another
        # field's `get_or_create_session()` call elsewhere growing the
        # shared `_sessions` dict mid-loop cannot raise "dictionary
        # changed size during iteration" on a caller iterating THIS view
        # (`for cid in coordinator.rcp_lan_ip_cache:` etc.) — the
        # original dedicated per-field dict this replaces never had that
        # cross-field growth risk, so preserving eager materialization
        # keeps the exact same failure mode (none).
        return iter(
            [
                cam_id
                for cam_id, session in self._sessions.items()
                if getattr(session, self._field_name) is not _UNSET
            ]
        )

    def __len__(self) -> int:
        return sum(
            1
            for session in self._sessions.values()
            if getattr(session, self._field_name) is not _UNSET
        )

    def __repr__(self) -> str:
        return f"CacheFieldView({dict(self)!r})"
