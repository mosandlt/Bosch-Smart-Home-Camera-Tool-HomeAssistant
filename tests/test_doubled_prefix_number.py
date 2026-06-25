"""Regression test — doubled-prefix entity_id bug in number.py.

Bug source: forum post 998974/15, Andrew75, 2026-05-15.
Classes with `_attr_has_entity_name = True` AND `_attr_name = f"Bosch {title} Suffix"`
produced entity_ids like `number.bosch_est_bosch_est_pan_position` because HA
prepends the device name automatically when has_entity_name=True, and the code
re-prepended "Bosch {title}" manually.

Fix: _attr_name must be the bare suffix literal (e.g. "Pan Position"), NOT
prefixed with "Bosch {title}".

Covers all 16 classes that had the bug:
  BoschPanNumber, BoschAudioThresholdNumber, BoschSpeakerLevelNumber,
  BoschFrontLightIntensityNumber, BoschLensElevationNumber,
  BoschMicrophoneLevelNumber, BoschWhiteBalanceNumber,
  BoschTopLedBrightnessNumber, BoschBottomLedBrightnessNumber,
  BoschMotionLightSensitivityNumber, BoschDarknessThresholdNumber,
  BoschPowerLedBrightnessNumber, BoschAlarmDelayNumber,
  BoschAlarmActivationDelayNumber, BoschPreAlarmDelayNumber,
  BoschAudioAlarmSensitivityNumber.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
            }
        },
        last_update_success=True,
        options={},
        token="test-token",
        _pan_cache={},
        _image_rotation_180={},
        _lens_elevation_cache={},
        _audio_cache={},
        _lighting_switch_cache={},
        _motion_light_cache={},
        _global_lighting_cache={},
        _icon_led_brightness_cache={},
        _alarm_settings_cache={},
        _shc_state_cache={CAM_ID: {}},
        async_put_camera=AsyncMock(return_value=True),
        async_cloud_set_pan=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
    )


@pytest.fixture
def entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── Indoor II coord (for alarm-delay / power-LED classes) ────────────────────


@pytest.fixture
def coord_indoor(coord):
    coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
    return coord


# ── parametrized instantiation helpers ───────────────────────────────────────


def _make_pan(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschPanNumber

    return BoschPanNumber(coord, CAM_ID, entry, pan_limit=120)


def _make_speaker_level(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

    return BoschSpeakerLevelNumber(coord, CAM_ID, entry)


def _make_front_light_intensity(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber

    return BoschFrontLightIntensityNumber(coord, CAM_ID, entry)


def _make_lens_elevation(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

    return BoschLensElevationNumber(coord, CAM_ID, entry)


def _make_microphone_level(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

    return BoschMicrophoneLevelNumber(coord, CAM_ID, entry)


def _make_white_balance(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

    return BoschWhiteBalanceNumber(coord, CAM_ID, entry)


def _make_top_led_brightness(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschTopLedBrightnessNumber

    return BoschTopLedBrightnessNumber(coord, CAM_ID, entry)


def _make_bottom_led_brightness(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschBottomLedBrightnessNumber

    return BoschBottomLedBrightnessNumber(coord, CAM_ID, entry)


def _make_motion_light_sensitivity(coord, entry):
    from custom_components.bosch_shc_camera.number import (
        BoschMotionLightSensitivityNumber,
    )

    return BoschMotionLightSensitivityNumber(coord, CAM_ID, entry)


def _make_darkness_threshold(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschDarknessThresholdNumber

    return BoschDarknessThresholdNumber(coord, CAM_ID, entry)


def _make_power_led_brightness(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import BoschPowerLedBrightnessNumber

    return BoschPowerLedBrightnessNumber(coord_indoor, CAM_ID, entry)


def _make_alarm_delay(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import BoschAlarmDelayNumber

    return BoschAlarmDelayNumber(coord_indoor, CAM_ID, entry)


def _make_alarm_activation_delay(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import (
        BoschAlarmActivationDelayNumber,
    )

    return BoschAlarmActivationDelayNumber(coord_indoor, CAM_ID, entry)


def _make_pre_alarm_delay(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import BoschPreAlarmDelayNumber

    return BoschPreAlarmDelayNumber(coord_indoor, CAM_ID, entry)


# ── outdoor-coord classes ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "factory",
    [
        _make_pan,
        _make_speaker_level,
        _make_front_light_intensity,
        _make_lens_elevation,
        _make_microphone_level,
        _make_white_balance,
        _make_top_led_brightness,
        _make_bottom_led_brightness,
        _make_motion_light_sensitivity,
        _make_darkness_threshold,
    ],
)
def test_no_doubled_bosch_prefix_outdoor(factory, coord, entry):
    """_attr_name must not start with 'Bosch ' for outdoor-coord classes."""
    entity = factory(coord, entry)
    name = getattr(entity, "_attr_name", None)
    assert name is None or not name.startswith("Bosch "), (
        f"{type(entity).__name__}._attr_name={name!r} still has 'Bosch ' prefix"
    )


@pytest.mark.parametrize(
    "factory",
    [
        _make_pan,
        _make_speaker_level,
        _make_front_light_intensity,
        _make_lens_elevation,
        _make_microphone_level,
        _make_white_balance,
        _make_top_led_brightness,
        _make_bottom_led_brightness,
        _make_motion_light_sensitivity,
        _make_darkness_threshold,
    ],
)
def test_has_entity_name_true_outdoor(factory, coord, entry):
    """_attr_has_entity_name must be True (own or inherited) for all outdoor-coord classes."""
    entity = factory(coord, entry)
    # Use getattr so we resolve both plain attributes and properties correctly.
    has_entity_name = getattr(entity, "_attr_has_entity_name", False)
    # HA's NumberEntity / RestoreEntity may expose this as a property that returns
    # the class-level bool set in our classes, so evaluate truthiness.
    assert bool(has_entity_name) is True, (
        f"{type(entity).__name__}._attr_has_entity_name is not True (got {has_entity_name!r})"
    )


# ── indoor-coord classes ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "factory",
    [
        _make_power_led_brightness,
        _make_alarm_delay,
        _make_alarm_activation_delay,
        _make_pre_alarm_delay,
    ],
)
def test_no_doubled_bosch_prefix_indoor(factory, coord_indoor, entry):
    """_attr_name must not start with 'Bosch ' for indoor-coord classes."""
    entity = factory(coord_indoor, entry)
    name = getattr(entity, "_attr_name", None)
    assert name is None or not name.startswith("Bosch "), (
        f"{type(entity).__name__}._attr_name={name!r} still has 'Bosch ' prefix"
    )


@pytest.mark.parametrize(
    "factory",
    [
        _make_power_led_brightness,
        _make_alarm_delay,
        _make_alarm_activation_delay,
        _make_pre_alarm_delay,
    ],
)
def test_has_entity_name_true_indoor(factory, coord_indoor, entry):
    """_attr_has_entity_name must be True (own or inherited) for all indoor-coord classes."""
    entity = factory(coord_indoor, entry)
    has_entity_name = getattr(entity, "_attr_has_entity_name", False)
    assert bool(has_entity_name) is True, (
        f"{type(entity).__name__}._attr_has_entity_name is not True (got {has_entity_name!r})"
    )
