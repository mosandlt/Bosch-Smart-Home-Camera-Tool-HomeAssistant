"""Sensor edge-branch + property coverage (Bucket B).

Pins the remaining `sensor.py` lines that no other test file covers:

  - L130     : `_BoschSensorBase.device_info` return path.
  - L336     : FirmwareVersionSensor.extra_state_attributes — featureSupport
                fallback when top-level `upToDate` is None.
  - L464     : BoschMotionSensitivitySensor.name property.
  - L468     : BoschMotionSensitivitySensor.unique_id property.
  - L484     : BoschMotionSensitivitySensor.extra_state_attributes empty-dict
                fallback when motion_settings() returns falsy.
  - L502     : BoschAudioAlarmSensor.name property.
  - L506     : BoschAudioAlarmSensor.unique_id property.
  - L540     : BoschLastEventTypeSensor.name property.
  - L544     : BoschLastEventTypeSensor.unique_id property.
  - L559-560 : BoschLastEventTypeSensor.extra_state_attributes — happy-path
                dict assembly when events present.
  - L579     : BoschMovementEventsTodaySensor.name property.
  - L583     : BoschMovementEventsTodaySensor.unique_id property.
  - L608     : BoschAudioEventsTodaySensor.name property.
  - L612     : BoschAudioEventsTodaySensor.unique_id property.
  - L643     : BoschFcmPushStatusSensor.name property.
  - L647     : BoschFcmPushStatusSensor.unique_id property.
  - L745     : BoschCommissionedSensor.extra_state_attributes empty-dict
                fallback when commissioned_cache returns None.
  - L885     : BoschMotionZonesSensor.native_unit_of_measurement.
  - L983     : BoschNetworkServicesSensor.native_unit_of_measurement.
  - L1023    : BoschIvaCatalogSensor.native_unit_of_measurement.
  - L1074    : BoschPrivateAreasSensor.native_unit_of_measurement.
  - L1142    : BoschAmbientLightScheduleSensor.extra_state_attributes empty
                cache → {}.
  - L1147    : Ambient schedule attributes — `schedule_str = schedule` (non-dict
                branch when schedule is a plain string).
  - L1161, L1163 : Manual schedule start/end times branch.
  - L1168-1175 : Per-light-group attribute expansion (frontLightSettings,
                topLedLightSettings, bottomLedLightSettings).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

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
                    "macAddress": "aa:bb:cc:33:14:ae",
                    "featureSupport": {"upToDate": True},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        # Sensor caches
        _commissioned_cache={},
        _firmware_cache={},
        _wifi_cache={},
        _ambient_light_cache={},
        _motion_sensitivity_cache={},
        _audio_alarm_cache={},
        _ledlight_brightness_cache={},
        _clock_offset_cache={},
        _ledlights_cache={},
        _last_event_seen={},
        _live_connections={},
        _stream_warming=set(),
        _stream_fell_back={},
        _stream_error_count={},
        _ambient_lighting_cache={},
        _rcp_dimmer_cache={},
        _unread_events_cache={},
        _rules_cache={},
        _rcp_alarm_catalog_cache={},
        _rcp_motion_zones_cache={},
        _rcp_motion_coords_cache={},
        _cloud_zones_cache={},
        _gen2_zones_cache={},
        _rcp_tls_cert_cache={},
        _rcp_network_services_cache={},
        _rcp_iva_catalog_cache={},
        _cloud_privacy_masks_cache={},
        _gen2_private_areas_cache={},
        last_update_success=True,
        token="tok",
        options={"enable_fcm_push": False},
        _fcm_running=False,
        _fcm_healthy=False,
        # Coordinator helpers used by sensors
        rcp_product_name=lambda cid: None,
        motion_settings=lambda cid: {},
        audio_alarm_settings=lambda cid: {},
        clock_offset=lambda cid: None,
        # FCM monotonic sentinel — use float('-inf') per SENTINEL_RULE
        _fcm_last_push=float('-inf'),
        _fcm_push_mode="auto",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord():
    return _stub_coord()


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── L130 — _BoschSensorBase.device_info ────────────────────────────────────


class TestSensorBaseDeviceInfo:
    """Every sensor exposes `device_info` so HA groups them under the camera
    device. Pin the return-dict shape — at least one concrete subclass."""

    def test_device_info_contains_model_and_fw(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        info = s.device_info
        assert isinstance(info, dict)
        assert info["manufacturer"] == "Bosch"
        assert info["sw_version"] == "9.40.25"
        # MAC populated → non-empty connections
        assert info["connections"]


# ── L336 — Firmware sensor featureSupport fallback ─────────────────────────


class TestFirmwareVersionSensorUpToDateFallback:
    """If `info["upToDate"]` is missing, fall through to
    `info["featureSupport"]["upToDate"]` (line 335-336)."""

    def test_uptodate_read_from_feature_support_when_top_level_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor
        # Top-level upToDate absent — only featureSupport carries it
        info = stub_coord.data[CAM_ID]["info"]
        info.pop("upToDate", None)
        info["featureSupport"] = {"upToDate": False}
        s = BoschFirmwareVersionSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["up_to_date"] is False


# ── L464 / L468 — MotionSensitivity name + unique_id ───────────────────────


class TestMotionSensitivityNameAndUid:
    def test_name(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor
        s = BoschMotionSensitivitySensor(stub_coord, CAM_ID, stub_entry)
        assert "Terrasse" in s.name
        assert "Motion Sensitivity" in s.name

    def test_unique_id(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor
        s = BoschMotionSensitivitySensor(stub_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_motion_sensitivity"


# ── L484 — MotionSensitivity extra_state_attributes empty-settings ─────────


class TestMotionSensitivityEmptyAttributes:
    """When motion_settings() returns falsy, extra_state_attributes must
    return an empty dict (not raise KeyError)."""

    def test_empty_settings_returns_empty_dict(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor
        stub_coord.motion_settings = lambda cid: {}
        s = BoschMotionSensitivitySensor(stub_coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes == {}


# ── L502 / L506 — AudioAlarm name + unique_id ──────────────────────────────


class TestAudioAlarmNameAndUid:
    def test_name(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor
        s = BoschAudioAlarmSensor(stub_coord, CAM_ID, stub_entry)
        assert "Audio Alarm" in s.name

    def test_unique_id(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor
        s = BoschAudioAlarmSensor(stub_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_audio_alarm"


# ── L540 / L544 / L559-560 — LastEventTypeSensor ───────────────────────────


class TestLastEventTypeSensor:
    def test_name(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor
        s = BoschLastEventTypeSensor(stub_coord, CAM_ID, stub_entry)
        assert "Last Event Type" in s.name

    def test_unique_id(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor
        s = BoschLastEventTypeSensor(stub_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_last_event_type"

    def test_extra_attrs_with_events(self, stub_coord, stub_entry):
        """When events present, attrs dict carries event_type/timestamp/id."""
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "PERSON", "timestamp": "2026-05-10T10:00:00Z", "id": "EVT123"}
        ]
        s = BoschLastEventTypeSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["event_type"] == "PERSON"
        assert attrs["timestamp"] == "2026-05-10T10:00:00Z"
        assert attrs["event_id"] == "EVT123"


# ── L579 / L583 — MovementEventsToday name + unique_id ─────────────────────


class TestMovementEventsTodayNameAndUid:
    def test_name(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMovementEventsTodaySensor
        s = BoschMovementEventsTodaySensor(stub_coord, CAM_ID, stub_entry)
        assert "Movement Events Today" in s.name

    def test_unique_id(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMovementEventsTodaySensor
        s = BoschMovementEventsTodaySensor(stub_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_movement_events_today"


# ── L608 / L612 — AudioEventsToday name + unique_id ────────────────────────


class TestAudioEventsTodayNameAndUid:
    def test_name(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioEventsTodaySensor
        s = BoschAudioEventsTodaySensor(stub_coord, CAM_ID, stub_entry)
        assert "Audio Events Today" in s.name

    def test_unique_id(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioEventsTodaySensor
        s = BoschAudioEventsTodaySensor(stub_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_audio_events_today"


# ── L643 / L647 — FcmPushStatus name + unique_id ───────────────────────────


class TestFcmPushStatusNameAndUid:
    def test_name(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor
        s = BoschFcmPushStatusSensor(stub_coord, CAM_ID, stub_entry)
        # FCM sensor is global (not per-cam), name reflects that
        assert "FCM Push Status" in s.name

    def test_unique_id(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor
        s = BoschFcmPushStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.unique_id == "bosch_shc_camera_fcm_push_status"


# ── L745 — CommissionedSensor empty-cache attributes ───────────────────────


class TestCommissionedSensorEmptyCache:
    """When the slow-tier cache hasn't filled, extra_state_attributes must
    return `{}` instead of crashing on None.get()."""

    def test_empty_cache_returns_empty_dict(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor
        # Cache empty (default)
        s = BoschCommissionedSensor(stub_coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes == {}


# ── L885 / L983 / L1023 / L1074 — native_unit_of_measurement properties ────


class TestNativeUnitProperties:
    """The unit strings are property methods rather than class attrs (they
    need to override even when EntityCategory.DIAGNOSTIC suppresses defaults)."""

    def test_motion_zones_unit(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor
        s = BoschMotionZonesSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "zones"

    def test_network_services_unit(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor
        s = BoschNetworkServicesSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "services"

    def test_iva_catalog_unit(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschIvaCatalogSensor
        s = BoschIvaCatalogSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "modules"

    def test_private_areas_unit(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor
        s = BoschPrivateAreasSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "masks"


# ── L1142 / L1147 / L1161 / L1163 / L1168-1175 — AmbientLightSchedule ──────


class TestAmbientLightScheduleAttributes:
    """`extra_state_attributes` has many branches covering schedule shapes
    (dict vs string), manual start/end, per-light-group expansion."""

    def test_empty_cache_returns_empty_dict(self, stub_coord, stub_entry):
        """Line 1142: `if not cache: return {}`."""
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        # cache empty
        s = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes == {}

    def test_string_schedule_takes_else_branch(self, stub_coord, stub_entry):
        """Line 1147: `schedule_str = schedule` when schedule isn't a dict."""
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "ENVIRONMENT",  # plain string, not dict
        }
        s = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["enabled"] is True
        assert attrs["schedule_type"] == "ENVIRONMENT"

    def test_manual_start_end_time_attrs(self, stub_coord, stub_entry):
        """Lines 1161, 1163: manual_start_time / manual_end_time both set."""
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "MANUAL",
            "ambientLightManualStartTime": "20:00",
            "ambientLightManualEndTime": "06:30",
        }
        s = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["manual_start_time"] == "20:00"
        assert attrs["manual_end_time"] == "06:30"

    def test_per_light_group_brightness_color_wb_expansion(self, stub_coord, stub_entry):
        """Lines 1168-1175: each lighting group's brightness/whiteBalance/color
        gets a prefixed attribute key."""
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "ENVIRONMENT",
            "frontLightSettings":  {"brightness": 80, "whiteBalance": 0.3, "color": None},
            "topLedLightSettings": {"brightness": 50, "whiteBalance": None, "color": "#FF0080"},
            "bottomLedLightSettings": {"brightness": 0,  "whiteBalance": -1.0, "color": None},
        }
        s = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        # Front light: brightness + whiteBalance (color is None → skipped)
        assert attrs["front_light_brightness"] == 80
        assert attrs["front_light_white_balance"] == 0.3
        assert "front_light_color" not in attrs
        # Top LED: brightness + color (whiteBalance is None → skipped)
        assert attrs["top_led_light_brightness"] == 50
        assert attrs["top_led_light_color"] == "#FF0080"
        assert "top_led_light_white_balance" not in attrs
        # Bottom LED: brightness + whiteBalance
        assert attrs["bottom_led_light_brightness"] == 0
        assert attrs["bottom_led_light_white_balance"] == -1.0
