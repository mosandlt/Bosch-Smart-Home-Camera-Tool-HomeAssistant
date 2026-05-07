"""config_flow.py — Sprint-A round-6 tests.

Covers missing branches from the 73% baseline:
  - BoschOAuth2Implementation.__init__ + async_generate_authorize_url
  - async_resolve_external_data + _async_refresh_token
  - _exchange_code (all branches)
  - BoschCameraConfigFlow: logger property, async_step_user,
    async_step_reauth_confirm (user_input path),
    async_step_reconfigure (user_input path),
    async_oauth_create_entry (all 3 branches),
    async_get_options_flow
  - BoschCameraOptionsFlow: async_step_relogin_show (None branch),
    async_step_relogin_paste (all branches)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


MODULE = "custom_components.bosch_shc_camera.config_flow"


# ── BoschOAuth2Implementation.__init__ ────────────────────────────────────────


class TestBoschOAuth2ImplementationInit:
    def test_init_sets_hass_and_verifier(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        fake_hass = MagicMock()
        impl = BoschOAuth2Implementation(fake_hass)
        assert impl.hass is fake_hass
        assert impl._last_verifier is None


# ── async_generate_authorize_url ──────────────────────────────────────────────


class TestAsyncGenerateAuthorizeUrl:
    @pytest.mark.asyncio
    async def test_stores_verifier_and_returns_url_with_challenge(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        fake_hass = MagicMock()
        impl = BoschOAuth2Implementation(fake_hass)
        with patch(f"{MODULE}._pkce_pair", return_value=("verifier_val", "challenge_val")), \
             patch(f"{MODULE}._encode_jwt", return_value="state_jwt"):
            url = await impl.async_generate_authorize_url("flow-id-1")
        assert impl._last_verifier == "verifier_val"
        assert "code_challenge=challenge_val" in url
        assert "code_challenge_method=S256" in url
        assert "state=state_jwt" in url

    @pytest.mark.asyncio
    async def test_url_contains_client_id_and_scope(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation, CLIENT_ID
        impl = BoschOAuth2Implementation(MagicMock())
        with patch(f"{MODULE}._pkce_pair", return_value=("v", "c")), \
             patch(f"{MODULE}._encode_jwt", return_value="s"):
            url = await impl.async_generate_authorize_url("flow-2")
        assert f"client_id={CLIENT_ID}" in url


# ── async_resolve_external_data ───────────────────────────────────────────────


def _make_mock_cm(status: int, json_data: dict, raise_for_status=None):
    """Build an async context-manager mock for session.post(...)."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value="error body")
    if raise_for_status:
        resp.raise_for_status = MagicMock(side_effect=raise_for_status)
    else:
        resp.raise_for_status = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestAsyncResolveExternalData:
    @pytest.mark.asyncio
    async def test_exchanges_code_for_tokens(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        impl = BoschOAuth2Implementation(MagicMock())
        impl._last_verifier = "myverifier"
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(200, {"access_token": "at", "refresh_token": "rt"})
        with patch(f"{MODULE}.async_get_clientsession", return_value=mock_session):
            result = await impl.async_resolve_external_data({
                "code": "authcode",
                "state": {"redirect_uri": "https://my.home-assistant.io/auth/callback"},
            })
        assert result["access_token"] == "at"

    @pytest.mark.asyncio
    async def test_logs_on_4xx_before_raise(self):
        """HTTP 4xx: error is logged and raise_for_status propagates it."""
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        import aiohttp
        impl = BoschOAuth2Implementation(MagicMock())
        impl._last_verifier = "v"
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(
            400, {}, raise_for_status=aiohttp.ClientResponseError(MagicMock(), ())
        )
        with patch(f"{MODULE}.async_get_clientsession", return_value=mock_session):
            with pytest.raises(aiohttp.ClientResponseError):
                await impl.async_resolve_external_data({
                    "code": "c", "state": {"redirect_uri": "https://r"},
                })


# ── _async_refresh_token ──────────────────────────────────────────────────────


class TestAsyncRefreshToken:
    @pytest.mark.asyncio
    async def test_returns_merged_token_on_200(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        impl = BoschOAuth2Implementation(MagicMock())
        old_token = {"refresh_token": "old_rt", "access_token": "old_at"}
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(200, {"access_token": "new_at"})
        with patch(f"{MODULE}.async_get_clientsession", return_value=mock_session):
            result = await impl._async_refresh_token(old_token)
        assert result["access_token"] == "new_at"
        assert result["refresh_token"] == "old_rt"

    @pytest.mark.asyncio
    async def test_logs_on_4xx_before_raise(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        import aiohttp
        impl = BoschOAuth2Implementation(MagicMock())
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(
            401, {}, raise_for_status=aiohttp.ClientResponseError(MagicMock(), ())
        )
        with patch(f"{MODULE}.async_get_clientsession", return_value=mock_session):
            with pytest.raises(aiohttp.ClientResponseError):
                await impl._async_refresh_token({"refresh_token": "rt"})


# ── _exchange_code ────────────────────────────────────────────────────────────


class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_200_returns_token_dict(self):
        from custom_components.bosch_shc_camera.config_flow import _exchange_code
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(200, {"access_token": "at"})
        result = await _exchange_code(mock_session, "code123", "verifier456")
        assert result["access_token"] == "at"

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        from custom_components.bosch_shc_camera.config_flow import _exchange_code
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(400, {})
        result = await _exchange_code(mock_session, "bad_code", "v")
        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        from custom_components.bosch_shc_camera.config_flow import _exchange_code
        mock_session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        result = await _exchange_code(mock_session, "c", "v")
        assert result is None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self):
        import aiohttp
        from custom_components.bosch_shc_camera.config_flow import _exchange_code
        mock_session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("conn failed"))
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_session.post.return_value = cm
        result = await _exchange_code(mock_session, "c", "v")
        assert result is None


# ── BoschCameraConfigFlow ──────────────────────────────────────────────────


class TestConfigFlowSteps:
    def _make_flow(self, source="user"):
        """Create a flow instance bypassing HA's config-flow framework."""
        from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow
        from homeassistant import config_entries
        flow = BoschCameraConfigFlow.__new__(BoschCameraConfigFlow)
        flow.hass = MagicMock()
        # source is a read-only property backed by context dict
        flow.context = {"source": source}
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_show_form = MagicMock(return_value={"type": "form", "step_id": "x"})
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
        flow._get_reauth_entry = MagicMock(return_value=MagicMock())
        flow._get_reconfigure_entry = MagicMock(return_value=MagicMock())
        return flow

    def test_logger_property_returns_module_logger(self):
        from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow
        import logging
        flow = BoschCameraConfigFlow.__new__(BoschCameraConfigFlow)
        assert isinstance(flow.logger, logging.Logger)

    @pytest.mark.asyncio
    async def test_async_step_user_registers_implementation(self):
        from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow
        from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler
        flow = self._make_flow(source="user")
        with patch(f"{MODULE}.async_register_implementation") as mock_reg, \
             patch.object(AbstractOAuth2FlowHandler, "async_step_user",
                          AsyncMock(return_value={"type": "form"})):
            result = await flow.async_step_user(None)
        assert mock_reg.called

    @pytest.mark.asyncio
    async def test_reauth_confirm_with_user_input_calls_step_user(self):
        """Submitting the reauth-confirm form triggers async_step_user (line 438)."""
        flow = self._make_flow(source="reauth")
        flow.async_step_user = AsyncMock(return_value={"type": "form"})
        result = await flow.async_step_reauth_confirm(user_input={"confirmed": True})
        flow.async_step_user.assert_called_once()

    @pytest.mark.asyncio
    async def test_reconfigure_with_user_input_calls_step_user(self):
        """Submitting the reconfigure form triggers async_step_user (line 451)."""
        flow = self._make_flow(source="reconfigure")
        flow.async_step_user = AsyncMock(return_value={"type": "form"})
        result = await flow.async_step_reconfigure(user_input={"confirmed": True})
        flow.async_step_user.assert_called_once()

    @pytest.mark.asyncio
    async def test_oauth_create_entry_new_flow_calls_async_create_entry(self):
        """Non-reauth / non-reconfigure source → async_create_entry (line 472)."""
        flow = self._make_flow(source="user")
        result = await flow.async_oauth_create_entry({
            "token": {"access_token": "at", "refresh_token": "rt"},
        })
        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args
        assert call_kwargs is not None

    @pytest.mark.asyncio
    async def test_oauth_create_entry_reauth_updates_existing(self):
        """SOURCE_REAUTH → async_update_reload_and_abort (lines 462-466)."""
        from homeassistant import config_entries
        flow = self._make_flow(source=config_entries.SOURCE_REAUTH)
        await flow.async_oauth_create_entry({
            "token": {"access_token": "new_at", "refresh_token": "new_rt"},
        })
        flow.async_update_reload_and_abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_oauth_create_entry_reconfigure_updates_existing(self):
        """SOURCE_RECONFIGURE → async_update_reload_and_abort (lines 467-471)."""
        from homeassistant import config_entries
        flow = self._make_flow(source=config_entries.SOURCE_RECONFIGURE)
        await flow.async_oauth_create_entry({
            "token": {"access_token": "new_at", "refresh_token": "new_rt"},
        })
        flow.async_update_reload_and_abort.assert_called_once()

    def test_async_get_options_flow_returns_options_flow_instance(self):
        """async_get_options_flow must return a BoschCameraOptionsFlow (line 480)."""
        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraConfigFlow, BoschCameraOptionsFlow,
        )
        entry = MagicMock()
        entry.options = {}
        entry.data = {}
        result = BoschCameraConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, BoschCameraOptionsFlow)


# ── BoschCameraOptionsFlow — relogin steps ────────────────────────────────


class TestOptionsFlowReloginSteps:
    def _make_options_flow(self):
        from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow
        flow = BoschCameraOptionsFlow.__new__(BoschCameraOptionsFlow)
        flow._verifier = "pkce_verifier"
        flow._auth_url = "https://id.bosch.com/auth?client_id=x"
        flow._pending_options = {"enable_snapshots": True}
        flow.hass = MagicMock()
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow._config_entry = MagicMock()
        flow._config_entry.entry_id = "01ENTRY"
        flow._config_entry.data = {"bearer_token": "", "refresh_token": ""}
        return flow

    @pytest.mark.asyncio
    async def test_relogin_show_none_input_shows_form(self):
        """user_input=None → show the login URL form (lines 850-854)."""
        flow = self._make_options_flow()
        result = await flow.async_step_relogin_show(user_input=None)
        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args
        assert call_kwargs[1].get("step_id") == "relogin_show" or \
               (call_kwargs[0] and "relogin_show" in str(call_kwargs))

    @pytest.mark.asyncio
    async def test_relogin_show_with_user_input_advances_to_paste(self):
        """Any user_input submission calls async_step_relogin_paste."""
        flow = self._make_options_flow()
        flow.async_step_relogin_paste = AsyncMock(return_value={"type": "form"})
        result = await flow.async_step_relogin_show(user_input={"login_url": "https://auth"})
        flow.async_step_relogin_paste.assert_called_once()

    @pytest.mark.asyncio
    async def test_relogin_paste_none_input_shows_form(self):
        """user_input=None → show the paste form without errors."""
        flow = self._make_options_flow()
        result = await flow.async_step_relogin_paste(user_input=None)
        flow.async_show_form.assert_called_once()

    @pytest.mark.asyncio
    async def test_relogin_paste_invalid_url_shows_error(self):
        """Redirect URL with no `code` parameter → errors['redirect_url'] set."""
        flow = self._make_options_flow()
        result = await flow.async_step_relogin_paste(user_input={"redirect_url": "https://no-code.example.com"})
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert kwargs.get("errors", {}).get("redirect_url") == "invalid_redirect_url"

    @pytest.mark.asyncio
    async def test_relogin_paste_failed_exchange_shows_error(self):
        """Valid code but token exchange fails → errors['redirect_url'] = token_exchange_failed."""
        flow = self._make_options_flow()
        with patch(f"{MODULE}._extract_code", return_value="valid_code"), \
             patch(f"{MODULE}._exchange_code", AsyncMock(return_value=None)), \
             patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
            result = await flow.async_step_relogin_paste(
                user_input={"redirect_url": "https://r.io?code=valid_code"}
            )
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert kwargs.get("errors", {}).get("redirect_url") == "token_exchange_failed"

    @pytest.mark.asyncio
    async def test_relogin_paste_success_reloads_integration(self):
        """Successful token exchange → async_create_entry called + reload scheduled."""
        flow = self._make_options_flow()
        with patch(f"{MODULE}._extract_code", return_value="good_code"), \
             patch(f"{MODULE}._exchange_code", AsyncMock(return_value={
                 "access_token": "new_at", "refresh_token": "new_rt",
             })), \
             patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
            result = await flow.async_step_relogin_paste(
                user_input={"redirect_url": "https://r.io?code=good_code"}
            )
        flow.async_create_entry.assert_called_once()
        flow.hass.config_entries.async_update_entry.assert_called_once()
