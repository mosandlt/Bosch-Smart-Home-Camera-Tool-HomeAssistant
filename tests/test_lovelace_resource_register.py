"""`_register_lovelace_resources` closure (lines 4275-4327 in __init__.py).

This closure is invoked from `async_setup`. When HA is already running
(reload case) it executes immediately; on cold start it defers to the
`EVENT_HOMEASSISTANT_STARTED` listener. We drive it via the running-path
(`hass.is_running = True`) and a fake `lovelace.resources` resource store.

Pins:
  - Legacy `/local/bosch-camera-card*` entries are removed (pre-v10.3.19
    leftover). Their presence alongside the new `/bosch_shc_camera/...`
    URL causes `customElements.define` to fire twice, which crashes the
    card (see code comment).
  - Resource whose URL already includes `?v={CARD_VERSION}` is left alone
    (no spurious update events).
  - Existing entry with stale version → `async_update_item`, not
    `async_create_item`.
  - Missing entry → `async_create_item` with `res_type=module`.
  - `lovelace` not in hass.data (frontend disabled / early boot) → bail
    silently with a warning, no AttributeError.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


MODULE = "custom_components.bosch_shc_camera"


def _make_resources(items):
    """Fake Lovelace resources store with async helpers."""
    res = MagicMock()
    res.async_load = AsyncMock()
    res.async_items = MagicMock(return_value=items)
    res.async_delete_item = AsyncMock()
    res.async_update_item = AsyncMock()
    res.async_create_item = AsyncMock()
    return res


def _make_hass(resources=None):
    hass = MagicMock()
    hass.is_running = True
    if resources is not None:
        hass.data = {"lovelace": SimpleNamespace(resources=resources)}
    else:
        hass.data = {}
    hass.http.async_register_static_paths = AsyncMock()
    hass.services.has_service.return_value = True  # short-circuit _register_services
    hass.bus.async_listen_once = MagicMock()
    return hass


class TestRegisterLovelaceResources:
    @pytest.mark.asyncio
    async def test_legacy_local_entry_removed(self):
        """Pre-v10.3.19 installs left `/local/bosch-camera-card.js` in
        Lovelace storage. Must be deleted on every setup to avoid double
        customElements.define."""
        from custom_components.bosch_shc_camera import async_setup
        items = [
            {"id": "leg1", "url": "/local/bosch-camera-card.js"},
            {"id": "leg2", "url": "/local/bosch-camera-autoplay-fix.js"},
        ]
        resources = _make_resources(items)
        hass = _make_hass(resources)
        result = await async_setup(hass, {})
        assert result is True
        # Both legacy entries deleted
        deleted_ids = [c.args[0] for c in resources.async_delete_item.call_args_list]
        assert "leg1" in deleted_ids
        assert "leg2" in deleted_ids

    @pytest.mark.asyncio
    async def test_current_versioned_entry_untouched(self):
        """If the resource URL already includes the CURRENT `?v=<CARD_VERSION>`,
        no update or create call must fire — prevents Lovelace storage churn
        on every HA restart."""
        from custom_components.bosch_shc_camera import async_setup
        from custom_components.bosch_shc_camera.const import CARD_VERSION
        items = [
            {"id": "c1", "url": f"/bosch_shc_camera/bosch-camera-card.js?v={CARD_VERSION}"},
            {"id": "c2", "url": f"/bosch_shc_camera/bosch-camera-autoplay-fix.js?v={CARD_VERSION}"},
        ]
        resources = _make_resources(items)
        hass = _make_hass(resources)
        await async_setup(hass, {})
        resources.async_update_item.assert_not_awaited()
        resources.async_create_item.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_versioned_entry_updated(self):
        """Resource with old `?v=...` → `async_update_item` (not create)."""
        from custom_components.bosch_shc_camera import async_setup
        from custom_components.bosch_shc_camera.const import CARD_VERSION
        items = [
            {"id": "s1", "url": "/bosch_shc_camera/bosch-camera-card.js?v=0.0.0-old"},
            {"id": "s2", "url": "/bosch_shc_camera/bosch-camera-autoplay-fix.js?v=0.0.0-old"},
        ]
        resources = _make_resources(items)
        hass = _make_hass(resources)
        await async_setup(hass, {})
        assert resources.async_update_item.await_count == 2
        # No fresh create (existing entry was found)
        resources.async_create_item.assert_not_awaited()
        # Updates carry the current CARD_VERSION
        for call in resources.async_update_item.await_args_list:
            assert f"?v={CARD_VERSION}" in call.args[1]["url"]
            assert call.args[1]["res_type"] == "module"

    @pytest.mark.asyncio
    async def test_missing_entries_created(self):
        """Fresh install (no /bosch_shc_camera entries) → async_create_item
        for both card + autoplay-fix."""
        from custom_components.bosch_shc_camera import async_setup
        from custom_components.bosch_shc_camera.const import CARD_VERSION
        resources = _make_resources([])
        hass = _make_hass(resources)
        await async_setup(hass, {})
        assert resources.async_create_item.await_count == 2
        for call in resources.async_create_item.await_args_list:
            payload = call.args[0]
            assert payload["res_type"] == "module"
            assert payload["url"].endswith(f"?v={CARD_VERSION}")

    @pytest.mark.asyncio
    async def test_lovelace_missing_warns_no_crash(self):
        """`hass.data['lovelace']` absent (e.g. frontend not loaded) → log
        warning and return; no AttributeError."""
        from custom_components.bosch_shc_camera import async_setup
        hass = _make_hass(resources=None)
        result = await async_setup(hass, {})
        assert result is True  # setup must still succeed

    @pytest.mark.asyncio
    async def test_defers_when_ha_not_running(self):
        """Cold boot: `hass.is_running` False → registration deferred via
        `bus.async_listen_once`; the resources store is NOT touched yet."""
        from custom_components.bosch_shc_camera import async_setup
        resources = _make_resources([])
        hass = _make_hass(resources)
        hass.is_running = False
        await async_setup(hass, {})
        hass.bus.async_listen_once.assert_called_once()
        # Closure not invoked — no touch on resources
        resources.async_load.assert_not_awaited()
