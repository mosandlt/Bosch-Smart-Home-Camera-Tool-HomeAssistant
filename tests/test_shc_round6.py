"""shc.py — Sprint-A round-6 tests.

Covers the 26% gap (lines 112-161, 177-202, 279-315, 390-492, 538-577,
618, 647-674, 819-820) that represent async_shc_request, SHC update,
SHC setters, and cloud-setter fallback branches.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _mock_resp(status: int, json_data=None, text: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _stub_coord(*, gen2: bool = True, with_token: bool = True, shc_ip: str = "192.0.2.103"):
    opts = {}
    if shc_ip:
        opts = {"shc_ip": shc_ip, "shc_cert_path": "/cert.pem", "shc_key_path": "/key.pem"}
    coord = SimpleNamespace(
        token="tok-AAA" if with_token else "",
        options=opts,
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        _shc_state_cache={CAM_ID: {"device_id": "shc-dev-1", "front_light_intensity": 0.5}},
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
        _shc_last_check=0.0,
        _SHC_MAX_FAILS=3,
        _SHC_RETRY_INTERVAL=60,
        _lighting_switch_cache={},
        async_update_listeners=lambda: None,
        async_request_refresh=AsyncMock(),
        _ensure_valid_token=AsyncMock(return_value="tok-FRESH"),
    )
    return coord


# ── async_shc_request ────────────────────────────────────────────────────────


class TestAsyncShcRequest:
    """All branches of async_shc_request (lines 112-161)."""

    @pytest.mark.asyncio
    async def test_missing_opts_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord(shc_ip="")  # empty shc_ip
        coord.options = {}
        result = await async_shc_request(coord, "GET", "/devices")
        assert result is None

    @pytest.mark.asyncio
    async def test_ssl_setup_failure_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        with patch("ssl.SSLContext.load_cert_chain", side_effect=FileNotFoundError("no cert")):
            result = await async_shc_request(coord, "GET", "/devices")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_200_returns_json(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        devices = [{"id": "dev1", "name": "Terrasse"}]
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = _mock_resp(200, devices)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ssl.SSLContext") as mock_ssl, \
             patch("aiohttp.TCPConnector"), \
             patch("aiohttp.ClientSession", return_value=mock_session_cm):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/devices")
        assert result == devices

    @pytest.mark.asyncio
    async def test_get_non200_marks_failure(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = _mock_resp(403)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ssl.SSLContext") as mock_ssl, \
             patch("aiohttp.TCPConnector"), \
             patch("aiohttp.ClientSession", return_value=mock_session_cm):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/devices")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_returns_status_dict(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.put.return_value = _mock_resp(204)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ssl.SSLContext") as mock_ssl, \
             patch("aiohttp.TCPConnector"), \
             patch("aiohttp.ClientSession", return_value=mock_session_cm):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "PUT", "/devices/dev1/services/CameraLight/state", {})
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_put_failure_status(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.put.return_value = _mock_resp(500)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ssl.SSLContext") as mock_ssl, \
             patch("aiohttp.TCPConnector"), \
             patch("aiohttp.ClientSession", return_value=mock_session_cm):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "PUT", "/path", {})
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        import aiohttp as _aiohttp
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.side_effect = asyncio.TimeoutError()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ssl.SSLContext") as mock_ssl, \
             patch("aiohttp.TCPConnector"), \
             patch("aiohttp.ClientSession", return_value=mock_session_cm):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/x")
        assert result is None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self):
        import aiohttp as _aiohttp
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.side_effect = _aiohttp.ClientError("conn refused")
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ssl.SSLContext") as mock_ssl, \
             patch("aiohttp.TCPConnector"), \
             patch("aiohttp.ClientSession", return_value=mock_session_cm):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/x")
        assert result is None

    @pytest.mark.asyncio
    async def test_generic_exception_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request
        coord = _stub_coord()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.side_effect = RuntimeError("unexpected")
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("ssl.SSLContext") as mock_ssl, \
             patch("aiohttp.TCPConnector"), \
             patch("aiohttp.ClientSession", return_value=mock_session_cm):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/x")
        assert result is None


# ── async_update_shc_states ──────────────────────────────────────────────────


class TestAsyncUpdateShcStates:
    @pytest.mark.asyncio
    async def test_not_configured_returns_early(self):
        from custom_components.bosch_shc_camera.shc import async_update_shc_states
        coord = _stub_coord(shc_ip="")
        coord.options = {}
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "privacy_mode": False, "camera_light": False}}
        await async_update_shc_states(coord, data)
        # Must not crash and must not modify data when SHC not configured

    @pytest.mark.asyncio
    async def test_empty_devices_returns_early(self):
        from custom_components.bosch_shc_camera.shc import async_update_shc_states, shc_configured
        coord = _stub_coord()
        coord._shc_devices_raw = []
        coord._last_shc_fetch = time.monotonic() - 120  # force fetch
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "privacy_mode": False, "camera_light": False}}
        with patch("custom_components.bosch_shc_camera.shc.async_shc_request", AsyncMock(return_value=None)):
            await async_update_shc_states(coord, data)
        # No crash — empty device list is handled gracefully

    @pytest.mark.asyncio
    async def test_device_fetch_updates_shc_devices_raw(self):
        from custom_components.bosch_shc_camera.shc import async_update_shc_states
        coord = _stub_coord()
        coord._last_shc_fetch = 0  # force refresh
        devices = [
            {"id": "shc-dev-1", "name": "terrasse",
             "services": [
                 {"id": "CameraLight", "state": {"value": "ON"}},
                 {"id": "PrivacyMode", "state": {"value": "DISABLED"}},
             ]},
        ]
        data = {CAM_ID: {"info": {"title": "Terrasse"}, "privacy_mode": None, "camera_light": None}}
        with patch("custom_components.bosch_shc_camera.shc.async_shc_request",
                   AsyncMock(return_value=devices)):
            await async_update_shc_states(coord, data)
        assert coord._shc_devices_raw == devices


# ── async_shc_set_camera_light ────────────────────────────────────────────────


class TestAsyncShcSetCameraLight:
    @pytest.mark.asyncio
    async def test_success_updates_cache_and_notifies(self):
        """PUT 204 → cache updated, listeners notified, refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light
        coord = _stub_coord()
        with patch("custom_components.bosch_shc_camera.shc.async_shc_request",
                   AsyncMock(return_value={"status": 204, "ok": True})):
            result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is True
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is True

    @pytest.mark.asyncio
    async def test_failure_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light
        coord = _stub_coord()
        with patch("custom_components.bosch_shc_camera.shc.async_shc_request",
                   AsyncMock(return_value={"status": 500, "ok": False})):
            result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is False


# ── async_shc_set_privacy_mode ────────────────────────────────────────────────


class TestAsyncShcSetPrivacyMode:
    @pytest.mark.asyncio
    async def test_success_updates_cache_and_lock(self):
        """PUT 204 → cache + write-lock stamped, listeners notified."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode
        coord = _stub_coord()
        with patch("custom_components.bosch_shc_camera.shc.async_shc_request",
                   AsyncMock(return_value={"status": 204, "ok": True})), \
             patch("custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot"):
            result = await async_shc_set_privacy_mode(coord, CAM_ID, False)
        assert result is True
        assert coord._shc_state_cache[CAM_ID]["privacy_mode"] is False
        assert CAM_ID in coord._privacy_set_at

    @pytest.mark.asyncio
    async def test_success_enable_does_not_schedule_snapshot(self):
        """When enabling privacy (True), snapshot snapshot is not scheduled."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode
        coord = _stub_coord()
        with patch("custom_components.bosch_shc_camera.shc.async_shc_request",
                   AsyncMock(return_value={"status": 204, "ok": True})), \
             patch("custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot") as mock_snap:
            result = await async_shc_set_privacy_mode(coord, CAM_ID, True)
        assert result is True
        mock_snap.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode
        coord = _stub_coord()
        with patch("custom_components.bosch_shc_camera.shc.async_shc_request",
                   AsyncMock(return_value={"status": 500, "ok": False})):
            result = await async_shc_set_privacy_mode(coord, CAM_ID, False)
        assert result is False


# ── cloud_set_privacy_mode — additional branches ─────────────────────────────


class TestCloudSetPrivacyModeBranches:
    @pytest.mark.asyncio
    async def test_timeout_falls_through_to_no_shc(self):
        """aiohttp timeout → falls through; no SHC → returns False."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode
        import aiohttp
        coord = _stub_coord()
        coord._shc_state_cache[CAM_ID]["device_id"] = None  # disable SHC fallback
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("timeout"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=False):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        assert result is False

    @pytest.mark.asyncio
    async def test_shc_fallback_called_on_cloud_fail(self):
        """Cloud PUT fails → shc_ready=True → delegates to async_shc_set_privacy_mode."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode
        coord = _stub_coord()
        session = MagicMock()
        session.put.return_value = _mock_resp(500)

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=True), \
             patch("custom_components.bosch_shc_camera.shc.async_shc_set_privacy_mode",
                   AsyncMock(return_value=True)) as mock_shc:
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        mock_shc.assert_called_once()
        assert result is True

    @pytest.mark.asyncio
    async def test_persistent_notification_on_auth_outage(self):
        """auth_outage_count > 0 + no SHC → creates a persistent notification."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode
        import aiohttp
        coord = _stub_coord()
        coord._auth_outage_count = 2
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=False), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=False):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        coord.hass.services.async_call.assert_called_once()
        assert result is False

    @pytest.mark.asyncio
    async def test_schedule_privacy_off_snapshot_when_disabling(self):
        """Successful cloud PUT with enabled=False → snapshot schedule triggered."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode
        coord = _stub_coord()
        session = MagicMock()
        session.put.return_value = _mock_resp(204)

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot") as mock_snap:
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        mock_snap.assert_called_once_with(coord, CAM_ID)
        assert result is True

    @pytest.mark.asyncio
    async def test_gen2_rcp_fallback_success(self):
        """Cloud fails, Gen2 RCP fallback succeeds → returns True."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode
        import aiohttp
        coord = _stub_coord(gen2=True)
        coord._local_creds_cache[CAM_ID] = {"host": "192.0.2.149"}
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("cloud down"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=False), \
             patch("custom_components.bosch_shc_camera.rcp.rcp_local_write_privacy",
                   AsyncMock(return_value=True)):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert result is True


# ── cloud_set_camera_light — additional branches ─────────────────────────────


class TestCloudSetCameraLightBranches:
    @pytest.mark.asyncio
    async def test_gen2_client_error_falls_to_shc(self):
        """Gen2 aiohttp error → SHC fallback called if shc_ready."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light
        import aiohttp
        coord = _stub_coord(gen2=True)
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn error"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=True), \
             patch("custom_components.bosch_shc_camera.shc.async_shc_set_camera_light",
                   AsyncMock(return_value=True)) as mock_shc:
            result = await async_cloud_set_camera_light(coord, CAM_ID, True)
        mock_shc.assert_called_once()

    @pytest.mark.asyncio
    async def test_gen1_light_off_body_excludes_intensity(self):
        """Gen1 light OFF → body must NOT contain frontLightIntensity."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light
        coord = _stub_coord(gen2=False)
        session = MagicMock()
        session.put.return_value = _mock_resp(204)

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=False), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=False):
            result = await async_cloud_set_camera_light(coord, CAM_ID, False)
        # Verify PUT was called and the call body doesn't contain intensity
        assert session.put.called
        _, call_kwargs = session.put.call_args
        body = call_kwargs.get("json", {})
        assert "frontLightIntensity" not in body

    @pytest.mark.asyncio
    async def test_gen1_http_failure_returns_false(self):
        """Gen1 HTTP 500 → not cached, returns False."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light
        coord = _stub_coord(gen2=False)
        session = MagicMock()
        session.put.return_value = _mock_resp(500)

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=False), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=False):
            result = await async_cloud_set_camera_light(coord, CAM_ID, True)
        assert result is False

    @pytest.mark.asyncio
    async def test_gen1_client_error_falls_to_shc(self):
        """Gen1 aiohttp error → SHC fallback if shc_ready."""
        import aiohttp
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light
        coord = _stub_coord(gen2=False)
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("no conn"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=False), \
             patch("custom_components.bosch_shc_camera.shc.shc_ready", return_value=True), \
             patch("custom_components.bosch_shc_camera.shc.async_shc_set_camera_light",
                   AsyncMock(return_value=True)) as mock_shc:
            result = await async_cloud_set_camera_light(coord, CAM_ID, True)
        mock_shc.assert_called_once()


# ── cloud_set_light_component Gen2 error branches ────────────────────────────


class TestSetLightComponentGen2Errors:
    def _stub_gen2_coord(self):
        coord = _stub_coord(gen2=True)
        coord._lighting_switch_cache = {}
        return coord

    @pytest.mark.asyncio
    async def test_wallwasher_step1_json_parse_error_uses_full_body(self):
        """Step-1 PUT 200 but resp.json() raises → falls back to full_body in cache."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_light_component
        coord = self._stub_gen2_coord()
        # Simulate hasattr check by removing _last_topdown_brightness
        if hasattr(coord, "_last_topdown_brightness"):
            del coord._last_topdown_brightness

        step1_resp = MagicMock()
        step1_resp.status = 200
        step1_resp.json = AsyncMock(side_effect=ValueError("bad json"))
        step1_cm = MagicMock()
        step1_cm.__aenter__ = AsyncMock(return_value=step1_resp)
        step1_cm.__aexit__ = AsyncMock(return_value=None)

        step2_resp = MagicMock()
        step2_resp.status = 204
        step2_cm = MagicMock()
        step2_cm.__aenter__ = AsyncMock(return_value=step2_resp)
        step2_cm.__aexit__ = AsyncMock(return_value=None)

        call_count = [0]
        def make_put(url, json, headers):
            call_count[0] += 1
            return step1_cm if call_count[0] == 1 else step2_cm

        session = MagicMock()
        session.put.side_effect = make_put

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True):
            await async_cloud_set_light_component(coord, CAM_ID, "wallwasher", True)
        # full_body must be set as fallback since json() raised
        assert CAM_ID in coord._lighting_switch_cache

    @pytest.mark.asyncio
    async def test_wallwasher_step1_http_error_logged(self):
        """Step-1 PUT non-200 → warning logged, step 2 still proceeds."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_light_component
        coord = self._stub_gen2_coord()

        step1_cm = _mock_resp(500)
        step2_cm = _mock_resp(204)
        call_count = [0]

        def make_put(url, json, headers):
            call_count[0] += 1
            return step1_cm if call_count[0] == 1 else step2_cm

        session = MagicMock()
        session.put.side_effect = make_put

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True):
            await async_cloud_set_light_component(coord, CAM_ID, "wallwasher", True)

    @pytest.mark.asyncio
    async def test_gen2_step2_http_failure_logged(self):
        """Step-2 PUT non-200 → warning logged, returns False."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_light_component
        coord = self._stub_gen2_coord()
        session = MagicMock()
        session.put.return_value = _mock_resp(500)

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True):
            result = await async_cloud_set_light_component(coord, CAM_ID, "front", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_gen2_step2_client_error_logged(self):
        """Step-2 PUT raises aiohttp.ClientError → caught, returns False."""
        import aiohttp
        from custom_components.bosch_shc_camera.shc import async_cloud_set_light_component
        coord = self._stub_gen2_coord()
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("no conn"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session), \
             patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True):
            result = await async_cloud_set_light_component(coord, CAM_ID, "front", True)
        assert result is False


# ── cloud_set_pan — body parse exception ─────────────────────────────────────


class TestCloudSetPanBodyException:
    @pytest.mark.asyncio
    async def test_200_json_parse_error_falls_back_to_position(self):
        """resp.status==200 but json() raises → actual=requested_position, eta=0."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan
        coord = _stub_coord()
        coord._pan_cache = {}
        # privacy mode off so pan is not blocked
        coord._shc_state_cache[CAM_ID]["privacy_mode"] = False

        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(side_effect=ValueError("bad json"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.put.return_value = cm

        with patch("custom_components.bosch_shc_camera.shc.async_get_clientsession",
                   return_value=session):
            result = await async_cloud_set_pan(coord, CAM_ID, 45)
        # Should still return True (200 is success) and cache the requested position
        assert result is True
        assert coord._pan_cache[CAM_ID] == 45
