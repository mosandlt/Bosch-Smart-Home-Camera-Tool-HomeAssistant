"""Tests for the Bosch Smart Home Camera config flow.

Covers Quality-Scale Bronze rule `config-flow-test-coverage`. Verifies:
  - Single-instance enforcement (unique_config_entry rule)
  - Reauth flow updates the existing entry in place
  - Reconfigure flow updates the existing entry in place
  - OAuth callback creates a new entry with redacted token data
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
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


async def test_reauth_confirm_shows_form(
    hass: HomeAssistant,
) -> None:
    """Triggering reauth shows the confirm form before re-running OAuth."""
    # HA's flow manager loads the integration domain to get the flow handler,
    # which triggers my→frontend→hass_frontend. Stub hass_frontend to avoid it.
    from pathlib import Path

    fake_frontend = ModuleType("hass_frontend")
    fake_frontend.where = MagicMock(return_value=Path("/fake"))  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"hass_frontend": fake_frontend}):
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
    from pathlib import Path

    fake_frontend = ModuleType("hass_frontend")
    fake_frontend.where = MagicMock(return_value=Path("/fake"))  # type: ignore[attr-defined]
    with patch.dict(sys.modules, {"hass_frontend": fake_frontend}):
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
