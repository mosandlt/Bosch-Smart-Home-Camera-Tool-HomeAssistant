"""go2rtc as the single upstream reader of a local-data-interface camera.

Runs the real registration code against an in-process go2rtc stand-in (an
session stand-in speaking the same /api and /api/streams surface, including the
HTTP 400 "yaml:" answer go2rtc gives when its config file is not writable).
Pins: register ok / 400-yaml soft success / idempotence / restart re-register /
changed source replaced, not added / go2rtc missing or unreachable, consumer
counting, unregister, leftover cleanup, log redaction.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from yarl import URL

from custom_components.bosch_shc_camera import ldi_go2rtc
from custom_components.bosch_shc_camera.go2rtc_client import unregister_go2rtc_stream

CAM = "11111111-1111-1111-1111-111111111111"
SRC = "rtsps://localuser:test-pw@10.0.0.50:9554/live"
NAME = "ldi_11111111"


class _Resp:
    def __init__(self, status: int, body: Any = None, text: str = "") -> None:
        self.status = status
        self._body = body
        self._text = text

    async def __aenter__(self) -> _Resp:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def json(self, content_type: str | None = None) -> Any:
        return self._body

    async def text(self) -> str:
        return self._text


class FakeGo2rtc:
    """Minimal go2rtc: application info, stream add/list/remove.

    Doubles as the aiohttp session core hands out (`get`/`put`/`delete`).
    """

    def __init__(self) -> None:
        self.streams: dict[str, list[str]] = {}
        self.puts: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.info: Any = {"rtsp": {"listen": "127.0.0.1:18554"}, "version": "1.9.12"}
        self.info_status = 200
        self.put_status = 200
        self.put_body = ""
        self.put_stores = True
        self.get_status = 200
        self.mask_urls = False
        self.consumers: dict[str, list[Any]] = {}
        self.gets = 0
        self.stream_body: Any = None
        self.listing_body: Any = None
        self.down = False
        self.session: Any = self

    def _check(self) -> None:
        if self.down:
            raise aiohttp.ClientConnectionError("down")

    def get(self, url: URL, params: dict[str, str] | None = None) -> _Resp:
        self._check()
        if url.path == "/api":
            return _Resp(self.info_status, self.info)
        self.gets += 1
        if self.get_status != 200:
            return _Resp(self.get_status)
        name = (params or {}).get("src")
        if name is None:
            if self.listing_body is not None:
                return _Resp(200, self.listing_body)
            return _Resp(
                200,
                {
                    n: {"producers": [{"url": u} for u in urls]}
                    for n, urls in self.streams.items()
                },
            )
        if self.stream_body is not None:
            return _Resp(200, self.stream_body)
        if name not in self.streams:
            return _Resp(404)
        urls = [
            "rtsps://***@10.0.0.50:9554/live" if self.mask_urls else u
            for u in self.streams[name]
        ]
        return _Resp(
            200,
            {
                "producers": [{"url": u} for u in urls],
                "consumers": self.consumers.get(name, []),
            },
        )

    def put(self, url: URL, params: dict[str, str]) -> _Resp:
        self._check()
        name, src = params["name"], params["src"]
        self.puts.append((name, src))
        if self.put_stores:
            self.streams.setdefault(name, []).append(src)
        return _Resp(self.put_status, text=self.put_body)

    def delete(self, url: URL, params: dict[str, str]) -> _Resp:
        self._check()
        self.deletes.append(params["name"])
        self.streams.pop(params["name"], None)
        return _Resp(200)


@pytest.fixture
def go2rtc() -> FakeGo2rtc:
    return FakeGo2rtc()


@pytest.fixture
def session(go2rtc: FakeGo2rtc) -> Any:
    return go2rtc.session


API = "http://localhost:11984/"


def _coord(fake: FakeGo2rtc | None, session: Any = None) -> SimpleNamespace:
    data: dict[str, Any] = {}
    if fake is not None:
        data["go2rtc"] = SimpleNamespace(
            url=API, session=session if session is not None else fake
        )
    return SimpleNamespace(hass=SimpleNamespace(data=data))


async def _endpoint(fake: FakeGo2rtc, session: Any = None) -> ldi_go2rtc.Go2rtcEndpoint:
    return ldi_go2rtc.Go2rtcEndpoint(
        session=session or fake, api=URL(API), rtsp_port=18554
    )


# ── pure helpers ────────────────────────────────────────────────────────────
class TestHelpers:
    def test_stream_name_uses_first_eight_lowercase(self) -> None:
        assert ldi_go2rtc.ldi_stream_name("AABBCCDD-1111-2222-3333-444444444444") == (
            "ldi_aabbccdd"
        )
        assert ldi_go2rtc.ldi_stream_name(CAM) == NAME

    def test_stream_name_strips_unsafe_characters(self) -> None:
        assert ldi_go2rtc.ldi_stream_name("ab:/?#cdef") == "ldi_abcd"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            (
                "src rtsps://localuser:test-pw@10.0.0.50:9554/live failed",
                "src rtsps://***@10.0.0.50:9554/live failed",
            ),
            (
                "GET /api/streams?name=x&src=rtsps%3A%2F%2Flocaluser%3Atest-pw%40"
                "10.0.0.50%3A9554%2Flive",
                "GET /api/streams?name=x&src=rtsps%3A%2F%2F***@10.0.0.50%3A9554%2Flive",
            ),
            ("rtsp://127.0.0.1:18554/ldi_1", "rtsp://127.0.0.1:18554/ldi_1"),
            ("no url at all", "no url at all"),
        ],
    )
    def test_redact_urls(self, text: str, expected: str) -> None:
        out = ldi_go2rtc.redact_urls(text)
        assert out == expected
        assert "test-pw" not in out

    @pytest.mark.parametrize(
        ("info", "port"),
        [
            ({"rtsp": {"listen": ":8554"}}, 8554),
            ({"rtsp": {"listen": "127.0.0.1:18554"}}, 18554),
            ({"rtsp": {"listen": "[::1]:9000"}}, 9000),
            ({"rtsp": {"listen": "nonsense"}}, None),
            ({"rtsp": {"listen": ":0"}}, None),
            ({"rtsp": {"listen": ":70000"}}, None),
            ({"rtsp": {"listen": 8554}}, None),
            ({"rtsp": "x"}, None),
            ({}, None),
            ([], None),
            (None, None),
        ],
    )
    def test_listen_port(self, info: object, port: int | None) -> None:
        assert ldi_go2rtc._listen_port(info) == port

    def test_restream_url_is_credential_free(self) -> None:
        ep = ldi_go2rtc.Go2rtcEndpoint(
            session=MagicMock(), api=URL("http://localhost:1/"), rtsp_port=18554
        )
        assert ep.restream_url(NAME) == "rtsp://127.0.0.1:18554/ldi_11111111"


# ── endpoint discovery ──────────────────────────────────────────────────────
class TestResolveEndpoint:
    @pytest.mark.asyncio
    async def test_uses_core_session_and_reported_rtsp_port(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.info = {"rtsp": {"listen": ":8554"}}
        ep = await ldi_go2rtc.resolve_endpoint(_coord(server, session))  # type: ignore[arg-type]
        assert ep is not None
        assert ep.session is session
        assert ep.rtsp_port == 8554

    @pytest.mark.asyncio
    async def test_no_core_go2rtc(self) -> None:
        assert await ldi_go2rtc.resolve_endpoint(_coord(None, None)) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_config_without_session(self, go2rtc: FakeGo2rtc) -> None:
        coord = SimpleNamespace(
            hass=SimpleNamespace(
                data={"go2rtc": SimpleNamespace(url=API, session=None)}
            )
        )
        assert await ldi_go2rtc.resolve_endpoint(coord) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_remote_go2rtc_rejected(self) -> None:
        coord = SimpleNamespace(
            hass=SimpleNamespace(
                data={
                    "go2rtc": SimpleNamespace(
                        url="http://192.0.2.9:1984/", session=MagicMock()
                    )
                }
            )
        )
        assert await ldi_go2rtc.resolve_endpoint(coord) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_info_error_status(self, go2rtc: FakeGo2rtc, session: Any) -> None:
        fake = server = go2rtc
        fake.info_status = 403
        assert await ldi_go2rtc.resolve_endpoint(_coord(server, session)) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_rtsp_module_missing(self, go2rtc: FakeGo2rtc, session: Any) -> None:
        fake = server = go2rtc
        fake.info = {"version": "1.9.12"}
        assert await ldi_go2rtc.resolve_endpoint(_coord(server, session)) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_unreachable(self) -> None:
        dead = FakeGo2rtc()
        dead.down = True
        coord = _coord(dead)
        assert await ldi_go2rtc.resolve_endpoint(coord) is None  # type: ignore[arg-type]


# ── registration ────────────────────────────────────────────────────────────
class TestRegister:
    @pytest.mark.asyncio
    async def test_registers_new_stream(self, go2rtc: FakeGo2rtc, session: Any) -> None:
        fake = server = go2rtc
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is True
        assert fake.puts == [(NAME, SRC)]
        assert fake.streams[NAME] == [SRC]

    @pytest.mark.asyncio
    async def test_yaml_400_is_in_memory_success(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.put_status = 400
        fake.put_body = "yaml: unmarshal errors"
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is True

    @pytest.mark.asyncio
    async def test_other_400_is_failure(self, go2rtc: FakeGo2rtc, session: Any) -> None:
        fake = server = go2rtc
        fake.put_status = 400
        fake.put_body = "bad request"
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is False

    @pytest.mark.asyncio
    async def test_server_error_on_put_is_failure(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.put_status = 500
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is False

    @pytest.mark.asyncio
    async def test_put_accepted_but_no_producer_is_failure(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.put_stores = False
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is False

    @pytest.mark.asyncio
    async def test_idempotent_when_source_matches(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        ep = await _endpoint(server, session)
        await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is True
        assert len(fake.puts) == 1
        assert fake.deletes == []

    @pytest.mark.asyncio
    async def test_known_source_trusted_when_go2rtc_masks_credentials(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        ep = await _endpoint(server, session)
        await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False)
        fake.mask_urls = True
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=True) is True
        assert len(fake.puts) == 1
        assert fake.deletes == []

    @pytest.mark.asyncio
    async def test_changed_source_is_replaced_not_added(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        ep = await _endpoint(server, session)
        await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False)
        new = "rtsps://localuser:other@10.0.0.50:9554/live"
        assert await ldi_go2rtc.register_stream(ep, NAME, new, known=False) is True
        assert fake.deletes == [NAME]
        assert fake.streams[NAME] == [new]

    @pytest.mark.asyncio
    async def test_password_with_reserved_characters_arrives_intact(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        ep = await _endpoint(server, session)
        src = "rtsps://localuser:p%40ss%3Aw%2F%25rd@10.0.0.50:9554/live"
        assert await ldi_go2rtc.register_stream(ep, NAME, src, known=False) is True
        assert fake.streams[NAME] == [src]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [500, 403])
    async def test_unreadable_state_is_failure_without_writes(
        self,
        go2rtc: FakeGo2rtc,
        session: Any,
        status: int,
    ) -> None:
        fake = server = go2rtc
        fake.get_status = status
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is False
        assert fake.puts == []

    @pytest.mark.asyncio
    async def test_non_dict_state_is_failure(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.stream_body = ["nope"]
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is False

    @pytest.mark.asyncio
    async def test_state_without_producer_list_counts_as_absent(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.stream_body = {"producers": None}
        ep = await _endpoint(server, session)
        assert await ldi_go2rtc._producers(ep, NAME) == []

    @pytest.mark.asyncio
    async def test_unreachable_go2rtc_is_failure(self) -> None:
        dead = FakeGo2rtc()
        dead.down = True
        ep = await _endpoint(dead)
        assert await ldi_go2rtc.register_stream(ep, NAME, SRC, known=False) is False
        assert await ldi_go2rtc._delete(ep, NAME) is False
        assert await ldi_go2rtc._put(ep, NAME, SRC) is False
        assert await ldi_go2rtc.consumer_count(ep, NAME) is None


# ── consumers ───────────────────────────────────────────────────────────────
class TestConsumerCount:
    @pytest.mark.asyncio
    async def test_counts_readers(self, go2rtc: FakeGo2rtc, session: Any) -> None:
        fake = server = go2rtc
        ep = await _endpoint(server, session)
        fake.streams[NAME] = [SRC]
        fake.consumers[NAME] = [{"a": 1}, {"b": 2}, {"c": 3}]
        assert await ldi_go2rtc.consumer_count(ep, NAME) == 3

    @pytest.mark.asyncio
    async def test_unknown_stream_has_no_consumers(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        _fake = server = go2rtc
        assert (
            await ldi_go2rtc.consumer_count(await _endpoint(server, session), NAME) == 0
        )

    @pytest.mark.asyncio
    async def test_server_error_is_unknown(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.get_status = 500
        assert (
            await ldi_go2rtc.consumer_count(await _endpoint(server, session), NAME)
            is None
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [["x"], {"consumers": None}, {}])
    async def test_odd_bodies_count_zero(
        self,
        go2rtc: FakeGo2rtc,
        session: Any,
        body: object,
    ) -> None:
        fake = server = go2rtc
        fake.stream_body = body
        assert (
            await ldi_go2rtc.consumer_count(await _endpoint(server, session), NAME) == 0
        )


# ── ensure_stream: registration lifecycle ───────────────────────────────────
class TestEnsureStream:
    @pytest.mark.asyncio
    async def test_registers_and_returns_restream_url(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        url = await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        assert url == "rtsp://127.0.0.1:18554/ldi_11111111"
        assert "localuser" not in url
        assert fake.streams[NAME] == [SRC]

    @pytest.mark.asyncio
    async def test_fresh_verification_is_reused(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        gets = fake.gets
        assert await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        assert fake.gets == gets

    @pytest.mark.asyncio
    async def test_go2rtc_restart_reregisters(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        fake.streams.clear()  # go2rtc restarted, registry lost
        coord.ldi_go2rtc_state[CAM]["verified_at"] = float("-inf")
        assert await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        assert fake.streams[NAME] == [SRC]
        assert len(fake.puts) == 2

    @pytest.mark.asyncio
    async def test_unchanged_registration_is_not_rewritten(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        fake.mask_urls = True
        coord.ldi_go2rtc_state[CAM]["verified_at"] = float("-inf")
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        assert len(fake.puts) == 1
        assert fake.deletes == []

    @pytest.mark.asyncio
    async def test_changed_password_replaces_source(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        new = "rtsps://localuser:rotated@10.0.0.50:9554/live"
        assert await ldi_go2rtc.ensure_stream(coord, CAM, new, force=True)  # type: ignore[arg-type]
        assert fake.streams[NAME] == [new]

    @pytest.mark.asyncio
    async def test_missing_go2rtc_fails_closed_with_backoff(self) -> None:
        coord = _coord(None, None)
        assert await ldi_go2rtc.ensure_stream(coord, CAM, SRC) is None  # type: ignore[arg-type]
        st = coord.ldi_go2rtc_state[CAM]
        assert st["fails"] == 1
        assert st["retry_at"] > 0
        with patch.object(
            ldi_go2rtc, "resolve_endpoint", new=AsyncMock(side_effect=AssertionError)
        ):
            # inside the backoff window go2rtc is not even asked
            assert await ldi_go2rtc.ensure_stream(coord, CAM, SRC) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_force_bypasses_backoff_and_backoff_is_bounded(
        self, go2rtc: FakeGo2rtc
    ) -> None:
        coord = _coord(None, None)
        for _ in range(9):
            await ldi_go2rtc.ensure_stream(coord, CAM, SRC, force=True)  # type: ignore[arg-type]
        st = coord.ldi_go2rtc_state[CAM]
        assert st["fails"] == 9
        import time

        assert st["retry_at"] - time.monotonic() <= ldi_go2rtc._BACKOFF_MAX_SEC
        coord.hass.data["go2rtc"] = SimpleNamespace(url=API, session=go2rtc)
        assert await ldi_go2rtc.ensure_stream(coord, CAM, SRC, force=True)  # type: ignore[arg-type]
        assert st["fails"] == 0

    @pytest.mark.asyncio
    async def test_no_password_in_logs(
        self,
        go2rtc: FakeGo2rtc,
        session: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        fake = server = go2rtc
        fake.put_status = 500
        coord = _coord(server, session)
        with caplog.at_level(logging.DEBUG):
            await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
            await ldi_go2rtc.ensure_stream(coord, CAM, SRC, force=True)  # type: ignore[arg-type]
        assert "test-pw" not in caplog.text
        assert "test-pw" not in repr(coord.ldi_go2rtc_state)

    @pytest.mark.asyncio
    async def test_fail_counter_is_capped_so_backoff_never_overflows(self) -> None:
        """2.0 ** fails raises OverflowError past ~1023 failures."""
        coord = _coord(None, None)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC, force=True)  # type: ignore[arg-type]
        coord.ldi_go2rtc_state[CAM]["fails"] = 5000
        assert await ldi_go2rtc.ensure_stream(coord, CAM, SRC, force=True) is None  # type: ignore[arg-type]
        assert coord.ldi_go2rtc_state[CAM]["fails"] == ldi_go2rtc._MAX_FAILS

    @pytest.mark.asyncio
    async def test_changed_source_within_fresh_window_is_reregistered(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        coord = _coord(go2rtc, session)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        new_src = SRC.replace("test-pw", "other-pw")
        await ldi_go2rtc.ensure_stream(coord, CAM, new_src)  # type: ignore[arg-type]
        assert go2rtc.streams[NAME] == [new_src]


# ── teardown ────────────────────────────────────────────────────────────────
class TestUnregister:
    @pytest.mark.asyncio
    async def test_removes_registered_stream(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        await ldi_go2rtc.unregister_stream(coord, CAM)  # type: ignore[arg-type]
        assert fake.deletes == [NAME]
        assert NAME not in fake.streams
        assert CAM not in coord.ldi_go2rtc_state

    @pytest.mark.asyncio
    async def test_unregister_waits_for_in_flight_registration(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        coord = _coord(go2rtc, session)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        lock = coord.ldi_go2rtc_state[CAM]["lock"]
        await lock.acquire()
        task = asyncio.ensure_future(ldi_go2rtc.unregister_stream(coord, CAM))  # type: ignore[arg-type]
        await asyncio.sleep(0)
        assert not task.done()
        assert CAM in coord.ldi_go2rtc_state
        lock.release()
        await task
        assert go2rtc.deletes == [NAME]

    @pytest.mark.asyncio
    async def test_unknown_camera_is_a_noop(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        await ldi_go2rtc.unregister_stream(coord, CAM)  # type: ignore[arg-type]
        assert fake.deletes == []
        assert fake.gets == 0

    @pytest.mark.asyncio
    async def test_state_dropped_even_without_go2rtc(self) -> None:
        coord = _coord(None, None)
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        await ldi_go2rtc.unregister_stream(coord, CAM)  # type: ignore[arg-type]
        assert CAM not in coord.ldi_go2rtc_state

    @pytest.mark.asyncio
    async def test_unregister_all(self, go2rtc: FakeGo2rtc, session: Any) -> None:
        fake = server = go2rtc
        coord = _coord(server, session)
        other = "22222222-2222-2222-2222-222222222222"
        await ldi_go2rtc.ensure_stream(coord, CAM, SRC)  # type: ignore[arg-type]
        await ldi_go2rtc.ensure_stream(coord, other, SRC)  # type: ignore[arg-type]
        await ldi_go2rtc.unregister_all(coord)  # type: ignore[arg-type]
        assert sorted(fake.deletes) == ["ldi_11111111", "ldi_22222222"]
        assert coord.ldi_go2rtc_state == {}

    @pytest.mark.asyncio
    async def test_unregister_all_without_state(self) -> None:
        await ldi_go2rtc.unregister_all(SimpleNamespace())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_go2rtc_client_unregister_also_removes_ldi_stream(self) -> None:
        coord = SimpleNamespace(camera_entities={})
        with (
            patch.object(ldi_go2rtc, "unregister_stream", new=AsyncMock()) as ldi,
            patch(
                "custom_components.bosch_shc_camera.go2rtc_client._go2rtc_client_session",
                side_effect=RuntimeError("closed"),
            ),
        ):
            await unregister_go2rtc_stream(coord, CAM)  # type: ignore[arg-type]
        ldi.assert_awaited_once_with(coord, CAM)


class TestRemoveLeftovers:
    @pytest.mark.asyncio
    async def test_removes_only_unkept_ldi_streams(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.streams = {
            "ldi_aaaaaaaa": [SRC],
            "ldi_bbbbbbbb": [SRC],
            "camera.other": ["rtsp://x"],
        }
        await ldi_go2rtc.remove_leftovers(_coord(server, session), {"ldi_bbbbbbbb"})  # type: ignore[arg-type]
        assert fake.deletes == ["ldi_aaaaaaaa"]
        assert set(fake.streams) == {"ldi_bbbbbbbb", "camera.other"}

    @pytest.mark.asyncio
    async def test_no_go2rtc(self) -> None:
        await ldi_go2rtc.remove_leftovers(_coord(None, None), set())  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_listing_error_changes_nothing(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        fake = server = go2rtc
        fake.streams = {"ldi_aaaaaaaa": [SRC]}
        fake.get_status = 500
        await ldi_go2rtc.remove_leftovers(_coord(server, session), set())  # type: ignore[arg-type]
        assert fake.deletes == []

    @pytest.mark.asyncio
    async def test_unreachable_listing(self, go2rtc: FakeGo2rtc) -> None:
        coord = _coord(go2rtc)
        real_get = go2rtc.get

        def flaky_get(url: URL, params: dict[str, str] | None = None) -> _Resp:
            if url.path == "/api/streams":
                raise aiohttp.ClientConnectionError("gone")
            return real_get(url, params)

        go2rtc.get = flaky_get  # type: ignore[method-assign]
        await ldi_go2rtc.remove_leftovers(coord, set())  # type: ignore[arg-type]
        assert go2rtc.deletes == []

    @pytest.mark.asyncio
    async def test_non_dict_listing(self, go2rtc: FakeGo2rtc) -> None:
        go2rtc.listing_body = ["x"]
        await ldi_go2rtc.remove_leftovers(_coord(go2rtc), set())  # type: ignore[arg-type]
        assert go2rtc.deletes == []

    @pytest.mark.asyncio
    async def test_streams_claimed_by_a_live_session_survive(
        self, go2rtc: FakeGo2rtc, session: Any
    ) -> None:
        go2rtc.streams = {NAME: [SRC], "ldi_aaaaaaaa": [SRC]}
        coord = _coord(go2rtc, session)
        coord.ldi_go2rtc_state = {CAM: {}}
        other = SimpleNamespace(
            ldi_go2rtc_state={"aaaaaaaa-0000-0000-0000-000000000000": {}}
        )
        coord.hass.config_entries = SimpleNamespace(
            async_entries=lambda _d: [
                SimpleNamespace(runtime_data=other),
                SimpleNamespace(runtime_data=None),
                SimpleNamespace(runtime_data=coord),
            ]
        )
        await ldi_go2rtc.remove_leftovers(coord, set())  # type: ignore[arg-type]
        assert go2rtc.deletes == []


# ── wiring: unload and startup ──────────────────────────────────────────────
class TestWiring:
    @pytest.mark.asyncio
    async def test_unload_cleanup_error_is_swallowed(self) -> None:
        from custom_components.bosch_shc_camera import _async_cancel_coordinator_tasks
        from tests.test_init import _make_minimal_coord

        coord = _make_minimal_coord([])
        with (
            patch.object(
                ldi_go2rtc, "unregister_all", new=AsyncMock(side_effect=OSError("x"))
            ) as cleanup,
            patch(
                "custom_components.bosch_shc_camera.nvr_recorder.stop_all",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.stop_all_proxies",
                new=AsyncMock(),
            ),
        ):
            await _async_cancel_coordinator_tasks(coord)  # type: ignore[arg-type]
        cleanup.assert_awaited_once_with(coord)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("options", "expected"),
        [({"local_passwords": {CAM: "x"}}, 1), ({}, 0), ({"local_passwords": {}}, 0)],
    )
    async def test_startup_cleanup_only_when_a_password_is_stored(
        self, options: dict[str, Any], expected: int
    ) -> None:
        from custom_components.bosch_shc_camera import async_setup_entry
        from tests.test_init import (
            _FakeStore,
            _make_coord_stub_setup_lan_fallback,
            _make_entry_setup_lan_fallback,
            _make_hass_setup_lan_fallback,
        )

        hass = _make_hass_setup_lan_fallback()
        entry = _make_entry_setup_lan_fallback(options)
        coord = _make_coord_stub_setup_lan_fallback([CAM])
        ent_reg = MagicMock()
        ent_reg.async_get_entity_id = MagicMock(return_value=None)
        cleanup = AsyncMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.BoschCameraCoordinator",
                return_value=coord,
            ),
            patch("homeassistant.helpers.storage.Store", return_value=_FakeStore(None)),
            patch("custom_components.bosch_shc_camera.cf_unbuffer.register"),
            patch(
                "homeassistant.helpers.entity_registry.async_get",
                return_value=ent_reg,
            ),
            patch.object(ldi_go2rtc, "remove_leftovers", new=cleanup),
        ):
            await async_setup_entry(hass, entry)
        names = [c.kwargs.get("name") for c in coord.spawn_tracked.call_args_list]
        assert names.count("bosch_shc_camera_ldi_go2rtc_cleanup") == expected
        assert cleanup.await_count == 0  # stub spawn closes the coroutine
        assert cleanup.call_count == expected
