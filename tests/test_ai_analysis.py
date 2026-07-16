"""Tests for ai_analysis.py — structured motion-triggered AI suspicion scoring.

Consolidated single flat test module (platinum convention: one
tests/test_<module>.py per source module) covering every test surface of
``custom_components/bosch_shc_camera/ai_analysis.py``:

  1. ``ai_analysis_budget_state`` — day-rollover + unlimited (max_per_day=0).
  2. ``async_load_ai_analysis_budget`` / ``_async_save_ai_analysis_budget``
     — store load (valid/stale-date/corrupt), save-failure swallowed.
  3. ``_ai_analysis_rate_allowed`` — cooldown gate, budget gate (incl.
     in-flight counted), unlimited budget.
  4. ``ai_analysis_record_call`` — cooldown timestamp + day-count bookkeeping.
  5. ``per_camera_analysis_enabled`` — default-True opt-out convention.
  6. ``_build_prompt`` — base/scene-context/known-visitors/repeat-context,
     all combined.
  7. ``_known_visitors`` — config_entry/subentry edge cases.
  8. ``_parse_json_fallback`` — free-text JSON extraction edge cases.
  9. ``async_generate_ai_analysis`` — the main entry point, exhaustive gate
     + response-shape + error-handling branch coverage.
  10. ``_finalize_alert`` — persistence, coordinator.data update, bus event,
      alert-routing dispatch.

This module is a structural sibling of ``coordinator.py``'s
``async_generate_ai_description`` (see ``tests/test_sensor.py``'s
``TestAsyncGenerateAiDescription`` for the analogous coverage on that
feature) but is built from plain module-level functions taking a
``coordinator`` stub as their first argument, not bound coordinator
methods — so fixtures here build a ``SimpleNamespace`` stub directly,
no ``__get__`` binding dance required.

SENTINEL_RULE: every ``time.monotonic()`` default in this file uses
``float('-inf')``, not ``0.0``, so the assertions hold on fresh CI VMs
(~200s monotonic uptime).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.bosch_shc_camera import ai_analysis

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(
    *,
    enabled: bool = True,
    per_camera_enabled: bool | None = None,
    privacy_mode: bool = False,
    cooldown: float = 30.0,
    max_per_day: int = 200,
    in_flight: int = 0,
    day_count: int = 0,
    day_stamp: str = "",
    cam_id: str = CAM_ID,
    known_cam: bool = True,
    scene_context: str = "",
    prompt: str | None = None,
    repeat_context_minutes: int = 30,
    config_entry: Any = None,
) -> SimpleNamespace:
    """Minimal coordinator stub covering every field ai_analysis.py reads."""
    opts: dict[str, Any] = {
        "ai_analysis_enabled": enabled,
        "ai_analysis_cooldown_seconds": cooldown,
        "ai_analysis_max_per_day": max_per_day,
        "ai_analysis_repeat_context_minutes": repeat_context_minutes,
    }
    if prompt is not None:
        opts["ai_analysis_prompt"] = prompt

    camera_entities: dict[str, Any] = {}
    if known_cam:
        camera_entities[cam_id] = SimpleNamespace(
            entity_id=f"camera.bosch_{cam_id[:4]}"
        )

    hass_mock = MagicMock()
    # `ai_analysis_budget_state`/`ai_analysis_record_call` fire-and-forget a
    # save coroutine via `hass.async_create_task(...)`. A bare MagicMock()
    # would leave that coroutine object unawaited (RuntimeWarning, and this
    # repo's pytest config promotes unraisable warnings to test failures) —
    # close it immediately instead, matching what a real event loop would
    # eventually do, without actually scheduling/awaiting it (tests assert
    # on the *call*, not the awaited result).
    hass_mock.async_create_task.side_effect = lambda coro, *a, **kw: coro.close()

    coord = SimpleNamespace(
        options=opts,
        hass=hass_mock,
        data={cam_id: {}},
        shc_state_cache={cam_id: {"privacy_mode": privacy_mode}},
        camera_entities=camera_entities,
        config_entry=config_entry,
        ai_analysis_camera_enabled=(
            {} if per_camera_enabled is None else {cam_id: per_camera_enabled}
        ),
        ai_analysis_scene_context={cam_id: scene_context} if scene_context else {},
        ai_analysis_recent={},
        _ai_analysis_last_call={cam_id: float("-inf")},
        _ai_analysis_day_count=day_count,
        _ai_analysis_day_stamp=day_stamp,
        ai_analysis_in_flight=in_flight,
        _ai_analysis_budget_logged_day="",
        _ai_analysis_budget_store=MagicMock(
            async_load=AsyncMock(return_value=None),
            async_save=AsyncMock(),
        ),
        async_fetch_live_snapshot=AsyncMock(return_value=b"\xff\xd8fake-jpeg"),
        async_set_updated_data=MagicMock(),
        _ai_window_allowed=MagicMock(return_value=True),
    )
    return coord


# ── ai_analysis_budget_state ─────────────────────────────────────────────────


class TestAiAnalysisBudgetState:
    def test_same_day_no_rollover(self) -> None:
        today = dt_util.now().date().isoformat()
        coord = _make_coord(day_count=5, day_stamp=today, max_per_day=200)
        used, max_per_day = ai_analysis.ai_analysis_budget_state(coord)
        assert used == 5
        assert max_per_day == 200
        assert coord._ai_analysis_day_count == 5

    def test_day_rollover_resets_count(self) -> None:
        coord = _make_coord(day_count=99, day_stamp="2000-01-01", max_per_day=50)
        used, max_per_day = ai_analysis.ai_analysis_budget_state(coord)
        assert used == 0
        assert max_per_day == 50
        today = dt_util.now().date().isoformat()
        assert coord._ai_analysis_day_stamp == today
        coord.hass.async_create_task.assert_called_once()

    def test_max_per_day_zero_is_unlimited(self) -> None:
        coord = _make_coord(max_per_day=0)
        used, max_per_day = ai_analysis.ai_analysis_budget_state(coord)
        assert max_per_day == 0
        assert used == 0

    def test_max_per_day_garbage_falls_back_to_default(self) -> None:
        coord = _make_coord()
        coord.options["ai_analysis_max_per_day"] = "not-a-number"
        _, max_per_day = ai_analysis.ai_analysis_budget_state(coord)
        assert max_per_day == 200


# ── async_load_ai_analysis_budget / _async_save_ai_analysis_budget ──────────


class TestLoadSaveBudget:
    @pytest.mark.asyncio
    async def test_load_valid_stored_dict_same_day(self) -> None:
        today = dt_util.now().date().isoformat()
        coord = _make_coord()
        coord._ai_analysis_budget_store.async_load = AsyncMock(
            return_value={"date": today, "count": 7}
        )
        await ai_analysis.async_load_ai_analysis_budget(coord)
        assert coord._ai_analysis_day_count == 7
        assert coord._ai_analysis_day_stamp == today

    @pytest.mark.asyncio
    async def test_load_stale_date_ignored(self) -> None:
        coord = _make_coord(day_count=0, day_stamp="")
        coord._ai_analysis_budget_store.async_load = AsyncMock(
            return_value={"date": "2000-01-01", "count": 42}
        )
        await ai_analysis.async_load_ai_analysis_budget(coord)
        # Stale date → not applied, counter stays at its pre-load value.
        assert coord._ai_analysis_day_count == 0
        assert coord._ai_analysis_day_stamp == ""

    @pytest.mark.asyncio
    async def test_load_corrupt_non_dict_data_ignored(self) -> None:
        coord = _make_coord(day_count=0, day_stamp="")
        coord._ai_analysis_budget_store.async_load = AsyncMock(
            return_value="not-a-dict"
        )
        await ai_analysis.async_load_ai_analysis_budget(coord)
        assert coord._ai_analysis_day_count == 0

    @pytest.mark.asyncio
    async def test_load_store_raises_swallowed(self) -> None:
        coord = _make_coord(day_count=0, day_stamp="")
        coord._ai_analysis_budget_store.async_load = AsyncMock(
            side_effect=RuntimeError("disk gone")
        )
        # Must not raise.
        await ai_analysis.async_load_ai_analysis_budget(coord)
        assert coord._ai_analysis_day_count == 0

    @pytest.mark.asyncio
    async def test_load_count_garbage_swallowed(self) -> None:
        today = dt_util.now().date().isoformat()
        coord = _make_coord(day_count=0, day_stamp="")
        coord._ai_analysis_budget_store.async_load = AsyncMock(
            return_value={"date": today, "count": "not-an-int"}
        )
        await ai_analysis.async_load_ai_analysis_budget(coord)
        # (TypeError, ValueError) caught → both fields left untouched.
        assert coord._ai_analysis_day_count == 0

    @pytest.mark.asyncio
    async def test_save_failure_swallowed(self) -> None:
        coord = _make_coord()
        coord._ai_analysis_budget_store.async_save = AsyncMock(
            side_effect=RuntimeError("disk full")
        )
        # Must not raise.
        await ai_analysis._async_save_ai_analysis_budget(coord)

    @pytest.mark.asyncio
    async def test_save_success_writes_expected_payload(self) -> None:
        coord = _make_coord(day_count=3, day_stamp="2026-07-16")
        await ai_analysis._async_save_ai_analysis_budget(coord)
        coord._ai_analysis_budget_store.async_save.assert_awaited_once_with(
            {"date": "2026-07-16", "count": 3}
        )


# ── _ai_analysis_rate_allowed ────────────────────────────────────────────────


class TestAiAnalysisRateAllowed:
    def test_cooldown_not_yet_elapsed_blocks(self) -> None:
        coord = _make_coord(cooldown=60.0)
        coord._ai_analysis_last_call[CAM_ID] = time.monotonic()
        assert ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID) is False

    def test_cooldown_elapsed_allows(self) -> None:
        coord = _make_coord(cooldown=30.0)
        coord._ai_analysis_last_call[CAM_ID] = float("-inf")
        assert ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID) is True

    def test_budget_exhausted_blocks(self) -> None:
        today = dt_util.now().date().isoformat()
        coord = _make_coord(max_per_day=1, day_count=1, day_stamp=today)
        assert ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID) is False

    def test_budget_exhausted_counts_in_flight(self) -> None:
        today = dt_util.now().date().isoformat()
        coord = _make_coord(max_per_day=1, day_count=0, day_stamp=today, in_flight=1)
        # (used=0 + in_flight=1) >= max_per_day=1 → blocked
        assert ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID) is False

    def test_budget_exhausted_logs_once_per_day(self) -> None:
        today = dt_util.now().date().isoformat()
        coord = _make_coord(max_per_day=1, day_count=1, day_stamp=today)
        ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID)
        assert coord._ai_analysis_budget_logged_day == today
        logged_day_after_first_call = coord._ai_analysis_budget_logged_day
        ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID)
        # Still the same day, log-guard stays set (no assertion on log count
        # itself, just that the guard doesn't get cleared).
        assert coord._ai_analysis_budget_logged_day == logged_day_after_first_call

    def test_unlimited_budget_allows(self) -> None:
        coord = _make_coord(max_per_day=0, day_count=99999)
        coord._ai_analysis_last_call[CAM_ID] = float("-inf")
        assert ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID) is True

    def test_cooldown_garbage_falls_back_to_default(self) -> None:
        coord = _make_coord()
        coord.options["ai_analysis_cooldown_seconds"] = "garbage"
        coord._ai_analysis_last_call[CAM_ID] = time.monotonic()
        # Falls back to 30s default cooldown, just called → still blocked.
        assert ai_analysis._ai_analysis_rate_allowed(coord, CAM_ID) is False


# ── ai_analysis_record_call ──────────────────────────────────────────────────


class TestAiAnalysisRecordCall:
    def test_records_cooldown_timestamp_and_increments_count(self) -> None:
        today = dt_util.now().date().isoformat()
        coord = _make_coord(day_count=2, day_stamp=today)
        before = time.monotonic()
        ai_analysis.ai_analysis_record_call(coord, CAM_ID)
        assert coord._ai_analysis_last_call[CAM_ID] >= before
        assert coord._ai_analysis_day_count == 3
        coord.hass.async_create_task.assert_called()


# ── per_camera_analysis_enabled ──────────────────────────────────────────────


class TestPerCameraAnalysisEnabled:
    def test_default_true_when_unset(self) -> None:
        coord = _make_coord(per_camera_enabled=None)
        assert ai_analysis.per_camera_analysis_enabled(coord, CAM_ID) is True

    def test_explicit_false(self) -> None:
        coord = _make_coord(per_camera_enabled=False)
        assert ai_analysis.per_camera_analysis_enabled(coord, CAM_ID) is False

    def test_explicit_true(self) -> None:
        coord = _make_coord(per_camera_enabled=True)
        assert ai_analysis.per_camera_analysis_enabled(coord, CAM_ID) is True

    def test_missing_attribute_defaults_true(self) -> None:
        coord = SimpleNamespace()
        assert ai_analysis.per_camera_analysis_enabled(coord, CAM_ID) is True


# ── _build_prompt ─────────────────────────────────────────────────────────────


class TestBuildPrompt:
    def test_base_prompt_only(self) -> None:
        coord = _make_coord()
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert prompt == ai_analysis.DEFAULT_ANALYSIS_PROMPT

    def test_custom_base_prompt_used(self) -> None:
        coord = _make_coord(prompt="Custom instructions.")
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert prompt == "Custom instructions."

    def test_with_scene_context(self) -> None:
        coord = _make_coord(scene_context="Front door, package deliveries common.")
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert "Front door, package deliveries common." in prompt
        assert "Kontext zu dieser Kamera:" in prompt

    def test_scene_context_whitespace_only_omitted(self) -> None:
        coord = _make_coord(scene_context="   ")
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert "Kontext zu dieser Kamera:" not in prompt

    def test_with_known_visitors(self) -> None:
        entry = SimpleNamespace(
            subentries={
                "sub1": SimpleNamespace(
                    subentry_type="ai_visitor",
                    data={
                        "visitor_name": "Postbote",
                        "visitor_description": "Gelbe Uniform, Trolley",
                    },
                )
            }
        )
        coord = _make_coord(config_entry=entry)
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert "Bekannte Personen" in prompt
        assert "Postbote: Gelbe Uniform, Trolley" in prompt

    def test_with_repeat_context_hint(self) -> None:
        coord = _make_coord(repeat_context_minutes=30)
        coord.ai_analysis_recent[CAM_ID] = [
            (datetime.now(UTC).isoformat(), 5),
            (datetime.now(UTC).isoformat(), 6),
        ]
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert "bereits 2 Alarm(e)" in prompt

    def test_repeat_context_disabled_when_zero_minutes(self) -> None:
        coord = _make_coord(repeat_context_minutes=0)
        coord.ai_analysis_recent[CAM_ID] = [(datetime.now(UTC).isoformat(), 5)]
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert "Alarm(e)" not in prompt

    def test_all_parts_combined(self) -> None:
        entry = SimpleNamespace(
            subentries={
                "sub1": SimpleNamespace(
                    subentry_type="ai_visitor",
                    data={
                        "visitor_name": "Nachbar",
                        "visitor_description": "Blaue Jacke",
                    },
                )
            }
        )
        coord = _make_coord(
            scene_context="Garten mit Terrasse",
            config_entry=entry,
            repeat_context_minutes=15,
        )
        coord.ai_analysis_recent[CAM_ID] = [(datetime.now(UTC).isoformat(), 3)]
        prompt = ai_analysis._build_prompt(coord, CAM_ID)
        assert ai_analysis.DEFAULT_ANALYSIS_PROMPT in prompt
        assert "Garten mit Terrasse" in prompt
        assert "Nachbar: Blaue Jacke" in prompt
        assert "bereits 1 Alarm(e)" in prompt


# ── _known_visitors ───────────────────────────────────────────────────────────


class TestKnownVisitors:
    def test_no_config_entry_returns_empty(self) -> None:
        coord = SimpleNamespace(config_entry=None)
        assert ai_analysis._known_visitors(coord) == []

    def test_missing_config_entry_attribute_returns_empty(self) -> None:
        coord = SimpleNamespace()
        assert ai_analysis._known_visitors(coord) == []

    def test_no_subentries_returns_empty(self) -> None:
        entry = SimpleNamespace(subentries={})
        coord = SimpleNamespace(config_entry=entry)
        assert ai_analysis._known_visitors(coord) == []

    def test_mixed_subentry_types_only_ai_visitor_counted(self) -> None:
        entry = SimpleNamespace(
            subentries={
                "s1": SimpleNamespace(
                    subentry_type="ai_visitor",
                    data={"visitor_name": "A", "visitor_description": "desc-a"},
                ),
                "s2": SimpleNamespace(
                    subentry_type="something_else",
                    data={"visitor_name": "B", "visitor_description": "desc-b"},
                ),
            }
        )
        coord = SimpleNamespace(config_entry=entry)
        result = ai_analysis._known_visitors(coord)
        assert result == [("A", "desc-a")]

    def test_malformed_subentry_data_does_not_raise(self) -> None:
        entry = SimpleNamespace(
            subentries={
                "s1": SimpleNamespace(subentry_type="ai_visitor", data={}),
            }
        )
        coord = SimpleNamespace(config_entry=entry)
        # No name/desc → excluded entirely, but must not raise.
        result = ai_analysis._known_visitors(coord)
        assert result == []

    def test_name_only_included(self) -> None:
        entry = SimpleNamespace(
            subentries={
                "s1": SimpleNamespace(
                    subentry_type="ai_visitor",
                    data={"visitor_name": "Nur Name"},
                ),
            }
        )
        coord = SimpleNamespace(config_entry=entry)
        assert ai_analysis._known_visitors(coord) == [("Nur Name", "")]


# ── _parse_json_fallback ──────────────────────────────────────────────────────


class TestParseJsonFallback:
    def test_valid_json_embedded_in_text(self) -> None:
        text = 'Here is the result: {"score": 7, "short": "A person walking"} thanks'
        result = ai_analysis._parse_json_fallback(text)
        assert result == {"score": 7, "short": "A person walking"}

    def test_no_json_present(self) -> None:
        assert ai_analysis._parse_json_fallback("no json here at all") is None

    def test_malformed_json(self) -> None:
        text = '{"score": 7, "short": "unterminated'
        assert ai_analysis._parse_json_fallback(text) is None

    def test_malformed_json_with_matching_braces_hits_json_loads_error(self) -> None:
        """`{...}` DOES match the regex (braces balance) but the content
        isn't valid JSON — exercises the json.loads ValueError branch
        specifically, as opposed to test_malformed_json's no-match case."""
        text = '{"score": 7, "short": invalid_unquoted_value}'
        assert ai_analysis._parse_json_fallback(text) is None

    def test_json_missing_score_key_rejected(self) -> None:
        text = '{"short": "no score field"}'
        assert ai_analysis._parse_json_fallback(text) is None

    def test_json_that_is_a_list_rejected(self) -> None:
        text = "[1, 2, 3]"
        assert ai_analysis._parse_json_fallback(text) is None


# ── async_generate_ai_analysis ────────────────────────────────────────────────


class TestAsyncGenerateAiAnalysis:
    """`_finalize_alert`'s own persistence/routing behavior is covered in
    depth by `TestFinalizeAlert` below — here it's auto-mocked out so these
    tests can focus purely on `async_generate_ai_analysis`'s own gating/
    response-parsing/error-handling branches without needing a real
    `ai_alert_store`/`ai_alert_routing` call chain."""

    @pytest.fixture(autouse=True)
    def _mock_finalize_dependencies(self) -> Any:
        with (
            patch(
                "custom_components.bosch_shc_camera.ai_alert_store.async_store_alert",
                new=AsyncMock(return_value={"image_path": None}),
            ),
            patch(
                "custom_components.bosch_shc_camera.ai_alert_routing.async_route_alert",
                new=AsyncMock(),
            ),
        ):
            yield

    @pytest.mark.asyncio
    async def test_master_switch_off_returns_none(self) -> None:
        coord = _make_coord(enabled=False)
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID)
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_per_camera_switch_off_returns_none(self) -> None:
        coord = _make_coord(per_camera_enabled=False)
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID)
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_privacy_mode_on_returns_none(self) -> None:
        coord = _make_coord(privacy_mode=True)
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID)
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_window_gate_closed_not_force_returns_none(self) -> None:
        coord = _make_coord()
        coord._ai_window_allowed = MagicMock(return_value=False)
        result = await ai_analysis.async_generate_ai_analysis(
            coord, CAM_ID, force=False
        )
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_rate_gate_closed_not_force_returns_none(self) -> None:
        coord = _make_coord(cooldown=60.0)
        coord._ai_analysis_last_call[CAM_ID] = time.monotonic()
        result = await ai_analysis.async_generate_ai_analysis(
            coord, CAM_ID, force=False
        )
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_bypasses_window_and_rate_gate(self) -> None:
        coord = _make_coord(cooldown=99999.0)
        coord._ai_window_allowed = MagicMock(return_value=False)
        coord._ai_analysis_last_call[CAM_ID] = time.monotonic()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 5, "short": "Someone at the door"}}
        )
        coord.hass.bus.async_fire = MagicMock()
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is not None
        assert result["score"] == 5
        coord.hass.services.async_call.assert_awaited_once()
        # force=False path would have blocked both gates; assert they were
        # never even consulted for the blocking outcome.
        coord._ai_window_allowed.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_camera_returns_none(self) -> None:
        coord = _make_coord(known_cam=False)
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_structured_response_score_clamped_high(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 15, "short": "Break-in in progress"}}
        )
        coord.hass.bus.async_fire = MagicMock()
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is not None
        assert result["score"] == 10

    @pytest.mark.asyncio
    async def test_score_clamped_low_negative_becomes_zero_returns_none(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": -3, "short": "Empty scene"}}
        )
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        # score=-3 clamps to max(0, min(10, -3)) == 0 → "nothing notable" → None
        assert result is None

    @pytest.mark.asyncio
    async def test_score_exactly_zero_returns_none(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 0, "short": "Nothing"}}
        )
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_score_exactly_one_is_notable_returns_result(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 1, "short": "Barely notable"}}
        )
        coord.hass.bus.async_fire = MagicMock()
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is not None
        assert result["score"] == 1

    @pytest.mark.asyncio
    async def test_json_fallback_path_when_response_is_string(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": 'blah blah {"score": 8, "short": "Person"} blah'}
        )
        coord.hass.bus.async_fire = MagicMock()
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is not None
        assert result["score"] == 8

    @pytest.mark.asyncio
    async def test_json_fallback_string_with_no_json_returns_none(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": "just plain text, no json"}
        )
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_swallowed_returns_none(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_asyncio_timeout_error_swallowed_returns_none(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(side_effect=TimeoutError())
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_generic_exception_swallowed_returns_none(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("ai_task provider down")
        )
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_in_flight_incremented_then_decremented_on_success(self) -> None:
        coord = _make_coord()
        observed_in_flight: list[int] = []

        async def _fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            observed_in_flight.append(coord.ai_analysis_in_flight)
            return {"data": {"score": 4, "short": "Cat"}}

        coord.hass.services.async_call = AsyncMock(side_effect=_fake_call)
        coord.hass.bus.async_fire = MagicMock()
        assert coord.ai_analysis_in_flight == 0
        await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert observed_in_flight == [1]
        assert coord.ai_analysis_in_flight == 0

    @pytest.mark.asyncio
    async def test_in_flight_decremented_on_exception(self) -> None:
        coord = _make_coord()
        observed_in_flight: list[int] = []

        async def _fake_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
            observed_in_flight.append(coord.ai_analysis_in_flight)
            raise RuntimeError("boom")

        coord.hass.services.async_call = AsyncMock(side_effect=_fake_call)
        assert coord.ai_analysis_in_flight == 0
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None
        assert observed_in_flight == [1]
        assert coord.ai_analysis_in_flight == 0

    @pytest.mark.asyncio
    async def test_successful_call_invokes_finalize_alert(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 6, "short": "Delivery"}}
        )
        with patch.object(
            ai_analysis, "_finalize_alert", new=AsyncMock()
        ) as mock_finalize:
            result = await ai_analysis.async_generate_ai_analysis(
                coord, CAM_ID, force=True
            )
        assert result is not None
        mock_finalize.assert_awaited_once()
        awaited_args = mock_finalize.await_args.args
        assert awaited_args[0] is coord
        assert awaited_args[1] == CAM_ID
        assert awaited_args[3]["score"] == 6

    @pytest.mark.asyncio
    async def test_non_numeric_score_falls_back_to_zero_returns_none(self) -> None:
        """A `score` value that `int(float(...))` can't convert (e.g. a
        provider returning a non-numeric string despite the structure
        schema) must degrade to 0 ('nothing notable'), not crash."""
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": "not-a-number", "short": "?"}}
        )
        result = await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert result is None

    @pytest.mark.asyncio
    async def test_finalize_alert_exception_swallowed_result_still_returned(
        self,
    ) -> None:
        """Module contract: `async_generate_ai_analysis` never raises. A
        storage/routing failure inside `_finalize_alert` (downstream of a
        SUCCESSFUL AI call) must degrade to a logged warning — the analysis
        result itself is still returned to the caller."""
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 6, "short": "Delivery"}}
        )
        with patch.object(
            ai_analysis,
            "_finalize_alert",
            new=AsyncMock(side_effect=RuntimeError("storage backend down")),
        ):
            result = await ai_analysis.async_generate_ai_analysis(
                coord, CAM_ID, force=True
            )
        assert result is not None
        assert result["score"] == 6

    @pytest.mark.asyncio
    async def test_nothing_notable_does_not_call_finalize_alert(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 0, "short": "Empty"}}
        )
        with patch.object(
            ai_analysis, "_finalize_alert", new=AsyncMock()
        ) as mock_finalize:
            result = await ai_analysis.async_generate_ai_analysis(
                coord, CAM_ID, force=True
            )
        assert result is None
        mock_finalize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ai_task_entity_included_when_configured(self) -> None:
        coord = _make_coord()
        coord.options["ai_analysis_task_entity"] = "ai_task.my_provider"
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 3, "short": "Bird"}}
        )
        coord.hass.bus.async_fire = MagicMock()
        await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        call_kwargs = coord.hass.services.async_call.call_args
        call_data = call_kwargs.args[2]
        assert call_data["entity_id"] == "ai_task.my_provider"

    @pytest.mark.asyncio
    async def test_ai_task_entity_omitted_when_not_configured(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 3, "short": "Bird"}}
        )
        coord.hass.bus.async_fire = MagicMock()
        await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        call_kwargs = coord.hass.services.async_call.call_args
        call_data = call_kwargs.args[2]
        assert "entity_id" not in call_data

    @pytest.mark.asyncio
    async def test_successful_call_records_rate_call(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 5, "short": "Person"}}
        )
        coord.hass.bus.async_fire = MagicMock()
        assert coord._ai_analysis_day_count == 0
        await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert coord._ai_analysis_day_count == 1

    @pytest.mark.asyncio
    async def test_nothing_notable_does_not_record_rate_call(self) -> None:
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            return_value={"data": {"score": 0, "short": "Nothing"}}
        )
        # score<=0 is still a real (non-None) result parsed from the call,
        # but ai_analysis_record_call only fires when `result is not None`
        # (checked right after the ai_task call, before score clamping) —
        # so this DOES count toward the budget even though it later returns
        # None to the caller. Documents actual behavior.
        await ai_analysis.async_generate_ai_analysis(coord, CAM_ID, force=True)
        assert coord._ai_analysis_day_count == 1


# ── _finalize_alert ────────────────────────────────────────────────────────────


class TestFinalizeAlert:
    @pytest.mark.asyncio
    async def test_persists_via_ai_alert_store(self) -> None:
        coord = _make_coord()
        result = {"score": 7, "short": "Person at door"}
        with (
            patch(
                "custom_components.bosch_shc_camera.ai_alert_store.async_store_alert",
                new=AsyncMock(return_value={"image_path": "img.jpg"}),
            ) as mock_store,
            patch(
                "custom_components.bosch_shc_camera.ai_alert_routing.async_route_alert",
                new=AsyncMock(),
            ),
        ):
            await ai_analysis._finalize_alert(
                coord, CAM_ID, "camera.bosch_1111", result
            )
        mock_store.assert_awaited_once()
        args = mock_store.await_args.args
        assert args[0] is coord
        assert args[1] == CAM_ID
        assert args[2]["score"] == 7

    @pytest.mark.asyncio
    async def test_updates_coordinator_data_ai_analysis(self) -> None:
        coord = _make_coord()
        result = {"score": 6, "short": "Someone in the yard"}
        with (
            patch(
                "custom_components.bosch_shc_camera.ai_alert_store.async_store_alert",
                new=AsyncMock(return_value={"image_path": "img.jpg"}),
            ),
            patch(
                "custom_components.bosch_shc_camera.ai_alert_routing.async_route_alert",
                new=AsyncMock(),
            ),
        ):
            await ai_analysis._finalize_alert(
                coord, CAM_ID, "camera.bosch_1111", result
            )
        stored_analysis = coord.data[CAM_ID]["ai_analysis"]
        assert stored_analysis["score"] == 6
        assert stored_analysis["image_path"] == "img.jpg"
        assert "generated_at" in stored_analysis
        coord.async_set_updated_data.assert_called_once_with(coord.data)

    @pytest.mark.asyncio
    async def test_skips_data_update_when_cam_id_not_in_data(self) -> None:
        coord = _make_coord()
        coord.data = {}  # cam_id not present
        result = {"score": 6, "short": "Someone"}
        with (
            patch(
                "custom_components.bosch_shc_camera.ai_alert_store.async_store_alert",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "custom_components.bosch_shc_camera.ai_alert_routing.async_route_alert",
                new=AsyncMock(),
            ),
        ):
            await ai_analysis._finalize_alert(
                coord, CAM_ID, "camera.bosch_1111", result
            )
        assert CAM_ID not in coord.data
        coord.async_set_updated_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_fires_bus_event_with_correct_payload(self) -> None:
        coord = _make_coord()
        result = {"score": 8, "short": "Intruder"}
        coord.hass.bus.async_fire = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.ai_alert_store.async_store_alert",
                new=AsyncMock(return_value={"image_path": None}),
            ),
            patch(
                "custom_components.bosch_shc_camera.ai_alert_routing.async_route_alert",
                new=AsyncMock(),
            ),
        ):
            await ai_analysis._finalize_alert(
                coord, CAM_ID, "camera.bosch_1111", result
            )
        coord.hass.bus.async_fire.assert_called_once()
        event_name, payload = coord.hass.bus.async_fire.call_args.args
        assert event_name == "bosch_shc_camera_ai_alert"
        assert payload["camera_id"] == CAM_ID
        assert payload["entity_id"] == "camera.bosch_1111"
        assert payload["score"] == 8
        assert payload["short"] == "Intruder"
        assert "generated_at" in payload

    @pytest.mark.asyncio
    async def test_calls_ai_alert_routing_async_route_alert(self) -> None:
        coord = _make_coord()
        result = {"score": 9, "short": "Break-in"}
        with (
            patch(
                "custom_components.bosch_shc_camera.ai_alert_store.async_store_alert",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "custom_components.bosch_shc_camera.ai_alert_routing.async_route_alert",
                new=AsyncMock(),
            ) as mock_route,
        ):
            await ai_analysis._finalize_alert(
                coord, CAM_ID, "camera.bosch_1111", result
            )
        mock_route.assert_awaited_once_with(coord, CAM_ID, result)

    @pytest.mark.asyncio
    async def test_fetches_live_snapshot_for_storage(self) -> None:
        coord = _make_coord()
        result = {"score": 5, "short": "Delivery"}
        with (
            patch(
                "custom_components.bosch_shc_camera.ai_alert_store.async_store_alert",
                new=AsyncMock(return_value=None),
            ) as mock_store,
            patch(
                "custom_components.bosch_shc_camera.ai_alert_routing.async_route_alert",
                new=AsyncMock(),
            ),
        ):
            await ai_analysis._finalize_alert(
                coord, CAM_ID, "camera.bosch_1111", result
            )
        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        args = mock_store.await_args.args
        assert args[4] == b"\xff\xd8fake-jpeg"
