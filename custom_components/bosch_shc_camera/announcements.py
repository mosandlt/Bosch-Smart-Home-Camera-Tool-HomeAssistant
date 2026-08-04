"""User-facing notification orchestration: maintenance windows, per-camera
online/offline transitions, and Bosch-cloud reachability transitions.

Extracted from `coordinator.py` (style audit, 2026-08-04) — this is
persistent_notification/notify-service dispatch business logic, not
"fetch and cache data" work, so it doesn't belong inline on the
`DataUpdateCoordinator` subclass. Matches the `tick_bootstrap`/
`tick_failure`/`tick_housekeeping` pattern already established in this
package: free functions taking the coordinator instance as their first
argument.

`BoschCameraCoordinator` keeps a thin delegating method for each of the
three public functions here (same name, calls straight into the
matching function). This is deliberate, not an oversight: `tick_failure.
dispatch_*` and `tick_housekeeping.run_housekeeping` reach
`_async_maybe_announce_camera_status`/`_async_maybe_announce_cloud_state`
via `getattr(coordinator, "...", None)` specifically so ~80 stub-
coordinator test fixtures across the suite (SimpleNamespace instances
that bypass `__init__` and never define these methods) get a safe no-op
instead of an AttributeError — see `tests/test_tick_failure.py::
test_missing_announce_method_is_a_noop` and `tests/
test_tick_housekeeping.py::test_stub_coordinator_without_*_no_crash`,
which pin that exact behavior. Removing the coordinator-level attribute
entirely (in favor of an unconditional call to a free function that
always exists) would silently change that tested contract. Keeping a
thin wrapper preserves it for free while still moving the actual logic
out of the coordinator class body.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator
    from .maintenance import MaintenanceWindow

_LOGGER = logging.getLogger(__name__)


async def maybe_announce_maintenance(
    coordinator: BoschCameraCoordinator, mw: MaintenanceWindow
) -> None:
    """Fire a user notification for a maintenance-window state transition.

    Triggers on state in {scheduled, active, past}, deduped by (link,
    state) so each window announces at most three times: scheduled when
    first seen, active when the window opens, past when it closes. The
    `past` announcement only fires if we previously announced `active`
    for the same link — otherwise an old past window discovered mid-feed
    would spam users with stale "wartung beendet" messages.

    Recent/unknown/idle states stay silent (no actionable info). Service
    routing: get_alert_services(coordinator, "system") — falls back to
    `alert_notify_service`, matching the existing TROUBLE event plumbing.

    Failure is non-fatal — a notify service can be misconfigured by the
    user, but maintenance discovery itself must keep working.
    """
    if not mw.camera_relevant:
        return
    state = mw.state()
    if state not in ("scheduled", "active", "past"):
        return
    # `past` only announces when we already announced `active` for this
    # same window (same link). Suppresses stale past-window discovery.
    if state == "past":
        prior = coordinator.maintenance_notified_key
        if prior is None or prior[0] != mw.link or prior[1] != "active":
            coordinator.maintenance_notified_key = (mw.link, state)
            getattr(coordinator, "_persist_maint_notified_key", lambda: None)()
            return
    notify_key = (mw.link, state)
    if coordinator.maintenance_notified_key == notify_key:
        return
    from .fcm import build_notify_data, get_alert_services

    services = get_alert_services(coordinator, "system")
    if not services:
        _LOGGER.debug("Maintenance announce skipped: no notify service configured")
        coordinator.maintenance_notified_key = notify_key
        getattr(coordinator, "_persist_maint_notified_key", lambda: None)()
        return
    from zoneinfo import ZoneInfo

    when = ""
    if mw.scheduled_start and mw.scheduled_end:
        tz = ZoneInfo("Europe/Berlin")
        start = mw.scheduled_start.astimezone(tz)
        end = mw.scheduled_end.astimezone(tz)
        when = f"{start.strftime('%a %d.%m. %H:%M')}–{end.strftime('%H:%M')}"
    verb_map = {"scheduled": "geplant", "active": "läuft", "past": "beendet"}
    verb = verb_map[state]
    title = f"Bosch Cloud-Wartung {verb}"
    body_lines = [mw.title or "Wartungsmeldung"]
    if when:
        body_lines.append(when)
    if state == "active":
        body_lines.append("Live-Bild und Snapshots ggf. eingeschränkt.")
    elif state == "past":
        body_lines.append("Cloud-Dienste sollten wieder normal funktionieren.")
    if mw.link:
        body_lines.append(mw.link)
    message = "\n".join(body_lines)
    for svc in services:
        try:
            data = build_notify_data(svc, message, title=title)
            # `alert_notify_service` option stores entries like `notify.<svc>`
            # OR bare service names `<svc>`. Mirror the FCM-side split so
            # `hass.services.async_call("notify", "<svc>", ...)` resolves
            # correctly. Pre-fix: hardcoded "notify" + svc="notify.<svc>"
            # produced `notify.notify.<svc>` and silently failed.
            _domain, _service = svc.split(".", 1) if "." in svc else ("notify", svc)
            await coordinator.hass.services.async_call(
                _domain, _service, data, blocking=False
            )
            _LOGGER.info(
                "Maintenance announce sent via notify.%s (state=%s, window=%s)",
                svc,
                state,
                when or "(no window)",
            )
        except Exception as exc:
            _LOGGER.warning(
                "Maintenance announce via notify.%s failed: %s",
                svc,
                exc,
            )
    coordinator.maintenance_notified_key = notify_key
    getattr(coordinator, "_persist_maint_notified_key", lambda: None)()


async def maybe_announce_camera_status(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    new_status: str,
) -> None:
    """Fire a notification when a camera flips between online and offline.

    The first observation per camera is silent — we record the baseline
    without notifying so a HA restart while a camera is offline does not
    re-announce the existing state. Only `online → offline` and
    `offline → online` transitions notify; `unknown` is treated as a
    non-event (camera info is just temporarily missing, not a real
    availability change).

    Routing matches the maintenance path: `alert_notify_system` falls
    back to `alert_notify_service`. Notify failures are swallowed.
    """
    # Lazy-init for SimpleNamespace test stubs that bypass __init__. The
    # real coordinator always sets a `FloatFieldView` here (Session-
    # State-Facade Slice 1, see session_state.py) — a plain dict is only
    # ever assigned on a bare test stub, never on the real class, hence
    # the type: ignore.
    if not hasattr(coordinator, "_offline_seen_at"):
        coordinator._offline_seen_at = {}  # type: ignore[assignment]
    last = coordinator._last_camera_status.get(cam_id)
    if last is None:
        # First tick after startup — record baseline silently.
        coordinator._last_camera_status[cam_id] = new_status
        return
    # Whenever the camera is currently online, drop any pending offline-grace
    # timer (covers recovery within the grace window AND the no-op
    # online→online tick below).
    if new_status == "online":
        coordinator._offline_seen_at.pop(cam_id, None)
    if new_status == last:
        return
    # Skip transitions involving "unknown" — coordinator hickups can flap
    # status to UNKNOWN for one tick during cloud transients; do not
    # convert that into spam.
    if new_status == "unknown" or last == "unknown":
        coordinator._last_camera_status[cam_id] = new_status
        return
    # Offline-announce grace: a camera on a Wi-Fi repeater/mesh briefly drops
    # during a repeater restart or DFS channel change and recovers within a
    # minute or two. Only announce offline once it has stayed offline for
    # CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC; a recovery within the window is
    # silent. We hold the baseline at "online" (don't commit the flip) until
    # the grace elapses, so the eventual recovery doesn't emit a spurious
    # "online" notification either.
    if new_status == "offline":
        # Local import (not top-level): CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC is a
        # module-level constant on coordinator.py itself (not const.py), and
        # coordinator.py imports this module at its own top level — a
        # top-level import here would be circular. Tests patch/import this
        # constant via `custom_components.bosch_shc_camera.coordinator.
        # CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC`, so it must stay defined there.
        from .coordinator import CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC

        seen = coordinator._offline_seen_at.get(cam_id)
        now_mono = time.monotonic()
        if seen is None:
            coordinator._offline_seen_at[cam_id] = now_mono
            return
        if (now_mono - seen) < CAMERA_OFFLINE_ANNOUNCE_GRACE_SEC:
            return
    coordinator._last_camera_status[cam_id] = new_status
    from .fcm import build_notify_data, get_alert_services

    services = get_alert_services(coordinator, "system")
    cam_info = coordinator.data.get(cam_id, {}).get("info", {})
    cam_name = cam_info.get("title") or cam_id[:8]
    if not services:
        _LOGGER.debug(
            "Camera status announce skipped for %s (%s→%s): no notify service configured",
            cam_name,
            last,
            new_status,
        )
        return
    if new_status == "offline":
        title = f"Bosch Kamera {cam_name} offline"
        message = (
            f"Bosch Kamera {cam_name} ist offline. "
            "Live-Bild und Snapshots sind bis zur Wiederverbindung nicht verfügbar."
        )
    else:
        title = f"Bosch Kamera {cam_name} wieder online"
        message = f"Bosch Kamera {cam_name} ist wieder erreichbar."
    for svc in services:
        try:
            data = build_notify_data(svc, message, title=title)
            # `alert_notify_service` option stores entries like `notify.<svc>`
            # OR bare service names `<svc>`. Mirror the FCM-side split so
            # `hass.services.async_call("notify", "<svc>", ...)` resolves
            # correctly. Pre-fix: hardcoded "notify" + svc="notify.<svc>"
            # produced `notify.notify.<svc>` and silently failed.
            _domain, _service = svc.split(".", 1) if "." in svc else ("notify", svc)
            await coordinator.hass.services.async_call(
                _domain, _service, data, blocking=False
            )
            _LOGGER.info(
                "Camera status announce sent via notify.%s for %s (%s→%s)",
                svc,
                cam_name,
                last,
                new_status,
            )
        except Exception as exc:
            _LOGGER.warning(
                "Camera status announce via notify.%s for %s failed: %s",
                svc,
                cam_name,
                exc,
            )


async def maybe_announce_cloud_state(
    coordinator: BoschCameraCoordinator, success: bool
) -> None:
    """Fire a user notification on cloud-reachability transitions.

    Outage path: when ``success=False`` for at least
    ``_CLOUD_OUTAGE_NOTIFY_AFTER_S`` seconds in a row, fire a one-shot
    "Bosch Cloud nicht erreichbar" notification. Recovery path: when the
    next ``success=True`` arrives after an outage was announced, fire
    "Bosch Cloud wieder erreichbar". One-tick failure blips never get
    announced — they self-clear on the next success.

    Suppressed while an RSS-announced maintenance window is `active`
    because the maintenance lifecycle notifier (v12.4.8) already told
    the user. We still record state transitions internally so we are
    able to announce a recovery once the window closes if needed.

    Routing: `alert_notify_system` → falls back to
    `alert_notify_service`, same path as TROUBLE_DISCONNECT and the
    maintenance announcements. Notify failures are swallowed.
    """
    now = time.monotonic()
    # Active-maintenance check — if Bosch announced this exact outage as
    # planned, stay silent.
    in_maintenance = False
    mw = coordinator.maintenance_cache
    if mw is not None and mw.camera_relevant and mw.state() == "active":
        in_maintenance = True
    if success:
        if not coordinator.cloud_outage_notified:
            # Was either healthy already or in a sub-grace blip — just
            # reset the tracker so the next outage starts a fresh window.
            coordinator._cloud_outage_started_at = None
            return
        # We previously announced an outage — announce recovery now.
        coordinator.cloud_outage_notified = False
        coordinator._cloud_outage_started_at = None
        getattr(coordinator, "_persist_cloud_outage_flag", lambda: None)()
        if in_maintenance:
            _LOGGER.debug("Cloud recovered during active maintenance — staying silent")
            return
        await _dispatch_cloud_alert(coordinator, recovered=True)
        return
    # success=False
    if coordinator._cloud_outage_started_at is None:
        coordinator._cloud_outage_started_at = now
        return
    if coordinator.cloud_outage_notified:
        return
    if (
        now - coordinator._cloud_outage_started_at
    ) < coordinator._CLOUD_OUTAGE_NOTIFY_AFTER_S:
        return
    # Outage has persisted long enough → announce, but stay silent during
    # known maintenance.
    coordinator.cloud_outage_notified = True
    getattr(coordinator, "_persist_cloud_outage_flag", lambda: None)()
    if in_maintenance:
        _LOGGER.debug("Cloud outage suppressed: known active maintenance window")
        return
    await _dispatch_cloud_alert(coordinator, recovered=False)


async def _dispatch_cloud_alert(
    coordinator: BoschCameraCoordinator, *, recovered: bool
) -> None:
    """Send the actual notification through the integration's alert pipeline."""
    from .fcm import build_notify_data, get_alert_services

    services = get_alert_services(coordinator, "system")
    if not services:
        _LOGGER.debug(
            "Cloud-state alert skipped (recovered=%s) — no notify service configured",
            recovered,
        )
        return
    if recovered:
        title = "Bosch Cloud wieder erreichbar"
        message = (
            "Die Bosch-Cloud antwortet wieder. "
            "Snapshots und Stream-Anfragen laufen normal."
        )
    else:
        title = "Bosch Cloud nicht erreichbar"
        message = (
            "Die Bosch-Cloud antwortet nicht mehr (HTTP 5xx / Timeout). "
            "Privacy- und Licht-Schalter gehen weiter über LAN, "
            "Snapshots und Stream-Anfragen sind eingeschränkt."
        )
    for svc in services:
        try:
            data = build_notify_data(svc, message, title=title)
            # `alert_notify_service` option stores entries like `notify.<svc>`
            # OR bare service names `<svc>`. Mirror the FCM-side split so
            # `hass.services.async_call("notify", "<svc>", ...)` resolves
            # correctly. Pre-fix: hardcoded "notify" + svc="notify.<svc>"
            # produced `notify.notify.<svc>` and silently failed.
            _domain, _service = svc.split(".", 1) if "." in svc else ("notify", svc)
            await coordinator.hass.services.async_call(
                _domain, _service, data, blocking=False
            )
            _LOGGER.info(
                "Cloud-state alert sent via notify.%s (recovered=%s)",
                svc,
                recovered,
            )
        except Exception as exc:
            _LOGGER.warning(
                "Cloud-state alert via notify.%s failed: %s",
                svc,
                exc,
            )
