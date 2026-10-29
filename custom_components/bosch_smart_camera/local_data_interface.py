"""Read-only status of the camera's local data interface.

Cloud status endpoint (per camera, GET):
  200 {"username": str}            -> active
  404 sh:entity.notfound           -> inactive (normal "off" state)
  449 sh:firmware.not.supported    -> unsupported
Any other status / network error / malformed body leaves the cached value
untouched. Only cameras on firmware >= LDI_MIN_FIRMWARE are polled.
"""

from __future__ import annotations

from typing import Any

LDI_ENDPOINT = "onvif_user"
LDI_MIN_FIRMWARE: tuple[int, ...] = (9, 40, 105)

STATE_ACTIVE = "active"
STATE_INACTIVE = "inactive"
STATE_UNSUPPORTED = "unsupported"


def parse_firmware(version: object) -> tuple[int, ...] | None:
    """Parse a dotted numeric firmware string; None for anything else."""
    if not isinstance(version, str):
        return None
    parts = version.strip().split(".")
    # Length cap: int() raises ValueError on absurdly long digit strings.
    if not all(p.isascii() and p.isdigit() and len(p) <= 9 for p in parts):
        return None
    return tuple(int(p) for p in parts)


def firmware_supports_ldi(version: object) -> bool:
    """True when the installed firmware is at or above the interface gate."""
    parsed = parse_firmware(version)
    return parsed is not None and parsed >= LDI_MIN_FIRMWARE


def state_from_response(status: int, body: object) -> dict[str, Any] | None:
    """Map an HTTP result to a cache entry, or None to keep the last value."""
    if status == 200:
        if isinstance(body, dict) and isinstance(body.get("username"), str):
            return {"state": STATE_ACTIVE, "username": body["username"]}
        return None
    if status == 404:
        return {"state": STATE_INACTIVE}
    if status == 449:
        return {"state": STATE_UNSUPPORTED}
    return None
