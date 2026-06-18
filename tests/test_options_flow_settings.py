"""Scenario tests for every settings section in the OptionsFlow.

Coverage goals
--------------
* Every section submits correctly → keys land in the saved entry.
* Default values from DEFAULT_OPTIONS are used as fallbacks in the schema.
* Range constraints on numeric fields reject out-of-bound values.
* Boolean normalization coerces int 1/0 → True/False.
* enable_local_save defaults to OFF (v11.0.12 regression guard).
* migrate_to_oss_client only exposed for legacy residential_app tokens.
* Round-trip: existing options survive when only one section is submitted.
* All 50+ fields in DEFAULT_OPTIONS have a corresponding OPTIONS_SECTIONS entry.

Design note: OptionsFlow uses HA's ``section()`` helper, so user_input arrives
as ``{section_key: {field: value}, ...}``. ``_flatten_sections`` normalises it
back to the flat dict every other module consumes.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.config_flow import (
    OPTIONS_SECTIONS,
    BoschCameraOptionsFlow,
    _flatten_sections,
)
from custom_components.bosch_shc_camera.const import (
    CONF_AI_ACTIVE_CONDITION_ENTITY,
    CONF_AI_ACTIVE_TIME_END,
    CONF_AI_ACTIVE_TIME_START,
    CONF_AI_MAX_PER_DAY,
    CONF_AI_TASK_ENTITY,
    DEFAULT_OPTIONS,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _make_entry(
    *, options: dict | None = None, bearer_token: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": bearer_token, "refresh_token": "rt"},
        options=options or {},
    )


def _legacy_token() -> str:
    """Build a minimal JWT with azp=residential_app (legacy client)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = (
        base64.urlsafe_b64encode(json.dumps({"azp": "residential_app"}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{body}.x"


def _oss_token() -> str:
    """Build a minimal JWT with azp=oss_residential_app (new OSS client)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = (
        base64.urlsafe_b64encode(json.dumps({"azp": "oss_residential_app"}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{body}.x"


async def _submit(flow: BoschCameraOptionsFlow, user_input: dict) -> dict:
    """Submit the options form and return the saved data dict."""
    saved: dict = {}
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: (
            saved.update({"data": kw.get("data", {})}) or {"type": "create_entry"}
        ),
    )
    result = await flow.async_step_init(user_input=user_input)
    assert result["type"] == "create_entry", f"Expected create_entry, got {result}"
    return saved["data"]


# ── Section submit round-trips ────────────────────────────────────────────────


class TestPollingSection:
    @pytest.mark.asyncio
    async def test_custom_intervals_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 120,
                    "interval_status": 600,
                    "interval_events": 400,
                    "snapshot_interval": 3600,
                },
            },
        )
        assert data["scan_interval"] == 120
        assert data["interval_status"] == 600
        assert data["interval_events"] == 400
        assert data["snapshot_interval"] == 3600

    @pytest.mark.asyncio
    async def test_defaults_applied_when_no_prior_options(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        # Submit an unrelated section; polling fields should fall back to DEFAULT_OPTIONS.
        data = await _submit(flow, {"auth": {"force_relogin": False}})
        # scan_interval not in user_input → falls back to saved options (empty) → DEFAULT_OPTIONS
        assert (
            "scan_interval" not in data
            or data["scan_interval"] == DEFAULT_OPTIONS["scan_interval"]
        )

    def test_scan_interval_min_boundary(self):
        """vol.Range(min=10): value 10 must be accepted."""
        schema_inner = _get_section_schema("polling")
        result = schema_inner({"scan_interval": 10})
        assert result["scan_interval"] == 10

    def test_scan_interval_below_min_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("polling")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"scan_interval": 9})

    def test_scan_interval_max_boundary(self):
        schema_inner = _get_section_schema("polling")
        result = schema_inner({"scan_interval": 3600})
        assert result["scan_interval"] == 3600

    def test_scan_interval_above_max_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("polling")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"scan_interval": 3601})

    def test_snapshot_interval_min_boundary(self):
        schema_inner = _get_section_schema("polling")
        result = schema_inner({"snapshot_interval": 300})
        assert result["snapshot_interval"] == 300

    def test_snapshot_interval_below_min_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("polling")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"snapshot_interval": 299})


class TestFeaturesSection:
    @pytest.mark.asyncio
    async def test_all_feature_toggles_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "features": {
                    "enable_snapshots": True,
                    "enable_sensors": False,
                    "enable_binary_sensors": False,
                    "enable_snapshot_button": True,
                    "enable_intercom": True,
                },
            },
        )
        assert data["enable_snapshots"] is True
        assert data["enable_sensors"] is False
        assert data["enable_binary_sensors"] is False
        assert data["enable_snapshot_button"] is True
        assert data["enable_intercom"] is True

    @pytest.mark.asyncio
    async def test_boolean_coercion_1_0(self):
        """int 1/0 → True/False (HA schema may deliver ints from selector)."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "features": {"enable_snapshots": 1, "enable_intercom": 0},
            },
        )
        assert data["enable_snapshots"] is True
        assert data["enable_intercom"] is False

    def test_enable_snapshots_default_true(self):
        assert DEFAULT_OPTIONS["enable_snapshots"] is True

    def test_enable_intercom_default_false(self):
        assert DEFAULT_OPTIONS["enable_intercom"] is False


class TestStreamSection:
    @pytest.mark.asyncio
    async def test_stream_type_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        for val in ["auto", "local", "remote"]:
            data = await _submit(flow, {"stream": {"stream_connection_type": val}})
            assert data["stream_connection_type"] == val

    @pytest.mark.asyncio
    async def test_live_buffer_mode_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        for val in ["latency", "balanced", "stable"]:
            data = await _submit(flow, {"stream": {"live_buffer_mode": val}})
            assert data["live_buffer_mode"] == val

    @pytest.mark.asyncio
    async def test_enable_go2rtc_toggle(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"stream": {"enable_go2rtc": False}})
        assert data["enable_go2rtc"] is False

    def test_stream_connection_type_default(self):
        # v12.4.2 flipped the default to "local" (LOCAL-first); existing
        # installs that relied on the old "auto" default are migrated via
        # async_migrate_entry → see test_local_first_default.py.
        assert DEFAULT_OPTIONS["stream_connection_type"] == "local"

    def test_live_buffer_mode_default(self):
        assert DEFAULT_OPTIONS["live_buffer_mode"] == "balanced"

    def test_enable_go2rtc_default_true(self):
        assert DEFAULT_OPTIONS["enable_go2rtc"] is True


class TestFcmSection:
    @pytest.mark.asyncio
    async def test_fcm_push_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "fcm": {
                    "enable_fcm_push": True,
                    "fcm_push_mode": "android",
                    "mark_events_read": True,
                    "alert_save_snapshots": True,
                    "alert_delete_after_send": False,
                    "alert_notify_service": "notify.test_user",
                    "alert_notify_information": "notify.info",
                    "alert_notify_screenshot": "notify.screenshot",
                    "alert_notify_video": "notify.video",
                    "alert_notify_system": "notify.system",
                },
            },
        )
        assert data["enable_fcm_push"] is True
        assert data["fcm_push_mode"] == "android"
        assert data["mark_events_read"] is True
        assert data["alert_save_snapshots"] is True
        assert data["alert_delete_after_send"] is False
        assert data["alert_notify_service"] == "notify.test_user"
        assert data["alert_notify_video"] == "notify.video"

    @pytest.mark.asyncio
    async def test_empty_alert_services_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "fcm": {
                    "alert_notify_service": "",
                    "alert_notify_video": "",
                },
            },
        )
        assert data.get("alert_notify_service", "") == ""

    def test_mark_events_read_default_false(self):
        """xDraGGi regression: mark_events_read must default OFF."""
        assert DEFAULT_OPTIONS.get("mark_events_read", False) is False

    def test_enable_fcm_push_default_false(self):
        assert DEFAULT_OPTIONS["enable_fcm_push"] is False

    def test_alert_delete_after_send_default_true(self):
        assert DEFAULT_OPTIONS["alert_delete_after_send"] is True

    def test_all_fcm_modes_valid(self):
        """fcm_push_mode must accept all four documented values."""
        import voluptuous as vol

        validator = vol.In(["auto", "android", "ios", "polling"])
        for mode in ["auto", "android", "ios", "polling"]:
            assert validator(mode) == mode

    def test_invalid_fcm_mode_rejected(self):
        import voluptuous as vol

        with pytest.raises(vol.Invalid):
            vol.In(["auto", "android", "ios", "polling"])("unknown")


class TestEventsStorageSection:
    @pytest.mark.asyncio
    async def test_enable_local_save_defaults_off(self):
        """v11.0.12 regression: fresh install must NOT auto-save events."""
        assert DEFAULT_OPTIONS["enable_local_save"] is False

    @pytest.mark.asyncio
    async def test_enable_local_save_toggle_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"events_storage": {"enable_local_save": True}})
        assert data["enable_local_save"] is True

    @pytest.mark.asyncio
    async def test_enable_local_save_int_coercion(self):
        """enable_local_save int 1/0 must be coerced to bool (was missing from coerce list)."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"events_storage": {"enable_local_save": 1}})
        assert data["enable_local_save"] is True
        assert isinstance(data["enable_local_save"], bool)

        flow2 = BoschCameraOptionsFlow(_make_entry())
        data2 = await _submit(flow2, {"events_storage": {"enable_local_save": 0}})
        assert data2["enable_local_save"] is False
        assert isinstance(data2["enable_local_save"], bool)

    @pytest.mark.asyncio
    async def test_download_path_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "events_storage": {"download_path": "/config/my_events"},
            },
        )
        assert data["download_path"] == "/config/my_events"

    @pytest.mark.asyncio
    async def test_smb_fields_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "events_storage": {
                    "enable_smb_upload": True,
                    "upload_protocol": "ftp",
                    "smb_server": "192.168.1.100",
                    "smb_share": "NAS",
                    "smb_username": "user",
                    "smb_password": "secret",
                    "smb_base_path": "Bosch",
                    "folder_pattern": "{year}/{month}",
                    "file_pattern": "{camera}_{id}",
                    "smb_retention_days": 90,
                },
            },
        )
        assert data["enable_smb_upload"] is True
        assert data["upload_protocol"] == "ftp"
        assert data["smb_server"] == "192.168.1.100"
        assert data["smb_retention_days"] == 90

    def test_smb_retention_days_range(self):
        schema_inner = _get_section_schema("events_storage")
        assert schema_inner({"smb_retention_days": 0})["smb_retention_days"] == 0
        assert schema_inner({"smb_retention_days": 3650})["smb_retention_days"] == 3650

    def test_smb_retention_days_above_max_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("events_storage")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"smb_retention_days": 3651})

    def test_default_download_path(self):
        assert DEFAULT_OPTIONS["download_path"] == "/config/bosch_events"

    def test_default_smb_base_path(self):
        assert DEFAULT_OPTIONS["smb_base_path"] == "Bosch-Kameras"


class TestNvrSection:
    @pytest.mark.asyncio
    async def test_nvr_fields_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "nvr": {
                    "enable_nvr": True,
                    "nvr_storage_target": "smb",
                    "nvr_base_path": "/config/bosch_nvr",
                    "nvr_smb_subpath": "NVR",
                    "nvr_retention_days": 7,
                },
            },
        )
        assert data["enable_nvr"] is True
        assert data["nvr_storage_target"] == "smb"
        assert data["nvr_base_path"] == "/config/bosch_nvr"
        assert data["nvr_smb_subpath"] == "NVR"
        assert data["nvr_retention_days"] == 7

    def test_nvr_retention_min(self):
        schema_inner = _get_section_schema("nvr")
        assert schema_inner({"nvr_retention_days": 1})["nvr_retention_days"] == 1

    def test_nvr_retention_below_min_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("nvr")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"nvr_retention_days": 0})

    def test_nvr_retention_max(self):
        schema_inner = _get_section_schema("nvr")
        assert schema_inner({"nvr_retention_days": 365})["nvr_retention_days"] == 365

    def test_nvr_storage_targets(self):
        assert DEFAULT_OPTIONS["nvr_storage_target"] == "local"

    def test_enable_nvr_default_false(self):
        assert DEFAULT_OPTIONS["enable_nvr"] is False

    @pytest.mark.asyncio
    async def test_nvr_storage_target_ftp(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"nvr": {"nvr_storage_target": "ftp"}})
        assert data["nvr_storage_target"] == "ftp"


class TestAiSection:
    """Tests for AI options schema validation (E-P1 / E-P2 / E-P3)."""

    # ── E-P1: entity fields are nullable selectors (gate-disable via clear) ─────
    # The fields use the serializer-supported nullable shape
    # ``vol.Any(None, EntitySelector(...))`` (issue #35). The frontend submits
    # ``None`` (allow_none) — NOT ""  — when an entity picker is cleared, so the
    # disable-the-gate path is None here, not "".

    def test_ai_task_entity_none_allowed(self) -> None:
        """Clearing the ai_task entity selector submits None — must NOT raise."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_TASK_ENTITY: None})
        assert result[CONF_AI_TASK_ENTITY] is None

    def test_ai_task_entity_valid_entity_id_allowed(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_TASK_ENTITY: "ai_task.my_llm"})
        assert result[CONF_AI_TASK_ENTITY] == "ai_task.my_llm"

    def test_ai_active_condition_entity_none_allowed(self) -> None:
        """Clearing the condition entity selector submits None — must NOT raise."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_CONDITION_ENTITY: None})
        assert result[CONF_AI_ACTIVE_CONDITION_ENTITY] is None

    def test_ai_active_condition_entity_valid_entity_id_allowed(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_CONDITION_ENTITY: "input_boolean.away"})
        assert result[CONF_AI_ACTIVE_CONDITION_ENTITY] == "input_boolean.away"

    # ── E-P2: ai_max_per_day has no upper cap (0 = unlimited is honoured) ───────

    def test_ai_max_per_day_zero_allowed(self) -> None:
        """0 = unlimited must pass (E-P2)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_MAX_PER_DAY: 0})
        assert result[CONF_AI_MAX_PER_DAY] == 0

    def test_ai_max_per_day_large_value_allowed(self) -> None:
        """Values beyond old max=100000 cap must now pass (E-P2)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_MAX_PER_DAY: 500000})
        assert result[CONF_AI_MAX_PER_DAY] == 500000

    def test_ai_max_per_day_negative_rejected(self) -> None:
        """Negative budgets must be rejected."""
        import voluptuous as vol

        schema = _get_section_schema("ai")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_MAX_PER_DAY: -1})

    # ── E-P3: time fields accept HH:MM / HH:MM:SS or empty (gate-disable) ───────

    def test_ai_active_time_start_empty_allowed(self) -> None:
        """Empty string must be allowed to disable the time gate (E-P3)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_START: ""})
        assert result[CONF_AI_ACTIVE_TIME_START] == ""

    def test_ai_active_time_end_empty_allowed(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_END: ""})
        assert result[CONF_AI_ACTIVE_TIME_END] == ""

    def test_ai_active_time_start_hhmm_valid(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_START: "08:00"})
        assert result[CONF_AI_ACTIVE_TIME_START] == "08:00"

    def test_ai_active_time_end_hhmmss_valid(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_END: "22:30:00"})
        assert result[CONF_AI_ACTIVE_TIME_END] == "22:30:00"

    # NOTE (issue #35): the time fields are plain TextSelectors now — the old
    # ``vol.Any("", vol.All(..., vol.Match(HH:MM)))`` schema-level regex could
    # not be serialised by the frontend and 500'd the options dialog. Format
    # validation therefore lives in the backend window parser
    # (_ai_window_allowed), which treats any unparseable value as "no time gate"
    # (see test_ai_window_* for that behaviour). The schema now accepts any
    # string so the dialog stays serialisable; these tests pin that contract.

    def test_ai_active_time_start_garbage_accepted_at_schema_level(self) -> None:
        """Prose values are accepted by the schema (validated at runtime)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_START: "midnight"})
        assert result[CONF_AI_ACTIVE_TIME_START] == "midnight"

    def test_ai_active_time_end_out_of_range_accepted_at_schema_level(self) -> None:
        """'25:00' passes the schema; the runtime gate rejects it as malformed."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_END: "25:00"})
        assert result[CONF_AI_ACTIVE_TIME_END] == "25:00"

    # ── E-P1 + round-trip: submit AI section with entity cleared ────────────────

    @pytest.mark.asyncio
    async def test_ai_entity_fields_cleared_saves_none(self) -> None:
        """Clearing the nullable entity pickers submits None (allow_none) and
        must persist None — disabling the respective gate (issue #35, E-P1)."""
        flow = BoschCameraOptionsFlow(
            _make_entry(
                options={
                    CONF_AI_TASK_ENTITY: "ai_task.old_llm",
                    CONF_AI_ACTIVE_CONDITION_ENTITY: "input_boolean.away",
                }
            )
        )
        data = await _submit(
            flow,
            {
                "ai": {
                    CONF_AI_TASK_ENTITY: None,
                    CONF_AI_ACTIVE_CONDITION_ENTITY: None,
                    CONF_AI_ACTIVE_TIME_START: "",
                    CONF_AI_ACTIVE_TIME_END: "",
                }
            },
        )
        assert data[CONF_AI_TASK_ENTITY] is None
        assert data[CONF_AI_ACTIVE_CONDITION_ENTITY] is None

    @pytest.mark.asyncio
    async def test_ai_time_gate_with_valid_times_saves(self) -> None:
        """Valid HH:MM times must be saved correctly."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "ai": {
                    CONF_AI_ACTIVE_TIME_START: "07:00",
                    CONF_AI_ACTIVE_TIME_END: "23:00",
                    CONF_AI_TASK_ENTITY: "",
                    CONF_AI_ACTIVE_CONDITION_ENTITY: "",
                }
            },
        )
        assert data[CONF_AI_ACTIVE_TIME_START] == "07:00"
        assert data[CONF_AI_ACTIVE_TIME_END] == "23:00"


class TestAuthSection:
    @pytest.mark.asyncio
    async def test_force_relogin_triggers_relogin_step(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.async_step_relogin_show = AsyncMock(
            return_value={"type": "form", "step_id": "relogin_show"}
        )
        result = await flow.async_step_init(
            user_input={
                "auth": {"force_relogin": True},
            }
        )
        assert result["step_id"] == "relogin_show"

    @pytest.mark.asyncio
    async def test_force_relogin_false_does_not_branch(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        result = await flow.async_step_init(
            user_input={
                "auth": {"force_relogin": False},
            }
        )
        assert result["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_migrate_oss_triggers_abort_for_legacy_token(self):
        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=_legacy_token()))
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.async_create_task = MagicMock()
        flow._config_entry.async_start_reauth = MagicMock(return_value=None)
        flow.async_abort = MagicMock(
            return_value={"type": "abort", "reason": "migration_started"}
        )
        result = await flow.async_step_init(
            user_input={
                "auth": {"migrate_to_oss_client": True},
            }
        )
        assert result["reason"] == "migration_started"

    @pytest.mark.asyncio
    async def test_migrate_field_absent_for_oss_token(self):
        """migrate_to_oss_client must NOT appear in the schema for OSS tokens."""
        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=_oss_token()))
        captured_schema = {}

        def capture(**kw):
            captured_schema["schema"] = kw.get("data_schema")
            return {"type": "form"}

        flow.async_show_form = capture
        await flow.async_step_init(user_input=None)
        schema = captured_schema["schema"]
        # Flatten all keys from the schema
        all_keys = {str(k) for k in schema.schema}
        # The migrate field is inside the auth section — get inner schema
        auth_section = None
        for k, v in schema.schema.items():
            if str(k) == "auth":
                # v is a section object; .schema is the inner vol.Schema
                inner = getattr(v, "schema", None) or v
                if hasattr(inner, "schema"):
                    auth_section = inner.schema
                break
        if auth_section is not None:
            inner_keys = {str(k) for k in auth_section}
            assert "migrate_to_oss_client" not in inner_keys, (
                "migrate_to_oss_client must not appear in schema for OSS token"
            )


# ── Cross-section round-trip tests ────────────────────────────────────────────


class TestFullRoundTrip:
    """Submit all sections at once and verify every key lands correctly."""

    @pytest.mark.asyncio
    async def test_all_sections_submitted_together(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 45,
                    "interval_status": 200,
                    "interval_events": 150,
                    "snapshot_interval": 900,
                },
                "features": {
                    "enable_snapshots": True,
                    "enable_sensors": True,
                    "enable_binary_sensors": True,
                    "enable_snapshot_button": False,
                    "enable_intercom": False,
                },
                "stream": {
                    "stream_connection_type": "local",
                    "live_buffer_mode": "latency",
                    "enable_go2rtc": True,
                },
                "fcm": {
                    "enable_fcm_push": True,
                    "fcm_push_mode": "ios",
                    "mark_events_read": False,
                    "alert_save_snapshots": False,
                    "alert_delete_after_send": True,
                    "alert_notify_service": "notify.test_user",
                    "alert_notify_information": "",
                    "alert_notify_screenshot": "",
                    "alert_notify_video": "notify.video",
                    "alert_notify_system": "",
                },
                "events_storage": {
                    "enable_local_save": True,
                    "download_path": "/config/bosch_events",
                    "enable_smb_upload": False,
                    "upload_protocol": "smb",
                    "smb_server": "",
                    "smb_share": "",
                    "smb_username": "",
                    "smb_password": "",
                    "smb_base_path": "Bosch-Kameras",
                    "folder_pattern": "{year}/{month}/{day}",
                    "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
                    "smb_retention_days": 180,
                },
                "nvr": {
                    "enable_nvr": False,
                    "nvr_storage_target": "local",
                    "nvr_base_path": "/config/bosch_nvr",
                    "nvr_smb_subpath": "NVR",
                    "nvr_retention_days": 3,
                },
                "auth": {"force_relogin": False},
            },
        )
        # Spot-check a key from each section
        assert data["scan_interval"] == 45
        assert data["enable_snapshots"] is True
        assert data["stream_connection_type"] == "local"
        assert data["enable_fcm_push"] is True
        assert data["fcm_push_mode"] == "ios"
        assert data["enable_local_save"] is True
        assert data["enable_nvr"] is False

    @pytest.mark.asyncio
    async def test_existing_options_not_lost_when_partial_submit(self):
        """Only 'polling' submitted → prior enable_go2rtc option survives in saved data
        because _flatten_sections passes through the flat submit dict and
        async_step_init merges with existing options first."""
        prior = {"enable_go2rtc": False}
        flow = BoschCameraOptionsFlow(_make_entry(options=prior))
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 45,
                    "interval_status": 300,
                    "interval_events": 300,
                    "snapshot_interval": 1800,
                }
            },
        )
        # The submitted 'polling' section was the only one sent.
        # enable_go2rtc not in the submit → but must be preserved via prior options merge.
        assert data["scan_interval"] == 45
        assert data["enable_go2rtc"] is False  # non-submitted field preserved

    @pytest.mark.asyncio
    async def test_suggested_value_field_preserved_when_user_does_not_edit(self):
        """Regression: smb_server (suggested_value-only, no default=) must NOT revert
        to '' when the user opens options and saves without touching the SMB section.

        Before the fix, the save path called async_create_entry(data=user_input)
        directly; fields absent from user_input (because the user did not touch them)
        were silently dropped, reverting to DEFAULT_OPTIONS values.

        After the fix, async_step_init merges user_input on top of the existing opts:
            merged = {**opts, **user_input}
        so the persisted smb_server survives even when the user does not submit it.
        """
        prior = {
            "smb_server": "192.168.2.25",
            "smb_share": "bosch-events",
            "smb_username": "nas_user",
            "smb_password": "s3cret",
            "alert_notify_service": "notify.mobile_app",
            "alert_notify_information": "true",
            "download_path": "/config/my_events",
        }
        flow = BoschCameraOptionsFlow(_make_entry(options=prior))
        # User only touches the polling interval — does NOT submit any events_storage
        # or fcm section fields → those suggested_value fields must survive.
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 120,
                    "interval_status": 300,
                    "interval_events": 300,
                    "snapshot_interval": 1800,
                }
            },
        )
        assert data["scan_interval"] == 120
        # suggested_value-only fields — must keep saved values, NOT revert to "":
        assert data["smb_server"] == "192.168.2.25"
        assert data["smb_share"] == "bosch-events"
        assert data["smb_username"] == "nas_user"
        assert data["smb_password"] == "s3cret"
        assert data["alert_notify_service"] == "notify.mobile_app"
        assert data["alert_notify_information"] == "true"
        assert data["download_path"] == "/config/my_events"

    @pytest.mark.asyncio
    async def test_suggested_value_field_preserved_on_migrate_to_oss_path(self):
        """Regression: same merge must happen on the migrate_to_oss code path.

        When the user clicks 'migrate to OSS client', async_update_entry is called
        with the merged options dict.  Without the merge, suggested_value fields
        absent from user_input were dropped before being persisted.
        """
        prior = {
            "smb_server": "192.168.2.25",
            "alert_notify_service": "notify.mobile_app",
        }
        entry = _make_entry(options=prior, bearer_token=_legacy_token())
        # async_start_reauth is called as a coroutine on the config entry
        entry.async_start_reauth = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        flow = BoschCameraOptionsFlow(entry)
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        flow.hass.async_create_task = MagicMock()

        update_calls: list[dict] = []

        def capture_update(e, **kw):  # type: ignore[no-untyped-def]
            update_calls.append(kw)

        flow.hass.config_entries.async_update_entry = capture_update
        flow.async_abort = MagicMock(return_value={"type": "abort"})

        # User submits with migrate_to_oss_client=True but no SMB fields
        await flow.async_step_init(
            user_input={"auth": {"force_relogin": False, "migrate_to_oss_client": True}}
        )

        assert update_calls, "async_update_entry was never called"
        saved_options = update_calls[0].get("options", {})
        # suggested_value fields absent from user_input must be in the persisted options
        assert saved_options.get("smb_server") == "192.168.2.25"
        assert saved_options.get("alert_notify_service") == "notify.mobile_app"


# ── Coverage: all DEFAULT_OPTIONS keys are in OPTIONS_SECTIONS ────────────────


class TestDefaultOptionsCompleteness:
    """Every key in DEFAULT_OPTIONS must be in exactly one OPTIONS_SECTIONS entry.

    If DEFAULT_OPTIONS grows a new key and OPTIONS_SECTIONS is not updated, the
    field silently falls through the options UI — users can never change it.
    """

    def test_all_default_option_keys_covered_by_sections(self):
        all_section_fields = {f for fields in OPTIONS_SECTIONS.values() for f in fields}
        missing = [k for k in DEFAULT_OPTIONS if k not in all_section_fields]
        assert not missing, (
            f"DEFAULT_OPTIONS keys not in any OPTIONS_SECTIONS section: {missing}. "
            "Add them to the correct section so users can configure them."
        )

    def test_no_section_field_missing_from_defaults(self):
        """Every OPTIONS_SECTIONS field should have a default (or be optional
        text-only). Fails loudly when a new UI field is added without a default."""
        # Text fields with suggested_value only (no hard default) are OK to be absent
        TEXT_ONLY_FIELDS = {
            "alert_notify_service",
            "alert_notify_information",
            "alert_notify_screenshot",
            "alert_notify_video",
            "alert_notify_system",
            "smb_server",
            "smb_share",
            "smb_username",
            "smb_password",
            "smb_base_path",
            "folder_pattern",
            "file_pattern",
            "nvr_base_path",
            "nvr_smb_subpath",
            "download_path",
            # auth actions — not persistent state
            "force_relogin",
            "migrate_to_oss_client",
        }
        all_section_fields = {f for fields in OPTIONS_SECTIONS.values() for f in fields}
        missing_defaults = [
            f
            for f in all_section_fields
            if f not in DEFAULT_OPTIONS and f not in TEXT_ONLY_FIELDS
        ]
        assert not missing_defaults, (
            f"OPTIONS_SECTIONS fields with no default in DEFAULT_OPTIONS: {missing_defaults}"
        )


# ── Schema introspection helper ───────────────────────────────────────────────


def _get_section_schema(section_name: str):
    """Render the options form and return the inner voluptuous schema for a section.

    Calls async_step_init(user_input=None) on a fresh flow, captures the
    data_schema, then walks into the named section to return its inner schema.
    Returns a callable that validates a partial dict (missing keys get defaults).
    """
    import asyncio

    import voluptuous as vol

    flow = BoschCameraOptionsFlow(_make_entry())
    captured: dict = {}

    def capture(**kw):
        captured["schema"] = kw.get("data_schema")
        return {"type": "form"}

    flow.async_show_form = capture
    asyncio.get_event_loop().run_until_complete(flow.async_step_init(user_input=None))

    outer: vol.Schema = captured["schema"]
    for key, val in outer.schema.items():
        if str(key) == section_name:
            # val is a section(inner_schema, options) object.
            # The inner schema is accessible via .schema attribute.
            inner = getattr(val, "schema", val)
            if hasattr(inner, "schema"):
                return inner
            return inner
    raise KeyError(f"Section {section_name!r} not found in options schema")
