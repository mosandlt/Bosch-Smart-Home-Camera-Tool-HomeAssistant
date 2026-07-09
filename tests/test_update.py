"""Tests for update.py — BoschFirmwareUpdate entity."""

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
                }
            }
        },
        _firmware_cache={},
        _firmware_set_at={},
        last_update_success=True,
        async_put_camera=AsyncMock(return_value=True),
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschFirmwareUpdate ─────────────────────────────────────────────────


class TestFirmwareUpdate:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_translation_key == "firmware_update"
        assert u._attr_unique_id.endswith("_firmware_update")

    def test_diagnostic_category(self, stub_coord, stub_entry):
        from homeassistant.helpers.entity import EntityCategory

        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_installed_version_falls_back_to_info_fw(self, stub_coord, stub_entry):
        """No firmware_cache → fallback to info.firmwareVersion."""
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.installed_version == "9.40.25"

    def test_installed_version_uses_cache_current_if_present(
        self, stub_coord, stub_entry
    ):
        stub_coord._firmware_cache[CAM_ID] = {"current": "9.41.00"}
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.installed_version == "9.41.00"

    def test_latest_version_when_up_to_date(self, stub_coord, stub_entry):
        stub_coord._firmware_cache[CAM_ID] = {
            "current": "9.40.25",
            "upToDate": True,
        }
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.latest_version == "9.40.25"

    def test_latest_version_when_update_available(self, stub_coord, stub_entry):
        stub_coord._firmware_cache[CAM_ID] = {
            "current": "9.40.25",
            "upToDate": False,
            "update": "9.41.00",
        }
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.latest_version == "9.41.00"

    def test_latest_version_fallback_when_no_update_field(self, stub_coord, stub_entry):
        """Not up to date but no `update` key → 'update available' placeholder."""
        stub_coord._firmware_cache[CAM_ID] = {
            "current": "9.40.25",
            "upToDate": False,
        }
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.latest_version == "update available"

    def test_in_progress_reflects_cache(self, stub_coord, stub_entry):
        stub_coord._firmware_cache[CAM_ID] = {"updating": True}
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.in_progress is True

    def test_in_progress_default_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.in_progress is False

    def test_available_follows_coordinator(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.available is True
        stub_coord.last_update_success = False
        assert u.available is False

    def test_extra_attrs(self, stub_coord, stub_entry):
        stub_coord._firmware_cache[CAM_ID] = {
            "upToDate": False,
            "updating": True,
            "status": "downloading",
        }
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        attrs = u.extra_state_attributes
        assert attrs["up_to_date"] is False
        assert attrs["updating"] is True
        assert attrs["status"] == "downloading"

    def test_device_info(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        info = u.device_info
        assert info["manufacturer"] == "Bosch"
        assert "Außenkamera" in info["model"]

    def test_latest_version_falls_back_when_fw_cache_empty(
        self, stub_coord, stub_entry
    ):
        """Empty fw cache → latest_version delegates to installed_version (line 80)."""
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert stub_coord._firmware_cache == {}
        assert u.latest_version == "9.40.25"

    def test_latest_version_returns_none_when_up_to_date_absent(
        self, stub_coord, stub_entry
    ):
        """Partial payload with no upToDate key → latest_version None (indeterminate).

        Previously defaulted to True → silently hid a pending update (B08 #1).
        """
        stub_coord._firmware_cache[CAM_ID] = {"current": "9.40.25"}
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.latest_version is None


class TestSetupEntry:
    """async_setup_entry creates one BoschFirmwareUpdate per cam in coordinator.data."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_one_entity_per_cam(self):
        from custom_components.bosch_shc_camera.update import (
            BoschFirmwareUpdate,
            async_setup_entry,
        )

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                    }
                },
                "22222222-OTHER": {
                    "info": {"title": "Innen", "hardwareVersion": "CAMERA_360"}
                },
            },
            _firmware_cache={},
            last_update_success=True,
        )
        entry = SimpleNamespace(
            entry_id="01ENTRY", data={}, options={}, runtime_data=coord
        )
        captured: list = []

        def add_entities(entities, update_before_add=False):
            captured.extend(entities)

        await async_setup_entry(
            hass=None, config_entry=entry, async_add_entities=add_entities
        )
        assert len(captured) == 2
        assert all(isinstance(e, BoschFirmwareUpdate) for e in captured)

    @pytest.mark.asyncio
    async def test_setup_entry_empty_data_yields_no_entities(self):
        from custom_components.bosch_shc_camera.update import async_setup_entry

        coord = SimpleNamespace(data={}, _firmware_cache={}, last_update_success=True)
        entry = SimpleNamespace(
            entry_id="01ENTRY", data={}, options={}, runtime_data=coord
        )
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        assert captured == []


class TestAsyncInstall:
    """async_install delegates to coordinator.async_install_firmware() — shared
    with the Repairs "Fix" flow (repairs.py). Detailed guard/write-lock
    behavior is tested against the coordinator method directly in
    tests/test_firmware_install.py; this class only pins the delegation.
    """

    @pytest.mark.asyncio
    async def test_install_delegates_to_coordinator(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        stub_coord.async_install_firmware = AsyncMock(return_value=None)
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)

        await u.async_install(version=None, backup=False)

        stub_coord.async_install_firmware.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_install_ignores_passed_version(self, stub_coord, stub_entry):
        """version param is ignored — the coordinator always targets its own
        cached `update` field, not whatever HA passes."""
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        stub_coord.async_install_firmware = AsyncMock(return_value=None)
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)

        await u.async_install(version="9.99.99", backup=False)

        stub_coord.async_install_firmware.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_install_propagates_coordinator_error(self, stub_coord, stub_entry):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        stub_coord.async_install_firmware = AsyncMock(
            side_effect=HomeAssistantError("no update available")
        )
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)

        with pytest.raises(HomeAssistantError, match="no update available"):
            await u.async_install(version=None, backup=False)

    @pytest.mark.asyncio
    async def test_supported_features_includes_install(self, stub_coord, stub_entry):
        from homeassistant.components.update import UpdateEntityFeature

        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_supported_features & UpdateEntityFeature.INSTALL

    @pytest.mark.asyncio
    async def test_supported_features_includes_progress(self, stub_coord, stub_entry):
        """Live bug (2026-07-08, Thomas): pressing Install showed no progress
        indicator at all. Root cause: without UpdateEntityFeature.PROGRESS, HA's
        own async_install_with_progress() ignores our `in_progress` property
        (which correctly tracks the coordinator's `_firmware_cache[...]['updating']`
        for the whole multi-minute on-camera flash) and instead drives an
        internal flag that's only True while async_install() itself is awaiting
        — i.e. for the single PUT call, not the following minutes of flashing.
        """
        from homeassistant.components.update import UpdateEntityFeature

        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_supported_features & UpdateEntityFeature.PROGRESS


# ── Doubled-prefix entity_id naming regression ──────────────────────────
#
# Source: Andrew75 forum post 998974/15 (2026-05-15) reported entity IDs
# like button.bosch_est_bosch_est_refresh_snapshot instead of
# button.bosch_est_refresh_snapshot — the same bug class also affected
# update.py's BoschFirmwareUpdate.
#
# Root cause: classes with `_attr_has_entity_name = True` AND
# `_attr_name = f"Bosch {self._cam_title} <Suffix>"` caused HA to prepend
# the device name automatically AND the code re-prepended "Bosch {title}"
# manually.
#
# Fix (v14.2.2): remove all `_attr_name` assignments; use
# `_attr_translation_key` instead so HA resolves the entity name from
# translations/en.json at runtime. `_attr_name` must be None (unset) for
# translation_key-based naming to work.


class TestFirmwareUpdateNaming:
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        name = getattr(u, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_firmware_update(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_translation_key == "firmware_update"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_has_entity_name is True
