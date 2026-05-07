"""Round-2 number entity tests — covers missing lines in number.py.

Targets:
- BoschPanNumber native_value with/without rotation_180, available, async_set_native_value
- BoschAudioThresholdNumber native_value (valid / None / bad), available, async_set_native_value
- BoschSpeakerLevelNumber native_value, async_set_native_value (200, non-200, exception)
- BoschFrontLightIntensityNumber native_value None, async_set_native_value
- BoschLensElevationNumber available/native_value/async_set_native_value
- BoschMicrophoneLevelNumber async_set_native_value + gen2_indoor privacy guard
- BoschWhiteBalanceNumber native_value (cache hit/miss), async_set_native_value (200, non-200, exception)
- _BoschLedBrightnessBase (BoschTopLedBrightnessNumber) native_value + async_set (200, non-200, exception)
- BoschMotionLightSensitivityNumber native_value, available, async_set_native_value (cache empty guard)
- BoschDarknessThresholdNumber native_value, async_set_native_value
- BoschPowerLedBrightnessNumber native_value, available, async_set_native_value (clamps 0-4)
- _BoschAlarmDelayBase (BoschAlarmDelayNumber) native_value, available, async_set_native_value
- BoschAudioAlarmSensitivityNumber native_value, async_set_native_value (privacy guard, cam data update)

No HA runtime needed — SimpleNamespace + AsyncMock pattern.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── coordinator / entity helpers ─────────────────────────────────────────────


def _coord(
    pan_cache=None,
    audio_alarm_settings_val=None,
    lens_elevation_cache=None,
    audio_cache=None,
    lighting_switch_cache=None,
    motion_light_cache=None,
    global_lighting_cache=None,
    icon_led_brightness_cache=None,
    alarm_settings_cache=None,
    shc_state_cache=None,
    hw="HOME_Eyes_Outdoor",
    **overrides,
):
    def _aas(cam_id):
        return audio_alarm_settings_val or {}

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "audioAlarm": audio_alarm_settings_val or {},
            }
        },
        last_update_success=True,
        options={},
        token="test-token",
        _pan_cache=pan_cache if pan_cache is not None else {},
        _image_rotation_180={},
        _lens_elevation_cache=lens_elevation_cache if lens_elevation_cache is not None else {},
        _audio_cache=audio_cache if audio_cache is not None else {},
        _lighting_switch_cache=lighting_switch_cache if lighting_switch_cache is not None else {},
        _motion_light_cache=motion_light_cache if motion_light_cache is not None else {},
        _global_lighting_cache=global_lighting_cache if global_lighting_cache is not None else {},
        _icon_led_brightness_cache=icon_led_brightness_cache if icon_led_brightness_cache is not None else {},
        _alarm_settings_cache=alarm_settings_cache if alarm_settings_cache is not None else {},
        _shc_state_cache=shc_state_cache if shc_state_cache is not None else {CAM_ID: {}},
        audio_alarm_settings=_aas,
        async_put_camera=AsyncMock(return_value=True),
        is_camera_online=lambda cid: True,
        **overrides,
    )
    return coord


def _make_hass():
    return SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
        config=SimpleNamespace(time_zone="Europe/Berlin"),
    )


def _entry():
    return SimpleNamespace(data={"bearer_token": "tok"}, options={}, runtime_data=None)


# ── BoschPanNumber ───────────────────────────────────────────────────────────


def _make_pan(pan_cache=None, rotation_180=False):
    from custom_components.bosch_shc_camera.number import BoschPanNumber

    pan_limit = 170
    coord = _coord(pan_cache=pan_cache or {})
    coord._image_rotation_180 = {CAM_ID: rotation_180}
    coord.async_cloud_set_pan = AsyncMock()
    sw = BoschPanNumber.__new__(BoschPanNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Kamera"
    sw._model_name = "360"
    sw._fw = "7.91.56"
    sw._mac = ""
    sw._attr_native_min_value = -pan_limit
    sw._attr_native_max_value = pan_limit
    sw.async_write_ha_state = MagicMock()
    return sw


def test_pan_native_value_no_rotation():
    sw = _make_pan(pan_cache={CAM_ID: 45})
    assert sw.native_value == 45


def test_pan_native_value_rotated():
    sw = _make_pan(pan_cache={CAM_ID: 45}, rotation_180=True)
    assert sw.native_value == -45


def test_pan_native_value_none():
    sw = _make_pan(pan_cache={})
    assert sw.native_value is None


def test_pan_available_true():
    sw = _make_pan(pan_cache={CAM_ID: 0})
    assert sw.available is True


def test_pan_available_false_no_cache():
    sw = _make_pan(pan_cache={})
    assert sw.available is False


@pytest.mark.asyncio
async def test_pan_set_native_value_normal():
    sw = _make_pan(pan_cache={CAM_ID: 0})
    await sw.async_set_native_value(30.0)
    sw.coordinator.async_cloud_set_pan.assert_awaited_once_with(CAM_ID, 30)


@pytest.mark.asyncio
async def test_pan_set_native_value_inverted_when_rotated():
    sw = _make_pan(pan_cache={CAM_ID: 0}, rotation_180=True)
    await sw.async_set_native_value(30.0)
    sw.coordinator.async_cloud_set_pan.assert_awaited_once_with(CAM_ID, -30)


# ── BoschAudioThresholdNumber ─────────────────────────────────────────────────


def _make_audio_threshold(settings=None, shc_privacy=False):
    from custom_components.bosch_shc_camera.number import BoschAudioThresholdNumber

    shc_state = {CAM_ID: {"privacy_mode": shc_privacy}}
    coord = _coord(
        audio_alarm_settings_val=settings,
        shc_state_cache=shc_state,
    )
    sw = BoschAudioThresholdNumber.__new__(BoschAudioThresholdNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_audio_threshold_native_value_valid():
    sw = _make_audio_threshold(settings={"threshold": 72, "enabled": True})
    assert sw.native_value == 72.0


def test_audio_threshold_native_value_none():
    sw = _make_audio_threshold(settings={})
    assert sw.native_value is None


def test_audio_threshold_native_value_bad_type():
    sw = _make_audio_threshold(settings={"threshold": "bad"})
    assert sw.native_value is None


def test_audio_threshold_available_true():
    sw = _make_audio_threshold(settings={"threshold": 72})
    assert sw.available is True


def test_audio_threshold_available_false():
    sw = _make_audio_threshold(settings={})
    assert sw.available is False


@pytest.mark.asyncio
async def test_audio_threshold_set_success():
    sw = _make_audio_threshold(settings={"threshold": 50, "enabled": True, "sensitivity": 0, "audioAlarmConfiguration": "CUSTOM"})
    await sw.async_set_native_value(72.0)
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["threshold"] == 72
    assert body["enabled"] is True


@pytest.mark.asyncio
async def test_audio_threshold_set_failure_logs():
    sw = _make_audio_threshold(settings={"threshold": 50, "enabled": True})
    sw.coordinator.async_put_camera = AsyncMock(return_value=False)
    # Should not raise
    await sw.async_set_native_value(60.0)
    sw.async_write_ha_state.assert_called()


# ── BoschSpeakerLevelNumber ──────────────────────────────────────────────────


def _make_speaker_level(current_level=50):
    from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

    coord = _coord()
    sw = BoschSpeakerLevelNumber.__new__(BoschSpeakerLevelNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._current_level = float(current_level)
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def _mock_aiohttp_session(status=200):
    @asynccontextmanager
    async def _put(*args, **kwargs):
        yield SimpleNamespace(status=status)

    session = MagicMock()
    session.put = _put
    return session


def test_speaker_level_native_value():
    sw = _make_speaker_level(75)
    assert sw.native_value == 75.0


@pytest.mark.asyncio
async def test_speaker_level_set_success():
    sw = _make_speaker_level(50)
    session = _mock_aiohttp_session(200)
    with patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=session):
        await sw.async_set_native_value(80.0)
    assert sw._current_level == 80.0


@pytest.mark.asyncio
async def test_speaker_level_set_non_200():
    sw = _make_speaker_level(50)
    session = _mock_aiohttp_session(500)
    with patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=session):
        await sw.async_set_native_value(80.0)
    assert sw._current_level == 50.0


@pytest.mark.asyncio
async def test_speaker_level_set_exception():
    import aiohttp

    @asynccontextmanager
    async def _bad_put(*args, **kwargs):
        raise aiohttp.ClientError("net error")
        yield

    session = MagicMock()
    session.put = _bad_put
    sw = _make_speaker_level(50)
    with patch("homeassistant.helpers.aiohttp_client.async_get_clientsession", return_value=session):
        await sw.async_set_native_value(90.0)
    assert sw._current_level == 50.0
    sw.async_write_ha_state.assert_called()


# ── BoschFrontLightIntensityNumber ───────────────────────────────────────────


def _make_front_light_intensity(shc_state_cache=None):
    from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber

    coord = _coord(shc_state_cache=shc_state_cache or {CAM_ID: {}})
    coord.async_cloud_set_light_component = AsyncMock()
    sw = BoschFrontLightIntensityNumber.__new__(BoschFrontLightIntensityNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_front_light_intensity_native_value_none():
    sw = _make_front_light_intensity(shc_state_cache={CAM_ID: {}})
    assert sw.native_value is None


def test_front_light_intensity_native_value():
    sw = _make_front_light_intensity(shc_state_cache={CAM_ID: {"front_light_intensity": 0.75}})
    assert sw.native_value == 75


@pytest.mark.asyncio
async def test_front_light_intensity_set():
    sw = _make_front_light_intensity()
    await sw.async_set_native_value(60.0)
    sw.coordinator.async_cloud_set_light_component.assert_awaited_once_with(
        CAM_ID, "intensity", 0.6
    )


# ── BoschLensElevationNumber ─────────────────────────────────────────────────


def _make_lens_elevation(elevation_cache=None):
    from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

    coord = _coord(lens_elevation_cache=elevation_cache or {})
    sw = BoschLensElevationNumber.__new__(BoschLensElevationNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_lens_elevation_native_value():
    sw = _make_lens_elevation(elevation_cache={CAM_ID: 2.5})
    assert sw.native_value == 2.5


def test_lens_elevation_available_true():
    sw = _make_lens_elevation(elevation_cache={CAM_ID: 2.0})
    assert sw.available is True


def test_lens_elevation_available_false():
    sw = _make_lens_elevation(elevation_cache={})
    assert sw.available is False


@pytest.mark.asyncio
async def test_lens_elevation_set_updates_cache():
    sw = _make_lens_elevation(elevation_cache={CAM_ID: 2.0})
    await sw.async_set_native_value(3.0)
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "lens_elevation", {"elevation": 3.0}
    )
    assert sw.coordinator._lens_elevation_cache[CAM_ID] == 3.0


# ── BoschMicrophoneLevelNumber ────────────────────────────────────────────────


def _make_mic_level(audio_cache=None, hw="HOME_Eyes_Indoor", privacy_on=False):
    from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

    shc_state = {CAM_ID: {"privacy_mode": privacy_on}}
    coord = _coord(hw=hw, audio_cache=audio_cache or {}, shc_state_cache=shc_state)
    sw = BoschMicrophoneLevelNumber.__new__(BoschMicrophoneLevelNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Indoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


@pytest.mark.asyncio
async def test_mic_level_set_privacy_blocked():
    sw = _make_mic_level(
        audio_cache={CAM_ID: {"microphoneLevel": 60}},
        hw="HOME_Eyes_Indoor",
        privacy_on=True,
    )
    await sw.async_set_native_value(80.0)
    sw.coordinator.async_put_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_mic_level_set_success():
    sw = _make_mic_level(
        audio_cache={CAM_ID: {"microphoneLevel": 60, "speakerLevel": 50}},
        hw="HOME_Eyes_Outdoor",
    )
    await sw.async_set_native_value(80.0)
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["microphoneLevel"] == 80


# ── BoschWhiteBalanceNumber ───────────────────────────────────────────────────


def _make_white_balance(lighting_switch_cache=None):
    from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

    coord = _coord(lighting_switch_cache=lighting_switch_cache or {})
    sw = BoschWhiteBalanceNumber.__new__(BoschWhiteBalanceNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._wb_value = None
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_white_balance_native_value_from_cache():
    cache = {CAM_ID: {"frontLightSettings": {"whiteBalance": 0.5}}}
    sw = _make_white_balance(lighting_switch_cache=cache)
    assert sw.native_value == 0.5


def test_white_balance_native_value_fallback_to_wb_value():
    sw = _make_white_balance()
    sw._wb_value = 0.3
    assert sw.native_value == 0.3


@pytest.mark.asyncio
async def test_white_balance_set_success():
    sw = _make_white_balance()
    session = _mock_aiohttp_session(200)
    resp_json = {"frontLightSettings": {"brightness": 0, "whiteBalance": 0.2, "color": None}}

    @asynccontextmanager
    async def _put(*args, **kwargs):
        yield SimpleNamespace(status=200, json=AsyncMock(return_value=resp_json))

    session.put = _put
    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        await sw.async_set_native_value(0.2)
    assert sw._wb_value == 0.2


@pytest.mark.asyncio
async def test_white_balance_set_non_200():
    sw = _make_white_balance()
    session = _mock_aiohttp_session(500)
    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        await sw.async_set_native_value(0.5)
    # _wb_value not updated on failure
    assert sw._wb_value is None
    sw.async_write_ha_state.assert_called()


@pytest.mark.asyncio
async def test_white_balance_set_exception():
    import aiohttp

    @asynccontextmanager
    async def _bad_put(*args, **kwargs):
        raise aiohttp.ClientError("net err")
        yield

    session = MagicMock()
    session.put = _bad_put
    sw = _make_white_balance()
    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        await sw.async_set_native_value(0.3)
    sw.async_write_ha_state.assert_called()


# ── BoschTopLedBrightnessNumber (_BoschLedBrightnessBase) ────────────────────


def _make_top_led(lighting_switch_cache=None):
    from custom_components.bosch_shc_camera.number import BoschTopLedBrightnessNumber

    coord = _coord(lighting_switch_cache=lighting_switch_cache or {})
    sw = BoschTopLedBrightnessNumber.__new__(BoschTopLedBrightnessNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._brightness = None
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_top_led_native_value_from_cache():
    cache = {CAM_ID: {"topLedLightSettings": {"brightness": 80}}}
    sw = _make_top_led(lighting_switch_cache=cache)
    assert sw.native_value == 80.0


def test_top_led_native_value_fallback():
    sw = _make_top_led()
    sw._brightness = 50.0
    assert sw.native_value == 50.0


@pytest.mark.asyncio
async def test_top_led_set_success():
    sw = _make_top_led()
    resp_json = {}

    @asynccontextmanager
    async def _put(*args, **kwargs):
        yield SimpleNamespace(status=204, json=AsyncMock(return_value=resp_json))

    session = MagicMock()
    session.put = _put
    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        await sw.async_set_native_value(60.0)
    assert sw._brightness == 60.0


@pytest.mark.asyncio
async def test_top_led_set_non_200():
    sw = _make_top_led()
    session = _mock_aiohttp_session(500)
    with patch(
        "homeassistant.helpers.aiohttp_client.async_get_clientsession",
        return_value=session,
    ):
        await sw.async_set_native_value(70.0)
    assert sw._brightness is None  # not updated


# ── BoschMotionLightSensitivityNumber ─────────────────────────────────────────


def _make_motion_light_sens(motion_light_cache=None):
    from custom_components.bosch_shc_camera.number import BoschMotionLightSensitivityNumber

    coord = _coord(motion_light_cache=motion_light_cache or {})
    sw = BoschMotionLightSensitivityNumber.__new__(BoschMotionLightSensitivityNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_motion_light_sens_native_value():
    sw = _make_motion_light_sens({CAM_ID: {"motionLightSensitivity": 3}})
    assert sw.native_value == 3.0


def test_motion_light_sens_available_false():
    sw = _make_motion_light_sens({})
    assert sw.available is False


@pytest.mark.asyncio
async def test_motion_light_sens_set_empty_cache_noop():
    sw = _make_motion_light_sens({})
    await sw.async_set_native_value(3.0)
    sw.coordinator.async_put_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_motion_light_sens_set_updates_cache():
    sw = _make_motion_light_sens({CAM_ID: {"motionLightSensitivity": 2, "duration": 30}})
    await sw.async_set_native_value(4.0)
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["motionLightSensitivity"] == 4


# ── BoschDarknessThresholdNumber ─────────────────────────────────────────────


def _make_darkness_threshold(global_lighting_cache=None):
    from custom_components.bosch_shc_camera.number import BoschDarknessThresholdNumber

    coord = _coord(global_lighting_cache=global_lighting_cache or {})
    sw = BoschDarknessThresholdNumber.__new__(BoschDarknessThresholdNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_darkness_threshold_native_value():
    sw = _make_darkness_threshold({CAM_ID: {"darknessThreshold": 0.47, "softLightFading": True}})
    assert sw.native_value == 47.0


def test_darkness_threshold_native_value_none():
    sw = _make_darkness_threshold({})
    assert sw.native_value is None


@pytest.mark.asyncio
async def test_darkness_threshold_set_preserves_soft_fading():
    cache = {CAM_ID: {"darknessThreshold": 0.5, "softLightFading": True}}
    sw = _make_darkness_threshold(cache)
    await sw.async_set_native_value(60.0)
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["darknessThreshold"] == pytest.approx(0.6, abs=0.0001)
    assert body["softLightFading"] is True


# ── BoschPowerLedBrightnessNumber ────────────────────────────────────────────


def _make_power_led(icon_led_cache=None):
    from custom_components.bosch_shc_camera.number import BoschPowerLedBrightnessNumber

    coord = _coord(icon_led_brightness_cache=icon_led_cache or {})
    sw = BoschPowerLedBrightnessNumber.__new__(BoschPowerLedBrightnessNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_power_led_native_value():
    sw = _make_power_led({CAM_ID: 3})
    assert sw.native_value == 3


def test_power_led_available_true():
    sw = _make_power_led({CAM_ID: 2})
    assert sw.available is True


def test_power_led_available_false():
    sw = _make_power_led({})
    assert sw.available is False


@pytest.mark.asyncio
async def test_power_led_set_value():
    sw = _make_power_led({CAM_ID: 2})
    await sw.async_set_native_value(3.0)
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "iconLedBrightness", {"value": 3}
    )


@pytest.mark.asyncio
async def test_power_led_set_clamps_max():
    sw = _make_power_led({CAM_ID: 2})
    await sw.async_set_native_value(10.0)  # clamp to 4
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["value"] == 4


@pytest.mark.asyncio
async def test_power_led_set_clamps_min():
    sw = _make_power_led({CAM_ID: 2})
    await sw.async_set_native_value(-5.0)  # clamp to 0
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["value"] == 0


# ── BoschAlarmDelayNumber (_BoschAlarmDelayBase) ──────────────────────────────


def _make_alarm_delay(alarm_settings=None):
    from custom_components.bosch_shc_camera.number import BoschAlarmDelayNumber

    coord = _coord(alarm_settings_cache={CAM_ID: alarm_settings} if alarm_settings is not None else {})
    sw = BoschAlarmDelayNumber.__new__(BoschAlarmDelayNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._field = "alarmDelayInSeconds"
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_alarm_delay_native_value():
    sw = _make_alarm_delay({"alarmDelayInSeconds": 60})
    assert sw.native_value == 60.0


def test_alarm_delay_native_value_none():
    sw = _make_alarm_delay({})
    assert sw.native_value is None


def test_alarm_delay_available_false_no_settings():
    sw = _make_alarm_delay({})
    assert sw.available is False


@pytest.mark.asyncio
async def test_alarm_delay_set_updates_cache():
    sw = _make_alarm_delay({"alarmDelayInSeconds": 60, "alarmMode": "ON"})
    await sw.async_set_native_value(90.0)
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["alarmDelayInSeconds"] == 90
    assert body["alarmMode"] == "ON"


@pytest.mark.asyncio
async def test_alarm_delay_set_empty_noop():
    sw = _make_alarm_delay({})
    await sw.async_set_native_value(30.0)
    sw.coordinator.async_put_camera.assert_not_awaited()


# ── BoschAudioAlarmSensitivityNumber ─────────────────────────────────────────


def _make_audio_alarm_sens(settings=None, hw="HOME_Eyes_Indoor", privacy_on=False):
    from custom_components.bosch_shc_camera.number import BoschAudioAlarmSensitivityNumber

    shc_state = {CAM_ID: {"privacy_mode": privacy_on}}
    coord = _coord(
        hw=hw,
        audio_alarm_settings_val=settings,
        shc_state_cache=shc_state,
    )
    sw = BoschAudioAlarmSensitivityNumber.__new__(BoschAudioAlarmSensitivityNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Indoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._last_written = 0
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_audio_alarm_sens_native_value_from_settings():
    sw = _make_audio_alarm_sens(settings={"sensitivity": 5, "threshold": 54, "enabled": True})
    assert sw.native_value == 5.0


def test_audio_alarm_sens_native_value_fallback():
    sw = _make_audio_alarm_sens(settings={"threshold": 54, "enabled": True})
    sw._last_written = 3
    assert sw.native_value == 3.0


@pytest.mark.asyncio
async def test_audio_alarm_sens_privacy_blocked():
    sw = _make_audio_alarm_sens(
        settings={"sensitivity": 0, "threshold": 54, "enabled": True, "audioAlarmConfiguration": "CUSTOM"},
        hw="HOME_Eyes_Indoor",
        privacy_on=True,
    )
    await sw.async_set_native_value(5.0)
    sw.coordinator.async_put_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_audio_alarm_sens_set_success():
    sw = _make_audio_alarm_sens(
        settings={"sensitivity": 0, "threshold": 54, "enabled": True, "audioAlarmConfiguration": "CUSTOM"},
        hw="HOME_Eyes_Outdoor",
    )
    await sw.async_set_native_value(7.0)
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["sensitivity"] == 7
    assert sw._last_written == 7


@pytest.mark.asyncio
async def test_audio_alarm_sens_updates_cam_data():
    """On success, cam_data["audioAlarm"] is updated."""
    settings = {"sensitivity": 0, "threshold": 54, "enabled": True, "audioAlarmConfiguration": "CUSTOM"}
    sw = _make_audio_alarm_sens(settings=settings, hw="HOME_Eyes_Outdoor")
    await sw.async_set_native_value(3.0)
    assert sw.coordinator.data[CAM_ID]["audioAlarm"]["sensitivity"] == 3
