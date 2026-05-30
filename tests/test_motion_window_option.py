"""Regression tests for configurable motion active-window.

Bug source: community.home-assistant.io/t/998974 — hardcoded 90 s window for
binary_sensor.bosch_<cam>_motion; users with slow automations or frequent
polling gaps requested a longer window.

Contract:
  - `motion_active_window` option (range 10-300 s, default 90 s) controls how
    long motion/audio/person binary sensors stay ON after an event.
  - Sensor reads from `entry.options["motion_active_window"]`; falls back to
    DEFAULT_MOTION_ACTIVE_WINDOW (90) when key is absent.
  - Out-of-range values are clamped: <10 → 10, >300 → 300.
  - Non-integer values fall back to default.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(events: list | None = None) -> SimpleNamespace:
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
    )


def _make_entry(motion_active_window: object = None) -> SimpleNamespace:
    opts: dict = {}
    if motion_active_window is not None:
        opts["motion_active_window"] = motion_active_window
    return SimpleNamespace(entry_id="01ENTRY", data={}, options=opts)


def _ago_iso(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


def _patched_hass(entity: object) -> object:
    fake_hass = MagicMock()
    fake_hass.config.time_zone = "UTC"
    entity.hass = fake_hass  # type: ignore[attr-defined]
    return entity


# ── _motion_active_window property ─────────────────────────────────────────


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


# ── is_on behaviour with different window values ────────────────────────────


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


# ── All three sensor types respect the window ───────────────────────────────


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


# ── const.py values ─────────────────────────────────────────────────────────


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


# ── config_flow OPTIONS_SECTIONS wiring ─────────────────────────────────────


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
