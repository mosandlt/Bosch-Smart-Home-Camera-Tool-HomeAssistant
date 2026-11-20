"""Edge cases of the local-first settings.

Pins: an unaddressable camera forgets its local privacy flag and settings, an
empty cloud answer (HTTP 200 with ``{}``) never leads to a partial PUT for
motion/ambient light, and the speaker level is only available when the audio
cache really holds it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_smart_camera.ldi_local import (
    ldi_privacy_on,
    refresh_ldi_rest,
)
from custom_components.bosch_smart_camera.number import BoschSpeakerLevelNumber
from custom_components.bosch_smart_camera.switch import (
    BoschAmbientLightSwitch,
    BoschMotionLightSwitch,
)
from tests.test_ldi_local import CAM, _coord
from tests.test_ldi_settings import _ENTRY
from tests.test_ldi_settings_lighting import _light_coord

SW = "custom_components.bosch_smart_camera.switch"


class TestUnaddressableCamera:
    @pytest.mark.asyncio
    async def test_forgets_local_privacy_and_settings(self) -> None:
        c = _coord(ip=None, privacy=False)
        c.ldi_rest_state = {CAM: {"privacy_on": True, "settings": {"x": 1}}}
        c.async_update_listeners = MagicMock()
        assert await refresh_ldi_rest(c, CAM) is None  # type: ignore[arg-type]
        # the stale local "on" no longer hides the cloud flag
        assert ldi_privacy_on(c, CAM) is False  # type: ignore[arg-type]
        assert c.ldi_rest_state[CAM]["settings"] is None
        c.async_update_listeners.assert_called_once()

    @pytest.mark.asyncio
    async def test_without_any_record_nothing_happens(self) -> None:
        c = _coord(ip=None)
        c.ldi_rest_state = {}
        assert await refresh_ldi_rest(c, CAM) is None  # type: ignore[arg-type]
        assert c.ldi_rest_state == {}


class TestEmptyCloudBlock:
    @pytest.mark.asyncio
    async def test_motion_light_switch_does_not_put_a_partial_block(self) -> None:
        c = _light_coord()
        c.motion_light_cache = {}
        c.async_put_camera = AsyncMock(return_value=True)
        sw = BoschMotionLightSwitch(c, CAM, _ENTRY)
        sw.hass = MagicMock()
        sw.hass.services.async_call = AsyncMock()
        sw.async_write_ha_state = MagicMock()
        with (
            patch(f"{SW}.fetch_json", new=AsyncMock(return_value={})),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await sw.async_turn_on()
        c.async_put_camera.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ambient_switch_does_not_put_a_partial_block(self) -> None:
        c = _light_coord()
        sw = BoschAmbientLightSwitch(c, CAM, _ENTRY)
        sw.hass = MagicMock()
        sw.hass.services.async_call = AsyncMock()
        sw.async_write_ha_state = MagicMock()
        put = AsyncMock()
        with (
            patch(f"{SW}.fetch_json", new=AsyncMock(return_value={})),
            patch(f"{SW}.put_setting", new=put),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await sw.async_turn_on()
        put.assert_not_awaited()


class TestSpeakerAvailability:
    @pytest.mark.parametrize(
        ("audio", "expected"),
        [
            ({"speakerLevel": 80}, True),
            ({"speakerLevel": 0}, True),
            ({}, False),
            ({"audioEnabled": True, "microphoneLevel": 60}, False),
        ],
    )
    def test_needs_the_speaker_level_in_the_cache(
        self, audio: dict[str, Any], expected: bool
    ) -> None:
        c = _light_coord()
        c.audio_cache = {CAM: audio}
        c.last_update_success = True
        assert BoschSpeakerLevelNumber(c, CAM, _ENTRY).available is expected

    def test_no_cache_entry_is_unavailable(self) -> None:
        c = _light_coord()
        c.audio_cache = {}
        c.last_update_success = True
        assert BoschSpeakerLevelNumber(c, CAM, _ENTRY).available is False
