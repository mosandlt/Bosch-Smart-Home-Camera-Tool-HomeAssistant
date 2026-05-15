"""Regression — ValueError from auth_utils must not escape camera_proxy.

Source: forum 998974/15 (Andrew75, 2026-05-15). His HA log showed
`Status code 500 (retry #1) loading /api/camera_proxy/camera.bosch_est`
emitted by `homeassistant.components.telegram_bot.bot`. Trigger: the
camera's 401 came back without a `WWW-Authenticate: Digest` header
(half-rotated Digest state during FCM-flap window). `auth_utils.async_
digest_request` raised `ValueError`, which slipped past the previous
`except (aiohttp.ClientError, asyncio.TimeoutError)` clauses in two
hot snapshot paths, propagated up to HA core, and produced HTTP 500.

Pin both paths so a future refactor of the `except` tuple cannot
reintroduce this:

1. `camera.py:_async_camera_image_impl` LOCAL Digest branch — used by
   `/api/camera_proxy/<entity>` (the endpoint Telegram + Lovelace +
   automations all share).
2. `__init__.py:async_fetch_live_snapshot_local` — used by the cloud
   fetch helper that falls back to direct LAN snap.jpg.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(**overrides):
    base = dict(
        data={CAM_ID: {"info": {"title": "Est", "hardwareVersion": "X"}, "events": []}},
        _live_connections={
            CAM_ID: {
                "_connection_type": "LOCAL",
                "proxyUrl": "https://192.0.2.1/snap.jpg",
                "_local_user": "cbs-1",
                "_local_password": "p",
            },
        },
        _live_opened_at={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        _shc_state_cache={},
        _stream_warming=set(),
        _image_rotation_180={},
        _local_creds_cache={},
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera(coord=None, **camera_overrides):
    from custom_components.bosch_shc_camera.camera import BoschCamera
    coord = coord or _make_coord()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = None
    cam._display_name = "Bosch Est"
    cam._cached_image = None
    cam._force_image_refresh = False
    cam._last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "X"
    cam._model_name = "X"
    cam._hw_version = "X"
    cam._fw = ""
    cam._mac = ""
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
        async_add_executor_job=AsyncMock(),
    )
    for k, v in camera_overrides.items():
        setattr(cam, k, v)
    return cam


# ── camera.py LOCAL Digest path ──────────────────────────────────────────


class TestCameraImageImplValueError:
    """`_async_camera_image_impl` LOCAL Digest branch must catch ValueError
    raised by `auth_utils.async_digest_request` (malformed/missing
    WWW-Authenticate). Without the catch, HA's CameraImageView gets a
    raised exception → HTTP 500 → Telegram, Lovelace and automation
    proxies all see a brown error frame / 500 response body."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("err_msg", [
        "Server returned 401 without WWW-Authenticate header for 'https://...'",
        "Expected Digest scheme, got: 'Basic'",
        "Digest challenge missing required 'nonce' directive",
    ])
    async def test_local_digest_value_error_returns_cached(self, err_msg: str):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = _make_camera(_cached_image=b"\xff\xd8cached")
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.camera.async_digest_request",
            new=AsyncMock(side_effect=ValueError(err_msg)),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        # LOCAL path returns cached image (or placeholder) on auth failure
        # instead of falling through to aiohttp (which would 401 again).
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_local_digest_value_error_no_cache_returns_placeholder(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = _make_camera()  # _cached_image=None
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.camera.async_digest_request",
            new=AsyncMock(side_effect=ValueError("auth broken")),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG

    @pytest.mark.asyncio
    async def test_local_digest_value_error_does_not_propagate(self):
        """Belt-and-braces: ValueError must NOT bubble up to the public
        async_camera_image wrapper (which has its own broad catch, but
        we don't rely on that — the LOCAL Digest except should swallow
        it directly)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = _make_camera()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.camera.async_digest_request",
            new=AsyncMock(side_effect=ValueError("no WWW-Authenticate")),
        ):
            # Must not raise
            await BoschCamera._async_camera_image_impl(cam)


# ── __init__.py async_fetch_live_snapshot_local ──────────────────────────


class TestFetchLiveSnapshotLocalValueError:
    """`BoschCameraCoordinator.async_fetch_live_snapshot_local` must catch
    ValueError from async_digest_request and return None instead of
    letting it propagate up the cloud-fetch caller chain.

    The function does PUT /connection → fetch snap.jpg via Digest. We
    mock the PUT to succeed and force async_digest_request to raise
    ValueError, then assert the function returns None (no propagation)."""

    def _put_resp_cm(self, status: int, body_json: dict):
        """Build a mock CM for session.put()."""
        import json as _json
        resp = MagicMock()
        resp.status = status
        resp.text = AsyncMock(return_value=_json.dumps(body_json))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    @pytest.mark.asyncio
    async def test_value_error_returns_none(self):
        from custom_components.bosch_shc_camera import (
            BoschCameraCoordinator,
        )
        coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
        coord._shc_state_cache = {}
        coord._refreshed_token = "tok"
        coord._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
        coord.get_quality_params = MagicMock(return_value=("720p", "low"))
        coord.hass = SimpleNamespace(data={})

        put_cm = self._put_resp_cm(
            200, {"user": "cbs-1", "password": "p", "urls": ["192.0.2.1:443"]},
        )
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_cm)
        # ClientSession(...) context manager:
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.aiohttp.ClientSession",
            return_value=session_cm,
        ), patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.async_digest_request",
            new=AsyncMock(side_effect=ValueError(
                "Server returned 401 without WWW-Authenticate header for "
                "'https://192.0.2.1:443/snap.jpg?JpegSize=1206'"
            )),
        ):
            out = await coord.async_fetch_live_snapshot_local(CAM_ID)
        assert out is None
