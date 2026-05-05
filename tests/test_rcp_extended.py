"""Extended tests for rcp.py — rcp_read session cache invalidation, async_update_rcp_data parsers.

Covers the uncovered lines in rcp.py (currently 34% → target ~65%):
  - rcp_read: 401/403 drops cached session, 0x0c0d drops session, 0x90 does NOT drop
  - rcp_read: <str> and <payload> tag variants both accepted
  - rcp_read: raw binary fallback (non-XML, e.g. JPEG)
  - rcp_read: no-payload XML response → None
  - get_cached_rcp_session: hit, miss, expired
  - async_update_rcp_data: dimmer valid/out-of-range, privacy byte[1], clock valid/invalid,
    LAN IP 4-byte binary / ASCII / unusable, product name, bitrate ladder
  - _skip/_mark_fail/_mark_ok: 3-failure threshold logic
"""

from __future__ import annotations

import asyncio
import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "TEST-CAM-0001"
PROXY_HOST = "proxy-99.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abcdef1234567890"
RCP_BASE = f"https://{PROXY_HOST}/{PROXY_HASH}/rcp.xml"


# ── rcp_read: HTTP error paths ───────────────────────────────────────────────


def _make_ha_resp(status: int, raw: bytes = b"") -> MagicMock:
    """Return a MagicMock that mimics an aiohttp.ClientResponse inside async with."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=raw)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_session(resp_ctx: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=resp_ctx)
    return session


class TestRcpReadHttpErrors:
    """rcp_read maps HTTP status to return value + session-cache side effects."""

    @pytest.mark.asyncio
    async def test_http_200_payload_tag(self):
        from custom_components.bosch_shc_camera.rcp import rcp_read

        payload_hex = "deadbeef"
        xml = f"<rcp><payload>{payload_hex}</payload></rcp>".encode()
        hass = MagicMock()
        cache = {}

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(200, xml)),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache)

        assert result == bytes.fromhex(payload_hex), (
            "rcp_read must decode hex from <payload> tag and return bytes"
        )

    @pytest.mark.asyncio
    async def test_http_200_str_tag(self):
        """Bosch firmwares sometimes use <str> instead of <payload>."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        payload_hex = "0a0a"
        xml = f"<rcp><str>{payload_hex}</str></rcp>".encode()
        hass = MagicMock()

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(200, xml)),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1")

        assert result == bytes.fromhex(payload_hex), (
            "rcp_read must accept <str> tag (some Bosch FW versions use this)"
        )

    @pytest.mark.asyncio
    async def test_http_200_raw_binary_fallback(self):
        """Non-XML binary payload (e.g. JPEG thumbnail) must be returned as-is."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        raw = b"\xff\xd8\xff\xe0jpeg-data"  # starts with 0xFF (not <)
        hass = MagicMock()

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(200, raw)),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0901", "sid1")

        assert result == raw

    @pytest.mark.asyncio
    async def test_http_200_xml_no_payload_returns_none(self):
        """XML response with no <payload>/<str> and no binary data → None."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        xml = b"<rcp><status>ok</status></rcp>"
        hass = MagicMock()

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(200, xml)),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1")

        assert result is None

    @pytest.mark.asyncio
    async def test_http_401_returns_none_and_drops_cache(self):
        """401 on RCP read means the session ID is dead — must drop it from cache."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        hass = MagicMock()
        cache = {PROXY_HASH: ("old-session-id", time.monotonic() + 300)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(401, b"")),
        ):
            result = await rcp_read(
                hass, RCP_BASE, "0x0c22", "sid1",
                session_cache=cache,
            )

        assert result is None
        assert PROXY_HASH not in cache, (
            "rcp_read must evict the session from cache on HTTP 401 — "
            "otherwise the next call replays a dead session ID"
        )

    @pytest.mark.asyncio
    async def test_http_403_drops_cache(self):
        from custom_components.bosch_shc_camera.rcp import rcp_read

        hass = MagicMock()
        cache = {PROXY_HASH: ("old-session-id", time.monotonic() + 300)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(403, b"")),
        ):
            await rcp_read(hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache)

        assert PROXY_HASH not in cache, "HTTP 403 must also evict the session cache"

    @pytest.mark.asyncio
    async def test_http_non_200_no_cache_evict_for_other_status(self):
        """HTTP 500 (server error) — cache stays intact (session may still be valid)."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        hass = MagicMock()
        cache = {PROXY_HASH: ("my-session-id", time.monotonic() + 300)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(500, b"")),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache)

        assert result is None
        assert PROXY_HASH in cache, "HTTP 500 must NOT evict the session cache"

    @pytest.mark.asyncio
    async def test_error_0x0c0d_drops_cache(self):
        """RCP error 0x0c0d = 'session closed' → must evict cache."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        xml = b"<rcp><err>0x0c0d</err></rcp>"
        hass = MagicMock()
        cache = {PROXY_HASH: ("live-session", time.monotonic() + 300)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(200, xml)),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache)

        assert result is None
        assert PROXY_HASH not in cache, (
            "Error 0x0c0d means session was closed server-side — cache must be "
            "evicted so the next call re-opens the handshake"
        )

    @pytest.mark.asyncio
    async def test_error_0x90_does_not_drop_cache(self):
        """RCP error 0x90 = 'not supported' — session still alive; cache must stay."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        xml = b"<rcp><err>0x90</err></rcp>"
        hass = MagicMock()
        cache = {PROXY_HASH: ("live-session", time.monotonic() + 300)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_make_session(_make_ha_resp(200, xml)),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache)

        assert result is None
        assert PROXY_HASH in cache, (
            "Error 0x90 means the command is unsupported, not session-expired — "
            "cache must survive so subsequent supported commands reuse the session"
        )

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        from custom_components.bosch_shc_camera.rcp import rcp_read

        hass = MagicMock()
        session = MagicMock()
        session.get = MagicMock(side_effect=asyncio.TimeoutError())

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=session,
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1")

        assert result is None


# ── get_cached_rcp_session ───────────────────────────────────────────────────


class TestGetCachedRcpSession:
    """Cache hit / miss / TTL expiry — without opening real connections."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_without_new_session(self):
        """If the cache has a live entry, no network call should be made."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache = {PROXY_HASH: ("cached-sid", time.monotonic() + 300)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new_callable=AsyncMock,
        ) as mock_open:
            result = await get_cached_rcp_session(cache, PROXY_HOST, PROXY_HASH)

        assert result == "cached-sid"
        mock_open.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_entry_opens_new_session(self):
        """An entry past its TTL must be evicted and a new handshake opened."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache = {PROXY_HASH: ("old-sid", time.monotonic() - 1)}  # already expired

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new_callable=AsyncMock,
            return_value="fresh-sid",
        ):
            result = await get_cached_rcp_session(cache, PROXY_HOST, PROXY_HASH)

        assert result == "fresh-sid"
        assert PROXY_HASH in cache
        assert cache[PROXY_HASH][0] == "fresh-sid"

    @pytest.mark.asyncio
    async def test_fresh_session_ttl_is_300s(self):
        """New sessions must be cached with exactly 300 s TTL."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache = {}
        before = time.monotonic()

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new_callable=AsyncMock,
            return_value="new-sid",
        ):
            await get_cached_rcp_session(cache, PROXY_HOST, PROXY_HASH)

        after = time.monotonic()
        _, expires_at = cache[PROXY_HASH]
        ttl = expires_at - before
        assert 295 <= ttl <= 305, (
            f"Session TTL should be ~300 s, got {ttl:.1f} s — "
            "too short causes excessive re-handshakes, too long risks stale sessions"
        )

    @pytest.mark.asyncio
    async def test_failed_session_not_cached(self):
        """If rcp_session returns None, nothing should be added to cache."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache = {}

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_cached_rcp_session(cache, PROXY_HOST, PROXY_HASH)

        assert result is None
        assert PROXY_HASH not in cache, (
            "Failed sessions must not be cached — next call must retry the handshake"
        )


# ── async_update_rcp_data: dimmer parsing ────────────────────────────────────


def _make_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    """Minimal coordinator stub for async_update_rcp_data."""
    return SimpleNamespace(
        hass=MagicMock(),
        _rcp_session_cache={},
        _rcp_dimmer_cache={},
        _rcp_privacy_cache={},
        _rcp_clock_offset_cache={},
        _rcp_lan_ip_cache={},
        _rcp_product_name_cache={},
        _rcp_bitrate_cache={},
        _rcp_alarm_catalog_cache={},
        _rcp_motion_zones_cache={},
        _rcp_motion_coords_cache={},
        _rcp_tls_cert_cache={},
        _rcp_network_services_cache={},
        _rcp_iva_catalog_cache={},
        _rcp_cmd_failures={cam_id: {}},
    )


class TestAsyncUpdateRcpDataDimmer:
    """async_update_rcp_data: LED dimmer (0x0c22) parsing."""

    @pytest.mark.asyncio
    async def test_valid_dimmer_cached(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        dimmer_bytes = struct.pack(">H", 75)  # 75% brightness

        # Session open succeeds, dimmer read succeeds, all others return None
        def _side_effect(*args, **kwargs):
            return AsyncMock(return_value=None)

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            # Return dimmer bytes for 0x0c22, None for everything else
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0c22":
                    return dimmer_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_dimmer_cache.get(CAM_ID) == 75

    @pytest.mark.asyncio
    async def test_out_of_range_dimmer_not_cached(self):
        """Gen2 returns 0x0A0A (2570) which is out of 0-100 range — must not cache."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # 2570 = 0x0A0A — what Gen2 Outdoor FW 9.40.25 returns
        out_of_range_bytes = struct.pack(">H", 2570)

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0c22":
                    return out_of_range_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_dimmer_cache, (
            "Out-of-range dimmer value (Gen2 returns 2570) must not be cached — "
            "would show 2570% brightness in the UI"
        )

    @pytest.mark.asyncio
    async def test_no_session_skips_all_reads(self):
        """If get_cached_rcp_session returns None, async_update_rcp_data must skip."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        mock_read.assert_not_called()


class TestAsyncUpdateRcpDataPrivacy:
    """async_update_rcp_data: privacy mask (0x0d00) parsing."""

    @pytest.mark.asyncio
    async def test_privacy_on_byte1_eq_1(self):
        """byte[1] == 1 → privacy ON."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        privacy_bytes = bytes([0x00, 0x01, 0x00, 0x00])  # byte[1] = 1 → ON

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0d00":
                    return privacy_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_privacy_cache.get(CAM_ID) == 1, (
            "Privacy ON state must cache byte[1] == 1"
        )

    @pytest.mark.asyncio
    async def test_privacy_off_byte1_eq_0(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        privacy_bytes = bytes([0x00, 0x00, 0x00, 0x00])

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0d00":
                    return privacy_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_privacy_cache.get(CAM_ID) == 0


class TestAsyncUpdateRcpDataLanIp:
    """async_update_rcp_data: LAN IP (0x0a36) — 4-byte binary and ASCII formats."""

    @pytest.mark.asyncio
    async def test_4_byte_binary_ip(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        ip_bytes = bytes([10, 0, 0, 5])  # 10.0.0.5

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0a36":
                    return ip_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_lan_ip_cache.get(CAM_ID) == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_ascii_ip_string(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        ip_bytes = b"192.0.2.100\x00"  # null-terminated ASCII

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0a36":
                    return ip_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_lan_ip_cache.get(CAM_ID) == "192.0.2.100"

    @pytest.mark.asyncio
    async def test_xml_wrapped_payload_not_cached(self):
        """Gen2 sometimes wraps the IP in a nested XML doc — must not pollute cache."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"<rcp><payload>00000000</payload></rcp>"  # starts with <

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0a36":
                    return xml_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_lan_ip_cache, (
            "XML-wrapped LAN IP payload must not be cached — it would store "
            "the XML fragment as the IP address"
        )


class TestAsyncUpdateRcpDataBitrate:
    """async_update_rcp_data: bitrate ladder (0x0c81) parsing."""

    @pytest.mark.asyncio
    async def test_bitrate_ladder_parsed(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Two bitrate entries: 1000 kbps and 2000 kbps
        bitrate_bytes = struct.pack(">II", 1000, 2000)

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0c81":
                    return bitrate_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        ladder = coord._rcp_bitrate_cache.get(CAM_ID)
        assert ladder == [1000, 2000], f"Bitrate ladder should be [1000, 2000], got {ladder}"


# ── _skip / _mark_fail threshold logic ──────────────────────────────────────


class TestSkipFailMarkLogic:
    """Pin the 3-failure threshold that suppresses persistently-unsupported commands."""

    @pytest.mark.asyncio
    async def test_command_skipped_after_3_failures(self):
        """After 3 consecutive None returns for 0x0c22, dimmer reads are skipped."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Pre-seed 3 failures for 0x0c22
        coord._rcp_cmd_failures[CAM_ID]["0x0c22"] = 3

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            mock_read.return_value = None
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)
            called_cmds = [call.args[2] for call in mock_read.call_args_list]

        assert "0x0c22" not in called_cmds, (
            "Dimmer command 0x0c22 must be skipped after 3 consecutive failures — "
            "prevents flooding logs with known-unsupported command retries"
        )

    @pytest.mark.asyncio
    async def test_command_not_skipped_at_2_failures(self):
        """2 failures — still worth retrying."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        coord._rcp_cmd_failures[CAM_ID]["0x0c22"] = 2

        with patch(
            "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
            new_callable=AsyncMock,
            return_value="fake-sid",
        ), patch(
            "custom_components.bosch_shc_camera.rcp.rcp_read",
            new_callable=AsyncMock,
        ) as mock_read:
            mock_read.return_value = None
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)
            called_cmds = [call.args[2] for call in mock_read.call_args_list]

        assert "0x0c22" in called_cmds, (
            "Command must still be attempted at 2 failures — threshold is 3"
        )
