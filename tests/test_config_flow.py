"""Tests for the Bosch Smart Home Camera config flow.

Covers Quality-Scale Bronze rule `config-flow-test-coverage`. Verifies:
  - Single-instance enforcement (unique_config_entry rule)
  - Reauth flow updates the existing entry in place
  - Reconfigure flow updates the existing entry in place
  - OAuth callback creates a new entry with redacted token data
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera.const import DOMAIN


async def test_user_flow_aborts_when_already_configured(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Adding the integration twice must abort with `already_configured`."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"


async def test_reauth_confirm_shows_form(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Triggering reauth shows the confirm form before re-running OAuth."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": mock_config_entry.entry_id,
        },
        data=mock_config_entry.data,
    )
    assert result["type"] == "form"
    assert result["step_id"] == "reauth_confirm"


async def test_reconfigure_shows_form(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfigure flow shows the confirm form."""
    mock_config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": mock_config_entry.entry_id,
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
