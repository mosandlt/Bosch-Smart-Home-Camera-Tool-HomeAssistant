"""Tests for BoschNvrStateSensor — Mini-NVR diagnostic sensor.

Pins the four attributes the sensor surfaces (``target``, ``pending_uploads``,
``failed_uploads``, ``last_segment_age_s``) and the three states
(``idle`` / ``recording`` / ``error``). Pure-property tests — no I/O, no
event loop — so they cannot regress under refactor.

User/forum source: project-internal v11.0.4 NVR-storage-target refactor —
Thomas asked for a "is recording actually working" diagnostic sensor.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _make_coord(
    *,
    drain_state: dict | None = None,
    nvr_processes: dict | None = None,
    user_intent: dict | None = None,
    error_state: dict | None = None,
    title: str = "Terrasse",
):
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": title, "hardwareVersion": "HOME_Eyes_Outdoor",
                                  "firmwareVersion": "9.40.25", "macAddress": ""}}},
        _nvr_drain_state=drain_state or {},
        _nvr_processes=nvr_processes or {},
        _nvr_user_intent=user_intent or {},
        _nvr_error_state=error_state or {},
    )


def _make_entry():
    return SimpleNamespace(entry_id="01TEST", data={}, options={})


# ── State machine ────────────────────────────────────────────────────────────


class TestNvrStateSensorState:
    def test_idle_when_no_process(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(_make_coord(), CAM_ID, _make_entry())
        assert s.native_value == "idle"

    def test_idle_when_process_but_no_user_intent(self):
        """Edge case — process is running, but user toggled off and we're
        between switch-tick and stop. Still ``idle`` so the dashboard
        doesn't lie."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(
                nvr_processes={CAM_ID: object()},
                user_intent={CAM_ID: False},
            ), CAM_ID, _make_entry(),
        )
        assert s.native_value == "idle"

    def test_recording_when_process_and_user_intent(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(
                nvr_processes={CAM_ID: object()},
                user_intent={CAM_ID: True},
            ), CAM_ID, _make_entry(),
        )
        assert s.native_value == "recording"

    def test_error_takes_precedence(self):
        """If the crash-loop guard tripped, ``error`` overrides everything
        else — including a running process — so the user notices."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(
                nvr_processes={CAM_ID: object()},
                user_intent={CAM_ID: True},
                error_state={CAM_ID: "ffmpeg crashed twice"},
            ), CAM_ID, _make_entry(),
        )
        assert s.native_value == "error"


# ── Attributes ───────────────────────────────────────────────────────────────


class TestNvrStateSensorAttributes:
    def test_target_attribute(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(drain_state={"target": "smb"}),
            CAM_ID, _make_entry(),
        )
        assert s.extra_state_attributes["target"] == "smb"

    def test_target_default_local_when_state_empty(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(_make_coord(), CAM_ID, _make_entry())
        assert s.extra_state_attributes["target"] == "local"

    def test_pending_and_failed_counts(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(drain_state={"pending": 4, "failed": 2}),
            CAM_ID, _make_entry(),
        )
        attrs = s.extra_state_attributes
        assert attrs["pending_uploads"] == 4
        assert attrs["failed_uploads"] == 2

    def test_last_segment_age_keyed_by_camera(self):
        """``_nvr_drain_state.last_age_by_cam`` is keyed by sanitized
        camera title so the per-camera lookup must use the same _safe_name."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(
                title="Terrasse",
                drain_state={"last_age_by_cam": {"Terrasse": 42.5}},
            ),
            CAM_ID, _make_entry(),
        )
        assert s.extra_state_attributes["last_segment_age_s"] == 42.5

    def test_last_segment_age_none_when_unknown(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(_make_coord(), CAM_ID, _make_entry())
        assert s.extra_state_attributes["last_segment_age_s"] is None

    def test_user_intent_exposed(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(user_intent={CAM_ID: True}),
            CAM_ID, _make_entry(),
        )
        assert s.extra_state_attributes["user_intent"] is True

    def test_error_attribute_exposed(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(
            _make_coord(error_state={CAM_ID: "ffmpeg crashed twice"}),
            CAM_ID, _make_entry(),
        )
        assert s.extra_state_attributes["error"] == "ffmpeg crashed twice"

    def test_camera_name_with_special_chars_sanitized(self):
        """A camera title with ``/`` or ``..`` must be _safe_name'd before
        looking up the per-camera age — same key the watcher writes."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        # `_safe_name("../../etc")` collapses to `_______etc` (one component).
        from custom_components.bosch_shc_camera.smb import _safe_name
        sanitized = _safe_name("../../etc")
        s = BoschNvrStateSensor(
            _make_coord(
                title="../../etc",
                drain_state={"last_age_by_cam": {sanitized: 99.0}},
            ),
            CAM_ID, _make_entry(),
        )
        assert s.extra_state_attributes["last_segment_age_s"] == 99.0


# ── Entity metadata ──────────────────────────────────────────────────────────


class TestNvrStateSensorMetadata:
    def test_unique_id_pinned(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(_make_coord(), CAM_ID, _make_entry())
        assert s.unique_id == f"bosch_shc_nvr_state_{CAM_ID.lower()}"

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(_make_coord(), CAM_ID, _make_entry())
        assert s.translation_key == "nvr_state"

    def test_disabled_by_default(self):
        """Diagnostic sensor — opt-in only, never adds noise on first run."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor
        s = BoschNvrStateSensor(_make_coord(), CAM_ID, _make_entry())
        assert s.entity_registry_enabled_default is False
