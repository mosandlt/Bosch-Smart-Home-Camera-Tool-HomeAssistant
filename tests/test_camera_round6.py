"""Sprint F — camera.py REMOTE proxy snapshot branches + idle cloud snapshot.

Covers _async_camera_image_impl lines 646-898:
  646-659: 200 + image content-type → cache and return
  660-684: 404 → try_live_connection → retry → 200 → return
  685-723: 401/403 age check → keep or renew session
  724-730: TimeoutError → _async_rcp_thumbnail fallback
  732-807: idle camera cloud snapshot (no proxy URL)
  808-898: outage fallback, cached image, event snapshot last resort

These tests use no real HA runtime, no network. Stub aiohttp sessions
via async context-manager mocks. Bind _async_camera_image_impl via
BoschCamera.__new__ (same pattern as test_camera_image_impl.py).
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
PROXY_URL = "https://proxy-01.live.cbs.boschsecurity.com/hash/snap.jpg"
LIVE_SESSION_TTL = 55  # mirrors const.py value


# ── helpers ───────────────────────────────────────────────────────────────────

def _resp_cm(status: int, body: bytes = b"", content_type: str = "image/jpeg"):
    """Async context-manager mock for session.get()."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"},
                "events": [],
            },
        },
        _live_connections={},
        _live_opened_at={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        _shc_state_cache={},
        _stream_warming=set(),
        _image_rotation_180={},
        _local_creds_cache={},
        _auth_outage_count=0,
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera(coord=None, **camera_overrides):
    """Instantiate BoschCamera without calling __init__."""
    from custom_components.bosch_shc_camera.camera import BoschCamera
    coord = coord or _make_coord()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = "Bosch Terrasse"
    cam._cached_image = None
    cam._force_image_refresh = False
    cam._last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor II"
    cam._hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "64:da:a0:33:14:ae"
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
        async_add_executor_job=AsyncMock(return_value=None),
    )
    for k, v in camera_overrides.items():
        setattr(cam, k, v)
    return cam


def _live_conn(proxy_url: str = PROXY_URL, opened_before: float = 1.0):
    """Return a coordinator with an active REMOTE live connection."""
    coord = _make_coord(
        _live_connections={CAM_ID: {"proxyUrl": proxy_url, "_connection_type": "REMOTE"}},
        _live_opened_at={CAM_ID: time.monotonic() - opened_before},
    )
    return coord


# ── 1. 200 + image/jpeg → cache and return ───────────────────────────────────

class TestRemoteProxy200:
    """Lines 646-659: successful snap.jpg fetch from REMOTE proxy.

    The happy path that should run on every streaming camera tick.
    Pin: cached_image + _last_image_fetch must be updated and the bytes returned.
    """

    @pytest.mark.asyncio
    async def test_200_image_jpeg_caches_and_returns(self):
        """HTTP 200 + image/jpeg → store in _cached_image, update timestamp, return bytes."""
        coord = _live_conn()
        cam = _make_camera(coord=coord)
        session = MagicMock()
        session.get.return_value = _resp_cm(200, body=b"\xff\xd8img", content_type="image/jpeg")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8img", "must return the fetched bytes on 200"
        assert cam._cached_image == b"\xff\xd8img", "_cached_image must be updated on 200"
        assert cam._last_image_fetch > 0, "_last_image_fetch must be set on 200"

    @pytest.mark.asyncio
    async def test_200_wrong_content_type_falls_through(self):
        """HTTP 200 + text/html (expired proxy page) → do not cache, fall through."""
        coord = _live_conn()
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8old")
        session = MagicMock()
        session.get.return_value = _resp_cm(200, body=b"<html>", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        # Falls through; final return is cached image
        assert cam._cached_image == b"\xff\xd8old", "must NOT overwrite cached on text/html 200"

    @pytest.mark.asyncio
    async def test_200_empty_body_falls_through(self):
        """HTTP 200 + image/jpeg but empty body → guard `if data:` must skip cache update."""
        coord = _live_conn()
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8old")
        session = MagicMock()
        session.get.return_value = _resp_cm(200, body=b"", content_type="image/jpeg")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert cam._cached_image == b"\xff\xd8old", "empty body must not overwrite cached image"


# ── 2. 404 → try_live_connection ─────────────────────────────────────────────

class TestRemoteProxy404:
    """Lines 660-684: proxy URL expired → refresh connection and retry.

    When the proxy session has expired (Bosch's proxy-NNs are ephemeral),
    the snap.jpg returns 404. We call try_live_connection to get a fresh
    proxyUrl and retry immediately.
    """

    @pytest.mark.asyncio
    async def test_404_then_new_url_then_200_returns_image(self):
        """404 → try_live_connection gives new URL → retry GET 200 → return bytes."""
        coord = _live_conn()
        new_url = "https://proxy-02.live.cbs.boschsecurity.com/new-hash/snap.jpg"
        coord.try_live_connection = AsyncMock(
            return_value={"proxyUrl": new_url, "_connection_type": "REMOTE"}
        )
        cam = _make_camera(coord=coord)

        # First GET → 404, second GET (new_url) → 200
        first_cm = _resp_cm(404, body=b"Not Found", content_type="text/html")
        second_cm = _resp_cm(200, body=b"\xff\xd8fresh", content_type="image/jpeg")
        session = MagicMock()
        session.get.side_effect = [first_cm, second_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8fresh", "must return fresh bytes after 404 → renew → 200"
        assert cam._cached_image == b"\xff\xd8fresh", "must cache the fresh bytes"
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_404_try_live_connection_returns_none_falls_through(self):
        """404 → try_live_connection returns None → no retry, fall through to cached."""
        coord = _live_conn()
        coord.try_live_connection = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(404, body=b"", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.try_live_connection.assert_awaited_once(), "try_live_connection must be called on 404"
        # Falls through to cached image return
        assert out == b"\xff\xd8cached", "must return cached when try_live_connection fails"

    @pytest.mark.asyncio
    async def test_404_new_live_has_no_proxy_url_falls_through(self):
        """404 → try_live_connection returns dict without proxyUrl → skip retry."""
        coord = _live_conn()
        coord.try_live_connection = AsyncMock(return_value={"_connection_type": "REMOTE"})
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(404, body=b"", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        # Only 1 GET call (the initial 404), no retry
        assert session.get.call_count == 1, "must not retry when new live has no proxyUrl"


# ── 3. 401/403 session age checks ────────────────────────────────────────────

class TestRemoteProxy401:
    """Lines 685-723: 401/403 with age < TTL → keep session;
    age >= TTL → renew or clear.

    CAMERA_360 always returns 401 on its REMOTE snap.jpg — we must NOT
    renew / clear the session just because snap.jpg needs auth; we keep
    the session alive so the stream switch shows correct state.
    """

    @pytest.mark.asyncio
    async def test_401_age_below_ttl_keeps_session(self):
        """401 with session age < LIVE_SESSION_TTL → do nothing, return cached.
        This is the CAMERA_360 steady-state: snap.jpg always 401 but stream is alive.
        """
        coord = _live_conn(opened_before=5.0)  # only 5s old — well below TTL
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(401, body=b"Unauthorized", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.try_live_connection.assert_not_awaited(), "must NOT renew when age < LIVE_SESSION_TTL"
        assert CAM_ID in coord._live_connections, "must NOT clear _live_connections on young 401"

    @pytest.mark.asyncio
    async def test_403_age_below_ttl_keeps_session(self):
        """403 with young session → same keep-alive logic as 401."""
        coord = _live_conn(opened_before=5.0)
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(403, body=b"Forbidden", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.try_live_connection.assert_not_awaited(), "must NOT renew on young 403"
        assert CAM_ID in coord._live_connections, "_live_connections must survive young 403"

    @pytest.mark.asyncio
    async def test_401_age_above_ttl_renews_and_returns_fresh(self):
        """401 with session age >= LIVE_SESSION_TTL → renew → retry → 200 → return."""
        # Session is 60s old — past the 55s TTL
        coord = _live_conn(opened_before=60.0)
        new_url = "https://proxy-99.live.cbs.boschsecurity.com/newhash/snap.jpg"
        coord.try_live_connection = AsyncMock(
            return_value={"proxyUrl": new_url, "_connection_type": "REMOTE"}
        )
        cam = _make_camera(coord=coord)

        first_cm = _resp_cm(401, body=b"Unauthorized", content_type="text/html")
        retry_cm = _resp_cm(200, body=b"\xff\xd8renewed", content_type="image/jpeg")
        session = MagicMock()
        session.get.side_effect = [first_cm, retry_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        assert out == b"\xff\xd8renewed", "must return fresh bytes after successful renewal"

    @pytest.mark.asyncio
    async def test_401_age_above_ttl_renewal_fails_clears_connection(self):
        """401 + expired + try_live_connection returns None → clear _live_connections.
        Pin: is_streaming must become False after clearing so the card shows correct state.
        """
        coord = _live_conn(opened_before=70.0)
        coord.try_live_connection = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8old")

        session = MagicMock()
        session.get.return_value = _resp_cm(401, body=b"Unauthorized", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert CAM_ID not in coord._live_connections, \
            "must clear _live_connections when renewal fails (so is_streaming → False)"
        assert CAM_ID not in coord._live_opened_at, \
            "must also clear _live_opened_at when renewal fails"


# ── 4. TimeoutError / ClientError → RCP thumbnail fallback ───────────────────

class TestRemoteProxyTimeout:
    """Lines 724-730: network error on snap.jpg → try RCP thumbnail.

    Observed: good LAN but proxy-NN is slow/unreachable → timeout after 10s.
    RCP 0x099e is much faster (~100ms) and served via the same proxy hash.
    """

    @pytest.mark.asyncio
    async def test_timeout_tries_rcp_thumbnail_and_returns(self):
        """TimeoutError → _async_rcp_thumbnail returns bytes → cache and return."""
        coord = _live_conn()
        cam = _make_camera(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp")

        session = MagicMock()
        session.get.side_effect = asyncio.TimeoutError()

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        cam._async_rcp_thumbnail.assert_awaited_once(), "must try RCP thumbnail on TimeoutError"
        assert out == b"\xff\xd8rcp", "must return RCP thumbnail bytes on snap.jpg timeout"
        assert cam._cached_image == b"\xff\xd8rcp", "must cache RCP thumbnail bytes"

    @pytest.mark.asyncio
    async def test_timeout_rcp_thumbnail_none_falls_through(self):
        """TimeoutError → _async_rcp_thumbnail returns None → fall through to idle path."""
        coord = _live_conn()
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8old")
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = asyncio.TimeoutError()

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        cam._async_rcp_thumbnail.assert_awaited_once(), "must attempt RCP on timeout"
        # Falls through; since streaming (live connection exists), skips idle path
        # and reaches cached image fallback

    @pytest.mark.asyncio
    async def test_aiohttp_client_error_tries_rcp(self):
        """aiohttp.ClientError → same RCP thumbnail fallback as TimeoutError."""
        import aiohttp
        coord = _live_conn()
        cam = _make_camera(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp")

        session = MagicMock()
        session.get.side_effect = aiohttp.ClientError("connection reset")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8rcp", "ClientError must also fall back to RCP thumbnail"


# ── 5. _async_rcp_thumbnail ───────────────────────────────────────────────────

class TestAsyncRcpThumbnail:
    """Lines 476-522: RCP thumbnail implementation.

    Tests the early-exit paths (no urls, bad url format, no session)
    and the JPEG-first vs YUV422 fallback logic.
    """

    @pytest.mark.asyncio
    async def test_no_urls_in_live_returns_none(self):
        """No 'urls' key in live connection → return None immediately."""
        coord = _make_coord(
            _live_connections={CAM_ID: {"proxyUrl": PROXY_URL, "_connection_type": "REMOTE"}},
        )
        cam = _make_camera(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when no 'urls' in live connection"

    @pytest.mark.asyncio
    async def test_no_live_connection_returns_none(self):
        """No live connection at all → return None."""
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when _live_connections is empty"

    @pytest.mark.asyncio
    async def test_bad_url_format_no_slash_returns_none(self):
        """urls[0] without '/' → len(parts) != 2 → return None."""
        coord = _make_coord(
            _live_connections={CAM_ID: {"urls": ["noslash"], "_connection_type": "REMOTE"}},
        )
        cam = _make_camera(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when url[0] has no '/' separator"

    @pytest.mark.asyncio
    async def test_no_rcp_session_returns_none(self):
        """_get_cached_rcp_session returns None → return None (no session)."""
        coord = _make_coord(
            _live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord._get_cached_rcp_session = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when no RCP session available"

    @pytest.mark.asyncio
    async def test_rcp_0x099e_jpeg_returned_directly(self):
        """_rcp_read returns JPEG bytes (starts with 0xFFD8) → return them directly."""
        coord = _make_coord(
            _live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord._get_cached_rcp_session = AsyncMock(return_value="sess-id-1")
        coord._rcp_read = AsyncMock(return_value=b"\xff\xd8jpeg-data")
        cam = _make_camera(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out == b"\xff\xd8jpeg-data", "must return JPEG directly from RCP 0x099e"

    @pytest.mark.asyncio
    async def test_rcp_0x099e_not_jpeg_falls_to_yuv422(self):
        """0x099e not JPEG → fall through to 0x0c98 YUV422 path."""
        coord = _make_coord(
            _live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord._get_cached_rcp_session = AsyncMock(return_value="sess-id-1")
        # First call (0x099e) returns non-JPEG; second (0x0c98) returns None
        coord._rcp_read = AsyncMock(side_effect=[b"\x00\x00not-jpeg", None])
        cam = _make_camera(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when neither 0x099e nor 0x0c98 yields usable data"
        assert coord._rcp_read.await_count == 2, "must attempt both RCP registers"

    @pytest.mark.asyncio
    async def test_rcp_0x0c98_wrong_size_returns_none(self):
        """0x0c98 returns data but not 115200 bytes → return None."""
        coord = _make_coord(
            _live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord._get_cached_rcp_session = AsyncMock(return_value="sess-id-1")
        # First call not-JPEG; second wrong size
        coord._rcp_read = AsyncMock(side_effect=[b"\x00\x00", b"\xab" * 1000])
        cam = _make_camera(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when 0x0c98 is unexpected size"


# ── 6. Idle camera cloud snapshot ────────────────────────────────────────────

class TestIdleCameraCloudSnapshot:
    """Lines 732-807: cloud snapshot for cameras not currently streaming.

    Two sub-modes:
    a) no cached image → fetch synchronously (cold start)
    b) cached image but stale → re-fetch synchronously

    The prefer_small path (width <= 640) tries RCP thumbnail first.
    """

    @pytest.mark.asyncio
    async def test_no_cache_fetches_via_async_fetch_live_snapshot(self):
        """No cached image → call async_fetch_live_snapshot → cache and return."""
        coord = _make_coord()  # no _live_connections → not streaming
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8snap")
        cam = _make_camera(coord=coord)
        # _cached_image=None (default)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        assert out == b"\xff\xd8snap", "must return snapshot from async_fetch_live_snapshot"
        assert cam._cached_image == b"\xff\xd8snap", "must cache the snapshot"

    @pytest.mark.asyncio
    async def test_no_cache_prefer_small_tries_rcp_first(self):
        """width=320 (prefer_small) + no cache → try RCP thumbnail before slow proxy."""
        coord = _make_coord()
        cam = _make_camera(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp-small")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam, width=320)

        cam._async_rcp_thumbnail.assert_awaited_once(), "must try RCP first on prefer_small"
        assert out == b"\xff\xd8rcp-small", "must return RCP thumbnail on prefer_small"

    @pytest.mark.asyncio
    async def test_no_cache_prefer_small_rcp_fails_falls_to_snap(self):
        """prefer_small + RCP returns None → fall through to async_fetch_live_snapshot."""
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8snap")
        cam = _make_camera(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam, width=320)

        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        assert out == b"\xff\xd8snap", "must fall to async_fetch_live_snapshot when RCP fails"

    @pytest.mark.asyncio
    async def test_no_cache_remote_401_tries_local_fallback(self):
        """async_fetch_live_snapshot returns None → try async_fetch_live_snapshot_local."""
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=b"\xff\xd8local")
        cam = _make_camera(coord=coord)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.async_fetch_live_snapshot_local.assert_awaited_once_with(CAM_ID)
        assert out == b"\xff\xd8local", "must use LOCAL fallback when REMOTE snap returns None"

    @pytest.mark.asyncio
    async def test_stale_cache_fetches_fresh(self):
        """Cache older than CLOUD_SNAP_CACHE_TTL → fetch fresh and return."""
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8fresh")
        cam = _make_camera(
            coord=coord,
            _cached_image=b"\xff\xd8stale",
            _last_image_fetch=time.monotonic() - 60,  # 60s ago — past the 30s TTL
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        assert out == b"\xff\xd8fresh", "must return fresh bytes when cache is stale"
        assert cam._cached_image == b"\xff\xd8fresh", "must update cache with fresh bytes"

    @pytest.mark.asyncio
    async def test_stale_cache_prefer_small_tries_rcp_first(self):
        """Stale cache + prefer_small → try RCP thumbnail before slow proxy."""
        coord = _make_coord()
        cam = _make_camera(
            coord=coord,
            _cached_image=b"\xff\xd8stale",
            _last_image_fetch=time.monotonic() - 60,
        )
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp-fresh")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam, width=400)

        cam._async_rcp_thumbnail.assert_awaited_once()
        assert out == b"\xff\xd8rcp-fresh", "stale cache + prefer_small must use RCP fresh"

    @pytest.mark.asyncio
    async def test_fresh_cache_returns_without_fetch(self):
        """Cache fresh (< CLOUD_SNAP_CACHE_TTL) → return cached without any network call."""
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8should-not")
        cam = _make_camera(
            coord=coord,
            _cached_image=b"\xff\xd8cached",
            _last_image_fetch=time.monotonic() - 5,  # only 5s old
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        coord.async_fetch_live_snapshot.assert_not_awaited(), \
            "must NOT fetch when cache is still fresh"
        assert out == b"\xff\xd8cached", "must return cached image when fresh"

    @pytest.mark.asyncio
    async def test_stale_both_fail_advances_timestamp_returns_cached(self):
        """Stale cache + both REMOTE and LOCAL return None → advance _last_image_fetch, return cached."""
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        before = time.monotonic() - 60
        cam = _make_camera(
            coord=coord,
            _cached_image=b"\xff\xd8old",
            _last_image_fetch=before,
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert cam._last_image_fetch > before, \
            "must advance _last_image_fetch so next tick retries instead of looping"
        assert out == b"\xff\xd8old", "must return stale cached image when both fetches fail"


# ── 7. Event snapshot last resort ─────────────────────────────────────────────

class TestEventSnapshotLastResort:
    """Lines 858-898: when all other methods fail, try event imageUrl.

    This is the startup scenario before any cloud fetch has completed.
    """

    @pytest.mark.asyncio
    async def test_event_image_url_fetched_and_cached(self):
        """Last resort: event imageUrl 200 → cache and return."""
        img_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [{
                        "imageUrl": img_url,
                        "timestamp": "2026-05-07T10:00:00.000Z",
                    }],
                }
            }
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord)  # _cached_image=None

        img_resp = _resp_cm(200, body=b"\xff\xd8event-img", content_type="image/jpeg")
        snap_resp = _resp_cm(404, body=b"", content_type="text/html")
        session = MagicMock()
        # The session.get may be called: first for snap fallback (not streaming → idle path),
        # then for event URL. We set side_effect as a list for reliability.
        session.get.return_value = img_resp

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        # async_fetch_live_snapshot was tried and returned None; then event path ran
        assert out == b"\xff\xd8event-img", "must return event imageUrl bytes as last resort"
        assert cam._cached_image == b"\xff\xd8event-img", "must cache event image"

    @pytest.mark.asyncio
    async def test_unsafe_image_url_rejected(self):
        """imageUrl that is not a Bosch HTTPS URL must be rejected (SSRF prevention)."""
        img_url = "http://evil.com/steal.jpg"
        coord = _make_coord(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [{"imageUrl": img_url, "timestamp": "2026-05-07T10:00:00.000Z"}],
                }
            }
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(200, body=b"\xff\xd8evil", content_type="image/jpeg")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out != b"\xff\xd8evil", "must NOT fetch unsafe (non-Bosch) imageUrl"

    @pytest.mark.asyncio
    async def test_event_401_returns_cached(self):
        """Event imageUrl returns 401 (expired token) → return cached, no further retries."""
        img_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [{"imageUrl": img_url, "timestamp": "2026-05-07T10:00:00.000Z"}],
                }
            }
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(401, body=b"Unauth", content_type="text/html")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8cached", "401 on event imageUrl must return cached"

    @pytest.mark.asyncio
    async def test_no_events_no_cache_returns_placeholder(self):
        """No events, no cache → return PLACEHOLDER_JPEG."""
        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        cam = _make_camera(coord=coord)  # _cached_image=None

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            out = await BoschCamera._async_camera_image_impl(cam)

        from custom_components.bosch_shc_camera.camera import BoschCamera as BC
        assert out == BC._PLACEHOLDER_JPEG, \
            "must return PLACEHOLDER_JPEG when all fetch methods fail and no cache"
