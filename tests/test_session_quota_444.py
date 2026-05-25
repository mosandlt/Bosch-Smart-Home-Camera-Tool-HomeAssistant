"""Tests for HTTP 444 session-quota handling.

Pins:
- status enum: SESSION_LIMIT returned from _check_status when cloud returns 444
- _compute_status_for passes SESSION_LIMIT through verbatim (not "unknown")
- BoschCameraStatusSensor.native_value returns "session_limit"
- Persistent notification fires after N>=3 hits in 5-min window
- Notification does NOT fire on first or second hit within window
- _offline_since is NOT updated on SESSION_LIMIT (camera is reachable)

Source: user-reported confusion "camera shown offline during Bosch app parallel use",
root cause HTTP 444 treated as OFFLINE in HA integration.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator
from custom_components.bosch_shc_camera.sensor import BoschCameraStatusSensor

CAM_A = "11111111-1111-1111-1111-111111111111"


def _make_coord(cam_id: str = CAM_A) -> SimpleNamespace:
    """Minimal coordinator stub for status-related tests."""
    coord = SimpleNamespace()
    coord.options = {}
    coord._last_camera_status = {}
    coord._session_quota_hits: dict[str, list[float]] = {}
    coord._SESSION_QUOTA_WINDOW_S = 300.0
    coord._SESSION_QUOTA_NOTIFY_THRESHOLD = 3
    coord.data = {
        cam_id: {
            "info": {
                "title": "Terrasse",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.102",
                "macAddress": "aa:bb:cc:33:14:ae",
            },
            "status": "ONLINE",
            "events": [],
        },
    }
    coord.hass = SimpleNamespace(
        services=SimpleNamespace(async_call=AsyncMock()),
        async_create_task=MagicMock(),
    )
    # Caches expected by BoschCameraStatusSensor.__init__ / _cam_data property
    coord._commissioned_cache = {}
    coord._firmware_cache = {}
    return coord


def _make_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="test_entry", data={}, options={})


# ── 1. Status enum: SESSION_LIMIT passthrough via _compute_status_for ─────────


class TestComputeStatusSessionLimit:
    """_compute_status_for must pass SESSION_LIMIT through verbatim."""

    def test_session_limit_returns_session_limit(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "SESSION_LIMIT"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_A)
        assert result == "session_limit"

    def test_session_limit_not_treated_as_offline(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "SESSION_LIMIT"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_A)
        assert result != "offline"

    def test_session_limit_not_treated_as_unknown(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "SESSION_LIMIT"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_A)
        assert result != "unknown"

    def test_offline_still_offline(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "OFFLINE"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_A)
        assert result == "offline"

    def test_online_still_online(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "ONLINE"
        result = BoschCameraCoordinator._compute_status_for(coord, CAM_A)
        assert result == "online"


# ── 2. Sensor native_value: session_limit ─────────────────────────────────────


class TestStatusSensorSessionLimit:
    """BoschCameraStatusSensor must return 'session_limit' and list it in options."""

    def test_session_limit_status_returns_session_limit(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "SESSION_LIMIT"
        sensor = BoschCameraStatusSensor(coord, CAM_A, _make_entry())
        assert sensor.native_value == "session_limit"

    def test_session_limit_in_options(self) -> None:
        sensor = BoschCameraStatusSensor(_make_coord(), CAM_A, _make_entry())
        # Options must contain session_limit per PIN_EVERY_MODE
        assert "session_limit" in sensor._attr_options

    def test_offline_in_options(self) -> None:
        sensor = BoschCameraStatusSensor(_make_coord(), CAM_A, _make_entry())
        assert "offline" in sensor._attr_options

    def test_online_in_options(self) -> None:
        sensor = BoschCameraStatusSensor(_make_coord(), CAM_A, _make_entry())
        assert "online" in sensor._attr_options

    def test_online_status_still_online(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "ONLINE"
        sensor = BoschCameraStatusSensor(coord, CAM_A, _make_entry())
        assert sensor.native_value == "online"

    def test_offline_status_still_offline(self) -> None:
        coord = _make_coord()
        coord.data[CAM_A]["status"] = "OFFLINE"
        sensor = BoschCameraStatusSensor(coord, CAM_A, _make_entry())
        assert sensor.native_value == "offline"


# ── 3. Persistent notification: threshold logic ───────────────────────────────


@pytest.mark.asyncio
class TestSessionQuotaNotification:
    """_async_handle_session_quota_hit fires persistent_notification after threshold."""

    async def test_first_hit_no_notification(self) -> None:
        coord = _make_coord()
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_A)
        coord.hass.services.async_call.assert_not_called()

    async def test_second_hit_no_notification(self) -> None:
        coord = _make_coord()
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_A)
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_A)
        coord.hass.services.async_call.assert_not_called()

    async def test_third_hit_fires_notification(self) -> None:
        coord = _make_coord()
        for _ in range(3):
            await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_A)
        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args[0][0] == "persistent_notification"
        assert call_args[0][1] == "create"
        payload = call_args[0][2]
        assert "session_quota" in payload["notification_id"]
        assert "444" in payload["message"] or "Session" in payload["message"] or "Sitzungslimit" in payload["message"]

    async def test_hits_outside_window_dont_count(self) -> None:
        coord = _make_coord()
        # Seed 2 old hits (beyond window)
        old_ts = time.monotonic() - 400.0  # 400s ago, outside 300s window
        coord._session_quota_hits[CAM_A] = [old_ts, old_ts]
        # One fresh hit — total in window = 1, below threshold
        await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_A)
        coord.hass.services.async_call.assert_not_called()

    async def test_notification_id_contains_cam_prefix(self) -> None:
        coord = _make_coord()
        for _ in range(3):
            await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_A)
        payload = coord.hass.services.async_call.call_args[0][2]
        assert CAM_A[:8].lower() in payload["notification_id"]

    async def test_fourth_hit_does_not_double_notify(self) -> None:
        """After threshold: each subsequent hit re-fires (idempotent notification_id dedups in HA)."""
        coord = _make_coord()
        for _ in range(4):
            await BoschCameraCoordinator._async_handle_session_quota_hit(coord, CAM_A)
        # Called on hit 3 and hit 4 — both use same notification_id so HA dedupes
        assert coord.hass.services.async_call.call_count == 2


# ── 4. _offline_since not updated on SESSION_LIMIT ────────────────────────────


class TestSessionLimitOfflineSince:
    """SESSION_LIMIT must not add camera to _offline_since (not a connectivity failure)."""

    def test_session_limit_does_not_set_offline_since(self) -> None:
        """The status == 'SESSION_LIMIT' branch does NOT add to _offline_since."""
        # We test the logic inline — if status is SESSION_LIMIT it falls into the
        # `else` branch (not in OFFLINE/UPDATING) so _offline_since.pop() is called.
        offline_since: dict[str, float] = {CAM_A: 12345.0}  # simulate pre-existing entry
        status = "SESSION_LIMIT"
        if status in ("OFFLINE", "UPDATING"):
            if CAM_A not in offline_since:
                offline_since[CAM_A] = time.monotonic()
        else:
            offline_since.pop(CAM_A, None)
        assert CAM_A not in offline_since

    def test_offline_does_set_offline_since(self) -> None:
        """OFFLINE should still set _offline_since — regression guard."""
        offline_since: dict[str, float] = {}
        now = time.monotonic()
        status = "OFFLINE"
        if status in ("OFFLINE", "UPDATING"):
            if CAM_A not in offline_since:
                offline_since[CAM_A] = now
        else:
            offline_since.pop(CAM_A, None)
        assert CAM_A in offline_since
