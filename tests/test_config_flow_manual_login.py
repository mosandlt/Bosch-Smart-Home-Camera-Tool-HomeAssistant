"""Regression tests for the manual-login fallback in the initial config flow.

Bug source: Bosch Smart Home Community private message from SebastianHarder
to mosandlt, "HA: SingleKey ID Login", received 2026-07-05. The reporter
could not complete SingleKey ID login on either the HA Companion mobile app
or a Mac browser — after a chain of OAuth redirects he ended up in the
wrong/last-opened browser tab, with the config flow toggling between a blank
screen, a brief success message, and the setup error on repeated
back-navigation. Prior to this fix, the only login path for a *fresh* setup
was the automatic browser redirect (via my.home-assistant.io); the existing
manual copy/paste fallback was only reachable through the options flow's
"force_relogin" checkbox on an *already-configured* entry, so an affected
user had no way to complete initial setup at all.

Fix: `async_step_user` now shows a menu ("auto_login" / "manual_login")
instead of unconditionally delegating to the automatic OAuth2 flow. This
covers the new `async_step_auto_login`, `async_step_manual_login`, and
`async_step_manual_paste` steps on `BoschCameraConfigFlow`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries

MODULE = "custom_components.bosch_shc_camera.config_flow"


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
