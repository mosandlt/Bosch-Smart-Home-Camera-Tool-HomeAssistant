"""Bridge release: hand the setup over to `bosch_smart_camera` once installed.

Pins: nothing happens without the new folder, with an existing new entry, after
a first attempt, or for a disabled entry; otherwise the import flow starts once.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import handover

MOD = "custom_components.bosch_shc_camera.handover"


def _hass(entries: list[object], installed: bool) -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_entries = MagicMock(return_value=entries)
    hass.config_entries.flow.async_init = AsyncMock(
        return_value={"type": "create_entry"}
    )
    hass.async_add_executor_job = AsyncMock(return_value=installed)
    return hass


def _store(loaded: object) -> MagicMock:
    store = MagicMock()
    store.async_load = AsyncMock(return_value=loaded)
    store.async_save = AsyncMock()
    return store


@pytest.mark.asyncio
async def test_new_integration_missing_does_nothing() -> None:
    hass = _hass([], installed=False)
    await handover._async_run_handover(hass)
    hass.config_entries.flow.async_init.assert_not_called()


@pytest.mark.asyncio
async def test_existing_new_entry_does_nothing() -> None:
    hass = _hass([object()], installed=True)
    await handover._async_run_handover(hass)
    hass.config_entries.flow.async_init.assert_not_called()


@pytest.mark.asyncio
async def test_already_attempted_does_nothing() -> None:
    hass = _hass([], installed=True)
    with patch(f"{MOD}.Store", return_value=_store({"started": True})):
        await handover._async_run_handover(hass)
    hass.config_entries.flow.async_init.assert_not_called()


@pytest.mark.asyncio
async def test_starts_import_flow_once_and_saves_flag_first() -> None:
    hass = _hass([], installed=True)
    store = _store(None)
    with patch(f"{MOD}.Store", return_value=store):
        await handover._async_run_handover(hass)
    store.async_save.assert_awaited_once_with({"started": True})
    hass.config_entries.flow.async_init.assert_awaited_once_with(
        "bosch_smart_camera", context={"source": "import"}, data={}
    )


@pytest.mark.asyncio
async def test_flow_error_is_swallowed() -> None:
    hass = _hass([], installed=True)
    hass.config_entries.flow.async_init = AsyncMock(side_effect=RuntimeError("boom"))
    with patch(f"{MOD}.Store", return_value=_store(None)):
        await handover._async_run_handover(hass)


def test_disabled_entry_not_scheduled() -> None:
    hass = _hass([], installed=True)
    entry = MagicMock()
    entry.disabled_by = "user"
    with patch(f"{MOD}.async_at_started") as at_started:
        handover.async_schedule_handover(hass, entry)
    at_started.assert_not_called()


def test_scheduled_only_once_per_run() -> None:
    hass = _hass([], installed=True)
    entry = MagicMock()
    entry.disabled_by = None
    with patch(f"{MOD}.async_at_started") as at_started:
        handover.async_schedule_handover(hass, entry)
        handover.async_schedule_handover(hass, entry)
    at_started.assert_called_once()


def test_new_integration_installed_checks_manifest(tmp_path) -> None:
    hass = MagicMock()
    hass.config.path = lambda *parts: str(tmp_path.joinpath(*parts))
    assert handover._new_integration_installed(hass) is False
    manifest = tmp_path / "custom_components" / "bosch_smart_camera" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    assert handover._new_integration_installed(hass) is True


@pytest.mark.asyncio
async def test_started_callback_creates_background_task() -> None:
    hass = _hass([], installed=True)
    entry = MagicMock()
    entry.disabled_by = None
    entry.domain = "bosch_shc_camera"
    with (
        patch(f"{MOD}.async_at_started") as at_started,
        patch(f"{MOD}._async_run_handover", new=MagicMock(return_value="coro")),
    ):
        handover.async_schedule_handover(hass, entry)
        await at_started.call_args.args[1](hass)
    hass.async_create_background_task.assert_called_once_with(
        "coro", "bosch_shc_camera hand-over"
    )
