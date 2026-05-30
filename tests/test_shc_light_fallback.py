"""Tests for the Gen2 LOCAL RCP front-light fallback in shc.py (v12.4.10).

When the Bosch cloud fails to set a front-light component, the integration
falls through to a direct RCP-LAN write (0x0c22 LED dimmer). Pins:

  - boolean `front` toggle maps to brightness 100 (on) / 0 (off)
  - `intensity` accepts both int 0-100 and float 0.0-1.0
  - wallwasher does NOT enter the fallback (payload too complex)
  - cache + `_local_write_at` stamped on success
  - `coordinator.async_update_listeners` fired so the UI re-reads
  - RCP-write failure returns False without touching the cache
  - Gen1 cams skip the fallback entirely
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord():
    coord = SimpleNamespace()
    # hass.data needed for async_get_clientsession (v12.4.10+ light fallback
    # now pre-allocates the session before checking token, so the test must
    # provide a minimal stub).
    coord.hass = SimpleNamespace(data={})
    coord.token = None  # no token → cloud branch is skipped, fallback only path
    coord._cached_status = {CAM_ID: "OFFLINE"}  # cloud-skip shortcut
    coord._shc_state_cache = {}
    coord._light_set_at = {}
    coord._local_write_at = {}
    coord._local_creds_cache = {}
    coord._rcp_lan_ip_cache = {CAM_ID: "192.0.2.10"}
    coord._hw_version = {CAM_ID: "HOME_Eyes_Outdoor"}  # Gen2
    coord.async_update_listeners = lambda: None
    coord.options = {}
    return coord


@pytest.fixture
def gen1_coord(stub_coord):
    stub_coord._hw_version = {CAM_ID: "OUTDOOR"}  # Gen1
    return stub_coord


@pytest.mark.asyncio
class TestGen2LocalRcpLightFallback:
    async def test_front_true_writes_brightness_100(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                stub_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is True
        mock_write.assert_awaited_once()
        # (hass, cam_host, brightness)
        assert mock_write.await_args.args[1] == "192.0.2.10"
        assert mock_write.await_args.args[2] == 100
        # Cache updated
        assert stub_coord._shc_state_cache[CAM_ID]["front_light"] is True
        # _local_write_at stamped for grace-period helper
        assert CAM_ID in stub_coord._local_write_at

    async def test_front_false_writes_brightness_0(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                stub_coord,
                CAM_ID,
                "front",
                False,
            )
        assert ok is True
        assert mock_write.await_args.args[2] == 0
        assert stub_coord._shc_state_cache[CAM_ID]["front_light"] is False

    async def test_intensity_float_maps_to_percent(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                stub_coord,
                CAM_ID,
                "intensity",
                0.5,
            )
        assert ok is True
        assert mock_write.await_args.args[2] == 50
        assert stub_coord._shc_state_cache[CAM_ID]["front_light_intensity"] == 0.5

    async def test_intensity_int_passes_through(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                stub_coord,
                CAM_ID,
                "intensity",
                75,
            )
        assert ok is True
        assert mock_write.await_args.args[2] == 75
        assert stub_coord._shc_state_cache[CAM_ID]["front_light_intensity"] == 75

    async def test_camera_light_flag_recomputed_after_local_write(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        stub_coord._shc_state_cache[CAM_ID] = {"wallwasher": False}
        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            await shc.async_cloud_set_light_component(stub_coord, CAM_ID, "front", True)
        assert stub_coord._shc_state_cache[CAM_ID]["camera_light"] is True

    async def test_rcp_failure_returns_false(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=False)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                stub_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is False
        # Cache NOT updated on failure
        assert "front_light" not in stub_coord._shc_state_cache.get(CAM_ID, {})

    async def test_wallwasher_skips_local_fallback(self, stub_coord):
        """Wallwasher write payload is too complex for the unauthenticated
        RCP path — must fall through without touching the camera."""
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                stub_coord,
                CAM_ID,
                "wallwasher",
                True,
            )
        assert ok is False
        mock_write.assert_not_awaited()

    async def test_gen1_skips_local_fallback(self, gen1_coord):
        """Gen1 cams never enter the LOCAL RCP fallback — auth model is
        different and the writes have not been verified there."""
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                gen1_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is False
        mock_write.assert_not_awaited()

    async def test_no_lan_ip_skips_fallback(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        stub_coord._rcp_lan_ip_cache = {}
        stub_coord._local_creds_cache = {}
        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                stub_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is False
        mock_write.assert_not_awaited()

    async def test_prefers_local_creds_host_over_rcp_cache(self, stub_coord):
        from custom_components.bosch_shc_camera import shc

        stub_coord._local_creds_cache[CAM_ID] = {"host": "10.0.0.5"}
        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            await shc.async_cloud_set_light_component(stub_coord, CAM_ID, "front", True)
        # local_creds.host wins over _rcp_lan_ip_cache
        assert mock_write.await_args.args[1] == "10.0.0.5"
