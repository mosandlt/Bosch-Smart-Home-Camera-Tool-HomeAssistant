"""Tests for coordinator data-helper methods (clock_offset, motion_settings, etc.)

These read-only methods are called from sensor + switch entity properties
on every state poll. NPE-style bugs here would cascade across all
entities; the tests pin the contract that each method returns a sensible
default (None / empty dict) when the cache is empty rather than raising.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def coord():
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": "x"}}},
        _rcp_clock_offset_cache={},
        _rcp_lan_ip_cache={},
        _rcp_product_name_cache={},
        _audio_alarm_cache={},
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
        "audio_alarm_settings": BoschCameraCoordinator.audio_alarm_settings,
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
        coord._rcp_lan_ip_cache[CAM_ID] = "192.168.20.149"
        assert helpers["rcp_lan_ip"](coord, CAM_ID) == "192.168.20.149"

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


# ── audio_alarm_settings ───────────────────────────────────────────────


class TestAudioAlarmSettings:
    def test_returns_persistent_cache_when_populated(self, coord, helpers):
        """Persistent cache wins over transient `data[cam_id]['audioAlarm']`."""
        coord._audio_alarm_cache[CAM_ID] = {
            "enabled": True, "threshold": 65, "sensitivity": "MEDIUM",
        }
        coord.data[CAM_ID]["audioAlarm"] = {
            "enabled": False, "threshold": 99,  # different/stale
        }
        result = helpers["audio_alarm_settings"](coord, CAM_ID)
        # Persistent cache wins
        assert result["enabled"] is True
        assert result["threshold"] == 65

    def test_falls_back_to_transient_data(self, coord, helpers):
        """When persistent cache is empty, fall back to data[cam_id]['audioAlarm']."""
        coord.data[CAM_ID]["audioAlarm"] = {"enabled": True, "threshold": 70}
        result = helpers["audio_alarm_settings"](coord, CAM_ID)
        assert result["threshold"] == 70

    def test_returns_empty_dict_when_both_empty(self, coord, helpers):
        assert helpers["audio_alarm_settings"](coord, CAM_ID) == {}

    def test_empty_cache_dict_does_not_block_fallback(self, coord, helpers):
        """An empty {} in the persistent cache must be falsy → fall back to data."""
        coord._audio_alarm_cache[CAM_ID] = {}
        coord.data[CAM_ID]["audioAlarm"] = {"enabled": True, "threshold": 50}
        result = helpers["audio_alarm_settings"](coord, CAM_ID)
        assert result["threshold"] == 50, (
            "Empty persistent cache must not shadow fresh transient data — "
            "the `if self._audio_alarm_cache.get(cam_id):` truthiness check "
            "was meant to skip empty-dict cache entries"
        )

    def test_unknown_camera_returns_empty(self, coord, helpers):
        assert helpers["audio_alarm_settings"](coord, "unknown-cam") == {}
