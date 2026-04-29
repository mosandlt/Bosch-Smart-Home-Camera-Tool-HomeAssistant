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
import time
import ssl
import threading


_LOGGER = logging.getLogger(__name__)

# Track server sockets so we can close them on stop
_proxy_servers: dict[str, socket.socket] = {}


def start_tls_proxy(
    ssl_ctx: ssl.SSLContext,
    cam_id: str,
    cam_host: str,
    cam_port: int,
    port_cache: dict[str, int],
    debug: bool = False,
    is_renewal: bool = False,
) -> int:
    """Start a local TCP→TLS proxy for a LOCAL RTSPS stream.

    Always creates a fresh proxy on each session — credential changes from
    PUT /connection require a new port so HA's stream worker builds a fresh
    RTSP URL with the new credentials instead of retrying cached old ones.
    """
    # Always stop any existing proxy first — fresh start per session
    if cam_id in port_cache or cam_id in _proxy_servers:
        stop_tls_proxy(cam_id, port_cache)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(4)
    srv.settimeout(None)
    _proxy_servers[cam_id] = srv

    # Burst-failure circuit breaker: when the camera goes physically offline
    # (Privacy hardware button, power cut, WiFi drop) HA's stream worker keeps
    # opening new client connections every few seconds, and each one triggers
    # an upstream connect attempt that times out / returns Errno 113. Without
    # a cap we log dozens of "failed to connect" warnings and burn CPU on
    # 10 s connect timeouts. After _MAX_BURST consecutive failures within
    # _BURST_WINDOW seconds we close the server socket — coordinator will
    # detect the situation (privacy_mode flag, OFFLINE status) and either
    # tear down the live session entirely or restart the proxy via
    # try_live_connection() once the camera is reachable again.
    _MAX_BURST = 5
    _BURST_WINDOW = 30.0
    fail_count = [0]
    first_fail_at = [0.0]

    def _proxy_thread() -> None:
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                break
            try:
                raw = socket.create_connection((cam_host, cam_port), timeout=10)
                # TCP keep-alive: prevent OS from dropping idle connections.
                raw.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                try:
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                    raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
                except (AttributeError, OSError):
                    pass
                try:
                    tls = ssl_ctx.wrap_socket(raw, server_hostname=cam_host)
                except Exception:
                    raw.close()  # close raw socket if TLS handshake fails
                    raise
                _LOGGER.debug(
                    "TLS proxy %s: connected to %s:%d (TLS %s, cipher %s)",
                    cam_id[:8], cam_host, cam_port,
                    tls.version(), tls.cipher()[0] if tls.cipher() else "?",
                )
                # Reset failure burst — a successful connect proves the
                # camera is reachable again.
                fail_count[0] = 0
                first_fail_at[0] = 0.0
            except Exception as exc:
                now = time.monotonic()
                if fail_count[0] == 0:
                    first_fail_at[0] = now
                fail_count[0] += 1
                _LOGGER.warning(
                    "TLS proxy %s: failed to connect to %s:%d — %s",
                    cam_id[:8], cam_host, cam_port, exc,
                )
                client.close()
                if (
                    fail_count[0] >= _MAX_BURST
                    and (now - first_fail_at[0]) <= _BURST_WINDOW
                ):
                    _LOGGER.warning(
                        "TLS proxy %s: %d consecutive connect failures in %.0fs — "
                        "closing server socket (camera unreachable). "
                        "Coordinator will rebuild the session when the camera is back.",
                        cam_id[:8], fail_count[0], now - first_fail_at[0],
                    )
                    try:
                        srv.close()
                    except Exception:
                        pass
                    break
                continue

            # TCP keep-alive on client socket too (FFmpeg side)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                client.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except (AttributeError, OSError):
                pass
            _dbg_count = [0]  # shared debug exchange counter

            def _pipe(
                src: socket.socket,
                dst: socket.socket,
                rewrite_transport: bool = False,
                direction: str = "???",
            ) -> None:
                """Forward bytes. If rewrite_transport=True, intercept RTSP
                SETUP requests and force TCP interleaved transport so FFmpeg
                doesn't try UDP (which can't work through the TCP proxy)."""
                _interleaved_counter = [0]  # tracks next interleaved channel pair
                try:
                    while True:
                        # CAM→C (rewrite_transport=False): no timeout — dark/still
                        # scenes have sparse RTP packets; TCP keepalive handles dead
                        # connections. C→CAM (rewrite_transport=True): 120s timeout.
                        pipe_timeout = 120 if rewrite_transport else None
                        r, _, _ = _select.select([src], [], [], pipe_timeout)
                        if not r:
                            break
                        data = src.recv(65536)
                        if not data:
                            break
                        # Debug: log first RTSP exchanges (text only, skip binary RTP)
                        if debug and _dbg_count[0] < 20 and len(data) < 2000 and data[:1] != b"$":
                            _dbg_count[0] += 1
                            preview = data[:500].decode("utf-8", errors="replace").replace("\r\n", "\\r\\n")
                            _LOGGER.debug(
                                "TLS proxy %s [%s] %d bytes: %.500s",
                                cam_id[:8], direction, len(data), preview,
                            )
                        if rewrite_transport and b"SETUP " in data:
                            # Replace UDP transport with TCP interleaved
                            text = data.decode("utf-8", errors="replace")
                            lo = _interleaved_counter[0]
                            hi = lo + 1
                            text = re.sub(
                                r"Transport:\s*RTP/AVP[^;\r\n]*;unicast;client_port=[^\r\n]+",
                                f"Transport: RTP/AVP/TCP;unicast;interleaved={lo}-{hi}",
                                text,
                            )
                            _interleaved_counter[0] = hi + 1
                            data = text.encode("utf-8")
                        dst.sendall(data)
                except Exception as exc:
                    if debug and str(exc):
                        _LOGGER.debug("TLS proxy %s [%s] pipe error: %s", cam_id[:8], direction, exc)
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
                target=_pipe, args=(client, tls, True, "C→CAM"), daemon=True
            )
            t2 = threading.Thread(
                target=_pipe, args=(tls, client, False, "CAM→C"), daemon=True
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


def stop_all_proxies(port_cache: dict[str, int]) -> None:
    """Stop all TLS proxies — called during integration unload."""
    for cam_id in list(_proxy_servers.keys()):
        stop_tls_proxy(cam_id, port_cache)


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
    ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode(), usedforsecurity=False).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode(), usedforsecurity=False).hexdigest()
    resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode(), usedforsecurity=False).hexdigest()
    return (
        f'Digest username="{user}",realm="{realm}",'
        f'nonce="{nonce}",uri="{uri}",response="{resp}"'
    )


async def pre_warm_rtsp(
    proxy_port: int, user: str, password: str, cam_host: str,
    max_attempts: int = 5, retry_wait: int = 3, post_success_wait: int = 3,
    describe_timeout: int = 5,
) -> bool:
    """Pre-warm camera's H.264 encoder via authenticated RTSP DESCRIBE.

    After PUT /connection LOCAL returns credentials, the camera needs a moment
    to initialize its encoder. Sending an authenticated DESCRIBE (codec
    negotiation) wakes the encoder so it's ready when FFmpeg connects.

    Only DESCRIBE — no SETUP/PLAY — so no RTSP session is created.
    This avoids conflicts with FFmpeg which needs to start its own session.

    Sequence: DESCRIBE (unauth) → 401 → DESCRIBE (digest) → 200 OK (SDP)

    Retries with configurable attempts and delay. Timing is model-specific:
    CAMERA_360 (indoor) is faster, CAMERA_EYES (outdoor) needs more retries.

    Returns True on success (got 200 OK to DESCRIBE), False on hard failure
    (all attempts exhausted or camera unreachable). The caller uses this to
    decide whether to fall back to REMOTE: if the camera's LAN IP isn't
    reachable from HA (firewall, wrong subnet, different VLAN), every retry
    times out and we should not pin the user on a dead LOCAL URL.
    """
    for attempt in range(1, max_attempts + 1):
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
            resp1 = await asyncio.wait_for(reader.read(4096), timeout=describe_timeout)

            # Step 2: Parse nonce, send authenticated DESCRIBE
            resp1_str = resp1.decode("utf-8", errors="replace")
            nonce_m = re.search(r'nonce="([^"]+)"', resp1_str)
            realm_m = re.search(r'realm="([^"]+)"', resp1_str)
            if not (nonce_m and realm_m):
                _LOGGER.debug(
                    "Pre-warm RTSP: no nonce/realm in response (port %d, attempt %d/%d): %.200s",
                    proxy_port, attempt, max_attempts, resp1_str,
                )
                writer.close()
                if attempt < max_attempts:
                    await asyncio.sleep(retry_wait)
                    continue
                return False
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
            resp2 = await asyncio.wait_for(reader.read(8192), timeout=describe_timeout)
            resp2_str = resp2.decode("utf-8", errors="replace")

            got_ok = "200 OK" in resp2_str
            if got_ok:
                _LOGGER.debug("Pre-warm RTSP complete (DESCRIBE 200 OK) on port %d", proxy_port)
            else:
                _LOGGER.warning(
                    "Pre-warm RTSP: unexpected response on port %d: %.200s",
                    proxy_port, resp2_str,
                )

            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            # Wait for the camera to fully release the TLS connection.
            # The camera only allows ~2 concurrent RTSP sessions per
            # PUT /connection credential set. Without this delay, FFmpeg
            # may connect before the pre-warm's TLS session is torn down.
            await asyncio.sleep(post_success_wait)
            return got_ok
        except Exception as exc:
            _LOGGER.debug(
                "Pre-warm RTSP failed on port %d (attempt %d/%d): %s",
                proxy_port, attempt, max_attempts, exc,
            )
            if attempt < max_attempts:
                await asyncio.sleep(retry_wait)
    return False
