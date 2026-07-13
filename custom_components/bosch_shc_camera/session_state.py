"""Per-camera live-session bookkeeping.

Phase 1 of the coordinator god-object rewrite (see
.claude/plans/jiggly-moseying-peacock.md, in the project root). Consolidates
per-camera coordinator dicts into one object per camera.

Slice 1 folded in three dicts with NO external readers (only
`BoschCameraCoordinator` itself, in ``__init__.py``, ever touched them):
``_auto_renew_generation``, ``_session_idle_since``, ``_stream_warming_started``.

Slice 2 (this one) folds in two more: ``_stream_warming`` (a ``set[str]``)
and ``_live_opened_at`` (a ``dict[str, float]``) — both DO have external
readers (``camera.py``: ``in``/``not in`` on ``_stream_warming``, ``.get()``/
``.pop()`` on ``_live_opened_at``). Rather than rewrite those call sites,
``_StreamWarmingView``/``_LiveOpenedAtView`` below are thin facades
implementing exactly the subset of the ``set``/``dict`` protocol those call
sites use, backed by the same ``_sessions`` store — so
``coordinator._stream_warming``/``coordinator._live_opened_at`` keep
behaving exactly as before to every external caller, with zero changes
needed in ``camera.py``/``switch.py``.

Deliberately NOT folded into this slice: ``_live_connections`` itself — a
much larger, heterogeneous ~15-key dict (raw Bosch API JSON plus derived
fields) with real external MUTATION (two direct ``.pop()`` call sites in
``camera.py``/``switch.py``), not just reads. That merge needs its own
dedicated design (likely a Mapping-protocol facade or similar) and is left
for a future slice rather than folded in here.

Slice 1 of the ``docs/stream-perf-stability-refactor-plan.md`` "Session-
State-Facade — inkrementeller Migrationsplan" (Anhang 2026-07-13) folds in
the diagnostic/write-lock timestamp fields: ``offline_seen_at`` and every
``*_set_at`` write-lock timestamp (13 of them — one per cloud-writable
field guarded by ``BoschCameraCoordinator._is_write_locked``), plus three
boolean "already logged/deferred this cycle" flags
(``notif_disabled_logged``, ``fw_update_alerted``, ``slow_tier_deferred``).
Generalizes the ``LiveOpenedAtView``/``StreamWarmingView`` pattern above
into reusable ``FloatFieldView``/``BoolFieldView`` classes parameterized by
field name, since Slice 1 has 14 float fields and 3 bool fields rather than
one each — a hand-rolled view class per field would be pure duplication.
External call sites (``shc.py``, ``switch.py``, ``select.py``, ``light.py``,
``number.py``, ``services.py``, ``slow_tier.py``) keep using
``coordinator._x_set_at.get()``/``[cam_id] = ...`` / ``in``/``.add()``/
``.discard()`` exactly as before — only the ``__init__.py`` declaration
changed from a bare ``dict``/``set`` to a view instance.

``generation`` is the TOCTOU guard central to this rewrite's motivation: a
caller that decides to tear down or renew a session captures the generation
at decision time, and re-checks it after any ``await`` (lock acquisition,
sleep) before acting — a generation mismatch means a newer session has
since superseded the stale decision.

Slice 2 of the ``docs/stream-perf-stability-refactor-plan.md`` "Session-
State-Facade — inkrementeller Migrationsplan" folds in 27 per-cam_id cache
fields with no cross-camera access: every ``_rcp_*_cache`` (RCP protocol
data — dimmer/privacy/clock-offset/lan-ip/product-name/bitrate/alarm-
catalog/motion-zones/motion-coords/tls-cert/network-services/iva-catalog/
onvif-scopes/version/state), ``_shc_state_cache``, ``_pan_cache``,
``_audio_cache``, ``_local_creds_cache``, ``_nvr_mode_preference``, and the
plain per-cam Mini-NVR status dicts ``_nvr_user_intent``/``_nvr_error_state``/
``_nvr_recent_crash``/``_nvr_auth_retry_count``/``_nvr_event_clip_enabled``/
``_nvr_preroll_last_crash``/``_nvr_preroll_segment_counts``. Unlike Slice 1's
float/bool fields, these are heterogeneous (dict/list/int/str/float/bool,
several themselves ``Optional``) — generalized into one generic
``CacheFieldView[_T]`` built on ``collections.abc.MutableMapping`` rather
than one hand-rolled view class per field. Uses the ``_UNSET`` sentinel
(not ``None``) for "no value yet", since several of these fields are
themselves ``Optional``-valued caches (e.g. ``rcp_privacy_cache: int |
None``) where a stored ``None`` is a legitimate cached value ("queried,
camera reported nothing") that must stay distinguishable from "never
queried" for `in`/`.get()` callers.

Deliberately NOT folded into Slice 2 (audited, not an oversight):
``_nvr_processes``/``_nvr_preroll_processes`` (live ``asyncio.subprocess.
Process`` handles, not simple cached data — same "needs its own dedicated
design" reasoning as ``_live_connections`` above), ``_nvr_preroll_tasks``
(``asyncio.Task`` handles), ``_nvr_recorder_locks``/
``_nvr_clip_assembly_locks`` (locks — Slice 4), and ``_nvr_drain_state``/
``_nvr_drain_failures`` — both LOOKED like per-cam_id caches from their
`dict[str, ...]` type hints and their presence in `_PURGE_CAM_DICT_ATTRS`,
but turned out on inspection of `recorder.py` to NOT be cam_id-keyed at
all: `_nvr_drain_state` is a single flat dict with fixed string keys
("target"/"pending"/"promoted"/"uploaded"/"failed"/"last_age_by_cam"/
"last_tick_ts") replaced wholesale every drain tick
(`recorder.py::sync_drain_tick`), and `_nvr_drain_failures` is keyed by
staging **file path**, not cam_id (`recorder.py:1774`,
`failures[full] = failures.get(full, 0) + 1`). Migrating either into a
per-cam `CameraSessionState` field would be actively wrong. Their
`_PURGE_CAM_DICT_ATTRS` membership was already a no-op in practice (a
cam_id never literally matches a staging file path or a fixed key like
"target"), so leaving them as plain dicts changes nothing.
"""

from __future__ import annotations

from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass
from typing import Any, TypeVar, cast

_T = TypeVar("_T")


class _Unset:
    """Sentinel type for an unpopulated `CameraSessionState` cache field.

    A dedicated type (not `None`) because several Slice 2 fields are
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

    Slice 1 fields below all use ``None`` as their "not set" sentinel for
    the float write-lock timestamps (matching ``idle_since``/``opened_at``
    above, and preserving the exact `dict.get(cam_id, default)` semantics
    of the dicts they replace — callers supply their own default, most
    commonly ``float('-inf')`` per SENTINEL_RULE) and ``False`` for the
    bool "already logged/deferred" flags (matching the `not in a_set`
    semantics of the sets they replace).
    """

    generation: int = 0
    idle_since: float | None = None
    warming_started: float = float("-inf")
    warming: bool = False
    opened_at: float | None = None
    # ── Slice 1: diagnostic/write-lock timestamps ──────────────────────
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
    # ── Slice 1: "already logged/deferred this cycle" flags ────────────
    notif_disabled_logged: bool = False
    fw_update_alerted: bool = False
    slow_tier_deferred: bool = False
    # ── Slice 2: per-cam caches without cross-camera access ─────────────
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

    Preserves the `_stream_warming: set[str]` contract external callers
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

    Preserves the `_live_opened_at: dict[str, float]` contract external
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

    Generalizes `LiveOpenedAtView` for reuse across the Slice 1 write-lock
    timestamp fields (``offline_seen_at``/every ``*_set_at`` — see the
    module docstring) — one instance per field, parameterized by
    `field_name`, instead of one hand-rolled class per field. Preserves the
    exact `dict[str, float]` contract (`.get()`/`[cam_id] = ...`/`.pop()`/
    `in`) external callers already rely on.
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

    Generalizes `StreamWarmingView` for reuse across the Slice 1
    "already logged/deferred this cycle" flags (``notif_disabled_logged``,
    ``fw_update_alerted``, ``slow_tier_deferred`` — see the module
    docstring) — one instance per field, parameterized by `field_name`.
    Preserves the exact `set[str]` contract (`in`/`.add()`/`.discard()`)
    external callers already rely on.
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
    of `CameraSessionState` (Session-State-Facade Slice 2 — see the module
    docstring).

    Generalizes `FloatFieldView`/`BoolFieldView` (Slice 1) for Slice 2's
    heterogeneous cache value types (dict/list/int/str/float/bool, several
    of them themselves `Optional`) — one instance per field, parameterized
    by `field_name`, built on `collections.abc.MutableMapping` rather than
    hand-writing every dict method: only `__getitem__`/`__setitem__`/
    `__delitem__`/`__iter__`/`__len__` are implemented below, and the mixin
    supplies `.get()`/`.pop()`/`.setdefault()`/`.update()`/`.clear()`/
    `.items()`/`.values()`/`.keys()`/`in`/`==`/`bool()` on top — including
    the two whole-dict-iteration call sites this slice's caches have
    (`_rcp_lan_ip_cache` in `__init__.py`'s outage-ping loop and
    `tick_housekeeping.py`'s persisted-snapshot comprehension;
    `_local_creds_cache` in the same `tick_housekeeping.py` snapshot path)
    and the nested-subscript-write pattern several `_shc_state_cache`
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
        # (`for cid in coordinator._rcp_lan_ip_cache:` etc.) — the
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
