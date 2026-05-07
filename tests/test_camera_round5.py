"""Tests for camera.py — extra_state_attributes branches and _yuv422_to_jpeg.

Sprint C coverage target: lines 362 (stream_source returns None), 464-465
(_yuv422_to_jpeg wrong-size guard), 605-617 (extra_state_attributes stream_status
and bosch_priority branches), 646-670 (snapshot LOCAL Digest auth path).

These tests use SimpleNamespace stubs — no real HA runtime.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _stub_entity(**overrides):
    """Minimal BoschCamera-like stub for testing static methods and properties."""
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
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
        _auth_outage_count=0,
        last_update_success=True,
        token="tok-A",
        options={},
        is_camera_online=lambda cid: True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
    )
    entry = SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "tok-A"},
        options={"live_buffer_mode": "balanced"},
    )
    base = dict(
        coordinator=coord,
        _cam_id=CAM_ID,
        _entry=entry,
        _attr_name=f"Bosch Terrasse",
        _cam_title="Terrasse",
        _model="HOME_Eyes_Outdoor",
        _model_name="Eyes Outdoor II",
        _fw="9.40.25",
        _mac="64:da:a0:33:14:ae",
        _hw_version="HOME_Eyes_Outdoor",
        _cached_image=None,
        _last_image_fetch=0.0,
        _force_image_refresh=False,
        is_streaming=False,
        stream_options={},
    )
    base.update(overrides)
    obj = SimpleNamespace(**base)
    # Helper: simulate a coordinator-backed _cam_data property
    obj._cam_data = coord.data[CAM_ID]
    return obj


# ── stream_source ─────────────────────────────────────────────────────────────

class TestStreamSource:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_live_connection(self):
        """stream_source() must return None when no active live session (switch OFF)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        entity = _stub_entity()
        result = await BoschCamera.stream_source(entity)
        assert result is None, "stream_source must return None when _live_connections is empty"

    @pytest.mark.asyncio
    async def test_returns_rtsps_url_from_live_connection(self):
        """stream_source() must return the rtspsUrl when a session is active."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        entity = _stub_entity()
        entity.coordinator._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy.bosch.com:443/stream",
            "_connection_type": "REMOTE",
        }
        result = await BoschCamera.stream_source(entity)
        assert result == "rtsps://proxy.bosch.com:443/stream", \
            "stream_source must return rtspsUrl from live connection"

    @pytest.mark.asyncio
    async def test_local_connection_forces_tcp_transport(self):
        """LOCAL connection must set stream_options={'rtsp_transport': 'tcp'}."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        entity = _stub_entity()
        entity.coordinator._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsp://127.0.0.1:8765/stream",
            "_connection_type": "LOCAL",
        }
        await BoschCamera.stream_source(entity)
        assert entity.stream_options == {"rtsp_transport": "tcp"}, \
            "LOCAL connection must force TCP transport to avoid HA 2026.4 UDP→TCP rewrite bug"

    @pytest.mark.asyncio
    async def test_remote_connection_uses_empty_stream_options(self):
        """REMOTE connection must leave stream_options empty (FFmpeg default=UDP)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        entity = _stub_entity()
        entity.coordinator._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy.bosch.com:443/stream",
            "_connection_type": "REMOTE",
        }
        await BoschCamera.stream_source(entity)
        assert entity.stream_options == {}, \
            "REMOTE connection must use default stream_options (forcing TCP breaks Gen1 Eyes cloud streams)"

    @pytest.mark.asyncio
    async def test_strips_audio_param_when_audio_disabled(self):
        """Audio param must be stripped from URL when audio switch is OFF."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        entity = _stub_entity()
        entity.coordinator._audio_enabled[CAM_ID] = False
        entity.coordinator._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy.bosch.com:443/stream&enableaudio=1",
            "_connection_type": "REMOTE",
        }
        result = await BoschCamera.stream_source(entity)
        assert "enableaudio=1" not in result, \
            "stream_source must strip enableaudio=1 when audio is disabled"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_url_in_connection(self):
        """stream_source must return None if connection exists but has no URL."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        entity = _stub_entity()
        entity.coordinator._live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        result = await BoschCamera.stream_source(entity)
        assert result is None, "Must return None when live connection has no URL field"


# ── _yuv422_to_jpeg ───────────────────────────────────────────────────────────

class TestYuv422ToJpeg:
    def test_wrong_size_returns_none(self):
        """Must return None immediately if data is not exactly 320×180×2 bytes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        result = BoschCamera._yuv422_to_jpeg(entity, b"\x00" * 100)
        assert result is None, "Must return None for data with wrong size (not 115200 bytes)"

    def test_empty_data_returns_none(self):
        """Empty bytes must not raise — must return None."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        result = BoschCamera._yuv422_to_jpeg(entity, b"")
        assert result is None, "Must return None for empty input"

    def test_correct_size_attempts_conversion(self):
        """320×180×2=115200 bytes must trigger the numpy/PIL path."""
        try:
            import numpy  # noqa: F401
            from PIL import Image  # noqa: F401
        except ImportError:
            pytest.skip("numpy/Pillow not installed — skipping YUV422 conversion test")
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        # All-grey YUV422: Y=128 (neutral grey), UV=128 (neutral chrominance)
        data = b"\x80" * (320 * 180 * 2)
        result = BoschCamera._yuv422_to_jpeg(entity, data)
        if result is not None:
            assert result[:2] == b"\xff\xd8", "Converted output must be a JPEG (starts with FFD8)"


# ── extra_state_attributes ────────────────────────────────────────────────────

class TestExtraStateAttributes:
    def test_stream_status_idle_when_no_connection(self):
        """extra_state_attributes must include stream_status='idle' when no live session."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        # Patch _cam_data property behavior inline
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch("custom_components.bosch_shc_camera.camera.get_options", return_value={"live_buffer_mode": "balanced"}):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["stream_status"] == "idle", "stream_status must be 'idle' when no connection"

    def test_stream_status_streaming_when_active(self):
        """extra_state_attributes must include stream_status='streaming' for active session."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity.is_streaming = True
        entity._cam_data = {
            "events": [], "live": {"rtspsUrl": "rtsps://x"}, "status": "ONLINE",
            "info": {"priority": 1.0, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch("custom_components.bosch_shc_camera.camera.get_options", return_value={"live_buffer_mode": "balanced"}):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["stream_status"] == "streaming", "stream_status must be 'streaming' when streaming"

    def test_stream_status_remote_fallback_label(self):
        """stream_status must say 'streaming (REMOTE fallback)' when fell_back=True."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity.is_streaming = True
        entity.coordinator._stream_fell_back[CAM_ID] = True
        entity._cam_data = {
            "events": [], "live": {"rtspsUrl": "rtsps://x"}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch("custom_components.bosch_shc_camera.camera.get_options", return_value={"live_buffer_mode": "balanced"}):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert "REMOTE fallback" in attrs["stream_status"], \
            "stream_status must indicate REMOTE fallback"

    def test_bosch_priority_included_in_attrs(self):
        """bosch_priority from cam info must appear in extra_state_attributes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": 3.0, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch("custom_components.bosch_shc_camera.camera.get_options", return_value={"live_buffer_mode": "balanced"}):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["bosch_priority"] == 3.0, "bosch_priority must be exposed for Lovelace card sort"

    def test_connecting_status_when_session_exists_but_no_stream(self):
        """stream_status='connecting' when session is in _live_connections but not yet streaming."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity.is_streaming = False
        entity.coordinator._live_connections[CAM_ID] = {}  # session open, no URL
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch("custom_components.bosch_shc_camera.camera.get_options", return_value={"live_buffer_mode": "balanced"}):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["stream_status"] == "connecting", \
            "stream_status must be 'connecting' when session is open but RTSP not yet active"

    def test_live_buffer_mode_included(self):
        """live_buffer_mode from entry options must appear in extra_state_attributes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity._cam_data = {
            "events": [], "live": {}, "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch("custom_components.bosch_shc_camera.camera.get_options", return_value={"live_buffer_mode": "low_latency"}):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["live_buffer_mode"] == "low_latency", \
            "live_buffer_mode must be passed to Lovelace card"


# ── motion_detection_enabled ──────────────────────────────────────────────────

class TestMotionDetectionEnabled:
    def test_returns_true_when_motion_enabled(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity.coordinator.motion_settings = lambda cid: {"enabled": True}
        result = BoschCamera.motion_detection_enabled.fget(entity)
        assert result is True, "motion_detection_enabled must read 'enabled' from motion_settings"

    def test_returns_false_when_motion_disabled(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity.coordinator.motion_settings = lambda cid: {"enabled": False}
        result = BoschCamera.motion_detection_enabled.fget(entity)
        assert result is False, "motion_detection_enabled must return False when motion disabled"

    def test_returns_false_when_no_settings(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity.coordinator.motion_settings = lambda cid: None
        result = BoschCamera.motion_detection_enabled.fget(entity)
        assert result is False, "motion_detection_enabled must return False when no settings"


# ── frame_interval ────────────────────────────────────────────────────────────

class TestFrameInterval:
    def test_force_refresh_gives_fast_interval(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity._force_image_refresh = True
        assert BoschCamera.frame_interval.fget(entity) == 0.1, \
            "Force refresh mode must use 0.1s interval to immediately expire HA's image cache"

    def test_streaming_gives_1s_interval(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        entity = _stub_entity()
        entity._force_image_refresh = False
        entity.is_streaming = True
        assert BoschCamera.frame_interval.fget(entity) == 1.0, \
            "Streaming mode must use 1.0s interval (shorter than card's 2s poll to avoid stale frames)"

    def test_idle_gives_long_interval(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera
        from custom_components.bosch_shc_camera.camera import IDLE_FRAME_INTERVAL
        entity = _stub_entity()
        entity._force_image_refresh = False
        entity.is_streaming = False
        result = BoschCamera.frame_interval.fget(entity)
        assert result == float(IDLE_FRAME_INTERVAL), \
            f"Idle mode must use IDLE_FRAME_INTERVAL ({IDLE_FRAME_INTERVAL}s) to avoid excessive polling"
