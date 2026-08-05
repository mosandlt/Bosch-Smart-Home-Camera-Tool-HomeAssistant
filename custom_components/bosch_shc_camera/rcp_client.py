"""RCP (Remote Configuration Protocol) session + read primitives.

Covers the cloud-proxy RCP session handshake/cache, the low-level READ
command, the camera's own LAN RCP endpoint, and the SSRF-guarding URL
validators the proxy path needs. This is the coordinator-internal
orchestration layer — the actual wire protocol (opcodes, session
handshake bytes) mirrors what `bosch_shc_camera_client.rcp` implements
for the higher-level `fetch_rcp_camera_data` cache-population path
(see `rcp.py`); this module exists for the lower-level per-command
reads `camera.py` and `rcp_diagnostics.py` need directly (RCP 0x099e
thumbnail probe, ONVIF/RCP-version LAN diagnostics) that don't go
through that bulk fetch.

Free functions taking the coordinator instance as their first argument
— matches the `quality_prefs`/`tick_bootstrap`/`tick_failure` pattern
already established here. `BoschCameraCoordinator` keeps a thin
delegating method for each of these (same name/signature, calls
straight into the matching function here) so every existing call site
— `camera.py`'s RCP 0x099e/0x0c98 probes, `rcp_diagnostics.py`,
the coordinator's own internal call sites — keeps working unchanged,
and so do the test suite's instance-attribute-patching patterns
(`coord._rcp_session = AsyncMock()`, `coord.get_cached_rcp_session =
AsyncMock()`, `coord._fetch_rcp_lan = ...`) and unbound-method-call
patterns (`BoschCameraCoordinator._proxy_hash_from_rcp_base(url)`,
`BoschCameraCoordinator._invalidate_rcp_session(coord, ...)`).

IMPORTANT: where one of these functions originally called another
extracted method on `self` (e.g. `rcp_read` calling `self._rcp_session`
via `get_cached_rcp_session`, or `rcp_read` calling
`self._proxy_hash_from_rcp_base`/`self._invalidate_rcp_session`), the
extracted version keeps calling through the COORDINATOR instance
(`coordinator.method_name(...)`) rather than the raw module-level
function directly — those methods are public/overridable coordinator
methods that tests patch per-instance, and calling the module function
directly would silently bypass any such patch.
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote as _unquote
from urllib.parse import urlencode, urlparse

import aiohttp

from .cloud_ssl import async_bosch_cloud_session_cm

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

# ── URL allowlist for the RCP cloud proxy (SSRF prevention) ────────────────
_SAFE_DOMAINS = frozenset({".boschsecurity.com", ".bosch.com"})


def _is_safe_bosch_host(host_and_port: str) -> bool:
    """Validate a bare ``host[:port]`` string (no scheme) against the Bosch allowlist.

    Used for the RCP proxy host/hash pair Bosch's cloud PUT /connection
    response hands back (e.g. "proxy-01.live.cbs.boschsecurity.com:42090")
    before it is used to build a request URL for the RCP client library —
    an unvalidated value here is an SSRF path. Parsed via ``urlparse`` (not a
    naive ``rsplit(":", 1)``) so this extracts the same authority a real
    HTTP client would connect to — a value like
    "proxy.boschsecurity.com:443@attacker.example" splits to an
    allowlisted-looking "proxy.boschsecurity.com" on the last colon, but an
    HTTP client parses it as userinfo and connects to attacker.example.
    Any "@" is rejected outright (a legitimate Bosch value never has one;
    aiohttp would otherwise turn userinfo into a Basic-Auth header sent to
    Bosch's real proxy), and a ``ValueError`` from ``urlparse`` on malformed
    input fails closed rather than propagating past callers' narrower
    exception handlers.
    """
    if "@" in host_and_port:
        return False
    try:
        hostname = urlparse(f"https://{host_and_port}").hostname
    except ValueError:
        return False
    return hostname is not None and any(hostname.endswith(d) for d in _SAFE_DOMAINS)


def _parse_safe_rcp_proxy_url(url_entry: str, cam_id: str) -> tuple[str, str] | None:
    """Split a Bosch ``urls[0]`` proxy entry into ``(host, hash)``, validated.

    Returns None (logging a warning) for a malformed entry or one whose host
    fails `_is_safe_bosch_host` — never hands back an unvalidated host to a
    caller that will use it to build a request URL.
    """
    parts = url_entry.split("/", 1)
    if len(parts) != 2 or not _is_safe_bosch_host(parts[0]):
        _LOGGER.warning(
            "Rejected unsafe/malformed RCP proxy entry for %s: %s",
            cam_id,
            url_entry[:60],
        )
        return None
    return parts[0], parts[1]


def _parse_onvif_scopes(raw: bytes) -> dict[str, Any]:
    """Parse ONVIF scope TLV payload from RCP 0x0a98 (ASCII, ~720 bytes).

    The payload is a series of null-terminated ASCII strings, each of which
    may be an ONVIF scope URI of the form:
        onvif://www.onvif.org/name/Bosch%20Smart%20Home%20Camera
        onvif://www.onvif.org/hardware/HOME_Eyes_Outdoor
        onvif://www.onvif.org/Profile/Streaming

    Returns a dict with parsed fields and the raw scope list:
        {
            "raw_scopes": [...],
            "name": "Bosch Smart Home Camera",
            "hardware": "HOME_Eyes_Outdoor",
            "profiles": ["Streaming", ...],
            "supported": True,
        }

    Returns {"supported": True, "raw_scopes": [], "name": "", "hardware": "", "profiles": []}
    on parse error (non-None raw means camera answered, so ONVIF is supported).
    """
    result: dict[str, Any] = {
        "supported": True,
        "raw_scopes": [],
        "name": "",
        "hardware": "",
        "profiles": [],
    }
    try:
        # Null-terminated or newline-separated ASCII strings
        text = raw.decode("ascii", errors="replace")
        # Split on null bytes, newlines, or whitespace runs
        scopes = [s.strip() for s in _re.split(r"[\x00\n\r]+", text) if s.strip()]
        result["raw_scopes"] = scopes
        for scope in scopes:
            if not scope.startswith("onvif://www.onvif.org/"):
                continue
            path = scope[len("onvif://www.onvif.org/") :]
            if "/" not in path:
                continue
            key, _sep, val = path.partition("/")
            val_decoded = _unquote(val).replace("+", " ")
            if key == "name":
                result["name"] = val_decoded
            elif key == "hardware":
                result["hardware"] = val_decoded
            elif key == "Profile":
                profiles: list[str] = result["profiles"]
                profiles.append(val_decoded)
    except Exception:  # noqa: S110 # pragma: no cover — defensive parse of raw camera bytes; partial result still returned
        pass
    return result


def _invalidate_rcp_session(
    coordinator: BoschCameraCoordinator, proxy_hash: str
) -> None:
    """Drop a cached RCP session so the next call reopens the handshake.

    Call this when a downstream RCP read returns HTTP 401 (auth dropped),
    HTTP 403 (session expired), or RCP error 0x0c0d (session closed).
    Without invalidation the cache would keep serving the dead ID for
    its full 5-min TTL — readers would see None until the entry expired.
    """
    if coordinator.rcp_session_cache.pop(proxy_hash, None) is not None:
        _LOGGER.debug("RCP session cache invalidated for %s", proxy_hash[:8])


async def get_cached_rcp_session(
    coordinator: BoschCameraCoordinator, proxy_host: str, proxy_hash: str
) -> str | None:
    """Return a cached RCP session ID, opening a new one if missing or expired.

    Caches valid session IDs for 5 minutes (TTL 300 s) to avoid the 2-step
    RCP handshake (0xff0c + 0xff0d) on every thumbnail or data fetch.

    Serialized per proxy_hash via `_get_rcp_session_lock` — Bosch's proxy
    only tolerates one live session per proxy_hash, so two callers racing
    an empty/expired cache would otherwise each open their own session and
    one gets rejected (sessionid 0x00000000).
    """
    async with coordinator._get_rcp_session_lock(proxy_hash):
        now = time.monotonic()
        cached = coordinator.rcp_session_cache.get(proxy_hash)
        if cached:
            session_id, expires_at = cached
            if now < expires_at:
                return session_id
            del coordinator.rcp_session_cache[proxy_hash]

        new_session_id: str | None = await coordinator._rcp_session(
            proxy_host, proxy_hash
        )
        if new_session_id:
            coordinator.rcp_session_cache[proxy_hash] = (
                new_session_id,
                now + 300.0,
            )  # 5-min TTL
        return new_session_id


async def _rcp_session(
    coordinator: BoschCameraCoordinator, proxy_host: str, proxy_hash: str
) -> str | None:
    """Open an RCP session via the cloud proxy and return the sessionid, or None on failure.

    The RCP handshake consists of two steps:
      1. WRITE command 0xff0c with a fixed payload → extract <sessionid> from XML response
      2. WRITE command 0xff0d with the sessionid → ACK (confirms the session)

    Auth=3 (anonymous via URL hash) provides read-only access.
    The proxy_host should be in the form "proxy-NN.live.cbs.boschsecurity.com:42090".

    Uses the shared, HA-lifecycle-managed Bosch-cloud session
    (`async_bosch_cloud_session_cm` / `cloud_ssl.py`) instead of opening a
    fresh ``aiohttp.ClientSession`` + ``TCPConnector`` per call — this
    proxy host is the exact same Bosch-pinned-CA TLS trust domain
    (`async_get_bosch_cloud_ssl_context`) the shared session already
    uses, so there is no separate trust reason to keep a private
    connector here.
    """
    base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"
    init_payload = "0x0102004000000000040000000000000000010000000000000001000000000000"

    async with async_bosch_cloud_session_cm(coordinator.hass) as session:
        # Step 1: open session
        params1 = {
            "command": "0xff0c",
            "direction": "WRITE",
            "type": "P_OCTET",
            "payload": init_payload,
        }
        try:
            async with asyncio.timeout(8):
                async with session.get(base, params=params1) as resp:
                    if resp.status != 200:
                        _LOGGER.debug(
                            "_rcp_session: step1 HTTP %d for %s",
                            resp.status,
                            proxy_host,
                        )
                        return None
                    text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("_rcp_session: step1 error for %s: %s", proxy_host, err)
            return None

        # Parse <sessionid> from XML response
        m = _re.search(r"<sessionid>(\S+)</sessionid>", text, _re.IGNORECASE)
        if not m:
            _LOGGER.debug(
                "_rcp_session: no <sessionid> in response for %s: %s",
                proxy_host,
                text[:200],
            )
            return None
        session_id = m.group(1)

        # Step 2: ACK the session
        params2 = {
            "command": "0xff0d",
            "direction": "WRITE",
            "type": "P_OCTET",
            "sessionid": session_id,
        }
        try:
            async with asyncio.timeout(8):
                async with session.get(base, params=params2) as resp2:
                    _LOGGER.debug(
                        "_rcp_session: ACK HTTP %d for %s (sessionid=%s)",
                        resp2.status,
                        proxy_host,
                        session_id,
                    )
        except (TimeoutError, aiohttp.ClientError) as err:
            _LOGGER.debug("_rcp_session: step2 error for %s: %s", proxy_host, err)
            # Session may still be valid — return it anyway

        return session_id


def _proxy_hash_from_rcp_base(rcp_base: str) -> str | None:
    """Extract proxy_hash from `https://host:port/{hash}/rcp.xml`."""
    parts = rcp_base.rstrip("/").split("/")
    if len(parts) >= 2 and parts[-1] == "rcp.xml":
        return parts[-2]
    return None


async def rcp_read(
    coordinator: BoschCameraCoordinator,
    rcp_base: str,
    command: str,
    sessionid: str,
    type_: str = "P_OCTET",
    num: int = 0,
) -> bytes | None:
    """READ an RCP command and return the raw payload bytes, or None on failure.

    Uses the HA shared session to avoid creating a new
    connector+session per RCP command (prevents socket exhaustion).
    Invalidates the session cache on HTTP 401/403 or RCP <err>0x0c0d</err>
    (session closed) — the dead ID would otherwise block reads until TTL.
    """
    # Local import (not top-level): keeps unittest.mock.patch(
    # "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
    # ...) working the same way it did before this moved out of
    # coordinator.py — those patches target the package's own namespace,
    # matching the pattern already used in live_connection.py.
    from . import (
        async_get_bosch_cloud_session as async_get_bosch_cloud_session,
    )

    params: dict[str, str] = {
        "command": command,
        "direction": "READ",
        "type": type_,
        "sessionid": sessionid,
    }
    if num:
        params["num"] = str(num)

    session = await async_get_bosch_cloud_session(coordinator.hass)
    try:
        async with asyncio.timeout(8):
            async with session.get(rcp_base, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.debug("rcp_read: command=%s HTTP %d", command, resp.status)
                    if resp.status in (401, 403):
                        proxy_hash = coordinator._proxy_hash_from_rcp_base(rcp_base)
                        if proxy_hash:
                            coordinator._invalidate_rcp_session(proxy_hash)
                    return None
                raw = await resp.read()
                # RCP session-closed response: <err>0x0c0d</err>. Drop the
                # cached session so the next read reopens the handshake.
                if b"0x0c0d" in raw and b"<err>" in raw:
                    proxy_hash = coordinator._proxy_hash_from_rcp_base(rcp_base)
                    if proxy_hash:
                        coordinator._invalidate_rcp_session(proxy_hash)
                    return None
                return bytes(raw)
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("rcp_read: command=%s error: %s", command, err)
        return None


async def _fetch_rcp_lan(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    opcode_hex: str,
) -> bytes | None:
    """Read an RCP value directly from the camera's LAN HTTPS endpoint (cbs Digest auth).

    Uses the cached LOCAL session credentials (``local_creds_cache``) which
    are populated on every successful PUT /connection LOCAL. The camera's
    ``rcp.xml`` endpoint on port 443 requires HTTP Digest auth with the
    rotating cbs-XXXXXXXX user/password pair.

    Returns the decoded payload bytes on success, None on any error
    (no LAN IP, no creds, network error, auth failure, RCP error).

    IMPORTANT: Do NOT call this from the event loop for opcodes that would
    rotate cbs creds (i.e. never issue PUT /connection LOCAL here — use
    the existing slow-tier RCP proxy path for writes). This helper is
    READ-ONLY and purely supplementary to the cloud-proxy path.
    """
    # Local import (not top-level): keeps unittest.mock.patch(
    # "custom_components.bosch_shc_camera.async_get_clientsession", ...)
    # working the same way it did before this moved out of coordinator.py —
    # matches the live_connection.py pattern.
    from . import (
        async_digest_request as async_digest_request,
    )
    from . import (
        async_get_clientsession as async_get_clientsession,
    )

    if coordinator._is_rcp_lan_denied(cam_id, opcode_hex):
        return None
    ip = coordinator.get_cam_lan_ip(cam_id)
    if not ip:
        return None
    creds = coordinator.local_creds_cache.get(cam_id)
    if not creds:
        return None
    user: str = creds.get("user", "")
    password: str = creds.get("password", "")
    if not (user and password):
        return None
    port: int = creds.get("port", 443)
    base = f"https://{ip}:{port}/rcp.xml"
    params: dict[str, str] = {
        "command": opcode_hex,
        "direction": "READ",
        "type": "P_OCTET",
        "num": "1",
    }
    url = f"{base}?{urlencode(params)}"
    try:
        async with await async_digest_request(
            async_get_clientsession(coordinator.hass, verify_ssl=False),
            "GET",
            url,
            user,
            password,
            timeout=8.0,
            ssl=False,
        ) as resp:
            if resp.status != 200:
                _LOGGER.debug(
                    "_fetch_rcp_lan: %s@%s HTTP %d", opcode_hex, ip, resp.status
                )
                if resp.status == 401:
                    # CBS user lacks permission for this opcode — stop hammering
                    # the camera every 5 min. Retry once the TTL expires.
                    coordinator._mark_rcp_lan_denied(cam_id, opcode_hex)
                return None
            coordinator._clear_rcp_lan_denied(cam_id, opcode_hex)
            raw = await resp.read()
            # Check for RCP-level error
            if b"<err>" in raw.lower():
                _LOGGER.debug(
                    "_fetch_rcp_lan: %s@%s RCP error: %s", opcode_hex, ip, raw[:120]
                )
                return None
            # Extract payload from <str>HEXDATA</str>
            m = _re.search(rb"<str>([0-9a-fA-F]+)</str>", raw, _re.IGNORECASE)
            if m:
                return bytes.fromhex(m.group(1).decode("ascii"))
            # Fallback: raw bytes if not XML envelope
            if raw and not raw.lstrip(b"\n\r\t ").startswith(b"<"):
                return bytes(raw)
            return None
    except (TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("_fetch_rcp_lan: %s@%s %s", opcode_hex, ip, err)
        return None
    except Exception as err:  # pragma: no cover
        _LOGGER.debug("_fetch_rcp_lan: %s@%s unexpected: %s", opcode_hex, ip, err)
        return None
