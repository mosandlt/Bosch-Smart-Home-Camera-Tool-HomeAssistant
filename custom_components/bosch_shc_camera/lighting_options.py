"""Serialized read-modify-write of /v11/video_inputs/{id}/lighting_options.

The endpoint only exists on Gen1 Eyes Outdoor. Bosch needs the complete object
on every PUT, so each write is GET → change one field → PUT. Two overlapping
writes (select + service, double click) would otherwise interleave their GETs
and PUT back a stale copy of each other's change, so all writers share one
lock per camera.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .guards import wrap_service_errors

# Values of the app's Illumination.ScheduleStatusOption enum.
SCHEDULE_STATUS_OPTIONS = [
    "follow_schedule",
    "ambient_light",
    "ambient_light_and_schedule",
    "always_off",
]


def lighting_write_lock(coordinator: Any, cam_id: str) -> asyncio.Lock:
    """Return the per-camera lock shared by every lighting_options writer."""
    locks: dict[str, asyncio.Lock] = vars(coordinator).setdefault(
        "lighting_write_locks", {}
    )
    return locks.setdefault(cam_id, asyncio.Lock())


def cached_if_write_locked(coordinator: Any, cam_id: str) -> dict[str, Any] | None:
    """Return our own last PUT payload while the cloud may still serve the old one.

    Right after a write, a GET can still return the pre-write object; merging
    onto it would silently undo that write. Inside the write-lock window the
    cached copy (what we just sent) is the authoritative base instead.
    """
    cached = coordinator.lighting_options_cache.get(cam_id)
    if (
        cached
        and coordinator.is_write_locked(cam_id, coordinator.lighting_options_set_at)
        is True
    ):
        return dict(cached)
    return None


def _http_error(action: str, status: int, body: str) -> HomeAssistantError:
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="http_error_with_body",
        translation_placeholders={
            "action": action,
            "status": str(status),
            "body": body[:200],
        },
    )


async def _persist_snapshot(coordinator: Any) -> None:
    store = getattr(coordinator, "lighting_snapshot_store", None)
    if store is not None:
        await store.async_save(coordinator.lighting_mode_light_snapshot)


async def _restore_lights(coordinator: Any, cam_id: str) -> None:
    """Switch back on what was on before always_off; failures stay queued."""
    pending = coordinator.lighting_mode_light_snapshot.get(cam_id, {})
    for component, was_on in list(pending.items()):
        if not was_on or await coordinator.async_cloud_set_light_component(
            cam_id, component, True
        ):
            del pending[component]
    if not pending:
        coordinator.lighting_mode_light_snapshot.pop(cam_id, None)
    await _persist_snapshot(coordinator)
    if pending:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="unexpected_error",
            translation_placeholders={
                "action": "Restore lights after lighting mode change",
                "error": "light write failed, will retry on the next mode change",
            },
        )


async def async_set_schedule_status(coordinator: Any, cam_id: str, option: str) -> None:
    """Set only ``scheduleStatus``; mode, threshold and schedule stay untouched."""
    # Local import (not top-level): same reason as services.py — patches target
    # the package namespace.
    from . import CLOUD_API, async_get_bosch_cloud_session

    if option not in SCHEDULE_STATUS_OPTIONS:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="unexpected_error",
            translation_placeholders={
                "action": "Set lighting mode",
                "error": f"unknown option {option}",
            },
        )
    url = f"{CLOUD_API}/v11/video_inputs/{cam_id}/lighting_options"
    async with lighting_write_lock(coordinator, cam_id):
        session = await async_get_bosch_cloud_session(coordinator.hass)
        headers = {
            "Authorization": f"Bearer {coordinator.token}",
            "Content-Type": "application/json",
        }
        with wrap_service_errors("Set lighting mode"):
            data = cached_if_write_locked(coordinator, cam_id)
            if data is None:
                async with asyncio.timeout(10):
                    async with session.get(url, headers=headers) as get_resp:
                        if get_resp.status != 200:
                            raise _http_error(
                                "Set lighting mode (fetch current)",
                                get_resp.status,
                                await get_resp.text(),
                            )
                        data = await get_resp.json()
            # Compared under the lock against the freshest state, so a queued
            # request is never dropped because of a stale pre-lock comparison.
            previous = str(data.get("scheduleStatus", "")).upper()
            if previous == option.upper():
                coordinator.lighting_options_cache[cam_id] = data
                return
            if option == "always_off":
                # always_off makes the camera drop the lights that were switched
                # on; remember them to put them back when leaving that mode.
                lights = coordinator.shc_state_cache.get(cam_id, {})
                coordinator.lighting_mode_light_snapshot[cam_id] = {
                    "wallwasher": bool(lights.get("wallwasher")),
                    "front": bool(lights.get("front_light")),
                }
                await _persist_snapshot(coordinator)
            data["scheduleStatus"] = option.upper()
            async with asyncio.timeout(10):
                async with session.put(url, headers=headers, json=data) as put_resp:
                    if put_resp.status not in (200, 204):
                        raise _http_error(
                            "Set lighting mode", put_resp.status, await put_resp.text()
                        )
            # Optimistic cache + write-lock, same pattern as set_lighting_schedule:
            # a slow-tier poll must not revert it before the cloud catches up.
            coordinator.lighting_options_cache[cam_id] = data
            coordinator.lighting_options_set_at[cam_id] = time.monotonic()
            if previous == "ALWAYS_OFF":
                await _restore_lights(coordinator, cam_id)
        await coordinator.async_request_refresh()
