"""Regression: privacy write failure must always notify the user (2026-07-07).

Live-reproduced by Thomas: a transient Bosch-cloud connection failure while
toggling `switch.bosch_terrasse_privacy_mode` left the switch looking like it
"does nothing" — no error, no notification, the button just silently
reverted. Root cause (two-part):

  1. `async_cloud_set_privacy_mode`'s final persistent_notification was gated
     on `coordinator._auth_outage_count > 0` — a counter that only tracks
     consecutive 5xx responses from the coordinator's own *polling* loop
     (`__init__.py`). A one-off write-time failure (a single
     ClientConnectorError while the user is pressing the switch) never
     touches that counter, so the notification never fired even though every
     fallback (cloud, Gen2 LOCAL RCP, SHC) had already been exhausted.
  2. `BoschPrivacyModeSwitch._apply_privacy` discarded the boolean result of
     `async_cloud_set_privacy_mode` entirely — a total failure was invisible
     even in the logs.

Fix: the notification now fires unconditionally when all paths fail (the
notification_id is deterministic per camera, so repeated failures during a
real outage overwrite the same entry rather than spamming), and the switch
logs a WARNING when the write comes back False.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _coord(**overrides: object) -> SimpleNamespace:
    coord = SimpleNamespace(
        token="tok-AAA",
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        _shc_state_cache={CAM_ID: {}},
        _privacy_set_at={},
        _local_creds_cache={},
        _rcp_lan_ip_cache={},
        _hw_version={},
        _cached_status={},
        _auth_outage_count=0,  # no consecutive polling 5xx recorded
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
        write paths exhausted. `_auth_outage_count` is still 0 (this was a
        single ad-hoc failure, not a run of polling 5xxs) — the
        notification must fire anyway.
        """
        from custom_components.bosch_shc_camera import shc

        coord = _coord()
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
        """Bug found by bug-hunt verification (2026-07-07): the SHC fallback
        branch used to `return await async_shc_set_privacy_mode(...)`
        directly — if SHC was reachable (`shc_ready` True) but its own local
        write also failed (e.g. no cached device_id yet), that `False` was
        handed straight back to the caller, *skipping* the notification
        tail entirely. This reproduced the exact bug the notification fix
        was written to close, just for this one sub-case.
        """
        from custom_components.bosch_shc_camera import shc

        coord = _coord()
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

        coord = _coord()
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

        coord = _coord()
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

        coord = _coord()
        coord._hw_version = {CAM_ID: "OUTDOOR"}  # Gen1 path (single PUT)
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
    async def test_apply_privacy_logs_warning_when_write_fails(self, caplog):
        """`_apply_privacy` must not silently discard a False result — pin a
        visible WARNING so the failure isn't only buried in a notification.
        """
        from custom_components.bosch_shc_camera.switch import (
            BoschPrivacyModeSwitch,
        )

        coord = SimpleNamespace(
            _live_connections={},
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
    async def test_apply_privacy_silent_when_write_succeeds(self, caplog):
        """No warning noise on the (normal) success path."""
        from custom_components.bosch_shc_camera.switch import (
            BoschPrivacyModeSwitch,
        )

        coord = SimpleNamespace(
            _live_connections={},
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
