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

            # Validate session ID before ACK — 0x00000000 means proxy rejected
            if session_id == "0x00000000":
                _LOGGER.debug(
                    "rcp_session: invalid session 0x00000000 for %s — proxy rejected",
                    proxy_host,
                )
                return None

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

            return session_id
    finally:
        await connector.close()


# ── Direct LOCAL RCP (no cloud proxy, no auth — Gen2 only) ───────────────────
#
# Gen2 cameras accept RCP commands on http://CAM_IP/rcp.xml without any
# authentication. Used as a fallback path when the Bosch cloud API or
# auth server is unreachable, so the integration can still read and
# (best-effort) write privacy state without going through the cloud.


async def rcp_local_read(
    hass: "HomeAssistant",
    cam_ip: str,
    command: str,
    type_: str = "P_OCTET",
    num: int = 0,
) -> bytes | None:
    """Read an RCP value directly from the camera's LAN HTTP endpoint.

    Returns the decoded payload bytes on success, None on any failure.
    Gen2 cameras answer unauthenticated RCP queries on port 80; Gen1 returns
    401 and this function will simply return None (graceful).
    """
    base = f"http://{cam_ip}/rcp.xml"
    params: dict[str, str] = {
        "command":   command,
        "direction": "READ",
        "type":      type_,
    }
    if num:
        params["num"] = str(num)
    session = async_get_clientsession(hass, verify_ssl=False)
    try:
        async with asyncio.timeout(5):
            async with session.get(base, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "rcp_local_read: %s@%s HTTP %d", command, cam_ip, resp.status,
                    )
                    return None
                raw = await resp.read()
                err_m = _re.search(rb"<err>(\S+)</err>", raw, _re.IGNORECASE)
                if err_m:
                    _LOGGER.debug(
                        "rcp_local_read: %s@%s err=%s",
                        command, cam_ip,
                        err_m.group(1).decode("ascii", errors="replace"),
                    )
                    return None
                payload_m = (
                    _re.search(rb"<str>([0-9a-fA-F]+)</str>", raw, _re.IGNORECASE)
                    or _re.search(rb"<payload>([0-9a-fA-F]+)</payload>", raw, _re.IGNORECASE)
                )
                if payload_m:
                    return bytes.fromhex(payload_m.group(1).decode("ascii"))
                if raw and not raw.startswith(b"<"):
                    return raw
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("rcp_local_read: %s@%s %s", command, cam_ip, err)
    return None


async def rcp_local_write(
    hass: "HomeAssistant",
    cam_ip: str,
    command: str,
    payload_hex: str,
    type_: str = "P_OCTET",
) -> bool:
    """Write an RCP value directly via the camera's LAN HTTP endpoint.

    Returns True on success. `payload_hex` may start with "0x" or not.
    Best-effort: any error returns False (caller should handle gracefully).
    """
    base = f"http://{cam_ip}/rcp.xml"
    if not payload_hex.lower().startswith("0x"):
        payload_hex = "0x" + payload_hex
    params = {
        "command":   command,
        "direction": "WRITE",
        "type":      type_,
        "payload":   payload_hex,
    }
    session = async_get_clientsession(hass, verify_ssl=False)
    try:
        async with asyncio.timeout(5):
            async with session.get(base, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "rcp_local_write: %s@%s HTTP %d",
                        command, cam_ip, resp.status,
                    )
                    return False
                raw = await resp.read()
                if b"<err>" in raw.lower():
                    _LOGGER.debug(
                        "rcp_local_write: %s@%s RCP error in response",
                        command, cam_ip,
                    )
                    return False
                return True
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.debug("rcp_local_write: %s@%s %s", command, cam_ip, err)
    return False


async def rcp_local_read_privacy(
    hass: "HomeAssistant", cam_ip: str,
) -> bool | None:
    """Read privacy-mode state via direct LOCAL RCP (Gen2, no auth).

    Uses command 0x0d00 (privacy mask), which returns 4 bytes where byte[1]
    indicates privacy state: 0=OFF, 1=ON. Returns None if unavailable.
    """
    raw = await rcp_local_read(hass, cam_ip, "0x0d00", "P_OCTET")
    if raw and len(raw) >= 2:
        return bool(raw[1])
    return None


async def rcp_local_write_privacy(
    hass: "HomeAssistant", cam_ip: str, enabled: bool,
) -> bool:
    """Write privacy-mode state via direct LOCAL RCP (Gen2, no auth).

    Best-effort fallback used when the cloud API is unreachable. Not all
    Gen2 models accept privacy writes over unauthenticated RCP — a False
    return means the caller should surface this to the user.
    """
    # Privacy mask payload: 4 bytes, byte[1] carries the mode. Keep the
    # remaining bytes zero so we don't stamp over other mask fields.
    payload = "00010000" if enabled else "00000000"
    return await rcp_local_write(hass, cam_ip, "0x0d00", payload, "P_OCTET")


# ── Read operations ──────────────────────────────────────────────────────────


async def rcp_read(
    hass: HomeAssistant,
    rcp_base: str,
    command: str,
    sessionid: str,
    type_: str = "P_OCTET",
    num: int = 0,
    session_cache: RcpSessionCache | None = None,
) -> bytes | None:
    """READ an RCP command and return the payload bytes, or None on failure.

    The RCP endpoint returns XML like:
      <rcp ... ><payload>0a1b2c3d...</payload></rcp>
    or for errors:
      <rcp ... ><err>0xa0</err></rcp>

    This function extracts the hex payload and returns it as bytes.
    Uses the HA shared session (verify_ssl=False for cloud proxy — non-standard certs).

    If session_cache is provided, the cached session for the URL's proxy_hash
    is invalidated on HTTP 401/403 or RCP <err>0x0c0d</err> (session closed)
    so the next call opens a fresh handshake instead of reusing a dead ID.
    """
    params: dict[str, str] = {
        "command": command,
        "direction": "READ",
        "type": type_,
        "sessionid": sessionid,
    }
    if num:
        params["num"] = str(num)

    def _drop_cached_session() -> None:
        if session_cache is None:
            return
        parts = rcp_base.rstrip("/").split("/")
        if len(parts) >= 2 and parts[-1] == "rcp.xml":
            proxy_hash = parts[-2]
            if session_cache.pop(proxy_hash, None) is not None:
                _LOGGER.debug("RCP session cache invalidated for %s", proxy_hash[:8])

    session = async_get_clientsession(hass, verify_ssl=False)
    try:
        async with asyncio.timeout(8):
            async with session.get(rcp_base, params=params) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "rcp_read: command=%s HTTP %d", command, resp.status
                    )
                    if resp.status in (401, 403):
                        _drop_cached_session()
                    return None
                raw = await resp.read()
                # RCP returns XML: <rcp ...><payload>HEX</payload></rcp>
                # or error: <rcp ...><err>0xa0</err></rcp>
                # Parse raw bytes with regex to avoid UTF-8 decode issues.

                # Check for error response
                err_m = _re.search(rb"<err>(\S+)</err>", raw, _re.IGNORECASE)
                if err_m:
                    err_code = err_m.group(1).decode("ascii", errors="replace")
                    _LOGGER.debug(
                        "rcp_read: command=%s error=%s", command, err_code,
                    )
                    # 0x0c0d = session closed → drop the cached ID so the next
                    # call reopens the handshake instead of replaying a dead one.
                    if err_code.lower() == "0x0c0d":
                        _drop_cached_session()
                    return None

                # Extract hex payload from XML — Bosch uses <str> or <payload> tag
                # depending on firmware version / request context
                payload_m = (
                    _re.search(rb"<str>([0-9a-fA-F]+)</str>", raw, _re.IGNORECASE)
                    or _re.search(rb"<payload>([0-9a-fA-F]+)</payload>", raw, _re.IGNORECASE)
                )
                if payload_m:
                    return bytes.fromhex(payload_m.group(1).decode("ascii"))

                # Fallback: raw binary response (non-XML, e.g. JPEG)
                if raw and not raw.startswith(b"<"):
                    return raw

                _LOGGER.debug(
                    "rcp_read: command=%s no payload in response (%d bytes): %.100s",
                    command, len(raw), raw[:100],
                )
                return None
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
    # Alias that forwards session_cache so any 401/403/0x0c0d response
    # drops the cached session ID and forces a fresh handshake next call.
    async def _read(command: str, type_: str = "P_OCTET", num: int = 0) -> bytes | None:
        return await rcp_read(
            hass, rcp_base, command, session_id,
            type_=type_, num=num,
            session_cache=coordinator._rcp_session_cache,
        )

    # Read LED dimmer (0x0c22) -- T_WORD, num=1 -> integer 0-100
    # Gen2 firmware occasionally returns 0x0A0A (2570) for the same setting;
    # format semantics differ per model. Clamp to 0..100 and skip the cache on
    # out-of-range values so the sensor doesn't surface nonsense.
    try:
        raw = await _read("0x0c22", type_="T_WORD", num=1)
        if raw and len(raw) >= 2:
            dimmer_val = struct.unpack(">H", raw[:2])[0]
            if 0 <= dimmer_val <= 100:
                coordinator._rcp_dimmer_cache[cam_id] = int(dimmer_val)
                _LOGGER.debug("RCP LED dimmer for %s: %d%%", cam_id, dimmer_val)
            else:
                _LOGGER.debug(
                    "RCP LED dimmer for %s: out-of-range raw=%d — cache skipped",
                    cam_id, dimmer_val,
                )
    except Exception as err:
        _LOGGER.debug("RCP dimmer read error for %s: %s", cam_id, err)

    # Read privacy mask (0x0d00) -- P_OCTET 4B -> byte[1]=1 means ON
    try:
        raw = await _read("0x0d00", type_="P_OCTET")
        if raw and len(raw) >= 2:
            coordinator._rcp_privacy_cache[cam_id] = int(raw[1])
            _LOGGER.debug(
                "RCP privacy mask for %s: byte[1]=%d", cam_id, raw[1]
            )
    except Exception as err:
        _LOGGER.debug("RCP privacy read error for %s: %s", cam_id, err)

    # Read camera clock (0x0a0f) -- 8 bytes -> compute offset vs server time
    # RCP clock format: year(2B big-endian) month(1B) day(1B) hour(1B) min(1B) sec(1B) weekday(1B)
    # Some firmwares (observed on Gen2) return a different layout with fields
    # that fall outside datetime ranges. Validate before constructing cam_dt so
    # parse failures skip silently instead of raising.
    try:
        raw = await _read("0x0a0f", type_="P_OCTET")
        if raw and len(raw) >= 8:
            year, month, day, hour, minute, second, _ = struct.unpack(
                ">HBBBBBB", raw[:8]
            )
            if (
                1970 <= year <= 2100
                and 1 <= month <= 12
                and 1 <= day <= 31
                and 0 <= hour <= 23
                and 0 <= minute <= 59
                and 0 <= second <= 59
            ):
                cam_dt = _dt.datetime(
                    year, month, day, hour, minute, second, tzinfo=_dt.timezone.utc
                )
                server_dt = _dt.datetime.now(_dt.timezone.utc)
                offset = (cam_dt - server_dt).total_seconds()
                coordinator._rcp_clock_offset_cache[cam_id] = round(offset, 1)
                _LOGGER.debug("RCP clock offset for %s: %.1fs", cam_id, offset)
            else:
                _LOGGER.debug(
                    "RCP clock for %s: unexpected layout "
                    "(Y=%d M=%d D=%d h=%d m=%d s=%d) — cache skipped",
                    cam_id, year, month, day, hour, minute, second,
                )
    except Exception as err:
        _LOGGER.debug("RCP clock read error for %s: %s", cam_id, err)

    # Read LAN IP via RCP (0x0a36) -- 4 bytes IPv4 or ASCII string
    # Gen1 returns raw bytes; Gen2 wraps the response in a nested XML document
    # whose hex-decoded payload starts with "<rcp>". Reject both empty and
    # XML-wrapped values so the cache isn't polluted.
    try:
        raw = await _read("0x0a36", type_="P_OCTET")
        if raw:
            if len(raw) == 4:
                ip_str = ".".join(str(b) for b in raw)
            else:
                ip_str = raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()
            if ip_str and ip_str != "0.0.0.0" and not ip_str.startswith("<"):
                coordinator._rcp_lan_ip_cache[cam_id] = ip_str
                _LOGGER.debug("RCP LAN IP for %s: %s", cam_id, ip_str)
            else:
                _LOGGER.debug(
                    "RCP LAN IP for %s: unusable payload (%r) — cache skipped",
                    cam_id, ip_str[:40],
                )
    except Exception as err:
        _LOGGER.debug("RCP LAN IP read error for %s: %s", cam_id, err)

    # Read product name via RCP (0x0aea) -- null-terminated ASCII
    # Same Gen2 XML-wrapper caveat as LAN IP above.
    try:
        raw = await _read("0x0aea", type_="P_OCTET")
        if raw:
            name_str = raw.rstrip(b"\x00").decode("ascii", errors="replace").strip()
            if name_str and not name_str.startswith("<"):
                coordinator._rcp_product_name_cache[cam_id] = name_str
                _LOGGER.debug("RCP product name for %s: %s", cam_id, name_str)
            else:
                _LOGGER.debug(
                    "RCP product name for %s: unusable payload (%r) — cache skipped",
                    cam_id, name_str[:40],
                )
    except Exception as err:
        _LOGGER.debug("RCP product name read error for %s: %s", cam_id, err)

    # Read bitrate ladder (0x0c81) -- series of big-endian uint32 kbps values
    try:
        raw = await _read("0x0c81", type_="P_OCTET")
        if raw and len(raw) >= 4:
            n = len(raw) // 4
            ladder = [struct.unpack(">I", raw[i * 4 : (i + 1) * 4])[0] for i in range(n)]
            coordinator._rcp_bitrate_cache[cam_id] = ladder
            _LOGGER.debug("RCP bitrate ladder for %s: %s", cam_id, ladder)
    except Exception as err:
        _LOGGER.debug("RCP bitrate read error for %s: %s", cam_id, err)

    # ── Phase 2 RCP reads ────────────────────────────────────────────────────

    # Read alarm catalog (0x0c38) -- UTF-16-BE, ~1366 bytes
    # Contains all alarm types the camera firmware supports (virtual 0-15,
    # flame, smoke, glass break, audio, storage, etc.)
    try:
        raw = await _read("0x0c38", type_="P_OCTET")
        if raw and len(raw) > 10:
            alarms = _parse_alarm_catalog(raw)
            coordinator._rcp_alarm_catalog_cache[cam_id] = alarms
            _LOGGER.debug("RCP alarm catalog for %s: %d types", cam_id, len(alarms))
    except Exception as err:
        _LOGGER.debug("RCP alarm catalog read error for %s: %s", cam_id, err)

    # Read motion detection zones (0x0c00) -- 5 zones × 28 bytes
    try:
        raw = await _read("0x0c00", type_="P_OCTET")
        if raw and len(raw) >= 28:
            zones = _parse_motion_zones(raw)
            coordinator._rcp_motion_zones_cache[cam_id] = zones
            _LOGGER.debug("RCP motion zones for %s: %d zones", cam_id, len(zones))
    except Exception as err:
        _LOGGER.debug("RCP motion zones read error for %s: %s", cam_id, err)

    # Read motion zone coordinates (0x0c0a) -- int32 normalized ±1.0 as ×2^31
    try:
        raw = await _read("0x0c0a", type_="P_OCTET")
        if raw and len(raw) >= 16:
            coords = _parse_motion_coords(raw)
            coordinator._rcp_motion_coords_cache[cam_id] = coords
            _LOGGER.debug("RCP motion coords for %s: %d points", cam_id, len(coords))
    except Exception as err:
        _LOGGER.debug("RCP motion coords read error for %s: %s", cam_id, err)

    # Read TLS certificate (0x0b91) -- DER X.509, ~455 bytes
    try:
        raw = await _read("0x0b91", type_="P_OCTET")
        if raw and len(raw) > 50:
            cert_info = _parse_tls_cert(raw)
            coordinator._rcp_tls_cert_cache[cam_id] = cert_info
            _LOGGER.debug("RCP TLS cert for %s: %s", cam_id, cert_info)
    except Exception as err:
        _LOGGER.debug("RCP TLS cert read error for %s: %s", cam_id, err)

    # Read network services (0x0c62) -- TLV list, ~469 bytes
    try:
        raw = await _read("0x0c62", type_="P_OCTET")
        if raw and len(raw) > 10:
            services = _parse_network_services(raw)
            coordinator._rcp_network_services_cache[cam_id] = services
            _LOGGER.debug("RCP network services for %s: %s", cam_id, services)
    except Exception as err:
        _LOGGER.debug("RCP network services read error for %s: %s", cam_id, err)

    # Read IVA analytics catalog (0x0b60) -- 65 entries × 6B
    try:
        raw = await _read("0x0b60", type_="P_OCTET")
        if raw and len(raw) >= 6:
            analytics = _parse_iva_catalog(raw)
            coordinator._rcp_iva_catalog_cache[cam_id] = analytics
            _LOGGER.debug("RCP IVA catalog for %s: %d modules", cam_id, len(analytics))
    except Exception as err:
        _LOGGER.debug("RCP IVA catalog read error for %s: %s", cam_id, err)


# ── Phase 2 parsers ─────────────────────────────────────────────────────────


def _parse_alarm_catalog(raw: bytes) -> list[dict]:
    """Parse alarm catalog (0x0c38) from UTF-16-BE encoded TLV data.

    Returns list of dicts: [{"id": 0, "name": "Virtual Alarm 0", "type": "virtual"}, ...]
    """
    alarms = []
    try:
        # The raw data contains TLV entries with alarm names in UTF-16-BE.
        # Each entry: 2B id + 2B length + UTF-16-BE name
        # Fallback: try decoding entire blob as UTF-16-BE and split by null chars
        text = raw.decode("utf-16-be", errors="replace")
        # Split by null characters and filter empty strings
        parts = [p.strip() for p in text.split("\x00") if p.strip()]
        for i, name in enumerate(parts):
            # Clean up control characters
            name = "".join(c for c in name if c.isprintable() or c == " ")
            if name and len(name) > 1:
                alarm_type = "unknown"
                name_lower = name.lower()
                if "virtual alarm" in name_lower:
                    alarm_type = "virtual"
                elif "flame" in name_lower:
                    alarm_type = "flame"
                elif "smoke" in name_lower:
                    alarm_type = "smoke"
                elif "audio" in name_lower:
                    alarm_type = "audio"
                elif "signal" in name_lower or "loss" in name_lower:
                    alarm_type = "signal"
                elif "storage" in name_lower or "disk" in name_lower:
                    alarm_type = "storage"
                elif "motion" in name_lower or "resilmotion" in name_lower or "resimotion" in name_lower:
                    alarm_type = "motion"
                elif "reference" in name_lower:
                    alarm_type = "reference"
                elif "config" in name_lower:
                    alarm_type = "config"
                elif "global" in name_lower:
                    alarm_type = "global_change"
                elif "task" in name_lower:
                    alarm_type = "task"
                alarms.append({"id": i, "name": name, "type": alarm_type})
    except Exception as err:
        _LOGGER.debug("_parse_alarm_catalog error: %s", err)
    return alarms


def _parse_motion_zones(raw: bytes) -> list[dict]:
    """Parse motion detection zones (0x0c00) — 5 zones × 28 bytes each.

    Returns list of dicts with zone info (id, enabled, sensitivity fields).
    """
    zones = []
    zone_size = 28
    n_zones = min(len(raw) // zone_size, 5)
    for i in range(n_zones):
        chunk = raw[i * zone_size : (i + 1) * zone_size]
        if len(chunk) < zone_size:
            break
        # First bytes contain zone config, exact struct is camera-specific
        # Expose raw hex for diagnostics, plus zone index
        zones.append({
            "zone_id": i,
            "raw_hex": chunk.hex(),
            "size": len(chunk),
        })
    return zones


def _parse_motion_coords(raw: bytes) -> list[dict]:
    """Parse motion region boundary coordinates (0x0c0a).

    Each zone is 8 bytes: x1(2B) y1(2B) x2(2B) y2(2B) in 0-10000 units.
    Returns list of zone rectangles as {x1, y1, x2, y2} in percent (0-100).
    """
    zones = []
    zone_size = 8
    n_zones = len(raw) // zone_size
    for z in range(n_zones):
        chunk = raw[z * zone_size : (z + 1) * zone_size]
        if len(chunk) < 8:
            break
        x1 = struct.unpack(">H", chunk[0:2])[0]
        y1 = struct.unpack(">H", chunk[2:4])[0]
        x2 = struct.unpack(">H", chunk[4:6])[0]
        y2 = struct.unpack(">H", chunk[6:8])[0]
        # Convert 0-10000 to 0-100 percent
        zones.append({
            "x1": round(x1 / 100, 1),
            "y1": round(y1 / 100, 1),
            "x2": round(x2 / 100, 1),
            "y2": round(y2 / 100, 1),
        })
    return zones


def _parse_tls_cert(raw: bytes) -> dict:
    """Parse DER X.509 certificate (0x0b91) and extract key info.

    Falls back to raw hex if pyOpenSSL/cryptography is not available.
    """
    info: dict = {"raw_size": len(raw)}
    try:
        from cryptography import x509
        cert = x509.load_der_x509_certificate(raw)
        info["issuer"] = cert.issuer.rfc4514_string()
        info["subject"] = cert.subject.rfc4514_string()
        info["serial"] = format(cert.serial_number, "x")
        info["not_before"] = cert.not_valid_before_utc.isoformat()
        info["not_after"] = cert.not_valid_after_utc.isoformat()
        info["key_size"] = cert.public_key().key_size
        info["signature_algorithm"] = cert.signature_algorithm_oid.dotted_string
    except ImportError:
        _LOGGER.debug("cryptography package not available — TLS cert raw only")
        info["raw_hex"] = raw[:40].hex() + "..."
    except Exception as err:
        _LOGGER.debug("TLS cert parse error: %s", err)
        info["raw_hex"] = raw[:40].hex() + "..."
    return info


def _parse_network_services(raw: bytes) -> list[str]:
    """Parse network services catalog (0x0c62) — TLV with service names.

    Returns list of service name strings.
    """
    services = []
    try:
        # TLV data contains ASCII service names separated by null bytes
        text = raw.decode("ascii", errors="replace")
        parts = [p.strip() for p in text.split("\x00") if p.strip()]
        for name in parts:
            clean = "".join(c for c in name if c.isprintable() or c == " ")
            if clean and len(clean) > 1:
                services.append(clean)
    except Exception as err:
        _LOGGER.debug("_parse_network_services error: %s", err)
    return services


def _parse_iva_catalog(raw: bytes) -> list[dict]:
    """Parse IVA analytics module catalog (0x0b60) — 65 entries × 6B.

    Returns list of dicts with module info.
    """
    modules = []
    entry_size = 6
    n = min(len(raw) // entry_size, 65)
    for i in range(n):
        chunk = raw[i * entry_size : (i + 1) * entry_size]
        if len(chunk) < entry_size:
            break
        # Each entry: 2B module_id + 2B version + 2B flags
        module_id = struct.unpack(">H", chunk[:2])[0]
        version = struct.unpack(">H", chunk[2:4])[0]
        flags = struct.unpack(">H", chunk[4:6])[0]
        if module_id > 0:  # skip empty entries
            modules.append({
                "module_id": module_id,
                "version": version,
                "flags": flags,
                "active": bool(flags & 0x01),
            })
    return modules
