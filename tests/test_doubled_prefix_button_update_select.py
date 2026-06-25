"""Regression tests for the doubled-prefix entity_id bug.

Source: Andrew75 forum post 998974/15 (2026-05-15) reported entity IDs like
  button.bosch_est_bosch_est_refresh_snapshot
instead of
  button.bosch_est_refresh_snapshot

Root cause: classes with `_attr_has_entity_name = True` AND
`_attr_name = f"Bosch {self._cam_title} <Suffix>"` caused HA to prepend the
device name automatically AND the code re-prepended "Bosch {title}" manually.

Fix (v14.2.2): remove all `_attr_name` assignments; use `_attr_translation_key`
instead so HA resolves the entity name from translations/en.json at runtime.
`_attr_name` must be None (unset) for translation_key-based naming to work.
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
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        """_attr_name must be None — translation_key provides the entity name."""
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        name = getattr(btn, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix — entity_id would be doubled"
        )

    def test_translation_key_is_refresh_snapshot(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        assert btn._attr_translation_key == "refresh_snapshot"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        assert btn._attr_has_entity_name is True


# ── BoschFirmwareUpdate ─────────────────────────────────────────────────────


class TestFirmwareUpdateNaming:
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        name = getattr(u, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_firmware_update(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_translation_key == "firmware_update"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.update import BoschFirmwareUpdate

        u = BoschFirmwareUpdate(stub_coord, CAM_ID, stub_entry)
        assert u._attr_has_entity_name is True


# ── BoschVideoQualitySelect ─────────────────────────────────────────────────


class TestVideoQualitySelectNaming:
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_video_quality(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "video_quality"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschVideoQualitySelect

        sel = BoschVideoQualitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschMotionSensitivitySelect ────────────────────────────────────────────


class TestMotionSensitivitySelectNaming:
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_motion_sensitivity(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "motion_sensitivity"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import (
            BoschMotionSensitivitySelect,
        )

        sel = BoschMotionSensitivitySelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschFcmPushModeSelect ──────────────────────────────────────────────────


class TestFcmPushModeSelectNaming:
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_fcm_push_mode(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "fcm_push_mode"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschFcmPushModeSelect

        sel = BoschFcmPushModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschStreamModeSelect ───────────────────────────────────────────────────


class TestStreamModeSelectNaming:
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_stream_mode(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "stream_mode"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschStreamModeSelect

        sel = BoschStreamModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True


# ── BoschDetectionModeSelect ────────────────────────────────────────────────


class TestDetectionModeSelectNaming:
    def test_attr_name_is_none(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        sel = BoschDetectionModeSelect(stub_coord, CAM_ID, stub_entry)
        name = getattr(sel, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix"
        )

    def test_translation_key_is_detection_mode(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        sel = BoschDetectionModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_translation_key == "detection_mode"

    def test_has_entity_name_is_true(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.select import BoschDetectionModeSelect

        sel = BoschDetectionModeSelect(stub_coord, CAM_ID, stub_entry)
        assert sel._attr_has_entity_name is True
