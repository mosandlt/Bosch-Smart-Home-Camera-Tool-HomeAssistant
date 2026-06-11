"""Remaining-line coverage tests for shc.py and select.py.

Targets:
  shc.py   lines 201-202  async_update_shc_states: no device matches cam title → debug + continue
  shc.py   lines 398-399  async_cloud_set_privacy_mode: 401 + _ensure_valid_token raises → pass
  shc.py   lines 659-660  async_cloud_set_light_component (gen2 wallwasher): aiohttp error on
                           /lighting/switch PUT → warning log
  select.py line 278      BoschFcmPushModeSelect.available: super().available is False → return False
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── shared helpers ────────────────────────────────────────────────────────────


def _mock_resp(status: int, json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value="")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _stub_coord(*, gen2: bool = True, shc_ip: str = "192.0.2.103"):
    opts = {"shc_ip": shc_ip, "shc_cert_path": "/cert.pem", "shc_key_path": "/key.pem"}
    coord = SimpleNamespace(
        token="tok-AAA",
        options=opts,
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        _shc_state_cache={},
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
        _last_shc_fetch=float("-inf"),
        _shc_available=True,
        _shc_fail_count=0,
        _shc_last_check=float("-inf"),
        _SHC_MAX_FAILS=3,
        _SHC_RETRY_INTERVAL=60,
        _lighting_switch_cache={},
        async_update_listeners=lambda: None,
        async_request_refresh=AsyncMock(),
        _ensure_valid_token=AsyncMock(return_value="tok-FRESH"),
        _fcm_last_push=float("-inf"),
    )
    return coord


# ── shc.py lines 201-202 ─────────────────────────────────────────────────────


class TestAsyncUpdateShcStatesNoDeviceMatch:
    """Lines 201-202: when no SHC device name matches the cam title → debug + continue."""

    @pytest.mark.asyncio
    async def test_no_device_match_logs_debug_and_continues(self):
        """Camera title 'Terrasse' does not match SHC device name 'Unknown' → lines 201-202."""
        from custom_components.bosch_shc_camera.shc import async_update_shc_states

        coord = _stub_coord()
        # Pre-populate device list so the fetch branch is skipped
        coord._shc_devices_raw = [{"id": "dev-99", "name": "Unknown Device"}]
        coord._last_shc_fetch = float("inf")  # force "recent enough" to skip re-fetch

        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},  # name mismatch → no device_id found
            }
        }

        # async_shc_request should NOT be called (last_shc_fetch is current)
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value=[]),
        ) as mock_req:
            await async_update_shc_states(coord, data)
            # The cam_id loop body should hit `continue` — no state-cache entry created
            assert CAM_ID not in coord._shc_state_cache
            # async_shc_request was not called because last_shc_fetch is "future"
            mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_device_match_multiple_cameras_continues_to_next(self):
        """Two cameras: only one matches. The non-matching one is skipped (lines 201-202)."""
        from custom_components.bosch_shc_camera.shc import async_update_shc_states

        CAM2_ID = "22222222-OTHER-CAM"
        coord = _stub_coord()
        coord._shc_devices_raw = [
            {"id": "dev-1", "name": "terrasse"}
        ]  # only matches first cam
        coord._last_shc_fetch = float("inf")

        # Pre-seed cache for CAM2 so we can confirm it's NOT updated
        coord._shc_state_cache = {}

        data = {
            CAM_ID: {"info": {"title": "Terrasse"}},  # matches → processed
            CAM2_ID: {"info": {"title": "Innenbereich"}},  # no match → skipped
        }

        # Patch the downstream SHC requests (CameraLight, PrivacyMode) to short-circuit
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value=None),
        ):
            await async_update_shc_states(coord, data)

        # CAM_ID was matched → cache entry created; CAM2_ID was not matched → absent
        assert CAM_ID in coord._shc_state_cache
        assert CAM2_ID not in coord._shc_state_cache


# ── shc.py lines 398-399 ─────────────────────────────────────────────────────


class TestCloudSetPrivacyMode401TokenRefreshFails:
    """Lines 398-399: 401 response + _ensure_valid_token raises → exception swallowed."""

    @pytest.mark.asyncio
    async def test_401_token_refresh_raises_falls_through_to_shc(self):
        """_ensure_valid_token raises RuntimeError on 401 → pass (lines 398-399), falls to SHC."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord()
        coord._ensure_valid_token = AsyncMock(
            side_effect=RuntimeError("refresh failed")
        )
        # SHC is configured but not ready (shc_available=False) so we get a clean return
        coord._shc_available = False

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_resp(401))

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_configured",
                return_value=True,
            ),
        ):
            # Must not raise — exception is swallowed at line 398-399
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)

        # Token refresh failed → no success, result is False
        assert result is False
        coord._ensure_valid_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_401_token_refresh_raises_exception_is_swallowed(self):
        """Verify the except block at lines 398-399 catches ANY exception type."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord()
        coord._ensure_valid_token = AsyncMock(
            side_effect=aiohttp.ClientError("network gone")
        )

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_resp(401))

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_configured",
                return_value=True,
            ),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)

        assert result is False


# ── shc.py lines 659-660 ─────────────────────────────────────────────────────


class TestCloudSetLightComponentGen2WallwasherNetworkError:
    """Lines 659-660: gen2 wallwasher /lighting/switch PUT raises aiohttp error → warning log."""

    @pytest.mark.asyncio
    async def test_lighting_switch_put_raises_client_error_logs_warning(self):
        """aiohttp.ClientError on /lighting/switch → warning + execution continues (lines 659-660)."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = _stub_coord(gen2=True)

        # Session raises ClientError on PUT /lighting/switch (step 1 of wallwasher logic)
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        failing_ctx.__aexit__ = AsyncMock(return_value=None)

        # Step 2 (topdown) uses a working response
        ok_ctx = _mock_resp(200, json_data={"enabled": True})

        session = MagicMock()
        # First call: /lighting/switch (raises), second call: /topdown (200)
        session.put = MagicMock(side_effect=[failing_ctx, ok_ctx])

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            # Should not raise — warning is logged and execution continues to step 2
            result = await async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", True
            )

        # Step 2 succeeded → ok = True
        assert result is True

    @pytest.mark.asyncio
    async def test_lighting_switch_put_raises_timeout_logs_warning(self):
        """asyncio.TimeoutError on /lighting/switch → warning + step 2 still runs (lines 659-660)."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = _stub_coord(gen2=True)

        # First PUT raises TimeoutError (via asyncio.timeout context manager)
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(side_effect=TimeoutError())
        failing_ctx.__aexit__ = AsyncMock(return_value=None)

        ok_ctx = _mock_resp(200, json_data={"enabled": False})

        session = MagicMock()
        session.put = MagicMock(side_effect=[failing_ctx, ok_ctx])

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", False
            )

        assert result is True


# ── select.py line 278 ───────────────────────────────────────────────────────


class TestFcmPushModeSelectAvailableSuperFalse:
    """Line 278: BoschFcmPushModeSelect.available returns False when super().available is False."""

    def _make(self, enable_fcm_push: bool = True):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                    },
                }
            },
            options={"enable_fcm_push": enable_fcm_push},
            last_update_success=False,  # makes CoordinatorEntity.available False
            async_stop_fcm_push=AsyncMock(),
            async_start_fcm_push=AsyncMock(),
            async_update_listeners=lambda: None,
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        sel = BoschFcmPushModeSelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_available_false_when_super_returns_false(self):
        """Line 278: super().available is False → available returns False immediately."""
        sel = self._make(enable_fcm_push=True)
        # CoordinatorEntity.available checks last_update_success — set to False
        # so super().available → False → our guard at line 277-278 fires
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
            new_callable=lambda: property(lambda s: False),
        ):
            assert sel.available is False

    def test_available_false_when_super_true_but_fcm_disabled(self):
        """Line 279: super().available True but enable_fcm_push False → still False."""
        sel = self._make(enable_fcm_push=False)
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
            new_callable=lambda: property(lambda s: True),
        ):
            assert sel.available is False

    def test_available_true_when_super_true_and_fcm_enabled(self):
        """Positive path: super() True + enable_fcm_push True → available True."""
        sel = self._make(enable_fcm_push=True)
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
            new_callable=lambda: property(lambda s: True),
        ):
            assert sel.available is True
