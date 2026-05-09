"""Tests for _StreamSupportNoiseFilter in __init__.py.

Covers the two burst-suppression cases:

  1. "does not support play stream service" — per-entity rate-limit 30 s
     (pre-warm window while stream_source() returns None)

  2. "Camera not found" inside "Error requesting stream" — global rate-limit
     60 s (startup race: browser reconnects before go2rtc re-registers stream)

Regression: Camera-not-found burst (count=17) appeared in system log after
HA restart when the browser immediately requested WebRTC for cameras not yet
registered in go2rtc.
"""

from __future__ import annotations

import logging
from unittest.mock import patch


MODULE = "custom_components.bosch_shc_camera"


def _make_filter():
    """Import and instantiate a fresh _StreamSupportNoiseFilter."""
    from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
    return _StreamSupportNoiseFilter()


def _record(msg: str, logger_name: str = "homeassistant.components.camera") -> logging.LogRecord:
    record = logging.LogRecord(
        name=logger_name,
        level=logging.ERROR,
        pathname="",
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )
    return record


# ── "does not support play stream service" — per-entity rate-limit ────────────


class TestPlayStreamServiceRateLimit:
    """Pin the 30 s per-entity rate-limit for the pre-warm burst."""

    def test_first_occurrence_passes(self):
        f = _make_filter()
        rec = _record("Error requesting stream: camera.bosch_terrasse does not support play stream service")
        assert f.filter(rec) is True

    def test_second_occurrence_within_30s_is_suppressed(self):
        f = _make_filter()
        rec = _record("Error requesting stream: camera.bosch_innenbereich does not support play stream service")
        f.filter(rec)  # let first through
        assert f.filter(rec) is False

    def test_different_entities_tracked_independently(self):
        f = _make_filter()
        rec_a = _record("Error requesting stream: camera.bosch_terrasse does not support play stream service")
        rec_b = _record("Error requesting stream: camera.bosch_innenbereich does not support play stream service")
        assert f.filter(rec_a) is True
        assert f.filter(rec_b) is True  # different entity — passes

    def test_non_bosch_entity_is_never_suppressed(self):
        f = _make_filter()
        rec = _record("Error requesting stream: camera.generic_cam does not support play stream service")
        assert f.filter(rec) is True
        assert f.filter(rec) is True  # second occurrence also passes

    def test_after_30s_window_passes_again(self):
        f = _make_filter()
        rec = _record("Error requesting stream: camera.bosch_kamera does not support play stream service")
        f.filter(rec)
        # Manually expire the window
        f._last_passed["bosch_kamera"] = float('-inf')
        assert f.filter(rec) is True

    def test_unrelated_message_passes_through(self):
        f = _make_filter()
        rec = _record("Some other camera warning")
        assert f.filter(rec) is True
        assert f.filter(rec) is True  # no rate-limiting for unrelated messages

    def test_max_tracked_prunes_oldest_entry(self):
        """When dict reaches _MAX_TRACKED, oldest entry is evicted."""
        f = _make_filter()
        # Fill up to max
        for i in range(f._MAX_TRACKED):
            f._last_passed[f"bosch_cam_{i:03d}"] = float(i)
        assert len(f._last_passed) == f._MAX_TRACKED
        # One more bosch entity triggers pruning
        rec = _record("Error requesting stream: camera.bosch_new does not support play stream service")
        f.filter(rec)
        assert len(f._last_passed) <= f._MAX_TRACKED


# ── "Camera not found" — global rate-limit ───────────────────────────────────


class TestCameraNotFoundRateLimit:
    """Pin the 60 s global rate-limit for the go2rtc startup race burst.

    When HA restarts and the browser reconnects, it sees cached 'streaming'
    state and immediately requests WebRTC for cameras not yet registered in
    go2rtc. go2rtc returns 404 → HA logs "Camera not found". This burst
    (up to 17 occurrences in 36 s) is rate-limited to 1 per 60 s.
    """

    def test_first_occurrence_passes(self):
        f = _make_filter()
        rec = _record("Error requesting stream: Camera not found")
        assert f.filter(rec) is True

    def test_second_occurrence_within_60s_is_suppressed(self):
        f = _make_filter()
        rec = _record("Error requesting stream: Camera not found")
        f.filter(rec)
        assert f.filter(rec) is False

    def test_burst_of_17_collapses_to_1(self):
        f = _make_filter()
        rec = _record("Error requesting stream: Camera not found")
        results = [f.filter(rec) for _ in range(17)]
        assert results[0] is True
        assert all(r is False for r in results[1:]), (
            "Camera-not-found burst must be collapsed to 1 occurrence"
        )

    def test_after_60s_window_passes_again(self):
        f = _make_filter()
        rec = _record("Error requesting stream: Camera not found")
        f.filter(rec)
        f._last_passed[f._NOT_FOUND_KEY] = float('-inf')
        assert f.filter(rec) is True

    def test_camera_not_found_without_error_requesting_prefix_passes(self):
        """Only suppress when message contains 'Error requesting stream' + 'Camera not found'."""
        f = _make_filter()
        rec = _record("Camera not found for some other reason")
        assert f.filter(rec) is True
        assert f.filter(rec) is True  # not rate-limited

    def test_not_found_and_play_service_use_separate_buckets(self):
        """'Camera not found' and 'does not support' rate-limits don't interfere."""
        f = _make_filter()
        rec_nf = _record("Error requesting stream: Camera not found")
        rec_ps = _record("Error requesting stream: camera.bosch_terrasse does not support play stream service")
        # Let both through once
        assert f.filter(rec_nf) is True
        assert f.filter(rec_ps) is True
        # Suppress both on second call
        assert f.filter(rec_nf) is False
        assert f.filter(rec_ps) is False

    def test_window_constant_is_60s(self):
        """_NOT_FOUND_WINDOW must be 60 s — long enough to cover a typical startup burst."""
        from custom_components.bosch_shc_camera import _StreamSupportNoiseFilter
        assert _StreamSupportNoiseFilter._NOT_FOUND_WINDOW == 60.0, (
            "Camera-not-found window must be 60 s to cover the go2rtc startup race burst"
        )


# ── _install_stream_support_noise_filter idempotency ─────────────────────────


class TestInstallIdempotent:
    """Installing the filter twice must not add duplicate filters."""

    def test_double_install_adds_only_one_filter(self):
        from custom_components.bosch_shc_camera import (
            _StreamSupportNoiseFilter,
            _install_stream_support_noise_filter,
        )
        cam_logger = logging.getLogger("homeassistant.components.camera")
        # Remove any existing Bosch filters for a clean slate
        cam_logger.filters = [f for f in cam_logger.filters if not isinstance(f, _StreamSupportNoiseFilter)]
        _install_stream_support_noise_filter()
        _install_stream_support_noise_filter()
        bosch_filters = [f for f in cam_logger.filters if isinstance(f, _StreamSupportNoiseFilter)]
        assert len(bosch_filters) == 1, (
            f"Expected exactly 1 _StreamSupportNoiseFilter, got {len(bosch_filters)}"
        )
        # Clean up
        cam_logger.filters = [f for f in cam_logger.filters if not isinstance(f, _StreamSupportNoiseFilter)]
