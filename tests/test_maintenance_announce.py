"""Tests for the maintenance announcement notify hook on the coordinator.

Pin every transition path so the same window can never spam the user, but a
genuine state change (scheduled -> active) gets one fresh announcement.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.maintenance import MaintenanceWindow


def _mw(
    state: str, link: str = "https://example/x", camera_relevant: bool = True
) -> MaintenanceWindow:
    """Build a MaintenanceWindow that classifies as `state` at frozen 'now'.

    Picks start/end relative to a fixed reference instant so the same
    `state()` evaluation lands in the expected bucket regardless of wall
    clock.
    """
    ref = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
    pub = ref - timedelta(hours=12)
    if state == "active":
        start, end = ref - timedelta(hours=1), ref + timedelta(hours=2)
    elif state == "scheduled":
        start, end = ref + timedelta(hours=3), ref + timedelta(hours=5)
    elif state == "past":
        start, end = ref - timedelta(hours=5), ref - timedelta(hours=3)
    elif state == "recent":
        start, end = None, None
        pub = ref - timedelta(hours=2)  # within recent window
    else:  # unknown
        start, end = None, None
        pub = ref - timedelta(days=60)
    return MaintenanceWindow(
        title="Wartung Kamera-Infrastruktur",
        link=link,
        pub_date=pub,
        summary="Window between 07:00 and 10:00 MESZ",
        scheduled_start=start,
        scheduled_end=end,
        source="rss:Wartungsarbeiten",
        camera_relevant=camera_relevant,
    )


def _make_coord(notify_service: str = "thomas") -> SimpleNamespace:
    """Stub coordinator carrying only what `_async_maybe_announce_maintenance` reads."""
    coord = SimpleNamespace()
    coord.options = {"alert_notify_service": notify_service}
    coord._maintenance_notified_key = None
    coord.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    return coord


@pytest.mark.asyncio
class TestMaintenanceAnnounce:
    async def _state_fixed_to(self, coord: SimpleNamespace, state: str) -> None:
        """Patch `MaintenanceWindow.state` so the test does not race wall clock."""
        # We just rely on _mw building windows that classify naturally — but
        # state() does evaluate against utcnow(). Freeze via monkeypatch in
        # tests that care (only "recent"/"unknown" depend on now; "active"
        # uses fixed +/-1h window around 2026-05-19 which may have already
        # passed in CI). Use freezegun via pytest-freezer (already in deps).
        pass

    async def test_announces_on_scheduled(self, freezer):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        mw = _mw("scheduled")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_called_once()
        args = coord.hass.services.async_call.await_args
        assert args.args[0] == "notify"
        assert args.args[1] == "thomas"
        assert "geplant" in args.args[2]["title"].lower()
        assert "Wartung" in args.args[2]["message"]
        assert coord._maintenance_notified_key == (mw.link, "scheduled")

    async def test_announces_again_on_scheduled_to_active(self, freezer):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        sched = _mw("scheduled")
        active = _mw("active", link=sched.link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, sched)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, active)
        assert coord.hass.services.async_call.await_count == 2
        # Second call carries the active wording.
        second = coord.hass.services.async_call.await_args_list[1]
        assert "läuft" in second.args[2]["title"].lower()
        assert coord._maintenance_notified_key == (active.link, "active")

    async def test_dedupes_duplicate_calls(self, freezer):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        mw = _mw("scheduled")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.parametrize("silent_state", ["past", "recent", "unknown"])
    async def test_silent_for_non_actionable_states(self, freezer, silent_state):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        mw = _mw(silent_state)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_not_called()

    async def test_silent_when_not_camera_relevant(self, freezer):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        mw = _mw("active", camera_relevant=False)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_not_called()

    async def test_no_service_configured_still_dedupes(self, freezer):
        """Without a notify service we record the key anyway so the user is
        not pestered the moment they later configure a service mid-window."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord(notify_service="")
        mw = _mw("scheduled")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_not_called()
        assert coord._maintenance_notified_key == (mw.link, "scheduled")

    async def test_notify_failure_is_swallowed(self, freezer):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        coord.hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("service down")
        )
        mw = _mw("active")
        # Must not raise — the maintenance fetch loop should not be brittle
        # to a misconfigured notify service.
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        # Key still gets recorded so we don't retry-storm on every coordinator tick.
        assert coord._maintenance_notified_key == (mw.link, "active")

    async def test_multiple_services_all_called(self, freezer):
        """alert_notify_service can be a comma-separated list — every entry is called."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord(notify_service="thomas, signalhome")
        mw = _mw("active")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        assert coord.hass.services.async_call.await_count == 2
        called = {c.args[1] for c in coord.hass.services.async_call.await_args_list}
        assert called == {"thomas", "signalhome"}

    async def test_new_window_link_re_announces(self, freezer):
        """A different announcement (new Bosch RSS item, different link)
        should re-announce even if the previous one was already 'scheduled'."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        first = _mw("scheduled", link="https://example/a")
        second = _mw("scheduled", link="https://example/b")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, first)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, second)
        assert coord.hass.services.async_call.await_count == 2

    async def test_active_to_past_announces_ended(self, freezer):
        """active → past transition for the same window fires one final
        'beendet' notification so users know the cloud should be back."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_coord()
        active = _mw("active")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, active)
        # Now jump past the window end (was ref + 2h)
        freezer.move_to("2026-05-19T10:00:00+00:00")
        past = _mw("past", link=active.link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, past)
        assert coord.hass.services.async_call.await_count == 2
        second = coord.hass.services.async_call.await_args_list[1]
        assert "beendet" in second.args[2]["title"].lower()
        assert coord._maintenance_notified_key == (past.link, "past")

    async def test_stale_past_window_does_not_announce(self, freezer):
        """A 'past' announcement discovered without a prior 'active' phase
        (e.g. integration restart after the window already closed) must
        stay silent — otherwise users get spammed about historical
        maintenance every time HA reboots."""
        freezer.move_to("2026-05-19T10:00:00+00:00")
        coord = _make_coord()
        past = _mw("past")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, past)
        coord.hass.services.async_call.assert_not_called()
        # Dedupe key is still set so a follow-up tick stays silent too.
        assert coord._maintenance_notified_key == (past.link, "past")

    async def test_full_scheduled_active_past_lifecycle(self, freezer):
        """End-to-end: scheduled → active → past for the same window
        triggers exactly three notifications in the right order."""
        freezer.move_to("2026-05-19T03:00:00+00:00")
        coord = _make_coord()
        link = "https://example/abc"
        # Phase 1: scheduled (now is before window start)
        sched = _mw("scheduled", link=link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, sched)
        # Phase 2: active (jump into window)
        freezer.move_to("2026-05-19T07:30:00+00:00")
        active = _mw("active", link=link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, active)
        # Phase 3: past (jump past end)
        freezer.move_to("2026-05-19T10:00:00+00:00")
        past = _mw("past", link=link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, past)
        assert coord.hass.services.async_call.await_count == 3
        titles = [
            c.args[2]["title"].lower()
            for c in coord.hass.services.async_call.await_args_list
        ]
        assert "geplant" in titles[0]
        assert "läuft" in titles[1]
        assert "beendet" in titles[2]
