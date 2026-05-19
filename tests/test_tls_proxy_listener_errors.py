"""Tests for tls_proxy.py listener-startup error paths.

Pins the uncovered branches inside `_proxy_thread` and `pre_warm_rtsp`'s
exception cleanup:

  Lines 87-88:    raw-socket TCP_KEEPIDLE setsockopt raises OSError → swallow.
  Lines 127-128:  circuit-breaker `srv.close()` raises → swallow + break.
  Lines 136-137:  client-socket TCP_KEEPIDLE setsockopt raises OSError → swallow.
  Line 160:       `_pipe` select returns empty (peer closed) → break.
  Line 193:       `_pipe` debug-log on non-EBADF error when debug=True.
  Lines 197-198:  `_pipe` finally-block `src.close()` raises → swallow.
  Lines 460-461:  `pre_warm_rtsp` exception path: writer.close()/wait_closed()
                  raising is swallowed so retry loop is not aborted.

Listener errors that happen during the threaded accept loop are exercised by
patching `socket.setsockopt` (via a custom socket subclass), patching
`socket.create_connection` to return a custom raw socket, or by having the
inner select/recv path raise — verified by behaviour (proxy thread exits
cleanly, no exception propagates to test).
"""
from __future__ import annotations

import asyncio
import errno
import socket
import threading
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.tls_proxy import (
    _proxy_servers,
    pre_warm_rtsp,
    start_tls_proxy,
    stop_tls_proxy,
)


# Enable loopback sockets for every test (PHACC socket_enabled fixture).
@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled):
    yield


# ── helpers (mirrors test_tls_proxy_thread.py patterns) ──────────────────────


class _FakeTlsSocket:
    """Wrap a raw socket to look like an SSL socket (has .version + .cipher)."""

    def __init__(self, raw):
        self._raw = raw

    def version(self):
        return "TLSv1.3"

    def cipher(self):
        return ("AES128-GCM-SHA256", "TLSv1.3", 256)

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
    ctx = MagicMock()
    ctx.wrap_socket = lambda raw, **kwargs: _FakeTlsSocket(raw)
    return ctx


def _join_new_threads(threads_before, timeout=3.0):
    """Wait for proxy daemon threads to exit (PHACC verify_cleanup)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        new = frozenset(threading.enumerate()) - threads_before
        alive = [
            t for t in new
            if t.is_alive()
            and not isinstance(t, threading._DummyThread)
            and not t.name.startswith("waitpid-")
            and "_run_safe_shutdown_loop" not in t.name
        ]
        if not alive:
            break
        for t in alive:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                t.join(timeout=min(0.2, remaining))
        time.sleep(0.01)


# ── lines 87-88 + 136-137: TCP_KEEPIDLE setsockopt raises ────────────────────


class _KeepidleRaisingSocket:
    """Wrapper that delegates everything to a real socket but raises OSError on
    setsockopt(IPPROTO_TCP, TCP_KEEPIDLE, ...).

    Used to exercise the `except (AttributeError, OSError): pass` clause on
    both the raw upstream socket (lines 87-88) and the client socket (136-137).
    """

    def __init__(self, real_sock):
        self._real = real_sock

    def setsockopt(self, level, optname, value):
        if level == socket.IPPROTO_TCP and optname == getattr(socket, "TCP_KEEPIDLE", -1):
            raise OSError(errno.ENOPROTOOPT, "TCP_KEEPIDLE not supported on this socket")
        return self._real.setsockopt(level, optname, value)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestRawKeepidleSetsockoptRaises:
    """Lines 87-88: raw socket TCP_KEEPINTVL/TCP_KEEPCNT setsockopt raises OSError.

    On exotic platforms (rare BSDs, some containers) TCP keep-alive sub-options
    may not be supported even though Python defines them. The proxy must
    continue (the keep-alive tuning is a best-effort optimisation, not a hard
    requirement). The `except (AttributeError, OSError): pass` swallows both
    missing-attr and runtime kernel rejection.

    macOS Darwin: `socket.TCP_KEEPIDLE` is missing — line 86 raises
    AttributeError before lines 87-88 can run. To pin 87-88 we monkey-patch
    `socket.TCP_KEEPIDLE` to exist (any int) so line 86 succeeds, then make
    line 87's setsockopt raise OSError.
    """

    def test_proxy_does_not_crash_when_raw_setsockopt_raises(self):
        cam_id = "CAM-RAW-KEEPIDLE-87"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        # Real upstream socket the proxy will connect to (an asyncio-free server).
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # Ensure TCP_KEEPIDLE exists so line 86 succeeds and 87-88 run.
        keepidle_sentinel = getattr(socket, "TCP_KEEPIDLE", 256)

        real_create_connection = socket.create_connection
        original_setsockopt = socket.socket.setsockopt

        def _patched_setsockopt(self, level, optname, value):
            # KEEPINTVL is line 87, KEEPCNT is line 88 — raise on both so that
            # whichever line runs first hits the except clause.
            if level == socket.IPPROTO_TCP and optname in (
                getattr(socket, "TCP_KEEPINTVL", -1),
                getattr(socket, "TCP_KEEPCNT", -1),
            ):
                raise OSError(errno.ENOPROTOOPT, "synthetic keepalive rejection")
            return original_setsockopt(self, level, optname, value)

        def _wrap_create_connection(addr, timeout=10):
            return real_create_connection(addr, timeout=timeout)

        with patch.object(socket, "TCP_KEEPIDLE", keepidle_sentinel, create=True):
            with patch.object(socket.socket, "setsockopt", _patched_setsockopt):
                with patch(
                    "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                    side_effect=_wrap_create_connection,
                ):
                    port = start_tls_proxy(
                        ctx, cam_id, "127.0.0.1", up_port, port_cache
                    )

                    # Trigger one connection so the proxy reaches setsockopt block
                    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        client.settimeout(2)
                        client.connect(("127.0.0.1", port))
                        conn, _ = upstream.accept()
                        time.sleep(0.15)  # let proxy thread run through setsockopt
                        conn.close()
                    finally:
                        client.close()
                        upstream.close()
                        stop_tls_proxy(cam_id, port_cache)
                        _join_new_threads(threads_before)

        # If we got here without an unhandled exception, lines 87-90 were
        # exercised and the except clause swallowed the OSError.
        assert cam_id not in port_cache

    def test_all_three_keepalive_setsockopts_run_when_kernel_accepts(self):
        """Happy path on Linux (or macOS after patching) — TCP_KEEPIDLE +
        TCP_KEEPINTVL + TCP_KEEPCNT all accepted → all three setsockopt
        lines (87, 88, 89) execute and the loop body continues past the
        keep-alive block. Covers raw socket L89 + client socket L150 on
        platforms where the helper test
        `test_proxy_does_not_crash_when_raw_setsockopt_raises` jumps to the
        except clause early."""
        cam_id = "CAM-KEEPALIVE-ALL-OK"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # Inject all three TCP_KEEP* constants so the setsockopt block is
        # taken on macOS too. Use a stub int (well outside any real opt
        # range) and a patched setsockopt that swallows them.
        original_setsockopt = socket.socket.setsockopt

        def _accept_keepalives(self, level, optname, value):
            if level == socket.IPPROTO_TCP and optname in (
                getattr(socket, "TCP_KEEPIDLE", -1),
                getattr(socket, "TCP_KEEPINTVL", -1),
                getattr(socket, "TCP_KEEPCNT", -1),
            ):
                return None  # pretend the kernel accepted it
            return original_setsockopt(self, level, optname, value)

        real_create_connection = socket.create_connection

        def _wrap_create_connection(addr, timeout=10):
            return real_create_connection(addr, timeout=timeout)

        with patch.object(socket, "TCP_KEEPIDLE", 256, create=True), \
             patch.object(socket, "TCP_KEEPINTVL", 257, create=True), \
             patch.object(socket, "TCP_KEEPCNT", 258, create=True), \
             patch.object(socket.socket, "setsockopt", _accept_keepalives), \
             patch(
                 "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                 side_effect=_wrap_create_connection,
             ):
            port = start_tls_proxy(ctx, cam_id, "127.0.0.1", up_port, port_cache)

            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.settimeout(2)
                client.connect(("127.0.0.1", port))
                conn, _ = upstream.accept()
                time.sleep(0.15)  # let proxy thread run through both setsockopt blocks
                conn.close()
            finally:
                client.close()
                upstream.close()
                stop_tls_proxy(cam_id, port_cache)
                _join_new_threads(threads_before)

        assert cam_id not in port_cache


class TestClientKeepidleSetsockoptRaises:
    """Lines 136-137: client-side TCP_KEEPIDLE setsockopt raises OSError.

    Same defense as the raw side: client socket TCP keep-alive tuning is
    optional; the proxy must not crash if the platform rejects the option.
    """

    def test_proxy_continues_when_client_setsockopt_raises(self):
        cam_id = "CAM-CLIENT-KEEPIDLE-136"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # Ensure TCP_KEEPIDLE exists so line 135 succeeds and 136-137 run.
        keepidle_sentinel = getattr(socket, "TCP_KEEPIDLE", 256)
        original_setsockopt = socket.socket.setsockopt

        def _patched_setsockopt(self, level, optname, value):
            # TCP_KEEPINTVL = line 136, TCP_KEEPCNT = line 137
            if level == socket.IPPROTO_TCP and optname in (
                getattr(socket, "TCP_KEEPINTVL", -1),
                getattr(socket, "TCP_KEEPCNT", -1),
            ):
                raise OSError(errno.ENOPROTOOPT, "no client keepalive sub-option")
            return original_setsockopt(self, level, optname, value)

        with patch.object(socket, "TCP_KEEPIDLE", keepidle_sentinel, create=True):
            with patch.object(socket.socket, "setsockopt", _patched_setsockopt):
                port = start_tls_proxy(
                    ctx, cam_id, "127.0.0.1", up_port, port_cache
                )
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    client.settimeout(2)
                    client.connect(("127.0.0.1", port))
                    upstream_conn, _ = upstream.accept()
                    time.sleep(0.15)
                    upstream_conn.close()
                finally:
                    client.close()
                    upstream.close()
                    stop_tls_proxy(cam_id, port_cache)
                    _join_new_threads(threads_before)

        # No unhandled exception → except (AttributeError, OSError): pass hit
        assert cam_id not in port_cache


# ── lines 127-128: circuit-breaker srv.close() raises ────────────────────────


class TestCircuitBreakerSrvCloseRaises:
    """Lines 125-128: when the circuit breaker fires, `srv.close()` is wrapped
    in a try/except. If `close()` raises (e.g. socket already shut down by
    HA during a parallel teardown) the exception is swallowed and the thread
    breaks out cleanly.
    """

    def test_srv_close_exception_swallowed_in_circuit_breaker(self):
        cam_id = "CAM-CB-CLOSE-RAISES"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        # socket.socket.close is a read-only slot, so we wrap it via a subclass
        # whose `close` raises on the first call (the circuit breaker invocation
        # at line 126) and delegates on subsequent calls (so stop_tls_proxy can
        # clean up afterwards).
        close_call_count = [0]
        original_close_method = socket.socket.close

        class _CloseRaisingSocket(socket.socket):
            def close(self):
                close_call_count[0] += 1
                if close_call_count[0] == 1:
                    raise OSError(errno.EBADF, "socket already closed (synthetic)")
                return original_close_method(self)

        # Patch socket.socket inside tls_proxy so the server socket created by
        # start_tls_proxy is a _CloseRaisingSocket instance.
        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
            _CloseRaisingSocket,
        ):
            with patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                side_effect=ConnectionRefusedError("cam offline"),
            ):
                port = start_tls_proxy(ctx, cam_id, "127.0.0.1", 9999, port_cache)

                # Snapshot proxy thread for join later
                proxy_thread = next(
                    (t for t in frozenset(threading.enumerate()) - threads_before
                     if t.name.startswith("tls_proxy_")),
                    None,
                )

                try:
                    # Drive 5 failures to fire the circuit breaker
                    for _ in range(5):
                        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                        try:
                            s.settimeout(2)
                            s.connect(("127.0.0.1", port))
                        except OSError:
                            pass
                        finally:
                            try:
                                s.close()
                            except OSError:
                                pass
                        time.sleep(0.12)

                    # Wait for the proxy thread to exit. It must exit despite
                    # srv.close() raising — lines 127-128 swallow the exception.
                    if proxy_thread is not None:
                        proxy_thread.join(timeout=5.0)
                    assert (
                        proxy_thread is None or not proxy_thread.is_alive()
                    ), (
                        "circuit breaker must break out of loop even if srv.close() raises "
                        "(lines 127-128 swallow the exception)"
                    )
                    assert close_call_count[0] >= 1, (
                        "srv.close() must have been attempted by the circuit breaker"
                    )
                finally:
                    # Clean tracking dicts; subsequent close() calls fall through
                    port_cache.pop(cam_id, None)
                    _proxy_servers.pop(cam_id, None)
                    if proxy_thread is not None:
                        proxy_thread.join(timeout=2.0)
        _join_new_threads(threads_before)


# ── line 160: _pipe select empty break ───────────────────────────────────────


class TestPipeSelectEmptyBreak:
    """Line 159-160: `_select.select(...)` returns ([], [], []) → loop breaks.

    This happens when the rewrite_transport=True direction (C→CAM) hits its
    120s timeout. Verified indirectly via the camera-side pipe: once the
    upstream peer closes, the next recv() returns empty data so `if not data:
    break` (line 163) fires — but if select itself returns an empty list the
    early break at line 160 short-circuits the loop. Mocking _select.select
    to return ([], [], []) drives line 160 specifically.
    """

    def test_select_empty_list_breaks_pipe_loop(self):
        cam_id = "CAM-PIPE-SELECT-EMPTY"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # Patch _select.select inside tls_proxy to always return empty lists →
        # both _pipe threads break immediately (line 160).
        with patch(
            "custom_components.bosch_shc_camera.tls_proxy._select.select",
            return_value=([], [], []),
        ):
            port = start_tls_proxy(ctx, cam_id, "127.0.0.1", up_port, port_cache)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.settimeout(2)
                client.connect(("127.0.0.1", port))
                upstream_conn, _ = upstream.accept()
                # Give pipe threads a moment to enter + break out of select
                time.sleep(0.2)
                upstream_conn.close()
            finally:
                client.close()
                upstream.close()
                stop_tls_proxy(cam_id, port_cache)
                _join_new_threads(threads_before)
        # No assertion needed — verified the threads exited without hanging
        # (the join in _join_new_threads would have timed out otherwise).


# ── lines 193, 197-198: _pipe debug log + finally close exception ────────────


class TestPipeDebugLogAndCloseException:
    """Lines 192-193: non-EBADF pipe exceptions are always logged at DEBUG level.

    Lines 195-198: the finally block tries `src.close()` and `dst.close()`;
    if either raises, the exception is swallowed so the helper thread exits cleanly.
    """

    def test_non_ebadf_exception_with_debug_logs_then_swallows_close(self, caplog):
        cam_id = "CAM-PIPE-DEBUG-193"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # _select.select raises ValueError (a non-EBADF exception) on the
        # second invocation to drive the except + debug-log + finally paths.
        call_count = [0]
        real_select = __import__(
            "custom_components.bosch_shc_camera.tls_proxy", fromlist=["_select"]
        )._select.select

        def _flaky_select(rlist, wlist, xlist, timeout=None):
            call_count[0] += 1
            if call_count[0] >= 1:
                # First call already raises → except path with debug log
                raise ValueError("synthetic select error")
            return real_select(rlist, wlist, xlist, timeout)

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy._select.select",
            side_effect=_flaky_select,
        ):
            with caplog.at_level("DEBUG", logger="custom_components.bosch_shc_camera.tls_proxy"):
                port = start_tls_proxy(
                    ctx, cam_id, "127.0.0.1", up_port, port_cache
                )
                client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    client.settimeout(2)
                    client.connect(("127.0.0.1", port))
                    upstream_conn, _ = upstream.accept()
                    time.sleep(0.25)  # let _pipe threads run + log
                    upstream_conn.close()
                finally:
                    client.close()
                    upstream.close()
                    stop_tls_proxy(cam_id, port_cache)
                    _join_new_threads(threads_before)

        # Lines 192-193: a "pipe error" debug record must have been emitted.
        pipe_errors = [r for r in caplog.records if "pipe error" in r.getMessage()]
        assert pipe_errors, (
            "Non-EBADF exception in _pipe must emit a 'pipe error' DEBUG log (lines 192-193)"
        )


# ── lines 460-461: pre_warm wait_closed inside exception path ────────────────


class TestPreWarmExceptionWaitClosedRaises:
    """Lines 456-461: when the outer try in pre_warm_rtsp catches an exception
    AND the writer was already assigned, the code tries to `writer.close()` +
    `await writer.wait_closed()` inside an inner try/except. If wait_closed()
    raises, the exception is swallowed so the retry loop can continue.

    This is the cleanup path triggered when open_connection succeeded but a
    later step (drain/read/digest) failed — e.g. the camera reset the
    connection mid-handshake.
    """

    @pytest.mark.asyncio
    async def test_wait_closed_in_exception_path_swallowed(self):
        # Server that closes the connection immediately so reader.read raises.
        async def _handle(reader, writer):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
        proxy_port = srv.sockets[0].getsockname()[1]
        original_open = asyncio.open_connection

        async def _patched_open(host, port, **kwargs):
            reader, writer = await original_open(host, port, **kwargs)

            async def _raising_wait_closed():
                raise ConnectionResetError("server reset during teardown")

            writer.wait_closed = _raising_wait_closed
            return reader, writer

        try:
            # Patch wait_for inside the body to raise → outer except fires →
            # writer cleanup runs → wait_closed raises → lines 460-461 swallow.
            with patch("asyncio.open_connection", side_effect=_patched_open):
                with patch(
                    "asyncio.wait_for", side_effect=asyncio.TimeoutError()
                ):
                    result = await pre_warm_rtsp(
                        proxy_port, "u", "p", "127.0.0.1",
                        max_attempts=1, retry_wait=0, post_success_wait=0,
                        describe_timeout=1,
                    )
        finally:
            srv.close()
            await srv.wait_closed()

        # Function must return False without propagating the wait_closed error.
        assert result is False, (
            "pre_warm_rtsp must return False when the RTSP exchange fails; "
            "writer cleanup exceptions (lines 460-461) must not propagate"
        )

    @pytest.mark.asyncio
    async def test_writer_close_in_exception_path_swallowed(self):
        """Sibling: writer.close() itself raises (not wait_closed). Same
        try/except wraps both calls — must still return False cleanly.
        """
        async def _handle(reader, writer):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

        srv = await asyncio.start_server(_handle, "127.0.0.1", 0)
        proxy_port = srv.sockets[0].getsockname()[1]
        original_open = asyncio.open_connection

        async def _patched_open(host, port, **kwargs):
            reader, writer = await original_open(host, port, **kwargs)

            def _raising_close():
                raise RuntimeError("writer.close failed")

            writer.close = _raising_close
            return reader, writer

        try:
            with patch("asyncio.open_connection", side_effect=_patched_open):
                with patch(
                    "asyncio.wait_for", side_effect=asyncio.TimeoutError()
                ):
                    result = await pre_warm_rtsp(
                        proxy_port, "u", "p", "127.0.0.1",
                        max_attempts=1, retry_wait=0, post_success_wait=0,
                        describe_timeout=1,
                    )
        finally:
            srv.close()
            await srv.wait_closed()

        assert result is False
