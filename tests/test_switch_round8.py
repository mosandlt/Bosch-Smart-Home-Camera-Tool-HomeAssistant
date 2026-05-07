"""Round-8 switch tests — covers missing lines in switch.py.

Targets:
- BoschMotionEnabledSwitch async_turn_on/off (gen2_indoor + _warn_if_privacy_on interaction)
- BoschRecordSoundSwitch async_turn_on/off
- BoschAutoFollowSwitch async_turn_on/off + is_on
- BoschIntercomSwitch async_turn_on (200, non-200, exception) + async_turn_off
- BoschPrivacySoundSwitch async_turn_on/off + available
- BoschTimestampSwitch async_turn_on/off + available
- BoschStatusLedSwitch async_turn_on/off + available (lines 1085-1090)
- BoschMotionLightSwitch is_on from cache / _set_motion_light with/without cache
- BoschAmbientLightSwitch is_on from cache / _set_ambient_light branches
- BoschSoftLightFadingSwitch _put_global_lighting branches
- BoschIntrusionDetectionSwitch _set_intrusion + privacy guard
- BoschNotificationTypeSwitch is_on / _set_type
- BoschAlarmSystemArmSwitch / _BoschAlarmSettingsSwitchBase / BoschAudioAlarmSwitch
- BoschImageRotation180Switch async_turn_on/off
- BoschNvrRecordingSwitch is_on / available / async_turn_on/off / extra_state_attributes

No HA runtime — SimpleNamespace + AsyncMock only.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── minimal coordinator stub ─────────────────────────────────────────────────


def _coord(
    hw="HOME_Eyes_Outdoor",
    privacy_on=False,
    motion_settings=None,
    recording_options=None,
    autofollow_data=None,
    privacy_sound_cache=None,
    timestamp_cache=None,
    ledlights_cache=None,
    motion_light_cache=None,
    ambient_lighting_cache=None,
    global_lighting_cache=None,
    intrusion_config=None,
    notifications_cache=None,
    alarm_settings_cache=None,
    arming_cache=None,
    image_rotation_180=None,
    nvr_user_intent=None,
    live_connections=None,
    audio_alarm_settings_val=None,
    **kwargs,
):
    shc_state = {CAM_ID: {"privacy_mode": privacy_on}}

    def _ms(cam_id):
        return motion_settings or {}

    def _ros(cam_id):
        return recording_options or {}

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
                "autofollow": autofollow_data,
                "audioAlarm": audio_alarm_settings_val or {},
            }
        },
        last_update_success=True,
        is_camera_online=lambda cid: True,
        _shc_state_cache=shc_state,
        _live_connections=live_connections if live_connections is not None else {},
        _audio_enabled={},
        options={},
        _privacy_sound_cache=privacy_sound_cache if privacy_sound_cache is not None else {},
        _timestamp_cache=timestamp_cache if timestamp_cache is not None else {},
        _timestamp_set_at={},
        _ledlights_cache=ledlights_cache if ledlights_cache is not None else {},
        _ledlights_set_at={},
        _motion_light_cache=motion_light_cache if motion_light_cache is not None else {},
        _ambient_lighting_cache=ambient_lighting_cache if ambient_lighting_cache is not None else {},
        _global_lighting_cache=global_lighting_cache if global_lighting_cache is not None else {},
        _intrusion_config_cache=intrusion_config if intrusion_config is not None else {},
        _notifications_cache=notifications_cache if notifications_cache is not None else {},
        _alarm_settings_cache=alarm_settings_cache if alarm_settings_cache is not None else {},
        _alarm_status_cache={},
        _arming_cache=arming_cache if arming_cache is not None else {},
        _arming_set_at={},
        _image_rotation_180=image_rotation_180 if image_rotation_180 is not None else {},
        _nvr_user_intent=nvr_user_intent if nvr_user_intent is not None else {},
        _nvr_processes={},
        _nvr_error_state={},
        _privacy_sound_set_at={},
        motion_settings=_ms,
        recording_options=_ros,
        audio_alarm_settings=_aas,
        token="test-token",
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
        hass=SimpleNamespace(
            async_create_task=MagicMock(),
            config=SimpleNamespace(time_zone="Europe/Berlin"),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        **kwargs,
    )
    return coord


def _make_hass():
    return SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
        config=SimpleNamespace(time_zone="Europe/Berlin"),
    )


def _entry():
    return SimpleNamespace(
        data={"bearer_token": "tok"},
        options={},
        runtime_data=None,
    )


# ── BoschMotionEnabledSwitch ─────────────────────────────────────────────────


def _make_motion_switch(hw="HOME_Eyes_Outdoor", privacy_on=False, settings=None):
    from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

    coord = _coord(hw=hw, privacy_on=privacy_on, motion_settings=settings or {"enabled": True, "motionAlarmConfiguration": "HIGH"})
    sw = BoschMotionEnabledSwitch.__new__(BoschMotionEnabledSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


@pytest.mark.asyncio
async def test_motion_turn_on_normal():
    sw = _make_motion_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[1] == "motion"
    assert args[2]["enabled"] is True


@pytest.mark.asyncio
async def test_motion_turn_off_normal():
    sw = _make_motion_switch()
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["enabled"] is False


@pytest.mark.asyncio
async def test_motion_turn_on_gen2_indoor_privacy_blocked():
    """Gen2 indoor + privacy ON → turn_on is blocked (returns early)."""
    sw = _make_motion_switch(hw="HOME_Eyes_Indoor", privacy_on=True)
    sw.hass.services.async_call = AsyncMock()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_motion_turn_off_gen2_indoor_privacy_blocked():
    sw = _make_motion_switch(hw="HOME_Eyes_Indoor", privacy_on=True)
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_not_awaited()


def test_motion_is_on_from_settings():
    sw = _make_motion_switch(settings={"enabled": True})
    assert sw.is_on is True


def test_motion_is_on_none_when_no_settings():
    # Use a sentinel empty dict directly in coord so motion_settings() returns {}
    from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

    coord = _coord(motion_settings=None)
    # Override so lambda returns empty dict (falsy → is_on returns None)
    coord.motion_settings = lambda cid: {}
    sw = BoschMotionEnabledSwitch.__new__(BoschMotionEnabledSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    assert sw.is_on is None


# ── BoschRecordSoundSwitch ───────────────────────────────────────────────────


def _make_record_sound_switch(recording_options=None):
    from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

    coord = _coord(recording_options=recording_options or {"recordSound": False})
    sw = BoschRecordSoundSwitch.__new__(BoschRecordSoundSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


@pytest.mark.asyncio
async def test_record_sound_turn_on():
    sw = _make_record_sound_switch(recording_options={"recordSound": False})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "recording_options", {"recordSound": True}
    )


@pytest.mark.asyncio
async def test_record_sound_turn_off():
    sw = _make_record_sound_switch(recording_options={"recordSound": True})
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "recording_options", {"recordSound": False}
    )


def test_record_sound_is_on_false():
    sw = _make_record_sound_switch(recording_options={"recordSound": False})
    assert sw.is_on is False


def test_record_sound_is_on_none_no_opts():
    from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

    coord = _coord()
    coord.recording_options = lambda cid: {}  # direct assignment — empty dict is falsy
    sw = BoschRecordSoundSwitch.__new__(BoschRecordSoundSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    assert sw.is_on is None


# ── BoschAutoFollowSwitch ────────────────────────────────────────────────────


def _make_autofollow_switch(autofollow_data=None):
    from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch

    coord = _coord(autofollow_data=autofollow_data)
    sw = BoschAutoFollowSwitch.__new__(BoschAutoFollowSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Kamera"
    sw._model_name = "360"
    sw._fw = "7.91.56"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_autofollow_is_on_true():
    sw = _make_autofollow_switch(autofollow_data={"result": True})
    assert sw.is_on is True


def test_autofollow_is_on_false():
    sw = _make_autofollow_switch(autofollow_data={"result": False})
    assert sw.is_on is False


def test_autofollow_is_on_none():
    sw = _make_autofollow_switch(autofollow_data=None)
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_autofollow_turn_on():
    sw = _make_autofollow_switch(autofollow_data={"result": False})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "autofollow", {"result": True}
    )


@pytest.mark.asyncio
async def test_autofollow_turn_off():
    sw = _make_autofollow_switch(autofollow_data={"result": True})
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "autofollow", {"result": False}
    )


# ── BoschIntercomSwitch ──────────────────────────────────────────────────────


def _make_intercom_switch():
    from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

    coord = _coord()
    sw = BoschIntercomSwitch.__new__(BoschIntercomSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._is_on = False
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def _mock_session_ctx(status=200):
    """Return a mock aiohttp session where PUT returns the given status."""

    @asynccontextmanager
    async def _resp_ctx():
        yield SimpleNamespace(status=status)

    @asynccontextmanager
    async def _session_put(*args, **kwargs):
        async with _resp_ctx() as r:
            yield r

    session = MagicMock()
    session.put = _session_put
    return session


@pytest.mark.asyncio
async def test_intercom_turn_on_success():
    sw = _make_intercom_switch()
    with patch(
        "custom_components.bosch_shc_camera.switch.async_get_clientsession",
        return_value=_mock_session_ctx(200),
    ):
        await sw.async_turn_on()
    assert sw._is_on is True
    sw.async_write_ha_state.assert_called()


@pytest.mark.asyncio
async def test_intercom_turn_on_non_200():
    sw = _make_intercom_switch()
    with patch(
        "custom_components.bosch_shc_camera.switch.async_get_clientsession",
        return_value=_mock_session_ctx(500),
    ):
        await sw.async_turn_on()
    assert sw._is_on is False  # not set to True on failure


@pytest.mark.asyncio
async def test_intercom_turn_on_exception():
    """Exception path — _is_on stays False, no crash."""

    @asynccontextmanager
    async def _bad_put(*args, **kwargs):
        raise aiohttp.ClientError("network error")
        yield  # noqa: unreachable

    import aiohttp

    session = MagicMock()
    session.put = _bad_put
    sw = _make_intercom_switch()
    with patch(
        "custom_components.bosch_shc_camera.switch.async_get_clientsession",
        return_value=session,
    ):
        await sw.async_turn_on()
    assert sw._is_on is False
    sw.async_write_ha_state.assert_called()


@pytest.mark.asyncio
async def test_intercom_turn_off_success():
    sw = _make_intercom_switch()
    sw._is_on = True
    with patch(
        "custom_components.bosch_shc_camera.switch.async_get_clientsession",
        return_value=_mock_session_ctx(204),
    ):
        await sw.async_turn_off()
    assert sw._is_on is False


@pytest.mark.asyncio
async def test_intercom_turn_off_non_200():
    sw = _make_intercom_switch()
    sw._is_on = True
    with patch(
        "custom_components.bosch_shc_camera.switch.async_get_clientsession",
        return_value=_mock_session_ctx(500),
    ):
        await sw.async_turn_off()
    # _is_on unchanged when HTTP non-200
    assert sw._is_on is True


# ── BoschPrivacySoundSwitch ──────────────────────────────────────────────────


def _make_privacy_sound_switch(privacy_sound_cache=None, cam_online=True):
    from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

    coord = _coord(privacy_sound_cache=privacy_sound_cache or {})
    coord.is_camera_online = lambda cid: cam_online
    sw = BoschPrivacySoundSwitch.__new__(BoschPrivacySoundSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Kamera"
    sw._model_name = "360"
    sw._fw = "7.91.56"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_privacy_sound_is_on_true():
    sw = _make_privacy_sound_switch(privacy_sound_cache={CAM_ID: True})
    assert sw.is_on is True


def test_privacy_sound_is_on_none():
    sw = _make_privacy_sound_switch(privacy_sound_cache={})
    assert sw.is_on is None


def test_privacy_sound_available_true():
    sw = _make_privacy_sound_switch(privacy_sound_cache={CAM_ID: True})
    assert sw.available is True


def test_privacy_sound_available_false_no_cache():
    sw = _make_privacy_sound_switch(privacy_sound_cache={})
    assert sw.available is False


@pytest.mark.asyncio
async def test_privacy_sound_turn_on_success():
    sw = _make_privacy_sound_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "privacy_sound_override", {"result": True}
    )
    assert sw.coordinator._privacy_sound_cache[CAM_ID] is True


@pytest.mark.asyncio
async def test_privacy_sound_turn_on_failure():
    sw = _make_privacy_sound_switch()
    sw.coordinator.async_put_camera = AsyncMock(return_value=False)
    await sw.async_turn_on()
    # Cache should not be updated on failure
    assert CAM_ID not in sw.coordinator._privacy_sound_cache


@pytest.mark.asyncio
async def test_privacy_sound_turn_off():
    sw = _make_privacy_sound_switch(privacy_sound_cache={CAM_ID: True})
    await sw.async_turn_off()
    assert sw.coordinator._privacy_sound_cache[CAM_ID] is False


# ── BoschStatusLedSwitch ─────────────────────────────────────────────────────


def _make_led_switch(ledlights_cache=None, cam_online=True):
    from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

    coord = _coord(ledlights_cache=ledlights_cache or {})
    coord.is_camera_online = lambda cid: cam_online
    sw = BoschStatusLedSwitch.__new__(BoschStatusLedSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_led_available_true():
    sw = _make_led_switch(ledlights_cache={CAM_ID: True})
    assert sw.available is True


def test_led_available_false_no_cache():
    sw = _make_led_switch(ledlights_cache={})
    assert sw.available is False


@pytest.mark.asyncio
async def test_led_turn_on():
    sw = _make_led_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "ledlights", {"state": "ON"}
    )
    assert sw.coordinator._ledlights_cache[CAM_ID] is True


@pytest.mark.asyncio
async def test_led_turn_off():
    sw = _make_led_switch(ledlights_cache={CAM_ID: True})
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "ledlights", {"state": "OFF"}
    )
    assert sw.coordinator._ledlights_cache[CAM_ID] is False


# ── BoschMotionLightSwitch ───────────────────────────────────────────────────


def _make_motion_light_switch(cache=None):
    from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

    coord = _coord(motion_light_cache=cache or {})
    sw = BoschMotionLightSwitch.__new__(BoschMotionLightSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._is_on = None
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_motion_light_is_on_reads_from_cache():
    cache = {CAM_ID: {"lightOnMotionEnabled": True}}
    sw = _make_motion_light_switch(cache=cache)
    assert sw.is_on is True
    assert sw._is_on is True


def test_motion_light_is_on_none_when_no_cache():
    sw = _make_motion_light_switch(cache={})
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_motion_light_set_with_cache():
    cache = {CAM_ID: {"lightOnMotionEnabled": False, "duration": 30}}
    sw = _make_motion_light_switch(cache=cache)
    await sw._set_motion_light(True)
    sw.coordinator.async_put_camera.assert_awaited_once()
    called_body = sw.coordinator.async_put_camera.call_args[0][2]
    assert called_body["lightOnMotionEnabled"] is True
    assert sw._is_on is True


@pytest.mark.asyncio
async def test_motion_light_set_without_cache_fetches_api():
    """When cache is empty, _set_motion_light fetches via aiohttp GET."""
    sw = _make_motion_light_switch(cache={})

    fetched_data = {"lightOnMotionEnabled": False, "duration": 30}

    @asynccontextmanager
    async def _get_ctx(*args, **kwargs):
        yield SimpleNamespace(status=200, json=AsyncMock(return_value=fetched_data))

    @asynccontextmanager
    async def _session_get(*args, **kwargs):
        async with _get_ctx() as r:
            yield r

    session = MagicMock()
    session.get = _session_get

    with patch(
        "custom_components.bosch_shc_camera.switch.async_get_clientsession",
        return_value=session,
    ):
        await sw._set_motion_light(True)

    sw.coordinator.async_put_camera.assert_awaited_once()


# ── BoschIntrusionDetectionSwitch ────────────────────────────────────────────


def _make_intrusion_switch(config=None, privacy_on=False):
    from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch

    coord = _coord(
        intrusion_config={CAM_ID: config} if config else {},
        privacy_on=privacy_on,
    )
    sw = BoschIntrusionDetectionSwitch.__new__(BoschIntrusionDetectionSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_intrusion_is_on_true():
    sw = _make_intrusion_switch(config={"enabled": True, "sensitivity": 3})
    assert sw.is_on is True


def test_intrusion_available_false_no_config():
    sw = _make_intrusion_switch(config=None)
    assert sw.available is False


def test_intrusion_extra_attrs():
    cfg = {"enabled": True, "sensitivity": 3, "detectionMode": "PERSON", "distance": 8}
    sw = _make_intrusion_switch(config=cfg)
    attrs = sw.extra_state_attributes
    assert attrs["sensitivity"] == 3
    assert attrs["detection_mode"] == "PERSON"


@pytest.mark.asyncio
async def test_intrusion_turn_on_privacy_blocked():
    sw = _make_intrusion_switch(config={"enabled": False, "sensitivity": 3}, privacy_on=True)
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_intrusion_turn_on_success():
    cfg = {"enabled": False, "sensitivity": 3, "detectionMode": "PERSON"}
    sw = _make_intrusion_switch(config=cfg)
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["enabled"] is True


@pytest.mark.asyncio
async def test_intrusion_turn_off():
    cfg = {"enabled": True, "sensitivity": 3}
    sw = _make_intrusion_switch(config=cfg)
    await sw.async_turn_off()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["enabled"] is False


# ── BoschNotificationTypeSwitch ──────────────────────────────────────────────


def _make_notif_type_switch(ntype="movement", cache=None):
    from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch

    coord = _coord(notifications_cache=cache or {})
    sw = BoschNotificationTypeSwitch.__new__(BoschNotificationTypeSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._ntype = ntype
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_notif_type_is_on_true():
    cache = {CAM_ID: {"movement": True, "person": False}}
    sw = _make_notif_type_switch("movement", cache=cache)
    assert sw.is_on is True


def test_notif_type_is_on_false():
    cache = {CAM_ID: {"movement": False}}
    sw = _make_notif_type_switch("movement", cache=cache)
    assert sw.is_on is False


def test_notif_type_is_on_none_no_cache():
    sw = _make_notif_type_switch("movement", cache={})
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_notif_type_turn_on():
    cache = {CAM_ID: {"movement": False, "person": True}}
    sw = _make_notif_type_switch("movement", cache=cache)
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["movement"] is True
    # Person should be preserved
    assert args[2]["person"] is True


@pytest.mark.asyncio
async def test_notif_type_turn_off():
    cache = {CAM_ID: {"movement": True}}
    sw = _make_notif_type_switch("movement", cache=cache)
    await sw.async_turn_off()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["movement"] is False


# ── BoschAlarmSystemArmSwitch ────────────────────────────────────────────────


def _make_alarm_arm_switch(arming_cache=None):
    from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

    coord = _coord(arming_cache=arming_cache or {})
    sw = BoschAlarmSystemArmSwitch.__new__(BoschAlarmSystemArmSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_alarm_arm_is_on_true():
    sw = _make_alarm_arm_switch(arming_cache={CAM_ID: True})
    assert sw.is_on is True


def test_alarm_arm_extra_attrs():
    sw = _make_alarm_arm_switch()
    attrs = sw.extra_state_attributes
    assert "alarm_type" in attrs
    assert "intrusion_system" in attrs


@pytest.mark.asyncio
async def test_alarm_arm_turn_on():
    sw = _make_alarm_arm_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "intrusionSystem/arming", {"arm": True}
    )
    assert sw.coordinator._arming_cache[CAM_ID] is True


@pytest.mark.asyncio
async def test_alarm_arm_turn_off():
    sw = _make_alarm_arm_switch(arming_cache={CAM_ID: True})
    await sw.async_turn_off()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["arm"] is False


# ── _BoschAlarmSettingsSwitchBase (via BoschAlarmModeSwitch) ─────────────────


def _make_alarm_mode_switch(settings=None):
    from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

    coord = _coord(alarm_settings_cache={CAM_ID: settings} if settings is not None else {})
    sw = BoschAlarmModeSwitch.__new__(BoschAlarmModeSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._field = "alarmMode"
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_alarm_mode_is_on_true():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "ON"})
    assert sw.is_on is True


def test_alarm_mode_is_on_false():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "OFF"})
    assert sw.is_on is False


def test_alarm_mode_is_on_none_no_settings():
    sw = _make_alarm_mode_switch(settings={})
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_alarm_mode_turn_on():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "OFF", "preAlarmMode": "ON"})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["alarmMode"] == "ON"
    assert body["preAlarmMode"] == "ON"  # preserved


@pytest.mark.asyncio
async def test_alarm_mode_turn_off():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "ON"})
    await sw.async_turn_off()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["alarmMode"] == "OFF"


@pytest.mark.asyncio
async def test_alarm_mode_set_skips_when_no_settings():
    """_set with empty settings exits early without calling async_put_camera."""
    sw = _make_alarm_mode_switch(settings={})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_not_awaited()


# ── BoschAudioAlarmSwitch ────────────────────────────────────────────────────


def _make_audio_alarm_switch(audio_settings=None, hw="HOME_Eyes_Outdoor", privacy_on=False):
    from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch

    coord = _coord(hw=hw, privacy_on=privacy_on, audio_alarm_settings_val=audio_settings or {})
    sw = BoschAudioAlarmSwitch.__new__(BoschAudioAlarmSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_audio_alarm_is_on_true():
    sw = _make_audio_alarm_switch({"enabled": True, "threshold": 54})
    assert sw.is_on is True


def test_audio_alarm_is_on_none_no_settings():
    sw = _make_audio_alarm_switch()
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_audio_alarm_turn_on():
    sw = _make_audio_alarm_switch({"enabled": False, "sensitivity": 0, "threshold": 54, "audioAlarmConfiguration": "CUSTOM"})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["enabled"] is True


@pytest.mark.asyncio
async def test_audio_alarm_gen2_indoor_privacy_blocked():
    sw = _make_audio_alarm_switch(
        audio_settings={"enabled": False, "threshold": 54},
        hw="HOME_Eyes_Indoor",
        privacy_on=True,
    )
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_not_awaited()


# ── BoschImageRotation180Switch ──────────────────────────────────────────────


def _make_image_rotation_switch(rotation_180=None):
    from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch

    coord = _coord(image_rotation_180=rotation_180 or {})
    sw = BoschImageRotation180Switch.__new__(BoschImageRotation180Switch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Kamera"
    sw._model_name = "360"
    sw._fw = "7.91.56"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_image_rotation_is_on_false_default():
    sw = _make_image_rotation_switch()
    assert sw.is_on is False


def test_image_rotation_is_on_true():
    sw = _make_image_rotation_switch(rotation_180={CAM_ID: True})
    assert sw.is_on is True


@pytest.mark.asyncio
async def test_image_rotation_turn_on():
    sw = _make_image_rotation_switch()
    await sw.async_turn_on()
    assert sw.coordinator._image_rotation_180[CAM_ID] is True
    sw.coordinator.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_image_rotation_turn_off():
    sw = _make_image_rotation_switch(rotation_180={CAM_ID: True})
    await sw.async_turn_off()
    assert sw.coordinator._image_rotation_180[CAM_ID] is False
    sw.coordinator.async_update_listeners.assert_called_once()


# ── BoschNvrRecordingSwitch ──────────────────────────────────────────────────


def _make_nvr_switch(nvr_intent=None, live_connections=None, cam_online=True):
    from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

    coord = _coord(
        nvr_user_intent=nvr_intent or {},
        live_connections=live_connections or {},
    )
    coord.is_camera_online = lambda cid: cam_online
    coord._nvr_error_state = {}
    coord._nvr_processes = {}
    sw = BoschNvrRecordingSwitch.__new__(BoschNvrRecordingSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_nvr_is_on_false_default():
    sw = _make_nvr_switch()
    assert sw.is_on is False


def test_nvr_is_on_true():
    sw = _make_nvr_switch(nvr_intent={CAM_ID: True})
    assert sw.is_on is True


def test_nvr_available_true_local():
    live = {CAM_ID: {"_connection_type": "LOCAL"}}
    sw = _make_nvr_switch(live_connections=live)
    assert sw.available is True


def test_nvr_available_false_remote():
    live = {CAM_ID: {"_connection_type": "REMOTE"}}
    sw = _make_nvr_switch(live_connections=live)
    assert sw.available is False


def test_nvr_available_false_cam_offline():
    live = {CAM_ID: {"_connection_type": "LOCAL"}}
    sw = _make_nvr_switch(live_connections=live, cam_online=False)
    assert sw.available is False


def test_nvr_extra_state_attributes_no_proc():
    sw = _make_nvr_switch()
    attrs = sw.extra_state_attributes
    assert attrs["ffmpeg_running"] is False
    assert "connection_type" in attrs
    assert "last_error" in attrs


@pytest.mark.asyncio
async def test_nvr_turn_on():
    sw = _make_nvr_switch()
    await sw.async_turn_on()
    sw.coordinator.start_recorder.assert_awaited_once_with(CAM_ID)
    sw.async_write_ha_state.assert_called()


@pytest.mark.asyncio
async def test_nvr_turn_off():
    sw = _make_nvr_switch(nvr_intent={CAM_ID: True})
    await sw.async_turn_off()
    sw.coordinator.stop_recorder.assert_awaited_once_with(CAM_ID)
    sw.async_write_ha_state.assert_called()
