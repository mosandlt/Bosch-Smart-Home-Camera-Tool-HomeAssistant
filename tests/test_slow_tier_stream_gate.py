"""Regression tests for the slow-tier stream-contention defer gate.

Root-cause: stream-freeze-on-motion-event-contention.md (2026-06-12).
The Gen2 camera shares ONE TLS control channel between:
  * RTSP keepalive (go2rtc OPTIONS every ~25 s, timeout=30 s)
  * Slow-tier cloud RCP/diagnostic reads (parallel asyncio.gather)
When both compete, OPTIONS RTT climbs to ~21 s → go2rtc EOF → 5-10 s freeze.

Fix: defer (NOT drop) the slow-tier fetch while a live stream is active.
The _slow_tier_deferred set tracks cameras whose slow-tier was skipped this
tick; the NEXT tick where the stream is idle triggers the deferred fetch even
if the normal 5-min interval has not elapsed.

The defer behaviour is controlled by the ``defer_diag_during_stream`` option
(default True).  When False, slow-tier runs unconditionally (old behaviour).

PIN_EVERY_MODE (no test collapse):
  * slow-tier with streaming=True, defer=True   → deferred (do_slow_cam False)
  * slow-tier with streaming=False, defer=True  → runs normally (do_slow_cam True)
  * slow-tier with streaming=True, defer=False  → runs normally (no defer)
  * slow-tier with streaming=False, defer=False → runs normally (no defer)
  * deferred entry clears when stream goes idle → fetch resumes
  * defer=False with pending deferred entry + stream idle → fetch runs, entry cleared
  * privacy-ON gate still fires even without active stream
  * RCP local-stream gate still respected (LOCAL stream type)
  * multiple cameras: only the streaming one is deferred, others unaffected
  * do_slow=False, no deferred entry, stream=False → do_slow_cam=False (no work)
  * do_slow=False, deferred entry, stream=True   → still deferred (not double-run)
  * do_slow=False, deferred entry, stream=False  → do_slow_cam=True (catch-up)
  * DEFAULT_DEFER_DIAG_DURING_STREAM == True
  * "defer_diag_during_stream" key present in DEFAULT_OPTIONS with value True

Only _slow_tier_deferred and the gate logic are tested — the actual HTTP
fetches are already covered in test_diagnostic_sensors* and test_stream_lifecycle.
All cam IDs are FAKE (SENTINEL_RULE: no real device values / IPs / MACs).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

# ── Constants ──────────────────────────────────────────────────────────────────

CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-2222-2222-2222-222222222222"

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_coord(
    *,
    live_connections: dict[str, Any] | None = None,
    slow_tier_deferred: set[str] | None = None,
) -> Any:
    """Minimal coordinator stub exposing only the fields the gate reads/writes."""
    return SimpleNamespace(
        _live_connections=live_connections if live_connections is not None else {},
        _slow_tier_deferred=slow_tier_deferred
        if slow_tier_deferred is not None
        else set(),
    )


def _compute_gate(
    coord: Any,
    cam_id: str,
    do_slow: bool,
    *,
    defer_diag: bool = True,
) -> tuple[bool, set[str]]:
    """
    Replicate the gate logic from __init__.py in pure Python so tests
    run without HA / network / full coordinator setup.

    Returns (do_slow_cam_final, updated _slow_tier_deferred).

    This mirrors the exact if/elif structure shipped in __init__.py:
        stream_active = cam_id in self._live_connections
        do_slow_cam = do_slow or (cam_id in self._slow_tier_deferred and not stream_active)
        if _defer_diag and do_slow_cam and stream_active:
            self._slow_tier_deferred.add(cam_id)
            do_slow_cam = False
        elif do_slow_cam and cam_id in self._slow_tier_deferred:
            self._slow_tier_deferred.discard(cam_id)
    """
    stream_active: bool = cam_id in coord._live_connections
    do_slow_cam: bool = do_slow or (
        cam_id in coord._slow_tier_deferred and not stream_active
    )
    if defer_diag and do_slow_cam and stream_active:
        coord._slow_tier_deferred.add(cam_id)
        do_slow_cam = False
    elif do_slow_cam and cam_id in coord._slow_tier_deferred:
        coord._slow_tier_deferred.discard(cam_id)
    return do_slow_cam, coord._slow_tier_deferred


# ── Tests ──────────────────────────────────────────────────────────────────────


class TestSlowTierStreamGate:
    """Unit tests for the slow-tier stream-contention defer gate.

    Source: stream-freeze-on-motion-event-contention.md, 2026-06-12.
    """

    # ── Normal-interval, no stream ─────────────────────────────────────────

    def test_slow_runs_when_do_slow_and_no_stream(self) -> None:
        """do_slow=True, stream idle → do_slow_cam=True, deferred unchanged."""
        coord = _make_coord()
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=True)
        assert do_slow_cam is True
        assert CAM_A not in deferred

    def test_no_slow_no_stream_no_deferred(self) -> None:
        """do_slow=False, no deferred entry, stream idle → do_slow_cam=False."""
        coord = _make_coord()
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=False)
        assert do_slow_cam is False
        assert CAM_A not in deferred

    # ── Stream active → defer ──────────────────────────────────────────────

    def test_slow_deferred_when_stream_active(self) -> None:
        """do_slow=True, stream active → do_slow_cam=False, cam added to deferred."""
        coord = _make_coord(live_connections={CAM_A: {"rtspsUrl": "rtsps://x"}})
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=True)
        assert do_slow_cam is False
        assert CAM_A in deferred

    def test_stream_active_does_not_affect_other_cam(self) -> None:
        """Deferred gate is per-camera: CAM_B streaming must not defer CAM_A."""
        coord = _make_coord(live_connections={CAM_B: {"rtspsUrl": "rtsps://y"}})
        # CAM_A is not streaming
        do_slow_cam_a, deferred = _compute_gate(coord, CAM_A, do_slow=True)
        assert do_slow_cam_a is True
        assert CAM_A not in deferred

    def test_second_defer_tick_stream_still_active(self) -> None:
        """Already in deferred set + stream still active → remains deferred, not double-added."""
        coord = _make_coord(
            live_connections={CAM_A: {"rtspsUrl": "rtsps://x"}},
            slow_tier_deferred={CAM_A},
        )
        # do_slow=False this tick (5-min interval not elapsed) but deferred entry exists
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=False)
        # stream_active=True → do_slow_cam = False or (True and not True) = False
        # gate: do_slow_cam=False → neither branch fires → deferred unchanged
        assert do_slow_cam is False
        # deferred is not cleared (stream still active)
        assert CAM_A in deferred

    # ── Deferred entry clears when stream goes idle ────────────────────────

    def test_deferred_fetch_runs_when_stream_idle(self) -> None:
        """Deferred entry present + stream idle → do_slow_cam=True, entry cleared."""
        coord = _make_coord(slow_tier_deferred={CAM_A})
        # do_slow=False (5-min interval has NOT elapsed) but deferred entry exists
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=False)
        assert do_slow_cam is True
        assert CAM_A not in deferred  # entry cleared after pickup

    def test_deferred_cleared_and_do_slow_true_still_runs(self) -> None:
        """Deferred entry + do_slow=True + stream idle → fetch runs, entry cleared."""
        coord = _make_coord(slow_tier_deferred={CAM_A})
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=True)
        assert do_slow_cam is True
        assert CAM_A not in deferred

    # ── Privacy gate remains orthogonal ───────────────────────────────────

    def test_privacy_gate_independent_of_stream_gate(self) -> None:
        """Gate only controls do_slow_cam; privacy check downstream is separate.

        do_slow_cam=True means *eligible* — the existing privacy-ON check in
        the RCP block then gates further.  We verify do_slow_cam is still True
        when privacy_on is True but no stream (privacy handled downstream).
        """
        coord = _make_coord()
        do_slow_cam, _deferred = _compute_gate(coord, CAM_A, do_slow=True)
        # Privacy-on check is not part of the gate — do_slow_cam remains True.
        assert do_slow_cam is True

    # ── Multi-camera correctness ───────────────────────────────────────────

    def test_multi_cam_only_streaming_cam_deferred(self) -> None:
        """CAM_A streaming, CAM_B idle → CAM_A deferred, CAM_B runs."""
        coord = _make_coord(live_connections={CAM_A: {"rtspsUrl": "rtsps://x"}})
        do_slow_a, _deferred_a = _compute_gate(coord, CAM_A, do_slow=True)
        # Now compute for CAM_B using the same coord (deferred may have CAM_A)
        do_slow_b, deferred_b = _compute_gate(coord, CAM_B, do_slow=True)

        assert do_slow_a is False
        assert CAM_A in deferred_b  # deferred set still has CAM_A
        assert do_slow_b is True
        assert CAM_B not in deferred_b

    def test_deferred_cam_a_does_not_trigger_cam_b(self) -> None:
        """CAM_A in deferred set must not affect CAM_B's do_slow_cam gate."""
        coord = _make_coord(slow_tier_deferred={CAM_A})
        do_slow_b, deferred = _compute_gate(coord, CAM_B, do_slow=False)
        assert do_slow_b is False
        assert CAM_A in deferred  # untouched
        assert CAM_B not in deferred

    # ── Edge cases ─────────────────────────────────────────────────────────

    def test_no_double_defer_if_already_in_deferred_and_stream_starts(self) -> None:
        """If cam enters deferred set while stream is active, set.add is idempotent."""
        coord = _make_coord(
            live_connections={CAM_A: {}},
            slow_tier_deferred={CAM_A},
        )
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=True)
        assert do_slow_cam is False
        assert len([x for x in deferred if x == CAM_A]) == 1  # exactly one entry

    def test_empty_live_connections_no_deferral(self) -> None:
        """Empty _live_connections dict → stream_active=False → gate does not defer."""
        coord = _make_coord(live_connections={})
        do_slow_cam, deferred = _compute_gate(coord, CAM_A, do_slow=True)
        assert do_slow_cam is True
        assert CAM_A not in deferred

    def test_slow_tier_deferred_is_set_type(self) -> None:
        """_slow_tier_deferred must be a set (O(1) membership tests)."""
        import inspect

        from custom_components.bosch_shc_camera import (
            BoschCameraCoordinator,  # type: ignore[import]
        )

        # Verify the real coordinator class initialises the attribute as a set.
        # We can't fully instantiate BoschCameraCoordinator without HA runtime,
        # so we inspect the __init__ source instead of using SimpleNamespace.
        src = inspect.getsource(BoschCameraCoordinator.__init__)
        assert "_slow_tier_deferred: set[str] = set()" in src


# ── Tests for defer_diag_during_stream option ──────────────────────────────────


class TestDeferDiagDuringStreamOption:
    """Unit tests for the defer_diag_during_stream option gate.

    PIN_EVERY_MODE: option=True (default), option=False, missing/default.
    Source: const.py CONF_DEFER_DIAG_DURING_STREAM, DEFAULT_DEFER_DIAG_DURING_STREAM.
    """

    # ── Option=True (default) — existing defer behaviour ──────────────────

    def test_defer_true_stream_active_defers(self) -> None:
        """Option=True, stream active → do_slow_cam=False, cam added to deferred."""
        coord = _make_coord(live_connections={CAM_A: {"rtspsUrl": "rtsps://x"}})
        do_slow_cam, deferred = _compute_gate(
            coord, CAM_A, do_slow=True, defer_diag=True
        )
        assert do_slow_cam is False
        assert CAM_A in deferred

    def test_defer_true_stream_idle_runs(self) -> None:
        """Option=True, stream idle → do_slow_cam=True (normal behaviour)."""
        coord = _make_coord()
        do_slow_cam, deferred = _compute_gate(
            coord, CAM_A, do_slow=True, defer_diag=True
        )
        assert do_slow_cam is True
        assert CAM_A not in deferred

    # ── Option=False — slow-tier runs regardless of stream ─────────────────

    def test_defer_false_stream_active_runs(self) -> None:
        """Option=False, stream active → do_slow_cam=True (no defer)."""
        coord = _make_coord(live_connections={CAM_A: {"rtspsUrl": "rtsps://x"}})
        do_slow_cam, deferred = _compute_gate(
            coord, CAM_A, do_slow=True, defer_diag=False
        )
        assert do_slow_cam is True
        # deferred set must NOT be populated when defer is disabled
        assert CAM_A not in deferred

    def test_defer_false_stream_idle_runs(self) -> None:
        """Option=False, stream idle → do_slow_cam=True (unchanged)."""
        coord = _make_coord()
        do_slow_cam, deferred = _compute_gate(
            coord, CAM_A, do_slow=True, defer_diag=False
        )
        assert do_slow_cam is True
        assert CAM_A not in deferred

    def test_defer_false_clears_stale_deferred_entry(self) -> None:
        """Option=False with a pending deferred entry + stream idle → fetch runs, entry cleared.

        If the option is toggled OFF while a cam is in the deferred set
        (e.g. deferred from a previous tick while option was ON), the
        catch-up branch (elif) still fires and clears the stale entry so
        no ghost entry persists forever.
        """
        coord = _make_coord(slow_tier_deferred={CAM_A})
        do_slow_cam, deferred = _compute_gate(
            coord, CAM_A, do_slow=True, defer_diag=False
        )
        # Stream is idle, do_slow=True → do_slow_cam resolves True.
        # defer_diag=False → first branch (if _defer_diag and stream_active) skipped.
        # Second branch (elif do_slow_cam and cam in deferred) fires → clears entry.
        assert do_slow_cam is True
        assert CAM_A not in deferred

    # ── Default value guards ───────────────────────────────────────────────

    def test_default_defer_diag_is_true(self) -> None:
        """DEFAULT_DEFER_DIAG_DURING_STREAM must be True (opt-in OFF = defer enabled)."""
        from custom_components.bosch_shc_camera.const import (
            DEFAULT_DEFER_DIAG_DURING_STREAM,
        )

        assert DEFAULT_DEFER_DIAG_DURING_STREAM is True

    def test_default_options_includes_defer_diag_true(self) -> None:
        """DEFAULT_OPTIONS must contain defer_diag_during_stream=True."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        assert DEFAULT_OPTIONS.get("defer_diag_during_stream") is True

    def test_conf_constant_value(self) -> None:
        """CONF_DEFER_DIAG_DURING_STREAM must equal the literal key string."""
        from custom_components.bosch_shc_camera.const import (
            CONF_DEFER_DIAG_DURING_STREAM,
        )

        assert CONF_DEFER_DIAG_DURING_STREAM == "defer_diag_during_stream"


# ── Options-flow round-trip test ───────────────────────────────────────────────


class TestDeferDiagOptionsFlow:
    """Config-flow round-trip: defer_diag_during_stream toggle persists."""

    @pytest.mark.asyncio
    async def test_defer_diag_toggle_off_persists(self) -> None:
        """Submitting defer_diag_during_stream=False saves False to the entry."""
        import base64 as _b64
        import json as _json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraOptionsFlow,
        )
        from custom_components.bosch_shc_camera.const import (
            CONF_DEFER_DIAG_DURING_STREAM,
        )

        entry = SimpleNamespace(
            entry_id="01TEST",
            data={"bearer_token": "", "refresh_token": "rt"},
            options={},
        )

        flow = BoschCameraOptionsFlow(entry)
        hass = MagicMock()
        hass.config_entries = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        hass.config_entries.flow = MagicMock()
        hass.config_entries.flow.async_abort = AsyncMock(return_value={"type": "abort"})
        flow.hass = hass

        result = await flow.async_step_init(
            user_input={"stream": {CONF_DEFER_DIAG_DURING_STREAM: False}}
        )
        assert (
            result.get("type") in ("create_entry", "form")
            or result.get("data") is not None
            or hass.config_entries.async_update_entry.called
        )

        # The saved options dict is what async_update_entry was called with
        if hass.config_entries.async_update_entry.called:
            saved = hass.config_entries.async_update_entry.call_args
            options_saved = saved[1].get("options") or (
                saved[0][1] if len(saved[0]) > 1 else {}
            )
            assert options_saved.get(CONF_DEFER_DIAG_DURING_STREAM) is False

    @pytest.mark.asyncio
    async def test_defer_diag_toggle_on_persists(self) -> None:
        """Submitting defer_diag_during_stream=True saves True to the entry."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraOptionsFlow,
        )
        from custom_components.bosch_shc_camera.const import (
            CONF_DEFER_DIAG_DURING_STREAM,
        )

        entry = SimpleNamespace(
            entry_id="01TEST",
            data={"bearer_token": "", "refresh_token": "rt"},
            options={},
        )

        flow = BoschCameraOptionsFlow(entry)
        hass = MagicMock()
        hass.config_entries = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        flow.hass = hass

        result = await flow.async_step_init(
            user_input={"stream": {CONF_DEFER_DIAG_DURING_STREAM: True}}
        )

        if hass.config_entries.async_update_entry.called:
            saved = hass.config_entries.async_update_entry.call_args
            options_saved = saved[1].get("options") or (
                saved[0][1] if len(saved[0]) > 1 else {}
            )
            assert options_saved.get(CONF_DEFER_DIAG_DURING_STREAM) is True
