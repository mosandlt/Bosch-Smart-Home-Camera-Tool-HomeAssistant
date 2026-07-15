"""Tests for custom_components/bosch_shc_camera/switch.py.

Covers all switch-platform entities: motion detection, recording,
autofollow, intercom/audio, privacy sound override, status LED,
motion/ambient/soft-fade lighting, intrusion detection, notification-type
toggles, alarm arm/mode/pre-alarm, image rotation, NVR recording, live-stream
switch, Gen2 Audio-Plus sound detection (glass-break/fire-alarm), the Gen2
panic-alarm (siren) switch, RTSP credential redaction, write-failure user
notification, and async_setup_entry entity-creation gating.

Each switch entity is a stateful adapter over coordinator caches
(`shc_state_cache`, `live_connections`, per-feature caches). Tests use a
stub coordinator + a ConfigEntry-like SimpleNamespace -- no real HA setup,
no aiohttp calls except where a switch write path talks to the cloud
directly (mocked via async_get_bosch_cloud_session / async_put_camera).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import aiohttp
import pytest

from custom_components.bosch_shc_camera import switch as switch_mod
from custom_components.bosch_shc_camera.session_state import (
    CameraSessionState,
    get_or_create_session,
)
from custom_components.bosch_shc_camera.switch import (
    BoschAmbientLightSwitch,
    BoschCameraLightSwitch,
    BoschIntercomSwitch,
    BoschIntrusionDetectionSwitch,
    BoschLiveStreamSwitch,
    BoschNotificationsSwitch,
    BoschPrivacyModeSwitch,
    BoschSoftLightFadingSwitch,
    BoschWallwasherSwitch,
    _BoschSwitchBase,
    _redact_rtsp_creds,
    async_setup_entry,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_ID2 = "22222222-0000-0000-0000-000000000002"
CAM_ID_GEN2 = "22222222-2222-2222-2222-222222222222"  # Innenkamera II (Gen2)
CAM_ID_INTERCOM = "DEAD-BEEF-INTERCOM"
MODULE = "custom_components.bosch_shc_camera.switch"


async def _noop_async(self) -> None:
    """Stand-in for super().async_added_to_hass() (skips the live-hass restore
    registration) so RestoreEntity restore logic can be tested in isolation."""
    return None


@pytest.fixture
def stub_coord() -> SimpleNamespace:
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
        live_connections={},
        user_intent_streams=set(),  # switch reads from this, not raw live_connections
        shc_state_cache={
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
        session_stale={},
        stream_warming=set(),
        privacy_set_at={},
        light_set_at={},
        audio_enabled={CAM_ID: True},
        privacy_sound_cache={CAM_ID: False},
        timestamp_cache={CAM_ID: True},
        ledlights_cache={CAM_ID: True},
        arming_cache={},
        rcp_privacy_cache={},
        last_update_success=True,
        options={},
        # Helper methods
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
    )
    return coord


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    """A minimal ConfigEntry-like object — switches only read .options for some checks."""
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "x"},
        options={},
    )


@pytest.fixture
def stub_coord_turnonoff() -> SimpleNamespace:
    """Coordinator stub for the turn_on/turn_off action coverage — adds the
    cloud-setter / async_put_camera AsyncMocks and a few extra data fields
    (autofollow, recordingOptions) that the base `stub_coord` above doesn't
    need for its state/availability-only tests."""
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
                "autofollow": {"result": False},
                "recordingOptions": {"recordSound": False},
            }
        },
        live_connections={},
        shc_state_cache={
            CAM_ID: {
                "privacy_mode": False,
                "camera_light": False,
                "front_light": None,
                "wallwasher": None,
                "notifications_status": "FOLLOW_CAMERA_SCHEDULE",
                "has_light": True,
            }
        },
        session_stale={},
        stream_warming=set(),
        privacy_set_at={},
        light_set_at={},
        audio_enabled={CAM_ID: True},
        privacy_sound_cache={CAM_ID: False},
        timestamp_cache={CAM_ID: True},
        ledlights_cache={CAM_ID: True},
        arming_cache={},
        rcp_privacy_cache={},
        last_update_success=True,
        options={},
        token="token-AAA",
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
        # Coordinator side-effect mocks
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_notifications=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        tear_down_live_stream=AsyncMock(),
        motion_settings=lambda cid: {
            "enabled": False,
            "motionAlarmConfiguration": "MEDIUM",
        },
        recording_options=lambda cid: {"recordSound": False},
    )
    return coord


@pytest.fixture(autouse=True)
def _patch_async_create_task():
    """Many switch turn_on/off methods call self.hass.async_create_task —
    swallow it everywhere with a MagicMock to avoid event-loop ceremony."""
    yield


def _bind_hass_turnonoff(switch):
    """Switches sometimes call self.hass.async_create_task in turn_on —
    attach a sync MagicMock so the call is observable but no-op."""
    switch.hass = SimpleNamespace(
        async_create_task=MagicMock(),
    )
    switch.async_write_ha_state = MagicMock()


def _stub_coord(**overrides):
    """Factory for the mode-pin coverage coordinator stub — a superset of
    caches/mocks covering notification-type, intercom, motion-light, ambient
    light, soft-light-fading, pre-alarm and panic-alarm switches, plus the
    base fields shared with the other stubs above."""
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0, "sound": True},
                    "featureStatus": {},
                },
                "status": "ONLINE",
                "autofollow": {"result": False},
                "recordingOptions": {"recordSound": False},
            }
        },
        live_connections={},
        user_intent_streams=set(),
        shc_state_cache={
            CAM_ID: {
                "privacy_mode": False,
                "camera_light": True,
                "front_light": False,
                "wallwasher": False,
            }
        },
        session_stale={},
        stream_warming=set(),
        privacy_set_at={},
        light_set_at={},
        audio_enabled={CAM_ID: True},
        audio_cache={},
        privacy_sound_cache={CAM_ID: False},
        privacy_sound_set_at={},
        timestamp_cache={CAM_ID: True},
        timestamp_set_at={},
        ledlights_cache={CAM_ID: True},
        ledlights_set_at={},
        motion_light_cache={},
        ambient_lighting_cache={},
        global_lighting_cache={},
        intrusion_config_cache={},
        intrusion_config_set_at={},
        motion_set_at={},
        alarm_settings_set_at={},
        notifications_cache={},
        arming_cache={},
        arming_set_at={},
        alarm_status_cache={},
        alarm_settings_cache={},
        image_rotation_180={},
        nvr_user_intent={},
        nvr_processes={},
        nvr_preroll_processes={},
        nvr_preroll_tasks={},
        nvr_error_state={},
        bg_tasks=set(),
        rcp_privacy_cache={},
        hw_version={},
        last_update_success=True,
        options={},
        token="tok-A",
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
        motion_settings=lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        },
        recording_options=lambda cid: {"recordSound": False},
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_notifications=AsyncMock(),
        tear_down_live_stream=AsyncMock(),
        try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        record_stream_error=MagicMock(),
        record_stream_success=MagicMock(),
        stop_tls_proxy=AsyncMock(),
        stop_viewing_front_door=AsyncMock(),
        stop_remote_viewing_front_door=AsyncMock(),
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def coord() -> SimpleNamespace:
    return _stub_coord()


@pytest.fixture
def entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "x"},
        options={"enable_snapshot_button": True, "enable_nvr": False},
    )


def _bind_hass_modepins(sw):
    sw.hass = SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    sw.async_write_ha_state = MagicMock()


class TestLiveStreamSwitch:
    def test_is_on_false_when_no_active_session(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_is_on_true_when_user_intent_set(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Switch reads user intent (`user_intent_streams`), not raw `live_connections`."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord.user_intent_streams.add(CAM_ID)
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_false_when_only_live_connections_populated(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Auto-opened sessions (Cast / dashboard) populate `live_connections`
        but do not flip the switch."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://..."}
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_unavailable_during_privacy(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Privacy ON → live_stream must be unavailable."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord.shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_unavailable_when_session_stale(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """LOCAL keepalive given up → live_stream unavailable to prevent
        showing a frozen stream as healthy."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord.is_session_stale = lambda cid: True
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_unavailable_when_camera_offline(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Camera OFFLINE → live_stream unavailable (super().available checks)."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord.is_camera_online = lambda cid: False
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_available_in_normal_state(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True

    def test_extra_attrs_exposes_connection_metadata(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord.live_connections[CAM_ID] = {
            "_connection_type": "LOCAL",
            "rtspsUrl": "rtsps://192.0.2.149/x",
            "proxyUrl": "https://proxy-37.live.cbs.boschsecurity.com/abc",
        }
        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = sw.extra_state_attributes
        assert attrs["connection_type"] == "LOCAL"
        assert attrs["rtsps_url"].startswith("rtsps://")
        assert attrs["proxy_snap_url"].startswith("https://")

    def test_extra_attrs_empty_when_no_session(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        sw = BoschLiveStreamSwitch(stub_coord, CAM_ID, stub_entry)
        attrs = sw.extra_state_attributes
        assert attrs["connection_type"] == ""
        assert attrs["rtsps_url"] == ""


class TestPrivacyModeSwitch:
    def test_is_on_reads_cache(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_off(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.shc_state_cache[CAM_ID]["privacy_mode"] = False
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_available_even_when_camera_offline(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
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

    def test_unavailable_when_cache_empty(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """If we've never seen a privacy_mode value (None), switch is unavailable."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.shc_state_cache[CAM_ID]["privacy_mode"] = None
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def test_extra_attrs_exposes_rcp_state(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """The RCP privacy reading is exposed for cross-validation."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.rcp_privacy_cache[CAM_ID] = 1  # RCP says ON
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.extra_state_attributes["rcp_state"] == 1

    def test_check_cooldown_blocks_during_warmup(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Privacy toggle during stream warm-up must be blocked."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.is_stream_warming = lambda cid: True
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw._check_cooldown() is False

    def test_check_cooldown_blocks_rapid_toggle(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A toggle within _PRIVACY_COOLDOWN seconds must be blocked."""
        import time as _time

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.privacy_set_at[CAM_ID] = _time.monotonic()  # just toggled
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw._check_cooldown() is False

    def test_check_cooldown_allows_after_window(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        import time as _time

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.privacy_set_at[CAM_ID] = _time.monotonic() - 100
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw._check_cooldown() is True

    def test_check_cooldown_first_toggle_not_blocked_low_monotonic(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Regression (SENTINEL_RULE): on a freshly booted host time.monotonic()
        can be < _PRIVACY_COOLDOWN. With no privacy_set_at entry yet (first
        toggle ever), a 0 default made `monotonic() - 0` look like a
        just-happened toggle and falsely blocked the very first privacy
        change. The default must be float('-inf').
        """
        from unittest.mock import patch

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.privacy_set_at.pop(CAM_ID, None)  # never toggled
        stub_coord.is_stream_warming = lambda cid: False
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        with patch(
            "custom_components.bosch_shc_camera.switch.time.monotonic",
            return_value=2.0,  # < _PRIVACY_COOLDOWN (5s), e.g. a just-booted VM
        ):
            assert sw._check_cooldown() is True

    @pytest.mark.asyncio
    async def test_turn_off_during_cooldown_defers_not_raises(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A privacy toggle inside the cooldown window must be DEFERRED +
        coalesced, never raised — a raised ServiceValidationError aborted an
        automation's remaining steps (live incident). The write is not
        applied immediately; the intent is recorded, is_on reflects it (so the
        card's optimistic flip stays correct, #27), and a deferred task is
        armed to apply it once the cooldown clears."""
        import time as _time
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.privacy_set_at[CAM_ID] = _time.monotonic()  # just toggled
        stub_coord.async_cloud_set_privacy_mode = AsyncMock()
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        sw.hass = MagicMock()
        sw.async_write_ha_state = MagicMock()

        # Must NOT raise.
        await sw.async_turn_off()

        stub_coord.async_cloud_set_privacy_mode.assert_not_called()
        assert sw._pending_privacy is False
        assert sw.is_on is False  # pending intent, no snap-back
        sw.hass.async_create_task.assert_called_once()
        # The deferred loop coroutine was passed to the mocked async_create_task
        # but never awaited — close it to avoid a "never awaited" warning.
        sw.hass.async_create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_deferred_privacy_coalesces_to_latest(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Toggle ON then OFF within the cooldown → coalesced to the LATEST
        intent (OFF), one deferred task armed, and the flush applies only OFF."""
        import time as _time
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.privacy_set_at[CAM_ID] = _time.monotonic()  # cooldown active
        stub_coord.async_cloud_set_privacy_mode = AsyncMock()
        stub_coord.tear_down_live_stream = AsyncMock()
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        sw.hass = MagicMock()
        # The deferred task stays "not done" so the 2nd toggle reuses it
        # (coalesce) instead of arming a second one.
        running_task = MagicMock()
        running_task.done.return_value = False
        sw.hass.async_create_task.return_value = running_task
        sw.async_write_ha_state = MagicMock()

        await sw.async_turn_on()  # pending = True
        await sw.async_turn_off()  # coalesced → pending = False

        assert sw._pending_privacy is False
        assert sw.hass.async_create_task.call_count == 1  # single armed task
        for c in sw.hass.async_create_task.call_args_list:
            c.args[0].close()

        # Flushing the pending state applies only the latest intent (OFF).
        await sw._flush_pending_privacy()
        stub_coord.async_cloud_set_privacy_mode.assert_awaited_once_with(CAM_ID, False)
        stub_coord.tear_down_live_stream.assert_not_called()  # OFF: no teardown
        assert sw._pending_privacy is None

    @pytest.mark.asyncio
    async def test_cooldown_message_reports_remaining_seconds(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        import time as _time

        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        stub_coord.privacy_set_at[CAM_ID] = _time.monotonic()  # fresh toggle
        sw = BoschPrivacyModeSwitch(stub_coord, CAM_ID, stub_entry)
        msg = sw._cooldown_message()
        assert "wait" in msg.lower() and "s before" in msg.lower(), msg


class TestAudioSwitch:
    def test_is_on_reads_enabled_state(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """is_on reflects the audio_enabled cache (fixture seeds CAM_ID=True)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_false_when_disabled(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord.audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_default_when_camera_unknown(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A brand-new camera defaults to OFF (muted) — no forced default-on.

        The switch is the single source of truth (persisted via
        RestoreEntity); a fresh camera that has never been toggled starts muted
        so the stream never opens with unexpected audio.
        """
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord.audio_enabled = {}  # camera not yet registered
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False
        # __init__ seeds the default into the cache.
        assert stub_coord.audio_enabled[CAM_ID] is False

    async def test_restore_persists_off_across_restart(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Switch OFF survives a restart: RestoreEntity replays the last state.

        Regression: streams always started with sound because the old
        in-memory dict + forced default-on reset the switch to ON on every
        restart. With RestoreEntity the user's OFF sticks.
        """
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord.audio_enabled = {}  # fresh boot: nothing seeded yet
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        # Seed default = OFF before restore runs.
        assert stub_coord.audio_enabled[CAM_ID] is False

        sw.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(state="off")
        )
        # Skip the real super().async_added_to_hass (needs a live hass) — focus on
        # the restore logic. _BoschSwitchBase has no override, so super() resolves
        # to RestoreEntity (mro[2]); neutralise it on the base (mro[1]).
        sw.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await sw.async_added_to_hass()
        assert stub_coord.audio_enabled[CAM_ID] is False
        assert sw.is_on is False

    async def test_restore_persists_on_across_restart(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Switch ON is likewise restored — existing users keep their sound on."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord.audio_enabled = {}
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(state="on")
        )
        sw.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await sw.async_added_to_hass()
        assert stub_coord.audio_enabled[CAM_ID] is True
        assert sw.is_on is True

    async def test_restore_no_previous_state_keeps_default_off(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """No restorable state (first ever boot) → stays at the OFF default."""
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord.audio_enabled = {}
        sw = BoschAudioSwitch(stub_coord, CAM_ID, stub_entry)
        sw.async_get_last_state = AsyncMock(return_value=None)  # type: ignore[method-assign]
        sw.__class__.__mro__[1].async_added_to_hass = _noop_async  # type: ignore[method-assign]
        await sw.async_added_to_hass()
        assert sw.is_on is False


class TestPrivacySoundSwitch:
    def test_is_on_reads_cache(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

        stub_coord.privacy_sound_cache[CAM_ID] = True
        sw = BoschPrivacySoundSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_unavailable_when_value_unknown(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """None in cache → unavailable."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

        stub_coord.privacy_sound_cache[CAM_ID] = None
        sw = BoschPrivacySoundSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False


class TestTimestampSwitch:
    def test_is_on_reads_cache(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        stub_coord.timestamp_cache[CAM_ID] = True
        sw = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_unavailable_when_unknown(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        stub_coord.timestamp_cache[CAM_ID] = None
        sw = BoschTimestampSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is False

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        return BoschTimestampSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_timestamp"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.timestamp_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.timestamp_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_missing(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.timestamp_cache = {}
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_result_true_and_caches(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.timestamp_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "timestamp", {"result": True}
        )
        assert coord.timestamp_cache[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_result_false_and_caches(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.timestamp_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "timestamp", {"result": False}
        )
        assert coord.timestamp_cache[CAM_ID] is False


class TestStatusLedSwitch:
    def test_is_on_reads_cache(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord.ledlights_cache[CAM_ID] = True
        sw = BoschStatusLedSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True


class TestStatusLedSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        return BoschStatusLedSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_ledlights"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.ledlights_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.ledlights_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_state_ON_and_caches_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.ledlights_cache[CAM_ID] = False
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "ledlights", {"state": "ON"}
        )
        assert coord.ledlights_cache[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_state_OFF_and_caches_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.ledlights_cache[CAM_ID] = True
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "ledlights", {"state": "OFF"}
        )
        assert coord.ledlights_cache[CAM_ID] is False


class TestNotificationsSwitch:
    def test_is_on_for_follow_camera_schedule(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """FOLLOW_CAMERA_SCHEDULE → switch is ON."""
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        stub_coord.shc_state_cache[CAM_ID]["notifications_status"] = (
            "FOLLOW_CAMERA_SCHEDULE"
        )
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_on_for_on_camera_schedule(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """ON_CAMERA_SCHEDULE → switch is ON."""
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        stub_coord.shc_state_cache[CAM_ID]["notifications_status"] = (
            "ON_CAMERA_SCHEDULE"
        )
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is True

    def test_is_off_for_always_off(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        stub_coord.shc_state_cache[CAM_ID]["notifications_status"] = "ALWAYS_OFF"
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.is_on is False

    def test_available_even_when_camera_offline(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Notifications switch is cloud-only — like privacy."""
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        stub_coord.is_camera_online = lambda cid: False
        sw = BoschNotificationsSwitch(stub_coord, CAM_ID, stub_entry)
        assert sw.available is True


class TestCameraLightSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_calls_cloud_setter_with_true(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        sw = BoschCameraLightSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        stub_coord_turnonoff.async_cloud_set_camera_light.assert_awaited_once_with(
            CAM_ID, True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_setter_with_false(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        sw = BoschCameraLightSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        stub_coord_turnonoff.async_cloud_set_camera_light.assert_awaited_once_with(
            CAM_ID, False
        )


class TestLightComponentSwitches:
    @pytest.mark.asyncio
    async def test_front_light_on_uses_front_component(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        sw = BoschFrontLightSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        stub_coord_turnonoff.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID,
            "front",
            True,
        )

    @pytest.mark.asyncio
    async def test_front_light_off(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        sw = BoschFrontLightSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        stub_coord_turnonoff.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID,
            "front",
            False,
        )

    @pytest.mark.asyncio
    async def test_wallwasher_uses_wallwasher_component(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Pin the literal 'wallwasher' string — the cloud handler
        switches on this exact key."""
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch

        sw = BoschWallwasherSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        stub_coord_turnonoff.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID,
            "wallwasher",
            True,
        )


class TestPrivacyModeSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_during_warmup_defers(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """During stream warm-up a privacy toggle is DEFERRED (not applied,
        not raised) — the TLS proxy + encoder init isn't a moment to flip the
        shutter, but the intent is queued and applied once warm-up clears."""
        stub_coord_turnonoff.is_stream_warming = lambda cid: True
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        sw = BoschPrivacyModeSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()  # must NOT raise
        # The cloud setter must NOT be called immediately while blocked.
        stub_coord_turnonoff.async_cloud_set_privacy_mode.assert_not_awaited()
        stub_coord_turnonoff.tear_down_live_stream.assert_not_awaited()
        assert sw._pending_privacy is True
        sw.hass.async_create_task.assert_called_once()
        sw.hass.async_create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_turn_on_within_cooldown_defers(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A second flip within the cooldown window is DEFERRED (not applied
        immediately, not raised) — protects the camera firmware from rapid
        shutter toggling while keeping automations running. Uses a just-toggled
        timestamp so the assertion is independent of the exact _PRIVACY_COOLDOWN
        value."""
        stub_coord_turnonoff.privacy_set_at[CAM_ID] = time.monotonic()  # just toggled
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        sw = BoschPrivacyModeSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()  # must NOT raise
        stub_coord_turnonoff.async_cloud_set_privacy_mode.assert_not_awaited()
        assert sw._pending_privacy is True
        sw.hass.async_create_task.assert_called_once()
        sw.hass.async_create_task.call_args.args[0].close()

    @pytest.mark.asyncio
    async def test_turn_on_tears_down_active_stream(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """If a live stream is active when privacy turns on, must
        teardown — otherwise stream_worker auto-restart loops against
        a dead camera (issue #6)."""
        stub_coord_turnonoff.live_connections[CAM_ID] = {"rtspsUrl": "rtsps://x"}
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        sw = BoschPrivacyModeSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        stub_coord_turnonoff.tear_down_live_stream.assert_awaited_once_with(CAM_ID)
        stub_coord_turnonoff.async_cloud_set_privacy_mode.assert_awaited_once_with(
            CAM_ID, True
        )

    @pytest.mark.asyncio
    async def test_turn_on_no_active_stream_skips_teardown(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        sw = BoschPrivacyModeSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        stub_coord_turnonoff.tear_down_live_stream.assert_not_awaited()
        stub_coord_turnonoff.async_cloud_set_privacy_mode.assert_awaited_once_with(
            CAM_ID, True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_setter_false(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        sw = BoschPrivacyModeSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        stub_coord_turnonoff.async_cloud_set_privacy_mode.assert_awaited_once_with(
            CAM_ID, False
        )


class TestNotificationsSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        sw = BoschNotificationsSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        stub_coord_turnonoff.async_cloud_set_notifications.assert_awaited_once_with(
            CAM_ID, True
        )

    @pytest.mark.asyncio
    async def test_turn_off(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNotificationsSwitch

        sw = BoschNotificationsSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        stub_coord_turnonoff.async_cloud_set_notifications.assert_awaited_once_with(
            CAM_ID, False
        )


class TestMotionEnabledSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_preserves_sensitivity(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Motion ON via PUT /motion must preserve the existing sensitivity
        — sending only `enabled` resets the level to API default."""
        stub_coord_turnonoff.motion_settings = lambda cid: {
            "enabled": False,
            "motionAlarmConfiguration": "SUPER_HIGH",
        }
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

        sw = BoschMotionEnabledSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        stub_coord_turnonoff.async_put_camera.assert_awaited_once()
        args = stub_coord_turnonoff.async_put_camera.call_args.args
        assert args[0] == CAM_ID
        assert args[1] == "motion"
        assert args[2] == {"enabled": True, "motionAlarmConfiguration": "SUPER_HIGH"}

    @pytest.mark.asyncio
    async def test_turn_off_preserves_sensitivity(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord_turnonoff.motion_settings = lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "MEDIUM_LOW",
        }
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

        sw = BoschMotionEnabledSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        args = stub_coord_turnonoff.async_put_camera.call_args.args
        assert args[2] == {"enabled": False, "motionAlarmConfiguration": "MEDIUM_LOW"}

    @pytest.mark.asyncio
    async def test_turn_on_default_sensitivity_when_unset(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """If no settings exist yet (first boot), default sensitivity is HIGH."""
        stub_coord_turnonoff.motion_settings = lambda cid: {}
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

        sw = BoschMotionEnabledSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        args = stub_coord_turnonoff.async_put_camera.call_args.args
        assert args[2] == {"enabled": True, "motionAlarmConfiguration": "HIGH"}


class TestRecordSoundSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sends_record_sound_true(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

        sw = BoschRecordSoundSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        args = stub_coord_turnonoff.async_put_camera.call_args.args
        assert args[0] == CAM_ID
        assert args[1] == "recording_options"
        assert args[2] == {"recordSound": True}

    @pytest.mark.asyncio
    async def test_turn_off(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

        sw = BoschRecordSoundSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        args = stub_coord_turnonoff.async_put_camera.call_args.args
        assert args[2] == {"recordSound": False}


class TestAutoFollowSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sends_result_true(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Auto-follow API uses the unusual {"result": bool} payload —
        not {"enabled": bool}. Pin so the body schema doesn't drift."""
        from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch

        sw = BoschAutoFollowSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        args = stub_coord_turnonoff.async_put_camera.call_args.args
        assert args[1] == "autofollow"
        assert args[2] == {"result": True}, (
            "Auto-follow API expects {'result': bool}, NOT {'enabled': bool}. "
            "Drift to 'enabled' produces a silent 200 + no-op on Bosch's side."
        )

    @pytest.mark.asyncio
    async def test_turn_off(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch

        sw = BoschAutoFollowSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        args = stub_coord_turnonoff.async_put_camera.call_args.args
        assert args[2] == {"result": False}


class TestAudioSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sets_in_memory_flag(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Audio switch is purely client-side — toggles `audio_enabled[cam_id]`.
        Stream-side audio is controlled via the URL `enableaudio=` param at
        the next stream_source() call."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_turnonoff.audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        assert stub_coord_turnonoff.audio_enabled[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_in_memory_flag(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_turnonoff.audio_enabled[CAM_ID] = True
        sw = BoschAudioSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        assert stub_coord_turnonoff.audio_enabled[CAM_ID] is False

    @pytest.mark.asyncio
    async def test_turn_on_writes_ha_state_immediately(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Regression #22: async_turn_on MUST push the new state to HA right
        away. is_on reads the audio_enabled dict (not coordinator data), so
        without async_write_ha_state the toggle stayed visually stale until the
        next coordinator refresh — which a camera pan happened to trigger,
        i.e. "can't re-enable audio until I move the camera"."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_turnonoff.audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_on()
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_turn_off_writes_ha_state_immediately(
        self, stub_coord_turnonoff: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Regression #22 — see test_turn_on_writes_ha_state_immediately."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_turnonoff.audio_enabled[CAM_ID] = True
        sw = BoschAudioSwitch(stub_coord_turnonoff, CAM_ID, stub_entry)
        _bind_hass_turnonoff(sw)
        await sw.async_turn_off()
        sw.async_write_ha_state.assert_called()


class TestNotificationTypeSwitchAudio:
    """ntype='audio'."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "audio")

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_audio"

    def test_is_on_true_when_cache_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"audio": True, "movement": False}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_when_cache_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"audio": False, "movement": True}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_true_and_caches(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"audio": False, "movement": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"audio": True, "movement": True}
        )
        assert coord.notifications_cache[CAM_ID]["audio"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_false_and_caches(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"audio": True, "movement": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"audio": False, "movement": True}
        )
        assert coord.notifications_cache[CAM_ID]["audio"] is False


class TestNotificationTypeSwitchTrouble:
    """ntype='trouble'."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "trouble")

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_trouble"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.notifications_cache[CAM_ID] = {"trouble": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.notifications_cache[CAM_ID] = {"trouble": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_correct_payload(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"trouble": False, "person": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"trouble": True, "person": True}
        )
        assert coord.notifications_cache[CAM_ID]["trouble"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_correct_payload(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"trouble": True, "person": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"trouble": False, "person": True}
        )
        assert coord.notifications_cache[CAM_ID]["trouble"] is False


class TestNotificationTypeSwitchCameraAlarm:
    """ntype='cameraAlarm'."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "cameraAlarm")

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_cameraAlarm"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.notifications_cache[CAM_ID] = {"cameraAlarm": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.notifications_cache[CAM_ID] = {"cameraAlarm": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"cameraAlarm": False}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"cameraAlarm": True}
        )
        assert coord.notifications_cache[CAM_ID]["cameraAlarm"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"cameraAlarm": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"cameraAlarm": False}
        )
        assert coord.notifications_cache[CAM_ID]["cameraAlarm"] is False


class TestNotificationTypeSwitchTroubleEmail:
    """ntype='troubleEmail'."""

    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        return BoschNotificationTypeSwitch(coord, CAM_ID, entry, "troubleEmail")

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_notif_troubleEmail"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.notifications_cache[CAM_ID] = {"troubleEmail": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.notifications_cache[CAM_ID] = {"troubleEmail": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_puts_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"troubleEmail": False}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"troubleEmail": True}
        )
        assert coord.notifications_cache[CAM_ID]["troubleEmail"] is True

    @pytest.mark.asyncio
    async def test_turn_off_puts_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.notifications_cache[CAM_ID] = {"troubleEmail": True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "notifications", {"troubleEmail": False}
        )
        assert coord.notifications_cache[CAM_ID]["troubleEmail"] is False


class TestIntercomSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        return BoschIntercomSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_intercom"

    def test_is_on_defaults_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_sends_audioEnabled_true_speakerlevel(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        """Regression: the body key is the API's "speakerLevel" (lowercase s)
        — confirmed via a real capture
        {"audioEnabled":true,"microphoneLevel":60,"speakerLevel":80}. A
        prior version sent "SpeakerLevel" (capital S), which the API
        silently ignored, so speaker level 50 never actually applied."""
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "audio", {"audioEnabled": True, "speakerLevel": 50}
        )
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sends_audioEnabled_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        sw._is_on = True
        _bind_hass_modepins(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "audio", {"audioEnabled": False}
        )
        assert sw.is_on is False


class TestAutoFollowSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch

        return BoschAutoFollowSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_autofollow"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.data[CAM_ID]["autofollow"] = {"result": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.data[CAM_ID]["autofollow"] = {"result": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_data(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.data[CAM_ID]["autofollow"] = None
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_result_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "autofollow", {"result": True}
        )

    @pytest.mark.asyncio
    async def test_turn_off_puts_result_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "autofollow", {"result": False}
        )


class TestRecordSoundSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

        return BoschRecordSoundSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_record_sound"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.recording_options = lambda cid: {"recordSound": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.recording_options = lambda cid: {"recordSound": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_opts(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.recording_options = lambda cid: None
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_recordSound_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "recording_options", {"recordSound": True}
        )

    @pytest.mark.asyncio
    async def test_turn_off_puts_recordSound_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "recording_options", {"recordSound": False}
        )


class TestMotionLightSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        return BoschMotionLightSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_motion_light"

    def test_is_on_true_from_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.motion_light_cache[CAM_ID] = {
            "lightOnMotionEnabled": True,
            "sensitivity": "HIGH",
        }
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_from_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_sets_lightOnMotionEnabled_true_in_put(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": False, "delay": 30}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass_modepins(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once()
        # positional args: (cam_id, endpoint, body)
        put_body = coord.async_put_camera.await_args_list[0][0][2]
        assert put_body["lightOnMotionEnabled"] is True
        assert sw._is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_lightOnMotionEnabled_false_in_put(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": True, "delay": 30}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass_modepins(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once()
        put_body = coord.async_put_camera.await_args_list[0][0][2]
        assert put_body["lightOnMotionEnabled"] is False
        assert sw._is_on is False


class TestAmbientLightSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        return BoschAmbientLightSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_ambient_light"

    def test_is_on_true_from_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_from_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.ambient_lighting_cache[CAM_ID] = {"ambientLightEnabled": False}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_ambient_with_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_on()
        sw._set_ambient_light.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_ambient_with_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_off()
        sw._set_ambient_light.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_set_ambient_light_on_sends_ambientLightEnabled_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        """Full HTTP path: PUT body must include ambientLightEnabled=True."""
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        get_resp = MagicMock()
        get_resp.status = 200
        get_resp.json = AsyncMock(
            return_value={"ambientLightEnabled": False, "schedule": "DUSK"}
        )
        put_resp = MagicMock()
        put_resp.status = 204
        get_ctx = MagicMock()
        get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=None)
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=get_ctx)
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._set_ambient_light(True)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["ambientLightEnabled"] is True
        assert sw._is_on is True

    @pytest.mark.asyncio
    async def test_set_ambient_light_off_sends_ambientLightEnabled_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        """Full HTTP path: PUT body must include ambientLightEnabled=False."""
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        get_resp = MagicMock()
        get_resp.status = 200
        get_resp.json = AsyncMock(
            return_value={"ambientLightEnabled": True, "schedule": "DUSK"}
        )
        put_resp = MagicMock()
        put_resp.status = 200
        get_ctx = MagicMock()
        get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=None)
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=get_ctx)
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._set_ambient_light(False)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["ambientLightEnabled"] is False
        assert sw._is_on is False


class TestSoftLightFadingSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        return BoschSoftLightFadingSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_soft_light_fading"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.global_lighting_cache[CAM_ID] = {
            "softLightFading": True,
            "darknessThreshold": 0.5,
        }
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.global_lighting_cache[CAM_ID] = {
            "softLightFading": False,
            "darknessThreshold": 0.5,
        }
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_delegates_put_global_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_on()
        sw._put_global_lighting.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_delegates_put_global_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_off()
        sw._put_global_lighting.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_put_global_on_sends_softLightFading_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.global_lighting_cache[CAM_ID] = {
            "softLightFading": False,
            "darknessThreshold": 0.4,
        }
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.json = AsyncMock(
            return_value={"softLightFading": True, "darknessThreshold": 0.4}
        )
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._put_global_lighting(True)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["softLightFading"] is True
        assert coord.global_lighting_cache[CAM_ID]["softLightFading"] is True

    @pytest.mark.asyncio
    async def test_put_global_off_sends_softLightFading_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.global_lighting_cache[CAM_ID] = {
            "softLightFading": True,
            "darknessThreshold": 0.6,
        }
        sw = self._make(coord, entry)
        _bind_hass_modepins(sw)
        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.json = AsyncMock(
            return_value={"softLightFading": False, "darknessThreshold": 0.6}
        )
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._put_global_lighting(False)
        _, put_kwargs = mock_session.put.call_args
        assert put_kwargs["json"]["softLightFading"] is False
        assert coord.global_lighting_cache[CAM_ID]["softLightFading"] is False


class TestPreAlarmSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschPreAlarmSwitch

        return BoschPreAlarmSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_prealarm"

    def test_is_on_true_when_setting_ON(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.alarm_settings_cache[CAM_ID] = {"preAlarmMode": "ON", "alarmMode": "OFF"}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false_when_setting_OFF(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.alarm_settings_cache[CAM_ID] = {"preAlarmMode": "OFF", "alarmMode": "ON"}
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_none_when_no_cache(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        assert sw.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_puts_preAlarmMode_ON(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.alarm_settings_cache[CAM_ID] = {"preAlarmMode": "OFF", "alarmMode": "ON"}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "alarm_settings", {"preAlarmMode": "ON", "alarmMode": "ON"}
        )
        assert coord.alarm_settings_cache[CAM_ID]["preAlarmMode"] == "ON"

    @pytest.mark.asyncio
    async def test_turn_off_puts_preAlarmMode_OFF(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.alarm_settings_cache[CAM_ID] = {"preAlarmMode": "ON", "alarmMode": "ON"}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "alarm_settings", {"preAlarmMode": "OFF", "alarmMode": "ON"}
        )
        assert coord.alarm_settings_cache[CAM_ID]["preAlarmMode"] == "OFF"


# BoschPanicAlarmSwitch — ON/OFF value pins
# NOTE: panic_alarm is a momentary/stateful siren — there is no cloud GET
#       endpoint, so state is purely local. Both ON and OFF send PUT.


class TestPanicAlarmSwitch:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

        return BoschPanicAlarmSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_camera_{CAM_ID}_panic_alarm"

    def test_is_on_false_by_default(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        assert sw.is_on is False

    def test_is_on_true_when_cache_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        coord.panic_alarm_cache = {CAM_ID: True}
        sw = self._make(coord, entry)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_sends_status_ON(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        """PUT body must carry {"status": "ON"}."""
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass_modepins(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "panic_alarm", {"status": "ON"}
        )
        assert coord.panic_alarm_cache[CAM_ID] is True
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sends_status_OFF(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        """PUT body must carry {"status": "OFF"}."""
        coord.panic_alarm_cache = {CAM_ID: True}
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass_modepins(sw)
        await sw.async_turn_off()
        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "panic_alarm", {"status": "OFF"}
        )
        assert coord.panic_alarm_cache[CAM_ID] is False
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_blocked_by_privacy(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        """Privacy-ON blocks turn_on — PUT must not be called."""
        coord.shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = self._make(coord, entry)
        sw.async_write_ha_state = MagicMock()
        _bind_hass_modepins(sw)
        await sw.async_turn_on()
        coord.async_put_camera.assert_not_called()


class TestFrontLightSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        return BoschFrontLightSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_front_light_{CAM_ID.lower()}"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.shc_state_cache[CAM_ID]["front_light"] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.shc_state_cache[CAM_ID]["front_light"] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_cloud_set_light_component_front_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_on()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "front", True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_set_light_component_front_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_off()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "front", False
        )


class TestWallwasherSwitchModePins:
    def _make(self, coord, entry):
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch

        return BoschWallwasherSwitch(coord, CAM_ID, entry)

    def test_unique_id(self, coord: SimpleNamespace, entry: SimpleNamespace):
        sw = self._make(coord, entry)
        assert sw.unique_id == f"bosch_shc_wallwasher_{CAM_ID.lower()}"

    def test_is_on_true(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.shc_state_cache[CAM_ID]["wallwasher"] = True
        sw = self._make(coord, entry)
        assert sw.is_on is True

    def test_is_on_false(self, coord: SimpleNamespace, entry: SimpleNamespace):
        coord.shc_state_cache[CAM_ID]["wallwasher"] = False
        sw = self._make(coord, entry)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_cloud_set_light_component_wallwasher_true(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_on()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "wallwasher", True
        )

    @pytest.mark.asyncio
    async def test_turn_off_calls_cloud_set_light_component_wallwasher_false(
        self, coord: SimpleNamespace, entry: SimpleNamespace
    ):
        sw = self._make(coord, entry)
        await sw.async_turn_off()
        coord.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "wallwasher", False
        )


def _base_info():
    return {
        "title": "Terrasse",
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "firmwareVersion": "9.40.25",
        "macAddress": "aa:bb:cc:dd:ee:01",
    }


def _stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _bind_hass(sw):
    """Attach a minimal hass (async_create_task + services.async_call) so
    async_write_ha_state doesn't raise."""
    sw.hass = SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    sw.async_write_ha_state = MagicMock()


def _resp_cm(status: int, json_data=None, raise_exc=None):
    """aiohttp-style async context manager mock."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    if raise_exc:
        cm.__aenter__ = AsyncMock(side_effect=raise_exc)
    else:
        cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _nvr_coord(**overrides):
    base = dict(
        data={CAM_ID: {"info": _base_info()}},
        nvr_user_intent={},
        nvr_processes={},
        nvr_error_state={},
        live_connections={},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
        options={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestNvrSwitchIntent:
    """is_on reflects intent immediately after toggle, before the coordinator
    has had a chance to refresh."""

    def test_is_on_reads_nvr_user_intent(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        assert sw.is_on is False

        coord.nvr_user_intent[CAM_ID] = True
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_sets_intent_before_write_ha_state(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        await sw.async_turn_on()

        # Intent MUST be set — is_on reads from it
        assert coord.nvr_user_intent[CAM_ID] is True
        coord.start_recorder.assert_awaited_once_with(CAM_ID)
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_turn_on_is_on_true_immediately(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord()
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        assert sw.is_on is False
        await sw.async_turn_on()
        # is_on must be True immediately — no coordinator tick needed
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_intent_false(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord(nvr_user_intent={CAM_ID: True})
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        assert sw.is_on is True
        await sw.async_turn_off()

        assert coord.nvr_user_intent[CAM_ID] is False
        assert sw.is_on is False
        coord.stop_recorder.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_turn_off_is_on_false_immediately(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        coord = _nvr_coord(nvr_user_intent={CAM_ID: True})
        sw = BoschNvrRecordingSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        await sw.async_turn_off()
        assert sw.is_on is False


# NVR event-clip opt-out switch (issue #43 follow-up feature request,
# realKim-dotcom) — default ON, per-camera opt-out of the native
# FCM-triggered event→clip assembly.


def _nvr_event_clip_coord(**overrides):
    base = dict(
        data={CAM_ID: {"info": _base_info()}},
        _nvr_event_clip_enabled={},
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    coord.get_nvr_event_clip_enabled = lambda cid: coord._nvr_event_clip_enabled.get(
        cid, True
    )
    coord.set_nvr_event_clip_enabled = lambda cid, enabled: (
        coord._nvr_event_clip_enabled.__setitem__(cid, enabled)
    )
    return coord


class TestNvrEventClipSwitch:
    """Default ON (backward compatible); OFF disables only the native
    event→clip assembly, not the underlying pre-roll ring."""

    def test_is_on_defaults_true(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrEventClipSwitch

        coord = _nvr_event_clip_coord()
        sw = BoschNvrEventClipSwitch(coord, CAM_ID, _stub_entry())
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_disabled(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrEventClipSwitch

        coord = _nvr_event_clip_coord()
        sw = BoschNvrEventClipSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        await sw.async_turn_off()

        assert coord._nvr_event_clip_enabled[CAM_ID] is False
        assert sw.is_on is False
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_turn_on_after_off_restores_true(self):
        from custom_components.bosch_shc_camera.switch import BoschNvrEventClipSwitch

        coord = _nvr_event_clip_coord(_nvr_event_clip_enabled={CAM_ID: False})
        sw = BoschNvrEventClipSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        assert sw.is_on is False
        await sw.async_turn_on()

        assert coord._nvr_event_clip_enabled[CAM_ID] is True
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_restore_off_state_from_previous_session(self):
        """async_added_to_hass must restore OFF — RestoreEntity persistence
        across HA restarts, same discipline as BoschNvrRecordingSwitch."""
        from custom_components.bosch_shc_camera.switch import BoschNvrEventClipSwitch

        coord = _nvr_event_clip_coord()
        sw = BoschNvrEventClipSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="off"))

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_restore_noop_when_no_previous_state_stays_default_true(self):
        """No previous state (fresh install / entity never toggled) — must
        stay at the coordinator's default (True), not silently flip to
        False."""
        from custom_components.bosch_shc_camera.switch import BoschNvrEventClipSwitch

        coord = _nvr_event_clip_coord()
        sw = BoschNvrEventClipSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=None)

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is True
        assert CAM_ID not in coord._nvr_event_clip_enabled

    @pytest.mark.asyncio
    async def test_restore_unavailable_state_does_not_disable(self):
        """Regression (bug-hunt finding, issue #43 follow-up): HA persists
        the restore-cache entry as "unavailable" if the coordinator's last
        update failed at shutdown. Since this entity defaults to enabled,
        blindly writing `last.state == "on"` for that case would silently
        disable a feature the user never turned off — must be a no-op,
        same as no previous state at all."""
        from custom_components.bosch_shc_camera.switch import BoschNvrEventClipSwitch

        coord = _nvr_event_clip_coord()
        sw = BoschNvrEventClipSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="unavailable"))

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is True
        assert CAM_ID not in coord._nvr_event_clip_enabled


def _setup_coord():
    """Minimal coordinator for async_setup_entry tests.

    Must supply every attribute that switch entity __init__ methods touch
    during construction (not async_added_to_hass, which runs later under HA).
    audio_enabled is seeded by BoschAudioSwitch.__init__ via setdefault().
    """
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {},
                }
            }
        },
        live_stream_entities={},
        audio_enabled={},
        options={},
    )


class TestSetupEntryGuard:
    """Setting enable_snapshot_button=False must NOT block switches."""

    @pytest.mark.asyncio
    async def test_switches_registered_when_snapshot_button_disabled(self):
        """async_setup_entry must register entities even if enable_snapshot_button=False."""
        from custom_components.bosch_shc_camera.switch import async_setup_entry

        coord = _setup_coord()

        # ConfigEntry has enable_snapshot_button=False — this must not abort setup early
        entry = SimpleNamespace(
            entry_id="01ENTRY",
            runtime_data=coord,
            data={},
            options={"enable_snapshot_button": False},
        )
        hass = SimpleNamespace()

        added: list = []

        def _add(entities: list, **kw: object) -> None:
            added.extend(entities)

        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=MagicMock(async_get_entity_id=MagicMock(return_value=None)),
        ):
            with patch(
                "custom_components.bosch_shc_camera.switch.get_options",
                return_value={"enable_snapshot_button": False},
            ):
                await async_setup_entry(hass, entry, _add)

        # At least BoschLiveStreamSwitch, BoschAudioSwitch, BoschPrivacyModeSwitch
        # must be present regardless of the snapshot-button option.
        assert len(added) > 0, (
            "No switches registered — the enable_snapshot_button guard incorrectly "
            "blocked all switch entities"
        )

    @pytest.mark.asyncio
    async def test_switches_registered_when_snapshot_button_enabled(self):
        """Baseline: entities are still registered when enable_snapshot_button=True."""
        from custom_components.bosch_shc_camera.switch import async_setup_entry

        coord = _setup_coord()
        entry = SimpleNamespace(
            entry_id="01ENTRY",
            runtime_data=coord,
            data={},
            options={"enable_snapshot_button": True},
        )
        hass = SimpleNamespace()
        added: list = []

        def _add(entities: list, **kw: object) -> None:
            added.extend(entities)

        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=MagicMock(async_get_entity_id=MagicMock(return_value=None)),
        ):
            with patch(
                "custom_components.bosch_shc_camera.switch.get_options",
                return_value={"enable_snapshot_button": True},
            ):
                await async_setup_entry(hass, entry, _add)

        assert len(added) > 0


def _ambient_coord(**overrides):
    base = dict(
        data={CAM_ID: {"info": _base_info()}},
        token="tok-A",
        ambient_lighting_cache={},
        last_update_success=True,
        is_camera_online=lambda cid: True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestAmbientLightCacheUpdate:
    """ambient_lighting_cache is updated on successful PUT (and left alone
    on failure), and is_on prefers the cache over the stale _is_on field."""

    @pytest.mark.asyncio
    async def test_cache_updated_on_turn_on(self):
        existing = {"ambientLightEnabled": False, "schedule": "dusk-to-dawn"}
        coord = _ambient_coord(ambient_lighting_cache={})
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        get_resp = _resp_cm(200, json_data=existing)
        put_resp = _resp_cm(204)
        session = MagicMock()
        session.get = MagicMock(return_value=get_resp)
        session.put = MagicMock(return_value=put_resp)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            await sw._set_ambient_light(True)

        # Cache must now contain the updated dict (not empty)
        cache = coord.ambient_lighting_cache.get(CAM_ID)
        assert cache is not None, "Cache not updated after successful PUT"
        assert cache["ambientLightEnabled"] is True
        # is_on should now read True from cache (not _is_on)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_cache_updated_on_turn_off(self):
        existing = {"ambientLightEnabled": True, "schedule": "dusk-to-dawn"}
        coord = _ambient_coord(ambient_lighting_cache={})
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        get_resp = _resp_cm(200, json_data=existing)
        put_resp = _resp_cm(200)
        session = MagicMock()
        session.get = MagicMock(return_value=get_resp)
        session.put = MagicMock(return_value=put_resp)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            await sw._set_ambient_light(False)

        cache = coord.ambient_lighting_cache.get(CAM_ID)
        assert cache is not None
        assert cache["ambientLightEnabled"] is False
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_cache_not_updated_on_http_error(self):
        """If PUT returns non-2xx, cache must NOT be updated."""
        existing = {"ambientLightEnabled": False}
        coord = _ambient_coord(ambient_lighting_cache={})
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)

        get_resp = _resp_cm(200, json_data=existing)
        put_resp = _resp_cm(500)  # server error
        session = MagicMock()
        session.get = MagicMock(return_value=get_resp)
        session.put = MagicMock(return_value=put_resp)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=session),
        ):
            await sw._set_ambient_light(True)

        # Cache should remain empty (no update on failure)
        assert coord.ambient_lighting_cache.get(CAM_ID) is None
        assert sw._is_on is None  # not set either

    @pytest.mark.asyncio
    async def test_is_on_prefers_cache_over_is_on_field(self):
        """is_on must return the cache value, not the stale _is_on field."""
        # Cache says True, _is_on will be set to True; then simulate a poll that
        # overwrites _is_on (via direct field set) while cache still says True.
        coord = _ambient_coord(
            ambient_lighting_cache={CAM_ID: {"ambientLightEnabled": True}}
        )
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _stub_entry())
        sw._is_on = False  # stale pre-cache value
        # is_on must prefer the cache
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_get_non_200_early_return(self):
        """Non-200 from the GET (of the existing ambient-light config) → early
        return before any PUT is attempted."""
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_make_response_cm(500))
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _make_entry())
        sw.hass = MagicMock()
        sw.async_write_ha_state = MagicMock()

        with patch(
            f"{switch_mod.__name__}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await sw._set_ambient_light(True)
        # PUT must not have been called (returned early after GET 500)
        assert session.put.call_count == 0 if hasattr(session, "put") else True

    @pytest.mark.asyncio
    async def test_exception_caught(self):
        """Any exception raised while reading/writing ambient-light config is
        logged and swallowed (never propagates to the caller)."""
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(side_effect=RuntimeError("boom"))
        sw = BoschAmbientLightSwitch(coord, CAM_ID, _make_entry())
        sw.hass = MagicMock()
        sw.async_write_ha_state = MagicMock()

        with patch(
            f"{switch_mod.__name__}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            # Must not raise — exception is swallowed by `except Exception`
            await sw._set_ambient_light(True)
        sw.async_write_ha_state.assert_called_once()


class TestIntercomRestoreEntity:
    """IntercomSwitch restores ON/OFF state across HA restarts."""

    def test_intercom_inherits_restore_entity(self):
        from homeassistant.helpers.restore_state import RestoreEntity

        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        assert issubclass(BoschIntercomSwitch, RestoreEntity), (
            "BoschIntercomSwitch must inherit RestoreEntity for state persistence"
        )

    def test_default_is_off_on_first_start(self):
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_restore_on_state_from_previous_session(self):
        """async_added_to_hass must restore ON from last persisted state."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="on"))

        # Patch the first base class async_added_to_hass to skip CoordinatorEntity
        # setup (which needs a real coordinator/hass).
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_restore_off_state_from_previous_session(self):
        """async_added_to_hass must restore OFF (not flip to default True)."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        sw._is_on = True  # as if turned on during this session
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="off"))

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_restore_noop_when_no_previous_state(self):
        """async_added_to_hass with None last_state must leave _is_on False (default)."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=None)

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_restore_noop_for_unknown_state(self):
        """async_added_to_hass must ignore non-on/off states (e.g. 'unavailable')."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        coord = SimpleNamespace(
            data={CAM_ID: {"info": _base_info()}},
        )
        sw = BoschIntercomSwitch(coord, CAM_ID, _stub_entry())
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=MagicMock(state="unavailable"))

        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()

        assert sw.is_on is False  # unchanged default


def _make_coord(**overrides):
    """Stub coordinator with all fields needed by switch.py paths."""
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0, "sound": False},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        last_update_success=True,
        is_camera_online=lambda cid: True,
        shc_state_cache={CAM_ID: {}},
        global_lighting_cache={
            CAM_ID: {"darknessThreshold": 0.5, "softLightFading": False}
        },
        privacy_set_at={},
        light_set_at={},
        live_connections={},
        tear_down_live_stream=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
        token="fake-token",
        hass=MagicMock(),
        options={"recording_quality": "high"},
        _intrusion_config={CAM_ID: {}},
        _notifications_pref={CAM_ID: False},
        audio_enabled={CAM_ID: True},
        privacy_sound_cache={CAM_ID: False},
        timestamp_cache={CAM_ID: True},
        _record_sound_cache={CAM_ID: True},
        _auto_follow_cache={CAM_ID: False},
        _motion_enabled_cache={CAM_ID: True},
        _intercom_cache={CAM_ID: False},
        _image_rotation_cache={CAM_ID: False},
        _front_light_cache={CAM_ID: False},
        _wallwasher_cache={CAM_ID: False},
        _status_led_cache={CAM_ID: True},
        motion_light_cache={CAM_ID: False},
        ambient_light_cache={CAM_ID: False},
        _soft_light_fading_cache={CAM_ID: False},
        _intrusion_detection_cache={CAM_ID: False},
        intrusion_config_cache={CAM_ID: {}},
        _alarm_system_cache={CAM_ID: False},
        _alarm_mode_cache={CAM_ID: "STANDARD"},
        _pre_alarm_cache={CAM_ID: False},
        _nvr_recording_cache={CAM_ID: False},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_entry():
    e = MagicMock()
    e.entry_id = "test-entry"
    e.options = {}
    return e


def _make_response_cm(status, json_data=None):
    """Build an async context-manager that returns a mock response."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_device_info_with_mac():
    """DeviceInfo is populated with a mac connection when macAddress is present."""
    coord = _make_coord()
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    info = sw.device_info
    assert info is not None
    assert (switch_mod.DOMAIN, CAM_ID) in info["identifiers"]
    assert info["manufacturer"] == "Bosch"
    assert info["connections"] == {("mac", "aa:bb:cc:dd:ee:01")}


def test_device_info_without_mac():
    """DeviceInfo connections is an empty set when no mac is present."""
    coord = _make_coord()
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    sw._mac = ""
    info = sw.device_info
    assert info["connections"] == set()


def test_camera_light_is_on_true():
    coord = _make_coord()
    coord.shc_state_cache[CAM_ID] = {"camera_light": True}
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is True


def test_camera_light_is_on_missing():
    coord = _make_coord()
    coord.shc_state_cache = {}  # no entry at all
    sw = BoschCameraLightSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_wallwasher_turn_off():
    coord = _make_coord()
    sw = BoschWallwasherSwitch(coord, CAM_ID, _make_entry())
    await sw.async_turn_off()
    coord.async_cloud_set_light_component.assert_called_once_with(
        CAM_ID, "wallwasher", False
    )


@pytest.mark.asyncio
async def test_privacy_turn_off_cooldown_defers():
    coord = _make_coord()
    # Within the cooldown window the toggle is DEFERRED + coalesced (not raised,
    # not applied immediately) so an automation's later steps keep running and
    # the card's optimistic flip stays correct. Applied once it clears.
    sw = BoschPrivacyModeSwitch(coord, CAM_ID, _make_entry())
    sw.hass = MagicMock()
    sw.async_write_ha_state = MagicMock()
    with patch.object(sw, "_privacy_block_remaining", return_value=3.0):
        await sw.async_turn_off()  # must NOT raise
    coord.async_cloud_set_privacy_mode.assert_not_called()
    assert sw._pending_privacy is False
    sw.hass.async_create_task.assert_called_once()
    sw.hass.async_create_task.call_args.args[0].close()


def test_notifications_is_on_none_when_status_none():
    coord = _make_coord()
    coord.shc_state_cache[CAM_ID] = {"notifications_status": None}
    sw = BoschNotificationsSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is None


def test_notifications_is_on_true_for_follow():
    coord = _make_coord()
    coord.shc_state_cache[CAM_ID] = {"notifications_status": "FOLLOW_CAMERA_SCHEDULE"}
    sw = BoschNotificationsSwitch(coord, CAM_ID, _make_entry())
    assert sw.is_on is True


@pytest.mark.asyncio
async def test_softlight_fading_json_exception_falls_back_to_body():
    """If json() raises on the PUT response, cache falls back to the request body."""
    coord = _make_coord()
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(side_effect=ValueError("not json"))
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.put = MagicMock(return_value=cm)

    sw = BoschSoftLightFadingSwitch(coord, CAM_ID, _make_entry())
    sw.hass = MagicMock()
    sw.async_write_ha_state = MagicMock()

    with patch(
        f"{switch_mod.__name__}.async_get_bosch_cloud_session",
        new=AsyncMock(return_value=session),
    ):
        await sw._put_global_lighting(True)

    # When json() raised, body fallback must populate cache
    cached = coord.global_lighting_cache[CAM_ID]
    assert cached["softLightFading"] is True
    assert cached["darknessThreshold"] == 0.5


@pytest.mark.asyncio
async def test_intrusion_detection_empty_config_returns():
    """An empty intrusion _config dict → early return without async_put_camera."""
    coord = _make_coord()
    # intrusion_config_cache returns {} → _config property is {} → `if not cfg: return`
    coord.intrusion_config_cache[CAM_ID] = {}
    sw = BoschIntrusionDetectionSwitch(coord, CAM_ID, _make_entry())
    sw.async_write_ha_state = MagicMock()
    with patch(
        f"{switch_mod.__name__}._warn_if_privacy_on", new=AsyncMock(return_value=False)
    ):
        await sw._set_intrusion(True)
    coord.async_put_camera.assert_not_called()


@pytest.mark.asyncio
async def test_setup_entry_auto_follow_for_pan_camera():
    """BoschAutoFollowSwitch is added when panLimit > 0 (CAMERA_360)."""
    coord = _make_coord()
    coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"
    coord.data[CAM_ID]["info"]["featureSupport"] = {
        "light": False,
        "panLimit": 360,
        "sound": False,
    }
    entry = _make_entry()
    entry.runtime_data = coord
    added = []

    def async_add(ents, **kw):
        return added.extend(ents)

    hass = MagicMock()
    await switch_mod.async_setup_entry(hass, entry, async_add)
    classes = {type(e).__name__ for e in added}
    assert "BoschAutoFollowSwitch" in classes


@pytest.mark.asyncio
async def test_setup_entry_audio_notification_when_sound_supported():
    """BoschNotificationTypeSwitch('audio') is added when has_sound=True."""
    coord = _make_coord()
    coord.data[CAM_ID]["info"]["featureSupport"]["sound"] = True
    entry = _make_entry()
    entry.runtime_data = coord
    added = []

    def async_add(ents, **kw):
        return added.extend(ents)

    hass = MagicMock()
    await switch_mod.async_setup_entry(hass, entry, async_add)
    # The audio NotificationType switch has _ntype="audio"
    audio_switches = [
        e
        for e in added
        if type(e).__name__ == "BoschNotificationTypeSwitch"
        and getattr(e, "_ntype", None) == "audio"
    ]
    assert len(audio_switches) == 1


@pytest.mark.asyncio
async def test_setup_entry_gen2_indoor_alarm_switches():
    """3 alarm switches are added for Gen2 Indoor cameras.

    BoschAudioAlarmSwitch was removed in v13.x — audio alarm feature dropped.
    """
    coord = _make_coord()
    coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
    entry = _make_entry()
    entry.runtime_data = coord
    added = []

    def async_add(ents, **kw):
        return added.extend(ents)

    hass = MagicMock()
    await switch_mod.async_setup_entry(hass, entry, async_add)
    classes = {type(e).__name__ for e in added}
    assert "BoschAlarmSystemArmSwitch" in classes
    assert "BoschAlarmModeSwitch" in classes
    assert "BoschPreAlarmSwitch" in classes
    assert "BoschPanicAlarmSwitch" in classes, (
        "BoschPanicAlarmSwitch added in v12.0.4 for Gen2 siren trigger"
    )


# BoschMotionLightSwitch — network-fetch fallback paths (empty cache forces
# a GET before the PUT; token-missing/HTTP-error/exception all short-circuit
# before any write, leaving local state untouched)


def _stub_coord_motionlight(token: str | None = "tok-A", **overrides):
    """Minimal coordinator stub with empty motion_light cache (forces GET fallback)."""
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                },
            },
        },
        token=token,
        motion_light_cache={},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_entry_c() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _resp_cm_motionlight(
    status: int, raise_exc: Exception | None = None, json_data=None
):
    """aiohttp-style async context manager mock returning a response with given status."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    cm = MagicMock()
    if raise_exc is not None:
        cm.__aenter__ = AsyncMock(side_effect=raise_exc)
    else:
        cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestMotionLightNoTokenEarlyReturn:
    """Empty cache + no token → return before HTTP/PUT.

    A coordinator without a bearer token cannot authenticate the GET, so the
    method must bail out cleanly. Without this guard a None Authorization
    header would be sent and the camera would 401.
    """

    @pytest.mark.asyncio
    async def test_no_token_short_circuits_before_get(
        self, stub_entry_c: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord_motionlight(token=None)
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry_c)
        _bind_hass(sw)

        # Sentinel: if we reach session.get the test failed.
        session = MagicMock()
        session.get = MagicMock(
            side_effect=AssertionError(
                "session.get must not be called when token is missing"
            )
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await sw.async_turn_on()

        coord.async_put_camera.assert_not_awaited()
        assert sw._is_on is None, "no token → toggle must not flip local state"


class TestMotionLightGetHttpError:
    """GET returns non-200 → warn + return; no PUT."""

    @pytest.mark.asyncio
    async def test_http_500_returns_without_put(self, stub_entry_c: SimpleNamespace):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord_motionlight()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry_c)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_motionlight(500))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await sw.async_turn_on()

        (
            coord.async_put_camera.assert_not_awaited(),
            ("non-200 GET must short-circuit before async_put_camera"),
        )
        assert sw._is_on is None, "failed GET must leave local state untouched"

    @pytest.mark.asyncio
    async def test_http_401_returns_without_put(self, stub_entry_c: SimpleNamespace):
        """401 (auth expired) is just another non-200 status — same early return."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord_motionlight()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry_c)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_motionlight(401))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await sw.async_turn_off()

        coord.async_put_camera.assert_not_awaited()


class TestMotionLightGetRaises:
    """GET raises → broad except logs + returns; no PUT.

    Covers timeout, connection error, and any other transport failure.
    """

    @pytest.mark.asyncio
    async def test_get_raises_timeout_returns_without_put(
        self, stub_entry_c: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord_motionlight()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry_c)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_motionlight(0, raise_exc=TimeoutError())
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            # Must not raise even though session.get raises
            await sw.async_turn_on()

        (
            coord.async_put_camera.assert_not_awaited(),
            ("GET timeout must be swallowed and short-circuit before PUT"),
        )
        assert sw._is_on is None

    @pytest.mark.asyncio
    async def test_get_raises_generic_returns_without_put(
        self, stub_entry_c: SimpleNamespace
    ):
        """Any unexpected error from session.get is caught and the call no-ops."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord_motionlight()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry_c)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_motionlight(0, raise_exc=RuntimeError("boom"))
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await sw.async_turn_off()

        coord.async_put_camera.assert_not_awaited()
        assert sw._is_on is None


# BoschLiveStreamSwitch._stream_health_watchdog — LOCAL stream health polling
#
# The watchdog runs after a LOCAL stream-on. It probes HA's `Stream` object
# at +60s and +120s (mocked via patched asyncio.sleep so the test runs in
# milliseconds). Three states map to three actions:
#   - "healthy" (Stream.available True) → record_stream_success, exit
#   - "no_consumer" (no Stream object) → exit silently (FFmpeg never
#     started, restart wouldn't help — frontend card unmounted)
#   - "unhealthy" (Stream object but available=False) → at first tick
#     record_stream_error + restart; at second tick saturate the error
#     counter to force REMOTE on next try_live_connection.


def _make_coord_streamhealth(**overrides):
    base = dict(
        live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
        user_intent_streams={CAM_ID},  # watchdog reconnect gate
        camera_entities={},
        stream_error_count={},
        stop_tls_proxy=AsyncMock(),
        stop_viewing_front_door=AsyncMock(),
        stop_remote_viewing_front_door=AsyncMock(),
        try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        record_stream_error=MagicMock(),
        record_stream_success=MagicMock(),
        get_model_config=lambda cid: SimpleNamespace(max_stream_errors=3),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_switch_streamhealth(coord=None):
    """Build a BoschLiveStreamSwitch stub bypassing __init__."""
    coord = coord or _make_coord_streamhealth()
    sw = BoschLiveStreamSwitch.__new__(BoschLiveStreamSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw.async_write_ha_state = MagicMock()
    return sw


class TestHealthClassifier:
    @pytest.mark.asyncio
    async def test_healthy_stream_calls_record_success(self):
        """Stream.available=True → record_stream_success + exit early.
        No restart, no further escalation. Pin so the success path
        keeps clearing the per-cam error counter."""
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=True))
        coord = _make_coord_streamhealth(camera_entities={CAM_ID: cam_entity})
        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.record_stream_success.assert_called_once_with(CAM_ID)
        coord.try_live_connection.assert_not_awaited()
        coord.stop_tls_proxy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_consumer_exits_silently(self):
        """No camera entity stream object → FFmpeg never started, so
        restarting the LOCAL session wouldn't help. Exit silently
        leaving the LOCAL session up for a future consumer."""
        cam_entity = SimpleNamespace(stream=None)
        coord = _make_coord_streamhealth(camera_entities={CAM_ID: cam_entity})
        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()
        coord.record_stream_error.assert_not_called()
        coord.record_stream_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_camera_entity_treated_as_no_consumer(self):
        """Camera entity not yet registered (race) → same outcome as
        no Stream object: silent exit."""
        coord = _make_coord_streamhealth(camera_entities={})  # cam not in dict
        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()


class TestStreamOffShortCircuit:
    @pytest.mark.asyncio
    async def test_user_turned_off_during_first_sleep(self):
        """Live conn cleared between watchdog start and first tick →
        nothing to watch, exit. Pin so the watchdog doesn't fire
        spurious restarts after the user already turned the switch off."""
        coord = _make_coord_streamhealth(live_connections={})  # already off
        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_remote_fallback_short_circuits(self):
        """If something else (manual mode change, REMOTE fallback)
        flipped the connection to REMOTE, the LOCAL watchdog stops —
        REMOTE has no LOCAL-specific failure modes."""
        coord = _make_coord_streamhealth(
            live_connections={
                CAM_ID: {"_connection_type": "REMOTE"},
            }
        )
        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.try_live_connection.assert_not_awaited()


class TestUnhealthyRestart:
    @pytest.mark.asyncio
    async def test_first_unhealthy_records_error_and_restarts(self):
        """First unhealthy tick → record_stream_error + tear down +
        try_live_connection. Counter NOT yet saturated — gradual
        escalation via per-model threshold."""
        # Stream object exists but available=False initially
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))
        coord = _make_coord_streamhealth(camera_entities={CAM_ID: cam_entity})

        # Mock try_live_connection to repopulate live_connections
        # (the real method does this; the mock must too for the watchdog
        # to find the live conn on the next tick).
        async def _restart(cid):
            coord.live_connections[cid] = {"_connection_type": "LOCAL"}
            return {"_connection_type": "LOCAL"}

        coord.try_live_connection = AsyncMock(side_effect=_restart)

        # Flip stream to healthy after the first sleep so the second
        # tick records success + exits cleanly.
        sleep_calls = [0]

        async def _sleep(_delay):
            sleep_calls[0] += 1
            if sleep_calls[0] == 2:
                cam_entity.stream = SimpleNamespace(available=True)

        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock(side_effect=_sleep)):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        coord.record_stream_error.assert_called_once_with(CAM_ID)
        coord.stop_tls_proxy.assert_awaited_once_with(CAM_ID)
        coord.try_live_connection.assert_awaited_once_with(CAM_ID)
        # Second tick was healthy → success recorded
        coord.record_stream_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_unhealthy_saturates_counter(self):
        """Second consecutive unhealthy tick → bypass the per-model
        threshold by setting `stream_error_count` directly to
        max_stream_errors. Forces REMOTE on the next try_live_connection."""
        # Always unhealthy
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))
        coord = _make_coord_streamhealth(camera_entities={CAM_ID: cam_entity})

        async def _restart(cid):
            coord.live_connections[cid] = {"_connection_type": "LOCAL"}
            return {"_connection_type": "LOCAL"}

        coord.try_live_connection = AsyncMock(side_effect=_restart)
        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        # Counter saturated at max_stream_errors (=3 in our stub config)
        assert coord.stream_error_count[CAM_ID] == 3
        # Two restart attempts (first + second tick)
        assert coord.stop_tls_proxy.await_count == 2
        assert coord.try_live_connection.await_count == 2

    @pytest.mark.asyncio
    async def test_remote_fallback_after_first_unhealthy_exits(self):
        """First tick unhealthy → restart. try_live_connection picks
        REMOTE this time → exit (no second tick)."""
        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))
        coord = _make_coord_streamhealth(camera_entities={CAM_ID: cam_entity})
        # Restart returns REMOTE → watchdog exits
        coord.try_live_connection = AsyncMock(
            return_value={"_connection_type": "REMOTE"},
        )
        sw = _make_switch_streamhealth(coord)
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM_ID)
        # One restart → REMOTE → exit (no second sleep)
        assert coord.try_live_connection.await_count == 1
        sw.async_write_ha_state.assert_called()


# RTSP credential redaction — `_redact_rtsp_creds` and
# BoschLiveStreamSwitch.extra_state_attributes must never leak Digest/proxy
# userinfo (user:password@host) into logs or entity attributes


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # LOCAL proxy URL with Digest userinfo — the actual leak.
        (
            "rtsp://cbs-13494370:N-pa%24sw0rd@127.0.0.1:33689/rtsp_tunnel?inst=1&enableaudio=1",
            "rtsp://***:***@127.0.0.1:33689/rtsp_tunnel?inst=1&enableaudio=1",
        ),
        # rtsps scheme (REMOTE) with creds.
        (
            "rtsps://user:secret@cloud.example.com:8554/path?token=abc",
            "rtsps://***:***@cloud.example.com:8554/path?token=abc",
        ),
        # Userinfo with no port.
        (
            "rtsp://u:p@10.0.0.5/stream",
            "rtsp://***:***@10.0.0.5/stream",
        ),
        # Username only (no colon) still gets masked.
        (
            "rtsp://justuser@host:554/s",
            "rtsp://***:***@host:554/s",
        ),
        # No credentials → returned unchanged.
        (
            "rtsp://127.0.0.1:33689/rtsp_tunnel?inst=1",
            "rtsp://127.0.0.1:33689/rtsp_tunnel?inst=1",
        ),
        # Empty string → empty string (matches result.get default).
        ("", ""),
    ],
)
def test_redact_rtsp_creds(url: str, expected: str) -> None:
    """No credentials survive; non-cred URLs and empties pass through."""
    redacted = _redact_rtsp_creds(url)
    assert redacted == expected
    # Defence in depth: the original userinfo must not appear anywhere.
    if "@" in url and ":" in url.split("@", 1)[0]:
        secret = url.split("://", 1)[1].split("@", 1)[0]
        assert secret not in redacted


class TestExtraStateAttributesRedaction:
    """extra_state_attributes must redact rtsps_url."""

    def test_extra_state_attributes_redacts_creds(self) -> None:
        """rtspsUrl with LOCAL/Digest creds in live_connections → attribute has ***:***."""
        raw_url = "rtsp://cbs-AABBCCDD:N-pa%24sw0rd@127.0.0.1:33689/rtsp_tunnel?inst=1"
        coord = SimpleNamespace(
            live_connections={
                "cam-1": {
                    "rtspsUrl": raw_url,
                    "_connection_type": "LOCAL",
                    "proxyUrl": "",
                }
            },
            shc_state_cache={"cam-1": {}},
            options={},
        )
        switch = object.__new__(BoschLiveStreamSwitch)
        switch.coordinator = coord
        switch._cam_id = "cam-1"

        attrs = switch.extra_state_attributes
        assert "***:***" in attrs["rtsps_url"]
        assert "cbs-AABBCCDD" not in attrs["rtsps_url"]
        assert "N-pa%24sw0rd" not in attrs["rtsps_url"]

    def test_extra_state_attributes_empty_url_stays_empty(self) -> None:
        """Empty rtspsUrl (stream off) → empty string attribute (no crash)."""
        coord = SimpleNamespace(
            live_connections={
                "cam-1": {"rtspsUrl": "", "_connection_type": "REMOTE", "proxyUrl": ""}
            },
            shc_state_cache={"cam-1": {}},
            options={},
        )
        switch = object.__new__(BoschLiveStreamSwitch)
        switch.coordinator = coord
        switch._cam_id = "cam-1"

        attrs = switch.extra_state_attributes
        assert attrs["rtsps_url"] == ""

    def test_extra_state_attributes_no_creds_passes_through(self) -> None:
        """REMOTE URL without userinfo → returned unchanged (no false redaction)."""
        no_cred_url = "rtsps://cloud.boschsecurity.com:8554/stream/abc123"
        coord = SimpleNamespace(
            live_connections={
                "cam-1": {
                    "rtspsUrl": no_cred_url,
                    "_connection_type": "REMOTE",
                    "proxyUrl": "",
                }
            },
            shc_state_cache={"cam-1": {}},
            options={},
        )
        switch = object.__new__(BoschLiveStreamSwitch)
        switch.coordinator = coord
        switch._cam_id = "cam-1"

        attrs = switch.extra_state_attributes
        assert attrs["rtsps_url"] == no_cred_url


# Write-failure warnings — camera-light/front-light/wallwasher/notifications
# switches must log a WARNING when the cloud write fails on every fallback
# path, and stay silent on success


def _coord_writefail(**overrides: object) -> SimpleNamespace:
    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        async_cloud_set_camera_light=AsyncMock(return_value=False),
        async_cloud_set_light_component=AsyncMock(return_value=False),
        async_cloud_set_notifications=AsyncMock(return_value=False),
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _entry_writefail() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


@pytest.mark.asyncio
class TestCameraLightSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog: pytest.LogCaptureFixture):
        sw = BoschCameraLightSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any(
            "Camera light toggle for Terrasse (ON) failed on all paths" in r.message
            for r in caplog.records
        )

    async def test_turn_off_failure_warns(self, caplog: pytest.LogCaptureFixture):
        sw = BoschCameraLightSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any(
            "Camera light toggle for Terrasse (OFF) failed on all paths" in r.message
            for r in caplog.records
        )

    async def test_turn_on_success_is_silent(self, caplog: pytest.LogCaptureFixture):
        sw = BoschCameraLightSwitch(
            _coord_writefail(async_cloud_set_camera_light=AsyncMock(return_value=True)),
            CAM_ID,
            _entry_writefail(),
        )
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert not any("failed on all paths" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestFrontLightSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog: pytest.LogCaptureFixture):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        sw = BoschFrontLightSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any(
            "Front light toggle for Terrasse (ON) failed on all paths" in r.message
            for r in caplog.records
        )

    async def test_turn_off_failure_warns(self, caplog: pytest.LogCaptureFixture):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        sw = BoschFrontLightSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any(
            "Front light toggle for Terrasse (OFF) failed on all paths" in r.message
            for r in caplog.records
        )


@pytest.mark.asyncio
class TestWallwasherSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog: pytest.LogCaptureFixture):
        sw = BoschWallwasherSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any(
            "Wallwasher toggle for Terrasse (ON) failed on all paths" in r.message
            for r in caplog.records
        )

    async def test_turn_off_failure_warns(self, caplog: pytest.LogCaptureFixture):
        sw = BoschWallwasherSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any(
            "Wallwasher toggle for Terrasse (OFF) failed on all paths" in r.message
            for r in caplog.records
        )


@pytest.mark.asyncio
class TestNotificationsSwitchWarnsOnFailure:
    async def test_turn_on_failure_warns(self, caplog: pytest.LogCaptureFixture):
        sw = BoschNotificationsSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_on()
        assert any(
            "Notifications toggle for Terrasse (ON) failed on all paths" in r.message
            for r in caplog.records
        )

    async def test_turn_off_failure_warns(self, caplog: pytest.LogCaptureFixture):
        sw = BoschNotificationsSwitch(_coord_writefail(), CAM_ID, _entry_writefail())
        with caplog.at_level(logging.WARNING):
            await sw.async_turn_off()
        assert any(
            "Notifications toggle for Terrasse (OFF) failed on all paths" in r.message
            for r in caplog.records
        )


def _coord_audio(cfg: dict | None, privacy_on: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        audio_detection_cache={CAM_ID: cfg} if cfg is not None else {},
        audio_detection_set_at={},
        shc_state_cache={CAM_ID: {"privacy_mode": privacy_on}},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )


def _entry_d() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={"bearer_token": "x"}, options={})


def _make(cls_name: str, coord: SimpleNamespace):
    import custom_components.bosch_shc_camera.switch as sw_mod

    sw = getattr(sw_mod, cls_name)(coord, CAM_ID, _entry_d())
    sw.async_write_ha_state = MagicMock()  # not added to hass in unit test
    sw.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    return sw


GLASS = "BoschGlassBreakDetectionSwitch"
FIRE = "BoschFireAlarmDetectionSwitch"


# ── state mapping (PIN_EVERY_MODE: on / off / unknown) ────────────────────
@pytest.mark.parametrize(
    ("cls", "cfg", "expected"),
    [
        (GLASS, {"detectGlassBreak": True, "detectFireAlarm": False}, True),
        (GLASS, {"detectGlassBreak": False, "detectFireAlarm": True}, False),
        (GLASS, {"detectFireAlarm": True}, None),  # field missing → unknown
        (FIRE, {"detectGlassBreak": False, "detectFireAlarm": True}, True),
        (FIRE, {"detectGlassBreak": True, "detectFireAlarm": False}, False),
        (FIRE, {"detectGlassBreak": True}, None),
    ],
)
def test_is_on_maps_its_own_field(
    cls: str, cfg: dict[str, bool] | None, expected: bool | None
):
    sw = _make(cls, _coord_audio(cfg))
    assert sw.is_on is expected


def test_unavailable_without_cache():
    sw = _make(GLASS, _coord_audio(None))
    assert sw.available is False


def test_available_with_cache():
    sw = _make(
        GLASS, _coord_audio({"detectGlassBreak": False, "detectFireAlarm": False})
    )
    assert sw.available is True


# ── write preserves the OTHER field (both always sent) ────────────────────
async def test_glass_on_preserves_fire():
    coord = _coord_audio({"detectGlassBreak": False, "detectFireAlarm": True})
    sw = _make(GLASS, coord)
    await sw.async_turn_on()
    coord.async_put_camera.assert_awaited_once_with(
        CAM_ID,
        "audioDetectionConfig",
        {"detectGlassBreak": True, "detectFireAlarm": True},
    )


async def test_fire_off_preserves_glass():
    coord = _coord_audio({"detectGlassBreak": True, "detectFireAlarm": True})
    sw = _make(FIRE, coord)
    await sw.async_turn_off()
    coord.async_put_camera.assert_awaited_once_with(
        CAM_ID,
        "audioDetectionConfig",
        {"detectGlassBreak": True, "detectFireAlarm": False},
    )


async def test_successful_write_updates_cache_and_lock():
    coord = _coord_audio({"detectGlassBreak": False, "detectFireAlarm": False})
    sw = _make(GLASS, coord)
    await sw.async_turn_on()
    assert coord.audio_detection_cache[CAM_ID]["detectGlassBreak"] is True
    assert CAM_ID in coord.audio_detection_set_at  # write-lock stamped


# ── privacy guard: write blocked, no PUT, no cache change ─────────────────
async def test_privacy_on_blocks_write():
    coord = _coord_audio(
        {"detectGlassBreak": False, "detectFireAlarm": False}, privacy_on=True
    )
    sw = _make(GLASS, coord)
    await sw.async_turn_on()
    coord.async_put_camera.assert_not_awaited()
    assert coord.audio_detection_cache[CAM_ID]["detectGlassBreak"] is False


# ── empty cache: write is a no-op (can't preserve unknown fields) ─────────
async def test_no_write_when_cache_empty():
    coord = _coord_audio(None)
    sw = _make(FIRE, coord)
    await sw.async_turn_on()
    coord.async_put_camera.assert_not_awaited()


# ── concurrent sibling toggles must not clobber ────────────
async def test_concurrent_glass_and_fire_no_clobber():
    """audioDetectionConfig requires BOTH fields in every PUT, so a scene
    toggling glass-break and fire-alarm at once used to have the later write
    re-send the other field's pre-toggle value — reverting it in cache AND
    on the camera. The per-camera lock + merge-only-own-field must make the
    later write read the sibling's committed result and preserve both."""
    coord = _coord_audio({"detectGlassBreak": False, "detectFireAlarm": False})
    put_bodies: list[dict] = []

    async def _slow_put(cid, ep, body):
        # Yield so the sibling task starts and blocks on the lock before we
        # commit — exactly the interleaving that used to clobber.
        await asyncio.sleep(0)
        put_bodies.append(dict(body))
        return True

    coord.async_put_camera = AsyncMock(side_effect=_slow_put)
    glass = _make(GLASS, coord)
    fire = _make(FIRE, coord)

    await asyncio.gather(glass.async_turn_on(), fire.async_turn_on())

    cache = coord.audio_detection_cache[CAM_ID]
    # Neither toggle reverted the other in the cache.
    assert cache["detectGlassBreak"] is True
    assert cache["detectFireAlarm"] is True
    # The second (serialized) PUT body carried the first write's committed value,
    # so it did not revert the sibling on the camera either.
    assert put_bodies[-1]["detectGlassBreak"] is True
    assert put_bodies[-1]["detectFireAlarm"] is True


@pytest.mark.asyncio
async def test_turn_on_sets_intrusion_write_lock(
    stub_entry_gen2: SimpleNamespace,
) -> None:
    """After turn_on, intrusion_config_set_at[cam_id] must be set — otherwise
    the next slow-tier poll (300s) overwrites the cache with the stale cloud
    value."""
    from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch

    coord = _make_coord_intrusion()
    entity = BoschIntrusionDetectionSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    entity.async_write_ha_state = MagicMock()

    before = time.monotonic()
    await entity.async_turn_on()
    after = time.monotonic()

    assert coord.intrusion_config_cache[CAM_ID_GEN2]["enabled"] is True
    assert CAM_ID_GEN2 in coord.intrusion_config_set_at, (
        "write-lock timestamp must be set after a successful PUT"
    )
    ts = coord.intrusion_config_set_at[CAM_ID_GEN2]
    assert before <= ts <= after


@pytest.mark.asyncio
async def test_failed_put_does_not_set_write_lock(
    stub_entry_gen2: SimpleNamespace,
) -> None:
    """If the PUT fails: neither cache nor write-lock may be touched."""
    from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch

    coord = _make_coord_intrusion()
    coord.async_put_camera = AsyncMock(return_value=False)
    entity = BoschIntrusionDetectionSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coord.intrusion_config_cache[CAM_ID_GEN2]["enabled"] is False, (
        "cache must stay unchanged on PUT failure"
    )
    assert CAM_ID_GEN2 not in coord.intrusion_config_set_at, (
        "no write-lock on PUT failure"
    )


def test_coordinator_has_intrusion_set_at_attribute() -> None:
    """Smoke: the write-lock dict `intrusion_config_set_at` must exist on
    coordinator init. If this attribute is removed, the intrusion switch
    raises AttributeError on its first turn_on."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    # Constructor smoke without mocking, via class-attribute introspection.
    init_src = BoschCameraCoordinator.__init__.__code__
    # Walk co_consts looking for the attribute name string literal.
    # If the attribute is removed, this test fails loudly.
    co_names = set(init_src.co_names)
    assert "intrusion_config_set_at" in co_names, (
        "BoschCameraCoordinator.__init__ must not drop `intrusion_config_set_at` "
        "— the intrusion switch would break on turn_on"
    )


def _make_coord_intrusion() -> SimpleNamespace:
    cfg = {
        "enabled": False,
        "sensitivity": 3,
        "detectionMode": "ZONES",
        "distance": 5,
    }
    coord = SimpleNamespace(
        data={
            CAM_ID_GEN2: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": "HOME_Eyes_Indoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                },
            }
        },
        intrusion_config_cache={CAM_ID_GEN2: dict(cfg)},
        intrusion_config_set_at={},
        motion_set_at={},
        alarm_settings_set_at={},
        shc_state_cache={CAM_ID_GEN2: {"privacy_mode": False}},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )
    return coord


@pytest.fixture
def coord_with_helpers() -> tuple[SimpleNamespace, Callable[..., bool]]:
    """Build a coordinator-shaped stub with the real `is_write_locked` method bound."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    coord = SimpleNamespace(
        privacy_sound_set_at={},
        timestamp_set_at={},
        ledlights_set_at={},
        arming_set_at={},
        privacy_set_at={},
        light_set_at={},
        notif_set_at={},
        WRITE_LOCK_SECS=30.0,
    )
    return coord, BoschCameraCoordinator.is_write_locked


# ── is_write_locked helper itself ──────────────────────────────────────


class TestIsWriteLocked:
    def test_no_entry_returns_false(
        self, coord_with_helpers: tuple[SimpleNamespace, Callable[..., bool]]
    ):
        coord, helper = coord_with_helpers
        assert helper(coord, CAM_ID, coord.privacy_sound_set_at) is False

    def test_fresh_write_returns_true(
        self, coord_with_helpers: tuple[SimpleNamespace, Callable[..., bool]]
    ):
        coord, helper = coord_with_helpers
        coord.privacy_sound_set_at[CAM_ID] = time.monotonic()
        assert helper(coord, CAM_ID, coord.privacy_sound_set_at) is True

    def test_old_write_returns_false(
        self, coord_with_helpers: tuple[SimpleNamespace, Callable[..., bool]]
    ):
        coord, helper = coord_with_helpers
        coord.privacy_sound_set_at[CAM_ID] = time.monotonic() - 60.0
        assert helper(coord, CAM_ID, coord.privacy_sound_set_at) is False

    def test_lock_window_boundary(
        self, coord_with_helpers: tuple[SimpleNamespace, Callable[..., bool]]
    ):
        """At exactly TTL seconds, lock has expired."""
        coord, helper = coord_with_helpers
        coord.privacy_sound_set_at[CAM_ID] = time.monotonic() - 30.0
        assert helper(coord, CAM_ID, coord.privacy_sound_set_at) is False

    def test_lock_works_on_other_field_dicts(
        self, coord_with_helpers: tuple[SimpleNamespace, Callable[..., bool]]
    ):
        """Helper is generic — works for every set_at dict."""
        coord, helper = coord_with_helpers
        coord.arming_set_at[CAM_ID] = time.monotonic()
        coord.timestamp_set_at[CAM_ID] = time.monotonic()
        assert helper(coord, CAM_ID, coord.arming_set_at) is True
        assert helper(coord, CAM_ID, coord.timestamp_set_at) is True


# ── switch turn_on/turn_off records timestamps ──────────────────────────


def _switch_stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"}
            }
        },
        privacy_sound_cache={},
        timestamp_cache={},
        ledlights_cache={},
        arming_cache={},
        privacy_sound_set_at={},
        timestamp_set_at={},
        ledlights_set_at={},
        arming_set_at={},
        last_update_success=True,
        async_put_camera=None,  # patched in test
    )


class TestPrivacySoundSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        """User toggles privacy_sound ON → cache + set_at populated together."""
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

        sw = BoschPrivacySoundSwitch(coord, CAM_ID, entry)
        # Patch async_write_ha_state since we're not in HA context
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert coord.privacy_sound_cache[CAM_ID] is True
        assert CAM_ID in coord.privacy_sound_set_at
        # Timestamp must be recent (within last second)
        assert time.monotonic() - coord.privacy_sound_set_at[CAM_ID] < 1.0


class TestTimestampSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        sw = BoschTimestampSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert CAM_ID in coord.timestamp_set_at


class TestStatusLedSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        sw = BoschStatusLedSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert CAM_ID in coord.ledlights_set_at


class TestArmingSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        """User arms the alarm system → cache + set_at populated."""
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        sw = BoschAlarmSystemArmSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert coord.arming_cache[CAM_ID] is True
        assert CAM_ID in coord.arming_set_at

    @pytest.mark.asyncio
    async def test_failed_put_does_not_record(self):
        """If the cloud PUT fails, neither the cache nor the timestamp must change."""
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=False)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        sw = BoschAlarmSystemArmSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert CAM_ID not in coord.arming_cache
        assert CAM_ID not in coord.arming_set_at


def _make_setup_inputs(*, enable_intercom: bool, registry_has_intercom: bool):
    """Build minimal HASS, config_entry, coordinator, async_add_entities."""
    hass = MagicMock()
    config_entry = MagicMock()
    config_entry.options = {
        "enable_intercom": enable_intercom,
        "enable_snapshot_button": True,
    }
    coordinator = MagicMock()
    coordinator.data = {
        CAM_ID_INTERCOM: {
            "info": {
                "title": "Testcam",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.102",
                "macAddress": "AA:BB:CC:00:00:01",
                "featureSupport": {"light": False, "panLimit": 0},
            }
        }
    }
    config_entry.runtime_data = coordinator

    # Fake entity registry: returns an entity_id iff registry has the intercom.
    ent_reg = MagicMock()
    if registry_has_intercom:
        ent_reg.async_get_entity_id.return_value = "switch.testcam_intercom"
    else:
        ent_reg.async_get_entity_id.return_value = None

    added: list = []

    def _async_add_entities(ents, *args, **kwargs):
        added.extend(ents)

    return hass, config_entry, ent_reg, _async_add_entities, added


def _intercom_count(entities) -> int:
    return sum(1 for e in entities if isinstance(e, BoschIntercomSwitch))


class TestIntercomOptionGate:
    """Pins the `enable_intercom` options-flow gate: registration is gated on
    `opts.get("enable_intercom", False)` OR a legacy entity-registry entry (to
    preserve installs that opted in via the UI before the gate existed)."""

    @pytest.mark.asyncio
    async def test_option_true_no_legacy_registers(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=True,
            registry_has_intercom=False,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        assert _intercom_count(added) == 1

    @pytest.mark.asyncio
    async def test_option_true_with_legacy_registers_once(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=True,
            registry_has_intercom=True,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        # Both gates true → still one entity (no duplicate registration).
        assert _intercom_count(added) == 1

    @pytest.mark.asyncio
    async def test_option_false_with_legacy_registers(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=False,
            registry_has_intercom=True,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        # Legacy users keep their entity even with option=False.
        assert _intercom_count(added) == 1

    @pytest.mark.asyncio
    async def test_option_false_no_legacy_skips(self) -> None:
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=False,
            registry_has_intercom=False,
        )
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        # Default state: no entity at all.
        assert _intercom_count(added) == 0

    @pytest.mark.asyncio
    async def test_option_missing_default_skips(self) -> None:
        """Option key absent (older install before key was introduced) →
        defaults to False → no entity (unless legacy registry has it)."""
        hass, ce, ent_reg, add_fn, added = _make_setup_inputs(
            enable_intercom=False,
            registry_has_intercom=False,
        )
        ce.options = {"enable_snapshot_button": True}  # intercom key missing
        with patch(
            "custom_components.bosch_shc_camera.switch.er.async_get",
            return_value=ent_reg,
        ):
            await async_setup_entry(hass, ce, add_fn)
        assert _intercom_count(added) == 0

    def test_class_no_longer_hides_by_default(self) -> None:
        """The `_attr_entity_registry_enabled_default = False` was dropped so
        a fresh opt-in makes the entity immediately visible. If you set this
        back to False, the option toggle becomes confusing (user enables it,
        nothing shows up until they also enable it in the entity registry)."""
        # Check the class's own __dict__ — not inherited attrs from
        # SwitchEntity (which uses a @property descriptor).
        own = BoschIntercomSwitch.__dict__.get("_attr_entity_registry_enabled_default")
        assert own is not False, (
            "BoschIntercomSwitch._attr_entity_registry_enabled_default must not "
            "be set to False on the class — the option toggle now controls visibility."
        )


@pytest.fixture
def stub_entry_gen2() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _make_coord_panic() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID_GEN2: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": "HOME_Eyes_Indoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                },
            }
        },
        panic_alarm_cache={},
        shc_state_cache={},  # required by _warn_if_privacy_on (privacy_mode defaults to False)
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_turn_on_sends_status_on(stub_entry_gen2: SimpleNamespace) -> None:
    """PUT /v11/video_inputs/{id}/panic_alarm {"status":"ON"} — pins the exact
    body Bosch expects (Gen2 siren trigger; the earlier /acoustic_alarm
    endpoint only exists on CAMERA_360 Gen1, which has no integrated siren)."""
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord_panic()
    entity = BoschPanicAlarmSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    coord.async_put_camera.assert_called_once()
    args = coord.async_put_camera.call_args.args
    assert args[1] == "panic_alarm", (
        "Endpoint must be /panic_alarm (not /acoustic_alarm)"
    )
    assert args[2] == {"status": "ON"}, 'Body must be {"status":"ON"}'
    assert coord.panic_alarm_cache[CAM_ID_GEN2] is True


@pytest.mark.asyncio
async def test_turn_off_sends_status_off(stub_entry_gen2: SimpleNamespace) -> None:
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord_panic()
    coord.panic_alarm_cache[CAM_ID_GEN2] = True
    entity = BoschPanicAlarmSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_off()

    args = coord.async_put_camera.call_args.args
    assert args[1] == "panic_alarm"
    assert args[2] == {"status": "OFF"}
    assert coord.panic_alarm_cache[CAM_ID_GEN2] is False


@pytest.mark.asyncio
async def test_is_on_tracks_local_cache(stub_entry_gen2: SimpleNamespace) -> None:
    """No GET endpoint exists — track last-sent state locally."""
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord_panic()
    entity = BoschPanicAlarmSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    assert entity.is_on is False

    coord.panic_alarm_cache[CAM_ID_GEN2] = True
    assert entity.is_on is True


@pytest.mark.asyncio
async def test_failed_put_does_not_set_cache_panic(
    stub_entry_gen2: SimpleNamespace,
) -> None:
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord_panic()
    coord.async_put_camera = AsyncMock(return_value=False)
    entity = BoschPanicAlarmSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert (
        coord.panic_alarm_cache.get(CAM_ID_GEN2) is None
        or coord.panic_alarm_cache[CAM_ID_GEN2] is False
    ), (
        "on PUT failure the cache must not read as True (switch would show ON with no effect)"
    )


def test_disabled_by_default(stub_entry_gen2: SimpleNamespace) -> None:
    """Panic-alarm switch defaults to disabled (75dB is loud, not something
    for the default dashboard). User must explicitly enable it."""
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord_panic()
    entity = BoschPanicAlarmSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    assert entity._attr_entity_registry_enabled_default is False


@pytest.mark.asyncio
async def test_lazy_init_creates_cache_on_legacy_coordinator(
    stub_entry_gen2: SimpleNamespace,
) -> None:
    """When a coordinator from an older build doesn't carry
    `panic_alarm_cache`, `_set()` lazy-inits it so the very first toggle
    works. Pins switch.py's lazy-init guard."""
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord_panic()
    # Simulate a pre-v10.x coordinator without the cache attribute.
    del coord.panic_alarm_cache
    assert not hasattr(coord, "panic_alarm_cache")
    entity = BoschPanicAlarmSwitch(coord, CAM_ID_GEN2, stub_entry_gen2)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    # Lazy-init created the dict and stored the new state.
    assert hasattr(coord, "panic_alarm_cache")
    assert coord.panic_alarm_cache[CAM_ID_GEN2] is True


# Live-stream user-intent switch.
#
# The coordinator tracks user intent in `user_intent_streams: set[str]`,
# decoupled from `live_connections` (which Cast/dashboard/`camera.play_stream`
# also populate via `async_create_stream` without the user ever toggling the
# switch). `BoschLiveStreamSwitch.is_on` reads intent, not `live_connections`.
# `async_turn_on` sets intent before connecting and reverts it on failure;
# `async_turn_off` and `tear_down_live_stream` both clear it; the health
# watchdog re-checks intent after its sleep before reconnecting.


class TestAsyncCreateStreamDoesNotFlipSwitch:
    """`async_create_stream` (HA Core) → `stream_source()` → `try_live_connection()`
    populates `live_connections`. The switch must NOT show "on" unless the
    user explicitly toggled it.
    """

    def test_is_on_false_when_only_live_connections_populated(self):
        """The Cast / dashboard / play_stream path populates `live_connections`
        but not `user_intent_streams`. Switch reads intent → off."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            live_connections={CAM_ID: {"rtspsUrl": "rtsps://x"}},
            user_intent_streams=set(),  # user did NOT toggle
        )
        stub = SimpleNamespace(_cam_id=CAM_ID, coordinator=coord)
        assert BoschLiveStreamSwitch.is_on.fget(stub) is False, (
            "REGRESSION: Switch shows on for auto-opened sessions. "
            "Dashboard / Cast / play_stream populates `live_connections` "
            "but should not flip the user-facing switch."
        )

    def test_is_on_true_when_user_intent_set(self):
        """Once `async_turn_on` runs, `user_intent_streams` is populated
        and the switch reads true regardless of `live_connections`."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            live_connections={},
            user_intent_streams={CAM_ID},
        )
        stub = SimpleNamespace(_cam_id=CAM_ID, coordinator=coord)
        assert BoschLiveStreamSwitch.is_on.fget(stub) is True


class TestHealthWatchdogIntentCheck:
    """The watchdog must NOT re-open the stream after a user OFF during
    the 60s sleep. The new guard checks `user_intent_streams` after the
    tear-down before calling `try_live_connection`."""

    @pytest.mark.asyncio
    async def test_watchdog_skips_reconnect_when_intent_gone(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Pin: if user toggled OFF during sleep, watchdog bails without re-opening."""
        # Mock sleep so the test runs instantly
        import asyncio as _asyncio

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        async def _no_sleep(*_a, **_kw):
            pass

        monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

        cam_entity = SimpleNamespace(
            stream=SimpleNamespace(
                available=False
            ),  # unhealthy → would normally trigger reconnect
        )

        try_live = AsyncMock()
        coord = SimpleNamespace(
            live_connections={
                CAM_ID: {"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"}
            },
            user_intent_streams={CAM_ID},  # user initially toggled on
            camera_entities={CAM_ID: cam_entity},
            stream_error_count={},
            stop_tls_proxy=AsyncMock(),
            stop_viewing_front_door=AsyncMock(),
            stop_remote_viewing_front_door=AsyncMock(),
            try_live_connection=try_live,
            record_stream_error=MagicMock(),
            record_stream_success=MagicMock(),
            get_model_config=MagicMock(
                return_value=SimpleNamespace(max_stream_errors=3)
            ),
        )

        # Simulate that the user toggled OFF between schedule and fire: the
        # OFF handler clears intent + live_connections by the time the
        # watchdog wakes up and reaches the reconnect step.
        async def _stop_then_clear(*_a, **_kw):
            coord.user_intent_streams.discard(CAM_ID)

        coord.stop_tls_proxy = _stop_then_clear

        switch_stub = SimpleNamespace(
            coordinator=coord,
            async_write_ha_state=MagicMock(),
            _cam_title="Terrasse",
        )

        await BoschLiveStreamSwitch._stream_health_watchdog(switch_stub, CAM_ID)

        (
            try_live.assert_not_called(),
            (
                "REGRESSION: Watchdog called try_live_connection after the user "
                "toggled OFF. The intent check between stop_tls_proxy and "
                "try_live_connection is broken."
            ),
        )

    @pytest.mark.asyncio
    async def test_watchdog_reconnects_when_intent_still_present(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Sanity: if intent is still True, watchdog DOES reconnect."""
        import asyncio as _asyncio

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        async def _no_sleep(*_a, **_kw):
            pass

        monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

        cam_entity = SimpleNamespace(stream=SimpleNamespace(available=False))

        try_live = AsyncMock(return_value={"_connection_type": "REMOTE"})
        coord = SimpleNamespace(
            live_connections={
                CAM_ID: {"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"}
            },
            user_intent_streams={CAM_ID},
            camera_entities={CAM_ID: cam_entity},
            stream_error_count={},
            stop_tls_proxy=AsyncMock(),
            stop_viewing_front_door=AsyncMock(),
            stop_remote_viewing_front_door=AsyncMock(),
            try_live_connection=try_live,
            record_stream_error=MagicMock(),
            record_stream_success=MagicMock(),
            get_model_config=MagicMock(
                return_value=SimpleNamespace(max_stream_errors=3)
            ),
        )

        switch_stub = SimpleNamespace(
            coordinator=coord,
            async_write_ha_state=MagicMock(),
            _cam_title="Terrasse",
        )

        await BoschLiveStreamSwitch._stream_health_watchdog(switch_stub, CAM_ID)

        try_live.assert_called_once_with(CAM_ID)


class TestTeardownClearsIntent:
    """External teardowns (privacy on, health-watchdog REMOTE escalation,
    NVR restart) genuinely end user intent. `tear_down_live_stream` must
    discard cam_id from `user_intent_streams`."""

    @pytest.mark.asyncio
    async def test_teardown_clears_intent(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = SimpleNamespace(
            _stream_locks={},
            live_connections={CAM_ID: {"rtspsUrl": "rtsps://x"}},
            user_intent_streams={CAM_ID},
            live_opened_at={CAM_ID: 100.0},
            stream_error_count={},
            stream_error_at={},
            stream_fell_back={},
            local_rescue_attempts={},
            local_rescue_at={},
            stream_warming={CAM_ID},
            _sessions={CAM_ID: CameraSessionState(warming_started=100.0)},
            renewal_tasks={},
            reaper_tasks={},
            camera_entities={},
            live_stream_entities={},
            stop_tls_proxy=AsyncMock(),
            stop_viewing_front_door=AsyncMock(),
            stop_remote_viewing_front_door=AsyncMock(),
            unregister_go2rtc_stream=AsyncMock(),
            nvr_processes={},
            nvr_user_intent={},
            stop_recorder=AsyncMock(),
        )
        coord.get_stream_lock = lambda cam_id: coord._stream_locks.setdefault(
            cam_id, asyncio.Lock()
        )
        coord.get_session = lambda cam_id: get_or_create_session(
            coord._sessions, cam_id
        )

        await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)

        assert CAM_ID not in coord.user_intent_streams, (
            "REGRESSION: tear_down_live_stream did not clear user intent. "
            "Privacy ON / health escalation should reset the switch state."
        )


class TestTurnOnFailureRevertsIntent:
    """`async_turn_on` sets intent BEFORE attempting the connection. If
    `try_live_connection` returns None (failure), intent must be reverted
    so the switch doesn't get stuck on 'on' with a dead session."""

    @pytest.mark.asyncio
    async def test_intent_reverted_on_try_live_connection_failure(self):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = SimpleNamespace(
            shc_state_cache={},
            user_intent_streams=set(),
            try_live_connection=AsyncMock(return_value=None),  # failure
            record_stream_error=MagicMock(),
            bg_tasks=set(),
        )
        switch_stub = SimpleNamespace(
            coordinator=coord,
            _cam_id=CAM_ID,
            _cam_title="Terrasse",
            _last_stream_off=float("-inf"),  # never stopped (SENTINEL_RULE)
            _STREAM_COOLDOWN=BoschLiveStreamSwitch._STREAM_COOLDOWN,
            async_write_ha_state=MagicMock(),
            hass=MagicMock(),
        )

        await BoschLiveStreamSwitch.async_turn_on(switch_stub)

        assert CAM_ID not in coord.user_intent_streams, (
            "REGRESSION: failed turn_on left user_intent_streams populated. "
            "The switch would report 'on' against a non-existent session."
        )


# Privacy switch: live-stream teardown interaction.
#
# When privacy is turned off, the live-stream switch must not stay stuck on
# "on" even though the underlying RTSP session is dead. Two contracts are
# pinned: (1) `live_connections` is popped before anything that can raise
# (e.g. `stop_recorder`), so the switch reads the correct state even when
# downstream cleanup fails; (2) the live-stream switch entity has
# `async_write_ha_state()` called on it immediately after teardown instead of
# waiting for the next coordinator refresh tick.


def _make_coord_privacy_livestream(stream_obj=None, *, with_ls_entity: bool = False):
    """Coordinator stub with everything `tear_down_live_stream` touches.

    Also seeds `nvr_processes` (so the stop_recorder branch is exercised)
    and optionally seeds `live_stream_entities` with a MagicMock entity
    that records calls to `async_write_ha_state()`.
    """
    cam_entity = SimpleNamespace(stream=stream_obj)
    coord = SimpleNamespace(
        _stream_locks={},
        live_connections={CAM_ID: {"rtspsUrl": "rtsps://x"}},
        user_intent_streams={CAM_ID},  # user-intent tracking
        live_opened_at={CAM_ID: 100.0},
        stream_error_count={CAM_ID: 2},
        stream_error_at={CAM_ID: 100.0},
        stream_fell_back={CAM_ID: True},
        local_rescue_attempts={CAM_ID: 1},
        local_rescue_at={CAM_ID: 100.0},
        stream_warming={CAM_ID},
        _sessions={CAM_ID: CameraSessionState(warming_started=100.0)},
        renewal_tasks={},
        reaper_tasks={},
        camera_entities={CAM_ID: cam_entity},
        live_stream_entities={},
        stop_tls_proxy=AsyncMock(),
        stop_viewing_front_door=AsyncMock(),
        stop_remote_viewing_front_door=AsyncMock(),
        unregister_go2rtc_stream=AsyncMock(),
        nvr_processes={CAM_ID: object()},  # NVR is running for this cam
        nvr_user_intent={CAM_ID: True},
        stop_recorder=AsyncMock(),
    )
    coord.get_stream_lock = lambda cam_id: coord._stream_locks.setdefault(
        cam_id, asyncio.Lock()
    )
    coord.get_session = lambda cam_id: get_or_create_session(coord._sessions, cam_id)
    if with_ls_entity:
        ls_entity = MagicMock()
        ls_entity.hass = object()  # truthy, simulates "added to hass"
        ls_entity.async_write_ha_state = MagicMock()
        coord.live_stream_entities[CAM_ID] = ls_entity
        return coord, cam_entity, ls_entity
    return coord, cam_entity, None


class TestTeardownResilience:
    """`live_connections` MUST be cleared before any operation that can fail,
    so `BoschLiveStreamSwitch.is_on` flips to False even when downstream
    cleanup hits an exception."""

    @pytest.mark.asyncio
    async def test_live_connections_cleared_before_stop_recorder(self):
        """Pin the call ORDER: pop happens before stop_recorder is awaited."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord_privacy_livestream()
        observations: list[bool] = []

        async def _stop_recorder_check(*args, **kwargs):
            observations.append(CAM_ID not in coord.live_connections)

        coord.stop_recorder = _stop_recorder_check

        await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)
        assert observations == [True], (
            "REGRESSION: stop_recorder ran while live_connections still had "
            "the cam entry. The pop must happen FIRST so the switch state is "
            "correct even when NVR teardown fails. Observation: "
            f"{observations}"
        )

    @pytest.mark.asyncio
    async def test_live_connections_cleared_even_if_stop_recorder_raises(self):
        """The pop must survive a stop_recorder OSError."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord_privacy_livestream()
        coord.stop_recorder = AsyncMock(side_effect=OSError("ffmpeg child gone"))

        # The teardown is allowed to swallow the OSError; what matters is the
        # state-dict invariant. If it re-raises, the test still passes as long
        # as live_connections is empty afterwards.
        try:
            await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)
        except OSError:
            pass

        assert CAM_ID not in coord.live_connections, (
            "REGRESSION: stop_recorder raised and live_connections still has "
            "the cam entry — BoschLiveStreamSwitch.is_on would stay True forever."
        )

    @pytest.mark.asyncio
    async def test_stop_recorder_exception_does_not_skip_proxy_cleanup(self):
        """A failed NVR stop must not leave the TLS proxy / go2rtc dangling."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord_privacy_livestream()
        coord.stop_recorder = AsyncMock(side_effect=RuntimeError("simulated"))

        try:
            await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)
        except RuntimeError:
            pass

        coord.stop_tls_proxy.assert_called_once_with(CAM_ID)
        coord.unregister_go2rtc_stream.assert_called_once_with(CAM_ID)


class TestTeardownStateWrite:
    """`async_write_ha_state()` must fire on the live-stream switch
    immediately after teardown so the UI does not show stale "on"."""

    @pytest.mark.asyncio
    async def test_state_write_fires_after_teardown(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, ls_entity = _make_coord_privacy_livestream(with_ls_entity=True)
        await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)

        assert ls_entity.async_write_ha_state.called, (
            "REGRESSION: live-stream switch entity was registered but "
            "tear_down_live_stream did not push its new state. UI will stay "
            "stale until next coordinator refresh tick."
        )

    @pytest.mark.asyncio
    async def test_state_write_skipped_when_no_entity_registered(self):
        """No KeyError / AttributeError if no entity is registered for cam."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, _ = _make_coord_privacy_livestream(with_ls_entity=False)
        await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)
        # No assertion — the contract is "no exception".

    @pytest.mark.asyncio
    async def test_state_write_skipped_when_entity_not_yet_added_to_hass(self):
        """Don't call async_write_ha_state on an entity with hass=None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord, _, ls_entity = _make_coord_privacy_livestream(with_ls_entity=True)
        ls_entity.hass = None  # entity registered but not yet added to hass
        await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)

        assert not ls_entity.async_write_ha_state.called, (
            "Calling async_write_ha_state on an entity whose hass is None "
            "raises in HA core. Teardown must guard."
        )


class TestSwitchIsOnContract:
    """`BoschLiveStreamSwitch.is_on` reads `live_connections`. After
    teardown, is_on must return False (not True), even if NVR teardown
    itself raised."""

    @pytest.mark.asyncio
    async def test_is_on_false_after_teardown_with_nvr_failure(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord, _, _ = _make_coord_privacy_livestream()
        coord.stop_recorder = AsyncMock(side_effect=Exception("NVR died"))

        try:
            await BoschCameraCoordinator.tear_down_live_stream(coord, CAM_ID)
        except Exception:
            pass

        # Synthetic switch instance — we only need is_on() to read coordinator
        # state correctly.
        switch_stub = SimpleNamespace(_cam_id=CAM_ID, coordinator=coord)
        assert BoschLiveStreamSwitch.is_on.fget(switch_stub) is False, (
            "User-visible regression: BoschLiveStreamSwitch.is_on returns True "
            "even though the stream is dead."
        )


# Privacy switch: offline-mode availability contract.
#
# Availability semantics for the privacy switch, including a cold-start
# fallback: cloud healthy + cached state → available (primary). Cloud down +
# cached state + Gen2 + LAN reachable → available (fallback). Cloud down + NO
# cached state + Gen2 + LAN reachable → available (cold-start case: no
# coordinator tick has succeeded yet, but the LAN RCP-write path still works).
# Cloud down + Gen1 → unavailable (no RCP-write path on Gen1). Cloud down +
# LAN unreachable → unavailable.


def _make_coord_privacy_offline(
    *,
    last_update_success: bool = True,
    has_cached_state: bool = True,
    is_lan_reachable_value=None,
    gen2: bool = True,
) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.last_update_success = last_update_success
    coord.shc_state_cache = (
        {CAM_ID: {"privacy_mode": False}} if has_cached_state else {CAM_ID: {}}
    )
    coord.rcp_privacy_cache = {}
    coord.data = {
        CAM_ID: {
            "info": {
                "title": "Terrasse",
                "hardwareVersion": "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR",
            }
        }
    }
    coord.hw_version = {CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"}
    if is_lan_reachable_value is None:
        # No helper attached at all — simulates a stub predating this helper.
        pass
    else:
        coord.is_lan_reachable = lambda _cid, _v=is_lan_reachable_value: _v
    return coord


def _make_switch_privacy_offline(coord) -> object:
    from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

    entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
    return BoschPrivacyModeSwitch(coord, CAM_ID, entry)


class TestPrivacySwitchOfflineMode:
    def test_cloud_healthy_with_state_is_available(self):
        coord = _make_coord_privacy_offline(
            last_update_success=True, has_cached_state=True
        )
        s = _make_switch_privacy_offline(coord)
        assert s.available is True

    def test_cloud_healthy_without_state_is_unavailable(self):
        """Before a single coordinator tick succeeded, we have no signal at all."""
        coord = _make_coord_privacy_offline(
            last_update_success=True, has_cached_state=False
        )
        s = _make_switch_privacy_offline(coord)
        assert s.available is False

    def test_cloud_down_cached_state_gen2_lan_reachable_is_available(self):
        """The cloud-down fallback path — cached state + Gen2 + reachable LAN."""
        coord = _make_coord_privacy_offline(
            last_update_success=False,
            has_cached_state=True,
            is_lan_reachable_value=True,
            gen2=True,
        )
        s = _make_switch_privacy_offline(coord)
        assert s.available is True

    def test_cloud_down_no_cached_state_gen2_lan_reachable_is_available(self):
        """Cold-start during a cloud outage with no cached state but a
        reachable LAN must keep the switch toggleable. is_on returns None
        (HA renders 'unknown'), but the user can still flip it."""
        coord = _make_coord_privacy_offline(
            last_update_success=False,
            has_cached_state=False,
            is_lan_reachable_value=True,
            gen2=True,
        )
        s = _make_switch_privacy_offline(coord)
        assert s.available is True
        # is_on returns None in this state (no cached value, no live data).
        assert s.is_on is None

    def test_cloud_down_gen1_is_unavailable(self):
        """Gen1 cameras have no RCP-write path → fallback is N/A."""
        coord = _make_coord_privacy_offline(
            last_update_success=False,
            has_cached_state=False,
            is_lan_reachable_value=True,
            gen2=False,
        )
        s = _make_switch_privacy_offline(coord)
        assert s.available is False

    def test_cloud_down_lan_unreachable_is_unavailable(self):
        coord = _make_coord_privacy_offline(
            last_update_success=False,
            has_cached_state=True,
            is_lan_reachable_value=False,
            gen2=True,
        )
        s = _make_switch_privacy_offline(coord)
        assert s.available is False

    def test_cloud_down_lan_unknown_is_unavailable(self):
        """is_lan_reachable returning None (no ping yet) — treat as unavailable."""
        coord = _make_coord_privacy_offline(
            last_update_success=False,
            has_cached_state=True,
            is_lan_reachable_value=None,
            gen2=True,
        )
        # Force the helper to be present but return None.
        coord.is_lan_reachable = lambda _cid: None
        s = _make_switch_privacy_offline(coord)
        assert s.available is False

    def test_no_is_lan_reachable_helper_returns_false(self):
        """Stub coordinators without an is_lan_reachable method must not
        crash — return False instead."""
        coord = _make_coord_privacy_offline(
            last_update_success=False,
            has_cached_state=True,
            gen2=True,
        )
        # No is_lan_reachable attached at all.
        s = _make_switch_privacy_offline(coord)
        assert s.available is False


# Privacy / camera-light write-failure notification.
#
# When every write path (cloud, Gen2 LOCAL RCP, SHC) is exhausted, the user
# must always get a persistent_notification, and the switch must log a
# WARNING instead of silently discarding the failure. The notification_id is
# deterministic per camera so repeated failures overwrite the same entry
# rather than spamming.


def _coord_privacy_write_failure(**overrides: object) -> SimpleNamespace:
    coord = SimpleNamespace(
        token="tok-AAA",
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        shc_state_cache={CAM_ID: {}},
        privacy_set_at={},
        local_creds_cache={},
        rcp_lan_ip_cache={},
        hw_version={},
        cached_status={},
        auth_outage_count=0,  # no consecutive polling 5xx recorded
        async_update_listeners=MagicMock(),
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _put_raises_session() -> MagicMock:
    """aiohttp session whose .put(...) raises immediately (connect failure)."""
    session = MagicMock()
    session.put = MagicMock(
        side_effect=aiohttp.ClientConnectorError(
            connection_key=MagicMock(), os_error=OSError("Connect call failed")
        )
    )
    return session


class TestAllPathsFailedStillNotifies:
    @pytest.mark.asyncio
    async def test_notification_fires_with_zero_auth_outage_count(self):
        """Cloud fails, no cached LOCAL RCP host, SHC not ready → all three
        write paths exhausted. `auth_outage_count` is still 0 (a single
        ad-hoc failure, not a run of polling 5xxs) — the notification must
        fire anyway.
        """
        from custom_components.bosch_shc_camera import shc

        coord = _coord_privacy_write_failure()
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_raises_session()),
            ),
            patch.object(shc, "shc_ready", return_value=False),
        ):
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

        assert ok is False
        coord.hass.services.async_call.assert_awaited_once()
        args, _kwargs = coord.hass.services.async_call.call_args
        assert args[0] == "persistent_notification"
        assert args[1] == "create"
        assert args[2]["notification_id"] == f"bosch_privacy_queued_{CAM_ID[:8]}"

    @pytest.mark.asyncio
    async def test_notification_fires_when_shc_reachable_but_its_write_fails(self):
        """The SHC fallback branch used to `return await
        async_shc_set_privacy_mode(...)` directly — if SHC was reachable
        (`shc_ready` True) but its own local write also failed (e.g. no
        cached device_id yet), that `False` was handed straight back to the
        caller, skipping the notification tail entirely. Pins the fix for
        this sub-case.
        """
        from custom_components.bosch_shc_camera import shc

        coord = _coord_privacy_write_failure()
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_raises_session()),
            ),
            patch.object(shc, "shc_ready", return_value=True),
            patch.object(
                shc, "async_shc_set_privacy_mode", new=AsyncMock(return_value=False)
            ),
        ):
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

        assert ok is False
        coord.hass.services.async_call.assert_awaited_once()
        args, _kwargs = coord.hass.services.async_call.call_args
        assert args[0] == "persistent_notification"
        assert args[2]["notification_id"] == f"bosch_privacy_queued_{CAM_ID[:8]}"

    @pytest.mark.asyncio
    async def test_shc_success_returns_true_without_notifying(self):
        """The happy SHC-fallback path must still return True and must NOT
        fire the failure notification (no regression from the fix above)."""
        from custom_components.bosch_shc_camera import shc

        coord = _coord_privacy_write_failure()
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_raises_session()),
            ),
            patch.object(shc, "shc_ready", return_value=True),
            patch.object(
                shc, "async_shc_set_privacy_mode", new=AsyncMock(return_value=True)
            ),
        ):
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

        assert ok is True
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_notification_id_is_stable_across_repeated_failures(self):
        """Repeated failures during a real outage must overwrite the same
        notification, not create a new one each time (no spam)."""
        from custom_components.bosch_shc_camera import shc

        coord = _coord_privacy_write_failure()
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_raises_session()),
            ),
            patch.object(shc, "shc_ready", return_value=False),
        ):
            await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)
            await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

        assert coord.hass.services.async_call.await_count == 2
        ids = {
            call.args[2]["notification_id"]
            for call in coord.hass.services.async_call.await_args_list
        }
        assert ids == {f"bosch_privacy_queued_{CAM_ID[:8]}"}


class TestCameraLightSameShcFallbackBug:
    """Same SHC-fallback-swallows-notification bug, for
    async_cloud_set_camera_light (shares the identical pattern)."""

    @pytest.mark.asyncio
    async def test_notification_fires_when_shc_reachable_but_its_write_fails(self):
        from custom_components.bosch_shc_camera import shc

        coord = _coord_privacy_write_failure()
        coord.hw_version = {CAM_ID: "OUTDOOR"}  # Gen1 path (single PUT)
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_raises_session()),
            ),
            patch.object(shc, "shc_ready", return_value=True),
            patch.object(
                shc, "async_shc_set_camera_light", new=AsyncMock(return_value=False)
            ),
        ):
            ok = await shc.async_cloud_set_camera_light(coord, CAM_ID, True)

        assert ok is False
        coord.hass.services.async_call.assert_awaited_once()
        args, _kwargs = coord.hass.services.async_call.call_args
        assert args[0] == "persistent_notification"
        assert args[2]["notification_id"] == f"bosch_light_queued_{CAM_ID[:8]}"


class TestSwitchLogsOnTotalFailure:
    @pytest.mark.asyncio
    async def test_apply_privacy_logs_warning_when_write_fails(
        self, caplog: pytest.LogCaptureFixture
    ):
        """`_apply_privacy` must not silently discard a False result — pins a
        visible WARNING so the failure isn't only buried in a notification.
        """
        from custom_components.bosch_shc_camera.switch import (
            BoschPrivacyModeSwitch,
        )

        coord = SimpleNamespace(
            live_connections={},
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            async_cloud_set_privacy_mode=AsyncMock(return_value=False),
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        switch = BoschPrivacyModeSwitch(coord, CAM_ID, entry)

        with caplog.at_level(logging.WARNING):
            await switch._apply_privacy(True)

        coord.async_cloud_set_privacy_mode.assert_awaited_once_with(CAM_ID, True)
        assert any("failed on all paths" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_apply_privacy_silent_when_write_succeeds(
        self, caplog: pytest.LogCaptureFixture
    ):
        """No warning noise on the (normal) success path."""
        from custom_components.bosch_shc_camera.switch import (
            BoschPrivacyModeSwitch,
        )

        coord = SimpleNamespace(
            live_connections={},
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            async_cloud_set_privacy_mode=AsyncMock(return_value=True),
        )
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        switch = BoschPrivacyModeSwitch(coord, CAM_ID, entry)

        with caplog.at_level(logging.WARNING):
            await switch._apply_privacy(False)

        assert not any(
            "failed on all paths" in record.message for record in caplog.records
        )


# ── minimal coordinator stub ─────────────────────────────────────────────────


def _coord(
    hw="HOME_Eyes_Outdoor",
    privacy_on=False,
    motion_settings=None,
    recording_options=None,
    autofollow_data=None,
    privacy_sound_cache=None,
    timestamp_cache=None,
    ledlights_cache=None,
    motion_light_cache=None,
    ambient_lighting_cache=None,
    global_lighting_cache=None,
    intrusion_config=None,
    notifications_cache=None,
    alarm_settings_cache=None,
    arming_cache=None,
    image_rotation_180=None,
    nvr_user_intent=None,
    live_connections=None,
    audio_cache=None,
    **kwargs,
):
    shc_state = {CAM_ID: {"privacy_mode": privacy_on}}

    def _ms(cam_id):
        return motion_settings or {}

    def _ros(cam_id):
        return recording_options or {}

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "autofollow": autofollow_data,
            }
        },
        last_update_success=True,
        is_camera_online=lambda cid: True,
        shc_state_cache=shc_state,
        live_connections=live_connections if live_connections is not None else {},
        audio_enabled={},
        audio_cache=audio_cache if audio_cache is not None else {},
        options={},
        privacy_sound_cache=privacy_sound_cache
        if privacy_sound_cache is not None
        else {},
        timestamp_cache=timestamp_cache if timestamp_cache is not None else {},
        timestamp_set_at={},
        ledlights_cache=ledlights_cache if ledlights_cache is not None else {},
        ledlights_set_at={},
        motion_light_cache=motion_light_cache if motion_light_cache is not None else {},
        ambient_lighting_cache=ambient_lighting_cache
        if ambient_lighting_cache is not None
        else {},
        global_lighting_cache=global_lighting_cache
        if global_lighting_cache is not None
        else {},
        intrusion_config_cache=intrusion_config if intrusion_config is not None else {},
        intrusion_config_set_at={},
        motion_set_at={},
        alarm_settings_set_at={},
        notifications_cache=notifications_cache
        if notifications_cache is not None
        else {},
        alarm_settings_cache=alarm_settings_cache
        if alarm_settings_cache is not None
        else {},
        alarm_status_cache={},
        arming_cache=arming_cache if arming_cache is not None else {},
        arming_set_at={},
        image_rotation_180=image_rotation_180 if image_rotation_180 is not None else {},
        nvr_user_intent=nvr_user_intent if nvr_user_intent is not None else {},
        nvr_processes={},
        nvr_preroll_processes={},
        nvr_preroll_tasks={},
        nvr_error_state={},
        privacy_sound_set_at={},
        motion_settings=_ms,
        recording_options=_ros,
        token="test-token",
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
        hass=SimpleNamespace(
            async_create_task=MagicMock(),
            config=SimpleNamespace(time_zone="Europe/Berlin"),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        **kwargs,
    )
    return coord


def _make_hass():
    return SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
        config=SimpleNamespace(time_zone="Europe/Berlin"),
    )


def _entry():
    return SimpleNamespace(
        data={"bearer_token": "tok"},
        options={},
        runtime_data=None,
    )


# ── BoschMotionEnabledSwitch ─────────────────────────────────────────────────


def _make_motion_switch(hw="HOME_Eyes_Outdoor", privacy_on=False, settings=None):
    from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

    coord = _coord(
        hw=hw,
        privacy_on=privacy_on,
        motion_settings=settings
        or {"enabled": True, "motionAlarmConfiguration": "HIGH"},
    )
    sw = BoschMotionEnabledSwitch.__new__(BoschMotionEnabledSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


@pytest.mark.asyncio
async def test_motion_turn_on_normal():
    sw = _make_motion_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[1] == "motion"
    assert args[2]["enabled"] is True


@pytest.mark.asyncio
async def test_motion_turn_off_normal():
    sw = _make_motion_switch()
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["enabled"] is False


@pytest.mark.asyncio
async def test_motion_turn_on_gen2_indoor_privacy_blocked():
    """Gen2 indoor + privacy ON → turn_on is blocked (returns early)."""
    sw = _make_motion_switch(hw="HOME_Eyes_Indoor", privacy_on=True)
    sw.hass.services.async_call = AsyncMock()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_motion_turn_off_gen2_indoor_privacy_blocked():
    sw = _make_motion_switch(hw="HOME_Eyes_Indoor", privacy_on=True)
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_not_awaited()


def test_motion_is_on_from_settings():
    sw = _make_motion_switch(settings={"enabled": True})
    assert sw.is_on is True


def test_motion_is_on_none_when_no_settings():
    # Use a sentinel empty dict directly in coord so motion_settings() returns {}
    from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

    coord = _coord(motion_settings=None)
    # Override so lambda returns empty dict (falsy → is_on returns None)
    coord.motion_settings = lambda cid: {}
    sw = BoschMotionEnabledSwitch.__new__(BoschMotionEnabledSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    assert sw.is_on is None


# ── BoschRecordSoundSwitch ───────────────────────────────────────────────────


def _make_record_sound_switch(recording_options=None):
    from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

    coord = _coord(recording_options=recording_options or {"recordSound": False})
    sw = BoschRecordSoundSwitch.__new__(BoschRecordSoundSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


@pytest.mark.asyncio
async def test_record_sound_turn_on():
    sw = _make_record_sound_switch(recording_options={"recordSound": False})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "recording_options", {"recordSound": True}
    )


@pytest.mark.asyncio
async def test_record_sound_turn_off():
    sw = _make_record_sound_switch(recording_options={"recordSound": True})
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "recording_options", {"recordSound": False}
    )


def test_record_sound_is_on_false():
    sw = _make_record_sound_switch(recording_options={"recordSound": False})
    assert sw.is_on is False


def test_record_sound_is_on_none_no_opts():
    from custom_components.bosch_shc_camera.switch import BoschRecordSoundSwitch

    coord = _coord()
    coord.recording_options = lambda cid: {}  # direct assignment — empty dict is falsy
    sw = BoschRecordSoundSwitch.__new__(BoschRecordSoundSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    assert sw.is_on is None


# ── BoschAutoFollowSwitch ────────────────────────────────────────────────────


def _make_autofollow_switch(autofollow_data=None):
    from custom_components.bosch_shc_camera.switch import BoschAutoFollowSwitch

    coord = _coord(autofollow_data=autofollow_data)
    sw = BoschAutoFollowSwitch.__new__(BoschAutoFollowSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Kamera"
    sw._model_name = "360"
    sw._fw = "7.91.56"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_autofollow_is_on_true():
    sw = _make_autofollow_switch(autofollow_data={"result": True})
    assert sw.is_on is True


def test_autofollow_is_on_false():
    sw = _make_autofollow_switch(autofollow_data={"result": False})
    assert sw.is_on is False


def test_autofollow_is_on_none():
    sw = _make_autofollow_switch(autofollow_data=None)
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_autofollow_turn_on():
    sw = _make_autofollow_switch(autofollow_data={"result": False})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "autofollow", {"result": True}
    )


@pytest.mark.asyncio
async def test_autofollow_turn_off():
    sw = _make_autofollow_switch(autofollow_data={"result": True})
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "autofollow", {"result": False}
    )


# ── BoschIntercomSwitch ──────────────────────────────────────────────────────


def _make_intercom_switch():
    from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

    coord = _coord()
    sw = BoschIntercomSwitch.__new__(BoschIntercomSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._is_on = False
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


@pytest.mark.asyncio
async def test_intercom_turn_on_success():
    sw = _make_intercom_switch()
    await sw.async_turn_on()
    assert sw._is_on is True
    sw.async_write_ha_state.assert_called()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "audio", {"audioEnabled": True, "speakerLevel": 50}
    )


@pytest.mark.asyncio
async def test_intercom_turn_on_put_failure():
    sw = _make_intercom_switch()
    sw.coordinator.async_put_camera = AsyncMock(return_value=False)
    await sw.async_turn_on()
    assert sw._is_on is False  # not set to True on failure


@pytest.mark.asyncio
async def test_intercom_turn_off_success():
    sw = _make_intercom_switch()
    sw._is_on = True
    await sw.async_turn_off()
    assert sw._is_on is False
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "audio", {"audioEnabled": False}
    )


@pytest.mark.asyncio
async def test_intercom_turn_off_put_failure():
    sw = _make_intercom_switch()
    sw._is_on = True
    sw.coordinator.async_put_camera = AsyncMock(return_value=False)
    await sw.async_turn_off()
    # _is_on unchanged when the PUT fails
    assert sw._is_on is True


# ── BoschPrivacySoundSwitch ──────────────────────────────────────────────────


def _make_privacy_sound_switch(privacy_sound_cache=None, cam_online=True):
    from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

    coord = _coord(privacy_sound_cache=privacy_sound_cache or {})
    coord.is_camera_online = lambda cid: cam_online
    sw = BoschPrivacySoundSwitch.__new__(BoschPrivacySoundSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Kamera"
    sw._model_name = "360"
    sw._fw = "7.91.56"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_privacy_sound_is_on_true():
    sw = _make_privacy_sound_switch(privacy_sound_cache={CAM_ID: True})
    assert sw.is_on is True


def test_privacy_sound_is_on_none():
    sw = _make_privacy_sound_switch(privacy_sound_cache={})
    assert sw.is_on is None


def test_privacy_sound_available_true():
    sw = _make_privacy_sound_switch(privacy_sound_cache={CAM_ID: True})
    assert sw.available is True


def test_privacy_sound_available_false_no_cache():
    sw = _make_privacy_sound_switch(privacy_sound_cache={})
    assert sw.available is False


@pytest.mark.asyncio
async def test_privacy_sound_turn_on_success():
    sw = _make_privacy_sound_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "privacy_sound_override", {"result": True}
    )
    assert sw.coordinator.privacy_sound_cache[CAM_ID] is True


@pytest.mark.asyncio
async def test_privacy_sound_turn_on_failure():
    sw = _make_privacy_sound_switch()
    sw.coordinator.async_put_camera = AsyncMock(return_value=False)
    await sw.async_turn_on()
    # Cache should not be updated on failure
    assert CAM_ID not in sw.coordinator.privacy_sound_cache


@pytest.mark.asyncio
async def test_privacy_sound_turn_off():
    sw = _make_privacy_sound_switch(privacy_sound_cache={CAM_ID: True})
    await sw.async_turn_off()
    assert sw.coordinator.privacy_sound_cache[CAM_ID] is False


# ── BoschStatusLedSwitch ─────────────────────────────────────────────────────


def _make_led_switch(ledlights_cache=None, cam_online=True):
    from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

    coord = _coord(ledlights_cache=ledlights_cache or {})
    coord.is_camera_online = lambda cid: cam_online
    sw = BoschStatusLedSwitch.__new__(BoschStatusLedSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_led_available_true():
    sw = _make_led_switch(ledlights_cache={CAM_ID: True})
    assert sw.available is True


def test_led_available_false_no_cache():
    sw = _make_led_switch(ledlights_cache={})
    assert sw.available is False


@pytest.mark.asyncio
async def test_led_turn_on():
    sw = _make_led_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "ledlights", {"state": "ON"}
    )
    assert sw.coordinator.ledlights_cache[CAM_ID] is True


@pytest.mark.asyncio
async def test_led_turn_off():
    sw = _make_led_switch(ledlights_cache={CAM_ID: True})
    await sw.async_turn_off()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "ledlights", {"state": "OFF"}
    )
    assert sw.coordinator.ledlights_cache[CAM_ID] is False


# ── BoschMotionLightSwitch ───────────────────────────────────────────────────


def _make_motion_light_switch(cache=None):
    from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

    coord = _coord(motion_light_cache=cache or {})
    sw = BoschMotionLightSwitch.__new__(BoschMotionLightSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._is_on = None
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_motion_light_is_on_reads_from_cache():
    cache = {CAM_ID: {"lightOnMotionEnabled": True}}
    sw = _make_motion_light_switch(cache=cache)
    assert sw.is_on is True


def test_motion_light_is_on_none_when_no_cache():
    sw = _make_motion_light_switch(cache={})
    assert sw.is_on is None


def test_motion_light_is_on_tracks_external_cache_change():
    """Regression: is_on must reflect a later coordinator re-poll (Bosch-app
    change), not freeze on the first read. Previously _is_on was cached once and
    external OFF→ON changes stayed invisible in HA until restart."""
    cache = {CAM_ID: {"lightOnMotionEnabled": False}}
    sw = _make_motion_light_switch(cache=cache)
    assert sw.is_on is False
    # Slow-tier coordinator re-poll picks up an external change made in the app.
    sw.coordinator.motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": True}
    assert sw.is_on is True


@pytest.mark.asyncio
async def test_motion_light_set_with_cache():
    cache = {CAM_ID: {"lightOnMotionEnabled": False, "duration": 30}}
    sw = _make_motion_light_switch(cache=cache)
    await sw._set_motion_light(True)
    sw.coordinator.async_put_camera.assert_awaited_once()
    called_body = sw.coordinator.async_put_camera.call_args[0][2]
    assert called_body["lightOnMotionEnabled"] is True
    assert sw._is_on is True


@pytest.mark.asyncio
async def test_motion_light_set_without_cache_fetches_api():
    """When cache is empty, _set_motion_light fetches via aiohttp GET."""
    sw = _make_motion_light_switch(cache={})

    fetched_data = {"lightOnMotionEnabled": False, "duration": 30}

    @asynccontextmanager
    async def _get_ctx(*args, **kwargs):
        yield SimpleNamespace(status=200, json=AsyncMock(return_value=fetched_data))

    @asynccontextmanager
    async def _session_get(*args, **kwargs):
        async with _get_ctx() as r:
            yield r

    session = MagicMock()
    session.get = _session_get

    with patch(
        "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
        new=AsyncMock(return_value=session),
    ):
        await sw._set_motion_light(True)

    sw.coordinator.async_put_camera.assert_awaited_once()


# ── BoschIntrusionDetectionSwitch ────────────────────────────────────────────


def _make_intrusion_switch(config=None, privacy_on=False):
    from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch

    coord = _coord(
        intrusion_config={CAM_ID: config} if config else {},
        privacy_on=privacy_on,
    )
    sw = BoschIntrusionDetectionSwitch.__new__(BoschIntrusionDetectionSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_intrusion_is_on_true():
    sw = _make_intrusion_switch(config={"enabled": True, "sensitivity": 3})
    assert sw.is_on is True


def test_intrusion_available_false_no_config():
    sw = _make_intrusion_switch(config=None)
    assert sw.available is False


def test_intrusion_extra_attrs():
    cfg = {"enabled": True, "sensitivity": 3, "detectionMode": "PERSON", "distance": 8}
    sw = _make_intrusion_switch(config=cfg)
    attrs = sw.extra_state_attributes
    assert attrs["sensitivity"] == 3
    assert attrs["detection_mode"] == "PERSON"


@pytest.mark.asyncio
async def test_intrusion_turn_on_privacy_blocked():
    sw = _make_intrusion_switch(
        config={"enabled": False, "sensitivity": 3}, privacy_on=True
    )
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_not_awaited()


@pytest.mark.asyncio
async def test_intrusion_turn_on_success():
    cfg = {"enabled": False, "sensitivity": 3, "detectionMode": "PERSON"}
    sw = _make_intrusion_switch(config=cfg)
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["enabled"] is True


@pytest.mark.asyncio
async def test_intrusion_turn_off():
    cfg = {"enabled": True, "sensitivity": 3}
    sw = _make_intrusion_switch(config=cfg)
    await sw.async_turn_off()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["enabled"] is False


# ── BoschNotificationTypeSwitch ──────────────────────────────────────────────


def _make_notif_type_switch(ntype="movement", cache=None):
    from custom_components.bosch_shc_camera.switch import BoschNotificationTypeSwitch

    coord = _coord(notifications_cache=cache or {})
    sw = BoschNotificationTypeSwitch.__new__(BoschNotificationTypeSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._ntype = ntype
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_notif_type_is_on_true():
    cache = {CAM_ID: {"movement": True, "person": False}}
    sw = _make_notif_type_switch("movement", cache=cache)
    assert sw.is_on is True


def test_notif_type_is_on_false():
    cache = {CAM_ID: {"movement": False}}
    sw = _make_notif_type_switch("movement", cache=cache)
    assert sw.is_on is False


def test_notif_type_is_on_none_no_cache():
    sw = _make_notif_type_switch("movement", cache={})
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_notif_type_turn_on():
    cache = {CAM_ID: {"movement": False, "person": True}}
    sw = _make_notif_type_switch("movement", cache=cache)
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["movement"] is True
    # Person should be preserved
    assert args[2]["person"] is True


@pytest.mark.asyncio
async def test_notif_type_turn_off():
    cache = {CAM_ID: {"movement": True}}
    sw = _make_notif_type_switch("movement", cache=cache)
    await sw.async_turn_off()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["movement"] is False


# ── BoschAlarmSystemArmSwitch ────────────────────────────────────────────────


def _make_alarm_arm_switch(arming_cache=None):
    from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

    coord = _coord(arming_cache=arming_cache or {})
    sw = BoschAlarmSystemArmSwitch.__new__(BoschAlarmSystemArmSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_alarm_arm_is_on_true():
    sw = _make_alarm_arm_switch(arming_cache={CAM_ID: True})
    assert sw.is_on is True


def test_alarm_arm_extra_attrs():
    sw = _make_alarm_arm_switch()
    attrs = sw.extra_state_attributes
    assert "alarm_type" in attrs
    assert "intrusion_system" in attrs


@pytest.mark.asyncio
async def test_alarm_arm_turn_on():
    sw = _make_alarm_arm_switch()
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once_with(
        CAM_ID, "intrusionSystem/arming", {"arm": True}
    )
    assert sw.coordinator.arming_cache[CAM_ID] is True


@pytest.mark.asyncio
async def test_alarm_arm_turn_off():
    sw = _make_alarm_arm_switch(arming_cache={CAM_ID: True})
    await sw.async_turn_off()
    args = sw.coordinator.async_put_camera.call_args[0]
    assert args[2]["arm"] is False


# ── _BoschAlarmSettingsSwitchBase (via BoschAlarmModeSwitch) ─────────────────


def _make_alarm_mode_switch(settings=None):
    from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

    coord = _coord(
        alarm_settings_cache={CAM_ID: settings} if settings is not None else {}
    )
    sw = BoschAlarmModeSwitch.__new__(BoschAlarmModeSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._field = "alarmMode"
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_alarm_mode_is_on_true():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "ON"})
    assert sw.is_on is True


def test_alarm_mode_is_on_false():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "OFF"})
    assert sw.is_on is False


def test_alarm_mode_is_on_none_no_settings():
    sw = _make_alarm_mode_switch(settings={})
    assert sw.is_on is None


@pytest.mark.asyncio
async def test_alarm_mode_turn_on():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "OFF", "preAlarmMode": "ON"})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_awaited_once()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["alarmMode"] == "ON"
    assert body["preAlarmMode"] == "ON"  # preserved


@pytest.mark.asyncio
async def test_alarm_mode_turn_off():
    sw = _make_alarm_mode_switch(settings={"alarmMode": "ON"})
    await sw.async_turn_off()
    body = sw.coordinator.async_put_camera.call_args[0][2]
    assert body["alarmMode"] == "OFF"


@pytest.mark.asyncio
async def test_alarm_mode_set_skips_when_no_settings():
    """_set with empty settings exits early without calling async_put_camera."""
    sw = _make_alarm_mode_switch(settings={})
    await sw.async_turn_on()
    sw.coordinator.async_put_camera.assert_not_awaited()


# ── BoschImageRotation180Switch ──────────────────────────────────────────────


def _make_image_rotation_switch(rotation_180=None):
    from custom_components.bosch_shc_camera.switch import BoschImageRotation180Switch

    coord = _coord(image_rotation_180=rotation_180 or {})
    sw = BoschImageRotation180Switch.__new__(BoschImageRotation180Switch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Kamera"
    sw._model_name = "360"
    sw._fw = "7.91.56"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_image_rotation_is_on_false_default():
    sw = _make_image_rotation_switch()
    assert sw.is_on is False


def test_image_rotation_is_on_true():
    sw = _make_image_rotation_switch(rotation_180={CAM_ID: True})
    assert sw.is_on is True


@pytest.mark.asyncio
async def test_image_rotation_turn_on():
    sw = _make_image_rotation_switch()
    await sw.async_turn_on()
    assert sw.coordinator.image_rotation_180[CAM_ID] is True
    sw.coordinator.async_update_listeners.assert_called_once()


@pytest.mark.asyncio
async def test_image_rotation_turn_off():
    sw = _make_image_rotation_switch(rotation_180={CAM_ID: True})
    await sw.async_turn_off()
    assert sw.coordinator.image_rotation_180[CAM_ID] is False
    sw.coordinator.async_update_listeners.assert_called_once()


# ── BoschNvrRecordingSwitch ──────────────────────────────────────────────────


def _make_nvr_switch(nvr_intent=None, live_connections=None, cam_online=True):
    from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

    coord = _coord(
        nvr_user_intent=nvr_intent or {},
        live_connections=live_connections or {},
    )
    coord.is_camera_online = lambda cid: cam_online
    coord.nvr_error_state = {}
    coord.nvr_processes = {}
    sw = BoschNvrRecordingSwitch.__new__(BoschNvrRecordingSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


def test_nvr_is_on_false_default():
    sw = _make_nvr_switch()
    assert sw.is_on is False


def test_nvr_is_on_true():
    sw = _make_nvr_switch(nvr_intent={CAM_ID: True})
    assert sw.is_on is True


def test_nvr_available_true_local():
    live = {CAM_ID: {"_connection_type": "LOCAL"}}
    sw = _make_nvr_switch(live_connections=live)
    assert sw.available is True


def test_nvr_available_false_remote():
    live = {CAM_ID: {"_connection_type": "REMOTE"}}
    sw = _make_nvr_switch(live_connections=live)
    assert sw.available is False


def test_nvr_available_false_cam_offline():
    live = {CAM_ID: {"_connection_type": "LOCAL"}}
    sw = _make_nvr_switch(live_connections=live, cam_online=False)
    assert sw.available is False


def test_nvr_extra_state_attributes_no_proc():
    sw = _make_nvr_switch()
    attrs = sw.extra_state_attributes
    assert attrs["ffmpeg_running"] is False
    assert "connection_type" in attrs
    assert "last_error" in attrs


@pytest.mark.asyncio
async def test_nvr_turn_on():
    sw = _make_nvr_switch()
    await sw.async_turn_on()
    sw.coordinator.start_recorder.assert_awaited_once_with(CAM_ID)
    sw.async_write_ha_state.assert_called()


@pytest.mark.asyncio
async def test_nvr_turn_off():
    sw = _make_nvr_switch(nvr_intent={CAM_ID: True})
    await sw.async_turn_off()
    sw.coordinator.stop_recorder.assert_awaited_once_with(CAM_ID)
    sw.async_write_ha_state.assert_called()


def _stub_coord_round9(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0, "sound": False},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        live_connections={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        privacy_sound_cache={CAM_ID: True},
        privacy_sound_set_at={},
        timestamp_cache={CAM_ID: True},
        timestamp_set_at={},
        ledlights_cache={CAM_ID: True},
        ledlights_set_at={},
        motion_light_cache={},
        ambient_lighting_cache={},
        global_lighting_cache={},
        intrusion_config_cache={},
        notifications_cache={},
        arming_cache={},
        arming_set_at={},
        alarm_status_cache={},
        alarm_settings_cache={},
        audio_enabled={CAM_ID: True},
        image_rotation_180={},
        nvr_user_intent={},
        nvr_processes={},
        nvr_preroll_processes={},
        nvr_preroll_tasks={},
        nvr_error_state={},
        last_update_success=True,
        options={
            "nvr_base_path": "/config/bosch_nvr",
            "nvr_retention_days": 3,
        },
        token="tok-A",
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
        motion_settings=lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        },
        recording_options=lambda cid: {"recordSound": False},
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord_round9() -> SimpleNamespace:
    return _stub_coord_round9()


@pytest.fixture
def stub_entry_round9() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── Helper: _is_gen2_indoor ───────────────────────────────────────────────────


class TestIsGen2Indoor:
    def test_returns_true_for_home_eyes_indoor(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschTimestampSwitch,
            _is_gen2_indoor,
        )

        stub_coord_round9.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert _is_gen2_indoor(entity) is True, (
            "HOME_Eyes_Indoor must be identified as Gen2 Indoor"
        )

    def test_returns_false_for_outdoor(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschTimestampSwitch,
            _is_gen2_indoor,
        )

        stub_coord_round9.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert _is_gen2_indoor(entity) is False, (
            "Outdoor camera must not be Gen2 Indoor"
        )

    def test_returns_true_for_camera_indoor_gen2(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschTimestampSwitch,
            _is_gen2_indoor,
        )

        stub_coord_round9.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_INDOOR_GEN2"
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert _is_gen2_indoor(entity) is True, (
            "CAMERA_INDOOR_GEN2 must be identified as Gen2 Indoor"
        )


# ── Helper: _warn_if_privacy_on ───────────────────────────────────────────────


class TestWarnIfPrivacyOn:
    @pytest.mark.asyncio
    async def test_returns_false_when_privacy_off(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschTimestampSwitch,
            _warn_if_privacy_on,
        )

        stub_coord_round9.shc_state_cache[CAM_ID]["privacy_mode"] = False
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        result = await _warn_if_privacy_on(entity, "Test")
        assert result is False, "Must return False when privacy mode is off"

    @pytest.mark.asyncio
    async def test_returns_true_when_privacy_on_and_sends_notification(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschTimestampSwitch,
            _warn_if_privacy_on,
        )

        stub_coord_round9.shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord_round9.data[CAM_ID]["info"]["title"] = "Garten"
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        services_mock = AsyncMock()
        entity.hass = SimpleNamespace(
            services=SimpleNamespace(async_call=services_mock)
        )
        result = await _warn_if_privacy_on(entity, "Einbrucherkennung")
        assert result is True, "Must return True to block the write when privacy is on"
        services_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_swallows_notification_error(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschTimestampSwitch,
            _warn_if_privacy_on,
        )

        stub_coord_round9.shc_state_cache[CAM_ID]["privacy_mode"] = True
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.hass = SimpleNamespace(
            services=SimpleNamespace(
                async_call=AsyncMock(side_effect=Exception("svc down"))
            )
        )
        # Must not raise even if persistent_notification.create fails
        result = await _warn_if_privacy_on(entity, "X")
        assert result is True, "Must still block the write even when notification fails"


# ── BoschPrivacySoundSwitch turn_on / turn_off ────────────────────────────────


class TestPrivacySoundSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_updates_cache_on_success(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschPrivacySoundSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord_round9.privacy_sound_cache[CAM_ID] is True, (
            "Cache must be updated to True on success"
        )

    @pytest.mark.asyncio
    async def test_turn_off_updates_cache_on_success(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschPrivacySoundSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord_round9.privacy_sound_cache[CAM_ID] is False, (
            "Cache must be updated to False on success"
        )

    @pytest.mark.asyncio
    async def test_turn_on_no_cache_update_on_failure(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch

        stub_coord_round9.privacy_sound_cache[CAM_ID] = True  # was ON
        stub_coord_round9.async_put_camera = AsyncMock(return_value=False)
        entity = BoschPrivacySoundSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()  # attempt to turn off
        assert stub_coord_round9.privacy_sound_cache[CAM_ID] is True, (
            "Cache must stay True when PUT fails"
        )


# ── BoschTimestampSwitch turn_on / turn_off ───────────────────────────────────


class TestTimestampSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_sets_cache_true(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        stub_coord_round9.async_put_camera = AsyncMock()
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord_round9.timestamp_cache[CAM_ID] is True, (
            "Timestamp cache must be True after turn_on"
        )

    @pytest.mark.asyncio
    async def test_turn_off_sets_cache_false(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        stub_coord_round9.async_put_camera = AsyncMock()
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord_round9.timestamp_cache[CAM_ID] is False, (
            "Timestamp cache must be False after turn_off"
        )

    @pytest.mark.asyncio
    async def test_turn_off_keeps_cache_on_put_failure(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        """Regression: a failed PUT must NOT flip the cache or stamp the write-lock.

        Bug (pre-fix): async_put_camera return value was discarded, so a failed
        cloud PUT both flipped the cache to the wrong value AND bumped
        timestamp_set_at — which then suppressed the coordinator's corrective
        overwrite for the full write-lock window (~30 s), leaving the UI wrong.
        """
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        stub_coord_round9.timestamp_cache[CAM_ID] = True  # currently ON
        stub_coord_round9.timestamp_set_at = {}
        stub_coord_round9.async_put_camera = AsyncMock(return_value=False)
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()  # attempt OFF, but PUT fails
        assert stub_coord_round9.timestamp_cache[CAM_ID] is True, (
            "Cache must stay True when the timestamp PUT fails"
        )
        assert CAM_ID not in stub_coord_round9.timestamp_set_at, (
            "Write-lock must not be stamped on a failed PUT"
        )

    @pytest.mark.asyncio
    async def test_turn_on_keeps_cache_on_put_failure(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch

        stub_coord_round9.timestamp_cache[CAM_ID] = False  # currently OFF
        stub_coord_round9.timestamp_set_at = {}
        stub_coord_round9.async_put_camera = AsyncMock(return_value=False)
        entity = BoschTimestampSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()  # attempt ON, but PUT fails
        assert stub_coord_round9.timestamp_cache[CAM_ID] is False, (
            "Cache must stay False when the timestamp PUT fails"
        )
        assert CAM_ID not in stub_coord_round9.timestamp_set_at, (
            "Write-lock must not be stamped on a failed PUT"
        )


# ── BoschStatusLedSwitch turn_on / turn_off ───────────────────────────────────


class TestStatusLedSwitchActionsRound9:
    @pytest.mark.asyncio
    async def test_turn_on_sets_cache_true_on_success(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord_round9.ledlights_cache[CAM_ID] = False
        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschStatusLedSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord_round9.ledlights_cache[CAM_ID] is True, (
            "Status-LED cache must be True after a successful turn_on"
        )

    @pytest.mark.asyncio
    async def test_turn_off_keeps_cache_on_put_failure(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        """Regression: failed PUT must not flip the cache nor stamp the write-lock."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord_round9.ledlights_cache[CAM_ID] = True  # currently ON
        stub_coord_round9.ledlights_set_at = {}
        stub_coord_round9.async_put_camera = AsyncMock(return_value=False)
        entity = BoschStatusLedSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()  # attempt OFF, but PUT fails
        assert stub_coord_round9.ledlights_cache[CAM_ID] is True, (
            "Cache must stay True when the ledlights PUT fails"
        )
        assert CAM_ID not in stub_coord_round9.ledlights_set_at, (
            "Write-lock must not be stamped on a failed PUT"
        )

    @pytest.mark.asyncio
    async def test_turn_on_keeps_cache_on_put_failure(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord_round9.ledlights_cache[CAM_ID] = False  # currently OFF
        stub_coord_round9.ledlights_set_at = {}
        stub_coord_round9.async_put_camera = AsyncMock(return_value=False)
        entity = BoschStatusLedSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()  # attempt ON, but PUT fails
        assert stub_coord_round9.ledlights_cache[CAM_ID] is False, (
            "Cache must stay False when the ledlights PUT fails"
        )
        assert CAM_ID not in stub_coord_round9.ledlights_set_at, (
            "Write-lock must not be stamped on a failed PUT"
        )


# ── BoschNotificationTypeSwitch ───────────────────────────────────────────────


class TestNotificationTypeSwitch:
    def test_is_on_reads_from_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        stub_coord_round9.notifications_cache[CAM_ID] = {
            "movement": True,
            "person": False,
        }
        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "movement"
        )
        assert entity.is_on is True, "Must read movement flag from notifications cache"

    def test_is_on_false_for_disabled_type(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        stub_coord_round9.notifications_cache[CAM_ID] = {
            "movement": True,
            "person": False,
        }
        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "person"
        )
        assert entity.is_on is False, "Must return False when type is False in cache"

    def test_is_on_none_when_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        stub_coord_round9.notifications_cache = {}
        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "movement"
        )
        assert entity.is_on is None, "Must return None when no cache exists"

    def test_available_false_when_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        stub_coord_round9.notifications_cache = {}
        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "movement"
        )
        assert entity.available is False, (
            "Must be unavailable when notifications cache is empty"
        )

    def test_available_true_when_cache_populated(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        stub_coord_round9.notifications_cache[CAM_ID] = {"movement": True}
        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "movement"
        )
        assert entity.available is True, (
            "Must be available when coordinator succeeded and cache exists"
        )

    def test_translation_key_normalises_camelcase(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "cameraAlarm"
        )
        assert entity._attr_translation_key == "notification_type_camera_alarm", (
            "cameraAlarm must map to notification_type_camera_alarm (snake_case)"
        )

    def test_trouble_email_normalised(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "troubleEmail"
        )
        assert entity._attr_translation_key == "notification_type_trouble_email", (
            "troubleEmail must map to notification_type_trouble_email"
        )

    @pytest.mark.asyncio
    async def test_turn_on_merges_with_existing_flags(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        stub_coord_round9.notifications_cache[CAM_ID] = {
            "movement": False,
            "person": True,
            "audio": False,
        }
        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "movement"
        )
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        sent = stub_coord_round9.async_put_camera.call_args[0][2]
        assert sent["movement"] is True, "turn_on must set movement=True"
        assert sent["person"] is True, "turn_on must preserve person=True"

    @pytest.mark.asyncio
    async def test_turn_off_updates_single_flag(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschNotificationTypeSwitch,
        )

        stub_coord_round9.notifications_cache[CAM_ID] = {"movement": True}
        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschNotificationTypeSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9, "movement"
        )
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord_round9.notifications_cache[CAM_ID]["movement"] is False, (
            "Cache must reflect False after successful turn_off"
        )


# ── BoschAlarmSystemArmSwitch ─────────────────────────────────────────────────


class TestAlarmSystemArmSwitch:
    def test_is_on_reads_arming_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        stub_coord_round9.arming_cache[CAM_ID] = True
        entity = BoschAlarmSystemArmSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is True, "Must read arming state from arming_cache"

    def test_is_on_none_when_no_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        stub_coord_round9.arming_cache = {}
        entity = BoschAlarmSystemArmSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is None, "Must return None when not yet known"

    def test_available_requires_camera_online(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        stub_coord_round9.is_camera_online = lambda cid: False
        entity = BoschAlarmSystemArmSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is False, "Must be unavailable when camera is offline"

    def test_extra_attrs_include_alarm_status(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        stub_coord_round9.alarm_status_cache[CAM_ID] = {
            "alarmType": "INTRUSION",
            "intrusionSystem": "ARMED",
        }
        entity = BoschAlarmSystemArmSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        attrs = entity.extra_state_attributes
        assert attrs["alarm_type"] == "INTRUSION", "extra_attrs must expose alarmType"
        assert attrs["intrusion_system"] == "ARMED", (
            "extra_attrs must expose intrusionSystem"
        )

    @pytest.mark.asyncio
    async def test_turn_on_updates_arming_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmSystemArmSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord_round9.arming_cache[CAM_ID] is True, (
            "Cache must be True after arm"
        )

    @pytest.mark.asyncio
    async def test_turn_off_updates_arming_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch

        stub_coord_round9.arming_cache[CAM_ID] = True
        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmSystemArmSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord_round9.arming_cache[CAM_ID] is False, (
            "Cache must be False after disarm"
        )


# ── _BoschAlarmSettingsSwitchBase / BoschAlarmModeSwitch / BoschPreAlarmSwitch ─


class TestAlarmSettingsSwitchBase:
    def test_is_on_true_when_field_is_ON(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache[CAM_ID] = {
            "alarmMode": "ON",
            "preAlarmMode": "OFF",
        }
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is True, "alarmMode=ON must yield is_on=True"

    def test_is_on_false_when_field_is_OFF(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache[CAM_ID] = {"alarmMode": "OFF"}
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is False, "alarmMode=OFF must yield is_on=False"

    def test_is_on_none_when_field_missing(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache = {}
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is None, "Must return None when no alarm settings cached"

    def test_available_false_when_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache = {}
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is False, (
            "Must be unavailable when alarm settings cache is empty"
        )

    def test_available_true_when_cache_and_online(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache[CAM_ID] = {"alarmMode": "ON"}
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is True, (
            "Must be available when coordinator ok and cache populated"
        )

    @pytest.mark.asyncio
    async def test_set_updates_field_to_ON(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache[CAM_ID] = {
            "alarmMode": "OFF",
            "preAlarmMode": "OFF",
        }
        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord_round9.alarm_settings_cache[CAM_ID]["alarmMode"] == "ON", (
            "_set must write alarmMode=ON on turn_on"
        )

    @pytest.mark.asyncio
    async def test_set_updates_field_to_OFF(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache[CAM_ID] = {
            "alarmMode": "ON",
            "preAlarmMode": "OFF",
        }
        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord_round9.alarm_settings_cache[CAM_ID]["alarmMode"] == "OFF", (
            "_set must write alarmMode=OFF on turn_off"
        )

    @pytest.mark.asyncio
    async def test_set_no_op_when_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        """No crash or API call when alarm_settings cache is empty (camera not yet polled)."""
        from custom_components.bosch_shc_camera.switch import BoschAlarmModeSwitch

        stub_coord_round9.alarm_settings_cache = {}
        stub_coord_round9.async_put_camera = AsyncMock()
        entity = BoschAlarmModeSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        stub_coord_round9.async_put_camera.assert_not_called()

    def test_prealarm_reads_prealarmmode_field(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschPreAlarmSwitch

        stub_coord_round9.alarm_settings_cache[CAM_ID] = {
            "alarmMode": "OFF",
            "preAlarmMode": "ON",
        }
        entity = BoschPreAlarmSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is True, (
            "BoschPreAlarmSwitch must read preAlarmMode not alarmMode"
        )


# ── BoschImageRotation180Switch ───────────────────────────────────────────────


class TestImageRotation180Switch:
    def test_is_on_reads_rotation_dict(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
        )

        stub_coord_round9.image_rotation_180 = {CAM_ID: True}
        entity = BoschImageRotation180Switch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.is_on is True, "is_on must read from image_rotation_180"

    def test_is_on_false_when_not_set(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
        )

        stub_coord_round9.image_rotation_180 = {}
        entity = BoschImageRotation180Switch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.is_on is False, "is_on must default to False when not in dict"

    def test_available_requires_only_coordinator_success(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
        )

        stub_coord_round9.is_camera_online = lambda cid: (
            False
        )  # camera offline — still available
        entity = BoschImageRotation180Switch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.available is True, (
            "ImageRotation is client-side — available even when camera offline"
        )

    @pytest.mark.asyncio
    async def test_turn_on_sets_dict_true(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
        )

        entity = BoschImageRotation180Switch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert stub_coord_round9.image_rotation_180[CAM_ID] is True, (
            "turn_on must set image_rotation_180[cam_id]=True"
        )
        stub_coord_round9.async_update_listeners.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_sets_dict_false(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
        )

        stub_coord_round9.image_rotation_180 = {CAM_ID: True}
        entity = BoschImageRotation180Switch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert stub_coord_round9.image_rotation_180[CAM_ID] is False, (
            "turn_off must clear image_rotation_180[cam_id]"
        )

    @pytest.mark.asyncio
    async def test_async_added_to_hass_restores_on_state(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
        )

        entity = BoschImageRotation180Switch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        last_state = SimpleNamespace(state="on")
        entity.async_get_last_state = AsyncMock(return_value=last_state)
        # Patch both parent async_added_to_hass to avoid HA runtime dependency
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch(
                "homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
                AsyncMock(),
            ),
        ):
            await entity.async_added_to_hass()
        assert stub_coord_round9.image_rotation_180[CAM_ID] is True, (
            "Must restore ON state from previous HA run"
        )

    @pytest.mark.asyncio
    async def test_async_added_to_hass_no_op_for_off_state(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
        )

        entity = BoschImageRotation180Switch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        last_state = SimpleNamespace(state="off")
        entity.async_get_last_state = AsyncMock(return_value=last_state)
        with (
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch(
                "homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
                AsyncMock(),
            ),
        ):
            await entity.async_added_to_hass()
        assert not stub_coord_round9.image_rotation_180.get(CAM_ID), (
            "Must not set rotation flag when previous state was off"
        )


# ── BoschNvrRecordingSwitch ───────────────────────────────────────────────────


class TestNvrRecordingSwitch:
    def test_is_on_reads_nvr_user_intent(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_coord_round9.nvr_user_intent[CAM_ID] = True
        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is True, "is_on must read from nvr_user_intent"

    def test_is_on_false_when_not_set(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is False, "is_on must default to False"

    def test_available_false_when_coordinator_failed(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_coord_round9.last_update_success = False
        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is False, "Must be unavailable when coordinator failed"

    def test_available_false_when_camera_offline(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_coord_round9.is_camera_online = lambda cid: False
        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is False, "Must be unavailable when camera is offline"

    def test_available_false_when_no_live_connection(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_coord_round9.live_connections = {}  # no active stream
        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is False, (
            "Must be unavailable when no live connection exists"
        )

    def test_available_false_when_live_is_remote(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_coord_round9.live_connections[CAM_ID] = {"_connection_type": "REMOTE"}
        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is False, (
            "NVR is LAN-only — must be unavailable when REMOTE"
        )

    def test_available_true_when_local_stream_active(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_coord_round9.live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.available is True, "Must be available when LOCAL stream is active"

    def test_extra_attrs_exposes_ffmpeg_state(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_coord_round9.nvr_processes[CAM_ID] = MagicMock(returncode=None)  # running
        stub_coord_round9.live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        attrs = entity.extra_state_attributes
        assert attrs["ffmpeg_running"] is True, (
            "extra_attrs must surface ffmpeg_running=True when process alive"
        )
        assert attrs["connection_type"] == "LOCAL", (
            "extra_attrs must include connection_type"
        )

    def test_entity_disabled_by_default(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        entity = BoschNvrRecordingSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity._attr_entity_registry_enabled_default is False, (
            "NVR switch must be opt-in (disabled by default)"
        )


# ── BoschMotionLightSwitch (is_on from cache) ─────────────────────────────────


class TestMotionLightSwitchIsOn:
    def test_is_on_reads_from_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        stub_coord_round9.motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": True}
        entity = BoschMotionLightSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is True, (
            "is_on must read lightOnMotionEnabled from motion_light_cache"
        )

    def test_is_on_none_when_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        stub_coord_round9.motion_light_cache = {}
        entity = BoschMotionLightSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        assert entity.is_on is None, (
            "is_on must be None when cache is empty (not yet polled)"
        )

    @pytest.mark.asyncio
    async def test_set_motion_light_updates_cache_on_success(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        stub_coord_round9.motion_light_cache[CAM_ID] = {
            "lightOnMotionEnabled": False,
            "delay": 30,
        }
        stub_coord_round9.async_put_camera = AsyncMock(return_value=True)
        entity = BoschMotionLightSwitch(stub_coord_round9, CAM_ID, stub_entry_round9)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert (
            stub_coord_round9.motion_light_cache[CAM_ID]["lightOnMotionEnabled"] is True
        ), "Cache must be updated after successful PUT"


# ── BoschSoftLightFadingSwitch ────────────────────────────────────────────────


class TestSoftLightFadingSwitchRound9:
    def test_is_on_reads_softlightfading(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_round9.global_lighting_cache[CAM_ID] = {
            "softLightFading": True,
            "darknessThreshold": 0.5,
        }
        entity = BoschSoftLightFadingSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.is_on is True, (
            "is_on must read softLightFading from global lighting cache"
        )

    def test_is_on_none_when_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        entity = BoschSoftLightFadingSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.is_on is None, "is_on must be None when cache empty"

    def test_available_false_when_no_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        entity = BoschSoftLightFadingSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.available is False, (
            "Must be unavailable when global_lighting_cache is empty"
        )

    def test_available_true_when_cache_present(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_round9.global_lighting_cache[CAM_ID] = {"softLightFading": False}
        entity = BoschSoftLightFadingSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.available is True, "Must be available when cache exists"


# ── BoschIntrusionDetectionSwitch ─────────────────────────────────────────────


class TestIntrusionDetectionSwitch:
    def test_is_on_reads_enabled_from_intrusion_cache(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschIntrusionDetectionSwitch,
        )

        stub_coord_round9.intrusion_config_cache[CAM_ID] = {
            "enabled": True,
            "sensitivity": 3,
            "detectionMode": "STANDARD",
            "distance": 5.0,
        }
        entity = BoschIntrusionDetectionSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.is_on is True, (
            "is_on must read 'enabled' from intrusion_config_cache"
        )

    def test_is_on_none_when_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschIntrusionDetectionSwitch,
        )

        entity = BoschIntrusionDetectionSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.is_on is None, "is_on must be None when cache is empty"

    def test_available_false_when_intrusion_cache_empty(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschIntrusionDetectionSwitch,
        )

        entity = BoschIntrusionDetectionSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        assert entity.available is False, (
            "Must be unavailable when intrusion config not yet polled"
        )

    def test_extra_attrs_expose_sensitivity_and_mode(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschIntrusionDetectionSwitch,
        )

        stub_coord_round9.intrusion_config_cache[CAM_ID] = {
            "enabled": True,
            "sensitivity": 4,
            "detectionMode": "HIGH_SENSITIVITY",
            "distance": 8.0,
        }
        entity = BoschIntrusionDetectionSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        attrs = entity.extra_state_attributes
        assert attrs["sensitivity"] == 4, "extra_attrs must expose sensitivity"
        assert attrs["detection_mode"] == "HIGH_SENSITIVITY", (
            "extra_attrs must expose detectionMode"
        )
        assert attrs["distance_meters"] == 8.0, "extra_attrs must expose distance"

    @pytest.mark.asyncio
    async def test_privacy_blocks_set_intrusion(
        self, stub_coord_round9: SimpleNamespace, stub_entry_round9: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import (
            BoschIntrusionDetectionSwitch,
        )

        stub_coord_round9.shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord_round9.intrusion_config_cache[CAM_ID] = {
            "enabled": False,
            "sensitivity": 3,
        }
        stub_coord_round9.async_put_camera = AsyncMock()
        entity = BoschIntrusionDetectionSwitch(
            stub_coord_round9, CAM_ID, stub_entry_round9
        )
        entity.async_write_ha_state = MagicMock()
        entity.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
        await entity.async_turn_on()
        stub_coord_round9.async_put_camera.assert_not_called()


def _stub_coord_sprintma(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0, "sound": False},
                    "featureStatus": {},
                },
                "status": "ONLINE",
                "events": [],
                "autofollow": {"result": False},
                "recordingOptions": {"recordSound": False},
                "audioAlarm": {
                    "enabled": True,
                    "threshold": 50,
                    "sensitivity": "MEDIUM",
                    "audioAlarmConfiguration": "CUSTOM",
                },
            }
        },
        live_connections={},
        user_intent_streams=set(),  # v12.4.12: switch reads from this
        shc_state_cache={
            CAM_ID: {
                "privacy_mode": False,
                "camera_light": True,
                "front_light": True,
                "wallwasher": False,
                "notifications_status": "FOLLOW_CAMERA_SCHEDULE",
                "has_light": True,
            }
        },
        session_stale={},
        stream_warming=set(),
        privacy_set_at={},
        light_set_at={},
        audio_enabled={CAM_ID: True},
        audio_cache={},
        privacy_sound_cache={CAM_ID: False},
        privacy_sound_set_at={},
        timestamp_cache={CAM_ID: True},
        timestamp_set_at={},
        ledlights_cache={CAM_ID: True},
        ledlights_set_at={},
        motion_light_cache={},
        ambient_lighting_cache={},
        global_lighting_cache={},
        intrusion_config_cache={},
        intrusion_config_set_at={},
        motion_set_at={},
        alarm_settings_set_at={},
        notifications_cache={},
        arming_cache={},
        arming_set_at={},
        alarm_status_cache={},
        alarm_settings_cache={},
        image_rotation_180={},
        nvr_user_intent={},
        nvr_processes={},
        nvr_preroll_processes={},
        nvr_preroll_tasks={},
        nvr_error_state={},
        bg_tasks=set(),
        last_update_success=True,
        options={
            "nvr_base_path": "/config/bosch_nvr",
            "nvr_retention_days": 3,
        },
        token="tok-A",
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: False,
        motion_settings=lambda cid: {
            "enabled": True,
            "motionAlarmConfiguration": "HIGH",
        },
        recording_options=lambda cid: {"recordSound": False},
        async_put_camera=AsyncMock(return_value=True),
        async_request_refresh=AsyncMock(),
        async_update_listeners=MagicMock(),
        async_cloud_set_camera_light=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
        async_cloud_set_privacy_mode=AsyncMock(),
        async_cloud_set_notifications=AsyncMock(),
        tear_down_live_stream=AsyncMock(),
        try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        record_stream_error=MagicMock(),
        stop_tls_proxy=AsyncMock(),
        stop_viewing_front_door=AsyncMock(),
        stop_remote_viewing_front_door=AsyncMock(),
        start_recorder=AsyncMock(),
        stop_recorder=AsyncMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord_sprintma() -> SimpleNamespace:
    return _stub_coord()


@pytest.fixture
def stub_entry_sprintma() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "x"},
        options={"enable_snapshot_button": True, "enable_nvr": False},
    )


# (`_bind_hass` helper is defined once, shared across this file — see the
# batch-C section below; body was byte-identical here, deduped.)


# ── async_setup_entry ─────────────────────────────────────────────────────────


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_early_return_when_snapshot_disabled(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 150: return early when enable_snapshot_button=False."""
        from custom_components.bosch_shc_camera.switch import async_setup_entry

        stub_entry_sprintma.options = {"enable_snapshot_button": False}
        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        async def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert added == [], (
            "No entities should be added when enable_snapshot_button=False"
        )

    @pytest.mark.asyncio
    async def test_creates_base_entities_for_cam(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 166-237: at least LiveStream + Audio + Privacy entities created."""
        from custom_components.bosch_shc_camera.switch import (
            BoschAudioSwitch,
            BoschLiveStreamSwitch,
            BoschPrivacyModeSwitch,
            async_setup_entry,
        )

        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        types = [type(e) for e in added]
        assert BoschLiveStreamSwitch in types
        assert BoschAudioSwitch in types
        assert BoschPrivacyModeSwitch in types

    @pytest.mark.asyncio
    async def test_creates_light_entities_when_feature_present(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 181-185: CameraLight, FrontLight, Wallwasher only when has_light=True."""
        from custom_components.bosch_shc_camera.switch import (
            BoschCameraLightSwitch,
            BoschFrontLightSwitch,
            BoschWallwasherSwitch,
            async_setup_entry,
        )

        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        types = [type(e) for e in added]
        assert BoschCameraLightSwitch in types
        assert BoschFrontLightSwitch in types
        assert BoschWallwasherSwitch in types

    @pytest.mark.asyncio
    async def test_skips_light_entities_when_no_light_feature(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 181-185: No light entities when featureSupport.light=False."""
        from custom_components.bosch_shc_camera.switch import (
            BoschCameraLightSwitch,
            async_setup_entry,
        )

        stub_coord_sprintma.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert not any(isinstance(e, BoschCameraLightSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_creates_nvr_switch_when_enabled(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 235: NvrRecordingSwitch added only if enable_nvr=True."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
            async_setup_entry,
        )

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert any(isinstance(e, BoschNvrRecordingSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_skips_nvr_switch_when_disabled(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 235: NvrRecordingSwitch NOT added when enable_nvr=False."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrRecordingSwitch,
            async_setup_entry,
        )

        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert not any(isinstance(e, BoschNvrRecordingSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_creates_nvr_event_clip_switch_when_enabled(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """BoschNvrEventClipSwitch added alongside BoschNvrRecordingSwitch
        whenever enable_nvr=True (issue #43 follow-up feature request)."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrEventClipSwitch,
            async_setup_entry,
        )

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert any(isinstance(e, BoschNvrEventClipSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_skips_nvr_event_clip_switch_when_disabled(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """NvrEventClipSwitch NOT added when enable_nvr=False, mirroring
        BoschNvrRecordingSwitch's own gating."""
        from custom_components.bosch_shc_camera.switch import (
            BoschNvrEventClipSwitch,
            async_setup_entry,
        )

        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert not any(isinstance(e, BoschNvrEventClipSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_creates_indoor_privacy_sound_switch(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 201-202: PrivacySoundSwitch for indoor cameras."""
        from custom_components.bosch_shc_camera.switch import (
            BoschPrivacySoundSwitch,
            async_setup_entry,
        )

        stub_coord_sprintma.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"
        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert any(isinstance(e, BoschPrivacySoundSwitch) for e in added)

    @pytest.mark.asyncio
    async def test_creates_gen2_entities_for_gen2_camera(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 207-212: StatusLed, MotionLight, AmbientLight, SoftLightFading, IntrusionDetection for Gen2."""
        from custom_components.bosch_shc_camera.switch import (
            BoschAmbientLightSwitch,
            BoschIntrusionDetectionSwitch,
            BoschMotionLightSwitch,
            BoschSoftLightFadingSwitch,
            BoschStatusLedSwitch,
            async_setup_entry,
        )

        stub_coord_sprintma.data[CAM_ID]["info"]["hardwareVersion"] = (
            "HOME_Eyes_Outdoor"
        )
        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        types = [type(e) for e in added]
        assert BoschStatusLedSwitch in types
        assert BoschMotionLightSwitch in types
        assert BoschAmbientLightSwitch in types
        assert BoschSoftLightFadingSwitch in types
        assert BoschIntrusionDetectionSwitch in types

    @pytest.mark.asyncio
    async def test_creates_image_rotation_for_indoor(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 231-232: ImageRotation180Switch for INDOOR cameras."""
        from custom_components.bosch_shc_camera.switch import (
            BoschImageRotation180Switch,
            async_setup_entry,
        )

        stub_coord_sprintma.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"
        stub_entry_sprintma.runtime_data = stub_coord_sprintma
        added = []

        def fake_add(ents, **kw):
            added.extend(ents)

        hass = MagicMock()
        with patch(
            "custom_components.bosch_shc_camera.switch.get_options",
            return_value=stub_entry_sprintma.options,
        ):
            await async_setup_entry(hass, stub_entry_sprintma, fake_add)
        assert any(isinstance(e, BoschImageRotation180Switch) for e in added)


# ── BoschLiveStreamSwitch ─────────────────────────────────────────────────────


class TestLiveStreamSwitchTurnOn:
    @pytest.mark.asyncio
    async def test_blocked_by_privacy_raises(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 285-289: ServiceValidationError when privacy mode is ON."""
        from homeassistant.exceptions import ServiceValidationError

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord_sprintma.shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschLiveStreamSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        with pytest.raises(ServiceValidationError):
            await sw.async_turn_on()
        stub_coord_sprintma.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_blocks_when_stream_just_stopped(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 291-297: cooldown guard — no connection attempt within STREAM_COOLDOWN."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        sw = BoschLiveStreamSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        sw._last_stream_off = time.monotonic()  # just stopped
        await sw.async_turn_on()
        stub_coord_sprintma.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_allows_after_enough_time(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Cooldown should NOT block when _last_stream_off is old enough."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        sw = BoschLiveStreamSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        sw._last_stream_off = time.monotonic() - 100  # 100 s ago — well past cooldown
        await sw.async_turn_on()
        stub_coord_sprintma.try_live_connection.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_success_local_schedules_watchdog(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 302-322: LOCAL result schedules _stream_health_watchdog task."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord_sprintma.try_live_connection = AsyncMock(
            return_value={"_connection_type": "LOCAL", "rtspsUrl": "rtsps://x"}
        )
        sw = BoschLiveStreamSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        sw.hass.async_create_task.assert_called_once()  # watchdog task scheduled
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_success_remote_no_watchdog(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 302-322: REMOTE result does NOT schedule a watchdog."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord_sprintma.try_live_connection = AsyncMock(
            return_value={"_connection_type": "REMOTE", "rtspsUrl": "rtsps://x"}
        )
        sw = BoschLiveStreamSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        sw.hass.async_create_task.assert_not_called()
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_failure_records_stream_error(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 323-325: None result → record_stream_error called."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        stub_coord_sprintma.try_live_connection = AsyncMock(return_value=None)
        sw = BoschLiveStreamSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord_sprintma.record_stream_error.assert_called_once_with(CAM_ID)
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_calls_teardown_and_refresh(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 428-437: turn_off tears down stream, writes state, requests refresh."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        sw = BoschLiveStreamSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord_sprintma.tear_down_live_stream.assert_awaited_once_with(CAM_ID)
        sw.async_write_ha_state.assert_called_once()
        sw.hass.async_create_task.assert_called_once()  # async_request_refresh task


# ── BoschAudioSwitch ──────────────────────────────────────────────────────────


class TestAudioSwitchActionsSprintma:
    @pytest.mark.asyncio
    async def test_turn_on_sets_flag_and_applies(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 464-468: turn_on sets audio_enabled True and calls _apply_audio_change."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_sprintma.audio_enabled[CAM_ID] = False
        sw = BoschAudioSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        assert stub_coord_sprintma.audio_enabled[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_sets_flag_and_applies(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 470-474: turn_off sets audio_enabled False and calls _apply_audio_change."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_sprintma.audio_enabled[CAM_ID] = True
        sw = BoschAudioSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_off()
        assert stub_coord_sprintma.audio_enabled[CAM_ID] is False

    @pytest.mark.asyncio
    async def test_turn_on_never_reconnects_even_during_privacy(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """The audio switch is a card-side mute now — turning it on must never
        re-open the live connection, including while privacy is active (the old
        _apply_audio_change reconnect was removed)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_sprintma.shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord_sprintma.live_connections[CAM_ID] = {
            "_connection_type": "LOCAL",
            "rtspsUrl": "rtsps://x",
        }
        sw = BoschAudioSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        assert stub_coord_sprintma.audio_enabled[CAM_ID] is True
        stub_coord_sprintma.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_does_not_reopen_stream_when_live(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Audio is now a synced card-side MUTE — the AAC track is always in the
        stream, so toggling the switch must NOT re-open the live connection
        (regression for the old reconnect-on-every-toggle jank, #22)."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        stub_coord_sprintma.live_connections[CAM_ID] = {
            "_connection_type": "LOCAL",
            "rtspsUrl": "rtsps://x",
        }
        sw = BoschAudioSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        assert stub_coord_sprintma.audio_enabled[CAM_ID] is True
        stub_coord_sprintma.try_live_connection.assert_not_called()

    @pytest.mark.asyncio
    async def test_toggle_no_reconnect_no_refresh_when_not_live(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Off-stream toggle just stores the preference + writes state — no
        stream re-open and no coordinator refresh."""
        from custom_components.bosch_shc_camera.switch import BoschAudioSwitch

        sw = BoschAudioSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_off()
        assert stub_coord_sprintma.audio_enabled[CAM_ID] is False
        stub_coord_sprintma.try_live_connection.assert_not_called()
        sw.hass.async_create_task.assert_not_called()


# ── BoschCameraLightSwitch.available ─────────────────────────────────────────


class TestCameraLightSwitchAvailable:
    def test_available_when_online(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 514-523: available=True when coordinator ok + camera online."""
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        sw = BoschCameraLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is True

    def test_unavailable_when_coordinator_fails(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """available=False when last_update_success=False."""
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        stub_coord_sprintma.last_update_success = False
        sw = BoschCameraLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is False

    def test_unavailable_when_camera_offline(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """available=False when camera offline."""
        from custom_components.bosch_shc_camera.switch import BoschCameraLightSwitch

        stub_coord_sprintma.is_camera_online = lambda cid: False
        sw = BoschCameraLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is False


# ── BoschFrontLightSwitch and BoschWallwasherSwitch is_on ─────────────────────


class TestFrontLightIsOn:
    def test_is_on_reads_from_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 550: reads front_light from shc_state_cache."""
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        stub_coord_sprintma.shc_state_cache[CAM_ID]["front_light"] = True
        sw = BoschFrontLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.is_on is True

    def test_is_on_false(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschFrontLightSwitch

        stub_coord_sprintma.shc_state_cache[CAM_ID]["front_light"] = False
        sw = BoschFrontLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.is_on is False


class TestWallwasherIsOn:
    def test_is_on_true(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 582, 588: reads wallwasher state from cache."""
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch

        stub_coord_sprintma.shc_state_cache[CAM_ID]["wallwasher"] = True
        sw = BoschWallwasherSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.is_on is True

    def test_is_on_false(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschWallwasherSwitch

        stub_coord_sprintma.shc_state_cache[CAM_ID]["wallwasher"] = False
        sw = BoschWallwasherSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.is_on is False


# ── BoschMotionEnabledSwitch gen2 privacy guard ───────────────────────────────


class TestMotionEnabledSwitchGen2Privacy:
    @pytest.mark.asyncio
    async def test_turn_on_blocked_by_privacy_for_gen2_indoor(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 690: gen2 indoor camera blocked when privacy ON."""
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

        stub_coord_sprintma.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        stub_coord_sprintma.shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschMotionEnabledSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        # async_put_camera must NOT be called
        stub_coord_sprintma.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_not_blocked_for_outdoor(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Outdoor cameras not blocked by privacy guard."""
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

        stub_coord_sprintma.data[CAM_ID]["info"]["hardwareVersion"] = (
            "HOME_Eyes_Outdoor"
        )
        stub_coord_sprintma.shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschMotionEnabledSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord_sprintma.async_put_camera.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_blocked_by_privacy_for_gen2_indoor(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 721: gen2 indoor camera turn_off also blocked by privacy."""
        from custom_components.bosch_shc_camera.switch import BoschMotionEnabledSwitch

        stub_coord_sprintma.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        stub_coord_sprintma.shc_state_cache[CAM_ID]["privacy_mode"] = True
        sw = BoschMotionEnabledSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_off()
        stub_coord_sprintma.async_put_camera.assert_not_called()


# ── BoschIntercomSwitch ───────────────────────────────────────────────────────


class TestIntercomSwitch:
    def test_is_on_defaults_false(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 889-896: _is_on defaults to False."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_success_sets_is_on(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Successful async_put_camera sets _is_on=True."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()
        assert sw._is_on is True
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_success_sets_is_on_false(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Successful async_put_camera sets _is_on=False."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw._is_on = True
        _bind_hass(sw)
        await sw.async_turn_off()
        assert sw._is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_failure_does_not_raise(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """async_put_camera returning False must not raise, and must still
        call async_write_ha_state."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        stub_coord_sprintma.async_put_camera = AsyncMock(return_value=False)
        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()  # must not raise
        assert sw._is_on is False
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_failure_does_not_raise(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """async_put_camera returning False must not raise on turn_off."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        stub_coord_sprintma.async_put_camera = AsyncMock(return_value=False)
        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw._is_on = True
        _bind_hass(sw)
        await sw.async_turn_off()  # must not raise
        assert sw._is_on is True  # unchanged — the write failed
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_on_uses_correct_field_casing_and_preserves_mic_level(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Regression: a prior version sent the wrong JSON key casing
        ("SpeakerLevel" instead of the API's "speakerLevel") — silently
        ignored by the API, so speaker level 50 never actually applied. It
        also sent a partial body that omitted microphoneLevel entirely."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        stub_coord_sprintma.audio_cache[CAM_ID] = {
            "audioEnabled": False,
            "microphoneLevel": 60,
            "speakerLevel": 20,
        }
        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()

        stub_coord_sprintma.async_put_camera.assert_awaited_once_with(
            CAM_ID,
            "audio",
            {"audioEnabled": True, "microphoneLevel": 60, "speakerLevel": 50},
        )

    @pytest.mark.asyncio
    async def test_turn_on_updates_shared_audio_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Regression: the prior implementation never wrote back to
        audio_cache, leaving BoschSpeakerLevelNumber/BoschMicrophoneLevelNumber's
        cached view permanently stale after every intercom toggle."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        stub_coord_sprintma.audio_cache[CAM_ID] = {
            "audioEnabled": False,
            "microphoneLevel": 60,
            "speakerLevel": 20,
        }
        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw.async_turn_on()

        assert stub_coord_sprintma.audio_cache[CAM_ID]["audioEnabled"] is True
        assert stub_coord_sprintma.audio_cache[CAM_ID]["speakerLevel"] == 50
        # microphoneLevel untouched
        assert stub_coord_sprintma.audio_cache[CAM_ID]["microphoneLevel"] == 60

    @pytest.mark.asyncio
    async def test_turn_off_does_not_touch_speaker_level(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """turn_off only sets audioEnabled=False; speakerLevel/microphoneLevel
        stay whatever they were, matching the ON-only "speakerLevel=50" body
        documented on the class."""
        from custom_components.bosch_shc_camera.switch import BoschIntercomSwitch

        stub_coord_sprintma.audio_cache[CAM_ID] = {
            "audioEnabled": True,
            "microphoneLevel": 60,
            "speakerLevel": 50,
        }
        sw = BoschIntercomSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw._is_on = True
        _bind_hass(sw)
        await sw.async_turn_off()

        stub_coord_sprintma.async_put_camera.assert_awaited_once_with(
            CAM_ID,
            "audio",
            {"audioEnabled": False, "microphoneLevel": 60, "speakerLevel": 50},
        )
        assert stub_coord_sprintma.audio_cache[CAM_ID]["speakerLevel"] == 50


# ── BoschStatusLedSwitch ──────────────────────────────────────────────────────


class TestStatusLedSwitchActions:
    @pytest.mark.asyncio
    async def test_turn_on_updates_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1076-1082: turn_on PUTs {"state":"ON"} and updates cache."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord_sprintma.ledlights_cache[CAM_ID] = False
        sw = BoschStatusLedSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        stub_coord_sprintma.async_put_camera.assert_awaited_once_with(
            CAM_ID, "ledlights", {"state": "ON"}
        )
        assert stub_coord_sprintma.ledlights_cache[CAM_ID] is True

    @pytest.mark.asyncio
    async def test_turn_off_updates_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1084-1090: turn_off PUTs {"state":"OFF"} and updates cache."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord_sprintma.ledlights_cache[CAM_ID] = True
        sw = BoschStatusLedSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        stub_coord_sprintma.async_put_camera.assert_awaited_once_with(
            CAM_ID, "ledlights", {"state": "OFF"}
        )
        assert stub_coord_sprintma.ledlights_cache[CAM_ID] is False

    def test_available_with_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1069-1074: available=True only when cache has value."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord_sprintma.ledlights_cache[CAM_ID] = True
        sw = BoschStatusLedSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is True

    def test_available_false_when_cache_none(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 1073: available=False when ledlights cache is None."""
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch

        stub_coord_sprintma.ledlights_cache[CAM_ID] = None
        sw = BoschStatusLedSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is False


# ── BoschMotionLightSwitch ────────────────────────────────────────────────────


class TestMotionLightSwitch:
    @pytest.mark.asyncio
    async def test_turn_on_with_cached_config(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1146-1163: cache hit path — no HTTP GET, PUT directly with toggled flag."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        stub_coord_sprintma.motion_light_cache[CAM_ID] = {
            "lightOnMotionEnabled": False,
            "sensitivity": "MEDIUM",
            "delay": 30,
        }
        stub_coord_sprintma.async_put_camera = AsyncMock(return_value=True)
        sw = BoschMotionLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord_sprintma.async_put_camera.assert_awaited_once()
        assert (
            stub_coord_sprintma.motion_light_cache[CAM_ID]["lightOnMotionEnabled"]
            is True
        )
        assert sw._is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_with_cached_config(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1168-1169: turn_off delegates to _set_motion_light(False)."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        stub_coord_sprintma.motion_light_cache[CAM_ID] = {
            "lightOnMotionEnabled": True,
            "sensitivity": "HIGH",
        }
        stub_coord_sprintma.async_put_camera = AsyncMock(return_value=True)
        sw = BoschMotionLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_off()
        assert (
            stub_coord_sprintma.motion_light_cache[CAM_ID]["lightOnMotionEnabled"]
            is False
        )
        assert sw._is_on is False

    @pytest.mark.asyncio
    async def test_set_motion_light_no_op_on_put_fail(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 1162: PUT failure — cache and _is_on not updated."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        stub_coord_sprintma.motion_light_cache[CAM_ID] = {"lightOnMotionEnabled": False}
        stub_coord_sprintma.async_put_camera = AsyncMock(return_value=False)
        sw = BoschMotionLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        # _is_on must remain None (not updated on failure)
        assert sw._is_on is None

    def test_available_true(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1121-1125: available when coordinator ok + online."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        sw = BoschMotionLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is True

    def test_available_false_coordinator_fail(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 1122: available=False when coordinator fails."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        stub_coord_sprintma.last_update_success = False
        sw = BoschMotionLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is False


# ── BoschAmbientLightSwitch ───────────────────────────────────────────────────


class TestAmbientLightSwitch:
    def test_is_on_reads_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1192-1196: is_on reads from ambient_lighting_cache."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        stub_coord_sprintma.ambient_lighting_cache[CAM_ID] = {
            "ambientLightEnabled": True
        }
        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.is_on is True

    def test_is_on_none_when_cache_empty(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """is_on returns None when no cache data."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.is_on is None

    def test_available_true(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1199-1203: available when coordinator ok + online."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is True

    def test_available_false_when_offline(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 1200: available=False when camera offline."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        stub_coord_sprintma.is_camera_online = lambda cid: False
        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        assert sw.available is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_set_ambient(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1227-1228: turn_on calls _set_ambient_light(True)."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        # Patch _set_ambient_light to verify delegation
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_on()
        sw._set_ambient_light.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_set_ambient(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1230-1231: turn_off calls _set_ambient_light(False)."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        sw._set_ambient_light = AsyncMock()
        await sw.async_turn_off()
        sw._set_ambient_light.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_set_ambient_light_http_success(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1206-1225: full GET+PUT path updates _is_on on 200."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        get_resp = MagicMock()
        get_resp.status = 200
        get_resp.json = AsyncMock(
            return_value={"ambientLightEnabled": False, "schedule": "ALL"}
        )
        put_resp = MagicMock()
        put_resp.status = 204
        get_ctx = MagicMock()
        get_ctx.__aenter__ = AsyncMock(return_value=get_resp)
        get_ctx.__aexit__ = AsyncMock(return_value=None)
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=get_ctx)
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._set_ambient_light(True)
        assert sw._is_on is True
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_ambient_light_no_token_returns_early(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 1207: return early when no token."""
        from custom_components.bosch_shc_camera.switch import BoschAmbientLightSwitch

        stub_coord_sprintma.token = None
        sw = BoschAmbientLightSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        await sw._set_ambient_light(True)  # must not raise
        assert sw._is_on is None


# ── BoschSoftLightFadingSwitch ────────────────────────────────────────────────


class TestSoftLightFadingSwitch:
    def test_is_on_reads_global_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1253-1255: reads softLightFading from global_lighting_cache."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_sprintma.global_lighting_cache[CAM_ID] = {
            "softLightFading": True,
            "darknessThreshold": 0.5,
        }
        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        assert sw.is_on is True

    def test_is_on_none_when_no_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        assert sw.is_on is None

    def test_available_requires_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1258-1263: available only when global_lighting_cache populated."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_sprintma.global_lighting_cache[CAM_ID] = {"softLightFading": False}
        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        assert sw.available is True

    def test_available_false_without_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        assert sw.available is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_put_global(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1293-1294: turn_on delegates to _put_global_lighting(True)."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        _bind_hass(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_on()
        sw._put_global_lighting.assert_awaited_once_with(True)

    @pytest.mark.asyncio
    async def test_turn_off_calls_put_global(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1296-1297: turn_off delegates to _put_global_lighting(False)."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        _bind_hass(sw)
        sw._put_global_lighting = AsyncMock()
        await sw.async_turn_off()
        sw._put_global_lighting.assert_awaited_once_with(False)

    @pytest.mark.asyncio
    async def test_put_global_lighting_success_updates_cache(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1266-1291: PUT success updates global_lighting_cache."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_sprintma.global_lighting_cache[CAM_ID] = {
            "darknessThreshold": 0.3,
            "softLightFading": False,
        }
        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        _bind_hass(sw)
        put_resp = MagicMock()
        put_resp.status = 200
        put_resp.json = AsyncMock(
            return_value={"darknessThreshold": 0.3, "softLightFading": True}
        )
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._put_global_lighting(True)
        assert (
            stub_coord_sprintma.global_lighting_cache[CAM_ID]["softLightFading"] is True
        )
        sw.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_global_lighting_no_token_returns_early(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Line 1267: return early when no token."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_sprintma.token = None
        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        _bind_hass(sw)
        await sw._put_global_lighting(True)  # must not raise

    @pytest.mark.asyncio
    async def test_put_global_lighting_non_dict_response_uses_body(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1285-1288: non-dict JSON response falls back to body dict."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_sprintma.global_lighting_cache[CAM_ID] = {
            "darknessThreshold": 0.5,
            "softLightFading": False,
        }
        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        _bind_hass(sw)
        put_resp = MagicMock()
        put_resp.status = 204
        put_resp.json = AsyncMock(return_value="ok")  # non-dict
        put_ctx = MagicMock()
        put_ctx.__aenter__ = AsyncMock(return_value=put_resp)
        put_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=put_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._put_global_lighting(True)
        # Cache should be updated with the body dict fallback
        assert (
            stub_coord_sprintma.global_lighting_cache[CAM_ID]["softLightFading"] is True
        )

    @pytest.mark.asyncio
    async def test_put_global_lighting_exception_swallowed(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1289-1290: network exception inside try block is swallowed, async_write_ha_state still called."""
        from custom_components.bosch_shc_camera.switch import BoschSoftLightFadingSwitch

        stub_coord_sprintma.global_lighting_cache[CAM_ID] = {
            "darknessThreshold": 0.5,
            "softLightFading": False,
        }
        sw = BoschSoftLightFadingSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        _bind_hass(sw)
        # Raise inside session.put (inside the try block), not before it
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(side_effect=Exception("network error"))
        failing_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session = MagicMock()
        mock_session.put = MagicMock(return_value=failing_ctx)
        with patch(
            "custom_components.bosch_shc_camera.switch.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            await sw._put_global_lighting(True)
        sw.async_write_ha_state.assert_called_once()


# ── BoschIntrusionDetectionSwitch privacy guard ───────────────────────────────


class TestIntrusionDetectionPrivacyGuard:
    @pytest.mark.asyncio
    async def test_set_intrusion_blocked_by_privacy(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1346-1350: _warn_if_privacy_on returns True → PUT not called."""
        from custom_components.bosch_shc_camera.switch import (
            BoschIntrusionDetectionSwitch,
        )

        stub_coord_sprintma.shc_state_cache[CAM_ID]["privacy_mode"] = True
        stub_coord_sprintma.intrusion_config_cache[CAM_ID] = {
            "enabled": False,
            "sensitivity": 3,
        }
        sw = BoschIntrusionDetectionSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord_sprintma.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_intrusion_allowed_when_privacy_off(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1351-1357: PUT called when privacy is OFF."""
        from custom_components.bosch_shc_camera.switch import (
            BoschIntrusionDetectionSwitch,
        )

        stub_coord_sprintma.shc_state_cache[CAM_ID]["privacy_mode"] = False
        stub_coord_sprintma.intrusion_config_cache[CAM_ID] = {
            "enabled": False,
            "sensitivity": 3,
        }
        stub_coord_sprintma.async_put_camera = AsyncMock(return_value=True)
        sw = BoschIntrusionDetectionSwitch(
            stub_coord_sprintma, CAM_ID, stub_entry_sprintma
        )
        sw.async_write_ha_state = MagicMock()
        _bind_hass(sw)
        await sw.async_turn_on()
        stub_coord_sprintma.async_put_camera.assert_awaited_once()
        assert stub_coord_sprintma.intrusion_config_cache[CAM_ID]["enabled"] is True


# ── BoschNvrRecordingSwitch.async_added_to_hass ───────────────────────────────


class TestNvrRecordingSwitchRestoreState:
    @pytest.mark.asyncio
    async def test_restores_on_state_and_sets_intent(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1777-1784: restore ON state sets nvr_user_intent."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        sw = BoschNvrRecordingSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "on"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        # Patch super().async_added_to_hass to be a no-op
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        assert stub_coord_sprintma.nvr_user_intent.get(CAM_ID) is True

    @pytest.mark.asyncio
    async def test_restores_off_state_no_intent(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1777-1784: restore OFF state leaves nvr_user_intent unchanged."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        sw = BoschNvrRecordingSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "off"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        assert stub_coord_sprintma.nvr_user_intent.get(CAM_ID) is not True

    @pytest.mark.asyncio
    async def test_no_previous_state_no_intent(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """None from async_get_last_state → no intent set."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        sw = BoschNvrRecordingSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        sw.async_get_last_state = AsyncMock(return_value=None)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        assert stub_coord_sprintma.nvr_user_intent.get(CAM_ID) is not True

    @pytest.mark.asyncio
    async def test_restores_on_and_kicks_recorder_when_live(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1788-1797: when LOCAL stream is already active, kicks off recorder task."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        stub_coord_sprintma.live_connections[CAM_ID] = {"_connection_type": "LOCAL"}
        sw = BoschNvrRecordingSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "on"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        # async_create_task must have been called to start the recorder
        sw.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_restores_on_no_kick_when_remote(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1788-1797: REMOTE stream → recorder NOT kicked off."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        stub_coord_sprintma.live_connections[CAM_ID] = {"_connection_type": "REMOTE"}
        sw = BoschNvrRecordingSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        _bind_hass(sw)
        last_state = MagicMock()
        last_state.state = "on"
        sw.async_get_last_state = AsyncMock(return_value=last_state)
        with patch.object(type(sw).__bases__[0], "async_added_to_hass", AsyncMock()):
            await sw.async_added_to_hass()
        # Recorder must NOT be kicked for REMOTE sessions
        sw.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_starts_recorder(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1799-1802: turn_on calls start_recorder."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        sw = BoschNvrRecordingSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        stub_coord_sprintma.start_recorder.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_turn_off_stops_recorder(
        self, stub_coord_sprintma: SimpleNamespace, stub_entry_sprintma: SimpleNamespace
    ):
        """Lines 1804-1807: turn_off calls stop_recorder."""
        from custom_components.bosch_shc_camera.switch import BoschNvrRecordingSwitch

        stub_entry_sprintma.options = {
            "enable_snapshot_button": True,
            "enable_nvr": True,
        }
        sw = BoschNvrRecordingSwitch(stub_coord_sprintma, CAM_ID, stub_entry_sprintma)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        stub_coord_sprintma.stop_recorder.assert_awaited_once_with(CAM_ID)


def _stub_coord_e(
    *,
    stream_warming: bool = False,
    privacy_set_at: float = float("-inf"),
    live_connections: dict | None = None,
    shc_state: dict | None = None,
) -> SimpleNamespace:
    """Coordinator stub matching the fields BoschPrivacyModeSwitch touches."""
    return SimpleNamespace(
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
        live_connections=live_connections if live_connections is not None else {},
        user_intent_streams=set(),
        shc_state_cache={
            CAM_ID: (shc_state if shc_state is not None else {"privacy_mode": False})
        },
        session_stale={},
        stream_warming={CAM_ID} if stream_warming else set(),
        privacy_set_at={CAM_ID: privacy_set_at}
        if privacy_set_at != float("-inf")
        else {},
        light_set_at={},
        audio_enabled={CAM_ID: True},
        privacy_sound_cache={CAM_ID: False},
        timestamp_cache={CAM_ID: True},
        ledlights_cache={CAM_ID: True},
        arming_cache={},
        rcp_privacy_cache={},
        last_update_success=True,
        options={},
        is_camera_online=lambda cid: True,
        is_session_stale=lambda cid: False,
        is_stream_warming=lambda cid: stream_warming,
        async_cloud_set_privacy_mode=AsyncMock(),
    )


def _stub_entry_e() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "x"},
        options={},
    )


def _make_privacy_switch(coord: SimpleNamespace, entry: SimpleNamespace | None = None):
    """Construct BoschPrivacyModeSwitch bypassing HA entity plumbing."""
    from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

    entry = entry or _stub_entry_e()
    sw = BoschPrivacyModeSwitch.__new__(BoschPrivacyModeSwitch)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._entry = entry
    sw._cam_title = "Terrasse"
    sw._model = "HOME_Eyes_Outdoor"
    sw._model_name = "Eyes Outdoor"
    sw._fw = "9.40.25"
    sw._mac = "aa:bb:cc:dd:ee:01"
    sw._pending_privacy = None
    sw._pending_apply_task = None
    sw.async_write_ha_state = MagicMock()
    return sw


class TestGen2OutdoorLightSwitches:
    """Gen2 Outdoor (HOME_Eyes_Outdoor) exposes separate front-light and
    wallwasher switch entities (distinct from the combined RGB lights)."""

    def test_gen2_outdoor_has_front_light_switch_class(self):
        """`BoschFrontLightSwitch` must exist for Gen2 outdoor."""
        from custom_components.bosch_shc_camera import switch as switch_mod

        assert hasattr(switch_mod, "BoschFrontLightSwitch"), (
            "Gen2 Outdoor needs its own front-light switch separate from the wallwasher"
        )

    def test_gen2_outdoor_has_wallwasher_switch(self):
        from custom_components.bosch_shc_camera import switch as switch_mod

        assert hasattr(switch_mod, "BoschWallwasherSwitch"), (
            "Wallwasher (top + bottom LEDs combined) must have its own "
            "switch entity for Gen2 Outdoor"
        )


class TestLiveStreamSwitchIntent:
    """BoschLiveStreamSwitch exists and reflects keepalive/session health."""

    def test_live_stream_switch_class_exists(self):
        from custom_components.bosch_shc_camera import switch as switch_mod

        assert hasattr(switch_mod, "BoschLiveStreamSwitch")

    def test_session_stale_blocks_live_stream_switch(self):
        """When `session_stale` is set for a cam, the live_stream switch
        becomes unavailable so users don't see a frozen stream as healthy."""
        from types import SimpleNamespace as _SN

        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        coord = _SN(
            data={
                CAM_ID: {
                    "info": {
                        "title": "x",
                        "hardwareVersion": "X",
                        "firmwareVersion": "x",
                        "macAddress": "x",
                    },
                    "status": "ONLINE",
                }
            },
            live_connections={},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
            session_stale={CAM_ID: True},  # keepalive given up
            last_update_success=True,
            is_camera_online=lambda cid: True,
            is_session_stale=lambda cid: True,
        )
        entry = _SN(entry_id="01", data={}, options={})
        sw = BoschLiveStreamSwitch(coord, CAM_ID, entry)
        assert sw.available is False, (
            "LiveStream switch must show unavailable when the keepalive "
            "loop has stalled — prevents users from thinking a frozen "
            "stream is healthy"
        )


class TestCooldownMessageStreamWarming:
    """_cooldown_message returns the stream-warming message when the stream
    is warming up, and the cooldown message when a recent privacy write is
    still blocking further writes."""

    def test_cooldown_message_stream_warming_branch(self) -> None:
        """When is_stream_warming returns True, _cooldown_message must
        return the 'stream is still starting' message."""
        coord = _stub_coord_e(stream_warming=True)
        sw = _make_privacy_switch(coord)

        msg = sw._cooldown_message()

        assert "stream is still starting" in msg
        assert "Terrasse" in msg

    def test_cooldown_message_cooldown_branch(self) -> None:
        """When no stream warming but privacy was recently set, returns the
        cooldown message (ensures only the stream-warming branch mentions
        'stream')."""
        import time

        coord = _stub_coord_e(stream_warming=False, privacy_set_at=time.monotonic())
        sw = _make_privacy_switch(coord)

        msg = sw._cooldown_message()

        assert "stream is still starting" not in msg
        assert "wait" in msg.lower() or "just changed" in msg.lower()


class TestPendingPrivacyLoop:
    """Tests for the three interesting branches of _pending_privacy_loop:
    the while-else timeout warning, the exception-from-flush handling, and
    the finally cleanup."""

    @pytest.mark.asyncio
    async def test_loop_timeout_warning_logged(self) -> None:
        """When _privacy_block_remaining never returns <=0 within the max
        wait, the while-else clause fires and logs a warning, then flush
        is still called."""
        coord = _stub_coord_e(stream_warming=True)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False

        # Shrink constants so the test completes quickly
        sw._PRIVACY_PENDING_MAX_WAIT = 0.05  # type: ignore[assignment]
        sw._PRIVACY_PENDING_POLL = 0.02  # type: ignore[assignment]

        # is_stream_warming stays True → block_remaining always > 0 → while-else fires
        flush_called = []

        async def _fake_flush() -> None:
            flush_called.append(True)

        sw._flush_pending_privacy = _fake_flush  # type: ignore[method-assign]

        with patch("custom_components.bosch_shc_camera.switch._LOGGER") as mock_log:
            await sw._pending_privacy_loop()

        # warning must have been emitted
        assert mock_log.warning.called
        warning_args = " ".join(
            str(a) for call in mock_log.warning.call_args_list for a in call.args
        )
        assert "still blocked" in warning_args or "applying now" in warning_args
        # flush must still have been called after the warning
        assert flush_called

    @pytest.mark.asyncio
    async def test_loop_exception_in_flush_logged_not_raised(self) -> None:
        """An Exception from _flush_pending_privacy must be caught and
        logged as a warning — the loop must NOT propagate."""
        coord = _stub_coord_e(stream_warming=False)
        # No cooldown blocking → block_remaining returns 0 → breaks immediately
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False

        async def _raise_flush() -> None:
            raise RuntimeError("simulated API failure")

        sw._flush_pending_privacy = _raise_flush  # type: ignore[method-assign]

        with patch("custom_components.bosch_shc_camera.switch._LOGGER") as mock_log:
            # Must not raise
            await sw._pending_privacy_loop()

        assert mock_log.warning.called
        warning_args = " ".join(
            str(a) for call in mock_log.warning.call_args_list for a in call.args
        )
        assert "simulated API failure" in warning_args or "failed" in warning_args

    @pytest.mark.asyncio
    async def test_loop_cancelled_error_propagates(self) -> None:
        """CancelledError must re-raise (not be swallowed)."""
        coord = _stub_coord_e(stream_warming=False)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False

        async def _cancel_flush() -> None:
            raise asyncio.CancelledError

        sw._flush_pending_privacy = _cancel_flush  # type: ignore[method-assign]

        with pytest.raises(asyncio.CancelledError):
            await sw._pending_privacy_loop()

    @pytest.mark.asyncio
    async def test_finally_clears_task_and_writes_state(self) -> None:
        """The finally block clears _pending_apply_task to None and calls
        async_write_ha_state, regardless of success or failure."""
        coord = _stub_coord_e(stream_warming=False)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = False
        # Simulate a running task
        fake_task: asyncio.Task[None] = MagicMock(spec=asyncio.Task)  # type: ignore[assignment]
        sw._pending_apply_task = fake_task

        # Happy path — flush does nothing
        async def _noop_flush() -> None:
            pass

        sw._flush_pending_privacy = _noop_flush  # type: ignore[method-assign]

        await sw._pending_privacy_loop()

        assert sw._pending_apply_task is None
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_finally_clears_task_even_on_exception(self) -> None:
        """The finally block fires even when _flush_pending_privacy raises."""
        coord = _stub_coord_e(stream_warming=False)
        sw = _make_privacy_switch(coord)
        sw._pending_privacy = True
        fake_task: asyncio.Task[None] = MagicMock(spec=asyncio.Task)  # type: ignore[assignment]
        sw._pending_apply_task = fake_task

        async def _bad_flush() -> None:
            raise ValueError("boom")

        sw._flush_pending_privacy = _bad_flush  # type: ignore[method-assign]

        # Exception is caught internally — must not propagate
        await sw._pending_privacy_loop()

        assert sw._pending_apply_task is None
        sw.async_write_ha_state.assert_called()


class TestAsyncWillRemoveFromHass:
    """async_will_remove_from_hass cancels a pending apply task on entity
    removal, but only if it isn't already done."""

    @pytest.mark.asyncio
    async def test_cancels_running_task_on_removal(self) -> None:
        """If _pending_apply_task is not done, cancel() is called."""
        coord = _stub_coord_e()
        sw = _make_privacy_switch(coord)

        task = MagicMock(spec=asyncio.Task)
        task.done.return_value = False
        task.cancel = MagicMock()
        sw._pending_apply_task = task

        # Patch super().async_will_remove_from_hass to avoid HA entity plumbing
        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

            await BoschPrivacyModeSwitch.async_will_remove_from_hass(sw)

        task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_does_not_cancel_completed_task_on_removal(self) -> None:
        """Task already done → cancel() must NOT be called."""
        coord = _stub_coord_e()
        sw = _make_privacy_switch(coord)

        task = MagicMock(spec=asyncio.Task)
        task.done.return_value = True
        task.cancel = MagicMock()
        sw._pending_apply_task = task

        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(),
        ):
            from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

            await BoschPrivacyModeSwitch.async_will_remove_from_hass(sw)

        task.cancel.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_task_removal_is_noop(self) -> None:
        """_pending_apply_task is None → no crash, super() is still called."""
        coord = _stub_coord_e()
        sw = _make_privacy_switch(coord)
        sw._pending_apply_task = None

        super_called = []

        async def _fake_super(self_inner: object) -> None:
            super_called.append(True)

        with patch(
            "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
            new=AsyncMock(side_effect=lambda: super_called.append(True)),
        ):
            from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

            await BoschPrivacyModeSwitch.async_will_remove_from_hass(sw)
        # must not raise


# Section: v12.4.13 LAN-fallback hardening — privacy availability during a
# cloud outage (relocated from tests/test_lan_fallback_during_outage.py; the
# rcp.py transport half lives in tests/test_rcp.py, the shc.py half in
# tests/test_shc.py)


class TestPrivacyAvailableWhenHwUnknown:
    """`BoschPrivacyModeSwitch.available` must return True when the camera
    is LAN-reachable + hw_version is unknown (cold-start during a cloud
    outage), and False when hw_version is a KNOWN Gen1 (no LAN RCP
    endpoint)."""

    def test_available_when_hw_unknown_and_lan_reachable(self):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        coord = SimpleNamespace(
            last_update_success=False,
            shc_state_cache={},
            hw_version={},  # empty — cold start
            is_lan_reachable=lambda cid: True,
            is_camera_online=lambda cid: True,
        )
        sw = SimpleNamespace(coordinator=coord, _cam_id=CAM_ID)
        with patch.object(
            BoschPrivacyModeSwitch.__bases__[0],
            "available",
            new_callable=lambda: property(lambda _self: True),
        ):
            result = BoschPrivacyModeSwitch.available.fget(sw)
        assert result is True

    def test_unavailable_when_gen1_known_and_cloud_down(self):
        """If we KNOW it's Gen1, deny — no LAN RCP endpoint."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        coord = SimpleNamespace(
            last_update_success=False,
            shc_state_cache={},
            hw_version={CAM_ID: "CAMERA_EYES"},  # Gen1
            is_lan_reachable=lambda cid: True,
            is_camera_online=lambda cid: True,
        )
        sw = SimpleNamespace(coordinator=coord, _cam_id=CAM_ID)
        with patch.object(
            BoschPrivacyModeSwitch.__bases__[0],
            "available",
            new_callable=lambda: property(lambda _self: True),
        ):
            result = BoschPrivacyModeSwitch.available.fget(sw)
        assert result is False, (
            "Gen1 cam must not show available during a cloud outage — "
            "LAN RCP is Gen2-only, Gen1 has no rcp.xml endpoint."
        )


# Section: cheap availability-property pins (relocated from
# tests/test_misc_small_gaps.py)


class TestSwitchAvailabilityGaps:
    def test_panic_alarm_available_requires_coordinator_success_and_online(self):
        """Panic-alarm switch must be unavailable when the coordinator failed
        OR the camera is offline."""
        from custom_components.bosch_shc_camera.switch import (
            BoschPanicAlarmSwitch,
        )

        sw = BoschPanicAlarmSwitch.__new__(BoschPanicAlarmSwitch)
        sw._cam_id = "C"
        coord = SimpleNamespace(
            last_update_success=True,
            is_camera_online=lambda _c: True,
        )
        sw.coordinator = coord
        assert sw.available is True
        coord.last_update_success = False
        assert sw.available is False
        coord.last_update_success = True
        coord.is_camera_online = lambda _c: False
        assert sw.available is False

    def test_external_stream_available_follows_coordinator(self):
        """External-stream URL switch is unconditional once the coordinator
        is healthy."""
        from custom_components.bosch_shc_camera.switch import (
            BoschExternalStreamSwitch,
        )

        sw = BoschExternalStreamSwitch.__new__(BoschExternalStreamSwitch)
        sw._cam_id = "C"
        sw.coordinator = SimpleNamespace(last_update_success=True)
        assert sw.available is True
        sw.coordinator.last_update_success = False
        assert sw.available is False


# Section: panic-alarm privacy guard + failed-PUT warning (relocated from
# tests/test_privacy_guard_branches.py — the light.py/number.py siblings
# live in tests/test_light.py and tests/test_number.py)


def _stub_coord_with_privacy_panic(
    privacy_on: bool = False, hw: str = "HOME_Eyes_Indoor"
):
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                },
            }
        },
        shc_state_cache={CAM_ID: {"privacy_mode": privacy_on}},
        panic_alarm_cache={},
        alarm_settings_cache={},
        alarm_settings_set_at={},
        lighting_switch_cache={},
        light_set_at={},
        last_update_success=True,
        token="tok-A",
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
        async_update_listeners=MagicMock(),
    )


def _hass_stub_panic():
    svc = SimpleNamespace(async_call=AsyncMock())
    return SimpleNamespace(services=svc)


class TestPanicAlarmPrivacyGuardAndFailedPut:
    """Covers the privacy-guard `return` on `_set(True)` and the
    failed-PUT warning path (cache not updated when the API PUT fails)."""

    def _make_switch(self, coord):
        from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        entity = BoschPanicAlarmSwitch(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity.hass = _hass_stub_panic()
        return entity

    @pytest.mark.asyncio
    async def test_set_blocked_when_privacy_on(self):
        """_set(True) with privacy ON must not call async_put_camera."""
        coord = _stub_coord_with_privacy_panic(privacy_on=True)
        entity = self._make_switch(coord)

        await entity._set(True)

        coord.async_put_camera.assert_not_called()
        assert coord.panic_alarm_cache.get(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_set_false_skips_privacy_guard(self):
        """_set(False) must skip the privacy guard (guard only fires for `enabled=True`)."""
        coord = _stub_coord_with_privacy_panic(privacy_on=True)
        entity = self._make_switch(coord)

        await entity._set(False)

        coord.async_put_camera.assert_called_once()

    @pytest.mark.asyncio
    async def test_failed_put_logs_warning_and_does_not_set_cache(self):
        """When async_put_camera returns False a warning is logged and the
        cache must NOT be set to the new state."""
        coord = _stub_coord_with_privacy_panic(privacy_on=False)
        coord.async_put_camera = AsyncMock(return_value=False)
        entity = self._make_switch(coord)

        await entity._set(True)

        coord.async_put_camera.assert_called_once()
        assert coord.panic_alarm_cache.get(CAM_ID) is not True

    @pytest.mark.asyncio
    async def test_successful_put_sets_cache(self):
        coord = _stub_coord_with_privacy_panic(privacy_on=False)
        entity = self._make_switch(coord)

        await entity._set(True)

        assert coord.panic_alarm_cache[CAM_ID] is True


# Section: firmware-install unavailability (relocated from
# tests/test_updating_unavailable.py — the camera.py/init.py/light.py
# siblings live in tests/test_camera.py, tests/test_init.py, and
# tests/test_light.py)


def _coord_updating(
    *, is_updating_value: bool, last_update_success: bool = True
) -> SimpleNamespace:
    return SimpleNamespace(
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
        firmware_cache={CAM_ID: {"updating": is_updating_value}},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
        lan_tcp_reachable={CAM_ID: (True, 0.0)},
        is_lan_reachable=lambda cam_id: True,
        is_session_stale=lambda cam_id: False,
        user_intent_streams=set(),
    )


class TestLiveStreamSwitchUpdatingUnavailable:
    def _mk_switch(self, coord):
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        sw = BoschLiveStreamSwitch.__new__(BoschLiveStreamSwitch)
        sw.coordinator = coord
        sw._cam_id = CAM_ID
        return sw

    def test_available_when_not_updating(self):
        """Live-stream switch is available when the camera is healthy and not
        mid firmware-install."""
        coord = _coord_updating(is_updating_value=False)
        sw = self._mk_switch(coord)
        with patch(
            "custom_components.bosch_shc_camera.switch._BoschSwitchBase.available",
            new_callable=PropertyMock,
            return_value=True,
        ):
            assert sw.available is True

    def test_unavailable_when_updating(self):
        """Live stream cannot start on a rebooting camera — the is_updating
        guard short-circuits before super().available."""
        coord = _coord_updating(is_updating_value=True)
        sw = self._mk_switch(coord)
        assert sw.available is False


class TestPrivacySwitchUpdatingUnavailable:
    def _mk_switch(self, coord):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        sw = BoschPrivacyModeSwitch.__new__(BoschPrivacyModeSwitch)
        sw.coordinator = coord
        sw._cam_id = CAM_ID
        return sw

    def test_available_when_not_updating(self):
        sw = self._mk_switch(_coord_updating(is_updating_value=False))
        assert sw.available is True

    def test_unavailable_when_updating(self):
        """Privacy mode toggle writes to the camera — fails mid-reboot."""
        sw = self._mk_switch(_coord_updating(is_updating_value=True))
        assert sw.available is False


# Section: concurrent-start false "Live stream failed" alarm (relocated from
# tests/test_stream_start_in_progress.py — the STREAM_START_SKIPPED
# coordinator-side contract lives in tests/test_init.py, the sentinel
# falsy/singleton contract and the const.py definition are exercised
# inline here since STREAM_START_SKIPPED is a shared symbol)


def _make_switch_stub_skip(coord):
    from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

    return SimpleNamespace(
        coordinator=coord,
        _cam_id=CAM_ID,
        _cam_title="Innenbereich",
        _last_stream_off=float("-inf"),  # never stopped (SENTINEL_RULE)
        _STREAM_COOLDOWN=BoschLiveStreamSwitch._STREAM_COOLDOWN,
        async_write_ha_state=MagicMock(),
        hass=MagicMock(),
    )


class TestTurnOnSkipIsNoOp:
    """A de-duplicated (coalesced) concurrent stream start must be treated as
    a benign no-op by the switch, not as a real failure — the reported bug
    dropped the user's stream intent and recorded a false stream error on
    every coalesced start."""

    @pytest.mark.asyncio
    async def test_skip_keeps_intent_and_records_no_error(self):
        from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        record_error = MagicMock()
        coord = SimpleNamespace(
            shc_state_cache={},
            user_intent_streams=set(),
            try_live_connection=AsyncMock(return_value=STREAM_START_SKIPPED),
            record_stream_error=record_error,
            bg_tasks=set(),
        )
        switch_stub = _make_switch_stub_skip(coord)

        await BoschLiveStreamSwitch.async_turn_on(switch_stub)

        assert CAM_ID in coord.user_intent_streams, (
            "A coalesced (in-progress) start must not drop the user intent "
            "that the in-flight start legitimately set."
        )
        record_error.assert_not_called()
        switch_stub.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_real_failure_still_reverts_intent_and_records_error(self):
        """Contrast/guard: a genuine None failure must STILL revert intent
        and record an error — the fix must not swallow real failures."""
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        record_error = MagicMock()
        coord = SimpleNamespace(
            shc_state_cache={},
            user_intent_streams=set(),
            try_live_connection=AsyncMock(return_value=None),  # real failure
            record_stream_error=record_error,
            bg_tasks=set(),
        )
        switch_stub = _make_switch_stub_skip(coord)

        await BoschLiveStreamSwitch.async_turn_on(switch_stub)

        assert CAM_ID not in coord.user_intent_streams
        record_error.assert_called_once_with(CAM_ID)
