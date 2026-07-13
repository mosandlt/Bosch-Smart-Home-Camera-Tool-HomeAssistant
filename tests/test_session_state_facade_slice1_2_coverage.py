"""Coverage-closing tests for FloatFieldView/BoolFieldView (Slice 1) and
CacheFieldView (Slice 2) in session_state.py — the generic per-field facade
classes that back the coordinator's `_*_set_at`/`_notif_disabled_logged`-style
and `_rcp_*_cache`-style attributes. Production code never happens to
exercise every branch (e.g. nothing currently calls `.pop()`/`len()`/
`del view[cam_id]`/`repr(view)` on these facades), so these tests instantiate
the view classes directly against a bare `sessions` dict, mirroring the style
of the existing LiveOpenedAtView/StreamWarmingView tests in
test_session_state.py."""

from __future__ import annotations

from custom_components.bosch_shc_camera.session_state import (
    BoolFieldView,
    CacheFieldView,
    CameraSessionState,
    FloatFieldView,
    _Unset,
)

CAM_A = "cam-a"
CAM_B = "cam-b"


class TestUnsetSentinelRepr:
    def test_repr(self):
        assert repr(_Unset()) == "<unset>"


class TestFloatFieldView:
    def test_get_missing_session_returns_default(self):
        sessions: dict[str, CameraSessionState] = {}
        view = FloatFieldView(sessions, "motion_set_at")
        assert view.get(CAM_A) is None
        assert view.get(CAM_A, 1.0) == 1.0

    def test_get_existing_session_unset_field_returns_default(self):
        sessions = {CAM_A: CameraSessionState()}
        view = FloatFieldView(sessions, "motion_set_at")
        assert view.get(CAM_A, 9.0) == 9.0

    def test_get_existing_session_set_field_returns_value(self):
        sessions = {CAM_A: CameraSessionState(motion_set_at=5.0)}
        view = FloatFieldView(sessions, "motion_set_at")
        assert view.get(CAM_A) == 5.0

    def test_setitem_creates_session_and_sets_value(self):
        sessions: dict[str, CameraSessionState] = {}
        view = FloatFieldView(sessions, "motion_set_at")
        view[CAM_A] = 3.5
        assert CAM_A in sessions
        assert sessions[CAM_A].motion_set_at == 3.5

    def test_contains_false_when_no_session(self):
        sessions: dict[str, CameraSessionState] = {}
        view = FloatFieldView(sessions, "motion_set_at")
        assert CAM_A not in view

    def test_contains_false_when_field_unset(self):
        sessions = {CAM_A: CameraSessionState()}
        view = FloatFieldView(sessions, "motion_set_at")
        assert CAM_A not in view

    def test_contains_true_when_field_set(self):
        sessions = {CAM_A: CameraSessionState(motion_set_at=1.0)}
        view = FloatFieldView(sessions, "motion_set_at")
        assert CAM_A in view

    def test_getitem_raises_keyerror_when_no_session(self):
        sessions: dict[str, CameraSessionState] = {}
        view = FloatFieldView(sessions, "motion_set_at")
        try:
            view[CAM_A]
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError")

    def test_getitem_raises_keyerror_when_field_unset(self):
        sessions = {CAM_A: CameraSessionState()}
        view = FloatFieldView(sessions, "motion_set_at")
        try:
            view[CAM_A]
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError")

    def test_getitem_returns_value_when_set(self):
        sessions = {CAM_A: CameraSessionState(motion_set_at=2.0)}
        view = FloatFieldView(sessions, "motion_set_at")
        assert view[CAM_A] == 2.0

    def test_pop_missing_session_returns_default(self):
        sessions: dict[str, CameraSessionState] = {}
        view = FloatFieldView(sessions, "motion_set_at")
        assert view.pop(CAM_A) is None
        assert view.pop(CAM_A, 4.0) == 4.0

    def test_pop_existing_session_field_unset_returns_default(self):
        sessions = {CAM_A: CameraSessionState()}
        view = FloatFieldView(sessions, "motion_set_at")
        assert view.pop(CAM_A, 6.0) == 6.0

    def test_pop_clears_value_but_keeps_session(self):
        sessions = {CAM_A: CameraSessionState(motion_set_at=8.0, generation=2)}
        view = FloatFieldView(sessions, "motion_set_at")
        val = view.pop(CAM_A)
        assert val == 8.0
        assert view.get(CAM_A) is None
        assert sessions[CAM_A].generation == 2

    def test_len_counts_only_set_fields(self):
        sessions = {
            CAM_A: CameraSessionState(motion_set_at=1.0),
            CAM_B: CameraSessionState(),
        }
        view = FloatFieldView(sessions, "motion_set_at")
        assert len(view) == 1

    def test_len_empty(self):
        assert len(FloatFieldView({}, "motion_set_at")) == 0


class TestBoolFieldView:
    def test_contains_false_when_no_session(self):
        sessions: dict[str, CameraSessionState] = {}
        view = BoolFieldView(sessions, "notif_disabled_logged")
        assert CAM_A not in view

    def test_add_and_contains(self):
        sessions: dict[str, CameraSessionState] = {}
        view = BoolFieldView(sessions, "notif_disabled_logged")
        view.add(CAM_A)
        assert CAM_A in view
        assert sessions[CAM_A].notif_disabled_logged is True

    def test_discard_missing_session_is_noop(self):
        sessions: dict[str, CameraSessionState] = {}
        view = BoolFieldView(sessions, "notif_disabled_logged")
        view.discard(CAM_A)  # must not raise
        assert CAM_A not in sessions

    def test_discard_clears_flag_but_keeps_session(self):
        sessions = {CAM_A: CameraSessionState(notif_disabled_logged=True, generation=1)}
        view = BoolFieldView(sessions, "notif_disabled_logged")
        view.discard(CAM_A)
        assert CAM_A not in view
        assert sessions[CAM_A].generation == 1

    def test_len_counts_only_true(self):
        sessions = {
            CAM_A: CameraSessionState(notif_disabled_logged=True),
            CAM_B: CameraSessionState(notif_disabled_logged=False),
        }
        view = BoolFieldView(sessions, "notif_disabled_logged")
        assert len(view) == 1

    def test_len_empty(self):
        assert len(BoolFieldView({}, "notif_disabled_logged")) == 0


class TestCacheFieldView:
    def test_delitem_raises_keyerror_when_no_session(self):
        sessions: dict[str, CameraSessionState] = {}
        view: CacheFieldView[object] = CacheFieldView(sessions, "shc_state_cache")
        try:
            del view[CAM_A]
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError")

    def test_delitem_raises_keyerror_when_field_unset(self):
        sessions = {CAM_A: CameraSessionState()}
        view: CacheFieldView[object] = CacheFieldView(sessions, "shc_state_cache")
        try:
            del view[CAM_A]
        except KeyError:
            pass
        else:
            raise AssertionError("expected KeyError")

    def test_delitem_clears_value_but_keeps_session(self):
        sessions = {CAM_A: CameraSessionState(shc_state_cache={"x": 1}, generation=5)}
        view: CacheFieldView[object] = CacheFieldView(sessions, "shc_state_cache")
        del view[CAM_A]
        assert CAM_A not in view
        assert sessions[CAM_A].generation == 5

    def test_len_counts_only_set_fields(self):
        sessions = {
            CAM_A: CameraSessionState(shc_state_cache={"x": 1}),
            CAM_B: CameraSessionState(),
        }
        view: CacheFieldView[object] = CacheFieldView(sessions, "shc_state_cache")
        assert len(view) == 1

    def test_len_empty(self):
        view: CacheFieldView[object] = CacheFieldView({}, "shc_state_cache")
        assert len(view) == 0

    def test_repr(self):
        sessions = {CAM_A: CameraSessionState(shc_state_cache={"x": 1})}
        view: CacheFieldView[object] = CacheFieldView(sessions, "shc_state_cache")
        assert repr(view) == f"CacheFieldView({{{CAM_A!r}: {{'x': 1}}}})"
