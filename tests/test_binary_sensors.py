"""Tests for binary_sensor.py — motion / audio_alarm / person event sensors.

Each sensor reads the most-recent event of its type from
`coordinator.data[cam_id]["events"]` and is ON only if that event's
timestamp is within EVENT_ACTIVE_WINDOW seconds of now.

The 90-second window covers the polling-only fallback (60s scan_interval
plus margin); shorter windows would systematically miss events.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                    "featureSupport": {"sound": True},
                },
                "events": [],
            }
        },
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _ago_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _patched_hass(entity):
    """Bind a fake hass.config.time_zone for `_event_within_window`."""
    fake_hass = MagicMock()
    fake_hass.config.time_zone = "UTC"
    entity.hass = fake_hass
    return entity


# ── BoschMotionBinarySensor ─────────────────────────────────────────────


class TestMotionBinarySensor:
    def test_off_when_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_on_with_recent_movement(self, stub_coord, stub_entry):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is True

    def test_off_with_old_movement(self, stub_coord, stub_entry):
        """An event older than 90s is outside the active window."""
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "id": "e1", "timestamp": _ago_iso(120)},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_off_when_only_audio_event(self, stub_coord, stub_entry):
        """A recent AUDIO_ALARM must NOT trigger the motion sensor."""
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "AUDIO_ALARM", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_attrs_include_event_metadata(self, stub_coord, stub_entry):
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
        assert attrs["image_url"] == "https://..."

    def test_attrs_empty_when_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.extra_state_attributes == {}

    def test_disabled_by_default(self, stub_coord, stub_entry):
        """Binary sensors are hidden until user enables — avoids UI clutter."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        assert s._attr_entity_registry_enabled_default is False


# ── BoschAudioAlarmBinarySensor ─────────────────────────────────────────


class TestAudioAlarmBinarySensor:
    def test_off_when_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )
        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_on_with_recent_audio_alarm(self, stub_coord, stub_entry):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "AUDIO_ALARM", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )
        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is True

    def test_attrs_include_audio_event_metadata(self, stub_coord, stub_entry):
        """extra_state_attributes returns event_id/timestamp/image_url when event present."""
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "AUDIO_ALARM", "id": "aud-99", "timestamp": _now_iso(), "imageUrl": "http://img"},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import BoschAudioAlarmBinarySensor
        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        attrs = s.extra_state_attributes
        assert attrs["event_id"] == "aud-99"
        assert attrs["image_url"] == "http://img"

    def test_attrs_empty_when_no_audio_event(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import BoschAudioAlarmBinarySensor
        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.extra_state_attributes == {}

    def test_off_with_only_movement_event(self, stub_coord, stub_entry):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschAudioAlarmBinarySensor,
        )
        s = _patched_hass(BoschAudioAlarmBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False


# ── BoschPersonDetectedBinarySensor ─────────────────────────────────────


class TestPersonDetectedBinarySensor:
    def test_off_when_no_events(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )
        s = _patched_hass(BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is False

    def test_on_with_recent_person_event(self, stub_coord, stub_entry):
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "PERSON", "id": "e1", "timestamp": _now_iso()},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )
        s = _patched_hass(BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.is_on is True

    def test_unique_id_includes_cam_id(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschPersonDetectedBinarySensor,
        )
        s = BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry)
        assert CAM_ID in s._attr_unique_id

    def test_attrs_include_person_event_metadata(self, stub_coord, stub_entry):
        """extra_state_attributes returns event_id/timestamp/image_url when PERSON event present."""
        stub_coord.data[CAM_ID]["events"] = [
            {"eventType": "PERSON", "id": "per-77", "timestamp": _now_iso(), "imageUrl": "http://pic"},
        ]
        from custom_components.bosch_shc_camera.binary_sensor import BoschPersonDetectedBinarySensor
        s = _patched_hass(BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry))
        attrs = s.extra_state_attributes
        assert attrs["event_id"] == "per-77"
        assert attrs["image_url"] == "http://pic"

    def test_attrs_empty_when_no_person_event(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import BoschPersonDetectedBinarySensor
        s = _patched_hass(BoschPersonDetectedBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s.extra_state_attributes == {}


# ── _event_within_window edge cases ─────────────────────────────────────


class TestEventWindow:
    def test_empty_timestamp_returns_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s._event_within_window({}) is False
        assert s._event_within_window({"timestamp": ""}) is False

    def test_malformed_timestamp_returns_false(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        assert s._event_within_window({"timestamp": "not-iso8601"}) is False

    def test_iso_with_milliseconds_works(self, stub_coord, stub_entry):
        """Bosch API may append `.000Z` — we strip to first 19 chars."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = _patched_hass(BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry))
        ts = _now_iso() + ".000Z"
        assert s._event_within_window({"timestamp": ts}) is True

    def test_utc_event_in_berlin_timezone_fires(self, stub_coord, stub_entry):
        """Bug #11 regression guard — UTC-Z timestamps must compare correctly
        in non-UTC user timezones.

        Pre-fix: `_event_within_window` stripped the `Z` suffix and replaced
        tzinfo with the user's local timezone (Europe/Berlin in summer = +02:00),
        so a UTC event from 30 s ago appeared as 2h 30s old → outside the
        90 s window → motion sensor never fired in non-UTC timezones.

        This is the timezone-bug component of geotie's forum complaint
        (post #8) 'Die obige Automation funktioniert, wird aber oft nicht
        ausgelöst.' — affected every user in DE/EU.
        """
        from unittest.mock import MagicMock
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

    def test_30s_old_utc_event_in_berlin(self, stub_coord, stub_entry):
        """30s-old event in Berlin TZ must also fire — within 90s window."""
        from unittest.mock import MagicMock
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        fake_hass.config.time_zone = "Europe/Berlin"
        s.hass = fake_hass
        ts_30s_ago = _ago_iso(30) + ".000Z"
        assert s._event_within_window({"timestamp": ts_30s_ago}) is True

    def test_device_info_structure(self, stub_coord, stub_entry):
        """device_info must include DOMAIN identifier and Bosch manufacturer."""
        from custom_components.bosch_shc_camera.binary_sensor import BoschMotionBinarySensor
        from custom_components.bosch_shc_camera import DOMAIN
        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        info = s.device_info
        assert info["manufacturer"] == "Bosch"
        assert (DOMAIN, CAM_ID) in info["identifiers"]

    def test_2hour_old_utc_event_in_berlin_does_not_fire(self, stub_coord, stub_entry):
        """A genuinely-old (2h) event must NOT fire even with the timezone fix.

        Sanity check that the fix didn't accidentally make stale events
        appear fresh. 2h old UTC event = 2h old in any timezone → outside
        90s window → False.
        """
        from unittest.mock import MagicMock
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        s = BoschMotionBinarySensor(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        fake_hass.config.time_zone = "Europe/Berlin"
        s.hass = fake_hass
        ts_old = _ago_iso(7200) + ".000Z"  # 2h ago
        assert s._event_within_window({"timestamp": ts_old}) is False


# ── async_setup_entry ────────────────────────────────────────────────────


class TestSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_motion_and_person_for_no_sound_cam(self):
        """Camera without sound feature → 2 entities (Motion + PersonDetected)."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            async_setup_entry, BoschMotionBinarySensor, BoschPersonDetectedBinarySensor,
        )
        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor",
                        "featureSupport": {"sound": False},
                    },
                    "events": [],
                }
            }
        )
        entry = SimpleNamespace(entry_id="01E", data={}, options={}, runtime_data=coord)
        captured: list = []
        await async_setup_entry(hass=None, config_entry=entry,
                                async_add_entities=lambda e, update_before_add=False: captured.extend(e))
        types_ = {type(e) for e in captured}
        assert BoschMotionBinarySensor in types_
        assert BoschPersonDetectedBinarySensor in types_
        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_creates_audio_sensor_when_sound_supported(self):
        """Camera with sound feature → 3 entities (Motion + Person + AudioAlarm)."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            async_setup_entry, BoschAudioAlarmBinarySensor,
        )
        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Innen", "hardwareVersion": "CAMERA_360",
                        "featureSupport": {"sound": True},
                    },
                    "events": [],
                }
            }
        )
        entry = SimpleNamespace(entry_id="01E", data={}, options={}, runtime_data=coord)
        captured: list = []
        await async_setup_entry(hass=None, config_entry=entry,
                                async_add_entities=lambda e, update_before_add=False: captured.extend(e))
        assert BoschAudioAlarmBinarySensor in {type(e) for e in captured}
        assert len(captured) == 3

    @pytest.mark.asyncio
    async def test_empty_coordinator_yields_no_entities(self):
        from custom_components.bosch_shc_camera.binary_sensor import async_setup_entry
        coord = SimpleNamespace(data={})
        entry = SimpleNamespace(entry_id="01E", data={}, options={}, runtime_data=coord)
        captured: list = []
        await async_setup_entry(hass=None, config_entry=entry,
                                async_add_entities=lambda e, update_before_add=False: captured.extend(e))
        assert captured == []
