"""Camera entity coverage round 2 — pure helpers + state-machine attrs.

Pins behaviors that test_camera.py left out:
  - `_yuv422_to_jpeg` 320×180 raw-frame fallback (RCP 0x0c98 path on
    Gen1 cameras when 0x099e JPEG is unavailable). Pure NumPy/PIL
    conversion — no I/O.
  - `extra_state_attributes` stream_status enum: idle / connecting /
    streaming / streaming (REMOTE fallback) / warming_up.
  - Optional attribute population (buffering_time_ms, connection_type,
    bosch_priority, stream_url) — must appear only when their backing
    fields are set, never as None / "".
  - `stream_source()` LOCAL vs REMOTE branch — sets `stream_options`
    side-effect ({"rtsp_transport":"tcp"} for LOCAL, empty for REMOTE).
"""

from __future__ import annotations

import asyncio
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
                "status": "ONLINE",
            }
        },
        _live_connections={},
        _camera_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        _audio_enabled={},
        last_update_success=True,
        motion_settings=lambda cam_id: {},
        is_stream_warming=lambda cam_id: False,
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800, "live_buffer_mode": "balanced"},
    )


# ── _yuv422_to_jpeg (RCP YUV422 raw-frame fallback) ─────────────────────


class TestYuv422ToJpeg:
    """Pin the YUV422→JPEG converter used as Gen1 thumbnail fallback.

    Reason: Gen1 360 cameras returned 320×180 raw YUV422 frames via RCP
    0x0c98 when the JPEG path (0x099e) was unavailable. Without this
    converter the integration falls through to placeholder, hiding live
    state from the dashboard. The dimensions (320×180) and total byte
    count (115200) are wired into the camera-side firmware — values
    other than 320*180*2 must reject so we don't hand garbage to PIL.
    """

    def _make_cam(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        return BoschSHCCamera(stub_coord, CAM_ID, stub_entry)

    def test_wrong_size_returns_none(self, stub_coord, stub_entry):
        cam = self._make_cam(stub_coord, stub_entry)
        assert cam._yuv422_to_jpeg(b"x" * 100) is None, (
            "Non-115200-byte payload must reject (firmware contract: "
            "320×180×2 = 115200). Accepting any other size would feed "
            "PIL malformed shape and crash the snapshot path."
        )

    def test_empty_bytes_returns_none(self, stub_coord, stub_entry):
        cam = self._make_cam(stub_coord, stub_entry)
        assert cam._yuv422_to_jpeg(b"") is None

    def test_one_byte_off_returns_none(self, stub_coord, stub_entry):
        """115199 bytes — off-by-one defends against truncated reads."""
        cam = self._make_cam(stub_coord, stub_entry)
        assert cam._yuv422_to_jpeg(b"\x00" * 115199) is None
        assert cam._yuv422_to_jpeg(b"\x00" * 115201) is None

    def test_all_zero_yuv_produces_valid_jpeg(self, stub_coord, stub_entry):
        """All-zero YUV422 = uniform dark green frame → must encode
        without error and produce JPEG bytes."""
        cam = self._make_cam(stub_coord, stub_entry)
        raw = b"\x00" * (320 * 180 * 2)
        out = cam._yuv422_to_jpeg(raw)
        assert out is not None
        assert out[:3] == b"\xff\xd8\xff", (
            "Output must be a JPEG (SOI marker FF D8 FF…). Anything "
            "else means our exception handler ate a real failure."
        )
        # SOI + APP0/JFIF + ... + EOI
        assert out[-2:] == b"\xff\xd9", "JPEG must end with EOI marker"

    def test_uniform_yuv_produces_valid_jpeg(self, stub_coord, stub_entry):
        """Y=128, U=128, V=128 → valid neutral grey frame."""
        import struct
        cam = self._make_cam(stub_coord, stub_entry)
        # YUYV: Y0 U Y1 V repeats. Make it a flat grey field.
        # raw[:,:,0] = Y plane (128), raw[:,:,1] alternates U=128/V=128
        raw = (b"\x80" + b"\x80") * (320 * 180)
        assert len(raw) == 115200
        out = cam._yuv422_to_jpeg(raw)
        assert out is not None
        assert out[:3] == b"\xff\xd8\xff"

    def test_high_contrast_yuv_produces_jpeg(self, stub_coord, stub_entry):
        """Alternating Y=0 / Y=255 across the frame must still encode."""
        cam = self._make_cam(stub_coord, stub_entry)
        # Build a frame with Y bytes alternating, UV all 128
        row = b""
        for _ in range(160):  # 320 px in YUYV = 160 macropixel pairs
            row += b"\x00\x80\xff\x80"  # Y0=0 U=128 Y1=255 V=128
        raw = row * 180
        assert len(raw) == 115200
        out = cam._yuv422_to_jpeg(raw)
        assert out is not None
        assert out.startswith(b"\xff\xd8\xff")


# ── extra_state_attributes stream_status state-machine ──────────────────


class TestStreamStatusAttribute:
    """Pin the 5-state stream_status enum exposed via attributes.

    The Lovelace card reads `stream_status` to render the badge color
    (idle / connecting / warming / streaming / fallback). Drift in this
    enum string immediately breaks badge rendering on every dashboard.
    """

    def test_idle_when_no_session_no_warming(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "idle"
        assert attrs["streaming_state"] == "idle"

    def test_streaming_when_live_session_active(self, stub_coord, stub_entry):
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "REMOTE",
        }
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "streaming"
        assert attrs["streaming_state"] == "active"

    def test_streaming_remote_fallback(self, stub_coord, stub_entry):
        """When LOCAL was tried and lost → REMOTE, badge shows fallback."""
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "REMOTE",
        }
        stub_coord._stream_fell_back[CAM_ID] = True
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "streaming (REMOTE fallback)"

    def test_warming_up_takes_priority(self, stub_coord, stub_entry):
        """While the encoder is pre-warming the badge must show
        `warming_up` even if a live_connection is in flight."""
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "LOCAL",
        }
        stub_coord.is_stream_warming = lambda cam_id: True
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        assert attrs["stream_status"] == "warming_up", (
            "warming_up must beat streaming so the card shows the "
            "spinner instead of the live badge while pre-warm is mid-flight."
        )


# ── Optional attributes — population invariants ─────────────────────────


class TestOptionalAttributes:
    """Optional attributes must appear only when backed by a real value
    and never as `None` / empty-string. HA logbook + recorder include
    every attribute change, so empty noise pollutes history."""

    def test_buffering_time_only_when_set(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        assert "buffering_time_ms" not in attrs
        assert "connection_type" not in attrs

    def test_buffering_time_appears_with_live_session(self, stub_coord, stub_entry):
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc",
            "_connection_type": "LOCAL",
            "_bufferingTime": 500,
        }
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        attrs = cam.extra_state_attributes
        assert attrs["buffering_time_ms"] == 500
        assert attrs["connection_type"] == "LOCAL"

    def test_bosch_priority_passes_through(self, stub_coord, stub_entry):
        """`info.priority` (cloud float) appears as bosch_priority for
        the overview card's `use_bosch_sort` option."""
        stub_coord.data[CAM_ID]["info"]["priority"] = 1.5
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.extra_state_attributes["bosch_priority"] == 1.5

    def test_bosch_priority_none_when_absent(self, stub_coord, stub_entry):
        """Missing priority → None (not "" or 0). Card distinguishes
        these via `priority != null` check before sorting."""
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.extra_state_attributes["bosch_priority"] is None

    def test_live_buffer_mode_propagates_from_options(self, stub_coord, stub_entry):
        """Player-side buffer profile must reach the card via attribute."""
        stub_entry.options["live_buffer_mode"] = "low_latency"
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.extra_state_attributes["live_buffer_mode"] == "low_latency"

    def test_live_buffer_mode_defaults_to_balanced(self, stub_coord, stub_entry):
        """Missing option → 'balanced'. This default is wired into the
        card's BOSCH_BUFFER_PROFILES table — both ends must agree."""
        stub_entry.options.pop("live_buffer_mode", None)
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        assert cam.extra_state_attributes["live_buffer_mode"] == "balanced"


# ── stream_source() LOCAL vs REMOTE branch ──────────────────────────────


class TestStreamSourceTransport:
    """Pin the LOCAL=tcp / REMOTE=default invariant. HA-Core 2026.4 +
    FFmpeg Lavf 62 reject the UDP→TCP transport rewrite the proxy used
    to do, so LOCAL must force `rtsp_transport=tcp` on SETUP. REMOTE
    streams go directly to Bosch cloud proxy via rtsps:// and forcing
    TCP there breaks Gen1 Eyes Outdoor cloud streams (regression test
    against an older 'always force tcp' patch).
    """

    @pytest.mark.asyncio
    async def test_local_sets_tcp_transport(self, stub_coord, stub_entry):
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://127.0.0.1:46597/rtsp_tunnel",
            "_connection_type": "LOCAL",
        }
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        url = await cam.stream_source()
        assert url == "rtsps://127.0.0.1:46597/rtsp_tunnel"
        assert cam.stream_options == {"rtsp_transport": "tcp"}

    @pytest.mark.asyncio
    async def test_remote_uses_default_transport(self, stub_coord, stub_entry):
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy-12.live.cbs.boschsecurity.com:443/abc/rtsp_tunnel",
            "_connection_type": "REMOTE",
        }
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        url = await cam.stream_source()
        assert url.startswith("rtsps://")
        assert cam.stream_options == {}, (
            "REMOTE must NOT force tcp — Gen1 Eyes Outdoor cloud streams "
            "break when forced to TCP transport."
        )

    @pytest.mark.asyncio
    async def test_audio_off_strips_audio_param(self, stub_coord, stub_entry):
        """When the audio switch is OFF, `enableaudio=1` must be removed
        from the RTSP URL so the server doesn't stream the audio track
        unnecessarily."""
        stub_coord._live_connections[CAM_ID] = {
            "rtspsUrl": "rtsps://proxy/abc/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1",
            "_connection_type": "REMOTE",
        }
        stub_coord._audio_enabled[CAM_ID] = False
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        url = await cam.stream_source()
        assert "enableaudio=1" not in url
        assert "inst=1" in url and "fmtp=1" in url

    @pytest.mark.asyncio
    async def test_no_session_returns_none(self, stub_coord, stub_entry):
        """No live_connections entry → None (HA sees stream_source==None
        and returns 503 to the WebSocket caller, which is the documented
        graceful behavior — see test_supported_features_always_advertises_stream)."""
        from custom_components.bosch_shc_camera.camera import BoschSHCCamera
        cam = BoschSHCCamera(stub_coord, CAM_ID, stub_entry)
        url = await cam.stream_source()
        assert url is None
