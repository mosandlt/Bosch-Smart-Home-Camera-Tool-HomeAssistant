"""Tests for the cloud-degraded rehydration helper (v12.4.10).

When the first cloud refresh raises `ConfigEntryNotReady`, the integration
walks the HA registries to rediscover cam_ids + human-readable titles so
platforms still create their entities. These tests pin every branch of
that helper.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

CAM_A = "11111111-1111-1111-1111-111111111111"
CAM_B = "22222222-2222-2222-2222-222222222222"


class TestLooksLikeUuidName:
    def test_full_uuid_name_detected(self):
        from custom_components.bosch_shc_camera import _looks_like_uuid_name

        assert (
            _looks_like_uuid_name("Bosch 11111111-1111-1111-1111-111111111111") is True
        )

    def test_proper_name_not_detected(self):
        from custom_components.bosch_shc_camera import _looks_like_uuid_name

        assert _looks_like_uuid_name("Bosch Terrasse") is False

    def test_short_name_not_detected(self):
        from custom_components.bosch_shc_camera import _looks_like_uuid_name

        assert _looks_like_uuid_name("Bosch") is False

    def test_dashes_alone_not_enough(self):
        from custom_components.bosch_shc_camera import _looks_like_uuid_name

        # 4 dashes but too short
        assert _looks_like_uuid_name("a-b-c-d-e") is False


class _MockEntry:
    def __init__(self, unique_id: str):
        self.unique_id = unique_id


class _MockDevice:
    def __init__(
        self,
        *,
        name: str | None = None,
        name_by_user: str | None = None,
        id: str = "device-id",
    ):
        self.name = name
        self.name_by_user = name_by_user
        self.id = id


def _make_hass(*, entries=None, devices=None, cam_eids=None):
    """Wire up entity_registry + device_registry mock returns."""
    hass = SimpleNamespace()
    return hass


def _patch_registries(entries, devices, cam_eids=None):
    """Returns a (patch_entity_reg, patch_device_reg) tuple to apply."""
    cam_eids = cam_eids or {}

    ereg = MagicMock()
    ereg.async_get_entity_id = lambda domain, platform, unique_id: cam_eids.get(
        unique_id
    )

    dreg = MagicMock()

    def _get_device(*, identifiers):
        cid = next((i[1] for i in identifiers if i[0] == "bosch_shc_camera"), None)
        return devices.get(cid)

    dreg.async_get_device = _get_device
    dreg.async_update_device = MagicMock()

    er_patch = patch(
        "homeassistant.helpers.entity_registry.async_get", return_value=ereg
    )
    er_entries_patch = patch(
        "homeassistant.helpers.entity_registry.async_entries_for_config_entry",
        return_value=entries,
    )
    dr_patch = patch(
        "homeassistant.helpers.device_registry.async_get", return_value=dreg
    )
    return er_patch, er_entries_patch, dr_patch, ereg, dreg


class TestRehydrateCamsFromRegistry:
    def test_empty_registry_returns_empty(self):
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        er_p, er_ents_p, dr_p, _, _ = _patch_registries([], {})
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            cam_ids, cam_titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert cam_ids == set()
        assert cam_titles == {}

    def test_single_cam_with_device_name(self):
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [_MockEntry(f"bosch_shc_camera_{CAM_A}_lan_reachable")]
        devices = {CAM_A: _MockDevice(name="Bosch Terrasse")}
        er_p, er_ents_p, dr_p, _, dreg = _patch_registries(entries, devices)
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            cam_ids, cam_titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert cam_ids == {CAM_A}
        assert cam_titles[CAM_A] == "Terrasse"
        dreg.async_update_device.assert_not_called()  # No repair needed

    def test_name_by_user_wins_over_device_name(self):
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [_MockEntry(f"bosch_shc_camera_{CAM_A}_lan_reachable")]
        devices = {
            CAM_A: _MockDevice(name="Bosch Terrasse", name_by_user="My Frontporch")
        }
        er_p, er_ents_p, dr_p, _, _ = _patch_registries(entries, devices)
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            _ids, titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert titles[CAM_A] == "My Frontporch"

    def test_uuid_device_name_repaired_from_camera_eid(self):
        """The bug we hit today: device.name was 'Bosch <UUID>' from a
        prior cloud-degraded run. Helper must repair it using the camera
        entity_id slug."""
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [_MockEntry(f"bosch_shc_camera_{CAM_A}_lan_reachable")]
        broken_device = _MockDevice(name=f"Bosch {CAM_A}", id="dev-a")
        devices = {CAM_A: broken_device}
        cam_eids = {f"bosch_shc_cam_{CAM_A.lower()}": "camera.bosch_terrasse"}
        er_p, er_ents_p, dr_p, _, dreg = _patch_registries(entries, devices, cam_eids)
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            _ids, titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert titles[CAM_A] == "Terrasse"
        # Device renamed in place.
        dreg.async_update_device.assert_called_once()
        kwargs = dreg.async_update_device.call_args
        assert kwargs.kwargs.get("name") == "Bosch Terrasse" or kwargs.args[1:] != ()

    def test_no_device_no_camera_eid_leaves_title_empty(self):
        """Cam_id discovered via entity unique_id but no device + no
        camera entity → title falls through, cam_titles entry omitted."""
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [_MockEntry(f"bosch_shc_camera_{CAM_A}_lan_reachable")]
        er_p, er_ents_p, dr_p, _, _ = _patch_registries(entries, {})
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            cam_ids, titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert cam_ids == {CAM_A}
        assert CAM_A not in titles  # No fallback title set

    def test_multiple_cams(self):
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [
            _MockEntry(f"bosch_shc_camera_{CAM_A}_status"),
            _MockEntry(f"bosch_shc_camera_{CAM_B}_status"),
            _MockEntry(f"bosch_shc_camera_{CAM_A}_privacy"),  # duplicate cam — deduped
        ]
        devices = {
            CAM_A: _MockDevice(name="Bosch Terrasse"),
            CAM_B: _MockDevice(name="Bosch Innenbereich"),
        }
        er_p, er_ents_p, dr_p, _, _ = _patch_registries(entries, devices)
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            cam_ids, titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert cam_ids == {CAM_A, CAM_B}
        assert titles == {CAM_A: "Terrasse", CAM_B: "Innenbereich"}

    def test_entries_without_uuid_in_unique_id_ignored(self):
        """Other entities (e.g. integration-level diagnostic sensors) have
        unique_ids without the embedded cam UUID — must be skipped."""
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [
            _MockEntry("bosch_shc_camera_fcm_push_status"),
            _MockEntry("bosch_shc_camera_cloud_maintenance"),
            _MockEntry(f"bosch_shc_camera_{CAM_A}_status"),
        ]
        devices = {CAM_A: _MockDevice(name="Bosch Terrasse")}
        er_p, er_ents_p, dr_p, _, _ = _patch_registries(entries, devices)
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            cam_ids, _ = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert cam_ids == {CAM_A}

    def test_device_name_without_bosch_prefix_kept(self):
        """User may have renamed device to bare 'Terrasse' without prefix —
        keep the title as-is (don't strip non-existent prefix)."""
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [_MockEntry(f"bosch_shc_camera_{CAM_A}_status")]
        devices = {CAM_A: _MockDevice(name="Terrasse")}
        er_p, er_ents_p, dr_p, _, _ = _patch_registries(entries, devices)
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            _ids, titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert titles[CAM_A] == "Terrasse"

    def test_falls_back_to_camera_eid_when_device_has_no_name(self):
        from custom_components.bosch_shc_camera import _rehydrate_cams_from_registry

        entries = [_MockEntry(f"bosch_shc_camera_{CAM_A}_status")]
        devices = {CAM_A: _MockDevice(name=None)}
        cam_eids = {f"bosch_shc_cam_{CAM_A.lower()}": "camera.bosch_garten_eingang"}
        er_p, er_ents_p, dr_p, _, _ = _patch_registries(entries, devices, cam_eids)
        with er_p, er_ents_p, dr_p:
            hass = SimpleNamespace()
            _ids, titles = _rehydrate_cams_from_registry(hass, "01ENTRY")
        assert (
            titles[CAM_A] == "Garten Eingang"
        )  # .title() preserves underscores → space
