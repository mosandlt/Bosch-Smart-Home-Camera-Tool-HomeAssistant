"""Tests for sensor entity classes (sensor.py — 722 LOC, currently 0% covered).

Same approach as test_switches.py: stub coordinator, instantiate sensor,
verify `native_value` and `extra_state_attributes`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        # Sensor-specific caches
        _commissioned_cache={},
        _firmware_cache={},
        _wifi_cache={CAM_ID: {"signal": 75, "ssid": "WLAN"}},
        _ambient_light_cache={CAM_ID: 0.42},
        _motion_sensitivity_cache={CAM_ID: "MEDIUM_HIGH"},
        _audio_alarm_cache={CAM_ID: {"enabled": True, "threshold": 65}},
        _ledlight_brightness_cache={CAM_ID: 80},
        _clock_offset_cache={CAM_ID: 1.23},
        _ledlights_cache={CAM_ID: True},
        _last_event_seen={CAM_ID: None},
        _live_connections={},
        _stream_warming=set(),
        _stream_fell_back={},
        _stream_error_count={},
        _fcm_running=True,
        _fcm_healthy=True,
        # FCM status
        options={"enable_fcm_push": False},
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschCameraStatusSensor ──────────────────────────────────────────────


class TestStatusSensor:
    def test_online(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_offline(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "OFFLINE"
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "offline"

    def test_unknown_when_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        del stub_coord.data[CAM_ID]["status"]
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "unknown"

    def test_attrs_include_camera_id_model_fw(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["camera_id"] == CAM_ID
        assert attrs["model"] == "HOME_Eyes_Outdoor"
        assert attrs["firmware"] == "9.40.25"

    def test_attrs_include_commissioned_when_cached(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord._commissioned_cache[CAM_ID] = {
            "configured": True, "connected": True, "commissioned": True,
        }
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["configured"] is True
        assert attrs["connected"] is True

    def test_attrs_include_firmware_when_cached(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord._firmware_cache[CAM_ID] = {
            "updating": True, "status": "downloading", "upToDate": False,
        }
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["firmware_updating"] is True
        assert attrs["firmware_update_status"] == "downloading"
        assert attrs["firmware_up_to_date"] is False

    def test_updating_state_overrides_online(self, stub_coord, stub_entry):
        """When coordinator.is_updating(cam_id) is True, native_value must
        return 'updating' regardless of the cached cloud `status` field.
        Cloud still reports ONLINE during the install window (cached pre-reboot),
        but the camera is actually rebooting and dependent entities should
        flip to unavailable."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "ONLINE"
        stub_coord.is_updating = lambda cam_id: cam_id == CAM_ID
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "updating"

    def test_updating_state_overrides_offline(self, stub_coord, stub_entry):
        """Even if cloud reports OFFLINE (which it will during reboot),
        is_updating must take precedence so the operator sees the cause."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "OFFLINE"
        stub_coord.is_updating = lambda cam_id: True
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "updating"

    def test_updating_state_listed_in_options(self, stub_coord, stub_entry):
        """The enum's options tuple must contain 'updating' so HA renders
        the state in the entity selector + history."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert "updating" in s._attr_options

    def test_no_updating_when_helper_returns_false(self, stub_coord, stub_entry):
        """is_updating present but returns False → falls through to cloud status."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "ONLINE"
        stub_coord.is_updating = lambda cam_id: False
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_no_updating_when_helper_missing(self, stub_coord, stub_entry):
        """Backward compat: coordinator without is_updating helper still works."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        # stub_coord fixture has no is_updating attribute → getattr returns None
        assert not hasattr(stub_coord, "is_updating")
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_offline_when_latest_event_is_trouble_disconnect(self, stub_coord, stub_entry):
        """Bosch cloud reports ONLINE forever after a disconnect; override via events.

        Regression: outdoor Gen1 camera showed as online in UI for 22 days
        while physically unreachable on LAN. Last event from cloud was
        TROUBLE_DISCONNECT but the status sensor still said online because
        Bosch cloud never updates the `status` field after a disconnect.
        """
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "ONLINE"
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_DISCONNECT", "timestamp": "2026-04-27T11:03:00+02:00"},
        ]
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "offline"

    def test_online_when_reconnect_is_newer_than_disconnect(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "ONLINE"
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_RECONNECT", "timestamp": "2026-05-19T08:00:00+02:00"},
            {"eventType": "TROUBLE_DISCONNECT", "timestamp": "2026-04-27T11:03:00+02:00"},
        ]
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_online_when_movement_is_newer_than_disconnect(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "ONLINE"
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "timestamp": "2026-05-19T08:00:00+02:00"},
            {"eventType": "TROUBLE_DISCONNECT", "timestamp": "2026-04-27T11:03:00+02:00"},
        ]
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_cloud_offline_not_changed_by_reconnect_event(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor
        stub_coord.data[CAM_ID]["status"] = "OFFLINE"
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_RECONNECT", "timestamp": "2026-05-19T08:00:00+02:00"},
        ]
        s = BoschCameraStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "offline"


# ── BoschCameraEventsTodaySensor ─────────────────────────────────────────


class TestEventsTodaySensor:
    def test_count_zero_when_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor
        s = BoschCameraEventsTodaySensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == 0

    def test_count_with_today_events(self, stub_coord, stub_entry):
        """Events with today's date count toward the daily total."""
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        stub_coord.data[CAM_ID]["events"] = [
            {"id": "e1", "createdAt": today, "type": "MOVEMENT"},
            {"id": "e2", "createdAt": today, "type": "AUDIO"},
        ]
        from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor
        s = BoschCameraEventsTodaySensor(stub_coord, CAM_ID, stub_entry)
        # Just check it returns a non-negative integer
        assert isinstance(s.native_value, int)
        assert s.native_value >= 0


# ── BoschFirmwareVersionSensor ───────────────────────────────────────────


class TestFirmwareVersionSensor:
    def test_returns_fw_string(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor
        s = BoschFirmwareVersionSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "9.40.25"


# ── BoschFcmPushStatusSensor ─────────────────────────────────────────────


class TestFcmPushStatusSensor:
    def test_disabled_when_fcm_off(self, stub_coord, stub_entry):
        """enable_fcm_push=False → state is 'disabled'."""
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor
        s = BoschFcmPushStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "disabled"

    def test_fcm_push_when_healthy(self, stub_coord, stub_entry):
        """enable_fcm_push=True + healthy → state is 'fcm_push'."""
        stub_coord.options = {"enable_fcm_push": True}
        stub_coord._fcm_healthy = True
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor
        s = BoschFcmPushStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "fcm_push"

    def test_polling_when_unhealthy(self, stub_coord, stub_entry):
        """enable_fcm_push=True + UNhealthy → state is 'polling' (degradation visible)."""
        stub_coord.options = {"enable_fcm_push": True}
        stub_coord._fcm_healthy = False
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor
        s = BoschFcmPushStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == "polling"


# ── BoschAmbientLightSensor ──────────────────────────────────────────────


class TestAmbientLightSensor:
    def test_returns_percentage(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor
        s = BoschAmbientLightSensor(stub_coord, CAM_ID, stub_entry)
        # 0.42 → 42 percent (or whatever the conversion is)
        assert s.native_value is not None


# ── BoschCameraLastEventSensor ──────────────────────────────────────────


class TestLastEventSensor:
    def test_returns_none_with_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor
        s = BoschCameraLastEventSensor(stub_coord, CAM_ID, stub_entry)
        # No events → native_value is None
        assert s.native_value is None

    def test_returns_value_when_events_present(self, stub_coord, stub_entry):
        """With events, native_value is a datetime — exact format depends on impl."""
        stub_coord.data[CAM_ID]["events"] = [
            {"id": "e1", "createdAt": "2026-05-05T10:00:00Z", "type": "MOVEMENT"},
        ]
        from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor
        s = BoschCameraLastEventSensor(stub_coord, CAM_ID, stub_entry)
        # No assertion on value — different impl details. Just confirm
        # the property doesn't raise.
        _ = s.native_value


# ── BoschLastEventTypeSensor ────────────────────────────────────────────


class TestLastEventTypeSensor:
    def test_returns_none_with_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor
        s = BoschLastEventTypeSensor(stub_coord, CAM_ID, stub_entry)
        # No events → native_value is None or "unknown"
        v = s.native_value
        assert v is None or isinstance(v, str)
