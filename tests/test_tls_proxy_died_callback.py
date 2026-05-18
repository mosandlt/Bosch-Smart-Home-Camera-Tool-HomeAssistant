"""Regression tests for tls_proxy.start_tls_proxy on_proxy_died callback.

Bug 2026-05-18: a Gen2 Indoor camera delivered 5 TLS resets in 3s during
WiFi jitter. Circuit breaker closed server socket at
tls_proxy.py:124-128 with log "Coordinator will rebuild the session when
the camera is back" — but no signal reached the coordinator. Camera-state
stayed stale on "streaming", stream_url pointed at the dead port, HA's
stream_worker got "Connection refused" forever. User had to toggle the
live-stream switch off→on manually to recover.

Fix: add `on_proxy_died` callback parameter; circuit breaker invokes it
after srv.close() so the coordinator can schedule a rebuild.

Tests pin:
  - on_proxy_died is called exactly once when circuit breaker fires
  - on_proxy_died exceptions are swallowed (proxy thread must not crash)
  - omitting on_proxy_died stays backward-compatible (no callback, no error)
"""

from __future__ import annotations

import ssl
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SRC = (
    Path(__file__).parent.parent
    / "custom_components" / "bosch_shc_camera" / "tls_proxy.py"
).read_text()


def _ssl_ctx():
    return MagicMock(spec=ssl.SSLContext)


def _server_mock(port: int = 54300):
    m = MagicMock()
    m.getsockname.return_value = ("127.0.0.1", port)
    return m


def _run_breaker_with_callback(callback, n_clients: int = 5):
    """Trip the circuit breaker n_clients times, return (srv_mock, accept_count)."""
    cam_id = f"CB-CB-{threading.get_ident()}"
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
        patch("custom_components.bosch_shc_camera.tls_proxy.socket.socket", return_value=srv),
        patch(
            "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ),
    ):
        from custom_components.bosch_shc_camera.tls_proxy import (
            start_tls_proxy, stop_tls_proxy,
        )
        start_tls_proxy(
            ctx, cam_id, "192.0.2.1", 443, cache,
            on_proxy_died=callback,
        )
        # Daemon thread needs time to chew through all failures and fire callback
        time.sleep(0.6)
        try:
            stop_tls_proxy(cam_id, cache)
        except Exception:
            pass

    return srv, call_count[0]


# ── on_proxy_died fires after circuit breaker ─────────────────────────────


def test_on_proxy_died_called_after_circuit_breaker() -> None:
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


def test_on_proxy_died_exception_swallowed() -> None:
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
    assert fired.wait(timeout=1.0), "callback did not run — can't test exception path"


def test_on_proxy_died_optional_backward_compat() -> None:
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
        patch("custom_components.bosch_shc_camera.tls_proxy.socket.socket", return_value=srv),
        patch(
            "custom_components.bosch_shc_camera.tls_proxy.socket.create_connection",
            side_effect=OSError("Connection refused"),
        ),
    ):
        from custom_components.bosch_shc_camera.tls_proxy import (
            start_tls_proxy, stop_tls_proxy,
        )
        # No on_proxy_died — must not crash
        start_tls_proxy(ctx, cam_id, "192.0.2.1", 443, cache)
        time.sleep(0.4)
        try:
            stop_tls_proxy(cam_id, cache)
        except Exception:
            pass

    assert srv.close.called, "circuit breaker still fires without callback"


# ── Source pin: parameter present in signature ────────────────────────────


def test_on_proxy_died_parameter_in_signature() -> None:
    """Pin the public API: start_tls_proxy must accept on_proxy_died kwarg."""
    import inspect
    from custom_components.bosch_shc_camera.tls_proxy import start_tls_proxy

    sig = inspect.signature(start_tls_proxy)
    assert "on_proxy_died" in sig.parameters, (
        "start_tls_proxy signature must include on_proxy_died kwarg — "
        "callback wiring depends on this name"
    )
    assert sig.parameters["on_proxy_died"].default is None, (
        "on_proxy_died must default to None for backward compatibility"
    )
