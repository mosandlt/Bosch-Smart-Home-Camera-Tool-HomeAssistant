"""Regression tests for status_compute.py — pure per-camera status-string
derivation, extracted out of coordinator.py (style audit, 2026-08-05).

Tests call the module function directly with a lightweight stub
(SimpleNamespace) standing in for the coordinator, mirroring the existing
`tests/test_quality_prefs.py` pattern — `compute_status_for` only ever
reads `coordinator.data`, never `self.hass` or coordinator-only machinery.

Input->output pins mirror the pre-extraction pins in tests/test_init.py
(`TestComputeStatusFor`, L7912-7934) and tests/test_sensor.py's
SESSION_LIMIT-passthrough pins, kept unchanged so the extraction is a
pure refactor, not a behavior change.
"""

from types import SimpleNamespace

from custom_components.bosch_shc_camera import status_compute
from custom_components.bosch_shc_camera.coordinator import BoschCameraCoordinator

CAM_A = "cam-a"


def _make_coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {"data": {}}
    base.update(overrides)
    return SimpleNamespace(**base)


class TestComputeStatusFor:
    def test_plain_online_status(self) -> None:
        coord = _make_coord(data={CAM_A: {"status": "ONLINE"}})
        assert status_compute.compute_status_for(coord, CAM_A) == "online"

    def test_online_with_trouble_disconnect_event_becomes_offline(self) -> None:
        coord = _make_coord(
            data={
                CAM_A: {
                    "status": "ONLINE",
                    "events": [{"eventType": "TROUBLE_DISCONNECT"}],
                }
            }
        )
        assert status_compute.compute_status_for(coord, CAM_A) == "offline"

    def test_plain_offline_status(self) -> None:
        coord = _make_coord(data={CAM_A: {"status": "OFFLINE"}})
        assert status_compute.compute_status_for(coord, CAM_A) == "offline"

    def test_missing_camera_falls_back_to_unknown(self) -> None:
        coord = _make_coord(data={})
        assert status_compute.compute_status_for(coord, "no-such-cam") == "unknown"

    def test_missing_status_key_falls_back_to_unknown(self) -> None:
        coord = _make_coord(data={CAM_A: {}})
        assert status_compute.compute_status_for(coord, CAM_A) == "unknown"

    def test_empty_coordinator_data_is_safe(self) -> None:
        coord = _make_coord(data=None)
        assert status_compute.compute_status_for(coord, CAM_A) == "unknown"

    def test_session_limit_status_passes_through_verbatim(self) -> None:
        """SESSION_LIMIT must not be swallowed by the online/offline branch."""
        coord = _make_coord(data={CAM_A: {"status": "SESSION_LIMIT"}})
        assert status_compute.compute_status_for(coord, CAM_A) == "session_limit"

    def test_online_with_non_trouble_event_stays_online(self) -> None:
        coord = _make_coord(
            data={CAM_A: {"status": "ONLINE", "events": [{"eventType": "MOTION"}]}}
        )
        assert status_compute.compute_status_for(coord, CAM_A) == "online"

    def test_online_with_empty_events_list_stays_online(self) -> None:
        coord = _make_coord(data={CAM_A: {"status": "ONLINE", "events": []}})
        assert status_compute.compute_status_for(coord, CAM_A) == "online"

    def test_explicit_cam_data_overrides_coordinator_data(self) -> None:
        """The `cam_data` argument lets the update-loop pass a fresh dict
        before `self.data` has been swapped by the parent class."""
        coord = _make_coord(data={CAM_A: {"status": "OFFLINE"}})
        result = status_compute.compute_status_for(
            coord, CAM_A, cam_data={"status": "ONLINE"}
        )
        assert result == "online"


class TestComputeStatusForCoordinatorDelegation:
    """Virtual-dispatch guard: BoschCameraCoordinator._compute_status_for
    must route to status_compute.compute_status_for, and be callable both
    bound and unbound (both patterns are used by call sites/tests)."""

    def test_unbound_call_matches_module_function(self) -> None:
        coord = _make_coord(data={CAM_A: {"status": "ONLINE"}})
        assert (
            BoschCameraCoordinator._compute_status_for(coord, CAM_A)  # type: ignore[arg-type]
            == status_compute.compute_status_for(coord, CAM_A)
        )

    def test_bound_method_present_on_stub_via_getattr(self) -> None:
        """tick_housekeeping.py reaches this via
        `getattr(coordinator, "_compute_status_for", None)` — confirm the
        thin delegator is a real attribute name on the class, not just a
        free function."""
        assert hasattr(BoschCameraCoordinator, "_compute_status_for")
