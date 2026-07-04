"""Regression tests for the `_user_intent_streams` decoupling.

Two related bugs were reported by Thomas in May 2026:

1) 2026-05-20 (`async_create_stream` auto-open):
   Dashboard / Cast / `camera.play_stream` service all cause HA Core to
   call `async_create_stream` on a camera entity, which our integration
   answers by calling `try_live_connection()` and populating
   `_live_connections[cam_id]`. The old `BoschLiveStreamSwitch.is_on`
   read directly from that dict, so the visible switch flipped to "on"
   even though the user never toggled it.

2) 2026-05-20 (health-watchdog race):
   `_stream_health_watchdog` runs as a background task after a successful
   user-driven turn-on. Between scheduling and the 60s tick, the user
   could turn the switch OFF (`_tear_down_live_stream` runs). The watchdog
   would still wake up and call `try_live_connection()` again — opening a
   stream the user no longer wanted.

Fix (v12.4.12):

  * Coordinator tracks user intent in `self._user_intent_streams: set[str]`.
  * `BoschLiveStreamSwitch.is_on` reads from that set, not `_live_connections`.
  * `async_turn_on` adds cam_id to the set BEFORE calling `try_live_connection`;
    reverts on failure.
  * `async_turn_off` discards cam_id BEFORE `_tear_down_live_stream`.
  * `_tear_down_live_stream` itself discards cam_id (privacy-on, NVR
    restart, external teardowns all end user intent too).
  * Health watchdog re-checks `cam_id in _user_intent_streams` after the
    sleep + before re-opening; bails if intent is gone.

These tests pin all four contracts.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── 1. Auto-open path: dashboard / Cast must NOT flip the switch ─────────


class TestAsyncCreateStreamDoesNotFlipSwitch:
    """`async_create_stream` (HA Core) → `stream_source()` → `try_live_connection()`
    populates `_live_connections`. The switch must NOT show "on" unless the
    user explicitly toggled it.
    """

    def test_is_on_false_when_only_live_connections_populated(self):
        """The Cast / dashboard / play_stream path populates `_live_connections`
        but not `_user_intent_streams`. Switch reads intent → off."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            _live_connections={CAM_ID: {"rtspsUrl": "rtsps://x"}},
            _user_intent_streams=set(),  # user did NOT toggle
        )
        stub = SimpleNamespace(_cam_id=CAM_ID, coordinator=coord)
        assert BoschLiveStreamSwitch.is_on.fget(stub) is False, (
            "REGRESSION: Switch shows on for auto-opened sessions. "
            "Dashboard / Cast / play_stream populates `_live_connections` "
            "but should not flip the user-facing switch."
        )

    def test_is_on_true_when_user_intent_set(self):
        """Once `async_turn_on` runs, `_user_intent_streams` is populated
        and the switch reads true regardless of `_live_connections`."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            _live_connections={},
            _user_intent_streams={CAM_ID},
        )
        stub = SimpleNamespace(_cam_id=CAM_ID, coordinator=coord)
        assert BoschLiveStreamSwitch.is_on.fget(stub) is True


# ── 2. Health-watchdog race: user OFF mid-sleep must abort reconnect ─────


class TestHealthWatchdogIntentCheck:
    """The watchdog must NOT re-open the stream after a user OFF during
    the 60s sleep. The new guard checks `_user_intent_streams` after the
    tear-down before calling `try_live_connection`."""

    @pytest.mark.asyncio
    async def test_watchdog_skips_reconnect_when_intent_gone(self, monkeypatch):
        """Pin: if user toggled OFF during sleep, watchdog bails without re-opening."""
        # Mock sleep so the test runs instantly
        import asyncio as _asyncio

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        async def _no_sleep(*_a, **_kw):
            pass

        monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

        cam_entity = SimpleNamespace(
            stream=SimpleNamespace(
                available=False
            ),  # unhealthy → would normally trigger reconnect
        )

        try_live = AsyncMock()
        coord = SimpleNamespace(
            _live_connections={
                CAM_ID: {"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"}
            },
            _user_intent_streams={CAM_ID},  # user initially toggled on
            _camera_entities={CAM_ID: cam_entity},
            _stream_error_count={},
            _stop_tls_proxy=AsyncMock(),
            try_live_connection=try_live,
            record_stream_error=MagicMock(),
            record_stream_success=MagicMock(),
            get_model_config=MagicMock(
                return_value=SimpleNamespace(max_stream_errors=3)
            ),
        )

        # Simulate that the user toggled OFF between schedule and fire: the
        # OFF handler clears intent + _live_connections by the time the
        # watchdog wakes up and reaches the reconnect step.
        async def _stop_then_clear(*_a, **_kw):
            coord._user_intent_streams.discard(CAM_ID)

        coord._stop_tls_proxy = _stop_then_clear

        switch_stub = SimpleNamespace(
            coordinator=coord,
            async_write_ha_state=MagicMock(),
            _cam_title="Terrasse",
        )

        await BoschLiveStreamSwitch._stream_health_watchdog(switch_stub, CAM_ID)

        (
            try_live.assert_not_called(),
            (
                "REGRESSION: Watchdog called try_live_connection after the user "
                "toggled OFF. The intent check between _stop_tls_proxy and "
                "try_live_connection is broken."
            ),
        )

    @pytest.mark.asyncio
    async def test_watchdog_reconnects_when_intent_still_present(self, monkeypatch):
        """Sanity: if intent is still True, watchdog DOES reconnect."""
        import asyncio as _asyncio

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        async def _no_sleep(*_a, **_kw):
            pass

        monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))

        try_live = AsyncMock(return_value={"_connection_type": "REMOTE"})
        coord = SimpleNamespace(
            _live_connections={
                CAM_ID: {"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"}
            },
            _user_intent_streams={CAM_ID},
            _camera_entities={CAM_ID: cam_entity},
            _stream_error_count={},
            _stop_tls_proxy=AsyncMock(),
            try_live_connection=try_live,
            record_stream_error=MagicMock(),
            record_stream_success=MagicMock(),
            get_model_config=MagicMock(
                return_value=SimpleNamespace(max_stream_errors=3)
            ),
        )

        switch_stub = SimpleNamespace(
            coordinator=coord,
            async_write_ha_state=MagicMock(),
            _cam_title="Terrasse",
        )

        await BoschLiveStreamSwitch._stream_health_watchdog(switch_stub, CAM_ID)

        try_live.assert_called_once_with(CAM_ID)


# ── 3. Teardown also clears intent ────────────────────────────────────────


class TestTeardownClearsIntent:
    """External teardowns (privacy on, health-watchdog REMOTE escalation,
    NVR restart) genuinely end user intent. `_tear_down_live_stream` must
    discard cam_id from `_user_intent_streams`."""

    @pytest.mark.asyncio
    async def test_teardown_clears_intent(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = SimpleNamespace(
            _stream_locks={},
            _live_connections={CAM_ID: {"rtspsUrl": "rtsps://x"}},
            _user_intent_streams={CAM_ID},
            _live_opened_at={CAM_ID: 100.0},
            _stream_error_count={},
            _stream_error_at={},
            _stream_fell_back={},
            _local_rescue_attempts={},
            _local_rescue_at={},
            _stream_warming={CAM_ID},
            _stream_warming_started={CAM_ID: 100.0},
            _renewal_tasks={},
            _reaper_tasks={},
            _session_idle_since={},
            _camera_entities={},
            _live_stream_entities={},
            _stop_tls_proxy=AsyncMock(),
            _unregister_go2rtc_stream=AsyncMock(),
            _nvr_processes={},
            _nvr_user_intent={},
            stop_recorder=AsyncMock(),
        )
        coord._get_stream_lock = lambda cam_id: coord._stream_locks.setdefault(
            cam_id, asyncio.Lock()
        )

        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)

        assert CAM_ID not in coord._user_intent_streams, (
            "REGRESSION: _tear_down_live_stream did not clear user intent. "
            "Privacy ON / health escalation should reset the switch state."
        )


# ── 4. Failed turn_on reverts intent ─────────────────────────────────────


class TestTurnOnFailureRevertsIntent:
    """`async_turn_on` sets intent BEFORE attempting the connection. If
    `try_live_connection` returns None (failure), intent must be reverted
    so the switch doesn't get stuck on 'on' with a dead session."""

    @pytest.mark.asyncio
    async def test_intent_reverted_on_try_live_connection_failure(self):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            _shc_state_cache={},
            _user_intent_streams=set(),
            try_live_connection=AsyncMock(return_value=None),  # failure
            record_stream_error=MagicMock(),
            _bg_tasks=set(),
        )
        switch_stub = SimpleNamespace(
            coordinator=coord,
            _cam_id=CAM_ID,
            _cam_title="Terrasse",
            _last_stream_off=float("-inf"),  # never stopped (SENTINEL_RULE)
            _STREAM_COOLDOWN=BoschLiveStreamSwitch._STREAM_COOLDOWN,
            async_write_ha_state=MagicMock(),
            hass=MagicMock(),
        )

        await BoschLiveStreamSwitch.async_turn_on(switch_stub)

        assert CAM_ID not in coord._user_intent_streams, (
            "REGRESSION: failed turn_on left _user_intent_streams populated. "
            "The switch would report 'on' against a non-existent session."
        )
