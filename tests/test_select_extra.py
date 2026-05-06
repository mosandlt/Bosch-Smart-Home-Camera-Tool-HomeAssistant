"""Select-entity coverage round 2 — current_option fallback chains
and the option-key contracts the integration silently relies on.

Most select entities use a tiered fallback chain to derive the
displayed option:
  1. In-memory override (`coordinator._stream_type_override`)
  2. Persisted entry option (`get_options(entry)["..."]`)
  3. Hard-coded default ("auto" / first option)
  4. None when the underlying data isn't fetched yet (slow-tier)

A regression in any tier silently flips the dropdown to the wrong
position — users notice only after their settings appear to "reset"
themselves on integration reload. These tests pin the fallback order
plus the option-key constants the JSON translations + APIs depend on.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def stub_coord():
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
        async_put_camera=AsyncMock(return_value=True),
        async_stop_fcm_push=AsyncMock(),
        async_start_fcm_push=AsyncMock(),
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={},
        options={"stream_connection_type": "auto", "fcm_push_mode": "auto"},
    )


# ── Option-key constants — pin the strings APIs/translations depend on ─


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
            MOTION_SENSITIVITY_OPTIONS, SENSITIVITY_TO_API,
        )
        # 6 levels including OFF — Bosch's PUT /motion accepts these UPPER-cased.
        assert MOTION_SENSITIVITY_OPTIONS == [
            "super_high", "high", "medium_high", "medium_low", "low", "off",
        ]
        # Wire format is upper-snake. The mapping must be 1:1 to prevent
        # a typo silently dropping levels (regression seen in v10.4.x).
        for key in MOTION_SENSITIVITY_OPTIONS:
            assert SENSITIVITY_TO_API[key] == key.upper()

    def test_detection_mode_options_pinned(self):
        from custom_components.bosch_shc_camera.select import DETECTION_MODE_OPTIONS
        assert DETECTION_MODE_OPTIONS == ["all_motions", "only_humans", "zones"]

    def test_fcm_push_mode_options_pinned(self):
        from custom_components.bosch_shc_camera.select import FCM_PUSH_MODE_OPTIONS
        # Order matters for the dropdown in the UI.
        assert FCM_PUSH_MODE_OPTIONS == ["auto", "android", "ios", "polling"]


# ── BoschStreamModeSelect: in-memory override beats persisted option ────


class TestStreamModeSelect:
    def test_override_takes_precedence_over_options(self, stub_coord, stub_entry):
        """When the user changes the dropdown live, `_stream_type_override`
        wins over the persisted option until the integration reloads.
        Otherwise the next coordinator tick would flip the dropdown back."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        stub_coord._stream_type_override = "local"
        stub_entry.options["stream_connection_type"] = "auto"
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "local"

    def test_falls_back_to_persisted_option(self, stub_coord, stub_entry):
        """Without an in-memory override, persisted option wins."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        stub_coord._stream_type_override = None
        stub_entry.options["stream_connection_type"] = "remote"
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "remote"

    def test_unknown_value_collapses_to_auto(self, stub_coord, stub_entry):
        """Garbage in the entry options must not poison the dropdown —
        the select entity would refuse to render an out-of-list value."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        stub_coord._stream_type_override = "made-up-mode"
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "auto"

    @pytest.mark.asyncio
    async def test_select_option_writes_override(self, stub_coord, stub_entry):
        """User picks 'remote' in the dropdown → `_stream_type_override`
        flips immediately. Takes effect on the next stream activation,
        no integration reload required."""
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("remote")
        assert stub_coord._stream_type_override == "remote"
        sel.async_write_ha_state.assert_called_once()


# ── BoschFcmPushModeSelect: availability + option fallback ──────────────


class TestFcmPushModeSelect:
    def test_unavailable_when_fcm_disabled(self, stub_coord, stub_entry):
        """If the integration option `enable_fcm_push` is False, the
        dropdown must show 'unavailable' so the user knows toggling here
        does nothing — the master switch lives in integration options."""
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        stub_coord.options["enable_fcm_push"] = False
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        # Bypass CoordinatorEntity.available chain (it needs hass + last_update)
        # by calling our class' available logic directly via name-mangled super.
        # Easier: stub last_update_success and rely on the conditional.
        # The class check is `not super().available -> False` then bool(opt).
        # CoordinatorEntity.available checks `self.coordinator.last_update_success`.
        assert sel.available is False, (
            "FCM push disabled in integration options must surface as "
            "'Unavailable' so the user explicitly sees the master toggle."
        )

    def test_available_when_fcm_enabled(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        stub_coord.options["enable_fcm_push"] = True
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel.available is True

    def test_current_option_reads_entry_options(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        stub_entry.options["fcm_push_mode"] = "ios"
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "ios"

    def test_current_option_default_auto(self, stub_coord, stub_entry):
        """Missing option key → 'auto'. Stable default ensures fresh
        installs land on the cross-platform-safe mode."""
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        stub_entry.options.pop("fcm_push_mode", None)
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "auto"

    def test_current_option_unknown_collapses(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        stub_entry.options["fcm_push_mode"] = "junk"
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "auto"


# ── BoschMotionSensitivitySelect: slow-tier availability + lowercase ────


class TestMotionSensitivitySelect:
    def test_current_option_lowercases_api_value(self, stub_coord, stub_entry):
        """Bosch API returns UPPER-snake (HIGH); the select entity's option
        key list is lower-snake (high). Without lower-casing, the UI shows
        the raw API value as a literal label and the dropdown mismatches."""
        stub_coord.motion_settings = lambda cid: {"motionAlarmConfiguration": "HIGH"}
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option == "high"

    def test_current_option_none_when_motion_unfetched(self, stub_coord, stub_entry):
        """Slow-tier data not yet pulled → None (HA renders 'unknown')
        instead of an arbitrary default that might mismatch the camera."""
        stub_coord.motion_settings = lambda cid: {}
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option is None

    def test_current_option_none_for_unknown_api_value(self, stub_coord, stub_entry):
        """If Bosch ever returns a level we don't list (e.g. the rumored
        EXTREME mode on Gen3), surface as 'unknown' rather than guessing
        — the user sees a missing label and we get a bug report instead
        of a silently wrong sensitivity."""
        stub_coord.motion_settings = lambda cid: {"motionAlarmConfiguration": "EXTREME"}
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel.current_option is None

    def test_unavailable_when_motion_settings_empty(self, stub_coord, stub_entry):
        """Slow tier hasn't run yet → entity unavailable. Avoids the
        select rendering with a stale 'auto' that the user might click,
        which would issue a write before the read populated the cache."""
        stub_coord.motion_settings = lambda cid: {}
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel.available is False

    def test_disabled_by_default_in_registry(self, stub_coord, stub_entry):
        """Hidden by default — too granular for most users; expose only
        when explicitly enabled via Settings → Entities."""
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_entity_registry_enabled_default is False

    @pytest.mark.asyncio
    async def test_select_option_uppercases_for_api(self, stub_coord, stub_entry):
        """The entity stores keys lowercased ('high'); Bosch API needs
        'HIGH'. The mapping happens in async_select_option — verify it."""
        # Don't trip the gen2 indoor privacy guard
        stub_coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        stub_coord.motion_settings = lambda cid: {
            "motionAlarmConfiguration": "MEDIUM_HIGH", "enabled": True,
        }
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("high")
        # Must be called with UPPER-cased value
        stub_coord.async_put_camera.assert_called_once()
        args, kwargs = stub_coord.async_put_camera.call_args
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
    async def test_invalid_option_silently_no_op(self, stub_coord, stub_entry):
        """An option outside the list must not call the API. Defends
        against typos in dashboard service calls (`select.select_option`
        with an arbitrary value)."""
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option("bogus_level")
        stub_coord.async_put_camera.assert_not_called()


# ── BoschVideoQualitySelect: persisted option + auto fallback ──────────


class TestVideoQualitySelectExtra:
    def test_current_option_passes_through_known_value(self, stub_coord, stub_entry):
        """get_quality returns the active level — must round-trip if
        in the option list."""
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        for opt in sel._attr_options:
            stub_coord.get_quality = lambda cid, _o=opt: _o
            sel2 = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
            assert sel2.current_option == opt, (
                f"Quality '{opt}' must round-trip through current_option. "
                f"If the option list and the coordinator drift, the "
                f"dropdown silently snaps to 'auto' for valid values."
            )
