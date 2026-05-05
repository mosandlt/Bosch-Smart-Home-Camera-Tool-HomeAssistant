"""Tests for update.py — BoschFirmwareUpdate entity."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                }
            }
        },
        _firmware_cache={},
        last_update_success=True,
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

    def test_installed_version_uses_cache_current_if_present(self, stub_coord, stub_entry):
        stub_coord._firmware_cache[CAM_ID] = {"current": "9.41.00"}
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.installed_version == "9.41.00"

    def test_latest_version_when_up_to_date(self, stub_coord, stub_entry):
        stub_coord._firmware_cache[CAM_ID] = {
            "current": "9.40.25", "upToDate": True,
        }
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.latest_version == "9.40.25"

    def test_latest_version_when_update_available(self, stub_coord, stub_entry):
        stub_coord._firmware_cache[CAM_ID] = {
            "current": "9.40.25", "upToDate": False, "update": "9.41.00",
        }
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u.latest_version == "9.41.00"

    def test_latest_version_fallback_when_no_update_field(self, stub_coord, stub_entry):
        """Not up to date but no `update` key → 'update available' placeholder."""
        stub_coord._firmware_cache[CAM_ID] = {
            "current": "9.40.25", "upToDate": False,
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
            "upToDate": False, "updating": True, "status": "downloading",
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
