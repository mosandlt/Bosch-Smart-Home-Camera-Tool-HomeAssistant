"""Regression tests for the FCM watchdog retry path (v12.8.3).

User incident 2026-05-21: FCM listener silent-died, self-heal fired once, the
fresh `checkin_or_register()` failed with PHONE_REGISTRATION_ERROR (Google
rate-limited the user's public IP for a few hours). After the failed self-heal
`_fcm_running` stayed False and the watchdog blocked every subsequent retry
because the trigger required `_fcm_running=True`. FCM stayed dead until the
user reloaded the integration manually — even though Google would have
accepted the registration ~30 min later.

These tests pin the third self-heal trigger added in v12.8.3:
  (c) `enable_fcm_push=True` + `_fcm_running=False` + cool-down expired
      → re-attempt self-heal (which restarts FCM from scratch).
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Reuse helpers from the existing sprint_ka test module — they build the same
# coordinator + session stubs used by the original watchdog tests, so the new
# tests behave identically except for the configured pre-conditions.
from tests.test_init_sprint_ka import (  # type: ignore[import-not-found]
    _PATCH_SESSION,
    _make_coord,
    _make_resp,
    _make_session,
)

_PATCH_CLOUD_SESSION = "custom_components.bosch_shc_camera.async_get_bosch_cloud_session"


class TestFcmWatchdogRetryAfterFailedSelfHeal:
    """v12.8.3: third self-heal trigger — retry after a failed previous heal."""

    @pytest.mark.asyncio
    async def test_not_running_with_cooldown_expired_triggers_retry(self):
        """`_fcm_running=False` + `enable_fcm_push=True` + cool-down OK → self-heal fires.

        Default `_fcm_last_self_heal = float('-inf')` means cool-down is always
        satisfied after the first tick following a failed heal.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _fcm_running=False,
            _fcm_healthy=False,
            _fcm_client=None,
            options={"enable_fcm_push": True},
            _fcm_last_self_heal=float("-inf"),
        )
        coord._first_tick_done = True

        session = _make_session(
            {
                "v11/video_inputs": _make_resp(200, []),
                "feature_flags": _make_resp(200, {}),
                "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            }
        )

        with (
            patch(_PATCH_SESSION, return_value=session),
            patch(_PATCH_CLOUD_SESSION, new=AsyncMock(return_value=session)),
            patch(
                "custom_components.bosch_shc_camera.fcm.async_self_heal_fcm_push"
            ) as mock_heal,
        ):
            mock_heal.return_value = None
            await BoschCameraCoordinator._async_update_data(coord)

        assert mock_heal.called, (
            "Watchdog must retry self-heal when FCM is enabled but not running "
            "(previous self-heal failed, e.g. PHONE_REGISTRATION_ERROR)"
        )
        assert coord._fcm_last_self_heal == pytest.approx(time.monotonic(), abs=2.0), (
            "`_fcm_last_self_heal` must be updated to start a new 30 min cool-down"
        )

    @pytest.mark.asyncio
    async def test_not_running_within_cooldown_no_retry(self):
        """`_fcm_running=False` + cool-down NOT expired → no self-heal.

        Pins the cool-down behavior for the retry path so a busy coordinator
        loop cannot hammer Google's GCM endpoint while the rate-limit holds.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _fcm_running=False,
            _fcm_healthy=False,
            _fcm_client=None,
            options={"enable_fcm_push": True},
            _fcm_last_self_heal=time.monotonic() - 300.0,  # 5 min ago — still cool-down
        )
        coord._first_tick_done = True

        session = _make_session(
            {
                "v11/video_inputs": _make_resp(200, []),
                "feature_flags": _make_resp(200, {}),
                "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            }
        )

        with (
            patch(_PATCH_SESSION, return_value=session),
            patch(_PATCH_CLOUD_SESSION, new=AsyncMock(return_value=session)),
            patch(
                "custom_components.bosch_shc_camera.fcm.async_self_heal_fcm_push"
            ) as mock_heal,
        ):
            mock_heal.return_value = None
            await BoschCameraCoordinator._async_update_data(coord)

        assert not mock_heal.called, (
            "Cool-down must suppress the retry path so we don't hammer Google "
            "while the GCM rate-limit holds"
        )

    @pytest.mark.asyncio
    async def test_not_running_with_fcm_disabled_no_retry(self):
        """`_fcm_running=False` + `enable_fcm_push=False` → no self-heal.

        The user explicitly disabled FCM — the retry path must not override
        that preference, otherwise toggling FCM off in Options would still
        trigger background registration attempts.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _fcm_running=False,
            _fcm_healthy=False,
            _fcm_client=None,
            options={"enable_fcm_push": False},
            _fcm_last_self_heal=float("-inf"),
        )
        coord._first_tick_done = True

        session = _make_session(
            {
                "v11/video_inputs": _make_resp(200, []),
                "feature_flags": _make_resp(200, {}),
                "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            }
        )

        with (
            patch(_PATCH_SESSION, return_value=session),
            patch(_PATCH_CLOUD_SESSION, new=AsyncMock(return_value=session)),
            patch(
                "custom_components.bosch_shc_camera.fcm.async_self_heal_fcm_push"
            ) as mock_heal,
        ):
            mock_heal.return_value = None
            await BoschCameraCoordinator._async_update_data(coord)

        assert not mock_heal.called, (
            "Retry path must respect `enable_fcm_push=False` — the user opted "
            "out of FCM, so the watchdog must not try to bring it back"
        )

    @pytest.mark.asyncio
    async def test_running_and_healthy_does_not_fall_into_retry_branch(self):
        """When FCM is up and healthy, no path (including the new retry) fires.

        Sanity check that adding branch (c) did not regress the happy path —
        a healthy listener must NOT trigger self-heal.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        fcm_client = MagicMock()
        fcm_client.is_started = MagicMock(return_value=True)  # healthy

        coord = _make_coord(
            _fcm_running=True,
            _fcm_healthy=True,
            _fcm_client=fcm_client,
            options={"enable_fcm_push": True},
            _fcm_last_self_heal=float("-inf"),
        )
        coord._first_tick_done = True

        session = _make_session(
            {
                "v11/video_inputs": _make_resp(200, []),
                "feature_flags": _make_resp(200, {}),
                "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            }
        )

        with (
            patch(_PATCH_SESSION, return_value=session),
            patch(_PATCH_CLOUD_SESSION, new=AsyncMock(return_value=session)),
            patch(
                "custom_components.bosch_shc_camera.fcm.async_self_heal_fcm_push"
            ) as mock_heal,
            patch(
                "custom_components.bosch_shc_camera.fcm.get_recent_fcm_error_count",
                return_value=0,
            ),
        ):
            mock_heal.return_value = None
            await BoschCameraCoordinator._async_update_data(coord)

        assert not mock_heal.called, "Healthy listener must not trigger self-heal"
