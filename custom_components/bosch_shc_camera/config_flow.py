"""Config flow for Bosch Smart Home Camera integration.

Initial setup:
  Step 1 — Enter Bearer token → validates against /v11/video_inputs

Options flow (Settings → Integrations → Configure):
  • Bearer token refresh  — paste a fresh token when the old one expires
  • Scan interval         — how often to refresh snapshots (seconds)
  • Feature toggles:
      enable_snapshots        — camera entities with latest JPEG
      enable_sensors          — status / last-event / events-today sensors
      enable_snapshot_button  — "Refresh Snapshot" + "Open Live Stream" buttons
      enable_auto_download    — background download of all event files
      download_path           — local folder for downloaded events
"""

import asyncio
import logging

import aiohttp
import async_timeout
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import DOMAIN, DEFAULT_OPTIONS

_LOGGER = logging.getLogger(__name__)
CLOUD_API = "https://residential.cbs.boschsecurity.com"


async def _validate_token(hass, token: str) -> tuple[bool, str]:
    """Validate bearer token by calling /v11/video_inputs."""
    session = async_get_clientsession(hass, verify_ssl=False)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with async_timeout.timeout(10):
            async with session.get(
                f"{CLOUD_API}/v11/video_inputs", headers=headers
            ) as resp:
                if resp.status == 200:
                    cams = await resp.json()
                    return True, f"Found {len(cams)} camera(s)"
                elif resp.status == 401:
                    return False, "token_expired"
                else:
                    return False, f"api_error_{resp.status}"
    except asyncio.TimeoutError:
        return False, "timeout"
    except aiohttp.ClientError:
        return False, "connection_error"


# ─────────────────────────────────────────────────────────────────────────────
class BoschSHCCameraConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            token = user_input["bearer_token"].strip()
            ok, msg = await _validate_token(self.hass, token)
            if ok:
                return self.async_create_entry(
                    title="Bosch Smart Home Camera",
                    data={"bearer_token": token},
                )
            errors["bearer_token"] = (
                "token_expired"    if "expired"    in msg or "401" in msg else
                "timeout"          if "timeout"    in msg else
                "cannot_connect"
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("bearer_token"): str,
            }),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return BoschSHCCameraOptionsFlow(config_entry)


# ─────────────────────────────────────────────────────────────────────────────
class BoschSHCCameraOptionsFlow(config_entries.OptionsFlow):
    """Handle options: token refresh + feature toggles."""

    def __init__(self, config_entry):
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        errors = {}

        # Merge saved options with defaults
        opts = dict(DEFAULT_OPTIONS)
        opts.update(self._config_entry.options)

        if user_input is not None:
            # ── Token refresh (optional) ─────────────────────────────────────
            new_token = user_input.pop("bearer_token", "").strip()
            if new_token:
                ok, msg = await _validate_token(self.hass, new_token)
                if ok:
                    # Update the config entry data with the new token
                    self.hass.config_entries.async_update_entry(
                        self._config_entry,
                        data={**self._config_entry.data, "bearer_token": new_token},
                    )
                    _LOGGER.info("Bearer token updated successfully")
                else:
                    errors["bearer_token"] = (
                        "token_expired" if "expired" in msg else "cannot_connect"
                    )
                    return self._show_form(opts, errors)

            # ── Save feature options ─────────────────────────────────────────
            # Ensure boolean fields are actual booleans (form can return strings)
            bool_keys = [
                "enable_snapshots", "enable_sensors",
                "enable_snapshot_button", "enable_auto_download",
            ]
            for k in bool_keys:
                if k in user_input:
                    user_input[k] = bool(user_input[k])

            return self.async_create_entry(title="", data=user_input)

        return self._show_form(opts, errors)

    def _show_form(self, opts: dict, errors: dict):
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                # ── Token refresh ─────────────────────────────────────────────
                vol.Optional("bearer_token", default=""): str,

                # ── Polling interval ──────────────────────────────────────────
                vol.Optional(
                    "scan_interval",
                    default=int(opts.get("scan_interval", 30)),
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),

                # ── Feature toggles ───────────────────────────────────────────
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

                # ── Auto-download ─────────────────────────────────────────────
                vol.Optional(
                    "enable_auto_download",
                    default=bool(opts.get("enable_auto_download", False)),
                ): bool,

                vol.Optional(
                    "download_path",
                    default=str(opts.get("download_path", "")),
                ): str,
            }),
            errors=errors,
            description_placeholders={
                "token_hint": "Leave blank to keep the existing token",
            },
        )
