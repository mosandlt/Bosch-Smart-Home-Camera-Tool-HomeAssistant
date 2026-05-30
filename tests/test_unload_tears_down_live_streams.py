"""Regression test for the live-stream-teardown-on-unload fix (2026-05-26).

PROBLEM (observed): two integration reloads back-to-back (use_mjpeg_snapshot
toggle on→off) left stale state:
  - go2rtc keeps the producer URL pointing at the now-dead tls_proxy port
  - HA's Stream object on the camera entity holds the dead URL
  - Browser polls a 404 m3u8 until user hard-refreshes the card

ROOT CAUSE: `_async_cancel_coordinator_tasks` called `stop_all_proxies()`
but never called the per-cam `_tear_down_live_stream(cam_id)` which is
the only path that unregisters go2rtc and nulls `cam_entity.stream`.

FIX: iterate `_live_connections.keys()` before `stop_all_proxies` and call
`_tear_down_live_stream` for each. Pin every part of the teardown contract.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks

CAM_A = "AAAA-CAM-A"
CAM_B = "BBBB-CAM-B"


def _make_minimal_coord(active_cam_ids: list[str]) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.async_stop_fcm_push = AsyncMock()
    coord._token_refresh_handle = None
    coord._renewal_tasks = {}
    coord._bg_tasks = set()
    coord._nvr_drain_task = None
    coord._tls_proxy_ports = {}
    coord._stream_log_listener = None
    coord._live_connections = {cid: {} for cid in active_cam_ids}
    coord._tear_down_live_stream = AsyncMock()
    return coord


class TestUnloadTearsDownLiveStreams:
    @pytest.mark.asyncio
    async def test_tear_down_called_for_each_active_cam(self) -> None:
        """Every cam_id present in `_live_connections` at unload time MUST
        receive a `_tear_down_live_stream(cam_id)` call."""
        coord = _make_minimal_coord([CAM_A, CAM_B])

        # Patch the module-level helpers that the function calls so we don't
        # need the real NVR / TLS subsystems.
        from custom_components import bosch_shc_camera as bsc_mod

        bsc_mod_orig_stop_all = bsc_mod.stop_all_proxies
        bsc_mod_orig_nvr_stop = bsc_mod.nvr_recorder.stop_all
        bsc_mod.stop_all_proxies = MagicMock()
        bsc_mod.nvr_recorder.stop_all = AsyncMock()
        try:
            await _async_cancel_coordinator_tasks(coord)
        finally:
            bsc_mod.stop_all_proxies = bsc_mod_orig_stop_all
            bsc_mod.nvr_recorder.stop_all = bsc_mod_orig_nvr_stop

        assert coord._tear_down_live_stream.await_count == 2
        called_cams = {c.args[0] for c in coord._tear_down_live_stream.await_args_list}
        assert called_cams == {CAM_A, CAM_B}

    @pytest.mark.asyncio
    async def test_no_teardown_when_no_active_streams(self) -> None:
        """Empty `_live_connections` → no teardown calls, no crash."""
        coord = _make_minimal_coord([])

        from custom_components import bosch_shc_camera as bsc_mod

        bsc_mod_orig_stop_all = bsc_mod.stop_all_proxies
        bsc_mod_orig_nvr_stop = bsc_mod.nvr_recorder.stop_all
        bsc_mod.stop_all_proxies = MagicMock()
        bsc_mod.nvr_recorder.stop_all = AsyncMock()
        try:
            await _async_cancel_coordinator_tasks(coord)
        finally:
            bsc_mod.stop_all_proxies = bsc_mod_orig_stop_all
            bsc_mod.nvr_recorder.stop_all = bsc_mod_orig_nvr_stop

        coord._tear_down_live_stream.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_teardown_exception_does_not_break_unload(self) -> None:
        """One cam's teardown raising must not prevent stop_all_proxies or
        the other cam's teardown."""
        coord = _make_minimal_coord([CAM_A, CAM_B])
        coord._tear_down_live_stream = AsyncMock(side_effect=RuntimeError("boom"))

        from custom_components import bosch_shc_camera as bsc_mod

        bsc_mod_orig_stop_all = bsc_mod.stop_all_proxies
        bsc_mod_orig_nvr_stop = bsc_mod.nvr_recorder.stop_all
        stop_all_mock = MagicMock()
        bsc_mod.stop_all_proxies = stop_all_mock
        bsc_mod.nvr_recorder.stop_all = AsyncMock()
        try:
            await _async_cancel_coordinator_tasks(coord)
        finally:
            bsc_mod.stop_all_proxies = bsc_mod_orig_stop_all
            bsc_mod.nvr_recorder.stop_all = bsc_mod_orig_nvr_stop

        # Both cams got their teardown attempt despite first one raising.
        assert coord._tear_down_live_stream.await_count == 2
        # stop_all_proxies still ran (defensive fallback for anything that
        # slipped through per-cam teardown).
        stop_all_mock.assert_called_once_with(coord._tls_proxy_ports)

    @pytest.mark.asyncio
    async def test_teardown_runs_before_stop_all_proxies(self) -> None:
        """Order matters: per-cam teardown (which unregisters go2rtc + stops
        HA Stream objects) must run BEFORE stop_all_proxies (defensive
        catch-all). Otherwise the brief window between port-close and
        stream-stop leaves the browser polling a 404 m3u8."""
        coord = _make_minimal_coord([CAM_A])

        call_order: list[str] = []
        coord._tear_down_live_stream = AsyncMock(
            side_effect=lambda cid: call_order.append(f"tear_down:{cid}"),
        )

        from custom_components import bosch_shc_camera as bsc_mod

        bsc_mod_orig_stop_all = bsc_mod.stop_all_proxies
        bsc_mod_orig_nvr_stop = bsc_mod.nvr_recorder.stop_all
        bsc_mod.stop_all_proxies = MagicMock(
            side_effect=lambda *_a, **_kw: call_order.append("stop_all_proxies"),
        )
        bsc_mod.nvr_recorder.stop_all = AsyncMock(
            side_effect=lambda *_a, **_kw: call_order.append("nvr_stop_all"),
        )
        try:
            await _async_cancel_coordinator_tasks(coord)
        finally:
            bsc_mod.stop_all_proxies = bsc_mod_orig_stop_all
            bsc_mod.nvr_recorder.stop_all = bsc_mod_orig_nvr_stop

        # NVR first (clean MP4 flush), then per-cam teardown, then defensive
        # stop_all_proxies for anything that slipped through.
        assert call_order == [
            "nvr_stop_all",
            f"tear_down:{CAM_A}",
            "stop_all_proxies",
        ]
