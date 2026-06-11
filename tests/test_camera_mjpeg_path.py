"""Integration tests for the MJPEG inst=3 snapshot path in camera.py.

Tests verify that BoschCamera._async_camera_image_impl:
  - Uses the MJPEG path when use_mjpeg_snapshot=True + Gen2 + LAN creds present
  - Skips MJPEG path and falls through when use_mjpeg_snapshot=False (default)
  - Skips MJPEG path when the camera is Gen1 (generation < 2)
  - Skips MJPEG path when LOCAL creds are missing from the cache
  - Falls back to existing paths when MJPEG returns None
  - Returns MJPEG result and caches it when MJPEG succeeds

Per PIN_EVERY_MODE rule: one test per discrete state.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20 + b"\xff\xd9"
PLACEHOLDER_JPEG = b"\xff\xd8placeholder"


def _make_coord(
    *,
    local_creds: dict | None = None,
    live_connections: dict | None = None,
    auth_outage_count: int = 0,
    **extra: object,
) -> SimpleNamespace:
    base: dict = dict(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"},
                "events": [],
                "live": {},
            },
        },
        _live_connections=live_connections if live_connections is not None else {},
        _live_opened_at={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        _shc_state_cache={},
        _stream_warming=set(),
        _image_rotation_180={},
        _local_creds_cache=(
            {CAM_ID: {**local_creds, "ts": local_creds.get("ts", time.monotonic())}}
            if local_creds
            else {}
        ),
        _timestamp_cache={},
        _audio_enabled={},
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        async_fetch_fresh_event_snapshot=AsyncMock(return_value=None),
        _auth_outage_count=auth_outage_count,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _make_camera(
    coord: SimpleNamespace | None = None,
    hw_version: str = "HOME_Eyes_Outdoor",
    opts: dict | None = None,
    **camera_overrides: object,
) -> object:
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(
        data={"bearer_token": "tok"},
        options=opts or {},
    )
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
    cam._cached_image = None
    cam._force_image_refresh = False
    cam._last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = hw_version
    cam._model_name = "Eyes Außenkamera II"
    cam._hw_version = hw_version
    cam._fw = "9.40.102"
    cam._mac = ""
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(
        data={},  # required by async_get_clientsession
        async_create_task=MagicMock(
            side_effect=lambda c: (
                asyncio.ensure_future(c) if asyncio.iscoroutine(c) else MagicMock()
            )
        ),
        async_add_executor_job=AsyncMock(side_effect=lambda fn, *a: fn(*a)),
    )
    for k, v in camera_overrides.items():
        setattr(cam, k, v)
    return cam


# ── Core pin tests ─────────────────────────────────────────────────────────────


def _patch_session() -> object:
    """Patch async_get_bosch_cloud_session so camera.py can call it without a real hass."""
    return patch(
        "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
        new=AsyncMock(return_value=MagicMock()),
    )


class TestMjpegPathEnabled:
    """use_mjpeg_snapshot=True, Gen2, creds present."""

    @pytest.mark.asyncio
    async def test_mjpeg_success_returns_jpeg_and_caches(self):
        """MJPEG fetch succeeds → return bytes, cache updated."""
        local_creds = {
            "user": "cbs-TEST1234",
            "password": "secret",
            "host": "192.0.2.149",
            "port": 443,
        }
        coord = _make_coord(local_creds=local_creds)
        cam = _make_camera(coord=coord, opts={"use_mjpeg_snapshot": True})

        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=AsyncMock(return_value=FAKE_JPEG),
            ),
        ):
            result = await cam._async_camera_image_impl()

        assert result == FAKE_JPEG
        assert cam._cached_image == FAKE_JPEG

    @pytest.mark.asyncio
    async def test_mjpeg_failure_falls_through_to_existing_paths(self):
        """MJPEG returns None → fall through; existing snapshot path called."""
        local_creds = {
            "user": "cbs-TEST1234",
            "password": "secret",
            "host": "192.0.2.149",
            "port": 443,
        }
        coord = _make_coord(local_creds=local_creds)
        cam = _make_camera(coord=coord, opts={"use_mjpeg_snapshot": True})
        # Seed a cached image so there is something to fall back to
        cam._cached_image = b"\xff\xd8fallback"
        cam._last_image_fetch = time.monotonic()  # not stale

        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=AsyncMock(return_value=None),
            ),
        ):
            result = await cam._async_camera_image_impl()

        # The existing path (cached image) should be returned
        assert result == b"\xff\xd8fallback"

    @pytest.mark.asyncio
    async def test_mjpeg_called_with_correct_args(self):
        """fetch_mjpeg_snapshot receives host/port/user/pass from cached creds."""
        local_creds = {
            "user": "cbs-VERIFY",
            "password": "mypassword",
            "host": "192.0.2.149",
            "port": 443,
        }
        coord = _make_coord(local_creds=local_creds)
        cam = _make_camera(coord=coord, opts={"use_mjpeg_snapshot": True})

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_called_once()
        call_kwargs = mock_fetch.call_args
        # Positional: (cam_host, cam_port, user, password)
        args = call_kwargs[0]
        assert args[0] == "192.0.2.149"
        assert args[1] == 443
        assert args[2] == "cbs-VERIFY"
        assert args[3] == "mypassword"


class TestMjpegPathDisabled:
    """use_mjpeg_snapshot=False (default) → MJPEG never called."""

    @pytest.mark.asyncio
    async def test_mjpeg_skipped_when_option_false(self):
        """Default off → fetch_mjpeg_snapshot not called."""
        local_creds = {
            "user": "cbs-TEST",
            "password": "secret",
            "host": "192.0.2.149",
            "port": 443,
        }
        coord = _make_coord(local_creds=local_creds)
        cam = _make_camera(coord=coord, opts={"use_mjpeg_snapshot": False})
        cam._cached_image = b"\xff\xd8old"
        cam._last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_mjpeg_skipped_when_option_absent_but_no_creds(self):
        """Default option is True since v13.2.0, but missing LAN creds in coordinator
        still gates the MJPEG path — fetch never called when creds cache empty."""
        coord = _make_coord()  # no creds populated
        cam = _make_camera(coord=coord, opts={})
        cam._cached_image = b"\xff\xd8old"
        cam._last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_not_called()


class TestMjpegPathGen1:
    """Gen1 cameras → MJPEG path skipped regardless of option."""

    @pytest.mark.asyncio
    async def test_gen1_indoor_skips_mjpeg(self):
        """INDOOR (Gen1 360) → not called even with option=True."""
        local_creds = {
            "user": "cbs-GEN1",
            "password": "secret",
            "host": "192.0.2.21",
            "port": 443,
        }
        coord = _make_coord(local_creds=local_creds)
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "INDOOR"
        cam = _make_camera(
            coord=coord,
            hw_version="INDOOR",
            opts={"use_mjpeg_snapshot": True},
        )
        cam._cached_image = b"\xff\xd8old"
        cam._last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_gen1_outdoor_skips_mjpeg(self):
        """OUTDOOR (Gen1 Eyes) → not called even with option=True."""
        local_creds = {
            "user": "cbs-GEN1",
            "password": "secret",
            "host": "192.0.2.27",
            "port": 443,
        }
        coord = _make_coord(local_creds=local_creds)
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "OUTDOOR"
        cam = _make_camera(
            coord=coord,
            hw_version="OUTDOOR",
            opts={"use_mjpeg_snapshot": True},
        )
        cam._cached_image = b"\xff\xd8old"
        cam._last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_not_called()


class TestMjpegPathMissingCreds:
    """No cached local creds → MJPEG path skipped."""

    @pytest.mark.asyncio
    async def test_no_creds_skips_mjpeg(self):
        """Empty _local_creds_cache → fetch_mjpeg_snapshot not called."""
        coord = _make_coord(local_creds=None)
        cam = _make_camera(
            coord=coord,
            hw_version="HOME_Eyes_Outdoor",
            opts={"use_mjpeg_snapshot": True},
        )
        cam._cached_image = b"\xff\xd8old"
        cam._last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_creds_missing_host_skips_mjpeg(self):
        """Creds dict present but host is empty → MJPEG skipped."""
        local_creds = {"user": "cbs-X", "password": "pw", "host": "", "port": 443}
        coord = _make_coord(local_creds=local_creds)
        cam = _make_camera(
            coord=coord,
            hw_version="HOME_Eyes_Outdoor",
            opts={"use_mjpeg_snapshot": True},
        )
        cam._cached_image = b"\xff\xd8old"
        cam._last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_creds_missing_user_skips_mjpeg(self):
        """Creds dict present but user is empty → MJPEG skipped."""
        local_creds = {"user": "", "password": "pw", "host": "192.0.2.149", "port": 443}
        coord = _make_coord(local_creds=local_creds)
        cam = _make_camera(
            coord=coord,
            hw_version="HOME_Eyes_Outdoor",
            opts={"use_mjpeg_snapshot": True},
        )
        cam._cached_image = b"\xff\xd8old"
        cam._last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            await cam._async_camera_image_impl()

        mock_fetch.assert_not_called()


class TestMjpegGen2Indoor:
    """HOME_Eyes_Indoor (Gen2) also qualifies for MJPEG path."""

    @pytest.mark.asyncio
    async def test_gen2_indoor_uses_mjpeg_when_enabled(self):
        """HOME_Eyes_Indoor is Gen2 → MJPEG path activated when option=True."""
        local_creds = {
            "user": "cbs-INDOOR",
            "password": "secret",
            "host": "192.0.2.150",
            "port": 443,
        }
        coord = _make_coord(local_creds=local_creds)
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        cam = _make_camera(
            coord=coord,
            hw_version="HOME_Eyes_Indoor",
            opts={"use_mjpeg_snapshot": True},
        )

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            result = await cam._async_camera_image_impl()

        mock_fetch.assert_called_once()
        assert result == FAKE_JPEG
