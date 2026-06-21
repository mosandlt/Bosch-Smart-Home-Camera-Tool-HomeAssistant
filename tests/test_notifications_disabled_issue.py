"""Regression tests for Repairs issue when camera notifications are disabled.

When Bosch cloud notifications (movement / person) are disabled for a camera,
the corresponding binary sensors stay permanently "Clear" with no error shown
to the user (issue #36 follow-up).

_refresh_notifications_disabled_issues() (called every coordinator tick):
  - disabled notification(s) → ir.async_create_issue + WARN log (once)
  - all enabled          → ir.async_delete_issue, logged set cleared
  - empty/missing dict   → neither create nor delete (no false-positive on startup)
  - WARN fires only once per camera; re-fires only after a clear→re-disabled cycle
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera"
ISSUE_MODULE = "custom_components.bosch_shc_camera.ir"

# Fake camera IDs — NEVER use real device values (SECRETS_SCAN rule).
CAM_A = "11111111-0000-0000-0000-000000000001"
CAM_B = "11111111-0000-0000-0000-000000000002"

ISSUE_ID_A = f"notifications_disabled_{CAM_A}"
ISSUE_ID_B = f"notifications_disabled_{CAM_B}"

DOMAIN = "bosch_shc_camera"


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_coord(
    notifications: dict[str, dict[str, object]],
    cam_titles: dict[str, str] | None = None,
) -> SimpleNamespace:
    """Minimal coordinator stub for _refresh_notifications_disabled_issues."""
    titles = cam_titles or {}

    # Build self.data structure: {cam_id: {"info": {"title": ...}}}
    data: dict[str, dict[str, object]] = {
        cam_id: {"info": {"title": titles.get(cam_id, cam_id)}}
        for cam_id in notifications
    }

    coord = SimpleNamespace(
        hass=SimpleNamespace(),
        data=data,
        _notifications_cache=notifications,
        _notif_disabled_logged=set(),
    )
    return coord


def _call_method(coord: SimpleNamespace) -> None:
    """Invoke _refresh_notifications_disabled_issues on the stub coordinator
    by importing and binding it directly (avoids instantiating the full class).
    """
    from custom_components.bosch_shc_camera import (
        BoschCameraCoordinator,
    )

    BoschCameraCoordinator._refresh_notifications_disabled_issues(coord)  # type: ignore[arg-type]


# ── test cases ────────────────────────────────────────────────────────────────


class TestNotificationsDisabledRepairs:
    """Pin every mode: movement-disabled, person-disabled, both, all-on, empty."""

    @patch(f"{MODULE}.ir")
    def test_movement_disabled_creates_issue(self, mock_ir: MagicMock) -> None:
        """movement=False → create_issue with correct issue_id and placeholders."""
        coord = _make_coord(
            {CAM_A: {"movement": False, "person": True, "audio": True}},
            {CAM_A: "Terrasse"},
        )
        _call_method(coord)

        mock_ir.async_create_issue.assert_called_once_with(
            coord.hass,
            DOMAIN,
            ISSUE_ID_A,
            is_fixable=False,
            is_persistent=False,
            severity=mock_ir.IssueSeverity.WARNING,
            translation_key="notifications_disabled",
            translation_placeholders={
                "camera": "Terrasse",
                "types": "Movement",
            },
        )
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_person_disabled_creates_issue(self, mock_ir: MagicMock) -> None:
        """person=False → create_issue with types='Person'."""
        coord = _make_coord(
            {CAM_A: {"movement": True, "person": False, "audio": True}},
            {CAM_A: "Garten"},
        )
        _call_method(coord)

        mock_ir.async_create_issue.assert_called_once()
        _, kwargs = mock_ir.async_create_issue.call_args
        assert kwargs["translation_placeholders"]["types"] == "Person"
        assert kwargs["translation_placeholders"]["camera"] == "Garten"
        assert kwargs["translation_key"] == "notifications_disabled"

    @patch(f"{MODULE}.ir")
    def test_both_disabled_creates_issue_with_combined_types(
        self, mock_ir: MagicMock
    ) -> None:
        """movement=False AND person=False → types='Movement + Person'."""
        coord = _make_coord(
            {CAM_A: {"movement": False, "person": False, "audio": True}},
            {CAM_A: "Innenbereich"},
        )
        _call_method(coord)

        mock_ir.async_create_issue.assert_called_once()
        _, kwargs = mock_ir.async_create_issue.call_args
        assert kwargs["translation_placeholders"]["types"] == "Movement + Person"

    @patch(f"{MODULE}.ir")
    def test_both_enabled_deletes_issue(self, mock_ir: MagicMock) -> None:
        """movement=True, person=True → async_delete_issue, no create."""
        coord = _make_coord(
            {CAM_A: {"movement": True, "person": True, "audio": True}},
        )
        _call_method(coord)

        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, ISSUE_ID_A
        )
        mock_ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_empty_notifications_dict_fires_nothing(self, mock_ir: MagicMock) -> None:
        """Empty dict (no data fetched yet) → neither create nor delete."""
        coord = _make_coord({CAM_A: {}})
        _call_method(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_missing_camera_in_cache_fires_nothing(self, mock_ir: MagicMock) -> None:
        """Camera not in _notifications_cache at all → nothing fires."""
        coord = _make_coord({})
        _call_method(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_warn_logged_once_per_camera(
        self, mock_ir: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """WARN is emitted on the first disabled tick but NOT on the second."""
        coord = _make_coord(
            {CAM_A: {"movement": False, "person": True}},
            {CAM_A: "Terrasse"},
        )
        with caplog.at_level(logging.WARNING, logger=MODULE):
            _call_method(coord)  # first call — should warn
            _call_method(coord)  # second call — must NOT warn again

        warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_msgs) == 1
        assert (
            "Movement" in warn_msgs[0].message
            or "movement" in warn_msgs[0].message.lower()
        )

    @patch(f"{MODULE}.ir")
    def test_warn_refires_after_re_enable_then_re_disable(
        self, mock_ir: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After a disable→enable→disable cycle the WARN fires again."""
        coord = _make_coord(
            {CAM_A: {"movement": False, "person": True}},
            {CAM_A: "Terrasse"},
        )
        with caplog.at_level(logging.WARNING, logger=MODULE):
            _call_method(coord)  # warn #1 → logged set = {CAM_A}

        # Simulate re-enable (clear → discard from logged set)
        coord._notifications_cache[CAM_A] = {"movement": True, "person": True}
        _call_method(coord)  # deletes issue, discards from set
        assert CAM_A not in coord._notif_disabled_logged

        # Re-disable → should warn again
        coord._notifications_cache[CAM_A] = {"movement": False, "person": True}
        with caplog.at_level(logging.WARNING, logger=MODULE):
            _call_method(coord)  # warn #2

        warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_msgs) == 2

    @patch(f"{MODULE}.ir")
    def test_multiple_cameras_independent(self, mock_ir: MagicMock) -> None:
        """Two cameras processed independently; only the disabled one creates an issue."""
        coord = _make_coord(
            {
                CAM_A: {"movement": False, "person": True},
                CAM_B: {"movement": True, "person": True},
            },
            {CAM_A: "Terrasse", CAM_B: "Garten"},
        )
        _call_method(coord)

        create_calls = mock_ir.async_create_issue.call_args_list
        delete_calls = mock_ir.async_delete_issue.call_args_list

        # CAM_A has movement disabled → create
        assert any(ISSUE_ID_A in str(c) for c in create_calls)
        # CAM_B all enabled → delete
        assert any(ISSUE_ID_B in str(c) for c in delete_calls)
        # No create for CAM_B
        assert not any(ISSUE_ID_B in str(c) for c in create_calls)

    @patch(f"{MODULE}.ir")
    def test_audio_only_disabled_does_not_trigger(self, mock_ir: MagicMock) -> None:
        """audio=False with movement+person enabled → no issue (audio is not in scope)."""
        coord = _make_coord(
            {CAM_A: {"movement": True, "person": True, "audio": False}},
        )
        _call_method(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, ISSUE_ID_A
        )

    @patch(f"{MODULE}.ir")
    def test_correct_issue_id_per_camera(self, mock_ir: MagicMock) -> None:
        """The Repairs issue ID includes the camera ID (one issue per camera)."""
        coord = _make_coord(
            {CAM_A: {"movement": False, "person": True}},
            {CAM_A: "Cam A"},
        )
        _call_method(coord)

        # Third positional arg is the issue_id
        args, _ = mock_ir.async_create_issue.call_args
        assert args[2] == ISSUE_ID_A

    @patch(f"{MODULE}.ir")
    def test_cam_title_fallback_to_cam_id(self, mock_ir: MagicMock) -> None:
        """If title not in data, cam_id is used as fallback camera placeholder."""
        # Provide no cam_titles — data will be {CAM_A: {"info": {"title": CAM_A}}}
        coord = _make_coord(
            {CAM_A: {"movement": False, "person": True}},
        )
        _call_method(coord)

        _, kwargs = mock_ir.async_create_issue.call_args
        # title falls back to cam_id itself
        assert kwargs["translation_placeholders"]["camera"] == CAM_A
