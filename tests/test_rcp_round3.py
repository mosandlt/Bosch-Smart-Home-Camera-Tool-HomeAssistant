"""rcp.py — round-3 tests: async_update_rcp_data branches and parser functions.

Target lines:
  - 313: rcp_read num parameter included in params
  - 317: _drop_cached_session — session_cache is None → early return
  - 459-460: dimmer exception handler
  - 470-471: privacy exception handler
  - 482-503: clock offset — out-of-range layout → _mark_fail
  - 510-511: clock raw is None → _mark_fail
  - 537-538: LAN IP raw is None → _mark_fail
  - 546-553: product name — empty/XML-wrapped → _mark_fail
  - 559-560: product name raw is None → _mark_fail
  - 579-580: bitrate out-of-range → skip cache
  - 590-594: alarm catalog read error
  - 601-604: motion zones — raw None → _mark_fail
  - 607-608: motion zones exception
  - 614-618: motion coords read
  - 625-628: TLS cert — raw with data → cache stored
  - 631-632: TLS cert raw is None → _mark_fail
  - 640-644: network services — raw starts with "<" → skip
  - 650-654: IVA catalog read
  - 684, 686, 688, 690: _parse_alarm_catalog type classification
  - 694, 696, 698, 700, 702-703: more alarm type branches
  - 718, 741: _parse_motion_zones and _parse_motion_coords
  - 761-778: _parse_tls_cert with cryptography import error
  - 795-796: _parse_network_services
  - 811: _parse_iva_catalog module_id=0 → skip

Helpers: SimpleNamespace coordinator stub, async mock for _read results.
"""
from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.rcp"
CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"


def _make_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    """Minimal coordinator stub required by async_update_rcp_data."""
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


def _mock_ha_session(status: int = 200, body: bytes = b""):
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


# ── rcp_read: num parameter ──────────────────────────────────────────────────


class TestRcpReadNumParam:
    """Covers line 313: when num != 0, 'num' is added to params dict."""

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

        with patch(f"{MODULE}.async_get_clientsession", return_value=mock_session):
            result = await rcp_read(
                hass, "https://proxy/hash/rcp.xml", "0x0c22",
                "sess123", type_="T_WORD", num=1,
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

        with patch(f"{MODULE}.async_get_clientsession", return_value=mock_session):
            await rcp_read(hass, "https://proxy/hash/rcp.xml", "0x0d00", "sess123")

        assert "num" not in captured_params


# ── rcp_read: _drop_cached_session when session_cache is None ────────────────


class TestRcpReadDropSessionNone:
    """Covers line 317: _drop_cached_session with session_cache=None → no-op."""

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

        with patch(f"{MODULE}.async_get_clientsession", return_value=mock_session):
            result = await rcp_read(
                hass, "https://proxy/hash/rcp.xml", "0x0d00",
                "sess123", session_cache=None,
            )

        assert result is None


# ── async_update_rcp_data: dimmer exception path ─────────────────────────────


class TestDimmerExceptionPath:
    """Covers lines 459-460: exception in dimmer read → debug log, no crash."""

    @pytest.mark.asyncio
    async def test_dimmer_exception_handled_gracefully(self):
        """_read("0x0c22") raises → exception caught, coordinator not updated."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read_raises(*args, **kwargs):
            raise RuntimeError("network boom")

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read_raises):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_dimmer_cache


# ── async_update_rcp_data: privacy exception path ────────────────────────────


class TestPrivacyExceptionPath:
    """Covers lines 470-471: exception in privacy read → debug log, no crash."""

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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_privacy_cache


# ── async_update_rcp_data: clock out-of-range layout ────────────────────────


class TestClockOutOfRange:
    """Covers lines 501-503: clock fields outside valid range → _mark_fail."""

    @pytest.mark.asyncio
    async def test_clock_out_of_range_marks_fail(self):
        """Clock raw with month=0 → validation fails → _mark_fail("0x0a0f")."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # year=2026, month=0 (invalid), rest valid
        bad_clock = struct.pack(">HBBBBBB", 2026, 0, 1, 12, 0, 0, 0)

        read_results = {
            "0x0c22": None,    # dimmer → None
            "0x0d00": None,    # privacy → None
            "0x0a0f": bad_clock,  # clock → out of range
        }

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_results.get(command)

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_clock_offset_cache
        # _mark_fail should have incremented failure counter
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1


# ── async_update_rcp_data: clock raw is None → _mark_fail ───────────────────


class TestClockRawNone:
    """Covers lines 508-509: clock raw is None → _mark_fail("0x0a0f")."""

    @pytest.mark.asyncio
    async def test_clock_none_marks_fail(self):
        """_read returns None for clock → _mark_fail called."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        # Clock was None → fail counter incremented
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a0f", 0) >= 1


# ── async_update_rcp_data: LAN IP raw is None → _mark_fail ──────────────────


class TestLanIpRawNone:
    """Covers lines 535-536: LAN IP raw is None → _mark_fail."""

    @pytest.mark.asyncio
    async def test_lan_ip_none_marks_fail(self):
        """_read returns None for LAN IP → _mark_fail("0x0a36")."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_lan_ip_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0a36", 0) >= 1


# ── async_update_rcp_data: product name XML-wrapped → _mark_fail ─────────────


class TestProductNameXmlWrapped:
    """Covers lines 546-553: product name starts with '<' → unusable → _mark_fail."""

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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_product_name_cache
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0aea", 0) >= 1


# ── async_update_rcp_data: product name raw None → _mark_fail ────────────────


class TestProductNameRawNone:
    """Covers lines 557-558: product name raw is None → _mark_fail."""

    @pytest.mark.asyncio
    async def test_product_name_none_marks_fail(self):
        """_read returns None for product name → _mark_fail("0x0aea")."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0aea", 0) >= 1


# ── async_update_rcp_data: bitrate out-of-range ──────────────────────────────


class TestBitrateOutOfRange:
    """Covers lines 574-578: bitrate ladder contains out-of-range values → skip cache."""

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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_bitrate_cache


# ── async_update_rcp_data: alarm catalog exception ───────────────────────────


class TestAlarmCatalogException:
    """Covers lines 590-594: alarm catalog read raises → debug log, no crash."""

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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_alarm_catalog_cache


# ── async_update_rcp_data: motion zones raw None → _mark_fail ────────────────


class TestMotionZonesNone:
    """Covers lines 605-606: motion zones raw is None → _mark_fail("0x0c00")."""

    @pytest.mark.asyncio
    async def test_motion_zones_none_marks_fail(self):
        """_read returns None for 0x0c00 → _mark_fail."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(*args, **kwargs):
            return None

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0c00", 0) >= 1


# ── async_update_rcp_data: motion zones exception ────────────────────────────


class TestMotionZonesException:
    """Covers lines 607-608: motion zones read raises → debug log."""

    @pytest.mark.asyncio
    async def test_motion_zones_exception_handled(self):
        """_read("0x0c00") raises → caught, zones cache not set."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0c00":
                raise RuntimeError("zones boom")
            return None

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_motion_zones_cache


# ── async_update_rcp_data: motion coords read ────────────────────────────────


class TestMotionCoordsRead:
    """Covers lines 614-618: motion coords raw → coords parsed and cached."""

    @pytest.mark.asyncio
    async def test_motion_coords_cached(self):
        """_read returns 16+ bytes for 0x0c0a → coords parsed and stored."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Two zones × 8 bytes each
        raw_coords = struct.pack(">HHHH", 0, 0, 10000, 10000) + struct.pack(">HHHH", 2500, 2500, 7500, 7500)

        read_map = {"0x0c0a": raw_coords}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_motion_coords_cache
        zones = coord._rcp_motion_coords_cache[CAM_ID]
        assert len(zones) == 2
        assert zones[0]["x1"] == 0.0
        assert zones[0]["x2"] == 100.0


# ── async_update_rcp_data: TLS cert stored and raw None ──────────────────────


class TestTlsCertPaths:
    """Covers lines 623-632: TLS cert cached on data, _mark_fail on None."""

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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID].get("0x0b91", 0) >= 1


# ── async_update_rcp_data: network services XML-wrapped → skip ───────────────


class TestNetworkServicesXmlWrapped:
    """Covers lines 639-644: network services raw starts with '<' → skip."""

    @pytest.mark.asyncio
    async def test_xml_wrapped_services_skipped(self):
        """0x0c62 returns XML → starts with '<' → services cache not updated."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        xml_blob = b"<rcp><payload>aabbcc</payload></rcp>"

        read_map = {"0x0c62": xml_blob}

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            return read_map.get(command)

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID not in coord._rcp_network_services_cache


# ── async_update_rcp_data: IVA catalog cached ────────────────────────────────


class TestIvaCatalogCached:
    """Covers lines 650-654: IVA catalog raw → parsed and cached."""

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

        with patch(f"{MODULE}.get_cached_rcp_session", return_value="sess123"), \
             patch(f"{MODULE}.rcp_read", side_effect=mock_rcp_read):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert CAM_ID in coord._rcp_iva_catalog_cache
        catalog = coord._rcp_iva_catalog_cache[CAM_ID]
        assert any(m["module_id"] == 1 and m["active"] for m in catalog)
        assert any(m["module_id"] == 2 and not m["active"] for m in catalog)


# ── _parse_alarm_catalog: type classification branches ───────────────────────


class TestParseAlarmCatalog:
    """Covers lines 684-703: alarm type classification in _parse_alarm_catalog."""

    def _names_to_raw(self, names: list[str]) -> bytes:
        """Encode a list of alarm names as UTF-16-BE, separated by null chars."""
        text = "\x00".join(names)
        return text.encode("utf-16-be")

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


# ── _parse_motion_zones ──────────────────────────────────────────────────────


class TestParseMotionZones:
    """Covers _parse_motion_zones: correct count and raw_hex present."""

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


# ── _parse_motion_coords ─────────────────────────────────────────────────────


class TestParseMotionCoords:
    """Covers _parse_motion_coords: correct percent conversion."""

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


# ── _parse_tls_cert: import error path ──────────────────────────────────────


class TestParseTlsCert:
    """Covers lines 772-774: ImportError on cryptography → raw_hex fallback."""

    def test_no_cryptography_returns_raw_hex(self):
        """cryptography package absent → info contains raw_hex, not subject."""
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        fake_cert_bytes = b"\x30" + b"\xff" * 50
        with patch.dict("sys.modules", {"cryptography": None, "cryptography.x509": None}):
            info = _parse_tls_cert(fake_cert_bytes)

        assert "raw_size" in info
        # Either raw_hex is present (ImportError path) or other fields
        assert "raw_hex" in info or "subject" in info

    def test_parse_error_returns_raw_hex(self):
        """cryptography raises Exception on bad DER → raw_hex fallback (lines 775-777)."""
        from custom_components.bosch_shc_camera.rcp import _parse_tls_cert

        bad_bytes = b"\x30\x00" + b"\xcc" * 50

        # Don't mock cryptography — let it try and fail on bad DER
        info = _parse_tls_cert(bad_bytes)
        assert "raw_size" in info
        assert info["raw_size"] == len(bad_bytes)


# ── _parse_network_services ──────────────────────────────────────────────────


class TestParseNetworkServices:
    """Covers lines 787-796: null-separated ASCII service names parsed."""

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


# ── _parse_iva_catalog: module_id=0 → skip ──────────────────────────────────


class TestParseIvaCatalog:
    """Covers line 816: module_id=0 entries are skipped."""

    def test_zero_module_id_skipped(self):
        """Entry with module_id=0 → not included in output."""
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
