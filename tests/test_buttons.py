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
                    "macAddress": "aa:bb:cc:dd:ee:01",
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
    """Refresh button instantiates with the expected unique_id + translation_key."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    assert btn._attr_unique_id.startswith("bosch_shc_refresh_")
    assert btn._attr_translation_key == "refresh_snapshot"
    # v14.2.2 — _attr_name is None; HA resolves the entity name from
    # translations/en.json via _attr_translation_key at runtime.
    assert getattr(btn, "_attr_name", None) is None


def test_refresh_button_device_info(stub_coord, stub_entry):
    """device_info propagates model name + firmware + mac."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["manufacturer"] == "Bosch"
    assert info["sw_version"] == "9.40.25"
    assert "Außenkamera" in info["model"]
    assert info["connections"] == {("mac", "aa:bb:cc:dd:ee:01")}


# ── No mac → no connection entry ────────────────────────────────────────


def test_device_info_no_mac_skipped(stub_coord, stub_entry):
    """Empty mac → device_info connections is an empty set."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

    stub_coord.data[CAM_ID]["info"]["macAddress"] = ""
    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["connections"] == set()

    assert info["sw_version"] == "9.40.25"


# ── async_press ─────────────────────────────────────────────────────────


class TestRefreshSnapshotPress:
    @pytest.mark.asyncio
    async def test_press_schedules_coordinator_refresh(self, stub_coord, stub_entry):
        """async_press must schedule coordinator.async_request_refresh via hass.async_create_task."""
        from unittest.mock import MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        # MagicMock.async_create_task returns a MagicMock (has .add_done_callback)
        btn.hass = fake_hass
        await btn.async_press()
        assert fake_hass.async_create_task.call_count >= 1

    @pytest.mark.asyncio
    async def test_press_also_triggers_image_refresh_when_cam_entity_present(
        self, stub_coord, stub_entry
    ):
        """When a camera entity is registered, async_press also schedules its image refresh."""
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        fake_cam = MagicMock()
        fake_cam._async_trigger_image_refresh = AsyncMock(return_value=None)
        stub_coord._camera_entities[CAM_ID] = fake_cam

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        btn.hass = fake_hass
        await btn.async_press()
        assert fake_hass.async_create_task.call_count == 2


class TestRefreshSnapshotErrorHandling:
    """async_press attaches error-logging callbacks to the created tasks."""

    @pytest.mark.asyncio
    async def test_done_callback_attached_to_refresh_task(self, stub_coord, stub_entry):
        """A done callback must be registered on the coordinator refresh task."""
        from unittest.mock import MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        btn.hass = fake_hass
        await btn.async_press()
        task_mock = fake_hass.async_create_task.return_value
        task_mock.add_done_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_done_callback_attached_to_image_refresh_task(
        self, stub_coord, stub_entry
    ):
        """A done callback must be registered on the image refresh task too."""
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        fake_cam = MagicMock()
        fake_cam._async_trigger_image_refresh = AsyncMock(return_value=None)
        stub_coord._camera_entities[CAM_ID] = fake_cam

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        btn.hass = fake_hass
        await btn.async_press()
        # Two tasks created → each should have add_done_callback called once
        assert fake_hass.async_create_task.call_count == 2
        for (
            call
        ) in fake_hass.async_create_task.return_value.add_done_callback.call_args_list:
            assert call is not None


# ── async_setup_entry ────────────────────────────────────────────────────


class TestSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_one_button_per_camera(self, stub_coord, stub_entry):
        """Default options → 1 button entity per camera (Refresh only).

        BoschAcousticAlarmButton was removed entirely in v13.3 (was kept as
        an orphan since v12.0.4). Gen1 cameras have no integrated siren;
        Gen2 cameras use BoschPanicAlarmSwitch via /panic_alarm in switch.py.
        """
        from custom_components.bosch_shc_camera.button import (
            BoschRefreshSnapshotButton,
            async_setup_entry,
        )

        stub_entry.runtime_data = stub_coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=stub_entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        types_ = {type(e) for e in captured}
        assert BoschRefreshSnapshotButton in types_
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_skips_all_buttons_when_disabled_in_options(
        self, stub_coord, stub_entry
    ):
        """enable_snapshot_button=False → setup_entry returns early, no entities created."""
        from custom_components.bosch_shc_camera.button import async_setup_entry

        stub_entry.options = {"enable_snapshot_button": False}
        stub_entry.data = {}
        stub_entry.runtime_data = stub_coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=stub_entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        assert captured == []
