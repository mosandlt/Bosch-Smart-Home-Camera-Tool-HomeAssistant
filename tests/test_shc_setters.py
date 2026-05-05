"""Tests for shc.py cloud setter functions with aiohttp response mocks.

Each setter follows the same pattern:
  - PUT to Bosch cloud API (~150ms)
  - On success → update _shc_state_cache + record write timestamp
  - On 401 → silent token refresh + retry
  - On failure → fall back to SHC local API (if configured)
  - All-fail → return False (some setters surface a notification)

The tests stub `aiohttp.ClientSession.put/get` so no real network calls
happen. Each test pins one branch of the strategy (cloud-success,
cloud-401-with-retry, cloud-fail-fallback-SHC, all-fail).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _mock_response(status: int, json_data=None, text: str = ""):
    """Build a mock aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _stub_coord(*, gen2: bool = True, with_token: bool = True):
    """Stub coordinator providing the fields shc.py setters touch."""
    return SimpleNamespace(
        token="token-AAA" if with_token else "",
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        _shc_state_cache={CAM_ID: {"front_light_intensity": 0.5}},
        _privacy_set_at={},
        _light_set_at={},
        _notif_set_at={},
        _local_creds_cache={},
        _rcp_lan_ip_cache={},
        _pan_cache={},
        _camera_entities={},  # used by _schedule_privacy_off_snapshot
        _hw_version={CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"},
        _auth_outage_count=0,
        async_update_listeners=lambda: None,
        async_request_refresh=AsyncMock(),
        _ensure_valid_token=AsyncMock(return_value="token-FRESH"),
    )


# ── async_cloud_set_privacy_mode ────────────────────────────────────────


class TestCloudSetPrivacyMode:
    @pytest.mark.asyncio
    async def test_success_updates_cache_and_lock(self):
        """Cloud PUT 204 → cache + lock + listener call."""
        coord = _stub_coord()
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            session.put = MagicMock(return_value=_mock_response(204))
            session_factory.return_value = session
            from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode
            ok = await async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert ok is True
        assert coord._shc_state_cache[CAM_ID]["privacy_mode"] is True
        assert CAM_ID in coord._privacy_set_at  # lock recorded
        # Lock timestamp must be recent
        assert time.monotonic() - coord._privacy_set_at[CAM_ID] < 1.0

    @pytest.mark.asyncio
    async def test_no_token_falls_through_to_shc(self):
        """No bearer token → skip cloud, try SHC fallback."""
        coord = _stub_coord(with_token=False)
        from custom_components.bosch_shc_camera import shc
        # SHC not configured → returns False
        with patch.object(shc, "shc_ready", return_value=False):
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert ok is False
        # Cache untouched (not optimistically written when nothing succeeded)
        assert "privacy_mode" not in coord._shc_state_cache.get(CAM_ID, {})

    @pytest.mark.asyncio
    async def test_http_401_triggers_token_refresh(self):
        """Cloud returns 401 → coordinator._ensure_valid_token called."""
        coord = _stub_coord()
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            # First PUT returns 401, second PUT (after refresh) returns 204
            session.put = MagicMock(
                side_effect=[
                    _mock_response(401),
                    _mock_response(204),
                ]
            )
            session_factory.return_value = session
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, False)
        assert coord._ensure_valid_token.called

    @pytest.mark.asyncio
    async def test_http_500_does_not_update_cache(self):
        """5xx response → cache stays untouched, no lock recorded."""
        coord = _stub_coord()
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory, \
             patch.object(shc, "shc_ready", return_value=False):
            session = MagicMock()
            session.put = MagicMock(return_value=_mock_response(500))
            session_factory.return_value = session
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert ok is False
        assert CAM_ID not in coord._privacy_set_at


# ── async_cloud_set_camera_light ────────────────────────────────────────


class TestCloudSetCameraLight:
    @pytest.mark.asyncio
    async def test_gen1_success(self):
        """Gen1 lighting_override PUT 204 → cache updated."""
        coord = _stub_coord(gen2=False)
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            session.put = MagicMock(return_value=_mock_response(204))
            session_factory.return_value = session
            ok = await shc.async_cloud_set_camera_light(coord, CAM_ID, True)
        assert ok is True
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is True
        assert CAM_ID in coord._light_set_at

    @pytest.mark.asyncio
    async def test_gen2_double_endpoint_partial_success(self):
        """Gen2: front + topdown endpoints. Partial success (one OK) is treated as success."""
        coord = _stub_coord(gen2=True)
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            session.put = MagicMock(
                side_effect=[
                    _mock_response(204),  # front OK
                    _mock_response(442),  # topdown not supported on this hw
                ]
            )
            session_factory.return_value = session
            ok = await shc.async_cloud_set_camera_light(coord, CAM_ID, True)
        # `ok = ok1 or ok2` → True
        assert ok is True

    @pytest.mark.asyncio
    async def test_gen2_both_endpoints_fail(self):
        coord = _stub_coord(gen2=True)
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory, \
             patch.object(shc, "shc_ready", return_value=False):
            session = MagicMock()
            session.put = MagicMock(
                side_effect=[
                    _mock_response(500),
                    _mock_response(500),
                ]
            )
            session_factory.return_value = session
            ok = await shc.async_cloud_set_camera_light(coord, CAM_ID, False)
        assert ok is False


# ── async_cloud_set_notifications ───────────────────────────────────────


class TestCloudSetNotifications:
    @pytest.mark.asyncio
    async def test_enable_writes_FOLLOW_CAMERA_SCHEDULE(self):
        coord = _stub_coord()
        captured_body = {}

        def _capture_put(url, json=None, headers=None):
            captured_body.update(json)
            return _mock_response(204)

        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            session.put = MagicMock(side_effect=_capture_put)
            session_factory.return_value = session
            ok = await shc.async_cloud_set_notifications(coord, CAM_ID, True)
        assert ok is True
        assert captured_body["enabledNotificationsStatus"] == "FOLLOW_CAMERA_SCHEDULE"
        assert coord._shc_state_cache[CAM_ID]["notifications_status"] == "FOLLOW_CAMERA_SCHEDULE"
        assert CAM_ID in coord._notif_set_at

    @pytest.mark.asyncio
    async def test_disable_writes_ALWAYS_OFF(self):
        coord = _stub_coord()
        captured = {}

        def _capture(url, json=None, headers=None):
            captured.update(json)
            return _mock_response(204)

        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            session.put = MagicMock(side_effect=_capture)
            session_factory.return_value = session
            ok = await shc.async_cloud_set_notifications(coord, CAM_ID, False)
        assert ok is True
        assert captured["enabledNotificationsStatus"] == "ALWAYS_OFF"

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        coord = _stub_coord(with_token=False)
        from custom_components.bosch_shc_camera import shc
        ok = await shc.async_cloud_set_notifications(coord, CAM_ID, True)
        assert ok is False

    @pytest.mark.asyncio
    async def test_http_failure_returns_false(self):
        coord = _stub_coord()
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            session.put = MagicMock(return_value=_mock_response(500))
            session_factory.return_value = session
            ok = await shc.async_cloud_set_notifications(coord, CAM_ID, True)
        assert ok is False
        # Cache must NOT have been updated
        assert "notifications_status" not in coord._shc_state_cache.get(CAM_ID, {})


# ── async_cloud_set_pan ─────────────────────────────────────────────────


class TestCloudSetPan:
    @pytest.mark.asyncio
    async def test_blocked_when_privacy_on(self):
        """Privacy ON → pan command must be blocked (camera motor disabled)."""
        coord = _stub_coord()
        coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        from custom_components.bosch_shc_camera import shc
        ok = await shc.async_cloud_set_pan(coord, CAM_ID, 30)
        assert ok is False

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        coord = _stub_coord(with_token=False)
        from custom_components.bosch_shc_camera import shc
        ok = await shc.async_cloud_set_pan(coord, CAM_ID, 30)
        assert ok is False

    @pytest.mark.asyncio
    async def test_success(self):
        coord = _stub_coord()
        from custom_components.bosch_shc_camera import shc
        with patch.object(shc, "async_get_clientsession") as session_factory:
            session = MagicMock()
            session.put = MagicMock(return_value=_mock_response(204))
            session_factory.return_value = session
            ok = await shc.async_cloud_set_pan(coord, CAM_ID, 30)
        assert ok is True
