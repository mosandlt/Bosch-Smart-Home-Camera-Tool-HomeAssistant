"""Tests for switch.py — Gen2/alarm/NVR classes not covered by previous rounds.

Sprint C coverage target: lines 83-112, 428-437, 900-948, 993-999, 1040-1045,
1085-1090, 1103-1169, 1182-1231, 1247-1297, 1311-1363, 1397-1440, 1462-1502,
1506-1563, 1584-1640, 1667-1702, 1753, 1762-1794.

Covers: _is_gen2_indoor, _warn_if_privacy_on helpers; BoschIntercomSwitch;
BoschPrivacySoundSwitch/TimestampSwitch turn_on/off; BoschMotionLightSwitch;
BoschAmbientLightSwitch; BoschSoftLightFadingSwitch; BoschIntrusionDetectionSwitch;
BoschNotificationTypeSwitch; BoschAlarmSystemArmSwitch; _BoschAlarmSettingsSwitchBase;
BoschAlarmModeSwitch; BoschPreAlarmSwitch; BoschAudioAlarmSwitch;
BoschImageRotation180Switch; BoschNvrRecordingSwitch.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
                    "featureSupport": {"light": True, "panLimit": 0, "sound": False},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        _live_connections={},
        _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        _privacy_sound_cache={CAM_ID: True},
        _privacy_sound_set_at={},
        _timestamp_cache={CAM_ID: True},
        _timestamp_set_at={},
        _ledlights_cache={CAM_ID: True},
        _ledlights_set_at={},
        _motion_light_cache={},
        _ambient_lighting_cache={},
        _global_lighting_cache={},
        _intrusion_config_cache={},
        _notifications_cache={},
        _arming_cache={},
        _arming_set_at={},
        _alarm_status_cache={},
        _alarm_settings_cache={},
        _audio_enabled={CAM_ID: True},
        _image_rotation_180={},
        _nvr_user_intent={},
        _nvr_processes={},
        _nvr_error_state={},
        last_update_success=True,
        options={"audio_default_on": True, "nvr_base_path": "/config/bosch_nvr", "nvr_retention_days": 3},
        token="tok-A",
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
        motion_settings=lambda cid: {"enabled": True, "motionAlarmConfiguration": "HIGH"},
        audio_alarm_settings=lambda cid: {"enabled": True, "threshold": 50, "sensitivity": "MEDIUM", "audioAlarmConfiguration": "CUSTOM"},
        recording_options=lambda cid: {"recordSound": False},
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord():
    return _stub_coord()


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── Helper: _is_gen2_indoor ───────────────────────────────────────────────────

class TestIsGen2Indoor:
    def test_returns_true_for_home_eyes_indoor(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import _is_gen2_indoor, BoschTimestampSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        assert _is_gen2_indoor(entity) is True, "HOME_Eyes_Indoor must be identified as Gen2 Indoor"

    def test_returns_false_for_outdoor(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import _is_gen2_indoor, BoschTimestampSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        assert _is_gen2_indoor(entity) is False, "Outdoor camera must not be Gen2 Indoor"

    def test_returns_true_for_camera_indoor_gen2(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import _is_gen2_indoor, BoschTimestampSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_INDOOR_GEN2"
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        assert _is_gen2_indoor(entity) is True, "CAMERA_INDOOR_GEN2 must be identified as Gen2 Indoor"


# ── Helper: _warn_if_privacy_on ───────────────────────────────────────────────

class TestWarnIfPrivacyOn:
    @pytest.mark.asyncio
    async def test_returns_false_when_privacy_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import _warn_if_privacy_on, BoschTimestampSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = False
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        result = await _warn_if_privacy_on(entity, "Test")
        assert result is False, "Must return False when privacy mode is off"

    @pytest.mark.asyncio
    async def test_returns_true_when_privacy_on_and_sends_notification(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import _warn_if_privacy_on, BoschTimestampSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord.data[CAM_ID]["info"]["title"] = "Garten"
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        services_mock = AsyncMock()
        entity.hass = SimpleNamespace(services=SimpleNamespace(async_call=services_mock))
        result = await _warn_if_privacy_on(entity, "Einbrucherkennung")
        assert result is True, "Must return True to block the write when privacy is on"
        services_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_notification_error(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import _warn_if_privacy_on, BoschTimestampSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        entity.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock(side_effect=Exception("svc down"))))
        # Must not raise even if persistent_notification.create fails
        result = await _warn_if_privacy_on(entity, "X")
        assert result is True, "Must still block the write even when notification fails"


# ── BoschPrivacySoundSwitch turn_on / turn_off ────────────────────────────────

class TestPrivacySoundSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_updates_cache_on_success(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschPrivacySoundSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord._privacy_sound_cache[CAM_ID] is True, "Cache must be updated to True on success"

    @pytest.mark.asyncio
    async def test_turn_off_updates_cache_on_success(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschPrivacySoundSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord._privacy_sound_cache[CAM_ID] is False, "Cache must be updated to False on success"

    @pytest.mark.asyncio
    async def test_turn_on_no_cache_update_on_failure(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch
        stub_coord._privacy_sound_cache[CAM_ID] = True  # was ON
        stub_coord.async_put_camera = AsyncMock(return_value=False)
        entity = BoschPrivacySoundSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()  # attempt to turn off
        assert stub_coord._privacy_sound_cache[CAM_ID] is True, "Cache must stay True when PUT fails"


# ── BoschTimestampSwitch turn_on / turn_off ───────────────────────────────────

class TestTimestampSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sets_cache_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch
        stub_coord.async_put_camera = AsyncMock()
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord._timestamp_cache[CAM_ID] is True, "Timestamp cache must be True after turn_on"

    @pytest.mark.asyncio
    async def test_turn_off_sets_cache_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch
        stub_coord.async_put_camera = AsyncMock()
        entity = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord._timestamp_cache[CAM_ID] is False, "Timestamp cache must be False after turn_off"


# ── BoschNotificationTypeSwitch ───────────────────────────────────────────────

class TestNotificationTypeSwitch:
    def test_is_on_reads_from_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        stub_coord._notifications_cache[CAM_ID] = {"movement": True, "person": False}
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "movement")
        assert entity.is_on is True, "Must read movement flag from notifications cache"

    def test_is_on_false_for_disabled_type(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        stub_coord._notifications_cache[CAM_ID] = {"movement": True, "person": False}
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "person")
        assert entity.is_on is False, "Must return False when type is False in cache"

    def test_is_on_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        stub_coord._notifications_cache = {}
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "movement")
        assert entity.is_on is None, "Must return None when no cache exists"

    def test_available_false_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        stub_coord._notifications_cache = {}
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "movement")
        assert entity.available is False, "Must be unavailable when notifications cache is empty"

    def test_available_true_when_cache_populated(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        stub_coord._notifications_cache[CAM_ID] = {"movement": True}
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "movement")
        assert entity.available is True, "Must be available when coordinator succeeded and cache exists"

    def test_translation_key_normalises_camelcase(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "cameraAlarm")
        assert entity._attr_translation_key == "notification_type_camera_alarm", \
            "cameraAlarm must map to notification_type_camera_alarm (snake_case)"

    def test_trouble_email_normalised(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "troubleEmail")
        assert entity._attr_translation_key == "notification_type_trouble_email", \
            "troubleEmail must map to notification_type_trouble_email"

    @pytest.mark.asyncio
    async def test_turn_on_merges_with_existing_flags(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        stub_coord._notifications_cache[CAM_ID] = {"movement": False, "person": True, "audio": False}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "movement")
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        sent = stub_coord.async_put_camera.call_args[0][2]
        assert sent["movement"] is True, "turn_on must set movement=True"
        assert sent["person"] is True, "turn_on must preserve person=True"

    @pytest.mark.asyncio
    async def test_turn_off_updates_single_flag(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch
        stub_coord._notifications_cache[CAM_ID] = {"movement": True}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschNotificationTypeSwitch(stub_coord, CAM_ID, stub_entry, "movement")
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord._notifications_cache[CAM_ID]["movement"] is False, \
            "Cache must reflect False after successful turn_off"


# ── BoschAlarmSystemArmSwitch ─────────────────────────────────────────────────

class TestAlarmSystemArmSwitch:
    def test_is_on_reads_arming_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        stub_coord._arming_cache[CAM_ID] = True
        entity = BoschAlarmSystemArmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "Must read arming state from _arming_cache"

    def test_is_on_none_when_no_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        stub_coord._arming_cache = {}
        entity = BoschAlarmSystemArmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is None, "Must return None when not yet known"

    def test_available_requires_camera_online(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        stub_coord.is_camera_online = lambda cid: False
        entity = BoschAlarmSystemArmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when camera is offline"

    def test_extra_attrs_include_alarm_status(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        stub_coord._alarm_status_cache[CAM_ID] = {"alarmType": "INTRUSION", "intrusionSystem": "ARMED"}
        entity = BoschAlarmSystemArmSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["alarm_type"] == "INTRUSION", "extra_attrs must expose alarmType"
        assert attrs["intrusion_system"] == "ARMED", "extra_attrs must expose intrusionSystem"

    @pytest.mark.asyncio
    async def test_turn_on_updates_arming_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmSystemArmSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord._arming_cache[CAM_ID] is True, "Cache must be True after arm"

    @pytest.mark.asyncio
    async def test_turn_off_updates_arming_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        stub_coord._arming_cache[CAM_ID] = True
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmSystemArmSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord._arming_cache[CAM_ID] is False, "Cache must be False after disarm"


# ── _BoschAlarmSettingsSwitchBase / BoschAlarmModeSwitch / BoschPreAlarmSwitch ─

class TestAlarmSettingsSwitchBase:
    def test_is_on_true_when_field_is_ON(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache[CAM_ID] = {"alarmMode": "ON", "preAlarmMode": "OFF"}
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "alarmMode=ON must yield is_on=True"

    def test_is_on_false_when_field_is_OFF(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache[CAM_ID] = {"alarmMode": "OFF"}
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is False, "alarmMode=OFF must yield is_on=False"

    def test_is_on_none_when_field_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache = {}
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is None, "Must return None when no alarm settings cached"

    def test_available_false_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache = {}
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when alarm settings cache is empty"

    def test_available_true_when_cache_and_online(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache[CAM_ID] = {"alarmMode": "ON"}
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when coordinator ok and cache populated"

    @pytest.mark.asyncio
    async def test_set_updates_field_to_ON(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache[CAM_ID] = {"alarmMode": "OFF", "preAlarmMode": "OFF"}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord._alarm_settings_cache[CAM_ID]["alarmMode"] == "ON", \
            "_set must write alarmMode=ON on turn_on"

    @pytest.mark.asyncio
    async def test_set_updates_field_to_OFF(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache[CAM_ID] = {"alarmMode": "ON", "preAlarmMode": "OFF"}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord._alarm_settings_cache[CAM_ID]["alarmMode"] == "OFF", \
            "_set must write alarmMode=OFF on turn_off"

    @pytest.mark.asyncio
    async def test_set_no_op_when_cache_empty(self, stub_coord, stub_entry):
        """No crash or API call when alarm_settings cache is empty (camera not yet polled)."""
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch
        stub_coord._alarm_settings_cache = {}
        stub_coord.async_put_camera = AsyncMock()
        entity = BoschAlarmModeSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        stub_coord.async_put_camera.assert_not_called()

    def test_prealarm_reads_prealarmmode_field(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPreAlarmSwitch
        stub_coord._alarm_settings_cache[CAM_ID] = {"alarmMode": "OFF", "preAlarmMode": "ON"}
        entity = BoschPreAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "BoschPreAlarmSwitch must read preAlarmMode not alarmMode"


# ── BoschAudioAlarmSwitch ─────────────────────────────────────────────────────

class TestAudioAlarmSwitch:
    def test_is_on_reads_enabled_from_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.audio_alarm_settings = lambda cid: {"enabled": True, "threshold": 60}
        entity = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "is_on must reflect enabled flag from audio_alarm_settings"

    def test_is_on_false_when_disabled(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.audio_alarm_settings = lambda cid: {"enabled": False}
        entity = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is False, "is_on must be False when enabled=False"

    def test_is_on_none_when_no_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.audio_alarm_settings = lambda cid: None
        entity = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is None, "is_on must be None when audio_alarm_settings returns None"

    def test_available_requires_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.audio_alarm_settings = lambda cid: {}
        entity = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when settings dict is empty"

    def test_extra_attrs_expose_threshold_and_config(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.audio_alarm_settings = lambda cid: {
            "enabled": True, "threshold": 42, "sensitivity": "LOW",
            "audioAlarmConfiguration": "CUSTOM",
        }
        entity = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["threshold"] == 42, "extra_attrs must expose threshold"
        assert attrs["configuration"] == "CUSTOM", "extra_attrs must expose audioAlarmConfiguration"

    @pytest.mark.asyncio
    async def test_turn_on_updates_enabled_in_cam_data(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        settings = {"enabled": False, "threshold": 50, "sensitivity": "MEDIUM", "audioAlarmConfiguration": "CUSTOM"}
        stub_coord.audio_alarm_settings = lambda cid: dict(settings)
        stub_coord.data[CAM_ID]["audioAlarm"] = dict(settings)
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord.data[CAM_ID]["audioAlarm"]["enabled"] is True, \
            "coordinator.data audioAlarm must be updated on successful turn_on"

    @pytest.mark.asyncio
    async def test_gen2_indoor_privacy_blocks_turn_on(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        entity = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        hass_mock = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
        entity.hass = hass_mock
        stub_coord.async_put_camera = AsyncMock()
        await entity.async_turn_on()
        stub_coord.async_put_camera.assert_not_called()


# ── BoschImageRotation180Switch ───────────────────────────────────────────────

class TestImageRotation180Switch:
    def test_is_on_reads_rotation_dict(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch
        stub_coord._image_rotation_180 = {CAM_ID: True}
        entity = BoschImageRotation180Switch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "is_on must read from _image_rotation_180"

    def test_is_on_false_when_not_set(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch
        stub_coord._image_rotation_180 = {}
        entity = BoschImageRotation180Switch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is False, "is_on must default to False when not in dict"

    def test_available_requires_only_coordinator_success(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch
        stub_coord.is_camera_online = lambda cid: False  # camera offline — still available
        entity = BoschImageRotation180Switch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "ImageRotation is client-side — available even when camera offline"

    @pytest.mark.asyncio
    async def test_turn_on_sets_dict_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch
        entity = BoschImageRotation180Switch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord._image_rotation_180[CAM_ID] is True, "turn_on must set _image_rotation_180[cam_id]=True"
        stub_coord.async_update_listeners.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_sets_dict_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch
        stub_coord._image_rotation_180 = {CAM_ID: True}
        entity = BoschImageRotation180Switch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord._image_rotation_180[CAM_ID] is False, "turn_off must clear _image_rotation_180[cam_id]"

    @pytest.mark.asyncio
    async def test_async_added_to_hass_restores_on_state(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch
        entity = BoschImageRotation180Switch(stub_coord, CAM_ID, stub_entry)
        last_state = SimpleNamespace(state="on")
        entity.async_get_last_state = AsyncMock(return_value=last_state)
        # Patch both parent async_added_to_hass to avoid HA runtime dependency
        with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass", AsyncMock()), \
             patch("homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass", AsyncMock()):
            await entity.async_added_to_hass()
        assert stub_coord._image_rotation_180[CAM_ID] is True, \
            "Must restore ON state from previous HA run"

    @pytest.mark.asyncio
    async def test_async_added_to_hass_no_op_for_off_state(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch
        entity = BoschImageRotation180Switch(stub_coord, CAM_ID, stub_entry)
        last_state = SimpleNamespace(state="off")
        entity.async_get_last_state = AsyncMock(return_value=last_state)
        with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass", AsyncMock()), \
             patch("homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass", AsyncMock()):
            await entity.async_added_to_hass()
        assert not stub_coord._image_rotation_180.get(CAM_ID), \
            "Must not set rotation flag when previous state was off"


# ── BoschNvrRecordingSwitch ───────────────────────────────────────────────────

class TestNvrRecordingSwitch:
    def test_is_on_reads_nvr_user_intent(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_coord._nvr_user_intent[CAM_ID] = True
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "is_on must read from _nvr_user_intent"

    def test_is_on_false_when_not_set(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is False, "is_on must default to False"

    def test_available_false_when_coordinator_failed(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_coord.last_update_success = False
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when coordinator failed"

    def test_available_false_when_camera_offline(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_coord.is_camera_online = lambda cid: False
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when camera is offline"

    def test_available_false_when_no_live_connection(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_coord._live_connections = {}  # no active stream
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when no live connection exists"

    def test_available_false_when_live_is_remote(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_coord._live_connections[CAM_ID] = {"_connection_type": "REMOTE"}
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "NVR is LAN-only — must be unavailable when REMOTE"

    def test_available_true_when_local_stream_active(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_coord._live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when LOCAL stream is active"

    def test_extra_attrs_exposes_ffmpeg_state(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_coord._nvr_processes[CAM_ID] = MagicMock(returncode=None)  # running
        stub_coord._live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["ffmpeg_running"] is True, "extra_attrs must surface ffmpeg_running=True when process alive"
        assert attrs["connection_type"] == "LOCAL", "extra_attrs must include connection_type"

    def test_entity_disabled_by_default(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        entity = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity._attr_entity_registry_enabled_default is False, \
            "NVR switch must be opt-in (disabled by default)"


# ── BoschMotionLightSwitch (is_on from cache) ─────────────────────────────────

class TestMotionLightSwitchIsOn:
    def test_is_on_reads_from_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        stub_coord._motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": True}
        entity = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "is_on must read lightOnMotionEnabled from motion_light_cache"

    def test_is_on_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        stub_coord._motion_light_cache = {}
        entity = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is None, "is_on must be None when cache is empty (not yet polled)"

    @pytest.mark.asyncio
    async def test_set_motion_light_updates_cache_on_success(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        stub_coord._motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": False, "delay": 30}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        entity = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord._motion_light_cache[CAM_ID]["lightOnMotionEnabled"] is True, \
            "Cache must be updated after successful PUT"


# ── BoschSoftLightFadingSwitch ────────────────────────────────────────────────

class TestSoftLightFadingSwitch:
    def test_is_on_reads_softlightfading(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord._global_lighting_cache[CAM_ID] = {"softLightFading": True, "darknessThreshold": 0.5}
        entity = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "is_on must read softLightFading from global lighting cache"

    def test_is_on_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        entity = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is None, "is_on must be None when cache empty"

    def test_available_false_when_no_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        entity = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when global_lighting_cache is empty"

    def test_available_true_when_cache_present(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord._global_lighting_cache[CAM_ID] = {"softLightFading": False}
        entity = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when cache exists"


# ── BoschIntrusionDetectionSwitch ─────────────────────────────────────────────

class TestIntrusionDetectionSwitch:
    def test_is_on_reads_enabled_from_intrusion_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch
        stub_coord._intrusion_config_cache[CAM_ID] = {
            "enabled": True, "sensitivity": 3, "detectionMode": "STANDARD", "distance": 5.0
        }
        entity = BoschIntrusionDetectionSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is True, "is_on must read 'enabled' from _intrusion_config_cache"

    def test_is_on_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch
        entity = BoschIntrusionDetectionSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.is_on is None, "is_on must be None when cache is empty"

    def test_available_false_when_intrusion_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch
        entity = BoschIntrusionDetectionSwitch(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when intrusion config not yet polled"

    def test_extra_attrs_expose_sensitivity_and_mode(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch
        stub_coord._intrusion_config_cache[CAM_ID] = {
            "enabled": True, "sensitivity": 4, "detectionMode": "HIGH_SENSITIVITY", "distance": 8.0
        }
        entity = BoschIntrusionDetectionSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["sensitivity"] == 4, "extra_attrs must expose sensitivity"
        assert attrs["detection_mode"] == "HIGH_SENSITIVITY", "extra_attrs must expose detectionMode"
        assert attrs["distance_meters"] == 8.0, "extra_attrs must expose distance"

    @pytest.mark.asyncio
    async def test_privacy_blocks_set_intrusion(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord._intrusion_config_cache[CAM_ID] = {"enabled": False, "sensitivity": 3}
        stub_coord.async_put_camera = AsyncMock()
        entity = BoschIntrusionDetectionSwitch(stub_coord, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        entity.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
        await entity.async_turn_on()
        stub_coord.async_put_camera.assert_not_called()
