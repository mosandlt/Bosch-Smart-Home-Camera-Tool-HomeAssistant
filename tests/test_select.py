"""Tests for select.py entity classes.

Covers all `select` platform entities: video quality, motion sensitivity,
FCM push mode, stream mode, detection mode, and the PTZ pan-preset select
(Gen1 360°, opt-in via `enable_ptz_controls`).

Sections:
  - Doubled-prefix entity naming regression (translation_key vs _attr_name)
  - Basic construction / current_option behavior
  - Fallback-chain and option-key contract pins
  - async_setup_entry, restore-on-restart, and write-path coverage
  - PTZ named-preset select (Gen1 360°)
  - `enable_ptz_controls` opt-in toggle gating
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera.const import (
    CONF_ENABLE_PTZ_CONTROLS,
    DEFAULT_OPTIONS,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"
PAN_CAM_ID = "22222222-AAAA-BBBB-CCCC-000000000001"


@pytest.fixture
def stub_entry():
    """Minimal config-entry stub shared by the naming/basic/pan-preset sections."""
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ═════════════════════════════════════════════════════════════════════════
# Doubled-prefix entity naming regression
#
# Source: Andrew75 forum post 998974/15 reported entity IDs like
#   button.bosch_est_bosch_est_refresh_snapshot
# instead of
#   button.bosch_est_refresh_snapshot
#
# Root cause: classes with `_attr_has_entity_name = True` AND
# `_attr_name = f"Bosch {self._cam_title} <Suffix>"` caused HA to prepend the
# device name automatically AND the code re-prepended "Bosch {title}" manually.
#
# Fix (v14.2.2): remove all `_attr_name` assignments; use `_attr_translation_key`
# instead so HA resolves the entity name from translations/en.json at runtime.
# `_attr_name` must be None (unset) for translation_key-based naming to work.
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture
def stub_coord_naming():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "live": {},
                "motion": {},
            }
        },
        _camera_entities={},
        _firmware_cache={},
        _intrusion_config_cache={},
        _stream_type_override=None,
        last_update_success=True,
        get_quality=lambda cid: "auto",
        set_quality=lambda cid, q: None,
        motion_settings=lambda cid: {},
        async_request_refresh=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
        options={"enable_fcm_push": False},
    )


class TestVideoQualitySelectNaming:
    def test_attr_name_is_none(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord_naming, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_video_quality(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "video_quality"

    def test_has_entity_name_is_true(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


class TestMotionSensitivitySelectNaming:
    def test_attr_name_is_none(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_naming, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_motion_sensitivity(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "motion_sensitivity"

    def test_has_entity_name_is_true(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


class TestFcmPushModeSelectNaming:
    def test_attr_name_is_none(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        sel = BoschFcmPushModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_fcm_push_mode(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        sel = BoschFcmPushModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "fcm_push_mode"

    def test_has_entity_name_is_true(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        sel = BoschFcmPushModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


class TestStreamModeSelectNaming:
    def test_attr_name_is_none(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_stream_mode(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "stream_mode"

    def test_has_entity_name_is_true(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


class TestDetectionModeSelectNaming:
    def test_attr_name_is_none(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        sel = BoschDetectionModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_detection_mode(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        sel = BoschDetectionModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "detection_mode"

    def test_has_entity_name_is_true(self, stub_coord_naming, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        sel = BoschDetectionModeSelect(stub_coord_naming, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ═════════════════════════════════════════════════════════════════════════
# Basic construction / current_option behavior
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture
def stub_coord_basic():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                },
                "live": {},
            }
        },
        get_quality=lambda cid: "auto",
        set_quality=lambda cid, q: None,
        options={
            "fcm_push_mode": "auto",
            "stream_connection_type": "auto",
        },
    )


class TestVideoQualitySelectBasic:
    def test_construction(self, stub_coord_basic, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord_basic, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "video_quality"
        assert sel._attr_unique_id.endswith("_video_quality")

    def test_current_option_reads_coordinator(self, stub_coord_basic, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord_basic, CAM_ID, stub_entry)
        assert sel.current_option == "auto"

    def test_current_option_falls_back_to_auto_for_unknown(
        self, stub_coord_basic, stub_entry
    ):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        stub_coord_basic.get_quality = lambda cid: "weird-not-an-option"
        sel = BoschVideoQualitySelect(stub_coord_basic, CAM_ID, stub_entry)
        assert sel.current_option == "auto"

    def test_options_list_present(self, stub_coord_basic, stub_entry):
        """A select entity must have a non-empty _attr_options."""
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord_basic, CAM_ID, stub_entry)
        assert len(sel._attr_options) >= 2
        assert "auto" in sel._attr_options


# ═════════════════════════════════════════════════════════════════════════
# BoschNvrModeSelect — per-camera Mini-NVR mode override (GitHub #43)
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture
def stub_coord_nvr_mode():
    calls = {}

    def _get_nvr_mode(cid):
        return calls.get(cid, "continuous")

    def _set_nvr_mode(cid, mode):
        calls[cid] = mode

    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"},
                "live": {},
            }
        },
        get_nvr_mode=_get_nvr_mode,
        set_nvr_mode=_set_nvr_mode,
        options={"enable_nvr": True},
        _nvr_processes={},
        _nvr_preroll_processes={},
        start_recorder=AsyncMock(),
    )


class TestNvrModeSelectBasic:
    def test_construction(self, stub_coord_nvr_mode, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "nvr_mode"
        assert sel._attr_unique_id.endswith("_nvr_mode")

    def test_device_info(self, stub_coord_nvr_mode, stub_entry):
        from custom_components.bosch_shc_camera import DOMAIN
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        info = sel.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]
        assert info["manufacturer"] == "Bosch"
        assert info["model"] == "HOME_Eyes_Outdoor"

    def test_options_are_continuous_and_event_buffered_only(
        self, stub_coord_nvr_mode, stub_entry
    ):
        """No 'off' option — that's the existing BoschNvrRecordingSwitch's job."""
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        assert sel._attr_options == ["continuous", "event_buffered"]

    def test_current_option_reads_coordinator_default(
        self, stub_coord_nvr_mode, stub_entry
    ):
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        assert sel.current_option == "continuous"

    def test_current_option_reflects_override(self, stub_coord_nvr_mode, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        stub_coord_nvr_mode.get_nvr_mode = lambda cid: "event_buffered"
        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        assert sel.current_option == "event_buffered"

    def test_current_option_falls_back_for_unknown_value(
        self, stub_coord_nvr_mode, stub_entry
    ):
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        stub_coord_nvr_mode.get_nvr_mode = lambda cid: "garbage"
        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        assert sel.current_option == "continuous"

    @pytest.mark.asyncio
    async def test_async_select_option_calls_coordinator_setter(
        self, stub_coord_nvr_mode, stub_entry
    ):
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("event_buffered")
        assert stub_coord_nvr_mode.get_nvr_mode(CAM_ID) == "event_buffered"
        sel.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_select_option_restarts_active_continuous_recorder(
        self, stub_coord_nvr_mode, stub_entry
    ):
        """Bug-hunt finding (2026-07-11): a mode change must apply immediately
        if a recorder is already running for this camera — otherwise a
        healthy long-running camera could be stuck on the old mode
        indefinitely (the proactive cred-rotation restart that used to
        pick this up was removed in v14.5.4)."""
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        stub_coord_nvr_mode._nvr_processes[CAM_ID] = MagicMock()  # already running
        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("event_buffered")
        stub_coord_nvr_mode.start_recorder.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_async_select_option_restarts_active_preroll_recorder(
        self, stub_coord_nvr_mode, stub_entry
    ):
        """Same as above, but the camera was running event-buffered mode
        (tracked in _nvr_preroll_processes, not _nvr_processes)."""
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        stub_coord_nvr_mode._nvr_preroll_processes[CAM_ID] = MagicMock()
        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("continuous")
        stub_coord_nvr_mode.start_recorder.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_async_select_option_no_restart_when_recorder_inactive(
        self, stub_coord_nvr_mode, stub_entry
    ):
        """No recorder running for this camera (e.g. the NVR switch is off) →
        must NOT call start_recorder, which would incorrectly turn NVR on."""
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("event_buffered")
        stub_coord_nvr_mode.start_recorder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restore_state_applies_valid_saved_option(
        self, stub_coord_nvr_mode, stub_entry
    ):
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        last = MagicMock()
        last.state = "event_buffered"
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch.object(sel, "async_get_last_state", AsyncMock(return_value=last)),
        ):
            await sel.async_added_to_hass()
        assert stub_coord_nvr_mode.get_nvr_mode(CAM_ID) == "event_buffered"

    @pytest.mark.asyncio
    async def test_restore_state_ignores_invalid_saved_option(
        self, stub_coord_nvr_mode, stub_entry
    ):
        """A stale/invalid restored state must not be applied as an override."""
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        last = MagicMock()
        last.state = "unknown"
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch.object(sel, "async_get_last_state", AsyncMock(return_value=last)),
        ):
            await sel.async_added_to_hass()
        assert stub_coord_nvr_mode.get_nvr_mode(CAM_ID) == "continuous"

    @pytest.mark.asyncio
    async def test_restore_no_last_state_leaves_default(
        self, stub_coord_nvr_mode, stub_entry
    ):
        from custom_components.bosch_shc_camera.select import BoschNvrModeSelect

        sel = BoschNvrModeSelect(stub_coord_nvr_mode, CAM_ID, stub_entry)
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch.object(sel, "async_get_last_state", AsyncMock(return_value=None)),
        ):
            await sel.async_added_to_hass()
        assert stub_coord_nvr_mode.get_nvr_mode(CAM_ID) == "continuous"


class TestFcmPushModeSelectBasic:
    def test_construction(self, stub_coord_basic, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        sel = BoschFcmPushModeSelect(stub_coord_basic, CAM_ID, stub_entry)
        # FCM mode select binds to the integration, not per-camera
        assert sel._attr_options


class TestStreamModeSelectBasic:
    def test_construction(self, stub_coord_basic, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord_basic, CAM_ID, stub_entry)
        assert sel._attr_options


class TestMotionSensitivitySelectBasic:
    def test_disabled_by_default(self, stub_coord_basic, stub_entry):
        """Motion-sensitivity select is hidden by default — disabled_by_default."""
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_basic, CAM_ID, stub_entry)
        assert sel._attr_entity_registry_enabled_default is False


# ═════════════════════════════════════════════════════════════════════════
# Fallback-chain and option-key contract pins
#
# Most select entities use a tiered fallback chain to derive the
# displayed option:
#   1. In-memory override (`coordinator._stream_type_override`)
#   2. Persisted entry option (`get_options(entry)["..."]`)
#   3. Hard-coded default ("auto" / first option)
#   4. None when the underlying data isn't fetched yet (slow-tier)
#
# A regression in any tier silently flips the dropdown to the wrong
# position — users notice only after their settings appear to "reset"
# themselves on integration reload. These tests pin the fallback order
# plus the option-key constants the JSON translations + APIs depend on.
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture
def stub_coord_extra():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                },
                "live": {},
                "motion": {},
            }
        },
        get_quality=lambda cid: "auto",
        set_quality=lambda cid, q: None,
        motion_settings=lambda cid: {},
        last_update_success=True,
        options={
            "fcm_push_mode": "auto",
            "stream_connection_type": "auto",
            "enable_fcm_push": True,
        },
        _stream_type_override=None,
        _fcm_push_mode="auto",
        _intrusion_config_cache={},
        _intrusion_config_set_at={},
        _motion_set_at={},
        _alarm_settings_set_at={},
        async_put_camera=AsyncMock(return_value=True),
        async_stop_fcm_push=AsyncMock(),
        async_start_fcm_push=AsyncMock(),
    )


@pytest.fixture
def stub_entry_extra():
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={},
        options={"stream_connection_type": "auto", "fcm_push_mode": "auto"},
    )


class TestOptionConstants:
    """The integration relies on exact lower-case keys in each list:
    - translations/de.json + en.json have one entry per option
    - icons.json maps state-based icons by these keys
    - APIs (motion sensitivity) upper-case the key for the wire payload
    Any drift breaks either the dropdown labels (untranslated key string
    leaks into the UI) or the API call (Bosch returns 400 invalid value)."""

    def test_stream_mode_options_pinned(self):
        from custom_components.bosch_shc_camera.select import STREAM_MODE_OPTIONS

        assert STREAM_MODE_OPTIONS == ["auto", "local", "remote"], (
            "Stream-mode option keys are referenced by translations/de.json + "
            "en.json (selector.stream_mode.*) and by the integration's "
            "stream_connection_type config-flow option. Drift = invisible "
            "dropdown labels."
        )

    def test_motion_sensitivity_options_pinned(self):
        from custom_components.bosch_shc_camera.select import (
            MOTION_SENSITIVITY_OPTIONS,
            SENSITIVITY_TO_API,
        )

        # 6 levels including OFF — Bosch's PUT /motion accepts these UPPER-cased.
        assert MOTION_SENSITIVITY_OPTIONS == [
            "super_high",
            "high",
            "medium_high",
            "medium_low",
            "low",
            "off",
        ]
        # Wire format is upper-snake. The mapping must be 1:1 to prevent
        # a typo silently dropping levels.
        for key in MOTION_SENSITIVITY_OPTIONS:
            assert SENSITIVITY_TO_API[key] == key.upper()

    def test_detection_mode_options_pinned(self):
        from custom_components.bosch_shc_camera.select import DETECTION_MODE_OPTIONS

        assert DETECTION_MODE_OPTIONS == ["all_motions", "only_humans", "zones"]

    def test_fcm_push_mode_options_pinned(self):
        from custom_components.bosch_shc_camera.select import FCM_PUSH_MODE_OPTIONS

        # v12.4.5: simplified to 2 options — OSS Android key handles both platforms.
        # Order matters for the dropdown in the UI.
        assert FCM_PUSH_MODE_OPTIONS == ["auto", "polling"]


class TestStreamModeSelectFallbackChain:
    def test_override_takes_precedence_over_options(
        self, stub_coord_extra, stub_entry_extra
    ):
        """When the user changes the dropdown live, `_stream_type_override`
        wins over the persisted option until the integration reloads.
        Otherwise the next coordinator tick would flip the dropdown back."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        stub_coord_extra._stream_type_override = "local"
        stub_entry_extra.options["stream_connection_type"] = "auto"
        sel = BoschStreamModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "local"

    def test_falls_back_to_persisted_option(self, stub_coord_extra, stub_entry_extra):
        """Without an in-memory override, persisted option wins."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        stub_coord_extra._stream_type_override = None
        stub_entry_extra.options["stream_connection_type"] = "remote"
        sel = BoschStreamModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "remote"

    def test_unknown_value_collapses_to_local(self, stub_coord_extra, stub_entry_extra):
        """Garbage in the entry options must not poison the dropdown —
        the select entity would refuse to render an out-of-list value.
        Default collapse target is 'local' since v12.4.2 (LOCAL-first)."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        stub_coord_extra._stream_type_override = "made-up-mode"
        sel = BoschStreamModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "local"

    def test_auto_mode_explicit_pin(self, stub_coord_extra, stub_entry_extra):
        """Pin: override=None + persisted='auto' → current_option == 'auto'.

        The 'auto' key must survive a round-trip through the fallback chain:
        no in-memory override → read entry option → return 'auto'.
        Without this pin a refactor could default to 'local' for persisted
        'auto', silently breaking existing installations that rely on
        LOCAL-first-then-REMOTE behaviour.
        """
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        stub_coord_extra._stream_type_override = None
        stub_entry_extra.options["stream_connection_type"] = "auto"
        sel = BoschStreamModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "auto", (
            "override=None + persisted='auto' must yield current_option='auto'. "
            "Falling through to 'local' would silently disable cloud fallback "
            "for all users who never touched the stream-mode dropdown."
        )

    @pytest.mark.asyncio
    async def test_select_option_writes_override(
        self, stub_coord_extra, stub_entry_extra
    ):
        """User picks 'remote' in the dropdown → `_stream_type_override`
        flips immediately. Takes effect on the next stream activation,
        no integration reload required."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("remote")
        assert stub_coord_extra._stream_type_override == "remote"
        sel.async_write_ha_state.assert_called_once()


class TestFcmPushModeSelectAvailability:
    def test_unavailable_when_fcm_disabled(self, stub_coord_extra, stub_entry_extra):
        """If the integration option `enable_fcm_push` is False, the
        dropdown must show 'unavailable' so the user knows toggling here
        does nothing — the master switch lives in integration options."""
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        stub_coord_extra.options["enable_fcm_push"] = False
        sel = BoschFcmPushModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.available is False, (
            "FCM push disabled in integration options must surface as "
            "'Unavailable' so the user explicitly sees the master toggle."
        )

    def test_available_when_fcm_enabled(self, stub_coord_extra, stub_entry_extra):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        stub_coord_extra.options["enable_fcm_push"] = True
        sel = BoschFcmPushModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.available is True

    def test_current_option_reads_entry_options(
        self, stub_coord_extra, stub_entry_extra
    ):
        # v12.4.5: valid options are now "auto" and "polling" only.
        # Pin "polling" — the non-default valid option — to verify persisted
        # entry options are read correctly through the fallback chain.
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        stub_entry_extra.options["fcm_push_mode"] = "polling"
        sel = BoschFcmPushModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "polling"

    def test_current_option_default_auto(self, stub_coord_extra, stub_entry_extra):
        """Missing option key → 'auto'. Stable default ensures fresh
        installs land on the cross-platform-safe mode."""
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        stub_entry_extra.options.pop("fcm_push_mode", None)
        sel = BoschFcmPushModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "auto"

    def test_current_option_unknown_collapses(self, stub_coord_extra, stub_entry_extra):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        stub_entry_extra.options["fcm_push_mode"] = "junk"
        sel = BoschFcmPushModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "auto"

    def test_polling_mode_pinned(self, stub_coord_extra, stub_entry_extra):
        """Pin: persisted 'polling' → current_option == 'polling'.

        FCM push mode 'polling' is the fallback for environments where
        FCM connectivity is blocked (corporate firewalls). Users who
        explicitly chose this mode must not be silently reverted to 'auto'.
        """
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        stub_entry_extra.options["fcm_push_mode"] = "polling"
        sel = BoschFcmPushModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "polling", (
            "Persisted 'polling' must survive the current_option fallback chain. "
            "Collapsing to 'auto' silently breaks push for users behind "
            "firewalls that block FCM long-lived connections."
        )


class TestMotionSensitivitySelectApiMapping:
    def test_current_option_lowercases_api_value(
        self, stub_coord_extra, stub_entry_extra
    ):
        """Bosch API returns UPPER-snake (HIGH); the select entity's option
        key list is lower-snake (high). Without lower-casing, the UI shows
        the raw API value as a literal label and the dropdown mismatches."""
        stub_coord_extra.motion_settings = lambda cid: {
            "motionAlarmConfiguration": "HIGH"
        }
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == "high"

    def test_current_option_none_when_motion_unfetched(
        self, stub_coord_extra, stub_entry_extra
    ):
        """Slow-tier data not yet pulled → None (HA renders 'unknown')
        instead of an arbitrary default that might mismatch the camera."""
        stub_coord_extra.motion_settings = lambda cid: {}
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option is None

    def test_current_option_default_for_unknown_api_value(
        self, stub_coord_extra, stub_entry_extra
    ):
        """If Bosch ever returns a level we don't list (e.g. the rumored
        EXTREME mode on Gen3), return the first valid option (with a warning)
        so the select entity does not appear de-selected/broken.
        Bug M8 fix: previously returned None (entity appeared de-selected)."""
        stub_coord_extra.motion_settings = lambda cid: {
            "motionAlarmConfiguration": "EXTREME"
        }
        from custom_components.bosch_shc_camera.select import (
            MOTION_SENSITIVITY_OPTIONS,
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        result = sel.current_option
        assert result == MOTION_SENSITIVITY_OPTIONS[0], (
            f"Unknown API value should return first option, got {result!r}"
        )

    def test_unavailable_when_motion_settings_empty(
        self, stub_coord_extra, stub_entry_extra
    ):
        """Slow tier hasn't run yet → entity unavailable. Avoids the
        select rendering with a stale 'auto' that the user might click,
        which would issue a write before the read populated the cache."""
        stub_coord_extra.motion_settings = lambda cid: {}
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.available is False

    def test_disabled_by_default_in_registry(self, stub_coord_extra, stub_entry_extra):
        """Hidden by default — too granular for most users; expose only
        when explicitly enabled via Settings → Entities."""
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel._attr_entity_registry_enabled_default is False

    @pytest.mark.asyncio
    async def test_select_option_uppercases_for_api(
        self, stub_coord_extra, stub_entry_extra
    ):
        """The entity stores keys lowercased ('high'); Bosch API needs
        'HIGH'. The mapping happens in async_select_option — verify it."""
        # Don't trip the gen2 indoor privacy guard
        stub_coord_extra.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        stub_coord_extra.motion_settings = lambda cid: {
            "motionAlarmConfiguration": "MEDIUM_HIGH",
            "enabled": True,
        }
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("high")
        # Must be called with UPPER-cased value
        stub_coord_extra.async_put_camera.assert_called_once()
        args, _kwargs = stub_coord_extra.async_put_camera.call_args
        # signature: (cam_id, "motion", {"enabled":..., "motionAlarmConfiguration":...})
        body = args[2]
        assert body["motionAlarmConfiguration"] == "HIGH", (
            "API receives wire-format value (UPPER-snake), not the entity "
            "key (lower-snake). Sending 'high' would yield Bosch HTTP 400."
        )
        assert body["enabled"] is True, (
            "PUT /motion is the same endpoint as on/off — must preserve "
            "the existing enabled state to avoid a side-effect of disabling."
        )

    @pytest.mark.asyncio
    async def test_invalid_option_silently_no_op(
        self, stub_coord_extra, stub_entry_extra
    ):
        """An option outside the list must not call the API. Defends
        against typos in dashboard service calls (`select.select_option`
        with an arbitrary value)."""
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("bogus_level")
        stub_coord_extra.async_put_camera.assert_not_called()


class TestVideoQualitySelectExtra:
    def test_current_option_passes_through_known_value(
        self, stub_coord_extra, stub_entry_extra
    ):
        """get_quality returns the active level — must round-trip if
        in the option list."""
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        for opt in sel._attr_options:
            stub_coord_extra.get_quality = lambda cid, _o=opt: _o
            sel2 = BoschVideoQualitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
            assert sel2.current_option == opt, (
                f"Quality '{opt}' must round-trip through current_option. "
                f"If the option list and the coordinator drift, the "
                f"dropdown silently snaps to 'auto' for valid values."
            )


class TestMotionSensitivitySelectAllLevels:
    """Parametrized pin: each API value (UPPER-snake) maps correctly to
    the entity option key (lower-snake) via current_option.

    The 'high' level is covered separately in
    TestMotionSensitivitySelectApiMapping. This class covers the 5 remaining
    levels: SUPER_HIGH, MEDIUM_HIGH, MEDIUM_LOW, LOW, OFF.

    Each level failure mode is the same: the select UI shows the raw
    API string as a label (untranslated) and the dropdown mismatches the
    actual camera setting.
    """

    @pytest.mark.parametrize(
        "api_value,expected_key",
        [
            ("SUPER_HIGH", "super_high"),
            ("MEDIUM_HIGH", "medium_high"),
            ("MEDIUM_LOW", "medium_low"),
            ("LOW", "low"),
            ("OFF", "off"),
        ],
    )
    def test_api_value_maps_to_option_key(
        self, stub_coord_extra, stub_entry_extra, api_value: str, expected_key: str
    ) -> None:
        """Pin: Bosch API value '{api_value}' → current_option == '{expected_key}'.

        Verifies that lower-casing the API wire value yields a key that
        exists in MOTION_SENSITIVITY_OPTIONS, so the dropdown renders the
        translated label instead of the raw UPPER-snake API string.
        """
        stub_coord_extra.motion_settings = lambda cid: {
            "motionAlarmConfiguration": api_value
        }
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == expected_key, (
            f"API value '{api_value}' must map to option key '{expected_key}'. "
            f"A mismatch means the dropdown shows the raw API string as a label "
            f"and the write-path sends the wrong wire value to Bosch."
        )


class TestDetectionModeSelectPins:
    """Parametrized pin: API detection mode values map to entity option keys.

    API (UPPER-snake): ALL_MOTIONS, ONLY_HUMANS, ZONES
    Entity keys (lower-snake): all_motions, only_humans, zones

    'only_humans' is covered by TestDetectionModeSelect; this class covers
    'all_motions' and 'zones'.
    """

    @pytest.mark.parametrize(
        "api_value,expected_key",
        [
            ("ALL_MOTIONS", "all_motions"),
            ("ZONES", "zones"),
        ],
    )
    def test_detection_mode_api_to_key(
        self, stub_coord_extra, stub_entry_extra, api_value: str, expected_key: str
    ) -> None:
        """Pin: coordinator cache value '{api_value}' → current_option == '{expected_key}'.

        BoschDetectionModeSelect reads coordinator._intrusion_config_cache[cam_id]
        ['detectionMode'] and lower-cases it. If the lower-cased value is in
        DETECTION_MODE_OPTIONS it is returned; otherwise None.
        A failure here means the detection-mode dropdown shows 'unknown' despite
        a valid API value being cached, confusing users who check their settings.
        """
        stub_coord_extra._intrusion_config_cache = {
            CAM_ID: {"detectionMode": api_value}
        }
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        sel = BoschDetectionModeSelect(stub_coord_extra, CAM_ID, stub_entry_extra)
        assert sel.current_option == expected_key, (
            f"API value '{api_value}' must map to option key '{expected_key}'. "
            f"The lower-casing + DETECTION_MODE_OPTIONS membership check must "
            f"cover all three API variants, not just 'only_humans'."
        )


# ═════════════════════════════════════════════════════════════════════════
# async_setup_entry, restore-on-restart, and write-path coverage
# ═════════════════════════════════════════════════════════════════════════


def _stub_coord_platform(gen2: bool = True):
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR",
                    "firmwareVersion": "9.40.25",
                },
                "live": {},
                "motion": {"motionAlarmConfiguration": "HIGH", "enabled": True},
            }
        },
        options={"enable_fcm_push": False},
        last_update_success=True,
        _stream_type_override=None,
        _intrusion_config_cache={},
        _intrusion_config_set_at={},
        _alarm_settings_set_at={},
        _motion_set_at={},
        _fcm_push_mode="unknown",
        motion_settings=lambda cam_id: {
            "motionAlarmConfiguration": "HIGH",
            "enabled": True,
        },
        get_quality=lambda cam_id: "auto",
        set_quality=lambda cam_id, q: None,
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_stop_fcm_push=AsyncMock(),
        async_start_fcm_push=AsyncMock(),
        async_update_listeners=lambda: None,
        try_live_connection=AsyncMock(return_value={"rtspsUrl": "rtsps://new"}),
        _register_go2rtc_stream=AsyncMock(),
    )
    return coord


def _stub_entry_platform(options=None):
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={},
        options=options or {},
        runtime_data=None,
    )


class TestAsyncSetupEntryPlatform:
    @pytest.mark.asyncio
    async def test_gen2_adds_detection_mode_select(self):
        from custom_components.bosch_shc_camera.select import (
            BoschDetectionModeSelect,
            async_setup_entry,
        )

        coord = _stub_coord_platform(gen2=True)
        entry = _stub_entry_platform()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e: captured.extend(e),
        )
        types_ = {type(e).__name__ for e in captured}
        assert "BoschDetectionModeSelect" in types_

    @pytest.mark.asyncio
    async def test_gen1_no_detection_mode_select(self):
        from custom_components.bosch_shc_camera.select import (
            BoschDetectionModeSelect,
            async_setup_entry,
        )

        coord = _stub_coord_platform(gen2=False)
        entry = _stub_entry_platform()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e: captured.extend(e),
        )
        types_ = {type(e).__name__ for e in captured}
        assert "BoschDetectionModeSelect" not in types_

    @pytest.mark.asyncio
    async def test_enable_nvr_true_adds_nvr_mode_select(self):
        from custom_components.bosch_shc_camera.select import async_setup_entry

        coord = _stub_coord_platform()
        entry = _stub_entry_platform(options={"enable_nvr": True})
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e: captured.extend(e),
        )
        types_ = {type(e).__name__ for e in captured}
        assert "BoschNvrModeSelect" in types_

    @pytest.mark.asyncio
    async def test_enable_nvr_false_no_nvr_mode_select(self):
        """Default (Mini-NVR disabled) → no NVR mode select clutters the entity list."""
        from custom_components.bosch_shc_camera.select import async_setup_entry

        coord = _stub_coord_platform()
        entry = _stub_entry_platform()  # enable_nvr absent → defaults False
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e: captured.extend(e),
        )
        types_ = {type(e).__name__ for e in captured}
        assert "BoschNvrModeSelect" not in types_

    @pytest.mark.asyncio
    async def test_integration_level_selects_added(self):
        from custom_components.bosch_shc_camera.select import (
            BoschFcmPushModeSelect,
            BoschStreamModeSelect,
            async_setup_entry,
        )

        coord = _stub_coord_platform()
        entry = _stub_entry_platform()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e: captured.extend(e),
        )
        types_ = {type(e).__name__ for e in captured}
        assert "BoschFcmPushModeSelect" in types_
        assert "BoschStreamModeSelect" in types_

    @pytest.mark.asyncio
    async def test_empty_data_no_entities(self):
        from custom_components.bosch_shc_camera.select import async_setup_entry

        coord = _stub_coord_platform()
        coord.data = {}
        entry = _stub_entry_platform()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=entry,
            async_add_entities=lambda e: captured.extend(e),
        )
        assert captured == []


class TestVideoQualitySelectDeviceInfoAndRestore:
    def _make(self, coord=None):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        coord = coord or _stub_coord_platform()
        entry = _stub_entry_platform()
        sel = BoschVideoQualitySelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_device_info_returns_identifiers(self):
        from custom_components.bosch_shc_camera import DOMAIN

        sel = self._make()
        info = sel.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]
        assert info["manufacturer"] == "Bosch"

    @pytest.mark.asyncio
    async def test_async_added_to_hass_restores_quality(self):
        """Restores saved quality from last_state on HA restart."""
        sel = self._make()
        last = MagicMock()
        last.state = "high"
        sel.coordinator.set_quality = MagicMock()
        _noop = AsyncMock()
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                _noop,
            ),
            patch.object(sel, "async_get_last_state", AsyncMock(return_value=last)),
        ):
            await sel.async_added_to_hass()
        sel.coordinator.set_quality.assert_called_once_with(CAM_ID, "high")

    @pytest.mark.asyncio
    async def test_async_added_to_hass_legacy_mapping(self):
        """Legacy display text 'Auto' maps to 'auto'."""
        sel = self._make()
        last = MagicMock()
        last.state = "Auto"
        sel.coordinator.set_quality = MagicMock()
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch.object(sel, "async_get_last_state", AsyncMock(return_value=last)),
        ):
            await sel.async_added_to_hass()
        sel.coordinator.set_quality.assert_called_with(CAM_ID, "auto")

    @pytest.mark.asyncio
    async def test_async_added_to_hass_no_last_state(self):
        """No saved state → coordinator.set_quality NOT called."""
        sel = self._make()
        sel.coordinator.set_quality = MagicMock()
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch.object(sel, "async_get_last_state", AsyncMock(return_value=None)),
        ):
            await sel.async_added_to_hass()
        sel.coordinator.set_quality.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_select_option_updates_quality(self):
        """Selecting quality updates coordinator and writes HA state."""
        sel = self._make()
        sel.coordinator.set_quality = MagicMock()
        await sel.async_select_option("high")
        sel.coordinator.set_quality.assert_called_with(CAM_ID, "high")
        sel.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_select_option_with_active_stream_reconnects(self):
        """When live stream is active, reconnects with new quality."""
        coord = _stub_coord_platform()
        coord.data[CAM_ID]["live"] = {"rtspsUrl": "rtsps://old"}
        coord.set_quality = MagicMock()
        sel = self._make(coord)
        await sel.async_select_option("low")
        coord.try_live_connection.assert_called_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_async_select_option_reconnect_exception_swallowed(self):
        """Reconnect raising an exception must not propagate (graceful degradation)."""
        coord = _stub_coord_platform()
        coord.data[CAM_ID]["live"] = {"rtspsUrl": "rtsps://old"}
        coord.set_quality = MagicMock()
        coord.try_live_connection = AsyncMock(side_effect=RuntimeError("boom"))
        sel = self._make(coord)
        await sel.async_select_option("low")  # must not raise
        sel.async_write_ha_state.assert_called_once()


class TestMotionSensitivitySelectWrite:
    def _make(self, coord=None, put_return=True):
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        coord = coord or _stub_coord_platform()
        coord.async_put_camera = AsyncMock(return_value=put_return)
        entry = _stub_entry_platform()
        sel = BoschMotionSensitivitySelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_device_info_returns_identifiers(self):
        from custom_components.bosch_shc_camera import DOMAIN

        sel = self._make()
        info = sel.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]

    @pytest.mark.asyncio
    async def test_select_option_success_updates_motion_data(self):
        sel = self._make(put_return=True)
        sel.coordinator.data[CAM_ID]["motion"] = {
            "motionAlarmConfiguration": "HIGH",
            "enabled": True,
        }
        with (
            patch(
                "custom_components.bosch_shc_camera.select._is_gen2_indoor",
                return_value=False,
            ),
            patch(
                "custom_components.bosch_shc_camera.select._warn_if_privacy_on",
                AsyncMock(return_value=False),
            ),
        ):
            await sel.async_select_option("low")
        assert (
            sel.coordinator.data[CAM_ID]["motion"]["motionAlarmConfiguration"] == "LOW"
        )
        # Write-lock stamped so the slow-tier poll won't revert the optimistic
        # value before the cloud catches up.
        assert CAM_ID in sel.coordinator._motion_set_at

    @pytest.mark.asyncio
    async def test_select_option_failure_logs_warning(self):
        """PUT fails → warning logged, state still written."""
        sel = self._make(put_return=False)
        with (
            patch(
                "custom_components.bosch_shc_camera.select._is_gen2_indoor",
                return_value=False,
            ),
            patch(
                "custom_components.bosch_shc_camera.select._warn_if_privacy_on",
                AsyncMock(return_value=False),
            ),
        ):
            await sel.async_select_option("medium_high")
        sel.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_option_skipped_when_privacy_on(self):
        """gen2 indoor + privacy ON → returns early, no PUT."""
        sel = self._make()
        with (
            patch(
                "custom_components.bosch_shc_camera.select._is_gen2_indoor",
                return_value=True,
            ),
            patch(
                "custom_components.bosch_shc_camera.select._warn_if_privacy_on",
                AsyncMock(return_value=True),
            ),
        ):
            await sel.async_select_option("high")
        sel.coordinator.async_put_camera.assert_not_called()


class TestFcmPushModeSelectRestart:
    def _make(self, options=None):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        coord = _stub_coord_platform()
        entry = _stub_entry_platform(options=options or {})
        sel = BoschFcmPushModeSelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.hass.config_entries.async_update_entry = MagicMock()
        sel.hass.async_create_task = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_device_info_returns_identifiers(self):
        from custom_components.bosch_shc_camera import DOMAIN

        sel = self._make()
        info = sel.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]

    def test_available_false_when_fcm_disabled(self):
        """FCM push disabled in options → entity unavailable."""
        sel = self._make(options={"enable_fcm_push": False})
        sel.coordinator.options = {"enable_fcm_push": False}
        # Patch super().available to True so we only test our guard
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
            new_callable=lambda: property(lambda s: True),
        ):
            assert sel.available is False

    @pytest.mark.asyncio
    async def test_select_option_updates_entry_and_restarts_fcm(self):
        """Selecting a mode persists to options and restarts FCM when enabled."""
        sel = self._make(options={"enable_fcm_push": True})
        sel.coordinator.options = {"enable_fcm_push": True}
        # Restart task is tracked on coordinator._bg_tasks (cancelled on
        # unload) instead of fire-and-forget.
        sel.coordinator._bg_tasks = set()

        def _create(coro):
            coro.close()  # avoid ResourceWarning; we only assert tracking here
            return MagicMock()

        sel.hass.async_create_task = _create
        await sel.async_select_option("android")
        sel.hass.config_entries.async_update_entry.assert_called_once()
        sel.coordinator.async_stop_fcm_push.assert_called_once()
        sel.async_write_ha_state.assert_called_once()
        # Restart task registered for cancellation on unload.
        assert len(sel.coordinator._bg_tasks) == 1

    @pytest.mark.asyncio
    async def test_select_option_no_fcm_restart_when_disabled(self):
        """FCM disabled → no async_start_fcm_push called."""
        sel = self._make(options={"enable_fcm_push": False})
        sel.coordinator.options = {"enable_fcm_push": False}
        await sel.async_select_option("ios")
        sel.hass.async_create_task.assert_not_called()


class TestStreamModeSelectDeviceInfo:
    def _make(self):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        coord = _stub_coord_platform()
        entry = _stub_entry_platform()
        sel = BoschStreamModeSelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_device_info_returns_identifiers(self):
        from custom_components.bosch_shc_camera import DOMAIN

        sel = self._make()
        info = sel.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]


class TestDetectionModeSelect:
    def _make(self, intrusion_cache=None, put_return=True):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        coord = _stub_coord_platform(gen2=True)
        coord._intrusion_config_cache = intrusion_cache or {}
        coord.async_put_camera = AsyncMock(return_value=put_return)
        entry = _stub_entry_platform()
        sel = BoschDetectionModeSelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_construction(self):
        sel = self._make()
        assert sel._attr_translation_key == "detection_mode"
        assert CAM_ID in sel._attr_unique_id

    def test_device_info_returns_identifiers(self):
        from custom_components.bosch_shc_camera import DOMAIN

        sel = self._make()
        info = sel.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]

    def test_current_option_maps_api_value(self):
        sel = self._make(intrusion_cache={CAM_ID: {"detectionMode": "ONLY_HUMANS"}})
        assert sel.current_option == "only_humans"

    def test_current_option_invalid_returns_default(self):
        """Bug M8 fix: unknown API value must return first option (+ warning), not None."""
        from custom_components.bosch_shc_camera.select import DETECTION_MODE_OPTIONS

        sel = self._make(intrusion_cache={CAM_ID: {"detectionMode": "UNKNOWN_MODE"}})
        result = sel.current_option
        assert result == DETECTION_MODE_OPTIONS[0], (
            f"Unknown detectionMode should return first option, got {result!r}"
        )

    def test_current_option_empty_cache_returns_none(self):
        sel = self._make()
        assert sel.current_option is None

    def test_available_true_when_cache_populated(self):
        sel = self._make(intrusion_cache={CAM_ID: {"detectionMode": "ALL_MOTIONS"}})
        assert sel.available is True

    def test_available_false_when_cache_empty(self):
        sel = self._make()
        assert sel.available is False

    @pytest.mark.asyncio
    async def test_select_option_success_updates_cache(self):
        """Successful PUT → cache updated."""
        sel = self._make(
            intrusion_cache={CAM_ID: {"detectionMode": "ALL_MOTIONS", "enabled": True}},
            put_return=True,
        )
        with patch(
            "custom_components.bosch_shc_camera.select._warn_if_privacy_on",
            AsyncMock(return_value=False),
        ):
            await sel.async_select_option("only_humans")
        assert (
            sel.coordinator._intrusion_config_cache[CAM_ID]["detectionMode"]
            == "ONLY_HUMANS"
        )

    @pytest.mark.asyncio
    async def test_select_option_failure_logs_warning(self):
        sel = self._make(
            intrusion_cache={CAM_ID: {"detectionMode": "ALL_MOTIONS"}},
            put_return=False,
        )
        with patch(
            "custom_components.bosch_shc_camera.select._warn_if_privacy_on",
            AsyncMock(return_value=False),
        ):
            await sel.async_select_option("only_humans")
        sel.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_option_invalid_returns_early(self):
        """Invalid option string → no PUT called."""
        sel = self._make(intrusion_cache={CAM_ID: {}})
        await sel.async_select_option("invalid_mode")
        sel.coordinator.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_select_option_empty_config_returns_early(self):
        """Empty config cache → return early without PUT."""
        sel = self._make(intrusion_cache={})
        with patch(
            "custom_components.bosch_shc_camera.select._warn_if_privacy_on",
            AsyncMock(return_value=False),
        ):
            await sel.async_select_option("only_humans")
        sel.coordinator.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_select_option_skipped_when_privacy_on(self):
        """Privacy mode ON → returns early, no PUT."""
        sel = self._make(intrusion_cache={CAM_ID: {"detectionMode": "ALL_MOTIONS"}})
        with patch(
            "custom_components.bosch_shc_camera.select._warn_if_privacy_on",
            AsyncMock(return_value=True),
        ):
            await sel.async_select_option("only_humans")
        sel.coordinator.async_put_camera.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════
# PTZ named-preset select (Gen1 360°)
#
# Covers:
#   - Construction and metadata
#   - current_option: each of the 5 preset angles → matching name
#   - current_option: non-preset position → None
#   - current_option: cache empty → None
#   - current_option: ceiling-mount (image_rotation_180) sign inversion
#   - available: True / False conditions
#   - async_select_option: each preset → async_cloud_set_pan called with correct angle
#   - async_select_option: unknown option → no call
#   - async_select_option: pan failure → cache NOT updated
#   - async_select_option: ceiling-mount → angle inverted before PUT
#   - PIN_EVERY_MODE: one test per preset value (home / left / right / back_left / back_right)
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture
def stub_coord_pan():
    """Minimal coordinator stub for PTZ preset tests."""
    coord = SimpleNamespace(
        data={
            PAN_CAM_ID: {
                "info": {
                    "title": "Kamera",
                    "hardwareVersion": "CAMERA_360",
                    "firmwareVersion": "7.91.56",
                    "macAddress": "aa:bb:cc:08:36:27",
                    "featureSupport": {"panLimit": 120},
                }
            }
        },
        _pan_cache={PAN_CAM_ID: 0},  # parked at home position
        _image_rotation_180={},
        last_update_success=True,
        async_cloud_set_pan=AsyncMock(return_value=True),
    )
    return coord


def _make_sel(coord: SimpleNamespace, entry: SimpleNamespace) -> object:
    from custom_components.bosch_shc_camera.select import BoschPanPresetSelect

    return BoschPanPresetSelect(coord, PAN_CAM_ID, entry, pan_limit=120)


class TestPanPresetConstruction:
    def test_translation_key(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel._attr_translation_key == "pan_preset"  # type: ignore[attr-defined]

    def test_unique_id(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel._attr_unique_id == f"bosch_shc_camera_{PAN_CAM_ID}_pan_preset"  # type: ignore[attr-defined]

    def test_options_list(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        from custom_components.bosch_shc_camera.select import PAN_PRESET_OPTIONS

        assert sel._attr_options == PAN_PRESET_OPTIONS  # type: ignore[attr-defined]
        assert len(sel._attr_options) == 5  # type: ignore[attr-defined]


class TestCurrentOptionPresetPositions:
    """Each of the 5 named angles must map back to the correct preset name."""

    def test_home_position(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 0
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "home"  # type: ignore[attr-defined]

    def test_left_position(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache[PAN_CAM_ID] = -60
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "left"  # type: ignore[attr-defined]

    def test_right_position(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 60
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "right"  # type: ignore[attr-defined]

    def test_back_left_position(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache[PAN_CAM_ID] = -120
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "back_left"  # type: ignore[attr-defined]

    def test_back_right_position(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 120
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "back_right"  # type: ignore[attr-defined]

    def test_between_presets_returns_none(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        """Manual slider move to a non-preset angle → current_option is None."""
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 45
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option is None  # type: ignore[attr-defined]

    def test_cache_empty_returns_none(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache = {}
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option is None  # type: ignore[attr-defined]


class TestCurrentOptionCeilingMount:
    """When _image_rotation_180 is set the pan cache value sign is inverted
    before comparing against preset angles — so the user sees the correct
    preset name even when the camera is ceiling-mounted.
    """

    def test_ceiling_home(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._image_rotation_180 = {PAN_CAM_ID: True}
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 0  # 0 inverted = 0 → home
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "home"  # type: ignore[attr-defined]

    def test_ceiling_left_shows_left(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        # Camera reports raw +60 (physical right); inversion → user sees −60 (left)
        stub_coord_pan._image_rotation_180 = {PAN_CAM_ID: True}
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 60
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "left"  # type: ignore[attr-defined]

    def test_ceiling_right_shows_right(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._image_rotation_180 = {PAN_CAM_ID: True}
        stub_coord_pan._pan_cache[PAN_CAM_ID] = -60
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.current_option == "right"  # type: ignore[attr-defined]


class TestAvailable:
    def test_available_when_cache_populated(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 0
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.available is True  # type: ignore[attr-defined]

    def test_not_available_when_cache_missing(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._pan_cache = {}
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.available is False  # type: ignore[attr-defined]

    def test_not_available_when_coordinator_failed(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan.last_update_success = False
        sel = _make_sel(stub_coord_pan, stub_entry)
        assert sel.available is False  # type: ignore[attr-defined]


class TestSelectOption:
    """Each preset must result in the correct angle being passed to async_cloud_set_pan."""

    @pytest.mark.asyncio
    async def test_select_home(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("home")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, 0)
        assert stub_coord_pan._pan_cache[PAN_CAM_ID] == 0

    @pytest.mark.asyncio
    async def test_select_left(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("left")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, -60)
        assert stub_coord_pan._pan_cache[PAN_CAM_ID] == -60

    @pytest.mark.asyncio
    async def test_select_right(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("right")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, 60)
        assert stub_coord_pan._pan_cache[PAN_CAM_ID] == 60

    @pytest.mark.asyncio
    async def test_select_back_left(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("back_left")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, -120)
        assert stub_coord_pan._pan_cache[PAN_CAM_ID] == -120

    @pytest.mark.asyncio
    async def test_select_back_right(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("back_right")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, 120)
        assert stub_coord_pan._pan_cache[PAN_CAM_ID] == 120

    @pytest.mark.asyncio
    async def test_unknown_option_no_call(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        """Invalid options must be silently rejected without calling the API."""
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("garbage_value")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_failure_does_not_update_cache(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        """When async_cloud_set_pan returns False the cache must not be updated."""
        stub_coord_pan.async_cloud_set_pan = AsyncMock(return_value=False)
        stub_coord_pan._pan_cache[PAN_CAM_ID] = 0  # parked at home
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("right")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, 60)
        # Cache must remain at 0 (unchanged) — no optimistic update on failure
        assert stub_coord_pan._pan_cache[PAN_CAM_ID] == 0


class TestSelectOptionCeilingMount:
    """For ceiling-mounted cameras the angle sent to the API must be sign-inverted."""

    @pytest.mark.asyncio
    async def test_right_inverted_to_minus60_on_ceiling(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._image_rotation_180 = {PAN_CAM_ID: True}
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("right")  # type: ignore[attr-defined]
        # "right" = +60 user-visible → inverted → -60 physical → API must receive -60
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, -60)

    @pytest.mark.asyncio
    async def test_left_inverted_to_plus60_on_ceiling(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._image_rotation_180 = {PAN_CAM_ID: True}
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("left")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, 60)

    @pytest.mark.asyncio
    async def test_home_not_inverted_on_ceiling(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord_pan._image_rotation_180 = {PAN_CAM_ID: True}
        sel = _make_sel(stub_coord_pan, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("home")  # type: ignore[attr-defined]
        stub_coord_pan.async_cloud_set_pan.assert_called_once_with(PAN_CAM_ID, 0)


class TestPanPresetSetupEntry:
    """BoschPanPresetSelect is created when panLimit > 0, skipped when panLimit == 0."""

    def test_created_when_pan_limit_positive(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from custom_components.bosch_shc_camera.select import (
            PAN_PRESET_OPTIONS,
            BoschPanPresetSelect,
        )

        sel = BoschPanPresetSelect(
            stub_coord_pan, PAN_CAM_ID, stub_entry, pan_limit=120
        )
        assert isinstance(sel, BoschPanPresetSelect)
        assert sel._attr_options == PAN_PRESET_OPTIONS  # type: ignore[attr-defined]

    def test_pan_preset_options_count(
        self, stub_coord_pan: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        from custom_components.bosch_shc_camera.select import PAN_PRESET_OPTIONS

        assert len(PAN_PRESET_OPTIONS) == 5

    def test_pan_preset_angles_all_in_range(self) -> None:
        """All mapped angles must be within the default ±120° pan range."""
        from custom_components.bosch_shc_camera.select import PAN_PRESET_ANGLES

        for name, angle in PAN_PRESET_ANGLES.items():
            assert -120 <= angle <= 120, (
                f"Preset {name!r} angle {angle} out of ±120° range"
            )


# ═════════════════════════════════════════════════════════════════════════
# `enable_ptz_controls` opt-in toggle gating
#
# PIN_EVERY_MODE: explicit tests for default, disabled, enabled, and the
# panLimit gate (so enabling the toggle on a non-pan camera still creates
# no entity).
#
# Covers:
#   - DEFAULT_OPTIONS contains `enable_ptz_controls: False`
#   - select platform: panLimit > 0 + toggle OFF → no BoschPanPresetSelect
#   - select platform: panLimit > 0 + toggle ON  → BoschPanPresetSelect created
#   - select platform: panLimit = 0 + toggle ON  → still no entity
# ═════════════════════════════════════════════════════════════════════════

CAM_PAN = "22222222-AAAA-BBBB-CCCC-000000000001"  # CAMERA_360, has pan
CAM_NOPAN = "11111111-AAAA-BBBB-CCCC-000000000001"  # Gen2 outdoor, no pan


def _coord(*, with_pan: bool):
    pan_limit = 120 if with_pan else 0
    hw = "CAMERA_360" if with_pan else "HOME_Eyes_Outdoor"
    cam_id = CAM_PAN if with_pan else CAM_NOPAN
    return SimpleNamespace(
        data={
            cam_id: {
                "info": {
                    "title": "Test cam",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:00:00:00",
                    "featureSupport": {"panLimit": pan_limit},
                }
            }
        },
        _pan_cache={cam_id: 0},
        _image_rotation_180={},
        last_update_success=True,
        async_cloud_set_pan=AsyncMock(return_value=True),
    )


def _entry(*, ptz_enabled: bool):
    return SimpleNamespace(
        options={CONF_ENABLE_PTZ_CONTROLS: ptz_enabled},
        entry_id="01TEST",
        title="Bosch",
        runtime_data=None,
    )


def test_default_options_has_ptz_disabled() -> None:
    """`enable_ptz_controls` must be in DEFAULT_OPTIONS and False by default."""
    assert "enable_ptz_controls" in DEFAULT_OPTIONS
    assert DEFAULT_OPTIONS["enable_ptz_controls"] is False


@pytest.mark.asyncio
async def test_pan_select_NOT_created_when_toggle_disabled() -> None:
    """panLimit > 0 + toggle off → no BoschPanPresetSelect entity."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    entry = _entry(ptz_enabled=False)
    entry.runtime_data = coord
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == [], (
        f"BoschPanPresetSelect must NOT be created when enable_ptz_controls=False. "
        f"Got {len(pan_selects)} entities."
    )


@pytest.mark.asyncio
async def test_pan_select_created_when_toggle_enabled() -> None:
    """panLimit > 0 + toggle on → exactly one BoschPanPresetSelect entity."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    entry = _entry(ptz_enabled=True)
    entry.runtime_data = coord
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert len(pan_selects) == 1


@pytest.mark.asyncio
async def test_pan_select_NOT_created_on_non_pan_camera_even_when_toggle_enabled() -> (
    None
):
    """panLimit = 0 + toggle on → still no entity (gate is panLimit AND toggle)."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=False)
    entry = _entry(ptz_enabled=True)
    entry.runtime_data = coord
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == []


@pytest.mark.asyncio
async def test_pan_select_options_value_missing_treated_as_disabled() -> None:
    """Missing key in options dict → fallback False → entity not created."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    entry = SimpleNamespace(
        options={},  # key missing entirely
        entry_id="01TEST",
        title="Bosch",
        runtime_data=coord,
    )
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == [], "Missing key must be treated as disabled (default off)."


@pytest.mark.asyncio
async def test_pan_select_options_value_garbage_collapses_to_disabled() -> None:
    """Non-bool truthy garbage in options collapses to disabled via bool-coerce
    in config_flow.py. Here we test that the runtime gate works either way:
    only literal True enables. (None / 0 / "" → False.)
    """
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    # explicit None — config_flow bool-coerce makes this False on submit, but
    # legacy options dicts written before the field existed contain no entry.
    entry = SimpleNamespace(
        options={CONF_ENABLE_PTZ_CONTROLS: None},
        entry_id="01TEST",
        title="Bosch",
        runtime_data=coord,
    )
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == []


# ─────────────────────────────────────────────────────────────────────────────
# Section: BoschFcmPushModeSelect.available (relocated from
# tests/test_shc_select_remaining_lines.py)
# ─────────────────────────────────────────────────────────────────────────────


class TestFcmPushModeSelectAvailableSuperFalse:
    """`available` must short-circuit to False when `super().available` is
    False, and separately require `enable_fcm_push` when super() is True."""

    def _make(self, enable_fcm_push: bool = True):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                    },
                }
            },
            options={"enable_fcm_push": enable_fcm_push},
            last_update_success=False,  # makes CoordinatorEntity.available False
            async_stop_fcm_push=AsyncMock(),
            async_start_fcm_push=AsyncMock(),
            async_update_listeners=lambda: None,
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        sel = BoschFcmPushModeSelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_available_false_when_super_returns_false(self):
        """super().available is False → available returns False immediately."""
        sel = self._make(enable_fcm_push=True)
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
            new_callable=lambda: property(lambda s: False),
        ):
            assert sel.available is False

    def test_available_false_when_super_true_but_fcm_disabled(self):
        """super().available True but enable_fcm_push False → still False."""
        sel = self._make(enable_fcm_push=False)
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
            new_callable=lambda: property(lambda s: True),
        ):
            assert sel.available is False

    def test_available_true_when_super_true_and_fcm_enabled(self):
        """Positive path: super() True + enable_fcm_push True → available True."""
        sel = self._make(enable_fcm_push=True)
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
            new_callable=lambda: property(lambda s: True),
        ):
            assert sel.available is True


# ── BoschFcmPushModeSelect: restart-task lifecycle tracking ──────────────────
@pytest.mark.asyncio
async def test_fcm_mode_select_tracks_restart_task() -> None:
    """Selecting a new FCM push mode must register the async_start_fcm_push()
    task in coordinator._bg_tasks so async_unload_entry can cancel it — an
    untracked fire-and-forget task could keep running (and re-establish FCM)
    after the entry was unloaded."""
    from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

    coordinator = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        options={"enable_fcm_push": True},
        _fcm_push_mode="auto",
        _bg_tasks=set(),
        async_stop_fcm_push=AsyncMock(),
        async_start_fcm_push=AsyncMock(),
        last_update_success=True,
    )
    entry = SimpleNamespace(entry_id="01ENTRY", options={"fcm_push_mode": "auto"})

    sel = BoschFcmPushModeSelect(coordinator, CAM_ID, entry)
    # Stand in for the HA-managed attributes the entity would get once added.
    sel.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
        async_create_task=lambda coro, **kw: asyncio.ensure_future(coro),
    )
    sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]

    await sel.async_select_option("all")

    # Task is registered before it completes.
    assert len(coordinator._bg_tasks) == 1
    coordinator.async_stop_fcm_push.assert_awaited_once()

    # Let the scheduled task run, then a further tick for the done-callback
    # (add_done_callback fires via call_soon on the next loop iteration).
    for _ in range(5):
        await asyncio.sleep(0)
        if not coordinator._bg_tasks:
            break
    coordinator.async_start_fcm_push.assert_awaited_once()
    assert len(coordinator._bg_tasks) == 0
