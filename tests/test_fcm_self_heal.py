"""Coverage tests for `async_self_heal_fcm_push` (FCM watchdog recovery)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_coord(
    *, entry_data: dict | None = None, enable_fcm: bool = True
) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord._entry = SimpleNamespace(data=dict(entry_data or {}))
    coord.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    coord.options = {"enable_fcm_push": enable_fcm}
    coord._fcm_start_lock = asyncio.Lock()
    return coord


def _mark_creds_stale() -> None:
    """Push a fresh staleness timestamp so the 2-stage decision routes to
    the HARD-heal branch — matches the path the original tests cover."""
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())


def _clear_staleness() -> None:
    """Make sure the soft-heal branch is taken by clearing any prior markers."""
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


@pytest.fixture(autouse=True)
def _reset_fcm_filter_state():
    """Class-level shared lists in _FCMNoiseFilter persist between tests when
    pytest collects them in the same process — a stale timestamp from a
    prior test can route a soft-heal scenario to hard-heal (or vice versa)
    and produce mysterious flakiness under pytest-randomly. Clear both lists
    at the start of every test so order-dependence is impossible."""
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS.clear()
    yield
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS.clear()


@pytest.mark.asyncio
class TestFcmSelfHealHard:
    """Hard-heal path: triggered when creds are missing OR proven stale."""

    async def test_purges_creds_and_restarts(self):
        """Stale creds (PHONE_REGISTRATION_ERROR seen recently) → hard purge + restart."""
        from custom_components.bosch_shc_camera import fcm

        _mark_creds_stale()
        coord = _make_coord(
            entry_data={
                "fcm_credentials": {"gcm": "x"},
                "fcm_registered_token": "stale-token",
                "bearer_token": "bt",  # non-fcm key must survive
            }
        )
        stop_mock = AsyncMock()
        start_mock = AsyncMock()
        reset_mock = MagicMock()
        with (
            patch.object(fcm, "async_stop_fcm_push", stop_mock),
            patch.object(fcm, "_async_start_fcm_push_locked", start_mock),
            patch.object(fcm, "reset_fcm_error_counter", reset_mock),
        ):
            await fcm.async_self_heal_fcm_push(coord)
        stop_mock.assert_awaited_once_with(coord)
        coord.hass.config_entries.async_update_entry.assert_called_once()
        new_data = coord.hass.config_entries.async_update_entry.call_args.kwargs.get(
            "data"
        )
        assert "fcm_credentials" not in new_data
        assert "fcm_registered_token" not in new_data
        assert new_data["bearer_token"] == "bt", "non-fcm keys must survive the purge"
        reset_mock.assert_called_once()
        start_mock.assert_awaited_once_with(coord)

    async def test_purges_all_fcm_prefix_keys(self):
        """Live bug 2026-05-21: leaving fcm_config and fcm_registered_device_type
        behind kept the integration in a PHONE_REGISTRATION_ERROR loop. Hard-heal
        must purge EVERY key beginning with 'fcm_', not just the two originally
        hardcoded ones."""
        from custom_components.bosch_shc_camera import fcm

        _mark_creds_stale()
        coord = _make_coord(
            entry_data={
                "fcm_credentials": {"gcm": "x"},
                "fcm_registered_token": "tok",
                "fcm_config": {"project_id": "p", "app_id": "a", "api_key": "k"},
                "fcm_registered_device_type": "ANDROID",
                "fcm_anything_future": "should_also_go",
                "bearer_token": "bt",
                "refresh_token": "rt",
            }
        )
        with (
            patch.object(fcm, "async_stop_fcm_push", AsyncMock()),
            patch.object(fcm, "_async_start_fcm_push_locked", AsyncMock()),
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            await fcm.async_self_heal_fcm_push(coord)
        new_data = coord.hass.config_entries.async_update_entry.call_args.kwargs.get(
            "data"
        )
        # All fcm_* keys must be gone:
        for k in (
            "fcm_credentials",
            "fcm_registered_token",
            "fcm_config",
            "fcm_registered_device_type",
            "fcm_anything_future",
        ):
            assert k not in new_data, f"{k!r} must be purged by self-heal"
        # Non-fcm keys must survive:
        assert new_data["bearer_token"] == "bt"
        assert new_data["refresh_token"] == "rt"

    async def test_does_not_restart_when_fcm_disabled(self):
        from custom_components.bosch_shc_camera import fcm

        _mark_creds_stale()
        coord = _make_coord(
            entry_data={"fcm_credentials": {"gcm": "x"}},
            enable_fcm=False,
        )
        stop_mock = AsyncMock()
        start_mock = AsyncMock()
        with (
            patch.object(fcm, "async_stop_fcm_push", stop_mock),
            patch.object(fcm, "_async_start_fcm_push_locked", start_mock),
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            await fcm.async_self_heal_fcm_push(coord)
        stop_mock.assert_awaited_once()
        start_mock.assert_not_awaited()

    async def test_works_when_no_creds_present(self):
        """No persisted credentials → hard-heal path (only option). Must be
        idempotent — calling on a freshly-installed integration without
        stored creds must not raise."""
        from custom_components.bosch_shc_camera import fcm

        _clear_staleness()
        coord = _make_coord(entry_data=None)
        with (
            patch.object(fcm, "async_stop_fcm_push", AsyncMock()),
            patch.object(fcm, "_async_start_fcm_push_locked", AsyncMock()),
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            # Must not raise.
            await fcm.async_self_heal_fcm_push(coord)

    async def test_lock_serialises_concurrent_starts(self):
        """Live bug 2026-05-21: setup-time start + watchdog self-heal raced and
        registered two device tokens in 2 s. The shared lock must collapse
        concurrent callers: the second observes `_fcm_running=True` and returns
        without a second checkin_or_register()."""
        from custom_components.bosch_shc_camera import fcm

        coord = _make_coord(
            entry_data={
                "fcm_config": {"api_key": "k", "project_id": "p", "app_id": "a"}
            }
        )
        # Simulate a coordinator that has not started FCM yet.
        coord._fcm_running = False
        coord.options = {"enable_fcm_push": True}

        # Replace `_async_start_fcm_push_locked` with a stub that sets _fcm_running=True
        # the moment it runs — mimicking what the real function does on success.
        call_count = {"n": 0}

        async def fake_locked(c):
            # Mirror the real `_async_start_fcm_push_locked` guard so the
            # second caller (after the lock releases) observes the running
            # flag and returns without re-registering.
            if c._fcm_running:
                return
            call_count["n"] += 1
            # Yield to the event loop so the lock release/re-acquire actually
            # happens between the two callers.
            await asyncio.sleep(0)
            c._fcm_running = True

        with patch.object(fcm, "_async_start_fcm_push_locked", side_effect=fake_locked):
            # Two concurrent starts — only the first should actually register.
            await asyncio.gather(
                fcm.async_start_fcm_push(coord),
                fcm.async_start_fcm_push(coord),
            )

        assert call_count["n"] == 1, (
            "Lock failed to serialise: both concurrent callers ran the registration path. "
            "This is the live bug from 2026-05-21 where two device tokens got registered."
        )


@pytest.mark.asyncio
class TestFcmSelfHealSoft:
    """Soft-heal path: triggered when creds are present AND no recent
    PHONE_REGISTRATION_ERROR markers. The whole point: avoid forcing
    gcm_register() (which is what triggers PHONE_REGISTRATION_ERROR in the
    upstream library)."""

    async def test_soft_heal_preserves_credentials(self):
        """Listener died (no staleness markers) but creds look valid →
        soft-heal MUST NOT purge entry.data. Library's checkin_or_register()
        will hit the cheap gcm_check_in() path and reconnect.
        Regression for: 2026-05-24 cascade where purging valid creds forced
        gcm_register() into Google's transient PHONE_REGISTRATION_ERROR."""
        from custom_components.bosch_shc_camera import fcm

        _clear_staleness()
        coord = _make_coord(
            entry_data={
                "fcm_credentials": {"gcm": "valid"},
                "fcm_registered_token": "tok",
                "fcm_config": {"project_id": "p", "app_id": "a", "api_key": "k"},
                "bearer_token": "bt",
            }
        )

        async def fake_start(c):
            c._fcm_running = True  # mimic successful soft restart

        with (
            patch.object(fcm, "async_stop_fcm_push", AsyncMock()),
            patch.object(
                fcm, "_async_start_fcm_push_locked", AsyncMock(side_effect=fake_start)
            ),
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            await fcm.async_self_heal_fcm_push(coord)

        # entry.data MUST be untouched — soft-heal preserves all creds.
        coord.hass.config_entries.async_update_entry.assert_not_called()
        assert coord._entry.data["fcm_credentials"] == {"gcm": "valid"}
        assert coord._entry.data["fcm_registered_token"] == "tok"
        assert coord._entry.data["fcm_config"] == {
            "project_id": "p",
            "app_id": "a",
            "api_key": "k",
        }

    async def test_soft_heal_escalates_to_hard_when_restart_fails(self):
        """If soft-heal's start_locked returns without _fcm_running=True,
        escalate to hard-heal in the same call so the watchdog's failure
        counter accurately tracks one heal attempt."""
        from custom_components.bosch_shc_camera import fcm

        _clear_staleness()
        coord = _make_coord(
            entry_data={
                "fcm_credentials": {"gcm": "looksValid"},
                "fcm_registered_token": "tok",
                "bearer_token": "bt",
            }
        )

        # start_locked returns without setting _fcm_running → soft-heal fails
        async def fake_start_fails(c):
            pass  # leave _fcm_running unset (effectively False)

        coord._fcm_running = False

        with (
            patch.object(fcm, "async_stop_fcm_push", AsyncMock()),
            patch.object(
                fcm,
                "_async_start_fcm_push_locked",
                AsyncMock(side_effect=fake_start_fails),
            ),
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            await fcm.async_self_heal_fcm_push(coord)

        # Escalation: entry.data should have been updated (hard-heal purge).
        coord.hass.config_entries.async_update_entry.assert_called_once()
        new_data = coord.hass.config_entries.async_update_entry.call_args.kwargs.get(
            "data"
        )
        assert "fcm_credentials" not in new_data
        assert "fcm_registered_token" not in new_data
        assert new_data["bearer_token"] == "bt"

    async def test_staleness_marker_forces_hard_heal_even_with_creds(self):
        """When recent log shows PHONE_REGISTRATION_ERROR-class marker, creds
        are proven stale (Google rejected them). Even though they're persisted,
        hard-heal MUST run because soft refresh would fail the same way."""
        from custom_components.bosch_shc_camera import fcm

        _mark_creds_stale()
        coord = _make_coord(
            entry_data={
                "fcm_credentials": {"gcm": "rejectedByGoogle"},
                "bearer_token": "bt",
            }
        )
        with (
            patch.object(fcm, "async_stop_fcm_push", AsyncMock()),
            patch.object(fcm, "_async_start_fcm_push_locked", AsyncMock()),
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            await fcm.async_self_heal_fcm_push(coord)

        # Hard-heal ran: entry.data updated with creds removed.
        coord.hass.config_entries.async_update_entry.assert_called_once()
        new_data = coord.hass.config_entries.async_update_entry.call_args.kwargs.get(
            "data"
        )
        assert "fcm_credentials" not in new_data

    async def test_soft_heal_resets_failure_counter_on_success(self):
        """Successful soft-heal short-circuits the watchdog's 10-min stability
        window — immediately reset the failure counter so the next outage
        starts at cool-down ladder index 0."""
        from custom_components.bosch_shc_camera import fcm

        _clear_staleness()
        coord = _make_coord(
            entry_data={
                "fcm_credentials": {"gcm": "valid"},
            }
        )
        coord._fcm_self_heal_failures = 3
        coord._fcm_self_heal_paused_logged = True

        async def fake_start(c):
            c._fcm_running = True

        with (
            patch.object(fcm, "async_stop_fcm_push", AsyncMock()),
            patch.object(
                fcm, "_async_start_fcm_push_locked", AsyncMock(side_effect=fake_start)
            ),
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            await fcm.async_self_heal_fcm_push(coord)

        assert coord._fcm_self_heal_failures == 0
        assert coord._fcm_self_heal_paused_logged is False

    async def test_get_recent_fcm_creds_staleness_count(self):
        """Helper must count only staleness-marker timestamps within window."""
        from custom_components.bosch_shc_camera import fcm
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        now = time.monotonic()
        # 2 recent (within 600s), 1 old (700s ago)
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.extend(
            [now - 700, now - 100, now - 10]
        )
        assert fcm.get_recent_fcm_creds_staleness_count(600.0) == 2

    async def test_soft_heal_disabled_fcm_does_not_purge_creds(self):
        """When `enable_fcm_push=False`, soft-heal must stop the client and
        return — NEVER escalate to hard-heal (which would purge valid creds).

        Pre-fix bug: after stop, `_fcm_running` stays False because we skip
        the start; the escalation check then wrongly fell through to
        hard-heal and purged credentials of a user who explicitly disabled
        FCM (and may re-enable it later). Production watchdog guards this
        today via its own `enable_fcm_push` gate, but the heal function must
        be correct in isolation."""
        from custom_components.bosch_shc_camera import fcm

        coord = _make_coord(
            entry_data={
                "fcm_credentials": {"gcm": "valid"},
                "fcm_registered_token": "tok",
                "bearer_token": "bt",
            },
            enable_fcm=False,
        )
        coord._fcm_running = False  # disabled means stopped

        with (
            patch.object(fcm, "async_stop_fcm_push", AsyncMock()) as stop_mock,
            patch.object(
                fcm, "_async_start_fcm_push_locked", AsyncMock()
            ) as start_mock,
            patch.object(fcm, "reset_fcm_error_counter", MagicMock()),
        ):
            await fcm.async_self_heal_fcm_push(coord)

        stop_mock.assert_awaited_once()
        start_mock.assert_not_awaited()
        # CRITICAL: entry.data must be UNTOUCHED — no purge.
        coord.hass.config_entries.async_update_entry.assert_not_called()
        assert coord._entry.data["fcm_credentials"] == {"gcm": "valid"}
        assert coord._entry.data["fcm_registered_token"] == "tok"

    async def test_reset_fcm_error_counter_clears_both_lists(self):
        """Reset must clear BOTH error timestamps and staleness timestamps —
        previously only the generic list was cleared, leaving staleness
        markers to incorrectly force hard-heal on the next outage."""
        from custom_components.bosch_shc_camera import fcm
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS.extend([time.monotonic()])
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.extend([time.monotonic()])
        fcm.reset_fcm_error_counter()
        assert _FCMNoiseFilter._SHARED_ERROR_TIMESTAMPS == []
        assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []
