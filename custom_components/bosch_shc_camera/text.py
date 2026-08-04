"""Bosch Smart Home Camera — Text Platform.

Creates one `text` entity per camera:
  text.<cam>_ai_scene_context

Free-form scene-context prompt hint for the AI Camera Analysis feature
(`ai_analysis.py`'s `_build_prompt`) — e.g. "the gate on the left is
usually closed", "delivery drop-off spot is the porch table". Backed by
`coordinator.ai_analysis_scene_context[cam_id]`, restored across HA
restarts via `RestoreEntity` (the coordinator dict is purely in-memory,
purged on device removal — see coordinator.py's `_PURGE_CAM_DICT_ATTRS`).

Structural pattern mirrors `select.py` exactly (module-level
`async_setup_entry` + per-camera fan-out over `coordinator.data`,
`CoordinatorEntity` + native HA entity type + `RestoreEntity`) — this
integration puts ALL per-camera settings on entities, never in the
options-flow schema (see select.py's `BoschNvrModeSelect` docstring).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BoschCameraCoordinator
from .const import DOMAIN
from .dynamic_devices import register_dynamic_camera_listener

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

AI_SCENE_CONTEXT_MAX_LEN = 500


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up text entities for each camera."""
    coordinator: BoschCameraCoordinator = config_entry.runtime_data

    def _build_entities_for_cam(cam_id: str) -> list[Any]:
        return [BoschAiSceneContextText(coordinator, cam_id, config_entry)]

    known_cam_ids: set[str] = set(coordinator.data)
    entities: list[Any] = []
    for cam_id in known_cam_ids:
        entities.extend(_build_entities_for_cam(cam_id))
    async_add_entities(entities, update_before_add=False)

    # Quality-Scale Gold `dynamic-devices`.
    config_entry.async_on_unload(
        register_dynamic_camera_listener(
            coordinator, known_cam_ids, async_add_entities, _build_entities_for_cam
        )
    )


# ─────────────────────────────────────────────────────────────────────────────
class BoschAiSceneContextText(CoordinatorEntity, TextEntity, RestoreEntity):  # type: ignore[misc]
    """Text entity: free-form per-camera scene-context hint for the AI
    Camera Analysis prompt builder.

    Always registered (not gated on the `ai_analysis_enabled` master
    option) — the same "config surface exists independent of the master
    toggle" reasoning as `BoschAiAnalysisSwitch`, so a camera's context can
    be written before AI analysis is ever turned on.

    Purely local/in-memory state (`coordinator.ai_analysis_scene_context`)
    — no cloud/camera API call, so this entity is always available.
    """

    _attr_has_entity_name = True
    _attr_mode = TextMode.TEXT
    _attr_native_max = AI_SCENE_CONTEXT_MAX_LEN
    _attr_native_min = 0

    def __init__(
        self,
        coordinator: BoschCameraCoordinator,
        cam_id: str,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._cam_id = cam_id
        cam_data = coordinator.data.get(cam_id, {})
        cam_info = cam_data.get("info", {})
        self._cam_title = cam_info.get("title", cam_id)
        self._entry = entry
        self._attr_unique_id = f"bosch_shc_camera_{cam_id}_ai_scene_context"
        self._attr_translation_key = "ai_scene_context"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_added_to_hass(self) -> None:
        """Restore the last scene-context text after HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            None,
            "unknown",
            "unavailable",
        ):
            self.coordinator.ai_analysis_scene_context[self._cam_id] = last_state.state

    @property
    def device_info(self) -> dict[str, Any]:
        cam_data = self.coordinator.data.get(self._cam_id, {})
        cam_info = cam_data.get("info", {})
        return {
            "identifiers": {(DOMAIN, self._cam_id)},
            "name": f"Bosch {self._cam_title}",
            "manufacturer": "Bosch",
            "model": cam_info.get("hardwareVersion", "Smart Home Camera"),
            "sw_version": cam_info.get("firmwareVersion", ""),
        }

    @property
    def available(self) -> bool:
        # Purely local state — never gated on camera online/offline.
        return bool(self.coordinator.last_update_success)

    @property
    def native_value(self) -> str | None:
        value: str = self.coordinator.ai_analysis_scene_context.get(self._cam_id, "")
        return value

    async def async_set_value(self, value: str) -> None:
        """Store the new scene-context text (truncated to the max length —
        HA's own selector already enforces this client-side, but a
        service-call / REST-API caller could bypass that)."""
        self.coordinator.ai_analysis_scene_context[self._cam_id] = value[
            :AI_SCENE_CONTEXT_MAX_LEN
        ]
        self.async_write_ha_state()
