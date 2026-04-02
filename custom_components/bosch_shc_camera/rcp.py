"""Bosch RCP (Remote Configuration Protocol) via cloud proxy.

Standalone functions extracted from the coordinator for RCP session management,
binary protocol reads, and camera data fetching (dimmer, privacy mask, clock
offset, LAN IP, product name, bitrate ladder).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re as _re
import struct
import time
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Type alias for the session cache: {proxy_hash: (session_id, expires_at_monotonic)}
RcpSessionCache = dict[str, tuple[str, float]]

# ── Session management ───────────────────────────────────────────────────────


async def get_cached_rcp_session(
    session_cache: RcpSessionCache,
    proxy_host: str,
    proxy_hash: str,
) -> str | None:
    """Return a cached RCP session ID, opening a new one if missing or expired.

    Caches valid session IDs for 5 minutes (TTL 300 s) to avoid the 2-step
    RCP handshake (0xff0c + 0xff0d) on every thumbnail or data fetch.
    """
    now = time.monotonic()
    cached = session_cache.get(proxy_hash)
    if cached:
        session_id, expires_at = cached
        if now < expires_at:
            return session_id
        del session_cache[proxy_hash]

    session_id = await rcp_session(session_cache, proxy_host, proxy_hash)
    if session_id:
        session_cache[proxy_hash] = (session_id, now + 300.0)  # 5-min TTL
    return session_id


async def rcp_session(
    session_cache: RcpSessionCache,
    proxy_host: str,
    proxy_hash: str,
) -> str | None:
    """Open an RCP session via the cloud proxy and return the sessionid, or None on failure.

    The RCP handshake consists of two steps:
      1. WRITE command 0xff0c with a fixed payload -> extract <sessionid> from XML response
      2. WRITE command 0xff0d with the sessionid -> ACK (confirms the session)

    Auth=3 (anonymous via URL hash) provides read-only access.
    The proxy_host should be in the form "proxy-NN.live.cbs.boschsecurity.com:42090".
    """
    base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"
    init_payload = (
        "0x0102004000000000040000000000000000010000000000000001000000000000"
    )

    connector = aiohttp.TCPConnector(ssl=False)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
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
                                "rcp_session: step1 HTTP %d for %s",
                                resp.status,
                                proxy_host,
                            )
                            return None
                        text = await resp.text()
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug(
                    "rcp_session: step1 error for %s: %s", proxy_host, err
                )
                return None

            # Parse <sessionid> from XML response
            m = _re.search(r"<sessionid>(\S+)</sessionid>", text, _re.IGNORECASE)
            if not m:
                _LOGGER.debug(
                    "rcp_session: no <sessionid> in response for %s: %s",
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
                            "rcp_session: ACK HTTP %d for %s (sessionid=%s)",
                            resp2.status,
                            proxy_host,
                            session_id,
                        )
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.debug(
                    "rcp_session: step2 error for %s: %s", proxy_host, err
                )
                # Session may still be valid -- return it anyway

            return session_id
    finally:
        await connector.close()


# ── Read operations ──────────────────────────────────────────────────────────


async def rcp_read(
    hass: HomeAssistant,
    rcp_base: str,
    command: str,
    sessionid: str,
    type_: str = "P_OCTET",
    num: int = 0,
) -> bytes | None:
    """READ an RCP command and return the raw payload bytes, or None on failure.

    Uses the HA shared session (verify_ssl=False) to avoid creating a new
    connector+session per RCP command (prevents socket exhaustion).
    """
    params: dict[str, str] = {
        "command": command,
        "direction": "READ",
        "type": type_,
        "sessionid": sessionid,
    }
    if num:
        params["num"] = str(num)

    session = async_get_clientsession(hass, verify_ssl=False)
    try:
        async with asyncio.timeout(8):
            async with session.get(rcp_base, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "rcp_read: command=%s HTTP %d", command, resp.status
                    )
                    return None
                return await resp.read()
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("rcp_read: command=%s error: %s", command, err)
        return None


# ── Data update (reads multiple RCP values for a camera) ─────────────────────


async def async_update_rcp_data(
    coordinator: Any,
    cam_id: str,
    proxy_host: str,
    proxy_hash: str,
) -> None:
    """Fetch RCP data (LED dimmer, privacy state, etc.) for a camera via cloud proxy.

    Opens a fresh RCP session, reads multiple RCP commands, and caches the
    results on the coordinator. Gracefully skips on any failure -- RCP is
    read-only supplementary data and must never block the main coordinator update.

    Expects the coordinator to have these dict attributes:
      - _rcp_session_cache
      - _rcp_dimmer_cache
      - _rcp_privacy_cache
      - _rcp_clock_offset_cache
      - _rcp_lan_ip_cache
      - _rcp_product_name_cache
      - _rcp_bitrate_cache
      - hass
    """
    session_id = await get_cached_rcp_session(
        coordinator._rcp_session_cache, proxy_host, proxy_hash
    )
    if not session_id:
        _LOGGER.debug(
            "async_update_rcp_data: could not open RCP session for %s", cam_id
        )
        return

    rcp_base = f"https://{proxy_host}/{proxy_hash}/rcp.xml"
    hass = coordinator.hass

    # Read LED dimmer (0x0c22) -- T_WORD, num=1 -> integer 0-100
    try:
        raw = await rcp_read(hass, rcp_base, "0x0c22", session_id, type_="T_WORD", num=1)
        if raw and len(raw) >= 2:
            dimmer_val = struct.unpack(">H", raw[:2])[0]
            coordinator._rcp_dimmer_cache[cam_id] = int(dimmer_val)
            _LOGGER.debug("RCP LED dimmer for %s: %d%%", cam_id, dimmer_val)
    except Exception as err:
        _LOGGER.debug("RCP dimmer read error for %s: %s", cam_id, err)

    # Read privacy mask (0x0d00) -- P_OCTET 4B -> byte[1]=1 means ON
    try:
        raw = await rcp_read(hass, rcp_base, "0x0d00", session_id, type_="P_OCTET")
        if raw and len(raw) >= 2:
            coordinator._rcp_privacy_cache[cam_id] = int(raw[1])
            _LOGGER.debug(
                "RCP privacy mask for %s: byte[1]=%d", cam_id, raw[1]
            )
    except Exception as err:
        _LOGGER.debug("RCP privacy read error for %s: %s", cam_id, err)

    # Read camera clock (0x0a0f) -- 8 bytes -> compute offset vs server time
    try:
        raw = await rcp_read(hass, rcp_base, "0x0a0f", session_id, type_="P_OCTET")
        if raw and len(raw) >= 8:
            # RCP clock format: year(2B big-endian) month(1B) day(1B) hour(1B) min(1B) sec(1B) weekday(1B)
            year, month, day, hour, minute, second, _ = struct.unpack(
                ">HBBBBBB", raw[:8]
            )
            cam_dt = _dt.datetime(
                year, month, day, hour, minute, second, tzinfo=_dt.timezone.utc
            )
            server_dt = _dt.datetime.now(_dt.timezone.utc)
            offset = (cam_dt - server_dt).total_seconds()
            coordinator._rcp_clock_offset_cache[cam_id] = round(offset, 1)
            _LOGGER.debug("RCP clock offset for %s: %.1fs", cam_id, offset)
    except Exception as err:
        _LOGGER.debug("RCP clock read error for %s: %s", cam_id, err)

    # Read LAN IP via RCP (0x0a36) -- 4 bytes IPv4 or ASCII string
    try:
        raw = await rcp_read(hass, rcp_base, "0x0a36", session_id, type_="P_OCTET")
        if raw:
            if len(raw) == 4:
                ip_str = ".".join(str(b) for b in raw)
            else:
                ip_str = raw.rstrip(b"\x00").decode("ascii", errors="replace")
            coordinator._rcp_lan_ip_cache[cam_id] = ip_str
            _LOGGER.debug("RCP LAN IP for %s: %s", cam_id, ip_str)
    except Exception as err:
        _LOGGER.debug("RCP LAN IP read error for %s: %s", cam_id, err)

    # Read product name via RCP (0x0aea) -- null-terminated ASCII
    try:
        raw = await rcp_read(hass, rcp_base, "0x0aea", session_id, type_="P_OCTET")
        if raw:
            name_str = raw.rstrip(b"\x00").decode("ascii", errors="replace")
            coordinator._rcp_product_name_cache[cam_id] = name_str
            _LOGGER.debug("RCP product name for %s: %s", cam_id, name_str)
    except Exception as err:
        _LOGGER.debug("RCP product name read error for %s: %s", cam_id, err)

    # Read bitrate ladder (0x0c81) -- series of big-endian uint32 kbps values
    try:
        raw = await rcp_read(hass, rcp_base, "0x0c81", session_id, type_="P_OCTET")
        if raw and len(raw) >= 4:
            n = len(raw) // 4
            ladder = [struct.unpack(">I", raw[i * 4 : (i + 1) * 4])[0] for i in range(n)]
            coordinator._rcp_bitrate_cache[cam_id] = ladder
            _LOGGER.debug("RCP bitrate ladder for %s: %s", cam_id, ladder)
    except Exception as err:
        _LOGGER.debug("RCP bitrate read error for %s: %s", cam_id, err)
