"""Tests for session_state.py — CameraSessionState and the StreamWarmingView/
LiveOpenedAtView facades that preserve the external `_stream_warming: set[str]`
/`_live_opened_at: dict[str, float]` contracts camera.py relies on (Phase 1
slice 2 of the coordinator rewrite — see .claude/plans/jiggly-moseying-peacock.md)."""

from __future__ import annotations

from custom_components.bosch_shc_camera.session_state import (
    CameraSessionState,
    LiveOpenedAtView,
    StreamWarmingView,
    get_or_create_session,
)

CAM_A = "cam-a"
CAM_B = "cam-b"


class TestCameraSessionStateDefaults:
    def test_default_field_values(self):
        session = CameraSessionState()
        assert session.generation == 0
        assert session.idle_since is None
        assert session.warming_started == float("-inf")
        assert session.warming is False
        assert session.opened_at is None


class TestGetOrCreateSession:
    def test_creates_new_session_for_unknown_cam(self):
        store: dict[str, CameraSessionState] = {}
        session = get_or_create_session(store, CAM_A)
        assert isinstance(session, CameraSessionState)
        assert store[CAM_A] is session

    def test_returns_same_session_on_second_call(self):
        store: dict[str, CameraSessionState] = {}
        s1 = get_or_create_session(store, CAM_A)
        s2 = get_or_create_session(store, CAM_A)
        assert s1 is s2


class TestStreamWarmingView:
    def test_unknown_cam_not_in_view(self):
        """A cam_id with no session entry at all is not "in" the view."""
        sessions: dict[str, CameraSessionState] = {}
        view = StreamWarmingView(sessions)
        assert CAM_A not in view

    def test_existing_session_with_warming_false_not_in_view(self):
        """A session object that exists but has warming=False must still
        report `not in` — existence of the session alone isn't membership."""
        sessions = {CAM_A: CameraSessionState(warming=False)}
        view = StreamWarmingView(sessions)
        assert CAM_A not in view

    def test_add_makes_cam_a_member(self):
        sessions: dict[str, CameraSessionState] = {}
        view = StreamWarmingView(sessions)
        view.add(CAM_A)
        assert CAM_A in view
        assert sessions[CAM_A].warming is True

    def test_discard_on_unknown_cam_is_a_noop(self):
        """discard() on a cam_id with no session entry must not crash or
        create one (mirrors set.discard's no-op-on-missing semantics)."""
        sessions: dict[str, CameraSessionState] = {}
        view = StreamWarmingView(sessions)
        view.discard(CAM_A)
        assert sessions == {}

    def test_discard_clears_membership(self):
        sessions = {CAM_A: CameraSessionState(warming=True)}
        view = StreamWarmingView(sessions)
        view.discard(CAM_A)
        assert CAM_A not in view
        # Session object persists (other fields like generation aren't reset)
        assert CAM_A in sessions

    def test_two_cams_independent_membership(self):
        sessions: dict[str, CameraSessionState] = {}
        view = StreamWarmingView(sessions)
        view.add(CAM_A)
        assert CAM_A in view
        assert CAM_B not in view

    def test_len_counts_only_warming_true(self):
        """len() must count only sessions with warming=True — a session
        object existing with warming=False (or no session at all for a
        cam_id) must not be counted. Regression: diagnostics.py calls
        len(coordinator._stream_warming) and crashed before __len__ existed."""
        sessions = {
            CAM_A: CameraSessionState(warming=True),
            CAM_B: CameraSessionState(warming=False),
            "cam-c": CameraSessionState(warming=True),
        }
        view = StreamWarmingView(sessions)
        assert len(view) == 2

    def test_len_empty(self):
        assert len(StreamWarmingView({})) == 0


class TestLiveOpenedAtView:
    def test_get_unknown_cam_returns_default(self):
        sessions: dict[str, CameraSessionState] = {}
        view = LiveOpenedAtView(sessions)
        assert view.get(CAM_A) is None
        assert view.get(CAM_A, 123.0) == 123.0

    def test_get_existing_session_with_opened_at_none_returns_default(self):
        """A session that exists but has opened_at=None must still return
        the default, not None-as-a-set-value confusion."""
        sessions = {CAM_A: CameraSessionState(opened_at=None)}
        view = LiveOpenedAtView(sessions)
        assert view.get(CAM_A, 99.0) == 99.0

    def test_setitem_then_get(self):
        sessions: dict[str, CameraSessionState] = {}
        view = LiveOpenedAtView(sessions)
        view[CAM_A] = 1000.0
        assert view.get(CAM_A) == 1000.0
        assert sessions[CAM_A].opened_at == 1000.0

    def test_pop_unknown_cam_returns_default_and_no_side_effect(self):
        sessions: dict[str, CameraSessionState] = {}
        view = LiveOpenedAtView(sessions)
        assert view.pop(CAM_A) is None
        assert view.pop(CAM_A, 5.0) == 5.0
        assert sessions == {}

    def test_pop_existing_session_with_opened_at_none_returns_default(self):
        sessions = {CAM_A: CameraSessionState(opened_at=None)}
        view = LiveOpenedAtView(sessions)
        assert view.pop(CAM_A, 7.0) == 7.0

    def test_pop_clears_value_but_keeps_session(self):
        sessions = {CAM_A: CameraSessionState(opened_at=42.0, generation=3)}
        view = LiveOpenedAtView(sessions)
        val = view.pop(CAM_A)
        assert val == 42.0
        assert view.get(CAM_A) is None
        # Session object persists with its other fields intact
        assert sessions[CAM_A].generation == 3

    def test_two_cams_independent(self):
        sessions: dict[str, CameraSessionState] = {}
        view = LiveOpenedAtView(sessions)
        view[CAM_A] = 1.0
        view[CAM_B] = 2.0
        assert view.get(CAM_A) == 1.0
        assert view.get(CAM_B) == 2.0

    def test_len_counts_only_opened_at_set(self):
        """len() must count only sessions with opened_at set — a session
        object existing with opened_at=None must not be counted."""
        sessions = {
            CAM_A: CameraSessionState(opened_at=1.0),
            CAM_B: CameraSessionState(opened_at=None),
            "cam-c": CameraSessionState(opened_at=2.0),
        }
        view = LiveOpenedAtView(sessions)
        assert len(view) == 2

    def test_len_empty(self):
        assert len(LiveOpenedAtView({})) == 0
