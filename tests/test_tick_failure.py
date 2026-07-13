"""Tests for tick_failure.py — dispatch functions for _async_update_data's
outer exception handlers (Phase 2 coordinator rewrite, step 1). Direct
unit tests in isolation; the end-to-end wiring through the real
_async_update_data orchestrator is covered by TestOuterExceptBranches/
TestCloudFiveHundredTriggers in test_init.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_shc_camera.tick_failure import (
    dispatch_client_error,
    dispatch_timeout,
    dispatch_update_failed,
)


def _make_coord(**overrides):
    def _create_task(coro, **kwargs):
        # Consume the coroutine so pytest doesn't warn about it being
        # unawaited — the real hass schedules it, this stub discards it.
        coro.close()
        return MagicMock()

    base = dict(
        hass=SimpleNamespace(async_create_task=MagicMock(side_effect=_create_task)),
        _async_maybe_announce_cloud_state=AsyncMock(),
        _async_refresh_maintenance=AsyncMock(),
        _async_outage_ping_all=AsyncMock(),
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    # `_spawn_tracked` mirrors BoschCameraCoordinator._spawn_tracked closely
    # enough for these direct-module unit tests: routes through
    # hass.async_create_task (already asserted on directly below) instead of
    # needing a real _bg_tasks set on this bare SimpleNamespace stub.
    coord._spawn_tracked = lambda coro, **kw: coord.hass.async_create_task(coro, **kw)
    return coord


class TestDispatchUpdateFailed:
    @pytest.mark.asyncio
    async def test_fires_cloud_down_alert(self):
        coord = _make_coord()
        await dispatch_update_failed(coord)
        coord.hass.async_create_task.assert_called_once()
        coord._async_maybe_announce_cloud_state.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_missing_announce_method_is_a_noop(self):
        """A stub coordinator without _async_maybe_announce_cloud_state
        (getattr(..., None) guard) must not crash."""
        coord = _make_coord()
        del coord._async_maybe_announce_cloud_state
        await dispatch_update_failed(coord)
        coord.hass.async_create_task.assert_not_called()


class TestDispatchTimeout:
    @pytest.mark.asyncio
    async def test_fires_maint_outage_and_cloud_down_in_order(self):
        """Order matters here (matches the original inline code): maint
        refresh, then outage ping, then cloud-down alert — verified via a
        shared call-order manager, not just three independent call_counts
        (which would pass even if the three calls were reordered)."""
        manager = MagicMock()
        coord = _make_coord()
        manager.attach_mock(coord._async_refresh_maintenance, "maint")
        manager.attach_mock(coord._async_outage_ping_all, "outage")
        manager.attach_mock(coord._async_maybe_announce_cloud_state, "cloud")

        result = await dispatch_timeout(coord)

        assert isinstance(result, UpdateFailed)
        assert "Timeout fetching camera data from Bosch cloud" in str(result)
        assert coord.hass.async_create_task.call_count == 3
        coord._async_refresh_maintenance.assert_called_once_with(reactive=True)
        coord._async_outage_ping_all.assert_called_once_with()
        coord._async_maybe_announce_cloud_state.assert_called_once_with(False)
        assert [c[0] for c in manager.mock_calls] == ["maint", "outage", "cloud"]

    @pytest.mark.asyncio
    async def test_missing_hooks_are_a_noop(self):
        coord = _make_coord()
        del coord._async_refresh_maintenance
        del coord._async_outage_ping_all
        del coord._async_maybe_announce_cloud_state
        result = await dispatch_timeout(coord)
        assert isinstance(result, UpdateFailed)
        coord.hass.async_create_task.assert_not_called()


class TestDispatchClientError:
    @pytest.mark.asyncio
    async def test_fires_cloud_down_alert_and_wraps_message(self):
        coord = _make_coord()
        err = aiohttp.ClientError("connreset")

        result = await dispatch_client_error(coord, err)

        assert isinstance(result, UpdateFailed)
        assert "Network error: connreset" in str(result)
        coord._async_maybe_announce_cloud_state.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_missing_announce_method_is_a_noop(self):
        coord = _make_coord()
        del coord._async_maybe_announce_cloud_state
        result = await dispatch_client_error(coord, aiohttp.ClientError("x"))
        assert isinstance(result, UpdateFailed)
        coord.hass.async_create_task.assert_not_called()
