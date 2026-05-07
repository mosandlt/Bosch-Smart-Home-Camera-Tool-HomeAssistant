"""Tests for camera.py entity properties (456 LOC, 0% covered).

Camera entity is the most user-facing surface — its `is_streaming`,
`available`, `motion_detection_enabled` and `extra_state_attributes`
read directly from coordinator state. NPE-style bugs here cascade
to every dashboard.

Covers the synchronous properties + frame_interval logic. The async
image-fetch path (snapshot fallback chain, RTSPS thumbnail fetch) is
heavier — needs aiohttp mocks and is filed for a separate sprint.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                },
                "events": [],
                "live": {},
            }
        },
        _live_connections={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )


# ── Construction ────────────────────────────────────────────────────────


class TestCameraConstruction:
    def test_unique_id_lowercased(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam._attr_unique_id == f"bosch_shc_cam_{CAM_ID.lower()}"

    def test_starts_with_placeholder_jpeg(self, stub_coord, stub_entry):
        """Initial _cached_image is a 1×1 black JPEG to prevent HTTP 500
        when HA proxies the first image before any real snapshot has
        been fetched."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam._cached_image is not None
        # JFIF marker = JPEG signature
        assert cam._cached_image.startswith(b"\xff\xd8\xff")

    def test_resolves_display_name(self, stub_coord, stub_entry):
        """`_model_name` resolves through models.get_display_name (Außenkamera II)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert "Außenkamera" in cam._model_name


# ── is_streaming ────────────────────────────────────────────────────────


class TestIsStreaming:
    def test_false_when_no_live_session(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.is_streaming is False

    def test_true_when_live_session_present(self, stub_coord, stub_entry):
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.is_streaming is True

    def test_supported_features_always_advertises_stream(
        self, stub_coord, stub_entry,
    ):
        """STREAM must always be advertised regardless of live-session state.
        Previously STREAM was hidden when the switch was OFF, causing HA-Core
        to reject play_stream WebSocket calls with "does not support play stream
        service" (reported via homeassistant.components.camera logger, 8 hits
        at 20:46 2026-05-05). Fix: _attr_supported_features = STREAM always;
        stream_source() returns None when no session is active, which HA
        handles gracefully."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        from homeassistant.components.camera import CameraEntityFeature
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        # No live session → STREAM still advertised (stream_source returns None)
        assert cam.supported_features == CameraEntityFeature.STREAM
        # With live session → still STREAM
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        assert cam.supported_features == CameraEntityFeature.STREAM


# ── frame_interval ──────────────────────────────────────────────────────


class TestFrameInterval:
    def test_force_refresh_uses_short_interval(self, stub_coord, stub_entry):
        """`_force_image_refresh = True` → 0.1s so HA's next proxy
        request fetches immediately."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        cam._force_image_refresh = True
        assert cam.frame_interval == 0.1

    def test_streaming_uses_1_second(self, stub_coord, stub_entry):
        """While streaming → 1s (must be < card's 2s setInterval to dodge
        cache aliasing)."""
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.frame_interval == 1.0

    def test_idle_uses_long_interval(self, stub_coord, stub_entry):
        """Idle (not streaming, no force-refresh) → IDLE_FRAME_INTERVAL (60s)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.frame_interval == 60.0  # IDLE_FRAME_INTERVAL


# ── motion_detection_enabled ────────────────────────────────────────────


class TestMotionDetectionEnabled:
    def test_false_when_no_motion_settings(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        # stub_coord.motion_settings returns {} → False
        assert cam.motion_detection_enabled is False

    def test_true_when_enabled(self, stub_coord, stub_entry):
        stub_coord.motion_settings = lambda cam_id: {
            "enabled": True, "motionAlarmConfiguration": "MEDIUM",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.motion_detection_enabled is True

    def test_false_when_disabled(self, stub_coord, stub_entry):
        stub_coord.motion_settings = lambda cam_id: {
            "enabled": False, "motionAlarmConfiguration": "OFF",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.motion_detection_enabled is False


# ── HA metadata properties ──────────────────────────────────────────────


class TestMetadata:
    def test_brand_is_bosch(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.brand == "Bosch"

    def test_model_returns_hardware_version(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.model == "HOME_Eyes_Outdoor"

    def test_available_follows_coordinator(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.available is True
        stub_coord.last_update_success = False
        assert cam.available is False

    def test_device_info_has_mac_connection(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        info = cam.device_info
        assert info["manufacturer"] == "Bosch"
        assert info["sw_version"] == "9.40.25"
        assert ("mac", "64:da:a0:33:14:ae") in info["connections"]

    def test_device_info_no_mac_empty_connections(self, stub_coord, stub_entry):
        """No mac in info dict → connections is empty set, not None."""
        stub_coord.data[CAM_ID]["info"]["macAddress"] = ""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.device_info["connections"] == set()


# ── _rotate_jpeg_180 (rotation helper) ──────────────────────────────────


class TestRotateJpeg180:
    def test_invalid_jpeg_returns_original(self):
        """Garbled bytes → return as-is (graceful degradation, no exception)."""
        from custom_components.bosch_shc_camera.camera import _rotate_jpeg_180
        result = _rotate_jpeg_180(b"not-a-jpeg")
        assert result == b"not-a-jpeg"

    def test_empty_bytes_returns_original(self):
        from custom_components.bosch_shc_camera.camera import _rotate_jpeg_180
        result = _rotate_jpeg_180(b"")
        assert result == b""

    def test_real_jpeg_rotates_without_error(self):
        """A real (tiny) JPEG must rotate without raising."""
        from custom_components.bosch_shc_camera.camera import (
            BoschCamera, _rotate_jpeg_180,
        )
        # Use the placeholder JPEG (1×1 black) as input — known good
        original = BoschCamera._PLACEHOLDER_JPEG
        rotated = _rotate_jpeg_180(original)
        # Rotated output must still be a JPEG (starts with \xff\xd8\xff)
        assert rotated.startswith(b"\xff\xd8\xff")
        # 1×1 image rotated 180° = same content visually, but the encoded
        # bytes can differ. We just assert it's a valid JPEG.


# ── extra_state_attributes ──────────────────────────────────────────────


class TestExtraStateAttributes:
    def test_no_events_no_live(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        # camera_id must always be present even with no events/live
        assert attrs["camera_id"] == CAM_ID
        assert attrs["model_name"] != ""

    def test_with_live_connection(self, stub_coord, stub_entry):
        stub_coord.data[CAM_ID]["live"] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "LOCAL",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        # rtsps_url should populate (different name in attrs)
        assert "live_rtsps" in attrs or "rtsps_url" in attrs

    def test_with_recent_event(self, stub_coord, stub_entry):
        stub_coord.data[CAM_ID]["events"] = [
            {"id": "e1", "createdAt": "2026-05-05T10:00:00Z", "eventType": "MOVEMENT"},
        ]
        from custom_components.bosch_shc_camera.camera import BoschCamera
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        # last_event / event_type should reflect the latest
        assert "last_event" in attrs
        assert "event_type" in attrs
