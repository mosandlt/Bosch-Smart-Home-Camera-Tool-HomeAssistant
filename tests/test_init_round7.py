"""Sprint B: __init__.py snapshot methods + RCP helpers.

Covers missing lines in:
  _cleanup_stale_devices (2216-2228)
  _async_fetch_live_snapshot_impl (2918-3064)
  async_fetch_fresh_event_snapshot (3073-3123)
  async_fetch_live_snapshot_local (3134-3211)
  _rcp_read_active (3225-3250)
  _invalidate_rcp_session, _get_cached_rcp_session, _rcp_session (3958-4015)
  _rcp_read (4040-4073)
"""

from __future__ import annotations

import asyncio
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
PROXY_URL = "proxy-01.live.cbs.boschsecurity.com:42090/abc123hash"


def _resp_cm(status: int, text: str = "", body: bytes = b"",
             headers: dict | None = None, json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=body or text.encode())
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    resp.headers = headers or {}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _timeout_cm():
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _client_error_cm():
    import aiohttp
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn error"))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _aiohttp_mocks():
    connector = MagicMock()
    connector.close = AsyncMock()
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    return connector, session


def _stub_coord(**kwargs):
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(return_value=None)
    coord = SimpleNamespace(
        token="test-bearer-token",
        hass=hass,
        _entry=SimpleNamespace(entry_id="01ENTRY"),
        _proxy_url_cache={},
        _camera_status_extra={},
        _rcp_session_cache={},
        _live_connections={},
    )
    coord.get_quality_params = MagicMock(return_value=(True, 0))
    coord._get_cached_rcp_session = AsyncMock(return_value=None)
    coord._rcp_read = AsyncMock(return_value=None)
    coord._rcp_session = AsyncMock(return_value="0xABCDEF01")
    coord._invalidate_rcp_session = MagicMock()
    for k, v in kwargs.items():
        setattr(coord, k, v)
    return coord


# ── _cleanup_stale_devices ──────────────────────────────────────────────────


class TestCleanupStaleDevices:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._cleanup_stale_devices = types.MethodType(
            BoschCameraCoordinator._cleanup_stale_devices, coord
        )
        return coord

    def test_no_devices_nothing_removed(self):
        coord = self._bind(_stub_coord())
        dev_reg = MagicMock()
        with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[]):
            coord._cleanup_stale_devices({"cam1"})
        dev_reg.async_remove_device.assert_not_called()

    def test_stale_device_removed(self):
        coord = self._bind(_stub_coord())
        dev_reg = MagicMock()
        stale = MagicMock()
        stale.identifiers = {("bosch_shc_camera", "STALE_CAM")}
        stale.id = "dev-stale"
        with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[stale]):
            coord._cleanup_stale_devices({"OTHER_CAM"})
        dev_reg.async_remove_device.assert_called_once_with("dev-stale")

    def test_active_device_not_removed(self):
        coord = self._bind(_stub_coord())
        dev_reg = MagicMock()
        active = MagicMock()
        active.identifiers = {("bosch_shc_camera", CAM_ID)}
        with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[active]):
            coord._cleanup_stale_devices({CAM_ID})
        dev_reg.async_remove_device.assert_not_called()

    def test_device_without_domain_identifier_skipped(self):
        coord = self._bind(_stub_coord())
        dev_reg = MagicMock()
        other = MagicMock()
        other.identifiers = {("other_domain", "some_id")}
        with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[other]):
            coord._cleanup_stale_devices(set())
        dev_reg.async_remove_device.assert_not_called()

    def test_mixed_devices_only_stale_removed(self):
        coord = self._bind(_stub_coord())
        dev_reg = MagicMock()
        active = MagicMock()
        active.identifiers = {("bosch_shc_camera", CAM_ID)}
        stale = MagicMock()
        stale.identifiers = {("bosch_shc_camera", "STALE")}
        stale.id = "stale-id"
        with patch("homeassistant.helpers.device_registry.async_get", return_value=dev_reg), \
             patch("homeassistant.helpers.device_registry.async_entries_for_config_entry",
                   return_value=[active, stale]):
            coord._cleanup_stale_devices({CAM_ID})
        dev_reg.async_remove_device.assert_called_once_with("stale-id")


# ── _async_fetch_live_snapshot_impl ─────────────────────────────────────────


class TestFetchLiveSnapshotImpl:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._async_fetch_live_snapshot_impl = types.MethodType(
            BoschCameraCoordinator._async_fetch_live_snapshot_impl, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        coord = self._bind(_stub_coord(token=None))
        assert await coord._async_fetch_live_snapshot_impl(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_privacy_mode_on_returns_none(self):
        coord = self._bind(_stub_coord(
            _camera_status_extra={CAM_ID: {"privacy_mode": True}}
        ))
        assert await coord._async_fetch_live_snapshot_impl(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_put_connection_non200_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(return_value=_resp_cm(403))
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_put_connection_no_urls_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(return_value=_resp_cm(200, text='{"urls": []}'))
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_snap_jpg_200_image_returns_bytes(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(
            return_value=_resp_cm(200, text=f'{{"urls": ["{PROXY_URL}"]}}')
        )
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"\xff\xd8\xff\xe0" + b"\x00" * 40,
                                  headers={"Content-Type": "image/jpeg"})
        )
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is not None and result[:2] == b"\xff\xd8"

    @pytest.mark.asyncio
    async def test_snap_jpg_empty_body_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(
            return_value=_resp_cm(200, text=f'{{"urls": ["{PROXY_URL}"]}}')
        )
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"", headers={"Content-Type": "image/jpeg"})
        )
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_snap_jpg_non_image_content_type_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(
            return_value=_resp_cm(200, text=f'{{"urls": ["{PROXY_URL}"]}}')
        )
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"<html/>", headers={"Content-Type": "text/html"})
        )
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_proxy_cache_hit_skips_put(self):
        """Cached proxy URL → PUT /connection not called."""
        coord = self._bind(_stub_coord(
            _proxy_url_cache={CAM_ID: (PROXY_URL, time.monotonic() + 30)}
        ))
        connector, session = _aiohttp_mocks()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"\xff\xd8\xff\xe0",
                                  headers={"Content-Type": "image/jpeg"})
        )
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        session.put.assert_not_called()
        assert result == b"\xff\xd8\xff\xe0"

    @pytest.mark.asyncio
    async def test_rcp_jpeg_returned_directly(self):
        """RCP 0x099e returns a JPEG → snap.jpg fetch is skipped."""
        coord = self._bind(_stub_coord(
            _proxy_url_cache={CAM_ID: (PROXY_URL, time.monotonic() + 30)},
        ))
        coord._get_cached_rcp_session = AsyncMock(return_value="0xSESSION")
        coord._rcp_read = AsyncMock(return_value=b"\xff\xd8\xff\xe0" + b"\x00" * 20)
        connector, session = _aiohttp_mocks()
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        session.get.assert_not_called()
        assert result is not None and result[:2] == b"\xff\xd8"

    @pytest.mark.asyncio
    async def test_rcp_non_jpeg_falls_through_to_snap(self):
        """RCP 0x099e returns non-JPEG (e.g. error response) → fall through to snap.jpg."""
        coord = self._bind(_stub_coord(
            _proxy_url_cache={CAM_ID: (PROXY_URL, time.monotonic() + 30)},
        ))
        coord._get_cached_rcp_session = AsyncMock(return_value="0xSESSION")
        coord._rcp_read = AsyncMock(return_value=b"\x00\x00\x00\x00")  # not JPEG
        connector, session = _aiohttp_mocks()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"\xff\xd8\xff\xe0",
                                  headers={"Content-Type": "image/jpeg"})
        )
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        session.get.assert_called()
        assert result is not None

    @pytest.mark.asyncio
    async def test_snap_404_retry_success(self):
        """snap.jpg 404 → cache cleared → retry PUT + GET → return image bytes."""
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()

        put_resp1 = MagicMock()
        put_resp1.status = 200
        put_resp1.text = AsyncMock(return_value=f'{{"urls": ["{PROXY_URL}"]}}')
        put_cm1 = MagicMock()
        put_cm1.__aenter__ = AsyncMock(return_value=put_resp1)
        put_cm1.__aexit__ = AsyncMock(return_value=None)

        put_resp2 = MagicMock()
        put_resp2.status = 200
        put_resp2.text = AsyncMock(return_value=f'{{"urls": ["{PROXY_URL}"]}}')
        put_cm2 = MagicMock()
        put_cm2.__aenter__ = AsyncMock(return_value=put_resp2)
        put_cm2.__aexit__ = AsyncMock(return_value=None)

        snap_404 = _resp_cm(404)
        snap_ok = _resp_cm(200, body=b"\xff\xd8\xff\xe0",
                           headers={"Content-Type": "image/jpeg"})

        session.put = MagicMock(side_effect=[put_cm1, put_cm2])
        session.get = MagicMock(side_effect=[snap_404, snap_ok])

        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is not None and result[:2] == b"\xff\xd8"

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(return_value=_timeout_cm())
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(return_value=_client_error_cm())
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._async_fetch_live_snapshot_impl(CAM_ID)
        assert result is None


# ── async_fetch_fresh_event_snapshot ────────────────────────────────────────


class TestFetchFreshEventSnapshot:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord.async_fetch_fresh_event_snapshot = types.MethodType(
            BoschCameraCoordinator.async_fetch_fresh_event_snapshot, coord
        )
        return coord

    _PATCH = "homeassistant.helpers.aiohttp_client.async_get_clientsession"

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        coord = self._bind(_stub_coord(token=None))
        assert await coord.async_fetch_fresh_event_snapshot(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_events_api_non200_returns_none(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(401))
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_events_returns_none(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, text="[]"))
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_event_without_imageurl_skipped(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, text='[{"timestamp": "2026-01-01T00:00:00Z"}]')
        )
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_unsafe_url_skipped_returns_none(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, text='[{"imageUrl": "https://evil.com/snap.jpg"}]')
        )
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_event_returns_bytes(self):
        coord = self._bind(_stub_coord())
        img_url = "https://events.cbs.boschsecurity.com/snap/img123.jpg"
        events_cm = _resp_cm(200, text=f'[{{"imageUrl": "{img_url}"}}]')
        img_cm = _resp_cm(200, body=b"\xff\xd8\xff\xe0" + b"\x00" * 50)
        session = MagicMock()
        session.get = MagicMock(side_effect=[events_cm, img_cm])
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is not None and result[:2] == b"\xff\xd8"

    @pytest.mark.asyncio
    async def test_image_fetch_empty_body_tries_next_event(self):
        """img fetch returns 200 but empty body → skip, return None (no more events)."""
        coord = self._bind(_stub_coord())
        img_url = "https://events.cbs.boschsecurity.com/snap/img1.jpg"
        events_cm = _resp_cm(200, text=f'[{{"imageUrl": "{img_url}"}}]')
        img_cm = _resp_cm(200, body=b"")  # empty
        session = MagicMock()
        session.get = MagicMock(side_effect=[events_cm, img_cm])
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_image_inner_timeout_continues_to_next(self):
        """Timeout on individual imageUrl fetch → try next event."""
        coord = self._bind(_stub_coord())
        img_url = "https://events.cbs.boschsecurity.com/snap/img1.jpg"
        events_cm = _resp_cm(200, text=f'[{{"imageUrl": "{img_url}"}}]')
        session = MagicMock()
        session.get = MagicMock(side_effect=[events_cm, _timeout_cm()])
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_outer_timeout_returns_none(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(return_value=_timeout_cm())
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_outer_client_error_returns_none(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(return_value=_client_error_cm())
        with patch(self._PATCH, return_value=session):
            result = await coord.async_fetch_fresh_event_snapshot(CAM_ID)
        assert result is None


# ── async_fetch_live_snapshot_local ─────────────────────────────────────────


class TestFetchLiveSnapshotLocal:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord.async_fetch_live_snapshot_local = types.MethodType(
            BoschCameraCoordinator.async_fetch_live_snapshot_local, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        coord = self._bind(_stub_coord(token=None))
        assert await coord.async_fetch_live_snapshot_local(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_privacy_mode_on_returns_none(self):
        coord = self._bind(_stub_coord(
            _camera_status_extra={CAM_ID: {"privacy_mode": True}}
        ))
        assert await coord.async_fetch_live_snapshot_local(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_put_local_non200_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(return_value=_resp_cm(403))
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord.async_fetch_live_snapshot_local(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_put_local_timeout_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(return_value=_timeout_cm())
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord.async_fetch_live_snapshot_local(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_user_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        # No "user" key in result
        session.put = MagicMock(
            return_value=_resp_cm(200, text='{"password":"p","urls":["192.0.2.149:443"]}')
        )
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord.async_fetch_live_snapshot_local(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_urls_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(
            return_value=_resp_cm(200, text='{"user":"u","password":"p","urls":[]}')
        )
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord.async_fetch_live_snapshot_local(CAM_ID)
        assert result is None

    @pytest.mark.asyncio
    async def test_digest_success_returns_bytes(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(
            return_value=_resp_cm(
                200, text='{"user":"u","password":"p","urls":["192.0.2.149:443"]}'
            )
        )
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        coord.hass.async_add_executor_job = AsyncMock(return_value=jpeg)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord.async_fetch_live_snapshot_local(CAM_ID)
        assert result == jpeg

    @pytest.mark.asyncio
    async def test_executor_returns_none_propagated(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.put = MagicMock(
            return_value=_resp_cm(
                200, text='{"user":"u","password":"p","urls":["192.0.2.149:443"]}'
            )
        )
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord.async_fetch_live_snapshot_local(CAM_ID)
        assert result is None


# ── _rcp_read_active ─────────────────────────────────────────────────────────


class TestRcpReadActive:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._rcp_read_active = types.MethodType(
            BoschCameraCoordinator._rcp_read_active, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_no_live_connection_returns_none(self):
        coord = self._bind(_stub_coord())
        assert await coord._rcp_read_active(CAM_ID, "0x0c22", "T_WORD") is None

    @pytest.mark.asyncio
    async def test_unknown_connection_type_returns_none(self):
        coord = self._bind(_stub_coord(
            _live_connections={CAM_ID: {"_connection_type": "TUNNEL"}}
        ))
        assert await coord._rcp_read_active(CAM_ID, "0x0c22", "T_WORD") is None

    @pytest.mark.asyncio
    async def test_local_missing_creds_returns_none(self):
        coord = self._bind(_stub_coord(
            _live_connections={CAM_ID: {
                "_connection_type": "LOCAL",
                "_local_user": "",
                "_local_password": "p",
                "urls": ["192.0.2.149:443"],
            }}
        ))
        assert await coord._rcp_read_active(CAM_ID, "0x0c22", "T_WORD") is None

    @pytest.mark.asyncio
    async def test_local_missing_urls_returns_none(self):
        coord = self._bind(_stub_coord(
            _live_connections={CAM_ID: {
                "_connection_type": "LOCAL",
                "_local_user": "u",
                "_local_password": "p",
                "urls": [],
            }}
        ))
        assert await coord._rcp_read_active(CAM_ID, "0x0c22", "T_WORD") is None

    @pytest.mark.asyncio
    async def test_local_dispatches_to_executor(self):
        coord = self._bind(_stub_coord(
            _live_connections={CAM_ID: {
                "_connection_type": "LOCAL",
                "_local_user": "user",
                "_local_password": "pass",
                "urls": ["192.0.2.149:443"],
            }}
        ))
        coord.hass.async_add_executor_job = AsyncMock(return_value=b"\x01\x02")
        result = await coord._rcp_read_active(CAM_ID, "0x0c22", "T_WORD")
        assert result == b"\x01\x02"
        coord.hass.async_add_executor_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_remote_missing_urls_returns_none(self):
        coord = self._bind(_stub_coord(
            _live_connections={CAM_ID: {"_connection_type": "REMOTE", "urls": []}}
        ))
        assert await coord._rcp_read_active(CAM_ID, "0x0c22", "T_WORD") is None

    @pytest.mark.asyncio
    async def test_remote_dispatches_to_executor(self):
        coord = self._bind(_stub_coord(
            _live_connections={CAM_ID: {
                "_connection_type": "REMOTE",
                "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hash123"],
            }}
        ))
        coord.hass.async_add_executor_job = AsyncMock(return_value=b"\xab\xcd")
        result = await coord._rcp_read_active(CAM_ID, "0x0c22", "T_WORD")
        assert result == b"\xab\xcd"
        coord.hass.async_add_executor_job.assert_awaited_once()


# ── RCP session cache helpers ────────────────────────────────────────────────


class TestInvalidateRcpSession:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._invalidate_rcp_session = types.MethodType(
            BoschCameraCoordinator._invalidate_rcp_session, coord
        )
        return coord

    def test_present_entry_removed(self):
        coord = self._bind(_stub_coord(
            _rcp_session_cache={"abc123": ("0x12345678", time.monotonic() + 300)}
        ))
        coord._invalidate_rcp_session("abc123")
        assert "abc123" not in coord._rcp_session_cache

    def test_absent_entry_no_error(self):
        coord = self._bind(_stub_coord(_rcp_session_cache={}))
        coord._invalidate_rcp_session("nonexistent")  # must not raise


class TestGetCachedRcpSession:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._get_cached_rcp_session = types.MethodType(
            BoschCameraCoordinator._get_cached_rcp_session, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_id(self):
        expires = time.monotonic() + 200
        coord = self._bind(_stub_coord(
            _rcp_session_cache={"abc123": ("0xCAFEBABE", expires)}
        ))
        coord._rcp_session = AsyncMock(return_value="0xNEW")
        result = await coord._get_cached_rcp_session("proxy-01:42090", "abc123")
        assert result == "0xCAFEBABE"
        coord._rcp_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cache_expired_calls_rcp_session(self):
        coord = self._bind(_stub_coord(
            _rcp_session_cache={"abc123": ("0xOLD", time.monotonic() - 1)}
        ))
        coord._rcp_session = AsyncMock(return_value="0xFRESH")
        result = await coord._get_cached_rcp_session("proxy-01:42090", "abc123")
        assert result == "0xFRESH"
        assert coord._rcp_session_cache["abc123"][0] == "0xFRESH"

    @pytest.mark.asyncio
    async def test_cache_miss_stores_new_session(self):
        coord = self._bind(_stub_coord(_rcp_session_cache={}))
        coord._rcp_session = AsyncMock(return_value="0x12345678")
        result = await coord._get_cached_rcp_session("proxy-01:42090", "abc123")
        assert result == "0x12345678"
        assert "abc123" in coord._rcp_session_cache

    @pytest.mark.asyncio
    async def test_rcp_session_none_not_cached(self):
        coord = self._bind(_stub_coord(_rcp_session_cache={}))
        coord._rcp_session = AsyncMock(return_value=None)
        result = await coord._get_cached_rcp_session("proxy-01:42090", "abc123")
        assert result is None
        assert "abc123" not in coord._rcp_session_cache


class TestCoordRcpSession:
    """BoschCameraCoordinator._rcp_session — coordinator method (parallel to rcp.rcp_session)."""

    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._rcp_session = types.MethodType(
            BoschCameraCoordinator._rcp_session, coord
        )
        return coord

    def _make_session(self, *response_cms):
        connector, session = _aiohttp_mocks()
        session.get = MagicMock(side_effect=list(response_cms))
        return connector, session

    @pytest.mark.asyncio
    async def test_success_returns_session_id(self):
        coord = self._bind(_stub_coord())
        step1 = _resp_cm(200, text="<sessionid>0x12345678</sessionid>")
        step2 = _resp_cm(200)
        connector, session = self._make_session(step1, step2)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._rcp_session("proxy-01:42090", "abc123hash")
        assert result == "0x12345678"

    @pytest.mark.asyncio
    async def test_step1_non200_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = self._make_session(_resp_cm(403))
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._rcp_session("proxy-01:42090", "abc123hash")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_sessionid_in_response_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = self._make_session(_resp_cm(200, text="<result>ok</result>"))
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._rcp_session("proxy-01:42090", "abc123hash")
        assert result is None

    @pytest.mark.asyncio
    async def test_step1_timeout_returns_none(self):
        coord = self._bind(_stub_coord())
        connector, session = _aiohttp_mocks()
        session.get = MagicMock(return_value=_timeout_cm())
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._rcp_session("proxy-01:42090", "abc123hash")
        assert result is None

    @pytest.mark.asyncio
    async def test_step2_timeout_still_returns_session_id(self):
        """Step2 (ACK) timeout is non-fatal — session_id already extracted."""
        coord = self._bind(_stub_coord())
        step1 = _resp_cm(200, text="<sessionid>0xABCDEF01</sessionid>")
        connector, session = _aiohttp_mocks()
        session.get = MagicMock(side_effect=[step1, _timeout_cm()])
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await coord._rcp_session("proxy-01:42090", "abc123hash")
        assert result == "0xABCDEF01"


# ── _rcp_read ────────────────────────────────────────────────────────────────


RCP_BASE = "https://proxy-01.live.cbs.boschsecurity.com:42090/abc123/rcp.xml"


class TestRcpRead:
    def _bind(self, coord):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord._rcp_read = types.MethodType(BoschCameraCoordinator._rcp_read, coord)
        coord._proxy_hash_from_rcp_base = BoschCameraCoordinator._proxy_hash_from_rcp_base
        coord._invalidate_rcp_session = types.MethodType(
            BoschCameraCoordinator._invalidate_rcp_session, coord
        )
        return coord

    @pytest.mark.asyncio
    async def test_200_returns_raw_bytes(self):
        coord = self._bind(_stub_coord())
        raw = b"\x01\x02\x03\x04"
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, body=raw))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord._rcp_read(RCP_BASE, "0x0c22", "0x12345678")
        assert result == raw

    @pytest.mark.asyncio
    async def test_401_invalidates_session_returns_none(self):
        coord = self._bind(_stub_coord(
            _rcp_session_cache={"abc123": ("0x12345678", time.monotonic() + 300)}
        ))
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(401))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord._rcp_read(RCP_BASE, "0x0c22", "0x12345678")
        assert result is None
        assert "abc123" not in coord._rcp_session_cache

    @pytest.mark.asyncio
    async def test_403_invalidates_session_returns_none(self):
        coord = self._bind(_stub_coord(
            _rcp_session_cache={"abc123": ("0x12345678", time.monotonic() + 300)}
        ))
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(403))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord._rcp_read(RCP_BASE, "0x0c22", "0x12345678")
        assert result is None
        assert "abc123" not in coord._rcp_session_cache

    @pytest.mark.asyncio
    async def test_session_closed_0x0c0d_invalidates_returns_none(self):
        coord = self._bind(_stub_coord(
            _rcp_session_cache={"abc123": ("0x12345678", time.monotonic() + 300)}
        ))
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"<err>0x0c0d</err>")
        )
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord._rcp_read(RCP_BASE, "0x0c22", "0x12345678")
        assert result is None
        assert "abc123" not in coord._rcp_session_cache

    @pytest.mark.asyncio
    async def test_500_returns_none_no_invalidate(self):
        coord = self._bind(_stub_coord(
            _rcp_session_cache={"abc123": ("0x12345678", time.monotonic() + 300)}
        ))
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord._rcp_read(RCP_BASE, "0x0c22", "0x12345678")
        assert result is None
        assert "abc123" in coord._rcp_session_cache  # not invalidated

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(return_value=_timeout_cm())
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await coord._rcp_read(RCP_BASE, "0x0c22", "0x12345678")
        assert result is None

    @pytest.mark.asyncio
    async def test_num_param_included_when_nonzero(self):
        coord = self._bind(_stub_coord())
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, body=b"\x01"))
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await coord._rcp_read(RCP_BASE, "0x0c22", "0x12345678", num=5)
        _, call_kwargs = session.get.call_args
        assert call_kwargs.get("params", {}).get("num") == "5"

    def test_proxy_hash_from_rcp_base_extracts_hash(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        h = BoschCameraCoordinator._proxy_hash_from_rcp_base(
            "https://proxy-01:42090/abc123hash/rcp.xml"
        )
        assert h == "abc123hash"

    def test_proxy_hash_from_rcp_base_invalid_returns_none(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        assert BoschCameraCoordinator._proxy_hash_from_rcp_base("https://nohash") is None
