"""Pin: during firmware install, camera/light/switch entities flip to
unavailable so dashboards don't show stale data and automations don't try
to control a rebooting endpoint.

Mirrors the status-sensor test in test_sensors.py — same is_updating signal,
applied to the available() properties of dependent entities.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"


def _coord(*, is_updating_value: bool, last_update_success: bool = True) -> SimpleNamespace:
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        last_update_success=last_update_success,
        is_updating=lambda cam_id: is_updating_value if cam_id == CAM_ID else False,
        # supporting caches used by some available() paths
        _firmware_cache={CAM_ID: {"updating": is_updating_value}},
        _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        _hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
        _lan_tcp_reachable={CAM_ID: (True, 0.0)},
        is_lan_reachable=lambda cam_id: True,
        is_session_stale=lambda cam_id: False,
        _user_intent_streams=set(),
    )
    return coord


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


class TestCameraAvailable:
    def test_available_when_not_updating(self, stub_entry):
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _coord(is_updating_value=False)
        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        assert cam.available is True

    def test_unavailable_when_updating(self, stub_entry):
        """Camera reboots during FW install — entity must flip unavailable
        even though coordinator.last_update_success is still True (cached
        from before the install started)."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _coord(is_updating_value=True, last_update_success=True)
        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        assert cam.available is False

    def test_unavailable_when_coordinator_failed(self, stub_entry):
        """Existing semantics preserved: no is_updating signal, but coordinator
        update failed → unavailable."""
        from custom_components.bosch_shc_camera.camera import BoschCamera
        coord = _coord(is_updating_value=False, last_update_success=False)
        cam = BoschCamera.__new__(BoschCamera)
        cam.coordinator = coord
        cam._cam_id = CAM_ID
        assert cam.available is False


class TestLightAvailable:
    def _mk_light(self, coord):
        # Concrete subclass — _BoschLightBase carries the available() override.
        from custom_components.bosch_shc_camera.light import BoschFrontLight
        light = BoschFrontLight.__new__(BoschFrontLight)
        light.coordinator = coord
        light._cam_id = CAM_ID
        return light

    def test_available_when_not_updating(self):
        light = self._mk_light(_coord(is_updating_value=False))
        assert light.available is True

    def test_unavailable_when_updating(self):
        """Light writes go via cloud or LAN RCP — both fail mid-reboot."""
        light = self._mk_light(_coord(is_updating_value=True))
        assert light.available is False


class TestLiveStreamSwitchAvailable:
    def _mk_switch(self, coord):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        sw = BoschLiveStreamSwitch.__new__(BoschLiveStreamSwitch)
        sw.coordinator = coord
        sw._cam_id = CAM_ID
        return sw

    def test_available_when_not_updating(self):
        """Live stream switch available when camera is healthy and not in FW install."""
        coord = _coord(is_updating_value=False)
        # Add the super().available chain — BoschLiveStreamSwitch inherits
        # from _BoschSwitchBase whose available checks last_update_success
        # and camera ONLINE status. Stub it as True for this test.
        sw = self._mk_switch(coord)
        # The is_updating guard runs FIRST and short-circuits before super(),
        # so for the "not updating" case we need the rest to be truthy.
        # Cleanest: patch super() via a synthetic BoschLiveStreamSwitch
        # subclass — but simpler: bypass by setting _shc_state_cache and
        # is_session_stale to consistent healthy values, then mock super.
        from unittest.mock import patch, PropertyMock
        with patch(
            "custom_components.bosch_shc_camera.switch._BoschSwitchBase.available",
            new_callable=PropertyMock,
            return_value=True,
        ):
            assert sw.available is True

    def test_unavailable_when_updating(self):
        """Live stream cannot start on a rebooting camera — is_updating guard
        short-circuits BEFORE super().available so no upstream check matters."""
        coord = _coord(is_updating_value=True)
        sw = self._mk_switch(coord)
        # No need to patch super — is_updating guard is the first check.
        assert sw.available is False


class TestPrivacySwitchAvailable:
    def _mk_switch(self, coord):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        sw = BoschPrivacyModeSwitch.__new__(BoschPrivacyModeSwitch)
        sw.coordinator = coord
        sw._cam_id = CAM_ID
        return sw

    def test_available_when_not_updating(self):
        sw = self._mk_switch(_coord(is_updating_value=False))
        assert sw.available is True

    def test_unavailable_when_updating(self):
        """Privacy mode toggle writes to camera — fails mid-reboot."""
        sw = self._mk_switch(_coord(is_updating_value=True))
        assert sw.available is False


class TestIsUpdatingHelper:
    """The coordinator helper must read _firmware_cache[cam_id]['updating']
    defensively — empty cache → False, missing key → False, True → True."""

    def test_returns_false_when_firmware_cache_empty(self):
        from custom_components.bosch_shc_camera import (
            BoschCameraCoordinator,
        )
        coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
        coord._firmware_cache = {}
        assert coord.is_updating(CAM_ID) is False

    def test_returns_false_when_updating_key_missing(self):
        from custom_components.bosch_shc_camera import (
            BoschCameraCoordinator,
        )
        coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
        coord._firmware_cache = {CAM_ID: {"status": "QUEUED"}}
        assert coord.is_updating(CAM_ID) is False

    def test_returns_true_when_updating_flag_set(self):
        from custom_components.bosch_shc_camera import (
            BoschCameraCoordinator,
        )
        coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
        coord._firmware_cache = {CAM_ID: {"updating": True}}
        assert coord.is_updating(CAM_ID) is True

    def test_returns_false_when_updating_flag_false(self):
        from custom_components.bosch_shc_camera import (
            BoschCameraCoordinator,
        )
        coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
        coord._firmware_cache = {CAM_ID: {"updating": False}}
        assert coord.is_updating(CAM_ID) is False
