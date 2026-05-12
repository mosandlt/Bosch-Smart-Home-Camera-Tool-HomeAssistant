"""Tests for custom_components.bosch_shc_camera.logbook.

Verifies that async_describe_events registers the correct callbacks for every
event type the integration fires, and that each callback returns the expected
LOGBOOK_ENTRY_NAME / LOGBOOK_ENTRY_MESSAGE for both normal and edge-case input.

Events covered
--------------
bosch_shc_camera_motion      — MOVEMENT events from fcm.py:508 / __init__.py:1729
bosch_shc_camera_audio_alarm — AUDIO_ALARM events from fcm.py:510 / __init__.py:1733
bosch_shc_camera_person      — PERSON events from fcm.py:512 / __init__.py:1737
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event

from custom_components.bosch_shc_camera.logbook import (
    EVENT_AUDIO_ALARM,
    EVENT_MOTION,
    EVENT_PERSON,
    async_describe_events,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_event(event_type: str, data: dict[str, Any]) -> Event[dict[str, Any]]:
    """Create a minimal HA Event with the given data dict."""
    ev: Event[dict[str, Any]] = MagicMock(spec=Event)
    ev.event_type = event_type
    ev.data = data
    return ev


def _collect_registrations(
    hass: MagicMock,
) -> dict[tuple[str, str], Any]:
    """Call async_describe_events and return {(domain, event_type): callback}."""
    registrations: dict[tuple[str, str], Any] = {}

    def _register(domain: str, event_type: str, callback: Any) -> None:
        registrations[(domain, event_type)] = callback

    async_describe_events(hass, _register)
    return registrations


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------

class TestRegistration:
    """Verify that all three event types are registered under the correct domain."""

    def setup_method(self) -> None:
        self.hass = MagicMock()
        self.registrations = _collect_registrations(self.hass)

    def test_motion_registered(self) -> None:
        assert ("bosch_shc_camera", EVENT_MOTION) in self.registrations

    def test_audio_alarm_registered(self) -> None:
        assert ("bosch_shc_camera", EVENT_AUDIO_ALARM) in self.registrations

    def test_person_registered(self) -> None:
        assert ("bosch_shc_camera", EVENT_PERSON) in self.registrations

    def test_exactly_three_registrations(self) -> None:
        """No stray event types are registered."""
        assert len(self.registrations) == 3

    def test_all_callbacks_callable(self) -> None:
        for cb in self.registrations.values():
            assert callable(cb)


# ---------------------------------------------------------------------------
# Motion event tests
# ---------------------------------------------------------------------------

class TestMotionDescribe:
    """Tests for the bosch_shc_camera_motion describe callback."""

    def setup_method(self) -> None:
        hass = MagicMock()
        regs = _collect_registrations(hass)
        self.cb = regs[("bosch_shc_camera", EVENT_MOTION)]

    def test_name_contains_camera_name(self) -> None:
        ev = _make_event(EVENT_MOTION, {"camera_name": "Terrasse", "camera_id": "abc"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch Terrasse"

    def test_message_says_motion(self) -> None:
        ev = _make_event(EVENT_MOTION, {"camera_name": "Terrasse", "camera_id": "abc"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_MESSAGE] == "detected motion"

    def test_missing_camera_name_falls_back(self) -> None:
        ev = _make_event(EVENT_MOTION, {"camera_id": "abc"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch unknown camera"

    def test_empty_camera_name_falls_back(self) -> None:
        ev = _make_event(EVENT_MOTION, {"camera_name": "", "camera_id": "abc"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch unknown camera"

    def test_none_camera_name_falls_back(self) -> None:
        ev = _make_event(EVENT_MOTION, {"camera_name": None, "camera_id": "abc"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch unknown camera"

    def test_returns_dict_with_required_keys(self) -> None:
        ev = _make_event(EVENT_MOTION, {"camera_name": "Garten"})
        result = self.cb(ev)
        assert LOGBOOK_ENTRY_NAME in result
        assert LOGBOOK_ENTRY_MESSAGE in result

    def test_extra_payload_fields_are_ignored(self) -> None:
        """Extra fields (timestamp, image_url, source) must not cause errors."""
        ev = _make_event(
            EVENT_MOTION,
            {
                "camera_name": "Innenbereich",
                "camera_id": "xyz",
                "timestamp": "2026-05-12T10:00:00Z",
                "image_url": "https://example.com/img.jpg",
                "event_id": "999",
                "source": "fcm_push",
            },
        )
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch Innenbereich"


# ---------------------------------------------------------------------------
# Audio alarm event tests
# ---------------------------------------------------------------------------

class TestAudioAlarmDescribe:
    """Tests for the bosch_shc_camera_audio_alarm describe callback."""

    def setup_method(self) -> None:
        hass = MagicMock()
        regs = _collect_registrations(hass)
        self.cb = regs[("bosch_shc_camera", EVENT_AUDIO_ALARM)]

    def test_name_contains_camera_name(self) -> None:
        ev = _make_event(EVENT_AUDIO_ALARM, {"camera_name": "Eingang"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch Eingang"

    def test_message_says_audio_alarm(self) -> None:
        ev = _make_event(EVENT_AUDIO_ALARM, {"camera_name": "Eingang"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_MESSAGE] == "detected an audio alarm"

    def test_missing_camera_name_falls_back(self) -> None:
        ev = _make_event(EVENT_AUDIO_ALARM, {})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch unknown camera"

    def test_empty_camera_name_falls_back(self) -> None:
        ev = _make_event(EVENT_AUDIO_ALARM, {"camera_name": ""})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch unknown camera"


# ---------------------------------------------------------------------------
# Person detection event tests
# ---------------------------------------------------------------------------

class TestPersonDescribe:
    """Tests for the bosch_shc_camera_person describe callback."""

    def setup_method(self) -> None:
        hass = MagicMock()
        regs = _collect_registrations(hass)
        self.cb = regs[("bosch_shc_camera", EVENT_PERSON)]

    def test_name_contains_camera_name(self) -> None:
        ev = _make_event(EVENT_PERSON, {"camera_name": "Kamera 360"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch Kamera 360"

    def test_message_says_person(self) -> None:
        ev = _make_event(EVENT_PERSON, {"camera_name": "Kamera 360"})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_MESSAGE] == "detected a person"

    def test_missing_camera_name_falls_back(self) -> None:
        ev = _make_event(EVENT_PERSON, {})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch unknown camera"

    def test_empty_camera_name_falls_back(self) -> None:
        ev = _make_event(EVENT_PERSON, {"camera_name": ""})
        result = self.cb(ev)
        assert result[LOGBOOK_ENTRY_NAME] == "Bosch unknown camera"

    def test_returns_only_string_values(self) -> None:
        """HA logbook requires dict[str, str] — all values must be str."""
        ev = _make_event(EVENT_PERSON, {"camera_name": "Terrasse"})
        result = self.cb(ev)
        for v in result.values():
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Event constant tests
# ---------------------------------------------------------------------------

class TestEventConstants:
    """Verify the exported event-type string constants match what integration fires."""

    def test_motion_constant(self) -> None:
        assert EVENT_MOTION == "bosch_shc_camera_motion"

    def test_audio_alarm_constant(self) -> None:
        assert EVENT_AUDIO_ALARM == "bosch_shc_camera_audio_alarm"

    def test_person_constant(self) -> None:
        assert EVENT_PERSON == "bosch_shc_camera_person"
