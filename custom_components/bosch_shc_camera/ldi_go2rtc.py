"""go2rtc as the single upstream reader of a local-data-interface camera.

The camera serves only a few concurrent RTSP sessions, so every consumer of a
local-only camera (live view, Mini-NVR, external recorder) reads from one
go2rtc stream instead of opening its own upstream connection. The stream is
registered through the go2rtc instance Home Assistant core itself uses: its
authenticated session and API URL come from core's go2rtc integration, the
RTSP listen port from the server's own application info. Nothing is hardcoded
and nothing falls back to another path: without go2rtc there is no stream.

The registered source carries the camera password, so it is never logged and
never stored on the coordinator (only a fingerprint is kept to detect
changes). Error text from aiohttp can echo request URLs, so failures log the
exception class only.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
from yarl import URL

from .const import DOMAIN

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

GO2RTC_DATA_KEY = "go2rtc"
STREAM_PREFIX = "ldi_"

_API_TIMEOUT = 5.0
_VERIFY_INTERVAL_SEC = 10.0
_BACKOFF_MAX_SEC = 60.0
_MAX_FAILS = 16
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_STREAMS_PATH = "/api/streams"

_URL_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+@")
_ENCODED_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*(?:://|%3A%2F%2F))"
    r"[^/@\s&]+(?:@|%40)",
    re.IGNORECASE,
)

_API_ERRORS = (TimeoutError, aiohttp.ClientError, ValueError, RuntimeError)


def redact_urls(text: str) -> str:
    """Replace the userinfo of every URL in `text` (plain or percent-encoded)."""
    text = _URL_USERINFO_RE.sub(r"\g<scheme>***@", text)
    return _ENCODED_USERINFO_RE.sub(r"\g<scheme>***@", text)


def ldi_stream_name(cam_id: str) -> str:
    """go2rtc stream name for a camera: `ldi_` + first 8 id characters."""
    short = re.sub(r"[^0-9a-z]", "", cam_id[:8].lower())
    return f"{STREAM_PREFIX}{short}"


@dataclass(frozen=True)
class Go2rtcEndpoint:
    """Authenticated access to core's go2rtc plus its RTSP restream port."""

    session: aiohttp.ClientSession
    api: URL
    rtsp_port: int

    def restream_url(self, name: str) -> str:
        """Credential-free RTSP URL under which go2rtc re-serves `name`."""
        return f"rtsp://127.0.0.1:{self.rtsp_port}/{name}"


def _listen_port(info: object) -> int | None:
    """RTSP listen port from go2rtc's application info, None when unusable."""
    rtsp = info.get("rtsp") if isinstance(info, dict) else None
    listen = rtsp.get("listen") if isinstance(rtsp, dict) else None
    if not isinstance(listen, str):
        return None
    tail = listen.rpartition(":")[2]
    if not tail.isdigit():
        return None
    port = int(tail)
    return port if 0 < port < 65536 else None


async def resolve_endpoint(
    coordinator: BoschCameraCoordinator,
) -> Go2rtcEndpoint | None:
    """Endpoint of the go2rtc instance HA core uses, None when unavailable.

    Only a loopback go2rtc qualifies: the restream URL is published to
    consumers on this host.
    """
    cfg = coordinator.hass.data.get(GO2RTC_DATA_KEY)
    url = getattr(cfg, "url", None)
    session = getattr(cfg, "session", None)
    if not isinstance(url, str) or session is None:
        return None
    api = URL(url)
    if api.host not in _LOOPBACK_HOSTS:
        return None
    try:
        async with asyncio.timeout(_API_TIMEOUT):
            async with session.get(api.with_path("/api")) as resp:
                if resp.status != 200:
                    return None
                info = await resp.json(content_type=None)
    except _API_ERRORS:
        return None
    port = _listen_port(info)
    if port is None:
        return None
    return Go2rtcEndpoint(session=session, api=api, rtsp_port=port)


async def _producers(endpoint: Go2rtcEndpoint, name: str) -> list[str] | None:
    """Producer source strings of a stream; [] when absent, None on failure."""
    try:
        async with asyncio.timeout(_API_TIMEOUT):
            async with endpoint.session.get(
                endpoint.api.with_path(_STREAMS_PATH), params={"src": name}
            ) as resp:
                if resp.status == 404:
                    return []
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except _API_ERRORS:
        return None
    if not isinstance(data, dict):
        return None
    producers = data.get("producers")
    if not isinstance(producers, list):
        return []
    return [str(p["url"]) for p in producers if isinstance(p, dict) and p.get("url")]


async def _delete(endpoint: Go2rtcEndpoint, name: str) -> bool:
    """Remove a stream. The status is ignored: go2rtc answers a yaml error
    after removing it from memory when its config file is not writable."""
    try:
        async with asyncio.timeout(_API_TIMEOUT):
            async with endpoint.session.delete(
                endpoint.api.with_path(_STREAMS_PATH), params={"name": name}
            ):
                return True
    except _API_ERRORS:
        return False


async def _put(endpoint: Go2rtcEndpoint, name: str, source: str) -> bool:
    """Add a stream. HTTP 400 with a yaml error is an in-memory success."""
    try:
        async with asyncio.timeout(_API_TIMEOUT):
            async with endpoint.session.put(
                endpoint.api.with_path(_STREAMS_PATH),
                params={"name": name, "src": source},
            ) as resp:
                if resp.status in (200, 204):
                    return True
                if resp.status != 400:
                    return False
                body: str = await resp.text()
                return body.lstrip().startswith("yaml:")
    except _API_ERRORS:
        return False


async def register_stream(
    endpoint: Go2rtcEndpoint, name: str, source: str, *, known: bool
) -> bool:
    """Make go2rtc hold exactly `source` under `name`; idempotent.

    `known` means this exact source was registered earlier in this run, so a
    single existing producer is trusted without comparing its text (go2rtc
    may mask credentials in its API output). Otherwise a producer that does
    not match is replaced rather than added to, so a changed password never
    leaves the old source behind. Success is judged by the producer state.
    """
    producers = await _producers(endpoint, name)
    if producers is None:
        return False
    if len(producers) == 1 and (known or producers[0] == source):
        return True
    if producers:
        await _delete(endpoint, name)
    if not await _put(endpoint, name, source):
        return False
    return len(await _producers(endpoint, name) or []) == 1


async def consumer_count(endpoint: Go2rtcEndpoint, name: str) -> int | None:
    """Number of readers attached to a stream; None when go2rtc is unreachable."""
    try:
        async with asyncio.timeout(_API_TIMEOUT):
            async with endpoint.session.get(
                endpoint.api.with_path(_STREAMS_PATH), params={"src": name}
            ) as resp:
                if resp.status == 404:
                    return 0
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
    except _API_ERRORS:
        return None
    consumers = data.get("consumers") if isinstance(data, dict) else None
    return len(consumers) if isinstance(consumers, list) else 0


def _state(coordinator: BoschCameraCoordinator, cam_id: str) -> dict[str, Any]:
    """Per-camera registration state, created on first use."""
    store: dict[str, dict[str, Any]] | None = getattr(
        coordinator, "ldi_go2rtc_state", None
    )
    if store is None:
        store = {}
        coordinator.ldi_go2rtc_state = store
    state = store.get(cam_id)
    if state is None:
        state = {
            "fp": None,
            "url": None,
            "verified_at": float("-inf"),
            "retry_at": float("-inf"),
            "fails": 0,
            "lock": asyncio.Lock(),
        }
        store[cam_id] = state
    return state


async def ensure_stream(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    source: str,
    *,
    force: bool = False,
) -> str | None:
    """Register `source` for the camera if needed; returns the restream URL.

    None means go2rtc is missing, unreachable or did not take the stream; the
    caller must fail closed. A fresh verification is reused for a few
    seconds, and failures back off exponentially (up to a minute) unless
    `force` (a session open the caller already bounds).
    """
    state = _state(coordinator, cam_id)
    async with state["lock"]:
        now = time.monotonic()
        fingerprint = hashlib.sha256(source.encode()).hexdigest()
        if not force:
            if now < state["retry_at"]:
                return None
            fresh = now - state["verified_at"] < _VERIFY_INTERVAL_SEC
            if fresh and state["url"] and state["fp"] == fingerprint:
                return str(state["url"])
        endpoint = await resolve_endpoint(coordinator)
        name = ldi_stream_name(cam_id)
        ok = endpoint is not None and await register_stream(
            endpoint, name, source, known=state["fp"] == fingerprint
        )
        if not ok or endpoint is None:
            state["fails"] = min(state["fails"] + 1, _MAX_FAILS)
            state["fp"] = None
            state["url"] = None
            state["retry_at"] = time.monotonic() + min(
                2.0 ** state["fails"], _BACKOFF_MAX_SEC
            )
            _LOGGER.debug("go2rtc stream for %s not available", cam_id[:8])
            return None
        state.update(
            fp=fingerprint,
            url=endpoint.restream_url(name),
            verified_at=time.monotonic(),
            retry_at=float("-inf"),
            fails=0,
        )
        return str(state["url"])


async def unregister_stream(coordinator: BoschCameraCoordinator, cam_id: str) -> None:
    """Remove the camera's stream from go2rtc and forget its state."""
    store: dict[str, dict[str, Any]] = (
        getattr(coordinator, "ldi_go2rtc_state", None) or {}
    )
    state = store.get(cam_id)
    if state is None:
        return
    # Serialize with ensure_stream: otherwise an in-flight registration could
    # finish after the pop and leave an untracked stream behind.
    async with state["lock"]:
        if store.get(cam_id) is state:
            store.pop(cam_id)
        endpoint = await resolve_endpoint(coordinator)
        if endpoint is not None:
            await _delete(endpoint, ldi_stream_name(cam_id))


async def unregister_all(coordinator: BoschCameraCoordinator) -> None:
    """Remove every stream this run registered (unload / options change)."""
    store: dict[str, dict[str, Any]] = (
        getattr(coordinator, "ldi_go2rtc_state", None) or {}
    )
    for cam_id in list(store):
        await unregister_stream(coordinator, cam_id)


def _claimed_names(coordinator: BoschCameraCoordinator) -> set[str]:
    """Stream names currently owned by any loaded entry of this integration."""
    coords: list[Any] = [coordinator]
    entries = getattr(
        getattr(coordinator.hass, "config_entries", None), "async_entries", None
    )
    if callable(entries):
        for entry in entries(DOMAIN):
            other = getattr(entry, "runtime_data", None)
            if other is not None and other is not coordinator:
                coords.append(other)
    names: set[str] = set()
    for coord in coords:
        store = getattr(coord, "ldi_go2rtc_state", None)
        if isinstance(store, dict):
            names.update(ldi_stream_name(cam_id) for cam_id in store)
    return names


async def remove_leftovers(coordinator: BoschCameraCoordinator, keep: set[str]) -> None:
    """Delete `ldi_*` streams a previous run left in a persistent go2rtc."""
    endpoint = await resolve_endpoint(coordinator)
    if endpoint is None:
        return
    try:
        async with asyncio.timeout(_API_TIMEOUT):
            async with endpoint.session.get(
                endpoint.api.with_path(_STREAMS_PATH)
            ) as resp:
                if resp.status != 200:
                    return
                data = await resp.json(content_type=None)
    except _API_ERRORS:
        return
    if not isinstance(data, dict):
        return
    for name in data:
        if (
            isinstance(name, str)
            and name.startswith(STREAM_PREFIX)
            and name not in keep
            # Re-read per stream: a session may register while we sweep, and
            # another config entry may own streams in the same go2rtc.
            and name not in _claimed_names(coordinator)
        ):
            await _delete(endpoint, name)
