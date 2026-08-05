"""Video-quality and Mini-NVR-mode preference getters/setters.

These are pure per-camera preference lookups/writes over a handful of
runtime-only dicts (`_quality_preference`, `_proxy_url_cache`,
`_quality_effective_inst`, `_nvr_mode_preference`, `_nvr_event_clip_enabled`)
plus two read-only coordinator views (`options`, `data`) — no `self.hass`,
no locking, no `async_set_updated_data`. They don't belong inline on the
`DataUpdateCoordinator` subclass any more than the other free-function
modules in this package do. Matches the `tick_bootstrap`/`tick_failure`/
`tick_housekeeping`/`device_actions` pattern already established here:
free functions taking the coordinator instance as their first argument.

`BoschCameraCoordinator` keeps a thin delegating method for each of these
(same name/signature, calls straight into the matching function here) so
every existing call site — `select.py`'s quality/NVR-mode selects,
`switch.py`'s recording/NVR-event-clip switches, `live_connection.py`,
`recorder.py`, `fcm.py`, `sensor.py`, `camera.py` — keeps working
unchanged, and so do the test suite's unbound-method-call-on-a-stub
patterns (`BoschCameraCoordinator.get_quality(stub_coord, cam_id)`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator


def get_quality(coordinator: BoschCameraCoordinator, cam_id: str) -> str:
    """Return current quality preference: 'auto', 'high', or 'low'.

    Priority:
      1. Runtime override set by BoschVideoQualitySelect (session-only)
      2. 'auto' (LAN streams are always forced to hq=True, inst=1 regardless)
    """
    if cam_id in coordinator._quality_preference:
        return coordinator._quality_preference[cam_id]
    return "auto"


def set_quality(coordinator: BoschCameraCoordinator, cam_id: str, quality: str) -> None:
    """Set quality preference. quality must be 'auto', 'high', or 'low'."""
    coordinator._quality_preference[cam_id] = quality
    # Invalidate proxy URL cache so next fetch uses a fresh PUT /connection
    # with the updated highQualityVideo flag
    coordinator._proxy_url_cache.pop(cam_id, None)


def get_quality_params(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> tuple[bool, int]:
    """Return (highQualityVideo: bool, inst: int) for current quality preference."""
    q = get_quality(coordinator, cam_id)
    if q == "high":
        return True, 1  # primary encoder, max quality (~30 Mbps)
    if q == "low":
        return False, 4  # low-bandwidth stream (~1.9 Mbps)
    return False, 2  # "auto" — iOS default, balanced (~7.5 Mbps)


def get_quality_remote_fallback_active(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> bool:
    """True if the "low" preference is set but the last successful
    connection actually used inst=2 (~7.5 Mbps) instead of inst=4
    (~1.9 Mbps) — i.e. a REMOTE session clamped it, since Bosch's REMOTE
    proxy rejects inst=4. Used by BoschVideoQualitySelect to surface the
    divergence instead of silently showing "low" while streaming ~4x
    the bandwidth the label promises.
    """
    return (
        get_quality(coordinator, cam_id) == "low"
        and coordinator._quality_effective_inst.get(cam_id) == 2
    )


def get_nvr_mode(coordinator: BoschCameraCoordinator, cam_id: str) -> str:
    """Return effective Mini-NVR mode for this camera: 'continuous' or 'event_buffered'.

    Priority:
      1. Per-camera override set by BoschNvrModeSelect — lets a
         mixed fleet run different strategies, e.g. glass-facing cameras
         where PIR never fires need continuous-while-armed, premises
         cameras want a lightweight pre-roll ring instead of 24/7 capture).
      2. Global ``nvr_event_only`` option, for full backward compatibility
         with installs that never touch the new per-camera select.
    """
    override = coordinator._nvr_mode_preference.get(cam_id)
    if override in ("continuous", "event_buffered"):
        return override
    return (
        "event_buffered"
        if coordinator.options.get("nvr_event_only", False)
        else "continuous"
    )


def set_nvr_mode(coordinator: BoschCameraCoordinator, cam_id: str, mode: str) -> None:
    """Set the per-camera Mini-NVR mode override. mode must be 'continuous' or 'event_buffered'."""
    coordinator._nvr_mode_preference[cam_id] = mode


def get_nvr_event_clip_enabled(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> bool:
    """Return whether native FCM-triggered event→clip assembly is on for this camera.

    Defaults to True (backward compatible with every install that
    predates the ``BoschNvrEventClipSwitch`` entity) unless explicitly
    turned off.
    """
    return coordinator._nvr_event_clip_enabled.get(cam_id, True)


def set_nvr_event_clip_enabled(
    coordinator: BoschCameraCoordinator, cam_id: str, enabled: bool
) -> None:
    """Set whether native FCM-triggered event→clip assembly runs for this camera."""
    coordinator._nvr_event_clip_enabled[cam_id] = enabled


def motion_settings(coordinator: BoschCameraCoordinator, cam_id: str) -> dict[str, Any]:
    """Return motion detection settings dict, or empty dict."""
    return coordinator.data.get(cam_id, {}).get("motion", {})  # type: ignore[no-any-return]


def recording_options(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> dict[str, Any]:
    """Return recording options dict, or empty dict."""
    return coordinator.data.get(cam_id, {}).get("recordingOptions", {})  # type: ignore[no-any-return]
