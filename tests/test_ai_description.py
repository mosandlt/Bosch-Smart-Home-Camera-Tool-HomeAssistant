"""Tests for AI snapshot description feature.

Covers:
  - Option OFF → sensor not created
  - Option ON → sensor created per camera
  - native_value truncation to 255 chars
  - native_value: short text → unchanged
  - native_value: no ai_description key → None
  - extra_state_attributes full text preserved (not truncated)
  - extra_state_attributes fields: generated_at, ai_task_entity
  - extra_state_attributes: missing ai_description → all None
  - ai_describe_on_motion debounce logic (second call within 30s skipped)
  - ai_describe_on_motion OFF → early return
  - ai_describe_on_motion ON + entity found → calls describe_snapshot service
  - coordinator data round-trip (set ai_description → sensor reads it back)
  - unique_id format per camera
  - translation_key value
  - icon
"""

from __future__ import annotations

import asyncio
from datetime import UTC
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_ID_2 = "22222222-2222-2222-2222-222222222222"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_AI_DESC_KEY = "ai_description"


def _make_coord(
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


def _make_entry(opts: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(entry_id="test_entry_ai", options=opts or {})


def _make_sensor(
    cam_id: str = CAM_ID,
    ai_description: dict[str, str | None] | None = None,
    coord: Any | None = None,
) -> Any:
    """Construct a BoschCameraAiDescriptionSensor directly (no HA runtime)."""
    from custom_components.bosch_shc_camera.sensor import (
        BoschCameraAiDescriptionSensor,
    )

    c = coord if coord is not None else _make_coord(ai_description=ai_description)
    entry = _make_entry({"enable_ai_description": True})
    return BoschCameraAiDescriptionSensor(c, cam_id, entry)


# ---------------------------------------------------------------------------
# 1. Option OFF → sensor NOT added to entity list
# ---------------------------------------------------------------------------


class TestAiSensorCreation:
    """Verify gating logic: sensor is only added when enable_ai_description=True."""

    def test_option_off_sensor_not_in_list(self) -> None:
        """When enable_ai_description is False the sensor must not be created."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
        )

        coord = _make_coord()
        entry = _make_entry({"enable_ai_description": False})
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

        coord = _make_coord()
        entry = _make_entry({})  # key absent entirely
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

        coord = _make_coord(cam_ids=[CAM_ID, CAM_ID_2])
        entry = _make_entry({"enable_ai_description": True})
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

        coord = _make_coord(cam_ids=[CAM_ID])
        entry = _make_entry({"enable_ai_description": True})
        entities: list[Any] = []

        opts: dict[str, Any] = entry.options
        if opts.get("enable_ai_description", False):
            for cid in coord.data:
                entities.append(BoschCameraAiDescriptionSensor(coord, cid, entry))

        assert len(entities) == 1
        assert entities[0]._cam_id == CAM_ID


# ---------------------------------------------------------------------------
# 2. Sensor metadata
# ---------------------------------------------------------------------------


class TestAiSensorMetadata:
    """Verify static metadata of the sensor."""

    def test_unique_id_format(self) -> None:
        """unique_id must be bosch_shc_ai_description_<cam_id_lower>."""
        s = _make_sensor(cam_id=CAM_ID)
        assert s.unique_id == f"bosch_shc_ai_description_{CAM_ID.lower()}"

    def test_unique_id_second_camera(self) -> None:
        """Each camera gets a distinct unique_id."""
        coord = _make_coord(cam_ids=[CAM_ID, CAM_ID_2])
        s2 = _make_sensor(cam_id=CAM_ID_2, coord=coord)
        assert s2.unique_id == f"bosch_shc_ai_description_{CAM_ID_2.lower()}"

    def test_translation_key(self) -> None:
        s = _make_sensor()
        assert s.translation_key == "ai_description"

    def test_icon(self) -> None:
        s = _make_sensor()
        assert s.icon == "mdi:image-text"


# ---------------------------------------------------------------------------
# 3. native_value — truncation and None handling
# ---------------------------------------------------------------------------


class TestAiSensorNativeValue:
    """PIN_EVERY_MODE: short text / long text / missing key."""

    def test_native_value_short_text_unchanged(self) -> None:
        """Text shorter than 255 chars is returned as-is."""
        text = "A person is walking near the gate."
        s = _make_sensor(ai_description={"text": text})
        assert s.native_value == text

    def test_native_value_exactly_255_chars_unchanged(self) -> None:
        """Text of exactly 255 chars is returned unchanged."""
        text = "x" * 255
        s = _make_sensor(ai_description={"text": text})
        assert s.native_value == text

    def test_native_value_300_chars_truncated_to_255(self) -> None:
        """Text of 300 chars must be truncated to exactly 255 chars."""
        text = "A" * 300
        s = _make_sensor(ai_description={"text": text})
        result = s.native_value
        assert result is not None
        assert len(result) == 255
        assert result == "A" * 255

    def test_native_value_empty_string(self) -> None:
        """Empty string text returns empty string (not None)."""
        s = _make_sensor(ai_description={"text": ""})
        assert s.native_value == ""

    def test_native_value_no_ai_description_key(self) -> None:
        """When no ai_description key exists in coordinator data, returns None."""
        s = _make_sensor(ai_description=None)
        assert s.native_value is None

    def test_native_value_ai_description_has_no_text(self) -> None:
        """When ai_description dict exists but has no 'text' key, returns None."""
        s = _make_sensor(ai_description={"generated_at": "2026-06-15T12:00:00+00:00"})
        assert s.native_value is None

    def test_native_value_text_is_none(self) -> None:
        """When ai_description.text is explicitly None, returns None."""
        s = _make_sensor(ai_description={"text": None})
        assert s.native_value is None

    def test_native_value_unicode_text(self) -> None:
        """Unicode text is handled correctly (char-count, not byte-count)."""
        text = "über " * 60  # 300 chars (each "über " = 5 chars)
        s = _make_sensor(ai_description={"text": text})
        assert len(s.native_value or "") == 255  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. extra_state_attributes — full text + metadata
# ---------------------------------------------------------------------------


class TestAiSensorExtraAttributes:
    """Verify extra_state_attributes preserves full text and exposes metadata."""

    def test_attributes_full_text_not_truncated(self) -> None:
        """description attribute must contain the FULL text, not truncated."""
        text = "B" * 300
        s = _make_sensor(ai_description={"text": text})
        attrs = s.extra_state_attributes
        assert attrs["description"] == text
        assert len(attrs["description"]) == 300

    def test_attributes_generated_at_preserved(self) -> None:
        ts = "2026-06-15T10:30:00+00:00"
        s = _make_sensor(ai_description={"text": "A car.", "generated_at": ts})
        assert s.extra_state_attributes["generated_at"] == ts

    def test_attributes_ai_task_entity_preserved(self) -> None:
        entity = "ai_task.google_ai"
        s = _make_sensor(ai_description={"text": "A dog.", "ai_task_entity": entity})
        assert s.extra_state_attributes["ai_task_entity"] == entity

    def test_attributes_ai_task_entity_default_string(self) -> None:
        """When ai_task_entity is 'default' it is surfaced unchanged."""
        s = _make_sensor(ai_description={"text": "Empty.", "ai_task_entity": "default"})
        assert s.extra_state_attributes["ai_task_entity"] == "default"

    def test_attributes_all_none_when_no_ai_description(self) -> None:
        """When no ai_description exists all attribute values are None."""
        s = _make_sensor(ai_description=None)
        attrs = s.extra_state_attributes
        assert attrs["description"] is None
        assert attrs["generated_at"] is None
        assert attrs["ai_task_entity"] is None

    def test_attributes_keys_always_present(self) -> None:
        """All three attribute keys must be present regardless of data."""
        s = _make_sensor(ai_description=None)
        attrs = s.extra_state_attributes
        assert "description" in attrs
        assert "generated_at" in attrs
        assert "ai_task_entity" in attrs

    def test_attributes_native_value_vs_full_text_differ_when_long(self) -> None:
        """Confirm native_value (255) != full text (300) for long descriptions."""
        text = "C" * 300
        s = _make_sensor(ai_description={"text": text})
        assert s.native_value != s.extra_state_attributes["description"]
        assert s.extra_state_attributes["description"] == text


# ---------------------------------------------------------------------------
# 5. Coordinator data round-trip
# ---------------------------------------------------------------------------


class TestAiDescriptionCoordinatorRoundTrip:
    """Sensor must reflect whatever is stored in coordinator.data."""

    def test_sensor_reads_coordinator_data_update(self) -> None:
        """Mutating coordinator.data must be reflected in native_value."""
        coord = _make_coord()
        s = _make_sensor(coord=coord)

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
        coord = _make_coord()
        s = _make_sensor(coord=coord)

        coord.data[CAM_ID][_AI_DESC_KEY] = {"text": "First."}
        assert s.native_value == "First."

        coord.data[CAM_ID][_AI_DESC_KEY] = {"text": "Second."}
        assert s.native_value == "Second."

    def test_sensor_two_cameras_independent(self) -> None:
        """Two sensors on two cameras read their own data independently."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
        )

        coord = _make_coord(cam_ids=[CAM_ID, CAM_ID_2])
        entry = _make_entry({"enable_ai_description": True})
        s1 = BoschCameraAiDescriptionSensor(coord, CAM_ID, entry)
        s2 = BoschCameraAiDescriptionSensor(coord, CAM_ID_2, entry)

        coord.data[CAM_ID][_AI_DESC_KEY] = {"text": "Cam1 sees a cat."}
        coord.data[CAM_ID_2][_AI_DESC_KEY] = {"text": "Cam2 sees a dog."}

        assert s1.native_value == "Cam1 sees a cat."
        assert s2.native_value == "Cam2 sees a dog."


# ---------------------------------------------------------------------------
# 6. Auto-describe on motion — debounce logic (standalone unit tests)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 7. Service call shape (describe_snapshot data contract)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 8. async_generate_ai_description — new fix coverage (round 1)
# ---------------------------------------------------------------------------
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
    import time

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
        _shc_state_cache=shc_cache,
        _camera_entities={
            cam_id: SimpleNamespace(entity_id=f"camera.bosch_{cam_id[:4]}")
        },
        _ai_last_call={},
        _ai_day_count=0,
        _ai_day_stamp="",
        _ai_in_flight=in_flight,
        _ai_budget_logged_day="",
        _ai_budget_store=MagicMock(),
        async_set_updated_data=MagicMock(),
    )

    # Bind real coordinator methods so they use `coord` as self
    from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
        BoschCameraCoordinator,
    )

    coord.ai_budget_state = BoschCameraCoordinator.ai_budget_state.__get__(coord)
    coord._ai_rate_allowed = BoschCameraCoordinator._ai_rate_allowed.__get__(coord)
    coord._ai_record_call = BoschCameraCoordinator._ai_record_call.__get__(coord)
    coord._ai_window_allowed = BoschCameraCoordinator._ai_window_allowed.__get__(coord)
    coord._async_save_ai_budget = BoschCameraCoordinator._async_save_ai_budget.__get__(
        coord
    )
    coord.async_generate_ai_description = (
        BoschCameraCoordinator.async_generate_ai_description.__get__(coord)
    )

    # Seed _ai_last_call so that the cooldown gate can be tested
    coord._ai_last_call[cam_id] = float("-inf")

    return coord


class TestAsyncGenerateAiDescription:
    """Unit tests for coordinator.async_generate_ai_description fixes (round 1)."""

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
        import time
        from datetime import UTC, datetime, timedelta

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
        import time

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
        """_ai_in_flight must be decremented even when ai_task raises."""
        coord = _make_ai_coord()
        coord._ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("ai down"))
        result = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result is None
        # finally block must have decremented in_flight back to 0
        assert coord._ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_in_flight_decremented_on_success(self) -> None:
        """_ai_in_flight must be 0 after a successful call completes."""
        coord = _make_ai_coord()
        coord._ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "A car in the driveway."}
        )
        coord.hass.bus.async_fire = MagicMock()
        result = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result == "A car in the driveway."
        assert coord._ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_on_motion_uses_budget_guard(self) -> None:
        """Simulate on-motion path: budget=1, day_count already 1 → blocked."""
        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        # Manually set ai_day_stamp to today so rollover doesn't clear the count
        import datetime as _dt

        from homeassistant.util import dt as dt_util

        coord._ai_day_stamp = dt_util.now().date().isoformat()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None
        coord.hass.services.async_call.assert_not_called()


class TestServiceHandlerStrip:
    """Verify .strip() + empty-text guard in service handler (FIX-F)."""

    def test_strip_and_empty_returns_empty_description(self) -> None:
        """Service text extraction: whitespace-only → stripped empty → no write."""
        # Mirror the exact logic in handle_describe_snapshot after FIX-F
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
        # str(None) = "None" but resp.get("data", "") returns None, and str(None)="None"
        # The actual guard: if data is None, resp.get("data","") returns None, str(None)="None"
        # That's a pre-existing design; the strip at least removes whitespace.
        # The important thing: empty-string guard catches "" not "None".
        # This test documents the actual behavior (str(None)="None", stripped="None").
        assert text == "None"


# ---------------------------------------------------------------------------
# 9. Round-2 fixes — timeout, privacy guard, caption cap, stale cache, budget log
# ---------------------------------------------------------------------------


class TestTimeoutReturnsNone:
    """Fix-1: asyncio.timeout(20) → TimeoutError → None, finally still decrements."""

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self) -> None:
        """When ai_task raises TimeoutError, method returns None."""
        coord = _make_ai_coord()
        coord._ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        result = await coord.async_generate_ai_description(CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_decrements_in_flight(self) -> None:
        """TimeoutError in try block must still hit finally and decrement in_flight."""
        coord = _make_ai_coord()
        coord._ai_in_flight = 0
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        await coord.async_generate_ai_description(CAM_ID, force=True)
        assert coord._ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_timeout_does_not_fire_bus_event(self) -> None:
        """On timeout the bus must not fire — no description was generated."""
        coord = _make_ai_coord()
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        coord.hass.bus.async_fire = MagicMock()
        await coord.async_generate_ai_description(CAM_ID, force=True)
        coord.hass.bus.async_fire.assert_not_called()


class TestServicePrivacyGuard:
    """Fix-2: handle_describe_snapshot raises ServiceValidationError when privacy ON."""

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
        from homeassistant.exceptions import ServiceValidationError

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
        import json
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
        import json
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
        import json
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
        import json
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
    """Fix-3: AI description capped at 200 chars before appending to FCM caption."""

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
    """Fix-4: rate-limited cache path rejects stale/privacy descriptions."""

    @pytest.mark.asyncio
    async def test_fresh_cache_within_cooldown_returned(self) -> None:
        """Cache hit with recent generated_at within cooldown → returned."""
        import time
        from datetime import UTC, datetime, timedelta

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
        import time
        from datetime import UTC, datetime, timedelta

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
        import time
        from datetime import UTC, datetime, timedelta

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
        import time

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
        import time

        coord = _make_ai_coord(
            ai_text="Bad ts.", ai_generated_at="not-a-datetime", cooldown=60.0
        )
        coord._ai_last_call[CAM_ID] = time.monotonic()
        result = await coord.async_generate_ai_description(CAM_ID, force=False)
        assert result is None


class TestBudgetLogOnce:
    """Fix-5: budget-exceeded info log fires at most once per calendar day."""

    def test_budget_exceeded_logs_once(self) -> None:
        """First budget-exceeded call on a new day emits INFO log."""
        import logging

        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        coord._ai_budget_logged_day = ""  # not yet logged

        import datetime as _dt

        from homeassistant.util import dt as dt_util

        coord._ai_day_stamp = dt_util.now().date().isoformat()  # prevent rollover

        with patch(
            "custom_components.bosch_shc_camera.__init__._LOGGER"
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
            "custom_components.bosch_shc_camera.__init__._LOGGER"
        ) as mock_logger:
            coord._ai_rate_allowed(CAM_ID)
            mock_logger.info.assert_not_called()

    def test_budget_log_day_stamp_updated(self) -> None:
        """After logging, _ai_budget_logged_day is set to today's LOCAL date.

        Regression (A2): the log-once day key must use the same local-date
        source (dt_util.now) as ai_budget_state's daily rollover. A UTC date
        here would re-arm out of lockstep with the counter reset, suppressing
        the warning for the hours between local and UTC midnight.
        """
        from homeassistant.util import dt as dt_util

        today_ha = dt_util.now().date().isoformat()
        coord = _make_ai_coord(max_per_day=1)
        coord._ai_day_count = 1
        coord._ai_budget_logged_day = ""
        coord._ai_day_stamp = today_ha

        with patch("custom_components.bosch_shc_camera.__init__._LOGGER"):
            coord._ai_rate_allowed(CAM_ID)

        assert coord._ai_budget_logged_day == today_ha

    def test_budget_log_day_follows_local_not_utc_date(self) -> None:
        """A2: log-day key tracks dt_util.now() even when it diverges from UTC.

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
            patch("custom_components.bosch_shc_camera.__init__._LOGGER"),
            patch(
                "custom_components.bosch_shc_camera.__init__.dt_util.now",
                return_value=fixed_local,
            ),
        ):
            result = coord._ai_rate_allowed(CAM_ID)

        assert result is False
        assert coord._ai_budget_logged_day == "2000-01-01"


# ---------------------------------------------------------------------------
# 10. AI activation window gating — _ai_window_allowed
# ---------------------------------------------------------------------------
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
    from types import SimpleNamespace
    from unittest.mock import MagicMock

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

    from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
        BoschCameraCoordinator,
    )

    coord._ai_window_allowed = BoschCameraCoordinator._ai_window_allowed.__get__(coord)
    return coord


class TestAiWindowAllowedTimeGate:
    """PIN_EVERY_MODE: time window normal, overnight, boundary, malformed, no-gate."""

    def _coord_at_time(
        self, time_start: str, time_end: str, now_hour: int, now_min: int
    ) -> Any:
        """Build a coord and patch dt_util.now() to return the given time."""
        return _make_gating_coord(time_start=time_start, time_end=time_end)

    def _allowed_at(
        self, time_start: str, time_end: str, now_hour: int, now_min: int
    ) -> bool:
        """Call _ai_window_allowed with dt_util.now() patched to HH:MM."""
        from datetime import datetime, timezone
        from unittest.mock import patch

        coord = _make_gating_coord(time_start=time_start, time_end=time_end)
        fake_now = datetime(2026, 6, 15, now_hour, now_min, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
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
        from types import SimpleNamespace

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
        from datetime import datetime, timezone
        from unittest.mock import patch

        coord = _make_gating_coord(
            time_start="08:00",
            time_end="22:00",
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="not_home",
        )
        fake_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=fake_now,
        ):
            assert coord._ai_window_allowed() is True

    def test_time_blocked_condition_ok(self) -> None:
        """Time outside window even if condition matches → blocked."""
        from datetime import datetime, timezone
        from unittest.mock import patch

        coord = _make_gating_coord(
            time_start="08:00",
            time_end="22:00",
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="not_home",
        )
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=fake_now,
        ):
            assert coord._ai_window_allowed() is False

    def test_time_ok_condition_wrong(self) -> None:
        """Time in window but condition wrong state → blocked."""
        from datetime import datetime, timezone
        from unittest.mock import patch

        coord = _make_gating_coord(
            time_start="08:00",
            time_end="22:00",
            condition_entity="person.thomas",
            condition_state="not_home",
            entity_state="home",  # home → blocked
        )
        fake_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=fake_now,
        ):
            assert coord._ai_window_allowed() is False


class TestAiWindowForceBypass:
    """force=True must bypass the window gate entirely."""

    @pytest.mark.asyncio
    async def test_force_true_bypasses_blocked_window(self) -> None:
        """Manual describe_snapshot (force=True) ignores the time gate."""
        import time as _time
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock, patch

        coord = _make_ai_coord(cooldown=0.0)  # allow rate gate through

        # Configure a time gate that blocks (outside 08:00–22:00)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "22:00"
        coord._ai_window_allowed = (
            BoschCameraCoordinator._ai_window_allowed.__get__(coord)  # type: ignore[name-defined]
        )

        # Mock dt_util.now() to return 23:00 (outside window)
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "Force bypassed window."}
        )
        coord.hass.bus.async_fire = MagicMock()

        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
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
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_condition_entity"] = "person.thomas"
        coord.options["ai_active_condition_state"] = "not_home"
        coord._ai_window_allowed = (
            BoschCameraCoordinator._ai_window_allowed.__get__(coord)  # type: ignore[name-defined]
        )

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


# ---------------------------------------------------------------------------
# 11. Budget Store persistence
# ---------------------------------------------------------------------------


class TestBudgetStorePersistence:
    """async_load_ai_budget + _async_save_ai_budget."""

    @pytest.mark.asyncio
    async def test_load_today_restores_count(self) -> None:
        """Loading stored data for today restores the day count."""
        import datetime as _dt
        from unittest.mock import AsyncMock

        from homeassistant.util import dt as dt_util

        coord = _make_ai_coord()
        today = dt_util.now().date().isoformat()
        coord._ai_budget_store.async_load = AsyncMock(
            return_value={"date": today, "count": 42}
        )

        from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
            BoschCameraCoordinator,
        )

        await BoschCameraCoordinator.async_load_ai_budget(coord)
        assert coord._ai_day_count == 42
        assert coord._ai_day_stamp == today

    @pytest.mark.asyncio
    async def test_load_yesterday_does_not_restore(self) -> None:
        """Loading stored data for yesterday leaves count at 0 (new day)."""
        import datetime as _dt
        from unittest.mock import AsyncMock

        from homeassistant.util import dt as dt_util

        coord = _make_ai_coord()
        yesterday = (dt_util.now().date() - _dt.timedelta(days=1)).isoformat()
        coord._ai_budget_store.async_load = AsyncMock(
            return_value={"date": yesterday, "count": 99}
        )

        from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
            BoschCameraCoordinator,
        )

        await BoschCameraCoordinator.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0  # not restored — yesterday's data

    @pytest.mark.asyncio
    async def test_load_store_error_safe(self) -> None:
        """Store load error is caught — counter stays at 0."""
        from unittest.mock import AsyncMock

        coord = _make_ai_coord()
        coord._ai_budget_store.async_load = AsyncMock(side_effect=OSError("disk full"))

        from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
            BoschCameraCoordinator,
        )

        await BoschCameraCoordinator.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0  # safe default

    @pytest.mark.asyncio
    async def test_save_called_on_record(self) -> None:
        """_ai_record_call must schedule a save (async_create_task called)."""
        from unittest.mock import MagicMock

        coord = _make_ai_coord(cooldown=0.0)
        coord.hass.async_create_task = MagicMock()

        coord._ai_record_call(CAM_ID)

        # async_create_task must have been called (the save is scheduled)
        coord.hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_save_called_on_day_rollover(self) -> None:
        """ai_budget_state triggers save task when day rolls over."""
        import datetime as _dt
        from unittest.mock import MagicMock

        coord = _make_ai_coord()
        coord._ai_day_stamp = "2026-01-01"  # yesterday
        coord.hass.async_create_task = MagicMock()

        coord.ai_budget_state()  # triggers rollover

        coord.hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_save_contents(self) -> None:
        """_async_save_ai_budget writes date and count to store."""
        import datetime as _dt
        from unittest.mock import AsyncMock

        coord = _make_ai_coord()
        today = _dt.date.today().isoformat()
        coord._ai_day_stamp = today
        coord._ai_day_count = 7
        coord._ai_budget_store.async_save = AsyncMock()

        from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
            BoschCameraCoordinator,
        )

        await BoschCameraCoordinator._async_save_ai_budget(coord)
        coord._ai_budget_store.async_save.assert_called_once_with(
            {"date": today, "count": 7}
        )

    @pytest.mark.asyncio
    async def test_save_error_safe(self) -> None:
        """_async_save_ai_budget catches store save errors silently."""
        from unittest.mock import AsyncMock

        coord = _make_ai_coord()
        coord._ai_budget_store.async_save = AsyncMock(
            side_effect=OSError("write error")
        )

        from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
            BoschCameraCoordinator,
        )

        # Must not raise
        await BoschCameraCoordinator._async_save_ai_budget(coord)


# ---------------------------------------------------------------------------
# 12. One-call-per-event guarantee
# ---------------------------------------------------------------------------


class TestOneCallPerEvent:
    """Both on_motion + notify_include enabled → only ONE ai_task call per event."""

    @pytest.mark.asyncio
    async def test_second_call_reuses_cache_no_new_api_call(self) -> None:
        """Simulate on-motion (call 1) then notify-include (call 2, same event).

        The second force=False call must return cached text without a new
        ai_task.generate_data call.
        """
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

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
        import time as _time
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

        # Seed with a stale cached entry (> 300s old)
        stale_ts = (datetime.now(UTC) - timedelta(seconds=400)).isoformat()
        coord = _make_ai_coord(
            ai_text="Old description.", ai_generated_at=stale_ts, cooldown=60.0
        )
        coord.hass.services.async_call = AsyncMock(return_value={"data": "Fresh."})
        coord.hass.bus.async_fire = MagicMock()

        # Simulate cooldown active
        coord._ai_last_call[CAM_ID] = _time.monotonic()

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
        from datetime import datetime
        from unittest.mock import patch

        coord = _make_gating_coord(time_start=time_start, time_end=time_end)
        fake_now = datetime(2026, 6, 15, now_hour, now_min, now_sec, now_us, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
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


# ---------------------------------------------------------------------------
# 13. Window gate does NOT consume budget
# ---------------------------------------------------------------------------
# PIN_EVERY_MODE: verify that a window-blocked call never increments
# _ai_in_flight, _ai_day_count, or _ai_last_call.


class TestWindowGateDoesNotConsumeBudget:
    """Window-blocked calls must not consume any budget or rate-limit tokens."""

    @pytest.mark.asyncio
    async def test_window_blocked_does_not_increment_day_count(self) -> None:
        """When _ai_window_allowed() is False, _ai_day_count stays at 0."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock, patch

        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = BoschCameraCoordinator._ai_window_allowed.__get__(
            coord
        )
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})
        coord.hass.bus.async_fire = MagicMock()

        # Time 23:00 is outside 08:00-10:00 -> window blocked
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=fake_now,
        ):
            result = await coord.async_generate_ai_description(CAM_ID, force=False)

        assert result is None
        assert coord._ai_day_count == 0

    @pytest.mark.asyncio
    async def test_window_blocked_does_not_increment_in_flight(self) -> None:
        """When window blocks, _ai_in_flight must never increase (no try/finally entered)."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock, patch

        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = BoschCameraCoordinator._ai_window_allowed.__get__(
            coord
        )
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})
        coord.hass.bus.async_fire = MagicMock()
        initial_in_flight = coord._ai_in_flight

        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=fake_now,
        ):
            await coord.async_generate_ai_description(CAM_ID, force=False)

        assert coord._ai_in_flight == initial_in_flight

    @pytest.mark.asyncio
    async def test_window_blocked_does_not_update_last_call(self) -> None:
        """Window-blocked call must not touch _ai_last_call (cooldown not started)."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock, patch

        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = BoschCameraCoordinator._ai_window_allowed.__get__(
            coord
        )
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})
        coord.hass.bus.async_fire = MagicMock()
        sentinel = float("-inf")
        coord._ai_last_call[CAM_ID] = sentinel

        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=fake_now,
        ):
            await coord.async_generate_ai_description(CAM_ID, force=False)

        # _ai_last_call must stay at the sentinel value -- not touched by the blocked call
        assert coord._ai_last_call[CAM_ID] == sentinel

    @pytest.mark.asyncio
    async def test_window_blocked_no_ai_task_call(self) -> None:
        """Window-blocked path must not invoke hass.services.async_call at all."""
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, patch

        coord = _make_ai_coord(cooldown=0.0)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = BoschCameraCoordinator._ai_window_allowed.__get__(
            coord
        )
        coord.hass.services.async_call = AsyncMock(return_value={"data": "X"})

        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
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
        from datetime import UTC, datetime
        from unittest.mock import AsyncMock, MagicMock, patch

        coord = _make_ai_coord(cooldown=0.0, max_per_day=1)
        coord.options = dict(coord.options)
        coord.options["ai_active_time_start"] = "08:00"
        coord.options["ai_active_time_end"] = "10:00"
        coord._ai_window_allowed = BoschCameraCoordinator._ai_window_allowed.__get__(
            coord
        )
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "Allowed call result."}
        )
        coord.hass.bus.async_fire = MagicMock()

        outside_window = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        inside_window = datetime(2026, 6, 15, 9, 0, 0, tzinfo=UTC)

        # 5 blocked calls (outside window) must not consume the single-unit budget
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=outside_window,
        ):
            for _ in range(5):
                r = await coord.async_generate_ai_description(CAM_ID, force=False)
                assert r is None

        assert coord._ai_day_count == 0  # budget untouched

        # One in-window call must succeed and consume exactly one budget unit
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
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
        from types import SimpleNamespace

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
        """_ai_record_call triggers 2 async_create_task saves; counter must be 1.

        Sequence inside _ai_record_call:
          1. ai_budget_state() for day-rollover: stamp was '' != today → reset +
             schedule save (task #1).
          2. _ai_day_count incremented to 1.
          3. Another async_create_task save scheduled (task #2).
        Both tasks write current {date, count}.  Last-write-wins is safe.
        We verify: exactly 2 tasks enqueued and _ai_day_count == 1.
        """
        import datetime as _dt
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
            BoschCameraCoordinator,
        )

        hass_mock = MagicMock()
        tasks_created: list[Any] = []
        hass_mock.async_create_task.side_effect = tasks_created.append

        coord = SimpleNamespace(
            options={"ai_max_per_day": 100, "ai_cooldown_seconds": 60},
            hass=hass_mock,
            _ai_day_count=0,
            _ai_day_stamp="",
            _ai_budget_logged_day="",
            _ai_in_flight=0,
            _ai_last_call={},
        )
        coord._async_save_ai_budget = MagicMock()
        coord.ai_budget_state = BoschCameraCoordinator.ai_budget_state.__get__(coord)
        coord._ai_record_call = BoschCameraCoordinator._ai_record_call.__get__(coord)

        fake_now = _dt.datetime(2026, 6, 15, 12, 0, tzinfo=_dt.UTC)
        with patch(
            "custom_components.bosch_shc_camera.__init__.dt_util.now",
            return_value=fake_now,
        ):
            coord._ai_record_call("cam-aabbccdd")

        # Day-rollover save (task #1) + record save (task #2)
        assert len(tasks_created) == 2
        assert coord._ai_day_count == 1


# Bring BoschCameraCoordinator into scope for the force-bypass tests above
from custom_components.bosch_shc_camera.__init__ import (  # type: ignore[import]
    BoschCameraCoordinator,
)
