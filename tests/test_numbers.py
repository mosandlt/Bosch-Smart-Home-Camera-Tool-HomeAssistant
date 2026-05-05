"""Tests for number.py entity classes (584 LOC, 0% covered).

Most number entities follow the same pattern:
  - read coordinator cache for native_value
  - write via coordinator method on async_set_native_value
  - available iff cache populated AND coordinator success

Also tests the rotation-180 sign-inversion for the pan slider — added in
v10.6.0 for ceiling-mounted cameras. The test confirms the user-visible
direction matches what they expect.
"""

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
            }
        },
        _pan_cache={},
        _image_rotation_180={},
        _front_light_intensity_cache={CAM_ID: 0.5},
        _front_light_color_temp_cache={CAM_ID: 4000},
        _top_led_brightness_cache={CAM_ID: 0.7},
        _bottom_led_brightness_cache={CAM_ID: 0.3},
        _ledlight_brightness_cache={CAM_ID: 80},
        _mounting_height_cache={CAM_ID: 2.5},
        _mic_level_cache={CAM_ID: 50},
        _speaker_level_cache={CAM_ID: 75},
        _white_balance_cache={CAM_ID: 5000},
        _audio_alarm_cache={CAM_ID: {"enabled": True, "threshold": 65}},
        _motion_light_sensitivity_cache={CAM_ID: 0.6},
        _darkness_threshold_cache={CAM_ID: 0.3},
        _power_led_brightness_cache={CAM_ID: 0.5},
        _alarm_delay_cache={CAM_ID: {"alarmDelay": 30}},
        last_update_success=True,
        async_cloud_set_pan=AsyncMock(),
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschPanNumber ──────────────────────────────────────────────────────


class TestPanNumber:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n._attr_translation_key == "pan_position"
        assert n._attr_native_min_value == -120
        assert n._attr_native_max_value == 120

    def test_native_value_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value is None

    def test_native_value_reads_cache(self, stub_coord, stub_entry):
        stub_coord._pan_cache[CAM_ID] = 30
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value == 30

    def test_unavailable_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.available is False

    def test_available_when_cache_populated(self, stub_coord, stub_entry):
        stub_coord._pan_cache[CAM_ID] = 0
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.available is True

    def test_rotation_180_inverts_sign_on_read(self, stub_coord, stub_entry):
        """Ceiling-mounted: cam-physical +30° → user-visible -30°."""
        stub_coord._pan_cache[CAM_ID] = 30
        stub_coord._image_rotation_180[CAM_ID] = True
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value == -30

    def test_rotation_180_off_no_inversion(self, stub_coord, stub_entry):
        stub_coord._pan_cache[CAM_ID] = 30
        stub_coord._image_rotation_180[CAM_ID] = False
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value == 30

    @pytest.mark.asyncio
    async def test_set_value_inverts_when_rotated(self, stub_coord, stub_entry):
        """User drags slider to +50 (right) on ceiling-mounted cam → send -50 to camera."""
        stub_coord._image_rotation_180[CAM_ID] = True
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        await n.async_set_native_value(50)
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, -50)

    @pytest.mark.asyncio
    async def test_set_value_no_invert_when_not_rotated(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschPanNumber
        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        await n.async_set_native_value(50)
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 50)


# ── BoschAudioThresholdNumber ───────────────────────────────────────────


class TestAudioThresholdNumber:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        n = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        # threshold range 0-100 dB
        assert n._attr_native_min_value == 0
        assert n._attr_native_max_value == 100
        assert n._attr_native_unit_of_measurement == "dB"

    def test_disabled_by_default(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        n = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        assert n._attr_entity_registry_enabled_default is False


# ── BoschSpeakerLevelNumber ─────────────────────────────────────────────


class TestSpeakerLevelNumber:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber
        n = BoschSpeakerLevelNumber(stub_coord, CAM_ID, stub_entry)
        # Just verify the entity instantiates without error
        assert n is not None


# ── BoschFrontLightIntensityNumber ──────────────────────────────────────


class TestFrontLightIntensityNumber:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber
        n = BoschFrontLightIntensityNumber(stub_coord, CAM_ID, stub_entry)
        assert n is not None
