"""Tests for the stable-URL REMOTE-session front-door (remote_viewing_front_door.py).

Covers `remote_resolve_inner` (the resolve callback wired into the shared
`FrontDoorRunner` via the new `relay_factory` hook),
`start_remote_viewing_front_door` (bind + sticky-port reuse + OSError
fallback + URL shape), `stop_remote_viewing_front_door`, and — the core
behaviour this whole feature exists for — `_PathRewriteRelay` correctly
rewriting an incoming client request's URI to the CURRENT session's
hash-bearing path against a real upstream socket, including across a
simulated session-boundary hash rotation between two client connects.

Reuses the same real-`asyncio.start_server`-based fake-upstream-server
pattern already established in `test_frigate_endpoint.py` /
`test_viewing_front_door.py`, and the same lightweight `SimpleNamespace`
coordinator-stub pattern as `test_viewing_front_door.py` for the
resolve/start/stop tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_shc_camera.frigate_endpoint import (
    AUTH_NONE,
    FrontDoorConfig,
    FrontDoorRunner,
)
from custom_components.bosch_shc_camera.remote_viewing_front_door import (
    RemoteTarget,
    _PathRewriteRelay,
    _remote_relay_factory,
    remote_resolve_inner,
    start_remote_viewing_front_door,
    stop_remote_viewing_front_door,
)

CAM_A = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled: None) -> Generator[None, None, None]:
    """Allow 127.0.0.1 loopback for the real front-door listener sockets
    this module's tests bind (matches test_frigate_endpoint.py's pattern)."""
    yield


def _coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        _live_connections={},
        _tls_proxy_ports={},
        _remote_viewing_front_door_runner=None,
        _remote_viewing_sticky_port={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────────────
# remote_resolve_inner
# ─────────────────────────────────────────────────────────────────────────────


class TestRemoteResolveInner:
    @pytest.mark.asyncio
    async def test_no_live_connection_returns_none(self):
        coord = _coord()
        assert await remote_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_local_connection_type_returns_none(self):
        """The LOCAL front-door has its own resolve/relay pair — a LOCAL
        session must never be served by the REMOTE relay's path-rewrite
        (there's no hash-bearing path to rewrite to in a LOCAL session)."""
        coord = _coord(
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
            _tls_proxy_ports={CAM_A: 12345},
        )
        assert await remote_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_missing_port_returns_none(self):
        coord = _coord(
            _live_connections={
                CAM_A: {
                    "_connection_type": "REMOTE",
                    "_remote_path": "/hashXXX/rtsp_tunnel?inst=1",
                }
            },
            _tls_proxy_ports={},  # no port cached yet
        )
        assert await remote_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_missing_path_returns_none(self):
        coord = _coord(
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            _tls_proxy_ports={CAM_A: 12345},
        )
        assert await remote_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_all_present_returns_remote_target(self):
        coord = _coord(
            _live_connections={
                CAM_A: {
                    "_connection_type": "REMOTE",
                    "_remote_path": "/hashFRESH/rtsp_tunnel?inst=1&fmtp=1",
                }
            },
            _tls_proxy_ports={CAM_A: 55123},
        )
        target = await remote_resolve_inner(coord, CAM_A)
        assert target is not None
        assert target.port == 55123
        assert target.path == "/hashFRESH/rtsp_tunnel?inst=1&fmtp=1"

    @pytest.mark.asyncio
    async def test_never_calls_try_live_connection(self):
        """Same 'reuse, don't open' contract as viewing_resolve_inner — a
        lazy-open here would mint a fresh hash/proxy port on every resolve,
        defeating the point of a stable published URL."""
        coord = _coord(try_live_connection=AsyncMock())
        await remote_resolve_inner(coord, CAM_A)
        coord.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_picks_up_rotated_hash_on_next_resolve(self):
        """A session-boundary reconnect mints a new hash — the NEXT resolve
        (i.e. the next client connect) must see it, with no special
        handling needed beyond reading `_live_connections` fresh."""
        coord = _coord(
            _live_connections={
                CAM_A: {
                    "_connection_type": "REMOTE",
                    "_remote_path": "/hashOLD/rtsp_tunnel?inst=1",
                }
            },
            _tls_proxy_ports={CAM_A: 11111},
        )
        first = await remote_resolve_inner(coord, CAM_A)
        assert first is not None
        assert first.path == "/hashOLD/rtsp_tunnel?inst=1"

        # Simulate the session-boundary reconnect: live_connection.py
        # overwrites _live_connections[cam_id] with a fresh hash + port.
        coord._live_connections[CAM_A] = {
            "_connection_type": "REMOTE",
            "_remote_path": "/hashNEW/rtsp_tunnel?inst=1",
        }
        coord._tls_proxy_ports[CAM_A] = 22222

        second = await remote_resolve_inner(coord, CAM_A)
        assert second is not None
        assert second.path == "/hashNEW/rtsp_tunnel?inst=1"
        assert second.port == 22222


# ─────────────────────────────────────────────────────────────────────────────
# start_remote_viewing_front_door / stop_remote_viewing_front_door
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def runner_coord():
    coord = _coord()
    yield coord
    runner = coord._remote_viewing_front_door_runner
    if runner is not None:
        runner.stop_all()


class TestStartRemoteViewingFrontDoor:
    @pytest.mark.asyncio
    async def test_returns_stable_hash_free_url(self, runner_coord):
        url = await start_remote_viewing_front_door(
            runner_coord,
            CAM_A,
            inst=1,
            audio_param="&enableaudio=1",
            max_session_duration=3600,
        )
        assert url is not None
        assert "@" not in url  # no embedded credentials (REMOTE never had any)
        assert url.startswith("rtsp://127.0.0.1:")
        assert "/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600" in url
        # No hash segment anywhere in the published URL — that's the whole point.
        assert "hash" not in url.lower()

    @pytest.mark.asyncio
    async def test_non_default_inst_value_threaded_through(self, runner_coord):
        url = await start_remote_viewing_front_door(
            runner_coord,
            CAM_A,
            inst=2,
            audio_param="&enableaudio=1",
            max_session_duration=3600,
        )
        assert url is not None
        assert "inst=2" in url
        assert "inst=1" not in url

    @pytest.mark.asyncio
    async def test_lazily_creates_runner(self, runner_coord):
        assert runner_coord._remote_viewing_front_door_runner is None
        await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        assert isinstance(
            runner_coord._remote_viewing_front_door_runner, FrontDoorRunner
        )

    @pytest.mark.asyncio
    async def test_binds_auth_none_localhost_only(self, runner_coord):
        await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        server = runner_coord._remote_viewing_front_door_runner._servers[CAM_A]
        assert server.config.bind_host == "127.0.0.1"
        assert server.config.auth_mode == AUTH_NONE
        assert server.config.ip_allowlist == frozenset()

    @pytest.mark.asyncio
    async def test_uses_the_path_rewrite_relay_factory(self, runner_coord):
        """The whole point of the `relay_factory` hook — this front-door's
        listener must be wired to `_remote_relay_factory`, NOT
        `frigate_endpoint.py`'s default Digest-injecting factory (there is
        no Authorization dance to conduct for REMOTE)."""
        await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        server = runner_coord._remote_viewing_front_door_runner._servers[CAM_A]
        assert server._relay_factory is _remote_relay_factory

    @pytest.mark.asyncio
    async def test_sticky_port_reused_across_calls(self, runner_coord):
        url1 = await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        port1 = runner_coord._remote_viewing_sticky_port[CAM_A]
        url2 = await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        port2 = runner_coord._remote_viewing_sticky_port[CAM_A]
        assert port1 == port2
        assert url1 == url2

    @pytest.mark.asyncio
    async def test_second_call_reuses_listener_does_not_rebind(self, runner_coord):
        """Same 'don't restart what doesn't need restarting' optimization as
        the LOCAL front-door — remote_resolve_inner reads fresh port/path
        per client (re)connect, so a routine call for an already-bound
        listener must not restart it."""
        await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        server_before = runner_coord._remote_viewing_front_door_runner._servers[CAM_A]
        with patch.object(
            FrontDoorRunner, "start_server", AsyncMock()
        ) as start_server_spy:
            await start_remote_viewing_front_door(
                runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
            )
        start_server_spy.assert_not_called()
        server_after = runner_coord._remote_viewing_front_door_runner._servers[CAM_A]
        assert server_before is server_after

    @pytest.mark.asyncio
    async def test_renewal_call_with_different_inst_updates_url_not_listener(
        self, runner_coord
    ):
        url1 = await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        port1 = runner_coord._remote_viewing_sticky_port[CAM_A]
        url2 = await start_remote_viewing_front_door(
            runner_coord,
            CAM_A,
            inst=2,
            audio_param="&enableaudio=1",
            max_session_duration=3600,
        )
        port2 = runner_coord._remote_viewing_sticky_port[CAM_A]
        assert port1 == port2  # same listener, same port
        assert "inst=1" in url1
        assert "inst=2" in url2

    @pytest.mark.asyncio
    async def test_oserror_on_sticky_port_falls_back_to_ephemeral(self, runner_coord):
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        blocked_port = blocker.sockets[0].getsockname()[1]
        try:
            runner_coord._remote_viewing_sticky_port[CAM_A] = blocked_port
            url = await start_remote_viewing_front_door(
                runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
            )
            assert url is not None
            new_port = runner_coord._remote_viewing_sticky_port[CAM_A]
            assert new_port != blocked_port
        finally:
            blocker.close()
            await blocker.wait_closed()

    @pytest.mark.asyncio
    async def test_total_bind_failure_returns_none(self, runner_coord, monkeypatch):
        async def _always_raise_oserror(*args, **kwargs):
            raise OSError("simulated bind failure")

        monkeypatch.setattr(FrontDoorRunner, "start_server", _always_raise_oserror)
        url = await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        assert url is None


class TestStopRemoteViewingFrontDoor:
    @pytest.mark.asyncio
    async def test_noop_when_runner_is_none(self):
        coord = _coord()
        await stop_remote_viewing_front_door(coord, CAM_A)

    @pytest.mark.asyncio
    async def test_stops_running_server(self, runner_coord):
        await start_remote_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=3600
        )
        assert runner_coord._remote_viewing_front_door_runner.has_server(CAM_A)
        await stop_remote_viewing_front_door(runner_coord, CAM_A)
        assert not runner_coord._remote_viewing_front_door_runner.has_server(CAM_A)


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end relay against a fake upstream ("inner TLS proxy") — the core
# path-rewrite behaviour.
# ─────────────────────────────────────────────────────────────────────────────


class FakeInnerProxy:
    """Loopback RTSP server standing in for the inner `tls_proxy.py` proxy.

    Records the request-URI of every request it receives (after the relay's
    rewrite) so a test can assert the CURRENT hash path was substituted, and
    always answers 200 OK with a tiny SDP body.
    """

    def __init__(self) -> None:
        self.port = 0
        self.request_uris: list[str] = []
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> FakeInnerProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                    if not chunk:
                        return
                    data += chunk
                first_line = data.split(b"\r\n", 1)[0].decode()
                parts = first_line.split(" ")
                self.request_uris.append(parts[1] if len(parts) >= 2 else "")
                body = b"v=0\r\n"
                writer.write(
                    b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
                await writer.drain()
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()


def _resolver(target: RemoteTarget | None):
    async def resolve(_cam_id: str) -> RemoteTarget | None:
        return target

    return resolve


async def _client_request(port: int, request: bytes, *, read: int = 4096) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    try:
        resp = await asyncio.wait_for(reader.read(read), timeout=5.0)
    finally:
        writer.close()
    return resp


@pytest.fixture
def relay_runner() -> Generator[FrontDoorRunner, None, None]:
    r = FrontDoorRunner()
    yield r
    r.stop_all()


class TestPathRewriteRelayEndToEnd:
    async def test_rewrites_uri_to_current_hash_path(self, relay_runner):
        async with FakeInnerProxy() as inner:
            target = RemoteTarget(
                port=inner.port, path="/hashCURRENT/rtsp_tunnel?inst=1"
            )
            port = await relay_runner.start_server(
                "camAAAAAA",
                FrontDoorConfig(),
                _resolver(target),
                relay_factory=_remote_relay_factory,
            )
            resp = await _client_request(
                port,
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel?inst=1 RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            )
            assert resp.startswith(b"RTSP/1.0 200 OK")
            assert len(inner.request_uris) == 1
            assert inner.request_uris[0] == (
                f"rtsp://127.0.0.1:{inner.port}/hashCURRENT/rtsp_tunnel?inst=1"
            )

    async def test_rewrites_every_request_in_a_multi_request_session(
        self, relay_runner
    ):
        """SETUP/PLAY/TEARDOWN-style multi-request sessions must have EVERY
        forwarded request's URI rewritten, not just the first."""
        async with FakeInnerProxy() as inner:
            target = RemoteTarget(port=inner.port, path="/hashXYZ/rtsp_tunnel?inst=1")
            port = await relay_runner.start_server(
                "camBBBBBB",
                FrontDoorConfig(),
                _resolver(target),
                relay_factory=_remote_relay_factory,
            )
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            for cseq in (1, 2, 3):
                writer.write(
                    f"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel?inst=1 RTSP/1.0\r\n"
                    f"CSeq: {cseq}\r\n\r\n".encode()
                )
                await writer.drain()
                await asyncio.wait_for(reader.read(4096), timeout=5.0)
            writer.close()
            assert len(inner.request_uris) == 3
            assert all(
                uri == f"rtsp://127.0.0.1:{inner.port}/hashXYZ/rtsp_tunnel?inst=1"
                for uri in inner.request_uris
            )

    async def test_session_boundary_hash_change_between_connects(self, relay_runner):
        """Core regression this whole feature exists for: a published,
        STABLE client-facing URL must transparently follow a session-
        boundary hash rotation — the SECOND client connect (after a
        simulated reconnect) must be rewritten to the NEW hash, without the
        client ever seeing or needing to know the hash changed."""
        async with FakeInnerProxy() as inner_old, FakeInnerProxy() as inner_new:
            current_target = RemoteTarget(
                port=inner_old.port, path="/hashOLD/rtsp_tunnel?inst=1"
            )

            async def resolve(_cam_id: str) -> RemoteTarget:
                return current_target

            port = await relay_runner.start_server(
                "camCCCCCC",
                FrontDoorConfig(),
                resolve,
                relay_factory=_remote_relay_factory,
            )

            # First client connect — old session, old hash, old inner proxy.
            resp1 = await _client_request(
                port,
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel?inst=1 RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            )
            assert resp1.startswith(b"RTSP/1.0 200 OK")
            assert inner_old.request_uris == [
                f"rtsp://127.0.0.1:{inner_old.port}/hashOLD/rtsp_tunnel?inst=1"
            ]
            assert inner_new.request_uris == []

            # Simulate a session-boundary reconnect: live_connection.py has
            # opened a fresh REMOTE session with a new hash + new inner
            # proxy port; remote_resolve_inner's next call sees the update.
            current_target = RemoteTarget(
                port=inner_new.port, path="/hashNEW/rtsp_tunnel?inst=1"
            )

            # Second client connect (same STABLE published port/path) — must
            # be rewritten to the NEW hash and land on the NEW inner proxy.
            resp2 = await _client_request(
                port,
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel?inst=1 RTSP/1.0\r\nCSeq: 1\r\n\r\n",
            )
            assert resp2.startswith(b"RTSP/1.0 200 OK")
            assert inner_new.request_uris == [
                f"rtsp://127.0.0.1:{inner_new.port}/hashNEW/rtsp_tunnel?inst=1"
            ]
            # The old inner proxy saw no second request — the client was
            # transparently routed to the new session, not left on the old one.
            assert inner_old.request_uris == [
                f"rtsp://127.0.0.1:{inner_old.port}/hashOLD/rtsp_tunnel?inst=1"
            ]

    async def test_interleaved_binary_frame_after_first_request_forwarded_raw(
        self, relay_runner
    ):
        """An interleaved RTP/RTCP binary frame (`$`-prefixed) arriving
        AFTER the initial RTSP request (the only realistic shape — a
        session's first message is always RTSP; interleaved frames only
        appear post-SETUP) must be forwarded verbatim, never parsed or
        rewritten as an RTSP request."""
        async with FakeInnerProxy() as inner:
            target = RemoteTarget(port=inner.port, path="/hashRAW/rtsp_tunnel")
            port = await relay_runner.start_server(
                "camDDDDDD",
                FrontDoorConfig(),
                _resolver(target),
                relay_factory=_remote_relay_factory,
            )
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert inner.request_uris == [
                f"rtsp://127.0.0.1:{inner.port}/hashRAW/rtsp_tunnel"
            ]

            binary_frame = b"$\x00\x00\x08" + b"\xaa" * 8
            writer.write(binary_frame)
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()
            # The binary frame was forwarded raw (not parsed as a 2nd RTSP
            # request) — request_uris still holds only the first request.
            assert inner.request_uris == [
                f"rtsp://127.0.0.1:{inner.port}/hashRAW/rtsp_tunnel"
            ]

    async def test_resolve_none_returns_503(self, relay_runner):
        port = await relay_runner.start_server(
            "camEEEEEE",
            FrontDoorConfig(),
            _resolver(None),
            relay_factory=_remote_relay_factory,
        )
        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 503")


class TestPathRewriteRelayUnit:
    """Direct unit coverage of `_PathRewriteRelay` internals not easily
    reached via the end-to-end socket tests above (framing edge cases)."""

    def test_rewritten_uri_shape(self):
        relay = _PathRewriteRelay(
            "camFFFFFF",
            client_reader=None,  # type: ignore[arg-type]
            client_writer=None,  # type: ignore[arg-type]
            target=RemoteTarget(port=9999, path="/hashZZZ/rtsp_tunnel?inst=1"),
            first_request=b"",
        )
        assert (
            relay._rewritten_uri() == "rtsp://127.0.0.1:9999/hashZZZ/rtsp_tunnel?inst=1"
        )

    @pytest.mark.asyncio
    async def test_drain_requests_holds_back_incomplete_head(self):
        """A request whose headers haven't fully arrived yet must be held
        back (returned as the tail), not forwarded prematurely."""

        class _RecordingWriter:
            def __init__(self) -> None:
                self.written: list[bytes] = []
                self._closing = False

            def write(self, data: bytes) -> None:
                self.written.append(data)

            async def drain(self) -> None:
                return None

            def is_closing(self) -> bool:
                return self._closing

        relay = _PathRewriteRelay(
            "camGGGGGG",
            client_reader=None,  # type: ignore[arg-type]
            client_writer=None,  # type: ignore[arg-type]
            target=RemoteTarget(port=1, path="/h/rtsp_tunnel"),
            first_request=b"",
        )
        relay._iw = _RecordingWriter()  # type: ignore[assignment]
        incomplete = b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n"
        remainder = await relay._drain_requests(incomplete)
        assert remainder == incomplete
        assert relay._iw.written == []  # nothing forwarded yet

    @pytest.mark.asyncio
    async def test_drain_requests_forwards_body_with_content_length(self):
        class _RecordingWriter:
            def __init__(self) -> None:
                self.written: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.written.append(data)

            async def drain(self) -> None:
                return None

            def is_closing(self) -> bool:
                return False

        relay = _PathRewriteRelay(
            "camHHHHHH",
            client_reader=None,  # type: ignore[arg-type]
            client_writer=None,  # type: ignore[arg-type]
            target=RemoteTarget(port=1, path="/h/rtsp_tunnel"),
            first_request=b"",
        )
        relay._iw = _RecordingWriter()  # type: ignore[assignment]
        body = b"v=0\r\n"
        req = (
            b"ANNOUNCE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\n"
            b"CSeq: 1\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n"
        )
        remainder = await relay._drain_requests(req + body)
        assert remainder == b""
        assert len(relay._iw.written) == 1
        assert relay._iw.written[0].endswith(body)
        assert b"rtsp://127.0.0.1:1/h/rtsp_tunnel" in relay._iw.written[0]

    @pytest.mark.asyncio
    async def test_drain_requests_waits_for_full_body(self):
        """Content-Length body arriving in a separate chunk must not be
        forwarded until the full body has accumulated in buf."""

        class _RecordingWriter:
            def __init__(self) -> None:
                self.written: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.written.append(data)

            async def drain(self) -> None:
                return None

            def is_closing(self) -> bool:
                return False

        relay = _PathRewriteRelay(
            "camIIIIII",
            client_reader=None,  # type: ignore[arg-type]
            client_writer=None,  # type: ignore[arg-type]
            target=RemoteTarget(port=1, path="/h/rtsp_tunnel"),
            first_request=b"",
        )
        relay._iw = _RecordingWriter()  # type: ignore[assignment]
        head = (
            b"ANNOUNCE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\n"
            b"CSeq: 1\r\nContent-Length: 5\r\n\r\n"
        )
        remainder = await relay._drain_requests(head)  # body not yet arrived
        assert remainder == head
        assert relay._iw.written == []

    @pytest.mark.asyncio
    async def test_drain_requests_oversized_non_rtsp_buffer_forwarded_raw(self):
        """A buffer that is neither a complete RTSP message nor an
        interleaved binary frame, and exceeds the head-size guard, must be
        forwarded raw rather than held forever waiting for a terminator
        that will never arrive (mirrors `_Relay._drain_requests`'s same
        guard)."""

        class _RecordingWriter:
            def __init__(self) -> None:
                self.written: list[bytes] = []

            def write(self, data: bytes) -> None:
                self.written.append(data)

            async def drain(self) -> None:
                return None

            def is_closing(self) -> bool:
                return False

        relay = _PathRewriteRelay(
            "camJJJJJJ",
            client_reader=None,  # type: ignore[arg-type]
            client_writer=None,  # type: ignore[arg-type]
            target=RemoteTarget(port=1, path="/h/rtsp_tunnel"),
            first_request=b"",
        )
        relay._iw = _RecordingWriter()  # type: ignore[assignment]
        oversized = b"x" * 70_000  # > _MAX_HEAD_BYTES, no \r\n\r\n, not "$"-prefixed
        remainder = await relay._drain_requests(oversized)
        assert remainder == b""
        assert relay._iw.written == [oversized]

    @pytest.mark.asyncio
    async def test_pipe_client_to_inner_connection_error_handled(self):
        """A ConnectionError/OSError raised while reading from the client
        must be caught, not propagated — matches
        `_Relay._pipe_client_to_inner`'s same guard."""

        class _FakeInnerWriter:
            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                return None

            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                pass

        class _FakeReader:
            async def read(self, n: int) -> bytes:
                raise ConnectionError("connection reset")

        relay = _PathRewriteRelay(
            "camKKKKKK",
            client_reader=_FakeReader(),  # type: ignore[arg-type]
            client_writer=None,  # type: ignore[arg-type]
            target=RemoteTarget(port=1, path="/h/rtsp_tunnel"),
            first_request=b"",
        )
        relay._iw = _FakeInnerWriter()  # type: ignore[assignment]

        await relay._pipe_client_to_inner()  # must not raise

    @pytest.mark.asyncio
    async def test_pipe_inner_to_client_oserror_handled(self):
        """Same guard as above, for the inner->client direction."""

        class _FakeClientWriter:
            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                return None

        class _FakeInnerReader:
            async def read(self, n: int) -> bytes:
                raise OSError("broken pipe")

        relay = _PathRewriteRelay(
            "camLLLLLL",
            client_reader=None,  # type: ignore[arg-type]
            client_writer=_FakeClientWriter(),  # type: ignore[arg-type]
            target=RemoteTarget(port=1, path="/h/rtsp_tunnel"),
            first_request=b"",
        )
        relay._ir = _FakeInnerReader()  # type: ignore[assignment]

        await relay._pipe_inner_to_client()  # must not raise

    @pytest.mark.asyncio
    async def test_run_finally_closes_both_writers_on_pre_gather_failure(self):
        """If the initial write/drain of the first (rewritten) request fails
        BEFORE `asyncio.gather` ever starts the pipe coroutines, `run()`'s
        own `finally` block must be the one to close both writers (the pipe
        coroutines' own finally blocks never ran) — mirrors
        `test_relay_run_finally_closes_writers` in test_frigate_endpoint.py
        for `_Relay`."""

        class _RaisingInnerWriter:
            def __init__(self) -> None:
                self._closing = False

            def write(self, data: bytes) -> None:
                pass

            async def drain(self) -> None:
                raise ConnectionResetError("simulated")

            def is_closing(self) -> bool:
                return self._closing

            def close(self) -> None:
                self._closing = True

            async def wait_closed(self) -> None:
                return None

        class _ClientWriter:
            def __init__(self) -> None:
                self._closing = False

            def is_closing(self) -> bool:
                return self._closing

            def close(self) -> None:
                self._closing = True

            async def wait_closed(self) -> None:
                return None

        inner_writer = _RaisingInnerWriter()
        client_writer = _ClientWriter()

        async def _fake_open_connection(host: str, port: int):
            return object(), inner_writer

        relay = _PathRewriteRelay(
            "camMMMMMM",
            client_reader=None,  # type: ignore[arg-type]
            client_writer=client_writer,  # type: ignore[arg-type]
            target=RemoteTarget(port=1, path="/h/rtsp_tunnel"),
            first_request=b"DESCRIBE rtsp://x/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n",
        )
        with patch("asyncio.open_connection", _fake_open_connection):
            with pytest.raises(ConnectionResetError):
                await relay.run()
        assert inner_writer.is_closing()
        assert client_writer.is_closing()
