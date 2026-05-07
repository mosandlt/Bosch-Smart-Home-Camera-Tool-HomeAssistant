"""Sprint D: __init__.py coordinator method branches — round 9.

Covers missing lines:
  async_put_camera (4124-4155): 401→refresh+retry, 401→refresh-fails→False,
    200/201/204→True, exception→False.
  get_quality (4073-4086): runtime-override, high_quality_video option, default 'auto'.
  get_quality_params (4094-4102): high→(True,1), low→(False,4), auto→(False,2).
  set_quality (4087-4092): sets preference + invalidates proxy_url_cache.
  _async_update_rcp_data (4048-4056): thin delegation wrapper.

Pattern: unbound method binding via types.MethodType on SimpleNamespace stubs,
identical to test_init_round8.py.
"""
from __future__ import annotations

import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _resp_cm(status: int):
    resp = MagicMock()
    resp.status = status
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _stub_coord(**kwargs):
    coord = SimpleNamespace(
        token="tok-A",
        hass=MagicMock(),
        _entry=SimpleNamespace(entry_id="01ENTRY"),
        _quality_preference={},
        _proxy_url_cache={},
        _rcp_state_cache={},
        _live_connections={},
    )
    for k, v in kwargs.items():
        setattr(coord, k, v)
    return coord


# ── async_put_camera ──────────────────────────────────────────────────────────


class TestAsyncPutCamera:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord.async_put_camera = types.MethodType(
            BoschCameraCoordinator.async_put_camera, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_http_200_returns_true(self):
        """HTTP 200 from PUT → True."""
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(200))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord.async_put_camera(CAM_ID, "privacy", {"enabled": True})

        assert result is True, "HTTP 200 must return True"

    @pytest.mark.asyncio
    async def test_http_204_returns_true(self):
        """HTTP 204 from PUT → True."""
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(204))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord.async_put_camera(CAM_ID, "privacy", {"enabled": False})

        assert result is True, "HTTP 204 must return True"

    @pytest.mark.asyncio
    async def test_http_403_returns_false(self):
        """HTTP 403 from PUT → False (not a success status)."""
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm(403))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord.async_put_camera(CAM_ID, "privacy", {})

        assert result is False, "HTTP 403 must return False"

    @pytest.mark.asyncio
    async def test_http_401_refreshes_token_and_retries(self):
        """HTTP 401 → refresh token → retry PUT → True on 200."""
        coord = self._bind(_stub_coord())
        coord._ensure_valid_token = AsyncMock(return_value="new-tok")

        first_resp = MagicMock()
        first_resp.status = 401
        retry_resp = MagicMock()
        retry_resp.status = 200

        call_count = [0]
        def _put_cm(*args, **kwargs):
            call_count[0] += 1
            cm = MagicMock()
            if call_count[0] == 1:
                cm.__aenter__ = AsyncMock(return_value=first_resp)
            else:
                cm.__aenter__ = AsyncMock(return_value=retry_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        session = MagicMock()
        session.put = MagicMock(side_effect=_put_cm)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord.async_put_camera(CAM_ID, "privacy", {})

        assert result is True, "After 401+refresh, successful retry must return True"
        coord._ensure_valid_token.assert_awaited_once()
        assert call_count[0] == 2, "PUT must be called twice (initial + retry)"

    @pytest.mark.asyncio
    async def test_http_401_refresh_fails_returns_false(self):
        """HTTP 401 → token refresh raises → returns False without crash."""
        coord = self._bind(_stub_coord())
        coord._ensure_valid_token = AsyncMock(side_effect=Exception("refresh failed"))

        first_resp = MagicMock()
        first_resp.status = 401
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=first_resp)
        cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.put = MagicMock(return_value=cm)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord.async_put_camera(CAM_ID, "privacy", {})

        assert result is False, "Failed token refresh after 401 must return False"

    @pytest.mark.asyncio
    async def test_401_retry_returns_false_on_non_success(self):
        """HTTP 401 → refresh OK → retry returns 403 → False."""
        coord = self._bind(_stub_coord())
        coord._ensure_valid_token = AsyncMock(return_value="new-tok")

        first_resp = MagicMock()
        first_resp.status = 401
        retry_resp = MagicMock()
        retry_resp.status = 403

        call_count = [0]
        def _put_cm(*args, **kwargs):
            call_count[0] += 1
            cm = MagicMock()
            if call_count[0] == 1:
                cm.__aenter__ = AsyncMock(return_value=first_resp)
            else:
                cm.__aenter__ = AsyncMock(return_value=retry_resp)
            cm.__aexit__ = AsyncMock(return_value=None)
            return cm

        session = MagicMock()
        session.put = MagicMock(side_effect=_put_cm)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord.async_put_camera(CAM_ID, "privacy", {})

        assert result is False, "Non-200/204 retry after 401 must return False"

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        """aiohttp exception → False, no crash."""
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.put.side_effect = OSError("connection refused")

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord.async_put_camera(CAM_ID, "privacy", {})

        assert result is False, "Network exception must return False"

    @pytest.mark.asyncio
    async def test_uses_bearer_token_in_header(self):
        """PUT request must include Authorization: Bearer header."""
        coord = self._bind(_stub_coord(token="my-secret-token"))
        captured_headers = []
        session = MagicMock()

        def _put_cm(*args, **kwargs):
            captured_headers.append(kwargs.get("headers", {}))
            return _resp_cm(200)

        session.put = MagicMock(side_effect=_put_cm)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await coord.async_put_camera(CAM_ID, "privacy", {})

        assert captured_headers[0].get("Authorization") == "Bearer my-secret-token", \
            "PUT must include Authorization: Bearer header with coordinator token"


# ── get_quality / set_quality / get_quality_params ────────────────────────────


class TestGetQuality:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord.get_quality = types.MethodType(BoschCameraCoordinator.get_quality, coord)
        coord.set_quality = types.MethodType(BoschCameraCoordinator.set_quality, coord)
        coord.get_quality_params = types.MethodType(
            BoschCameraCoordinator.get_quality_params, coord
        )
        return coord

    def _make_entry(self, **opts):
        return SimpleNamespace(options=opts)

    def test_runtime_override_takes_precedence(self):
        """Runtime preference in _quality_preference overrides entry options."""
        coord = self._bind(_stub_coord(
            _quality_preference={CAM_ID: "low"},
            _entry=self._make_entry(high_quality_video=True),
        ))
        assert coord.get_quality(CAM_ID) == "low", \
            "Runtime _quality_preference must override entry options"

    def test_high_quality_option_returns_high(self):
        """high_quality_video=True in entry options → 'high' when no runtime override."""
        coord = self._bind(_stub_coord(
            _quality_preference={},
            _entry=self._make_entry(high_quality_video=True),
        ))
        with patch(f"{MODULE}.get_options", return_value={"high_quality_video": True}):
            result = coord.get_quality(CAM_ID)
        assert result == "high", "high_quality_video=True must return 'high'"

    def test_default_returns_auto(self):
        """No runtime override and high_quality_video=False → 'auto'."""
        coord = self._bind(_stub_coord(
            _quality_preference={},
            _entry=self._make_entry(high_quality_video=False),
        ))
        with patch(f"{MODULE}.get_options", return_value={"high_quality_video": False}):
            result = coord.get_quality(CAM_ID)
        assert result == "auto", "Default quality must be 'auto'"

    def test_no_option_returns_auto(self):
        """No high_quality_video key in options → 'auto'."""
        coord = self._bind(_stub_coord(
            _quality_preference={},
            _entry=self._make_entry(),
        ))
        with patch(f"{MODULE}.get_options", return_value={}):
            result = coord.get_quality(CAM_ID)
        assert result == "auto", "Missing high_quality_video key must default to 'auto'"


class TestSetQuality:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord.set_quality = types.MethodType(BoschCameraCoordinator.set_quality, coord)
        return coord

    def test_set_quality_updates_preference(self):
        """set_quality must store the value in _quality_preference."""
        coord = self._bind(_stub_coord(_quality_preference={}, _proxy_url_cache={}))
        coord.set_quality(CAM_ID, "high")
        assert coord._quality_preference[CAM_ID] == "high", \
            "set_quality must write to _quality_preference"

    def test_set_quality_invalidates_proxy_url_cache(self):
        """set_quality must evict the cam's entry from _proxy_url_cache."""
        coord = self._bind(_stub_coord(
            _quality_preference={},
            _proxy_url_cache={CAM_ID: "rtsp://old-url"},
        ))
        coord.set_quality(CAM_ID, "low")
        assert CAM_ID not in coord._proxy_url_cache, \
            "set_quality must invalidate cached proxy URL to force fresh PUT /connection"


class TestGetQualityParams:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord.get_quality = types.MethodType(BoschCameraCoordinator.get_quality, coord)
        coord.get_quality_params = types.MethodType(
            BoschCameraCoordinator.get_quality_params, coord
        )
        return coord

    def test_high_returns_primary_encoder(self):
        """quality='high' → (True, 1) — primary encoder, max quality."""
        coord = self._bind(_stub_coord(_quality_preference={CAM_ID: "high"}))
        hq, inst = coord.get_quality_params(CAM_ID)
        assert hq is True and inst == 1, \
            "high quality must return (True, 1) — primary encoder"

    def test_low_returns_low_bandwidth_stream(self):
        """quality='low' → (False, 4) — low-bandwidth stream ~1.9 Mbps."""
        coord = self._bind(_stub_coord(_quality_preference={CAM_ID: "low"}))
        hq, inst = coord.get_quality_params(CAM_ID)
        assert hq is False and inst == 4, \
            "low quality must return (False, 4) — low-bandwidth stream"

    def test_auto_returns_balanced_stream(self):
        """quality='auto' → (False, 2) — balanced iOS default ~7.5 Mbps."""
        coord = self._bind(_stub_coord(_quality_preference={}))
        with patch(f"{MODULE}.get_options", return_value={}):
            hq, inst = coord.get_quality_params(CAM_ID)
        assert hq is False and inst == 2, \
            "auto quality must return (False, 2) — balanced stream"


# ── _async_update_rcp_data delegation ────────────────────────────────────────


class TestAsyncUpdateRcpDataDelegation:
    @pytest.mark.asyncio
    async def test_delegates_to_rcp_module(self):
        """_async_update_rcp_data must delegate to rcp.async_update_rcp_data."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _stub_coord()
        coord._async_update_rcp_data = types.MethodType(
            BoschCameraCoordinator._async_update_rcp_data, coord
        )

        with patch(f"{MODULE}.async_update_rcp_data", new_callable=AsyncMock) as mock_rcp:
            await coord._async_update_rcp_data(CAM_ID, "proxy-host", "proxy-hash")

        mock_rcp.assert_awaited_once_with(coord, CAM_ID, "proxy-host", "proxy-hash")
