"""Cloudflare-Tunnel HLS-Buffering Workaround (HA stream view monkey-patch).

cloudflared buffers HTTP responses by default. Per its source — `shouldFlush()`
in `connection/connection.go` — it only switches to streaming mode when one of:

    (A) no Content-Length header
    (B) Transfer-Encoding contains "chunked"
    (C) Content-Type starts with `text/event-stream` / `application/grpc`
        / `application/x-ndjson`

HA's HLS endpoints (`/api/hls/<token>/*.m3u8` and `*.m4s` segments) return
`application/vnd.apple.mpegurl` / `video/mp4` with `Content-Length` set, so
cloudflared collects each response fully at the edge before forwarding. On
cellular (high RTT) the iOS Companion App's WKWebView times out before the
buffer flushes — visible in the cloudflared add-on log as
`Incoming request ended abruptly: context canceled`. Mobile Safari is more
tolerant on the same network, which is why the same camera works in Safari
on 5G but hangs in the App. WLAN works in both because no buffering boundary
applies on the LAN-direct path.

Two-pronged fix, one per response shape:

1. **Manifests** (`*.m3u8` from `HlsMasterPlaylistView`, `HlsPlaylistView`):
   Rewrite `Content-Type` to `text/event-stream; x-actual=...` — cloudflared
   `HasPrefix`-matches Branch (C) → streams. Players dispatch HLS playlists
   by URL extension and parse them as text, so a bogus Content-Type is fine.

2. **Binary segments** (`init.mp4`, `*.m4s` from `HlsInitView`, `HlsPartView`,
   `HlsSegmentView`): The Content-Type lie does NOT work here — iOS native
   AVFoundation parses these as MP4 and rejects them when the Content-Type
   doesn't match the container. AVPlayer paints the init frame, then the
   segment GET stalls inside cloudflared's buffer for ~10 s before the
   player times out — last decoded frame stays on screen ("Standbild"
   symptom). Instead we re-emit the response as `web.StreamResponse` with
   `Transfer-Encoding: chunked` and no `Content-Length` — that triggers
   cloudflared's Branch (B) without lying about the body type. AVFoundation
   handles HTTP chunked encoding natively (it's HTTP/1.1 standard).

Why view monkey-patch instead of aiohttp middleware / on_response_prepare:
both `app.middlewares` and `app.on_response_prepare` are frozen by HA after
HTTP setup completes — appending after that point either raises
"Cannot modify frozen list" (middlewares) or silently fails. Patching the
view classes works at any time because aiohttp resolves `class_handler.handle`
via getattr at request dispatch time, so a class-level monkey-patch applied
AFTER routes are registered still wins on the next request.

Sources:
- cloudflared shouldFlush(): https://github.com/cloudflare/cloudflared/blob/master/connection/connection.go
- cloudflared#199 (SSE buffered, open since 2022): https://github.com/cloudflare/cloudflared/issues/199
- cloudflared#1095 (Tunnel buffers HTTP responses): https://github.com/cloudflare/cloudflared/issues/1095
- HA stream component HlsSegmentView: https://github.com/home-assistant/core/blob/dev/homeassistant/components/stream/hls.py
- knowledge-base/cloudflared-tunnel-hls-buffering.md (full diagnosis)
"""

from __future__ import annotations

import logging
from functools import wraps

from aiohttp import web

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_FLUSH_PREFIX = "text/event-stream; x-actual="

_PLAYLIST_VIEW_CLASSES = (
    "HlsMasterPlaylistView",
    "HlsPlaylistView",
)

_SEGMENT_VIEW_CLASSES = (
    "HlsInitView",
    "HlsPartView",
    "HlsSegmentView",
)


def _wrap_playlist_response(response):
    """Manifest path — rewrite Content-Type to bypass cloudflared buffer."""
    if response is None or not hasattr(response, "headers"):
        return response
    original = response.headers.get("Content-Type", "")
    if not original.startswith("text/event-stream"):
        response.headers["Content-Type"] = f"{_FLUSH_PREFIX}{original}"
    return response


async def _emit_segment_chunked(request, response):
    """Binary-segment path — re-emit body via chunked StreamResponse.

    aiohttp's `web.Response` always sets Content-Length when the body is
    bytes. We need Transfer-Encoding: chunked (no Content-Length) to trigger
    cloudflared's Branch (B). Solution: drop Content-Length, prepare a
    StreamResponse, write the body in one chunk, end. aiohttp emits the
    chunked framing automatically.
    """
    body = response.body
    if not body:
        return response

    new_resp = web.StreamResponse(
        status=response.status,
        reason=response.reason,
    )
    for name, value in response.headers.items():
        if name.lower() in ("content-length", "transfer-encoding"):
            continue
        new_resp.headers[name] = value
    # No Content-Length → aiohttp uses chunked transfer encoding.
    await new_resp.prepare(request)
    await new_resp.write(body)
    await new_resp.write_eof()
    return new_resp


_PATCHED = False


def _make_playlist_wrapper(orig_handle):
    @wraps(orig_handle)
    async def _wrapped(self, *args, **kwargs):
        response = await orig_handle(self, *args, **kwargs)
        return _wrap_playlist_response(response)

    _wrapped._cf_wrapped = True  # type: ignore[attr-defined]
    return _wrapped


def _make_segment_wrapper(orig_handle):
    @wraps(orig_handle)
    async def _wrapped(self, request, *args, **kwargs):
        response = await orig_handle(self, request, *args, **kwargs)
        if response is None or not isinstance(response, web.Response):
            return response
        try:
            return await _emit_segment_chunked(request, response)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("CF unbuffer segment chunked emit failed: %s", exc)
            return response

    _wrapped._cf_wrapped = True  # type: ignore[attr-defined]
    return _wrapped


def register(hass: HomeAssistant) -> None:
    global _PATCHED
    if _PATCHED:
        return
    try:
        from homeassistant.components.stream import hls as _hls

        patched_playlist = []
        for cls_name in _PLAYLIST_VIEW_CLASSES:
            cls = getattr(_hls, cls_name, None)
            if cls is None:
                continue
            orig_handle = getattr(cls, "handle", None)
            if orig_handle is None or getattr(orig_handle, "_cf_wrapped", False):
                continue
            cls.handle = _make_playlist_wrapper(orig_handle)
            patched_playlist.append(cls_name)

        patched_segment = []
        for cls_name in _SEGMENT_VIEW_CLASSES:
            cls = getattr(_hls, cls_name, None)
            if cls is None:
                continue
            orig_handle = getattr(cls, "handle", None)
            if orig_handle is None or getattr(orig_handle, "_cf_wrapped", False):
                continue
            cls.handle = _make_segment_wrapper(orig_handle)
            patched_segment.append(cls_name)

        _PATCHED = True
        _LOGGER.warning(
            "Bosch CF-tunnel HLS unbuffer patch applied — "
            "playlists [%s] get text/event-stream Content-Type, "
            "segments [%s] re-emit as chunked StreamResponse "
            "(both bypass cloudflared HTTP buffer)",
            ", ".join(patched_playlist) if patched_playlist else "none",
            ", ".join(patched_segment) if patched_segment else "none",
        )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("CF unbuffer patch failed: %s", exc)
