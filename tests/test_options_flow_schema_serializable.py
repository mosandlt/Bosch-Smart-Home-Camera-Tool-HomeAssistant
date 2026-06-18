"""Regression: the OptionsFlow schema must be frontend-serializable (issue #35).

Bug source: GitHub issue #35 (GhostRider2809, v13.7.2) — opening
Settings → Integrations → Bosch Smart Home Camera → *Configure* failed with
"Der Konfigurationsfluss konnte nicht geladen werden: 500 Internal Server
Error". Reproduced on the maintainer's own instance.

Root cause: the AI section (added in v13.7.0) declared four fields as
``vol.Any("", <Selector>)`` to allow an empty value. HA serialises every
options/config-flow schema to JSON for the frontend via
``voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)``
(homeassistant/helpers/data_entry_flow.py ``_prepare_result_json``).
``voluptuous_serialize`` has no converter for a ``vol.Any`` node and raises
``ValueError: Unable to convert schema: Any(...)`` — surfaced to the browser as
a 500. The dialog had been unopenable since v13.7.0; it only manifests when a
user actually opens *Configure*.

Fix: selectors are optional/clearable on their own — drop the ``vol.Any``
wrappers and use the bare ``EntitySelector`` / ``TextSelector``. This test pins
the schema against the *exact* serialisation HA performs, so any future
unserialisable node (``vol.Any``, a raw lambda, …) fails here instead of in a
user's browser.
"""

from __future__ import annotations

import pytest
import voluptuous_serialize
from homeassistant.helpers import config_validation as cv

from custom_components.bosch_shc_camera.config_flow import (
    BoschCameraOptionsFlow,
)
from custom_components.bosch_shc_camera.const import (
    CONF_AI_ACTIVE_CONDITION_ENTITY,
    CONF_AI_ACTIVE_TIME_END,
    CONF_AI_ACTIVE_TIME_START,
    CONF_AI_TASK_ENTITY,
)
from tests.test_options_flow_settings import _legacy_token, _make_entry


async def _capture_init_schema(flow: BoschCameraOptionsFlow):
    """Run the GET path of async_step_init and return its data_schema."""
    captured: dict = {}

    def capture(**kw):
        captured["schema"] = kw.get("data_schema")
        return {"type": "form"}

    flow.async_show_form = capture  # type: ignore[method-assign]
    await flow.async_step_init(user_input=None)
    return captured["schema"]


def _serialize_like_frontend(schema) -> list:
    """Exactly what homeassistant.helpers.data_entry_flow does before sending
    the form to the browser. Raises ValueError on an unserialisable node."""
    return voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)


class TestOptionsSchemaSerializable:
    @pytest.mark.asyncio
    async def test_default_entry_schema_serializes(self):
        """The very thing that 500'd: serialise the freshly-built options form."""
        flow = BoschCameraOptionsFlow(_make_entry())
        schema = await _capture_init_schema(flow)
        # Must not raise "Unable to convert schema: Any(...)".
        result = _serialize_like_frontend(schema)
        assert isinstance(result, list) and result, "schema serialised to nothing"

    @pytest.mark.asyncio
    async def test_legacy_token_schema_serializes(self):
        """Legacy token adds the migrate_to_oss_client field — serialise too."""
        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=_legacy_token()))
        schema = await _capture_init_schema(flow)
        result = _serialize_like_frontend(schema)
        assert isinstance(result, list) and result

    @pytest.mark.asyncio
    async def test_schema_with_existing_ai_options_serializes(self):
        """Pre-existing AI option values (suggested_value path) must serialise."""
        flow = BoschCameraOptionsFlow(
            _make_entry(
                options={
                    CONF_AI_TASK_ENTITY: "ai_task.openai",
                    CONF_AI_ACTIVE_TIME_START: "08:00",
                    CONF_AI_ACTIVE_TIME_END: "22:00",
                    CONF_AI_ACTIVE_CONDITION_ENTITY: "person.thomas",
                }
            )
        )
        schema = await _capture_init_schema(flow)
        result = _serialize_like_frontend(schema)
        assert isinstance(result, list) and result

    @pytest.mark.asyncio
    async def test_ai_fields_still_present(self):
        """Guard against the fix accidentally dropping the AI fields."""
        flow = BoschCameraOptionsFlow(_make_entry())
        schema = await _capture_init_schema(flow)
        ai_section = None
        for k, v in schema.schema.items():
            if str(k) == "ai":
                inner = getattr(v, "schema", None) or v
                ai_section = getattr(inner, "schema", inner)
                break
        assert ai_section is not None, "AI section missing from options schema"
        keys = {str(k) for k in ai_section}
        for field in (
            CONF_AI_TASK_ENTITY,
            CONF_AI_ACTIVE_TIME_START,
            CONF_AI_ACTIVE_TIME_END,
            CONF_AI_ACTIVE_CONDITION_ENTITY,
        ):
            assert field in keys, f"{field} missing from AI section after fix"
