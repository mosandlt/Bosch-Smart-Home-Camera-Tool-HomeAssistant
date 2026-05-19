"""Tests for the v12.4.11 cloud-up/cloud-down transition notifier.

Pins:
- First observation (healthy or failed) is silent — baseline only.
- One-tick failure blips never fire (must persist ≥ _CLOUD_OUTAGE_NOTIFY_AFTER_S).
- Outage announcement fires exactly once when the threshold is crossed.
- Recovery fires immediately when the next success arrives after an outage.
- Active RSS maintenance suppresses both outage and recovery announcements.
- Notify-service failure is swallowed.
- No notify service configured = silent + state still tracked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.maintenance import MaintenanceWindow


def _make_coord(notify_service: str = "thomas", maintenance: MaintenanceWindow | None = None) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.options = {"alert_notify_service": notify_service}
    coord._cloud_outage_started_at = None
    coord._cloud_outage_notified = False
    coord._CLOUD_OUTAGE_NOTIFY_AFTER_S = 60.0
    coord._maintenance_cache = maintenance
    coord.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    coord._async_dispatch_cloud_alert = (
        BoschCameraCoordinator._async_dispatch_cloud_alert.__get__(coord)
    )
    return coord


def _active_maintenance() -> MaintenanceWindow:
    ref = datetime(2026, 5, 19, 7, 30, tzinfo=timezone.utc)
    return MaintenanceWindow(
        title="Wartung Kamera-Infrastruktur",
        link="https://example/x",
        pub_date=ref - timedelta(hours=12),
        summary="07:00–10:00 MESZ",
        scheduled_start=ref - timedelta(hours=1),
        scheduled_end=ref + timedelta(hours=2),
        source="rss:Wartungsarbeiten",
        camera_relevant=True,
    )


@pytest.mark.asyncio
class TestCloudStateAnnounce:
    async def test_first_success_is_silent(self):
        coord = _make_coord()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_not_called()

    async def test_first_failure_is_silent(self):
        """Single failed tick must never fire — could be a transient blip."""
        coord = _make_coord()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_not_called()
        assert coord._cloud_outage_started_at == 1000.0
        assert coord._cloud_outage_notified is False

    async def test_failure_under_threshold_stays_silent(self):
        coord = _make_coord()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1030.0):  # +30s
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_not_called()
        assert coord._cloud_outage_notified is False

    async def test_failure_past_threshold_fires_once(self):
        coord = _make_coord()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1070.0):  # +70s
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_called_once()
        args = coord.hass.services.async_call.await_args.args
        assert args[0] == "notify"
        assert args[1] == "thomas"
        assert "nicht erreichbar" in args[2]["title"].lower()
        assert coord._cloud_outage_notified is True
        # Subsequent failed ticks don't re-fire.
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1200.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        assert coord.hass.services.async_call.await_count == 1

    async def test_blip_clears_without_announcing(self):
        """One failed tick followed by a success must not announce anything."""
        coord = _make_coord()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1010.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_not_called()
        assert coord._cloud_outage_started_at is None
        assert coord._cloud_outage_notified is False

    async def test_recovery_fires_immediately(self):
        coord = _make_coord()
        coord._cloud_outage_started_at = 1000.0
        coord._cloud_outage_notified = True
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1500.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_called_once()
        title = coord.hass.services.async_call.await_args.args[2]["title"]
        assert "wieder erreichbar" in title.lower()
        assert coord._cloud_outage_notified is False
        assert coord._cloud_outage_started_at is None

    async def test_active_maintenance_suppresses_outage(self):
        coord = _make_coord(maintenance=_active_maintenance())
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0), \
             patch("custom_components.bosch_shc_camera.maintenance.datetime") as dt_mock:
            dt_mock.now.return_value = datetime(2026, 5, 19, 7, 30, tzinfo=timezone.utc)
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0):
            with patch.object(
                MaintenanceWindow, "state", return_value="active",
            ):
                await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_not_called()
        # Internal state still flipped so a recovery during maintenance does
        # not later re-fire — but no notification was sent.
        assert coord._cloud_outage_notified is True

    async def test_active_maintenance_suppresses_recovery(self):
        coord = _make_coord(maintenance=_active_maintenance())
        coord._cloud_outage_notified = True
        coord._cloud_outage_started_at = 1000.0
        with patch.object(MaintenanceWindow, "state", return_value="active"):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_not_called()
        # Tracker still reset so the next genuine outage starts fresh.
        assert coord._cloud_outage_notified is False
        assert coord._cloud_outage_started_at is None

    async def test_no_service_configured_still_tracks_state(self):
        coord = _make_coord(notify_service="")
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_not_called()
        # State still tracked so configuring a service mid-outage doesn't
        # surface a stale notification on the next failed tick.
        assert coord._cloud_outage_notified is True

    async def test_notify_failure_is_swallowed(self):
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("svc down"))
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0):
            # Must not raise.
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        # State still flipped so we do not retry-storm.
        assert coord._cloud_outage_notified is True

    async def test_multiple_services_all_called(self):
        coord = _make_coord(notify_service="thomas, signalhome")
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        assert coord.hass.services.async_call.await_count == 2
        services = {c.args[1] for c in coord.hass.services.async_call.await_args_list}
        assert services == {"thomas", "signalhome"}

    async def test_notify_prefix_stripped(self):
        """Regression for the 2026-05-19 13:42:59 bug — the option stored
        `notify.thomas` (with prefix) and the hardcoded `domain="notify"` +
        full string as service produced `notify.notify.thomas`, which HA
        rejects with `Action notify.notify.thomas not found`. Now the helper
        splits any `notify.<name>` form so the call lands as
        `domain="notify"`, `service="<name>"`."""
        coord = _make_coord(notify_service="notify.thomas")
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_called_once()
        args = coord.hass.services.async_call.await_args.args
        assert args[0] == "notify"
        assert args[1] == "thomas"  # NOT "notify.thomas"
