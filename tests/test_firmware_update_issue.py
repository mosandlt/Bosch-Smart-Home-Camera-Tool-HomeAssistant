"""Regression tests for Repairs issue when a camera firmware update is available.

Bug (Thomas, live report 2026-07-07): a camera firmware update became available
but no alert was shown anywhere in Home Assistant — the integration had no
active detection/notification code path at all for this; the only signal was
the generic HA core Settings -> Updates panel (easy to miss). Fixed by mirroring
the existing _refresh_notifications_disabled_issues Repairs-issue pattern.

_refresh_firmware_update_issues() (called every coordinator tick):
  - upToDate=False           -> ir.async_create_issue (is_fixable=True,
                                 data={"cam_id": ...}) + INFO log (once)
  - upToDate=True            -> ir.async_delete_issue, alerted set cleared
  - upToDate missing/None    -> neither create nor delete (partial payload)
  - empty/missing cache entry -> neither create nor delete (no data fetched yet)
  - INFO fires only once per camera; re-fires only after an install->new-update cycle

is_fixable=True + the cam_id in `data` let the Repairs "Fix" button install
the update directly (repairs.py, FirmwareUpdateRepairFlow) instead of only
pointing the user at the separate Firmware entity's Install button.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"

# Fake camera IDs — NEVER use real device values (SECRETS_SCAN rule).
CAM_A = "11111111-0000-0000-0000-000000000001"
CAM_B = "11111111-0000-0000-0000-000000000002"

ISSUE_ID_A = f"firmware_update_available_{CAM_A}"
ISSUE_ID_B = f"firmware_update_available_{CAM_B}"

DOMAIN = "bosch_shc_camera"


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_coord(
    firmware: dict[str, dict[str, object]],
    cam_titles: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Minimal coordinator stub for _refresh_firmware_update_issues."""
    titles = cam_titles or {}

    data: dict[str, dict[str, object]] = {
        cam_id: {"info": {"title": titles.get(cam_id, cam_id)}} for cam_id in firmware
    }

    coord = SimpleNamespace(
        hass=SimpleNamespace(),
        data=data,
        _firmware_cache=firmware,
        _fw_update_alerted=set(),
    )
    return coord


def _call_method(coord: SimpleNamespace) -> None:
    """Invoke _refresh_firmware_update_issues on the stub coordinator
    by importing and binding it directly (avoids instantiating the full class).
    """
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    BoschCameraCoordinator._refresh_firmware_update_issues(coord)  # type: ignore[arg-type]


# ── test cases ────────────────────────────────────────────────────────────────


class TestFirmwareUpdateAvailableRepairs:
    """Pin every mode: update-available, up-to-date, partial payload, empty."""

    @patch(f"{MODULE}.ir")
    def test_update_available_creates_issue(self, mock_ir: MagicMock) -> None:
        """upToDate=False -> create_issue with correct issue_id and placeholders."""
        coord = _make_coord(
            {
                CAM_A: {
                    "current": "9.40.102",
                    "upToDate": False,
                    "update": "9.40.104",
                }
            },
            {CAM_A: "Terrasse"},
        )
        _call_method(coord)

        mock_ir.async_create_issue.assert_called_once_with(
            coord.hass,
            DOMAIN,
            ISSUE_ID_A,
            is_fixable=True,
            is_persistent=False,
            severity=mock_ir.IssueSeverity.WARNING,
            translation_key="firmware_update_available",
            translation_placeholders={
                "camera": "Terrasse",
                "current": "9.40.102",
                "latest": "9.40.104",
            },
            data={"cam_id": CAM_A},
        )
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_up_to_date_deletes_issue(self, mock_ir: MagicMock) -> None:
        """upToDate=True -> async_delete_issue, no create."""
        coord = _make_coord(
            {CAM_A: {"current": "9.40.104", "upToDate": True, "update": None}},
        )
        _call_method(coord)

        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, ISSUE_ID_A
        )
        mock_ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_missing_up_to_date_key_fires_nothing(self, mock_ir: MagicMock) -> None:
        """Partial payload (upToDate absent) -> indeterminate, no create/delete."""
        coord = _make_coord({CAM_A: {"current": "9.40.102"}})
        _call_method(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_empty_firmware_cache_entry_fires_nothing(self, mock_ir: MagicMock) -> None:
        """Empty dict (no data fetched yet) -> neither create nor delete."""
        coord = _make_coord({CAM_A: {}})
        _call_method(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_missing_camera_in_cache_fires_nothing(self, mock_ir: MagicMock) -> None:
        """Camera not in _firmware_cache at all -> nothing fires."""
        coord = _make_coord({})
        _call_method(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_info_logged_once_per_camera(
        self, mock_ir: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """INFO is emitted on the first update-available tick but NOT on the second."""
        coord = _make_coord(
            {CAM_A: {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}},
            {CAM_A: "Terrasse"},
        )
        with caplog.at_level(logging.INFO, logger=MODULE):
            _call_method(coord)  # first call — should log
            _call_method(coord)  # second call — must NOT log again

        info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_msgs) == 1
        assert "Firmware update available" in info_msgs[0].message

    @patch(f"{MODULE}.ir")
    def test_info_refires_after_install_then_new_update(
        self, mock_ir: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After an available->installed->available-again cycle, INFO fires again."""
        coord = _make_coord(
            {CAM_A: {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}},
            {CAM_A: "Terrasse"},
        )
        with caplog.at_level(logging.INFO, logger=MODULE):
            _call_method(coord)  # log #1 -> alerted set = {CAM_A}

        # Simulate the update installing (upToDate flips True).
        coord._firmware_cache[CAM_A] = {
            "current": "9.40.104",
            "upToDate": True,
            "update": None,
        }
        _call_method(coord)  # deletes issue, discards from set
        assert CAM_A not in coord._fw_update_alerted

        # A new update becomes available -> should log again.
        coord._firmware_cache[CAM_A] = {
            "current": "9.40.104",
            "upToDate": False,
            "update": "9.40.110",
        }
        with caplog.at_level(logging.INFO, logger=MODULE):
            _call_method(coord)  # log #2

        info_msgs = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(info_msgs) == 2

    @patch(f"{MODULE}.ir")
    def test_multiple_cameras_independent(self, mock_ir: MagicMock) -> None:
        """Two cameras processed independently; only the outdated one creates an issue."""
        coord = _make_coord(
            {
                CAM_A: {
                    "current": "9.40.102",
                    "upToDate": False,
                    "update": "9.40.104",
                },
                CAM_B: {"current": "9.40.104", "upToDate": True, "update": None},
            },
            {CAM_A: "Terrasse", CAM_B: "Garten"},
        )
        _call_method(coord)

        create_calls = mock_ir.async_create_issue.call_args_list
        delete_calls = mock_ir.async_delete_issue.call_args_list

        assert any(ISSUE_ID_A in str(c) for c in create_calls)
        assert any(ISSUE_ID_B in str(c) for c in delete_calls)
        assert not any(ISSUE_ID_B in str(c) for c in create_calls)

    @patch(f"{MODULE}.ir")
    def test_missing_update_version_falls_back_to_placeholder(
        self, mock_ir: MagicMock
    ) -> None:
        """upToDate=False but no 'update' field -> latest placeholder is '?'."""
        coord = _make_coord(
            {CAM_A: {"current": "9.40.102", "upToDate": False}},
        )
        _call_method(coord)

        _, kwargs = mock_ir.async_create_issue.call_args
        assert kwargs["translation_placeholders"]["latest"] == "?"
        assert kwargs["translation_placeholders"]["current"] == "9.40.102"

    @patch(f"{MODULE}.ir")
    def test_cam_title_fallback_to_cam_id(self, mock_ir: MagicMock) -> None:
        """If title not in data, cam_id is used as fallback camera placeholder."""
        coord = _make_coord(
            {CAM_A: {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}},
        )
        _call_method(coord)

        _, kwargs = mock_ir.async_create_issue.call_args
        assert kwargs["translation_placeholders"]["camera"] == CAM_A
