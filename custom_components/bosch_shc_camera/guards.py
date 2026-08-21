"""Shared hardware-generation and privacy-mode guard utilities.

These helpers are used across multiple entity platforms (switch, number, select,
light). Extracted from switch.py to break the import cycle where those modules
imported from switch at function-call time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError, Unauthorized

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import Context, HomeAssistant, ServiceCall

_LOGGER = logging.getLogger(__name__)


async def async_require_admin(hass: HomeAssistant, context: Context | None) -> None:
    """Raise Unauthorized unless ``context`` resolves to an admin user.

    Shared by the switch-entity admin gate (`_BoschSwitchBase._require_admin`)
    and the account/destructive-action services in `services.py`
    (hacs/default#8181 review, round 2). Fails closed unconditionally: no
    context, no ``user_id``, an unresolvable ``user_id``, or a resolved
    non-admin user are ALL rejected — including the no-user_id case
    (automations/scripts), unlike HA core's own
    `helpers.service.async_register_admin_service`, which treats a missing
    user_id as implicitly trusted. That's deliberately stricter here: every
    caller of an admin-gated service must be traceable to an actual admin
    user, the same way a direct frontend call is.
    """
    user_id = context.user_id if context is not None else None
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if user is None or not user.is_admin:
        raise Unauthorized(context=context)


def admin_only_service(
    hass: HomeAssistant,
    handler: Callable[[ServiceCall], Awaitable[Any]],
) -> Callable[[ServiceCall], Awaitable[Any]]:
    """Wrap a service handler so it rejects any non-admin caller.

    Applied to every service this integration registers directly via
    `hass.services.async_register` (`services.py`) — several of them are
    account-level or destructive (`share_camera`, `invite_friend`,
    `remove_friend`, `delete_event`, the `*_rule` set, …). Uses
    `async_require_admin` rather than
    `homeassistant.helpers.service.async_register_admin_service` because
    that helper's default schema (`vol.Schema({}, extra=vol.PREVENT_EXTRA)`)
    would reject every call carrying service data unless every handler's
    schema were rebuilt to match — this wrapper leaves the existing
    `hass.services.async_register` call (and its permissive default
    schema) untouched.
    """

    async def _wrapped(call: ServiceCall) -> Any:
        await async_require_admin(hass, call.context)
        return await handler(call)

    return _wrapped


_GEN2_INDOOR_HW = {"HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"}
_INDOOR_HW = {"INDOOR", "CAMERA_360", "HOME_Eyes_Indoor", "CAMERA_INDOOR_GEN2"}


def _get_cam_lock(coordinator: Any, lock_attr: str, cam_id: str) -> asyncio.Lock:
    """Return (lazily creating) a per-camera asyncio.Lock stored on the
    coordinator under ``lock_attr``, keyed by ``cam_id``.

    Several entity classes across switch.py/number.py/light.py can share one
    Bosch cloud endpoint that requires a full-body PUT (multiple sibling
    fields in one object — e.g. audioEnabled+speakerLevel+microphoneLevel on
    /audio). Concurrent read-modify-write calls for two different fields on
    the SAME endpoint must serialize on the SAME lock instance and merge only
    their own field back into the shared cache afterward, or one write's
    stale snapshot can silently revert the other's just-written field.
    """
    locks: dict[str, asyncio.Lock] | None = getattr(coordinator, lock_attr, None)
    if locks is None:
        locks = {}
        setattr(coordinator, lock_attr, locks)
    lock = locks.get(cam_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[cam_id] = lock
    return lock


def _is_gen2_indoor(entity: Any) -> bool:
    """Return True if the entity's camera is a Gen2 Indoor model."""
    hw = (
        entity.coordinator.data.get(entity._cam_id, {})
        .get("info", {})
        .get("hardwareVersion", "")
    )
    return hw in _GEN2_INDOOR_HW


async def _warn_if_privacy_on(entity: Any, feature_name: str) -> bool:
    """Show a persistent notification when the user tries to change a
    privacy-gated setting while privacy mode is ON. Returns True if the
    write was blocked.

    The Bosch cloud API returns HTTP 443 "sh:camera.in.privacy.mode" on
    reads and writes to /intrusionDetectionConfig, /zones, /privateAreas,
    /motion, and some lighting endpoints while the camera is in privacy
    mode. Without a guard the write silently fails in the logs; with this
    guard the user sees a clear notification explaining why.
    """
    coordinator = entity.coordinator
    cam_id = entity._cam_id
    cache = coordinator.shc_state_cache.get(cam_id, {})
    privacy_on = bool(cache.get("privacy_mode"))
    if not privacy_on:
        return False
    cam_title = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    _LOGGER.warning(
        "%s write blocked for %s — camera is in privacy mode (HTTP 443 would follow).",
        feature_name,
        cam_title,
    )
    try:
        await entity.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": f"{feature_name} — Kamera im Privacy-Mode",
                "message": (
                    f"Die Einstellung **{feature_name}** für **{cam_title}** kann nicht "
                    f"geändert werden, solange der Privacy-Mode aktiv ist.\n\n"
                    f"Die Kamera liefert in diesem Zustand `HTTP 443 sh:camera.in.privacy.mode` "
                    f"auf Schreibzugriffe. Schalte zuerst den Privacy-Mode aus "
                    f"(`switch.bosch_{cam_title.lower()}_privacy_mode`) und versuche es erneut."
                ),
                "notification_id": f"bosch_privacy_blocked_{cam_id}",
            },
            blocking=False,
        )
    except Exception as err:
        _LOGGER.debug("persistent_notification create failed: %s", err)
    return True


@contextmanager
def wrap_service_errors(action: str) -> Iterator[None]:
    """Translate any non-HomeAssistantError raised inside the block into a
    HomeAssistantError with a consistent ``unexpected_error`` translation key.

    Deduplicates the identical try/except HomeAssistantError-passthrough /
    except Exception-wrap pattern that used to be repeated at every HTTP-call
    site in services.py (style audit, 2026-08-05). A plain sync
    `@contextmanager` works fine inside `async def` handlers since exception
    translation itself needs no `await`.
    """
    try:
        yield
    except HomeAssistantError:
        raise
    except Exception as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="unexpected_error",
            translation_placeholders={"action": action, "error": str(err)},
        ) from err
