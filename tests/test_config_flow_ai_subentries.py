"""Tests for the AI Camera Analysis config-flow additions in
`custom_components/bosch_shc_camera/config_flow.py`:

  - the ``ai_analysis`` OptionsFlow section (12 fields, 2 nullable
    EntitySelector fields — issue #35 nullable-selector contract)
  - the 2 ``ConfigSubentryFlow`` handlers (``ai_target``, ``ai_visitor``)
    driven through the real HA subentry flow-manager
    (``hass.config_entries.subentries``). NOTE: an earlier draft also had a
    3rd ``ai_camera_scope`` subentry type duplicating
    ``switch.<cam>_ai_analysis`` with no backend consumer — removed before
    release (see const.py's note next to the known-visitor field names).
  - the backend read-back contract: ``ai_alert_routing._alert_targets`` and
    ``ai_analysis._known_visitors`` must read subentry ``.data`` using the
    exact keys the flow schemas write.

Mirrors the house style already established in ``tests/test_config_flow.py``
(``_make_entry``/``_get_section_schema``/``_submit`` helpers, real-hass
harness for flow-manager-driven tests).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.bosch_shc_camera import ai_alert_routing, ai_analysis
from custom_components.bosch_shc_camera.config_flow import (
    AiTargetSubentryFlowHandler,
    AiVisitorSubentryFlowHandler,
    BoschCameraConfigFlow,
    BoschCameraOptionsFlow,
)
from custom_components.bosch_shc_camera.const import (
    AI_TARGET_CONDITIONS,
    CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY,
    CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE,
    CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE,
    CONF_AI_ANALYSIS_ALARMO_ENABLED,
    CONF_AI_ANALYSIS_COOLDOWN_SECONDS,
    CONF_AI_ANALYSIS_ENABLED,
    CONF_AI_ANALYSIS_MAX_PER_DAY,
    CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES,
    CONF_AI_ANALYSIS_RETENTION_DAYS,
    CONF_AI_ANALYSIS_SNAPSHOT_COUNT,
    CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS,
    CONF_AI_ANALYSIS_TASK_ENTITY,
    CONF_AI_TARGET_CAMERA_FILTER,
    CONF_AI_TARGET_CONDITION,
    CONF_AI_TARGET_MIN_SCORE,
    CONF_AI_TARGET_NAME,
    CONF_AI_TARGET_NOTIFY_SERVICE,
    CONF_AI_VISITOR_DESCRIPTION,
    CONF_AI_VISITOR_NAME,
    DEFAULT_OPTIONS,
    DOMAIN,
)

# Re-use the exact helpers already validated in tests/test_config_flow.py so
# these new tests follow the identical house pattern.
from tests.test_config_flow import _get_section_schema, _make_entry, _submit

DOMAIN_CONST = DOMAIN  # local alias, avoids shadowing import in fixtures


# ─────────────────────────────────────────────────────────────────────────────
# ai_analysis OptionsFlow section
# ─────────────────────────────────────────────────────────────────────────────


class TestAiAnalysisSectionLayout:
    def test_ai_analysis_section_has_all_12_fields(self) -> None:
        from custom_components.bosch_shc_camera.config_flow import OPTIONS_SECTIONS

        assert OPTIONS_SECTIONS["ai_analysis"] == [
            "ai_analysis_enabled",
            "ai_analysis_task_entity",
            "ai_analysis_snapshot_count",
            "ai_analysis_snapshot_interval_ms",
            "ai_analysis_cooldown_seconds",
            "ai_analysis_max_per_day",
            "ai_analysis_retention_days",
            "ai_analysis_repeat_context_minutes",
            "ai_analysis_alarm_panel_entity",
            "ai_analysis_alarmo_enabled",
            "ai_analysis_alarm_trigger_service",
            "ai_analysis_alarm_trigger_score",
        ]

    def test_default_options_has_matching_defaults(self) -> None:
        """DEFAULT_OPTIONS must define every field referenced by the section
        (pins the real defaults the schema falls back to)."""
        assert DEFAULT_OPTIONS["ai_analysis_enabled"] is False
        assert DEFAULT_OPTIONS["ai_analysis_snapshot_count"] == 3
        assert DEFAULT_OPTIONS["ai_analysis_snapshot_interval_ms"] == 800
        assert DEFAULT_OPTIONS["ai_analysis_cooldown_seconds"] == 30
        assert DEFAULT_OPTIONS["ai_analysis_max_per_day"] == 200
        assert DEFAULT_OPTIONS["ai_analysis_retention_days"] == 30
        assert DEFAULT_OPTIONS["ai_analysis_repeat_context_minutes"] == 30
        assert DEFAULT_OPTIONS["ai_analysis_alarmo_enabled"] is False
        assert DEFAULT_OPTIONS["ai_analysis_alarm_trigger_score"] == 7


class TestAiAnalysisSectionSchema:
    """Pins default rendering + validation bounds for each field."""

    def test_snapshot_count_default_and_bounds(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_SNAPSHOT_COUNT] == 3
        assert (
            schema({CONF_AI_ANALYSIS_SNAPSHOT_COUNT: 2})[
                CONF_AI_ANALYSIS_SNAPSHOT_COUNT
            ]
            == 2
        )
        assert (
            schema({CONF_AI_ANALYSIS_SNAPSHOT_COUNT: 10})[
                CONF_AI_ANALYSIS_SNAPSHOT_COUNT
            ]
            == 10
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_SNAPSHOT_COUNT: 1})
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_SNAPSHOT_COUNT: 11})

    def test_snapshot_interval_ms_bounds(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS] == 800
        assert (
            schema({CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS: 100})[
                CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS
            ]
            == 100
        )
        assert (
            schema({CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS: 5000})[
                CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS
            ]
            == 5000
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS: 50})
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS: 5001})

    def test_cooldown_seconds_bounds(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_COOLDOWN_SECONDS] == 30
        assert (
            schema({CONF_AI_ANALYSIS_COOLDOWN_SECONDS: 0})[
                CONF_AI_ANALYSIS_COOLDOWN_SECONDS
            ]
            == 0
        )
        assert (
            schema({CONF_AI_ANALYSIS_COOLDOWN_SECONDS: 3600})[
                CONF_AI_ANALYSIS_COOLDOWN_SECONDS
            ]
            == 3600
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_COOLDOWN_SECONDS: -1})
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_COOLDOWN_SECONDS: 3601})

    def test_max_per_day_zero_is_unlimited_and_no_upper_cap(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_MAX_PER_DAY] == 200
        assert (
            schema({CONF_AI_ANALYSIS_MAX_PER_DAY: 0})[CONF_AI_ANALYSIS_MAX_PER_DAY] == 0
        )
        assert (
            schema({CONF_AI_ANALYSIS_MAX_PER_DAY: 500000})[CONF_AI_ANALYSIS_MAX_PER_DAY]
            == 500000
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_MAX_PER_DAY: -1})

    def test_retention_days_bounds(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_RETENTION_DAYS] == 30
        assert (
            schema({CONF_AI_ANALYSIS_RETENTION_DAYS: 1})[
                CONF_AI_ANALYSIS_RETENTION_DAYS
            ]
            == 1
        )
        assert (
            schema({CONF_AI_ANALYSIS_RETENTION_DAYS: 90})[
                CONF_AI_ANALYSIS_RETENTION_DAYS
            ]
            == 90
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_RETENTION_DAYS: 0})
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_RETENTION_DAYS: 91})

    def test_repeat_context_minutes_bounds(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES] == 30
        assert (
            schema({CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES: 0})[
                CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES
            ]
            == 0
        )
        assert (
            schema({CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES: 120})[
                CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES
            ]
            == 120
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES: 121})

    def test_alarm_trigger_score_bounds(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE] == 7
        assert (
            schema({CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 1})[
                CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE
            ]
            == 1
        )
        assert (
            schema({CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 10})[
                CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE
            ]
            == 10
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 0})
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 11})

    def test_alarmo_enabled_default_false(self) -> None:
        schema = _get_section_schema("ai_analysis")
        assert schema({})[CONF_AI_ANALYSIS_ALARMO_ENABLED] is False

    def test_alarm_trigger_service_default_empty_string(self) -> None:
        # This field uses voluptuous' description={"suggested_value": ...} UI
        # pre-fill convention (not default=) — the same convention as the
        # sibling alert_notify_service field. That means the key is genuinely
        # ABSENT from the validated output when omitted (no default value is
        # injected by voluptuous); "suggested_value" only affects what the
        # frontend pre-fills into the form, not schema-validation behavior.
        schema = _get_section_schema("ai_analysis")
        assert CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE not in schema({})
        assert (
            schema({CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "notify.mobile_app"})[
                CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE
            ]
            == "notify.mobile_app"
        )


class TestAiAnalysisNullableEntitySelectors:
    """Regression coverage for issue #35's nullable-selector contract
    (``vol.Any(None, EntitySelector(...))``), mirrored exactly for the two
    new nullable fields in the ai_analysis section."""

    def test_ai_analysis_task_entity_none_allowed(self) -> None:
        """Clearing the ai_task entity picker submits None — must NOT raise."""
        schema = _get_section_schema("ai_analysis")
        result = schema({CONF_AI_ANALYSIS_TASK_ENTITY: None})
        assert result[CONF_AI_ANALYSIS_TASK_ENTITY] is None

    def test_ai_analysis_task_entity_valid_entity_id_allowed(self) -> None:
        schema = _get_section_schema("ai_analysis")
        result = schema({CONF_AI_ANALYSIS_TASK_ENTITY: "ai_task.my_llm"})
        assert result[CONF_AI_ANALYSIS_TASK_ENTITY] == "ai_task.my_llm"

    def test_ai_analysis_alarm_panel_entity_none_allowed(self) -> None:
        """Clearing the alarm-panel entity picker submits None — must NOT raise."""
        schema = _get_section_schema("ai_analysis")
        result = schema({CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: None})
        assert result[CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY] is None

    def test_ai_analysis_alarm_panel_entity_valid_entity_id_allowed(self) -> None:
        schema = _get_section_schema("ai_analysis")
        result = schema(
            {CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: "alarm_control_panel.home"}
        )
        assert result[CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY] == "alarm_control_panel.home"

    @pytest.mark.asyncio
    async def test_ai_analysis_entity_fields_cleared_saves_none(self) -> None:
        """Full round-trip through async_step_init: submitting None for both
        nullable pickers must NOT throw/500 and must persist None (matches
        the ai_task_entity regression test in tests/test_config_flow.py)."""
        flow = BoschCameraOptionsFlow(
            _make_entry(
                options={
                    CONF_AI_ANALYSIS_TASK_ENTITY: "ai_task.old_llm",
                    CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: "alarm_control_panel.old",
                }
            )
        )
        data = await _submit(
            flow,
            {
                "ai_analysis": {
                    CONF_AI_ANALYSIS_ENABLED: False,
                    CONF_AI_ANALYSIS_TASK_ENTITY: None,
                    CONF_AI_ANALYSIS_SNAPSHOT_COUNT: 3,
                    CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS: 800,
                    CONF_AI_ANALYSIS_COOLDOWN_SECONDS: 30,
                    CONF_AI_ANALYSIS_MAX_PER_DAY: 200,
                    CONF_AI_ANALYSIS_RETENTION_DAYS: 30,
                    CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES: 30,
                    CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: None,
                    CONF_AI_ANALYSIS_ALARMO_ENABLED: False,
                    CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "",
                    CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 7,
                }
            },
        )
        assert data[CONF_AI_ANALYSIS_TASK_ENTITY] is None
        assert data[CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY] is None

    @pytest.mark.asyncio
    async def test_ai_analysis_entity_fields_set_saves_values(self) -> None:
        """Round-trip with real entity ids set (opposite of the clear case)."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "ai_analysis": {
                    CONF_AI_ANALYSIS_ENABLED: True,
                    CONF_AI_ANALYSIS_TASK_ENTITY: "ai_task.new_llm",
                    CONF_AI_ANALYSIS_SNAPSHOT_COUNT: 5,
                    CONF_AI_ANALYSIS_SNAPSHOT_INTERVAL_MS: 1000,
                    CONF_AI_ANALYSIS_COOLDOWN_SECONDS: 60,
                    CONF_AI_ANALYSIS_MAX_PER_DAY: 50,
                    CONF_AI_ANALYSIS_RETENTION_DAYS: 7,
                    CONF_AI_ANALYSIS_REPEAT_CONTEXT_MINUTES: 15,
                    CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY: "alarm_control_panel.home",
                    CONF_AI_ANALYSIS_ALARMO_ENABLED: True,
                    CONF_AI_ANALYSIS_ALARM_TRIGGER_SERVICE: "alarmo.trigger",
                    CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE: 9,
                }
            },
        )
        assert data[CONF_AI_ANALYSIS_ENABLED] is True
        assert data[CONF_AI_ANALYSIS_TASK_ENTITY] == "ai_task.new_llm"
        assert data[CONF_AI_ANALYSIS_ALARM_PANEL_ENTITY] == "alarm_control_panel.home"
        assert data[CONF_AI_ANALYSIS_ALARMO_ENABLED] is True
        assert data[CONF_AI_ANALYSIS_ALARM_TRIGGER_SCORE] == 9


# ─────────────────────────────────────────────────────────────────────────────
# async_get_supported_subentry_types
# ─────────────────────────────────────────────────────────────────────────────


class TestSupportedSubentryTypes:
    def test_returns_exactly_the_two_ai_types(self) -> None:
        result = BoschCameraConfigFlow.async_get_supported_subentry_types(
            SimpleNamespace()
        )
        assert set(result.keys()) == {"ai_target", "ai_visitor"}
        assert result["ai_target"] is AiTargetSubentryFlowHandler
        assert result["ai_visitor"] is AiVisitorSubentryFlowHandler


# ─────────────────────────────────────────────────────────────────────────────
# Subentry flows — driven through the real HA subentry flow manager, exactly
# like tests/test_config_flow.py drives the top-level config flow via
# `hass.config_entries.flow`.
# ─────────────────────────────────────────────────────────────────────────────


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Bosch Smart Home Camera",
        data={"bearer_token": "tok", "refresh_token": "rtok"},
        options={},
        unique_id=DOMAIN,
        version=1,
    )
    entry.add_to_hass(hass)
    return entry


async def _start_subentry(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_type: str
):
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, subentry_type),
        context={"source": config_entries.SOURCE_USER},
    )


class TestAiTargetSubentryFlow:
    async def test_create_stores_all_fields_with_defaults(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_target")
        assert result["type"] == "form"

        result2 = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_TARGET_NAME: "Familie",
                CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile_app_phone",
            },
        )
        assert result2["type"] == "create_entry"
        subentry = next(iter(entry.subentries.values()))
        assert subentry.subentry_type == "ai_target"
        assert subentry.title == "Familie"
        assert subentry.data[CONF_AI_TARGET_NAME] == "Familie"
        assert subentry.data[CONF_AI_TARGET_NOTIFY_SERVICE] == "notify.mobile_app_phone"
        # Defaults applied by the schema.
        assert subentry.data[CONF_AI_TARGET_MIN_SCORE] == 5
        assert subentry.data[CONF_AI_TARGET_CONDITION] == "always"
        assert subentry.data[CONF_AI_TARGET_CAMERA_FILTER] == []
        # ai_target subentries are NOT camera-scoped by unique_id (multiple
        # named targets are the whole point) — unique_id is None.
        assert subentry.unique_id is None

    async def test_create_with_explicit_score_condition_and_filter(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_target")
        result2 = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_TARGET_NAME: "Security",
                CONF_AI_TARGET_NOTIFY_SERVICE: "notify.security_team",
                CONF_AI_TARGET_MIN_SCORE: 8,
                CONF_AI_TARGET_CONDITION: "armed",
                CONF_AI_TARGET_CAMERA_FILTER: ["camera.terrasse", "camera.garten"],
            },
        )
        assert result2["type"] == "create_entry"
        subentry = next(iter(entry.subentries.values()))
        assert subentry.data[CONF_AI_TARGET_MIN_SCORE] == 8
        assert subentry.data[CONF_AI_TARGET_CONDITION] == "armed"
        assert subentry.data[CONF_AI_TARGET_CAMERA_FILTER] == [
            "camera.terrasse",
            "camera.garten",
        ]

    def test_min_score_schema_bounds(self) -> None:
        from custom_components.bosch_shc_camera.config_flow import _ai_target_schema

        schema = vol.Schema(_ai_target_schema())
        base = {
            CONF_AI_TARGET_NAME: "x",
            CONF_AI_TARGET_NOTIFY_SERVICE: "notify.x",
        }
        assert (
            schema({**base, CONF_AI_TARGET_MIN_SCORE: 1})[CONF_AI_TARGET_MIN_SCORE] == 1
        )
        assert (
            schema({**base, CONF_AI_TARGET_MIN_SCORE: 10})[CONF_AI_TARGET_MIN_SCORE]
            == 10
        )
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({**base, CONF_AI_TARGET_MIN_SCORE: 0})
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({**base, CONF_AI_TARGET_MIN_SCORE: 11})

    def test_condition_options_match_ai_target_conditions_const(self) -> None:
        """The condition SelectSelector's options list must be built from
        AI_TARGET_CONDITIONS — not a hand-duplicated literal list that could
        drift out of sync with ai_alert_routing._condition_matches."""
        from custom_components.bosch_shc_camera.config_flow import _ai_target_schema

        schema_dict = _ai_target_schema()
        condition_key = next(
            k for k in schema_dict if str(k) == CONF_AI_TARGET_CONDITION
        )
        selector = schema_dict[condition_key]
        options = selector.config["options"]
        assert set(options) == set(AI_TARGET_CONDITIONS)

    def test_condition_rejects_value_outside_allowed_set(self) -> None:
        from custom_components.bosch_shc_camera.config_flow import _ai_target_schema

        schema = vol.Schema(_ai_target_schema())
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema(
                {
                    CONF_AI_TARGET_NAME: "x",
                    CONF_AI_TARGET_NOTIFY_SERVICE: "notify.x",
                    CONF_AI_TARGET_CONDITION: "not_a_real_condition",
                }
            )

    async def test_reconfigure_updates_data_without_growing_count(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        create = await _start_subentry(hass, entry, "ai_target")
        await hass.config_entries.subentries.async_configure(
            create["flow_id"],
            user_input={
                CONF_AI_TARGET_NAME: "Familie",
                CONF_AI_TARGET_NOTIFY_SERVICE: "notify.old",
            },
        )
        subentry_id = next(iter(entry.subentries))
        assert len(entry.subentries) == 1

        reconf = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "ai_target"),
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
                "subentry_id": subentry_id,
            },
        )
        assert reconf["type"] == "form"
        assert reconf["step_id"] == "reconfigure"

        result = await hass.config_entries.subentries.async_configure(
            reconf["flow_id"],
            user_input={
                CONF_AI_TARGET_NAME: "Familie",
                CONF_AI_TARGET_NOTIFY_SERVICE: "notify.new",
                CONF_AI_TARGET_MIN_SCORE: 9,
                CONF_AI_TARGET_CONDITION: "away",
                CONF_AI_TARGET_CAMERA_FILTER: [],
            },
        )
        assert result["type"] == "abort"
        assert result["reason"] == "reconfigure_successful"

        assert len(entry.subentries) == 1, "reconfigure must not grow the count"
        updated = entry.subentries[subentry_id]
        assert updated.data[CONF_AI_TARGET_NOTIFY_SERVICE] == "notify.new"
        assert updated.data[CONF_AI_TARGET_MIN_SCORE] == 9
        assert updated.title == "Familie"

    async def test_multiple_targets_allowed_no_dedup_guard(
        self, hass: HomeAssistant
    ) -> None:
        """Unlike ai_camera_scope, ai_target has no dedup guard by design —
        multiple named targets are the whole point. Pin that two targets
        with the SAME name are both accepted (no accidental rejection)."""
        entry = _entry(hass)
        for _ in range(2):
            result = await _start_subentry(hass, entry, "ai_target")
            result2 = await hass.config_entries.subentries.async_configure(
                result["flow_id"],
                user_input={
                    CONF_AI_TARGET_NAME: "Familie",
                    CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile_app_phone",
                },
            )
            assert result2["type"] == "create_entry"
        assert len(entry.subentries) == 2


class TestAiVisitorSubentryFlow:
    async def test_create_stores_name_and_description(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_visitor")
        assert result["type"] == "form"

        result2 = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_VISITOR_NAME: "Postbote",
                CONF_AI_VISITOR_DESCRIPTION: "Trägt gelbe DHL-Uniform, kommt werktags",
            },
        )
        assert result2["type"] == "create_entry"
        subentry = next(iter(entry.subentries.values()))
        assert subentry.subentry_type == "ai_visitor"
        assert subentry.title == "Postbote"
        assert subentry.data[CONF_AI_VISITOR_NAME] == "Postbote"
        assert (
            subentry.data[CONF_AI_VISITOR_DESCRIPTION]
            == "Trägt gelbe DHL-Uniform, kommt werktags"
        )

    async def test_empty_name_rejected_by_schema(self, hass: HomeAssistant) -> None:
        """CONF_AI_VISITOR_NAME is vol.Required(...) : str — voluptuous's
        bare `str` validator accepts an empty string (it only checks type,
        not truthiness). Pin the ACTUAL current behavior rather than assume:
        this documents that an empty name is schema-VALID today (a finding,
        not silently skipped)."""
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_visitor")
        result2 = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_VISITOR_NAME: "",
                CONF_AI_VISITOR_DESCRIPTION: "some description",
            },
        )
        # Documented finding: the schema currently has no vol.Length(min=1)
        # guard, so an empty name is accepted and creates a subentry with an
        # empty title. If this ever becomes unwanted, add
        # vol.All(str, vol.Length(min=1)) to _ai_visitor_schema().
        assert result2["type"] == "create_entry"
        subentry = next(iter(entry.subentries.values()))
        assert subentry.data[CONF_AI_VISITOR_NAME] == ""
        assert subentry.title == ""

    async def test_empty_description_rejected_by_schema(
        self, hass: HomeAssistant
    ) -> None:
        """Same finding as above but for CONF_AI_VISITOR_DESCRIPTION
        (TextSelector, also accepts empty string at the schema level)."""
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_visitor")
        result2 = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_VISITOR_NAME: "Nachbar",
                CONF_AI_VISITOR_DESCRIPTION: "",
            },
        )
        assert result2["type"] == "create_entry"
        subentry = next(iter(entry.subentries.values()))
        assert subentry.data[CONF_AI_VISITOR_DESCRIPTION] == ""

    async def test_reconfigure_updates_data_without_growing_count(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        create = await _start_subentry(hass, entry, "ai_visitor")
        await hass.config_entries.subentries.async_configure(
            create["flow_id"],
            user_input={
                CONF_AI_VISITOR_NAME: "Postbote",
                CONF_AI_VISITOR_DESCRIPTION: "old description",
            },
        )
        subentry_id = next(iter(entry.subentries))
        assert len(entry.subentries) == 1

        reconf = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "ai_visitor"),
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
                "subentry_id": subentry_id,
            },
        )
        assert reconf["type"] == "form"
        assert reconf["step_id"] == "reconfigure"

        result = await hass.config_entries.subentries.async_configure(
            reconf["flow_id"],
            user_input={
                CONF_AI_VISITOR_NAME: "Postbote",
                CONF_AI_VISITOR_DESCRIPTION: "new description",
            },
        )
        assert result["type"] == "abort"
        assert result["reason"] == "reconfigure_successful"

        assert len(entry.subentries) == 1, "reconfigure must not grow the count"
        updated = entry.subentries[subentry_id]
        assert updated.data[CONF_AI_VISITOR_DESCRIPTION] == "new description"


# ─────────────────────────────────────────────────────────────────────────────
# Backend contract verification — the flow writes subentry `.data` with
# CONF_AI_TARGET_*/CONF_AI_VISITOR_* keys; ai_alert_routing._alert_targets and
# ai_analysis._known_visitors must read those SAME keys back. This is exactly
# the kind of silent writer/reader key-name mismatch that would slip past
# flow-only or backend-only tests.
# ─────────────────────────────────────────────────────────────────────────────


def _stub_coordinator(entry: MockConfigEntry, hass: HomeAssistant) -> SimpleNamespace:
    """Minimal coordinator stub carrying only what
    ai_alert_routing._alert_targets / ai_analysis._known_visitors read."""
    return SimpleNamespace(
        config_entry=entry,
        hass=hass,
        options={},
        camera_entities={},
    )


class TestAiTargetBackendContract:
    async def test_alert_targets_reads_back_flow_written_keys(
        self, hass: HomeAssistant
    ) -> None:
        """Create a real ai_target subentry via the flow, then confirm
        ai_alert_routing._alert_targets(coordinator) reads it back with the
        exact same field values — proves writer and reader agree on key
        names end to end (not just both importing the same constant, which
        would still pass if one side used a different constant with the
        same string value by coincidence)."""
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_target")
        result2 = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_TARGET_NAME: "Familie",
                CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile_app_phone",
                CONF_AI_TARGET_MIN_SCORE: 6,
                CONF_AI_TARGET_CONDITION: "armed",
                CONF_AI_TARGET_CAMERA_FILTER: ["camera.terrasse"],
            },
        )
        assert result2["type"] == "create_entry"

        coordinator = _stub_coordinator(entry, hass)
        targets = ai_alert_routing._alert_targets(coordinator)

        assert len(targets) == 1
        target = targets[0]
        assert target[CONF_AI_TARGET_NAME] == "Familie"
        assert target[CONF_AI_TARGET_NOTIFY_SERVICE] == "notify.mobile_app_phone"
        assert target[CONF_AI_TARGET_MIN_SCORE] == 6
        assert target[CONF_AI_TARGET_CONDITION] == "armed"
        assert target[CONF_AI_TARGET_CAMERA_FILTER] == ["camera.terrasse"]

    async def test_alert_targets_empty_when_no_subentries(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        coordinator = _stub_coordinator(entry, hass)
        assert ai_alert_routing._alert_targets(coordinator) == []

    async def test_alert_targets_ignores_other_subentry_types(
        self, hass: HomeAssistant
    ) -> None:
        """A subentry of a DIFFERENT type (e.g. ai_visitor) sharing the same
        config entry must not leak into _alert_targets."""
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_visitor")
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_VISITOR_NAME: "Postbote",
                CONF_AI_VISITOR_DESCRIPTION: "desc",
            },
        )
        coordinator = _stub_coordinator(entry, hass)
        assert ai_alert_routing._alert_targets(coordinator) == []

    async def test_async_route_alert_dispatches_using_flow_written_target(
        self, hass: HomeAssistant
    ) -> None:
        """End-to-end: a flow-created target with min_score=5/condition=always
        actually receives the dispatched service call for a score-8 alert."""
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_target")
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_TARGET_NAME: "Familie",
                CONF_AI_TARGET_NOTIFY_SERVICE: "notify.mobile_app_phone",
                CONF_AI_TARGET_MIN_SCORE: 5,
                CONF_AI_TARGET_CONDITION: "always",
                CONF_AI_TARGET_CAMERA_FILTER: [],
            },
        )
        coordinator = _stub_coordinator(entry, hass)
        coordinator.options = {CONF_AI_ANALYSIS_ALARMO_ENABLED: False}

        # hass.services.async_call is a read-only attribute on the real
        # ServiceRegistry (can't be monkeypatched via setattr/patch.object —
        # it's slotted). Register a real fake "notify.mobile_app_phone"
        # service instead, the standard pytest-homeassistant-custom-component
        # convention, and capture the actual ServiceCall it receives.
        calls = async_mock_service(hass, "notify", "mobile_app_phone")

        await ai_alert_routing.async_route_alert(
            coordinator, "cam1", {"score": 8, "short": "Person detected"}
        )

        assert len(calls) == 1
        call = calls[0]
        assert call.domain == "notify"
        assert call.service == "mobile_app_phone"
        assert call.data["data"]["camera"] == "cam1"


class TestAiVisitorBackendContract:
    async def test_known_visitors_reads_back_flow_written_keys(
        self, hass: HomeAssistant
    ) -> None:
        """Create a real ai_visitor subentry via the flow, then confirm
        ai_analysis._known_visitors(coordinator) reads it back correctly."""
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_visitor")
        result2 = await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                CONF_AI_VISITOR_NAME: "Postbote",
                CONF_AI_VISITOR_DESCRIPTION: "Trägt gelbe DHL-Uniform",
            },
        )
        assert result2["type"] == "create_entry"

        coordinator = _stub_coordinator(entry, hass)
        visitors = ai_analysis._known_visitors(coordinator)

        assert visitors == [("Postbote", "Trägt gelbe DHL-Uniform")]

    async def test_known_visitors_empty_when_no_subentries(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        coordinator = _stub_coordinator(entry, hass)
        assert ai_analysis._known_visitors(coordinator) == []

    async def test_known_visitors_skips_blank_entries(
        self, hass: HomeAssistant
    ) -> None:
        """A visitor subentry with both name and description blank (see
        TestAiVisitorSubentryFlow's empty-field finding — schema allows it)
        must be filtered out by _known_visitors' own `if name or desc` guard,
        not surfaced as a bogus ("", "") prompt line."""
        entry = _entry(hass)
        result = await _start_subentry(hass, entry, "ai_visitor")
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={CONF_AI_VISITOR_NAME: "", CONF_AI_VISITOR_DESCRIPTION: ""},
        )
        coordinator = _stub_coordinator(entry, hass)
        assert ai_analysis._known_visitors(coordinator) == []

    async def test_known_visitors_multiple_subentries_all_returned(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(hass)
        for name, desc in (("Postbote", "DHL"), ("Nachbar", "Wohnt nebenan")):
            result = await _start_subentry(hass, entry, "ai_visitor")
            await hass.config_entries.subentries.async_configure(
                result["flow_id"],
                user_input={
                    CONF_AI_VISITOR_NAME: name,
                    CONF_AI_VISITOR_DESCRIPTION: desc,
                },
            )
        coordinator = _stub_coordinator(entry, hass)
        visitors = set(ai_analysis._known_visitors(coordinator))
        assert visitors == {("Postbote", "DHL"), ("Nachbar", "Wohnt nebenan")}
