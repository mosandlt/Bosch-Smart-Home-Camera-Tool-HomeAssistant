"""Regression tests for B07 sensor.py bug-hunt fixes (2026-06-15).

Covers:
  BUG-1: trouble_connect present in last_event_type options
  BUG-2: BoschTlsCertSensor returns tz-aware datetime
  BUG-3: BoschCameraEventsTodaySensor uses UTC date bucketing (datetime.now(UTC))
  BUG-4: BoschCommissionedSensor uses snake_case ENUM options
  BUG-5: BoschCloudFeatureFlagsSensor truncates state at 255 chars
  BUG-6: BoschOnvifScopesSensor is ENUM with option "supported"

PIN_EVERY_MODE: one test per mode + default + edge per class.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _stub_coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        _commissioned_cache={},
        _firmware_cache={},
        _wifiinfo_cache={},
        _ambient_light_cache={},
        _rcp_dimmer_cache={},
        _rcp_alarm_catalog_cache={},
        _rcp_motion_zones_cache={},
        _rcp_motion_coords_cache={},
        _cloud_zones_cache={},
        _gen2_zones_cache={},
        _rcp_tls_cert_cache={},
        _rcp_network_services_cache={},
        _rcp_iva_catalog_cache={},
        _rcp_private_areas_cache={},
        _gen2_private_areas_cache={},
        _cloud_privacy_masks_cache={},
        _ambient_lighting_cache={},
        _alarm_status_cache={},
        _alarm_settings_cache={},
        _arming_cache={},
        _live_connections={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_warming=set(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_push_mode="auto",
        _fcm_last_push=float("-inf"),
        _maintenance_cache=None,
        _maintenance_last_fetch=float("-inf"),
        _nvr_drain_state={},
        _nvr_error_state={},
        _nvr_processes={},
        _nvr_user_intent={},
        _nvr_preroll_segment_counts={},
        _nvr_preroll_processes={},
        _unread_events_cache={},
        _rules_cache={},
        _feature_flags={},
        _rcp_onvif_scopes_cache={},
        _rcp_version_cache={},
        _external_stream_enabled={},
        last_update_success=True,
        options={"enable_fcm_push": True, "enable_sensors": True, "enable_nvr": False},
        motion_settings=lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        },
        is_camera_online=lambda cid: True,
        is_stream_warming=lambda cid: False,
        clock_offset=lambda cid: None,
        rcp_lan_ip=lambda cid: None,
        rcp_bitrate_ladder=lambda cid: None,
        rcp_product_name=lambda cid: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="ENTRY01", data={}, options={})


# ── BUG-1: trouble_connect in _attr_options ─────────────────────────────────


class TestLastEventTypeOptions:
    """BUG-1: _attr_options must include trouble_connect."""

    def test_trouble_connect_in_options(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord()
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert "trouble_connect" in s._attr_options, (
            "trouble_connect must be in _attr_options so HA ENUM validation passes"
        )

    def test_trouble_connect_returned_as_value(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord()
        coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_CONNECT", "timestamp": "2026-06-15T10:00:00.000Z"}
        ]
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "trouble_connect"

    def test_trouble_disconnect_still_works(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord()
        coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_DISCONNECT", "timestamp": "2026-06-15T10:00:00.000Z"}
        ]
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "trouble_disconnect"

    def test_trouble_reconnect_still_works(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord()
        coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_RECONNECT", "timestamp": "2026-06-15T10:00:00.000Z"}
        ]
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "trouble_reconnect"

    def test_unknown_event_type_maps_to_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord()
        coord.data[CAM_ID]["events"] = [
            {
                "eventType": "UNKNOWN_FUTURE_TYPE",
                "timestamp": "2026-06-15T10:00:00.000Z",
            }
        ]
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "none"

    def test_no_events_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord()
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "none"


# ── BUG-2: BoschTlsCertSensor tz-aware datetime ─────────────────────────────


class TestTlsCertSensor:
    """BUG-2: native_value must return a tz-aware datetime."""

    def test_naive_iso_string_gets_utc_attached(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord(
            _rcp_tls_cert_cache={CAM_ID: {"not_after": "2027-06-15T12:00:00"}}
        )
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val is not None
        assert val.tzinfo is not None, "naive datetime must not be returned"
        assert val.tzinfo == UTC

    def test_utc_z_suffix_remains_tz_aware(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord(
            _rcp_tls_cert_cache={CAM_ID: {"not_after": "2027-06-15T12:00:00+00:00"}}
        )
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val is not None
        assert val.tzinfo is not None

    def test_missing_cert_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord(_rcp_tls_cert_cache={})
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None

    def test_malformed_date_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord(_rcp_tls_cert_cache={CAM_ID: {"not_after": "not-a-date"}})
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None


# ── BUG-3: EventsTodaySensor uses UTC date bucketing ─────────────────────────


class TestEventsTodaySensor:
    """BUG-3: today sensors bucket events by the event's LOCAL calendar date.

    Bosch timestamps carry an explicit offset; production code parses it and
    buckets by the local date of the true instant (see time_utils / issue #34).
    These tests use ``Z`` timestamps which, under the default UTC test
    timezone, fall on the same local date — so basic counting still holds.
    Boundary-crossing behavior is pinned in
    test_event_timestamp_offset.TestTodayBucketsLocalDate.
    """

    def test_events_today_uses_utc_date(self) -> None:
        """Event whose instant falls on today's local date is counted.

        The clock is frozen so the fixture date and the sensor's local-date
        bucketing always agree: the default test timezone is US/Pacific, so a
        real "UTC today" timestamp can land on the previous *local* day during
        the UTC-morning boundary window — which made this test flaky by
        time-of-day. Freeze now + force as_local→UTC so it is deterministic.
        """
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraEventsTodaySensor,
        )

        coord = _stub_coord()
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        day = fixed_now.strftime("%Y-%m-%d")
        coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "timestamp": f"{day}T10:00:00.000Z"},
        ]
        s = BoschCameraEventsTodaySensor(coord, CAM_ID, _stub_entry())
        with (
            patch(
                "custom_components.bosch_shc_camera.sensor.dt_util.now",
                return_value=fixed_now,
            ),
            patch(
                "custom_components.bosch_shc_camera.sensor.dt_util.as_local",
                side_effect=lambda dt: dt.astimezone(UTC),
            ),
        ):
            assert s.native_value == 1

    def test_events_today_zero_when_no_matching_day(self) -> None:
        """Event with a past UTC date (2000-01-01) must yield 0."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraEventsTodaySensor,
        )

        coord = _stub_coord()
        coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "timestamp": "2000-01-01T23:30:00.000Z"},
        ]
        s = BoschCameraEventsTodaySensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == 0

    def test_events_today_extra_attrs_consistent_day(self) -> None:
        """extra_state_attributes lists all events from today's local date.

        Clock frozen for determinism — see test_events_today_uses_utc_date.
        """
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraEventsTodaySensor,
        )

        coord = _stub_coord()
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        day = fixed_now.strftime("%Y-%m-%d")
        coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "timestamp": f"{day}T10:00:00.000Z"},
            {"eventType": "MOVEMENT", "timestamp": f"{day}T09:00:00.000Z"},
        ]
        s = BoschCameraEventsTodaySensor(coord, CAM_ID, _stub_entry())
        with (
            patch(
                "custom_components.bosch_shc_camera.sensor.dt_util.now",
                return_value=fixed_now,
            ),
            patch(
                "custom_components.bosch_shc_camera.sensor.dt_util.as_local",
                side_effect=lambda dt: dt.astimezone(UTC),
            ),
        ):
            attrs = s.extra_state_attributes
        assert attrs["events_in_feed"] == 2
        assert len(attrs["latest_timestamps"]) == 2


# ── BUG-4: BoschCommissionedSensor snake_case ENUM ───────────────────────────


class TestCommissionedSensor:
    """BUG-4: ENUM options and native_value must use snake_case."""

    def test_options_are_snake_case(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord()
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s._attr_options == ["commissioned", "not_commissioned", "not_connected"]

    def test_commissioned_true(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord(
            _commissioned_cache={
                CAM_ID: {"configured": True, "connected": True, "commissioned": True}
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "commissioned"

    def test_not_commissioned(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord(
            _commissioned_cache={
                CAM_ID: {"configured": True, "connected": True, "commissioned": False}
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "not_commissioned"

    def test_not_connected(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord(
            _commissioned_cache={
                CAM_ID: {"configured": False, "connected": False, "commissioned": False}
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "not_connected"

    def test_no_cache_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord(_commissioned_cache={})
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None

    def test_all_options_in_attr_options_are_valid_enum_values(self) -> None:
        """Every value returned by native_value must be in _attr_options."""
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord()
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        options = set(s._attr_options)
        for data, expected in [
            (
                {"configured": True, "connected": True, "commissioned": True},
                "commissioned",
            ),
            (
                {"configured": True, "connected": True, "commissioned": False},
                "not_commissioned",
            ),
            (
                {"configured": False, "connected": False, "commissioned": False},
                "not_connected",
            ),
        ]:
            coord._commissioned_cache[CAM_ID] = data
            val = s.native_value
            assert val in options, f"{val!r} not in _attr_options"
            assert val == expected


# ── BUG-5: BoschCloudFeatureFlagsSensor 255-char truncation ──────────────────


class TestCloudFeatureFlagsSensor:
    """BUG-5: native_value must not exceed 255 chars."""

    def test_truncates_at_255_chars(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        # Build a flags dict whose joined string exceeds 255 chars
        many_flags = {f"feature_flag_{i:04d}": True for i in range(50)}
        coord = _stub_coord(_feature_flags=many_flags)
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val is not None
        assert len(val) <= 255

    def test_short_flags_not_truncated(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        coord = _stub_coord(_feature_flags={"alpha": True, "beta": False})
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "alpha"

    def test_no_enabled_flags_returns_none_string(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        coord = _stub_coord(_feature_flags={"alpha": False, "beta": False})
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "none"

    def test_empty_flags_dict_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        coord = _stub_coord(_feature_flags={})
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None


# ── BUG-6: BoschOnvifScopesSensor ENUM ─────────────────────────────────────


class TestOnvifScopesSensor:
    """BUG-6: sensor must be ENUM with option 'supported', not a free-text string."""

    def test_device_class_is_enum(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass

        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord()
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        assert s._attr_device_class == SensorDeviceClass.ENUM

    def test_options_contain_supported(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord()
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        assert "supported" in s._attr_options

    def test_native_value_is_supported_when_scopes_present(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord(
            _rcp_onvif_scopes_cache={
                CAM_ID: {
                    "name": "Terrasse",
                    "hardware": "HOME_Eyes_Outdoor",
                    "profiles": ["S"],
                }
            }
        )
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "supported"

    def test_native_value_is_none_when_no_scopes(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord(_rcp_onvif_scopes_cache={})
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None

    def test_value_is_in_options(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord(_rcp_onvif_scopes_cache={CAM_ID: {"profiles": ["S", "T"]}})
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val in s._attr_options
