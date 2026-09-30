"""External-recorder endpoint for a local-data-interface camera.

The front door relays the recorder to go2rtc's restream (no TLS proxy, no
Digest dance, no second camera connection); allowlist and connection-cap
semantics are the front door's own and stay untouched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera import frigate_endpoint as fe
from custom_components.bosch_shc_camera.remote_viewing_front_door import (
    RemoteTarget,
    _PathRewriteRelay,
)
from tests.test_ldi_local import CAM, RESTREAM, _coord

MODULE = "custom_components.bosch_shc_camera"
LDI_LIVE: dict[str, Any] = {"_connection_type": "LOCAL", "_ldi": True}


def _patched_ensure(url: str | None = RESTREAM) -> Any:
    return patch(
        f"{MODULE}.frigate_endpoint.ensure_ldi_stream", new=AsyncMock(return_value=url)
    )


class TestResolveTarget:
    @pytest.mark.asyncio
    async def test_points_at_the_restream(self) -> None:
        c = _coord()
        c.live_connections = {CAM: dict(LDI_LIVE)}
        c.try_live_connection = AsyncMock()
        with _patched_ensure():
            target = await BoschCameraCoordinator._frigate_resolve_inner(c, CAM)  # type: ignore[arg-type]
        assert target == RemoteTarget(port=18554, path="/ldi_11111111", keep_track=True)
        c.try_live_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_tls_proxy_or_credentials_involved(self) -> None:
        c = _coord()
        c.tls_proxy_ports = {}
        c.live_connections = {CAM: dict(LDI_LIVE)}
        with _patched_ensure():
            target = await BoschCameraCoordinator._frigate_resolve_inner(c, CAM)  # type: ignore[arg-type]
        assert target is not None
        assert not hasattr(target, "digest_password")

    @pytest.mark.asyncio
    async def test_go2rtc_not_serving_means_503_not_cloud(self) -> None:
        c = _coord()
        c.live_connections = {CAM: dict(LDI_LIVE)}
        c.try_live_connection = AsyncMock()
        with _patched_ensure(None):
            assert await BoschCameraCoordinator._frigate_resolve_inner(c, CAM) is None  # type: ignore[arg-type]
        c.try_live_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_url_without_port_is_rejected(self) -> None:
        c = _coord()
        c.live_connections = {CAM: dict(LDI_LIVE)}
        with _patched_ensure("rtsp://127.0.0.1/ldi_11111111"):
            assert await BoschCameraCoordinator._frigate_resolve_inner(c, CAM) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_opens_the_session_on_demand_and_gives_up_when_it_fails(self) -> None:
        c = _coord()
        c.try_live_connection = AsyncMock(return_value=None)
        with _patched_ensure() as ensure:
            assert await BoschCameraCoordinator._frigate_resolve_inner(c, CAM) is None  # type: ignore[arg-type]
        c.try_live_connection.assert_awaited_once_with(CAM)
        ensure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cloud_camera_keeps_the_digest_target(self) -> None:
        """Terrasse case: interface active but no password stored."""
        c = _coord(passwords={})
        c.live_connections = {
            CAM: {
                "_connection_type": "LOCAL",
                "_local_user": "u",
                "_local_password": "test-pw",
            }
        }
        c.tls_proxy_ports = {CAM: 40000}
        with _patched_ensure() as ensure:
            target = await BoschCameraCoordinator._frigate_resolve_inner(c, CAM)  # type: ignore[arg-type]
        assert (target.port, target.digest_user, target.digest_password) == (
            40000,
            "u",
            "test-pw",
        )
        ensure.assert_not_awaited()


class TestRelayFactory:
    def _args(self, target: Any) -> Any:
        return (
            "cam-1",
            MagicMock(),
            MagicMock(),
            target,
            b"DESCRIBE x RTSP/1.0\r\n\r\n",
        )

    def test_restream_target_gets_the_path_rewriting_relay(self) -> None:
        relay = fe._default_relay_factory(
            *self._args(RemoteTarget(18554, "/ldi_11111111"))
        )
        assert isinstance(relay, _PathRewriteRelay)
        assert relay._rewritten_uri() == "rtsp://127.0.0.1:18554/ldi_11111111"

    def test_restream_keeps_only_the_track_token(self) -> None:
        """go2rtc picks the track from `streamid=N` (server.go reqTrackID)."""
        relay = fe._default_relay_factory(
            *self._args(RemoteTarget(18554, "/ldi_11111111", keep_track=True))
        )
        base = "rtsp://127.0.0.1:18554/ldi_11111111"
        setup = b"SETUP rtsp://h:1/evil/../x/streamid=1 RTSP/1.0\r\n\r\n"
        assert relay._rewritten_uri(setup) == f"{base}/streamid=1"
        # go2rtc's SDP advertises `a=control:trackID=N`, so ffmpeg-based
        # recorders send that form, appended after the DESCRIBE query; it
        # used to be dropped and go2rtc answered 400 to the bare path.
        track = (
            b"SETUP rtsp://127.0.0.1:1/rtsp_tunnel?inst=1&enableaudio=1"
            b"/trackID=1 RTSP/1.0\r\n\r\n"
        )
        assert relay._rewritten_uri(track) == f"{base}/trackID=1"
        for req in (
            b"DESCRIBE rtsp://h:1/anything RTSP/1.0\r\n\r\n",
            b"SETUP rtsp://h:1/x/streamid=1/../../y RTSP/1.0\r\n\r\n",
            b"GARBAGE\r\n\r\n",
            b"",
        ):
            assert relay._rewritten_uri(req) == base

    def test_digest_target_keeps_the_digest_relay(self) -> None:
        relay = fe._default_relay_factory(*self._args(fe.InnerTarget(9, "u", "p")))
        assert isinstance(relay, fe._Relay)

    def test_other_targets_keep_the_digest_relay(self) -> None:
        assert isinstance(
            fe._default_relay_factory(*self._args(SimpleNamespace())), fe._Relay
        )
