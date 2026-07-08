"""Regression tests for BoschCameraCoordinator.async_install_firmware().

Shared by two entry points: update.py's BoschFirmwareUpdate.async_install
(the update entity's Install button) and repairs.py's
FirmwareUpdateRepairFlow (the "Fix" action on the firmware_update_available
Repairs issue) — one implementation so both stay in sync instead of
duplicating the guard/write-lock logic (previously lived only in update.py).

Pins every mode:
  - happy path: PUTs the cached `update` field as {"id": ...}
  - no update cached / upToDate=True -> raises, no PUT
  - already updating -> raises immediately, no PUT (double-press guard)
  - cloud rejects the PUT -> raises, no optimistic lock written
  - success -> cache optimistically marked updating=True + write-locked
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(firmware: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        _firmware_cache={CAM_ID: dict(firmware)} if firmware is not None else {},
        _firmware_set_at={},
        async_put_camera=AsyncMock(return_value=True),
    )


def _call(coord: SimpleNamespace, cam_id: str = CAM_ID):
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    return BoschCameraCoordinator.async_install_firmware(coord, cam_id)  # type: ignore[arg-type]


class TestAsyncInstallFirmware:
    @pytest.mark.asyncio
    async def test_puts_update_field_as_id(self):
        coord = _make_coord(
            {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}
        )

        await _call(coord)

        coord.async_put_camera.assert_awaited_once_with(
            CAM_ID, "firmware", {"id": "9.40.104"}
        )

    @pytest.mark.asyncio
    async def test_no_update_available_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coord({"current": "9.40.104", "upToDate": True})

        with pytest.raises(HomeAssistantError, match="No firmware update"):
            await _call(coord)

        coord.async_put_camera.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_cache_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coord(None)

        with pytest.raises(HomeAssistantError, match="No firmware update"):
            await _call(coord)

        coord.async_put_camera.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_updating_raises_and_skips_put(self):
        """A second install attempt while one is already in progress must not
        fire a second PUT at the real camera (bug-hunt finding: double-press /
        update.install service-call race, no slow-tier poll refresh yet)."""
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coord(
            {
                "current": "9.40.102",
                "upToDate": False,
                "update": "9.40.104",
                "updating": True,
            }
        )

        with pytest.raises(HomeAssistantError, match="already in progress"):
            await _call(coord)

        coord.async_put_camera.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cloud_rejects_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coord(
            {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}
        )
        coord.async_put_camera.return_value = False

        with pytest.raises(HomeAssistantError, match="rejected"):
            await _call(coord)

    @pytest.mark.asyncio
    async def test_success_optimistically_locks_updating_state(self):
        """On success, the cache is marked updating=True and write-locked so a
        slow-tier poll landing seconds later can't revert it to stale state
        before Bosch's backend actually reports the install in progress."""
        coord = _make_coord(
            {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}
        )

        await _call(coord)

        assert coord._firmware_cache[CAM_ID]["updating"] is True
        assert CAM_ID in coord._firmware_set_at

    @pytest.mark.asyncio
    async def test_failure_does_not_lock_updating_state(self):
        """A rejected PUT must NOT optimistically mark updating=True — nothing
        was actually queued on the camera."""
        from homeassistant.exceptions import HomeAssistantError

        coord = _make_coord(
            {"current": "9.40.102", "upToDate": False, "update": "9.40.104"}
        )
        coord.async_put_camera.return_value = False

        with pytest.raises(HomeAssistantError, match="rejected"):
            await _call(coord)

        assert "updating" not in coord._firmware_cache[CAM_ID]
        assert CAM_ID not in coord._firmware_set_at
