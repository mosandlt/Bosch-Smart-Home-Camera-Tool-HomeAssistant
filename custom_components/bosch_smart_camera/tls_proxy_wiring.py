"""Coordinator-level wiring around the low-level TCP->TLS proxy.

The bodies below are the former `BoschCameraCoordinator` methods
`start_tls_proxy`, `on_tls_proxy_died`, `create_ssl_ctx` and
`stop_tls_proxy`, `self` -> `coordinator`. `BoschCameraCoordinator` keeps a
thin same-named method for each that delegates here — these are exercised
extensively from other coordinator-facing modules (live_connection.py,
stream_lifecycle.py, switch.py) as bound `coordinator._foo(...)` calls and
from the test suite both as bound methods and via
`BoschCameraCoordinator._method(coord, ...)` unbound-style calls plus
direct `AsyncMock()` attribute patching — all of which requires the method
to keep existing on the class. Keeping the thin dispatch avoids rewriting
that entire call surface.

Named `tls_proxy_wiring.py` to avoid colliding with the low-level TCP<->TLS
proxy server implementation (`start_tls_proxy`/`stop_tls_proxy` free
functions, no coordinator dependency), which lives in the
`bosch_shc_camera_client` PyPI library and is imported from here.

The proxy is asyncio-native (`asyncio.start_server`, no daemon threads) —
its `on_proxy_died` callback fires from a coroutine already running on the
HA event loop, so the callback below just schedules the rebuild task
directly (no thread->event-loop `call_soon_threadsafe` hop needed).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from typing import TYPE_CHECKING

from bosch_shc_camera_client.tls_proxy import start_tls_proxy, stop_tls_proxy

from .cloud_ssl import BOSCH_CLOUD_CA_PEM

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


def create_ssl_ctx(is_cloud: bool = False) -> ssl.SSLContext:
    """Create SSL context for TLS proxy (blocking — runs in executor).

    Two distinct trust models, deliberately not shared:

    - LOCAL (is_cloud=False): the camera on the LAN presents its own
      self-signed certificate with no publicly-verifiable chain. There is
      nothing a system trust store can validate here, so verification stays
      off — the same posture as any LAN device with a self-signed cert.

    - CLOUD (is_cloud=True): the peer is a fixed public-internet hostname
      (`proxy-NN.live.cbs.boschsecurity.com`). Live video, not on any public
      trust store: `cloud_ssl.py` documents (and this was independently
      re-verified live) that Bosch's cloud/video-proxy hosts are served by a
      *private* Bosch PKI (`Bosch ST Root CA` -> `Video CA 2A`), not a
      publicly trusted CA — the system default trust store rejects it with
      `CERTIFICATE_VERIFY_FAILED`. This pins the same `BOSCH_CLOUD_CA_PEM`
      `cloud_ssl.py` already pins for the Bosch REST API, with
      `VERIFY_X509_PARTIAL_CHAIN` since the pinned cert is an intermediate,
      not a root. `check_hostname` stays off because the proxy-NN hostname
      doesn't match the cert's SAN (`*.residential.connect.boschsecurity.
      com` — go2rtc's Go RTSP client independently confirms this with `tls:
      failed to verify certificate` when hostname checking is attempted).
      Requiring the Bosch-CA-anchored chain closes the actual gap: an
      attacker now needs a certificate chaining to Bosch's own private CA,
      not literally any self-signed certificate as `CERT_NONE` previously
      allowed.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    if is_cloud:
        ctx.load_verify_locations(cadata=BOSCH_CLOUD_CA_PEM)
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        ctx.verify_mode = ssl.CERT_REQUIRED
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def start_tls_proxy_wiring(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    cam_host: str,
    cam_port: int,
    is_renewal: bool = False,
    is_cloud: bool = False,
) -> int:
    """Start a local TCP→TLS proxy for a LOCAL or cloud-proxied RTSPS stream."""
    if getattr(coordinator, "tls_proxy_teardown_done", False):
        # _async_cancel_coordinator_tasks already took its stop_all_proxies
        # snapshot (unload/HA-stop) — a straggler call racing that point
        # must not start a fresh proxy nothing will ever see or close
        # again. Mirrors the go2rtc-session RuntimeError-on-teardown guard
        # in go2rtc_client.py.
        raise RuntimeError("TLS proxy unavailable — coordinator is shutting down")
    # Lazy-init SSL context in executor (blocking I/O, must not run in event loop).
    # LOCAL and CLOUD contexts are cached separately (different verify_mode,
    # see create_ssl_ctx) so a REMOTE session can never accidentally reuse a
    # LOCAL CERT_NONE context or vice versa.
    # Two cameras' first start of the same kind can race this check-then-act
    # across the await (both see None, both schedule an executor job) —
    # harmless in itself (both contexts built are equivalent), but
    # re-checking after the await makes the cache genuinely single-flight
    # instead of silently discarding one of the two built contexts.
    ctx_attr = "tls_ssl_ctx_cloud" if is_cloud else "tls_ssl_ctx_local"
    if getattr(coordinator, ctx_attr) is None:
        new_ctx = await coordinator.hass.async_add_executor_job(
            coordinator.create_ssl_ctx, is_cloud
        )
        if getattr(coordinator, ctx_attr) is None:
            setattr(coordinator, ctx_attr, new_ctx)
    ssl_ctx: ssl.SSLContext = getattr(coordinator, ctx_attr)

    # The circuit breaker fires on transient WiFi jitter; without this
    # signal the stream stays dead until the next heartbeat (up to 3600s
    # for Indoor Gen2). Runs on the event loop already (tls_proxy.py is
    # asyncio-native) — just schedule the rebuild coroutine as a tracked
    # background task.
    def _died_callback() -> None:
        if coordinator.hass.is_stopping:
            return
        t = coordinator.hass.async_create_task(coordinator.on_tls_proxy_died(cam_id))
        coordinator.bg_tasks.add(t)
        t.add_done_callback(coordinator.bg_tasks.discard)

    return await start_tls_proxy(
        ssl_ctx,
        cam_id,
        cam_host,
        cam_port,
        coordinator.tls_proxy_ports,
        coordinator.tls_proxy_servers,
        is_renewal=is_renewal,
        on_proxy_died=_died_callback,
    )


async def on_tls_proxy_died(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """Auto-rebuild the LOCAL session after the TLS proxy circuit breaker fires.

    Triggered by start_tls_proxy's on_proxy_died callback when the proxy
    closes its server socket after 5 consecutive connect failures (WiFi
    jitter, brief camera reboot, Bosch FW glitch).

    Backoff: skip if another rebuild ran within _TLS_PROXY_REBUILD_MIN_INTERVAL
    seconds — prevents a storm when the new proxy also dies immediately
    because the camera is still flapping.
    """
    _TLS_PROXY_REBUILD_MIN_INTERVAL = 30.0
    _PRE_WAIT = 5.0  # give the camera a moment to actually recover

    now = time.monotonic()
    last = coordinator.tls_proxy_rebuild_last.get(cam_id, float("-inf"))
    if (now - last) < _TLS_PROXY_REBUILD_MIN_INTERVAL:
        _LOGGER.debug(
            "TLS proxy rebuild for %s skipped — last rebuild %.0fs ago (< %.0fs)",
            cam_id[:8],
            now - last,
            _TLS_PROXY_REBUILD_MIN_INTERVAL,
        )
        return
    coordinator.tls_proxy_rebuild_last[cam_id] = now

    await asyncio.sleep(_PRE_WAIT)

    # Re-check state AFTER the wait — user may have toggled off,
    # or another flow may have already rebuilt.
    live = coordinator.live_connections.get(cam_id)
    if not live:
        _LOGGER.debug(
            "TLS proxy rebuild for %s skipped — stream no longer active",
            cam_id[:8],
        )
        return
    if live.get("_connection_type") != "LOCAL":
        _LOGGER.debug(
            "TLS proxy rebuild for %s skipped — active connection is %s, "
            "not LOCAL (another recovery flow owns it)",
            cam_id[:8],
            live.get("_connection_type"),
        )
        return

    _LOGGER.warning(
        "TLS proxy for %s died (circuit breaker) — rebuilding LOCAL session",
        cam_id[:8],
    )
    # force_reset clears stale state (live-session, warm-up flags) and stops
    # the dead proxy INSIDE the stream lock, so the teardown can't race a
    # concurrent renewal/heartbeat rebuild. The camera was demonstrably
    # unreachable for ~30 s, so the privacy toggle deserves to be reactive
    # again (warm-up reset) and a fresh PUT /connection runs end-to-end.
    try:
        result = await coordinator.try_live_connection(cam_id, force_reset=True)
        if result:
            _LOGGER.info(
                "TLS proxy rebuild for %s succeeded (%s)",
                cam_id[:8],
                result.get("_connection_type", "?"),
            )
        else:
            _LOGGER.warning(
                "TLS proxy rebuild for %s returned no result — next "
                "heartbeat/renewal will retry",
                cam_id[:8],
            )
    except Exception as exc:
        _LOGGER.warning(
            "TLS proxy rebuild for %s failed: %s — next heartbeat/renewal will retry",
            cam_id[:8],
            exc,
        )


async def stop_tls_proxy_wiring(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Stop the TLS proxy for a camera."""
    await stop_tls_proxy(
        cam_id, coordinator.tls_proxy_ports, coordinator.tls_proxy_servers
    )
