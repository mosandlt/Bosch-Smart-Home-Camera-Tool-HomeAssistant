"""Switch entity turn_on / turn_off coverage round.

`test_switches.py` already covers `is_on` / `available` / attributes.
This file covers the symmetric `async_turn_on` / `async_turn_off`
flows: each must call the right coordinator method (or PUT endpoint)
with the right payload. Tests use AsyncMock so the actual cloud call
is never made — focus is on contract pinning, not network behavior.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def stub_coord():
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                },
                "status": "ONLINE",
                "events": [],
                "autofollow": {"result": False},
                "recordingOptions": {"recordSound": False},
            }
        },
        _live_connections={},
        _shc_state_cache={
            CAM_ID: {
                "privacy_mode": False,
                "camera_light": False,
                "front_light": None,
                "wallwasher": None,
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
        _timestamp_cache={CAM_ID: True},
        _ledlights_cache={CAM_ID: True},
        _arming_cache={},
        _rcp_privacy_cache={},
        _audio_alarm_cache={CAM_ID: {}},
        last_update_success=True,
        options={"audio_default_on": True},
        token="token-AAA",
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
        # Coordinator side-effect mocks
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_notifications=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        _tear_down_live_stream=AsyncMock(),
        motion_settings=lambda cid: {"enabled": False, "motionAlarmConfiguration": "MEDIUM"},
        recording_options=lambda cid: {"recordSound": False},
        audio_alarm_settings=lambda cid: {},
    )
    return coord


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={"bearer_token": "x"}, options={})


@pytest.fixture(autouse=True)
def _patch_async_create_task():
    """Many switch turn_on/off methods call self.hass.async_create_task —
    swallow it everywhere with a MagicMock to avoid event-loop ceremony."""
    yield


def _bind_hass(switch):
    """Switches sometimes call self.hass.async_create_task in turn_on —
    attach a sync MagicMock so the call is observable but no-op."""
    switch.hass = SimpleNamespace(
        async_create_task=MagicMock(),
    )
    switch.async_write_ha_state = MagicMock()


# ── Camera Light Switch ─────────────────────────────────────────────────


class TestCameraLightSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_calls_cloud_setter_with_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch
        sw = BoschCameraLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_cloud_set_camera_light.assert_awaited_once_with(CAM_ID, True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_setter_with_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch
        sw = BoschCameraLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord.async_cloud_set_camera_light.assert_awaited_once_with(CAM_ID, False)


# ── Front Light + Wallwasher (component-specific cloud setter) ──────────


class TestLightComponentSwitches:
    @pytest.mark.asyncio
    async def test_front_light_on_uses_front_component(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch
        sw = BoschFrontLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "front", True,
        )

    @pytest.mark.asyncio
    async def test_front_light_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch
        sw = BoschFrontLightSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "front", False,
        )

    @pytest.mark.asyncio
    async def test_wallwasher_uses_wallwasher_component(self, stub_coord, stub_entry):
        """Pin the literal 'wallwasher' string — the cloud handler
        switches on this exact key."""
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch
        sw = BoschWallwasherSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "wallwasher", True,
        )


# ── Privacy Mode (cooldown gate + stream teardown) ──────────────────────


class TestPrivacyModeSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_during_warmup_blocked(self, stub_coord, stub_entry):
        """Cooldown blocks privacy toggle during stream warm-up — the
        TLS proxy + encoder init isn't a moment to flip the shutter."""
        stub_coord.is_stream_warming = lambda cid: True
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        # The cloud setter must NOT be called when blocked
        stub_coord.async_cloud_set_privacy_mode.assert_not_awaited()
        stub_coord._tear_down_live_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_on_within_cooldown_blocked(self, stub_coord, stub_entry):
        """Two flips within 10 s must block — protects the camera firmware
        from rapid shutter toggling (red LED / reboot risk)."""
        stub_coord._privacy_set_at[CAM_ID] = time.monotonic() - 5  # 5 s ago
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_cloud_set_privacy_mode.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_on_tears_down_active_stream(self, stub_coord, stub_entry):
        """If a live stream is active when privacy turns on, must
        teardown — otherwise stream_worker auto-restart loops against
        a dead camera (issue #6)."""
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord._tear_down_live_stream.assert_awaited_once_with(CAM_ID)
        stub_coord.async_cloud_set_privacy_mode.assert_awaited_once_with(CAM_ID, True)

    @pytest.mark.asyncio
    async def test_turn_on_no_active_stream_skips_teardown(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord._tear_down_live_stream.assert_not_awaited()
        stub_coord.async_cloud_set_privacy_mode.assert_awaited_once_with(CAM_ID, True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_setter_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord.async_cloud_set_privacy_mode.assert_awaited_once_with(CAM_ID, False)


# ── Notifications Switch ────────────────────────────────────────────────


class TestNotificationsSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_cloud_set_notifications.assert_awaited_once_with(CAM_ID, True)

    @pytest.mark.asyncio
    async def test_turn_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord.async_cloud_set_notifications.assert_awaited_once_with(CAM_ID, False)


# ── Motion Detection Switch ─────────────────────────────────────────────


class TestMotionEnabledSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_preserves_sensitivity(self, stub_coord, stub_entry):
        """Motion ON via PUT /motion must preserve the existing sensitivity
        — sending only `enabled` resets the level to API default."""
        stub_coord.motion_settings = lambda cid: {
            "enabled": False, "motionAlarmConfiguration": "SUPER_HIGH",
        }
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch
        sw = BoschMotionEnabledSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord.async_put_camera.assert_awaited_once()
        args = stub_coord.async_put_camera.call_args.args
        assert args[0] == CAM_ID
        assert args[1] == "motion"
        assert args[2] == {"enabled": True, "motionAlarmConfiguration": "SUPER_HIGH"}

    @pytest.mark.asyncio
    async def test_turn_off_preserves_sensitivity(self, stub_coord, stub_entry):
        stub_coord.motion_settings = lambda cid: {
            "enabled": True, "motionAlarmConfiguration": "MEDIUM_LOW",
        }
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch
        sw = BoschMotionEnabledSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        args = stub_coord.async_put_camera.call_args.args
        assert args[2] == {"enabled": False, "motionAlarmConfiguration": "MEDIUM_LOW"}

    @pytest.mark.asyncio
    async def test_turn_on_default_sensitivity_when_unset(self, stub_coord, stub_entry):
        """If no settings exist yet (first boot), default sensitivity is HIGH."""
        stub_coord.motion_settings = lambda cid: {}
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch
        sw = BoschMotionEnabledSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        args = stub_coord.async_put_camera.call_args.args
        assert args[2] == {"enabled": True, "motionAlarmConfiguration": "HIGH"}


# ── Record Sound Switch ─────────────────────────────────────────────────


class TestRecordSoundSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sends_record_sound_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch
        sw = BoschRecordSoundSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        args = stub_coord.async_put_camera.call_args.args
        assert args[0] == CAM_ID
        assert args[1] == "recording_options"
        assert args[2] == {"recordSound": True}

    @pytest.mark.asyncio
    async def test_turn_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch
        sw = BoschRecordSoundSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        args = stub_coord.async_put_camera.call_args.args
        assert args[2] == {"recordSound": False}


# ── Auto Follow Switch ──────────────────────────────────────────────────


class TestAutoFollowSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sends_result_true(self, stub_coord, stub_entry):
        """Auto-follow API uses the unusual {"result": bool} payload —
        not {"enabled": bool}. Pin so the body schema doesn't drift."""
        from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch
        sw = BoschAutoFollowSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        args = stub_coord.async_put_camera.call_args.args
        assert args[1] == "autofollow"
        assert args[2] == {"result": True}, (
            "Auto-follow API expects {'result': bool}, NOT {'enabled': bool}. "
            "Drift to 'enabled' produces a silent 200 + no-op on Bosch's side."
        )

    @pytest.mark.asyncio
    async def test_turn_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch
        sw = BoschAutoFollowSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        args = stub_coord.async_put_camera.call_args.args
        assert args[2] == {"result": False}


# ── Audio Switch (in-memory only — no API) ──────────────────────────────


class TestAudioSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sets_in_memory_flag(self, stub_coord, stub_entry):
        """Audio switch is purely client-side — toggles `_audio_enabled[cam_id]`.
        Stream-side audio is controlled via the URL `enableaudio=` param at
        the next stream_source() call."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_on()
        assert stub_coord._audio_enabled[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_in_memory_flag(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._audio_enabled[CAM_ID] = True
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        _bind_hass(sw)
        await sw.async_turn_off()
        assert stub_coord._audio_enabled[CAM_ID] is False
