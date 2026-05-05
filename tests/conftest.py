"""Shared pytest fixtures for Bosch Smart Home Camera tests.

Uses pytest-homeassistant-custom-component (PHACC) which provides the
`hass` fixture and `MockConfigEntry` helper for HACS custom integrations.

Install with:
    pip install pytest pytest-homeassistant-custom-component pytest-asyncio
"""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera.const import DOMAIN

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading our custom integration in all tests."""
    yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Mock config entry with valid bearer + refresh tokens."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Bosch Smart Home Camera",
        data={
            "bearer_token": "test_bearer_token",
            "refresh_token": "test_refresh_token",
        },
        options={},
        unique_id=DOMAIN,
        version=1,
    )


@pytest.fixture
def mock_oauth_token() -> dict:
    """Token payload returned by the OAuth flow."""
    return {
        "access_token": "fresh_bearer_token",
        "refresh_token": "fresh_refresh_token",
        "expires_in": 1800,
        "token_type": "Bearer",
    }


@pytest.fixture
def mock_cloud_api_video_inputs() -> Generator[MagicMock, None, None]:
    """Mock the GET /v11/video_inputs endpoint."""
    with patch(
        "custom_components.bosch_shc_camera.shc.async_get_clientsession"
    ) as session:
        resp = MagicMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=[
            {
                "id": "11111111-2222-3333-4444-555555555555",
                "title": "Test Cam",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.25",
                "privacyMode": "OFF",
            }
        ])
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        session.return_value.get = MagicMock(return_value=ctx)
        yield session
