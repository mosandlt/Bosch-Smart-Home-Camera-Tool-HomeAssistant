"""Final coverage push — cheap remaining gaps in local_rcp / smb / rcp.

Targets:
- local_rcp.py L120-121: Cloud-Proxy returns non-200 → debug log + None
- smb.py L88-89: `_http_get_chunked` builds the Bearer-auth Request +
  urlopens it (used by media-source download path)
- rcp.py L493-494: `_mark_fail` when LED-dimmer raw is out-of-range (>100)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── local_rcp.rcp_read_remote_sync — non-200 branch ──────────────────────
class TestRcpReadRemoteSyncNon200:
    def test_returns_none_when_proxy_returns_500(self):
        """Cloud-Proxy `/rcp.xml` returns HTTP 500 → debug log + None,
        no XML parse attempted. Pins local_rcp.py L120-121."""
        from custom_components.bosch_shc_camera import local_rcp

        fake_resp = MagicMock()
        fake_resp.status = 500
        fake_resp.read = MagicMock(return_value=b"")
        fake_resp.__enter__ = MagicMock(return_value=fake_resp)
        fake_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=fake_resp):
            result = local_rcp.rcp_read_remote_sync(
                "proxy.example/abc123", "0x0d00", "P_OCTET",
            )
        assert result is None


# ── smb._http_get_chunked — Request building + urlopen ───────────────────
class TestHttpGetChunked:
    def test_builds_bearer_request_and_urlopens(self):
        """`_http_get_chunked` must build a Request with `Authorization:
        Bearer <token>` and pass the SSL context + timeout to urlopen.
        Pins smb.py L88-89."""
        from custom_components.bosch_shc_camera import smb

        captured_req = {}
        sentinel = object()

        def _fake_urlopen(req, context=None, timeout=None, **_kw):
            captured_req["req"] = req
            captured_req["context"] = context
            captured_req["timeout"] = timeout
            return sentinel

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = smb._http_get_chunked(
                "https://example/clip.mp4", "TOKEN42", timeout=30,
            )

        assert result is sentinel
        req = captured_req["req"]
        # Verify the request was built with the Bearer token.
        # urllib lowercases header names internally.
        assert req.get_header("Authorization") == "Bearer TOKEN42"
        assert captured_req["timeout"] == 30


# ── rcp.async_update_rcp_data — out-of-range dimmer mark_fail ────────────
class TestRcpUpdateDimmerOutOfRange:
    @pytest.mark.asyncio
    async def test_dimmer_out_of_range_marks_failure(self):
        """A Gen2 cam returning `0x0A0A` (2570) for the LED dimmer is
        out-of-spec; the helper must call `_mark_fail("0x0c22")` so the
        skip-counter advances and the command stops being polled.
        Pins rcp.py L493-494."""
        from custom_components.bosch_shc_camera import rcp
        from unittest.mock import AsyncMock

        coord = SimpleNamespace()
        coord._rcp_dimmer_cache = {}
        coord._rcp_privacy_cache = {}
        coord._rcp_clock_offset_cache = {}
        coord._rcp_lan_ip_cache = {}
        coord._rcp_product_name_cache = {}
        coord._rcp_bitrate_cache = {}
        coord._rcp_session_cache = {}
        coord._rcp_cmd_failures = {}
        coord.hass = MagicMock()

        # 0x0A0A = 2570 — out of the 0..100 dimmer range.
        bad_bytes = bytes.fromhex("0a0a")

        async def _fake_read(_hass, _base, command, _sid, *, type_=None, num=0, session_cache=None):
            if command == "0x0c22":
                return bad_bytes
            return None

        with patch.object(rcp, "get_cached_rcp_session",
                          new=AsyncMock(return_value="SID-42")), \
             patch.object(rcp, "rcp_read", new=_fake_read), \
             patch.object(rcp, "_is_xml_envelope", return_value=False):
            await rcp.async_update_rcp_data(coord, "CAM-ID", "proxy.example", "hash123")

        # Out-of-range raw → dimmer cache must NOT be populated.
        assert "CAM-ID" not in coord._rcp_dimmer_cache
        # Failure counter advanced to 1 for 0x0c22.
        assert coord._rcp_cmd_failures.get("CAM-ID", {}).get("0x0c22", 0) >= 1
