"""Regression test for Bug #1 (2026-05-26): `bosch_shc_camera_intrusion` event
was registered as webhook target + listed in services.yaml selector, but NO code
path ever fired it. Real intrusion events from the Bosch alarm system silently
skipped webhook delivery.

Fix: rising-edge detection on `alarmStatus.alarmType` (NONE → non-NONE) fires
`bosch_shc_camera_intrusion` once per transition. Falling edge, identical
repeats, and missing payloads must NOT fire.

Pin-tests cover every transition (PIN_EVERY_MODE).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

CAM_A = "DEAD-BEEF-0001-AAAA"
CAM_B = "DEAD-BEEF-0002-BBBB"


def _make_coordinator() -> BoschCameraCoordinator:
    """Minimal coordinator instance with the firing-helper accessible."""
    coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
    coord.hass = MagicMock()
    coord.hass.bus = MagicMock()
    coord.hass.bus.async_fire = MagicMock()
    coord._last_alarm_type = {}
    return coord


# ─────────────────────────────────────────────────────────────────────────────
# Pin-tests for every transition
# ─────────────────────────────────────────────────────────────────────────────


class TestIntrusionEventFire:
    def test_rising_edge_none_to_alarm_fires_event(self) -> None:
        """alarmType "NONE" → "INTRUSION_DETECTED" must fire the event once."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "NONE"
        status = {"alarmType": "INTRUSION_DETECTED", "intrusionSystem": "ACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_called_once()
        evt_name, payload = coord.hass.bus.async_fire.call_args[0]
        assert evt_name == "bosch_shc_camera_intrusion"
        assert payload["camera_id"] == CAM_A
        assert payload["camera_name"] == "Terrasse"
        assert payload["alarm_type"] == "INTRUSION_DETECTED"
        assert payload["intrusion_system"] == "ACTIVE"
        assert coord._last_alarm_type[CAM_A] == "INTRUSION_DETECTED"

    def test_first_observation_non_none_fires(self) -> None:
        """First-ever alarmStatus seen is non-NONE — must fire (alarm already
        triggered while HA was down; user still needs to know)."""
        coord = _make_coordinator()
        # _last_alarm_type[CAM_A] is not set yet.
        status = {"alarmType": "AUDIO_INTRUSION", "intrusionSystem": "ACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_called_once()
        assert coord._last_alarm_type[CAM_A] == "AUDIO_INTRUSION"

    def test_no_fire_when_alarmtype_stays_none(self) -> None:
        """alarmType remains "NONE" — must NOT fire."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "NONE"
        status = {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_not_called()

    def test_no_fire_on_first_observation_when_none(self) -> None:
        """First-ever observation of alarmStatus is "NONE" — must NOT fire."""
        coord = _make_coordinator()
        status = {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_not_called()
        assert coord._last_alarm_type[CAM_A] == "NONE"

    def test_no_fire_when_alarmtype_unchanged_non_none(self) -> None:
        """alarmType stays "INTRUSION_DETECTED" — must NOT double-fire."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "INTRUSION_DETECTED"
        status = {"alarmType": "INTRUSION_DETECTED", "intrusionSystem": "ACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_not_called()

    def test_no_fire_on_falling_edge(self) -> None:
        """alarmType "INTRUSION_DETECTED" → "NONE" (alarm cleared) — must NOT
        fire intrusion event (only rising edge fires)."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "INTRUSION_DETECTED"
        status = {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_not_called()
        assert coord._last_alarm_type[CAM_A] == "NONE"

    def test_re_fire_after_reset(self) -> None:
        """After falling edge resets to NONE, the next rising edge MUST fire again."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "NONE"

        # First intrusion
        coord._maybe_fire_intrusion_event(
            CAM_A,
            "Terrasse",
            {"alarmType": "INTRUSION_DETECTED", "intrusionSystem": "ACTIVE"},
        )
        # Cleared
        coord._maybe_fire_intrusion_event(
            CAM_A,
            "Terrasse",
            {"alarmType": "NONE", "intrusionSystem": "INACTIVE"},
        )
        # Second intrusion
        coord._maybe_fire_intrusion_event(
            CAM_A,
            "Terrasse",
            {"alarmType": "AUDIO_INTRUSION", "intrusionSystem": "ACTIVE"},
        )

        assert coord.hass.bus.async_fire.call_count == 2

    def test_no_fire_when_status_empty(self) -> None:
        """Empty alarmStatus dict — must NOT fire, must NOT crash."""
        coord = _make_coordinator()
        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", {})

        coord.hass.bus.async_fire.assert_not_called()
        # No tracking entry created for empty status.
        assert CAM_A not in coord._last_alarm_type

    def test_no_fire_when_alarmtype_missing(self) -> None:
        """alarmStatus has intrusionSystem but no alarmType — must NOT fire."""
        coord = _make_coordinator()
        status = {"intrusionSystem": "ACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_not_called()

    def test_independent_per_camera(self) -> None:
        """Cam A rising-edge fires, Cam B unchanged — Cam B must not fire."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "NONE"
        coord._last_alarm_type[CAM_B] = "NONE"

        coord._maybe_fire_intrusion_event(
            CAM_A,
            "Terrasse",
            {"alarmType": "INTRUSION_DETECTED", "intrusionSystem": "ACTIVE"},
        )

        coord.hass.bus.async_fire.assert_called_once()
        assert coord._last_alarm_type[CAM_A] == "INTRUSION_DETECTED"
        assert coord._last_alarm_type[CAM_B] == "NONE"

    def test_alarmtype_treated_case_insensitive_upper(self) -> None:
        """Bosch returns uppercase strings — make sure comparison is robust to
        a stray lowercase response (defensive)."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "NONE"
        # lowercase "none" would be a weird response, but treat semantically
        status = {"alarmType": "none", "intrusionSystem": "INACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_not_called()

    def test_empty_alarmtype_treated_as_none(self) -> None:
        """alarmType is "" — same semantic as NONE, must NOT fire."""
        coord = _make_coordinator()
        coord._last_alarm_type[CAM_A] = "NONE"
        status = {"alarmType": "", "intrusionSystem": "INACTIVE"}

        coord._maybe_fire_intrusion_event(CAM_A, "Terrasse", status)

        coord.hass.bus.async_fire.assert_not_called()
