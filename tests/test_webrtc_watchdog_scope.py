"""Regression tests for the webrtc-watchdog over-broad provider refresh.

Bug reported by Thomas 2026-05-20: woke up to find `switch.bosch_innenbereich_live_stream`
visibly ON even though no user / automation / card had requested it. The Bosch app
showed the camera as actively streaming.

Log trace pinpointed the trigger:

    05:15:49.452 webrtc-watchdog: refreshed providers on camera.bosch_innenbereich
    05:15:49.452 webrtc-watchdog: refreshed providers on camera.bosch_kamera
    05:15:49.452 webrtc-watchdog: refreshed providers on camera.bosch_garten
    05:15:49.499 fetch_live_snapshot: proxy cache MISS for 22222222 — PUT /connection done
    05:15:49.650 PUT /connection type=LOCAL → HTTP 200
    05:15:49.651 Live connection opened! type=LOCAL
    05:15:49.685 TLS proxy for 22222222 started on 127.0.0.1:45907
    05:15:49.685 LOCAL pre-warm for 22222222 (Eyes Innenkamera II)

Root cause: `_ensure_go2rtc_schemes_fresh` (and the post-reload arm of
`_webrtc_recovery_watchdog`) iterated ALL camera entities with the STREAM
feature flag and called `async_refresh_providers()` on each. HA Core's
`async_refresh_providers` resolves the WebRTC provider by calling
`stream_source()` on the camera entity. Our `stream_source()` opens a new
LOCAL session via `try_live_connection()` if none is active — populating
`_live_connections[cam_id]` and flipping the live-stream switch to ON on
the spot, even for cameras nobody asked to view.

Fix: only refresh providers on cameras that are already streaming
(`cam_id in coordinator._live_connections`). The watchdog exists to
restore WebRTC support on an active stream after a stale-schemes race;
it has no reason to touch idle cameras.

These tests pin the contract so the bug cannot regress.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_STREAMING = "11111111-1111-1111-1111-111111111111"
CAM_IDLE_1    = "22222222-2222-2222-2222-222222222222"
CAM_IDLE_2    = "44444444-4444-4444-4444-444444444444"


def _make_cam_entity(cam_id: str, *, with_stream_feature: bool = True):
    """Camera-entity stub: has STREAM feature flag + async_refresh_providers."""
    from homeassistant.components.camera import CameraEntityFeature

    ent = MagicMock()
    ent.cam_id = cam_id
    ent.entity_id = f"camera.bosch_{cam_id[:8].lower()}"
    ent.supported_features = (
        CameraEntityFeature.STREAM if with_stream_feature else CameraEntityFeature(0)
    )
    ent.async_refresh_providers = AsyncMock()
    return ent


def _make_coord(*, streaming: set[str], idle: set[str]):
    """Coordinator stub seeded with mixed streaming+idle cams."""
    cams = {cid: _make_cam_entity(cid) for cid in streaming | idle}
    live_connections = {cid: {"rtspsUrl": "rtsps://x"} for cid in streaming}
    return SimpleNamespace(
        hass=MagicMock(),
        _camera_entities=cams,
        _live_connections=live_connections,
        _last_schemes_refresh=float("-inf"),
        _last_go2rtc_reload=float("-inf"),
    ), cams


class TestEnsureGo2rtcSchemesFresh:
    """`_ensure_go2rtc_schemes_fresh` must refresh providers ONLY on
    cameras that already have an active session. Calling it on idle
    cameras triggers our `stream_source` → `try_live_connection` chain
    and opens unwanted LOCAL sessions."""

    @pytest.mark.asyncio
    async def test_idle_cameras_are_not_refreshed(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, cams = _make_coord(
            streaming={CAM_STREAMING},
            idle={CAM_IDLE_1, CAM_IDLE_2},
        )
        provider = MagicMock()
        provider._supported_schemes = set()
        provider._rest_client.schemes.list = AsyncMock(return_value={"hls", "web_rtc"})

        with patch(
            "homeassistant.components.camera.webrtc.DATA_WEBRTC_PROVIDERS",
            create=True, new="webrtc_providers_key",
        ):
            coord.hass.data = {"webrtc_providers_key": {provider}}
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        cams[CAM_STREAMING].async_refresh_providers.assert_called_once()
        cams[CAM_IDLE_1].async_refresh_providers.assert_not_called()
        cams[CAM_IDLE_2].async_refresh_providers.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_streaming_cams_all_refreshed(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, cams = _make_coord(
            streaming={CAM_STREAMING, CAM_IDLE_1},  # both streaming
            idle={CAM_IDLE_2},
        )
        provider = MagicMock()
        provider._supported_schemes = set()
        provider._rest_client.schemes.list = AsyncMock(return_value={"hls", "web_rtc"})

        with patch(
            "homeassistant.components.camera.webrtc.DATA_WEBRTC_PROVIDERS",
            create=True, new="webrtc_providers_key",
        ):
            coord.hass.data = {"webrtc_providers_key": {provider}}
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        cams[CAM_STREAMING].async_refresh_providers.assert_called_once()
        cams[CAM_IDLE_1].async_refresh_providers.assert_called_once()
        cams[CAM_IDLE_2].async_refresh_providers.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_refresh_when_no_streams_active(self):
        """Edge case: watchdog fires but no cam is streaming. No-op."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, cams = _make_coord(streaming=set(), idle={CAM_STREAMING, CAM_IDLE_1})
        provider = MagicMock()
        provider._supported_schemes = set()
        provider._rest_client.schemes.list = AsyncMock(return_value={"hls", "web_rtc"})

        with patch(
            "homeassistant.components.camera.webrtc.DATA_WEBRTC_PROVIDERS",
            create=True, new="webrtc_providers_key",
        ):
            coord.hass.data = {"webrtc_providers_key": {provider}}
            await BoschCameraCoordinator._ensure_go2rtc_schemes_fresh(coord)

        for ent in cams.values():
            ent.async_refresh_providers.assert_not_called()


class TestWebRTCRecoveryWatchdog:
    """Post-reload arm of `_check_and_recover_webrtc` must apply the same
    filter. After reloading the go2rtc config entry it iterates camera
    entities to push the fresh provider cache — that loop must skip
    idle cams for the same reason."""

    def test_recovery_loop_has_live_connections_filter(self):
        """Source-level pin: the per-cam loop inside `_check_and_recover_webrtc`
        must reference `_live_connections` so idle cams are skipped.

        The watchdog is hard to invoke standalone (depends on go2rtc config
        entries, live stream context, etc.). Source-grep is a small but
        unambiguous regression guard — if the filter is removed, this fails.
        """
        import inspect
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        src = inspect.getsource(BoschCameraCoordinator._check_and_recover_webrtc)
        assert "_live_connections" in src, (
            "REGRESSION: _check_and_recover_webrtc source no longer references "
            "_live_connections — the per-cam-iteration filter must be in place "
            "to avoid opening new sessions on idle cams during post-reload "
            "provider refresh."
        )

    def test_schemes_fresh_loop_has_live_connections_filter(self):
        """Companion to the above: same source-grep guard for
        `_ensure_go2rtc_schemes_fresh`."""
        import inspect
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        src = inspect.getsource(BoschCameraCoordinator._ensure_go2rtc_schemes_fresh)
        assert "_live_connections" in src, (
            "REGRESSION: _ensure_go2rtc_schemes_fresh source no longer references "
            "_live_connections — the per-cam-iteration filter must be in place."
        )
