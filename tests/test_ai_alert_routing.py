"""Tests for ai_alert_routing.py — AI Camera Analysis multi-target notify
routing + generalized alarm/siren trigger.

Consolidated single flat test module (platinum convention: one
tests/test_<module>.py per source module) covering every test surface of
``custom_components/bosch_shc_camera/ai_alert_routing.py``:

  1. ``_alert_targets`` — reads ``ai_target`` subentries off the config
     entry, ignoring any other subentry type, never raising on a missing
     ``config_entry``/empty ``subentries``.

  2. ``_panel_state`` — resolves the configured alarm-panel entity's state,
     ``None`` when unconfigured or the entity is missing from
     ``hass.states``.

  3. ``_condition_matches`` — the condition-matrix core (PIN_EVERY_MODE):
     every ``condition`` value against every relevant ``panel_state``,
     exhaustively, one assertion per combination rather than a loop that
     could hide an individual failure.

  4. ``_dispatch`` — the raw ``domain.service`` call wrapper: malformed
     service string, valid call, and swallowed exception.

  5. ``async_route_alert`` — the main orchestrator: per-target score/camera-
     filter/condition/empty-service gating, multi-target dispatch, and the
     Alarmo/siren trigger's fail-closed gating (this is safety-critical —
     every gating dimension is tested independently).

SENTINEL_RULE: no ``time.monotonic()`` usage in the module under test, so
no sentinel-default concern here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera import ai_alert_routing
from custom_components.bosch_shc_camera.const import (
    CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY,
    CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE,
    CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE,
    CONF_AI_ANALYSIS_ALARMO_ENABLED,
    CONF_AI_TARGET_CAMERA_FILTER,
    CONF_AI_TARGET_CONDITION,
    CONF_AI_TARGET_MIN_SCORE,
    CONF_AI_TARGET_NOTIFY_SERVICE,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_ENTITY_ID = "camera.bosch_terrasse"
PANEL_ENTITY = "alarm_control_panel.home"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_subentry(subentry_type: str, data: dict) -> SimpleNamespace:
    return SimpleNamespace(subentry_type=subentry_type, data=data)


def _make_entry(*subentries: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(subentries={str(i): s for i, s in enumerate(subentries)})


def _make_coord(
    *,
    config_entry: SimpleNamespace | None = "unset",
    options: dict | None = None,
    camera_entities: dict | None = None,
    states: dict | None = None,
) -> SimpleNamespace:
    """Build a SimpleNamespace coordinator stub matching this repo's
    house style (options is a plain dict, not a MagicMock — see
    tests/test_recorder.py / tests/test_switch.py conventions).

    config_entry="unset" (sentinel, default) means the attribute is never
    set on the stub at all, exercising the real `getattr(..., None)`
    fallback in `_alert_targets` rather than an explicit None.
    """
    states = states or {}
    hass = SimpleNamespace(
        states=SimpleNamespace(get=MagicMock(side_effect=lambda eid: states.get(eid))),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    coord = SimpleNamespace(
        hass=hass,
        options=options if options is not None else {},
        camera_entities=camera_entities if camera_entities is not None else {},
    )
    if config_entry != "unset":
        coord.config_entry = config_entry
    return coord


# ---------------------------------------------------------------------------
# _alert_targets
# ---------------------------------------------------------------------------


def test_alert_targets_no_config_entry_attr():
    coord = _make_coord()  # no .config_entry set at all
    assert ai_alert_routing._alert_targets(coord) == []


def test_alert_targets_empty_subentries():
    coord = _make_coord(config_entry=_make_entry())
    assert ai_alert_routing._alert_targets(coord) == []


def test_alert_targets_mixed_subentry_types_only_ai_target_counted():
    target = _make_subentry("ai_target", {"notify_service": "notify.mobile"})
    visitor = _make_subentry("ai_visitor", {"something": "else"})
    scope = _make_subentry("ai_camera_scope", {"cams": [CAM_ID]})
    coord = _make_coord(config_entry=_make_entry(target, visitor, scope))

    result = ai_alert_routing._alert_targets(coord)

    assert result == [{"notify_service": "notify.mobile"}]


def test_alert_targets_multiple_ai_target_all_returned():
    t1 = _make_subentry("ai_target", {"notify_service": "notify.a"})
    t2 = _make_subentry("ai_target", {"notify_service": "notify.b"})
    t3 = _make_subentry("ai_target", {"notify_service": "notify.c"})
    coord = _make_coord(config_entry=_make_entry(t1, t2, t3))

    result = ai_alert_routing._alert_targets(coord)

    assert result == [
        {"notify_service": "notify.a"},
        {"notify_service": "notify.b"},
        {"notify_service": "notify.c"},
    ]


def test_alert_targets_malformed_subentry_data_skipped_not_raised():
    """A subentry whose `.data` isn't dict()-convertible must be skipped
    (logged at debug) rather than raising past the docstring's "never
    raises on a malformed subentry" contract."""
    bad = _make_subentry("ai_target", 42)  # int — dict(42) raises TypeError
    good = _make_subentry("ai_target", {"notify_service": "notify.a"})
    coord = _make_coord(config_entry=_make_entry(bad, good))

    result = ai_alert_routing._alert_targets(coord)

    assert result == [{"notify_service": "notify.a"}]


# ---------------------------------------------------------------------------
# _panel_state
# ---------------------------------------------------------------------------


def test_panel_state_no_entity_configured():
    coord = _make_coord(options={CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: ""})
    assert ai_alert_routing._panel_state(coord) is None


def test_panel_state_not_configured_at_all():
    coord = _make_coord(options={})
    assert ai_alert_routing._panel_state(coord) is None


def test_panel_state_entity_configured_but_missing_from_states():
    coord = _make_coord(
        options={CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY},
        states={},
    )
    assert ai_alert_routing._panel_state(coord) is None


def test_panel_state_entity_found_returns_state():
    coord = _make_coord(
        options={CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY},
        states={PANEL_ENTITY: SimpleNamespace(state="armed_away")},
    )
    assert ai_alert_routing._panel_state(coord) == "armed_away"


# ---------------------------------------------------------------------------
# _condition_matches — exhaustive matrix (PIN_EVERY_MODE)
# ---------------------------------------------------------------------------


def test_condition_always_with_none_panel_state():
    assert ai_alert_routing._condition_matches("always", None) is True


def test_condition_always_with_any_panel_state():
    assert ai_alert_routing._condition_matches("always", "disarmed") is True
    assert ai_alert_routing._condition_matches("always", "armed_away") is True
    assert ai_alert_routing._condition_matches("always", "unknown") is True


def test_condition_armed_with_none_panel_state_fails_closed():
    assert ai_alert_routing._condition_matches("armed", None) is False


def test_condition_armed_with_unknown_panel_state():
    assert ai_alert_routing._condition_matches("armed", "unknown") is False


def test_condition_armed_with_unavailable_panel_state():
    assert ai_alert_routing._condition_matches("armed", "unavailable") is False


@pytest.mark.parametrize(
    "armed_state",
    sorted(ai_alert_routing._ARMED_STATES),
)
def test_condition_armed_matches_every_armed_state(armed_state):
    assert ai_alert_routing._condition_matches("armed", armed_state) is True


def test_condition_armed_with_disarmed_panel_state():
    assert ai_alert_routing._condition_matches("armed", "disarmed") is False


def test_condition_away_with_armed_away():
    assert ai_alert_routing._condition_matches("away", "armed_away") is True


def test_condition_away_with_armed_vacation():
    assert ai_alert_routing._condition_matches("away", "armed_vacation") is True


def test_condition_away_with_not_home():
    assert ai_alert_routing._condition_matches("away", "not_home") is True


def test_condition_away_with_armed_home():
    assert ai_alert_routing._condition_matches("away", "armed_home") is False


def test_condition_away_with_disarmed():
    assert ai_alert_routing._condition_matches("away", "disarmed") is False


def test_condition_away_or_armed_with_armed_home():
    # armed but not away -> still matches
    assert ai_alert_routing._condition_matches("away_or_armed", "armed_home") is True


def test_condition_away_or_armed_with_not_home():
    # away (not_home) but not armed -> still matches
    assert ai_alert_routing._condition_matches("away_or_armed", "not_home") is True


def test_condition_away_or_armed_with_armed_away():
    # both armed and away -> matches
    assert ai_alert_routing._condition_matches("away_or_armed", "armed_away") is True


def test_condition_away_or_armed_with_disarmed():
    assert ai_alert_routing._condition_matches("away_or_armed", "disarmed") is False


def test_condition_unknown_value_fails_closed_not_exception():
    assert (
        ai_alert_routing._condition_matches("some_garbage_value", "armed_away") is False
    )
    assert ai_alert_routing._condition_matches("some_garbage_value", None) is False


# ---------------------------------------------------------------------------
# _dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_malformed_service_string_logs_and_skips():
    coord = _make_coord()

    await ai_alert_routing._dispatch(coord, "not_a_domain_service", {"a": 1})

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_valid_service_invoked_with_correct_args():
    coord = _make_coord()
    data = {"message": "hi", "title": "t"}

    await ai_alert_routing._dispatch(coord, "notify.mobile_app", data)

    coord.hass.services.async_call.assert_awaited_once_with(
        "notify", "mobile_app", data
    )


@pytest.mark.asyncio
async def test_dispatch_swallows_service_call_exception():
    coord = _make_coord()
    coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))

    # must not raise
    await ai_alert_routing._dispatch(coord, "notify.mobile_app", {"x": 1})

    coord.hass.services.async_call.assert_awaited_once()


# ---------------------------------------------------------------------------
# async_route_alert — main orchestrator
# ---------------------------------------------------------------------------


def _result(score: int = 5, short: str = "Person detected") -> dict:
    return {"score": score, "short": short}


@pytest.mark.asyncio
async def test_route_alert_no_targets_no_dispatch_no_crash():
    coord = _make_coord(config_entry=_make_entry(), options={})

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result())

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_target_min_score_above_alert_score_skipped():
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_MIN_SCORE: 8,
        },
    )
    coord = _make_coord(config_entry=_make_entry(target), options={})

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=5))

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_target_non_numeric_min_score_falls_back_to_one():
    """A malformed `min_score` (non-numeric) must degrade to the documented
    default of 1, not raise — so a low real alert score (e.g. 1) is still
    dispatched rather than being silently blocked by a config typo."""
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_MIN_SCORE: "not-a-number",
        },
    )
    coord = _make_coord(config_entry=_make_entry(target), options={})

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=1))

    coord.hass.services.async_call.assert_called_once()


@pytest.mark.asyncio
async def test_route_alert_target_min_score_at_threshold_included():
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_MIN_SCORE: 5,
        },
    )
    coord = _make_coord(config_entry=_make_entry(target), options={})

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=5))

    coord.hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_alert_target_min_score_above_alert_score_included():
    # score exceeds min_score -> included
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_MIN_SCORE: 3,
        },
    )
    coord = _make_coord(config_entry=_make_entry(target), options={})

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=9))

    coord.hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_alert_empty_camera_filter_matches_any_camera():
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_CAMERA_FILTER: [],
        },
    )
    coord = _make_coord(config_entry=_make_entry(target), options={})

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result())

    coord.hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_alert_camera_filter_matches_entity_id():
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_CAMERA_FILTER: [CAM_ENTITY_ID],
        },
    )
    coord = _make_coord(
        config_entry=_make_entry(target),
        options={},
        camera_entities={CAM_ID: SimpleNamespace(entity_id=CAM_ENTITY_ID)},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result())

    coord.hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_alert_camera_filter_matches_cam_id_dual_check():
    # camera_filter lists the raw cam_id (not the resolved entity_id) --
    # the code explicitly checks `cam_id not in camera_filter` too, so this
    # must also match. Verifies that dual-check is real, not dead code.
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_CAMERA_FILTER: [CAM_ID],
        },
    )
    coord = _make_coord(
        config_entry=_make_entry(target),
        options={},
        camera_entities={CAM_ID: SimpleNamespace(entity_id=CAM_ENTITY_ID)},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result())

    coord.hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_alert_camera_filter_matches_neither_skipped():
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_CAMERA_FILTER: ["camera.some_other_cam"],
        },
    )
    coord = _make_coord(
        config_entry=_make_entry(target),
        options={},
        camera_entities={CAM_ID: SimpleNamespace(entity_id=CAM_ENTITY_ID)},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result())

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_condition_mismatch_target_skipped():
    target = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile",
            CONF_AI_TARGET_CONDITION: "armed",
        },
    )
    coord = _make_coord(
        config_entry=_make_entry(target),
        options={},  # no panel configured -> panel_state None -> fail closed
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result())

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_empty_notify_service_skipped_without_dispatch():
    target = _make_subentry(
        "ai_target",
        {CONF_AI_TARGET_NOTIFY_SERVICE: ""},
    )
    coord = _make_coord(config_entry=_make_entry(target), options={})

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result())

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_multiple_targets_only_matching_dispatched():
    matching_low_score = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.low_score_ok",
            CONF_AI_TARGET_MIN_SCORE: 1,
        },
    )
    non_matching_high_score = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.too_high",
            CONF_AI_TARGET_MIN_SCORE: 9,
        },
    )
    non_matching_camera = _make_subentry(
        "ai_target",
        {
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.wrong_cam",
            CONF_AI_TARGET_CAMERA_FILTER: ["camera.unrelated"],
        },
    )
    matching_empty_filter = _make_subentry(
        "ai_target",
        {CONF_AI_TARGET_NOTIFY_SERVICE: "notify.also_matches"},
    )
    coord = _make_coord(
        config_entry=_make_entry(
            matching_low_score,
            non_matching_high_score,
            non_matching_camera,
            matching_empty_filter,
        ),
        options={},
        camera_entities={CAM_ID: SimpleNamespace(entity_id=CAM_ENTITY_ID)},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=5))

    called_services = [
        call.args[:2] for call in coord.hass.services.async_call.call_args_list
    ]
    assert called_services == [
        ("notify", "low_score_ok"),
        ("notify", "also_matches"),
    ]


# --- Alarmo/siren trigger -------------------------------------------------


@pytest.mark.asyncio
async def test_route_alert_alarmo_disabled_never_dispatched():
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: False,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 1,
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="armed_away")},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=10))

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_alarmo_enabled_empty_trigger_service_not_dispatched():
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "",
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="armed_away")},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=10))

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_alarmo_score_below_threshold_not_dispatched():
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 8,
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="armed_away")},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=7))

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_alarmo_score_at_threshold_but_panel_none_fail_closed():
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 7,
            # no panel entity configured -> panel_state is None
        },
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=7))

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_alarmo_score_at_threshold_panel_unknown_fail_closed():
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 7,
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="unknown")},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=7))

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_alarmo_score_at_threshold_panel_disarmed_fail_closed():
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 7,
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="disarmed")},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=7))

    coord.hass.services.async_call.assert_not_called()


@pytest.mark.asyncio
async def test_route_alert_alarmo_score_at_threshold_panel_armed_dispatched():
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 7,
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="armed_away")},
        camera_entities={CAM_ID: SimpleNamespace(entity_id=CAM_ENTITY_ID)},
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=8))

    coord.hass.services.async_call.assert_awaited_once_with(
        "siren",
        "turn_on",
        {"camera": CAM_ENTITY_ID, "score": 8, "message": "Person detected"},
    )


@pytest.mark.asyncio
async def test_route_alert_alarmo_non_numeric_trigger_score_falls_back_to_seven():
    """A malformed `ai_analysis_alarm_trigger_score` option must degrade to
    the documented default of 7, not raise."""
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: "not-a-number",
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="armed_away")},
        camera_entities={CAM_ID: SimpleNamespace(entity_id=CAM_ENTITY_ID)},
    )

    # score=8 >= the fallback default of 7 -> still dispatched.
    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=8))

    coord.hass.services.async_call.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_alert_unknown_camera_falls_back_to_cam_id():
    # camera_entities has no entry for cam_id -> entity_id falls back to
    # cam_id itself (per async_route_alert's `getattr(...).get(cam_id)`
    # -> None -> `entity_id = cam_id`), must not raise.
    coord = _make_coord(
        config_entry=_make_entry(),
        options={
            CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "siren.turn_on",
            CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 5,
            CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: PANEL_ENTITY,
        },
        states={PANEL_ENTITY: SimpleNamespace(state="armed_home")},
        camera_entities={},  # cam_id NOT present
    )

    await ai_alert_routing.async_route_alert(coord, CAM_ID, _result(score=5))

    coord.hass.services.async_call.assert_awaited_once_with(
        "siren",
        "turn_on",
        {"camera": CAM_ID, "score": 5, "message": "Person detected"},
    )
