"""Async HTTP Digest authentication helper (RFC 7616 / 2617).

Uses only aiohttp + stdlib — no `requests` dependency.
Implements MD5 and MD5-sess; SHA-256 is accepted if Bosch ever upgrades.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _md5(data: str) -> str:
    # usedforsecurity=False: MD5 is protocol-mandated by HTTP Digest (RFC 7616),
    # not used as a cryptographic security primitive.  Required on FIPS systems
    # (Python 3.9+) which otherwise reject MD5 outright.
    return hashlib.md5(data.encode(), usedforsecurity=False).hexdigest()


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode()).hexdigest()


def _parse_digest_challenge(www_auth: str) -> dict[str, str]:
    """Parse a WWW-Authenticate: Digest header into a dict of directives.

    Raises ValueError if the header is not a Digest challenge or if the
    required `nonce` directive is absent.
    """
    scheme, _, params_str = www_auth.partition(" ")
    if scheme.strip().lower() != "digest":
        raise ValueError(f"Expected Digest scheme, got: {scheme!r}")

    # Match key=value or key="value" pairs
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([\w./+=-]+))', params_str):
        key = m.group(1).lower()
        value = m.group(2) if m.group(2) is not None else m.group(3)
        params[key] = value

    if "nonce" not in params:
        raise ValueError("Digest challenge missing required 'nonce' directive")

    return params


def _build_digest_header(
    method: str,
    url: str,
    user: str,
    password: str,
    challenge: dict[str, str],
) -> str:
    """Compute and return the full Authorization: Digest header value."""
    realm = challenge.get("realm", "")
    nonce = challenge["nonce"]
    opaque = challenge.get("opaque", "")
    qop = challenge.get("qop", "")
    algorithm = challenge.get("algorithm", "MD5").upper()

    # Strip query string for the digest URI
    uri = url.split("?")[0] if "?" in url else url
    # Use full URL path portion for the URI field
    # RFC 7616 §3.4: digest-uri = request-uri
    from urllib.parse import urlparse

    parsed = urlparse(url)
    uri = parsed.path
    if parsed.query:
        uri = f"{uri}?{parsed.query}"

    # Select hash function
    if algorithm.startswith("SHA-256"):
        _hash = _sha256
    else:
        _hash = _md5

    # HA1
    ha1 = _hash(f"{user}:{realm}:{password}")
    if algorithm in ("MD5-SESS", "SHA-256-SESS"):
        cnonce = secrets.token_hex(8)
        ha1 = _hash(f"{ha1}:{nonce}:{cnonce}")
    else:
        cnonce = secrets.token_hex(8)

    # HA2
    ha2 = _hash(f"{method.upper()}:{uri}")

    # Response
    nc = "00000001"
    qop_value = qop.split(",")[0].strip().lower() if qop else ""
    if qop_value == "auth":
        response = _hash(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop_value}:{ha2}")
    else:
        # Legacy: no qop
        response = _hash(f"{ha1}:{nonce}:{ha2}")

    # Build header
    parts = [
        f'username="{user}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{uri}"',
        f"algorithm={algorithm}",
        f'response="{response}"',
        f'cnonce="{cnonce}"',
    ]
    if qop_value == "auth":
        parts.append(f"qop={qop_value}")
        parts.append(f"nc={nc}")
    if opaque:
        parts.append(f'opaque="{opaque}"')

    return "Digest " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def async_digest_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    user: str,
    password: str,
    *,
    timeout: float = 10.0,
    ssl: bool | aiohttp.Fingerprint = False,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> aiohttp.ClientResponse:
    """Perform an HTTP request with Digest authentication.

    Sends an initial request, expects a 401 with WWW-Authenticate: Digest,
    parses the challenge, computes the response, and retries with Authorization.

    If the server returns 200 on the first attempt (no auth required), that
    response is returned directly.

    Returns the second response (authenticated). The response is NOT consumed —
    caller must read `.read()` / `.text()` / `.json()` and close it.

    Usage::

        async with await async_digest_request(session, "GET", url, u, p) as resp:
            data = await resp.read()

    Raises:
        ValueError: If the 401 response has no WWW-Authenticate header, uses a
            non-Digest scheme, or is missing required Digest directives.
        aiohttp.ClientError: On network-level errors.
        asyncio.TimeoutError: If the request exceeds *timeout* seconds.
    """
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    req_kwargs: dict[str, Any] = {
        "ssl": ssl,
        "timeout": client_timeout,
    }
    if data is not None:
        req_kwargs["data"] = data
    if headers:
        req_kwargs["headers"] = headers

    # First attempt — no Authorization
    first_resp = await session.request(method, url, **req_kwargs)

    if first_resp.status != 401:
        # Server accepted without auth (or returned an unrecoverable error)
        return first_resp

    # Must consume body before the connection can be reused
    await first_resp.read()

    www_auth = first_resp.headers.get("WWW-Authenticate", "")
    if not www_auth:
        raise ValueError(
            f"Server returned 401 without WWW-Authenticate header for {url!r}"
        )

    challenge = _parse_digest_challenge(www_auth)
    auth_header = _build_digest_header(method, url, user, password, challenge)

    auth_headers: dict[str, str] = dict(headers) if headers else {}
    auth_headers["Authorization"] = auth_header

    req_kwargs["headers"] = auth_headers

    # Second attempt — with Digest Authorization
    return await session.request(method, url, **req_kwargs)
