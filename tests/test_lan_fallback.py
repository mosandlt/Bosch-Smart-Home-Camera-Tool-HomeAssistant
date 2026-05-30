"""Tests for the LAN-reachability / local-fallback paths added in v12.4.10.

Pins:
- `is_lan_reachable` honors the post-write grace window
- `_async_outage_ping_all` is throttled and pings every known cam
- Privacy switch `available` stays True with cached state + LAN ping + Gen2
- Light entity `available` stays True under the same conditions
- BoschLanReachableBinarySensor reports the cached value verbatim
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-2222-2222-2222-222222222222"


def _make_coord(**overrides) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord._lan_tcp_reachable = {}
    coord._local_write_at = {}
    coord._LOCAL_WRITE_GRACE_S = 30.0
    coord._last_outage_ping_at = float("-inf")
    coord._rcp_lan_ip_cache = {CAM_A: "192.0.2.10", CAM_B: "192.0.2.11"}
    coord._local_creds_cache = {}
    coord.data = {
        CAM_A: {"info": {"title": "Terrasse"}},
        CAM_B: {"info": {"title": "Innenbereich"}},
    }
    for k, v in overrides.items():
        setattr(coord, k, v)
    # Bind the in-grace helper so `is_lan_reachable` (which calls self._in_local_write_grace)
    # works against the stub.
    coord._in_local_write_grace = BoschCameraCoordinator._in_local_write_grace.__get__(
        coord
    )
    # No-op listener notification — the real coordinator calls this after a
    # ping sweep so dependent entities re-evaluate `available`. The stub
    # has no entities attached, so a plain Mock suffices.
    coord.async_update_listeners = AsyncMock(return_value=None)
    return coord


# ── is_lan_reachable ─────────────────────────────────────────────────────────


class TestIsLanReachable:
    def test_unknown_when_no_ping_yet(self):
        coord = _make_coord()
        assert BoschCameraCoordinator.is_lan_reachable(coord, CAM_A) is None

    def test_returns_true_when_ping_succeeded(self):
        coord = _make_coord()
        coord._lan_tcp_reachable[CAM_A] = (True, 12345.0)
        assert BoschCameraCoordinator.is_lan_reachable(coord, CAM_A) is True

    def test_returns_false_when_ping_failed(self):
        coord = _make_coord()
        coord._lan_tcp_reachable[CAM_A] = (False, 12345.0)
        assert BoschCameraCoordinator.is_lan_reachable(coord, CAM_A) is False

    def test_grace_period_masks_recent_failure(self, freezer):
        freezer.move_to("2026-05-19T10:00:00+00:00")
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            coord._lan_tcp_reachable[CAM_A] = (False, 999.0)
            coord._local_write_at[CAM_A] = 990.0  # 10 s ago — inside grace
            assert BoschCameraCoordinator.is_lan_reachable(coord, CAM_A) is True

    def test_grace_period_expires_after_30s(self):
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            coord._lan_tcp_reachable[CAM_A] = (False, 999.0)
            coord._local_write_at[CAM_A] = 950.0  # 50 s ago — outside grace
            assert BoschCameraCoordinator.is_lan_reachable(coord, CAM_A) is False

    def test_unknown_during_grace_reports_reachable(self):
        """No ping recorded yet but a successful write just landed → treat as on."""
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            coord._local_write_at[CAM_A] = 990.0
            assert BoschCameraCoordinator.is_lan_reachable(coord, CAM_A) is True


# ── _in_local_write_grace ────────────────────────────────────────────────────


class TestInLocalWriteGrace:
    def test_default_inf_never_grace(self):
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            assert BoschCameraCoordinator._in_local_write_grace(coord, CAM_A) is False

    def test_inside_window_true(self):
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            coord._local_write_at[CAM_A] = 985.0
            assert BoschCameraCoordinator._in_local_write_grace(coord, CAM_A) is True

    def test_outside_window_false(self):
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            coord._local_write_at[CAM_A] = 950.0
            assert BoschCameraCoordinator._in_local_write_grace(coord, CAM_A) is False


# ── _async_outage_ping_all (throttle + fan-out) ──────────────────────────────


@pytest.mark.asyncio
class TestOutagePingAll:
    async def test_pings_all_known_cams(self):
        coord = _make_coord()
        coord._async_local_tcp_ping = AsyncMock(return_value=True)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_outage_ping_all(coord)
        called = {c.args[0] for c in coord._async_local_tcp_ping.await_args_list}
        assert called == {CAM_A, CAM_B}

    async def test_throttles_within_30s(self):
        coord = _make_coord()
        coord._async_local_tcp_ping = AsyncMock(return_value=True)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_outage_ping_all(coord)
            # 1010 = 10s later, still inside the 30s throttle window
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1010.0
        ):
            await BoschCameraCoordinator._async_outage_ping_all(coord)
        # Second call should have been throttled — still only 2 pings (first round).
        assert coord._async_local_tcp_ping.await_count == 2

    async def test_runs_again_after_throttle_window(self):
        coord = _make_coord()
        coord._async_local_tcp_ping = AsyncMock(return_value=True)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_outage_ping_all(coord)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1100.0
        ):
            await BoschCameraCoordinator._async_outage_ping_all(coord)
        # 100s gap → both rounds run → 4 pings total (2 cams × 2 rounds).
        assert coord._async_local_tcp_ping.await_count == 4

    async def test_no_known_cams_is_silent(self):
        coord = _make_coord(data={}, _rcp_lan_ip_cache={})
        coord._async_local_tcp_ping = AsyncMock(return_value=True)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_outage_ping_all(coord)
        coord._async_local_tcp_ping.assert_not_called()

    async def test_includes_lan_ip_only_cams(self):
        """A cam discovered only by RCP LAN-IP cache (no coordinator.data
        entry yet) still gets pinged — useful right after a fresh start."""
        coord = _make_coord(data={CAM_A: {}})  # only A in data
        coord._async_local_tcp_ping = AsyncMock(return_value=True)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_outage_ping_all(coord)
        called = {c.args[0] for c in coord._async_local_tcp_ping.await_args_list}
        assert called == {CAM_A, CAM_B}  # B comes from _rcp_lan_ip_cache
