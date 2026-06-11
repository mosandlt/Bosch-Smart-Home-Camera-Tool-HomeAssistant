"""Tests for switch.py BoschMotionLightSwitch network-fetch fallback paths.

Pins the uncovered lines in `_set_motion_light` when the cache is empty and
the GET fallback is exercised:

  Line 1135: `token` is falsy → early return (no GET, no PUT).
  Lines 1146-1150: HTTP error path — GET returns non-200 → warning + return;
                   GET raises → exception logged → return.

In all three cases `coordinator.async_put_camera` must NOT be called and
local `_is_on` must remain None (the toggle had no effect).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera.switch"


def _stub_coord(token: str | None = "tok-A", **overrides):
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
        _motion_light_cache={},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _bind_hass(sw):
    """Attach a minimal hass + write_ha_state so async_write_ha_state doesn't raise."""
    sw.hass = SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
    )
    sw.async_write_ha_state = MagicMock()


def _resp_cm(status: int, raise_exc: Exception | None = None, json_data=None):
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
    """Line 1135: empty cache + no token → return before HTTP/PUT.

    A coordinator without a bearer token cannot authenticate the GET, so the
    method must bail out cleanly. Without this guard a None Authorization
    header would be sent and the camera would 401.
    """

    @pytest.mark.asyncio
    async def test_no_token_short_circuits_before_get(self, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord(token=None)
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry)
        _bind_hass(sw)

        # Sentinel: if we reach session.get the test failed.
        session = MagicMock()
        session.get = MagicMock(
            side_effect=AssertionError(
                "session.get must not be called when token is missing"
            )
        )

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            await sw.async_turn_on()

        coord.async_put_camera.assert_not_awaited()
        assert sw._is_on is None, "no token → toggle must not flip local state"


class TestMotionLightGetHttpError:
    """Lines 1146-1147: GET returns non-200 → warn + return; no PUT."""

    @pytest.mark.asyncio
    async def test_http_500_returns_without_put(self, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500))

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            await sw.async_turn_on()

        (
            coord.async_put_camera.assert_not_awaited(),
            ("non-200 GET must short-circuit before async_put_camera"),
        )
        assert sw._is_on is None, "failed GET must leave local state untouched"

    @pytest.mark.asyncio
    async def test_http_401_returns_without_put(self, stub_entry):
        """401 (auth expired) is just another non-200 status — same early return."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(401))

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            await sw.async_turn_off()

        coord.async_put_camera.assert_not_awaited()


class TestMotionLightGetRaises:
    """Lines 1148-1150: GET raises → broad except logs + returns; no PUT.

    Covers timeout, connection error, and any other transport failure.
    """

    @pytest.mark.asyncio
    async def test_get_raises_timeout_returns_without_put(self, stub_entry):
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(0, raise_exc=TimeoutError()))

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            # Must not raise even though session.get raises
            await sw.async_turn_on()

        (
            coord.async_put_camera.assert_not_awaited(),
            ("GET timeout must be swallowed and short-circuit before PUT"),
        )
        assert sw._is_on is None

    @pytest.mark.asyncio
    async def test_get_raises_generic_returns_without_put(self, stub_entry):
        """Any unexpected error from session.get is caught and the call no-ops."""
        from custom_components.bosch_shc_camera.switch import BoschMotionLightSwitch

        coord = _stub_coord()
        sw = BoschMotionLightSwitch(coord, CAM_ID, stub_entry)
        _bind_hass(sw)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(0, raise_exc=RuntimeError("boom"))
        )

        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)):
            await sw.async_turn_off()

        coord.async_put_camera.assert_not_awaited()
        assert sw._is_on is None
