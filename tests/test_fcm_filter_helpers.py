"""Coverage tests for the FCM noise-filter helpers in `fcm.py`.

Targets the public helpers used by the FCM supervisor:
- ``get_recent_fcm_creds_staleness_count`` — counts staleness timestamps within a window.
- ``reset_fcm_creds_staleness_counter`` — clears the staleness list after hard-heal.
- ``_install_fcm_noise_filter`` — idempotent install on both loggers.

Shared state lives in ``_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS``;
each test snapshots + restores it to stay order-independent.
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
    get_recent_fcm_creds_staleness_count,
    reset_fcm_creds_staleness_counter,
)


@pytest.fixture(autouse=True)
def _isolate_shared_state():
    """Snapshot + restore staleness timestamps + logger filters between tests."""
    prev_ts = list(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS)
    lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
    bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
    prev_lib = list(lib_logger.filters)
    prev_bosch = list(bosch_logger.filters)
    yield
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = prev_ts
    lib_logger.filters[:] = prev_lib
    bosch_logger.filters[:] = prev_bosch


class TestCredsStatenessHelpers:
    def test_count_zero_on_empty_list(self):
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        assert get_recent_fcm_creds_staleness_count() == 0

    def test_count_within_window(self):
        now = time.monotonic()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = [
            now - 200.0,   # in default 600s window
            now - 50.0,    # in window
            now - 700.0,   # outside window
        ]
        assert get_recent_fcm_creds_staleness_count() == 2

    def test_count_custom_window(self):
        now = time.monotonic()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = [now - 30.0, now - 90.0]
        # 60s window catches one; default 600s would catch both.
        assert get_recent_fcm_creds_staleness_count(window_seconds=60.0) == 1

    def test_reset_clears_list(self):
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = [1.0, 2.0, 3.0]
        reset_fcm_creds_staleness_counter()
        assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []


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
        _install_fcm_noise_filter()
        assert f in bosch_logger.filters
        bosch_fcm = [g for g in bosch_logger.filters if isinstance(g, _FCMNoiseFilter)]
        assert bosch_fcm == [f]


class TestFailureMarkers:
    """Creds-staleness markers must record to _SHARED_STALENESS_TIMESTAMPS;
    connectivity-only markers must dedupe but NOT record there."""

    def _make_record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="firebase_messaging.fcmregister",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_phone_registration_error_records_staleness_timestamp(self):
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        passed = f.filter(
            self._make_record(
                "GCM register request attempt 1 out of 2 has failed with Error=PHONE_REGISTRATION_ERROR"
            )
        )
        assert passed is True
        assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) == 1

    def test_unable_to_complete_gcm_auth_records_staleness_timestamp(self):
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        f.filter(
            self._make_record(
                "Unable to complete gcm auth request after 2 tries, last error was Error=PHONE_REGISTRATION_ERROR"
            )
        )
        assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) == 1

    def test_unable_to_establish_subscription_records_staleness_timestamp(self):
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        f.filter(
            self._make_record(
                "FCM registration failed: Unable to establish subscription with Google Cloud Messaging."
            )
        )
        assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) == 1

    def test_connectivity_error_does_not_record_staleness_timestamp(self):
        """'Unexpected exception during read' is deduplicated but NOT a staleness marker."""
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        passed = f.filter(
            self._make_record("Unexpected exception during read")
        )
        assert passed is True  # first occurrence passes through
        assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []

    def test_unrelated_message_passes_through_untouched(self):
        """Non-failure messages pass through without recording any timestamp."""
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        passed = f.filter(self._make_record("FCM push listener started"))
        assert passed is True
        assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []


class TestPatchClassImportError:
    def test_returns_none_when_library_missing(self):
        import builtins as _bi

        real = _bi.__import__

        def _fake(name, *a, **kw):
            if name == "firebase_messaging":
                raise ImportError("simulated absence")
            return real(name, *a, **kw)

        with patch("builtins.__import__", side_effect=_fake):
            result = fcm._QuietFcmPushClient._patch_class()
        assert result is None
