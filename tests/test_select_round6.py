"""select.py — Sprint-A round-6 tests.

Covers the 39% gap (lines 48-64, 93-137, 176-178, 212, 228, 261-299,
338-340, 382-441) for async_setup_entry, restore, select_option, and
BoschDetectionModeSelect.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM2_ID = "20E053B5-OTHER"


def _stub_coord(gen2: bool = True):
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
        _fcm_push_mode="unknown",
        motion_settings=lambda cam_id: {"motionAlarmConfiguration": "HIGH", "enabled": True},
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


def _stub_entry(options=None):
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={},
        options=options or {},
        runtime_data=None,
    )


# ── async_setup_entry ─────────────────────────────────────────────────────────


class TestSetupEntry:
    @pytest.mark.asyncio
    async def test_gen2_adds_detection_mode_select(self):
        from custom_components.bosch_shc_camera.select import (
            async_setup_entry, BoschDetectionModeSelect,
        )
        coord = _stub_coord(gen2=True)
        entry = _stub_entry()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(hass=None, config_entry=entry,
                                async_add_entities=lambda e: captured.extend(e))
        types_ = {type(e).__name__ for e in captured}
        assert "BoschDetectionModeSelect" in types_

    @pytest.mark.asyncio
    async def test_gen1_no_detection_mode_select(self):
        from custom_components.bosch_shc_camera.select import (
            async_setup_entry, BoschDetectionModeSelect,
        )
        coord = _stub_coord(gen2=False)
        entry = _stub_entry()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(hass=None, config_entry=entry,
                                async_add_entities=lambda e: captured.extend(e))
        types_ = {type(e).__name__ for e in captured}
        assert "BoschDetectionModeSelect" not in types_

    @pytest.mark.asyncio
    async def test_integration_level_selects_added(self):
        from custom_components.bosch_shc_camera.select import (
            async_setup_entry, BoschFcmPushModeSelect, BoschStreamModeSelect,
        )
        coord = _stub_coord()
        entry = _stub_entry()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(hass=None, config_entry=entry,
                                async_add_entities=lambda e: captured.extend(e))
        types_ = {type(e).__name__ for e in captured}
        assert "BoschFcmPushModeSelect" in types_
        assert "BoschStreamModeSelect" in types_

    @pytest.mark.asyncio
    async def test_empty_data_no_entities(self):
        from custom_components.bosch_shc_camera.select import async_setup_entry
        coord = _stub_coord()
        coord.data = {}
        entry = _stub_entry()
        entry.runtime_data = coord
        captured: list = []
        await async_setup_entry(hass=None, config_entry=entry,
                                async_add_entities=lambda e: captured.extend(e))
        assert captured == []


# ── BoschVideoQualitySelect ───────────────────────────────────────────────────


class TestVideoQualitySelect:
    def _make(self, coord=None):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        coord = coord or _stub_coord()
        entry = _stub_entry()
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
        with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                   _noop), \
             patch.object(sel, "async_get_last_state", AsyncMock(return_value=last)):
            await sel.async_added_to_hass()
        sel.coordinator.set_quality.assert_called_once_with(CAM_ID, "high")

    @pytest.mark.asyncio
    async def test_async_added_to_hass_legacy_mapping(self):
        """Legacy display text 'Auto' maps to 'auto'."""
        sel = self._make()
        last = MagicMock()
        last.state = "Auto"
        sel.coordinator.set_quality = MagicMock()
        with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                   AsyncMock()), \
             patch.object(sel, "async_get_last_state", AsyncMock(return_value=last)):
            await sel.async_added_to_hass()
        sel.coordinator.set_quality.assert_called_with(CAM_ID, "auto")

    @pytest.mark.asyncio
    async def test_async_added_to_hass_no_last_state(self):
        """No saved state → coordinator.set_quality NOT called."""
        sel = self._make()
        sel.coordinator.set_quality = MagicMock()
        with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                   AsyncMock()), \
             patch.object(sel, "async_get_last_state", AsyncMock(return_value=None)):
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
        coord = _stub_coord()
        coord.data[CAM_ID]["live"] = {"rtspsUrl": "rtsps://old"}
        coord.set_quality = MagicMock()
        sel = self._make(coord)
        await sel.async_select_option("low")
        coord.try_live_connection.assert_called_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_async_select_option_reconnect_exception_swallowed(self):
        """Reconnect raising an exception must not propagate (graceful degradation)."""
        coord = _stub_coord()
        coord.data[CAM_ID]["live"] = {"rtspsUrl": "rtsps://old"}
        coord.set_quality = MagicMock()
        coord.try_live_connection = AsyncMock(side_effect=RuntimeError("boom"))
        sel = self._make(coord)
        await sel.async_select_option("low")  # must not raise
        sel.async_write_ha_state.assert_called_once()


# ── BoschMotionSensitivitySelect ──────────────────────────────────────────────


class TestMotionSensitivitySelect:
    def _make(self, coord=None, put_return=True):
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        coord = coord or _stub_coord()
        coord.async_put_camera = AsyncMock(return_value=put_return)
        entry = _stub_entry()
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
        sel.coordinator.data[CAM_ID]["motion"] = {"motionAlarmConfiguration": "HIGH", "enabled": True}
        with patch("custom_components.bosch_shc_camera.switch._is_gen2_indoor", return_value=False), \
             patch("custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
                   AsyncMock(return_value=False)):
            await sel.async_select_option("low")
        assert sel.coordinator.data[CAM_ID]["motion"]["motionAlarmConfiguration"] == "LOW"

    @pytest.mark.asyncio
    async def test_select_option_failure_logs_warning(self):
        """PUT fails → warning logged, state still written."""
        sel = self._make(put_return=False)
        with patch("custom_components.bosch_shc_camera.switch._is_gen2_indoor", return_value=False), \
             patch("custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
                   AsyncMock(return_value=False)):
            await sel.async_select_option("medium_high")
        sel.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_option_skipped_when_privacy_on(self):
        """gen2 indoor + privacy ON → returns early, no PUT."""
        sel = self._make()
        with patch("custom_components.bosch_shc_camera.switch._is_gen2_indoor", return_value=True), \
             patch("custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
                   AsyncMock(return_value=True)):
            await sel.async_select_option("high")
        sel.coordinator.async_put_camera.assert_not_called()


# ── BoschFcmPushModeSelect ────────────────────────────────────────────────────


class TestFcmPushModeSelect:
    def _make(self, options=None):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        coord = _stub_coord()
        entry = _stub_entry(options=options or {})
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
        with patch("homeassistant.helpers.update_coordinator.CoordinatorEntity.available",
                   new_callable=lambda: property(lambda s: True)):
            assert sel.available is False

    @pytest.mark.asyncio
    async def test_select_option_updates_entry_and_restarts_fcm(self):
        """Selecting a mode persists to options and restarts FCM when enabled."""
        sel = self._make(options={"enable_fcm_push": True})
        sel.coordinator.options = {"enable_fcm_push": True}
        # Drain coroutines created via async_create_task to avoid ResourceWarning
        sel.hass.async_create_task = lambda coro: coro.close()
        await sel.async_select_option("android")
        sel.hass.config_entries.async_update_entry.assert_called_once()
        sel.coordinator.async_stop_fcm_push.assert_called_once()
        sel.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_select_option_no_fcm_restart_when_disabled(self):
        """FCM disabled → no async_start_fcm_push called."""
        sel = self._make(options={"enable_fcm_push": False})
        sel.coordinator.options = {"enable_fcm_push": False}
        await sel.async_select_option("ios")
        sel.hass.async_create_task.assert_not_called()


# ── BoschStreamModeSelect ─────────────────────────────────────────────────────


class TestStreamModeSelect:
    def _make(self):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        coord = _stub_coord()
        entry = _stub_entry()
        sel = BoschStreamModeSelect(coord, CAM_ID, entry)
        sel.hass = MagicMock()
        sel.async_write_ha_state = MagicMock()
        return sel

    def test_device_info_returns_identifiers(self):
        from custom_components.bosch_shc_camera import DOMAIN
        sel = self._make()
        info = sel.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]


# ── BoschDetectionModeSelect ──────────────────────────────────────────────────


class TestDetectionModeSelect:
    def _make(self, intrusion_cache=None, put_return=True):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect
        coord = _stub_coord(gen2=True)
        coord._intrusion_config_cache = intrusion_cache or {}
        coord.async_put_camera = AsyncMock(return_value=put_return)
        entry = _stub_entry()
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

    def test_current_option_invalid_returns_none(self):
        sel = self._make(intrusion_cache={CAM_ID: {"detectionMode": "UNKNOWN_MODE"}})
        assert sel.current_option is None

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
        with patch("custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
                   AsyncMock(return_value=False)):
            await sel.async_select_option("only_humans")
        assert sel.coordinator._intrusion_config_cache[CAM_ID]["detectionMode"] == "ONLY_HUMANS"

    @pytest.mark.asyncio
    async def test_select_option_failure_logs_warning(self):
        sel = self._make(
            intrusion_cache={CAM_ID: {"detectionMode": "ALL_MOTIONS"}},
            put_return=False,
        )
        with patch("custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
                   AsyncMock(return_value=False)):
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
        with patch("custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
                   AsyncMock(return_value=False)):
            await sel.async_select_option("only_humans")
        sel.coordinator.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_select_option_skipped_when_privacy_on(self):
        """Privacy mode ON → returns early, no PUT."""
        sel = self._make(intrusion_cache={CAM_ID: {"detectionMode": "ALL_MOTIONS"}})
        with patch("custom_components.bosch_shc_camera.switch._warn_if_privacy_on",
                   AsyncMock(return_value=True)):
            await sel.async_select_option("only_humans")
        sel.coordinator.async_put_camera.assert_not_called()
