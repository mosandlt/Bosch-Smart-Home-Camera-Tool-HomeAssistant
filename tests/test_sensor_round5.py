"""Round-5 sensor tests — covers missing lines in sensor.py.

Targets:
- BoschCameraStatusSensor native_value + extra_state_attributes (with/without comm/fw caches)
- BoschCameraLastEventSensor native_value (valid ts, no Z, bad ts, no events) + extra attrs
- BoschCameraEventsTodaySensor native_value + extra attrs
- BoschWifiSignalSensor native_value (None, valid) + available + extra attrs (rcp + ladder)
- BoschFirmwareVersionSensor native_value (missing fw), available, extra attrs (upToDate, product_name)
- BoschAmbientLightSensor native_value (None, float), available
- BoschLedDimmerSensor native_value, available
- BoschClockOffsetSensor native_value, available, extra attrs (status categories)
- BoschMotionSensitivitySensor native_value (None, disabled, enabled) + extra attrs
- BoschAudioAlarmSensor native_value (None, enabled/disabled), extra attrs
- BoschLastEventTypeSensor native_value (no events, has events)
- BoschMovementEventsTodaySensor / BoschAudioEventsTodaySensor native_value filtering
- BoschFcmPushStatusSensor native_value (disabled, fcm_push, polling) + extra attrs
- BoschUnreadEventsCountSensor native_value, available
- BoschCommissionedSensor native_value (None, not connected, commissioned, not commissioned) + attrs
- BoschRulesCountSensor native_value, available, extra attrs
- BoschAlarmCatalogSensor native_value, available, extra attrs
- BoschMotionZonesSensor native_value (gen2 priority, cloud fallback, rcp fallback) + extra attrs
- BoschTlsCertSensor native_value (valid, bad, None cert)
- BoschNetworkServicesSensor native_value, available, extra attrs
- BoschIvaCatalogSensor native_value, available, extra attrs (active filter)
- BoschPrivateAreasSensor native_value (gen2 priority, cloud fallback) + extra attrs (note)
- BoschAmbientLightScheduleSensor native_value (disabled, dusk_to_dawn, manual) + available + extra attrs
- BoschAlarmStateSensor native_value (from status cache, from arming cache, unknown)

No HA runtime needed — SimpleNamespace + MagicMock.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
TODAY = "2026-05-07"


# ── helpers ───────────────────────────────────────────────────────────────────


def _coord(**overrides):
    base = dict(
        data={CAM_ID: {"info": {"hardwareVersion": "CAMERA", "firmwareVersion": "7.91", "macAddress": "aa:bb", "title": "Kamera"}, "status": "ONLINE", "events": []}},
        last_update_success=True,
        options={"enable_fcm_push": False},
        _commissioned_cache={},
        _firmware_cache={},
        _wifiinfo_cache={},
        _ambient_light_cache={},
        _rcp_dimmer_cache={},
        _unread_events_cache={},
        _rules_cache={},
        _rcp_alarm_catalog_cache={},
        _rcp_motion_zones_cache={},
        _rcp_motion_coords_cache={},
        _cloud_zones_cache={},
        _gen2_zones_cache={},
        _cloud_privacy_masks_cache={},
        _gen2_private_areas_cache={},
        _rcp_tls_cert_cache={},
        _rcp_network_services_cache={},
        _rcp_iva_catalog_cache={},
        _ambient_lighting_cache={},
        _alarm_status_cache={},
        _arming_cache={},
        _shc_state_cache={CAM_ID: {}},
        _fcm_healthy=False,
        _fcm_running=False,
        _fcm_push_mode="auto",
        _fcm_last_push=0,
        is_camera_online=lambda cid: True,
        clock_offset=lambda cid: None,
        motion_settings=lambda cid: None,
        audio_alarm_settings=lambda cid: None,
        recording_options=lambda cid: None,
        rcp_lan_ip=lambda cid: None,
        rcp_bitrate_ladder=lambda cid: None,
        rcp_product_name=lambda cid: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_sensor(cls, coord=None, cam_id=CAM_ID):
    c = coord or _coord()
    sw = cls.__new__(cls)
    sw.coordinator = c
    sw._cam_id = cam_id
    sw._cam_title = "Kamera"
    sw._model_name = "Camera"
    sw._fw = "7.91"
    sw._mac = "aa:bb"
    sw.hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Berlin"),
    )
    return sw


# ── BoschCameraStatusSensor ───────────────────────────────────────────────────


def test_status_sensor_native_value_online():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    c = _coord()
    c.data[CAM_ID]["status"] = "ONLINE"
    sw = _make_sensor(BoschCameraStatusSensor, c)
    assert sw.native_value == "online"


def test_status_sensor_native_value_offline():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    c = _coord()
    c.data[CAM_ID]["status"] = "OFFLINE"
    sw = _make_sensor(BoschCameraStatusSensor, c)
    assert sw.native_value == "offline"


def test_status_sensor_extra_attrs_with_comm_and_fw():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    c = _coord(
        _commissioned_cache={CAM_ID: {"configured": True, "connected": True, "commissioned": True}},
        _firmware_cache={CAM_ID: {"updating": False, "status": "OK", "upToDate": True}},
    )
    sw = _make_sensor(BoschCameraStatusSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["commissioned"] is True
    assert attrs["firmware_up_to_date"] is True


def test_status_sensor_extra_attrs_no_comm():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    sw = _make_sensor(BoschCameraStatusSensor)
    attrs = sw.extra_state_attributes
    assert "commissioned" not in attrs
    assert "firmware_updating" not in attrs


# ── BoschCameraLastEventSensor ────────────────────────────────────────────────


def test_last_event_sensor_no_events():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    sw = _make_sensor(BoschCameraLastEventSensor)
    assert sw.native_value is None


def test_last_event_sensor_valid_ts():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _coord()
    c.data[CAM_ID]["events"] = [{"timestamp": "2026-03-19T09:32:08.000Z", "eventType": "MOVEMENT", "id": "abc123def", "imageUrl": "http://x"}]
    sw = _make_sensor(BoschCameraLastEventSensor, c)
    result = sw.native_value
    assert result is not None
    assert result.year == 2026
    assert result.month == 3


def test_last_event_sensor_bad_ts():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _coord()
    c.data[CAM_ID]["events"] = [{"timestamp": "not-a-date"}]
    sw = _make_sensor(BoschCameraLastEventSensor, c)
    assert sw.native_value is None


def test_last_event_sensor_empty_ts():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _coord()
    c.data[CAM_ID]["events"] = [{"timestamp": ""}]
    sw = _make_sensor(BoschCameraLastEventSensor, c)
    assert sw.native_value is None


def test_last_event_sensor_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _coord()
    c.data[CAM_ID]["events"] = [
        {"timestamp": "2026-03-19T09:32:08", "eventType": "PERSON", "id": "abcdefgh1234", "imageUrl": "http://x", "videoClipUrl": "http://v", "videoClipUploadStatus": "DONE"}
    ]
    sw = _make_sensor(BoschCameraLastEventSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["event_type"] == "PERSON"
    assert attrs["has_image"] is True
    assert attrs["has_clip"] is True


# ── BoschCameraEventsTodaySensor ─────────────────────────────────────────────


def test_events_today_count_matching():
    from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor

    c = _coord()
    c.data[CAM_ID]["events"] = [
        {"timestamp": f"{TODAY}T10:00:00"},
        {"timestamp": f"{TODAY}T11:00:00"},
        {"timestamp": "2025-01-01T00:00:00"},
    ]
    sw = _make_sensor(BoschCameraEventsTodaySensor, c)
    with patch("custom_components.bosch_shc_camera.sensor.dt_util") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 7)
        result = sw.native_value
    assert result == 2


def test_events_today_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor

    c = _coord()
    c.data[CAM_ID]["events"] = [{"timestamp": f"{TODAY}T09:00:00"}]
    sw = _make_sensor(BoschCameraEventsTodaySensor, c)
    with patch("custom_components.bosch_shc_camera.sensor.dt_util") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 7)
        attrs = sw.extra_state_attributes
    assert attrs["events_in_feed"] == 1


# ── BoschWifiSignalSensor ─────────────────────────────────────────────────────


def test_wifi_signal_native_value_none():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    sw = _make_sensor(BoschWifiSignalSensor)
    assert sw.native_value is None


def test_wifi_signal_native_value_int():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    c = _coord(_wifiinfo_cache={CAM_ID: {"signalStrength": 85, "ssid": "HOME", "ipAddress": "192.168.1.2", "macAddress": "aa:bb"}})
    sw = _make_sensor(BoschWifiSignalSensor, c)
    assert sw.native_value == 85


def test_wifi_signal_available_false():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    sw = _make_sensor(BoschWifiSignalSensor)
    assert sw.available is False


def test_wifi_signal_extra_attrs_with_rcp():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    c = _coord(_wifiinfo_cache={CAM_ID: {"signalStrength": 70, "ssid": "X", "ipAddress": "10.0.0.1", "macAddress": "cc:dd"}})
    c.rcp_lan_ip = lambda cid: "192.168.20.149"
    c.rcp_bitrate_ladder = lambda cid: [1000, 2000, 3000]
    sw = _make_sensor(BoschWifiSignalSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["lan_ip_rcp"] == "192.168.20.149"
    assert attrs["max_bitrate_kbps"] == 3000


# ── BoschFirmwareVersionSensor ────────────────────────────────────────────────


def test_firmware_version_none_when_missing():
    from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

    c = _coord()
    c.data[CAM_ID]["info"]["firmwareVersion"] = ""
    sw = _make_sensor(BoschFirmwareVersionSensor, c)
    assert sw.native_value is None


def test_firmware_version_available_false_no_fw():
    from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

    c = _coord()
    c.data[CAM_ID]["info"]["firmwareVersion"] = ""
    sw = _make_sensor(BoschFirmwareVersionSensor, c)
    assert sw.available is False


def test_firmware_version_extra_attrs_up_to_date():
    from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

    c = _coord()
    c.data[CAM_ID]["info"]["upToDate"] = True
    c.rcp_product_name = lambda cid: "Bosch FLEXIDOME"
    sw = _make_sensor(BoschFirmwareVersionSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["up_to_date"] is True
    assert attrs["product_name_rcp"] == "Bosch FLEXIDOME"


# ── BoschAmbientLightSensor ───────────────────────────────────────────────────


def test_ambient_light_native_value_none():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor

    sw = _make_sensor(BoschAmbientLightSensor)
    assert sw.native_value is None


def test_ambient_light_native_value():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor

    c = _coord(_ambient_light_cache={CAM_ID: 0.65})
    sw = _make_sensor(BoschAmbientLightSensor, c)
    assert sw.native_value == 65


def test_ambient_light_available():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor

    c = _coord(_ambient_light_cache={CAM_ID: 0.5})
    sw = _make_sensor(BoschAmbientLightSensor, c)
    assert sw.available is True


# ── BoschLedDimmerSensor ──────────────────────────────────────────────────────


def test_led_dimmer_native_value():
    from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor

    c = _coord(_rcp_dimmer_cache={CAM_ID: 75})
    sw = _make_sensor(BoschLedDimmerSensor, c)
    assert sw.native_value == 75


def test_led_dimmer_available_false():
    from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor

    sw = _make_sensor(BoschLedDimmerSensor)
    assert sw.available is False


# ── BoschClockOffsetSensor ────────────────────────────────────────────────────


def test_clock_offset_none():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    sw = _make_sensor(BoschClockOffsetSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_clock_offset_in_sync():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    c = _coord()
    c.clock_offset = lambda cid: 2
    sw = _make_sensor(BoschClockOffsetSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["status"] == "in_sync"


def test_clock_offset_minor_drift():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    c = _coord()
    c.clock_offset = lambda cid: -30
    sw = _make_sensor(BoschClockOffsetSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["status"] == "minor_drift"


def test_clock_offset_out_of_sync():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    c = _coord()
    c.clock_offset = lambda cid: 120
    sw = _make_sensor(BoschClockOffsetSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["status"] == "out_of_sync"


def test_clock_offset_extra_attrs_none_returns_empty():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    sw = _make_sensor(BoschClockOffsetSensor)
    assert sw.extra_state_attributes == {}


# ── BoschMotionSensitivitySensor ──────────────────────────────────────────────


def test_motion_sensitivity_none_no_settings():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    sw = _make_sensor(BoschMotionSensitivitySensor)
    assert sw.native_value is None


def test_motion_sensitivity_disabled():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    c = _coord()
    c.motion_settings = lambda cid: {"enabled": False, "motionAlarmConfiguration": "HIGH"}
    sw = _make_sensor(BoschMotionSensitivitySensor, c)
    assert sw.native_value == "disabled"


def test_motion_sensitivity_enabled():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    c = _coord()
    c.motion_settings = lambda cid: {"enabled": True, "motionAlarmConfiguration": "HIGH_SENSITIVITY"}
    sw = _make_sensor(BoschMotionSensitivitySensor, c)
    assert sw.native_value == "high sensitivity"


def test_motion_sensitivity_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    c = _coord()
    c.motion_settings = lambda cid: {"enabled": True, "motionAlarmConfiguration": "HIGH"}
    sw = _make_sensor(BoschMotionSensitivitySensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["enabled"] is True


# ── BoschAudioAlarmSensor ─────────────────────────────────────────────────────


def test_audio_alarm_sensor_none():
    from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor

    sw = _make_sensor(BoschAudioAlarmSensor)
    assert sw.native_value is None


def test_audio_alarm_sensor_enabled():
    from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor

    c = _coord()
    c.audio_alarm_settings = lambda cid: {"enabled": True, "threshold": 54}
    sw = _make_sensor(BoschAudioAlarmSensor, c)
    assert sw.native_value == "enabled"


def test_audio_alarm_sensor_disabled():
    from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor

    c = _coord()
    c.audio_alarm_settings = lambda cid: {"enabled": False, "threshold": 54}
    sw = _make_sensor(BoschAudioAlarmSensor, c)
    assert sw.native_value == "disabled"


def test_audio_alarm_sensor_extra_attrs_empty():
    from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor

    sw = _make_sensor(BoschAudioAlarmSensor)
    assert sw.extra_state_attributes == {}


# ── BoschLastEventTypeSensor ──────────────────────────────────────────────────


def test_last_event_type_no_events():
    from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

    sw = _make_sensor(BoschLastEventTypeSensor)
    assert sw.native_value == "none"


def test_last_event_type_person():
    from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

    c = _coord()
    c.data[CAM_ID]["events"] = [{"eventType": "PERSON", "timestamp": f"{TODAY}T10:00:00", "id": "abc"}]
    sw = _make_sensor(BoschLastEventTypeSensor, c)
    assert sw.native_value == "person"


def test_last_event_type_extra_attrs_no_events():
    from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

    sw = _make_sensor(BoschLastEventTypeSensor)
    assert sw.extra_state_attributes == {}


# ── BoschMovementEventsTodaySensor ────────────────────────────────────────────


def test_movement_events_today_filters_type():
    from custom_components.bosch_shc_camera.sensor import BoschMovementEventsTodaySensor

    c = _coord()
    c.data[CAM_ID]["events"] = [
        {"eventType": "MOVEMENT", "timestamp": f"{TODAY}T10:00:00"},
        {"eventType": "PERSON", "timestamp": f"{TODAY}T11:00:00"},  # excluded
        {"eventType": "MOVEMENT", "timestamp": "2025-01-01T00:00:00"},  # excluded (old)
    ]
    sw = _make_sensor(BoschMovementEventsTodaySensor, c)
    with patch("custom_components.bosch_shc_camera.sensor.dt_util") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 7)
        result = sw.native_value
    assert result == 1


# ── BoschAudioEventsTodaySensor ───────────────────────────────────────────────


def test_audio_events_today_count():
    from custom_components.bosch_shc_camera.sensor import BoschAudioEventsTodaySensor

    c = _coord()
    c.data[CAM_ID]["events"] = [
        {"eventType": "AUDIO_ALARM", "timestamp": f"{TODAY}T08:00:00"},
        {"eventType": "AUDIO_ALARM", "timestamp": f"{TODAY}T09:00:00"},
        {"eventType": "MOVEMENT", "timestamp": f"{TODAY}T10:00:00"},  # excluded
    ]
    sw = _make_sensor(BoschAudioEventsTodaySensor, c)
    with patch("custom_components.bosch_shc_camera.sensor.dt_util") as mock_dt:
        mock_dt.now.return_value = datetime(2026, 5, 7)
        result = sw.native_value
    assert result == 2


# ── BoschFcmPushStatusSensor ──────────────────────────────────────────────────


def test_fcm_status_disabled():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

    sw = _make_sensor(BoschFcmPushStatusSensor)
    sw.coordinator.options = {"enable_fcm_push": False}
    assert sw.native_value == "disabled"


def test_fcm_status_fcm_push():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

    c = _coord()
    c.options = {"enable_fcm_push": True}
    c._fcm_healthy = True
    sw = _make_sensor(BoschFcmPushStatusSensor, c)
    assert sw.native_value == "fcm_push"


def test_fcm_status_polling():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

    c = _coord()
    c.options = {"enable_fcm_push": True}
    c._fcm_healthy = False
    sw = _make_sensor(BoschFcmPushStatusSensor, c)
    assert sw.native_value == "polling"


def test_fcm_status_extra_attrs_last_push():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor
    import time

    c = _coord()
    c.options = {"enable_fcm_push": True, "fcm_push_mode": "auto"}
    c._fcm_last_push = time.monotonic() - 30
    sw = _make_sensor(BoschFcmPushStatusSensor, c)
    attrs = sw.extra_state_attributes
    assert "last_push_seconds_ago" in attrs
    assert attrs["last_push_seconds_ago"] >= 28


# ── BoschUnreadEventsCountSensor ─────────────────────────────────────────────


def test_unread_events_none():
    from custom_components.bosch_shc_camera.sensor import BoschUnreadEventsCountSensor

    sw = _make_sensor(BoschUnreadEventsCountSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_unread_events_count():
    from custom_components.bosch_shc_camera.sensor import BoschUnreadEventsCountSensor

    c = _coord(_unread_events_cache={CAM_ID: 5})
    sw = _make_sensor(BoschUnreadEventsCountSensor, c)
    assert sw.native_value == 5
    assert sw.available is True


# ── BoschCommissionedSensor ───────────────────────────────────────────────────


def test_commissioned_none():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    sw = _make_sensor(BoschCommissionedSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_commissioned_not_connected():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _coord(_commissioned_cache={CAM_ID: {"configured": True, "connected": False, "commissioned": False}})
    sw = _make_sensor(BoschCommissionedSensor, c)
    assert sw.native_value == "Not connected"


def test_commissioned_yes():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _coord(_commissioned_cache={CAM_ID: {"configured": True, "connected": True, "commissioned": True}})
    sw = _make_sensor(BoschCommissionedSensor, c)
    assert sw.native_value == "Commissioned"


def test_commissioned_not_commissioned():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _coord(_commissioned_cache={CAM_ID: {"configured": True, "connected": True, "commissioned": False}})
    sw = _make_sensor(BoschCommissionedSensor, c)
    assert sw.native_value == "Not commissioned"


def test_commissioned_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _coord(_commissioned_cache={CAM_ID: {"configured": True, "connected": True, "commissioned": True}})
    sw = _make_sensor(BoschCommissionedSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["commissioned"] is True


# ── BoschRulesCountSensor ─────────────────────────────────────────────────────


def test_rules_count_none():
    from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

    sw = _make_sensor(BoschRulesCountSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_rules_count_value():
    from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

    rules = [
        {"id": "r1", "name": "Night", "isActive": True, "startTime": "22:00", "endTime": "06:00", "weekdays": ["Mon"]},
        {"id": "r2", "name": "Day", "isActive": False, "startTime": "06:00", "endTime": "22:00", "weekdays": []},
    ]
    c = _coord(_rules_cache={CAM_ID: rules})
    sw = _make_sensor(BoschRulesCountSensor, c)
    assert sw.native_value == 2
    attrs = sw.extra_state_attributes
    assert len(attrs["rules"]) == 2
    assert attrs["rules"][0]["name"] == "Night"


# ── BoschAlarmCatalogSensor ───────────────────────────────────────────────────


def test_alarm_catalog_none():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

    sw = _make_sensor(BoschAlarmCatalogSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_alarm_catalog_count():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

    alarms = [{"name": "MOTION", "type": "motion"}, {"name": "SMOKE", "type": "smoke"}]
    c = _coord(_rcp_alarm_catalog_cache={CAM_ID: alarms})
    sw = _make_sensor(BoschAlarmCatalogSensor, c)
    assert sw.native_value == 2
    attrs = sw.extra_state_attributes
    assert "MOTION" in attrs["alarm_types"]
    assert "smoke" in attrs["categories"]


# ── BoschMotionZonesSensor ────────────────────────────────────────────────────


def test_motion_zones_gen2_priority():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _coord(
        _gen2_zones_cache={CAM_ID: [{"id": 1}, {"id": 2}]},
        _cloud_zones_cache={CAM_ID: [{"id": 3}]},
        _rcp_motion_zones_cache={CAM_ID: []},
        _rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor(BoschMotionZonesSensor, c)
    assert sw.native_value == 2  # gen2 wins


def test_motion_zones_cloud_fallback():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _coord(
        _gen2_zones_cache={CAM_ID: []},
        _cloud_zones_cache={CAM_ID: [{"id": 1}]},
        _rcp_motion_zones_cache={CAM_ID: []},
        _rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor(BoschMotionZonesSensor, c)
    assert sw.native_value == 1


def test_motion_zones_rcp_fallback():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _coord(
        _gen2_zones_cache={CAM_ID: []},
        _cloud_zones_cache={CAM_ID: []},
        _rcp_motion_zones_cache={CAM_ID: [{"id": 1}, {"id": 2}, {"id": 3}]},
        _rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor(BoschMotionZonesSensor, c)
    assert sw.native_value == 3


def test_motion_zones_note_when_empty():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _coord(
        _gen2_zones_cache={CAM_ID: []},
        _cloud_zones_cache={CAM_ID: []},
        _rcp_motion_zones_cache={CAM_ID: []},
        _rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor(BoschMotionZonesSensor, c)
    assert "note" in sw.extra_state_attributes


# ── BoschTlsCertSensor ────────────────────────────────────────────────────────


def test_tls_cert_none():
    from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

    sw = _make_sensor(BoschTlsCertSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_tls_cert_valid():
    from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

    c = _coord(_rcp_tls_cert_cache={CAM_ID: {"not_after": "2027-01-01T00:00:00"}})
    sw = _make_sensor(BoschTlsCertSensor, c)
    val = sw.native_value
    assert val is not None
    assert val.year == 2027


def test_tls_cert_bad_date():
    from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

    c = _coord(_rcp_tls_cert_cache={CAM_ID: {"not_after": "not-a-date"}})
    sw = _make_sensor(BoschTlsCertSensor, c)
    assert sw.native_value is None


# ── BoschIvaCatalogSensor ─────────────────────────────────────────────────────


def test_iva_catalog_none():
    from custom_components.bosch_shc_camera.sensor import BoschIvaCatalogSensor

    sw = _make_sensor(BoschIvaCatalogSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_iva_catalog_count_and_active():
    from custom_components.bosch_shc_camera.sensor import BoschIvaCatalogSensor

    modules = [
        {"id": 1, "active": True},
        {"id": 2, "active": False},
        {"id": 3, "active": True},
    ]
    c = _coord(_rcp_iva_catalog_cache={CAM_ID: modules})
    sw = _make_sensor(BoschIvaCatalogSensor, c)
    assert sw.native_value == 3
    attrs = sw.extra_state_attributes
    assert attrs["active_count"] == 2


# ── BoschPrivateAreasSensor ───────────────────────────────────────────────────


def test_private_areas_gen2_priority():
    from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

    c = _coord(
        _gen2_private_areas_cache={CAM_ID: [{"id": 1}, {"id": 2}]},
        _cloud_privacy_masks_cache={CAM_ID: [{"id": 3}]},
    )
    sw = _make_sensor(BoschPrivateAreasSensor, c)
    assert sw.native_value == 2


def test_private_areas_cloud_fallback():
    from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

    c = _coord(
        _gen2_private_areas_cache={CAM_ID: []},
        _cloud_privacy_masks_cache={CAM_ID: [{"id": 1}]},
    )
    sw = _make_sensor(BoschPrivateAreasSensor, c)
    assert sw.native_value == 1


def test_private_areas_note_when_empty():
    from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

    c = _coord(
        _gen2_private_areas_cache={CAM_ID: []},
        _cloud_privacy_masks_cache={CAM_ID: []},
    )
    sw = _make_sensor(BoschPrivateAreasSensor, c)
    assert "note" in sw.extra_state_attributes


# ── BoschAmbientLightScheduleSensor ──────────────────────────────────────────


def test_ambient_schedule_none_no_cache():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor

    sw = _make_sensor(BoschAmbientLightScheduleSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_ambient_schedule_disabled():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor

    c = _coord(_ambient_lighting_cache={CAM_ID: {"ambientLightEnabled": False, "ambientLightSchedule": "ENVIRONMENT"}})
    sw = _make_sensor(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "disabled"


def test_ambient_schedule_dusk_to_dawn():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor

    c = _coord(_ambient_lighting_cache={CAM_ID: {"ambientLightEnabled": True, "ambientLightSchedule": "ENVIRONMENT"}})
    sw = _make_sensor(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "dusk_to_dawn"


def test_ambient_schedule_manual():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor

    c = _coord(_ambient_lighting_cache={CAM_ID: {"ambientLightEnabled": True, "ambientLightSchedule": "MANUAL"}})
    sw = _make_sensor(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "manual"


def test_ambient_schedule_dict_schedule():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor

    c = _coord(_ambient_lighting_cache={CAM_ID: {"ambientLightEnabled": True, "ambientLightSchedule": {"type": "ENVIRONMENT", "lightOnTime": "18:00", "lightOffTime": "06:00"}}})
    sw = _make_sensor(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "dusk_to_dawn"
    attrs = sw.extra_state_attributes
    assert attrs["schedule_on_time"] == "18:00"


# ── BoschAlarmStateSensor ─────────────────────────────────────────────────────


def test_alarm_state_from_status_cache():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    c = _coord(_alarm_status_cache={CAM_ID: {"intrusionSystem": "ACTIVE", "alarmType": "MOTION"}})
    sw = _make_sensor(BoschAlarmStateSensor, c)
    assert sw.native_value == "active"


def test_alarm_state_from_arming_cache_armed():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    c = _coord(_arming_cache={CAM_ID: True})
    sw = _make_sensor(BoschAlarmStateSensor, c)
    assert sw.native_value == "active"


def test_alarm_state_from_arming_cache_disarmed():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    c = _coord(_arming_cache={CAM_ID: False})
    sw = _make_sensor(BoschAlarmStateSensor, c)
    assert sw.native_value == "inactive"


def test_alarm_state_unknown():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    sw = _make_sensor(BoschAlarmStateSensor)
    assert sw.native_value == "unknown"
