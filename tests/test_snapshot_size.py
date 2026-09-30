"""`snapshot_size` option: size of the still images served to dashboards.

auto follows the requested width (unchanged behaviour), small <=320 px,
medium <=640 px, full never downscales. Pins every mode x requested width
(none/200/500/1000) on the cloud path (JpegSize / RCP-thumbnail selection) and
on the local-interface path (Pillow downscale of the shared full frame), plus
no-upscale, garbage bytes, executor use, the memoised resize and that the
persisted frame stays full size. Fixtures are built with Pillow, fake only.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from custom_components.bosch_shc_camera import ldi_snapshot as snap
from custom_components.bosch_shc_camera.camera import BoschCamera
from custom_components.bosch_shc_camera.const import (
    DEFAULT_OPTIONS,
    JPEG_SIZE_MEDIUM,
    JPEG_SIZE_THUMB,
    SNAPSHOT_SIZES,
    effective_snapshot_width,
    snapshot_size_limit,
)
from tests.test_camera import CAM_ID, _make_camera_r6, _make_coord_r6
from tests.test_ldi_local import PW
from tests.test_ldi_snapshot import _ldi_camera

MODES = ["auto", "small", "medium", "full"]
WIDTHS = [None, 200, 500, 1000]


def _jpeg(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


FULL = _jpeg(1920, 1080)


class _InlineExecutor:
    """hass stand-in whose executor call really runs the function."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        self.calls.append(func)
        return func(*args)


def _with_mode(cam: Any, mode: str | None) -> Any:
    cam._entry = SimpleNamespace(
        data={"bearer_token": "tok"},
        options={} if mode is None else {"snapshot_size": mode},
    )
    cam.hass = _InlineExecutor()
    cam._snapshot_size_memo = None
    return cam


class TestPureHelpers:
    def test_default_is_auto(self) -> None:
        assert DEFAULT_OPTIONS["snapshot_size"] == "auto"
        assert set(SNAPSHOT_SIZES) == set(MODES)

    @pytest.mark.parametrize("width", WIDTHS)
    def test_auto_and_garbage_keep_the_requested_width(self, width: Any) -> None:
        for mode in ("auto", "bogus", None, 5):
            assert effective_snapshot_width(mode, width) == width

    @pytest.mark.parametrize("width", WIDTHS)
    def test_full_drops_the_width(self, width: Any) -> None:
        assert effective_snapshot_width("full", width) is None

    @pytest.mark.parametrize(
        ("width", "expected"), [(None, 320), (0, 320), (200, 200), (500, 320)]
    )
    def test_small(self, width: Any, expected: int) -> None:
        assert effective_snapshot_width("small", width) == expected

    @pytest.mark.parametrize(
        ("width", "expected"),
        [(None, 640), (-1, 640), (200, 200), (500, 500), (1000, 640)],
    )
    def test_medium(self, width: Any, expected: int) -> None:
        assert effective_snapshot_width("medium", width) == expected

    def test_limits(self) -> None:
        assert snapshot_size_limit("small") == JPEG_SIZE_THUMB
        assert snapshot_size_limit("medium") == JPEG_SIZE_MEDIUM
        assert snapshot_size_limit("full") is None
        assert snapshot_size_limit("auto") is None
        assert snapshot_size_limit("junk") is None


# expected JpegSize handed to the camera (None = full) and whether the RCP
# 320x180 thumbnail is tried first, per (mode, requested width)
def _expected_cloud(mode: str, width: int | None) -> tuple[int | None, bool]:
    if mode == "small":
        return JPEG_SIZE_THUMB, True
    if mode == "medium":
        if width is not None and width <= JPEG_SIZE_THUMB:
            return JPEG_SIZE_THUMB, True
        return JPEG_SIZE_MEDIUM, True
    if mode == "full":
        return None, False
    if width is None or width > JPEG_SIZE_MEDIUM:
        return None, False
    return (JPEG_SIZE_THUMB if width <= JPEG_SIZE_THUMB else JPEG_SIZE_MEDIUM), True


class TestCloudPath:
    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.asyncio
    async def test_size_selection(self, mode: str, width: int | None) -> None:
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8snap")
        cam = _with_mode(_make_camera_r6(coord=coord), mode)
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            await BoschCamera._async_camera_image_impl(cam, width=width)
        size, rcp = _expected_cloud(mode, width)
        coord.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID, jpeg_size=size)
        assert cam._async_rcp_thumbnail.await_count == (1 if rcp else 0)

    @pytest.mark.asyncio
    async def test_unset_option_behaves_like_auto(self) -> None:
        coord = _make_coord_r6()
        coord.async_fetch_live_snapshot = AsyncMock(return_value=b"\xff\xd8snap")
        cam = _with_mode(_make_camera_r6(coord=coord), None)
        cam._async_rcp_thumbnail = AsyncMock(return_value=None)
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            await BoschCamera._async_camera_image_impl(cam, width=200)
        coord.async_fetch_live_snapshot.assert_awaited_once_with(
            CAM_ID, jpeg_size=JPEG_SIZE_THUMB
        )

    @pytest.mark.asyncio
    async def test_small_forces_the_rcp_thumbnail_without_a_width(self) -> None:
        coord = _make_coord_r6()
        cam = _with_mode(_make_camera_r6(coord=coord), "small")
        cam._async_rcp_thumbnail = AsyncMock(return_value=b"\xff\xd8rcp")
        with patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ):
            out = await BoschCamera._async_camera_image_impl(cam)
        assert out == b"\xff\xd8rcp"
        coord.async_fetch_live_snapshot.assert_not_awaited()


class TestDownscaleJpeg:
    def test_shrinks_keeping_aspect_ratio(self) -> None:
        out = snap.downscale_jpeg(FULL, 320)
        assert _size(out) == (320, 180)
        assert len(out) < len(FULL)

    def test_never_upscales_and_returns_the_same_bytes(self) -> None:
        small = _jpeg(200, 100)
        assert snap.downscale_jpeg(small, 320) is small
        equal = _jpeg(320, 180)
        assert snap.downscale_jpeg(equal, 320) is equal

    def test_garbage_returns_the_original(self) -> None:
        assert snap.downscale_jpeg(b"not a jpeg", 320) == b"not a jpeg"
        assert snap.downscale_jpeg(b"", 320) == b""

    def test_missing_pillow_returns_the_original(self) -> None:
        with patch.dict("sys.modules", {"PIL": None}):
            assert snap.downscale_jpeg(FULL, 320) is FULL

    def test_tall_image_keeps_at_least_one_pixel(self) -> None:
        out = snap.downscale_jpeg(_jpeg(2000, 2), 320)
        assert _size(out) == (320, 1)


class TestLocalInterfacePath:
    @staticmethod
    def _cam(mode: str | None) -> Any:
        cam = _ldi_camera(privacy_cloud=False, passwords={CAM_ID: PW})
        cam.coordinator.async_fetch_live_snapshot = AsyncMock(return_value=FULL)
        return _with_mode(cam, mode)

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("width", WIDTHS)
    @pytest.mark.asyncio
    async def test_served_width(self, mode: str, width: int | None) -> None:
        cam = self._cam(mode)
        out = await BoschCamera.async_camera_image(cam, width=width)
        expected = {"auto": 1920, "small": 320, "medium": 640, "full": 1920}[mode]
        assert _size(out)[0] == expected  # the requested width never matters
        assert cam.cached_image == FULL  # persisted frame stays full size
        cam.coordinator.async_fetch_live_snapshot.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_auto_is_byte_identical_and_uses_no_executor(self) -> None:
        cam = self._cam("auto")
        assert await BoschCamera.async_camera_image(cam, width=200) == FULL
        assert cam.hass.calls == []

    @pytest.mark.asyncio
    async def test_resize_runs_in_the_executor_and_is_memoised(self) -> None:
        cam = self._cam("small")
        first = await BoschCamera.async_camera_image(cam)
        second = await BoschCamera.async_camera_image(cam)
        assert first is second
        assert cam.hass.calls == [snap.downscale_jpeg]  # one resize, not two

    @pytest.mark.asyncio
    async def test_size_change_or_new_frame_recomputes(self) -> None:
        cam = self._cam("small")
        await BoschCamera.async_camera_image(cam)
        cam._entry.options["snapshot_size"] = "medium"
        assert _size(await BoschCamera.async_camera_image(cam))[0] == 640
        cam.coordinator.async_fetch_live_snapshot = AsyncMock(
            return_value=_jpeg(1280, 720)
        )
        assert _size(await BoschCamera.async_camera_image(cam))[0] == 640
        assert len(cam.hass.calls) == 3

    @pytest.mark.asyncio
    async def test_one_camera_fetch_per_request_regardless_of_size(self) -> None:
        """Sizes are derived from the single shared frame — no extra session."""
        cam = self._cam("small")
        await BoschCamera.async_camera_image(cam, width=1000)
        await BoschCamera.async_camera_image(cam, width=100)
        assert cam.coordinator.async_fetch_live_snapshot.await_count == 2
        cam.coordinator.async_fetch_live_snapshot_local.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_frame_is_capped_too(self) -> None:
        cam = _ldi_camera(
            privacy_cloud=False, passwords={CAM_ID: PW}, cached_image=FULL
        )
        _with_mode(cam, "small")
        out = await BoschCamera.async_camera_image(cam)
        assert _size(out)[0] == 320
        assert cam.cached_image == FULL

    @pytest.mark.asyncio
    async def test_privacy_on_still_serves_the_placeholder(self) -> None:
        cam = _ldi_camera(privacy_cloud=True, passwords={CAM_ID: PW})
        _with_mode(cam, "small")
        out = await BoschCamera.async_camera_image(cam)
        assert out == BoschCamera._PLACEHOLDER_JPEG
        assert cam.hass.calls == []

    @pytest.mark.asyncio
    async def test_undecodable_frame_is_served_unchanged(self) -> None:
        cam = self._cam("small")
        cam.coordinator.async_fetch_live_snapshot = AsyncMock(
            return_value=b"\xff\xd8broken"
        )
        assert await BoschCamera.async_camera_image(cam) == b"\xff\xd8broken"


class TestOptionsFlowWiring:
    def test_option_lives_in_the_stream_section(self) -> None:
        from custom_components.bosch_shc_camera.config_flow import OPTIONS_SECTIONS

        assert "snapshot_size" in OPTIONS_SECTIONS["stream"]
