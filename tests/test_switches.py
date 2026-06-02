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

CAM_ID = "11111111-1111-1111-1111-111111111111"


async def _noop_async(self) -> None:
    """Stand-in for super().async_added_to_hass() (skips the live-hass restore
    registration) so RestoreEntity restore logic can be tested in isolation."""
    return None


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
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        # Default coordinator state — every switch reads from these
        _live_connections={},
        _user_intent_streams=set(),  # v12.4.12: switch reads from this
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
        options={},
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

    def test_is_on_true_when_user_intent_set(self, stub_coord, stub_entry):
        """v12.4.12: switch reads user intent, not raw `_live_connections`."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord._user_intent_streams.add(CAM_ID)
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_false_when_only_live_connections_populated(
        self, stub_coord, stub_entry
    ):
        """v12.4.12: auto-opened sessions (Cast / dashboard) populate
        `_live_connections` but do not flip the switch."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord._live_connections[CAM_ID] = {"rtspsUrl": "rtsps://..."}
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

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
            "rtspsUrl": "rtsps://192.0.2.149/x",
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

    @pytest.mark.asyncio
    async def test_turn_off_during_cooldown_raises_not_silent(
        self, stub_coord, stub_entry
    ):
        """Regression #27: a privacy toggle inside the cooldown window raises
        ServiceValidationError (visible rejection) instead of returning
        silently — a silent drop made the card flip to the wrong state for 8s
        and look like the button had hung."""
        import time as _time
        from unittest.mock import AsyncMock

        from homeassistant.exceptions import ServiceValidationError

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord._privacy_set_at[CAM_ID] = _time.monotonic()  # just toggled
        stub_coord.async_cloud_set_privacy_mode = AsyncMock()
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        with pytest.raises(ServiceValidationError):
            await sw.async_turn_off()
        stub_coord.async_cloud_set_privacy_mode.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_message_reports_remaining_seconds(
        self, stub_coord, stub_entry
    ):
        import time as _time

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord._privacy_set_at[CAM_ID] = _time.monotonic()  # fresh toggle
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        msg = sw._cooldown_message()
        assert "wait" in msg.lower() and "s before" in msg.lower(), msg


# ── BoschAudioSwitch ─────────────────────────────────────────────────────


class TestAudioSwitch:
    def test_is_on_reads_enabled_state(self, stub_coord, stub_entry):
        """is_on reflects the _audio_enabled cache (fixture seeds CAM_ID=True)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_false_when_disabled(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord._audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_default_when_camera_unknown(self, stub_coord, stub_entry):
        """A brand-new camera defaults to OFF (muted) — no forced default-on.

        The switch is now the single source of truth (persisted via
        RestoreEntity); a fresh camera that has never been toggled starts muted
        so the stream never opens with unexpected audio.
        """
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord._audio_enabled = {}  # camera not yet registered
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False
        # __init__ seeds the default into the cache.
        assert stub_coord._audio_enabled[CAM_ID] is False

    async def test_restore_persists_off_across_restart(self, stub_coord, stub_entry):
        """Switch OFF survives a restart: RestoreEntity replays the last state.

        Regression for the 2026-06-02 report (Innenbereich) that streams always
        started with sound: the old in-memory dict + forced default-on reset the
        switch to ON on every restart. With RestoreEntity the user's OFF sticks.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord._audio_enabled = {}  # fresh boot: nothing seeded yet
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        # Seed default = OFF before restore runs.
        assert stub_coord._audio_enabled[CAM_ID] is False

        sw.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(state="off")
        )
        # Skip the real super().async_added_to_hass (needs a live hass) — focus on
        # the restore logic. _BoschSwitchBase has no override, so super() resolves
        # to RestoreEntity (mro[2]); neutralise it on the base (mro[1]).
        sw.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await sw.async_added_to_hass()
        assert stub_coord._audio_enabled[CAM_ID] is False
        assert sw.is_on is False

    async def test_restore_persists_on_across_restart(self, stub_coord, stub_entry):
        """Switch ON is likewise restored — existing users keep their sound on."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord._audio_enabled = {}
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(state="on")
        )
        sw.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await sw.async_added_to_hass()
        assert stub_coord._audio_enabled[CAM_ID] is True
        assert sw.is_on is True

    async def test_restore_no_previous_state_keeps_default_off(
        self, stub_coord, stub_entry
    ):
        """No restorable state (first ever boot) → stays at the OFF default."""
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord._audio_enabled = {}
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_get_last_state = AsyncMock(return_value=None)  # type: ignore[method-assign]
        sw.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await sw.async_added_to_hass()
        assert sw.is_on is False


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

        stub_coord._shc_state_cache[CAM_ID]["notifications_status"] = (
            "FOLLOW_CAMERA_SCHEDULE"
        )
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_for_on_camera_schedule(self, stub_coord, stub_entry):
        """ON_CAMERA_SCHEDULE → switch is ON."""
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        stub_coord._shc_state_cache[CAM_ID]["notifications_status"] = (
            "ON_CAMERA_SCHEDULE"
        )
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
