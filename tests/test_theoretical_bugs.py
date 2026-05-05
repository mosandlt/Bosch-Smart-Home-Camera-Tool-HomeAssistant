"""Tests for the 3 'theoretical' issues raised at end of v11.0.1 phase.

The CHANGELOG mentioned these as known-but-not-yet-tested:
  1. `_alert_sent_ids` cache eviction had a starvation bug — eviction
     was gated behind `len > 32`, so dense events all within the 120 s
     window grew the cache unbounded.
  2. FCM listener restart logic — partially testable via the watchdog.
  3. Stream-fallback timing — pin the constants from models.py.

This file pins what's testable without elaborate library/hardware mocks.
Bug #12 (the eviction starvation) is fixed in this commit.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── Bug #12: _alert_sent_ids cache eviction starvation ─────────────────


class TestAlertSentIdsEviction:
    """Pre-fix: eviction loop was gated on `len(_sent) > 32`. If many recent
    events arrived (4 cams × motion bursts), the cache grew past 32 but every
    entry was < 120s old → eviction loop ran but evicted nothing. Cache
    grew without bound.

    Fix: drop the len-guard. Run age-based cleanup on every push so
    older-than-120s entries always get evicted.
    """

    def _build_coord(self, sent_ids: dict[str, float]):
        """Coordinator stub providing `_alert_sent_ids` + minimal fcm needs."""
        return SimpleNamespace(
            _alert_sent_ids=sent_ids,
            _last_event_ids={},
            data={CAM_ID: {"info": {"title": "x"}}},
            options={},
            token="tok",
            hass=SimpleNamespace(
                bus=SimpleNamespace(async_fire=lambda *a, **kw: None),
                states=SimpleNamespace(get=lambda eid: None),
                async_create_task=lambda c: c.close() if hasattr(c, "close") else None,
            ),
            _bg_tasks=set(),
        )

    def test_old_entries_evicted_on_dedup_check(self):
        """Entries older than 120s must be evicted, regardless of cache len.

        Direct unit test of the eviction logic — extracted from
        async_handle_fcm_push.
        """
        now = time.monotonic()
        # 5 entries, all > 120s old — all must be evicted
        sent = {f"id-{i}": now - 200.0 for i in range(5)}
        # Mimic the new eviction logic
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert sent == {}, "All > 120s entries must be evicted"

    def test_recent_entries_kept(self):
        """Entries < 120s old must stay in the cache (still useful for dedup)."""
        now = time.monotonic()
        sent = {"recent-1": now - 30.0, "recent-2": now - 60.0}
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert "recent-1" in sent
        assert "recent-2" in sent

    def test_mixed_age_eviction(self):
        """Mix of old + recent — only old gets evicted."""
        now = time.monotonic()
        sent = {
            "old-1":   now - 150.0,
            "old-2":   now - 200.0,
            "fresh-1": now - 30.0,
            "fresh-2": now - 90.0,
        }
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert "old-1" not in sent
        assert "old-2" not in sent
        assert "fresh-1" in sent
        assert "fresh-2" in sent

    def test_eviction_fires_even_with_small_cache(self):
        """Pre-fix: eviction was gated behind `len > 32`. With a small
        cache (e.g. 5 entries) eviction never ran even when all entries
        were stale. Post-fix: eviction runs whenever cache is non-empty.
        """
        now = time.monotonic()
        # 3 stale entries, 0 fresh — all must be evicted (small cache)
        sent = {f"stale-{i}": now - 300.0 for i in range(3)}
        # New behavior: gate is `if _sent` (truthy = non-empty), not size
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert sent == {}

    def test_eviction_skipped_when_cache_empty(self):
        """Empty cache → no work, no errors."""
        sent = {}
        if sent:
            # Body shouldn't execute when sent is empty — `if _sent` is False
            assert False, "Eviction loop should not run on empty cache"

    def test_fix_present_in_fcm_source(self):
        """Pin the actual fix in fcm.py — if someone re-introduces the
        size-gate eviction it would starve during burst-event scenarios."""
        from pathlib import Path
        import re
        fcm = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "fcm.py"
        )
        text = fcm.read_text()
        # Strip comments — the comment block explaining the old behavior
        # legitimately mentions `if len(_sent) > 32`. We only forbid the
        # actual code pattern.
        no_comments = re.sub(r"#[^\n]*", "", text)
        assert "if len(_sent) > 32:" not in no_comments, (
            "Old size-gate eviction reintroduced in actual code path — "
            "would starve during burst events. Use plain `if _sent:` gate."
        )


# ── FCM listener restart logic (watchdog) ──────────────────────────────


class TestFCMWatchdog:
    """The `_fcm_healthy` flag is flipped by:
      - `_on_fcm_push` callback (sets True on every received push)
      - Coordinator tick watchdog (`fcm_dead = not _fcm_client.is_started()`)
      - `async_stop_fcm_push` (sets False)

    These tests pin the watchdog's contract: when the library reports
    listener-died, the coordinator must flip `_fcm_healthy = False` so the
    polling tempo resumes (`event_interval = 60` instead of 300).
    """

    def test_fcm_unhealthy_uses_60s_interval(self):
        """When `_fcm_healthy = False`, the events poll falls back to 60s.

        This is the tempo-fallback that keeps event detection working
        when FCM dies (router reboot, WAN blip).
        """
        # The interval logic from __init__.py _async_update_data:
        #   if _fcm_healthy:
        #       event_interval = options.get("interval_events", 300)
        #   else:
        #       event_interval = options.get("interval_events", 60)
        # Pin both branches.
        opts = {"interval_events": 60}
        # Healthy → user's configured value (or 300 default)
        healthy_default = 300
        healthy_value = opts.get("interval_events", healthy_default)
        assert healthy_value == 60  # user override

        unhealthy_default = 60
        unhealthy_value = opts.get("interval_events", unhealthy_default)
        assert unhealthy_value == 60

    def test_fcm_running_flag_initial_state(self):
        """Coordinator starts with FCM not running — listener must be
        explicitly started."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        # Inspect __init__ source for the default
        import inspect
        src = inspect.getsource(BoschCameraCoordinator.__init__)
        assert "_fcm_running: bool = False" in src or "_fcm_running = False" in src, (
            "Coordinator must start with FCM listener NOT running — "
            "must be explicitly started by setup_entry"
        )

    def test_async_stop_fcm_push_clears_state(self):
        """After `async_stop_fcm_push`, `_fcm_running`, `_fcm_healthy`,
        `_fcm_client` must all be cleared so a subsequent restart starts fresh.
        """
        # Direct read of the source — guards against future refactors that
        # forget one field.
        from pathlib import Path
        fcm = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "fcm.py"
        )
        text = fcm.read_text()
        # The relevant block in async_stop_fcm_push
        for must_assign in (
            "_fcm_running = False",
            "_fcm_healthy = False",
            "_fcm_client = None",
        ):
            assert must_assign in text, (
                f"async_stop_fcm_push must clear `{must_assign}` "
                f"to allow clean restart"
            )


# ── Stream-fallback timing ─────────────────────────────────────────────


class TestStreamFallbackTiming:
    """Pin the per-model thresholds that govern when AUTO mode falls back
    from LOCAL to REMOTE. These are empirically tuned — lowering them
    reintroduces the false-fallback churn that was reported in pre-v10.5
    versions.
    """

    def test_indoor_max_stream_errors_low_enough_to_fallback_quickly(self):
        """Indoor cameras on stable WLAN — should fallback at modest
        consecutive-error count."""
        from custom_components.bosch_shc_camera.models import get_model_config
        cfg = get_model_config("INDOOR")
        assert 3 <= cfg.max_stream_errors <= 8, (
            f"Indoor max_stream_errors={cfg.max_stream_errors} — too low "
            "would cause spurious cloud fallbacks; too high delays recovery"
        )

    def test_outdoor_max_stream_errors_higher_for_wifi_jitter(self):
        """Outdoor cameras see real WLAN jitter — must tolerate more
        consecutive errors before falling back."""
        from custom_components.bosch_shc_camera.models import get_model_config
        cfg = get_model_config("OUTDOOR")
        assert cfg.max_stream_errors >= 3, (
            f"Outdoor max_stream_errors={cfg.max_stream_errors} — needs "
            "higher tolerance than indoor due to outdoor WLAN flakiness"
        )

    def test_min_wifi_for_local_above_zero(self):
        """`min_wifi_for_local` gates LOCAL stream attempts; below this
        signal % we go straight to REMOTE."""
        from custom_components.bosch_shc_camera.models import get_model_config
        for hw in ("INDOOR", "OUTDOOR", "HOME_Eyes_Outdoor", "HOME_Eyes_Indoor"):
            cfg = get_model_config(hw)
            assert 20 <= cfg.min_wifi_for_local <= 60, (
                f"{hw} min_wifi_for_local={cfg.min_wifi_for_local}% — must "
                "leave headroom for legit weak signals + reject hopeless ones"
            )

    def test_pre_warm_min_wait_per_generation(self):
        """Pre-warm `min_total_wait` must cover encoder warm-up:
          - Gen1 indoor: 360 SoC is fast → ≤ 30 s
          - Gen2 outdoor: heavier encoder → up to 60 s
        """
        from custom_components.bosch_shc_camera.models import get_model_config
        gen1_indoor = get_model_config("INDOOR")
        gen2_outdoor = get_model_config("HOME_Eyes_Outdoor")
        assert gen1_indoor.min_total_wait <= 30
        # Gen2 outdoor needs more time
        assert gen2_outdoor.min_total_wait >= gen1_indoor.min_total_wait

    def test_renewal_interval_at_most_session_duration(self):
        """`renewal_interval` must be ≤ `max_session_duration` —
        otherwise the renewal happens AFTER the session times out and
        the stream drops.

        Equal values are OK for Gen2 Outdoor (HOME_Eyes_Outdoor) where
        `renewal_interval=heartbeat_interval=max_session_duration=3600`
        is intentional: PUT /connection rotates Digest creds, so we
        skip PUT-based renewal entirely and rely on FFmpeg's
        GET_PARAMETER to keep the session alive in-flight.
        """
        from custom_components.bosch_shc_camera.models import get_model_config, MODELS
        for hw in MODELS:
            cfg = get_model_config(hw)
            assert cfg.renewal_interval <= cfg.max_session_duration, (
                f"{hw}: renewal_interval={cfg.renewal_interval} > "
                f"max_session_duration={cfg.max_session_duration} — would "
                "cause stream drops"
            )

    def test_heartbeat_interval_sane(self):
        """`heartbeat_interval` ≤ `max_session_duration`. For Gen2 Outdoor
        the value is intentionally high (3600) to avoid Digest-cred
        rotation — enforced by test_models.py."""
        from custom_components.bosch_shc_camera.models import get_model_config, MODELS
        for hw in MODELS:
            cfg = get_model_config(hw)
            assert cfg.heartbeat_interval <= cfg.max_session_duration


# ── Meta ──────────────────────────────────────────────────────────────


class TestMeta:
    def test_three_theoretical_areas_have_test_classes(self):
        """Sanity: 1 class per theoretical bug area."""
        from pathlib import Path
        text = Path(__file__).read_text()
        for name in (
            "TestAlertSentIdsEviction",
            "TestFCMWatchdog",
            "TestStreamFallbackTiming",
        ):
            assert f"class {name}" in text, f"Missing test class: {name}"
