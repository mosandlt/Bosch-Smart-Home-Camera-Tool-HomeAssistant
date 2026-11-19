"""Entity states read from the camera's local REST interface.

Pins: per-field parsing of the settings endpoints (404 / wrong type / garbage
affect only their own fields; a rejected password or silence ends the batch),
the two-minute refresh with one batch in flight per camera, clearing on any
failure, the five-minute expiry, the hold after a cloud write, and per entity
group: the local value first, the cloud cache as fallback, and cameras
without a local password exactly as before.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.bosch_smart_camera import ldi_local, ldi_rest
from custom_components.bosch_smart_camera.ldi_local import (
    LDI_SETTINGS_MAX_AGE_SEC,
    LDI_SETTINGS_POLL_SEC,
    LDI_WRITE_HOLD_SEC,
    ldi_setting,
    merge_local_into_cfg,
    note_cloud_write,
    poll_ldi_local,
    refresh_ldi_settings,
)
from custom_components.bosch_smart_camera.number import (
    BoschAlarmActivationDelayNumber,
    BoschAlarmDelayNumber,
    BoschIntrusionDistanceNumber,
    BoschIntrusionSensitivityNumber,
    BoschPowerLedBrightnessNumber,
    BoschPreAlarmDelayNumber,
)
from custom_components.bosch_smart_camera.switch import (
    BoschAlarmModeSwitch,
    BoschAlarmSystemArmSwitch,
    BoschFireAlarmDetectionSwitch,
    BoschGlassBreakDetectionSwitch,
    BoschIntrusionDetectionSwitch,
    BoschPreAlarmSwitch,
    BoschPrivacySoundSwitch,
    BoschRecordSoundSwitch,
    BoschStatusLedSwitch,
)
from tests.test_ldi_local import CAM, IP, PW, _coord
from tests.test_ldi_rest import _patched, _Resp, _script, _Session

LDI = "custom_components.bosch_smart_camera.ldi_local"
USER = "localuser"

_FULL = {
    "/sh/data/statusLed": _Resp(200, '{"enable": true}'),
    "/sh/data/recording/audioRecording": _Resp(200, '{"enable": false}'),
    "/sh/data/alarm/audioDetection/config": _Resp(
        200, '{"detectGlassBreak": true, "detectFireAlarm": false}'
    ),
    "/sh/data/intrusionSystem/config": _Resp(
        200,
        '{"enabled": false, "sensitivity": 0, "detectionMode": "ALL", "distance": 5}',
    ),
    "/sh/data/alarm/config": _Resp(
        200,
        '{"enable": true, "enablePreAlarm": false, "alarmActivationDelay": 0.0,'
        ' "alarmDelay": 60.0, "preAlarmDelay": 30.5, "other": 1}',
    ),
    "/sh/data/privacyMode/jingle": _Resp(200, '{"enable": false}'),
    "/sh/data/lighting/iconLed/brightness": _Resp(200, '{"value": 0}'),
    "/sh/data/lighting/motion": _Resp(
        200,
        '{"enable": false, "uptime": 60, "top": {"brightness": 0},'
        ' "zoneMap": [], "sensitivity": 3}',
    ),
    "/sh/data/lighting/ambient": _Resp(
        200,
        '{"enable": true, "trigger": "ENVIRONMENT", "onTime": "18:00:00+0200"}',
    ),
    "/sh/data/lighting/config": _Resp(
        200, '{"environmentalBrightnessThreshold": 0.67, "fadingEnabled": true}'
    ),
    "/sh/data/audio/volumeLevel": _Resp(200, '{"volumeLevel": 50}'),
    "/sh/data/calibration/extrinsic": _Resp(200, '{"elevation": 1.85}'),
}

_ALL_PATHS = [path for path, _ in ldi_rest.SETTINGS_ENDPOINTS]


async def _read(reply: Any, skip: frozenset[str] = frozenset()) -> ldi_rest.LdiSettings:
    p1, p2, _ = _patched(_Session(reply))
    with p1, p2:
        return await ldi_rest.get_settings(MagicMock(), IP, USER, PW, skip=skip)


def _values(batch: ldi_rest.LdiSettings) -> dict[str, Any]:
    return {k: f.value for k, f in batch.fields.items() if f.status == "ok"}


# ── ldi_rest.get_settings ───────────────────────────────────────────────────
class TestGetSettings:
    @pytest.mark.asyncio
    async def test_every_endpoint_once_in_order_and_false_zero_are_values(
        self,
    ) -> None:
        reply = _script(**_FULL)
        batch = await _read(reply)
        assert batch.result == ldi_rest.PROBE_OK
        assert reply.asked == _ALL_PATHS
        assert "/sh/data/intrusionSystem/arming" not in reply.asked
        assert _values(batch) == {
            "status_led": True,
            "record_sound": False,
            "glass_break": True,
            "fire_alarm": False,
            "intrusion_enabled": False,
            "intrusion_sensitivity": 0,
            "intrusion_distance": 5,
            "alarm_enable": True,
            "pre_alarm_enable": False,
            "alarm_delay": 60.0,
            "alarm_activation_delay": 0.0,
            "pre_alarm_delay": 30.5,
            "privacy_sound": False,
            "icon_led_brightness": 0,
            "motion_light_enabled": False,
            "motion_light_sensitivity": 3,
            "ambient_light_enabled": True,
            "darkness_threshold": 0.67,
            "light_fading": True,
            "speaker_level": 50,
            "lens_elevation": 1.85,
        }
        assert batch.missing_paths == frozenset()

    @pytest.mark.asyncio
    async def test_404_affects_only_its_own_fields(self) -> None:
        by_path = dict(_FULL)
        by_path["/sh/data/alarm/config"] = _Resp(404)
        by_path["/sh/data/privacyMode/jingle"] = _Resp(404)
        batch = await _read(_script(**by_path))
        assert batch.result == ldi_rest.PROBE_OK
        assert batch.fields["alarm_enable"].status == ldi_rest.FIELD_UNAVAILABLE
        assert batch.fields["privacy_sound"].status == ldi_rest.FIELD_UNAVAILABLE
        assert batch.fields["status_led"] == ldi_rest.LdiSetting("ok", True)
        assert batch.missing_paths == frozenset(
            {"/sh/data/alarm/config", "/sh/data/privacyMode/jingle"}
        )

    @pytest.mark.asyncio
    async def test_other_http_error_marks_fields_error(self) -> None:
        by_path = dict(_FULL)
        by_path["/sh/data/statusLed"] = _Resp(500)
        batch = await _read(_script(**by_path))
        assert batch.result == ldi_rest.PROBE_OK
        assert batch.fields["status_led"].status == ldi_rest.FIELD_ERROR
        assert batch.missing_paths == frozenset()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            "",
            "not json",
            "[]",
            '"enable"',
            "null",
            '{"enable": "true"}',
            '{"enable": 1}',
            '{"enable": null}',
            '{"other": true}',
        ],
    )
    async def test_garbage_bool_body_is_invalid(self, body: str) -> None:
        by_path = dict(_FULL)
        by_path["/sh/data/statusLed"] = _Resp(200, body)
        batch = await _read(_script(**by_path))
        assert batch.fields["status_led"].status == ldi_rest.FIELD_INVALID
        assert batch.fields["record_sound"].status == ldi_rest.FIELD_OK

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("3", 3), ("true", None), ("3.5", None), ('"3"', None), ("null", None)],
    )
    async def test_int_field_types(self, raw: str, expected: int | None) -> None:
        by_path = dict(_FULL)
        by_path["/sh/data/lighting/iconLed/brightness"] = _Resp(
            200, f'{{"value": {raw}}}'
        )
        field = (await _read(_script(**by_path))).fields["icon_led_brightness"]
        assert field.value == expected
        assert field.status == ("ok" if expected is not None else "invalid")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("12", 12), ("12.5", 12.5), ("false", None), ("NaN", None), ('"1"', None)],
    )
    async def test_number_field_types(self, raw: str, expected: float | None) -> None:
        by_path = dict(_FULL)
        by_path["/sh/data/alarm/config"] = _Resp(200, f'{{"alarmDelay": {raw}}}')
        batch = await _read(_script(**by_path))
        assert batch.fields["alarm_delay"].value == expected
        # missing siblings in the same body are invalid, not errors
        assert batch.fields["alarm_enable"].status == ldi_rest.FIELD_INVALID

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_rejected_password_ends_the_batch(self, status: int) -> None:
        by_path = dict(_FULL)
        by_path["/sh/data/recording/audioRecording"] = _Resp(status)
        reply = _script(**by_path)
        batch = await _read(reply)
        assert batch == ldi_rest.LdiSettings(ldi_rest.PROBE_AUTH, {})
        assert reply.asked == _ALL_PATHS[:2]

    @pytest.mark.asyncio
    async def test_silence_ends_the_batch(self) -> None:
        asked: list[str] = []

        def reply(url: str) -> Any:
            asked.append(url)
            if len(asked) == 2:
                raise aiohttp.ClientConnectionError("down")
            return _FULL[url.removeprefix(f"https://{IP}")]

        batch = await _read(reply)
        assert batch == ldi_rest.LdiSettings(ldi_rest.PROBE_UNREACHABLE, {})
        assert len(asked) == 2

    @pytest.mark.asyncio
    async def test_overall_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ldi_rest, "LDI_SETTINGS_DEADLINE", 0.05)

        async def slow_get(*_a: Any, **_k: Any) -> ldi_rest.LdiRestResponse:
            await asyncio.sleep(1)
            return ldi_rest.LdiRestResponse(200, {"enable": True})

        with patch.object(ldi_rest, "_get", new=slow_get):
            batch = await ldi_rest.get_settings(MagicMock(), IP, USER, PW)
        assert batch.result == ldi_rest.PROBE_UNREACHABLE and batch.fields == {}

    @pytest.mark.asyncio
    async def test_skipped_paths_are_not_asked(self) -> None:
        reply = _script(**_FULL)
        batch = await _read(reply, skip=frozenset({"/sh/data/alarm/config"}))
        assert "/sh/data/alarm/config" not in reply.asked
        assert batch.fields["alarm_delay"].status == ldi_rest.FIELD_UNAVAILABLE
        assert batch.missing_paths == frozenset()

    @pytest.mark.asyncio
    async def test_get_only_no_redirects(self) -> None:
        session = _Session(_script(**_FULL))
        p1, p2, _ = _patched(session)
        with p1, p2:
            await ldi_rest.get_settings(MagicMock(), IP, USER, PW)
        assert all(kw["allow_redirects"] is False for _, kw in session.calls)


# ── ldi_local.refresh_ldi_settings ──────────────────────────────────────────
def _ok_batch(**fields: Any) -> ldi_rest.LdiSettings:
    return ldi_rest.LdiSettings(
        ldi_rest.PROBE_OK,
        {k: ldi_rest.LdiSetting("ok", v) for k, v in fields.items()},
    )


def _probed(c: SimpleNamespace, result: str = ldi_rest.PROBE_OK) -> SimpleNamespace:
    ldi_local._rest_entry(c, CAM)["result"] = result  # type: ignore[arg-type]
    return c


def _patched_settings(batch: ldi_rest.LdiSettings) -> Any:
    return patch(f"{LDI}.ldi_rest.get_settings", new=AsyncMock(return_value=batch))


class TestRefresh:
    @pytest.mark.asyncio
    async def test_stores_values_and_notifies(self) -> None:
        c = _probed(_coord())
        with _patched_settings(_ok_batch(status_led=False)) as read:
            assert await refresh_ldi_settings(c, CAM) is True  # type: ignore[arg-type]
        read.assert_awaited_once_with(c.hass, IP, USER, PW, skip=frozenset())
        assert ldi_setting(c, CAM, "status_led") is False  # type: ignore[arg-type]
        c.async_update_listeners.assert_called_once()

    @pytest.mark.asyncio
    async def test_at_most_every_two_minutes(self) -> None:
        c = _probed(_coord())
        with _patched_settings(_ok_batch(status_led=True)) as read:
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
            assert await refresh_ldi_settings(c, CAM) is False  # type: ignore[arg-type]
            c.ldi_rest_state[CAM]["settings_checked_at"] -= LDI_SETTINGS_POLL_SEC
            assert await refresh_ldi_settings(c, CAM) is True  # type: ignore[arg-type]
        assert read.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result",
        [ldi_rest.PROBE_AUTH, ldi_rest.PROBE_UNREACHABLE, ldi_rest.PROBE_ERROR, None],
    )
    async def test_only_after_a_good_probe(self, result: str | None) -> None:
        c = _probed(_coord(), result)  # type: ignore[arg-type]
        with _patched_settings(_ok_batch()) as read:
            assert await refresh_ldi_settings(c, CAM) is False  # type: ignore[arg-type]
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_overlapping_batch(self) -> None:
        c = _probed(_coord())
        gate = asyncio.Event()
        calls = 0

        async def slow(*_a: Any, **_k: Any) -> ldi_rest.LdiSettings:
            nonlocal calls
            calls += 1
            await gate.wait()
            return _ok_batch(status_led=True)

        with patch(f"{LDI}.ldi_rest.get_settings", new=slow):
            first = asyncio.create_task(refresh_ldi_settings(c, CAM))  # type: ignore[arg-type]
            await asyncio.sleep(0)
            assert c.ldi_rest_state[CAM]["settings_busy"] is True
            # forced, and even with the stamp due again: still no second batch
            c.ldi_rest_state[CAM]["settings_checked_at"] = float("-inf")
            assert await refresh_ldi_settings(c, CAM, force=True) is False  # type: ignore[arg-type]
            gate.set()
            assert await first is True
        assert calls == 1
        assert c.ldi_rest_state[CAM]["settings_busy"] is False

    @pytest.mark.asyncio
    async def test_batch_does_not_revive_settings_after_probe_failure(self) -> None:
        c = _probed(_coord())

        async def read(*_a: Any, **_k: Any) -> ldi_rest.LdiSettings:
            c.ldi_rest_state[CAM]["result"] = ldi_rest.PROBE_UNREACHABLE
            return _ok_batch(status_led=True)

        with patch(f"{LDI}.ldi_rest.get_settings", new=read):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_regular_poll_in_hold_keeps_the_scheduled_reread(self) -> None:
        c = _probed(_coord())
        note_cloud_write(c, CAM, "ledlights")  # type: ignore[arg-type]
        due = c.ldi_rest_state[CAM]["settings_due"]
        c.ldi_rest_state[CAM]["settings_checked_at"] = float("-inf")
        with _patched_settings(_ok_batch()):
            assert await refresh_ldi_settings(c, CAM) is True  # type: ignore[arg-type]
        assert c.ldi_rest_state[CAM]["settings_due"] == due

    @pytest.mark.asyncio
    async def test_stamp_taken_before_the_request(self) -> None:
        c = _probed(_coord())
        seen: list[float] = []

        async def read(*_a: Any, **_k: Any) -> ldi_rest.LdiSettings:
            seen.append(c.ldi_rest_state[CAM]["settings_checked_at"])
            return _ok_batch()

        with patch(f"{LDI}.ldi_rest.get_settings", new=read):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        assert seen[0] > float("-inf")

    @pytest.mark.asyncio
    async def test_busy_flag_released_on_error(self) -> None:
        c = _probed(_coord())
        with (
            patch(
                f"{LDI}.ldi_rest.get_settings",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            pytest.raises(RuntimeError),
        ):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        assert c.ldi_rest_state[CAM]["settings_busy"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "result", [ldi_rest.PROBE_AUTH, ldi_rest.PROBE_UNREACHABLE]
    )
    async def test_failed_batch_clears_and_notifies(self, result: str) -> None:
        c = _probed(_coord())
        with _patched_settings(_ok_batch(status_led=True)):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        c.async_update_listeners.reset_mock()
        with _patched_settings(ldi_rest.LdiSettings(result, {})):
            await refresh_ldi_settings(c, CAM, force=True)  # type: ignore[arg-type]
        assert c.ldi_rest_state[CAM]["settings"] is None
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]
        c.async_update_listeners.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [None, 401, 500])
    async def test_failed_probe_clears(self, status: int | None) -> None:
        from tests.test_ldi_local import _probe

        c = _probed(_coord())
        with _patched_settings(_ok_batch(status_led=True)):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        ldi_local._apply_probe(c, CAM, _probe(status))  # type: ignore[arg-type]
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_missing_source_clears(self) -> None:
        c = _probed(_coord())
        with _patched_settings(_ok_batch(status_led=True)):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        c.get_cam_lan_ip = lambda _cid: None
        with _patched_settings(_ok_batch()) as read:
            assert await refresh_ldi_settings(c, CAM, force=True) is False  # type: ignore[arg-type]
        read.assert_not_awaited()
        assert c.ldi_rest_state[CAM]["settings"] is None

    @pytest.mark.asyncio
    async def test_404_paths_skipped_for_an_hour_then_asked_again(self) -> None:
        c = _probed(_coord())
        batch = ldi_rest.LdiSettings(
            ldi_rest.PROBE_OK, {}, frozenset({"/sh/data/alarm/config"})
        )
        with _patched_settings(batch):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        with _patched_settings(_ok_batch()) as read:
            await refresh_ldi_settings(c, CAM, force=True)  # type: ignore[arg-type]
        assert read.await_args.kwargs["skip"] == frozenset({"/sh/data/alarm/config"})
        missing = c.ldi_rest_state[CAM]["settings_missing"]
        missing["/sh/data/alarm/config"] -= 3600.0
        with _patched_settings(_ok_batch()) as read:
            await refresh_ldi_settings(c, CAM, force=True)  # type: ignore[arg-type]
        assert read.await_args.kwargs["skip"] == frozenset()
        assert "/sh/data/alarm/config" not in missing  # answered this time

    @pytest.mark.asyncio
    async def test_poll_runs_probe_then_settings(self) -> None:
        c = _coord()
        order: list[str] = []
        with (
            patch(
                f"{LDI}.refresh_ldi_rest",
                new=AsyncMock(side_effect=lambda *_a, **_k: order.append("probe")),
            ),
            patch(
                f"{LDI}.refresh_ldi_settings",
                new=AsyncMock(side_effect=lambda *_a, **_k: order.append("settings")),
            ),
        ):
            await poll_ldi_local(c, CAM)  # type: ignore[arg-type]
        assert order == ["probe", "settings"]


# ── ldi_setting: expiry, password gate, write hold ──────────────────────────
def _with_settings(
    c: SimpleNamespace, *, age: float = 0.0, **fields: Any
) -> SimpleNamespace:
    entry = ldi_local._rest_entry(c, CAM)  # type: ignore[arg-type]
    entry["settings"] = {"at": time.monotonic() - age, "fields": fields}
    return c


class TestLdiSetting:
    def test_fresh_value(self) -> None:
        c = _with_settings(_coord(), intrusion_sensitivity=0)
        assert ldi_setting(c, CAM, "intrusion_sensitivity") == 0  # type: ignore[arg-type]

    def test_stale_value_is_ignored(self) -> None:
        c = _with_settings(_coord(), age=LDI_SETTINGS_MAX_AGE_SEC + 1, status_led=True)
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    def test_unknown_key(self) -> None:
        c = _with_settings(_coord(), status_led=True)
        assert ldi_setting(c, CAM, "record_sound") is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "kwargs",
        [{"passwords": "absent"}, {"passwords": {CAM: "  "}}, {"state": "inactive"}],
    )
    def test_no_password_means_no_local_value(self, kwargs: dict[str, Any]) -> None:
        c = _with_settings(_coord(**kwargs), status_led=True)
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "store",
        [
            None,
            "x",
            {CAM: None},
            {CAM: {"settings": "x"}},
            {CAM: {"settings": {"at": "x"}}},
        ],
    )
    def test_garbage_store(self, store: Any) -> None:
        c = _coord()
        c.ldi_rest_state = store
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    def test_magicmock_coordinator(self) -> None:
        assert ldi_setting(MagicMock(), CAM, "status_led") is None  # type: ignore[arg-type]

    def test_write_holds_back_the_local_value(self) -> None:
        c = _with_settings(_coord(), status_led=True, record_sound=True)
        note_cloud_write(c, CAM, "ledlights")  # type: ignore[arg-type]
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]
        # other fields are not affected
        assert ldi_setting(c, CAM, "record_sound") is True  # type: ignore[arg-type]

    def test_hold_over_but_read_older_than_write(self) -> None:
        c = _with_settings(_coord(), age=LDI_WRITE_HOLD_SEC + 5, status_led=True)
        entry = c.ldi_rest_state[CAM]
        entry["writes"] = {"status_led": time.monotonic() - LDI_WRITE_HOLD_SEC - 1}
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    def test_read_after_the_hold_is_used(self) -> None:
        c = _with_settings(_coord(), status_led=False)
        entry = c.ldi_rest_state[CAM]
        entry["writes"] = {"status_led": time.monotonic() - LDI_WRITE_HOLD_SEC - 2}
        entry["settings"]["at"] = time.monotonic() - 1
        assert ldi_setting(c, CAM, "status_led") is False  # type: ignore[arg-type]

    def test_read_taken_inside_the_hold_stays_unused_after_it(self) -> None:
        """A poll during the hold may still carry the pre-write value."""
        c = _with_settings(_coord(), status_led=False)
        entry = c.ldi_rest_state[CAM]
        now = time.monotonic()
        entry["writes"] = {"status_led": now - LDI_WRITE_HOLD_SEC - 1}
        entry["settings"]["at"] = now - LDI_WRITE_HOLD_SEC + 4
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    def test_write_schedules_a_read_right_after_the_hold(self) -> None:
        c = _with_settings(_coord())
        before = time.monotonic()
        note_cloud_write(c, CAM, "alarm_settings")  # type: ignore[arg-type]
        entry = c.ldi_rest_state[CAM]
        assert before + LDI_WRITE_HOLD_SEC < entry["settings_due"]
        assert entry["settings_due"] <= time.monotonic() + LDI_WRITE_HOLD_SEC + 1
        assert set(entry["writes"]) == {
            "alarm_enable",
            "pre_alarm_enable",
            "alarm_delay",
            "alarm_activation_delay",
            "pre_alarm_delay",
        }

    @pytest.mark.asyncio
    async def test_due_read_runs_before_two_minutes(self) -> None:
        c = _probed(_coord())
        with _patched_settings(_ok_batch()):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        c.ldi_rest_state[CAM]["settings_due"] = time.monotonic() - 1
        with _patched_settings(_ok_batch()) as read:
            assert await refresh_ldi_settings(c, CAM) is True  # type: ignore[arg-type]
        read.assert_awaited_once()
        assert c.ldi_rest_state[CAM]["settings_due"] == float("inf")

    @pytest.mark.parametrize("endpoint", ["timestamp", "notifications", "motion"])
    def test_unrelated_write_holds_nothing(self, endpoint: str) -> None:
        c = _with_settings(_coord(), status_led=True)
        note_cloud_write(c, CAM, endpoint)  # type: ignore[arg-type]
        assert "writes" not in c.ldi_rest_state[CAM]

    def test_write_note_on_bare_stub_never_raises(self) -> None:
        note_cloud_write(SimpleNamespace(), CAM, "ledlights")  # type: ignore[arg-type]
        note_cloud_write(MagicMock(), CAM, "ledlights")  # type: ignore[arg-type]


class TestCloudWriteHook:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("success", [True, False])
    async def test_put_camera_notes_successful_writes(self, success: bool) -> None:
        from custom_components.bosch_smart_camera import BoschCameraCoordinator

        coord = SimpleNamespace(hass=MagicMock())
        with (
            patch(
                "custom_components.bosch_smart_camera.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "custom_components.bosch_smart_camera.coordinator._put_camera_via_lib",
                new=AsyncMock(return_value=success),
            ),
            patch(
                "custom_components.bosch_smart_camera.coordinator.note_cloud_write"
            ) as note,
        ):
            result = await BoschCameraCoordinator.async_put_camera(
                coord,  # type: ignore[arg-type]
                CAM,
                "ledlights",
                {"state": "ON"},
            )
        assert result is success
        if success:
            note.assert_called_once_with(coord, CAM, "ledlights")
        else:
            note.assert_not_called()


# ── entities: local first, cloud fallback ───────────────────────────────────
def _entity_coord(**kwargs: Any) -> SimpleNamespace:
    c = _coord(**kwargs)
    c.data = {
        CAM: {
            "info": {
                "title": "Cam",
                "hardwareVersion": "HOME_Eyes_Indoor",
                "firmwareVersion": "9.40.203",
                "macAddress": "aa:bb:cc:dd:ee:01",
            },
            "status": "ONLINE",
        }
    }
    c.last_update_success = True
    c.is_camera_online = lambda _cid: True
    c.ledlights_cache = {CAM: False}
    c.ledlights_set_at = {}
    c.privacy_sound_cache = {CAM: True}
    c.privacy_sound_set_at = {}
    c.arming_cache = {CAM: False}
    c.arming_set_at = {}
    c.alarm_status_cache = {}
    c.recording_options = lambda _cid: {"recordSound": True}
    c.intrusion_config_cache = {CAM: {"enabled": True, "sensitivity": 5, "distance": 2}}
    c.audio_detection_cache = {
        CAM: {"detectGlassBreak": False, "detectFireAlarm": True}
    }
    c.alarm_settings_cache = {
        CAM: {
            "alarmMode": "OFF",
            "preAlarmMode": "ON",
            "alarmDelayInSeconds": 70,
            "alarmActivationDelaySeconds": 10,
            "preAlarmDelayInSeconds": 35,
        }
    }
    c.icon_led_brightness_cache = {CAM: 3}
    return c


_ENTRY = SimpleNamespace(entry_id="01ENTRY", data={}, options={})

# (entity class, local key, local value, expected with it, cloud-cache value)
_SWITCH_CASES = [
    (BoschStatusLedSwitch, "status_led", True, True, False),
    (BoschPrivacySoundSwitch, "privacy_sound", False, False, True),
    (BoschRecordSoundSwitch, "record_sound", False, False, True),
    (BoschIntrusionDetectionSwitch, "intrusion_enabled", False, False, True),
    (BoschGlassBreakDetectionSwitch, "glass_break", True, True, False),
    (BoschFireAlarmDetectionSwitch, "fire_alarm", False, False, True),
    (BoschAlarmModeSwitch, "alarm_enable", True, True, False),
    (BoschPreAlarmSwitch, "pre_alarm_enable", False, False, True),
]

_NUMBER_CASES = [
    (BoschIntrusionSensitivityNumber, "intrusion_sensitivity", 0, 0.0, 5.0),
    (BoschIntrusionDistanceNumber, "intrusion_distance", 8, 8.0, 2.0),
    (BoschAlarmDelayNumber, "alarm_delay", 60.0, 60.0, 70.0),
    (BoschAlarmActivationDelayNumber, "alarm_activation_delay", 0.0, 0.0, 10.0),
    (BoschPreAlarmDelayNumber, "pre_alarm_delay", 30.5, 30.5, 35.0),
    (BoschPowerLedBrightnessNumber, "icon_led_brightness", 0, 0.0, 3.0),
]


class TestEntities:
    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _SWITCH_CASES)
    def test_switch_local_first(
        self, cls: Any, key: str, local: bool, want: bool, cloud: bool
    ) -> None:
        c = _with_settings(_entity_coord(), **{key: local})
        assert cls(c, CAM, _ENTRY).is_on is want

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _SWITCH_CASES)
    def test_switch_cloud_fallback_without_local_value(
        self, cls: Any, key: str, local: bool, want: bool, cloud: bool
    ) -> None:
        assert cls(_entity_coord(), CAM, _ENTRY).is_on is cloud

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _SWITCH_CASES)
    def test_switch_without_password_unchanged(
        self, cls: Any, key: str, local: bool, want: bool, cloud: bool
    ) -> None:
        c = _with_settings(_entity_coord(passwords="absent"), **{key: local})
        assert cls(c, CAM, _ENTRY).is_on is cloud

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _SWITCH_CASES)
    def test_switch_held_after_cloud_write(
        self, cls: Any, key: str, local: bool, want: bool, cloud: bool
    ) -> None:
        c = _with_settings(_entity_coord(), **{key: local})
        c.ldi_rest_state[CAM]["writes"] = {key: time.monotonic()}
        assert cls(c, CAM, _ENTRY).is_on is cloud

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _NUMBER_CASES)
    def test_number_local_first(
        self, cls: Any, key: str, local: float, want: float, cloud: float
    ) -> None:
        c = _with_settings(_entity_coord(), **{key: local})
        assert cls(c, CAM, _ENTRY).native_value == want

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _NUMBER_CASES)
    def test_number_cloud_fallback(
        self, cls: Any, key: str, local: float, want: float, cloud: float
    ) -> None:
        c = _with_settings(
            _entity_coord(), age=LDI_SETTINGS_MAX_AGE_SEC + 1, **{key: local}
        )
        assert cls(c, CAM, _ENTRY).native_value == cloud

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _NUMBER_CASES)
    def test_number_without_password_unchanged(
        self, cls: Any, key: str, local: float, want: float, cloud: float
    ) -> None:
        c = _with_settings(_entity_coord(passwords="absent"), **{key: local})
        assert cls(c, CAM, _ENTRY).native_value == cloud

    @pytest.mark.parametrize(
        ("cls", "key", "bad"),
        [
            (BoschPowerLedBrightnessNumber, "icon_led_brightness", 50),
            (BoschIntrusionDistanceNumber, "intrusion_distance", 0),
            (BoschIntrusionSensitivityNumber, "intrusion_sensitivity", 9),
            (BoschAlarmDelayNumber, "alarm_delay", 5000.0),
            (BoschAlarmDelayNumber, "alarm_delay", True),
        ],
    )
    def test_number_out_of_range_local_value_falls_back(
        self, cls: Any, key: str, bad: Any
    ) -> None:
        c = _with_settings(_entity_coord(), **{key: bad})
        entity = cls(c, CAM, _ENTRY)
        cloud = _with_settings(_entity_coord())
        assert entity.native_value == cls(cloud, CAM, _ENTRY).native_value

    def test_switch_ignores_non_bool_local_value(self) -> None:
        c = _with_settings(_entity_coord(), status_led=1)
        assert BoschStatusLedSwitch(c, CAM, _ENTRY).is_on is False

    def test_status_led_available_from_local_value_alone(self) -> None:
        c = _with_settings(_entity_coord(), status_led=True)
        c.ledlights_cache = {}
        sw = BoschStatusLedSwitch(c, CAM, _ENTRY)
        assert sw.available is True and sw.is_on is True
        c.ldi_rest_state[CAM]["settings"] = None
        assert sw.available is False

    def test_status_led_still_needs_the_cloud_tick(self) -> None:
        c = _with_settings(_entity_coord(), status_led=True)
        c.last_update_success = False
        assert BoschStatusLedSwitch(c, CAM, _ENTRY).available is False

    def test_arm_switch_stays_on_the_cloud_state(self) -> None:
        """The local `arm` is the wanted state; the switch shows the system state."""
        c = _with_settings(_entity_coord(), arm=True, alarm_arm=True)
        assert BoschAlarmSystemArmSwitch(c, CAM, _ENTRY).is_on is False

    @pytest.mark.asyncio
    async def test_status_led_write_stays_on_the_cloud(self) -> None:
        c = _with_settings(_entity_coord(), status_led=False)
        c.async_put_camera = AsyncMock(return_value=True)
        sw = BoschStatusLedSwitch(c, CAM, _ENTRY)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_on()
        c.async_put_camera.assert_awaited_once_with(CAM, "ledlights", {"state": "ON"})
        assert c.ledlights_cache[CAM] is True
        # the stub skips the coordinator hook, so note the write as it would
        note_cloud_write(c, CAM, "ledlights")  # type: ignore[arg-type]
        assert sw.is_on is True  # optimistic cloud value, stale local held back


class TestDefensive:
    def test_garbage_fields_container(self) -> None:
        c = _with_settings(_coord())
        c.ldi_rest_state[CAM]["settings"]["fields"] = ["status_led"]
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]

    def test_garbage_write_record_never_raises(self) -> None:
        c = _with_settings(_coord(), status_led=True)
        c.ldi_rest_state[CAM]["writes"] = "x"
        note_cloud_write(c, CAM, "ledlights")  # type: ignore[arg-type]
        assert c.ldi_rest_state[CAM]["writes"] == "x"

    def test_number_with_unmapped_field_uses_the_cloud(self) -> None:
        c = _with_settings(_entity_coord(), alarm_delay=60.0)
        entity = BoschAlarmDelayNumber(c, CAM, _ENTRY)
        entity._field = "unmappedField"
        c.alarm_settings_cache[CAM]["unmappedField"] = 12
        assert entity.native_value == 12.0


class TestMergeLocalIntoCfg:
    """Whole-block cloud writes must not push a stale sibling field."""

    def test_local_sibling_replaces_stale_cloud_value(self) -> None:
        c = _with_settings(_coord(), glass_break=False, fire_alarm=True)
        cfg = {"detectGlassBreak": False, "detectFireAlarm": False}
        out = merge_local_into_cfg(c, CAM, cfg, "audioDetectionConfig")  # type: ignore[arg-type]
        assert out == {"detectGlassBreak": False, "detectFireAlarm": True}
        assert cfg == {"detectGlassBreak": False, "detectFireAlarm": False}

    def test_alarm_on_off_strings_and_int_delays(self) -> None:
        c = _with_settings(
            _coord(), alarm_enable=True, pre_alarm_enable=False, alarm_delay=52.0
        )
        cfg = {"alarmMode": "OFF", "preAlarmMode": "ON", "alarmDelayInSeconds": 10}
        out = merge_local_into_cfg(c, CAM, cfg, "alarm_settings")  # type: ignore[arg-type]
        assert out == {
            "alarmMode": "ON",
            "preAlarmMode": "OFF",
            "alarmDelayInSeconds": 52,
        }
        assert isinstance(out["alarmDelayInSeconds"], int)

    def test_no_local_value_keeps_cloud_cfg(self) -> None:
        c = _coord()
        cfg = {"enabled": True, "sensitivity": 3, "distance": 8, "detectionMode": "X"}
        assert merge_local_into_cfg(c, CAM, cfg, "intrusionDetectionConfig") == cfg  # type: ignore[arg-type]

    def test_unknown_endpoint_and_missing_field_untouched(self) -> None:
        c = _with_settings(_coord(), glass_break=True)
        assert merge_local_into_cfg(c, CAM, {"a": 1}, "nope") == {"a": 1}  # type: ignore[arg-type]
        assert merge_local_into_cfg(c, CAM, {}, "audioDetectionConfig") == {}  # type: ignore[arg-type]

    def test_held_field_is_not_merged(self) -> None:
        c = _with_settings(_coord(), glass_break=True)
        note_cloud_write(c, CAM, "audioDetectionConfig")  # type: ignore[arg-type]
        cfg = {"detectGlassBreak": False}
        assert merge_local_into_cfg(c, CAM, cfg, "audioDetectionConfig") == cfg  # type: ignore[arg-type]


class TestLateFixes:
    def test_out_of_range_local_number_is_not_merged(self) -> None:
        c = _with_settings(_coord(), intrusion_distance=0, intrusion_sensitivity=3)
        cfg = {"distance": 8, "sensitivity": 1}
        out = merge_local_into_cfg(c, CAM, cfg, "intrusionDetectionConfig")  # type: ignore[arg-type]
        assert out == {"distance": 8, "sensitivity": 3}

    def test_newest_write_sets_the_reread(self) -> None:
        c = _with_settings(_coord())
        note_cloud_write(c, CAM, "ledlights")  # type: ignore[arg-type]
        entry = c.ldi_rest_state[CAM]
        entry["settings_due"] = time.monotonic() + 1  # earlier, from a past write
        note_cloud_write(c, CAM, "ledlights")  # type: ignore[arg-type]
        assert entry["settings_due"] > time.monotonic() + LDI_WRITE_HOLD_SEC

    @pytest.mark.asyncio
    async def test_failed_then_good_probe_during_batch_drops_the_batch(self) -> None:
        c = _probed(_coord())

        async def read(*_a: Any, **_k: Any) -> ldi_rest.LdiSettings:
            entry = c.ldi_rest_state[CAM]
            entry["probe_fail_seq"] = entry.get("probe_fail_seq", 0) + 1
            entry["result"] = ldi_rest.PROBE_OK
            return _ok_batch(status_led=True)

        with patch(f"{LDI}.ldi_rest.get_settings", new=read):
            await refresh_ldi_settings(c, CAM)  # type: ignore[arg-type]
        assert ldi_setting(c, CAM, "status_led") is None  # type: ignore[arg-type]
