"""Coverage tests for the FCM noise-filter helpers in `fcm.py`.

Targets the public helpers used by the coordinator's watchdog:
- ``get_recent_fcm_error_count`` — counts shared timestamps within a window.
- ``reset_fcm_error_counter`` — clears the shared list after self-heal.
- ``_install_fcm_noise_filter`` — idempotent install on both loggers.

These touch shared module state via ``_FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS``,
so each test snapshots + restores the list to stay order-independent.
"""

from __future__ import annotations

import logging
import time
from unittest.mock import patch

import pytest

from custom_components.bosch_shc_camera import fcm
from custom_components.bosch_shc_camera.fcm import (
    _FCMNoiseFilter,
    _install_fcm_noise_filter,
    get_recent_fcm_error_count,
    reset_fcm_error_counter,
)


@pytest.fixture(autouse=True)
def _isolate_shared_state():
    """Snapshot + restore both shared lists + logger filters so tests cannot
    leak filter installs into other test modules."""
    prev_ts = list(_FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS)
    lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
    bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
    prev_lib = list(lib_logger.filters)
    prev_bosch = list(bosch_logger.filters)
    yield
    _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS[:] = prev_ts
    lib_logger.filters[:] = prev_lib
    bosch_logger.filters[:] = prev_bosch


class TestErrorCountHelpers:
    def test_count_zero_on_empty_list(self):
        _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS.clear()
        # L118 — early-return branch when there are no timestamps at all.
        assert get_recent_fcm_error_count() == 0

    def test_count_within_window(self):
        now = time.monotonic()
        _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS[:] = [
            now - 200.0,  # in window
            now - 50.0,   # in window
            now - 900.0,  # outside default 300s window
        ]
        assert get_recent_fcm_error_count() == 2

    def test_count_custom_window(self):
        now = time.monotonic()
        _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS[:] = [now - 30.0, now - 90.0]
        # 60s window catches one; default 300s would catch both.
        assert get_recent_fcm_error_count(window_seconds=60.0) == 1

    def test_reset_clears_list(self):
        _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS[:] = [1.0, 2.0, 3.0]
        # L126 — `reset_fcm_error_counter()` clears the shared list.
        reset_fcm_error_counter()
        assert _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS == []


class TestInstallNoiseFilter:
    def test_install_adds_filter_to_both_loggers(self):
        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
        lib_logger.filters[:] = []
        bosch_logger.filters[:] = []
        _install_fcm_noise_filter()
        assert any(isinstance(f, _FCMNoiseFilter) for f in lib_logger.filters)
        assert any(isinstance(f, _FCMNoiseFilter) for f in bosch_logger.filters)

    def test_install_repairs_partial_install(self):
        """If the lib logger has the filter but the bosch logger lost it
        (e.g. after a partial reload), a second call must re-attach to the
        bosch logger without creating a second `_FCMNoiseFilter` instance."""
        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
        f = _FCMNoiseFilter()
        lib_logger.filters[:] = [f]
        bosch_logger.filters[:] = []
        # L153 — branch that addFilter(f) to the bosch logger.
        _install_fcm_noise_filter()
        assert f in bosch_logger.filters
        # Only one shared instance — not a second one.
        bosch_fcm = [g for g in bosch_logger.filters if isinstance(g, _FCMNoiseFilter)]
        assert bosch_fcm == [f]


class TestPatchClassImportError:
    def test_returns_none_when_library_missing(self):
        # Force the inner `from firebase_messaging import …` to fail.
        # ``builtins.__import__`` is wrapped so only firebase_messaging blows up.
        import builtins as _bi
        real = _bi.__import__

        def _fake(name, *a, **kw):
            if name == "firebase_messaging":
                raise ImportError("simulated absence")
            return real(name, *a, **kw)

        # L202-L203 — `_patch_class()` ImportError fallback returns None.
        with patch("builtins.__import__", side_effect=_fake):
            result = fcm._QuietFcmPushClient._patch_class()
        assert result is None
