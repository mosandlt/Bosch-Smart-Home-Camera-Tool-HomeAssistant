"""Tests for button entity classes (button.py — 78 LOC, 2 entity types)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    assert "Terrasse" in btn._attr_name


def test_refresh_button_device_info(stub_coord, stub_entry):
    """device_info propagates model name + firmware + mac."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["manufacturer"] == "Bosch"
    assert info["sw_version"] == "9.40.25"
    assert "Außenkamera" in info["model"]
    assert info["connections"] == {("mac", "64:da:a0:33:14:ae")}


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
