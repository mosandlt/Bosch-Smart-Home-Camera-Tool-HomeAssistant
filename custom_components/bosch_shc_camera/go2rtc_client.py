"""go2rtc stream (de)registration + go2rtc WebRTC-provider scheme refresh.

Phase 3 step 3 of the coordinator-rewrite split (see
docs/stream-perf-stability-refactor-plan.md). Pure structural move: the
bodies below are the former `BoschCameraCoordinator` methods
`_ensure_go2rtc_schemes_fresh`, `_register_go2rtc_stream` and
`_unregister_go2rtc_stream`, unchanged except for `self` → `coordinator`.
`BoschCameraCoordinator` keeps a thin same-named method for each that
delegates here — these functions are exercised extensively from other
coordinator-facing modules (live_connection.py, select.py,
session_renewal.py, stream_lifecycle.py) and from the test suite both as
bound methods and via `BoschCameraCoordinator._method(coord, ...)`
unbound-style calls plus direct `AsyncMock()` attribute patching — all of
which requires the method to keep existing on the class. Keeping the thin
dispatch avoids rewriting that entire call surface for a purely structural
move.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


async def _get_go2rtc_session(
    coordinator: BoschCameraCoordinator,
) -> aiohttp.ClientSession:
    """Lazily create/return the coordinator's shared go2rtc-API session.

    go2rtc is reached over plain HTTP on localhost (11984/1984) — a
    different trust domain from the Bosch-cloud TLS session in cloud_ssl.py,
    so it gets its own pooled session instead of reusing that one. Was
    previously a fresh `aiohttp.ClientSession()` per call on all three
    go2rtc call sites (go2rtc_consumer_count / register_go2rtc_stream /
    unregister_go2rtc_stream — Work Package 1,
    stream-perf-stability-refactor). Closed exactly once, in
    _async_cancel_coordinator_tasks (__init__.py), on config-entry unload /
    HA stop.

    A free function taking `coordinator` explicitly (matching the existing
    poll_statuses/poll_events/run_housekeeping/try_live_connection_inner
    pattern in this codebase) rather than a coordinator method — it uses
    getattr/setattr instead of direct attribute access so the many
    SimpleNamespace-based coordinator test doubles in tests/test_init.py
    keep working without every one of them growing a
    `_go2rtc_session`/`_go2rtc_session_lock` attribute.
    """
    existing = getattr(coordinator, "_go2rtc_session", None)
    if existing is not None and not existing.closed:
        return existing
    if getattr(coordinator, "_go2rtc_teardown_done", False):
        # _async_cancel_coordinator_tasks already ran and closed the shared
        # session for good (unload/HA-stop). A stray caller racing that
        # teardown — e.g. camera.py's stream_source() from a live frontend
        # request landing in the gap between _async_cancel_coordinator_tasks
        # and hass.config_entries.async_unload_platforms in
        # async_unload_entry — must NOT lazily mint a brand-new session here:
        # nothing will ever close it again (teardown only runs once per
        # unload/stop), so it would leak ("Unclosed client session"). Raise
        # RuntimeError instead — every one of the three go2rtc call sites
        # already catches RuntimeError and treats it like an unreachable
        # endpoint (see the (..., RuntimeError) except clauses below).
        raise RuntimeError("go2rtc session unavailable — coordinator is shutting down")
    lock = getattr(coordinator, "_go2rtc_session_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        coordinator._go2rtc_session_lock = lock
    async with lock:
        # Double-check inside the lock — another coroutine may have already
        # created it while we awaited the lock (register/unregister/
        # consumer-count can all fire concurrently across cameras).
        existing = getattr(coordinator, "_go2rtc_session", None)
        if existing is not None and not existing.closed:
            return existing
        if getattr(coordinator, "_go2rtc_teardown_done", False):
            raise RuntimeError(
                "go2rtc session unavailable — coordinator is shutting down"
            )
        session = aiohttp.ClientSession()
        coordinator._go2rtc_session = session
        return session


@asynccontextmanager
async def _go2rtc_client_session(
    coordinator: BoschCameraCoordinator, connector: aiohttp.BaseConnector | None
) -> AsyncIterator[aiohttp.ClientSession]:
    """Yield a session for one localhost go2rtc API call.

    The common case (`connector is None`, plain TCP to 127.0.0.1) reuses the
    shared, pooled `_get_go2rtc_session()` session and does NOT close it on
    exit. A Unix-socket connector can't be attached to that shared TCP
    session, so the (rare) socket path still gets its own private,
    short-lived session — closed when this context manager exits, same as
    the previous per-call `aiohttp.ClientSession(connector=...)`.
    """
    if connector is not None:
        async with aiohttp.ClientSession(connector=connector) as session:
            yield session
    else:
        yield await _get_go2rtc_session(coordinator)


async def ensure_go2rtc_schemes_fresh(coordinator: BoschCameraCoordinator) -> None:
    """Pre-emptive: re-fetch `_supported_schemes` directly on the existing
    WebRTCProvider instance(s) so the very first stream activation finds
    the right scheme set. Avoids the race where the card asks for
    capabilities before the post-stream watchdog had a chance to fire.

    Direct-refresh (private-API hack) instead of full config-entry reload,
    because reload was found to not actually populate the schemes set in
    time before camera state writes happen — the bundled go2rtc binary
    may not yet be answering `/api/schemes` when the new provider's
    `initialize()` runs during reload, so the fresh provider also caches
    an empty set. Calling `provider._rest_client.schemes.list()` directly
    on the existing instance bypasses the reload churn and pulls the
    current scheme list now that go2rtc is ready.
    """
    if not hasattr(coordinator, "_last_schemes_refresh"):
        coordinator._last_schemes_refresh = float("-inf")
    now = time.monotonic()
    if now - coordinator._last_schemes_refresh < 600:
        return
    try:
        from homeassistant.components.camera.webrtc import DATA_WEBRTC_PROVIDERS
    except ImportError:
        return
    providers = coordinator.hass.data.get(DATA_WEBRTC_PROVIDERS, set())
    if not providers:
        return
    coordinator._last_schemes_refresh = now
    refreshed = False
    for provider in providers:
        if not hasattr(provider, "_rest_client") or not hasattr(
            provider, "_supported_schemes"
        ):
            continue  # not the bundled go2rtc provider
        try:
            fresh = await provider._rest_client.schemes.list()
            if fresh:
                old_count = len(provider._supported_schemes)
                provider._supported_schemes = fresh
                refreshed = True
                _LOGGER.info(
                    "webrtc-watchdog: refreshed go2rtc provider _supported_schemes "
                    "(was %d schemes, now %d)",
                    old_count,
                    len(fresh),
                )
        except Exception as err:
            _LOGGER.debug("webrtc-watchdog: scheme-refresh failed: %s", err)
    # Push the now-fresh provider to every camera entity that has STREAM
    # in supported_features. Without this, cams that ran async_refresh_providers
    # against a stale scheme set keep `_webrtc_provider = None` cached, and
    # the next `camera/capabilities` query advertises only HLS — even though
    # the provider's schemes are now fresh. The auto-fire only triggers on
    # `supported_features & STREAM` flips, but our streams may already be up.
    if refreshed:
        from homeassistant.components.camera import CameraEntityFeature

        for cam_id_x, cam_ent in list(coordinator._camera_entities.items()):
            # Only touch cameras that already have an active session.
            # HA Core's `async_refresh_providers` calls `stream_source()`
            # on the entity, which our implementation answers with
            # `try_live_connection()` — opening a fresh LOCAL stream on
            # idle cams the user never asked to view. Bug 2026-05-20:
            # Innenbereich woke up streaming after this loop ran on a
            # Terrasse stream-open. Guard added so the watchdog stays
            # scoped to the cam that triggered it.
            if cam_id_x not in coordinator._live_connections:
                continue
            try:
                if CameraEntityFeature.STREAM in cam_ent.supported_features:
                    await cam_ent.async_refresh_providers()
                    _LOGGER.debug(
                        "webrtc-watchdog: refreshed providers on %s",
                        getattr(cam_ent, "entity_id", "?"),
                    )
            except Exception as err:
                _LOGGER.debug(
                    "webrtc-watchdog: cam refresh-providers failed for %s: %s",
                    getattr(cam_ent, "entity_id", "?"),
                    err,
                )


async def register_go2rtc_stream(
    coordinator: BoschCameraCoordinator, cam_id: str, rtsps_url: str
) -> bool:
    """Register the Bosch RTSP stream in go2rtc for WebRTC support.

    go2rtc is HA's built-in RTSP→WebRTC bridge. Once registered, HA's
    camera card can display live 30fps H.264 + AAC audio via WebRTC
    (~2s latency) or HLS (~12s latency) directly from go2rtc.

    The stream is registered under the camera entity unique_id so HA's
    stream component can find it automatically.

    go2rtc API endpoints (tried in order):
    1. Unix socket (HA 2024+): /config/go2rtc.sock or /homeassistant/go2rtc.sock
    2. Port 11984 (HA 2024+ internal)
    3. Port 1984 (legacy / standalone go2rtc)
    """
    # HA's bundled go2rtc provider (homeassistant/components/go2rtc/__init__.py
    # line ~380) registers streams lazily under `camera.entity_id` when a
    # WebRTC offer or snapshot request arrives. To have our pre-registration
    # actually benefit HA's WebRTC / snapshot paths, we must use the same
    # name — otherwise we create a parallel stream go2rtc knows about but
    # HA never looks at. Falls back to the legacy internal name when the
    # camera entity hasn't been added yet (first registration race).
    cam_entity = coordinator._camera_entities.get(cam_id)
    if cam_entity is not None and cam_entity.entity_id:
        stream_name = cam_entity.entity_id
    else:
        stream_name = f"bosch_shc_cam_{cam_id.lower()}"
    go2rtc_src = rtsps_url

    # The rtspx:// scheme skips TLS verification in go2rtc. Bosch Cloud's
    # RTSPS proxy returns a cert for *.residential.connect.boschsecurity.com
    # but serves session URLs on proxy-NN.live.cbs.boschsecurity.com hosts —
    # go2rtc's native Go RTSP client refuses the mismatch with `tls: failed
    # to verify certificate`. Without the rewrite, registration succeeds but
    # the first consumer request 500s and HA never consumes from go2rtc.
    # Default behavior since v10.3.23 (was Beta-gated v10.3.21–v10.3.22).
    # See: https://github.com/AlexxIT/go2rtc/blob/master/internal/rtsp/README.md
    if go2rtc_src.startswith("rtsps://"):
        go2rtc_src = "rtspx://" + go2rtc_src[len("rtsps://") :]

    # Try multiple go2rtc API endpoints
    endpoints = [
        "http://localhost:11984/api/streams",
        "http://localhost:1984/api/streams",
    ]
    # Also try Unix socket if available
    config_dir = coordinator.hass.config.config_dir
    sock_path: str | None = None
    for _candidate in (
        os.path.join(config_dir, "go2rtc.sock") if config_dir else None,
        "/homeassistant/go2rtc.sock",
    ):
        if _candidate and os.path.exists(_candidate):
            sock_path = _candidate
            break

    for url in endpoints:
        try:
            async with asyncio.timeout(3):
                connector = None
                if sock_path and url == endpoints[0]:
                    # Try Unix socket first
                    try:
                        connector = aiohttp.UnixConnector(path=sock_path)
                    except (OSError, RuntimeError) as err:
                        _LOGGER.debug(
                            "go2rtc Unix socket connector unavailable: %s", err
                        )
                async with _go2rtc_client_session(coordinator, connector) as s:
                    put_url = url if not connector else "http://localhost/api/streams"
                    resp = await s.put(
                        put_url,
                        params={"src": go2rtc_src, "name": stream_name},
                    )
                    body = await resp.text()
                    # go2rtc bundled with HA writes the stream to its in-memory
                    # registry via URL query params, THEN tries to persist to
                    # /config/go2rtc.yaml. The YAML-persist step fails on HA
                    # (minimal go2rtc.yaml not meant for writes) and returns
                    # HTTP 400 with body `yaml: ... did not find expected key`
                    # — but the in-memory stream is registered. Verified live
                    # (go2rtc 1.9.12) + documented at
                    # https://github.com/AlexxIT/go2rtc/issues/1386.
                    is_yaml_persist_warning = resp.status == 400 and body.startswith(
                        "yaml:"
                    )
                    if resp.status in (200, 201, 204) or is_yaml_persist_warning:
                        # Verify by probing /api/streams?src=<name> — returns
                        # producers/consumers JSON when registered, 404 when
                        # not. This catches any silent mis-registration.
                        verified = False
                        try:
                            async with s.get(
                                put_url, params={"src": stream_name}
                            ) as check_resp:
                                if check_resp.status == 200:
                                    verified = True
                        except (TimeoutError, aiohttp.ClientError):
                            pass
                        if verified:
                            _LOGGER.info(
                                "go2rtc stream '%s' registered via %s (HTTP %d%s)",
                                stream_name,
                                "unix socket" if connector else url,
                                resp.status,
                                ", yaml-persist warn ignored"
                                if is_yaml_persist_warning
                                else "",
                            )
                            return True  # verified-registered success
                        _LOGGER.debug(
                            "go2rtc PUT returned %d via %s but verify GET missed '%s' — trying next endpoint",
                            resp.status,
                            "unix socket" if connector else url,
                            stream_name,
                        )
                        continue
                    _LOGGER.debug(
                        "go2rtc stream '%s' → HTTP %d via %s (body: %s)",
                        stream_name,
                        resp.status,
                        "unix socket" if connector else url,
                        body[:80],
                    )
                    continue
        except (TimeoutError, aiohttp.ClientError, OSError, RuntimeError):
            # RuntimeError: the shared go2rtc session can be mid-close/
            # closed if this call raced coordinator teardown — treat
            # exactly like an unreachable endpoint, try the next one.
            continue

    _LOGGER.debug("go2rtc API not reachable on any endpoint — using TLS proxy + HLS")
    return False


async def unregister_go2rtc_stream(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Remove the camera stream from go2rtc when the live session ends.

    Name must match register_go2rtc_stream — prefer camera.entity_id
    (HA's bundled go2rtc provider uses this) and fall back to the legacy
    internal name when the entity is unavailable.
    """
    cam_entity = coordinator._camera_entities.get(cam_id)
    if cam_entity is not None and cam_entity.entity_id:
        stream_name = cam_entity.entity_id
    else:
        stream_name = f"bosch_shc_cam_{cam_id.lower()}"
    # Try same endpoints as register_go2rtc_stream — DELETE must reach the
    # port where the stream was actually registered (11984 on HA 2024+, 1984 legacy).
    endpoints = [
        "http://localhost:11984/api/streams",
        "http://localhost:1984/api/streams",
    ]
    for url in endpoints:
        try:
            async with asyncio.timeout(3):
                async with _go2rtc_client_session(coordinator, None) as s:
                    resp = await s.delete(url, params={"name": stream_name})
                    # Only a real removal (200/204) ends the loop. aiohttp
                    # does not raise on 4xx/5xx, so an unconditional break
                    # would stop on a 404 (stream registered on the OTHER
                    # port) or a 500 and never reach the endpoint where the
                    # stream actually lives — defeating the documented
                    # multi-endpoint retry and leaking a stale stream (with
                    # its dead proxy port) in go2rtc.
                    if resp.status in (200, 204):
                        _LOGGER.debug(
                            "go2rtc stream '%s' removed via %s (HTTP %d)",
                            stream_name,
                            url,
                            resp.status,
                        )
                        break
                    _LOGGER.debug(
                        "go2rtc DELETE '%s' via %s → HTTP %d — trying next endpoint",
                        stream_name,
                        url,
                        resp.status,
                    )
        except (TimeoutError, aiohttp.ClientError, RuntimeError):
            # RuntimeError: the shared go2rtc session can be mid-close/
            # closed if this call raced coordinator teardown.
            pass  # go2rtc may not be running on this port — try next
