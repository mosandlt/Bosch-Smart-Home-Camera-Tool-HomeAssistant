"""Regression tests: doubled-prefix entity_id bug — binary_sensor + light.

Bug: classes with `_attr_has_entity_name = True` AND `_attr_name = f"Bosch
{cam_title} <Suffix>"` produced entity_ids like
`light.bosch_est_bosch_est_oberes_licht` because HA prepends the device name
automatically when `has_entity_name=True`, and the code re-prepended it.

Fix: `_attr_name` must be the bare suffix only (e.g. `"Motion"`, `"Oberes
Licht"`).  Translation keys exist in `_attr_translation_key` but the
corresponding `entity.<platform>.<key>.name` entries are not yet in
strings.json, so literal suffixes are used (no None).

Source: forum post 998974/15 — Andrew75, 2026-05-15.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord_bs():
    """Minimal coordinator for binary sensor tests."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                    "featureSupport": {"sound": True},
                },
                "events": [],
            }
        },
    )


@pytest.fixture
def stub_coord_light():
    """Minimal coordinator for light tests."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                }
            }
        },
        _lighting_switch_cache={
            CAM_ID: {
                "frontLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "topLedLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "bottomLedLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
            }
        },
        last_update_success=True,
        token="tok",
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ─── helpers ──────────────────────────────────────────────────────────────────


def _no_doubled_prefix(entity) -> bool:
    """Return True when _attr_name is None or does not start with 'Bosch '."""
    name = getattr(entity, "_attr_name", None)
    return name is None or not name.startswith("Bosch ")


def _has_entity_name(entity) -> bool:
    """Resolve _attr_has_entity_name through the MRO."""
    for cls in type(entity).__mro__:
        if "_attr_has_entity_name" in cls.__dict__:
            return bool(cls.__dict__["_attr_has_entity_name"])
    return bool(getattr(entity, "_attr_has_entity_name", False))


# ─── binary_sensor ────────────────────────────────────────────────────────────


class TestMotionBinarySensorPrefix:
    """binary_sensor.py:155  BoschMotionBinarySensor"""

    def test_name_no_doubled_prefix(self, stub_coord_bs, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        entity = BoschMotionBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(self, stub_coord_bs, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        entity = BoschMotionBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


class TestAudioAlarmBinarySensorPrefix:
    """binary_sensor.py:192  BoschAudioAlarmBinarySensor"""

    def test_name_no_doubled_prefix(self, stub_coord_bs, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        entity = BoschAudioAlarmBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(self, stub_coord_bs, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        entity = BoschAudioAlarmBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


class TestPersonDetectedBinarySensorPrefix:
    """binary_sensor.py:229  BoschPersonDetectedBinarySensor"""

    def test_name_no_doubled_prefix(self, stub_coord_bs, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        entity = BoschPersonDetectedBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(self, stub_coord_bs, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        entity = BoschPersonDetectedBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


# ─── light ────────────────────────────────────────────────────────────────────


class TestTopLedLightPrefix:
    """light.py:384  BoschTopLedLight (Oberes Licht)"""

    def test_name_no_doubled_prefix(self, stub_coord_light, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entity = BoschTopLedLight(stub_coord_light, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(self, stub_coord_light, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entity = BoschTopLedLight(stub_coord_light, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


class TestBottomLedLightPrefix:
    """light.py:398  BoschBottomLedLight (Unteres Licht)"""

    def test_name_no_doubled_prefix(self, stub_coord_light, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschBottomLedLight

        entity = BoschBottomLedLight(stub_coord_light, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(self, stub_coord_light, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschBottomLedLight

        entity = BoschBottomLedLight(stub_coord_light, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


class TestFrontLightPrefix:
    """light.py:421  BoschFrontLight (Frontlicht)"""

    def test_name_no_doubled_prefix(self, stub_coord_light, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(stub_coord_light, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(self, stub_coord_light, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(stub_coord_light, CAM_ID, stub_entry)
        assert _has_entity_name(entity)
