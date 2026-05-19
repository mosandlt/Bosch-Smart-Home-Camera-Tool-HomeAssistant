"""Coverage tests for `_async_refresh_maintenance` (v12.4.7+).

Periodic + reactive refresh helper that hits the Bosch community RSS feed
in the background. The cooldown logic and the exception-swallow path are
not exercised by the existing maintenance tests because they go through
the public RSS fetcher.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.maintenance import MaintenanceWindow


def _mw() -> MaintenanceWindow:
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


def _make_coord(
    *, last_fetch: float = float("-inf"),
    cooldown: float = 300.0,
    cache: MaintenanceWindow | None = None,
) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord._maintenance_last_fetch = last_fetch
    coord._MAINTENANCE_REACTIVE_COOLDOWN_S = cooldown
    coord._maintenance_cache = cache
    coord.hass = SimpleNamespace(data={})
    # Stub out announce side-effect so the test only exercises the refresh path.
    coord._async_maybe_announce_maintenance = AsyncMock(return_value=None)
    return coord


@pytest.mark.asyncio
class TestAsyncRefreshMaintenance:
    async def test_periodic_fetch_updates_cache(self):
        coord = _make_coord()
        new_mw = _mw()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0), \
             patch("custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                   new=AsyncMock(return_value=new_mw)), \
             patch("custom_components.bosch_shc_camera.async_get_clientsession",
                   return_value=object()):
            await BoschCameraCoordinator._async_refresh_maintenance(coord, reactive=False)
        assert coord._maintenance_cache is new_mw
        assert coord._maintenance_last_fetch == 1000.0
        coord._async_maybe_announce_maintenance.assert_awaited_once_with(new_mw)

    async def test_reactive_within_cooldown_is_noop(self):
        coord = _make_coord(last_fetch=950.0, cooldown=300.0)
        fetch_mock = AsyncMock(return_value=_mw())
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0), \
             patch("custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                   new=fetch_mock):
            await BoschCameraCoordinator._async_refresh_maintenance(coord, reactive=True)
        fetch_mock.assert_not_awaited()
        # Cache untouched, last_fetch untouched (we returned before stamping).
        assert coord._maintenance_cache is None
        assert coord._maintenance_last_fetch == 950.0

    async def test_reactive_outside_cooldown_runs(self):
        coord = _make_coord(last_fetch=500.0, cooldown=300.0)
        new_mw = _mw()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0), \
             patch("custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                   new=AsyncMock(return_value=new_mw)), \
             patch("custom_components.bosch_shc_camera.async_get_clientsession",
                   return_value=object()):
            await BoschCameraCoordinator._async_refresh_maintenance(coord, reactive=True)
        assert coord._maintenance_cache is new_mw

    async def test_periodic_ignores_cooldown(self):
        """Cooldown gate only applies to reactive calls — periodic ticks
        always fetch when scheduled."""
        coord = _make_coord(last_fetch=950.0, cooldown=300.0)
        new_mw = _mw()
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0), \
             patch("custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                   new=AsyncMock(return_value=new_mw)), \
             patch("custom_components.bosch_shc_camera.async_get_clientsession",
                   return_value=object()):
            await BoschCameraCoordinator._async_refresh_maintenance(coord, reactive=False)
        assert coord._maintenance_cache is new_mw

    async def test_fetch_exception_keeps_previous_cache(self):
        previous = _mw()
        coord = _make_coord(cache=previous)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0), \
             patch("custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                   new=AsyncMock(side_effect=RuntimeError("network broken"))), \
             patch("custom_components.bosch_shc_camera.async_get_clientsession",
                   return_value=object()):
            # Must not raise.
            await BoschCameraCoordinator._async_refresh_maintenance(coord, reactive=False)
        # Cache unchanged — sensor stays stable across community-site outage.
        assert coord._maintenance_cache is previous
        coord._async_maybe_announce_maintenance.assert_not_awaited()

    async def test_fetch_returns_none_keeps_previous_cache(self):
        previous = _mw()
        coord = _make_coord(cache=previous)
        with patch("custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0), \
             patch("custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                   new=AsyncMock(return_value=None)), \
             patch("custom_components.bosch_shc_camera.async_get_clientsession",
                   return_value=object()):
            await BoschCameraCoordinator._async_refresh_maintenance(coord, reactive=False)
        assert coord._maintenance_cache is previous
        coord._async_maybe_announce_maintenance.assert_not_awaited()
