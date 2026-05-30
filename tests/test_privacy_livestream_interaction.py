"""Regression tests for the privacy ↔ livestream coupling.

Bug reported by Thomas 2026-05-19: he turned privacy OFF on the Terrasse cam
and the `switch.bosch_terrasse_live_stream` was visibly ON, even though the
underlying RTSP session was dead. Repro context: the `_check_cooldown`
warning fired four times for the Terrasse privacy switch in the hour
before, so the user was rapidly toggling privacy.

Root causes identified:

  1. `_tear_down_live_stream` called `stop_recorder()` BEFORE popping
     `_live_connections`. The Mini-NVR is still BETA — if `stop_recorder`
     raises (file lock, missing ffmpeg child, permission glitch), the pop
     never runs and `BoschLiveStreamSwitch.is_on` stays True forever.

  2. The pop happens in the coordinator, but the switch entity is never
     told to push its new state to HA. The UI stays stale until the next
     coordinator refresh tick.

Fix (commits 2026-05-19):

  * Pop `_live_connections` (and the other per-cam dicts) FIRST so the
    visible state is correct even if anything later raises.
  * Wrap `stop_recorder()` in a try/except — log + continue on failure.
  * Add `_live_stream_entities` registry to the coordinator (mirrors
    `_camera_entities`); register on entity setup and call
    `async_write_ha_state()` after teardown so HA picks up the new state
    immediately instead of on the next coordinator tick.

These tests pin both contracts so the bug cannot regress silently.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(stream_obj=None, *, with_ls_entity: bool = False):
    """Coordinator stub with everything `_tear_down_live_stream` touches.

    Mirrors the stub in tests/test_stream_lifecycle.py but additionally
    seeds `_nvr_processes` (so the stop_recorder branch is exercised) and
    optionally seeds `_live_stream_entities` with a MagicMock entity that
    records calls to `async_write_ha_state()`.
    """
    cam_entity = SimpleNamespace(stream=stream_obj)
    coord = SimpleNamespace(
        _live_connections={CAM_ID: {"rtspsUrl": "rtsps://x"}},
        _user_intent_streams={CAM_ID},  # v12.4.12: user intent tracking
        _live_opened_at={CAM_ID: 100.0},
        _stream_error_count={CAM_ID: 2},
        _stream_error_at={CAM_ID: 100.0},
        _stream_fell_back={CAM_ID: True},
        _local_rescue_attempts={CAM_ID: 1},
        _local_rescue_at={CAM_ID: 100.0},
        _renewal_tasks={},
        _camera_entities={CAM_ID: cam_entity},
        _live_stream_entities={},
        _stop_tls_proxy=AsyncMock(),
        _unregister_go2rtc_stream=AsyncMock(),
        _nvr_processes={CAM_ID: object()},  # NVR is running for this cam
        _nvr_user_intent={CAM_ID: True},
        stop_recorder=AsyncMock(),
    )
    if with_ls_entity:
        ls_entity = MagicMock()
        ls_entity.hass = object()  # truthy, simulates "added to hass"
        ls_entity.async_write_ha_state = MagicMock()
        coord._live_stream_entities[CAM_ID] = ls_entity
        return coord, cam_entity, ls_entity
    return coord, cam_entity, None


# ── Bug 1: stop_recorder must not block the pop ──────────────────────────


class TestTeardownResilience:
    """`_live_connections` MUST be cleared before any operation that can fail,
    so `BoschLiveStreamSwitch.is_on` flips to False even when downstream
    cleanup hits an exception. Reported by Thomas 2026-05-19."""

    @pytest.mark.asyncio
    async def test_live_connections_cleared_before_stop_recorder(self):
        """Pin the call ORDER: pop happens before stop_recorder is awaited."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord()
        observations: list[bool] = []

        async def _stop_recorder_check(*args, **kwargs):
            observations.append(CAM_ID not in coord._live_connections)

        coord.stop_recorder = _stop_recorder_check

        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        assert observations == [True], (
            "REGRESSION: stop_recorder ran while _live_connections still had "
            "the cam entry. The pop must happen FIRST so the switch state is "
            "correct even when NVR teardown fails. Observation: "
            f"{observations}"
        )

    @pytest.mark.asyncio
    async def test_live_connections_cleared_even_if_stop_recorder_raises(self):
        """The pop must survive a stop_recorder OSError."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord()
        coord.stop_recorder = AsyncMock(side_effect=OSError("ffmpeg child gone"))

        # The teardown is allowed to swallow the OSError; what matters is the
        # state-dict invariant. If it re-raises, the test still passes as long
        # as _live_connections is empty afterwards.
        try:
            await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        except OSError:
            pass

        assert CAM_ID not in coord._live_connections, (
            "REGRESSION: stop_recorder raised and _live_connections still has "
            "the cam entry. BoschLiveStreamSwitch.is_on will stay True forever, "
            "reproducing Thomas's 2026-05-19 report (stream switch stuck on "
            "after privacy toggle)."
        )

    @pytest.mark.asyncio
    async def test_stop_recorder_exception_does_not_skip_proxy_cleanup(self):
        """A failed NVR stop must not leave the TLS proxy / go2rtc dangling."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord()
        coord.stop_recorder = AsyncMock(side_effect=RuntimeError("simulated"))

        try:
            await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        except RuntimeError:
            pass

        coord._stop_tls_proxy.assert_called_once_with(CAM_ID)
        coord._unregister_go2rtc_stream.assert_called_once_with(CAM_ID)


# ── Bug 2: state must be pushed to HA, not wait for next refresh ─────────


class TestTeardownStateWrite:
    """`async_write_ha_state()` must fire on the live-stream switch
    immediately after teardown so the UI does not show stale "on"."""

    @pytest.mark.asyncio
    async def test_state_write_fires_after_teardown(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, ls_entity = _make_coord(with_ls_entity=True)
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)

        assert ls_entity.async_write_ha_state.called, (
            "REGRESSION: live-stream switch entity was registered but "
            "_tear_down_live_stream did not push its new state. UI will stay "
            "stale until next coordinator refresh tick."
        )

    @pytest.mark.asyncio
    async def test_state_write_skipped_when_no_entity_registered(self):
        """No KeyError / AttributeError if no entity is registered for cam."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord(with_ls_entity=False)
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        # No assertion — the contract is "no exception".

    @pytest.mark.asyncio
    async def test_state_write_skipped_when_entity_not_yet_added_to_hass(self):
        """Don't call async_write_ha_state on an entity with hass=None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, ls_entity = _make_coord(with_ls_entity=True)
        ls_entity.hass = None  # entity registered but not yet added to hass
        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)

        assert not ls_entity.async_write_ha_state.called, (
            "Calling async_write_ha_state on an entity whose hass is None "
            "raises in HA core. Teardown must guard."
        )


# ── Integration-style: switch is_on contract after teardown ──────────────


class TestSwitchIsOnContract:
    """`BoschLiveStreamSwitch.is_on` reads `_live_connections`. After
    teardown, is_on must return False (not True). This is the contract
    Thomas relied on when reporting the bug."""

    @pytest.mark.asyncio
    async def test_is_on_false_after_teardown_with_nvr_failure(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord, _, _ = _make_coord()
        coord.stop_recorder = AsyncMock(side_effect=Exception("NVR died"))

        try:
            await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_ID)
        except Exception:
            pass

        # Synthetic switch instance — we only need is_on() to read coordinator
        # state correctly.
        switch_stub = SimpleNamespace(_cam_id=CAM_ID, coordinator=coord)
        assert BoschLiveStreamSwitch.is_on.fget(switch_stub) is False, (
            "User-visible regression: BoschLiveStreamSwitch.is_on returns True "
            "even though the stream is dead. Cosmetic-bug-of-record."
        )
