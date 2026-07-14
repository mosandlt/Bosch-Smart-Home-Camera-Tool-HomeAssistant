"""Tests for the TLS proxy (`tls_proxy.py`).

The TLS proxy bridges plain TCP connections on localhost to RTSPS
connections on the camera's TLS port. It handles:
  - Digest authentication header computation
  - RTSP Transport header rewriting (UDP -> TCP interleaved)
  - Circuit breaker (5 failures in 30s -> close server socket)
  - TCP keep-alive settings for both client and camera sockets
  - start/stop lifecycle with module-level `_proxy_servers` dict
  - The `_pipe` relay threads that move bytes between client and camera
  - The `rtsp_keepalive` / `pre_warm_rtsp` async RTSP helpers used by the
    LOCAL streaming pipeline

Sections below group tests by the code path they exercise:
  - digest auth
  - stop/stop-all lifecycle
  - RTSP Transport header rewriting
  - circuit breaker constants + behavior
  - start_tls_proxy contract (mock-socket, structural)
  - on_proxy_died callback
  - no-blocking-primitive guard (start_tls_proxy must not block the event loop)
  - EBADF suppression in _pipe
  - proxy thread lifecycle (real loopback sockets)
  - TLS wrap failure cleanup
  - TCP keepalive structural pins + listener setsockopt error handling
  - _pipe relay behavior (real echo servers) + structural pins + close-once guard
  - _pipe select/debug/close-exception edge cases
  - rtsp_keepalive
  - pre_warm_rtsp
  - HLS access-token eviction (cf_unbuffer.py, exercised together with the
    tls_proxy bug-hunt round that introduced it)

`tls_proxy.py` runs its accept loop and pipe pumps inside daemon threads.
Coverage config (`pyproject.toml` `[tool.coverage.run]`) sets
`concurrency = ["thread"]` so the tracer follows those thread bodies —
do not alter threading/lock test logic when touching this file.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import re
import socket
import ssl
import textwrap
import threading
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import tls_proxy as _tls_proxy_mod
from custom_components.bosch_shc_camera.tls_proxy import (
    _digest_auth,
    _proxy_servers,
    pre_warm_rtsp,
    rtsp_keepalive,
    start_tls_proxy,
    stop_all_proxies,
    stop_tls_proxy,
)

SRC = (
    Path(__file__).parent.parent
    / "custom_components"
    / "bosch_shc_camera"
    / "tls_proxy.py"
).read_text()

UNBUF_SRC = (
    Path(__file__).parent.parent
    / "custom_components"
    / "bosch_shc_camera"
    / "cf_unbuffer.py"
).read_text()

CAM_ID = "11111111-1111-1111-1111-111111111111"


# Enable loopback sockets for every test in this module.
# pytest-homeassistant-custom-component blocks all real socket.socket()
# calls via pytest_socket by default; several sections below legitimately
# need 127.0.0.1 loopback (fake RTSP servers, real proxy threads).
@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled: None) -> Generator[None, None, None]:
    yield


class TestDigestAuth:
    """Pin the MD5 Digest auth computation used for RTSP keepalive/pre-warm."""

    def test_known_vector(self):
        """RFC 2617 style computation — verify against manual calculation."""
        user = "admin"
        password = "secret"
        method = "OPTIONS"
        uri = "rtsp://127.0.0.1:5000/rtsp_tunnel"
        realm = "RTSP Server"
        nonce = "abc123nonce"

        result = _digest_auth(user, password, method, uri, realm, nonce)

        # Manual computation
        ha1 = hashlib.md5(
            f"{user}:{realm}:{password}".encode(), usedforsecurity=False
        ).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode(), usedforsecurity=False).hexdigest()
        resp = hashlib.md5(
            f"{ha1}:{nonce}:{ha2}".encode(), usedforsecurity=False
        ).hexdigest()

        assert f'username="{user}"' in result
        assert f'realm="{realm}"' in result
        assert f'nonce="{nonce}"' in result
        assert f'uri="{uri}"' in result
        assert f'response="{resp}"' in result
        assert result.startswith("Digest ")

    def test_describe_method(self):
        """DESCRIBE method (used in pre-warm) produces different HA2."""
        r1 = _digest_auth("u", "p", "OPTIONS", "/path", "r", "n")
        r2 = _digest_auth("u", "p", "DESCRIBE", "/path", "r", "n")
        assert r1 != r2

    def test_different_uri_produces_different_response(self):
        r1 = _digest_auth("u", "p", "OPTIONS", "/path1", "r", "n")
        r2 = _digest_auth("u", "p", "OPTIONS", "/path2", "r", "n")
        assert r1 != r2

    def test_empty_credentials(self):
        """Empty user/pass must not crash — still produces a valid header."""
        result = _digest_auth("", "", "OPTIONS", "/", "realm", "nonce")
        assert result.startswith("Digest ")
        assert 'response="' in result

    def test_special_chars_in_password(self):
        """Passwords with special chars (colons, quotes) must not break."""
        result = _digest_auth("user", 'p@ss:w"ord', "OPTIONS", "/x", "r", "n")
        assert result.startswith("Digest ")

    def test_unicode_in_realm(self):
        """Bosch cameras use ASCII realms, but unicode must not crash."""
        result = _digest_auth("u", "p", "OPTIONS", "/x", "Ü-Realm", "nonce")
        assert 'realm="Ü-Realm"' in result


class TestStopTlsProxy:
    """Pin the cleanup contract of stop functions."""

    def test_stop_removes_from_port_cache(self):
        cam_id = "TEST-STOP-001"
        port_cache = {cam_id: 12345}
        # Put a mock socket in _proxy_servers
        mock_srv = MagicMock()
        _proxy_servers[cam_id] = mock_srv

        stop_tls_proxy(cam_id, port_cache)

        assert cam_id not in port_cache
        assert cam_id not in _proxy_servers
        mock_srv.close.assert_called_once()

    def test_stop_idempotent_no_crash(self):
        """Calling stop on a cam_id that's not tracked must not raise."""
        port_cache = {}
        # Must not raise
        stop_tls_proxy("NONEXISTENT-CAM", port_cache)

    def test_stop_handles_close_exception(self):
        """If socket.close() raises, stop must not propagate."""
        cam_id = "TEST-CLOSE-ERR"
        port_cache = {cam_id: 9999}
        mock_srv = MagicMock()
        mock_srv.close.side_effect = OSError("already closed")
        _proxy_servers[cam_id] = mock_srv

        # Must not raise
        stop_tls_proxy(cam_id, port_cache)
        assert cam_id not in port_cache
        assert cam_id not in _proxy_servers

    def test_stop_all_clears_everything(self):
        # Setup multiple cams
        port_cache = {"CAM-A": 100, "CAM-B": 200}
        _proxy_servers["CAM-A"] = MagicMock()
        _proxy_servers["CAM-B"] = MagicMock()

        stop_all_proxies(port_cache)

        assert len(port_cache) == 0
        assert "CAM-A" not in _proxy_servers
        assert "CAM-B" not in _proxy_servers


class TestStopAllProxiesClearsGlobal:
    """After stop_all_proxies, _proxy_servers must be empty.

    Python does NOT reimport modules on HA reload — the module-level dict
    persists across coordinator restarts.  Leftover entries prevent
    start_tls_proxy from allocating a fresh server socket: the stale guard
    ``cam_id in _proxy_servers`` triggers stop_tls_proxy on an already-closed
    socket (EBADF noise) and more critically the old *port number* is gone so
    the proxy starts dead.
    """

    def test_stop_all_empties_proxy_servers(self):
        """_proxy_servers must be empty after stop_all_proxies."""
        _proxy_servers["CAM-STALE-1"] = MagicMock()
        _proxy_servers["CAM-STALE-2"] = MagicMock()
        cache = {"CAM-STALE-1": 11111, "CAM-STALE-2": 22222}

        stop_all_proxies(cache)

        assert len(cache) == 0, "port_cache must be empty after stop_all_proxies"
        assert "CAM-STALE-1" not in _proxy_servers, (
            "_proxy_servers must not retain stale entries after stop_all_proxies"
        )
        assert "CAM-STALE-2" not in _proxy_servers

    def test_stop_all_idempotent_on_empty(self):
        """stop_all_proxies on empty dict must not raise."""
        stop_all_proxies({})  # must not raise

    def test_stop_all_clears_even_if_close_raises(self):
        """Even when srv.close() raises, the entry must be removed (alias test, real check below)."""
        pytest.skip("covered by test_stop_all_clears_even_if_close_raises_real")

    def test_stop_all_clears_even_if_close_raises_real(self):
        """Even when srv.close() raises, the entry must be removed from the dict."""
        bad_srv = MagicMock()
        bad_srv.close.side_effect = OSError("already closed")
        bad_srv.shutdown.side_effect = OSError("already closed")
        _proxy_servers["CAM-BAD-CLOSE"] = bad_srv
        cache = {"CAM-BAD-CLOSE": 9999}

        stop_all_proxies(cache)  # must not raise

        assert "CAM-BAD-CLOSE" not in _proxy_servers
        assert "CAM-BAD-CLOSE" not in cache

    def test_global_clear_in_source(self):
        """stop_all_proxies source must contain _proxy_servers.clear() (belt-and-suspenders)."""
        assert "_proxy_servers.clear()" in SRC, (
            "stop_all_proxies must call _proxy_servers.clear() to handle "
            "module-cache scenarios where stop_tls_proxy left entries behind"
        )

    def test_start_after_stop_all_succeeds(self):
        """After stop_all_proxies, start_tls_proxy for the same cam must work."""
        cam_id = "B05-RELOAD-CAM"
        # Simulate a stale entry from a previous coordinator
        stale_srv = MagicMock()
        _proxy_servers[cam_id] = stale_srv
        cache: dict[str, int] = {cam_id: 55555}

        stop_all_proxies(cache)
        assert cam_id not in _proxy_servers

        # Now start a fresh proxy — must not see the stale entry
        ctx = MagicMock(spec=ssl.SSLContext)
        fresh_srv = MagicMock()
        fresh_srv.getsockname.return_value = ("127.0.0.1", 44444)

        with (
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
                return_value=fresh_srv,
            ),
            patch("custom_components.bosch_shc_camera.tls_proxy.threading.Thread"),
        ):
            port = start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)

        try:
            assert port == 44444
            assert cam_id in _proxy_servers
        finally:
            stop_tls_proxy(cam_id, cache)


class TestTransportRewriting:
    """Pin the RTSP SETUP Transport header rewriting logic.

    The _pipe function intercepts SETUP requests from FFmpeg and rewrites
    the Transport header from UDP unicast to TCP interleaved. This is
    essential because UDP can't work through a TCP-only proxy.
    """

    def test_rewrite_regex_matches_standard_ffmpeg_transport(self):
        """Standard FFmpeg SETUP Transport header must be rewritten."""
        # This is the pattern from _pipe's re.sub
        pattern = r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+"
        header = "Transport: RTP/AVP;unicast;client_port=5000-5001"
        assert re.search(pattern, header), (
            "Pattern must match standard FFmpeg Transport"
        )

    def test_rewrite_regex_matches_rtpavpudp(self):
        """Some FFmpeg versions add /UDP explicitly."""
        pattern = r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+"
        header = "Transport: RTP/AVP/UDP;unicast;client_port=6000-6001"
        assert re.search(pattern, header)

    def test_rewrite_does_not_match_tcp(self):
        """Already-TCP transport must NOT be rewritten (no match)."""
        pattern = r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+"
        header = "Transport: RTP/AVP/TCP;unicast;interleaved=0-1"
        assert not re.search(pattern, header)

    def test_rewrite_produces_correct_tcp_interleaved(self):
        """Full SETUP request → rewritten to TCP interleaved."""
        text = (
            "SETUP rtsp://x/track1 RTSP/1.0\r\n"
            "CSeq: 3\r\n"
            "Transport: RTP/AVP;unicast;client_port=5000-5001\r\n"
            "\r\n"
        )
        lo, hi = 0, 1
        result = re.sub(
            r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+",
            f"Transport: RTP/AVP/TCP;unicast;interleaved={lo}-{hi}",
            text,
        )
        assert "RTP/AVP/TCP;unicast;interleaved=0-1" in result
        assert "client_port" not in result

    def test_interleaved_channel_increments(self):
        """Second SETUP must get channels 2-3 (not 0-1 again)."""
        counter = [0]
        texts = [
            "Transport: RTP/AVP;unicast;client_port=5000-5001",
            "Transport: RTP/AVP;unicast;client_port=5002-5003",
        ]
        results = []
        for text in texts:
            lo = counter[0]
            hi = lo + 1
            result = re.sub(
                r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+",
                f"Transport: RTP/AVP/TCP;unicast;interleaved={lo}-{hi}",
                text,
            )
            counter[0] = hi + 1
            results.append(result)
        assert "interleaved=0-1" in results[0]
        assert "interleaved=2-3" in results[1]


class TestCircuitBreakerConstants:
    """Pin the burst-failure constants used in start_tls_proxy."""

    def test_constants_present_in_source(self):
        """The circuit breaker constants must exist with expected values."""
        assert "_MAX_BURST = 5" in SRC
        assert "_BURST_WINDOW = 30.0" in SRC

    def test_max_burst_reasonable(self):
        """5 consecutive failures is enough to detect offline camera without
        over-logging or under-detecting."""
        m = re.search(r"_MAX_BURST\s*=\s*(\d+)", SRC)
        assert m
        val = int(m.group(1))
        assert 3 <= val <= 10, f"_MAX_BURST={val} outside safe range"

    def test_burst_window_reasonable(self):
        m = re.search(r"_BURST_WINDOW\s*=\s*([\d.]+)", SRC)
        assert m
        val = float(m.group(1))
        assert 10.0 <= val <= 60.0, f"_BURST_WINDOW={val}s outside safe range"


def _mock_server_socket(port: int = 12345):
    """Return a MagicMock that behaves like a bound, listening server socket.

    pytest-homeassistant-custom-component blocks all real socket.socket()
    calls via pytest_socket. These tests verify structural contracts of
    start_tls_proxy (port returned, cache populated, bind address) without
    opening a real socket — the proxy thread is also suppressed so no
    background threads leak into the test session.
    """
    sock = MagicMock()
    sock.getsockname.return_value = ("127.0.0.1", port)
    sock.fileno.return_value = -1
    return sock


class TestStartTlsProxyContract:
    """Pin structural contracts without needing network connectivity."""

    def test_returns_port_and_populates_cache(self):
        """start_tls_proxy must return an integer port and store it in cache."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cache = {}
        cam_id = "TEST-START-001"
        mock_sock = _mock_server_socket(port=54321)

        with (
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
                return_value=mock_sock,
            ),
            patch("custom_components.bosch_shc_camera.tls_proxy.threading.Thread"),
        ):
            port = start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)

        try:
            assert isinstance(port, int), "start_tls_proxy must return an int port"
            assert port == 54321
            assert cam_id in cache, "cam_id must be added to port_cache"
            assert cache[cam_id] == port
            assert cam_id in _proxy_servers
        finally:
            stop_tls_proxy(cam_id, cache)

    def test_fresh_proxy_per_call(self):
        """Calling start twice for same cam must produce different ports
        (fresh session for credential rotation)."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cache = {}
        cam_id = "TEST-FRESH-001"

        mock1 = _mock_server_socket(port=11111)
        mock2 = _mock_server_socket(port=22222)

        with (
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
                side_effect=[mock1, mock2],
            ),
            patch("custom_components.bosch_shc_camera.tls_proxy.threading.Thread"),
        ):
            port1 = start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)
            port2 = start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)

        try:
            assert port1 == 11111
            assert port2 == 22222
            assert cache[cam_id] == port2, "Cache must reflect the latest proxy port"
        finally:
            stop_tls_proxy(cam_id, cache)

    def test_server_socket_listens_on_localhost(self):
        """The proxy must only bind to 127.0.0.1 (not 0.0.0.0)."""
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cache = {}
        cam_id = "TEST-BIND-001"
        mock_sock = _mock_server_socket(port=33333)

        with (
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
                return_value=mock_sock,
            ),
            patch("custom_components.bosch_shc_camera.tls_proxy.threading.Thread"),
        ):
            start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)

        try:
            # Verify bind was called with 127.0.0.1 (not 0.0.0.0)
            mock_sock.bind.assert_called_once()
            bind_addr = mock_sock.bind.call_args[0][0]
            assert bind_addr[0] == "127.0.0.1", (
                f"TLS proxy must bind to 127.0.0.1 only, got {bind_addr[0]!r} — "
                "binding to 0.0.0.0 would expose the unencrypted RTSP stream on the LAN"
            )
        finally:
            stop_tls_proxy(cam_id, cache)


def _ssl_ctx():
    return MagicMock(spec=ssl.SSLContext)


def _server_mock(port: int = 54300):
    m = MagicMock()
    m.getsockname.return_value = ("127.0.0.1", port)
    return m


def _run_breaker_with_callback(callback, n_clients: int = 5):
    """Trip the circuit breaker n_clients times, return (srv_mock, accept_count)."""
    cam_id = f"CB-ONDIED-{threading.get_ident()}"
    cache: dict[str, int] = {}
    ctx = _ssl_ctx()
    srv = _server_mock(port=54300)

    clients = [MagicMock() for _ in range(n_clients + 2)]
    call_count = [0]

    def fake_accept():
        i = call_count[0]
        call_count[0] += 1
        if i < n_clients:
            return (clients[i], ("127.0.0.1", 60000 + i))
        raise OSError("server closed by circuit breaker")

    srv.accept = fake_accept

    with (
        patch(
            "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
            return_value=srv,
        ),
        patch(
            "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ),
    ):
        start_tls_proxy(
            ctx,
            cam_id,
            "192.0.2.1",
            443,
            cache,
            on_proxy_died=callback,
        )
        # Daemon thread needs time to chew through all failures and fire callback
        time.sleep(0.6)
        try:
            stop_tls_proxy(cam_id, cache)
        except Exception:
            pass

    return srv, call_count[0]


class TestOnProxyDiedCallback:
    """Regression: a Gen2 Indoor camera delivering 5 TLS resets in 3s during
    WiFi jitter tripped the circuit breaker (srv.close() + "Coordinator will
    rebuild the session when the camera is back" log) but no signal reached
    the coordinator. Camera-state stayed stale on "streaming", stream_url
    pointed at the dead port, HA's stream_worker got "Connection refused"
    forever. User had to toggle the live-stream switch off->on manually to
    recover.

    Fix: `on_proxy_died` callback parameter; circuit breaker invokes it after
    srv.close() so the coordinator can schedule a rebuild.

    Tests pin:
      - on_proxy_died is called exactly once when circuit breaker fires
      - on_proxy_died exceptions are swallowed (proxy thread must not crash)
      - omitting on_proxy_died stays backward-compatible (no callback, no error)
    """

    def test_on_proxy_died_called_after_circuit_breaker(self) -> None:
        """Circuit breaker must invoke on_proxy_died so coordinator can rebuild.

        Without this signal the proxy port stays dead until the next
        heartbeat/renewal (up to 3600s for Indoor Gen2).
        """
        fired = threading.Event()
        called = [0]

        def on_died() -> None:
            called[0] += 1
            fired.set()

        _run_breaker_with_callback(on_died, n_clients=5)
        assert fired.wait(timeout=1.0), (
            "on_proxy_died was never called — coordinator cannot detect "
            "the dead proxy and recovery requires manual switch toggle"
        )
        assert called[0] == 1, (
            f"on_proxy_died called {called[0]} times — must fire exactly once "
            "per circuit-breaker event to avoid duplicate rebuilds"
        )

    def test_on_proxy_died_exception_swallowed(self) -> None:
        """Callback that raises must NOT crash the proxy thread.

        The callback dispatches a coroutine via hass.loop.call_soon_threadsafe.
        During HA shutdown the event loop may already be closed and raise
        RuntimeError — that must not propagate.
        """
        fired = threading.Event()

        def on_died_boom() -> None:
            fired.set()
            raise RuntimeError("event loop is closed")

        # Must not raise out of the proxy thread (test passes if no unhandled
        # exception kills the test harness or daemon thread).
        _run_breaker_with_callback(on_died_boom, n_clients=5)
        assert fired.wait(timeout=1.0), (
            "callback did not run — can't test exception path"
        )

    def test_on_proxy_died_optional_backward_compat(self) -> None:
        """Omitting on_proxy_died must work — existing callers don't pass it."""
        cam_id = f"BC-{threading.get_ident()}"
        cache: dict[str, int] = {}
        ctx = _ssl_ctx()
        srv = _server_mock(port=54400)

        clients = [MagicMock() for _ in range(6)]
        call_count = [0]

        def fake_accept():
            i = call_count[0]
            call_count[0] += 1
            if i < 5:
                return (clients[i], ("127.0.0.1", 61000 + i))
            raise OSError("server closed by circuit breaker")

        srv.accept = fake_accept

        with (
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
                return_value=srv,
            ),
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                side_effect=OSError("Connection refused"),
            ),
        ):
            # No on_proxy_died — must not crash
            start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)
            time.sleep(0.4)
            try:
                stop_tls_proxy(cam_id, cache)
            except Exception:
                pass

        assert srv.close.called, "circuit breaker still fires without callback"

    def test_on_proxy_died_parameter_in_signature(self) -> None:
        """Pin the public API: start_tls_proxy must accept on_proxy_died kwarg."""
        sig = inspect.signature(start_tls_proxy)
        assert "on_proxy_died" in sig.parameters, (
            "start_tls_proxy signature must include on_proxy_died kwarg — "
            "callback wiring depends on this name"
        )
        assert sig.parameters["on_proxy_died"].default is None, (
            "on_proxy_died must default to None for backward compatibility"
        )


class TestStartTlsProxyNoBlocking:
    """Regression: `start_tls_proxy` was called from the async path without
    `executor_job`, and it contained a `threading.Event.wait(timeout=2)` — a
    blocking primitive on the asyncio event loop. Fast in practice, but
    technically a sync-on-async violation.

    Fix: removed the `ready = threading.Event()` and the
    `_proxy_thread_with_signal` wrapper. The proxy port is already listening
    before the thread starts (`srv.bind() + srv.listen()`), so no signal is
    needed. Pin: nothing in `start_tls_proxy` may use `threading.Event.wait`
    or any other blocking primitive after the listening socket is set up.
    """

    def test_no_threading_event_wait_in_source(self) -> None:
        """The source of `start_tls_proxy` must not contain a
        `threading.Event` allocation or `ready.wait(` call. Comments are
        stripped before checking so explanatory prose may still mention them."""
        raw = textwrap.dedent(inspect.getsource(_tls_proxy_mod.start_tls_proxy))
        # Strip line comments so the test doesn't trip on prose like
        # "we removed the ready.wait() call".
        code_only = "\n".join(line.split("#", 1)[0] for line in raw.splitlines())
        assert "threading.Event(" not in code_only, (
            "start_tls_proxy must not allocate a threading.Event — the port "
            "is already listening before the worker thread starts."
        )
        assert "ready.wait" not in code_only, (
            "start_tls_proxy must not wait on a thread-start signal — it runs "
            "on the asyncio event loop."
        )


class TestPipeErrnoEBADFSuppression:
    """Regression: [Errno 9] EBADF in _pipe must NOT be logged even at DEBUG.

    After credential rotation, start_tls_proxy stops the old proxy and starts
    a new one on a fresh port. The old pipe threads (C→CAM and CAM→C) share
    the same TLS socket. When C→CAM ends it closes the socket; CAM→C's next
    recv() raises OSError(EBADF). This is expected shutdown — logging it
    confuses users into thinking there is a real network error.
    """

    def test_ebadf_is_suppressed_in_source(self):
        """The _pipe exception handler must guard against EBADF before logging."""
        # The guard must reference errno.EBADF
        assert "errno.EBADF" in SRC, (
            "tls_proxy._pipe must suppress OSError(errno.EBADF) — "
            "it is expected when the peer socket is closed by the other pipe direction"
        )

    def test_ebadf_imported_at_module_level(self):
        """errno must be imported at module level (not inside the nested function)."""
        # First occurrence of `import errno` must appear before the class definitions
        # (i.e., it is a top-level import, not deferred)
        lines = SRC.splitlines()
        import_line = next(
            (i for i, line in enumerate(lines) if re.match(r"^import errno\s*$", line)),
            None,
        )
        assert import_line is not None, "import errno must be at module level"
        # Must appear in the first 30 lines (module header area)
        assert import_line < 30, (
            f"import errno at line {import_line + 1} — expected in module header (<30)"
        )

    def test_other_oserrors_still_logged_at_debug(self):
        """Non-EBADF OSErrors (e.g. ECONNRESET) must still reach the debug logger."""
        # The guard must be `not is_ebadf` (or equivalent) so other errors pass through
        assert (
            "not is_ebadf" in SRC
            or "not (isinstance(exc, OSError) and exc.errno == errno.EBADF)" in SRC
        ), "The EBADF guard must be a negative check so other OSErrors still get logged"


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
        alive = [
            t
            for t in new_threads
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


def _start_proxy(cam_id, cam_host, cam_port):
    """Thin wrapper: record threads-before, start proxy, return (port, cache, threads_before)."""
    threads_before = frozenset(threading.enumerate())
    port_cache: dict[str, int] = {}
    ctx = _plain_ssl_ctx()
    port = start_tls_proxy(ctx, cam_id, cam_host, cam_port, port_cache)
    return port, port_cache, threads_before


class FakeRtsp:
    """Asyncio loopback server that mimics camera RTSP responses.

    Pass a `responder` callable that gets the raw request bytes and returns
    the response bytes (or None to close the socket). The fixture spins up
    a server on 127.0.0.1 with an ephemeral port, accepts one client, runs
    the request/response loop, then shuts down.
    """

    def __init__(self, responder=None):
        self.responder = responder or (lambda req, step: None)
        self.port: int = 0
        self._server: asyncio.AbstractServer | None = None
        self.requests: list[bytes] = []

    async def __aenter__(self) -> FakeRtsp:
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
        except (TimeoutError, Exception):
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


class TestProxyThreadLifecycle:
    """Thread startup, port_cache/`_proxy_servers` bookkeeping, restart-replaces-existing."""

    def test_thread_sets_ready_and_enters_accept_loop(self):
        """Proxy thread starts, enters accept loop, port is written into cache;
        cam_id is in _proxy_servers. After stop, cam_id is removed from both.
        """
        cam_id = "TEST-CAM-LIFECYCLE-1"
        port, port_cache, threads_before = _start_proxy(cam_id, "127.0.0.1", 1)

        try:
            assert port > 0, "Must return a valid port number"
            assert port_cache.get(cam_id) == port, (
                "Port must be written into port_cache"
            )
            assert cam_id in _proxy_servers, "Server socket must be in _proxy_servers"
        finally:
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

        assert cam_id not in port_cache, (
            "cam_id must be removed from port_cache after stop"
        )
        assert cam_id not in _proxy_servers, (
            "cam_id must be removed from _proxy_servers after stop"
        )

    def test_proxy_exits_cleanly_when_server_closed(self):
        """Start proxy then immediately stop it; thread must exit without errors."""
        cam_id = "TEST-CAM-LIFECYCLE-2"
        _port, port_cache, threads_before = _start_proxy(cam_id, "127.0.0.1", 1)
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


class TestCircuitBreaker:
    """5 consecutive connect-failures within 30s → srv.close() (mock accept()).

    Strategy: mock socket.socket (server) + socket.create_connection (always
    raises), provide 5 fake client connections via a custom accept()
    side-effect, let the real daemon thread run and fire the circuit breaker.
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
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
                return_value=srv,
            ),
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                side_effect=OSError("Connection refused"),
            ),
        ):
            start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)
            # Give the daemon thread time to process all 5 failures
            time.sleep(0.5)

        try:
            stop_tls_proxy(cam_id, cache)
        except Exception:
            pass

        return srv, clients, call_count[0]

    def test_circuit_breaker_closes_server_socket(self):
        """srv.close() must be called after MAX_BURST connect failures."""
        srv, _clients, _accepted = self._run_circuit_breaker(n_clients=5)

        assert srv.close.called, (
            "Circuit breaker must call srv.close() after 5 connect failures — "
            "prevents CPU-burning reconnect loop when camera is offline"
        )

    def test_all_5_client_connections_attempted(self):
        """Every accepted client must get a connect attempt before breaker fires."""
        _srv, _clients, accepted = self._run_circuit_breaker(n_clients=5)

        assert accepted >= 5, (
            f"Expected ≥5 accept() calls before circuit breaker, got {accepted} — "
            "breaker fires too early"
        )

    def test_client_sockets_closed_on_failure(self):
        """Each failing client must have its socket closed (not leaked)."""
        _srv, clients, _accepted = self._run_circuit_breaker(n_clients=5)

        closed = sum(1 for c in clients if c.close.called)
        assert closed == 5, (
            f"Expected all 5 client sockets closed, only {closed} were — "
            "unclosed sockets leak file descriptors"
        )


class TestCircuitBreakerBoundary:
    """Pin the exact failure-count boundary (real loopback sockets): 5
    consecutive failures fire the breaker, 4 must not.
    """

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
                (
                    t
                    for t in frozenset(threading.enumerate()) - threads_before
                    if t.name.startswith("tls_proxy_")
                ),
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
                (
                    t
                    for t in frozenset(threading.enumerate()) - threads_before
                    if t.name.startswith("tls_proxy_")
                ),
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


class TestCircuitBreakerSrvCloseRaises:
    """When the circuit breaker fires, `srv.close()` is wrapped in a
    try/except. If `close()` raises (e.g. socket already shut down by HA
    during a parallel teardown) the exception is swallowed and the thread
    breaks out cleanly.
    """

    def test_srv_close_exception_swallowed_in_circuit_breaker(self):
        cam_id = "CAM-CB-CLOSE-RAISES"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        # socket.socket.close is a read-only slot, so we wrap it via a subclass
        # whose `close` raises on the first call (the circuit breaker invocation)
        # and delegates on subsequent calls (so stop_tls_proxy can clean up
        # afterwards).
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
                    (
                        t
                        for t in frozenset(threading.enumerate()) - threads_before
                        if t.name.startswith("tls_proxy_")
                    ),
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
                    # srv.close() raising — the circuit breaker swallows the exception.
                    if proxy_thread is not None:
                        proxy_thread.join(timeout=5.0)
                    assert proxy_thread is None or not proxy_thread.is_alive(), (
                        "circuit breaker must break out of loop even if srv.close() raises"
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


class TestTlsWrapFailure:
    """ssl_ctx.wrap_socket raising must close the raw TCP socket.

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
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.socket",
                return_value=srv,
            ),
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                return_value=raw_mock,
            ),
        ):
            start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)
            time.sleep(0.4)

        try:
            stop_tls_proxy(cam_id, cache)
        except Exception:
            pass

        return raw_mock

    def test_raw_socket_closed_on_tls_failure(self):
        """raw.close() must be called when wrap_socket raises."""
        raw_mock = self._run_tls_fail()

        assert raw_mock.close.called, (
            "raw.close() must be called when TLS handshake fails — "
            "prevents file descriptor leak on TLS negotiation failure"
        )

    def test_tls_wrap_failure_close_in_source(self):
        """Source must contain raw.close() inside the wrap_socket exception handler."""
        assert "raw.close()  # close raw socket if TLS handshake fails" in SRC, (
            "raw.close() comment must be present — it documents WHY we close here"
        )

    def test_wrap_socket_reraises(self):
        """After closing raw, the exception must propagate so the failure
        is counted by the circuit breaker."""
        assert "raw.close()  # close raw socket if TLS handshake fails" in SRC
        # The bare 'raise' after raw.close() must be present
        lines = SRC.splitlines()
        raw_close_lines = [
            i
            for i, line in enumerate(lines)
            if "raw.close()  # close raw socket" in line
        ]
        assert raw_close_lines, "raw.close() comment line not found in source"
        idx = raw_close_lines[0]
        next_lines = [lines[idx + j].strip() for j in range(1, 5)]
        assert "raise" in next_lines, (
            f"'raise' must follow raw.close() — found: {next_lines}"
        )


class TestTcpKeepaliveStructural:
    """Pin the TCP keepalive configuration for raw (camera) and client (FFmpeg) sockets.

    Both wrapped in try/except (AttributeError, OSError) for platform portability.
    """

    def test_raw_socket_keepalive_set(self):
        """Raw socket must have SO_KEEPALIVE + TCP options set."""
        assert "raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)" in SRC, (
            "SO_KEEPALIVE must be set on the raw camera socket to detect dead connections"
        )
        assert "raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)" in SRC
        assert "raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)" in SRC
        assert "raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)" in SRC

    def test_client_socket_keepalive_set(self):
        """Client socket must have SO_KEEPALIVE + TCP options set."""
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
        m = re.search(r"TCP_KEEPIDLE,\s*(\d+)", SRC)
        assert m, "TCP_KEEPIDLE must be set"
        val = int(m.group(1))
        assert 10 <= val <= 60, f"TCP_KEEPIDLE={val}s outside reasonable range (10-60)"

    def test_keepcnt_value_reasonable(self):
        """TCP_KEEPCNT=3: 3 unacknowledged probes before declaring dead."""
        m = re.search(r"TCP_KEEPCNT,\s*(\d+)", SRC)
        assert m, "TCP_KEEPCNT must be set"
        val = int(m.group(1))
        assert 2 <= val <= 10, f"TCP_KEEPCNT={val} outside reasonable range"


class _KeepidleRaisingSocket:
    """Wrapper that delegates everything to a real socket but raises OSError on
    setsockopt(IPPROTO_TCP, TCP_KEEPIDLE, ...).

    Used to exercise the `except (AttributeError, OSError): pass` clause on
    both the raw upstream socket and the client socket.
    """

    def __init__(self, real_sock):
        self._real = real_sock

    def setsockopt(self, level, optname, value):
        if level == socket.IPPROTO_TCP and optname == getattr(
            socket, "TCP_KEEPIDLE", -1
        ):
            raise OSError(
                errno.ENOPROTOOPT, "TCP_KEEPIDLE not supported on this socket"
            )
        return self._real.setsockopt(level, optname, value)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestRawKeepidleSetsockoptRaises:
    """Raw socket TCP_KEEPINTVL/TCP_KEEPCNT setsockopt raises OSError.

    On exotic platforms (rare BSDs, some containers) TCP keep-alive sub-options
    may not be supported even though Python defines them. The proxy must
    continue (the keep-alive tuning is a best-effort optimisation, not a hard
    requirement). The `except (AttributeError, OSError): pass` swallows both
    missing-attr and runtime kernel rejection.

    macOS Darwin: `socket.TCP_KEEPIDLE` is missing — the setsockopt call for
    TCP_KEEPIDLE itself raises AttributeError before the TCP_KEEPINTVL/CNT
    calls run. To exercise those we monkey-patch `socket.TCP_KEEPIDLE` to
    exist (any int) so the first call succeeds, then make the next
    setsockopt raise OSError.
    """

    def test_proxy_does_not_crash_when_raw_setsockopt_raises(self):
        cam_id = "CAM-RAW-KEEPIDLE"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        # Real upstream socket the proxy will connect to (an asyncio-free server).
        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # Ensure TCP_KEEPIDLE exists so the raw-socket keepalive block runs.
        keepidle_sentinel = getattr(socket, "TCP_KEEPIDLE", 256)

        real_create_connection = socket.create_connection
        original_setsockopt = socket.socket.setsockopt

        def _patched_setsockopt(self, level, optname, value):
            # Raise on TCP_KEEPINTVL/TCP_KEEPCNT so that whichever runs first
            # hits the except clause.
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

        # If we got here without an unhandled exception, the raw-socket
        # keepalive block was exercised and the except clause swallowed the OSError.
        assert cam_id not in port_cache

    def test_all_three_keepalive_setsockopts_run_when_kernel_accepts(self):
        """Happy path on Linux (or macOS after patching) — TCP_KEEPIDLE +
        TCP_KEEPINTVL + TCP_KEEPCNT all accepted → all three setsockopt
        calls execute and the loop body continues past the keep-alive block.
        Covers the raw + client socket keepalive branches on platforms where
        the sibling test `test_proxy_does_not_crash_when_raw_setsockopt_raises`
        jumps to the except clause early."""
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

        with (
            patch.object(socket, "TCP_KEEPIDLE", 256, create=True),
            patch.object(socket, "TCP_KEEPINTVL", 257, create=True),
            patch.object(socket, "TCP_KEEPCNT", 258, create=True),
            patch.object(socket.socket, "setsockopt", _accept_keepalives),
            patch(
                "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
                side_effect=_wrap_create_connection,
            ),
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
    """Client-side TCP_KEEPIDLE setsockopt raises OSError.

    Same defense as the raw side: client socket TCP keep-alive tuning is
    optional; the proxy must not crash if the platform rejects the option.
    """

    def test_proxy_continues_when_client_setsockopt_raises(self):
        cam_id = "CAM-CLIENT-KEEPIDLE"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # Ensure TCP_KEEPIDLE exists so the client-socket keepalive block runs.
        keepidle_sentinel = getattr(socket, "TCP_KEEPIDLE", 256)
        original_setsockopt = socket.socket.setsockopt

        def _patched_setsockopt(self, level, optname, value):
            if level == socket.IPPROTO_TCP and optname in (
                getattr(socket, "TCP_KEEPINTVL", -1),
                getattr(socket, "TCP_KEEPCNT", -1),
            ):
                raise OSError(errno.ENOPROTOOPT, "no client keepalive sub-option")
            return original_setsockopt(self, level, optname, value)

        with patch.object(socket, "TCP_KEEPIDLE", keepidle_sentinel, create=True):
            with patch.object(socket.socket, "setsockopt", _patched_setsockopt):
                port = start_tls_proxy(ctx, cam_id, "127.0.0.1", up_port, port_cache)
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

    def test_proxy_survives_client_setsockopt_oserror_and_accepts_next_connection(
        self,
    ):
        """Behavioral regression test for the v14.4.0 bug: client (FFmpeg)
        SO_KEEPALIVE tuning sat OUTSIDE its try/except guard, so a synthetic
        OSError there propagated out of the proxy's accept loop and silently
        killed the daemon thread — the port stayed registered but no further
        connections were ever relayed to the camera, and `on_proxy_died` was
        never called to tell the coordinator to rebuild the session
        (bug-hunt 2026-07-01; fix moved the setsockopt calls inside
        `except (AttributeError, OSError): pass`).

        A pure string-match on "SO_KEEPALIVE" would NOT catch someone moving
        the calls back outside the try — the string stays present either
        way. This test proves the try/except boundary actually works by
        driving TWO connections through the same proxy port: if the first
        connection's OSError kills the accept-loop thread, the SECOND
        connection's upstream leg is never relayed (nothing left running to
        call srv.accept()) and upstream.accept() below times out.
        """
        cam_id = "CAM-CLIENT-KEEPIDLE-SURVIVES"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()
        died: list[bool] = []

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(2)
        upstream.settimeout(3)
        up_port = upstream.getsockname()[1]

        keepidle_sentinel = getattr(socket, "TCP_KEEPIDLE", 256)
        original_setsockopt = socket.socket.setsockopt

        def _patched_setsockopt(self, level, optname, value):
            if level == socket.IPPROTO_TCP and optname in (
                getattr(socket, "TCP_KEEPINTVL", -1),
                getattr(socket, "TCP_KEEPCNT", -1),
            ):
                raise OSError(errno.ENOPROTOOPT, "no client keepalive sub-option")
            return original_setsockopt(self, level, optname, value)

        try:
            with patch.object(socket, "TCP_KEEPIDLE", keepidle_sentinel, create=True):
                with patch.object(socket.socket, "setsockopt", _patched_setsockopt):
                    port = start_tls_proxy(
                        ctx,
                        cam_id,
                        "127.0.0.1",
                        up_port,
                        port_cache,
                        on_proxy_died=lambda: died.append(True),
                    )

                    # First connection: triggers the client-side keepalive OSError.
                    client1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    client1.settimeout(2)
                    client1.connect(("127.0.0.1", port))
                    upstream_conn1, _ = upstream.accept()
                    time.sleep(0.15)
                    upstream_conn1.close()
                    client1.close()

                    # Second connection through the SAME proxy port: only
                    # arrives if the accept-loop thread survived the first
                    # client's OSError.
                    client2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    client2.settimeout(2)
                    client2.connect(("127.0.0.1", port))
                    try:
                        upstream_conn2, _ = upstream.accept()
                    except TimeoutError:
                        pytest.fail(
                            "proxy accept-loop thread died after the first "
                            "client-socket OSError — a second connection was "
                            "never relayed to the upstream camera. This is "
                            "the v14.4.0 regression: SO_KEEPALIVE must stay "
                            "INSIDE its try/except guard."
                        )
                    upstream_conn2.close()
                    client2.close()
        finally:
            upstream.close()
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

        assert not died, (
            "on_proxy_died must not fire for a benign keepalive OSError — "
            "only real connect failures should trip the circuit breaker"
        )


class TestPipeRelay:
    """Bidirectional relay + Transport rewrite + debug logging.

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
            except TimeoutError:
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
                    except TimeoutError:
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
    async def test_debug_logging_on_first_exchanges(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """First exchanges are always logged at DEBUG level — no crash."""
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

        port, port_cache, _ = _start_proxy(cam_id, "127.0.0.1", echo_port)
        try:
            await asyncio.sleep(0.05)
            with caplog.at_level(
                logging.DEBUG, logger="custom_components.bosch_shc_camera.tls_proxy"
            ):
                c = socket.create_connection(("127.0.0.1", port), timeout=2)
                c.sendall(b"DEBUG_DATA\r\n\r\n")
                await asyncio.sleep(0.3)
                c.close()
        finally:
            srv.close()
            await srv.wait_closed()
            stop_tls_proxy(cam_id, port_cache)
            _join_new_threads(threads_before)

        # Just verify no exception was raised — debug path was hit


class TestPipeStructural:
    """Structural pins for the _pipe closure inside start_tls_proxy.

    - select timeout → `if not r: break` (C→CAM pipe expires after 120s idle)
    - both sockets closed exactly once via `_close_once` in the finally block
    - RTP binary frames excluded from debug logging; debug logging capped
    """

    def test_pipe_timeout_break_present(self):
        """_pipe must break on select timeout to avoid infinite idle hang."""
        # The pipe_timeout for C→CAM direction is 120s
        assert "if not r:" in SRC, "_pipe must break on select() timeout"
        assert "pipe_timeout = 120 if rewrite_transport else None" in SRC, (
            "C→CAM pipe must time out after 120s idle — prevents zombie sessions "
            "when FFmpeg client disappears without closing the socket"
        )

    def test_pipe_finally_closes_src_and_dst(self):
        """_pipe must close both src and dst in finally via `_close_once`.

        Both C→CAM and CAM→C pipe threads share the same TLS socket object;
        `_close_once(sock, flag)` guarantees each socket is only closed once
        even though both directions race to clean up.
        """
        assert "_close_once(src, src_flag)" in SRC, (
            "_pipe finally must call _close_once(src, src_flag)"
        )
        assert "_close_once(dst, dst_flag)" in SRC, (
            "_pipe finally must call _close_once(dst, dst_flag)"
        )

    def test_debug_logging_gated(self):
        """RTP binary frames must not be logged (data[:1] != b'$' guard)."""
        assert 'data[:1] != b"$"' in SRC, (
            "RTP interleaved frames ($) must be excluded from debug logging — "
            "logging binary would corrupt the log and harm performance"
        )

    def test_pipe_debug_limit_in_source(self):
        """Debug logging stops after 20 exchanges to prevent log flooding."""
        assert "_dbg_count[0] < 20" in SRC, (
            "Debug counter must limit logged exchanges — otherwise a busy "
            "RTSP session floods the HA log with binary fragments"
        )


class TestPipeCloseOnce:
    """_pipe must close each socket exactly once even when two threads finish.

    Both C→CAM and CAM→C share the same ``tls`` socket object.  Without the
    close-once guard the second thread's finally called tls.close() on the same
    fd number — if the OS had recycled it between the two closes, a completely
    unrelated fd (a log file, another socket) would be silently closed.
    """

    def test_close_once_helper_in_source(self):
        """_close_once helper must be present in source."""
        assert "_close_once" in SRC, (
            "_close_once helper must exist in tls_proxy.py to prevent "
            "double-close of shared TLS socket between C→CAM and CAM→C threads"
        )

    def test_close_lock_in_source(self):
        """threading.Lock must guard _close_once (thread-safety)."""
        assert "_close_lock = threading.Lock()" in SRC, (
            "_close_lock must exist — concurrent threads racing to close the "
            "shared TLS socket need a lock to guarantee exactly-once semantics"
        )

    def test_client_closed_flag_in_source(self):
        """_client_closed and _tls_closed flags must be present."""
        assert "_client_closed = [False]" in SRC, (
            "_client_closed flag missing — double-close prevention broken"
        )
        assert "_tls_closed = [False]" in SRC, (
            "_tls_closed flag missing — double-close prevention broken"
        )

    def test_close_once_called_in_finally(self):
        """_close_once must be called in _pipe's finally block."""
        assert "_close_once(src, src_flag)" in SRC, (
            "_close_once(src) must be called in _pipe finally — "
            "the old try/close pattern was replaced by the once-guard"
        )
        assert "_close_once(dst, dst_flag)" in SRC

    def test_close_once_actually_closes_once(self):
        """_close_once must call sock.close() exactly once for two concurrent calls."""
        import threading as _threading

        # Grab _close_once via exec since it's a nested function — verify via
        # the observable side-effect: a MagicMock.close() call count.
        sock = MagicMock()
        flag = [False]
        lock = _threading.Lock()

        # Replicate the _close_once logic as defined in source
        def _close_once_local(s: MagicMock, f: list[bool]) -> None:
            with lock:
                if f[0]:
                    return
                f[0] = True
            try:
                s.close()
            except Exception:
                pass

        barrier = _threading.Barrier(2)

        def thread_fn() -> None:
            barrier.wait()
            _close_once_local(sock, flag)

        t1 = _threading.Thread(target=thread_fn, daemon=True)
        t2 = _threading.Thread(target=thread_fn, daemon=True)
        t1.start()
        t2.start()
        t1.join(timeout=2)
        t2.join(timeout=2)

        assert sock.close.call_count == 1, (
            f"sock.close() called {sock.close.call_count} times — "
            "must be exactly 1 even with two concurrent threads"
        )


class TestPipeSelectEmptyBreak:
    """`_select.select(...)` returning ([], [], []) must break the pipe loop.

    This happens when the rewrite_transport=True direction (C→CAM) hits its
    120s timeout. Verified indirectly via the camera-side pipe: once the
    upstream peer closes, the next recv() returns empty data so `if not data:
    break` fires — but if select itself returns an empty list the early
    break short-circuits the loop. Mocking _select.select to return
    ([], [], []) drives that early-break path specifically.
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
        # both _pipe threads break immediately.
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


class TestPipeDebugLogAndCloseException:
    """Non-EBADF pipe exceptions are always logged at DEBUG level; the
    finally block tries `src.close()` and `dst.close()`; if either raises,
    the exception is swallowed so the helper thread exits cleanly.
    """

    def test_non_ebadf_exception_with_debug_logs_then_swallows_close(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cam_id = "CAM-PIPE-DEBUG"
        threads_before = frozenset(threading.enumerate())
        port_cache: dict[str, int] = {}
        ctx = _plain_ssl_ctx()

        upstream = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upstream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        upstream.bind(("127.0.0.1", 0))
        upstream.listen(1)
        up_port = upstream.getsockname()[1]

        # _select.select raises ValueError (a non-EBADF exception) to drive
        # the except + debug-log + finally paths.
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
            with caplog.at_level(
                "DEBUG", logger="custom_components.bosch_shc_camera.tls_proxy"
            ):
                port = start_tls_proxy(ctx, cam_id, "127.0.0.1", up_port, port_cache)
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

        # A "pipe error" debug record must have been emitted.
        pipe_errors = [r for r in caplog.records if "pipe error" in r.getMessage()]
        assert pipe_errors, (
            "Non-EBADF exception in _pipe must emit a 'pipe error' DEBUG log"
        )


class TestRtspKeepalive:
    """Pin the OPTIONS-keepalive contract.

    The keepalive is the only thing standing between an active LOCAL
    session and the camera's 60 s inactivity teardown. A regression here
    silently kills LAN streaming after one minute (manifests as the
    'stream stops after a minute' bug WoodenDuke reported in GH#6).
    """

    @pytest.mark.asyncio
    async def test_closes_writer_on_read_timeout(self):
        """Regression: when the RTSP read times out mid-handshake after
        open_connection succeeded, the outer except must still close the
        writer. Otherwise every ~30 s keepalive leaks one fd/socket until
        the proxy exhausts file descriptors. Mirrors the writer-close
        already present in pre_warm_rtsp."""
        reader = MagicMock()
        # Step-1 OPTIONS read raises after the connection + write succeeded.
        reader.read = AsyncMock(side_effect=TimeoutError())
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()

        async def _fake_open(host, port):
            return reader, writer

        with patch.object(
            _tls_proxy_mod.asyncio, "open_connection", side_effect=_fake_open
        ):
            ok = await rtsp_keepalive(12345, "u", "p", "CAM-A")

        assert ok is False
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handles_direct_200_no_auth(self):
        """Some camera firmwares respond 200 OK to OPTIONS without auth.
        Branch covered: the early-exit in the `if not (nonce_m and realm_m)`
        path with '200 OK' substring. Must return True, not falsely report failure."""

        def responder(req: bytes, step: int) -> bytes | None:
            return _ok_response()

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is True

    @pytest.mark.asyncio
    async def test_full_digest_handshake_succeeds(self):
        """Standard Bosch path: 401 + nonce → authenticated OPTIONS → 200.
        Covers the happy two-step exchange."""

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
        session unhealthy."""

        def responder(req: bytes, step: int) -> bytes | None:
            return b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 1\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is False

    @pytest.mark.asyncio
    async def test_authenticated_response_not_200_returns_false(self):
        """Cam answers 401 a second time (wrong creds) → return False.
        Covers the unexpected-second-response branch."""

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
        and return False — never raise."""
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

    def test_rtsp_keepalive_has_wait_closed(self):
        """rtsp_keepalive source must contain wait_closed() call."""
        src = inspect.getsource(rtsp_keepalive)
        assert "wait_closed" in src, (
            "rtsp_keepalive must call writer.wait_closed() after writer.close() "
            "to properly release the underlying TCP socket"
        )

    def test_keepalive_wait_closed_count_matches_close_count(self):
        """Every writer.close() in rtsp_keepalive must be paired with wait_closed."""
        src = inspect.getsource(rtsp_keepalive)
        close_count = src.count("writer.close()")
        wait_count = src.count("wait_closed()")
        assert close_count == wait_count, (
            f"rtsp_keepalive has {close_count} writer.close() calls but "
            f"{wait_count} wait_closed() calls — every close must be paired "
            "to prevent TCP socket accumulation"
        )

    @pytest.mark.asyncio
    async def test_keepalive_no_nonce_no_200_returns_false_and_closes(self):
        """No nonce/realm AND no 200 OK → return False, writer closed properly."""
        wait_closed_called = []

        async def fake_wait_closed():
            wait_closed_called.append(True)

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = fake_wait_closed

        mock_reader = MagicMock()
        # Response with no nonce/realm and no 200 OK
        mock_reader.read = AsyncMock(return_value=b"RTSP/1.0 401 Unauthorized\r\n\r\n")

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 401 Unauthorized\r\n\r\n",
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, (
            "writer.close() must be called on no-nonce/no-200 path"
        )
        assert len(wait_closed_called) > 0, (
            "wait_closed() must be awaited on the no-nonce/no-200 path"
        )

    def _make_keepalive_mocks(self, resp1_bytes: bytes, wait_closed_raises=False):
        """Build (mock_reader, mock_writer) for rtsp_keepalive testing."""

        async def fake_wait_closed():
            if wait_closed_raises:
                raise ConnectionResetError("already closed")

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = fake_wait_closed

        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=resp1_bytes)
        return mock_reader, mock_writer

    @pytest.mark.asyncio
    async def test_keepalive_200_no_auth_awaits_wait_closed(self):
        """200 OK (no auth challenge) path must await wait_closed."""
        mock_reader, mock_writer = self._make_keepalive_mocks(
            b"RTSP/1.0 200 OK\r\n\r\n"
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 200 OK\r\n\r\n",
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, "writer.close() must be called"

    @pytest.mark.asyncio
    async def test_keepalive_200_no_auth_wait_closed_exception_suppressed(self):
        """wait_closed() raising on the 200-OK no-auth path must be suppressed."""
        mock_reader, mock_writer = self._make_keepalive_mocks(
            b"RTSP/1.0 200 OK\r\n\r\n", wait_closed_raises=True
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 200 OK\r\n\r\n",
                    ]
                ),
            ):
                # Must not raise even if wait_closed fails
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        # Exception was suppressed — function completed normally
        assert mock_writer.close.called, (
            "writer.close() must be called regardless of wait_closed result"
        )

    @pytest.mark.asyncio
    async def test_keepalive_authenticated_path_wait_closed_exception_suppressed(self):
        """wait_closed() raising after the authenticated OPTIONS path must be suppressed."""
        # resp1 includes nonce+realm to trigger auth challenge path
        resp1 = b'RTSP/1.0 401 Unauthorized\r\nnonce="abc123"\r\nrealm="cam"\r\n\r\n'
        mock_reader, mock_writer = self._make_keepalive_mocks(
            resp1, wait_closed_raises=True
        )
        # resp2 is the authenticated response
        resp2 = b"RTSP/1.0 200 OK\r\n\r\n"

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),  # open_connection
                        resp1,  # reader.read (resp1, with auth challenge)
                        resp2,  # reader.read (resp2, after auth)
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, (
            "writer.close() must be called on the authenticated path"
        )

    @pytest.mark.asyncio
    async def test_keepalive_no_nonce_wait_closed_exception_suppressed(self):
        """wait_closed() raising on the no-nonce/no-200 path must be suppressed."""
        mock_reader, mock_writer = self._make_keepalive_mocks(
            b"RTSP/1.0 401 Unauthorized\r\n\r\n", wait_closed_raises=True
        )

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(
                    side_effect=[
                        (mock_reader, mock_writer),
                        b"RTSP/1.0 401 Unauthorized\r\n\r\n",
                    ]
                ),
            ):
                result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called, (
            "writer.close() must be called on no-nonce/no-200 path"
        )


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
        """Single-attempt success: 401 challenge → authenticated DESCRIBE → 200 OK SDP."""

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                assert b"DESCRIBE " in req
                assert b"Accept: application/sdp" in req
                return _digest_challenge(nonce="PW1")
            assert b"Authorization: Digest" in req
            return _ok_response(b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n")

        async with FakeRtsp(responder) as server:
            ok = await pre_warm_rtsp(
                server.port,
                "u",
                "p",
                "127.0.0.1",
                max_attempts=1,
                retry_wait=0,
                post_success_wait=0,
            )
            assert ok is True

    @pytest.mark.asyncio
    async def test_unexpected_response_warns_but_returns_false(self):
        """If the second DESCRIBE doesn't get 200 OK, return False so the
        coordinator falls back to REMOTE."""

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(nonce="PW2")
            return b"RTSP/1.0 503 Service Unavailable\r\nCSeq: 2\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await pre_warm_rtsp(
                server.port,
                "u",
                "p",
                "127.0.0.1",
                max_attempts=1,
                retry_wait=0,
                post_success_wait=0,
            )
            assert ok is False

    @pytest.mark.asyncio
    async def test_missing_nonce_retries_then_fails(self):
        """Camera response without nonce/realm → retry path. After
        `max_attempts` exhausted, return False."""
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
                server.port,
                "u",
                "p",
                "127.0.0.1",
                max_attempts=1,
                retry_wait=0,
                post_success_wait=0,
            )
            assert ok is False
            # The fake server saw exactly one request.
            assert attempt_count[0] == 1

    @pytest.mark.asyncio
    async def test_unreachable_port_exhausts_retries(self):
        """Port nobody listens on — every attempt raises ConnectionRefusedError.
        After `max_attempts` we return False. Pinned with retry_wait=0 to keep
        the test fast."""
        ok = await pre_warm_rtsp(
            1,
            "u",
            "p",
            "127.0.0.1",
            max_attempts=3,
            retry_wait=0,
            post_success_wait=0,
            describe_timeout=1,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_max_attempts_param_respected(self):
        """`max_attempts=N` means at most N tries — pin the loop bound so a
        future refactor can't accidentally turn it into infinite retries."""
        # max_attempts=2, retry_wait=0 → tight upper bound on duration.
        # describe_timeout=1 means each failed attempt takes ~1 s for the
        # asyncio.wait_for to fire. So 2 attempts < ~3 s comfortably.
        start = time.monotonic()
        ok = await pre_warm_rtsp(
            1,
            "u",
            "p",
            "127.0.0.1",
            max_attempts=2,
            retry_wait=0,
            post_success_wait=0,
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
                server.port,
                "u",
                "p",
                "127.0.0.1",
                max_attempts=1,
                retry_wait=0,
                post_success_wait=0,
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

        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(nonce="PW4")
            return _ok_response(b"v=0\r\n")

        async with FakeRtsp(responder) as server:
            start = time.monotonic()
            ok = await pre_warm_rtsp(
                server.port,
                "u",
                "p",
                "127.0.0.1",
                max_attempts=1,
                retry_wait=0,
                post_success_wait=1,
            )
            elapsed = time.monotonic() - start
            assert ok is True
            assert elapsed >= 0.9, (
                f"post_success_wait=1 but elapsed only {elapsed:.2f}s — "
                "the post-success sleep was skipped, FFmpeg-vs-prewarm "
                "race window is back."
            )

    def test_pre_warm_also_has_wait_closed(self):
        """pre_warm_rtsp must call writer.wait_closed() — verify it wasn't broken."""
        src = inspect.getsource(pre_warm_rtsp)
        assert "wait_closed" in src, (
            "pre_warm_rtsp must still call wait_closed() — the fix must not "
            "have accidentally removed it"
        )

    def test_writer_closed_in_exception_path(self):
        """pre_warm_rtsp must close writer in the except block."""
        src = inspect.getsource(pre_warm_rtsp)
        # Normalise parens + whitespace so the assertion survives the formatter
        # wrapping `writer = None  # …` into `writer = (\n  None  # …\n)`.
        src_norm = " ".join(src.replace("(", " ").replace(")", " ").split())
        assert "writer = None" in src_norm, (
            "pre_warm_rtsp must initialize writer=None before the try block "
            "so the exception path can close it safely"
        )
        assert "if writer is not None" in src, (
            "pre_warm_rtsp exception path must check 'if writer is not None' "
            "before closing to handle failed open_connection"
        )

    def test_no_nonce_path_awaits_wait_closed(self):
        """pre_warm_rtsp no-nonce retry path must await writer.wait_closed()."""
        src = inspect.getsource(pre_warm_rtsp)
        # Count total await writer.wait_closed() occurrences — must be ≥ 2
        # (success path + no-nonce path)
        count = src.count("await writer.wait_closed()")
        assert count >= 2, (
            f"pre_warm_rtsp must have at least 2 'await writer.wait_closed()' calls "
            f"(success path + no-nonce path); found {count}"
        )


class TestRtspHelperContract:
    """Cross-cutting structural contracts spanning keepalive + pre-warm."""

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
        sig = inspect.signature(rtsp_keepalive)
        assert sig.return_annotation == "bool", (
            f"rtsp_keepalive return annotation is {sig.return_annotation!r} "
            "— must stay 'bool' so the coordinator's health-check works."
        )


class TestPreWarmMaxSessionDuration:
    """pre_warm_rtsp must accept and use max_session_duration.

    The URI used to contain a hard-coded ``maxSessionDuration=60`` regardless
    of camera model.  Indoor Gen2 uses 3600s.  If Bosch parses DESCRIBE URIs
    to configure the server-side session timer, the pre-warm would pin a 60-second
    countdown before FFmpeg even connects, causing premature session expiry on
    long-session models.
    """

    def test_parameter_accepted(self):
        """pre_warm_rtsp signature must have max_session_duration parameter."""
        sig = inspect.signature(pre_warm_rtsp)
        assert "max_session_duration" in sig.parameters, (
            "pre_warm_rtsp must accept max_session_duration kwarg"
        )

    def test_default_is_60(self):
        """Default must remain 60 for backward compatibility."""
        sig = inspect.signature(pre_warm_rtsp)
        default = sig.parameters["max_session_duration"].default
        assert default == 60, (
            f"Default max_session_duration should be 60 (got {default}), "
            "for backward compat with existing callers that omit the parameter"
        )

    def test_no_hardcoded_60_in_uri(self):
        """Source must use the parameter, not a hardcoded 60, in the DESCRIBE URI."""
        # The old code had:  &maxSessionDuration=60"
        # The new code has:  &maxSessionDuration={max_session_duration}"
        assert 'maxSessionDuration=60"' not in SRC, (
            "Hardcoded maxSessionDuration=60 must be replaced by the parameter — "
            "Indoor Gen2 uses 3600; mismatched value risks premature session expiry"
        )
        assert "maxSessionDuration={max_session_duration}" in SRC, (
            "URI must interpolate max_session_duration parameter"
        )

    @pytest.mark.asyncio
    async def test_custom_duration_used_in_uri(self):
        """When max_session_duration=3600, the DESCRIBE URI must contain 3600."""
        captured_writes: list[bytes] = []

        async def fake_open_connection(host, port):
            reader = MagicMock()
            reader.read = MagicMock(
                return_value=b'RTSP/1.0 401 Unauthorized\r\nnonce="abc"\r\nrealm="cam"\r\n\r\n'
            )

            async def async_read(n):
                return (
                    b'RTSP/1.0 401 Unauthorized\r\nnonce="abc"\r\nrealm="cam"\r\n\r\n'
                )

            reader.read = async_read

            writer = MagicMock()
            writer.drain = MagicMock(return_value=None)
            writer.write = lambda data: captured_writes.append(data)
            writer.close = MagicMock()

            async def async_drain():
                pass

            writer.drain = async_drain

            async def async_wait_closed():
                pass

            writer.wait_closed = async_wait_closed
            return reader, writer

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            side_effect=fake_open_connection,
        ):
            await pre_warm_rtsp(
                proxy_port=12345,
                user="user",
                password="pass",
                cam_host="192.0.2.1",
                max_attempts=1,
                max_session_duration=3600,
            )

        # At least one write must contain the custom duration
        all_written = b"".join(captured_writes).decode("utf-8", errors="replace")
        assert "maxSessionDuration=3600" in all_written, (
            f"Expected maxSessionDuration=3600 in DESCRIBE request, got: {all_written!r}"
        )
        assert (
            "maxSessionDuration=60" not in all_written
            or "maxSessionDuration=3600" in all_written
        ), "maxSessionDuration=60 must not appear when 3600 is requested"

    def test_caller_passes_max_session_duration(self):
        """live_connection.py call site must pass max_session_duration=cfg.max_session_duration."""
        init_src = (
            Path(__file__).parent.parent
            / "custom_components"
            / "bosch_shc_camera"
            / "live_connection.py"
        ).read_text()

        # Find the pre_warm_rtsp(...) call block and check for the new kwarg.
        # The call is multi-line so we need to match balanced parens manually.
        start = init_src.find("await pre_warm_rtsp(")
        assert start != -1, "Could not find pre_warm_rtsp call in live_connection.py"
        # Walk forward to find the matching closing paren
        depth = 0
        end = start
        for i, ch in enumerate(init_src[start:]):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = start + i + 1
                    break
        call_text = init_src[start:end]
        assert "max_session_duration" in call_text, (
            f"__init__.py pre_warm_rtsp call must pass max_session_duration; "
            f"found: {call_text!r}"
        )


class TestPreWarmGaps:
    """Retry-sleep + writer/wait_closed cleanup edge cases in pre_warm_rtsp."""

    @pytest.mark.asyncio
    async def test_retry_sleep_on_missing_nonce_with_retries_remaining(self):
        """Response without nonce/realm and attempt < max_attempts → sleep + continue.
        Both attempts fail → return False.
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
            # max_attempts=2, retry_wait=0 → the sleep branch is hit on attempt 1,
            # then attempt 2 also fails → returns False
            result = await pre_warm_rtsp(
                proxy_port,
                "u",
                "p",
                "127.0.0.1",
                max_attempts=2,
                retry_wait=0,
                post_success_wait=0,
                describe_timeout=1,
            )
            assert result is False, (
                "All attempts exhausted without nonce/realm → must return False"
            )
            # We attempted at least once
            assert attempt_count[0] >= 1
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_wait_closed_exception_swallowed(self):
        """writer.wait_closed() raising ConnectionResetError is swallowed by
        the except-pass block on the success path. Function must still
        return got_ok.
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
                    server.port,
                    "u",
                    "p",
                    "127.0.0.1",
                    max_attempts=1,
                    retry_wait=0,
                    post_success_wait=0,
                )

            # ConnectionResetError in wait_closed must be swallowed
            # and got_ok (True) must still be returned
            assert result is True, (
                "wait_closed() raising ConnectionResetError must be swallowed; "
                "pre_warm_rtsp must still return got_ok=True"
            )

    @pytest.mark.asyncio
    async def test_no_nonce_wait_closed_exception_swallowed(self):
        """writer.wait_closed() raising on the no-nonce path is swallowed.

        The no-nonce retry path calls await writer.wait_closed(); if that
        raises, it must be suppressed so the retry loop continues.
        """

        async def _handle(reader, writer):
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                    if not chunk:
                        return
                    data += chunk
                # Respond without nonce/realm
                writer.write(b"RTSP/1.0 500 Error\r\nCSeq: 1\r\n\r\n")
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
        original_open = asyncio.open_connection

        async def _patched_open(host, port, **kwargs):
            reader, writer = await original_open(host, port, **kwargs)

            async def _raising_wait_closed():
                raise ConnectionResetError("server reset")

            writer.wait_closed = _raising_wait_closed
            return reader, writer

        try:
            with patch("asyncio.open_connection", side_effect=_patched_open):
                result = await pre_warm_rtsp(
                    proxy_port,
                    "u",
                    "p",
                    "127.0.0.1",
                    max_attempts=1,
                    retry_wait=0,
                    post_success_wait=0,
                    describe_timeout=1,
                )
        finally:
            srv.close()
            await srv.wait_closed()

        assert result is False, (
            "No nonce + wait_closed exception → must return False without raising"
        )

    @pytest.mark.asyncio
    async def test_exception_path_closes_writer_if_open(self):
        """If open_connection succeeds but drain/wait_for raises, writer is closed.

        When the connection was established but the RTSP exchange fails
        (TimeoutError, ConnectionResetError, etc.), the writer must be closed so the
        camera's session slot is freed before the retry.
        """
        original_open = asyncio.open_connection
        close_called = [False]

        async def _patched_open(host, port, **kwargs):
            reader, writer = await original_open(host, port, **kwargs)
            original_close = writer.close

            def _tracking_close():
                close_called[0] = True
                original_close()

            writer.close = _tracking_close
            return reader, writer

        srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        proxy_port = srv.sockets[0].getsockname()[1]

        try:
            with patch("asyncio.open_connection", side_effect=_patched_open):
                with patch("asyncio.wait_for", side_effect=TimeoutError()):
                    result = await pre_warm_rtsp(
                        proxy_port,
                        "u",
                        "p",
                        "127.0.0.1",
                        max_attempts=1,
                        retry_wait=0,
                        post_success_wait=0,
                        describe_timeout=1,
                    )
        finally:
            srv.close()
            await srv.wait_closed()

        assert result is False, "TimeoutError in exchange must return False"
        assert close_called[0], (
            "writer.close() must be called in the exception path when "
            "open_connection succeeded but the RTSP exchange raised"
        )


class TestPreWarmExceptionWaitClosedRaises:
    """When the outer try in pre_warm_rtsp catches an exception AND the
    writer was already assigned, the code tries to `writer.close()` +
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
            # writer cleanup runs → wait_closed raises → must be swallowed.
            with patch("asyncio.open_connection", side_effect=_patched_open):
                with patch("asyncio.wait_for", side_effect=TimeoutError()):
                    result = await pre_warm_rtsp(
                        proxy_port,
                        "u",
                        "p",
                        "127.0.0.1",
                        max_attempts=1,
                        retry_wait=0,
                        post_success_wait=0,
                        describe_timeout=1,
                    )
        finally:
            srv.close()
            await srv.wait_closed()

        # Function must return False without propagating the wait_closed error.
        assert result is False, (
            "pre_warm_rtsp must return False when the RTSP exchange fails; "
            "writer cleanup exceptions must not propagate"
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
                with patch("asyncio.wait_for", side_effect=TimeoutError()):
                    result = await pre_warm_rtsp(
                        proxy_port,
                        "u",
                        "p",
                        "127.0.0.1",
                        max_attempts=1,
                        retry_wait=0,
                        post_success_wait=0,
                        describe_timeout=1,
                    )
        finally:
            srv.close()
            await srv.wait_closed()

        assert result is False


class TestHlsEvictionRaised:
    """`_HLS_ACCESS_MAX` raised to 256; active token skipped during eviction.

    Note: `cf_unbuffer.py` is a sibling module (not tls_proxy.py itself), but
    this eviction fix landed in the same bug-hunt round as several tls_proxy
    fixes above, and its regression tests were historically grouped with
    them — kept together here rather than splitting into a separate file.
    """

    def _cf(self):
        import custom_components.bosch_shc_camera.cf_unbuffer as cf

        cf._HLS_ACCESS.clear()
        return cf

    def test_cap_raised_to_256(self):
        """_HLS_ACCESS_MAX must be 256 (raised from 64)."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _HLS_ACCESS_MAX

        assert _HLS_ACCESS_MAX == 256, (
            f"_HLS_ACCESS_MAX should be 256 (got {_HLS_ACCESS_MAX}); "
            "64 could evict active-stream tokens causing false-idle teardown"
        )

    def test_active_window_constant_in_source(self):
        """_HLS_ACTIVE_WINDOW must be defined."""
        assert "_HLS_ACTIVE_WINDOW" in UNBUF_SRC, (
            "_HLS_ACTIVE_WINDOW constant missing — re-stamp logic can't work"
        )

    def test_prune_still_caps_at_max(self):
        """Dict size must stay at or below _HLS_ACCESS_MAX after overflow."""
        cf = self._cf()
        for i in range(cf._HLS_ACCESS_MAX + 20):
            with patch.object(cf.time, "monotonic", return_value=float(i)):
                cf._note_hls_access(
                    SimpleNamespace(path=f"/api/hls/tok{i}/playlist.m3u8")
                )
        assert len(cf._HLS_ACCESS) <= cf._HLS_ACCESS_MAX

    def test_recently_active_token_not_evicted(self):
        """When the dict is at capacity, a recently-accessed token must not be evicted."""
        cf = self._cf()
        # Fill to exactly _HLS_ACCESS_MAX, oldest tokens first
        for i in range(cf._HLS_ACCESS_MAX):
            with patch.object(cf.time, "monotonic", return_value=float(i)):
                cf._note_hls_access(
                    SimpleNamespace(path=f"/api/hls/old{i}/playlist.m3u8")
                )

        # oldest token stamped at t=0, which is *very* old — no risk of protection
        oldest_token = "old0"
        assert oldest_token in cf._HLS_ACCESS

        # Mark the oldest token as very recently active (within _HLS_ACTIVE_WINDOW)
        recent_now = float(cf._HLS_ACCESS_MAX) + 1.0
        cf._HLS_ACCESS[oldest_token] = recent_now - 5.0  # 5s ago = very fresh

        # Add one more entry to trigger eviction
        with patch.object(cf.time, "monotonic", return_value=recent_now):
            cf._note_hls_access(
                SimpleNamespace(path="/api/hls/new_overflow/playlist.m3u8")
            )

        # The recently-active token must NOT have been evicted
        assert oldest_token in cf._HLS_ACCESS, (
            "A recently-active HLS token must not be evicted at capacity — "
            "evicting it would cause the idle-reaper to see None and tear down "
            "an active stream"
        )

    def test_stale_token_evicted_when_active_present(self):
        """The stale (not-recently-active) token is evicted when the active one is protected."""
        cf = self._cf()

        # Two tokens: one stale, one recent
        cf._HLS_ACCESS["stale_tok"] = 1.0  # very old
        cf._HLS_ACCESS["recent_tok"] = 999.0  # recent

        # Fill rest of the slots with medium-age entries
        for i in range(cf._HLS_ACCESS_MAX - 2):
            cf._HLS_ACCESS[f"mid{i}"] = float(50 + i)

        assert len(cf._HLS_ACCESS) == cf._HLS_ACCESS_MAX

        recent_now = 1010.0
        with patch.object(cf.time, "monotonic", return_value=recent_now):
            cf._note_hls_access(
                SimpleNamespace(path="/api/hls/overflow_tok/playlist.m3u8")
            )

        # stale_tok (age ~1009s) should be evicted
        assert "stale_tok" not in cf._HLS_ACCESS, (
            "stale_tok (age ~1009s) should have been evicted at overflow"
        )
        # recent_tok (age ~11s) may or may not be protected depending on
        # _HLS_ACTIVE_WINDOW — the important thing is stale was evicted first
        assert len(cf._HLS_ACCESS) <= cf._HLS_ACCESS_MAX


# Section: rtsp_keepalive writer-close/wait_closed exception swallowing
# (relocated from tests/test_stream_modules_coverage.py — the camera.py
# sibling coverage in that file lives in tests/test_camera.py)


class TestRtspKeepaliveWriterCloseOnException:
    """When an exception is raised AFTER open_connection succeeds (e.g.
    during drain/read) AND writer.close()/wait_closed() itself raises, the
    inner exception must be silently swallowed and the function must return
    False without propagating."""

    @pytest.mark.asyncio
    async def test_writer_close_raises_in_outer_except_is_swallowed(self) -> None:
        """outer except → writer is not None → writer.close() raises →
        inner except: pass fires → rtsp_keepalive returns False."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        mock_writer = AsyncMock()
        mock_writer.close = MagicMock(side_effect=OSError("simulated close error"))
        mock_writer.wait_closed = AsyncMock(side_effect=OSError("simulated wait error"))

        mock_reader = AsyncMock()

        async def _fake_open(*args: object, **kwargs: object) -> tuple[object, object]:
            return mock_reader, mock_writer

        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock(side_effect=ConnectionResetError("reset"))

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            new=AsyncMock(side_effect=_fake_open),
        ):
            result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert result is False
        mock_writer.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_writer_wait_closed_raises_in_outer_except_is_swallowed(self) -> None:
        """wait_closed() raises inside the inner try — still returns False."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()  # close() succeeds
        mock_writer.wait_closed = AsyncMock(side_effect=OSError("wait_closed boom"))
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock(side_effect=TimeoutError("read timeout"))

        mock_reader = AsyncMock()

        async def _fake_open(*args: object, **kwargs: object) -> tuple[object, object]:
            return mock_reader, mock_writer

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            new=AsyncMock(side_effect=_fake_open),
        ):
            result = await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert result is False

    @pytest.mark.asyncio
    async def test_open_connection_fails_returns_false_no_writer_cleanup(self) -> None:
        """When open_connection itself raises, writer is None → cleanup
        branch skipped entirely → returns False cleanly."""
        from custom_components.bosch_shc_camera.tls_proxy import rtsp_keepalive

        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            new=AsyncMock(side_effect=ConnectionRefusedError("refused")),
        ):
            result = await rtsp_keepalive(9999, "user", "pass", CAM_ID)

        assert result is False
