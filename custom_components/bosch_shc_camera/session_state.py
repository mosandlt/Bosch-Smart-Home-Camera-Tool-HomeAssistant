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

``generation`` is the TOCTOU guard central to this rewrite's motivation: a
caller that decides to tear down or renew a session captures the generation
at decision time, and re-checks it after any ``await`` (lock acquisition,
sleep) before acting — a generation mismatch means a newer session has
since superseded the stale decision.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CameraSessionState:
    """Per-camera live-session bookkeeping.

    ``idle_since``/``opened_at`` use ``None``, ``warming_started`` uses
    ``float('-inf')``, as their "not currently set" sentinel — matching the
    exact semantics of the dict/set lookups they replace (SENTINEL_RULE:
    never ``0.0`` for "never done", since CI/production hosts boot with a
    nonzero monotonic clock already).
    """

    generation: int = 0
    idle_since: float | None = None
    warming_started: float = float("-inf")
    warming: bool = False
    opened_at: float | None = None


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
