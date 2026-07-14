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


@pytest.fixture(autouse=True)
def auto_stub_application_credentials_import():
    """Stub async_import_client_credential for every test.

    Both __init__.py's async_setup() AND config_flow.py's
    BoschCameraConfigFlow.async_step_user() call the real
    homeassistant.components.application_credentials.async_import_client_credential
    (application_credentials port — see those modules' docstrings; the
    config_flow.py call site is load-bearing, not redundant: HA-core's
    _load_integration does NOT run a fresh install's own async_setup()
    before the config flow starts, so the credential import has to also
    happen from inside the flow itself, or first-time auto_login would abort
    with missing_credentials — found by the THREE_PER_ISSUE_PER_CHANGE
    bug-hunt during this port). That function requires the real
    application_credentials integration to have run its own async_setup
    first (it raises ValueError otherwise) — most unit tests here build a
    bare MagicMock `hass` without a real HA core instance, so the unpatched
    call would fail every one of them. Tests that specifically want to
    assert the import call happened (tests/test_init.py
    TestAsyncSetup::test_async_setup_imports_default_client_credential,
    tests/test_config_flow.py's TestAsyncStepUserShowsMenu import-related
    tests) apply their own inner patch on top of this one and inspect that
    instead.
    """
    with (
        patch(
            "custom_components.bosch_shc_camera.async_import_client_credential",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.bosch_shc_camera.config_flow.async_import_client_credential",
            new=AsyncMock(),
        ),
    ):
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
        resp.json = AsyncMock(
            return_value=[
                {
                    "id": "11111111-2222-3333-4444-555555555555",
                    "title": "Test Cam",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "privacyMode": "OFF",
                }
            ]
        )
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        session.return_value.get = MagicMock(return_value=ctx)
        yield session
