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


def start_tls_proxy(
    ssl_ctx: ssl.SSLContext,
    cam_id: str,
    cam_host: str,
    cam_port: int,
    port_cache: dict[str, int],
) -> int:
    """Start a local TCP→TLS proxy for a LOCAL RTSPS stream.

    Args:
        ssl_ctx: Pre-created SSL context (check_hostname=False, verify_mode=CERT_NONE).
        cam_id: Camera identifier (used for logging and port_cache key).
        cam_host: Camera IP address.
        cam_port: Camera TLS port (typically 443).
        port_cache: Dict mapping cam_id → proxy port. Updated in-place.

    Returns:
        The local proxy port number on 127.0.0.1.
    """
    # Reuse existing proxy if already running for this camera
    if cam_id in port_cache:
        port = port_cache[cam_id]
        # Quick check if port is still listening
        try:
            test = socket.create_connection(("127.0.0.1", port), timeout=0.5)
            test.close()
            return port  # proxy still alive
        except OSError:
            pass  # proxy dead, start new one
        port_cache.pop(cam_id, None)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(4)
    srv.settimeout(None)

    def _proxy_thread() -> None:
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                break
            try:
                raw = socket.create_connection((cam_host, cam_port), timeout=10)
                tls = ssl_ctx.wrap_socket(raw, server_hostname=cam_host)
            except Exception:
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

    # Use an Event to wait for the proxy thread to be ready (accepting connections)
    # before returning. Without this, FFmpeg connects before the thread calls accept().
    ready = threading.Event()

    def _proxy_thread_with_signal() -> None:
        ready.set()  # Signal that we're about to accept
        _proxy_thread()

    t = threading.Thread(
        target=_proxy_thread_with_signal,
        daemon=True,
        name=f"tls_proxy_{cam_id[:8]}",
    )
    t.start()
    ready.wait(timeout=2)  # Wait up to 2s for thread to be ready
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
    """Stop the TLS proxy for a camera (removes from port cache)."""
    port_cache.pop(cam_id, None)


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
        await asyncio.wait_for(reader.read(8192), timeout=3)

        writer.close()
        _LOGGER.debug("Pre-warm RTSP complete (DESCRIBE only) on port %d", proxy_port)
    except Exception:
        pass  # Pre-warm is best-effort, failure is not critical
