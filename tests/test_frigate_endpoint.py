"""Tests for the credential-free always-on RTSP front-door (frigate_endpoint.py).

Covers the pure RTSP-parsing/gate helpers and end-to-end relay behaviour
(Digest auth-dance injection, gate auth modes, IP allowlist, lazy resolve)
against a hermetic fake "camera" RTSP server on 127.0.0.1.

Source of behaviour: docstring of frigate_endpoint.py + the ioBroker
rtsp_auth.ts state machine this module ports.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import frigate_endpoint as fe
from custom_components.bosch_shc_camera.frigate_endpoint import (
    AUTH_BASIC,
    AUTH_NONE,
    AUTH_PATH_TOKEN,
    FrontDoorConfig,
    FrontDoorRunner,
    InnerTarget,
    build_public_url,
    check_basic_auth,
    content_length,
    extract_header,
    find_rtsp_message_end,
    has_authorization_header,
    inject_auth_header,
    ip_allowed,
    parse_request_start_line,
    parse_response_status,
    split_path_token,
)


@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled):
    """Allow 127.0.0.1 loopback for the fake camera + front-door servers."""
    yield


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_find_rtsp_message_end():
    assert find_rtsp_message_end(b"A: b\r\n\r\nbody") == 8
    assert find_rtsp_message_end(b"incomplete\r\n") == -1


def test_parse_request_start_line():
    assert parse_request_start_line(b"DESCRIBE rtsp://h/p?x RTSP/1.0\r\n") == (
        "DESCRIBE",
        "rtsp://h/p?x",
    )
    assert parse_request_start_line(b"GET / HTTP/1.1\r\n") is None
    assert parse_request_start_line(b"garbage") is None
    # lowercase method is rejected
    assert parse_request_start_line(b"describe rtsp://h RTSP/1.0\r\n") is None


def test_parse_response_status():
    assert parse_response_status(b"RTSP/1.0 200 OK\r\n") == 200
    assert parse_response_status(b"RTSP/1.0 401 Unauthorized\r\n") == 401
    assert parse_response_status(b"not a response\r\n") is None
    assert parse_response_status(b"RTSP/1.0 NaN Bad\r\n") is None


def test_extract_header_case_insensitive():
    buf = b"DESCRIBE x RTSP/1.0\r\nCSeq: 1\r\nContent-Length: 42\r\n\r\n"
    assert extract_header(buf, "content-length") == "42"
    assert extract_header(buf, "CSEQ") == "1"
    assert extract_header(buf, "Missing") is None


def test_has_authorization_header():
    assert has_authorization_header(b"X RTSP/1.0\r\nAuthorization: Digest ...\r\n\r\n")
    assert not has_authorization_header(b"X RTSP/1.0\r\nCSeq: 1\r\n\r\n")


def test_content_length():
    assert content_length(b"X\r\nContent-Length: 7\r\n\r\n") == 7
    assert content_length(b"X\r\nCSeq: 1\r\n\r\n") == 0
    assert content_length(b"X\r\nContent-Length: abc\r\n\r\n") == 0


def test_inject_auth_header_adds_and_replaces():
    req = b"DESCRIBE x RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    out = inject_auth_header(req, "Digest foo")
    assert b"Authorization: Digest foo\r\n\r\n" in out
    # existing Authorization is dropped (only the injected one survives)
    req2 = b"DESCRIBE x RTSP/1.0\r\nAuthorization: Basic old\r\nCSeq: 1\r\n\r\n"
    out2 = inject_auth_header(req2, "Digest new")
    assert out2.count(b"Authorization:") == 1
    assert b"Digest new" in out2 and b"Basic old" not in out2


def test_inject_auth_header_no_terminator_returns_input():
    assert inject_auth_header(b"no terminator", "x") == b"no terminator"


@pytest.mark.parametrize(
    ("peer", "allow", "expected"),
    [
        ("192.168.1.5", frozenset(), True),  # empty = allow all
        ("192.168.1.5", frozenset({"192.168.1.5"}), True),
        ("192.168.1.6", frozenset({"192.168.1.5"}), False),
        ("192.168.1.20", frozenset({"192.168.1.0/24"}), True),
        ("10.0.0.1", frozenset({"192.168.1.0/24"}), False),
        ("not-an-ip", frozenset({"192.168.1.5"}), False),
        ("192.168.1.5", frozenset({"", "bogus", "192.168.1.5"}), True),  # skips junk
    ],
)
def test_ip_allowed(peer, allow, expected):
    assert ip_allowed(peer, allow) is expected


def test_split_path_token():
    # no token configured → pass-through unchanged
    assert split_path_token("rtsp://h:8/rtsp_tunnel?inst=1", "") == (
        True,
        "rtsp://h:8/rtsp_tunnel?inst=1",
    )
    # valid token stripped, query preserved
    assert split_path_token("rtsp://h:8/sek/rtsp_tunnel?inst=1", "sek") == (
        True,
        "rtsp://h:8/rtsp_tunnel?inst=1",
    )
    # wrong token rejected
    assert split_path_token("rtsp://h:8/bad/rtsp_tunnel", "sek")[0] is False
    # missing path after host rejected
    assert split_path_token("rtsp://h:8", "sek")[0] is False
    # relative URI form (no scheme) also works
    assert split_path_token("/sek/rtsp_tunnel", "sek") == (True, "/rtsp_tunnel")


def test_check_basic_auth():
    good = base64.b64encode(b"frigate:secret").decode()
    buf = f"X RTSP/1.0\r\nAuthorization: Basic {good}\r\n\r\n".encode()
    assert check_basic_auth(buf, "frigate", "secret")
    assert not check_basic_auth(buf, "frigate", "wrong")
    assert not check_basic_auth(b"X RTSP/1.0\r\nCSeq: 1\r\n\r\n", "frigate", "secret")
    assert not check_basic_auth(
        b"X RTSP/1.0\r\nAuthorization: Digest abc\r\n\r\n", "frigate", "secret"
    )
    assert not check_basic_auth(
        b"X RTSP/1.0\r\nAuthorization: Basic !!notb64!!\r\n\r\n", "frigate", "secret"
    )


def test_build_public_url_all_modes():
    none_cfg = FrontDoorConfig(auth_mode=AUTH_NONE)
    assert build_public_url("192.168.1.10", 8600, "high", none_cfg) == (
        "rtsp://192.168.1.10:8600/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60"
    )
    assert "inst=2" in build_public_url("h", 8600, "low", none_cfg)

    basic_cfg = FrontDoorConfig(auth_mode=AUTH_BASIC, token="secret", basic_user="rec")
    assert build_public_url("h", 8600, "high", basic_cfg).startswith(
        "rtsp://rec:secret@h:8600/"
    )

    tok_cfg = FrontDoorConfig(auth_mode=AUTH_PATH_TOKEN, token="sek")
    assert build_public_url("h", 8600, "high", tok_cfg).startswith(
        "rtsp://h:8600/sek/rtsp_tunnel"
    )

    # mode set but token empty → no credential / no prefix
    empty = FrontDoorConfig(auth_mode=AUTH_BASIC, token="")
    assert "@" not in build_public_url("h", 8600, "high", empty)


def test_module_private_rewrite_and_strip():
    req = b"DESCRIBE rtsp://h/sek/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    out = fe._rewrite_request_uri(req, "rtsp://h/rtsp_tunnel")
    assert out.startswith(b"DESCRIBE rtsp://h/rtsp_tunnel RTSP/1.0")
    assert fe._rewrite_request_uri(b"noeol", "x") == b"noeol"
    assert fe._rewrite_request_uri(b"TWO tokens\r\nx", "y") == b"TWO tokens\r\nx"

    authed = b"X y RTSP/1.0\r\nAuthorization: Basic z\r\nCSeq: 1\r\n\r\n"
    assert b"Authorization" not in fe._strip_authorization(authed)
    assert fe._strip_authorization(b"no terminator") == b"no terminator"


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end relay against a fake camera
# ─────────────────────────────────────────────────────────────────────────────


class FakeCamera:
    """Loopback RTSP server mimicking a Bosch camera's Digest behaviour.

    First DESCRIBE without Authorization → 401 + challenge. A DESCRIBE that
    carries Authorization → 200 OK + tiny SDP. Records every request so a test
    can assert the front-door injected the Digest header.
    """

    def __init__(self, *, stale: bool = False, never_challenge: bool = False):
        self.port = 0
        self.requests: list[bytes] = []
        self._server: asyncio.AbstractServer | None = None
        self._stale = stale
        self._never_challenge = never_challenge

    async def __aenter__(self) -> FakeCamera:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                    if not chunk:
                        return
                    data += chunk
                self.requests.append(data)
                if self._never_challenge:
                    writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
                elif has_authorization_header(data):
                    if self._stale:
                        writer.write(b"RTSP/1.0 401 Unauthorized\r\nCSeq: 2\r\n\r\n")
                    else:
                        body = b"v=0\r\n"
                        writer.write(
                            b"RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Length: "
                            + str(len(body)).encode()
                            + b"\r\n\r\n"
                            + body
                        )
                else:
                    writer.write(
                        b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n"
                        b'WWW-Authenticate: Digest realm="bosch", nonce="abc123"\r\n\r\n'
                    )
                await writer.drain()
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()


def _resolver(target: InnerTarget | None) -> Callable:
    async def resolve(_cam_id: str) -> InnerTarget | None:
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
def runner():
    r = FrontDoorRunner()
    yield r
    r.stop_all()


async def test_relay_none_injects_digest(runner):
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camAAAAAA", FrontDoorConfig(), _resolver(target)
        )
        assert port > 0
        assert runner.port("camAAAAAA") == port
        resp = await _client_request(
            port,
            b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel?inst=1 RTSP/1.0\r\nCSeq: 1\r\n\r\n",
        )
        # client never sees the swallowed 401 — only the authed 200
        assert resp.startswith(b"RTSP/1.0 200 OK")
        # the camera's second request carried the injected Digest header
        assert any(has_authorization_header(r) for r in cam.requests)
        assert any(b'realm="bosch"' not in r for r in cam.requests)


async def test_relay_stale_creds_forwards_401(runner):
    async with FakeCamera(stale=True) as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camBBBBBB", FrontDoorConfig(), _resolver(target)
        )
        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 401")
        assert len(cam.requests) == 2  # unauth probe + authed retry (both 401)


async def test_relay_camera_no_challenge(runner):
    async with FakeCamera(never_challenge=True) as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camCCCCCC", FrontDoorConfig(), _resolver(target)
        )
        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 200 OK")


async def test_relay_passthrough_when_client_has_auth(runner):
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camDDDDDD", FrontDoorConfig(), _resolver(target)
        )
        # client supplies its own Authorization → passthrough; camera answers 200
        resp = await _client_request(
            port,
            b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\n"
            b"Authorization: Digest realm=x\r\nCSeq: 1\r\n\r\n",
        )
        assert resp.startswith(b"RTSP/1.0 200 OK")


async def test_resolve_none_returns_503(runner):
    port = await runner.start_server("camEEEEEE", FrontDoorConfig(), _resolver(None))
    resp = await _client_request(
        port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    )
    assert resp.startswith(b"RTSP/1.0 503")


async def test_ip_allowlist_denies(runner):
    cfg = FrontDoorConfig(ip_allowlist=frozenset({"10.99.99.99"}))
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "u", "p")
        port = await runner.start_server("camFFFFFF", cfg, _resolver(target))
        # connecting client is 127.0.0.1 → not in allowlist → socket closed
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        await writer.drain()
        try:
            resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        except (ConnectionResetError, ConnectionError):
            resp = b""  # hard reset also means "rejected"
        writer.close()
        assert resp == b""  # closed without any RTSP response


async def test_path_token_gate(runner):
    cfg = FrontDoorConfig(auth_mode=AUTH_PATH_TOKEN, token="sek")
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "u", "p")
        port = await runner.start_server("camGGGGGG", cfg, _resolver(target))
        ok = await _client_request(
            port,
            b"DESCRIBE rtsp://127.0.0.1/sek/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n",
        )
        assert ok.startswith(b"RTSP/1.0 200 OK")
        # camera saw the token stripped (canonical path)
        assert any(b"/sek/" not in r for r in cam.requests)
        bad = await _client_request(
            port,
            b"DESCRIBE rtsp://127.0.0.1/wrong/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n",
        )
        assert bad == b""  # rejected, socket closed


async def test_basic_auth_gate(runner):
    cfg = FrontDoorConfig(auth_mode=AUTH_BASIC, token="secret", basic_user="rec")
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "u", "p")
        port = await runner.start_server("camHHHHHH", cfg, _resolver(target))
        # missing creds → 401 Basic challenge
        miss = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert miss.startswith(b"RTSP/1.0 401") and b"Basic" in miss
        # correct creds → 200
        cred = base64.b64encode(b"rec:secret").decode()
        ok = await _client_request(
            port,
            f"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\n"
            f"Authorization: Basic {cred}\r\nCSeq: 1\r\n\r\n".encode(),
        )
        assert ok.startswith(b"RTSP/1.0 200 OK")


async def test_runner_active_count_and_restart(runner):
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "u", "p")
        port1 = await runner.start_server(
            "camIIIIII", FrontDoorConfig(), _resolver(target)
        )
        assert runner.has_server("camIIIIII")
        assert runner.active_count("camIIIIII") == 0
        assert runner.active_count("unknown") == 0
        assert runner.port("unknown") == 0
        # restart picks a (possibly new) port and keeps working
        port2 = await runner.start_server(
            "camIIIIII", FrontDoorConfig(), _resolver(target)
        )
        assert port2 > 0
        runner.stop_server("camIIIIII")
        assert not runner.has_server("camIIIIII")
        runner.stop_server("camIIIIII")  # idempotent


# ─────────────────────────────────────────────────────────────────────────────
# ip_allowed: ValueError branch in the inner loop (line 200-201)
# ─────────────────────────────────────────────────────────────────────────────


def test_ip_allowed_invalid_cidr_entry_skipped():
    """A garbled CIDR entry raises ValueError inside the loop → skip, not crash."""
    # "192.168.1.0/999" is invalid; the valid "192.168.1.5" entry still matches.
    allow = frozenset({"192.168.1.0/999", "192.168.1.5"})
    assert ip_allowed("192.168.1.5", allow) is True
    # And a non-matching IP returns False after skipping the bad entry.
    assert ip_allowed("10.0.0.1", allow) is False


# ─────────────────────────────────────────────────────────────────────────────
# _serve: first-request body read (lines 514-515) + malformed first line (535-538)
# ─────────────────────────────────────────────────────────────────────────────


async def test_serve_reads_body_when_content_length_present(runner):
    """_serve reads an extra ``Content-Length`` body before passing to the relay."""

    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camJJJJJJ", FrontDoorConfig(), _resolver(target)
        )
        body = b"x" * 7
        req = (
            b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\n"
            b"CSeq: 1\r\n"
            b"Content-Length: 7\r\n"
            b"\r\n" + body
        )
        resp = await _client_request(port, req)
        # The server should still complete the auth dance and return 200
        assert resp.startswith(b"RTSP/1.0 200 OK")


async def test_serve_drops_malformed_request_line(runner):
    """_serve closes the socket if the first line is not a valid RTSP request."""
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camKKKKKK", FrontDoorConfig(), _resolver(target)
        )
        # Send something that ends with \r\n\r\n but has an invalid request line
        bad = b"not a valid rtsp line\r\n\r\n"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(bad)
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        writer.close()
        assert resp == b""  # closed without response


# ─────────────────────────────────────────────────────────────────────────────
# _serve: resolve_inner raises (lines 611-615) + inner connect failure (628-629)
# ─────────────────────────────────────────────────────────────────────────────


async def test_resolve_raises_returns_503(runner):
    """If resolve_inner raises an exception the front-door returns 503."""

    async def _bad_resolve(_cam_id: str) -> InnerTarget | None:
        raise RuntimeError("simulated resolve failure")

    port = await runner.start_server("camLLLLLL", FrontDoorConfig(), _bad_resolve)
    resp = await _client_request(
        port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    )
    assert resp.startswith(b"RTSP/1.0 503")


async def test_relay_run_inner_connect_failure(runner):
    """When the inner proxy port is dead, _Relay.run raises OSError (line 628-629)."""
    # Use a port number that is (very likely) not listening.
    dead_target = InnerTarget(1, "user", "pass")  # port 1 = privileged, never open
    port = await runner.start_server(
        "camMMMMMM", FrontDoorConfig(), _resolver(dead_target)
    )
    resp = await _client_request(
        port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    )
    # The relay failed to connect; front-door logs + returns (no RTSP response written)
    assert resp == b"" or resp.startswith(b"RTSP/1.0 503")


# ─────────────────────────────────────────────────────────────────────────────
# _auth_dance: 401 with no parseable challenge (lines 385-390)
# ─────────────────────────────────────────────────────────────────────────────


class FakeCameraNoChallenge:
    """Returns 401 WITHOUT a valid Digest challenge → forces the forward-401 branch."""

    def __init__(self):
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> FakeCameraNoChallenge:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            while True:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                    if not chunk:
                        return
                    data += chunk
                # Always return 401 with an INVALID (non-Digest) challenge
                writer.write(
                    b"RTSP/1.0 401 Unauthorized\r\nWWW-Authenticate: Basic realm=x\r\n\r\n"
                )
                await writer.drain()
        except (TimeoutError, ConnectionError, OSError):
            pass
        finally:
            if not writer.is_closing():
                writer.close()


async def test_auth_dance_no_parseable_challenge_forwards_401(runner):
    """401 with non-Digest WWW-Authenticate → challenge is None → forward 401."""
    async with FakeCameraNoChallenge() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camNNNNNN", FrontDoorConfig(), _resolver(target)
        )
        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 401")


# ─────────────────────────────────────────────────────────────────────────────
# _pipe_client_to_inner + _drain_requests + _pipe_inner_to_client steady-state
# (lines 411-449, 461-462)
# ─────────────────────────────────────────────────────────────────────────────


class FakeCameraMultiRequest:
    """Fake camera that keeps the connection open and echoes responses to multiple requests.

    After the initial Digest dance (DESCRIBE → 401 → DESCRIBE+auth → 200),
    subsequent requests (SETUP, PLAY, etc.) get a simple 200 OK back so the
    steady-state relay pipes are exercised.
    """

    def __init__(self):
        self.port = 0
        self.requests: list[bytes] = []
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> FakeCameraMultiRequest:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            authed = False
            while True:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                    if not chunk:
                        return
                    data += chunk
                self.requests.append(data)
                if not authed:
                    if has_authorization_header(data):
                        authed = True
                        writer.write(b"RTSP/1.0 200 OK\r\nCSeq: 2\r\n\r\n")
                    else:
                        writer.write(
                            b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n"
                            b'WWW-Authenticate: Digest realm="bosch", nonce="abc123"\r\n\r\n'
                        )
                else:
                    # Steady state: respond with 200 + small body
                    body = b"OK\r\n"
                    writer.write(
                        b"RTSP/1.0 200 OK\r\nCSeq: 3\r\nContent-Length: "
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


async def test_steady_state_relay_injects_digest_on_subsequent_requests(runner):
    """After auth dance, SETUP/PLAY get Digest injected via _pipe_client_to_inner."""
    async with FakeCameraMultiRequest() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camOOOOOO", FrontDoorConfig(), _resolver(target)
        )

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            # First request: triggers the Digest auth dance
            describe = (
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            )
            writer.write(describe)
            await writer.drain()
            # Consume the 200 OK response
            resp1 = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert resp1.startswith(b"RTSP/1.0 200 OK")

            # Follow-up request: SETUP — the front-door must inject Digest
            setup = (
                b"SETUP rtsp://127.0.0.1/rtsp_tunnel/track1 RTSP/1.0\r\nCSeq: 2\r\n\r\n"
            )
            writer.write(setup)
            await writer.drain()
            resp2 = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert resp2.startswith(b"RTSP/1.0 200 OK")

            # The camera's steady-state requests should have the Authorization header
            steady_reqs = cam.requests[2:]  # skip the two auth-dance requests
            assert any(has_authorization_header(r) for r in steady_reqs)
        finally:
            writer.close()


async def test_drain_requests_interleaved_rtp_frame(runner):
    """A '$'-prefixed interleaved RTP frame is forwarded raw without RTSP parsing."""
    async with FakeCameraMultiRequest() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camPPPPPP", FrontDoorConfig(), _resolver(target)
        )

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            # Complete auth dance first
            writer.write(
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            )
            await writer.drain()
            resp1 = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert resp1.startswith(b"RTSP/1.0 200 OK")

            # Send an interleaved RTP frame (starts with 0x24 = '$')
            rtp_frame = b"$\x00\x00\x08" + b"\xde\xad\xbe\xef\xca\xfe\xba\xbe"
            writer.write(rtp_frame)
            await writer.drain()
            # Give the relay time to process it; we just need no crash
            await asyncio.sleep(0.1)
        finally:
            writer.close()


async def test_drain_requests_oversized_non_rtsp_flushed(runner):
    """A buffer > _MAX_HEAD_BYTES without RTSP header is forwarded raw (line 420-424)."""
    async with FakeCameraMultiRequest() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camQQQQQQ", FrontDoorConfig(), _resolver(target)
        )

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            # Complete auth dance
            writer.write(
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            )
            await writer.drain()
            resp1 = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            assert resp1.startswith(b"RTSP/1.0 200 OK")

            # Send more than _MAX_HEAD_BYTES (64 KiB) of non-RTSP, non-'$' data
            big_blob = b"X" * (64 * 1024 + 1)
            writer.write(big_blob)
            await writer.drain()
            await asyncio.sleep(0.1)
        finally:
            writer.close()


async def test_pipe_inner_to_client_relay(runner):
    """_pipe_inner_to_client forwards data from inner→client verbatim."""
    async with FakeCameraMultiRequest() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camRRRRRR", FrontDoorConfig(), _resolver(target)
        )

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(
                b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
            )
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            # The inner-to-client pipe ran (client received the response)
            assert resp.startswith(b"RTSP/1.0 200 OK")
        finally:
            writer.close()


# ─────────────────────────────────────────────────────────────────────────────
# _drain_requests: incomplete body wait (lines 429-430) + body complete (431)
# ─────────────────────────────────────────────────────────────────────────────


async def test_drain_requests_incomplete_body_waits_for_more():
    """_drain_requests returns req+tail when body bytes are still missing."""
    # We call _drain_requests directly via a minimal _Relay instance
    import io

    # Build a fake _Relay with a mock inner writer
    loop = asyncio.get_event_loop()

    # Fake writer that records what was written
    written: list[bytes] = []

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    fake_iw = _FakeWriter()
    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camXXXXXX"
    relay._iw = fake_iw
    relay._challenge = {"realm": "bosch", "nonce": "abc123"}
    relay._target = InnerTarget(9999, "user", "pass")

    # A DESCRIBE with Content-Length: 10 but only 3 bytes of body in buf
    head = b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\nContent-Length: 10\r\n\r\n"
    partial_body = b"abc"  # only 3 of 10 bytes
    buf = head + partial_body

    tail = await relay._drain_requests(buf)
    # Should return the whole buffer unchanged (body incomplete → wait for more)
    assert tail == buf
    assert written == []  # nothing forwarded yet


async def test_drain_requests_complete_body_forwarded():
    """When body bytes arrive, the full message (head+body) is forwarded."""
    written: list[bytes] = []

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camXXXXXX"
    relay._iw = _FakeWriter()
    relay._challenge = {"realm": "bosch", "nonce": "abc123"}
    relay._target = InnerTarget(9999, "user", "pass")

    body = b"0123456789"  # exactly 10 bytes
    head = b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\nContent-Length: 10\r\n\r\n"
    buf = head + body

    tail = await relay._drain_requests(buf)
    assert tail == b""  # fully consumed
    # The written data should contain the injected Authorization header
    combined = b"".join(written)
    assert b"Authorization:" in combined


# ─────────────────────────────────────────────────────────────────────────────
# _drain_requests: inject_auth_header raises ValueError/KeyError (lines 444-445)
# ─────────────────────────────────────────────────────────────────────────────


async def test_drain_requests_auth_inject_exception_falls_back_to_raw():
    """If _build_digest_header raises, the raw request is forwarded unchanged."""
    from unittest.mock import patch

    written: list[bytes] = []

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camXXXXXX"
    relay._iw = _FakeWriter()
    relay._challenge = {"realm": "bosch", "nonce": "abc123"}
    relay._target = InnerTarget(9999, "user", "pass")

    buf = b"SETUP rtsp://127.0.0.1/rtsp_tunnel/t RTSP/1.0\r\nCSeq: 2\r\n\r\n"

    with patch(
        "custom_components.bosch_shc_camera.frigate_endpoint._build_digest_header",
        side_effect=ValueError("simulated build failure"),
    ):
        tail = await relay._drain_requests(buf)

    assert tail == b""
    # Raw request (without injected auth) should have been forwarded
    combined = b"".join(written)
    assert b"SETUP" in combined
    assert b"Authorization:" not in combined


# ─────────────────────────────────────────────────────────────────────────────
# _serve: on_active callback raises (lines 535-538) + on_idle raises (544-547)
# ─────────────────────────────────────────────────────────────────────────────


async def test_on_active_callback_exception_does_not_kill_connection(runner):
    """on_active raising must not abort the connection (lines 535-538)."""

    def _boom():
        raise RuntimeError("on_active exploded")

    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camSSSSSS",
            FrontDoorConfig(),
            _resolver(target),
            on_active=_boom,
        )
        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        # Connection still completes despite the on_active exception
        assert resp.startswith(b"RTSP/1.0 200 OK")


async def test_on_idle_callback_exception_does_not_crash(runner):
    """on_idle raising inside the idle-linger must not propagate. idle_timeout=0
    so on_idle fires promptly after the client disconnects (bug-hunt 2026-07-01)."""

    idle_ran = asyncio.Event()

    def _boom_idle():
        idle_ran.set()
        raise RuntimeError("on_idle exploded")

    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camTTTTTT",
            FrontDoorConfig(idle_timeout=0),
            _resolver(target),
            on_idle=_boom_idle,
        )
        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 200 OK")
        # on_idle fired (idle_timeout=0) and its exception was swallowed — no crash.
        await asyncio.wait_for(idle_ran.wait(), timeout=5.0)


async def test_idle_timeout_zero_fires_on_idle_after_last_client(runner):
    """C5 (bug-hunt 2026-07-01): frigate_idle_timeout now actually drives the
    front-door. With idle_timeout=0 on_idle is signalled promptly once the last
    recorder client disconnects (previously the option was a dead no-op)."""

    idle_called = asyncio.Event()

    def _on_idle():
        idle_called.set()

    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camIDLE00",
            FrontDoorConfig(idle_timeout=0),
            _resolver(target),
            on_idle=_on_idle,
        )
        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 200 OK")
        await asyncio.wait_for(idle_called.wait(), timeout=5.0)


async def test_idle_linger_cancelled_on_reconnect_does_not_signal():
    """C5 (bug-hunt 2026-07-01): a pending idle-linger is cancelled when a new
    client connects, so a recorder that briefly reconnects (segment boundary)
    doesn't get its on-demand session torn down."""
    from custom_components.bosch_shc_camera.frigate_endpoint import _CameraServer

    calls: list[int] = []
    server = _CameraServer(
        "camRECON0",
        FrontDoorConfig(idle_timeout=100),  # long linger so it won't fire on its own
        _resolver(None),
        None,  # on_active
        lambda: calls.append(1),  # on_idle
    )
    server.client_count = 0
    # Arm the linger exactly as _handle does when the last client leaves.
    server._idle_task = asyncio.create_task(server._idle_linger())
    await asyncio.sleep(0)  # let it start and reach the 100s sleep
    # Simulate a reconnect: _handle's active branch cancels the pending linger.
    server._idle_task.cancel()
    await asyncio.sleep(0)
    assert calls == []  # on_idle never fired — the teardown was averted


async def test_close_cancels_pending_idle_linger():
    """close() cancels a still-pending idle-linger task (line 537), so the
    front-door doesn't fire a stale on_idle after it's already been stopped."""

    async def _resolve(_cam_id: str) -> InnerTarget | None:
        return None

    server = fe._CameraServer(
        "camCLOSE0", FrontDoorConfig(idle_timeout=100), _resolve, None, None
    )
    server._idle_task = asyncio.create_task(server._idle_linger())
    await asyncio.sleep(0)  # let it start and reach the 100s sleep
    pending = server._idle_task

    server.close()

    assert server._idle_task is None
    # _idle_linger swallows CancelledError internally (`except ...: return`), so
    # the task completes normally rather than reporting .cancelled() — the
    # real signal that close() actually cancelled it is that awaiting it
    # returns almost instantly instead of hanging for the full 100s sleep.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(pending, timeout=2.0)


async def test_handle_reconnect_cancels_pending_idle_linger_via_real_connection(
    runner,
):
    """C5 (bug-hunt 2026-07-01): a real client connection (not a direct
    _idle_linger manipulation) exercises _handle's own connect-time guard
    (line 573) that cancels a stale pending linger."""
    calls: list[int] = []
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camRECON1",
            FrontDoorConfig(idle_timeout=100),
            _resolver(target),
            on_idle=lambda: calls.append(1),
        )
        server = runner._servers["camRECON1"]
        server._idle_task = asyncio.create_task(server._idle_linger())
        await asyncio.sleep(0)
        pending = server._idle_task

        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 200 OK")

        # _idle_linger swallows CancelledError (`except ...: return`), so it
        # completes normally rather than reporting .cancelled() — the real
        # proof it was actually cancelled is that this returns almost
        # instantly instead of hanging for the full 100s idle_timeout.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=2.0)
        assert calls == []  # the stale linger's on_idle never fired


async def test_handle_finally_cancels_stale_idle_task_at_zero_clients(runner):
    """Defensive guard (line 595) distinct from the connect-time one (line 573):
    if a pending idle-linger task is still set at the exact moment client_count
    reaches zero in _handle's finally block — e.g. another client disconnected
    while this one was still being served, so this connection's own connect
    saw client_count go 1->2 and skipped the connect-time clear — it must still
    be cancelled before a fresh linger is armed."""
    calls: list[int] = []
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camRACE001",
            FrontDoorConfig(idle_timeout=100),
            _resolver(target),
            on_idle=lambda: calls.append(1),
        )
        server = runner._servers["camRACE001"]
        # Simulate another client already connected, so this connection's own
        # connect-time check sees client_count go 1->2 (not 0->1) and skips the
        # line-573 guard entirely, leaving this stale task untouched.
        server.client_count = 1
        server._idle_task = asyncio.create_task(server._idle_linger())
        await asyncio.sleep(0)
        stale = server._idle_task

        orig_serve = server._serve

        async def _serve_and_drop_other_client(reader, writer, peer_ip):
            # The "other" client disconnects while this one is still being served.
            server.client_count -= 1
            await orig_serve(reader, writer, peer_ip)

        server._serve = _serve_and_drop_other_client

        resp = await _client_request(
            port, b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
        )
        assert resp.startswith(b"RTSP/1.0 200 OK")

        # _idle_linger swallows CancelledError (`except ...: return`), so it
        # completes normally rather than reporting .cancelled() — the real
        # proof it was actually cancelled is that this returns almost
        # instantly instead of hanging for the full 100s idle_timeout.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(stale, timeout=2.0)
        assert calls == []  # the stale linger's on_idle never fired
        assert server.client_count == 0
        assert server._idle_task is not None
        assert server._idle_task is not stale


# ─────────────────────────────────────────────────────────────────────────────
# _serve: first-request body read error path (lines 557-564 / 569-571)
# ─────────────────────────────────────────────────────────────────────────────


async def test_serve_body_read_incomplete_closes_connection(runner):
    """If reading the Content-Length body fails mid-stream, connection is closed."""
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camUUUUUU", FrontDoorConfig(), _resolver(target)
        )

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Send a head with Content-Length: 100 but close the connection before body
        head = b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\nContent-Length: 100\r\n\r\n"
        writer.write(head)
        await writer.drain()
        writer.close()  # close before sending body → IncompleteReadError on server

        resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        assert resp == b""  # server closed cleanly, no RTSP response


# ─────────────────────────────────────────────────────────────────────────────
# _CameraServer.stop: OSError branch (lines 513-515)
# ─────────────────────────────────────────────────────────────────────────────


def test_camera_server_close_is_sync():
    """_CameraServer.close() stops accepting (sync, best-effort)."""
    cfg = FrontDoorConfig()

    async def _resolve(_cam_id: str) -> InnerTarget | None:
        return None

    server = fe._CameraServer("camVVVVVV", cfg, _resolve, None, None)

    mock_server = MagicMock()
    mock_server.sockets = [MagicMock()]
    mock_server.sockets[0].getsockname.return_value = ("127.0.0.1", 9999)
    mock_server.close = MagicMock()
    server._server = mock_server

    server.close()
    mock_server.close.assert_called_once()
    assert server._server is None


# ─────────────────────────────────────────────────────────────────────────────
# FrontDoorRunner.stop_server: broad-exception branch (lines 716-717)
# ─────────────────────────────────────────────────────────────────────────────


async def test_runner_stop_server_calls_close_and_removes(runner):
    """stop_server calls _CameraServer.close() and removes it from the registry."""
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        await runner.start_server("camWWWWWW", FrontDoorConfig(), _resolver(target))
        assert runner.has_server("camWWWWWW")

        runner.stop_server("camWWWWWW")
        assert not runner.has_server("camWWWWWW")

        # Idempotent: second stop_server is a no-op
        runner.stop_server("camWWWWWW")


# ─────────────────────────────────────────────────────────────────────────────
# _serve: first-request read fails (timeout / incomplete) → close (lines 557-564)
# ─────────────────────────────────────────────────────────────────────────────


async def test_serve_first_read_incomplete_closes_connection(runner):
    """If readuntil raises IncompleteReadError (EOF before \\r\\n\\r\\n), close quietly."""
    async with FakeCamera() as cam:
        target = InnerTarget(cam.port, "user", "pass")
        port = await runner.start_server(
            "camXXXXXX2", FrontDoorConfig(), _resolver(target)
        )

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        # Send a partial request and immediately close — triggers IncompleteReadError
        writer.write(b"DESCRIBE rtsp://h/t RTSP/1.0\r\n")
        await writer.drain()
        writer.close()
        resp = await asyncio.wait_for(reader.read(4096), timeout=5.0)
        assert resp == b""


# ─────────────────────────────────────────────────────────────────────────────
# _pipe_client_to_inner: ConnectionError/OSError exception path (lines 403-404)
# ─────────────────────────────────────────────────────────────────────────────


async def test_pipe_client_to_inner_connection_error_handled():
    """_pipe_client_to_inner catches ConnectionError raised during read."""
    from unittest.mock import AsyncMock, MagicMock, patch

    written: list[bytes] = []

    class _FakeInnerWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camYYYYYY"
    relay._iw = _FakeInnerWriter()
    relay._challenge = {"realm": "bosch", "nonce": "abc123"}
    relay._target = InnerTarget(9999, "user", "pass")
    relay._ir = MagicMock()
    relay._cw = MagicMock()
    relay._cw.is_closing.return_value = False

    # Make the client reader raise ConnectionError on the first read
    mock_reader = MagicMock()
    mock_reader.read = AsyncMock(side_effect=ConnectionError("connection reset"))
    relay._cr = mock_reader

    # Must complete without raising
    await relay._pipe_client_to_inner()


# ─────────────────────────────────────────────────────────────────────────────
# _drain_requests: else branch — no challenge (line 447)
# ─────────────────────────────────────────────────────────────────────────────


async def test_drain_requests_no_challenge_forwards_raw():
    """When _challenge is None, requests are forwarded without auth injection."""
    written: list[bytes] = []

    class _FakeWriter:
        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camZZZZZZ"
    relay._iw = _FakeWriter()
    relay._challenge = None  # no challenge → else branch
    relay._target = InnerTarget(9999, "user", "pass")

    buf = b"SETUP rtsp://127.0.0.1/rtsp_tunnel/t RTSP/1.0\r\nCSeq: 2\r\n\r\n"
    tail = await relay._drain_requests(buf)
    assert tail == b""
    combined = b"".join(written)
    assert b"SETUP" in combined
    assert b"Authorization:" not in combined


# ─────────────────────────────────────────────────────────────────────────────
# _pipe_inner_to_client: ConnectionError/OSError exception path (lines 461-462)
# ─────────────────────────────────────────────────────────────────────────────


async def test_pipe_inner_to_client_oserror_handled():
    """_pipe_inner_to_client catches OSError raised during inner read."""
    from unittest.mock import AsyncMock, MagicMock

    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camAAAAAAA"
    relay._cw = MagicMock()
    relay._cw.is_closing.return_value = False

    mock_inner_reader = MagicMock()
    mock_inner_reader.read = AsyncMock(side_effect=OSError("broken pipe"))
    relay._ir = mock_inner_reader

    # Must complete without raising
    await relay._pipe_inner_to_client()


# ─────────────────────────────────────────────────────────────────────────────
# _relay.run finally: writer.close() when not already closing (line 329)
# ─────────────────────────────────────────────────────────────────────────────


async def test_relay_run_finally_closes_writers():
    """_Relay.run finally block calls close() on writers that are not already closing."""
    inner_closed: list[bool] = []
    client_closed: list[bool] = []

    class _FakeInnerWriter:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            inner_closed.append(True)

    class _FakeClientWriter:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            pass

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            client_closed.append(True)

    mock_cr = MagicMock()
    mock_cr.read = AsyncMock(return_value=b"")  # EOF immediately

    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camBBBBBBB"
    relay._cr = mock_cr
    relay._cw = _FakeClientWriter()
    relay._target = InnerTarget(9999, "user", "pass")
    relay._challenge = None
    relay._first = b"DESCRIBE rtsp://h/t RTSP/1.0\r\nCSeq: 1\r\n\r\n"

    fake_iw = _FakeInnerWriter()
    fake_ir = MagicMock()
    fake_ir.read = AsyncMock(return_value=b"")

    with patch(
        "custom_components.bosch_shc_camera.frigate_endpoint.asyncio.open_connection",
        return_value=(fake_ir, fake_iw),
    ):
        relay._auth_dance = AsyncMock(return_value=None)
        relay._pipe_client_to_inner = AsyncMock(return_value=None)
        relay._pipe_inner_to_client = AsyncMock(return_value=None)
        await relay.run()

    assert inner_closed  # inner writer was closed in finally
    assert client_closed  # client writer was closed in finally


# ─────────────────────────────────────────────────────────────────────────────
# _serve: 503 drain OSError (lines 620-621)
# ─────────────────────────────────────────────────────────────────────────────


async def test_serve_503_drain_oserror_swallowed():
    """When writer.drain() raises OSError after writing 503, it is swallowed (620-621)."""
    # Call _CameraServer._serve directly with a mock writer whose drain raises OSError.
    # This avoids patching asyncio.StreamWriter.drain globally (which breaks the client).
    written: list[bytes] = []

    class _ClosingWriter:
        """Fake writer: drain raises OSError; records write calls."""

        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            raise OSError("simulated broken pipe")

        def is_closing(self) -> bool:
            return False

        def close(self) -> None:
            pass

        def get_extra_info(self, key: str, default=None):  # type: ignore[override]
            return ("127.0.0.1", 9999) if key == "peername" else default

    async def _resolve_none(_cam_id: str) -> InnerTarget | None:
        return None

    server = fe._CameraServer(
        "camDDDDDDD", FrontDoorConfig(), _resolve_none, None, None
    )

    # Build a StreamReader that has the request buffered
    reader = asyncio.StreamReader()
    request = b"DESCRIBE rtsp://127.0.0.1/rtsp_tunnel RTSP/1.0\r\nCSeq: 1\r\n\r\n"
    reader.feed_data(request)
    reader.feed_eof()

    # _serve must not raise even though drain raises OSError on the 503 response
    await server._serve(reader, _ClosingWriter(), "127.0.0.1")  # type: ignore[arg-type]

    # The 503 bytes were written before the drain raised
    combined = b"".join(written)
    assert b"503" in combined


# ─────────────────────────────────────────────────────────────────────────────
# ip_allowed: IPv4-mapped IPv6 branch (::ffff:x.x.x.x)
# ─────────────────────────────────────────────────────────────────────────────


def test_ip_allowed_ipv4_mapped_ipv6():
    """IPv4-mapped IPv6 address (::ffff:x.x.x.x) matches an IPv4 allowlist entry."""
    # A dual-stack bind reports IPv4 clients as IPv4-mapped IPv6
    assert ip_allowed("::ffff:192.168.1.5", frozenset({"192.168.1.5"})) is True
    assert ip_allowed("::ffff:10.0.0.1", frozenset({"192.168.1.5"})) is False


# ─────────────────────────────────────────────────────────────────────────────
# _read_message: TimeoutError / LimitOverrunError → ConnectionError
# ─────────────────────────────────────────────────────────────────────────────


async def test_read_message_timeout_raises_connection_error():
    """_read_message converts TimeoutError to ConnectionError."""
    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camTIMEOUT"

    mock_reader = MagicMock()
    mock_reader.readuntil = AsyncMock(side_effect=TimeoutError("timed out"))

    with pytest.raises(ConnectionError):
        await relay._read_message(mock_reader)


async def test_read_message_limit_overrun_raises_connection_error():
    """_read_message converts LimitOverrunError to ConnectionError."""
    relay = fe._Relay.__new__(fe._Relay)
    relay._cam = "camOVERRUN"

    mock_reader = MagicMock()
    mock_reader.readuntil = AsyncMock(
        side_effect=asyncio.LimitOverrunError("too big", 0)
    )

    with pytest.raises(ConnectionError):
        await relay._read_message(mock_reader)


# ─────────────────────────────────────────────────────────────────────────────
# _CameraServer.close(): no-op when _server is None
# ─────────────────────────────────────────────────────────────────────────────


def test_camera_server_close_noop_when_no_server():
    """close() is a no-op when _server is None."""

    async def _resolve(_cam_id: str) -> InnerTarget | None:
        return None

    server = fe._CameraServer("camNOSRV", FrontDoorConfig(), _resolve, None, None)
    server._server = None
    server.close()  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# Section: connection-cap rejection (relocated from
# tests/test_coverage_gates_v14.py)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_front_door_rejects_client_when_connection_cap_reached() -> None:
    """`_CameraServer._handle` closes the writer immediately when all
    semaphore slots (max_connections) are already taken."""
    config = FrontDoorConfig(max_connections=1)
    server = fe._CameraServer(
        cam_id="11111111-1111-1111-1111-111111111111",
        config=config,
        resolve_inner=AsyncMock(),
        on_active=None,
        on_idle=None,
    )

    # Exhaust the semaphore so _sem.locked() returns True.
    await server._sem.acquire()

    writer = MagicMock()
    writer.get_extra_info = MagicMock(return_value=("127.0.0.1", 5000))
    writer.close = MagicMock()

    await server._handle(MagicMock(), writer)

    writer.close.assert_called_once()

    # Restore semaphore so it isn't leaked.
    server._sem.release()
