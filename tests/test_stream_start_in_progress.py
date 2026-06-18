"""Regression tests for the concurrent-start "Live stream failed" false alarm.

Bug reported from a user's HA logs (2026-06-18):

    WARNING ... try_live_connection: already in progress for <cam> — skipping
    WARNING ... switch: Live stream failed for <cam> — check HA logs

A second, concurrent (non-renewal, non-recovery) start for a camera whose
per-camera stream lock is already held is *deliberately* skipped — the in-flight
start will publish the session. But `try_live_connection` used to return the
same `None` it returns on a real failure, so `BoschLiveStreamSwitch.async_turn_on`
mistook the de-dup skip for a failure and:

  1. logged a spurious "Live stream failed" WARNING,
  2. discarded the user's `_user_intent_streams` entry (set by the in-flight
     start it was coalescing into), and
  3. recorded a (false) stream error that nudges the camera toward REMOTE
     fallback after enough hits.

The stream actually came up ~1 min later in the real log — so all three were
false. Fix: the skip path returns the dedicated falsy sentinel
`STREAM_START_SKIPPED`; the switch (and camera play_stream) treat it as a
benign no-op while every other `if result:` consumer keeps seeing a falsy
value exactly as before.

These tests pin the contract.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED
from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── 1. The sentinel must be falsy and a unique singleton ──────────────────────


def test_sentinel_is_falsy_singleton():
    """The whole design relies on `STREAM_START_SKIPPED` being falsy so the
    many `if result:` / `if not result:` consumers keep treating it like the
    old `None` (no `.get()` on a non-dict), while the few harmful consumers
    compare with `is`."""
    assert bool(STREAM_START_SKIPPED) is False
    assert STREAM_START_SKIPPED is STREAM_START_SKIPPED
    # It must NOT be None — that is the whole point (None == real failure).
    assert STREAM_START_SKIPPED is not None


# ── 2. Coordinator returns the sentinel (not None) on the de-dup skip ─────────


class TestTryLiveConnectionSkipReturnsSentinel:
    @pytest.mark.asyncio
    async def test_returns_sentinel_when_lock_held_and_opportunistic(self):
        """lock held + not renewal + not force_reset → STREAM_START_SKIPPED."""
        held = asyncio.Lock()
        await held.acquire()
        try:
            coord = SimpleNamespace(
                _shc_state_cache={},  # privacy off
                _get_stream_lock=lambda _cid: held,
                _ensure_go2rtc_schemes_fresh=AsyncMock(),
            )
            result = await BoschCameraCoordinator.try_live_connection(
                coord, CAM_ID, is_renewal=False, force_reset=False
            )
            assert result is STREAM_START_SKIPPED
            # The opportunistic skip must short-circuit BEFORE the (throttled)
            # go2rtc scheme refresh — it does no work at all.
            coord._ensure_go2rtc_schemes_fresh.assert_not_called()
        finally:
            held.release()


# ── 3. Switch turn_on treats the sentinel as a benign no-op ───────────────────


def _make_switch_stub(coord):
    return SimpleNamespace(
        coordinator=coord,
        _cam_id=CAM_ID,
        _cam_title="Innenbereich",
        _last_stream_off=float("-inf"),  # never stopped (SENTINEL_RULE)
        _STREAM_COOLDOWN=BoschLiveStreamSwitch._STREAM_COOLDOWN,
        async_write_ha_state=MagicMock(),
        hass=MagicMock(),
    )


class TestTurnOnSkipIsNoOp:
    @pytest.mark.asyncio
    async def test_skip_keeps_intent_and_records_no_error(self):
        """The reported bug: on a coalesced start the switch must NOT revert
        intent and must NOT record a stream error (no false REMOTE nudge)."""
        record_error = MagicMock()
        coord = SimpleNamespace(
            _shc_state_cache={},
            _user_intent_streams=set(),
            try_live_connection=AsyncMock(return_value=STREAM_START_SKIPPED),
            record_stream_error=record_error,
            _bg_tasks=set(),
        )
        switch_stub = _make_switch_stub(coord)

        await BoschLiveStreamSwitch.async_turn_on(switch_stub)

        assert CAM_ID in coord._user_intent_streams, (
            "REGRESSION: a coalesced (in-progress) start dropped the user "
            "intent that the in-flight start legitimately set."
        )
        record_error.assert_not_called()
        switch_stub.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_real_failure_still_reverts_intent_and_records_error(self):
        """Contrast/guard: a genuine None failure must STILL revert intent and
        record an error — the fix must not swallow real failures."""
        record_error = MagicMock()
        coord = SimpleNamespace(
            _shc_state_cache={},
            _user_intent_streams=set(),
            try_live_connection=AsyncMock(return_value=None),  # real failure
            record_stream_error=record_error,
            _bg_tasks=set(),
        )
        switch_stub = _make_switch_stub(coord)

        await BoschLiveStreamSwitch.async_turn_on(switch_stub)

        assert CAM_ID not in coord._user_intent_streams
        record_error.assert_called_once_with(CAM_ID)
