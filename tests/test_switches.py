"""Tests for switch entity classes (switch.py — 850 LOC, currently 0% covered).

Each switch entity is a stateful adapter over `coordinator._shc_state_cache`,
`coordinator._live_connections`, etc. The high-leverage tests verify:
  - `is_on` reads the right cache field
  - `available` honors privacy gates / camera-online gates correctly
  - `extra_state_attributes` exposes the documented contract

These tests use a stub coordinator + ConfigEntry — no real HA setup,
no aiohttp calls. Each switch class has predictable behavior tied to a
single dict lookup, so a tight stub covers the whole class.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def stub_coord():
    """Coordinator stub good enough for switch entity properties."""
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        # Default coordinator state — every switch reads from these
        _live_connections={},
        _shc_state_cache={
            CAM_ID: {
                "privacy_mode": False,
                "camera_light": False,
                "front_light": None,
                "wallwasher": None,
                "front_light_intensity": None,
                "notifications_status": "FOLLOW_CAMERA_SCHEDULE",
                "has_light": True,
            }
        },
        _session_stale={},
        _stream_warming=set(),
        _privacy_set_at={},
        _light_set_at={},
        _audio_enabled={CAM_ID: True},
        _privacy_sound_cache={CAM_ID: False},
        _timestamp_cache={CAM_ID: True},
        _ledlights_cache={CAM_ID: True},
        _arming_cache={},
        _rcp_privacy_cache={},
        last_update_success=True,
        options={"audio_default_on": True},
        # Helper methods
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
    )
    return coord


@pytest.fixture
def stub_entry():
    """A minimal ConfigEntry-like object — switches only read .options for some checks."""
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "x"},
        options={},
    )


# ── BoschLiveStreamSwitch ────────────────────────────────────────────────


class TestLiveStreamSwitch:
    def test_is_on_false_when_no_active_session(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_is_on_true_when_active_session(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://..."}
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_unavailable_during_privacy(self, stub_coord, stub_entry):
        """Privacy ON → live_stream must be unavailable."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_unavailable_when_session_stale(self, stub_coord, stub_entry):
        """LOCAL keepalive given up → live_stream unavailable to prevent
        showing a frozen stream as healthy."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord.is_session_stale = lambda cid: True
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_unavailable_when_camera_offline(self, stub_coord, stub_entry):
        """Camera OFFLINE → live_stream unavailable (super().available checks)."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord.is_camera_online = lambda cid: False
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_available_in_normal_state(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_extra_attrs_exposes_connection_metadata(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        stub_coord._live_connections[CAM_ID] = {
            "_connection_type": "LOCAL",
            "rtspsUrl": "rtsps://192.168.20.149/x",
            "proxyUrl": "https://proxy-37.live.cbs.boschsecurity.com/abc",
        }
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = sw.extra_state_attributes
        assert attrs["connection_type"] == "LOCAL"
        assert attrs["rtsps_url"].startswith("rtsps://")
        assert attrs["proxy_snap_url"].startswith("https://")

    def test_extra_attrs_empty_when_no_session(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = sw.extra_state_attributes
        assert attrs["connection_type"] == ""
        assert attrs["rtsps_url"] == ""


# ── BoschPrivacyModeSwitch ───────────────────────────────────────────────


class TestPrivacyModeSwitch:
    def test_is_on_reads_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = False
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_available_even_when_camera_offline(self, stub_coord, stub_entry):
        """Privacy switch is cloud-only — must stay available even with offline camera.

        Contract: privacy state lives in the cloud API response, not on the
        camera. Switching to offline-camera mode must NOT lock out the privacy
        switch (the user might want to enable privacy precisely BECAUSE the
        camera is acting up).
        """
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord.is_camera_online = lambda cid: False
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_unavailable_when_cache_empty(self, stub_coord, stub_entry):
        """If we've never seen a privacy_mode value (None), switch is unavailable."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord._shc_state_cache[CAM_ID]["privacy_mode"] = None
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_extra_attrs_exposes_rcp_state(self, stub_coord, stub_entry):
        """The RCP privacy reading is exposed for cross-validation."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord._rcp_privacy_cache[CAM_ID] = 1  # RCP says ON
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.extra_state_attributes["rcp_state"] == 1

    def test_check_cooldown_blocks_during_warmup(self, stub_coord, stub_entry):
        """Privacy toggle during stream warm-up must be blocked."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord.is_stream_warming = lambda cid: True
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw._check_cooldown() is False

    def test_check_cooldown_blocks_rapid_toggle(self, stub_coord, stub_entry):
        """A toggle within _PRIVACY_COOLDOWN seconds must be blocked."""
        import time as _time
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord._privacy_set_at[CAM_ID] = _time.monotonic()  # just toggled
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw._check_cooldown() is False

    def test_check_cooldown_allows_after_window(self, stub_coord, stub_entry):
        import time as _time
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch
        stub_coord._privacy_set_at[CAM_ID] = _time.monotonic() - 100
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw._check_cooldown() is True


# ── BoschAudioSwitch ─────────────────────────────────────────────────────


class TestAudioSwitch:
    def test_is_on_default_true(self, stub_coord, stub_entry):
        """Default audio state is ON (per coordinator init)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_false_when_disabled(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_default_when_camera_unknown(self, stub_coord, stub_entry):
        """Camera not yet in _audio_enabled defaults to True (ON)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch
        stub_coord._audio_enabled = {}  # camera not yet registered
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True


# ── BoschPrivacySoundSwitch ──────────────────────────────────────────────


class TestPrivacySoundSwitch:
    def test_is_on_reads_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch
        stub_coord._privacy_sound_cache[CAM_ID] = True
        sw = BoschPrivacySoundSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_unavailable_when_value_unknown(self, stub_coord, stub_entry):
        """None in cache → unavailable."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch
        stub_coord._privacy_sound_cache[CAM_ID] = None
        sw = BoschPrivacySoundSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False


# ── BoschTimestampSwitch ─────────────────────────────────────────────────


class TestTimestampSwitch:
    def test_is_on_reads_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch
        stub_coord._timestamp_cache[CAM_ID] = True
        sw = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_unavailable_when_unknown(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch
        stub_coord._timestamp_cache[CAM_ID] = None
        sw = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False


# ── BoschStatusLedSwitch ─────────────────────────────────────────────────


class TestStatusLedSwitch:
    def test_is_on_reads_cache(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch
        stub_coord._ledlights_cache[CAM_ID] = True
        sw = BoschStatusLedSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True


# ── BoschNotificationsSwitch ─────────────────────────────────────────────


class TestNotificationsSwitch:
    def test_is_on_for_follow_camera_schedule(self, stub_coord, stub_entry):
        """FOLLOW_CAMERA_SCHEDULE → switch is ON."""
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch
        stub_coord._shc_state_cache[CAM_ID]["notifications_status"] = "FOLLOW_CAMERA_SCHEDULE"
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_for_on_camera_schedule(self, stub_coord, stub_entry):
        """ON_CAMERA_SCHEDULE → switch is ON."""
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch
        stub_coord._shc_state_cache[CAM_ID]["notifications_status"] = "ON_CAMERA_SCHEDULE"
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_off_for_always_off(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch
        stub_coord._shc_state_cache[CAM_ID]["notifications_status"] = "ALWAYS_OFF"
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_available_even_when_camera_offline(self, stub_coord, stub_entry):
        """Notifications switch is cloud-only — like privacy."""
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch
        stub_coord.is_camera_online = lambda cid: False
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True
