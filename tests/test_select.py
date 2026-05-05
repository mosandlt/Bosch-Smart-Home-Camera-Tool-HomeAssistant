"""Tests for select.py entity classes (Video Quality, Stream Mode, FCM Mode)."""

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
                },
                "live": {},
            }
        },
        get_quality=lambda cid: "auto",
        set_quality=lambda cid, q: None,
        options={
            "fcm_push_mode": "auto",
            "stream_connection_type": "auto",
        },
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschVideoQualitySelect ──────────────────────────────────────────────


class TestVideoQualitySelect:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "video_quality"
        assert sel._attr_unique_id.endswith("_video_quality")

    def test_current_option_reads_coordinator(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "auto"

    def test_current_option_falls_back_to_auto_for_unknown(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        stub_coord.get_quality = lambda cid: "weird-not-an-option"
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "auto"

    def test_options_list_present(self, stub_coord, stub_entry):
        """A select entity must have a non-empty _attr_options."""
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert len(sel._attr_options) >= 2
        assert "auto" in sel._attr_options


# ── BoschFcmPushModeSelect ──────────────────────────────────────────────


class TestFcmPushModeSelect:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        # FCM mode select binds to the integration, not per-camera
        assert sel._attr_options


# ── BoschStreamModeSelect ──────────────────────────────────────────────


class TestStreamModeSelect:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_options


# ── BoschMotionSensitivitySelect ─────────────────────────────────────────


class TestMotionSensitivitySelect:
    def test_disabled_by_default(self, stub_coord, stub_entry):
        """Motion-sensitivity select is hidden by default — disabled_by_default."""
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_entity_registry_enabled_default is False
