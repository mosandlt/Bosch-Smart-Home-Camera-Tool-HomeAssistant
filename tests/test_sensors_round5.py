"""Tests for sensor.py classes not yet covered by test_sensors.py (Round 5).

`test_sensors.py` covers 7 of the ~25 sensor classes. This file adds
the remaining ones — all property-only entities that read from
coordinator caches and dicts. Each gets:

  - native_value with data
  - native_value with missing data
  - extra_state_attributes (where present)
  - available (where the property is non-trivial)

Coverage targets:
  - BoschWifiSignalSensor / BoschLedDimmerSensor / BoschClockOffsetSensor
  - BoschMotionSensitivitySensor / BoschAudioAlarmSensor
  - BoschMovementEventsTodaySensor / BoschAudioEventsTodaySensor
  - BoschUnreadEventsCountSensor / BoschCommissionedSensor
  - BoschRulesCountSensor / BoschAlarmCatalogSensor / BoschTlsCertSensor
  - BoschNetworkServicesSensor / BoschIvaCatalogSensor
  - BoschPrivateAreasSensor / BoschMotionZonesSensor
  - BoschAmbientLightScheduleSensor
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _stub_coord(**overrides):
    """Comprehensive coordinator stub for sensor tests."""
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:00:00:00:00:01",
                },
                "status": "ONLINE",
                "events": [],
            },
        },
        # Caches
        _wifiinfo_cache={},
        _rcp_dimmer_cache={},
        _ambient_light_cache={},
        _rcp_clock_offset_cache={},
        _commissioned_cache={},
        _rules_cache={},
        _unread_events_cache={},
        _audio_alarm_cache={},
        _rcp_alarm_catalog_cache={},
        _rcp_tls_cert_cache={},
        _rcp_network_services_cache={},
        _rcp_iva_catalog_cache={},
        _rcp_private_areas_cache={},
        _ambient_schedule_cache={},
        # Coord helpers
        last_update_success=True,
        motion_settings=lambda cid: {},
        audio_alarm_settings=lambda cid: {},
        clock_offset=lambda cid: None,
        rcp_lan_ip=lambda cid: None,
        rcp_bitrate_ladder=lambda cid: [],
        rcp_product_name=lambda cid: None,
        options={},
        _fcm_running=False,
        _fcm_healthy=False,
        _fcm_push_mode="auto",
        _fcm_last_push=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord():
    return _stub_coord()


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschWifiSignalSensor ────────────────────────────────────────────────


class TestWifiSignalSensor:
    def test_native_value_from_cache(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor
        coord = _stub_coord(_wifiinfo_cache={CAM_ID: {"signalStrength": 75}})
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 75

    def test_native_value_none_when_no_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor
        s = BoschWifiSignalSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_native_value_none_when_field_missing(self, stub_entry):
        """Cache entry exists but `signalStrength` field missing → None,
        not crash. Defensive against partial cache writes."""
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor
        coord = _stub_coord(_wifiinfo_cache={CAM_ID: {"ssid": "wlan"}})
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_available_requires_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor
        s = BoschWifiSignalSensor(stub_coord, CAM_ID, stub_entry)
        assert s.available is False
        coord = _stub_coord(_wifiinfo_cache={CAM_ID: {"signalStrength": 50}})
        s2 = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s2.available is True

    def test_extra_state_includes_ssid_ip_mac(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor
        coord = _stub_coord(_wifiinfo_cache={CAM_ID: {
            "signalStrength": 80, "ssid": "MYWLAN",
            "ipAddress": "10.0.0.5", "macAddress": "aa:bb:cc:dd:ee:ff",
        }})
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["ssid"] == "MYWLAN"
        assert attrs["ip_address"] == "10.0.0.5"
        assert attrs["mac_address"] == "aa:bb:cc:dd:ee:ff"

    def test_extra_state_adds_lan_ip_rcp_when_known(self, stub_entry):
        """When the coordinator's RCP LAN-IP cache has an entry, surface
        it for dashboards that display both."""
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor
        coord = _stub_coord(_wifiinfo_cache={CAM_ID: {"signalStrength": 50}})
        coord.rcp_lan_ip = lambda cid: "10.0.0.7"
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["lan_ip_rcp"] == "10.0.0.7"

    def test_extra_state_adds_bitrate_ladder(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor
        coord = _stub_coord(_wifiinfo_cache={CAM_ID: {"signalStrength": 50}})
        coord.rcp_bitrate_ladder = lambda cid: [1500, 2500, 4000]
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["bitrate_ladder_kbps"] == [1500, 2500, 4000]
        assert attrs["max_bitrate_kbps"] == 4000


# ── BoschLedDimmerSensor ────────────────────────────────────────────────


class TestLedDimmerSensor:
    def test_native_value_from_cache(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor
        coord = _stub_coord(_rcp_dimmer_cache={CAM_ID: 60})
        s = BoschLedDimmerSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 60

    def test_native_value_none_when_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor
        s = BoschLedDimmerSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_available_follows_cache(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor
        s = BoschLedDimmerSensor(_stub_coord(), CAM_ID, stub_entry)
        assert s.available is False
        coord = _stub_coord(_rcp_dimmer_cache={CAM_ID: 30})
        s2 = BoschLedDimmerSensor(coord, CAM_ID, stub_entry)
        assert s2.available is True


# ── BoschClockOffsetSensor ──────────────────────────────────────────────


class TestClockOffsetSensor:
    def test_in_sync_status(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor
        coord = _stub_coord()
        coord.clock_offset = lambda cid: 2.5  # < 5s → in_sync
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["status"] == "in_sync"
        assert attrs["offset_seconds"] == 2.5

    def test_minor_drift_status(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor
        coord = _stub_coord()
        coord.clock_offset = lambda cid: 30.0  # 5-60s → minor_drift
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["status"] == "minor_drift"

    def test_out_of_sync_status(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor
        coord = _stub_coord()
        coord.clock_offset = lambda cid: 120.0  # >= 60s → out_of_sync
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["status"] == "out_of_sync"

    def test_negative_offset_uses_abs(self, stub_entry):
        """Camera ahead of HA by 30s also counts as minor_drift, not as
        in_sync. Pin so a refactor of abs() can't silently break the
        reverse-skew detection."""
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor
        coord = _stub_coord()
        coord.clock_offset = lambda cid: -30.0
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["status"] == "minor_drift"

    def test_no_offset_returns_empty_attrs(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor
        s = BoschClockOffsetSensor(stub_coord, CAM_ID, stub_entry)
        # clock_offset returns None default
        assert s.extra_state_attributes == {}

    def test_available_requires_offset(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor
        s = BoschClockOffsetSensor(stub_coord, CAM_ID, stub_entry)
        assert s.available is False
        coord = _stub_coord()
        coord.clock_offset = lambda cid: 1.0
        s2 = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s2.available is True


# ── BoschMotionSensitivitySensor ────────────────────────────────────────


class TestMotionSensitivitySensor:
    def test_disabled_when_motion_off(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor
        coord = _stub_coord()
        coord.motion_settings = lambda cid: {"enabled": False, "motionAlarmConfiguration": "HIGH"}
        s = BoschMotionSensitivitySensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "disabled"

    def test_enabled_returns_lowercased_sensitivity(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor
        coord = _stub_coord()
        coord.motion_settings = lambda cid: {
            "enabled": True, "motionAlarmConfiguration": "MEDIUM_HIGH",
        }
        s = BoschMotionSensitivitySensor(coord, CAM_ID, stub_entry)
        # MEDIUM_HIGH → "medium high" (underscore → space, lowercase)
        assert s.native_value == "medium high"

    def test_no_settings_returns_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor
        s = BoschMotionSensitivitySensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_extra_state_passes_through_raw_settings(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor
        coord = _stub_coord()
        coord.motion_settings = lambda cid: {
            "enabled": True, "motionAlarmConfiguration": "HIGH",
        }
        s = BoschMotionSensitivitySensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["enabled"] is True
        assert attrs["sensitivity"] == "HIGH"


# ── BoschAudioAlarmSensor ───────────────────────────────────────────────


class TestAudioAlarmSensor:
    def test_enabled(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor
        coord = _stub_coord()
        coord.audio_alarm_settings = lambda cid: {"enabled": True, "threshold": 70}
        s = BoschAudioAlarmSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "enabled"

    def test_disabled(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor
        coord = _stub_coord()
        coord.audio_alarm_settings = lambda cid: {"enabled": False}
        s = BoschAudioAlarmSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "disabled"

    def test_no_settings_returns_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor
        s = BoschAudioAlarmSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_extra_state_passes_through_threshold(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAudioAlarmSensor
        coord = _stub_coord()
        coord.audio_alarm_settings = lambda cid: {"enabled": True, "threshold": 65}
        s = BoschAudioAlarmSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["threshold"] == 65


# ── BoschMovementEventsTodaySensor / BoschAudioEventsTodaySensor ────────


class TestEventsTodaySensors:
    def _coord_with_events(self, events: list[dict]):
        coord = _stub_coord()
        coord.data[CAM_ID]["events"] = events
        return coord

    def test_movement_today_counts_only_today_movement(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )
        from homeassistant.util import dt as dt_util
        today = dt_util.now().strftime("%Y-%m-%d")
        events = [
            {"eventType": "MOVEMENT", "timestamp": f"{today}T10:00:00"},
            {"eventType": "MOVEMENT", "timestamp": f"{today}T11:00:00"},
            {"eventType": "AUDIO_ALARM", "timestamp": f"{today}T12:00:00"},  # wrong type
            {"eventType": "MOVEMENT", "timestamp": "2020-01-01T00:00:00"},  # wrong date
        ]
        coord = self._coord_with_events(events)
        s = BoschMovementEventsTodaySensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 2

    def test_movement_today_zero_when_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )
        s = BoschMovementEventsTodaySensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value == 0

    def test_audio_today_counts_only_today_audio(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAudioEventsTodaySensor,
        )
        from homeassistant.util import dt as dt_util
        today = dt_util.now().strftime("%Y-%m-%d")
        events = [
            {"eventType": "AUDIO_ALARM", "timestamp": f"{today}T05:00:00"},
            {"eventType": "MOVEMENT", "timestamp": f"{today}T05:00:00"},  # wrong type
        ]
        coord = self._coord_with_events(events)
        s = BoschAudioEventsTodaySensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 1

    def test_handles_missing_timestamp(self, stub_entry):
        """Some Bosch responses come back without timestamp during a
        cloud hiccup — must not crash the count, just exclude that event."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )
        events = [{"eventType": "MOVEMENT"}]  # no timestamp
        coord = self._coord_with_events(events)
        s = BoschMovementEventsTodaySensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 0


# ── BoschUnreadEventsCountSensor ────────────────────────────────────────


class TestUnreadEventsCountSensor:
    def test_native_value_from_cache(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import (
            BoschUnreadEventsCountSensor,
        )
        coord = _stub_coord(_unread_events_cache={CAM_ID: 7})
        s = BoschUnreadEventsCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 7

    def test_native_value_none_when_missing(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import (
            BoschUnreadEventsCountSensor,
        )
        s = BoschUnreadEventsCountSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_zero_is_a_valid_value(self, stub_entry):
        """Cache may legitimately hold 0 (all read) — must NOT be
        treated as unavailable. Pin so a `if not value` mistake doesn't
        creep in."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschUnreadEventsCountSensor,
        )
        coord = _stub_coord(_unread_events_cache={CAM_ID: 0})
        s = BoschUnreadEventsCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 0
        assert s.available is True


# ── BoschCommissionedSensor ─────────────────────────────────────────────


class TestCommissionedSensor:
    def test_commissioned_state(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor
        coord = _stub_coord(_commissioned_cache={
            CAM_ID: {"configured": True, "connected": True, "commissioned": True},
        })
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "Commissioned"

    def test_not_commissioned_state(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor
        coord = _stub_coord(_commissioned_cache={
            CAM_ID: {"configured": True, "connected": True, "commissioned": False},
        })
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "Not commissioned"

    def test_not_connected_state(self, stub_entry):
        """`connected=False` overrides commissioning state — camera
        unreachable trumps everything else."""
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor
        coord = _stub_coord(_commissioned_cache={
            CAM_ID: {"configured": True, "connected": False, "commissioned": True},
        })
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "Not connected"

    def test_no_cache_returns_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor
        s = BoschCommissionedSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value is None
        assert s.available is False

    def test_extra_state_passes_through_all_three_fields(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor
        coord = _stub_coord(_commissioned_cache={
            CAM_ID: {"configured": True, "connected": True, "commissioned": False},
        })
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs == {"configured": True, "connected": True, "commissioned": False}


# ── BoschRulesCountSensor ───────────────────────────────────────────────


class TestRulesCountSensor:
    def test_count_from_cache(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor
        coord = _stub_coord(_rules_cache={
            CAM_ID: [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}],
        })
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 3

    def test_zero_when_empty_list(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor
        coord = _stub_coord(_rules_cache={CAM_ID: []})
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 0

    def test_none_when_no_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor
        s = BoschRulesCountSensor(stub_coord, CAM_ID, stub_entry)
        assert s.native_value is None
        assert s.available is False

    def test_extra_state_includes_full_rules(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor
        coord = _stub_coord(_rules_cache={CAM_ID: [{
            "id": "r1", "name": "Night Mode",
            "isActive": True, "startTime": "22:00", "endTime": "06:00",
            "weekdays": [0, 1, 2, 3, 4, 5, 6],
        }]})
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        rules = s.extra_state_attributes["rules"]
        assert len(rules) == 1
        assert rules[0]["id"] == "r1"
        assert rules[0]["name"] == "Night Mode"
        assert rules[0]["active"] is True
        assert rules[0]["start"] == "22:00"
        assert rules[0]["end"] == "06:00"
        assert rules[0]["weekdays"] == [0, 1, 2, 3, 4, 5, 6]

    def test_extra_state_handles_missing_optional_fields(self, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor
        coord = _stub_coord(_rules_cache={CAM_ID: [{}]})  # empty rule dict
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        rules = s.extra_state_attributes["rules"]
        # All fields default to safe values; no KeyError
        assert rules[0]["active"] is False
        assert rules[0]["weekdays"] == []
