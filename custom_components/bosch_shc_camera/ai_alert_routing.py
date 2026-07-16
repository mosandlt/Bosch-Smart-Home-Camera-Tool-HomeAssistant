"""AI Camera Analysis — multi-target notify routing + generalized alarm trigger.

Reads alert-target subentries off the config entry (``subentry_type ==
"ai_target"``, see `const.py`'s ``CONF_AI_TARGET_*`` field names) — each
with its own score threshold, camera filter, and armed/away condition — and
dispatches to `notify_service` the same way `fcm.py`'s
`get_alert_services`/`_notify_type` already does (``svc.split(".", 1)`` →
``hass.services.async_call``), so this reuses the exact, already-proven
dispatch mechanic rather than inventing a new one.

The optional Alarmo/siren trigger is deliberately NOT an Alarmo-specific
API call (its exact service semantics weren't verified against source) —
it's a configurable ``domain.service`` string, called via the same dispatch
mechanic, gated on score + the configured ``alarm_control_panel.*``
entity's armed state. Works with Alarmo or any other alarm integration.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .const import (
    CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY,
    CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE,
    CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE,
    CONF_AI_ANALYSIS_ALARMO_ENABLED,
    CONF_AI_TARGET_CAMERA_FILTER,
    CONF_AI_TARGET_CONDITION,
    CONF_AI_TARGET_MIN_SCORE,
    CONF_AI_TARGET_NOTIFY_SERVICE,
)

if TYPE_CHECKING:
    from .coordinator import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

# Armed-ish states across common alarm_control_panel integrations (core HA
# states + Alarmo's own, which reuses the core STATE_ALARM_ARMED_* set).
_ARMED_STATES = frozenset(
    {
        "armed_home",
        "armed_away",
        "armed_night",
        "armed_vacation",
        "armed_custom_bypass",
    }
)
_AWAY_STATES = frozenset({"armed_away", "armed_vacation", "not_home"})


def _alert_targets(coordinator: BoschCameraCoordinator) -> list[dict[str, Any]]:
    """Read alert-target subentries off the config entry. Never raises on a
    malformed subentry — skips it and logs at debug."""
    entry = getattr(coordinator, "config_entry", None)
    if entry is None:
        return []
    out: list[dict[str, Any]] = []
    for subentry in getattr(entry, "subentries", {}).values():
        if getattr(subentry, "subentry_type", None) != "ai_target":
            continue
        try:
            out.append(dict(subentry.data))
        except (TypeError, ValueError):
            _LOGGER.debug("AI alert routing: malformed ai_target subentry skipped")
            continue
    return out


def _panel_state(coordinator: BoschCameraCoordinator) -> str | None:
    panel_entity = (
        coordinator.options.get(CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY) or ""
    ).strip()
    if not panel_entity:
        return None
    state_obj = coordinator.hass.states.get(panel_entity)
    return state_obj.state if state_obj is not None else None


def _condition_matches(condition: str, panel_state: str | None) -> bool:
    """Evaluate a target's `condition` field against the alarm panel state.

    "always" never needs the panel. Any condition OTHER than "always" with
    no panel configured (panel_state is None) is treated as NOT matching
    (fail-closed — a target scoped to "armed" must not fire when there's no
    way to know whether the system is armed)."""
    if condition == "always":
        return True
    if panel_state is None or panel_state in ("unknown", "unavailable"):
        return False
    if condition == "armed":
        return panel_state in _ARMED_STATES
    if condition == "away":
        return panel_state in _AWAY_STATES
    if condition == "away_or_armed":
        return panel_state in _ARMED_STATES or panel_state in _AWAY_STATES
    return False  # unknown condition value — fail closed, don't guess


async def _dispatch(
    coordinator: BoschCameraCoordinator, svc: str, data: dict[str, Any]
) -> None:
    """Call a raw `domain.service` string with `data`, matching `fcm.py`'s
    existing `svc.split(".", 1)` → `hass.services.async_call` dispatch."""
    try:
        domain, service = svc.split(".", 1)
    except ValueError:
        _LOGGER.warning("AI alert routing: malformed service string %r — skipping", svc)
        return
    try:
        await coordinator.hass.services.async_call(domain, service, data)
    except Exception as err:
        _LOGGER.warning("AI alert routing: service call %s failed: %s", svc, err)


async def async_route_alert(
    coordinator: BoschCameraCoordinator, cam_id: str, result: dict[str, Any]
) -> None:
    """Route one finalized AI-analysis alert to every matching target's
    notify service, then (if configured) trigger the generalized
    alarm/siren service. Never raises — a broken target config must not
    break the analysis pipeline (each target/step is independently
    try/excepted via `_dispatch`)."""
    score = int(result.get("score", 0))
    cam_entity = getattr(coordinator, "camera_entities", {}).get(cam_id)
    entity_id = cam_entity.entity_id if cam_entity is not None else cam_id
    panel_state = _panel_state(coordinator)
    message = str(result.get("short") or result.get("detail") or "AI camera alert")

    for target in _alert_targets(coordinator):
        try:
            min_score = int(target.get(CONF_AI_TARGET_MIN_SCORE, 1) or 1)
        except (TypeError, ValueError):
            min_score = 1
        if score < min_score:
            continue
        camera_filter = target.get(CONF_AI_TARGET_CAMERA_FILTER) or []
        if (
            camera_filter
            and entity_id not in camera_filter
            and cam_id not in camera_filter
        ):
            continue
        condition = str(target.get(CONF_AI_TARGET_CONDITION, "always") or "always")
        if not _condition_matches(condition, panel_state):
            continue
        svc = str(target.get(CONF_AI_TARGET_NOTIFY_SERVICE, "") or "").strip()
        if not svc:
            continue
        await _dispatch(
            coordinator,
            svc,
            {
                "message": message,
                "title": f"AI Kamera-Alarm ({score}/10)",
                "data": {"camera": entity_id, **result},
            },
        )

    if not coordinator.options.get(CONF_AI_ANALYSIS_ALARMO_ENABLED, False):
        return
    trigger_svc = (
        coordinator.options.get(CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE) or ""
    ).strip()
    if not trigger_svc:
        return
    try:
        trigger_score = int(
            coordinator.options.get(CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE, 7) or 7
        )
    except (TypeError, ValueError):
        trigger_score = 7
    if score < trigger_score:
        return
    if panel_state not in _ARMED_STATES:
        return  # fail-closed: no panel / not armed / unknown → never trigger
    await _dispatch(
        coordinator,
        trigger_svc,
        {"camera": entity_id, "score": score, "message": message},
    )
