"""Tests for `_refresh_token_locked` — the OAuth refresh path.

Round-1B+ targeted the small wrappers; this file goes after the heart
of the auth layer. `_refresh_token_locked` is ~130 LOC with many
branches:

  - valid-token short-circuit (skip POST when another caller refreshed)
  - auth-server-outage cooldown gate (don't hammer a known-down server)
  - missing refresh_token → ConfigEntryAuthFailed (reauth needed)
  - RefreshTokenInvalidError (Keycloak invalid_grant) → ConfigEntryAuthFailed
  - AuthServerOutageError (5xx) → exponential backoff + UpdateFailed,
    repair-issue surfaced after 3 consecutive outages
  - successful refresh → persists access_token + refresh_token to entry,
    schedules next refresh, clears failure counters + repair issues
  - transient failure (returns None) → 2s retry up to 3 attempts;
    after 3 hard failures triggers ConfigEntryAuthFailed

Each branch is its own test. Stubs the aiohttp session +
`config_flow._do_refresh` so no real HTTP fires.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_coord(**overrides):
    """Coordinator stub with everything `_refresh_token_locked` reads."""

    def _create_task(coro):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-OLD", "refresh_token": "rfr-OLD"},
            options={},
        ),
        _refreshed_token=None,
        _refreshed_refresh=None,
        _auth_outage_count=0,
        _auth_outage_next_retry_ts=0.0,
        _auth_outage_alert_sent=False,
        _token_fail_count=0,
        _token_alert_sent=False,
        # _token_still_valid stub — defaults to False so we don't early-return
        _token_still_valid=lambda min_remaining=60: False,
        _schedule_token_refresh=MagicMock(),
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            config_entries=SimpleNamespace(async_update_entry=MagicMock()),
        ),
        debug=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Early returns ────────────────────────────────────────────────────────


class TestEarlyReturns:
    @pytest.mark.asyncio
    async def test_returns_existing_token_when_still_valid(self):
        """Another caller may have already refreshed while we waited on
        the lock. Skip the POST."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_token_still_valid=lambda min_remaining=60: True)
        coord.token = "still-valid-tok"
        out = await BoschCameraCoordinator._refresh_token_locked(coord)
        assert out == "still-valid-tok"

    @pytest.mark.asyncio
    async def test_outage_cooldown_raises_update_failed(self):
        """In the back-off window after an outage, raise UpdateFailed
        instead of POSTing — avoids hammering a known-down server."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.helpers.update_coordinator import UpdateFailed
        future = time.monotonic() + 60
        coord = _make_coord(
            _auth_outage_count=2,
            _auth_outage_next_retry_ts=future,
        )
        with pytest.raises(UpdateFailed) as exc:
            await BoschCameraCoordinator._refresh_token_locked(coord)
        assert "outage" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_no_refresh_token_raises_config_entry_auth_failed(self):
        """No refresh token at all → trigger HA's reauth flow."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.exceptions import ConfigEntryAuthFailed
        coord = _make_coord(
            _entry=SimpleNamespace(data={}, options={}),
        )
        with pytest.raises(ConfigEntryAuthFailed) as exc:
            await BoschCameraCoordinator._refresh_token_locked(coord)
        assert "re-authentication" in str(exc.value).lower()


# ── Hard-error branches ──────────────────────────────────────────────────


class TestHardErrors:
    @pytest.mark.asyncio
    async def test_invalid_grant_raises_reauth(self):
        """Keycloak's invalid_grant means the refresh token itself was
        rejected — no point retrying, ask the user to re-authenticate."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.config_flow import RefreshTokenInvalidError
        from homeassistant.exceptions import ConfigEntryAuthFailed
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(side_effect=RefreshTokenInvalidError("invalid_grant")),
        ):
            with pytest.raises(ConfigEntryAuthFailed) as exc:
                await BoschCameraCoordinator._refresh_token_locked(coord)
            assert "Reconfigure" in str(exc.value)

    @pytest.mark.asyncio
    async def test_auth_server_outage_backs_off_60s(self):
        """5xx from Keycloak = server outage → exponential back-off.
        First outage: 60s window."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError
        from homeassistant.helpers.update_coordinator import UpdateFailed
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(side_effect=AuthServerOutageError("502 Bad Gateway")),
        ):
            with pytest.raises(UpdateFailed):
                await BoschCameraCoordinator._refresh_token_locked(coord)
        assert coord._auth_outage_count == 1
        # Back-off ~60s; allow small drift
        diff = coord._auth_outage_next_retry_ts - time.monotonic()
        assert 55 < diff < 65

    @pytest.mark.asyncio
    async def test_auth_server_outage_doubles_backoff(self):
        """Each outage doubles the back-off, capped at 600s."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError
        from homeassistant.helpers.update_coordinator import UpdateFailed
        coord = _make_coord(_auth_outage_count=3)  # next backoff = 60 * 2^3 = 480
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(side_effect=AuthServerOutageError("503")),
        ), patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ):
            with pytest.raises(UpdateFailed):
                await BoschCameraCoordinator._refresh_token_locked(coord)
        assert coord._auth_outage_count == 4
        # Outage #4 → 60 * 2^3 = 480s
        diff = coord._auth_outage_next_retry_ts - time.monotonic()
        assert 470 < diff < 490

    @pytest.mark.asyncio
    async def test_auth_server_outage_caps_at_600s(self):
        """Even on outage #10, back-off must cap at 600s (10min)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError
        from homeassistant.helpers.update_coordinator import UpdateFailed
        coord = _make_coord(_auth_outage_count=10)  # 60 * 2^10 = 61440 > 600
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(side_effect=AuthServerOutageError("503")),
        ), patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ):
            with pytest.raises(UpdateFailed):
                await BoschCameraCoordinator._refresh_token_locked(coord)
        diff = coord._auth_outage_next_retry_ts - time.monotonic()
        assert 590 < diff < 610

    @pytest.mark.asyncio
    async def test_auth_server_outage_creates_repair_issue_after_3(self):
        """After 3 consecutive outages, surface a repair-issue so the
        user sees a clear explanation under Settings → Repairs."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError
        from homeassistant.helpers.update_coordinator import UpdateFailed
        coord = _make_coord(_auth_outage_count=2)  # next is #3
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(side_effect=AuthServerOutageError("503")),
        ), patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ) as create_issue:
            with pytest.raises(UpdateFailed):
                await BoschCameraCoordinator._refresh_token_locked(coord)
        create_issue.assert_called_once()
        assert coord._auth_outage_alert_sent is True

    @pytest.mark.asyncio
    async def test_repair_issue_only_created_once(self):
        """If `_auth_outage_alert_sent` is already True, don't re-create
        the same issue on every subsequent outage tick."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from custom_components.bosch_shc_camera.config_flow import AuthServerOutageError
        from homeassistant.helpers.update_coordinator import UpdateFailed
        coord = _make_coord(
            _auth_outage_count=5,
            _auth_outage_alert_sent=True,
        )
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(side_effect=AuthServerOutageError("503")),
        ), patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ) as create_issue:
            with pytest.raises(UpdateFailed):
                await BoschCameraCoordinator._refresh_token_locked(coord)
        create_issue.assert_not_called()


# ── Successful refresh ───────────────────────────────────────────────────


class TestSuccessfulRefresh:
    @pytest.mark.asyncio
    async def test_success_persists_new_tokens(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        new_tokens = {"access_token": "tok-NEW", "refresh_token": "rfr-NEW"}
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(return_value=new_tokens),
        ):
            out = await BoschCameraCoordinator._refresh_token_locked(coord)
        assert out == "tok-NEW"
        assert coord._refreshed_token == "tok-NEW"
        assert coord._refreshed_refresh == "rfr-NEW"
        # Entry was updated since both tokens changed
        coord.hass.config_entries.async_update_entry.assert_called_once()
        # Next proactive refresh scheduled
        coord._schedule_token_refresh.assert_called_once()

    @pytest.mark.asyncio
    async def test_success_no_persist_when_tokens_unchanged(self):
        """Some Keycloak setups return the same tokens (offline_access).
        Skip the persist call to avoid useless HA bus events."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        unchanged = {
            "access_token": coord._entry.data["bearer_token"],
            "refresh_token": coord._entry.data["refresh_token"],
        }
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(return_value=unchanged),
        ):
            await BoschCameraCoordinator._refresh_token_locked(coord)
        coord.hass.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_success_clears_outage_state(self):
        """A successful refresh after prior outages clears the outage
        counter + cooldown + dismisses the repair issue. Set the cooldown
        timestamp in the past so the early gate doesn't trigger before
        the success path can run."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _auth_outage_count=4,
            _auth_outage_next_retry_ts=time.monotonic() - 10,  # cooldown expired
            _auth_outage_alert_sent=True,
        )
        new_tokens = {"access_token": "tok-NEW", "refresh_token": "rfr-NEW"}
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(return_value=new_tokens),
        ), patch(
            "custom_components.bosch_shc_camera.ir.async_delete_issue",
        ) as del_issue:
            await BoschCameraCoordinator._refresh_token_locked(coord)
        assert coord._auth_outage_count == 0
        assert coord._auth_outage_next_retry_ts == 0.0
        assert coord._auth_outage_alert_sent is False
        # async_delete_issue called for both token_expired (no-op) and auth_server_outage
        assert del_issue.called

    @pytest.mark.asyncio
    async def test_success_clears_token_alert(self):
        """If a previous failure surfaced a repair issue, success
        dismisses it."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _token_fail_count=2,
            _token_alert_sent=True,
        )
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(return_value={"access_token": "x", "refresh_token": "y"}),
        ), patch(
            "custom_components.bosch_shc_camera.ir.async_delete_issue",
        ) as del_issue:
            await BoschCameraCoordinator._refresh_token_locked(coord)
        assert coord._token_fail_count == 0
        assert coord._token_alert_sent is False
        del_issue.assert_called()


# ── Transient retry path ─────────────────────────────────────────────────


class TestTransientRetry:
    @pytest.mark.asyncio
    async def test_retries_on_none_then_succeeds(self):
        """First two calls return None (transient), third succeeds → ok."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        responses = [None, None, {"access_token": "tok-N", "refresh_token": "rfr-N"}]
        do_refresh = AsyncMock(side_effect=responses)
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=do_refresh,
        ), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            coord = _make_coord()
            out = await BoschCameraCoordinator._refresh_token_locked(coord)
        assert out == "tok-N"
        assert do_refresh.await_count == 3

    @pytest.mark.asyncio
    async def test_three_none_returns_increments_fail_count_and_raises(self):
        """All 3 attempts return None → no exception caught, falls
        through to the post-loop branch: increment fail count, raise
        UpdateFailed (or ConfigEntryAuthFailed at fail_count >= 3)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.helpers.update_coordinator import UpdateFailed
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(return_value=None),
        ), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            coord = _make_coord(_token_fail_count=0)
            with pytest.raises(UpdateFailed):
                await BoschCameraCoordinator._refresh_token_locked(coord)
        assert coord._token_fail_count == 1

    @pytest.mark.asyncio
    async def test_third_consecutive_failure_triggers_reauth(self):
        """fail_count reaches 3 → ConfigEntryAuthFailed forcing user reauth."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        from homeassistant.exceptions import ConfigEntryAuthFailed
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.bosch_shc_camera.config_flow._do_refresh",
            new=AsyncMock(return_value=None),
        ), patch(
            "asyncio.sleep", new=AsyncMock(),
        ):
            coord = _make_coord(_token_fail_count=2)  # one more = 3
            with pytest.raises(ConfigEntryAuthFailed):
                await BoschCameraCoordinator._refresh_token_locked(coord)
        assert coord._token_fail_count == 3


# ── _async_token_failure_alert (1161-1188) ───────────────────────────────


class TestTokenFailureAlert:
    @pytest.mark.asyncio
    async def test_short_circuits_when_already_sent(self):
        """One alert per failure cycle — re-firing on every tick would
        spam the user. Pin so a refactor can't accidentally drop the
        idempotency check."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_token_alert_sent=True)
        with patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ) as create_issue:
            await BoschCameraCoordinator._async_token_failure_alert(coord, "token failed")
        create_issue.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_repair_issue_and_marks_sent(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._get_alert_services = MagicMock(return_value=[])
        with patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ) as create_issue:
            await BoschCameraCoordinator._async_token_failure_alert(coord, "token failed")
        create_issue.assert_called_once()
        assert coord._token_alert_sent is True

    @pytest.mark.asyncio
    async def test_calls_notify_services_when_available(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._get_alert_services = MagicMock(return_value=["notify.test_user"])
        coord.hass.services = SimpleNamespace(
            has_service=MagicMock(return_value=True),
            async_call=AsyncMock(),
        )
        with patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ):
            await BoschCameraCoordinator._async_token_failure_alert(coord, "msg")
        coord.hass.services.async_call.assert_awaited_once()
        call_args = coord.hass.services.async_call.await_args
        assert call_args[0][0] == "notify"
        assert call_args[0][1] == "test_user"

    @pytest.mark.asyncio
    async def test_skips_notify_service_not_registered(self):
        """Alert service was configured by user but the provider is
        currently unavailable (e.g. Signal addon stopped) → skip
        without crashing."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._get_alert_services = MagicMock(return_value=["notify.deleted"])
        coord.hass.services = SimpleNamespace(
            has_service=MagicMock(return_value=False),
            async_call=AsyncMock(),
        )
        with patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ):
            await BoschCameraCoordinator._async_token_failure_alert(coord, "msg")
        coord.hass.services.async_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_notify_call_exception(self):
        """notify service might raise — the alert helper must not
        propagate the exception."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._get_alert_services = MagicMock(return_value=["notify.test_user"])
        coord.hass.services = SimpleNamespace(
            has_service=MagicMock(return_value=True),
            async_call=AsyncMock(side_effect=RuntimeError("notify down")),
        )
        with patch(
            "custom_components.bosch_shc_camera.ir.async_create_issue",
        ):
            # Must NOT raise
            await BoschCameraCoordinator._async_token_failure_alert(coord, "msg")
