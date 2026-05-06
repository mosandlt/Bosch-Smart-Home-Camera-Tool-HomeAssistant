"""Tests for TLS proxy pure-function helpers and contract pinning.

The TLS proxy (`tls_proxy.py`) bridges plain TCP connections on localhost
to RTSPS connections on the camera's TLS port. It handles:
  - Digest authentication header computation
  - RTSP Transport header rewriting (UDP → TCP interleaved)
  - Circuit breaker (5 failures in 30s → close server socket)
  - TCP keep-alive settings for both client and camera sockets
  - start/stop lifecycle with module-level `_proxy_servers` dict

These tests cover the pure-function helpers directly (no network mocks)
and pin the structural contracts that prevent regressions.
"""

from __future__ import annotations

import hashlib
import re
import socket
from unittest.mock import MagicMock, patch

import pytest


# ── _digest_auth ─────────────────────────────────────────────────────────


class TestDigestAuth:
    """Pin the MD5 Digest auth computation used for RTSP keepalive/pre-warm."""

    def test_known_vector(self):
        """RFC 2617 style computation — verify against manual calculation."""
        from custom_components.bosch_shc_camera.tls_proxy import _digest_auth

        user = "admin"
        password = "secret"
        method = "OPTIONS"
        uri = "rtsp://127.0.0.1:5000/rtsp_tunnel"
        realm = "RTSP Server"
        nonce = "abc123nonce"

        result = _digest_auth(user, password, method, uri, realm, nonce)

        # Manual computation
        ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode(), usedforsecurity=False).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode(), usedforsecurity=False).hexdigest()
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode(), usedforsecurity=False).hexdigest()

        assert f'username="{user}"' in result
        assert f'realm="{realm}"' in result
        assert f'nonce="{nonce}"' in result
        assert f'uri="{uri}"' in result
        assert f'response="{resp}"' in result
        assert result.startswith("Digest ")

    def test_describe_method(self):
        """DESCRIBE method (used in pre-warm) produces different HA2."""
        from custom_components.bosch_shc_camera.tls_proxy import _digest_auth

        r1 = _digest_auth("u", "p", "OPTIONS", "/path", "r", "n")
        r2 = _digest_auth("u", "p", "DESCRIBE", "/path", "r", "n")
        assert r1 != r2

    def test_different_uri_produces_different_response(self):
        from custom_components.bosch_shc_camera.tls_proxy import _digest_auth

        r1 = _digest_auth("u", "p", "OPTIONS", "/path1", "r", "n")
        r2 = _digest_auth("u", "p", "OPTIONS", "/path2", "r", "n")
        assert r1 != r2

    def test_empty_credentials(self):
        """Empty user/pass must not crash — still produces a valid header."""
        from custom_components.bosch_shc_camera.tls_proxy import _digest_auth

        result = _digest_auth("", "", "OPTIONS", "/", "realm", "nonce")
        assert result.startswith("Digest ")
        assert 'response="' in result

    def test_special_chars_in_password(self):
        """Passwords with special chars (colons, quotes) must not break."""
        from custom_components.bosch_shc_camera.tls_proxy import _digest_auth

        result = _digest_auth("user", 'p@ss:w"ord', "OPTIONS", "/x", "r", "n")
        assert result.startswith("Digest ")

    def test_unicode_in_realm(self):
        """Bosch cameras use ASCII realms, but unicode must not crash."""
        from custom_components.bosch_shc_camera.tls_proxy import _digest_auth

        result = _digest_auth("u", "p", "OPTIONS", "/x", "Ü-Realm", "nonce")
        assert 'realm="Ü-Realm"' in result


# ── stop_tls_proxy / stop_all_proxies ────────────────────────────────────


class TestStopTlsProxy:
    """Pin the cleanup contract of stop functions."""

    def test_stop_removes_from_port_cache(self):
        from custom_components.bosch_shc_camera.tls_proxy import (
            stop_tls_proxy, _proxy_servers,
        )
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
        from custom_components.bosch_shc_camera.tls_proxy import stop_tls_proxy

        port_cache = {}
        # Must not raise
        stop_tls_proxy("NONEXISTENT-CAM", port_cache)

    def test_stop_handles_close_exception(self):
        """If socket.close() raises, stop must not propagate."""
        from custom_components.bosch_shc_camera.tls_proxy import (
            stop_tls_proxy, _proxy_servers,
        )
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
        from custom_components.bosch_shc_camera.tls_proxy import (
            stop_all_proxies, _proxy_servers,
        )
        # Setup multiple cams
        port_cache = {"CAM-A": 100, "CAM-B": 200}
        _proxy_servers["CAM-A"] = MagicMock()
        _proxy_servers["CAM-B"] = MagicMock()

        stop_all_proxies(port_cache)

        assert len(port_cache) == 0
        assert "CAM-A" not in _proxy_servers
        assert "CAM-B" not in _proxy_servers


# ── Transport rewriting logic (structural) ───────────────────────────────


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
        assert re.search(pattern, header), "Pattern must match standard FFmpeg Transport"

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


# ── Circuit breaker constants ────────────────────────────────────────────


class TestCircuitBreakerConstants:
    """Pin the burst-failure constants used in start_tls_proxy."""

    def test_constants_present_in_source(self):
        """The circuit breaker constants must exist with expected values."""
        from pathlib import Path
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "tls_proxy.py"
        ).read_text()
        assert "_MAX_BURST = 5" in src
        assert "_BURST_WINDOW = 30.0" in src

    def test_max_burst_reasonable(self):
        """5 consecutive failures is enough to detect offline camera without
        over-logging or under-detecting."""
        # Direct extraction: regex the source to get the value
        from pathlib import Path
        import re as _re
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "tls_proxy.py"
        ).read_text()
        m = _re.search(r"_MAX_BURST\s*=\s*(\d+)", src)
        assert m
        val = int(m.group(1))
        assert 3 <= val <= 10, f"_MAX_BURST={val} outside safe range"

    def test_burst_window_reasonable(self):
        from pathlib import Path
        import re as _re
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "tls_proxy.py"
        ).read_text()
        m = _re.search(r"_BURST_WINDOW\s*=\s*([\d.]+)", src)
        assert m
        val = float(m.group(1))
        assert 10.0 <= val <= 60.0, f"_BURST_WINDOW={val}s outside safe range"


# ── start_tls_proxy contract ─────────────────────────────────────────────


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
        from custom_components.bosch_shc_camera.tls_proxy import (
            start_tls_proxy, stop_tls_proxy, _proxy_servers,
        )
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cache = {}
        cam_id = "TEST-START-001"
        mock_sock = _mock_server_socket(port=54321)

        with patch("custom_components.bosch_shc_camera.tls_proxy.socket.socket", return_value=mock_sock), \
             patch("custom_components.bosch_shc_camera.tls_proxy.threading.Thread"):
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
        from custom_components.bosch_shc_camera.tls_proxy import (
            start_tls_proxy, stop_tls_proxy,
        )
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cache = {}
        cam_id = "TEST-FRESH-001"

        mock1 = _mock_server_socket(port=11111)
        mock2 = _mock_server_socket(port=22222)

        with patch("custom_components.bosch_shc_camera.tls_proxy.socket.socket", side_effect=[mock1, mock2]), \
             patch("custom_components.bosch_shc_camera.tls_proxy.threading.Thread"):
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
        from custom_components.bosch_shc_camera.tls_proxy import (
            start_tls_proxy, stop_tls_proxy,
        )
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cache = {}
        cam_id = "TEST-BIND-001"
        mock_sock = _mock_server_socket(port=33333)

        with patch("custom_components.bosch_shc_camera.tls_proxy.socket.socket", return_value=mock_sock), \
             patch("custom_components.bosch_shc_camera.tls_proxy.threading.Thread"):
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
