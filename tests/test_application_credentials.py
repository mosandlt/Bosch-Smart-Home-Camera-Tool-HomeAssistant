"""Tests for custom_components/bosch_shc_camera/application_credentials.py.

Covers the `async_get_auth_implementation` platform hook that
homeassistant.components.application_credentials calls to build the OAuth2
implementation for this integration's single (auto-imported) ClientCredential
— see that module's docstring and __init__.py's async_setup for the full
Core-submission-prep rationale (this integration ports to
application_credentials without requiring end users to visit Settings ->
Application Credentials, since Bosch's OAuth client is a fixed public secret
embedded in every Android APK, not a per-user credential).
"""

from unittest.mock import MagicMock

import pytest


class TestAsyncGetAuthImplementation:
    @pytest.mark.asyncio
    async def test_returns_bosch_oauth2_implementation(self) -> None:
        from homeassistant.components.application_credentials import ClientCredential

        from custom_components.bosch_shc_camera.application_credentials import (
            async_get_auth_implementation,
        )
        from custom_components.bosch_shc_camera.config_flow import (
            BoschOAuth2Implementation,
        )

        hass = MagicMock()
        credential = ClientCredential(
            client_id="fake_client_id", client_secret="fake_client_secret"
        )
        impl = await async_get_auth_implementation(
            hass, "bosch_shc_camera.fake_client_id", credential
        )
        assert isinstance(impl, BoschOAuth2Implementation)

    @pytest.mark.asyncio
    async def test_implementation_uses_credential_client_id_and_secret(self) -> None:
        """The implementation must be built FROM the ClientCredential's own
        client_id/client_secret, not the module-level defaults — otherwise
        an admin override via Settings -> Application Credentials would be
        silently ignored (see application_credentials.py docstring)."""
        from homeassistant.components.application_credentials import ClientCredential

        from custom_components.bosch_shc_camera.application_credentials import (
            async_get_auth_implementation,
        )

        hass = MagicMock()
        credential = ClientCredential(
            client_id="override_client_id", client_secret="override_client_secret"
        )
        impl = await async_get_auth_implementation(
            hass, "bosch_shc_camera.override_client_id", credential
        )
        assert impl._client_id == "override_client_id"
        assert impl._client_secret == "override_client_secret"

    @pytest.mark.asyncio
    async def test_implementation_receives_hass(self) -> None:
        from homeassistant.components.application_credentials import ClientCredential

        from custom_components.bosch_shc_camera.application_credentials import (
            async_get_auth_implementation,
        )

        hass = MagicMock()
        credential = ClientCredential(client_id="cid", client_secret="csecret")
        impl = await async_get_auth_implementation(hass, "auth_domain", credential)
        assert impl.hass is hass

    @pytest.mark.asyncio
    async def test_auth_domain_param_is_accepted_and_ignored(self) -> None:
        """`auth_domain` is part of the ApplicationCredentialsProtocol contract
        (HA-core passes it so a platform CAN key multiple credentials
        differently) but this integration only ever has one credential, so
        it is intentionally unused — verify the signature still accepts it
        without erroring for any auth_domain value."""
        from homeassistant.components.application_credentials import ClientCredential

        from custom_components.bosch_shc_camera.application_credentials import (
            async_get_auth_implementation,
        )

        hass = MagicMock()
        credential = ClientCredential(client_id="cid", client_secret="csecret")
        for auth_domain in ("bosch_shc_camera.cid", "anything_else", ""):
            impl = await async_get_auth_implementation(hass, auth_domain, credential)
            assert impl._client_id == "cid"
