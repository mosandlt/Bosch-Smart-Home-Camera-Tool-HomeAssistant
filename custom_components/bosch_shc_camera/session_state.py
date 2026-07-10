"""Per-camera live-session bookkeeping.

First slice of the coordinator god-object rewrite (Phase 1 of
.claude/plans/jiggly-moseying-peacock.md, in the project root). Consolidates
three previously separate per-camera coordinator dicts —
``_auto_renew_generation``, ``_session_idle_since``, and
``_stream_warming_started`` — into one object per camera.

These three were chosen for the FIRST slice specifically because they have
NO external readers (only `BoschCameraCoordinator` itself, in
``__init__.py``, ever touches them) — unlike ``_live_connections`` and
``_stream_warming``, which are read/mutated directly from ``camera.py`` and
``switch.py`` and therefore need a compatibility-preserving design before
they can move. This slice is a pure internal consolidation with zero
blast radius on the other entity-platform files, mirroring the
``lock_utils.py`` Phase 0 step.

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

    ``idle_since`` and ``warming_started`` use ``None``/``float('-inf')``
    respectively as their "not currently set" sentinel — matching the exact
    semantics of the dict lookups they replace (SENTINEL_RULE: never ``0.0``
    for "never done", since CI/production hosts boot with a nonzero
    monotonic clock already).
    """

    generation: int = 0
    idle_since: float | None = None
    warming_started: float = float("-inf")


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
