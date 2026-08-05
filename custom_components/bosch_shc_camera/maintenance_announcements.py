"""Maintenance-feed fetch + the small persisted-flag/notification helpers
that ride alongside it: the Bosch community maintenance-window fetch, the
two dedup-flag `Store` persistence helpers used by `announcements.py`'s
maintenance/cloud-state notifiers, and the session-quota (HTTP 444)
persistent_notification.

Extracted from `coordinator.py` (style audit, 2026-08-05) — every
function here does real `self.hass`/`Store`/`persistent_notification`
side-effecting work (background fetch, disk persistence, or a service
call), unlike the single pure status-derivation helper split out
alongside it into `status_compute.py` in the same round. Matches the
`announcements.py`/`repairs.py`/`device_actions.py` pattern already
established here: free functions taking the coordinator instance as
their first argument.

`BoschCameraCoordinator` keeps a thin delegating method for each of
these (same name/signature, calls straight into the matching function
here) so every existing call site keeps working unchanged:
  - `_async_refresh_maintenance`: `tick_failure.py`/`tick_housekeeping.py`/
    `camera_list.py` all reach it via `getattr(coordinator,
    "_async_refresh_maintenance", None)`, and the test suite both mocks
    it as an attribute (`coord._async_refresh_maintenance = AsyncMock()`)
    and calls it unbound (`BoschCameraCoordinator._async_refresh_
    maintenance(coord, reactive=...)`) — see tests/test_maintenance.py,
    tests/test_tick_failure.py, tests/test_tick_housekeeping.py,
    tests/test_camera_list.py.
  - `_persist_maint_notified_key`/`_persist_cloud_outage_flag`:
    `announcements.py` reaches both via `getattr(coordinator, "...",
    lambda: None)()` (safe no-op for stub coordinators that don't define
    them) — removing the coordinator-level attribute in favor of an
    unconditional free-function call would silently change that tested
    contract, same reasoning as `announcements.py`'s own module
    docstring.
  - `_async_handle_session_quota_hit`: `camera_status.py` reaches it via
    `getattr(coordinator, "_async_handle_session_quota_hit", None)`, and
    tests both mock it as an attribute and call it unbound — see
    tests/test_camera_status.py, tests/test_sensor.py, tests/test_init.py.

IMPORTANT: `_async_refresh_maintenance` originally called
`self._async_maybe_announce_maintenance(result)` — the extracted
`async_refresh_maintenance` below keeps calling through the COORDINATOR
instance (`coordinator._async_maybe_announce_maintenance(...)`) rather
than `announcements.maybe_announce_maintenance` directly, since that
coordinator method is itself patched per-instance by several tests (see
tests/test_maintenance.py) and calling the module function directly
would silently bypass any such patch.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_refresh_maintenance(
    coordinator: BoschCameraCoordinator, *, reactive: bool
) -> None:
    """Fetch the Bosch community maintenance announcement in the background.

    Reactive calls (triggered by cloud 5xx/timeout) are rate-limited so a
    flapping cloud does not hammer the community site. Periodic calls run
    once per _MAINTENANCE_INTERVAL_S regardless of cloud health.

    Failure is silent — the previous cache value is retained so the sensor
    does not flap on a transient community-site outage.
    """
    # Local import (not top-level): keeps unittest.mock.patch(
    # "custom_components.bosch_shc_camera.async_get_clientsession", ...)
    # working the same way it did when this lived on the coordinator —
    # matches the live_connection.py/rcp_client.py pattern.
    from . import (
        async_get_clientsession as async_get_clientsession,
    )
    from .maintenance import async_fetch_maintenance

    now = time.monotonic()
    if (
        reactive
        and (now - coordinator.maintenance_last_fetch)
        < coordinator._MAINTENANCE_REACTIVE_COOLDOWN_S
    ):
        return
    coordinator.maintenance_last_fetch = now
    try:
        session = async_get_clientsession(coordinator.hass)
        result = await async_fetch_maintenance(session)
    except Exception as exc:
        _LOGGER.debug("Maintenance fetch raised: %s", exc)
        return
    if result is not None:
        coordinator.maintenance_cache = result
        _LOGGER.debug(
            "Maintenance: %s state=%s window=%s..%s",
            result.title[:60],
            result.state(),
            result.scheduled_start,
            result.scheduled_end,
        )
        await coordinator._async_maybe_announce_maintenance(result)


def persist_maint_notified_key(coordinator: BoschCameraCoordinator) -> None:
    """Write `maintenance_notified_key` to disk so HA restarts mid-
    window do not re-fire the active-state announcement on the next
    coordinator tick — otherwise every restart wipes the in-memory
    dedup key and a single maintenance window can produce dozens of
    duplicate alerts.
    """
    key = coordinator.maintenance_notified_key
    store = getattr(coordinator, "maint_notified_store", None)
    if store is None or key is None:
        return
    coordinator.hass.async_create_task(
        store.async_save({"link": key[0], "state": key[1]})
    )


def persist_cloud_outage_flag(coordinator: BoschCameraCoordinator) -> None:
    """Mirror the maintenance-key persistence for the cloud-state
    dedup flag, so a restart mid-outage doesn't re-fire "Cloud nicht
    erreichbar"."""
    store = getattr(coordinator, "cloud_alert_store", None)
    if store is None:
        return
    # Tracked (not a bare hass.async_create_task) — an untracked save
    # can still complete after config-entry removal deletes the Store,
    # recreating integration-owned state on disk after removal and
    # bypassing the teardown behavior spawn_tracked() documents.
    coordinator.spawn_tracked(
        store.async_save({"outage_notified": bool(coordinator.cloud_outage_notified)}),
        name="bosch_shc_camera_persist_cloud_outage_flag",
    )


async def async_handle_session_quota_hit(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> None:
    """Track HTTP 444 hits per camera and fire a persistent notification if repeated.

    After _SESSION_QUOTA_NOTIFY_THRESHOLD (3) hits within _SESSION_QUOTA_WINDOW_S (5 min)
    a HA persistent_notification is created advising the user to close other clients.
    Non-fatal — any failure is swallowed so the caller's status update is unaffected.
    """
    try:
        now = time.monotonic()
        hits = coordinator._session_quota_hits.setdefault(cam_id, [])
        # Prune hits outside the window
        hits[:] = [t for t in hits if (now - t) < coordinator._SESSION_QUOTA_WINDOW_S]
        hits.append(now)

        if len(hits) >= coordinator._SESSION_QUOTA_NOTIFY_THRESHOLD:
            cam_info = (
                coordinator.data.get(cam_id, {}).get("info", {})
                if coordinator.data
                else {}
            )
            cam_name = cam_info.get("title") or cam_id[:8]
            notification_id = f"bosch_session_quota_{cam_id[:8].lower()}"
            title = f"Bosch Kamera {cam_name}: Sitzungslimit erreicht"
            message = (
                f"Kamera {cam_name} meldet HTTP 444 (Session-Quota). "
                "Zu viele gleichzeitige Live-Verbindungen im Bosch-Konto. "
                "Bitte schließen Sie die Bosch App auf weiteren Geräten "
                "oder deaktivieren Sie parallele Integrationen (ioBroker, Python CLI). "
                "Die Integration wiederholt den Verbindungsaufbau automatisch."
            )
            await coordinator.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": title,
                    "message": message,
                    "notification_id": notification_id,
                },
                blocking=False,
            )
            _LOGGER.warning(
                "Session-quota persistent notification created for %s (%d hits in %.0fs)",
                cam_id[:8],
                len(hits),
                coordinator._SESSION_QUOTA_WINDOW_S,
            )
    except Exception as exc:
        _LOGGER.debug("Session-quota notification failed (non-fatal): %s", exc)
