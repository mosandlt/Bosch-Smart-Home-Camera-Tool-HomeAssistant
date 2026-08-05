"""AI Camera Analysis — motion-triggered structured suspicion scoring.

Sibling to the free-text AI Snapshot Description feature
(`ai_analysis_runtime.py`'s `async_generate_ai_description`): that feature asks for
a plain description string, this one asks `ai_task.generate_data` for a
STRUCTURED 1-10 suspicion score + fields, with its own separate
cooldown/daily-budget so the two AI features never compete for the same
allowance.

Design inspired by concepts from
github.com/simpleaddins/HomeAssistantAICameraCentre (MIT) per Thomas's
explicit request — independently reimplemented from scratch against this
integration's own coordinator/entity/data model; no code copied. See the
README credit line.

Module shape mirrors `recorder.py`: plain `async def`/`def` functions
taking `coordinator` as the first argument, unit-testable with a stub
coordinator and no running Home Assistant instance.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from . import ai_alert_store
from .const import (
    CONF_AI_ANALYSIS_COOLDOWN_SECONDS,
    CONF_AI_ANALYSIS_ENABLED,
    CONF_AI_ANALYSIS_MAX_PER_DAY,
    CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES,
    CONF_AI_ANALYSIS_SNAPSHOT_COUNT,
    CONF_AI_ANALYSIS_TASK_ENTITY,
)

if TYPE_CHECKING:
    from .coordinator import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

TIMEOUT_AI_ANALYSIS_CALL = 25.0

# ai_task.generate_data `structure` param — HA-native selector-based schema
# (confirmed shape: {field: {description, required, selector: {type: {}}}})
# so the response comes back as a validated dict, not free text to parse.
STRUCTURE_SCHEMA: dict[str, Any] = {
    "score": {
        "description": (
            "Suspicion/security-relevance score, 1 (nothing notable) to "
            "10 (clear threat/break-in in progress)"
        ),
        "required": True,
        "selector": {"number": {"min": 1, "max": 10, "mode": "box"}},
    },
    "short": {
        "description": "One-sentence summary of what is happening",
        "required": True,
        "selector": {"text": {}},
    },
    "detail": {
        "description": "Longer description of the observed activity",
        "required": False,
        "selector": {"text": {}},
    },
    "direction": {
        "description": (
            "Movement direction if a person/vehicle is present "
            "(e.g. approaching, leaving, passing)"
        ),
        "required": False,
        "selector": {"text": {}},
    },
    "carrying": {
        "description": "Any object being carried, if visible",
        "required": False,
        "selector": {"text": {}},
    },
    "activity": {
        "description": "Short activity label, e.g. walking, delivering, loitering",
        "required": False,
        "selector": {"text": {}},
    },
    "gate_state": {
        "description": "State of any visible gate/door/entry point, if applicable",
        "required": False,
        "selector": {"text": {}},
    },
    "gate_risk": {
        "description": "True if the gate/entry point appears compromised or forced",
        "required": False,
        "selector": {"boolean": {}},
    },
    "known_person": {
        "description": (
            "True if the visible person matches one of the known-visitor "
            "descriptions supplied in the prompt"
        ),
        "required": False,
        "selector": {"boolean": {}},
    },
}

DEFAULT_ANALYSIS_PROMPT = (
    "Du bist eine Überwachungskamera-Assistenz für Sicherheitsanalyse. "
    "Bewerte JEDES Bild auf einer Skala von 1 (nichts Auffälliges) bis "
    "10 (klare Bedrohung/Einbruch) und beschreibe kurz, was zu sehen ist. "
    "Score 1 für leere Szenen, Tiere, Wetter, Schatten, Pflanzen. Score "
    "steigt mit unbekannten Personen, verdächtigem Verhalten "
    "(Herumschleichen, Maskierung, Werkzeug an Türen/Fenstern), niedriger "
    "Uhrzeit. Rate nicht — wenn unklar, wähle einen niedrigen Score."
)


# ── Gating (own cooldown/budget, mirrors coordinator.py's describe-feature
#    helpers exactly in shape, deliberately separate state) ─────────────────


def ai_analysis_budget_state(
    coordinator: BoschCameraCoordinator,
) -> tuple[int, int]:
    """Return (used_today, max_per_day) for the AI-analysis daily budget.

    Own counter — mirrors `BoschCameraCoordinator.ai_budget_state` exactly
    in shape but reads/writes the analysis-specific coordinator fields
    (`_ai_analysis_day_count` etc.) so this feature's budget can never be
    silently consumed by (or silently consume) the AI Snapshot Description
    feature's separate daily allowance. `max_per_day == 0` means unlimited.
    """
    opts = coordinator.options
    try:
        max_per_day = int(opts.get(CONF_AI_ANALYSIS_MAX_PER_DAY, 200) or 0)
    except (TypeError, ValueError):
        max_per_day = 200
    today = dt_util.now().date().isoformat()
    if coordinator._ai_analysis_day_stamp != today:
        coordinator._ai_analysis_day_stamp = today
        coordinator._ai_analysis_day_count = 0
        coordinator.hass.async_create_task(_async_save_ai_analysis_budget(coordinator))
    return coordinator._ai_analysis_day_count, max_per_day


async def async_load_ai_analysis_budget(coordinator: BoschCameraCoordinator) -> None:
    """Load persisted daily AI-analysis budget from storage (called on setup,
    mirrors `BoschCameraCoordinator.async_load_ai_budget`)."""
    try:
        stored = await coordinator._ai_analysis_budget_store.async_load()
    except Exception as err:
        _LOGGER.debug("AI analysis budget store load failed: %s", err)
        stored = None
    if isinstance(stored, dict):
        stored_date: str = stored.get("date", "")
        today = dt_util.now().date().isoformat()
        if stored_date == today:
            try:
                coordinator._ai_analysis_day_count = int(stored.get("count", 0))
                coordinator._ai_analysis_day_stamp = stored_date
            except (TypeError, ValueError):
                pass


async def _async_save_ai_analysis_budget(coordinator: BoschCameraCoordinator) -> None:
    try:
        await coordinator._ai_analysis_budget_store.async_save(
            {
                "date": coordinator._ai_analysis_day_stamp,
                "count": coordinator._ai_analysis_day_count,
            }
        )
    except Exception as err:
        _LOGGER.debug("AI analysis budget store save failed: %s", err)


def _ai_analysis_rate_allowed(coordinator: BoschCameraCoordinator, cam_id: str) -> bool:
    """Per-camera cooldown + global daily-budget gate."""
    opts = coordinator.options
    try:
        cooldown = float(opts.get(CONF_AI_ANALYSIS_COOLDOWN_SECONDS, 30) or 0)
    except (TypeError, ValueError):
        cooldown = 30.0
    used, max_per_day = ai_analysis_budget_state(coordinator)
    if max_per_day and (used + coordinator.ai_analysis_in_flight) >= max_per_day:
        today = dt_util.now().date().isoformat()
        if coordinator._ai_analysis_budget_logged_day != today:
            coordinator._ai_analysis_budget_logged_day = today
            _LOGGER.info(
                "AI camera analysis daily budget of %d reached — skipping "
                "until tomorrow",
                max_per_day,
            )
        return False
    last = coordinator._ai_analysis_last_call.get(cam_id, float("-inf"))
    return (time.monotonic() - last) >= cooldown


def ai_analysis_record_call(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """Record an AI analysis call for cooldown + daily-budget accounting."""
    ai_analysis_budget_state(coordinator)  # ensure day-rollover runs first
    coordinator._ai_analysis_last_call[cam_id] = time.monotonic()
    coordinator._ai_analysis_day_count += 1
    coordinator.hass.async_create_task(_async_save_ai_analysis_budget(coordinator))


def per_camera_analysis_enabled(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> bool:
    """`switch.<cam>_ai_analysis` state — default True (opt-OUT per camera)
    once the global master switch (`ai_analysis_enabled`) is on, matching
    this integration's existing per-camera-switch-defaults-on convention.
    Backed by `coordinator.ai_analysis_camera_enabled` (CacheFieldView,
    wired up alongside the switch entity)."""
    return bool(
        getattr(coordinator, "ai_analysis_camera_enabled", {}).get(cam_id, True)
    )


# ── Prompt construction ──────────────────────────────────────────────────────


def _build_prompt(coordinator: BoschCameraCoordinator, cam_id: str) -> str:
    """Base instructions + this camera's scene-context text entity + all
    known-visitor subentry descriptions + repeat-context hint."""
    base = coordinator.options.get("ai_analysis_prompt") or DEFAULT_ANALYSIS_PROMPT
    parts = [base]

    scene_context = (
        getattr(coordinator, "ai_analysis_scene_context", {}).get(cam_id) or ""
    ).strip()
    if scene_context:
        parts.append(f"Kontext zu dieser Kamera: {scene_context}")

    visitors = _known_visitors(coordinator)
    if visitors:
        visitor_lines = "\n".join(
            f"- {name}: {desc}" for name, desc in visitors if name or desc
        )
        if visitor_lines:
            parts.append(
                "Bekannte Personen (NICHT automatisch als verdächtig werten, "
                f"wenn die Beschreibung passt):\n{visitor_lines}"
            )

    repeat_minutes = int(
        coordinator.options.get(CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES, 30) or 0
    )
    if repeat_minutes > 0:
        recent = ai_alert_store.recent_alerts(
            coordinator, cam_id, minutes=repeat_minutes
        )
        if recent:
            parts.append(
                f"Hinweis: in den letzten {repeat_minutes} Minuten gab es "
                f"bereits {len(recent)} Alarm(e) für diese Kamera — werte "
                "fortlaufende, bereits bekannte Aktivität nicht automatisch "
                "höher als beim ersten Mal."
            )

    return "\n\n".join(parts)


def _known_visitors(coordinator: BoschCameraCoordinator) -> list[tuple[str, str]]:
    """Read known-visitor subentries off the config entry. Returns
    [(name, description), ...]. Pure/testable: takes only what it needs
    off `coordinator.config_entry`, never raises on a malformed subentry."""
    entry = getattr(coordinator, "config_entry", None)
    if entry is None:
        return []
    out: list[tuple[str, str]] = []
    for subentry in getattr(entry, "subentries", {}).values():
        if getattr(subentry, "subentry_type", None) != "ai_visitor":
            continue
        data = subentry.data
        name = str(data.get("visitor_name", "")).strip()
        desc = str(data.get("visitor_description", "")).strip()
        if name or desc:
            out.append((name, desc))
    return out


def _parse_json_fallback(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction for an AI-Task provider that doesn't
    honor the `structure` param and returns free text instead.

    Security note: the extracted dict is camera-scene-derived text (an
    AI-Task response, ultimately influenced by whatever is visible to the
    camera) and flows into a user-configured `domain.service` call's
    `data` payload (`ai_alert_routing.py`). Unlike the `structure`-enforced
    path, free-text JSON has no schema enforcement, so a crafted scene
    (e.g. a sign held up to the camera) could inject arbitrary extra keys
    into that payload. Only pass through keys `STRUCTURE_SCHEMA` actually
    declares — everything else is dropped before it ever reaches a
    service-call payload.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group())
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or "score" not in parsed:
        return None
    return {k: v for k, v in parsed.items() if k in STRUCTURE_SCHEMA}


# ── Main entry point ─────────────────────────────────────────────────────────


async def async_generate_ai_analysis(
    coordinator: BoschCameraCoordinator, cam_id: str, *, force: bool = False
) -> dict[str, Any] | None:
    """Run a structured suspicion-scoring AI-Task analysis for one camera.

    Mirrors `BoschCameraCoordinator.async_generate_ai_description`'s
    gating/call/error-handling shape but requests STRUCTURED output via
    `ai_task.generate_data`'s `structure` param instead of free text, and
    gates on its own separate cooldown/budget (module docstring). Never
    raises — every failure path returns None so a broken/rate-limited AI
    provider can never break the motion-event pipeline. `force=True`
    (manual `analyze_camera_ai` service call) bypasses cooldown/window but
    still counts toward the daily budget, matching the sibling feature's
    manual-service convention.

    A "nothing notable" result (score <= 0 after clamping, or the call
    failed/was rate-limited) returns None WITHOUT persisting an alert,
    updating entities, or firing an event — mirrors AICameraCentre's own
    documented "no obvious motion" drop.
    """
    if not coordinator.options.get(CONF_AI_ANALYSIS_ENABLED, False):
        return None
    if not per_camera_analysis_enabled(coordinator, cam_id):
        return None
    if coordinator.shc_state_cache.get(cam_id, {}).get("privacy_mode"):
        return None
    if not force and not coordinator._ai_window_allowed():
        return None
    if not force and not _ai_analysis_rate_allowed(coordinator, cam_id):
        return None

    cam_entity = getattr(coordinator, "camera_entities", {}).get(cam_id)
    if cam_entity is None:
        return None
    entity_id = cam_entity.entity_id

    opts = coordinator.options
    ai_task_entity = (
        opts.get(CONF_AI_ANALYSIS_TASK_ENTITY) or opts.get("ai_task_entity") or ""
    ).strip()
    snapshot_count = max(1, int(opts.get(CONF_AI_ANALYSIS_SNAPSHOT_COUNT, 3) or 1))
    # A single media-source attachment per requested frame — the media
    # source resolves to a FRESH snapshot each time it's attached (same
    # path the sibling describe feature already uses for its one
    # attachment), so N identical attachment entries give a real N-frame
    # burst without a bespoke capture mechanism.
    attachments = [
        {
            "media_content_id": f"media-source://camera/{entity_id}",
            "media_content_type": "image/jpeg",
        }
        for _ in range(snapshot_count)
    ]

    ai_call_data: dict[str, Any] = {
        "task_name": "Bosch camera AI analysis",
        "instructions": _build_prompt(coordinator, cam_id),
        "attachments": attachments,
        "structure": STRUCTURE_SCHEMA,
    }
    if ai_task_entity:
        ai_call_data["entity_id"] = ai_task_entity

    coordinator.ai_analysis_in_flight += 1
    result: dict[str, Any] | None = None
    try:
        async with asyncio.timeout(TIMEOUT_AI_ANALYSIS_CALL):
            resp = await coordinator.hass.services.async_call(
                "ai_task",
                "generate_data",
                ai_call_data,
                blocking=True,
                return_response=True,
            )
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict) and "score" in data:
            result = data
        elif isinstance(data, str) and data.strip():
            result = _parse_json_fallback(data)
        if result is not None:
            ai_analysis_record_call(coordinator, cam_id)
    except TimeoutError:
        _LOGGER.debug(
            "AI camera analysis timed out (%.0fs) for %s",
            TIMEOUT_AI_ANALYSIS_CALL,
            cam_id[:8],
        )
    except Exception as err:
        _LOGGER.debug("AI camera analysis failed for %s: %s", cam_id[:8], err)
    finally:
        coordinator.ai_analysis_in_flight -= 1

    if result is None:
        return None

    try:
        score = int(float(result.get("score", 0)))
    except (TypeError, ValueError, OverflowError):
        # OverflowError: json.loads accepts the JSON extensions Infinity/
        # -Infinity as valid floats (NaN is already a ValueError on int()),
        # so a misbehaving provider returning "score": Infinity in its
        # free-text JSON-fallback response must not crash this function.
        score = 0
    result = dict(result)
    result["score"] = max(0, min(10, score))

    if result["score"] <= 0:
        return None

    try:
        await _finalize_alert(coordinator, cam_id, entity_id, result)
    except Exception as err:
        # Module contract (see this function's docstring): "Never raises".
        # A storage/routing failure downstream of a successful AI call must
        # degrade to a logged warning, not propagate into the motion-event
        # listener / manual-service caller as an unhandled exception.
        _LOGGER.warning(
            "AI camera analysis: alert finalize/routing failed for %s "
            "(analysis itself succeeded, score=%d): %s",
            cam_id[:8],
            result["score"],
            err,
        )
    return result


async def _finalize_alert(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    entity_id: str,
    result: dict[str, Any],
) -> None:
    """Persist, update entities, fire the event, route notify/Alarmo.

    Split out of `async_generate_ai_analysis` so tests can exercise the
    AI-call half and the finalize/routing half independently (matches this
    file's own module-level-function-per-concern style).
    """
    generated_at = datetime.now(UTC).isoformat()
    image_bytes = await coordinator.async_fetch_live_snapshot(cam_id)
    stored = await ai_alert_store.async_store_alert(
        coordinator, cam_id, result, generated_at, image_bytes
    )

    if cam_id in coordinator.data:
        coordinator.data[cam_id]["ai_analysis"] = {
            **result,
            "generated_at": generated_at,
            "image_path": stored.get("image_path") if stored else None,
        }
        coordinator.async_set_updated_data(coordinator.data)

    coordinator.hass.bus.async_fire(
        "bosch_shc_camera_ai_alert",
        {
            "camera_id": cam_id,
            "entity_id": entity_id,
            "generated_at": generated_at,
            **result,
        },
    )

    from . import ai_alert_routing  # local import: avoids a hard import-time

    # cycle with coordinator.py (ai_alert_routing also type-checks against
    # BoschCameraCoordinator via TYPE_CHECKING only).
    await ai_alert_routing.async_route_alert(coordinator, cam_id, result)
