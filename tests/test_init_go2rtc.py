"""Tests for `__init__.py` go2rtc integration + WebRTC watchdog.

Covers:
  - `_register_go2rtc_stream` (~113 LOC) — endpoint discovery (11984 → 1984),
    rtsps://→rtspx:// rewrite (skip TLS verify for Bosch cert mismatch),
    yaml-persist HTTP-400 soft-success path, verify-GET probe.
  - `_unregister_go2rtc_stream` (~26 LOC) — DELETE on both endpoints,
    swallow ClientError so go2rtc-not-running doesn't surface.
  - `_check_and_recover_webrtc` (~90 LOC) — the v10.3.24 watchdog for
    HA's bundled go2rtc WebRTCProvider stale-schemes bug. Test the
    early-return branches and the direct-refresh recovery path.
  - `_refresh_rcp_state` (~27 LOC) — post-stream-start hook (currently
    a marker, was real RCP read in v10.4.8, reverted in v10.4.9).
  - `_create_ssl_ctx` static method.

These all delegate aiohttp work — patch the session + endpoints and
verify the request shape.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _make_coord(**overrides):
    base = dict(
        _camera_entities={},
        _live_connections={},
        _rcp_state_cache={},
        _tls_proxy_ports={},
        _tls_ssl_ctx=None,
        _last_schemes_refresh=0.0,
        _last_go2rtc_reload=0.0,
        debug=False,
        hass=SimpleNamespace(
            config=SimpleNamespace(config_dir="/tmp/ha_test"),
            config_entries=SimpleNamespace(
                async_entries=MagicMock(return_value=[]),
                async_reload=AsyncMock(),
            ),
            async_add_executor_job=AsyncMock(),
        ),
        _ensure_go2rtc_schemes_fresh=AsyncMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Fake aiohttp response context manager ────────────────────────────────


@asynccontextmanager
async def _fake_resp(status=200, text=""):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    yield resp


def _make_session(put_responses=None, get_responses=None, delete_responses=None):
    """Fake aiohttp.ClientSession with cycling response queues."""
    put_iter = iter(put_responses or [])
    get_iter = iter(get_responses or [])
    delete_iter = iter(delete_responses or [])

    def _put(*a, **kw):
        try:
            r = next(put_iter)
        except StopIteration:
            r = (500, "")
        return _fake_resp(*r)

    def _get(*a, **kw):
        try:
            r = next(get_iter)
        except StopIteration:
            r = (404, "")
        return _fake_resp(*r)

    def _delete(*a, **kw):
        try:
            r = next(delete_iter)
        except StopIteration:
            r = (404, "")
        return _fake_resp(*r)

    session = MagicMock()
    session.put = _put
    session.get = _get
    session.delete = _delete
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


# ── _register_go2rtc_stream ──────────────────────────────────────────────


class TestRegisterGo2rtcStream:
    """Pin the registration contract — name = camera.entity_id (HA's
    bundled go2rtc provider uses this), rtsps→rtspx rewrite, verify
    GET after PUT, yaml-persist HTTP-400 soft success."""

    def _make_session_for_register(self, put_status=200, put_body="", get_status=200, capture=None):
        """Build a fake aiohttp.ClientSession matching how _register_go2rtc_stream
        calls it: PUT is `await s.put(...)`, GET is `async with s.get(...)`."""

        async def _put(*args, **kw):
            if capture is not None:
                capture["params"] = kw.get("params", {})
            return SimpleNamespace(
                status=put_status,
                text=AsyncMock(return_value=put_body),
            )

        @asynccontextmanager
        async def _get(*args, **kw):
            yield SimpleNamespace(status=get_status)

        session = MagicMock()
        session.put = _put
        session.get = _get
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    @pytest.mark.asyncio
    async def test_uses_entity_id_as_stream_name(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        cam_entity = SimpleNamespace(entity_id="camera.bosch_terrasse")
        coord = _make_coord(_camera_entities={CAM_A: cam_entity})
        captured = {}
        session = self._make_session_for_register(capture=captured)
        with patch("aiohttp.ClientSession", return_value=session):
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://x:y@host/path",
            )
        assert captured["params"]["name"] == "camera.bosch_terrasse"

    @pytest.mark.asyncio
    async def test_rtsps_rewritten_to_rtspx(self):
        """Bosch's RTSPS proxy returns a cert for *.residential.connect.bosch
        but serves session URLs on proxy-NN.live.cbs.bosch — go2rtc's
        Go RTSP client refuses the mismatch. Force rtspx:// (skip TLS
        verify in go2rtc) so consumer requests don't 500."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        cam_entity = SimpleNamespace(entity_id="camera.bosch_terrasse")
        coord = _make_coord(_camera_entities={CAM_A: cam_entity})
        captured = {}
        session = self._make_session_for_register(capture=captured)
        with patch("aiohttp.ClientSession", return_value=session):
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://user:pass@cam.bosch/path",
            )
        assert captured["params"]["src"].startswith("rtspx://")
        assert captured["params"]["src"] == "rtspx://user:pass@cam.bosch/path"

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_name_when_no_entity_id(self):
        """First-registration race: cam_entity not yet added → use the
        legacy `bosch_shc_cam_<id>` name."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_camera_entities={})  # no entity yet
        captured = {}
        session = self._make_session_for_register(capture=captured)
        with patch("aiohttp.ClientSession", return_value=session):
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://x/y",
            )
        assert captured["params"]["name"] == f"bosch_shc_cam_{CAM_A.lower()}"

    @pytest.mark.asyncio
    async def test_yaml_persist_400_treated_as_success(self):
        """go2rtc bundled with HA writes the stream to its in-memory
        registry via URL params, then tries to persist to /config/go2rtc.yaml.
        The YAML persist fails with HTTP 400 + body 'yaml: ...'. The
        in-memory stream IS registered. Pin the soft-success path."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        cam_entity = SimpleNamespace(entity_id="camera.bosch_terrasse")
        coord = _make_coord(_camera_entities={CAM_A: cam_entity})
        session = self._make_session_for_register(
            put_status=400,
            put_body="yaml: line 5: did not find expected key",
            get_status=200,
        )
        with patch("aiohttp.ClientSession", return_value=session):
            # Must complete without raising, even though HTTP 400
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://x/y",
            )

    @pytest.mark.asyncio
    async def test_all_endpoints_fail_no_exception(self):
        """When go2rtc is not running on any port, the function must
        log + return silently — fall back to TLS proxy + HLS."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()

        with patch(
            "aiohttp.ClientSession",
            side_effect=aiohttp_client_error,
        ):
            # Must NOT raise
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://x/y",
            )


def aiohttp_client_error(*args, **kwargs):
    import aiohttp
    raise aiohttp.ClientError("connection refused")


# ── _unregister_go2rtc_stream ────────────────────────────────────────────


class TestUnregisterGo2rtcStream:
    def _make_session_for_unregister(self, capture=None):
        """Build a fake aiohttp.ClientSession matching how
        _unregister_go2rtc_stream calls it: DELETE is `await s.delete(...)`."""

        async def _delete(*args, **kw):
            if capture is not None:
                capture["params"] = kw.get("params", {})
            return SimpleNamespace(status=200, text=AsyncMock(return_value=""))

        session = MagicMock()
        session.delete = _delete
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        return session

    @pytest.mark.asyncio
    async def test_uses_entity_id_for_delete(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        cam_entity = SimpleNamespace(entity_id="camera.bosch_terrasse")
        coord = _make_coord(_camera_entities={CAM_A: cam_entity})
        captured = {}
        session = self._make_session_for_unregister(capture=captured)
        with patch("aiohttp.ClientSession", return_value=session):
            await BoschCameraCoordinator._unregister_go2rtc_stream(coord, CAM_A)
        assert captured["params"]["name"] == "camera.bosch_terrasse"

    @pytest.mark.asyncio
    async def test_swallows_client_error(self):
        """go2rtc not running on either endpoint → swallow ClientError."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "aiohttp.ClientSession",
            side_effect=aiohttp_client_error,
        ):
            # Must NOT raise
            await BoschCameraCoordinator._unregister_go2rtc_stream(coord, CAM_A)

    @pytest.mark.asyncio
    async def test_legacy_name_fallback(self):
        """No camera entity yet → use legacy `bosch_shc_cam_<id>` name."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_camera_entities={})
        captured = {}
        session = self._make_session_for_unregister(capture=captured)
        with patch("aiohttp.ClientSession", return_value=session):
            await BoschCameraCoordinator._unregister_go2rtc_stream(coord, CAM_A)
        assert captured["params"]["name"] == f"bosch_shc_cam_{CAM_A.lower()}"


# ── _check_and_recover_webrtc ────────────────────────────────────────────


class TestCheckAndRecoverWebrtc:
    @pytest.mark.asyncio
    async def test_returns_when_no_camera_entity(self):
        """Cam removed between stream-start and watchdog tick → exit
        silently."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_camera_entities={})
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

    @pytest.mark.asyncio
    async def test_returns_when_supported_features_no_stream(self):
        """Stream not yet ready (supported_features doesn't have STREAM
        flag) → exit; nothing to check."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.components.camera import CameraEntityFeature
        cam_entity = SimpleNamespace(supported_features=CameraEntityFeature(0))
        coord = _make_coord(_camera_entities={CAM_A: cam_entity})
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)
        # async_reload should not be called
        coord.hass.config_entries.async_reload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returns_when_webrtc_already_present(self):
        """The capability is already correct → exit; no recovery needed."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.components.camera import CameraEntityFeature, StreamType
        caps = SimpleNamespace(frontend_stream_types={StreamType.WEB_RTC})
        cam_entity = SimpleNamespace(
            supported_features=CameraEntityFeature.STREAM,
            camera_capabilities=caps,
        )
        coord = _make_coord(_camera_entities={CAM_A: cam_entity})
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)
        coord._ensure_go2rtc_schemes_fresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_capabilities_probe_exception_returns_silently(self):
        """If `camera_capabilities` raises (defensive), bail without
        recovery — better to leave the stream alone than crash the
        watchdog."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.components.camera import CameraEntityFeature

        class _Cam:
            supported_features = CameraEntityFeature.STREAM

            @property
            def camera_capabilities(self):
                raise RuntimeError("HA internal error")

        coord = _make_coord(_camera_entities={CAM_A: _Cam()})
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)
        coord._ensure_go2rtc_schemes_fresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_direct_refresh_restores_webrtc(self):
        """The direct schemes-refresh succeeds → return without full
        config-entry reload (cheap recovery path)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.components.camera import CameraEntityFeature, StreamType

        caps_calls = [
            SimpleNamespace(frontend_stream_types={StreamType.HLS}),  # first read
            SimpleNamespace(frontend_stream_types={StreamType.HLS, StreamType.WEB_RTC}),  # after refresh
        ]
        caps_iter = iter(caps_calls)

        class _Cam:
            supported_features = CameraEntityFeature.STREAM

            @property
            def camera_capabilities(self):
                return next(caps_iter)

            def _invalidate_camera_capabilities_cache(self):
                pass

        coord = _make_coord(_camera_entities={CAM_A: _Cam()})
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)
        coord._ensure_go2rtc_schemes_fresh.assert_awaited_once()
        # Forced refresh — _last_schemes_refresh reset to 0
        # (it was set to 0 inside but happened before the await; still 0)
        # Reload NOT called since direct refresh succeeded
        coord.hass.config_entries.async_reload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_throttles_full_reload(self):
        """If the direct refresh fails AND _last_go2rtc_reload was
        recent (<3600s), skip the reload — don't spam reloads when
        go2rtc is genuinely broken."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.components.camera import CameraEntityFeature, StreamType

        caps = SimpleNamespace(frontend_stream_types={StreamType.HLS})

        class _Cam:
            supported_features = CameraEntityFeature.STREAM
            camera_capabilities = caps

            def _invalidate_camera_capabilities_cache(self):
                pass

        coord = _make_coord(
            _camera_entities={CAM_A: _Cam()},
            _last_go2rtc_reload=time.monotonic() - 100,  # 100s ago < 3600
        )
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)
        coord.hass.config_entries.async_reload.assert_not_awaited()


# ── _refresh_rcp_state ───────────────────────────────────────────────────


class TestRefreshRcpState:
    @pytest.mark.asyncio
    async def test_no_existing_cache_no_op(self):
        """If `_rcp_state_cache[cam_id]` is empty (never had a RCP read
        for this cam), the function leaves it that way — only mutates
        existing entries."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        await BoschCameraCoordinator._refresh_rcp_state(coord, CAM_A)
        # setdefault inserted an empty dict but no source/fetched_at written
        assert coord._rcp_state_cache[CAM_A] == {}

    @pytest.mark.asyncio
    async def test_existing_cache_gets_source_stamped(self):
        """When the cache already has data from a prior LOCAL stream,
        re-stamp source + fetched_at so consumers know it's fresh."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
            _rcp_state_cache={CAM_A: {"privacy_mode": False}},
        )
        await BoschCameraCoordinator._refresh_rcp_state(coord, CAM_A)
        assert coord._rcp_state_cache[CAM_A]["source"] == "local"
        assert "fetched_at" in coord._rcp_state_cache[CAM_A]
        # Existing field preserved
        assert coord._rcp_state_cache[CAM_A]["privacy_mode"] is False


# ── _create_ssl_ctx + _start_tls_proxy ───────────────────────────────────


class TestSslContextAndStartTlsProxy:
    def test_ssl_ctx_disables_verification(self):
        """Bosch cameras use self-signed certs — must disable hostname
        check + cert verify or the TLS proxy can't connect."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        import ssl
        ctx = BoschCameraCoordinator._create_ssl_ctx()
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE

    @pytest.mark.asyncio
    async def test_start_tls_proxy_creates_ssl_ctx_lazily(self):
        """First call must dispatch _create_ssl_ctx via executor (it's
        blocking I/O) — subsequent calls reuse the cached context."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_tls_ssl_ctx=None)
        # Stub the staticmethod on the coord (it's accessed via self._create_ssl_ctx)
        coord._create_ssl_ctx = BoschCameraCoordinator._create_ssl_ctx
        coord.hass.async_add_executor_job = AsyncMock(return_value="MOCK_CTX")
        with patch(
            "custom_components.bosch_shc_camera.start_tls_proxy",
            return_value=12345,
        ):
            port = await BoschCameraCoordinator._start_tls_proxy(
                coord, CAM_A, "192.0.2.1", 443,
            )
        assert port == 12345
        coord.hass.async_add_executor_job.assert_awaited_once()
        # Cached for next call
        assert coord._tls_ssl_ctx == "MOCK_CTX"

    @pytest.mark.asyncio
    async def test_start_tls_proxy_reuses_cached_ssl_ctx(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_tls_ssl_ctx="ALREADY_CACHED")
        coord.hass.async_add_executor_job = AsyncMock()
        with patch(
            "custom_components.bosch_shc_camera.start_tls_proxy",
            return_value=12345,
        ):
            await BoschCameraCoordinator._start_tls_proxy(
                coord, CAM_A, "192.0.2.1", 443,
            )
        coord.hass.async_add_executor_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_tls_proxy_delegates_to_module(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._tls_proxy_ports = {CAM_A: 12345}
        with patch(
            "custom_components.bosch_shc_camera.stop_tls_proxy",
        ) as stop:
            await BoschCameraCoordinator._stop_tls_proxy(coord, CAM_A)
        stop.assert_called_once_with(CAM_A, coord._tls_proxy_ports)
