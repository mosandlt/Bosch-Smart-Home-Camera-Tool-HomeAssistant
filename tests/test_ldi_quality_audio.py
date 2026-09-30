"""Stream URL, quality and audio of a local-data-interface camera.

The go2rtc source is `rtsp_tunnel?line=1&inst=<N>&enableaudio=<0|1>`: inst 1
is the high and 2 the low stream (the camera's quality select), audio is
always requested like on the cloud path. A changed quality replaces the
registered source. The external recorder's low switch reads its own `_low`
go2rtc stream; the recorder records one video and one audio track.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import frigate_endpoint as fe
from custom_components.bosch_shc_camera import ldi_go2rtc, ldi_local, recorder
from custom_components.bosch_shc_camera.frigate_endpoint import (
    FrontDoorRunner,
    _resolve_restream,
)
from custom_components.bosch_shc_camera.remote_viewing_front_door import RemoteTarget
from tests.test_ldi_go2rtc import API, FakeGo2rtc
from tests.test_ldi_local import CAM, IP, PW, RESTREAM, _coord, _open_patches

LDI = "custom_components.bosch_shc_camera.ldi_local"
BASE = f"rtsps://localuser:{PW}@{IP}:9554/rtsp_tunnel"


def _with_go2rtc(fake: FakeGo2rtc, quality: str = "auto") -> SimpleNamespace:
    c = _coord()
    c.hass = SimpleNamespace(data={"go2rtc": SimpleNamespace(url=API, session=fake)})
    c.get_quality = lambda _cid: quality  # type: ignore[assignment]
    return c


class TestSourceUrl:
    @pytest.mark.parametrize("inst", [1, 2, 3])
    @pytest.mark.parametrize("audio", [True, False])
    def test_every_inst_and_audio_mode(self, inst: int, audio: bool) -> None:
        url = ldi_local.ldi_source_url(IP, "localuser", PW, inst=inst, audio=audio)
        assert url == f"{BASE}?line=1&inst={inst}&enableaudio={int(audio)}"

    def test_defaults_are_high_with_audio(self) -> None:
        assert ldi_local.ldi_source_url(IP, "localuser", PW).endswith(
            "?line=1&inst=1&enableaudio=1"
        )

    def test_the_old_live_path_is_gone(self) -> None:
        assert "/live" not in ldi_local.ldi_source_url(IP, "localuser", PW)
        assert ldi_local.LDI_STREAM_PATH == "/rtsp_tunnel"

    @pytest.mark.parametrize(
        ("quality", "inst"),
        [("high", 1), ("auto", 1), ("low", 2), ("garbage", 1), (None, 1), (3, 1)],
    )
    def test_quality_to_inst(self, quality: object, inst: int) -> None:
        assert ldi_local.ldi_inst_for_quality(quality) == inst


class TestMainStream:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("quality", "inst"),
        [("high", 1), ("auto", 1), ("low", 2), ("garbage", 1)],
    )
    async def test_source_follows_the_quality_select_with_audio(
        self, quality: str, inst: int
    ) -> None:
        c = _coord()
        c.get_quality = lambda _cid: quality  # type: ignore[assignment]
        with patch(
            f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=RESTREAM)
        ) as ensure:
            assert await ldi_local.ensure_ldi_stream(c, CAM) == RESTREAM  # type: ignore[arg-type]
        ensure.assert_awaited_once_with(
            c, CAM, f"{BASE}?line=1&inst={inst}&enableaudio=1"
        )

    @pytest.mark.asyncio
    async def test_coordinator_without_a_quality_getter_means_high(self) -> None:
        c = _coord()
        del c.get_quality
        with patch(
            f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=RESTREAM)
        ) as ensure:
            await ldi_local.ensure_ldi_stream(c, CAM)  # type: ignore[arg-type]
        assert "inst=1&" in ensure.await_args.args[2]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("quality", "inst"), [("high", 1), ("low", 2), ("auto", 1)]
    )
    async def test_open_registers_the_quality_and_reports_it(
        self, quality: str, inst: int
    ) -> None:
        c = _coord()
        c.get_quality = lambda _cid: quality  # type: ignore[assignment]
        probe, ensure = _open_patches()
        with probe, ensure as ensure_mock:
            res = await ldi_local.open_ldi_connection(c, CAM, (IP, "localuser", PW))  # type: ignore[arg-type]
        assert res is not None
        assert ensure_mock.await_args.args[2] == (
            f"{BASE}?line=1&inst={inst}&enableaudio=1"
        )
        # cloud-equivalent id (1 high, 4 low): never the "clamped to 2" marker
        assert c._quality_effective_inst[CAM] == (4 if quality == "low" else 1)

    @pytest.mark.asyncio
    async def test_changed_quality_replaces_the_registered_source(self) -> None:
        fake = FakeGo2rtc()
        quality = {"q": "high"}
        c = _with_go2rtc(fake)
        c.get_quality = lambda _cid: quality["q"]  # type: ignore[assignment]
        assert await ldi_local.ensure_ldi_stream(c, CAM) == RESTREAM  # type: ignore[arg-type]
        assert fake.streams["ldi_11111111"] == [f"{BASE}?line=1&inst=1&enableaudio=1"]
        quality["q"] = "low"
        assert await ldi_local.ensure_ldi_stream(c, CAM) == RESTREAM  # type: ignore[arg-type]
        # replaced, not added to: exactly one producer, now the low stream
        assert fake.streams["ldi_11111111"] == [f"{BASE}?line=1&inst=2&enableaudio=1"]
        assert len(fake.puts) == 2

    @pytest.mark.asyncio
    async def test_unchanged_quality_is_not_re_registered(self) -> None:
        fake = FakeGo2rtc()
        c = _with_go2rtc(fake)
        await ldi_local.ensure_ldi_stream(c, CAM)  # type: ignore[arg-type]
        await ldi_local.ensure_ldi_stream(c, CAM)  # type: ignore[arg-type]
        assert len(fake.puts) == 1


class TestLowVariant:
    @pytest.mark.asyncio
    async def test_low_has_its_own_go2rtc_stream_and_leaves_the_main_one(
        self,
    ) -> None:
        fake = FakeGo2rtc()
        c = _with_go2rtc(fake, "high")
        main = await ldi_local.ensure_ldi_stream(c, CAM)  # type: ignore[arg-type]
        low = await ldi_local.ensure_ldi_stream(c, CAM, low=True)  # type: ignore[arg-type]
        assert main == "rtsp://127.0.0.1:18554/ldi_11111111"
        assert low == "rtsp://127.0.0.1:18554/ldi_11111111_low"
        assert fake.streams["ldi_11111111"] == [f"{BASE}?line=1&inst=1&enableaudio=1"]
        assert fake.streams["ldi_11111111_low"] == [
            f"{BASE}?line=1&inst=2&enableaudio=1"
        ]

    @pytest.mark.asyncio
    async def test_high_users_keep_a_single_upstream(self) -> None:
        fake = FakeGo2rtc()
        c = _with_go2rtc(fake, "high")
        await ldi_local.ensure_ldi_stream(c, CAM)  # type: ignore[arg-type]
        assert list(fake.streams) == ["ldi_11111111"]

    @pytest.mark.asyncio
    async def test_main_already_low_is_reused_for_the_low_switch(self) -> None:
        fake = FakeGo2rtc()
        c = _with_go2rtc(fake, "low")
        url = await ldi_local.ensure_ldi_stream(c, CAM, low=True)  # type: ignore[arg-type]
        assert url == "rtsp://127.0.0.1:18554/ldi_11111111"
        assert list(fake.streams) == ["ldi_11111111"]

    @pytest.mark.asyncio
    async def test_low_without_go2rtc_fails_closed(self) -> None:
        c = _coord()
        with patch(f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=None)):
            assert await ldi_local.ensure_ldi_stream(c, CAM, low=True) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_unregister_removes_the_variants_too(self) -> None:
        fake = FakeGo2rtc()
        c = _with_go2rtc(fake, "high")
        await ldi_local.ensure_ldi_stream(c, CAM)  # type: ignore[arg-type]
        await ldi_local.ensure_ldi_stream(c, CAM, low=True)  # type: ignore[arg-type]
        await ldi_go2rtc.unregister_stream(c, CAM)  # type: ignore[arg-type]
        assert fake.streams == {}
        assert CAM not in c.ldi_go2rtc_state

    @pytest.mark.asyncio
    async def test_leftover_sweep_spares_claimed_variants(self) -> None:
        fake = FakeGo2rtc()
        c = _with_go2rtc(fake, "high")
        c.hass.config_entries = None
        await ldi_local.ensure_ldi_stream(c, CAM, low=True)  # type: ignore[arg-type]
        fake.streams["ldi_deadbeef_low"] = ["x"]
        await ldi_go2rtc.remove_leftovers(c, set())  # type: ignore[arg-type]
        assert "ldi_11111111_low" in fake.streams
        assert "ldi_deadbeef_low" not in fake.streams

    def test_variant_names(self) -> None:
        assert ldi_go2rtc.ldi_stream_name(CAM, "low") == "ldi_11111111_low"
        assert ldi_go2rtc.ldi_stream_name(CAM, "snap") == "ldi_11111111_snap"
        assert ldi_go2rtc.ldi_stream_name(CAM) == "ldi_11111111"

    def test_hand_built_state_without_variants_still_works(self) -> None:
        c = SimpleNamespace(ldi_go2rtc_state={CAM: {"lock": asyncio.Lock()}})
        sub = ldi_go2rtc._state(c, CAM, "low")  # type: ignore[arg-type]
        assert sub["fp"] is None
        assert c.ldi_go2rtc_state[CAM]["variants"]["low"] is sub


class TestFrigateQuality:
    def _live(self) -> SimpleNamespace:
        c = _coord()
        c.live_connections = {CAM: {"_connection_type": "LOCAL", "_ldi": True}}
        return c

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("uri", "low"),
        [
            ("rtsp://h:1/rtsp_tunnel?inst=2&enableaudio=1", True),
            ("rtsp://h:1/tok/rtsp_tunnel?inst=2", True),
            ("rtsp://h:1/rtsp_tunnel?inst=1&enableaudio=1", False),
            ("rtsp://h:1/rtsp_tunnel?enableaudio=1&inst=1", False),
            ("rtsp://h:1/rtsp_tunnel?inst=3", False),
            ("rtsp://h:1/rtsp_tunnel", False),
            ("", False),
        ],
    )
    async def test_inst_in_the_request_picks_the_stream(
        self, uri: str, low: bool
    ) -> None:
        token = fe._REQUEST_URI.set(uri)
        try:
            with patch.object(
                fe, "ensure_ldi_stream", new=AsyncMock(return_value=RESTREAM)
            ) as ensure:
                target = await _resolve_restream(self._live(), CAM)
        finally:
            fe._REQUEST_URI.reset(token)
        ensure.assert_awaited_once()
        assert ensure.await_args.kwargs == {"low": low}
        assert target == RemoteTarget(port=18554, path="/ldi_11111111", keep_track=True)

    @pytest.mark.asyncio
    async def test_low_target_points_at_the_low_stream(self) -> None:
        token = fe._REQUEST_URI.set("rtsp://h:1/rtsp_tunnel?inst=2")
        try:
            with patch.object(
                fe,
                "ensure_ldi_stream",
                new=AsyncMock(return_value=RESTREAM + "_low"),
            ):
                target = await _resolve_restream(self._live(), CAM)
        finally:
            fe._REQUEST_URI.reset(token)
        assert target.path == "/ldi_11111111_low"

    @pytest.mark.asyncio
    async def test_front_door_hands_the_request_uri_to_the_resolver(
        self, socket_enabled: None
    ) -> None:
        seen: list[str] = []

        async def resolve(_cam: str) -> None:
            seen.append(fe._REQUEST_URI.get())

        runner = FrontDoorRunner()
        try:
            port = await runner.start_server("camAAAAAA", fe.FrontDoorConfig(), resolve)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel?inst=2&enableaudio=1 "
                b"RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            )
            await writer.drain()
            reply = await asyncio.wait_for(reader.read(4096), timeout=5)
            writer.close()
        finally:
            runner.stop_all()
        assert reply.startswith(b"RTSP/1.0 503")
        assert seen == ["rtsp://127.0.0.1/rtsp_tunnel?inst=2&enableaudio=1"]


class TestRecorderTracks:
    _ARGS: ClassVar[list[str]] = [
        "ffmpeg",
        "-i",
        "u",
        "-map",
        "0",
        "-c",
        "copy",
        "-f",
        "segment",
        "x",
    ]

    def test_map_is_one_video_and_an_optional_audio_track(self) -> None:
        out = recorder._single_av_tracks(list(self._ARGS))
        assert out == [
            "ffmpeg", "-i", "u", "-map", "0:v:0", "-map", "0:a:0?",
            "-c", "copy", "-f", "segment", "x",
        ]  # fmt: skip

    def test_other_maps_and_a_dangling_map_are_left_alone(self) -> None:
        args = ["-map", "0:v", "-i", "u", "-map"]
        assert recorder._single_av_tracks(args) == args

    def test_real_argv_builders_contain_the_replaced_map(self) -> None:
        for args in (
            recorder._build_ffmpeg_args(RESTREAM, "/x/%H.mp4"),
            recorder._build_preroll_ffmpeg_args(RESTREAM, "/x/%H.mp4"),
        ):
            out = recorder._single_av_tracks(args)
            assert "-map" in out and out.count("-map") == 2
            assert out[out.index("-map") + 1] == "0:v:0"
            assert "0:a:0?" in out
            assert "-c" in out and out[out.index("-c") + 1] == "copy"
