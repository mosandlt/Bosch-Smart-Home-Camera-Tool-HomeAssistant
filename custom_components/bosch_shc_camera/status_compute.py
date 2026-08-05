"""Pure per-camera status-string derivation.

Covers the single status-derivation helper that mirrors
`sensor.BoschCameraStatusSensor.native_value` so the announce path and
the sensor never drift apart. This is pure logic over a `dict[str, Any]`
snapshot (the caller-supplied `cam_data`, or a read-only fallback lookup
into `coordinator.data`) — no `self.hass`, no `Store`/repairs-issue/
`persistent_notification` calls, no writes to any coordinator attribute.
It doesn't belong inline on the `DataUpdateCoordinator` subclass any more
than `quality_prefs.py`'s read-only preference getters do (which also
read a coordinator view — `coordinator.data`/`coordinator.options` — with
no side effects). Matches that established pattern: a free function
taking the coordinator instance as its first argument.

Split out of the "maintenance/announcements" extraction round (style
audit, 2026-08-05) specifically BECAUSE it doesn't share that cluster's
side-effecting nature — see `maintenance_announcements.py`'s module
docstring for the paired side-effecting half of that round.

`BoschCameraCoordinator` keeps a thin delegating method
(`_compute_status_for`, same name/signature, calls straight into
`compute_status_for` here) so every existing call site —
`tick_housekeeping.run_housekeeping`'s `getattr(coordinator,
"_compute_status_for", None)` reach, and the test suite's
unbound-method-call pattern (`BoschCameraCoordinator._compute_status_for(
coord, cam_id)`) — keeps working unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator


def compute_status_for(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    cam_data: dict[str, Any] | None = None,
) -> str:
    """Re-uses the BoschCameraStatusSensor logic so the announce path and
    the sensor never drift apart.

    Mirror of `sensor.BoschCameraStatusSensor.native_value`: cloud ONLINE
    + latest event TROUBLE_DISCONNECT → offline; otherwise the cloud
    status verbatim. The `cam_data` argument lets the update-loop pass
    the fresh data dict before `self.data` has been swapped by the
    parent class (`_async_update_data` returns after the per-cam
    transition check fires).
    """
    if cam_data is None:
        cam_data = coordinator.data.get(cam_id, {}) if coordinator.data else {}
    raw = str(cam_data.get("status", "UNKNOWN")).lower()
    if raw == "online":
        events = cam_data.get("events", [])
        if (
            events
            and str(events[0].get("eventType", "")).upper() == "TROUBLE_DISCONNECT"
        ):
            return "offline"
    return raw
