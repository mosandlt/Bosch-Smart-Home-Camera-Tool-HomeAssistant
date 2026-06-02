"""Mode-pin tests for switch entities not covered (or only partially covered) in earlier rounds.

Covers ON/OFF value pins + is_on state for:
  - BoschNotificationTypeSwitch (audio, trouble, cameraAlarm, troubleEmail)
  - BoschTimestampSwitch
  - BoschIntercomSwitch
  - BoschAutoFollowSwitch
  - BoschRecordSoundSwitch
  - BoschStatusLedSwitch
  - BoschMotionLightSwitch
  - BoschAmbientLightSwitch
  - BoschSoftLightFadingSwitch
  - BoschPreAlarmSwitch
  - BoschPanicAlarmSwitch
  - BoschFrontLightSwitch
  - BoschWallwasherSwitch
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _stub_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0, "sound": True},
                    "featureStatus": {},
                },
                "status": "ONLINE",
                "autofollow": {"result": False},
                "recordingOptions": {"recordSound": False},
            }
        },
        _live_connections={},
        _user_intent_streams=set(),
        _shc_state_cache={
            CAM_ID: {
                "privacy_mode": False,
                "camera_light": True,
                "front_light": False,
                "wallwasher": False,
            }
        },
        _session_stale={},
        _stream_warming=set(),
        _privacy_set_at={},
        _light_set_at={},
        _audio_enabled={CAM_ID: True},
        _privacy_sound_cache={CAM_ID: False},
        _privacy_sound_set_at={},
        _timestamp_cache={CAM_ID: True},
        _timestamp_set_at={},
        _ledlights_cache={CAM_ID: True},
        _ledlights_set_at={},
        _motion_light_cache={},
        _ambient_lighting_cache={},
        _global_lighting_cache={},
        _intrusion_config_cache={},
        _intrusion_config_set_at={},
        _motion_set_at={},
        _alarm_settings_set_at={},
        _notifications_cache={},
        _arming_cache={},
        _arming_set_at={},
        _alarm_status_cache={},
        _alarm_settings_cache={},
        _image_rotation_180={},
        _nvr_user_intent={},
        _nvr_processes={},
        _nvr_preroll_processes={},
        _nvr_preroll_tasks={},
        _nvr_error_state={},
        _bg_tasks=set(),
        _rcp_privacy_cache={},
        _hw_version={},
        last_update_success=True,
        options={},
        token="tok-A",
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
        motion_settings=lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        },
        recording_options=lambda cid: {"recordSound": False},
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_notifications=AsyncMock(),
        _tear_down_live_stream=AsyncMock(),
        try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        record_stream_error=MagicMock(),
        record_stream_success=MagicMock(),
        _stop_tls_proxy=AsyncMock(),
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def coord():
    return _stub_coord()


@pytest.fixture
def entry():
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "x"},
        options={"enable_snapshot_button": True, "enable_nvr": False},
    )


def _bind_hass(sw):
    sw.hass = SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    sw.async_write_ha_state = MagicMock()


# ─────────────────────────────────────────────────────────────────────────────
# BoschNotificationTypeSwitch — per ntype
# ─────────────────────────────────────────────────────────────────────────────


class TestNotificationTypeSwitchAudio:
    """ntype='audio' — MISSING entirely in previous rounds."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "audio")

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_audio"

    def test_is_on_true_when_cache_true(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"audio": True, "movement": False}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_when_cache_false(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"audio": False, "movement": True}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_true_and_caches(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"audio": False, "movement": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"audio": True, "movement": True}
        )
        assert coord._notifications_cache[CAM_ID]["audio"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_false_and_caches(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"audio": True, "movement": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"audio": False, "movement": True}
        )
        assert coord._notifications_cache[CAM_ID]["audio"] is False


class TestNotificationTypeSwitchTrouble:
    """ntype='trouble' — ON/OFF MISSING in previous rounds."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "trouble")

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_trouble"

    def test_is_on_true(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"trouble": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"trouble": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_correct_payload(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"trouble": False, "person": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"trouble": True, "person": True}
        )
        assert coord._notifications_cache[CAM_ID]["trouble"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_correct_payload(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"trouble": True, "person": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"trouble": False, "person": True}
        )
        assert coord._notifications_cache[CAM_ID]["trouble"] is False


class TestNotificationTypeSwitchCameraAlarm:
    """ntype='cameraAlarm' — had ON only, need OFF pin."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "cameraAlarm")

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_cameraAlarm"

    def test_is_on_true(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"cameraAlarm": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"cameraAlarm": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_true(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"cameraAlarm": False}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"cameraAlarm": True}
        )
        assert coord._notifications_cache[CAM_ID]["cameraAlarm"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_false(self, coord, entry):
        """OFF pin — previously missing."""
        coord._notifications_cache[CAM_ID] = {"cameraAlarm": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"cameraAlarm": False}
        )
        assert coord._notifications_cache[CAM_ID]["cameraAlarm"] is False


class TestNotificationTypeSwitchTroubleEmail:
    """ntype='troubleEmail' — had ON only, need OFF pin."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "troubleEmail")

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_troubleEmail"

    def test_is_on_true(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"troubleEmail": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"troubleEmail": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_true(self, coord, entry):
        coord._notifications_cache[CAM_ID] = {"troubleEmail": False}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"troubleEmail": True}
        )
        assert coord._notifications_cache[CAM_ID]["troubleEmail"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_false(self, coord, entry):
        """OFF pin — previously missing."""
        coord._notifications_cache[CAM_ID] = {"troubleEmail": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"troubleEmail": False}
        )
        assert coord._notifications_cache[CAM_ID]["troubleEmail"] is False


# ─────────────────────────────────────────────────────────────────────────────
# BoschTimestampSwitch
# ─────────────────────────────────────────────────────────────────────────────


class TestTimestampSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        return BoschTimestampSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_timestamp"

    def test_is_on_true(self, coord, entry):
        coord._timestamp_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._timestamp_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_missing(self, coord, entry):
        coord._timestamp_cache = {}
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_result_true_and_caches(self, coord, entry):
        coord._timestamp_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "timestamp", {"result": True}
        )
        assert coord._timestamp_cache[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_result_false_and_caches(self, coord, entry):
        coord._timestamp_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "timestamp", {"result": False}
        )
        assert coord._timestamp_cache[CAM_ID] is False


# ─────────────────────────────────────────────────────────────────────────────
# BoschIntercomSwitch — unique_id + is_on default + ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestIntercomSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        return BoschIntercomSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_intercom"

    def test_is_on_defaults_false(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_sends_audioEnabled_true_speakerlevel(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=mock_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_clientsession",
            return_value=mock_session,
        ):
            await sw.async_turn_on()
        _, call_kwargs = mock_session.put.call_args
        body = call_kwargs.get("json", {})
        assert body["audioEnabled"] is True
        assert body["SpeakerLevel"] == 50
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sends_audioEnabled_false(self, coord, entry):
        sw = self._make(coord, entry)
        sw._is_on = True
        _bind_hass(sw)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=mock_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_clientsession",
            return_value=mock_session,
        ):
            await sw.async_turn_off()
        _, call_kwargs = mock_session.put.call_args
        body = call_kwargs.get("json", {})
        assert body["audioEnabled"] is False
        assert "SpeakerLevel" not in body
        assert sw.is_on is False


# ─────────────────────────────────────────────────────────────────────────────
# BoschAutoFollowSwitch
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoFollowSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch

        return BoschAutoFollowSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_autofollow"

    def test_is_on_true(self, coord, entry):
        coord.data[CAM_ID]["autofollow"] = {"result": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord.data[CAM_ID]["autofollow"] = {"result": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_data(self, coord, entry):
        coord.data[CAM_ID]["autofollow"] = None
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_result_true(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "autofollow", {"result": True}
        )

    @pytest.mark.asyncio
    async def test_turn_off_puts_result_false(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "autofollow", {"result": False}
        )


# ─────────────────────────────────────────────────────────────────────────────
# BoschRecordSoundSwitch
# ─────────────────────────────────────────────────────────────────────────────


class TestRecordSoundSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

        return BoschRecordSoundSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_record_sound"

    def test_is_on_true(self, coord, entry):
        coord.recording_options = lambda cid: {"recordSound": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord.recording_options = lambda cid: {"recordSound": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_opts(self, coord, entry):
        coord.recording_options = lambda cid: None
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_recordSound_true(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "recording_options", {"recordSound": True}
        )

    @pytest.mark.asyncio
    async def test_turn_off_puts_recordSound_false(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "recording_options", {"recordSound": False}
        )


# ─────────────────────────────────────────────────────────────────────────────
# BoschStatusLedSwitch — ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestStatusLedSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        return BoschStatusLedSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_ledlights"

    def test_is_on_true(self, coord, entry):
        coord._ledlights_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._ledlights_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_state_ON_and_caches_true(self, coord, entry):
        coord._ledlights_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "ledlights", {"state": "ON"}
        )
        assert coord._ledlights_cache[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_state_OFF_and_caches_false(self, coord, entry):
        coord._ledlights_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "ledlights", {"state": "OFF"}
        )
        assert coord._ledlights_cache[CAM_ID] is False


# ─────────────────────────────────────────────────────────────────────────────
# BoschMotionLightSwitch — ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestMotionLightSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        return BoschMotionLightSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_motion_light"

    def test_is_on_true_from_cache(self, coord, entry):
        coord._motion_light_cache[CAM_ID] = {
            "lightOnMotionEnabled": True,
            "sensitivity": "HIGH",
        }
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_from_cache(self, coord, entry):
        coord._motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_sets_lightOnMotionEnabled_true_in_put(self, coord, entry):
        coord._motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": False, "delay": 30}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once()
        # positional args: (cam_id, endpoint, body)
        put_body = coord.async_put_camera.await_args_list[0][0][2]
        assert put_body["lightOnMotionEnabled"] is True
        assert sw._is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_lightOnMotionEnabled_false_in_put(self, coord, entry):
        coord._motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": True, "delay": 30}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once()
        put_body = coord.async_put_camera.await_args_list[0][0][2]
        assert put_body["lightOnMotionEnabled"] is False
        assert sw._is_on is False


# ─────────────────────────────────────────────────────────────────────────────
# BoschAmbientLightSwitch — ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestAmbientLightSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        return BoschAmbientLightSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_ambient_light"

    def test_is_on_true_from_cache(self, coord, entry):
        coord._ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_from_cache(self, coord, entry):
        coord._ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_ambient_with_true(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_on()
        sw._set_ambient_light.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_ambient_with_false(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_off()
        sw._set_ambient_light.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_set_ambient_light_on_sends_ambientLightEnabled_true(
        self, coord, entry
    ):
        """Full HTTP path: PUT body must include ambientLightEnabled=True."""
        sw = self._make(coord, entry)
        _bind_hass(sw)
        get_resp = MagicMock()
        get_resp.status = 200
        get_resp.json = AsyncMock(
            return_value={"ambientLightEnabled": False, "schedule": "DUSK"}
        )
        put_resp = MagicMock()
        put_resp.status = 204
        get_ctx = MagicMock()
        get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=None)
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=get_ctx)
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_clientsession",
            return_value=mock_session,
        ):
            await sw._set_ambient_light(True)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["ambientLightEnabled"] is True
        assert sw._is_on is True

    @pytest.mark.asyncio
    async def test_set_ambient_light_off_sends_ambientLightEnabled_false(
        self, coord, entry
    ):
        """Full HTTP path: PUT body must include ambientLightEnabled=False."""
        sw = self._make(coord, entry)
        _bind_hass(sw)
        get_resp = MagicMock()
        get_resp.status = 200
        get_resp.json = AsyncMock(
            return_value={"ambientLightEnabled": True, "schedule": "DUSK"}
        )
        put_resp = MagicMock()
        put_resp.status = 200
        get_ctx = MagicMock()
        get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=None)
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=get_ctx)
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_clientsession",
            return_value=mock_session,
        ):
            await sw._set_ambient_light(False)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["ambientLightEnabled"] is False
        assert sw._is_on is False


# ─────────────────────────────────────────────────────────────────────────────
# BoschSoftLightFadingSwitch — ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestSoftLightFadingSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        return BoschSoftLightFadingSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_soft_light_fading"

    def test_is_on_true(self, coord, entry):
        coord._global_lighting_cache[CAM_ID] = {
            "softLightFading": True,
            "darknessThreshold": 0.5,
        }
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._global_lighting_cache[CAM_ID] = {
            "softLightFading": False,
            "darknessThreshold": 0.5,
        }
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_delegates_put_global_true(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_on()
        sw._put_global_lighting.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_delegates_put_global_false(self, coord, entry):
        sw = self._make(coord, entry)
        _bind_hass(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_off()
        sw._put_global_lighting.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_put_global_on_sends_softLightFading_true(self, coord, entry):
        coord._global_lighting_cache[CAM_ID] = {
            "softLightFading": False,
            "darknessThreshold": 0.4,
        }
        sw = self._make(coord, entry)
        _bind_hass(sw)
        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.json = AsyncMock(
            return_value={"softLightFading": True, "darknessThreshold": 0.4}
        )
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_clientsession",
            return_value=mock_session,
        ):
            await sw._put_global_lighting(True)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["softLightFading"] is True
        assert coord._global_lighting_cache[CAM_ID]["softLightFading"] is True

    @pytest.mark.asyncio
    async def test_put_global_off_sends_softLightFading_false(self, coord, entry):
        coord._global_lighting_cache[CAM_ID] = {
            "softLightFading": True,
            "darknessThreshold": 0.6,
        }
        sw = self._make(coord, entry)
        _bind_hass(sw)
        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.json = AsyncMock(
            return_value={"softLightFading": False, "darknessThreshold": 0.6}
        )
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_clientsession",
            return_value=mock_session,
        ):
            await sw._put_global_lighting(False)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["softLightFading"] is False
        assert coord._global_lighting_cache[CAM_ID]["softLightFading"] is False


# ─────────────────────────────────────────────────────────────────────────────
# BoschPreAlarmSwitch — ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestPreAlarmSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschPreAlarmSwitch

        return BoschPreAlarmSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_prealarm"

    def test_is_on_true_when_setting_ON(self, coord, entry):
        coord._alarm_settings_cache[CAM_ID] = {"preAlarmMode": "ON", "alarmMode": "OFF"}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_when_setting_OFF(self, coord, entry):
        coord._alarm_settings_cache[CAM_ID] = {"preAlarmMode": "OFF", "alarmMode": "ON"}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_preAlarmMode_ON(self, coord, entry):
        coord._alarm_settings_cache[CAM_ID] = {"preAlarmMode": "OFF", "alarmMode": "ON"}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "alarm_settings", {"preAlarmMode": "ON", "alarmMode": "ON"}
        )
        assert coord._alarm_settings_cache[CAM_ID]["preAlarmMode"] == "ON"

    @pytest.mark.asyncio
    async def test_turn_off_puts_preAlarmMode_OFF(self, coord, entry):
        coord._alarm_settings_cache[CAM_ID] = {"preAlarmMode": "ON", "alarmMode": "ON"}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "alarm_settings", {"preAlarmMode": "OFF", "alarmMode": "ON"}
        )
        assert coord._alarm_settings_cache[CAM_ID]["preAlarmMode"] == "OFF"


# ─────────────────────────────────────────────────────────────────────────────
# BoschPanicAlarmSwitch — ON/OFF value pins
# NOTE: panic_alarm is a momentary/stateful siren — there is no cloud GET
#       endpoint, so state is purely local. Both ON and OFF send PUT.
# ─────────────────────────────────────────────────────────────────────────────


class TestPanicAlarmSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

        return BoschPanicAlarmSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_panic_alarm"

    def test_is_on_false_by_default(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_true_when_cache_true(self, coord, entry):
        coord._panic_alarm_cache = {CAM_ID: True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_sends_status_ON(self, coord, entry):
        """PUT body must carry {"status": "ON"}."""
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "panic_alarm", {"status": "ON"}
        )
        assert coord._panic_alarm_cache[CAM_ID] is True
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sends_status_OFF(self, coord, entry):
        """PUT body must carry {"status": "OFF"}."""
        coord._panic_alarm_cache = {CAM_ID: True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "panic_alarm", {"status": "OFF"}
        )
        assert coord._panic_alarm_cache[CAM_ID] is False
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_blocked_by_privacy(self, coord, entry):
        """Privacy-ON blocks turn_on — PUT must not be called."""
        coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# BoschFrontLightSwitch — ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestFrontLightSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        return BoschFrontLightSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_front_light_{CAM_ID.lower()}"

    def test_is_on_true(self, coord, entry):
        coord._shc_state_cache[CAM_ID]["front_light"] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._shc_state_cache[CAM_ID]["front_light"] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_cloud_set_light_component_front_true(
        self, coord, entry
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_on()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "front", True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_set_light_component_front_false(
        self, coord, entry
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_off()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "front", False
        )


# ─────────────────────────────────────────────────────────────────────────────
# BoschWallwasherSwitch — ON/OFF value pins
# ─────────────────────────────────────────────────────────────────────────────


class TestWallwasherSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch

        return BoschWallwasherSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord, entry):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_wallwasher_{CAM_ID.lower()}"

    def test_is_on_true(self, coord, entry):
        coord._shc_state_cache[CAM_ID]["wallwasher"] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord, entry):
        coord._shc_state_cache[CAM_ID]["wallwasher"] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_cloud_set_light_component_wallwasher_true(
        self, coord, entry
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_on()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "wallwasher", True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_set_light_component_wallwasher_false(
        self, coord, entry
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_off()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "wallwasher", False
        )
