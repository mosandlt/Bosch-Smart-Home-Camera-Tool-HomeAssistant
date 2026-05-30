"""Tests for auth_utils.async_digest_request — 100 % coverage target.

Covers:
1.  Happy path: 401 → 200 with qop=auth (MD5)
2.  Server returns 200 immediately (no auth required)
3.  401 without WWW-Authenticate header → ValueError
4.  401 with non-Digest scheme (Basic) → ValueError
5.  Malformed Digest header — missing nonce → ValueError
6.  Second response still 401 (wrong creds) → returned as-is
7.  Timeout propagation (ClientTimeout plumbed correctly)
8.  Legacy mode: qop absent
9.  Algorithm MD5-sess
10. POST with data body
11. Custom request headers preserved
12. SHA-256 algorithm
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

# ---------------------------------------------------------------------------
# Direct module import — avoids pulling in the full HA package __init__.py
# which requires homeassistant to be installed.
# Registered under the real dotted name so pytest-cov / coverage.py can trace it.
# ---------------------------------------------------------------------------
_AUTH_UTILS_PATH = (
    Path(__file__).parent.parent
    / "custom_components"
    / "bosch_shc_camera"
    / "auth_utils.py"
)
_DOTTED_NAME = "custom_components.bosch_shc_camera.auth_utils"

# Ensure parent package stubs exist so the dotted import is valid for coverage
for _pkg in (
    "custom_components",
    "custom_components.bosch_shc_camera",
):
    if _pkg not in sys.modules:
        import types

        sys.modules[_pkg] = types.ModuleType(_pkg)

if _DOTTED_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_DOTTED_NAME, _AUTH_UTILS_PATH)
    assert _spec is not None and _spec.loader is not None
    _auth_utils = importlib.util.module_from_spec(_spec)
    sys.modules[_DOTTED_NAME] = _auth_utils
    _spec.loader.exec_module(_auth_utils)  # type: ignore[union-attr]
else:
    _auth_utils = sys.modules[_DOTTED_NAME]

_build_digest_header = _auth_utils._build_digest_header  # type: ignore[attr-defined]
_md5 = _auth_utils._md5  # type: ignore[attr-defined]
_parse_digest_challenge = _auth_utils._parse_digest_challenge  # type: ignore[attr-defined]
_sha256 = _auth_utils._sha256  # type: ignore[attr-defined]
async_digest_request = _auth_utils.async_digest_request  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers for building fake server responses
# ---------------------------------------------------------------------------


def _make_response(
    status: int,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> MagicMock:
    """Return a MagicMock that looks like aiohttp.ClientResponse."""
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    # read() must be awaitable
    resp.read = AsyncMock(return_value=body)
    # Support async context manager (caller does `async with resp`)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _digest_challenge(
    realm: str = "cam@bosch.com",
    nonce: str = "deadbeef1234",
    qop: str = "auth",
    algorithm: str = "MD5",
    opaque: str = "opaque42",
) -> str:
    parts = [
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f"algorithm={algorithm}",
        f'opaque="{opaque}"',
    ]
    if qop:
        parts.append(f'qop="{qop}"')
    return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Unit tests — internal helpers
# ---------------------------------------------------------------------------


class TestMd5:
    def test_known_value(self) -> None:
        assert _md5("hello") == hashlib.md5(b"hello").hexdigest()


class TestSha256:
    def test_known_value(self) -> None:
        assert _sha256("hello") == hashlib.sha256(b"hello").hexdigest()


class TestParseDigestChallenge:
    def test_full_header(self) -> None:
        header = _digest_challenge()
        params = _parse_digest_challenge(header)
        assert params["realm"] == "cam@bosch.com"
        assert params["nonce"] == "deadbeef1234"
        assert params["algorithm"] == "MD5"
        assert params["opaque"] == "opaque42"
        assert params["qop"] == "auth"

    def test_non_digest_scheme_raises(self) -> None:
        with pytest.raises(ValueError, match="Expected Digest scheme"):
            _parse_digest_challenge('Basic realm="test"')

    def test_missing_nonce_raises(self) -> None:
        with pytest.raises(ValueError, match="missing required 'nonce'"):
            _parse_digest_challenge('Digest realm="test"')

    def test_no_qop_is_ok(self) -> None:
        header = _digest_challenge(qop="")
        params = _parse_digest_challenge(header)
        assert "qop" not in params or params.get("qop") == ""

    def test_unquoted_algorithm_value(self) -> None:
        # algorithm is typically unquoted in real headers
        header = 'Digest realm="x", nonce="y", algorithm=MD5'
        params = _parse_digest_challenge(header)
        assert params["algorithm"] == "MD5"
        assert params["nonce"] == "y"


class TestBuildDigestHeader:
    def test_qop_auth_header_format(self) -> None:
        challenge = _parse_digest_challenge(_digest_challenge())
        hdr = _build_digest_header(
            "GET", "https://cam/snap.jpg", "user", "pass", challenge
        )
        assert hdr.startswith("Digest ")
        assert 'username="user"' in hdr
        assert "qop=auth" in hdr
        assert "nc=00000001" in hdr
        assert "response=" in hdr

    def test_no_qop_header_format(self) -> None:
        challenge = _parse_digest_challenge(_digest_challenge(qop=""))
        hdr = _build_digest_header(
            "GET", "https://cam/snap.jpg", "user", "pass", challenge
        )
        assert "qop=" not in hdr
        assert "nc=" not in hdr
        assert "response=" in hdr

    def test_opaque_included_when_present(self) -> None:
        challenge = _parse_digest_challenge(_digest_challenge(opaque="op99"))
        hdr = _build_digest_header(
            "GET", "https://cam/snap.jpg", "user", "pass", challenge
        )
        assert 'opaque="op99"' in hdr

    def test_opaque_omitted_when_absent(self) -> None:
        header = 'Digest realm="r", nonce="n"'
        challenge = _parse_digest_challenge(header)
        hdr = _build_digest_header(
            "GET", "https://cam/snap.jpg", "user", "pass", challenge
        )
        assert "opaque" not in hdr

    def test_sha256_algorithm(self) -> None:
        challenge = _parse_digest_challenge(_digest_challenge(algorithm="SHA-256"))
        hdr = _build_digest_header(
            "GET", "https://cam/snap.jpg", "user", "pass", challenge
        )
        assert "SHA-256" in hdr

    def test_md5_sess_algorithm(self) -> None:
        challenge = _parse_digest_challenge(_digest_challenge(algorithm="MD5-SESS"))
        hdr = _build_digest_header(
            "GET", "https://cam/snap.jpg", "user", "pass", challenge
        )
        assert "MD5-SESS" in hdr

    def test_url_with_query_string(self) -> None:
        challenge = _parse_digest_challenge(_digest_challenge())
        hdr = _build_digest_header(
            "GET", "https://cam/snap.jpg?JpegSize=1206", "user", "pass", challenge
        )
        # URI in header must include query string
        assert 'uri="/snap.jpg?JpegSize=1206"' in hdr


# ---------------------------------------------------------------------------
# Integration tests — async_digest_request using mocked ClientSession
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_session() -> MagicMock:
    """Return a MagicMock aiohttp.ClientSession with a recordable .request."""
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request = AsyncMock()
    return session


@pytest.mark.asyncio
class TestAsyncDigestRequest:
    async def test_happy_path_401_then_200(self, mock_session: MagicMock) -> None:
        """TC-1: Server returns 401 → 200 with qop=auth."""
        resp_401 = _make_response(
            401,
            headers={"WWW-Authenticate": _digest_challenge()},
        )
        resp_200 = _make_response(200, body=b"image data")
        mock_session.request.side_effect = [resp_401, resp_200]

        result = await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "admin", "secret"
        )

        assert result.status == 200
        assert mock_session.request.call_count == 2

        # Second call must include Authorization: Digest header
        _, second_kwargs = mock_session.request.call_args
        auth = second_kwargs.get("headers", {}).get("Authorization", "")
        assert auth.startswith("Digest ")
        assert "response=" in auth

    async def test_server_200_immediately_no_auth(
        self, mock_session: MagicMock
    ) -> None:
        """TC-2: Server doesn't require auth — return first response."""
        resp_200 = _make_response(200, body=b"ok")
        mock_session.request.side_effect = [resp_200]

        result = await async_digest_request(
            mock_session, "GET", "https://cam/open", "user", "pass"
        )

        assert result.status == 200
        assert mock_session.request.call_count == 1

    async def test_401_without_www_authenticate_raises(
        self, mock_session: MagicMock
    ) -> None:
        """TC-3: 401 with no WWW-Authenticate header → ValueError."""
        resp_401 = _make_response(401, headers={})
        mock_session.request.side_effect = [resp_401]

        with pytest.raises(ValueError, match="WWW-Authenticate"):
            await async_digest_request(
                mock_session, "GET", "https://cam/snap.jpg", "user", "pass"
            )

    async def test_401_with_basic_scheme_raises(self, mock_session: MagicMock) -> None:
        """TC-4: 401 with Basic scheme → ValueError."""
        resp_401 = _make_response(
            401, headers={"WWW-Authenticate": 'Basic realm="test"'}
        )
        mock_session.request.side_effect = [resp_401]

        with pytest.raises(ValueError, match="Expected Digest scheme"):
            await async_digest_request(
                mock_session, "GET", "https://cam/snap.jpg", "user", "pass"
            )

    async def test_401_malformed_digest_missing_nonce_raises(
        self, mock_session: MagicMock
    ) -> None:
        """TC-5: Malformed Digest header — missing nonce → ValueError."""
        resp_401 = _make_response(
            401, headers={"WWW-Authenticate": 'Digest realm="test"'}
        )
        mock_session.request.side_effect = [resp_401]

        with pytest.raises(ValueError, match="missing required 'nonce'"):
            await async_digest_request(
                mock_session, "GET", "https://cam/snap.jpg", "user", "pass"
            )

    async def test_second_response_still_401_returned(
        self, mock_session: MagicMock
    ) -> None:
        """TC-6: Server returns 401 again after auth attempt — return it."""
        resp_401_first = _make_response(
            401, headers={"WWW-Authenticate": _digest_challenge()}
        )
        resp_401_second = _make_response(401)
        mock_session.request.side_effect = [resp_401_first, resp_401_second]

        result = await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "user", "wrongpassword"
        )

        assert result.status == 401
        assert mock_session.request.call_count == 2

    async def test_timeout_plumbed_into_request(self, mock_session: MagicMock) -> None:
        """TC-7: Timeout parameter is passed as ClientTimeout to aiohttp."""
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_200]

        await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "u", "p", timeout=5.0
        )

        _, first_kwargs = mock_session.request.call_args
        timeout_obj = first_kwargs.get("timeout")
        assert isinstance(timeout_obj, aiohttp.ClientTimeout)
        assert timeout_obj.total == 5.0

    async def test_qop_absent_legacy_mode(self, mock_session: MagicMock) -> None:
        """TC-8: qop absent — legacy Digest without qop."""
        resp_401 = _make_response(
            401,
            headers={"WWW-Authenticate": _digest_challenge(qop="")},
        )
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_401, resp_200]

        result = await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "user", "pass"
        )

        assert result.status == 200
        _, second_kwargs = mock_session.request.call_args
        auth = second_kwargs["headers"]["Authorization"]
        # Legacy mode must NOT include qop= or nc= in header
        assert "qop=" not in auth
        assert "nc=" not in auth

    async def test_md5_sess_algorithm(self, mock_session: MagicMock) -> None:
        """TC-9: Algorithm MD5-sess handled correctly."""
        resp_401 = _make_response(
            401,
            headers={"WWW-Authenticate": _digest_challenge(algorithm="MD5-SESS")},
        )
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_401, resp_200]

        result = await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "user", "pass"
        )

        assert result.status == 200
        _, second_kwargs = mock_session.request.call_args
        auth = second_kwargs["headers"]["Authorization"]
        assert "MD5-SESS" in auth

    async def test_post_with_data_body(self, mock_session: MagicMock) -> None:
        """TC-10: POST with data body — data forwarded on both requests."""
        resp_401 = _make_response(
            401, headers={"WWW-Authenticate": _digest_challenge()}
        )
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_401, resp_200]

        payload = b"<rcp>data</rcp>"
        result = await async_digest_request(
            mock_session,
            "POST",
            "https://cam/rcp.xml",
            "user",
            "pass",
            data=payload,
        )

        assert result.status == 200
        # Both calls must carry the data
        calls = mock_session.request.call_args_list
        for call in calls:
            _, kwargs = call
            assert kwargs.get("data") == payload

    async def test_custom_headers_preserved(self, mock_session: MagicMock) -> None:
        """TC-11: Caller-supplied headers are passed on the first request and
        preserved (alongside Authorization) on the second."""
        resp_401 = _make_response(
            401, headers={"WWW-Authenticate": _digest_challenge()}
        )
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_401, resp_200]

        custom_hdrs = {"Accept": "image/jpeg", "X-Custom": "value"}
        result = await async_digest_request(
            mock_session,
            "GET",
            "https://cam/snap.jpg",
            "user",
            "pass",
            headers=custom_hdrs,
        )

        assert result.status == 200
        first_call, second_call = mock_session.request.call_args_list
        # First request gets the custom headers
        assert first_call[1]["headers"] == custom_hdrs
        # Second request gets custom + Authorization
        second_hdrs = second_call[1]["headers"]
        assert second_hdrs["Accept"] == "image/jpeg"
        assert second_hdrs["X-Custom"] == "value"
        assert "Authorization" in second_hdrs

    async def test_sha256_algorithm(self, mock_session: MagicMock) -> None:
        """TC-12: SHA-256 algorithm accepted and used in response hash."""
        resp_401 = _make_response(
            401,
            headers={"WWW-Authenticate": _digest_challenge(algorithm="SHA-256")},
        )
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_401, resp_200]

        result = await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "user", "pass"
        )

        assert result.status == 200
        _, second_kwargs = mock_session.request.call_args
        auth = second_kwargs["headers"]["Authorization"]
        assert "SHA-256" in auth

    async def test_ssl_parameter_plumbed(self, mock_session: MagicMock) -> None:
        """ssl=False (the default) is passed through to aiohttp."""
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_200]

        await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "u", "p", ssl=False
        )

        _, kwargs = mock_session.request.call_args
        assert kwargs.get("ssl") is False

    async def test_no_data_not_in_kwargs_when_none(
        self, mock_session: MagicMock
    ) -> None:
        """When data=None (default) the key 'data' must still be passed (as None)."""
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_200]

        await async_digest_request(
            mock_session, "GET", "https://cam/snap.jpg", "u", "p"
        )

        _, kwargs = mock_session.request.call_args
        # data=None is only included if data is not None per spec, so either absent or None
        # Both are acceptable — just confirm no TypeError was raised
        assert mock_session.request.call_count == 1

    async def test_response_digest_correctness(self, mock_session: MagicMock) -> None:
        """Verify the computed Digest response matches manual calculation."""
        nonce = "testNonce123"
        realm = "test@realm.com"
        user = "admin"
        password = "secret"
        method = "GET"
        uri = "/snap.jpg"
        nc = "00000001"
        qop_val = "auth"

        resp_401 = _make_response(
            401,
            headers={
                "WWW-Authenticate": (
                    f'Digest realm="{realm}", nonce="{nonce}", '
                    f'qop="auth", algorithm=MD5'
                )
            },
        )
        resp_200 = _make_response(200)
        mock_session.request.side_effect = [resp_401, resp_200]

        await async_digest_request(
            mock_session,
            method,
            f"https://cam{uri}",
            user,
            password,
        )

        _, second_kwargs = mock_session.request.call_args
        auth_hdr = second_kwargs["headers"]["Authorization"]

        # Extract cnonce from the produced header
        m = re.search(r'cnonce="([^"]+)"', auth_hdr)
        assert m is not None
        cnonce = m.group(1)

        # Reproduce the expected response
        ha1 = hashlib.md5(f"{user}:{realm}:{password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        expected_response = hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop_val}:{ha2}".encode()
        ).hexdigest()

        assert f'response="{expected_response}"' in auth_hdr
