"""TLS proxy for Bosch Smart Home Camera LOCAL RTSPS streams.

Bosch cameras use RTSPS (RTSP over TLS) with a self-signed certificate
and Digest auth. FFmpeg/HA's stream component can't handle this combination.
This module provides a TCP→TLS proxy that accepts plain TCP connections on
localhost and forwards them to the camera over TLS. FFmpeg handles Digest
auth itself — the proxy only unwraps TLS.

Uses threading (not asyncio) because HA's stream_worker runs in a separate
thread and the asyncio event loop may be busy during stream negotiation.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import select as _select
import socket
import ssl
import threading
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Track server sockets so we can close them on stop
_proxy_servers: dict[str, socket.socket] = {}


def start_tls_proxy(
    ssl_ctx: ssl.SSLContext,
    cam_id: str,
    cam_host: str,
    cam_port: int,
    port_cache: dict[str, int],
) -> int:
    """Start a local TCP→TLS proxy for a LOCAL RTSPS stream.

    Stops any existing proxy for this cam_id first, then starts fresh.
    Tries to reuse the same port (SO_REUSEADDR) so HA's cached Stream URL
    stays valid when the proxy is recycled.
    """
    old_port = port_cache.get(cam_id)
    # Always stop existing proxy to avoid stale state
    if cam_id in port_cache:
        _LOGGER.debug(
            "TLS proxy for %s: stopping existing proxy on port %d before restart",
            cam_id[:8], port_cache[cam_id],
        )
        stop_tls_proxy(cam_id, port_cache)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Try to reuse old port so HA's cached stream URL stays valid
    if old_port:
        try:
            srv.bind(("127.0.0.1", old_port))
        except OSError:
            srv.bind(("127.0.0.1", 0))
    else:
        srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(4)
    srv.settimeout(None)
    _proxy_servers[cam_id] = srv

    def _proxy_thread() -> None:
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                break
            try:
                raw = socket.create_connection((cam_host, cam_port), timeout=10)
                tls = ssl_ctx.wrap_socket(raw, server_hostname=cam_host)
                _LOGGER.debug(
                    "TLS proxy %s: connected to %s:%d (TLS %s, cipher %s)",
                    cam_id[:8], cam_host, cam_port,
                    tls.version(), tls.cipher()[0] if tls.cipher() else "?",
                )
            except Exception as exc:
                _LOGGER.warning(
                    "TLS proxy %s: failed to connect to %s:%d — %s",
                    cam_id[:8], cam_host, cam_port, exc,
                )
                client.close()
                continue

            def _pipe(
                src: socket.socket,
                dst: socket.socket,
                rewrite_transport: bool = False,
            ) -> None:
                """Forward bytes. If rewrite_transport=True, intercept RTSP
                SETUP requests and force TCP interleaved transport so FFmpeg
                doesn't try UDP (which can't work through the TCP proxy)."""
                try:
                    while True:
                        r, _, _ = _select.select([src], [], [], 60)
                        if not r:
                            break
                        data = src.recv(65536)
                        if not data:
                            break
                        if rewrite_transport and b"SETUP " in data:
                            # Replace UDP transport with TCP interleaved
                            text = data.decode("utf-8", errors="replace")
                            text = re.sub(
                                r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+",
                                "Transport: RTP/AVP/TCP;unicast;interleaved=0-1",
                                text,
                            )
                            data = text.encode("utf-8")
                        dst.sendall(data)
                except Exception:
                    pass
                finally:
                    try:
                        src.close()
                    except Exception:
                        pass
                    try:
                        dst.close()
                    except Exception:
                        pass

            # client→camera: rewrite SETUP Transport to force TCP interleaved
            t1 = threading.Thread(
                target=_pipe, args=(client, tls, True), daemon=True
            )
            t2 = threading.Thread(
                target=_pipe, args=(tls, client, False), daemon=True
            )
            t1.start()
            t2.start()

    ready = threading.Event()

    def _proxy_thread_with_signal() -> None:
        ready.set()
        _proxy_thread()

    t = threading.Thread(
        target=_proxy_thread_with_signal,
        daemon=True,
        name=f"tls_proxy_{cam_id[:8]}",
    )
    t.start()
    ready.wait(timeout=2)
    port_cache[cam_id] = port
    _LOGGER.info(
        "TLS proxy for %s started on 127.0.0.1:%d -> %s:%d (threading)",
        cam_id[:8],
        port,
        cam_host,
        cam_port,
    )
    return port


def stop_tls_proxy(cam_id: str, port_cache: dict[str, int]) -> None:
    """Stop the TLS proxy for a camera by closing its server socket."""
    port_cache.pop(cam_id, None)
    srv = _proxy_servers.pop(cam_id, None)
    if srv is not None:
        try:
            srv.close()
            _LOGGER.debug("TLS proxy for %s: server socket closed", cam_id[:8])
        except Exception:
            pass


async def rtsp_keepalive(
    proxy_port: int, user: str, password: str, cam_id: str
) -> bool:
    """Send an RTSP OPTIONS keepalive through the proxy to prevent 60s timeout.

    The Bosch camera enforces a 60-second session timeout regardless of
    maxSessionDuration in the URL.  Sending an authenticated OPTIONS every
    ~30s resets the inactivity timer and keeps the TCP connection alive for
    FFmpeg/go2rtc.

    Returns True if the keepalive succeeded (camera replied 200 OK).
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", proxy_port), timeout=5
        )
        uri = f"rtsp://127.0.0.1:{proxy_port}/rtsp_tunnel"

        # Step 1: OPTIONS without auth → 401 + realm/nonce
        writer.write(
            f"OPTIONS {uri} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"\r\n".encode()
        )
        await writer.drain()
        resp1 = await asyncio.wait_for(reader.read(4096), timeout=5)
        resp1_str = resp1.decode("utf-8", errors="replace")

        nonce_m = re.search(r'nonce="([^"]+)"', resp1_str)
        realm_m = re.search(r'realm="([^"]+)"', resp1_str)
        if not (nonce_m and realm_m):
            # Camera may respond 200 without auth challenge — that's fine too
            if "200 OK" in resp1_str:
                _LOGGER.debug("Keepalive OPTIONS 200 OK (no auth needed) on port %d", proxy_port)
                writer.close()
                return True
            _LOGGER.debug(
                "Keepalive: no nonce/realm on port %d (%.100s)", proxy_port, resp1_str
            )
            writer.close()
            return False

        nonce, realm = nonce_m.group(1), realm_m.group(1)
        auth = _digest_auth(user, password, "OPTIONS", uri, realm, nonce)

        # Step 2: authenticated OPTIONS
        writer.write(
            f"OPTIONS {uri} RTSP/1.0\r\n"
            f"CSeq: 2\r\n"
            f"Authorization: {auth}\r\n"
            f"\r\n".encode()
        )
        await writer.drain()
        resp2 = await asyncio.wait_for(reader.read(4096), timeout=5)
        resp2_str = resp2.decode("utf-8", errors="replace")
        writer.close()

        if "200 OK" in resp2_str:
            _LOGGER.debug("Keepalive OPTIONS 200 OK on port %d", proxy_port)
            return True
        _LOGGER.debug(
            "Keepalive: unexpected response on port %d: %.100s", proxy_port, resp2_str
        )
        return False
    except Exception as exc:
        _LOGGER.debug("Keepalive failed on port %d: %s", proxy_port, exc)
        return False


def _digest_auth(
    user: str, password: str, method: str, uri: str,
    realm: str, nonce: str,
) -> str:
    """Compute Digest auth header value."""
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return (
        f'Digest username="{user}",realm="{realm}",'
        f'nonce="{nonce}",uri="{uri}",response="{resp}"'
    )


async def pre_warm_rtsp(
    proxy_port: int, user: str, password: str, cam_host: str
) -> None:
    """Pre-warm camera's H.264 encoder via authenticated RTSP DESCRIBE.

    After PUT /connection LOCAL returns credentials, the camera needs a moment
    to initialize its encoder. Sending an authenticated DESCRIBE (codec
    negotiation) wakes the encoder so it's ready when FFmpeg connects.

    Only DESCRIBE — no SETUP/PLAY — so no RTSP session is created.
    This avoids conflicts with FFmpeg which needs to start its own session.

    Sequence: DESCRIBE (unauth) → 401 → DESCRIBE (digest) → 200 OK (SDP)
    """
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        uri = (
            f"rtsp://127.0.0.1:{proxy_port}"
            "/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=60"
        )

        # Step 1: DESCRIBE without auth → 401 + nonce/realm
        writer.write(
            f"DESCRIBE {uri} RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"Accept: application/sdp\r\n"
            f"\r\n".encode()
        )
        await writer.drain()
        resp1 = await asyncio.wait_for(reader.read(4096), timeout=3)

        # Step 2: Parse nonce, send authenticated DESCRIBE
        resp1_str = resp1.decode("utf-8", errors="replace")
        nonce_m = re.search(r'nonce="([^"]+)"', resp1_str)
        realm_m = re.search(r'realm="([^"]+)"', resp1_str)
        if not (nonce_m and realm_m):
            _LOGGER.debug(
                "Pre-warm RTSP: no nonce/realm in response (port %d): %.200s",
                proxy_port, resp1_str,
            )
            writer.close()
            return
        nonce, realm = nonce_m.group(1), realm_m.group(1)

        auth = _digest_auth(user, password, "DESCRIBE", uri, realm, nonce)
        writer.write(
            f"DESCRIBE {uri} RTSP/1.0\r\n"
            f"CSeq: 2\r\n"
            f"Accept: application/sdp\r\n"
            f"Authorization: {auth}\r\n"
            f"\r\n".encode()
        )
        await writer.drain()
        resp2 = await asyncio.wait_for(reader.read(8192), timeout=3)
        resp2_str = resp2.decode("utf-8", errors="replace")

        if "200 OK" in resp2_str:
            _LOGGER.debug("Pre-warm RTSP complete (DESCRIBE 200 OK) on port %d", proxy_port)
        else:
            _LOGGER.warning(
                "Pre-warm RTSP: unexpected response on port %d: %.200s",
                proxy_port, resp2_str,
            )

        writer.close()
    except Exception as exc:
        _LOGGER.debug("Pre-warm RTSP failed on port %d: %s", proxy_port, exc)
