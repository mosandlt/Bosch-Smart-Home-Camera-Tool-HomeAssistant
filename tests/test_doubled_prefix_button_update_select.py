"""Regression tests for the doubled-prefix entity_id bug.

Source: Andrew75 forum post 998974/15 (2026-05-15) reported entity IDs like
  button.bosch_est_bosch_est_refresh_snapshot
instead of
  button.bosch_est_refresh_snapshot

Root cause: classes with `_attr_has_entity_name = True` AND
`_attr_name = f"Bosch {self._cam_title} <Suffix>"` cause HA to prepend the
device name automatically AND we re-prepended it manually in the constructor.

Fix: strip the "Bosch <cam_title>" prefix from _attr_name — keep only the
suffix literal.  Because strings.json has no entity.<platform>.<key>.name
entries for these classes, we leave _attr_name as a suffix string literal
rather than setting it to None.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_TITLE = "Terrasse"


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": CAM_TITLE,
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


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschRefreshSnapshotButton ──────────────────────────────────────────────

class TestRefreshSnapshotButtonNaming:
    def test_name_does_not_contain_bosch_prefix(self, stub_coord, stub_entry):
        """_attr_name must not start with 'Bosch ' — that double-prefixes the entity_id."""
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        assert not btn._attr_name.startswith("Bosch "), (
            f"_attr_name '{btn._attr_name}' still contains the 'Bosch' prefix — "
            "entity_id would be doubled"
        )

    def test_name_is_refresh_snapshot_suffix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        assert btn._attr_name == "Refresh Snapshot"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton
        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        assert btn._attr_has_entity_name is True


# ── BoschFirmwareUpdate ─────────────────────────────────────────────────────

class TestFirmwareUpdateNaming:
    def test_name_does_not_contain_bosch_prefix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert not u._attr_name.startswith("Bosch "), (
            f"_attr_name '{u._attr_name}' still contains the 'Bosch' prefix"
        )

    def test_name_is_firmware_suffix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_name == "Firmware"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate
        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_has_entity_name is True


# ── BoschVideoQualitySelect ─────────────────────────────────────────────────

class TestVideoQualitySelectNaming:
    def test_name_does_not_contain_bosch_prefix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert not sel._attr_name.startswith("Bosch "), (
            f"_attr_name '{sel._attr_name}' still contains the 'Bosch' prefix"
        )

    def test_name_is_video_quality_suffix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_name == "Video Quality"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect
        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschMotionSensitivitySelect ────────────────────────────────────────────

class TestMotionSensitivitySelectNaming:
    def test_name_does_not_contain_bosch_prefix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert not sel._attr_name.startswith("Bosch "), (
            f"_attr_name '{sel._attr_name}' still contains the 'Bosch' prefix"
        )

    def test_name_is_motion_sensitivity_suffix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_name == "Motion Sensitivity"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschMotionSensitivitySelect
        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschFcmPushModeSelect ──────────────────────────────────────────────────

class TestFcmPushModeSelectNaming:
    def test_name_does_not_contain_bosch_prefix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert not sel._attr_name.startswith("Bosch "), (
            f"_attr_name '{sel._attr_name}' still contains the 'Bosch' prefix"
        )

    def test_name_is_fcm_push_mode_suffix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_name == "FCM Push Mode"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect
        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschStreamModeSelect ───────────────────────────────────────────────────

class TestStreamModeSelectNaming:
    def test_name_does_not_contain_bosch_prefix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert not sel._attr_name.startswith("Bosch "), (
            f"_attr_name '{sel._attr_name}' still contains the 'Bosch' prefix"
        )

    def test_name_is_stream_modus_suffix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_name == "Stream Modus"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect
        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschDetectionModeSelect ────────────────────────────────────────────────

class TestDetectionModeSelectNaming:
    def test_name_does_not_contain_bosch_prefix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect
        sel = BoschDetectionModeSelect(stub_coord, CAM_ID, stub_entry)
        assert not sel._attr_name.startswith("Bosch "), (
            f"_attr_name '{sel._attr_name}' still contains the 'Bosch' prefix"
        )

    def test_name_is_erkennungsmodus_suffix(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect
        sel = BoschDetectionModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_name == "Erkennungsmodus"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect
        sel = BoschDetectionModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True
