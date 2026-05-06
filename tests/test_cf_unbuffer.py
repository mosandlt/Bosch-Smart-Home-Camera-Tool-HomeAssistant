"""Tests for cf_unbuffer.py — Cloudflare-Tunnel HLS buffering workaround.

cf_unbuffer patches HA's HLS view classes at class level so cloudflared
stops buffering HTTP responses. Two strategies are covered:

  Playlist path  — Content-Type rewritten to text/event-stream prefix
                   (triggers cloudflared's "HasPrefix" flush branch)
  Segment path   — Body re-emitted as chunked StreamResponse, no Content-Length
                   (triggers cloudflared's Transfer-Encoding: chunked branch)

Tests verify the pure-function helpers (_wrap_playlist_response,
_emit_segment_chunked) and the idempotency guard (_PATCHED / _cf_wrapped).
No HA runtime required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── _wrap_playlist_response ───────────────────────────────────────────────────


class TestWrapPlaylistResponse:
    """Pin the Content-Type rewrite behaviour for HLS manifest responses."""

    def test_plain_content_type_gets_event_stream_prefix(self):
        """application/vnd.apple.mpegurl → text/event-stream; x-actual=…"""
        from custom_components.bosch_shc_camera.cf_unbuffer import (
            _FLUSH_PREFIX,
            _wrap_playlist_response,
        )

        resp = MagicMock()
        resp.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
        result = _wrap_playlist_response(resp)

        ct = result.headers["Content-Type"]
        assert ct.startswith(_FLUSH_PREFIX), (
            f"Expected Content-Type to start with {_FLUSH_PREFIX!r}, got {ct!r}"
        )
        assert "application/vnd.apple.mpegurl" in ct, (
            "Original Content-Type must be preserved in the x-actual suffix"
        )

    def test_already_event_stream_is_not_double_wrapped(self):
        """Idempotency — text/event-stream must not be wrapped again."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _wrap_playlist_response

        resp = MagicMock()
        resp.headers = {"Content-Type": "text/event-stream; x-actual=application/vnd.apple.mpegurl"}
        result = _wrap_playlist_response(resp)

        ct = result.headers["Content-Type"]
        assert ct.count("text/event-stream") == 1, (
            "Double-wrapping would add a second text/event-stream prefix"
        )

    def test_none_response_passes_through(self):
        """None from the original view must not crash the wrapper."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _wrap_playlist_response

        assert _wrap_playlist_response(None) is None

    def test_response_without_headers_passes_through(self):
        """Objects without headers attribute (e.g. StreamResponse) must pass through unchanged."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _wrap_playlist_response

        bare = object()
        assert _wrap_playlist_response(bare) is bare


# ── _emit_segment_chunked ─────────────────────────────────────────────────────


class TestEmitSegmentChunked:
    """Pin the Transfer-Encoding: chunked re-emit for binary HLS segments."""

    @pytest.mark.asyncio
    async def test_re_emits_body_as_stream_response(self):
        """Body bytes are written to a StreamResponse — Content-Length header removed."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _emit_segment_chunked
        from aiohttp import web

        body = b"\x00\x01\x02\x03" * 16
        resp = MagicMock()
        resp.body = body
        resp.status = 200
        resp.reason = "OK"
        resp.headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(body)),
        }

        request = MagicMock()
        stream_resp = MagicMock(spec=web.StreamResponse)
        stream_resp.prepare = AsyncMock()
        stream_resp.write = AsyncMock()
        stream_resp.write_eof = AsyncMock()
        stream_resp.headers = {}

        with patch("custom_components.bosch_shc_camera.cf_unbuffer.web") as mock_web:
            mock_web.StreamResponse.return_value = stream_resp
            result = await _emit_segment_chunked(request, resp)

        assert stream_resp.prepare.called, "StreamResponse.prepare() must be awaited"
        assert stream_resp.write.called, "Body bytes must be written to the stream"
        assert stream_resp.write_eof.called, "write_eof() must be called to finish the chunk"
        # Content-Length must NOT be in the new response headers
        assert "content-length" not in {k.lower() for k in stream_resp.headers}, (
            "Content-Length in chunked response causes cloudflared to buffer again"
        )

    @pytest.mark.asyncio
    async def test_empty_body_returns_original_response(self):
        """No body → original response returned unchanged (nothing to re-emit)."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _emit_segment_chunked

        resp = MagicMock()
        resp.body = b""
        result = await _emit_segment_chunked(MagicMock(), resp)

        assert result is resp, "Empty-body response must be passed through unchanged"

    @pytest.mark.asyncio
    async def test_none_body_returns_original_response(self):
        """None body → original response returned unchanged."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _emit_segment_chunked

        resp = MagicMock()
        resp.body = None
        result = await _emit_segment_chunked(MagicMock(), resp)

        assert result is resp, "None-body response must be passed through unchanged"


# ── _make_playlist_wrapper / _make_segment_wrapper idempotency ────────────────


class TestWrapperIdempotency:
    """Wrapped views must carry _cf_wrapped=True so re-registration is a no-op."""

    def test_playlist_wrapper_sets_cf_wrapped_flag(self):
        """_make_playlist_wrapper sets _cf_wrapped on the returned coroutine."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _make_playlist_wrapper

        async def fake_handle(self, *a, **kw):
            return MagicMock()

        wrapped = _make_playlist_wrapper(fake_handle)
        assert getattr(wrapped, "_cf_wrapped", False) is True, (
            "_cf_wrapped=True prevents double-patching on HA restart"
        )

    def test_segment_wrapper_sets_cf_wrapped_flag(self):
        """_make_segment_wrapper sets _cf_wrapped on the returned coroutine."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _make_segment_wrapper

        async def fake_handle(self, request, *a, **kw):
            return MagicMock()

        wrapped = _make_segment_wrapper(fake_handle)
        assert getattr(wrapped, "_cf_wrapped", False) is True, (
            "_cf_wrapped=True prevents double-patching on HA restart"
        )


# ── register() idempotency ────────────────────────────────────────────────────


class TestRegisterIdempotency:
    """register() must be safe to call multiple times (HA reload / test isolation)."""

    def test_second_call_is_noop(self):
        """After the first register(), _PATCHED is True — second call returns immediately."""
        import types
        import custom_components.bosch_shc_camera.cf_unbuffer as mod

        # Reset module-level state for a clean test
        original_patched = mod._PATCHED
        mod._PATCHED = False

        # Build a fake hls module object with all required view classes
        fake_hls = types.ModuleType("homeassistant.components.stream.hls")

        for cls_name in (
            "HlsMasterPlaylistView",
            "HlsPlaylistView",
            "HlsInitView",
            "HlsPartView",
            "HlsSegmentView",
        ):
            cls = type(cls_name, (), {
                "handle": staticmethod(lambda self, *a, **kw: None)
            })
            setattr(fake_hls, cls_name, cls)

        # Patch the import at the point cf_unbuffer calls it
        stream_pkg = types.ModuleType("homeassistant.components.stream")
        stream_pkg.hls = fake_hls

        with patch.dict("sys.modules", {
            "homeassistant.components.stream": stream_pkg,
            "homeassistant.components.stream.hls": fake_hls,
        }):
            mod.register(MagicMock())
            assert mod._PATCHED is True, "register() must set _PATCHED=True"
            # Capture handle references after first call
            handle_after_first = fake_hls.HlsMasterPlaylistView.handle
            mod.register(MagicMock())
            # Handle must not have been re-wrapped
            assert fake_hls.HlsMasterPlaylistView.handle is handle_after_first, (
                "Second register() must not re-wrap an already-wrapped handler"
            )

        # Restore state to avoid side-effects on other tests
        mod._PATCHED = original_patched

    def test_register_handles_missing_hls_module(self):
        """register() must not raise when homeassistant.components.stream.hls is missing."""
        import types
        import custom_components.bosch_shc_camera.cf_unbuffer as mod

        original_patched = mod._PATCHED
        mod._PATCHED = False

        # Simulate a stream package where hls attribute doesn't exist / import fails
        stream_pkg = types.ModuleType("homeassistant.components.stream")
        # No .hls attribute — getattr inside register() will fall back to exception path

        with patch.dict("sys.modules", {
            "homeassistant.components.stream": stream_pkg,
        }), patch.dict("sys.modules", {
            "homeassistant.components.stream.hls": None,  # None = import blocked
        }):
            try:
                mod.register(MagicMock())
            except Exception as exc:
                pytest.fail(f"register() must not raise when hls is unavailable: {exc}")

        mod._PATCHED = original_patched


# ── Structural contract ───────────────────────────────────────────────────────


class TestStructuralContract:
    """Pin class-level constants so a rename/refactor surfaces here first."""

    def test_playlist_class_names_pinned(self):
        """_PLAYLIST_VIEW_CLASSES must target the two HLS manifest views."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _PLAYLIST_VIEW_CLASSES

        assert "HlsMasterPlaylistView" in _PLAYLIST_VIEW_CLASSES, (
            "Master playlist view name changed — update cf_unbuffer too"
        )
        assert "HlsPlaylistView" in _PLAYLIST_VIEW_CLASSES

    def test_segment_class_names_pinned(self):
        """_SEGMENT_VIEW_CLASSES must target all three binary-segment views."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _SEGMENT_VIEW_CLASSES

        for name in ("HlsInitView", "HlsPartView", "HlsSegmentView"):
            assert name in _SEGMENT_VIEW_CLASSES, (
                f"{name} missing from _SEGMENT_VIEW_CLASSES — chunked re-emit won't apply to it"
            )

    def test_flush_prefix_starts_with_text_event_stream(self):
        """cloudflared's HasPrefix check matches 'text/event-stream' — prefix must not change."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _FLUSH_PREFIX

        assert _FLUSH_PREFIX.startswith("text/event-stream"), (
            "_FLUSH_PREFIX must start with 'text/event-stream' for cloudflared to flush"
        )


class TestWrapperInvocation:
    """Invoke the wrapped coroutines so the inner await branches are exercised."""

    @pytest.mark.asyncio
    async def test_playlist_wrapper_invokes_orig_and_rewrites_ct(self):
        """Calling the wrapper must run orig_handle then pass result through _wrap_playlist_response."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _make_playlist_wrapper

        captured_args: list = []

        async def fake_handle(self, *args, **kwargs):
            captured_args.append((args, kwargs))
            resp = MagicMock()
            resp.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
            return resp

        wrapped = _make_playlist_wrapper(fake_handle)
        result = await wrapped(MagicMock(), "request_arg")
        assert captured_args, "orig_handle must be awaited inside the wrapper"
        ct = result.headers["Content-Type"]
        assert ct.startswith("text/event-stream")

    @pytest.mark.asyncio
    async def test_segment_wrapper_invokes_orig_and_rewrites_to_chunked(self):
        """Wrapper happy path: aiohttp.web.Response with a body → re-emitted as StreamResponse."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _make_segment_wrapper
        from aiohttp import web

        async def fake_handle(self, request, *a, **kw):
            return web.Response(body=b"\x00" * 32, headers={"Content-Type": "video/mp4"})

        wrapped = _make_segment_wrapper(fake_handle)

        request = MagicMock()
        stream_resp = MagicMock(spec=web.StreamResponse)
        stream_resp.prepare = AsyncMock()
        stream_resp.write = AsyncMock()
        stream_resp.write_eof = AsyncMock()
        stream_resp.headers = {}

        with patch("custom_components.bosch_shc_camera.cf_unbuffer.web") as mock_web:
            mock_web.StreamResponse.return_value = stream_resp
            mock_web.Response = web.Response
            await wrapped(MagicMock(), request)

        assert stream_resp.prepare.called
        assert stream_resp.write.called

    @pytest.mark.asyncio
    async def test_segment_wrapper_passes_through_non_response(self):
        """If orig_handle returns something that's not a web.Response (e.g. StreamResponse) → pass through."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _make_segment_wrapper

        sentinel = object()

        async def fake_handle(self, request, *a, **kw):
            return sentinel

        wrapped = _make_segment_wrapper(fake_handle)
        result = await wrapped(MagicMock(), MagicMock())
        assert result is sentinel

    @pytest.mark.asyncio
    async def test_segment_wrapper_passes_through_none(self):
        """orig_handle returning None → wrapper returns None (no chunked re-emit attempt)."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _make_segment_wrapper

        async def fake_handle(self, request, *a, **kw):
            return None

        wrapped = _make_segment_wrapper(fake_handle)
        assert await wrapped(MagicMock(), MagicMock()) is None

    @pytest.mark.asyncio
    async def test_segment_wrapper_swallows_emit_exception(self):
        """If chunked re-emit raises, wrapper logs at DEBUG and returns the original response."""
        from custom_components.bosch_shc_camera.cf_unbuffer import _make_segment_wrapper
        from aiohttp import web

        original = web.Response(body=b"x" * 16, headers={"Content-Type": "video/mp4"})

        async def fake_handle(self, request, *a, **kw):
            return original

        async def boom(*a, **kw):
            raise RuntimeError("simulated chunked emit failure")

        wrapped = _make_segment_wrapper(fake_handle)
        with patch("custom_components.bosch_shc_camera.cf_unbuffer._emit_segment_chunked", side_effect=boom):
            result = await wrapped(MagicMock(), MagicMock())
        assert result is original, "On emit failure the original Response must be returned unchanged"


class TestRegisterEdgeBranches:
    """Cover the inner-loop short-circuits (orig_handle is None / already wrapped) and outer-try failure."""

    def test_register_skips_views_with_no_handle_attribute(self):
        """A view class without a `handle` method must be skipped (continue branch)."""
        import types
        import custom_components.bosch_shc_camera.cf_unbuffer as mod

        original_patched = mod._PATCHED
        mod._PATCHED = False

        fake_hls = types.ModuleType("homeassistant.components.stream.hls")

        # Two playlist view classes: one without handle, one with — must not crash and must wrap the second
        class NoHandlePlaylist:
            pass

        class GoodPlaylist:
            async def handle(self, *a, **kw):
                return None

        fake_hls.HlsMasterPlaylistView = NoHandlePlaylist
        fake_hls.HlsPlaylistView = GoodPlaylist

        # Segment classes — one with no handle, one good
        class NoHandleSegment:
            pass

        class GoodSegment:
            async def handle(self, request, *a, **kw):
                return None

        fake_hls.HlsInitView = NoHandleSegment
        fake_hls.HlsPartView = GoodSegment
        fake_hls.HlsSegmentView = GoodSegment

        stream_pkg = types.ModuleType("homeassistant.components.stream")
        stream_pkg.hls = fake_hls

        with patch.dict("sys.modules", {
            "homeassistant.components.stream": stream_pkg,
            "homeassistant.components.stream.hls": fake_hls,
        }):
            mod.register(MagicMock())

        assert mod._PATCHED is True
        # GoodPlaylist.handle must be wrapped, NoHandlePlaylist untouched
        assert getattr(GoodPlaylist.handle, "_cf_wrapped", False) is True
        assert not hasattr(NoHandlePlaylist, "handle")

        mod._PATCHED = original_patched

    def test_register_skips_already_wrapped_handles(self):
        """If a handle already carries _cf_wrapped, the wrapper must not be applied a second time."""
        import types
        import custom_components.bosch_shc_camera.cf_unbuffer as mod

        original_patched = mod._PATCHED
        mod._PATCHED = False

        fake_hls = types.ModuleType("homeassistant.components.stream.hls")

        async def already_wrapped_playlist(self, *a, **kw):
            return None
        already_wrapped_playlist._cf_wrapped = True  # type: ignore[attr-defined]

        async def already_wrapped_segment(self, request, *a, **kw):
            return None
        already_wrapped_segment._cf_wrapped = True  # type: ignore[attr-defined]

        for cls_name in ("HlsMasterPlaylistView", "HlsPlaylistView"):
            setattr(fake_hls, cls_name, type(cls_name, (), {"handle": already_wrapped_playlist}))
        for cls_name in ("HlsInitView", "HlsPartView", "HlsSegmentView"):
            setattr(fake_hls, cls_name, type(cls_name, (), {"handle": already_wrapped_segment}))

        stream_pkg = types.ModuleType("homeassistant.components.stream")
        stream_pkg.hls = fake_hls

        with patch.dict("sys.modules", {
            "homeassistant.components.stream": stream_pkg,
            "homeassistant.components.stream.hls": fake_hls,
        }):
            mod.register(MagicMock())

        assert mod._PATCHED is True
        # Handle reference must be unchanged (no re-wrap)
        assert fake_hls.HlsMasterPlaylistView.handle is already_wrapped_playlist
        assert fake_hls.HlsInitView.handle is already_wrapped_segment

        mod._PATCHED = original_patched

    def test_register_swallows_outer_exception(self):
        """If the import of homeassistant.components.stream.hls raises, register() logs at WARNING and returns."""
        import custom_components.bosch_shc_camera.cf_unbuffer as mod

        original_patched = mod._PATCHED
        mod._PATCHED = False

        # Force the inner `from homeassistant.components.stream import hls` to raise
        # by monkey-patching the import inside the try-block via the builtins import hook.
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

        def boom_import(name, *a, **kw):
            if name == "homeassistant.components.stream" and a and len(a) >= 3 and "hls" in a[2]:
                raise ImportError("simulated stream.hls import failure")
            return real_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=boom_import):
            try:
                mod.register(MagicMock())
            except Exception as exc:
                pytest.fail(f"register() must swallow ImportError, got: {exc}")

        # _PATCHED must remain False because the patch never completed
        assert mod._PATCHED is False
        mod._PATCHED = original_patched
