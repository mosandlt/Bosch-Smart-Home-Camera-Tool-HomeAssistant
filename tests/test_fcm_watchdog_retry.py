"""Tests for the FCM supervisor watchdog behavior in _async_update_data.

With the supervisor model (v14.3.0+), the watchdog no longer manages cool-downs
or schedules self-heals directly. Instead it just ensures the supervisor task is
alive, spawning a new one when it is None or done(). The supervisor handles all
retry/backoff/soft/hard-heal logic internally.

These tests replace the old v12.8.3 retry tests which tested the now-removed
self-heal ladder. The invariants being pinned:
  (1) FCM not running + supervisor=None → watchdog spawns supervisor
  (2) FCM running + supervisor already alive → watchdog does NOT spawn again
  (3) FCM disabled → watchdog never spawns supervisor
  (4) Healthy listener + supervisor running → no extra spawn
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.test_init_sprint_ka import (
    _PATCH_SESSION,
    _make_coord,
    _make_resp,
    _make_session,
)

_PATCH_CLOUD_SESSION = (
    "custom_components.bosch_shc_camera.async_get_bosch_cloud_session"
)


class TestFcmWatchdogSupervisorSpawn:
    """Watchdog in _async_update_data must start/not-start the supervisor correctly."""

    @pytest.mark.asyncio
    async def test_not_running_supervisor_none_spawns_supervisor(self):
        """FCM enabled + supervisor=None → watchdog spawns supervisor.

        Equivalent to the old v12.8.3 "trigger (c)" test: FCM not running after
        a failed previous start (e.g. PHONE_REGISTRATION_ERROR). In the supervisor
        model the watchdog doesn't manage cooldowns — it just ensures the task exists.
        The supervisor handles backoff and retry internally.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _fcm_running=False,
            _fcm_healthy=False,
            _fcm_client=None,
            _fcm_supervisor_task=None,
            options={"enable_fcm_push": True},
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
                "custom_components.bosch_shc_camera._fcm_async_ensure_supervisor",
                new_callable=AsyncMock,
            ) as mock_ensure,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert mock_ensure.called, (
            "Watchdog must spawn supervisor when FCM is enabled but supervisor is None "
            "(previous start failed — supervisor will retry with backoff)"
        )

    @pytest.mark.asyncio
    async def test_supervisor_already_running_no_respawn(self):
        """Supervisor already alive (done()=False) → watchdog must NOT spawn again.

        Replaces the old "within cooldown no retry" test: if the supervisor is
        already running (doing its backoff sleep between retries), the watchdog
        must not create a duplicate.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        running_task = MagicMock(spec=asyncio.Task)
        running_task.done = MagicMock(return_value=False)

        coord = _make_coord(
            _fcm_running=False,
            _fcm_healthy=False,
            _fcm_client=None,
            _fcm_supervisor_task=running_task,
            options={"enable_fcm_push": True},
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
                "custom_components.bosch_shc_camera._fcm_async_ensure_supervisor",
                new_callable=AsyncMock,
            ) as mock_ensure,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert not mock_ensure.called, (
            "Watchdog must NOT spawn a second supervisor while one is already running "
            "— the running supervisor handles its own retry backoff"
        )

    @pytest.mark.asyncio
    async def test_fcm_disabled_no_supervisor_spawn(self):
        """enable_fcm_push=False → watchdog must NEVER spawn supervisor.

        The user explicitly disabled FCM — the watchdog must not override that.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _fcm_running=False,
            _fcm_healthy=False,
            _fcm_client=None,
            _fcm_supervisor_task=None,
            options={"enable_fcm_push": False},
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
                "custom_components.bosch_shc_camera._fcm_async_ensure_supervisor",
                new_callable=AsyncMock,
            ) as mock_ensure,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert not mock_ensure.called, (
            "Watchdog must NOT spawn supervisor when enable_fcm_push=False — "
            "the user opted out of FCM"
        )

    @pytest.mark.asyncio
    async def test_healthy_listener_with_running_supervisor_no_extra_spawn(self):
        """Happy path: FCM running + healthy + supervisor running → nothing extra spawned.

        Sanity check that a healthy FCM state with a live supervisor doesn't
        trigger a redundant supervisor spawn.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        running_task = MagicMock(spec=asyncio.Task)
        running_task.done = MagicMock(return_value=False)

        fcm_client = MagicMock()
        fcm_client.is_started = MagicMock(return_value=True)

        coord = _make_coord(
            _fcm_running=True,
            _fcm_healthy=True,
            _fcm_client=fcm_client,
            _fcm_supervisor_task=running_task,
            options={"enable_fcm_push": True},
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
                "custom_components.bosch_shc_camera._fcm_async_ensure_supervisor",
                new_callable=AsyncMock,
            ) as mock_ensure,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert not mock_ensure.called, (
            "Healthy listener with running supervisor must not trigger extra spawn"
        )
