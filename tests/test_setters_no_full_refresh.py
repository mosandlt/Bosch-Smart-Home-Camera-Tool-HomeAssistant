"""Regression: light / notifications / pan setters must NOT trigger a full coordinator refresh.

Path C (2026-05-29): a forced async_request_refresh() after any cloud/SHC PUT
re-touches go2rtc stream registration for ALL active cameras, causing go2rtc to
TEARDOWN + reconnect unrelated cameras' live sessions. These setters already
write an optimistic cache and call async_update_listeners() immediately, so the
forced refresh is redundant for the toggled camera and destructive for others.

Pins for each of the 5 setters (light SHC, light cloud, light-component,
notifications, pan):
  - optimistic cache is updated
  - async_update_listeners() is called
  - async_request_refresh is NOT called
  - hass.async_create_task is NOT called

Also pins pan-specific: async_update_listeners() is called (added 2026-05-29 —
pan previously had no optimistic listener push at all).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── Shared helpers ────────────────────────────────────────────────────────────


def _put_204_session() -> MagicMock:
    """aiohttp session whose .put(...) async-context-manager yields HTTP 204."""
    resp = MagicMock()
    resp.status = 204
    resp.text = AsyncMock(return_value="")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.put.return_value = cm
    return session


def _put_200_json_session(json_data: dict) -> MagicMock:
    """aiohttp session whose .put(...) yields HTTP 200 with JSON body."""
    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=json_data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.put.return_value = cm
    return session


def _base_coord() -> SimpleNamespace:
    return SimpleNamespace(
        token="tok-AAA",
        _cached_status={},
        _privacy_set_at={},
        _notif_set_at={},
        _light_set_at={},
        _pan_cache={},
        _shc_state_cache={CAM_ID: {"device_id": "shc-dev-1", "front_light_intensity": 0.5}},
        _hw_version={CAM_ID: "HOME_Eyes_Outdoor"},  # gen2
        _lighting_switch_cache={},
        _local_creds_cache={},
        _rcp_lan_ip_cache={},
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
        hass=SimpleNamespace(async_create_task=MagicMock()),
    )


# ── async_shc_set_camera_light ────────────────────────────────────────────────


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
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is True
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_off_no_refresh(self) -> None:
        """SHC light OFF: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_shc_set_camera_light

        coord = _base_coord()
        coord._shc_state_cache[CAM_ID]["camera_light"] = True
        with patch(
            "custom_components.bosch_shc_camera.shc.async_shc_request",
            AsyncMock(return_value={"status": 204, "ok": True}),
        ):
            result = await async_shc_set_camera_light(coord, CAM_ID, False)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is False
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


# ── async_cloud_set_camera_light ──────────────────────────────────────────────


class TestCloudSetCameraLightNoRefresh:
    @pytest.mark.asyncio
    async def test_gen2_on_no_refresh(self) -> None:
        """Cloud light ON (Gen2): cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _base_coord()
        # Gen2 makes two PUT calls (front + topdown)
        resp = MagicMock()
        resp.status = 204
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.put.return_value = cm

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=session,
        ), patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=True,
        ):
            result = await async_cloud_set_camera_light(coord, CAM_ID, True)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is True
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_gen1_off_no_refresh(self) -> None:
        """Cloud light OFF (Gen1): cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_camera_light

        coord = _base_coord()
        coord._hw_version[CAM_ID] = "OUTDOOR"  # Gen1

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ), patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=False,
        ):
            result = await async_cloud_set_camera_light(coord, CAM_ID, False)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is False
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


# ── async_cloud_set_light_component ──────────────────────────────────────────


class TestCloudSetLightComponentNoRefresh:
    @pytest.mark.asyncio
    async def test_front_on_no_refresh(self) -> None:
        """Light component 'front' ON: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_light_component

        coord = _base_coord()

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ), patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=False,
        ):
            result = await async_cloud_set_light_component(coord, CAM_ID, "front", True)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["front_light"] is True
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_front_off_no_refresh(self) -> None:
        """Light component 'front' OFF: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_light_component

        coord = _base_coord()
        coord._shc_state_cache[CAM_ID]["front_light"] = True

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ), patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=False,
        ):
            result = await async_cloud_set_light_component(coord, CAM_ID, "front", False)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["front_light"] is False
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


# ── async_cloud_set_notifications ────────────────────────────────────────────


class TestCloudSetNotificationsNoRefresh:
    @pytest.mark.asyncio
    async def test_enable_no_refresh(self) -> None:
        """Notifications enabled: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _base_coord()

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, True)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["notifications_status"] == "FOLLOW_CAMERA_SCHEDULE"
        assert CAM_ID in coord._notif_set_at
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_disable_no_refresh(self) -> None:
        """Notifications disabled: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_notifications

        coord = _base_coord()

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ):
            result = await async_cloud_set_notifications(coord, CAM_ID, False)

        assert result is True
        assert coord._shc_state_cache[CAM_ID]["notifications_status"] == "ALWAYS_OFF"
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()


# ── async_cloud_set_pan ───────────────────────────────────────────────────────


class TestCloudSetPanNoRefresh:
    @pytest.mark.asyncio
    async def test_pan_positive_no_refresh(self) -> None:
        """Pan to positive position: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan

        coord = _base_coord()
        coord._shc_state_cache[CAM_ID]["privacy_mode"] = False

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ):
            result = await async_cloud_set_pan(coord, CAM_ID, 90)

        assert result is True
        assert coord._pan_cache[CAM_ID] == 90
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_pan_negative_no_refresh(self) -> None:
        """Pan to negative position: cache updated, listeners called, no refresh scheduled."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan

        coord = _base_coord()
        coord._shc_state_cache[CAM_ID]["privacy_mode"] = False

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_204_session(),
        ):
            result = await async_cloud_set_pan(coord, CAM_ID, -45)

        assert result is True
        assert coord._pan_cache[CAM_ID] == -45
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_pan_200_with_json_body_no_refresh(self) -> None:
        """Pan 200 with actual position from response body: correct cache + no refresh."""
        from custom_components.bosch_shc_camera.shc import async_cloud_set_pan

        coord = _base_coord()
        coord._shc_state_cache[CAM_ID]["privacy_mode"] = False

        with patch(
            "custom_components.bosch_shc_camera.shc.async_get_clientsession",
            return_value=_put_200_json_session(
                {"currentAbsolutePosition": 42, "estimatedTimeToCompletion": 500}
            ),
        ):
            result = await async_cloud_set_pan(coord, CAM_ID, 45)

        assert result is True
        assert coord._pan_cache[CAM_ID] == 42  # actual from response, not requested
        coord.async_update_listeners.assert_called()
        coord.async_request_refresh.assert_not_called()
        coord.hass.async_create_task.assert_not_called()
