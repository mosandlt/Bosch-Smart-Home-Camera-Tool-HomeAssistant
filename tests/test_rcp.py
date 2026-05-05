"""Tests for rcp.py — RCP protocol helpers and binary payload parsers.

rcp.py provides:
  get_cached_rcp_session    — 5-min TTL session cache with eviction on expiry
  rcp_local_read_privacy    — 0x0d00 byte[1] decode → bool
  rcp_local_write_privacy   — bool → 0x0d00 4-byte payload
  _parse_alarm_catalog      — UTF-16-BE blob → typed alarm dicts
  _parse_motion_zones       — 5 × 28B struct → zone dicts
  _parse_motion_coords      — 8B per zone, 0-10000 → 0-100% coords
  _parse_network_services   — null-separated ASCII → service list
  _parse_iva_catalog        — 65 × 6B TLV → module dicts
  _drop_cached_session      — invoked by rcp_read on 401/403/0x0c0d

All pure-function / no-network tests. Async helpers that hit aiohttp
are covered via AsyncMock stubs.
"""

from __future__ import annotations

import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── get_cached_rcp_session ────────────────────────────────────────────────────


class TestGetCachedRcpSession:
    """Pin the 5-minute TTL cache contract."""

    @pytest.mark.asyncio
    async def test_cache_miss_opens_new_session(self):
        """Empty cache → rcp_session called, result stored with TTL."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache: dict = {}
        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=AsyncMock(return_value="session-ABC"),
        ):
            result = await get_cached_rcp_session(cache, "proxy-10:42090", "hash123")

        assert result == "session-ABC", "Cache miss must return the newly opened session"
        assert "hash123" in cache, "New session must be stored in the cache"
        sid, expires = cache["hash123"]
        assert sid == "session-ABC"
        assert expires > time.monotonic(), "Expiry must be in the future"
        assert expires < time.monotonic() + 305, "TTL must be ≤ 5 minutes"

    @pytest.mark.asyncio
    async def test_cache_hit_reuses_session(self):
        """Valid unexpired entry → rcp_session NOT called."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        future_expiry = time.monotonic() + 200.0
        cache = {"hash123": ("session-CACHED", future_expiry)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=AsyncMock(return_value="session-NEW"),
        ) as mock_session:
            result = await get_cached_rcp_session(cache, "proxy-10:42090", "hash123")

        assert result == "session-CACHED", "Unexpired entry must be returned from cache"
        assert not mock_session.called, "rcp_session must NOT be called on a cache hit"

    @pytest.mark.asyncio
    async def test_expired_entry_is_evicted_and_refreshed(self):
        """Expired entry → removed, new session opened."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        past_expiry = time.monotonic() - 1.0  # already expired
        cache = {"hash123": ("session-OLD", past_expiry)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=AsyncMock(return_value="session-FRESH"),
        ):
            result = await get_cached_rcp_session(cache, "proxy-10:42090", "hash123")

        assert result == "session-FRESH", "Expired session must be replaced"
        sid, _ = cache["hash123"]
        assert sid == "session-FRESH", "Cache must be updated with the new session"

    @pytest.mark.asyncio
    async def test_failed_session_not_cached(self):
        """rcp_session returning None → cache must NOT store a None entry."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache: dict = {}
        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=AsyncMock(return_value=None),
        ):
            result = await get_cached_rcp_session(cache, "proxy-10:42090", "hash123")

        assert result is None
        assert "hash123" not in cache, "Failed session must not pollute the cache"


# ── rcp_local_read_privacy / rcp_local_write_privacy ─────────────────────────


class TestRcpLocalPrivacy:
    """Pin the 4-byte payload contract for 0x0d00 privacy read/write."""

    @pytest.mark.asyncio
    async def test_read_privacy_on_returns_true(self):
        """byte[1]=1 → privacy ON → True."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_read_privacy

        payload = b"\x00\x01\x00\x00"  # byte[1]=1
        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_local_read",
            new=AsyncMock(return_value=payload),
        ):
            result = await rcp_local_read_privacy(MagicMock(), "10.0.0.1")

        assert result is True, "byte[1]=1 must decode to privacy ON"

    @pytest.mark.asyncio
    async def test_read_privacy_off_returns_false(self):
        """byte[1]=0 → privacy OFF → False."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_read_privacy

        payload = b"\x00\x00\x00\x00"  # byte[1]=0
        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_local_read",
            new=AsyncMock(return_value=payload),
        ):
            result = await rcp_local_read_privacy(MagicMock(), "10.0.0.1")

        assert result is False, "byte[1]=0 must decode to privacy OFF"

    @pytest.mark.asyncio
    async def test_read_privacy_none_when_rcp_fails(self):
        """rcp_local_read returning None → None (camera offline)."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_read_privacy

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_local_read",
            new=AsyncMock(return_value=None),
        ):
            result = await rcp_local_read_privacy(MagicMock(), "10.0.0.1")

        assert result is None

    @pytest.mark.asyncio
    async def test_read_privacy_none_when_payload_too_short(self):
        """Payload shorter than 2 bytes → None (can't read byte[1])."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_read_privacy

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_local_read",
            new=AsyncMock(return_value=b"\x01"),  # only 1 byte
        ):
            result = await rcp_local_read_privacy(MagicMock(), "10.0.0.1")

        assert result is None

    @pytest.mark.asyncio
    async def test_write_privacy_on_sends_correct_payload(self):
        """enabled=True → payload '00010000' (byte[1]=1)."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write_privacy

        captured = {}

        async def _mock_write(hass, cam_ip, command, payload_hex, type_="P_OCTET"):
            captured["payload"] = payload_hex
            captured["command"] = command
            return True

        with patch("custom_components.bosch_shc_camera.rcp.rcp_local_write", _mock_write):
            result = await rcp_local_write_privacy(MagicMock(), "10.0.0.1", True)

        assert result is True
        assert captured["payload"] == "00010000", (
            "Privacy ON must send payload '00010000' (byte[1]=1)"
        )
        assert captured["command"] == "0x0d00"

    @pytest.mark.asyncio
    async def test_write_privacy_off_sends_correct_payload(self):
        """enabled=False → payload '00000000' (all zero)."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write_privacy

        captured = {}

        async def _mock_write(hass, cam_ip, command, payload_hex, type_="P_OCTET"):
            captured["payload"] = payload_hex
            return True

        with patch("custom_components.bosch_shc_camera.rcp.rcp_local_write", _mock_write):
            await rcp_local_write_privacy(MagicMock(), "10.0.0.1", False)

        assert captured["payload"] == "00000000", (
            "Privacy OFF must send all-zero payload"
        )


# ── _parse_alarm_catalog ──────────────────────────────────────────────────────


class TestParseAlarmCatalog:
    """Pin _parse_alarm_catalog UTF-16-BE decoder and alarm type classifier."""

    def _make_utf16be_blob(self, *names: str) -> bytes:
        """Encode names as UTF-16-BE with null separators."""
        parts = [n.encode("utf-16-be") for n in names]
        return b"\x00\x00".join(parts)

    def test_virtual_alarm_type_classified(self):
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._make_utf16be_blob("Virtual Alarm 0", "Virtual Alarm 1")
        result = _parse_alarm_catalog(raw)

        virtual = [a for a in result if a.get("type") == "virtual"]
        assert len(virtual) >= 1, "Names containing 'Virtual Alarm' must get type=virtual"

    def test_flame_alarm_classified(self):
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._make_utf16be_blob("Flame Detector")
        result = _parse_alarm_catalog(raw)
        types = {a["type"] for a in result}
        assert "flame" in types, "Alarm names containing 'flame' must get type=flame"

    def test_motion_alarm_classified(self):
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._make_utf16be_blob("Motion Detector")
        result = _parse_alarm_catalog(raw)
        types = {a["type"] for a in result}
        assert "motion" in types

    def test_empty_blob_returns_empty_list(self):
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        assert _parse_alarm_catalog(b"") == []

    def test_garbage_bytes_does_not_raise(self):
        """Arbitrary bytes must not raise — fallback to empty or partial list."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        try:
            result = _parse_alarm_catalog(b"\xff\xfe\x00\xab\xcd\xef")
            assert isinstance(result, list)
        except Exception as exc:
            pytest.fail(f"_parse_alarm_catalog must not raise on garbage input: {exc}")

    def test_result_dicts_have_required_keys(self):
        """Each result dict must have id, name, type."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._make_utf16be_blob("Virtual Alarm 0")
        result = _parse_alarm_catalog(raw)

        for alarm in result:
            assert "id" in alarm, f"Alarm dict missing 'id': {alarm}"
            assert "name" in alarm, f"Alarm dict missing 'name': {alarm}"
            assert "type" in alarm, f"Alarm dict missing 'type': {alarm}"


# ── _parse_motion_zones ───────────────────────────────────────────────────────


class TestParseMotionZones:
    """Pin _parse_motion_zones 28-byte chunk layout."""

    def test_single_zone_parsed(self):
        """28 bytes → exactly 1 zone."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        raw = bytes(28)  # all zeros — valid 1-zone payload
        result = _parse_motion_zones(raw)

        assert len(result) == 1, "28 bytes must produce exactly 1 zone"
        assert result[0]["zone_id"] == 0
        assert len(result[0]["raw_hex"]) == 56  # 28 bytes × 2 hex chars

    def test_five_zones_max(self):
        """5 × 28 = 140 bytes → 5 zones (cap at 5)."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        raw = bytes(28 * 5)
        result = _parse_motion_zones(raw)
        assert len(result) == 5, "5 × 28-byte payload must yield exactly 5 zones"

    def test_extra_bytes_beyond_5_ignored(self):
        """More than 5 × 28 bytes → still max 5 zones."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        raw = bytes(28 * 8)  # 8 "zones" in the data
        result = _parse_motion_zones(raw)
        assert len(result) == 5, "Zone count must be capped at 5"

    def test_too_short_returns_empty(self):
        """Less than 28 bytes → no zones."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        assert _parse_motion_zones(b"\x00" * 10) == []

    def test_zone_ids_are_sequential(self):
        """zone_id values must be 0-based sequential indices."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        raw = bytes(28 * 3)
        result = _parse_motion_zones(raw)
        ids = [z["zone_id"] for z in result]
        assert ids == [0, 1, 2], f"Zone IDs must be sequential, got {ids}"


# ── _parse_motion_coords ──────────────────────────────────────────────────────


class TestParseMotionCoords:
    """Pin _parse_motion_coords 0-10000 → 0-100% coordinate conversion."""

    def _make_coord_bytes(self, x1: int, y1: int, x2: int, y2: int) -> bytes:
        """Pack one zone's coordinates as big-endian uint16."""
        return struct.pack(">HHHH", x1, y1, x2, y2)

    def test_full_frame_zone_is_100_percent(self):
        """0-10000 range → 100% coverage."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        raw = self._make_coord_bytes(0, 0, 10000, 10000)
        result = _parse_motion_coords(raw)

        assert len(result) == 1
        assert result[0]["x1"] == 0.0
        assert result[0]["y1"] == 0.0
        assert result[0]["x2"] == 100.0
        assert result[0]["y2"] == 100.0

    def test_half_frame_zone(self):
        """5000 → 50%."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        raw = self._make_coord_bytes(0, 0, 5000, 5000)
        result = _parse_motion_coords(raw)

        assert result[0]["x2"] == 50.0, "5000/10000 must convert to 50.0%"
        assert result[0]["y2"] == 50.0

    def test_multiple_zones_parsed(self):
        """Two 8-byte entries → two zone dicts."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        raw = (
            self._make_coord_bytes(0, 0, 5000, 5000)
            + self._make_coord_bytes(5000, 5000, 10000, 10000)
        )
        result = _parse_motion_coords(raw)
        assert len(result) == 2

    def test_too_short_returns_empty(self):
        """Less than 8 bytes → empty list."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        assert _parse_motion_coords(b"\x00" * 4) == []

    def test_coords_rounded_to_one_decimal(self):
        """Conversion must round to 1 decimal place."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        raw = self._make_coord_bytes(0, 0, 3333, 6667)
        result = _parse_motion_coords(raw)

        # 3333/100 = 33.3 (rounded to 1dp)
        assert result[0]["x2"] == round(3333 / 100, 1)
        assert result[0]["y2"] == round(6667 / 100, 1)


# ── _parse_network_services ───────────────────────────────────────────────────


class TestParseNetworkServices:
    """Pin _parse_network_services null-separated ASCII decoder."""

    def test_single_service_name(self):
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        raw = b"RTSP\x00"
        result = _parse_network_services(raw)
        assert "RTSP" in result

    def test_multiple_services(self):
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        raw = b"RTSP\x00HTTP\x00ONVIF\x00"
        result = _parse_network_services(raw)
        assert len(result) >= 2, "Multiple null-separated names must all be returned"
        assert any("RTSP" in s for s in result)
        assert any("HTTP" in s for s in result)

    def test_empty_blob_returns_empty(self):
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        assert _parse_network_services(b"") == []

    def test_only_null_bytes_returns_empty(self):
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        assert _parse_network_services(b"\x00\x00\x00") == []

    def test_single_char_entries_filtered(self):
        """1-char entries must be skipped (len > 1 requirement)."""
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        raw = b"X\x00RTSP\x00Y\x00"
        result = _parse_network_services(raw)
        assert not any(len(s) <= 1 for s in result), "Single-char entries must be filtered"

    def test_garbage_bytes_does_not_raise(self):
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        try:
            result = _parse_network_services(b"\xff\xfe\xab\xcd\x00RTSP\x00")
            assert isinstance(result, list)
        except Exception as exc:
            pytest.fail(f"Must not raise on garbage input: {exc}")


# ── _parse_iva_catalog ────────────────────────────────────────────────────────


class TestParseIvaCatalog:
    """Pin _parse_iva_catalog 6-byte TLV entry decoder."""

    def _make_entry(self, module_id: int, version: int, flags: int) -> bytes:
        return struct.pack(">HHH", module_id, version, flags)

    def test_active_module_flag(self):
        """flags bit 0 set → active=True."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        raw = self._make_entry(module_id=1, version=2, flags=0x01)
        result = _parse_iva_catalog(raw)

        assert len(result) == 1
        assert result[0]["active"] is True
        assert result[0]["module_id"] == 1
        assert result[0]["version"] == 2

    def test_inactive_module_flag(self):
        """flags bit 0 clear → active=False."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        raw = self._make_entry(module_id=3, version=1, flags=0x00)
        result = _parse_iva_catalog(raw)

        assert result[0]["active"] is False

    def test_zero_module_id_skipped(self):
        """module_id=0 is an empty slot — must be filtered out."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        raw = self._make_entry(0, 0, 0)
        result = _parse_iva_catalog(raw)
        assert result == [], "module_id=0 must be treated as empty and skipped"

    def test_max_65_entries_cap(self):
        """More than 65 × 6 bytes → capped at 65 entries."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        # 70 entries, all with module_id=1 so none are filtered
        raw = self._make_entry(1, 1, 1) * 70
        result = _parse_iva_catalog(raw)
        assert len(result) <= 65, "IVA catalog must be capped at 65 entries"

    def test_too_short_returns_empty(self):
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        assert _parse_iva_catalog(b"\x00" * 4) == []

    def test_multiple_modules_all_returned(self):
        """Three valid entries → three dicts."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        raw = (
            self._make_entry(1, 1, 0x01)
            + self._make_entry(2, 2, 0x00)
            + self._make_entry(3, 3, 0x01)
        )
        result = _parse_iva_catalog(raw)
        assert len(result) == 3
        ids = [m["module_id"] for m in result]
        assert ids == [1, 2, 3]


# ── rcp_read session cache invalidation (_drop_cached_session) ────────────────


class TestRcpReadSessionInvalidation:
    """Pin the session cache invalidation paths inside rcp_read."""

    @pytest.mark.asyncio
    async def test_http_401_invalidates_cache(self):
        """HTTP 401 response → cached session for the proxy_hash removed."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        proxy_hash = "abc123def"
        cache = {proxy_hash: ("session-OLD", time.monotonic() + 300)}
        rcp_base = f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"

        mock_resp = MagicMock()
        mock_resp.status = 401
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await rcp_read(
                MagicMock(), rcp_base, "0x0c22", "session-OLD",
                session_cache=cache,
            )

        assert result is None
        assert proxy_hash not in cache, (
            "HTTP 401 must evict the cached session — dead sessions must not be replayed"
        )

    @pytest.mark.asyncio
    async def test_http_403_invalidates_cache(self):
        """HTTP 403 response → cached session evicted."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        proxy_hash = "abc123def"
        cache = {proxy_hash: ("session-OLD", time.monotonic() + 300)}
        rcp_base = f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"

        mock_resp = MagicMock()
        mock_resp.status = 403
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=mock_session,
        ):
            await rcp_read(
                MagicMock(), rcp_base, "0x0c22", "session-OLD",
                session_cache=cache,
            )

        assert proxy_hash not in cache, "HTTP 403 must evict the cached session"

    @pytest.mark.asyncio
    async def test_rcp_err_0x0c0d_invalidates_cache(self):
        """RCP <err>0x0c0d</err> (session closed by server) → cache evicted."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        proxy_hash = "abc123def"
        cache = {proxy_hash: ("session-OLD", time.monotonic() + 300)}
        rcp_base = f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"

        xml = b"<rcp><err>0x0c0d</err></rcp>"
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=xml)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await rcp_read(
                MagicMock(), rcp_base, "0x0c22", "session-OLD",
                session_cache=cache,
            )

        assert result is None
        assert proxy_hash not in cache, (
            "RCP err 0x0c0d (session closed) must evict the cached session"
        )

    @pytest.mark.asyncio
    async def test_other_rcp_error_does_not_invalidate_cache(self):
        """A non-0x0c0d RCP error (e.g. 0x90 = not supported) must NOT evict the cache."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        proxy_hash = "abc123def"
        cache = {proxy_hash: ("session-VALID", time.monotonic() + 300)}
        rcp_base = f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"

        xml = b"<rcp><err>0x0090</err></rcp>"
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=xml)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=mock_session,
        ):
            await rcp_read(
                MagicMock(), rcp_base, "0x0c22", "session-VALID",
                session_cache=cache,
            )

        assert proxy_hash in cache, (
            "Non-session-close errors must not evict the cache — "
            "the session is still valid, the command just isn't supported"
        )

    @pytest.mark.asyncio
    async def test_success_returns_payload_bytes(self):
        """200 + <payload> hex → bytes."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        xml = b"<rcp><payload>0102030405</payload></rcp>"
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=xml)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await rcp_read(
                MagicMock(),
                "https://proxy-10:42090/hash/rcp.xml",
                "0x0c22",
                "session-ID",
            )

        assert result == b"\x01\x02\x03\x04\x05", (
            "<payload> hex must be decoded to bytes"
        )

    @pytest.mark.asyncio
    async def test_str_tag_also_accepted(self):
        """200 + <str> hex → bytes (some FW versions use <str> instead of <payload>)."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        xml = b"<rcp><str>AABBCC</str></rcp>"
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=xml)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=mock_session,
        ):
            result = await rcp_read(
                MagicMock(),
                "https://proxy-10:42090/hash/rcp.xml",
                "0x0c22",
                "session-ID",
            )

        assert result == b"\xaa\xbb\xcc"
