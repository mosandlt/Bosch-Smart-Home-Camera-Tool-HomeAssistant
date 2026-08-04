"""Tests for binary_sensor.py — motion / audio_alarm / person / LAN-reachable
binary sensors, plus the feature flags that gate whether the platform (and
go2rtc auto-setup) is set up at all.

Each event-based sensor (motion / audio_alarm / person) reads the most-recent
event of its type from `coordinator.data[cam_id]["events"]` and is ON only if
that event's timestamp is within the configured active window of now. The
window defaults to 90 seconds (`DEFAULT_MOTION_ACTIVE_WINDOW`) — long enough
to cover the polling-only fallback (60s scan_interval plus margin) — and is
configurable via the `motion_active_window` option (range 10-300s, clamped;
non-integer values fall back to the default).

`BoschLanReachableBinarySensor` surfaces the coordinator's LAN-ping cache to
automations and the overview-card LAN tiles; unlike the event-based sensors
it is always available (readable even while the Bosch cloud is down) and
passes through None/True/False from the coordinator helper untouched.

The `enable_binary_sensors` option gates whether the `binary_sensor` platform
is forwarded at all in `async_setup_entry` (in `__init__.py`); `enable_go2rtc`
(tested alongside it here since both flags were audited together) gates
whether go2rtc auto-setup runs.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.const import ALL_PLATFORMS, DEFAULT_OPTIONS
from tests.source_match import assert_in_source

CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"sound": True},
                },
                "events": [],
            }
        },
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _ago_iso(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _patched_hass(entity):
    """Bind a fake hass.config.time_zone for `_event_within_window`."""
    fake_hass = MagicMock()
    fake_hass.config.time_zone = "UTC"
    entity.hass = fake_hass
    return entity


def _make_coord(events: list | None = None) -> SimpleNamespace:
    """Build a stub coordinator with a configurable events list (used by the
    motion-active-window tests, which need many distinct event timestamps)."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"sound": True},
                },
                "events": events or [],
            }
        },
        async_add_listener=MagicMock(return_value=MagicMock()),
    )


def _make_entry(motion_active_window: object = None) -> SimpleNamespace:
    opts: dict = {}
    if motion_active_window is not None:
        opts["motion_active_window"] = motion_active_window
    return SimpleNamespace(entry_id="01ENTRY", data={}, options=opts)


class TestMotionBinarySensor:
    def test_off_when_no_events(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_on_with_recent_movement(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is True

    def test_off_with_old_movement(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """An event older than 90s is outside the active window."""
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(120)},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_off_when_only_audio_event(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A recent AUDIO_ALARM must NOT trigger the motion sensor."""
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "AUDIO_ALARM", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_attrs_include_event_metadata(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "MOVEMENT",
                "id": "evt-123",
                "timestamp": _now_iso(),
                "imageUrl": "https://...",
            }
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        attrs = s.extra_state_attributes
        assert attrs["event_id"] == "evt-123"
        assert "image_url" not in attrs

    def test_attrs_empty_when_no_events(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.extra_state_attributes == {}

    def test_disabled_by_default(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Binary sensors are hidden until user enables — avoids UI clutter."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        assert s._attr_entity_registry_enabled_default is False


class TestAudioAlarmBinarySensor:
    def test_off_when_no_events(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_on_with_recent_audio_alarm(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "AUDIO_ALARM", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is True

    def test_attrs_include_audio_event_metadata(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """extra_state_attributes returns event_id/timestamp only (image_url omitted — PII)."""
        stub_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "AUDIO_ALARM",
                "id": "aud-99",
                "timestamp": _now_iso(),
                "imageUrl": "http://img",
            },
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        attrs = s.extra_state_attributes
        assert attrs["event_id"] == "aud-99"
        assert "image_url" not in attrs

    def test_attrs_empty_when_no_audio_event(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.extra_state_attributes == {}

    def test_off_with_only_movement_event(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False


class TestPersonDetectedBinarySensor:
    def test_off_when_no_events(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        s = _patched_hass(
            BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        )
        assert s.is_on is False

    def test_on_with_recent_person_event(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "PERSON", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        s = _patched_hass(
            BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        )
        assert s.is_on is True

    def test_unique_id_includes_cam_id(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        s = BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        assert CAM_ID in s._attr_unique_id

    def test_attrs_include_person_event_metadata(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """extra_state_attributes returns event_id/timestamp only (image_url omitted — PII)."""
        stub_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "PERSON",
                "id": "per-77",
                "timestamp": _now_iso(),
                "imageUrl": "http://pic",
            },
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        s = _patched_hass(
            BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        )
        attrs = s.extra_state_attributes
        assert attrs["event_id"] == "per-77"
        assert "image_url" not in attrs

    def test_attrs_empty_when_no_person_event(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        s = _patched_hass(
            BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        )
        assert s.extra_state_attributes == {}


class TestPersonDetectedDeviceClass:
    """Person sensor must be OCCUPANCY, not MOTION — voice/automation class filter."""

    def test_person_sensor_device_class_is_occupancy(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        s = BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        assert s._attr_device_class == BinarySensorDeviceClass.OCCUPANCY

    def test_motion_sensor_device_class_is_motion(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from homeassistant.components.binary_sensor import BinarySensorDeviceClass

        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        assert s._attr_device_class == BinarySensorDeviceClass.MOTION

    def test_image_url_not_in_motion_attrs(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Signed Bosch URLs must never reach the HA recorder via sensor attrs."""
        stub_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "MOVEMENT",
                "id": "pii-1",
                "timestamp": _now_iso(),
                "imageUrl": "https://bosch.example.com/signed?token=abc",
            }
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert "image_url" not in s.extra_state_attributes

    def test_image_url_not_in_person_attrs(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Signed Bosch URLs must never reach the HA recorder via sensor attrs."""
        stub_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "PERSON",
                "id": "pii-2",
                "timestamp": _now_iso(),
                "imageUrl": "https://bosch.example.com/signed?token=xyz",
            }
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        s = _patched_hass(
            BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        )
        assert "image_url" not in s.extra_state_attributes

    def test_image_url_not_in_audio_alarm_attrs(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Signed Bosch URLs must never reach the HA recorder via sensor attrs."""
        stub_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "AUDIO_ALARM",
                "id": "pii-3",
                "timestamp": _now_iso(),
                "imageUrl": "https://bosch.example.com/signed?token=def",
            }
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert "image_url" not in s.extra_state_attributes


class TestEventWindow:
    def test_empty_timestamp_returns_false(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s._event_within_window({}) is False
        assert s._event_within_window({"timestamp": ""}) is False

    def test_malformed_timestamp_returns_false(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s._event_within_window({"timestamp": "not-iso8601"}) is False

    def test_iso_with_milliseconds_works(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Bosch API may append `.000Z` — we strip to first 19 chars."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        ts = _now_iso() + ".000Z"
        assert s._event_within_window({"timestamp": ts}) is True

    def test_utc_event_in_berlin_timezone_fires(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """UTC-Z timestamps must compare correctly in non-UTC user timezones.

        `_event_within_window` must not strip the `Z` suffix and replace
        tzinfo with the user's local timezone (e.g. Europe/Berlin, +02:00 in
        summer) — doing so makes a UTC event from 30s ago appear 2h 30s old,
        i.e. outside the active window, so the motion sensor would never fire
        for users in non-UTC timezones.
        """
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        fake_hass.config.time_zone = "Europe/Berlin"  # user's HA tz
        s.hass = fake_hass
        # Bosch /v11/events response format — UTC with Z suffix
        ts_utc = _now_iso() + ".000Z"
        assert s._event_within_window({"timestamp": ts_utc}) is True

    def test_30s_old_utc_event_in_berlin(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """30s-old event in Berlin TZ must also fire — within the 90s window."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        fake_hass.config.time_zone = "Europe/Berlin"
        s.hass = fake_hass
        ts_30s_ago = _ago_iso(30) + ".000Z"
        assert s._event_within_window({"timestamp": ts_30s_ago}) is True

    def test_device_info_structure(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """device_info must include DOMAIN identifier and Bosch manufacturer."""
        from custom_components.bosch_shc_camera import DOMAIN
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        info = s.device_info
        assert info["manufacturer"] == "Bosch"
        assert (DOMAIN, CAM_ID) in info["identifiers"]

    def test_2hour_old_utc_event_in_berlin_does_not_fire(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A genuinely-old (2h) event must NOT fire even with the timezone fix.

        Sanity check that the timezone fix didn't accidentally make stale
        events appear fresh: a 2h-old UTC event is 2h old in any timezone,
        so it stays outside the 90s window → False.
        """
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        fake_hass.config.time_zone = "Europe/Berlin"
        s.hass = fake_hass
        ts_old = _ago_iso(7200) + ".000Z"  # 2h ago
        assert s._event_within_window({"timestamp": ts_old}) is False


class TestSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_motion_and_person_for_no_sound_cam(self):
        """Camera without sound feature → 2 entities (Motion + PersonDetected)."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
            BoschPersonDetectedBinarySensor,
            async_setup_entry,
        )

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "featureSupport": {"sound": False},
                    },
                    "events": [],
                }
            },
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            entry_id="01E",
            data={},
            options={},
            runtime_data=coord,
            async_on_unload=MagicMock(),
        )
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        types_ = {type(e) for e in captured}
        assert BoschMotionBinarySensor in types_
        assert BoschPersonDetectedBinarySensor in types_
        # +1 BoschLanReachableBinarySensor: always-on diagnostic for
        # LAN-fallback paths. Pinning by class set keeps this stable across
        # future additions of unrelated entities.
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschLanReachableBinarySensor,
        )

        assert BoschLanReachableBinarySensor in types_
        assert len(captured) == 3

    @pytest.mark.asyncio
    async def test_creates_audio_sensor_when_sound_supported(self):
        """Camera with sound feature → 4 entities (Motion + Person + LanReachable + AudioAlarm)."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
            BoschLanReachableBinarySensor,
            async_setup_entry,
        )

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Innen",
                        "hardwareVersion": "CAMERA_360",
                        "featureSupport": {"sound": True},
                    },
                    "events": [],
                }
            },
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            entry_id="01E",
            data={},
            options={},
            runtime_data=coord,
            async_on_unload=MagicMock(),
        )
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        types_ = {type(e) for e in captured}
        assert BoschAudioAlarmBinarySensor in types_
        assert BoschLanReachableBinarySensor in types_
        assert len(captured) == 4

    @pytest.mark.asyncio
    async def test_empty_coordinator_yields_no_entities(self):
        from custom_components.bosch_shc_camera.binary_sensor import async_setup_entry

        coord = SimpleNamespace(
            data={}, async_add_listener=MagicMock(return_value=MagicMock())
        )
        entry = SimpleNamespace(
            entry_id="01E",
            data={},
            options={},
            runtime_data=coord,
            async_on_unload=MagicMock(),
        )
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        assert captured == []

    @pytest.mark.asyncio
    async def test_new_camera_gets_entities_added_dynamically(self):
        """Quality-Scale Gold `dynamic-devices`."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
            async_setup_entry,
        )

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "featureSupport": {"sound": False},
                    },
                    "events": [],
                }
            },
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            entry_id="01E",
            data={},
            options={},
            runtime_data=coord,
            async_on_unload=MagicMock(),
        )
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        coord.async_add_listener.assert_called_once()
        entry.async_on_unload.assert_called_once()
        listener = coord.async_add_listener.call_args[0][0]

        captured.clear()
        coord.data["NEW-CAM"] = {
            "info": {"hardwareVersion": "HOME_Eyes_Outdoor", "featureSupport": {}},
            "events": [],
        }
        listener()
        assert any(
            isinstance(e, BoschMotionBinarySensor) and e._cam_id == "NEW-CAM"
            for e in captured
        )

        captured.clear()
        listener()
        assert captured == []


class TestEnableBinarySensors:
    """binary_sensor platform is conditionally included based on the option.

    The gate lives in async_setup_entry in __init__.py:
        platforms = [p for p in ALL_PLATFORMS if p != "binary_sensor"]
        if opts.get("enable_binary_sensors", True):
            platforms = ["binary_sensor"] + platforms
    """

    def _build_platforms(self, opts: dict) -> list[str]:
        """Reproduce the platform-list logic from async_setup_entry."""
        platforms = [p for p in ALL_PLATFORMS if p != "binary_sensor"]
        if opts.get("enable_binary_sensors", True):
            platforms = ["binary_sensor", *platforms]
        return platforms

    def test_binary_sensor_included_when_enabled(self):
        platforms = self._build_platforms({"enable_binary_sensors": True})
        assert "binary_sensor" in platforms

    def test_binary_sensor_excluded_when_disabled(self):
        """Core regression: user sets enable_binary_sensors=False → platform
        must not be forwarded so no motion/audio/person sensors are created."""
        platforms = self._build_platforms({"enable_binary_sensors": False})
        assert "binary_sensor" not in platforms

    def test_binary_sensor_included_by_default(self):
        """Default (option absent) → binary sensors are active."""
        platforms = self._build_platforms({})
        assert "binary_sensor" in platforms

    def test_all_other_platforms_always_present(self):
        """Disabling binary_sensor must not drop any other platform."""
        disabled = set(self._build_platforms({"enable_binary_sensors": False}))
        for p in ALL_PLATFORMS:
            if p != "binary_sensor":
                assert p in disabled, (
                    f"Platform {p!r} disappeared from the list when "
                    "enable_binary_sensors=False — platform gating logic is broken"
                )

    def test_gate_present_in_source(self):
        """Pin the exact source-level guard so a refactor can't silently remove it."""
        # async_setup_entry is module-level, not a method — inspect the module source.
        import custom_components.bosch_shc_camera as init_module

        src = inspect.getsource(init_module)
        assert_in_source(  # enable_binary_sensors gate missing from __init__.py async_setup_entry — disabling the option would have no effect
            src, 'opts.get("enable_binary_sensors", True)'
        )

    def test_default_enable_binary_sensors_true(self):
        assert DEFAULT_OPTIONS.get("enable_binary_sensors", True) is True


class TestEnableGo2rtc:
    """go2rtc auto-setup is skipped when enable_go2rtc=False.

    The gate lives in async_setup_entry in __init__.py:
        if opts.get("enable_go2rtc", True):
            go2rtc_lock = hass.data.setdefault(...)
            async with go2rtc_lock:
                go2rtc_entries = hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await hass.config_entries.flow.async_init("go2rtc", ...)
    """

    def test_gate_present_in_source(self):
        """Pin the source-level guard so a refactor can't silently remove it."""
        import custom_components.bosch_shc_camera as init_module

        src = inspect.getsource(init_module)
        assert_in_source(  # enable_go2rtc gate missing from __init__.py async_setup_entry — disabling go2rtc would have no effect
            src, 'opts.get("enable_go2rtc", True)'
        )

    def test_default_enable_go2rtc_true(self):
        assert DEFAULT_OPTIONS.get("enable_go2rtc", True) is True

    @pytest.mark.asyncio
    async def test_go2rtc_init_skipped_when_disabled(self):
        """When enable_go2rtc=False, flow.async_init must never be called."""
        import asyncio
        from unittest.mock import AsyncMock

        flow_init = AsyncMock(return_value={"type": "create_entry"})
        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(return_value=[]),
                flow=SimpleNamespace(async_init=flow_init),
            ),
        )
        opts = {"enable_go2rtc": False}

        # Replicate the gated block from async_setup_entry
        if opts.get("enable_go2rtc", True):
            go2rtc_lock = fake_hass.data.setdefault(
                "bosch_shc_camera_go2rtc_init_lock", asyncio.Lock()
            )
            async with go2rtc_lock:
                go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await fake_hass.config_entries.flow.async_init(
                        "go2rtc", context={"source": "system"}, data={}
                    )

        flow_init.assert_not_called()

    @pytest.mark.asyncio
    async def test_go2rtc_init_called_when_enabled_and_no_existing_entry(self):
        """When enable_go2rtc=True and no go2rtc entry exists, init is called."""
        import asyncio
        from unittest.mock import AsyncMock

        flow_init = AsyncMock(return_value={"type": "create_entry"})
        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(return_value=[]),  # no existing entry
                flow=SimpleNamespace(async_init=flow_init),
            ),
        )
        opts = {"enable_go2rtc": True}

        if opts.get("enable_go2rtc", True):
            go2rtc_lock = fake_hass.data.setdefault(
                "bosch_shc_camera_go2rtc_init_lock", asyncio.Lock()
            )
            async with go2rtc_lock:
                go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await fake_hass.config_entries.flow.async_init(
                        "go2rtc", context={"source": "system"}, data={}
                    )

        flow_init.assert_called_once_with(
            "go2rtc", context={"source": "system"}, data={}
        )

    @pytest.mark.asyncio
    async def test_go2rtc_init_skipped_when_entry_already_exists(self):
        """When go2rtc entry already active, init must NOT be called (no duplicates)."""
        import asyncio
        from unittest.mock import AsyncMock

        existing_entry = SimpleNamespace(entry_id="existing_go2rtc")
        flow_init = AsyncMock(return_value={"type": "create_entry"})
        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(return_value=[existing_entry]),
                flow=SimpleNamespace(async_init=flow_init),
            ),
        )
        opts = {"enable_go2rtc": True}

        if opts.get("enable_go2rtc", True):
            go2rtc_lock = fake_hass.data.setdefault(
                "bosch_shc_camera_go2rtc_init_lock", asyncio.Lock()
            )
            async with go2rtc_lock:
                go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await fake_hass.config_entries.flow.async_init(
                        "go2rtc", context={"source": "system"}, data={}
                    )

        flow_init.assert_not_called()

    @pytest.mark.asyncio
    async def test_go2rtc_lock_prevents_duplicate_parallel_inits(self):
        """Two concurrent callers share the same lock — only one fires async_init."""
        import asyncio

        call_count = 0
        created_entries: list = []

        async def fake_init(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Simulate: after first init, entry now exists
            created_entries.append(SimpleNamespace(entry_id="new_go2rtc"))
            return {"type": "create_entry"}

        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(
                    side_effect=lambda domain: list(created_entries)
                ),
                flow=SimpleNamespace(async_init=fake_init),
            ),
        )
        opts = {"enable_go2rtc": True}

        async def _setup():
            if opts.get("enable_go2rtc", True):
                go2rtc_lock = fake_hass.data.setdefault(
                    "bosch_shc_camera_go2rtc_init_lock", asyncio.Lock()
                )
                async with go2rtc_lock:
                    go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                    if not go2rtc_entries:
                        await fake_hass.config_entries.flow.async_init(
                            "go2rtc", context={"source": "system"}, data={}
                        )

        await asyncio.gather(_setup(), _setup())
        assert call_count == 1, (
            "go2rtc async_init called more than once despite the lock — "
            "duplicate go2rtc entries would be created on parallel setup"
        )


class TestBinarySensorSetupEntry:
    """When the platform IS forwarded (enable_binary_sensors=True), verify that
    async_setup_entry inside binary_sensor.py creates the expected entities.
    This complements TestEnableBinarySensors' __init__.py-gate test with an
    end-to-end check that the platform itself works when enabled.
    """

    def test_setup_entry_creates_entities_when_enabled(self):
        """With a coordinator that has one camera, setup must add entities."""
        import asyncio

        from custom_components.bosch_shc_camera.binary_sensor import async_setup_entry

        cam_id = "TEST-CAM-001"
        coord = SimpleNamespace(
            data={
                cam_id: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                        "macAddress": "xx:xx:xx:xx:xx:xx",
                        "featureSupport": {"sound": False},
                    },
                    "events": [],
                }
            },
            options={"enable_binary_sensors": True},
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            runtime_data=coord,
            entry_id="01TEST",
            data={},
            options={},
            async_on_unload=MagicMock(),
        )
        added: list = []
        asyncio.run(async_setup_entry(None, entry, lambda e, **kw: added.extend(e)))
        assert len(added) >= 2, (
            "Expected at least motion + person entities when enable_binary_sensors=True"
        )

    def test_setup_entry_creates_audio_sensor_when_sound_supported(self):
        """Camera with featureSupport.sound=True gets an extra audio alarm sensor."""
        import asyncio

        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
            async_setup_entry,
        )

        cam_id = "TEST-CAM-SOUND"
        coord = SimpleNamespace(
            data={
                cam_id: {
                    "info": {
                        "title": "Innen",
                        "hardwareVersion": "CAMERA_360",
                        "firmwareVersion": "7.91.56",
                        "macAddress": "xx:xx:xx:xx:xx:xx",
                        "featureSupport": {"sound": True},
                    },
                    "events": [],
                }
            },
            options={},
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            runtime_data=coord,
            entry_id="01TEST2",
            data={},
            options={},
            async_on_unload=MagicMock(),
        )
        added: list = []
        asyncio.run(async_setup_entry(None, entry, lambda e, **kw: added.extend(e)))
        audio_sensors = [e for e in added if isinstance(e, BoschAudioAlarmBinarySensor)]
        assert len(audio_sensors) == 1, (
            "Expected one BoschAudioAlarmBinarySensor for sound-capable camera"
        )


class TestFeatureFlagCoverage:
    """Enforce that every boolean feature flag has a behavior test somewhere.

    This is a soft guard — it checks the test suite for test functions
    referencing each flag alongside behavior-asserting keywords.

    Coverage for flags not exercised in this file lives elsewhere:
      - enable_snapshots       -> test_camera_async.py::test_skip_when_snapshots_disabled
      - enable_sensors         -> test_sensor_round6.py::test_sensors_skipped_when_disabled
      - enable_snapshot_button -> test_buttons.py::test_snapshot_button_skipped_disabled
      - enable_local_save      -> test_fcm_round8.py
      - enable_fcm_push        -> test_sensors.py (health sensor state)
      - enable_nvr             -> test_sensor_round6.py, test_recorder.py
      - enable_smb_upload      -> test_fcm_round8.py
      - mark_events_read       -> test_fcm_round8.py (called/not called)
      - alert_save_snapshots   -> test_fcm_round7.py
      - stream_connection_type -> test_init_sprint_kd.py
    """

    FEATURE_FLAGS = [  # noqa: RUF012 # test-local constant list, not entity state
        "enable_snapshots",
        "enable_sensors",
        "enable_binary_sensors",
        "enable_snapshot_button",
        "enable_local_save",
        "enable_fcm_push",
        "enable_nvr",
        "enable_smb_upload",
        "enable_go2rtc",
        "mark_events_read",
        "alert_save_snapshots",
    ]

    def test_each_flag_referenced_in_tests(self):
        """Every feature flag must appear in at least one test file.

        Catching the 'someone added a flag but never wrote a test' case.
        """
        from pathlib import Path

        tests_dir = Path(__file__).parent
        all_test_text = "\n".join(f.read_text() for f in tests_dir.glob("test_*.py"))
        missing = [flag for flag in self.FEATURE_FLAGS if flag not in all_test_text]
        assert not missing, (
            f"Feature flags with NO test coverage at all: {missing}. "
            "Add a behavior test for each."
        )


# BoschLanReachableBinarySensor stays available while the Bosch cloud is down and passes None/True/False through from the coordinator helper untouched.


@pytest.fixture
def lan_stub_coord() -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": []}}
    coord.lan_tcp_reachable = {}
    coord.local_write_at = {}
    coord.LOCAL_WRITE_GRACE_S = 30.0
    coord.is_lan_reachable = lambda _cid: None
    return coord


def _make_lan_sensor(lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace):
    from custom_components.bosch_shc_camera.binary_sensor import (
        BoschLanReachableBinarySensor,
    )

    return BoschLanReachableBinarySensor(lan_stub_coord, CAM_ID, stub_entry)


class TestAvailable:
    def test_always_returns_true(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """The sensor must stay readable while the Bosch cloud is down —
        that is the entire reason it exists."""
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        assert s.available is True

    def test_available_even_when_coordinator_last_update_success_false(
        self,
        lan_stub_coord: SimpleNamespace,
        stub_entry: SimpleNamespace,
    ):
        lan_stub_coord.last_update_success = False
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        assert s.available is True


class TestIsOn:
    def test_returns_none_when_coordinator_helper_missing(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Stub coordinators in older tests may lack `is_lan_reachable` —
        getattr fallback returns None instead of raising."""
        del lan_stub_coord.is_lan_reachable
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        assert s.is_on is None

    def test_returns_none_when_helper_returns_none(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        lan_stub_coord.is_lan_reachable = lambda _cid: None
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        assert s.is_on is None

    def test_returns_true_when_helper_returns_true(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        lan_stub_coord.is_lan_reachable = lambda _cid: True
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        assert s.is_on is True

    def test_returns_false_when_helper_returns_false(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        lan_stub_coord.is_lan_reachable = lambda _cid: False
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        assert s.is_on is False


class TestExtraStateAttributes:
    def test_minimal_attrs_when_cache_empty(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs == {"camera_id": CAM_ID}

    def test_adds_last_check_seconds_ago(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        # cache populated 10s ago (current monotonic - 10)
        with patch("time.monotonic", return_value=1010.0):
            lan_stub_coord.lan_tcp_reachable[CAM_ID] = (True, 1000.0)
            s = _make_lan_sensor(lan_stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        assert attrs["camera_id"] == CAM_ID
        assert attrs["last_check_seconds_ago"] == 10

    def test_adds_write_grace_when_inside_window(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        with patch("time.monotonic", return_value=1010.0):
            lan_stub_coord.local_write_at[CAM_ID] = 1000.0  # 10s ago, inside 30s grace
            s = _make_lan_sensor(lan_stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        # grace_left = 30 - 10 = 20s
        assert attrs.get("write_grace_seconds_left") == 20

    def test_no_write_grace_when_outside_window(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        with patch("time.monotonic", return_value=1100.0):
            lan_stub_coord.local_write_at[CAM_ID] = (
                1000.0  # 100s ago, outside 30s grace
            )
            s = _make_lan_sensor(lan_stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        assert "write_grace_seconds_left" not in attrs

    def test_no_grace_when_coordinator_lacks_local_write_at(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Belt-and-braces guard for stub coordinators in legacy tests."""
        del lan_stub_coord.local_write_at
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        attrs = s.extra_state_attributes
        assert "write_grace_seconds_left" not in attrs

    def test_combines_last_check_and_grace_when_both_set(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        with patch("time.monotonic", return_value=1010.0):
            lan_stub_coord.lan_tcp_reachable[CAM_ID] = (True, 1005.0)
            lan_stub_coord.local_write_at[CAM_ID] = 1000.0
            s = _make_lan_sensor(lan_stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        assert attrs["last_check_seconds_ago"] == 5
        assert attrs["write_grace_seconds_left"] == 20
        assert attrs["camera_id"] == CAM_ID

    def test_volatile_attrs_are_unrecorded(
        self, lan_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Both freshness fields change every tick, so they must be excluded
        from the recorder to avoid bloating `state_attributes` — they are
        still emitted live (asserted above); only their recording is
        suppressed."""
        s = _make_lan_sensor(lan_stub_coord, stub_entry)
        assert "last_check_seconds_ago" in s._unrecorded_attributes
        assert "write_grace_seconds_left" in s._unrecorded_attributes


# motion_active_window option (range 10-300s, default 90s, non-integer values fall back to default) controls how long motion/audio/person sensors stay ON after an event; out-of-range values are clamped.


class TestMotionActiveWindowProperty:
    """Unit tests for _BoschBinarySensorBase._motion_active_window."""

    def _make_sensor(self, option_value: object = None) -> object:
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        coord = _make_coord()
        entry = _make_entry(option_value)
        return BoschMotionBinarySensor(coord, CAM_ID, entry)

    def test_default_when_option_absent(self) -> None:
        """No option set → _motion_active_window returns DEFAULT_MOTION_ACTIVE_WINDOW (90)."""
        from custom_components.bosch_shc_camera.const import (
            DEFAULT_MOTION_ACTIVE_WINDOW,
        )

        sensor = self._make_sensor()
        assert sensor._motion_active_window == DEFAULT_MOTION_ACTIVE_WINDOW

    def test_custom_value_30s(self) -> None:
        """Custom value 30 is returned as-is (within valid range)."""
        sensor = self._make_sensor(30)
        assert sensor._motion_active_window == 30

    def test_boundary_min_10s(self) -> None:
        """Boundary min 10 s is accepted and returned."""
        sensor = self._make_sensor(10)
        assert sensor._motion_active_window == 10

    def test_boundary_max_300s(self) -> None:
        """Boundary max 300 s is accepted and returned."""
        sensor = self._make_sensor(300)
        assert sensor._motion_active_window == 300

    def test_garbage_negative_clamped_to_min(self) -> None:
        """Negative value -5 is clamped to MOTION_ACTIVE_WINDOW_MIN (10)."""
        from custom_components.bosch_shc_camera.const import MOTION_ACTIVE_WINDOW_MIN

        sensor = self._make_sensor(-5)
        assert sensor._motion_active_window == MOTION_ACTIVE_WINDOW_MIN

    def test_garbage_too_large_clamped_to_max(self) -> None:
        """Value 1000 is clamped to MOTION_ACTIVE_WINDOW_MAX (300)."""
        from custom_components.bosch_shc_camera.const import MOTION_ACTIVE_WINDOW_MAX

        sensor = self._make_sensor(1000)
        assert sensor._motion_active_window == MOTION_ACTIVE_WINDOW_MAX

    def test_non_integer_string_falls_back_to_default(self) -> None:
        """Non-numeric string falls back to DEFAULT_MOTION_ACTIVE_WINDOW."""
        from custom_components.bosch_shc_camera.const import (
            DEFAULT_MOTION_ACTIVE_WINDOW,
        )

        sensor = self._make_sensor("not_a_number")
        assert sensor._motion_active_window == DEFAULT_MOTION_ACTIVE_WINDOW

    def test_none_value_falls_back_to_default(self) -> None:
        """Explicit None stored in options falls back to DEFAULT_MOTION_ACTIVE_WINDOW.

        `_make_entry(None)` stores `options={"motion_active_window": None}`.
        """
        from custom_components.bosch_shc_camera.const import (
            DEFAULT_MOTION_ACTIVE_WINDOW,
        )

        entry = SimpleNamespace(
            entry_id="01E", data={}, options={"motion_active_window": None}
        )
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        sensor = BoschMotionBinarySensor(_make_coord(), CAM_ID, entry)
        assert sensor._motion_active_window == DEFAULT_MOTION_ACTIVE_WINDOW

    def test_float_is_truncated_to_int(self) -> None:
        """Float 45.9 is coerced to int 45 (within range)."""
        sensor = self._make_sensor(45.9)
        assert sensor._motion_active_window == 45


class TestMotionSensorIsOnWithWindow:
    """Integration-level: is_on respects the configured active window."""

    def _motion_sensor(self, option: object, events: list) -> object:
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        coord = _make_coord(events)
        entry = _make_entry(option)
        return _patched_hass(BoschMotionBinarySensor(coord, CAM_ID, entry))

    def test_default_90s_event_at_89s_is_on(self) -> None:
        """89 s old event is ON with default 90 s window."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(89)}]
        sensor = self._motion_sensor(None, events)
        assert sensor.is_on is True

    def test_default_90s_event_at_91s_is_off(self) -> None:
        """91 s old event is OFF with default 90 s window."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(91)}]
        sensor = self._motion_sensor(None, events)
        assert sensor.is_on is False

    def test_window_30s_event_at_29s_is_on(self) -> None:
        """29 s old event is ON with custom 30 s window."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(29)}]
        sensor = self._motion_sensor(30, events)
        assert sensor.is_on is True

    def test_window_30s_event_at_31s_is_off(self) -> None:
        """31 s old event is OFF with custom 30 s window."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(31)}]
        sensor = self._motion_sensor(30, events)
        assert sensor.is_on is False

    def test_window_min_10s_event_at_9s_is_on(self) -> None:
        """Boundary-min 10 s window: 9 s old event is ON."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(9)}]
        sensor = self._motion_sensor(10, events)
        assert sensor.is_on is True

    def test_window_min_10s_event_at_11s_is_off(self) -> None:
        """Boundary-min 10 s window: 11 s old event is OFF."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(11)}]
        sensor = self._motion_sensor(10, events)
        assert sensor.is_on is False

    def test_window_max_300s_event_at_299s_is_on(self) -> None:
        """Boundary-max 300 s window: 299 s old event is ON."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(299)}]
        sensor = self._motion_sensor(300, events)
        assert sensor.is_on is True

    def test_window_max_300s_event_at_301s_is_off(self) -> None:
        """Boundary-max 300 s window: 301 s old event is OFF."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(301)}]
        sensor = self._motion_sensor(300, events)
        assert sensor.is_on is False

    def test_garbage_negative_clamped_10s_event_at_9s_is_on(self) -> None:
        """Garbage value -5 clamped to 10 s: 9 s old event is ON."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(9)}]
        sensor = self._motion_sensor(-5, events)
        assert sensor.is_on is True

    def test_garbage_1000s_clamped_300s_event_at_299s_is_on(self) -> None:
        """Garbage value 1000 clamped to 300 s: 299 s old event is ON."""
        events = [{"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(299)}]
        sensor = self._motion_sensor(1000, events)
        assert sensor.is_on is True


class TestAllSensorTypesRespectWindow:
    """AudioAlarm and PersonDetected sensors share the same window logic."""

    def test_audio_alarm_uses_configured_window(self) -> None:
        """AudioAlarmBinarySensor: 29 s old event with 30 s window is ON."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        coord = _make_coord(
            [{"eventType": "AUDIO_ALARM", "id": "a1", "timestamp": _ago_iso(29)}]
        )
        sensor = _patched_hass(
            BoschAudioAlarmBinarySensor(coord, CAM_ID, _make_entry(30))
        )
        assert sensor.is_on is True

    def test_audio_alarm_outside_window_is_off(self) -> None:
        """AudioAlarmBinarySensor: 31 s old event with 30 s window is OFF."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        coord = _make_coord(
            [{"eventType": "AUDIO_ALARM", "id": "a1", "timestamp": _ago_iso(31)}]
        )
        sensor = _patched_hass(
            BoschAudioAlarmBinarySensor(coord, CAM_ID, _make_entry(30))
        )
        assert sensor.is_on is False

    def test_person_detected_uses_configured_window(self) -> None:
        """PersonDetectedBinarySensor: 29 s old event with 30 s window is ON."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        coord = _make_coord(
            [{"eventType": "PERSON", "id": "p1", "timestamp": _ago_iso(29)}]
        )
        sensor = _patched_hass(
            BoschPersonDetectedBinarySensor(coord, CAM_ID, _make_entry(30))
        )
        assert sensor.is_on is True

    def test_person_detected_outside_window_is_off(self) -> None:
        """PersonDetectedBinarySensor: 31 s old event with 30 s window is OFF."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        coord = _make_coord(
            [{"eventType": "PERSON", "id": "p1", "timestamp": _ago_iso(31)}]
        )
        sensor = _patched_hass(
            BoschPersonDetectedBinarySensor(coord, CAM_ID, _make_entry(30))
        )
        assert sensor.is_on is False


class TestConstValues:
    def test_default_is_90(self) -> None:
        from custom_components.bosch_shc_camera.const import (
            DEFAULT_MOTION_ACTIVE_WINDOW,
        )

        assert DEFAULT_MOTION_ACTIVE_WINDOW == 90

    def test_min_is_10(self) -> None:
        from custom_components.bosch_shc_camera.const import MOTION_ACTIVE_WINDOW_MIN

        assert MOTION_ACTIVE_WINDOW_MIN == 10

    def test_max_is_300(self) -> None:
        from custom_components.bosch_shc_camera.const import MOTION_ACTIVE_WINDOW_MAX

        assert MOTION_ACTIVE_WINDOW_MAX == 300

    def test_default_in_default_options(self) -> None:
        """DEFAULT_OPTIONS must carry the motion_active_window key with correct default."""
        from custom_components.bosch_shc_camera.const import (
            DEFAULT_MOTION_ACTIVE_WINDOW,
            DEFAULT_OPTIONS,
        )

        assert "motion_active_window" in DEFAULT_OPTIONS
        assert DEFAULT_OPTIONS["motion_active_window"] == DEFAULT_MOTION_ACTIVE_WINDOW

    def test_default_within_bounds(self) -> None:
        from custom_components.bosch_shc_camera.const import (
            DEFAULT_MOTION_ACTIVE_WINDOW,
            MOTION_ACTIVE_WINDOW_MAX,
            MOTION_ACTIVE_WINDOW_MIN,
        )

        assert (
            MOTION_ACTIVE_WINDOW_MIN
            <= DEFAULT_MOTION_ACTIVE_WINDOW
            <= MOTION_ACTIVE_WINDOW_MAX
        )


class TestConfigFlowWiring:
    def test_motion_active_window_in_features_section(self) -> None:
        """motion_active_window must be listed in the 'features' OPTIONS_SECTIONS key."""
        from custom_components.bosch_shc_camera.config_flow import OPTIONS_SECTIONS

        assert "motion_active_window" in OPTIONS_SECTIONS["features"]

    def test_flatten_sections_handles_motion_active_window(self) -> None:
        """_flatten_sections correctly unwraps motion_active_window from features section."""
        from custom_components.bosch_shc_camera.config_flow import _flatten_sections

        # Build a minimal sectioned user_input that includes motion_active_window
        user_input = {
            "features": {"motion_active_window": 120, "enable_binary_sensors": True},
        }
        flat = _flatten_sections(user_input)
        assert flat["motion_active_window"] == 120


# Doubled entity-name-prefix regression (relocated from tests/test_doubled_prefix_light_binary_sensor.py; light.py half lives in tests/test_light.py) — has_entity_name=True plus a "Bosch <title>"-prefixed _attr_name produced doubled entity_ids like binary_sensor.bosch_est_bosch_est_motion.


@pytest.fixture
def stub_coord_bs() -> SimpleNamespace:
    """Minimal coordinator for the doubled-prefix binary-sensor tests."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                    "featureSupport": {"sound": True},
                },
                "events": [],
            }
        },
    )


def _no_doubled_prefix(entity) -> bool:
    """Return True when _attr_name is None or does not start with 'Bosch '."""
    name = getattr(entity, "_attr_name", None)
    return name is None or not name.startswith("Bosch ")


def _has_entity_name(entity) -> bool:
    """Resolve _attr_has_entity_name through the MRO."""
    for cls in type(entity).__mro__:
        if "_attr_has_entity_name" in cls.__dict__:
            return bool(cls.__dict__["_attr_has_entity_name"])
    return bool(getattr(entity, "_attr_has_entity_name", False))


class TestMotionBinarySensorPrefix:
    """binary_sensor.py BoschMotionBinarySensor"""

    def test_name_no_doubled_prefix(
        self, stub_coord_bs: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        entity = BoschMotionBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(
        self, stub_coord_bs: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        entity = BoschMotionBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


class TestAudioAlarmBinarySensorPrefix:
    """binary_sensor.py BoschAudioAlarmBinarySensor"""

    def test_name_no_doubled_prefix(
        self, stub_coord_bs: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        entity = BoschAudioAlarmBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(
        self, stub_coord_bs: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )

        entity = BoschAudioAlarmBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


class TestPersonDetectedBinarySensorPrefix:
    """binary_sensor.py BoschPersonDetectedBinarySensor"""

    def test_name_no_doubled_prefix(
        self, stub_coord_bs: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        entity = BoschPersonDetectedBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _no_doubled_prefix(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(
        self, stub_coord_bs: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )

        entity = BoschPersonDetectedBinarySensor(stub_coord_bs, CAM_ID, stub_entry)
        assert _has_entity_name(entity)


# Bosch event-timestamp offset regression, GitHub issue #34 (relocated from tests/test_event_timestamp_offset.py; time_utils.py parser tests live in tests/test_time_utils.py, sensor.py "today" bucket tests in tests/test_sensor.py) — the motion active-window instant must not appear shifted into the future.


class TestMotionWindowOffset:
    """The motion window must compute true age from the offset instant.

    With the pre-fix truncation, an event whose local reading is "now" but
    is in fact hours old (or shifted by a timezone offset) appeared in the
    future — the `age <= window` check stayed satisfied → motion stuck on.
    """

    def _offset_iso(self, *, minutes_ago: int) -> str:
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        berlin = ZoneInfo("Europe/Berlin")
        ts = datetime.now(berlin) - timedelta(minutes=minutes_ago)
        return ts.isoformat() + "[Europe/Berlin]"

    def test_stale_offset_event_is_off(self) -> None:
        """Event 5 min old (default window 90s) must be OFF, not stuck on."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        coord = SimpleNamespace(
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
                    "events": [
                        {
                            "eventType": "MOVEMENT",
                            "id": "e1",
                            "timestamp": self._offset_iso(minutes_ago=5),
                        }
                    ],
                }
            },
        )
        entry = SimpleNamespace(entry_id="ENTRY01", data={}, options={})
        s = BoschMotionBinarySensor(coord, CAM_ID, entry)
        assert s.is_on is False

    def test_fresh_offset_event_is_on(self) -> None:
        """A just-now event (offset format) must be ON."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        coord = SimpleNamespace(
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
                    "events": [
                        {
                            "eventType": "MOVEMENT",
                            "id": "e1",
                            "timestamp": self._offset_iso(minutes_ago=0),
                        }
                    ],
                }
            },
        )
        entry = SimpleNamespace(entry_id="ENTRY01", data={}, options={})
        s = BoschMotionBinarySensor(coord, CAM_ID, entry)
        assert s.is_on is True


# simon42-forum issue #5/#6 — binary sensor missing/inconsistent motion events (relocated from tests/test_forum_issues.py; __init__.py polling-seeds-last_event_ids part of the same issue lives in tests/test_init.py).


class TestForumIssueBinarySensorMissesEvents:
    """geotie (simon42 forum) — the automation using the motion binary
    sensor 'funktioniert, wird aber oft nicht ausgeloest'. `EVENT_ACTIVE_WINDOW`
    must stay >= 90s to cover the worst-case 60s polling lag, or a genuinely
    fresh event can be missed."""

    def _make_hass(self):
        fake_hass = MagicMock()
        fake_hass.config.time_zone = "UTC"
        return fake_hass

    def test_window_covers_60s_polling_lag(self):
        """Window must cover the 60s scan_interval + margin; lowering below
        90s reintroduces the missed-trigger bug."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            EVENT_ACTIVE_WINDOW,
        )

        assert EVENT_ACTIVE_WINDOW >= 90

    def test_motion_sensor_fires_for_60s_old_event(self):
        """A 60s-old event (max polling lag) still triggers — the polling
        path can be that lagged."""
        from datetime import UTC, datetime, timedelta

        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                        "macAddress": "x",
                    },
                    "events": [
                        {
                            "eventType": "MOVEMENT",
                            "id": "e1",
                            "timestamp": (
                                datetime.now(UTC) - timedelta(seconds=60)
                            ).strftime("%Y-%m-%dT%H:%M:%S"),
                        }
                    ],
                }
            }
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        s = BoschMotionBinarySensor(coord, CAM_ID, entry)
        s.hass = self._make_hass()
        assert s.is_on is True


# Issue #36 — Gen2 DualRadar reports a human as eventType=MOVEMENT + eventTags=["PERSON"]; the Person sensor (matching only eventType=="PERSON") stayed OFF. Relocated from tests/test_issue36_fcm_delivery_and_person.py (that file's fcm.py content was already consolidated into tests/test_fcm.py by an earlier merge pass).


def _ago_iso_person(seconds: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _make_person_sensor(events: list[dict]):
    from custom_components.bosch_shc_camera.binary_sensor import (
        BoschPersonDetectedBinarySensor,
    )

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Eingang",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.102",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {},
                },
                "events": events,
            }
        },
    )
    entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
    return BoschPersonDetectedBinarySensor(coord, CAM_ID, entry)


class TestPersonSensorTagUpgrade:
    def test_gen2_movement_with_person_tag_is_on(self) -> None:
        """Gen2 sends MOVEMENT+eventTags=[PERSON]; Person sensor must be ON."""
        sensor = _make_person_sensor(
            [
                {
                    "id": "e1",
                    "eventType": "MOVEMENT",
                    "eventTags": ["PERSON"],
                    "timestamp": _ago_iso_person(5),
                }
            ]
        )
        assert sensor.is_on is True

    def test_explicit_person_event_is_on(self) -> None:
        sensor = _make_person_sensor(
            [{"id": "e1", "eventType": "PERSON", "timestamp": _ago_iso_person(5)}]
        )
        assert sensor.is_on is True

    def test_movement_without_person_tag_is_off(self) -> None:
        """A plain MOVEMENT (no PERSON tag) must NOT flip the Person sensor."""
        sensor = _make_person_sensor(
            [
                {
                    "id": "e1",
                    "eventType": "MOVEMENT",
                    "eventTags": ["ANIMAL"],
                    "timestamp": _ago_iso_person(5),
                }
            ]
        )
        assert sensor.is_on is False

    def test_person_tagged_movement_outside_window_is_off(self) -> None:
        sensor = _make_person_sensor(
            [
                {
                    "id": "e1",
                    "eventType": "MOVEMENT",
                    "eventTags": ["PERSON"],
                    "timestamp": _ago_iso_person(10_000),
                }
            ]
        )
        assert sensor.is_on is False

    def test_person_attrs_use_tagged_event(self) -> None:
        sensor = _make_person_sensor(
            [
                {
                    "id": "evt-person",
                    "eventType": "MOVEMENT",
                    "eventTags": ["PERSON"],
                    "timestamp": _ago_iso_person(5),
                }
            ]
        )
        assert sensor.extra_state_attributes.get("event_id") == "evt-person"

    def test_no_events_is_off(self) -> None:
        sensor = _make_person_sensor([])
        assert sensor.is_on is False


# BoschAiRecentAlertBinarySensor — ON for AI_RECENT_ALERT_WINDOW_MINUTES after
# the most recent AI Camera Analysis alert. Source of truth:
# coordinator.ai_analysis_recent[cam_id] (list of (generated_at_iso, score)).


def _make_ai_alert_coord(entries: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"sound": True},
                },
                "events": [],
            }
        },
        ai_analysis_recent={CAM_ID: entries or []},
    )


def _make_ai_alert_sensor(
    coord: SimpleNamespace | None = None,
    entries: list | None = None,
) -> Any:
    from custom_components.bosch_shc_camera.binary_sensor import (
        BoschAiRecentAlertBinarySensor,
    )

    if coord is None:
        coord = _make_ai_alert_coord(entries)
    entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
    return BoschAiRecentAlertBinarySensor(coord, CAM_ID, entry)


class TestAiRecentAlertBinarySensor:
    def test_unique_id_and_translation_key(self) -> None:
        sensor = _make_ai_alert_sensor()
        assert sensor.unique_id == f"bosch_shc_camera_{CAM_ID}_ai_recent_alert"
        assert sensor._attr_translation_key == "ai_recent_alert"

    def test_is_off_when_no_alert_ever(self) -> None:
        sensor = _make_ai_alert_sensor(entries=[])
        assert sensor.is_on is False

    def test_is_on_within_window(self) -> None:
        recent = _ago_iso(60)  # 1 minute ago, well within the 10-minute window
        sensor = _make_ai_alert_sensor(entries=[(recent, 7)])
        assert sensor.is_on is True

    def test_is_off_outside_window(self) -> None:
        from custom_components.bosch_shc_camera.binary_sensor import (
            AI_RECENT_ALERT_WINDOW_MINUTES,
        )

        stale = _ago_iso((AI_RECENT_ALERT_WINDOW_MINUTES + 1) * 60)
        sensor = _make_ai_alert_sensor(entries=[(stale, 7)])
        assert sensor.is_on is False

    def test_is_on_right_at_window_boundary(self) -> None:
        from custom_components.bosch_shc_camera.binary_sensor import (
            AI_RECENT_ALERT_WINDOW_MINUTES,
        )

        # 1 second inside the boundary — must still be ON.
        just_inside = _ago_iso(AI_RECENT_ALERT_WINDOW_MINUTES * 60 - 1)
        sensor = _make_ai_alert_sensor(entries=[(just_inside, 3)])
        assert sensor.is_on is True

    def test_uses_latest_of_multiple_entries(self) -> None:
        old = _ago_iso(3600)
        recent = _ago_iso(30)
        sensor = _make_ai_alert_sensor(entries=[(old, 1), (recent, 9)])
        assert sensor.is_on is True
        assert sensor.extra_state_attributes["last_score"] == 9

    def test_is_off_on_garbage_timestamp(self) -> None:
        sensor = _make_ai_alert_sensor(entries=[("not-a-timestamp", 5)])
        assert sensor.is_on is False

    def test_available_always_true(self) -> None:
        sensor = _make_ai_alert_sensor(entries=[])
        assert sensor.available is True

    def test_extra_state_attributes_empty_when_no_alert(self) -> None:
        sensor = _make_ai_alert_sensor(entries=[])
        assert sensor.extra_state_attributes == {}

    def test_extra_state_attributes_shape_when_alert_present(self) -> None:
        recent = _ago_iso(15)
        sensor = _make_ai_alert_sensor(entries=[(recent, 4)])
        attrs = sensor.extra_state_attributes
        assert attrs["last_score"] == 4
        assert attrs["generated_at"] == recent
        assert "seconds_since_last_alert" in attrs
        assert attrs["seconds_since_last_alert"] >= 15

    def test_extra_state_attributes_omits_seconds_on_garbage_timestamp(self) -> None:
        """generated_at survives even if it can't be parsed into a duration."""
        sensor = _make_ai_alert_sensor(entries=[("garbage", 2)])
        attrs = sensor.extra_state_attributes
        assert attrs["generated_at"] == "garbage"
        assert "seconds_since_last_alert" not in attrs

    def test_unrecorded_attributes_excludes_churning_field(self) -> None:
        """v14.3.1 recorder-DB-bloat discipline (HA#39): seconds_since_last_alert
        changes every coordinator tick while on/off stays put, so it MUST be
        excluded from the recorder via `_unrecorded_attributes` — verified
        directly on the class, not merely assumed from the docstring."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAiRecentAlertBinarySensor,
        )

        assert "seconds_since_last_alert" in (
            BoschAiRecentAlertBinarySensor._unrecorded_attributes
        )
        # last_score/generated_at only change when a NEW alert lands (same
        # cadence as on/off itself) — they are safe to record and must NOT
        # be excluded, otherwise history for the alert content is lost.
        assert "last_score" not in BoschAiRecentAlertBinarySensor._unrecorded_attributes
        assert (
            "generated_at" not in BoschAiRecentAlertBinarySensor._unrecorded_attributes
        )


class TestAiRecentAlertBinarySensorSetupGating:
    """Wired into async_setup_entry only when CONF_AI_ANALYSIS_ENABLED is set."""

    @pytest.mark.asyncio
    async def test_entity_created_when_ai_analysis_enabled(self) -> None:
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAiRecentAlertBinarySensor,
            async_setup_entry,
        )
        from custom_components.bosch_shc_camera.const import CONF_AI_ANALYSIS_ENABLED

        coord = _make_coord()
        entry = SimpleNamespace(
            entry_id="01ENTRY",
            data={},
            options={CONF_AI_ANALYSIS_ENABLED: True},
            runtime_data=coord,
            async_on_unload=MagicMock(),
        )
        added: list[Any] = []
        with patch(
            "custom_components.bosch_shc_camera.binary_sensor.get_options",
            return_value={CONF_AI_ANALYSIS_ENABLED: True},
        ):
            await async_setup_entry(
                MagicMock(), entry, lambda ents, **kw: added.extend(ents)
            )
        assert any(isinstance(e, BoschAiRecentAlertBinarySensor) for e in added)

    @pytest.mark.asyncio
    async def test_entity_not_created_when_ai_analysis_disabled(self) -> None:
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAiRecentAlertBinarySensor,
            async_setup_entry,
        )
        from custom_components.bosch_shc_camera.const import CONF_AI_ANALYSIS_ENABLED

        coord = _make_coord()
        entry = SimpleNamespace(
            entry_id="01ENTRY",
            data={},
            options={CONF_AI_ANALYSIS_ENABLED: False},
            runtime_data=coord,
            async_on_unload=MagicMock(),
        )
        added: list[Any] = []
        with patch(
            "custom_components.bosch_shc_camera.binary_sensor.get_options",
            return_value={CONF_AI_ANALYSIS_ENABLED: False},
        ):
            await async_setup_entry(
                MagicMock(), entry, lambda ents, **kw: added.extend(ents)
            )
        assert not any(isinstance(e, BoschAiRecentAlertBinarySensor) for e in added)
