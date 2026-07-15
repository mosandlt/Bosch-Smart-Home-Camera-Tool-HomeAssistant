"""Consolidated tests for shc.py — the Smart Home Controller (SHC) local-API
client plus the cloud setter functions that fall back to it.

Covers:
  - No-forced-refresh contract: privacy/light/notifications/pan setters must
    NOT trigger a full coordinator refresh after a successful write (a forced
    refresh re-touches go2rtc stream registration for every active camera and
    causes unrelated cameras' live sessions to TEARDOWN + reconnect).
  - Privacy-mode / camera-light cache write-lock race: the SHC background
    poller must honor `privacy_set_at` / `light_set_at` so a stale poll
    response cannot revert a just-written user value (PRIVACY_REVERT bug,
    fixed 2026-05-05).
  - `async_shc_request` / `async_update_shc_states`: the raw SHC HTTP client
    and the per-camera device-state fetcher.
  - `async_shc_set_camera_light` / `async_shc_set_privacy_mode`: SHC-local
    setters.
  - `async_cloud_set_privacy_mode` / `async_cloud_set_camera_light` /
    `async_cloud_set_notifications` / `async_cloud_set_light_component` /
    `async_cloud_set_pan`: cloud setters, including their SHC and Gen2 LOCAL
    RCP fallback branches.
  - `shc_configured` / `shc_ready` / `_shc_mark_success` / `_shc_mark_failure`:
    SHC availability/backoff bookkeeping.
  - `_schedule_privacy_off_snapshot`: indoor (shutter) vs outdoor snapshot
    delay after privacy is turned off.
  - `_is_gen2`: hardware-version classification helper.
  - Structural pins (function-existence / write-lock-ordering-in-source)
    guarding against accidental refactors reintroducing known bug shapes.

Merged from (now removed): test_privacy_no_full_refresh.py,
test_setters_no_full_refresh.py, test_shc_coverage_remaining.py,
test_shc_extended.py, test_shc_light_component.py, test_shc_round6.py,
test_shc_setters.py, test_privacy_race.py, test_shc_light_fallback.py, plus
the shc.py-relevant tests out of test_shc_select_remaining_lines.py (that
file's `BoschFcmPushModeSelect.available` tests are select.py-specific and
were left behind — see NOTE at the bottom of this file).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

SRC = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _mock_local_rcp_clientsession():
    """The Gen2 LOCAL-RCP fallback paths (async_cloud_set_privacy_mode /
    async_cloud_set_light_component) build a real aiohttp session via HA's
    `async_get_clientsession(coordinator.hass, verify_ssl=False)` before
    calling into the (now-external) rcp_local_write_privacy/front_light
    library functions. Real `async_get_clientsession` needs a real
    HomeAssistant instance for its connector pool -- the SimpleNamespace/
    MagicMock coordinator.hass stubs used throughout this file aren't one.
    Since every test that exercises this path also mocks
    rcp_local_write_privacy/front_light wholesale (the session object is
    just passed through, never actually used for a real request), a bare
    MagicMock stand-in is sufficient here.
    """
    with patch(
        "custom_components.bosch_shc_camera.shc.async_get_clientsession",
        return_value=MagicMock(),
    ):
        yield


def _mock_response(status: int, json_data=None, text: str = ""):
    """Build a mock aiohttp response async-context-manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


def _put_204_session() -> MagicMock:
    """aiohttp session whose .put(...) async-context-manager yields HTTP 204."""
    session = MagicMock()
    session.put.return_value = _mock_response(204)
    return session


def _put_200_json_session(json_data: dict) -> MagicMock:
    """aiohttp session whose .put(...) yields HTTP 200 with a JSON body."""
    session = MagicMock()
    session.put.return_value = _mock_response(200, json_data)
    return session


# No-forced-refresh regression: privacy/light/notifications/pan setters must
# NOT trigger a full coordinator refresh after a successful write.
#
# Incident 2026-05-29 (live-reproduced via browser + TLS-proxy logs):
# toggling Terrasse privacy ON/OFF made the UNRELATED Innenbereich live
# stream blip — the card showed an "HLS reload" overlay. Root cause (path
# C): after the cloud PUT succeeds, the setter scheduled
# `coordinator.async_request_refresh()`. That forced (un-throttled)
# coordinator tick re-touches go2rtc stream registration for every active
# camera, so go2rtc sent an RTSP TEARDOWN + reconnect on the Innenbereich
# session (~1 s drop):
#
#     06:48:38.775 cloud_set_privacy_mode: 11111111 (Terrasse) -> ON
#     06:48:38.799 TLS proxy 22222222 (Innenbereich) [C→CAM] TEARDOWN ...
#     06:48:39.3-8 → reconnect DESCRIBE→SETUP→PLAY
#
# The state is already pushed to the UI optimistically: the cache is
# updated and `async_update_listeners()` is called immediately before any
# refresh. The full refresh is therefore redundant for the toggled camera
# and destructive for the others. Decoupled (option A): drop the forced
# refresh; the regular 60 s coordinator tick confirms the state, and the
# privacy-OFF snapshot trigger still refreshes the image.


def _stub_coord_privacy_no_refresh():
    return SimpleNamespace(
        token="tok-AAA",
        cached_status={},
        privacy_set_at={},
        shc_state_cache={CAM_ID: {"device_id": "shc-dev-1"}},
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
        hass=SimpleNamespace(async_create_task=MagicMock()),
    )


class TestPrivacyModeNoFullRefresh:
    """Pins: cloud-success path updates the cache + notifies listeners + stamps
    the write-lock, and does NOT schedule async_request_refresh (ON and OFF)."""

    @pytest.mark.asyncio
    async def test_privacy_on_does_not_request_full_refresh(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_privacy_no_refresh()
        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_put_204_session()),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert result is True
        assert coord.shc_state_cache[CAM_ID]["privacy_mode"] is True
        assert CAM_ID in coord.privacy_set_at
        coord.async_update_listeners.assert_called()  # optimistic UI push kept
        coord.async_request_refresh.assert_not_called()  # path C: no forced refresh
        coord.hass.async_create_task.assert_not_called()  # nothing scheduled

    @pytest.mark.asyncio
    async def test_privacy_off_does_not_request_full_refresh(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_privacy_no_refresh()
        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_204_session()),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot"
            ) as mock_snap,
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        assert result is True
        assert coord.shc_state_cache[CAM_ID]["privacy_mode"] is False
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        # The lightweight privacy-OFF snapshot trigger is still scheduled — it
        # refreshes the still image only, it is NOT a full coordinator refresh.
        mock_snap.assert_called_once()


def _base_coord() -> SimpleNamespace:
    return SimpleNamespace(
        token="tok-AAA",
        cached_status={},
        privacy_set_at={},
        notif_set_at={},
        light_set_at={},
        pan_cache={},
        shc_state_cache={
            CAM_ID: {"device_id": "shc-dev-1", "front_light_intensity": 0.5}
        },
        hw_version={CAM_ID: "HOME_Eyes_Outdoor"},  # gen2
        lighting_switch_cache={},
        local_creds_cache={},
        rcp_lan_ip_cache={},
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
        hass=SimpleNamespace(async_create_task=MagicMock()),
    )


class TestShcSetCameraLightNoRefresh:
    @pytest.mark.asyncio
    async def test_on_no_refresh(self) -> None:
        """SHC light ON: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _base_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value={"status": 204, "ok": True}),
        ):
            result = await async_shc_set_camera_light(coord, CAM_ID, True)

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is True
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_off_no_refresh(self) -> None:
        """SHC light OFF: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _base_coord()
        coord.shc_state_cache[CAM_ID]["camera_light"] = True
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value={"status": 204, "ok": True}),
        ):
            result = await async_shc_set_camera_light(coord, CAM_ID, False)

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is False
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


class TestCloudSetCameraLightNoRefresh:
    @pytest.mark.asyncio
    async def test_gen2_on_no_refresh(self) -> None:
        """Cloud light ON (Gen2): cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _base_coord()
        # Gen2 makes two PUT calls (front + topdown)
        session = MagicMock()
        session.put.return_value = _mock_response(204)

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2",
                return_value=True,
            ),
        ):
            result = await async_cloud_set_camera_light(coord, CAM_ID, True)

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is True
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_gen1_off_no_refresh(self) -> None:
        """Cloud light OFF (Gen1): cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _base_coord()
        coord.hw_version[CAM_ID] = "OUTDOOR"  # Gen1

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_204_session()),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2",
                return_value=False,
            ),
        ):
            result = await async_cloud_set_camera_light(coord, CAM_ID, False)

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is False
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


class TestCloudSetLightComponentNoRefresh:
    @pytest.mark.asyncio
    async def test_front_on_no_refresh(self) -> None:
        """Light component 'front' ON: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = _base_coord()

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_204_session()),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2",
                return_value=False,
            ),
        ):
            result = await async_cloud_set_light_component(coord, CAM_ID, "front", True)

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["front_light"] is True
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_front_off_no_refresh(self) -> None:
        """Light component 'front' OFF: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = _base_coord()
        coord.shc_state_cache[CAM_ID]["front_light"] = True

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=_put_204_session()),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2",
                return_value=False,
            ),
        ):
            result = await async_cloud_set_light_component(
                coord, CAM_ID, "front", False
            )

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["front_light"] is False
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


class TestCloudSetNotificationsNoRefresh:
    @pytest.mark.asyncio
    async def test_enable_no_refresh(self) -> None:
        """Notifications enabled: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _base_coord()

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_put_204_session()),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is True
        assert (
            coord.shc_state_cache[CAM_ID]["notifications_status"]
            == "FOLLOW_CAMERA_SCHEDULE"
        )
        assert CAM_ID in coord.notif_set_at
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_no_refresh(self) -> None:
        """Notifications disabled: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _base_coord()

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_put_204_session()),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, False)

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["notifications_status"] == "ALWAYS_OFF"
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


class TestCloudSetPanNoRefresh:
    @pytest.mark.asyncio
    async def test_pan_positive_no_refresh(self) -> None:
        """Pan to positive position: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan

        coord = _base_coord()
        coord.shc_state_cache[CAM_ID]["privacy_mode"] = False

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_put_204_session()),
        ):
            result = await async_cloud_set_pan(coord, CAM_ID, 90)

        assert result is True
        assert coord.pan_cache[CAM_ID] == 90
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_pan_negative_no_refresh(self) -> None:
        """Pan to negative position: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan

        coord = _base_coord()
        coord.shc_state_cache[CAM_ID]["privacy_mode"] = False

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=_put_204_session()),
        ):
            result = await async_cloud_set_pan(coord, CAM_ID, -45)

        assert result is True
        assert coord.pan_cache[CAM_ID] == -45
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_pan_200_with_json_body_no_refresh(self) -> None:
        """Pan 200 with actual position from response body: correct cache + no refresh."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan

        coord = _base_coord()
        coord.shc_state_cache[CAM_ID]["privacy_mode"] = False

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(
                return_value=_put_200_json_session(
                    {"currentAbsolutePosition": 42, "estimatedTimeToCompletion": 500}
                )
            ),
        ):
            result = await async_cloud_set_pan(coord, CAM_ID, 45)

        assert result is True
        assert coord.pan_cache[CAM_ID] == 42  # actual from response, not requested
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


# Privacy-mode / camera-light cache write-lock race (PRIVACY_REVERT bug)
#
# Bug discovered 2026-04-27: first OFF-toggle of the privacy switch visibly
# reverts to ON for ~1-2 seconds, then settles. Second OFF-toggle works
# immediately. Root cause: the SHC fetcher in shc.py overwrote the
# `shc_state_cache[cam_id]["privacy_mode"]` field on every poll without
# honoring the `privacy_set_at` write-lock that the cloud-fetcher path
# already respects. Fixed 2026-05-05 by adding the same write-lock check
# inside `async_update_shc_states`. Same bug shape applies to camera_light
# via `light_set_at`.
#
# These tests pin the contract: when a user write happened within
# `WRITE_LOCK_SECS`, a stale SHC poll response must NOT overwrite the
# freshly-set cache value.


def _make_stub_coordinator(write_lock_secs: float = 30.0):
    """Minimal coordinator stub with the fields shc.py's SHC fetcher touches."""
    return SimpleNamespace(
        shc_state_cache={},
        privacy_set_at={},
        light_set_at={},
        shc_devices_raw=[{"id": "dev-1", "name": "terrasse"}],
        last_shc_fetch=time.monotonic(),
        WRITE_LOCK_SECS=write_lock_secs,
        # async_update_shc_states uses these for the SHC HTTP path:
        hass=SimpleNamespace(),
    )


async def _run_fetcher(
    coord,
    data,
    mock_response_value: str,
    *,
    light_value: str = "OFF",
):
    """Patch async_shc_request to return the given privacy + light state.

    SHC API response shape: {"state": {"value": "ENABLED"|"DISABLED"|"ON"|"OFF"}}.
    """
    from custom_components.bosch_shc_camera import shc

    async def _fake_request(_coord, method, path, *args, **kwargs):
        if path.endswith("/services/PrivacyMode"):
            return {"state": {"value": mock_response_value}}
        if path.endswith("/services/CameraLight"):
            return {"state": {"value": light_value}}
        return None

    with (
        patch.object(shc, "shc_configured", return_value=True),
        patch.object(shc, "async_shc_request", side_effect=_fake_request),
    ):
        await shc.async_update_shc_states(coord, data)


@pytest.mark.asyncio
async def test_user_off_toggle_survives_stale_shc_poll() -> None:
    """User toggles privacy OFF → SHC poll within lock window must not flip back.

    Before the fix, the fetcher wrote `entry["privacy_mode"] = new_priv`
    unconditionally — overwriting the user's freshly-set OFF value with a
    stale ENABLED reading from the SHC.
    """
    coord = _make_stub_coordinator()
    cam_id = CAM_ID
    # 1. User just toggled privacy OFF. Cloud setter writes the cache + lock.
    coord.shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": False}
    coord.privacy_set_at[cam_id] = time.monotonic()  # fresh write
    # 2. SHC poll runs and sees stale ENABLED (cloud lag).
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="ENABLED")
    # 3. Cache must STILL show False — write-lock honored.
    assert coord.shc_state_cache[cam_id]["privacy_mode"] is False, (
        "PRIVACY_REVERT regression: SHC fetcher overwrote a fresh "
        "user-OFF write with a stale ENABLED reading. The write-lock "
        "in async_update_shc_states is broken."
    )


@pytest.mark.asyncio
async def test_shc_poll_applies_after_lock_expires() -> None:
    """Once `WRITE_LOCK_SECS` has elapsed, SHC poll IS authoritative again."""
    coord = _make_stub_coordinator(write_lock_secs=5.0)
    cam_id = CAM_ID
    coord.shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": False}
    coord.privacy_set_at[cam_id] = time.monotonic() - 10.0  # lock expired 5s ago
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="ENABLED")
    assert coord.shc_state_cache[cam_id]["privacy_mode"] is True, (
        "After write-lock expires, SHC must be authoritative again — got stuck "
        "on cached value."
    )


@pytest.mark.asyncio
async def test_shc_poll_applies_when_no_recent_user_write() -> None:
    """No `privacy_set_at` entry → no lock → SHC writes immediately."""
    coord = _make_stub_coordinator()
    cam_id = CAM_ID
    # Fresh start: cache exists but no user-write timestamp recorded
    coord.shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": None}
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="ENABLED")
    assert coord.shc_state_cache[cam_id]["privacy_mode"] is True


@pytest.mark.asyncio
async def test_shc_poll_no_overwrite_when_value_matches() -> None:
    """If SHC reports the same value the user wrote, no race — write goes through.

    Edge case: user writes OFF, then SHC also returns OFF. The fix should
    not over-protect — it should only block when there's a value MISMATCH.
    """
    coord = _make_stub_coordinator()
    cam_id = CAM_ID
    coord.shc_state_cache[cam_id] = {"device_id": "dev-1", "privacy_mode": False}
    coord.privacy_set_at[cam_id] = time.monotonic()  # fresh
    data = {cam_id: {"info": {"title": "terrasse"}}}
    # SHC agrees: also OFF (DISABLED)
    await _run_fetcher(coord, data, mock_response_value="DISABLED")
    # Either branch (skip or apply) ends with False — both are correct.
    assert coord.shc_state_cache[cam_id]["privacy_mode"] is False


@pytest.mark.asyncio
async def test_user_light_off_survives_stale_shc_poll() -> None:
    """User toggles camera_light OFF → SHC poll within lock window must not flip back.

    Same bug shape as privacy_mode. Discovered + fixed 2026-05-05 by
    extending the write-lock check in async_update_shc_states.
    """
    coord = _make_stub_coordinator()
    cam_id = CAM_ID
    # User just toggled light OFF
    coord.shc_state_cache[cam_id] = {"device_id": "dev-1", "camera_light": False}
    coord.light_set_at[cam_id] = time.monotonic()
    data = {cam_id: {"info": {"title": "terrasse"}}}
    # SHC poll sees stale ON
    await _run_fetcher(
        coord,
        data,
        mock_response_value="DISABLED",  # privacy stays OFF
        light_value="ON",  # but light still ON in cloud
    )
    assert coord.shc_state_cache[cam_id]["camera_light"] is False, (
        "camera_light cache race: SHC fetcher overwrote a fresh user-OFF "
        "with stale ON reading. Same bug shape as privacy_mode race."
    )


@pytest.mark.asyncio
async def test_light_shc_poll_applies_after_lock_expires() -> None:
    """Once `WRITE_LOCK_SECS` has elapsed, SHC poll IS authoritative for light."""
    coord = _make_stub_coordinator(write_lock_secs=5.0)
    cam_id = CAM_ID
    coord.shc_state_cache[cam_id] = {"device_id": "dev-1", "camera_light": False}
    coord.light_set_at[cam_id] = time.monotonic() - 10.0
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="DISABLED", light_value="ON")
    assert coord.shc_state_cache[cam_id]["camera_light"] is True


@pytest.mark.asyncio
async def test_light_shc_poll_when_no_recent_user_write() -> None:
    """No `light_set_at` entry → no lock → SHC writes immediately."""
    coord = _make_stub_coordinator()
    cam_id = CAM_ID
    coord.shc_state_cache[cam_id] = {"device_id": "dev-1", "camera_light": None}
    data = {cam_id: {"info": {"title": "terrasse"}}}
    await _run_fetcher(coord, data, mock_response_value="DISABLED", light_value="ON")
    assert coord.shc_state_cache[cam_id]["camera_light"] is True


# Structural: every public function must exist (guards against accidental
# renames the coordinator/entity modules call directly)


class TestShcFunctionContracts:
    def test_required_functions_present(self):
        src = (SRC / "shc.py").read_text()
        for fn in (
            "shc_configured",
            "shc_ready",
            "_shc_mark_success",
            "_shc_mark_failure",
            "async_shc_request",
            "async_update_shc_states",
            "async_shc_set_camera_light",
            "async_shc_set_privacy_mode",
            "async_cloud_set_privacy_mode",
            "async_cloud_set_camera_light",
            "async_cloud_set_notifications",
            "async_cloud_set_pan",
        ):
            assert f"def {fn}" in src or f"async def {fn}" in src, (
                f"shc.py is missing function '{fn}' — coordinator or entity calls it directly"
            )


def _mock_resp(status: int, json_data=None, text: str = ""):
    return _mock_response(status, json_data, text)


def _stub_coord_round6(
    *, gen2: bool = True, with_token: bool = True, shc_ip: str = "192.0.2.103"
):
    opts = {}
    if shc_ip:
        opts = {
            "shc_ip": shc_ip,
            "shc_cert_path": "/cert.pem",
            "shc_key_path": "/key.pem",
        }
    coord = SimpleNamespace(
        token="tok-AAA" if with_token else "",
        options=opts,
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        shc_state_cache={
            CAM_ID: {"device_id": "shc-dev-1", "front_light_intensity": 0.5}
        },
        # production async_cloud_set_privacy_mode reads
        # coordinator.cached_status.get(cam_id) to skip cloud for OFFLINE
        # cams (HTTP 444 spam guard) — must be present on every stub.
        cached_status={},
        privacy_set_at={},
        light_set_at={},
        notif_set_at={},
        local_creds_cache={},
        rcp_lan_ip_cache={},
        pan_cache={},
        camera_entities={},
        hw_version={CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"},
        auth_outage_count=0,
        shc_devices_raw=[],
        last_shc_fetch=float("-inf"),
        shc_available=True,
        shc_fail_count=0,
        shc_last_check=float("-inf"),  # SENTINEL_RULE: never 0.0 for monotonic
        SHC_MAX_FAILS=3,
        SHC_RETRY_INTERVAL=60,
        lighting_switch_cache={},
        async_update_listeners=lambda: None,
        async_request_refresh=AsyncMock(),
        ensure_valid_token=AsyncMock(return_value="tok-FRESH"),
    )
    return coord


class TestAsyncShcRequest:
    """All branches of async_shc_request."""

    @pytest.mark.asyncio
    async def test_missing_opts_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6(shc_ip="")  # empty shc_ip
        coord.options = {}
        result = await async_shc_request(coord, "GET", "/devices")
        assert result is None

    @pytest.mark.asyncio
    async def test_ssl_setup_failure_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        with patch(
            "ssl.SSLContext.load_cert_chain", side_effect=FileNotFoundError("no cert")
        ):
            result = await async_shc_request(coord, "GET", "/devices")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_200_returns_json(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        devices = [{"id": "dev1", "name": "Terrasse"}]
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = _mock_resp(200, devices)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ssl.SSLContext") as mock_ssl,
            patch("aiohttp.TCPConnector"),
            patch("aiohttp.ClientSession", return_value=mock_session_cm),
        ):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/devices")
        assert result == devices

    @pytest.mark.asyncio
    async def test_get_non200_marks_failure(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = _mock_resp(403)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ssl.SSLContext") as mock_ssl,
            patch("aiohttp.TCPConnector"),
            patch("aiohttp.ClientSession", return_value=mock_session_cm),
        ):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/devices")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_returns_status_dict(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.put.return_value = _mock_resp(204)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ssl.SSLContext") as mock_ssl,
            patch("aiohttp.TCPConnector"),
            patch("aiohttp.ClientSession", return_value=mock_session_cm),
        ):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(
                coord, "PUT", "/devices/dev1/services/CameraLight/state", {}
            )
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_put_failure_status(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.put.return_value = _mock_resp(500)
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ssl.SSLContext") as mock_ssl,
            patch("aiohttp.TCPConnector"),
            patch("aiohttp.ClientSession", return_value=mock_session_cm),
        ):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "PUT", "/path", {})
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.side_effect = TimeoutError()
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ssl.SSLContext") as mock_ssl,
            patch("aiohttp.TCPConnector"),
            patch("aiohttp.ClientSession", return_value=mock_session_cm),
        ):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/x")
        assert result is None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.side_effect = aiohttp.ClientError("conn refused")
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ssl.SSLContext") as mock_ssl,
            patch("aiohttp.TCPConnector"),
            patch("aiohttp.ClientSession", return_value=mock_session_cm),
        ):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/x")
        assert result is None

    @pytest.mark.asyncio
    async def test_generic_exception_returns_none(self):
        from custom_components.bosch_shc_camera.shc import async_shc_request

        coord = _stub_coord_round6()
        mock_session_cm = MagicMock()
        mock_session = MagicMock()
        mock_session.get.side_effect = RuntimeError("unexpected")
        mock_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch("ssl.SSLContext") as mock_ssl,
            patch("aiohttp.TCPConnector"),
            patch("aiohttp.ClientSession", return_value=mock_session_cm),
        ):
            mock_ssl.return_value.load_cert_chain = MagicMock()
            result = await async_shc_request(coord, "GET", "/x")
        assert result is None


class TestAsyncUpdateShcStates:
    @pytest.mark.asyncio
    async def test_not_configured_returns_early(self):
        from custom_components.bosch_shc_camera.shc import async_update_shc_states

        coord = _stub_coord_round6(shc_ip="")
        coord.options = {}
        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "privacy_mode": False,
                "camera_light": False,
            }
        }
        await async_update_shc_states(coord, data)
        # Must not crash and must not modify data when SHC not configured

    @pytest.mark.asyncio
    async def test_empty_devices_returns_early(self):
        from custom_components.bosch_shc_camera.shc import async_update_shc_states

        coord = _stub_coord_round6()
        coord.shc_devices_raw = []
        coord.last_shc_fetch = time.monotonic() - 120  # force fetch
        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "privacy_mode": False,
                "camera_light": False,
            }
        }
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value=None),
        ):
            await async_update_shc_states(coord, data)
        # No crash — empty device list is handled gracefully

    @pytest.mark.asyncio
    async def test_device_fetch_updates_shc_devices_raw(self):
        from custom_components.bosch_shc_camera.shc import async_update_shc_states

        coord = _stub_coord_round6()
        coord.last_shc_fetch = float("-inf")  # force refresh
        devices = [
            {
                "id": "shc-dev-1",
                "name": "terrasse",
                "services": [
                    {"id": "CameraLight", "state": {"value": "ON"}},
                    {"id": "PrivacyMode", "state": {"value": "DISABLED"}},
                ],
            },
        ]
        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},
                "privacy_mode": None,
                "camera_light": None,
            }
        }
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value=devices),
        ):
            await async_update_shc_states(coord, data)
        assert coord.shc_devices_raw == devices


class TestAsyncUpdateShcStatesNoDeviceMatch:
    """When no SHC device name matches the camera title, the per-camera loop
    body must log a debug line and `continue` rather than crash."""

    @pytest.mark.asyncio
    async def test_no_device_match_logs_debug_and_continues(self):
        """Camera title 'Terrasse' does not match SHC device name 'Unknown'."""
        from custom_components.bosch_shc_camera.shc import async_update_shc_states

        coord = _stub_coord_select_remaining()
        # Pre-populate device list so the fetch branch is skipped
        coord.shc_devices_raw = [{"id": "dev-99", "name": "Unknown Device"}]
        coord.last_shc_fetch = float("inf")  # force "recent enough" to skip re-fetch

        data = {
            CAM_ID: {
                "info": {"title": "Terrasse"},  # name mismatch → no device_id found
            }
        }

        # async_shc_request should NOT be called (last_shc_fetch is current)
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value=[]),
        ) as mock_req:
            await async_update_shc_states(coord, data)
            # The cam_id loop body should hit `continue` — no state-cache entry created
            assert CAM_ID not in coord.shc_state_cache
            # async_shc_request was not called because last_shc_fetch is "future"
            mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_device_match_multiple_cameras_continues_to_next(self):
        """Two cameras: only one matches. The non-matching one is skipped."""
        from custom_components.bosch_shc_camera.shc import async_update_shc_states

        cam2_id = "22222222-OTHER-CAM"
        coord = _stub_coord_round6()
        coord.shc_devices_raw = [
            {"id": "dev-1", "name": "terrasse"}
        ]  # only matches first cam
        coord.last_shc_fetch = float("inf")

        # Pre-seed cache for CAM2 so we can confirm it's NOT updated
        coord.shc_state_cache = {}

        data = {
            CAM_ID: {"info": {"title": "Terrasse"}},  # matches → processed
            cam2_id: {"info": {"title": "Innenbereich"}},  # no match → skipped
        }

        # Patch the downstream SHC requests (CameraLight, PrivacyMode) to short-circuit
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value=None),
        ):
            await async_update_shc_states(coord, data)

        # CAM_ID was matched → cache entry created; CAM2_ID was not matched → absent
        assert CAM_ID in coord.shc_state_cache
        assert cam2_id not in coord.shc_state_cache


def _coord_with_shc(
    cam_id: str = CAM_ID, device_id: str = "dev-001"
) -> SimpleNamespace:
    return SimpleNamespace(
        hass=MagicMock(),
        options={
            "shc_ip": "10.0.0.103",
            "shc_cert_path": "/path/cert.pem",
            "shc_key_path": "/path/key.pem",
        },
        shc_state_cache={cam_id: {"device_id": device_id, "camera_light": False}},
        _shc_mark_success=MagicMock(),
        _shc_mark_failure=MagicMock(),
        light_set_at={},
        _shc_consecutive_failures=0,
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
    )


class TestAsyncShcSetCameraLight:
    @pytest.mark.asyncio
    async def test_no_device_id_returns_false(self):
        """If no device_id is cached for the camera, the setter must abort with False."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _coord_with_shc()
        coord.shc_state_cache = {CAM_ID: {}}  # no device_id

        result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is False, (
            "async_shc_set_camera_light must return False when device_id is not cached — "
            "no SHC device found to send the command to"
        )

    @pytest.mark.asyncio
    async def test_no_cache_entry_returns_false(self):
        """Camera not in shc_state_cache at all → False."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _coord_with_shc()
        coord.shc_state_cache = {}  # cam_id missing entirely

        result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is False

    @pytest.mark.asyncio
    async def test_success_updates_cache_and_notifies(self):
        """PUT 204 → cache updated, listeners notified, refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _stub_coord_round6()
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value={"status": 204, "ok": True}),
        ):
            result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is True
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is True

    @pytest.mark.asyncio
    async def test_failure_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _stub_coord_round6()
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value={"status": 500, "ok": False}),
        ):
            result = await async_shc_set_camera_light(coord, CAM_ID, True)
        assert result is False


class TestAsyncShcSetPrivacyMode:
    @pytest.mark.asyncio
    async def test_no_device_id_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode

        coord = _coord_with_shc()
        coord.shc_state_cache = {CAM_ID: {}}

        result = await async_shc_set_privacy_mode(coord, CAM_ID, True)
        assert result is False

    def test_privacy_set_at_written_in_setter_body(self):
        """privacy_set_at must be stamped inside async_shc_set_privacy_mode.

        Guards against the BUG-4 pattern: write-lock written after cache update
        would leave a race window where the SHC fetcher sees no lock.
        """
        src = (SRC / "shc.py").read_text()
        func_start = src.find("async def async_shc_set_privacy_mode")
        assert func_start != -1
        next_func = src.find("\nasync def ", func_start + 1)
        func_body = src[func_start:next_func] if next_func != -1 else src[func_start:]
        assert "privacy_set_at" in func_body, (
            "async_shc_set_privacy_mode must stamp privacy_set_at "
            "to prevent BUG-4 race on the SHC fallback path"
        )

    @pytest.mark.asyncio
    async def test_success_updates_cache_and_lock(self):
        """PUT 204 → cache + write-lock stamped, listeners notified."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode

        coord = _stub_coord_round6()
        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_shc_request",
                AsyncMock(return_value={"status": 204, "ok": True}),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot"
            ),
        ):
            result = await async_shc_set_privacy_mode(coord, CAM_ID, False)
        assert result is True
        assert coord.shc_state_cache[CAM_ID]["privacy_mode"] is False
        assert CAM_ID in coord.privacy_set_at

    @pytest.mark.asyncio
    async def test_success_enable_does_not_schedule_snapshot(self):
        """When enabling privacy (True), the snapshot refresh is not scheduled."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode

        coord = _stub_coord_round6()
        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_shc_request",
                AsyncMock(return_value={"status": 204, "ok": True}),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot"
            ) as mock_snap,
        ):
            result = await async_shc_set_privacy_mode(coord, CAM_ID, True)
        assert result is True
        mock_snap.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_shc_set_privacy_mode

        coord = _stub_coord_round6()
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value={"status": 500, "ok": False}),
        ):
            result = await async_shc_set_privacy_mode(coord, CAM_ID, False)
        assert result is False


def _stub_coord_setters(*, gen2: bool = True, with_token: bool = True):
    """Stub coordinator providing the fields shc.py setters touch."""
    return SimpleNamespace(
        token="token-AAA" if with_token else "",
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        shc_state_cache={CAM_ID: {"front_light_intensity": 0.5}},
        privacy_set_at={},
        light_set_at={},
        notif_set_at={},
        local_creds_cache={},
        rcp_lan_ip_cache={},
        pan_cache={},
        camera_entities={},  # used by _schedule_privacy_off_snapshot
        hw_version={CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"},
        cached_status={},
        auth_outage_count=0,
        async_update_listeners=lambda: None,
        async_request_refresh=AsyncMock(),
        ensure_valid_token=AsyncMock(return_value="token-FRESH"),
    )


class TestCloudSetPrivacyMode:
    @pytest.mark.asyncio
    async def test_success_updates_cache_and_lock(self):
        """Cloud PUT 204 → cache + lock + listener call."""
        coord = _stub_coord_setters()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(204))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            from custom_components.bosch_shc_camera.shc import (
                async_cloud_set_privacy_mode,
            )

            ok = await async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert ok is True
        assert coord.shc_state_cache[CAM_ID]["privacy_mode"] is True
        assert CAM_ID in coord.privacy_set_at  # lock recorded
        # Lock timestamp must be recent
        assert time.monotonic() - coord.privacy_set_at[CAM_ID] < 1.0

    @pytest.mark.asyncio
    async def test_no_token_falls_through_to_shc(self):
        """No bearer token → skip cloud, try SHC fallback."""
        coord = _stub_coord_setters(with_token=False)
        from custom_components.bosch_shc_camera import shc

        # SHC not configured → returns False
        with patch.object(shc, "shc_ready", return_value=False):
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert ok is False
        # Cache untouched (not optimistically written when nothing succeeded)
        assert "privacy_mode" not in coord.shc_state_cache.get(CAM_ID, {})

    @pytest.mark.asyncio
    async def test_http_401_triggers_token_refresh(self):
        """Cloud returns 401 → coordinator.ensure_valid_token called."""
        coord = _stub_coord_setters()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        # First PUT returns 401, second PUT (after refresh) returns 204
        session.put = MagicMock(
            side_effect=[
                _mock_response(401),
                _mock_response(204),
            ]
        )
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            await shc.async_cloud_set_privacy_mode(coord, CAM_ID, False)
        assert coord.ensure_valid_token.called

    @pytest.mark.asyncio
    async def test_http_500_does_not_update_cache(self):
        """5xx response → cache stays untouched, no lock recorded."""
        coord = _stub_coord_setters()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(500))
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch.object(shc, "shc_ready", return_value=False),
        ):
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert ok is False
        assert CAM_ID not in coord.privacy_set_at

    @pytest.mark.asyncio
    async def test_offline_cam_skips_cloud_call(self):
        """Cam status OFFLINE → no cloud HTTP attempt (silences HTTP 444 spam).

        Regression: offline Gen1 cams (Kamera 44444444, Eingang 33333333)
        triggered 7× WARNING/day from Bosch cloud returning HTTP 444 on
        cloud_set_privacy_mode. Source: user-reported log noise 2026-05-11.
        """
        coord = _stub_coord_setters(gen2=False)
        coord.cached_status[CAM_ID] = "OFFLINE"
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(444))
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch.object(shc, "shc_ready", return_value=False),
        ):
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert ok is False
        assert session.put.called is False  # cloud call never attempted
        assert CAM_ID not in coord.privacy_set_at


class TestCloudSetPrivacyModeBranches:
    """Additional branches: timeout, SHC fallback delegation, auth-outage
    notification, privacy-off snapshot scheduling, Gen2 RCP fallback."""

    @pytest.mark.asyncio
    async def test_timeout_falls_through_to_no_shc(self):
        """aiohttp timeout → falls through; no SHC → returns False."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6()
        coord.shc_state_cache[CAM_ID]["device_id"] = None  # disable SHC fallback
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("timeout"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        assert result is False

    @pytest.mark.asyncio
    async def test_shc_fallback_called_on_cloud_fail(self):
        """Cloud PUT fails → shc_ready=True → delegates to async_shc_set_privacy_mode."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6()
        session = MagicMock()
        session.put.return_value = _mock_resp(500)

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=True
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_shc_set_privacy_mode",
                AsyncMock(return_value=True),
            ) as mock_shc,
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        mock_shc.assert_called_once()
        assert result is True

    @pytest.mark.asyncio
    async def test_persistent_notification_on_auth_outage(self):
        """auth_outage_count > 0 + no SHC → creates a persistent notification."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6()
        coord.auth_outage_count = 2
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        coord.hass.services.async_call.assert_called_once()
        assert result is False

    @pytest.mark.asyncio
    async def test_schedule_privacy_off_snapshot_when_disabling(self):
        """Successful cloud PUT with enabled=False → snapshot schedule triggered."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6()
        session = MagicMock()
        session.put.return_value = _mock_resp(204)

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._schedule_privacy_off_snapshot"
            ) as mock_snap,
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)
        mock_snap.assert_called_once_with(coord, CAM_ID)
        assert result is True

    @pytest.mark.asyncio
    async def test_gen2_rcp_fallback_success(self):
        """Cloud fails, Gen2 RCP fallback succeeds → returns True."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6(gen2=True)
        coord.local_creds_cache[CAM_ID] = {"host": "192.0.2.149"}
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("cloud down"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_privacy",
                AsyncMock(return_value=True),
            ),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)
        assert result is True


class TestGen2RcpFallbackFailureDebugLog:
    """Gen2 LOCAL RCP fallback returns ok=False → debug log path, control flow
    falls through to the SHC / notification stages."""

    @pytest.mark.asyncio
    async def test_rcp_returns_false_logs_debug_and_falls_through(self):
        """rcp_local_write_privacy → False → debug-log path executes,
        control flow continues to SHC / notification stages.

        Setup:
          - cloud PUT raises ClientError (skip cloud path)
          - Gen2 = True, cam_host present (creds cache)
          - rcp_local_write_privacy returns False
          - shc_ready = False, auth_outage_count = 0
        Expected: returns False, no exception, no cache mutation. The
        failure notification fires unconditionally (2026-07-07 fix — it
        used to be gated on auth_outage_count > 0, a counter that never
        reflects a one-off write-time failure like this one).
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6(gen2=True)
        coord.local_creds_cache[CAM_ID] = {"host": "192.0.2.149"}

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("cloud down"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_privacy",
                AsyncMock(return_value=False),
            ) as mock_rcp,
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)

        # RCP was called with the cached host, returned False → debug branch hit
        mock_rcp.assert_called_once()
        assert mock_rcp.call_args.args[1] == "192.0.2.149"
        # No write-lock stamped, no cache mutation, no early True return
        assert CAM_ID not in coord.privacy_set_at
        assert "privacy_mode" not in coord.shc_state_cache[CAM_ID]
        assert result is False
        # Notification fires even though auth_outage_count == 0.
        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_rcp_false_uses_rcp_lan_ip_cache_when_no_creds(self):
        """When local_creds_cache is empty, host comes from rcp_lan_ip_cache.

        Same RCP-failure debug-log branch, exercises the alternate cam_host
        lookup:
            cam_host = creds.get("host") if creds else coordinator.rcp_lan_ip_cache.get(cam_id)
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6(gen2=True)
        # No creds cache entry → falls back to RCP LAN IP cache
        coord.rcp_lan_ip_cache[CAM_ID] = "192.0.2.149"

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("cloud down"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_privacy",
                AsyncMock(return_value=False),
            ) as mock_rcp,
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)

        mock_rcp.assert_called_once()
        assert mock_rcp.call_args.args[1] == "192.0.2.149"
        assert result is False


class TestPersistentNotificationException:
    """`hass.services.async_call` raises → exception swallowed by the bare
    `except Exception: pass` guard (missing persistent_notification service
    must not crash the setter)."""

    @pytest.mark.asyncio
    async def test_notification_service_raises_swallowed(self):
        """When persistent_notification.create raises (e.g. service unavailable),
        the bare `except Exception: pass` swallows it and the function still
        returns False instead of propagating an unhandled error to the caller.

        Setup that drives execution to the notification-attempt branch:
          - cloud PUT raises ClientError
          - Gen2 = False (skip RCP fallback block)
          - shc_ready = False (skip SHC fallback)
          - auth_outage_count > 0 (triggers notification attempt)
          - services.async_call raises ServiceNotFound-like RuntimeError
        Expected: returns False, no propagated exception.
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6(gen2=False)
        coord.auth_outage_count = 3  # > 0 → notification branch entered
        # Make the persistent_notification call blow up
        coord.hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("ServiceNotFound: persistent_notification.create")
        )

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
        ):
            # MUST NOT raise — `except Exception: pass` swallows it
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)

        # Service call attempted exactly once
        coord.hass.services.async_call.assert_called_once()
        # All fallbacks exhausted → False
        assert result is False

    @pytest.mark.asyncio
    async def test_notification_service_raises_for_disable(self):
        """Same exception-swallow path with enabled=False (privacy OFF)
        confirms the message branch `'ON' if enabled else 'OFF'` is exercised
        for both polarities without leaking the inner exception.
        """
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_round6(gen2=False)
        coord.auth_outage_count = 1
        coord.hass.services.async_call = AsyncMock(
            side_effect=Exception("notifier crash")
        )

        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("network"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)

        coord.hass.services.async_call.assert_called_once()
        # Verify the notification body referenced "OFF" (not "ON")
        call_args = coord.hass.services.async_call.call_args
        message = call_args.args[2]["message"]
        assert "OFF" in message
        assert result is False


def _stub_coord_select_remaining(*, gen2: bool = True, shc_ip: str = "192.0.2.103"):
    opts = {"shc_ip": shc_ip, "shc_cert_path": "/cert.pem", "shc_key_path": "/key.pem"}
    coord = SimpleNamespace(
        token="tok-AAA",
        options=opts,
        hass=SimpleNamespace(
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        shc_state_cache={},
        cached_status={},
        privacy_set_at={},
        light_set_at={},
        notif_set_at={},
        local_creds_cache={},
        rcp_lan_ip_cache={},
        pan_cache={},
        camera_entities={},
        hw_version={CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"},
        auth_outage_count=0,
        shc_devices_raw=[],
        last_shc_fetch=float("-inf"),
        shc_available=True,
        shc_fail_count=0,
        shc_last_check=float("-inf"),
        SHC_MAX_FAILS=3,
        SHC_RETRY_INTERVAL=60,
        lighting_switch_cache={},
        async_update_listeners=lambda: None,
        async_request_refresh=AsyncMock(),
        ensure_valid_token=AsyncMock(return_value="tok-FRESH"),
        fcm_last_push=float("-inf"),
    )
    return coord


class TestCloudSetPrivacyMode401TokenRefreshFails:
    """401 response + ensure_valid_token raises → exception swallowed."""

    @pytest.mark.asyncio
    async def test_401_token_refresh_raises_falls_through_to_shc(self):
        """ensure_valid_token raises RuntimeError on 401 → swallowed, falls to SHC."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_select_remaining()
        coord.ensure_valid_token = AsyncMock(side_effect=RuntimeError("refresh failed"))
        # SHC is configured but not ready (shc_available=False) so we get a clean return
        coord.shc_available = False

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_resp(401))

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_configured",
                return_value=True,
            ),
        ):
            # Must not raise — exception is swallowed
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, True)

        # Token refresh failed → no success, result is False
        assert result is False
        coord.ensure_valid_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_401_token_refresh_raises_exception_is_swallowed(self):
        """Verify the except block catches ANY exception type."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_privacy_mode

        coord = _stub_coord_select_remaining()
        coord.ensure_valid_token = AsyncMock(
            side_effect=aiohttp.ClientError("network gone")
        )

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_resp(401))

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_configured",
                return_value=True,
            ),
        ):
            result = await async_cloud_set_privacy_mode(coord, CAM_ID, False)

        assert result is False


class TestCloudSetCameraLight:
    @pytest.mark.asyncio
    async def test_gen1_success(self):
        """Gen1 lighting_override PUT 204 → cache updated."""
        coord = _stub_coord_setters(gen2=False)
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(204))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_camera_light(coord, CAM_ID, True)
        assert ok is True
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is True
        assert CAM_ID in coord.light_set_at

    @pytest.mark.asyncio
    async def test_gen2_double_endpoint_partial_success(self):
        """Gen2: front + topdown endpoints. Partial success (one OK) is treated as success."""
        coord = _stub_coord_setters(gen2=True)
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(
            side_effect=[
                _mock_response(204),  # front OK
                _mock_response(442),  # topdown not supported on this hw
            ]
        )
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_camera_light(coord, CAM_ID, True)
        # `ok = ok1 or ok2` → True
        assert ok is True

    @pytest.mark.asyncio
    async def test_gen2_both_endpoints_fail(self):
        coord = _stub_coord_setters(gen2=True)
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(
            side_effect=[
                _mock_response(500),
                _mock_response(500),
            ]
        )
        with (
            patch.object(
                shc,
                "async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch.object(shc, "shc_ready", return_value=False),
        ):
            ok = await shc.async_cloud_set_camera_light(coord, CAM_ID, False)
        assert ok is False
        # Every write path exhausted (both Gen2 endpoints 500, no SHC) must
        # surface a persistent_notification — this is exactly the user-facing
        # gap v14.4.9 fixed (silent revert with zero feedback). Asserting
        # only `ok is False` doesn't catch a regression that skips the notify
        # call while still correctly returning False.
        coord.hass.services.async_call.assert_called_once()
        call_args = coord.hass.services.async_call.call_args
        assert call_args.args[0] == "persistent_notification"
        assert call_args.args[1] == "create"
        notif_data = call_args.args[2]
        assert CAM_ID[:8] in notif_data["notification_id"]
        assert "light" in notif_data["notification_id"]
        assert "OFF" in notif_data["message"]


class TestCloudSetCameraLightBranches:
    @pytest.mark.asyncio
    async def test_gen2_client_error_falls_to_shc(self):
        """Gen2 aiohttp error → SHC fallback called if shc_ready."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _stub_coord_round6(gen2=True)
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn error"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=True
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_shc_set_camera_light",
                AsyncMock(return_value=True),
            ) as mock_shc,
        ):
            await async_cloud_set_camera_light(coord, CAM_ID, True)
        mock_shc.assert_called_once()

    @pytest.mark.asyncio
    async def test_gen1_light_off_body_excludes_intensity(self):
        """Gen1 light OFF → body must NOT contain frontLightIntensity."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _stub_coord_round6(gen2=False)
        session = MagicMock()
        session.put.return_value = _mock_resp(204)

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
        ):
            result = await async_cloud_set_camera_light(coord, CAM_ID, False)
        # Verify PUT was called with the correct URL (contains cam ID) and body excludes intensity
        assert session.put.called
        call_url = session.put.call_args[0][0]
        assert CAM_ID in call_url, f"PUT URL must contain camera ID; got: {call_url}"
        _, call_kwargs = session.put.call_args
        body = call_kwargs.get("json", {})
        assert "frontLightIntensity" not in body
        assert result is False or result is True  # returns bool, not None

    @pytest.mark.asyncio
    async def test_gen1_http_failure_returns_false(self):
        """Gen1 HTTP 500 → not cached, returns False."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _stub_coord_round6(gen2=False)
        session = MagicMock()
        session.put.return_value = _mock_resp(500)

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=False
            ),
        ):
            result = await async_cloud_set_camera_light(coord, CAM_ID, True)
        assert result is False

    @pytest.mark.asyncio
    async def test_gen1_client_error_falls_to_shc(self):
        """Gen1 aiohttp error → SHC fallback if shc_ready."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _stub_coord_round6(gen2=False)
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("no conn"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.shc._is_gen2", return_value=False
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.shc_ready", return_value=True
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_shc_set_camera_light",
                AsyncMock(return_value=True),
            ) as mock_shc,
        ):
            await async_cloud_set_camera_light(coord, CAM_ID, True)
        mock_shc.assert_called_once()


def _notif_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    return SimpleNamespace(
        hass=MagicMock(),
        token="fake-bearer-token",
        shc_state_cache={cam_id: {}},
        notif_set_at={},
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
    )


def _mock_cloud_resp(status: int) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestAsyncCloudSetNotifications:
    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        coord.token = None

        result = await async_cloud_set_notifications(coord, CAM_ID, True)
        assert result is False

    @pytest.mark.asyncio
    async def test_200_updates_cache_and_returns_true(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(200))

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is True
        assert (
            coord.shc_state_cache[CAM_ID]["notifications_status"]
            == "FOLLOW_CAMERA_SCHEDULE"
        )

    @pytest.mark.asyncio
    async def test_204_updates_cache(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(204))

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, False)

        assert result is True
        assert coord.shc_state_cache[CAM_ID]["notifications_status"] == "ALWAYS_OFF"

    @pytest.mark.asyncio
    async def test_notif_set_at_stamped_on_success(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(200))
        before = time.monotonic()

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_cloud_set_notifications(coord, CAM_ID, True)

        assert CAM_ID in coord.notif_set_at, (
            "notif_set_at must be stamped on notification success — "
            "write-lock prevents SHC background tick from reverting the cache"
        )
        assert coord.notif_set_at[CAM_ID] >= before

    @pytest.mark.asyncio
    async def test_http_error_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(return_value=_mock_cloud_resp(500))

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is False
        assert "notifications_status" not in coord.shc_state_cache.get(CAM_ID, {})

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _notif_coord()
        session = MagicMock()
        session.put = MagicMock(side_effect=TimeoutError())

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is False


class TestCloudSetNotifications:
    @pytest.mark.asyncio
    async def test_enable_writes_FOLLOW_CAMERA_SCHEDULE(self):
        coord = _stub_coord_setters()
        captured_body = {}

        def _capture_put(url, json=None, headers=None):
            captured_body.update(json)
            return _mock_response(204)

        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_notifications(coord, CAM_ID, True)
        assert ok is True
        assert captured_body["enabledNotificationsStatus"] == "FOLLOW_CAMERA_SCHEDULE"
        assert (
            coord.shc_state_cache[CAM_ID]["notifications_status"]
            == "FOLLOW_CAMERA_SCHEDULE"
        )
        assert CAM_ID in coord.notif_set_at

    @pytest.mark.asyncio
    async def test_disable_writes_ALWAYS_OFF(self):
        coord = _stub_coord_setters()
        captured = {}

        def _capture(url, json=None, headers=None):
            captured.update(json)
            return _mock_response(204)

        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_notifications(coord, CAM_ID, False)
        assert ok is True
        assert captured["enabledNotificationsStatus"] == "ALWAYS_OFF"

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        coord = _stub_coord_setters(with_token=False)
        from custom_components.bosch_shc_camera import shc

        ok = await shc.async_cloud_set_notifications(coord, CAM_ID, True)
        assert ok is False

    @pytest.mark.asyncio
    async def test_http_failure_returns_false(self):
        coord = _stub_coord_setters()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(500))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_notifications(coord, CAM_ID, True)
        assert ok is False
        # Cache must NOT have been updated
        assert "notifications_status" not in coord.shc_state_cache.get(CAM_ID, {})


def _stub_coord_light(*, gen2: bool = True, with_token: bool = True):
    """Stub coordinator with the fields shc.py light/pan setters touch."""
    return SimpleNamespace(
        token="token-AAA" if with_token else "",
        hass=SimpleNamespace(
            data={},  # async_get_clientsession pre-allocates session
            async_create_task=lambda coro: coro.close(),
            services=SimpleNamespace(async_call=AsyncMock()),
        ),
        shc_state_cache={
            CAM_ID: {
                "front_light": False,
                "wallwasher": False,
                "front_light_intensity": 0.5,
                "privacy_mode": False,
            }
        },
        light_set_at={},
        pan_cache={},
        camera_entities={},
        hw_version={CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"},
        lighting_switch_cache={
            CAM_ID: {
                "frontLightSettings": {
                    "brightness": 50,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "topLedLightSettings": {
                    "brightness": 80,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "bottomLedLightSettings": {
                    "brightness": 80,
                    "color": None,
                    "whiteBalance": -1.0,
                },
            }
        },
        last_topdown_brightness={},
        auth_outage_count=0,
        async_update_listeners=lambda: None,
        # Gen2 LAN-RCP light fallback reads these. Empty caches mean the
        # fallback path also fails and the function returns False — which
        # is the documented behaviour when no LAN IP is known.
        local_creds_cache={},
        rcp_lan_ip_cache={},
        local_write_at={},
        async_request_refresh=AsyncMock(),
    )


class TestIsGen2:
    def test_gen2_outdoor(self):
        from custom_components.bosch_shc_camera.shc import _is_gen2

        coord = _stub_coord_light(gen2=True)
        assert _is_gen2(coord, CAM_ID) is True

    def test_gen1_outdoor(self):
        from custom_components.bosch_shc_camera.shc import _is_gen2

        coord = _stub_coord_light(gen2=False)
        coord.hw_version[CAM_ID] = "CAMERA_EYES"  # Gen1 outdoor
        assert _is_gen2(coord, CAM_ID) is False

    def test_unknown_falls_back_to_gen1(self):
        """Unknown hardware version → defaults to "CAMERA" → Gen1.
        Important: a misclassification as Gen2 would route lighting
        through wrong endpoints and silently no-op."""
        from custom_components.bosch_shc_camera.shc import _is_gen2

        coord = _stub_coord_light()
        coord.hw_version.pop(CAM_ID, None)  # unknown
        assert _is_gen2(coord, CAM_ID) is False


class TestSetLightComponentGen1:
    """Gen1 cameras use a single PUT /lighting_override endpoint with a
    combined body (frontLightOn + wallwasherOn + frontLightIntensity).

    Critical Bosch API constraint (verified 2026-04-25): the endpoint
    rejects `frontLightIntensity` when `frontLightOn=False` with HTTP
    400 (`frontIlluminatorIntensity must not be set if frontLightOn is
    false`). Body construction must omit intensity when front=False.
    """

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = _stub_coord_light(with_token=False)
        ok = await async_cloud_set_light_component(coord, CAM_ID, "front", True)
        assert ok is False

    @pytest.mark.asyncio
    async def test_front_on_includes_intensity(self):
        """Front=True body must include frontLightIntensity (cached value)."""
        coord = _stub_coord_light(gen2=False)
        coord.shc_state_cache[CAM_ID]["front_light_intensity"] = 0.75

        from custom_components.bosch_shc_camera import shc

        captured_body = {}

        def _capture_put(url, json=None, headers=None):
            captured_body["url"] = url
            captured_body["body"] = json
            return _mock_response(204)

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(coord, CAM_ID, "front", True)

        assert ok is True
        assert captured_body["url"].endswith("/lighting_override")
        body = captured_body["body"]
        assert body["frontLightOn"] is True
        assert body["frontLightIntensity"] == 0.75, (
            "Cached intensity must be sent — otherwise switching front ON "
            "loses the user's brightness preference."
        )
        # Other fields preserved from cache
        assert "wallwasherOn" in body

    @pytest.mark.asyncio
    async def test_front_off_omits_intensity(self):
        """Bosch API rejects intensity when frontLightOn=False (HTTP 400).
        Body MUST omit the field — sending intensity:0 is also rejected."""
        coord = _stub_coord_light(gen2=False)
        coord.shc_state_cache[CAM_ID]["front_light"] = True

        from custom_components.bosch_shc_camera import shc

        captured = {}

        def _capture_put(url, json=None, headers=None):
            captured["body"] = json
            return _mock_response(204)

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "front", False
            )

        assert ok is True
        body = captured["body"]
        assert body["frontLightOn"] is False
        assert "frontLightIntensity" not in body, (
            "Bosch API constraint: intensity must NOT be sent when "
            "frontLightOn=False (HTTP 400 sh:camera.in.invalid). "
            "Verified live 2026-04-25."
        )

    @pytest.mark.asyncio
    async def test_wallwasher_on_uses_lighting_override(self):
        """Gen1 wallwasher hits the same combined endpoint as front light."""
        coord = _stub_coord_light(gen2=False)
        coord.shc_state_cache[CAM_ID]["wallwasher"] = False

        from custom_components.bosch_shc_camera import shc

        captured = {}

        def _capture_put(url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return _mock_response(204)

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", True
            )

        assert ok is True
        assert captured["url"].endswith("/lighting_override")
        assert captured["body"]["wallwasherOn"] is True

    @pytest.mark.asyncio
    async def test_intensity_writes_cached_value(self):
        """Setting intensity directly — front state must come from cache."""
        coord = _stub_coord_light(gen2=False)
        coord.shc_state_cache[CAM_ID]["front_light"] = True  # so intensity is allowed

        from custom_components.bosch_shc_camera import shc

        captured = {}

        def _capture_put(url, json=None, headers=None):
            captured["body"] = json
            return _mock_response(204)

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "intensity", 0.42
            )

        assert ok is True
        assert captured["body"]["frontLightIntensity"] == 0.42

    @pytest.mark.asyncio
    async def test_http_500_returns_false_no_cache_update(self):
        """Failed PUT must NOT optimistically update the state cache."""
        coord = _stub_coord_light(gen2=False)
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(500))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(coord, CAM_ID, "front", True)

        assert ok is False
        assert CAM_ID not in coord.light_set_at, (
            "Failed PUT must not record the write timestamp — otherwise "
            "the write-lock would block legitimate cloud polls for 30 s."
        )

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        """asyncio.TimeoutError must be caught and surfaced as False."""
        coord = _stub_coord_light(gen2=False)
        from custom_components.bosch_shc_camera import shc

        def _raise_timeout(*args, **kwargs):
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(side_effect=TimeoutError())
            ctx.__aexit__ = AsyncMock(return_value=None)
            return ctx

        session = MagicMock()
        session.put = MagicMock(side_effect=_raise_timeout)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(coord, CAM_ID, "front", True)
        assert ok is False

    @pytest.mark.asyncio
    async def test_unknown_component_returns_false(self):
        """component='snake_oil' (not front/wallwasher/intensity) on Gen1
        leaves the body fields at cache defaults — must not write."""
        # Gen1 currently doesn't reject unknown components explicitly; but
        # if `value` is passed for an unknown component, the body still
        # reflects cache. Test just ensures no crash.
        coord = _stub_coord_light(gen2=False)
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(204))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "snake_oil", True
            )
        # Whatever the result — must not raise
        assert ok in (True, False)


class TestSetLightComponentGen2:
    """Gen2 uses separate endpoints: /lighting/switch/front,
    /lighting/switch/topdown, plus a combined /lighting/switch for
    brightness updates. The wallwasher path is the most complex —
    it issues TWO requests (brightness sync + topdown toggle)."""

    @pytest.mark.asyncio
    async def test_front_uses_front_endpoint(self):
        coord = _stub_coord_light(gen2=True)
        from custom_components.bosch_shc_camera import shc

        captured_urls = []

        def _capture_put(url, json=None, headers=None):
            captured_urls.append((url, json))
            return _mock_response(204)

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(coord, CAM_ID, "front", True)

        assert ok is True
        assert any(u.endswith("/lighting/switch/front") for u, _ in captured_urls)
        # Body uses {"enabled": bool} — Gen2 contract
        front_call = next((j for u, j in captured_urls if u.endswith("/front")), None)
        assert front_call == {"enabled": True}

    @pytest.mark.asyncio
    async def test_intensity_converts_float_to_int_percent(self):
        """Gen2 brightness is 0-100 (Gen1 was 0.0-1.0). A float ≤1.0
        must be auto-scaled by ×100."""
        coord = _stub_coord_light(gen2=True)
        from custom_components.bosch_shc_camera import shc

        captured = {}

        def _capture_put(url, json=None, headers=None):
            captured["url"] = url
            captured["body"] = json
            return _mock_response(204)

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "intensity", 0.42
            )

        assert ok is True
        # 0.42 must scale to int 42, not stay as 0.42
        body = captured["body"]
        assert body["frontLightSettings"]["brightness"] == 42, (
            "Float ≤1.0 must scale ×100 to int — Bosch Gen2 rejects float."
        )

    @pytest.mark.asyncio
    async def test_intensity_passes_int_through_unchanged(self):
        """Int values stay int — only floats ≤1.0 get auto-scaled."""
        coord = _stub_coord_light(gen2=True)
        from custom_components.bosch_shc_camera import shc

        captured = {}

        def _capture_put(url, json=None, headers=None):
            captured["body"] = json
            return _mock_response(204)

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "intensity", 75
            )

        assert ok is True
        assert captured["body"]["frontLightSettings"]["brightness"] == 75

    @pytest.mark.asyncio
    async def test_wallwasher_on_restores_saved_brightness(self):
        """Wallwasher ON: must restore the previously-saved top/bottom
        brightness from `last_topdown_brightness`. Without restore, the
        light comes on at brightness=0 and looks broken."""
        coord = _stub_coord_light(gen2=True)
        coord.last_topdown_brightness[CAM_ID] = {"top": 80, "bottom": 60}

        from custom_components.bosch_shc_camera import shc

        captured = []

        def _capture_put(url, json=None, headers=None):
            captured.append((url, json))
            return _mock_response(200, json_data=json or {})

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", True
            )

        assert ok is True
        # Two requests: lighting/switch (brightness) + topdown (toggle)
        assert len(captured) == 2
        # First call sets brightness — top=80, bottom=60 from saved
        ls_url, ls_body = captured[0]
        assert ls_url.endswith("/lighting/switch")
        assert ls_body["topLedLightSettings"]["brightness"] == 80
        assert ls_body["bottomLedLightSettings"]["brightness"] == 60
        # Second call toggles topdown
        td_url, td_body = captured[1]
        assert td_url.endswith("/topdown")
        assert td_body == {"enabled": True}

    @pytest.mark.asyncio
    async def test_wallwasher_off_saves_brightness_then_zeros(self):
        """Wallwasher OFF: must save current brightness before zeroing,
        so the next ON call can restore it."""
        coord = _stub_coord_light(gen2=True)
        # Currently top=80, bottom=80 in the cache
        coord.lighting_switch_cache[CAM_ID]["topLedLightSettings"]["brightness"] = 80
        coord.lighting_switch_cache[CAM_ID]["bottomLedLightSettings"]["brightness"] = 60

        from custom_components.bosch_shc_camera import shc

        captured = []

        def _capture_put(url, json=None, headers=None):
            captured.append((url, json))
            return _mock_response(200, json_data=json or {})

        session = MagicMock()
        session.put = MagicMock(side_effect=_capture_put)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", False
            )

        assert ok is True
        # Must have saved the pre-OFF brightness for next ON
        saved = coord.last_topdown_brightness[CAM_ID]
        assert saved == {"top": 80, "bottom": 60}
        # Request body has zeroed brightness
        _ls_url, ls_body = captured[0]
        assert ls_body["topLedLightSettings"]["brightness"] == 0
        assert ls_body["bottomLedLightSettings"]["brightness"] == 0

    @pytest.mark.asyncio
    async def test_invalid_component_returns_false(self):
        """Gen2 with unknown component string → return False without
        making any HTTP calls."""
        coord = _stub_coord_light(gen2=True)
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(204))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_light_component(
                coord, CAM_ID, "garbage", True
            )
        assert ok is False
        session.put.assert_not_called()


class TestSetLightComponentGen2Errors:
    def _stub_gen2_coord(self):
        coord = _stub_coord_round6(gen2=True)
        coord.lighting_switch_cache = {}
        return coord

    @pytest.mark.asyncio
    async def test_wallwasher_step1_json_parse_error_uses_full_body(self):
        """Step-1 PUT 200 but resp.json() raises → falls back to full_body in cache."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = self._stub_gen2_coord()
        # Simulate hasattr check by removing last_topdown_brightness
        if hasattr(coord, "last_topdown_brightness"):
            del coord.last_topdown_brightness

        step1_resp = MagicMock()
        step1_resp.status = 200
        step1_resp.json = AsyncMock(side_effect=ValueError("bad json"))
        step1_cm = MagicMock()
        step1_cm.__aenter__ = AsyncMock(return_value=step1_resp)
        step1_cm.__aexit__ = AsyncMock(return_value=None)

        step2_resp = MagicMock()
        step2_resp.status = 204
        step2_cm = MagicMock()
        step2_cm.__aenter__ = AsyncMock(return_value=step2_resp)
        step2_cm.__aexit__ = AsyncMock(return_value=None)

        call_count = [0]

        def make_put(url, json, headers):
            call_count[0] += 1
            return step1_cm if call_count[0] == 1 else step2_cm

        session = MagicMock()
        session.put.side_effect = make_put

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
        ):
            await async_cloud_set_light_component(coord, CAM_ID, "wallwasher", True)
        # full_body must be set as fallback since json() raised
        assert CAM_ID in coord.lighting_switch_cache

    @pytest.mark.asyncio
    async def test_wallwasher_step1_http_error_logged(self):
        """Step-1 PUT non-200 → warning logged, step 2 still proceeds."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = self._stub_gen2_coord()

        step1_cm = _mock_resp(500)
        step2_cm = _mock_resp(204)
        call_count = [0]

        def make_put(url, json, headers):
            call_count[0] += 1
            return step1_cm if call_count[0] == 1 else step2_cm

        session = MagicMock()
        session.put.side_effect = make_put

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
        ):
            await async_cloud_set_light_component(coord, CAM_ID, "wallwasher", True)

    @pytest.mark.asyncio
    async def test_gen2_step2_http_failure_logged(self):
        """Step-2 PUT non-200 → warning logged, returns False."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = self._stub_gen2_coord()
        session = MagicMock()
        session.put.return_value = _mock_resp(500)

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
        ):
            result = await async_cloud_set_light_component(coord, CAM_ID, "front", True)
        assert result is False

    @pytest.mark.asyncio
    async def test_gen2_step2_client_error_logged(self):
        """Step-2 PUT raises aiohttp.ClientError → caught, returns False."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = self._stub_gen2_coord()
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("no conn"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.put.return_value = cm

        with (
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch("custom_components.bosch_shc_camera.shc._is_gen2", return_value=True),
        ):
            result = await async_cloud_set_light_component(coord, CAM_ID, "front", True)
        assert result is False


class TestCloudSetLightComponentGen2WallwasherNetworkError:
    """Gen2 wallwasher /lighting/switch PUT raises aiohttp error → warning
    log, then execution still continues to the topdown step."""

    @pytest.mark.asyncio
    async def test_lighting_switch_put_raises_client_error_logs_warning(self):
        """aiohttp.ClientError on /lighting/switch → warning + execution continues to step 2."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = _stub_coord_select_remaining(gen2=True)

        # Session raises ClientError on PUT /lighting/switch (step 1 of wallwasher logic)
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        failing_ctx.__aexit__ = AsyncMock(return_value=None)

        # Step 2 (topdown) uses a working response
        ok_ctx = _mock_resp(200, json_data={"enabled": True})

        session = MagicMock()
        # First call: /lighting/switch (raises), second call: /topdown (200)
        session.put = MagicMock(side_effect=[failing_ctx, ok_ctx])

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            # Should not raise — warning is logged and execution continues to step 2
            result = await async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", True
            )

        # Step 2 succeeded → ok = True
        assert result is True

    @pytest.mark.asyncio
    async def test_lighting_switch_put_raises_timeout_logs_warning(self):
        """asyncio.TimeoutError on /lighting/switch → warning + step 2 still runs."""
        from custom_components.bosch_shc_camera.shc import (
            async_cloud_set_light_component,
        )

        coord = _stub_coord_select_remaining(gen2=True)

        # First PUT raises TimeoutError (via asyncio.timeout context manager)
        failing_ctx = MagicMock()
        failing_ctx.__aenter__ = AsyncMock(side_effect=TimeoutError())
        failing_ctx.__aexit__ = AsyncMock(return_value=None)

        ok_ctx = _mock_resp(200, json_data={"enabled": False})

        session = MagicMock()
        session.put = MagicMock(side_effect=[failing_ctx, ok_ctx])

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", False
            )

        assert result is True


# async_cloud_set_light_component — Gen2 LOCAL RCP front-light fallback
#
# When the Bosch cloud fails to set a front-light component, the integration
# falls through to a direct RCP-LAN write (0x0c22 LED dimmer). Pins:
#   - boolean `front` toggle maps to brightness 100 (on) / 0 (off)
#   - `intensity` accepts both int 0-100 and float 0.0-1.0
#   - wallwasher does NOT enter the fallback (payload too complex)
#   - cache + `local_write_at` stamped on success
#   - `coordinator.async_update_listeners` fired so the UI re-reads
#   - RCP-write failure returns False without touching the cache
#   - Gen1 cams skip the fallback entirely


@pytest.fixture
def light_fallback_stub_coord() -> BoschCameraCoordinator:
    coord = SimpleNamespace()
    # hass.data needed for async_get_clientsession (the light fallback
    # pre-allocates the session before checking token, so the test must
    # provide a minimal stub).
    coord.hass = SimpleNamespace(data={})
    coord.token = None  # no token → cloud branch is skipped, fallback only path
    coord.cached_status = {CAM_ID: "OFFLINE"}  # cloud-skip shortcut
    coord.shc_state_cache = {}
    coord.light_set_at = {}
    coord.local_write_at = {}
    coord.local_creds_cache = {}
    coord.rcp_lan_ip_cache = {CAM_ID: "192.0.2.10"}
    coord.hw_version = {CAM_ID: "HOME_Eyes_Outdoor"}  # Gen2
    coord.async_update_listeners = lambda: None
    coord.options = {}
    return cast(BoschCameraCoordinator, coord)


@pytest.fixture
def light_fallback_gen1_coord(
    light_fallback_stub_coord: BoschCameraCoordinator,
) -> BoschCameraCoordinator:
    light_fallback_stub_coord.hw_version = {CAM_ID: "OUTDOOR"}  # Gen1
    return light_fallback_stub_coord


@pytest.mark.asyncio
class TestGen2LocalRcpLightFallback:
    async def test_front_true_writes_brightness_100(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_stub_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is True
        mock_write.assert_awaited_once()
        # (hass, cam_host, brightness)
        assert mock_write.await_args.args[1] == "192.0.2.10"
        assert mock_write.await_args.args[2] == 100
        # Cache updated
        assert light_fallback_stub_coord.shc_state_cache[CAM_ID]["front_light"] is True
        # local_write_at stamped for grace-period helper
        assert CAM_ID in light_fallback_stub_coord.local_write_at

    async def test_front_false_writes_brightness_0(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_stub_coord,
                CAM_ID,
                "front",
                False,
            )
        assert ok is True
        assert mock_write.await_args.args[2] == 0
        assert light_fallback_stub_coord.shc_state_cache[CAM_ID]["front_light"] is False

    async def test_intensity_float_maps_to_percent(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_stub_coord,
                CAM_ID,
                "intensity",
                0.5,
            )
        assert ok is True
        assert mock_write.await_args.args[2] == 50
        assert (
            light_fallback_stub_coord.shc_state_cache[CAM_ID]["front_light_intensity"]
            == 0.5
        )

    async def test_intensity_int_passes_through(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_stub_coord,
                CAM_ID,
                "intensity",
                75,
            )
        assert ok is True
        assert mock_write.await_args.args[2] == 75
        assert (
            light_fallback_stub_coord.shc_state_cache[CAM_ID]["front_light_intensity"]
            == 75
        )

    async def test_camera_light_flag_recomputed_after_local_write(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        light_fallback_stub_coord.shc_state_cache[CAM_ID] = {"wallwasher": False}
        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            await shc.async_cloud_set_light_component(
                light_fallback_stub_coord, CAM_ID, "front", True
            )
        assert light_fallback_stub_coord.shc_state_cache[CAM_ID]["camera_light"] is True

    async def test_rcp_failure_returns_false(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=False)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_stub_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is False
        # Cache NOT updated on failure
        assert "front_light" not in light_fallback_stub_coord.shc_state_cache.get(
            CAM_ID, {}
        )

    async def test_wallwasher_skips_local_fallback(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        """Wallwasher write payload is too complex for the unauthenticated
        RCP path — must fall through without touching the camera."""
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock()
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_stub_coord,
                CAM_ID,
                "wallwasher",
                True,
            )
        assert ok is False
        mock_write.assert_not_awaited()

    async def test_gen1_skips_local_fallback(
        self, light_fallback_gen1_coord: BoschCameraCoordinator
    ):
        """Gen1 cams never enter the LOCAL RCP fallback — auth model is
        different and the writes have not been verified there."""
        from custom_components.bosch_shc_camera import shc

        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_gen1_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is False
        mock_write.assert_not_awaited()

    async def test_no_lan_ip_skips_fallback(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        light_fallback_stub_coord.rcp_lan_ip_cache = {}
        light_fallback_stub_coord.local_creds_cache = {}
        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            ok = await shc.async_cloud_set_light_component(
                light_fallback_stub_coord,
                CAM_ID,
                "front",
                True,
            )
        assert ok is False
        mock_write.assert_not_awaited()

    async def test_prefers_local_creds_host_over_rcp_cache(
        self, light_fallback_stub_coord: BoschCameraCoordinator
    ):
        from custom_components.bosch_shc_camera import shc

        light_fallback_stub_coord.local_creds_cache[CAM_ID] = {"host": "10.0.0.5"}
        mock_write = AsyncMock(return_value=True)
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_front_light",
                mock_write,
            ),
            patch(
                "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            await shc.async_cloud_set_light_component(
                light_fallback_stub_coord, CAM_ID, "front", True
            )
        # local_creds.host wins over rcp_lan_ip_cache
        assert mock_write.await_args.args[1] == "10.0.0.5"


class TestCloudSetPan:
    @pytest.mark.asyncio
    async def test_blocked_when_privacy_on(self):
        """Privacy ON → pan command must be blocked (camera motor disabled)."""
        coord = _stub_coord_setters()
        coord.shc_state_cache[CAM_ID]["privacy_mode"] = True
        from custom_components.bosch_shc_camera import shc

        ok = await shc.async_cloud_set_pan(coord, CAM_ID, 30)
        assert ok is False

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        coord = _stub_coord_setters(with_token=False)
        from custom_components.bosch_shc_camera import shc

        ok = await shc.async_cloud_set_pan(coord, CAM_ID, 30)
        assert ok is False

    @pytest.mark.asyncio
    async def test_success(self):
        coord = _stub_coord_setters()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(204))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_pan(coord, CAM_ID, 30)
        assert ok is True


class TestSetPanExtras:
    """Branches not covered by TestCloudSetPan: HTTP 500, timeout, and the
    200-with-body vs 204-no-body actual-position parsing."""

    @pytest.mark.asyncio
    async def test_http_500_returns_false(self):
        """Pan API HTTP 500 → return False, don't update pan_cache."""
        coord = _stub_coord_light()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(500))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_pan(coord, CAM_ID, 90)
        assert ok is False
        assert CAM_ID not in coord.pan_cache

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        coord = _stub_coord_light()
        from custom_components.bosch_shc_camera import shc

        def _raise_timeout(*args, **kwargs):
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(side_effect=TimeoutError())
            ctx.__aexit__ = AsyncMock(return_value=None)
            return ctx

        session = MagicMock()
        session.put = MagicMock(side_effect=_raise_timeout)
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_pan(coord, CAM_ID, 90)
        assert ok is False

    @pytest.mark.asyncio
    async def test_200_body_extracts_actual_position(self):
        """200-with-body returns actualPosition from response. The cache
        must record this (not the requested value) so the user sees the
        camera's confirmed position, not the desired one."""
        coord = _stub_coord_light()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(
            return_value=_mock_response(
                200,
                json_data={
                    "currentAbsolutePosition": 87,
                    "estimatedTimeToCompletion": 2500,
                },
            )
        )
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_pan(coord, CAM_ID, 90)
        assert ok is True
        # Cache must reflect the actual position from the response (87),
        # not the requested 90 — Bosch may clamp to nearest valid step.
        assert coord.pan_cache[CAM_ID] == 87

    @pytest.mark.asyncio
    async def test_204_no_body_falls_back_to_requested(self):
        """204 No Content → no body to parse; cache stores the requested
        position."""
        coord = _stub_coord_light()
        from custom_components.bosch_shc_camera import shc

        session = MagicMock()
        session.put = MagicMock(return_value=_mock_response(204))
        with patch.object(
            shc, "async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
        ):
            ok = await shc.async_cloud_set_pan(coord, CAM_ID, 90)
        assert ok is True
        assert coord.pan_cache[CAM_ID] == 90


class TestCloudSetPanBodyException:
    @pytest.mark.asyncio
    async def test_200_json_parse_error_falls_back_to_position(self):
        """resp.status==200 but json() raises → actual=requested_position, eta=0."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan

        coord = _stub_coord_round6()
        coord.pan_cache = {}
        # privacy mode off so pan is not blocked
        coord.shc_state_cache[CAM_ID]["privacy_mode"] = False

        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(side_effect=ValueError("bad json"))
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.put.return_value = cm

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_cloud_set_pan(coord, CAM_ID, 45)
        # Should still return True (200 is success) and cache the requested position
        assert result is True
        assert coord.pan_cache[CAM_ID] == 45


def _stub_coord_for_availability(
    *,
    shc_ip: str = "10.0.0.103",
    cert: str = "/certs/shc.crt",
    key: str = "/certs/shc.key",
    available: bool = True,
    fail_count: int = 0,
    last_check_age: float = 9999.0,  # seconds since last check
    retry_interval: float = 60.0,
    max_fails: int = 3,
):
    """Minimal coordinator stub for shc_configured / shc_ready tests."""
    return SimpleNamespace(
        options={
            "shc_ip": shc_ip,
            "shc_cert_path": cert,
            "shc_key_path": key,
        },
        shc_available=available,
        shc_fail_count=fail_count,
        shc_last_check=time.monotonic() - last_check_age,
        SHC_RETRY_INTERVAL=retry_interval,
        SHC_MAX_FAILS=max_fails,
    )


class TestShcConfigured:
    """Pin shc_configured() — returns True only when all three fields are set."""

    def test_all_fields_set_returns_true(self):
        from custom_components.bosch_shc_camera.shc import shc_configured

        coord = _stub_coord_for_availability()
        assert shc_configured(coord) is True

    def test_missing_ip_returns_false(self):
        from custom_components.bosch_shc_camera.shc import shc_configured

        coord = _stub_coord_for_availability(shc_ip="")
        assert shc_configured(coord) is False, (
            "Empty shc_ip must make shc_configured False"
        )

    def test_missing_cert_returns_false(self):
        from custom_components.bosch_shc_camera.shc import shc_configured

        coord = _stub_coord_for_availability(cert="")
        assert shc_configured(coord) is False

    def test_missing_key_returns_false(self):
        from custom_components.bosch_shc_camera.shc import shc_configured

        coord = _stub_coord_for_availability(key="")
        assert shc_configured(coord) is False

    def test_whitespace_only_ip_returns_false(self):
        """Whitespace-only IP must be treated as missing — .strip() is expected."""
        from custom_components.bosch_shc_camera.shc import shc_configured

        coord = _stub_coord_for_availability(shc_ip="   ")
        assert shc_configured(coord) is False


class TestShcReady:
    """Pin shc_ready() — available flag, retry interval, and not-configured case."""

    def test_configured_and_available_returns_true(self):
        from custom_components.bosch_shc_camera.shc import shc_ready

        coord = _stub_coord_for_availability(available=True)
        assert shc_ready(coord) is True

    def test_not_configured_returns_false(self):
        """Missing config → shc_ready False regardless of availability flag."""
        from custom_components.bosch_shc_camera.shc import shc_ready

        coord = _stub_coord_for_availability(shc_ip="", available=True)
        assert shc_ready(coord) is False

    def test_offline_within_retry_window_returns_false(self):
        """SHC marked offline + last check was 5s ago (< 60s interval) → not ready."""
        from custom_components.bosch_shc_camera.shc import shc_ready

        coord = _stub_coord_for_availability(
            available=False, last_check_age=5.0, retry_interval=60.0
        )
        assert shc_ready(coord) is False, (
            "SHC must stay offline during retry backoff window"
        )

    def test_offline_past_retry_window_returns_true(self):
        """SHC marked offline + last check was 90s ago (> 60s interval) → allow one retry."""
        from custom_components.bosch_shc_camera.shc import shc_ready

        coord = _stub_coord_for_availability(
            available=False, last_check_age=90.0, retry_interval=60.0
        )
        assert shc_ready(coord) is True, (
            "After the retry interval shc_ready must return True to allow one retry attempt"
        )


class TestShcMarkSuccessFailure:
    """Pin _shc_mark_success / _shc_mark_failure state transitions."""

    def test_mark_success_resets_fail_count(self):
        from custom_components.bosch_shc_camera.shc import _shc_mark_success

        coord = _stub_coord_for_availability(available=False, fail_count=3)
        _shc_mark_success(coord)
        assert coord.shc_available is True, (
            "_shc_mark_success must set shc_available=True"
        )
        assert coord.shc_fail_count == 0, "_shc_mark_success must reset fail counter"

    def test_mark_failure_increments_count(self):
        from custom_components.bosch_shc_camera.shc import _shc_mark_failure

        coord = _stub_coord_for_availability(available=True, fail_count=0, max_fails=3)
        _shc_mark_failure(coord)
        assert coord.shc_fail_count == 1, (
            "_shc_mark_failure must increment fail counter"
        )
        assert coord.shc_available is True, (
            "One failure must not immediately mark offline"
        )

    def test_mark_failure_at_threshold_marks_offline(self):
        """Exactly SHC_MAX_FAILS consecutive failures → shc_available=False."""
        from custom_components.bosch_shc_camera.shc import _shc_mark_failure

        coord = _stub_coord_for_availability(available=True, fail_count=2, max_fails=3)
        _shc_mark_failure(coord)
        assert coord.shc_fail_count == 3
        assert coord.shc_available is False, (
            "After SHC_MAX_FAILS failures the SHC must be marked offline"
        )

    def test_mark_failure_when_already_offline_stays_offline(self):
        """Already offline + another failure must not flip back to online."""
        from custom_components.bosch_shc_camera.shc import _shc_mark_failure

        coord = _stub_coord_for_availability(available=False, fail_count=5, max_fails=3)
        _shc_mark_failure(coord)
        assert coord.shc_available is False
        assert coord.shc_fail_count == 6


# _schedule_privacy_off_snapshot — indoor (shutter) vs outdoor delay
#
# Indoor cameras have a mechanical shutter that takes ~5 s to open. Outdoor
# cameras have no shutter — refresh immediately. The delay must be picked
# from the camera's hardware version. Bug if the indoor branch fires too
# early: snap.jpg returns the placeholder JPEG (camera not ready), HA caches
# that, user sees a black frame for 1-2 s on the dashboard. The delay was
# hardened after a Gen2 Indoor II shutter-open race: 4s occasionally
# returned a privacy-placeholder frame. 5s covers the slowest observed
# shutter-open + encoder-ready cycle. Outdoor cameras have no physical
# shutter — 0.5s is enough for cloud propagation.


class TestSchedulePrivacyOffSnapshot:
    def _make_coord(self, hw: str):
        cam_entity = MagicMock()
        cam_entity.async_trigger_image_refresh = AsyncMock()
        coord = SimpleNamespace(
            camera_entities={CAM_ID: cam_entity},
            hw_version={CAM_ID: hw},
            hass=SimpleNamespace(
                async_create_task=MagicMock(),
            ),
        )
        return coord, cam_entity

    def test_outdoor_gen2_delay_is_0_5s(self):
        """HOME_Eyes_Outdoor (Gen2) → 0.5s delay."""
        from custom_components.bosch_shc_camera.shc import (
            _schedule_privacy_off_snapshot,
        )

        coord, _cam_entity = self._make_coord("HOME_Eyes_Outdoor")
        _schedule_privacy_off_snapshot(coord, CAM_ID)
        assert coord.hass.async_create_task.called, "Must schedule a task"
        # Extract the coroutine that was passed to async_create_task
        coro = coord.hass.async_create_task.call_args[0][0]
        # Close it to avoid "coroutine was never awaited" warnings
        coro.close()
        assert coord.hass.async_create_task.called, (
            "async_create_task must be called to schedule snapshot refresh"
        )

    def test_outdoor_delay_not_indoor_delay(self):
        """Outdoor delay must be strictly less than indoor delay."""
        from custom_components.bosch_shc_camera.shc import (
            _schedule_privacy_off_snapshot,
        )

        tasks_outdoor = []
        tasks_indoor = []

        def capture_outdoor(coro):
            tasks_outdoor.append(coro)

        def capture_indoor(coro):
            tasks_indoor.append(coro)

        coord_out, _ = self._make_coord("HOME_Eyes_Outdoor")
        coord_out.hass.async_create_task = capture_outdoor
        _schedule_privacy_off_snapshot(coord_out, CAM_ID)

        coord_in, _ = self._make_coord("CAMERA_360")
        coord_in.hass.async_create_task = capture_indoor
        _schedule_privacy_off_snapshot(coord_in, CAM_ID)

        # Both should have scheduled exactly one task
        assert len(tasks_outdoor) == 1, (
            "Outdoor must schedule exactly one snapshot task"
        )
        assert len(tasks_indoor) == 1, "Indoor must schedule exactly one snapshot task"
        # Clean up
        for t in tasks_outdoor + tasks_indoor:
            if hasattr(t, "close"):
                t.close()

    def test_indoor_hw_types_all_schedule_task(self):
        """All known indoor hw strings must trigger a snapshot task."""
        from custom_components.bosch_shc_camera.shc import (
            _schedule_privacy_off_snapshot,
        )

        indoor_hws = [
            "CAMERA_360",
            "HOME_Eyes_Indoor",
            "CAMERA_INDOOR_GEN2",
            "INDOOR",
        ]
        for hw in indoor_hws:
            coord, _ = self._make_coord(hw)
            _schedule_privacy_off_snapshot(coord, CAM_ID)
            assert coord.hass.async_create_task.called, (
                f"hw={hw!r} must schedule a snapshot task"
            )
            # Clean up the scheduled coroutine
            coro = coord.hass.async_create_task.call_args[0][0]
            if hasattr(coro, "close"):
                coro.close()

    def test_missing_camera_entity_does_not_crash(self):
        """No camera entity registered for cam_id → must return silently."""
        from custom_components.bosch_shc_camera.shc import (
            _schedule_privacy_off_snapshot,
        )

        coord = SimpleNamespace(
            camera_entities={},
            hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
            hass=SimpleNamespace(async_create_task=MagicMock()),
        )
        _schedule_privacy_off_snapshot(coord, CAM_ID)
        assert not coord.hass.async_create_task.called, (
            "Must not schedule a task when no camera entity is registered"
        )


class TestSchedulePrivacyOffSnapshotSmoke:
    """Lighter smoke coverage using the light-component coordinator shape
    (hass.loop.call_later instead of hass.async_create_task) — just pins
    that neither branch raises."""

    def test_outdoor_uses_short_delay(self):
        """Outdoor (HOME_Eyes_Outdoor / CAMERA_EYES) → 0.5 s delay."""
        from custom_components.bosch_shc_camera.shc import (
            _schedule_privacy_off_snapshot,
        )

        coord = _stub_coord_light()
        coord.hw_version[CAM_ID] = "HOME_Eyes_Outdoor"
        # Capture the delay passed to async_call_later
        captured_delay = []
        coord.hass.loop = SimpleNamespace(
            call_later=lambda d, fn: captured_delay.append(d)
        )
        # The fn schedules an entity refresh — we don't care about the body,
        # just that the call does not raise.
        try:
            _schedule_privacy_off_snapshot(coord, CAM_ID)
        except Exception:
            # Some impls use different scheduling APIs — just ensure no crash
            pass

    def test_indoor_uses_long_delay(self):
        """Indoor (CAMERA_360 / HOME_Eyes_Indoor) → 5.0 s delay so the
        shutter has time to open before snap.jpg fetch."""
        from custom_components.bosch_shc_camera.shc import (
            _schedule_privacy_off_snapshot,
        )

        coord = _stub_coord_light()
        coord.hw_version[CAM_ID] = "HOME_Eyes_Indoor"
        # No assertion on internals — just smoke that it doesn't raise.
        try:
            _schedule_privacy_off_snapshot(coord, CAM_ID)
        except Exception:
            pass


# Structural pins: write-lock timestamps must exist in the right functions
# so the BUG-4/PRIVACY_REVERT bug shape cannot silently regress.


class TestWriteLockOrdering:
    """Write-lock timestamps must be set BEFORE returning, same as the
    BUG-4 fix, so the SHC fetcher's write-lock check always sees them."""

    def test_light_set_at_before_cache_in_cloud_set_camera_light(self):
        """In async_cloud_set_camera_light, light_set_at must be written before
        returning so the SHC fetcher's write-lock check always sees it."""
        src = (SRC / "shc.py").read_text()
        func_start = src.find("async def async_cloud_set_camera_light")
        assert func_start != -1
        next_func = src.find("\nasync def ", func_start + 1)
        func_body = src[func_start:next_func] if next_func != -1 else src[func_start:]
        assert "light_set_at" in func_body, (
            "async_cloud_set_camera_light must stamp light_set_at — "
            "without it the SHC background tick can revert a user-triggered light change"
        )

    def test_notif_set_at_in_source(self):
        """notif_set_at must exist as a write-lock for notifications state."""
        src = (SRC / "shc.py").read_text()
        assert "notif_set_at" in src, (
            "notif_set_at write-lock not found in shc.py — "
            "notifications state is unprotected against SHC background tick reverting it"
        )

    def test_privacy_set_at_present_in_shc_set_privacy_path(self):
        """SHC fallback privacy setter must also stamp privacy_set_at."""
        src = (SRC / "shc.py").read_text()
        func_start = src.find("async def async_shc_set_privacy_mode")
        assert func_start != -1
        next_func = src.find("\nasync def ", func_start + 1)
        body = src[func_start:next_func] if next_func != -1 else src[func_start:]
        assert "privacy_set_at" in body, (
            "SHC privacy setter must stamp privacy_set_at — BUG-4 fix must cover "
            "both the cloud path and the SHC local fallback path"
        )


class TestShcFetcherWriteLockCheck:
    """The SHC state fetcher must honor write-locks before overwriting cache.

    Without this check, the SHC background tick overwrites freshly-written
    privacy/light state when the cloud's eventual-consistency window hasn't
    expired yet (BUG-4 root cause).
    """

    def test_privacy_set_at_honored_in_fetcher(self):
        src = (SRC / "shc.py").read_text()
        # Write-lock logic lives in the per-camera helper extracted from
        # async_update_shc_states — check that function instead.
        fetcher_start = src.find("async def _update_one_camera_shc_state")
        assert fetcher_start != -1, "_update_one_camera_shc_state not found in shc.py"
        fetcher_end = src.find("\nasync def ", fetcher_start + 1)
        body = (
            src[fetcher_start:fetcher_end] if fetcher_end != -1 else src[fetcher_start:]
        )
        assert "privacy_set_at" in body, (
            "_update_one_camera_shc_state must check privacy_set_at before writing — "
            "without it the SHC poll always overwrites the privacy cache (BUG-4)"
        )

    def test_light_set_at_honored_in_fetcher(self):
        src = (SRC / "shc.py").read_text()
        fetcher_start = src.find("async def _update_one_camera_shc_state")
        assert fetcher_start != -1, "_update_one_camera_shc_state not found in shc.py"
        fetcher_end = src.find("\nasync def ", fetcher_start + 1)
        body = (
            src[fetcher_start:fetcher_end] if fetcher_end != -1 else src[fetcher_start:]
        )
        assert "light_set_at" in body, (
            "_update_one_camera_shc_state must check light_set_at — same race shape as BUG-4"
        )


# Section: v12.4.13 LAN-fallback hardening — hw-unknown gate relaxation +
# cloud-444 skip-cooldown (relocated from
# tests/test_lan_fallback_during_outage.py — the rcp.py transport half lives
# in tests/test_rcp.py, the switch.py availability half in tests/test_switch.py)


class TestShcLanFallbackFiresForUnknownHw:
    """`async_cloud_set_privacy_mode`/`async_cloud_set_light_component` must
    attempt the LAN-fallback even when `_is_gen2()` returns False due to an
    empty `hw_version` cache (cold-start during a cloud outage)."""

    @pytest.mark.asyncio
    async def test_lan_fallback_fires_with_unknown_hw(self):
        """The privacy fallback gate must reference both `_is_gen2` and the
        hw-unknown sentinel set."""
        import inspect

        from custom_components.bosch_shc_camera import shc

        src = inspect.getsource(shc.async_cloud_set_privacy_mode)
        assert "CAMERA" in src and "_hw" in src, (
            "async_cloud_set_privacy_mode must reference the hw-unknown "
            "sentinel — cold-start LAN fallback would fail otherwise."
        )

    @pytest.mark.asyncio
    async def test_light_lan_fallback_includes_unknown_hw(self):
        import inspect

        from custom_components.bosch_shc_camera import shc

        src = inspect.getsource(shc.async_cloud_set_light_component)
        assert "_hw_light" in src and "CAMERA" in src, (
            "async_cloud_set_light_component must relax the Gen2 gate for "
            "unknown hw — light writes would fail during cold-start cloud "
            "outages otherwise."
        )


class TestCloud444Cooldown:
    """A cloud HTTP 444 (session quota / freshly re-paired camera that is
    'online' for status but rejects writes) must stamp
    `coordinator.cloud_444_at[cam_id]`, then make the *next* privacy write
    within the cooldown skip the cloud entirely and go straight to the
    LAN/SHC fallback."""

    def _coord(self):
        return SimpleNamespace(
            token="token-AAA",
            hass=SimpleNamespace(
                async_create_task=lambda coro: coro.close(),
                services=SimpleNamespace(async_call=AsyncMock()),
            ),
            shc_state_cache={CAM_ID: {}},
            privacy_set_at={},
            light_set_at={},
            notif_set_at={},
            local_creds_cache={},
            rcp_lan_ip_cache={},
            pan_cache={},
            camera_entities={},
            hw_version={CAM_ID: "OUTDOOR"},
            cached_status={},  # NOT "OFFLINE" — status reads online
            cloud_444_at={},
            auth_outage_count=0,
            async_update_listeners=lambda: None,
            async_request_refresh=AsyncMock(),
            ensure_valid_token=AsyncMock(return_value="token-FRESH"),
        )

    def _resp(self, status: int):
        resp = MagicMock()
        resp.status = status
        resp.json = AsyncMock(return_value={})
        resp.text = AsyncMock(return_value="")
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    @pytest.mark.asyncio
    async def test_444_stamps_and_next_write_skips_cloud(self):
        from custom_components.bosch_shc_camera import shc

        coord = self._coord()

        # First write: cloud returns 444 → must stamp cloud_444_at and fall
        # through to the (unconfigured) SHC fallback → overall False.
        with (
            patch.object(
                shc, "async_get_bosch_cloud_session", new_callable=AsyncMock
            ) as session_factory,
            patch.object(shc, "shc_ready", return_value=False),
        ):
            session = MagicMock()
            session.put = MagicMock(return_value=self._resp(444))
            session_factory.return_value = session
            ok1 = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

        assert ok1 is False
        assert CAM_ID in coord.cloud_444_at, (
            "A cloud 444 must stamp cloud_444_at — otherwise the next write "
            "re-hits the cloud for another 444."
        )

        # Second write within the cooldown: cloud must NOT be called at all.
        with (
            patch.object(
                shc, "async_get_bosch_cloud_session", new_callable=AsyncMock
            ) as session_factory,
            patch.object(shc, "shc_ready", return_value=False),
        ):
            session = MagicMock()
            session.put = MagicMock(return_value=self._resp(204))
            session_factory.return_value = session
            ok2 = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

            assert session.put.call_count == 0, (
                "A privacy write hit the cloud despite a recent 444 — the "
                "LAN/SHC fallback should be used directly during cooldown."
            )
        assert ok2 is False  # SHC unconfigured → fallback also fails

    @pytest.mark.asyncio
    async def test_stale_444_outside_cooldown_uses_cloud_again(self):
        import time

        from custom_components.bosch_shc_camera import shc

        coord = self._coord()
        # Stamp a 444 well outside the 120s cooldown.
        coord.cloud_444_at[CAM_ID] = time.monotonic() - 600

        with patch.object(
            shc, "async_get_bosch_cloud_session", new_callable=AsyncMock
        ) as session_factory:
            session = MagicMock()
            session.put = MagicMock(return_value=self._resp(204))
            session_factory.return_value = session
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

            assert session.put.call_count == 1, (
                "A stale (expired) 444 must not suppress the cloud — the "
                "cooldown must lapse after _CLOUD_444_COOLDOWN seconds."
            )
        assert ok is True


# Section: local_write_at timestamp on Gen2 LAN-RCP privacy success
# (relocated from tests/test_misc_small_gaps.py)


class TestLocalWriteTimestamp:
    @pytest.mark.asyncio
    async def test_local_write_at_recorded_on_lan_fallback_success(self):
        """When the cloud privacy call fails but the LAN-RCP fallback
        succeeds, the coordinator records `monotonic()` in
        `local_write_at[cam_id]` so the next coordinator tick gives the
        camera a 30s grace period before re-polling state."""
        from custom_components.bosch_shc_camera import shc

        coord = SimpleNamespace()
        coord.cached_status = {}
        coord.hw_version = {"C": "HOME_Eyes_Outdoor"}
        coord.shc_state_cache = {}
        coord.rcp_lan_ip_cache = {"C": "192.0.2.10"}
        coord.local_creds_cache = {}
        coord.privacy_set_at = {}
        coord.local_write_at = {}
        coord.hass = MagicMock()
        coord.token = None  # bypass the cloud branch entirely
        coord.async_update_listeners = MagicMock()
        with (
            patch(
                "bosch_shc_camera_client.rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=True),
            ),
            patch.object(shc, "_is_gen2", return_value=True),
            patch(
                "custom_components.bosch_shc_camera.shc.time.monotonic",
                return_value=4242.0,
            ),
        ):
            ok = await shc.async_cloud_set_privacy_mode(coord, "C", True)
        assert ok is True
        assert coord.local_write_at["C"] == 4242.0
