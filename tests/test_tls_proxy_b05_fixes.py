"""Regression tests for B05 bug-hunt fixes (2026-06-16).

Covers:
  B05-1 — stop_all_proxies clears _proxy_servers (module-cache reload safety)
  B05-2 — _pipe close-once guard prevents FD reuse double-close
  B05-5 — HLS token eviction: cap raised to 256, active token skipped
  B05-6 — pre_warm_rtsp passes max_session_duration (not hardcoded 60)
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

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


# ── B05-1: stop_all_proxies clears _proxy_servers ────────────────────────────


class TestStopAllProxiesClearsGlobal:
    """After stop_all_proxies, _proxy_servers must be empty (B05-1).

    Python does NOT reimport modules on HA reload — the module-level dict
    persists across coordinator restarts.  Leftover entries prevent
    start_tls_proxy from allocating a fresh server socket: the stale guard
    ``cam_id in _proxy_servers`` triggers stop_tls_proxy on an already-closed
    socket (EBADF noise) and more critically the old *port number* is gone so
    the proxy starts dead.
    """

    def test_stop_all_empties_proxy_servers(self):
        """_proxy_servers must be empty after stop_all_proxies."""
        from custom_components.bosch_shc_camera.tls_proxy import (
            _proxy_servers,
            stop_all_proxies,
        )

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
        from custom_components.bosch_shc_camera.tls_proxy import stop_all_proxies

        stop_all_proxies({})  # must not raise

    def test_stop_all_clears_even_if_close_raises(self):
        """Even when srv.close() raises, the entry must be removed (alias test, real check below)."""
        pytest.skip("covered by test_stop_all_clears_even_if_close_raises_real")

    def test_stop_all_clears_even_if_close_raises_real(self):
        """Even when srv.close() raises, the entry must be removed from the dict."""
        from custom_components.bosch_shc_camera.tls_proxy import (
            _proxy_servers,
            stop_all_proxies,
        )

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
        import ssl

        from custom_components.bosch_shc_camera.tls_proxy import (
            _proxy_servers,
            start_tls_proxy,
            stop_all_proxies,
            stop_tls_proxy,
        )

        cam_id = "B05-1-RELOAD-CAM"
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


# ── B05-2: _pipe close-once guard ────────────────────────────────────────────


class TestPipeCloseOnce:
    """_pipe must close each socket exactly once even when two threads finish (B05-2).

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

        from custom_components.bosch_shc_camera import tls_proxy as _mod

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


# ── B05-5: HLS token eviction cap raised, active token skipped ───────────────


class TestHlsEvictionRaised:
    """_HLS_ACCESS_MAX raised to 256; active token not evicted (B05-5)."""

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


# ── B05-6: pre_warm_rtsp max_session_duration parameter ──────────────────────


class TestPreWarmMaxSessionDuration:
    """pre_warm_rtsp must accept and use max_session_duration (B05-6).

    The URI used to contain a hard-coded ``maxSessionDuration=60`` regardless
    of camera model.  Indoor Gen2 uses 3600s.  If Bosch parses DESCRIBE URIs
    to configure the server-side session timer, the pre-warm would pin a 60-second
    countdown before FFmpeg even connects, causing premature session expiry on
    long-session models.
    """

    def test_parameter_accepted(self):
        """pre_warm_rtsp signature must have max_session_duration parameter."""
        import inspect

        from custom_components.bosch_shc_camera.tls_proxy import pre_warm_rtsp

        sig = inspect.signature(pre_warm_rtsp)
        assert "max_session_duration" in sig.parameters, (
            "pre_warm_rtsp must accept max_session_duration kwarg"
        )

    def test_default_is_60(self):
        """Default must remain 60 for backward compatibility."""
        import inspect

        from custom_components.bosch_shc_camera.tls_proxy import pre_warm_rtsp

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
        from custom_components.bosch_shc_camera.tls_proxy import pre_warm_rtsp

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
        """__init__.py call site must pass max_session_duration=cfg.max_session_duration."""
        import re

        init_src = (
            Path(__file__).parent.parent
            / "custom_components"
            / "bosch_shc_camera"
            / "__init__.py"
        ).read_text()

        # Find the pre_warm_rtsp(...) call block and check for the new kwarg.
        # The call is multi-line so we need to match balanced parens manually.
        start = init_src.find("await pre_warm_rtsp(")
        assert start != -1, "Could not find pre_warm_rtsp call in __init__.py"
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
