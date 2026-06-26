"""Regression tests for issue #36 — Gen2 Eyes Outdoor II motion stuck on 'Clear'.

Root causes (all confirmed against the code, see CLAUDE.md analysis 2026-06-21):

  A. The FCM watchdog used only `FcmPushClient.is_started()` (socket liveness)
     as the health signal. Google/Bosch can stop DELIVERING to a token while
     the socket still reports started → `_fcm_healthy` stayed True forever and
     no heal ever fired. Fix: the periodic /v11/events poll is ground truth —
     a genuinely new event with no real push in FCM_DELIVERY_DEAD_AFTER_SEC
     flags `_fcm_force_hard_heal`, and `async_self_heal_fcm_push` short-circuits
     to a HARD heal (purge + re-register) regardless of socket state.

  B. `register_fcm_with_bosch` skipped the POST /v11/devices forever while the
     token was unchanged → a server-side-dropped Bosch registration never
     re-announced. Fix: re-POST when the persisted `fcm_registered_at` is older
     than FCM_REREGISTER_INTERVAL_SEC, and stamp `fcm_registered_at` on success.

  C. With push dead the poll IS the detection path, but it ran at the relaxed
     `interval_events` (300 s) behind a 90 s motion window → polled events aged
     out before the binary sensor could see them. Fix: poll at
     FCM_DOWN_EVENT_POLL_SEC when not healthy. Guarded here by the invariant
     FCM_DOWN_EVENT_POLL_SEC < DEFAULT_MOTION_ACTIVE_WINDOW.

  D. Gen2 cameras report a human as eventType=MOVEMENT + eventTags=["PERSON"];
     the raw event dict is never rewritten, so the Person sensor (matching only
     eventType=="PERSON") stayed OFF. Fix: `_get_latest_person_event` accepts a
     PERSON-tagged MOVEMENT event.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.fcm"
CAM_ID = "11111111-1111-1111-1111-111111111111"


def _ago_iso(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )


# ─────────────────────────────────────────────────────────────────────────────
# D — Person sensor must accept a PERSON-tagged MOVEMENT event (Gen2 DualRadar)
# ─────────────────────────────────────────────────────────────────────────────


def _make_person_sensor(events: list[dict]):
    from custom_components.bosch_shc_camera.binary_sensor import (
        BoschPersonDetectedBinarySensor,
    )

    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Eingang",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.102",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {},
                },
                "events": events,
            }
        },
    )
    entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
    return BoschPersonDetectedBinarySensor(coord, CAM_ID, entry)


class TestPersonSensorTagUpgrade:
    def test_gen2_movement_with_person_tag_is_on(self) -> None:
        """Issue #36: Gen2 sends MOVEMENT+eventTags=[PERSON]; Person sensor ON."""
        sensor = _make_person_sensor(
            [
                {
                    "id": "e1",
                    "eventType": "MOVEMENT",
                    "eventTags": ["PERSON"],
                    "timestamp": _ago_iso(5),
                }
            ]
        )
        assert sensor.is_on is True

    def test_explicit_person_event_is_on(self) -> None:
        sensor = _make_person_sensor(
            [{"id": "e1", "eventType": "PERSON", "timestamp": _ago_iso(5)}]
        )
        assert sensor.is_on is True

    def test_movement_without_person_tag_is_off(self) -> None:
        """A plain MOVEMENT (no PERSON tag) must NOT flip the Person sensor."""
        sensor = _make_person_sensor(
            [
                {
                    "id": "e1",
                    "eventType": "MOVEMENT",
                    "eventTags": ["ANIMAL"],
                    "timestamp": _ago_iso(5),
                }
            ]
        )
        assert sensor.is_on is False

    def test_person_tagged_movement_outside_window_is_off(self) -> None:
        sensor = _make_person_sensor(
            [
                {
                    "id": "e1",
                    "eventType": "MOVEMENT",
                    "eventTags": ["PERSON"],
                    "timestamp": _ago_iso(10_000),
                }
            ]
        )
        assert sensor.is_on is False

    def test_person_attrs_use_tagged_event(self) -> None:
        sensor = _make_person_sensor(
            [
                {
                    "id": "evt-person",
                    "eventType": "MOVEMENT",
                    "eventTags": ["PERSON"],
                    "timestamp": _ago_iso(5),
                }
            ]
        )
        assert sensor.extra_state_attributes.get("event_id") == "evt-person"

    def test_no_events_is_off(self) -> None:
        sensor = _make_person_sensor([])
        assert sensor.is_on is False


# ─────────────────────────────────────────────────────────────────────────────
# B — periodic re-registration with Bosch CBS
# ─────────────────────────────────────────────────────────────────────────────


def _make_register_coord(data: dict) -> SimpleNamespace:
    update_calls: list[dict] = []

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        update_calls.append(dict(kwargs))
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry),
    )
    coord = SimpleNamespace(
        token="bearer-abc",
        _fcm_token="fcm-tok-X",
        _entry=SimpleNamespace(data=dict(data)),
        hass=hass,
    )
    coord._update_calls = update_calls  # type: ignore[attr-defined]
    return coord


def _resp_cm(status: int, body: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _session_post(resp_cm: MagicMock) -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(return_value=resp_cm)
    return session


@pytest.mark.asyncio
class TestPeriodicReRegistration:
    async def test_fresh_registration_skips_post(self) -> None:
        """Token unchanged + ANDROID + registered just now → skip the POST."""
        coord = _make_register_coord(
            {
                "fcm_registered_token": "fcm-tok-X",
                "fcm_registered_device_type": "ANDROID",
                "fcm_registered_at": time.time(),
            }
        )
        session = _session_post(_resp_cm(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

            result = await register_fcm_with_bosch(coord)
        assert result is True
        session.post.assert_not_called()

    async def test_stale_registration_reposts(self) -> None:
        """Token unchanged but fcm_registered_at older than the interval → re-POST
        and refresh the timestamp (issue #36: heal a dropped Bosch registration)."""
        from custom_components.bosch_shc_camera.fcm import (
            FCM_REREGISTER_INTERVAL_SEC,
            register_fcm_with_bosch,
        )

        coord = _make_register_coord(
            {
                "fcm_registered_token": "fcm-tok-X",
                "fcm_registered_device_type": "ANDROID",
                "fcm_registered_at": time.time() - FCM_REREGISTER_INTERVAL_SEC - 3600,
            }
        )
        session = _session_post(_resp_cm(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await register_fcm_with_bosch(coord)
        assert result is True
        session.post.assert_called_once()
        # timestamp refreshed on success
        last = coord._update_calls[-1]["data"]
        assert last["fcm_registered_at"] >= time.time() - 5

    async def test_malformed_registered_at_treated_as_stale(self) -> None:
        """A non-numeric fcm_registered_at must not crash — treat as stale (0.0)
        and re-POST so a corrupted stamp self-heals."""
        coord = _make_register_coord(
            {
                "fcm_registered_token": "fcm-tok-X",
                "fcm_registered_device_type": "ANDROID",
                "fcm_registered_at": "not-a-number",
            }
        )
        session = _session_post(_resp_cm(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

            assert await register_fcm_with_bosch(coord) is True
        session.post.assert_called_once()

    async def test_successful_post_stamps_registered_at(self) -> None:
        """Fresh install: a 204 must persist fcm_registered_at for the gate."""
        coord = _make_register_coord({})
        session = _session_post(_resp_cm(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

            assert await register_fcm_with_bosch(coord) is True
        last = coord._update_calls[-1]["data"]
        assert "fcm_registered_at" in last
        assert last["fcm_registered_token"] == "fcm-tok-X"


# ─────────────────────────────────────────────────────────────────────────────
# A — force-hard-heal short-circuit (delivery confirmed dead by polling)
# ─────────────────────────────────────────────────────────────────────────────


def _make_heal_coord(entry_data: dict, force_hard: bool = True) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord._entry = SimpleNamespace(data=dict(entry_data))
    coord.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    coord.options = {"enable_fcm_push": True}
    coord._fcm_start_lock = asyncio.Lock()
    coord._fcm_force_hard_heal = force_hard
    coord._fcm_last_push = float("-inf")
    coord._fcm_running = False
    coord._fcm_healthy = False
    return coord


@pytest.mark.asyncio
class TestForceHardHeal:
    async def test_supervisor_clears_force_hard_flag_and_purges_creds(self) -> None:
        """`_fcm_force_hard_heal=True` → supervisor purges fcm_* creds and clears flag."""
        from custom_components.bosch_shc_camera import fcm
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        coord = _make_heal_coord(
            {
                "fcm_credentials": {"gcm": "x"},
                "fcm_registered_token": "tok",
                "other": "y",
            },
            force_hard=True,
        )

        with (
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "reset_fcm_creds_staleness_counter"),
            patch.object(
                fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=False)
            ),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            await asyncio.sleep(
                0.05
            )  # let one iteration run (hard-heal + failed start)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert coord._fcm_force_hard_heal is False, (
            "_fcm_force_hard_heal must be cleared after supervisor hard-heal"
        )
        update_call = coord.hass.config_entries.async_update_entry.call_args
        assert update_call is not None, (
            "async_update_entry must be called during hard-heal"
        )
        new_data = (
            update_call.kwargs.get("data")
            or update_call[1].get("data")
            or update_call[0][1]
        )
        assert "fcm_credentials" not in new_data, (
            "fcm_* keys must be purged on hard-heal"
        )

    async def test_no_force_flag_skips_hard_heal(self) -> None:
        """Without _fcm_force_hard_heal=True and no staleness markers, supervisor
        takes the soft path — async_update_entry (cred-purge) is NOT called."""
        from custom_components.bosch_shc_camera import fcm
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        coord = _make_heal_coord(
            {"fcm_credentials": {"gcm": "x"}, "fcm_registered_token": "tok"},
            force_hard=False,
        )

        with (
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "get_recent_fcm_creds_staleness_count", return_value=0),
            patch.object(
                fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=False)
            ),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        (
            coord.hass.config_entries.async_update_entry.assert_not_called(),
            ("Soft path must not purge creds (async_update_entry must not be called)"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# C — poll cadence invariant: fast poll must be inside the motion window
# ─────────────────────────────────────────────────────────────────────────────


def test_fast_poll_is_inside_default_motion_window() -> None:
    """A polled event must be younger than the motion window when first seen,
    otherwise the motion sensor can never turn ON in polling-only mode (#36)."""
    import custom_components.bosch_shc_camera as init_mod
    from custom_components.bosch_shc_camera.const import DEFAULT_MOTION_ACTIVE_WINDOW

    assert init_mod.FCM_DOWN_EVENT_POLL_SEC < DEFAULT_MOTION_ACTIVE_WINDOW
