"""Coverage push on rcp.py parser + branch paths.

These tests target three areas with weak / missing coverage:

  1. `_parse_clock_offset` (lines 487-507) — datetime validation guard.
     The Gen2 firmware sometimes returns clock bytes with fields outside
     valid datetime ranges (e.g. month=13, day=32, hour=25).  The guard
     must reject those without raising and call `_mark_fail("0x0a0f")`
     so the failure counter increments and the command gets skipped
     after 3 strikes.  Without the guard `datetime(...)` would raise
     ValueError, the surrounding try/except would swallow it, and the
     cache would silently stop updating — much harder to diagnose.

  2. `_parse_tls_cert` ImportError fallback (lines 760-778).
     If `cryptography` is unavailable the parser must return a dict with
     a `raw_hex` fallback and `raw_size`, never raise ImportError.

  3. Motion-zones short-payload + read-exception (lines 596-608).
     - Payload < 28 bytes → no cache write (must be NOT in cache).
     - `_read` raising → debug log only, no crash, no cache write.

User/forum source: project-internal — these are the Phase 2 RCP read
branches that get exercised whenever the cloud proxy returns truncated
or malformed payloads (observed weekly in mitmproxy captures from Gen1
360 cameras).
"""

from __future__ import annotations

import struct
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.rcp"
CAM_ID = "11111111-1111-1111-1111-111111111111"
PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"


def _make_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    """Minimal coordinator stub for async_update_rcp_data."""
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


# ── 1. clock offset — invalid date components → _mark_fail ──────────────────


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
        """Sanity check: valid bytes hit the cache-write branch (lines 493-500).

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


# ── 2. _parse_tls_cert — ImportError → raw_hex fallback ─────────────────────


class TestParseTlsCertImportError:
    """Pin the ImportError branch at line 772-774 of rcp._parse_tls_cert.

    The previous test (test_rcp_round3 / TestParseTlsCert) patches
    `sys.modules` which works on first import but is fragile across
    Python versions.  Here we patch `cryptography.x509.load_der_x509_certificate`
    directly to raise ImportError — same effect, more robust.
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
        fallback via the second `except Exception` branch (lines 775-777).

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


# ── 3. Motion zones — short payload + read exception (lines 596-608) ────────


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

        Pin: branches 600-604 (the happy path) are covered too, so that
        a refactor breaking the cache assignment is caught.
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
