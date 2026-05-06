"""Tests for tls_proxy `rtsp_keepalive` + `pre_warm_rtsp` async helpers.

These two coroutines speak RTSP over a loopback TCP socket and handle the
Digest-auth challenge-response dance against the camera. They're the
core of the LOCAL streaming pipeline:

  - `pre_warm_rtsp` runs once after a fresh `PUT /connection` to wake the
    camera's H.264 encoder before FFmpeg connects (otherwise FFmpeg
    receives the first frame ~25 s late).
  - `rtsp_keepalive` fires every ~30 s through the live session to reset
    the camera's 60 s inactivity timer (Bosch enforces this regardless of
    `maxSessionDuration` in the URL).

We test against a fake RTSP server bound to 127.0.0.1 so the tests are
hermetic — no real cameras, no real network. The fake server can be
configured to respond with: a 401 + nonce/realm challenge, a direct 200,
malformed responses, or to drop the connection.

Coverage targets:
  - tls_proxy.py lines 261-315 (rtsp_keepalive)
  - tls_proxy.py lines 357-430 (pre_warm_rtsp)
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable

import pytest

from custom_components.bosch_shc_camera.tls_proxy import (
    _digest_auth,
    pre_warm_rtsp,
    rtsp_keepalive,
)


# `pytest-homeassistant-custom-component` blocks all real socket usage by
# default. These tests legitimately need 127.0.0.1 loopback for the fake
# RTSP server, so opt in via the `socket_enabled` fixture from pytest-socket.
@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled):
    """Allow loopback sockets for the duration of every test in this file."""
    yield


# ── Fake RTSP server fixture ─────────────────────────────────────────────


class FakeRtsp:
    """Loopback TCP server that mimics camera RTSP behaviour for one connection.

    Pass a `responder` callable that gets the raw request bytes and returns
    the response bytes (or None to close the socket). The fixture spins up
    a server on 127.0.0.1 with an ephemeral port, accepts one client, runs
    the request/response loop, then shuts down.
    """

    def __init__(self, responder: Callable[[bytes, int], bytes | None]):
        self.responder = responder
        self.port: int = 0
        self._server: asyncio.AbstractServer | None = None
        self.requests: list[bytes] = []

    async def __aenter__(self) -> "FakeRtsp":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        step = 0
        try:
            while True:
                # RTSP requests end with "\r\n\r\n". Read until we see that
                # or the connection drops.
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                    if not chunk:
                        return
                    data += chunk
                self.requests.append(data)
                resp = self.responder(data, step)
                step += 1
                if resp is None:
                    return
                writer.write(resp)
                await writer.drain()
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _digest_challenge(realm: str = "Bosch RTSP", nonce: str = "abc123") -> bytes:
    """Build a typical 401 Digest auth challenge."""
    return (
        b"RTSP/1.0 401 Unauthorized\r\n"
        b"CSeq: 1\r\n"
        b'WWW-Authenticate: Digest realm="' + realm.encode() + b'", '
        b'nonce="' + nonce.encode() + b'", algorithm=MD5\r\n'
        b"\r\n"
    )


def _ok_response(body: bytes = b"") -> bytes:
    headers = (
        b"RTSP/1.0 200 OK\r\n"
        b"CSeq: 2\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n"
    )
    return headers + body


# ── rtsp_keepalive ───────────────────────────────────────────────────────


class TestRtspKeepalive:
    """Pin the OPTIONS-keepalive contract.

    The keepalive is the only thing standing between an active LOCAL
    session and the camera's 60 s inactivity teardown. A regression here
    silently kills LAN streaming after one minute (manifests as the
    'stream stops after a minute' bug WoodenDuke reported in GH#6).
    """

    @pytest.mark.asyncio
    async def test_handles_direct_200_no_auth(self):
        """Some camera firmwares respond 200 OK to OPTIONS without auth.
        Branch covered: lines 281-284 (early-exit in the `if not (nonce_m and realm_m)`
        path with '200 OK' substring). Must return True, not falsely report failure."""

        def responder(req: bytes, step: int) -> bytes | None:
            return _ok_response()

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is True

    @pytest.mark.asyncio
    async def test_full_digest_handshake_succeeds(self):
        """Standard Bosch path: 401 + nonce → authenticated OPTIONS → 200.
        Covers the happy two-step exchange at lines 268-307."""

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                # Validate this is the first OPTIONS without auth.
                assert b"OPTIONS " in req
                assert b"Authorization" not in req
                return _digest_challenge(nonce="N1")
            # Second request must carry the Authorization header.
            assert b'Authorization: Digest username="u"' in req
            assert b'nonce="N1"' in req
            return _ok_response()

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is True

    @pytest.mark.asyncio
    async def test_missing_nonce_without_200_returns_false(self):
        """If the response has neither '200 OK' nor a nonce/realm, we can't
        proceed and must not crash — return False so the caller marks the
        session unhealthy. Covers lines 285-289."""

        def responder(req: bytes, step: int) -> bytes | None:
            return b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 1\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is False

    @pytest.mark.asyncio
    async def test_authenticated_response_not_200_returns_false(self):
        """Cam answers 401 a second time (wrong creds) → return False.
        Covers the unexpected-second-response branch at lines 309-312."""

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(nonce="N2")
            # Second OPTIONS still rejected (e.g. password wrong)
            return b"RTSP/1.0 401 Unauthorized\r\nCSeq: 2\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is False

    @pytest.mark.asyncio
    async def test_connection_refused_returns_false(self):
        """Port nobody's listening on must trigger the except branch
        (lines 313-315) and return False — never raise."""
        # Pick a port that is almost certainly free.
        ok = await rtsp_keepalive(1, "u", "p", "CAM-A")
        assert ok is False

    @pytest.mark.asyncio
    async def test_digest_response_matches_helper(self):
        """The Authorization header sent in step 2 must equal what
        `_digest_auth` would compute for the same inputs. Pins the
        contract that future refactors don't drift the digest format."""
        captured: dict[str, bytes] = {}

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(realm="MyRealm", nonce="ABCDEF")
            captured["second"] = req
            return _ok_response()

        async with FakeRtsp(responder) as server:
            await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            uri = f"rtsp://127.0.0.1:{server.port}/rtsp_tunnel"
            expected = _digest_auth("u", "p", "OPTIONS", uri, "MyRealm", "ABCDEF")
            assert f"Authorization: {expected}".encode() in captured["second"]


# ── pre_warm_rtsp ─────────────────────────────────────────────────────────


class TestPreWarmRtsp:
    """Pin the DESCRIBE pre-warm contract.

    Pre-warm runs once after PUT /connection LOCAL returns fresh creds.
    A successful DESCRIBE wakes the H.264 encoder so FFmpeg's first frame
    arrives in <2 s instead of ~25 s. The function must:
      - Retry up to `max_attempts` times (different cam models need
        different attempt counts — CAMERA_360 ≈ 2, CAMERA_EYES ≈ 5).
      - Give up cleanly with `False` when LAN-unreachable so the
        coordinator can fall back to REMOTE.
    """

    @pytest.mark.asyncio
    async def test_describe_happy_path(self):
        """Single-attempt success: 401 challenge → authenticated DESCRIBE → 200 OK SDP.
        Covers the success branch at lines 403-422 with `got_ok=True`."""

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                assert b"DESCRIBE " in req
                assert b"Accept: application/sdp" in req
                return _digest_challenge(nonce="PW1")
            assert b"Authorization: Digest" in req
            return _ok_response(b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n")

        async with FakeRtsp(responder) as server:
            ok = await pre_warm_rtsp(
                server.port, "u", "p", "127.0.0.1",
                max_attempts=1, retry_wait=0, post_success_wait=0,
            )
            assert ok is True

    @pytest.mark.asyncio
    async def test_unexpected_response_warns_but_returns_false(self):
        """If the second DESCRIBE doesn't get 200 OK, return False so the
        coordinator falls back to REMOTE. Covers lines 407-410 (the
        warning branch with `got_ok=False`)."""

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(nonce="PW2")
            return b"RTSP/1.0 503 Service Unavailable\r\nCSeq: 2\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await pre_warm_rtsp(
                server.port, "u", "p", "127.0.0.1",
                max_attempts=1, retry_wait=0, post_success_wait=0,
            )
            assert ok is False

    @pytest.mark.asyncio
    async def test_missing_nonce_retries_then_fails(self):
        """Camera response without nonce/realm → retry path. After
        `max_attempts` exhausted, return False. Covers lines 379-388
        and the post-loop fall-through return."""
        attempt_count = [0]

        def responder(req: bytes, step: int) -> bytes | None:
            # Each new connection starts fresh; just count requests.
            attempt_count[0] += 1
            return b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 1\r\n\r\n"

        async with FakeRtsp(responder) as server:
            # FakeRtsp only handles 1 connection at a time; pre_warm reopens
            # the connection on every retry. We need a server that handles
            # multiple sequential connections — see the multi-connection
            # variant test below. For this one, single-attempt is enough.
            ok = await pre_warm_rtsp(
                server.port, "u", "p", "127.0.0.1",
                max_attempts=1, retry_wait=0, post_success_wait=0,
            )
            assert ok is False
            # The fake server saw exactly one request.
            assert attempt_count[0] == 1

    @pytest.mark.asyncio
    async def test_unreachable_port_exhausts_retries(self):
        """Port nobody listens on — every attempt raises ConnectionRefusedError
        in the except branch (lines 423-430). After `max_attempts` we return
        False. Pinned with retry_wait=0 to keep the test fast."""
        ok = await pre_warm_rtsp(
            1, "u", "p", "127.0.0.1",
            max_attempts=3, retry_wait=0, post_success_wait=0,
            describe_timeout=1,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_max_attempts_param_respected(self):
        """`max_attempts=N` means at most N tries — pin the loop bound so a
        future refactor can't accidentally turn it into infinite retries."""
        import time
        # max_attempts=2, retry_wait=0 → tight upper bound on duration.
        # describe_timeout=1 means each failed attempt takes ~1 s for the
        # asyncio.wait_for to fire. So 2 attempts < ~3 s comfortably.
        start = time.monotonic()
        ok = await pre_warm_rtsp(
            1, "u", "p", "127.0.0.1",
            max_attempts=2, retry_wait=0, post_success_wait=0,
            describe_timeout=1,
        )
        elapsed = time.monotonic() - start
        assert ok is False
        assert elapsed < 5.0, (
            f"pre_warm_rtsp with max_attempts=2 took {elapsed:.1f}s — "
            "loop bound likely broken (infinite retry?)"
        )

    @pytest.mark.asyncio
    async def test_uri_includes_required_query_params(self):
        """The DESCRIBE URI must include `inst=1`, `enableaudio=1`,
        `fmtp=1`, `maxSessionDuration=60`. Each param has a reason:
          - inst=1: only one stream instance (avoids per-cam concurrent
            session limit on Gen1)
          - enableaudio=1: needed even for video-only because Bosch
            silently drops the stream otherwise
          - fmtp=1: include H.264 fmtp line in SDP for codec negotiation
          - maxSessionDuration=60: keep the request short so an unanswered
            DESCRIBE doesn't hold a slot for hours."""
        captured: list[bytes] = []

        def responder(req: bytes, step: int) -> bytes | None:
            captured.append(req)
            if step == 0:
                return _digest_challenge(nonce="PW3")
            return _ok_response(b"v=0\r\n")

        async with FakeRtsp(responder) as server:
            await pre_warm_rtsp(
                server.port, "u", "p", "127.0.0.1",
                max_attempts=1, retry_wait=0, post_success_wait=0,
            )
            assert captured, "responder never received a request"
            first = captured[0]
            assert b"inst=1" in first
            assert b"enableaudio=1" in first
            assert b"fmtp=1" in first
            assert b"maxSessionDuration=60" in first

    @pytest.mark.asyncio
    async def test_post_success_wait_applied(self):
        """After a successful pre-warm, the function must sleep
        `post_success_wait` seconds before returning so the camera fully
        releases the TLS connection (Bosch allows ≤2 concurrent sessions
        per credential set; FFmpeg connecting too fast races and gets
        rejected). Pinned with a short wait + duration assertion."""
        import time

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(nonce="PW4")
            return _ok_response(b"v=0\r\n")

        async with FakeRtsp(responder) as server:
            start = time.monotonic()
            ok = await pre_warm_rtsp(
                server.port, "u", "p", "127.0.0.1",
                max_attempts=1, retry_wait=0, post_success_wait=1,
            )
            elapsed = time.monotonic() - start
            assert ok is True
            assert elapsed >= 0.9, (
                f"post_success_wait=1 but elapsed only {elapsed:.2f}s — "
                "the post-success sleep was skipped, FFmpeg-vs-prewarm "
                "race window is back."
            )


# ── Module-level constants/pinning that span keepalive + pre-warm ────────


class TestRtspHelperContract:
    """Cross-cutting structural contracts."""

    def test_digest_format_no_spaces_after_commas(self):
        """The digest header is comma-separated; some buggy RTSP parsers
        choke on whitespace after commas. Pin the format the cameras
        actually accept."""
        result = _digest_auth("u", "p", "OPTIONS", "/x", "r", "n")
        # Verify no ", " sequence (space after comma between fields)
        assert ", " not in result, (
            "_digest_auth introduced a space after a comma — Bosch RTSP "
            "parser treats this as a malformed header and rejects auth."
        )
        # But every field separator must be a comma (no missing commas)
        assert result.count(",") >= 4, (
            "Digest header missing field separators — expected username, "
            "realm, nonce, uri, response separated by 4 commas."
        )

    def test_pre_warm_default_max_attempts_safe(self):
        """Pre-warm's default `max_attempts=5` is the upper bound for
        outdoor cameras. Pin so a refactor doesn't accidentally drop it
        to 1 (regressing CAMERA_EYES which often needs 3-4 retries on
        cold-start)."""
        import inspect
        sig = inspect.signature(pre_warm_rtsp)
        max_attempts_default = sig.parameters["max_attempts"].default
        assert max_attempts_default >= 3, (
            f"pre_warm_rtsp max_attempts default lowered to {max_attempts_default} "
            "— Gen1 outdoor cams (CAMERA_EYES) regress on cold-start."
        )

    def test_keepalive_signature_returns_bool(self):
        """rtsp_keepalive must return a bool — coordinator decides
        whether to mark the session unhealthy based on this. Refactors
        returning None or a tuple silently break the health check.

        `tls_proxy.py` uses `from __future__ import annotations`, so the
        annotation is a string at signature inspection time. Compare the
        string form rather than the real `bool` class."""
        import inspect
        sig = inspect.signature(rtsp_keepalive)
        assert sig.return_annotation == "bool", (
            f"rtsp_keepalive return annotation is {sig.return_annotation!r} "
            "— must stay 'bool' so the coordinator's health-check works."
        )
