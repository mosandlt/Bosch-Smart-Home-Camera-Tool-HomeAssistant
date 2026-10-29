"""Shared helper for the `dynamic-devices` Quality-Scale (Gold) rule.

Every platform builds its entities ONCE in `async_setup_entry`, keyed by the
camera IDs present in `coordinator.data` at HA startup. Without this helper,
a camera added to the Bosch account after HA is already running never gets
entities — the user has to reload the integration manually
(https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/dynamic-devices/).

Usage in a platform module::

    async def async_setup_entry(hass, config_entry, async_add_entities):
        coordinator = config_entry.runtime_data

        def _build_entities_for_cam(cam_id: str) -> list[Entity]:
            return [BoschSomeEntity(coordinator, cam_id, config_entry), ...]

        known_cam_ids: set[str] = set(coordinator.data)
        entities: list[Entity] = []
        for cam_id in known_cam_ids:
            entities.extend(_build_entities_for_cam(cam_id))
        async_add_entities(entities, update_before_add=False)

        config_entry.async_on_unload(
            register_dynamic_camera_listener(
                coordinator, known_cam_ids, async_add_entities, _build_entities_for_cam
            )
        )

`_build_entities_for_cam` MUST be the exact same per-camera entity-
construction logic used for the initial pass — pulled out into a closure
purely so both the initial and the dynamic-add path share one definition
(no duplicated entity lists to drift apart). Any account-level entity (one
per config entry, not per camera — e.g. sensor.py's FCM-push-status sensor)
must stay OUTSIDE `_build_entities_for_cam` and only ever be added during
the initial pass, never re-added by the listener.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator


def register_dynamic_camera_listener(
    coordinator: BoschCameraCoordinator | Any,
    known_cam_ids: set[str],
    async_add_entities: AddEntitiesCallback,
    build_entities_for_cam: Callable[[str], list[Entity]],
) -> Callable[[], None]:
    """Register + return a coordinator-update listener that adds entities
    for any `cam_id` newly seen in `coordinator.data`.

    The caller is responsible for wiring the returned unsubscribe callback
    into `config_entry.async_on_unload(...)` — this function does not do
    that itself, matching `coordinator.async_add_listener`'s own contract.

    `known_cam_ids` MUST already contain every cam_id built during the
    initial `async_setup_entry` pass before this is called — it is mutated
    in place (new cam_ids are added to it) so repeated ticks don't re-add
    the same camera's entities.
    """

    @callback  # type: ignore[untyped-decorator]  # HA @callback is untyped (no py.typed)
    def _check_for_new_cameras() -> None:
        data = coordinator.data
        if not data:
            return
        new_cam_ids = set(data) - known_cam_ids
        if not new_cam_ids:
            return
        new_entities: list[Entity] = []
        # Sorted for deterministic ordering (matters for tests + logs, not
        # for correctness — HA doesn't care about entity add order).
        for cam_id in sorted(new_cam_ids):
            known_cam_ids.add(cam_id)
            new_entities.extend(build_entities_for_cam(cam_id))
        if new_entities:
            async_add_entities(new_entities, update_before_add=False)

    unsub: Callable[[], None] = coordinator.async_add_listener(_check_for_new_cameras)
    return unsub
