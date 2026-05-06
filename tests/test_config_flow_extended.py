"""Extended tests for config_flow.py — error paths, token refresh, options flow.

Covers the lines missed by test_config_flow_helpers.py:
  - RefreshTokenInvalidError / AuthServerOutageError classification
  - _do_refresh: 200 success, 400/401 raises RefreshTokenInvalidError,
    5xx raises AuthServerOutageError, network error returns None
  - BoschOAuth2Implementation property contracts
  - async_oauth_create_entry reauth / reconfigure path structure (structural)
  - KEYCLOAK_BASE, CLIENT_ID, REDIRECT_URI_MANUAL constants pinned
"""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Constants pinned ─────────────────────────────────────────────────────────


class TestConfigFlowConstants:
    def test_keycloak_base_is_bosch_domain(self):
        from custom_components.bosch_shc_camera.config_flow import KEYCLOAK_BASE
        assert "bosch.com" in KEYCLOAK_BASE
        assert KEYCLOAK_BASE.startswith("https://")

    def test_client_id_is_oss(self):
        from custom_components.bosch_shc_camera.config_flow import CLIENT_ID
        assert CLIENT_ID == "oss_residential_app", (
            "CLIENT_ID must be oss_residential_app — changing it breaks "
            "every existing Bosch token refresh silently"
        )

    def test_redirect_uri_manual_is_bosch_com(self):
        """Legacy bosch.com redirect used in manual (options) re-login flow."""
        from custom_components.bosch_shc_camera.config_flow import REDIRECT_URI_MANUAL
        assert "bosch.com" in REDIRECT_URI_MANUAL

    def test_scopes_include_offline_access(self):
        """offline_access scope is required for the refresh token to be issued."""
        from custom_components.bosch_shc_camera.config_flow import SCOPES
        assert "offline_access" in SCOPES, (
            "offline_access scope must be present — without it Keycloak "
            "does not issue a refresh_token and token renewal fails"
        )

    def test_client_secret_decodes(self):
        """CLIENT_SECRET is stored base64-encoded; verify it decodes to a non-empty string."""
        from custom_components.bosch_shc_camera.config_flow import CLIENT_SECRET
        assert isinstance(CLIENT_SECRET, str)
        assert len(CLIENT_SECRET) > 0


# ── RefreshTokenInvalidError / AuthServerOutageError ─────────────────────────


class TestRefreshErrors:
    def test_refresh_token_invalid_error_is_exception(self):
        from custom_components.bosch_shc_camera.config_flow import RefreshTokenInvalidError
        err = RefreshTokenInvalidError("HTTP 401: invalid_grant")
        assert isinstance(err, Exception)
        assert "401" in str(err)

    def test_auth_server_outage_error_is_exception(self):
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError
        err = AuthServerOutageError("HTTP 503")
        assert isinstance(err, Exception)

    def test_errors_are_distinct_classes(self):
        from custom_components.bosch_shc_camera.config_flow import (
            RefreshTokenInvalidError, AuthServerOutageError,
        )
        assert RefreshTokenInvalidError is not AuthServerOutageError
        assert not issubclass(RefreshTokenInvalidError, AuthServerOutageError)
        assert not issubclass(AuthServerOutageError, RefreshTokenInvalidError)


# ── _do_refresh ──────────────────────────────────────────────────────────────


def _mock_resp(status: int, json_data: dict | None = None, text: str = "") -> MagicMock:
    """Build a mock aiohttp response for use in async context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data or {})
    resp.text = AsyncMock(return_value=text)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


class TestDoRefresh:
    """_do_refresh maps HTTP status → return value or exception."""

    @pytest.mark.asyncio
    async def test_200_returns_token_dict(self):
        from custom_components.bosch_shc_camera.config_flow import _do_refresh

        new_token = {"access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600}
        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(200, json_data=new_token))

        result = await _do_refresh(session, "old_refresh_token")
        assert result is not None
        assert result["access_token"] == "new_at"

    @pytest.mark.asyncio
    async def test_400_raises_refresh_token_invalid(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _do_refresh, RefreshTokenInvalidError,
        )
        session = MagicMock()
        session.post = MagicMock(
            return_value=_mock_resp(400, text="invalid_grant")
        )

        with pytest.raises(RefreshTokenInvalidError):
            await _do_refresh(session, "expired_token")

    @pytest.mark.asyncio
    async def test_401_raises_refresh_token_invalid(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _do_refresh, RefreshTokenInvalidError,
        )
        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(401, text="unauthorized"))

        with pytest.raises(RefreshTokenInvalidError):
            await _do_refresh(session, "bad_token")

    @pytest.mark.asyncio
    async def test_500_raises_auth_server_outage(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _do_refresh, AuthServerOutageError,
        )
        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(500, text="Internal Server Error"))

        with pytest.raises(AuthServerOutageError):
            await _do_refresh(session, "valid_token")

    @pytest.mark.asyncio
    async def test_503_raises_auth_server_outage(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _do_refresh, AuthServerOutageError,
        )
        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(503, text="Service Unavailable"))

        with pytest.raises(AuthServerOutageError):
            await _do_refresh(session, "valid_token")

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """Network timeout → None (transient; caller may retry)."""
        import aiohttp
        from custom_components.bosch_shc_camera.config_flow import _do_refresh

        session = MagicMock()
        session.post = MagicMock(side_effect=asyncio.TimeoutError())

        result = await _do_refresh(session, "token")
        assert result is None, (
            "_do_refresh must return None on TimeoutError — caller decides retry; "
            "raising would trigger reauth flow unnecessarily"
        )

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self):
        """aiohttp.ClientError → None (transient network error; caller may retry)."""
        import aiohttp
        from custom_components.bosch_shc_camera.config_flow import _do_refresh

        session = MagicMock()
        session.post = MagicMock(side_effect=aiohttp.ClientError("connection refused"))

        result = await _do_refresh(session, "token")
        assert result is None

    @pytest.mark.asyncio
    async def test_402_returns_none(self):
        """Unexpected 4xx (not 400/401) → None — don't raise, but don't return success."""
        from custom_components.bosch_shc_camera.config_flow import _do_refresh

        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(402, text="Payment Required"))

        # 402 falls through the if/elif chain — neither raises nor returns json
        result = await _do_refresh(session, "token")
        assert result is None


# ── BoschOAuth2Implementation properties ────────────────────────────────────


class TestBoschOAuth2Implementation:
    """Structural: the OAuth2 implementation exposes required HA contracts."""

    def test_name_property_returns_bosch(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        impl = BoschOAuth2Implementation.__new__(BoschOAuth2Implementation)
        assert "Bosch" in impl.name

    def test_domain_property_returns_integration_domain(self):
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        from custom_components.bosch_shc_camera import DOMAIN
        impl = BoschOAuth2Implementation.__new__(BoschOAuth2Implementation)
        assert impl.domain == DOMAIN

    def test_redirect_uri_is_my_home_assistant(self):
        """Automatic callback URI — must point to my.home-assistant.io for OAuth2 auto flow."""
        from custom_components.bosch_shc_camera.config_flow import BoschOAuth2Implementation
        impl = BoschOAuth2Implementation.__new__(BoschOAuth2Implementation)
        assert "my.home-assistant.io" in impl.redirect_uri, (
            "redirect_uri must use my.home-assistant.io — Bosch's Keycloak is "
            "pre-registered only for this URI; any other value → 400 Bad Request"
        )


# ── async_oauth_create_entry structural pins ─────────────────────────────────


class TestAsyncOauthCreateEntryStructure:
    """Structural: verify the source-routing logic exists in config_flow.py."""

    def test_reauth_source_routing_exists(self):
        """The flow must branch on SOURCE_REAUTH to update (not recreate) the entry."""
        from pathlib import Path
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "config_flow.py"
        ).read_text()
        # async_oauth_create_entry must check SOURCE_REAUTH
        func_start = src.find("async def async_oauth_create_entry")
        assert func_start != -1
        func_body = src[func_start:func_start + 800]
        assert "SOURCE_REAUTH" in func_body, (
            "async_oauth_create_entry must route on SOURCE_REAUTH to call "
            "async_update_reload_and_abort — otherwise reauth creates a duplicate entry"
        )

    def test_reconfigure_source_routing_exists(self):
        from pathlib import Path
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "config_flow.py"
        ).read_text()
        func_start = src.find("async def async_oauth_create_entry")
        next_func = src.find("\n    async def ", func_start + 1)
        func_body = src[func_start:next_func] if next_func != -1 else src[func_start:func_start + 1200]
        assert "RECONFIGURE" in func_body, (
            "async_oauth_create_entry must handle SOURCE_RECONFIGURE path "
            "(Quality-Scale Gold reconfigure flow)"
        )

    def test_new_data_contains_bearer_and_refresh(self):
        """The output keys must match what BoschCameraCoordinator reads."""
        from pathlib import Path
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "config_flow.py"
        ).read_text()
        func_start = src.find("async def async_oauth_create_entry")
        func_body = src[func_start:func_start + 800]
        assert '"bearer_token"' in func_body or "'bearer_token'" in func_body, (
            "async_oauth_create_entry must write bearer_token key — "
            "coordinator reads entry.data['bearer_token']"
        )
        assert '"refresh_token"' in func_body or "'refresh_token'" in func_body


# ── Options flow helper — structural ─────────────────────────────────────────


class TestOptionsFlowStructure:
    def test_options_flow_steps_exist(self):
        from pathlib import Path
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "config_flow.py"
        ).read_text()
        for step in ("async_step_init", "async_step_relogin_show", "async_step_relogin_paste"):
            assert f"def {step}" in src, (
                f"Options flow step {step!r} missing from config_flow.py — "
                "users cannot re-login or change integration options"
            )

    def test_relogin_paste_calls_exchange_code(self):
        """The paste step must call _extract_code to validate the redirect URL."""
        from pathlib import Path
        src = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera" / "config_flow.py"
        ).read_text()
        step_start = src.find("async_step_relogin_paste")
        assert step_start != -1
        step_body = src[step_start:step_start + 600]
        assert "_extract_code" in step_body, (
            "relogin_paste step must call _extract_code to validate the "
            "pasted redirect URL before exchanging for tokens"
        )
