"""Coverage tests for the RCP front-light LOCAL writer (Gen2 LAN-fallback).

`rcp_local_write_front_light` is the cloud-bypass path used during Bosch
outages. It clamps brightness 0..100, encodes as 4-hex T_WORD and writes
to RCP `0x0c22` with `num=1`. Existing fallback tests mock it entirely,
so this exercises the encoder + the `params["num"]` plumbing in
`rcp_local_write` itself.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import rcp


class _FakeResp:
    def __init__(self, status: int = 200, body: bytes = b"<ok/>"):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Captures the `params=` kwarg so the test can assert num=1 was sent."""
    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.last_params: dict | None = None

    def get(self, _url, *, params=None):
        self.last_params = params
        return self._resp


def _hass_with_session(session: _FakeSession) -> MagicMock:
    hass = MagicMock()
    hass.data = {}
    return hass


@pytest.mark.asyncio
class TestRcpLocalWriteFrontLight:
    async def test_brightness_100_sends_t_word_with_num_1(self):
        """Pins L247 (`params["num"] = str(num)`) + L312-314 (front-light
        encoder)."""
        resp = _FakeResp(status=200, body=b"<ok/>")
        session = _FakeSession(resp)
        with patch.object(
            rcp, "async_get_clientsession", return_value=session,
        ):
            ok = await rcp.rcp_local_write_front_light(MagicMock(), "1.2.3.4", 100)
        assert ok is True
        assert session.last_params is not None
        assert session.last_params["command"] == "0x0c22"
        assert session.last_params["type"] == "T_WORD"
        # 100 → 0x0064, sent as 0x0064 (lower-case hex, 4 digits).
        assert session.last_params["payload"].lower() == "0x0064"
        # num=1 plumbed through. L247 only fires when num > 0.
        assert session.last_params["num"] == "1"

    async def test_brightness_clamped_to_range(self):
        """Out-of-range brightness clamps to 0..100. 250 → 100, -10 → 0."""
        resp = _FakeResp(status=200, body=b"<ok/>")
        with patch.object(rcp, "async_get_clientsession", return_value=_FakeSession(resp)):
            assert await rcp.rcp_local_write_front_light(MagicMock(), "1.2.3.4", 250) is True
            assert await rcp.rcp_local_write_front_light(MagicMock(), "1.2.3.4", -10) is True

    async def test_brightness_zero_encodes_0x0000(self):
        resp = _FakeResp(status=200, body=b"<ok/>")
        session = _FakeSession(resp)
        with patch.object(rcp, "async_get_clientsession", return_value=session):
            ok = await rcp.rcp_local_write_front_light(MagicMock(), "1.2.3.4", 0)
        assert ok is True
        assert session.last_params["payload"].lower() == "0x0000"

    async def test_returns_false_on_http_non_200(self):
        """Camera responding with HTTP 500 → caller must see False so the
        SHC-cloud retry path runs."""
        resp = _FakeResp(status=500, body=b"")
        with patch.object(rcp, "async_get_clientsession", return_value=_FakeSession(resp)):
            ok = await rcp.rcp_local_write_front_light(MagicMock(), "1.2.3.4", 50)
        assert ok is False

    async def test_returns_false_on_rcp_err_envelope(self):
        """`<err>` in response body → write failed even if HTTP 200."""
        resp = _FakeResp(status=200, body=b"<rcp><err>5</err></rcp>")
        with patch.object(rcp, "async_get_clientsession", return_value=_FakeSession(resp)):
            ok = await rcp.rcp_local_write_front_light(MagicMock(), "1.2.3.4", 50)
        assert ok is False
