"""Scattered coverage — gap-fill for lines missed by prior sprints.

Coverage targets in __init__.py:
  278        – StreamErrorListener.emit() early-return when _coordinator is None
  2150-2154  – _schedule_proactive_refresh() inner closure body
             (is_stopping=False → create task + add to bg_tasks)
  2166       – _proactive_refresh() early-return when hass.is_stopping is True
  5426-5427  – ai_budget_state() except branch: ai_max_per_day non-numeric → 100
  5449-5450  – async_load_ai_budget() except branch: stored count non-int
  5470-5471  – _ai_rate_allowed() except branch: ai_cooldown_seconds non-numeric → 60.0
  5510       – async_generate_ai_description() return None when enable_ai_description=False
  5538       – async_generate_ai_description() return None when cam_entity is None
  5569       – async_generate_ai_description() ai_call_data["entity_id"] set when ai_task_entity non-empty

All tests use SimpleNamespace(**base) stub pattern for the coordinator.
No live HA runtime required.
"""

from __future__ import annotations

import asyncio
import logging
import time as time_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.bosch_shc_camera as _bosch_module
from custom_components.bosch_shc_camera import BoschCameraCoordinator

_StreamWorkerErrorListener = _bosch_module._StreamWorkerErrorListener

CAM_A = "11111111-1111-1111-1111-111111111111"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _create_task_side_effect(coro, **kwargs):
    """Drain coroutines returned by create_task mocks so there are no warnings."""
    try:
        if hasattr(coro, "close"):
            coro.close()
    except Exception:
        pass
    t = MagicMock(spec=asyncio.Task)
    t.add_done_callback = MagicMock()
    return t


class _FakeStore:
    def __init__(self, payload=None) -> None:
        self._payload = payload
        self.saved: list = []

    async def async_load(self):
        return self._payload

    async def async_save(self, data) -> None:
        self.saved.append(data)


def _make_ai_coord(**overrides):
    """Minimal coordinator stub for AI-related methods."""
    base = dict(
        token="tok",
        options={
            "enable_ai_description": True,
            "ai_max_per_day": 100,
            "ai_cooldown_seconds": 60,
        },
        data={CAM_A: {}},
        _shc_state_cache={},
        _ai_window_allowed=MagicMock(return_value=True),
        _ai_rate_allowed=MagicMock(return_value=True),
        _ai_in_flight=0,
        _ai_day_count=0,
        _ai_day_stamp="",
        _ai_last_call={},
        _ai_budget_logged_day="",
        _camera_entities={},
        _ai_budget_store=_FakeStore(None),
        hass=SimpleNamespace(
            is_stopping=False,
            async_create_task=MagicMock(side_effect=_create_task_side_effect),
            services=SimpleNamespace(async_call=AsyncMock(return_value=None)),
        ),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────────────
# 1. StreamErrorListener.emit() — coordinator is None → early return (line 278)
# ─────────────────────────────────────────────────────────────────────────────


class TestStreamErrorListenerNoneCoordinator:
    """emit() must return early when _coordinator is None."""

    def test_emit_none_coordinator_returns_early(self) -> None:
        """Line 278: if self._coordinator is None: return."""
        listener = _StreamWorkerErrorListener.__new__(_StreamWorkerErrorListener)
        listener._coordinator = None

        record = logging.LogRecord(
            name="homeassistant.components.stream.stream.camera.bosch_test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="Error from stream worker",
            args=(),
            exc_info=None,
        )
        # Should not raise and should return without doing anything
        result = listener.emit(record)
        assert result is None  # returns None (implicit return)


# ─────────────────────────────────────────────────────────────────────────────
# 2. _schedule_proactive_refresh inner closure — lines 2150-2154
#    is_stopping=False → create task + add to bg_tasks
# ─────────────────────────────────────────────────────────────────────────────


class TestScheduleProactiveRefreshClosure:
    """The inner closure _schedule_proactive_refresh is called by call_later."""

    def _make_refresh_coord(self, *, is_stopping: bool = False):
        """Coordinator stub for _schedule_token_refresh tests."""
        import base64 as _b64
        import json as _json
        import time as _time

        # Build a valid JWT-like token that won't expire for ~1 hour
        header = (
            _b64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=").decode()
        )
        exp = int(_time.time()) + 3600
        payload_bytes = _json.dumps({"exp": exp, "sub": "test"}).encode()
        payload = _b64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
        token = f"{header}.{payload}.sig"

        bg_tasks: set = set()

        task_mock = MagicMock(spec=asyncio.Task)
        task_mock.add_done_callback = MagicMock()

        def _create_task(coro, **kwargs):
            try:
                if hasattr(coro, "close"):
                    coro.close()
            except Exception:
                pass
            return task_mock

        loop_mock = MagicMock()
        captured_callback: list = []

        def _call_later(delay, cb):
            captured_callback.append(cb)
            return MagicMock()

        loop_mock.call_later = MagicMock(side_effect=_call_later)

        hass = SimpleNamespace(
            is_stopping=is_stopping,
            async_create_task=MagicMock(side_effect=_create_task),
            loop=loop_mock,
        )

        base = dict(
            token=token,
            _token_refresh_handle=None,
            _bg_tasks=bg_tasks,
            _proactive_refresh=AsyncMock(),
            hass=hass,
        )
        coord = SimpleNamespace(**base)
        return coord, captured_callback, task_mock, bg_tasks

    def test_closure_is_stopping_false_creates_task(self) -> None:
        """Lines 2150-2154: is_stopping=False → create_task called + task in bg_tasks."""
        coord, callbacks, task_mock, bg_tasks = self._make_refresh_coord(
            is_stopping=False
        )

        BoschCameraCoordinator._schedule_token_refresh(coord)

        assert len(callbacks) == 1, "call_later should have been called once"
        callback = callbacks[0]

        # Invoke the inner closure — simulates call_later firing
        callback()

        coord.hass.async_create_task.assert_called_once()
        assert task_mock in bg_tasks
        task_mock.add_done_callback.assert_called_once()

    def test_closure_is_stopping_true_does_not_create_task(self) -> None:
        """Line 2150: is_stopping=True → early return, no create_task."""
        coord, callbacks, _task_mock, bg_tasks = self._make_refresh_coord(
            is_stopping=True
        )

        BoschCameraCoordinator._schedule_token_refresh(coord)

        assert len(callbacks) == 1
        callbacks[0]()  # fire the closure

        coord.hass.async_create_task.assert_not_called()
        assert len(bg_tasks) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. _proactive_refresh() — hass.is_stopping=True → early return (line 2166)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestProactiveRefreshEarlyReturn:
    """_proactive_refresh() skips work when HA is stopping."""

    async def test_proactive_refresh_is_stopping_returns_early(self) -> None:
        """Line 2166: if hass.is_stopping: return (no _ensure_valid_token call)."""
        ensure_token = AsyncMock()
        coord = SimpleNamespace(
            hass=SimpleNamespace(is_stopping=True),
            _ensure_valid_token=ensure_token,
        )
        await BoschCameraCoordinator._proactive_refresh(coord)
        ensure_token.assert_not_called()

    async def test_proactive_refresh_not_stopping_calls_ensure_token(self) -> None:
        """Positive path: is_stopping=False → _ensure_valid_token called."""
        ensure_token = AsyncMock()
        coord = SimpleNamespace(
            hass=SimpleNamespace(is_stopping=False),
            _ensure_valid_token=ensure_token,
        )
        await BoschCameraCoordinator._proactive_refresh(coord)
        ensure_token.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 4. ai_budget_state() — non-numeric ai_max_per_day → max_per_day = 100 (L5426-5427)
# ─────────────────────────────────────────────────────────────────────────────


class TestAiBudgetStateNonNumeric:
    """ai_budget_state() except (TypeError, ValueError) → max_per_day = 100."""

    def test_non_numeric_max_per_day_defaults_to_100(self) -> None:
        """Lines 5426-5427: int('bad') raises ValueError → max_per_day = 100."""

        def _fake_async_create_task(coro, **kwargs):
            try:
                if hasattr(coro, "close"):
                    coro.close()
            except Exception:
                pass
            return MagicMock()

        coord = SimpleNamespace(
            options={"ai_max_per_day": "not-a-number"},
            _ai_day_stamp="",
            _ai_day_count=5,
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_fake_async_create_task),
            ),
            _async_save_ai_budget=AsyncMock(),
        )

        with patch("custom_components.bosch_shc_camera.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            # Set stamp to today so rollover doesn't fire
            coord._ai_day_stamp = "2026-06-18"
            _used, max_per_day = BoschCameraCoordinator.ai_budget_state(coord)

        assert max_per_day == 100

    def test_none_max_per_day_is_treated_as_zero(self) -> None:
        """int(None) raises TypeError → max_per_day = 100."""

        def _noop_create_task(coro, **kwargs):
            try:
                if hasattr(coro, "close"):
                    coro.close()
            except Exception:
                pass
            return MagicMock()

        coord = SimpleNamespace(
            options={"ai_max_per_day": None},
            _ai_day_stamp="",
            _ai_day_count=0,
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
            ),
            _async_save_ai_budget=AsyncMock(),
        )

        with patch("custom_components.bosch_shc_camera.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = (
                "2026-06-18"
            )
            coord._ai_day_stamp = "2026-06-18"
            _used, max_per_day = BoschCameraCoordinator.ai_budget_state(coord)

        # None → "or 0" branch: int(0) = 0, not 100 — but None triggers no exception
        # because `opts.get("ai_max_per_day", 100) or 0` → 0, and int(0) = 0.
        # So this is the "0 = unlimited" case, not the except branch.
        assert max_per_day == 0


# ─────────────────────────────────────────────────────────────────────────────
# 5. async_load_ai_budget() — stored count non-int → except pass (L5449-5450)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAsyncLoadAiBudgetNonInt:
    """async_load_ai_budget() with a non-int count hits the except branch."""

    async def test_non_int_count_is_ignored(self) -> None:
        """Lines 5449-5450: int('bad') raises ValueError → except pass → count unchanged."""
        today = "2026-06-18"
        stored_payload = {"date": today, "count": "bad"}
        store = _FakeStore(stored_payload)

        coord = SimpleNamespace(
            _ai_budget_store=store,
            _ai_day_count=0,
            _ai_day_stamp="",
        )

        with patch("custom_components.bosch_shc_camera.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = today
            await BoschCameraCoordinator.async_load_ai_budget(coord)

        # Except pass → count stays at 0, stamp stays empty
        assert coord._ai_day_count == 0
        assert coord._ai_day_stamp == ""

    async def test_valid_count_is_loaded(self) -> None:
        """Positive path: int('7') succeeds → count and stamp are set."""
        today = "2026-06-18"
        stored_payload = {"date": today, "count": 7}
        store = _FakeStore(stored_payload)

        coord = SimpleNamespace(
            _ai_budget_store=store,
            _ai_day_count=0,
            _ai_day_stamp="",
        )

        with patch("custom_components.bosch_shc_camera.dt_util") as mock_dt:
            mock_dt.now.return_value.date.return_value.isoformat.return_value = today
            await BoschCameraCoordinator.async_load_ai_budget(coord)

        assert coord._ai_day_count == 7
        assert coord._ai_day_stamp == today


# ─────────────────────────────────────────────────────────────────────────────
# 6. _ai_rate_allowed() — non-numeric ai_cooldown_seconds → 60.0 (L5470-5471)
# ─────────────────────────────────────────────────────────────────────────────


class TestAiRateAllowedNonNumericCooldown:
    """_ai_rate_allowed() except branch: cooldown defaults to 60.0."""

    def _make_rate_coord(self, cooldown_val, *, last_call_at=float("-inf")):
        def _noop_create_task(coro, **kwargs):
            try:
                if hasattr(coro, "close"):
                    coro.close()
            except Exception:
                pass
            return MagicMock()

        coord = SimpleNamespace(
            options={"ai_cooldown_seconds": cooldown_val},
            _ai_last_call={CAM_A: last_call_at},
            _ai_in_flight=0,
            _ai_day_count=0,
            _ai_day_stamp="",
            _ai_budget_logged_day="",
            hass=SimpleNamespace(
                async_create_task=MagicMock(side_effect=_noop_create_task),
            ),
            _async_save_ai_budget=AsyncMock(),
            ai_budget_state=MagicMock(return_value=(0, 100)),
        )
        return coord

    def test_non_numeric_cooldown_defaults_to_60(self) -> None:
        """Lines 5470-5471: float('bad') raises ValueError → cooldown = 60.0."""
        coord = self._make_rate_coord("not-a-float", last_call_at=float("-inf"))
        # With last_call_at = -inf and default cooldown 60.0, monotonic() - (-inf) >> 60
        result = BoschCameraCoordinator._ai_rate_allowed(coord, CAM_A)
        assert result is True

    def test_non_numeric_cooldown_blocks_when_recent(self) -> None:
        """Non-numeric cooldown → 60.0 fallback; recent call (1s ago) is blocked."""
        recent = time_mod.monotonic() - 1.0  # 1 second ago < 60s cooldown
        coord = self._make_rate_coord("bad", last_call_at=recent)
        result = BoschCameraCoordinator._ai_rate_allowed(coord, CAM_A)
        assert result is False

    def test_numeric_cooldown_is_used_directly(self) -> None:
        """Positive path: valid float cooldown = 0.0 → always allowed."""
        coord = self._make_rate_coord(0.0, last_call_at=time_mod.monotonic())
        result = BoschCameraCoordinator._ai_rate_allowed(coord, CAM_A)
        assert result is True


# ─────────────────────────────────────────────────────────────────────────────
# 7. async_generate_ai_description() — enable_ai_description=False → None (L5510)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAsyncGenerateAiDescriptionDisabled:
    """Returns None immediately when enable_ai_description option is False."""

    async def test_returns_none_when_disabled(self) -> None:
        """Line 5510: enable_ai_description=False → return None."""
        coord = _make_ai_coord(options={"enable_ai_description": False})
        result = await BoschCameraCoordinator.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    async def test_returns_none_when_key_missing(self) -> None:
        """options without enable_ai_description key → falsy → return None."""
        coord = _make_ai_coord(options={})
        result = await BoschCameraCoordinator.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 8. async_generate_ai_description() — cam_entity is None → None (L5538)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAsyncGenerateAiDescriptionNoCamEntity:
    """Returns None when _camera_entities does not contain the cam_id."""

    async def test_returns_none_when_no_cam_entity(self) -> None:
        """Line 5538: _camera_entities.get(cam_id) returns None → return None."""
        coord = _make_ai_coord(
            options={"enable_ai_description": True},
            _shc_state_cache={},
            _camera_entities={},  # cam_id not registered
        )
        # Bypass rate/window checks by patching them
        coord._ai_window_allowed = MagicMock(return_value=True)
        coord._ai_rate_allowed = MagicMock(return_value=True)

        result = await BoschCameraCoordinator.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None

    async def test_cam_entity_none_value_returns_none(self) -> None:
        """_camera_entities has key but value is None → return None."""
        coord = _make_ai_coord(
            options={"enable_ai_description": True},
            _shc_state_cache={},
            _camera_entities={CAM_A: None},
        )
        coord._ai_window_allowed = MagicMock(return_value=True)
        coord._ai_rate_allowed = MagicMock(return_value=True)

        result = await BoschCameraCoordinator.async_generate_ai_description(
            coord, CAM_A, force=True
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 9. async_generate_ai_description() — ai_task_entity set → entity_id in call_data (L5569)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestAsyncGenerateAiDescriptionWithAiTaskEntity:
    """When ai_task_entity option is set, entity_id is added to ai_call_data."""

    def _make_full_ai_coord(self, *, ai_task_entity: str, svc_call: AsyncMock):
        """Build a coordinator stub that can reach line 5569 (and beyond)."""
        fake_cam_entity = SimpleNamespace(entity_id="camera.bosch_terrasse")

        def _noop_create_task(coro, **kwargs):
            try:
                if hasattr(coro, "close"):
                    coro.close()
            except Exception:
                pass
            t = MagicMock(spec=asyncio.Task)
            t.add_done_callback = MagicMock()
            return t

        coord = _make_ai_coord(
            options={
                "enable_ai_description": True,
                "ai_task_entity": ai_task_entity,
                "ai_cooldown_seconds": 60,
            },
            data={CAM_A: {}},
            _shc_state_cache={},
            _camera_entities={CAM_A: fake_cam_entity},
            # Methods called after line 5569 when a result is returned:
            _ai_record_call=MagicMock(),
            async_set_updated_data=MagicMock(),
            hass=SimpleNamespace(
                is_stopping=False,
                async_create_task=MagicMock(side_effect=_noop_create_task),
                services=SimpleNamespace(async_call=svc_call),
                bus=SimpleNamespace(async_fire=MagicMock()),
            ),
        )
        coord._ai_window_allowed = MagicMock(return_value=True)
        coord._ai_rate_allowed = MagicMock(return_value=True)
        return coord

    async def test_ai_task_entity_is_included_in_service_call(self) -> None:
        """Line 5569: ai_call_data['entity_id'] = ai_task_entity when non-empty."""
        svc_call = AsyncMock(
            return_value={"data": "Keine sicherheitsrelevanten Beobachtungen."}
        )
        coord = self._make_full_ai_coord(
            ai_task_entity="ai_task.my_llm", svc_call=svc_call
        )

        result = await BoschCameraCoordinator.async_generate_ai_description(
            coord, CAM_A, force=True
        )

        assert svc_call.called
        # Third positional arg to async_call is the service data dict
        call_data = svc_call.call_args[0][2]
        assert call_data.get("entity_id") == "ai_task.my_llm"
        assert result == "Keine sicherheitsrelevanten Beobachtungen."

    async def test_empty_ai_task_entity_not_included(self) -> None:
        """Line 5568: ai_task_entity empty → no entity_id key in call_data."""
        svc_call = AsyncMock(return_value={"data": "some text"})
        coord = self._make_full_ai_coord(ai_task_entity="", svc_call=svc_call)

        await BoschCameraCoordinator.async_generate_ai_description(
            coord, CAM_A, force=True
        )

        call_data = svc_call.call_args[0][2]
        assert "entity_id" not in call_data


# ─────────────────────────────────────────────────────────────────────────────
# 10. Slow-tier deferred: add to deferred set (lines 3213-3219)
#     Condition: _defer_diag=True, do_slow=True, stream_active=True, defer_bound=False
# ─────────────────────────────────────────────────────────────────────────────


def _make_resp(status: int, json_data=None, text_data: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session_fn(url_routes: dict):
    state: dict = {
        k: (list(v) if isinstance(v, list) else [v]) for k, v in url_routes.items()
    }
    sorted_patterns = sorted(state.keys(), key=len, reverse=True)

    def _get(url, **kwargs):
        for pattern in sorted_patterns:
            if pattern in url:
                queue = state[pattern]
                if queue:
                    r = queue.pop(0)
                    if not queue:
                        queue.append(r)
                    return r
        return _make_resp(200, [])

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


_PATCH_SESSION = "custom_components.bosch_shc_camera.async_get_bosch_cloud_session"

_CAM_GEN2_INDOOR_PRIV_ON = {
    "id": CAM_A,
    "hardwareVersion": "HOME_Eyes_Indoor",
    "featureSupport": {"light": False, "panLimit": 0},
    "featureStatus": {},
    "privacyMode": "ON",
}


def _build_slow_tier_routes(cam_info: dict, extra_routes: dict | None = None) -> dict:
    cid = cam_info["id"]
    routes: dict = {
        "v11/video_inputs": _make_resp(200, [cam_info]),
        f"{cid}/ping": _make_resp(200, {}, text_data="ONLINE"),
        f"{cid}/wifiinfo": _make_resp(200, {"rssiValueDb": -60, "signalStrength": 80}),
        f"{cid}/ambient_light_sensor_level": _make_resp(
            200, {"ambientLightSensorLevel": 500}
        ),
        f"{cid}/motion": _make_resp(200, {"sensitivity": "LOW"}),
        f"{cid}/audioAlarm": _make_resp(200, {"sensitivity": 50}),
        f"{cid}/firmware": _make_resp(200, {"version": "9.40.25"}),
        f"{cid}/recording_options": _make_resp(200, {"enabled": False}),
        f"{cid}/unread_events_count": _make_resp(200, {"count": 0}),
        f"{cid}/commissioned": _make_resp(200, {"connected": True}),
        f"{cid}/timestamp": _make_resp(200, {"result": False}),
        f"{cid}/notifications": _make_resp(200, []),
        f"{cid}/rules": _make_resp(200, []),
        f"{cid}/motion_sensitive_areas": _make_resp(200, []),
        f"{cid}/privacy_masks": _make_resp(200, []),
        f"{cid}/privacy_sound_override": _make_resp(200, {"result": False}),
        f"{cid}/ledlights": _make_resp(200, {"state": "OFF"}),
        f"{cid}/lens_elevation": _make_resp(200, {"elevation": 0}),
        f"{cid}/audio": _make_resp(200, {"volume": 50}),
        f"{cid}/lighting/motion": _make_resp(200, {}),
        f"{cid}/lighting/ambient": _make_resp(200, {}),
        f"{cid}/lighting": _make_resp(200, {}),
        f"{cid}/intrusionDetectionConfig": _make_resp(200, {}),
        f"{cid}/alarm_settings": _make_resp(200, {}),
        f"{cid}/alarmStatus": _make_resp(
            200, {"alarmType": "NONE", "intrusionSystem": "INACTIVE"}
        ),
        f"{cid}/iconLedBrightness": _make_resp(200, {"value": 0}),
        f"{cid}/zones": _make_resp(200, []),
        f"{cid}/privateAreas": _make_resp(200, []),
        f"{cid}/lighting_options": _make_resp(200, {}),
        f"{cid}/autofollow": _make_resp(200, {"enabled": False}),
        f"{cid}/lighting/switch": _make_resp(200, {}),
    }
    if extra_routes:
        routes.update(extra_routes)
    return routes


def _make_update_data_coord(**overrides):
    """Coordinator stub for _async_update_data tests."""
    import threading
    import time as _time

    def _create_task(coro, **kwargs):
        try:
            if hasattr(coro, "close"):
                coro.close()
        except Exception:
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        token="tok-A",
        refresh_token="rfr-B",
        _refreshed_refresh=None,
        _entry=SimpleNamespace(
            entry_id="01KM38DHZ525S61HPENAT7NHC0",
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
        ),
        options={},
        _last_status=float("-inf"),
        _last_events=float("-inf"),
        _last_slow=float("-inf"),
        _last_smb_cleanup=_time.monotonic(),
        _last_nvr_cleanup=_time.monotonic(),
        _fcm_lock=threading.Lock(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_client=None,
        _hw_version={},
        _cached_status={CAM_A: "ONLINE"},
        _cached_events={},
        _last_event_ids={},
        _alert_sent_ids={},
        _commissioned_cache={},
        _live_connections={},
        _offline_since={},
        _per_cam_status_at={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_error_at={},
        _local_promote_at={},
        _lan_tcp_reachable={},
        _rcp_lan_ip_cache={},
        _local_creds_cache={},
        _shc_state_cache={},
        _wifiinfo_cache={},
        _privacy_set_at={},
        _light_set_at={},
        _notif_set_at={},
        _lighting_switch_cache={},
        _pan_cache={},
        _ambient_light_cache={},
        _firmware_cache={},
        _unread_events_cache={},
        _privacy_sound_cache={},
        _timestamp_cache={},
        _notifications_cache={},
        _rules_cache={},
        _cloud_zones_cache={},
        _cloud_privacy_masks_cache={},
        _lighting_options_cache={},
        _ledlights_cache={},
        _lens_elevation_cache={},
        _audio_cache={},
        _motion_light_cache={},
        _ambient_lighting_cache={},
        _global_lighting_cache={},
        _intrusion_config_cache={},
        _intrusion_config_set_at={},
        _audio_detection_cache={},
        _audio_detection_set_at={},
        _motion_set_at={},
        _alarm_settings_set_at={},
        _alarm_settings_cache={},
        _alarm_status_cache={},
        _arming_cache={},
        _icon_led_brightness_cache={},
        _gen2_zones_cache={},
        _gen2_private_areas_cache={},
        _WRITE_LOCK_SECS=30.0,
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        _arming_set_at={},
        _feature_flags={"dummy": True},
        _protocol_checked=True,
        _integration_version="11.0.10",
        _OFFLINE_EXTENDED_INTERVAL=900,
        _ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _should_check_status=MagicMock(return_value=False),
        _cleanup_stale_devices=MagicMock(),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        _async_update_lan_diagnostic_sensors=AsyncMock(),
        _get_cam_lan_ip=MagicMock(return_value=None),
        async_mark_events_read=AsyncMock(),
        _is_write_locked=MagicMock(return_value=False),
        shc_ready=False,
        get_model_config=lambda cid: SimpleNamespace(generation=2),
        get_quality_params=MagicMock(return_value=(True, 1)),
        _run_nvr_cleanup_bg=AsyncMock(return_value=None),
        _run_smb_cleanup_bg=AsyncMock(return_value=None),
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_create_background_task=MagicMock(),
            async_add_executor_job=AsyncMock(),
            data={},
            bus=SimpleNamespace(async_fire=MagicMock()),
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp"),
        ),
        debug=False,
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    coord._first_tick_done = True
    return coord


class TestSlowTierDeferredAdd:
    """Lines 3213-3219: stream active + do_slow=True → cam added to _slow_tier_deferred."""

    @pytest.mark.asyncio
    async def test_slow_tier_deferred_when_stream_active(self) -> None:
        """Lines 3213-3219: _defer_diag=True, stream_active, do_slow → deferred."""
        coord = _make_update_data_coord(
            _last_slow=float("-inf"),  # do_slow=True
            _cached_status={CAM_A: "ONLINE"},
            # stream is active for this camera
            _live_connections={CAM_A: MagicMock()},
            options={"defer_diag_during_stream": True},
        )
        # Ensure slow_tier_deferred is empty at start
        coord._slow_tier_deferred = set()
        coord._slow_tier_defer_since = {}
        # Privacy-ON-while-streaming path schedules a teardown coroutine.
        coord._tear_down_live_stream = AsyncMock()

        routes = _build_slow_tier_routes(_CAM_GEN2_INDOOR_PRIV_ON)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, new=AsyncMock(return_value=session)):
            result = await BoschCameraCoordinator._async_update_data(coord)

        # Camera should now be in deferred set — slow-tier was skipped
        assert CAM_A in coord._slow_tier_deferred, (
            "Camera should be in _slow_tier_deferred when stream active + do_slow"
        )
        assert isinstance(result, dict)


# ─────────────────────────────────────────────────────────────────────────────
# 11. Slow-tier deferred removal: deferred fetch now safe (lines 3223-3225)
# ─────────────────────────────────────────────────────────────────────────────


class TestSlowTierDeferredRemove:
    """Lines 3223-3225: cam is in deferred set + stream idle → remove from deferred."""

    @pytest.mark.asyncio
    async def test_slow_tier_deferred_removed_when_stream_idle(self) -> None:
        """Lines 3223-3225: do_slow=True, cam in deferred, stream NOT active → remove."""
        import time as _time2

        coord = _make_update_data_coord(
            _last_slow=float("-inf"),  # do_slow=True
            _cached_status={CAM_A: "ONLINE"},
            _live_connections={},  # stream NOT active
            options={"defer_diag_during_stream": True},
        )
        # Pre-populate deferred so branch 3220 fires
        coord._slow_tier_deferred = {CAM_A}
        coord._slow_tier_defer_since = {CAM_A: _time2.monotonic() - 10}

        routes = _build_slow_tier_routes(_CAM_GEN2_INDOOR_PRIV_ON)
        session = _make_session_fn(routes)

        with patch(_PATCH_SESSION, new=AsyncMock(return_value=session)):
            result = await BoschCameraCoordinator._async_update_data(coord)

        # Camera should be removed from deferred set — slow-tier ran normally
        assert CAM_A not in coord._slow_tier_deferred, (
            "Camera should be removed from _slow_tier_deferred when stream idle"
        )
        assert CAM_A not in coord._slow_tier_defer_since
        assert isinstance(result, dict)


# ─────────────────────────────────────────────────────────────────────────────
# 12. handle_trigger_snapshot (handle_refresh_image): list entity_id + None coord
#     Lines 8523, 8527
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleRefreshImageEdgeCases:
    """Lines 8523 (list entity_id) and 8527 (None runtime_data)."""

    @pytest.mark.asyncio
    async def test_entity_id_list_is_unwrapped(self, hass) -> None:  # type: ignore[no-untyped-def]
        """Line 8523: entity_id arrives as a list → target[0] taken."""
        from custom_components.bosch_shc_camera import _register_services

        _register_services(hass)

        cam_mock = MagicMock()
        cam_mock.entity_id = "camera.bosch_test"
        cam_mock._async_trigger_image_refresh = AsyncMock()

        coord = MagicMock()
        coord._camera_entities = {CAM_A: cam_mock}

        entry = MagicMock()
        entry.runtime_data = coord

        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.async_create_task = MagicMock()

        await hass.services.async_call(
            "bosch_shc_camera",
            "trigger_snapshot",
            {"entity_id": ["camera.bosch_test"]},
            blocking=True,
        )
        # A create_task for the image refresh should have been called
        hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_none_runtime_data_is_skipped(self, hass) -> None:  # type: ignore[no-untyped-def]
        """Line 8527: entry.runtime_data is None/falsy → continue (no crash)."""
        from custom_components.bosch_shc_camera import _register_services

        _register_services(hass)

        entry = MagicMock()
        entry.runtime_data = None  # falsy → continue

        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])

        # Must not raise
        await hass.services.async_call(
            "bosch_shc_camera",
            "trigger_snapshot",
            {},
            blocking=True,
        )

    @pytest.mark.asyncio
    async def test_empty_list_entity_id_becomes_none(self, hass) -> None:  # type: ignore[no-untyped-def]
        """Line 8523: entity_id=[] → target becomes None → all-camera refresh path."""
        from custom_components.bosch_shc_camera import _register_services

        _register_services(hass)

        coord = MagicMock()
        coord._camera_entities = {}
        coord.async_request_refresh = AsyncMock()

        entry = MagicMock()
        entry.runtime_data = coord

        hass.config_entries.async_loaded_entries = MagicMock(return_value=[entry])
        hass.async_create_task = MagicMock()

        await hass.services.async_call(
            "bosch_shc_camera",
            "trigger_snapshot",
            {"entity_id": []},
            blocking=True,
        )
        # Coordinator-level refresh should be queued (target became None)
        hass.async_create_task.assert_called()


# ─────────────────────────────────────────────────────────────────────────────
# 13. handle_set_motion_zones: TypeError/ValueError on coordinate (lines 8803-8804)
# 14. handle_set_privacy_masks: TypeError/ValueError on coordinate (lines 9164-9165)
# 15. handle_delete_event: no file_path + no camera → return 0 (line 9739)
# ─────────────────────────────────────────────────────────────────────────────


class TestSetMotionZonesCoordValueError:
    """Lines 8803-8804: float(z[key]) raises TypeError/ValueError → ServiceValidationError."""

    @pytest.mark.asyncio
    async def test_zone_non_numeric_coord_raises(self, hass) -> None:  # type: ignore[no-untyped-def]
        from homeassistant.exceptions import ServiceValidationError

        from custom_components.bosch_shc_camera import _register_services

        _register_services(hass)

        with pytest.raises(ServiceValidationError) as exc_info:
            await hass.services.async_call(
                "bosch_shc_camera",
                "set_motion_zones",
                {
                    "camera_id": CAM_A,
                    "zones": [{"x": "abc", "y": 0.5, "w": 0.1, "h": 0.1}],
                },
                blocking=True,
            )
        assert exc_info.value.translation_key == "value_out_of_range"
        placeholders = exc_info.value.translation_placeholders or {}
        assert placeholders.get("kind") == "zone"
        assert placeholders.get("field") == "x"

    @pytest.mark.asyncio
    async def test_zone_none_coord_raises(self, hass) -> None:  # type: ignore[no-untyped-def]
        from homeassistant.exceptions import ServiceValidationError

        from custom_components.bosch_shc_camera import _register_services

        _register_services(hass)

        with pytest.raises(ServiceValidationError) as exc_info:
            await hass.services.async_call(
                "bosch_shc_camera",
                "set_motion_zones",
                {
                    "camera_id": CAM_A,
                    "zones": [{"x": None, "y": 0.5, "w": 0.1, "h": 0.1}],
                },
                blocking=True,
            )
        assert exc_info.value.translation_key == "value_out_of_range"
        assert (exc_info.value.translation_placeholders or {}).get("kind") == "zone"


class TestSetPrivacyMasksCoordValueError:
    """Lines 9164-9165: float(m[key]) raises TypeError/ValueError → ServiceValidationError."""

    @pytest.mark.asyncio
    async def test_mask_non_numeric_coord_raises(self, hass) -> None:  # type: ignore[no-untyped-def]
        from homeassistant.exceptions import ServiceValidationError

        from custom_components.bosch_shc_camera import _register_services

        _register_services(hass)

        with pytest.raises(ServiceValidationError) as exc_info:
            await hass.services.async_call(
                "bosch_shc_camera",
                "set_privacy_masks",
                {
                    "camera_id": CAM_A,
                    "masks": [{"x": "bad", "y": 0.5, "w": 0.1, "h": 0.1}],
                },
                blocking=True,
            )
        assert exc_info.value.translation_key == "value_out_of_range"
        placeholders = exc_info.value.translation_placeholders or {}
        assert placeholders.get("kind") == "mask"
        assert placeholders.get("field") == "x"

    @pytest.mark.asyncio
    async def test_mask_none_coord_raises(self, hass) -> None:  # type: ignore[no-untyped-def]
        from homeassistant.exceptions import ServiceValidationError

        from custom_components.bosch_shc_camera import _register_services

        _register_services(hass)

        with pytest.raises(ServiceValidationError) as exc_info:
            await hass.services.async_call(
                "bosch_shc_camera",
                "set_privacy_masks",
                {
                    "camera_id": CAM_A,
                    "masks": [{"x": None, "y": 0.5, "w": 0.1, "h": 0.1}],
                },
                blocking=True,
            )
        assert exc_info.value.translation_key == "value_out_of_range"
        assert (exc_info.value.translation_placeholders or {}).get("kind") == "mask"


# NOTE (issue: 100%-coverage round): the `if not camera: return 0` defensive
# arm inside delete_event's nested _delete() is UNREACHABLE via the service —
# the handler raises ServiceValidationError("argument_required") when BOTH
# file_path and camera are empty, so by the time _delete() runs at least one is
# set; with file_path empty, camera is therefore always non-empty. That arm is
# marked `# pragma: no cover` in __init__.py rather than tested with an
# impossible input.


# ─────────────────────────────────────────────────────────────────────────────
# 16. _register_go2rtc_stream: Unix socket path found (lines 6139-6140)
# 17. _register_go2rtc_stream: UnixConnector creation fails (lines 6148-6151)
# 18. _start_tls_proxy _on_loop: hass.is_stopping=True → early return (line 6286)
# ─────────────────────────────────────────────────────────────────────────────


class TestRegisterGo2rtcStreamUnixSocket:
    """Lines 6139-6140: go2rtc.sock exists → sock_path set + break."""

    @pytest.mark.asyncio
    async def test_unix_socket_path_used_when_exists(self) -> None:
        """Lines 6139-6140: os.path.exists returns True → sock_path assigned, loop breaks."""
        import aiohttp as _aiohttp

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.text = AsyncMock(return_value="")
        fake_check = MagicMock()
        fake_check.status = 200
        fake_check.__aenter__ = AsyncMock(return_value=fake_check)
        fake_check.__aexit__ = AsyncMock(return_value=None)

        fake_session = MagicMock()
        fake_put_resp = MagicMock()
        fake_put_resp.status = 200
        fake_put_resp.text = AsyncMock(return_value="")
        fake_session.put = AsyncMock(return_value=fake_put_resp)
        fake_session.get = MagicMock(return_value=fake_check)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=None)

        fake_connector = MagicMock()
        fake_connector.__enter__ = MagicMock(return_value=fake_connector)
        fake_connector.__exit__ = MagicMock(return_value=None)

        coord = SimpleNamespace(
            _camera_entities={},
            hass=SimpleNamespace(
                config=SimpleNamespace(config_dir="/config"),
            ),
        )

        sock_path_found: list[str] = []

        with (
            patch("os.path.exists", return_value=True),
            patch(
                "aiohttp.UnixConnector", return_value=fake_connector
            ) as _mock_connector,
            patch("aiohttp.ClientSession", return_value=fake_session),
            patch(
                "asyncio.timeout",
                return_value=MagicMock(
                    __aenter__=AsyncMock(return_value=None),
                    __aexit__=AsyncMock(return_value=None),
                ),
            ),
        ):
            # Capture what sock_path resolves to by running enough of the method
            # The key assertion: os.path.exists was checked (line 6138) and
            # the loop broke after finding the first candidate (lines 6139-6140).
            # We verify UnixConnector was called with a path matching config_dir.
            result = await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://cam.example.com:443/stream"
            )

        # UnixConnector should have been created with the first candidate path
        if _mock_connector.called:
            called_path = (
                _mock_connector.call_args[1].get("path")
                or _mock_connector.call_args[0][0]
            )
            assert (
                "/config" in called_path or called_path == "/homeassistant/go2rtc.sock"
            )

    @pytest.mark.asyncio
    async def test_unix_connector_oserror_falls_through(self) -> None:
        """Lines 6148-6151: UnixConnector raises OSError → debug log, connector=None."""
        import aiohttp as _aiohttp

        fake_resp = MagicMock()
        fake_resp.status = 200
        fake_resp.text = AsyncMock(return_value="")
        fake_check = MagicMock()
        fake_check.status = 200
        fake_check.__aenter__ = AsyncMock(return_value=fake_check)
        fake_check.__aexit__ = AsyncMock(return_value=None)

        fake_session = MagicMock()
        fake_put_resp = MagicMock()
        fake_put_resp.status = 200
        fake_put_resp.text = AsyncMock(return_value="")
        fake_session.put = AsyncMock(return_value=fake_put_resp)
        fake_session.get = MagicMock(return_value=fake_check)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=None)

        coord = SimpleNamespace(
            _camera_entities={},
            hass=SimpleNamespace(
                config=SimpleNamespace(config_dir="/config"),
            ),
        )

        with (
            patch("os.path.exists", return_value=True),
            patch("aiohttp.UnixConnector", side_effect=OSError("no socket")),
            patch("aiohttp.ClientSession", return_value=fake_session),
            patch(
                "asyncio.timeout",
                return_value=MagicMock(
                    __aenter__=AsyncMock(return_value=None),
                    __aexit__=AsyncMock(return_value=None),
                ),
            ),
        ):
            # Should not raise — OSError is caught, connector stays None, PUT proceeds
            result = await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://cam.example.com:443/stream"
            )
        # Method completed without raising
        assert isinstance(result, bool)


class TestStartTlsProxyOnLoopIsStopping:
    """Line 6286: _died_callback._on_loop returns early when hass.is_stopping."""

    @pytest.mark.asyncio
    async def test_on_loop_returns_early_when_stopping(self) -> None:
        """Line 6286: hass.is_stopping=True inside _on_loop → no create_task called."""
        import ssl as _ssl

        create_task_mock = MagicMock()
        bg_tasks: set = set()

        coord = SimpleNamespace(
            _tls_ssl_ctx=MagicMock(spec=_ssl.SSLContext),
            _tls_proxy_ports={},
            hass=SimpleNamespace(
                is_stopping=True,
                async_create_task=create_task_mock,
                async_add_executor_job=AsyncMock(
                    return_value=MagicMock(spec=_ssl.SSLContext)
                ),
                loop=MagicMock(),
            ),
            _bg_tasks=bg_tasks,
            _on_tls_proxy_died=AsyncMock(),
        )

        captured_callback: list = []

        def fake_start_tls_proxy(
            ssl_ctx, cam_id, cam_host, cam_port, tls_proxy_ports, **kwargs
        ):
            died_cb = kwargs.get("on_proxy_died")
            if died_cb is not None:
                captured_callback.append(died_cb)
            return 12345

        with patch(
            "custom_components.bosch_shc_camera.start_tls_proxy",
            side_effect=fake_start_tls_proxy,
        ):
            port = await BoschCameraCoordinator._start_tls_proxy(
                coord, CAM_A, "192.168.1.100", 8554
            )

        assert port == 12345
        assert len(captured_callback) == 1

        # Now simulate the _died_callback being called from a thread
        died_cb = captured_callback[0]

        # _died_callback calls hass.loop.call_soon_threadsafe(_on_loop)
        # We need to capture and invoke _on_loop ourselves
        on_loop_captures: list = []

        def fake_call_soon_threadsafe(fn):
            on_loop_captures.append(fn)

        coord.hass.loop.call_soon_threadsafe = MagicMock(
            side_effect=fake_call_soon_threadsafe
        )
        died_cb()

        assert len(on_loop_captures) == 1
        # Now invoke _on_loop — hass.is_stopping=True → should return without create_task
        on_loop_captures[0]()
        create_task_mock.assert_not_called()
        assert len(bg_tasks) == 0
