"""Tests for repairs.py — Repairs "Fix" flow for firmware_update_available.

Pins every mode:
  - async_create_fix_flow resolves the coordinator + cam_id from the issue's
    stashed `data` and returns a FirmwareUpdateRepairFlow
  - no user_input yet -> shows the confirm form with camera/latest placeholders
  - confirm -> calls coordinator.async_install_firmware(cam_id), then
    async_create_entry
  - coordinator raises HomeAssistantError -> async_abort("install_failed")
  - no coordinator resolvable (e.g. entry unloaded) -> async_abort immediately,
    never touches user_input or calls the coordinator
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_ID_2 = "22222222-2222-2222-2222-222222222222"

MODULE = "custom_components.bosch_shc_camera"
DOMAIN = "bosch_shc_camera"


def _make_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        firmware_cache={
            CAM_ID: {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}
        },
        async_install_firmware=AsyncMock(return_value=None),
    )


class TestAsyncCreateFixFlow:
    @pytest.mark.asyncio
    async def test_resolves_coordinator_and_cam_id(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
            async_create_fix_flow,
        )

        coord = _make_coord()
        entry = SimpleNamespace(runtime_data=coord)
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_loaded_entries=lambda domain: [entry])
        )

        flow = await async_create_fix_flow(
            hass, "firmware_update_available_" + CAM_ID, {"cam_id": CAM_ID}
        )

        assert isinstance(flow, FirmwareUpdateRepairFlow)
        assert flow._coordinator is coord
        assert flow._cam_id == CAM_ID

    @pytest.mark.asyncio
    async def test_no_loaded_entry_yields_none_coordinator(self):
        from custom_components.bosch_shc_camera.repairs import async_create_fix_flow

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_loaded_entries=lambda domain: [])
        )

        flow = await async_create_fix_flow(
            hass, "firmware_update_available_" + CAM_ID, {"cam_id": CAM_ID}
        )

        assert flow._coordinator is None
        assert flow._cam_id == CAM_ID

    @pytest.mark.asyncio
    async def test_missing_data_yields_empty_cam_id(self):
        from custom_components.bosch_shc_camera.repairs import async_create_fix_flow

        hass = SimpleNamespace(
            config_entries=SimpleNamespace(async_loaded_entries=lambda domain: [])
        )

        flow = await async_create_fix_flow(hass, "firmware_update_available_x", None)

        assert flow._cam_id == ""


class TestFirmwareUpdateRepairFlow:
    @pytest.mark.asyncio
    async def test_init_step_shows_confirm_form_with_placeholders(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        coord = _make_coord()
        flow = FirmwareUpdateRepairFlow(coord, CAM_ID)

        result = await flow.async_step_init()

        assert result["type"] == "form"
        assert result["step_id"] == "confirm"
        assert result["description_placeholders"] == {
            "camera": "Terrasse",
            "current": "9.40.102",
            "latest": "9.40.104",
        }
        coord.async_install_firmware.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_confirm_installs_and_creates_entry(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        coord = _make_coord()
        flow = FirmwareUpdateRepairFlow(coord, CAM_ID)

        result = await flow.async_step_confirm(user_input={})

        coord.async_install_firmware.assert_awaited_once_with(CAM_ID)
        assert result["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_confirm_install_failure_aborts(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        coord = _make_coord()
        coord.async_install_firmware = AsyncMock(
            side_effect=HomeAssistantError("rejected")
        )
        flow = FirmwareUpdateRepairFlow(coord, CAM_ID)

        result = await flow.async_step_confirm(user_input={})

        assert result["type"] == "abort"
        assert result["reason"] == "install_failed"

    @pytest.mark.asyncio
    async def test_no_coordinator_aborts_immediately(self):
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        flow = FirmwareUpdateRepairFlow(None, CAM_ID)

        result = await flow.async_step_confirm()

        assert result["type"] == "abort"
        assert result["reason"] == "install_failed"

    @pytest.mark.asyncio
    async def test_no_coordinator_never_shows_form(self):
        """Guard runs before the user_input branch — no coordinator means no
        form should ever be shown, since there's nothing to install."""
        from custom_components.bosch_shc_camera.repairs import (
            FirmwareUpdateRepairFlow,
        )

        flow = FirmwareUpdateRepairFlow(None, CAM_ID)

        result = await flow.async_step_confirm(user_input=None)

        assert result["type"] == "abort"


# ─────────────────────────────────────────────────────────────────────────────
# Regression tests for refresh_nvr_not_recording_issue (GitHub #70): "Enable
# NVR" being on in the integration options only creates the per-camera NVR
# select/switch entities (entity_registry_enabled_default=False, hidden by
# default) — it does not itself start recording. Without this Repairs issue a
# user could enable NVR, never find/enable the 3 hidden per-camera entities,
# and get zero recordings with zero visible error ("media folder is correctly
# linked but no event is saved"). Same idempotent create/delete pattern as
# refresh_notifications_disabled_issues / refresh_firmware_update_issues /
# refresh_smb_unavailable_issue — one issue for the whole config entry (not
# per-camera), issue_id "nvr_enabled_not_recording".
# ─────────────────────────────────────────────────────────────────────────────


REPAIRS_MODULE = "custom_components.bosch_shc_camera.repairs"
NVR_GRACE_SEC = 300.0  # mirrors coordinator.NVR_NOT_RECORDING_GRACE_SEC


def _make_coord_nvr_not_recording(
    *,
    enable_nvr: bool,
    data: dict[str, dict[str, object]] | None,
    nvr_user_intent: dict[str, bool] | None = None,
    nvr_modes: dict[str, str] | None = None,
    nvr_preroll_seconds: int = 0,
    nvr_postroll_seconds: int = 0,
) -> SimpleNamespace:
    """Minimal coordinator stub for refresh_nvr_not_recording_issue.

    `nvr_modes` defaults every camera to "continuous" (no preroll/postroll
    requirement) unless a cam_id is explicitly mapped to "event_buffered".
    """
    modes = nvr_modes or {}

    def _get_nvr_mode(cam_id: str) -> str:
        return modes.get(cam_id, "continuous")

    return SimpleNamespace(
        hass=SimpleNamespace(),
        options={
            "enable_nvr": enable_nvr,
            "nvr_preroll_seconds": nvr_preroll_seconds,
            "nvr_postroll_seconds": nvr_postroll_seconds,
        },
        data=data,
        nvr_user_intent=nvr_user_intent or {},
        get_nvr_mode=_get_nvr_mode,
        _nvr_not_recording_logged=False,
        _nvr_not_recording_since=float("-inf"),
    )


def _call_nvr_not_recording(coord: SimpleNamespace) -> None:
    from custom_components.bosch_shc_camera.repairs import (
        refresh_nvr_not_recording_issue,
    )

    refresh_nvr_not_recording_issue(coord)


class TestNvrNotRecordingRepairs:
    """Pin every mode: enabled+nothing-recording, enabled+recording,
    disabled, no-data-yet (startup false-positive guard), the
    NVR_NOT_RECORDING_GRACE_SEC debounce, and the event_buffered
    preroll/postroll==0 false-negative fix."""

    @patch(f"{REPAIRS_MODULE}.time.monotonic")
    @patch(f"{MODULE}.ir")
    def test_enabled_and_nothing_recording_creates_issue_after_grace(
        self,
        mock_ir: MagicMock,
        mock_monotonic: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID: False},
        )

        with caplog.at_level(
            logging.WARNING, logger="custom_components.bosch_shc_camera.repairs"
        ):
            mock_monotonic.return_value = 1000.0
            _call_nvr_not_recording(coord)  # first-observed-true — no issue yet
            mock_ir.async_create_issue.assert_not_called()
            assert coord._nvr_not_recording_since == 1000.0

            mock_monotonic.return_value = 1000.0 + NVR_GRACE_SEC - 1
            _call_nvr_not_recording(coord)  # still within grace — no issue yet
            mock_ir.async_create_issue.assert_not_called()

            mock_monotonic.return_value = 1000.0 + NVR_GRACE_SEC
            _call_nvr_not_recording(coord)  # grace elapsed — creates + warns
            mock_monotonic.return_value = 1000.0 + NVR_GRACE_SEC + 60
            _call_nvr_not_recording(coord)  # next tick — must NOT warn again

        mock_ir.async_create_issue.assert_called_with(
            coord.hass,
            DOMAIN,
            "nvr_enabled_not_recording",
            is_fixable=False,
            is_persistent=False,
            severity=mock_ir.IssueSeverity.WARNING,
            translation_key="nvr_enabled_not_recording",
            translation_placeholders={},
        )
        assert mock_ir.async_create_issue.call_count == 2
        mock_ir.async_delete_issue.assert_not_called()

        warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_msgs) == 1
        assert coord._nvr_not_recording_logged is True
        # since is set once on first-observed-true, NOT reset on later still-true ticks.
        assert coord._nvr_not_recording_since == 1000.0

    @patch(f"{MODULE}.ir")
    def test_enabled_and_one_camera_recording_deletes_issue(
        self, mock_ir: MagicMock
    ) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={
                CAM_ID: {"info": {"title": "Terrasse"}},
                CAM_ID_2: {"info": {"title": "Garten"}},
            },
            nvr_user_intent={CAM_ID: False, CAM_ID_2: True},
        )
        coord._nvr_not_recording_logged = True  # simulate a previously-open issue
        coord._nvr_not_recording_since = 500.0

        _call_nvr_not_recording(coord)

        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, "nvr_enabled_not_recording"
        )
        mock_ir.async_create_issue.assert_not_called()
        assert coord._nvr_not_recording_logged is False
        assert coord._nvr_not_recording_since == float("-inf")

    @patch(f"{MODULE}.ir")
    def test_enable_nvr_false_never_creates_issue_regardless_of_intent(
        self, mock_ir: MagicMock
    ) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=False,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID: False},
        )

        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, "nvr_enabled_not_recording"
        )

    @patch(f"{MODULE}.ir")
    def test_no_data_yet_clears_stale_issue(self, mock_ir: MagicMock) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={},
            nvr_user_intent={},
        )
        coord._nvr_not_recording_logged = True  # simulate a stale open issue
        coord._nvr_not_recording_since = 42.0

        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, "nvr_enabled_not_recording"
        )
        assert coord._nvr_not_recording_logged is False
        assert coord._nvr_not_recording_since == float("-inf")

    @patch(f"{MODULE}.ir")
    def test_data_none_clears_stale_issue(self, mock_ir: MagicMock) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data=None,
            nvr_user_intent={},
        )
        coord._nvr_not_recording_logged = True

        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, "nvr_enabled_not_recording"
        )
        assert coord._nvr_not_recording_logged is False

    @patch(f"{REPAIRS_MODULE}.time.monotonic")
    @patch(f"{MODULE}.ir")
    def test_warn_refires_after_recording_starts_then_stops_again(
        self,
        mock_ir: MagicMock,
        mock_monotonic: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID: False},
        )
        logger_name = "custom_components.bosch_shc_camera.repairs"

        with caplog.at_level(logging.WARNING, logger=logger_name):
            mock_monotonic.return_value = 0.0
            _call_nvr_not_recording(coord)  # first-observed-true, no warn yet
            mock_monotonic.return_value = NVR_GRACE_SEC
            _call_nvr_not_recording(coord)  # warn #1, logged=True
        assert coord._nvr_not_recording_logged is True

        coord.nvr_user_intent[CAM_ID] = True
        mock_monotonic.return_value = NVR_GRACE_SEC + 1
        _call_nvr_not_recording(coord)  # clears issue, resets logged=False + since
        assert coord._nvr_not_recording_logged is False
        assert coord._nvr_not_recording_since == float("-inf")

        coord.nvr_user_intent[CAM_ID] = False
        with caplog.at_level(logging.WARNING, logger=logger_name):
            mock_monotonic.return_value = NVR_GRACE_SEC + 2
            _call_nvr_not_recording(coord)  # first-observed-true again, no warn
            mock_monotonic.return_value = NVR_GRACE_SEC + 2 + NVR_GRACE_SEC
            _call_nvr_not_recording(coord)  # warn #2

        warn_msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warn_msgs) == 2

    @patch(f"{REPAIRS_MODULE}.time.monotonic")
    @patch(f"{MODULE}.ir")
    def test_divergent_key_sets_missing_intent_counts_as_not_recording(
        self, mock_ir: MagicMock, mock_monotonic: MagicMock
    ) -> None:
        """Mutation-testing pin (round-1 bug-hunt finding): data and
        nvr_user_intent must have divergent key sets, so a mutation reading
        `nvr_user_intent.values()` instead of enumerating `coordinator.data`'s
        keys is caught. CAM_ID is in data but missing from nvr_user_intent
        entirely (must count as "not recording", not be silently skipped);
        CAM_ID_2 is in nvr_user_intent (True) but absent from data (must be
        ignored, since only data's cameras are enumerated)."""
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID_2: True},
        )

        mock_monotonic.return_value = 0.0
        _call_nvr_not_recording(coord)
        mock_monotonic.return_value = NVR_GRACE_SEC
        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_called_once()
        mock_ir.async_delete_issue.assert_not_called()

    @patch(f"{REPAIRS_MODULE}.time.monotonic")
    @patch(f"{MODULE}.ir")
    def test_event_buffered_zero_preroll_postroll_still_fires_issue(
        self, mock_ir: MagicMock, mock_monotonic: MagicMock
    ) -> None:
        """GitHub #70 bug-hunt false-negative fix: nvr_user_intent True is not
        enough in event_buffered mode when both preroll and postroll are 0 —
        recorder.py's own _nvr_secs_ok gate would never let a clip assemble."""
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID: True},
            nvr_modes={CAM_ID: "event_buffered"},
            nvr_preroll_seconds=0,
            nvr_postroll_seconds=0,
        )

        mock_monotonic.return_value = 0.0
        _call_nvr_not_recording(coord)
        mock_monotonic.return_value = NVR_GRACE_SEC
        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_called_once()

    @patch(f"{MODULE}.ir")
    def test_event_buffered_with_preroll_set_does_not_fire_issue(
        self, mock_ir: MagicMock
    ) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID: True},
            nvr_modes={CAM_ID: "event_buffered"},
            nvr_preroll_seconds=5,
            nvr_postroll_seconds=0,
        )

        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, "nvr_enabled_not_recording"
        )

    @patch(f"{MODULE}.ir")
    def test_event_buffered_with_postroll_set_does_not_fire_issue(
        self, mock_ir: MagicMock
    ) -> None:
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID: True},
            nvr_modes={CAM_ID: "event_buffered"},
            nvr_preroll_seconds=0,
            nvr_postroll_seconds=10,
        )

        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, "nvr_enabled_not_recording"
        )

    @patch(f"{MODULE}.ir")
    def test_continuous_mode_ignores_preroll_postroll(self, mock_ir: MagicMock) -> None:
        """continuous mode never needs preroll/postroll — intent alone suffices."""
        coord = _make_coord_nvr_not_recording(
            enable_nvr=True,
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            nvr_user_intent={CAM_ID: True},
            nvr_modes={CAM_ID: "continuous"},
            nvr_preroll_seconds=0,
            nvr_postroll_seconds=0,
        )

        _call_nvr_not_recording(coord)

        mock_ir.async_create_issue.assert_not_called()
        mock_ir.async_delete_issue.assert_called_once_with(
            coord.hass, DOMAIN, "nvr_enabled_not_recording"
        )

    def test_delegator_calls_module_function(self) -> None:
        """coordinator._refresh_nvr_not_recording_issue is a thin delegator —
        same unbound-method-call pattern used for the sibling checks."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord_nvr_not_recording(enable_nvr=False, data={})

        with patch(
            "custom_components.bosch_shc_camera.repairs.refresh_nvr_not_recording_issue"
        ) as mock_refresh:
            BoschCameraCoordinator._refresh_nvr_not_recording_issue(coord)

        mock_refresh.assert_called_once_with(coord)
