"""go2rtc is the only upstream reader of a local-data-interface camera.

Pins the consumers that read from the go2rtc restream: camera stream source
(fail closed without go2rtc), Mini-NVR recorder and pre-roll ring (no quality
rewrite), consumer counting for the idle reaper and health watchdog, the
Repairs issue for a missing go2rtc, and the per-tick re-registration.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator, ldi_go2rtc
from custom_components.bosch_shc_camera import recorder as nvr_recorder
from custom_components.bosch_shc_camera.repairs import (
    LDI_NO_GO2RTC_GRACE_SEC,
    refresh_local_data_interface_auth_issue,
)
from custom_components.bosch_shc_camera.stream_lifecycle import (
    go2rtc_consumer_count,
    has_active_consumer,
)
from tests.test_ldi_lifecycle import _repairs_coord
from tests.test_ldi_local import CAM, RESTREAM
from tests.test_recorder import CAM_ID_SHORT, _make_phase_coord

MODULE = "custom_components.bosch_shc_camera"
LDI_LIVE: dict[str, Any] = {
    "_connection_type": "LOCAL",
    "_ldi": True,
    "rtspsUrl": RESTREAM,
    "rtspUrl": RESTREAM,
}


# ── camera stream source ────────────────────────────────────────────────────
class TestStreamSource:
    def _camera(self, live: dict[str, Any]) -> Any:
        from custom_components.bosch_shc_camera.camera import BoschCamera

        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = SimpleNamespace(live_connections={CAM: live})
        cam._cam_id = CAM
        cam.stream_options = {}
        return cam

    @pytest.mark.asyncio
    async def test_ldi_camera_reads_the_go2rtc_restream(self) -> None:
        cam = self._camera(dict(LDI_LIVE))
        with patch(
            f"{MODULE}.camera.ensure_ldi_stream", new=AsyncMock(return_value=RESTREAM)
        ) as ensure:
            assert await cam.stream_source() == RESTREAM
        ensure.assert_awaited_once_with(cam.coordinator, CAM)
        assert cam.stream_options == {"rtsp_transport": "tcp"}

    @pytest.mark.asyncio
    async def test_missing_go2rtc_means_no_stream_never_another_url(self) -> None:
        live = dict(LDI_LIVE, rtspsUrl="rtsp://127.0.0.1:1/cloud-ish")
        cam = self._camera(live)
        with patch(
            f"{MODULE}.camera.ensure_ldi_stream", new=AsyncMock(return_value=None)
        ):
            assert await cam.stream_source() is None

    @pytest.mark.asyncio
    async def test_cloud_session_ignores_the_ldi_path(self) -> None:
        live = {
            "_connection_type": "LOCAL",
            "rtspsUrl": "rtsp://127.0.0.1:41000/rtsp_tunnel?inst=1",
        }
        cam = self._camera(live)
        with patch(f"{MODULE}.camera.ensure_ldi_stream", new=AsyncMock()) as ensure:
            assert await cam.stream_source() == live["rtspsUrl"]
        ensure.assert_not_awaited()


# ── recorder and pre-roll ring ──────────────────────────────────────────────
def _argv_input(args: tuple[Any, ...]) -> str:
    return str(args[args.index("-i") + 1])


async def _recorded_input(ldi: bool, quality: str, *, preroll: bool) -> str:
    return _argv_input(await _recorded_argv(ldi, quality, preroll=preroll))


async def _recorded_argv(ldi: bool, quality: str, *, preroll: bool) -> tuple[Any, ...]:
    coord = _make_phase_coord(
        opts={
            "nvr_base_path": "/config/bosch_nvr",
            "nvr_preroll_cache_dir": "/dev/shm/bosch_nvr_cache",
            "nvr_preroll_seconds": 30,
            "nvr_quality": quality,
        }
    )
    coord._nvr_mode_preference[CAM_ID_SHORT] = (
        "event_buffered" if preroll else "continuous"
    )
    live = coord.live_connections[CAM_ID_SHORT]
    if ldi:
        live.update(_ldi=True, rtspsUrl=RESTREAM)
        live.pop("rtspUrl", None)
    proc = MagicMock()
    proc.returncode = None
    proc.wait = AsyncMock(return_value=0)
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    spawn = AsyncMock(return_value=proc)
    with (
        patch("asyncio.create_subprocess_exec", new=spawn),
        patch.object(
            nvr_recorder,
            "_spawn_preroll_recorder_locked",
            wraps=nvr_recorder._spawn_preroll_recorder_locked,
        ),
    ):
        if preroll:
            await nvr_recorder.start_preroll_recorder(coord, CAM_ID_SHORT)
        else:
            await nvr_recorder.start_recorder(coord, CAM_ID_SHORT)
    spawn.assert_called()
    return tuple(spawn.call_args.args)


class TestRecorderQuality:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("quality", ["low", "auto", "garbage"])
    @pytest.mark.parametrize("preroll", [False, True])
    async def test_ldi_restream_url_is_never_rewritten(
        self, quality: str, preroll: bool
    ) -> None:
        assert await _recorded_input(True, quality, preroll=preroll) == RESTREAM

    @pytest.mark.asyncio
    @pytest.mark.parametrize("preroll", [False, True])
    async def test_cloud_session_low_quality_still_rewrites(
        self, preroll: bool
    ) -> None:
        url = await _recorded_input(False, "low", preroll=preroll)
        assert "inst=4" in url and "inst=1" not in url

    @pytest.mark.asyncio
    async def test_cloud_session_auto_unchanged(self) -> None:
        assert "inst=1" in await _recorded_input(False, "auto", preroll=False)


class TestRecorderAudio:
    """The restream now carries audio: record one video + the audio track."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("preroll", [False, True])
    async def test_ldi_records_first_video_and_optional_audio_track(
        self, preroll: bool
    ) -> None:
        argv = await _recorded_argv(True, "auto", preroll=preroll)
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        assert maps == ["0:v:0", "0:a:0?"]
        assert argv[argv.index("-c") + 1] == "copy"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("preroll", [False, True])
    async def test_cloud_session_keeps_map_all(self, preroll: bool) -> None:
        argv = await _recorded_argv(False, "auto", preroll=preroll)
        maps = [argv[i + 1] for i, a in enumerate(argv) if a == "-map"]
        assert maps == ["0"]


# ── consumer counting ───────────────────────────────────────────────────────
class TestConsumerCount:
    @pytest.mark.asyncio
    async def test_ldi_counts_on_its_own_stream_via_core_go2rtc(self) -> None:
        c = SimpleNamespace(live_connections={CAM: dict(LDI_LIVE)})
        ep = MagicMock()
        with (
            patch(
                f"{MODULE}.stream_lifecycle.resolve_endpoint",
                new=AsyncMock(return_value=ep),
            ),
            patch(
                f"{MODULE}.stream_lifecycle.consumer_count",
                new=AsyncMock(return_value=3),
            ) as count,
        ):
            assert await go2rtc_consumer_count(c, CAM) == 3  # type: ignore[arg-type]
        count.assert_awaited_once_with(ep, ldi_go2rtc.ldi_stream_name(CAM))

    @pytest.mark.asyncio
    async def test_ldi_without_go2rtc_is_unknown(self) -> None:
        c = SimpleNamespace(live_connections={CAM: dict(LDI_LIVE)})
        with patch(
            f"{MODULE}.stream_lifecycle.resolve_endpoint",
            new=AsyncMock(return_value=None),
        ):
            assert await go2rtc_consumer_count(c, CAM) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_cloud_session_keeps_the_entity_stream_count(self) -> None:
        c = SimpleNamespace(
            live_connections={CAM: {"_connection_type": "LOCAL"}}, camera_entities={}
        )
        with (
            patch(f"{MODULE}.stream_lifecycle.resolve_endpoint", new=AsyncMock()) as ep,
            patch(
                f"{MODULE}.stream_lifecycle._go2rtc_client_session",
                side_effect=RuntimeError("closed"),
            ),
        ):
            assert await go2rtc_consumer_count(c, CAM) is None  # type: ignore[arg-type]
        ep.assert_not_awaited()


class TestIdleSemantics:
    """Reaper and watchdog read the go2rtc count of the camera's own stream,
    which includes the recorder and the external-recorder endpoint."""

    def _coord(self, count: int | None) -> SimpleNamespace:
        return SimpleNamespace(
            nvr_processes={},
            nvr_preroll_processes={},
            camera_entities={},
            frigate_runner=None,
            go2rtc_consumer_count=AsyncMock(return_value=count),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("count", "active"), [(0, False), (1, True), (4, True), (None, True)]
    )
    async def test_active_only_when_go2rtc_reports_a_reader(
        self, count: int | None, active: bool
    ) -> None:
        assert await has_active_consumer(self._coord(count), CAM) is active  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_recorder_keeps_the_session_even_at_zero_readers(self) -> None:
        c = self._coord(0)
        c.nvr_preroll_processes = {CAM: object()}
        assert await has_active_consumer(c, CAM) is True  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_external_recorder_keeps_the_session(self) -> None:
        c = self._coord(0)
        c.frigate_runner = SimpleNamespace(active_count=lambda _c: 1)
        assert await has_active_consumer(c, CAM) is True  # type: ignore[arg-type]


# ── Repairs issue ───────────────────────────────────────────────────────────
class TestNoGo2rtcIssue:
    @patch(f"{MODULE}.ir")
    def test_raised_after_grace(self, ir: MagicMock) -> None:
        old = time.monotonic() - LDI_NO_GO2RTC_GRACE_SEC - 5
        c = _repairs_coord(status={"reason": "no_go2rtc", "since": old})
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        kw = ir.async_create_issue.call_args.kwargs
        assert kw["translation_key"] == "local_data_interface_no_go2rtc"
        assert kw["is_fixable"] is False
        assert kw["translation_placeholders"] == {"camera": "Terrasse"}

    @patch(f"{MODULE}.ir")
    def test_a_go2rtc_restart_blip_is_not_raised(self, ir: MagicMock) -> None:
        c = _repairs_coord(status={"reason": "no_go2rtc", "since": time.monotonic()})
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_privacy_never_raises_it(self, ir: MagicMock) -> None:
        old = time.monotonic() - 10 * LDI_NO_GO2RTC_GRACE_SEC
        c = _repairs_coord(status={"reason": "no_go2rtc", "since": old}, privacy=True)
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_cleared_once_the_status_is_gone(self, ir: MagicMock) -> None:
        c = _repairs_coord(status=None)
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_delete_issue.assert_called_once()

    def test_translated_everywhere(self) -> None:
        import json
        import pathlib

        base = pathlib.Path(MODULE.replace(".", "/"))
        for path in [
            base / "strings.json",
            *sorted((base / "translations").glob("*.json")),
        ]:
            issue = json.loads(path.read_text(encoding="utf-8"))["issues"][
                "local_data_interface_no_go2rtc"
            ]
            assert issue["title"] and "{camera}" in issue["description"], path.name


# ── per-tick re-registration ────────────────────────────────────────────────
class TestTickReRegistration:
    def test_spawns_for_local_only_sessions_only(self) -> None:
        c = MagicMock()
        c.live_connections = {
            CAM: dict(LDI_LIVE),
            "other": {"_connection_type": "LOCAL"},
        }
        ensured: list[str] = []

        def _ensure(_c: Any, cam_id: str) -> Any:
            ensured.append(cam_id)
            return asyncio.sleep(0)

        with patch(
            f"{MODULE}.coordinator.ensure_ldi_stream",
            new=MagicMock(side_effect=_ensure),
        ):
            BoschCameraCoordinator._ensure_ldi_go2rtc_streams(c)  # type: ignore[arg-type]
        assert ensured == [CAM]
        assert c.spawn_tracked.call_count == 1
        for call in c.spawn_tracked.call_args_list:
            call.args[0].close()

    def test_no_sessions_no_tasks(self) -> None:
        c = MagicMock()
        c.live_connections = {}
        BoschCameraCoordinator._ensure_ldi_go2rtc_streams(c)  # type: ignore[arg-type]
        c.spawn_tracked.assert_not_called()
