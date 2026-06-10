"""Tests for camera.py `async_camera_image` wrapper + `_async_camera_image_impl`.

Round-2 follow-up. Targets the snapshot-fetch path that other tests
skipped because it's tangled with aiohttp + requests + executor calls.

Focuses on the high-confidence wrapper contract and a handful of impl
branches that don't need a live network:

  - `async_camera_image` (lines 525-556) — exception swallow, 180°
    rotation hook, placeholder fallback, CancelledError propagate.
  - `_async_camera_image_impl` — early exits when no live connection,
    LOCAL-with-no-creds skip, REMOTE proxyUrl 404 / 401-with-fresh-session
    branches via mocked sessions.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "X"},
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
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
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


# ── async_camera_image wrapper (525-556) ─────────────────────────────────


class TestAsyncCameraImageWrapper:
    """The public entrypoint that HA's camera proxy calls. Wraps the
    complex `_async_camera_image_impl` so any uncaught exception still
    yields a valid JPEG instead of HTTP 500 (which Lovelace's <img>
    renders as a brown text-bytes-as-pixels error frame)."""

    @pytest.mark.asyncio
    async def test_returns_impl_result_when_present(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera()
        cam._async_camera_image_impl = AsyncMock(return_value=b"\xff\xd8live-img")
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8live-img"

    @pytest.mark.asyncio
    async def test_returns_placeholder_when_impl_returns_none(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera()
        cam._async_camera_image_impl = AsyncMock(return_value=None)
        out = await BoschCamera.async_camera_image(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG

    @pytest.mark.asyncio
    async def test_returns_cached_when_impl_raises(self):
        """Observed 2026-04-27: unhandled exception in impl propagated up
        and HA returned 26-byte text 500 body. Lovelace rendered that as
        a brown error frame on every camera card sharing the broken
        endpoint. Pin: any non-CancelledError exception must surface
        the cached JPEG instead."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera(_cached_image=b"\xff\xd8cached")
        cam._async_camera_image_impl = AsyncMock(side_effect=RuntimeError("oops"))
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_returns_placeholder_when_impl_raises_and_no_cache(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera()  # _cached_image=None
        cam._async_camera_image_impl = AsyncMock(side_effect=RuntimeError("oops"))
        out = await BoschCamera.async_camera_image(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        """CancelledError must propagate cleanly so HA's outer-task
        cancellation (timeout, shutdown) isn't swallowed."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera()
        cam._async_camera_image_impl = AsyncMock(
            side_effect=asyncio.CancelledError(),
        )
        with pytest.raises(asyncio.CancelledError):
            await BoschCamera.async_camera_image(cam)

    @pytest.mark.asyncio
    async def test_rotation_applied_when_enabled(self):
        """Bild 180° drehen switch ON → rotate the JPEG via executor.
        Pin so a refactor of the rotation hook can't silently drop it
        (the indoor cams Thomas has on the ceiling rely on this)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(_image_rotation_180={CAM_ID: True})
        cam = _make_camera(coord=coord)
        cam._async_camera_image_impl = AsyncMock(return_value=b"\xff\xd8orig")
        cam.hass.async_add_executor_job = AsyncMock(return_value=b"\xff\xd8rotated")
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8rotated"
        cam.hass.async_add_executor_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rotation_skipped_for_placeholder(self):
        """Don't waste an executor round-trip rotating the 1×1 black
        placeholder — there's nothing meaningful to rotate."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(_image_rotation_180={CAM_ID: True})
        cam = _make_camera(coord=coord)
        cam._async_camera_image_impl = AsyncMock(return_value=None)
        out = await BoschCamera.async_camera_image(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG
        cam.hass.async_add_executor_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_disabled_no_executor_call(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(_image_rotation_180={CAM_ID: False})
        cam = _make_camera(coord=coord)
        cam._async_camera_image_impl = AsyncMock(return_value=b"\xff\xd8orig")
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8orig"
        cam.hass.async_add_executor_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_when_attribute_missing(self):
        """`_image_rotation_180` may not exist on older coordinator
        snapshots — getattr default {} keeps the rotation off."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        # Remove the attribute entirely
        if hasattr(coord, "_image_rotation_180"):
            delattr(coord, "_image_rotation_180")
        cam = _make_camera(coord=coord)
        cam._async_camera_image_impl = AsyncMock(return_value=b"\xff\xd8orig")
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8orig"


# ── _async_camera_image_impl LOCAL Digest path ───────────────────────────


class TestAsyncCameraImageImplLocalDigest:
    """The LOCAL path uses async_digest_request (aiohttp-native Digest auth).

    Mock async_digest_request to simulate the async fetch result
    without actually doing any HTTP."""

    def _local_coord(self):
        return _make_coord(
            _live_connections={
                CAM_ID: {
                    "_connection_type": "LOCAL",
                    "proxyUrl": "https://192.0.2.1/snap.jpg",
                    "_local_user": "cbs-1",
                    "_local_password": "p",
                },
            }
        )

    def _digest_resp_cm(
        self, status: int, body: bytes = b"", content_type: str = "image/jpeg"
    ):
        """Build a mock CM for async_digest_request."""
        from unittest.mock import AsyncMock, MagicMock

        resp = MagicMock()
        resp.status = status
        resp.headers = {"Content-Type": content_type}
        resp.read = AsyncMock(return_value=body)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    @pytest.mark.asyncio
    async def test_local_digest_success_caches_image(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera(coord=self._local_coord())
        img = b"\xff\xd8local-img"
        cm = self._digest_resp_cm(200, img, "image/jpeg")
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == img
        assert cam._cached_image == img
        assert cam._last_image_fetch > 0

    @pytest.mark.asyncio
    async def test_local_digest_timeout_returns_placeholder(self):
        """LOCAL Digest fetch times out — return cached/placeholder
        immediately rather than racing HA's outer 10s timeout. Pin:
        the function MUST NOT fall through to aiohttp for LOCAL
        (the proxy_url for LOCAL is the camera's HTTPS endpoint that
        requires Digest auth — unauth aiohttp would 401 in another
        ~10 s, blowing HA's outer timeout)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera(coord=self._local_coord(), _cached_image=b"\xff\xd8cached")
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        # TimeoutError caught inside the LOCAL block, early-return cached/placeholder
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_local_digest_fetch_failure_returns_cached(self):
        """If async_digest_request returns non-image (aiohttp error, 401, etc.),
        skip aiohttp and return cached/placeholder."""
        import aiohttp

        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera(coord=self._local_coord(), _cached_image=b"\xff\xd8cached")
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=aiohttp.ClientError("network error")),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == b"\xff\xd8cached"


# ── _yuv422_to_jpeg additional edge ──────────────────────────────────────


class TestYuv422EdgeCases:
    """Additional defensive tests beyond test_camera_extra.py."""

    def test_zero_sized_input_returns_none(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera()
        out = BoschCamera._yuv422_to_jpeg(cam, b"")
        assert out is None


# ── Section 2b outage fallback gated by is_streaming (bug-hunt 2026-06-10) ───


class TestOutageFallbackStreamingGuard:
    """Section 2b (LOCAL snap.jpg via cached Digest creds during a cloud
    outage) must NOT run while the camera is streaming: opening a second HTTP
    Digest session against the camera contends with the Bosch 3-session limit
    and can tear down the active RTSP stream. Section 2 already guards on
    `not is_streaming`; 2b previously did not. Bug found 2026-06-10."""

    def _outage_coord(self, **overrides):
        base = dict(
            _auth_outage_count=1,
            _local_creds_cache={
                CAM_ID: {
                    "user": "cbs-1",
                    "password": "p",
                    "host": "192.0.2.5",
                    "port": 443,
                }
            },
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        base.update(overrides)
        return _make_coord(**base)

    @pytest.mark.asyncio
    async def test_streaming_skips_outage_digest(self):
        """is_streaming True (live rtspsUrl, empty proxyUrl) → section 2b must
        NOT call async_digest_request; returns the cached image instead."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = self._outage_coord(
            # rtspsUrl present → is_streaming True; no proxyUrl → section 1 skipped
            _live_connections={CAM_ID: {"rtspsUrl": "rtsps://192.0.2.9/s"}},
        )
        cam = _make_camera(coord=coord, _cached_image=b"\xff\xd8cached")
        digest = AsyncMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=digest,
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)

        digest.assert_not_called()
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_idle_outage_still_uses_digest(self):
        """Positive control: idle (not streaming) + cloud outage + cached creds
        → section 2b DOES attempt the LOCAL Digest snap.jpg fallback."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = self._outage_coord(_live_connections={})  # is_streaming False
        # No cached image → section 2 first-load branch falls through to 2b
        # when the cloud fetch fails (instead of returning a stale cache).
        cam = _make_camera(coord=coord, _cached_image=None)

        resp = MagicMock()
        resp.status = 200
        resp.headers = {"Content-Type": "image/jpeg"}
        resp.read = AsyncMock(return_value=b"\xff\xd8outage-snap")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ) as digest,
        ):
            out = await BoschCamera._async_camera_image_impl(cam)

        digest.assert_called_once()
        assert out == b"\xff\xd8outage-snap"
