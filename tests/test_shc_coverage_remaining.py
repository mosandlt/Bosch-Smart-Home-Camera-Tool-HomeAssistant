"""shc.py — coverage remainder tests.

Targets the last uncovered branches in async_cloud_set_privacy_mode:

* Line 470 — Gen2 LOCAL RCP fallback returns ok=False → debug log path.
  Existing test_gen2_rcp_fallback_success only covers ok=True (lines 458-469).
  This test pins the ok=False path so the warning/debug remains exercised
  and the control flow falls through to the SHC/notification stage.

* Lines 499-500 — persistent_notification service.async_call raises an
  exception → it is swallowed by `except Exception: pass`. This guards
  against unhandled exceptions when the notification service is missing
  or misbehaves at runtime; existing tests only cover the happy path.

No file overlap with tests/test_shc_round*.py, test_shc_extended.py,
test_shc_setters.py or test_shc_light_component.py — only the
`_stub_coord` builder pattern is reused.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _stub_coord(
    *, gen2: bool = True, with_token: bool = True, shc_ip: str = "192.0.2.103"
):
    """Stub coordinator mirroring tests/test_shc_round6.py::_stub_coord.

    Kept local so this file has no import dependency on sibling test modules
    (pytest collection does not always make sibling test files importable).
    """
    opts = {}
    if shc_ip:
        opts = {
            "shc_ip": shc_ip,
            "shc_cert_path": "/cert.pem",
            "shc_key_path": "/key.pem",
        }
    coord = SimpleNamespace(
        token="tok-AAA" if with_token else "",
        options=opts,
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        _shc_state_cache={CAM_ID: {"device_id": "shc-dev-1"}},
        _cached_status={},
        _privacy_set_at={},
        _light_set_at={},
        _notif_set_at={},
        _local_creds_cache={},
        _rcp_lan_ip_cache={},
        _pan_cache={},
        _camera_entities={},
        _hw_version={CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"},
        _auth_outage_count=0,
        _shc_devices_raw=[],
        _last_shc_fetch=0,
        _shc_available=True,
        _shc_fail_count=0,
        _shc_last_check=float("-inf"),  # SENTINEL_RULE: never use 0.0 for monotonic
        _SHC_MAX_FAILS=3,
        _SHC_RETRY_INTERVAL=60,
        _lighting_switch_cache={},
        async_update_listeners=lambda: None,
        async_request_refresh=AsyncMock(),
        _ensure_valid_token=AsyncMock(return_value="tok-FRESH"),
    )
    return coord


class TestGen2RcpFallbackFailureDebugLog:
    """Pin line 470: Gen2 LOCAL RCP fallback returns ok=False → debug log."""

    @pytest.mark.asyncio
    async def test_rcp_returns_false_logs_debug_and_falls_through(self):
        """rcp_local_write_privacy → False → debug-log path executes,
        control flow continues to SHC / notification stages.

        Setup:
          - cloud PUT raises ClientError (skip cloud path)
          - Gen2 = True, cam_host present (creds cache)
          - rcp_local_write_privacy returns False
          - shc_ready = False, auth_outage_count = 0
        Expected: returns False, no exception, no cache mutation. The
        failure notification fires unconditionally now (2026-07-07 fix —
        it used to be gated on auth_outage_count > 0, a counter that never
        reflects a one-off write-time failure like this one).
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord(gen2=True)
        coord._local_creds_cache[CAM_ID] = {"host": "192.0.2.149"}

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("cloud down"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_privacy",
                AsyncMock(return_value=False),
            ) as mock_rcp,
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)

        # RCP was called with the cached host, returned False → debug branch hit
        mock_rcp.assert_called_once()
        assert mock_rcp.call_args.args[1] == "192.0.2.149"
        # No write-lock stamped, no cache mutation, no early True return
        assert CAM_ID not in coord._privacy_set_at
        assert "privacy_mode" not in coord._shc_state_cache[CAM_ID]
        assert result is False
        # Notification fires even though auth_outage_count == 0 — see
        # test_privacy_write_failure_notification.py for the dedicated pin.
        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_rcp_false_uses_rcp_lan_ip_cache_when_no_creds(self):
        """When _local_creds_cache is empty, host comes from _rcp_lan_ip_cache.

        Same RCP-failure debug-log branch (line 470) but exercises the
        alternate cam_host lookup at line 455:
            cam_host = creds.get("host") if creds else coordinator._rcp_lan_ip_cache.get(cam_id)
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord(gen2=True)
        # No creds cache entry → falls back to RCP LAN IP cache
        coord._rcp_lan_ip_cache[CAM_ID] = "192.0.2.149"

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("cloud down"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_privacy",
                AsyncMock(return_value=False),
            ) as mock_rcp,
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)

        mock_rcp.assert_called_once()
        assert mock_rcp.call_args.args[1] == "192.0.2.149"
        assert result is False


class TestPersistentNotificationException:
    """Pin lines 499-500: hass.services.async_call raises → exception swallowed."""

    @pytest.mark.asyncio
    async def test_notification_service_raises_swallowed(self):
        """When persistent_notification.create raises (e.g. service unavailable),
        the bare `except Exception: pass` swallows it and the function still
        returns False instead of propagating an unhandled error to the caller.

        Setup that drives execution to lines 484-500:
          - cloud PUT raises ClientError
          - Gen2 = False (skip RCP fallback block)
          - shc_ready = False (skip SHC fallback)
          - auth_outage_count > 0 (triggers notification attempt)
          - services.async_call raises ServiceNotFound-like RuntimeError
        Expected: returns False, no propagated exception.
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord(gen2=False)
        coord._auth_outage_count = 3  # > 0 → notification branch entered
        # Make the persistent_notification call blow up
        coord.hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("ServiceNotFound: persistent_notification.create")
        )

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
        ):
            # MUST NOT raise — `except Exception: pass` swallows it
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)

        # Service call attempted exactly once
        coord.hass.services.async_call.assert_called_once()
        # All fallbacks exhausted → False
        assert result is False

    @pytest.mark.asyncio
    async def test_notification_service_raises_for_disable(self):
        """Same exception-swallow path with enabled=False (privacy OFF)
        confirms the message branch `'ON' if enabled else 'OFF'` is exercised
        for both polarities without leaking the inner exception.
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord(gen2=False)
        coord._auth_outage_count = 1
        coord.hass.services.async_call = AsyncMock(
            side_effect=Exception("notifier crash")
        )

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)

        coord.hass.services.async_call.assert_called_once()
        # Verify the notification body referenced "OFF" (not "ON")
        call_args = coord.hass.services.async_call.call_args
        message = call_args.args[2]["message"]
        assert "OFF" in message
        assert result is False
