"""Sprint H: tls_proxy.py threading paths and internal branch coverage.

Covers the nested _proxy_thread / _pipe closures inside start_tls_proxy that
cannot be reached by the structural-only tests in test_tls_proxy.py:

  - Lines 92-94: ssl_ctx.wrap_socket raises → raw.close() + re-raise
  - Lines 126-127: 5 rapid failures → circuit breaker srv.close() + break
  - Lines 86-87, 135-136, 159, 190-195: structural source-code pins
    (TCP keepalive options, _pipe timeout break, _pipe finally close)

All tests mock the actual socket/TLS calls so no real network is needed.
The proxy daemon thread is allowed to run (NOT mocked) so the internal
branch logic executes.
"""

from __future__ import annotations

import ssl
import time
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest


SRC = (
    Path(__file__).parent.parent
    / "custom_components" / "bosch_shc_camera" / "tls_proxy.py"
).read_text()

CAM_ID = "TLS-H-TEST-PROXY"


def _ssl_ctx():
    return MagicMock(spec=ssl.SSLContext)


def _server_mock(port=12345):
    m = MagicMock()
    m.getsockname.return_value = ("127.0.0.1", port)
    return m


# ── Circuit breaker (lines 126-127) ──────────────────────────────────────────


class TestCircuitBreaker:
    """5 consecutive connect-failures within 30s → srv.close() (lines 126-127).

    Strategy: mock socket.socket (server) + socket.create_connection (always raises),
    provide 5 fake client connections via a custom accept() side-effect,
    let the real daemon thread run and fire the circuit breaker.
    """

    def _run_circuit_breaker(self, n_clients=5):
        cam_id = f"CB-{threading.get_ident()}"
        cache = {}
        ctx = _ssl_ctx()
        srv = _server_mock(port=54200)

        clients = [MagicMock() for _ in range(n_clients)]
        call_count = [0]
        done = threading.Event()

        def fake_accept():
            i = call_count[0]
            call_count[0] += 1
            if i < n_clients:
                return (clients[i], ("127.0.0.1", 50000 + i))
            # Thread exited via circuit breaker; accept won't be called again.
            # Raise OSError to cleanly exit the while loop if it somehow continues.
            done.set()
            raise OSError("server closed by circuit breaker")

        srv.accept = fake_accept

        with (
            patch("custom_components.bosch_shc_camera.tls_proxy.socket.socket", return_value=srv),
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                side_effect=OSError("Connection refused"),
            ),
        ):
            from custom_components.bosch_shc_camera.tls_proxy import (
                start_tls_proxy, stop_tls_proxy,
            )
            start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)
            # Give the daemon thread time to process all 5 failures
            time.sleep(0.5)

        try:
            stop_tls_proxy(cam_id, cache)
        except Exception:
            pass

        return srv, clients, call_count[0]

    def test_circuit_breaker_closes_server_socket(self):
        """srv.close() must be called after MAX_BURST connect failures (lines 126-127)."""
        srv, clients, accepted = self._run_circuit_breaker(n_clients=5)

        assert srv.close.called, (
            "Circuit breaker must call srv.close() after 5 connect failures — "
            "prevents CPU-burning reconnect loop when camera is offline"
        )

    def test_all_5_client_connections_attempted(self):
        """Every accepted client must get a connect attempt before breaker fires."""
        srv, clients, accepted = self._run_circuit_breaker(n_clients=5)

        assert accepted >= 5, (
            f"Expected ≥5 accept() calls before circuit breaker, got {accepted} — "
            "breaker fires too early"
        )

    def test_client_sockets_closed_on_failure(self):
        """Each failing client must have its socket closed (not leaked)."""
        srv, clients, accepted = self._run_circuit_breaker(n_clients=5)

        closed = sum(1 for c in clients if c.close.called)
        assert closed == 5, (
            f"Expected all 5 client sockets closed, only {closed} were — "
            "unclosed sockets leak file descriptors"
        )

    def test_circuit_breaker_constants_in_source(self):
        """_MAX_BURST and _BURST_WINDOW must be present with documented values."""
        assert "_MAX_BURST = 5" in SRC, "_MAX_BURST must be 5 (balance: detect offline vs false alarm)"
        assert "_BURST_WINDOW = 30.0" in SRC, "_BURST_WINDOW must be 30.0s"


# ── TLS wrap failure (lines 92-94) ───────────────────────────────────────────


class TestTlsWrapFailure:
    """ssl_ctx.wrap_socket raising must close the raw TCP socket (lines 92-94).

    Without raw.close(), the raw TCP connection to the camera leaks a file
    descriptor and holds a TCP connection open until GC or process exit.
    """

    def _run_tls_fail(self):
        cam_id = "TLS-WRAP-FAIL"
        cache = {}
        ctx = _ssl_ctx()
        ctx.wrap_socket = MagicMock(side_effect=ssl.SSLError("handshake failed"))

        srv = _server_mock(port=54201)
        client_mock = MagicMock()
        raw_mock = MagicMock()
        # raw_mock.setsockopt must not raise so we reach wrap_socket
        raw_mock.setsockopt = MagicMock()

        call_count = [0]

        def fake_accept():
            i = call_count[0]
            call_count[0] += 1
            if i < 5:  # provide enough clients to trigger circuit breaker
                return (client_mock, ("127.0.0.1", 50000))
            raise OSError("done")

        srv.accept = fake_accept

        with (
            patch("custom_components.bosch_shc_camera.tls_proxy.socket.socket", return_value=srv),
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                return_value=raw_mock,
            ),
        ):
            from custom_components.bosch_shc_camera.tls_proxy import (
                start_tls_proxy, stop_tls_proxy,
            )
            start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)
            time.sleep(0.4)

        try:
            stop_tls_proxy(cam_id, cache)
        except Exception:
            pass

        return raw_mock

    def test_raw_socket_closed_on_tls_failure(self):
        """raw.close() must be called when wrap_socket raises (line 93)."""
        raw_mock = self._run_tls_fail()

        assert raw_mock.close.called, (
            "raw.close() must be called when TLS handshake fails (line 93) — "
            "prevents file descriptor leak on TLS negotiation failure"
        )

    def test_tls_wrap_failure_close_in_source(self):
        """Source must contain raw.close() inside the wrap_socket exception handler."""
        # Structural pin: ensure the pattern doesn't get accidentally removed
        assert "raw.close()  # close raw socket if TLS handshake fails" in SRC, (
            "raw.close() comment must be present — it documents WHY we close here"
        )

    def test_wrap_socket_reraises(self):
        """After closing raw, the exception must propagate so the failure
        is counted by the circuit breaker."""
        # Structural pin: the 'raise' after raw.close() must be present
        # (without it, TLS failures would silently succeed, leaking broken state)
        # We verify via the source rather than execution, since the exception
        # propagates into the circuit breaker which increments fail_count.
        assert "raw.close()  # close raw socket if TLS handshake fails" in SRC
        # The bare 'raise' must follow
        lines = SRC.splitlines()
        raw_close_lines = [i for i, l in enumerate(lines) if "raw.close()  # close raw socket" in l]
        assert raw_close_lines, "raw.close() comment line not found in source"
        idx = raw_close_lines[0]
        next_lines = [lines[idx + j].strip() for j in range(1, 5)]
        assert "raise" in next_lines, (
            f"'raise' must follow raw.close() — found: {next_lines}"
        )


# ── TCP keepalive structural pins (lines 86-87, 135-136) ─────────────────────


class TestTcpKeepaliveStructural:
    """Pin the TCP keepalive configuration for raw (camera) and client (FFmpeg) sockets.

    Lines 85-89: raw socket (camera-side) keepalive options.
    Lines 133-138: client socket (FFmpeg-side) keepalive options.
    Both wrapped in try/except (AttributeError, OSError) for platform portability.
    """

    def test_raw_socket_keepalive_set(self):
        """Raw socket must have SO_KEEPALIVE + TCP options set (lines 82-89)."""
        assert "raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)" in SRC, (
            "SO_KEEPALIVE must be set on the raw camera socket to detect dead connections"
        )
        assert "raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)" in SRC
        assert "raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)" in SRC
        assert "raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)" in SRC

    def test_client_socket_keepalive_set(self):
        """Client socket must have SO_KEEPALIVE + TCP options set (lines 132-138)."""
        assert "client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)" in SRC, (
            "SO_KEEPALIVE must be set on client (FFmpeg) socket"
        )
        assert "client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)" in SRC
        assert "client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)" in SRC

    def test_keepalive_options_in_try_except(self):
        """TCP_KEEPIDLE etc. not available on all platforms (e.g. Windows) —
        must be wrapped in try/except (AttributeError, OSError)."""
        # Both try blocks use the same exception tuple
        assert SRC.count("except (AttributeError, OSError):") >= 2, (
            "Both raw and client socket keepalive blocks must have "
            "except (AttributeError, OSError) for cross-platform portability"
        )

    def test_keepidle_value_reasonable(self):
        """TCP_KEEPIDLE=30s: detect dead connections within 30s (not too short to flood)."""
        import re
        m = re.search(r"TCP_KEEPIDLE,\s*(\d+)", SRC)
        assert m, "TCP_KEEPIDLE must be set"
        val = int(m.group(1))
        assert 10 <= val <= 60, f"TCP_KEEPIDLE={val}s outside reasonable range (10-60)"

    def test_keepcnt_value_reasonable(self):
        """TCP_KEEPCNT=3: 3 unacknowledged probes before declaring dead."""
        import re
        m = re.search(r"TCP_KEEPCNT,\s*(\d+)", SRC)
        assert m, "TCP_KEEPCNT must be set"
        val = int(m.group(1))
        assert 2 <= val <= 10, f"TCP_KEEPCNT={val} outside reasonable range"


# ── _pipe structural pins (lines 159, 190-195) ────────────────────────────────


class TestPipeStructural:
    """Structural pins for the _pipe closure inside start_tls_proxy.

    Line 159: select timeout → `if not r: break` (C→CAM pipe expires after 120s idle)
    Lines 190-191: src.close() in finally
    Lines 194-195: dst.close() in finally
    """

    def test_pipe_timeout_break_present(self):
        """_pipe must break on select timeout (line 159) to avoid infinite idle hang."""
        # The pipe_timeout for C→CAM direction is 120s
        assert "if not r:" in SRC, "_pipe must break on select() timeout"
        assert "pipe_timeout = 120 if rewrite_transport else None" in SRC, (
            "C→CAM pipe must time out after 120s idle — prevents zombie sessions "
            "when FFmpeg client disappears without closing the socket"
        )

    def test_pipe_finally_closes_src_and_dst(self):
        """_pipe must close both src and dst in finally (lines 190-195)."""
        assert "src.close()" in SRC, "_pipe finally must close src socket"
        assert "dst.close()" in SRC, "_pipe finally must close dst socket"
        # Both in try/except to handle already-closed sockets
        close_lines = [l.strip() for l in SRC.splitlines() if ".close()" in l and l.strip().startswith("try:")]
        # Structural: the finally block has 2 try/except close() pairs
        finally_idx = SRC.find("finally:\n                    try:\n                        src.close()")
        assert finally_idx != -1, (
            "_pipe finally block must use try/except around src.close() — "
            "src may already be closed by the other pipe direction"
        )

    def test_pipe_closes_counterpart_on_exit(self):
        """When one direction dies (e.g. camera disconnects), the pipe must
        close both sockets so the other direction also terminates."""
        # Structural: after while loop, both src and dst are closed.
        # This prevents half-open sessions where FFmpeg keeps writing
        # to a TLS socket whose upstream camera has disconnected.
        assert "src.close()" in SRC
        assert "dst.close()" in SRC

    def test_debug_logging_gated(self):
        """RTP binary frames must not be logged (data[:1] != b'$' guard)."""
        assert "data[:1] != b\"$\"" in SRC, (
            "RTP interleaved frames ($) must be excluded from debug logging — "
            "logging binary would corrupt the log and harm performance"
        )

    def test_pipe_debug_limit_in_source(self):
        """Debug logging stops after 20 exchanges to prevent log flooding."""
        assert "_dbg_count[0] < 20" in SRC, (
            "Debug counter must limit logged exchanges — otherwise a busy "
            "RTSP session floods the HA log with binary fragments"
        )
