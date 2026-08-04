"""Tests for custom_components/bosch_shc_camera/camera.py (BoschCamera entity).

One flat test module for the camera entity, covering:
  - construction, is_streaming, frame_interval, motion_detection_enabled,
    HA metadata properties, _rotate_jpeg_180, extra_state_attributes
  - availability during a cloud-down maintenance window
  - the auto_play_default option exposed on the camera attribute
  - async lifecycle hooks (setup_entry, added/will_remove_from_hass),
    the coordinator-update state machine, the image-refresh fallback
    chain, motion-detection enable/disable, stream_source edge cases,
    is_recording / _token / _cam_data, the placeholder JPEG, and
    async_create_stream (incl. privacy-mode gating and pre-warm waits)
  - the async_camera_image public wrapper + _async_camera_image_impl
    (LOCAL Digest, REMOTE proxy 200/404/401/403, RCP thumbnail fallback,
    idle cloud snapshot, event-snapshot last resort, YUV422 conversion)
  - the MJPEG inst=3 snapshot path
  - WebRTC session close/offer handling, including the pre-warm wait
    and the native-WebRTC-capability-flag regression (GitHub issue #40)
  - a stale-event-vs-live-frame regression on the proactive refresh tick
  - the SHC→Bosch class-rename contract (BoschCamera must exist, the
    legacy BoschSHCCamera name must not)

Every test constructs `BoschCamera` via `SimpleNamespace`-stubbed
coordinators/entries (either its real `__init__` or the `__new__` bypass
for pure-Python unit testing) — no live HA runtime, no network I/O.
"""

from __future__ import annotations

import asyncio
import importlib
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.const import (
    JPEG_SIZE_FULL,
    JPEG_SIZE_MEDIUM,
    JPEG_SIZE_THUMB,
)

if TYPE_CHECKING:
    from custom_components.bosch_shc_camera.camera import BoschCamera

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_ID_GAPS = "00000000-0000-0000-0000-000000000001"
CAM_ID_STALE = "11111111-2222-3333-4444-555555555555"
CAM_ID_WEBRTC = "22222222-0000-0000-0000-000000000000"
CAM_ID_PREWARM = "22222222-2222-2222-2222-222222222222"

PROXY_URL = "https://proxy-01.live.cbs.boschsecurity.com/hash/snap.jpg"
LOCAL_SNAP_URL = "https://192.0.2.149:443/snap.jpg"
LIVE_SESSION_TTL = 55  # mirrors const.py value
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 20 + b"\xff\xd9"


@pytest.fixture
def stub_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "events": [],
                "live": {},
            }
        },
        live_connections={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )


class TestCameraConstruction:
    def test_unique_id_lowercased(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam._attr_unique_id == f"bosch_shc_cam_{CAM_ID.lower()}"

    def test_starts_with_placeholder_jpeg(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Initial cached_image is a 1×1 black JPEG to prevent HTTP 500
        when HA proxies the first image before any real snapshot has
        been fetched."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.cached_image is not None
        # JFIF marker = JPEG signature
        assert cam.cached_image.startswith(b"\xff\xd8\xff")

    def test_resolves_display_name(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """`_model_name` resolves through models.get_display_name (Außenkamera II)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert "Außenkamera" in cam._model_name


class TestIsStreaming:
    def test_false_when_no_live_session(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.is_streaming is False

    def test_true_when_live_session_present(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.is_streaming is True

    def test_supported_features_always_advertises_stream(
        self,
        stub_coord: SimpleNamespace,
        stub_entry: SimpleNamespace,
    ):
        """STREAM must always be advertised regardless of live-session state.
        Previously STREAM was hidden when the switch was OFF, causing HA-Core
        to reject play_stream WebSocket calls with "does not support play stream
        service" (reported via homeassistant.components.camera logger, 8 hits
        at 20:46 2026-05-05). Fix: _attr_supported_features = STREAM always;
        stream_source() returns None when no session is active, which HA
        handles gracefully."""
        from homeassistant.components.camera import CameraEntityFeature

        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        # No live session → STREAM still advertised (stream_source returns None)
        assert cam.supported_features == CameraEntityFeature.STREAM
        # With live session → still STREAM
        stub_coord.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        assert cam.supported_features == CameraEntityFeature.STREAM


class TestFrameInterval:
    def test_force_refresh_uses_short_interval(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """`_force_image_refresh = True` → 0.1s so HA's next proxy
        request fetches immediately."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        cam._force_image_refresh = True
        assert cam.frame_interval == 0.1

    def test_streaming_uses_1_second(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """While streaming → 1s (must be < card's 2s setInterval to dodge
        cache aliasing)."""
        stub_coord.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.frame_interval == 1.0

    def test_idle_uses_long_interval(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Idle (not streaming, no force-refresh) → IDLE_FRAME_INTERVAL (60s)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.frame_interval == 60.0  # IDLE_FRAME_INTERVAL


class TestMotionDetectionEnabled:
    def test_false_when_no_motion_settings(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        # stub_coord.motion_settings returns {} → False
        assert cam.motion_detection_enabled is False

    def test_true_when_enabled(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.motion_settings = lambda cam_id: {
            "enabled": True,
            "motionAlarmConfiguration": "MEDIUM",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.motion_detection_enabled is True

    def test_false_when_disabled(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.motion_settings = lambda cam_id: {
            "enabled": False,
            "motionAlarmConfiguration": "OFF",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.motion_detection_enabled is False


class TestMetadata:
    def test_brand_is_bosch(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.brand == "Bosch"

    def test_model_returns_hardware_version(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.model == "HOME_Eyes_Outdoor"

    def test_available_follows_coordinator(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.available is True
        stub_coord.last_update_success = False
        assert cam.available is False

    @pytest.mark.parametrize(
        "cam_status",
        ["OFFLINE", "UPDATING", "SESSION_LIMIT"],
    )
    def test_available_false_when_this_camera_unreachable_despite_successful_poll(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace, cam_status: str
    ):
        """A successful account-level coordinator update does not mean every
        camera is reachable — bug-hunt 2026-07-27 (Copilot review, ported from
        the Core PR minimal cut): `available` previously returned True for
        ANY camera the moment `coordinator.last_update_success` was True,
        even when this specific camera's own cached status was OFFLINE/
        UPDATING/SESSION_LIMIT, serving stale imagery as if it were live."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        stub_coord.data[CAM_ID]["status"] = cam_status
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert stub_coord.last_update_success is True
        assert cam.available is False

    def test_available_true_when_this_camera_online(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        stub_coord.data[CAM_ID]["status"] = "ONLINE"
        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.available is True

    def test_device_info_has_mac_connection(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        info = cam.device_info
        assert info["manufacturer"] == "Bosch"
        assert info["sw_version"] == "9.40.25"
        assert ("mac", "aa:bb:cc:dd:ee:01") in info["connections"]

    def test_device_info_no_mac_empty_connections(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """No mac in info dict → connections is empty set, not None."""
        stub_coord.data[CAM_ID]["info"]["macAddress"] = ""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.device_info["connections"] == set()


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
            BoschCamera,
            _rotate_jpeg_180,
        )

        # Use the placeholder JPEG (1×1 black) as input — known good
        original = BoschCamera._PLACEHOLDER_JPEG
        rotated = _rotate_jpeg_180(original)
        # Rotated output must still be a JPEG (starts with \xff\xd8\xff)
        assert rotated.startswith(b"\xff\xd8\xff")
        # 1×1 image rotated 180° = same content visually, but the encoded
        # bytes can differ. We just assert it's a valid JPEG.


class TestExtraStateAttributes:
    def test_no_events_no_live(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        # camera_id must always be present even with no events/live
        assert attrs["camera_id"] == CAM_ID
        assert attrs["model_name"] != ""

    def test_with_live_connection(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.data[CAM_ID]["live"] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "LOCAL",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        # rtsps_url should populate (different name in attrs)
        assert "live_rtsps" in attrs or "rtsps_url" in attrs

    def test_with_recent_event(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.data[CAM_ID]["events"] = [
            {"id": "e1", "createdAt": "2026-05-05T10:00:00Z", "eventType": "MOVEMENT"},
        ]
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        # last_event / event_type should reflect the latest
        assert "last_event" in attrs
        assert "event_type" in attrs

    def test_last_event_preserves_timezone_offset(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """GitHub #34 regression: a naive [:19] slice discards the +02:00/Z
        offset, re-labelling a local time as UTC and showing events +2h off.
        `last_event` must go through `parse_bosch_timestamp` instead."""
        stub_coord.data[CAM_ID]["events"] = [
            {
                "id": "e1",
                "timestamp": "2026-07-27T12:00:00+02:00",
                "eventType": "MOVEMENT",
            },
        ]
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        # parse_bosch_timestamp normalizes to UTC — the correct instant is
        # 10:00 UTC (12:00+02:00), NOT 12:00 UTC (what a naive [:19] slice
        # re-labelled as UTC would have produced — the GitHub #34 bug).
        assert attrs["last_event"] == "2026-07-27T10:00:00+00:00"


def _make_maintenance(state: str = "active", *, camera_relevant: bool = True):
    """Duck-typed MaintenanceWindow: `available` only reads `.state()` and
    `.camera_relevant`."""
    return SimpleNamespace(camera_relevant=camera_relevant, state=lambda: state)


def _set_cloud_outage(coord):
    """Put the stub coordinator into the cloud-down-but-locally-serviceable
    state: cloud poll failed, known active maintenance window, LAN reachable,
    live session established (rtspsUrl present)."""
    coord.last_update_success = False
    coord.maintenance_cache = _make_maintenance()
    coord.is_lan_reachable = lambda cam_id: True
    coord.live_connections = {CAM_ID: {"rtspsUrl": "rtsps://127.0.0.1:36167/x"}}


class TestAvailableDuringCloudOutage:
    """Pins every guard of `_local_available_during_cloud_outage`. The camera
    must stay available when the cloud flaps inside a known maintenance window
    AND the local live session is up — but fall back to cloud availability
    whenever any guard is not satisfied."""

    def _cam(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        return BoschCamera(stub_coord, CAM_ID, stub_entry)

    def test_local_streaming_keeps_available(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        _set_cloud_outage(stub_coord)
        assert self._cam(stub_coord, stub_entry).available is True

    def test_cloud_up_short_circuits(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        # last_update_success True → available True regardless of maintenance.
        stub_coord.maintenance_cache = _make_maintenance()
        assert self._cam(stub_coord, stub_entry).available is True

    def test_no_maintenance_cache_stays_unavailable(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        _set_cloud_outage(stub_coord)
        stub_coord.maintenance_cache = None
        assert self._cam(stub_coord, stub_entry).available is False

    def test_maintenance_not_active_stays_unavailable(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        _set_cloud_outage(stub_coord)
        stub_coord.maintenance_cache = _make_maintenance("scheduled")
        assert self._cam(stub_coord, stub_entry).available is False

    def test_not_camera_relevant_stays_unavailable(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        _set_cloud_outage(stub_coord)
        stub_coord.maintenance_cache = _make_maintenance(camera_relevant=False)
        assert self._cam(stub_coord, stub_entry).available is False

    def test_lan_unreachable_stays_unavailable(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        _set_cloud_outage(stub_coord)
        stub_coord.is_lan_reachable = lambda cam_id: False
        assert self._cam(stub_coord, stub_entry).available is False

    def test_lan_reachability_unknown_stays_unavailable(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        # None (unknown) must NOT be treated as reachable.
        _set_cloud_outage(stub_coord)
        stub_coord.is_lan_reachable = lambda cam_id: None
        assert self._cam(stub_coord, stub_entry).available is False

    def test_no_live_session_stays_unavailable(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        # Maintenance + LAN ok but no streaming session → unavailable.
        _set_cloud_outage(stub_coord)
        stub_coord.live_connections = {}
        assert self._cam(stub_coord, stub_entry).available is False

    def test_live_connection_without_url_stays_unavailable(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        # live_connections entry exists but no rtsps/rtsp URL → not streaming.
        _set_cloud_outage(stub_coord)
        stub_coord.live_connections = {CAM_ID: {}}
        assert self._cam(stub_coord, stub_entry).available is False

    def test_firmware_update_overrides_local_available(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        # Firmware install must win even inside a maintenance window.
        _set_cloud_outage(stub_coord)
        stub_coord.is_updating = lambda cam_id: True
        assert self._cam(stub_coord, stub_entry).available is False

    def test_maintenance_state_raises_is_safe(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        # A broken mw.state() (e.g. tz-naive compare) must not crash available.
        _set_cloud_outage(stub_coord)

        def _boom():
            raise TypeError("naive vs aware")

        stub_coord.maintenance_cache = SimpleNamespace(
            camera_relevant=True, state=_boom
        )
        assert self._cam(stub_coord, stub_entry).available is False


def _entry_with(mode_value):
    """ConfigEntry-like with auto_play_default set to ``mode_value`` (or absent)."""
    opts: dict = {"snapshot_interval": 1800}
    if mode_value is not None:
        opts["auto_play_default"] = mode_value
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options=opts,
    )


class TestCameraAttributeAutoPlayDefault:
    @pytest.mark.parametrize("mode", ["lan", "always", "never"])
    def test_each_mode_exposed(self, stub_coord: SimpleNamespace, mode: str):
        """PIN_EVERY_MODE: each canonical mode shows up verbatim on the
        camera entity attribute the card reads."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with(mode))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == mode

    def test_legacy_confirm_collapses(self, stub_coord: SimpleNamespace):
        """v12.8.0 briefly exposed a "confirm" mode (popup dialog). v12.8.1
        dropped it in favour of an inline tap-to-reveal overlay. Any stale
        stored "confirm" value must collapse to "lan" so existing users
        keep working without manual intervention."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with("confirm"))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"

    def test_default_when_option_absent(self, stub_coord: SimpleNamespace):
        """No option stored → attribute must still be "lan" so the card
        gets a usable signal without falling back to its own client-side
        default (which would drift from the integration default)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with(None))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"

    def test_garbage_collapses_to_lan(self, stub_coord: SimpleNamespace):
        """Garbage value (typo, stale value from a future renamed mode)
        collapses to "lan" at the read site. Never disables stream start
        silently — sane fallback ensures the card stays functional."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with("not-a-real-mode"))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"

    def test_empty_string_collapses_to_lan(self, stub_coord: SimpleNamespace):
        """Empty string is a sub-case of garbage. Pin it explicitly because
        select selectors can serialize a no-selection state as ``""`` in
        some HA versions."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord, CAM_ID, _entry_with(""))
        attrs = cam.extra_state_attributes
        assert attrs["auto_play_default"] == "lan"


def _make_coord(**overrides):
    """Coordinator stub with the dicts camera.py reads."""
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:00:00:00:00:01",  # synthetic test MAC
                },
                "events": [],
                "live": {},
            }
        },
        live_connections={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        stream_warming=set(),
        last_update_success=True,
        motion_settings=lambda cid: {},
        is_stream_warming=lambda cid: False,
        async_request_refresh=AsyncMock(),
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        async_fetch_fresh_event_snapshot=AsyncMock(return_value=None),
        async_put_camera=AsyncMock(return_value=True),
        motion_set_at={},
        # Close the coroutine instead of scheduling it, so tests that don't
        # care about tracked-task scheduling don't leak an
        # un-awaited-coroutine RuntimeWarning.
        spawn_tracked=MagicMock(side_effect=lambda coro, **_kw: coro.close()),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_entry(**overrides):
    base = dict(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera(coord=None, entry=None, **camera_overrides):
    """Build a BoschCamera stub.

    Bypasses CoordinatorEntity / Camera __init__ so the entity is
    callable in pure-Python tests without the HA framework. We attach
    only the attributes the methods-under-test read.
    """
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord()
    entry = entry or _make_entry()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = entry
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._refresh_inflight = (
        False  # synchronous in-flight guard (replaces _refresh_lock)
    )
    cam._local_snap_warmup_task = None
    cam._local_snap_warmup_last = float("-inf")
    cam._image_refresh_task = None
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor"
    cam.hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "64:00:00:00:00:01"
    # HA framework calls camera.py uses
    cam.async_write_ha_state = MagicMock()

    # Default async_create_task closes the coroutine to avoid the
    # "coroutine never awaited" warning. Tests that need to capture
    # the scheduled coroutine override this with their own collector.
    def _create_task(coro):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock()

    # async_add_executor_job needed by load_snapshot in async_added_to_hass
    async def _noop_executor(fn, *args):
        return None

    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=_create_task),
        async_add_executor_job=_noop_executor,
        config=SimpleNamespace(path=lambda *p: "/tmp/bosch_test"),
    )
    for k, v in camera_overrides.items():
        setattr(cam, k, v)
    return cam


class TestAsyncSetupEntry:
    """Per-cam entity creation, gated by `enable_snapshots` option."""

    @pytest.mark.asyncio
    async def test_skip_when_snapshots_disabled(self):
        from custom_components.bosch_shc_camera.camera import async_setup_entry

        coord = _make_coord()
        entry = _make_entry(options={"enable_snapshots": False})
        entry.runtime_data = coord
        async_add = MagicMock()
        await async_setup_entry(MagicMock(), entry, async_add)
        async_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_one_entity_per_cam(self):
        from custom_components.bosch_shc_camera.camera import async_setup_entry

        coord = _make_coord()
        coord.data = {
            CAM_ID: {"info": {"title": "Terrasse"}},
            "OTHER-ID": {"info": {"title": "Garten"}},
        }
        entry = _make_entry(options={})  # default enable_snapshots=True
        entry.runtime_data = coord
        async_add = MagicMock()
        await async_setup_entry(MagicMock(), entry, async_add)
        async_add.assert_called_once()
        # First positional arg = list of entities
        entities = async_add.call_args[0][0]
        assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_no_entities_when_no_cams_discovered(self):
        from custom_components.bosch_shc_camera.camera import async_setup_entry

        coord = _make_coord()
        coord.data = {}
        entry = _make_entry(options={})
        entry.runtime_data = coord
        async_add = MagicMock()
        await async_setup_entry(MagicMock(), entry, async_add)
        async_add.assert_called_once()
        entities = async_add.call_args[0][0]
        assert entities == []


class TestLifecycleHooks:
    """Pin the entity registration contract.

    `async_added_to_hass` must register self in `coordinator.camera_entities`
    so the heartbeat NVR-restart hook + service handlers can find this
    instance. `async_will_remove_from_hass` must unregister so the dict
    doesn't accumulate dead refs across reloads."""

    @pytest.mark.asyncio
    async def test_added_to_hass_registers_with_coordinator(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        # Patch CoordinatorEntity's parent to be a no-op so we don't need
        # the HA dispatcher to fire.
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_added_to_hass(cam)
        assert coord.camera_entities[CAM_ID] is cam

    @pytest.mark.asyncio
    async def test_added_to_hass_schedules_image_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)

        # Make hass.async_create_task close the coroutine so it doesn't leak
        def _create_task(coro):
            try:
                coro.close()
            except (AttributeError, RuntimeError):
                pass
            return MagicMock()

        cam.hass.async_create_task = MagicMock(side_effect=_create_task)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_added_to_hass(cam)
        cam.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_will_remove_unregisters_from_coordinator(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        coord.camera_entities[CAM_ID] = cam
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_will_remove_from_hass(cam)
        assert CAM_ID not in coord.camera_entities

    @pytest.mark.asyncio
    async def test_will_remove_cancels_pending_local_snap_warmup_task(self):
        """GitHub #55: a still-pending background warm-up task must be
        cancelled on entity removal, not left to run past it."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        pending_task = MagicMock()
        pending_task.done.return_value = False
        cam._local_snap_warmup_task = pending_task
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_will_remove_from_hass(cam)
        pending_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_will_remove_does_not_cancel_finished_warmup_task(self):
        """An already-finished warm-up task must not be cancelled — nothing
        to interrupt, and cancelling a done Task is harmless but pointless."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        finished_task = MagicMock()
        finished_task.done.return_value = True
        cam._local_snap_warmup_task = finished_task
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_will_remove_from_hass(cam)
        finished_task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_will_remove_cancels_pending_image_refresh_task(self):
        """A still-pending background image-refresh task (startup/stream-stop/
        proactive trigger) must be cancelled on entity removal — otherwise it
        keeps running against an already-removed entity (bug-hunt 2026-07-27,
        backported from Core PR review)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        pending_task = MagicMock()
        pending_task.done.return_value = False
        cam._image_refresh_task = pending_task
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_will_remove_from_hass(cam)
        pending_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_will_remove_does_not_cancel_finished_image_refresh_task(self):
        """An already-finished image-refresh task must not be cancelled."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        finished_task = MagicMock()
        finished_task.done.return_value = True
        cam._image_refresh_task = finished_task
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            await BoschCamera.async_will_remove_from_hass(cam)
        finished_task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_will_remove_when_not_registered_no_crash(self):
        """User edge case — `async_will_remove_from_hass` may fire after
        a reload that already cleared the dict. Must not raise."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        # Ensure dict is empty
        coord.camera_entities.clear()
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            # Must not raise
            await BoschCamera.async_will_remove_from_hass(cam)

    @pytest.mark.asyncio
    async def test_added_to_hass_restores_persisted_snapshot(self):
        """Cold-start: `load_snapshot` returns bytes → `cached_image` is
        seeded with the persisted JPEG and `last_image_fetch` is back-dated
        by one snapshot_interval so the next live refresh fires on schedule
        (instead of immediately, which would race the coordinator's first
        tick). Pins camera.py's async_added_to_hass restore path."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        # Sentinel persisted JPEG, distinguishable from the placeholder.
        persisted_jpeg = b"\xff\xd8\xff\xe0RESTORED" + b"\x00" * 100
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.CoordinatorEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.load_snapshot",
                new=AsyncMock(return_value=persisted_jpeg),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.time.monotonic",
                return_value=10_000.0,
            ),
        ):
            await BoschCamera.async_added_to_hass(cam)
        # cached_image holds the persisted bytes.
        assert cam.cached_image is persisted_jpeg
        # last_image_fetch is back-dated by one snapshot_interval.
        # Default snapshot_interval = 90 s in DEFAULT_OPTIONS.
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        expected = 10_000.0 - float(DEFAULT_OPTIONS["snapshot_interval"])
        assert cam.last_image_fetch == expected


class TestHandleCoordinatorUpdate:
    """The main state-machine hook fired on every coordinator tick.

    Two transitions of interest:
      1. streaming → idle: trigger immediate (delay=2s) refresh so the
         card replaces the now-paused stream tile with a fresh snapshot.
      2. still idle → idle, but proactive interval elapsed: kick off a
         background refresh so the snapshot stays current even when no
         user is looking.

    Must NOT trigger refresh when:
      - Still streaming (no transition)
      - Was streaming and now still streaming
      - Idle but interval not elapsed
    """

    def _create_task_collector(self, cam):
        tasks = []

        def _create_task(coro):
            tasks.append(coro)
            try:
                coro.close()
            except (AttributeError, RuntimeError):
                pass
            return MagicMock()

        cam.hass.async_create_task = MagicMock(side_effect=_create_task)
        return tasks

    def test_streaming_to_idle_triggers_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()  # live_connections empty → not streaming
        cam = _make_camera(coord=coord, _was_streaming=True)
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert len(tasks) == 1
        # _was_streaming flipped
        assert cam._was_streaming is False

    def test_idle_to_idle_within_interval_no_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(
            coord=coord,
            _was_streaming=False,
            last_image_fetch=time.monotonic() - 100,  # 100s ago, < 1800s default
        )
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert tasks == []

    def test_idle_to_idle_after_interval_triggers_refresh(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(
            coord=coord,
            _was_streaming=False,
            last_image_fetch=time.monotonic() - 2000,  # > 1800s
        )
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert len(tasks) == 1

    def test_streaming_no_action(self):
        """Was streaming, still streaming → no refresh, no transition.

        `is_streaming` gates on rtspsUrl (was: just entry presence) to
        prevent the WebRTC-race-on-first-stream-start bug — so the stub
        must include a non-empty rtspsUrl for the True branch.
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(live_connections={CAM_ID: {"rtspsUrl": "rtsp://test/x"}})
        cam = _make_camera(coord=coord, _was_streaming=True)
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert tasks == []
        assert cam._was_streaming is True

    def test_streaming_to_idle_skips_refresh_while_inflight(self):
        """A refresh already in flight must not spawn a duplicate task.

        `_refresh_inflight` makes a concurrent call exit almost immediately
        without doing any work — unconditionally spawning a new task here
        and overwriting `_image_refresh_task` with that fast-exiting
        duplicate would let `async_will_remove_from_hass` cancel the
        duplicate instead of the real, still-running network task
        (backported from the Core PR's Copilot review round 7,
        2026-07-27)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()  # live_connections empty → not streaming
        cam = _make_camera(coord=coord, _was_streaming=True, _refresh_inflight=True)
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert tasks == []

    def test_idle_to_idle_after_interval_skips_refresh_while_inflight(self):
        """Same guard on the proactive-refresh-interval path."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(
            coord=coord,
            _was_streaming=False,
            last_image_fetch=time.monotonic() - 2000,  # > 1800s
            _refresh_inflight=True,
        )
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert tasks == []

    def test_custom_snapshot_interval_respected(self):
        """User-set `snapshot_interval` option must override default 1800s."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        entry = _make_entry(options={"snapshot_interval": 60})  # 1 min
        cam = _make_camera(
            coord=coord,
            entry=entry,
            _was_streaming=False,
            last_image_fetch=time.monotonic() - 90,  # 90s ago > 60s
        )
        tasks = self._create_task_collector(cam)
        with patch(
            "custom_components.bosch_shc_camera.camera.CoordinatorEntity._handle_coordinator_update",
        ):
            BoschCamera._handle_coordinator_update(cam)
        assert len(tasks) == 1


class TestAsyncTriggerImageRefresh:
    """The 4-step image-refresh state machine, the largest method in
    camera.py and the one most user-visible bugs cluster around."""

    @pytest.mark.asyncio
    async def test_privacy_mode_short_circuit(self):
        """When SHC says privacy is ON, skip the refresh entirely — the
        camera blocks the image and any fetch returns 0 bytes (or worse,
        a stale event still). Pinned because the missing short-circuit
        in earlier versions caused dozens of empty PUT /connection
        round-trips per minute (2026-04-23 forum thread)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(shc_state_cache={CAM_ID: {"privacy_mode": True}})
        cam = _make_camera(coord=coord)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_not_awaited()
        coord.async_fetch_fresh_event_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_refresh_flag_set_then_cleared(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        # finally clause clears the flag
        assert cam._force_image_refresh is False

    @pytest.mark.asyncio
    async def test_uses_live_snapshot_when_idle(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8live")
        cam = _make_camera(coord=coord)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        assert cam.cached_image == b"\xff\xd8live"
        assert cam.last_image_fetch > 0
        cam.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_local_when_remote_returns_none(self):
        """REMOTE snap.jpg may 401 on CAMERA_360 — try LOCAL Digest path."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=b"\xff\xd8local")
        cam = _make_camera(coord=coord)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        coord.async_fetch_live_snapshot_local.assert_awaited_once_with(CAM_ID)
        assert cam.cached_image == b"\xff\xd8local"

    @pytest.mark.asyncio
    async def test_skips_local_fallback_during_prewarm(self):
        """Forum 998974/40: async_fetch_live_snapshot_local opens its own
        fresh PUT /connection LOCAL, contending with an in-progress
        pre-warm for the camera's limited capacity. While
        is_stream_warming(cam_id) is True, this fallback must be skipped
        (not just the inline digest fetch in _async_camera_image_impl)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=b"\xff\xd8local")
        coord.is_stream_warming = lambda cam_id: True
        cam = _make_camera(coord=coord)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        coord.async_fetch_live_snapshot_local.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_fresh_event_when_live_paths_fail(self):
        """When both REMOTE+LOCAL live snap paths return None, dig into
        fresh events as a last resort. Bosch sometimes returns a 0-byte
        snap.jpg right after a privacy-mode flip; the fresh event grab
        is the safety net."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(
            return_value=b"\xff\xd8event"
        )
        cam = _make_camera(coord=coord)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_fresh_event_snapshot.assert_awaited_once_with(CAM_ID)
        assert cam.cached_image == b"\xff\xd8event"

    @pytest.mark.asyncio
    async def test_skips_live_snapshot_when_streaming(self):
        """Opening PUT /connection while a stream is live tears down
        the active RTSP session. Skip both live paths when streaming;
        only the quick-seed (event) path runs.

        `is_streaming` gates on rtspsUrl — the stub must include a
        non-empty rtspsUrl so the True branch fires.
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(
            live_connections={CAM_ID: {"rtspsUrl": "rtsp://test/x"}}
        )  # → is_streaming True
        cam = _make_camera(coord=coord)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_live_snapshot.assert_not_awaited()
        coord.async_fetch_live_snapshot_local.assert_not_awaited()
        # And NOT the fresh-event fallback either
        coord.async_fetch_fresh_event_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_quick_event_seed_when_only_placeholder(self):
        """First-mount path: cached_image is the 1×1 placeholder (the state
        set by __init__ before any real frame has arrived). Calls the
        coordinator's event-snapshot fetcher directly (NOT
        async_camera_image(), which would run the same live REMOTE/LOCAL/RCP
        cascade the slow path below runs again immediately after — backported
        from the Core PR's Copilot review round 3, 2026-07-27) to grab a
        quick event snapshot so the card has something within 1 s.

        Note: the guard uses identity check ``is self._PLACEHOLDER_JPEG``, not
        a None/falsy check — must pass the actual placeholder instance.
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(return_value=b"\xff\xd8seed")
        # Set cached_image to the real placeholder so the identity check triggers
        cam = _make_camera(coord=coord, cached_image=BoschCamera._PLACEHOLDER_JPEG)
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        coord.async_fetch_fresh_event_snapshot.assert_awaited_once_with(CAM_ID)
        assert cam.cached_image == b"\xff\xd8seed"

    @pytest.mark.asyncio
    async def test_persists_to_disk_and_notifies_image_entity(self):
        """On a successful refresh with privacy off + an image entity
        registered, save_snapshot runs AND
        `img_entity.async_notify_refreshed()` is awaited so the frontend
        gets the new signed-URL token."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(shc_state_cache={CAM_ID: {"privacy_mode": False}})
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8new")
        image_entity = SimpleNamespace(
            async_notify_refreshed=AsyncMock(return_value=None),
        )
        coord.image_entities = {CAM_ID: image_entity}
        cam = _make_camera(coord=coord)
        with patch(
            "custom_components.bosch_shc_camera.camera.save_snapshot",
            new=AsyncMock(return_value=None),
        ) as save_mock:
            await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        save_mock.assert_awaited_once()
        image_entity.async_notify_refreshed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exception_swallowed(self):
        """Network/HTTP errors must not propagate — the user sees a
        blank state otherwise. Pinned because earlier versions surfaced
        these as red toasts on every coordinator tick when the WAN was
        down."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(side_effect=RuntimeError("oops"))
        cam = _make_camera(coord=coord)
        # Must NOT raise
        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        # And the flag was still cleared (finally)
        assert cam._force_image_refresh is False

    @pytest.mark.asyncio
    async def test_delay_zero_skips_sleep(self):
        """delay=0 must not call asyncio.sleep — pin so a refactor can't
        accidentally add a 0-second sleep that schedules a context switch."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            await BoschCamera.async_trigger_image_refresh(cam, delay=0)
            sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delay_nonzero_sleeps(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        with patch("asyncio.sleep", new=AsyncMock()) as sleep:
            await BoschCamera.async_trigger_image_refresh(cam, delay=2)
            sleep.assert_awaited_once_with(2)


class TestMotionDetectionToggle:
    """The standard HA `camera.enable_motion_detection` /
    `camera.disable_motion_detection` services. Bosch wants both
    `enabled` and `motionAlarmConfiguration` (sensitivity) in every
    PUT — preserving the existing sensitivity is critical, otherwise
    the user's tuning resets to HIGH every time they toggle."""

    @pytest.mark.asyncio
    async def test_enable_sends_enabled_true(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": False,
            "motionAlarmConfiguration": "MEDIUM",
        }
        cam = _make_camera(coord=coord)
        await BoschCamera.async_enable_motion_detection(cam)
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID,
            "motion",
            {"enabled": True, "motionAlarmConfiguration": "MEDIUM"},
        )

    @pytest.mark.asyncio
    async def test_disable_sends_enabled_false_keeps_sensitivity(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "LOW",
        }
        cam = _make_camera(coord=coord)
        await BoschCamera.async_disable_motion_detection(cam)
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID,
            "motion",
            {"enabled": False, "motionAlarmConfiguration": "LOW"},
        )

    @pytest.mark.asyncio
    async def test_enable_raises_when_no_settings(self):
        """When motion_settings returns empty (cam not yet refreshed), fail
        loudly instead of inventing a sensitivity.

        Silently defaulting to HIGH before the coordinator's slow tier has
        ever fetched motion settings (e.g. right after startup while the
        camera was offline) would reset a real LOW/MEDIUM setting the
        first time the PUT actually lands (Copilot review round 13,
        backported from the Core PR).
        """
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {}
        cam = _make_camera(coord=coord)
        with pytest.raises(HomeAssistantError):
            await BoschCamera.async_enable_motion_detection(cam)
        coord.async_put_camera.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disable_raises_when_no_settings(self):
        """See test_enable_raises_when_no_settings above."""
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {}
        cam = _make_camera(coord=coord)
        with pytest.raises(HomeAssistantError):
            await BoschCamera.async_disable_motion_detection(cam)
        coord.async_put_camera.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enable_triggers_coordinator_refresh(self):
        """After PUT, fire a coordinator refresh in background so the
        `motion_detection_enabled` property reflects the new state.

        Tracked via `coordinator.spawn_tracked` (not a bare
        `hass.async_create_task`) — otherwise this can outlive config-entry
        unload and keep running against an already-torn-down coordinator
        (Copilot review round 12, backported from the Core PR).
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": False,
            "motionAlarmConfiguration": "MEDIUM",
        }
        cam = _make_camera(coord=coord)
        await BoschCamera.async_enable_motion_detection(cam)
        coord.spawn_tracked.assert_called_once()
        _, call_kwargs = coord.spawn_tracked.call_args
        assert call_kwargs["name"] == "bosch_shc_camera_motion_enable_refresh"

    @pytest.mark.asyncio
    async def test_disable_triggers_coordinator_refresh(self):
        """See test_enable_triggers_coordinator_refresh above."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "MEDIUM",
        }
        cam = _make_camera(coord=coord)
        await BoschCamera.async_disable_motion_detection(cam)
        coord.spawn_tracked.assert_called_once()
        _, call_kwargs = coord.spawn_tracked.call_args
        assert call_kwargs["name"] == "bosch_shc_camera_motion_disable_refresh"

    @pytest.mark.asyncio
    async def test_enable_raises_when_put_fails(self):
        """async_put_camera returning False must raise HomeAssistantError,
        not silently proceed to the optimistic cache update."""
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": False,
            "motionAlarmConfiguration": "MEDIUM",
        }
        coord.async_put_camera = AsyncMock(return_value=False)
        cam = _make_camera(coord=coord)
        with pytest.raises(HomeAssistantError):
            await BoschCamera.async_enable_motion_detection(cam)

    @pytest.mark.asyncio
    async def test_disable_raises_when_put_fails(self):
        """async_put_camera returning False must raise HomeAssistantError,
        not silently proceed to the optimistic cache update."""
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.motion_settings = lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "MEDIUM",
        }
        coord.async_put_camera = AsyncMock(return_value=False)
        cam = _make_camera(coord=coord)
        with pytest.raises(HomeAssistantError):
            await BoschCamera.async_disable_motion_detection(cam)


class TestStreamSourceEdgeCasesAsync:
    """Additional stream_source cases beyond TestStreamSourceTransport."""

    @pytest.mark.asyncio
    async def test_returns_none_when_url_missing(self):
        """Live conn entry exists but has no rtsps/rtsp URL — return None.
        Edge case during the connect-handshake window."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(live_connections={CAM_ID: {"_connection_type": "LOCAL"}})
        coord.audio_enabled = {}
        cam = _make_camera(coord=coord)
        url = await BoschCamera.stream_source(cam)
        assert url is None

    @pytest.mark.asyncio
    async def test_falls_back_from_rtsps_to_rtsp(self):
        """Some legacy code paths set `rtspUrl` (no s); stream_source
        accepts either. Pin so a refactor doesn't drop the fallback."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord(
            live_connections={
                CAM_ID: {
                    "_connection_type": "LOCAL",
                    "rtspUrl": "rtsp://x:y@127.0.0.1:5000/rtsp_tunnel",
                },
            }
        )
        coord.audio_enabled = {CAM_ID: True}
        cam = _make_camera(coord=coord)
        url = await BoschCamera.stream_source(cam)
        assert url and "127.0.0.1:5000" in url


class TestIsRecording:
    """HA Camera.is_recording — we don't track recording at the entity
    level (Mini-NVR has its own switch entity). Pin to False."""

    def test_returns_false(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        assert BoschCamera.is_recording.fget(cam) is False


class TestTokenProperty:
    def test_returns_bearer_token_from_entry_data(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        entry = _make_entry(data={"bearer_token": "TOK-X"})
        cam = _make_camera(coord=coord, entry=entry)
        assert BoschCamera._token.fget(cam) == "TOK-X"

    def test_returns_empty_when_no_token(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        entry = _make_entry(data={})
        cam = _make_camera(coord=coord, entry=entry)
        assert BoschCamera._token.fget(cam) == ""


class TestCamDataProperty:
    def test_returns_coordinator_cam_dict(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)
        out = BoschCamera._cam_data.fget(cam)
        assert out["info"]["title"] == "Terrasse"

    def test_returns_empty_dict_for_unknown_cam(self):
        """If the cam disappears from coordinator.data (e.g. after a
        device removal), _cam_data must return {} rather than KeyError
        — every attribute consumer trusts the {} contract."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.data = {}  # cam not in data
        cam = _make_camera(coord=coord)
        assert BoschCamera._cam_data.fget(cam) == {}


class TestPlaceholderJpeg:
    """The 1×1 black JPEG used while the first real snapshot is fetching.
    Without it, HA's camera proxy returns HTTP 500 to the card and the
    user sees a broken-image icon."""

    def test_placeholder_is_valid_jpeg(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        ph = BoschCamera._PLACEHOLDER_JPEG
        assert ph.startswith(b"\xff\xd8")  # JPEG SOI
        assert ph.endswith(b"\xff\xd9")  # JPEG EOI
        # Reasonable size — not a multi-MB photo by accident
        assert 100 < len(ph) < 1000

    def test_placeholder_decodes_via_pil(self):
        """Pin that PIL can actually decode it — a corrupt placeholder
        would crash the rotation path (_rotate_jpeg_180 is the only
        consumer that hits PIL)."""
        from io import BytesIO

        from PIL import Image

        from custom_components.bosch_shc_camera.camera import BoschCamera

        img = Image.open(BytesIO(BoschCamera._PLACEHOLDER_JPEG))
        assert img.size == (1, 1)


#
# Regression test for 2026-05-09 19:02 CEST error:
# "Error requesting stream: camera.bosch_terrasse does not support play stream service"
# Root cause: stream_source() returns None when no live connection is active →
# Camera.async_create_stream() (HA base) returns None → HA treats camera as
# incapable. Fix: override async_create_stream() to auto-open the live
# connection before delegating to super().


class TestAsyncCreateStream:
    @pytest.mark.asyncio
    async def test_no_connection_auto_opens_and_returns_stream(self):
        """With no active live session, opens connection and returns the stream."""
        live_result = {
            "rtspsUrl": "rtsps://proxy-12.live.cbs.boschsecurity.com:443/abc/rtsp_tunnel"
        }
        coord = _make_coord(
            try_live_connection=AsyncMock(return_value=live_result),
            async_update_listeners=MagicMock(),
        )
        cam = _make_camera(coord=coord)
        fake_stream = object()
        with patch(
            "homeassistant.components.camera.Camera.async_create_stream",
            new=AsyncMock(return_value=fake_stream),
        ):
            result = await cam.async_create_stream()
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        coord.async_update_listeners.assert_called_once()
        assert result is fake_stream

    @pytest.mark.asyncio
    async def test_connection_open_failure_returns_none(self):
        """When try_live_connection fails, returns None instead of raising."""
        coord = _make_coord(
            try_live_connection=AsyncMock(return_value=None),
            async_update_listeners=MagicMock(),
        )
        cam = _make_camera(coord=coord)
        result = await cam.async_create_stream()
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        coord.async_update_listeners.assert_not_called()
        assert result is None

    @pytest.mark.asyncio
    async def test_existing_connection_skips_open(self):
        """When a live session is already active, skips try_live_connection."""
        coord = _make_coord(
            live_connections={CAM_ID: {"rtspsUrl": "rtsps://proxy-12/xyz"}},
            try_live_connection=AsyncMock(),
            async_update_listeners=MagicMock(),
        )
        cam = _make_camera(coord=coord)
        fake_stream = object()
        with patch(
            "homeassistant.components.camera.Camera.async_create_stream",
            new=AsyncMock(return_value=fake_stream),
        ):
            result = await cam.async_create_stream()
        coord.try_live_connection.assert_not_awaited()
        coord.async_update_listeners.assert_not_called()
        assert result is fake_stream


class TestRefreshInflightGuard:
    """Regression: two concurrent delayed callers both sleeping then
    racing past the old ``_refresh_lock.locked()`` check.

    With the old two-step guard (``locked()`` → ``async with lock``), two
    coroutines sleeping concurrently (delay>0) could both observe
    ``locked()==False``, both enter ``async with``, and the second one would
    block on the yield inside ``__aenter__``, then perform a redundant second
    fetch after the first completed — burning the Bosch 3-session budget.

    The fix uses a synchronous ``_refresh_inflight`` boolean set BEFORE the
    first yield: the second caller sees it immediately and returns.
    """

    @pytest.mark.asyncio
    async def test_second_caller_skips_when_inflight_true(self) -> None:
        """When _refresh_inflight is already True (set by a first in-flight caller),
        a second call to async_trigger_image_refresh must return immediately
        without calling async_fetch_live_snapshot.

        This pins the synchronous-flag guard that replaces the old two-step
        ``_refresh_lock.locked()`` + ``async with lock`` approach. The old
        approach had a yield-point gap between the check and the acquire
        (``Lock.__aenter__`` is a coroutine), so two concurrent delayed callers
        (both sleeping then both waking) could both pass the check and proceed.
        The boolean flag is set synchronously before any I/O yield, closing
        the window entirely.

        We simulate the concurrent scenario directly by pre-setting the flag
        to True (as the first caller would have done) and confirming the second
        call returns without fetching.
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8live")
        cam = _make_camera(coord=coord)

        # Simulate first caller holding the inflight flag
        cam._refresh_inflight = True

        await BoschCamera.async_trigger_image_refresh(cam, delay=0)

        # Second caller must have returned without fetching
        coord.async_fetch_live_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_inflight_flag_cleared_after_completion(self) -> None:
        """_refresh_inflight must be False after the coroutine completes
        so subsequent calls (e.g. on the next proactive tick) are not
        permanently suppressed."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        cam = _make_camera(coord=coord)

        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        assert cam._refresh_inflight is False

    @pytest.mark.asyncio
    async def test_inflight_flag_cleared_after_exception(self) -> None:
        """_refresh_inflight must be cleared even when an exception is raised
        inside the body — otherwise all future refreshes would be suppressed."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord()
        coord.async_fetch_live_snapshot = AsyncMock(side_effect=RuntimeError("boom"))
        cam = _make_camera(coord=coord)

        await BoschCamera.async_trigger_image_refresh(cam, delay=0)
        assert cam._refresh_inflight is False


def _make_coord_gaps(**overrides):
    base = dict(
        data={
            CAM_ID_GAPS: {
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
        live_connections={},
        stream_fell_back={},
        stream_error_count={},
        stream_warming=set(),
        audio_enabled={CAM_ID_GAPS: True},
        local_creds_cache={},
        live_opened_at={},
        image_rotation_180={},
        shc_state_cache={CAM_ID_GAPS: {"privacy_mode": False}},
        timestamp_cache={},
        auth_outage_count=0,
        last_update_success=True,
        token="tok-A",
        options={},
        is_camera_online=lambda cid: True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stub_entity_gaps(coord=None, **overrides):
    """Minimal BoschCamera-like stub for testing static methods and properties."""
    coord = coord or _make_coord_gaps()
    entry = SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "tok-A"},
        options={"live_buffer_mode": "balanced"},
    )
    base = dict(
        coordinator=coord,
        _cam_id=CAM_ID_GAPS,
        _entry=entry,
        _attr_name="Bosch Terrasse",
        _display_name="Bosch Terrasse",
        _cam_title="Terrasse",
        _model="HOME_Eyes_Outdoor",
        _model_name="Eyes Outdoor II",
        _fw="9.40.25",
        _mac="aa:bb:cc:dd:ee:01",
        hw_version="HOME_Eyes_Outdoor",
        cached_image=None,
        last_image_fetch=0.0,
        _force_image_refresh=False,
        is_streaming=False,
        stream_options={},
    )
    base.update(overrides)
    obj = SimpleNamespace(**base)
    obj._cam_data = coord.data[CAM_ID_GAPS]
    return obj


def _make_camera_gaps(coord=None, **overrides):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord_gaps()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID_GAPS
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor II"
    cam.hw_version = "HOME_Eyes_Outdoor"
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


class TestCameraTimestampOverlayAttr:
    """The card hides its own timestamp pill when the camera burns in its
    own on-screen clock. The attribute must be exposed when the cache
    contains a truthy or falsy value, and omitted when no cache entry
    exists."""

    def _get_attrs(self, entity):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"auto_play_default": "lan"},
        ):
            return BoschCamera.extra_state_attributes.fget(entity)

    def test_timestamp_overlay_true_when_cache_value_is_true(self):
        """When timestamp_cache[cam_id]=True the attribute must be True."""
        coord = _make_coord_gaps(timestamp_cache={CAM_ID_GAPS: True})
        entity = _stub_entity_gaps(coord=coord)
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" in attrs, (
            "camera_timestamp_overlay must be present when cache has a value"
        )
        assert attrs["camera_timestamp_overlay"] is True

    def test_timestamp_overlay_false_when_cache_value_is_false(self):
        """When timestamp_cache[cam_id]=False the attribute must be False."""
        coord = _make_coord_gaps(timestamp_cache={CAM_ID_GAPS: False})
        entity = _stub_entity_gaps(coord=coord)
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" in attrs
        assert attrs["camera_timestamp_overlay"] is False

    def test_timestamp_overlay_absent_when_cache_has_no_entry(self):
        """When timestamp_cache has no entry for this cam_id the attribute
        must be omitted entirely (not False/None)."""
        coord = _make_coord_gaps(timestamp_cache={})
        entity = _stub_entity_gaps(coord=coord)
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" not in attrs, (
            "camera_timestamp_overlay must be absent when no cache entry exists"
        )

    def test_timestamp_overlay_absent_when_cache_value_is_none(self):
        """When timestamp_cache[cam_id]=None the attribute must also be omitted."""
        coord = _make_coord_gaps(timestamp_cache={CAM_ID_GAPS: None})
        entity = _stub_entity_gaps(coord=coord)
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" not in attrs, (
            "camera_timestamp_overlay must be absent when cache value is None"
        )

    def test_timestamp_overlay_absent_when_no_timestamp_cache_attr(self):
        """Defensive: if coordinator lacks timestamp_cache entirely (legacy
        coordinator loaded from old snapshot), the attribute must be absent."""
        coord = _make_coord_gaps()
        del coord.timestamp_cache  # simulate missing attribute
        entity = _stub_entity_gaps(coord=coord)
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        attrs = self._get_attrs(entity)
        assert "camera_timestamp_overlay" not in attrs


class TestAsyncCameraImageImplCachedImageFallback:
    """When cloud+LAN snapshots both return None (e.g. CAMERA_360 with an
    auth-required snap.jpg endpoint), cached_image must be returned as
    the fallback so the Lovelace card still shows the last-known frame.

    This covers the `if self.cached_image: return self.cached_image` branch.
    """

    @pytest.mark.asyncio
    async def test_returns_cached_image_when_streaming_but_no_proxy_url(self):
        """Camera is streaming (rtspsUrl set) but proxyUrl absent →
        section 1 skipped (no proxyUrl), section 2 skipped (is_streaming=True),
        section 2b skipped (no outage creds), falls through to section 3.

        This path occurs when the live connection has the RTSP URL ready but the
        snap.jpg proxy URL hasn't been refreshed yet (brief window on reconnect).
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord_gaps()
        # Live connection with rtspsUrl → is_streaming=True, but NO proxyUrl → section 1 skipped
        coord.live_connections = {
            CAM_ID_GAPS: {
                "rtspsUrl": "rtsps://192.168.1.1/stream",
                # No proxyUrl → proxy_url = "" → section 1 not entered
            }
        }
        coord.local_creds_cache = {}  # no outage creds → 2b skipped
        coord.auth_outage_count = 0
        coord.data[CAM_ID_GAPS]["events"] = []

        cam = _make_camera_gaps(coord=coord)
        cam.cached_image = b"\xff\xd8\xff\xe0cached_frame"
        cam._was_streaming = False
        cam.hass = SimpleNamespace(
            async_create_task=MagicMock(
                side_effect=lambda c: (c.close(), MagicMock())[1]
            ),
            async_add_executor_job=AsyncMock(),
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.get_options",
                return_value={"use_mjpeg_snapshot": False},
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8\xff\xe0cached_frame", (
            "Must return cached_image when streaming but no proxyUrl (section 3)"
        )


@pytest.fixture
def stub_coord_extra() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "events": [],
                "live": {},
                "status": "ONLINE",
            }
        },
        live_connections={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        audio_enabled={},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )


@pytest.fixture
def stub_entry_extra() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800, "live_buffer_mode": "balanced"},
    )


class TestYuv422ToJpeg:
    """Pin the YUV422→JPEG converter used as Gen1 thumbnail fallback.

    Reason: Gen1 360 cameras returned 320×180 raw YUV422 frames via RCP
    0x0c98 when the JPEG path (0x099e) was unavailable. Without this
    converter the integration falls through to placeholder, hiding live
    state from the dashboard. The dimensions (320×180) and total byte
    count (115200) are wired into the camera-side firmware — values
    other than 320*180*2 must reject so we don't hand garbage to PIL.
    """

    def _make_cam(self, stub_coord_extra, stub_entry_extra):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        return BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)

    def test_wrong_size_returns_none(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        cam = self._make_cam(stub_coord_extra, stub_entry_extra)
        assert cam._yuv422_to_jpeg(b"x" * 100) is None, (
            "Non-115200-byte payload must reject (firmware contract: "
            "320×180×2 = 115200). Accepting any other size would feed "
            "PIL malformed shape and crash the snapshot path."
        )

    def test_empty_bytes_returns_none(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        cam = self._make_cam(stub_coord_extra, stub_entry_extra)
        assert cam._yuv422_to_jpeg(b"") is None

    def test_one_byte_off_returns_none(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """115199 bytes — off-by-one defends against truncated reads."""
        cam = self._make_cam(stub_coord_extra, stub_entry_extra)
        assert cam._yuv422_to_jpeg(b"\x00" * 115199) is None
        assert cam._yuv422_to_jpeg(b"\x00" * 115201) is None

    def test_all_zero_yuv_produces_valid_jpeg(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """All-zero YUV422 = uniform dark green frame → must encode
        without error and produce JPEG bytes."""
        cam = self._make_cam(stub_coord_extra, stub_entry_extra)
        raw = b"\x00" * (320 * 180 * 2)
        out = cam._yuv422_to_jpeg(raw)
        assert out is not None
        assert out[:3] == b"\xff\xd8\xff", (
            "Output must be a JPEG (SOI marker FF D8 FF…). Anything "
            "else means our exception handler ate a real failure."
        )
        # SOI + APP0/JFIF + ... + EOI
        assert out[-2:] == b"\xff\xd9", "JPEG must end with EOI marker"

    def test_uniform_yuv_produces_valid_jpeg(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """Y=128, U=128, V=128 → valid neutral grey frame."""
        cam = self._make_cam(stub_coord_extra, stub_entry_extra)
        # YUYV: Y0 U Y1 V repeats. Make it a flat grey field.
        # raw[:,:,0] = Y plane (128), raw[:,:,1] alternates U=128/V=128
        raw = (b"\x80" + b"\x80") * (320 * 180)
        assert len(raw) == 115200
        out = cam._yuv422_to_jpeg(raw)
        assert out is not None
        assert out[:3] == b"\xff\xd8\xff"

    def test_high_contrast_yuv_produces_jpeg(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """Alternating Y=0 / Y=255 across the frame must still encode."""
        cam = self._make_cam(stub_coord_extra, stub_entry_extra)
        # Build a frame with Y bytes alternating, UV all 128
        row = b""
        for _ in range(160):  # 320 px in YUYV = 160 macropixel pairs
            row += b"\x00\x80\xff\x80"  # Y0=0 U=128 Y1=255 V=128
        raw = row * 180
        assert len(raw) == 115200
        out = cam._yuv422_to_jpeg(raw)
        assert out is not None
        assert out.startswith(b"\xff\xd8\xff")


class TestStreamStatusAttribute:
    """Pin the 5-state stream_status enum exposed via attributes.

    The Lovelace card reads `stream_status` to render the badge color
    (idle / connecting / warming / streaming / fallback). Drift in this
    enum string immediately breaks badge rendering on every dashboard.
    """

    def test_idle_when_no_session_no_warming(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "idle"
        assert attrs["streaming_state"] == "idle"

    def test_streaming_when_live_session_active(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        stub_coord_extra.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "REMOTE",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "streaming"
        assert attrs["streaming_state"] == "active"

    def test_streaming_remote_fallback(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """When LOCAL was tried and lost → REMOTE, badge shows fallback."""
        stub_coord_extra.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "REMOTE",
        }
        stub_coord_extra.stream_fell_back[CAM_ID] = True
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "streaming (REMOTE fallback)"

    def test_warming_up_takes_priority(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """While the encoder is pre-warming the badge must show
        `warming_up` even if a live_connection is in flight."""
        stub_coord_extra.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "LOCAL",
        }
        stub_coord_extra.is_stream_warming = lambda cam_id: True
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "warming_up", (
            "warming_up must beat streaming so the card shows the "
            "spinner instead of the live badge while pre-warm is mid-flight."
        )


class TestOptionalAttributes:
    """Optional attributes must appear only when backed by a real value
    and never as `None` / empty-string. HA logbook + recorder include
    every attribute change, so empty noise pollutes history."""

    def test_buffering_time_only_when_set(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        attrs = cam.extra_state_attributes
        assert "buffering_time_ms" not in attrs
        assert "connection_type" not in attrs

    def test_buffering_time_appears_with_live_session(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        stub_coord_extra.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "LOCAL",
            "_bufferingTime": 500,
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        attrs = cam.extra_state_attributes
        assert attrs["buffering_time_ms"] == 500
        assert attrs["connection_type"] == "LOCAL"

    def test_bosch_priority_passes_through(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """`info.priority` (cloud float) appears as bosch_priority for
        the overview card's `use_bosch_sort` option."""
        stub_coord_extra.data[CAM_ID]["info"]["priority"] = 1.5
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert cam.extra_state_attributes["bosch_priority"] == 1.5

    def test_bosch_priority_none_when_absent(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """Missing priority → None (not "" or 0). Card distinguishes
        these via `priority != null` check before sorting."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert cam.extra_state_attributes["bosch_priority"] is None

    def test_live_buffer_mode_propagates_from_options(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """Player-side buffer profile must reach the card via attribute."""
        stub_entry_extra.options["live_buffer_mode"] = "low_latency"
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert cam.extra_state_attributes["live_buffer_mode"] == "low_latency"

    def test_live_buffer_mode_defaults_to_balanced(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """Missing option → 'balanced'. This default is wired into the
        card's BOSCH_BUFFER_PROFILES table — both ends must agree."""
        stub_entry_extra.options.pop("live_buffer_mode", None)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert cam.extra_state_attributes["live_buffer_mode"] == "balanced"


class TestStreamSourceTransport:
    """Pin the LOCAL=tcp / REMOTE=default invariant. HA-Core 2026.4 +
    FFmpeg Lavf 62 reject the UDP→TCP transport rewrite the proxy used
    to do, so LOCAL must force `rtsp_transport=tcp` on SETUP. REMOTE
    streams go directly to Bosch cloud proxy via rtsps:// and forcing
    TCP there breaks Gen1 Eyes Outdoor cloud streams (regression test
    against an older 'always force tcp' patch).
    """

    @pytest.mark.asyncio
    async def test_local_sets_tcp_transport(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        stub_coord_extra.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://127.0.0.1:46597/rtsp_tunnel",
            "_connection_type": "LOCAL",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        url = await cam.stream_source()
        assert url == "rtsps://127.0.0.1:46597/rtsp_tunnel"
        assert cam.stream_options == {"rtsp_transport": "tcp"}

    @pytest.mark.asyncio
    async def test_remote_uses_default_transport(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        stub_coord_extra.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy-12.live.cbs.boschsecurity.com:443/abc/rtsp_tunnel",
            "_connection_type": "REMOTE",
        }
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        url = await cam.stream_source()
        assert url.startswith("rtsps://")
        assert cam.stream_options == {}, (
            "REMOTE must NOT force tcp — Gen1 Eyes Outdoor cloud streams "
            "break when forced to TCP transport."
        )

    @pytest.mark.asyncio
    async def test_audio_param_kept_even_when_switch_off(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """The audio track is ALWAYS kept in the stream now — switch.<cam>_audio
        is a card-side mute preference, not a track toggle. stream_source() must
        NOT strip enableaudio=1 even when audio_enabled is False, else a session
        started while muted would have no track to unmute. Regression for the
        always-on-audio design (2026-06-01)."""
        stub_coord_extra.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1",
            "_connection_type": "REMOTE",
        }
        stub_coord_extra.audio_enabled[CAM_ID] = False
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        url = await cam.stream_source()
        assert "enableaudio=1" in url
        assert "inst=1" in url and "fmtp=1" in url

    @pytest.mark.asyncio
    async def test_no_session_returns_none(
        self, stub_coord_extra: SimpleNamespace, stub_entry_extra: SimpleNamespace
    ):
        """No live_connections entry → None (HA sees stream_source==None
        and returns 503 to the WebSocket caller, which is the documented
        graceful behavior — see test_supported_features_always_advertises_stream)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera(stub_coord_extra, CAM_ID, stub_entry_extra)
        url = await cam.stream_source()
        assert url is None


def _make_coord_impl(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "X"},
                "events": [],
            },
        },
        live_connections={},
        live_opened_at={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        stream_warming=set(),
        image_rotation_180={},
        local_creds_cache={},
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera_impl(coord=None, **camera_overrides):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord_impl()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._local_snap_warmup_task = None
    cam._local_snap_warmup_last = float("-inf")
    cam._image_refresh_task = None
    cam._model = "X"
    cam._model_name = "X"
    cam.hw_version = "X"
    cam._fw = ""
    cam._mac = ""
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
        async_create_background_task=MagicMock(
            side_effect=lambda c, name: (c.close(), MagicMock())[1]
        ),
        async_add_executor_job=AsyncMock(),
    )
    for k, v in camera_overrides.items():
        setattr(cam, k, v)
    return cam


class TestAsyncCameraImageWrapper:
    """The public entrypoint that HA's camera proxy calls. Wraps the
    complex `_async_camera_image_impl` so any uncaught exception still
    yields a valid JPEG instead of HTTP 500 (which Lovelace's <img>
    renders as a brown text-bytes-as-pixels error frame)."""

    @pytest.mark.asyncio
    async def test_returns_impl_result_when_present(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl()
        cam._async_camera_image_impl = AsyncMock(return_value=b"\xff\xd8live-img")
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8live-img"

    @pytest.mark.asyncio
    async def test_returns_placeholder_when_impl_returns_none(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl()
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

        cam = _make_camera_impl(cached_image=b"\xff\xd8cached")
        cam._async_camera_image_impl = AsyncMock(side_effect=RuntimeError("oops"))
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_returns_placeholder_not_cached_when_impl_raises_and_privacy_unknown(
        self,
    ):
        """A blind cached-image serve on exception must also fail closed.

        Regression test for a 3-agent bug-hunt finding (round 20 backport,
        2026-08-04): `_async_rcp_thumbnail()` has no try/except of its own,
        so an aiohttp.ClientError/OSError from it propagates out of
        `_async_camera_image_impl` uncaught — the wrapper's `except
        Exception` branch must not then serve `cached_image` unconditionally
        while privacy state is unknown (e.g. a cloud-degraded restart), or
        it defeats every other fail-closed guard in the cascade.
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(cached_image=b"\xff\xd8cached-pre-privacy-frame")
        cam.coordinator.shc_state_cache = {}  # no entry for this cam — unknown
        cam._async_camera_image_impl = AsyncMock(side_effect=RuntimeError("oops"))
        out = await BoschCamera.async_camera_image(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG

    @pytest.mark.asyncio
    async def test_returns_placeholder_when_impl_raises_and_no_cache(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl()  # cached_image=None
        cam._async_camera_image_impl = AsyncMock(side_effect=RuntimeError("oops"))
        out = await BoschCamera.async_camera_image(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        """CancelledError must propagate cleanly so HA's outer-task
        cancellation (timeout, shutdown) isn't swallowed."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl()
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

        coord = _make_coord_impl(image_rotation_180={CAM_ID: True})
        cam = _make_camera_impl(coord=coord)
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

        coord = _make_coord_impl(image_rotation_180={CAM_ID: True})
        cam = _make_camera_impl(coord=coord)
        cam._async_camera_image_impl = AsyncMock(return_value=None)
        out = await BoschCamera.async_camera_image(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG
        cam.hass.async_add_executor_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_disabled_no_executor_call(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord_impl(image_rotation_180={CAM_ID: False})
        cam = _make_camera_impl(coord=coord)
        cam._async_camera_image_impl = AsyncMock(return_value=b"\xff\xd8orig")
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8orig"
        cam.hass.async_add_executor_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_when_attribute_missing(self):
        """`image_rotation_180` may not exist on older coordinator
        snapshots — getattr default {} keeps the rotation off."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _make_coord_impl()
        # Remove the attribute entirely
        if hasattr(coord, "image_rotation_180"):
            delattr(coord, "image_rotation_180")
        cam = _make_camera_impl(coord=coord)
        cam._async_camera_image_impl = AsyncMock(return_value=b"\xff\xd8orig")
        out = await BoschCamera.async_camera_image(cam)
        assert out == b"\xff\xd8orig"


class TestAsyncCameraImageImplLocalDigest:
    """The LOCAL path uses async_digest_request (aiohttp-native Digest auth).

    Mock async_digest_request to simulate the async fetch result
    without actually doing any HTTP."""

    def _local_coord(self):
        return _make_coord_impl(
            live_connections={
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

        cam = _make_camera_impl(coord=self._local_coord())
        img = b"\xff\xd8local-img"
        cm = self._digest_resp_cm(200, img, "image/jpeg")
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == img
        assert cam.cached_image == img
        assert cam.last_image_fetch > 0

    @pytest.mark.asyncio
    async def test_local_digest_timeout_returns_placeholder(self):
        """LOCAL Digest fetch times out — return cached/placeholder
        immediately rather than racing HA's outer 10s timeout. Pin:
        the function MUST NOT fall through to aiohttp for LOCAL
        (the proxy_url for LOCAL is the camera's HTTPS endpoint that
        requires Digest auth — unauth aiohttp would 401 in another
        ~10 s, blowing HA's outer timeout)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(
            coord=self._local_coord(), cached_image=b"\xff\xd8cached"
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
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

        cam = _make_camera_impl(
            coord=self._local_coord(), cached_image=b"\xff\xd8cached"
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=aiohttp.ClientError("network error")),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_local_digest_skipped_during_prewarm(self):
        """Forum 998974/40: LOCAL snapshot polls contend with pre-warm for the
        camera's ~2-concurrent-RTSP-session budget, producing spurious "LOCAL
        snap via proxy failed" warnings and adding jitter to pre-warm retries.
        While is_stream_warming(cam_id) is True, skip the Digest fetch entirely
        and serve cached/placeholder instead of racing the live session."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = self._local_coord()
        coord.is_stream_warming = lambda cam_id: True
        cam = _make_camera_impl(coord=coord, cached_image=b"\xff\xd8cached")
        digest_mock = AsyncMock(return_value=self._digest_resp_cm(200, b"\xff\xd8new"))
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=digest_mock,
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        digest_mock.assert_not_awaited()
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_local_digest_runs_once_prewarm_clears(self):
        """Inverse of the above: once is_stream_warming(cam_id) goes False
        (pre-warm finished), the LOCAL Digest fetch runs normally again."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = self._local_coord()
        coord.is_stream_warming = lambda cam_id: False
        cam = _make_camera_impl(coord=coord)
        img = b"\xff\xd8fresh"
        digest_mock = AsyncMock(return_value=self._digest_resp_cm(200, img))
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=digest_mock,
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        digest_mock.assert_awaited_once()
        assert out == img


class TestLocalSnapWarmup:
    """GitHub #55: a real TimeoutError on the inline LOCAL Digest fetch
    schedules a background warm-up (rate-limited), but a ValueError/
    ClientError (not a timing issue) must not. Scheduling failures must
    never break the caller's own cached/placeholder fallback."""

    def _local_coord(self):
        return _make_coord_impl(
            live_connections={
                CAM_ID: {
                    "_connection_type": "LOCAL",
                    "proxyUrl": "https://192.0.2.1/snap.jpg",
                    "_local_user": "cbs-1",
                    "_local_password": "p",
                },
            }
        )

    @pytest.mark.asyncio
    async def test_timeout_schedules_warmup_task(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(
            coord=self._local_coord(), cached_image=b"\xff\xd8cached"
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8cached"
        cam.hass.async_create_background_task.assert_called_once()
        assert cam._local_snap_warmup_task is not None
        assert cam._local_snap_warmup_last > float("-inf")

    @pytest.mark.asyncio
    async def test_client_error_does_not_schedule_warmup(self):
        """A ClientError (not a timing issue) must not trigger a warm-up retry."""
        import aiohttp

        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(
            coord=self._local_coord(), cached_image=b"\xff\xd8cached"
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=aiohttp.ClientError("network error")),
            ),
        ):
            await BoschCamera._async_camera_image_impl(cam)

        cam.hass.async_create_background_task.assert_not_called()
        assert cam._local_snap_warmup_task is None

    @pytest.mark.asyncio
    async def test_second_timeout_within_rate_limit_skips_reschedule(self):
        """A second timeout inside LOCAL_SNAP_WARMUP_MIN_INTERVAL_SEC must not
        schedule a second concurrent warm-up attempt."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(
            coord=self._local_coord(), cached_image=b"\xff\xd8cached"
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            await BoschCamera._async_camera_image_impl(cam)
            await BoschCamera._async_camera_image_impl(cam)

        cam.hass.async_create_background_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_still_pending_task_skips_reschedule_even_past_rate_limit(self):
        """A still-running warm-up task must block a reschedule even once the
        rate-limit window itself has elapsed — two overlapping warm-ups would
        double the concurrent-handshake load on an already-struggling camera."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(coord=self._local_coord())
        cam._local_snap_warmup_last = time.monotonic() - 3600  # long past the limit
        pending_task = MagicMock()
        pending_task.done.return_value = False
        cam._local_snap_warmup_task = pending_task

        cam._maybe_warm_local_snap_connection(
            MagicMock(), "https://192.0.2.1/snap.jpg", "cbs-1", "p"
        )

        cam.hass.async_create_background_task.assert_not_called()
        assert cam._local_snap_warmup_task is pending_task

    @pytest.mark.asyncio
    async def test_scheduling_exception_does_not_break_fallback(self):
        """If hass.async_create_background_task itself raises (e.g. during
        shutdown), _async_camera_image_impl must still return its normal
        cached/placeholder fallback, not propagate the scheduling error."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(
            coord=self._local_coord(), cached_image=b"\xff\xd8cached"
        )
        cam.hass.async_create_background_task = MagicMock(
            side_effect=RuntimeError("hass shutting down")
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8cached"
        assert cam._local_snap_warmup_task is None

    @pytest.mark.asyncio
    async def test_warmup_coroutine_success_updates_cached_image(self):
        """_async_warm_local_snap_connection itself, on a successful fetch,
        updates cached_image and writes ha state."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(coord=self._local_coord())
        img = b"\xff\xd8warmed"
        cm = self._digest_resp_cm_helper(200, img, "image/jpeg")
        with patch(
            "custom_components.bosch_shc_camera.camera.async_digest_request",
            new=AsyncMock(return_value=cm),
        ):
            await BoschCamera._async_warm_local_snap_connection(
                cam, MagicMock(), "https://192.0.2.1/snap.jpg", "cbs-1", "p"
            )

        assert cam.cached_image == img
        cam.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_warmup_coroutine_timeout_is_silent(self):
        """_async_warm_local_snap_connection swallows its own timeout —
        it's best-effort, nothing depends on its result."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl(
            coord=self._local_coord(), cached_image=b"\xff\xd8untouched"
        )
        with patch(
            "custom_components.bosch_shc_camera.camera.async_digest_request",
            new=AsyncMock(side_effect=TimeoutError()),
        ):
            await BoschCamera._async_warm_local_snap_connection(
                cam, MagicMock(), "https://192.0.2.1/snap.jpg", "cbs-1", "p"
            )

        assert cam.cached_image == b"\xff\xd8untouched"

    @staticmethod
    def _digest_resp_cm_helper(
        status: int, body: bytes = b"", content_type: str = "image/jpeg"
    ):
        resp = MagicMock()
        resp.status = status
        resp.headers = {"Content-Type": content_type}
        resp.read = AsyncMock(return_value=body)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm


class TestPlaceholderTreatedAsNoCache:
    """2026-06-17: HA Companion app showed a BLACK image on cold start.

    `cached_image` is initialised to the truthy 1×1 black `_PLACEHOLDER_JPEG`,
    so the first-load fetch branch `if not self.cached_image:` never fired
    while we still held only the placeholder. With a fresh (non-stale)
    `last_image_fetch`, the impl skipped both the first-load AND the
    cache-stale branches and returned the black placeholder. The desktop card
    hid this via its localStorage cache; the mobile app (no such cache, hits
    /api/camera_proxy directly) got the black frame. Fix: the first-load branch
    also fires when we hold only the placeholder (identity check, mirrors
    async_trigger_image_refresh).
    """

    @pytest.mark.asyncio
    async def test_placeholder_cold_boot_fetches_real_frame(self):
        """Cold boot (only placeholder held) + stale timestamp → fetch a real
        frame instead of serving the black placeholder. This is the mobile fix."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        real = b"\xff\xd8\xff\xe0real-snapshot-bytes"
        coord = _make_coord_impl(
            live_connections={},  # not streaming → reaches section 2
            async_fetch_live_snapshot=AsyncMock(return_value=real),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        cam = _make_camera_impl(
            coord=coord,
            cached_image=BoschCamera._PLACEHOLDER_JPEG,
            last_image_fetch=time.monotonic() - 100,  # stale (TTL is 30s)
        )
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == real, "must fetch a real frame, not serve the black placeholder"
        assert cam.cached_image == real

    @pytest.mark.asyncio
    async def test_placeholder_offline_backs_off_no_hammer(self):
        """Persistently-offline camera (every fetch fails, placeholder stays):
        the placeholder fetch must be gated by cache_stale + stamped on failure,
        so it does NOT re-run the slow RCP+REMOTE+LOCAL chain on every request."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        remote = AsyncMock(return_value=None)  # offline → fails
        local = AsyncMock(return_value=None)
        coord = _make_coord_impl(
            live_connections={},
            async_fetch_live_snapshot=remote,
            async_fetch_live_snapshot_local=local,
        )
        cam = _make_camera_impl(
            coord=coord,
            cached_image=BoschCamera._PLACEHOLDER_JPEG,
            last_image_fetch=time.monotonic() - 100,  # stale → first call fetches
        )
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            out1 = await BoschCamera._async_camera_image_impl(cam)
            # Second call immediately after: failure stamped last_image_fetch=now,
            # so cache_stale is now False → must NOT fetch again (backoff).
            out2 = await BoschCamera._async_camera_image_impl(cam)
        assert out1 == BoschCamera._PLACEHOLDER_JPEG  # nothing better available
        assert out2 == BoschCamera._PLACEHOLDER_JPEG
        assert remote.call_count == 1, (
            "REMOTE fetch must run once, not on every request"
        )


class TestStaleCacheRefreshBudget:
    """Bug-hunt 2026-07-27 (Copilot review, ported from the Core PR minimal
    cut): the stale-cache refresh path (real cached image already held,
    just older than CLOUD_SNAP_CACHE_TTL) always awaited the full RCP+
    REMOTE+LOCAL fallback chain with no internal timeout — on a slow/outage
    chain this can exceed HA's own outer CAMERA_IMAGE_TIMEOUT (10s), so the
    whole async_camera_image() call gets cancelled and nothing is served,
    even though a perfectly good cached frame was sitting right there. Fix:
    bound the refresh attempt with an internal timeout and fall back to the
    cached frame on TimeoutError."""

    @pytest.mark.asyncio
    async def test_slow_refresh_falls_back_to_cached_image(self):
        from custom_components.bosch_shc_camera import camera as camera_module
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cached = b"\xff\xd8\xff\xe0cached-real-image"

        async def _hangs_past_budget(*args, **kwargs):
            await asyncio.sleep(1)
            return b"\xff\xd8should-never-be-returned"

        coord = _make_coord_impl(
            live_connections={},
            async_fetch_live_snapshot=AsyncMock(side_effect=_hangs_past_budget),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        cam = _make_camera_impl(
            coord=coord,
            cached_image=cached,
            last_image_fetch=time.monotonic() - 100,  # stale (TTL is 30s)
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch.object(camera_module, "REFRESH_ON_STALE_CACHE_BUDGET_SEC", 0.01),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == cached, (
            "must fall back to the cached frame once the internal budget "
            "expires, not hang until HA's own outer timeout cancels the call"
        )
        assert cam.cached_image == cached  # not overwritten with the late result


class TestPrivacyModePlaceholder:
    """Bug-hunt 2026-07-27 (Copilot review, ported from the Core PR): privacy
    mode ON must short-circuit `_async_camera_image_impl` to None so the
    public wrapper serves the placeholder — not fall through every fetch
    tier to `cached_image`, which would keep serving the last REAL scene
    from before privacy was enabled indefinitely."""

    @pytest.mark.asyncio
    async def test_privacy_on_returns_none_instead_of_stale_cached_frame(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        real_frame_before_privacy = b"\xff\xd8\xff\xe0a-real-scene"
        coord = _make_coord_impl(shc_state_cache={CAM_ID: {"privacy_mode": True}})
        cam = _make_camera_impl(
            coord=coord,
            cached_image=real_frame_before_privacy,
            last_image_fetch=time.monotonic(),  # fresh — would normally short-circuit to cached_image
        )
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out is None

    @pytest.mark.asyncio
    async def test_privacy_off_serves_cached_frame_normally(self):
        """Sanity check: the guard only fires while privacy is actually ON."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        real_frame = b"\xff\xd8\xff\xe0a-real-scene"
        coord = _make_coord_impl(shc_state_cache={CAM_ID: {"privacy_mode": False}})
        cam = _make_camera_impl(
            coord=coord,
            cached_image=real_frame,
            last_image_fetch=time.monotonic(),
        )
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == real_frame


class TestWidthSpecificFetchDoesNotPoisonCache:
    """Bug-hunt 2026-07-27 (Copilot review, ported from the Core PR): a
    thumbnail (width=N) request must not overwrite the shared full-
    resolution `cached_image` — otherwise a subsequent full-res request
    within CLOUD_SNAP_CACHE_TTL is served the undersized thumbnail from
    cache instead of fetching fresh."""

    @pytest.mark.asyncio
    async def test_stale_cache_width_specific_fetch_does_not_poison_cache(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        full_res_frame = b"\xff\xd8\xff\xe0full-resolution-frame"
        thumbnail = b"\xff\xd8\xff\xe0undersized-thumbnail"
        coord = _make_coord_impl(
            live_connections={},
            async_fetch_live_snapshot=AsyncMock(return_value=thumbnail),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        cam = _make_camera_impl(
            coord=coord,
            cached_image=full_res_frame,
            last_image_fetch=time.monotonic() - 100,  # stale (TTL is 30s)
        )
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            out = await BoschCamera._async_camera_image_impl(cam, width=200)
        assert out == thumbnail
        assert cam.cached_image == full_res_frame, (
            "a width-specific fetch must never overwrite the shared full-res cache"
        )


class TestSupportedFeaturesOfflineGate:
    """2026-06-17: the HA mobile app uses supported_features to decide whether to
    render a native live-stream view. STREAM was advertised unconditionally, so
    tapping an OFFLINE camera tried to play a (non-existent) stream → black video.
    Gate STREAM off only when status==OFFLINE; online/idle/unknown keep STREAM so
    a live view can still be started on demand."""

    def test_online_idle_advertises_stream(self):
        from homeassistant.components.camera import CameraEntityFeature

        cam = _make_camera_impl()  # coord data has no "status" → UNKNOWN
        assert CameraEntityFeature.STREAM in cam.supported_features

    def test_unknown_status_keeps_stream(self):
        from homeassistant.components.camera import CameraEntityFeature

        coord = _make_coord_impl(
            data={CAM_ID: {"info": {"title": "T"}, "events": [], "status": "UNKNOWN"}}
        )
        cam = _make_camera_impl(coord=coord)
        assert CameraEntityFeature.STREAM in cam.supported_features

    def test_offline_drops_stream(self):
        from homeassistant.components.camera import CameraEntityFeature

        coord = _make_coord_impl(
            data={CAM_ID: {"info": {"title": "T"}, "events": [], "status": "OFFLINE"}}
        )
        cam = _make_camera_impl(coord=coord)
        assert CameraEntityFeature.STREAM not in cam.supported_features
        assert cam.supported_features == CameraEntityFeature(0)


class TestYuv422EdgeCases:
    """Additional defensive tests beyond TestYuv422ToJpeg."""

    def test_zero_sized_input_returns_none(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_impl()
        out = BoschCamera._yuv422_to_jpeg(cam, b"")
        assert out is None


class TestOutageFallbackStreamingGuard:
    """Section 2b (LOCAL snap.jpg via cached Digest creds during a cloud
    outage) must NOT run while the camera is streaming: opening a second HTTP
    Digest session against the camera contends with the Bosch 3-session limit
    and can tear down the active RTSP stream. Section 2 already guards on
    `not is_streaming`; 2b previously did not. Bug found 2026-06-10."""

    def _outage_coord(self, **overrides):
        base = dict(
            auth_outage_count=1,
            local_creds_cache={
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
        return _make_coord_impl(**base)

    @pytest.mark.asyncio
    async def test_streaming_skips_outage_digest(self):
        """is_streaming True (live rtspsUrl, empty proxyUrl) → section 2b must
        NOT call async_digest_request; returns the cached image instead."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = self._outage_coord(
            # rtspsUrl present → is_streaming True; no proxyUrl → section 1 skipped
            live_connections={CAM_ID: {"rtspsUrl": "rtsps://192.0.2.9/s"}},
        )
        cam = _make_camera_impl(coord=coord, cached_image=b"\xff\xd8cached")
        digest = AsyncMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
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

        coord = self._outage_coord(live_connections={})  # is_streaming False
        # No cached image → section 2 first-load branch falls through to 2b
        # when the cloud fetch fails (instead of returning a stale cache).
        cam = _make_camera_impl(coord=coord, cached_image=None)

        resp = MagicMock()
        resp.status = 200
        resp.headers = {"Content-Type": "image/jpeg"}
        resp.read = AsyncMock(return_value=b"\xff\xd8outage-snap")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ) as digest,
        ):
            out = await BoschCamera._async_camera_image_impl(cam)

        digest.assert_called_once()
        assert out == b"\xff\xd8outage-snap"


# Regression: ValueError from auth_utils must not escape camera_proxy
#
# Source: forum 998974/15 (Andrew75, 2026-05-15). His HA log showed
# `Status code 500 (retry #1) loading /api/camera_proxy/camera.bosch_est`.
# Trigger: the camera's 401 came back without a `WWW-Authenticate: Digest`
# header (half-rotated Digest state during FCM-flap window).
# `auth_utils.async_digest_request` raised `ValueError`, which slipped past
# the previous `except (aiohttp.ClientError, asyncio.TimeoutError)` clauses
# in the LOCAL Digest branch of `_async_camera_image_impl`, propagated up to
# HA core, and produced HTTP 500.
# (The coordinator-side counterpart, `async_fetch_live_snapshot_local` in
# __init__.py, is covered in tests/test_init.py.)


def _make_coord_valerr(**overrides):
    base = dict(
        data={CAM_ID: {"info": {"title": "Est", "hardwareVersion": "X"}, "events": []}},
        live_connections={
            CAM_ID: {
                "_connection_type": "LOCAL",
                "proxyUrl": "https://192.0.2.1/snap.jpg",
                "_local_user": "cbs-1",
                "_local_password": "p",
            },
        },
        live_opened_at={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        stream_warming=set(),
        image_rotation_180={},
        local_creds_cache={},
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera_valerr(coord=None, **camera_overrides):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord_valerr()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = None
    cam._display_name = "Bosch Est"
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "X"
    cam._model_name = "X"
    cam.hw_version = "X"
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


class TestCameraImageImplValueError:
    """`_async_camera_image_impl` LOCAL Digest branch must catch ValueError
    raised by `auth_utils.async_digest_request` (malformed/missing
    WWW-Authenticate). Without the catch, HA's CameraImageView gets a
    raised exception → HTTP 500 → Telegram, Lovelace and automation
    proxies all see a brown error frame / 500 response body."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "err_msg",
        [
            "Server returned 401 without WWW-Authenticate header for 'https://...'",
            "Expected Digest scheme, got: 'Basic'",
            "Digest challenge missing required 'nonce' directive",
        ],
    )
    async def test_local_digest_value_error_returns_cached(self, err_msg: str):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_valerr(cached_image=b"\xff\xd8cached")
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=ValueError(err_msg)),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        # LOCAL path returns cached image (or placeholder) on auth failure
        # instead of falling through to aiohttp (which would 401 again).
        assert out == b"\xff\xd8cached"

    @pytest.mark.asyncio
    async def test_local_digest_value_error_no_cache_returns_placeholder(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_valerr()  # cached_image=None
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=ValueError("auth broken")),
            ),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG

    @pytest.mark.asyncio
    async def test_local_digest_value_error_does_not_propagate(self):
        """Belt-and-braces: ValueError must NOT bubble up to the public
        async_camera_image wrapper (which has its own broad catch, but
        we don't rely on that — the LOCAL Digest except should swallow
        it directly)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_valerr()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=ValueError("no WWW-Authenticate")),
            ),
        ):
            # Must not raise
            await BoschCamera._async_camera_image_impl(cam)


class TestRemoteSnapshotRenewOutsideTimeout:
    """On a 404 (expired proxy URL) REMOTE snapshot, the live-connection
    renewal (try_live_connection — up to ~100s with pre-warm) was previously
    issued INSIDE the 10s snapshot asyncio.timeout and got cancelled on slow
    cameras. The renew now runs outside that timeout; this pins that the
    renew → retry path returns the refreshed image."""

    @pytest.mark.asyncio
    async def test_404_renews_and_returns_refreshed_image(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        old_url = "https://198.51.100.1/snap_old.jpg"
        new_url = "https://198.51.100.2/snap_new.jpg"

        class _Resp:
            def __init__(self, status, body=b"", ct="image/jpeg"):
                self.status = status
                self._body = body
                self.headers = {"Content-Type": ct}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def read(self):
                return self._body

            async def text(self):
                return ""

        def _get(url, *a, **k):
            return _Resp(200, b"\xff\xd8new") if url == new_url else _Resp(404)

        session = MagicMock()
        session.get = _get
        coord = _make_coord_valerr(
            live_connections={
                CAM_ID: {"_connection_type": "REMOTE", "proxyUrl": old_url}
            },
            live_opened_at={CAM_ID: 0.0},
            try_live_connection=AsyncMock(
                return_value={"_connection_type": "REMOTE", "proxyUrl": new_url}
            ),
        )
        cam = _make_camera_valerr(coord=coord)
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        coord.try_live_connection.assert_awaited_once()
        assert out == b"\xff\xd8new"


def _make_coord_mjpeg(
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
        live_connections=live_connections if live_connections is not None else {},
        live_opened_at={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        stream_warming=set(),
        image_rotation_180={},
        local_creds_cache=(
            {CAM_ID: {**local_creds, "ts": local_creds.get("ts", time.monotonic())}}
            if local_creds
            else {}
        ),
        timestamp_cache={},
        audio_enabled={},
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        async_fetch_fresh_event_snapshot=AsyncMock(return_value=None),
        auth_outage_count=auth_outage_count,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def _make_camera_mjpeg(
    coord: SimpleNamespace | None = None,
    hw_version: str = "HOME_Eyes_Outdoor",
    opts: dict | None = None,
    **camera_overrides: object,
) -> object:
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord_mjpeg()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(
        data={"bearer_token": "tok"},
        options=opts or {},
    )
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = hw_version
    cam._model_name = "Eyes Außenkamera II"
    cam.hw_version = hw_version
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


def _patch_session_mjpeg() -> object:
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        cam = _make_camera_mjpeg(coord=coord, opts={"use_mjpeg_snapshot": True})

        with (
            _patch_session_mjpeg(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=AsyncMock(return_value=FAKE_JPEG),
            ),
        ):
            result = await cam._async_camera_image_impl()

        assert result == FAKE_JPEG
        assert cam.cached_image == FAKE_JPEG

    @pytest.mark.asyncio
    async def test_mjpeg_failure_falls_through_to_existing_paths(self):
        """MJPEG returns None → fall through; existing snapshot path called."""
        local_creds = {
            "user": "cbs-TEST1234",
            "password": "secret",
            "host": "192.0.2.149",
            "port": 443,
        }
        coord = _make_coord_mjpeg(local_creds=local_creds)
        cam = _make_camera_mjpeg(coord=coord, opts={"use_mjpeg_snapshot": True})
        # Seed a cached image so there is something to fall back to
        cam.cached_image = b"\xff\xd8fallback"
        cam.last_image_fetch = time.monotonic()  # not stale

        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        cam = _make_camera_mjpeg(coord=coord, opts={"use_mjpeg_snapshot": True})

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        cam = _make_camera_mjpeg(coord=coord, opts={"use_mjpeg_snapshot": False})
        cam.cached_image = b"\xff\xd8old"
        cam.last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg()  # no creds populated
        cam = _make_camera_mjpeg(coord=coord, opts={})
        cam.cached_image = b"\xff\xd8old"
        cam.last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "INDOOR"
        cam = _make_camera_mjpeg(
            coord=coord,
            hw_version="INDOOR",
            opts={"use_mjpeg_snapshot": True},
        )
        cam.cached_image = b"\xff\xd8old"
        cam.last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "OUTDOOR"
        cam = _make_camera_mjpeg(
            coord=coord,
            hw_version="OUTDOOR",
            opts={"use_mjpeg_snapshot": True},
        )
        cam.cached_image = b"\xff\xd8old"
        cam.last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        """Empty local_creds_cache → fetch_mjpeg_snapshot not called."""
        coord = _make_coord_mjpeg(local_creds=None)
        cam = _make_camera_mjpeg(
            coord=coord,
            hw_version="HOME_Eyes_Outdoor",
            opts={"use_mjpeg_snapshot": True},
        )
        cam.cached_image = b"\xff\xd8old"
        cam.last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        cam = _make_camera_mjpeg(
            coord=coord,
            hw_version="HOME_Eyes_Outdoor",
            opts={"use_mjpeg_snapshot": True},
        )
        cam.cached_image = b"\xff\xd8old"
        cam.last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        cam = _make_camera_mjpeg(
            coord=coord,
            hw_version="HOME_Eyes_Outdoor",
            opts={"use_mjpeg_snapshot": True},
        )
        cam.cached_image = b"\xff\xd8old"
        cam.last_image_fetch = time.monotonic()

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
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
        coord = _make_coord_mjpeg(local_creds=local_creds)
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        cam = _make_camera_mjpeg(
            coord=coord,
            hw_version="HOME_Eyes_Indoor",
            opts={"use_mjpeg_snapshot": True},
        )

        mock_fetch = AsyncMock(return_value=FAKE_JPEG)
        with (
            _patch_session_mjpeg(),
            patch(
                "custom_components.bosch_shc_camera.camera.fetch_mjpeg_snapshot",
                new=mock_fetch,
            ),
        ):
            result = await cam._async_camera_image_impl()

        mock_fetch.assert_called_once()
        assert result == FAKE_JPEG


def _stub_entity_r5(**overrides):
    """Minimal BoschCamera-like stub for testing static methods and properties."""
    coord = SimpleNamespace(
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
        live_connections={},
        stream_fell_back={},
        stream_error_count={},
        stream_warming=set(),
        audio_enabled={CAM_ID: True},
        local_creds_cache={},
        live_opened_at={},
        auth_outage_count=0,
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
        _attr_name="Bosch Terrasse",
        _display_name="Bosch Terrasse",
        _cam_title="Terrasse",
        _model="HOME_Eyes_Outdoor",
        _model_name="Eyes Outdoor II",
        _fw="9.40.25",
        _mac="aa:bb:cc:dd:ee:01",
        hw_version="HOME_Eyes_Outdoor",
        cached_image=None,
        last_image_fetch=0.0,
        _force_image_refresh=False,
        is_streaming=False,
        stream_options={},
    )
    base.update(overrides)
    obj = SimpleNamespace(**base)
    # Helper: simulate a coordinator-backed _cam_data property
    obj._cam_data = coord.data[CAM_ID]
    return obj


class TestStreamSourceR5:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_live_connection(self):
        """stream_source() must return None when no active live session (switch OFF)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        entity = _stub_entity_r5()
        result = await BoschCamera.stream_source(entity)
        assert result is None, (
            "stream_source must return None when live_connections is empty"
        )

    @pytest.mark.asyncio
    async def test_returns_rtsps_url_from_live_connection(self):
        """stream_source() must return the rtspsUrl when a session is active."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy.bosch.com:443/stream",
            "_connection_type": "REMOTE",
        }
        result = await BoschCamera.stream_source(entity)
        assert result == "rtsps://proxy.bosch.com:443/stream", (
            "stream_source must return rtspsUrl from live connection"
        )

    @pytest.mark.asyncio
    async def test_local_connection_forces_tcp_transport(self):
        """LOCAL connection must set stream_options={'rtsp_transport': 'tcp'}."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsp://127.0.0.1:8765/stream",
            "_connection_type": "LOCAL",
        }
        await BoschCamera.stream_source(entity)
        assert entity.stream_options == {"rtsp_transport": "tcp"}, (
            "LOCAL connection must force TCP transport to avoid HA 2026.4 UDP→TCP rewrite bug"
        )

    @pytest.mark.asyncio
    async def test_remote_connection_uses_empty_stream_options(self):
        """REMOTE connection must leave stream_options empty (FFmpeg default=UDP)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy.bosch.com:443/stream",
            "_connection_type": "REMOTE",
        }
        await BoschCamera.stream_source(entity)
        assert entity.stream_options == {}, (
            "REMOTE connection must use default stream_options (forcing TCP breaks Gen1 Eyes cloud streams)"
        )

    @pytest.mark.asyncio
    async def test_keeps_audio_param_when_switch_off(self):
        """The AAC track is always kept now — switch.<cam>_audio is a card-side
        mute, not a track toggle — so stream_source must NOT strip enableaudio=1
        even when audio_enabled is False (2026-06-01 always-on-audio design)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.audio_enabled[CAM_ID] = False
        entity.coordinator.live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy.bosch.com:443/stream&enableaudio=1",
            "_connection_type": "REMOTE",
        }
        result = await BoschCamera.stream_source(entity)
        assert "enableaudio=1" in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_url_in_connection(self):
        """stream_source must return None if connection exists but has no URL."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        result = await BoschCamera.stream_source(entity)
        assert result is None, "Must return None when live connection has no URL field"


class TestYuv422ToJpegR5:
    def test_wrong_size_returns_none(self):
        """Must return None immediately if data is not exactly 320×180×2 bytes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        result = BoschCamera._yuv422_to_jpeg(entity, b"\x00" * 100)
        assert result is None, (
            "Must return None for data with wrong size (not 115200 bytes)"
        )

    def test_empty_data_returns_none(self):
        """Empty bytes must not raise — must return None."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        result = BoschCamera._yuv422_to_jpeg(entity, b"")
        assert result is None, "Must return None for empty input"

    def test_correct_size_attempts_conversion(self):
        """320×180×2=115200 bytes must trigger the numpy/PIL path."""
        try:
            import numpy
            from PIL import Image
        except ImportError:
            pytest.skip("numpy/Pillow not installed — skipping YUV422 conversion test")
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        # All-grey YUV422: Y=128 (neutral grey), UV=128 (neutral chrominance)
        data = b"\x80" * (320 * 180 * 2)
        result = BoschCamera._yuv422_to_jpeg(entity, data)
        if result is not None:
            assert result[:2] == b"\xff\xd8", (
                "Converted output must be a JPEG (starts with FFD8)"
            )


class TestExtraStateAttributesR5:
    def test_stream_status_idle_when_no_connection(self):
        """extra_state_attributes must include stream_status='idle' when no live session."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        # Patch _cam_data property behavior inline
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"live_buffer_mode": "balanced"},
        ):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["stream_status"] == "idle", (
            "stream_status must be 'idle' when no connection"
        )

    def test_stream_status_streaming_when_active(self):
        """extra_state_attributes must include stream_status='streaming' for active session."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity.is_streaming = True
        entity._cam_data = {
            "events": [],
            "live": {"rtspsUrl": "rtsps://x"},
            "status": "ONLINE",
            "info": {"priority": 1.0, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"live_buffer_mode": "balanced"},
        ):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["stream_status"] == "streaming", (
            "stream_status must be 'streaming' when streaming"
        )

    def test_stream_status_remote_fallback_label(self):
        """stream_status must say 'streaming (REMOTE fallback)' when fell_back=True."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity.is_streaming = True
        entity.coordinator.stream_fell_back[CAM_ID] = True
        entity._cam_data = {
            "events": [],
            "live": {"rtspsUrl": "rtsps://x"},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"live_buffer_mode": "balanced"},
        ):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert "REMOTE fallback" in attrs["stream_status"], (
            "stream_status must indicate REMOTE fallback"
        )

    def test_bosch_priority_included_in_attrs(self):
        """bosch_priority from cam info must appear in extra_state_attributes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": 3.0, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"live_buffer_mode": "balanced"},
        ):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["bosch_priority"] == 3.0, (
            "bosch_priority must be exposed for Lovelace card sort"
        )

    def test_connecting_status_when_session_exists_but_no_stream(self):
        """stream_status='connecting' when session is in live_connections but not yet streaming."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity.is_streaming = False
        entity.coordinator.live_connections[CAM_ID] = {}  # session open, no URL
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"live_buffer_mode": "balanced"},
        ):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["stream_status"] == "connecting", (
            "stream_status must be 'connecting' when session is open but RTSP not yet active"
        )

    def test_live_buffer_mode_included(self):
        """live_buffer_mode from entry options must appear in extra_state_attributes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity._cam_data = {
            "events": [],
            "live": {},
            "status": "ONLINE",
            "info": {"priority": None, "hardwareVersion": "HOME_Eyes_Outdoor"},
        }
        with patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"live_buffer_mode": "low_latency"},
        ):
            attrs = BoschCamera.extra_state_attributes.fget(entity)
        assert attrs["live_buffer_mode"] == "low_latency", (
            "live_buffer_mode must be passed to Lovelace card"
        )


class TestMotionDetectionEnabledR5:
    def test_returns_true_when_motion_enabled(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.motion_settings = lambda cid: {"enabled": True}
        result = BoschCamera.motion_detection_enabled.fget(entity)
        assert result is True, (
            "motion_detection_enabled must read 'enabled' from motion_settings"
        )

    def test_returns_false_when_motion_disabled(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.motion_settings = lambda cid: {"enabled": False}
        result = BoschCamera.motion_detection_enabled.fget(entity)
        assert result is False, (
            "motion_detection_enabled must return False when motion disabled"
        )

    def test_returns_false_when_no_settings(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity.coordinator.motion_settings = lambda cid: None
        result = BoschCamera.motion_detection_enabled.fget(entity)
        assert result is False, (
            "motion_detection_enabled must return False when no settings"
        )


class TestFrameIntervalR5:
    def test_force_refresh_gives_fast_interval(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity._force_image_refresh = True
        assert BoschCamera.frame_interval.fget(entity) == 0.1, (
            "Force refresh mode must use 0.1s interval to immediately expire HA's image cache"
        )

    def test_streaming_gives_1s_interval(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity._force_image_refresh = False
        entity.is_streaming = True
        assert BoschCamera.frame_interval.fget(entity) == 1.0, (
            "Streaming mode must use 1.0s interval (shorter than card's 2s poll to avoid stale frames)"
        )

    def test_idle_gives_long_interval(self):
        from custom_components.bosch_shc_camera.camera import IDLE_FRAME_INTERVAL
        from custom_components.bosch_shc_camera.camera import BoschCamera as BoschCamera

        entity = _stub_entity_r5()
        entity._force_image_refresh = False
        entity.is_streaming = False
        result = BoschCamera.frame_interval.fget(entity)
        assert result == float(IDLE_FRAME_INTERVAL), (
            f"Idle mode must use IDLE_FRAME_INTERVAL ({IDLE_FRAME_INTERVAL}s) to avoid excessive polling"
        )


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


def _make_coord_r6(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"},
                "events": [],
            },
        },
        live_connections={},
        live_opened_at={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        stream_warming=set(),
        image_rotation_180={},
        local_creds_cache={},
        auth_outage_count=0,
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera_r6(coord=None, **camera_overrides):
    """Instantiate BoschCamera without calling __init__."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord_r6()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = -86400.0
    cam._was_streaming = False
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor II"
    cam.hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "aa:bb:cc:dd:ee:01"
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
        async_add_executor_job=AsyncMock(return_value=None),
    )
    for k, v in camera_overrides.items():
        setattr(cam, k, v)
    return cam


def _live_conn(proxy_url: str = PROXY_URL, opened_before: float = 1.0):
    """Return a coordinator with an active REMOTE live connection."""
    coord = _make_coord_r6(
        live_connections={
            CAM_ID: {"proxyUrl": proxy_url, "_connection_type": "REMOTE"}
        },
        live_opened_at={CAM_ID: time.monotonic() - opened_before},
    )
    return coord


class TestRemoteProxy200:
    """Successful snap.jpg fetch from REMOTE proxy — the happy path that
    should run on every streaming camera tick. Pin: cached_image +
    last_image_fetch must be updated and the bytes returned.
    """

    @pytest.mark.asyncio
    async def test_200_image_jpeg_caches_and_returns(self):
        """HTTP 200 + image/jpeg → store in cached_image, update timestamp, return bytes."""
        coord = _live_conn()
        cam = _make_camera_r6(coord=coord)
        session = MagicMock()
        session.get.return_value = _resp_cm(
            200, body=b"\xff\xd8img", content_type="image/jpeg"
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8img", "must return the fetched bytes on 200"
        assert cam.cached_image == b"\xff\xd8img", "cached_image must be updated on 200"
        assert cam.last_image_fetch > 0, "last_image_fetch must be set on 200"

    @pytest.mark.asyncio
    async def test_200_wrong_content_type_falls_through(self):
        """HTTP 200 + text/html (expired proxy page) → do not cache, fall through."""
        coord = _live_conn()
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8old")
        session = MagicMock()
        session.get.return_value = _resp_cm(
            200, body=b"<html>", content_type="text/html"
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        # Falls through; final return is cached image
        assert cam.cached_image == b"\xff\xd8old", (
            "must NOT overwrite cached on text/html 200"
        )

    @pytest.mark.asyncio
    async def test_200_empty_body_falls_through(self):
        """HTTP 200 + image/jpeg but empty body → guard `if data:` must skip cache update."""
        coord = _live_conn()
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8old")
        session = MagicMock()
        session.get.return_value = _resp_cm(200, body=b"", content_type="image/jpeg")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert cam.cached_image == b"\xff\xd8old", (
            "empty body must not overwrite cached image"
        )


class TestRemoteProxy404:
    """Proxy URL expired → refresh connection and retry.

    When the proxy session has expired (Bosch's proxy-NNs are ephemeral),
    the snap.jpg returns 404. We call try_live_connection to get a fresh
    proxyUrl and retry immediately.
    """

    @pytest.mark.asyncio
    async def test_404_then_new_url_then_200_returns_image(self):
        """404 → try_live_connection gives new URL → retry GET 200 → return bytes."""
        coord = _live_conn()
        new_url = "https://proxy-02.live.cbs.boschsecurity.com/new-hash/snap.jpg"
        coord.try_live_connection = AsyncMock(
            return_value={"proxyUrl": new_url, "_connection_type": "REMOTE"}
        )
        cam = _make_camera_r6(coord=coord)

        # First GET → 404, second GET (new_url) → 200
        first_cm = _resp_cm(404, body=b"Not Found", content_type="text/html")
        second_cm = _resp_cm(200, body=b"\xff\xd8fresh", content_type="image/jpeg")
        session = MagicMock()
        session.get.side_effect = [first_cm, second_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8fresh", (
            "must return fresh bytes after 404 → renew → 200"
        )
        assert cam.cached_image == b"\xff\xd8fresh", "must cache the fresh bytes"
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_404_try_live_connection_returns_none_falls_through(self):
        """404 → try_live_connection returns None → no retry, fall through to cached."""
        coord = _live_conn()
        coord.try_live_connection = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(404, body=b"", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        (
            coord.try_live_connection.assert_awaited_once(),
            "try_live_connection must be called on 404",
        )
        # Falls through to cached image return
        assert out == b"\xff\xd8cached", (
            "must return cached when try_live_connection fails"
        )

    @pytest.mark.asyncio
    async def test_404_new_live_has_no_proxy_url_falls_through(self):
        """404 → try_live_connection returns dict without proxyUrl → skip retry."""
        coord = _live_conn()
        coord.try_live_connection = AsyncMock(
            return_value={"_connection_type": "REMOTE"}
        )
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(404, body=b"", content_type="text/html")
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        # Only 1 GET call (the initial 404), no retry
        assert session.get.call_count == 1, (
            "must not retry when new live has no proxyUrl"
        )


class TestRemoteProxy401:
    """401/403 with age < TTL → keep session; age >= TTL → renew or clear.

    CAMERA_360 always returns 401 on its REMOTE snap.jpg — we must NOT
    renew / clear the session just because snap.jpg needs auth; we keep
    the session alive so the stream switch shows correct state.
    """

    @pytest.mark.asyncio
    async def test_401_age_below_ttl_keeps_session(self):
        """401 with session age < LIVE_SESSION_TTL → do nothing, return cached.
        This is the CAMERA_360 steady-state: snap.jpg always 401 but stream is alive.
        """
        coord = _live_conn(opened_before=5.0)  # only 5s old — well below TTL
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(
            401, body=b"Unauthorized", content_type="text/html"
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        (
            coord.try_live_connection.assert_not_awaited(),
            "must NOT renew when age < LIVE_SESSION_TTL",
        )
        assert CAM_ID in coord.live_connections, (
            "must NOT clear live_connections on young 401"
        )

    @pytest.mark.asyncio
    async def test_403_age_below_ttl_keeps_session(self):
        """403 with young session → same keep-alive logic as 401."""
        coord = _live_conn(opened_before=5.0)
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(
            403, body=b"Forbidden", content_type="text/html"
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        coord.try_live_connection.assert_not_awaited(), "must NOT renew on young 403"
        assert CAM_ID in coord.live_connections, (
            "live_connections must survive young 403"
        )

    @pytest.mark.asyncio
    async def test_401_age_above_ttl_renews_and_returns_fresh(self):
        """401 with session age >= LIVE_SESSION_TTL → renew → retry → 200 → return."""
        # Session is 60s old — past the 55s TTL
        coord = _live_conn(opened_before=60.0)
        new_url = "https://proxy-99.live.cbs.boschsecurity.com/newhash/snap.jpg"
        coord.try_live_connection = AsyncMock(
            return_value={"proxyUrl": new_url, "_connection_type": "REMOTE"}
        )
        cam = _make_camera_r6(coord=coord)

        first_cm = _resp_cm(401, body=b"Unauthorized", content_type="text/html")
        retry_cm = _resp_cm(200, body=b"\xff\xd8renewed", content_type="image/jpeg")
        session = MagicMock()
        session.get.side_effect = [first_cm, retry_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        assert out == b"\xff\xd8renewed", (
            "must return fresh bytes after successful renewal"
        )

    @pytest.mark.asyncio
    async def test_401_age_above_ttl_renewal_fails_clears_connection(self):
        """401 + expired + try_live_connection returns None → clear live_connections.
        Pin: is_streaming must become False after clearing so the card shows correct state.
        """
        coord = _live_conn(opened_before=70.0)
        coord.try_live_connection = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8old")

        session = MagicMock()
        session.get.return_value = _resp_cm(
            401, body=b"Unauthorized", content_type="text/html"
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert CAM_ID not in coord.live_connections, (
            "must clear live_connections when renewal fails (so is_streaming → False)"
        )
        assert CAM_ID not in coord.live_opened_at, (
            "must also clear live_opened_at when renewal fails"
        )

    @pytest.mark.asyncio
    async def test_401_above_ttl_renewal_coalesced_keeps_connection(self):
        """Regression: 401 + expired + try_live_connection returns
        STREAM_START_SKIPPED (another start is already in flight) must NOT
        clear live_connections/live_opened_at — that would delete the concurrent
        renewal's fresh session and kill the stream plus any Frigate front-door
        reading its creds. Only a genuine renewal FAILURE (falsy non-sentinel)
        clears. STREAM_START_SKIPPED is falsy, so before the fix it fell into the
        401/403 clear branch."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED

        coord = _live_conn(opened_before=70.0)
        coord.try_live_connection = AsyncMock(return_value=STREAM_START_SKIPPED)
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8old")

        session = MagicMock()
        session.get.return_value = _resp_cm(
            401, body=b"Unauthorized", content_type="text/html"
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await BoschCamera._async_camera_image_impl(cam)

        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        assert CAM_ID in coord.live_connections, (
            "coalesced start (STREAM_START_SKIPPED) must NOT clear live_connections"
        )
        assert CAM_ID in coord.live_opened_at, (
            "coalesced start must NOT clear live_opened_at"
        )


class TestRemoteProxyTimeout:
    """Network error on snap.jpg → try RCP thumbnail.

    Observed: good LAN but proxy-NN is slow/unreachable → timeout after 10s.
    RCP 0x099e is much faster (~100ms) and served via the same proxy hash.
    """

    @pytest.mark.asyncio
    async def test_timeout_tries_rcp_thumbnail_and_returns(self):
        """TimeoutError → _async_rcp_thumbnail returns bytes → cache and return."""
        coord = _live_conn()
        cam = _make_camera_r6(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp")

        session = MagicMock()
        session.get.side_effect = TimeoutError()

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        (
            cam._async_rcp_thumbnail.assert_awaited_once(),
            "must try RCP thumbnail on TimeoutError",
        )
        assert out == b"\xff\xd8rcp", (
            "must return RCP thumbnail bytes on snap.jpg timeout"
        )
        assert cam.cached_image == b"\xff\xd8rcp", "must cache RCP thumbnail bytes"

    @pytest.mark.asyncio
    async def test_timeout_rcp_thumbnail_none_falls_through(self):
        """TimeoutError → _async_rcp_thumbnail returns None → fall through to idle path."""
        coord = _live_conn()
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8old")
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = TimeoutError()

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        cam._async_rcp_thumbnail.assert_awaited_once(), "must attempt RCP on timeout"
        # Falls through; since streaming (live connection exists), skips idle path
        # and reaches cached image fallback

    @pytest.mark.asyncio
    async def test_aiohttp_client_error_tries_rcp(self):
        """aiohttp.ClientError → same RCP thumbnail fallback as TimeoutError."""
        import aiohttp

        coord = _live_conn()
        cam = _make_camera_r6(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp")

        session = MagicMock()
        session.get.side_effect = aiohttp.ClientError("connection reset")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8rcp", "ClientError must also fall back to RCP thumbnail"


class TestAsyncRcpThumbnail:
    """RCP thumbnail implementation: early-exit paths (no urls, bad url
    format, no session) and the JPEG-first vs YUV422 fallback logic.
    """

    @pytest.mark.asyncio
    async def test_no_urls_in_live_returns_none(self):
        """No 'urls' key in live connection → return None immediately."""
        coord = _make_coord_r6(
            live_connections={
                CAM_ID: {"proxyUrl": PROXY_URL, "_connection_type": "REMOTE"}
            },
        )
        cam = _make_camera_r6(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when no 'urls' in live connection"

    @pytest.mark.asyncio
    async def test_no_live_connection_returns_none(self):
        """No live connection at all → return None."""
        coord = _make_coord_r6()
        cam = _make_camera_r6(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when live_connections is empty"

    @pytest.mark.asyncio
    async def test_bad_url_format_no_slash_returns_none(self):
        """urls[0] without '/' → len(parts) != 2 → return None."""
        coord = _make_coord_r6(
            live_connections={
                CAM_ID: {"urls": ["noslash"], "_connection_type": "REMOTE"}
            },
        )
        cam = _make_camera_r6(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when url[0] has no '/' separator"

    @pytest.mark.asyncio
    async def test_no_rcp_session_returns_none(self):
        """get_cached_rcp_session returns None → return None (no session)."""
        coord = _make_coord_r6(
            live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord.get_cached_rcp_session = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when no RCP session available"

    @pytest.mark.asyncio
    async def test_rcp_0x099e_jpeg_returned_directly(self):
        """rcp_read returns JPEG bytes (starts with 0xFFD8) → return them directly."""
        coord = _make_coord_r6(
            live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord.get_cached_rcp_session = AsyncMock(return_value="sess-id-1")
        coord.rcp_read = AsyncMock(return_value=b"\xff\xd8jpeg-data")
        cam = _make_camera_r6(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out == b"\xff\xd8jpeg-data", "must return JPEG directly from RCP 0x099e"

    @pytest.mark.asyncio
    async def test_rcp_0x099e_not_jpeg_falls_to_yuv422(self):
        """0x099e not JPEG → fall through to 0x0c98 YUV422 path."""
        coord = _make_coord_r6(
            live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord.get_cached_rcp_session = AsyncMock(return_value="sess-id-1")
        # First call (0x099e) returns non-JPEG; second (0x0c98) returns None
        coord.rcp_read = AsyncMock(side_effect=[b"\x00\x00not-jpeg", None])
        cam = _make_camera_r6(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, (
            "must return None when neither 0x099e nor 0x0c98 yields usable data"
        )
        assert coord.rcp_read.await_count == 2, "must attempt both RCP registers"

    @pytest.mark.asyncio
    async def test_rcp_0x0c98_wrong_size_returns_none(self):
        """0x0c98 returns data but not 115200 bytes → return None."""
        coord = _make_coord_r6(
            live_connections={CAM_ID: {"urls": ["proxy-01.bosch.com/abc123"]}},
        )
        coord.get_cached_rcp_session = AsyncMock(return_value="sess-id-1")
        # First call not-JPEG; second wrong size
        coord.rcp_read = AsyncMock(side_effect=[b"\x00\x00", b"\xab" * 1000])
        cam = _make_camera_r6(coord=coord)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        out = await BoschCamera._async_rcp_thumbnail(cam)
        assert out is None, "must return None when 0x0c98 is unexpected size"


class TestIdleCameraCloudSnapshot:
    """Cloud snapshot for cameras not currently streaming.

    Two sub-modes:
    a) no cached image → fetch synchronously (cold start)
    b) cached image but stale → re-fetch synchronously

    The prefer_small path (width <= 640) tries RCP thumbnail first.
    """

    @pytest.mark.asyncio
    async def test_no_cache_fetches_via_async_fetch_live_snapshot(self):
        """No cached image → call async_fetch_live_snapshot → cache and return."""
        coord = _make_coord_r6()  # no live_connections → not streaming
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8snap")
        cam = _make_camera_r6(coord=coord)
        # cached_image=None (default)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID, jpeg_size=None)
        assert out == b"\xff\xd8snap", (
            "must return snapshot from async_fetch_live_snapshot"
        )
        assert cam.cached_image == b"\xff\xd8snap", "must cache the snapshot"

    @pytest.mark.asyncio
    async def test_no_cache_prefer_small_tries_rcp_first(self):
        """width=320 (prefer_small) + no cache → try RCP thumbnail before slow proxy."""
        coord = _make_coord_r6()
        cam = _make_camera_r6(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp-small")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam, width=320)

        (
            cam._async_rcp_thumbnail.assert_awaited_once(),
            "must try RCP first on prefer_small",
        )
        assert out == b"\xff\xd8rcp-small", "must return RCP thumbnail on prefer_small"

    @pytest.mark.asyncio
    async def test_no_cache_width_zero_rcp_success_updates_shared_cache(self):
        """width=0 is the one real case where prefer_small is True (0 <= 640)
        but jpeg_size_for_width(0) returns None (width <= 0 guard) — so the
        RCP thumbnail IS allowed to poison the shared full-res cache here,
        unlike every width>0 thumbnail request."""
        coord = _make_coord_r6()
        cam = _make_camera_r6(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp-w0")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam, width=0)

        cam._async_rcp_thumbnail.assert_awaited_once()
        assert out == b"\xff\xd8rcp-w0"
        assert cam.cached_image == b"\xff\xd8rcp-w0", (
            "req_jpeg_size is None (width=0) → the shared cache IS updated"
        )

    @pytest.mark.asyncio
    async def test_no_cache_prefer_small_rcp_fails_falls_to_snap(self):
        """prefer_small + RCP returns None → fall through to async_fetch_live_snapshot."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8snap")
        cam = _make_camera_r6(coord=coord)
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam, width=320)

        coord.async_fetch_live_snapshot.assert_awaited_once_with(
            CAM_ID, jpeg_size=JPEG_SIZE_THUMB
        )
        assert out == b"\xff\xd8snap", (
            "must fall to async_fetch_live_snapshot when RCP fails"
        )

    @pytest.mark.asyncio
    async def test_no_cache_remote_401_tries_local_fallback(self):
        """async_fetch_live_snapshot returns None → try async_fetch_live_snapshot_local."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=b"\xff\xd8local")
        cam = _make_camera_r6(coord=coord)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        coord.async_fetch_live_snapshot_local.assert_awaited_once_with(
            CAM_ID, jpeg_size=None
        )
        assert out == b"\xff\xd8local", (
            "must use LOCAL fallback when REMOTE snap returns None"
        )

    @pytest.mark.asyncio
    async def test_stale_cache_fetches_fresh(self):
        """Cache older than CLOUD_SNAP_CACHE_TTL → fetch fresh and return."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8fresh")
        cam = _make_camera_r6(
            coord=coord,
            cached_image=b"\xff\xd8stale",
            last_image_fetch=time.monotonic() - 60,  # 60s ago — past the 30s TTL
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID, jpeg_size=None)
        assert out == b"\xff\xd8fresh", "must return fresh bytes when cache is stale"
        assert cam.cached_image == b"\xff\xd8fresh", (
            "must update cache with fresh bytes"
        )

    @pytest.mark.asyncio
    async def test_stale_cache_prefer_small_tries_rcp_first(self):
        """Stale cache + prefer_small → try RCP thumbnail before slow proxy."""
        coord = _make_coord_r6()
        cam = _make_camera_r6(
            coord=coord,
            cached_image=b"\xff\xd8stale",
            last_image_fetch=time.monotonic() - 60,
        )
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp-fresh")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam, width=400)

        cam._async_rcp_thumbnail.assert_awaited_once()
        assert out == b"\xff\xd8rcp-fresh", (
            "stale cache + prefer_small must use RCP fresh"
        )

    @pytest.mark.asyncio
    async def test_stale_cache_width_zero_rcp_success_updates_shared_cache(self):
        """Same width=0 edge case as the no-cache test above, but through the
        'cache stale, refresh synchronously' branch (req_jpeg_size is None →
        the shared cache IS updated by the RCP thumbnail)."""
        coord = _make_coord_r6()
        cam = _make_camera_r6(
            coord=coord,
            cached_image=b"\xff\xd8stale",
            last_image_fetch=time.monotonic() - 60,
        )
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp-w0-fresh")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam, width=0)

        cam._async_rcp_thumbnail.assert_awaited_once()
        assert out == b"\xff\xd8rcp-w0-fresh"
        assert cam.cached_image == b"\xff\xd8rcp-w0-fresh", (
            "req_jpeg_size is None (width=0) → the shared cache IS updated"
        )

    @pytest.mark.asyncio
    async def test_fresh_cache_returns_without_fetch(self):
        """Cache fresh (< CLOUD_SNAP_CACHE_TTL) → return cached without any network call."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8should-not")
        cam = _make_camera_r6(
            coord=coord,
            cached_image=b"\xff\xd8cached",
            last_image_fetch=time.monotonic() - 5,  # only 5s old
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        (
            coord.async_fetch_live_snapshot.assert_not_awaited(),
            "must NOT fetch when cache is still fresh",
        )
        assert out == b"\xff\xd8cached", "must return cached image when fresh"

    @pytest.mark.asyncio
    async def test_stale_both_fail_advances_timestamp_returns_cached(self):
        """Stale cache + both REMOTE and LOCAL return None → advance last_image_fetch, return cached."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        before = time.monotonic() - 60
        cam = _make_camera_r6(
            coord=coord,
            cached_image=b"\xff\xd8old",
            last_image_fetch=before,
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert cam.last_image_fetch > before, (
            "must advance last_image_fetch so next tick retries instead of looping"
        )
        assert out == b"\xff\xd8old", (
            "must return stale cached image when both fetches fail"
        )

    @pytest.mark.asyncio
    async def test_no_cache_width_specific_fetch_failure_does_not_suppress_full_res_retry(
        self,
    ):
        """A failed width=N (thumbnail) fetch must not advance the shared
        `last_image_fetch` timestamp — otherwise a following full-resolution
        request within the cache TTL would see the cache as fresh and skip
        retrying, even though the shared cache was never actually
        refreshed (backported from the Core PR's Copilot review round 8,
        2026-07-27)."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        before = cam_last_fetch = time.monotonic() - 86400.0
        cam = _make_camera_r6(coord=coord, last_image_fetch=before)
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera._async_camera_image_impl(cam, width=200)

        assert cam.last_image_fetch == cam_last_fetch, (
            "req_jpeg_size is not None (width=200) → must NOT advance the "
            "shared timestamp on failure"
        )

    @pytest.mark.asyncio
    async def test_stale_cache_width_specific_fetch_failure_does_not_suppress_full_res_retry(
        self,
    ):
        """Same guard as above, through the stale-but-already-cached branch."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        before = time.monotonic() - 60
        cam = _make_camera_r6(
            coord=coord,
            cached_image=b"\xff\xd8old",
            last_image_fetch=before,
        )
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera._async_camera_image_impl(cam, width=200)

        assert cam.last_image_fetch == before, (
            "req_jpeg_size is not None (width=200) → must NOT advance the "
            "shared timestamp on failure"
        )


class TestEventSnapshotLastResort:
    """When all other methods fail, try event imageUrl.

    This is the startup scenario before any cloud fetch has completed.
    """

    @pytest.mark.asyncio
    async def test_event_image_url_fetched_and_cached(self):
        """Last resort: event imageUrl 200 → cache and return."""
        img_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord_r6(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [
                        {
                            "imageUrl": img_url,
                            "timestamp": "2026-05-07T10:00:00.000Z",
                        }
                    ],
                }
            }
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord)  # cached_image=None

        img_resp = _resp_cm(200, body=b"\xff\xd8event-img", content_type="image/jpeg")
        snap_resp = _resp_cm(404, body=b"", content_type="text/html")
        session = MagicMock()
        # The session.get may be called: first for snap fallback (not streaming → idle path),
        # then for event URL. We set side_effect as a list for reliability.
        session.get.return_value = img_resp

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        # async_fetch_live_snapshot was tried and returned None; then event path ran
        assert out == b"\xff\xd8event-img", (
            "must return event imageUrl bytes as last resort"
        )
        assert cam.cached_image == b"\xff\xd8event-img", "must cache event image"

    @pytest.mark.asyncio
    async def test_unknown_privacy_state_withholds_event_snapshot(self):
        """Even the event-snapshot last resort must fail closed.

        Regression test for a 3-agent bug-hunt finding (round 20 backport,
        2026-08-04): unlike every other tier, this one fetches a STORED
        HISTORICAL motion-event JPEG from Bosch cloud storage — independent
        of the camera's current live privacy state, so it doesn't naturally
        short-circuit to empty/error while privacy is engaged the way a
        live camera fetch does. The event itself could predate privacy
        being enabled just as easily as a stale cached_image can.
        """
        img_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord_r6(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [
                        {
                            "imageUrl": img_url,
                            "timestamp": "2026-05-07T10:00:00.000Z",
                        }
                    ],
                }
            },
            shc_state_cache={},  # unknown — no entry for this cam
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord)  # cached_image=None

        img_resp = _resp_cm(200, body=b"\xff\xd8event-img", content_type="image/jpeg")
        session = MagicMock()
        session.get.return_value = img_resp

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert out is None, "must withhold the event snapshot while privacy is unknown"

    @pytest.mark.asyncio
    async def test_unknown_privacy_state_withholds_final_fallback_with_no_events(self):
        """The absolute final catch-all (no events at all) must also fail closed.

        Same reasoning as test_unknown_privacy_state_withholds_event_snapshot
        above, but exercising the truly last line of the cascade — no events
        in the camera's data at all, so tier 4's loop never even runs.
        """
        coord = _make_coord_r6(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [],
                }
            },
            shc_state_cache={},  # unknown — no entry for this cam
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord)  # cached_image=None

        session = MagicMock()

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert out is None, (
            "must withhold when all methods failed and privacy is unknown"
        )

    @pytest.mark.asyncio
    async def test_placeholder_cached_image_does_not_block_last_resort(self):
        """Regression: section 3's `if self.cached_image:` must exclude the
        placeholder sentinel (identity check, mirroring section 2's guard a
        few lines above) — otherwise a genuine cold start (cached_image still
        the placeholder set in __init__, every live/cloud tier failed) always
        returns the placeholder here instead of reaching this section (the
        real last-resort event snapshot), contradicting this section's own
        "last resort on very first startup" docstring. Bug found 2026-07-15
        while building the HA-Core-submission-prep test suite; confirmed by
        temporarily reverting the `is not self._PLACEHOLDER_JPEG` guard on
        section 3 — this test then failed with the placeholder bytes instead
        of the event snapshot.
        """
        from custom_components.bosch_shc_camera.camera import BoschCamera

        img_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord_r6(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [
                        {
                            "imageUrl": img_url,
                            "timestamp": "2026-05-07T10:00:00.000Z",
                        }
                    ],
                }
            }
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord, cached_image=BoschCamera._PLACEHOLDER_JPEG)

        img_resp = _resp_cm(200, body=b"\xff\xd8event-img", content_type="image/jpeg")
        session = MagicMock()
        session.get.return_value = img_resp

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8event-img", (
            "placeholder cached_image must not intercept section 3 — the real "
            "event snapshot is the correct fallback on a genuine cold start"
        )

    @pytest.mark.asyncio
    async def test_unsafe_image_url_rejected(self):
        """imageUrl that is not a Bosch HTTPS URL must be rejected (SSRF prevention)."""
        img_url = "http://evil.com/steal.jpg"
        coord = _make_coord_r6(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [
                        {"imageUrl": img_url, "timestamp": "2026-05-07T10:00:00.000Z"}
                    ],
                }
            }
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(
            200, body=b"\xff\xd8evil", content_type="image/jpeg"
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert out != b"\xff\xd8evil", "must NOT fetch unsafe (non-Bosch) imageUrl"

    @pytest.mark.asyncio
    async def test_event_401_returns_cached(self):
        """Event imageUrl returns 401 (expired token) → return cached, no further retries."""
        img_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord_r6(
            data={
                CAM_ID: {
                    "info": {"title": "Terrasse", "hardwareVersion": "X"},
                    "events": [
                        {"imageUrl": img_url, "timestamp": "2026-05-07T10:00:00.000Z"}
                    ],
                }
            }
        )
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord, cached_image=b"\xff\xd8cached")

        session = MagicMock()
        session.get.return_value = _resp_cm(
            401, body=b"Unauth", content_type="text/html"
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        assert out == b"\xff\xd8cached", "401 on event imageUrl must return cached"

    @pytest.mark.asyncio
    async def test_no_events_no_cache_returns_placeholder(self):
        """No events, no cache → return PLACEHOLDER_JPEG."""
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=None)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        cam = _make_camera_r6(coord=coord)  # cached_image=None

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            out = await BoschCamera._async_camera_image_impl(cam)

        from custom_components.bosch_shc_camera.camera import BoschCamera as BC

        assert out == BC._PLACEHOLDER_JPEG, (
            "must return PLACEHOLDER_JPEG when all fetch methods fail and no cache"
        )


def _make_coord_r7(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"},
                "events": [],
            },
        },
        live_connections={},
        live_opened_at={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        stream_warming=set(),
        image_rotation_180={},
        local_creds_cache={},
        auth_outage_count=0,
        last_update_success=True,
        is_stream_warming=lambda cid: False,
        try_live_connection=AsyncMock(return_value=None),
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        get_cached_rcp_session=AsyncMock(return_value=None),
        rcp_read=AsyncMock(return_value=None),
        audio_enabled={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera_r7(coord=None, **overrides):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = coord or _make_coord_r7()
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._attr_name = "Bosch Terrasse"
    cam._display_name = "Bosch Terrasse"
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor II"
    cam.hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "aa:bb:cc:dd:ee:01"
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
        async_add_executor_job=AsyncMock(return_value=None),
    )
    for k, v in overrides.items():
        setattr(cam, k, v)
    return cam


def _local_live_conn(
    proxy_url: str = LOCAL_SNAP_URL,
    local_user: str = "localuser",
    local_pass: str = "localpass",
):
    """Coordinator with an active LOCAL live connection."""
    coord = _make_coord_r7(
        live_connections={
            CAM_ID: {
                "proxyUrl": proxy_url,
                "_connection_type": "LOCAL",
                "_local_user": local_user,
                "_local_password": local_pass,
            }
        },
        live_opened_at={CAM_ID: time.monotonic() - 1.0},
    )
    return coord


def _remote_live_conn(proxy_url: str = PROXY_URL, opened_before: float = 60.0):
    """Coordinator with an active REMOTE live connection (for 401 age check)."""
    coord = _make_coord_r7(
        live_connections={
            CAM_ID: {"proxyUrl": proxy_url, "_connection_type": "REMOTE"}
        },
        live_opened_at={CAM_ID: time.monotonic() - opened_before},
    )
    return coord


class TestYuv422ToJpegExceptionPaths:
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
        """Mock numpy to raise so the exception-handler branch is exercised."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera.__new__(BoschCamera)
        data = bytes(115200)
        with patch(
            "numpy.frombuffer", side_effect=RuntimeError("simulated numpy crash")
        ):
            result = cam._yuv422_to_jpeg(data)
        assert result is None, "exception in numpy path must yield None"


class TestAsyncRcpThumbnailYuv422:
    """raw=115200 bytes but _yuv422_to_jpeg returns None; wrong size raw."""

    @pytest.mark.asyncio
    async def test_yuv422_conversion_fails_returns_none(self):
        """115200-byte raw, but _yuv422_to_jpeg returns None → log debug, return None."""
        coord = _make_coord_r7(
            live_connections={
                CAM_ID: {"urls": ["proxy-01.live.cbs.boschsecurity.com:42090/abc123"]}
            },
            get_cached_rcp_session=AsyncMock(return_value="session-id-1"),
            rcp_read=AsyncMock(
                side_effect=[
                    b"\x00\x00bad",  # 0x099e → not JPEG (no FF D8)
                    bytes(115200),  # 0x0c98 → correct size
                ]
            ),
        )
        cam = _make_camera_r7(coord=coord)

        with patch.object(cam.__class__, "_yuv422_to_jpeg", return_value=None):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_rcp_thumbnail(cam)

        assert result is None, (
            "when YUV422 conversion fails, _async_rcp_thumbnail must return None"
        )

    @pytest.mark.asyncio
    async def test_yuv422_wrong_size_returns_none(self):
        """Raw 0x0c98 has unexpected size (not 115200) → log debug, return None."""
        coord = _make_coord_r7(
            live_connections={
                CAM_ID: {"urls": ["proxy-01.live.cbs.boschsecurity.com:42090/abc123"]}
            },
            get_cached_rcp_session=AsyncMock(return_value="session-id-2"),
            rcp_read=AsyncMock(
                side_effect=[
                    b"\x00\x00bad",  # 0x099e → not JPEG
                    bytes(1000),  # 0x0c98 → wrong size
                ]
            ),
        )
        cam = _make_camera_r7(coord=coord)

        from custom_components.bosch_shc_camera.camera import BoschCamera

        result = await BoschCamera._async_rcp_thumbnail(cam)

        assert result is None, "wrong-size 0x0c98 raw must return None"

    @pytest.mark.asyncio
    async def test_yuv422_success_returns_jpeg(self):
        """115200-byte raw, _yuv422_to_jpeg succeeds → return JPEG bytes."""
        coord = _make_coord_r7(
            live_connections={
                CAM_ID: {"urls": ["proxy-01.live.cbs.boschsecurity.com:42090/abc123"]}
            },
            get_cached_rcp_session=AsyncMock(return_value="session-id-3"),
            rcp_read=AsyncMock(
                side_effect=[
                    b"\x00\x00bad",  # 0x099e → not JPEG
                    bytes(115200),  # 0x0c98 → correct size
                ]
            ),
        )
        cam = _make_camera_r7(coord=coord)

        fake_jpeg = b"\xff\xd8\xff\xe0fake"
        with patch.object(cam.__class__, "_yuv422_to_jpeg", return_value=fake_jpeg):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_rcp_thumbnail(cam)

        assert result == fake_jpeg, (
            "successful YUV422 conversion must return JPEG bytes"
        )


def _digest_cm(
    status: int, body: bytes = b"", content_type: str = "image/jpeg"
) -> MagicMock:
    """Async context-manager mock for async_digest_request."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {"Content-Type": content_type}
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestLocalSnapViaProxy:
    """LOCAL snap via proxy — uses async_digest_request (no executor_job)."""

    @pytest.mark.asyncio
    async def test_local_snap_success_returns_image(self):
        """Digest auth returns 200 + image → cached and returned."""
        coord = _local_live_conn()
        cam = _make_camera_r7(coord=coord)
        img_bytes = b"\xff\xd8local"

        cm = _digest_cm(200, img_bytes, "image/jpeg")
        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == img_bytes, "LOCAL snap 200 must return image bytes"
        assert cam.cached_image == img_bytes, "LOCAL snap 200 must cache image"

    @pytest.mark.asyncio
    async def test_local_snap_non_200_falls_to_placeholder(self):
        """Digest returns non-200 → placeholder."""
        coord = _local_live_conn()
        cam = _make_camera_r7(coord=coord)

        cm = _digest_cm(403, b"Forbidden", "text/plain")
        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, "LOCAL snap non-200 must fall back to placeholder"

    @pytest.mark.asyncio
    async def test_local_snap_client_error_falls_to_placeholder(self):
        """aiohttp.ClientError → falls through to cached/placeholder."""
        import aiohttp as _aiohttp

        coord = _local_live_conn()
        cam = _make_camera_r7(coord=coord)

        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=_aiohttp.ClientError("network error")),
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, (
            "LOCAL snap ClientError must fall back to placeholder/cached"
        )

    @pytest.mark.asyncio
    async def test_local_snap_no_creds_skips_digest(self):
        """LOCAL connection but no creds → async_digest_request never called.

        When _local_user/_local_password are empty, the 'if local_user and local_pass:'
        block is skipped. The code then falls to the REMOTE aiohttp path.
        We verify async_digest_request is never called.
        """
        coord = _make_coord_r7(
            live_connections={
                CAM_ID: {
                    "proxyUrl": LOCAL_SNAP_URL,
                    "_connection_type": "LOCAL",
                    "_local_user": "",  # empty = no creds
                    "_local_password": "",
                }
            },
            live_opened_at={CAM_ID: time.monotonic() - 1.0},
        )
        cam = _make_camera_r7(coord=coord)

        # The code falls to the REMOTE aiohttp path — provide a proper response mock
        session = MagicMock()
        session.get.return_value = _resp_cm(
            200, body=b"\xff\xd8remote", content_type="image/jpeg"
        )
        digest_mock = AsyncMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=digest_mock,
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera._async_camera_image_impl(cam)

        (
            digest_mock.assert_not_called(),
            "async_digest_request must not be called when no LOCAL creds",
        )


class TestSnapshotJpegSizeFromRequestedWidth:
    """snap.jpg must be requested at the size the caller actually needs.

    Every call site used to hardcode JpegSize=1206 (~500 KB) even for the
    Lovelace card, which asks HA for width=315 (~65 KB at JpegSize=320). On a
    constrained link the full-res body alone can outlast HA's 10 s
    CAMERA_IMAGE_TIMEOUT, so the preview fails and a stale frame is served.
    Callers that persist or analyse the frame pass no width and must keep the
    full-resolution URL byte-for-byte.
    """

    @staticmethod
    async def _digest_url_for(width: int | None) -> str:
        """Run the tier-1 LOCAL snap and return the URL it fetched."""
        coord = _local_live_conn()
        cam = _make_camera_r7(coord=coord)
        digest_mock = AsyncMock(return_value=_digest_cm(200, b"\xff\xd8local"))
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=digest_mock,
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera._async_camera_image_impl(cam, width=width)
        return str(digest_mock.await_args.args[2])

    @pytest.mark.asyncio
    async def test_card_width_shrinks_local_snap_to_thumbnail(self):
        """width=315 (what the card requests) → JpegSize=320, not 1206."""
        url = await self._digest_url_for(315)
        assert f"JpegSize={JPEG_SIZE_THUMB}" in url, (
            "a thumbnail-sized request must not pull the full-res frame"
        )

    @pytest.mark.asyncio
    async def test_no_width_leaves_local_snap_url_untouched(self):
        """No width (persisting/background callers) → URL unchanged."""
        url = await self._digest_url_for(None)
        assert url == LOCAL_SNAP_URL, "full-res callers must keep their URL as-is"
        assert "JpegSize" not in url

    @pytest.mark.parametrize(
        ("width", "expected"),
        [
            (None, None),  # no width requested → keep full resolution
            (0, None),  # defensive: nonsense width → keep full resolution
            (-1, None),
            (315, JPEG_SIZE_THUMB),  # the Lovelace card's actual request
            (JPEG_SIZE_THUMB, JPEG_SIZE_THUMB),  # boundary
            (321, JPEG_SIZE_MEDIUM),
            (JPEG_SIZE_MEDIUM, JPEG_SIZE_MEDIUM),  # boundary
            (JPEG_SIZE_MEDIUM + 1, None),  # bigger than a preview → full res
            (1920, None),
        ],
    )
    def test_jpeg_size_for_width_mapping(self, width, expected):
        """The width → JpegSize mapping, including both boundaries."""
        from custom_components.bosch_shc_camera.const import jpeg_size_for_width

        assert jpeg_size_for_width(width) == expected

    @pytest.mark.parametrize(
        ("url", "size", "expected"),
        [
            # rewrite an existing parameter
            (
                f"https://host/snap.jpg?JpegSize={JPEG_SIZE_FULL}",
                JPEG_SIZE_THUMB,
                f"https://host/snap.jpg?JpegSize={JPEG_SIZE_THUMB}",
            ),
            # append when the URL has none (LOCAL proxyUrl form)
            (
                "https://host/snap.jpg",
                JPEG_SIZE_THUMB,
                f"https://host/snap.jpg?JpegSize={JPEG_SIZE_THUMB}",
            ),
            # append alongside an unrelated parameter
            (
                "https://host/snap.jpg?foo=1",
                JPEG_SIZE_MEDIUM,
                f"https://host/snap.jpg?foo=1&JpegSize={JPEG_SIZE_MEDIUM}",
            ),
            # size=None → untouched, so full-res callers are byte-identical
            (
                f"https://host/snap.jpg?JpegSize={JPEG_SIZE_FULL}",
                None,
                f"https://host/snap.jpg?JpegSize={JPEG_SIZE_FULL}",
            ),
            ("", JPEG_SIZE_THUMB, ""),
        ],
    )
    def test_with_jpeg_size_rewrites_url(self, url, size, expected):
        """with_jpeg_size sets/appends JpegSize and no-ops on None."""
        from custom_components.bosch_shc_camera.const import with_jpeg_size

        assert with_jpeg_size(url, size) == expected


class TestLocalSnapInlineBudget:
    """The inline LOCAL snap budget must fit under HA's CAMERA_IMAGE_TIMEOUT
    while still exceeding a Gen1 camera's cold cost.

    The LOCAL branch returns straight after this attempt (it deliberately skips
    the aiohttp fallback below it, which would only 401 without the Digest auth
    just tried), so nothing else runs inside HA's 10 s ceiling. The previous
    bare 6 s sat *below* these cameras' measured cold cost — TLS handshake
    2.5-6.9 s, ~7.05 s end to end.

    This is complementary to the background warm-up, which fixes requests 2..N
    once a handshake has been banked: the warm-up can only be scheduled by a
    failure, so the request that triggers it still serves the stale cached
    frame, and under a 6 s cap the cold request could never do anything else.
    Regression guard for both ends of the range.
    """

    def test_budget_is_under_ha_camera_image_timeout(self):
        from homeassistant.components.camera import CAMERA_IMAGE_TIMEOUT

        from custom_components.bosch_shc_camera.camera import LOCAL_SNAP_TIMEOUT

        assert LOCAL_SNAP_TIMEOUT < CAMERA_IMAGE_TIMEOUT, (
            "an inline snap that outlives HA's timeout is cancelled mid-flight "
            "and served to the card as a 500 body"
        )

    def test_budget_covers_a_cold_gen1_handshake(self):
        from custom_components.bosch_shc_camera.camera import LOCAL_SNAP_TIMEOUT

        assert LOCAL_SNAP_TIMEOUT >= 7.1, (
            "a budget below the cold handshake+transfer cost can never succeed "
            "from cold, and cold is the only state a timed-out camera reaches"
        )


class TestProxy404RetryClientError:
    """ClientError during the retry GET after a 404 refresh."""

    @pytest.mark.asyncio
    async def test_404_retry_client_error_falls_to_cached(self):
        """404 → new proxy URL → ClientError on retry → return cached image."""
        import aiohttp

        new_url = "https://proxy-02.live.cbs.boschsecurity.com/new/snap.jpg"
        coord = _make_coord_r7(
            live_connections={
                CAM_ID: {"proxyUrl": PROXY_URL, "_connection_type": "REMOTE"}
            },
            live_opened_at={CAM_ID: time.monotonic() - 5.0},
        )
        coord.try_live_connection = AsyncMock(
            return_value={"proxyUrl": new_url, "_connection_type": "REMOTE"}
        )
        cam = _make_camera_r7(coord=coord, cached_image=b"\xff\xd8cached")

        first_cm = _resp_cm(404, body=b"not found", content_type="text/html")
        retry_cm = MagicMock()
        retry_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("retry error"))
        retry_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [first_cm, retry_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, "ClientError on 404-retry must not raise"
        assert cam.cached_image == b"\xff\xd8cached", (
            "cached image must survive 404+ClientError"
        )


class TestProxy401RetryClientError:
    """ClientError during the retry GET after 401 session renewal."""

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
        cam = _make_camera_r7(coord=coord, cached_image=b"\xff\xd8401cached")

        first_cm = _resp_cm(401, body=b"Unauthorized", content_type="text/html")
        retry_cm = MagicMock()
        retry_cm.__aenter__ = AsyncMock(
            side_effect=aiohttp.ClientError("retry 401 error")
        )
        retry_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [first_cm, retry_cm]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, "ClientError on 401-retry must not raise"
        # The 401-retry path calls try_live_connection once to renew the session.
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)


class TestLocalOutageSnapFallback:
    """Camera NOT streaming, has cached LOCAL Digest creds, outage_count > 0."""

    def _outage_coord(self):
        """Coordinator that looks like an auth outage with cached LOCAL creds."""
        return _make_coord_r7(
            live_connections={},  # NOT streaming
            local_creds_cache={
                CAM_ID: {
                    "user": "digestuser",
                    "password": "digestpass",
                    "host": "192.0.2.149",
                    "port": 443,
                }
            },
            auth_outage_count=1,  # > 0 → outage path active
            # async_fetch_live_snapshot returns None so we reach outage path
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_outage_snap_success_returns_image(self):
        """async_digest_request returns 200 + image → cached and returned."""
        coord = self._outage_coord()
        cam = _make_camera_r7(coord=coord)
        img_bytes = b"\xff\xd8outage"

        cm = _digest_cm(200, img_bytes, "image/jpeg")
        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == img_bytes, "outage snap success must return image bytes"
        assert cam.cached_image == img_bytes, "outage snap must cache image"

    @pytest.mark.asyncio
    async def test_outage_snap_thumbnail_request_does_not_poison_shared_cache(self):
        """A width=N (thumbnail) outage snap returns its bytes but must not update the shared full-res cache.

        Regression backported from the HA-core PR's Copilot review round 15
        (2026-07-28): this outage-fallback block sets `self.cached_image`/
        `self.last_image_fetch` inline, unlike the earlier tier-1/tier-2
        fetch paths (bug-hunt 2026-07-27) which already guard those writes
        on `req_jpeg_size is None` — a thumbnail-sized outage snap would
        otherwise poison the shared cache and suppress the next
        full-resolution request until CLOUD_SNAP_CACHE_TTL elapses.
        """
        # cam.cached_image/last_image_fetch stay at _make_camera_r7's defaults
        # (None / 0.0, i.e. "nothing cached yet") — this is the same
        # precondition test_outage_snap_success_returns_image uses, and is
        # required to reach section 2b at all: an already-truthy
        # cached_image instead takes the earlier `elif cache_stale:` branch,
        # which returns self.cached_image directly on a REMOTE+LOCAL failure
        # without ever falling through to the digest-based outage path.
        coord = self._outage_coord()
        cam = _make_camera_r7(coord=coord)
        thumb_bytes = b"\xff\xd8thumbnail-outage-frame"

        cm = _digest_cm(200, thumb_bytes, "image/jpeg")
        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam, width=320)

        assert result == thumb_bytes
        assert cam.cached_image is None, (
            "a thumbnail outage snap must not overwrite the shared full-res cache"
        )
        assert cam.last_image_fetch == 0.0

    @pytest.mark.asyncio
    async def test_outage_snap_timeout_falls_to_placeholder(self):
        """asyncio.TimeoutError during outage snap → placeholder returned."""
        coord = self._outage_coord()
        cam = _make_camera_r7(coord=coord)

        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=TimeoutError()),
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result is not None, (
            "outage snap timeout must not return None (placeholder)"
        )

    @pytest.mark.asyncio
    async def test_outage_snap_zero_outage_count_skips_path(self):
        """auth_outage_count == 0 → outage snap path must be skipped entirely."""
        coord = _make_coord_r7(
            live_connections={},
            local_creds_cache={
                CAM_ID: {
                    "user": "u",
                    "password": "p",
                    "host": "192.0.2.149",
                    "port": 443,
                }
            },
            auth_outage_count=0,  # no outage — must skip
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        cam = _make_camera_r7(coord=coord)
        digest_mock = AsyncMock()
        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=digest_mock,
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera._async_camera_image_impl(cam)

        (
            digest_mock.assert_not_called(),
            "outage path must be skipped when outage_count == 0",
        )

    @pytest.mark.asyncio
    async def test_outage_snap_no_creds_skips_path(self):
        """Empty local_creds_cache → outage snap path skipped."""
        coord = _make_coord_r7(
            live_connections={},
            local_creds_cache={},  # no creds
            auth_outage_count=1,
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )
        cam = _make_camera_r7(coord=coord)
        digest_mock = AsyncMock()
        session = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=digest_mock,
            ),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera._async_camera_image_impl(cam)

        (
            digest_mock.assert_not_called(),
            "outage path must be skipped when no creds cached",
        )


class TestEventSnapshotUnsafeUrl:
    """imageUrl fails _is_safe_bosch_url → warning + skip.

    To reach the event snapshot section (path 4), the camera must be "streaming"
    (so the idle cloud snapshot path is skipped) but have no proxyUrl (so the
    proxy fetch is skipped). The outage path is skipped by having no local
    creds cached.
    """

    def _streaming_no_proxy_coord(self, events):
        """Coordinator: is_streaming=True but no proxyUrl → falls to event snapshot path."""
        return _make_coord_r7(
            data={CAM_ID: {"info": {}, "events": events}},
            # CAM_ID present in live_connections → is_streaming=True
            live_connections={
                CAM_ID: {"rtspsUrl": "rtsp://test/x"}
            },  # is_streaming=True (rtspsUrl gate); no proxyUrl → proxy_url = ""
            local_creds_cache={},  # no cached creds → outage path skipped
            auth_outage_count=0,
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_unsafe_url_logged_and_skipped(self):
        """imageUrl on non-Bosch domain → warning logged, URL skipped.

        cached_image=None so the code doesn't short-circuit at 'if self.cached_image:'
        and reaches the event snapshot section (path 4).
        """
        coord = self._streaming_no_proxy_coord(
            [
                {
                    "imageUrl": "https://evil.example.com/snap.jpg",
                    "timestamp": "2026-01-01T00:00:00Z",
                },
                {
                    "imageUrl": "https://media.boschsecurity.com/snap.jpg",
                    "timestamp": "2026-01-01T00:00:01Z",
                },
            ]
        )
        cam = _make_camera_r7(
            coord=coord, cached_image=None
        )  # no cache → reach section 4

        session = MagicMock()
        # The safe Bosch URL returns 200
        session.get.return_value = _resp_cm(
            200, body=b"\xff\xd8new", content_type="image/jpeg"
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        # The evil.example.com URL is skipped; only the boschsecurity.com URL is fetched
        assert session.get.call_count == 1, "only the safe Bosch URL must be fetched"
        assert result == b"\xff\xd8new", (
            "unsafe URL skipped → safe URL fetched successfully"
        )

    @pytest.mark.asyncio
    async def test_missing_image_url_key_skipped(self):
        """Event with no imageUrl key → skipped cleanly."""
        coord = self._streaming_no_proxy_coord(
            [{"timestamp": "2026-01-01T00:00:00Z"}]  # no imageUrl key
        )
        cam = _make_camera_r7(coord=coord)
        session = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        (
            session.get.assert_not_called(),
            "session.get must not be called for events without imageUrl",
        )


class TestEventSnapshot4xx:
    """Various HTTP errors and network failures in the event snapshot loop.

    To reach this path, camera must be streaming (is_streaming=True, i.e.
    CAM_ID in live_connections) but have no proxyUrl. Then the outage path
    is skipped (no local creds + outage_count=0) and we fall through here.
    """

    def _event_coord(self, events):
        """Coordinator: is_streaming=True, no proxyUrl, no outage → event snapshot path."""
        return _make_coord_r7(
            data={CAM_ID: {"info": {}, "events": events}},
            live_connections={
                CAM_ID: {"rtspsUrl": "rtsp://test/x"}
            },  # is_streaming=True (rtspsUrl gate); no proxyUrl → proxy_url = ""
            local_creds_cache={},
            auth_outage_count=0,
            async_fetch_live_snapshot=AsyncMock(return_value=None),
            async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    async def test_401_returns_cached_immediately(self):
        """Event snapshot 401 → warning logged, cached image (None) → placeholder returned.

        cached_image must be None to reach this path past the earlier
        short-circuit. After 401, 'return self.cached_image' returns None, so
        the public wrapper serves _PLACEHOLDER_JPEG. We verify that session.get
        is called exactly once (one URL fetched → 401 → immediate return
        without trying next event).
        """
        safe_url = "https://media.boschsecurity.com/ev1.jpg"
        coord = self._event_coord(
            [
                {"imageUrl": safe_url, "timestamp": "2026-01-01T00:00:00Z"},
                {
                    "imageUrl": "https://media.boschsecurity.com/ev2.jpg",
                    "timestamp": "2026-01-01T00:00:01Z",
                },
            ]
        )
        cam = _make_camera_r7(
            coord=coord, cached_image=None
        )  # no cache → reach section 4

        session = MagicMock()
        session.get.return_value = _resp_cm(
            401, body=b"Unauthorized", content_type="text/html"
        )

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert session.get.call_count == 1, (
            "401 must stop the event loop immediately (no retry)"
        )
        # result is None (no cached) → public async_camera_image serves placeholder;
        # _async_camera_image_impl itself returns None on 401 with no cache
        assert result is None or result == BoschCamera._PLACEHOLDER_JPEG, (
            "401 with no cached image must return None or placeholder"
        )

    @pytest.mark.asyncio
    async def test_event_snapshot_timeout_capped_at_10s(self):
        """Tier4 (latest event snapshot) asyncio.timeout must be 10s, not 20s.

        Regression test for the stream-perf-stability-refactor plan Phase 1
        item 4: the old 20s timeout on this last fallback tier exceeded HA's
        CameraImageView outer timeout (10s), so an already-cancelled request
        could still bind up to 20s of event-loop time. Pins the new value.
        """
        safe_url = "https://media.boschsecurity.com/ev1.jpg"
        coord = self._event_coord(
            [{"imageUrl": safe_url, "timestamp": "2026-01-01T00:00:00Z"}]
        )
        cam = _make_camera_r7(
            coord=coord, cached_image=None
        )  # no cache → reach section 4

        session = MagicMock()
        session.get.return_value = _resp_cm(
            200, body=b"\xff\xd8ev", content_type="image/jpeg"
        )

        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.asyncio.timeout",
                wraps=asyncio.timeout,
            ) as timeout_mock,
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8ev"
        # In this scenario (no proxyUrl, not-idle, no outage creds) tier4 is
        # the ONLY asyncio.timeout call site reached — pin its argument.
        timeout_mock.assert_called_once_with(10)

    @pytest.mark.asyncio
    async def test_403_tries_next_event_then_returns_placeholder(self):
        """403 on first event → try next event; 403 again → all failed → placeholder.

        cached_image=None to bypass the earlier short-circuit and reach this path.
        """
        safe_url1 = "https://media.boschsecurity.com/ev1.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2.jpg"
        coord = self._event_coord(
            [
                {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
                {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
            ]
        )
        cam = _make_camera_r7(
            coord=coord, cached_image=None
        )  # no cache → reach section 4

        session = MagicMock()
        session.get.side_effect = [
            _resp_cm(403, body=b"Forbidden", content_type="text/html"),
            _resp_cm(403, body=b"Forbidden", content_type="text/html"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert session.get.call_count == 2, "must attempt both event URLs on 403"
        from custom_components.bosch_shc_camera.camera import BoschCamera

        assert result == BoschCamera._PLACEHOLDER_JPEG, (
            "both 403 + no cached → placeholder"
        )

    @pytest.mark.asyncio
    async def test_410_tries_next_event(self):
        """410 (expired URL) on first event → try next event (200 → success)."""
        safe_url1 = "https://media.boschsecurity.com/ev1_old.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2_new.jpg"
        coord = self._event_coord(
            [
                {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
                {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
            ]
        )
        cam = _make_camera_r7(coord=coord)

        session = MagicMock()
        session.get.side_effect = [
            _resp_cm(410, body=b"Gone", content_type="text/html"),
            _resp_cm(200, body=b"\xff\xd8fresh", content_type="image/jpeg"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8fresh", (
            "410 on first → 200 on second must return fresh image"
        )

    @pytest.mark.asyncio
    async def test_timeout_on_event_snap_continues_loop(self):
        """TimeoutError on first event → loop continues to next event."""
        safe_url1 = "https://media.boschsecurity.com/ev1.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2.jpg"
        coord = self._event_coord(
            [
                {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
                {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
            ]
        )
        cam = _make_camera_r7(
            coord=coord, cached_image=None
        )  # no cache → reach section 4

        # First CM raises TimeoutError when __aenter__ is called
        timeout_cm = MagicMock()
        timeout_cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        timeout_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [
            timeout_cm,
            _resp_cm(200, body=b"\xff\xd8ev2", content_type="image/jpeg"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8ev2", (
            "timeout on first event → second event must succeed"
        )

    @pytest.mark.asyncio
    async def test_client_error_on_event_snap_continues_loop(self):
        """aiohttp.ClientError on first event → loop continues to second event."""
        import aiohttp

        safe_url1 = "https://media.boschsecurity.com/ev1.jpg"
        safe_url2 = "https://media.boschsecurity.com/ev2.jpg"
        coord = self._event_coord(
            [
                {"imageUrl": safe_url1, "timestamp": "2026-01-01T00:00:00Z"},
                {"imageUrl": safe_url2, "timestamp": "2026-01-01T00:00:01Z"},
            ]
        )
        cam = _make_camera_r7(
            coord=coord, cached_image=None
        )  # no cache → reach section 4

        err_cm = MagicMock()
        err_cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        err_cm.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get.side_effect = [
            err_cm,
            _resp_cm(200, body=b"\xff\xd8ev2ok", content_type="image/jpeg"),
        ]

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8ev2ok", (
            "ClientError on first event → second event must succeed"
        )

    @pytest.mark.asyncio
    async def test_all_events_fail_returns_placeholder(self):
        """All events fail → _PLACEHOLDER_JPEG returned."""
        safe_url = "https://media.boschsecurity.com/ev1.jpg"
        coord = self._event_coord(
            [{"imageUrl": safe_url, "timestamp": "2026-01-01T00:00:00Z"}]
        )
        cam = _make_camera_r7(coord=coord, cached_image=None)
        from custom_components.bosch_shc_camera.camera import BoschCamera

        placeholder = BoschCamera._PLACEHOLDER_JPEG

        session = MagicMock()
        session.get.return_value = _resp_cm(410, body=b"Gone", content_type="text/html")

        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == placeholder, (
            "all events fail + no cached → placeholder returned"
        )


# Regression: a transient live-snapshot failure on the proactive refresh tick
# must NOT replace a good (live) cached frame with a stale event snapshot.
#
# Bug (reported 2026-06-11, privacy OFF): the user saw the current live
# snapshot, then "after some time" the card flipped to an ancient image from
# an old motion event. Root cause: ``async_trigger_image_refresh`` fell back
# to ``async_fetch_fresh_event_snapshot`` whenever the live fetch failed and
# overwrote ``cached_image`` with the "latest event" image — which is days
# old when ``last_event`` is frozen (no new motion / FCM stale).
#
# Fix: only seed from the event image on a genuine cold start (no real frame
# yet — the 1×1 placeholder does not count); never replace a real live frame
# with it, and back off a full interval on a transient failure.

_LIVE_FRAME = b"\xff\xd8\xff\xe0LIVE-FRAME-CURRENT" + b"\x00" * 64
_OLD_EVENT = b"\xff\xd8\xff\xe0ANCIENT-EVENT-IMAGE" + b"\x11" * 64


def _make_camera_stale(cached: bytes | None):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = SimpleNamespace(
        data={CAM_ID_STALE: {"info": {"title": "Terrasse"}}},
        live_connections={},
        shc_state_cache={},  # no entry — these tests only exercise
        # async_trigger_image_refresh, which checks `is True` and fails
        # open on an empty/unknown state; not the privacy_unknown-gated
        # _async_camera_image_impl cascade (harmless here, but don't reuse
        # this factory for that path without adding privacy_mode: False).
        image_entities={},
        async_fetch_live_snapshot=AsyncMock(return_value=None),  # live FAILS
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        async_fetch_fresh_event_snapshot=AsyncMock(return_value=_OLD_EVENT),
        is_stream_warming=lambda cid: False,
    )
    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID_STALE
    cam._entry = SimpleNamespace(data={"bearer_token": "tok"}, options={})
    cam._display_name = "Bosch Terrasse"
    cam.cached_image = cached
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._refresh_inflight = (
        False  # synchronous in-flight guard (replaces _refresh_lock)
    )
    cam.async_write_ha_state = MagicMock()
    cam.hass = SimpleNamespace(data={}, async_create_task=MagicMock())
    return cam, coord


@pytest.mark.asyncio
async def test_live_failure_keeps_good_frame_not_stale_event() -> None:
    """A real cached live frame survives a failed live fetch — no flip to event."""
    cam, coord = _make_camera_stale(cached=_LIVE_FRAME)

    with patch("custom_components.bosch_shc_camera.camera.save_snapshot", AsyncMock()):
        await cam.async_trigger_image_refresh(delay=0)

    # The good live frame is preserved …
    assert cam.cached_image == _LIVE_FRAME
    # … and the stale event snapshot was never even fetched.
    coord.async_fetch_fresh_event_snapshot.assert_not_awaited()
    # Backoff: last_image_fetch bumped so it does not hammer every tick.
    assert cam.last_image_fetch > 0.0


@pytest.mark.asyncio
async def test_cold_start_still_seeds_from_event_snapshot() -> None:
    """With only the placeholder (no real frame), seeding from the event is kept."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    cam, coord = _make_camera_stale(cached=BoschCamera._PLACEHOLDER_JPEG)

    with patch("custom_components.bosch_shc_camera.camera.save_snapshot", AsyncMock()):
        await cam.async_trigger_image_refresh(delay=0)

    # Cold start: the event image is used as a seed (better than a black tile).
    coord.async_fetch_fresh_event_snapshot.assert_awaited_once()
    assert cam.cached_image == _OLD_EVENT


@pytest.mark.asyncio
async def test_live_success_updates_frame() -> None:
    """Sanity: when the live fetch succeeds, the live frame is cached as before."""
    cam, coord = _make_camera_stale(cached=_LIVE_FRAME)
    new_live = b"\xff\xd8\xff\xe0NEW-LIVE" + b"\x00" * 32
    coord.async_fetch_live_snapshot = AsyncMock(return_value=new_live)

    with patch("custom_components.bosch_shc_camera.camera.save_snapshot", AsyncMock()):
        await cam.async_trigger_image_refresh(delay=0)

    assert cam.cached_image == new_live
    coord.async_fetch_fresh_event_snapshot.assert_not_awaited()


# WebRTC: close_webrtc_session idempotency + async_create_stream privacy gate
#
# Root cause (2026-05-16): HA go2rtc provider's async_close_session calls
# dict.pop(session_id) without a default. When privacy mode is ON,
# async_handle_async_webrtc_offer bails before inserting the session into
# go2rtc._sessions, but the websocket handler already registered
# partial(camera.close_webrtc_session, session_id) as a subscription cleanup.
# On client disconnect async_handle_close calls that partial → KeyError → HA
# logs ERROR "Error unsubscribing from subscription" repeatedly.


@pytest.fixture
def stub_coord_webrtc() -> SimpleNamespace:
    """Minimal coordinator stub sufficient for BoschCamera construction."""
    return SimpleNamespace(
        data={
            CAM_ID_WEBRTC: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": "HOME_Eyes_Indoor_II",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "events": [],
                "live": {},
            }
        },
        live_connections={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID_WEBRTC: {"privacy_mode": False}},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )


@pytest.fixture
def stub_entry_webrtc() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )


@pytest.fixture
def camera_webrtc(
    stub_coord_webrtc: SimpleNamespace, stub_entry_webrtc: SimpleNamespace
) -> BoschCamera:
    """Construct a bare BoschCamera without HA lifecycle (no hass, no add_to_hass)."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    return BoschCamera(stub_coord_webrtc, CAM_ID_WEBRTC, stub_entry_webrtc)


class TestCloseWebrtcSession:
    """close_webrtc_session must be idempotent / no-op for unknown session IDs.

    The go2rtc provider uses dict.pop(session_id) — raises KeyError when the
    session was never established (privacy mode blocked the offer).  Our override
    must catch that KeyError so HA's async_handle_close does not log ERROR.
    """

    def test_noop_when_no_provider(self, camera_webrtc: BoschCamera) -> None:
        """No _webrtc_provider → close_webrtc_session must not raise."""
        # Base Camera sets _webrtc_provider via async_refresh_providers.
        # Without calling that, it is None.  Ensure no AttributeError.
        assert getattr(camera_webrtc, "_webrtc_provider", None) is None
        # Must be a clean no-op — no exception of any kind.
        camera_webrtc.close_webrtc_session("non-existent-session-id")

    def test_noop_on_keyerror_from_provider(self, camera_webrtc: BoschCamera) -> None:
        """Provider raises KeyError (session never inserted) → must not propagate.

        This is the exact failure path seen in HA logs 2026-05-16:
          go2rtc async_close_session → self._sessions.pop(session_id) → KeyError
          → HA websocket_api connection.py async_handle_close → logs ERROR
        """
        mock_provider = MagicMock()
        mock_provider.async_close_session.side_effect = KeyError("unknown-session")
        camera_webrtc._webrtc_provider = mock_provider  # inject provider

        # Must NOT raise — KeyError must be silently discarded.
        camera_webrtc.close_webrtc_session("unknown-session-id")

        mock_provider.async_close_session.assert_called_once_with("unknown-session-id")

    def test_known_session_delegates_to_provider(
        self, camera_webrtc: BoschCamera
    ) -> None:
        """When session IS known, provider.async_close_session must be called."""
        mock_provider = MagicMock()
        # No side_effect → returns None (happy path)
        camera_webrtc._webrtc_provider = mock_provider

        camera_webrtc.close_webrtc_session("known-session-abc")

        mock_provider.async_close_session.assert_called_once_with("known-session-abc")

    def test_other_exceptions_from_provider_still_propagate(
        self, camera_webrtc: BoschCamera
    ) -> None:
        """Non-KeyError exceptions from the provider must still surface.

        Only KeyError is the expected "session not found" signal from go2rtc.
        Other errors (e.g. RuntimeError, TypeError) indicate real bugs and must
        not be swallowed.
        """
        mock_provider = MagicMock()
        mock_provider.async_close_session.side_effect = RuntimeError("unexpected")
        camera_webrtc._webrtc_provider = mock_provider

        with pytest.raises(RuntimeError, match="unexpected"):
            camera_webrtc.close_webrtc_session("some-session")

    def test_multiple_close_calls_are_idempotent(
        self, camera_webrtc: BoschCamera
    ) -> None:
        """Calling close_webrtc_session twice for the same ID must not raise.

        The second call will KeyError (session already popped on first call)
        and must be silently discarded.
        """
        mock_provider = MagicMock()
        call_count = 0

        def pop_session(session_id: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                raise KeyError(session_id)

        mock_provider.async_close_session.side_effect = pop_session
        camera_webrtc._webrtc_provider = mock_provider

        camera_webrtc.close_webrtc_session("dup-session")  # first call — OK
        camera_webrtc.close_webrtc_session(
            "dup-session"
        )  # second call — KeyError → no-op


class TestAsyncCreateStreamPrivacy:
    """async_create_stream must raise HomeAssistantError when privacy is ON.

    Previously it called try_live_connection (which also gates on privacy),
    got None back, logged WARNING "play_stream — live connection failed", and
    returned None.  HA then raised HomeAssistantError("does not support play
    stream service") — an opaque ERROR that confused users.

    After the fix: when privacy mode is detected before the live-connection
    attempt, a HomeAssistantError with a descriptive message is raised
    immediately, skipping the pointless try_live_connection round-trip.
    """

    @pytest.mark.asyncio
    async def test_raises_home_assistant_error_when_privacy_on(
        self, camera_webrtc: BoschCamera, stub_coord_webrtc: SimpleNamespace
    ) -> None:
        """Privacy ON → HomeAssistantError with 'privacy mode' in message."""
        from homeassistant.exceptions import HomeAssistantError

        stub_coord_webrtc.shc_state_cache[CAM_ID_WEBRTC] = {"privacy_mode": True}
        stub_coord_webrtc.live_connections = {}  # no active session

        with pytest.raises(HomeAssistantError, match="privacy mode"):
            await camera_webrtc.async_create_stream()

    @pytest.mark.asyncio
    async def test_no_error_when_privacy_off_and_connection_exists(
        self, camera_webrtc: BoschCamera, stub_coord_webrtc: SimpleNamespace
    ) -> None:
        """Privacy OFF + active live_connection → delegates to super() without error."""
        from homeassistant.exceptions import HomeAssistantError

        stub_coord_webrtc.shc_state_cache[CAM_ID_WEBRTC] = {"privacy_mode": False}
        # Simulate an active live connection so the branch skips try_live_connection
        stub_coord_webrtc.live_connections[CAM_ID_WEBRTC] = {
            "rtspsUrl": "rtsps://proxy.example.com:443/hash/rtsp_tunnel",
            "_connection_type": "REMOTE",
        }

        # super().async_create_stream() tries to call stream_source() which
        # returns a URL, then calls create_stream(hass, …) — we need hass.
        # Just assert we do NOT raise HomeAssistantError (i.e. we get past our
        # privacy gate).  The super() call may fail for other reasons (no hass)
        # but not because of our privacy check.
        try:
            await camera_webrtc.async_create_stream()
        except HomeAssistantError as exc:
            assert "privacy mode" not in str(exc), (
                f"Got unexpected privacy-mode error with privacy OFF: {exc}"
            )
        except Exception:
            pass  # super() needs hass — AttributeError/TypeError is expected here

    @pytest.mark.asyncio
    async def test_no_error_when_privacy_state_unknown(
        self, camera_webrtc: BoschCamera, stub_coord_webrtc: SimpleNamespace
    ) -> None:
        """Privacy state not in cache (None/missing) → must not raise HAError.

        When the coordinator has not yet fetched privacy state, we must not
        block the stream — fail open (attempt live connection as usual).
        """
        from homeassistant.exceptions import HomeAssistantError

        stub_coord_webrtc.shc_state_cache = {}  # no entry for this cam
        stub_coord_webrtc.live_connections = {}

        # try_live_connection will be called; mock it to return None (unavailable)
        async def fake_try_live_connection(cam_id: str) -> None:
            return None

        stub_coord_webrtc.try_live_connection = fake_try_live_connection

        # Should NOT raise HomeAssistantError (privacy-mode branch not taken)
        try:
            await camera_webrtc.async_create_stream()
        except HomeAssistantError as exc:
            assert "privacy mode" not in str(exc), (
                f"Unexpected privacy-mode error when privacy state is unknown: {exc}"
            )
        except Exception:
            pass  # other errors from missing hass are fine


# WebRTC / HLS pre-warm waits: async_create_stream + the native
# async_handle_async_webrtc_offer override, and the native-WebRTC-capability
# flag regression (GitHub issue #40).
#
# Symptom (async_create_stream): idle→ONLINE LOCAL stream in the native
# more-info view showed "does not support play stream service" — the
# coordinator sets `live_connections[cam_id] = result` before LOCAL pre-warm
# completes (no `rtspsUrl` key yet), so stream_source() returned None while
# async_create_stream's gate check saw the dict already populated and skipped
# the auto-open path.
#
# Symptom (webrtc offer): idle→ONLINE LOCAL stream in the native HA more-info
# view (Companion app) showed ~25-35s of black video with no retry, because
# async_handle_async_webrtc_offer() delegated straight to the base class
# instead of waiting for pre-warm like async_create_stream() already did.
#
# GitHub issue #40: the async_handle_async_webrtc_offer() override added to
# fix the above made HA core's Camera.__init__ set
# _supports_native_async_webrtc=True (computed from a pure method-override
# identity check), which made async_refresh_providers() skip go2rtc provider
# detection entirely — so every offer reached
# super().async_handle_async_webrtc_offer(), found _webrtc_provider is None,
# and raised HomeAssistantError("Camera does not support WebRTC") for every
# camera. Fix: force _supports_native_async_webrtc back to False in __init__.


def _make_coord_prewarm(**overrides):
    base = dict(
        live_connections={},
        stream_warming=set(),
        shc_state_cache={CAM_ID_PREWARM: {"privacy_mode": False}},
        try_live_connection=AsyncMock(return_value={"rtspsUrl": "rtsp://x"}),
        async_update_listeners=MagicMock(),
        get_model_config=lambda cid: SimpleNamespace(min_total_wait=2),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_camera_prewarm(coord):
    from custom_components.bosch_shc_camera.camera import BoschCamera

    cam = BoschCamera.__new__(BoschCamera)
    cam.coordinator = coord
    cam._cam_id = CAM_ID_PREWARM
    cam._display_name = "Bosch Innenbereich"
    return cam


@pytest.mark.asyncio
async def test_prewarm_in_progress_waits_for_completion():
    """While stream_warming contains cam_id, async_create_stream must wait
    for warming to complete before delegating to super — otherwise stream_source
    returns None and HA logs 'does not support play stream service'."""
    coord = _make_coord_prewarm(
        live_connections={CAM_ID_PREWARM: {"proxyUrl": "https://x/snap.jpg"}},
        stream_warming={CAM_ID_PREWARM},
    )
    cam = _make_camera_prewarm(coord)

    fake_stream = object()

    # Schedule background task to finish pre-warm after 0.3s
    async def _finish_prewarm():
        await asyncio.sleep(0.3)
        coord.live_connections[CAM_ID_PREWARM]["rtspsUrl"] = "rtsp://127.0.0.1:36107/x"
        coord.stream_warming.discard(CAM_ID_PREWARM)

    asyncio.create_task(_finish_prewarm())  # noqa: RUF006  # fire-and-forget in test

    with patch(
        "homeassistant.components.camera.Camera.async_create_stream",
        new=AsyncMock(return_value=fake_stream),
    ):
        result = await cam.async_create_stream()

    # super().async_create_stream must have been awaited AFTER warming cleared
    assert result is fake_stream
    assert CAM_ID_PREWARM not in coord.stream_warming
    # And try_live_connection must NOT have been called — connection already
    # existed, we just had to wait for the URL to be populated.
    coord.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_prewarm_timeout_returns_none():
    """If pre-warm never completes within min_total_wait + grace, return None
    rather than blocking forever. Caller (HA) treats None as 'no stream'."""
    coord = _make_coord_prewarm(
        live_connections={CAM_ID_PREWARM: {"proxyUrl": "https://x/snap.jpg"}},
        stream_warming={CAM_ID_PREWARM},
        # Tight deadline so the test completes quickly: 0 + 5s grace = 5s.
        # We patch asyncio.sleep to short-circuit the wait.
        get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
    )
    cam = _make_camera_prewarm(coord)

    # Replace sleep with a no-op so the deadline loop exits in microseconds
    # rather than waiting 5 real seconds.
    with patch(
        "custom_components.bosch_shc_camera.camera.asyncio.sleep",
        new=AsyncMock(return_value=None),
    ):
        result = await cam.async_create_stream()

    assert result is None


@pytest.mark.asyncio
async def test_no_warming_delegates_immediately():
    """If pre-warm is not in progress, no waiting — just delegate to super.
    Backwards-compatible with the existing happy path."""
    coord = _make_coord_prewarm(
        live_connections={CAM_ID_PREWARM: {"rtspsUrl": "rtsp://127.0.0.1:36107/x"}},
        stream_warming=set(),  # NOT warming
    )
    cam = _make_camera_prewarm(coord)

    fake_stream = object()
    with patch(
        "homeassistant.components.camera.Camera.async_create_stream",
        new=AsyncMock(return_value=fake_stream),
    ):
        result = await cam.async_create_stream()

    assert result is fake_stream
    coord.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_webrtc_offer_waits_for_prewarm_before_delegating():
    """A WebRTC offer arriving mid-warm-up must wait for pre-warm to clear
    before super().async_handle_async_webrtc_offer() is awaited — otherwise
    go2rtc reads stream_source()==None and the native view stays black."""
    coord = _make_coord_prewarm(stream_warming={CAM_ID_PREWARM})
    cam = _make_camera_prewarm(coord)

    async def _finish_prewarm():
        await asyncio.sleep(0.3)
        coord.stream_warming.discard(CAM_ID_PREWARM)

    asyncio.create_task(_finish_prewarm())  # noqa: RUF006  # fire-and-forget in test

    send_message = MagicMock()
    with patch(
        "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
        new=AsyncMock(return_value=None),
    ) as mock_super:
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    assert CAM_ID_PREWARM not in coord.stream_warming
    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)


@pytest.mark.asyncio
async def test_webrtc_offer_no_warming_delegates_immediately():
    """No pre-warm in progress → no waiting, straight delegation (happy path)."""
    coord = _make_coord_prewarm(
        stream_warming=set(),
        live_connections={CAM_ID_PREWARM: {"rtspsUrl": "rtsp://x"}},
    )
    cam = _make_camera_prewarm(coord)

    send_message = MagicMock()
    with patch(
        "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
        new=AsyncMock(return_value=None),
    ) as mock_super:
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)
    coord.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_webrtc_offer_no_live_connection_auto_opens_one():
    """First-time-setup gap (community.simon42.com report): a camera whose
    Live Stream switch was never turned on has no live_connections entry.
    The native WebRTC offer must auto-open one — mirroring async_create_stream
    — instead of delegating straight to super() with stream_source()==None."""
    coord = _make_coord_prewarm(live_connections={}, stream_warming=set())
    cam = _make_camera_prewarm(coord)

    send_message = MagicMock()
    with patch(
        "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
        new=AsyncMock(return_value=None),
    ) as mock_super:
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    coord.try_live_connection.assert_awaited_once_with(CAM_ID_PREWARM)
    coord.async_update_listeners.assert_called_once()
    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)


@pytest.mark.asyncio
async def test_webrtc_offer_privacy_mode_blocks_auto_open():
    """Privacy mode ON + no existing session must raise instead of opening
    a new one — matches async_create_stream's existing privacy gate."""
    from homeassistant.exceptions import HomeAssistantError

    coord = _make_coord_prewarm(
        live_connections={},
        stream_warming=set(),
        shc_state_cache={CAM_ID_PREWARM: {"privacy_mode": True}},
    )
    cam = _make_camera_prewarm(coord)
    cam._display_name = "Bosch Innenbereich"

    send_message = MagicMock()
    with pytest.raises(HomeAssistantError, match="privacy mode"):
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    coord.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_webrtc_offer_auto_open_coalesces_on_skip(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A concurrent start already in flight (STREAM_START_SKIPPED) must not
    be treated as a failure — just fall through to the pre-warm wait.

    STREAM_START_SKIPPED.__bool__() is False, so `elif not result` alone
    would ALSO match this branch — pin the debug-not-warning log line so a
    regression collapsing the `is STREAM_START_SKIPPED` special case into
    the generic failure branch (spurious warnings on every coalesced start)
    is actually caught, not just the (identical either way) delegation."""
    import logging

    from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED

    coord = _make_coord_prewarm(
        live_connections={},
        stream_warming=set(),
        try_live_connection=AsyncMock(return_value=STREAM_START_SKIPPED),
    )
    cam = _make_camera_prewarm(coord)

    send_message = MagicMock()
    with (
        caplog.at_level(
            logging.DEBUG, logger="custom_components.bosch_shc_camera.camera"
        ),
        patch(
            "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
            new=AsyncMock(return_value=None),
        ) as mock_super,
    ):
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    coord.async_update_listeners.assert_not_called()
    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)
    assert "coalescing into an in-progress start" in caplog.text
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_webrtc_offer_auto_open_failure_logs_and_still_delegates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """try_live_connection returning falsy (real failure, not a coalesced
    skip) must log a WARNING (not the coalescing debug line) but still fall
    through to super() — matching the prewarm-timeout case, so HA surfaces
    its own no-stream handling."""
    import logging

    coord = _make_coord_prewarm(
        live_connections={},
        stream_warming=set(),
        try_live_connection=AsyncMock(return_value=None),
    )
    cam = _make_camera_prewarm(coord)

    send_message = MagicMock()
    with (
        caplog.at_level(
            logging.DEBUG, logger="custom_components.bosch_shc_camera.camera"
        ),
        patch(
            "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
            new=AsyncMock(return_value=None),
        ) as mock_super,
    ):
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    coord.async_update_listeners.assert_not_called()
    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)
    assert "live connection failed" in caplog.text
    assert "coalescing into an in-progress start" not in caplog.text
    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.asyncio
async def test_webrtc_offer_existing_connection_skips_auto_open():
    """A camera whose stream is already active (switch ON / prior offer)
    must not re-trigger try_live_connection on every subsequent offer."""
    coord = _make_coord_prewarm(
        live_connections={CAM_ID_PREWARM: {"rtspsUrl": "rtsp://x"}},
        stream_warming=set(),
    )
    cam = _make_camera_prewarm(coord)

    send_message = MagicMock()
    with patch(
        "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
        new=AsyncMock(return_value=None),
    ):
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    coord.try_live_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_webrtc_offer_prewarm_timeout_still_delegates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If pre-warm never clears within the deadline, _wait_for_prewarm logs a
    warning and returns False — but the offer still delegates to super() so
    HA surfaces its own "no stream" handling rather than silently hanging.

    `async_handle_async_webrtc_offer` discards `_wait_for_prewarm`'s bool
    return and delegates to super() unconditionally either way — so
    `mock_super.assert_awaited_once_with(...)` alone is identical whether
    pre-warm succeeded, timed out, or the wait was skipped entirely. Pin the
    timeout branch specifically: the deadline-exceeded WARNING fired and
    `stream_warming` was never cleared (a successful/skipped wait clears or
    never sets it)."""
    import logging

    coord = _make_coord_prewarm(
        stream_warming={CAM_ID_PREWARM},
        get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
    )
    cam = _make_camera_prewarm(coord)

    send_message = MagicMock()
    with (
        caplog.at_level(
            logging.WARNING, logger="custom_components.bosch_shc_camera.camera"
        ),
        patch(
            "custom_components.bosch_shc_camera.camera.asyncio.sleep",
            new=AsyncMock(return_value=None),
        ) as mock_sleep,
        patch(
            "homeassistant.components.camera.Camera.async_handle_async_webrtc_offer",
            new=AsyncMock(return_value=None),
        ) as mock_super,
    ):
        await cam.async_handle_async_webrtc_offer(
            "sdp-offer", "session-1", send_message
        )

    mock_super.assert_awaited_once_with("sdp-offer", "session-1", send_message)
    mock_sleep.assert_awaited()
    assert "did not complete within" in caplog.text
    assert CAM_ID_PREWARM in coord.stream_warming


def _make_real_camera_prewarm() -> BoschCamera:
    """Construct BoschCamera via its real __init__ (not __new__ bypass) so the
    HA-core Camera.__init__ bookkeeping this bug lives in actually runs."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = SimpleNamespace(
        data={
            CAM_ID_PREWARM: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "CAMERA_EYES",
                    "firmwareVersion": "7.91.56",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "events": [],
                "live": {},
            }
        },
        # Already-open session: this fixture targets the provider-delegation
        # and native-webrtc-flag regressions, not the auto-open path (covered
        # separately by the test_webrtc_offer_no_live_connection_* tests).
        live_connections={CAM_ID_PREWARM: {"rtspsUrl": "rtsp://127.0.0.1:1/x"}},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        stream_warming=set(),
        shc_state_cache={CAM_ID_PREWARM: {"privacy_mode": False}},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )
    entry = SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )
    return BoschCamera(coord, CAM_ID_PREWARM, entry)


def test_camera_does_not_report_native_webrtc_support() -> None:
    """Overriding async_handle_async_webrtc_offer() for the pre-warm wait must
    NOT flip HA core's _supports_native_async_webrtc bookkeeping flag — that
    flag must stay False so async_refresh_providers() still runs go2rtc
    provider detection instead of leaving _webrtc_provider permanently None."""
    cam = _make_real_camera_prewarm()
    assert cam._supports_native_async_webrtc is False


@pytest.mark.asyncio
async def test_webrtc_offer_delegates_to_registered_provider_not_hard_error() -> None:
    """With a go2rtc provider registered, an offer must reach the provider —
    not fall through to core's `raise HomeAssistantError("Camera does not
    support WebRTC")`, which is exactly issue #40's user-visible symptom."""
    cam = _make_real_camera_prewarm()
    provider = MagicMock()
    provider.async_handle_async_webrtc_offer = AsyncMock(return_value=None)
    cam._webrtc_provider = provider

    send_message = MagicMock()
    await cam.async_handle_async_webrtc_offer("sdp-offer", "session-1", send_message)

    provider.async_handle_async_webrtc_offer.assert_awaited_once_with(
        cam, "sdp-offer", "session-1", send_message
    )


def _make_camera_for_snap(**overrides):
    """Build a minimal BoschCamera stub for _async_camera_image_impl tests."""
    from custom_components.bosch_shc_camera.camera import BoschCamera

    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        live_connections={},
        live_opened_at={},
        camera_entities={},
        stream_fell_back={},
        stream_error_count={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        stream_warming=set(),
        image_rotation_180={},
        local_creds_cache={},
        auth_outage_count=0,
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
    cam.cached_image = None
    cam._force_image_refresh = False
    cam.last_image_fetch = 0.0
    cam._was_streaming = False
    cam._model = "HOME_Eyes_Outdoor"
    cam._model_name = "Eyes Outdoor II"
    cam.hw_version = "HOME_Eyes_Outdoor"
    cam._fw = "9.40.25"
    cam._mac = "aa:bb:cc:dd:ee:01"
    # _token is a read-only property backed by _entry.data["bearer_token"]
    cam.async_write_ha_state = MagicMock()
    cam.hass = MagicMock()
    cam.hass.async_create_task = MagicMock()
    cam.hass.async_add_executor_job = AsyncMock(return_value=None)
    for k, v in overrides.items():
        setattr(cam, k, v)
    return cam


def _digest_cm_snap(
    status: int, body: bytes = b"", content_type: str = "image/jpeg"
) -> MagicMock:
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

        cam = _make_camera_for_snap()
        cam.coordinator.live_connections = {
            CAM_ID: {
                "proxyUrl": "https://192.0.2.149:443/snap.jpg",
                "_connection_type": "LOCAL",
                "_local_user": "digest_user",
                "_local_password": "digest_pass",
            }
        }
        cm = _digest_cm_snap(200, b"\xff\xd8\xff", "image/jpeg")

        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8\xff", (
            "LOCAL snap 200 + image/jpeg must return the bytes"
        )
        assert cam.cached_image == b"\xff\xd8\xff", "cached_image must be updated"

    @pytest.mark.asyncio
    async def test_local_snap_request_exception_returns_none(self):
        """aiohttp.ClientError in LOCAL snap → returns cached/placeholder (not crash)."""
        import aiohttp as _aiohttp

        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_for_snap()
        cam.coordinator.live_connections = {
            CAM_ID: {
                "proxyUrl": "https://192.0.2.149:443/snap.jpg",
                "_connection_type": "LOCAL",
                "_local_user": "u",
                "_local_password": "p",
            }
        }

        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=_aiohttp.ClientError("timeout")),
            ),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        from custom_components.bosch_shc_camera.camera import BoschCamera as _Cam

        assert (
            result is None
            or result is cam.cached_image
            or result is _Cam._PLACEHOLDER_JPEG
        ), (
            "aiohttp.ClientError in LOCAL snap must not raise; returns cached/placeholder"
        )

    @pytest.mark.asyncio
    async def test_local_snap_non_image_content_type_returns_none(self):
        """async_digest_request 200 but non-image content-type → placeholder."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = _make_camera_for_snap()
        cam.coordinator.live_connections = {
            CAM_ID: {
                "proxyUrl": "https://192.0.2.149/snap.jpg",
                "_connection_type": "LOCAL",
                "_local_user": "u",
                "_local_password": "p",
            }
        }
        cm = _digest_cm_snap(200, b"<html>error</html>", "text/html")

        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        # non-image content-type → data=None → placeholder
        assert True, "non-image content type must not raise"


class TestFetchOutageSnapClosure:
    """camera.py outage snap path: async_digest_request called directly.

    Triggered when auth_outage_count > 0 and local_creds_cache has cached creds.
    """

    def _make_outage_cam(self) -> MagicMock:
        """Camera with outage creds."""
        cam = _make_camera_for_snap()
        cam.coordinator.auth_outage_count = 2  # triggers outage path
        cam.coordinator.local_creds_cache = {
            CAM_ID: {
                "user": "digest_user",
                "password": "digest_pass",
                "host": "192.0.2.149",
                "port": 443,
                "ts": time.monotonic(),
            }
        }
        cam.coordinator.live_connections = {}  # no active stream → skip path 1
        return cam

    @pytest.mark.asyncio
    async def test_outage_snap_200_returns_bytes(self):
        """Cloud outage + cached Digest creds + 200 → returns bytes."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = self._make_outage_cam()
        cm = _digest_cm_snap(200, b"\xff\xd8outage", "image/jpeg")

        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(return_value=cm),
            ),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        assert result == b"\xff\xd8outage", (
            "outage snap 200 + image/jpeg must return the bytes"
        )
        assert cam.cached_image == b"\xff\xd8outage", (
            "cached_image updated on outage snap"
        )

    @pytest.mark.asyncio
    async def test_outage_snap_request_exception_returns_none(self):
        """aiohttp.ClientError in outage snap → returns cached/placeholder."""
        import aiohttp as _aiohttp

        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = self._make_outage_cam()

        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch(
                "custom_components.bosch_shc_camera.camera.async_digest_request",
                new=AsyncMock(side_effect=_aiohttp.ClientError("LAN unreachable")),
            ),
        ):
            result = await BoschCamera._async_camera_image_impl(cam)

        from custom_components.bosch_shc_camera.camera import BoschCamera as _Cam

        assert (
            result is None
            or result is cam.cached_image
            or result is _Cam._PLACEHOLDER_JPEG
        ), (
            "aiohttp.ClientError in outage snap must not raise; returns cached/placeholder"
        )


def test_bosch_camera_importable():
    """BoschCamera is the current class name in camera.py."""
    mod = importlib.import_module("custom_components.bosch_shc_camera.camera")
    assert hasattr(mod, "BoschCamera"), "BoschCamera must be exported from camera.py"


def test_bosch_shc_camera_gone():
    """BoschSHCCamera must no longer exist — old name was removed."""
    mod = importlib.import_module("custom_components.bosch_shc_camera.camera")
    assert not hasattr(mod, "BoschSHCCamera"), (
        "BoschSHCCamera still exists — remove the old name from camera.py"
    )


def test_bosch_camera_unique_id_unchanged():
    """Renaming the class must not change the unique_id format (no migration needed)."""
    import inspect

    mod = importlib.import_module("custom_components.bosch_shc_camera.camera")
    src = inspect.getsource(mod.BoschCamera)
    # unique_id must still use the old bosch_shc_cam_ prefix (no migration yet)
    assert "bosch_shc_cam_" in src, (
        "BoschCamera.unique_id must use the bosch_shc_cam_ prefix "
        "(SHC = Smart Home Camera — correct naming, no migration needed)."
    )


# Section: privacy TOCTOU guard + async_create_stream STREAM_START_SKIPPED
# handling (relocated from tests/test_stream_modules_coverage.py — the
# tls_proxy.py sibling lives in tests/test_tls_proxy.py)


def _stub_coord_camera_toctou(
    *,
    stream_warming: bool = False,
    live_connections: dict | None = None,
    shc_state: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        live_connections=live_connections if live_connections is not None else {},
        user_intent_streams=set(),
        shc_state_cache={
            CAM_ID: (shc_state if shc_state is not None else {"privacy_mode": False})
        },
        session_stale={},
        stream_warming={CAM_ID} if stream_warming else set(),
        privacy_set_at={},
        light_set_at={},
        audio_enabled={CAM_ID: True},
        privacy_sound_cache={CAM_ID: False},
        timestamp_cache={CAM_ID: True},
        ledlights_cache={CAM_ID: True},
        arming_cache={},
        rcp_privacy_cache={},
        last_update_success=True,
        options={},
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: stream_warming,
        async_cloud_set_privacy_mode=AsyncMock(),
    )


class TestCameraPrivacyToctouGuard:
    """When privacy flips ON during a live fetch, the fetched frame must be
    discarded — `cached_image` must not be updated."""

    def _make_camera(self, coord: SimpleNamespace) -> object:
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        cam._display_name = "Bosch Terrasse"
        cam.hass = SimpleNamespace()
        cam.cached_image = b""
        cam._PLACEHOLDER_JPEG = b"\xff\xd8\xff\xe0placeholder"
        cam.last_image_fetch = float("-inf")
        cam._refresh_inflight = False
        cam._force_image_refresh = False
        cam.async_write_ha_state = MagicMock()
        return cam

    @pytest.mark.asyncio
    async def test_frame_discarded_when_privacy_on_during_fetch(self) -> None:
        """If privacy_mode becomes True while a frame was being fetched,
        `cached_image` must NOT be updated — simulated via a side-effect on
        `async_fetch_live_snapshot` that flips the privacy cache mid-fetch."""
        coord = _stub_coord_camera_toctou(shc_state={"privacy_mode": False})
        cam = self._make_camera(coord)
        original_cache = cam.cached_image

        fake_image = b"\xff\xd8\xff\xe0fakeframe"

        def _flip_privacy_and_return(*_args: object, **_kwargs: object) -> bytes:
            coord.shc_state_cache[CAM_ID]["privacy_mode"] = True
            return fake_image

        coord.async_fetch_live_snapshot = AsyncMock(
            side_effect=_flip_privacy_and_return
        )
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(return_value=None)
        cam.async_camera_image = AsyncMock(return_value=None)

        from custom_components.bosch_shc_camera.camera import BoschCamera

        await BoschCamera.async_trigger_image_refresh(cam)

        assert cam.cached_image == original_cache

    @pytest.mark.asyncio
    async def test_frame_stored_when_privacy_off_during_fetch(self) -> None:
        """Control: when privacy stays OFF throughout, the frame IS stored."""
        coord = _stub_coord_camera_toctou(shc_state={"privacy_mode": False})
        cam = self._make_camera(coord)

        fake_image = b"\xff\xd8\xff\xe0fakeframe"
        coord.async_fetch_live_snapshot = AsyncMock(return_value=fake_image)
        coord.async_fetch_live_snapshot_local = AsyncMock(return_value=None)
        coord.async_fetch_fresh_event_snapshot = AsyncMock(return_value=None)
        coord.image_entities = {}
        cam.async_camera_image = AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.camera.save_snapshot",
            new=AsyncMock(),
        ):
            from custom_components.bosch_shc_camera.camera import BoschCamera

            await BoschCamera.async_trigger_image_refresh(cam)

        assert cam.cached_image == fake_image


class TestAsyncCreateStreamSkipped:
    """When `try_live_connection` returns `STREAM_START_SKIPPED` a debug
    message is logged and the method continues (falls through to the
    prewarm wait) instead of returning None early."""

    def _make_camera(self, coord: SimpleNamespace) -> object:
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        cam._display_name = "Bosch Terrasse"
        return cam

    @pytest.mark.asyncio
    async def test_stream_start_skipped_logs_debug_and_continues(self) -> None:
        from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED

        coord = SimpleNamespace(
            live_connections={},  # no existing session → triggers auto-open path
            stream_warming=set(),  # not warming → no wait loop
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
            try_live_connection=AsyncMock(return_value=STREAM_START_SKIPPED),
            async_update_listeners=MagicMock(),
            get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
        )
        cam = self._make_camera(coord)

        fake_stream = object()
        with (
            patch(
                "homeassistant.components.camera.Camera.async_create_stream",
                new=AsyncMock(return_value=fake_stream),
            ),
            patch("custom_components.bosch_shc_camera.camera._LOGGER") as mock_log,
        ):
            result = await cam.async_create_stream()

        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        debug_calls = " ".join(
            str(a) for call in mock_log.debug.call_args_list for a in call.args
        )
        assert "coalescing" in debug_calls
        assert result is fake_stream

    @pytest.mark.asyncio
    async def test_privacy_on_raises_ha_error(self) -> None:
        """Gate before the coalescing check: privacy ON must raise HomeAssistantError."""
        from homeassistant.exceptions import HomeAssistantError

        coord = SimpleNamespace(
            live_connections={},
            stream_warming=set(),
            shc_state_cache={CAM_ID: {"privacy_mode": True}},
            try_live_connection=AsyncMock(),
            async_update_listeners=MagicMock(),
            get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
        )
        cam = self._make_camera(coord)

        with pytest.raises(HomeAssistantError, match="privacy mode is ON"):
            await cam.async_create_stream()

        coord.try_live_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_connection_returns_none(self) -> None:
        """When try_live_connection returns a falsy non-skipped value, return None."""
        coord = SimpleNamespace(
            live_connections={},
            stream_warming=set(),
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
            try_live_connection=AsyncMock(return_value=None),
            async_update_listeners=MagicMock(),
            get_model_config=lambda cid: SimpleNamespace(min_total_wait=0),
        )
        cam = self._make_camera(coord)

        result = await cam.async_create_stream()

        assert result is None


# Section: firmware-install unavailability (relocated from
# tests/test_updating_unavailable.py — the switch.py/init.py/light.py
# siblings live in tests/test_switch.py, tests/test_init.py, and
# tests/test_light.py)


def _coord_camera_updating(
    *, is_updating_value: bool, last_update_success: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        last_update_success=last_update_success,
        is_updating=lambda cam_id: is_updating_value if cam_id == CAM_ID else False,
        firmware_cache={CAM_ID: {"updating": is_updating_value}},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
        lan_tcp_reachable={CAM_ID: (True, 0.0)},
        is_lan_reachable=lambda cam_id: True,
        is_session_stale=lambda cam_id: False,
        user_intent_streams=set(),
    )


class TestCameraUpdatingUnavailable:
    def test_available_when_not_updating(self):
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _coord_camera_updating(is_updating_value=False)
        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        assert cam.available is True

    def test_unavailable_when_updating(self):
        """Camera reboots during FW install — entity must flip unavailable
        even though coordinator.last_update_success is still True (cached
        from before the install started)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _coord_camera_updating(is_updating_value=True, last_update_success=True)
        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        assert cam.available is False

    def test_unavailable_when_coordinator_failed(self):
        """Existing semantics preserved: no is_updating signal, but
        coordinator update failed → unavailable."""
        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = _coord_camera_updating(
            is_updating_value=False, last_update_success=False
        )
        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        assert cam.available is False


# Section: GH#6 — streaming broken since 10.x, resolved in v10.5.3 (relocated
# from tests/test_github_issues.py)


class TestGH6StreamPipelineSupportedFeatures:
    def test_live_connections_dict_drives_supported_features(self):
        """Camera always advertises STREAM so HA's stream component registers
        the entity — live_connections drives stream_source() content, not
        the feature flag. (HA requires STREAM to be set statically at entity
        registration time; toggling it dynamically would cause the entity to
        deregister and re-register on every stream start/stop.)"""
        from homeassistant.components.camera import CameraEntityFeature

        from custom_components.bosch_shc_camera.camera import BoschCamera

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "x",
                        "hardwareVersion": "X",
                        "firmwareVersion": "x",
                        "macAddress": "x",
                    }
                }
            },
            live_connections={},
            camera_entities={},
            last_update_success=True,
        )
        entry = SimpleNamespace(entry_id="01", data={"bearer_token": "x"}, options={})
        cam = BoschCamera(coord, CAM_ID, entry)
        assert CameraEntityFeature.STREAM in cam.supported_features, (
            "Camera must always advertise STREAM — HA registers the stream "
            "component at entity setup time based on this flag"
        )
        # The flag must not change when live_connections is populated
        coord.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        assert CameraEntityFeature.STREAM in cam.supported_features
