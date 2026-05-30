"""Lightweight tests for BoschCameraConfigFlow reauth/reconfigure entry points.

Covers lines 444, 451, 464 which are unreachable by the full HA integration
test harness because it requires `hass_frontend` (not installed in this venv).

Strategy: bypass the HA framework entirely by calling the methods directly on
a lightweight stub instance that only stubs out the HA-layer calls needed by
the specific method under test.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_flow():
    """Create a minimal BoschCameraConfigFlow stub via __new__ (bypasses
    AbstractOAuth2FlowHandler.__init__ which needs a real hass instance)."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow

    flow = BoschCameraConfigFlow.__new__(BoschCameraConfigFlow)
    flow.hass = SimpleNamespace()
    flow.context = {}
    return flow


class TestReauthEntryPoint:
    """async_step_reauth delegates to async_step_reauth_confirm (line 444)."""

    @pytest.mark.asyncio
    async def test_reauth_calls_reauth_confirm(self):
        """async_step_reauth(entry_data) must call and return
        async_step_reauth_confirm() (line 444)."""
        flow = _make_flow()

        sentinel = object()
        flow.async_step_reauth_confirm = AsyncMock(return_value=sentinel)

        result = await flow.async_step_reauth({})

        flow.async_step_reauth_confirm.assert_called_once_with()
        assert result is sentinel


class TestReauthConfirmNoInput:
    """async_step_reauth_confirm with user_input=None shows form (line 451)."""

    @pytest.mark.asyncio
    async def test_no_user_input_returns_form(self):
        """user_input is None → return async_show_form('reauth_confirm') (line 451)."""
        flow = _make_flow()

        form_sentinel = {"type": "form", "step_id": "reauth_confirm"}
        flow.async_show_form = MagicMock(return_value=form_sentinel)

        result = await flow.async_step_reauth_confirm(user_input=None)

        flow.async_show_form.assert_called_once_with(step_id="reauth_confirm")
        assert result is form_sentinel

    @pytest.mark.asyncio
    async def test_with_user_input_calls_step_user(self):
        """user_input provided → delegate to async_step_user()."""
        flow = _make_flow()

        user_step_result = {"type": "create_entry"}
        flow.async_step_user = AsyncMock(return_value=user_step_result)

        result = await flow.async_step_reauth_confirm(user_input={})

        flow.async_step_user.assert_called_once_with()
        assert result is user_step_result


class TestReconfigureNoInput:
    """async_step_reconfigure with user_input=None shows form (line 464)."""

    @pytest.mark.asyncio
    async def test_no_user_input_returns_form(self):
        """user_input is None → return async_show_form('reconfigure') (line 464)."""
        flow = _make_flow()

        form_sentinel = {"type": "form", "step_id": "reconfigure"}
        flow.async_show_form = MagicMock(return_value=form_sentinel)

        result = await flow.async_step_reconfigure(user_input=None)

        flow.async_show_form.assert_called_once_with(step_id="reconfigure")
        assert result is form_sentinel

    @pytest.mark.asyncio
    async def test_with_user_input_calls_step_user(self):
        """user_input provided → delegate to async_step_user()."""
        flow = _make_flow()

        user_step_result = {"type": "create_entry"}
        flow.async_step_user = AsyncMock(return_value=user_step_result)

        result = await flow.async_step_reconfigure(user_input={})

        flow.async_step_user.assert_called_once_with()
        assert result is user_step_result
