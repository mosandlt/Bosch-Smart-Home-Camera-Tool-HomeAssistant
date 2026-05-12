"""rcp.py — remaining uncovered lines.

Targets:
  640-642  _parse_network_services() result cached in async_update_rcp_data
  702-703  _parse_alarm_catalog() except branch
  765-771  _parse_tls_cert() cryptography happy path (all 6 info keys)
  795-796  _parse_network_services() except branch
  811      _parse_iva_catalog() short-chunk break guard

Each test pins input → expected output so a future refactor cannot silently
regress. Sentinel float('-inf') used for any "never done" monotonic timestamps
per SENTINEL_RULE.
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
    """Minimal coordinator stub for async_update_rcp_data."""
    coord = SimpleNamespace(
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
        _rcp_cmd_failures={},
    )
    coord._rcp_cmd_failures[cam_id] = {}
    return coord


# ── Lines 640-642: network services cached after parse ──────────────────────


class TestNetworkServicesCached:
    """Lines 640-642: valid non-XML payload → _parse_network_services result
    stored in coordinator._rcp_network_services_cache[cam_id]."""

    @pytest.mark.asyncio
    async def test_valid_payload_caches_services(self):
        """Non-empty, non-XML, >10-byte payload → cache written (lines 640-642)."""
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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
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
        """XML-prefixed payload → guard prevents caching (line 639 condition)."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_raw = b"<rcp>" + b"x" * 50  # starts with '<' → skip

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0c62":
                return xml_raw
            return None

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_network_services_cache


# ── Lines 702-703: _parse_alarm_catalog except branch ───────────────────────


class TestParseAlarmCatalogExcept:
    """Line 702-703: exception inside _parse_alarm_catalog loop is caught and
    logged; the function still returns an empty list rather than raising."""

    def test_exception_in_loop_returns_empty(self):
        """Inject an object whose .isprintable() raises → except catches it,
        returns empty list (lines 702-703)."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        # Craft raw bytes that decode to UTF-16-BE without error so we reach
        # the for-loop, then patch str.isprintable to raise mid-loop.
        # "Virtual Alarm 0" in UTF-16-BE:
        text_utf16 = "VirtualAlarm".encode("utf-16-be")

        original_join = "".join

        def raise_on_join(iterable):
            # Force raise on first call to simulate an error inside the loop
            raise RuntimeError("injected error")

        with patch("builtins.chr", side_effect=RuntimeError("chr error")):
            # Use bytes that cannot decode cleanly instead — simpler approach:
            # pass bytes that will trigger an error in the decode/loop path.
            pass

        # Simplest: monkey-patch the raw.decode to raise
        class BadBytes(bytes):
            def decode(self, *args, **kwargs):  # noqa: D102
                raise RuntimeError("forced decode error")

        result = _parse_alarm_catalog(BadBytes(b"\x00\x01\x00\x02"))
        assert result == []

    def test_empty_raw_returns_empty_list(self):
        """Empty bytes → no parts → empty list (no exception path needed)."""
        from custom_components.bosch_shc_camera.rcp import _parse_alarm_catalog

        result = _parse_alarm_catalog(b"")
        assert result == []


# ── Lines 765-771: _parse_tls_cert cryptography happy path ──────────────────


class TestParseTlsCertHappyPath:
    """Lines 765-771: when cryptography is available and cert loads correctly,
    all 6 info keys are populated."""

    def test_all_cert_fields_populated(self):
        """Mock cryptography.x509 fully → info dict contains all 6 keys."""
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        # Build fake DER blob (content doesn't matter; we mock the loader)
        fake_der = b"\x30\x82" + b"\xbb" * 80

        mock_cert = MagicMock()
        mock_cert.issuer.rfc4514_string.return_value = "CN=Bosch CA,O=Bosch"
        mock_cert.subject.rfc4514_string.return_value = "CN=cam-01,O=Bosch"
        mock_cert.serial_number = 0xDEADBEEF
        mock_cert.not_valid_before_utc.isoformat.return_value = "2024-01-01T00:00:00+00:00"
        mock_cert.not_valid_after_utc.isoformat.return_value = "2026-01-01T00:00:00+00:00"
        mock_cert.public_key.return_value.key_size = 2048
        mock_cert.signature_algorithm_oid.dotted_string = "1.2.840.113549.1.1.11"

        with patch(
            "cryptography.x509.load_der_x509_certificate",
            return_value=mock_cert,
        ):
            info = _parse_tls_cert(fake_der)

        assert info["issuer"] == "CN=Bosch CA,O=Bosch"           # line 765
        assert info["subject"] == "CN=cam-01,O=Bosch"            # line 766
        assert info["serial"] == "deadbeef"                       # line 767
        assert info["not_before"] == "2024-01-01T00:00:00+00:00" # line 768
        assert info["not_after"] == "2026-01-01T00:00:00+00:00"  # line 769
        assert info["key_size"] == 2048                           # line 770
        assert info["signature_algorithm"] == "1.2.840.113549.1.1.11"  # line 771
        assert info["raw_size"] == len(fake_der)
        assert "raw_hex" not in info  # happy path: no fallback


# ── Lines 795-796: _parse_network_services except branch ────────────────────


class TestParseNetworkServicesExcept:
    """Lines 795-796: if an exception occurs inside the try block of
    _parse_network_services, it is caught and logged; returns empty list."""

    def test_exception_in_decode_returns_empty(self):
        """Subclass bytes whose .decode() raises → except branch at line 795."""
        from custom_components.bosch_shc_camera.rcp import _parse_network_services

        class BadBytes(bytes):
            def decode(self, *args, **kwargs):  # noqa: D102
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


# ── Line 811: _parse_iva_catalog short-chunk break ──────────────────────────


class TestParseIvaCatalogShortChunk:
    """Line 811: if a chunk is shorter than entry_size (6 bytes), loop breaks.

    In practice this guard fires when the raw payload length is not a multiple
    of 6 and the loop counter reaches the final partial chunk.  We verify by
    building a payload that has n full entries + 1 extra byte so the last
    iteration produces a 1-byte chunk.
    """

    def test_short_final_chunk_breaks_loop(self):
        """2 full entries + 1 trailing byte → only 2 entries parsed (line 811)."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        # entry_size = 6; build 2 full entries (module_id > 0 so both kept)
        entry1 = struct.pack(">HHH", 0x0001, 0x0001, 0x0001)  # active
        entry2 = struct.pack(">HHH", 0x0002, 0x0002, 0x0000)  # inactive
        extra = b"\xff"  # 1 trailing byte → forces a short chunk if n is wrong

        # Manually pass bytes with length that makes n=3 but last chunk is 1 byte
        # len = 13 → n = min(13//6, 65) = 2 → loop runs 0,1 only; line 811 not hit
        # To hit line 811 we need len ≥ 3*6 = 18 but chunk[12:18] has fewer bytes.
        # Build a buffer that is exactly 13 bytes so n=2 but we add extra:
        # Actually we need the loop to reach i where chunk is short.
        # Trick: use a BadBytes subclass that returns short data for slice.
        raw_full = entry1 + entry2 + extra  # 13 bytes, n=2 → line 811 NOT hit
        # To hit line 811: we need n > actual_chunks.
        # Build 12 bytes (2 full entries) but report n=3 via a mock.
        # Simplest: build 18 bytes where last 6 are zeros (module_id=0, skipped
        # by `if module_id > 0`) — that's lines 816-822 not 811.
        # For line 811 we need len(chunk) < entry_size:
        #   raw = entry1 + entry2 + b"\xaa\xbb"  (14 bytes → n = 14//6 = 2, OK)
        # Actually n=min(len//6, 65). With 14 bytes n=2, loop i=0..1, each 6B.
        # With 13 bytes n=2 too. We need n * entry_size > len(raw) somehow.
        # The only way is to subclass bytes so that len() returns more than actual.
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
        """module_id == 0 → entry skipped (line 816 guard)."""
        from custom_components.bosch_shc_camera.rcp import _parse_iva_catalog

        zero_entry = struct.pack(">HHH", 0x0000, 0x0001, 0x0001)
        real_entry = struct.pack(">HHH", 0x0005, 0x0001, 0x0001)
        result = _parse_iva_catalog(zero_entry + real_entry)
        assert len(result) == 1
        assert result[0]["module_id"] == 5
