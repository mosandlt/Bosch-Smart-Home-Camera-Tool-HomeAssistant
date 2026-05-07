"""Tests for sensor.py — Phase 2 RCP sensors and new entity classes.

Sprint C coverage target: lines 41-102 (async_setup_entry factory),
818-907 (AlarmCatalog, MotionZones, TlsCert, NetworkServices, IvaCatalog),
923-1036 (PrivateAreas, MotionZones extras, AmbientLightSchedule),
1058-1176 (AlarmStateSensor, StreamStatusSensor, NvrStateSensor),
1194-1267 (FcmPushStatusSensor extra_state_attributes).

Covers: BoschAlarmCatalogSensor, BoschTlsCertSensor, BoschNetworkServicesSensor,
BoschIvaCatalogSensor, BoschAmbientLightScheduleSensor, BoschAlarmStateSensor,
BoschStreamStatusSensor, async_setup_entry entity-creation gating.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM2_ID = "20E053B5-BE64-4E45-A2CA-BBDC20F5C351"


def _stub_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                    "featureSupport": {"light": True, "panLimit": 0},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        _wifiinfo_cache={},
        _rcp_alarm_catalog_cache={},
        _rcp_motion_zones_cache={},
        _rcp_motion_coords_cache={},
        _cloud_zones_cache={},
        _gen2_zones_cache={},
        _rcp_tls_cert_cache={},
        _rcp_network_services_cache={},
        _rcp_iva_catalog_cache={},
        _rcp_private_areas_cache={},
        _ambient_lighting_cache={},
        _ambient_schedule_cache={},
        _alarm_status_cache={},
        _alarm_settings_cache={},
        _arming_cache={},
        _live_connections={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_warming=set(),
        _nvr_drain_state={},
        _commissioned_cache={},
        _firmware_cache={},
        _unread_events_cache={},
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_push_mode="auto",
        _fcm_last_push=0.0,
        last_update_success=True,
        options={"enable_fcm_push": True, "enable_sensors": True, "enable_nvr": False},
        motion_settings=lambda cid: {"enabled": True, "motionAlarmConfiguration": "HIGH"},
        audio_alarm_settings=lambda cid: {"enabled": True, "threshold": 50},
        is_camera_online=lambda cid: True,
        is_stream_warming=lambda cid: False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord():
    return _stub_coord()


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── async_setup_entry entity-gating ──────────────────────────────────────────

class TestAsyncSetupEntryGating:
    def test_light_sensor_added_only_when_has_light(self):
        """BoschLedDimmerSensor must only be added for cameras with featureSupport.light=True."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = True
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschLedDimmerSensor" in entity_classes, \
            "LedDimmerSensor must be added when has_light=True"

    def test_light_sensor_skipped_when_no_light(self):
        """BoschLedDimmerSensor must not be added when featureSupport.light=False."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschLedDimmerSensor" not in entity_classes, \
            "LedDimmerSensor must be skipped when has_light=False"

    def test_ambient_schedule_sensor_added_for_gen2_outdoor(self):
        """BoschAmbientLightScheduleSensor added for Gen2 Outdoor, not for Indoor II."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAmbientLightScheduleSensor" in entity_classes, \
            "AmbientLightScheduleSensor must be added for Gen2 Outdoor"

    def test_ambient_schedule_sensor_skipped_for_indoor_ii(self):
        """BoschAmbientLightScheduleSensor must not appear for HOME_Eyes_Indoor."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAmbientLightScheduleSensor" not in entity_classes, \
            "AmbientLightScheduleSensor must NOT be added for HOME_Eyes_Indoor (no RGB lights)"

    def test_alarm_state_sensor_added_for_indoor_ii(self):
        """BoschAlarmStateSensor only for HOME_Eyes_Indoor / CAMERA_INDOOR_GEN2."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry
        coord = _stub_coord()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAlarmStateSensor" in entity_classes, \
            "AlarmStateSensor must be added for Gen2 Indoor II"

    def test_nvr_sensor_added_only_when_enable_nvr(self):
        """BoschNvrStateSensor must only appear when options.enable_nvr=True."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry
        coord = _stub_coord()
        coord.options = {"enable_nvr": True, "enable_sensors": True}
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschNvrStateSensor" in entity_classes, \
            "NvrStateSensor must be added when enable_nvr=True"

    def test_sensors_skipped_when_disabled_in_options(self):
        """When enable_sensors=False, setup must return immediately (no entities added)."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry
        coord = _stub_coord()
        coord.options = {"enable_sensors": False}
        added = []
        def _fake_add(entities, **kw): added.extend(entities)
        import asyncio
        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert added == [], "No entities must be registered when enable_sensors=False"


# ── BoschAlarmCatalogSensor ───────────────────────────────────────────────────

class TestAlarmCatalogSensor:
    def test_native_value_is_count(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor
        stub_coord._rcp_alarm_catalog_cache[CAM_ID] = [
            {"name": "motion", "type": "motion"},
            {"name": "audio", "type": "audio"},
        ]
        entity = BoschAlarmCatalogSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == 2, "native_value must return count of alarm types"

    def test_native_value_none_when_no_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor
        entity = BoschAlarmCatalogSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when cache not populated"

    def test_available_false_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor
        entity = BoschAlarmCatalogSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when no RCP data"

    def test_available_true_when_cache_populated(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor
        stub_coord._rcp_alarm_catalog_cache[CAM_ID] = []
        entity = BoschAlarmCatalogSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when cache is present (even if empty)"

    def test_extra_attrs_list_alarm_types(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor
        stub_coord._rcp_alarm_catalog_cache[CAM_ID] = [
            {"name": "flame", "type": "fire"},
            {"name": "motion", "type": "motion"},
        ]
        entity = BoschAlarmCatalogSensor(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert "flame" in attrs["alarm_types"], "extra_attrs must list alarm type names"
        assert "fire" in attrs["categories"], "extra_attrs must list unique categories"

    def test_native_unit_is_types(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor
        entity = BoschAlarmCatalogSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_unit_of_measurement == "types", "Unit must be 'types'"


# ── BoschTlsCertSensor ────────────────────────────────────────────────────────

class TestTlsCertSensor:
    def test_native_value_parses_iso_date(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor
        stub_coord._rcp_tls_cert_cache[CAM_ID] = {
            "not_after": "2028-12-31T23:59:59",
            "not_before": "2024-01-01T00:00:00",
            "issuer": "Bosch",
            "subject": "cam123",
        }
        entity = BoschTlsCertSensor(stub_coord, CAM_ID, stub_entry)
        val = entity.native_value
        assert isinstance(val, datetime), "native_value must be a datetime object"
        assert val.year == 2028, "Must parse year 2028 from ISO date"

    def test_native_value_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor
        entity = BoschTlsCertSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when no cert cached"

    def test_native_value_none_for_malformed_date(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor
        stub_coord._rcp_tls_cert_cache[CAM_ID] = {"not_after": "not-a-date"}
        entity = BoschTlsCertSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None for unparseable date"

    def test_available_follows_cache_presence(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor
        stub_coord._rcp_tls_cert_cache[CAM_ID] = {"not_after": "2028-01-01T00:00:00"}
        entity = BoschTlsCertSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when cert data is cached"

    def test_extra_attrs_include_issuer_and_subject(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor
        stub_coord._rcp_tls_cert_cache[CAM_ID] = {
            "issuer": "Bosch CA", "subject": "camera-001",
            "key_size": 2048, "serial": "AABBCC",
            "not_before": "2024-01-01", "not_after": "2028-01-01",
            "signature_algorithm": "SHA256withRSA",
        }
        entity = BoschTlsCertSensor(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["issuer"] == "Bosch CA", "extra_attrs must expose issuer"
        assert attrs["key_size"] == 2048, "extra_attrs must expose key_size"


# ── BoschNetworkServicesSensor ────────────────────────────────────────────────

class TestNetworkServicesSensor:
    def test_native_value_is_count(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor
        stub_coord._rcp_network_services_cache[CAM_ID] = [
            {"name": "RTSP", "enabled": True}, {"name": "HTTPS", "enabled": True}
        ]
        entity = BoschNetworkServicesSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == 2, "native_value must count services"

    def test_native_value_none_when_not_cached(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor
        entity = BoschNetworkServicesSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when not yet fetched via RCP"

    def test_available_false_without_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor
        entity = BoschNetworkServicesSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when no RCP data"

    def test_extra_attrs_include_services_list(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor
        services = [{"name": "RTSP", "enabled": True}]
        stub_coord._rcp_network_services_cache[CAM_ID] = services
        entity = BoschNetworkServicesSensor(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["services"] == services, "extra_attrs must expose the services list"


# ── BoschAmbientLightScheduleSensor ──────────────────────────────────────────

class TestAmbientLightScheduleSensor:
    def test_disabled_when_ambient_light_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": False}
        entity = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "disabled", "Must return 'disabled' when ambientLightEnabled=False"

    def test_dusk_to_dawn_when_schedule_environment(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": {"type": "ENVIRONMENT"},
        }
        entity = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "dusk_to_dawn", "ENVIRONMENT schedule must map to 'dusk_to_dawn'"

    def test_dusk_to_dawn_for_string_schedule(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "ENVIRONMENT",  # flat string form
        }
        entity = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "dusk_to_dawn", "String 'ENVIRONMENT' must also map to 'dusk_to_dawn'"

    def test_manual_for_non_environment_schedule(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": {"type": "MANUAL", "lightOnTime": "21:00", "lightOffTime": "06:00"},
        }
        entity = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "manual", "Non-ENVIRONMENT schedule must map to 'manual'"

    def test_native_value_none_when_cache_empty(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        entity = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when cache is empty"

    def test_available_requires_non_empty_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": False}
        entity = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when cache has data"

    def test_extra_attrs_include_schedule_times(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightScheduleSensor
        stub_coord._ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": {"type": "MANUAL", "lightOnTime": "20:00", "lightOffTime": "07:00"},
        }
        entity = BoschAmbientLightScheduleSensor(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["schedule_on_time"] == "20:00", "extra_attrs must include lightOnTime"
        assert attrs["schedule_off_time"] == "07:00", "extra_attrs must include lightOffTime"


# ── BoschAlarmStateSensor ─────────────────────────────────────────────────────

class TestAlarmStateSensor:
    def test_native_value_from_alarm_status_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor
        stub_coord._alarm_status_cache[CAM_ID] = {"intrusionSystem": "ACTIVE", "alarmType": "INTRUSION"}
        entity = BoschAlarmStateSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "active", "Must lowercase intrusionSystem for state"

    def test_native_value_falls_back_to_arming_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor
        stub_coord._alarm_status_cache = {}
        stub_coord._arming_cache[CAM_ID] = True
        entity = BoschAlarmStateSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "active", "Must fall back to arming cache when status cache empty"

    def test_native_value_inactive_from_arming_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor
        stub_coord._alarm_status_cache = {}
        stub_coord._arming_cache[CAM_ID] = False
        entity = BoschAlarmStateSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "inactive", "Must return 'inactive' when arming_cache=False"

    def test_native_value_unknown_when_no_data(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor
        entity = BoschAlarmStateSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "unknown", "Must return 'unknown' when no data available"

    def test_available_requires_only_coordinator_success(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor
        stub_coord.is_camera_online = lambda cid: False
        entity = BoschAlarmStateSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.available is True, "AlarmStateSensor must not gate on camera-online"

    def test_extra_attrs_include_alarm_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor
        stub_coord._alarm_settings_cache[CAM_ID] = {
            "alarmMode": "ON", "preAlarmMode": "OFF",
            "alarmDelayInSeconds": 30, "alarmActivationDelaySeconds": 10,
        }
        stub_coord._alarm_status_cache[CAM_ID] = {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}
        entity = BoschAlarmStateSensor(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["alarm_mode"] == "ON", "extra_attrs must expose alarmMode"
        assert attrs["siren_duration_s"] == 30, "extra_attrs must expose alarmDelayInSeconds"


# ── BoschStreamStatusSensor ───────────────────────────────────────────────────

class TestStreamStatusSensor:
    def test_idle_when_no_connection(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor
        stub_coord._live_connections = {}
        entity = BoschStreamStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "idle", "Must be 'idle' when no live connection"

    def test_warming_up_when_stream_warming(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor
        stub_coord.is_stream_warming = lambda cid: True
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        entity = BoschStreamStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "warming_up", "Must be 'warming_up' while stream pre-warms"

    def test_streaming_when_rtsps_url_present(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://cam/stream", "_connection_type": "LOCAL"}
        entity = BoschStreamStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "streaming", "Must be 'streaming' when RTSP URL available"

    def test_streaming_remote_when_fell_back(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://cam/stream"}
        stub_coord._stream_fell_back[CAM_ID] = True
        entity = BoschStreamStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "streaming_remote", "Must be 'streaming_remote' when fell back to cloud"

    def test_connecting_when_session_open_but_no_url(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor
        stub_coord._live_connections[CAM_ID] = {}  # session open but no rtspsUrl yet
        entity = BoschStreamStatusSensor(stub_coord, CAM_ID, stub_entry)
        assert entity.native_value == "connecting", "Must be 'connecting' when session exists but no URL"

    def test_extra_attrs_include_connection_type(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor
        stub_coord._live_connections[CAM_ID] = {"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"}
        stub_coord._stream_error_count[CAM_ID] = 2
        entity = BoschStreamStatusSensor(stub_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["connection_type"] == "LOCAL", "extra_attrs must include connection_type"
        assert attrs["stream_errors"] == 2, "extra_attrs must include stream_errors"
