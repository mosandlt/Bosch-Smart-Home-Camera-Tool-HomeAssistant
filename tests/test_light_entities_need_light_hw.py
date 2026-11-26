"""Light entities are only created for cameras that have light hardware.

Bug: the Gen2 Indoor II (hardwareVersion HOME_Eyes_Indoor / CAMERA_INDOOR_GEN2)
got the switches "Dauerlicht", "Licht bei Bewegung" and "Weiches Lichtfading"
plus the ambient-light sensor, none of which can ever work without light
hardware (they stayed unavailable/unknown). The gate was `generation >= 2`;
it is now `generation >= 2 and CameraModelConfig.has_light`.
Existing entities keep their unique_ids; orphaned ones stay in the registry.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bosch_smart_camera.models import get_model_config

CAM = "11111111-1111-1111-1111-111111111111"
LIGHT_SWITCHES = {
    "BoschMotionLightSwitch",
    "BoschAmbientLightSwitch",
    "BoschSoftLightFadingSwitch",
}
# (hardwareVersion, light switches expected, ambient sensor expected)
CASES = [
    ("HOME_Eyes_Outdoor", True, True),
    ("CAMERA_OUTDOOR_GEN2", True, True),
    ("HOME_Eyes_Indoor", False, False),
    ("CAMERA_INDOOR_GEN2", False, False),
    ("OUTDOOR", False, True),
    ("CAMERA_EYES", False, True),
    ("INDOOR", False, True),
    ("CAMERA_360", False, True),
    ("SOMETHING_UNKNOWN", False, True),
    ("", False, True),
]


def _coord(hw: str) -> MagicMock:
    coord = MagicMock()
    coord.data = {CAM: {"info": {"hardwareVersion": hw, "featureSupport": {}}}}
    coord.hw_version = {}
    coord.options = {"enable_sensors": True}
    return coord


def _setup(platform: Any, hw: str) -> set[str]:
    coord = _coord(hw)
    entry = SimpleNamespace(
        runtime_data=coord, options=coord.options, async_on_unload=MagicMock()
    )
    added: list[Any] = []

    def _add(entities: Any, **_kw: Any) -> None:
        added.extend(entities)

    with patch("homeassistant.helpers.entity_registry.async_get"):
        asyncio.run(platform.async_setup_entry(None, entry, _add))
    return {type(e).__name__ for e in added}


@pytest.mark.parametrize(("hw", "lights", "_sensor"), CASES)
def test_light_switches_per_hardware(hw: str, lights: bool, _sensor: bool) -> None:
    from custom_components.bosch_smart_camera import switch

    names = _setup(switch, hw)
    assert (LIGHT_SWITCHES <= names) is lights
    assert not (LIGHT_SWITCHES & names) or lights


@pytest.mark.parametrize(("hw", "_lights", "sensor"), CASES)
def test_ambient_light_sensor_per_hardware(
    hw: str, _lights: bool, sensor: bool
) -> None:
    from custom_components.bosch_smart_camera import sensor as sensor_mod

    names = _setup(sensor_mod, hw)
    assert ("BoschAmbientLightSensor" in names) is sensor


@pytest.mark.parametrize("hw", ["HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"])
def test_indoor_ii_keeps_non_light_gen2_switches(hw: str) -> None:
    from custom_components.bosch_smart_camera import switch

    names = _setup(switch, hw)
    assert "BoschStatusLedSwitch" in names
    assert "BoschIntrusionDetectionSwitch" in names


@pytest.mark.parametrize(
    ("hw", "has_light"),
    [
        ("HOME_Eyes_Outdoor", True),
        ("CAMERA_OUTDOOR_GEN2", True),
        ("OUTDOOR", True),
        ("HOME_Eyes_Indoor", False),
        ("CAMERA_INDOOR_GEN2", False),
        ("INDOOR", False),
        ("CAMERA_360", False),
    ],
)
def test_model_has_light_flag(hw: str, has_light: bool) -> None:
    assert get_model_config(hw).has_light is has_light
