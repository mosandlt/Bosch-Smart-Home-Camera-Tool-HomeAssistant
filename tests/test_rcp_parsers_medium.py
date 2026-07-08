"""Coverage push on rcp.py — Step 2b MEDIUM (no overlap with test_rcp_parsers_coverage.py).

Targets the remaining missing branches in rcp.py that exercise the
*success / cache-write* paths of:

  - lines 548-550  product name cache write (0x0aea happy path)
  - lines 590-592  alarm catalog cache write (0x0c38 happy path)
  - lines 718      defensive `break` in _parse_motion_zones
                   (chunk shorter than zone_size)
  - lines 741      defensive `break` in _parse_motion_coords
                   (chunk shorter than zone_size)

Existing round3 / parsers_coverage tests cover the FAIL/None/XML-wrapped
paths but never the cache-write line — refactors that lose the assignment
would slip through unnoticed otherwise.

Bosch byte layouts (from `captures/*.mitm` and rcpdoc.htm):

  0x0aea  product name   null-terminated ASCII (e.g. b"FLEXIDOME IP starlight 8000i\\x00")
  0x0c38  alarm catalog  UTF-16-BE concatenated names separated by NULs
                         (~1366 B on Gen2 Outdoor, real capture trimmed in this test)
  0x0c00  motion zones   5 × 28 B per zone — defensive `break` on truncation
  0x0c0a  motion coords  N × 8 B (x1, y1, x2, y2) big-endian uint16 in 0-10000 units

Lines 718/741 are defensive `break`s — they CANNOT be reached through the
public `_parse_motion_zones` / `_parse_motion_coords` entry points because
`n_zones = len(raw) // zone_size` already bounds the iteration to fully-
sized chunks.  Triggering them requires mutating the buffer mid-iteration,
which we simulate with a `BytesView` subclass that returns a short slice
on the last iteration — pins the defensive contract.
"""

from __future__ import annotations

import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.rcp"
CAM_ID = "11111111-1111-1111-1111-111111111111"
PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"


def _make_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    """Minimal coordinator stub for async_update_rcp_data — matches the
    shape used in test_rcp_parsers_coverage.py / test_rcp_round3.py.
    """
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


# ── 1. Product name happy path — lines 548-550 ──────────────────────────────


class TestProductNameCacheWrite:
    """Pin the success branch of the 0x0aea product-name read.

    Existing tests in test_rcp_round3 verify the FAIL paths (None, XML-
    wrapped, empty) but never the cache-write line.  Without this pin,
    a refactor that breaks the assignment to `_rcp_product_name_cache`
    would still let all existing tests pass — silently producing a
    missing diagnostics field.
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

        # Line 548 cache write
        assert (
            coord._rcp_product_name_cache.get(CAM_ID) == "FLEXIDOME IP starlight 8000i"
        )
        # Line 550 _mark_ok clears the fail counter
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0aea", 0) == 0

    @pytest.mark.asyncio
    async def test_short_ascii_name_with_whitespace_cached(self):
        """Whitespace trimmed before cache write — defensive normalisation."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Leading + trailing whitespace must be stripped (line 546 strip()).
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


# ── 2. Alarm catalog happy path — lines 590-592 ─────────────────────────────


class TestAlarmCatalogCacheWrite:
    """Pin the success branch of the 0x0c38 alarm-catalog read.

    Round3 covers only the exception branch (line 593-594).  This class
    covers the success path 588-592 where _parse_alarm_catalog returns a
    non-empty list and the result hits the cache.
    """

    @staticmethod
    def _make_alarm_blob(names: list[str]) -> bytes:
        """Build a Bosch-shaped 0x0c38 payload — UTF-16-BE names joined
        with NULs.  Matches the real format observed in mitm captures
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
        # Sanity: payload must be > 10 bytes (the gate on line 589)
        assert len(raw) > 10

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw if command == "0x0c38" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        # Line 591 cache write
        cached = coord._rcp_alarm_catalog_cache.get(CAM_ID)
        assert cached is not None
        assert len(cached) == len(names)
        # Verify types got classified (covers some of 679-700 too)
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

        Pin: the `len(raw) > 10` gate (line 589) is inclusive of >10 only,
        so a 12-byte payload must still hit the cache-write branch.
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


# ── 3. _parse_motion_coords happy path + edge cases ─────────────────────────


class TestParseMotionCoordsHappyPath:
    """Cover the _parse_motion_coords parser body (lines 735-753) with
    real Bosch coordinate layouts.

    NOTE: line 741 is a defensive `break` and is documented as
    unreachable through this entry point (see TestDefensiveBreakBranches
    below).  These tests cover the *normal* parse path.
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


# ── 4. Coordinator round-trip → motion coords cache write (lines 614-618) ───


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
        # Gate on line 613: len(raw) >= 16
        assert len(raw_coords) >= 16

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return raw_coords if command == "0x0c0a" else None

        with (
            patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        # Line 615 cache write
        cached = coord._rcp_motion_coords_cache.get(CAM_ID)
        assert cached is not None
        assert len(cached) == 2
        assert cached[0] == {"x1": 0.0, "y1": 0.0, "x2": 100.0, "y2": 100.0}


# ── 5. Defensive `break` branches — lines 718 and 741 ───────────────────────


class TestDefensiveBreakBranches:
    """Pin the defensive `break` statements in _parse_motion_zones (718)
    and _parse_motion_coords (741).

    Both are guarded by `n_zones = len(raw) // zone_size`, so reaching
    them through the function entry is impossible without buffer
    mutation mid-iteration.  We use a `bytes` subclass that returns a
    short slice on the n-th access — simulates the contract a future
    refactor (e.g. streaming reader) might require.

    Without these pins, a refactor that drops the defensive `break`
    while introducing a partial-buffer reader would still pass all
    other tests, and the next firmware that returns a half-zone trailer
    would crash with a struct.error.
    """

    def test_motion_zones_break_on_short_slice(self):
        """Mid-iteration short slice → `break` at line 718 fires.

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
        """Mid-iteration short slice → `break` at line 741 fires."""
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
