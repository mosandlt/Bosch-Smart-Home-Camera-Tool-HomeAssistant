"""Bosch Smart Home Camera — Repairs Fix Flows + Repairs-issue lifecycle.

Combines the firmware-update-available Repairs issue (create/clear side,
`refresh_firmware_update_issues` below) with the install action
(device_actions.async_install_firmware, also used by update.py's Install
button) — pressing "Fix" on the Repairs issue installs the update
directly instead of only pointing the user at a separate button
elsewhere.

Also owns the two sibling per-tick Repairs-issue lifecycle checks
(notifications-disabled, SMB-unavailable) alongside the firmware one,
since all three share the exact same idempotent create/delete
issue_registry pattern. `BoschCameraCoordinator` keeps a thin delegating
method for each (same name) so `_async_update_data`'s existing call
sites and the test suite's unbound-method-call patterns keep working
unchanged — see the docstring on `announcements.py` for the fuller
rationale (this module follows the same thin-wrapper convention).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN
from .smb import smb_available, smb_dependent_features

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)


class FirmwareUpdateRepairFlow(RepairsFlow):  # type: ignore[misc]
    """Confirm, then install the pending firmware update for one camera."""

    def __init__(self, coordinator: Any, cam_id: str) -> None:
        self._coordinator = coordinator
        self._cam_id = cam_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if self._coordinator is None:
            return self.async_abort(reason="install_failed")
        if user_input is not None:
            try:
                await self._coordinator.async_install_firmware(self._cam_id)
            except HomeAssistantError:
                return self.async_abort(reason="install_failed")
            return self.async_create_entry(data={})

        fw: dict[str, Any] = self._coordinator.firmware_cache.get(self._cam_id, {})
        cam_title: str = (
            (self._coordinator.data or {})
            .get(self._cam_id, {})
            .get("info", {})
            .get("title", self._cam_id)
        )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "camera": cam_title,
                "current": fw.get("current") or "?",
                "latest": fw.get("update") or "?",
            },
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Return the fix flow for a given Repairs issue.

    Only `firmware_update_available_*` issues are fixable today — `data`
    carries the `cam_id` stashed at ir.async_create_issue() time.
    """
    cam_id = str((data or {}).get("cam_id", ""))
    coordinator = None
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.runtime_data is not None:
            coordinator = entry.runtime_data
            break
    return FirmwareUpdateRepairFlow(coordinator, cam_id)


def refresh_notifications_disabled_issues(
    coordinator: BoschCameraCoordinator,
) -> None:
    """Create or clear Repairs issues for cameras with disabled movement/person notifications.

    Called once per coordinator tick (inside _async_update_data) AFTER data is
    built.  Idempotent — safe to call every tick.

    A camera is only processed when its notifications dict is non-empty
    (i.e. the endpoint has been fetched at least once).  Cameras with no
    notification data yet are skipped entirely to avoid false-positive
    issues on startup.
    """
    # Local import (not top-level): keeps unittest.mock.patch(
    # "custom_components.bosch_shc_camera.ir", ...) working the same
    # way it did before this check moved out of coordinator.py — matches
    # the pattern already used in live_connection.py.
    from . import ir as ir  # type: ignore[attr-defined]

    for cam_id, notif in coordinator.notifications_cache.items():
        if not notif:
            # No data fetched yet — skip to avoid false positives.
            continue

        disabled = [t for t in ("movement", "person") if notif.get(t) is False]

        if disabled:
            cam_title: str = (
                (coordinator.data or {})
                .get(cam_id, {})
                .get("info", {})
                .get("title", cam_id)
            )
            types_str = " + ".join(t.capitalize() for t in disabled)
            ir.async_create_issue(
                coordinator.hass,
                DOMAIN,
                f"notifications_disabled_{cam_id}",
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="notifications_disabled",
                translation_placeholders={
                    "camera": cam_title,
                    "types": types_str,
                },
            )
            if cam_id not in coordinator._notif_disabled_logged:
                coordinator._notif_disabled_logged.add(cam_id)
                _LOGGER.warning(
                    "Camera %r has %s cloud notification(s) disabled — "
                    "the corresponding binary sensor(s) will stay 'Clear'. "
                    "Enable the notification switch(es) in Home Assistant or "
                    "the Bosch Smart Home app.",
                    cam_title,
                    types_str,
                )
        else:
            ir.async_delete_issue(
                coordinator.hass,
                DOMAIN,
                f"notifications_disabled_{cam_id}",
            )
            coordinator._notif_disabled_logged.discard(cam_id)


def refresh_firmware_update_issues(coordinator: BoschCameraCoordinator) -> None:
    """Create or clear Repairs issues for cameras with a firmware update available.

    Called once per coordinator tick (inside _async_update_data) AFTER data is
    built. Idempotent — safe to call every tick. Mirrors
    refresh_notifications_disabled_issues (same Repairs-issue pattern):
    previously a firmware update becoming available had NO user-visible
    signal from the integration at all — only HA core's own generic
    Settings → Updates panel, easy to miss.

    A camera is only processed once its firmware endpoint has been fetched
    at least once (`firmware_cache[cam_id]['upToDate']` present) to avoid
    a false-positive "issue cleared" transition on startup.
    """
    # Local import (not top-level): keeps unittest.mock.patch(
    # "custom_components.bosch_shc_camera.ir", ...) working the same
    # way it did before this check moved out of coordinator.py — matches
    # the pattern already used in live_connection.py.
    from . import ir as ir  # type: ignore[attr-defined]

    for cam_id, fw in coordinator.firmware_cache.items():
        if not fw:
            # No data fetched yet — skip to avoid false positives.
            continue

        up_to_date = fw.get("upToDate")
        if up_to_date is None:
            continue

        issue_id = f"firmware_update_available_{cam_id}"

        if not up_to_date:
            cam_title: str = (
                (coordinator.data or {})
                .get(cam_id, {})
                .get("info", {})
                .get("title", cam_id)
            )
            current = fw.get("current") or "?"
            latest = fw.get("update") or "?"
            ir.async_create_issue(
                coordinator.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="firmware_update_available",
                translation_placeholders={
                    "camera": cam_title,
                    "current": current,
                    "latest": latest,
                },
                data={"cam_id": cam_id},
            )
            if cam_id not in coordinator._fw_update_alerted:
                coordinator._fw_update_alerted.add(cam_id)
                _LOGGER.info(
                    "Firmware update available for %r: %s -> %s",
                    cam_title,
                    current,
                    latest,
                )
        else:
            ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
            coordinator._fw_update_alerted.discard(cam_id)


def refresh_smb_unavailable_issue(coordinator: BoschCameraCoordinator) -> None:
    """Create or clear a Repairs issue when smbprotocol is missing but needed.

    Called once per coordinator tick (inside _async_update_data), same
    idempotent create/delete pattern as refresh_notifications_disabled_issues
    and refresh_firmware_update_issues. `smbprotocol` is an optional
    runtime dependency (manifest.json requirement that can fail to install
    on an unsupported OS/architecture) — without this check, a user who
    enables an SMB-dependent feature on such a system gets no signal at
    all beyond a DEBUG/WARNING log line buried in the SMB upload/drain
    code path (sync_smb_upload, recorder._upload_smb), which log-and-skip
    by design so a transient NAS blip never breaks the coordinator tick.
    This makes the "package genuinely missing" case loud instead of
    silently-degraded.

    Not fixable from within HA (installing a Python package isn't
    something a Repairs fix flow can safely do) — the issue tells the
    user to try restarting Home Assistant once (in case install merely
    hadn't completed yet) or to switch the affected feature's storage
    target to Local/FTP instead.
    """
    # Local import (not top-level): keeps unittest.mock.patch(
    # "custom_components.bosch_shc_camera.ir", ...) working the same
    # way it did before this check moved out of coordinator.py — matches
    # the pattern already used in live_connection.py.
    from . import ir as ir  # type: ignore[attr-defined]

    features = smb_dependent_features(coordinator.options)

    issue_id = "smb_unavailable"
    if features and not smb_available():
        features_str = " + ".join(features)
        ir.async_create_issue(
            coordinator.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="smb_unavailable",
            translation_placeholders={"features": features_str},
        )
        if not coordinator._smb_unavailable_logged:
            coordinator._smb_unavailable_logged = True
            _LOGGER.warning(
                "smbprotocol is not installed, but %s %s configured — SMB "
                "upload/recording is disabled until the package is "
                "available. Try restarting Home Assistant once, or switch "
                "the affected feature to a Local/FTP target.",
                features_str,
                "is" if len(features) == 1 else "are",
            )
    else:
        ir.async_delete_issue(coordinator.hass, DOMAIN, issue_id)
        coordinator._smb_unavailable_logged = False
