"""Cover the remaining 18 uncovered lines in switch.py."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import switch as switch_mod
from custom_components.bosch_shc_camera.switch import (
    BoschAmbientLightSwitch,
    BoschCameraLightSwitch,
    BoschIntrusionDetectionSwitch,
    BoschNotificationsSwitch,
    BoschPrivacyModeSwitch,
    BoschSoftLightFadingSwitch,
    BoschWallwasherSwitch,
    _BoschSwitchBase,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(**overrides):
    """Stub coordinator with all fields needed by switch.py paths."""
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0, "sound": False},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        last_update_success=True,
        is_camera_online=lambda cid: True,
        _shc_state_cache={CAM_ID: {}},
        _global_lighting_cache={
            CAM_ID: {"darknessThreshold": 0.5, "softLightFading": False}
        },
        _privacy_set_at={},
        _light_set_at={},
        _live_connections={},
        _tear_down_live_stream=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
        token="fake-token",
        hass=MagicMock(),
        options={"recording_quality": "high"},
        _intrusion_config={CAM_ID: {}},
        _notifications_pref={CAM_ID: False},
        _audio_enabled={CAM_ID: True},
        _privacy_sound_cache={CAM_ID: False},
        _timestamp_cache={CAM_ID: True},
        _record_sound_cache={CAM_ID: True},
        _auto_follow_cache={CAM_ID: False},
        _motion_enabled_cache={CAM_ID: True},
        _intercom_cache={CAM_ID: False},
        _image_rotation_cache={CAM_ID: False},
        _front_light_cache={CAM_ID: False},
        _wallwasher_cache={CAM_ID: False},
        _status_led_cache={CAM_ID: True},
        _motion_light_cache={CAM_ID: False},
        _ambient_light_cache={CAM_ID: False},
        _soft_light_fading_cache={CAM_ID: False},
        _intrusion_detection_cache={CAM_ID: False},
        _intrusion_config_cache={CAM_ID: {}},
        _alarm_system_cache={CAM_ID: False},
        _alarm_mode_cache={CAM_ID: "STANDARD"},
        _pre_alarm_cache={CAM_ID: False},
        _nvr_recording_cache={CAM_ID: False},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_entry():
    e = MagicMock()
    e.entry_id = "test-entry"
    e.options = {}
    return e


# ── Line 150: DeviceInfo property (mac path) ─────────────────────────────────


def test_device_info_with_mac():
    """Line 150-156: DeviceInfo populated with mac connection."""
    coord = _make_coord()
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    info = sw.device_info
    assert info is not None
    assert (switch_mod.DOMAIN, CAM_ID) in info["identifiers"]
    assert info["manufacturer"] == "Bosch"
    assert info["connections"] == {("mac", "aa:bb:cc:dd:ee:01")}


def test_device_info_without_mac():
    """Line 156: empty set when no mac present."""
    coord = _make_coord()
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    sw._mac = ""
    info = sw.device_info
    assert info["connections"] == set()


# ── Line 511: BoschCameraLightSwitch.is_on cache read ────────────────────────


def test_camera_light_is_on_true():
    coord = _make_coord()
    coord._shc_state_cache[CAM_ID] = {"camera_light": True}
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is True


def test_camera_light_is_on_missing():
    coord = _make_coord()
    coord._shc_state_cache = {}  # no entry at all
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is None


# ── Line 588: BoschWallwasherSwitch.async_turn_off ───────────────────────────


@pytest.mark.asyncio
async def test_wallwasher_turn_off():
    coord = _make_coord()
    sw = BoschWallwasherSwitch(coord, CAM_ID, _make_entry())
    await sw.async_turn_off()
    coord.async_cloud_set_light_component.assert_called_once_with(
        CAM_ID, "wallwasher", False
    )


# ── Line 690: BoschPrivacyModeSwitch.async_turn_off cooldown rejection ───────


@pytest.mark.asyncio
async def test_privacy_turn_off_cooldown_defers():
    coord = _make_coord()
    # Within the cooldown window the toggle is DEFERRED + coalesced (not raised,
    # not applied immediately) so an automation's later steps keep running and
    # the card's optimistic flip stays correct (#27). Applied once it clears.
    sw = BoschPrivacyModeSwitch(coord, CAM_ID, _make_entry())
    sw.hass = MagicMock()
    sw.async_write_ha_state = MagicMock()
    with patch.object(sw, "_privacy_block_remaining", return_value=3.0):
        await sw.async_turn_off()  # must NOT raise
    coord.async_cloud_set_privacy_mode.assert_not_called()
    assert sw._pending_privacy is False
    sw.hass.async_create_task.assert_called_once()
    sw.hass.async_create_task.call_args.args[0].close()


# ── Line 721: BoschNotificationsSwitch.is_on returns None when status missing ─


def test_notifications_is_on_none_when_status_none():
    coord = _make_coord()
    coord._shc_state_cache[CAM_ID] = {"notifications_status": None}
    sw = BoschNotificationsSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is None


def test_notifications_is_on_true_for_follow():
    coord = _make_coord()
    coord._shc_state_cache[CAM_ID] = {"notifications_status": "FOLLOW_CAMERA_SCHEDULE"}
    sw = BoschNotificationsSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is True


# ── Lines 1216, 1223-1224: BoschAmbientLightSwitch error paths ───────────────


def _make_response_cm(status, json_data=None):
    """Build an async context-manager that returns a mock response."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_ambient_light_get_non_200_early_return():
    """Line 1215-1216: non-200 from GET ambient → early return."""
    coord = _make_coord()
    session = MagicMock()
    session.get = MagicMock(return_value=_make_response_cm(500))
    sw = BoschAmbientLightSwitch(coord, CAM_ID, _make_entry())
    sw.hass = MagicMock()
    sw.async_write_ha_state = MagicMock()

    with patch(f"{switch_mod.__name__}.async_get_clientsession", return_value=session):
        await sw._set_ambient_light(True)
    # PUT must not have been called (returned early after GET 500)
    assert session.put.call_count == 0 if hasattr(session, "put") else True


@pytest.mark.asyncio
async def test_ambient_light_exception_caught():
    """Lines 1223-1224: any exception in ambient PUT/GET is logged + swallowed."""
    coord = _make_coord()
    session = MagicMock()
    session.get = MagicMock(side_effect=RuntimeError("boom"))
    sw = BoschAmbientLightSwitch(coord, CAM_ID, _make_entry())
    sw.hass = MagicMock()
    sw.async_write_ha_state = MagicMock()

    with patch(f"{switch_mod.__name__}.async_get_clientsession", return_value=session):
        # Must not raise — exception is swallowed by `except Exception`
        await sw._set_ambient_light(True)
    sw.async_write_ha_state.assert_called_once()


# ── Lines 1287-1288: SoftLightFading json() exception → fall back to body ────


@pytest.mark.asyncio
async def test_softlight_fading_json_exception_falls_back_to_body():
    coord = _make_coord()
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(side_effect=ValueError("not json"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.put = MagicMock(return_value=cm)

    sw = BoschSoftLightFadingSwitch(coord, CAM_ID, _make_entry())
    sw.hass = MagicMock()
    sw.async_write_ha_state = MagicMock()

    with patch(f"{switch_mod.__name__}.async_get_clientsession", return_value=session):
        await sw._put_global_lighting(True)

    # When json() raised, body fallback must populate cache
    cached = coord._global_lighting_cache[CAM_ID]
    assert cached["softLightFading"] is True
    assert cached["darknessThreshold"] == 0.5


# ── Line 1350: BoschIntrusionDetectionSwitch empty config early return ───────


@pytest.mark.asyncio
async def test_intrusion_detection_empty_config_returns():
    """Line 1350: empty _config dict → early return without async_put_camera."""
    coord = _make_coord()
    # _intrusion_config_cache returns {} → _config property is {} → `if not cfg: return`
    coord._intrusion_config_cache[CAM_ID] = {}
    sw = BoschIntrusionDetectionSwitch(coord, CAM_ID, _make_entry())
    sw.async_write_ha_state = MagicMock()
    with patch(
        f"{switch_mod.__name__}._warn_if_privacy_on", new=AsyncMock(return_value=False)
    ):
        await sw._set_intrusion(True)
    coord.async_put_camera.assert_not_called()


# ── Lines 195, 219, 222-225: async_setup_entry entity creation gates ─────────


@pytest.mark.asyncio
async def test_setup_entry_auto_follow_for_pan_camera():
    """Line 195: BoschAutoFollowSwitch added when panLimit > 0 (CAMERA_360)."""
    coord = _make_coord()
    coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"
    coord.data[CAM_ID]["info"]["featureSupport"] = {
        "light": False,
        "panLimit": 360,
        "sound": False,
    }
    entry = _make_entry()
    entry.runtime_data = coord
    added = []

    def async_add(ents, **kw):
        return added.extend(ents)

    hass = MagicMock()
    await switch_mod.async_setup_entry(hass, entry, async_add)
    classes = {type(e).__name__ for e in added}
    assert "BoschAutoFollowSwitch" in classes


@pytest.mark.asyncio
async def test_setup_entry_audio_notification_when_sound_supported():
    """Line 219: BoschNotificationTypeSwitch('audio') added when has_sound=True."""
    coord = _make_coord()
    coord.data[CAM_ID]["info"]["featureSupport"]["sound"] = True
    entry = _make_entry()
    entry.runtime_data = coord
    added = []

    def async_add(ents, **kw):
        return added.extend(ents)

    hass = MagicMock()
    await switch_mod.async_setup_entry(hass, entry, async_add)
    # The audio NotificationType switch has _ntype="audio"
    audio_switches = [
        e
        for e in added
        if type(e).__name__ == "BoschNotificationTypeSwitch"
        and getattr(e, "_ntype", None) == "audio"
    ]
    assert len(audio_switches) == 1


@pytest.mark.asyncio
async def test_setup_entry_gen2_indoor_alarm_switches():
    """Lines 222-226: 3 alarm switches added for Gen2 Indoor cameras.

    BoschAudioAlarmSwitch removed in v13.x — audio alarm feature dropped.
    """
    coord = _make_coord()
    coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
    entry = _make_entry()
    entry.runtime_data = coord
    added = []

    def async_add(ents, **kw):
        return added.extend(ents)

    hass = MagicMock()
    await switch_mod.async_setup_entry(hass, entry, async_add)
    classes = {type(e).__name__ for e in added}
    assert "BoschAlarmSystemArmSwitch" in classes
    assert "BoschAlarmModeSwitch" in classes
    assert "BoschPreAlarmSwitch" in classes
    assert "BoschPanicAlarmSwitch" in classes, (
        "BoschPanicAlarmSwitch added in v12.0.4 for Gen2 siren trigger"
    )
