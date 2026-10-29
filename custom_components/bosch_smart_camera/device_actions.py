"""One-shot device-action triggers: firmware install, soft/hard reset.

These are device-action triggers, not "fetch and cache data" work, so they
don't belong inline on the `DataUpdateCoordinator` subclass. Matches the
`tick_bootstrap`/`tick_failure`/`tick_housekeeping` pattern already
established in this package: free functions taking the coordinator
instance as their first argument.

`BoschCameraCoordinator` keeps a thin delegating method for each of
these (same name, calls straight into the matching function here) so
every existing call site — `update.py`'s Install button,
`repairs.py`'s Fix-flow, `button.py`'s soft/hard-reset buttons, and the
test suite's attribute-mocking/unbound-method-call patterns — keeps
working unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .lock_utils import get_or_create_lock

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator


async def async_install_firmware(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Install the pending firmware update for `cam_id` right now.

    Shared by two entry points: the `update` entity's Install button
    (update.py, BoschFirmwareUpdate.async_install) and the "Fix" action on
    the `firmware_update_available` Repairs issue (repairs.py) — one
    implementation so both stay in sync instead of duplicating the
    guard/write-lock logic.

    PUTs the same endpoint/payload the official Bosch app's "Update now"
    button uses (research/apk_2.12.0 decompile: FirmwareBackendService.
    UpdateCameraFirmware — {"id": <update field>} to the same URL this
    integration already GETs for status).
    """
    # Serializes the check-then-PUT-then-set sequence below across BOTH
    # call sites (update.py's Install button, repairs.py's Fix action) —
    # without this, a double-click or a race between the two could send
    # two overlapping install PUTs. The write-lock
    # timestamp set at the end guards against a LATER poll reverting the
    # optimistic state, not this — it's set only after the first PUT
    # already succeeded, so it can't prevent a second concurrent caller.
    async with get_or_create_lock(coordinator._firmware_install_locks, cam_id):
        fw = coordinator.firmware_cache.get(cam_id, {})
        if fw.get("updating"):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_install_in_progress",
            )
        target = fw.get("update")
        if not target:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_no_update_available",
            )
        ok = await coordinator.async_put_camera(cam_id, "firmware", {"id": target})
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_install_rejected",
                translation_placeholders={"target": str(target)},
            )
        fw["updating"] = True
        coordinator.firmware_cache[cam_id] = fw
        coordinator.firmware_set_at[cam_id] = time.monotonic()


async def async_soft_reset_camera(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Reboot the camera (soft reset).

    PUTs the same bodyless endpoint the official Bosch app's camera
    "Restart" action uses (research/apk_2.12.0 decompile:
    BackendUrlProviderService.GetCameraSoftResetUrl → PUT
    video_inputs/{id}/soft_reset). The camera briefly drops offline
    while it reboots; no local state to update here — the next
    status poll picks up the new online/offline state naturally.

    Live-tested against a real online camera: Bosch's
    cloud returned HTTP 404 sh:entity.notfound despite the request
    matching the app byte-for-byte — the button entity is disabled
    by default (button.py) for this reason.
    """
    ok = await coordinator.async_put_camera(cam_id, "soft_reset", None)
    if not ok:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="soft_reset_rejected",
        )


async def async_hard_reset_camera(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Factory-reset the camera (hard reset).

    PUTs the same bodyless endpoint the official Bosch app's camera
    "Factory Reset" action uses (research/apk_2.12.0 decompile:
    BackendUrlProviderService.GetCameraHardResetUrl → PUT
    video_inputs/{id}/hard_reset). Unlike soft reset, this is
    destructive — the camera loses its Bosch account pairing and
    must be re-commissioned from scratch via the Bosch app before it
    will work with this integration again. The button entity is
    disabled by default for exactly this reason (button.py).
    """
    ok = await coordinator.async_put_camera(cam_id, "hard_reset", None)
    if not ok:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="hard_reset_rejected",
        )
