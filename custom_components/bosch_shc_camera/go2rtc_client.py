"""go2rtc stream unregistration + go2rtc WebRTC-provider scheme refresh.

There is deliberately no `register_go2rtc_stream` here: HA-core's own
bundled go2rtc integration already auto-registers whatever
`camera.stream_source()` returns on every WebRTC offer
(`homeassistant/components/go2rtc/__init__.py`
`WebRTCProvider._update_stream_source`), so a manual `PUT /api/streams`
would duplicate Core-owned protocol logic. This is only safe because both
LOCAL (`viewing_front_door.py`) and REMOTE (`remote_viewing_front_door.py`)
publish a STABLE URL per session — go2rtc's registration is purely
additive with no removal API, so registering a URL that changed on every
credential rotation (as fast as ~15s on Gen1 LOCAL sessions) would leak a
fresh dead entry every time. `unregister_go2rtc_stream`
(`DELETE /api/streams`) is still needed — there is no native equivalent at
all (go2rtc's registration API has no removal call, confirmed by reading
`python-go2rtc-client`'s actual surface), so this remains the only way to
keep the registry tidy on a genuine session teardown.

`ensure_go2rtc_schemes_fresh` is unrelated to registration — a separate
workaround for an HA-core provider-initialization race.

`BoschCameraCoordinator` keeps a thin same-named method for each function
here that delegates to it — exercised extensively from other
coordinator-facing modules (stream_lifecycle.py) and from the test suite
both as bound methods and via `BoschCameraCoordinator._method(coord, ...)`
unbound-style calls plus direct `AsyncMock()` attribute patching — all of
which requires the method to keep existing on the class.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.helpers import aiohttp_client

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


async def _get_go2rtc_session(
    coordinator: BoschCameraCoordinator,
) -> aiohttp.ClientSession:
    """Lazily create/return the coordinator's shared go2rtc-API session.

    go2rtc is reached over plain HTTP on localhost (11984/1984) — a
    different trust domain from the Bosch-cloud TLS session in cloud_ssl.py,
    so it gets its own pooled session instead of reusing that one. Closed
    exactly once, in _async_cancel_coordinator_tasks (__init__.py), on
    config-entry unload / HA stop.

    Built via `homeassistant.helpers.aiohttp_client.async_create_clientsession`
    (`auto_cleanup=False` — this function keeps its own coordinator-scoped
    close/teardown-race handling below, HA's own auto-cleanup would be
    redundant) instead of a bare `aiohttp.ClientSession()`. This still shares HA's pooled
    connector (`homeassistant.helpers.aiohttp_client`'s per-hass
    `HomeAssistantTCPConnector`, keyed by verify_ssl/family/ssl_cipher) rather
    than opening a private one, and gains the SSRF-redirect middleware for
    free; no TLS/cert behavior changes since these are plain `http://`
    localhost calls with no certificate involved. Because the returned
    session's connector is HA-owned and shared with the rest of the
    integration (and other integrations), teardown below calls `.detach()`
    (releases this session's reference without closing the shared connector),
    never `.close()` (which would tear down HA's shared connector pool).

    A free function taking `coordinator` explicitly (matching the existing
    poll_statuses/poll_events/run_housekeeping/try_live_connection_inner
    pattern in this codebase) rather than a coordinator method — it uses
    getattr/setattr instead of direct attribute access so the many
    SimpleNamespace-based coordinator test doubles in tests/test_init.py
    keep working without every one of them growing a
    `go2rtc_session`/`go2rtc_session_lock` attribute.
    """
    existing = getattr(coordinator, "go2rtc_session", None)
    if existing is not None and not existing.closed:
        return existing
    if getattr(coordinator, "go2rtc_teardown_done", False):
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
    lock = getattr(coordinator, "go2rtc_session_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        coordinator.go2rtc_session_lock = lock
    async with lock:
        # Double-check inside the lock — another coroutine may have already
        # created it while we awaited the lock (register/unregister/
        # consumer-count can all fire concurrently across cameras).
        existing = getattr(coordinator, "go2rtc_session", None)
        if existing is not None and not existing.closed:
            return existing
        if getattr(coordinator, "go2rtc_teardown_done", False):
            raise RuntimeError(
                "go2rtc session unavailable — coordinator is shutting down"
            )
        session = aiohttp_client.async_create_clientsession(
            coordinator.hass, auto_cleanup=False
        )
        coordinator.go2rtc_session = session
        return session


@asynccontextmanager
async def _go2rtc_client_session(
    coordinator: BoschCameraCoordinator,
) -> AsyncIterator[aiohttp.ClientSession]:
    """Yield the shared, pooled session for one localhost go2rtc API call.

    Both callers (`unregister_go2rtc_stream`,
    `stream_lifecycle.go2rtc_consumer_count`) always want the shared,
    plain-TCP-to-127.0.0.1 session.
    """
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
    if not hasattr(coordinator, "last_schemes_refresh"):
        coordinator.last_schemes_refresh = float("-inf")
    now = time.monotonic()
    if now - coordinator.last_schemes_refresh < 600:
        return
    try:
        from homeassistant.components.camera.webrtc import DATA_WEBRTC_PROVIDERS
    except ImportError:
        return
    providers = coordinator.hass.data.get(DATA_WEBRTC_PROVIDERS, set())
    if not providers:
        return
    coordinator.last_schemes_refresh = now
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

        for cam_id_x, cam_ent in list(coordinator.camera_entities.items()):
            # Only touch cameras that already have an active session.
            # HA Core's `async_refresh_providers` calls `stream_source()`
            # on the entity, which our implementation answers with
            # `try_live_connection()` — opening a fresh LOCAL stream on
            # idle cams the user never asked to view. Guard scopes the
            # watchdog to the cam that triggered it.
            if cam_id_x not in coordinator.live_connections:
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


async def unregister_go2rtc_stream(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Remove the camera stream from go2rtc when the live session ends.

    Name must match register_go2rtc_stream — prefer camera.entity_id
    (HA's bundled go2rtc provider uses this) and fall back to the legacy
    internal name when the entity is unavailable.
    """
    cam_entity = coordinator.camera_entities.get(cam_id)
    if cam_entity is not None and cam_entity.entity_id:
        stream_name = cam_entity.entity_id
    else:
        stream_name = f"bosch_shc_cam_{cam_id.lower()}"
    # Try both ports the stream could have been registered on (11984 on HA
    # 2024+, 1984 legacy) — DELETE must reach whichever one HA-core's own
    # go2rtc provider actually used to auto-register it.
    endpoints = [
        "http://localhost:11984/api/streams",
        "http://localhost:1984/api/streams",
    ]
    for url in endpoints:
        try:
            async with asyncio.timeout(3):
                async with _go2rtc_client_session(coordinator) as s:
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
