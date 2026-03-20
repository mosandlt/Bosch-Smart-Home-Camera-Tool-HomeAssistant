"""Config flow for Bosch Smart Home Camera integration.

Setup flow (two steps):
  Step "user" — shows login URL as a read-only text field + instructions
  Step "auth" — user pastes the redirect URL, tokens are exchanged

Options flow:
  Step "init"          — feature toggles
  Step "relogin_show"  — shows login URL as read-only field
  Step "relogin_paste" — paste redirect URL

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


# ── Token exchange / refresh ──────────────────────────────────────────────────

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


# ─────────────────────────────────────────────────────────────────────────────
class BoschSHCCameraConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow — OAuth2 PKCE browser login."""

    VERSION = 1

    def __init__(self) -> None:
        self._verifier: str = ""
        self._auth_url: str = ""

    async def async_step_user(self, user_input=None):
        """
        Step 1: Show the login URL as a pre-filled text field.
        PKCE pair is generated once and stored — NOT regenerated on submit.
        User copies the URL, opens it in browser, logs in, then clicks Submit.
        """
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        # Generate PKCE pair only once (not on every call)
        if not self._verifier:
            self._verifier, challenge = _pkce_pair()
            state          = secrets.token_urlsafe(16)
            self._auth_url = _build_auth_url(challenge, state)

        if user_input is not None:
            # User clicked Submit after opening the URL → go to paste step
            return await self.async_step_auth()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Optional("login_url", default=self._auth_url): str,
            }),
        )

    async def async_step_auth(self, user_input=None):
        """Step 2: Paste the redirect URL, exchange code for tokens."""
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
                    return self.async_create_entry(
                        title="Bosch Smart Home Camera",
                        data={
                            "bearer_token":  tokens["access_token"],
                            "refresh_token": tokens.get("refresh_token", ""),
                        },
                    )

        return self.async_show_form(
            step_id="auth",
            data_schema=vol.Schema({
                vol.Required("redirect_url"): str,
            }),
            errors=errors,
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

        if user_input is not None:
            force_relogin = user_input.pop("force_relogin", False)

            for k in ["enable_snapshots", "enable_sensors",
                      "enable_snapshot_button", "enable_auto_download"]:
                if k in user_input:
                    user_input[k] = bool(user_input[k])

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
                    default=str(opts.get("download_path", "")),
                ): str,
                # SHC local API — camera light + privacy mode
                vol.Optional(
                    "shc_ip",
                    default=str(opts.get("shc_ip", "")),
                ): str,
                vol.Optional(
                    "shc_cert_path",
                    default=str(opts.get("shc_cert_path", "")),
                ): str,
                vol.Optional(
                    "shc_key_path",
                    default=str(opts.get("shc_key_path", "")),
                ): str,
                vol.Optional("force_relogin", default=False): bool,
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
                    _LOGGER.info("Token re-authenticated successfully")
                    return self.async_create_entry(title="", data=self._pending_options)

        return self.async_show_form(
            step_id="relogin_paste",
            data_schema=vol.Schema({
                vol.Required("redirect_url"): str,
            }),
            errors=errors,
        )
