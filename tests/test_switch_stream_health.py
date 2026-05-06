"""Tests for `BoschLiveStreamSwitch._stream_health_watchdog` (Round 4).

The watchdog runs after a LOCAL stream-on. It probes HA's `Stream`
object at +60s and +120s. Three states map to three actions:
  - "healthy" (Stream.available True) → record_stream_success, exit
  - "no_consumer" (no Stream object) → exit silently (FFmpeg never
    started, restart wouldn't help — frontend card unmounted)
  - "unhealthy" (Stream object but available=False) → at first tick
    record_stream_error + restart; at second tick saturate the error
    counter to force REMOTE on next try_live_connection.

This is the v10.4.0 fix that catches the GH#6 yellow→blue→yellow
cycle that polling alone missed.

We bypass the 2× 60s sleeps via `patch("asyncio.sleep")` so the test
runs in milliseconds. The watchdog does its escalation purely through
coordinator state mutations + try_live_connection() calls.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _make_coord(**overrides):
    base = dict(
        _live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
        _camera_entities={},
        _stream_error_count={},
        _stop_tls_proxy=AsyncMock(),
        try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        record_stream_error=MagicMock(),
        record_stream_success=MagicMock(),
        get_model_config=lambda cid: SimpleNamespace(max_stream_errors=3),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_switch(coord=None):
    """Build a BoschLiveStreamSwitch stub bypassing __init__."""
    from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
    coord = coord or _make_coord()
    sw = BoschLiveStreamSwitch.__new__(BoschLiveStreamSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw.async_write_ha_state = MagicMock()
    return sw


# ── State classifier branches ────────────────────────────────────────────


class TestHealthClassifier:
    @pytest.mark.asyncio
    async def test_healthy_stream_calls_record_success(self):
        """Stream.available=True → record_stream_success + exit early.
        No restart, no further escalation. Pin so the success path
        keeps clearing the per-cam error counter."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=True))
        coord = _make_coord(_camera_entities={CAM_ID: cam_entity})
        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.record_stream_success.assert_called_once_with(CAM_ID)
        coord.try_live_connection.assert_not_awaited()
        coord._stop_tls_proxy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_consumer_exits_silently(self):
        """No camera entity stream object → FFmpeg never started, so
        restarting the LOCAL session wouldn't help. Exit silently
        leaving the LOCAL session up for a future consumer."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        cam_entity = SimpleNamespace(stream=None)
        coord = _make_coord(_camera_entities={CAM_ID: cam_entity})
        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()
        coord.record_stream_error.assert_not_called()
        coord.record_stream_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_camera_entity_treated_as_no_consumer(self):
        """Camera entity not yet registered (race) → same outcome as
        no Stream object: silent exit."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        coord = _make_coord(_camera_entities={})  # cam not in dict
        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()


# ── Stream-off short-circuit ─────────────────────────────────────────────


class TestStreamOffShortCircuit:
    @pytest.mark.asyncio
    async def test_user_turned_off_during_first_sleep(self):
        """Live conn cleared between watchdog start and first tick →
        nothing to watch, exit. Pin so the watchdog doesn't fire
        spurious restarts after the user already turned the switch off."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        coord = _make_coord(_live_connections={})  # already off
        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_remote_fallback_short_circuits(self):
        """If something else (manual mode change, REMOTE fallback)
        flipped the connection to REMOTE, the LOCAL watchdog stops —
        REMOTE has no LOCAL-specific failure modes."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        coord = _make_coord(_live_connections={
            CAM_ID: {"_connection_type": "REMOTE"},
        })
        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()


# ── Unhealthy → restart path ─────────────────────────────────────────────


class TestUnhealthyRestart:
    @pytest.mark.asyncio
    async def test_first_unhealthy_records_error_and_restarts(self):
        """First unhealthy tick → record_stream_error + tear down +
        try_live_connection. Counter NOT yet saturated — gradual
        escalation via per-model threshold."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        # Stream object exists but available=False initially
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))
        coord = _make_coord(_camera_entities={CAM_ID: cam_entity})

        # Mock try_live_connection to repopulate _live_connections
        # (the real method does this; the mock must too for the watchdog
        # to find the live conn on the next tick).
        async def _restart(cid):
            coord._live_connections[cid] = {"_connection_type": "LOCAL"}
            return {"_connection_type": "LOCAL"}

        coord.try_live_connection = AsyncMock(side_effect=_restart)

        # Flip stream to healthy after the first sleep so the second
        # tick records success + exits cleanly.
        sleep_calls = [0]

        async def _sleep(_delay):
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                cam_entity.stream = SimpleNamespace(available=True)

        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep)):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.record_stream_error.assert_called_once_with(CAM_ID)
        coord._stop_tls_proxy.assert_awaited_once_with(CAM_ID)
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        # Second tick was healthy → success recorded
        coord.record_stream_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_unhealthy_saturates_counter(self):
        """Second consecutive unhealthy tick → bypass the per-model
        threshold by setting `_stream_error_count` directly to
        max_stream_errors. Forces REMOTE on the next try_live_connection."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        # Always unhealthy
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))
        coord = _make_coord(_camera_entities={CAM_ID: cam_entity})

        async def _restart(cid):
            coord._live_connections[cid] = {"_connection_type": "LOCAL"}
            return {"_connection_type": "LOCAL"}

        coord.try_live_connection = AsyncMock(side_effect=_restart)
        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        # Counter saturated at max_stream_errors (=3 in our stub config)
        assert coord._stream_error_count[CAM_ID] == 3
        # Two restart attempts (first + second tick)
        assert coord._stop_tls_proxy.await_count == 2
        assert coord.try_live_connection.await_count == 2

    @pytest.mark.asyncio
    async def test_remote_fallback_after_first_unhealthy_exits(self):
        """First tick unhealthy → restart. try_live_connection picks
        REMOTE this time → exit (no second tick)."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))
        coord = _make_coord(_camera_entities={CAM_ID: cam_entity})
        # Restart returns REMOTE → watchdog exits
        coord.try_live_connection = AsyncMock(
            return_value={"_connection_type": "REMOTE"},
        )
        sw = _make_switch(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        # One restart → REMOTE → exit (no second sleep)
        assert coord.try_live_connection.await_count == 1
        sw.async_write_ha_state.assert_called()
