"""Takeover of a pre-v18 `bosch_shc_camera` setup by `bosch_smart_camera`.

The domain was renamed; a config entry cannot change its domain, so the new
entry adopts the old entry's devices/entities (entity IDs stay) and the Store
files the old remove hook would delete.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_smart_camera.const import (
    DOMAIN,
    LEGACY_DOMAIN,
    STORAGE_PREFIX,
)
from custom_components.bosch_smart_camera.legacy_migration import (
    LEGACY_ENTRY_KEY,
    LEGACY_FOLDER_ISSUE,
    _move_registry_entries,
    async_migrate_legacy_entry,
    async_update_legacy_folder_issue,
    get_legacy_entry,
    legacy_subentries,
    register_legacy_service_aliases,
)

CAM = "11111111-2222-3333-4444-555555555555"


def _entries(hass: HomeAssistant) -> tuple[MockConfigEntry, MockConfigEntry]:
    legacy = MockConfigEntry(
        domain=LEGACY_DOMAIN, version=3, data={"bearer_token": "t"}, title="Old"
    )
    new = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={LEGACY_ENTRY_KEY: legacy.entry_id},
        title="New",
    )
    legacy.add_to_hass(hass)
    new.add_to_hass(hass)
    return legacy, new


def _seed_registry(hass: HomeAssistant, legacy: MockConfigEntry) -> tuple[str, str]:
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=legacy.entry_id,
        identifiers={(LEGACY_DOMAIN, CAM)},
        name="Terrasse",
    )
    entity = er.async_get(hass).async_get_or_create(
        "camera",
        LEGACY_DOMAIN,
        f"{STORAGE_PREFIX}_{CAM}",
        config_entry=legacy,
        device_id=device.id,
        suggested_object_id="terrasse",
    )
    return device.id, entity.entity_id


async def test_no_legacy_entry_is_a_noop(hass: HomeAssistant) -> None:
    new = MockConfigEntry(domain=DOMAIN, data={})
    new.add_to_hass(hass)
    assert get_legacy_entry(hass) is None
    assert await async_migrate_legacy_entry(hass, new) is False


async def test_fresh_login_does_not_adopt_a_legacy_entry(
    hass: HomeAssistant,
) -> None:
    """Only an entry created by the migrate step takes the old setup over."""
    legacy = MockConfigEntry(domain=LEGACY_DOMAIN, version=3, data={})
    fresh = MockConfigEntry(domain=DOMAIN, version=3, data={"bearer_token": "x"})
    legacy.add_to_hass(hass)
    fresh.add_to_hass(hass)
    _seed_registry(hass, legacy)

    assert await async_migrate_legacy_entry(hass, fresh) is False
    assert hass.config_entries.async_get_entry(legacy.entry_id) is legacy


async def test_marker_of_a_vanished_legacy_entry_is_cleared(
    hass: HomeAssistant,
) -> None:
    new = MockConfigEntry(domain=DOMAIN, data={LEGACY_ENTRY_KEY: "gone", "k": 1})
    new.add_to_hass(hass)
    assert await async_migrate_legacy_entry(hass, new) is False
    assert new.data == {"k": 1}


async def test_legacy_entry_is_unloaded_first_and_data_taken_over(
    hass: HomeAssistant,
) -> None:
    """A running legacy instance may have rotated tokens since the flow copied them."""
    legacy, new = _entries(hass)
    hass.config_entries.async_update_entry(
        legacy, data={"bearer_token": "rotated", "refresh_token": "new-r"}
    )
    calls: list[str] = []
    real_unload = hass.config_entries.async_unload

    async def unload(entry_id: str, *args: object, **kwargs: object) -> bool:
        calls.append("unload")
        return await real_unload(entry_id, *args, **kwargs)

    real_move = er.EntityRegistry.async_update_entity_platform

    def move(self: er.EntityRegistry, *args: object, **kwargs: object):
        calls.append("move")
        return real_move(self, *args, **kwargs)

    _seed_registry(hass, legacy)
    with (
        patch.object(hass.config_entries, "async_unload", unload),
        patch.object(er.EntityRegistry, "async_update_entity_platform", move),
    ):
        assert await async_migrate_legacy_entry(hass, new) is True

    assert calls[:2] == ["unload", "move"]
    assert new.data == {"bearer_token": "rotated", "refresh_token": "new-r"}


async def test_failed_unload_keeps_the_legacy_entry(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    legacy, new = _entries(hass)
    device_id, _ = _seed_registry(hass, legacy)
    for outcome in (False, HomeAssistantError("busy")):
        with patch.object(
            hass.config_entries,
            "async_unload",
            AsyncMock(
                side_effect=None if outcome is False else outcome, return_value=False
            ),
        ):
            assert await async_migrate_legacy_entry(hass, new) is False
    assert hass.config_entries.async_get_entry(legacy.entry_id) is legacy
    assert dr.async_get(hass).async_get(device_id).identifiers == {(LEGACY_DOMAIN, CAM)}
    assert "Could not unload" in caplog.text


async def test_incomplete_move_keeps_the_legacy_entry_and_the_stores(
    hass: HomeAssistant,
) -> None:
    legacy, new = _entries(hass)
    _seed_registry(hass, legacy)
    await Store(hass, version=1, key=f"{STORAGE_PREFIX}_lan_ips").async_save({"a": 1})
    with patch.object(
        er.EntityRegistry, "async_update_entity_platform", side_effect=ValueError
    ):
        assert await async_migrate_legacy_entry(hass, new) is False

    assert hass.config_entries.async_get_entry(legacy.entry_id) is legacy
    assert LEGACY_ENTRY_KEY not in new.data
    assert await Store(
        hass, version=1, key=f"{STORAGE_PREFIX}_lan_ips"
    ).async_load() == {"a": 1}


async def test_interrupted_migration_restores_the_stashed_stores(
    hass: HomeAssistant,
) -> None:
    """The legacy remove hook deleted the files, then HA stopped before the restore."""
    new = MockConfigEntry(domain=DOMAIN, data={})
    new.add_to_hass(hass)
    await Store(
        hass, version=1, key=f"{DOMAIN}_migration_stash", private=True
    ).async_save({f"{STORAGE_PREFIX}_local_creds": {"cam": "x"}, "unrelated": 1})

    assert await async_migrate_legacy_entry(hass, new) is False

    creds = await Store(
        hass, version=1, key=f"{STORAGE_PREFIX}_local_creds", private=True
    ).async_load()
    assert creds == {"cam": "x"}
    assert (
        await Store(
            hass, version=1, key=f"{DOMAIN}_migration_stash", private=True
        ).async_load()
        is None
    )


async def test_adopts_devices_and_entities_and_removes_legacy(
    hass: HomeAssistant,
) -> None:
    legacy, new = _entries(hass)
    device_id, entity_id = _seed_registry(hass, legacy)

    assert await async_migrate_legacy_entry(hass, new) is True

    assert hass.config_entries.async_get_entry(legacy.entry_id) is None
    device = dr.async_get(hass).async_get(device_id)
    assert device is not None
    assert device.identifiers == {(DOMAIN, CAM)}
    assert device.config_entries == {new.entry_id}
    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None
    assert entity.platform == DOMAIN
    assert entity.config_entry_id == new.entry_id
    assert entity.unique_id == f"{STORAGE_PREFIX}_{CAM}"


async def test_second_run_finds_nothing_left(hass: HomeAssistant) -> None:
    legacy, new = _entries(hass)
    _seed_registry(hass, legacy)
    await async_migrate_legacy_entry(hass, new)
    assert await async_migrate_legacy_entry(hass, new) is False


async def test_store_files_survive_the_legacy_remove_hook(
    hass: HomeAssistant,
) -> None:
    _, new = _entries(hass)
    await Store(hass, version=1, key=f"{STORAGE_PREFIX}_lan_ips").async_save(
        {"cam": "192.0.2.1"}
    )
    await Store(
        hass, version=1, key=f"{STORAGE_PREFIX}_local_creds", private=True
    ).async_save({"cam": {"user": "u"}})
    real_remove = hass.config_entries.async_remove

    async def remove_and_wipe(entry_id: str) -> dict:
        # What the still-installed legacy `async_remove_entry` does.
        for key in ("lan_ips", "local_creds"):
            await Store(hass, version=1, key=f"{STORAGE_PREFIX}_{key}").async_remove()
        return await real_remove(entry_id)

    with patch.object(hass.config_entries, "async_remove", remove_and_wipe):
        assert await async_migrate_legacy_entry(hass, new) is True

    lan = await Store(hass, version=1, key=f"{STORAGE_PREFIX}_lan_ips").async_load()
    creds = await Store(
        hass, version=1, key=f"{STORAGE_PREFIX}_local_creds", private=True
    ).async_load()
    assert lan == {"cam": "192.0.2.1"}
    assert creds == {"cam": {"user": "u"}}


async def test_entity_that_cannot_move_is_left_and_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    legacy, new = _entries(hass)
    _, entity_id = _seed_registry(hass, legacy)
    with patch.object(
        er.EntityRegistry, "async_update_entity_platform", side_effect=ValueError
    ):
        await async_migrate_legacy_entry(hass, new)
    assert f"Could not move to {DOMAIN}: {entity_id}" in caplog.text
    assert hass.config_entries.async_get_entry(legacy.entry_id) is legacy


async def test_device_identifier_collision_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    legacy, new = _entries(hass)
    device_id, _ = _seed_registry(hass, legacy)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=new.entry_id, identifiers={(DOMAIN, CAM)}
    )
    await async_migrate_legacy_entry(hass, new)
    assert device_id in caplog.text
    assert hass.config_entries.async_get_entry(legacy.entry_id) is legacy


async def test_legacy_subentries_are_copied(hass: HomeAssistant) -> None:
    legacy = MockConfigEntry(
        domain=LEGACY_DOMAIN,
        version=3,
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={"name": "Phone"},
                subentry_type="ai_target",
                title="Phone",
                unique_id="phone",
            )
        ],
    )
    copied = legacy_subentries(legacy)
    assert copied == [
        {
            "data": {"name": "Phone"},
            "subentry_type": "ai_target",
            "title": "Phone",
            "unique_id": "phone",
        }
    ]


async def test_folder_issue_follows_the_leftover_folder(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    (tmp_path / "custom_components" / LEGACY_DOMAIN).mkdir(parents=True)
    with patch.object(hass.config, "config_dir", str(tmp_path)):
        await async_update_legacy_folder_issue(hass)
        issue = ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_FOLDER_ISSUE)
        assert issue is not None
        assert issue.translation_placeholders == {
            "folder": f"custom_components/{LEGACY_DOMAIN}"
        }

        (tmp_path / "custom_components" / LEGACY_DOMAIN).rmdir()
        await async_update_legacy_folder_issue(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_FOLDER_ISSUE) is None


async def test_service_alias_forwards_and_warns(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[dict] = []

    async def handler(call: ServiceCall) -> dict:
        seen.append(dict(call.data))
        return {"ok": True}

    hass.services.async_register(
        DOMAIN, "ping", handler, supports_response=SupportsResponse.OPTIONAL
    )
    register_legacy_service_aliases(hass)

    with caplog.at_level(logging.WARNING):
        result = await hass.services.async_call(
            LEGACY_DOMAIN,
            "ping",
            {"camera_id": CAM},
            blocking=True,
            return_response=True,
        )
    assert seen == [{"camera_id": CAM}]
    assert result == {"ok": True}
    assert f"{LEGACY_DOMAIN}.ping is deprecated" in caplog.text


async def test_service_alias_does_not_replace_unless_forced(
    hass: HomeAssistant,
) -> None:
    calls: list[str] = []

    async def new_handler(call: ServiceCall) -> None:
        calls.append("new")

    async def old_handler(call: ServiceCall) -> None:
        calls.append("old")

    hass.services.async_register(DOMAIN, "ping", new_handler)
    hass.services.async_register(LEGACY_DOMAIN, "ping", old_handler)

    register_legacy_service_aliases(hass)
    await hass.services.async_call(LEGACY_DOMAIN, "ping", blocking=True)
    assert calls == ["old"]

    register_legacy_service_aliases(hass, force=True)
    await hass.services.async_call(LEGACY_DOMAIN, "ping", blocking=True)
    assert calls == ["old", "new"]


async def test_flow_offers_migration_and_copies_the_setup(
    hass: HomeAssistant,
) -> None:
    legacy = MockConfigEntry(
        domain=LEGACY_DOMAIN,
        version=3,
        title="Old",
        data={"bearer_token": "b", "refresh_token": "r"},
        options={"enable_snapshots": False},
        subentries_data=[
            config_entries.ConfigSubentryData(
                data={"name": "Phone"},
                subentry_type="ai_target",
                title="Phone",
                unique_id="phone",
            )
        ],
    )
    legacy.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "menu"
    assert result["menu_options"][0] == "migrate_legacy"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "migrate_legacy"}
    )
    assert result["type"] == "form"
    assert result["step_id"] == "migrate_legacy"

    with patch(
        "custom_components.bosch_smart_camera.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    assert result["title"] == "Old"
    assert result["data"] == {
        "bearer_token": "b",
        "refresh_token": "r",
        LEGACY_ENTRY_KEY: legacy.entry_id,
    }
    assert result["options"] == {"enable_snapshots": False}
    entry = result["result"]
    assert [s.subentry_type for s in entry.subentries.values()] == ["ai_target"]


async def test_flow_has_no_migration_without_legacy_entry(
    hass: HomeAssistant,
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert "migrate_legacy" not in result["menu_options"]


async def test_flow_hides_migration_for_an_unmigrated_legacy_entry(
    hass: HomeAssistant,
) -> None:
    MockConfigEntry(domain=LEGACY_DOMAIN, version=2, data={}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert "migrate_legacy" not in result["menu_options"]


async def test_migrate_step_aborts_when_the_legacy_entry_vanished(
    hass: HomeAssistant,
) -> None:
    legacy = MockConfigEntry(domain=LEGACY_DOMAIN, version=3, data={})
    legacy.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.async_remove(legacy.entry_id)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "migrate_legacy"}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "no_legacy_entry"


async def test_reattach_failure_is_logged(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    legacy, new = _entries(hass)
    _, entity_id = _seed_registry(hass, legacy)
    with patch.object(er.EntityRegistry, "async_update_entity", side_effect=ValueError):
        await async_migrate_legacy_entry(hass, new)
    assert entity_id in caplog.text
    assert hass.config_entries.async_get_entry(legacy.entry_id) is legacy


async def test_older_home_assistant_keeps_the_device_and_adds_the_entry(
    hass: HomeAssistant,
) -> None:
    """Before one-entry-per-device: the device gains the new entry, entities keep it."""
    legacy, new = _entries(hass)
    dev = MagicMock()
    dev.id = "dev1"
    dev.identifiers = {(LEGACY_DOMAIN, CAM), ("other", "x")}
    ent = MagicMock()
    ent.entity_id = "camera.terrasse"
    ent.device_id = "dev1"
    dev_reg = MagicMock()
    ent_reg = MagicMock()
    with (
        patch(
            "custom_components.bosch_smart_camera.legacy_migration._SINGLE_OWNER_DEVICES",
            False,
        ),
        patch.object(dr, "async_get", return_value=dev_reg),
        patch.object(er, "async_get", return_value=ent_reg),
        patch.object(dr, "async_entries_for_config_entry", return_value=[dev]),
        patch.object(er, "async_entries_for_config_entry", return_value=[ent]),
    ):
        _move_registry_entries(hass, legacy, new)

    dev_reg.async_update_device.assert_called_once_with(
        "dev1",
        new_identifiers={(DOMAIN, CAM), ("other", "x")},
        add_config_entry_id=new.entry_id,
    )
    ent_reg.async_update_entity_platform.assert_called_once_with(
        "camera.terrasse", DOMAIN, new_config_entry_id=new.entry_id
    )
    ent_reg.async_update_entity.assert_not_called()
