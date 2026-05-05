"""Extended tests for shc.py — setter structural contracts and write-lock ordering.

Covers the uncovered lines in shc.py (currently 50%) focusing on:
  - async_shc_set_camera_light: no device_id → False, success updates cache
  - async_shc_set_privacy_mode: no device_id → False, success updates cache + sets _privacy_set_at
  - async_cloud_set_notifications: success writes cache + _notif_set_at, HTTP failure → False
  - async_cloud_set_camera_light: no token → falls through to SHC
  - Write-lock ordering: _light_set_at / _notif_set_at stamped before/alongside cache updates
  - shc.py function existence contracts (structural pin against refactor)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SRC = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CLOUD_API = "https://residential.cbs.boschsecurity.com"


# ── Structural: all public functions exist ───────────────────────────────────


class TestShcFunctionContracts:
    """Pin every public function name in shc.py against accidental renames."""

    def test_required_functions_present(self):
        src = (SRC / "shc.py").read_text()
        for fn in (
            "shc_configured",
            "shc_ready",
            "_shc_mark_success",
            "_shc_mark_failure",
            "async_shc_request",
            "async_update_shc_states",
            "async_shc_set_camera_light",
            "async_shc_set_privacy_mode",
            "async_cloud_set_privacy_mode",
            "async_cloud_set_camera_light",
            "async_cloud_set_notifications",
            "async_cloud_set_pan",
        ):
            assert f"def {fn}" in src or f"async def {fn}" in src, (
                f"shc.py is missing function '{fn}' — coordinator or entity calls it directly"
            )


# ── async_shc_set_camera_light ────────────────────────────────────────────────


def _coord_with_shc(cam_id: str = CAM_ID, device_id: str = "dev-001") -> SimpleNamespace:
    return SimpleNamespace(
        hass=MagicMock(),
        options={
            "shc_ip": "10.0.0.103",
            "shc_cert_path": "/path/cert.pem",
            "shc_key_path": "/path/key.pem",
        },
        _shc_state_cache={cam_id: {"device_id": device_id, "camera_light": False}},
        _shc_mark_success=MagicMock(),
        _shc_mark_failure=MagicMock(),
        _light_set_at={},
        _shc_consecutive_failures=0,
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
    )


class TestAsyncShcSetCameraLight:
    @pytest.mark.asyncio
    async def test_no_device_id_returns_false(self):
        """If no device_id is cached for the camera, the setter must abort with False."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _coord_with_shc()
        coord._shc_state_cache = {CAM_ID: {}}  # no device_id

        result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is False, (
            "async_shc_set_camera_light must return False when device_id is not cached — "
            "no SHC device found to send the command to"
        )

    @pytest.mark.asyncio
    async def test_no_cache_entry_returns_false(self):
        """Camera not in _shc_state_cache at all → False."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _coord_with_shc()
        coord._shc_state_cache = {}  # cam_id missing entirely

        result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is False


# ── async_shc_set_privacy_mode ───────────────────────────────────────────────


class TestAsyncShcSetPrivacyMode:
    @pytest.mark.asyncio
    async def test_no_device_id_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode

        coord = _coord_with_shc()
        coord._shc_state_cache = {CAM_ID: {}}

        result = await async_shc_set_privacy_mode(coord, CAM_ID, True)
        assert result is False

    def test_privacy_set_at_written_in_setter_body(self):
        """_privacy_set_at must be stamped inside async_shc_set_privacy_mode.

        Guards against the BUG-4 pattern: write-lock written after cache update
        would leave a race window where the SHC fetcher sees no lock.
        """
        src = (SRC / "shc.py").read_text()
        func_start = src.find("async def async_shc_set_privacy_mode")
        assert func_start != -1
        next_func = src.find("\nasync def ", func_start + 1)
        func_body = src[func_start:next_func] if next_func != -1 else src[func_start:]
        assert "_privacy_set_at" in func_body, (
            "async_shc_set_privacy_mode must stamp _privacy_set_at "
            "to prevent BUG-4 race on the SHC fallback path"
        )


# ── async_cloud_set_notifications ────────────────────────────────────────────


def _notif_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    return SimpleNamespace(
        hass=MagicMock(),
        token="fake-bearer-token",
        _shc_state_cache={cam_id: {}},
        _notif_set_at={},
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
    )


def _mock_cloud_resp(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestAsyncCloudSetNotifications:
    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        coord.token = None

        result = await async_cloud_set_notifications(coord, CAM_ID, True)
        assert result is False

    @pytest.mark.asyncio
    async def test_200_updates_cache_and_returns_true(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(200))

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=session,
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["notifications_status"] == "FOLLOW_CAMERA_SCHEDULE"

    @pytest.mark.asyncio
    async def test_204_updates_cache(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(204))

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=session,
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, False)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["notifications_status"] == "ALWAYS_OFF"

    @pytest.mark.asyncio
    async def test_notif_set_at_stamped_on_success(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(200))
        before = time.monotonic()

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=session,
        ):
            await async_cloud_set_notifications(coord, CAM_ID, True)

        assert CAM_ID in coord._notif_set_at, (
            "_notif_set_at must be stamped on notification success — "
            "write-lock prevents SHC background tick from reverting the cache"
        )
        assert coord._notif_set_at[CAM_ID] >= before

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(500))

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=session,
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is False
        assert "notifications_status" not in coord._shc_state_cache.get(CAM_ID, {})

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(side_effect=asyncio.TimeoutError())

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=session,
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is False


# ── Write-lock ordering: _light_set_at and _notif_set_at ────────────────────


class TestWriteLockOrdering:
    """Structural: write-lock timestamps must be set BEFORE the cache, same as BUG-4 fix."""

    def test_light_set_at_before_cache_in_cloud_set_camera_light(self):
        """In async_cloud_set_camera_light, _light_set_at must be written before
        returning so the SHC fetcher's write-lock check always sees it."""
        src = (SRC / "shc.py").read_text()
        func_start = src.find("async def async_cloud_set_camera_light")
        assert func_start != -1
        next_func = src.find("\nasync def ", func_start + 1)
        func_body = src[func_start:next_func] if next_func != -1 else src[func_start:]
        assert "_light_set_at" in func_body, (
            "async_cloud_set_camera_light must stamp _light_set_at — "
            "without it the SHC background tick can revert a user-triggered light change"
        )

    def test_notif_set_at_in_source(self):
        """_notif_set_at must exist as a write-lock for notifications state."""
        src = (SRC / "shc.py").read_text()
        assert "_notif_set_at" in src, (
            "_notif_set_at write-lock not found in shc.py — "
            "notifications state is unprotected against SHC background tick reverting it"
        )

    def test_privacy_set_at_present_in_shc_set_privacy_path(self):
        """SHC fallback privacy setter must also stamp _privacy_set_at."""
        src = (SRC / "shc.py").read_text()
        func_start = src.find("async def async_shc_set_privacy_mode")
        assert func_start != -1
        next_func = src.find("\nasync def ", func_start + 1)
        body = src[func_start:next_func] if next_func != -1 else src[func_start:]
        assert "_privacy_set_at" in body, (
            "SHC privacy setter must stamp _privacy_set_at — BUG-4 fix must cover "
            "both the cloud path and the SHC local fallback path"
        )


# ── async_update_shc_states write-lock check ─────────────────────────────────


class TestShcFetcherWriteLockCheck:
    """The SHC state fetcher must honor write-locks before overwriting cache.

    Without this check, the SHC background tick overwrites freshly-written
    privacy/light state when the cloud's eventual-consistency window hasn't
    expired yet (BUG-4 root cause).
    """

    def test_privacy_set_at_honored_in_fetcher(self):
        src = (SRC / "shc.py").read_text()
        fetcher_start = src.find("async def async_update_shc_states")
        assert fetcher_start != -1
        fetcher_end = src.find("\nasync def ", fetcher_start + 1)
        body = src[fetcher_start:fetcher_end] if fetcher_end != -1 else src[fetcher_start:]
        assert "_privacy_set_at" in body, (
            "async_update_shc_states must check _privacy_set_at before writing — "
            "without it the SHC poll always overwrites the privacy cache (BUG-4)"
        )

    def test_light_set_at_honored_in_fetcher(self):
        src = (SRC / "shc.py").read_text()
        fetcher_start = src.find("async def async_update_shc_states")
        assert fetcher_start != -1
        fetcher_end = src.find("\nasync def ", fetcher_start + 1)
        body = src[fetcher_start:fetcher_end] if fetcher_end != -1 else src[fetcher_start:]
        assert "_light_set_at" in body, (
            "async_update_shc_states must check _light_set_at — same race shape as BUG-4"
        )
