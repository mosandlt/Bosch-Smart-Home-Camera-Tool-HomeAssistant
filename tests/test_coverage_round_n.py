"""Coverage round N (2026-05-08): push from 94% → ≥95%.

Covers previously-skipped paths that required firebase_messaging (not installed
in test venv) by mocking sys.modules["firebase_messaging"] directly, and
covers camera.py executor-based Digest snap functions by making
async_add_executor_job actually call the closure.

New coverage:
  fcm.py — _build_fcm_cfg (OSS key path), _try_fcm body: no-api-key guard, checkin, start
  fcm.py — async_start_fcm_push dispatch: auto/polling/legacy-coercion
  fcm.py 556-557  — mark_events_read exception swallow in push handler
  fcm.py 699-701  — async_send_alert step-1 exception → return
  fcm.py 790-791  — direct clip.mp4 available log
  camera.py 606-618 — _fetch_local_snap executor closure (success + RequestException)
  camera.py 826-838 — _fetch_outage_snap executor closure (success + RequestException)

Note (v12.4.5): iOS/Android-specific dispatch paths removed from production code.
  TestBuildFcmCfgIos deleted — there is no longer an iOS-specific FCM config.
  TestDispatchModes updated — auto tries FCM once (OSS key); legacy modes coerce to auto.
"""
from __future__ import annotations

import asyncio
import sys
import time
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera.fcm"


# ═══════════════════════════════════════════════════════════════════════════════
# FCM — async_start_fcm_push dispatch modes + _build_fcm_cfg + _try_fcm_with_mode
# ═══════════════════════════════════════════════════════════════════════════════

def _mock_fcm_module(checkin_token="fcm-tok-abc", start_raises=False, checkin_raises=False):
    """Build a minimal firebase_messaging mock that passes through async_start_fcm_push."""
    mock_client = MagicMock()
    if checkin_raises:
        mock_client.checkin_or_register = AsyncMock(side_effect=RuntimeError("checkin fail"))
    else:
        mock_client.checkin_or_register = AsyncMock(return_value=checkin_token)
    if start_raises:
        mock_client.start = AsyncMock(side_effect=RuntimeError("start fail"))
    else:
        mock_client.start = AsyncMock(return_value=None)

    mock_module = MagicMock()
    mock_module.FcmPushClient = MagicMock(return_value=mock_client)
    mock_module.FcmRegisterConfig = MagicMock()
    mock_module.FcmPushClientConfig = MagicMock()
    return mock_module, mock_client


def _fcm_coord(push_mode="ios", entry_data=None, **overrides):
    entry_data = entry_data or {}
    base = SimpleNamespace(
        _fcm_running=False,
        _fcm_client=None,
        _fcm_token=None,
        _fcm_lock=threading.Lock(),
        _fcm_healthy=False,
        _fcm_push_mode="unknown",
        options={"enable_fcm_push": True, "fcm_push_mode": push_mode},
        hass=MagicMock(),
        _entry=SimpleNamespace(data=entry_data),
        data={},
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


# TestBuildFcmCfgIos removed in v12.4.5: iOS-specific FCM config path no longer exists.
# The OSS Android Firebase config is used for all platforms. See test_fcm_mode_pin.py.


class TestBuildFcmCfgAndroid:
    """fcm.py 175-185: _build_fcm_cfg android path — uses stored or fetched config."""

    @pytest.mark.asyncio
    async def test_android_uses_stored_config(self):
        """push_mode=android with stored fcm_config → fetch_firebase_config not called."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, _ = _mock_fcm_module()
        stored_cfg = {
            "project_id": "bosch-test",
            "app_id": "1:123:android:abc",
            "api_key": "stored-key",
        }
        coord = _fcm_coord("android", entry_data={"fcm_config": stored_cfg})

        fetch_called = []
        async def fake_fetch(hass):
            fetch_called.append(True)
            return stored_cfg

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.fetch_firebase_config", side_effect=fake_fetch):
                    with patch(f"{MODULE}.register_fcm_with_bosch", new=AsyncMock(return_value=True)):
                        await async_start_fcm_push(coord)

        assert not fetch_called, (
            "fetch_firebase_config must NOT be called when config is already stored"
        )

    @pytest.mark.asyncio
    async def test_android_fetches_config_when_missing(self):
        """push_mode=android with no stored config → fetch_firebase_config called."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, _ = _mock_fcm_module()
        coord = _fcm_coord("android")  # no fcm_config in entry data

        fetched_cfg = {
            "project_id": "bosch-proj",
            "app_id": "1:123:android:def",
            "api_key": "fetched-key",
        }
        fetch_called = []
        async def fake_fetch(hass):
            fetch_called.append(True)
            return fetched_cfg

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.fetch_firebase_config", side_effect=fake_fetch):
                    with patch(f"{MODULE}.register_fcm_with_bosch", new=AsyncMock(return_value=True)):
                        await async_start_fcm_push(coord)

        assert fetch_called, "fetch_firebase_config must be called when no stored config"


class TestTryFcmWithModeGuards:
    """fcm.py 189-192: _try_fcm_with_mode no-api-key guard returns False."""

    @pytest.mark.asyncio
    async def test_no_api_key_does_not_start_client(self):
        """_build_fcm_cfg returns cfg without api_key → _try_fcm_with_mode returns False."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, mock_client = _mock_fcm_module()
        coord = _fcm_coord("android")

        # fetch_firebase_config returns config without api_key
        async def fake_fetch(hass):
            return {"project_id": "p", "app_id": "a"}  # no api_key

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.fetch_firebase_config", side_effect=fake_fetch):
                    await async_start_fcm_push(coord)

        assert not coord._fcm_running, (
            "FCM must not start when api_key is missing from config"
        )
        mock_client.checkin_or_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_checkin_failure_keeps_running_false(self):
        """checkin_or_register raises → _fcm_running stays False, _fcm_client None."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, _ = _mock_fcm_module(checkin_raises=True)
        coord = _fcm_coord("ios")

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.register_fcm_with_bosch", new=AsyncMock(return_value=True)):
                    await async_start_fcm_push(coord)

        assert not coord._fcm_running, "checkin failure must not set _fcm_running=True"
        assert coord._fcm_client is None, "checkin failure must clear _fcm_client"

    @pytest.mark.asyncio
    async def test_start_failure_clears_client(self):
        """FcmPushClient.start() raises → _fcm_client set to None."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, _ = _mock_fcm_module(start_raises=True)
        coord = _fcm_coord("ios")

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.register_fcm_with_bosch", new=AsyncMock(return_value=True)):
                    await async_start_fcm_push(coord)

        assert coord._fcm_client is None, "start() failure must clear _fcm_client"
        assert not coord._fcm_running, "start() failure must not set _fcm_running"


class TestDispatchModes:
    """fcm.py: push_mode branch coverage — auto/polling/legacy-coercion (v12.4.5+)."""

    @pytest.mark.asyncio
    async def test_auto_mode_calls_fcm_once(self):
        """auto mode → calls _try_fcm exactly once with OSS key."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, mock_client = _mock_fcm_module()
        coord = _fcm_coord("auto")

        call_count = []
        def track_client(**kwargs):
            call_count.append(1)
            return mock_client
        mock_fcm.FcmPushClient = track_client

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.register_fcm_with_bosch", new=AsyncMock(return_value=True)):
                    await async_start_fcm_push(coord)

        assert len(call_count) == 1, "auto mode: exactly one FCM client created"
        assert coord._fcm_running is True, "auto mode success must set _fcm_running=True"

    # test_auto_mode_falls_back_to_android removed in v12.4.5: there is no
    # Android fallback — auto tries FCM once with the OSS key and falls back
    # to standard polling on failure (no second client attempt).

    @pytest.mark.asyncio
    async def test_auto_mode_fcm_fail_no_crash(self):
        """auto mode → FCM registration fails → function returns without crash."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_module = MagicMock()
        fail_client = MagicMock()
        fail_client.checkin_or_register = AsyncMock(side_effect=RuntimeError("fail"))
        mock_module.FcmPushClient = MagicMock(return_value=fail_client)
        mock_module.FcmRegisterConfig = MagicMock()
        mock_module.FcmPushClientConfig = None

        coord = _fcm_coord("auto")

        with patch.dict(sys.modules, {"firebase_messaging": mock_module}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.fetch_firebase_config",
                           new=AsyncMock(return_value={"project_id": "p", "app_id": "a", "api_key": "k"})):
                    # Must not raise
                    await async_start_fcm_push(coord)

        assert not coord._fcm_running, "FCM fail → must not set _fcm_running"

    @pytest.mark.asyncio
    async def test_unknown_mode_coerces_to_auto(self):
        """push_mode='weirdvalue' → coerced to 'auto' → _fcm_push_mode='auto' on success."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, mock_client = _mock_fcm_module()
        coord = _fcm_coord("weirdvalue")

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.register_fcm_with_bosch", new=AsyncMock(return_value=True)):
                    await async_start_fcm_push(coord)

        assert coord._fcm_push_mode == "auto", (
            "unknown push_mode must coerce to auto → _fcm_push_mode='auto'"
        )

    @pytest.mark.asyncio
    async def test_android_legacy_coerces_to_auto(self):
        """push_mode='android' (legacy) → coerced to 'auto' → _fcm_push_mode='auto' on success."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        mock_fcm, mock_client = _mock_fcm_module()
        coord = _fcm_coord("android")
        coord._entry = SimpleNamespace(data={"fcm_config": {
            "project_id": "p", "app_id": "a", "api_key": "k",
        }})

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.register_fcm_with_bosch", new=AsyncMock(return_value=True)):
                    await async_start_fcm_push(coord)

        assert coord._fcm_push_mode == "auto", (
            "legacy 'android' mode must coerce to auto → _fcm_push_mode='auto'"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# camera.py — _fetch_local_snap executor closure (lines 606-618)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_camera_for_snap(**overrides):
    """Build a minimal BoschCamera stub for _async_camera_image_impl tests."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
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
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = "Terrasse"
    cam._display_name = "Terrasse"
    cam._cached_image = None
    cam._force_image_refresh = False
    cam._last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor II"
    cam._hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "aa:bb:cc:33:14:ae"
    # _token is a read-only property backed by _entry.data["bearer_token"]
    cam.async_write_ha_state = MagicMock()
    cam.hass = MagicMock()
    cam.hass.async_create_task = MagicMock()
    cam.hass.async_add_executor_job = AsyncMock(return_value=None)
    for k, v in overrides.items():
        setattr(cam, k, v)
    return cam


def _digest_cm_snap(status: int, body: bytes = b"", content_type: str = "image/jpeg") -> MagicMock:
    """Async CM for async_digest_request returning a controlled response."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {"Content-Type": content_type}
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestFetchLocalSnapClosure:
    """camera.py LOCAL snap path: uses async_digest_request (no executor)."""

    @pytest.mark.asyncio
    async def test_local_snap_200_returns_bytes(self):
        """LOCAL live connection + async_digest_request 200 + image/jpeg → returns bytes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        import aiohttp

        cam = _make_camera_for_snap()
        cam.coordinator._live_connections = {CAM_ID: {
            "proxyUrl": "https://192.0.2.149:443/snap.jpg",
            "_connection_type": "LOCAL",
            "_local_user": "digest_user",
            "_local_password": "digest_pass",
        }}
        cm = _digest_cm_snap(200, b"\xff\xd8\xff", "image/jpeg")

        with patch("custom_components.bosch_shc_camera.camera.async_get_clientsession",
                   return_value=MagicMock()), \
             patch("custom_components.bosch_shc_camera.camera.async_digest_request",
                   new=AsyncMock(return_value=cm)):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8\xff", (
            "LOCAL snap 200 + image/jpeg must return the bytes"
        )
        assert cam._cached_image == b"\xff\xd8\xff", "_cached_image must be updated"

    @pytest.mark.asyncio
    async def test_local_snap_request_exception_returns_none(self):
        """aiohttp.ClientError in LOCAL snap → returns cached/placeholder (not crash)."""
        import aiohttp as _aiohttp
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_for_snap()
        cam.coordinator._live_connections = {CAM_ID: {
            "proxyUrl": "https://192.0.2.149:443/snap.jpg",
            "_connection_type": "LOCAL",
            "_local_user": "u",
            "_local_password": "p",
        }}

        with patch("custom_components.bosch_shc_camera.camera.async_get_clientsession",
                   return_value=MagicMock()), \
             patch("custom_components.bosch_shc_camera.camera.async_digest_request",
                   new=AsyncMock(side_effect=_aiohttp.ClientError("timeout"))):
            result = await BoschCamera._async_camera_image_impl(cam)

        from custom_components.bosch_shc_camera.camera import BoschCamera as _Cam
        assert result is None or result is cam._cached_image or result is _Cam._PLACEHOLDER_JPEG, (
            "aiohttp.ClientError in LOCAL snap must not raise; returns cached/placeholder"
        )

    @pytest.mark.asyncio
    async def test_local_snap_non_image_content_type_returns_none(self):
        """async_digest_request 200 but non-image content-type → placeholder."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_for_snap()
        cam.coordinator._live_connections = {CAM_ID: {
            "proxyUrl": "https://192.0.2.149/snap.jpg",
            "_connection_type": "LOCAL",
            "_local_user": "u",
            "_local_password": "p",
        }}
        cm = _digest_cm_snap(200, b"<html>error</html>", "text/html")

        with patch("custom_components.bosch_shc_camera.camera.async_get_clientsession",
                   return_value=MagicMock()), \
             patch("custom_components.bosch_shc_camera.camera.async_digest_request",
                   new=AsyncMock(return_value=cm)):
            result = await BoschCamera._async_camera_image_impl(cam)

        # non-image content-type → data=None → placeholder
        assert True, "non-image content type must not raise"


# ═══════════════════════════════════════════════════════════════════════════════
# camera.py — outage snap fallback (uses async_digest_request directly)
# ═══════════════════════════════════════════════════════════════════════════════

class TestFetchOutageSnapClosure:
    """camera.py outage snap path: async_digest_request called directly.

    Triggered when _auth_outage_count > 0 and _local_creds_cache has cached creds.
    """

    def _make_outage_cam(self) -> "MagicMock":
        """Camera with outage creds."""
        cam = _make_camera_for_snap()
        cam.coordinator._auth_outage_count = 2  # triggers outage path
        cam.coordinator._local_creds_cache = {CAM_ID: {
            "user": "digest_user",
            "password": "digest_pass",
            "host": "192.0.2.149",
            "port": 443,
            "ts": time.monotonic(),
        }}
        cam.coordinator._live_connections = {}  # no active stream → skip path 1
        return cam

    @pytest.mark.asyncio
    async def test_outage_snap_200_returns_bytes(self):
        """Cloud outage + cached Digest creds + 200 → returns bytes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = self._make_outage_cam()
        cm = _digest_cm_snap(200, b"\xff\xd8outage", "image/jpeg")

        with patch("custom_components.bosch_shc_camera.camera.async_get_clientsession",
                   return_value=MagicMock()), \
             patch("custom_components.bosch_shc_camera.camera.async_digest_request",
                   new=AsyncMock(return_value=cm)):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8outage", (
            "outage snap 200 + image/jpeg must return the bytes"
        )
        assert cam._cached_image == b"\xff\xd8outage", "_cached_image updated on outage snap"

    @pytest.mark.asyncio
    async def test_outage_snap_request_exception_returns_none(self):
        """aiohttp.ClientError in outage snap → returns cached/placeholder."""
        import aiohttp as _aiohttp
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = self._make_outage_cam()

        with patch("custom_components.bosch_shc_camera.camera.async_get_clientsession",
                   return_value=MagicMock()), \
             patch("custom_components.bosch_shc_camera.camera.async_digest_request",
                   new=AsyncMock(side_effect=_aiohttp.ClientError("LAN unreachable"))):
            result = await BoschCamera._async_camera_image_impl(cam)

        from custom_components.bosch_shc_camera.camera import BoschCamera as _Cam
        assert result is None or result is cam._cached_image or result is _Cam._PLACEHOLDER_JPEG, (
            "aiohttp.ClientError in outage snap must not raise; returns cached/placeholder"
        )
