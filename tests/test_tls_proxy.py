"""Tests for the TLS proxy (`tls_proxy.py`).

The TLS proxy bridges plain TCP connections on localhost to RTSPS
connections on the camera's TLS port. It handles:
  - Digest authentication header computation
  - RTSP Transport header rewriting (UDP -> TCP interleaved)
  - Circuit breaker (5 failures in 30s -> close server)
  - TCP keep-alive settings for both client and camera sockets
  - start/stop lifecycle via coordinator-owned `port_cache`/`server_cache`
    dicts (no module-level listener state — see the asyncio-native rewrite
    below)
  - The `_pipe` relay coroutines that move bytes between client and camera
  - The `rtsp_keepalive` / `pre_warm_rtsp` async RTSP helpers used by the
    LOCAL streaming pipeline

`tls_proxy.py` is asyncio-native (`asyncio.start_server`, no daemon
threads, no raw blocking sockets) — this replaced a thread-based
implementation as part of the HA-Core-submission TLS-proxy refactor
(2026-07). Server/state ownership lives entirely on the caller-supplied
``port_cache``/``server_cache`` dicts; there is no module-level dict to
reset between tests.

Sections below group tests by the code path they exercise:
  - digest auth
  - RTSP Transport header rewriting
  - circuit breaker constants
  - start/stop/stop-all lifecycle (real asyncio.start_server)
  - on_proxy_died callback
  - byte relay (real loopback servers, no threads)
  - SETUP rewrite via the relay
  - TCP keepalive best-effort tuning
  - rtsp_keepalive
  - pre_warm_rtsp
  - HLS access-token eviction (cf_unbuffer.py, historically grouped here)
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import re
import socket
import ssl
import struct
import time
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import tls_proxy as _tls_proxy_mod
from custom_components.bosch_shc_camera.tls_proxy import (
    _digest_auth,
    _set_keepalive,
    confirm_encoder_ready,
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
# calls via pytest_socket by default; every section below needs real
# 127.0.0.1 loopback (fake RTSP servers, real proxy servers).
@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled: None) -> Generator[None, None, None]:
    yield


def _caches() -> tuple[dict[str, int], dict[str, asyncio.base_events.Server]]:
    """Fresh (port_cache, server_cache) pair for a test."""
    return {}, {}


# Captured BEFORE any patch.object() call below replaces
# `asyncio.open_connection` — `_tls_proxy_mod.asyncio` is the same module
# object as the global `asyncio`, so patching it patches this name too.
# Calling the (unpatched) real function through this reference avoids
# infinite recursion.
_real_open_connection = asyncio.open_connection


async def _no_tls_open_connection(
    host: str, port: int, *, ssl: ssl.SSLContext | None = None, **kwargs: object
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Stand-in for `asyncio.open_connection` that skips the TLS handshake.

    Tests don't have a real cert for a fake "camera" server, and the proxy
    itself has no TLS logic of its own to verify (that's Python's `ssl`
    module) — only that it calls `open_connection(cam_host, cam_port,
    ssl=ssl_ctx, server_hostname=cam_host)` and relays bytes over whatever
    connection comes back. Dropping the `ssl=`/`server_hostname=` kwargs and
    connecting in plaintext to the same fake-camera server exercises that
    relay logic end-to-end without needing a certificate.
    """
    return await _real_open_connection(host, port)


@pytest.fixture
def _patched_open_connection() -> Generator[None, None, None]:
    """Patch tls_proxy's open_connection to skip TLS (see helper above)."""
    with patch.object(
        _tls_proxy_mod.asyncio, "open_connection", _no_tls_open_connection
    ):
        yield


async def _raw_client_connect(port: int) -> None:
    """Open+close a real TCP connection to `port` via a plain blocking
    socket in an executor.

    `_tls_proxy_mod.asyncio` IS the global `asyncio` module (same object),
    so any `patch.object(_tls_proxy_mod.asyncio, "open_connection", ...)`
    — used below to fake out the proxy's *outbound* connect to the camera —
    would also intercept a test's own `asyncio.open_connection()` call
    simulating an FFmpeg *client* connecting to the proxy's listening port.
    Using a raw socket here keeps the client side real and unpatched.
    """
    loop = asyncio.get_running_loop()

    def _connect() -> None:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()

    await loop.run_in_executor(None, _connect)


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
        r1 = _digest_auth("u", "p", "OPTIONS", "/path", "r", "n")
        r2 = _digest_auth("u", "p", "DESCRIBE", "/path", "r", "n")
        assert r1 != r2

    def test_different_uri_produces_different_response(self):
        r1 = _digest_auth("u", "p", "OPTIONS", "/path1", "r", "n")
        r2 = _digest_auth("u", "p", "OPTIONS", "/path2", "r", "n")
        assert r1 != r2

    def test_empty_credentials(self):
        result = _digest_auth("", "", "OPTIONS", "/", "realm", "nonce")
        assert result.startswith("Digest ")
        assert 'response="' in result

    def test_special_chars_in_password(self):
        result = _digest_auth("user", 'p@ss:w"ord', "OPTIONS", "/x", "r", "n")
        assert result.startswith("Digest ")

    def test_unicode_in_realm(self):
        result = _digest_auth("u", "p", "OPTIONS", "/x", "Ü-Realm", "nonce")
        assert 'realm="Ü-Realm"' in result


class TestTransportRewriting:
    """Pin the RTSP SETUP Transport header rewriting logic (regex is
    unchanged by the asyncio rewrite — same string, same behavior)."""

    def test_rewrite_regex_matches_standard_ffmpeg_transport(self):
        pattern = r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+"
        header = "Transport: RTP/AVP;unicast;client_port=5000-5001"
        assert re.search(pattern, header)

    def test_rewrite_regex_matches_rtpavpudp(self):
        pattern = r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+"
        header = "Transport: RTP/AVP/UDP;unicast;client_port=6000-6001"
        assert re.search(pattern, header)

    def test_rewrite_does_not_match_tcp(self):
        pattern = r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+"
        header = "Transport: RTP/AVP/TCP;unicast;interleaved=0-1"
        assert not re.search(pattern, header)

    def test_rewrite_produces_correct_tcp_interleaved(self):
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


class TestCircuitBreakerConstants:
    """Pin the burst-failure constants used in start_tls_proxy."""

    def test_constants_present_in_source(self):
        assert "_MAX_BURST = 5" in SRC
        assert "_BURST_WINDOW = 30.0" in SRC

    def test_max_burst_reasonable(self):
        m = re.search(r"_MAX_BURST\s*=\s*(\d+)", SRC)
        assert m
        val = int(m.group(1))
        assert 3 <= val <= 10, f"_MAX_BURST={val} outside safe range"

    def test_burst_window_reasonable(self):
        m = re.search(r"_BURST_WINDOW\s*=\s*([\d.]+)", SRC)
        assert m
        val = float(m.group(1))
        assert 10.0 <= val <= 60.0, f"_BURST_WINDOW={val}s outside safe range"


class TestAsyncioNative:
    """Pin: no threads, no raw sockets — the proxy runs entirely on the
    HA event loop via `asyncio.start_server`."""

    def test_no_threading_import(self):
        assert "import threading" not in SRC, (
            "tls_proxy.py must not import threading — the proxy is "
            "asyncio-native (Core-submission requirement: no OS thread "
            "owning a persistent listener outside the event loop)"
        )

    def test_no_module_level_listener_dict(self):
        assert "_proxy_servers" not in SRC, (
            "tls_proxy.py must not keep a module-level socket/server dict — "
            "server ownership belongs to the caller-supplied server_cache "
            "so a config-entry unload can close everything deterministically"
        )

    def test_uses_asyncio_start_server(self):
        assert "asyncio.start_server(" in SRC


@pytest.mark.asyncio
class TestStartStopLifecycle:
    """Real `asyncio.start_server` lifecycle: start, restart, stop, stop_all."""

    async def test_returns_port_and_populates_caches(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        port = await start_tls_proxy(
            ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache
        )

        try:
            assert isinstance(port, int)
            assert port > 0
            assert port_cache[CAM_ID] == port
            assert CAM_ID in server_cache
            assert isinstance(server_cache[CAM_ID], asyncio.base_events.Server)
        finally:
            await stop_tls_proxy(CAM_ID, port_cache, server_cache)

    async def test_binds_only_to_localhost(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        await start_tls_proxy(ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache)
        try:
            srv = server_cache[CAM_ID]
            addr = srv.sockets[0].getsockname()
            assert addr[0] == "127.0.0.1", (
                f"TLS proxy must bind to 127.0.0.1 only, got {addr[0]!r} — "
                "binding to 0.0.0.0 would expose the unencrypted RTSP "
                "stream on the LAN"
            )
        finally:
            await stop_tls_proxy(CAM_ID, port_cache, server_cache)

    async def test_fresh_proxy_per_call_different_ports(self):
        """Calling start twice for same cam must produce different ports
        (fresh session for credential rotation)."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        port1 = await start_tls_proxy(
            ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache
        )
        srv1 = server_cache[CAM_ID]
        port2 = await start_tls_proxy(
            ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache
        )
        try:
            assert port1 != port2, "Restart must allocate a fresh ephemeral port"
            assert port_cache[CAM_ID] == port2
            assert server_cache[CAM_ID] is not srv1
            assert not srv1.is_serving(), "Old server must be closed on restart"
        finally:
            await stop_tls_proxy(CAM_ID, port_cache, server_cache)

    async def test_two_cameras_get_independent_ports(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        port_a = await start_tls_proxy(
            ctx, "CAM-A", "192.0.2.1", 443, port_cache, server_cache
        )
        port_b = await start_tls_proxy(
            ctx, "CAM-B", "192.0.2.1", 443, port_cache, server_cache
        )
        try:
            assert port_a != port_b
            assert port_cache["CAM-A"] == port_a
            assert port_cache["CAM-B"] == port_b
        finally:
            await stop_tls_proxy("CAM-A", port_cache, server_cache)
            await stop_tls_proxy("CAM-B", port_cache, server_cache)

    async def test_stop_removes_from_both_caches_and_closes_server(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        await start_tls_proxy(ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache)
        srv = server_cache[CAM_ID]

        await stop_tls_proxy(CAM_ID, port_cache, server_cache)

        assert CAM_ID not in port_cache
        assert CAM_ID not in server_cache
        assert not srv.is_serving()

    async def test_stop_idempotent_no_crash(self):
        port_cache, server_cache = _caches()
        await stop_tls_proxy("NONEXISTENT-CAM", port_cache, server_cache)

    async def test_stop_handles_wait_closed_exception(self):
        """If server.wait_closed() raises, stop must not propagate."""
        port_cache, server_cache = _caches()
        bad_srv = MagicMock()
        bad_srv.close = MagicMock()
        bad_srv.wait_closed = AsyncMock(side_effect=OSError("already closed"))
        server_cache[CAM_ID] = bad_srv
        port_cache[CAM_ID] = 9999

        await stop_tls_proxy(CAM_ID, port_cache, server_cache)

        assert CAM_ID not in port_cache
        assert CAM_ID not in server_cache
        bad_srv.close.assert_called_once()

    async def test_stop_all_clears_everything(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        await start_tls_proxy(ctx, "CAM-A", "192.0.2.1", 443, port_cache, server_cache)
        await start_tls_proxy(ctx, "CAM-B", "192.0.2.1", 443, port_cache, server_cache)

        await stop_all_proxies(port_cache, server_cache)

        assert port_cache == {}
        assert server_cache == {}

    async def test_stop_all_idempotent_on_empty(self):
        await stop_all_proxies({}, {})  # must not raise

    async def test_stop_all_clears_even_if_close_raises(self):
        port_cache, server_cache = _caches()
        bad_srv = MagicMock()
        bad_srv.close = MagicMock(side_effect=OSError("already closed"))
        bad_srv.wait_closed = AsyncMock()
        server_cache["CAM-BAD-CLOSE"] = bad_srv
        port_cache["CAM-BAD-CLOSE"] = 9999

        await stop_all_proxies(port_cache, server_cache)  # must not raise

        assert "CAM-BAD-CLOSE" not in server_cache
        assert "CAM-BAD-CLOSE" not in port_cache

    async def test_start_after_stop_all_succeeds(self):
        """After stop_all_proxies, start_tls_proxy for the same cam must work."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        await start_tls_proxy(ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache)

        await stop_all_proxies(port_cache, server_cache)
        assert CAM_ID not in server_cache

        port = await start_tls_proxy(
            ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache
        )
        try:
            assert port > 0
            assert CAM_ID in server_cache
        finally:
            await stop_tls_proxy(CAM_ID, port_cache, server_cache)


@pytest.mark.asyncio
class TestOnProxyDiedCallback:
    """Regression (pre-existing, carried over from the thread-based
    implementation): a Gen2 Indoor camera delivering repeated TLS resets
    during WiFi jitter must trip the circuit breaker AND signal the
    coordinator via `on_proxy_died` so it can rebuild — without this,
    camera state stays stale on "streaming" pointing at a dead port until
    the next heartbeat/renewal (up to 3600s for Indoor Gen2)."""

    async def test_fires_once_after_five_consecutive_failures(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        called = []

        async def _always_refused(host, port, **kwargs):
            raise ConnectionRefusedError("camera offline")

        with patch.object(_tls_proxy_mod.asyncio, "open_connection", _always_refused):
            await start_tls_proxy(
                ctx,
                CAM_ID,
                "192.0.2.1",
                443,
                port_cache,
                server_cache,
                on_proxy_died=lambda: called.append(True),
            )
            proxy_port = port_cache[CAM_ID]

            for _ in range(5):
                try:
                    await _raw_client_connect(proxy_port)
                except OSError:
                    break
                await asyncio.sleep(0.02)

            # Give the server's connection handlers a moment to run.
            deadline = time.monotonic() + 2.0
            while not called and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

        assert called == [True], (
            f"on_proxy_died must fire exactly once, fired {len(called)} times"
        )
        assert CAM_ID not in server_cache, "circuit breaker must close the server"
        assert CAM_ID not in port_cache

    async def test_exception_in_callback_is_swallowed(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        fired = []

        def _boom() -> None:
            fired.append(True)
            raise RuntimeError("event loop is closed")

        async def _always_refused(host, port, **kwargs):
            raise ConnectionRefusedError("camera offline")

        with patch.object(_tls_proxy_mod.asyncio, "open_connection", _always_refused):
            await start_tls_proxy(
                ctx,
                CAM_ID,
                "192.0.2.1",
                443,
                port_cache,
                server_cache,
                on_proxy_died=_boom,
            )
            proxy_port = port_cache[CAM_ID]
            for _ in range(5):
                try:
                    await _raw_client_connect(proxy_port)
                except OSError:
                    break
                await asyncio.sleep(0.02)
            deadline = time.monotonic() + 2.0
            while not fired and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

        assert fired == [True], "callback did not run — can't test exception path"

    async def test_optional_backward_compat(self):
        """Omitting on_proxy_died must work — existing callers don't pass it."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        async def _always_refused(host, port, **kwargs):
            raise ConnectionRefusedError("camera offline")

        with patch.object(_tls_proxy_mod.asyncio, "open_connection", _always_refused):
            await start_tls_proxy(
                ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache
            )
            proxy_port = port_cache[CAM_ID]
            for _ in range(5):
                try:
                    await _raw_client_connect(proxy_port)
                except OSError:
                    break
                await asyncio.sleep(0.02)
            deadline = time.monotonic() + 2.0
            while CAM_ID in server_cache and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

        assert CAM_ID not in server_cache, (
            "circuit breaker still fires without callback"
        )

    async def test_signature_accepts_on_proxy_died_kwarg(self):
        sig = inspect.signature(start_tls_proxy)
        assert "on_proxy_died" in sig.parameters
        assert sig.parameters["on_proxy_died"].default is None

    async def test_stale_generation_skips_caches_and_callback(self):
        """If a NEWER proxy has already been installed under the same
        cam_id by the time an OLD generation's circuit breaker fires (a
        slow-to-fail connect attempt outliving a renewal/rebuild), the
        stale trip must close its own server but must NOT evict the newer
        generation from the caches or fire a spurious rebuild callback."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        called = []

        async def _always_refused(host, port, **kwargs):
            raise ConnectionRefusedError("camera offline")

        with patch.object(_tls_proxy_mod.asyncio, "open_connection", _always_refused):
            await start_tls_proxy(
                ctx,
                CAM_ID,
                "192.0.2.1",
                443,
                port_cache,
                server_cache,
                on_proxy_died=lambda: called.append(True),
            )
            proxy_port = port_cache[CAM_ID]

            # Simulate a renewal/rebuild installing a newer generation under
            # the same cam_id — WITHOUT going through stop_tls_proxy, so the
            # old generation's closure (and its in-flight fail_count state)
            # stays alive exactly as it would if a real coroutine were still
            # mid-connect when the swap happened.
            newer_server = MagicMock()
            server_cache[CAM_ID] = newer_server
            port_cache[CAM_ID] = 99999

            # Drive the OLD generation's circuit breaker to firing.
            for _ in range(5):
                try:
                    await _raw_client_connect(proxy_port)
                except OSError:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)

        assert called == [], (
            "on_proxy_died must NOT fire for a stale/superseded generation "
            "— it would trigger a spurious rebuild of an already-fine session"
        )
        assert server_cache[CAM_ID] is newer_server, (
            "a stale circuit-breaker trip must not evict the newer, "
            "healthy generation from server_cache"
        )
        assert port_cache[CAM_ID] == 99999

    async def test_double_fire_is_deduped_by_died_fired_guard(self):
        """Several concurrently-failing handlers can all observe
        fail_count >= _MAX_BURST before any of them finishes closing the
        server — the `died_fired` guard must still ensure the callback and
        cache cleanup only happen once."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        called = []

        async def _always_refused(host, port, **kwargs):
            raise ConnectionRefusedError("camera offline")

        with patch.object(_tls_proxy_mod.asyncio, "open_connection", _always_refused):
            await start_tls_proxy(
                ctx,
                CAM_ID,
                "192.0.2.1",
                443,
                port_cache,
                server_cache,
                on_proxy_died=lambda: called.append(True),
            )
            proxy_port = port_cache[CAM_ID]

            # Fire 8 connects at once (well past _MAX_BURST=5) so multiple
            # _handle_client coroutines can race past the threshold check
            # before the first one finishes tearing the server down.
            await asyncio.gather(
                *(_raw_client_connect(proxy_port) for _ in range(8)),
                return_exceptions=True,
            )
            deadline = time.monotonic() + 2.0
            while not called and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

        assert called == [True], (
            f"on_proxy_died must fire exactly once even under a concurrent "
            f"burst, fired {len(called)} times"
        )

    async def test_server_close_exception_during_circuit_breaker_is_swallowed(self):
        """If server.close() itself raises while the circuit breaker is
        tearing the server down, the on_proxy_died callback must still
        fire — the exception must not prevent the coordinator from being
        notified that it needs to rebuild the session."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        called = []

        async def _always_refused(host, port, **kwargs):
            raise ConnectionRefusedError("camera offline")

        with (
            patch.object(_tls_proxy_mod.asyncio, "open_connection", _always_refused),
            patch.object(
                asyncio.base_events.Server,
                "close",
                side_effect=RuntimeError("synthetic close failure"),
            ),
        ):
            await start_tls_proxy(
                ctx,
                CAM_ID,
                "192.0.2.1",
                443,
                port_cache,
                server_cache,
                on_proxy_died=lambda: called.append(True),
            )
            proxy_port = port_cache[CAM_ID]

            for _ in range(5):
                try:
                    await _raw_client_connect(proxy_port)
                except OSError:
                    break
                await asyncio.sleep(0.02)

            deadline = time.monotonic() + 2.0
            while not called and time.monotonic() < deadline:
                await asyncio.sleep(0.02)

        assert called == [True], (
            "on_proxy_died must still fire even when server.close() raises "
            "during the circuit-breaker teardown"
        )

    async def test_less_than_five_failures_do_not_trip_breaker(self):
        """4 consecutive failures must NOT close the server."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()
        called = []

        async def _always_refused(host, port, **kwargs):
            raise ConnectionRefusedError("camera offline")

        with patch.object(_tls_proxy_mod.asyncio, "open_connection", _always_refused):
            await start_tls_proxy(
                ctx,
                CAM_ID,
                "192.0.2.1",
                443,
                port_cache,
                server_cache,
                on_proxy_died=lambda: called.append(True),
            )
            proxy_port = port_cache[CAM_ID]
            for _ in range(4):
                await _raw_client_connect(proxy_port)
                await asyncio.sleep(0.05)

            await asyncio.sleep(0.3)
            assert CAM_ID in server_cache, (
                "4 consecutive failures must NOT trigger the circuit breaker"
            )
            assert called == []
        await stop_tls_proxy(CAM_ID, port_cache, server_cache)


class _EchoCamera:
    """Real asyncio loopback server standing in for the camera. Echoes
    whatever it receives back to the caller (used to assert the proxy
    relayed bytes in a given direction) and records every request it saw.
    """

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.base_events.Server | None = None
        self.received: list[bytes] = []

    async def __aenter__(self) -> _EchoCamera:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                data = await asyncio.wait_for(reader.read(65536), timeout=2.0)
                if not data:
                    break
                self.received.append(data)
                writer.write(data)
                await writer.drain()
        except (TimeoutError, OSError):
            pass
        finally:
            writer.close()


@pytest.mark.asyncio
@pytest.mark.usefixtures("_patched_open_connection")
class TestPipeRelay:
    """Bidirectional relay + Transport rewrite, driven through a real
    proxy server and a real (plaintext, TLS skipped per
    `_patched_open_connection`) fake-camera server. No threads involved."""

    async def test_client_to_camera_relay(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        async with _EchoCamera() as cam:
            await start_tls_proxy(
                ctx, CAM_ID, "127.0.0.1", cam.port, port_cache, server_cache
            )
            try:
                _reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port_cache[CAM_ID]
                )
                writer.write(b"HELLO_CAM\r\n\r\n")
                await writer.drain()

                deadline = time.monotonic() + 2.0
                while not cam.received and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)

                writer.close()
            finally:
                await stop_tls_proxy(CAM_ID, port_cache, server_cache)

        assert cam.received, "Client data must reach the fake camera via the proxy"
        assert b"HELLO_CAM" in cam.received[0]

    async def test_camera_to_client_relay(self):
        """Echo server sends data back — must arrive at the client."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        async with _EchoCamera() as cam:
            await start_tls_proxy(
                ctx, CAM_ID, "127.0.0.1", cam.port, port_cache, server_cache
            )
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port_cache[CAM_ID]
                )
                writer.write(b"PING\r\n\r\n")
                await writer.drain()
                reply = await asyncio.wait_for(reader.read(65536), timeout=2.0)
                writer.close()
            finally:
                await stop_tls_proxy(CAM_ID, port_cache, server_cache)

        assert b"PING" in reply, (
            "Echo server reply must be relayed back through the proxy"
        )

    async def test_setup_rewrite_udp_to_tcp_interleaved(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        async with _EchoCamera() as cam:
            await start_tls_proxy(
                ctx, CAM_ID, "127.0.0.1", cam.port, port_cache, server_cache
            )
            setup_request = (
                b"SETUP rtsp://127.0.0.1/stream RTSP/1.0\r\n"
                b"CSeq: 3\r\n"
                b"Transport: RTP/AVP;unicast;client_port=5000-5001\r\n"
                b"\r\n"
            )
            try:
                _reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port_cache[CAM_ID]
                )
                writer.write(setup_request)
                await writer.drain()

                deadline = time.monotonic() + 2.0
                while not cam.received and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)

                writer.close()
            finally:
                await stop_tls_proxy(CAM_ID, port_cache, server_cache)

        assert cam.received, "Proxy must forward SETUP to the camera"
        forwarded = b"".join(cam.received)
        assert b"RTP/AVP/TCP" in forwarded
        assert b"interleaved=0-1" in forwarded
        assert b"client_port" not in forwarded

    async def test_second_setup_gets_incrementing_channels(self):
        """Real FFmpeg waits for each SETUP's response before sending the
        next — pace the two writes accordingly so each lands in its own
        read() cycle. (Two SETUPs arriving in the *same* read buffer would
        both get the same interleaved channel pair — a pre-existing, latent
        edge case carried over unchanged from the thread-based
        implementation, not something this rewrite introduces or fixes.)
        """
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        async with _EchoCamera() as cam:
            await start_tls_proxy(
                ctx, CAM_ID, "127.0.0.1", cam.port, port_cache, server_cache
            )
            try:
                _reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port_cache[CAM_ID]
                )
                for client_port in ("5000-5001", "5002-5003"):
                    writer.write(
                        b"SETUP rtsp://x/track RTSP/1.0\r\nCSeq: 1\r\n"
                        b"Transport: RTP/AVP;unicast;client_port="
                        + client_port.encode()
                        + b"\r\n\r\n"
                    )
                    await writer.drain()
                    # Give the proxy's pipe a full read/rewrite/forward cycle
                    # before sending the next SETUP.
                    deadline = time.monotonic() + 2.0
                    prev_count = len(cam.received)
                    while (
                        len(cam.received) == prev_count and time.monotonic() < deadline
                    ):
                        await asyncio.sleep(0.02)
                writer.close()
            finally:
                await stop_tls_proxy(CAM_ID, port_cache, server_cache)

        forwarded = b"".join(cam.received)
        assert b"interleaved=0-1" in forwarded
        assert b"interleaved=2-3" in forwarded

    async def test_idle_client_to_camera_times_out(self):
        """C→CAM direction must break after its idle timeout so a dead
        FFmpeg client doesn't hold the connection (and camera session)
        open forever."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        with patch.object(_tls_proxy_mod, "_CLIENT_TO_CAM_IDLE_TIMEOUT", 0.1):
            async with _EchoCamera() as cam:
                await start_tls_proxy(
                    ctx, CAM_ID, "127.0.0.1", cam.port, port_cache, server_cache
                )
                try:
                    reader, writer = await asyncio.open_connection(
                        "127.0.0.1", port_cache[CAM_ID]
                    )
                    # Send nothing — idle timeout must fire and close the pipe.
                    data = await asyncio.wait_for(reader.read(1), timeout=2.0)
                    assert data == b"", (
                        "camera→client side must observe EOF once the idle "
                        "C→CAM pipe times out and closes the shared connection"
                    )
                    writer.close()
                finally:
                    await stop_tls_proxy(CAM_ID, port_cache, server_cache)

    async def test_camera_hard_reset_mid_write_logs_and_closes_cleanly(self):
        """A camera that RSTs the connection mid-relay (credential rotation,
        brief WiFi drop) must not crash the pipe — the write/drain that
        raises must be caught, logged at DEBUG, and both ends closed."""
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        async def _reset_immediately(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
                )
            writer.close()

        cam_srv = await asyncio.start_server(_reset_immediately, "127.0.0.1", 0)
        cam_port = cam_srv.sockets[0].getsockname()[1]

        try:
            await start_tls_proxy(
                ctx, CAM_ID, "127.0.0.1", cam_port, port_cache, server_cache
            )
            _reader, writer = await asyncio.open_connection(
                "127.0.0.1", port_cache[CAM_ID]
            )
            # Give the proxy a moment to connect upstream and hit the RST.
            for _ in range(20):
                writer.write(b"PING\r\n\r\n")
                try:
                    await writer.drain()
                except OSError:
                    break
                await asyncio.sleep(0.02)
            writer.close()
        finally:
            await stop_tls_proxy(CAM_ID, port_cache, server_cache)
            cam_srv.close()
            await cam_srv.wait_closed()


class TestSetKeepalive:
    """`_set_keepalive` is a best-effort tuning helper — must never raise,
    even when the writer has no underlying socket (e.g. a MagicMock in
    other tests) or the platform rejects an option."""

    def test_noop_when_no_underlying_socket(self):
        writer = MagicMock()
        writer.get_extra_info.return_value = None
        _set_keepalive(writer)  # must not raise

    def test_applies_to_real_socket(self):
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            writer = MagicMock()
            writer.get_extra_info.return_value = raw
            _set_keepalive(writer)  # must not raise
        finally:
            raw.close()

    def test_swallows_setsockopt_oserror(self):
        raw = MagicMock()
        raw.setsockopt.side_effect = OSError(errno.ENOPROTOOPT, "unsupported")
        writer = MagicMock()
        writer.get_extra_info.return_value = raw
        _set_keepalive(writer)  # must not raise

    def test_swallows_setsockopt_attributeerror(self):
        raw = MagicMock()
        raw.setsockopt.side_effect = AttributeError("no such option")
        writer = MagicMock()
        writer.get_extra_info.return_value = raw
        _set_keepalive(writer)  # must not raise


@pytest.mark.asyncio
class TestTlsConnectFailureCircuitBreakerReset:
    """A successful connect after prior failures must reset the burst
    counter — a single transient blip must not accumulate toward a later,
    unrelated burst."""

    async def test_success_resets_failure_count(self):
        port_cache, server_cache = _caches()
        ctx = ssl.create_default_context()

        async with _EchoCamera() as cam:
            call_count = 0

            async def _flaky(host, port, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count <= 3:
                    raise ConnectionRefusedError("transient")
                return await _real_open_connection("127.0.0.1", cam.port)

            with patch.object(_tls_proxy_mod.asyncio, "open_connection", _flaky):
                await start_tls_proxy(
                    ctx, CAM_ID, "192.0.2.1", 443, port_cache, server_cache
                )
                proxy_port = port_cache[CAM_ID]

                # 3 failures (below _MAX_BURST=5), then a success. Client-side
                # connects use the unpatched real open_connection reference —
                # `_flaky` above only fakes the proxy's outbound connect to
                # the (fake) camera, not these inbound client connects.
                for _ in range(3):
                    _r, w = await _real_open_connection("127.0.0.1", proxy_port)
                    w.close()
                    await asyncio.sleep(0.03)
                _r, w = await _real_open_connection("127.0.0.1", proxy_port)
                w.write(b"PING")
                await w.drain()
                await asyncio.sleep(0.1)
                w.close()

                # Server must still be alive — the successful connect reset
                # the burst counter, so 2 more failures shouldn't trip it.
                for _ in range(2):
                    _r, w = await _real_open_connection("127.0.0.1", proxy_port)
                    w.close()
                    await asyncio.sleep(0.03)
                await asyncio.sleep(0.2)

                assert CAM_ID in server_cache, (
                    "a successful connect must reset the failure-burst "
                    "counter — 3 failures + 1 success + 2 failures must "
                    "not trip the 5-in-a-row circuit breaker"
                )

            await stop_tls_proxy(CAM_ID, port_cache, server_cache)


class TestRtspKeepalive:
    """Pin the OPTIONS-keepalive contract. Unaffected by the asyncio
    proxy rewrite — `rtsp_keepalive` already used `asyncio.open_connection`
    directly against the (already-open) proxy port."""

    @pytest.mark.asyncio
    async def test_closes_writer_on_read_timeout(self):
        reader = MagicMock()
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
        def responder(req: bytes, step: int) -> bytes | None:
            return _ok_response()

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is True

    @pytest.mark.asyncio
    async def test_full_digest_handshake_succeeds(self):
        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                assert b"OPTIONS " in req
                assert b"Authorization" not in req
                return _digest_challenge(nonce="N1")
            assert b'Authorization: Digest username="u"' in req
            assert b'nonce="N1"' in req
            return _ok_response()

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is True

    @pytest.mark.asyncio
    async def test_missing_nonce_without_200_returns_false(self):
        def responder(req: bytes, step: int) -> bytes | None:
            return b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 1\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is False

    @pytest.mark.asyncio
    async def test_authenticated_response_not_200_returns_false(self):
        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(nonce="N2")
            return b"RTSP/1.0 401 Unauthorized\r\nCSeq: 2\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await rtsp_keepalive(server.port, "u", "p", "CAM-A")
            assert ok is False

    @pytest.mark.asyncio
    async def test_connection_refused_returns_false(self):
        ok = await rtsp_keepalive(1, "u", "p", "CAM-A")
        assert ok is False

    @pytest.mark.asyncio
    async def test_digest_response_matches_helper(self):
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
        src = inspect.getsource(rtsp_keepalive)
        assert "wait_closed" in src

    def test_keepalive_wait_closed_count_matches_close_count(self):
        src = inspect.getsource(rtsp_keepalive)
        close_count = src.count("writer.close()")
        wait_count = src.count("wait_closed()")
        assert close_count == wait_count, (
            f"rtsp_keepalive has {close_count} writer.close() calls but "
            f"{wait_count} wait_closed() calls — every close must be paired"
        )

    @pytest.mark.asyncio
    async def test_keepalive_no_nonce_no_200_returns_false_and_closes(self):
        wait_closed_called = []

        async def fake_wait_closed():
            wait_closed_called.append(True)

        mock_writer = MagicMock()
        mock_writer.write = MagicMock()
        mock_writer.drain = AsyncMock()
        mock_writer.close = MagicMock()
        mock_writer.wait_closed = fake_wait_closed

        mock_reader = MagicMock()
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
                await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called
        assert len(wait_closed_called) > 0

    def _make_keepalive_mocks(self, resp1_bytes: bytes, wait_closed_raises=False):
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
                await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called

    @pytest.mark.asyncio
    async def test_keepalive_200_no_auth_wait_closed_exception_suppressed(self):
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
                await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called

    @pytest.mark.asyncio
    async def test_keepalive_authenticated_path_wait_closed_exception_suppressed(self):
        resp1 = b'RTSP/1.0 401 Unauthorized\r\nnonce="abc123"\r\nrealm="cam"\r\n\r\n'
        mock_reader, mock_writer = self._make_keepalive_mocks(
            resp1, wait_closed_raises=True
        )
        resp2 = b"RTSP/1.0 200 OK\r\n\r\n"

        with patch(
            "asyncio.open_connection",
            AsyncMock(return_value=(mock_reader, mock_writer)),
        ):
            with patch(
                "asyncio.wait_for",
                new=AsyncMock(side_effect=[(mock_reader, mock_writer), resp1, resp2]),
            ):
                await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called

    @pytest.mark.asyncio
    async def test_keepalive_no_nonce_wait_closed_exception_suppressed(self):
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
                await rtsp_keepalive(12345, "user", "pass", CAM_ID)

        assert mock_writer.close.called


class TestRtspKeepaliveWriterCloseOnException:
    """When an exception is raised AFTER open_connection succeeds (e.g.
    during drain/read) AND writer.close()/wait_closed() itself raises, the
    inner exception must be silently swallowed and the function must return
    False without propagating."""

    @pytest.mark.asyncio
    async def test_writer_close_raises_in_outer_except_is_swallowed(self) -> None:
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
        mock_writer = AsyncMock()
        mock_writer.close = MagicMock()
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
        with patch(
            "custom_components.bosch_shc_camera.tls_proxy.asyncio.open_connection",
            new=AsyncMock(side_effect=ConnectionRefusedError("refused")),
        ):
            result = await rtsp_keepalive(9999, "user", "pass", CAM_ID)

        assert result is False


class TestPreWarmRtsp:
    """Pin the DESCRIBE pre-warm contract. Unaffected by the asyncio proxy
    rewrite — `pre_warm_rtsp` already used `asyncio.open_connection`
    directly against the (already-open) proxy port."""

    @pytest.mark.asyncio
    async def test_describe_happy_path(self):
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
        attempt_count = [0]

        def responder(req: bytes, step: int) -> bytes | None:
            attempt_count[0] += 1
            return b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 1\r\n\r\n"

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
            assert attempt_count[0] == 1

    @pytest.mark.asyncio
    async def test_unreachable_port_exhausts_retries(self):
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
        assert elapsed < 5.0

    @pytest.mark.asyncio
    async def test_uri_includes_required_query_params(self):
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
            assert captured
            first = captured[0]
            assert b"inst=1" in first
            assert b"enableaudio=1" in first
            assert b"fmtp=1" in first
            assert b"maxSessionDuration=60" in first

    @pytest.mark.asyncio
    async def test_post_success_wait_applied(self):
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
            assert elapsed >= 0.9

    def test_pre_warm_also_has_wait_closed(self):
        src = inspect.getsource(pre_warm_rtsp)
        assert "wait_closed" in src

    def test_writer_closed_in_exception_path(self):
        src = inspect.getsource(pre_warm_rtsp)
        src_norm = " ".join(src.replace("(", " ").replace(")", " ").split())
        assert "writer = None" in src_norm
        assert "if writer is not None" in src

    def test_no_nonce_path_awaits_wait_closed(self):
        src = inspect.getsource(pre_warm_rtsp)
        count = src.count("await writer.wait_closed()")
        assert count >= 2


class TestConfirmEncoderReady:
    """Single-shot confirmation DESCRIBE used to shorten the min_total_wait
    blind floor. Must never retry and never sleep post-success — that's the
    whole point vs. pre_warm_rtsp."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_true(self):
        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                assert b"DESCRIBE " in req
                return _digest_challenge(nonce="CONF1")
            assert b"Authorization: Digest" in req
            return _ok_response(b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n")

        async with FakeRtsp(responder) as server:
            ok = await confirm_encoder_ready(
                server.port, "u", "p", "127.0.0.1", describe_timeout=2
            )
            assert ok is True

    @pytest.mark.asyncio
    async def test_no_retry_on_missing_nonce(self):
        attempt_count = [0]

        def responder(req: bytes, step: int) -> bytes | None:
            attempt_count[0] += 1
            return b"RTSP/1.0 500 Internal Server Error\r\nCSeq: 1\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await confirm_encoder_ready(
                server.port, "u", "p", "127.0.0.1", describe_timeout=2
            )
            assert ok is False
            assert attempt_count[0] == 1

    @pytest.mark.asyncio
    async def test_non_200_returns_false(self):
        def responder(req: bytes, step: int) -> bytes | None:
            if step == 0:
                return _digest_challenge(nonce="CONF2")
            return b"RTSP/1.0 503 Service Unavailable\r\nCSeq: 2\r\n\r\n"

        async with FakeRtsp(responder) as server:
            ok = await confirm_encoder_ready(
                server.port, "u", "p", "127.0.0.1", describe_timeout=2
            )
            assert ok is False

    @pytest.mark.asyncio
    async def test_unreachable_port_returns_false_fast(self):
        start = time.monotonic()
        ok = await confirm_encoder_ready(1, "u", "p", "127.0.0.1", describe_timeout=1)
        elapsed = time.monotonic() - start
        assert ok is False
        assert elapsed < 3  # no retry loop — must fail fast

    def test_no_post_success_sleep_param(self):
        sig = inspect.signature(confirm_encoder_ready)
        assert "post_success_wait" not in sig.parameters
        assert "max_attempts" not in sig.parameters

    def test_writer_closed_on_all_paths(self):
        src = inspect.getsource(confirm_encoder_ready)
        assert "writer = None" in src
        assert "if writer is not None" in src
        assert "wait_closed" in src

    @pytest.mark.asyncio
    async def test_writer_close_failure_in_finally_swallowed(self):
        """A raising writer.close()/wait_closed() in the finally block must
        not propagate — mirrors pre_warm_rtsp's equivalent coverage."""

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
                    result = await confirm_encoder_ready(
                        proxy_port, "u", "p", "127.0.0.1", describe_timeout=1
                    )
        finally:
            srv.close()
            await srv.wait_closed()

        assert result is False


class TestRtspHelperContract:
    """Cross-cutting structural contracts spanning keepalive + pre-warm."""

    def test_digest_format_no_spaces_after_commas(self):
        result = _digest_auth("u", "p", "OPTIONS", "/x", "r", "n")
        assert ", " not in result
        assert result.count(",") >= 4

    def test_pre_warm_default_max_attempts_safe(self):
        sig = inspect.signature(pre_warm_rtsp)
        max_attempts_default = sig.parameters["max_attempts"].default
        assert max_attempts_default >= 3

    def test_keepalive_signature_returns_bool(self):
        sig = inspect.signature(rtsp_keepalive)
        assert sig.return_annotation == "bool"


class TestPreWarmMaxSessionDuration:
    """pre_warm_rtsp must accept and use max_session_duration."""

    def test_parameter_accepted(self):
        sig = inspect.signature(pre_warm_rtsp)
        assert "max_session_duration" in sig.parameters

    def test_default_is_60(self):
        sig = inspect.signature(pre_warm_rtsp)
        default = sig.parameters["max_session_duration"].default
        assert default == 60

    def test_no_hardcoded_60_in_uri(self):
        assert 'maxSessionDuration=60"' not in SRC
        assert "maxSessionDuration={max_session_duration}" in SRC

    @pytest.mark.asyncio
    async def test_custom_duration_used_in_uri(self):
        captured_writes: list[bytes] = []

        async def fake_open_connection(host, port):
            reader = MagicMock()

            async def async_read(n):
                return (
                    b'RTSP/1.0 401 Unauthorized\r\nnonce="abc"\r\nrealm="cam"\r\n\r\n'
                )

            reader.read = async_read

            writer = MagicMock()
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

        all_written = b"".join(captured_writes).decode("utf-8", errors="replace")
        assert "maxSessionDuration=3600" in all_written

    def test_caller_passes_max_session_duration(self):
        init_src = (
            Path(__file__).parent.parent
            / "custom_components"
            / "bosch_shc_camera"
            / "live_connection.py"
        ).read_text()

        start = init_src.find("await pre_warm_rtsp(")
        assert start != -1
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
        assert "max_session_duration" in call_text


class TestPreWarmGaps:
    """Retry-sleep + writer/wait_closed cleanup edge cases in pre_warm_rtsp."""

    @pytest.mark.asyncio
    async def test_retry_sleep_on_missing_nonce_with_retries_remaining(self):
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
            assert result is False
            assert attempt_count[0] >= 1
        finally:
            srv.close()
            await srv.wait_closed()

    @pytest.mark.asyncio
    async def test_wait_closed_exception_swallowed(self):
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

            assert result is True

    @pytest.mark.asyncio
    async def test_no_nonce_wait_closed_exception_swallowed(self):
        async def _handle(reader, writer):
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                    if not chunk:
                        return
                    data += chunk
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

        assert result is False

    @pytest.mark.asyncio
    async def test_exception_path_closes_writer_if_open(self):
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

        assert result is False
        assert close_called[0]


class TestPreWarmExceptionWaitClosedRaises:
    """When the outer try in pre_warm_rtsp catches an exception AND the
    writer was already assigned, writer cleanup exceptions must be
    swallowed so the retry loop can continue."""

    @pytest.mark.asyncio
    async def test_wait_closed_in_exception_path_swallowed(self):
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

    @pytest.mark.asyncio
    async def test_writer_close_in_exception_path_swallowed(self):
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


class TestHlsEvictionRaised:
    """`_HLS_ACCESS_MAX` raised to 256; active token skipped during eviction.

    Note: `cf_unbuffer.py` is a sibling module (not tls_proxy.py itself), but
    this eviction fix historically landed in the same bug-hunt round as
    several tls_proxy fixes and its regression tests were grouped with
    them — kept together here rather than splitting into a separate file.
    Entirely unaffected by the asyncio proxy rewrite.
    """

    def _cf(self):
        import custom_components.bosch_shc_camera.cf_unbuffer as cf

        cf._HLS_ACCESS.clear()
        return cf

    def test_cap_raised_to_256(self):
        from custom_components.bosch_shc_camera.cf_unbuffer import _HLS_ACCESS_MAX

        assert _HLS_ACCESS_MAX == 256

    def test_active_window_constant_in_source(self):
        assert "_HLS_ACTIVE_WINDOW" in UNBUF_SRC

    def test_prune_still_caps_at_max(self):
        cf = self._cf()
        for i in range(cf._HLS_ACCESS_MAX + 20):
            with patch.object(cf.time, "monotonic", return_value=float(i)):
                cf._note_hls_access(
                    SimpleNamespace(path=f"/api/hls/tok{i}/playlist.m3u8")
                )
        assert len(cf._HLS_ACCESS) <= cf._HLS_ACCESS_MAX

    def test_recently_active_token_not_evicted(self):
        cf = self._cf()
        for i in range(cf._HLS_ACCESS_MAX):
            with patch.object(cf.time, "monotonic", return_value=float(i)):
                cf._note_hls_access(
                    SimpleNamespace(path=f"/api/hls/old{i}/playlist.m3u8")
                )

        oldest_token = "old0"
        assert oldest_token in cf._HLS_ACCESS

        recent_now = float(cf._HLS_ACCESS_MAX) + 1.0
        cf._HLS_ACCESS[oldest_token] = recent_now - 5.0

        with patch.object(cf.time, "monotonic", return_value=recent_now):
            cf._note_hls_access(
                SimpleNamespace(path="/api/hls/new_overflow/playlist.m3u8")
            )

        assert oldest_token in cf._HLS_ACCESS

    def test_stale_token_evicted_when_active_present(self):
        cf = self._cf()

        cf._HLS_ACCESS["stale_tok"] = 1.0
        cf._HLS_ACCESS["recent_tok"] = 999.0

        for i in range(cf._HLS_ACCESS_MAX - 2):
            cf._HLS_ACCESS[f"mid{i}"] = float(50 + i)

        assert len(cf._HLS_ACCESS) == cf._HLS_ACCESS_MAX

        recent_now = 1010.0
        with patch.object(cf.time, "monotonic", return_value=recent_now):
            cf._note_hls_access(
                SimpleNamespace(path="/api/hls/overflow_tok/playlist.m3u8")
            )

        assert "stale_tok" not in cf._HLS_ACCESS
        assert len(cf._HLS_ACCESS) <= cf._HLS_ACCESS_MAX
