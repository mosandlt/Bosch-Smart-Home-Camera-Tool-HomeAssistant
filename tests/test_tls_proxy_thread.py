"""Tests for tls_proxy.py threading/proxy-loop internals.

Target coverage:
  - lines 75-205, 210-211: daemon thread body (_proxy_thread_with_signal,
    _proxy_thread, circuit breaker, _pipe relay)
  - lines 386-387: pre_warm_rtsp retry-sleep path (no nonce, retries remain)
  - lines 415-416: pre_warm_rtsp wait_closed exception swallowed

Uses real loopback sockets where needed (opt-in via socket_enabled fixture).

Thread-cleanup contract:
  pytest-homeassistant-custom-component's verify_cleanup fixture asserts that
  no new threads remain alive after each test (except _DummyThread and
  waitpid-* threads).  We satisfy this by:
  1. Collecting threads running before the proxy starts.
  2. Calling stop_tls_proxy() to close the server socket (makes the proxy
     thread exit its accept() loop via OSError).
  3. Joining any new threads with a 2-second timeout before returning.

  Helper echo servers use asyncio (started/stopped inside async tests) to
  avoid spawning additional OS threads.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.tls_proxy import (
    _proxy_servers,
    pre_warm_rtsp,
    start_tls_proxy,
    stop_tls_proxy,
)


# Enable loopback sockets for every test in this file.
@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled):
    """Allow loopback sockets for every test in this file."""
    yield


# ── helpers ───────────────────────────────────────────────────────────────────

class _FakeTlsSocket:
    """Thin wrapper that makes a plain TCP socket look like an SSL socket.

    The real proxy calls tls.version() and tls.cipher() after wrap_socket
    returns. A raw socket object has neither; this wrapper adds them while
    delegating all actual socket operations to the underlying raw socket.
    """

    def __init__(self, raw: socket.socket):
        self._raw = raw

    def version(self):
        return "TLSv1.3"

    def cipher(self):
        return ("AES128-GCM-SHA256", "TLSv1.3", 256)

    # Delegate socket operations
    def recv(self, n):
        return self._raw.recv(n)

    def sendall(self, data):
        return self._raw.sendall(data)

    def close(self):
        return self._raw.close()

    def fileno(self):
        return self._raw.fileno()

    def setsockopt(self, *args):
        return self._raw.setsockopt(*args)

    def settimeout(self, t):
        return self._raw.settimeout(t)

    def getsockname(self):
        return self._raw.getsockname()


def _plain_ssl_ctx():
    """SSL context mock: wrap_socket returns a _FakeTlsSocket that has
    .version() and .cipher() but delegates all data I/O to the raw socket.
    """
    ctx = MagicMock()
    ctx.wrap_socket = lambda raw, **kwargs: _FakeTlsSocket(raw)
    return ctx


def _join_new_threads(threads_before: frozenset, timeout: float = 3.0) -> None:
    """Join any threads that were not running before the proxy was started.

    Call this after stop_tls_proxy() so the proxy's daemon thread has a chance
    to exit before PHACC's verify_cleanup fixture runs.

    Loops until all new threads have exited or the timeout expires.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new_threads = frozenset(threading.enumerate()) - threads_before
        alive = [t for t in new_threads
                 if t.is_alive()
                 and not isinstance(t, threading._DummyThread)
                 and not t.name.startswith("waitpid-")
                 and "_run_safe_shutdown_loop" not in t.name]
        if not alive:
            break
        for t in alive:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                t.join(timeout=min(0.2, remaining))
        time.sleep(0.01)


def _start_proxy(cam_id, cam_host, cam_port, debug=False):
    """Thin wrapper: record threads-before, start proxy, return (port, cache, threads_before)."""
    threads_before = frozenset(threading.enumerate())
    port_cache: dict[str, int] = {}
    ctx = _plain_ssl_ctx()
    port = start_tls_proxy(ctx, cam_id, cam_host, cam_port, port_cache, debug=debug)
    return port, port_cache, threads_before


# ── FakeRtsp (asyncio-based, single-use per connection) ──────────────────────

class FakeRtsp:
    """Asyncio loopback server that mimics camera RTSP responses."""

    def __init__(self, responder=None):
        self.responder = responder or (lambda req, step: None)
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

    async def _handle(self, reader, writer):
        step = 0
        try:
            while True:
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
        except (asyncio.TimeoutError, Exception):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


def _digest_challenge(realm: str = "Bosch", nonce: str = "abc123") -> bytes:
    return (
        b"RTSP/1.0 401 Unauthorized\r\n"
        b"CSeq: 1\r\n"
        b'WWW-Authenticate: Digest realm="' + realm.encode() + b'", '
        b'nonce="' + nonce.encode() + b'"\r\n'
        b"\r\n"
    )


def _ok_response(body: bytes = b"") -> bytes:
    return (
        b"RTSP/1.0 200 OK\r\n"
        b"CSeq: 2\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n"
    ) + body


# ── TestProxyThreadLifecycle ──────────────────────────────────────────────────


class TestProxyThreadLifecycle:
    """Covers lines 209-228: thread startup, ready.set(), port_cache update."""

    def test_thread_sets_ready_and_enters_accept_loop(self):
        """Proxy thread starts, signals ready, enters accept loop.
        Port is written into cache; cam_id is in _proxy_servers.
        After stop, cam_id is removed from both.
        """
        cam_id = "TEST-CAM-LIFECYCLE-1"
        port, port_cache, threads_before = _start_proxy(cam_id, "127.0.0.1", 1)

        try:
            assert port > 0, "Must return a valid port number"
            assert port_cache.get(cam_id) == port, "Port must be written into port_cache"
            assert cam_id in _proxy_servers, "Server socket must be in _proxy_servers"
        finally:
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

        assert cam_id not in port_cache, "cam_id must be removed from port_cache after stop"
        assert cam_id not in _proxy_servers, "cam_id must be removed from _proxy_servers after stop"

    def test_proxy_exits_cleanly_when_server_closed(self):
        """Start proxy then immediately stop it; thread must exit without errors."""
        cam_id = "TEST-CAM-LIFECYCLE-2"
        port, port_cache, threads_before = _start_proxy(cam_id, "127.0.0.1", 1)
        stop_tls_proxy(cam_id, port_cache)
        _join_new_threads(threads_before)

    def test_two_proxies_use_different_ports(self):
        """Two cameras get separate proxy ports — no collision in port_cache."""
        cam_a = "CAM-PORT-A"
        cam_b = "CAM-PORT-B"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        port_a = start_tls_proxy(ctx, cam_a, "127.0.0.1", 1, port_cache)
        port_b = start_tls_proxy(ctx, cam_b, "127.0.0.1", 1, port_cache)

        try:
            assert port_a != port_b, "Each camera must get its own proxy port"
            assert port_cache[cam_a] == port_a
            assert port_cache[cam_b] == port_b
        finally:
            stop_tls_proxy(cam_a, port_cache)
            stop_tls_proxy(cam_b, port_cache)
            _join_new_threads(threads_before)

    def test_restart_replaces_existing_proxy(self):
        """Starting a second proxy for the same cam_id tears down the first."""
        cam_id = "CAM-RESTART"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        start_tls_proxy(ctx, cam_id, "127.0.0.1", 1, port_cache)
        port2 = start_tls_proxy(ctx, cam_id, "127.0.0.1", 1, port_cache)

        try:
            # New port assigned; cam_id maps to port2
            assert port_cache[cam_id] == port2
        finally:
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)


# ── TestCircuitBreaker ────────────────────────────────────────────────────────


class TestCircuitBreaker:
    """Covers lines 104-129: burst-failure circuit breaker logic."""

    def _trigger_n_failures(self, proxy_port: int, n: int) -> None:
        """Connect to proxy n times; each connection attempt results in a
        failed upstream connect (cam is unreachable) which increments
        fail_count inside the proxy thread.

        NOTE: Do NOT use socket.create_connection() here — it is patched to
        raise ConnectionRefusedError in both the proxy AND the test context
        (same socket module object). Use socket.socket().connect() directly
        to bypass the patched create_connection.
        """
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.settimeout(2)
                s.connect(("127.0.0.1", proxy_port))
            except OSError:
                pass
            finally:
                try:
                    s.close()
                except OSError:
                    pass
            # Brief pause so the proxy thread processes each accept() in sequence.
            time.sleep(0.15)

    def test_5_consecutive_failures_close_server(self):
        """After 5 connect failures within the burst window, the proxy closes
        its server socket — the proxy thread exits (circuit breaker fired).
        """
        cam_id = "CAM-CB-CLOSE"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        # Patch create_connection so every upstream attempt fails instantly
        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
            side_effect=ConnectionRefusedError("camera offline"),
        ):
            port = start_tls_proxy(ctx, cam_id, "127.0.0.1", 9999, port_cache)

            # Snapshot the proxy thread so we can join it directly
            proxy_thread = next(
                (t for t in frozenset(threading.enumerate()) - threads_before
                 if t.name.startswith("tls_proxy_")),
                None,
            )

            try:
                self._trigger_n_failures(port, 5)

                # Wait for the proxy thread to exit (circuit breaker fires, thread returns)
                if proxy_thread is not None:
                    proxy_thread.join(timeout=5.0)

                circuit_breaker_fired = (
                    proxy_thread is None or not proxy_thread.is_alive()
                )
                assert circuit_breaker_fired, (
                    "After 5 consecutive failures the proxy thread must exit "
                    "(circuit breaker did not fire)"
                )
            finally:
                # Circuit breaker already closed the srv socket; clean up cache
                port_cache.pop(cam_id, None)
                _proxy_servers.pop(cam_id, None)
                # Ensure thread is fully gone before teardown
                if proxy_thread is not None:
                    proxy_thread.join(timeout=2.0)

    def test_less_than_5_failures_do_not_close_server(self):
        """4 consecutive failures → circuit breaker must NOT fire; proxy thread stays alive."""
        cam_id = "CAM-CB-OPEN"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
            side_effect=ConnectionRefusedError("camera offline"),
        ):
            port = start_tls_proxy(ctx, cam_id, "127.0.0.1", 9999, port_cache)

            # Snapshot the proxy thread
            proxy_thread = next(
                (t for t in frozenset(threading.enumerate()) - threads_before
                 if t.name.startswith("tls_proxy_")),
                None,
            )

            try:
                self._trigger_n_failures(port, 4)

                # Wait briefly, then verify the proxy thread is still alive
                # (circuit breaker has NOT fired — only fires at 5 failures)
                time.sleep(0.3)
                thread_still_alive = (
                    proxy_thread is not None and proxy_thread.is_alive()
                )
                assert thread_still_alive, (
                    "4 consecutive failures must NOT trigger the circuit breaker — "
                    "proxy thread must still be alive"
                )
            finally:
                stop_tls_proxy(cam_id, port_cache)
                _join_new_threads(threads_before)


# ── TestPipeRelay ────────────────────────────────────────────────────────────


class TestPipeRelay:
    """Covers lines 141-205: _pipe() relay, Transport rewrite, debug logging.

    These tests use asyncio echo servers (started within the async test's
    event loop) instead of OS threads, so no additional threads are spawned
    and PHACC's thread-cleanup check is satisfied.
    """

    @pytest.mark.asyncio
    async def test_pipe_relays_data_from_client_to_camera(self):
        """Bytes sent by the client arrive at the camera (asyncio echo server)."""
        cam_id = "CAM-PIPE-C2CAM"
        threads_before = frozenset(threading.enumerate())
        received: list[bytes] = []

        async def _echo_handle(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=2.0)
                if data:
                    received.append(data)
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(_echo_handle, "127.0.0.1", 0)
        echo_port = srv.sockets[0].getsockname()[1]

        port, port_cache, _ = _start_proxy(cam_id, "127.0.0.1", echo_port)
        try:
            # Give the proxy thread a moment to enter accept()
            await asyncio.sleep(0.05)

            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            c.sendall(b"HELLO_CAM\r\n\r\n")
            await asyncio.sleep(0.5)
            c.close()

            deadline = time.monotonic() + 2.0
            while not received and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            assert received, "Client data must reach the echo server via the proxy"
            assert b"HELLO_CAM" in received[0]
        finally:
            srv.close()
            await srv.wait_closed()
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

    @pytest.mark.asyncio
    async def test_pipe_relays_data_from_camera_to_client(self):
        """Data from the camera (asyncio echo server) is relayed back to the client."""
        cam_id = "CAM-PIPE-CAM2C"
        threads_before = frozenset(threading.enumerate())

        async def _echo_handle(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=2.0)
                if data:
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(_echo_handle, "127.0.0.1", 0)
        echo_port = srv.sockets[0].getsockname()[1]

        port, port_cache, _ = _start_proxy(cam_id, "127.0.0.1", echo_port)
        try:
            await asyncio.sleep(0.05)

            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            c.settimeout(3.0)
            c.sendall(b"PING\r\n\r\n")
            await asyncio.sleep(0.3)

            reply = b""
            try:
                while True:
                    chunk = c.recv(65536)
                    if not chunk:
                        break
                    reply += chunk
            except socket.timeout:
                pass
            except OSError:
                pass
            c.close()

            assert b"PING" in reply, (
                "Echo server reply must be relayed back through the proxy to the client"
            )
        finally:
            srv.close()
            await srv.wait_closed()
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

    @pytest.mark.asyncio
    async def test_setup_rewrite_in_pipe(self):
        """RTSP SETUP with UDP Transport is rewritten to TCP interleaved before forwarding."""
        cam_id = "CAM-PIPE-SETUP"
        threads_before = frozenset(threading.enumerate())
        captured_by_cam: list[bytes] = []

        async def _cap_handle(reader, writer):
            try:
                buf = b""
                while True:
                    try:
                        chunk = await asyncio.wait_for(reader.read(65536), timeout=1.0)
                        if not chunk:
                            break
                        buf += chunk
                    except asyncio.TimeoutError:
                        break
                if buf:
                    captured_by_cam.append(buf)
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(_cap_handle, "127.0.0.1", 0)
        cap_port = srv.sockets[0].getsockname()[1]

        port, port_cache, _ = _start_proxy(cam_id, "127.0.0.1", cap_port)
        try:
            await asyncio.sleep(0.05)

            setup_request = (
                b"SETUP rtsp://127.0.0.1/stream RTSP/1.0\r\n"
                b"CSeq: 3\r\n"
                b"Transport: RTP/AVP;unicast;client_port=5000-5001\r\n"
                b"\r\n"
            )
            c = socket.create_connection(("127.0.0.1", port), timeout=2)
            c.sendall(setup_request)
            await asyncio.sleep(0.5)
            c.close()

            deadline = time.monotonic() + 2.0
            while not captured_by_cam and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            assert captured_by_cam, "Proxy must forward SETUP to the camera"
            forwarded = b"".join(captured_by_cam)
            assert b"RTP/AVP/TCP" in forwarded, (
                "UDP Transport must be rewritten to RTP/AVP/TCP"
            )
            assert b"interleaved=0-1" in forwarded, (
                "Interleaved channels 0-1 must be present after rewrite"
            )
            assert b"client_port" not in forwarded, (
                "client_port must be removed from the rewritten Transport header"
            )
        finally:
            srv.close()
            await srv.wait_closed()
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

    @pytest.mark.asyncio
    async def test_debug_logging_on_first_exchanges(self, caplog):
        """With debug=True the first exchanges are logged — no crash.
        Covers lines 164-170 (debug log path in _pipe).
        """
        import logging

        cam_id = "CAM-DEBUG-LOG"
        threads_before = frozenset(threading.enumerate())

        async def _echo_handle(reader, writer):
            try:
                data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                if data:
                    writer.write(data)
                    await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(_echo_handle, "127.0.0.1", 0)
        echo_port = srv.sockets[0].getsockname()[1]

        port, port_cache, _ = _start_proxy(cam_id, "127.0.0.1", echo_port, debug=True)
        try:
            await asyncio.sleep(0.05)
            with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera.tls_proxy"):
                c = socket.create_connection(("127.0.0.1", port), timeout=2)
                c.sendall(b"DEBUG_DATA\r\n\r\n")
                await asyncio.sleep(0.3)
                c.close()
        finally:
            srv.close()
            await srv.wait_closed()
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

        # Just verify no exception was raised — debug path (lines 164-170) was hit


# ── TestPreWarmGaps ──────────────────────────────────────────────────────────


class TestPreWarmGaps:
    """Covers lines 386-387 and 415-416 of pre_warm_rtsp."""

    @pytest.mark.asyncio
    async def test_retry_sleep_on_missing_nonce_with_retries_remaining(self):
        """Response without nonce/realm and attempt < max_attempts → sleep + continue
        (covers lines 385-387). Both attempts fail → return False.
        """
        attempt_count = [0]

        async def _handle(reader, writer):
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                    if not chunk:
                        return
                    data += chunk
                attempt_count[0] += 1
                # Respond WITHOUT nonce/realm → triggers the no-nonce branch
                writer.write(b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 1\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
        proxy_port = srv.sockets[0].getsockname()[1]

        try:
            # max_attempts=2, retry_wait=0 → line 386 (sleep) hit on attempt 1,
            # then attempt 2 also fails → returns False
            result = await pre_warm_rtsp(
                proxy_port, "u", "p", "127.0.0.1",
                max_attempts=2, retry_wait=0, post_success_wait=0,
                describe_timeout=1,
            )
            assert result is False, (
                "All attempts exhausted without nonce/realm → must return False"
            )
            # We attempted at least once (lines 385-387 path)
            assert attempt_count[0] >= 1
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_wait_closed_exception_swallowed(self):
        """writer.wait_closed() raising ConnectionResetError is swallowed by
        the except-pass block at lines 415-416. Function must still return got_ok.
        """
        def _responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return (
                    b"RTSP/1.0 401 Unauthorized\r\n"
                    b"CSeq: 1\r\n"
                    b'WWW-Authenticate: Digest realm="Bosch", nonce="XYZ"\r\n'
                    b"\r\n"
                )
            return (
                b"RTSP/1.0 200 OK\r\n"
                b"CSeq: 2\r\n"
                b"Content-Length: 10\r\n"
                b"\r\n"
                b"v=0\r\no=- 0\r\n"
            )

        async with FakeRtsp(_responder) as server:
            # Patch asyncio.open_connection so the returned writer's wait_closed raises
            original_open = asyncio.open_connection

            async def _patched_open(host, port, **kwargs):
                reader, writer = await original_open(host, port, **kwargs)

                async def _raising_wait_closed():
                    raise ConnectionResetError("reset by peer")

                writer.wait_closed = _raising_wait_closed
                return reader, writer

            with patch("asyncio.open_connection", side_effect=_patched_open):
                result = await pre_warm_rtsp(
                    server.port, "u", "p", "127.0.0.1",
                    max_attempts=1, retry_wait=0, post_success_wait=0,
                )

            # ConnectionResetError in wait_closed must be swallowed (lines 415-416)
            # and got_ok (True) must still be returned
            assert result is True, (
                "wait_closed() raising ConnectionResetError must be swallowed; "
                "pre_warm_rtsp must still return got_ok=True"
            )
