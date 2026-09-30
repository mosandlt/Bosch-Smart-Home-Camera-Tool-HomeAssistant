"""Snapshots of a local-data-interface camera without the Bosch cloud.

One frame of the camera's 1 Hz JPEG preview stream (inst=3, no audio) is read
through a short-lived go2rtc producer `ldi_<id8>_snap`. Pins: success, go2rtc
missing (fails closed, cached image kept), privacy on (no frame, no request),
one camera session shared by several entity requests, retry throttling, the
camera entity and coordinator seams, and the Terrasse case (interface active,
no password) staying on the cloud path.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from yarl import URL

from custom_components.bosch_shc_camera import BoschCameraCoordinator, ldi_go2rtc
from custom_components.bosch_shc_camera import ldi_snapshot as snap
from custom_components.bosch_shc_camera.camera import BoschCamera
from tests.test_camera import CAM_ID, _make_camera_impl, _make_coord_impl
from tests.test_ldi_go2rtc import API, FakeGo2rtc, _Resp
from tests.test_ldi_local import CAM, IP, PW, _coord

JPEG = b"\xff\xd8\xff\xe0fake-preview-frame"
SNAP_SRC = f"rtsps://localuser:{PW}@{IP}:9554/rtsp_tunnel?line=1&inst=3&enableaudio=0"


class _Frame:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> _Frame:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def read(self) -> bytes:
        return self._body


class FrameGo2rtc(FakeGo2rtc):
    """go2rtc stand-in that also serves /api/frame.jpeg."""

    def __init__(self) -> None:
        super().__init__()
        self.frames: list[str] = []
        self.frame_status = 200
        self.frame_body = JPEG
        self.frame_error: BaseException | None = None

    def get(self, url: URL, params: dict[str, str] | None = None) -> Any:
        if url.path == "/api/frame.jpeg":
            self.frames.append((params or {})["src"])
            if self.frame_error is not None:
                raise self.frame_error
            return _Frame(self.frame_status, self.frame_body)
        return super().get(url, params)


def _coord_snap(fake: FakeGo2rtc | None, **kw: Any) -> SimpleNamespace:
    c = _coord(**kw)
    c.hass = SimpleNamespace(
        data={"go2rtc": SimpleNamespace(url=API, session=fake)} if fake else {}
    )
    return c


@pytest.fixture
def go2rtc() -> FrameGo2rtc:
    return FrameGo2rtc()


class TestFetchSnapshot:
    @pytest.mark.asyncio
    async def test_reads_one_frame_of_the_preview_stream(
        self, go2rtc: FrameGo2rtc
    ) -> None:
        c = _coord_snap(go2rtc)
        assert await snap.fetch_snapshot(c, CAM) == JPEG  # type: ignore[arg-type]
        # own stream, preview stream without audio; the main stream is untouched
        assert go2rtc.streams == {"ldi_11111111_snap": [SNAP_SRC]}
        assert go2rtc.frames == ["ldi_11111111_snap"]

    @pytest.mark.asyncio
    async def test_no_cloud_call(self, go2rtc: FrameGo2rtc) -> None:
        c = _coord_snap(go2rtc)
        c.token = None
        with patch(
            "custom_components.bosch_shc_camera.cloud_ssl.async_get_bosch_cloud_session",
            new=AsyncMock(side_effect=AssertionError("cloud")),
        ):
            assert await snap.fetch_snapshot(c, CAM) == JPEG  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_concurrent_requests_share_one_camera_session(
        self, go2rtc: FrameGo2rtc
    ) -> None:
        c = _coord_snap(go2rtc)
        results = await asyncio.gather(
            *(snap.fetch_snapshot(c, CAM) for _ in range(5))  # type: ignore[arg-type]
        )
        assert results == [JPEG] * 5
        assert len(go2rtc.frames) == 1
        assert len(go2rtc.puts) == 1

    @pytest.mark.asyncio
    async def test_frame_is_reused_for_a_few_seconds_then_refetched(
        self, go2rtc: FrameGo2rtc
    ) -> None:
        c = _coord_snap(go2rtc)
        await snap.fetch_snapshot(c, CAM)  # type: ignore[arg-type]
        await snap.fetch_snapshot(c, CAM)  # type: ignore[arg-type]
        assert len(go2rtc.frames) == 1
        c.ldi_snapshot_state[CAM]["at"] -= snap.SNAPSHOT_SHARE_SEC + 1
        go2rtc.frame_body = JPEG + b"2"
        assert await snap.fetch_snapshot(c, CAM) == JPEG + b"2"  # type: ignore[arg-type]
        assert len(go2rtc.frames) == 2
        assert len(go2rtc.puts) == 1  # the producer stays registered

    @pytest.mark.asyncio
    async def test_returned_frame_is_a_copy_of_the_shared_one(
        self, go2rtc: FrameGo2rtc
    ) -> None:
        c = _coord_snap(go2rtc)
        first = await snap.fetch_snapshot(c, CAM)  # type: ignore[arg-type]
        second = await snap.fetch_snapshot(c, CAM)  # type: ignore[arg-type]
        assert first == second and isinstance(second, bytes)

    # ── failure modes: always fail closed, never the cloud ──────────────────
    @pytest.mark.asyncio
    async def test_go2rtc_missing_fails_closed_and_is_not_hammered(self) -> None:
        c = _coord_snap(None)
        with patch.object(
            ldi_go2rtc, "ensure_stream", new=AsyncMock(return_value=None)
        ) as ensure:
            assert await snap.fetch_snapshot(c, CAM) is None  # type: ignore[arg-type]
            assert await snap.fetch_snapshot(c, CAM) is None  # type: ignore[arg-type]
        assert ensure.await_count == 1  # second call is inside the retry window

    @pytest.mark.asyncio
    async def test_retries_after_the_window(self, go2rtc: FrameGo2rtc) -> None:
        c = _coord_snap(go2rtc)
        go2rtc.frame_status = 500
        assert await snap.fetch_snapshot(c, CAM) is None  # type: ignore[arg-type]
        go2rtc.frame_status = 200
        assert await snap.fetch_snapshot(c, CAM) is None  # type: ignore[arg-type]
        c.ldi_snapshot_state[CAM]["failed_at"] -= snap.SNAPSHOT_RETRY_SEC + 1
        assert await snap.fetch_snapshot(c, CAM) == JPEG  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mutate",
        [
            lambda g: setattr(g, "frame_status", 404),
            lambda g: setattr(g, "frame_body", b"<html>"),
            lambda g: setattr(g, "frame_body", b""),
            lambda g: setattr(g, "frame_error", TimeoutError()),
            lambda g: setattr(g, "frame_error", aiohttp.ClientConnectionError()),
        ],
    )
    async def test_bad_frame_answers(self, go2rtc: FrameGo2rtc, mutate: Any) -> None:
        mutate(go2rtc)
        assert await snap.fetch_snapshot(_coord_snap(go2rtc), CAM) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_go2rtc_vanishing_between_register_and_frame(
        self, go2rtc: FrameGo2rtc
    ) -> None:
        c = _coord_snap(go2rtc)
        with (
            patch.object(
                ldi_go2rtc,
                "ensure_stream",
                new=AsyncMock(return_value="rtsp://127.0.0.1:1/x"),
            ),
            patch.object(
                ldi_go2rtc, "resolve_endpoint", new=AsyncMock(return_value=None)
            ),
        ):
            assert await snap.fetch_snapshot(c, CAM) is None  # type: ignore[arg-type]

    # ── privacy ────────────────────────────────────────────────────────────
    @pytest.mark.asyncio
    @pytest.mark.parametrize("where", ["cloud", "camera"])
    async def test_privacy_on_means_no_frame_and_no_request(
        self, go2rtc: FrameGo2rtc, where: str
    ) -> None:
        c = _coord_snap(go2rtc, privacy=where == "cloud")
        if where == "camera":
            c.ldi_rest_state = {CAM: {"privacy_on": True}}
        assert await snap.fetch_snapshot(c, CAM) is None  # type: ignore[arg-type]
        assert go2rtc.puts == [] and go2rtc.frames == []
        assert getattr(c, "ldi_snapshot_state", {}) == {}

    @pytest.mark.asyncio
    async def test_camera_says_privacy_off_beats_a_stale_cloud_flag(
        self, go2rtc: FrameGo2rtc
    ) -> None:
        c = _coord_snap(go2rtc, privacy=True)
        c.ldi_rest_state = {CAM: {"privacy_on": False}}
        assert await snap.fetch_snapshot(c, CAM) == JPEG  # type: ignore[arg-type]

    # ── who is asked at all ────────────────────────────────────────────────
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kw",
        [{"passwords": {}}, {"state": "inactive"}, {"ip": None}],
    )
    async def test_cameras_not_on_the_local_path_get_nothing(
        self, go2rtc: FrameGo2rtc, kw: dict[str, Any]
    ) -> None:
        c = _coord_snap(go2rtc, **kw)
        assert await snap.fetch_snapshot(c, CAM) is None  # type: ignore[arg-type]
        assert go2rtc.puts == [] and go2rtc.frames == []


class TestCoordinatorSeam:
    @pytest.mark.asyncio
    async def test_ldi_camera_snapshot_comes_from_the_preview_stream(self) -> None:
        c = SimpleNamespace(token="tok", shc_state_cache={})
        with (
            patch(
                "custom_components.bosch_shc_camera.coordinator.ldi_wanted",
                return_value=True,
            ),
            patch(
                "custom_components.bosch_shc_camera.coordinator.fetch_ldi_snapshot",
                new=AsyncMock(return_value=JPEG),
            ) as fetch,
        ):
            out = await BoschCameraCoordinator._async_fetch_live_snapshot_impl(c, CAM)  # type: ignore[arg-type]
        assert out == JPEG
        fetch.assert_awaited_once_with(c, CAM)

    @pytest.mark.asyncio
    async def test_local_digest_variant_still_never_calls_the_cloud(self) -> None:
        c = SimpleNamespace(token="tok")
        with patch(
            "custom_components.bosch_shc_camera.coordinator.ldi_wanted",
            return_value=True,
        ):
            assert (
                await BoschCameraCoordinator.async_fetch_live_snapshot_local(c, CAM)  # type: ignore[arg-type]
                is None
            )


def _ldi_camera(
    *, privacy_cloud: bool | None, passwords: dict[str, str] | None, **kw: Any
) -> Any:
    coord = _make_coord_impl(
        shc_state_cache={CAM_ID: {"privacy_mode": privacy_cloud}},
        local_data_interface_cache={CAM_ID: {"state": "active"}},
        entry=SimpleNamespace(options={"local_passwords": passwords or {}}),
        token="tok",
        async_fetch_live_snapshot=AsyncMock(return_value=None),
        async_fetch_live_snapshot_local=AsyncMock(return_value=None),
        ldi_rest_state={},
    )
    return _make_camera_impl(coord=coord, **kw)


class TestCameraEntity:
    @pytest.mark.asyncio
    async def test_success_updates_the_cached_image(self) -> None:
        cam = _ldi_camera(privacy_cloud=False, passwords={CAM_ID: PW})
        cam.coordinator.async_fetch_live_snapshot = AsyncMock(return_value=JPEG)
        assert await BoschCamera._async_camera_image_impl(cam) == JPEG
        assert cam.cached_image == JPEG
        assert cam.last_image_fetch > 0
        cam.coordinator.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)
        cam.coordinator.async_fetch_live_snapshot_local.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_go2rtc_missing_keeps_the_last_image_while_privacy_is_off(
        self,
    ) -> None:
        cam = _ldi_camera(
            privacy_cloud=False, passwords={CAM_ID: PW}, cached_image=b"\xff\xd8old"
        )
        assert await BoschCamera._async_camera_image_impl(cam) == b"\xff\xd8old"

    @pytest.mark.asyncio
    async def test_unknown_privacy_and_no_frame_serves_nothing(self) -> None:
        cam = _ldi_camera(
            privacy_cloud=None, passwords={CAM_ID: PW}, cached_image=b"\xff\xd8old"
        )
        assert await BoschCamera._async_camera_image_impl(cam) is None

    @pytest.mark.asyncio
    async def test_cloud_privacy_on_short_circuits_before_any_fetch(self) -> None:
        cam = _ldi_camera(privacy_cloud=True, passwords={CAM_ID: PW})
        assert await BoschCamera._async_camera_image_impl(cam) is None
        cam.coordinator.async_fetch_live_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_privacy_reported_by_the_camera_serves_nothing_quietly(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cam = _ldi_camera(
            privacy_cloud=False, passwords={CAM_ID: PW}, cached_image=b"\xff\xd8old"
        )
        cam.coordinator.ldi_rest_state = {CAM_ID: {"privacy_on": True}}
        assert await BoschCamera._async_camera_image_impl(cam) is None
        assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]

    @pytest.mark.asyncio
    async def test_interface_active_without_password_stays_on_the_cloud_path(
        self,
    ) -> None:
        """Terrasse case: no LDI snapshot machinery is touched."""
        cam = _ldi_camera(privacy_cloud=False, passwords=None)
        cam.coordinator.async_fetch_live_snapshot = AsyncMock(
            return_value=b"\xff\xd8cloud"
        )
        with (
            patch(
                "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
            patch.object(cam, "_async_ldi_image", new=AsyncMock()) as ldi_image,
            patch(
                "custom_components.bosch_shc_camera.ldi_snapshot.fetch_snapshot",
                new=AsyncMock(),
            ) as fetch,
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == b"\xff\xd8cloud"
        ldi_image.assert_not_awaited()
        fetch.assert_not_awaited()
