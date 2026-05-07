"""Switch coverage sprint MA — targets lines not yet covered by previous rounds.

Covers:
  - async_setup_entry: early return (line 150) + entity creation paths (166-237)
  - BoschLiveStreamSwitch.async_turn_on: privacy guard, cooldown guard, success/fail (285-326)
  - BoschLiveStreamSwitch.async_turn_off (428-437)
  - BoschAudioSwitch.async_turn_on/off (464-474) + _apply_audio_change privacy guard
    and live-reconnect paths (479-487)
  - BoschCameraLightSwitch.available (511, 520)
  - BoschFrontLightSwitch / BoschWallwasherSwitch is_on (550, 582, 588)
  - BoschMotionEnabledSwitch.async_turn_on/off gen2 privacy guard (690, 721)
  - BoschIntercomSwitch.__init__ + is_on (889-896)
  - BoschStatusLedSwitch.async_turn_on/off (1076-1090) + available (1122, 1135)
  - BoschMotionLightSwitch._set_motion_light cache-hit path (1146-1150, 1162, 1169)
  - BoschAmbientLightSwitch is_on/available/turn_on/off (1182-1196, 1200, 1227-1231)
  - BoschSoftLightFadingSwitch._put_global_lighting + turn_on/off (1206-1225, 1228, 1231,
    1266-1291, 1294, 1297)
  - BoschIntrusionDetectionSwitch._set_intrusion privacy guard (1346-1350)
  - BoschAudioAlarmSwitch._set gen2 privacy guard + turn_on/off (1620, 1636-1640)
  - BoschNvrRecordingSwitch.async_added_to_hass restore state path (1777-1794)
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM_ID2 = "20E053B5-0000-0000-0000-000000000002"


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
                    "featureStatus": {},
                },
                "status": "ONLINE",
                "events": [],
                "autofollow": {"result": False},
                "recordingOptions": {"recordSound": False},
                "audioAlarm": {"enabled": True, "threshold": 50, "sensitivity": "MEDIUM", "audioAlarmConfiguration": "CUSTOM"},
            }
        },
        _live_connections={},
        _shc_state_cache={
            CAM_ID: {
                "privacy_mode": False,
                "camera_light": True,
                "front_light": True,
                "wallwasher": False,
                "notifications_status": "FOLLOW_CAMERA_SCHEDULE",
                "has_light": True,
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
        _notifications_cache={},
        _arming_cache={},
        _arming_set_at={},
        _alarm_status_cache={},
        _alarm_settings_cache={},
        _audio_alarm_cache={CAM_ID: {}},
        _image_rotation_180={},
        _nvr_user_intent={},
        _nvr_processes={},
        _nvr_error_state={},
        _bg_tasks=set(),
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
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_notifications=AsyncMock(),
        _tear_down_live_stream=AsyncMock(),
        try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        record_stream_error=MagicMock(),
        _stop_tls_proxy=AsyncMock(),
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord():
    return _stub_coord()


@pytest.fixture
def stub_entry():
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


# ── async_setup_entry ─────────────────────────────────────────────────────────

class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_early_return_when_snapshot_disabled(self, stub_coord, stub_entry):
        """Line 150: return early when enable_snapshot_button=False."""
        from custom_components.bosch_shc_camera.switch import async_setup_entry
        stub_entry.options = {"enable_snapshot_button": False}
        stub_entry.runtime_data = stub_coord
        added = []
        async def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        assert added == [], "No entities should be added when enable_snapshot_button=False"

    @pytest.mark.asyncio
    async def test_creates_base_entities_for_cam(self, stub_coord, stub_entry):
        """Lines 166-237: at least LiveStream + Audio + Privacy entities created."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschLiveStreamSwitch, BoschAudioSwitch, BoschPrivacyModeSwitch,
        )
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        types = [type(e) for e in added]
        assert BoschLiveStreamSwitch in types
        assert BoschAudioSwitch in types
        assert BoschPrivacyModeSwitch in types

    @pytest.mark.asyncio
    async def test_creates_light_entities_when_feature_present(self, stub_coord, stub_entry):
        """Lines 181-185: CameraLight, FrontLight, Wallwasher only when has_light=True."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschCameraLightSwitch, BoschFrontLightSwitch, BoschWallwasherSwitch,
        )
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        types = [type(e) for e in added]
        assert BoschCameraLightSwitch in types
        assert BoschFrontLightSwitch in types
        assert BoschWallwasherSwitch in types

    @pytest.mark.asyncio
    async def test_skips_light_entities_when_no_light_feature(self, stub_coord, stub_entry):
        """Lines 181-185: No light entities when featureSupport.light=False."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschCameraLightSwitch,
        )
        stub_coord.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        assert not any(isinstance(e, BoschCameraLightSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_creates_nvr_switch_when_enabled(self, stub_coord, stub_entry):
        """Line 235: NvrRecordingSwitch added only if enable_nvr=True."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschNvrRecordingSwitch,
        )
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        assert any(isinstance(e, BoschNvrRecordingSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_skips_nvr_switch_when_disabled(self, stub_coord, stub_entry):
        """Line 235: NvrRecordingSwitch NOT added when enable_nvr=False."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschNvrRecordingSwitch,
        )
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        assert not any(isinstance(e, BoschNvrRecordingSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_creates_indoor_privacy_sound_switch(self, stub_coord, stub_entry):
        """Line 201-202: PrivacySoundSwitch for indoor cameras."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschPrivacySoundSwitch,
        )
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        assert any(isinstance(e, BoschPrivacySoundSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_creates_gen2_entities_for_gen2_camera(self, stub_coord, stub_entry):
        """Lines 207-212: StatusLed, MotionLight, AmbientLight, SoftLightFading, IntrusionDetection for Gen2."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschStatusLedSwitch, BoschMotionLightSwitch,
            BoschAmbientLightSwitch, BoschSoftLightFadingSwitch, BoschIntrusionDetectionSwitch,
        )
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        types = [type(e) for e in added]
        assert BoschStatusLedSwitch in types
        assert BoschMotionLightSwitch in types
        assert BoschAmbientLightSwitch in types
        assert BoschSoftLightFadingSwitch in types
        assert BoschIntrusionDetectionSwitch in types

    @pytest.mark.asyncio
    async def test_creates_image_rotation_for_indoor(self, stub_coord, stub_entry):
        """Lines 231-232: ImageRotation180Switch for INDOOR cameras."""
        from custom_components.bosch_shc_camera.switch import (
            async_setup_entry, BoschImageRotation180Switch,
        )
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"
        stub_entry.runtime_data = stub_coord
        added = []
        def fake_add(ents, **kw):
            added.extend(ents)
        hass = MagicMock()
        with patch("custom_components.bosch_shc_camera.switch.get_options",
                   return_value=stub_entry.options):
            await async_setup_entry(hass, stub_entry, fake_add)
        assert any(isinstance(e, BoschImageRotation180Switch) for e in added)


# ── BoschLiveStreamSwitch ─────────────────────────────────────────────────────

class TestLiveStreamSwitchTurnOn:
    @pytest.mark.asyncio
    async def test_blocked_by_privacy_raises(self, stub_coord, stub_entry):
        """Line 285-289: ServiceValidationError when privacy mode is ON."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        from homeassistant.exceptions import ServiceValidationError
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        with pytest.raises(ServiceValidationError):
            await sw.async_turn_on()
        stub_coord.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_blocks_when_stream_just_stopped(self, stub_coord, stub_entry):
        """Lines 291-297: cooldown guard — no connection attempt within STREAM_COOLDOWN."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        sw._last_stream_off = time.monotonic()  # just stopped
        await sw.async_turn_on()
        stub_coord.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_allows_after_enough_time(self, stub_coord, stub_entry):
        """Cooldown should NOT block when _last_stream_off is old enough."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        sw._last_stream_off = time.monotonic() - 100  # 100 s ago — well past cooldown
        await sw.async_turn_on()
        stub_coord.try_live_connection.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_success_local_schedules_watchdog(self, stub_coord, stub_entry):
        """Lines 302-322: LOCAL result schedules _stream_health_watchdog task."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord.try_live_connection = AsyncMock(return_value={"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"})
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        sw.hass.async_create_task.assert_called_once()  # watchdog task scheduled
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_success_remote_no_watchdog(self, stub_coord, stub_entry):
        """Lines 302-322: REMOTE result does NOT schedule a watchdog."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord.try_live_connection = AsyncMock(return_value={"_connection_type": "REMOTE", "rtspsUrl": "rtsps://x"})
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        sw.hass.async_create_task.assert_not_called()
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_failure_records_stream_error(self, stub_coord, stub_entry):
        """Lines 323-325: None result → record_stream_error called."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord.try_live_connection = AsyncMock(return_value=None)
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.record_stream_error.assert_called_once_with(CAM_ID)
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_calls_teardown_and_refresh(self, stub_coord, stub_entry):
        """Lines 428-437: turn_off tears down stream, writes state, requests refresh."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord._tear_down_live_stream.assert_awaited_once_with(CAM_ID)
        sw.async_write_ha_state.assert_called_once()
        sw.hass.async_create_task.assert_called_once()  # async_request_refresh task


# ── BoschAudioSwitch ──────────────────────────────────────────────────────────

class TestAudioSwitch:
    @pytest.mark.asyncio
    async def test_turn_on_sets_flag_and_applies(self, stub_coord, stub_entry):
        """Lines 464-468: turn_on sets _audio_enabled True and calls _apply_audio_change."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        assert stub_coord._audio_enabled[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_flag_and_applies(self, stub_coord, stub_entry):
        """Lines 470-474: turn_off sets _audio_enabled False and calls _apply_audio_change."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._audio_enabled[CAM_ID] = True
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        assert stub_coord._audio_enabled[CAM_ID] is False

    @pytest.mark.asyncio
    async def test_apply_audio_skipped_during_privacy(self, stub_coord, stub_entry):
        """Lines 478-482: _apply_audio_change skips reconnect when privacy ON."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        # try_live_connection must NOT be called when privacy is active
        stub_coord.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_audio_reconnects_when_live(self, stub_coord, stub_entry):
        """Line 483-487: reconnects when stream is active."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._live_connections[CAM_ID] = {"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"}
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.try_live_connection.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_apply_audio_requests_refresh_when_not_live(self, stub_coord, stub_entry):
        """Line 489: requests refresh when no active stream."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        # No live connection
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.try_live_connection.assert_not_called()
        sw.hass.async_create_task.assert_called_once()


# ── BoschCameraLightSwitch.available ─────────────────────────────────────────

class TestCameraLightSwitchAvailable:
    def test_available_when_online(self, stub_coord, stub_entry):
        """Lines 514-523: available=True when coordinator ok + camera online."""
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch
        sw = BoschCameraLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_unavailable_when_coordinator_fails(self, stub_coord, stub_entry):
        """available=False when last_update_success=False."""
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch
        stub_coord.last_update_success = False
        sw = BoschCameraLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_unavailable_when_camera_offline(self, stub_coord, stub_entry):
        """available=False when camera offline."""
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch
        stub_coord.is_camera_online = lambda cid: False
        sw = BoschCameraLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False


# ── BoschFrontLightSwitch and BoschWallwasherSwitch is_on ─────────────────────

class TestFrontLightIsOn:
    def test_is_on_reads_from_cache(self, stub_coord, stub_entry):
        """Line 550: reads front_light from _shc_state_cache."""
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch
        stub_coord._shc_state_cache[CAM_ID]["front_light"] = True
        sw = BoschFrontLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch
        stub_coord._shc_state_cache[CAM_ID]["front_light"] = False
        sw = BoschFrontLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False


class TestWallwasherIsOn:
    def test_is_on_true(self, stub_coord, stub_entry):
        """Lines 582, 588: reads wallwasher state from cache."""
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch
        stub_coord._shc_state_cache[CAM_ID]["wallwasher"] = True
        sw = BoschWallwasherSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch
        stub_coord._shc_state_cache[CAM_ID]["wallwasher"] = False
        sw = BoschWallwasherSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False


# ── BoschMotionEnabledSwitch gen2 privacy guard ───────────────────────────────

class TestMotionEnabledSwitchGen2Privacy:
    @pytest.mark.asyncio
    async def test_turn_on_blocked_by_privacy_for_gen2_indoor(self, stub_coord, stub_entry):
        """Line 690: gen2 indoor camera blocked when privacy ON."""
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschMotionEnabledSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        # async_put_camera must NOT be called
        stub_coord.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_not_blocked_for_outdoor(self, stub_coord, stub_entry):
        """Outdoor cameras not blocked by privacy guard."""
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschMotionEnabledSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_blocked_by_privacy_for_gen2_indoor(self, stub_coord, stub_entry):
        """Line 721: gen2 indoor camera turn_off also blocked by privacy."""
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschMotionEnabledSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord.async_put_camera.assert_not_called()


# ── BoschIntercomSwitch ───────────────────────────────────────────────────────

class TestIntercomSwitch:
    def test_is_on_defaults_false(self, stub_coord, stub_entry):
        """Lines 889-896: _is_on defaults to False."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch
        sw = BoschIntercomSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_success_sets_is_on(self, stub_coord, stub_entry):
        """Lines 898-922: successful PUT sets _is_on=True."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch
        sw = BoschIntercomSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=mock_ctx)
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw.async_turn_on()
        assert sw._is_on is True
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_success_sets_is_on_false(self, stub_coord, stub_entry):
        """Lines 924-948: successful PUT sets _is_on=False."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch
        sw = BoschIntercomSwitch(stub_coord, CAM_ID, stub_entry)
        sw._is_on = True
        _bind_hass(sw)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=mock_ctx)
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw.async_turn_off()
        assert sw._is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_exception_does_not_raise(self, stub_coord, stub_entry):
        """Lines 920-922: exception inside try/except swallowed, async_write_ha_state still called."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch
        sw = BoschIntercomSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        # Exception must be raised inside the try block (from session.put), not before it
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(side_effect=Exception("network error"))
        failing_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=failing_ctx)
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw.async_turn_on()  # must not raise
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_exception_does_not_raise(self, stub_coord, stub_entry):
        """Lines 946-948: exception swallowed in turn_off."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch
        sw = BoschIntercomSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(side_effect=Exception("network error"))
        failing_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=failing_ctx)
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw.async_turn_off()
        sw.async_write_ha_state.assert_called_once()


# ── BoschStatusLedSwitch ──────────────────────────────────────────────────────

class TestStatusLedSwitch:
    @pytest.mark.asyncio
    async def test_turn_on_updates_cache(self, stub_coord, stub_entry):
        """Lines 1076-1082: turn_on PUTs {"state":"ON"} and updates cache."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch
        stub_coord._ledlights_cache[CAM_ID] = False
        sw = BoschStatusLedSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_awaited_once_with(CAM_ID, "ledlights", {"state": "ON"})
        assert stub_coord._ledlights_cache[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_updates_cache(self, stub_coord, stub_entry):
        """Lines 1084-1090: turn_off PUTs {"state":"OFF"} and updates cache."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch
        stub_coord._ledlights_cache[CAM_ID] = True
        sw = BoschStatusLedSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        stub_coord.async_put_camera.assert_awaited_once_with(CAM_ID, "ledlights", {"state": "OFF"})
        assert stub_coord._ledlights_cache[CAM_ID] is False

    def test_available_with_cache(self, stub_coord, stub_entry):
        """Lines 1069-1074: available=True only when cache has value."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch
        stub_coord._ledlights_cache[CAM_ID] = True
        sw = BoschStatusLedSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_available_false_when_cache_none(self, stub_coord, stub_entry):
        """Line 1073: available=False when ledlights cache is None."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch
        stub_coord._ledlights_cache[CAM_ID] = None
        sw = BoschStatusLedSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False


# ── BoschMotionLightSwitch ────────────────────────────────────────────────────

class TestMotionLightSwitch:
    @pytest.mark.asyncio
    async def test_turn_on_with_cached_config(self, stub_coord, stub_entry):
        """Lines 1146-1163: cache hit path — no HTTP GET, PUT directly with toggled flag."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        stub_coord._motion_light_cache[CAM_ID] = {
            "lightOnMotionEnabled": False,
            "sensitivity": "MEDIUM",
            "delay": 30,
        }
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        sw = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_awaited_once()
        assert stub_coord._motion_light_cache[CAM_ID]["lightOnMotionEnabled"] is True
        assert sw._is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_with_cached_config(self, stub_coord, stub_entry):
        """Lines 1168-1169: turn_off delegates to _set_motion_light(False)."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        stub_coord._motion_light_cache[CAM_ID] = {
            "lightOnMotionEnabled": True,
            "sensitivity": "HIGH",
        }
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        sw = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_off()
        assert stub_coord._motion_light_cache[CAM_ID]["lightOnMotionEnabled"] is False
        assert sw._is_on is False

    @pytest.mark.asyncio
    async def test_set_motion_light_no_op_on_put_fail(self, stub_coord, stub_entry):
        """Line 1162: PUT failure — cache and _is_on not updated."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        stub_coord._motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": False}
        stub_coord.async_put_camera = AsyncMock(return_value=False)
        sw = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        # _is_on must remain None (not updated on failure)
        assert sw._is_on is None

    def test_available_true(self, stub_coord, stub_entry):
        """Lines 1121-1125: available when coordinator ok + online."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        sw = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_available_false_coordinator_fail(self, stub_coord, stub_entry):
        """Line 1122: available=False when coordinator fails."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch
        stub_coord.last_update_success = False
        sw = BoschMotionLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False


# ── BoschAmbientLightSwitch ───────────────────────────────────────────────────

class TestAmbientLightSwitch:
    def test_is_on_reads_cache(self, stub_coord, stub_entry):
        """Lines 1192-1196: is_on reads from _ambient_lighting_cache."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        stub_coord._ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": True}
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_none_when_cache_empty(self, stub_coord, stub_entry):
        """is_on returns None when no cache data."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is None

    def test_available_true(self, stub_coord, stub_entry):
        """Lines 1199-1203: available when coordinator ok + online."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_available_false_when_offline(self, stub_coord, stub_entry):
        """Line 1200: available=False when camera offline."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        stub_coord.is_camera_online = lambda cid: False
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_ambient(self, stub_coord, stub_entry):
        """Lines 1227-1228: turn_on calls _set_ambient_light(True)."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        # Patch _set_ambient_light to verify delegation
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_on()
        sw._set_ambient_light.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_ambient(self, stub_coord, stub_entry):
        """Lines 1230-1231: turn_off calls _set_ambient_light(False)."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_off()
        sw._set_ambient_light.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_set_ambient_light_http_success(self, stub_coord, stub_entry):
        """Lines 1206-1225: full GET+PUT path updates _is_on on 200."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        get_resp = MagicMock()
        get_resp.status = 200
        get_resp.json = AsyncMock(return_value={"ambientLightEnabled": False, "schedule": "ALL"})
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
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw._set_ambient_light(True)
        assert sw._is_on is True
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_ambient_light_no_token_returns_early(self, stub_coord, stub_entry):
        """Line 1207: return early when no token."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch
        stub_coord.token = None
        sw = BoschAmbientLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw._set_ambient_light(True)  # must not raise
        assert sw._is_on is None


# ── BoschSoftLightFadingSwitch ────────────────────────────────────────────────

class TestSoftLightFadingSwitch:
    def test_is_on_reads_global_cache(self, stub_coord, stub_entry):
        """Lines 1253-1255: reads softLightFading from _global_lighting_cache."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord._global_lighting_cache[CAM_ID] = {"softLightFading": True, "darknessThreshold": 0.5}
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_none_when_no_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is None

    def test_available_requires_cache(self, stub_coord, stub_entry):
        """Lines 1258-1263: available only when _global_lighting_cache populated."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord._global_lighting_cache[CAM_ID] = {"softLightFading": False}
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_available_false_without_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_put_global(self, stub_coord, stub_entry):
        """Lines 1293-1294: turn_on delegates to _put_global_lighting(True)."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_on()
        sw._put_global_lighting.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_put_global(self, stub_coord, stub_entry):
        """Lines 1296-1297: turn_off delegates to _put_global_lighting(False)."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_off()
        sw._put_global_lighting.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_put_global_lighting_success_updates_cache(self, stub_coord, stub_entry):
        """Lines 1266-1291: PUT success updates _global_lighting_cache."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord._global_lighting_cache[CAM_ID] = {"darknessThreshold": 0.3, "softLightFading": False}
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.json = AsyncMock(return_value={"darknessThreshold": 0.3, "softLightFading": True})
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw._put_global_lighting(True)
        assert stub_coord._global_lighting_cache[CAM_ID]["softLightFading"] is True
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_global_lighting_no_token_returns_early(self, stub_coord, stub_entry):
        """Line 1267: return early when no token."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord.token = None
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw._put_global_lighting(True)  # must not raise

    @pytest.mark.asyncio
    async def test_put_global_lighting_non_dict_response_uses_body(self, stub_coord, stub_entry):
        """Lines 1285-1288: non-dict JSON response falls back to body dict."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord._global_lighting_cache[CAM_ID] = {"darknessThreshold": 0.5, "softLightFading": False}
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        put_resp = MagicMock()
        put_resp.status = 204
        put_resp.json = AsyncMock(return_value="ok")  # non-dict
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw._put_global_lighting(True)
        # Cache should be updated with the body dict fallback
        assert stub_coord._global_lighting_cache[CAM_ID]["softLightFading"] is True

    @pytest.mark.asyncio
    async def test_put_global_lighting_exception_swallowed(self, stub_coord, stub_entry):
        """Lines 1289-1290: network exception inside try block is swallowed, async_write_ha_state still called."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch
        stub_coord._global_lighting_cache[CAM_ID] = {"darknessThreshold": 0.5, "softLightFading": False}
        sw = BoschSoftLightFadingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        # Raise inside session.put (inside the try block), not before it
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(side_effect=Exception("network error"))
        failing_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=failing_ctx)
        with patch("custom_components.bosch_shc_camera.switch.async_get_clientsession",
                   return_value=mock_session):
            await sw._put_global_lighting(True)
        sw.async_write_ha_state.assert_called_once()


# ── BoschIntrusionDetectionSwitch privacy guard ───────────────────────────────

class TestIntrusionDetectionPrivacyGuard:
    @pytest.mark.asyncio
    async def test_set_intrusion_blocked_by_privacy(self, stub_coord, stub_entry):
        """Lines 1346-1350: _warn_if_privacy_on returns True → PUT not called."""
        from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord._intrusion_config_cache[CAM_ID] = {"enabled": False, "sensitivity": 3}
        sw = BoschIntrusionDetectionSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_intrusion_allowed_when_privacy_off(self, stub_coord, stub_entry):
        """Lines 1351-1357: PUT called when privacy is OFF."""
        from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = False
        stub_coord._intrusion_config_cache[CAM_ID] = {"enabled": False, "sensitivity": 3}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        sw = BoschIntrusionDetectionSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_awaited_once()
        assert stub_coord._intrusion_config_cache[CAM_ID]["enabled"] is True


# ── BoschAudioAlarmSwitch gen2 privacy guard ──────────────────────────────────

class TestAudioAlarmSwitchPrivacyGuard:
    @pytest.mark.asyncio
    async def test_turn_on_blocked_for_gen2_indoor_with_privacy(self, stub_coord, stub_entry):
        """Line 1620: gen2 indoor + privacy ON blocks the PUT."""
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord.audio_alarm_settings = lambda cid: {"enabled": False, "threshold": 50, "sensitivity": "MEDIUM", "audioAlarmConfiguration": "CUSTOM"}
        sw = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_allowed_for_outdoor(self, stub_coord, stub_entry):
        """Outdoor camera not blocked even with privacy ON."""
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord.audio_alarm_settings = lambda cid: {"enabled": False, "threshold": 50, "sensitivity": "MEDIUM", "audioAlarmConfiguration": "CUSTOM"}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        sw = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_turn_on_delegates_to_set(self, stub_coord, stub_entry):
        """Lines 1636-1637: turn_on calls _set(True)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.audio_alarm_settings = lambda cid: {"enabled": False, "threshold": 50, "sensitivity": "MEDIUM", "audioAlarmConfiguration": "CUSTOM"}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        sw = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        # cam_data["audioAlarm"] updated on success
        cam_data = stub_coord.data.get(CAM_ID)
        if cam_data is not None:
            assert cam_data.get("audioAlarm", {}).get("enabled") is True

    @pytest.mark.asyncio
    async def test_turn_off_delegates_to_set(self, stub_coord, stub_entry):
        """Lines 1639-1640: turn_off calls _set(False)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch
        stub_coord.audio_alarm_settings = lambda cid: {"enabled": True, "threshold": 50, "sensitivity": "MEDIUM", "audioAlarmConfiguration": "CUSTOM"}
        stub_coord.async_put_camera = AsyncMock(return_value=True)
        sw = BoschAudioAlarmSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_off()
        cam_data = stub_coord.data.get(CAM_ID)
        if cam_data is not None:
            assert cam_data.get("audioAlarm", {}).get("enabled") is False


# ── BoschNvrRecordingSwitch.async_added_to_hass ───────────────────────────────

class TestNvrRecordingSwitchRestoreState:
    @pytest.mark.asyncio
    async def test_restores_on_state_and_sets_intent(self, stub_coord, stub_entry):
        """Lines 1777-1784: restore ON state sets _nvr_user_intent."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        sw = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "on"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        # Patch super().async_added_to_hass to be a no-op
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        assert stub_coord._nvr_user_intent.get(CAM_ID) is True

    @pytest.mark.asyncio
    async def test_restores_off_state_no_intent(self, stub_coord, stub_entry):
        """Lines 1777-1784: restore OFF state leaves _nvr_user_intent unchanged."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        sw = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "off"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        assert stub_coord._nvr_user_intent.get(CAM_ID) is not True

    @pytest.mark.asyncio
    async def test_no_previous_state_no_intent(self, stub_coord, stub_entry):
        """None from async_get_last_state → no intent set."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        sw = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=None)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        assert stub_coord._nvr_user_intent.get(CAM_ID) is not True

    @pytest.mark.asyncio
    async def test_restores_on_and_kicks_recorder_when_live(self, stub_coord, stub_entry):
        """Lines 1788-1797: when LOCAL stream is already active, kicks off recorder task."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        stub_coord._live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        sw = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "on"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        # async_create_task must have been called to start the recorder
        sw.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_restores_on_no_kick_when_remote(self, stub_coord, stub_entry):
        """Lines 1788-1797: REMOTE stream → recorder NOT kicked off."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        stub_coord._live_connections[CAM_ID] = {"_connection_type": "REMOTE"}
        sw = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "on"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        # Recorder must NOT be kicked for REMOTE sessions
        sw.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_starts_recorder(self, stub_coord, stub_entry):
        """Lines 1799-1802: turn_on calls start_recorder."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        sw = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        stub_coord.start_recorder.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_turn_off_stops_recorder(self, stub_coord, stub_entry):
        """Lines 1804-1807: turn_off calls stop_recorder."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch
        stub_entry.options = {"enable_snapshot_button": True, "enable_nvr": True}
        sw = BoschNvrRecordingSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        stub_coord.stop_recorder.assert_awaited_once_with(CAM_ID)
