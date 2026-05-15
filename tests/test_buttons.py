"""Tests for button entity classes (button.py — 78 LOC, 2 entity types)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:33:14:ae",
                },
                "status": "ONLINE",
            }
        },
        _camera_entities={},
        async_request_refresh=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschRefreshSnapshotButton ──────────────────────────────────────────


def test_refresh_button_construction(stub_coord, stub_entry):
    """Refresh button instantiates with the expected unique_id + name."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    assert btn._attr_unique_id.startswith("bosch_shc_refresh_")
    assert btn._attr_translation_key == "refresh_snapshot"
    # v12.3.0 — `_attr_name` is the bare suffix; HA prepends the device
    # name automatically via `_attr_has_entity_name=True`. The cam title
    # lives only in `device_info["name"]`.
    assert btn._attr_name == "Refresh Snapshot"


def test_refresh_button_device_info(stub_coord, stub_entry):
    """device_info propagates model name + firmware + mac."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["manufacturer"] == "Bosch"
    assert info["sw_version"] == "9.40.25"
    assert "Außenkamera" in info["model"]
    assert info["connections"] == {("mac", "aa:bb:cc:33:14:ae")}


# ── BoschAcousticAlarmButton ────────────────────────────────────────────


def test_acoustic_alarm_button_disabled_by_default(stub_coord, stub_entry):
    """Siren button starts hidden — `_attr_entity_registry_enabled_default = False`."""
    from custom_components.bosch_shc_camera.button import BoschAcousticAlarmButton
    btn = BoschAcousticAlarmButton(stub_coord, CAM_ID, stub_entry)
    assert btn._attr_entity_registry_enabled_default is False


def test_acoustic_alarm_button_construction(stub_coord, stub_entry):
    from custom_components.bosch_shc_camera.button import BoschAcousticAlarmButton
    btn = BoschAcousticAlarmButton(stub_coord, CAM_ID, stub_entry)
    assert btn._attr_translation_key == "acoustic_alarm"
    assert btn._attr_unique_id.startswith("bosch_shc_siren_")


def test_acoustic_alarm_button_in_config_category(stub_coord, stub_entry):
    """Siren button must be in the CONFIG entity category, not in the default UI."""
    from homeassistant.helpers.entity import EntityCategory
    from custom_components.bosch_shc_camera.button import BoschAcousticAlarmButton
    btn = BoschAcousticAlarmButton(stub_coord, CAM_ID, stub_entry)
    assert btn._attr_entity_category == EntityCategory.CONFIG


# ── No mac → no connection entry ────────────────────────────────────────


def test_device_info_no_mac_skipped(stub_coord, stub_entry):
    """Empty mac → device_info connections is an empty set."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
    stub_coord.data[CAM_ID]["info"]["macAddress"] = ""
    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["connections"] == set()


def test_acoustic_alarm_device_info(stub_coord, stub_entry):
    """BoschAcousticAlarmButton.device_info propagates identifiers and manufacturer."""
    from custom_components.bosch_shc_camera.button import BoschAcousticAlarmButton
    from custom_components.bosch_shc_camera import DOMAIN
    btn = BoschAcousticAlarmButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["manufacturer"] == "Bosch"
    assert (DOMAIN, CAM_ID) in info["identifiers"]
    assert info["sw_version"] == "9.40.25"


# ── async_press ─────────────────────────────────────────────────────────


class TestRefreshSnapshotPress:
    @pytest.mark.asyncio
    async def test_press_schedules_coordinator_refresh(self, stub_coord, stub_entry):
        """async_press must schedule coordinator.async_request_refresh via hass.async_create_task."""
        from unittest.mock import MagicMock
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        tasks_created = []
        fake_hass = MagicMock()
        fake_hass.async_create_task = lambda coro: tasks_created.append(coro)
        btn.hass = fake_hass
        await btn.async_press()
        assert len(tasks_created) >= 1

    @pytest.mark.asyncio
    async def test_press_also_triggers_image_refresh_when_cam_entity_present(self, stub_coord, stub_entry):
        """When a camera entity is registered, async_press also schedules its image refresh."""
        from unittest.mock import MagicMock, AsyncMock
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
        fake_cam = MagicMock()
        fake_cam._async_trigger_image_refresh = AsyncMock(return_value=None)
        stub_coord._camera_entities[CAM_ID] = fake_cam

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        tasks_created = []
        fake_hass = MagicMock()
        fake_hass.async_create_task = lambda coro: tasks_created.append(coro)
        btn.hass = fake_hass
        await btn.async_press()
        assert len(tasks_created) == 2


class TestAcousticAlarmPress:
    @pytest.mark.asyncio
    async def test_press_calls_put_camera_acoustic_alarm(self, stub_coord, stub_entry):
        """async_press must call coordinator.async_put_camera with acoustic_alarm payload."""
        from custom_components.bosch_shc_camera.button import BoschAcousticAlarmButton
        btn = BoschAcousticAlarmButton(stub_coord, CAM_ID, stub_entry)
        from unittest.mock import MagicMock
        btn.hass = MagicMock()
        await btn.async_press()
        stub_coord.async_put_camera.assert_called_once_with(
            CAM_ID, "acoustic_alarm", {"enabled": True}
        )

    @pytest.mark.asyncio
    async def test_press_logs_warning_on_false_return(self, stub_coord, stub_entry):
        """Non-success return from put_camera → warning logged, no exception raised."""
        from custom_components.bosch_shc_camera.button import BoschAcousticAlarmButton
        from unittest.mock import MagicMock
        stub_coord.async_put_camera = AsyncMock(return_value=False)
        btn = BoschAcousticAlarmButton(stub_coord, CAM_ID, stub_entry)
        btn.hass = MagicMock()
        await btn.async_press()  # must not raise

    @pytest.mark.asyncio
    async def test_press_swallows_exception(self, stub_coord, stub_entry):
        """Exception from async_put_camera is logged as error and not re-raised."""
        from custom_components.bosch_shc_camera.button import BoschAcousticAlarmButton
        from unittest.mock import MagicMock
        stub_coord.async_put_camera = AsyncMock(side_effect=RuntimeError("boom"))
        btn = BoschAcousticAlarmButton(stub_coord, CAM_ID, stub_entry)
        btn.hass = MagicMock()
        try:
            await btn.async_press()
        except RuntimeError:
            pytest.fail("async_press must swallow RuntimeError from async_put_camera")


# ── async_setup_entry ────────────────────────────────────────────────────


class TestSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_one_button_per_camera(self, stub_coord, stub_entry):
        """Default options → 1 button entity per camera (Refresh only).

        BoschAcousticAlarmButton was removed from async_setup_entry in v12.0.4:
        Gen1 cameras have no integrated siren, Gen2 cameras use the panic_alarm
        endpoint via switch.py BoschPanicAlarmSwitch instead.
        """
        from custom_components.bosch_shc_camera.button import (
            async_setup_entry, BoschRefreshSnapshotButton, BoschAcousticAlarmButton,
        )
        stub_entry.runtime_data = stub_coord
        captured: list = []
        await async_setup_entry(hass=None, config_entry=stub_entry,
                                async_add_entities=lambda e, update_before_add=False: captured.extend(e))
        types_ = {type(e) for e in captured}
        assert BoschRefreshSnapshotButton in types_
        assert BoschAcousticAlarmButton not in types_, (
            "AcousticAlarmButton must not be instantiated — Gen1 has no siren, "
            "Gen2 uses BoschPanicAlarmSwitch via /panic_alarm endpoint"
        )
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_skips_all_buttons_when_disabled_in_options(self, stub_coord, stub_entry):
        """enable_snapshot_button=False → setup_entry returns early, no entities created."""
        from custom_components.bosch_shc_camera.button import async_setup_entry
        stub_entry.options = {"enable_snapshot_button": False}
        stub_entry.data = {}
        stub_entry.runtime_data = stub_coord
        captured: list = []
        await async_setup_entry(hass=None, config_entry=stub_entry,
                                async_add_entities=lambda e, update_before_add=False: captured.extend(e))
        assert captured == []
