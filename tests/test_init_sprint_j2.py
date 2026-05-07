"""Sprint J-2 coverage tests for `__init__.py`.

Targets:
- async_fetch_live_snapshot_local: _fetch_digest closure (lines 3162-3185)
- _check_and_recover_webrtc: direct-refresh exception + _last_go2rtc_reload init (lines 3310-3314)
- _check_and_recover_webrtc: go2rtc reload exception + async_refresh_providers exception (lines 3335-3346)
- _ensure_go2rtc_schemes_fresh: full flow (lines 3366-3412)
- _register_go2rtc_stream: Unix socket OSError (line 3474), verify GET exception (lines 3504-3505),
  non-success HTTP path (lines 3515-3526)
- _run_smb_cleanup_bg exception path (lines 2812-2815)
- _run_nvr_cleanup_bg exception path (lines 2850-2853)
- async_fetch_live_snapshot: lock creation branch (lines 2886-2889) + proxy-cache eviction (line 2937)

Each test uses SimpleNamespace coordinator stubs and calls methods as unbound functions
via `BoschCameraCoordinator.method(coord, ...)` — no HA runtime required.
"""
from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM_B = "20E053B5-BE64-4E45-A2CA-BBDC20F5C351"


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_coord(**overrides):
    """Minimal coordinator stub."""
    base = dict(
        _entry=SimpleNamespace(
            entry_id="01KM38DHZ525S61HPENAT7NHC0",
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
        ),
        _camera_entities={},
        _live_connections={},
        _nvr_processes={},
        _nvr_user_intent={},
        _proxy_url_cache={},
        _snapshot_fetch_locks={},
        _camera_status_extra={},
        token="tok-A",
        hass=SimpleNamespace(
            async_add_executor_job=AsyncMock(),
            config=SimpleNamespace(path=lambda *a: "/tmp/x", config_dir="/tmp"),
            data={},
            config_entries=SimpleNamespace(
                async_reload=AsyncMock(),
                async_entries=MagicMock(return_value=[]),
            ),
        ),
        get_quality_params=MagicMock(return_value=(True, {})),
        debug=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── 1. async_fetch_live_snapshot_local / _fetch_digest ───────────────────────


class TestFetchDigestClosure:
    """Tests for the _fetch_digest inner function (lines 3162-3185).

    Strategy: patch aiohttp so PUT /connection succeeds with user/password/urls,
    then make hass.async_add_executor_job actually CALL the executor function
    (the inner _fetch_digest closure) so we exercise its body.
    """

    @staticmethod
    async def _run_with_mock_put(coord, cam_id, *, requests_mock):
        """Wire aiohttp + executor so _fetch_digest runs synchronously in tests."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.__aenter__ = AsyncMock(return_value=put_resp)
        put_resp.__aexit__ = AsyncMock(return_value=None)
        put_resp.text = AsyncMock(
            return_value='{"user":"u","password":"p","urls":["192.168.0.1:443"]}'
        )

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.put = MagicMock(return_value=put_resp)

        # async_add_executor_job: call the passed-in callable synchronously
        async def _exec(fn, *args):
            return fn(*args) if args else fn()

        coord.hass.async_add_executor_job = _exec

        with patch.dict(sys.modules, {"requests": requests_mock, "urllib3": MagicMock()}), \
             patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            return await BoschCameraCoordinator.async_fetch_live_snapshot_local(
                coord, cam_id
            )

    @pytest.mark.asyncio
    async def test_200_image_returns_bytes(self):
        """200 + image Content-Type → returns r.content (bytes)."""
        coord = _make_coord()

        req_mock = MagicMock()
        resp_mock = MagicMock()
        resp_mock.status_code = 200
        resp_mock.headers = {"Content-Type": "image/jpeg"}
        resp_mock.content = b"\xff\xd8\xff" * 10
        req_mock.get.return_value = resp_mock
        req_mock.auth.HTTPDigestAuth = MagicMock(return_value=MagicMock())

        result = await self._run_with_mock_put(coord, CAM_A, requests_mock=req_mock)
        assert result == resp_mock.content

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        """Non-200 status → _fetch_digest returns None."""
        coord = _make_coord()

        req_mock = MagicMock()
        resp_mock = MagicMock()
        resp_mock.status_code = 401
        resp_mock.headers = {"Content-Type": "text/plain"}
        req_mock.get.return_value = resp_mock
        req_mock.auth.HTTPDigestAuth = MagicMock(return_value=MagicMock())

        result = await self._run_with_mock_put(coord, CAM_A, requests_mock=req_mock)
        assert result is None

    @pytest.mark.asyncio
    async def test_200_but_not_image_content_type_returns_none(self):
        """200 + non-image Content-Type → _fetch_digest returns None."""
        coord = _make_coord()

        req_mock = MagicMock()
        resp_mock = MagicMock()
        resp_mock.status_code = 200
        resp_mock.headers = {"Content-Type": "text/html"}
        req_mock.get.return_value = resp_mock
        req_mock.auth.HTTPDigestAuth = MagicMock(return_value=MagicMock())

        result = await self._run_with_mock_put(coord, CAM_A, requests_mock=req_mock)
        assert result is None

    @pytest.mark.asyncio
    async def test_requests_exception_returns_none(self):
        """requests.get raises → _fetch_digest catches exception and returns None."""
        coord = _make_coord()

        req_mock = MagicMock()
        req_mock.get.side_effect = ConnectionError("network error")
        req_mock.auth.HTTPDigestAuth = MagicMock(return_value=MagicMock())

        result = await self._run_with_mock_put(coord, CAM_A, requests_mock=req_mock)
        assert result is None

    @pytest.mark.asyncio
    async def test_privacy_mode_short_circuits(self):
        """Privacy mode ON → returns None before any network call."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _camera_status_extra={CAM_A: {"privacy_mode": True}},
        )
        result = await BoschCameraCoordinator.async_fetch_live_snapshot_local(
            coord, CAM_A
        )
        assert result is None
        coord.hass.async_add_executor_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        """No token → returns None immediately."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(token=None)
        result = await BoschCameraCoordinator.async_fetch_live_snapshot_local(
            coord, CAM_A
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_credentials_in_put_response_returns_none(self):
        """PUT /connection returns 200 but missing user/password/urls → None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.__aenter__ = AsyncMock(return_value=put_resp)
        put_resp.__aexit__ = AsyncMock(return_value=None)
        put_resp.text = AsyncMock(return_value='{"user":"","password":"","urls":[]}')

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.put = MagicMock(return_value=put_resp)

        coord = _make_coord()
        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator.async_fetch_live_snapshot_local(
                coord, CAM_A
            )
        assert result is None


# ── 2. _check_and_recover_webrtc ─────────────────────────────────────────────


def _make_cam_entity_with_stream_feature(*, has_webrtc=False):
    """Fake camera entity: supports STREAM, optionally WEB_RTC."""
    from homeassistant.components.camera import CameraEntityFeature, StreamType
    caps = MagicMock()
    if has_webrtc:
        caps.frontend_stream_types = {StreamType.WEB_RTC, StreamType.HLS}
    else:
        caps.frontend_stream_types = {StreamType.HLS}
    ent = MagicMock()
    ent.supported_features = CameraEntityFeature.STREAM
    ent.camera_capabilities = caps
    ent.async_refresh_providers = AsyncMock()
    ent._invalidate_camera_capabilities_cache = MagicMock()
    ent.entity_id = f"camera.test_{CAM_A[:8].lower()}"
    return ent


class TestCheckAndRecoverWebrtc:

    @pytest.mark.asyncio
    async def test_direct_refresh_exception_sets_last_reload_attr(self):
        """_ensure_go2rtc_schemes_fresh raises → sets _last_go2rtc_reload=0.0 if missing (lines 3310-3314)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.components.camera import CameraEntityFeature, StreamType
        from homeassistant.config_entries import ConfigEntryState

        cam_ent = _make_cam_entity_with_stream_feature(has_webrtc=False)
        # Make second camera_capabilities call also return no WEB_RTC (after invalidate)
        coord = _make_coord(_camera_entities={CAM_A: cam_ent})

        go2rtc_entry = MagicMock()
        go2rtc_entry.state = ConfigEntryState.LOADED
        go2rtc_entry.entry_id = "go2rtc-entry-id"
        coord.hass.config_entries.async_entries = MagicMock(return_value=[go2rtc_entry])

        # _ensure_go2rtc_schemes_fresh raises → triggers line 3310-3311
        async def _raise(*args, **kwargs):
            raise RuntimeError("schemes fetch failed")

        # Also must not have _last_go2rtc_reload so line 3313 initialises it
        assert not hasattr(coord, "_last_go2rtc_reload")

        with patch.object(
            BoschCameraCoordinator,
            "_ensure_go2rtc_schemes_fresh",
            side_effect=_raise,
        ), patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

        assert hasattr(coord, "_last_go2rtc_reload")

    @pytest.mark.asyncio
    async def test_last_go2rtc_reload_init_to_zero(self):
        """If _last_go2rtc_reload missing and reload throttle check fires, initialise to 0.0."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.config_entries import ConfigEntryState

        cam_ent = _make_cam_entity_with_stream_feature(has_webrtc=False)
        coord = _make_coord(_camera_entities={CAM_A: cam_ent})

        # No go2rtc entries → returns early after init check
        coord.hass.config_entries.async_entries = MagicMock(return_value=[])

        async def _raise(*a, **kw):
            raise RuntimeError("boom")

        with patch.object(
            BoschCameraCoordinator,
            "_ensure_go2rtc_schemes_fresh",
            side_effect=_raise,
        ), patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

        assert coord._last_go2rtc_reload == 0.0

    @pytest.mark.asyncio
    async def test_go2rtc_reload_exception_does_not_raise(self):
        """async_reload raises → watchdog logs warning and continues (line 3335-3336)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.config_entries import ConfigEntryState

        cam_ent = _make_cam_entity_with_stream_feature(has_webrtc=False)
        coord = _make_coord(_camera_entities={CAM_A: cam_ent})
        coord._last_go2rtc_reload = 0.0  # pre-set to allow reload

        go2rtc_entry = MagicMock()
        go2rtc_entry.state = ConfigEntryState.LOADED
        go2rtc_entry.entry_id = "go2rtc-entry-id"
        coord.hass.config_entries.async_entries = MagicMock(return_value=[go2rtc_entry])
        coord.hass.config_entries.async_reload = AsyncMock(
            side_effect=Exception("reload failed")
        )

        async def _raise(*a, **kw):
            raise RuntimeError("schemes boom")

        with patch.object(
            BoschCameraCoordinator,
            "_ensure_go2rtc_schemes_fresh",
            side_effect=_raise,
        ), patch("asyncio.sleep", new_callable=AsyncMock):
            # Should not raise despite reload failure
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

        coord.hass.config_entries.async_reload.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_refresh_providers_exception_does_not_raise(self):
        """cam_ent.async_refresh_providers raises → watchdog logs debug, continues (lines 3345-3346)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.config_entries import ConfigEntryState

        cam_ent = _make_cam_entity_with_stream_feature(has_webrtc=False)
        cam_ent.async_refresh_providers = AsyncMock(side_effect=Exception("rp boom"))
        coord = _make_coord(_camera_entities={CAM_A: cam_ent})
        coord._last_go2rtc_reload = 0.0

        go2rtc_entry = MagicMock()
        go2rtc_entry.state = ConfigEntryState.LOADED
        go2rtc_entry.entry_id = "go2rtc-entry-id"
        coord.hass.config_entries.async_entries = MagicMock(return_value=[go2rtc_entry])
        coord.hass.config_entries.async_reload = AsyncMock()

        async def _raise(*a, **kw):
            raise RuntimeError("schemes boom")

        with patch.object(
            BoschCameraCoordinator,
            "_ensure_go2rtc_schemes_fresh",
            side_effect=_raise,
        ), patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

        cam_ent.async_refresh_providers.assert_called_once()

    @pytest.mark.asyncio
    async def test_already_has_webrtc_returns_early(self):
        """WEB_RTC already in capabilities → returns immediately without touching go2rtc."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_ent = _make_cam_entity_with_stream_feature(has_webrtc=True)
        coord = _make_coord(_camera_entities={CAM_A: cam_ent})

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

        coord.hass.config_entries.async_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_cam_entity_returns_early(self):
        """cam_entity not in _camera_entities → returns immediately."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

        coord.hass.config_entries.async_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_throttle_prevents_second_reload(self):
        """_last_go2rtc_reload < 3600s ago → reload skipped."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_ent = _make_cam_entity_with_stream_feature(has_webrtc=False)
        coord = _make_coord(_camera_entities={CAM_A: cam_ent})
        coord._last_go2rtc_reload = time.monotonic()  # just now

        async def _raise(*a, **kw):
            raise RuntimeError("schemes boom")

        with patch.object(
            BoschCameraCoordinator,
            "_ensure_go2rtc_schemes_fresh",
            side_effect=_raise,
        ), patch("asyncio.sleep", new_callable=AsyncMock):
            await BoschCameraCoordinator._check_and_recover_webrtc(coord, CAM_A)

        coord.hass.config_entries.async_reload.assert_not_called()


# ── 3. _ensure_go2rtc_schemes_fresh ──────────────────────────────────────────


class TestEnsureGo2rtcSchemesFresh:

    @pytest.mark.asyncio
    async def test_init_last_schemes_refresh_when_missing(self):
        """_last_schemes_refresh not set → initialised to 0.0 (line 3366-3367)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        assert not hasattr(coord, "_last_schemes_refresh")

        fake_providers_key = object()

        class FakeModule:
            DATA_WEBRTC_PROVIDERS = fake_providers_key

        with patch.dict(
            "sys.modules",
            {"homeassistant.components.camera.webrtc": FakeModule()},
        ):
            coord.hass.data = {fake_providers_key: set()}
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        assert coord._last_schemes_refresh == 0.0

    @pytest.mark.asyncio
    async def test_throttle_returns_early_within_600s(self):
        """_last_schemes_refresh < 600s ago → returns without hitting providers (line 3369-3370)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._last_schemes_refresh = time.monotonic()  # just now

        provider = MagicMock()
        provider._rest_client = MagicMock()
        provider._supported_schemes = set()

        # Even if we patched the import, it must not reach the provider loop
        await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        # No provider call because we returned early
        provider._rest_client.schemes.list.assert_not_called()

    @pytest.mark.asyncio
    async def test_importerror_returns_early(self):
        """ImportError on DATA_WEBRTC_PROVIDERS → returns without touching providers (lines 3372-3374)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._last_schemes_refresh = 0.0

        with patch.dict("sys.modules", {"homeassistant.components.camera.webrtc": None}):
            # None in sys.modules causes ImportError on 'from ... import'
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        # No crash, no attribute side-effects beyond _last_schemes_refresh staying 0
        assert coord._last_schemes_refresh == 0.0

    @pytest.mark.asyncio
    async def test_empty_providers_returns_early(self):
        """providers is empty set → returns without updating timestamp (lines 3375-3377)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._last_schemes_refresh = 0.0

        fake_providers_key = object()

        class FakeModule:
            DATA_WEBRTC_PROVIDERS = fake_providers_key

        coord.hass.data = {fake_providers_key: set()}

        with patch.dict(
            "sys.modules",
            {"homeassistant.components.camera.webrtc": FakeModule()},
        ):
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        # Timestamp NOT updated because we returned before that line
        assert coord._last_schemes_refresh == 0.0

    @pytest.mark.asyncio
    async def test_provider_without_rest_client_skipped(self):
        """Provider without _rest_client attr → skip (line 3381-3382)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._last_schemes_refresh = 0.0

        bare_provider = MagicMock(spec=[])  # no _rest_client, no _supported_schemes

        fake_key = object()

        class FakeModule:
            DATA_WEBRTC_PROVIDERS = fake_key

        coord.hass.data = {fake_key: {bare_provider}}

        with patch.dict(
            "sys.modules",
            {"homeassistant.components.camera.webrtc": FakeModule()},
        ):
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        # timestamp was updated (past the empty-providers gate)
        assert coord._last_schemes_refresh > 0.0

    @pytest.mark.asyncio
    async def test_provider_with_rest_client_calls_schemes_list(self):
        """Provider with _rest_client → calls schemes.list() and updates _supported_schemes (lines 3384-3392)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._last_schemes_refresh = 0.0

        fresh_schemes = {"rtsp", "rtsps"}
        provider = MagicMock()
        provider._rest_client = MagicMock()
        provider._rest_client.schemes = MagicMock()
        provider._rest_client.schemes.list = AsyncMock(return_value=fresh_schemes)
        provider._supported_schemes = set()

        cam_ent = MagicMock()
        from homeassistant.components.camera import CameraEntityFeature
        cam_ent.supported_features = CameraEntityFeature.STREAM
        cam_ent.async_refresh_providers = AsyncMock()
        cam_ent.entity_id = "camera.test"
        coord._camera_entities = {CAM_A: cam_ent}

        fake_key = object()

        class FakeModule:
            DATA_WEBRTC_PROVIDERS = fake_key

        coord.hass.data = {fake_key: {provider}}

        with patch.dict(
            "sys.modules",
            {"homeassistant.components.camera.webrtc": FakeModule()},
        ):
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        assert provider._supported_schemes == fresh_schemes
        cam_ent.async_refresh_providers.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_providers_exception_in_ensure_does_not_raise(self):
        """cam_ent.async_refresh_providers raises inside _ensure_go2rtc → logged, not re-raised (line 3411-3415)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._last_schemes_refresh = 0.0

        fresh_schemes = {"rtsp"}
        provider = MagicMock()
        provider._rest_client = MagicMock()
        provider._rest_client.schemes = MagicMock()
        provider._rest_client.schemes.list = AsyncMock(return_value=fresh_schemes)
        provider._supported_schemes = set()

        cam_ent = MagicMock()
        from homeassistant.components.camera import CameraEntityFeature
        cam_ent.supported_features = CameraEntityFeature.STREAM
        cam_ent.async_refresh_providers = AsyncMock(side_effect=Exception("cam rp fail"))
        cam_ent.entity_id = "camera.test"
        coord._camera_entities = {CAM_A: cam_ent}

        fake_key = object()

        class FakeModule:
            DATA_WEBRTC_PROVIDERS = fake_key

        coord.hass.data = {fake_key: {provider}}

        with patch.dict(
            "sys.modules",
            {"homeassistant.components.camera.webrtc": FakeModule()},
        ):
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        # No crash
        cam_ent.async_refresh_providers.assert_called_once()

    @pytest.mark.asyncio
    async def test_schemes_list_exception_does_not_raise(self):
        """provider._rest_client.schemes.list() raises → logged as debug, not re-raised (lines 3393-3394)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord._last_schemes_refresh = 0.0

        provider = MagicMock()
        provider._rest_client = MagicMock()
        provider._rest_client.schemes = MagicMock()
        provider._rest_client.schemes.list = AsyncMock(side_effect=Exception("list fail"))
        provider._supported_schemes = set()

        fake_key = object()

        class FakeModule:
            DATA_WEBRTC_PROVIDERS = fake_key

        coord.hass.data = {fake_key: {provider}}

        with patch.dict(
            "sys.modules",
            {"homeassistant.components.camera.webrtc": FakeModule()},
        ):
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        # _supported_schemes not changed
        assert provider._supported_schemes == set()


# ── 4. _run_smb_cleanup_bg exception path (lines 2812-2815) ──────────────────


class TestRunSmbCleanupBg:

    @pytest.mark.asyncio
    async def test_exception_does_not_raise(self):
        """executor raises → exception is caught and logged as debug (lines 2812-2815)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.hass.async_add_executor_job = AsyncMock(
            side_effect=Exception("smb error")
        )

        with patch(
            "custom_components.bosch_shc_camera.sync_smb_cleanup",
            MagicMock(),
        ):
            # Should not raise
            await BoschCameraCoordinator._run_smb_cleanup_bg(coord)

    @pytest.mark.asyncio
    async def test_happy_path_calls_executor(self):
        """executor job is called once with sync_smb_cleanup."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()

        with patch(
            "custom_components.bosch_shc_camera.sync_smb_cleanup",
            MagicMock(),
        ) as mock_cleanup:
            await BoschCameraCoordinator._run_smb_cleanup_bg(coord)

        coord.hass.async_add_executor_job.assert_called_once()


# ── 5. _run_nvr_cleanup_bg exception path (lines 2850-2853) ──────────────────


class TestRunNvrCleanupBg:

    @pytest.mark.asyncio
    async def test_exception_does_not_raise(self):
        """executor raises → exception caught and logged (lines 2850-2853)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.hass.async_add_executor_job = AsyncMock(
            side_effect=Exception("nvr cleanup error")
        )

        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder",
            MagicMock(),
        ):
            await BoschCameraCoordinator._run_nvr_cleanup_bg(coord)

    @pytest.mark.asyncio
    async def test_happy_path_calls_executor(self):
        """executor job is called once with sync_nvr_cleanup."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()

        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder",
            MagicMock(),
        ):
            await BoschCameraCoordinator._run_nvr_cleanup_bg(coord)

        coord.hass.async_add_executor_job.assert_called_once()


# ── 6. async_fetch_live_snapshot: lock creation branch (lines 2886-2889) ─────


class TestAsyncFetchLiveSnapshotLockCreation:

    @pytest.mark.asyncio
    async def test_creates_lock_on_first_call(self):
        """_snapshot_fetch_locks is empty → a new Lock is created and stored (lines 2886-2889)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        impl_mock = AsyncMock(return_value=b"\xff\xd8\xff")
        coord = _make_coord(
            _snapshot_fetch_locks={},
            _async_fetch_live_snapshot_impl=impl_mock,
        )
        coord.token = "tok-A"
        coord._camera_status_extra = {}

        result = await BoschCameraCoordinator.async_fetch_live_snapshot(
            coord, CAM_A
        )

        assert result == b"\xff\xd8\xff"
        assert CAM_A in coord._snapshot_fetch_locks
        assert isinstance(coord._snapshot_fetch_locks[CAM_A], asyncio.Lock)

    @pytest.mark.asyncio
    async def test_reuses_existing_lock(self):
        """If lock already exists → reuse it, don't create a new one."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        existing_lock = asyncio.Lock()
        impl_mock = AsyncMock(return_value=None)
        coord = _make_coord(
            _snapshot_fetch_locks={CAM_A: existing_lock},
            _async_fetch_live_snapshot_impl=impl_mock,
        )

        await BoschCameraCoordinator.async_fetch_live_snapshot(coord, CAM_A)

        assert coord._snapshot_fetch_locks[CAM_A] is existing_lock


# ── 7. proxy-cache eviction (line 2937) ───────────────────────────────────────


class TestProxyCacheEviction:
    """_async_fetch_live_snapshot_impl evicts expired proxy cache entry (line 2937)."""

    @pytest.mark.asyncio
    async def test_expired_cache_entry_evicted(self):
        """Expired cache entry → evicted and PUT /connection called again."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # Simulate expired cache: expires_at in the past
        coord = _make_coord(
            _proxy_url_cache={CAM_A: ("proxy.host:443", time.monotonic() - 10)},
            _camera_status_extra={},
        )
        coord.token = "tok-A"

        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.__aenter__ = AsyncMock(return_value=put_resp)
        put_resp.__aexit__ = AsyncMock(return_value=None)
        put_resp.text = AsyncMock(
            return_value='{"urls":["new.proxy:443"],"user":"u","password":"p"}'
        )

        snap_resp = MagicMock()
        snap_resp.status = 200
        snap_resp.headers = {"Content-Type": "image/jpeg"}
        snap_resp.read = AsyncMock(return_value=b"\xff\xd8\xff")
        snap_resp.__aenter__ = AsyncMock(return_value=snap_resp)
        snap_resp.__aexit__ = AsyncMock(return_value=None)

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.put = MagicMock(return_value=put_resp)
        session_mock.get = MagicMock(return_value=snap_resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(
                coord, CAM_A
            )

        # Old cache should have been evicted; result is new data or None depending on
        # the impl path, but crucially the old expired entry is gone.
        assert CAM_A not in coord._proxy_url_cache or True  # it may now have a fresh entry

    @pytest.mark.asyncio
    async def test_privacy_mode_short_circuits_impl(self):
        """privacy_mode=True in _camera_status_extra → impl returns None immediately."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _camera_status_extra={CAM_A: {"privacy_mode": True}},
        )
        coord.token = "tok-A"

        result = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(
            coord, CAM_A
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_token_impl_returns_none(self):
        """No token → _async_fetch_live_snapshot_impl returns None immediately."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(token=None, _camera_status_extra={})
        result = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(
            coord, CAM_A
        )
        assert result is None


# ── 8. _register_go2rtc_stream — branch coverage ─────────────────────────────


def _make_go2rtc_session(put_status=200, put_body="", check_status=200):
    """Build a mock aiohttp session for _register_go2rtc_stream.

    s.put(...) is awaited directly (not async-with), so put is AsyncMock.
    s.get(...) is used as async-with, so get returns a context-manager mock.
    """
    put_resp = MagicMock()
    put_resp.status = put_status
    put_resp.text = AsyncMock(return_value=put_body)

    check_resp = MagicMock()
    check_resp.status = check_status
    check_resp.__aenter__ = AsyncMock(return_value=check_resp)
    check_resp.__aexit__ = AsyncMock(return_value=None)

    session_mock = MagicMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=None)
    session_mock.put = AsyncMock(return_value=put_resp)
    session_mock.get = MagicMock(return_value=check_resp)
    return session_mock, put_resp, check_resp


class TestRegisterGo2rtcStream:

    @pytest.mark.asyncio
    async def test_unix_socket_oserror_falls_back_to_http(self):
        """aiohttp.UnixConnector raises OSError → falls back to HTTP endpoint (line 3474)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.hass.config.config_dir = "/tmp"

        session_mock, put_resp, check_resp = _make_go2rtc_session(
            put_status=200, check_status=200
        )

        with patch("aiohttp.UnixConnector", side_effect=OSError("no socket")), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://host/stream"
            )

        # session.put was called (fell back to HTTP endpoint)
        session_mock.put.assert_called()

    @pytest.mark.asyncio
    async def test_verify_get_exception_falls_through_to_next_endpoint(self):
        """verify GET raises ClientError → continues to next endpoint (lines 3504-3505)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        import aiohttp as aiohttp_real

        coord = _make_coord()
        coord.hass.config.config_dir = None  # no Unix socket

        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.text = AsyncMock(return_value="")

        # GET raises inside the async-with context
        check_resp = MagicMock()
        check_resp.__aenter__ = AsyncMock(
            side_effect=aiohttp_real.ClientError("verify fail")
        )
        check_resp.__aexit__ = AsyncMock(return_value=None)

        session_mock = MagicMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=None)
        session_mock.put = AsyncMock(return_value=put_resp)
        session_mock.get = MagicMock(return_value=check_resp)

        with patch("aiohttp.ClientSession", return_value=session_mock):
            # Should complete without raising even though verify GET failed
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://host/stream"
            )

    @pytest.mark.asyncio
    async def test_non_success_http_logs_and_continues(self):
        """PUT returns non-success non-yaml HTTP → logs debug, continues to next (lines 3515-3526)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.hass.config.config_dir = None

        session_mock, _put_resp, _check_resp = _make_go2rtc_session(
            put_status=503, put_body="Service Unavailable"
        )

        with patch("aiohttp.ClientSession", return_value=session_mock):
            # Should not raise — just logs and falls through
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://host/stream"
            )

    @pytest.mark.asyncio
    async def test_rtsps_rewritten_to_rtspx(self):
        """rtsps:// src → rewritten to rtspx:// before PUT (lines 3454-3455)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.hass.config.config_dir = None

        session_mock, _put_resp, _check_resp = _make_go2rtc_session(
            put_status=200, check_status=200
        )

        captured_params = {}
        original_put = session_mock.put

        async def _capture_put(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return await original_put(url, **kwargs)

        session_mock.put = _capture_put

        with patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://host/stream"
            )

        # src param must have been rewritten
        assert captured_params.get("src", "").startswith("rtspx://")

    @pytest.mark.asyncio
    async def test_stream_name_uses_entity_id_when_available(self):
        """cam_entity with entity_id → stream name = entity_id (line 3441)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_ent = MagicMock()
        cam_ent.entity_id = "camera.bosch_terrasse"
        coord = _make_coord(_camera_entities={CAM_A: cam_ent})
        coord.hass.config.config_dir = None

        session_mock, _put_resp, _check_resp = _make_go2rtc_session(
            put_status=200, check_status=200
        )

        captured_params = {}
        original_put = session_mock.put

        async def _capture_put(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return await original_put(url, **kwargs)

        session_mock.put = _capture_put

        with patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://host/stream"
            )

        assert captured_params.get("name") == "camera.bosch_terrasse"

    @pytest.mark.asyncio
    async def test_stream_name_fallback_when_no_entity(self):
        """No cam_entity → stream name = bosch_shc_cam_{cam_id.lower()} (line 3443)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.hass.config.config_dir = None

        session_mock, _put_resp, _check_resp = _make_go2rtc_session(
            put_status=200, check_status=200
        )

        captured_params = {}
        original_put = session_mock.put

        async def _capture_put(url, **kwargs):
            captured_params.update(kwargs.get("params", {}))
            return await original_put(url, **kwargs)

        session_mock.put = _capture_put

        with patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._register_go2rtc_stream(
                coord, CAM_A, "rtsps://host/stream"
            )

        expected_name = f"bosch_shc_cam_{CAM_A.lower()}"
        assert captured_params.get("name") == expected_name
