"""Tests for tick_housekeeping.py — post-tick SMB/NVR cleanup, stale
device pruning, availability notify, persistence, maintenance-feed
refresh, cloud-state notify (Phase 2 step 6 of the coordinator
rewrite). Direct unit tests in isolation; the existing integration
tests exercising the full _async_update_data (test_init.py) already
cover end-to-end wiring."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.tick_housekeeping import run_housekeeping

CAM_A = "11111111-1111-1111-1111-111111111111"
NOW = 1000.0


def _make_coord(**overrides):
    """SimpleNamespace, NOT MagicMock — several branches rely on
    `getattr(coordinator, "_x_store", None)` defaulting to None when
    absent, which a plain MagicMock would defeat by auto-vivifying the
    attribute instead of leaving it missing."""
    coord = SimpleNamespace(
        hass=MagicMock(),
        last_smb_cleanup=overrides.pop("last_smb_cleanup", float("-inf")),
        last_nvr_cleanup=overrides.pop("last_nvr_cleanup", float("-inf")),
        run_smb_cleanup_bg=MagicMock(return_value="smb-coro"),
        run_nvr_cleanup_bg=MagicMock(return_value="nvr-coro"),
        cleanup_stale_devices=MagicMock(),
        rcp_lan_ip_cache=overrides.pop("rcp_lan_ip_cache", {}),
        hw_version=overrides.pop("hw_version", {}),
        local_creds_cache=overrides.pop("local_creds_cache", {}),
    )
    # `spawn_tracked` mirrors BoschCameraCoordinator.spawn_tracked closely
    # enough for these direct-module unit tests: routes through
    # hass.async_create_task (a MagicMock here, asserted on directly below)
    # instead of needing a real bg_tasks set on this bare SimpleNamespace
    # stub.
    coord.spawn_tracked = lambda coro, **kw: coord.hass.async_create_task(coro, **kw)
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


class TestSmbCleanup:
    @pytest.mark.asyncio
    async def test_smb_cleanup_triggered_when_due_and_enabled(self):
        coord = _make_coord(last_smb_cleanup=float("-inf"))
        opts = {
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_retention_days": 30,
        }
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_called_once()
        assert coord.hass.async_create_background_task.call_args[0][1] == (
            "bosch_shc_camera_smb_cleanup"
        )
        assert coord.last_smb_cleanup > 0  # advanced from time.monotonic()

    @pytest.mark.asyncio
    async def test_smb_cleanup_skipped_when_disabled(self):
        coord = _make_coord(last_smb_cleanup=float("-inf"))
        opts = {
            "enable_smb_upload": False,
            "smb_server": "nas.local",
            "smb_retention_days": 30,
        }
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_smb_cleanup_skipped_when_no_server(self):
        coord = _make_coord(last_smb_cleanup=float("-inf"))
        opts = {"enable_smb_upload": True, "smb_server": "", "smb_retention_days": 30}
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_smb_cleanup_skipped_when_retention_zero(self):
        coord = _make_coord(last_smb_cleanup=float("-inf"))
        opts = {
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_retention_days": 0,
        }
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_smb_cleanup_skipped_when_not_yet_due(self):
        coord = _make_coord(last_smb_cleanup=time.monotonic())
        opts = {
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_retention_days": 30,
        }
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_not_called()


class TestNvrCleanup:
    @pytest.mark.asyncio
    async def test_nvr_cleanup_triggered_when_due_and_enabled(self):
        coord = _make_coord(last_nvr_cleanup=float("-inf"))
        opts = {"enable_nvr": True, "nvr_retention_days": 3}
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_called_once()
        assert coord.hass.async_create_background_task.call_args[0][1] == (
            "bosch_shc_camera_nvr_cleanup"
        )

    @pytest.mark.asyncio
    async def test_nvr_cleanup_skipped_when_disabled(self):
        coord = _make_coord(last_nvr_cleanup=float("-inf"))
        opts = {"enable_nvr": False, "nvr_retention_days": 3}
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_nvr_cleanup_skipped_when_retention_zero(self):
        coord = _make_coord(last_nvr_cleanup=float("-inf"))
        opts = {"enable_nvr": True, "nvr_retention_days": 0}
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_nvr_cleanup_skipped_when_not_yet_due(self):
        coord = _make_coord(last_nvr_cleanup=time.monotonic())
        opts = {"enable_nvr": True, "nvr_retention_days": 3}
        await run_housekeeping(coord, {}, opts, NOW, False)
        coord.hass.async_create_background_task.assert_not_called()


class TestStaleDeviceCleanup:
    @pytest.mark.asyncio
    async def test_stale_cleanup_runs_with_data_on_normal_tick(self):
        coord = _make_coord()
        await run_housekeeping(coord, {CAM_A: {}}, {}, NOW, False)
        coord.cleanup_stale_devices.assert_called_once_with({CAM_A})

    @pytest.mark.asyncio
    async def test_stale_cleanup_skipped_on_first_tick(self):
        coord = _make_coord()
        await run_housekeeping(coord, {CAM_A: {}}, {}, NOW, True)
        coord.cleanup_stale_devices.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_cleanup_runs_for_definitive_empty_camera_list(self):
        """The account's last camera being removed (data == {}) must still
        run cleanup — fetch_camera_list already raises on any fetch failure
        before housekeeping ever runs, so an empty `data` here is a genuine
        zero-camera account, not a glitch (backported from the Core PR's
        Copilot review round 3, 2026-07-27)."""
        coord = _make_coord()
        await run_housekeeping(coord, {}, {}, NOW, False)
        coord.cleanup_stale_devices.assert_called_once_with(set())


class TestAvailabilityNotifier:
    @pytest.mark.asyncio
    async def test_announces_status_for_each_camera(self):
        compute = MagicMock(return_value="ONLINE")
        coord = _make_coord(
            _async_maybe_announce_camera_status=MagicMock(return_value="coro"),
            _compute_status_for=compute,
        )
        await run_housekeeping(coord, {CAM_A: {"status": "ONLINE"}}, {}, NOW, False)
        compute.assert_called_once_with(CAM_A, {"status": "ONLINE"})
        coord.hass.async_create_task.assert_called()

    @pytest.mark.asyncio
    async def test_skipped_on_first_tick(self):
        coord = _make_coord(
            _async_maybe_announce_camera_status=MagicMock(),
            _compute_status_for=MagicMock(),
        )
        await run_housekeeping(coord, {CAM_A: {}}, {}, NOW, True)
        coord._compute_status_for.assert_not_called()

    @pytest.mark.asyncio
    async def test_stub_coordinator_without_announce_helpers_no_crash(self):
        """Defensive getattr must handle stub coordinators (unit-test
        fixtures bypassing __init__) that lack these attributes entirely."""
        coord = _make_coord()
        await run_housekeeping(coord, {CAM_A: {}}, {}, NOW, False)
        coord.cleanup_stale_devices.assert_called_once()


class TestLanIpPersistence:
    @pytest.mark.asyncio
    async def test_saves_when_snapshot_changed(self):
        store = MagicMock()
        store.async_save = MagicMock(return_value="coro")
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "192.0.2.1"},
            lan_ips_store=store,
            lan_ips_snapshot=None,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_called_once_with({CAM_A: "192.0.2.1"})
        assert coord.lan_ips_snapshot == {CAM_A: "192.0.2.1"}

    @pytest.mark.asyncio
    async def test_skips_save_when_unchanged(self):
        store = MagicMock()
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: "192.0.2.1"},
            lan_ips_store=store,
            lan_ips_snapshot={CAM_A: "192.0.2.1"},
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_store_configured_skips(self):
        coord = _make_coord(rcp_lan_ip_cache={CAM_A: "192.0.2.1"})
        await run_housekeeping(coord, {}, {}, NOW, False)  # must not raise

    @pytest.mark.asyncio
    async def test_empty_ip_values_filtered_out(self):
        """An empty-string IP is filtered out, but the resulting empty
        snapshot must still be saved if it differs from the previous one
        (`None` here) — no truthiness guard on the snapshot itself
        (backported from the Core PR's Copilot review round 5,
        2026-07-27)."""
        store = MagicMock()
        coord = _make_coord(
            rcp_lan_ip_cache={CAM_A: ""},
            lan_ips_store=store,
            lan_ips_snapshot=None,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_called_once_with({})


class TestHwVersionPersistence:
    @pytest.mark.asyncio
    async def test_saves_when_snapshot_changed(self):
        store = MagicMock()
        coord = _make_coord(
            hw_version={CAM_A: "CAMERA_360"},
            hw_version_store=store,
            hw_version_snapshot=None,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_called_once_with({CAM_A: "CAMERA_360"})

    @pytest.mark.asyncio
    async def test_skips_save_when_unchanged(self):
        store = MagicMock()
        coord = _make_coord(
            hw_version={CAM_A: "CAMERA_360"},
            hw_version_store=store,
            hw_version_snapshot={CAM_A: "CAMERA_360"},
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_store_configured_skips(self):
        coord = _make_coord(hw_version={CAM_A: "CAMERA_360"})
        await run_housekeeping(coord, {}, {}, NOW, False)  # must not raise

    @pytest.mark.asyncio
    async def test_saves_empty_snapshot_after_last_camera_removed(self):
        """cleanup_stale_devices() clears hw_version when the account's last
        camera is removed — that empty snapshot must still be saved, or the
        removed camera's hardware version is reloaded from .storage after a
        restart (backported from the Core PR's Copilot review round 6,
        2026-07-27 — a prior version's `and coordinator.hw_version`
        truthiness guard skipped this save entirely)."""
        store = MagicMock()
        coord = _make_coord(
            hw_version={},
            hw_version_store=store,
            hw_version_snapshot={CAM_A: "CAMERA_360"},
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_called_once_with({})


class TestLocalCredsPersistence:
    @pytest.mark.asyncio
    async def test_saves_full_creds_when_changed(self):
        store = MagicMock()
        coord = _make_coord(
            local_creds_cache={
                CAM_A: {
                    "user": "admin",
                    "password": "secret",
                    "host": "192.0.2.1",
                    "port": 443,
                }
            },
            local_creds_store=store,
            local_creds_snapshot=None,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_called_once_with(
            {
                CAM_A: {
                    "user": "admin",
                    "password": "secret",
                    "host": "192.0.2.1",
                    "port": 443,
                }
            }
        )

    @pytest.mark.asyncio
    async def test_defaults_missing_port_to_443(self):
        store = MagicMock()
        coord = _make_coord(
            local_creds_cache={
                CAM_A: {"user": "admin", "password": "secret", "host": "192.0.2.1"}
            },
            local_creds_store=store,
            local_creds_snapshot=None,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        saved = store.async_save.call_args[0][0]
        assert saved[CAM_A]["port"] == 443

    @pytest.mark.asyncio
    async def test_incomplete_entry_filtered_out(self):
        """An incomplete entry (missing password/host) is filtered out of
        the snapshot, but the resulting empty snapshot must still be saved
        if it differs from the previous one (`None` here) — no truthiness
        guard on the snapshot itself (backported from the Core PR's
        Copilot review round 3, 2026-07-27)."""
        store = MagicMock()
        coord = _make_coord(
            local_creds_cache={CAM_A: {"user": "admin"}},  # missing password/host
            local_creds_store=store,
            local_creds_snapshot=None,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_called_once_with({})

    @pytest.mark.asyncio
    async def test_persists_empty_snapshot_when_last_camera_removed(self):
        """Clearing the last camera's LOCAL creds must still persist the
        now-empty snapshot — otherwise a deleted camera's Digest credentials
        remain in `.storage` indefinitely (backported from the Core PR's
        Copilot review round 3, 2026-07-27)."""
        store = MagicMock()
        coord = _make_coord(
            local_creds_cache={},
            local_creds_store=store,
            local_creds_snapshot={
                CAM_A: {"user": "admin", "password": "secret", "host": "192.0.2.1"}
            },
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_called_once_with({})
        assert coord.local_creds_snapshot == {}

    @pytest.mark.asyncio
    async def test_skips_save_when_unchanged(self):
        store = MagicMock()
        snapshot = {
            CAM_A: {
                "user": "admin",
                "password": "secret",
                "host": "192.0.2.1",
                "port": 443,
            }
        }
        coord = _make_coord(
            local_creds_cache={
                CAM_A: {
                    "user": "admin",
                    "password": "secret",
                    "host": "192.0.2.1",
                    "port": 443,
                }
            },
            local_creds_store=store,
            local_creds_snapshot=snapshot,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_store_configured_skips(self):
        coord = _make_coord(
            local_creds_cache={
                CAM_A: {"user": "a", "password": "b", "host": "c", "port": 443}
            }
        )
        await run_housekeeping(coord, {}, {}, NOW, False)  # must not raise


class TestMaintenanceFeedRefresh:
    @pytest.mark.asyncio
    async def test_refresh_triggered_when_due(self):
        refresh = MagicMock(return_value="coro")
        coord = _make_coord(
            maintenance_last_fetch=float("-inf"),
            _MAINTENANCE_INTERVAL_S=3600.0,
            _async_refresh_maintenance=refresh,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        refresh.assert_called_once_with(reactive=False)

    @pytest.mark.asyncio
    async def test_refresh_skipped_when_not_due(self):
        refresh = MagicMock()
        coord = _make_coord(
            maintenance_last_fetch=NOW,
            _MAINTENANCE_INTERVAL_S=3600.0,
            _async_refresh_maintenance=refresh,
        )
        await run_housekeeping(coord, {}, {}, NOW, False)
        refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_stub_coordinator_without_refresh_helper_no_crash(self):
        coord = _make_coord()
        await run_housekeeping(coord, {}, {}, NOW, False)  # must not raise


class TestCloudStateNotifier:
    @pytest.mark.asyncio
    async def test_notifier_awaited_directly_when_present(self):
        # Called directly (awaited), NOT scheduled via hass.async_create_task
        # — the early-return-heavy success=True path is cheap enough that
        # spawning a task per tick was pure overhead (perf-refactor Phase 1
        # step 5). Must be an AsyncMock: a real `await` on the return value
        # of a plain MagicMock would raise TypeError.
        notifier = AsyncMock()
        coord = _make_coord(_async_maybe_announce_cloud_state=notifier)
        await run_housekeeping(coord, {}, {}, NOW, False)
        notifier.assert_awaited_once_with(True)
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_stub_coordinator_without_notifier_no_crash(self):
        coord = _make_coord()
        await run_housekeeping(coord, {}, {}, NOW, False)  # must not raise
