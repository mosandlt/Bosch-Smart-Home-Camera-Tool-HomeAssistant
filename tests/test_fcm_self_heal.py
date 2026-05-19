"""Coverage tests for `async_self_heal_fcm_push` (FCM watchdog recovery)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_coord(*, fcm_creds: dict | None = None, enable_fcm: bool = True) -> SimpleNamespace:
    coord = SimpleNamespace()
    entry_data = {}
    if fcm_creds:
        entry_data["fcm_credentials"] = fcm_creds
        entry_data["fcm_registered_token"] = "stale-token"
    coord._entry = SimpleNamespace(data=entry_data)
    coord.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    coord.options = {"enable_fcm_push": enable_fcm}
    return coord


@pytest.mark.asyncio
class TestFcmSelfHeal:
    async def test_purges_creds_and_restarts(self):
        from custom_components.bosch_shc_camera import fcm
        coord = _make_coord(fcm_creds={"gcm": "x"})
        stop_mock = AsyncMock()
        start_mock = AsyncMock()
        reset_mock = MagicMock()
        with patch.object(fcm, "async_stop_fcm_push", stop_mock), \
             patch.object(fcm, "async_start_fcm_push", start_mock), \
             patch.object(fcm, "reset_fcm_error_counter", reset_mock):
            await fcm.async_self_heal_fcm_push(coord)
        stop_mock.assert_awaited_once_with(coord)
        # Entry was updated with the cleared dict.
        coord.hass.config_entries.async_update_entry.assert_called_once()
        new_data = coord.hass.config_entries.async_update_entry.call_args.kwargs.get("data")
        assert "fcm_credentials" not in new_data
        assert "fcm_registered_token" not in new_data
        reset_mock.assert_called_once()
        start_mock.assert_awaited_once_with(coord)

    async def test_does_not_restart_when_fcm_disabled(self):
        from custom_components.bosch_shc_camera import fcm
        coord = _make_coord(fcm_creds={"gcm": "x"}, enable_fcm=False)
        stop_mock = AsyncMock()
        start_mock = AsyncMock()
        with patch.object(fcm, "async_stop_fcm_push", stop_mock), \
             patch.object(fcm, "async_start_fcm_push", start_mock), \
             patch.object(fcm, "reset_fcm_error_counter", MagicMock()):
            await fcm.async_self_heal_fcm_push(coord)
        stop_mock.assert_awaited_once()
        start_mock.assert_not_awaited()

    async def test_works_when_no_creds_present(self):
        """Self-heal must be idempotent — calling it on a freshly-installed
        integration without stored creds must not raise."""
        from custom_components.bosch_shc_camera import fcm
        coord = _make_coord(fcm_creds=None)
        with patch.object(fcm, "async_stop_fcm_push", AsyncMock()), \
             patch.object(fcm, "async_start_fcm_push", AsyncMock()), \
             patch.object(fcm, "reset_fcm_error_counter", MagicMock()):
            # Must not raise.
            await fcm.async_self_heal_fcm_push(coord)
