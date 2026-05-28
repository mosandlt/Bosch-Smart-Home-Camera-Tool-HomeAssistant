"""Tests for camera.py coverage gaps (v13.3.0 sprint).

Covers:
  - extra_state_attributes: camera_timestamp_overlay branch (lines 472-475)
    When _timestamp_cache contains a value for the cam_id, the attribute must
    be included. When the key is absent or the value is None, it must be omitted.
  - _async_camera_image_impl: cached image fallback (line 1039)
    When cloud+LAN snapshots return None but _cached_image is set, the cache
    must be returned as the fallback image.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "00000000-0000-0000-0000-000000000001"


def _make_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "priority": 1.0,
                },
                "status": "ONLINE",
                "events": [],
                "live": {"rtspsUrl": "rtsps://cam/stream"},
            }
        },
        _live_connections={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_warming=set(),
        _audio_enabled={CAM_ID: True},
        _local_creds_cache={},
        _live_opened_at={},
        _image_rotation_180={},
        _shc_state_cache={},
        _timestamp_cache={},
        _auth_outage_count=0,
        last_update_success=True,
        token="tok-A",
        options={},
        is_camera_online=lambda cid: True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_entity(coord=None, **overrides):
    """Minimal BoschCamera-like stub for testing static methods and properties."""
    coord = coord or _make_coord()
    entry = SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "tok-A"},
        options={"live_buffer_mode": "balanced"},
    )
    base = dict(
        coordinator=coord,
        _cam_id=CAM_ID,
        _entry=entry,
        _attr_name="Bosch Terrasse",
        _display_name="Bosch Terrasse",
        _cam_title="Terrasse",
        _model="HOME_Eyes_Outdoor",
        _model_name="Eyes Outdoor II",
        _fw="9.40.25",
        _mac="aa:bb:cc:dd:ee:01",
        _hw_version="HOME_Eyes_Outdoor",
        _cached_image=None,
        _last_image_fetch=0.0,
        _force_image_refresh=False,
        is_streaming=False,
        stream_options={},
    )
    base.update(overrides)
    obj = SimpleNamespace(**base)
    obj._cam_data = coord.data[CAM_ID]
    return obj


def _make_camera(coord=None, **overrides):
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
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor II"
    cam._hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "aa:bb:cc:dd:ee:01"
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
        async_add_executor_job=AsyncMock(),
    )
    for k, v in overrides.items():
        setattr(cam, k, v)
    return cam


# ── extra_state_attributes: camera_timestamp_overlay ────────────────────────


class TestCameraTimestampOverlayAttr:
    """Lines 472-475: The card hides its own timestamp pill when the camera
    burns in its own on-screen clock. The attribute must be exposed when the
    cache contains a truthy or falsy value, and omitted when no cache entry
    exists."""

    def _get_attrs(self, entity):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"auto_play_default": "lan"},
        ):
            return BoschCamera.extra_state_attributes.fget(entity)

    def test_timestamp_overlay_true_when_cache_value_is_true(self):
        """When _timestamp_cache[cam_id]=True the attribute must be True."""
        coord = _make_coord(_timestamp_cache={CAM_ID: True})
        entity = _stub_entity(coord=coord)
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" in attrs, (
            "camera_timestamp_overlay must be present when cache has a value"
        )
        assert attrs["camera_timestamp_overlay"] is True

    def test_timestamp_overlay_false_when_cache_value_is_false(self):
        """When _timestamp_cache[cam_id]=False the attribute must be False."""
        coord = _make_coord(_timestamp_cache={CAM_ID: False})
        entity = _stub_entity(coord=coord)
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" in attrs
        assert attrs["camera_timestamp_overlay"] is False

    def test_timestamp_overlay_absent_when_cache_has_no_entry(self):
        """When _timestamp_cache has no entry for this cam_id the attribute
        must be omitted entirely (not False/None)."""
        coord = _make_coord(_timestamp_cache={})
        entity = _stub_entity(coord=coord)
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" not in attrs, (
            "camera_timestamp_overlay must be absent when no cache entry exists"
        )

    def test_timestamp_overlay_absent_when_cache_value_is_none(self):
        """When _timestamp_cache[cam_id]=None the attribute must also be omitted."""
        coord = _make_coord(_timestamp_cache={CAM_ID: None})
        entity = _stub_entity(coord=coord)
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" not in attrs, (
            "camera_timestamp_overlay must be absent when cache value is None"
        )

    def test_timestamp_overlay_absent_when_no_timestamp_cache_attr(self):
        """Defensive: if coordinator lacks _timestamp_cache entirely (legacy
        coordinator loaded from old snapshot), the attribute must be absent."""
        coord = _make_coord()
        del coord._timestamp_cache  # simulate missing attribute
        entity = _stub_entity(coord=coord)
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" not in attrs


# ── _async_camera_image_impl: cached image fallback (line 1039) ──────────────


class TestAsyncCameraImageImplCachedImageFallback:
    """Line 1039: When cloud+LAN snapshots both return None (e.g. CAMERA_360
    with auth-required snap.jpg endpoint), _cached_image must be returned as
    the fallback so the Lovelace card still shows the last-known frame.

    This covers the `if self._cached_image: return self._cached_image` branch.
    """

    @pytest.mark.asyncio
    async def test_returns_cached_image_when_streaming_but_no_proxy_url(self):
        """Camera is streaming (rtspsUrl set) but proxyUrl absent →
        section 1 skipped (no proxyUrl), section 2 skipped (is_streaming=True),
        section 2b skipped (no outage creds), falls through to section 3 (line 1039).

        This path occurs when the live connection has the RTSP URL ready but the
        snap.jpg proxy URL hasn't been refreshed yet (brief window on reconnect).
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _make_coord()
        # Live connection with rtspsUrl → is_streaming=True, but NO proxyUrl → section 1 skipped
        coord._live_connections = {
            CAM_ID: {
                "rtspsUrl": "rtsps://192.168.1.1/stream",
                # No proxyUrl → proxy_url = "" → section 1 not entered
            }
        }
        coord._local_creds_cache = {}               # no outage creds → 2b skipped
        coord._auth_outage_count = 0
        coord.data[CAM_ID]["events"] = []

        cam = _make_camera(coord=coord)
        cam._cached_image = b"\xff\xd8\xff\xe0cached_frame"
        cam._was_streaming = False
        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
            async_add_executor_job=AsyncMock(),
        )
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"use_mjpeg_snapshot": False},
        ), patch(
            "custom_components.bosch_shc_camera.camera.async_get_clientsession",
            return_value=MagicMock(),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8\xff\xe0cached_frame", (
            "Must return _cached_image when streaming but no proxyUrl (section 3, line 1039)"
        )
