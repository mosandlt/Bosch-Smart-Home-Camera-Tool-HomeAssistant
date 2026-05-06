"""Tests for local_rcp.py — RCP+ XML response parser and sync read helpers.

local_rcp.py provides:
  _parse_rcp_xml(text, type_)  — pure XML parser for RCP+ read responses
  rcp_read_local_sync(...)     — Digest-auth GET to camera LAN IP
  rcp_read_remote_sync(...)    — empty-auth GET via cloud proxy

The parser is the only testable unit without network. _parse_rcp_xml handles
five type codes (T_WORD, T_DWORD, T_BYTE, P_STRING, P_OCTET), error elements,
and malformed XML. Tests pin all branches so a protocol change surfaces here.

Note: rcp_read_local_sync / rcp_read_remote_sync are thin wrappers around
`requests` + _parse_rcp_xml. Their network paths are not unit-tested (they
require a live camera) — only the error/fallback paths are pinned here via
mock.

Historical note: field-specific helpers for 0x0d00 (privacy mask) and 0x0c22
(LED dimmer) were removed in v10.4.9 after A/B testing proved they did NOT
match the user-facing privacy toggle. Do not add them back without live
verification (see the RETIRED section in local_rcp.py).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


# ── _parse_rcp_xml ────────────────────────────────────────────────────────────


class TestParseRcpXml:
    """Pin every branch of _parse_rcp_xml."""

    def _parse(self, text: str, type_: str):
        from custom_components.bosch_shc_camera.local_rcp import _parse_rcp_xml
        return _parse_rcp_xml(text, type_)

    # ── Integer types ─────────────────────────────────────────────────────────

    def test_t_word_returns_integer(self):
        """T_WORD decimal → int."""
        xml = "<rcp><result><dec>42</dec></result></rcp>"
        assert self._parse(xml, "T_WORD") == 42, "T_WORD must return the integer value"

    def test_t_dword_returns_integer(self):
        """T_DWORD decimal → int."""
        xml = "<rcp><result><dec>65535</dec></result></rcp>"
        assert self._parse(xml, "T_DWORD") == 65535, "T_DWORD must return the integer value"

    def test_t_byte_returns_integer(self):
        """T_BYTE decimal → int."""
        xml = "<rcp><result><dec>1</dec></result></rcp>"
        assert self._parse(xml, "T_BYTE") == 1, "T_BYTE must return the integer value"

    def test_t_word_zero(self):
        """Zero decimal is a valid value (not falsy-None)."""
        xml = "<rcp><result><dec>0</dec></result></rcp>"
        result = self._parse(xml, "T_WORD")
        assert result == 0, "0 is a valid RCP value — must not be confused with None"
        assert result is not None

    def test_t_word_missing_dec_returns_none(self):
        """Missing <dec> element → None."""
        xml = "<rcp><result></result></rcp>"
        assert self._parse(xml, "T_WORD") is None

    def test_t_word_non_integer_dec_returns_none(self):
        """Non-numeric <dec> text → None (not a crash)."""
        xml = "<rcp><result><dec>NOTANUMBER</dec></result></rcp>"
        assert self._parse(xml, "T_WORD") is None

    # ── String type ───────────────────────────────────────────────────────────

    def test_p_string_returns_text(self):
        """P_STRING <str> element → plain string."""
        xml = "<rcp><result><str>Bosch_Camera</str></result></rcp>"
        assert self._parse(xml, "P_STRING") == "Bosch_Camera"

    def test_p_string_empty_element_returns_empty_string(self):
        """Empty <str/> element → empty string, not None."""
        xml = "<rcp><result><str/></result></rcp>"
        result = self._parse(xml, "P_STRING")
        assert result == "", "Empty P_STRING must return '' not None"

    def test_p_string_missing_str_returns_none(self):
        xml = "<rcp><result></result></rcp>"
        assert self._parse(xml, "P_STRING") is None

    # ── Octet type ────────────────────────────────────────────────────────────

    def test_p_octet_returns_bytes(self):
        """P_OCTET space-separated hex → bytes."""
        xml = "<rcp><result><str>01 02 03 04</str></result></rcp>"
        result = self._parse(xml, "P_OCTET")
        assert result == b"\x01\x02\x03\x04", "P_OCTET must decode space-separated hex to bytes"

    def test_p_octet_compact_no_spaces(self):
        """P_OCTET without spaces also parses correctly."""
        xml = "<rcp><result><str>DEADBEEF</str></result></rcp>"
        result = self._parse(xml, "P_OCTET")
        assert result == bytes.fromhex("DEADBEEF")

    def test_p_octet_invalid_hex_returns_none(self):
        """Non-hex content in <str> → None (not a crash)."""
        xml = "<rcp><result><str>ZZ ZZ</str></result></rcp>"
        assert self._parse(xml, "P_OCTET") is None

    def test_p_octet_missing_str_returns_none(self):
        xml = "<rcp><result></result></rcp>"
        assert self._parse(xml, "P_OCTET") is None

    # ── Error element ─────────────────────────────────────────────────────────

    def test_err_element_returns_none(self):
        """<err> in the response indicates wrong auth-level / unsupported command → None."""
        xml = "<rcp><result><err>0x04</err></result></rcp>"
        assert self._parse(xml, "T_WORD") is None, (
            "<err> must produce None — not silently return 0 or raise"
        )

    def test_err_element_beats_dec_element(self):
        """When both <err> and <dec> are present, <err> wins → None."""
        xml = "<rcp><result><err>0x01</err><dec>99</dec></result></rcp>"
        assert self._parse(xml, "T_WORD") is None

    # ── Malformed / non-XML ───────────────────────────────────────────────────

    def test_malformed_xml_returns_none(self):
        """Parse failure must return None, not raise."""
        assert self._parse("<not valid XML <<", "T_WORD") is None

    def test_empty_string_returns_none(self):
        assert self._parse("", "T_WORD") is None

    def test_unknown_type_returns_none(self):
        """Unknown type code → None (no crash, no silently wrong value)."""
        xml = "<rcp><result><dec>1</dec></result></rcp>"
        assert self._parse(xml, "T_UNKNOWN_TYPE") is None

    # ── Realistic XML from Gen2 camera (FW 9.40.25) ───────────────────────────

    def test_realistic_t_word_dimmer_response(self):
        """Realistic XML for LED dimmer read (0x0c22 T_WORD)."""
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<rcp>"
            "  <command><hex>0x0c22</hex><str>LED dimmer</str></command>"
            "  <type>T_WORD</type>"
            "  <result><dec>50</dec></result>"
            "</rcp>"
        )
        assert self._parse(xml, "T_WORD") == 50

    def test_realistic_p_string_product_name(self):
        """Realistic XML for product name read (P_STRING)."""
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<rcp>"
            "  <type>P_STRING</type>"
            "  <result><str>HOME_Eyes_Outdoor_II</str></result>"
            "</rcp>"
        )
        assert self._parse(xml, "P_STRING") == "HOME_Eyes_Outdoor_II"


# ── rcp_read_local_sync error-path ────────────────────────────────────────────


class TestRcpReadLocalSyncErrors:
    """Pin the error/fallback paths of rcp_read_local_sync without network."""

    def test_http_non_200_returns_none(self):
        """Non-200 HTTP response → None (not a crash)."""
        from custom_components.bosch_shc_camera.local_rcp import rcp_read_local_sync

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        with patch("custom_components.bosch_shc_camera.local_rcp.rcp_read_local_sync.__module__"):
            pass  # structural — just import check
        import requests as req
        with patch.object(req, "get", return_value=mock_resp):
            result = rcp_read_local_sync("10.0.0.1:443", "cbs-user", "pass", "0x0c22", "T_WORD")
        assert result is None, "HTTP 403 must return None"

    def test_connection_error_returns_none(self):
        """Connection error → None (camera offline)."""
        from custom_components.bosch_shc_camera.local_rcp import rcp_read_local_sync

        import requests as req
        with patch.object(req, "get", side_effect=req.exceptions.ConnectionError("refused")):
            result = rcp_read_local_sync("10.0.0.1:443", "cbs-user", "pass", "0x0c22", "T_WORD")
        assert result is None

    def test_timeout_returns_none(self):
        """Timeout → None (slow camera response)."""
        from custom_components.bosch_shc_camera.local_rcp import rcp_read_local_sync

        import requests as req
        with patch.object(req, "get", side_effect=req.exceptions.Timeout("timeout")):
            result = rcp_read_local_sync("10.0.0.1:443", "cbs-user", "pass", "0x0c22", "T_WORD")
        assert result is None

    def test_success_delegates_to_parse(self):
        """200 response → _parse_rcp_xml called with response text."""
        from custom_components.bosch_shc_camera.local_rcp import rcp_read_local_sync

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<rcp><result><dec>77</dec></result></rcp>"

        import requests as req
        with patch.object(req, "get", return_value=mock_resp):
            result = rcp_read_local_sync("10.0.0.1:443", "u", "p", "0x0c22", "T_WORD")
        assert result == 77, "200 response body must be parsed via _parse_rcp_xml"


# ── rcp_read_remote_sync error-path ──────────────────────────────────────────


class TestRcpReadRemoteSyncErrors:
    """Pin the error/fallback paths of rcp_read_remote_sync without network."""

    def test_http_non_200_returns_none(self):
        from custom_components.bosch_shc_camera.local_rcp import rcp_read_remote_sync

        mock_resp = MagicMock()
        mock_resp.status_code = 503
        import requests as req
        with patch.object(req, "get", return_value=mock_resp):
            result = rcp_read_remote_sync("proxy-20.live.cbs.boschsecurity.com:42090/abc123", "0x0c22", "T_WORD")
        assert result is None

    def test_connection_error_returns_none(self):
        from custom_components.bosch_shc_camera.local_rcp import rcp_read_remote_sync

        import requests as req
        with patch.object(req, "get", side_effect=req.exceptions.ConnectionError("refused")):
            result = rcp_read_remote_sync("proxy-20.live.cbs.boschsecurity.com:42090/abc123", "0x0c22", "T_WORD")
        assert result is None

    def test_success_delegates_to_parse(self):
        from custom_components.bosch_shc_camera.local_rcp import rcp_read_remote_sync

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<rcp><result><str>48 65 6c 6c 6f</str></result></rcp>"

        import requests as req
        with patch.object(req, "get", return_value=mock_resp):
            result = rcp_read_remote_sync("proxy-20.live.cbs.boschsecurity.com:42090/abc123", "0x0c22", "P_OCTET")
        assert result == b"\x48\x65\x6c\x6c\x6f"  # "Hello"
