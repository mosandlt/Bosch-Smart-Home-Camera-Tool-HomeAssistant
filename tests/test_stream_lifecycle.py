"""Tests for stream lifecycle: `_tear_down_live_stream` cleanup invariants.

Stream teardown is invoked from 4 different entry points:
  - User toggles `switch.live_stream` OFF
  - User toggles `switch.privacy_mode` ON (camera shutter closes)
  - StreamWorkerErrorListener after worker errors
  - Coordinator stream-health watchdog

All four paths must reach the same end state — no leftover renewal tasks,
no leftover TLS proxies, no stale `_live_connections` entries — otherwise
the next stream-on attempt either re-uses dead state or races with cleanup.
GH#6 (WoodenDuke 'streaming broken since 10.x') was caused by leftover
`stream` references after privacy-toggle teardown.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _make_coord(stream_obj=None):
    """Coordinator stub with all the dicts `_tear_down_live_stream` touches."""
    cam_entity = SimpleNamespace(stream=stream_obj)
    return SimpleNamespace(
        _live_connections={CAM_ID: {"rtspsUrl": "rtsps://x"}},
        _live_opened_at={CAM_ID: 100.0},
        _stream_error_count={CAM_ID: 2},
        _stream_error_at={CAM_ID: 100.0},
        _stream_fell_back={CAM_ID: True},
        _local_rescue_attempts={CAM_ID: 1},
        _local_rescue_at={CAM_ID: 100.0},
        _renewal_tasks={},
        _camera_entities={CAM_ID: cam_entity},
        _stop_tls_proxy=AsyncMock(),
        _unregister_go2rtc_stream=AsyncMock(),
        # Mini-NVR Phase 1 — _tear_down_live_stream stops the recorder before
        # the proxy goes away. Empty dict = no recorder running, branch skipped.
        _nvr_processes={},
        _nvr_user_intent={},
        stop_recorder=AsyncMock(),
    ), cam_entity


# ── Cleanup invariants ──────────────────────────────────────────────────


class TestTearDownLiveStream:
    @pytest.mark.asyncio
    async def test_clears_all_per_cam_state(self):
        """Every cam-keyed dict must lose the cam_id entry."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        for d_name in (
            "_live_connections", "_live_opened_at", "_stream_error_count",
            "_stream_error_at", "_stream_fell_back",
            "_local_rescue_attempts", "_local_rescue_at",
        ):
            d = getattr(coord, d_name)
            assert CAM_ID not in d, f"{d_name} still has the cam entry after teardown"

    @pytest.mark.asyncio
    async def test_calls_tls_proxy_stop(self):
        """TLS proxy must be torn down so the camera detects disconnect."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        coord._stop_tls_proxy.assert_called_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_calls_go2rtc_unregister(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        coord._unregister_go2rtc_stream.assert_called_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_cancels_renewal_task_if_present(self):
        """If a LOCAL keepalive task is running, it must be cancelled."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        # Mock task
        task = MagicMock()
        task.done = MagicMock(return_value=False)
        task.cancel = MagicMock()
        coord._renewal_tasks[CAM_ID] = task
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_cancel_completed_task(self):
        """A task that's already done must not be cancelled (no-op safe)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        task = MagicMock()
        task.done = MagicMock(return_value=True)
        task.cancel = MagicMock()
        coord._renewal_tasks[CAM_ID] = task
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_no_camera_entity_gracefully(self):
        """If `_camera_entities[cam_id]` is missing (race during unload),
        teardown must not crash."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        coord._camera_entities = {}  # camera entity already gone
        # Must not raise
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_handles_no_stream_attr(self):
        """Camera entity exists but has no `stream` attribute (idle camera)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord(stream_obj=None)
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        # No exception is the assertion

    @pytest.mark.asyncio
    async def test_stops_camera_stream_when_present(self):
        """When `cam.stream` is non-None, it must get `await stream.stop()`."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        stream_mock = MagicMock()
        stream_mock.stop = AsyncMock()
        coord, cam_entity = _make_coord(stream_obj=stream_mock)
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        stream_mock.stop.assert_called_once()
        # And then cleared
        assert cam_entity.stream is None

    @pytest.mark.asyncio
    async def test_stream_stop_timeout_does_not_block(self):
        """If `stream.stop()` hangs, a 5 s wait_for limits the blockage —
        otherwise next stream-on waits indefinitely."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        async def _hang():
            await asyncio.sleep(60)  # would hang if not bounded

        stream_mock = MagicMock()
        stream_mock.stop = _hang
        coord, cam_entity = _make_coord(stream_obj=stream_mock)
        # Should complete in ≤ 5 s instead of hanging on stop()
        import time
        start = time.monotonic()
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        elapsed = time.monotonic() - start
        assert elapsed < 6.0, (
            f"Teardown took {elapsed:.1f}s — must bound stream.stop() at 5s "
            "to prevent the GH#6 yellow→blue→yellow loop"
        )
        # Stream reference cleared even after timeout
        assert cam_entity.stream is None

    @pytest.mark.asyncio
    async def test_idempotent_when_called_twice(self):
        """Double-tear-down must not raise — covers a race where
        privacy-on and live-stream-off fire near-simultaneously."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        # Second call: cam_id no longer in any dict
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        # `pop(cam_id, None)` is safe; assertion is "no exception"

    @pytest.mark.asyncio
    async def test_cam_id_not_yet_streaming_no_op(self):
        """Cam never had a live session → all dicts empty for this id →
        teardown still runs cleanly."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord, _ = _make_coord()
        # Reset to "never streamed"
        coord._live_connections = {}
        coord._live_opened_at = {}
        coord._stream_error_count = {}
        coord._stream_error_at = {}
        coord._stream_fell_back = {}
        coord._local_rescue_attempts = {}
        coord._local_rescue_at = {}
        coord._renewal_tasks = {}
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        # Still calls the cleanup hooks (idempotent at the proxy/go2rtc level)
        coord._stop_tls_proxy.assert_called_once_with(CAM_ID)
        coord._unregister_go2rtc_stream.assert_called_once_with(CAM_ID)
