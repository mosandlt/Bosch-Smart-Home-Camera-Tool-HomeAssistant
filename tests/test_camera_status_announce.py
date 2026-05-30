"""Tests for per-camera offline/online transition notifications.

Pin every transition path so the user gets a notification on a real
availability change but never on a transient `unknown` flap and never on
the very first observation after a HA restart.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-2222-2222-2222-222222222222"


def _make_coord(notify_service: str = "thomas") -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.options = {"alert_notify_service": notify_service}
    coord._last_camera_status = {}
    coord.data = {
        CAM_A: {"info": {"title": "Terrasse"}, "status": "ONLINE", "events": []},
        CAM_B: {"info": {"title": "Innenbereich"}, "status": "ONLINE", "events": []},
    }
    coord.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    return coord


# ── _compute_status_for: mirrors BoschCameraStatusSensor.native_value ────────


class TestComputeStatus:
    def test_online_cloud_no_events_returns_online(self):
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "ONLINE"
        coord.data[CAM_A]["events"] = []
        assert BoschCameraCoordinator._compute_status_for(coord, CAM_A) == "online"

    def test_online_cloud_but_disconnect_event_returns_offline(self):
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "ONLINE"
        coord.data[CAM_A]["events"] = [{"eventType": "TROUBLE_DISCONNECT"}]
        assert BoschCameraCoordinator._compute_status_for(coord, CAM_A) == "offline"

    def test_offline_cloud_returns_offline_regardless_of_events(self):
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "OFFLINE"
        coord.data[CAM_A]["events"] = [{"eventType": "TROUBLE_RECONNECT"}]
        assert BoschCameraCoordinator._compute_status_for(coord, CAM_A) == "offline"

    def test_missing_status_returns_unknown(self):
        coord = _make_coord()
        del coord.data[CAM_A]["status"]
        assert BoschCameraCoordinator._compute_status_for(coord, CAM_A) == "unknown"

    def test_missing_cam_id_returns_unknown(self):
        coord = _make_coord()
        assert (
            BoschCameraCoordinator._compute_status_for(coord, "no-such-cam")
            == "unknown"
        )


# ── _async_maybe_announce_camera_status: notify transitions ──────────────────


@pytest.mark.asyncio
class TestCameraStatusAnnounce:
    async def test_first_observation_is_silent(self):
        coord = _make_coord()
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "online"
        )
        coord.hass.services.async_call.assert_not_called()
        assert coord._last_camera_status[CAM_A] == "online"

    async def test_no_change_is_silent(self):
        coord = _make_coord()
        coord._last_camera_status[CAM_A] = "online"
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "online"
        )
        coord.hass.services.async_call.assert_not_called()

    async def test_online_to_offline_announces(self):
        coord = _make_coord()
        coord._last_camera_status[CAM_A] = "online"
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "offline"
        )
        coord.hass.services.async_call.assert_called_once()
        args = coord.hass.services.async_call.await_args
        assert args.args[0] == "notify"
        assert args.args[1] == "thomas"
        assert "offline" in args.args[2]["title"].lower()
        assert "Terrasse" in args.args[2]["title"]
        assert coord._last_camera_status[CAM_A] == "offline"

    async def test_offline_to_online_announces_recovery(self):
        coord = _make_coord()
        coord._last_camera_status[CAM_A] = "offline"
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "online"
        )
        coord.hass.services.async_call.assert_called_once()
        title = coord.hass.services.async_call.await_args.args[2]["title"]
        assert "online" in title.lower() or "wieder" in title.lower()

    async def test_unknown_transitions_are_silent(self):
        """Coordinator flaps to UNKNOWN on a transient cloud hickup. Treat as
        non-event so users do not get a notification spam during outages."""
        coord = _make_coord()
        coord._last_camera_status[CAM_A] = "online"
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "unknown"
        )
        coord.hass.services.async_call.assert_not_called()
        assert coord._last_camera_status[CAM_A] == "unknown"
        # Now flap back — still silent (unknown → online is not a real
        # availability change, just metadata recovery).
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "online"
        )
        coord.hass.services.async_call.assert_not_called()

    async def test_no_service_configured_still_records_baseline(self):
        coord = _make_coord(notify_service="")
        coord._last_camera_status[CAM_A] = "online"
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "offline"
        )
        coord.hass.services.async_call.assert_not_called()
        # Status was still recorded so the next genuine change announces.
        assert coord._last_camera_status[CAM_A] == "offline"

    async def test_notify_failure_is_swallowed(self):
        coord = _make_coord()
        coord._last_camera_status[CAM_A] = "online"
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("svc down"))
        # Must not raise — coordinator update loop should not be brittle to
        # a misconfigured notify service.
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "offline"
        )
        # State still recorded so we do not retry-storm on every tick.
        assert coord._last_camera_status[CAM_A] == "offline"

    async def test_per_camera_state_is_isolated(self):
        """Two cameras flipping independently each get their own announcement."""
        coord = _make_coord()
        coord._last_camera_status = {CAM_A: "online", CAM_B: "online"}
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "offline"
        )
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_B, "offline"
        )
        assert coord.hass.services.async_call.await_count == 2
        titles = {
            c.args[2]["title"] for c in coord.hass.services.async_call.await_args_list
        }
        assert any("Terrasse" in t for t in titles)
        assert any("Innenbereich" in t for t in titles)

    async def test_multiple_services_all_called(self):
        coord = _make_coord(notify_service="thomas, signalhome")
        coord._last_camera_status[CAM_A] = "online"
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "offline"
        )
        assert coord.hass.services.async_call.await_count == 2
        services = {c.args[1] for c in coord.hass.services.async_call.await_args_list}
        assert services == {"thomas", "signalhome"}

    async def test_cam_name_fallback_to_id_prefix_when_no_title(self):
        coord = _make_coord()
        coord.data[CAM_A]["info"] = {}  # no title
        coord._last_camera_status[CAM_A] = "online"
        await BoschCameraCoordinator._async_maybe_announce_camera_status(
            coord, CAM_A, "offline"
        )
        title = coord.hass.services.async_call.await_args.args[2]["title"]
        assert CAM_A[:8] in title
