"""Regression tests for ai_analysis_runtime.py — AI-description budget/rate/
window gating + the ai_task generation call, extracted out of coordinator.py
(structural cleanup toward Platinum quality_scale).

Tests call the module functions directly with a lightweight stub
(SimpleNamespace) standing in for the coordinator, mirroring
test_quality_prefs.py's / test_rcp_client.py's convention. Where the
original coordinator method called another extracted coordinator method on
`self` (e.g. `async_generate_ai_description` calling
`self._ai_window_allowed()`/`self._ai_rate_allowed()`/`self.ai_record_call()`,
`_ai_rate_allowed` calling `self.ai_budget_state()`, `ai_budget_state` and
`ai_record_call` calling `self._async_save_ai_budget()`), the stub binds an
instance-level callable for that name so a test can both observe the call
and (via a subclass-style override) prove virtual dispatch is preserved —
matching quality_prefs.py's `coord.get_quality = lambda cam_id: ...` pattern.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import ai_analysis_runtime

CAM_A = "cam-a"
MODULE = "custom_components.bosch_shc_camera.ai_analysis_runtime"


def _noop_create_task(coro: object, **_kwargs: object) -> MagicMock:
    """Mimic hass.async_create_task without leaving an unawaited coroutine."""
    if hasattr(coro, "close"):
        coro.close()  # type: ignore[union-attr]
    return MagicMock()


def _make_coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "options": {},
        "data": {},
        "shc_state_cache": {},
        "camera_entities": {},
        "_ai_last_call": {},
        "_ai_day_count": 0,
        "_ai_day_stamp": "",
        "_ai_budget_logged_day": "",
        "ai_in_flight": 0,
        "_ai_budget_store": SimpleNamespace(
            async_load=AsyncMock(return_value=None),
            async_save=AsyncMock(),
        ),
        "hass": SimpleNamespace(
            async_create_task=MagicMock(side_effect=_noop_create_task),
            states=SimpleNamespace(get=MagicMock(return_value=None)),
            services=SimpleNamespace(async_call=AsyncMock(return_value=None)),
            bus=SimpleNamespace(async_fire=MagicMock()),
        ),
    }
    base.update(overrides)
    coord = SimpleNamespace(**base)
    # Delegating-stub bindings — mirrors coordinator.py's real thin wrappers,
    # so a test overriding one of these on the instance is honored by the
    # callee exactly like a real per-instance patch would be.
    coord.ai_budget_state = lambda: ai_analysis_runtime.ai_budget_state(coord)  # type: ignore[attr-defined]
    coord._async_save_ai_budget = lambda: ai_analysis_runtime._async_save_ai_budget(  # type: ignore[attr-defined]
        coord
    )
    coord._ai_window_allowed = lambda: ai_analysis_runtime._ai_window_allowed(coord)  # type: ignore[attr-defined]
    coord._ai_rate_allowed = lambda cam_id: ai_analysis_runtime._ai_rate_allowed(  # type: ignore[attr-defined]
        coord, cam_id
    )
    coord.ai_record_call = lambda cam_id: ai_analysis_runtime.ai_record_call(  # type: ignore[attr-defined]
        coord, cam_id
    )
    return coord


# ── _ai_window_allowed ───────────────────────────────────────────────────────


class TestAiWindowAllowed:
    def test_no_gates_configured_always_allowed(self) -> None:
        coord = _make_coord()
        assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_normal_window_inside(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "08:00", "ai_active_time_end": "22:00"}
        )
        fake_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_normal_window_before_start_blocked(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "08:00", "ai_active_time_end": "22:00"}
        )
        fake_now = datetime(2026, 6, 15, 7, 59, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is False

    def test_normal_window_after_end_blocked(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "08:00", "ai_active_time_end": "22:00"}
        )
        fake_now = datetime(2026, 6, 15, 22, 1, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is False

    def test_normal_window_at_start_boundary_allowed(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "08:00", "ai_active_time_end": "22:00"}
        )
        fake_now = datetime(2026, 6, 15, 8, 0, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_overnight_window_after_start_allowed(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "22:00", "ai_active_time_end": "06:00"}
        )
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_overnight_window_before_end_allowed(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "22:00", "ai_active_time_end": "06:00"}
        )
        fake_now = datetime(2026, 6, 15, 5, 0, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_overnight_window_in_gap_blocked(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "22:00", "ai_active_time_end": "06:00"}
        )
        fake_now = datetime(2026, 6, 15, 14, 0, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is False

    def test_only_start_configured_disables_time_gate(self) -> None:
        """Only one of start/end set → time gate disabled (fail-open), warns."""
        coord = _make_coord(options={"ai_active_time_start": "08:00"})
        with patch(f"{MODULE}._LOGGER") as mock_logger:
            assert ai_analysis_runtime._ai_window_allowed(coord) is True
            mock_logger.warning.assert_called_once()

    def test_only_end_configured_disables_time_gate(self) -> None:
        coord = _make_coord(options={"ai_active_time_end": "22:00"})
        assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_malformed_time_fails_open(self) -> None:
        coord = _make_coord(
            options={"ai_active_time_start": "bogus", "ai_active_time_end": "22:00"}
        )
        # both set → time gate active, but parsing raises → fail-open (True)
        assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_condition_entity_missing_blocks(self) -> None:
        coord = _make_coord(
            options={"ai_active_condition_entity": "input_boolean.armed"},
            hass=SimpleNamespace(
                states=SimpleNamespace(get=MagicMock(return_value=None))
            ),
        )
        assert ai_analysis_runtime._ai_window_allowed(coord) is False

    def test_condition_entity_unavailable_blocks(self) -> None:
        coord = _make_coord(
            options={"ai_active_condition_entity": "input_boolean.armed"},
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=MagicMock(return_value=SimpleNamespace(state="unavailable"))
                )
            ),
        )
        assert ai_analysis_runtime._ai_window_allowed(coord) is False

    def test_condition_entity_unknown_blocks(self) -> None:
        coord = _make_coord(
            options={"ai_active_condition_entity": "input_boolean.armed"},
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=MagicMock(return_value=SimpleNamespace(state="unknown"))
                )
            ),
        )
        assert ai_analysis_runtime._ai_window_allowed(coord) is False

    def test_condition_entity_matching_state_allows(self) -> None:
        coord = _make_coord(
            options={
                "ai_active_condition_entity": "person.thomas",
                "ai_active_condition_state": "not_home",
            },
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=MagicMock(return_value=SimpleNamespace(state="not_home"))
                )
            ),
        )
        assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_condition_entity_mismatched_state_blocks(self) -> None:
        coord = _make_coord(
            options={
                "ai_active_condition_entity": "person.thomas",
                "ai_active_condition_state": "not_home",
            },
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=MagicMock(return_value=SimpleNamespace(state="home"))
                )
            ),
        )
        assert ai_analysis_runtime._ai_window_allowed(coord) is False

    def test_both_gates_must_pass(self) -> None:
        coord = _make_coord(
            options={
                "ai_active_time_start": "08:00",
                "ai_active_time_end": "22:00",
                "ai_active_condition_entity": "person.thomas",
                "ai_active_condition_state": "not_home",
            },
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=MagicMock(return_value=SimpleNamespace(state="not_home"))
                )
            ),
        )
        fake_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is True

    def test_both_gates_time_blocks_even_if_condition_ok(self) -> None:
        coord = _make_coord(
            options={
                "ai_active_time_start": "08:00",
                "ai_active_time_end": "22:00",
                "ai_active_condition_entity": "person.thomas",
                "ai_active_condition_state": "not_home",
            },
            hass=SimpleNamespace(
                states=SimpleNamespace(
                    get=MagicMock(return_value=SimpleNamespace(state="not_home"))
                )
            ),
        )
        fake_now = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
        with patch(f"{MODULE}.dt_util.now", return_value=fake_now):
            assert ai_analysis_runtime._ai_window_allowed(coord) is False


# ── ai_budget_state ───────────────────────────────────────────────────────


class TestAiBudgetState:
    def test_default_max_per_day_is_100(self) -> None:
        coord = _make_coord(_ai_day_stamp="2026-06-18")
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            used, max_per_day = ai_analysis_runtime.ai_budget_state(coord)
        assert (used, max_per_day) == (0, 100)

    def test_non_numeric_max_per_day_defaults_to_100(self) -> None:
        coord = _make_coord(
            options={"ai_max_per_day": "not-a-number"}, _ai_day_stamp="2026-06-18"
        )
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            _used, max_per_day = ai_analysis_runtime.ai_budget_state(coord)
        assert max_per_day == 100

    def test_none_max_per_day_is_unlimited_zero(self) -> None:
        coord = _make_coord(
            options={"ai_max_per_day": None}, _ai_day_stamp="2026-06-18"
        )
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            _used, max_per_day = ai_analysis_runtime.ai_budget_state(coord)
        assert max_per_day == 0

    def test_same_day_no_rollover(self) -> None:
        coord = _make_coord(_ai_day_stamp="2026-06-18", _ai_day_count=5)
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            used, _max = ai_analysis_runtime.ai_budget_state(coord)
        assert used == 5
        coord.hass.async_create_task.assert_not_called()

    def test_new_day_rolls_over_and_saves(self) -> None:
        coord = _make_coord(_ai_day_stamp="2026-06-17", _ai_day_count=42)
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            used, _max = ai_analysis_runtime.ai_budget_state(coord)
        assert used == 0
        assert coord._ai_day_stamp == "2026-06-18"
        coord.hass.async_create_task.assert_called_once()

    def test_rollover_calls_save_via_coordinator_instance(self) -> None:
        """Virtual-dispatch guard: ai_budget_state's rollover must call
        `coordinator._async_save_ai_budget()`, not the raw module function
        directly — an instance-level override must be honored."""
        coord = _make_coord(_ai_day_stamp="2026-06-17", _ai_day_count=1)
        coord._async_save_ai_budget = MagicMock(return_value="SENTINEL_COROUTINE")
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            ai_analysis_runtime.ai_budget_state(coord)
        coord._async_save_ai_budget.assert_called_once_with()
        coord.hass.async_create_task.assert_called_once_with("SENTINEL_COROUTINE")


# ── async_load_ai_budget ─────────────────────────────────────────────────


class TestAsyncLoadAiBudget:
    @pytest.mark.asyncio
    async def test_store_load_exception_leaves_defaults(self) -> None:
        coord = _make_coord(
            _ai_budget_store=SimpleNamespace(
                async_load=AsyncMock(side_effect=RuntimeError("boom"))
            )
        )
        await ai_analysis_runtime.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0
        assert coord._ai_day_stamp == ""

    @pytest.mark.asyncio
    async def test_non_dict_stored_value_is_ignored(self) -> None:
        coord = _make_coord(
            _ai_budget_store=SimpleNamespace(async_load=AsyncMock(return_value="oops"))
        )
        await ai_analysis_runtime.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0

    @pytest.mark.asyncio
    async def test_stale_stored_date_leaves_counter_at_zero(self) -> None:
        coord = _make_coord(
            _ai_budget_store=SimpleNamespace(
                async_load=AsyncMock(return_value={"date": "2020-01-01", "count": 99})
            )
        )
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            await ai_analysis_runtime.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0
        assert coord._ai_day_stamp == ""

    @pytest.mark.asyncio
    async def test_matching_date_loads_count(self) -> None:
        coord = _make_coord(
            _ai_budget_store=SimpleNamespace(
                async_load=AsyncMock(return_value={"date": "2026-06-18", "count": 7})
            )
        )
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            await ai_analysis_runtime.async_load_ai_budget(coord)
        assert coord._ai_day_count == 7
        assert coord._ai_day_stamp == "2026-06-18"

    @pytest.mark.asyncio
    async def test_non_int_count_is_ignored(self) -> None:
        coord = _make_coord(
            _ai_budget_store=SimpleNamespace(
                async_load=AsyncMock(
                    return_value={"date": "2026-06-18", "count": "bad"}
                )
            )
        )
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            await ai_analysis_runtime.async_load_ai_budget(coord)
        assert coord._ai_day_count == 0
        assert coord._ai_day_stamp == ""


# ── _async_save_ai_budget ────────────────────────────────────────────────


class TestAsyncSaveAiBudget:
    @pytest.mark.asyncio
    async def test_saves_current_stamp_and_count(self) -> None:
        coord = _make_coord(_ai_day_stamp="2026-06-18", _ai_day_count=3)
        await ai_analysis_runtime._async_save_ai_budget(coord)
        coord._ai_budget_store.async_save.assert_awaited_once_with(
            {"date": "2026-06-18", "count": 3}
        )

    @pytest.mark.asyncio
    async def test_save_exception_is_swallowed(self) -> None:
        coord = _make_coord(
            _ai_budget_store=SimpleNamespace(
                async_save=AsyncMock(side_effect=RuntimeError("disk full"))
            )
        )
        # Must not raise — persistence failures never break the calling path.
        await ai_analysis_runtime._async_save_ai_budget(coord)


# ── _ai_rate_allowed ──────────────────────────────────────────────────────


class TestAiRateAllowed:
    def test_no_prior_call_is_allowed(self) -> None:
        coord = _make_coord(options={"ai_cooldown_seconds": 60})
        assert ai_analysis_runtime._ai_rate_allowed(coord, CAM_A) is True

    def test_recent_call_within_cooldown_blocked(self) -> None:
        coord = _make_coord(
            options={"ai_cooldown_seconds": 60},
            _ai_last_call={CAM_A: time.monotonic()},
        )
        assert ai_analysis_runtime._ai_rate_allowed(coord, CAM_A) is False

    def test_call_past_cooldown_allowed(self) -> None:
        coord = _make_coord(
            options={"ai_cooldown_seconds": 60},
            _ai_last_call={CAM_A: time.monotonic() - 61},
        )
        assert ai_analysis_runtime._ai_rate_allowed(coord, CAM_A) is True

    def test_non_numeric_cooldown_defaults_to_60(self) -> None:
        coord = _make_coord(
            options={"ai_cooldown_seconds": "bad"},
            _ai_last_call={CAM_A: time.monotonic() - 1},
        )
        assert ai_analysis_runtime._ai_rate_allowed(coord, CAM_A) is False

    def test_none_cooldown_defaults_to_60(self) -> None:
        coord = _make_coord(
            options={"ai_cooldown_seconds": None},
            _ai_last_call={CAM_A: time.monotonic()},
        )
        # "or 0" → 0.0 cooldown → always allowed regardless of last call
        assert ai_analysis_runtime._ai_rate_allowed(coord, CAM_A) is True

    def test_budget_exhausted_blocks_and_logs_once(self) -> None:
        coord = _make_coord(
            options={"ai_max_per_day": 1},
            _ai_day_count=1,
            _ai_day_stamp="2026-06-18",
        )
        with (
            patch(f"{MODULE}.dt_util") as mock_dt,
            patch(f"{MODULE}._LOGGER") as mock_logger,
        ):
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            result = ai_analysis_runtime._ai_rate_allowed(coord, CAM_A)
        assert result is False
        mock_logger.info.assert_called_once()
        assert coord._ai_budget_logged_day == "2026-06-18"

    def test_budget_exhausted_does_not_log_twice_same_day(self) -> None:
        coord = _make_coord(
            options={"ai_max_per_day": 1},
            _ai_day_count=1,
            _ai_day_stamp="2026-06-18",
            _ai_budget_logged_day="2026-06-18",
        )
        with (
            patch(f"{MODULE}.dt_util") as mock_dt,
            patch(f"{MODULE}._LOGGER") as mock_logger,
        ):
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            ai_analysis_runtime._ai_rate_allowed(coord, CAM_A)
        mock_logger.info.assert_not_called()

    def test_in_flight_counts_toward_budget(self) -> None:
        """used=0 but 1 already in-flight against max_per_day=1 → still blocked."""
        coord = _make_coord(
            options={"ai_max_per_day": 1},
            _ai_day_count=0,
            _ai_day_stamp="2026-06-18",
            ai_in_flight=1,
        )
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            assert ai_analysis_runtime._ai_rate_allowed(coord, CAM_A) is False

    def test_zero_max_per_day_is_unlimited(self) -> None:
        coord = _make_coord(
            options={"ai_max_per_day": 0},
            _ai_day_count=99999,
            _ai_day_stamp="2026-06-18",
        )
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            assert ai_analysis_runtime._ai_rate_allowed(coord, CAM_A) is True

    def test_calls_budget_state_via_coordinator_instance(self) -> None:
        """Virtual-dispatch guard: _ai_rate_allowed must call
        `coordinator.ai_budget_state()`, not the raw module function
        directly — an instance-level override must be honored."""
        coord = _make_coord(options={"ai_cooldown_seconds": 60})
        coord.ai_budget_state = MagicMock(return_value=(0, 100))
        ai_analysis_runtime._ai_rate_allowed(coord, CAM_A)
        coord.ai_budget_state.assert_called_once_with()


# ── ai_record_call ────────────────────────────────────────────────────────


class TestAiRecordCall:
    def test_records_last_call_time_and_increments_count(self) -> None:
        coord = _make_coord(_ai_day_stamp="2026-06-18")
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            ai_analysis_runtime.ai_record_call(coord, CAM_A)
        assert CAM_A in coord._ai_last_call
        assert coord._ai_day_count == 1
        coord.hass.async_create_task.assert_called_once()

    def test_calls_budget_state_via_coordinator_instance_for_rollover(self) -> None:
        """Virtual-dispatch guard: ai_record_call must call
        `coordinator.ai_budget_state()` (to run the day-rollover first), not
        the raw module function directly."""
        coord = _make_coord()
        coord.ai_budget_state = MagicMock(return_value=(0, 100))
        ai_analysis_runtime.ai_record_call(coord, CAM_A)
        coord.ai_budget_state.assert_called_once_with()

    def test_calls_save_via_coordinator_instance(self) -> None:
        """Virtual-dispatch guard: ai_record_call must call
        `coordinator._async_save_ai_budget()`, not the raw module function
        directly."""
        coord = _make_coord(_ai_day_stamp="2026-06-18")
        coord._async_save_ai_budget = MagicMock(return_value="SENTINEL")
        with patch(f"{MODULE}.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            ai_analysis_runtime.ai_record_call(coord, CAM_A)
        coord._async_save_ai_budget.assert_called_once_with()
        coord.hass.async_create_task.assert_called_once_with("SENTINEL")


# ── async_generate_ai_description ────────────────────────────────────────


def _full_coord(**overrides: object) -> SimpleNamespace:
    """Coordinator stub wired far enough to reach the ai_task service call."""
    fake_cam_entity = SimpleNamespace(entity_id="camera.bosch_terrasse")
    base: dict[str, object] = {
        "options": {"enable_ai_description": True, "ai_cooldown_seconds": 60},
        "data": {CAM_A: {}},
        "shc_state_cache": {},
        "camera_entities": {CAM_A: fake_cam_entity},
    }
    base.update(overrides)
    coord = _make_coord(**base)
    coord._ai_window_allowed = MagicMock(return_value=True)  # type: ignore[attr-defined]
    coord._ai_rate_allowed = MagicMock(return_value=True)  # type: ignore[attr-defined]
    coord.ai_record_call = MagicMock()  # type: ignore[attr-defined]
    coord.async_set_updated_data = MagicMock()  # type: ignore[attr-defined]
    return coord


class TestAsyncGenerateAiDescription:
    @pytest.mark.asyncio
    async def test_disabled_option_returns_none(self) -> None:
        coord = _full_coord(options={"enable_ai_description": False})
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_privacy_mode_returns_none(self) -> None:
        coord = _full_coord(shc_state_cache={CAM_A: {"privacy_mode": True}})
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_window_blocked_and_not_forced_returns_none(self) -> None:
        coord = _full_coord()
        coord._ai_window_allowed = MagicMock(return_value=False)
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_window_blocked_is_bypassed_when_forced(self) -> None:
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(
                    async_call=AsyncMock(return_value={"data": "desc"})
                ),
                bus=SimpleNamespace(async_fire=MagicMock()),
            )
        )
        coord._ai_window_allowed = MagicMock(return_value=False)
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result == "desc"

    @pytest.mark.asyncio
    async def test_rate_blocked_no_cache_returns_none(self) -> None:
        coord = _full_coord()
        coord._ai_rate_allowed = MagicMock(return_value=False)
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_rate_blocked_fresh_cache_returns_cached_text(self) -> None:
        gen_at = datetime.now(UTC).isoformat()
        coord = _full_coord(
            data={
                CAM_A: {"ai_description": {"text": "cached!", "generated_at": gen_at}}
            },
        )
        coord._ai_rate_allowed = MagicMock(return_value=False)
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        assert result == "cached!"

    @pytest.mark.asyncio
    async def test_rate_blocked_stale_cache_returns_none(self) -> None:
        old_gen_at = "2000-01-01T00:00:00+00:00"
        coord = _full_coord(
            data={
                CAM_A: {"ai_description": {"text": "old!", "generated_at": old_gen_at}}
            },
        )
        coord._ai_rate_allowed = MagicMock(return_value=False)
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_rate_blocked_cache_missing_generated_at_returns_none(self) -> None:
        coord = _full_coord(
            data={CAM_A: {"ai_description": {"text": "no ts"}}},
        )
        coord._ai_rate_allowed = MagicMock(return_value=False)
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_rate_blocked_cache_malformed_generated_at_returns_none(
        self,
    ) -> None:
        coord = _full_coord(
            data={
                CAM_A: {
                    "ai_description": {"text": "bad ts", "generated_at": "not-a-date"}
                }
            },
        )
        coord._ai_rate_allowed = MagicMock(return_value=False)
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_rate_blocked_cache_during_current_privacy_returns_none(
        self,
    ) -> None:
        gen_at = datetime.now(UTC).isoformat()
        coord = _full_coord(
            data={
                CAM_A: {"ai_description": {"text": "cached!", "generated_at": gen_at}}
            },
            shc_state_cache={CAM_A: {"privacy_mode": True}},
        )
        # privacy_mode check happens BEFORE this, so this actually exits earlier —
        # kept as its own test to pin that behavior explicitly.
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_camera_returns_none(self) -> None:
        coord = _full_coord(camera_entities={})
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_camera_entity_none_value_returns_none(self) -> None:
        coord = _full_coord(camera_entities={CAM_A: None})
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_ai_task_entity_included_when_configured(self) -> None:
        svc_call = AsyncMock(return_value={"data": "desc text"})
        coord = _full_coord(
            options={
                "enable_ai_description": True,
                "ai_cooldown_seconds": 60,
                "ai_task_entity": "ai_task.my_llm",
            },
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(async_call=svc_call),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result == "desc text"
        call_data = svc_call.call_args[0][2]
        assert call_data["entity_id"] == "ai_task.my_llm"

    @pytest.mark.asyncio
    async def test_no_ai_task_entity_key_omitted(self) -> None:
        svc_call = AsyncMock(return_value={"data": "desc text"})
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(async_call=svc_call),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        call_data = svc_call.call_args[0][2]
        assert "entity_id" not in call_data

    @pytest.mark.asyncio
    async def test_custom_prompt_and_language_used(self) -> None:
        svc_call = AsyncMock(return_value={"data": "desc"})
        coord = _full_coord(
            options={
                "enable_ai_description": True,
                "ai_cooldown_seconds": 60,
                "ai_describe_prompt": "Custom prompt.",
                "ai_describe_language": "English",
            },
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(async_call=svc_call),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        call_data = svc_call.call_args[0][2]
        assert "Custom prompt." in call_data["instructions"]
        assert "English" in call_data["instructions"]

    @pytest.mark.asyncio
    async def test_timeout_returns_none_and_resets_in_flight(self) -> None:
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(
                    async_call=AsyncMock(side_effect=TimeoutError())
                ),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None
        assert coord.ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_generic_exception_returns_none_and_resets_in_flight(self) -> None:
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(
                    async_call=AsyncMock(side_effect=RuntimeError("boom"))
                ),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None
        assert coord.ai_in_flight == 0

    @pytest.mark.asyncio
    async def test_none_response_returns_none(self) -> None:
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(async_call=AsyncMock(return_value=None)),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_data_field_returns_none(self) -> None:
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(
                    async_call=AsyncMock(return_value={"data": ""})
                ),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_non_dict_response_is_stringified(self) -> None:
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(
                    async_call=AsyncMock(return_value="a plain string result")
                ),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result == "a plain string result"

    @pytest.mark.asyncio
    async def test_success_updates_data_and_fires_event(self) -> None:
        svc_call = AsyncMock(return_value={"data": "It's a delivery person."})
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(async_call=svc_call),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result == "It's a delivery person."
        assert coord.data[CAM_A]["ai_description"]["text"] == result
        coord.async_set_updated_data.assert_called_once_with(coord.data)
        coord.hass.bus.async_fire.assert_called_once()
        fired_event, fired_payload = coord.hass.bus.async_fire.call_args[0]
        assert fired_event == "bosch_shc_camera_ai_description"
        assert fired_payload["camera_id"] == CAM_A
        assert fired_payload["description"] == result

    @pytest.mark.asyncio
    async def test_success_records_call_via_coordinator_instance(self) -> None:
        """Virtual-dispatch guard: a successful generation must call
        `coordinator.ai_record_call(cam_id)`, not the raw module function
        directly — an instance-level override must be honored."""
        svc_call = AsyncMock(return_value={"data": "desc"})
        coord = _full_coord(
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(async_call=svc_call),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        coord.ai_record_call.assert_called_once_with(CAM_A)

    @pytest.mark.asyncio
    async def test_success_does_not_touch_data_when_cam_id_absent(self) -> None:
        """cam_id not (yet) in coordinator.data → event still fires, no KeyError."""
        svc_call = AsyncMock(return_value={"data": "desc"})
        coord = _full_coord(
            data={},
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
                states=SimpleNamespace(get=MagicMock(return_value=None)),
                services=SimpleNamespace(async_call=svc_call),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result == "desc"
        assert coord.data == {}
        coord.async_set_updated_data.assert_not_called()
        coord.hass.bus.async_fire.assert_called_once()

    @pytest.mark.asyncio
    async def test_calls_window_and_rate_gates_via_coordinator_instance(self) -> None:
        """Virtual-dispatch guard: async_generate_ai_description must call
        `coordinator._ai_window_allowed()`/`coordinator._ai_rate_allowed()`,
        not the raw module functions directly."""
        coord = _full_coord()
        result = await ai_analysis_runtime.async_generate_ai_description(
            coord, CAM_A, force=False
        )
        coord._ai_window_allowed.assert_called_once_with()
        coord._ai_rate_allowed.assert_called_once_with(CAM_A)
        # Both stubbed True/True with a working service call → succeeds.
        assert result is None  # services.async_call default returns None → no text
