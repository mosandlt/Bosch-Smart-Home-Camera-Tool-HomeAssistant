"""Coverage pin: every switch/number call site added by the 2026-07-07
notify-on-total-failure fix must exercise its `if not success:` warning
branch — the first CI run of v14.4.9 caught these at 99.93% coverage
(switch.py lines 122/673/680/711/718/748/755/1121/1129, number.py:340),
none of which were hit by the existing suite (every prior test only
exercised the success path for these four switches + the light-intensity
number entity).
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _coord(**overrides: object) -> SimpleNamespace:
    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        async_cloud_set_camera_light=AsyncMock(return_value=False),
        async_cloud_set_light_component=AsyncMock(return_value=False),
        async_cloud_set_notifications=AsyncMock(return_value=False),
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


@pytest.mark.asyncio
class TestCameraLightSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        sw = BoschCameraLightSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any("failed on all paths" in r.message for r in caplog.records)

    async def test_turn_off_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        sw = BoschCameraLightSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any("failed on all paths" in r.message for r in caplog.records)

    async def test_turn_on_success_is_silent(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        sw = BoschCameraLightSwitch(
            _coord(async_cloud_set_camera_light=AsyncMock(return_value=True)),
            CAM_ID,
            _entry(),
        )
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert not any("failed on all paths" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestFrontLightSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        sw = BoschFrontLightSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any("failed on all paths" in r.message for r in caplog.records)

    async def test_turn_off_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        sw = BoschFrontLightSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any("failed on all paths" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestWallwasherSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch

        sw = BoschWallwasherSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any("failed on all paths" in r.message for r in caplog.records)

    async def test_turn_off_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch

        sw = BoschWallwasherSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any("failed on all paths" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestNotificationsSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        sw = BoschNotificationsSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any("failed on all paths" in r.message for r in caplog.records)

    async def test_turn_off_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        sw = BoschNotificationsSwitch(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any("failed on all paths" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestFrontLightIntensityNumberWarnsOnFailure:
    async def test_set_value_failure_warns(self, caplog):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        num = BoschFrontLightIntensityNumber(_coord(), CAM_ID, _entry())
        with caplog.at_level(logging.WARNING):
            await num.async_set_native_value(75.0)
        assert any("failed on all paths" in r.message for r in caplog.records)

    async def test_set_value_success_is_silent(self, caplog):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        num = BoschFrontLightIntensityNumber(
            _coord(async_cloud_set_light_component=AsyncMock(return_value=True)),
            CAM_ID,
            _entry(),
        )
        with caplog.at_level(logging.WARNING):
            await num.async_set_native_value(75.0)
        assert not any("failed on all paths" in r.message for r in caplog.records)
