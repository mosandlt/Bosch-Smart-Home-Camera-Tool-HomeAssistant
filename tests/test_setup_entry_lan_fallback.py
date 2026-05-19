"""`async_setup_entry` cloud-degraded + LAN-fallback branches (v12.4.10/.11).

Covers the heavy paths inside `async_setup_entry` not exercised by the
go2rtc tests:

- LAN-IP store load (`_lan_ips_store.async_load`) → populates
  `coordinator._rcp_lan_ip_cache` before the first cloud call
- `async_config_entry_first_refresh` raising ConfigEntryNotReady →
  registry rehydrate path (set `coordinator.data`, fire outage-ping)
- ConfigEntryNotReady on truly-first-install (empty registry) → re-raises
- v12.4.10 stale `binary_sensor.bosch_<X>_bosch_<X>_lan_reachable`
  migration removes the doubled-prefix entries
- `enable_fcm_push=True` → `coordinator.async_start_fcm_push()` scheduled
- `enable_nvr=True` → drain watcher background task created
- HA stop listener wired via `bus.async_listen_once(EVENT_HOMEASSISTANT_STOP)`
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.exceptions import ConfigEntryNotReady


MODULE = "custom_components.bosch_shc_camera"
CAM_A = "11111111-1111-1111-1111-111111111111"


def _make_coord_stub(camera_ids, *, first_refresh_raises=None):
    """Stub BoschCameraCoordinator — only the attributes accessed by
    async_setup_entry need to exist."""
    coord = MagicMock()
    if first_refresh_raises is not None:
        coord.async_config_entry_first_refresh = AsyncMock(side_effect=first_refresh_raises)
    else:
        coord.async_config_entry_first_refresh = AsyncMock()
    coord.data = {cid: {} for cid in camera_ids}
    coord._rcp_lan_ip_cache = {}
    coord._schedule_token_refresh = MagicMock()
    coord._renewal_tasks = {}
    coord._bg_tasks = set()
    coord._tls_proxy_ports = {}
    coord._nvr_drain_task = None
    coord._token_refresh_handle = None
    coord._stream_log_listener = None
    coord._async_outage_ping_all = AsyncMock(return_value=None)
    coord.async_start_fcm_push = AsyncMock(return_value=None)
    return coord


def _make_entry(options=None):
    entry = MagicMock()
    entry.options = options or {}
    entry.data = {"bearer_token": "tok", "refresh_token": "ref"}
    entry.entry_id = "test_entry_id"
    entry.async_on_unload = MagicMock()
    entry.add_update_listener = MagicMock(return_value=lambda: None)
    return entry


def _make_hass(*, persisted_ips=None, stale_lan_ids=None):
    """Hass stubbed to expose:
      - Store(persisted_ips) for the LAN-IP loader
      - entity_registry returning `stale_lan_ids` for the v12.4.10 migration
    """
    hass = MagicMock()
    hass.data = {}
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    hass.config_entries.async_entries = MagicMock(return_value=[])
    hass.config_entries.flow.async_init = AsyncMock(return_value={"type": "create_entry"})
    hass.async_create_task = MagicMock()
    hass.async_create_background_task = MagicMock()
    hass.bus.async_listen_once = MagicMock(return_value=lambda: None)
    return hass


class _FakeStore:
    """Replaces `homeassistant.helpers.storage.Store` so the test can hand
    back arbitrary persisted LAN-IP maps."""
    def __init__(self, payload):
        self._payload = payload
        self.saved = []

    async def async_load(self):
        return self._payload

    async def async_save(self, data):
        self.saved.append(data)


# ── Cloud-degraded startup ────────────────────────────────────────────────


class TestCloudDegradedStartup:
    @pytest.mark.asyncio
    async def test_rehydrate_from_registry_when_cloud_first_refresh_fails(self):
        """First refresh raises ConfigEntryNotReady → walks registry for
        cam_ids, seeds coordinator.data, schedules outage ping. Pins
        L5215-5223 + L5231-5242."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub(
            [], first_refresh_raises=ConfigEntryNotReady("Bosch 503"),
        )

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)), \
             patch(f"{MODULE}._rehydrate_cams_from_registry",
                   return_value=({CAM_A}, {CAM_A: "Terrasse"})), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # data was rehydrated (L5221-5223)
        assert CAM_A in coord_stub.data
        assert coord_stub.data[CAM_A]["info"]["title"] == "Terrasse"
        # last_update_success flipped to False (L5231)
        assert coord_stub.last_update_success is False
        # outage ping scheduled (L5242) — verify the helper was called and
        # its coroutine handed to async_create_task.
        coord_stub._async_outage_ping_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_registry_reraises_config_entry_not_ready(self):
        """No registry entries → propagate the original ConfigEntryNotReady
        so HA shows the standard setup-failed UI. Pins L5245 (raise branch)."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub(
            [], first_refresh_raises=ConfigEntryNotReady("Bosch 503"),
        )

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)), \
             patch(f"{MODULE}._rehydrate_cams_from_registry",
                   return_value=(set(), {})), \
             patch(f"{MODULE}.cf_unbuffer.register"):
            with pytest.raises(ConfigEntryNotReady):
                await async_setup_entry(hass, entry)


# ── Persistent LAN-IP store load ──────────────────────────────────────────


class TestPersistedLanIps:
    @pytest.mark.asyncio
    async def test_persisted_ips_loaded_into_cache(self):
        """`Store.async_load()` returns a dict → entries are mapped into the
        coordinator's `_rcp_lan_ip_cache` (cam_id upper-cased). Pins
        L5196-5199."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([])
        coord_stub.data = {CAM_A: {"info": {"title": "Terrasse"}}}

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        persisted = {
            CAM_A.lower(): "192.0.2.10",
            "garbage": 42,           # wrong type → must be skipped
            123: "ignored",          # wrong key type → must be skipped
        }

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(persisted)), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # Cam_id was upper-cased on load (CLAUDE.md: cam_id matches uppercase UUIDs)
        assert coord_stub._rcp_lan_ip_cache[CAM_A] == "192.0.2.10"
        assert "garbage" not in coord_stub._rcp_lan_ip_cache
        assert 123 not in coord_stub._rcp_lan_ip_cache


# ── FCM start + NVR drain task gating ─────────────────────────────────────


class TestOptionsGatedBackgroundTasks:
    @pytest.mark.asyncio
    async def test_fcm_push_started_when_option_enabled(self):
        """`enable_fcm_push=True` → coordinator.async_start_fcm_push is
        scheduled as a background task. Pins L5374."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass()
        entry = _make_entry(options={"enable_fcm_push": True})
        coord_stub = _make_coord_stub([CAM_A])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg):
            await async_setup_entry(hass, entry)

        # async_start_fcm_push was invoked and its coroutine scheduled.
        coord_stub.async_start_fcm_push.assert_called_once()
        hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_nvr_drain_task_started_when_option_enabled(self):
        """`enable_nvr=True` → drain watcher background task created. Pins L5380."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass()
        entry = _make_entry(options={"enable_nvr": True})
        coord_stub = _make_coord_stub([CAM_A])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch(f"{MODULE}.nvr_recorder._drain_staging_to_remote"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg):
            await async_setup_entry(hass, entry)

        hass.async_create_background_task.assert_called()
        # First positional arg = coroutine; second = task name
        name = hass.async_create_background_task.call_args.args[1]
        assert name == "bosch_nvr_drain_watcher"


# ── HA stop listener registration ─────────────────────────────────────────


class TestHaStopListener:
    @pytest.mark.asyncio
    async def test_listener_registered_for_homeassistant_stop(self):
        """The `EVENT_HOMEASSISTANT_STOP` async_listen_once must be wired
        so background coroutines are cancelled at HA shutdown. Pins
        L5366 + the bus.listen call."""
        from custom_components.bosch_shc_camera import async_setup_entry
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg):
            await async_setup_entry(hass, entry)

        # async_listen_once was called with EVENT_HOMEASSISTANT_STOP
        listen_args = [c.args for c in hass.bus.async_listen_once.call_args_list]
        assert any(args[0] == EVENT_HOMEASSISTANT_STOP for args in listen_args)

    @pytest.mark.asyncio
    async def test_stop_listener_body_cancels_coordinator_tasks(self):
        """Capture the registered `_on_ha_stop` callback and invoke it —
        verifies the listener body actually drives
        `_async_cancel_coordinator_tasks(coordinator)`. Pins L5366."""
        from custom_components.bosch_shc_camera import async_setup_entry
        from homeassistant.const import EVENT_HOMEASSISTANT_STOP

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])

        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch(f"{MODULE}._async_cancel_coordinator_tasks",
                   new=AsyncMock(return_value=None)) as cancel_mock, \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg):
            await async_setup_entry(hass, entry)

            # Pull the captured callback for EVENT_HOMEASSISTANT_STOP and fire it.
            stop_cb = None
            for c in hass.bus.async_listen_once.call_args_list:
                if c.args[0] == EVENT_HOMEASSISTANT_STOP:
                    stop_cb = c.args[1]
                    break
            assert stop_cb is not None, "HA stop listener was not registered"
            await stop_cb(MagicMock())

        cancel_mock.assert_awaited_once_with(coord_stub)


# ── v12.4.10 stale lan_reachable migration ────────────────────────────────


class TestStaleLanReachableMigration:
    @pytest.mark.asyncio
    async def test_stale_doubled_prefix_lan_reachable_removed(self):
        """A `binary_sensor.bosch_<X>_bosch_<X>_lan_reachable` entry from
        the first v12.4.10 build is removed so platform setup re-creates
        it with the canonical slug. Pins L5265-5266."""
        from custom_components.bosch_shc_camera import async_setup_entry

        hass = _make_hass()
        entry = _make_entry()
        coord_stub = _make_coord_stub([CAM_A])

        stale_id = "binary_sensor.bosch_terrasse_bosch_terrasse_lan_reachable"
        stale_entry = MagicMock(entity_id=stale_id)
        clean_entry = MagicMock(entity_id="binary_sensor.bosch_terrasse_motion")
        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)
        ent_reg.async_remove = MagicMock()

        with patch(f"{MODULE}.BoschCameraCoordinator", return_value=coord_stub), \
             patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)), \
             patch(f"{MODULE}.cf_unbuffer.register"), \
             patch("homeassistant.helpers.entity_registry.async_get",
                   return_value=ent_reg), \
             patch("homeassistant.helpers.entity_registry.async_entries_for_config_entry",
                   return_value=[stale_entry, clean_entry]):
            await async_setup_entry(hass, entry)

        # Only the stale doubled-prefix entry was removed; the clean
        # motion entity stayed.
        ent_reg.async_remove.assert_any_call(stale_id)
        for c in ent_reg.async_remove.call_args_list:
            assert c.args[0] != "binary_sensor.bosch_terrasse_motion"
