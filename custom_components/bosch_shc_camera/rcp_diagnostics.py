"""LAN-sourced RCP diagnostic sensors (ONVIF scopes, RCP version) + cache getters.

Depends on `rcp_client.py`'s `_parse_onvif_scopes`. Free functions taking
the coordinator instance as their first argument — matches the
`quality_prefs`/`rcp_client` pattern already established here.
`BoschCameraCoordinator` keeps a thin delegating method for each of these
(same name/signature, calls straight into the matching function here) so
every existing call site — the coordinator's own slow-tier tick,
`sensor.py`'s clock-offset/LAN-IP/product-name/bitrate-ladder sensors —
keeps working unchanged, and so does the test suite's
instance-attribute-patching pattern (`coord._async_update_lan_diagnostic_sensors
= AsyncMock()`) and unbound-method-call pattern
(`BoschCameraCoordinator.clock_offset(coord, cam_id)`).

`_async_update_lan_diagnostic_sensors` calls `coordinator._fetch_rcp_lan(...)`
(the coordinator's own delegating method, itself backed by `rcp_client.py`),
not `rcp_client._fetch_rcp_lan` directly — tests patch `coord._fetch_rcp_lan`
as an instance attribute, and calling the module function directly would
silently bypass that patch.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .rcp_client import _parse_onvif_scopes

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


async def _async_update_lan_diagnostic_sensors(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Fetch F4 (ONVIF scopes) and F6 (RCP version) for a single camera via LAN.

    Called on slow-tier when the camera is ONLINE, LAN IP is known, and
    cbs creds are cached. Failures are non-fatal: caches keep their last
    known value or remain absent (sensor shows unavailable).
    """
    # F4: ONVIF scopes via RCP 0x0a98 — ~720 B ASCII TLV
    try:
        raw_onvif = await coordinator._fetch_rcp_lan(cam_id, "0x0a98")
        if raw_onvif:
            scopes_dict = _parse_onvif_scopes(raw_onvif)
            coordinator.rcp_onvif_scopes_cache[cam_id] = scopes_dict
            _LOGGER.debug("ONVIF scopes for %s: %s", cam_id[:8], scopes_dict)
    except Exception as err:
        _LOGGER.debug(
            "ONVIF scopes fetch error for %s: %s",
            cam_id[:8],
            coordinator.err_str(err),
        )

    # F6: RCP protocol versions via 0xff00 (primary) + 0xff04 (secondary)
    try:
        raw_ver = await coordinator._fetch_rcp_lan(cam_id, "0xff00")
        if raw_ver and len(raw_ver) >= 4:
            version_str = f"{raw_ver[0]}.{raw_ver[1]}.{raw_ver[2]}.{raw_ver[3]}"
            coordinator.rcp_version_cache[cam_id] = version_str
            _LOGGER.debug("RCP version for %s: %s", cam_id[:8], version_str)
    except Exception as err:
        _LOGGER.debug(
            "RCP version fetch error for %s: %s",
            cam_id[:8],
            coordinator.err_str(err),
        )


def clock_offset(coordinator: BoschCameraCoordinator, cam_id: str) -> float | None:
    """Return clock offset in seconds (camera time − server time), or None."""
    return coordinator.rcp_clock_offset_cache.get(cam_id)


def rcp_lan_ip(coordinator: BoschCameraCoordinator, cam_id: str) -> str | None:
    """Return camera LAN IP from RCP 0x0a36, or None."""
    return coordinator.rcp_lan_ip_cache.get(cam_id)


def rcp_product_name(coordinator: BoschCameraCoordinator, cam_id: str) -> str | None:
    """Return camera product name from RCP 0x0aea, or None."""
    return coordinator.rcp_product_name_cache.get(cam_id)


def rcp_bitrate_ladder(coordinator: BoschCameraCoordinator, cam_id: str) -> list[int]:
    """Return bitrate ladder (kbps) from RCP 0x0c81, or empty list."""
    return coordinator.rcp_bitrate_cache.get(cam_id, [])
