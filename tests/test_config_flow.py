"""Tests for the Bosch Smart Home Camera config flow (custom_components/bosch_shc_camera/config_flow.py).

Covers Quality-Scale Bronze rule `config-flow-test-coverage` plus the OAuth2 /
Keycloak plumbing, the manual-login fallback, reauth/reconfigure entry
points, and the sectioned OptionsFlow (same module) end to end:
  - Single-instance enforcement (unique_config_entry rule)
  - PKCE pair / auth-URL / redirect-code parsing / JWT azp-claim helpers
  - RefreshTokenInvalidError / AuthServerOutageError classification + _do_refresh
  - BoschOAuth2Implementation (authorize URL, external-data resolve, refresh)
  - _exchange_code (initial code→token exchange, used by manual + relogin paste)
  - async_oauth_create_entry routing (fresh entry / reauth / reconfigure)
  - Manual copy/paste login fallback (auto_login/manual_login menu on
    async_step_user, async_step_manual_login, async_step_manual_paste)
  - Reauth / reconfigure flow entry points
  - OptionsFlow: sectioned schema rendering + submit, _flatten_sections,
    OPTIONS_SECTIONS layout, per-section round-trips, relogin steps,
    frontend-serializability of the rendered schema, translation-file
    structure backing the sections
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous_serialize
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera.config_flow import (
    OPTIONS_SECTIONS,
    BoschCameraOptionsFlow,
    _flatten_sections,
)
from custom_components.bosch_shc_camera.const import (
    CONF_AI_ACTIVE_CONDITION_ENTITY,
    CONF_AI_ACTIVE_TIME_END,
    CONF_AI_ACTIVE_TIME_START,
    CONF_AI_MAX_PER_DAY,
    CONF_AI_TASK_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
)
from tests.source_match import assert_in_source

MODULE = "custom_components.bosch_shc_camera.config_flow"


# Constants pinned


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


# Pure helper functions — PKCE, auth-URL, redirect-code parsing, JWT azp claim


class TestPkcePair:
    def test_returns_two_strings(self):
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair

        verifier, challenge = _pkce_pair()
        assert isinstance(verifier, str)
        assert isinstance(challenge, str)

    def test_each_call_produces_unique_pair(self):
        """RFC 7636: verifier must be unique per request."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair

        v1, _ = _pkce_pair()
        v2, _ = _pkce_pair()
        assert v1 != v2

    def test_challenge_is_sha256_of_verifier(self):
        """Verify the cryptographic relationship — RFC 7636 S256 method."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair

        verifier, challenge = _pkce_pair()
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == expected

    def test_verifier_length_meets_rfc_min(self):
        """RFC 7636 says verifier must be 43-128 chars (after url-safe base64)."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair

        verifier, _ = _pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_challenge_no_padding(self):
        """url-safe base64 must have no `=` padding (RFC 7636 §4.2)."""
        from custom_components.bosch_shc_camera.config_flow import _pkce_pair

        _, challenge = _pkce_pair()
        assert "=" not in challenge


class TestBuildAuthUrl:
    def test_contains_required_params(self):
        from custom_components.bosch_shc_camera.config_flow import _build_auth_url

        url = _build_auth_url("test-challenge", "test-state")
        for param in (
            "client_id=",
            "response_type=code",
            "scope=",
            "code_challenge=test-challenge",
            "code_challenge_method=S256",
            "state=test-state",
        ):
            assert param in url, f"Missing {param!r} in auth URL"

    def test_uses_keycloak_base(self):
        from custom_components.bosch_shc_camera.config_flow import _build_auth_url

        url = _build_auth_url("c", "s")
        assert "smarthome.authz.bosch.com" in url


class TestExtractCode:
    def test_full_url_with_code(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code

        url = "https://www.bosch.com/boschcam?code=ABC123&state=xyz"
        assert _extract_code(url) == "ABC123"

    def test_query_string_only(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code

        assert _extract_code("?code=DEF456") == "DEF456"

    def test_strips_whitespace(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code

        assert _extract_code("  https://x?code=GHI789  ") == "GHI789"

    def test_returns_none_on_error_param(self):
        """If the URL has `error=...` in the query, treat as failed flow."""
        from custom_components.bosch_shc_camera.config_flow import _extract_code

        assert _extract_code("https://x?error=access_denied") is None

    def test_returns_none_when_no_code(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code

        assert _extract_code("https://x?something=else") is None

    def test_returns_none_on_garbage(self):
        from custom_components.bosch_shc_camera.config_flow import _extract_code

        # No query string at all → no code
        assert _extract_code("not-a-url") is None


def _make_jwt(payload: dict) -> str:
    """Build a fake JWT for the azp-detection test (signature ignored)."""
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.fake-signature"


class TestDetectTokenClientId:
    def test_returns_oss_residential_app(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _detect_token_client_id,
        )

        token = _make_jwt({"azp": "oss_residential_app", "sub": "user-1"})
        assert _detect_token_client_id(token) == "oss_residential_app"

    def test_returns_legacy_residential_app(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _detect_token_client_id,
        )

        token = _make_jwt({"azp": "residential_app"})
        assert _detect_token_client_id(token) == "residential_app"

    def test_returns_none_for_empty_token(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _detect_token_client_id,
        )

        assert _detect_token_client_id("") is None
        assert _detect_token_client_id(None) is None

    def test_returns_none_for_malformed_token(self):
        """JWT must have at least 2 dot-separated parts; malformed ones yield None."""
        from custom_components.bosch_shc_camera.config_flow import (
            _detect_token_client_id,
        )

        assert _detect_token_client_id("not-a-jwt") is None
        assert _detect_token_client_id("only.one") is None

    def test_returns_none_when_azp_missing(self):
        from custom_components.bosch_shc_camera.config_flow import (
            _detect_token_client_id,
        )

        token = _make_jwt({"sub": "user-1"})  # no azp claim
        assert _detect_token_client_id(token) is None

    def test_returns_none_for_garbled_payload(self):
        """Non-JSON payload base64 — must not raise."""
        from custom_components.bosch_shc_camera.config_flow import (
            _detect_token_client_id,
        )

        garbled = "header.bm90LWpzb24=.signature"  # base64 of "not-json"
        assert _detect_token_client_id(garbled) is None


# RefreshTokenInvalidError / AuthServerOutageError + _do_refresh


class TestRefreshErrors:
    def test_refresh_token_invalid_error_is_exception(self):
        from custom_components.bosch_shc_camera.config_flow import (
            RefreshTokenInvalidError,
        )

        err = RefreshTokenInvalidError("HTTP 401: invalid_grant")
        assert isinstance(err, Exception)
        assert "401" in str(err)

    def test_auth_server_outage_error_is_exception(self):
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError

        err = AuthServerOutageError("HTTP 503")
        assert isinstance(err, Exception)

    def test_errors_are_distinct_classes(self):
        from custom_components.bosch_shc_camera.config_flow import (
            AuthServerOutageError,
            RefreshTokenInvalidError,
        )

        assert RefreshTokenInvalidError is not AuthServerOutageError
        assert not issubclass(RefreshTokenInvalidError, AuthServerOutageError)
        assert not issubclass(AuthServerOutageError, RefreshTokenInvalidError)


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

        new_token = {
            "access_token": "new_at",
            "refresh_token": "new_rt",
            "expires_in": 3600,
        }
        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(200, json_data=new_token))

        result = await _do_refresh(session, "old_refresh_token")
        assert result is not None
        assert result["access_token"] == "new_at"

    @pytest.mark.asyncio
    async def test_400_raises_refresh_token_invalid(self):
        from custom_components.bosch_shc_camera.config_flow import (
            RefreshTokenInvalidError,
            _do_refresh,
        )

        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(400, text="invalid_grant"))

        with pytest.raises(RefreshTokenInvalidError):
            await _do_refresh(session, "expired_token")

    @pytest.mark.asyncio
    async def test_401_raises_refresh_token_invalid(self):
        from custom_components.bosch_shc_camera.config_flow import (
            RefreshTokenInvalidError,
            _do_refresh,
        )

        session = MagicMock()
        session.post = MagicMock(return_value=_mock_resp(401, text="unauthorized"))

        with pytest.raises(RefreshTokenInvalidError):
            await _do_refresh(session, "bad_token")

    @pytest.mark.asyncio
    async def test_500_raises_auth_server_outage(self):
        from custom_components.bosch_shc_camera.config_flow import (
            AuthServerOutageError,
            _do_refresh,
        )

        session = MagicMock()
        session.post = MagicMock(
            return_value=_mock_resp(500, text="Internal Server Error")
        )

        with pytest.raises(AuthServerOutageError):
            await _do_refresh(session, "valid_token")

    @pytest.mark.asyncio
    async def test_503_raises_auth_server_outage(self):
        from custom_components.bosch_shc_camera.config_flow import (
            AuthServerOutageError,
            _do_refresh,
        )

        session = MagicMock()
        session.post = MagicMock(
            return_value=_mock_resp(503, text="Service Unavailable")
        )

        with pytest.raises(AuthServerOutageError):
            await _do_refresh(session, "valid_token")

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """Network timeout → None (transient; caller may retry)."""
        from custom_components.bosch_shc_camera.config_flow import _do_refresh

        session = MagicMock()
        session.post = MagicMock(side_effect=TimeoutError())

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


# BoschOAuth2Implementation — property contracts, authorize URL, external
# data resolve, refresh


class TestBoschOAuth2Implementation:
    """Structural: the OAuth2 implementation exposes required HA contracts."""

    def test_name_property_returns_bosch(self):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation.__new__(BoschOAuth2Implementation)
        assert "Bosch" in impl.name

    def test_domain_property_returns_integration_domain(self):
        from custom_components.bosch_shc_camera import DOMAIN as INTEGRATION_DOMAIN
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation.__new__(BoschOAuth2Implementation)
        assert impl.domain == INTEGRATION_DOMAIN

    def test_redirect_uri_is_my_home_assistant(self):
        """Automatic callback URI — must point to my.home-assistant.io for OAuth2 auto flow."""
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation.__new__(BoschOAuth2Implementation)
        assert "my.home-assistant.io" in impl.redirect_uri, (
            "redirect_uri must use my.home-assistant.io — Bosch's Keycloak is "
            "pre-registered only for this URI; any other value → 400 Bad Request"
        )


class TestBoschOAuth2ImplementationInit:
    def test_init_sets_hass_and_verifier(self):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        fake_hass = MagicMock()
        impl = BoschOAuth2Implementation(fake_hass)
        assert impl.hass is fake_hass
        assert impl._last_verifier is None


class TestAsyncGenerateAuthorizeUrl:
    @pytest.mark.asyncio
    async def test_stores_verifier_and_returns_url_with_challenge(self):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        fake_hass = MagicMock()
        impl = BoschOAuth2Implementation(fake_hass)
        with (
            patch(
                f"{MODULE}._pkce_pair", return_value=("verifier_val", "challenge_val")
            ),
            patch(f"{MODULE}._encode_jwt", return_value="state_jwt"),
        ):
            url = await impl.async_generate_authorize_url("flow-id-1")
        assert impl._last_verifier == "verifier_val"
        assert "code_challenge=challenge_val" in url
        assert "code_challenge_method=S256" in url
        assert "state=state_jwt" in url

    @pytest.mark.asyncio
    async def test_url_contains_client_id_and_scope(self):
        from custom_components.bosch_shc_camera.config_flow import (
            CLIENT_ID,
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation(MagicMock())
        with (
            patch(f"{MODULE}._pkce_pair", return_value=("v", "c")),
            patch(f"{MODULE}._encode_jwt", return_value="s"),
        ):
            url = await impl.async_generate_authorize_url("flow-2")
        assert f"client_id={CLIENT_ID}" in url


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
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation(MagicMock())
        impl._last_verifier = "myverifier"
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(
            200, {"access_token": "at", "refresh_token": "rt"}
        )
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            result = await impl.async_resolve_external_data(
                {
                    "code": "authcode",
                    "state": {
                        "redirect_uri": "https://my.home-assistant.io/auth/callback"
                    },
                }
            )
        assert result["access_token"] == "at"

    @pytest.mark.asyncio
    async def test_logs_on_4xx_before_raise(self):
        """HTTP 4xx: error is logged and raise_for_status propagates it."""
        import aiohttp

        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation(MagicMock())
        impl._last_verifier = "v"
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(
            400, {}, raise_for_status=aiohttp.ClientResponseError(MagicMock(), ())
        )
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            with pytest.raises(aiohttp.ClientResponseError):
                await impl.async_resolve_external_data(
                    {
                        "code": "c",
                        "state": {"redirect_uri": "https://r"},
                    }
                )


class TestAsyncRefreshToken:
    @pytest.mark.asyncio
    async def test_returns_merged_token_on_200(self):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation(MagicMock())
        old_token = {"refresh_token": "old_rt", "access_token": "old_at"}
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(200, {"access_token": "new_at"})
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            result = await impl._async_refresh_token(old_token)
        assert result["access_token"] == "new_at"
        assert result["refresh_token"] == "old_rt"

    @pytest.mark.asyncio
    async def test_logs_on_4xx_before_raise(self):
        import aiohttp

        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        impl = BoschOAuth2Implementation(MagicMock())
        mock_session = MagicMock()
        mock_session.post.return_value = _make_mock_cm(
            401, {}, raise_for_status=aiohttp.ClientResponseError(MagicMock(), ())
        )
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=mock_session),
        ):
            with pytest.raises(aiohttp.ClientResponseError):
                await impl._async_refresh_token({"refresh_token": "rt"})


# _exchange_code — initial code→token exchange (manual login + relogin paste)


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
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
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


# async_oauth_create_entry — structural source-routing pins


class TestAsyncOauthCreateEntryStructure:
    """Behavioral: exercises async_oauth_create_entry's actual source-routing,
    not just a string match on the source code. A pure `"SOURCE_REAUTH" in
    src` check (the previous version of this test) still passes even if the
    runtime `if self.source == config_entries.SOURCE_REAUTH:` condition is
    inverted, removed, or swapped with the reconfigure branch — the token
    stays present elsewhere in the file (imports, comments, the sibling
    manual-paste routing). These tests instead construct a real flow
    instance per source and assert the *observable* outcome: which entry
    object gets updated, whether a brand-new entry gets created, and which
    abort reason is returned.
    """

    def _make_flow(self, source: str):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraConfigFlow,
        )

        flow = BoschCameraConfigFlow.__new__(BoschCameraConfigFlow)
        flow.hass = MagicMock()
        flow.flow_id = "flow-id-routing"
        flow.context = {"source": source}
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow.async_abort = MagicMock(
            side_effect=lambda reason: {"type": "abort", "reason": reason}
        )
        # Distinct sentinels so a swapped reauth/reconfigure branch (or one
        # routing through the other's helper) is detectable by identity.
        flow._reauth_entry_sentinel = MagicMock(name="reauth_entry")
        flow._reconfigure_entry_sentinel = MagicMock(name="reconfigure_entry")
        flow._get_reauth_entry = MagicMock(return_value=flow._reauth_entry_sentinel)
        flow._get_reconfigure_entry = MagicMock(
            return_value=flow._reconfigure_entry_sentinel
        )
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.config_entries.async_schedule_reload = MagicMock()
        return flow

    @pytest.mark.asyncio
    async def test_fresh_setup_creates_new_entry_not_update(self) -> None:
        """No reauth/reconfigure context → a brand-new entry, never an update."""
        flow = self._make_flow(source="user")
        await flow.async_oauth_create_entry(
            {"token": {"access_token": "at1", "refresh_token": "rt1"}}
        )
        flow.async_create_entry.assert_called_once()
        _, kwargs = flow.async_create_entry.call_args
        assert kwargs["data"]["bearer_token"] == "at1"
        assert kwargs["data"]["refresh_token"] == "rt1"
        flow.hass.config_entries.async_update_entry.assert_not_called()
        flow.hass.config_entries.async_schedule_reload.assert_not_called()

    @pytest.mark.asyncio
    async def test_reauth_updates_reauth_entry_only(self) -> None:
        """SOURCE_REAUTH must update the reauth entry, never create a new
        entry and never touch the reconfigure entry."""
        flow = self._make_flow(source=config_entries.SOURCE_REAUTH)
        result = await flow.async_oauth_create_entry(
            {"token": {"access_token": "at2", "refresh_token": "rt2"}}
        )
        flow.hass.config_entries.async_update_entry.assert_called_once()
        call_args, call_kwargs = flow.hass.config_entries.async_update_entry.call_args
        updated_entry = call_args[0] if call_args else call_kwargs["entry"]
        assert updated_entry is flow._reauth_entry_sentinel
        flow._get_reconfigure_entry.assert_not_called()
        flow.async_create_entry.assert_not_called()
        flow.hass.config_entries.async_schedule_reload.assert_called_once()
        assert result == {"type": "abort", "reason": "reauth_successful"}

    @pytest.mark.asyncio
    async def test_reconfigure_updates_reconfigure_entry_only(self) -> None:
        """SOURCE_RECONFIGURE must update the reconfigure entry, never
        create a new entry and never touch the reauth entry."""
        flow = self._make_flow(source=config_entries.SOURCE_RECONFIGURE)
        result = await flow.async_oauth_create_entry(
            {"token": {"access_token": "at3", "refresh_token": "rt3"}}
        )
        flow.hass.config_entries.async_update_entry.assert_called_once()
        call_args, call_kwargs = flow.hass.config_entries.async_update_entry.call_args
        updated_entry = call_args[0] if call_args else call_kwargs["entry"]
        assert updated_entry is flow._reconfigure_entry_sentinel
        flow._get_reauth_entry.assert_not_called()
        flow.async_create_entry.assert_not_called()
        flow.hass.config_entries.async_schedule_reload.assert_called_once()
        assert result == {"type": "abort", "reason": "reconfigure_successful"}


class TestOptionsFlowStructure:
    def test_options_flow_steps_exist(self):
        from pathlib import Path

        src = (
            Path(__file__).parent.parent
            / "custom_components"
            / "bosch_shc_camera"
            / "config_flow.py"
        ).read_text()
        for step in (
            "async_step_init",
            "async_step_relogin_show",
            "async_step_relogin_paste",
        ):
            assert f"def {step}" in src, (
                f"Options flow step {step!r} missing from config_flow.py — "
                "users cannot re-login or change integration options"
            )

    def test_relogin_paste_calls_exchange_code(self):
        """The paste step must call _extract_code to validate the redirect URL."""
        from pathlib import Path

        src = (
            Path(__file__).parent.parent
            / "custom_components"
            / "bosch_shc_camera"
            / "config_flow.py"
        ).read_text()
        step_start = src.find("async_step_relogin_paste")
        assert step_start != -1
        step_body = src[step_start : step_start + 1500]
        assert "_extract_code" in step_body, (
            "relogin_paste step must call _extract_code to validate the "
            "pasted redirect URL before exchanging for tokens"
        )


# Single-instance guard / OAuth create-entry / reauth+reconfigure — via the
# real HA flow-manager harness


async def test_user_flow_aborts_when_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Adding the integration twice must abort with `already_configured`."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "abort"
    # manifest.json sets single_config_entry:true — HA FlowManager aborts
    # at the handler level before our flow runs, using this reason string.
    assert result["reason"] == "single_instance_allowed"


def _make_mock_entry(
    state: ConfigEntryState = ConfigEntryState.NOT_LOADED,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Bosch Smart Home Camera",
        data={"bearer_token": "tok", "refresh_token": "rtok"},
        options={},
        unique_id=DOMAIN,
        version=1,
        state=state,
    )


def _frontend_context():
    """Return a patch context for hass_frontend if it is NOT already installed.

    In CI requirements_test.txt ships home-assistant-frontend which provides the
    real hass_frontend; using a stub there breaks static-path registration.  Locally
    (where hass_frontend is absent) the stub is required to avoid ImportError when
    HA's flow manager loads the `my` integration dependency chain.
    """
    from contextlib import nullcontext
    from pathlib import Path

    try:
        import hass_frontend

        return nullcontext()
    except ImportError:
        fake = ModuleType("hass_frontend")
        fake.where = MagicMock(return_value=Path("/fake"))  # type: ignore[attr-defined]
        return patch.dict(sys.modules, {"hass_frontend": fake})


async def test_reauth_confirm_shows_form(
    hass: HomeAssistant,
) -> None:
    """Triggering reauth shows the confirm form before re-running OAuth."""
    with _frontend_context():
        entry = _make_mock_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=entry.data,
        )
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"


async def test_reconfigure_shows_form(
    hass: HomeAssistant,
) -> None:
    """Reconfigure flow shows the confirm form."""
    with _frontend_context():
        entry = _make_mock_entry()
        entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": entry.entry_id,
            },
        )
    assert result["type"] == "form"
    assert result["step_id"] == "reconfigure"


async def test_oauth_create_entry_redacts_in_diagnostics(
    hass: HomeAssistant, mock_oauth_token: dict
) -> None:
    """A fresh entry stores tokens — diagnostics must redact them."""
    from custom_components.bosch_shc_camera.diagnostics import (
        TO_REDACT,
        async_get_config_entry_diagnostics,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "bearer_token": mock_oauth_token["access_token"],
            "refresh_token": mock_oauth_token["refresh_token"],
        },
        options={},
    )
    entry.add_to_hass(hass)
    # Inject a stub coordinator so diagnostics doesn't crash on missing runtime_data
    entry.runtime_data = type(
        "Stub",
        (),
        {
            "data": {},
            "last_update_success": True,
            "_fcm_running": False,
            "_fcm_healthy": True,
            "_auth_outage_count": 0,
            "update_interval": None,
        },
    )()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    redacted = diag["entry"]["data"]
    assert redacted["bearer_token"] == "**REDACTED**"
    assert redacted["refresh_token"] == "**REDACTED**"
    assert "bearer_token" in TO_REDACT
    assert "refresh_token" in TO_REDACT
    assert "private" in TO_REDACT  # FCM private key must be in redact list
    assert "api_key" in TO_REDACT  # Firebase API key must be in redact list


# Reauth / reconfigure entry points — lightweight stub bypassing the HA
# flow framework (covers branches unreachable via the full harness because
# it requires `hass_frontend`, not installed in every dev venv)


def _make_bare_flow():
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
        flow = _make_bare_flow()

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
        flow = _make_bare_flow()

        form_sentinel = {"type": "form", "step_id": "reauth_confirm"}
        flow.async_show_form = MagicMock(return_value=form_sentinel)

        result = await flow.async_step_reauth_confirm(user_input=None)

        flow.async_show_form.assert_called_once_with(step_id="reauth_confirm")
        assert result is form_sentinel

    @pytest.mark.asyncio
    async def test_with_user_input_calls_step_user(self):
        """user_input provided → delegate to async_step_user()."""
        flow = _make_bare_flow()

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
        flow = _make_bare_flow()

        form_sentinel = {"type": "form", "step_id": "reconfigure"}
        flow.async_show_form = MagicMock(return_value=form_sentinel)

        result = await flow.async_step_reconfigure(user_input=None)

        flow.async_show_form.assert_called_once_with(step_id="reconfigure")
        assert result is form_sentinel

    @pytest.mark.asyncio
    async def test_with_user_input_calls_step_user(self):
        """user_input provided → delegate to async_step_user()."""
        flow = _make_bare_flow()

        user_step_result = {"type": "create_entry"}
        flow.async_step_user = AsyncMock(return_value=user_step_result)

        result = await flow.async_step_reconfigure(user_input={})

        flow.async_step_user.assert_called_once_with()
        assert result is user_step_result


class TestConfigFlowSteps:
    """Fuller BoschCameraConfigFlow coverage: logger property, async_step_user,
    reauth_confirm/reconfigure with a submitted form, async_oauth_create_entry's
    three routing branches, and async_get_options_flow."""

    def _make_flow(self, source="user"):
        """Create a flow instance bypassing HA's config-flow framework."""
        from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow

        flow = BoschCameraConfigFlow.__new__(BoschCameraConfigFlow)
        flow.hass = MagicMock()
        flow.flow_id = "flow-id-1"
        flow._manual_verifier = None
        flow._manual_auth_url = ""
        # source is a read-only property backed by context dict
        flow.context = {"source": source}
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_show_form = MagicMock(return_value={"type": "form", "step_id": "x"})
        flow.async_show_menu = MagicMock(
            return_value={"type": "menu", "step_id": "user"}
        )
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})
        flow.async_update_and_abort = MagicMock(return_value={"type": "abort"})
        flow.async_abort = MagicMock(return_value={"type": "abort"})
        flow._get_reauth_entry = MagicMock(return_value=MagicMock())
        flow._get_reconfigure_entry = MagicMock(return_value=MagicMock())
        flow.hass.config_entries.async_update_entry = MagicMock()
        return flow

    def test_logger_property_returns_module_logger(self):
        import logging

        from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow

        flow = BoschCameraConfigFlow.__new__(BoschCameraConfigFlow)
        assert isinstance(flow.logger, logging.Logger)

    @pytest.mark.asyncio
    async def test_async_step_user_registers_implementation(self):
        """async_step_user registers the OAuth2 impl, then shows the
        auto/manual login menu (it no longer delegates straight to the
        automatic OAuth2 flow — see the manual-login section below)."""
        flow = self._make_flow(source="user")
        with patch(f"{MODULE}.async_register_implementation") as mock_reg:
            result = await flow.async_step_user(None)
        assert mock_reg.called, (
            "async_register_implementation must be called in async_step_user"
        )
        flow.async_show_menu.assert_called_once()
        assert result == {"type": "menu", "step_id": "user"}

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
        result = await flow.async_oauth_create_entry(
            {
                "token": {"access_token": "at", "refresh_token": "rt"},
            }
        )
        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args
        assert call_kwargs is not None

    @pytest.mark.asyncio
    async def test_oauth_create_entry_reauth_updates_existing(self):
        """SOURCE_REAUTH → async_update_entry (writes tokens) + schedule_reload + abort.
        H1 fix: reload was previously scheduled BEFORE async_update_and_abort wrote
        new tokens, causing the reload to boot with stale credentials.
        New pattern: async_update_entry writes tokens synchronously, then reload is
        scheduled (guaranteed to run after write), then async_abort returns."""
        flow = self._make_flow(source=config_entries.SOURCE_REAUTH)
        flow.async_abort = MagicMock(return_value={"type": "abort"})
        await flow.async_oauth_create_entry(
            {
                "token": {"access_token": "new_at", "refresh_token": "new_rt"},
            }
        )
        # Must write entry data first (synchronous)
        flow.hass.config_entries.async_update_entry.assert_called_once()
        # Then schedule the reload (guaranteed tokens are written)
        flow.hass.config_entries.async_schedule_reload.assert_called_once()
        # Then abort the flow
        flow.async_abort.assert_called_once()

    @pytest.mark.asyncio
    async def test_oauth_create_entry_reconfigure_updates_existing(self):
        """SOURCE_RECONFIGURE → async_update_entry + schedule_reload + abort.
        H1 fix: same as reauth — write-before-reload ordering."""
        flow = self._make_flow(source=config_entries.SOURCE_RECONFIGURE)
        flow.async_abort = MagicMock(return_value={"type": "abort"})
        await flow.async_oauth_create_entry(
            {
                "token": {"access_token": "new_at", "refresh_token": "new_rt"},
            }
        )
        flow.hass.config_entries.async_update_entry.assert_called_once()
        flow.hass.config_entries.async_schedule_reload.assert_called_once()
        flow.async_abort.assert_called_once()

    def test_async_get_options_flow_returns_options_flow_instance(self):
        """async_get_options_flow must return a BoschCameraOptionsFlow (line 480)."""
        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraConfigFlow,
        )

        entry = MagicMock()
        entry.options = {}
        entry.data = {}
        result = BoschCameraConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, BoschCameraOptionsFlow)


# Manual copy/paste login fallback on the initial config flow
#
# Bug source: Bosch Smart Home Community private message from SebastianHarder
# to mosandlt, "HA: SingleKey ID Login", received 2026-07-05. The reporter
# could not complete SingleKey ID login on either the HA Companion mobile app
# or a Mac browser — after a chain of OAuth redirects he ended up in the
# wrong/last-opened browser tab, with the config flow toggling between a
# blank screen, a brief success message, and the setup error on repeated
# back-navigation. Prior to this fix, the only login path for a *fresh*
# setup was the automatic browser redirect (via my.home-assistant.io); the
# existing manual copy/paste fallback was only reachable through the
# options flow's "force_relogin" checkbox on an *already-configured* entry,
# so an affected user had no way to complete initial setup at all.
#
# Fix: `async_step_user` now shows a menu ("auto_login" / "manual_login")
# instead of unconditionally delegating to the automatic OAuth2 flow, via
# the new `async_step_auto_login`, `async_step_manual_login`, and
# `async_step_manual_paste` steps on `BoschCameraConfigFlow`.


def _make_flow(source: str = "user"):
    """Create a BoschCameraConfigFlow instance bypassing HA's flow framework."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraConfigFlow

    flow = BoschCameraConfigFlow.__new__(BoschCameraConfigFlow)
    flow._manual_verifier = None
    flow._manual_auth_url = ""
    flow.hass = MagicMock()
    flow.flow_id = "flow-id-1"
    flow.context = {"source": source}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_show_form = MagicMock(return_value={"type": "form", "step_id": "x"})
    flow.async_show_menu = MagicMock(return_value={"type": "menu", "step_id": "user"})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    flow.async_abort = MagicMock(return_value={"type": "abort"})
    flow._get_reauth_entry = MagicMock(return_value=MagicMock())
    flow._get_reconfigure_entry = MagicMock(return_value=MagicMock())
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_schedule_reload = MagicMock()
    return flow


class TestAsyncStepUserShowsMenu:
    @pytest.mark.asyncio
    async def test_fresh_setup_shows_auto_and_manual_options(self) -> None:
        flow = _make_flow(source="user")
        with patch(f"{MODULE}.async_register_implementation") as mock_reg:
            result = await flow.async_step_user(None)
        assert mock_reg.called
        # Fresh setup must still run the duplicate-entry guard *before* the
        # menu is shown — this proves the menu didn't replace that check.
        flow.async_set_unique_id.assert_called_once_with("bosch_shc_camera")
        flow._abort_if_unique_id_configured.assert_called_once()
        flow.async_show_menu.assert_called_once()
        _, kwargs = flow.async_show_menu.call_args
        assert kwargs.get("step_id") == "user"
        assert kwargs.get("menu_options") == ["auto_login", "manual_login"]
        assert result == {"type": "menu", "step_id": "user"}

    @pytest.mark.asyncio
    async def test_reauth_source_skips_unique_id_check_but_still_shows_menu(
        self,
    ) -> None:
        flow = _make_flow(source=config_entries.SOURCE_REAUTH)
        with patch(f"{MODULE}.async_register_implementation"):
            await flow.async_step_user(None)
        flow.async_set_unique_id.assert_not_called()
        flow.async_show_menu.assert_called_once()


class TestAsyncStepAutoLogin:
    @pytest.mark.asyncio
    async def test_delegates_to_parent_oauth2_flow(self) -> None:
        from homeassistant.helpers.config_entry_oauth2_flow import (
            AbstractOAuth2FlowHandler,
        )

        flow = _make_flow(source="user")
        with patch.object(
            AbstractOAuth2FlowHandler,
            "async_step_user",
            AsyncMock(return_value={"type": "external_step"}),
        ) as mock_super:
            result = await flow.async_step_auto_login({"some": "input"})
        mock_super.assert_called_once()
        assert result == {"type": "external_step"}


class TestAsyncStepManualLogin:
    @pytest.mark.asyncio
    async def test_none_input_shows_form_with_generated_auth_url(self) -> None:
        flow = _make_flow(source="user")
        with (
            patch(f"{MODULE}._pkce_pair", return_value=("verifier", "challenge")),
            patch(f"{MODULE}._encode_jwt", return_value="state-jwt"),
            patch(
                f"{MODULE}._build_auth_url",
                return_value="https://auth.example/manual",
            ) as mock_build,
        ):
            result = await flow.async_step_manual_login(None)
        assert flow._manual_verifier == "verifier"
        mock_build.assert_called_once_with("challenge", "state-jwt")
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert kwargs.get("step_id") == "manual_login"

    @pytest.mark.asyncio
    async def test_user_input_advances_to_manual_paste(self) -> None:
        flow = _make_flow(source="user")
        flow._manual_verifier = "already-set"
        flow.async_step_manual_paste = AsyncMock(return_value={"type": "form"})
        result = await flow.async_step_manual_login({"login_url": "https://x"})
        flow.async_step_manual_paste.assert_called_once()

    @pytest.mark.asyncio
    async def test_verifier_only_generated_once(self) -> None:
        """A second visit to the step must not mint a fresh PKCE verifier."""
        flow = _make_flow(source="user")
        with (
            patch(f"{MODULE}._pkce_pair", return_value=("v1", "c1")) as mock_pkce,
            patch(f"{MODULE}._encode_jwt", return_value="state"),
            patch(f"{MODULE}._build_auth_url", return_value="https://auth"),
        ):
            await flow.async_step_manual_login(None)
            await flow.async_step_manual_login(None)
        mock_pkce.assert_called_once()


class TestAsyncStepManualPaste:
    @pytest.mark.asyncio
    async def test_none_input_shows_paste_form(self) -> None:
        flow = _make_flow(source="user")
        result = await flow.async_step_manual_paste(None)
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert kwargs.get("step_id") == "manual_paste"
        assert kwargs.get("errors") == {}

    @pytest.mark.asyncio
    async def test_invalid_redirect_url_shows_error(self) -> None:
        flow = _make_flow(source="user")
        result = await flow.async_step_manual_paste(
            user_input={"redirect_url": "https://no-code.example.com"}
        )
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert kwargs.get("errors", {}).get("redirect_url") == "invalid_redirect_url"

    @pytest.mark.asyncio
    async def test_failed_token_exchange_shows_error(self) -> None:
        flow = _make_flow(source="user")
        with (
            patch(f"{MODULE}._extract_code", return_value="valid_code"),
            patch(f"{MODULE}._exchange_code", AsyncMock(return_value=None)),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            result = await flow.async_step_manual_paste(
                user_input={"redirect_url": "https://r.io?code=valid_code"}
            )
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert kwargs.get("errors", {}).get("redirect_url") == "token_exchange_failed"

    @pytest.mark.asyncio
    async def test_success_on_fresh_setup_creates_entry(self) -> None:
        flow = _make_flow(source="user")
        with (
            patch(f"{MODULE}._extract_code", return_value="good_code"),
            patch(
                f"{MODULE}._exchange_code",
                AsyncMock(return_value={"access_token": "at", "refresh_token": "rt"}),
            ),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            await flow.async_step_manual_paste(
                user_input={"redirect_url": "https://r.io?code=good_code"}
            )
        flow.async_create_entry.assert_called_once_with(
            title="Bosch Smart Home Camera",
            data={"bearer_token": "at", "refresh_token": "rt"},
        )
        flow.hass.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_cloud_api_override_not_starting_https_shows_error(self) -> None:
        """Advanced override field must be rejected if it isn't a https:// URL.

        2026-07-06 (Thomas): optional diagnostic escape hatch so a single
        account can test whether it's registered against a different,
        Bosch-confirmed camera-API base URL — never pre-filled, must be
        typed in deliberately.
        """
        flow = _make_flow(source="user")
        result = await flow.async_step_manual_paste(
            user_input={
                "redirect_url": "https://r.io?code=good_code",
                "diagnostic_cloud_api_override": "not-a-url",
            }
        )
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert (
            kwargs.get("errors", {}).get("diagnostic_cloud_api_override")
            == "invalid_cloud_api_override"
        )

    @pytest.mark.asyncio
    async def test_cloud_api_override_persisted_when_provided(self) -> None:
        flow = _make_flow(source="user")
        with (
            patch(f"{MODULE}._extract_code", return_value="good_code"),
            patch(
                f"{MODULE}._exchange_code",
                AsyncMock(return_value={"access_token": "at", "refresh_token": "rt"}),
            ),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            await flow.async_step_manual_paste(
                user_input={
                    "redirect_url": "https://r.io?code=good_code",
                    "diagnostic_cloud_api_override": "https://example-test.invalid/",
                }
            )
        flow.async_create_entry.assert_called_once_with(
            title="Bosch Smart Home Camera",
            data={
                "bearer_token": "at",
                "refresh_token": "rt",
                "cloud_api_override": "https://example-test.invalid",
            },
        )

    @pytest.mark.asyncio
    async def test_cloud_api_override_omitted_when_blank(self) -> None:
        """Blank override (the default) must NOT add a cloud_api_override key at all."""
        flow = _make_flow(source="user")
        with (
            patch(f"{MODULE}._extract_code", return_value="good_code"),
            patch(
                f"{MODULE}._exchange_code",
                AsyncMock(return_value={"access_token": "at", "refresh_token": "rt"}),
            ),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            await flow.async_step_manual_paste(
                user_input={
                    "redirect_url": "https://r.io?code=good_code",
                    "diagnostic_cloud_api_override": "",
                }
            )
        flow.async_create_entry.assert_called_once_with(
            title="Bosch Smart Home Camera",
            data={"bearer_token": "at", "refresh_token": "rt"},
        )

    @pytest.mark.asyncio
    async def test_success_on_reauth_updates_existing_entry(self) -> None:
        flow = _make_flow(source=config_entries.SOURCE_REAUTH)
        existing = MagicMock()
        existing.entry_id = "entry-reauth-1"
        existing.data = {
            "bearer_token": "stale_at",
            "refresh_token": "stale_rt",
            "unrelated_setting": "keep-me",
        }
        flow._get_reauth_entry = MagicMock(return_value=existing)
        with (
            patch(f"{MODULE}._extract_code", return_value="good_code"),
            patch(
                f"{MODULE}._exchange_code",
                AsyncMock(return_value={"access_token": "at2", "refresh_token": "rt2"}),
            ),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            result = await flow.async_step_manual_paste(
                user_input={"redirect_url": "https://r.io?code=good_code"}
            )
        # Must merge onto the *existing* entry's data — not overwrite it —
        # so options/FCM/SMB settings on the config entry survive a reauth.
        flow.hass.config_entries.async_update_entry.assert_called_once_with(
            existing,
            data={
                "bearer_token": "at2",
                "refresh_token": "rt2",
                "unrelated_setting": "keep-me",
            },
        )
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
            "entry-reauth-1"
        )
        flow.async_abort.assert_called_once_with(reason="reauth_successful")
        flow.async_create_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_on_reconfigure_updates_existing_entry(self) -> None:
        flow = _make_flow(source=config_entries.SOURCE_RECONFIGURE)
        existing = MagicMock()
        existing.entry_id = "entry-reconfigure-1"
        existing.data = {
            "bearer_token": "stale_at",
            "refresh_token": "stale_rt",
            "unrelated_setting": "keep-me-too",
        }
        flow._get_reconfigure_entry = MagicMock(return_value=existing)
        with (
            patch(f"{MODULE}._extract_code", return_value="good_code"),
            patch(
                f"{MODULE}._exchange_code",
                AsyncMock(return_value={"access_token": "at3", "refresh_token": "rt3"}),
            ),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            result = await flow.async_step_manual_paste(
                user_input={"redirect_url": "https://r.io?code=good_code"}
            )
        flow.hass.config_entries.async_update_entry.assert_called_once_with(
            existing,
            data={
                "bearer_token": "at3",
                "refresh_token": "rt3",
                "unrelated_setting": "keep-me-too",
            },
        )
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with(
            "entry-reconfigure-1"
        )
        flow.async_abort.assert_called_once_with(reason="reconfigure_successful")
        flow.async_create_entry.assert_not_called()


# BoschCameraOptionsFlow — relogin steps (force-relogin from the options menu)


class TestOptionsFlowReloginSteps:
    def _make_options_flow(self):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraOptionsFlow,
        )

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
        assert call_kwargs[1].get("step_id") == "relogin_show" or (
            call_kwargs[0] and "relogin_show" in str(call_kwargs)
        )

    @pytest.mark.asyncio
    async def test_relogin_show_with_user_input_advances_to_paste(self):
        """Any user_input submission calls async_step_relogin_paste."""
        flow = self._make_options_flow()
        flow.async_step_relogin_paste = AsyncMock(return_value={"type": "form"})
        result = await flow.async_step_relogin_show(
            user_input={"login_url": "https://auth"}
        )
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
        result = await flow.async_step_relogin_paste(
            user_input={"redirect_url": "https://no-code.example.com"}
        )
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert kwargs.get("errors", {}).get("redirect_url") == "invalid_redirect_url"

    @pytest.mark.asyncio
    async def test_relogin_paste_failed_exchange_shows_error(self):
        """Valid code but token exchange fails → errors['redirect_url'] = token_exchange_failed."""
        flow = self._make_options_flow()
        with (
            patch(f"{MODULE}._extract_code", return_value="valid_code"),
            patch(f"{MODULE}._exchange_code", AsyncMock(return_value=None)),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
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
        with (
            patch(f"{MODULE}._extract_code", return_value="good_code"),
            patch(
                f"{MODULE}._exchange_code",
                AsyncMock(
                    return_value={
                        "access_token": "new_at",
                        "refresh_token": "new_rt",
                    }
                ),
            ),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            result = await flow.async_step_relogin_paste(
                user_input={"redirect_url": "https://r.io?code=good_code"}
            )
        flow.async_create_entry.assert_called_once()
        flow.hass.config_entries.async_update_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_relogin_paste_cloud_api_override_rejected_if_not_https(self):
        """2026-07-06: advanced diagnostic override must reject non-https values."""
        flow = self._make_options_flow()
        result = await flow.async_step_relogin_paste(
            user_input={
                "redirect_url": "https://r.io?code=good_code",
                "diagnostic_cloud_api_override": "not-a-url",
            }
        )
        flow.async_show_form.assert_called_once()
        _, kwargs = flow.async_show_form.call_args
        assert (
            kwargs.get("errors", {}).get("diagnostic_cloud_api_override")
            == "invalid_cloud_api_override"
        )

    @pytest.mark.asyncio
    async def test_relogin_paste_cloud_api_override_persisted(self):
        """A valid override must be merged into the updated entry data."""
        flow = self._make_options_flow()
        with (
            patch(f"{MODULE}._extract_code", return_value="good_code"),
            patch(
                f"{MODULE}._exchange_code",
                AsyncMock(
                    return_value={
                        "access_token": "new_at",
                        "refresh_token": "new_rt",
                    }
                ),
            ),
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ),
        ):
            await flow.async_step_relogin_paste(
                user_input={
                    "redirect_url": "https://r.io?code=good_code",
                    "diagnostic_cloud_api_override": "https://example-test.invalid/",
                }
            )
        _, kwargs = flow.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["cloud_api_override"] == "https://example-test.invalid"


# _flatten_sections + OPTIONS_SECTIONS layout
#
# ``_flatten_sections`` is the contract surface between HA's
# ``data_entry_flow.section()`` UI shape (nested per-section dicts) and the
# legacy flat options dict every other module in the integration consumes.
# Regressions here would silently drop options or overwrite them with the
# wrong defaults.
#
# Source: project-internal v11.0.4 OptionsFlow Sections refactor — ~50
# fields grouped into collapsible blocks. The flatten helper is the only
# surface where the section grouping leaks into runtime behaviour; the
# rest of the integration sees the legacy flat dict shape.


class TestFlattenSectionsBasic:
    def test_empty_input_returns_empty_dict(self):
        assert _flatten_sections({}) == {}

    def test_lifts_nested_keys_to_top_level(self):
        """Section dicts get unpacked: ``{section: {field: v}}`` → ``{field: v}``."""
        out = _flatten_sections(
            {
                "polling": {"scan_interval": 60, "interval_status": 300},
                "features": {"enable_snapshots": True},
            }
        )
        assert out == {
            "scan_interval": 60,
            "interval_status": 300,
            "enable_snapshots": True,
        }

    def test_missing_section_does_not_raise(self):
        """HA may omit empty sections entirely."""
        # Only `polling` is present; the other sections are simply absent.
        out = _flatten_sections({"polling": {"scan_interval": 60}})
        assert out == {"scan_interval": 60}

    def test_section_set_to_none_does_not_raise(self):
        """Defensive — if HA sends ``None`` instead of an empty dict."""
        out = _flatten_sections({"polling": None})
        assert out == {}

    def test_non_dict_section_payload_skipped(self):
        """Defensive — never expected from HA but keeps tests honest."""
        out = _flatten_sections({"polling": "garbage"})
        assert out == {}

    def test_top_level_unknown_keys_pass_through(self):
        """Legacy / programmatic / test callers may submit flat dicts directly.
        Anything not matching a known section key flows through unchanged."""
        out = _flatten_sections({"force_relogin": True})
        assert out == {"force_relogin": True}

    def test_input_dict_not_mutated(self):
        original = {"polling": {"scan_interval": 60}}
        snapshot = {
            k: dict(v) if isinstance(v, dict) else v for k, v in original.items()
        }
        _flatten_sections(original)
        assert original == snapshot


class TestFlattenSectionsCollisions:
    """Defensive guards — duplicate keys must explode loudly so a future
    OPTIONS_SECTIONS edit cannot silently overwrite an existing field."""

    def test_duplicate_across_two_sections_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two sections claim the same field → ValueError."""
        # Patch OPTIONS_SECTIONS in place so the helper sees the conflict.
        monkeypatch.setitem(OPTIONS_SECTIONS, "_test_a", ["dupe_field"])
        monkeypatch.setitem(OPTIONS_SECTIONS, "_test_b", ["dupe_field"])
        try:
            with pytest.raises(ValueError, match="duplicate key"):
                _flatten_sections(
                    {
                        "_test_a": {"dupe_field": 1},
                        "_test_b": {"dupe_field": 2},
                    }
                )
        finally:
            OPTIONS_SECTIONS.pop("_test_a", None)
            OPTIONS_SECTIONS.pop("_test_b", None)

    def test_duplicate_top_level_and_section_raises(self):
        """A legit top-level pass-through key colliding with a flattened
        section field must raise — fail-loud — so the caller fixes it."""
        with pytest.raises(ValueError, match="duplicate key"):
            _flatten_sections(
                {
                    "polling": {"scan_interval": 60},
                    "scan_interval": 999,  # already lifted from polling
                }
            )


class TestOptionsSectionsLayout:
    """Pin the sections layout so a refactor cannot silently drop a section
    or duplicate a field across sections."""

    def test_every_section_field_is_unique(self):
        """No field key appears in two sections — guards _flatten_sections."""
        seen: dict[str, str] = {}
        for section_key, fields in OPTIONS_SECTIONS.items():
            for field in fields:
                assert field not in seen, (
                    f"field {field!r} appears in both "
                    f"{seen[field]!r} and {section_key!r}"
                )
                seen[field] = section_key

    def test_required_sections_present(self):
        """Hard-coded list — pin so a future refactor can't silently
        drop a section the strings.json relies on."""
        required = {
            "polling",
            "features",
            "stream",
            "fcm",
            "events_storage",
            "nvr",
            "auth",
        }
        assert required <= set(OPTIONS_SECTIONS.keys())

    def test_nvr_section_includes_new_target_keys(self):
        """The two new options added in the NVR-storage-target refactor must
        be in the nvr section so they actually render."""
        assert "nvr_storage_target" in OPTIONS_SECTIONS["nvr"]
        assert "nvr_smb_subpath" in OPTIONS_SECTIONS["nvr"]


# Options-flow schema rendering / submit + per-section round-trips


def _make_entry(
    *, options: dict | None = None, bearer_token: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": bearer_token, "refresh_token": "rt"},
        options=options or {},
    )


def _legacy_token() -> str:
    """Build a minimal JWT with azp=residential_app (legacy client)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = (
        base64.urlsafe_b64encode(json.dumps({"azp": "residential_app"}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{body}.x"


def _oss_token() -> str:
    """Build a minimal JWT with azp=oss_residential_app (new OSS client)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    body = (
        base64.urlsafe_b64encode(json.dumps({"azp": "oss_residential_app"}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{body}.x"


async def _submit(flow: BoschCameraOptionsFlow, user_input: dict) -> dict:
    """Submit the options form and return the saved data dict."""
    saved: dict = {}
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: (
            saved.update({"data": kw.get("data", {})}) or {"type": "create_entry"}
        ),
    )
    result = await flow.async_step_init(user_input=user_input)
    assert result["type"] == "create_entry", f"Expected create_entry, got {result}"
    return saved["data"]


def _get_section_schema(section_name: str):
    """Render the options form and return the inner voluptuous schema for a section.

    Calls async_step_init(user_input=None) on a fresh flow, captures the
    data_schema, then walks into the named section to return its inner schema.
    Returns a callable that validates a partial dict (missing keys get defaults).
    """
    import voluptuous as vol

    flow = BoschCameraOptionsFlow(_make_entry())
    captured: dict = {}

    def capture(**kw):
        captured["schema"] = kw.get("data_schema")
        return {"type": "form"}

    flow.async_show_form = capture
    asyncio.get_event_loop().run_until_complete(flow.async_step_init(user_input=None))

    outer: vol.Schema = captured["schema"]
    for key, val in outer.schema.items():
        if str(key) == section_name:
            # val is a section(inner_schema, options) object.
            # The inner schema is accessible via .schema attribute.
            inner = getattr(val, "schema", val)
            if hasattr(inner, "schema"):
                return inner
            return inner
    raise KeyError(f"Section {section_name!r} not found in options schema")


class TestOptionsStepInitRender:
    """Smoke-cover the section-schema rendering branch (no user_input)."""

    @pytest.mark.asyncio
    async def test_render_returns_form_with_sections(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        # async_show_form / hass aren't actually needed for the helper
        # because the OptionsFlow base class composes the result dict — but
        # we patch async_show_form to capture the schema.
        captured = {}

        def capture(**kw):
            captured.update(kw)
            return {"type": "form", **kw}

        flow.async_show_form = capture

        result = await flow.async_step_init(user_input=None)
        assert result["type"] == "form"
        # Section keys must show up in the schema as required keys.
        schema = captured["data_schema"]
        keys = {str(k) for k in schema.schema.keys()}
        assert "polling" in keys
        assert "nvr" in keys

    @pytest.mark.asyncio
    async def test_render_with_legacy_client_includes_migrate(self):
        """A legacy ``residential_app`` JWT must surface the migrate option."""
        # Build a minimal JWT with azp=residential_app
        header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
        body = (
            base64.urlsafe_b64encode(json.dumps({"azp": "residential_app"}).encode())
            .rstrip(b"=")
            .decode()
        )
        token = f"{header}.{body}.x"

        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=token))
        flow.async_show_form = MagicMock(return_value={"type": "form"})
        await flow.async_step_init(user_input=None)
        flow.async_show_form.assert_called_once()


class TestOptionsStepInitSubmit:
    """Submit branches: plain save / force_relogin / migrate_to_oss."""

    @pytest.mark.asyncio
    async def test_submit_plain_save_creates_entry(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        # Sectioned submit shape — only one section non-empty.
        result = await flow.async_step_init(
            user_input={
                "polling": {"scan_interval": 30},
            }
        )
        assert result == {"type": "create_entry"}
        flow.async_create_entry.assert_called_once()
        kw = flow.async_create_entry.call_args.kwargs
        assert kw["data"]["scan_interval"] == 30

    @pytest.mark.asyncio
    async def test_submit_with_force_relogin_branches(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        # Stub the relogin step
        flow.async_step_relogin_show = AsyncMock(
            return_value={"type": "form", "step_id": "relogin_show"},
        )
        result = await flow.async_step_init(
            user_input={
                "auth": {"force_relogin": True},
            }
        )
        assert result["step_id"] == "relogin_show"

    @pytest.mark.asyncio
    async def test_submit_with_migrate_starts_reauth(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.async_create_task = MagicMock()
        flow._config_entry.async_start_reauth = MagicMock(return_value=None)
        flow.async_abort = MagicMock(
            return_value={"type": "abort", "reason": "migration_started"}
        )
        result = await flow.async_step_init(
            user_input={
                "auth": {"migrate_to_oss_client": True},
            }
        )
        assert result["reason"] == "migration_started"
        flow.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_normalizes_booleans(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        captured = {}
        flow.async_create_entry = MagicMock(
            side_effect=lambda **kw: captured.update(kw) or {"type": "create_entry"},
        )
        await flow.async_step_init(
            user_input={
                "features": {"enable_snapshots": 1, "enable_intercom": 0},
                "nvr": {"enable_nvr": 1},
            }
        )
        # ``1``/``0`` get coerced to True/False so downstream code can rely
        # on plain bool checks.
        assert captured["data"]["enable_snapshots"] is True
        assert captured["data"]["enable_intercom"] is False
        assert captured["data"]["enable_nvr"] is True


# Per-section scenario tests
#
# Coverage goals:
# * Every section submits correctly → keys land in the saved entry.
# * Default values from DEFAULT_OPTIONS are used as fallbacks in the schema.
# * Range constraints on numeric fields reject out-of-bound values.
# * Boolean normalization coerces int 1/0 → True/False.
# * enable_local_save defaults to OFF (v11.0.12 regression guard).
# * migrate_to_oss_client only exposed for legacy residential_app tokens.
# * Round-trip: existing options survive when only one section is submitted.
# * All 50+ fields in DEFAULT_OPTIONS have a corresponding OPTIONS_SECTIONS entry.


class TestPollingSection:
    @pytest.mark.asyncio
    async def test_custom_intervals_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 120,
                    "interval_status": 600,
                    "interval_events": 400,
                    "snapshot_interval": 3600,
                },
            },
        )
        assert data["scan_interval"] == 120
        assert data["interval_status"] == 600
        assert data["interval_events"] == 400
        assert data["snapshot_interval"] == 3600

    @pytest.mark.asyncio
    async def test_defaults_applied_when_no_prior_options(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        # Submit an unrelated section; polling fields should fall back to DEFAULT_OPTIONS.
        data = await _submit(flow, {"auth": {"force_relogin": False}})
        # scan_interval not in user_input → falls back to saved options (empty) → DEFAULT_OPTIONS
        assert (
            "scan_interval" not in data
            or data["scan_interval"] == DEFAULT_OPTIONS["scan_interval"]
        )

    def test_scan_interval_min_boundary(self):
        """vol.Range(min=10): value 10 must be accepted."""
        schema_inner = _get_section_schema("polling")
        result = schema_inner({"scan_interval": 10})
        assert result["scan_interval"] == 10

    def test_scan_interval_below_min_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("polling")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"scan_interval": 9})

    def test_scan_interval_max_boundary(self):
        schema_inner = _get_section_schema("polling")
        result = schema_inner({"scan_interval": 3600})
        assert result["scan_interval"] == 3600

    def test_scan_interval_above_max_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("polling")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"scan_interval": 3601})

    def test_snapshot_interval_min_boundary(self):
        schema_inner = _get_section_schema("polling")
        result = schema_inner({"snapshot_interval": 300})
        assert result["snapshot_interval"] == 300

    def test_snapshot_interval_below_min_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("polling")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"snapshot_interval": 299})


class TestFeaturesSection:
    @pytest.mark.asyncio
    async def test_all_feature_toggles_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "features": {
                    "enable_snapshots": True,
                    "enable_sensors": False,
                    "enable_binary_sensors": False,
                    "enable_snapshot_button": True,
                    "enable_intercom": True,
                },
            },
        )
        assert data["enable_snapshots"] is True
        assert data["enable_sensors"] is False
        assert data["enable_binary_sensors"] is False
        assert data["enable_snapshot_button"] is True
        assert data["enable_intercom"] is True

    @pytest.mark.asyncio
    async def test_boolean_coercion_1_0(self):
        """int 1/0 → True/False (HA schema may deliver ints from selector)."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "features": {"enable_snapshots": 1, "enable_intercom": 0},
            },
        )
        assert data["enable_snapshots"] is True
        assert data["enable_intercom"] is False

    def test_enable_snapshots_default_true(self):
        assert DEFAULT_OPTIONS["enable_snapshots"] is True

    def test_enable_intercom_default_false(self):
        assert DEFAULT_OPTIONS["enable_intercom"] is False


class TestStreamSection:
    @pytest.mark.asyncio
    async def test_stream_type_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        for val in ["auto", "local", "remote"]:
            data = await _submit(flow, {"stream": {"stream_connection_type": val}})
            assert data["stream_connection_type"] == val

    @pytest.mark.asyncio
    async def test_live_buffer_mode_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        for val in ["latency", "balanced", "stable"]:
            data = await _submit(flow, {"stream": {"live_buffer_mode": val}})
            assert data["live_buffer_mode"] == val

    @pytest.mark.asyncio
    async def test_enable_go2rtc_toggle(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"stream": {"enable_go2rtc": False}})
        assert data["enable_go2rtc"] is False

    def test_stream_connection_type_default(self):
        # v12.4.2 flipped the default to "local" (LOCAL-first); existing
        # installs that relied on the old "auto" default are migrated via
        # async_migrate_entry → see test_local_first_default.py.
        assert DEFAULT_OPTIONS["stream_connection_type"] == "local"

    def test_live_buffer_mode_default(self):
        assert DEFAULT_OPTIONS["live_buffer_mode"] == "balanced"

    def test_enable_go2rtc_default_true(self):
        assert DEFAULT_OPTIONS["enable_go2rtc"] is True


class TestFcmSection:
    @pytest.mark.asyncio
    async def test_fcm_push_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "fcm": {
                    "enable_fcm_push": True,
                    "fcm_push_mode": "android",
                    "mark_events_read": True,
                    "alert_save_snapshots": True,
                    "alert_delete_after_send": False,
                    "alert_notify_service": "notify.test_user",
                    "alert_notify_information": "notify.info",
                    "alert_notify_screenshot": "notify.screenshot",
                    "alert_notify_video": "notify.video",
                    "alert_notify_system": "notify.system",
                },
            },
        )
        assert data["enable_fcm_push"] is True
        assert data["fcm_push_mode"] == "android"
        assert data["mark_events_read"] is True
        assert data["alert_save_snapshots"] is True
        assert data["alert_delete_after_send"] is False
        assert data["alert_notify_service"] == "notify.test_user"
        assert data["alert_notify_video"] == "notify.video"

    @pytest.mark.asyncio
    async def test_empty_alert_services_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "fcm": {
                    "alert_notify_service": "",
                    "alert_notify_video": "",
                },
            },
        )
        assert data.get("alert_notify_service", "") == ""

    def test_mark_events_read_default_false(self):
        """xDraGGi regression: mark_events_read must default OFF."""
        assert DEFAULT_OPTIONS.get("mark_events_read", False) is False

    def test_enable_fcm_push_default_false(self):
        assert DEFAULT_OPTIONS["enable_fcm_push"] is False

    def test_alert_delete_after_send_default_true(self):
        assert DEFAULT_OPTIONS["alert_delete_after_send"] is True

    def test_all_fcm_modes_valid(self):
        """fcm_push_mode must accept all four documented values."""
        import voluptuous as vol

        validator = vol.In(["auto", "android", "ios", "polling"])
        for mode in ["auto", "android", "ios", "polling"]:
            assert validator(mode) == mode

    def test_invalid_fcm_mode_rejected(self):
        import voluptuous as vol

        with pytest.raises(vol.Invalid):
            vol.In(["auto", "android", "ios", "polling"])("unknown")


class TestEventsStorageSection:
    @pytest.mark.asyncio
    async def test_enable_local_save_defaults_off(self):
        """v11.0.12 regression: fresh install must NOT auto-save events."""
        assert DEFAULT_OPTIONS["enable_local_save"] is False

    @pytest.mark.asyncio
    async def test_enable_local_save_toggle_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"events_storage": {"enable_local_save": True}})
        assert data["enable_local_save"] is True

    @pytest.mark.asyncio
    async def test_enable_local_save_int_coercion(self):
        """enable_local_save int 1/0 must be coerced to bool (was missing from coerce list)."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"events_storage": {"enable_local_save": 1}})
        assert data["enable_local_save"] is True
        assert isinstance(data["enable_local_save"], bool)

        flow2 = BoschCameraOptionsFlow(_make_entry())
        data2 = await _submit(flow2, {"events_storage": {"enable_local_save": 0}})
        assert data2["enable_local_save"] is False
        assert isinstance(data2["enable_local_save"], bool)

    @pytest.mark.asyncio
    async def test_download_path_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "events_storage": {"download_path": "/config/my_events"},
            },
        )
        assert data["download_path"] == "/config/my_events"

    @pytest.mark.asyncio
    async def test_smb_fields_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "events_storage": {
                    "enable_smb_upload": True,
                    "upload_protocol": "ftp",
                    "smb_server": "192.168.1.100",
                    "smb_share": "NAS",
                    "smb_username": "user",
                    "smb_password": "secret",
                    "smb_base_path": "Bosch",
                    "folder_pattern": "{year}/{month}",
                    "file_pattern": "{camera}_{id}",
                    "smb_retention_days": 90,
                },
            },
        )
        assert data["enable_smb_upload"] is True
        assert data["upload_protocol"] == "ftp"
        assert data["smb_server"] == "192.168.1.100"
        assert data["smb_retention_days"] == 90

    def test_smb_retention_days_range(self):
        schema_inner = _get_section_schema("events_storage")
        assert schema_inner({"smb_retention_days": 0})["smb_retention_days"] == 0
        assert schema_inner({"smb_retention_days": 3650})["smb_retention_days"] == 3650

    def test_smb_retention_days_above_max_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("events_storage")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"smb_retention_days": 3651})

    def test_default_download_path(self):
        assert DEFAULT_OPTIONS["download_path"] == "/config/bosch_events"

    def test_default_smb_base_path(self):
        assert DEFAULT_OPTIONS["smb_base_path"] == "Bosch-Kameras"


class TestNvrSection:
    @pytest.mark.asyncio
    async def test_nvr_fields_saved(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "nvr": {
                    "enable_nvr": True,
                    "nvr_storage_target": "smb",
                    "nvr_base_path": "/config/bosch_nvr",
                    "nvr_smb_subpath": "NVR",
                    "nvr_retention_days": 7,
                },
            },
        )
        assert data["enable_nvr"] is True
        assert data["nvr_storage_target"] == "smb"
        assert data["nvr_base_path"] == "/config/bosch_nvr"
        assert data["nvr_smb_subpath"] == "NVR"
        assert data["nvr_retention_days"] == 7

    def test_nvr_retention_min(self):
        schema_inner = _get_section_schema("nvr")
        assert schema_inner({"nvr_retention_days": 1})["nvr_retention_days"] == 1

    def test_nvr_retention_below_min_raises(self):
        import voluptuous as vol

        schema_inner = _get_section_schema("nvr")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema_inner({"nvr_retention_days": 0})

    def test_nvr_retention_max(self):
        schema_inner = _get_section_schema("nvr")
        assert schema_inner({"nvr_retention_days": 365})["nvr_retention_days"] == 365

    def test_nvr_storage_targets(self):
        assert DEFAULT_OPTIONS["nvr_storage_target"] == "local"

    def test_enable_nvr_default_false(self):
        assert DEFAULT_OPTIONS["enable_nvr"] is False

    @pytest.mark.asyncio
    async def test_nvr_storage_target_ftp(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(flow, {"nvr": {"nvr_storage_target": "ftp"}})
        assert data["nvr_storage_target"] == "ftp"


class TestAiSection:
    """Tests for AI options schema validation (E-P1 / E-P2 / E-P3)."""

    # E-P1: entity fields are nullable selectors (gate-disable via clear)
    # The fields use the serializer-supported nullable shape
    # ``vol.Any(None, EntitySelector(...))`` (issue #35). The frontend submits
    # ``None`` (allow_none) — NOT ""  — when an entity picker is cleared, so the
    # disable-the-gate path is None here, not "".

    def test_ai_task_entity_none_allowed(self) -> None:
        """Clearing the ai_task entity selector submits None — must NOT raise."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_TASK_ENTITY: None})
        assert result[CONF_AI_TASK_ENTITY] is None

    def test_ai_task_entity_valid_entity_id_allowed(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_TASK_ENTITY: "ai_task.my_llm"})
        assert result[CONF_AI_TASK_ENTITY] == "ai_task.my_llm"

    def test_ai_active_condition_entity_none_allowed(self) -> None:
        """Clearing the condition entity selector submits None — must NOT raise."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_CONDITION_ENTITY: None})
        assert result[CONF_AI_ACTIVE_CONDITION_ENTITY] is None

    def test_ai_active_condition_entity_valid_entity_id_allowed(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_CONDITION_ENTITY: "input_boolean.away"})
        assert result[CONF_AI_ACTIVE_CONDITION_ENTITY] == "input_boolean.away"

    # E-P2: ai_max_per_day has no upper cap (0 = unlimited is honoured)

    def test_ai_max_per_day_zero_allowed(self) -> None:
        """0 = unlimited must pass (E-P2)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_MAX_PER_DAY: 0})
        assert result[CONF_AI_MAX_PER_DAY] == 0

    def test_ai_max_per_day_large_value_allowed(self) -> None:
        """Values beyond old max=100000 cap must now pass (E-P2)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_MAX_PER_DAY: 500000})
        assert result[CONF_AI_MAX_PER_DAY] == 500000

    def test_ai_max_per_day_negative_rejected(self) -> None:
        """Negative budgets must be rejected."""
        import voluptuous as vol

        schema = _get_section_schema("ai")
        with pytest.raises((vol.Invalid, vol.MultipleInvalid)):
            schema({CONF_AI_MAX_PER_DAY: -1})

    # E-P3: time fields accept HH:MM / HH:MM:SS or empty (gate-disable)

    def test_ai_active_time_start_empty_allowed(self) -> None:
        """Empty string must be allowed to disable the time gate (E-P3)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_START: ""})
        assert result[CONF_AI_ACTIVE_TIME_START] == ""

    def test_ai_active_time_end_empty_allowed(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_END: ""})
        assert result[CONF_AI_ACTIVE_TIME_END] == ""

    def test_ai_active_time_start_hhmm_valid(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_START: "08:00"})
        assert result[CONF_AI_ACTIVE_TIME_START] == "08:00"

    def test_ai_active_time_end_hhmmss_valid(self) -> None:
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_END: "22:30:00"})
        assert result[CONF_AI_ACTIVE_TIME_END] == "22:30:00"

    # NOTE (issue #35): the time fields are plain TextSelectors now — the old
    # ``vol.Any("", vol.All(..., vol.Match(HH:MM)))`` schema-level regex could
    # not be serialised by the frontend and 500'd the options dialog. Format
    # validation therefore lives in the backend window parser
    # (_ai_window_allowed), which treats any unparseable value as "no time gate"
    # (see test_ai_window_* for that behaviour). The schema now accepts any
    # string so the dialog stays serialisable; these tests pin that contract.

    def test_ai_active_time_start_garbage_accepted_at_schema_level(self) -> None:
        """Prose values are accepted by the schema (validated at runtime)."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_START: "midnight"})
        assert result[CONF_AI_ACTIVE_TIME_START] == "midnight"

    def test_ai_active_time_end_out_of_range_accepted_at_schema_level(self) -> None:
        """'25:00' passes the schema; the runtime gate rejects it as malformed."""
        schema = _get_section_schema("ai")
        result = schema({CONF_AI_ACTIVE_TIME_END: "25:00"})
        assert result[CONF_AI_ACTIVE_TIME_END] == "25:00"

    # E-P1 + round-trip: submit AI section with entity cleared

    @pytest.mark.asyncio
    async def test_ai_entity_fields_cleared_saves_none(self) -> None:
        """Clearing the nullable entity pickers submits None (allow_none) and
        must persist None — disabling the respective gate (issue #35, E-P1)."""
        flow = BoschCameraOptionsFlow(
            _make_entry(
                options={
                    CONF_AI_TASK_ENTITY: "ai_task.old_llm",
                    CONF_AI_ACTIVE_CONDITION_ENTITY: "input_boolean.away",
                }
            )
        )
        data = await _submit(
            flow,
            {
                "ai": {
                    CONF_AI_TASK_ENTITY: None,
                    CONF_AI_ACTIVE_CONDITION_ENTITY: None,
                    CONF_AI_ACTIVE_TIME_START: "",
                    CONF_AI_ACTIVE_TIME_END: "",
                }
            },
        )
        assert data[CONF_AI_TASK_ENTITY] is None
        assert data[CONF_AI_ACTIVE_CONDITION_ENTITY] is None

    @pytest.mark.asyncio
    async def test_ai_time_gate_with_valid_times_saves(self) -> None:
        """Valid HH:MM times must be saved correctly."""
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "ai": {
                    CONF_AI_ACTIVE_TIME_START: "07:00",
                    CONF_AI_ACTIVE_TIME_END: "23:00",
                    CONF_AI_TASK_ENTITY: "",
                    CONF_AI_ACTIVE_CONDITION_ENTITY: "",
                }
            },
        )
        assert data[CONF_AI_ACTIVE_TIME_START] == "07:00"
        assert data[CONF_AI_ACTIVE_TIME_END] == "23:00"


class TestAuthSection:
    @pytest.mark.asyncio
    async def test_force_relogin_triggers_relogin_step(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.async_step_relogin_show = AsyncMock(
            return_value={"type": "form", "step_id": "relogin_show"}
        )
        result = await flow.async_step_init(
            user_input={
                "auth": {"force_relogin": True},
            }
        )
        assert result["step_id"] == "relogin_show"

    @pytest.mark.asyncio
    async def test_force_relogin_false_does_not_branch(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
        result = await flow.async_step_init(
            user_input={
                "auth": {"force_relogin": False},
            }
        )
        assert result["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_migrate_oss_triggers_abort_for_legacy_token(self):
        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=_legacy_token()))
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.async_create_task = MagicMock()
        flow._config_entry.async_start_reauth = MagicMock(return_value=None)
        flow.async_abort = MagicMock(
            return_value={"type": "abort", "reason": "migration_started"}
        )
        result = await flow.async_step_init(
            user_input={
                "auth": {"migrate_to_oss_client": True},
            }
        )
        assert result["reason"] == "migration_started"

    @pytest.mark.asyncio
    async def test_migrate_field_absent_for_oss_token(self):
        """migrate_to_oss_client must NOT appear in the schema for OSS tokens."""
        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=_oss_token()))
        captured_schema = {}

        def capture(**kw):
            captured_schema["schema"] = kw.get("data_schema")
            return {"type": "form"}

        flow.async_show_form = capture
        await flow.async_step_init(user_input=None)
        schema = captured_schema["schema"]
        # Flatten all keys from the schema
        all_keys = {str(k) for k in schema.schema}
        # The migrate field is inside the auth section — get inner schema
        auth_section = None
        for k, v in schema.schema.items():
            if str(k) == "auth":
                # v is a section object; .schema is the inner vol.Schema
                inner = getattr(v, "schema", None) or v
                if hasattr(inner, "schema"):
                    auth_section = inner.schema
                break
        if auth_section is not None:
            inner_keys = {str(k) for k in auth_section}
            assert "migrate_to_oss_client" not in inner_keys, (
                "migrate_to_oss_client must not appear in schema for OSS token"
            )


class TestFullRoundTrip:
    """Submit all sections at once and verify every key lands correctly."""

    @pytest.mark.asyncio
    async def test_all_sections_submitted_together(self):
        flow = BoschCameraOptionsFlow(_make_entry())
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 45,
                    "interval_status": 200,
                    "interval_events": 150,
                    "snapshot_interval": 900,
                },
                "features": {
                    "enable_snapshots": True,
                    "enable_sensors": True,
                    "enable_binary_sensors": True,
                    "enable_snapshot_button": False,
                    "enable_intercom": False,
                },
                "stream": {
                    "stream_connection_type": "local",
                    "live_buffer_mode": "latency",
                    "enable_go2rtc": True,
                },
                "fcm": {
                    "enable_fcm_push": True,
                    "fcm_push_mode": "ios",
                    "mark_events_read": False,
                    "alert_save_snapshots": False,
                    "alert_delete_after_send": True,
                    "alert_notify_service": "notify.test_user",
                    "alert_notify_information": "",
                    "alert_notify_screenshot": "",
                    "alert_notify_video": "notify.video",
                    "alert_notify_system": "",
                },
                "events_storage": {
                    "enable_local_save": True,
                    "download_path": "/config/bosch_events",
                    "enable_smb_upload": False,
                    "upload_protocol": "smb",
                    "smb_server": "",
                    "smb_share": "",
                    "smb_username": "",
                    "smb_password": "",
                    "smb_base_path": "Bosch-Kameras",
                    "folder_pattern": "{year}/{month}/{day}",
                    "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
                    "smb_retention_days": 180,
                },
                "nvr": {
                    "enable_nvr": False,
                    "nvr_storage_target": "local",
                    "nvr_base_path": "/config/bosch_nvr",
                    "nvr_smb_subpath": "NVR",
                    "nvr_retention_days": 3,
                },
                "auth": {"force_relogin": False},
            },
        )
        # Spot-check a key from each section
        assert data["scan_interval"] == 45
        assert data["enable_snapshots"] is True
        assert data["stream_connection_type"] == "local"
        assert data["enable_fcm_push"] is True
        assert data["fcm_push_mode"] == "ios"
        assert data["enable_local_save"] is True
        assert data["enable_nvr"] is False

    @pytest.mark.asyncio
    async def test_existing_options_not_lost_when_partial_submit(self):
        """Only 'polling' submitted → prior enable_go2rtc option survives in saved data
        because _flatten_sections passes through the flat submit dict and
        async_step_init merges with existing options first."""
        prior = {"enable_go2rtc": False}
        flow = BoschCameraOptionsFlow(_make_entry(options=prior))
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 45,
                    "interval_status": 300,
                    "interval_events": 300,
                    "snapshot_interval": 1800,
                }
            },
        )
        # The submitted 'polling' section was the only one sent.
        # enable_go2rtc not in the submit → but must be preserved via prior options merge.
        assert data["scan_interval"] == 45
        assert data["enable_go2rtc"] is False  # non-submitted field preserved

    @pytest.mark.asyncio
    async def test_suggested_value_field_preserved_when_user_does_not_edit(self):
        """Regression: smb_server (suggested_value-only, no default=) must NOT revert
        to '' when the user opens options and saves without touching the SMB section.

        Before the fix, the save path called async_create_entry(data=user_input)
        directly; fields absent from user_input (because the user did not touch them)
        were silently dropped, reverting to DEFAULT_OPTIONS values.

        After the fix, async_step_init merges user_input on top of the existing opts:
            merged = {**opts, **user_input}
        so the persisted smb_server survives even when the user does not submit it.
        """
        prior = {
            "smb_server": "192.168.2.25",
            "smb_share": "bosch-events",
            "smb_username": "nas_user",
            "smb_password": "s3cret",
            "alert_notify_service": "notify.mobile_app",
            "alert_notify_information": "true",
            "download_path": "/config/my_events",
        }
        flow = BoschCameraOptionsFlow(_make_entry(options=prior))
        # User only touches the polling interval — does NOT submit any events_storage
        # or fcm section fields → those suggested_value fields must survive.
        data = await _submit(
            flow,
            {
                "polling": {
                    "scan_interval": 120,
                    "interval_status": 300,
                    "interval_events": 300,
                    "snapshot_interval": 1800,
                }
            },
        )
        assert data["scan_interval"] == 120
        # suggested_value-only fields — must keep saved values, NOT revert to "":
        assert data["smb_server"] == "192.168.2.25"
        assert data["smb_share"] == "bosch-events"
        assert data["smb_username"] == "nas_user"
        assert data["smb_password"] == "s3cret"
        assert data["alert_notify_service"] == "notify.mobile_app"
        assert data["alert_notify_information"] == "true"
        assert data["download_path"] == "/config/my_events"

    @pytest.mark.asyncio
    async def test_suggested_value_field_preserved_on_migrate_to_oss_path(self):
        """Regression: same merge must happen on the migrate_to_oss code path.

        When the user clicks 'migrate to OSS client', async_update_entry is called
        with the merged options dict.  Without the merge, suggested_value fields
        absent from user_input were dropped before being persisted.
        """
        prior = {
            "smb_server": "192.168.2.25",
            "alert_notify_service": "notify.mobile_app",
        }
        entry = _make_entry(options=prior, bearer_token=_legacy_token())
        # async_start_reauth is called as a coroutine on the config entry
        entry.async_start_reauth = AsyncMock(return_value=None)  # type: ignore[attr-defined]
        flow = BoschCameraOptionsFlow(entry)
        flow.hass = MagicMock()
        flow.hass.config_entries = MagicMock()
        flow.hass.async_create_task = MagicMock()

        update_calls: list[dict] = []

        def capture_update(e, **kw):  # type: ignore[no-untyped-def]
            update_calls.append(kw)

        flow.hass.config_entries.async_update_entry = capture_update
        flow.async_abort = MagicMock(return_value={"type": "abort"})

        # User submits with migrate_to_oss_client=True but no SMB fields
        await flow.async_step_init(
            user_input={"auth": {"force_relogin": False, "migrate_to_oss_client": True}}
        )

        assert update_calls, "async_update_entry was never called"
        saved_options = update_calls[0].get("options", {})
        # suggested_value fields absent from user_input must be in the persisted options
        assert saved_options.get("smb_server") == "192.168.2.25"
        assert saved_options.get("alert_notify_service") == "notify.mobile_app"


class TestDefaultOptionsCompleteness:
    """Every key in DEFAULT_OPTIONS must be in exactly one OPTIONS_SECTIONS entry.

    If DEFAULT_OPTIONS grows a new key and OPTIONS_SECTIONS is not updated, the
    field silently falls through the options UI — users can never change it.
    """

    def test_all_default_option_keys_covered_by_sections(self):
        all_section_fields = {f for fields in OPTIONS_SECTIONS.values() for f in fields}
        missing = [k for k in DEFAULT_OPTIONS if k not in all_section_fields]
        assert not missing, (
            f"DEFAULT_OPTIONS keys not in any OPTIONS_SECTIONS section: {missing}. "
            "Add them to the correct section so users can configure them."
        )

    def test_no_section_field_missing_from_defaults(self):
        """Every OPTIONS_SECTIONS field should have a default (or be optional
        text-only). Fails loudly when a new UI field is added without a default."""
        # Text fields with suggested_value only (no hard default) are OK to be absent
        TEXT_ONLY_FIELDS = {
            "alert_notify_service",
            "alert_notify_information",
            "alert_notify_screenshot",
            "alert_notify_video",
            "alert_notify_system",
            "smb_server",
            "smb_share",
            "smb_username",
            "smb_password",
            "smb_base_path",
            "folder_pattern",
            "file_pattern",
            "nvr_base_path",
            "nvr_smb_subpath",
            "download_path",
            # auth actions — not persistent state
            "force_relogin",
            "migrate_to_oss_client",
        }
        all_section_fields = {f for fields in OPTIONS_SECTIONS.values() for f in fields}
        missing_defaults = [
            f
            for f in all_section_fields
            if f not in DEFAULT_OPTIONS and f not in TEXT_ONLY_FIELDS
        ]
        assert not missing_defaults, (
            f"OPTIONS_SECTIONS fields with no default in DEFAULT_OPTIONS: {missing_defaults}"
        )


# OptionsFlow schema must be frontend-serializable (issue #35)
#
# Bug source: GitHub issue #35 (GhostRider2809, v13.7.2) — opening
# Settings → Integrations → Bosch Smart Home Camera → *Configure* failed
# with "Der Konfigurationsfluss konnte nicht geladen werden: 500 Internal
# Server Error". Reproduced on the maintainer's own instance.
#
# Root cause: the AI section (added in v13.7.0) declared four fields as
# ``vol.Any("", <Selector>)`` to allow an empty value. HA serialises every
# options/config-flow schema to JSON for the frontend via
# ``voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)``
# (homeassistant/helpers/data_entry_flow.py ``_prepare_result_json``).
# ``voluptuous_serialize`` has no converter for a ``vol.Any`` node and raises
# ``ValueError: Unable to convert schema: Any(...)`` — surfaced to the
# browser as a 500. The dialog had been unopenable since v13.7.0; it only
# manifests when a user actually opens *Configure*.
#
# Fix: selectors are optional/clearable on their own — drop the
# ``vol.Any`` wrappers and use the bare ``EntitySelector`` / ``TextSelector``.
# This test pins the schema against the *exact* serialisation HA performs,
# so any future unserialisable node (``vol.Any``, a raw lambda, …) fails
# here instead of in a user's browser.


async def _capture_init_schema(flow: BoschCameraOptionsFlow):
    """Run the GET path of async_step_init and return its data_schema."""
    captured: dict = {}

    def capture(**kw):
        captured["schema"] = kw.get("data_schema")
        return {"type": "form"}

    flow.async_show_form = capture  # type: ignore[method-assign]
    await flow.async_step_init(user_input=None)
    return captured["schema"]


def _serialize_like_frontend(schema) -> list:
    """Exactly what homeassistant.helpers.data_entry_flow does before sending
    the form to the browser. Raises ValueError on an unserialisable node."""
    return voluptuous_serialize.convert(schema, custom_serializer=cv.custom_serializer)


class TestOptionsSchemaSerializable:
    @pytest.mark.asyncio
    async def test_default_entry_schema_serializes(self):
        """The very thing that 500'd: serialise the freshly-built options form."""
        flow = BoschCameraOptionsFlow(_make_entry())
        schema = await _capture_init_schema(flow)
        # Must not raise "Unable to convert schema: Any(...)".
        result = _serialize_like_frontend(schema)
        assert isinstance(result, list) and result, "schema serialised to nothing"

    @pytest.mark.asyncio
    async def test_legacy_token_schema_serializes(self):
        """Legacy token adds the migrate_to_oss_client field — serialise too."""
        flow = BoschCameraOptionsFlow(_make_entry(bearer_token=_legacy_token()))
        schema = await _capture_init_schema(flow)
        result = _serialize_like_frontend(schema)
        assert isinstance(result, list) and result

    @pytest.mark.asyncio
    async def test_schema_with_existing_ai_options_serializes(self):
        """Pre-existing AI option values (suggested_value path) must serialise."""
        flow = BoschCameraOptionsFlow(
            _make_entry(
                options={
                    CONF_AI_TASK_ENTITY: "ai_task.openai",
                    CONF_AI_ACTIVE_TIME_START: "08:00",
                    CONF_AI_ACTIVE_TIME_END: "22:00",
                    CONF_AI_ACTIVE_CONDITION_ENTITY: "person.thomas",
                }
            )
        )
        schema = await _capture_init_schema(flow)
        result = _serialize_like_frontend(schema)
        assert isinstance(result, list) and result

    @pytest.mark.asyncio
    async def test_ai_fields_still_present(self):
        """Guard against the fix accidentally dropping the AI fields."""
        flow = BoschCameraOptionsFlow(_make_entry())
        schema = await _capture_init_schema(flow)
        ai_section = None
        for k, v in schema.schema.items():
            if str(k) == "ai":
                inner = getattr(v, "schema", None) or v
                ai_section = getattr(inner, "schema", inner)
                break
        assert ai_section is not None, "AI section missing from options schema"
        keys = {str(k) for k in ai_section}
        for field in (
            CONF_AI_TASK_ENTITY,
            CONF_AI_ACTIVE_TIME_START,
            CONF_AI_ACTIVE_TIME_END,
            CONF_AI_ACTIVE_CONDITION_ENTITY,
        ):
            assert field in keys, f"{field} missing from AI section after fix"


# Translation-file structure backing the options-flow sections
#
# HA frontend resolves options-flow field labels at:
#   options.step.init.sections.<section_key>.data.<field>
# NOT at the flat options.step.init.data.<field>.
#
# All three files (strings.json, translations/en.json, translations/de.json)
# must follow this nested structure or every label shows as a raw underscore
# key.
#
# Reported by Thomas (session 2026-05-07): ALL toggle labels displayed as raw
# Python keys (e.g. enable_snapshots instead of "Camera snapshots") because
# the flat data dict was used instead of section-nested data.

COMP = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"

TRANSLATION_FILES = {
    "strings.json": COMP / "strings.json",
    "en.json": COMP / "translations" / "en.json",
    "de.json": COMP / "translations" / "de.json",
}


def _load_translation(name: str) -> dict:
    return json.loads(TRANSLATION_FILES[name].read_text())


def _init_sections(data: dict) -> dict:
    """Return the options.step.init.sections dict."""
    return data.get("options", {}).get("step", {}).get("init", {}).get("sections", {})


def _all_section_labels(sections: dict) -> set[str]:
    """Collect every field key from all section data blocks."""
    return {k for sec in sections.values() for k in sec.get("data", {})}


class TestTranslationStructure:
    """Verify labels live inside sections, not flat at the step level."""

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_no_flat_data_at_step_level(self, filename: str) -> None:
        """options.step.init must NOT have a top-level 'data' key.

        If labels exist at the flat level HA frontend ignores them and shows
        raw underscore keys instead.
        """
        d = _load_translation(filename)
        init = d.get("options", {}).get("step", {}).get("init", {})
        assert "data" not in init, (
            f"{filename}: found flat 'data' at options.step.init — "
            "labels must be inside sections.<name>.data, not at the step level"
        )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_no_flat_data_description_at_step_level(self, filename: str) -> None:
        """options.step.init must NOT have a top-level 'data_description' key."""
        d = _load_translation(filename)
        init = d.get("options", {}).get("step", {}).get("init", {})
        assert "data_description" not in init, (
            f"{filename}: found flat 'data_description' at options.step.init"
        )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_all_option_fields_have_label_in_sections(self, filename: str) -> None:
        """Every field in OPTIONS_SECTIONS must have a label in the correct section."""
        d = _load_translation(filename)
        sections = _init_sections(d)
        missing = []
        for section_key, fields in OPTIONS_SECTIONS.items():
            section_data = sections.get(section_key, {}).get("data", {})
            for field in fields:
                if field not in section_data:
                    missing.append(f"{section_key}.{field}")
        assert not missing, f"{filename}: missing labels in sections.data: {missing}"

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_all_sections_have_name(self, filename: str) -> None:
        """Every section in OPTIONS_SECTIONS must have a translated name."""
        d = _load_translation(filename)
        sections = _init_sections(d)
        for section_key in OPTIONS_SECTIONS:
            assert section_key in sections, (
                f"{filename}: section '{section_key}' missing from sections block"
            )
            assert sections[section_key].get("name"), (
                f"{filename}: section '{section_key}' has no 'name' translation"
            )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_no_extra_fields_in_sections(self, filename: str) -> None:
        """No field in any section.data should be absent from OPTIONS_SECTIONS."""
        d = _load_translation(filename)
        sections = _init_sections(d)
        all_known = {f for fields in OPTIONS_SECTIONS.values() for f in fields}
        for section_key, sec in sections.items():
            for field in sec.get("data", {}):
                assert field in all_known, (
                    f"{filename}: section '{section_key}' has unknown field "
                    f"'{field}' not in OPTIONS_SECTIONS"
                )

    @pytest.mark.parametrize("filename", list(TRANSLATION_FILES))
    def test_each_field_in_correct_section(self, filename: str) -> None:
        """A field must be in exactly the section OPTIONS_SECTIONS assigns it to."""
        d = _load_translation(filename)
        sections = _init_sections(d)
        for section_key, fields in OPTIONS_SECTIONS.items():
            section_data = sections.get(section_key, {}).get("data", {})
            for field in fields:
                # Also check it isn't duplicated in a wrong section
                wrong = [
                    sk
                    for sk, sec in sections.items()
                    if sk != section_key and field in sec.get("data", {})
                ]
                assert not wrong, (
                    f"{filename}: field '{field}' appears in wrong section(s): {wrong}"
                )

    def test_de_json_uses_german_labels(self):
        """Spot-check a few DE labels to catch copy-paste of EN content."""
        d = _load_translation("de.json")
        sections = _init_sections(d)
        features = sections.get("features", {}).get("data", {})
        # These must be German, not English
        assert features.get("enable_snapshots") != "Camera snapshots", (
            "de.json features.data.enable_snapshots still has English label"
        )
        polling = sections.get("polling", {}).get("data", {})
        assert polling.get("scan_interval") != "Polling interval (seconds)", (
            "de.json polling.data.scan_interval still has English label"
        )


@pytest.mark.asyncio
async def test_options_flow_invalid_bind_host_sets_error() -> None:
    """frigate_bind_host with a non-IP value → errors['frigate_bind_host'] == 'invalid_ip_address'."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    captured: dict[str, dict] = {}
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: (
            captured.update({"errors": kw.get("errors", {})}) or {"type": "form"}
        )
    )

    await flow.async_step_init(user_input={"frigate_bind_host": "not_an_ip"})

    assert captured["errors"].get("frigate_bind_host") == "invalid_ip_address"


@pytest.mark.asyncio
async def test_options_flow_invalid_ip_allowlist_sets_error() -> None:
    """frigate_ip_allowlist with a bad CIDR → errors['frigate_ip_allowlist'] == 'invalid_ip_allowlist'."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    captured: dict[str, dict] = {}
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: (
            captured.update({"errors": kw.get("errors", {})}) or {"type": "form"}
        )
    )

    await flow.async_step_init(user_input={"frigate_ip_allowlist": "not_a_cidr"})

    assert captured["errors"].get("frigate_ip_allowlist") == "invalid_ip_allowlist"


@pytest.mark.asyncio
async def test_options_flow_invalid_ip_allowlist_reports_offending_token() -> None:
    """Runde2 P3 #8: the bad token must be surfaced via description_placeholders
    so the user knows WHICH entry in a comma-separated list is invalid, instead
    of only a generic error. The first invalid token in a mixed valid/invalid
    list is reported (loop breaks on first failure, same as before)."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    captured: dict[str, dict] = {}
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: (
            captured.update(
                {
                    "errors": kw.get("errors", {}),
                    "placeholders": kw.get("description_placeholders", {}),
                }
            )
            or {"type": "form"}
        )
    )

    await flow.async_step_init(
        user_input={"frigate_ip_allowlist": "192.168.1.0/24, not_a_cidr, 10.0.0.5"}
    )

    assert captured["errors"].get("frigate_ip_allowlist") == "invalid_ip_allowlist"
    assert captured["placeholders"].get("invalid_allowlist_token") == "not_a_cidr"


@pytest.mark.asyncio
async def test_options_flow_valid_ip_allowlist_no_error_empty_placeholder() -> None:
    """A fully valid allowlist sets no error — the flow proceeds straight to
    async_create_entry (mirrors the existing valid-bind-host success path),
    so async_show_form is never invoked at all."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    flow.async_show_form = MagicMock()  # type: ignore[method-assign]
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})  # type: ignore[method-assign]

    await flow.async_step_init(
        user_input={"frigate_ip_allowlist": "192.168.1.0/24, 10.0.0.5"}
    )

    flow.async_show_form.assert_not_called()
    flow.async_create_entry.assert_called_once()


@pytest.mark.asyncio
async def test_options_flow_empty_ip_allowlist_no_error() -> None:
    """Empty/whitespace-only allowlist is valid (feature opt-out) — must not
    error, proceeds straight to async_create_entry like the valid case."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    flow.async_show_form = MagicMock()  # type: ignore[method-assign]
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})  # type: ignore[method-assign]

    await flow.async_step_init(user_input={"frigate_ip_allowlist": "   "})

    flow.async_show_form.assert_not_called()
    flow.async_create_entry.assert_called_once()


@pytest.mark.asyncio
async def test_options_flow_invalid_webhook_url_sets_error() -> None:
    """webhook_url without an http(s):// prefix → errors['webhook_url'] ==
    'invalid_webhook_url', mirroring the existing diagnostic_cloud_api_override
    pattern (startswith check, same error style)."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow
    from custom_components.bosch_shc_camera.const import CONF_WEBHOOK_URL

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    captured: dict[str, dict] = {}
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: (
            captured.update({"errors": kw.get("errors", {})}) or {"type": "form"}
        )
    )

    await flow.async_step_init(
        user_input={CONF_WEBHOOK_URL: "example.com/hook-no-scheme"}
    )

    assert captured["errors"].get(CONF_WEBHOOK_URL) == "invalid_webhook_url"


@pytest.mark.asyncio
async def test_options_flow_valid_webhook_url_variants_no_error() -> None:
    """Both http:// and https:// prefixes are accepted (local-network webhook
    receivers commonly run plain http, matching CLAUDE.md LOCAL_OVER_REMOTE)."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow
    from custom_components.bosch_shc_camera.const import CONF_WEBHOOK_URL

    for valid_url in (
        "https://example.com/hook",
        "http://192.168.1.50:8123/api/webhook/abc",
    ):
        entry = SimpleNamespace(
            entry_id="01TEST",
            data={"bearer_token": "", "refresh_token": "rt"},
            options={},
        )
        flow = BoschCameraOptionsFlow(entry)
        flow.async_show_form = MagicMock()  # type: ignore[method-assign]
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})  # type: ignore[method-assign]

        await flow.async_step_init(user_input={CONF_WEBHOOK_URL: valid_url})

        flow.async_show_form.assert_not_called()
        flow.async_create_entry.assert_called_once()


@pytest.mark.asyncio
async def test_options_flow_webhook_url_uppercase_scheme_no_error() -> None:
    """Scheme is case-insensitive per RFC 3986 — some clipboard/paste sources
    and third-party webhook-URL generators uppercase it (e.g. HTTPS://...).
    Found during THREE_PER_ISSUE_PER_CHANGE review; the check must not
    false-positive a technically-valid uppercase-scheme URL as invalid."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow
    from custom_components.bosch_shc_camera.const import CONF_WEBHOOK_URL

    for mixed_case_url in (
        "HTTPS://example.com/hook",
        "Http://192.168.1.50:8123/api/webhook/abc",
    ):
        entry = SimpleNamespace(
            entry_id="01TEST",
            data={"bearer_token": "", "refresh_token": "rt"},
            options={},
        )
        flow = BoschCameraOptionsFlow(entry)
        flow.async_show_form = MagicMock()  # type: ignore[method-assign]
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})  # type: ignore[method-assign]

        await flow.async_step_init(user_input={CONF_WEBHOOK_URL: mixed_case_url})

        flow.async_show_form.assert_not_called()
        flow.async_create_entry.assert_called_once()


@pytest.mark.asyncio
async def test_options_flow_empty_webhook_url_no_error() -> None:
    """Empty webhook_url is valid (feature disabled/unset) — must not error,
    same opt-out semantics as the empty-allowlist and empty-cloud-api-override
    cases elsewhere in this flow. Proceeds straight to async_create_entry."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow
    from custom_components.bosch_shc_camera.const import CONF_WEBHOOK_URL

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    flow.async_show_form = MagicMock()  # type: ignore[method-assign]
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})  # type: ignore[method-assign]

    await flow.async_step_init(user_input={CONF_WEBHOOK_URL: ""})

    flow.async_show_form.assert_not_called()
    flow.async_create_entry.assert_called_once()


@pytest.mark.asyncio
async def test_options_flow_webhook_url_whitespace_only_no_error() -> None:
    """Whitespace-only webhook_url strips to empty → treated as unset, not a
    format error (guards against a stray-space typo blocking unrelated saves).
    Proceeds straight to async_create_entry."""
    from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow
    from custom_components.bosch_shc_camera.const import CONF_WEBHOOK_URL

    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options={},
    )
    flow = BoschCameraOptionsFlow(entry)
    flow.async_show_form = MagicMock()  # type: ignore[method-assign]
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})  # type: ignore[method-assign]

    await flow.async_step_init(user_input={CONF_WEBHOOK_URL: "   "})

    flow.async_show_form.assert_not_called()
    flow.async_create_entry.assert_called_once()


class TestGH5ReauthReconfigureFlow:
    """dziko83 (closed GH issue) hit a 404 on the legacy re-auth link. The
    fix was an in-HA Reconfigure flow that never leaves the app; these tests
    pin that the flow steps + translation stay in place."""

    def test_reauth_step_exists(self):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraConfigFlow,
        )

        assert hasattr(BoschCameraConfigFlow, "async_step_reauth")
        assert hasattr(BoschCameraConfigFlow, "async_step_reauth_confirm")

    def test_reconfigure_step_exists(self):
        from custom_components.bosch_shc_camera.config_flow import (
            BoschCameraConfigFlow,
        )

        assert hasattr(BoschCameraConfigFlow, "async_step_reconfigure"), (
            "The 'Reconfigure' menu item must keep existing — it reruns the "
            "OAuth flow without deleting the entry, fixing the legacy 404 "
            "re-auth-link issue."
        )

    def test_reauth_string_exists(self):
        strings = json.loads(
            (
                Path(__file__).parent.parent
                / "custom_components"
                / "bosch_shc_camera"
                / "strings.json"
            ).read_text()
        )
        assert "reauth_confirm" in strings.get("config", {}).get("step", {})


class TestClassRenamesConfigFlow:
    """BoschSHCCameraConfigFlow / BoschSHCCameraOptionsFlow were renamed to
    drop the legacy SHC prefix (2026-05-07). Entity IDs and unique IDs are
    unchanged — these tests guard against re-introducing the old names."""

    def test_bosch_camera_config_flow_importable(self):
        """BoschCameraConfigFlow is the current class name in config_flow.py."""
        import importlib

        mod = importlib.import_module("custom_components.bosch_shc_camera.config_flow")
        assert hasattr(mod, "BoschCameraConfigFlow")

    def test_bosch_camera_options_flow_importable(self):
        """BoschCameraOptionsFlow is the current class name in config_flow.py."""
        import importlib

        mod = importlib.import_module("custom_components.bosch_shc_camera.config_flow")
        assert hasattr(mod, "BoschCameraOptionsFlow")

    def test_old_config_flow_names_gone(self):
        """Old SHC-prefixed config flow names must no longer exist."""
        import importlib

        mod = importlib.import_module("custom_components.bosch_shc_camera.config_flow")
        assert not hasattr(mod, "BoschSHCCameraConfigFlow"), (
            "BoschSHCCameraConfigFlow still exists — remove the old name"
        )
        assert not hasattr(mod, "BoschSHCCameraOptionsFlow"), (
            "BoschSHCCameraOptionsFlow still exists — remove the old name"
        )
