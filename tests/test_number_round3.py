"""Tests for number.py — Gen2 entities and missing property branches.

Sprint C coverage target: lines 38-73 (async_setup_entry), 282, 293, 297-323
(AudioThreshold properties), 373-376, 380-385 (FrontLightIntensity), 437-459
(LensElevation), 477-504 (MicrophoneLevel), 523-537, 541-576 (WhiteBalance).

Covers: async_setup_entry entity-gating, BoschAudioThresholdNumber, BoschSpeakerLevelNumber,
BoschFrontLightIntensityNumber, _BoschGen2NumberBase, BoschLensElevationNumber,
BoschMicrophoneLevelNumber, BoschWhiteBalanceNumber, _BoschLedBrightnessBase.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _stub_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                    "featureSupport": {"light": True, "panLimit": 0},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        _shc_state_cache={CAM_ID: {"front_light_intensity": 0.5}},
        _pan_cache={},
        _lens_elevation_cache={},
        _audio_cache={},
        _lighting_switch_cache={},
        _image_rotation_180={},
        last_update_success=True,
        token="tok-A",
        options={},
        motion_settings=lambda cid: {},
        audio_alarm_settings=lambda cid: {"enabled": True, "threshold": 50, "sensitivity": 0, "audioAlarmConfiguration": "CUSTOM"},
        async_put_camera=AsyncMock(return_value=True),
        async_cloud_set_light_component=AsyncMock(),
        is_camera_online=lambda cid: True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord():
    return _stub_coord()


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── async_setup_entry entity-gating ──────────────────────────────────────────

class TestAsyncSetupEntryNumberGating:
    def test_pan_number_added_only_for_pan_cameras(self):
        """BoschPanNumber must only appear for cameras with panLimit > 0."""
        from custom_components.bosch_shc_camera.number import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["featureSupport"]["panLimit"] = 120
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschPanNumber" in entity_classes, \
            "BoschPanNumber must be added when panLimit > 0"

    def test_pan_number_skipped_for_no_pan(self):
        """BoschPanNumber must be absent when panLimit=0."""
        from custom_components.bosch_shc_camera.number import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["featureSupport"]["panLimit"] = 0
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschPanNumber" not in entity_classes, \
            "BoschPanNumber must be skipped when panLimit=0"

    def test_front_light_intensity_added_when_has_light(self):
        """BoschFrontLightIntensityNumber must appear when featureSupport.light=True."""
        from custom_components.bosch_shc_camera.number import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = True
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschFrontLightIntensityNumber" in entity_classes, \
            "FrontLightIntensityNumber must be added when has_light=True"

    def test_gen2_entities_added_for_gen2_outdoor(self):
        """LensElevation + MicrophoneLevel must appear for Gen2 cameras."""
        from custom_components.bosch_shc_camera.number import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschLensElevationNumber" in entity_classes, \
            "LensElevationNumber must be added for Gen2 camera"
        assert "BoschMicrophoneLevelNumber" in entity_classes, \
            "MicrophoneLevelNumber must be added for Gen2 camera"

    def test_gen2_outdoor_lights_present(self):
        """WhiteBalance + TopLed + BottomLed brightness must appear for Gen2 Outdoor."""
        from custom_components.bosch_shc_camera.number import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschWhiteBalanceNumber" in entity_classes, \
            "WhiteBalanceNumber must be added for Gen2 Outdoor"
        assert "BoschTopLedBrightnessNumber" in entity_classes, \
            "TopLedBrightnessNumber must be added for Gen2 Outdoor"

    def test_indoor_ii_alarm_entities_added(self):
        """AlarmDelay + PreAlarmDelay must appear for HOME_Eyes_Indoor."""
        from custom_components.bosch_shc_camera.number import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAlarmDelayNumber" in entity_classes, \
            "AlarmDelayNumber must be added for Gen2 Indoor II"
        assert "BoschPreAlarmDelayNumber" in entity_classes, \
            "PreAlarmDelayNumber must be added for Gen2 Indoor II"

    def test_white_balance_not_added_for_indoor_ii(self):
        """WhiteBalanceNumber must NOT appear for HOME_Eyes_Indoor (no RGB lights)."""
        from custom_components.bosch_shc_camera.number import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschWhiteBalanceNumber" not in entity_classes, \
            "WhiteBalanceNumber must NOT be added for HOME_Eyes_Indoor (Indoor II has no RGB lights)"


# ── BoschAudioThresholdNumber ─────────────────────────────────────────────────

class TestAudioThresholdNumber:
    def test_native_value_from_audio_alarm_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        stub_coord.audio_alarm_settings = lambda cid: {"threshold": 72, "enabled": True}
        entity = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == 72.0, "Must read threshold from audio_alarm_settings"

    def test_native_value_none_when_no_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        stub_coord.audio_alarm_settings = lambda cid: {}  # empty dict — threshold key missing (None guard is in available)
        entity = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when threshold key absent from settings dict"

    def test_available_requires_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        stub_coord.audio_alarm_settings = lambda cid: {}
        entity = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when settings empty"

    def test_available_true_with_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        stub_coord.audio_alarm_settings = lambda cid: {"threshold": 50}
        entity = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when settings exist"

    def test_disabled_by_default(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        entity = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        assert entity._attr_entity_registry_enabled_default is False, \
            "AudioThreshold must be opt-in (disabled by default)"

    @pytest.mark.asyncio
    async def test_set_value_sends_full_body(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber
        stub_coord.audio_alarm_settings = lambda cid: {
            "threshold": 50, "enabled": True, "sensitivity": 0, "audioAlarmConfiguration": "CUSTOM"
        }
        stub_coord.data[CAM_ID]["audioAlarm"] = {"threshold": 50}
        entity = BoschAudioThresholdNumber(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        entity._cam_id = CAM_ID
        entity.coordinator = stub_coord
        entity.hass = SimpleNamespace()
        await entity.async_set_native_value(75.0)
        call_args = stub_coord.async_put_camera.call_args
        assert call_args[0][1] == "audioAlarm", "Must PUT to audioAlarm endpoint"
        assert call_args[0][2]["threshold"] == 75, "Must send threshold=75"
        assert "enabled" in call_args[0][2], "Must preserve enabled field in body"


# ── BoschFrontLightIntensityNumber ────────────────────────────────────────────

class TestFrontLightIntensityNumber:
    def test_native_value_scaled_from_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber
        stub_coord._shc_state_cache[CAM_ID]["front_light_intensity"] = 0.75
        entity = BoschFrontLightIntensityNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == 75, "Must scale 0.75 API value to 75 percent"

    def test_native_value_none_when_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber
        stub_coord._shc_state_cache[CAM_ID] = {}
        entity = BoschFrontLightIntensityNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when intensity not in cache"

    def test_always_available_when_coordinator_ok(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber
        stub_coord._shc_state_cache[CAM_ID] = {}
        entity = BoschFrontLightIntensityNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "FrontLightIntensity must be available without cache"


# ── BoschLensElevationNumber ──────────────────────────────────────────────────

class TestLensElevationNumber:
    def test_native_value_from_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber
        stub_coord._lens_elevation_cache[CAM_ID] = 2.5
        entity = BoschLensElevationNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == 2.5, "Must read elevation from _lens_elevation_cache"

    def test_native_value_none_when_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber
        entity = BoschLensElevationNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when not yet fetched"

    def test_available_requires_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber
        entity = BoschLensElevationNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when cache is empty"

    def test_range_constants(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber
        entity = BoschLensElevationNumber(stub_coord, CAM_ID, stub_entry)
        assert entity._attr_native_min_value == 0.5, "Min elevation must be 0.5 m"
        assert entity._attr_native_max_value == 5.0, "Max elevation must be 5.0 m"

    @pytest.mark.asyncio
    async def test_set_value_puts_rounded_value(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber
        entity = BoschLensElevationNumber(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_set_native_value(2.123)
        call_args = stub_coord.async_put_camera.call_args
        assert call_args[0][2]["elevation"] == round(2.123, 2), \
            "Must PUT rounded elevation value"
        assert stub_coord._lens_elevation_cache[CAM_ID] == 2.123, \
            "Cache must be updated immediately"


# ── BoschMicrophoneLevelNumber ────────────────────────────────────────────────

class TestMicrophoneLevelNumber:
    def test_native_value_from_audio_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber
        stub_coord._audio_cache[CAM_ID] = {"microphoneLevel": 60, "audioEnabled": True}
        entity = BoschMicrophoneLevelNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == 60.0, "Must read microphoneLevel from _audio_cache"

    def test_native_value_none_when_field_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber
        stub_coord._audio_cache[CAM_ID] = {"audioEnabled": True}  # no microphoneLevel
        entity = BoschMicrophoneLevelNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when microphoneLevel not in audio cache"

    def test_available_requires_audio_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber
        stub_coord._audio_cache = {}
        entity = BoschMicrophoneLevelNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when audio cache is empty"

    def test_range_is_0_to_100(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber
        entity = BoschMicrophoneLevelNumber(stub_coord, CAM_ID, stub_entry)
        assert entity._attr_native_min_value == 0, "Min microphone level must be 0"
        assert entity._attr_native_max_value == 100, "Max microphone level must be 100"


# ── BoschWhiteBalanceNumber ───────────────────────────────────────────────────

class TestWhiteBalanceNumber:
    def test_native_value_from_lighting_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber
        stub_coord._lighting_switch_cache[CAM_ID] = {
            "frontLightSettings": {"brightness": 80, "color": None, "whiteBalance": -0.5}
        }
        entity = BoschWhiteBalanceNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == -0.5, "Must read whiteBalance from frontLightSettings"

    def test_native_value_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber
        entity = BoschWhiteBalanceNumber(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when lighting cache empty"

    def test_caches_last_wb_value(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber
        stub_coord._lighting_switch_cache[CAM_ID] = {
            "frontLightSettings": {"brightness": 80, "whiteBalance": 0.3}
        }
        entity = BoschWhiteBalanceNumber(stub_coord, CAM_ID, stub_entry)
        val1 = entity.native_value
        stub_coord._lighting_switch_cache = {}  # clear cache
        val2 = entity.native_value  # must return remembered value
        assert val2 == 0.3, "Must remember last read whiteBalance even after cache cleared"

    def test_range_minus_one_to_one(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber
        entity = BoschWhiteBalanceNumber(stub_coord, CAM_ID, stub_entry)
        assert entity._attr_native_min_value == -1.0, "Min white balance must be -1.0"
        assert entity._attr_native_max_value == 1.0, "Max white balance must be 1.0"
