"""AI-description budget/rate/window gating + the ai_task generation call.

Covers the AUTO-path activation-window gate (time-of-day + condition
entity), the daily budget counter (persisted via a `Store`, rolled over
on local-date change), the per-camera cooldown gate, and the actual
`ai_task.generate_data` call that produces a camera snapshot description
(`async_generate_ai_description`) — shared by the notify-include path
(`fcm.py`) and the on-motion auto path (`__init__.py`).

These read/write a handful of coordinator-instance containers
(`_ai_last_call`, `_ai_day_count`, `_ai_day_stamp`, `_ai_budget_logged_day`,
`ai_in_flight`, `_ai_budget_store`) plus `self.hass`/`self.options`/
`self.data`/`self.shc_state_cache`/`self.camera_entities` — they don't
belong inline on the `DataUpdateCoordinator` subclass any more than the
other free-function modules in this package do. Matches the
`quality_prefs`/`rcp_client` pattern already established here: free
functions taking the coordinator instance as their first argument.

`BoschCameraCoordinator` keeps a thin delegating method for each of these
(same name/signature, calls straight into the matching function here) so
every existing call site — `fcm.py`, `__init__.py`'s on-motion auto path,
`services.py`'s `analyze_camera_ai` service, `ai_analysis.py` — keeps
working unchanged, and so does the test suite's unbound-method-call-on-
a-stub pattern (`BoschCameraCoordinator.ai_budget_state(coord)`).

IMPORTANT: where one of these functions originally called another
extracted method on `self` (e.g. `async_generate_ai_description` calling
`self._ai_window_allowed()`/`self._ai_rate_allowed()`, `_ai_rate_allowed`
calling `self.ai_budget_state()`, `ai_record_call` and `ai_budget_state`
calling `self._async_save_ai_budget()`), the extracted version keeps
calling through the COORDINATOR instance (`coordinator.method_name(...)`)
rather than the raw module-level function directly — those methods are
public/overridable coordinator methods that tests patch per-instance, and
calling the module function directly would silently bypass any such patch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


def _ai_window_allowed(coordinator: BoschCameraCoordinator) -> bool:
    """Time-window + condition-entity gate for AUTO AI analyses.

    Returns True if the current moment is within the configured activation
    window AND the condition entity (if any) is in the expected state.
    When neither gate is configured, always returns True.
    Manual force=True callers MUST bypass this — callers are responsible.
    """
    opts = coordinator.options
    time_start_raw: str = (opts.get("ai_active_time_start") or "").strip()
    time_end_raw: str = (opts.get("ai_active_time_end") or "").strip()
    condition_entity_id: str = (opts.get("ai_active_condition_entity") or "").strip()
    condition_state: str = (opts.get("ai_active_condition_state") or "not_home").strip()

    time_gate_active = bool(time_start_raw and time_end_raw)
    if bool(time_start_raw) != bool(time_end_raw):
        _LOGGER.warning(
            "AI activation window: only one of start/end time is configured"
            " (start=%r end=%r) — time gate disabled. Set both or neither.",
            time_start_raw,
            time_end_raw,
        )
    condition_gate_active = bool(condition_entity_id)

    if not time_gate_active and not condition_gate_active:
        return True

    time_allowed = True
    if time_gate_active:
        try:
            from datetime import time as _dt_time

            def _parse_t(s: str) -> _dt_time:
                parts = s.split(":")
                h, m = int(parts[0]), int(parts[1])
                sec = int(parts[2]) if len(parts) > 2 else 0
                return _dt_time(h, m, sec)

            t_start = _parse_t(time_start_raw)
            t_end = _parse_t(time_end_raw)
            now_t = dt_util.now().time().replace(microsecond=0)
            if t_end >= t_start:
                # Normal window: e.g. 08:00–22:00. start==end is a zero-width
                # window (allowed only at that exact second) — matches live.
                time_allowed = t_start <= now_t <= t_end
            else:
                # Overnight window: e.g. 22:00–06:00
                time_allowed = now_t >= t_start or now_t <= t_end
        except Exception:
            _LOGGER.debug(
                "AI activation window: malformed time value (start=%r end=%r)"
                " — treating as no time gate",
                time_start_raw,
                time_end_raw,
            )
            time_allowed = True  # malformed → allow (fail-open)

    condition_allowed = True
    if condition_gate_active:
        state_obj = coordinator.hass.states.get(condition_entity_id)
        if state_obj is None or state_obj.state in ("unknown", "unavailable"):
            condition_allowed = False  # conservative: don't burn credits
            _LOGGER.debug(
                "AI activation window: condition entity %s is %s — blocking AI",
                condition_entity_id,
                state_obj.state if state_obj else "missing",
            )
        else:
            condition_allowed = state_obj.state == condition_state

    return time_allowed and condition_allowed


def ai_budget_state(coordinator: BoschCameraCoordinator) -> tuple[int, int]:
    """Return (used_today, max_per_day) for the AI-analysis daily budget.

    Rolls the counter over when the local calendar date changes.
    max_per_day == 0 means unlimited.
    """
    opts = coordinator.options
    try:
        max_per_day = int(opts.get("ai_max_per_day", 100) or 0)
    except (TypeError, ValueError):
        max_per_day = 100
    today = dt_util.now().date().isoformat()
    if coordinator._ai_day_stamp != today:
        coordinator._ai_day_stamp = today
        coordinator._ai_day_count = 0
        coordinator.hass.async_create_task(coordinator._async_save_ai_budget())
    return coordinator._ai_day_count, max_per_day


async def async_load_ai_budget(coordinator: BoschCameraCoordinator) -> None:
    """Load persisted daily AI budget from storage (called on setup)."""
    try:
        stored = await coordinator._ai_budget_store.async_load()
    except Exception as err:
        _LOGGER.debug("AI budget store load failed: %s", err)
        stored = None
    if isinstance(stored, dict):
        stored_date: str = stored.get("date", "")
        today = dt_util.now().date().isoformat()
        if stored_date == today:
            try:
                coordinator._ai_day_count = int(stored.get("count", 0))
                coordinator._ai_day_stamp = stored_date
            except (TypeError, ValueError):
                pass
        # else: stored day != today → counter stays at 0 (already reset for new day)


async def _async_save_ai_budget(coordinator: BoschCameraCoordinator) -> None:
    """Persist daily AI budget count to storage."""
    try:
        await coordinator._ai_budget_store.async_save(
            {
                "date": coordinator._ai_day_stamp,
                "count": coordinator._ai_day_count,
            }
        )
    except Exception as err:
        _LOGGER.debug("AI budget store save failed: %s", err)


def _ai_rate_allowed(coordinator: BoschCameraCoordinator, cam_id: str) -> bool:
    """Cooldown + daily-budget gate for AUTO AI analyses."""
    opts = coordinator.options
    try:
        cooldown = float(opts.get("ai_cooldown_seconds", 60) or 0)
    except (TypeError, ValueError):
        cooldown = 60.0
    used, max_per_day = coordinator.ai_budget_state()
    if max_per_day and (used + coordinator.ai_in_flight) >= max_per_day:
        # Use the SAME local-date source as ai_budget_state() above so the
        # one-shot "budget reached" log re-arms in lockstep with the daily
        # counter reset (a UTC date here would suppress the log for the
        # hours between local and UTC midnight). Lesson: events-today UTC bug.
        today = dt_util.now().date().isoformat()
        if coordinator._ai_budget_logged_day != today:
            coordinator._ai_budget_logged_day = today
            _LOGGER.info(
                "AI analysis daily budget of %d reached — skipping until tomorrow",
                max_per_day,
            )
        return False
    last = coordinator._ai_last_call.get(cam_id, float("-inf"))
    return (time.monotonic() - last) >= cooldown


def ai_record_call(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """Record an AI analysis for cooldown + daily-budget accounting."""
    coordinator.ai_budget_state()  # ensure the day-rollover runs first
    coordinator._ai_last_call[cam_id] = time.monotonic()
    coordinator._ai_day_count += 1
    coordinator.hass.async_create_task(coordinator._async_save_ai_budget())


async def async_generate_ai_description(
    coordinator: BoschCameraCoordinator, cam_id: str, *, force: bool = False
) -> str | None:
    """Generate an AI description of a camera's current snapshot via ai_task.

    Shared by the notify-include path (F2) and the on-motion auto path.
    Returns the description text, or None when skipped (rate-limited,
    camera unknown, ai_task unavailable, or empty result). Auto callers
    pass force=False so the cooldown + daily budget apply; manual/service
    callers pass force=True to bypass the cooldown (still counts toward
    the daily budget). Never raises — failures return None so the calling
    notification/event path is never broken.
    """
    if not coordinator.options.get("enable_ai_description", False):
        return None
    if coordinator.shc_state_cache.get(cam_id, {}).get("privacy_mode"):
        return None
    if not force and not coordinator._ai_window_allowed():
        return None
    if not force and not coordinator._ai_rate_allowed(cam_id):
        # Reuse cached description only if not stale and not from a privacy era
        cached_entry = coordinator.data.get(cam_id, {}).get("ai_description", {})
        cached_text: str | None = cached_entry.get("text")
        if cached_text and not coordinator.shc_state_cache.get(cam_id, {}).get(
            "privacy_mode"
        ):
            # Reject cache if generated_at is older than cooldown window or 300s cap
            try:
                opts_cs = coordinator.options
                cooldown_secs = float(opts_cs.get("ai_cooldown_seconds", 60) or 0)
                max_age = min(cooldown_secs, 300.0)
                gen_at_str: str | None = cached_entry.get("generated_at")
                if gen_at_str:
                    gen_dt = datetime.fromisoformat(gen_at_str)
                    age_secs = (datetime.now(UTC) - gen_dt).total_seconds()
                    if max_age > 0 and age_secs <= max_age:
                        return cached_text
            except Exception as _cache_err:
                _LOGGER.debug("AI cache staleness check failed: %s", _cache_err)
        return None
    cam_entity = getattr(coordinator, "camera_entities", {}).get(cam_id)
    if cam_entity is None:
        return None
    entity_id = cam_entity.entity_id
    opts = coordinator.options
    prompt = opts.get("ai_describe_prompt") or (
        "Du bist eine Überwachungskamera-Assistenz. Melde NUR"
        " sicherheitsrelevante Beobachtungen: Personen (auch nur teilweise"
        " sichtbar: Beine, Arme, Silhouette, Schatten), Fahrzeuge, Tiere,"
        " Pakete oder ungewöhnliche Aktivität. Beschreibe NICHT die"
        " Umgebung, Räume, Möbel, Architektur oder Bildqualität und benenne"
        " KEINE Orte. Rate nicht: Fußmatten, Teppiche, Bodenfliesen und"
        " Schatten sind kein Paket. Wenn nichts Sicherheitsrelevantes"
        " erkennbar ist, sage das kurz, z. B.: Keine"
        " sicherheitsrelevanten Beobachtungen."
    )
    language = (opts.get("ai_describe_language") or "").strip() or "Deutsch"
    full_instructions = (
        f"{prompt}\n\nRespond only in {language}."
        f" Antworte ausschließlich auf {language}."
    )
    ai_task_entity = (opts.get("ai_task_entity") or "").strip()
    ai_call_data: dict[str, Any] = {
        "task_name": "Bosch camera snapshot",
        "instructions": full_instructions,
        "attachments": [
            {
                "media_content_id": f"media-source://camera/{entity_id}",
                "media_content_type": "image/jpeg",
            }
        ],
    }
    if ai_task_entity:
        ai_call_data["entity_id"] = ai_task_entity
    coordinator.ai_in_flight += 1
    _ai_resp: Any = None
    _text_result: str | None = None
    try:
        async with asyncio.timeout(20):
            _ai_resp = await coordinator.hass.services.async_call(
                "ai_task",
                "generate_data",
                ai_call_data,
                blocking=True,
                return_response=True,
            )
        if _ai_resp is not None:
            _text_candidate = (
                str(_ai_resp.get("data", ""))
                if isinstance(_ai_resp, dict)
                else str(_ai_resp or "")
            ).strip()
            if _text_candidate:
                _text_result = _text_candidate
                # Record the call while ai_in_flight is still 1 so the
                # budget counter reflects in-progress work correctly.
                coordinator.ai_record_call(cam_id)
    except TimeoutError:
        _LOGGER.debug("AI description timed out (20s) for %s", cam_id[:8])
    except Exception as err:
        _LOGGER.debug("AI description generate failed for %s: %s", cam_id[:8], err)
    finally:
        coordinator.ai_in_flight -= 1
    if _text_result is None:
        return None
    text = _text_result
    generated_at = datetime.now(UTC).isoformat()
    if cam_id in coordinator.data:
        coordinator.data[cam_id]["ai_description"] = {
            "text": text,
            "generated_at": generated_at,
            "ai_task_entity": ai_task_entity or "default",
        }
        coordinator.async_set_updated_data(coordinator.data)
    coordinator.hass.bus.async_fire(
        "bosch_shc_camera_ai_description",
        {
            "camera_id": cam_id,
            "entity_id": entity_id,
            "description": text,
            "generated_at": generated_at,
        },
    )
    return text
