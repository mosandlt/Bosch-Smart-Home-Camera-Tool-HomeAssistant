"""Regression tests for maintenance_announcements.py — the maintenance-feed
fetch + the persisted-flag/session-quota-notification side-effecting
helpers, extracted out of coordinator.py (style audit, 2026-08-05).

Tests call the module functions directly with a lightweight stub
(SimpleNamespace) standing in for the coordinator, mirroring the existing
`tests/test_quality_prefs.py`/`tests/test_maintenance.py` patterns.
tests/test_init.py's `TestPersistMethods` and tests/test_maintenance.py's
`TestAsyncRefreshMaintenance` already pin the behavior end-to-end through
the coordinator's unbound-method delegators — this file adds direct
module-level coverage plus explicit virtual-dispatch guards (lesson from
prior extraction rounds: a cross-call inside an extracted function must
route through `coordinator.method_name(...)`, never the raw module
function, so a per-instance patch on the coordinator is still honored).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import maintenance_announcements

CAM_A = "cam-a"


def _make_refresh_coord(
    *,
    last_fetch: float = float("-inf"),
    cooldown: float = 300.0,
    cache: object | None = None,
) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.maintenance_last_fetch = last_fetch
    coord._MAINTENANCE_REACTIVE_COOLDOWN_S = cooldown
    coord.maintenance_cache = cache
    coord.hass = SimpleNamespace(data={})
    coord._async_maybe_announce_maintenance = AsyncMock(return_value=None)
    return coord


class _FakeMaintenanceWindow:
    def __init__(self, title: str = "Wartung") -> None:
        self.title = title
        self.scheduled_start = None
        self.scheduled_end = None

    def state(self) -> str:
        return "active"


@pytest.mark.asyncio
class TestAsyncRefreshMaintenance:
    async def test_reactive_within_cooldown_skips_fetch(self) -> None:
        coord = _make_refresh_coord(last_fetch=950.0, cooldown=300.0)
        fetch_mock = AsyncMock(return_value=_FakeMaintenanceWindow())
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                return_value=1000.0,
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=fetch_mock,
            ),
        ):
            await maintenance_announcements.async_refresh_maintenance(
                coord, reactive=True
            )
        fetch_mock.assert_not_awaited()
        assert coord.maintenance_cache is None

    async def test_successful_fetch_updates_cache_and_announces(self) -> None:
        coord = _make_refresh_coord()
        new_mw = _FakeMaintenanceWindow("Wartung Kamera")
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                return_value=1000.0,
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(return_value=new_mw),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            await maintenance_announcements.async_refresh_maintenance(
                coord, reactive=False
            )
        assert coord.maintenance_cache is new_mw
        assert coord.maintenance_last_fetch == 1000.0
        coord._async_maybe_announce_maintenance.assert_awaited_once_with(new_mw)

    async def test_fetch_exception_is_swallowed_cache_unchanged(self) -> None:
        previous = _FakeMaintenanceWindow("previous")
        coord = _make_refresh_coord(cache=previous)
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                return_value=1000.0,
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            await maintenance_announcements.async_refresh_maintenance(
                coord, reactive=False
            )
        assert coord.maintenance_cache is previous
        coord._async_maybe_announce_maintenance.assert_not_awaited()

    async def test_fetch_none_result_keeps_previous_cache(self) -> None:
        previous = _FakeMaintenanceWindow("previous")
        coord = _make_refresh_coord(cache=previous)
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                return_value=1000.0,
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            await maintenance_announcements.async_refresh_maintenance(
                coord, reactive=False
            )
        assert coord.maintenance_cache is previous
        coord._async_maybe_announce_maintenance.assert_not_awaited()

    async def test_calls_through_coordinator_not_announcements_module_directly(
        self,
    ) -> None:
        """Virtual-dispatch guard: a per-instance override of
        `coordinator._async_maybe_announce_maintenance` must be honored —
        the extracted function must NOT call
        `announcements.maybe_announce_maintenance` directly."""
        coord = _make_refresh_coord()
        new_mw = _FakeMaintenanceWindow()
        override = AsyncMock(return_value=None)
        coord._async_maybe_announce_maintenance = override
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic",
                return_value=1000.0,
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(return_value=new_mw),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
            patch(
                "custom_components.bosch_shc_camera.announcements.maybe_announce_maintenance",
                new=AsyncMock(
                    side_effect=AssertionError(
                        "must not call announcements module directly"
                    )
                ),
            ),
        ):
            await maintenance_announcements.async_refresh_maintenance(
                coord, reactive=False
            )
        override.assert_awaited_once_with(new_mw)


class TestPersistMaintNotifiedKey:
    def test_store_and_key_present_schedules_save(self) -> None:
        store = MagicMock()
        store.async_save = AsyncMock()
        coord = SimpleNamespace(
            hass=MagicMock(),
            maintenance_notified_key=("https://example.com/maint", "active"),
            maint_notified_store=store,
        )
        maintenance_announcements.persist_maint_notified_key(coord)
        coord.hass.async_create_task.assert_called_once()

    def test_no_store_is_noop(self) -> None:
        coord = SimpleNamespace(
            hass=MagicMock(),
            maintenance_notified_key=("link", "active"),
        )
        maintenance_announcements.persist_maint_notified_key(coord)
        coord.hass.async_create_task.assert_not_called()

    def test_no_key_is_noop(self) -> None:
        store = MagicMock()
        coord = SimpleNamespace(
            hass=MagicMock(), maintenance_notified_key=None, maint_notified_store=store
        )
        maintenance_announcements.persist_maint_notified_key(coord)
        coord.hass.async_create_task.assert_not_called()


class TestPersistCloudOutageFlag:
    def test_store_present_spawns_tracked_save(self) -> None:
        store = MagicMock()
        store.async_save = AsyncMock()

        def _close(coro, **_kwargs):
            coro.close()
            return MagicMock()

        coord = SimpleNamespace(
            hass=MagicMock(),
            cloud_outage_notified=True,
            cloud_alert_store=store,
            spawn_tracked=MagicMock(side_effect=_close),
        )
        maintenance_announcements.persist_cloud_outage_flag(coord)
        coord.spawn_tracked.assert_called_once()
        _, call_kwargs = coord.spawn_tracked.call_args
        assert call_kwargs["name"] == "bosch_shc_camera_persist_cloud_outage_flag"
        store.async_save.assert_called_once_with({"outage_notified": True})

    def test_falsy_flag_still_persisted_as_bool(self) -> None:
        store = MagicMock()
        store.async_save = AsyncMock()

        def _close(coro, **_kwargs):
            coro.close()
            return MagicMock()

        coord = SimpleNamespace(
            hass=MagicMock(),
            cloud_outage_notified=None,
            cloud_alert_store=store,
            spawn_tracked=MagicMock(side_effect=_close),
        )
        maintenance_announcements.persist_cloud_outage_flag(coord)
        store.async_save.assert_called_once_with({"outage_notified": False})

    def test_no_store_is_noop(self) -> None:
        coord = SimpleNamespace(
            hass=MagicMock(),
            cloud_outage_notified=False,
            spawn_tracked=MagicMock(),
        )
        maintenance_announcements.persist_cloud_outage_flag(coord)
        coord.spawn_tracked.assert_not_called()


def _make_quota_coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "_session_quota_hits": {},
        "_SESSION_QUOTA_WINDOW_S": 300.0,
        "_SESSION_QUOTA_NOTIFY_THRESHOLD": 3,
        "data": {},
        "hass": SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock())),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
class TestAsyncHandleSessionQuotaHit:
    async def test_below_threshold_no_notification(self) -> None:
        coord = _make_quota_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1.0
        ):
            await maintenance_announcements.async_handle_session_quota_hit(coord, CAM_A)
            await maintenance_announcements.async_handle_session_quota_hit(coord, CAM_A)
        coord.hass.services.async_call.assert_not_awaited()
        assert len(coord._session_quota_hits[CAM_A]) == 2

    async def test_threshold_hit_fires_persistent_notification(self) -> None:
        coord = _make_quota_coord(
            data={CAM_A: {"info": {"title": "Terrasse"}}},
        )
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1.0
        ):
            for _ in range(3):
                await maintenance_announcements.async_handle_session_quota_hit(
                    coord, CAM_A
                )
        coord.hass.services.async_call.assert_awaited_once()
        args, kwargs = coord.hass.services.async_call.call_args
        assert args[0] == "persistent_notification"
        assert args[1] == "create"
        assert "Terrasse" in args[2]["title"]
        assert kwargs["blocking"] is False

    async def test_old_hits_outside_window_are_pruned(self) -> None:
        coord = _make_quota_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1.0
        ):
            await maintenance_announcements.async_handle_session_quota_hit(coord, CAM_A)
            await maintenance_announcements.async_handle_session_quota_hit(coord, CAM_A)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await maintenance_announcements.async_handle_session_quota_hit(coord, CAM_A)
        # The two old hits (t=1.0) are outside the 300s window at t=1000 —
        # only the fresh one should remain, so no notification fires.
        coord.hass.services.async_call.assert_not_awaited()
        assert len(coord._session_quota_hits[CAM_A]) == 1

    async def test_missing_cam_name_falls_back_to_id_prefix(self) -> None:
        coord = _make_quota_coord(data={})
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1.0
        ):
            for _ in range(3):
                await maintenance_announcements.async_handle_session_quota_hit(
                    coord, CAM_A
                )
        args, _ = coord.hass.services.async_call.call_args
        assert CAM_A[:8] in args[2]["title"]

    async def test_exception_inside_is_swallowed_non_fatal(self) -> None:
        coord = _make_quota_coord()
        # Force an exception by making _session_quota_hits not a dict.
        coord._session_quota_hits = None
        # Must not raise.
        await maintenance_announcements.async_handle_session_quota_hit(coord, CAM_A)
