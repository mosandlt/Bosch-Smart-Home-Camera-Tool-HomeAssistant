"""Regression tests for the 4 follow-up cache-race bugs documented in v11.0.1.

Bug shape (same as PRIVACY_REVERT): user toggles a switch → optimistic
cache write on PUT success → next coordinator slow-tier poll within the
cloud's eventual-consistency window returns the stale value → cache
clobbered → switch UI flickers back to the old value for ~1 tick.

Fix: each user-write path now records `_<field>_set_at[cam_id] =
time.monotonic()`, and the corresponding handler in the coordinator's
`_async_update_data` calls `self._is_write_locked(cam_id, set_at_dict)`
before writing to the cache.

This file pins the contract: a fresh user-write must NOT be overwritten
by a stale coordinator-poll value within `_WRITE_LOCK_SECS`.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def coord_with_helpers():
    """Build a coordinator-shaped stub with the real `_is_write_locked` method bound."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = SimpleNamespace(
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        _arming_set_at={},
        _privacy_set_at={},
        _light_set_at={},
        _notif_set_at={},
        _audio_alarm_set_at={},
        _WRITE_LOCK_SECS=30.0,
    )
    return coord, BoschCameraCoordinator._is_write_locked


# ── _is_write_locked helper itself ──────────────────────────────────────


class TestIsWriteLocked:
    def test_no_entry_returns_false(self, coord_with_helpers):
        coord, helper = coord_with_helpers
        assert helper(coord, CAM_ID, coord._privacy_sound_set_at) is False

    def test_fresh_write_returns_true(self, coord_with_helpers):
        coord, helper = coord_with_helpers
        coord._privacy_sound_set_at[CAM_ID] = time.monotonic()
        assert helper(coord, CAM_ID, coord._privacy_sound_set_at) is True

    def test_old_write_returns_false(self, coord_with_helpers):
        coord, helper = coord_with_helpers
        coord._privacy_sound_set_at[CAM_ID] = time.monotonic() - 60.0
        assert helper(coord, CAM_ID, coord._privacy_sound_set_at) is False

    def test_lock_window_boundary(self, coord_with_helpers):
        """At exactly TTL seconds, lock has expired."""
        coord, helper = coord_with_helpers
        coord._privacy_sound_set_at[CAM_ID] = time.monotonic() - 30.0
        assert helper(coord, CAM_ID, coord._privacy_sound_set_at) is False

    def test_lock_works_on_other_field_dicts(self, coord_with_helpers):
        """Helper is generic — works for every set_at dict."""
        coord, helper = coord_with_helpers
        coord._arming_set_at[CAM_ID] = time.monotonic()
        coord._timestamp_set_at[CAM_ID] = time.monotonic()
        assert helper(coord, CAM_ID, coord._arming_set_at) is True
        assert helper(coord, CAM_ID, coord._timestamp_set_at) is True


# ── Switch turn_on/turn_off records timestamps ──────────────────────────


def _switch_stub_coord():
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse", "hardwareVersion": "HOME_Eyes_Outdoor"}}},
        _privacy_sound_cache={},
        _timestamp_cache={},
        _ledlights_cache={},
        _arming_cache={},
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},
        _arming_set_at={},
        last_update_success=True,
        async_put_camera=None,  # patched in test
    )


class TestPrivacySoundSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        """User toggles privacy_sound ON → cache + set_at populated together."""
        from unittest.mock import AsyncMock
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschPrivacySoundSwitch
        sw = BoschPrivacySoundSwitch(coord, CAM_ID, entry)
        # Patch async_write_ha_state since we're not in HA context
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert coord._privacy_sound_cache[CAM_ID] is True
        assert CAM_ID in coord._privacy_sound_set_at
        # Timestamp must be recent (within last second)
        assert time.monotonic() - coord._privacy_sound_set_at[CAM_ID] < 1.0


class TestTimestampSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        from unittest.mock import AsyncMock
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschTimestampSwitch
        sw = BoschTimestampSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert CAM_ID in coord._timestamp_set_at


class TestStatusLedSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        from unittest.mock import AsyncMock
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschStatusLedSwitch
        sw = BoschStatusLedSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert CAM_ID in coord._ledlights_set_at


class TestArmingSwitchRecordsTimestamp:
    @pytest.mark.asyncio
    async def test_turn_on_records_set_at(self):
        """User arms the alarm system → cache + set_at populated."""
        from unittest.mock import AsyncMock
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        sw = BoschAlarmSystemArmSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert coord._arming_cache[CAM_ID] is True
        assert CAM_ID in coord._arming_set_at

    @pytest.mark.asyncio
    async def test_failed_put_does_not_record(self):
        """If the cloud PUT fails, neither the cache nor the timestamp must change."""
        from unittest.mock import AsyncMock
        coord = _switch_stub_coord()
        coord.async_put_camera = AsyncMock(return_value=False)
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        from custom_components.bosch_shc_camera.switch import BoschAlarmSystemArmSwitch
        sw = BoschAlarmSystemArmSwitch(coord, CAM_ID, entry)
        sw.async_write_ha_state = lambda: None
        await sw.async_turn_on()
        assert CAM_ID not in coord._arming_cache
        assert CAM_ID not in coord._arming_set_at
