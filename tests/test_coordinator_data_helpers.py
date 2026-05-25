"""Tests for coordinator data-helper methods (clock_offset, motion_settings, etc.)

These read-only methods are called from sensor + switch entity properties
on every state poll. NPE-style bugs here would cascade across all
entities; the tests pin the contract that each method returns a sensible
default (None / empty dict) when the cache is empty rather than raising.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def coord():
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": "x"}}},
        _rcp_clock_offset_cache={},
        _rcp_lan_ip_cache={},
        _rcp_product_name_cache={},
    )


@pytest.fixture
def helpers():
    """Bind the unbound methods from BoschCameraCoordinator."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    return {
        "clock_offset": BoschCameraCoordinator.clock_offset,
        "rcp_lan_ip": BoschCameraCoordinator.rcp_lan_ip,
        "rcp_product_name": BoschCameraCoordinator.rcp_product_name,
        "motion_settings": BoschCameraCoordinator.motion_settings,
    }


# ── clock_offset ────────────────────────────────────────────────────────


class TestClockOffset:
    def test_default_none_when_empty(self, coord, helpers):
        assert helpers["clock_offset"](coord, CAM_ID) is None

    def test_returns_cached_value(self, coord, helpers):
        coord._rcp_clock_offset_cache[CAM_ID] = -1.42
        assert helpers["clock_offset"](coord, CAM_ID) == -1.42

    def test_zero_offset_returned_correctly(self, coord, helpers):
        """0.0 must NOT be confused with "not cached" — the camera is in sync."""
        coord._rcp_clock_offset_cache[CAM_ID] = 0.0
        assert helpers["clock_offset"](coord, CAM_ID) == 0.0


# ── rcp_lan_ip / rcp_product_name ──────────────────────────────────────


class TestRcpHelpers:
    def test_lan_ip_default_none(self, coord, helpers):
        assert helpers["rcp_lan_ip"](coord, CAM_ID) is None

    def test_lan_ip_returns_cached(self, coord, helpers):
        coord._rcp_lan_ip_cache[CAM_ID] = "192.0.2.149"
        assert helpers["rcp_lan_ip"](coord, CAM_ID) == "192.0.2.149"

    def test_product_name_default_none(self, coord, helpers):
        assert helpers["rcp_product_name"](coord, CAM_ID) is None

    def test_product_name_returns_cached(self, coord, helpers):
        coord._rcp_product_name_cache[CAM_ID] = "HOME_Eyes_Outdoor"
        assert helpers["rcp_product_name"](coord, CAM_ID) == "HOME_Eyes_Outdoor"


# ── motion_settings ────────────────────────────────────────────────────


class TestMotionSettings:
    def test_returns_empty_dict_when_no_motion_data(self, coord, helpers):
        assert helpers["motion_settings"](coord, CAM_ID) == {}

    def test_returns_motion_dict_from_data(self, coord, helpers):
        coord.data[CAM_ID]["motion"] = {
            "motionAlarmConfiguration": "MEDIUM_HIGH", "enabled": True,
        }
        result = helpers["motion_settings"](coord, CAM_ID)
        assert result["motionAlarmConfiguration"] == "MEDIUM_HIGH"

    def test_returns_empty_dict_for_unknown_camera(self, coord, helpers):
        assert helpers["motion_settings"](coord, "unknown-cam-id") == {}

    def test_does_not_raise_on_missing_data_key(self, coord, helpers):
        """If `data[cam_id]` exists but has no `motion` key, return {} (no NPE)."""
        coord.data[CAM_ID] = {"info": {"title": "x"}}  # no "motion" key
        assert helpers["motion_settings"](coord, CAM_ID) == {}
