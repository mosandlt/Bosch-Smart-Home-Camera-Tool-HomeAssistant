"""Config flow for Bosch Smart Home Camera integration.

Setup flow (no existing refresh_token):
  Step 1 — "auth" — Shows the Bosch login URL, user logs in, pastes redirect URL
  Step 2 — Exchanges auth code for access_token + refresh_token → creates entry

Setup flow (refresh_token already stored in entry data):
  → Silent renewal via refresh_token, no browser needed

Options flow (Settings → Integrations → Configure):
  • Force re-login  — clear refresh_token and repeat browser flow
  • Scan interval   — how often to refresh snapshots (seconds)
  • Feature toggles

OAuth2 details:
  Issuer:       smarthome.authz.bosch.com/auth/realms/home_auth_provider
  Client ID:    residential_app
  Redirect URI: https://www.bosch.com/boschcam  (shows 404 — expected)
  Scopes:       email offline_access profile openid
"""

import asyncio
import base64
import hashlib
import logging
import secrets
from urllib.parse import parse_qs, urlencode

import aiohttp
import async_timeout
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DOMAIN, DEFAULT_OPTIONS

_LOGGER = logging.getLogger(__name__)

KEYCLOAK_BASE = (
    "https://smarthome.authz.bosch.com"
    "/auth/realms/home_auth_provider/protocol/openid-connect"
)
CLIENT_ID     = "residential_app"
CLIENT_SECRET = "yUmjfFutWfKbYOOficWFrcFeD14oFW0C"
SCOPES        = "email offline_access profile openid"
REDIRECT_URI  = "https://www.bosch.com/boschcam"
CLOUD_API     = "https://residential.cbs.boschsecurity.com"


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(64)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _build_auth_url(code_challenge: str, state: str) -> str:
    params = {
        "client_id":             CLIENT_ID,
        "response_type":         "code",
        "scope":                 SCOPES,
        "redirect_uri":          REDIRECT_URI,
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


# ── Token exchange / refresh ───────────────────────────────────────────────────

async def _exchange_code(session, code: str, verifier: str) -> dict | None:
    """Exchange auth code for access_token + refresh_token."""
    try:
        async with async_timeout.timeout(15):
            async with session.post(
                f"{KEYCLOAK_BASE}/token",
                data={
                    "client_id":     CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  REDIRECT_URI,
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


async def _do_refresh(session, refresh_token: str) -> dict | None:
    """Silent renewal via saved refresh_token."""
    try:
        async with async_timeout.timeout(15):
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
                _LOGGER.warning("Token refresh HTTP %d", resp.status)
    except (asyncio.TimeoutError, aiohttp.ClientError) as err:
        _LOGGER.warning("Token refresh error: %s", err)
    return None


async def _validate_token(hass, token: str) -> bool:
    """Quick check: does this token work against /v11/video_inputs?"""
    session = async_get_clientsession(hass, verify_ssl=False)
    try:
        async with async_timeout.timeout(10):
            async with session.get(
                f"{CLOUD_API}/v11/video_inputs",
                headers={"Authorization": f"Bearer {token}"},
                ssl=False,
            ) as resp:
                return resp.status == 200
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
class BoschSHCCameraConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow — OAuth2 PKCE browser login."""

    VERSION = 1

    def __init__(self) -> None:
        self._verifier: str = ""
        self._challenge: str = ""
        self._state: str = ""
        self._auth_url: str = ""

    async def async_step_user(self, user_input=None):
        """Entry point — generate PKCE pair and show login URL."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Generate PKCE pair and auth URL
        self._verifier, self._challenge = _pkce_pair()
        self._state    = secrets.token_urlsafe(16)
        self._auth_url = _build_auth_url(self._challenge, self._state)

        return await self.async_step_auth()

    async def async_step_auth(self, user_input=None):
        """Show login URL and wait for the user to paste the redirect URL."""
        errors = {}

        if user_input is not None:
            redirect_url = user_input.get("redirect_url", "").strip()
            code = _extract_code(redirect_url)

            if not code:
                errors["redirect_url"] = "invalid_redirect_url"
            else:
                session = async_get_clientsession(self.hass, verify_ssl=False)
                tokens  = await _exchange_code(session, code, self._verifier)

                if not tokens:
                    errors["redirect_url"] = "token_exchange_failed"
                else:
                    access  = tokens.get("access_token", "")
                    refresh = tokens.get("refresh_token", "")

                    if not access:
                        errors["redirect_url"] = "token_exchange_failed"
                    else:
                        return self.async_create_entry(
                            title="Bosch Smart Home Camera",
                            data={
                                "bearer_token":  access,
                                "refresh_token": refresh,
                            },
                        )

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema({
                vol.Required("redirect_url"): str,
            }),
            errors=errors,
            description_placeholders={
                "auth_url": self._auth_url,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BoschSHCCameraOptionsFlow(config_entry)


# ─────────────────────────────────────────────────────────────────────────────
class BoschSHCCameraOptionsFlow(config_entries.OptionsFlow):
    """Handle options: re-login + feature toggles."""

    def __init__(self, config_entry) -> None:
        self._config_entry = config_entry
        self._verifier: str = ""
        self._challenge: str = ""
        self._state: str = ""
        self._auth_url: str = ""

    async def async_step_init(self, user_input=None):
        """Main options page."""
        errors = {}
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self._config_entry.options)

        if user_input is not None:
            force_relogin = user_input.pop("force_relogin", False)

            if force_relogin:
                # Start browser login flow from options
                self._verifier, self._challenge = _pkce_pair()
                self._state    = secrets.token_urlsafe(16)
                self._auth_url = _build_auth_url(self._challenge, self._state)
                # Save the other options first so they survive the relogin step
                self._pending_options = user_input
                return await self.async_step_relogin()

            # Save options as-is
            for k in ["enable_snapshots", "enable_sensors",
                      "enable_snapshot_button", "enable_auto_download"]:
                if k in user_input:
                    user_input[k] = bool(user_input[k])

            return self.async_create_entry(title="", data=user_input)

        return self._show_options_form(opts, errors)

    def _show_options_form(self, opts, errors):
        refresh_token = self._config_entry.data.get("refresh_token", "")
        relogin_desc  = "Re-login required (no refresh token saved)" if not refresh_token else "Force new browser login (refresh token already saved — usually not needed)"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    "scan_interval",
                    default=int(opts.get("scan_interval", 30)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),

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
                    default=str(opts.get("download_path", "")),
                ): str,

                vol.Optional("force_relogin", default=False): bool,
            }),
            errors=errors,
            description_placeholders={"relogin_desc": relogin_desc},
        )

    async def async_step_relogin(self, user_input=None):
        """Re-login step from options — same as initial auth step."""
        errors = {}

        if user_input is not None:
            redirect_url = user_input.get("redirect_url", "").strip()
            code = _extract_code(redirect_url)

            if not code:
                errors["redirect_url"] = "invalid_redirect_url"
            else:
                session = async_get_clientsession(self.hass, verify_ssl=False)
                tokens  = await _exchange_code(session, code, self._verifier)

                if not tokens:
                    errors["redirect_url"] = "token_exchange_failed"
                else:
                    access  = tokens.get("access_token", "")
                    refresh = tokens.get("refresh_token", "")
                    if access:
                        self.hass.config_entries.async_update_entry(
                            self._config_entry,
                            data={
                                **self._config_entry.data,
                                "bearer_token":  access,
                                "refresh_token": refresh,
                            },
                        )
                        _LOGGER.info("Token re-authenticated successfully")
                        pending = getattr(self, "_pending_options", {})
                        for k in ["enable_snapshots", "enable_sensors",
                                  "enable_snapshot_button", "enable_auto_download"]:
                            if k in pending:
                                pending[k] = bool(pending[k])
                        return self.async_create_entry(title="", data=pending)
                    errors["redirect_url"] = "token_exchange_failed"

        return self.async_show_form(
            step_id="relogin",
            data_schema=vol.Schema({
                vol.Required("redirect_url"): str,
            }),
            errors=errors,
            description_placeholders={
                "auth_url": self._auth_url,
            },
        )
