"""Tests for text.py — BoschAiSceneContextText entity.

Free-form per-camera scene-context hint for the AI Camera Analysis prompt
builder (`ai_analysis.py`'s `_build_prompt`). Structural pattern mirrors
`select.py` (module-level `async_setup_entry` + `CoordinatorEntity` +
`RestoreEntity`, no `_attr_name`/translation_key naming) — see
`tests/test_select.py` for the sibling test-file structure this mirrors.

Purely local/in-memory state (`coordinator.ai_analysis_scene_context`) — no
cloud/camera API call, so the entity is always available whenever the
coordinator itself is healthy.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.text import (
    AI_SCENE_CONTEXT_MAX_LEN,
    BoschAiSceneContextText,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"


async def _noop_async(self) -> None:
    """Stand-in for super().async_added_to_hass() so RestoreEntity restore
    logic can be tested in isolation (mirrors tests/test_switch.py)."""
    return None


@pytest.fixture
def stub_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
            }
        },
        ai_analysis_scene_context={},
        last_update_success=True,
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


class TestAiSceneContextTextBasics:
    def test_unique_id(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent._attr_unique_id == f"bosch_shc_camera_{CAM_ID}_ai_scene_context"

    def test_translation_key(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent._attr_translation_key == "ai_scene_context"

    def test_entity_category_config(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from homeassistant.helpers.entity import EntityCategory

        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent._attr_entity_category == EntityCategory.CONFIG

    def test_has_entity_name_true(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent._attr_has_entity_name is True

    def test_attr_name_is_none(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        """No hardcoded `_attr_name` — naming comes purely from translation_key
        (doubled-prefix regression class, see test_select.py)."""
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        name = getattr(ent, "_attr_name", None)
        assert name is None

    def test_device_info_returns_identifiers(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from custom_components.bosch_shc_camera import DOMAIN

        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        info = ent.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]
        assert info["manufacturer"] == "Bosch"


class TestAiSceneContextTextNativeValue:
    def test_native_value_empty_by_default(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent.native_value == ""

    def test_native_value_reflects_dict(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord.ai_analysis_scene_context[CAM_ID] = (
            "gate on the left is usually closed"
        )
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent.native_value == "gate on the left is usually closed"


class TestAiSceneContextTextSetValue:
    @pytest.mark.asyncio
    async def test_set_value_updates_dict(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from unittest.mock import MagicMock

        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        ent.async_write_ha_state = MagicMock()
        await ent.async_set_value("delivery drop-off is the porch table")
        assert (
            stub_coord.ai_analysis_scene_context[CAM_ID]
            == "delivery drop-off is the porch table"
        )
        assert ent.native_value == "delivery drop-off is the porch table"
        ent.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_value_enforces_max_length(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        """A service-call/REST-API caller could bypass the client-side
        selector's max-length — the entity itself must truncate."""
        from unittest.mock import MagicMock

        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        ent.async_write_ha_state = MagicMock()
        too_long = "x" * (AI_SCENE_CONTEXT_MAX_LEN + 100)
        await ent.async_set_value(too_long)
        stored = stub_coord.ai_analysis_scene_context[CAM_ID]
        assert len(stored) == AI_SCENE_CONTEXT_MAX_LEN
        assert stored == "x" * AI_SCENE_CONTEXT_MAX_LEN

    @pytest.mark.asyncio
    async def test_set_value_empty_string(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from unittest.mock import MagicMock

        stub_coord.ai_analysis_scene_context[CAM_ID] = "previous value"
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        ent.async_write_ha_state = MagicMock()
        await ent.async_set_value("")
        assert stub_coord.ai_analysis_scene_context[CAM_ID] == ""
        assert ent.native_value == ""


class TestAiSceneContextTextAvailability:
    def test_available_when_coordinator_healthy(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent.available is True

    def test_unavailable_when_coordinator_update_failed(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord.last_update_success = False
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent.available is False

    def test_available_regardless_of_camera_online_state(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        """Purely local state — never gated on camera online/offline (this
        entity has no `is_camera_online` check at all in the source)."""
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        assert ent.available is True


class TestAiSceneContextTextRestore:
    def test_is_restore_entity(self) -> None:
        from homeassistant.helpers.restore_state import RestoreEntity

        assert issubclass(BoschAiSceneContextText, RestoreEntity)

    @pytest.mark.asyncio
    async def test_restore_previous_text_across_restart(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        ent.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(state="the gate on the left is closed")
        )
        ent.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await ent.async_added_to_hass()
        assert (
            stub_coord.ai_analysis_scene_context[CAM_ID]
            == "the gate on the left is closed"
        )

    @pytest.mark.asyncio
    async def test_restore_ignores_unknown_state(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        ent.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(state="unknown")
        )
        ent.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await ent.async_added_to_hass()
        assert CAM_ID not in stub_coord.ai_analysis_scene_context

    @pytest.mark.asyncio
    async def test_restore_ignores_unavailable_state(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        ent.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(state="unavailable")
        )
        ent.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await ent.async_added_to_hass()
        assert CAM_ID not in stub_coord.ai_analysis_scene_context

    @pytest.mark.asyncio
    async def test_restore_no_previous_state(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        ent = BoschAiSceneContextText(stub_coord, CAM_ID, stub_entry)
        ent.async_get_last_state = AsyncMock(return_value=None)  # type: ignore[method-assign]
        ent.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await ent.async_added_to_hass()
        assert CAM_ID not in stub_coord.ai_analysis_scene_context
        assert ent.native_value == ""


class TestAiSceneContextTextSetupEntry:
    @pytest.mark.asyncio
    async def test_setup_entry_creates_one_per_camera(self) -> None:
        from custom_components.bosch_shc_camera.text import async_setup_entry

        coord = SimpleNamespace(
            data={
                CAM_ID: {"info": {"title": "Terrasse"}},
                "22222222-0000-0000-0000-000000000002": {
                    "info": {"title": "Innenbereich"}
                },
            },
            ai_analysis_scene_context={},
            last_update_success=True,
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            runtime_data=coord,
            entry_id="01ENTRY",
            options={},
            async_on_unload=MagicMock(),
        )
        added: list = []
        hass = SimpleNamespace()

        await async_setup_entry(hass, entry, lambda ents, **kw: added.extend(ents))

        assert len(added) == 2
        assert all(isinstance(e, BoschAiSceneContextText) for e in added)

    @pytest.mark.asyncio
    async def test_new_camera_gets_entity_added_dynamically(self) -> None:
        from custom_components.bosch_shc_camera.text import async_setup_entry

        coord = SimpleNamespace(
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            ai_analysis_scene_context={},
            last_update_success=True,
            async_add_listener=MagicMock(return_value=MagicMock()),
        )
        entry = SimpleNamespace(
            runtime_data=coord,
            entry_id="01ENTRY",
            options={},
            async_on_unload=MagicMock(),
        )
        added: list = []
        hass = SimpleNamespace()

        await async_setup_entry(hass, entry, lambda ents, **kw: added.extend(ents))

        assert len(added) == 1
        coord.async_add_listener.assert_called_once()
        entry.async_on_unload.assert_called_once()

        listener = coord.async_add_listener.call_args[0][0]

        coord.data["22222222-0000-0000-0000-000000000002"] = {
            "info": {"title": "Innenbereich"}
        }
        listener()

        assert len(added) == 2
        assert all(isinstance(e, BoschAiSceneContextText) for e in added)

        listener()
        assert len(added) == 2
