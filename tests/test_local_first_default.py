"""Regression tests for the v12.4.3 LOCAL-first default flip.

DEFAULT_OPTIONS['stream_connection_type'] changed from 'auto' to 'local'.
Migration logic preserves the legacy 'auto' default for existing entries
that never explicitly set the option, so users don't silently lose
REMOTE-fallback on upgrade.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_default_options_stream_connection_type_is_local():
    """New installs (no explicit stream_connection_type set) get local-only."""
    from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
    assert DEFAULT_OPTIONS["stream_connection_type"] == "local"


@pytest.mark.asyncio
async def test_get_options_returns_local_for_empty_entry():
    """Entries with empty options dict resolve to local-only via DEFAULT_OPTIONS."""
    from custom_components.bosch_shc_camera import get_options
    entry = SimpleNamespace(options={})
    opts = get_options(entry)
    assert opts["stream_connection_type"] == "local"


@pytest.mark.asyncio
async def test_get_options_preserves_explicit_auto():
    """Explicit user choice of 'auto' (cloud-fallback) is preserved over the
    new local default. Pins the AUTO mode: existing users on AUTO keep their
    REMOTE-fallback safety net even after the v12.4.2 default flip."""
    from custom_components.bosch_shc_camera import get_options
    entry = SimpleNamespace(options={"stream_connection_type": "auto"})
    opts = get_options(entry)
    assert opts["stream_connection_type"] == "auto"


@pytest.mark.asyncio
async def test_get_options_preserves_explicit_local():
    """Explicit user choice of 'local' is preserved. Pins the LOCAL mode:
    a user who explicitly picked LOCAL keeps pure-LAN behaviour, no cloud
    round-trip on the stream path."""
    from custom_components.bosch_shc_camera import get_options
    entry = SimpleNamespace(options={"stream_connection_type": "local"})
    opts = get_options(entry)
    assert opts["stream_connection_type"] == "local"


@pytest.mark.asyncio
async def test_get_options_preserves_explicit_remote():
    """Explicit user choice of 'remote' is preserved. Pins the REMOTE mode:
    a user who explicitly picked REMOTE (cloud-only, e.g. LAN never works
    for their network setup) keeps that behaviour."""
    from custom_components.bosch_shc_camera import get_options
    entry = SimpleNamespace(options={"stream_connection_type": "remote"})
    opts = get_options(entry)
    assert opts["stream_connection_type"] == "remote"


@pytest.mark.asyncio
async def test_migration_v1_to_v2_preserves_legacy_auto():
    """Existing entry without explicit stream_connection_type gets auto on migration."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    captured: dict = {}

    def _update_entry(entry, **kwargs):
        captured.update(kwargs)
        entry.options = kwargs.get("options", entry.options)
        entry.version = kwargs.get("version", entry.version)

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="existing-install",
        version=1,
        options={"enable_snapshots": True},  # no stream_connection_type
    )

    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["version"] == 3
    assert captured["options"]["stream_connection_type"] == "auto"
    # And the rest of options is preserved
    assert captured["options"]["enable_snapshots"] is True


@pytest.mark.asyncio
async def test_migration_v1_to_v2_preserves_explicit_choice():
    """Existing entry with explicit 'local' or 'remote' stays as-is."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    captured: dict = {}

    def _update_entry(entry, **kwargs):
        captured.update(kwargs)

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="explicit-local",
        version=1,
        options={"stream_connection_type": "remote"},
    )

    result = await async_migrate_entry(hass, entry)
    assert result is True
    # Only version bump — user choice preserved untouched
    assert captured["options"]["stream_connection_type"] == "remote"


@pytest.mark.asyncio
async def test_migration_v2_to_v3_is_noop_for_clean_entry():
    """A v2 entry with no legacy fcm_push_mode is bumped to v3 without changing options."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    captured: dict = {}

    def _update_entry(entry, **kwargs):
        captured.update(kwargs)
        entry.version = kwargs.get("version", entry.version)
        entry.options = kwargs.get("options", entry.options)

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="new-install",
        version=2,
        options={},
    )

    result = await async_migrate_entry(hass, entry)
    assert result is True
    # v2→v3 migration bumps version; options unchanged (no legacy fcm_push_mode)
    assert captured.get("version") == 3
    assert captured.get("options") == {}


@pytest.mark.asyncio
async def test_migration_already_v3_is_noop():
    """A v3 entry (current version) should not be touched by migration."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    captured: dict = {}

    def _update_entry(entry, **kwargs):
        captured.update(kwargs)

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="current-version",
        version=3,
        options={},
    )

    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured == {}  # no update call for already-current entry


@pytest.mark.asyncio
async def test_config_flow_version_is_3():
    """ConfigFlow.VERSION must be 3 so HA invokes async_migrate_entry on v1/v2 entries."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow
    assert BoschCameraConfigFlow.VERSION == 3
