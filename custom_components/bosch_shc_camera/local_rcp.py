"""Lokales / Cloud-Proxy RCP+ READ über `/rcp.xml` (XML-Format, kein binäres TLV).

Read-only — Writes brauchen `service`-Account-Auth, die Bosch nur intern hält.
Diese Datei liefert nur Read-Helfer; Aufrufer (Coordinator) entscheidet wann.

Zwei Auth-Modi:
  LOCAL  — HTTP Digest mit `cbs-…`-User aus `PUT /connection LOCAL`. URL: `https://<cam_ip>:443/rcp.xml?…`.
  REMOTE — HTTP Basic (empty:empty) mit Cloud-Proxy-Hash-URL aus `PUT /connection REMOTE`.
           Hash ist die Credential — URL: `https://proxy-XX:42090/{hash}/rcp.xml?…`.

XML-Response-Format (verifiziert 2026-04-27 gegen Gen2 Outdoor FW 9.40.25):
  <rcp>
    <command><hex>0xNNNN</hex>…</command>
    <type>P_OCTET|P_STRING|T_WORD|T_DWORD</type>
    <result>
      <dec>N</dec>             ← T_WORD/T_DWORD
      <str>HEX BYTES…</str>     ← P_OCTET (space-separated hex) oder P_STRING (ASCII)
      <err>0xNN</err>           ← Read fehlgeschlagen / wrong auth-level
    </result>
  </rcp>

Empfindlichkeiten:
- Bei Gen2 Outdoor (FW 9.40.25) rotiert `PUT /connection` die Digest-Cred. Ein normaler
  RCP-Read rotiert *nicht* — verifiziert per Stresstest (10 reads/10 s, Stream blieb up).
- TLS verify=False: Bosch verwendet eine private CA (NXP-BUID) für die Camera-Cert.
"""

import logging
import ssl
import urllib.error
import urllib.request
from typing import Any

from defusedxml import ElementTree as ET  # XXE-safe drop-in for xml.etree.ElementTree

_LOGGER = logging.getLogger(__name__)


def _parse_rcp_xml(text: str, type_: str) -> Any:
    """Parse RCP+ XML response. Returns int / bytes / str, or None on error/parse-fail."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        _LOGGER.debug("rcp parse: not XML (first 100 chars): %r", text[:100])
        return None
    err_el = root.find(".//result/err")
    if err_el is not None:
        _LOGGER.debug("rcp parse: <err>%s</err> for type=%s", err_el.text, type_)
        return None
    if type_ in ("T_WORD", "T_DWORD", "T_BYTE"):
        dec = root.find(".//result/dec")
        if dec is not None and dec.text:
            try:
                return int(dec.text.strip())
            except ValueError:
                return None
        return None
    if type_ == "P_STRING":
        s = root.find(".//result/str")
        return s.text if s is not None else None
    if type_ == "P_OCTET":
        s = root.find(".//result/str")
        if s is None or not s.text:
            return None
        try:
            return bytes.fromhex(s.text.replace(" ", ""))
        except ValueError:
            return None
    return None


def rcp_read_local_sync(
    host: str, user: str, pwd: str, command: str, type_: str, timeout: float = 5.0
) -> Any:
    """RCP+ READ via local HTTPS endpoint with HTTP Digest auth (cbs-…-user).

    `host` is "192.168.x.x:443". Returns parsed value or None on any failure.
    """
    url = f"https://{host}/rcp.xml?command={command}&type={type_}&direction=READ&num=1&payload="
    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, url, user, pwd)
    handler = urllib.request.HTTPDigestAuthHandler(pwd_mgr)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    https_handler = urllib.request.HTTPSHandler(context=ctx)
    opener = urllib.request.build_opener(handler, https_handler)
    try:
        with opener.open(url, timeout=timeout) as r:
            if r.status != 200:
                _LOGGER.debug("rcp local: %s %s → HTTP %d", command, type_, r.status)
                return None
            return _parse_rcp_xml(r.read().decode(errors="replace"), type_)
    except (urllib.error.URLError, OSError, ValueError) as err:
        _LOGGER.debug("rcp local: %s %s → error: %s", command, type_, err)
        return None


def rcp_read_remote_sync(
    proxy_url_with_hash: str, command: str, type_: str, timeout: float = 5.0
) -> Any:
    """RCP+ READ via Bosch Cloud-Proxy. Hash inside the URL IS the credential.

    `proxy_url_with_hash` is e.g.
    "proxy-20.live.cbs.boschsecurity.com:42090/abc123def".
    Returns parsed value or None on any failure.

    Note: per `research/rcp_findings.txt` (2026-03-22 Cloud-Proxy probe), the
    Cloud-Proxy may return *binary TLV* on `/rcp.xml` rather than XML, depending
    on Content-Type negotiation. We always try XML parse first; if that fails
    we return None (no binary path implemented). When a real REMOTE-only test
    surfaces a binary response, extend `_parse_rcp_xml` with a binary fallback.
    """
    url = f"https://{proxy_url_with_hash}/rcp.xml?command={command}&type={type_}&direction=READ&num=1&payload="
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:  # noqa: S310 # local camera RCP endpoint, fixed https scheme, not user-supplied
            if r.status != 200:
                _LOGGER.debug("rcp remote: %s %s → HTTP %d", command, type_, r.status)
                return None
            return _parse_rcp_xml(r.read().decode(errors="replace"), type_)
    except (urllib.error.URLError, OSError, ValueError) as err:
        _LOGGER.debug("rcp remote: %s %s → error: %s", command, type_, err)
        return None


# ── Field-specific helpers — RETIRED ────────────────────────────────────────
# Earlier versions exported parse_privacy_state(0x0d00) and
# parse_led_dimmer_percent(0x0c22) here. Both were removed in v10.4.9 after
# A/B testing proved the byte mappings did NOT match the user-facing
# privacy-mode toggle (0x0d00 byte[1] stayed 1 even with the mode OFF).
# rcp_findings.txt's "PRIVACY MASK" label was taken to mean "privacy mode";
# it doesn't. Don't add field-specific helpers here again without:
#   1. Toggling the user-facing setting both ways
#   2. Re-reading the RCP value after each toggle
#   3. Confirming the RCP value actually changes
# The generic rcp_read_local_sync / rcp_read_remote_sync helpers remain
# correct and are kept for future verified uses.
