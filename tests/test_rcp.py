"""Tests for rcp.py — RCP protocol helpers and binary payload parsers.

rcp.py provides:
  get_cached_rcp_session    — 5-min TTL session cache with eviction on expiry
  rcp_session               — cloud-proxy RCP handshake (2-step session open)
  rcp_read / rcp_local_read — RCP command read via cloud proxy / direct LAN
  rcp_local_write           — RCP command write via direct LAN
  rcp_local_read_privacy    — 0x0d00 byte[1] decode → bool
  rcp_local_write_privacy   — bool → 0x0d00 4-byte payload
  rcp_local_write_front_light — Gen2 LAN-fallback front-light brightness writer
  async_update_rcp_data     — coordinator-side poll of all RCP diagnostic fields
  _parse_alarm_catalog      — UTF-16-BE blob → typed alarm dicts
  _parse_motion_zones       — 5 × 28B struct → zone dicts
  _parse_motion_coords      — 8B per zone, 0-10000 → 0-100% coords
  _parse_network_services   — null-separated ASCII → service list
  _parse_iva_catalog        — 65 × 6B TLV → module dicts
  _parse_tls_cert           — DER cert bytes → info dict (cryptography, with
                              raw_hex fallback if unavailable/unparseable)
  _is_xml_envelope          — detects cloud-proxy XML-leak responses
  _drop_cached_session      — invoked by rcp_read on 401/403/0x0c0d

All pure-function / no-network tests. Async helpers that hit aiohttp are
covered via AsyncMock stubs. Coordinator-facing tests use a minimal
SimpleNamespace stub (`_make_coord`) instead of the real coordinator class.
"""

from __future__ import annotations

import struct
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import rcp as rcp_module

MODULE = "custom_components.bosch_shc_camera.rcp"
CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_IP = "192.0.2.149"
PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"
RCP_BASE = f"https://{PROXY_HOST}/{PROXY_HASH}/rcp.xml"


def _make_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    """Minimal coordinator stub required by async_update_rcp_data."""
    coord = SimpleNamespace(
        hass=MagicMock(),
        _rcp_session_cache={},
        _rcp_session_locks={},
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
        _rcp_cmd_failures={},
    )
    coord._rcp_cmd_failures[cam_id] = {}
    return coord


def _make_ha_resp(status: int, raw: bytes = b"") -> MagicMock:
    """Return a MagicMock mimicking an aiohttp.ClientResponse inside `async with`."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=raw)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _make_session(*responses: MagicMock) -> MagicMock:
    """Return a mock aiohttp session whose `.get()` yields responses in order."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.get = MagicMock(side_effect=list(responses))
    return session


def _mock_resp(status: int, text: str = "", body: bytes = b"") -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=body or text.encode())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _mock_ha_session(status: int = 200, body: bytes = b"") -> MagicMock:
    """Return a mock HA aiohttp session yielding a single response."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.get = MagicMock(return_value=cm)
    return session


class TestGetCachedRcpSession:
    """Pin the 5-minute TTL cache contract for get_cached_rcp_session."""

    @pytest.mark.asyncio
    async def test_cache_miss_opens_new_session(self):
        """Empty cache → rcp_session called, result stored with TTL."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache: dict = {}
        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=AsyncMock(return_value="session-ABC"),
        ):
            result = await get_cached_rcp_session(
                MagicMock(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-ABC", (
            "Cache miss must return the newly opened session"
        )
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
            result = await get_cached_rcp_session(
                MagicMock(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-CACHED", "Unexpired entry must be returned from cache"
        assert not mock_session.called, "rcp_session must NOT be called on a cache hit"

    @pytest.mark.asyncio
    async def test_cache_hit_returns_without_new_session(self):
        """If the cache has a live entry, no network call should be made."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache = {PROXY_HASH: ("cached-sid", time.monotonic() + 300)}

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new_callable=AsyncMock,
        ) as mock_open:
            result = await get_cached_rcp_session(
                MagicMock(), cache, PROXY_HOST, PROXY_HASH
            )

        assert result == "cached-sid"
        mock_open.assert_not_called()

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
            result = await get_cached_rcp_session(
                MagicMock(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-FRESH", "Expired session must be replaced"
        sid, _ = cache["hash123"]
        assert sid == "session-FRESH", "Cache must be updated with the new session"

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
            result = await get_cached_rcp_session(
                MagicMock(), cache, PROXY_HOST, PROXY_HASH
            )

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
            await get_cached_rcp_session(MagicMock(), cache, PROXY_HOST, PROXY_HASH)

        after = time.monotonic()
        _, expires_at = cache[PROXY_HASH]
        ttl = expires_at - before
        assert 295 <= ttl <= 305, (
            f"Session TTL should be ~300 s, got {ttl:.1f} s — "
            "too short causes excessive re-handshakes, too long risks stale sessions"
        )

    @pytest.mark.asyncio
    async def test_failed_session_not_cached(self):
        """rcp_session returning None → cache must NOT store a None entry."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache: dict = {}
        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=AsyncMock(return_value=None),
        ):
            result = await get_cached_rcp_session(
                MagicMock(), cache, "proxy-10:42090", "hash123"
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_failed_session_not_cached_shared_constants(self):
        """Same contract as above, exercised via the shared PROXY_HOST/PROXY_HASH
        module constants instead of inline literals — kept as a distinct test
        since it pins the shared-fixture path used by the rest of this file."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache = {}

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_cached_rcp_session(
                MagicMock(), cache, PROXY_HOST, PROXY_HASH
            )

        assert result is None
        assert PROXY_HASH not in cache, (
            "Failed sessions must not be cached — next call must retry the handshake"
        )


class TestGetCachedRcpSessionConcurrency:
    """Regression: two concurrent openers for the same proxy_hash raced
    Bosch's cloud RCP proxy, which only tolerates one live session per
    proxy_hash — the loser got sessionid 0x00000000 ("proxy rejected"),
    observed live 2026-07-08 (a privacy-mode-off snapshot trigger racing
    the coordinator's own RCP data refresh for the same camera). Passing
    a shared `session_locks` dict serializes same-proxy_hash opens so the
    second caller awaits the first's in-flight open and reads the cache
    instead of firing its own handshake.
    """

    @pytest.mark.asyncio
    async def test_concurrent_callers_same_hash_open_session_once(self):
        import asyncio

        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache: dict = {}
        locks: dict = {}
        open_calls = 0

        async def _fake_rcp_session(hass, session_cache, proxy_host, proxy_hash):
            nonlocal open_calls
            open_calls += 1
            # Yield control so a real race would interleave here if unlocked.
            await asyncio.sleep(0.01)
            return "session-SHARED"

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=_fake_rcp_session,
        ):
            results = await asyncio.gather(
                get_cached_rcp_session(
                    MagicMock(), cache, "proxy-10:42090", "hash123", locks
                ),
                get_cached_rcp_session(
                    MagicMock(), cache, "proxy-10:42090", "hash123", locks
                ),
            )

        assert open_calls == 1, (
            "Second caller must await the first's in-flight open (via the "
            "shared lock) instead of firing its own concurrent handshake"
        )
        assert results == ["session-SHARED", "session-SHARED"]

    @pytest.mark.asyncio
    async def test_concurrent_callers_different_hash_not_serialized(self):
        """Locks are per-proxy_hash — different cameras must not block each other."""
        import asyncio

        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache: dict = {}
        locks: dict = {}
        open_calls = 0

        async def _fake_rcp_session(hass, session_cache, proxy_host, proxy_hash):
            nonlocal open_calls
            open_calls += 1
            await asyncio.sleep(0.01)
            return f"session-{proxy_hash}"

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=_fake_rcp_session,
        ):
            results = await asyncio.gather(
                get_cached_rcp_session(
                    MagicMock(), cache, "proxy-10:42090", "hashA", locks
                ),
                get_cached_rcp_session(
                    MagicMock(), cache, "proxy-11:42090", "hashB", locks
                ),
            )

        assert open_calls == 2, "Distinct proxy_hash values must open independently"
        assert results == ["session-hashA", "session-hashB"]

    @pytest.mark.asyncio
    async def test_no_locks_arg_preserves_prior_unlocked_behavior(self):
        """Omitting session_locks (existing call sites) must still work —
        backward compatible, no forced serialization."""
        from custom_components.bosch_shc_camera.rcp import get_cached_rcp_session

        cache: dict = {}
        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_session",
            new=AsyncMock(return_value="session-X"),
        ):
            result = await get_cached_rcp_session(
                MagicMock(), cache, "proxy-10:42090", "hash123"
            )

        assert result == "session-X"
        assert cache["hash123"][0] == "session-X"


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

        async def _mock_write(
            hass,
            cam_ip,
            command,
            payload_hex,
            type_="P_OCTET",
            num=0,
            *,
            user=None,
            password=None,
        ):
            captured["payload"] = payload_hex
            captured["command"] = command
            return True

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_local_write", _mock_write
        ):
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

        async def _mock_write(
            hass,
            cam_ip,
            command,
            payload_hex,
            type_="P_OCTET",
            num=0,
            *,
            user=None,
            password=None,
        ):
            captured["payload"] = payload_hex
            return True

        with patch(
            "custom_components.bosch_shc_camera.rcp.rcp_local_write", _mock_write
        ):
            await rcp_local_write_privacy(MagicMock(), "10.0.0.1", False)

        assert captured["payload"] == "00000000", (
            "Privacy OFF must send all-zero payload"
        )


class TestRcpReadSessionInvalidation:
    """Pin the session cache invalidation paths inside rcp_read."""

    @pytest.mark.asyncio
    async def test_http_401_invalidates_cache(self):
        """HTTP 401 response → cached session for the proxy_hash removed."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        proxy_hash = "abc123def"
        cache = {proxy_hash: ("session-OLD", time.monotonic() + 300)}
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

        mock_resp = MagicMock()
        mock_resp.status = 401
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            result = await rcp_read(
                MagicMock(),
                rcp_base,
                "0x0c22",
                "session-OLD",
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
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

        mock_resp = MagicMock()
        mock_resp.status = 403
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=ctx)

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await rcp_read(
                MagicMock(),
                rcp_base,
                "0x0c22",
                "session-OLD",
                session_cache=cache,
            )

        assert proxy_hash not in cache, "HTTP 403 must evict the cached session"

    @pytest.mark.asyncio
    async def test_rcp_err_0x0c0d_invalidates_cache(self):
        """RCP <err>0x0c0d</err> (session closed by server) → cache evicted."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        proxy_hash = "abc123def"
        cache = {proxy_hash: ("session-OLD", time.monotonic() + 300)}
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            result = await rcp_read(
                MagicMock(),
                rcp_base,
                "0x0c22",
                "session-OLD",
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
        rcp_base = (
            f"https://proxy-10.live.cbs.boschsecurity.com:42090/{proxy_hash}/rcp.xml"
        )

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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await rcp_read(
                MagicMock(),
                rcp_base,
                "0x0c22",
                "session-VALID",
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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            result = await rcp_read(
                MagicMock(),
                "https://proxy-10:42090/hash/rcp.xml",
                "0x0c22",
                "session-ID",
            )

        assert result == b"\xaa\xbb\xcc"


class TestRcpReadHttpErrors:
    """rcp_read maps HTTP status to return value + session-cache side effects.

    Uses the shared PROXY_HOST/PROXY_HASH/RCP_BASE module constants and the
    shared `_make_ha_resp`/`_make_session` helpers — distinct scenarios from
    TestRcpReadSessionInvalidation above (which uses inline per-test literals),
    kept as a separate class to avoid churn while merging.
    """

    @pytest.mark.asyncio
    async def test_http_200_payload_tag(self):
        from custom_components.bosch_shc_camera.rcp import rcp_read

        payload_hex = "deadbeef"
        xml = f"<rcp><payload>{payload_hex}</payload></rcp>".encode()
        hass = MagicMock()
        cache = {}

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(200, xml))),
        ):
            result = await rcp_read(
                hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache
            )

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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(200, xml))),
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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(200, raw))),
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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(200, xml))),
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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(401, b""))),
        ):
            result = await rcp_read(
                hass,
                RCP_BASE,
                "0x0c22",
                "sid1",
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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(403, b""))),
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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(500, b""))),
        ):
            result = await rcp_read(
                hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache
            )

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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(200, xml))),
        ):
            result = await rcp_read(
                hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache
            )

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
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_make_session(_make_ha_resp(200, xml))),
        ):
            result = await rcp_read(
                hass, RCP_BASE, "0x0c22", "sid1", session_cache=cache
            )

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
        session.get = MagicMock(side_effect=TimeoutError())

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await rcp_read(hass, RCP_BASE, "0x0c22", "sid1")

        assert result is None


class TestRcpReadNumParam:
    """rcp_read: when num != 0, 'num' is included in the request params."""

    @pytest.mark.asyncio
    async def test_num_param_included_when_nonzero(self):
        """rcp_read with num=1 → params dict contains 'num': '1'."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        captured_params = {}

        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"<rcp><payload>0102</payload></rcp>")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)

        def fake_get(url, params=None, **kwargs):
            captured_params.update(params or {})
            return cm

        mock_session = MagicMock()
        mock_session.get = fake_get

        hass = MagicMock()

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            result = await rcp_read(
                hass,
                "https://proxy/hash/rcp.xml",
                "0x0c22",
                "sess123",
                type_="T_WORD",
                num=1,
            )

        assert captured_params.get("num") == "1"
        assert result == bytes.fromhex("0102")

    @pytest.mark.asyncio
    async def test_num_param_absent_when_zero(self):
        """rcp_read with num=0 (default) → params dict does NOT contain 'num'."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        captured_params = {}

        resp = MagicMock()
        resp.status = 200
        resp.read = AsyncMock(return_value=b"<rcp><payload>aabb</payload></rcp>")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)

        def fake_get(url, params=None, **kwargs):
            captured_params.update(params or {})
            return cm

        mock_session = MagicMock()
        mock_session.get = fake_get
        hass = MagicMock()

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await rcp_read(hass, "https://proxy/hash/rcp.xml", "0x0d00", "sess123")

        assert "num" not in captured_params


class TestRcpReadDropSessionNone:
    """rcp_read: _drop_cached_session with session_cache=None is a safe no-op."""

    @pytest.mark.asyncio
    async def test_401_with_none_cache_does_not_crash(self):
        """HTTP 401 + session_cache=None → returns None without any AttributeError."""
        from custom_components.bosch_shc_camera.rcp import rcp_read

        resp = MagicMock()
        resp.status = 401
        resp.read = AsyncMock(return_value=b"")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=cm)
        hass = MagicMock()

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            result = await rcp_read(
                hass,
                "https://proxy/hash/rcp.xml",
                "0x0d00",
                "sess123",
                session_cache=None,
            )

        assert result is None


class TestRcpSession:
    """All branches of rcp_session (2-step cloud proxy session open)."""

    @pytest.mark.asyncio
    async def test_success_returns_session_id(self):
        """Happy path: step1 returns <sessionid>, step2 ACKs → returns session_id."""
        from custom_components.bosch_shc_camera.rcp import rcp_session

        step1 = _mock_resp(200, text="<sessionid>0x12345678</sessionid>")
        step2 = _mock_resp(200, text="<result>OK</result>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1, step2)
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=False),
            ),
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(MagicMock(), {}, PROXY_HOST, PROXY_HASH)
        assert result == "0x12345678"

    @pytest.mark.asyncio
    async def test_step1_non200_returns_none(self):
        """HTTP 403 on step1 → returns None."""
        from custom_components.bosch_shc_camera.rcp import rcp_session

        step1 = _mock_resp(403)
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1)
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=False),
            ),
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(MagicMock(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step1_timeout_returns_none(self):
        from custom_components.bosch_shc_camera.rcp import rcp_session

        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get.return_value = cm
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=False),
            ),
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(MagicMock(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step1_client_error_returns_none(self):
        import aiohttp

        from custom_components.bosch_shc_camera.rcp import rcp_session

        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn refused"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get.return_value = cm
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=False),
            ),
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(MagicMock(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_sessionid_in_response_returns_none(self):
        """Step1 200 but no <sessionid> in body → returns None."""
        from custom_components.bosch_shc_camera.rcp import rcp_session

        step1 = _mock_resp(200, text="<result>ok</result>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1)
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=False),
            ),
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(MagicMock(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_session_id_0x00000000_returns_none(self):
        """Proxy rejection indicated by sessionid=0x00000000 → returns None."""
        from custom_components.bosch_shc_camera.rcp import rcp_session

        step1 = _mock_resp(200, text="<sessionid>0x00000000</sessionid>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1)
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=False),
            ),
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(MagicMock(), {}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step2_timeout_still_returns_session_id(self):
        """ACK (step2) timeout is non-fatal — session_id already extracted, return it."""
        from custom_components.bosch_shc_camera.rcp import rcp_session

        step1 = _mock_resp(200, text="<sessionid>0xABCDEF01</sessionid>")
        step2_cm = MagicMock()
        step2_cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        step2_cm.__aexit__ = AsyncMock(return_value=None)
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1, step2_cm)
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=False),
            ),
            patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock),
            patch(f"{MODULE}.aiohttp.ClientSession", return_value=session),
        ):
            result = await rcp_session(MagicMock(), {}, PROXY_HOST, PROXY_HASH)
        # step2 timeout is caught — should still return the session_id
        assert result == "0xABCDEF01"


class TestRcpLocalRead:
    """All branches of rcp_local_read (direct LAN RCP GET)."""

    def _mock_hass_session(self, response_cm):
        fake_hass = MagicMock()
        session = MagicMock()
        session.get.return_value = response_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            return fake_hass, session

    @pytest.mark.asyncio
    async def test_non200_returns_none(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        resp_cm = _mock_resp(401)
        session = MagicMock()
        session.get.return_value = resp_cm
        fake_hass = MagicMock()
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_read(fake_hass, CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_200_with_payload_tag_returns_bytes(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        raw = b"<payload>deadbeef</payload>"
        resp_cm = _mock_resp(200, body=raw)
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_read(MagicMock(), CAM_IP, "0x0c22")
        assert result == bytes.fromhex("deadbeef")

    @pytest.mark.asyncio
    async def test_200_with_str_tag_returns_bytes(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        raw = b"<str>cafebabe</str>"
        resp_cm = _mock_resp(200, body=raw)
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_read(MagicMock(), CAM_IP, "0x0c22")
        assert result == bytes.fromhex("cafebabe")

    @pytest.mark.asyncio
    async def test_200_with_err_tag_returns_none(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        raw = b"<err>0x01</err>"
        resp_cm = _mock_resp(200, body=raw)
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_read(MagicMock(), CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_200_raw_binary_fallback(self):
        """No <str>/<payload>/<err> tag and raw doesn't start with '<' → return raw bytes."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        raw = b"\x01\x02\x03\x04"
        resp_cm = _mock_resp(200, body=raw)
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_read(MagicMock(), CAM_IP, "0x0c22")
        assert result == raw

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get.return_value = cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_read(MagicMock(), CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self):
        import aiohttp

        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn error"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get.return_value = cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_read(MagicMock(), CAM_IP, "0x0c22")
        assert result is None

    @pytest.mark.asyncio
    async def test_num_param_included(self):
        """When `num` > 0, params should include the 'num' key."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_read

        raw = b"\x01\x02"
        resp_cm = _mock_resp(200, body=raw)
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await rcp_local_read(MagicMock(), CAM_IP, "0x0c22", num=3)
        _, call_kwargs = session.get.call_args
        assert "num" in call_kwargs.get("params", {})
        assert call_kwargs["params"]["num"] == "3"


class TestRcpLocalWrite:
    """All branches of rcp_local_write (direct LAN RCP WRITE)."""

    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        resp_cm = _mock_resp(200, body=b"<result>OK</result>")
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_write(MagicMock(), CAM_IP, "0x0c22", "deadbeef")
        assert result is True

    @pytest.mark.asyncio
    async def test_0x_prefix_preserved(self):
        """Payloads without '0x' prefix get it added.

        Params are embedded in the URL query string (not the `params=` kwarg).
        """
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        resp_cm = _mock_resp(200, body=b"ok")
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await rcp_local_write(MagicMock(), CAM_IP, "0x0c22", "deadbeef")
        # First positional arg is the URL; payload="0xdeadbeef" lives in the query.
        call_args, _ = session.get.call_args
        url = call_args[0] if call_args else ""
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(url).query)
        assert qs.get("payload", [""])[0].startswith("0x")

    @pytest.mark.asyncio
    async def test_non200_returns_false(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        resp_cm = _mock_resp(403)
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_write(MagicMock(), CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_err_in_body_returns_false(self):
        """200 response with <err> tag → returns False."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        resp_cm = _mock_resp(200, body=b"<err>0x01</err>")
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_write(MagicMock(), CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get.return_value = cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_write(MagicMock(), CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False

    @pytest.mark.asyncio
    async def test_client_error_returns_false(self):
        import aiohttp

        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("no conn"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get.return_value = cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            result = await rcp_local_write(MagicMock(), CAM_IP, "0x0c22", "0xdeadbeef")
        assert result is False


class _FakeRcpResp:
    def __init__(self, status: int = 200, body: bytes = b"<ok/>"):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeRcpSession:
    """Captures the URL query so the test can assert num=1 was sent.

    `rcp_local_write` embeds params directly into the URL (HTTPS path)
    instead of passing them via `params=`. This fake parses the query
    string back into `last_params` so assertions keep working.
    """

    def __init__(self, resp: _FakeRcpResp):
        self._resp = resp
        self.last_params: dict | None = None
        self.last_url: str | None = None

    def get(self, url, **_kwargs):
        from urllib.parse import parse_qs, urlparse

        self.last_url = url
        parsed = urlparse(url)
        self.last_params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return self._resp


@pytest.mark.asyncio
class TestRcpLocalWriteFrontLight:
    """Coverage for the RCP front-light LOCAL writer (Gen2 LAN-fallback).

    `rcp_local_write_front_light` is the cloud-bypass path used during Bosch
    outages. It clamps brightness 0..100, encodes as 4-hex T_WORD and writes
    to RCP `0x0c22` with `num=1`. Other fallback tests mock it entirely, so
    this exercises the encoder + the `params["num"]` plumbing in
    `rcp_local_write` itself.
    """

    async def test_brightness_100_sends_t_word_with_num_1(self):
        """Pins `params["num"] = str(num)` + the front-light encoder."""
        resp = _FakeRcpResp(status=200, body=b"<ok/>")
        session = _FakeRcpSession(resp)
        with patch.object(
            rcp_module,
            "async_get_clientsession",
            return_value=session,
        ):
            ok = await rcp_module.rcp_local_write_front_light(
                MagicMock(), "1.2.3.4", 100
            )
        assert ok is True
        assert session.last_params is not None
        assert session.last_params["command"] == "0x0c22"
        assert session.last_params["type"] == "T_WORD"
        # 100 → 0x0064, sent as 0x0064 (lower-case hex, 4 digits).
        assert session.last_params["payload"].lower() == "0x0064"
        # num=1 plumbed through — only fires when num > 0.
        assert session.last_params["num"] == "1"

    async def test_brightness_clamped_to_range(self):
        """Out-of-range brightness clamps to 0..100. 250 → 100, -10 → 0."""
        resp = _FakeRcpResp(status=200, body=b"<ok/>")
        with patch.object(
            rcp_module, "async_get_clientsession", return_value=_FakeRcpSession(resp)
        ):
            assert (
                await rcp_module.rcp_local_write_front_light(
                    MagicMock(), "1.2.3.4", 250
                )
                is True
            )
            assert (
                await rcp_module.rcp_local_write_front_light(
                    MagicMock(), "1.2.3.4", -10
                )
                is True
            )

    async def test_brightness_zero_encodes_0x0000(self):
        resp = _FakeRcpResp(status=200, body=b"<ok/>")
        session = _FakeRcpSession(resp)
        with patch.object(rcp_module, "async_get_clientsession", return_value=session):
            ok = await rcp_module.rcp_local_write_front_light(MagicMock(), "1.2.3.4", 0)
        assert ok is True
        assert session.last_params["payload"].lower() == "0x0000"

    async def test_returns_false_on_http_non_200(self):
        """Camera responding with HTTP 500 → caller must see False so the
        SHC-cloud retry path runs."""
        resp = _FakeRcpResp(status=500, body=b"")
        with patch.object(
            rcp_module, "async_get_clientsession", return_value=_FakeRcpSession(resp)
        ):
            ok = await rcp_module.rcp_local_write_front_light(
                MagicMock(), "1.2.3.4", 50
            )
        assert ok is False

    async def test_returns_false_on_rcp_err_envelope(self):
        """`<err>` in response body → write failed even if HTTP 200."""
        resp = _FakeRcpResp(status=200, body=b"<rcp><err>5</err></rcp>")
        with patch.object(
            rcp_module, "async_get_clientsession", return_value=_FakeRcpSession(resp)
        ):
            ok = await rcp_module.rcp_local_write_front_light(
                MagicMock(), "1.2.3.4", 50
            )
        assert ok is False


class TestIsXmlEnvelopeHelper:
    """_is_xml_envelope: shared detection of cloud-proxy XML-leak responses.

    Gen2 cloud proxy occasionally returns the outer RCP XML envelope as the
    P_OCTET payload bytes (Bosch-side limitation). The envelope starts with
    whitespace + '<rcp>...'. Short responses (T_WORD = 2 bytes) may contain
    only the leading whitespace and never reach '<' — those are XML too.
    """

    def test_none_is_not_xml(self):
        from custom_components.bosch_shc_camera.rcp import _is_xml_envelope

        assert _is_xml_envelope(None) is False

    def test_empty_is_not_xml(self):
        from custom_components.bosch_shc_camera.rcp import _is_xml_envelope

        assert _is_xml_envelope(b"") is False

    def test_plain_xml_detected(self):
        from custom_components.bosch_shc_camera.rcp import _is_xml_envelope

        assert _is_xml_envelope(b"<rcp><command>0x0c81</command></rcp>") is True

    def test_whitespace_prefixed_xml_detected(self):
        from custom_components.bosch_shc_camera.rcp import _is_xml_envelope

        assert (
            _is_xml_envelope(b"\n\n<rcp>\n\n\t<command>0x0c81</command></rcp>") is True
        )

    def test_pure_whitespace_detected(self):
        """T_WORD (2 bytes) truncates the XML envelope to just '\\n\\n'."""
        from custom_components.bosch_shc_camera.rcp import _is_xml_envelope

        assert _is_xml_envelope(b"\n\n") is True
        assert _is_xml_envelope(b"\t ") is True
        assert _is_xml_envelope(b"\r\n") is True

    def test_binary_payload_not_xml(self):
        # Valid bitrate ladder uint32 big-endian: 1000, 2000 kbps
        import struct as _struct

        from custom_components.bosch_shc_camera.rcp import _is_xml_envelope

        assert _is_xml_envelope(_struct.pack(">II", 1000, 2000)) is False
        # Valid LED dimmer 50% as T_WORD
        assert _is_xml_envelope(b"\x00\x32") is False
        # Single byte of zero
        assert _is_xml_envelope(b"\x00") is False

    def test_ascii_text_not_xml(self):
        from custom_components.bosch_shc_camera.rcp import _is_xml_envelope

        # Product name (legitimate ASCII), not XML
        assert _is_xml_envelope(b"Bosch Smart Camera\x00") is False


class TestAsyncUpdateRcpDataDimmer:
    """async_update_rcp_data: LED dimmer (0x0c22) parsing."""

    @pytest.mark.asyncio
    async def test_valid_dimmer_cached(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        dimmer_bytes = struct.pack(">H", 75)  # 75% brightness

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):
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

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

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

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        mock_read.assert_not_called()


class TestDimmerExceptionPath:
    """async_update_rcp_data: exception in dimmer read → debug log, no crash."""

    @pytest.mark.asyncio
    async def test_dimmer_exception_handled_gracefully(self):
        """_read("0x0c22") raises → exception caught, coordinator not updated."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read_raises(*args, **kwargs):
            raise RuntimeError("network boom")

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read_raises),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_dimmer_cache


class TestAsyncUpdateRcpDataDimmerXmlGuard:
    """async_update_rcp_data: LED dimmer (0x0c22) XML-wrapper handling.

    Real-world log (2026-05-13, FW 9.40.25):
        RCP LED dimmer for 11111111: out-of-range raw=2570 — cache skipped

    raw=2570 (0x0A 0x0A) is the first two bytes of the cloud-proxy XML envelope
    '\\n\\n<rcp>...'. The T_WORD num=1 read truncates to 2 bytes so '<' never
    appears in the payload; _is_xml_envelope catches the pure-whitespace case.
    """

    @pytest.mark.asyncio
    async def test_xml_envelope_truncated_to_2_bytes_not_cached(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        truncated_xml = b"\n\n"  # first 2 bytes of '\n\n<rcp>...'

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                return truncated_xml if cmd == "0x0c22" else None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_dimmer_cache, (
            "Truncated XML envelope must not be cached as a dimmer value"
        )
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c22", 0) >= 1, (
            "Dimmer XML-wrapper must call _mark_fail so retries are bounded"
        )

    @pytest.mark.asyncio
    async def test_valid_dimmer_still_cached(self):
        """Regression — ensure the XML guard doesn't reject valid 0-100 values."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        valid_50 = b"\x00\x32"  # uint16 big-endian = 50

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                return valid_50 if cmd == "0x0c22" else None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_dimmer_cache.get(CAM_ID) == 50, (
            "Valid 50% dimmer reading must still be cached after XML-guard refactor"
        )


class TestAsyncUpdateRcpDataPrivacy:
    """async_update_rcp_data: privacy mask (0x0d00) parsing."""

    @pytest.mark.asyncio
    async def test_privacy_on_byte1_eq_1(self):
        """byte[1] == 1 → privacy ON."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        privacy_bytes = bytes([0x00, 0x01, 0x00, 0x00])  # byte[1] = 1 → ON

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

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

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0d00":
                    return privacy_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_privacy_cache.get(CAM_ID) == 0


class TestPrivacyExceptionPath:
    """async_update_rcp_data: exception in privacy read → debug log, no crash."""

    @pytest.mark.asyncio
    async def test_privacy_exception_handled_gracefully(self):
        """_read("0x0d00") raises → caught, privacy cache not updated."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        call_count = {"n": 0}

        async def mock_rcp_read(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return None  # dimmer → None → _mark_fail
            raise RuntimeError("privacy boom")

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_privacy_cache


class TestClockInvalidDateComponents:
    """Pin the per-field validation guard around the cam_dt construction.

    Without these branches a single malformed byte from the camera would
    raise ValueError in `datetime(...)`, get swallowed by the broad except,
    and silently disable the clock-offset diagnostic forever.
    """

    @pytest.mark.asyncio
    async def test_month_13_marks_fail_no_cache(self):
        """month=13 → outside `1 <= month <= 12` → mark_fail, no cache."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # year=2026, month=13 (invalid), day=1, hour=12
        raw_clock = struct.pack(">HBBBBBB", 2026, 13, 1, 12, 0, 0, 0)

        read_map = {"0x0a0f": raw_clock}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_clock_offset_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1

    @pytest.mark.asyncio
    async def test_day_32_marks_fail_no_cache(self):
        """day=32 → outside `1 <= day <= 31` → mark_fail, no cache."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        raw_clock = struct.pack(">HBBBBBB", 2026, 1, 32, 12, 0, 0, 0)

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw_clock if command == "0x0a0f" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_clock_offset_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1

    @pytest.mark.asyncio
    async def test_hour_25_marks_fail_no_cache(self):
        """hour=25 → outside `0 <= hour <= 23` → mark_fail, no cache."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        raw_clock = struct.pack(">HBBBBBB", 2026, 1, 1, 25, 0, 0, 0)

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw_clock if command == "0x0a0f" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_clock_offset_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1

    @pytest.mark.asyncio
    async def test_valid_clock_caches_offset(self):
        """Sanity check: valid bytes hit the cache-write branch.

        This pins the happy path so a future refactor that breaks the
        cache-write assignment is caught.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # year=2026, month=5, day=12, hour=12, minute=0, second=0, weekday=1
        raw_clock = struct.pack(">HBBBBBB", 2026, 5, 12, 12, 0, 0, 1)

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw_clock if command == "0x0a0f" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_clock_offset_cache
        # Offset value is a float in seconds — sign/magnitude depends on
        # the wall-clock at test time, so we don't pin a specific value.
        assert isinstance(coord._rcp_clock_offset_cache[CAM_ID], float)


class TestClockOutOfRange:
    """async_update_rcp_data: clock fields outside valid range → _mark_fail."""

    @pytest.mark.asyncio
    async def test_clock_out_of_range_marks_fail(self):
        """Clock raw with month=0 → validation fails → _mark_fail("0x0a0f")."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # year=2026, month=0 (invalid), rest valid
        bad_clock = struct.pack(">HBBBBBB", 2026, 0, 1, 12, 0, 0, 0)

        read_results = {
            "0x0c22": None,  # dimmer → None
            "0x0d00": None,  # privacy → None
            "0x0a0f": bad_clock,  # clock → out of range
        }

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_results.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_clock_offset_cache
        # _mark_fail should have incremented failure counter
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1


class TestClockInvalidCalendarDate:
    """Regression (bug-hunt): day=30, month=2 passes every per-field range
    check (1<=month<=12, 1<=day<=31, ...) but isn't a real calendar date.
    datetime(...) then raised ValueError, which was only caught by the outer
    `except Exception` — bypassing _mark_fail entirely. Without _mark_fail
    incrementing the counter, _skip()'s 3-strikes backoff never engages, so
    a firmware/proxy returning such garbage retried the clock read on every
    coordinator poll forever instead of being suppressed like every other
    guarded RCP field."""

    @pytest.mark.asyncio
    async def test_invalid_calendar_date_marks_fail(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # year=2026, month=2, day=30 — all per-field ranges pass, but
        # Feb 30 doesn't exist -> datetime() raises ValueError.
        bad_clock = struct.pack(">HBBBBBB", 2026, 2, 30, 12, 0, 0, 0)

        read_results = {
            "0x0c22": None,
            "0x0d00": None,
            "0x0a0f": bad_clock,
        }

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_results.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_clock_offset_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1


class TestClockRawNone:
    """async_update_rcp_data: clock raw is None → _mark_fail("0x0a0f")."""

    @pytest.mark.asyncio
    async def test_clock_none_marks_fail(self):
        """_read returns None for clock → _mark_fail called."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        # Clock was None → fail counter incremented
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1


class TestAsyncUpdateRcpDataClockXmlGuard:
    """async_update_rcp_data: clock (0x0a0f) XML-wrapper handling.

    Real-world log (2026-05-13, Terrasse 11111111 FW 9.40.25):
        RCP clock for 11111111: unexpected layout
        (Y=2570 M=60 D=114 h=99 m=112 s=62) — cache skipped

    Decoded big-endian those bytes spell '\\n\\n<rcp>\\n\\n\\t<co' — the cloud
    proxy returned its XML envelope as P_OCTET payload. The 8-byte struct unpack
    happily parsed it as a (garbage) datetime and produced a confusing log line.
    Fix: detect the XML prefix early and silently mark_fail.
    """

    @pytest.mark.asyncio
    async def test_whitespace_prefixed_xml_clock_not_cached(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0a0f</command>\n</rcp>"

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                return xml_bytes if cmd == "0x0a0f" else None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_clock_offset_cache, (
            "XML-wrapped clock response must not be cached"
        )
        # Ensures _mark_fail was called (counter incremented)
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1, (
            "Clock XML-wrapper must call _mark_fail so retries are bounded"
        )


class TestAsyncUpdateRcpDataLanIp:
    """async_update_rcp_data: LAN IP (0x0a36) — 4-byte binary and ASCII formats."""

    @pytest.mark.asyncio
    async def test_4_byte_binary_ip(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        ip_bytes = bytes([10, 0, 0, 5])  # 10.0.0.5

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

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

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

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

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

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


class TestLanIpRawNone:
    """async_update_rcp_data: LAN IP raw is None → _mark_fail."""

    @pytest.mark.asyncio
    async def test_lan_ip_none_marks_fail(self):
        """_read returns None for LAN IP → _mark_fail("0x0a36")."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_lan_ip_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a36", 0) >= 1


class TestProductNameCacheWrite:
    """Pin the success branch of the 0x0aea product-name read.

    The FAIL paths (None, XML-wrapped, empty) are covered separately below —
    this covers the cache-write line, so a refactor that breaks the
    assignment to `_rcp_product_name_cache` doesn't slip through unnoticed.
    """

    @pytest.mark.asyncio
    async def test_gen2_outdoor_product_name_cached(self):
        """Real Gen2 Outdoor name layout: ASCII + null pad → cache write."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Real Bosch Gen2 product names always end on null padding; the
        # parser rstrips NUL before decoding.
        raw_name = b"FLEXIDOME IP starlight 8000i\x00\x00\x00"

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw_name if command == "0x0aea" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert (
            coord._rcp_product_name_cache.get(CAM_ID) == "FLEXIDOME IP starlight 8000i"
        )
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0aea", 0) == 0

    @pytest.mark.asyncio
    async def test_short_ascii_name_with_whitespace_cached(self):
        """Whitespace trimmed before cache write — defensive normalisation."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Leading + trailing whitespace must be stripped.
        raw_name = b"  CAMERA_360  \x00"

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw_name if command == "0x0aea" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_product_name_cache.get(CAM_ID) == "CAMERA_360"
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0aea", 0) == 0


class TestProductNameXmlWrapped:
    """async_update_rcp_data: product name starts with '<' → unusable → _mark_fail."""

    @pytest.mark.asyncio
    async def test_xml_wrapped_product_name_skipped(self):
        """Product name raw is XML → starts with '<' → mark fail, cache not set."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_blob = b"<rcp><payload>0000</payload></rcp>"

        read_map = {
            "0x0aea": xml_blob,
        }

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_product_name_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0aea", 0) >= 1


class TestProductNameRawNone:
    """async_update_rcp_data: product name raw None → _mark_fail."""

    @pytest.mark.asyncio
    async def test_product_name_none_marks_fail(self):
        """_read returns None for product name → _mark_fail("0x0aea")."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0aea", 0) >= 1


class TestAsyncUpdateRcpDataBitrate:
    """async_update_rcp_data: bitrate ladder (0x0c81) parsing."""

    @pytest.mark.asyncio
    async def test_bitrate_ladder_parsed(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Two bitrate entries: 1000 kbps and 2000 kbps (within valid 100-50000 range)
        bitrate_bytes = struct.pack(">II", 1000, 2000)

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0c81":
                    return bitrate_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        ladder = coord._rcp_bitrate_cache.get(CAM_ID)
        assert ladder == [1000, 2000], (
            f"Bitrate ladder should be [1000, 2000], got {ladder}"
        )

    @pytest.mark.asyncio
    async def test_garbage_bitrate_from_xml_not_cached(self):
        """Gen1/360 cameras return XML-wrapped data — garbage uint32 values must not cache.

        Regression for the log entry:
        RCP bitrate for 22222222: [168442994, 1668300298, ...] — cache skipped
        Root cause: cloud proxy returns full RCP XML; parser was treating XML chars
        as big-endian uint32 kbps values. Fix: out-of-range values (> 50000) are rejected.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Simulate what Gen1 returns: XML-decoded payload bytes (garbage uint32s)
        garbage_bytes = struct.pack(">II", 168442994, 1668300298)  # from real log

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0c81":
                    return garbage_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_bitrate_cache, (
            "Out-of-range bitrate values (>50000 kbps) must not be cached — "
            "Gen1 cameras return XML-wrapped data that decodes to garbage uint32s"
        )

    @pytest.mark.asyncio
    async def test_xml_wrapped_bitrate_not_cached(self):
        """If raw bitrate bytes start with '<', it's XML — must not cache."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"<rcp><payload>00000000</payload></rcp>"

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0c81":
                    return xml_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_bitrate_cache, (
            "XML-wrapped bitrate response must not be cached (starts with '<')"
        )

    @pytest.mark.asyncio
    async def test_whitespace_prefixed_xml_bitrate_not_cached(self):
        """Gen2 FW 9.40 prefixes the XML envelope with whitespace (\\n\\n<rcp>...).

        Real-world log (2026-05-13, Innenbereich 22222222 FW 9.40.25):
            RCP bitrate for 22222222: out-of-range values
            [168442994, 1668300298, 168377443, 1869442401] — cache skipped

        Decoded big-endian those bytes spell '\\n\\n<rcp>\\n\\n\\t<comma' — the
        camera returned its XML envelope as P_OCTET payload. The original guard
        ``raw.startswith(b'<')`` did not catch this because byte 0 is 0x0A (\\n),
        not '<'. Fixed by lstrip-ing whitespace before the XML-prefix check.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0c81</command>\n</rcp>"

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                if cmd == "0x0c81":
                    return xml_bytes
                return None

            mock_read.side_effect = read_side
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_bitrate_cache, (
            "Whitespace-prefixed XML response must not be cached — Gen2 FW 9.40 "
            "returns '\\n\\n<rcp>...' which the original startswith('<') guard missed"
        )

    @pytest.mark.asyncio
    async def test_xml_wrapped_bitrate_marks_failure(self):
        """XML-wrapped bitrate response must call _mark_fail so retries are bounded.

        Without _mark_fail, a permanently XML-returning cloud proxy would cause
        the bitrate read to be retried on every coordinator update, polluting
        logs forever. The _skip/_mark_fail threshold (3 strikes) silences it.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0c81</command>\n</rcp>"

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):

            async def read_side(hass, base, cmd, sid, **kw):
                return xml_bytes if cmd == "0x0c81" else None

            mock_read.side_effect = read_side

            # Run 3 times — bitrate should be probed 3x then skipped
            for _ in range(4):
                await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        called_bitrate = [
            call for call in mock_read.call_args_list if call.args[2] == "0x0c81"
        ]
        assert len(called_bitrate) == 3, (
            f"Bitrate command 0x0c81 must be skipped after 3 XML-wrapped responses "
            f"(got {len(called_bitrate)} retries — _mark_fail/_skip not wired up)"
        )


class TestBitrateOutOfRange:
    """async_update_rcp_data: bitrate ladder contains out-of-range values → skip cache."""

    @pytest.mark.asyncio
    async def test_out_of_range_bitrate_skips_cache(self):
        """Bitrate with value > 50000 kbps → sanity check fails → cache not set."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Pack one uint32 = 999999 kbps (way out of range)
        bad_bitrate = struct.pack(">I", 999999)

        read_map = {"0x0c81": bad_bitrate}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_bitrate_cache


class TestAlarmCatalogCacheWrite:
    """Pin the success branch of the 0x0c38 alarm-catalog read.

    Covers the success path where _parse_alarm_catalog returns a non-empty
    list and the result hits the cache (the exception branch is covered
    separately by TestAlarmCatalogException below).
    """

    @staticmethod
    def _make_alarm_blob(names: list[str]) -> bytes:
        """Build a Bosch-shaped 0x0c38 payload — UTF-16-BE names joined
        with NULs. Matches the real format observed in mitm captures
        from Gen2 Outdoor firmware 9.40.25.
        """
        return ("\x00".join(names) + "\x00").encode("utf-16-be")

    @pytest.mark.asyncio
    async def test_gen2_alarm_catalog_cached(self):
        """Multiple alarm entries → list cached, types classified."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        names = [
            "Virtual Alarm 0",
            "Virtual Alarm 1",
            "Flame detected",
            "Smoke detected",
            "Audio alarm",
            "Signal loss",
            "Storage failure",
            "Motion detected",
        ]
        raw = self._make_alarm_blob(names)
        # Sanity: payload must be > 10 bytes (the length gate)
        assert len(raw) > 10

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw if command == "0x0c38" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        cached = coord._rcp_alarm_catalog_cache.get(CAM_ID)
        assert cached is not None
        assert len(cached) == len(names)
        types = {entry["type"] for entry in cached}
        assert "virtual" in types
        assert "flame" in types
        assert "smoke" in types
        assert "audio" in types
        assert "signal" in types
        assert "storage" in types
        assert "motion" in types

    @pytest.mark.asyncio
    async def test_minimal_alarm_catalog_above_threshold(self):
        """Just above the 10-byte threshold → parser runs, cache populated.

        Pin: the `len(raw) > 10` gate is inclusive of >10 only, so a 12-byte
        payload must still hit the cache-write branch.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # "AB\x00CD" in UTF-16-BE = 10 bytes; pad with one more to be > 10.
        raw = "AB\x00CD\x00EF".encode("utf-16-be")
        assert len(raw) > 10

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw if command == "0x0c38" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_alarm_catalog_cache

    @pytest.mark.asyncio
    async def test_short_payload_below_threshold_no_cache(self):
        """raw of 8 bytes (≤10) → gate fails, cache stays empty.

        Pin: the `len(raw) > 10` guard prevents tiny / handshake-only
        payloads from triggering the parser (which would yield garbage
        single-character "alarm names").
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        raw = b"\x00" * 8

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw if command == "0x0c38" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_alarm_catalog_cache


class TestAlarmCatalogException:
    """async_update_rcp_data: alarm catalog read raises → debug log, no crash."""

    @pytest.mark.asyncio
    async def test_alarm_catalog_exception_handled(self):
        """_read("0x0c38") raises → exception caught, alarm cache not updated."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        call_map = {"0x0c38": RuntimeError("catalog boom")}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command in call_map:
                raise call_map[command]
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_alarm_catalog_cache


class TestMotionZonesEdgeCases:
    """Pin two branches that protect the motion-zones cache:

    a) `raw and len(raw) >= 28` is False AND raw is not None
       → neither cache write nor `_mark_fail` (zones cache empty).
    b) `_read` raises Exception → debug log only, cache empty.
    """

    @pytest.mark.asyncio
    async def test_short_payload_under_28_bytes_no_cache(self):
        """0x0c00 returns 20 bytes (< 28) → no cache, no _mark_fail.

        Pin: a truncated payload from the cloud proxy must NOT silently
        become an empty zones list in the cache (which would mislead the
        diagnostics sensor into showing "no zones configured"). It must
        also NOT count toward the 3-strike skip rule, because the next
        read attempt could still succeed.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # 20 bytes — too short to be a single 28-byte zone
        short_raw = b"\x00" * 20

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return short_raw if command == "0x0c00" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        # Cache must NOT have an entry — truncated data is unusable.
        assert CAM_ID not in coord._rcp_motion_zones_cache
        # Fail counter must NOT have been incremented for 0x0c00 because
        # raw was not None — it was just too short. Branch design: only
        # `elif raw is None` calls _mark_fail.
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c00", 0) == 0

    @pytest.mark.asyncio
    async def test_read_exception_logged_no_crash(self):
        """`_read("0x0c00")` raises → broad except logs, no cache write, no crash.

        Pin: an aiohttp transport error mid-fetch must not propagate
        upward — async_update_rcp_data is best-effort and called from
        the main coordinator loop.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0c00":
                raise RuntimeError("transport boom")
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            # Must not raise
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_motion_zones_cache

    @pytest.mark.asyncio
    async def test_valid_28_byte_payload_caches_zones(self):
        """Sanity check: 28 bytes → one zone cached, _mark_ok called.

        Pin: the happy path is covered too, so that a refactor breaking
        the cache assignment is caught.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # 28 bytes exactly = one zone (recorder concept §RCP)
        one_zone = b"\x01" + b"\x00" * 27

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return one_zone if command == "0x0c00" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_motion_zones_cache
        zones = coord._rcp_motion_zones_cache[CAM_ID]
        assert len(zones) == 1
        assert zones[0]["zone_id"] == 0
        # _mark_ok clears the failure counter — must be 0/absent
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c00", 0) == 0


class TestMotionZonesNone:
    """async_update_rcp_data: motion zones raw is None → _mark_fail("0x0c00")."""

    @pytest.mark.asyncio
    async def test_motion_zones_none_marks_fail(self):
        """_read returns None for 0x0c00 → _mark_fail."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c00", 0) >= 1


class TestMotionZonesException:
    """async_update_rcp_data: motion zones read raises → debug log."""

    @pytest.mark.asyncio
    async def test_motion_zones_exception_handled(self):
        """_read("0x0c00") raises → caught, zones cache not set."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0c00":
                raise RuntimeError("zones boom")
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_motion_zones_cache


class TestMotionCoordsCacheWrite:
    """End-to-end pin of the 0x0c0a read → parse → cache-write path."""

    @pytest.mark.asyncio
    async def test_motion_coords_real_layout_cached(self):
        """Real ≥16-byte payload (2 zones) → cache hit."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        raw_coords = struct.pack(
            ">HHHH HHHH",
            0,
            0,
            10000,
            10000,
            2500,
            2500,
            7500,
            7500,
        )
        # Gate: len(raw) >= 16
        assert len(raw_coords) >= 16

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw_coords if command == "0x0c0a" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        cached = coord._rcp_motion_coords_cache.get(CAM_ID)
        assert cached is not None
        assert len(cached) == 2
        assert cached[0] == {"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 100.0}


class TestMotionCoordsRead:
    """async_update_rcp_data: motion coords raw → coords parsed and cached."""

    @pytest.mark.asyncio
    async def test_motion_coords_cached(self):
        """_read returns 16+ bytes for 0x0c0a → coords parsed and stored."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Two zones × 8 bytes each
        raw_coords = struct.pack(">HHHH", 0, 0, 10000, 10000) + struct.pack(
            ">HHHH", 2500, 2500, 7500, 7500
        )

        read_map = {"0x0c0a": raw_coords}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_motion_coords_cache
        zones = coord._rcp_motion_coords_cache[CAM_ID]
        assert len(zones) == 2
        assert zones[0]["x1"] == 0.0
        assert zones[0]["x2"] == 100.0


class TestTlsCertPaths:
    """async_update_rcp_data: TLS cert cached on data, _mark_fail on None."""

    @pytest.mark.asyncio
    async def test_tls_cert_cached_when_data_present(self):
        """_read returns 60+ bytes for 0x0b91 → cert_info cached."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Fake DER bytes (60 bytes, non-XML) — cryptography will fail to parse
        # but the cache entry should still be stored (raw_hex fallback)
        fake_cert = b"\x30\x82" + b"\xff" * 58

        read_map = {"0x0b91": fake_cert}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_tls_cert_cache
        assert "raw_size" in coord._rcp_tls_cert_cache[CAM_ID]

    @pytest.mark.asyncio
    async def test_tls_cert_none_marks_fail(self):
        """_read returns None for 0x0b91 → _mark_fail."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0b91", 0) >= 1


class TestNetworkServicesCached:
    """Valid non-XML payload → _parse_network_services result stored in
    coordinator._rcp_network_services_cache[cam_id]."""

    @pytest.mark.asyncio
    async def test_valid_payload_caches_services(self):
        """Non-empty, non-XML, >10-byte payload → cache written."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # ASCII service names separated by null bytes — not XML, len > 10
        network_raw = b"HTTP\x00HTTPS\x00RTSP\x00"
        assert len(network_raw) > 10
        assert not network_raw.startswith(b"<")

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0c62":
                return network_raw
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_network_services_cache
        services = coord._rcp_network_services_cache[CAM_ID]
        assert isinstance(services, list)
        # "HTTP", "HTTPS", "RTSP" all > 1 char → all kept
        assert "HTTP" in services
        assert "HTTPS" in services
        assert "RTSP" in services

    @pytest.mark.asyncio
    async def test_xml_payload_skips_cache(self):
        """XML-prefixed payload → guard prevents caching."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_raw = b"<rcp>" + b"x" * 50  # starts with '<' → skip

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0c62":
                return xml_raw
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_network_services_cache


class TestNetworkServicesXmlWrapped:
    """async_update_rcp_data: network services raw starts with '<' → skip."""

    @pytest.mark.asyncio
    async def test_xml_wrapped_services_skipped(self):
        """0x0c62 returns XML → starts with '<' → services cache not updated."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_blob = b"<rcp><payload>aabbcc</payload></rcp>"

        read_map = {"0x0c62": xml_blob}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_network_services_cache

    @pytest.mark.asyncio
    async def test_whitespace_prefixed_xml_envelope_rejected(self):
        """Regression (bug-hunt): the guard was `not raw.startswith(b"<")`,
        which only catches XML starting at byte 0. Gen2 FW 9.40 prefixes the
        envelope with whitespace (`\\n\\n<rcp>…`, same case 0x0c81 already
        guards against via _is_xml_envelope's lstrip) — the old guard missed
        this and decoded the envelope as garbage service names instead of
        rejecting it."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        whitespace_prefixed_xml = b"\n\n<rcp><payload>aabbcc</payload></rcp>"

        read_map = {"0x0c62": whitespace_prefixed_xml}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_network_services_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c62", 0) >= 1


class TestIvaCatalogCached:
    """async_update_rcp_data: IVA catalog raw → parsed and cached."""

    @pytest.mark.asyncio
    async def test_iva_catalog_cached(self):
        """0x0b60 returns 12+ bytes → IVA catalog parsed and stored."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Two entries: module_id=1 (active), module_id=2 (inactive)
        entry1 = struct.pack(">HHH", 1, 0x0100, 0x0001)  # active
        entry2 = struct.pack(">HHH", 2, 0x0200, 0x0000)  # inactive
        raw_iva = entry1 + entry2

        read_map = {"0x0b60": raw_iva}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_iva_catalog_cache
        catalog = coord._rcp_iva_catalog_cache[CAM_ID]
        assert any(m["module_id"] == 1 and m["active"] for m in catalog)
        assert any(m["module_id"] == 2 and not m["active"] for m in catalog)


# Section: async_update_rcp_data — Phase-2 command skip-guard regression
# Regression (bug-hunt): 0x0c38/0x0c0a/0x0c62/0x0b60 had no
# _skip()/_mark_fail()/_mark_ok() at all, unlike every earlier-added command
# and 0x0c00/0x0b91 — on a camera that doesn't support one of these, it
# retried every single coordinator poll forever instead of being suppressed
# after 3 consecutive failures.


class TestPhase2CommandsNowSkipGuarded:
    @pytest.mark.asyncio
    async def test_alarm_catalog_none_marks_fail_and_engages_skip(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            for _ in range(3):
                await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c38", 0) >= 3

        # A 4th poll must now SKIP the read entirely — was previously an
        # infinite unguarded retry.
        call_count = {"0x0c38": 0}

        async def mock_rcp_read_counting(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0c38":
                call_count["0x0c38"] += 1
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read_counting),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert call_count["0x0c38"] == 0, (
            "0x0c38 must be skipped after 3 consecutive failures, not retried forever"
        )

    @pytest.mark.asyncio
    async def test_motion_coords_none_marks_fail(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c0a", 0) >= 1

    @pytest.mark.asyncio
    async def test_network_services_none_marks_fail(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c62", 0) >= 1

    @pytest.mark.asyncio
    async def test_iva_catalog_none_marks_fail(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0b60", 0) >= 1

    @pytest.mark.asyncio
    async def test_alarm_catalog_xml_envelope_marks_fail(self):
        """0x0c38 XML-wrapped cloud-proxy leak → _is_xml_envelope branch → _mark_fail.

        Distinct from test_alarm_catalog_none_marks_fail_and_engages_skip above:
        that test feeds raw=None which falls through to the plain `else`
        branch's _mark_fail, never touching the `if _is_xml_envelope(raw):`
        branch body. This pins the XML-envelope-specific _mark_fail call.
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0c38</command>\n</rcp>"

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return xml_bytes if command == "0x0c38" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c38", 0) >= 1
        assert CAM_ID not in coord._rcp_alarm_catalog_cache

    @pytest.mark.asyncio
    async def test_motion_coords_xml_envelope_marks_fail(self):
        """0x0c0a XML-wrapped cloud-proxy leak → _is_xml_envelope branch → _mark_fail.

        Distinct from test_motion_coords_none_marks_fail above (raw=None hits
        the plain `else` branch, not the `if _is_xml_envelope(raw):` body).
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0c0a</command>\n</rcp>"

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return xml_bytes if command == "0x0c0a" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c0a", 0) >= 1
        assert CAM_ID not in coord._rcp_motion_coords_cache

    @pytest.mark.asyncio
    async def test_iva_catalog_xml_envelope_marks_fail(self):
        """0x0b60 XML-wrapped cloud-proxy leak → _is_xml_envelope branch → _mark_fail.

        Distinct from test_iva_catalog_none_marks_fail above (raw=None hits
        the plain `else` branch, not the `if _is_xml_envelope(raw):` body).
        """
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_bytes = b"\n\n<rcp>\n\n\t<command>0x0b60</command>\n</rcp>"

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return xml_bytes if command == "0x0b60" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0b60", 0) >= 1
        assert CAM_ID not in coord._rcp_iva_catalog_cache


class TestSkipFailMarkLogic:
    """Pin the 3-failure threshold that suppresses persistently-unsupported commands."""

    @pytest.mark.asyncio
    async def test_command_skipped_after_3_failures(self):
        """After 3 consecutive None returns for 0x0c22, dimmer reads are skipped."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Pre-seed 3 failures for 0x0c22
        coord._rcp_cmd_failures[CAM_ID]["0x0c22"] = 3

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):
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

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                new_callable=AsyncMock,
                return_value="fake-sid",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                new_callable=AsyncMock,
            ) as mock_read,
        ):
            mock_read.return_value = None
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)
            called_cmds = [call.args[2] for call in mock_read.call_args_list]

        assert "0x0c22" in called_cmds, (
            "Command must still be attempted at 2 failures — threshold is 3"
        )


class TestParseAlarmCatalog:
    """Pin _parse_alarm_catalog's UTF-16-BE decoder and alarm-type classifier."""

    def _make_utf16be_blob(self, *names: str) -> bytes:
        """Encode names as UTF-16-BE with null separators."""
        parts = [n.encode("utf-16-be") for n in names]
        return b"\x00\x00".join(parts)

    def _names_to_raw(self, names: list[str]) -> bytes:
        """Encode a list of alarm names as UTF-16-BE, separated by null chars."""
        text = "\x00".join(names)
        return text.encode("utf-16-be")

    def test_virtual_alarm_type_classified(self):
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._make_utf16be_blob("Virtual Alarm 0", "Virtual Alarm 1")
        result = _parse_alarm_catalog(raw)

        virtual = [a for a in result if a.get("type") == "virtual"]
        assert len(virtual) >= 1, (
            "Names containing 'Virtual Alarm' must get type=virtual"
        )

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

    def test_flame_type(self):
        """Name containing 'flame' → type='flame'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Flame Detector"])
        result = _parse_alarm_catalog(raw)
        types = {a["type"] for a in result}
        assert "flame" in types

    def test_smoke_type(self):
        """Name containing 'smoke' → type='smoke'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Smoke Detector"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "smoke" for a in result)

    def test_audio_type(self):
        """Name containing 'audio' → type='audio'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Audio Detection"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "audio" for a in result)

    def test_signal_loss_type(self):
        """Name containing 'signal' → type='signal'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Video Signal Loss"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "signal" for a in result)

    def test_storage_type(self):
        """Name containing 'storage' → type='storage'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Storage Failure"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "storage" for a in result)

    def test_motion_type(self):
        """Name containing 'motion' → type='motion'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Motion Detection"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "motion" for a in result)

    def test_reference_type(self):
        """Name containing 'reference' → type='reference'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Reference Image Changed"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "reference" for a in result)

    def test_config_type(self):
        """Name containing 'config' → type='config'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Config Changed"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "config" for a in result)

    def test_global_change_type(self):
        """Name containing 'global' → type='global_change'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Global Change Alarm"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "global_change" for a in result)

    def test_task_type(self):
        """Name containing 'task' → type='task'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Scheduled Task"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "task" for a in result)

    def test_unknown_type_fallback(self):
        """Name not matching any keyword → type='unknown'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Unrecognized Alarm Type"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "unknown" for a in result)

    def test_virtual_alarm_type(self):
        """Name containing 'Virtual Alarm' → type='virtual'."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        raw = self._names_to_raw(["Virtual Alarm 0"])
        result = _parse_alarm_catalog(raw)
        assert any(a["type"] == "virtual" for a in result)


class TestParseAlarmCatalogExcept:
    """Exception inside _parse_alarm_catalog's loop is caught and logged;
    the function still returns an empty list rather than raising."""

    def test_exception_in_loop_returns_empty(self):
        """A raw.decode() failure mid-parse → except catches it, returns []."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        class BadBytes(bytes):
            def decode(self, *args, **kwargs):
                raise RuntimeError("forced decode error")

        result = _parse_alarm_catalog(BadBytes(b"\x00\x01\x00\x02"))
        assert result == []

    def test_empty_raw_returns_empty_list(self):
        """Empty bytes → no parts → empty list (no exception path needed)."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        result = _parse_alarm_catalog(b"")
        assert result == []


class TestParseMotionZones:
    """Pin _parse_motion_zones' 28-byte chunk layout."""

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

    def test_parses_two_zones(self):
        """28*2 bytes → 2 zones returned."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        raw = bytes(28 * 2)
        zones = _parse_motion_zones(raw)
        assert len(zones) == 2
        assert "raw_hex" in zones[0]
        assert zones[0]["zone_id"] == 0
        assert zones[1]["zone_id"] == 1

    def test_short_raw_returns_empty(self):
        """Fewer than 28 bytes → no zones parsed."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        assert _parse_motion_zones(b"\x00" * 10) == []


class TestParseMotionCoords:
    """Pin _parse_motion_coords' 0-10000 → 0-100% coordinate conversion."""

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

        raw = self._make_coord_bytes(0, 0, 5000, 5000) + self._make_coord_bytes(
            5000, 5000, 10000, 10000
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

    def test_single_zone_converts_to_percent(self):
        """One zone: x1=0 y1=0 x2=10000 y2=10000 → 0.0/0.0/100.0/100.0 percent."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        raw = struct.pack(">HHHH", 0, 0, 10000, 10000)
        zones = _parse_motion_coords(raw)
        assert len(zones) == 1
        assert zones[0] == {"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 100.0}

    def test_partial_zone_skipped(self):
        """7 bytes (< 8) → no zone returned."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        assert _parse_motion_coords(b"\x00" * 7) == []


class TestParseMotionCoordsHappyPath:
    """Cover the _parse_motion_coords parser body with real Bosch coordinate
    layouts. The defensive `break` on a short mid-iteration chunk is
    documented as unreachable through this entry point — see
    TestDefensiveBreakBranches below, which pins that contract separately.
    """

    def test_single_full_zone(self):
        """One 8-byte zone → one rect with percent conversion."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        # x1=1000 y1=2000 x2=9000 y2=8000  (in 0-10000 units)
        raw = struct.pack(">HHHH", 1000, 2000, 9000, 8000)
        zones = _parse_motion_coords(raw)
        assert zones == [{"x1": 10.0, "y1": 20.0, "x2": 90.0, "y2": 80.0}]

    def test_multiple_zones(self):
        """Real Bosch capture: 4 zones × 8 B → 4 rects."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        raw = struct.pack(
            ">HHHH HHHH HHHH HHHH",
            0,
            0,
            10000,
            10000,  # full frame
            2500,
            2500,
            7500,
            7500,  # centre quadrant
            0,
            0,
            5000,
            5000,  # top-left
            5000,
            5000,
            10000,
            10000,  # bottom-right
        )
        zones = _parse_motion_coords(raw)
        assert len(zones) == 4
        assert zones[0] == {"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 100.0}
        assert zones[1] == {"x1": 25.0, "y1": 25.0, "x2": 75.0, "y2": 75.0}

    def test_empty_payload(self):
        """0 bytes → 0 zones — n_zones is 0, loop never enters."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        assert _parse_motion_coords(b"") == []

    def test_seven_bytes_truncated_below_one_zone(self):
        """7 bytes → less than one full 8-B zone → 0 zones.

        Pin: `n_zones = len(raw) // 8 = 0` so the loop body never runs.
        This guarantees no IndexError from unpacking partial chunks.
        """
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        assert _parse_motion_coords(b"\x00" * 7) == []

    def test_trailing_garbage_bytes_ignored(self):
        """1 full zone (8 B) + 3 extra bytes → still 1 zone (extras dropped)."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        raw = struct.pack(">HHHH", 0, 0, 5000, 5000) + b"\xff\xff\xff"
        zones = _parse_motion_coords(raw)
        assert zones == [{"x1": 0.0, "y1": 0.0, "x2": 50.0, "y2": 50.0}]


class TestParseNetworkServices:
    """Pin _parse_network_services' null-separated ASCII decoder."""

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
        assert not any(len(s) <= 1 for s in result), (
            "Single-char entries must be filtered"
        )

    def test_garbage_bytes_does_not_raise(self):
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        try:
            result = _parse_network_services(b"\xff\xfe\xab\xcd\x00RTSP\x00")
            assert isinstance(result, list)
        except Exception as exc:
            pytest.fail(f"Must not raise on garbage input: {exc}")

    def test_parses_service_names(self):
        """ASCII blob with null separators → list of service strings."""
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        raw = b"HTTP\x00RTSP\x00HTTPS\x00"
        services = _parse_network_services(raw)
        assert "HTTP" in services
        assert "RTSP" in services
        assert "HTTPS" in services

    def test_empty_parts_filtered(self):
        """Multiple consecutive nulls → empty strings filtered out."""
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        raw = b"\x00\x00HTTP\x00\x00"
        services = _parse_network_services(raw)
        assert "" not in services


class TestParseNetworkServicesExcept:
    """If an exception occurs inside the try block of _parse_network_services,
    it is caught and logged; the function returns an empty list."""

    def test_exception_in_decode_returns_empty(self):
        """Subclass bytes whose .decode() raises → except branch fires."""
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        class BadBytes(bytes):
            def decode(self, *args, **kwargs):
                raise RuntimeError("forced decode failure")

        result = _parse_network_services(BadBytes(b"HTTP\x00HTTPS"))
        assert result == []

    def test_normal_bytes_returns_services(self):
        """Sanity: normal ASCII payload parses correctly (no exception)."""
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        raw = b"HTTP\x00HTTPS\x00RTSP\x00"
        result = _parse_network_services(raw)
        assert "HTTP" in result
        assert "RTSP" in result

    def test_single_char_names_filtered_out(self):
        """Names of length <= 1 are excluded (clean and len(clean) > 1 guard)."""
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        # "A" alone is filtered; "BB" is kept
        raw = b"A\x00BB\x00"
        result = _parse_network_services(raw)
        assert "A" not in result
        assert "BB" in result


class TestParseIvaCatalog:
    """Pin _parse_iva_catalog's 6-byte TLV entry decoder."""

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

    def test_zero_module_id_skipped_amid_valid_entries(self):
        """Entry with module_id=0 → not included in output, sibling entry kept.

        Distinct from test_zero_module_id_skipped above (single all-zero
        entry vs. a zero entry alongside a real one)."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        # module_id=0, version=1, flags=1
        entry_zero = struct.pack(">HHH", 0, 1, 1)
        # module_id=5, version=2, flags=0
        entry_five = struct.pack(">HHH", 5, 2, 0)
        raw = entry_zero + entry_five

        modules = _parse_iva_catalog(raw)
        assert all(m["module_id"] != 0 for m in modules)
        assert any(m["module_id"] == 5 for m in modules)

    def test_active_flag_parsed(self):
        """flags & 0x01 == 1 → active=True."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        entry = struct.pack(">HHH", 7, 0x0100, 0x0001)
        modules = _parse_iva_catalog(entry)
        assert len(modules) == 1
        assert modules[0]["active"] is True
        assert modules[0]["module_id"] == 7


class TestParseIvaCatalogShortChunk:
    """If a chunk is shorter than entry_size (6 bytes), the parse loop breaks.

    In practice this guard fires when the raw payload length is not a
    multiple of 6 and the loop counter reaches the final partial chunk. We
    verify by building a payload that lies about its own length via `__len__`
    so the last iteration produces a short chunk.
    """

    def test_short_final_chunk_breaks_loop(self):
        """2 full entries + a lying __len__ → only 2 entries parsed, no crash."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        entry1 = struct.pack(">HHH", 0x0001, 0x0001, 0x0001)  # active
        entry2 = struct.pack(">HHH", 0x0002, 0x0002, 0x0000)  # inactive

        class PaddedBytes(bytes):
            """Bytes that lie about their length to force a short chunk."""

            def __len__(self):
                # Report 18 bytes (n=3) but actual slice at i=2 returns 1 byte
                return 18

        padded = PaddedBytes(entry1 + entry2 + b"\xaa")  # 13 real bytes
        result = _parse_iva_catalog(padded)
        # Loop runs for i=0,1 (full chunks), i=2 → chunk=b"\xaa" (1 byte) → break
        assert len(result) == 2
        assert result[0]["module_id"] == 1
        assert result[1]["module_id"] == 2

    def test_normal_payload_all_entries_parsed(self):
        """Sanity: clean 12-byte payload (2 entries) → both returned correctly."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        entry1 = struct.pack(">HHH", 0x0010, 0x0002, 0x0001)  # active flag set
        entry2 = struct.pack(">HHH", 0x0020, 0x0003, 0x0000)  # inactive
        result = _parse_iva_catalog(entry1 + entry2)
        assert len(result) == 2
        assert result[0]["active"] is True
        assert result[1]["active"] is False

    def test_zero_module_id_skipped(self):
        """module_id == 0 → entry skipped."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        zero_entry = struct.pack(">HHH", 0x0000, 0x0001, 0x0001)
        real_entry = struct.pack(">HHH", 0x0005, 0x0001, 0x0001)
        result = _parse_iva_catalog(zero_entry + real_entry)
        assert len(result) == 1
        assert result[0]["module_id"] == 5


class TestParseTlsCert:
    """ImportError on cryptography → raw_hex fallback."""

    def test_no_cryptography_returns_raw_hex(self):
        """cryptography package absent → info contains raw_hex, not subject."""
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        fake_cert_bytes = b"\x30" + b"\xff" * 50
        with patch.dict(
            "sys.modules", {"cryptography": None, "cryptography.x509": None}
        ):
            info = _parse_tls_cert(fake_cert_bytes)

        assert "raw_size" in info
        # Either raw_hex is present (ImportError path) or other fields
        assert "raw_hex" in info or "subject" in info

    def test_parse_error_returns_raw_hex(self):
        """cryptography raises Exception on bad DER → raw_hex fallback."""
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        bad_bytes = b"\x30\x00" + b"\xcc" * 50

        # Don't mock cryptography — let it try and fail on bad DER
        info = _parse_tls_cert(bad_bytes)
        assert "raw_size" in info
        assert info["raw_size"] == len(bad_bytes)


class TestParseTlsCertImportError:
    """Pin the ImportError branch of rcp._parse_tls_cert.

    Patches `cryptography.x509.load_der_x509_certificate` directly to raise
    ImportError, distinct from TestParseTlsCert above which patches
    `sys.modules` — kept as a separate class since both approaches were
    written independently and each is a more robust regression pin for a
    slightly different failure mode.
    """

    def test_load_der_importerror_falls_back_to_raw_hex(self):
        """Patch the loader to raise ImportError → info["raw_hex"] is set.

        Pin: when cryptography is broken/missing, the parser returns a
        usable dict so the diagnostics sensor can still display *something*
        rather than the entry being None.
        """
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        # Build a fake DER prefix so the function actually enters the try block
        fake_cert = b"\x30\x82" + b"\xaa" * 60

        with patch(
            "cryptography.x509.load_der_x509_certificate",
            side_effect=ImportError("cryptography missing"),
        ):
            info = _parse_tls_cert(fake_cert)

        # ImportError branch sets raw_hex (truncated) and raw_size
        assert "raw_size" in info
        assert info["raw_size"] == len(fake_cert)
        assert "raw_hex" in info
        # subject etc. must NOT be set on ImportError path
        assert "subject" not in info
        assert "issuer" not in info

    def test_load_der_value_error_falls_back_to_raw_hex(self):
        """Generic Exception (not ImportError) in cryptography → raw_hex
        fallback via the second `except Exception` branch.

        Pin: malformed DER bytes (cryptography raises ValueError) must
        not break the diagnostics path either.
        """
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        # 70 bytes of garbage — guaranteed not a valid DER cert
        bad_cert = b"\xff" * 70
        info = _parse_tls_cert(bad_cert)

        assert "raw_size" in info
        assert info["raw_size"] == 70
        # Either we got raw_hex (parse error) or subject (cryptography
        # somehow accepted it — defensive). The contract is: never raise.
        assert "raw_hex" in info or "subject" in info


class TestParseTlsCertHappyPath:
    """When cryptography is available and the cert loads correctly, all 6
    info keys are populated."""

    def test_all_cert_fields_populated(self):
        """Mock cryptography.x509 fully → info dict contains all 6 keys."""
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        # Build fake DER blob (content doesn't matter; we mock the loader)
        fake_der = b"\x30\x82" + b"\xbb" * 80

        mock_cert = MagicMock()
        mock_cert.issuer.rfc4514_string.return_value = "CN=Bosch CA,O=Bosch"
        mock_cert.subject.rfc4514_string.return_value = "CN=cam-01,O=Bosch"
        mock_cert.serial_number = 0xDEADBEEF
        mock_cert.not_valid_before_utc.isoformat.return_value = (
            "2024-01-01T00:00:00+00:00"
        )
        mock_cert.not_valid_after_utc.isoformat.return_value = (
            "2026-01-01T00:00:00+00:00"
        )
        mock_cert.public_key.return_value.key_size = 2048
        mock_cert.signature_algorithm_oid.dotted_string = "1.2.840.113549.1.1.11"

        with patch(
            "cryptography.x509.load_der_x509_certificate",
            return_value=mock_cert,
        ):
            info = _parse_tls_cert(fake_der)

        assert info["issuer"] == "CN=Bosch CA,O=Bosch"
        assert info["subject"] == "CN=cam-01,O=Bosch"
        assert info["serial"] == "deadbeef"
        assert info["not_before"] == "2024-01-01T00:00:00+00:00"
        assert info["not_after"] == "2026-01-01T00:00:00+00:00"
        assert info["key_size"] == 2048
        assert info["signature_algorithm"] == "1.2.840.113549.1.1.11"
        assert info["raw_size"] == len(fake_der)
        assert "raw_hex" not in info  # happy path: no fallback


class TestDefensiveBreakBranches:
    """Pin the defensive `break` statements in _parse_motion_zones and
    _parse_motion_coords.

    Both are guarded by `n_zones = len(raw) // zone_size`, so reaching
    them through the function entry is impossible without buffer
    mutation mid-iteration. We use a `bytes` subclass that returns a
    short slice on the n-th access — simulates the contract a future
    refactor (e.g. streaming reader) might require.

    Without these pins, a refactor that drops the defensive `break`
    while introducing a partial-buffer reader would still pass all
    other tests, and the next firmware that returns a half-zone trailer
    would crash with a struct.error.
    """

    def test_motion_zones_break_on_short_slice(self):
        """Mid-iteration short slice → the defensive `break` fires.

        We subclass `bytes` so `raw[start:end]` returns a 10-byte chunk
        on the second iteration even though `len(raw) // 28 == 2`.
        """
        from custom_components.bosch_shc_camera.rcp import _parse_motion_zones

        class TruncatingBytes(bytes):
            """Returns a deliberately short slice on the 2nd __getitem__."""

            _calls = 0

            def __getitem__(self, key):
                cls = type(self)
                if isinstance(key, slice):
                    cls._calls += 1
                    # 1st call: full 28-byte chunk (normal zone)
                    # 2nd call: only 10 bytes → triggers `if len(chunk) < 28: break`
                    if cls._calls == 2:
                        return bytes.__getitem__(self, key)[:10]
                return bytes.__getitem__(self, key)

        TruncatingBytes._calls = 0
        # 56 bytes → n_zones = 2; second iteration will get a short slice
        raw = TruncatingBytes(b"\x00" * 56)
        zones = _parse_motion_zones(raw)
        # First zone parsed, second triggered `break` → only 1 result
        assert len(zones) == 1
        assert zones[0]["zone_id"] == 0
        assert zones[0]["size"] == 28

    def test_motion_coords_break_on_short_slice(self):
        """Mid-iteration short slice → the defensive `break` fires."""
        from custom_components.bosch_shc_camera.rcp import _parse_motion_coords

        class TruncatingBytes(bytes):
            _calls = 0

            def __getitem__(self, key):
                cls = type(self)
                if isinstance(key, slice):
                    cls._calls += 1
                    # 1st call: full 8-byte chunk
                    # 2nd call: only 3 bytes → triggers `if len(chunk) < 8: break`
                    if cls._calls == 2:
                        return bytes.__getitem__(self, key)[:3]
                return bytes.__getitem__(self, key)

        TruncatingBytes._calls = 0
        # 16 bytes → n_zones = 2; second iteration short-slices
        raw = TruncatingBytes(
            struct.pack(">HHHH HHHH", 0, 0, 5000, 5000, 1000, 1000, 9000, 9000)
        )
        zones = _parse_motion_coords(raw)
        assert len(zones) == 1
        assert zones[0] == {"x1": 0.0, "y1": 0.0, "x2": 50.0, "y2": 50.0}


# Section: rcp_local_write HTTPS transport (relocated from
# tests/test_lan_fallback_during_outage.py — the switch.py/shc.py siblings
# live in tests/test_switch.py and tests/test_shc.py)


class TestRcpLocalWriteTransport:
    """`rcp_local_write` must issue HTTPS (not HTTP) and use
    `async_digest_request` when user+password are supplied — cameras only
    listen on HTTPS port 443, so opening plain HTTP always fails with
    connection-refused."""

    @pytest.mark.asyncio
    async def test_url_is_https_when_creds_supplied(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        observed_url: list[str] = []

        class _FakeResp:
            status = 200

            async def read(self):
                return b"<rcp><payload>00</payload></rcp>"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        async def _fake_digest_request(session, method, url, user, password, **_):
            observed_url.append(url)
            return _FakeResp()

        with patch(
            "custom_components.bosch_shc_camera.auth_utils.async_digest_request",
            side_effect=_fake_digest_request,
        ):
            with patch(
                "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
                return_value=MagicMock(),
            ):
                ok = await rcp_local_write(
                    MagicMock(),
                    "192.0.2.149",
                    "0x0d00",
                    "00010000",
                    "P_OCTET",
                    user="cbs-xxx",
                    password="secret",
                )

        assert ok is True
        assert observed_url, "async_digest_request was not invoked"
        assert observed_url[0].startswith("https://"), (
            f"rcp_local_write opened {observed_url[0]} — must be HTTPS so the "
            "camera (port 443, no port 80 listener) accepts it."
        )
        assert "192.0.2.149/rcp.xml" in observed_url[0]

    @pytest.mark.asyncio
    async def test_no_digest_when_creds_missing(self):
        """Anonymous fallback path still issues HTTPS, just no auth."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        observed_url: list[str] = []

        class _FakeResp:
            status = 200

            async def read(self):
                return b"<rcp><payload>00</payload></rcp>"

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        class _FakeSession:
            def get(self, url, **kwargs):
                observed_url.append(url)
                return _FakeResp()

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_FakeSession(),
        ):
            await rcp_local_write(
                MagicMock(),
                "192.0.2.149",
                "0x0d00",
                "00010000",
                "P_OCTET",
            )

        assert observed_url
        assert observed_url[0].startswith("https://"), (
            "Anonymous path emitted HTTP — must be HTTPS."
        )


# Section: 0x0d00 privacy-mask XML-envelope handling (relocated from
# tests/test_misc_modules_coverage.py)


class TestRcpPrivacyXmlEnvelope:
    """`_read("0x0d00")` returning an XML envelope must call `_mark_fail`
    instead of writing a bogus value to `_rcp_privacy_cache`."""

    def _make_coord(self):
        coord = SimpleNamespace(
            hass=MagicMock(),
            _rcp_session_cache={},
            _rcp_session_locks={},
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
            _rcp_cmd_failures={},
        )
        coord._rcp_cmd_failures[CAM_ID] = {}
        return coord

    @pytest.mark.asyncio
    async def test_privacy_xml_envelope_marks_fail(self):
        """0x0d00 returns an XML envelope → _mark_fail, privacy cache not written."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = self._make_coord()

        xml_envelope = b"<Result><Error>NotSupported</Error></Result>"

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0d00":
                return xml_envelope
            return None

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                return_value="sess123",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                side_effect=mock_rcp_read,
            ),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_privacy_cache, (
            "XML envelope response must NOT write to _rcp_privacy_cache"
        )
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0d00", 0) >= 1, (
            "XML envelope must trigger _mark_fail (failure counter >= 1)"
        )

    @pytest.mark.asyncio
    async def test_privacy_xml_envelope_is_not_treated_as_valid_data(self):
        """Complement: a valid 2-byte payload writes the cache and does NOT mark fail."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = self._make_coord()

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0d00":
                return b"\x00\x01"  # byte[1]=1 → privacy ON
            return None

        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.get_cached_rcp_session",
                return_value="sess123",
            ),
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_read",
                side_effect=mock_rcp_read,
            ),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_privacy_cache.get(CAM_ID) == 1, (
            "Valid 2-byte response must write byte[1] value to cache"
        )
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0d00", 0) == 0, (
            "Valid response must not mark fail"
        )


# Section: LED-dimmer out-of-range failure (relocated from
# tests/test_remaining_cheap_gaps.py — the local_rcp.py/smb.py siblings live
# in tests/test_local_rcp.py and tests/test_smb.py)


class TestRcpUpdateDimmerOutOfRange:
    @pytest.mark.asyncio
    async def test_dimmer_out_of_range_marks_failure(self):
        """A Gen2 cam returning `0x0A0A` (2570) for the LED dimmer is
        out-of-spec; the helper must call `_mark_fail("0x0c22")` so the
        skip-counter advances and the command stops being polled."""
        from custom_components.bosch_shc_camera import rcp

        coord = SimpleNamespace()
        coord._rcp_dimmer_cache = {}
        coord._rcp_privacy_cache = {}
        coord._rcp_clock_offset_cache = {}
        coord._rcp_lan_ip_cache = {}
        coord._rcp_product_name_cache = {}
        coord._rcp_bitrate_cache = {}
        coord._rcp_session_cache = {}
        coord._rcp_session_locks = {}
        coord._rcp_cmd_failures = {}
        coord.hass = MagicMock()

        # 0x0A0A = 2570 — out of the 0..100 dimmer range.
        bad_bytes = bytes.fromhex("0a0a")

        async def _fake_read(
            _hass, _base, command, _sid, *, type_=None, num=0, session_cache=None
        ):
            if command == "0x0c22":
                return bad_bytes
            return None

        with (
            patch.object(
                rcp, "get_cached_rcp_session", new=AsyncMock(return_value="SID-42")
            ),
            patch.object(rcp, "rcp_read", new=_fake_read),
            patch.object(rcp, "_is_xml_envelope", return_value=False),
        ):
            await rcp.async_update_rcp_data(coord, "CAM-ID", "proxy.example", "hash123")

        assert "CAM-ID" not in coord._rcp_dimmer_cache, (
            "Out-of-range raw must NOT populate the dimmer cache"
        )
        assert coord._rcp_cmd_failures.get("CAM-ID", {}).get("0x0c22", 0) >= 1, (
            "Failure counter must advance to 1 for 0x0c22"
        )
