"""Regression tests for BoschCameraCoordinator.async_soft_reset_camera() /
async_hard_reset_camera(), and the button entities that call them.

Reverse-engineered from the official Bosch app (research/apk_2.12.0
decompile): BackendUrlProviderService.GetCameraSoftResetUrl /
GetCameraHardResetUrl -> bodyless PUT video_inputs/{id}/soft_reset and
.../hard_reset. Applies to all camera generations (gating in the app is
"camera online + not shared", not hardware-version-specific).

Pins:
  - both methods PUT the correct endpoint with an empty body
  - both raise HomeAssistantError when the cloud rejects the PUT
  - the hard-reset button is disabled by default (destructive — requires
    re-pairing the camera in the Bosch app afterward)
  - the soft-reset button is NOT disabled by default (non-destructive)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord() -> SimpleNamespace:
    return SimpleNamespace(async_put_camera=AsyncMock(return_value=True))


class TestAsyncSoftResetCamera:
    @pytest.mark.asyncio
    async def test_puts_soft_reset_endpoint_with_empty_body(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        await BoschCameraCoordinator.async_soft_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]
        coord.async_put_camera.assert_awaited_once_with(CAM_ID, "soft_reset", None)

    @pytest.mark.asyncio
    async def test_cloud_rejects_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.async_put_camera.return_value = False
        with pytest.raises(HomeAssistantError, match="soft-reset"):
            await BoschCameraCoordinator.async_soft_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]


class TestAsyncHardResetCamera:
    @pytest.mark.asyncio
    async def test_puts_hard_reset_endpoint_with_empty_body(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        await BoschCameraCoordinator.async_hard_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]
        coord.async_put_camera.assert_awaited_once_with(CAM_ID, "hard_reset", None)

    @pytest.mark.asyncio
    async def test_cloud_rejects_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.async_put_camera.return_value = False
        with pytest.raises(HomeAssistantError, match="hard-reset"):
            await BoschCameraCoordinator.async_hard_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]


def _make_button_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {"info": {"title": "Terrasse", "hardwareVersion": "CAMERA_EYES"}}
        },
        async_soft_reset_camera=AsyncMock(return_value=None),
        async_hard_reset_camera=AsyncMock(return_value=None),
    )


class TestButtonEntities:
    def test_soft_reset_button_disabled_by_default(self):
        """Live-tested 2026-07-08: Bosch's cloud rejects this with HTTP 404
        sh:entity.notfound despite the request matching the app exactly —
        disabled by default until Bosch's backend actually supports it."""
        from custom_components.bosch_shc_camera.button import BoschSoftResetButton

        btn = BoschSoftResetButton(_make_button_coord(), CAM_ID, MagicMock())
        assert btn._attr_entity_registry_enabled_default is False

    def test_hard_reset_button_disabled_by_default(self):
        """Destructive action — must not be silently enabled for every user."""
        from custom_components.bosch_shc_camera.button import BoschHardResetButton

        btn = BoschHardResetButton(_make_button_coord(), CAM_ID, MagicMock())
        assert btn._attr_entity_registry_enabled_default is False

    @pytest.mark.asyncio
    async def test_soft_reset_button_press_calls_coordinator(self):
        from custom_components.bosch_shc_camera.button import BoschSoftResetButton

        coord = _make_button_coord()
        btn = BoschSoftResetButton(coord, CAM_ID, MagicMock())
        await btn.async_press()
        coord.async_soft_reset_camera.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_hard_reset_button_press_calls_coordinator(self):
        from custom_components.bosch_shc_camera.button import BoschHardResetButton

        coord = _make_button_coord()
        btn = BoschHardResetButton(coord, CAM_ID, MagicMock())
        await btn.async_press()
        coord.async_hard_reset_camera.assert_awaited_once_with(CAM_ID)
