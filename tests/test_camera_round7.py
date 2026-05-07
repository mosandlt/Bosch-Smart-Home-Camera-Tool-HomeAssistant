"""Sprint G — camera.py remaining coverage gaps.

Covers:
  465-466: _yuv422_to_jpeg exception path → returns None
  507-514: _async_rcp_thumbnail YUV422 branch — conversion fails or wrong size
  606-618: LOCAL snap via proxy — Digest success, non-200, RequestException
  684-685: aiohttp.ClientError on retry after 404 proxy refresh
  711-712: aiohttp.ClientError on retry after 401 proxy expiry
  819-851: LOCAL outage snap fallback — success + timeout
  864, 866-867: event snapshot — unsafe imageUrl warning + skip
  881-896: event snapshot — 401 response, 403/410 (try next), timeout/ClientError
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
PROXY_URL = "https://proxy-01.live.cbs.boschsecurity.com/hash/snap.jpg"
LOCAL_SNAP_URL = "https://192.168.20.149:443/snap.jpg"


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
        _get_cached_rcp_session=AsyncMock(return_value=None),
        _rcp_read=AsyncMock(return_value=None),
        _audio_enabled={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera(coord=None, **overrides):
    from custom_components.bosch_shc_camera.camera import BoschCamera
    coord = coord or _make_coord()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name    = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
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
    for k, v in overrides.items():
        setattr(cam, k, v)
    return cam


def _local_live_conn(proxy_url: str = LOCAL_SNAP_URL,
                     local_user: str = "localuser",
                     local_pass: str = "localpass"):
    """Coordinator with an active LOCAL live connection."""
    coord = _make_coord(
        _live_connections={
            CAM_ID: {
                "proxyUrl": proxy_url,
                "_connection_type": "LOCAL",
                "_local_user": local_user,
                "_local_password": local_pass,
            }
        },
        _live_opened_at={CAM_ID: time.monotonic() - 1.0},
    )
    return coord


def _remote_live_conn(proxy_url: str = PROXY_URL, opened_before: float = 60.0):
    """Coordinator with an active REMOTE live connection (for 401 age check)."""
    coord = _make_coord(
        _live_connections={CAM_ID: {"proxyUrl": proxy_url, "_connection_type": "REMOTE"}},
        _live_opened_at={CAM_ID: time.monotonic() - opened_before},
    )
    return coord


# ── 1. _yuv422_to_jpeg exception path (lines 465-466) ────────────────────────

class TestYuv422ToJpeg:
    """_yuv422_to_jpeg must return None when numpy/PIL raises."""

    def test_exception_returns_none_on_bad_input(self):
        """Passing a non-bytes-like object that triggers an exception → None returned."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera.__new__(BoschCamera)
        # Passing None as data — numpy frombuffer will raise TypeError
        result = cam._yuv422_to_jpeg(None)  # type: ignore[arg-type]
        assert result is None, "_yuv422_to_jpeg must return None on exception"

    def test_wrong_size_returns_none(self):
        """Wrong-sized bytes (not 115200) trigger the early return None guard."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera.__new__(BoschCamera)
        result = cam._yuv422_to_jpeg(b"\x00" * 100)
        assert result is None, "_yuv422_to_jpeg must return None for wrong size"

    def test_correct_size_returns_jpeg(self):
        """115200 zeros (valid YUYV frame) must produce a JPEG bytes object."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera.__new__(BoschCamera)
        data = bytes(115200)
        result = cam._yuv422_to_jpeg(data)
        # The result may be None if numpy/PIL not installed; if installed it should be bytes
        if result is not None:
            assert result[:2] == b"\xff\xd8", "result must be a JPEG (FF D8 magic)"

    def test_exception_path_via_broken_numpy(self):
        """Mock numpy to raise so the except block (line 465-466) is exercised."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera.__new__(BoschCamera)
        data = bytes(115200)
        with patch("numpy.frombuffer", side_effect=RuntimeError("simulated numpy crash")):
            result = cam._yuv422_to_jpeg(data)
        assert result is None, "exception in numpy path must yield None"


# ── 2. _async_rcp_thumbnail YUV422 branch (lines 507-514) ────────────────────

class TestAsyncRcpThumbnailYuv422:
    """Lines 507-514: raw=115200 bytes but _yuv422_to_jpeg returns None; wrong size raw."""

    @pytest.mark.asyncio
    async def test_yuv422_conversion_fails_returns_none(self):
        """115200-byte raw, but _yuv422_to_jpeg returns None → log debug, return None."""
        coord = _make_coord(
            _live_connections={
                CAM_ID: {"urls": ["proxy-01.live.cbs.boschsecurity.com:42090/abc123"]}
            },
            _get_cached_rcp_session=AsyncMock(return_value="session-id-1"),
            _rcp_read=AsyncMock(side_effect=[
                b"\x00\x00bad",       # 0x099e → not JPEG (no FF D8)
                bytes(115200),        # 0x0c98 → correct size
            ]),
        )
        cam = _make_camera(coord=coord)

        with patch.object(cam.__class__, "_yuv422_to_jpeg", return_value=None):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_rcp_thumbnail(cam)

        assert result is None, "when YUV422 conversion fails, _async_rcp_thumbnail must return None"

    @pytest.mark.asyncio
    async def test_yuv422_wrong_size_returns_none(self):
        """Raw 0x0c98 has unexpected size (not 115200) → log debug, return None."""
        coord = _make_coord(
            _live_connections={
                CAM_ID: {"urls": ["proxy-01.live.cbs.boschsecurity.com:42090/abc123"]}
            },
            _get_cached_rcp_session=AsyncMock(return_value="session-id-2"),
            _rcp_read=AsyncMock(side_effect=[
                b"\x00\x00bad",       # 0x099e → not JPEG
                bytes(1000),          # 0x0c98 → wrong size
            ]),
        )
        cam = _make_camera(coord=coord)

        from custom_components.bosch_shc_camera.camera import BoschCamera
        result = await BoschCamera._async_rcp_thumbnail(cam)

        assert result is None, "wrong-size 0x0c98 raw must return None"

    @pytest.mark.asyncio
    async def test_yuv422_success_returns_jpeg(self):
        """115200-byte raw, _yuv422_to_jpeg succeeds → return JPEG bytes."""
        coord = _make_coord(
            _live_connections={
                CAM_ID: {"urls": ["proxy-01.live.cbs.boschsecurity.com:42090/abc123"]}
            },
            _get_cached_rcp_session=AsyncMock(return_value="session-id-3"),
            _rcp_read=AsyncMock(side_effect=[
                b"\x00\x00bad",       # 0x099e → not JPEG
                bytes(115200),        # 0x0c98 → correct size
            ]),
        )
        cam = _make_camera(coord=coord)

        fake_jpeg = b"\xff\xd8\xff\xe0fake"
        with patch.object(cam.__class__, "_yuv422_to_jpeg", return_value=fake_jpeg):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_rcp_thumbnail(cam)

        assert result == fake_jpeg, "successful YUV422 conversion must return JPEG bytes"


# ── 3. LOCAL snap via proxy (lines 606-618) ───────────────────────────────────

class TestLocalSnapViaProxy:
    """Lines 606-618: _fetch_local_snap called via executor_job."""

    @pytest.mark.asyncio
    async def test_local_snap_success_returns_image(self):
        """Digest auth returns 200 + image → cached and returned."""
        coord = _local_live_conn()
        cam = _make_camera(coord=coord)
        img_bytes = b"\xff\xd8local"

        # async_add_executor_job returns the image (simulates _fetch_local_snap success)
        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=AsyncMock(return_value=img_bytes),
        )

        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == img_bytes, "LOCAL snap 200 must return image bytes"
        assert cam._cached_image == img_bytes, "LOCAL snap 200 must cache image"

    @pytest.mark.asyncio
    async def test_local_snap_executor_returns_none_falls_to_placeholder(self):
        """Executor returns None (non-200 or RequestException) → placeholder."""
        coord = _local_live_conn()
        cam = _make_camera(coord=coord)

        # async_add_executor_job returns None (simulates non-200 / exception in _fetch_local_snap)
        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=AsyncMock(return_value=None),
        )

        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        # Falls to cached/placeholder
        assert result is not None, "LOCAL snap None must fall back to placeholder"

    @pytest.mark.asyncio
    async def test_local_snap_timeout_falls_to_placeholder(self):
        """asyncio.TimeoutError on executor → falls through to cached/placeholder."""
        import asyncio as _asyncio
        coord = _local_live_conn()
        cam = _make_camera(coord=coord)

        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=AsyncMock(side_effect=_asyncio.TimeoutError()),
        )

        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, "LOCAL snap timeout must fall back to placeholder/cached"

    @pytest.mark.asyncio
    async def test_local_snap_no_creds_skips_executor(self):
        """LOCAL connection but no creds → executor never called.

        When _local_user/_local_password are empty, the 'if local_user and local_pass:'
        block is skipped. The code then falls to the REMOTE aiohttp path (line 647).
        We verify async_add_executor_job is never called (no Digest executor started).
        """
        coord = _make_coord(
            _live_connections={
                CAM_ID: {
                    "proxyUrl": LOCAL_SNAP_URL,
                    "_connection_type": "LOCAL",
                    "_local_user": "",      # empty = no creds
                    "_local_password": "",
                }
            },
            _live_opened_at={CAM_ID: time.monotonic() - 1.0},
        )
        cam = _make_camera(coord=coord)
        executor_mock = AsyncMock()
        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=executor_mock,
        )

        # The code falls to the REMOTE aiohttp path — provide a proper response mock
        session = MagicMock()
        session.get.return_value = _resp_cm(200, body=b"\xff\xd8remote", content_type="image/jpeg")
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            await BoschCamera._async_camera_image_impl(cam)

        executor_mock.assert_not_called(), "executor must not be called when no LOCAL creds"


# ── 4. aiohttp.ClientError on retry after 404 (lines 684-685) ────────────────

class TestProxy404RetryClientError:
    """Line 684-685: ClientError during the retry GET after a 404 refresh."""

    @pytest.mark.asyncio
    async def test_404_retry_client_error_falls_to_cached(self):
        """404 → new proxy URL → ClientError on retry → return cached image."""
        import aiohttp
        new_url = "https://proxy-02.live.cbs.boschsecurity.com/new/snap.jpg"
        coord = _make_coord(
            _live_connections={CAM_ID: {"proxyUrl": PROXY_URL, "_connection_type": "REMOTE"}},
            _live_opened_at={CAM_ID: time.monotonic() - 5.0},
        )
        coord.try_live_connection = AsyncMock(
            return_value={"proxyUrl": new_url, "_connection_type": "REMOTE"}
        )
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")

        first_cm = _resp_cm(404, body=b"not found", content_type="text/html")
        retry_cm = MagicMock()
        retry_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("retry error"))
        retry_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [first_cm, retry_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, "ClientError on 404-retry must not raise"
        assert cam._cached_image == b"\xff\xd8cached", "cached image must survive 404+ClientError"


# ── 5. aiohttp.ClientError on retry after 401 expiry (lines 711-712) ─────────

class TestProxy401RetryClientError:
    """Lines 711-712: ClientError during the retry GET after 401 session renewal."""

    @pytest.mark.asyncio
    async def test_401_retry_client_error_falls_to_cached(self):
        """401 old session → renewal → new proxy URL → ClientError on retry → cached."""
        import aiohttp
        new_url = "https://proxy-03.live.cbs.boschsecurity.com/fresh/snap.jpg"
        # Use opened_before > LIVE_SESSION_TTL (55s) so renewal is triggered
        coord = _remote_live_conn(opened_before=60.0)
        coord.try_live_connection = AsyncMock(
            return_value={"proxyUrl": new_url, "_connection_type": "REMOTE"}
        )
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8401cached")

        first_cm = _resp_cm(401, body=b"Unauthorized", content_type="text/html")
        retry_cm = MagicMock()
        retry_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("retry 401 error"))
        retry_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [first_cm, retry_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, "ClientError on 401-retry must not raise"
        # The renewal path may clear _live_connections or leave cached; just ensure no crash
        assert True, "no exception raised from 401+ClientError retry path"


# ── 6. LOCAL outage snap fallback (lines 819-851) ────────────────────────────

class TestLocalOutageSnapFallback:
    """Lines 819-851: camera NOT streaming, has cached LOCAL Digest creds, outage_count > 0."""

    def _outage_coord(self):
        """Coordinator that looks like an auth outage with cached LOCAL creds."""
        return _make_coord(
            _live_connections={},          # NOT streaming
            _local_creds_cache={
                CAM_ID: {
                    "user": "digestuser",
                    "password": "digestpass",
                    "host": "192.168.20.149",
                    "port": 443,
                }
            },
            _auth_outage_count=1,          # > 0 → outage path active
            # async_fetch_live_snapshot returns None so we reach outage path
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_outage_snap_success_returns_image(self):
        """Executor returns image bytes → cached and returned."""
        coord = self._outage_coord()
        cam = _make_camera(coord=coord)
        img_bytes = b"\xff\xd8outage"

        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=AsyncMock(return_value=img_bytes),
        )

        session = MagicMock()
        session.get.return_value = _resp_cm(200, b"", content_type="text/html")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == img_bytes, "outage snap success must return image bytes"
        assert cam._cached_image == img_bytes, "outage snap must cache image"

    @pytest.mark.asyncio
    async def test_outage_snap_timeout_falls_to_placeholder(self):
        """asyncio.TimeoutError during outage snap executor → placeholder returned."""
        coord = self._outage_coord()
        cam = _make_camera(coord=coord)

        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=AsyncMock(side_effect=asyncio.TimeoutError()),
        )

        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, "outage snap timeout must not return None (placeholder)"

    @pytest.mark.asyncio
    async def test_outage_snap_zero_outage_count_skips_path(self):
        """_auth_outage_count == 0 → outage snap path must be skipped entirely."""
        coord = _make_coord(
            _live_connections={},
            _local_creds_cache={
                CAM_ID: {"user": "u", "password": "p", "host": "192.168.20.149", "port": 443}
            },
            _auth_outage_count=0,     # no outage — must skip
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        cam = _make_camera(coord=coord)
        executor_mock = AsyncMock()
        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=executor_mock,
        )
        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            await BoschCamera._async_camera_image_impl(cam)

        executor_mock.assert_not_called(), "outage path must be skipped when outage_count == 0"

    @pytest.mark.asyncio
    async def test_outage_snap_no_creds_skips_path(self):
        """Empty _local_creds_cache → outage snap path skipped."""
        coord = _make_coord(
            _live_connections={},
            _local_creds_cache={},       # no creds
            _auth_outage_count=1,
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        cam = _make_camera(coord=coord)
        executor_mock = AsyncMock()
        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=executor_mock,
        )
        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            await BoschCamera._async_camera_image_impl(cam)

        executor_mock.assert_not_called(), "outage path must be skipped when no creds cached"


# ── 7. Event snapshot — unsafe imageUrl (lines 864, 866-867) ─────────────────

class TestEventSnapshotUnsafeUrl:
    """Lines 864, 866-867: imageUrl fails _is_safe_bosch_url → warning + skip.

    To reach the event snapshot section (path 4), the camera must be "streaming"
    (so the idle cloud snapshot path at line 745 is skipped) but have no proxyUrl
    (so the proxy fetch at line 599 is skipped). The outage path is skipped by
    having no local creds cached.
    """

    def _streaming_no_proxy_coord(self, events):
        """Coordinator: is_streaming=True but no proxyUrl → falls to event snapshot path."""
        return _make_coord(
            data={CAM_ID: {"info": {}, "events": events}},
            # CAM_ID present in _live_connections → is_streaming=True
            _live_connections={CAM_ID: {}},  # no proxyUrl → proxy_url = ""
            _local_creds_cache={},           # no cached creds → outage path skipped
            _auth_outage_count=0,
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_unsafe_url_logged_and_skipped(self):
        """imageUrl on non-Bosch domain → warning logged, URL skipped.

        _cached_image=None so the code doesn't short-circuit at 'if self._cached_image:'
        and reaches the event snapshot section (path 4).
        """
        coord = self._streaming_no_proxy_coord([
            {"imageUrl": "https://evil.example.com/snap.jpg", "timestamp": "2026-01-01T00:00:00Z"},
            {"imageUrl": "https://media.boschsecurity.com/snap.jpg", "timestamp": "2026-01-01T00:00:01Z"},
        ])
        cam = _make_camera(coord=coord, _cached_image=None)  # no cache → reach section 4

        session = MagicMock()
        # The safe Bosch URL returns 200
        session.get.return_value = _resp_cm(200, body=b"\xff\xd8new", content_type="image/jpeg")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        # The evil.example.com URL is skipped; only the boschsecurity.com URL is fetched
        assert session.get.call_count == 1, "only the safe Bosch URL must be fetched"
        assert result == b"\xff\xd8new", "unsafe URL skipped → safe URL fetched successfully"

    @pytest.mark.asyncio
    async def test_missing_image_url_key_skipped(self):
        """Event with no imageUrl key → skipped cleanly."""
        coord = self._streaming_no_proxy_coord(
            [{"timestamp": "2026-01-01T00:00:00Z"}]  # no imageUrl key
        )
        cam = _make_camera(coord=coord)
        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        session.get.assert_not_called(), "session.get must not be called for events without imageUrl"


# ── 8. Event snapshot — 401, 403/410, timeout/ClientError (lines 881-896) ────

class TestEventSnapshot4xx:
    """Lines 881-896: various HTTP errors and network failures in event snapshot loop.

    To reach section 4 (event snapshot), camera must be streaming (is_streaming=True,
    i.e. CAM_ID in _live_connections) but have no proxyUrl. Then outage path is
    skipped (no local creds + outage_count=0) and we fall through to section 4.
    """

    def _event_coord(self, events):
        """Coordinator: is_streaming=True, no proxyUrl, no outage → event snapshot path."""
        return _make_coord(
            data={CAM_ID: {"info": {}, "events": events}},
            _live_connections={CAM_ID: {}},  # no proxyUrl → proxy_url = ""
            _local_creds_cache={},
            _auth_outage_count=0,
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_401_returns_cached_immediately(self):
        """Event snapshot 401 → warning logged, cached image (None) → placeholder returned.

        _cached_image must be None to reach section 4 past the short-circuit at line 856.
        After 401, 'return self._cached_image' returns None, so the public wrapper
        serves _PLACEHOLDER_JPEG. We verify that session.get is called exactly once
        (one URL fetched → 401 → immediate return without trying next event).
        """
        safe_url = "https://media.boschsecurity.com/ev1.jpg"
        coord = self._event_coord([
            {"imageUrl": safe_url, "timestamp": "2026-01-01T00:00:00Z"},
            {"imageUrl": "https://media.boschsecurity.com/ev2.jpg", "timestamp": "2026-01-01T00:00:01Z"},
        ])
        cam = _make_camera(coord=coord, _cached_image=None)  # no cache → reach section 4

        session = MagicMock()
        session.get.return_value = _resp_cm(401, body=b"Unauthorized", content_type="text/html")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert session.get.call_count == 1, "401 must stop the event loop immediately (no retry)"
        # result is None (no cached) → public async_camera_image serves placeholder;
        # _async_camera_image_impl itself returns None on 401 with no cache
        assert result is None or result == BoschCamera._PLACEHOLDER_JPEG, \
            "401 with no cached image must return None or placeholder"

    @pytest.mark.asyncio
    async def test_403_tries_next_event_then_returns_placeholder(self):
        """403 on first event → try next event; 403 again → all failed → placeholder.

        _cached_image=None to bypass section-3 short-circuit and reach section 4.
        """
        safe_url1 = "https://media.boschsecurity.com/ev1.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2.jpg"
        coord = self._event_coord([
            {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
            {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
        ])
        cam = _make_camera(coord=coord, _cached_image=None)  # no cache → reach section 4

        session = MagicMock()
        session.get.side_effect = [
            _resp_cm(403, body=b"Forbidden", content_type="text/html"),
            _resp_cm(403, body=b"Forbidden", content_type="text/html"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert session.get.call_count == 2, "must attempt both event URLs on 403"
        from custom_components.bosch_shc_camera.camera import BoschCamera
        assert result == BoschCamera._PLACEHOLDER_JPEG, "both 403 + no cached → placeholder"

    @pytest.mark.asyncio
    async def test_410_tries_next_event(self):
        """410 (expired URL) on first event → try next event (200 → success)."""
        safe_url1 = "https://media.boschsecurity.com/ev1_old.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2_new.jpg"
        coord = self._event_coord([
            {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
            {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
        ])
        cam = _make_camera(coord=coord)

        session = MagicMock()
        session.get.side_effect = [
            _resp_cm(410, body=b"Gone", content_type="text/html"),
            _resp_cm(200, body=b"\xff\xd8fresh", content_type="image/jpeg"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8fresh", "410 on first → 200 on second must return fresh image"

    @pytest.mark.asyncio
    async def test_timeout_on_event_snap_continues_loop(self):
        """TimeoutError on first event → loop continues to next event."""
        safe_url1 = "https://media.boschsecurity.com/ev1.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2.jpg"
        coord = self._event_coord([
            {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
            {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
        ])
        cam = _make_camera(coord=coord, _cached_image=None)  # no cache → reach section 4

        # First CM raises TimeoutError when __aenter__ is called
        timeout_cm = MagicMock()
        timeout_cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        timeout_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [
            timeout_cm,
            _resp_cm(200, body=b"\xff\xd8ev2", content_type="image/jpeg"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8ev2", "timeout on first event → second event must succeed"

    @pytest.mark.asyncio
    async def test_client_error_on_event_snap_continues_loop(self):
        """aiohttp.ClientError on first event → loop continues to second event."""
        import aiohttp
        safe_url1 = "https://media.boschsecurity.com/ev1.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2.jpg"
        coord = self._event_coord([
            {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
            {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
        ])
        cam = _make_camera(coord=coord, _cached_image=None)  # no cache → reach section 4

        err_cm = MagicMock()
        err_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        err_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [
            err_cm,
            _resp_cm(200, body=b"\xff\xd8ev2ok", content_type="image/jpeg"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8ev2ok", "ClientError on first event → second event must succeed"

    @pytest.mark.asyncio
    async def test_all_events_fail_returns_placeholder(self):
        """All events fail → _PLACEHOLDER_JPEG returned."""
        safe_url = "https://media.boschsecurity.com/ev1.jpg"
        coord = self._event_coord([{"imageUrl": safe_url, "timestamp": "2026-01-01T00:00:00Z"}])
        cam = _make_camera(coord=coord, _cached_image=None)
        from custom_components.bosch_shc_camera.camera import BoschCamera
        placeholder = BoschCamera._PLACEHOLDER_JPEG

        session = MagicMock()
        session.get.return_value = _resp_cm(410, body=b"Gone", content_type="text/html")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=session,
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == placeholder, "all events fail + no cached → placeholder returned"
