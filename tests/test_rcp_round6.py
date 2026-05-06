"""rcp.py — Sprint-A round-6 tests.

Covers the 42% gap for the three most accessible function bodies:
  - rcp_session (70-144): cloud proxy RCP handshake
  - rcp_local_read (168-204): direct LAN RCP GET
  - rcp_local_write (219-248): direct LAN RCP WRITE
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


MODULE = "custom_components.bosch_shc_camera.rcp"

PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"
CAM_IP = "192.0.2.149"


def _mock_resp(status: int, text: str = "", body: bytes = b""):
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.read = AsyncMock(return_value=body or text.encode())
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_session(*responses):
    """Return a mock aiohttp.ClientSession that yields responses in order."""
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.get = MagicMock(side_effect=list(responses))
    return session


# ── rcp_session ───────────────────────────────────────────────────────────────


class TestRcpSession:
    """All branches of rcp_session (lines 70-144)."""

    @pytest.mark.asyncio
    async def test_success_returns_session_id(self):
        """Happy path: step1 returns <sessionid>, step2 ACKs → returns session_id."""
        from custom_components.bosch_shc_camera.rcp import rcp_session
        step1 = _mock_resp(200, text="<sessionid>0x12345678</sessionid>")
        step2 = _mock_resp(200, text="<result>OK</result>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1, step2)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await rcp_session({}, PROXY_HOST, PROXY_HASH)
        assert result == "0x12345678"

    @pytest.mark.asyncio
    async def test_step1_non200_returns_none(self):
        """HTTP 403 on step1 → returns None."""
        from custom_components.bosch_shc_camera.rcp import rcp_session
        step1 = _mock_resp(403)
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await rcp_session({}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step1_timeout_returns_none(self):
        import aiohttp
        from custom_components.bosch_shc_camera.rcp import rcp_session
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=None)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get.return_value = cm
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await rcp_session({}, PROXY_HOST, PROXY_HASH)
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
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await rcp_session({}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_sessionid_in_response_returns_none(self):
        """Step1 200 but no <sessionid> in body → returns None."""
        from custom_components.bosch_shc_camera.rcp import rcp_session
        step1 = _mock_resp(200, text="<result>ok</result>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await rcp_session({}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_invalid_session_id_0x00000000_returns_none(self):
        """Proxy rejection indicated by sessionid=0x00000000 → returns None."""
        from custom_components.bosch_shc_camera.rcp import rcp_session
        step1 = _mock_resp(200, text="<sessionid>0x00000000</sessionid>")
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await rcp_session({}, PROXY_HOST, PROXY_HASH)
        assert result is None

    @pytest.mark.asyncio
    async def test_step2_timeout_still_returns_session_id(self):
        """ACK (step2) timeout is non-fatal — session_id already extracted, return it."""
        import aiohttp
        from custom_components.bosch_shc_camera.rcp import rcp_session
        step1 = _mock_resp(200, text="<sessionid>0xABCDEF01</sessionid>")
        step2_cm = MagicMock()
        step2_cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        step2_cm.__aexit__ = AsyncMock(return_value=None)
        connector_mock = MagicMock()
        connector_mock.close = AsyncMock()
        session = _make_session(step1, step2_cm)
        with patch(f"{MODULE}.aiohttp.TCPConnector", return_value=connector_mock), \
             patch(f"{MODULE}.aiohttp.ClientSession", return_value=session):
            result = await rcp_session({}, PROXY_HOST, PROXY_HASH)
        # step2 timeout is caught — should still return the session_id
        assert result == "0xABCDEF01"


# ── rcp_local_read ────────────────────────────────────────────────────────────


class TestRcpLocalRead:
    """All branches of rcp_local_read (lines 168-204)."""

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
        import aiohttp
        from custom_components.bosch_shc_camera.rcp import rcp_local_read
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
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
        """When `num` > 0, params should include the 'num' key (line 175)."""
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


# ── rcp_local_write ───────────────────────────────────────────────────────────


class TestRcpLocalWrite:
    """All branches of rcp_local_write (lines 219-248)."""

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
        """Payloads without '0x' prefix get it added."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write
        resp_cm = _mock_resp(200, body=b"ok")
        session = MagicMock()
        session.get.return_value = resp_cm
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            await rcp_local_write(MagicMock(), CAM_IP, "0x0c22", "deadbeef")
        _, call_kwargs = session.get.call_args
        assert call_kwargs["params"]["payload"].startswith("0x")

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
        cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
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
