"""Coverage pin: number.py's front-light-intensity call site added by the
2026-07-07 notify-on-total-failure fix must exercise its `if not success:`
warning branch (number.py:340) — none of which were hit by the existing
suite (prior tests only exercised the success path).

Note: the equivalent switch.py coverage (camera-light/front-light/
wallwasher/notifications switches) for this same fix now lives in
tests/test_switch.py.
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
