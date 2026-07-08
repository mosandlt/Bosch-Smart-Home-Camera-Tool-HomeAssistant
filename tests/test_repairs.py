"""Tests for repairs.py — Repairs "Fix" flow for firmware_update_available.

Pins every mode:
  - async_create_fix_flow resolves the coordinator + cam_id from the issue's
    stashed `data` and returns a FirmwareUpdateRepairFlow
  - no user_input yet -> shows the confirm form with camera/latest placeholders
  - confirm -> calls coordinator.async_install_firmware(cam_id), then
    async_create_entry
  - coordinator raises HomeAssistantError -> async_abort("install_failed")
  - no coordinator resolvable (e.g. entry unloaded) -> async_abort immediately,
    never touches user_input or calls the coordinator
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        _firmware_cache={
            CAM_ID: {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}
        },
        async_install_firmware=AsyncMock(return_value=None),
    )


class TestAsyncCreateFixFlow:
    @pytest.mark.asyncio
    async def test_resolves_coordinator_and_cam_id(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
            async_create_fix_flow,
        )

        coord = _make_coord()
        entry = SimpleNamespace(runtime_data=coord)
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_loaded_entries=lambda domain: [entry])
        )

        flow = await async_create_fix_flow(
            hass, "firmware_update_available_" + CAM_ID, {"cam_id": CAM_ID}
        )

        assert isinstance(flow, FirmwareUpdateRepairFlow)
        assert flow._coordinator is coord
        assert flow._cam_id == CAM_ID

    @pytest.mark.asyncio
    async def test_no_loaded_entry_yields_none_coordinator(self):
        from custom_components.bosch_shc_camera.repairs import async_create_fix_flow

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_loaded_entries=lambda domain: [])
        )

        flow = await async_create_fix_flow(
            hass, "firmware_update_available_" + CAM_ID, {"cam_id": CAM_ID}
        )

        assert flow._coordinator is None
        assert flow._cam_id == CAM_ID

    @pytest.mark.asyncio
    async def test_missing_data_yields_empty_cam_id(self):
        from custom_components.bosch_shc_camera.repairs import async_create_fix_flow

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_loaded_entries=lambda domain: [])
        )

        flow = await async_create_fix_flow(hass, "firmware_update_available_x", None)

        assert flow._cam_id == ""


class TestFirmwareUpdateRepairFlow:
    @pytest.mark.asyncio
    async def test_init_step_shows_confirm_form_with_placeholders(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        coord = _make_coord()
        flow = FirmwareUpdateRepairFlow(coord, CAM_ID)

        result = await flow.async_step_init()

        assert result["type"] == "form"
        assert result["step_id"] == "confirm"
        assert result["description_placeholders"] == {
            "camera": "Terrasse",
            "current": "9.40.102",
            "latest": "9.40.104",
        }
        coord.async_install_firmware.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_installs_and_creates_entry(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        coord = _make_coord()
        flow = FirmwareUpdateRepairFlow(coord, CAM_ID)

        result = await flow.async_step_confirm(user_input={})

        coord.async_install_firmware.assert_awaited_once_with(CAM_ID)
        assert result["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_confirm_install_failure_aborts(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        coord = _make_coord()
        coord.async_install_firmware = AsyncMock(
            side_effect=HomeAssistantError("rejected")
        )
        flow = FirmwareUpdateRepairFlow(coord, CAM_ID)

        result = await flow.async_step_confirm(user_input={})

        assert result["type"] == "abort"
        assert result["reason"] == "install_failed"

    @pytest.mark.asyncio
    async def test_no_coordinator_aborts_immediately(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        flow = FirmwareUpdateRepairFlow(None, CAM_ID)

        result = await flow.async_step_confirm()

        assert result["type"] == "abort"
        assert result["reason"] == "install_failed"

    @pytest.mark.asyncio
    async def test_no_coordinator_never_shows_form(self):
        """Guard runs before the user_input branch — no coordinator means no
        form should ever be shown, since there's nothing to install."""
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        flow = FirmwareUpdateRepairFlow(None, CAM_ID)

        result = await flow.async_step_confirm(user_input=None)

        assert result["type"] == "abort"
