"""Session-State-Facade Slice 3 (docs/stream-perf-stability-refactor-plan.md)
— dedicated regression tests for the `live_connections`/`user_intent_streams`
migration onto `CameraSessionState.live_connection`/`.user_intent_stream`.

Slice 3 was flagged in the plan as higher-risk than Slice 1/2 because
`live_connections` is actively read AND mutated (multiple `.pop()` call
sites) by today's Phase 1/2/3 code. Before relying on the existing Slice 2
`CacheFieldView` for it, its `.pop()` behavior (inherited from
`collections.abc.MutableMapping`, never previously exercised by any Slice 2
caller/test) needed dedicated verification against plain-`dict.pop()`
semantics — that's most of this file. The rest covers the in-place nested-
mutation pattern `session_renewal.py` relies on, and purge correctness for
both new fields (auto-discovery in `test_cam_id_purge_completeness.py`
cannot see either — see that file's Slice 2 precedent, `CacheFieldView`/
`BoolFieldView` instances are not `dict`/`set` instances).
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.bosch_shc_camera.session_state import (
    BoolFieldView,
    CacheFieldView,
    CameraSessionState,
)

CAM_A = "cam-a"
CAM_B = "cam-b"


class TestCacheFieldViewPopSemanticsForLiveConnections:
    """`.pop()` on `CacheFieldView` must match plain `dict.pop()` exactly —
    this is what the two (now more than two) `.pop(cam_id, None)` call
    sites across `camera.py`/`switch.py`/`stream_lifecycle.py`/
    `live_connection.py` rely on."""

    def test_pop_present_key_returns_value_and_removes_it(self):
        sessions = {CAM_A: CameraSessionState(live_connection={"proxyUrl": "x"})}
        view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )
        result = view.pop(CAM_A, None)
        assert result == {"proxyUrl": "x"}
        assert CAM_A not in view
        # Matches plain dict.pop: the underlying session survives (other
        # fields on it are untouched), only this field is cleared.
        assert CAM_A in sessions

    def test_pop_absent_key_with_default_returns_default(self):
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )
        # Mirrors every real call site's `.pop(cam_id, None)` two-arg form.
        assert view.pop(CAM_A, None) is None
        sentinel = {"fallback": True}
        assert view.pop(CAM_A, sentinel) is sentinel

    def test_pop_absent_key_no_default_raises_keyerror(self):
        """dict.pop(missing_key) with NO default raises KeyError — verify
        CacheFieldView matches, since no production call site actually uses
        the no-default form (all use `.pop(cam_id, None)`), so this branch
        was previously unverified."""
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )
        with pytest.raises(KeyError):
            view.pop(CAM_A)

    def test_pop_field_unset_but_session_exists_returns_default(self):
        """A session exists (e.g. another field on it was already written)
        but `live_connection` itself was never set — must behave exactly
        like popping a missing dict key, not like popping a stored `None`."""
        sessions = {CAM_A: CameraSessionState(generation=3)}
        view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )
        assert view.pop(CAM_A, "default") == "default"
        assert sessions[CAM_A].generation == 3  # untouched

    def test_pop_matches_plain_dict_pop_behavior_side_by_side(self):
        """Direct behavioral comparison against a real `dict` performing the
        exact same sequence of operations, to catch any subtle semantic
        drift a hand-written assertion might miss."""
        plain: dict[str, dict[str, Any]] = {CAM_A: {"k": "v"}}
        sessions = {CAM_A: CameraSessionState(live_connection={"k": "v"})}
        view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )

        assert view.pop(CAM_A, None) == plain.pop(CAM_A, None)
        assert view.pop(CAM_A, None) == plain.pop(CAM_A, None)  # both now absent
        assert view.pop(CAM_B, "x") == plain.pop(CAM_B, "x")


class TestCacheFieldViewNestedMutationForLiveConnections:
    """`session_renewal.py`'s credential-rotation path does
    `live = coordinator.live_connections.get(cam_id); live["rtspsUrl"] = ...`
    — the returned dict must be the SAME object stored on the session, not a
    copy, or the mutation would silently vanish."""

    def test_getitem_returns_same_object_reference(self):
        conn = {"rtspsUrl": "old"}
        sessions = {CAM_A: CameraSessionState(live_connection=conn)}
        view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )
        fetched = view[CAM_A]
        assert fetched is conn
        fetched["rtspsUrl"] = "new"
        assert sessions[CAM_A].live_connection["rtspsUrl"] == "new"

    def test_get_returns_same_object_reference_for_inplace_write(self):
        conn = {"_local_user": "u1"}
        sessions = {CAM_A: CameraSessionState(live_connection=conn)}
        view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )
        live = view.get(CAM_A)
        assert live is not None
        live["_local_user"] = "u2"
        live["_local_password"] = "p2"
        assert view[CAM_A] == {"_local_user": "u2", "_local_password": "p2"}


class TestBoolFieldViewForUserIntentStream:
    """`user_intent_streams` migrated onto `CameraSessionState.
    user_intent_stream` via the existing `BoolFieldView` — same contract as
    the Slice 1 boolean flags, verified here with the actual field name
    used in production (`session_state.py`'s generic tests already cover
    the mechanism with a different field name)."""

    def test_add_and_contains(self):
        sessions: dict[str, CameraSessionState] = {}
        view = BoolFieldView(sessions, "user_intent_stream")
        assert CAM_A not in view
        view.add(CAM_A)
        assert CAM_A in view
        assert sessions[CAM_A].user_intent_stream is True

    def test_discard_removes_intent_but_keeps_session(self):
        sessions = {CAM_A: CameraSessionState(user_intent_stream=True, generation=7)}
        view = BoolFieldView(sessions, "user_intent_stream")
        view.discard(CAM_A)
        assert CAM_A not in view
        assert sessions[CAM_A].generation == 7

    def test_discard_missing_cam_is_noop(self):
        sessions: dict[str, CameraSessionState] = {}
        view = BoolFieldView(sessions, "user_intent_stream")
        view.discard(CAM_A)  # must not raise
        assert CAM_A not in sessions

    def test_independent_of_live_connection_field_on_same_session(self):
        """Both fields now live on the SAME `CameraSessionState` instance
        (previously two entirely separate dicts) — verify writing one does
        not clobber the other."""
        sessions: dict[str, CameraSessionState] = {}
        intent_view = BoolFieldView(sessions, "user_intent_stream")
        conn_view: CacheFieldView[dict[str, Any]] = CacheFieldView(
            sessions, "live_connection"
        )

        intent_view.add(CAM_A)
        assert CAM_A not in conn_view  # live_connection still unset

        conn_view[CAM_A] = {"proxyUrl": "y"}
        assert CAM_A in intent_view  # user_intent_stream untouched by the above

        intent_view.discard(CAM_A)
        assert CAM_A in conn_view  # live_connection survives discard of intent
