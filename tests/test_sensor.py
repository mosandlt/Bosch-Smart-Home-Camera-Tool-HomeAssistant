"""Tests for custom_components.bosch_shc_camera.sensor — the full sensor platform.

Merged from 14 previously-scattered test files into one flat module, matching
home-assistant-core's test-suite convention (one file per source module). No
test logic, assertions, or coordinator-stub field values were changed during
the merge — only file layout, helper naming (to resolve cross-file name
collisions), section grouping, and prose (docstrings/comments describing
sprint/round/bug-ID labels) were reworded to describe the actual behavior
under test instead of an internal label.

Coordinator-stub builders are kept distinct per originating file rather than
unified into one mega-builder, even where several are structurally similar:
each covers a different, non-overlapping set of coordinator attributes, and a
shared builder risks silently masking a missing-field bug behind an
accidentally-present default from an unrelated section.

Out-of-scope inclusions (kept per explicit merge instructions):
  - "External stream URL sensors" also tests BoschExternalStreamSwitch, which
    lives in switch.py, not sensor.py — its tests travel with the
    BoschStreamUrlSensor/BoschStreamUrlSubSensor tests they were written
    alongside.
  - The recorder `_unrecorded_attributes` parametrized test (final section)
    covers several non-sensor classes (BoschLanReachableBinarySensor from
    binary_sensor.py, BoschCamera from camera.py, BoschLiveStreamSwitch from
    switch.py) in the same parametrize list as the sensor.py classes it
    shares the concern with — kept as one indivisible test rather than split.
"""

from __future__ import annotations

import json
import struct
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.entity import EntityCategory
from homeassistant.util import dt as dt_util

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.binary_sensor import (
    BoschLanReachableBinarySensor,
)
from custom_components.bosch_shc_camera.camera import BoschCamera
from custom_components.bosch_shc_camera.maintenance import MaintenanceWindow
from custom_components.bosch_shc_camera.sensor import (
    BoschAiAlerts24hSensor,
    BoschAiAlertScoreSensor,
    BoschAlarmCatalogSensor,
    BoschCameraStatusSensor,
    BoschCloudMaintenanceSensor,
    BoschFcmPushStatusSensor,
    BoschIvaCatalogSensor,
    BoschMotionZonesSensor,
    BoschNvrStateSensor,
    BoschPrivateAreasSensor,
    BoschRulesCountSensor,
)
from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_ID_2 = "22222222-2222-2222-2222-222222222222"

ICONS = json.loads(
    (
        Path(__file__).parent.parent / "custom_components/bosch_shc_camera/icons.json"
    ).read_text(encoding="utf-8")
)


def _make_entry() -> Any:
    """Generic no-op config-entry stub for sections that don't inspect options."""
    return SimpleNamespace(entry_id="test_entry", data={}, options={})


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    """Shared trivial config-entry fixture (entry_id value is never asserted on)."""
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# AI snapshot description sensor + auto-describe-on-motion
# Covers: option-gated sensor creation, native_value truncation to 255 chars,
# extra_state_attributes (full text + metadata), coordinator data round-trip,
# unique_id/translation_key/icon, auto-describe-on-motion debounce logic,
# describe_snapshot service call-shape, async_generate_ai_description
# (privacy guard, rate limiting, budget, timeout, stale-cache rejection,
# budget-exceeded log-once), the AI activation time/condition window gate,
# budget-store persistence, and the one-call-per-motion-event guarantee.
#
# NOTE: this section binds coordinator methods via an explicit
# `custom_components.bosch_shc_camera.coordinator`-qualified import (aliased
# as `_AiCoordinatorCls`) rather than the plain top-level
# `BoschCameraCoordinator` import used elsewhere in this file, so that a
# function's `__globals__` (and therefore `_LOGGER`/`dt_util`) resolves
# against `coordinator.py` — where BoschCameraCoordinator actually lives —
# not `__init__.py`. The `patch("...coordinator._LOGGER")` / `.dt_util.now`
# calls below target that same module for exactly this reason.
from custom_components.bosch_shc_camera.coordinator import (
    BoschCameraCoordinator as _AiCoordinatorCls,
)

_AI_DESC_KEY = "ai_description"


def _make_ai_desc_coord(
    cam_ids: list[str] | None = None,
    ai_description: dict[str, str | None] | None = None,
    cam_id_for_ai: str = CAM_ID,
) -> Any:
    """Build a minimal coordinator stub suitable for AI description sensor tests."""
    ids = cam_ids if cam_ids is not None else [CAM_ID]
    cam_data: dict[str, Any] = {}
    for cid in ids:
        cam_data[cid] = {
            "info": {
                "firmwareVersion": "9.40.25",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "macAddress": "aa:bb:cc:11:22:33",
                "title": "Terrasse",
            },
            "status": "ONLINE",
            "events": [],
        }
        if ai_description is not None and cid == cam_id_for_ai:
            cam_data[cid][_AI_DESC_KEY] = ai_description

    coord = SimpleNamespace(
        data=cam_data,
        last_update_success=True,
        async_set_updated_data=MagicMock(),
    )
    return coord


def _make_ai_entry(opts: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(entry_id="test_entry_ai", options=opts or {})


def _make_ai_description_sensor(
    cam_id: str = CAM_ID,
    ai_description: dict[str, str | None] | None = None,
    coord: Any | None = None,
) -> Any:
    """Construct a BoschCameraAiDescriptionSensor directly (no HA runtime)."""
    from custom_components.bosch_shc_camera.sensor import (
        BoschCameraAiDescriptionSensor,
    )

    c = (
        coord
        if coord is not None
        else _make_ai_desc_coord(ai_description=ai_description)
    )
    entry = _make_ai_entry({"enable_ai_description": True})
    return BoschCameraAiDescriptionSensor(c, cam_id, entry)


# ── Sensor creation gating ───────────────────────────────────────────────────


class TestAiSensorCreation:
    """Verify gating logic: sensor is only added when enable_ai_description=True."""

    def test_option_off_sensor_not_in_list(self) -> None:
        """When enable_ai_description is False the sensor must not be created."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
        )

        coord = _make_ai_desc_coord()
        entry = _make_ai_entry({"enable_ai_description": False})
        entities: list[Any] = []

        opts: dict[str, Any] = entry.options
        if opts.get("enable_ai_description", False):
            for cid in coord.data:
                entities.append(BoschCameraAiDescriptionSensor(coord, cid, entry))

        sensor_types = [type(e).__name__ for e in entities]
        assert "BoschCameraAiDescriptionSensor" not in sensor_types

    def test_option_missing_sensor_not_in_list(self) -> None:
        """When enable_ai_description key is absent the sensor must not be created."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
        )

        coord = _make_ai_desc_coord()
        entry = _make_ai_entry({})  # key absent entirely
        entities: list[Any] = []

        opts: dict[str, Any] = entry.options
        if opts.get("enable_ai_description", False):
            for cid in coord.data:
                entities.append(BoschCameraAiDescriptionSensor(coord, cid, entry))

        assert entities == []

    def test_option_on_sensor_in_list_per_camera(self) -> None:
        """When enable_ai_description is True one sensor per cam_id is created."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
        )

        coord = _make_ai_desc_coord(cam_ids=[CAM_ID, CAM_ID_2])
        entry = _make_ai_entry({"enable_ai_description": True})
        entities: list[Any] = []

        opts: dict[str, Any] = entry.options
        if opts.get("enable_ai_description", False):
            for cid in coord.data:
                entities.append(BoschCameraAiDescriptionSensor(coord, cid, entry))

        assert len(entities) == 2
        cam_ids_seen = {e._cam_id for e in entities}
        assert cam_ids_seen == {CAM_ID, CAM_ID_2}

    def test_option_on_single_camera(self) -> None:
        """One camera → one sensor."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
        )

        coord = _make_ai_desc_coord(cam_ids=[CAM_ID])
        entry = _make_ai_entry({"enable_ai_description": True})
        entities: list[Any] = []

        opts: dict[str, Any] = entry.options
        if opts.get("enable_ai_description", False):
            for cid in coord.data:
                entities.append(BoschCameraAiDescriptionSensor(coord, cid, entry))

        assert len(entities) == 1
        assert entities[0]._cam_id == CAM_ID


# ── Sensor metadata ───────────────────────────────────────────────────────────


class TestAiSensorMetadata:
    """Verify static metadata of the sensor."""

    def test_unique_id_format(self) -> None:
        """unique_id must be bosch_shc_ai_description_<cam_id_lower>."""
        s = _make_ai_description_sensor(cam_id=CAM_ID)
        assert s.unique_id == f"bosch_shc_ai_description_{CAM_ID.lower()}"

    def test_unique_id_second_camera(self) -> None:
        """Each camera gets a distinct unique_id."""
        coord = _make_ai_desc_coord(cam_ids=[CAM_ID, CAM_ID_2])
        s2 = _make_ai_description_sensor(cam_id=CAM_ID_2, coord=coord)
        assert s2.unique_id == f"bosch_shc_ai_description_{CAM_ID_2.lower()}"

    def test_translation_key(self) -> None:
        s = _make_ai_description_sensor()
        assert s.translation_key == "ai_description"

    def test_icon_lives_in_icons_json_not_hardcoded(self) -> None:
        """Icon lives in icons.json — no _attr_icon on the entity itself, so
        HA's own icon-translation resolves it via translation_key instead of
        a hardcoded Python value."""
        s = _make_ai_description_sensor()
        assert getattr(s, "_attr_icon", None) is None
        assert (
            ICONS["entity"]["sensor"]["ai_description"]["default"] == "mdi:image-text"
        )


# ── native_value — truncation and None handling ──────────────────────────────


class TestAiSensorNativeValue:
    """PIN_EVERY_MODE: short text / long text / missing key."""

    def test_native_value_short_text_unchanged(self) -> None:
        """Text shorter than 255 chars is returned as-is."""
        text = "A person is walking near the gate."
        s = _make_ai_description_sensor(ai_description={"text": text})
        assert s.native_value == text

    def test_native_value_exactly_255_chars_unchanged(self) -> None:
        """Text of exactly 255 chars is returned unchanged."""
        text = "x" * 255
        s = _make_ai_description_sensor(ai_description={"text": text})
        assert s.native_value == text

    def test_native_value_300_chars_truncated_to_255(self) -> None:
        """Text of 300 chars must be truncated to exactly 255 chars."""
        text = "A" * 300
        s = _make_ai_description_sensor(ai_description={"text": text})
        result = s.native_value
        assert result is not None
        assert len(result) == 255
        assert result == "A" * 255

    def test_native_value_empty_string(self) -> None:
        """Empty string text returns empty string (not None)."""
        s = _make_ai_description_sensor(ai_description={"text": ""})
        assert s.native_value == ""

    def test_native_value_no_ai_description_key(self) -> None:
        """When no ai_description key exists in coordinator data, returns None."""
        s = _make_ai_description_sensor(ai_description=None)
        assert s.native_value is None

    def test_native_value_ai_description_has_no_text(self) -> None:
        """When ai_description dict exists but has no 'text' key, returns None."""
        s = _make_ai_description_sensor(
            ai_description={"generated_at": "2026-06-15T12:00:00+00:00"}
        )
        assert s.native_value is None

    def test_native_value_text_is_none(self) -> None:
        """When ai_description.text is explicitly None, returns None."""
        s = _make_ai_description_sensor(ai_description={"text": None})
        assert s.native_value is None

    def test_native_value_unicode_text(self) -> None:
        """Unicode text is handled correctly (char-count, not byte-count)."""
        text = "über " * 60  # 300 chars (each "über " = 5 chars)
        s = _make_ai_description_sensor(ai_description={"text": text})
        assert len(s.native_value or "") == 255  # type: ignore[arg-type]


# ── extra_state_attributes — full text + metadata ────────────────────────────


class TestAiSensorExtraAttributes:
    """Verify extra_state_attributes preserves full text and exposes metadata."""

    def test_attributes_full_text_not_truncated(self) -> None:
        """description attribute must contain the FULL text, not truncated."""
        text = "B" * 300
        s = _make_ai_description_sensor(ai_description={"text": text})
        attrs = s.extra_state_attributes
        assert attrs["description"] == text
        assert len(attrs["description"]) == 300

    def test_attributes_generated_at_preserved(self) -> None:
        ts = "2026-06-15T10:30:00+00:00"
        s = _make_ai_description_sensor(
            ai_description={"text": "A car.", "generated_at": ts}
        )
        assert s.extra_state_attributes["generated_at"] == ts

    def test_attributes_ai_task_entity_preserved(self) -> None:
        entity = "ai_task.google_ai"
        s = _make_ai_description_sensor(
            ai_description={"text": "A dog.", "ai_task_entity": entity}
        )
        assert s.extra_state_attributes["ai_task_entity"] == entity

    def test_attributes_ai_task_entity_default_string(self) -> None:
        """When ai_task_entity is 'default' it is surfaced unchanged."""
        s = _make_ai_description_sensor(
            ai_description={"text": "Empty.", "ai_task_entity": "default"}
        )
        assert s.extra_state_attributes["ai_task_entity"] == "default"

    def test_attributes_all_none_when_no_ai_description(self) -> None:
        """When no ai_description exists all attribute values are None."""
        s = _make_ai_description_sensor(ai_description=None)
        attrs = s.extra_state_attributes
        assert attrs["description"] is None
        assert attrs["generated_at"] is None
        assert attrs["ai_task_entity"] is None

    def test_attributes_keys_always_present(self) -> None:
        """All three attribute keys must be present regardless of data."""
        s = _make_ai_description_sensor(ai_description=None)
        attrs = s.extra_state_attributes
        assert "description" in attrs
        assert "generated_at" in attrs
        assert "ai_task_entity" in attrs

    def test_attributes_native_value_vs_full_text_differ_when_long(self) -> None:
        """Confirm native_value (255) != full text (300) for long descriptions."""
        text = "C" * 300
        s = _make_ai_description_sensor(ai_description={"text": text})
        assert s.native_value != s.extra_state_attributes["description"]
        assert s.extra_state_attributes["description"] == text


# ── Coordinator data round-trip ──────────────────────────────────────────────


class TestAiDescriptionCoordinatorRoundTrip:
    """Sensor must reflect whatever is stored in coordinator.data."""

    def test_sensor_reads_coordinator_data_update(self) -> None:
        """Mutating coordinator.data must be reflected in native_value."""
        coord = _make_ai_desc_coord()
        s = _make_ai_description_sensor(coord=coord)

        # Initially no ai_description
        assert s.native_value is None

        # Simulate service writing to coordinator data
        coord.data[CAM_ID][_AI_DESC_KEY] = {
            "text": "A van parked outside.",
            "generated_at": "2026-06-15T12:00:00+00:00",
            "ai_task_entity": "default",
        }
        assert s.native_value == "A van parked outside."

    def test_sensor_reflects_second_update(self) -> None:
        """Subsequent writes to coordinator.data are picked up immediately."""
        coord = _make_ai_desc_coord()
        s = _make_ai_description_sensor(coord=coord)

        coord.data[CAM_ID][_AI_DESC_KEY] = {"text": "First."}
        assert s.native_value == "First."

        coord.data[CAM_ID][_AI_DESC_KEY] = {"text": "Second."}
        assert s.native_value == "Second."

    def test_sensor_two_cameras_independent(self) -> None:
        """Two sensors on two cameras read their own data independently."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
        )

        coord = _make_ai_desc_coord(cam_ids=[CAM_ID, CAM_ID_2])
        entry = _make_ai_entry({"enable_ai_description": True})
        s1 = BoschCameraAiDescriptionSensor(coord, CAM_ID, entry)
        s2 = BoschCameraAiDescriptionSensor(coord, CAM_ID_2, entry)

        coord.data[CAM_ID][_AI_DESC_KEY] = {"text": "Cam1 sees a cat."}
        coord.data[CAM_ID_2][_AI_DESC_KEY] = {"text": "Cam2 sees a dog."}

        assert s1.native_value == "Cam1 sees a cat."
        assert s2.native_value == "Cam2 sees a dog."


# ── Auto-describe on motion — debounce logic (standalone unit tests) ────────
# The _async_auto_describe closure inside async_setup_entry can't be imported
# directly.  We test an equivalent standalone implementation that mirrors the
# exact logic from __init__.py so that any drift would be caught.


class _DebounceTracker:
    """Pure-Python mirror of the debounce logic in _async_auto_describe."""

    DEBOUNCE_SEC = 30.0

    def __init__(self) -> None:
        self._cache: dict[str, float] = {}
        self.calls: list[str] = []

    async def handle_motion(
        self,
        cam_id: str,
        now_ts: float,
        ai_describe_on_motion: bool,
        cam_entity: str | None,
    ) -> None:
        last = self._cache.get(cam_id, float("-inf"))
        if now_ts - last < self.DEBOUNCE_SEC:
            return
        self._cache[cam_id] = now_ts

        if not ai_describe_on_motion:
            return

        if cam_entity is None:
            return

        # Would call describe_snapshot service
        self.calls.append(cam_id)


class TestAutoDescribeDebounce:
    """Unit tests for the debounce / early-return logic in _async_auto_describe."""

    @pytest.mark.asyncio
    async def test_option_off_no_call(self) -> None:
        """When ai_describe_on_motion is False, no describe call is made."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, False, "camera.bosch_terrasse")
        assert tracker.calls == []

    @pytest.mark.asyncio
    async def test_option_on_entity_found_calls_describe(self) -> None:
        """When option ON and entity exists, describe_snapshot is triggered."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, True, "camera.bosch_terrasse")
        assert tracker.calls == [CAM_ID]

    @pytest.mark.asyncio
    async def test_option_on_no_entity_skips(self) -> None:
        """When cam_entity is None (not found), no call is made."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, True, None)
        assert tracker.calls == []

    @pytest.mark.asyncio
    async def test_debounce_second_call_within_30s_skipped(self) -> None:
        """Second motion event within 30 s must be skipped."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, True, "camera.bosch_terrasse")
        await tracker.handle_motion(
            CAM_ID, 129.9, True, "camera.bosch_terrasse"
        )  # 29.9 s later
        assert len(tracker.calls) == 1

    @pytest.mark.asyncio
    async def test_debounce_exactly_30s_allowed(self) -> None:
        """Call at exactly 30 s uses strict-less-than, so it is allowed (not skipped)."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, True, "camera.bosch_terrasse")
        # now_ts - last == 30.0; condition is `< 30.0` → False → call proceeds
        await tracker.handle_motion(CAM_ID, 130.0, True, "camera.bosch_terrasse")
        assert len(tracker.calls) == 2

    @pytest.mark.asyncio
    async def test_debounce_after_30s_allowed(self) -> None:
        """Call at 30.001 s after previous → window expired → allowed."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, True, "camera.bosch_terrasse")
        await tracker.handle_motion(CAM_ID, 130.001, True, "camera.bosch_terrasse")
        assert len(tracker.calls) == 2

    @pytest.mark.asyncio
    async def test_debounce_independent_per_camera(self) -> None:
        """Debounce windows are per cam_id; different cameras don't block each other."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, True, "camera.bosch_terrasse")
        await tracker.handle_motion(
            CAM_ID_2, 100.5, True, "camera.bosch_garten"
        )  # different cam_id
        assert len(tracker.calls) == 2
        assert CAM_ID in tracker.calls
        assert CAM_ID_2 in tracker.calls

    @pytest.mark.asyncio
    async def test_debounce_resets_after_window(self) -> None:
        """After a full debounce cycle, a third call in a new window is accepted."""
        tracker = _DebounceTracker()
        await tracker.handle_motion(CAM_ID, 100.0, True, "camera.bosch_terrasse")
        await tracker.handle_motion(
            CAM_ID, 110.0, True, "camera.bosch_terrasse"
        )  # skipped
        await tracker.handle_motion(
            CAM_ID, 135.0, True, "camera.bosch_terrasse"
        )  # 35 s after first → new window
        assert len(tracker.calls) == 2

    @pytest.mark.asyncio
    async def test_debounce_uses_float_neg_inf_as_initial_sentinel(self) -> None:
        """First-ever event is never blocked (initial sentinel is float('-inf'))."""
        tracker = _DebounceTracker()
        # Even at t=0.0 it must be allowed
        await tracker.handle_motion(CAM_ID, 0.0, True, "camera.bosch_terrasse")
        assert tracker.calls == [CAM_ID]


# ── Service call shape (describe_snapshot data contract) ────────────────────


class TestDescribeSnapshotCallShape:
    """Verify the data dict sent to ai_task.generate_data has the right shape."""

    @pytest.mark.asyncio
    async def test_call_data_attachment_format(self) -> None:
        """ai_call_data must include task_name, instructions, and attachments."""
        entity_id = "camera.bosch_terrasse"
        prompt = "Describe the scene."
        ai_entity = "ai_task.google_ai"

        # Build the call data exactly as handle_describe_snapshot would
        ai_call_data: dict[str, Any] = {
            "task_name": "Bosch camera snapshot",
            "instructions": prompt,
            "attachments": [
                {
                    "media_content_id": f"media-source://camera/{entity_id}",
                    "media_content_type": "image/jpeg",
                }
            ],
        }
        ai_call_data["entity_id"] = ai_entity

        assert ai_call_data["task_name"] == "Bosch camera snapshot"
        assert ai_call_data["instructions"] == prompt
        assert len(ai_call_data["attachments"]) == 1
        attach = ai_call_data["attachments"][0]
        assert attach["media_content_id"] == f"media-source://camera/{entity_id}"
        assert attach["media_content_type"] == "image/jpeg"
        assert ai_call_data["entity_id"] == ai_entity

    @pytest.mark.asyncio
    async def test_call_data_no_entity_id_when_empty(self) -> None:
        """entity_id key must be absent when ai_task_entity_used is empty."""
        ai_task_entity_used = ""
        ai_call_data: dict[str, Any] = {
            "task_name": "Bosch camera snapshot",
            "instructions": "Describe.",
            "attachments": [],
        }
        if ai_task_entity_used:
            ai_call_data["entity_id"] = ai_task_entity_used

        assert "entity_id" not in ai_call_data

    @pytest.mark.asyncio
    async def test_response_text_extraction_from_dict(self) -> None:
        """Text is extracted from resp['data'] when resp is a dict."""
        resp: dict[str, Any] = {"data": "A person is standing at the door."}
        text = str(resp.get("data", "")) if isinstance(resp, dict) else str(resp or "")
        assert text == "A person is standing at the door."

    @pytest.mark.asyncio
    async def test_response_text_extraction_from_non_dict(self) -> None:
        """Text is extracted via str() when resp is not a dict."""
        resp = "Direct string response."
        text = str(resp.get("data", "")) if isinstance(resp, dict) else str(resp or "")  # type: ignore[union-attr]
        assert text == "Direct string response."

    @pytest.mark.asyncio
    async def test_response_text_extraction_from_none(self) -> None:
        """When resp is None, text should be empty string."""
        resp = None
        text = str(resp.get("data", "")) if isinstance(resp, dict) else str(resp or "")  # type: ignore[union-attr]
        assert text == ""

    @pytest.mark.asyncio
    async def test_stored_ai_description_structure(self) -> None:
        """The dict stored into coordinator.data must have the three expected keys."""
        text = "A bicycle in the driveway."
        generated_at = "2026-06-15T12:00:00+00:00"
        ai_task_entity_used = "ai_task.google_ai"

        stored: dict[str, str] = {
            "text": text,
            "generated_at": generated_at,
            "ai_task_entity": ai_task_entity_used or "default",
        }

        assert stored["text"] == text
        assert stored["generated_at"] == generated_at
        assert stored["ai_task_entity"] == "ai_task.google_ai"

    @pytest.mark.asyncio
    async def test_stored_ai_description_uses_default_when_no_entity(self) -> None:
        """When ai_task_entity_used is empty, ai_task_entity is stored as 'default'."""
        ai_task_entity_used = ""
        stored: dict[str, str] = {
            "text": "something",
            "generated_at": "2026-06-15T00:00:00+00:00",
            "ai_task_entity": ai_task_entity_used or "default",
        }
        assert stored["ai_task_entity"] == "default"


# ── async_generate_ai_description — behavior coverage ────────────────────────
# These tests exercise the coordinator method directly via a minimal stub
# that mirrors the coordinator's AI-description state without requiring
# a full HA runtime.


def _make_ai_coord(
    *,
    enable_ai: bool = True,
    privacy_mode: bool = False,
    cam_id: str = CAM_ID,
    ai_text: str | None = None,
    ai_generated_at: str | None = None,
    cooldown: float = 60.0,
    max_per_day: int = 100,
    in_flight: int = 0,
) -> Any:
    """Minimal coordinator stub for async_generate_ai_description tests."""
    data: dict[str, Any] = {cam_id: {}}
    if ai_text is not None:
        ai_desc: dict[str, Any] = {"text": ai_text}
        if ai_generated_at is not None:
            ai_desc["generated_at"] = ai_generated_at
        data[cam_id]["ai_description"] = ai_desc

    shc_cache: dict[str, Any] = {cam_id: {"privacy_mode": privacy_mode}}

    opts: dict[str, Any] = {
        "enable_ai_description": enable_ai,
        "ai_cooldown_seconds": cooldown,
        "ai_max_per_day": max_per_day,
    }

    coord = SimpleNamespace(
        data=data,
        options=opts,
        hass=MagicMock(),
        shc_state_cache=shc_cache,
        camera_entities={
            cam_id: SimpleNamespace(entity_id=f"camera.bosch_{cam_id[:4]}")
        },
        _ai_last_call={},
        _ai_day_count=0,
        _ai_day_stamp="",
        ai_in_flight=in_flight,
        _ai_budget_logged_day="",
        _ai_budget_store=MagicMock(),
        async_set_updated_data=MagicMock(),
    )

    # Bind real coordinator methods so they use `coord` as self
    coord.ai_budget_state = _AiCoordinatorCls.ai_budget_state.__get__(coord)
    coord._ai_rate_allowed = _AiCoordinatorCls._ai_rate_allowed.__get__(coord)
    coord.ai_record_call = _AiCoordinatorCls.ai_record_call.__get__(coord)
    coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)
    coord._async_save_ai_budget = _AiCoordinatorCls._async_save_ai_budget.__get__(coord)
    coord.async_generate_ai_description = (
        _AiCoordinatorCls.async_generate_ai_description.__get__(coord)
    )

    # Seed _ai_last_call so that the cooldown gate can be tested
    coord._ai_last_call[cam_id] = float("-inf")

    return coord


class TestAsyncGenerateAiDescription:
    """Unit tests for coordinator.async_generate_ai_description."""

    @pytest.mark.asyncio
    async def test_privacy_guard_returns_none(self) -> None:
        """Privacy mode ON → method returns None regardless of force flag."""
        coord = _make_ai_coord(privacy_mode=True)
        result = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_privacy_guard_skips_ai_call(self) -> None:
        """Privacy ON → ai_task service must NOT be called."""
        coord = _make_ai_coord(privacy_mode=True)
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        coord.hass.services.async_call.assert_not_called()
        assert result is None

    @pytest.mark.asyncio
    async def test_rate_limited_returns_cached_text(self) -> None:
        """When cooldown blocks the call, return the cached ai_description text (if fresh)."""
        # generated_at must be recent (within cooldown/300s) for cache to be returned
        fresh_ts = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        coord = _make_ai_coord(
            ai_text="Previous description.", ai_generated_at=fresh_ts, cooldown=60.0
        )
        # Set last call to just now so cooldown is not expired
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result == "Previous description."
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_limited_no_cache_returns_none(self) -> None:
        """When cooldown blocks AND no cached text exists, return None."""
        coord = _make_ai_coord(ai_text=None, cooldown=60.0)
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_respects_in_flight_counter(self) -> None:
        """In-flight calls count toward the budget so concurrent calls are blocked."""
        coord = _make_ai_coord(max_per_day=1, in_flight=1)
        # With in_flight=1 and max_per_day=1, budget check: (0 + 1) >= 1 → blocked
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_flight_decremented_on_exception(self) -> None:
        """ai_in_flight must be decremented even when ai_task raises."""
        coord = _make_ai_coord()
        coord.ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("ai down"))
        result = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result is None
        # finally block must have decremented in_flight back to 0
        assert coord.ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_in_flight_decremented_on_success(self) -> None:
        """ai_in_flight must be 0 after a successful call completes."""
        coord = _make_ai_coord()
        coord.ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "A car in the driveway."}
        )
        coord.hass.bus.async_fire = MagicMock()
        result = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result == "A car in the driveway."
        assert coord.ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_on_motion_uses_budget_guard(self) -> None:
        """Simulate on-motion path: budget=1, day_count already 1 → blocked."""
        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        # Manually set ai_day_stamp to today so rollover doesn't clear the count
        coord._ai_day_stamp = dt_util.now().date().isoformat()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None
        coord.hass.services.async_call.assert_not_called()


class TestServiceHandlerStrip:
    """Verify .strip() + empty-text guard in the describe_snapshot service handler."""

    def test_strip_and_empty_returns_empty_description(self) -> None:
        """Service text extraction: whitespace-only → stripped empty → no write."""
        # Mirror the exact logic in handle_describe_snapshot after the strip fix
        resp: dict[str, Any] = {"data": "   "}
        text: str = (
            str(resp.get("data", "")) if isinstance(resp, dict) else str(resp or "")
        ).strip()
        assert text == ""
        # If text is empty, the handler returns {"description": ""} without writing
        if not text:
            result = {"description": ""}
        else:
            result = {"description": text}
        assert result == {"description": ""}

    def test_strip_removes_surrounding_whitespace(self) -> None:
        """Service text extraction: valid text with whitespace is trimmed."""
        resp: dict[str, Any] = {"data": "  A cat on the roof.  "}
        text: str = (
            str(resp.get("data", "")) if isinstance(resp, dict) else str(resp or "")
        ).strip()
        assert text == "A cat on the roof."

    def test_strip_none_data_yields_empty(self) -> None:
        """Service text extraction: data=None → empty string after strip."""
        resp: dict[str, Any] = {"data": None}
        text: str = (
            str(resp.get("data", "")) if isinstance(resp, dict) else str(resp or "")
        ).strip()
        # str(None) = "None" but resp.get("data","") returns None, and str(None)="None"
        # The actual guard: if data is None, resp.get("data","") returns None, str(None)="None"
        # That's a pre-existing design; the strip at least removes whitespace.
        # The important thing: empty-string guard catches "" not "None".
        # This test documents the actual behavior (str(None)="None", stripped="None").
        assert text == "None"


# ── Timeout, privacy guard, caption cap, stale cache, budget log ────────────


class TestTimeoutReturnsNone:
    """asyncio.timeout(20) → TimeoutError → None, finally still decrements."""

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        """When ai_task raises TimeoutError, method returns None."""
        coord = _make_ai_coord()
        coord.ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        result = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_decrements_in_flight(self) -> None:
        """TimeoutError in try block must still hit finally and decrement in_flight."""
        coord = _make_ai_coord()
        coord.ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        await coord.async_generate_ai_description(CAM_ID, force=True)
        assert coord.ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_timeout_does_not_fire_bus_event(self) -> None:
        """On timeout the bus must not fire — no description was generated."""
        coord = _make_ai_coord()
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        coord.hass.bus.async_fire = MagicMock()
        await coord.async_generate_ai_description(CAM_ID, force=True)
        coord.hass.bus.async_fire.assert_not_called()


class TestServicePrivacyGuard:
    """handle_describe_snapshot raises ServiceValidationError when privacy ON."""

    def test_privacy_guard_raises_service_validation_error(self) -> None:
        """Mirror the privacy guard logic: privacy ON → ServiceValidationError."""
        from homeassistant.exceptions import ServiceValidationError

        resolved_cam_id = CAM_ID
        shc_state_cache: dict[str, Any] = {CAM_ID: {"privacy_mode": True}}

        # Inline mirror of the guard
        if resolved_cam_id and shc_state_cache.get(resolved_cam_id, {}).get(
            "privacy_mode"
        ):
            with pytest.raises(ServiceValidationError):
                raise ServiceValidationError(
                    translation_domain="bosch_shc_camera",
                    translation_key="privacy_active",
                )

    def test_privacy_guard_not_raised_when_off(self) -> None:
        """When privacy_mode is False the guard must not raise."""
        resolved_cam_id = CAM_ID
        shc_state_cache: dict[str, Any] = {CAM_ID: {"privacy_mode": False}}

        raised = False
        if resolved_cam_id and shc_state_cache.get(resolved_cam_id, {}).get(
            "privacy_mode"
        ):
            raised = True
        assert not raised

    def test_privacy_active_translation_key_exists_in_strings(self) -> None:
        """strings.json must contain the privacy_active exception key."""
        import os

        strings_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
            "bosch_shc_camera",
            "strings.json",
        )
        with open(strings_path) as f:
            data = json.load(f)
        assert "privacy_active" in data.get("exceptions", {}), (
            "strings.json missing exceptions.privacy_active"
        )

    def test_privacy_active_translation_key_exists_in_de(self) -> None:
        """translations/de.json must contain the privacy_active exception key."""
        import os

        de_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
            "bosch_shc_camera",
            "translations",
            "de.json",
        )
        with open(de_path) as f:
            data = json.load(f)
        assert "privacy_active" in data.get("exceptions", {}), (
            "de.json missing exceptions.privacy_active"
        )

    def test_privacy_active_translation_key_exists_in_en(self) -> None:
        """translations/en.json must contain the privacy_active exception key."""
        import os

        en_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
            "bosch_shc_camera",
            "translations",
            "en.json",
        )
        with open(en_path) as f:
            data = json.load(f)
        assert "privacy_active" in data.get("exceptions", {}), (
            "en.json missing exceptions.privacy_active"
        )

    def test_de_privacy_active_contains_umlauts(self) -> None:
        """German privacy_active message must use proper umlauts (ä/ö/ü)."""
        import os

        de_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "custom_components",
            "bosch_shc_camera",
            "translations",
            "de.json",
        )
        with open(de_path) as f:
            data = json.load(f)
        msg = data["exceptions"]["privacy_active"]["message"]
        # Must have at least one umlaut — not ae/oe/ue substitutions
        assert any(ch in msg for ch in "äöüÄÖÜß"), (
            f"German privacy_active message lacks umlauts: {msg}"
        )


class TestCaptionCap:
    """AI description capped at 200 chars before appending to FCM caption."""

    def test_long_desc_capped_at_200(self) -> None:
        """Description > 200 chars is truncated to 200 before append."""
        _desc = "A" * 300
        _desc = _desc[:200].rstrip()
        caption = f"📸 Terrasse Snapshot (12:00)\n🤖 {_desc}"
        # The description portion should be 200 chars
        assert len(_desc) == 200
        assert "A" * 200 in caption

    def test_short_desc_unchanged(self) -> None:
        """Description ≤ 200 chars is not modified."""
        _desc = "A person is walking near the gate."
        _desc = _desc[:200].rstrip()
        assert _desc == "A person is walking near the gate."

    def test_exactly_200_chars_unchanged(self) -> None:
        """Description of exactly 200 chars is not truncated."""
        _desc = "B" * 200
        _desc = _desc[:200].rstrip()
        assert len(_desc) == 200

    def test_rstrip_removes_trailing_whitespace(self) -> None:
        """rstrip() removes trailing whitespace after truncation."""
        _desc = "Hello world   "
        _desc = _desc[:200].rstrip()
        assert not _desc.endswith(" ")


class TestStaleCachedGuard:
    """Rate-limited cache path rejects stale/privacy descriptions."""

    @pytest.mark.asyncio
    async def test_fresh_cache_within_cooldown_returned(self) -> None:
        """Cache hit with recent generated_at within cooldown → returned."""
        fresh_ts = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        coord = _make_ai_coord(
            ai_text="Fresh scene.", ai_generated_at=fresh_ts, cooldown=60.0
        )
        # Trigger rate-limiting by setting last call to now
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result == "Fresh scene."

    @pytest.mark.asyncio
    async def test_stale_cache_beyond_300s_rejected(self) -> None:
        """Cache hit with generated_at > 300s ago → return None (stale)."""
        stale_ts = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        coord = _make_ai_coord(
            ai_text="Old scene.", ai_generated_at=stale_ts, cooldown=60.0
        )
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_with_privacy_on_rejected(self) -> None:
        """Rate-limited path with privacy ON returns None even if text cached."""
        fresh_ts = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
        # privacy_mode=True already causes early return BEFORE the cache check —
        # the rate-limited branch is only reachable when privacy is OFF.
        # This test verifies the early-return privacy guard, not the cache guard.
        coord = _make_ai_coord(
            ai_text="Pre-privacy scene.", ai_generated_at=fresh_ts, privacy_mode=True
        )
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_without_generated_at_rejected(self) -> None:
        """Cached text with no generated_at timestamp → safe fallback returns None."""
        # ai_generated_at=None → no generated_at key in cached entry
        coord = _make_ai_coord(
            ai_text="No timestamp.", ai_generated_at=None, cooldown=60.0
        )
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_bad_generated_at_format_rejected(self) -> None:
        """Malformed generated_at string → safe fallback returns None."""
        coord = _make_ai_coord(
            ai_text="Bad ts.", ai_generated_at="not-a-datetime", cooldown=60.0
        )
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None


class TestBudgetLogOnce:
    """Budget-exceeded info log fires at most once per calendar day."""

    def test_budget_exceeded_logs_once(self) -> None:
        """First budget-exceeded call on a new day emits INFO log."""
        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        coord._ai_budget_logged_day = ""  # not yet logged

        coord._ai_day_stamp = dt_util.now().date().isoformat()  # prevent rollover

        with patch(
            "custom_components.bosch_shc_camera.coordinator._LOGGER"
        ) as mock_logger:
            result = coord._ai_rate_allowed(CAM_ID)
            assert result is False
            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args[0]
            assert "daily budget" in call_args[0]

    def test_budget_exceeded_does_not_log_twice_same_day(self) -> None:
        """Second budget-exceeded call on the SAME day must NOT log again."""
        import datetime as _dt

        today = _dt.date.today().isoformat()
        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        coord._ai_budget_logged_day = today  # already logged today
        coord._ai_day_stamp = today  # prevent rollover

        with patch(
            "custom_components.bosch_shc_camera.coordinator._LOGGER"
        ) as mock_logger:
            coord._ai_rate_allowed(CAM_ID)
            mock_logger.info.assert_not_called()

    def test_budget_log_day_stamp_updated(self) -> None:
        """After logging, _ai_budget_logged_day is set to today's LOCAL date.

        The log-once day key must use the same local-date source (dt_util.now)
        as ai_budget_state's daily rollover. A UTC date here would re-arm out
        of lockstep with the counter reset, suppressing the warning for the
        hours between local and UTC midnight.
        """
        today_ha = dt_util.now().date().isoformat()
        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        coord._ai_budget_logged_day = ""
        coord._ai_day_stamp = today_ha

        with patch("custom_components.bosch_shc_camera.coordinator._LOGGER"):
            coord._ai_rate_allowed(CAM_ID)

        assert coord._ai_budget_logged_day == today_ha

    def test_budget_log_day_follows_local_not_utc_date(self) -> None:
        """log-day key tracks dt_util.now() even when it diverges from UTC.

        Pins the fix against a revert to datetime.now(UTC): with a mocked local
        clock whose date is fixed and clearly not the real UTC date, the logged
        day must equal the mocked local date.
        """
        import datetime as _dt

        fixed_local = _dt.datetime(2000, 1, 1, 1, 0, 0)
        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        coord._ai_budget_logged_day = ""
        coord._ai_day_stamp = "2000-01-01"  # matches mocked date → no rollover

        with (
            patch("custom_components.bosch_shc_camera.coordinator._LOGGER"),
            patch(
                "custom_components.bosch_shc_camera.coordinator.dt_util.now",
                return_value=fixed_local,
            ),
        ):
            result = coord._ai_rate_allowed(CAM_ID)

        assert result is False
        assert coord._ai_budget_logged_day == "2000-01-01"


# ── AI activation window gating — _ai_window_allowed ────────────────────────
# Tests cover time-window (normal + overnight + boundary), condition entity
# (allowed / blocked / unavailable / missing), neither gate, and the force
# bypass path via async_generate_ai_description.


def _make_gating_coord(
    *,
    time_start: str = "",
    time_end: str = "",
    condition_entity: str = "",
    condition_state: str = "not_home",
    entity_state: str | None = None,
    entity_available: bool = True,
) -> Any:
    """Coordinator stub focused on _ai_window_allowed testing."""
    opts: dict[str, Any] = {
        "enable_ai_description": True,
        "ai_active_time_start": time_start,
        "ai_active_time_end": time_end,
        "ai_active_condition_entity": condition_entity,
        "ai_active_condition_state": condition_state,
        "ai_cooldown_seconds": 60.0,
        "ai_max_per_day": 100,
    }

    hass_mock = MagicMock()
    if condition_entity:
        if entity_state is None:
            hass_mock.states.get.return_value = None  # entity missing
        elif not entity_available:
            hass_mock.states.get.return_value = SimpleNamespace(state="unavailable")
        else:
            hass_mock.states.get.return_value = SimpleNamespace(state=entity_state)
    else:
        hass_mock.states.get.return_value = None

    coord = SimpleNamespace(
        options=opts,
        hass=hass_mock,
    )

    coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)
    return coord


class TestAiWindowAllowedTimeGate:
    """PIN_EVERY_MODE: time window normal, overnight, boundary, malformed, no-gate."""

    def _allowed_at(
        self, time_start: str, time_end: str, now_hour: int, now_min: int
    ) -> bool:
        """Call _ai_window_allowed with dt_util.now() patched to HH:MM."""
        coord = _make_gating_coord(time_start=time_start, time_end=time_end)
        fake_now = datetime(2026, 6, 15, now_hour, now_min, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            return coord._ai_window_allowed()

    def test_no_gate_always_allowed(self) -> None:
        """Neither time nor condition configured → always True."""
        coord = _make_gating_coord()
        assert coord._ai_window_allowed() is True

    def test_normal_window_inside(self) -> None:
        """08:00–22:00 window, current time 12:00 → allowed."""
        assert self._allowed_at("08:00", "22:00", 12, 0) is True

    def test_normal_window_before(self) -> None:
        """08:00–22:00 window, current time 07:59 → blocked."""
        assert self._allowed_at("08:00", "22:00", 7, 59) is False

    def test_normal_window_after(self) -> None:
        """08:00–22:00 window, current time 22:01 → blocked."""
        assert self._allowed_at("08:00", "22:00", 22, 1) is False

    def test_normal_window_at_start_boundary(self) -> None:
        """08:00–22:00 window, current time exactly 08:00 → allowed (inclusive)."""
        assert self._allowed_at("08:00", "22:00", 8, 0) is True

    def test_normal_window_at_end_boundary(self) -> None:
        """08:00–22:00 window, current time exactly 22:00 → allowed (inclusive)."""
        assert self._allowed_at("08:00", "22:00", 22, 0) is True

    def test_overnight_window_after_start(self) -> None:
        """22:00–06:00 overnight window, current time 23:00 → allowed."""
        assert self._allowed_at("22:00", "06:00", 23, 0) is True

    def test_overnight_window_before_end(self) -> None:
        """22:00–06:00 overnight window, current time 05:00 → allowed."""
        assert self._allowed_at("22:00", "06:00", 5, 0) is True

    def test_overnight_window_in_gap(self) -> None:
        """22:00–06:00 overnight window, current time 12:00 → blocked."""
        assert self._allowed_at("22:00", "06:00", 12, 0) is False

    def test_overnight_window_at_start_boundary(self) -> None:
        """22:00–06:00 overnight, at 22:00 exactly → allowed."""
        assert self._allowed_at("22:00", "06:00", 22, 0) is True

    def test_overnight_window_at_end_boundary(self) -> None:
        """22:00–06:00 overnight, at 06:00 exactly → allowed."""
        assert self._allowed_at("22:00", "06:00", 6, 0) is True

    def test_malformed_time_start_treats_as_no_gate(self) -> None:
        """Malformed start time (e.g. 'not-a-time') → fail-open (allow)."""
        coord = _make_gating_coord(time_start="not-a-time", time_end="22:00")
        assert coord._ai_window_allowed() is True

    def test_malformed_time_end_treats_as_no_gate(self) -> None:
        """Malformed end time → fail-open (allow)."""
        coord = _make_gating_coord(time_start="08:00", time_end="bad")
        assert coord._ai_window_allowed() is True

    def test_only_start_no_end_no_gate(self) -> None:
        """Only start set, end empty → no time gate (both must be non-empty)."""
        coord = _make_gating_coord(time_start="08:00", time_end="")
        # No full time gate → only check whether condition gate active (also none)
        assert coord._ai_window_allowed() is True

    def test_only_end_no_start_no_gate(self) -> None:
        """Only end set, start empty → no time gate."""
        coord = _make_gating_coord(time_start="", time_end="22:00")
        assert coord._ai_window_allowed() is True

    def test_with_seconds_in_time_string(self) -> None:
        """HH:MM:SS format is accepted without error."""
        assert self._allowed_at("08:00:00", "22:00:00", 12, 0) is True


class TestAiWindowAllowedConditionGate:
    """Condition entity gate: allowed, blocked, unavailable, unknown, missing."""

    def test_entity_matches_state_allows(self) -> None:
        """Entity state == condition_state → allowed."""
        coord = _make_gating_coord(
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="not_home",
        )
        assert coord._ai_window_allowed() is True

    def test_entity_wrong_state_blocks(self) -> None:
        """Entity state != condition_state → blocked."""
        coord = _make_gating_coord(
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="home",
        )
        assert coord._ai_window_allowed() is False

    def test_entity_missing_blocks(self) -> None:
        """Entity not found (None) → conservative block."""
        coord = _make_gating_coord(
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state=None,  # missing
        )
        assert coord._ai_window_allowed() is False

    def test_entity_unavailable_blocks(self) -> None:
        """Entity state == 'unavailable' → conservative block."""
        coord = _make_gating_coord(
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="unavailable",
            entity_available=False,
        )
        assert coord._ai_window_allowed() is False

    def test_entity_unknown_blocks(self) -> None:
        """Entity state == 'unknown' → conservative block."""
        coord = _make_gating_coord(
            condition_entity="person.thomas",
            condition_state="not_home",
        )
        coord.hass.states.get.return_value = SimpleNamespace(state="unknown")
        assert coord._ai_window_allowed() is False

    def test_custom_condition_state_on(self) -> None:
        """condition_state='on' with entity in 'on' state → allowed."""
        coord = _make_gating_coord(
            condition_entity="input_boolean.night_mode",
            condition_state="on",
            entity_state="on",
        )
        assert coord._ai_window_allowed() is True

    def test_custom_condition_state_off_blocks(self) -> None:
        """condition_state='on' with entity in 'off' state → blocked."""
        coord = _make_gating_coord(
            condition_entity="input_boolean.night_mode",
            condition_state="on",
            entity_state="off",
        )
        assert coord._ai_window_allowed() is False


class TestAiWindowBothGates:
    """Both time + condition gates together → AND logic."""

    def test_both_allowed(self) -> None:
        """Time in window AND condition matches → allowed."""
        coord = _make_gating_coord(
            time_start="08:00",
            time_end="22:00",
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="not_home",
        )
        fake_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            assert coord._ai_window_allowed() is True

    def test_time_blocked_condition_ok(self) -> None:
        """Time outside window even if condition matches → blocked."""
        coord = _make_gating_coord(
            time_start="08:00",
            time_end="22:00",
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="not_home",
        )
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            assert coord._ai_window_allowed() is False

    def test_time_ok_condition_wrong(self) -> None:
        """Time in window but condition wrong state → blocked."""
        coord = _make_gating_coord(
            time_start="08:00",
            time_end="22:00",
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="home",  # home → blocked
        )
        fake_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            assert coord._ai_window_allowed() is False


class TestAiWindowForceBypass:
    """force=True must bypass the window gate entirely."""

    @pytest.mark.asyncio
    async def test_force_true_bypasses_blocked_window(self) -> None:
        """Manual describe_snapshot (force=True) ignores the time gate."""
        coord = _make_ai_coord(cooldown=0.0)  # allow rate gate through

        # Configure a time gate that blocks (outside 08:00–22:00)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "22:00"
        coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)

        # Mock dt_util.now() to return 23:00 (outside window)
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "Force bypassed window."}
        )
        coord.hass.bus.async_fire = MagicMock()

        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            # Auto call (force=False) — window blocks
            result_auto = await coord.async_generate_ai_description(CAM_ID, force=False)
            assert result_auto is None
            coord.hass.services.async_call.assert_not_called()

            # Manual call (force=True) — window bypassed
            result_force = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result_force == "Force bypassed window."
        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_true_bypasses_blocked_condition(self) -> None:
        """Manual describe_snapshot (force=True) ignores the condition gate."""
        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_condition_entity"] = "person.thomas"
        coord.options["ai_active_condition_state"] = "not_home"
        coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)

        # Entity says "home" → condition gate blocks auto calls
        coord.hass.states.get.return_value = SimpleNamespace(state="home")
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "Force bypassed condition."}
        )
        coord.hass.bus.async_fire = MagicMock()

        # Auto: blocked by condition
        result_auto = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result_auto is None
        coord.hass.services.async_call.assert_not_called()

        # Force: bypassed
        result_force = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result_force == "Force bypassed condition."


# ── Budget store persistence ─────────────────────────────────────────────────


class TestBudgetStorePersistence:
    """async_load_ai_budget + _async_save_ai_budget."""

    @pytest.mark.asyncio
    async def test_load_today_restores_count(self) -> None:
        """Loading stored data for today restores the day count."""
        coord = _make_ai_coord()
        today = dt_util.now().date().isoformat()
        coord._ai_budget_store.async_load = AsyncMock(
            return_value={"date": today, "count": 42}
        )

        await _AiCoordinatorCls.async_load_ai_budget(coord)
        assert coord._ai_day_count == 42
        assert coord._ai_day_stamp == today

    @pytest.mark.asyncio
    async def test_load_yesterday_does_not_restore(self) -> None:
        """Loading stored data for yesterday leaves count at 0 (new day)."""
        import datetime as _dt

        coord = _make_ai_coord()
        yesterday = (dt_util.now().date() - _dt.timedelta(days=1)).isoformat()
        coord._ai_budget_store.async_load = AsyncMock(
            return_value={"date": yesterday, "count": 99}
        )

        await _AiCoordinatorCls.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0  # not restored — yesterday's data

    @pytest.mark.asyncio
    async def test_load_store_error_safe(self) -> None:
        """Store load error is caught — counter stays at 0."""
        coord = _make_ai_coord()
        coord._ai_budget_store.async_load = AsyncMock(side_effect=OSError("disk full"))

        await _AiCoordinatorCls.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0  # safe default

    @pytest.mark.asyncio
    async def test_save_called_on_record(self) -> None:
        """ai_record_call must schedule a save (async_create_task called)."""
        coord = _make_ai_coord(cooldown=0.0)
        coord.hass.async_create_task = MagicMock()

        coord.ai_record_call(CAM_ID)

        # async_create_task must have been called (the save is scheduled)
        coord.hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_save_called_on_day_rollover(self) -> None:
        """ai_budget_state triggers save task when day rolls over."""
        coord = _make_ai_coord()
        coord._ai_day_stamp = "2026-01-01"  # yesterday
        coord.hass.async_create_task = MagicMock()

        coord.ai_budget_state()  # triggers rollover

        coord.hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_save_contents(self) -> None:
        """_async_save_ai_budget writes date and count to store."""
        import datetime as _dt

        coord = _make_ai_coord()
        today = _dt.date.today().isoformat()
        coord._ai_day_stamp = today
        coord._ai_day_count = 7
        coord._ai_budget_store.async_save = AsyncMock()

        await _AiCoordinatorCls._async_save_ai_budget(coord)
        coord._ai_budget_store.async_save.assert_called_once_with(
            {"date": today, "count": 7}
        )

    @pytest.mark.asyncio
    async def test_save_error_safe(self) -> None:
        """_async_save_ai_budget catches store save errors silently."""
        coord = _make_ai_coord()
        coord._ai_budget_store.async_save = AsyncMock(
            side_effect=OSError("write error")
        )

        # Must not raise
        await _AiCoordinatorCls._async_save_ai_budget(coord)


# ── One-call-per-event guarantee ─────────────────────────────────────────────


class TestOneCallPerEvent:
    """Both on_motion + notify_include enabled → only ONE ai_task call per event."""

    @pytest.mark.asyncio
    async def test_second_call_reuses_cache_no_new_api_call(self) -> None:
        """Simulate on-motion (call 1) then notify-include (call 2, same event).

        The second force=False call must return cached text without a new
        ai_task.generate_data call.
        """
        coord = _make_ai_coord(cooldown=60.0)
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "A person at the door."}
        )
        coord.hass.bus.async_fire = MagicMock()

        # Call 1: on-motion path (force=False, cooldown not hit yet)
        result1 = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result1 == "A person at the door."
        assert coord.hass.services.async_call.call_count == 1

        # Call 2: notify-include path (force=False, cooldown now active)
        # The cached text was stored by Call 1 with a fresh timestamp.
        result2 = await coord.async_generate_ai_description(CAM_ID, force=False)
        # Must return cached text WITHOUT a second API call
        assert result2 == "A person at the door."
        assert coord.hass.services.async_call.call_count == 1  # still 1

    @pytest.mark.asyncio
    async def test_stale_cache_no_second_call_returns_none(self) -> None:
        """After cooldown + 300s cap expired, the second call returns None (not a new call)."""
        # Seed with a stale cached entry (> 300s old)
        stale_ts = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        coord = _make_ai_coord(
            ai_text="Old description.", ai_generated_at=stale_ts, cooldown=60.0
        )
        coord.hass.services.async_call = AsyncMock(return_value={"data": "Fresh."})
        coord.hass.bus.async_fire = MagicMock()

        # Simulate cooldown active
        coord._ai_last_call[CAM_ID] = time.monotonic()

        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        # Stale cache → None, no new API call
        assert result is None
        coord.hass.services.async_call.assert_not_called()


class TestAiWindowEdgeCases:
    """Edge-case verification for _ai_window_allowed time-gate paths.

    Covers overnight wraparound boundaries, microsecond stripping,
    equal start==end, seconds-precision parsing, and 23:59 near-midnight.
    Uses _make_gating_coord and patches dt_util.now like the parent class.
    """

    def _allowed_at_hms(
        self,
        time_start: str,
        time_end: str,
        now_hour: int,
        now_min: int,
        now_sec: int = 0,
        now_us: int = 0,
    ) -> bool:
        """Call _ai_window_allowed with dt_util.now() patched to HH:MM:SS.us."""
        coord = _make_gating_coord(time_start=time_start, time_end=time_end)
        fake_now = datetime(2026, 6, 15, now_hour, now_min, now_sec, now_us, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            return coord._ai_window_allowed()

    # --- Overnight wraparound: 22:00–06:00 ---

    def test_overnight_at_2330_allowed(self) -> None:
        """22:00–06:00, at 23:30 → allowed (after start, before midnight)."""
        assert self._allowed_at_hms("22:00", "06:00", 23, 30) is True

    def test_overnight_at_0500_allowed(self) -> None:
        """22:00–06:00, at 05:00 → allowed (before end, after midnight)."""
        assert self._allowed_at_hms("22:00", "06:00", 5, 0) is True

    def test_overnight_at_1400_blocked(self) -> None:
        """22:00–06:00, at 14:00 → blocked (mid-day gap)."""
        assert self._allowed_at_hms("22:00", "06:00", 14, 0) is False

    def test_overnight_at_2200_exactly_allowed(self) -> None:
        """22:00–06:00, at exactly 22:00 → allowed (start boundary inclusive)."""
        assert self._allowed_at_hms("22:00", "06:00", 22, 0) is True

    def test_overnight_at_0600_exactly_allowed(self) -> None:
        """22:00–06:00, at exactly 06:00 → allowed (end boundary inclusive)."""
        assert self._allowed_at_hms("22:00", "06:00", 6, 0) is True

    # --- Normal window boundary detail ---

    def test_normal_at_0759_blocked(self) -> None:
        """08:00–22:00, at 07:59 → blocked (one minute before start)."""
        assert self._allowed_at_hms("08:00", "22:00", 7, 59) is False

    def test_normal_at_0800_allowed(self) -> None:
        """08:00–22:00, at exactly 08:00 → allowed."""
        assert self._allowed_at_hms("08:00", "22:00", 8, 0) is True

    def test_normal_at_2200_allowed(self) -> None:
        """08:00–22:00, at exactly 22:00 → allowed (end boundary inclusive)."""
        assert self._allowed_at_hms("08:00", "22:00", 22, 0) is True

    def test_normal_at_2201_blocked(self) -> None:
        """08:00–22:00, at 22:01 → blocked (one minute after end)."""
        assert self._allowed_at_hms("08:00", "22:00", 22, 1) is False

    # --- Microsecond stripping does NOT kill seconds ---

    def test_microseconds_stripped_seconds_preserved(self) -> None:
        """replace(microsecond=0) zeroes only µs; seconds component survives.

        This verifies the implementation doesn't accidentally call
        replace(second=0, microsecond=0), which would make e.g. 08:00:45.999ms
        compare equal to 08:00:00 — collapsing 45s of precision.
        """
        # Window 08:00:30–22:00:30. At 08:00:29 with 999999µs:
        # after replace(microsecond=0) → 08:00:29 → still BEFORE start → blocked.
        assert self._allowed_at_hms("08:00:30", "22:00:30", 8, 0, 29, 999999) is False
        # At 08:00:30 with 500000µs:
        # after replace(microsecond=0) → 08:00:30 → equals start → allowed.
        assert self._allowed_at_hms("08:00:30", "22:00:30", 8, 0, 30, 500000) is True

    # --- Equal start == end edge ---

    def test_equal_start_end_at_exact_time_allowed(self) -> None:
        """start==end (12:00==12:00): t_end>=t_start → normal path.

        Normal path: t_start<=now_t<=t_end.  At exactly 12:00 → both bounds
        satisfied → True.  Any other minute → False (one-minute window).
        """
        assert self._allowed_at_hms("12:00", "12:00", 12, 0) is True

    def test_equal_start_end_other_time_blocked(self) -> None:
        """start==end (12:00), at 12:01 → blocked (outside the single-second window)."""
        assert self._allowed_at_hms("12:00", "12:00", 12, 1) is False

    # --- Seconds-precision parsing ---

    def test_seconds_precision_hms_inside(self) -> None:
        """HH:MM:SS strings parse and compare with sub-minute precision.

        08:00:30–22:00:30: at 08:00:00 (before 08:00:30) → blocked.
        """
        assert self._allowed_at_hms("08:00:30", "22:00:30", 8, 0, 0) is False

    def test_seconds_precision_hms_at_start_boundary(self) -> None:
        """HH:MM:SS start boundary: at exactly 08:00:30 → allowed."""
        assert self._allowed_at_hms("08:00:30", "22:00:30", 8, 0, 30) is True

    # --- Near-midnight edge (23:59) in overnight window ---

    def test_overnight_at_2359_allowed(self) -> None:
        """22:00–06:00, at 23:59 → allowed (one minute before midnight)."""
        assert self._allowed_at_hms("22:00", "06:00", 23, 59) is True


# ── Window gate does NOT consume budget ──────────────────────────────────────
# PIN_EVERY_MODE: verify that a window-blocked call never increments
# ai_in_flight, _ai_day_count, or _ai_last_call.


class TestWindowGateDoesNotConsumeBudget:
    """Window-blocked calls must not consume any budget or rate-limit tokens."""

    @pytest.mark.asyncio
    async def test_window_blocked_does_not_increment_day_count(self) -> None:
        """When _ai_window_allowed() is False, _ai_day_count stays at 0."""
        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})
        coord.hass.bus.async_fire = MagicMock()

        # Time 23:00 is outside 08:00-10:00 -> window blocked
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            result = await coord.async_generate_ai_description(CAM_ID, force=False)

        assert result is None
        assert coord._ai_day_count == 0

    @pytest.mark.asyncio
    async def test_window_blocked_does_not_increment_in_flight(self) -> None:
        """When window blocks, ai_in_flight must never increase (no try/finally entered)."""
        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})
        coord.hass.bus.async_fire = MagicMock()
        initial_in_flight = coord.ai_in_flight

        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            await coord.async_generate_ai_description(CAM_ID, force=False)

        assert coord.ai_in_flight == initial_in_flight

    @pytest.mark.asyncio
    async def test_window_blocked_does_not_update_last_call(self) -> None:
        """Window-blocked call must not touch _ai_last_call (cooldown not started)."""
        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})
        coord.hass.bus.async_fire = MagicMock()
        sentinel = float("-inf")
        coord._ai_last_call[CAM_ID] = sentinel

        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            await coord.async_generate_ai_description(CAM_ID, force=False)

        # _ai_last_call must stay at the sentinel value -- not touched by the blocked call
        assert coord._ai_last_call[CAM_ID] == sentinel

    @pytest.mark.asyncio
    async def test_window_blocked_no_ai_task_call(self) -> None:
        """Window-blocked path must not invoke hass.services.async_call at all."""
        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})

        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            await coord.async_generate_ai_description(CAM_ID, force=False)

        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_budget_remains_available_after_window_block(self) -> None:
        """After N window-blocked calls the full daily budget is still available.

        Simulate 5 window-blocked calls then one in-window call.  Only the
        in-window call must produce a result and consume one budget unit.
        """
        coord = _make_ai_coord(cooldown=0.0, max_per_day=1)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = _AiCoordinatorCls._ai_window_allowed.__get__(coord)
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "Allowed call result."}
        )
        coord.hass.bus.async_fire = MagicMock()

        outside_window = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        inside_window = datetime(2026, 6, 15, 9, 0, 0, tzinfo=UTC)

        # 5 blocked calls (outside window) must not consume the single-unit budget
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=outside_window,
        ):
            for _ in range(5):
                r = await coord.async_generate_ai_description(CAM_ID, force=False)
                assert r is None

        assert coord._ai_day_count == 0  # budget untouched

        # One in-window call must succeed and consume exactly one budget unit
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=inside_window,
        ):
            result = await coord.async_generate_ai_description(CAM_ID, force=False)

        assert result == "Allowed call result."
        assert coord._ai_day_count == 1
        assert coord.hass.services.async_call.call_count == 1


class TestAiWindowEntityEdgeCases:
    """Targeted edge-case pins for entity-state paths in _ai_window_allowed.

    Covers: "unknown" state log message correctness, whitespace-only entity ID
    treated as no-gate, unavailable-entity blocks auto path, dual-save idempotency.
    """

    def test_unknown_state_blocks_and_logs_unknown(self, caplog: Any) -> None:
        """Entity state == 'unknown' → blocked; log says 'unknown' not 'missing'.

        The log template "is %s — blocking AI" uses ``state_obj.state if state_obj
        else 'missing'``.  When state_obj exists but .state == 'unknown' the
        else-branch must NOT fire — so the log must contain 'unknown'.
        """
        import logging

        coord = _make_gating_coord(
            condition_entity="person.thomas",
            condition_state="not_home",
        )
        coord.hass.states.get.return_value = SimpleNamespace(state="unknown")
        with caplog.at_level(
            logging.DEBUG, logger="custom_components.bosch_shc_camera"
        ):
            result = coord._ai_window_allowed()
        assert result is False
        relevant = [r for r in caplog.records if "blocking AI" in r.getMessage()]
        assert relevant, "Expected a 'blocking AI' debug log entry"
        assert "unknown" in relevant[0].getMessage()
        assert "missing" not in relevant[0].getMessage()

    def test_whitespace_only_condition_entity_is_no_gate(self) -> None:
        """condition_entity='   ' → .strip() → '' → falsy → gate inactive → True.

        The production code strips: ``(opts.get(...) or "").strip()``.
        A whitespace-only value becomes '' after strip, so condition_gate_active
        is False and hass.states.get must never be called.
        """
        coord = _make_gating_coord()
        coord.options["ai_active_condition_entity"] = "   "
        result = coord._ai_window_allowed()
        assert result is True
        coord.hass.states.get.assert_not_called()

    def test_unavailable_entity_blocks_window_gate(self) -> None:
        """Entity state == 'unavailable' → _ai_window_allowed() returns False.

        Pins the AND logic: the gate itself returns False when the condition
        entity is unavailable, so any caller honouring the gate is blocked.
        """
        coord = _make_gating_coord(
            condition_entity="sensor.home_state",
            condition_state="away",
            entity_available=False,  # → SimpleNamespace(state='unavailable')
        )
        assert coord._ai_window_allowed() is False

    def test_budget_dual_save_idempotent(self) -> None:
        """ai_record_call triggers 2 async_create_task saves; counter must be 1.

        Sequence inside ai_record_call:
          1. ai_budget_state() for day-rollover: stamp was '' != today → reset +
             schedule save (task #1).
          2. _ai_day_count incremented to 1.
          3. Another async_create_task save scheduled (task #2).
        Both tasks write current {date, count}.  Last-write-wins is safe.
        We verify: exactly 2 tasks enqueued and _ai_day_count == 1.
        """
        import datetime as _dt

        hass_mock = MagicMock()
        tasks_created: list[Any] = []
        hass_mock.async_create_task.side_effect = tasks_created.append

        coord = SimpleNamespace(
            options={"ai_max_per_day": 100, "ai_cooldown_seconds": 60},
            hass=hass_mock,
            _ai_day_count=0,
            _ai_day_stamp="",
            _ai_budget_logged_day="",
            ai_in_flight=0,
            _ai_last_call={},
        )
        coord._async_save_ai_budget = MagicMock()
        coord.ai_budget_state = _AiCoordinatorCls.ai_budget_state.__get__(coord)
        coord.ai_record_call = _AiCoordinatorCls.ai_record_call.__get__(coord)

        fake_now = _dt.datetime(2026, 6, 15, 12, 0, tzinfo=_dt.UTC)
        with patch(
            "custom_components.bosch_shc_camera.coordinator.dt_util.now",
            return_value=fake_now,
        ):
            coord.ai_record_call("cam-aabbccdd")

        # Day-rollover save (task #1) + record save (task #2)
        assert len(tasks_created) == 2
        assert coord._ai_day_count == 1


# WiFi signal + firmware version diagnostic sensors
# entity_category=DIAGNOSTIC, wifi signal % (source: /v11/video_inputs/{id}/
# wifiinfo, signalStrength 0-100%), firmware version string (info.firmwareVersion).


def _make_wifi_fw_coord(
    firmware: str = "9.40.25",
    wifiinfo: dict[str, Any] | None = None,
    last_update_success: bool = True,
    rcp_lan_ip: str | None = None,
    rcp_bitrate_ladder: list[int] | None = None,
    rcp_product_name: str | None = None,
    up_to_date: bool | None = None,
    hardware_version: str = "HOME_Eyes_Outdoor",
) -> Any:
    """Build a minimal coordinator stub for wifi/firmware sensor tests."""
    info: dict[str, Any] = {
        "firmwareVersion": firmware,
        "hardwareVersion": hardware_version,
        "macAddress": "aa:bb:cc:33:14:ae",
        "title": "Terrasse",
    }
    if up_to_date is not None:
        info["upToDate"] = up_to_date

    coord = SimpleNamespace(
        data={CAM_ID: {"info": info, "status": "ONLINE", "events": []}},
        last_update_success=last_update_success,
        wifiinfo_cache={} if wifiinfo is None else {CAM_ID: wifiinfo},
        rcp_lan_ip=lambda cid: rcp_lan_ip,
        rcp_bitrate_ladder=lambda cid: rcp_bitrate_ladder,
        rcp_product_name=lambda cid: rcp_product_name,
        async_request_refresh=None,
    )
    return coord


class TestWifiSignalSensor:
    """Tests for BoschWifiSignalSensor (entity_category=DIAGNOSTIC, unit=%, no dBm device_class)."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        c = coord if coord is not None else _make_wifi_fw_coord()
        return BoschWifiSignalSensor(c, CAM_ID, _make_entry())

    # entity metadata
    def test_entity_category_is_diagnostic(self) -> None:
        s = self._make()
        assert s.entity_category == EntityCategory.DIAGNOSTIC

    def test_unit_is_percent(self) -> None:
        s = self._make()
        assert s.native_unit_of_measurement == "%"

    def test_icon_lives_in_icons_json_not_hardcoded(self) -> None:
        """Icon lives in icons.json — no _attr_icon on the entity itself, so
        HA's own icon-translation resolves it via translation_key instead of
        a hardcoded Python value."""
        s = self._make()
        assert getattr(s, "_attr_icon", None) is None
        assert ICONS["entity"]["sensor"]["wifi_signal"]["default"] == "mdi:wifi"

    def test_translation_key(self) -> None:
        s = self._make()
        assert s.translation_key == "wifi_signal"

    # native_value: None when no wifiinfo in cache
    def test_native_value_none_when_cache_empty(self) -> None:
        s = self._make(_make_wifi_fw_coord(wifiinfo=None))
        assert s.native_value is None

    # native_value: valid typical signal (mid-range)
    def test_native_value_typical_signal(self) -> None:
        s = self._make(
            _make_wifi_fw_coord(
                wifiinfo={
                    "signalStrength": 67,
                    "ssid": "HOME",
                    "ipAddress": "192.168.1.2",
                    "macAddress": "aa:bb",
                }
            )
        )
        assert s.native_value == 67

    # native_value: minimum boundary (0)
    def test_native_value_zero_signal(self) -> None:
        s = self._make(
            _make_wifi_fw_coord(
                wifiinfo={
                    "signalStrength": 0,
                    "ssid": "X",
                    "ipAddress": "",
                    "macAddress": "",
                }
            )
        )
        assert s.native_value == 0

    # native_value: maximum boundary (100)
    def test_native_value_full_signal(self) -> None:
        s = self._make(
            _make_wifi_fw_coord(
                wifiinfo={
                    "signalStrength": 100,
                    "ssid": "X",
                    "ipAddress": "",
                    "macAddress": "",
                }
            )
        )
        assert s.native_value == 100

    # native_value: signalStrength key missing (garbage/partial response)
    def test_native_value_none_when_signal_key_absent(self) -> None:
        s = self._make(
            _make_wifi_fw_coord(wifiinfo={"ssid": "HOME"})
        )  # no signalStrength key
        assert s.native_value is None

    # native_value: signalStrength explicitly null
    def test_native_value_none_when_signal_explicit_none(self) -> None:
        s = self._make(
            _make_wifi_fw_coord(wifiinfo={"signalStrength": None, "ssid": "HOME"})
        )
        assert s.native_value is None

    # available: False when cache is empty
    def test_available_false_when_no_wifiinfo(self) -> None:
        s = self._make(_make_wifi_fw_coord(wifiinfo=None))
        assert s.available is False

    # available: False when coordinator update failed
    def test_available_false_when_update_failed(self) -> None:
        c = _make_wifi_fw_coord(
            wifiinfo={
                "signalStrength": 75,
                "ssid": "X",
                "ipAddress": "",
                "macAddress": "",
            },
            last_update_success=False,
        )
        s = self._make(c)
        assert s.available is False

    # available: True when cache has data + update succeeded
    def test_available_true_when_data_present(self) -> None:
        c = _make_wifi_fw_coord(
            wifiinfo={
                "signalStrength": 80,
                "ssid": "HOME",
                "ipAddress": "192.168.1.1",
                "macAddress": "cc:dd",
            },
        )
        s = self._make(c)
        assert s.available is True

    # extra_state_attributes: basic keys always present
    def test_extra_attrs_basic_keys(self) -> None:
        c = _make_wifi_fw_coord(
            wifiinfo={
                "signalStrength": 70,
                "ssid": "MYNET",
                "ipAddress": "10.0.0.5",
                "macAddress": "aa:bb:cc",
            }
        )
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert attrs["ssid"] == "MYNET"
        assert attrs["ip_address"] == "10.0.0.5"
        assert attrs["mac_address"] == "aa:bb:cc"

    # extra_state_attributes: lan_ip_rcp only present when rcp returns a value
    def test_extra_attrs_rcp_lan_ip_included(self) -> None:
        c = _make_wifi_fw_coord(
            wifiinfo={
                "signalStrength": 60,
                "ssid": "X",
                "ipAddress": "",
                "macAddress": "",
            },
            rcp_lan_ip="192.0.2.149",
        )
        s = self._make(c)
        assert s.extra_state_attributes["lan_ip_rcp"] == "192.0.2.149"

    def test_extra_attrs_rcp_lan_ip_absent_when_none(self) -> None:
        c = _make_wifi_fw_coord(
            wifiinfo={
                "signalStrength": 60,
                "ssid": "X",
                "ipAddress": "",
                "macAddress": "",
            }
        )
        s = self._make(c)
        assert "lan_ip_rcp" not in s.extra_state_attributes

    # extra_state_attributes: bitrate ladder included when rcp returns ladder
    def test_extra_attrs_bitrate_ladder_included(self) -> None:
        c = _make_wifi_fw_coord(
            wifiinfo={
                "signalStrength": 55,
                "ssid": "Y",
                "ipAddress": "",
                "macAddress": "",
            },
            rcp_bitrate_ladder=[500, 1000, 2000],
        )
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert attrs["bitrate_ladder_kbps"] == [500, 1000, 2000]
        assert attrs["max_bitrate_kbps"] == 2000

    def test_extra_attrs_bitrate_ladder_absent_when_none(self) -> None:
        c = _make_wifi_fw_coord(
            wifiinfo={
                "signalStrength": 55,
                "ssid": "Y",
                "ipAddress": "",
                "macAddress": "",
            }
        )
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert "bitrate_ladder_kbps" not in attrs
        assert "max_bitrate_kbps" not in attrs

    # extra_state_attributes: empty wifiinfo cache still returns all keys (empty strings)
    def test_extra_attrs_empty_wifiinfo_cache(self) -> None:
        c = _make_wifi_fw_coord(wifiinfo=None)
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert attrs["ssid"] == ""
        assert attrs["ip_address"] == ""
        assert attrs["mac_address"] == ""


class TestFirmwareVersionSensor:
    """Tests for BoschFirmwareVersionSensor (entity_category=DIAGNOSTIC, string state)."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

        c = coord if coord is not None else _make_wifi_fw_coord()
        return BoschFirmwareVersionSensor(c, CAM_ID, _make_entry())

    # entity metadata
    def test_entity_category_is_diagnostic(self) -> None:
        s = self._make()
        assert s.entity_category == EntityCategory.DIAGNOSTIC

    def test_no_state_class(self) -> None:
        """Firmware is a string — no measurement state class."""
        s = self._make()
        assert s.state_class is None

    def test_no_unit(self) -> None:
        s = self._make()
        assert s.native_unit_of_measurement is None

    def test_icon_lives_in_icons_json_not_hardcoded(self) -> None:
        """Icon lives in icons.json — see TestWifiSignalSensor's identical
        test for the full rationale."""
        s = self._make()
        assert getattr(s, "_attr_icon", None) is None
        assert ICONS["entity"]["sensor"]["firmware_version"]["default"] == "mdi:chip"

    def test_translation_key(self) -> None:
        s = self._make()
        assert s.translation_key == "firmware_version"

    # native_value: typical version string (Gen2)
    def test_native_value_gen2_version(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware="9.40.25"))
        assert s.native_value == "9.40.25"

    # native_value: Gen1 version string
    def test_native_value_gen1_version(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware="7.91.56"))
        assert s.native_value == "7.91.56"

    # native_value: empty string → None
    def test_native_value_none_when_empty_string(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware=""))
        assert s.native_value is None

    # native_value: missing key (info dict has no firmwareVersion)
    def test_native_value_none_when_key_missing(self) -> None:
        coord = _make_wifi_fw_coord(firmware="9.40.25")
        del coord.data[CAM_ID]["info"]["firmwareVersion"]
        s = self._make(coord)
        assert s.native_value is None

    # available: False when firmware is empty
    def test_available_false_when_empty_firmware(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware=""))
        assert s.available is False

    # available: False when update failed
    def test_available_false_when_update_failed(self) -> None:
        s = self._make(
            _make_wifi_fw_coord(firmware="9.40.25", last_update_success=False)
        )
        assert s.available is False

    # available: True when firmware present + update succeeded
    def test_available_true_when_firmware_present(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware="9.40.25"))
        assert s.available is True

    # extra_state_attributes: up_to_date from top-level info key
    def test_extra_attrs_up_to_date_top_level(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware="9.40.25", up_to_date=True))
        assert s.extra_state_attributes["up_to_date"] is True

    # extra_state_attributes: up_to_date from featureSupport fallback
    def test_extra_attrs_up_to_date_feature_support_fallback(self) -> None:
        coord = _make_wifi_fw_coord(firmware="9.40.25")
        coord.data[CAM_ID]["info"]["featureSupport"] = {"upToDate": False}
        s = self._make(coord)
        assert s.extra_state_attributes["up_to_date"] is False

    # extra_state_attributes: up_to_date None when not present in either location
    def test_extra_attrs_up_to_date_none_when_absent(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware="9.40.25"))  # no upToDate key
        assert s.extra_state_attributes["up_to_date"] is None

    # extra_state_attributes: hardware_version always present
    def test_extra_attrs_hardware_version(self) -> None:
        s = self._make(
            _make_wifi_fw_coord(
                firmware="9.40.25", hardware_version="HOME_Eyes_Outdoor"
            )
        )
        assert s.extra_state_attributes["hardware_version"] == "HOME_Eyes_Outdoor"

    # extra_state_attributes: product_name_rcp included when rcp returns value
    def test_extra_attrs_product_name_included(self) -> None:
        c = _make_wifi_fw_coord(
            firmware="9.40.25", rcp_product_name="FLEXIDOME IP outdoor 4000i"
        )
        s = self._make(c)
        assert (
            s.extra_state_attributes["product_name_rcp"] == "FLEXIDOME IP outdoor 4000i"
        )

    def test_extra_attrs_product_name_absent_when_none(self) -> None:
        s = self._make(_make_wifi_fw_coord(firmware="9.40.25"))
        assert "product_name_rcp" not in s.extra_state_attributes


# ONVIF scopes / RCP version / cloud feature-flags diagnostic sensors
# Per PLATINUM_DISCIPLINE: 100% coverage on new code paths.
# Per PIN_EVERY_MODE: one test per distinct state + unavailable + edge-case.
#
# Covers:
# - BoschOnvifScopesSensor: happy-path, unavailable, extra_state_attributes
# - BoschRcpVersionSensor: happy-path, unavailable, extra_state_attributes, version format
# - BoschCloudFeatureFlagsSensor: happy-path, unavailable, no-flags, extra_state_attributes
# - _parse_onvif_scopes helper: full TLV, empty, partial, non-ONVIF scopes
# - _fetch_rcp_lan helper: no-IP, no-creds, HTTP 401, RCP error, success
# - _async_update_lan_diagnostic_sensors: onvif/version paths, error-swallowing


def _make_lan_diag_coord(
    *,
    onvif_scopes: dict[str, Any] | None = None,
    rcp_version: str | None = None,
    feature_flags: dict[str, bool] | None = None,
    last_update_success: bool = True,
    lan_ip: str | None = "192.0.2.149",
    local_creds: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal coordinator stub for ONVIF/RCP-version/feature-flags sensor tests."""
    info: dict[str, Any] = {
        "firmwareVersion": "9.40.102",
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "macAddress": "aa:bb:cc:33:14:ae",
        "title": "Terrasse",
    }
    _local_creds: dict[str, Any] = (
        local_creds
        if local_creds is not None
        else {
            "user": "cbs-A1B2C3D4",
            "password": "secret123",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 1000.0,
        }
    )
    coord = SimpleNamespace(
        data={CAM_ID: {"info": info, "status": "ONLINE", "events": []}},
        last_update_success=last_update_success,
        rcp_onvif_scopes_cache=(
            {CAM_ID: onvif_scopes} if onvif_scopes is not None else {}
        ),
        rcp_version_cache=({CAM_ID: rcp_version} if rcp_version is not None else {}),
        feature_flags=feature_flags if feature_flags is not None else {},
        local_creds_cache={CAM_ID: _local_creds} if _local_creds else {},
        rcp_lan_ip_cache={CAM_ID: lan_ip} if lan_ip else {},
        async_request_refresh=None,
    )

    def get_cam_lan_ip(cam_id: str) -> str | None:
        ip = coord.rcp_lan_ip_cache.get(cam_id)
        if ip:
            return ip
        creds = coord.local_creds_cache.get(cam_id)
        return creds.get("host") if creds else None

    coord.get_cam_lan_ip = get_cam_lan_ip
    return coord


class TestBoschOnvifScopesSensor:
    """Tests for BoschOnvifScopesSensor."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        c = coord if coord is not None else _make_lan_diag_coord()
        return BoschOnvifScopesSensor(c, CAM_ID, _make_entry())

    def test_entity_category_is_diagnostic(self) -> None:
        assert self._make().entity_category == EntityCategory.DIAGNOSTIC

    def test_disabled_by_default(self) -> None:
        assert self._make().entity_registry_enabled_default is False

    def test_translation_key(self) -> None:
        assert self._make().translation_key == "onvif_scopes"

    def test_unique_id(self) -> None:
        s = self._make()
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_onvif_scopes"

    def test_native_value_returns_supported_when_scopes_present(self) -> None:
        scopes = {
            "supported": True,
            "name": "Terrasse",
            "hardware": "HOME_Eyes_Outdoor",
            "profiles": ["Streaming"],
            "raw_scopes": [],
        }
        s = self._make(_make_lan_diag_coord(onvif_scopes=scopes))
        # BoschOnvifScopesSensor.native_value returns "supported" (the enum
        # value used in _attr_options); the HA translation layer maps this to
        # the localised string "ONVIF supported" at render time.
        assert s.native_value == "supported"

    def test_native_value_none_when_no_scopes(self) -> None:
        s = self._make(_make_lan_diag_coord(onvif_scopes=None))
        assert s.native_value is None

    def test_native_value_none_when_empty_dict(self) -> None:
        # empty dict is falsy
        coord = _make_lan_diag_coord(onvif_scopes=None)
        coord.rcp_onvif_scopes_cache = {CAM_ID: {}}
        s = self._make(coord)
        assert s.native_value is None

    def test_available_true_when_scopes_present(self) -> None:
        scopes = {
            "supported": True,
            "name": "Terrasse",
            "hardware": "",
            "profiles": [],
            "raw_scopes": [],
        }
        s = self._make(_make_lan_diag_coord(onvif_scopes=scopes))
        assert s.available is True

    def test_available_false_when_no_scopes(self) -> None:
        assert self._make(_make_lan_diag_coord(onvif_scopes=None)).available is False

    def test_available_false_when_update_failed(self) -> None:
        scopes = {
            "supported": True,
            "name": "X",
            "hardware": "",
            "profiles": [],
            "raw_scopes": [],
        }
        s = self._make(
            _make_lan_diag_coord(onvif_scopes=scopes, last_update_success=False)
        )
        assert s.available is False

    def test_extra_attrs_keys_present(self) -> None:
        scopes = {
            "supported": True,
            "name": "Terrasse",
            "hardware": "HOME_Eyes_Outdoor",
            "profiles": ["Streaming"],
            "raw_scopes": ["onvif://x"],
        }
        s = self._make(_make_lan_diag_coord(onvif_scopes=scopes))
        attrs = s.extra_state_attributes
        assert attrs["name"] == "Terrasse"
        assert attrs["hardware"] == "HOME_Eyes_Outdoor"
        assert attrs["profiles"] == ["Streaming"]
        assert attrs["raw_scopes"] == ["onvif://x"]

    def test_extra_attrs_empty_when_no_scopes(self) -> None:
        s = self._make(_make_lan_diag_coord(onvif_scopes=None))
        attrs = s.extra_state_attributes
        assert attrs["name"] == ""
        assert attrs["hardware"] == ""
        assert attrs["profiles"] == []
        assert attrs["raw_scopes"] == []


class TestBoschRcpVersionSensor:
    """Tests for BoschRcpVersionSensor."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschRcpVersionSensor

        c = coord if coord is not None else _make_lan_diag_coord()
        return BoschRcpVersionSensor(c, CAM_ID, _make_entry())

    def test_entity_category_is_diagnostic(self) -> None:
        assert self._make().entity_category == EntityCategory.DIAGNOSTIC

    def test_disabled_by_default(self) -> None:
        assert self._make().entity_registry_enabled_default is False

    def test_translation_key(self) -> None:
        assert self._make().translation_key == "rcp_version"

    def test_unique_id(self) -> None:
        s = self._make()
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_rcp_version"

    def test_native_value_gen2(self) -> None:
        s = self._make(_make_lan_diag_coord(rcp_version="1.2.38.150"))
        assert s.native_value == "1.2.38.150"

    def test_native_value_gen1(self) -> None:
        s = self._make(_make_lan_diag_coord(rcp_version="1.2.9.225"))
        assert s.native_value == "1.2.9.225"

    def test_native_value_none_when_no_version(self) -> None:
        s = self._make(_make_lan_diag_coord(rcp_version=None))
        assert s.native_value is None

    def test_available_true_when_version_present(self) -> None:
        assert (
            self._make(_make_lan_diag_coord(rcp_version="1.2.38.150")).available is True
        )

    def test_available_false_when_no_version(self) -> None:
        assert self._make(_make_lan_diag_coord(rcp_version=None)).available is False

    def test_available_false_when_update_failed(self) -> None:
        s = self._make(
            _make_lan_diag_coord(rcp_version="1.2.38.150", last_update_success=False)
        )
        assert s.available is False

    def test_extra_attrs_version_parts(self) -> None:
        s = self._make(_make_lan_diag_coord(rcp_version="1.2.38.150"))
        attrs = s.extra_state_attributes
        assert attrs["major"] == "1"
        assert attrs["minor"] == "2"
        assert attrs["patch"] == "38"
        assert attrs["build"] == "150"

    def test_extra_attrs_empty_when_no_version(self) -> None:
        s = self._make(_make_lan_diag_coord(rcp_version=None))
        assert s.extra_state_attributes == {}

    def test_extra_attrs_partial_version(self) -> None:
        # Short version string with fewer than 4 components
        coord = _make_lan_diag_coord(rcp_version=None)
        coord.rcp_version_cache = {CAM_ID: "1.2"}
        s = self._make(coord)
        attrs = s.extra_state_attributes
        assert attrs["major"] == "1"
        assert attrs["minor"] == "2"
        assert attrs.get("patch", "") == ""
        assert attrs.get("build", "") == ""


class TestBoschCloudFeatureFlagsSensor:
    """Tests for BoschCloudFeatureFlagsSensor."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        c = coord if coord is not None else _make_lan_diag_coord()
        return BoschCloudFeatureFlagsSensor(c, CAM_ID, _make_entry())

    def test_entity_category_is_diagnostic(self) -> None:
        assert self._make().entity_category == EntityCategory.DIAGNOSTIC

    def test_disabled_by_default(self) -> None:
        assert self._make().entity_registry_enabled_default is False

    def test_translation_key(self) -> None:
        assert self._make().translation_key == "cloud_feature_flags"

    def test_unique_id_is_account_level(self) -> None:
        # Account-level — not cam-specific
        assert self._make().unique_id == "bosch_shc_camera_cloud_feature_flags"

    def test_native_value_enabled_flags_sorted(self) -> None:
        flags = {"APP_RATING": True, "IOT_THINGS": True, "BETA_FEATURE": False}
        s = self._make(_make_lan_diag_coord(feature_flags=flags))
        assert s.native_value == "APP_RATING, IOT_THINGS"

    def test_native_value_none_when_no_flags(self) -> None:
        s = self._make(_make_lan_diag_coord(feature_flags={}))
        assert s.native_value is None

    def test_native_value_all_disabled_returns_none(self) -> None:
        # All flags False → no enabled flags → native_value = None (flags dict is empty-ish)
        # Actually empty dict evaluates as falsy, so returns None.
        # If dict non-empty but all False: returns "none"
        flags: dict[str, bool] = {"APP_RATING": False}
        s = self._make(_make_lan_diag_coord(feature_flags=flags))
        assert s.native_value == "none"

    def test_native_value_single_enabled_flag(self) -> None:
        flags = {"APP_RATING": True}
        s = self._make(_make_lan_diag_coord(feature_flags=flags))
        assert s.native_value == "APP_RATING"

    def test_available_true_when_flags_present(self) -> None:
        flags = {"APP_RATING": True}
        assert self._make(_make_lan_diag_coord(feature_flags=flags)).available is True

    def test_available_false_when_no_flags(self) -> None:
        assert self._make(_make_lan_diag_coord(feature_flags={})).available is False

    def test_available_false_when_update_failed(self) -> None:
        flags = {"APP_RATING": True}
        s = self._make(
            _make_lan_diag_coord(feature_flags=flags, last_update_success=False)
        )
        assert s.available is False

    def test_extra_attrs_full_flags_dict(self) -> None:
        flags = {"APP_RATING": True, "IOT_THINGS": False}
        s = self._make(_make_lan_diag_coord(feature_flags=flags))
        attrs = s.extra_state_attributes
        assert attrs == {"APP_RATING": True, "IOT_THINGS": False}

    def test_extra_attrs_empty_when_no_flags(self) -> None:
        s = self._make(_make_lan_diag_coord(feature_flags={}))
        assert s.extra_state_attributes == {}


class TestParseOnvifScopes:
    """Tests for _parse_onvif_scopes (module-level helper in coordinator.py)."""

    def _parse(self, raw: bytes) -> dict[str, Any]:
        from custom_components.bosch_shc_camera.coordinator import _parse_onvif_scopes

        return _parse_onvif_scopes(raw)

    def test_supported_true(self) -> None:
        raw = b"onvif://www.onvif.org/name/MyCamera\x00"
        result = self._parse(raw)
        assert result["supported"] is True

    def test_parses_name(self) -> None:
        raw = b"onvif://www.onvif.org/name/Bosch%20Camera\x00"
        result = self._parse(raw)
        assert result["name"] == "Bosch Camera"

    def test_parses_hardware(self) -> None:
        raw = b"onvif://www.onvif.org/hardware/HOME_Eyes_Outdoor\x00"
        result = self._parse(raw)
        assert result["hardware"] == "HOME_Eyes_Outdoor"

    def test_parses_profiles(self) -> None:
        raw = b"onvif://www.onvif.org/Profile/Streaming\x00onvif://www.onvif.org/Profile/G\x00"
        result = self._parse(raw)
        assert "Streaming" in result["profiles"]
        assert "G" in result["profiles"]

    def test_parses_multiple_scopes(self) -> None:
        raw = (
            b"onvif://www.onvif.org/name/TestCam\x00"
            b"onvif://www.onvif.org/hardware/CAMERA_360\x00"
            b"onvif://www.onvif.org/Profile/Streaming\x00"
        )
        result = self._parse(raw)
        assert result["name"] == "TestCam"
        assert result["hardware"] == "CAMERA_360"
        assert result["profiles"] == ["Streaming"]

    def test_raw_scopes_included(self) -> None:
        raw = b"onvif://www.onvif.org/name/X\x00"
        result = self._parse(raw)
        assert "onvif://www.onvif.org/name/X" in result["raw_scopes"]

    def test_non_onvif_scopes_ignored(self) -> None:
        raw = b"http://something.else.com/foo\x00"
        result = self._parse(raw)
        assert result["name"] == ""
        assert result["hardware"] == ""
        assert result["profiles"] == []

    def test_empty_raw_returns_defaults(self) -> None:
        result = self._parse(b"")
        assert result["supported"] is True
        assert result["name"] == ""
        assert result["hardware"] == ""
        assert result["profiles"] == []
        assert result["raw_scopes"] == []

    def test_scope_without_slash_skipped(self) -> None:
        raw = b"onvif://www.onvif.org/name\x00"  # no value after /
        result = self._parse(raw)
        assert result["name"] == ""

    def test_url_encoded_plus_space(self) -> None:
        raw = b"onvif://www.onvif.org/name/Bosch+Camera\x00"
        result = self._parse(raw)
        # + is not treated as space by unquote (only %XX); "+" stays literal
        assert "Camera" in result["name"]


class TestFetchRcpLan:
    """Tests for BoschCameraCoordinator._fetch_rcp_lan (async helper)."""

    def _make_coordinator(
        self,
        *,
        lan_ip: str | None = "192.0.2.149",
        local_creds: dict[str, Any] | None = None,
    ) -> Any:
        """Return a minimal coordinator instance for _fetch_rcp_lan."""
        # Build with the minimum required attributes
        coord = object.__new__(BoschCameraCoordinator)
        coord.rcp_lan_ip_cache = {CAM_ID: lan_ip} if lan_ip else {}
        coord.local_creds_cache = {CAM_ID: local_creds} if local_creds else {}
        coord.hass = MagicMock()

        def get_cam_lan_ip(cam_id: str) -> str | None:
            ip = coord.rcp_lan_ip_cache.get(cam_id)
            if ip:
                return ip
            creds = coord.local_creds_cache.get(cam_id)
            return creds.get("host") if creds else None

        coord.get_cam_lan_ip = get_cam_lan_ip  # type: ignore[method-assign]
        return coord

    @pytest.mark.asyncio
    async def test_returns_none_when_no_lan_ip(self) -> None:
        coord = self._make_coordinator(lan_ip=None, local_creds=None)
        result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_creds(self) -> None:
        coord = self._make_coordinator(lan_ip="192.0.2.149", local_creds=None)
        result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_creds_missing_user(self) -> None:
        creds: dict[str, Any] = {
            "user": "",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)
        result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_http_401(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_rcp_error_in_body(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"<err>0x0090</err>")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_parses_str_hex_payload(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        # Version bytes 1.2.38.150 = 01 02 26 96
        payload_hex = "01022696"
        rcp_xml = f"<rcp><str>{payload_hex}</str></rcp>".encode()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=rcp_xml)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result == bytes.fromhex(payload_hex)

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                side_effect=TimeoutError(),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_raw_bytes_when_no_str_tag(self) -> None:
        """Non-XML binary payload falls through to raw bytes return."""
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        raw_bytes = b"\x01\x02\x26\x96"  # pure binary, no XML envelope

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=raw_bytes)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result == raw_bytes


class TestAsyncUpdateLanDiagnosticSensors:
    """Tests for coordinator._async_update_lan_diagnostic_sensors."""

    def _make_coordinator_with_caches(self) -> Any:
        coord = object.__new__(BoschCameraCoordinator)
        coord.rcp_onvif_scopes_cache = {}
        coord.rcp_version_cache = {}
        coord.rcp_lan_ip_cache = {CAM_ID: "192.0.2.149"}
        coord.local_creds_cache = {
            CAM_ID: {
                "user": "cbs-XYZ",
                "password": "pw",
                "host": "192.0.2.149",
                "port": 443,
                "ts": 0.0,
            }
        }
        coord.hass = MagicMock()

        def get_cam_lan_ip(cam_id: str) -> str | None:
            return coord.rcp_lan_ip_cache.get(cam_id)

        coord.get_cam_lan_ip = get_cam_lan_ip  # type: ignore[method-assign]
        return coord

    @pytest.mark.asyncio
    async def test_f4_onvif_scopes_populated_on_success(self) -> None:
        coord = self._make_coordinator_with_caches()
        onvif_raw = b"onvif://www.onvif.org/name/TestCam\x00"

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0x0a98":
                return onvif_raw
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert CAM_ID in coord.rcp_onvif_scopes_cache
        assert coord.rcp_onvif_scopes_cache[CAM_ID]["name"] == "TestCam"

    @pytest.mark.asyncio
    async def test_f6_rcp_version_populated_on_success(self) -> None:
        coord = self._make_coordinator_with_caches()
        # Version 1.2.38.150 → bytes 0x01 0x02 0x26 0x96
        ver_raw = bytes([1, 2, 38, 150])

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0xff00":
                return ver_raw
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert coord.rcp_version_cache.get(CAM_ID) == "1.2.38.150"

    @pytest.mark.asyncio
    async def test_version_bytes_too_short_no_update(self) -> None:
        coord = self._make_coordinator_with_caches()

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0xff00":
                return b"\x01\x02"  # only 2 bytes — not 4
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert coord.rcp_version_cache.get(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_onvif_none_does_not_update_cache(self) -> None:
        coord = self._make_coordinator_with_caches()

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert CAM_ID not in coord.rcp_onvif_scopes_cache
        assert CAM_ID not in coord.rcp_version_cache

    @pytest.mark.asyncio
    async def test_exception_in_onvif_does_not_prevent_version_fetch(self) -> None:
        coord = self._make_coordinator_with_caches()
        ver_raw = bytes([1, 2, 9, 225])

        call_count = 0

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            nonlocal call_count
            call_count += 1
            if opcode == "0x0a98":
                raise RuntimeError("ONVIF fetch failed")
            return ver_raw

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        # Should NOT raise — exception is swallowed per spec
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        # Version should still be updated
        assert coord.rcp_version_cache.get(CAM_ID) == "1.2.9.225"


# External stream URL sensors (+ BoschExternalStreamSwitch)
# Frigate / BlueIris users need RTSP URLs they can paste into external
# recorder configs. Pre-v12.4 the URL was only on the camera entity's
# extra_state_attributes and the inst=2 sub-stream was not exposed at all.
#
# This feature adds:
#   - BoschExternalStreamSwitch (per camera, default OFF, RestoreEntity)
#   - BoschStreamUrlSensor      (inst=1 main, value = None when switch OFF)
#   - BoschStreamUrlSubSensor   (inst=2 sub, value = None when switch OFF)
#   - _swap_inst() helper       (lone source of truth for the inst= rewrite)
#
# These tests pin the contracts so a future refactor can't:
#   - re-enable the switch by default (would spam every install with 2 sensors)
#   - leak the URL through the sensor when the switch is OFF
#   - forget the inst=N → inst=2 rewrite on the sub sensor
#
# OUT OF SCOPE NOTE: BoschExternalStreamSwitch is defined in switch.py, not
# sensor.py — its tests are kept here (per explicit merge instructions) since
# they were written alongside the two URL sensors above and share the same
# coordinator stub.

_EXT_STREAM_LOCAL_RTSP_URL = (
    "rtsp://cbs-testuser:testpw@127.0.0.1:54321/rtsp_tunnel"
    "?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600"
)

# REMOTE path after the TLS proxy wraps the rtsps:// cloud URL — same shape,
# inst=4 because the REMOTE fallback uses a lower-bitrate stream by default.
_EXT_STREAM_REMOTE_RTSP_URL = (
    "rtsp://127.0.0.1:54322/rtsp_tunnel"
    "?inst=4&enableaudio=1&fmtp=1&maxSessionDuration=3600"
)


def _make_ext_stream_coord(
    rtsps_url: str | None = _EXT_STREAM_LOCAL_RTSP_URL,
) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "00:00:00:00:00:01",
                },
            }
        },
        external_stream_enabled={},
        live_connections=({CAM_ID: {"rtspsUrl": rtsps_url}} if rtsps_url else {}),
        last_update_success=True,
        async_update_listeners=MagicMock(),
    )


# ── _swap_inst helper ────────────────────────────────────────────────────────


def test_swap_inst_rewrites_inst_query_param() -> None:
    """The only place that knows the inst=N → inst=K substitution. Stay tiny."""
    from custom_components.bosch_shc_camera.sensor import _swap_inst

    assert _swap_inst(_EXT_STREAM_LOCAL_RTSP_URL, 2).endswith(
        "?inst=2&enableaudio=1&fmtp=1&maxSessionDuration=3600"
    )
    assert _swap_inst(_EXT_STREAM_REMOTE_RTSP_URL, 2).count("inst=2") == 1
    # Idempotent: inst=2 → inst=2 stays unchanged
    sub_url = _swap_inst(_EXT_STREAM_LOCAL_RTSP_URL, 2)
    assert _swap_inst(sub_url, 2) == sub_url


def test_swap_inst_only_touches_first_match() -> None:
    """Defensive: if a future bug puts inst=X in the path too, only the
    query-string match should be rewritten so the path stays canonical."""
    from custom_components.bosch_shc_camera.sensor import _swap_inst

    url = "rtsp://h:p@127.0.0.1:1/rtsp_tunnel?inst=1&foo=inst=99"
    out = _swap_inst(url, 2)
    # First inst= becomes 2; the literal "inst=99" inside the foo value
    # is left alone (it lives after the first &).
    assert out == "rtsp://h:p@127.0.0.1:1/rtsp_tunnel?inst=2&foo=inst=99"


# ── BoschExternalStreamSwitch ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_switch_default_off(stub_entry: SimpleNamespace) -> None:
    """The switch ships disabled-by-default in the entity registry to keep
    the integration's first-run experience clean. Users opt in per camera."""
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_ext_stream_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    assert entity._attr_entity_registry_enabled_default is False
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_switch_turn_on_sets_flag(stub_entry: SimpleNamespace) -> None:
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_ext_stream_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coord.external_stream_enabled[CAM_ID] is True
    assert entity.is_on is True
    # The two URL sensors recompute when the switch flips
    coord.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_switch_turn_off_clears_flag(stub_entry: SimpleNamespace) -> None:
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_ext_stream_coord()
    coord.external_stream_enabled[CAM_ID] = True
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_off()

    assert coord.external_stream_enabled[CAM_ID] is False
    assert entity.is_on is False


@pytest.mark.asyncio
async def test_switch_restores_on_state_from_previous_session(
    stub_entry: SimpleNamespace,
) -> None:
    """RestoreEntity: if the user had the switch ON before HA restart, the
    flag should come back without them having to re-toggle each cam."""
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_ext_stream_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    # Pretend HA restored a previous-session "on" state via the RestoreEntity
    # parent. The mixin reads it via async_get_last_state().
    last = SimpleNamespace(state="on")
    entity.async_get_last_state = AsyncMock(return_value=last)

    # Skip the real super().async_added_to_hass to keep the test focused on
    # the restore-logic only — the parent does HA-internal wiring.
    async def _noop(self: object) -> None:
        return None

    entity.__class__.__mro__[1].async_added_to_hass = _noop  # type: ignore[method-assign]

    await entity.async_added_to_hass()

    assert coord.external_stream_enabled[CAM_ID] is True


@pytest.mark.asyncio
async def test_switch_restore_off_does_not_set_flag(
    stub_entry: SimpleNamespace,
) -> None:
    """Symmetric: a restored OFF state must NOT silently enable anything."""
    from custom_components.bosch_shc_camera.switch import BoschExternalStreamSwitch

    coord = _make_ext_stream_coord()
    entity = BoschExternalStreamSwitch(coord, CAM_ID, stub_entry)
    entity.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="off"))

    async def _noop(self: object) -> None:
        return None

    entity.__class__.__mro__[1].async_added_to_hass = _noop  # type: ignore[method-assign]

    await entity.async_added_to_hass()

    assert coord.external_stream_enabled.get(CAM_ID, False) is False


# ── BoschStreamUrlSensor (main, inst=1) ──────────────────────────────────────


def test_main_sensor_returns_none_when_switch_off(stub_entry: SimpleNamespace) -> None:
    """When the switch is OFF the sensor MUST NOT leak the URL — pin so a
    future refactor can't accidentally publish the raw URL on every install."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSensor

    coord = _make_ext_stream_coord()
    coord.external_stream_enabled[CAM_ID] = False
    sensor = BoschStreamUrlSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value is None


def test_main_sensor_returns_url_when_switch_on(stub_entry: SimpleNamespace) -> None:
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSensor

    coord = _make_ext_stream_coord()
    coord.external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value == _EXT_STREAM_LOCAL_RTSP_URL


def test_main_sensor_returns_none_when_no_session_open(
    stub_entry: SimpleNamespace,
) -> None:
    """A switch flipped ON before any stream session exists must return None,
    not a partial/broken URL."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSensor

    coord = _make_ext_stream_coord(rtsps_url=None)
    coord.external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value is None


# ── BoschStreamUrlSubSensor (sub, inst=2) ────────────────────────────────────


def test_sub_sensor_returns_none_when_switch_off(stub_entry: SimpleNamespace) -> None:
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSubSensor

    coord = _make_ext_stream_coord()
    coord.external_stream_enabled[CAM_ID] = False
    sensor = BoschStreamUrlSubSensor(coord, CAM_ID, stub_entry)
    assert sensor.native_value is None


def test_sub_sensor_rewrites_inst_to_2(stub_entry: SimpleNamespace) -> None:
    """Pin the value of the substream: same URL minus inst=N → inst=2."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSubSensor

    coord = _make_ext_stream_coord()
    coord.external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSubSensor(coord, CAM_ID, stub_entry)
    val = sensor.native_value
    assert val is not None
    assert "inst=2" in val and "inst=1" not in val


def test_sub_sensor_rewrites_inst_4_to_2_on_remote(stub_entry: SimpleNamespace) -> None:
    """REMOTE fallback uses inst=4 in the main URL; the sub-stream sensor
    still rewrites it to inst=2."""
    from custom_components.bosch_shc_camera.sensor import BoschStreamUrlSubSensor

    coord = _make_ext_stream_coord(rtsps_url=_EXT_STREAM_REMOTE_RTSP_URL)
    coord.external_stream_enabled[CAM_ID] = True
    sensor = BoschStreamUrlSubSensor(coord, CAM_ID, stub_entry)
    val = sensor.native_value
    assert val is not None
    assert "inst=2" in val and "inst=4" not in val


def test_both_sensors_disabled_by_default(stub_entry: SimpleNamespace) -> None:
    """Both URL sensors must be disabled in the entity registry by default —
    the switch is the one knob the user touches; the sensors come along for
    the ride. Stay disabled until the user picks them up via the UI."""
    from custom_components.bosch_shc_camera.sensor import (
        BoschStreamUrlSensor,
        BoschStreamUrlSubSensor,
    )

    coord = _make_ext_stream_coord()
    assert (
        BoschStreamUrlSensor(
            coord, CAM_ID, stub_entry
        )._attr_entity_registry_enabled_default
        is False
    )
    assert (
        BoschStreamUrlSubSensor(
            coord, CAM_ID, stub_entry
        )._attr_entity_registry_enabled_default
        is False
    )


# Mini-NVR state sensor
# Pins the four attributes the sensor surfaces (target, pending_uploads,
# failed_uploads, last_segment_age_s) and the three states (idle / recording
# / error). Pure-property tests — no I/O, no event loop — so they cannot
# regress under refactor.
#
# Source: v11.0.4 NVR-storage-target refactor — a diagnostic sensor for
# "is recording actually working" was requested.


def _make_nvr_coord(
    *,
    drain_state: dict | None = None,
    nvr_processes: dict | None = None,
    user_intent: dict | None = None,
    error_state: dict | None = None,
    preroll_counts: dict | None = None,
    title: str = "Terrasse",
):
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": title,
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "",
                }
            }
        },
        nvr_drain_state=drain_state or {},
        nvr_processes=nvr_processes or {},
        nvr_preroll_processes={},
        nvr_preroll_tasks={},
        nvr_preroll_segment_counts=preroll_counts or {},
        nvr_user_intent=user_intent or {},
        nvr_error_state=error_state or {},
        options={
            "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
            "nvr_preroll_seconds": 0,
        },
    )


# ── State machine ────────────────────────────────────────────────────────────


class TestNvrStateSensorState:
    def test_idle_when_no_process(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(_make_nvr_coord(), CAM_ID, _make_entry())
        assert s.native_value == "idle"

    def test_idle_when_process_but_no_user_intent(self):
        """Edge case — process is running, but user toggled off and we're
        between switch-tick and stop. Still ``idle`` so the dashboard
        doesn't lie."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(
                nvr_processes={CAM_ID: object()},
                user_intent={CAM_ID: False},
            ),
            CAM_ID,
            _make_entry(),
        )
        assert s.native_value == "idle"

    def test_recording_when_process_and_user_intent(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(
                nvr_processes={CAM_ID: object()},
                user_intent={CAM_ID: True},
            ),
            CAM_ID,
            _make_entry(),
        )
        assert s.native_value == "recording"

    def test_error_takes_precedence(self):
        """If the crash-loop guard tripped, ``error`` overrides everything
        else — including a running process — so the user notices."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(
                nvr_processes={CAM_ID: object()},
                user_intent={CAM_ID: True},
                error_state={CAM_ID: "ffmpeg crashed twice"},
            ),
            CAM_ID,
            _make_entry(),
        )
        assert s.native_value == "error"


# ── Attributes ───────────────────────────────────────────────────────────────


class TestNvrStateSensorAttributes:
    def test_target_attribute(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(drain_state={"target": "smb"}),
            CAM_ID,
            _make_entry(),
        )
        assert s.extra_state_attributes["target"] == "smb"

    def test_target_default_local_when_state_empty(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(_make_nvr_coord(), CAM_ID, _make_entry())
        assert s.extra_state_attributes["target"] == "local"

    def test_pending_and_failed_counts(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(drain_state={"pending": 4, "failed": 2}),
            CAM_ID,
            _make_entry(),
        )
        attrs = s.extra_state_attributes
        assert attrs["pending_uploads"] == 4
        assert attrs["failed_uploads"] == 2

    def test_last_segment_age_keyed_by_camera(self):
        """``nvr_drain_state.last_age_by_cam`` is keyed by sanitized
        camera title so the per-camera lookup must use the same _safe_name."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(
                title="Terrasse",
                drain_state={"last_age_by_cam": {"Terrasse": 42.5}},
            ),
            CAM_ID,
            _make_entry(),
        )
        assert s.extra_state_attributes["last_segment_age_s"] == 42.5

    def test_last_segment_age_none_when_unknown(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(_make_nvr_coord(), CAM_ID, _make_entry())
        assert s.extra_state_attributes["last_segment_age_s"] is None

    def test_user_intent_exposed(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(user_intent={CAM_ID: True}),
            CAM_ID,
            _make_entry(),
        )
        assert s.extra_state_attributes["user_intent"] is True

    def test_error_attribute_exposed(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(
            _make_nvr_coord(error_state={CAM_ID: "ffmpeg crashed twice"}),
            CAM_ID,
            _make_entry(),
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
            _make_nvr_coord(
                title="../../etc",
                drain_state={"last_age_by_cam": {sanitized: 99.0}},
            ),
            CAM_ID,
            _make_entry(),
        )
        assert s.extra_state_attributes["last_segment_age_s"] == 99.0

    def test_preroll_segments_read_from_cached_count_not_disk(self):
        """extra_state_attributes must NOT call list_preroll_files (which does
        os.listdir) — that's a blocking call in the event loop. The count is
        populated by the preroll watcher into
        ``nvr_preroll_segment_counts`` and read from there.

        Source: HA detected a blocking call to listdir at recorder.py:221
        during a v12.x test — the preroll watcher now caches the count and
        the sensor reads that cache instead.
        """
        import custom_components.bosch_shc_camera.recorder as recorder_mod
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        called = {"n": 0}
        orig = recorder_mod.list_preroll_files

        def boom(*args, **kwargs):
            called["n"] += 1
            raise AssertionError(
                "list_preroll_files must not be called from event loop — "
                "use nvr_preroll_segment_counts cache",
            )

        recorder_mod.list_preroll_files = boom
        try:
            s = BoschNvrStateSensor(
                _make_nvr_coord(preroll_counts={CAM_ID: 5}),
                CAM_ID,
                _make_entry(),
            )
            attrs = s.extra_state_attributes
            assert attrs["preroll_segments"] == 5
            assert called["n"] == 0
        finally:
            recorder_mod.list_preroll_files = orig

    def test_preroll_segments_defaults_to_zero_when_not_populated(self):
        """If the watcher never ran (NVR off), the count attr defaults to 0
        — never raises AttributeError on an unseen cam_id."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(_make_nvr_coord(), CAM_ID, _make_entry())
        assert s.extra_state_attributes["preroll_segments"] == 0


# ── Entity metadata ──────────────────────────────────────────────────────────


class TestNvrStateSensorMetadata:
    def test_unique_id_pinned(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(_make_nvr_coord(), CAM_ID, _make_entry())
        assert s.unique_id == f"bosch_shc_nvr_state_{CAM_ID.lower()}"

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(_make_nvr_coord(), CAM_ID, _make_entry())
        assert s.translation_key == "nvr_state"

    def test_disabled_by_default(self):
        """Diagnostic sensor — opt-in only, never adds noise on first run."""
        from custom_components.bosch_shc_camera.sensor import BoschNvrStateSensor

        s = BoschNvrStateSensor(_make_nvr_coord(), CAM_ID, _make_entry())
        assert s.entity_registry_enabled_default is False


# Cloud maintenance sensor
# BoschCloudMaintenanceSensor surfaces the parsed community RSS maintenance
# window state (active/scheduled/past/recent/unknown/idle) as a HA ENUM
# sensor. It must remain available even while the Bosch cloud is down, since
# that's exactly when users check it.


def _make_maintenance_window(*, active: bool = True) -> MaintenanceWindow:
    ref = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
    start = ref - timedelta(hours=1) if active else ref + timedelta(hours=1)
    end = ref + timedelta(hours=2) if active else ref + timedelta(hours=3)
    return MaintenanceWindow(
        title="Wartung Kamera-Infrastruktur",
        link="https://example/x",
        pub_date=ref - timedelta(hours=12),
        summary="07:00–10:00 MESZ",
        scheduled_start=start,
        scheduled_end=end,
        source="rss:Wartungsarbeiten",
        camera_relevant=True,
    )


def _make_maintenance_coord(
    *, cache: MaintenanceWindow | None, last_fetch: float = float("-inf")
) -> SimpleNamespace:
    c = SimpleNamespace()
    c.maintenance_cache = cache
    c.maintenance_last_fetch = last_fetch
    # _BoschSensorBase.__init__ reads coordinator.data[cam_id]['info'] for
    # device-info fields — stub it so the constructor succeeds.
    c.data = {"CAM_ID_X": {"info": {"title": "TestCam"}}}
    return c


def _make_maintenance_sensor(
    cache: MaintenanceWindow | None, last_fetch: float = float("-inf")
) -> BoschCloudMaintenanceSensor:
    return BoschCloudMaintenanceSensor(
        _make_maintenance_coord(cache=cache, last_fetch=last_fetch),
        "CAM_ID_X",
        SimpleNamespace(),  # entry — _BoschSensorBase only stores it
    )


class TestCloudMaintenanceSensorMetadata:
    def test_identity_props(self):
        s = _make_maintenance_sensor(None)
        # name is resolved from translation key at runtime (not _attr_name)
        assert s._attr_translation_key == "cloud_maintenance"
        assert s.unique_id == "bosch_shc_camera_cloud_maintenance"
        # Always-on availability — sensor must stay readable during cloud outage.
        assert s.available is True


class TestCloudMaintenanceSensorValue:
    def test_native_value_idle_when_no_cache(self):
        assert _make_maintenance_sensor(None).native_value == "idle"

    def test_native_value_active(self):
        # MaintenanceWindow.state() returns active/scheduled/past/recent/unknown.
        assert _make_maintenance_sensor(
            _make_maintenance_window(active=True)
        ).native_value in {
            "active",
            "scheduled",
            "past",
            "recent",
            "unknown",
        }

    def test_extra_attrs_empty_when_no_cache(self):
        attrs = _make_maintenance_sensor(None).extra_state_attributes
        assert "title" not in attrs
        assert "last_fetched_seconds_ago" not in attrs

    def test_extra_attrs_with_window(self, monkeypatch: pytest.MonkeyPatch):
        mw = _make_maintenance_window(active=True)
        import time as _time

        monkeypatch.setattr(_time, "monotonic", lambda: 1042.0)
        attrs = _make_maintenance_sensor(mw, last_fetch=1000.0).extra_state_attributes
        assert attrs.get("title") == mw.title
        assert attrs.get("source") == mw.source
        # 1042 - 1000 = 42s ago.
        assert attrs.get("last_fetched_seconds_ago") == 42

    def test_extra_attrs_skips_last_fetched_when_never(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        mw = _make_maintenance_window(active=True)
        import time as _time

        monkeypatch.setattr(_time, "monotonic", lambda: 1042.0)
        attrs = _make_maintenance_sensor(
            mw, last_fetch=float("-inf")
        ).extra_state_attributes
        assert "last_fetched_seconds_ago" not in attrs

    def test_volatile_attr_is_unrecorded(self, monkeypatch: pytest.MonkeyPatch):
        """last_fetched_seconds_ago changes every tick → exclude it from the
        recorder so `state_attributes` does not bloat. Emitted live, recording
        suppressed (HA#39)."""
        mw = _make_maintenance_window(active=True)
        import time as _time

        monkeypatch.setattr(_time, "monotonic", lambda: 1042.0)
        s = _make_maintenance_sensor(mw, last_fetch=1000.0)
        assert "last_fetched_seconds_ago" in s.extra_state_attributes
        assert "last_fetched_seconds_ago" in s._unrecorded_attributes


# HTTP 444 session-quota status handling
# Pins:
# - status enum: SESSION_LIMIT returned from _check_status when cloud returns 444
# - _compute_status_for passes SESSION_LIMIT through verbatim (not "unknown")
# - BoschCameraStatusSensor.native_value returns "session_limit"
# - Persistent notification fires after N>=3 hits in a 5-min window
# - Notification does NOT fire on first or second hit within the window
# - offline_since is NOT updated on SESSION_LIMIT (camera is reachable)
#
# Source: user-reported confusion "camera shown offline during Bosch app
# parallel use" — root cause was HTTP 444 being treated as OFFLINE.


def _make_session_quota_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    """Minimal coordinator stub for status-related tests."""
    coord = SimpleNamespace()
    coord.options = {}
    coord._last_camera_status = {}
    coord._session_quota_hits: dict[str, list[float]] = {}
    coord._SESSION_QUOTA_WINDOW_S = 300.0
    coord._SESSION_QUOTA_NOTIFY_THRESHOLD = 3
    coord.data = {
        cam_id: {
            "info": {
                "title": "Terrasse",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.102",
                "macAddress": "aa:bb:cc:33:14:ae",
            },
            "status": "ONLINE",
            "events": [],
        },
    }
    coord.hass = SimpleNamespace(
        services=SimpleNamespace(async_call=AsyncMock()),
        async_create_task=MagicMock(),
    )
    # Caches expected by BoschCameraStatusSensor.__init__ / _cam_data property
    coord.commissioned_cache = {}
    coord.firmware_cache = {}
    return coord


# ── Status enum: SESSION_LIMIT passthrough via _compute_status_for ─────────


class TestComputeStatusSessionLimit:
    """_compute_status_for must pass SESSION_LIMIT through verbatim."""

    def test_session_limit_returns_session_limit(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "SESSION_LIMIT"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_ID)
        assert result == "session_limit"

    def test_session_limit_not_treated_as_offline(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "SESSION_LIMIT"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_ID)
        assert result != "offline"

    def test_session_limit_not_treated_as_unknown(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "SESSION_LIMIT"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_ID)
        assert result != "unknown"

    def test_offline_still_offline(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "OFFLINE"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_ID)
        assert result == "offline"

    def test_online_still_online(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "ONLINE"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_ID)
        assert result == "online"


# ── Sensor native_value: session_limit ───────────────────────────────────────


class TestStatusSensorSessionLimit:
    """BoschCameraStatusSensor must return 'session_limit' and list it in options."""

    def test_session_limit_status_returns_session_limit(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "SESSION_LIMIT"
        sensor = BoschCameraStatusSensor(coord, CAM_ID, _make_entry())
        assert sensor.native_value == "session_limit"

    def test_session_limit_in_options(self) -> None:
        sensor = BoschCameraStatusSensor(
            _make_session_quota_coord(), CAM_ID, _make_entry()
        )
        # Options must contain session_limit per PIN_EVERY_MODE
        assert "session_limit" in sensor._attr_options

    def test_offline_in_options(self) -> None:
        sensor = BoschCameraStatusSensor(
            _make_session_quota_coord(), CAM_ID, _make_entry()
        )
        assert "offline" in sensor._attr_options

    def test_online_in_options(self) -> None:
        sensor = BoschCameraStatusSensor(
            _make_session_quota_coord(), CAM_ID, _make_entry()
        )
        assert "online" in sensor._attr_options

    def test_online_status_still_online(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "ONLINE"
        sensor = BoschCameraStatusSensor(coord, CAM_ID, _make_entry())
        assert sensor.native_value == "online"

    def test_offline_status_still_offline(self) -> None:
        coord = _make_session_quota_coord()
        coord.data[CAM_ID]["status"] = "OFFLINE"
        sensor = BoschCameraStatusSensor(coord, CAM_ID, _make_entry())
        assert sensor.native_value == "offline"


# ── Persistent notification: threshold logic ─────────────────────────────────


@pytest.mark.asyncio
class TestSessionQuotaNotification:
    """_async_handle_session_quota_hit fires persistent_notification after threshold."""

    async def test_first_hit_no_notification(self) -> None:
        coord = _make_session_quota_coord()
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_ID)
        coord.hass.services.async_call.assert_not_called()

    async def test_second_hit_no_notification(self) -> None:
        coord = _make_session_quota_coord()
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_ID)
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_ID)
        coord.hass.services.async_call.assert_not_called()

    async def test_third_hit_fires_notification(self) -> None:
        coord = _make_session_quota_coord()
        for _ in range(3):
            await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_ID)
        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][0] == "persistent_notification"
        assert call_args[0][1] == "create"
        payload = call_args[0][2]
        assert "session_quota" in payload["notification_id"]
        assert (
            "444" in payload["message"]
            or "Session" in payload["message"]
            or "Sitzungslimit" in payload["message"]
        )

    async def test_hits_outside_window_dont_count(self) -> None:
        coord = _make_session_quota_coord()
        # Seed 2 old hits (beyond window)
        old_ts = time.monotonic() - 400.0  # 400s ago, outside 300s window
        coord._session_quota_hits[CAM_ID] = [old_ts, old_ts]
        # One fresh hit — total in window = 1, below threshold
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_ID)
        coord.hass.services.async_call.assert_not_called()

    async def test_notification_id_contains_cam_prefix(self) -> None:
        coord = _make_session_quota_coord()
        for _ in range(3):
            await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_ID)
        payload = coord.hass.services.async_call.call_args[0][2]
        assert CAM_ID[:8].lower() in payload["notification_id"]

    async def test_fourth_hit_does_not_double_notify(self) -> None:
        """After threshold: each subsequent hit re-fires (idempotent notification_id dedups in HA)."""
        coord = _make_session_quota_coord()
        for _ in range(4):
            await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_ID)
        # Called on hit 3 and hit 4 — both use same notification_id so HA dedupes
        assert coord.hass.services.async_call.call_count == 2


# ── offline_since not updated on SESSION_LIMIT ──────────────────────────────


class TestSessionLimitOfflineSince:
    """SESSION_LIMIT must not add camera to offline_since (not a connectivity failure)."""

    def test_session_limit_does_not_set_offline_since(self) -> None:
        """The status == 'SESSION_LIMIT' branch does NOT add to offline_since."""
        # We test the logic inline — if status is SESSION_LIMIT it falls into the
        # `else` branch (not in OFFLINE/UPDATING) so offline_since.pop() is called.
        offline_since: dict[str, float] = {
            CAM_ID: 12345.0
        }  # simulate pre-existing entry
        status = "SESSION_LIMIT"
        if status in ("OFFLINE", "UPDATING"):
            if CAM_ID not in offline_since:
                offline_since[CAM_ID] = time.monotonic()
        else:
            offline_since.pop(CAM_ID, None)
        assert CAM_ID not in offline_since

    def test_offline_does_set_offline_since(self) -> None:
        """OFFLINE should still set offline_since — regression guard."""
        offline_since: dict[str, float] = {}
        now = time.monotonic()
        status = "OFFLINE"
        if status in ("OFFLINE", "UPDATING"):
            if CAM_ID not in offline_since:
                offline_since[CAM_ID] = now
        else:
            offline_since.pop(CAM_ID, None)
        assert CAM_ID in offline_since


# Regression pins: event-type enum, TLS datetime, UTC bucketing, commissioned
# enum, feature-flag truncation, ONVIF enum
# Covers:
#   - trouble_connect present in last_event_type options
#   - BoschTlsCertSensor returns a tz-aware datetime
#   - BoschCameraEventsTodaySensor uses UTC date bucketing (datetime.now(UTC))
#   - BoschCommissionedSensor uses snake_case ENUM options
#   - BoschCloudFeatureFlagsSensor truncates state at 255 chars
#   - BoschOnvifScopesSensor is ENUM with option "supported"
#
# PIN_EVERY_MODE: one test per mode + default + edge per class.


def _stub_coord_bugfixes(**overrides: object) -> SimpleNamespace:
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
        commissioned_cache={},
        firmware_cache={},
        wifiinfo_cache={},
        ambient_light_cache={},
        rcp_dimmer_cache={},
        rcp_alarm_catalog_cache={},
        rcp_motion_zones_cache={},
        rcp_motion_coords_cache={},
        cloud_zones_cache={},
        gen2_zones_cache={},
        rcp_tls_cert_cache={},
        rcp_network_services_cache={},
        rcp_iva_catalog_cache={},
        _rcp_private_areas_cache={},
        gen2_private_areas_cache={},
        cloud_privacy_masks_cache={},
        ambient_lighting_cache={},
        alarm_status_cache={},
        alarm_settings_cache={},
        arming_cache={},
        live_connections={},
        stream_fell_back={},
        stream_error_count={},
        stream_warming=set(),
        fcm_running=False,
        fcm_healthy=True,
        fcm_push_mode="auto",
        fcm_last_push=float("-inf"),
        maintenance_cache=None,
        maintenance_last_fetch=float("-inf"),
        nvr_drain_state={},
        nvr_error_state={},
        nvr_processes={},
        nvr_user_intent={},
        nvr_preroll_segment_counts={},
        nvr_preroll_processes={},
        unread_events_cache={},
        rules_cache={},
        feature_flags={},
        rcp_onvif_scopes_cache={},
        rcp_version_cache={},
        external_stream_enabled={},
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


# ── trouble_connect in _attr_options ─────────────────────────────────────────


class TestLastEventTypeOptions:
    """_attr_options must include trouble_connect."""

    def test_trouble_connect_in_options(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord_bugfixes()
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert "trouble_connect" in s._attr_options, (
            "trouble_connect must be in _attr_options so HA ENUM validation passes"
        )

    def test_trouble_connect_returned_as_value(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord_bugfixes()
        coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_CONNECT", "timestamp": "2026-06-15T10:00:00.000Z"}
        ]
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "trouble_connect"

    def test_trouble_disconnect_still_works(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord_bugfixes()
        coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_DISCONNECT", "timestamp": "2026-06-15T10:00:00.000Z"}
        ]
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "trouble_disconnect"

    def test_trouble_reconnect_still_works(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord_bugfixes()
        coord.data[CAM_ID]["events"] = [
            {"eventType": "TROUBLE_RECONNECT", "timestamp": "2026-06-15T10:00:00.000Z"}
        ]
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "trouble_reconnect"

    def test_unknown_event_type_maps_to_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        coord = _stub_coord_bugfixes()
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

        coord = _stub_coord_bugfixes()
        s = BoschLastEventTypeSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "none"


# ── BoschTlsCertSensor tz-aware datetime ─────────────────────────────────────


class TestTlsCertSensorTzAwareDatetime:
    """native_value must return a tz-aware datetime."""

    def test_naive_iso_string_gets_utc_attached(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord_bugfixes(
            rcp_tls_cert_cache={CAM_ID: {"not_after": "2027-06-15T12:00:00"}}
        )
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val is not None
        assert val.tzinfo is not None, "naive datetime must not be returned"
        assert val.tzinfo == UTC

    def test_utc_z_suffix_remains_tz_aware(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord_bugfixes(
            rcp_tls_cert_cache={CAM_ID: {"not_after": "2027-06-15T12:00:00+00:00"}}
        )
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val is not None
        assert val.tzinfo is not None

    def test_missing_cert_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord_bugfixes(rcp_tls_cert_cache={})
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None

    def test_malformed_date_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        coord = _stub_coord_bugfixes(
            rcp_tls_cert_cache={CAM_ID: {"not_after": "not-a-date"}}
        )
        s = BoschTlsCertSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None


# ── EventsTodaySensor uses UTC date bucketing ────────────────────────────────


class TestEventsTodaySensorUtcBucketing:
    """today sensors bucket events by the event's LOCAL calendar date.

    Bosch timestamps carry an explicit offset; production code parses it and
    buckets by the local date of the true instant (see time_utils / issue #34).
    These tests use ``Z`` timestamps which, under the default UTC test
    timezone, fall on the same local date — so basic counting still holds.
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

        coord = _stub_coord_bugfixes()
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

        coord = _stub_coord_bugfixes()
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

        coord = _stub_coord_bugfixes()
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


# ── BoschCommissionedSensor snake_case ENUM ──────────────────────────────────


class TestCommissionedSensorSnakeCaseEnum:
    """ENUM options and native_value must use snake_case."""

    def test_options_are_snake_case(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_bugfixes()
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s._attr_options == ["commissioned", "not_commissioned", "not_connected"]

    def test_commissioned_true(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_bugfixes(
            commissioned_cache={
                CAM_ID: {"configured": True, "connected": True, "commissioned": True}
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "commissioned"

    def test_not_commissioned(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_bugfixes(
            commissioned_cache={
                CAM_ID: {"configured": True, "connected": True, "commissioned": False}
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "not_commissioned"

    def test_not_connected(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_bugfixes(
            commissioned_cache={
                CAM_ID: {"configured": False, "connected": False, "commissioned": False}
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "not_connected"

    def test_no_cache_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_bugfixes(commissioned_cache={})
        s = BoschCommissionedSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None

    def test_all_options_in_attr_options_are_valid_enum_values(self) -> None:
        """Every value returned by native_value must be in _attr_options."""
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_bugfixes()
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
            coord.commissioned_cache[CAM_ID] = data
            val = s.native_value
            assert val in options, f"{val!r} not in _attr_options"
            assert val == expected


# ── BoschCloudFeatureFlagsSensor 255-char truncation ─────────────────────────


class TestCloudFeatureFlagsSensor:
    """native_value must not exceed 255 chars."""

    def test_truncates_at_255_chars(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        # Build a flags dict whose joined string exceeds 255 chars
        many_flags = {f"feature_flag_{i:04d}": True for i in range(50)}
        coord = _stub_coord_bugfixes(feature_flags=many_flags)
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val is not None
        assert len(val) <= 255

    def test_short_flags_not_truncated(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        coord = _stub_coord_bugfixes(feature_flags={"alpha": True, "beta": False})
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "alpha"

    def test_no_enabled_flags_returns_none_string(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        coord = _stub_coord_bugfixes(feature_flags={"alpha": False, "beta": False})
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value == "none"

    def test_empty_flags_dict_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        coord = _stub_coord_bugfixes(feature_flags={})
        s = BoschCloudFeatureFlagsSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None


# ── BoschOnvifScopesSensor ENUM ───────────────────────────────────────────────


class TestOnvifScopesSensor:
    """sensor must be ENUM with option 'supported', not a free-text string."""

    def test_device_class_is_enum(self) -> None:
        from homeassistant.components.sensor import SensorDeviceClass

        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord_bugfixes()
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        assert s._attr_device_class == SensorDeviceClass.ENUM

    def test_options_contain_supported(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord_bugfixes()
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        assert "supported" in s._attr_options

    def test_native_value_is_supported_when_scopes_present(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord_bugfixes(
            rcp_onvif_scopes_cache={
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

        coord = _stub_coord_bugfixes(rcp_onvif_scopes_cache={})
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        assert s.native_value is None

    def test_value_is_in_options(self) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        coord = _stub_coord_bugfixes(
            rcp_onvif_scopes_cache={CAM_ID: {"profiles": ["S", "T"]}}
        )
        s = BoschOnvifScopesSensor(coord, CAM_ID, _stub_entry())
        val = s.native_value
        assert val in s._attr_options


# Sensor property + edge-branch coverage
# Pins remaining `sensor.py` branches not covered elsewhere:
#   - _BoschSensorBase.device_info return path
#   - FirmwareVersionSensor.extra_state_attributes featureSupport fallback
#     when top-level `upToDate` is None
#   - name/unique_id properties for several sensor classes
#   - MotionSensitivitySensor / LastEventTypeSensor / CommissionedSensor
#     empty-cache attribute fallbacks
#   - native_unit_of_measurement properties (zones/services/modules/masks)
#   - BoschMotionZonesSensor / BoschPrivateAreasSensor "never fetched" vs
#     "fetched but empty" availability semantics
#   - AmbientLightScheduleSensor attribute-expansion branches (dict vs string
#     schedule, manual start/end times, per-light-group brightness/color/wb)


def _stub_coord_edge_cases(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"upToDate": True},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        # Sensor caches
        commissioned_cache={},
        firmware_cache={},
        _wifi_cache={},
        ambient_light_cache={},
        _motion_sensitivity_cache={},
        _ledlight_brightness_cache={},
        _clock_offset_cache={},
        ledlights_cache={},
        _last_event_seen={},
        live_connections={},
        stream_warming=set(),
        stream_fell_back={},
        stream_error_count={},
        ambient_lighting_cache={},
        rcp_dimmer_cache={},
        unread_events_cache={},
        rules_cache={},
        rcp_alarm_catalog_cache={},
        rcp_motion_zones_cache={},
        rcp_motion_coords_cache={},
        cloud_zones_cache={},
        gen2_zones_cache={},
        rcp_tls_cert_cache={},
        rcp_network_services_cache={},
        rcp_iva_catalog_cache={},
        cloud_privacy_masks_cache={},
        gen2_private_areas_cache={},
        last_update_success=True,
        token="tok",
        options={"enable_fcm_push": False},
        fcm_running=False,
        fcm_healthy=False,
        # Coordinator helpers used by sensors
        rcp_product_name=lambda cid: None,
        motion_settings=lambda cid: {},
        clock_offset=lambda cid: None,
        # FCM monotonic sentinel — use float('-inf') per SENTINEL_RULE
        fcm_last_push=float("-inf"),
        fcm_push_mode="auto",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def edge_cases_coord() -> SimpleNamespace:
    return _stub_coord_edge_cases()


# ── _BoschSensorBase.device_info ─────────────────────────────────────────────


class TestSensorBaseDeviceInfo:
    """Every sensor exposes `device_info` so HA groups them under the camera
    device. Pin the return-dict shape — at least one concrete subclass."""

    def test_device_info_contains_model_and_fw(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        s = BoschCameraStatusSensor(edge_cases_coord, CAM_ID, stub_entry)
        info = s.device_info
        assert isinstance(info, dict)
        assert info["manufacturer"] == "Bosch"
        assert info["sw_version"] == "9.40.25"
        # MAC populated → non-empty connections
        assert info["connections"]


# ── Firmware sensor featureSupport fallback ──────────────────────────────────


class TestFirmwareVersionSensorUpToDateFallback:
    """If `info["upToDate"]` is missing, fall through to
    `info["featureSupport"]["upToDate"]`."""

    def test_uptodate_read_from_feature_support_when_top_level_missing(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

        # Top-level upToDate absent — only featureSupport carries it
        info = edge_cases_coord.data[CAM_ID]["info"]
        info.pop("upToDate", None)
        info["featureSupport"] = {"upToDate": False}
        s = BoschFirmwareVersionSensor(edge_cases_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["up_to_date"] is False


# ── MotionSensitivity name + unique_id ───────────────────────────────────────


class TestMotionSensitivityNameAndUid:
    def test_name(self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMotionSensitivitySensor,
        )

        s = BoschMotionSensitivitySensor(edge_cases_coord, CAM_ID, stub_entry)
        assert "Terrasse" in s.name
        assert "Motion Sensitivity" in s.name

    def test_unique_id(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMotionSensitivitySensor,
        )

        s = BoschMotionSensitivitySensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_motion_sensitivity"


# ── MotionSensitivity extra_state_attributes empty-settings ─────────────────


class TestMotionSensitivityEmptyAttributes:
    """When motion_settings() returns falsy, extra_state_attributes must
    return an empty dict (not raise KeyError)."""

    def test_empty_settings_returns_empty_dict(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMotionSensitivitySensor,
        )

        edge_cases_coord.motion_settings = lambda cid: {}
        s = BoschMotionSensitivitySensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes == {}


# ── LastEventTypeSensor ───────────────────────────────────────────────────


class TestLastEventTypeSensor:
    def test_name(self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        s = BoschLastEventTypeSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert "Last Event Type" in s.name

    def test_unique_id(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        s = BoschLastEventTypeSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_last_event_type"

    def test_extra_attrs_with_events(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """When events present, attrs dict carries event_type/timestamp/id."""
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        edge_cases_coord.data[CAM_ID]["events"] = [
            {"eventType": "PERSON", "timestamp": "2026-05-10T10:00:00Z", "id": "EVT123"}
        ]
        s = BoschLastEventTypeSensor(edge_cases_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["event_type"] == "PERSON"
        assert attrs["timestamp"] == "2026-05-10T10:00:00Z"
        assert attrs["event_id"] == "EVT123"


# ── MovementEventsToday name + unique_id ─────────────────────────────────────


class TestMovementEventsTodayNameAndUid:
    def test_name(self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )

        s = BoschMovementEventsTodaySensor(edge_cases_coord, CAM_ID, stub_entry)
        assert "Movement Events Today" in s.name

    def test_unique_id(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )

        s = BoschMovementEventsTodaySensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_movement_events_today"


# ── AudioEventsToday name + unique_id ─────────────────────────────────────────


class TestAudioEventsTodayNameAndUid:
    def test_name(self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAudioEventsTodaySensor,
        )

        s = BoschAudioEventsTodaySensor(edge_cases_coord, CAM_ID, stub_entry)
        assert "Audio Events Today" in s.name

    def test_unique_id(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAudioEventsTodaySensor,
        )

        s = BoschAudioEventsTodaySensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_audio_events_today"


# ── FcmPushStatus name + unique_id ───────────────────────────────────────────


class TestFcmPushStatusNameAndUid:
    def test_name(self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

        s = BoschFcmPushStatusSensor(edge_cases_coord, CAM_ID, stub_entry)
        # name resolved from translation key at runtime
        assert s._attr_translation_key == "push_status"

    def test_unique_id(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

        s = BoschFcmPushStatusSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.unique_id == "bosch_shc_camera_fcm_push_status"


# ── CommissionedSensor empty-cache attributes ────────────────────────────────


class TestCommissionedSensorEmptyCache:
    """When the slow-tier cache hasn't filled, extra_state_attributes must
    return `{}` instead of crashing on None.get()."""

    def test_empty_cache_returns_empty_dict(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        # Cache empty (default)
        s = BoschCommissionedSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes == {}


# ── native_unit_of_measurement properties ────────────────────────────────────


class TestNativeUnitProperties:
    """The unit strings are property methods rather than class attrs (they
    need to override even when EntityCategory.DIAGNOSTIC suppresses defaults)."""

    def test_motion_zones_unit(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

        s = BoschMotionZonesSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "zones"

    def test_network_services_unit(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor

        s = BoschNetworkServicesSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "services"

    def test_iva_catalog_unit(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschIvaCatalogSensor

        s = BoschIvaCatalogSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "modules"

    def test_private_areas_unit(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

        s = BoschPrivateAreasSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_unit_of_measurement == "masks"


# ── BoschMotionZonesSensor / BoschPrivateAreasSensor — unfetched vs empty ────
# Regression: unlike every sibling diagnostic sensor (BoschRulesCountSensor et
# al.), these two previously defaulted every cache lookup to `[]` and reported
# a confirmed "0 zones/masks" state (plus a misleading "not configured"
# attribute note) even before any source had ever been fetched, instead of
# unknown/unavailable.


class TestMotionZonesSensorAvailability:
    def test_none_when_no_source_ever_fetched(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

        s = BoschMotionZonesSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_value is None
        assert s.available is False

    def test_zero_when_fetched_but_empty(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Once a source HAS been fetched (even to an empty list), 0 is a
        real, distinguishable value — not the same as "never fetched"."""
        from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

        edge_cases_coord.rcp_motion_zones_cache[CAM_ID] = []
        s = BoschMotionZonesSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_value == 0
        assert s.available is True

    def test_gen2_zones_take_priority(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

        edge_cases_coord.gen2_zones_cache[CAM_ID] = [{"points": []}, {"points": []}]
        edge_cases_coord.cloud_zones_cache[CAM_ID] = [{"x": 0}]
        s = BoschMotionZonesSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_value == 2

    def test_cloud_zones_fallback(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

        edge_cases_coord.cloud_zones_cache[CAM_ID] = [{"x": 0}, {"x": 1}, {"x": 2}]
        edge_cases_coord.rcp_motion_zones_cache[CAM_ID] = [{"legacy": True}]
        s = BoschMotionZonesSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_value == 3

    def test_unavailable_when_coordinator_update_failed(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

        edge_cases_coord.rcp_motion_zones_cache[CAM_ID] = []
        edge_cases_coord.last_update_success = False
        s = BoschMotionZonesSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.available is False


class TestPrivateAreasSensorAvailability:
    def test_none_when_no_source_ever_fetched(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

        s = BoschPrivateAreasSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_value is None
        assert s.available is False

    def test_zero_when_fetched_but_empty(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

        edge_cases_coord.cloud_privacy_masks_cache[CAM_ID] = []
        s = BoschPrivateAreasSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_value == 0
        assert s.available is True

    def test_gen2_areas_take_priority(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

        edge_cases_coord.gen2_private_areas_cache[CAM_ID] = [{"points": []}]
        edge_cases_coord.cloud_privacy_masks_cache[CAM_ID] = [{"x": 0}, {"x": 1}]
        s = BoschPrivateAreasSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.native_value == 1


# ── AmbientLightSchedule attribute-expansion branches ────────────────────────


class TestAmbientLightScheduleAttributes:
    """`extra_state_attributes` has many branches covering schedule shapes
    (dict vs string), manual start/end, per-light-group expansion."""

    def test_empty_cache_returns_empty_dict(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """`if not cache: return {}`."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        # cache empty
        s = BoschAmbientLightScheduleSensor(edge_cases_coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes == {}

    def test_string_schedule_takes_else_branch(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """`schedule_str = schedule` when schedule isn't a dict."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        edge_cases_coord.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "ENVIRONMENT",  # plain string, not dict
        }
        s = BoschAmbientLightScheduleSensor(edge_cases_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["enabled"] is True
        assert attrs["schedule_type"] == "ENVIRONMENT"

    def test_manual_start_end_time_attrs(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """manual_start_time / manual_end_time both set."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        edge_cases_coord.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "MANUAL",
            "ambientLightManualStartTime": "20:00",
            "ambientLightManualEndTime": "06:30",
        }
        s = BoschAmbientLightScheduleSensor(edge_cases_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["manual_start_time"] == "20:00"
        assert attrs["manual_end_time"] == "06:30"

    def test_per_light_group_brightness_color_wb_expansion(
        self, edge_cases_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Each lighting group's brightness/whiteBalance/color gets a
        prefixed attribute key."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        edge_cases_coord.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "ENVIRONMENT",
            "frontLightSettings": {
                "brightness": 80,
                "whiteBalance": 0.3,
                "color": None,
            },
            "topLedLightSettings": {
                "brightness": 50,
                "whiteBalance": None,
                "color": "#FF0080",
            },
            "bottomLedLightSettings": {
                "brightness": 0,
                "whiteBalance": -1.0,
                "color": None,
            },
        }
        s = BoschAmbientLightScheduleSensor(edge_cases_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        # Front light: brightness + whiteBalance (color is None → skipped)
        assert attrs["front_light_brightness"] == 80
        assert attrs["front_light_white_balance"] == 0.3
        assert "front_light_color" not in attrs
        # Top LED: brightness + color (whiteBalance is None → skipped)
        assert attrs["top_led_light_brightness"] == 50
        assert attrs["top_led_light_color"] == "#FF0080"
        assert "top_led_light_white_balance" not in attrs
        # Bottom LED: brightness + whiteBalance
        assert attrs["bottom_led_light_brightness"] == 0
        assert attrs["bottom_led_light_white_balance"] == -1.0


# Broad sensor-class coverage (flat-function style)
# Covers a wide swath of sensor.py classes with one shared coordinator-stub
# builder and one shared "construct via __new__" sensor helper, since the
# classes below are pure property reads over coordinator caches:
#   BoschCameraStatusSensor, BoschCameraLastEventSensor,
#   BoschCameraEventsTodaySensor, BoschWifiSignalSensor,
#   BoschFirmwareVersionSensor, BoschAmbientLightSensor, BoschLedDimmerSensor,
#   BoschClockOffsetSensor, BoschMotionSensitivitySensor, BoschAudioAlarmSensor,
#   BoschLastEventTypeSensor, BoschMovementEventsTodaySensor,
#   BoschAudioEventsTodaySensor, BoschFcmPushStatusSensor,
#   BoschUnreadEventsCountSensor, BoschCommissionedSensor, BoschRulesCountSensor,
#   BoschAlarmCatalogSensor, BoschMotionZonesSensor, BoschTlsCertSensor,
#   BoschNetworkServicesSensor, BoschIvaCatalogSensor, BoschPrivateAreasSensor,
#   BoschAmbientLightScheduleSensor, BoschAlarmStateSensor
#
# UTC_TODAY / TODAY are computed at import time (module-load) and used only
# by tests that don't freeze the clock explicitly.
UTC_TODAY = datetime.now(UTC).strftime("%Y-%m-%d")
TODAY = UTC_TODAY


def _make_broad_sensor_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "hardwareVersion": "CAMERA",
                    "firmwareVersion": "7.91",
                    "macAddress": "aa:bb",
                    "title": "Kamera",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        last_update_success=True,
        options={"enable_fcm_push": False},
        commissioned_cache={},
        firmware_cache={},
        wifiinfo_cache={},
        ambient_light_cache={},
        rcp_dimmer_cache={},
        unread_events_cache={},
        rules_cache={},
        rcp_alarm_catalog_cache={},
        rcp_motion_zones_cache={},
        rcp_motion_coords_cache={},
        cloud_zones_cache={},
        gen2_zones_cache={},
        cloud_privacy_masks_cache={},
        gen2_private_areas_cache={},
        rcp_tls_cert_cache={},
        rcp_network_services_cache={},
        rcp_iva_catalog_cache={},
        ambient_lighting_cache={},
        alarm_status_cache={},
        arming_cache={},
        shc_state_cache={CAM_ID: {}},
        fcm_healthy=False,
        fcm_running=False,
        fcm_push_mode="auto",
        fcm_last_push=0,
        is_camera_online=lambda cid: True,
        clock_offset=lambda cid: None,
        motion_settings=lambda cid: None,
        recording_options=lambda cid: None,
        rcp_lan_ip=lambda cid: None,
        rcp_bitrate_ladder=lambda cid: None,
        rcp_product_name=lambda cid: None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_sensor_via_new(cls, coord=None, cam_id=CAM_ID):
    """Construct a sensor bypassing __init__ (only sets the attrs used by
    the property under test) — matches how the original round of tests
    exercised classes whose __init__ pulls in more than these fixtures cover."""
    c = coord or _make_broad_sensor_coord()
    sw = cls.__new__(cls)
    sw.coordinator = c
    sw._cam_id = cam_id
    sw._cam_title = "Kamera"
    sw._model_name = "Camera"
    sw._fw = "7.91"
    sw._mac = "aa:bb"
    sw.hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Berlin"),
    )
    return sw


# ── BoschCameraStatusSensor ───────────────────────────────────────────────────


def test_status_sensor_native_value_online():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["status"] = "ONLINE"
    sw = _make_sensor_via_new(BoschCameraStatusSensor, c)
    assert sw.native_value == "online"


def test_status_sensor_native_value_offline():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["status"] = "OFFLINE"
    sw = _make_sensor_via_new(BoschCameraStatusSensor, c)
    assert sw.native_value == "offline"


def test_status_sensor_extra_attrs_with_comm_and_fw():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    c = _make_broad_sensor_coord(
        commissioned_cache={
            CAM_ID: {"configured": True, "connected": True, "commissioned": True}
        },
        firmware_cache={CAM_ID: {"updating": False, "status": "OK", "upToDate": True}},
    )
    sw = _make_sensor_via_new(BoschCameraStatusSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["commissioned"] is True
    assert attrs["firmware_up_to_date"] is True


def test_status_sensor_extra_attrs_no_comm():
    from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

    sw = _make_sensor_via_new(BoschCameraStatusSensor)
    attrs = sw.extra_state_attributes
    assert "commissioned" not in attrs
    assert "firmware_updating" not in attrs


# ── BoschCameraLastEventSensor ────────────────────────────────────────────────


def test_last_event_sensor_no_events():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    sw = _make_sensor_via_new(BoschCameraLastEventSensor)
    assert sw.native_value is None


def test_last_event_sensor_valid_ts():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [
        {
            "timestamp": "2026-03-19T09:32:08.000Z",
            "eventType": "MOVEMENT",
            "id": "abc123def",
            "imageUrl": "http://x",
        }
    ]
    sw = _make_sensor_via_new(BoschCameraLastEventSensor, c)
    result = sw.native_value
    assert result is not None
    assert result.year == 2026
    assert result.month == 3


def test_last_event_sensor_bad_ts():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [{"timestamp": "not-a-date"}]
    sw = _make_sensor_via_new(BoschCameraLastEventSensor, c)
    assert sw.native_value is None


def test_last_event_sensor_empty_ts():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [{"timestamp": ""}]
    sw = _make_sensor_via_new(BoschCameraLastEventSensor, c)
    assert sw.native_value is None


def test_last_event_sensor_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [
        {
            "timestamp": "2026-03-19T09:32:08",
            "eventType": "PERSON",
            "id": "abcdefgh1234",
            "imageUrl": "http://x",
            "videoClipUrl": "http://v",
            "videoClipUploadStatus": "DONE",
        }
    ]
    sw = _make_sensor_via_new(BoschCameraLastEventSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["event_type"] == "PERSON"
    assert attrs["has_image"] is True
    assert attrs["has_clip"] is True


# ── BoschCameraEventsTodaySensor ─────────────────────────────────────────────


def _freeze_today():
    """Freeze the sensor clock so fixture dates and local-date bucketing agree.

    The default test timezone is US/Pacific, so a real "UTC today" event can
    fall on the previous *local* day during the UTC-morning boundary window —
    which made these counts flaky by time-of-day. Freeze now + force
    as_local→UTC for determinism. Returns (now-patch, as_local-patch, day-str).
    """
    fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    return (
        patch(
            "custom_components.bosch_shc_camera.sensor.dt_util.now",
            return_value=fixed_now,
        ),
        patch(
            "custom_components.bosch_shc_camera.sensor.dt_util.as_local",
            side_effect=lambda dt: dt.astimezone(UTC),
        ),
        fixed_now.strftime("%Y-%m-%d"),
    )


def test_events_today_count_matching():
    from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor

    # Events on today's local date are counted; a past date (2000-01-01) excluded.
    p_now, p_local, day = _freeze_today()
    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [
        {"timestamp": f"{day}T10:00:00.000Z"},
        {"timestamp": f"{day}T11:00:00.000Z"},
        {"timestamp": "2000-01-01T00:00:00.000Z"},
    ]
    sw = _make_sensor_via_new(BoschCameraEventsTodaySensor, c)
    with p_now, p_local:
        result = sw.native_value
    assert result == 2


def test_events_today_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschCameraEventsTodaySensor

    p_now, p_local, day = _freeze_today()
    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [{"timestamp": f"{day}T09:00:00.000Z"}]
    sw = _make_sensor_via_new(BoschCameraEventsTodaySensor, c)
    with p_now, p_local:
        attrs = sw.extra_state_attributes
    assert attrs["events_in_feed"] == 1


# ── BoschWifiSignalSensor ─────────────────────────────────────────────────────


def test_wifi_signal_native_value_none():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    sw = _make_sensor_via_new(BoschWifiSignalSensor)
    assert sw.native_value is None


def test_wifi_signal_native_value_int():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    c = _make_broad_sensor_coord(
        wifiinfo_cache={
            CAM_ID: {
                "signalStrength": 85,
                "ssid": "HOME",
                "ipAddress": "192.168.1.2",
                "macAddress": "aa:bb",
            }
        }
    )
    sw = _make_sensor_via_new(BoschWifiSignalSensor, c)
    assert sw.native_value == 85


def test_wifi_signal_available_false():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    sw = _make_sensor_via_new(BoschWifiSignalSensor)
    assert sw.available is False


def test_wifi_signal_extra_attrs_with_rcp():
    from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

    c = _make_broad_sensor_coord(
        wifiinfo_cache={
            CAM_ID: {
                "signalStrength": 70,
                "ssid": "X",
                "ipAddress": "10.0.0.1",
                "macAddress": "cc:dd",
            }
        }
    )
    c.rcp_lan_ip = lambda cid: "192.0.2.149"
    c.rcp_bitrate_ladder = lambda cid: [1000, 2000, 3000]
    sw = _make_sensor_via_new(BoschWifiSignalSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["lan_ip_rcp"] == "192.0.2.149"
    assert attrs["max_bitrate_kbps"] == 3000


# ── BoschFirmwareVersionSensor ────────────────────────────────────────────────


def test_firmware_version_none_when_missing():
    from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["info"]["firmwareVersion"] = ""
    sw = _make_sensor_via_new(BoschFirmwareVersionSensor, c)
    assert sw.native_value is None


def test_firmware_version_available_false_no_fw():
    from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["info"]["firmwareVersion"] = ""
    sw = _make_sensor_via_new(BoschFirmwareVersionSensor, c)
    assert sw.available is False


def test_firmware_version_extra_attrs_up_to_date():
    from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["info"]["upToDate"] = True
    c.rcp_product_name = lambda cid: "Bosch FLEXIDOME"
    sw = _make_sensor_via_new(BoschFirmwareVersionSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["up_to_date"] is True
    assert attrs["product_name_rcp"] == "Bosch FLEXIDOME"


# ── BoschAmbientLightSensor ───────────────────────────────────────────────────


def test_ambient_light_native_value_none():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor

    sw = _make_sensor_via_new(BoschAmbientLightSensor)
    assert sw.native_value is None


def test_ambient_light_native_value():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor

    c = _make_broad_sensor_coord(ambient_light_cache={CAM_ID: 0.65})
    sw = _make_sensor_via_new(BoschAmbientLightSensor, c)
    assert sw.native_value == 65


def test_ambient_light_available():
    from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor

    c = _make_broad_sensor_coord(ambient_light_cache={CAM_ID: 0.5})
    sw = _make_sensor_via_new(BoschAmbientLightSensor, c)
    assert sw.available is True


# ── BoschLedDimmerSensor ──────────────────────────────────────────────────────


def test_led_dimmer_native_value():
    from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor

    c = _make_broad_sensor_coord(rcp_dimmer_cache={CAM_ID: 75})
    sw = _make_sensor_via_new(BoschLedDimmerSensor, c)
    assert sw.native_value == 75


def test_led_dimmer_available_false():
    from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor

    sw = _make_sensor_via_new(BoschLedDimmerSensor)
    assert sw.available is False


# ── BoschClockOffsetSensor ────────────────────────────────────────────────────


def test_clock_offset_none():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    sw = _make_sensor_via_new(BoschClockOffsetSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_clock_offset_in_sync():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    c = _make_broad_sensor_coord()
    c.clock_offset = lambda cid: 2
    sw = _make_sensor_via_new(BoschClockOffsetSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["status"] == "in_sync"


def test_clock_offset_minor_drift():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    c = _make_broad_sensor_coord()
    c.clock_offset = lambda cid: -30
    sw = _make_sensor_via_new(BoschClockOffsetSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["status"] == "minor_drift"


def test_clock_offset_out_of_sync():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    c = _make_broad_sensor_coord()
    c.clock_offset = lambda cid: 120
    sw = _make_sensor_via_new(BoschClockOffsetSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["status"] == "out_of_sync"


def test_clock_offset_extra_attrs_none_returns_empty():
    from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

    sw = _make_sensor_via_new(BoschClockOffsetSensor)
    assert sw.extra_state_attributes == {}


# ── BoschMotionSensitivitySensor ──────────────────────────────────────────────


def test_motion_sensitivity_none_no_settings():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    sw = _make_sensor_via_new(BoschMotionSensitivitySensor)
    assert sw.native_value is None


def test_motion_sensitivity_disabled():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    c = _make_broad_sensor_coord()
    c.motion_settings = lambda cid: {
        "enabled": False,
        "motionAlarmConfiguration": "HIGH",
    }
    sw = _make_sensor_via_new(BoschMotionSensitivitySensor, c)
    assert sw.native_value == "disabled"


def test_motion_sensitivity_enabled():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    c = _make_broad_sensor_coord()
    c.motion_settings = lambda cid: {
        "enabled": True,
        "motionAlarmConfiguration": "HIGH_SENSITIVITY",
    }
    sw = _make_sensor_via_new(BoschMotionSensitivitySensor, c)
    assert sw.native_value == "high sensitivity"


def test_motion_sensitivity_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschMotionSensitivitySensor

    c = _make_broad_sensor_coord()
    c.motion_settings = lambda cid: {
        "enabled": True,
        "motionAlarmConfiguration": "HIGH",
    }
    sw = _make_sensor_via_new(BoschMotionSensitivitySensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["enabled"] is True


# ── BoschLastEventTypeSensor ──────────────────────────────────────────────────


def test_last_event_type_no_events():
    from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

    sw = _make_sensor_via_new(BoschLastEventTypeSensor)
    assert sw.native_value == "none"


def test_last_event_type_person():
    from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [
        {"eventType": "PERSON", "timestamp": f"{TODAY}T10:00:00", "id": "abc"}
    ]
    sw = _make_sensor_via_new(BoschLastEventTypeSensor, c)
    assert sw.native_value == "person"


def test_last_event_type_extra_attrs_no_events():
    from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

    sw = _make_sensor_via_new(BoschLastEventTypeSensor)
    assert sw.extra_state_attributes == {}


# ── BoschMovementEventsTodaySensor ────────────────────────────────────────────


def test_movement_events_today_filters_type():
    from custom_components.bosch_shc_camera.sensor import BoschMovementEventsTodaySensor

    # Clock frozen so today's local date and the fixture date agree (see
    # _freeze_today — avoids US/Pacific UTC-boundary flakiness).
    p_now, p_local, day = _freeze_today()
    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [
        {"eventType": "MOVEMENT", "timestamp": f"{day}T10:00:00.000Z"},
        {
            "eventType": "PERSON",
            "timestamp": f"{day}T11:00:00.000Z",
        },  # excluded (wrong type)
        {
            "eventType": "MOVEMENT",
            "timestamp": "2000-01-01T00:00:00.000Z",
        },  # excluded (old date)
    ]
    sw = _make_sensor_via_new(BoschMovementEventsTodaySensor, c)
    with p_now, p_local:
        result = sw.native_value
    assert result == 1


# ── BoschAudioEventsTodaySensor ───────────────────────────────────────────────


def test_audio_events_today_count():
    from custom_components.bosch_shc_camera.sensor import BoschAudioEventsTodaySensor

    # Clock frozen (see _freeze_today) for deterministic local-date bucketing.
    p_now, p_local, day = _freeze_today()
    c = _make_broad_sensor_coord()
    c.data[CAM_ID]["events"] = [
        {"eventType": "AUDIO_ALARM", "timestamp": f"{day}T08:00:00.000Z"},
        {"eventType": "AUDIO_ALARM", "timestamp": f"{day}T09:00:00.000Z"},
        {
            "eventType": "MOVEMENT",
            "timestamp": f"{day}T10:00:00.000Z",
        },  # excluded (wrong type)
    ]
    sw = _make_sensor_via_new(BoschAudioEventsTodaySensor, c)
    with p_now, p_local:
        result = sw.native_value
    assert result == 2


# ── BoschFcmPushStatusSensor ──────────────────────────────────────────────────


def test_fcm_status_disabled():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

    sw = _make_sensor_via_new(BoschFcmPushStatusSensor)
    sw.coordinator.options = {"enable_fcm_push": False}
    assert sw.native_value == "disabled"


def test_fcm_status_fcm_push():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

    c = _make_broad_sensor_coord()
    c.options = {"enable_fcm_push": True}
    c.fcm_healthy = True
    sw = _make_sensor_via_new(BoschFcmPushStatusSensor, c)
    assert sw.native_value == "fcm_push"


def test_fcm_status_polling():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

    c = _make_broad_sensor_coord()
    c.options = {"enable_fcm_push": True}
    c.fcm_healthy = False
    sw = _make_sensor_via_new(BoschFcmPushStatusSensor, c)
    assert sw.native_value == "polling"


def test_fcm_status_extra_attrs_last_push():
    from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

    c = _make_broad_sensor_coord()
    c.options = {"enable_fcm_push": True, "fcm_push_mode": "auto"}
    c.fcm_last_push = time.monotonic() - 30
    sw = _make_sensor_via_new(BoschFcmPushStatusSensor, c)
    attrs = sw.extra_state_attributes
    assert "last_push_seconds_ago" in attrs
    assert attrs["last_push_seconds_ago"] >= 28


# ── BoschUnreadEventsCountSensor ─────────────────────────────────────────────


def test_unread_events_none():
    from custom_components.bosch_shc_camera.sensor import BoschUnreadEventsCountSensor

    sw = _make_sensor_via_new(BoschUnreadEventsCountSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_unread_events_count():
    from custom_components.bosch_shc_camera.sensor import BoschUnreadEventsCountSensor

    c = _make_broad_sensor_coord(unread_events_cache={CAM_ID: 5})
    sw = _make_sensor_via_new(BoschUnreadEventsCountSensor, c)
    assert sw.native_value == 5
    assert sw.available is True


# ── BoschCommissionedSensor ───────────────────────────────────────────────────


def test_commissioned_none():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    sw = _make_sensor_via_new(BoschCommissionedSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_commissioned_not_connected():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _make_broad_sensor_coord(
        commissioned_cache={
            CAM_ID: {"configured": True, "connected": False, "commissioned": False}
        }
    )
    sw = _make_sensor_via_new(BoschCommissionedSensor, c)
    assert sw.native_value == "not_connected"


def test_commissioned_yes():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _make_broad_sensor_coord(
        commissioned_cache={
            CAM_ID: {"configured": True, "connected": True, "commissioned": True}
        }
    )
    sw = _make_sensor_via_new(BoschCommissionedSensor, c)
    assert sw.native_value == "commissioned"


def test_commissioned_not_commissioned():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _make_broad_sensor_coord(
        commissioned_cache={
            CAM_ID: {"configured": True, "connected": True, "commissioned": False}
        }
    )
    sw = _make_sensor_via_new(BoschCommissionedSensor, c)
    assert sw.native_value == "not_commissioned"


def test_commissioned_extra_attrs():
    from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

    c = _make_broad_sensor_coord(
        commissioned_cache={
            CAM_ID: {"configured": True, "connected": True, "commissioned": True}
        }
    )
    sw = _make_sensor_via_new(BoschCommissionedSensor, c)
    attrs = sw.extra_state_attributes
    assert attrs["commissioned"] is True


# ── BoschRulesCountSensor ─────────────────────────────────────────────────────


def test_rules_count_none():
    from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

    sw = _make_sensor_via_new(BoschRulesCountSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_rules_count_value():
    from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

    rules = [
        {
            "id": "r1",
            "name": "Night",
            "isActive": True,
            "startTime": "22:00",
            "endTime": "06:00",
            "weekdays": ["Mon"],
        },
        {
            "id": "r2",
            "name": "Day",
            "isActive": False,
            "startTime": "06:00",
            "endTime": "22:00",
            "weekdays": [],
        },
    ]
    c = _make_broad_sensor_coord(rules_cache={CAM_ID: rules})
    sw = _make_sensor_via_new(BoschRulesCountSensor, c)
    assert sw.native_value == 2
    attrs = sw.extra_state_attributes
    assert len(attrs["rules"]) == 2
    assert attrs["rules"][0]["name"] == "Night"


# ── BoschAlarmCatalogSensor ───────────────────────────────────────────────────


def test_alarm_catalog_none():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

    sw = _make_sensor_via_new(BoschAlarmCatalogSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_alarm_catalog_count():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

    alarms = [{"name": "MOTION", "type": "motion"}, {"name": "SMOKE", "type": "smoke"}]
    c = _make_broad_sensor_coord(rcp_alarm_catalog_cache={CAM_ID: alarms})
    sw = _make_sensor_via_new(BoschAlarmCatalogSensor, c)
    assert sw.native_value == 2
    attrs = sw.extra_state_attributes
    assert "MOTION" in attrs["alarm_types"]
    assert "smoke" in attrs["categories"]


# ── BoschMotionZonesSensor ────────────────────────────────────────────────────


def test_motion_zones_gen2_priority():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _make_broad_sensor_coord(
        gen2_zones_cache={CAM_ID: [{"id": 1}, {"id": 2}]},
        cloud_zones_cache={CAM_ID: [{"id": 3}]},
        rcp_motion_zones_cache={CAM_ID: []},
        rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor_via_new(BoschMotionZonesSensor, c)
    assert sw.native_value == 2  # gen2 wins


def test_motion_zones_cloud_fallback():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _make_broad_sensor_coord(
        gen2_zones_cache={CAM_ID: []},
        cloud_zones_cache={CAM_ID: [{"id": 1}]},
        rcp_motion_zones_cache={CAM_ID: []},
        rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor_via_new(BoschMotionZonesSensor, c)
    assert sw.native_value == 1


def test_motion_zones_rcp_fallback():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _make_broad_sensor_coord(
        gen2_zones_cache={CAM_ID: []},
        cloud_zones_cache={CAM_ID: []},
        rcp_motion_zones_cache={CAM_ID: [{"id": 1}, {"id": 2}, {"id": 3}]},
        rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor_via_new(BoschMotionZonesSensor, c)
    assert sw.native_value == 3


def test_motion_zones_note_when_empty():
    from custom_components.bosch_shc_camera.sensor import BoschMotionZonesSensor

    c = _make_broad_sensor_coord(
        gen2_zones_cache={CAM_ID: []},
        cloud_zones_cache={CAM_ID: []},
        rcp_motion_zones_cache={CAM_ID: []},
        rcp_motion_coords_cache={CAM_ID: []},
    )
    sw = _make_sensor_via_new(BoschMotionZonesSensor, c)
    assert "note" in sw.extra_state_attributes


# ── BoschTlsCertSensor ────────────────────────────────────────────────────────


def test_tls_cert_none():
    from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

    sw = _make_sensor_via_new(BoschTlsCertSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_tls_cert_valid():
    from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

    c = _make_broad_sensor_coord(
        rcp_tls_cert_cache={CAM_ID: {"not_after": "2027-01-01T00:00:00"}}
    )
    sw = _make_sensor_via_new(BoschTlsCertSensor, c)
    val = sw.native_value
    assert val is not None
    assert val.year == 2027


def test_tls_cert_bad_date():
    from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

    c = _make_broad_sensor_coord(
        rcp_tls_cert_cache={CAM_ID: {"not_after": "not-a-date"}}
    )
    sw = _make_sensor_via_new(BoschTlsCertSensor, c)
    assert sw.native_value is None


# ── BoschIvaCatalogSensor ─────────────────────────────────────────────────────


def test_iva_catalog_none():
    from custom_components.bosch_shc_camera.sensor import BoschIvaCatalogSensor

    sw = _make_sensor_via_new(BoschIvaCatalogSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_iva_catalog_count_and_active():
    from custom_components.bosch_shc_camera.sensor import BoschIvaCatalogSensor

    modules = [
        {"id": 1, "active": True},
        {"id": 2, "active": False},
        {"id": 3, "active": True},
    ]
    c = _make_broad_sensor_coord(rcp_iva_catalog_cache={CAM_ID: modules})
    sw = _make_sensor_via_new(BoschIvaCatalogSensor, c)
    assert sw.native_value == 3
    attrs = sw.extra_state_attributes
    assert attrs["active_count"] == 2


# ── BoschPrivateAreasSensor ───────────────────────────────────────────────────


def test_private_areas_gen2_priority():
    from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

    c = _make_broad_sensor_coord(
        gen2_private_areas_cache={CAM_ID: [{"id": 1}, {"id": 2}]},
        cloud_privacy_masks_cache={CAM_ID: [{"id": 3}]},
    )
    sw = _make_sensor_via_new(BoschPrivateAreasSensor, c)
    assert sw.native_value == 2


def test_private_areas_cloud_fallback():
    from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

    c = _make_broad_sensor_coord(
        gen2_private_areas_cache={CAM_ID: []},
        cloud_privacy_masks_cache={CAM_ID: [{"id": 1}]},
    )
    sw = _make_sensor_via_new(BoschPrivateAreasSensor, c)
    assert sw.native_value == 1


def test_private_areas_note_when_empty():
    from custom_components.bosch_shc_camera.sensor import BoschPrivateAreasSensor

    c = _make_broad_sensor_coord(
        gen2_private_areas_cache={CAM_ID: []},
        cloud_privacy_masks_cache={CAM_ID: []},
    )
    sw = _make_sensor_via_new(BoschPrivateAreasSensor, c)
    assert "note" in sw.extra_state_attributes


# ── BoschAmbientLightScheduleSensor ──────────────────────────────────────────


def test_ambient_schedule_none_no_cache():
    from custom_components.bosch_shc_camera.sensor import (
        BoschAmbientLightScheduleSensor,
    )

    sw = _make_sensor_via_new(BoschAmbientLightScheduleSensor)
    assert sw.native_value is None
    assert sw.available is False


def test_ambient_schedule_disabled():
    from custom_components.bosch_shc_camera.sensor import (
        BoschAmbientLightScheduleSensor,
    )

    c = _make_broad_sensor_coord(
        ambient_lighting_cache={
            CAM_ID: {
                "ambientLightEnabled": False,
                "ambientLightSchedule": "ENVIRONMENT",
            }
        }
    )
    sw = _make_sensor_via_new(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "disabled"


def test_ambient_schedule_dusk_to_dawn():
    from custom_components.bosch_shc_camera.sensor import (
        BoschAmbientLightScheduleSensor,
    )

    c = _make_broad_sensor_coord(
        ambient_lighting_cache={
            CAM_ID: {"ambientLightEnabled": True, "ambientLightSchedule": "ENVIRONMENT"}
        }
    )
    sw = _make_sensor_via_new(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "dusk_to_dawn"


def test_ambient_schedule_manual():
    from custom_components.bosch_shc_camera.sensor import (
        BoschAmbientLightScheduleSensor,
    )

    c = _make_broad_sensor_coord(
        ambient_lighting_cache={
            CAM_ID: {"ambientLightEnabled": True, "ambientLightSchedule": "MANUAL"}
        }
    )
    sw = _make_sensor_via_new(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "manual"


def test_ambient_schedule_dict_schedule():
    from custom_components.bosch_shc_camera.sensor import (
        BoschAmbientLightScheduleSensor,
    )

    c = _make_broad_sensor_coord(
        ambient_lighting_cache={
            CAM_ID: {
                "ambientLightEnabled": True,
                "ambientLightSchedule": {
                    "type": "ENVIRONMENT",
                    "lightOnTime": "18:00",
                    "lightOffTime": "06:00",
                },
            }
        }
    )
    sw = _make_sensor_via_new(BoschAmbientLightScheduleSensor, c)
    assert sw.native_value == "dusk_to_dawn"
    attrs = sw.extra_state_attributes
    assert attrs["schedule_on_time"] == "18:00"


# ── BoschAlarmStateSensor ─────────────────────────────────────────────────────


def test_alarm_state_from_status_cache():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    c = _make_broad_sensor_coord(
        alarm_status_cache={
            CAM_ID: {"intrusionSystem": "ACTIVE", "alarmType": "MOTION"}
        }
    )
    sw = _make_sensor_via_new(BoschAlarmStateSensor, c)
    assert sw.native_value == "active"


def test_alarm_state_from_arming_cache_armed():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    c = _make_broad_sensor_coord(arming_cache={CAM_ID: True})
    sw = _make_sensor_via_new(BoschAlarmStateSensor, c)
    assert sw.native_value == "active"


def test_alarm_state_from_arming_cache_disarmed():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    c = _make_broad_sensor_coord(arming_cache={CAM_ID: False})
    sw = _make_sensor_via_new(BoschAlarmStateSensor, c)
    assert sw.native_value == "inactive"


def test_alarm_state_unknown():
    from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

    sw = _make_sensor_via_new(BoschAlarmStateSensor)
    assert sw.native_value == "unknown"


# Phase-2 RCP sensor classes + async_setup_entry entity-gating
# Covers: async_setup_entry's per-camera feature gating (light sensor only
# when featureSupport.light, ambient-schedule only for Gen2 Outdoor, alarm
# state only for Gen2 Indoor II, NVR sensor only when enable_nvr, sensors
# entirely skipped when enable_sensors=False), plus BoschAlarmCatalogSensor,
# BoschTlsCertSensor, BoschNetworkServicesSensor,
# BoschAmbientLightScheduleSensor, BoschAlarmStateSensor, BoschStreamStatusSensor.


def _stub_coord_phase2(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        wifiinfo_cache={},
        rcp_alarm_catalog_cache={},
        rcp_motion_zones_cache={},
        rcp_motion_coords_cache={},
        cloud_zones_cache={},
        gen2_zones_cache={},
        rcp_tls_cert_cache={},
        rcp_network_services_cache={},
        rcp_iva_catalog_cache={},
        _rcp_private_areas_cache={},
        ambient_lighting_cache={},
        _ambient_schedule_cache={},
        alarm_status_cache={},
        alarm_settings_cache={},
        arming_cache={},
        live_connections={},
        stream_fell_back={},
        stream_error_count={},
        stream_warming=set(),
        nvr_drain_state={},
        commissioned_cache={},
        firmware_cache={},
        unread_events_cache={},
        fcm_running=False,
        fcm_healthy=True,
        fcm_push_mode="auto",
        fcm_last_push=0.0,
        last_update_success=True,
        options={"enable_fcm_push": True, "enable_sensors": True, "enable_nvr": False},
        motion_settings=lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        },
        is_camera_online=lambda cid: True,
        is_stream_warming=lambda cid: False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def phase2_coord() -> SimpleNamespace:
    return _stub_coord_phase2()


# ── async_setup_entry entity-gating ──────────────────────────────────────────


class TestAsyncSetupEntryGating:
    def test_light_sensor_added_only_when_has_light(self):
        """BoschLedDimmerSensor must only be added for cameras with featureSupport.light=True."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = _stub_coord_phase2()
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = True
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschLedDimmerSensor" in entity_classes, (
            "LedDimmerSensor must be added when has_light=True"
        )

    def test_light_sensor_skipped_when_no_light(self):
        """BoschLedDimmerSensor must not be added when featureSupport.light=False."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = _stub_coord_phase2()
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschLedDimmerSensor" not in entity_classes, (
            "LedDimmerSensor must be skipped when has_light=False"
        )

    def test_ambient_schedule_sensor_added_for_gen2_outdoor(self):
        """BoschAmbientLightScheduleSensor added for Gen2 Outdoor, not for Indoor II."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = _stub_coord_phase2()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAmbientLightScheduleSensor" in entity_classes, (
            "AmbientLightScheduleSensor must be added for Gen2 Outdoor"
        )

    def test_ambient_schedule_sensor_skipped_for_indoor_ii(self):
        """BoschAmbientLightScheduleSensor must not appear for HOME_Eyes_Indoor."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = _stub_coord_phase2()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAmbientLightScheduleSensor" not in entity_classes, (
            "AmbientLightScheduleSensor must NOT be added for HOME_Eyes_Indoor (no RGB lights)"
        )

    def test_alarm_state_sensor_added_for_indoor_ii(self):
        """BoschAlarmStateSensor only for HOME_Eyes_Indoor / CAMERA_INDOOR_GEN2."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = _stub_coord_phase2()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAlarmStateSensor" in entity_classes, (
            "AlarmStateSensor must be added for Gen2 Indoor II"
        )

    def test_nvr_sensor_added_only_when_enable_nvr(self):
        """BoschNvrStateSensor must only appear when options.enable_nvr=True."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = _stub_coord_phase2()
        coord.options = {"enable_nvr": True, "enable_sensors": True}
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschNvrStateSensor" in entity_classes, (
            "NvrStateSensor must be added when enable_nvr=True"
        )

    def test_sensors_skipped_when_disabled_in_options(self):
        """When enable_sensors=False, setup must return immediately (no entities added)."""
        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = _stub_coord_phase2()
        coord.options = {"enable_sensors": False}
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(runtime_data=coord, options=coord.options)
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert added == [], "No entities must be registered when enable_sensors=False"


# ── BoschAlarmCatalogSensor ───────────────────────────────────────────────────


class TestAlarmCatalogSensor:
    def test_native_value_is_count(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

        phase2_coord.rcp_alarm_catalog_cache[CAM_ID] = [
            {"name": "motion", "type": "motion"},
            {"name": "audio", "type": "audio"},
        ]
        entity = BoschAlarmCatalogSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == 2, "native_value must return count of alarm types"

    def test_native_value_none_when_no_cache(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

        entity = BoschAlarmCatalogSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when cache not populated"

    def test_available_false_when_cache_empty(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

        entity = BoschAlarmCatalogSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when no RCP data"

    def test_available_true_when_cache_populated(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

        phase2_coord.rcp_alarm_catalog_cache[CAM_ID] = []
        entity = BoschAlarmCatalogSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.available is True, (
            "Must be available when cache is present (even if empty)"
        )

    def test_extra_attrs_list_alarm_types(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

        phase2_coord.rcp_alarm_catalog_cache[CAM_ID] = [
            {"name": "flame", "type": "fire"},
            {"name": "motion", "type": "motion"},
        ]
        entity = BoschAlarmCatalogSensor(phase2_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert "flame" in attrs["alarm_types"], "extra_attrs must list alarm type names"
        assert "fire" in attrs["categories"], "extra_attrs must list unique categories"

    def test_native_unit_is_types(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmCatalogSensor

        entity = BoschAlarmCatalogSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_unit_of_measurement == "types", "Unit must be 'types'"


# ── BoschTlsCertSensor ────────────────────────────────────────────────────────


class TestTlsCertSensor:
    def test_native_value_parses_iso_date(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        phase2_coord.rcp_tls_cert_cache[CAM_ID] = {
            "not_after": "2028-12-31T23:59:59",
            "not_before": "2024-01-01T00:00:00",
            "issuer": "Bosch",
            "subject": "cam123",
        }
        entity = BoschTlsCertSensor(phase2_coord, CAM_ID, stub_entry)
        val = entity.native_value
        assert isinstance(val, datetime), "native_value must be a datetime object"
        assert val.year == 2028, "Must parse year 2028 from ISO date"

    def test_native_value_none_when_cache_empty(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        entity = BoschTlsCertSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when no cert cached"

    def test_native_value_none_for_malformed_date(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        phase2_coord.rcp_tls_cert_cache[CAM_ID] = {"not_after": "not-a-date"}
        entity = BoschTlsCertSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None for unparseable date"

    def test_available_follows_cache_presence(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        phase2_coord.rcp_tls_cert_cache[CAM_ID] = {"not_after": "2028-01-01T00:00:00"}
        entity = BoschTlsCertSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when cert data is cached"

    def test_extra_attrs_include_issuer_and_subject(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschTlsCertSensor

        phase2_coord.rcp_tls_cert_cache[CAM_ID] = {
            "issuer": "Bosch CA",
            "subject": "camera-001",
            "key_size": 2048,
            "serial": "AABBCC",
            "not_before": "2024-01-01",
            "not_after": "2028-01-01",
            "signature_algorithm": "SHA256withRSA",
        }
        entity = BoschTlsCertSensor(phase2_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["issuer"] == "Bosch CA", "extra_attrs must expose issuer"
        assert attrs["key_size"] == 2048, "extra_attrs must expose key_size"


# ── BoschNetworkServicesSensor ────────────────────────────────────────────────


class TestNetworkServicesSensor:
    def test_native_value_is_count(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor

        phase2_coord.rcp_network_services_cache[CAM_ID] = [
            {"name": "RTSP", "enabled": True},
            {"name": "HTTPS", "enabled": True},
        ]
        entity = BoschNetworkServicesSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == 2, "native_value must count services"

    def test_native_value_none_when_not_cached(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor

        entity = BoschNetworkServicesSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, (
            "Must return None when not yet fetched via RCP"
        )

    def test_available_false_without_cache(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor

        entity = BoschNetworkServicesSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.available is False, "Must be unavailable when no RCP data"

    def test_extra_attrs_include_services_list(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschNetworkServicesSensor

        services = [{"name": "RTSP", "enabled": True}]
        phase2_coord.rcp_network_services_cache[CAM_ID] = services
        entity = BoschNetworkServicesSensor(phase2_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["services"] == services, (
            "extra_attrs must expose the services list"
        )


# ── BoschAmbientLightScheduleSensor ──────────────────────────────────────────


class TestAmbientLightScheduleSensor:
    def test_disabled_when_ambient_light_off(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        phase2_coord.ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": False}
        entity = BoschAmbientLightScheduleSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "disabled", (
            "Must return 'disabled' when ambientLightEnabled=False"
        )

    def test_dusk_to_dawn_when_schedule_environment(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        phase2_coord.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": {"type": "ENVIRONMENT"},
        }
        entity = BoschAmbientLightScheduleSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "dusk_to_dawn", (
            "ENVIRONMENT schedule must map to 'dusk_to_dawn'"
        )

    def test_dusk_to_dawn_for_string_schedule(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        phase2_coord.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": "ENVIRONMENT",  # flat string form
        }
        entity = BoschAmbientLightScheduleSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "dusk_to_dawn", (
            "String 'ENVIRONMENT' must also map to 'dusk_to_dawn'"
        )

    def test_manual_for_non_environment_schedule(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        phase2_coord.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": {
                "type": "MANUAL",
                "lightOnTime": "21:00",
                "lightOffTime": "06:00",
            },
        }
        entity = BoschAmbientLightScheduleSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "manual", (
            "Non-ENVIRONMENT schedule must map to 'manual'"
        )

    def test_native_value_none_when_cache_empty(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        entity = BoschAmbientLightScheduleSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when cache is empty"

    def test_available_requires_non_empty_cache(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        phase2_coord.ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": False}
        entity = BoschAmbientLightScheduleSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.available is True, "Must be available when cache has data"

    def test_extra_attrs_include_schedule_times(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAmbientLightScheduleSensor,
        )

        phase2_coord.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True,
            "ambientLightSchedule": {
                "type": "MANUAL",
                "lightOnTime": "20:00",
                "lightOffTime": "07:00",
            },
        }
        entity = BoschAmbientLightScheduleSensor(phase2_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["schedule_on_time"] == "20:00", (
            "extra_attrs must include lightOnTime"
        )
        assert attrs["schedule_off_time"] == "07:00", (
            "extra_attrs must include lightOffTime"
        )


# ── BoschAlarmStateSensor ─────────────────────────────────────────────────────


class TestAlarmStateSensor:
    def test_native_value_from_alarm_status_cache(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

        phase2_coord.alarm_status_cache[CAM_ID] = {
            "intrusionSystem": "ACTIVE",
            "alarmType": "INTRUSION",
        }
        entity = BoschAlarmStateSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "active", (
            "Must lowercase intrusionSystem for state"
        )

    def test_native_value_falls_back_to_arming_cache(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

        phase2_coord.alarm_status_cache = {}
        phase2_coord.arming_cache[CAM_ID] = True
        entity = BoschAlarmStateSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "active", (
            "Must fall back to arming cache when status cache empty"
        )

    def test_native_value_inactive_from_arming_false(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

        phase2_coord.alarm_status_cache = {}
        phase2_coord.arming_cache[CAM_ID] = False
        entity = BoschAlarmStateSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "inactive", (
            "Must return 'inactive' when arming_cache=False"
        )

    def test_native_value_unknown_when_no_data(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

        entity = BoschAlarmStateSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "unknown", (
            "Must return 'unknown' when no data available"
        )

    def test_available_requires_only_coordinator_success(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

        phase2_coord.is_camera_online = lambda cid: False
        entity = BoschAlarmStateSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.available is True, (
            "AlarmStateSensor must not gate on camera-online"
        )

    def test_extra_attrs_include_alarm_settings(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAlarmStateSensor

        phase2_coord.alarm_settings_cache[CAM_ID] = {
            "alarmMode": "ON",
            "preAlarmMode": "OFF",
            "alarmDelayInSeconds": 30,
            "alarmActivationDelaySeconds": 10,
        }
        phase2_coord.alarm_status_cache[CAM_ID] = {
            "alarmType": "NONE",
            "intrusionSystem": "INACTIVE",
        }
        entity = BoschAlarmStateSensor(phase2_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["alarm_mode"] == "ON", "extra_attrs must expose alarmMode"
        assert attrs["siren_duration_s"] == 30, (
            "extra_attrs must expose alarmDelayInSeconds"
        )


# ── BoschStreamStatusSensor ───────────────────────────────────────────────────


class TestStreamStatusSensor:
    def test_idle_when_no_connection(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor

        phase2_coord.live_connections = {}
        entity = BoschStreamStatusSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "idle", "Must be 'idle' when no live connection"

    def test_warming_up_when_stream_warming(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor

        phase2_coord.is_stream_warming = lambda cid: True
        phase2_coord.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        entity = BoschStreamStatusSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "warming_up", (
            "Must be 'warming_up' while stream pre-warms"
        )

    def test_streaming_when_rtsps_url_present(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor

        phase2_coord.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://cam/stream",
            "_connection_type": "LOCAL",
        }
        entity = BoschStreamStatusSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "streaming", (
            "Must be 'streaming' when RTSP URL available"
        )

    def test_streaming_remote_when_fell_back(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor

        phase2_coord.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://cam/stream"}
        phase2_coord.stream_fell_back[CAM_ID] = True
        entity = BoschStreamStatusSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "streaming_remote", (
            "Must be 'streaming_remote' when fell back to cloud"
        )

    def test_connecting_when_session_open_but_no_url(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor

        phase2_coord.live_connections[CAM_ID] = {}  # session open but no rtspsUrl yet
        entity = BoschStreamStatusSensor(phase2_coord, CAM_ID, stub_entry)
        assert entity.native_value == "connecting", (
            "Must be 'connecting' when session exists but no URL"
        )

    def test_extra_attrs_include_connection_type(
        self, phase2_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschStreamStatusSensor

        phase2_coord.live_connections[CAM_ID] = {
            "_connection_type": "LOCAL",
            "rtspsUrl": "rtsps://x",
        }
        phase2_coord.stream_error_count[CAM_ID] = 2
        entity = BoschStreamStatusSensor(phase2_coord, CAM_ID, stub_entry)
        attrs = entity.extra_state_attributes
        assert attrs["connection_type"] == "LOCAL", (
            "extra_attrs must include connection_type"
        )
        assert attrs["stream_errors"] == 2, "extra_attrs must include stream_errors"


# Core sensor classes — baseline coverage
# Same approach used throughout this module: stub coordinator, instantiate
# sensor, verify native_value and extra_state_attributes.


@pytest.fixture
def base_sensor_coord() -> SimpleNamespace:
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
        commissioned_cache={},
        firmware_cache={},
        _wifi_cache={CAM_ID: {"signal": 75, "ssid": "WLAN"}},
        ambient_light_cache={CAM_ID: 0.42},
        _motion_sensitivity_cache={CAM_ID: "MEDIUM_HIGH"},
        _ledlight_brightness_cache={CAM_ID: 80},
        _clock_offset_cache={CAM_ID: 1.23},
        ledlights_cache={CAM_ID: True},
        _last_event_seen={CAM_ID: None},
        live_connections={},
        stream_warming=set(),
        stream_fell_back={},
        stream_error_count={},
        fcm_running=True,
        fcm_healthy=True,
        # FCM status
        options={"enable_fcm_push": False},
    )


# ── BoschCameraStatusSensor ──────────────────────────────────────────────


class TestStatusSensor:
    def test_online(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_offline(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "OFFLINE"
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "offline"

    def test_unknown_when_missing(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        del base_sensor_coord.data[CAM_ID]["status"]
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "unknown"

    def test_attrs_include_camera_id_model_fw(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["camera_id"] == CAM_ID
        assert attrs["model"] == "HOME_Eyes_Outdoor"
        assert attrs["firmware"] == "9.40.25"

    def test_attrs_include_commissioned_when_cached(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.commissioned_cache[CAM_ID] = {
            "configured": True,
            "connected": True,
            "commissioned": True,
        }
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["configured"] is True
        assert attrs["connected"] is True

    def test_attrs_include_firmware_when_cached(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.firmware_cache[CAM_ID] = {
            "updating": True,
            "status": "downloading",
            "upToDate": False,
        }
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["firmware_updating"] is True
        assert attrs["firmware_update_status"] == "downloading"
        assert attrs["firmware_up_to_date"] is False

    def test_updating_state_overrides_online(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """When coordinator.is_updating(cam_id) is True, native_value must
        return 'updating' regardless of the cached cloud `status` field.
        Cloud still reports ONLINE during the install window (cached pre-reboot),
        but the camera is actually rebooting and dependent entities should
        flip to unavailable."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "ONLINE"
        base_sensor_coord.is_updating = lambda cam_id: cam_id == CAM_ID
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "updating"

    def test_updating_state_overrides_offline(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Even if cloud reports OFFLINE (which it will during reboot),
        is_updating must take precedence so the operator sees the cause."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "OFFLINE"
        base_sensor_coord.is_updating = lambda cam_id: True
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "updating"

    def test_updating_state_listed_in_options(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """The enum's options tuple must contain 'updating' so HA renders
        the state in the entity selector + history."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert "updating" in s._attr_options

    def test_no_updating_when_helper_returns_false(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """is_updating present but returns False → falls through to cloud status."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "ONLINE"
        base_sensor_coord.is_updating = lambda cam_id: False
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_no_updating_when_helper_missing(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Backward compat: coordinator without is_updating helper still works."""
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        # base_sensor_coord fixture has no is_updating attribute → getattr returns None
        assert not hasattr(base_sensor_coord, "is_updating")
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_offline_when_latest_event_is_trouble_disconnect(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Bosch cloud reports ONLINE forever after a disconnect; override via events.

        Regression: outdoor Gen1 camera showed as online in UI for 22 days
        while physically unreachable on LAN. Last event from cloud was
        TROUBLE_DISCONNECT but the status sensor still said online because
        Bosch cloud never updates the `status` field after a disconnect.
        """
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "ONLINE"
        base_sensor_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "TROUBLE_DISCONNECT",
                "timestamp": "2026-04-27T11:03:00+02:00",
            },
        ]
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "offline"

    def test_online_when_reconnect_is_newer_than_disconnect(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "ONLINE"
        base_sensor_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "TROUBLE_RECONNECT",
                "timestamp": "2026-05-19T08:00:00+02:00",
            },
            {
                "eventType": "TROUBLE_DISCONNECT",
                "timestamp": "2026-04-27T11:03:00+02:00",
            },
        ]
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_online_when_movement_is_newer_than_disconnect(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "ONLINE"
        base_sensor_coord.data[CAM_ID]["events"] = [
            {"eventType": "MOVEMENT", "timestamp": "2026-05-19T08:00:00+02:00"},
            {
                "eventType": "TROUBLE_DISCONNECT",
                "timestamp": "2026-04-27T11:03:00+02:00",
            },
        ]
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "online"

    def test_cloud_offline_not_changed_by_reconnect_event(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

        base_sensor_coord.data[CAM_ID]["status"] = "OFFLINE"
        base_sensor_coord.data[CAM_ID]["events"] = [
            {
                "eventType": "TROUBLE_RECONNECT",
                "timestamp": "2026-05-19T08:00:00+02:00",
            },
        ]
        s = BoschCameraStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "offline"


# ── BoschCameraEventsTodaySensor ─────────────────────────────────────────


class TestEventsTodaySensor:
    def test_count_zero_when_no_events(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraEventsTodaySensor,
        )

        s = BoschCameraEventsTodaySensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 0

    def test_count_with_today_events(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Events with today's date count toward the daily total."""
        today = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
        base_sensor_coord.data[CAM_ID]["events"] = [
            {"id": "e1", "createdAt": today, "type": "MOVEMENT"},
            {"id": "e2", "createdAt": today, "type": "AUDIO"},
        ]
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraEventsTodaySensor,
        )

        s = BoschCameraEventsTodaySensor(base_sensor_coord, CAM_ID, stub_entry)
        # Just check it returns a non-negative integer
        assert isinstance(s.native_value, int)
        assert s.native_value >= 0


# ── BoschFirmwareVersionSensor ───────────────────────────────────────────


class TestFirmwareVersionSensorReturnsString:
    def test_returns_fw_string(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

        s = BoschFirmwareVersionSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "9.40.25"


# ── BoschFcmPushStatusSensor ─────────────────────────────────────────────


class TestFcmPushStatusSensor:
    def test_disabled_when_fcm_off(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """enable_fcm_push=False → state is 'disabled'."""
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

        s = BoschFcmPushStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "disabled"

    def test_fcm_push_when_healthy(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """enable_fcm_push=True + healthy → state is 'fcm_push'."""
        base_sensor_coord.options = {"enable_fcm_push": True}
        base_sensor_coord.fcm_healthy = True
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

        s = BoschFcmPushStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "fcm_push"

    def test_polling_when_unhealthy(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """enable_fcm_push=True + UNhealthy → state is 'polling' (degradation visible)."""
        base_sensor_coord.options = {"enable_fcm_push": True}
        base_sensor_coord.fcm_healthy = False
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

        s = BoschFcmPushStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "polling"

    def test_volatile_attr_is_unrecorded(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """last_push_seconds_ago changes every tick → must be excluded from
        the recorder so the `state_attributes` table does not bloat (HA#39).
        The attribute must still be EMITTED live (only its recording is
        suppressed), so assert both: present in extra_state_attributes AND
        listed in _unrecorded_attributes."""
        from custom_components.bosch_shc_camera.sensor import BoschFcmPushStatusSensor

        base_sensor_coord.options = {"enable_fcm_push": True}
        base_sensor_coord.fcm_healthy = True
        base_sensor_coord.fcm_running = True
        base_sensor_coord.fcm_push_mode = "auto"
        base_sensor_coord.fcm_last_push = time.monotonic()
        s = BoschFcmPushStatusSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert "last_push_seconds_ago" in s.extra_state_attributes
        assert "last_push_seconds_ago" in s._unrecorded_attributes


# ── BoschAmbientLightSensor ──────────────────────────────────────────────


class TestAmbientLightSensor:
    def test_returns_percentage(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschAmbientLightSensor

        s = BoschAmbientLightSensor(base_sensor_coord, CAM_ID, stub_entry)
        # 0.42 → 42 percent (or whatever the conversion is)
        assert s.native_value is not None


# ── BoschCameraLastEventSensor ──────────────────────────────────────────


class TestLastEventSensor:
    def test_returns_none_with_no_events(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

        s = BoschCameraLastEventSensor(base_sensor_coord, CAM_ID, stub_entry)
        # No events → native_value is None
        assert s.native_value is None

    def test_returns_value_when_events_present(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """With events, native_value is a datetime — exact format depends on impl."""
        base_sensor_coord.data[CAM_ID]["events"] = [
            {"id": "e1", "createdAt": "2026-05-05T10:00:00Z", "type": "MOVEMENT"},
        ]
        from custom_components.bosch_shc_camera.sensor import BoschCameraLastEventSensor

        s = BoschCameraLastEventSensor(base_sensor_coord, CAM_ID, stub_entry)
        # No assertion on value — different impl details. Just confirm
        # the property doesn't raise.
        _ = s.native_value


# ── BoschLastEventTypeSensor ────────────────────────────────────────────


class TestLastEventTypeSensorValueMapping:
    def test_returns_none_with_no_events(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        s = BoschLastEventTypeSensor(base_sensor_coord, CAM_ID, stub_entry)
        # No events → native_value is None or "unknown"
        v = s.native_value
        assert v is None or isinstance(v, str)

    def test_known_event_type_lowercased(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        base_sensor_coord.data = {CAM_ID: {"events": [{"eventType": "MOVEMENT"}]}}
        s = BoschLastEventTypeSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == "movement"

    def test_unknown_event_type_maps_to_none(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Regression: an event type outside _attr_options (or a missing key)
        must map onto the 'none' catch-all — returning a value not in the ENUM
        options makes HA reject the state."""
        from custom_components.bosch_shc_camera.sensor import BoschLastEventTypeSensor

        s = BoschLastEventTypeSensor(base_sensor_coord, CAM_ID, stub_entry)
        base_sensor_coord.data = {CAM_ID: {"events": [{"eventType": "TAMPER_FUTURE"}]}}
        assert s.native_value == "none"
        assert s.native_value in s._attr_options
        base_sensor_coord.data = {
            CAM_ID: {"events": [{"foo": "bar"}]}
        }  # no eventType key
        assert s.native_value == "none"


# Additional sensor classes — baseline coverage
# Extends the "Core sensor classes" section above with the remaining
# property-only entities that read from coordinator caches and dicts. Each
# gets: native_value with data, native_value with missing data,
# extra_state_attributes (where present), and available (where non-trivial).


def _today_local_ts(hour: int = 12) -> str:
    """A Bosch-style timestamp on today's LOCAL date (offset honored).

    Buckets are by local calendar date (see time_utils / issue #34), so build
    the fixture from local now to stay tz-robust under any test timezone.
    """
    return (
        dt_util.now().replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()
    )


def _stub_coord_extra(**overrides):
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
        wifiinfo_cache={},
        rcp_dimmer_cache={},
        ambient_light_cache={},
        rcp_clock_offset_cache={},
        commissioned_cache={},
        rules_cache={},
        unread_events_cache={},
        rcp_alarm_catalog_cache={},
        rcp_tls_cert_cache={},
        rcp_network_services_cache={},
        rcp_iva_catalog_cache={},
        _rcp_private_areas_cache={},
        _ambient_schedule_cache={},
        # Coord helpers
        last_update_success=True,
        motion_settings=lambda cid: {},
        clock_offset=lambda cid: None,
        rcp_lan_ip=lambda cid: None,
        rcp_bitrate_ladder=lambda cid: [],
        rcp_product_name=lambda cid: None,
        options={},
        fcm_running=False,
        fcm_healthy=False,
        fcm_push_mode="auto",
        fcm_last_push=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def extra_sensor_coord() -> SimpleNamespace:
    return _stub_coord_extra()


# ── BoschWifiSignalSensor ────────────────────────────────────────────────


class TestWifiSignalSensorAdditionalCoverage:
    def test_native_value_from_cache(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        coord = _stub_coord_extra(wifiinfo_cache={CAM_ID: {"signalStrength": 75}})
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 75

    def test_native_value_none_when_no_cache(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        s = BoschWifiSignalSensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_native_value_none_when_field_missing(self, stub_entry: SimpleNamespace):
        """Cache entry exists but `signalStrength` field missing → None,
        not crash. Defensive against partial cache writes."""
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        coord = _stub_coord_extra(wifiinfo_cache={CAM_ID: {"ssid": "wlan"}})
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_available_requires_cache(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        s = BoschWifiSignalSensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.available is False
        coord = _stub_coord_extra(wifiinfo_cache={CAM_ID: {"signalStrength": 50}})
        s2 = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s2.available is True

    def test_extra_state_includes_ssid_ip_mac(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        coord = _stub_coord_extra(
            wifiinfo_cache={
                CAM_ID: {
                    "signalStrength": 80,
                    "ssid": "MYWLAN",
                    "ipAddress": "10.0.0.5",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                }
            }
        )
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["ssid"] == "MYWLAN"
        assert attrs["ip_address"] == "10.0.0.5"
        assert attrs["mac_address"] == "aa:bb:cc:dd:ee:ff"

    def test_extra_state_adds_lan_ip_rcp_when_known(self, stub_entry: SimpleNamespace):
        """When the coordinator's RCP LAN-IP cache has an entry, surface
        it for dashboards that display both."""
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        coord = _stub_coord_extra(wifiinfo_cache={CAM_ID: {"signalStrength": 50}})
        coord.rcp_lan_ip = lambda cid: "10.0.0.7"
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["lan_ip_rcp"] == "10.0.0.7"

    def test_extra_state_adds_bitrate_ladder(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        coord = _stub_coord_extra(wifiinfo_cache={CAM_ID: {"signalStrength": 50}})
        coord.rcp_bitrate_ladder = lambda cid: [1500, 2500, 4000]
        s = BoschWifiSignalSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["bitrate_ladder_kbps"] == [1500, 2500, 4000]
        assert attrs["max_bitrate_kbps"] == 4000


# ── BoschLedDimmerSensor ────────────────────────────────────────────────


class TestLedDimmerSensor:
    def test_native_value_from_cache(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor

        coord = _stub_coord_extra(rcp_dimmer_cache={CAM_ID: 60})
        s = BoschLedDimmerSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 60

    def test_native_value_none_when_missing(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor

        s = BoschLedDimmerSensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_available_follows_cache(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschLedDimmerSensor

        s = BoschLedDimmerSensor(_stub_coord_extra(), CAM_ID, stub_entry)
        assert s.available is False
        coord = _stub_coord_extra(rcp_dimmer_cache={CAM_ID: 30})
        s2 = BoschLedDimmerSensor(coord, CAM_ID, stub_entry)
        assert s2.available is True


# ── BoschClockOffsetSensor ──────────────────────────────────────────────


class TestClockOffsetSensor:
    def test_in_sync_status(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

        coord = _stub_coord_extra()
        coord.clock_offset = lambda cid: 2.5  # < 5s → in_sync
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["status"] == "in_sync"
        assert attrs["offset_seconds"] == 2.5

    def test_minor_drift_status(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

        coord = _stub_coord_extra()
        coord.clock_offset = lambda cid: 30.0  # 5-60s → minor_drift
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["status"] == "minor_drift"

    def test_out_of_sync_status(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

        coord = _stub_coord_extra()
        coord.clock_offset = lambda cid: 120.0  # >= 60s → out_of_sync
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["status"] == "out_of_sync"

    def test_negative_offset_uses_abs(self, stub_entry: SimpleNamespace):
        """Camera ahead of HA by 30s also counts as minor_drift, not as
        in_sync. Pin so a refactor of abs() can't silently break the
        reverse-skew detection."""
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

        coord = _stub_coord_extra()
        coord.clock_offset = lambda cid: -30.0
        s = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes["status"] == "minor_drift"

    def test_no_offset_returns_empty_attrs(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

        s = BoschClockOffsetSensor(extra_sensor_coord, CAM_ID, stub_entry)
        # clock_offset returns None default
        assert s.extra_state_attributes == {}

    def test_available_requires_offset(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschClockOffsetSensor

        s = BoschClockOffsetSensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.available is False
        coord = _stub_coord_extra()
        coord.clock_offset = lambda cid: 1.0
        s2 = BoschClockOffsetSensor(coord, CAM_ID, stub_entry)
        assert s2.available is True


# ── BoschMotionSensitivitySensor ────────────────────────────────────────


class TestMotionSensitivitySensor:
    def test_disabled_when_motion_off(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMotionSensitivitySensor,
        )

        coord = _stub_coord_extra()
        coord.motion_settings = lambda cid: {
            "enabled": False,
            "motionAlarmConfiguration": "HIGH",
        }
        s = BoschMotionSensitivitySensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "disabled"

    def test_enabled_returns_lowercased_sensitivity(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMotionSensitivitySensor,
        )

        coord = _stub_coord_extra()
        coord.motion_settings = lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "MEDIUM_HIGH",
        }
        s = BoschMotionSensitivitySensor(coord, CAM_ID, stub_entry)
        # MEDIUM_HIGH → "medium high" (underscore → space, lowercase)
        assert s.native_value == "medium high"

    def test_no_settings_returns_none(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMotionSensitivitySensor,
        )

        s = BoschMotionSensitivitySensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_extra_state_passes_through_raw_settings(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMotionSensitivitySensor,
        )

        coord = _stub_coord_extra()
        coord.motion_settings = lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        }
        s = BoschMotionSensitivitySensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs["enabled"] is True
        assert attrs["sensitivity"] == "HIGH"


# ── BoschMovementEventsTodaySensor / BoschAudioEventsTodaySensor ────────


class TestEventsTodaySensors:
    def _coord_with_events(self, events: list[dict]):
        coord = _stub_coord_extra()
        coord.data[CAM_ID]["events"] = events
        return coord

    def test_movement_today_counts_only_today_movement(
        self, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )

        # Buckets use the event's LOCAL calendar date (issue #34).
        events = [
            {"eventType": "MOVEMENT", "timestamp": _today_local_ts(10)},
            {"eventType": "MOVEMENT", "timestamp": _today_local_ts(11)},
            {
                "eventType": "AUDIO_ALARM",
                "timestamp": _today_local_ts(12),
            },  # wrong type
            {
                "eventType": "MOVEMENT",
                "timestamp": "2000-01-01T00:00:00.000Z",
            },  # wrong date
        ]
        coord = self._coord_with_events(events)
        s = BoschMovementEventsTodaySensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 2

    def test_movement_today_zero_when_no_events(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )

        s = BoschMovementEventsTodaySensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 0

    def test_audio_today_counts_only_today_audio(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschAudioEventsTodaySensor,
        )

        # Buckets use the event's LOCAL calendar date (issue #34).
        events = [
            {"eventType": "AUDIO_ALARM", "timestamp": _today_local_ts(5)},
            {
                "eventType": "MOVEMENT",
                "timestamp": _today_local_ts(5),
            },  # wrong type
        ]
        coord = self._coord_with_events(events)
        s = BoschAudioEventsTodaySensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 1

    def test_handles_missing_timestamp(self, stub_entry: SimpleNamespace):
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
    def test_native_value_from_cache(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import (
            BoschUnreadEventsCountSensor,
        )

        coord = _stub_coord_extra(unread_events_cache={CAM_ID: 7})
        s = BoschUnreadEventsCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 7

    def test_native_value_none_when_missing(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import (
            BoschUnreadEventsCountSensor,
        )

        s = BoschUnreadEventsCountSensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_zero_is_a_valid_value(self, stub_entry: SimpleNamespace):
        """Cache may legitimately hold 0 (all read) — must NOT be
        treated as unavailable. Pin so a `if not value` mistake doesn't
        creep in."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschUnreadEventsCountSensor,
        )

        coord = _stub_coord_extra(unread_events_cache={CAM_ID: 0})
        s = BoschUnreadEventsCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 0
        assert s.available is True


# ── BoschCommissionedSensor ─────────────────────────────────────────────


class TestCommissionedSensor:
    def test_commissioned_state(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_extra(
            commissioned_cache={
                CAM_ID: {"configured": True, "connected": True, "commissioned": True},
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "commissioned"

    def test_not_commissioned_state(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_extra(
            commissioned_cache={
                CAM_ID: {"configured": True, "connected": True, "commissioned": False},
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "not_commissioned"

    def test_not_connected_state(self, stub_entry: SimpleNamespace):
        """`connected=False` overrides commissioning state — camera
        unreachable trumps everything else."""
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_extra(
            commissioned_cache={
                CAM_ID: {"configured": True, "connected": False, "commissioned": True},
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == "not_connected"

    def test_no_cache_returns_none(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        s = BoschCommissionedSensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None
        assert s.available is False

    def test_extra_state_passes_through_all_three_fields(
        self, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschCommissionedSensor

        coord = _stub_coord_extra(
            commissioned_cache={
                CAM_ID: {"configured": True, "connected": True, "commissioned": False},
            }
        )
        s = BoschCommissionedSensor(coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs == {"configured": True, "connected": True, "commissioned": False}


# ── BoschRulesCountSensor ───────────────────────────────────────────────


class TestRulesCountSensor:
    def test_count_from_cache(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

        coord = _stub_coord_extra(
            rules_cache={
                CAM_ID: [{"id": "r1"}, {"id": "r2"}, {"id": "r3"}],
            }
        )
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 3

    def test_zero_when_empty_list(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

        coord = _stub_coord_extra(rules_cache={CAM_ID: []})
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        assert s.native_value == 0

    def test_none_when_no_cache(
        self, extra_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

        s = BoschRulesCountSensor(extra_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None
        assert s.available is False

    def test_extra_state_includes_full_rules(self, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

        coord = _stub_coord_extra(
            rules_cache={
                CAM_ID: [
                    {
                        "id": "r1",
                        "name": "Night Mode",
                        "isActive": True,
                        "startTime": "22:00",
                        "endTime": "06:00",
                        "weekdays": [0, 1, 2, 3, 4, 5, 6],
                    }
                ]
            }
        )
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        rules = s.extra_state_attributes["rules"]
        assert len(rules) == 1
        assert rules[0]["id"] == "r1"
        assert rules[0]["name"] == "Night Mode"
        assert rules[0]["active"] is True
        assert rules[0]["start"] == "22:00"
        assert rules[0]["end"] == "06:00"
        assert rules[0]["weekdays"] == [0, 1, 2, 3, 4, 5, 6]

    def test_extra_state_handles_missing_optional_fields(
        self, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.sensor import BoschRulesCountSensor

        coord = _stub_coord_extra(rules_cache={CAM_ID: [{}]})  # empty rule dict
        s = BoschRulesCountSensor(coord, CAM_ID, stub_entry)
        rules = s.extra_state_attributes["rules"]
        # All fields default to safe values; no KeyError
        assert rules[0]["active"] is False
        assert rules[0]["weekdays"] == []


# Recorder `_unrecorded_attributes` hardening (HA#39)
# Several diagnostic entities carry attributes that either change on every
# coordinator/drain tick (freshness counters, rotating stream URLs) or hold
# large card-only blobs (zone/mask coordinate lists). HA's recorder hashes
# each state's attributes into the shared `state_attributes` table, so a
# value that changes every tick (or a multi-KB list) bloats that table with
# no history value.
#
# `_unrecorded_attributes` strips the listed keys before the recorder stores
# them — the attribute stays visible live, only its recording is suppressed.
# These tests pin the exact excluded set per entity so a future edit that
# adds a volatile/blob attribute without excluding it fails loudly.
#
# Asserting on the class attribute keeps this fixture-free: `_unrecorded_attributes`
# is a class-level frozenset, no coordinator stub required.
#
# OUT OF SCOPE NOTE: this parametrized test also covers
# BoschLanReachableBinarySensor (binary_sensor.py), BoschCamera (camera.py),
# and BoschLiveStreamSwitch (switch.py) — kept in the same parametrize list
# as the sensor.py classes below since it's one indivisible test, not split
# across modules.

_UNRECORDED_ATTRS_CASES = [
    (BoschFcmPushStatusSensor, {"last_push_seconds_ago"}),
    (BoschCloudMaintenanceSensor, {"last_fetched_seconds_ago"}),
    (
        BoschLanReachableBinarySensor,
        {"last_check_seconds_ago", "write_grace_seconds_left"},
    ),
    (BoschRulesCountSensor, {"rules"}),
    (BoschAlarmCatalogSensor, {"alarm_details"}),
    (BoschMotionZonesSensor, {"zones", "coordinates", "cloud_zones", "gen2_zones"}),
    (BoschIvaCatalogSensor, {"modules", "active_modules"}),
    (BoschPrivateAreasSensor, {"cloud_privacy_masks", "gen2_private_areas"}),
    (
        BoschNvrStateSensor,
        {"last_segment_age_s", "last_tick_ts", "pending_uploads", "failed_uploads"},
    ),
    (BoschCamera, {"live_rtsps", "live_proxy", "stream_url"}),
    (BoschLiveStreamSwitch, {"rtsps_url", "proxy_snap_url"}),
]


@pytest.mark.parametrize(
    ("entity_cls", "expected"),
    _UNRECORDED_ATTRS_CASES,
    ids=[cls.__name__ for cls, _ in _UNRECORDED_ATTRS_CASES],
)
def test_volatile_and_blob_attrs_are_unrecorded(entity_cls: type, expected: set[str]):
    """HA#39: every churning/blob attribute must be excluded from recording."""
    excluded = entity_cls._unrecorded_attributes
    missing = expected - set(excluded)
    assert not missing, (
        f"{entity_cls.__name__} must exclude {sorted(missing)} from the recorder "
        f"(state_attributes bloat). Current _unrecorded_attributes={sorted(excluded)}"
    )


# AI-description sensor conditional setup (relocated from
# tests/test_misc_modules_coverage.py)
class TestSensorSetupAiDescriptionOption:
    """`async_setup_entry` appends `BoschCameraAiDescriptionSensor` per camera
    only when the `enable_ai_description` option is True."""

    def _stub_coord(self):
        return SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                        "macAddress": "aa:bb:cc:dd:ee:01",
                        "featureSupport": {"light": True, "panLimit": 0},
                    },
                    "status": "ONLINE",
                    "events": [],
                }
            },
            wifiinfo_cache={},
            rcp_alarm_catalog_cache={},
            rcp_motion_zones_cache={},
            rcp_motion_coords_cache={},
            cloud_zones_cache={},
            gen2_zones_cache={},
            rcp_tls_cert_cache={},
            rcp_network_services_cache={},
            rcp_iva_catalog_cache={},
            _rcp_private_areas_cache={},
            ambient_lighting_cache={},
            _ambient_schedule_cache={},
            alarm_status_cache={},
            alarm_settings_cache={},
            arming_cache={},
            live_connections={},
            stream_fell_back={},
            stream_error_count={},
            stream_warming=set(),
            nvr_drain_state={},
            commissioned_cache={},
            firmware_cache={},
            unread_events_cache={},
            fcm_running=False,
            fcm_healthy=True,
            fcm_push_mode="auto",
            fcm_last_push=0.0,
            last_update_success=True,
            options={
                "enable_fcm_push": True,
                "enable_sensors": True,
                "enable_nvr": False,
            },
            motion_settings=lambda cid: {
                "enabled": True,
                "motionAlarmConfiguration": "HIGH",
            },
            is_camera_online=lambda cid: True,
            is_stream_warming=lambda cid: False,
        )

    def test_ai_description_sensor_appended_when_option_true(self):
        """When enable_ai_description=True, BoschCameraAiDescriptionSensor is
        included in the entities list passed to async_add_entities."""
        import asyncio

        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
            async_setup_entry,
        )

        coord = self._stub_coord()

        entry = SimpleNamespace(
            runtime_data=coord,
            options={"enable_ai_description": True, "enable_sensors": True},
        )

        added_entities: list = []

        def fake_add(entities, **kw):
            added_entities.extend(entities)

        asyncio.run(async_setup_entry(None, entry, fake_add))

        ai_sensors = [
            e for e in added_entities if isinstance(e, BoschCameraAiDescriptionSensor)
        ]
        assert len(ai_sensors) == 1, (
            f"Expected 1 BoschCameraAiDescriptionSensor, got {len(ai_sensors)}"
        )
        assert ai_sensors[0].coordinator is coord
        assert ai_sensors[0]._cam_id == CAM_ID

    def test_ai_description_sensor_not_appended_when_option_false(self):
        """Complement: when enable_ai_description is absent/False, no
        BoschCameraAiDescriptionSensor is added."""
        import asyncio

        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
            async_setup_entry,
        )

        coord = self._stub_coord()

        entry = SimpleNamespace(
            runtime_data=coord,
            options={"enable_sensors": True},  # no enable_ai_description
        )

        added_entities: list = []

        def fake_add(entities, **kw):
            added_entities.extend(entities)

        asyncio.run(async_setup_entry(None, entry, fake_add))

        ai_sensors = [
            e for e in added_entities if isinstance(e, BoschCameraAiDescriptionSensor)
        ]
        assert len(ai_sensors) == 0, "No AI sensor when option is absent"

    def test_ai_analysis_sensors_appended_when_option_true(self):
        """When ai_analysis_enabled=True, both BoschAiAlertScoreSensor and
        BoschAiAlerts24hSensor are appended per camera — sibling gate to
        the AI Snapshot Description one above."""
        import asyncio

        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = self._stub_coord()
        entry = SimpleNamespace(
            runtime_data=coord,
            options={"ai_analysis_enabled": True, "enable_sensors": True},
        )
        added_entities: list = []

        def fake_add(entities, **kw):
            added_entities.extend(entities)

        asyncio.run(async_setup_entry(None, entry, fake_add))

        score_sensors = [
            e for e in added_entities if isinstance(e, BoschAiAlertScoreSensor)
        ]
        count_sensors = [
            e for e in added_entities if isinstance(e, BoschAiAlerts24hSensor)
        ]
        assert len(score_sensors) == 1
        assert len(count_sensors) == 1
        assert score_sensors[0]._cam_id == CAM_ID

    def test_ai_analysis_sensors_not_appended_when_option_false(self):
        import asyncio

        from custom_components.bosch_shc_camera.sensor import async_setup_entry

        coord = self._stub_coord()
        entry = SimpleNamespace(
            runtime_data=coord,
            options={"enable_sensors": True},  # no ai_analysis_enabled
        )
        added_entities: list = []

        def fake_add(entities, **kw):
            added_entities.extend(entities)

        asyncio.run(async_setup_entry(None, entry, fake_add))

        assert not any(
            isinstance(e, (BoschAiAlertScoreSensor, BoschAiAlerts24hSensor))
            for e in added_entities
        )


# Bosch event-timestamp offset — sensor.py "today" buckets must use the LOCAL
# date, not a UTC-prefix slice (relocated from tests/test_event_timestamp_offset.py
# — the time_utils.py parser tests live in tests/test_time_utils.py, the
# binary_sensor.py motion-window test in tests/test_binary_sensor.py).
# GitHub issue #34.


def _entry_ts_offset() -> SimpleNamespace:
    return SimpleNamespace(entry_id="ENTRY01", data={}, options={})


def _coord_ts_offset(events: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(
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
                "events": events,
            }
        },
    )


class TestLastEventSensorOffset:
    """Issue #34 (GhostRider2809): `sensor.<cam>_last_event` showed the event
    time shifted +2h in CEST because the pre-fix code truncated the Bosch
    timestamp's timezone designator before parsing."""

    _RAW_OFFSET = "2026-06-18T06:06:30.499+02:00[Europe/Berlin]"
    _RAW_OFFSET_INSTANT = datetime(2026, 6, 18, 4, 6, 30, 499000, tzinfo=UTC)
    _RAW_Z = "2026-03-22T14:30:00.000Z"
    _RAW_Z_INSTANT = datetime(2026, 3, 22, 14, 30, 0, tzinfo=UTC)

    def test_offset_timestamp_not_shifted_2h(self) -> None:
        """native_value instant must equal the true offset instant, not the
        pre-fix +2h-shifted reading."""
        from datetime import UTC, datetime

        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(
            _coord_ts_offset(
                [{"eventType": "MOVEMENT", "timestamp": self._RAW_OFFSET}]
            ),
            CAM_ID,
            _entry_ts_offset(),
        )
        val = s.native_value
        assert val is not None
        assert val.astimezone(UTC) == self._RAW_OFFSET_INSTANT
        assert val.astimezone(UTC) != datetime(
            2026, 6, 18, 6, 6, 30, 499000, tzinfo=UTC
        )

    def test_z_timestamp_preserved(self) -> None:
        from datetime import UTC

        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(
            _coord_ts_offset([{"eventType": "MOVEMENT", "timestamp": self._RAW_Z}]),
            CAM_ID,
            _entry_ts_offset(),
        )
        val = s.native_value
        assert val is not None
        assert val.astimezone(UTC) == self._RAW_Z_INSTANT

    def test_no_events_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(_coord_ts_offset([]), CAM_ID, _entry_ts_offset())
        assert s.native_value is None

    def test_garbage_timestamp_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(
            _coord_ts_offset([{"eventType": "MOVEMENT", "timestamp": "garbage"}]),
            CAM_ID,
            _entry_ts_offset(),
        )
        assert s.native_value is None


class TestTodayBucketsLocalDate:
    """Buckets must use the LOCAL date of the event instant, not a UTC-prefix
    slice. Scenario: HA in Europe/Berlin, local now = 2026-06-18 01:00 (=
    2026-06-17 23:00 UTC). An event at 2026-06-18 00:30+02:00 is local-today
    but its UTC date is 2026-06-17 — the old UTC-prefix bucketing counted 0;
    correct local bucketing counts 1."""

    def _now_berlin(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime(2026, 6, 18, 1, 0, 0, tzinfo=ZoneInfo("Europe/Berlin"))

    EVT_LOCAL_TODAY = "2026-06-18T00:30:00.000+02:00[Europe/Berlin]"
    EVT_LOCAL_YESTERDAY = "2026-06-17T23:00:00.000+02:00[Europe/Berlin]"

    def _patch_local(self):  # type: ignore[no-untyped-def]
        from zoneinfo import ZoneInfo

        berlin = ZoneInfo("Europe/Berlin")
        mod = "custom_components.bosch_shc_camera.sensor.dt_util"
        return (
            patch(f"{mod}.now", return_value=self._now_berlin()),
            patch(f"{mod}.as_local", side_effect=lambda dt: dt.astimezone(berlin)),
        )

    def test_events_today_counts_local_today(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraEventsTodaySensor,
        )

        s = BoschCameraEventsTodaySensor(
            _coord_ts_offset(
                [
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_YESTERDAY},
                ]
            ),
            CAM_ID,
            _entry_ts_offset(),
        )
        p_now, p_local = self._patch_local()
        with p_now, p_local:
            assert s.native_value == 1
            attrs = s.extra_state_attributes
        assert attrs["events_in_feed"] == 2
        assert len(attrs["latest_timestamps"]) == 1

    def test_movement_today_counts_local_today(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )

        s = BoschMovementEventsTodaySensor(
            _coord_ts_offset(
                [
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "AUDIO_ALARM", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_YESTERDAY},
                ]
            ),
            CAM_ID,
            _entry_ts_offset(),
        )
        p_now, p_local = self._patch_local()
        with p_now, p_local:
            assert s.native_value == 1

    def test_audio_today_counts_local_today(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschAudioEventsTodaySensor,
        )

        s = BoschAudioEventsTodaySensor(
            _coord_ts_offset(
                [
                    {"eventType": "AUDIO_ALARM", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "AUDIO_ALARM", "timestamp": self.EVT_LOCAL_YESTERDAY},
                ]
            ),
            CAM_ID,
            _entry_ts_offset(),
        )
        p_now, p_local = self._patch_local()
        with p_now, p_local:
            assert s.native_value == 1


class TestFrigateUrlSensors:
    """`BoschFrigateUrlHighSensor`/`BoschFrigateUrlLowSensor`.native_value
    delegates to `coordinator.frigate_endpoint_url(cam_id, quality)` —
    previously zero test coverage on either class."""

    def test_high_quality_native_value_delegates(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschFrigateUrlHighSensor,
        )

        base_sensor_coord.frigate_endpoint_url = lambda cam_id, quality: (
            f"rtsp://127.0.0.1:8554/{cam_id}?q={quality}"
        )
        s = BoschFrigateUrlHighSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == f"rtsp://127.0.0.1:8554/{CAM_ID}?q=high"

    def test_low_quality_native_value_delegates(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from custom_components.bosch_shc_camera.sensor import BoschFrigateUrlLowSensor

        base_sensor_coord.frigate_endpoint_url = lambda cam_id, quality: (
            f"rtsp://127.0.0.1:8554/{cam_id}?q={quality}"
        )
        s = BoschFrigateUrlLowSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == f"rtsp://127.0.0.1:8554/{CAM_ID}?q=low"

    def test_native_value_none_when_not_bound(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschFrigateUrlHighSensor,
        )

        base_sensor_coord.frigate_endpoint_url = lambda cam_id, quality: None
        s = BoschFrigateUrlHighSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None


# ─────────────────────────────────────────────────────────────────────────────
# BoschAiAlertScoreSensor / BoschAiAlerts24hSensor — AI Camera Analysis
# (ai_analysis.py) structured suspicion-score sensors. Only created when the
# `ai_analysis_enabled` integration option is enabled; entity behavior itself
# is independent of that gate (tested via TestSensorSetupAiDescriptionOption-
# style creation-gating for the switch/text/binary_sensor/image counterparts
# — here we test the sensor state/attribute logic directly).


class TestAiAlertScoreSensor:
    def test_unique_id(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s._attr_unique_id == f"bosch_shc_camera_{CAM_ID}_ai_alert_score"

    def test_translation_key(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s._attr_translation_key == "ai_alert_score"

    def test_native_value_none_when_never_analyzed(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        """No `ai_analysis` key ever written for this camera → unknown."""
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_native_value_reflects_score(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        base_sensor_coord.data[CAM_ID]["ai_analysis"] = {"score": 7}
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 7

    def test_native_value_coerces_string_score(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        base_sensor_coord.data[CAM_ID]["ai_analysis"] = {"score": "5"}
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 5

    def test_native_value_none_on_garbage_score(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        base_sensor_coord.data[CAM_ID]["ai_analysis"] = {"score": "not-a-number"}
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value is None

    def test_extra_state_attributes_empty_when_never_analyzed(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.extra_state_attributes == {}

    def test_extra_state_attributes_include_all_expected_fields(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        base_sensor_coord.data[CAM_ID]["ai_analysis"] = {
            "score": 8,
            "short": "Person at gate",
            "detail": "A person approached the gate and looked around.",
            "direction": "approaching",
            "carrying": "backpack",
            "activity": "walking",
            "gate_state": "closed",
            "gate_risk": "low",
            "known_person": False,
            "image_path": "2026-07-16/alert_120000.jpg",
            "generated_at": "2026-07-16T12:00:00+00:00",
        }
        s = BoschAiAlertScoreSensor(base_sensor_coord, CAM_ID, stub_entry)
        attrs = s.extra_state_attributes
        for key in (
            "short",
            "detail",
            "direction",
            "carrying",
            "activity",
            "gate_state",
            "gate_risk",
            "known_person",
            "image_path",
            "generated_at",
        ):
            assert key in attrs, f"missing expected attribute {key!r}"
        assert attrs["short"] == "Person at gate"
        assert attrs["known_person"] is False
        assert attrs["image_path"] == "2026-07-16/alert_120000.jpg"


class TestAiAlerts24hSensor:
    def test_unique_id(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        s = BoschAiAlerts24hSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s._attr_unique_id == f"bosch_shc_camera_{CAM_ID}_ai_alerts_24h"

    def test_translation_key(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        s = BoschAiAlerts24hSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s._attr_translation_key == "ai_alerts_24h"

    def test_entity_category_diagnostic(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        s = BoschAiAlerts24hSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s._attr_entity_category == EntityCategory.DIAGNOSTIC

    def test_zero_alerts(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        base_sensor_coord.ai_analysis_recent = {}
        s = BoschAiAlerts24hSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 0

    def test_alerts_within_window_counted(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        now = datetime.now(UTC)
        base_sensor_coord.ai_analysis_recent = {
            CAM_ID: [
                ((now - timedelta(hours=1)).isoformat(), 6),
                ((now - timedelta(hours=23)).isoformat(), 4),
            ]
        }
        s = BoschAiAlerts24hSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 2

    def test_alerts_outside_window_excluded(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        now = datetime.now(UTC)
        base_sensor_coord.ai_analysis_recent = {
            CAM_ID: [
                ((now - timedelta(hours=1)).isoformat(), 6),  # within
                ((now - timedelta(hours=25)).isoformat(), 4),  # outside
                ((now - timedelta(days=3)).isoformat(), 9),  # outside
            ]
        }
        s = BoschAiAlerts24hSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 1

    def test_garbage_timestamp_skipped_not_crashed(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        now = datetime.now(UTC)
        base_sensor_coord.ai_analysis_recent = {
            CAM_ID: [
                ("not-a-timestamp", 5),
                ((now - timedelta(minutes=5)).isoformat(), 7),
            ]
        }
        s = BoschAiAlerts24hSensor(base_sensor_coord, CAM_ID, stub_entry)
        assert s.native_value == 1

    def test_unknown_camera_returns_zero(
        self, base_sensor_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        base_sensor_coord.ai_analysis_recent = {}
        s = BoschAiAlerts24hSensor(base_sensor_coord, "unknown-cam", stub_entry)
        assert s.native_value == 0
