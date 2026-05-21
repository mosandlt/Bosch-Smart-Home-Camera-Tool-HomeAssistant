"""Coverage tests for `async_self_heal_fcm_push` (FCM watchdog recovery)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_coord(*, entry_data: dict | None = None, enable_fcm: bool = True) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord._entry = SimpleNamespace(data=dict(entry_data or {}))
    coord.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    coord.options = {"enable_fcm_push": enable_fcm}
    coord._fcm_start_lock = asyncio.Lock()
    return coord


@pytest.mark.asyncio
class TestFcmSelfHeal:
    async def test_purges_creds_and_restarts(self):
        from custom_components.bosch_shc_camera import fcm
        coord = _make_coord(entry_data={
            "fcm_credentials": {"gcm": "x"},
            "fcm_registered_token": "stale-token",
            "bearer_token": "bt",     # non-fcm key must survive
        })
        stop_mock = AsyncMock()
        start_mock = AsyncMock()
        reset_mock = MagicMock()
        with patch.object(fcm, "async_stop_fcm_push", stop_mock), \
             patch.object(fcm, "_async_start_fcm_push_locked", start_mock), \
             patch.object(fcm, "reset_fcm_error_counter", reset_mock):
            await fcm.async_self_heal_fcm_push(coord)
        stop_mock.assert_awaited_once_with(coord)
        coord.hass.config_entries.async_update_entry.assert_called_once()
        new_data = coord.hass.config_entries.async_update_entry.call_args.kwargs.get("data")
        assert "fcm_credentials" not in new_data
        assert "fcm_registered_token" not in new_data
        assert new_data["bearer_token"] == "bt", "non-fcm keys must survive the purge"
        reset_mock.assert_called_once()
        start_mock.assert_awaited_once_with(coord)

    async def test_purges_all_fcm_prefix_keys(self):
        """Live bug 2026-05-21: leaving fcm_config and fcm_registered_device_type
        behind kept the integration in a PHONE_REGISTRATION_ERROR loop. Self-heal
        must purge EVERY key beginning with 'fcm_', not just the two originally
        hardcoded ones."""
        from custom_components.bosch_shc_camera import fcm
        coord = _make_coord(entry_data={
            "fcm_credentials": {"gcm": "x"},
            "fcm_registered_token": "tok",
            "fcm_config": {"project_id": "p", "app_id": "a", "api_key": "k"},
            "fcm_registered_device_type": "ANDROID",
            "fcm_anything_future": "should_also_go",
            "bearer_token": "bt",
            "refresh_token": "rt",
        })
        with patch.object(fcm, "async_stop_fcm_push", AsyncMock()), \
             patch.object(fcm, "_async_start_fcm_push_locked", AsyncMock()), \
             patch.object(fcm, "reset_fcm_error_counter", MagicMock()):
            await fcm.async_self_heal_fcm_push(coord)
        new_data = coord.hass.config_entries.async_update_entry.call_args.kwargs.get("data")
        # All fcm_* keys must be gone:
        for k in ("fcm_credentials", "fcm_registered_token", "fcm_config",
                  "fcm_registered_device_type", "fcm_anything_future"):
            assert k not in new_data, f"{k!r} must be purged by self-heal"
        # Non-fcm keys must survive:
        assert new_data["bearer_token"] == "bt"
        assert new_data["refresh_token"] == "rt"

    async def test_does_not_restart_when_fcm_disabled(self):
        from custom_components.bosch_shc_camera import fcm
        coord = _make_coord(
            entry_data={"fcm_credentials": {"gcm": "x"}},
            enable_fcm=False,
        )
        stop_mock = AsyncMock()
        start_mock = AsyncMock()
        with patch.object(fcm, "async_stop_fcm_push", stop_mock), \
             patch.object(fcm, "_async_start_fcm_push_locked", start_mock), \
             patch.object(fcm, "reset_fcm_error_counter", MagicMock()):
            await fcm.async_self_heal_fcm_push(coord)
        stop_mock.assert_awaited_once()
        start_mock.assert_not_awaited()

    async def test_works_when_no_creds_present(self):
        """Self-heal must be idempotent — calling it on a freshly-installed
        integration without stored creds must not raise."""
        from custom_components.bosch_shc_camera import fcm
        coord = _make_coord(entry_data=None)
        with patch.object(fcm, "async_stop_fcm_push", AsyncMock()), \
             patch.object(fcm, "_async_start_fcm_push_locked", AsyncMock()), \
             patch.object(fcm, "reset_fcm_error_counter", MagicMock()):
            # Must not raise.
            await fcm.async_self_heal_fcm_push(coord)

    async def test_lock_serialises_concurrent_starts(self):
        """Live bug 2026-05-21: setup-time start + watchdog self-heal raced and
        registered two device tokens in 2 s. The shared lock must collapse
        concurrent callers: the second observes `_fcm_running=True` and returns
        without a second checkin_or_register()."""
        from custom_components.bosch_shc_camera import fcm

        coord = _make_coord(entry_data={"fcm_config": {"api_key": "k", "project_id": "p", "app_id": "a"}})
        # Simulate a coordinator that has not started FCM yet.
        coord._fcm_running = False
        coord.options = {"enable_fcm_push": True}

        # Replace `_async_start_fcm_push_locked` with a stub that sets _fcm_running=True
        # the moment it runs — mimicking what the real function does on success.
        call_count = {"n": 0}

        async def fake_locked(c):
            # Mirror the real `_async_start_fcm_push_locked` guard so the
            # second caller (after the lock releases) observes the running
            # flag and returns without re-registering.
            if c._fcm_running:
                return
            call_count["n"] += 1
            # Yield to the event loop so the lock release/re-acquire actually
            # happens between the two callers.
            await asyncio.sleep(0)
            c._fcm_running = True

        with patch.object(fcm, "_async_start_fcm_push_locked", side_effect=fake_locked):
            # Two concurrent starts — only the first should actually register.
            await asyncio.gather(
                fcm.async_start_fcm_push(coord),
                fcm.async_start_fcm_push(coord),
            )

        assert call_count["n"] == 1, (
            "Lock failed to serialise: both concurrent callers ran the registration path. "
            "This is the live bug from 2026-05-21 where two device tokens got registered."
        )
