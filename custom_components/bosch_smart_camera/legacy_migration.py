"""Takeover of a pre-v18 `bosch_shc_camera` setup by the `bosch_smart_camera` domain.

The integration was renamed to match Home Assistant core. A config entry cannot
change its domain, so the new entry adopts the old entry's devices and entities
(entity IDs, history, areas and customisations stay) and then removes the old
entry. Storage keys and unique IDs keep their legacy names on purpose.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentryData
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import DOMAIN, LEGACY_DOMAIN, STORAGE_PREFIX

_LOGGER = logging.getLogger(__name__)

LEGACY_FOLDER_ISSUE = "legacy_folder"

# Marks an entry created by the "migrate" flow step; only such an entry adopts the
# legacy setup (a fresh login with another account must not take it over).
LEGACY_ENTRY_KEY = "migrate_from_legacy_entry"
_STASH_KEY = f"{DOMAIN}_migration_stash"

# Home Assistant 2026.8 moved to one config entry per device (`new_config_entry_id`).
_SINGLE_OWNER_DEVICES = (
    "new_config_entry_id"
    in inspect.signature(dr.DeviceRegistry.async_update_device).parameters
)

# Store files the legacy `async_remove_entry` deletes: they hold LAN credentials,
# LAN IPs and the Mini-NVR intent, which the new entry must inherit.
_STASHED_STORES: tuple[tuple[str, bool], ...] = (
    (f"{STORAGE_PREFIX}_cloud_alert_state", False),
    (f"{STORAGE_PREFIX}_lan_ips", False),
    (f"{STORAGE_PREFIX}_hw_versions", False),
    (f"{STORAGE_PREFIX}_local_creds", True),
    (f"{STORAGE_PREFIX}_nvr_user_intent", False),
)


def get_legacy_entry(hass: HomeAssistant) -> ConfigEntry | None:
    """Return the config entry of the pre-v18 domain, if one still exists."""
    entries = hass.config_entries.async_entries(LEGACY_DOMAIN)
    return entries[0] if entries else None


def legacy_subentries(legacy: ConfigEntry) -> list[ConfigSubentryData]:
    """Copy the legacy entry's subentries (AI targets/visitors) for a new entry."""
    return [
        ConfigSubentryData(
            data=dict(sub.data),
            subentry_type=sub.subentry_type,
            title=sub.title,
            unique_id=sub.unique_id,
        )
        for sub in legacy.subentries.values()
    ]


def _move_registry_entries(
    hass: HomeAssistant, legacy: ConfigEntry, entry: ConfigEntry
) -> bool:
    """Point devices and entities of the legacy entry at the new entry.

    Newer Home Assistant lets a device belong to exactly one config entry and
    deletes entities that no longer match it, so the entities are detached
    while the device moves. Older versions keep both entries on the device until
    the legacy entry is removed, which clears that last link.

    Returns False when anything could not be moved; the caller then keeps the
    legacy entry, because removing it would delete what is left behind.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    detach = _SINGLE_OWNER_DEVICES
    reattach: list[tuple[str, str]] = []
    failed: list[str] = []

    def move_entities() -> None:
        for entity in er.async_entries_for_config_entry(ent_reg, legacy.entry_id):
            device_id = entity.device_id
            detached = detach and device_id is not None
            extra: dict[str, Any] = {"new_device_id": None} if detached else {}
            try:
                ent_reg.async_update_entity_platform(
                    entity.entity_id,
                    DOMAIN,
                    new_config_entry_id=entry.entry_id,
                    **extra,
                )
            except ValueError:
                failed.append(entity.entity_id)
            else:
                if detached and device_id is not None:
                    reattach.append((entity.entity_id, device_id))

    if detach:
        move_entities()

    for device in dr.async_entries_for_config_entry(dev_reg, legacy.entry_id):
        identifiers = {
            (DOMAIN, ident) if domain == LEGACY_DOMAIN else (domain, ident)
            for domain, ident in device.identifiers
        }
        entry_change = (
            {"new_config_entry_id": entry.entry_id}
            if detach
            else {"add_config_entry_id": entry.entry_id}
        )
        try:
            dev_reg.async_update_device(
                device.id, new_identifiers=identifiers, **entry_change
            )
        except (ValueError, dr.DeviceIdentifierCollisionError):
            failed.append(device.id)

    if not detach:
        move_entities()

    for entity_id, device_id in reattach:
        try:
            ent_reg.async_update_entity(entity_id, device_id=device_id)
        except ValueError:
            failed.append(entity_id)

    if failed:
        _LOGGER.warning("Could not move to %s: %s", DOMAIN, ", ".join(failed))
    return not failed


async def _async_stash_stores(hass: HomeAssistant) -> None:
    """Persist the stores the legacy remove hook deletes, to survive a crash."""
    stash: dict[str, Any] = {}
    for key, private in _STASHED_STORES:
        data = await Store(hass, version=1, key=key, private=private).async_load()
        if data is not None:
            stash[key] = data
    if stash:
        await Store(hass, version=1, key=_STASH_KEY, private=True).async_save(stash)


async def _async_restore_stash(hass: HomeAssistant) -> None:
    """Write stashed stores back (also finishes a migration a crash interrupted)."""
    stash_store = Store[dict[str, Any]](hass, version=1, key=_STASH_KEY, private=True)
    stash = await stash_store.async_load()
    if not stash:
        return
    for key, private in _STASHED_STORES:
        if key in stash:
            await Store(hass, version=1, key=key, private=private).async_save(
                stash[key]
            )
    await stash_store.async_remove()


def _clear_marker(
    hass: HomeAssistant, entry: ConfigEntry, legacy: ConfigEntry | None
) -> None:
    """Drop the migrate marker, taking over the legacy entry's current data."""
    data = {**entry.data, **(dict(legacy.data) if legacy else {})}
    data.pop(LEGACY_ENTRY_KEY, None)
    hass.config_entries.async_update_entry(entry, data=data)


async def async_migrate_legacy_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Adopt the registry entries of the legacy entry this entry was migrated from.

    Returns True when the legacy entry was taken over and removed. Safe to call
    on every setup: without the marker (or after a finished run) it is a no-op.
    """
    await _async_restore_stash(hass)

    legacy_id = entry.data.get(LEGACY_ENTRY_KEY)
    if legacy_id is None:
        return False
    legacy = hass.config_entries.async_get_entry(legacy_id)
    if legacy is None or legacy.domain != LEGACY_DOMAIN:
        _clear_marker(hass, entry, None)
        return False

    # Loaded entities cannot change platform, and a running legacy instance
    # would rotate the tokens we are about to copy.
    try:
        unloaded = await hass.config_entries.async_unload(legacy.entry_id)
    except HomeAssistantError:
        unloaded = False
    if not unloaded:
        _LOGGER.warning("Could not unload the %s entry; keeping it", LEGACY_DOMAIN)
        return False

    await _async_stash_stores(hass)
    complete = _move_registry_entries(hass, legacy, entry)
    _clear_marker(hass, entry, legacy)
    if not complete:
        await _async_restore_stash(hass)
        return False

    await hass.config_entries.async_remove(legacy.entry_id)
    # A still-installed legacy copy of the integration deletes these files in
    # its remove hook.
    await _async_restore_stash(hass)

    _LOGGER.info("Adopted devices and entities of the %s entry", LEGACY_DOMAIN)
    return True


def _remove_legacy_folder(folder: Path) -> bool:
    """Delete the leftover legacy folder; False when it is not safe or fails."""
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("domain") != LEGACY_DOMAIN:
            return False
        shutil.rmtree(folder)
    except (OSError, ValueError):
        return False
    return True


async def async_update_legacy_folder_issue(
    hass: HomeAssistant, *, remove: bool = True
) -> None:
    """Delete a leftover `bosch_shc_camera` folder, or tell the user to.

    Deleted only when no legacy entry is left and `remove` is set (not in the
    run that just migrated: the legacy code is still loaded then). When the
    deletion fails the user gets a repair hint instead.
    """
    folder = Path(hass.config.path("custom_components", LEGACY_DOMAIN))
    if await hass.async_add_executor_job(folder.is_dir):
        if (
            remove
            and not hass.config_entries.async_entries(LEGACY_DOMAIN)
            and await hass.async_add_executor_job(_remove_legacy_folder, folder)
        ):
            _LOGGER.info(
                "Removed the unused folder custom_components/%s", LEGACY_DOMAIN
            )
            ir.async_delete_issue(hass, DOMAIN, LEGACY_FOLDER_ISSUE)
            return
        ir.async_create_issue(
            hass,
            DOMAIN,
            LEGACY_FOLDER_ISSUE,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=LEGACY_FOLDER_ISSUE,
            translation_placeholders={"folder": f"custom_components/{LEGACY_DOMAIN}"},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, LEGACY_FOLDER_ISSUE)


def register_legacy_service_aliases(
    hass: HomeAssistant, *, force: bool = False
) -> None:
    """Keep `bosch_shc_camera.<service>` working for automations that call it.

    `force` replaces handlers a still-installed legacy copy registered itself.
    """
    for name, service in hass.services.async_services_for_domain(DOMAIN).items():
        if hass.services.has_service(LEGACY_DOMAIN, name):
            if not force:
                continue
            hass.services.async_remove(LEGACY_DOMAIN, name)

        async def _alias(call: ServiceCall, _name: str = name) -> Any:
            _LOGGER.warning(
                "Service %s.%s is deprecated, use %s.%s",
                LEGACY_DOMAIN,
                _name,
                DOMAIN,
                _name,
            )
            return await hass.services.async_call(
                DOMAIN,
                _name,
                dict(call.data),
                blocking=True,
                context=call.context,
                return_response=call.return_response,
            )

        hass.services.async_register(
            LEGACY_DOMAIN,
            name,
            _alias,
            schema=None,
            supports_response=service.supports_response,
        )
