"""Config flow for Bosch Smart Home Camera integration.

Setup flow (automatic OAuth2 with PKCE):
  One-click browser login via Bosch SingleKey ID.
  Uses my.home-assistant.io redirect for automatic callback.

Options flow:
  Step "init"          — feature toggles
  Step "relogin_show"  — shows login URL as read-only field
  Step "relogin_paste" — paste redirect URL

OAuth2 details:
  Issuer:       smarthome.authz.bosch.com/auth/realms/home_auth_provider
  Client ID:    oss_residential_app
  Redirect URI: https://my.home-assistant.io/redirect/oauth
  Scopes:       email offline_access profile openid
"""

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from typing import Any
from urllib.parse import parse_qs, urlencode

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    AbstractOAuth2FlowHandler,
    AbstractOAuth2Implementation,
    async_register_implementation,
    _encode_jwt,
)
from homeassistant.helpers.selector import (
    SelectSelector, SelectSelectorConfig, SelectSelectorMode, SelectOptionDict,
)

from . import DOMAIN, DEFAULT_OPTIONS

_LOGGER = logging.getLogger(__name__)

KEYCLOAK_BASE = (
    "https://smarthome.authz.bosch.com"
    "/auth/realms/home_auth_provider/protocol/openid-connect"
)
CLIENT_ID     = "oss_residential_app"
CLIENT_SECRET = base64.b64decode("RjFqWnpzRzVOdHc3eDJWVmM4SjZxZ3NuaXNNT2ZhWmc=").decode()
SCOPES        = "email offline_access profile openid"
REDIRECT_URI  = "https://my.home-assistant.io/redirect/oauth"
REDIRECT_URI_MANUAL = "https://www.bosch.com/boschcam"
CLOUD_API     = "https://residential.cbs.boschsecurity.com"


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ── OAuth2 Implementation (automatic flow via my.home-assistant.io) ──────────

class BoschOAuth2Implementation(AbstractOAuth2Implementation):
    """Bosch Keycloak OAuth2 implementation with PKCE."""

    def __init__(self, hass) -> None:
        self.hass = hass
        self._last_verifier: str | None = None

    @property
    def name(self) -> str:
        return "Bosch SingleKey ID"

    @property
    def domain(self) -> str:
        return DOMAIN

    @property
    def redirect_uri(self) -> str:
        return REDIRECT_URI

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """Generate Keycloak authorization URL with PKCE challenge."""
        self._last_verifier, challenge = _pkce_pair()
        redirect_uri = self.redirect_uri
        state = _encode_jwt(self.hass, {
            "flow_id": flow_id,
            "redirect_uri": redirect_uri,
        })
        params = {
            "client_id":             CLIENT_ID,
            "response_type":         "code",
            "scope":                 SCOPES,
            "redirect_uri":          redirect_uri,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
            "state":                 state,
        }
        return f"{KEYCLOAK_BASE}/auth?" + urlencode(params)

    async def async_resolve_external_data(self, external_data: Any) -> dict:
        """Exchange authorization code for tokens."""
        code = external_data["code"]
        redirect_uri = external_data["state"]["redirect_uri"]
        session = async_get_clientsession(self.hass, verify_ssl=False)
        async with session.post(
            f"{KEYCLOAK_BASE}/token",
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  redirect_uri,
                "code_verifier": self._last_verifier,
            },
            ssl=False,
        ) as resp:
            if resp.status >= 400:
                body = await resp.text()
                _LOGGER.error("Token exchange failed: HTTP %d — %s", resp.status, body[:200])
            resp.raise_for_status()
            return await resp.json()

    async def _async_refresh_token(self, token: dict) -> dict:
        """Refresh access token via Keycloak."""
        session = async_get_clientsession(self.hass, verify_ssl=False)
        async with session.post(
            f"{KEYCLOAK_BASE}/token",
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "refresh_token",
                "refresh_token": token["refresh_token"],
            },
            ssl=False,
        ) as resp:
            if resp.status >= 400:
                _LOGGER.error("Token refresh failed: HTTP %d", resp.status)
            resp.raise_for_status()
            new_token = await resp.json()
            return {**token, **new_token}


# ── Manual flow helpers (for options re-login) ───────────────────────────────

def _build_auth_url(code_challenge: str, state: str) -> str:
    """Build auth URL for manual re-login (uses bosch.com redirect)."""
    params = {
        "client_id":             CLIENT_ID,
        "response_type":         "code",
        "scope":                 SCOPES,
        "redirect_uri":          REDIRECT_URI_MANUAL,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
        "state":                 state,
    }
    return f"{KEYCLOAK_BASE}/auth?" + urlencode(params)


def _extract_code(redirect_url: str) -> str | None:
    """Extract the auth code from the pasted redirect URL."""
    url = redirect_url.strip()
    if "?" in url:
        url = url.split("?", 1)[1]
    qs = parse_qs(url)
    if qs.get("error"):
        return None
    codes = qs.get("code")
    return codes[0] if codes else None


async def _exchange_code(session, code: str, verifier: str) -> dict | None:
    """Exchange auth code for tokens (manual flow, bosch.com redirect)."""
    try:
        async with asyncio.timeout(15):
            async with session.post(
                f"{KEYCLOAK_BASE}/token",
                data={
                    "client_id":     CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  REDIRECT_URI_MANUAL,
                    "code_verifier": verifier,
                },
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                _LOGGER.warning("Token exchange HTTP %d: %s", resp.status, await resp.text())
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("Token exchange error: %s", err)
    return None


def _detect_token_client_id(bearer_token: str) -> str | None:
    """Parse a Bosch Keycloak JWT and return the `azp` (authorized party) claim.

    Returns e.g. "oss_residential_app" (new OSS client) or "residential_app"
    (legacy client), or None if the token can't be parsed. Used by the options
    flow to decide whether to show the "migrate to new OAuth client" button.
    """
    if not bearer_token:
        return None
    try:
        parts = bearer_token.split(".")
        if len(parts) < 2:
            return None
        payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("azp")
    except Exception:
        return None


class RefreshTokenInvalidError(Exception):
    """Keycloak rejected the refresh token (invalid_grant / 400 / 401).

    This is non-recoverable without user interaction — the caller should
    trigger the reauth flow instead of retrying.
    """


class AuthServerOutageError(Exception):
    """Bosch Keycloak auth server returned 5xx — server-side outage.

    The refresh token is probably still valid; retrying later will recover
    once Bosch's infrastructure is back. Caller should NOT trigger the
    reauth flow (nothing for the user to fix) — just back off and retry.
    """


async def _do_refresh(session, refresh_token: str) -> dict | None:
    """Silent renewal via saved refresh_token.

    Returns the token dict on success.
    Returns None on transient client-side failures (network error, timeout)
    — caller may retry.
    Raises RefreshTokenInvalidError on 400/401 (invalid_grant) — caller should
    trigger the reauth flow, retrying is pointless.
    Raises AuthServerOutageError on 5xx — Bosch server is down, retry later
    but do NOT trigger reauth.
    """
    try:
        async with asyncio.timeout(15):
            async with session.post(
                f"{KEYCLOAK_BASE}/token",
                data={
                    "client_id":     CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh_token,
                },
                ssl=False,
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                body = (await resp.text())[:300]
                _LOGGER.warning(
                    "Token refresh HTTP %d — Keycloak response: %s",
                    resp.status, body,
                )
                if resp.status in (400, 401):
                    raise RefreshTokenInvalidError(
                        f"Keycloak HTTP {resp.status}: {body}"
                    )
                if 500 <= resp.status < 600:
                    raise AuthServerOutageError(
                        f"Bosch Keycloak HTTP {resp.status}"
                    )
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("Token refresh error: %s", err)
    return None


# ─────────────────────────────────────────────────────────────────────────────
class BoschSHCCameraConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Handle the initial setup flow — automatic OAuth2 PKCE browser login."""

    DOMAIN = DOMAIN
    VERSION = 1

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    async def async_step_user(self, user_input=None):
        """Start OAuth2 flow — register implementation, then delegate to parent."""
        # Only enforce unique_id uniqueness on fresh setup, not on reauth —
        # reauth reuses the existing entry.
        if self.source != config_entries.SOURCE_REAUTH:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

        # Register our OAuth2 implementation (idempotent — safe to call multiple times)
        async_register_implementation(
            self.hass, DOMAIN, BoschOAuth2Implementation(self.hass)
        )

        return await super().async_step_user(user_input)

    async def async_step_reauth(
        self, entry_data: dict
    ) -> config_entries.ConfigFlowResult:
        """Start a reauth flow triggered by invalid_grant/expired refresh token.

        Shows a confirmation dialog, then runs the normal auto-login flow
        (browser → Bosch SingleKey ID → redirect back via my.home-assistant.io).
        On success, the existing config entry is updated in place — options,
        entities, and automations are preserved.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict | None = None
    ) -> config_entries.ConfigFlowResult:
        """Show confirmation, then delegate to the OAuth2 user flow."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict) -> config_entries.ConfigFlowResult:
        """Handle completed OAuth2 flow — create new entry or update existing (reauth)."""
        token_data = data.get("token", {})
        new_data = {
            "bearer_token":  token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
        }
        # Reauth: update the existing entry in place (keeps options, entities,
        # automations, FCM config, SMB settings — everything).
        if self.source == config_entries.SOURCE_REAUTH:
            existing = self._get_reauth_entry()
            return self.async_update_reload_and_abort(
                existing, data_updates=new_data,
            )
        return self.async_create_entry(
            title="Bosch Smart Home Camera",
            data=new_data,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BoschSHCCameraOptionsFlow(config_entry)


# ─────────────────────────────────────────────────────────────────────────────
class BoschSHCCameraOptionsFlow(config_entries.OptionsFlow):
    """Handle options: feature toggles + optional re-login."""

    def __init__(self, config_entry) -> None:
        self._config_entry  = config_entry
        self._verifier: str = ""
        self._auth_url: str = ""
        self._pending_options: dict = {}

    async def async_step_init(self, user_input=None):
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self._config_entry.options)

        current_client = _detect_token_client_id(
            self._config_entry.data.get("bearer_token", "")
        )
        is_legacy_client = current_client == "residential_app"

        if user_input is not None:
            force_relogin = user_input.pop("force_relogin", False)
            migrate_to_oss = user_input.pop("migrate_to_oss_client", False)

            for k in ["enable_snapshots", "enable_sensors",
                      "enable_snapshot_button", "enable_auto_download",
                      "high_quality_video", "enable_binary_sensors",
                      "enable_fcm_push", "alert_save_snapshots",
                      "alert_delete_after_send", "audio_default_on",
                      "enable_intercom",
                      "enable_smb_upload",
                      "enable_go2rtc",
                      "debug_logging"]:
                if k in user_input:
                    user_input[k] = bool(user_input[k])

            if migrate_to_oss:
                # Persist any other option changes first so they survive reauth
                self.hass.config_entries.async_update_entry(
                    self._config_entry, options=user_input,
                )
                # Use HA's native reauth trigger — scheduled as a task so the
                # options dialog closes before the reauth flow registers
                # (prevents UI race with stacked dialogs). async_start_reauth
                # is a coroutine in HA 2022.7+, so it must be awaited or
                # wrapped in a task.
                self.hass.async_create_task(
                    self._config_entry.async_start_reauth(self.hass)
                )
                return self.async_abort(reason="migration_started")

            if force_relogin:
                self._pending_options  = user_input
                self._verifier, challenge = _pkce_pair()
                self._auth_url = _build_auth_url(challenge, secrets.token_urlsafe(16))
                return await self.async_step_relogin_show()

            return self.async_create_entry(title="", data=user_input)

        has_refresh = bool(self._config_entry.data.get("refresh_token", ""))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    "scan_interval",
                    default=int(opts.get("scan_interval", 60)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Optional(
                    "interval_status",
                    default=int(opts.get("interval_status", 300)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Optional(
                    "interval_events",
                    default=int(opts.get("interval_events", 300)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Optional(
                    "snapshot_interval",
                    default=int(opts.get("snapshot_interval", 1800)),
                ): vol.All(vol.Coerce(int), vol.Range(min=300, max=86400)),
                vol.Optional(
                    "enable_snapshots",
                    default=bool(opts.get("enable_snapshots", True)),
                ): bool,
                vol.Optional(
                    "enable_sensors",
                    default=bool(opts.get("enable_sensors", True)),
                ): bool,
                vol.Optional(
                    "enable_snapshot_button",
                    default=bool(opts.get("enable_snapshot_button", True)),
                ): bool,
                vol.Optional(
                    "enable_auto_download",
                    default=bool(opts.get("enable_auto_download", False)),
                ): bool,
                vol.Optional(
                    "download_path",
                    description={"suggested_value": opts.get("download_path", "")},
                ): str,
                # SHC local API — camera light + privacy mode
                vol.Optional(
                    "shc_ip",
                    description={"suggested_value": opts.get("shc_ip", "")},
                ): str,
                vol.Optional(
                    "shc_cert_path",
                    description={"suggested_value": opts.get("shc_cert_path", "")},
                ): str,
                vol.Optional(
                    "shc_key_path",
                    description={"suggested_value": opts.get("shc_key_path", "")},
                ): str,
                vol.Optional(
                    "high_quality_video",
                    default=bool(opts.get("high_quality_video", False)),
                ): bool,
                vol.Optional(
                    "stream_connection_type",
                    default=str(opts.get("stream_connection_type", "auto")),
                ): SelectSelector(SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value="auto", label="Auto (Lokal → Cloud Fallback)"),
                        SelectOptionDict(value="local", label="Nur Lokal (LAN direkt)"),
                        SelectOptionDict(value="remote", label="Nur Cloud (Bosch Proxy)"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )),
                vol.Optional(
                    "live_buffer_mode",
                    default=str(opts.get("live_buffer_mode", "balanced")),
                ): SelectSelector(SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value="latency",  label="Latenz (geringe Verzögerung, kann ruckeln)"),
                        SelectOptionDict(value="balanced", label="Ausgewogen (Standard)"),
                        SelectOptionDict(value="stable",   label="Stabil (kein Ruckeln, mehr Verzögerung)"),
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )),
                vol.Optional(
                    "enable_binary_sensors",
                    default=bool(opts.get("enable_binary_sensors", True)),
                ): bool,
                vol.Optional(
                    "enable_fcm_push",
                    default=bool(opts.get("enable_fcm_push", False)),
                ): bool,
                vol.Optional(
                    "alert_notify_service",
                    description={"suggested_value": opts.get("alert_notify_service", "")},
                ): str,
                vol.Optional(
                    "alert_notify_system",
                    description={"suggested_value": opts.get("alert_notify_system", "")},
                ): str,
                vol.Optional(
                    "alert_notify_information",
                    description={"suggested_value": opts.get("alert_notify_information", "")},
                ): str,
                vol.Optional(
                    "alert_notify_screenshot",
                    description={"suggested_value": opts.get("alert_notify_screenshot", "")},
                ): str,
                vol.Optional(
                    "alert_notify_video",
                    description={"suggested_value": opts.get("alert_notify_video", "")},
                ): str,
                vol.Optional(
                    "alert_save_snapshots",
                    default=bool(opts.get("alert_save_snapshots", False)),
                ): bool,
                vol.Optional(
                    "alert_delete_after_send",
                    default=bool(opts.get("alert_delete_after_send", True)),
                ): bool,
                vol.Optional(
                    "fcm_push_mode",
                    default=str(opts.get("fcm_push_mode", "auto")),
                ): vol.In(["auto", "android", "ios", "polling"]),
                vol.Optional(
                    "audio_default_on",
                    default=bool(opts.get("audio_default_on", True)),
                ): bool,
                vol.Optional(
                    "enable_intercom",
                    default=bool(opts.get("enable_intercom", False)),
                ): bool,
                vol.Optional(
                    "enable_smb_upload",
                    default=bool(opts.get("enable_smb_upload", False)),
                ): bool,
                vol.Optional(
                    "smb_server",
                    description={"suggested_value": opts.get("smb_server", "")},
                ): str,
                vol.Optional(
                    "smb_share",
                    description={"suggested_value": opts.get("smb_share", "")},
                ): str,
                vol.Optional(
                    "smb_username",
                    description={"suggested_value": opts.get("smb_username", "")},
                ): str,
                vol.Optional(
                    "smb_password",
                    description={"suggested_value": opts.get("smb_password", "")},
                ): str,
                vol.Optional(
                    "smb_base_path",
                    description={"suggested_value": opts.get("smb_base_path", "Bosch-Kameras")},
                ): str,
                vol.Optional(
                    "smb_folder_pattern",
                    description={"suggested_value": opts.get("smb_folder_pattern", "{year}/{month}")},
                ): str,
                vol.Optional(
                    "smb_file_pattern",
                    description={"suggested_value": opts.get("smb_file_pattern", "{camera}_{date}_{time}_{type}_{id}")},
                ): str,
                vol.Optional(
                    "smb_retention_days",
                    default=int(opts.get("smb_retention_days", 180)),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=3650)),
                vol.Optional(
                    "smb_disk_warn_mb",
                    default=int(opts.get("smb_disk_warn_mb", 5120)),
                ): vol.All(vol.Coerce(int), vol.Range(min=0, max=1000000)),
                vol.Optional(
                    "enable_go2rtc",
                    default=bool(opts.get("enable_go2rtc", True)),
                ): bool,
                vol.Optional(
                    "debug_logging",
                    default=bool(opts.get("debug_logging", False)),
                ): bool,
                vol.Optional("force_relogin", default=False): bool,
                **({
                    vol.Optional("migrate_to_oss_client", default=False): bool,
                } if is_legacy_client else {}),
            }),
            description_placeholders={
                "token_status": "active (auto-renews)" if has_refresh else "no refresh token",
            },
        )

    async def async_step_relogin_show(self, user_input=None):
        """Show login URL as a pre-filled text field. PKCE already generated in init."""
        if user_input is not None:
            return await self.async_step_relogin_paste()

        return self.async_show_form(
            step_id="relogin_show",
            data_schema=vol.Schema({
                vol.Optional("login_url", default=self._auth_url): str,
            }),
        )

    async def async_step_relogin_paste(self, user_input=None):
        """Paste the redirect URL and exchange for new tokens."""
        errors = {}

        if user_input is not None:
            redirect_url = user_input.get("redirect_url", "").strip()
            code = _extract_code(redirect_url)

            if not code:
                errors["redirect_url"] = "invalid_redirect_url"
            else:
                session = async_get_clientsession(self.hass, verify_ssl=False)
                tokens  = await _exchange_code(session, code, self._verifier)

                if not tokens or not tokens.get("access_token"):
                    errors["redirect_url"] = "token_exchange_failed"
                else:
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data={
                            **self._config_entry.data,
                            "bearer_token":  tokens["access_token"],
                            "refresh_token": tokens.get("refresh_token", ""),
                        },
                    )
                    _LOGGER.info("Token re-authenticated successfully — reloading integration")
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(self._config_entry.entry_id)
                    )
                    return self.async_create_entry(title="", data=self._pending_options)

        return self.async_show_form(
            step_id="relogin_paste",
            data_schema=vol.Schema({
                vol.Required("redirect_url"): str,
            }),
            errors=errors,
        )
