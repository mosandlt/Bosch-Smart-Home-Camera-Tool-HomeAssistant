"""Settings entities stay available on the camera's local value (v18.2.0-beta-3).

Pins: with the cloud copy missing (privacy mode answers 443, cloud outage) an
entity is available when the camera answered locally with a valid, fresh,
not-held value and its last local probe succeeded; it is not available for a
camera the local path knows offline or locked out, nor without a local
password (unchanged). Writers with no cloud copy never send a partial body:
privacy on -> the visible warning and no PUT, privacy off -> skipped. The
whole-block lighting PUTs serialize per camera and endpoint. A settings read
cut short by its deadline keeps the fields it already got.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_smart_camera import ldi_rest
from custom_components.bosch_smart_camera.ldi_local import (
    LDI_SETTINGS_MAX_AGE_SEC,
    RESULT_AUTH,
    RESULT_NO_GO2RTC,
    RESULT_UNREACHABLE,
    ldi_probe_ok,
    ldi_setting,
    refresh_ldi_settings,
)
from custom_components.bosch_smart_camera.number import (
    BoschAlarmActivationDelayNumber,
    BoschAlarmDelayNumber,
    BoschDarknessThresholdNumber,
    BoschIntrusionDistanceNumber,
    BoschIntrusionSensitivityNumber,
    BoschLensElevationNumber,
    BoschMotionLightSensitivityNumber,
    BoschPowerLedBrightnessNumber,
    BoschPreAlarmDelayNumber,
    BoschSpeakerLevelNumber,
)
from custom_components.bosch_smart_camera.select import BoschDetectionModeSelect
from custom_components.bosch_smart_camera.switch import (
    BoschAlarmModeSwitch,
    BoschAmbientLightSwitch,
    BoschFireAlarmDetectionSwitch,
    BoschGlassBreakDetectionSwitch,
    BoschIntrusionDetectionSwitch,
    BoschMotionLightSwitch,
    BoschPreAlarmSwitch,
    BoschSoftLightFadingSwitch,
)
from tests.test_ldi_local import CAM, IP, PW
from tests.test_ldi_settings import (
    _ENTRY,
    _ok_batch,
    _patched_settings,
    _probed,
    _with_settings,
)
from tests.test_ldi_settings_lighting import _light_coord

SW = "custom_components.bosch_smart_camera.switch"

# (entity class, local key, local value, expected state, cloud cache attr)
_SWITCHES = [
    (
        BoschIntrusionDetectionSwitch,
        "intrusion_enabled",
        False,
        False,
        "intrusion_config_cache",
    ),
    (
        BoschGlassBreakDetectionSwitch,
        "glass_break",
        True,
        True,
        "audio_detection_cache",
    ),
    (
        BoschFireAlarmDetectionSwitch,
        "fire_alarm",
        False,
        False,
        "audio_detection_cache",
    ),
    (BoschAlarmModeSwitch, "alarm_enable", True, True, "alarm_settings_cache"),
    (BoschPreAlarmSwitch, "pre_alarm_enable", False, False, "alarm_settings_cache"),
    (BoschSoftLightFadingSwitch, "light_fading", True, True, "global_lighting_cache"),
    (
        BoschMotionLightSwitch,
        "motion_light_enabled",
        False,
        False,
        "motion_light_cache",
    ),
    (
        BoschAmbientLightSwitch,
        "ambient_light_enabled",
        True,
        True,
        "ambient_lighting_cache",
    ),
]

_NUMBERS = [
    (
        BoschIntrusionSensitivityNumber,
        "intrusion_sensitivity",
        3,
        3.0,
        "intrusion_config_cache",
    ),
    (
        BoschIntrusionDistanceNumber,
        "intrusion_distance",
        5,
        5.0,
        "intrusion_config_cache",
    ),
    (BoschAlarmDelayNumber, "alarm_delay", 60.0, 60.0, "alarm_settings_cache"),
    (
        BoschAlarmActivationDelayNumber,
        "alarm_activation_delay",
        0.0,
        0.0,
        "alarm_settings_cache",
    ),
    (BoschPreAlarmDelayNumber, "pre_alarm_delay", 30.5, 30.5, "alarm_settings_cache"),
    (
        BoschPowerLedBrightnessNumber,
        "icon_led_brightness",
        2,
        2.0,
        "icon_led_brightness_cache",
    ),
    (
        BoschMotionLightSensitivityNumber,
        "motion_light_sensitivity",
        3,
        3.0,
        "motion_light_cache",
    ),
    (
        BoschDarknessThresholdNumber,
        "darkness_threshold",
        0.67,
        67.0,
        "global_lighting_cache",
    ),
    (BoschSpeakerLevelNumber, "speaker_level", 50, 50.0, "audio_cache"),
    (BoschLensElevationNumber, "lens_elevation", 1.85, 1.85, "lens_elevation_cache"),
]

_ALL = [(cls, key, local, want, attr) for cls, key, local, want, attr in _SWITCHES] + [
    (cls, key, local, want, attr) for cls, key, local, want, attr in _NUMBERS
]
_IDS = [case[0].__name__ for case in _ALL]


def _state(entity: Any) -> Any:
    return entity.is_on if hasattr(entity, "is_on") else entity.native_value


def _no_cloud(attr: str, **kwargs: Any) -> SimpleNamespace:
    """Probed camera whose cloud copy of the entity's block is missing."""
    c = _probed(_light_coord(**kwargs))
    setattr(c, attr, {})
    return c


# ── availability ────────────────────────────────────────────────────────────
class TestAvailability:
    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_local_value_alone(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        entity = cls(_with_settings(_no_cloud(attr), **{key: local}), CAM, _ENTRY)
        assert entity.available is True
        assert _state(entity) == want

    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_local_value_during_cloud_outage(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        c = _with_settings(_no_cloud(attr), **{key: local})
        c.last_update_success = False
        c.is_camera_online = lambda _cid: False
        assert cls(c, CAM, _ENTRY).available is True

    @pytest.mark.parametrize(
        ("cls", "key", "local", "want", "attr"),
        [
            case
            for case in _ALL
            if case[0] not in (BoschMotionLightSwitch, BoschAmbientLightSwitch)
        ],
    )
    def test_neither_local_nor_cloud(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        assert cls(_with_settings(_no_cloud(attr)), CAM, _ENTRY).available is False

    @pytest.mark.parametrize(
        ("cls", "key", "local", "want", "attr"),
        [
            case
            for case in _ALL
            if case[0] in (BoschMotionLightSwitch, BoschAmbientLightSwitch)
        ],
    )
    def test_ungated_lights_need_the_cloud_tick_without_local(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        c = _with_settings(_no_cloud(attr))
        assert cls(c, CAM, _ENTRY).available is True  # unchanged: no cache gate
        c.last_update_success = False
        assert cls(c, CAM, _ENTRY).available is False

    @pytest.mark.parametrize(
        "probe", [ldi_rest.PROBE_UNREACHABLE, ldi_rest.PROBE_AUTH, ldi_rest.PROBE_ERROR]
    )
    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_failed_probe(
        self, cls: Any, key: str, local: Any, want: Any, attr: str, probe: str
    ) -> None:
        c = _with_settings(_no_cloud(attr), **{key: local})
        c.ldi_rest_state[CAM]["result"] = probe
        c.last_update_success = False
        assert cls(c, CAM, _ENTRY).available is False

    @pytest.mark.parametrize("reason", [RESULT_UNREACHABLE, RESULT_AUTH])
    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_known_offline_or_locked_out(
        self, cls: Any, key: str, local: Any, want: Any, attr: str, reason: str
    ) -> None:
        c = _with_settings(_no_cloud(attr), **{key: local})
        c.ldi_open_status[CAM] = {"reason": reason, "since": 0.0}
        c.last_update_success = False
        assert cls(c, CAM, _ENTRY).available is False

    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_never_probed(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        c = _with_settings(_light_coord(), **{key: local})
        setattr(c, attr, {})
        c.last_update_success = False
        assert cls(c, CAM, _ENTRY).available is False

    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_stale_value(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        c = _with_settings(
            _no_cloud(attr), age=LDI_SETTINGS_MAX_AGE_SEC + 1, **{key: local}
        )
        c.last_update_success = False
        assert cls(c, CAM, _ENTRY).available is False

    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_held_by_a_cloud_write(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        import time

        c = _with_settings(_no_cloud(attr), **{key: local})
        c.ldi_rest_state[CAM]["writes"] = {key: time.monotonic()}
        c.last_update_success = False
        assert cls(c, CAM, _ENTRY).available is False

    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_without_password_unchanged(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        c = _with_settings(_no_cloud(attr, passwords="absent"), **{key: local})
        c.last_update_success = False
        assert cls(c, CAM, _ENTRY).available is False

    @pytest.mark.parametrize(("cls", "key", "local", "want", "attr"), _ALL, ids=_IDS)
    def test_cloud_copy_present_unchanged(
        self, cls: Any, key: str, local: Any, want: Any, attr: str
    ) -> None:
        entity = cls(_light_coord(), CAM, _ENTRY)
        assert entity.available is True
        assert _state(entity) is not None

    @pytest.mark.parametrize(
        ("cls", "key", "bad", "attr"),
        [
            (
                BoschPowerLedBrightnessNumber,
                "icon_led_brightness",
                9,
                "icon_led_brightness_cache",
            ),
            (
                BoschIntrusionDistanceNumber,
                "intrusion_distance",
                0,
                "intrusion_config_cache",
            ),
            (BoschAlarmDelayNumber, "alarm_delay", True, "alarm_settings_cache"),
            (
                BoschDarknessThresholdNumber,
                "darkness_threshold",
                1.5,
                "global_lighting_cache",
            ),
            (BoschSpeakerLevelNumber, "speaker_level", 101, "audio_cache"),
            (
                BoschIntrusionDetectionSwitch,
                "intrusion_enabled",
                1,
                "intrusion_config_cache",
            ),
        ],
    )
    def test_invalid_local_value_does_not_count(
        self, cls: Any, key: str, bad: Any, attr: str
    ) -> None:
        c = _with_settings(_no_cloud(attr), **{key: bad})
        assert cls(c, CAM, _ENTRY).available is False

    def test_other_open_problem_keeps_settings_available(self) -> None:
        """go2rtc missing breaks the stream, not the camera's REST answers."""
        c = _with_settings(_no_cloud("intrusion_config_cache"), intrusion_enabled=True)
        c.ldi_open_status[CAM] = {"reason": RESULT_NO_GO2RTC, "since": 0.0}
        assert BoschIntrusionDetectionSwitch(c, CAM, _ENTRY).available is True

    @pytest.mark.parametrize(
        "store", [None, [], {CAM: []}, {CAM: {"result": None}}, {"other": {}}]
    )
    def test_probe_ok_defensive(self, store: Any) -> None:
        c = SimpleNamespace(ldi_rest_state=store, ldi_open_status=None)
        assert ldi_probe_ok(c, CAM) is False  # type: ignore[arg-type]

    def test_probe_ok_never_raises_on_a_broken_store(self) -> None:
        class _Broken(dict):  # type: ignore[type-arg]
            def get(self, *_a: Any, **_k: Any) -> Any:
                raise TypeError("boom")

        c = SimpleNamespace(ldi_rest_state=_Broken(), ldi_open_status=None)
        assert ldi_probe_ok(c, CAM) is False  # type: ignore[arg-type]


# ── writers with no cloud copy ──────────────────────────────────────────────
def _write_coord(attr: str, *, privacy: str) -> SimpleNamespace:
    c = _no_cloud(attr)
    if privacy == "cloud":
        c.shc_state_cache[CAM]["privacy_mode"] = True
    elif privacy == "local":
        c.ldi_rest_state[CAM]["privacy_on"] = True
    c.async_put_camera = AsyncMock(return_value=True)
    return c


def _entity(cls: Any, c: SimpleNamespace) -> Any:
    entity = cls(c, CAM, _ENTRY)
    entity.hass = MagicMock()
    entity.hass.services.async_call = AsyncMock()
    entity.async_write_ha_state = MagicMock()
    return entity


async def _write(entity: Any) -> None:
    if isinstance(entity, BoschDetectionModeSelect):
        await entity.async_select_option("zones")
    elif hasattr(entity, "async_turn_on"):
        await entity.async_turn_on()
    else:
        lo = entity.entity_description.native_min_value
        await entity.async_set_native_value(lo + 1)


# (entity class, cloud cache attr)
_WRITERS = [
    (BoschIntrusionDetectionSwitch, "intrusion_config_cache"),
    (BoschGlassBreakDetectionSwitch, "audio_detection_cache"),
    (BoschFireAlarmDetectionSwitch, "audio_detection_cache"),
    (BoschAlarmModeSwitch, "alarm_settings_cache"),
    (BoschPreAlarmSwitch, "alarm_settings_cache"),
    (BoschSoftLightFadingSwitch, "global_lighting_cache"),
    (BoschDetectionModeSelect, "intrusion_config_cache"),
    (BoschIntrusionSensitivityNumber, "intrusion_config_cache"),
    (BoschIntrusionDistanceNumber, "intrusion_config_cache"),
    (BoschAlarmDelayNumber, "alarm_settings_cache"),
    (BoschAlarmActivationDelayNumber, "alarm_settings_cache"),
    (BoschPreAlarmDelayNumber, "alarm_settings_cache"),
    (BoschMotionLightSensitivityNumber, "motion_light_cache"),
    (BoschDarknessThresholdNumber, "global_lighting_cache"),
    (BoschSpeakerLevelNumber, "audio_cache"),
]
_WRITER_IDS = [cls.__name__ for cls, _ in _WRITERS]


class TestWritersWithoutCloudCopy:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("privacy", ["cloud", "local"])
    @pytest.mark.parametrize(("cls", "attr"), _WRITERS, ids=_WRITER_IDS)
    async def test_privacy_on_warns_and_sends_nothing(
        self, cls: Any, attr: str, privacy: str
    ) -> None:
        c = _write_coord(attr, privacy=privacy)
        entity = _entity(cls, c)
        put = AsyncMock()
        with patch(f"{SW}.put_setting", new=put):
            await _write(entity)
        c.async_put_camera.assert_not_awaited()
        put.assert_not_awaited()
        call = entity.hass.services.async_call.await_args
        assert call.args[:2] == ("persistent_notification", "create")
        assert call.args[2]["notification_id"] == f"bosch_privacy_blocked_{CAM}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("cls", "attr"), _WRITERS, ids=_WRITER_IDS)
    async def test_privacy_off_skips_without_a_partial_body(
        self, cls: Any, attr: str
    ) -> None:
        c = _write_coord(attr, privacy="off")
        # every local value of the block is known, still no body is built
        _with_settings(
            c,
            intrusion_enabled=True,
            intrusion_sensitivity=3,
            intrusion_distance=5,
            glass_break=True,
            fire_alarm=False,
            alarm_enable=True,
            pre_alarm_enable=False,
            alarm_delay=60.0,
            alarm_activation_delay=0.0,
            pre_alarm_delay=30.0,
            motion_light_enabled=True,
            motion_light_sensitivity=3,
            darkness_threshold=0.5,
            light_fading=True,
            speaker_level=50,
        )
        entity = _entity(cls, c)
        put = AsyncMock()
        with patch(f"{SW}.put_setting", new=put):
            await _write(entity)
        c.async_put_camera.assert_not_awaited()
        put.assert_not_awaited()
        entity.hass.services.async_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_intrusion_number_privacy_on_with_cloud_copy_warns(self) -> None:
        """The cloud rejects this endpoint in privacy mode: no doomed PUT."""
        c = _probed(_light_coord(privacy=True))
        c.async_put_camera = AsyncMock(return_value=True)
        entity = _entity(BoschIntrusionDistanceNumber, c)
        await entity.async_set_native_value(4)
        c.async_put_camera.assert_not_awaited()
        entity.hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_alarm_switch_privacy_on_with_cloud_copy_warns(self) -> None:
        c = _probed(_light_coord(privacy=True))
        c.async_put_camera = AsyncMock(return_value=True)
        entity = _entity(BoschAlarmModeSwitch, c)
        await entity.async_turn_on()
        c.async_put_camera.assert_not_awaited()
        entity.hass.services.async_call.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("cls", "endpoint", "body"),
        [
            (
                BoschIntrusionDistanceNumber,
                "intrusionDetectionConfig",
                {"enabled": True, "sensitivity": 5, "distance": 2},
            ),
            (
                BoschAlarmDelayNumber,
                "alarm_settings",
                {
                    "alarmMode": "OFF",
                    "preAlarmMode": "ON",
                    "alarmDelayInSeconds": 11,
                    "alarmActivationDelaySeconds": 10,
                    "preAlarmDelayInSeconds": 35,
                },
            ),
            (
                BoschAlarmModeSwitch,
                "alarm_settings",
                {
                    "alarmMode": "ON",
                    "preAlarmMode": "ON",
                    "alarmDelayInSeconds": 70,
                    "alarmActivationDelaySeconds": 10,
                    "preAlarmDelayInSeconds": 35,
                },
            ),
            (
                BoschMotionLightSensitivityNumber,
                "lighting/motion",
                {
                    "lightOnMotionEnabled": True,
                    "lightOnMotionFollowUpTimeSeconds": 60,
                    "frontLightSettings": {"brightness": 0},
                    "motionLightSensitivity": 2,
                },
            ),
            (
                BoschDarknessThresholdNumber,
                "lighting",
                {"softLightFading": False, "darknessThreshold": 0.01},
            ),
            (
                BoschSpeakerLevelNumber,
                "audio",
                {"audioEnabled": True, "microphoneLevel": 60, "speakerLevel": 1},
            ),
        ],
    )
    async def test_cloud_copy_present_body_unchanged(
        self, cls: Any, endpoint: str, body: dict[str, Any]
    ) -> None:
        c = _probed(_light_coord())
        c.async_put_camera = AsyncMock(return_value=True)
        c.intrusion_config_set_at = {}
        c.alarm_settings_set_at = {}
        entity = _entity(cls, c)
        if cls is BoschIntrusionDistanceNumber:
            body = {**body, "distance": 2}
            await entity.async_set_native_value(2)
        else:
            await _write(entity)
        c.async_put_camera.assert_awaited_once_with(CAM, endpoint, body)
        entity.hass.services.async_call.assert_not_awaited()


# ── one whole-block write at a time per camera and endpoint ─────────────────
def _slow_put(bodies: list[tuple[str, dict[str, Any]]]) -> AsyncMock:
    async def put(_cam: str, endpoint: str, body: dict[str, Any]) -> bool:
        bodies.append((endpoint, copy.deepcopy(body)))
        await asyncio.sleep(0.01)
        return True

    return AsyncMock(side_effect=put)


class TestLightingWriteLocks:
    @pytest.mark.asyncio
    async def test_motion_switch_and_sensitivity_keep_both_changes(self) -> None:
        c = _probed(_light_coord())
        bodies: list[tuple[str, dict[str, Any]]] = []
        c.async_put_camera = _slow_put(bodies)
        sw = _entity(BoschMotionLightSwitch, c)
        num = _entity(BoschMotionLightSensitivityNumber, c)
        await asyncio.gather(sw.async_turn_off(), num.async_set_native_value(2))
        assert [ep for ep, _ in bodies] == ["lighting/motion", "lighting/motion"]
        last = bodies[-1][1]
        assert last["lightOnMotionEnabled"] is False
        assert last["motionLightSensitivity"] == 2
        assert c.motion_light_cache[CAM] == last

    @pytest.mark.asyncio
    async def test_fading_switch_and_threshold_keep_both_changes(self) -> None:
        c = _probed(_light_coord())
        bodies: list[tuple[str, dict[str, Any]]] = []
        c.async_put_camera = _slow_put(bodies)

        async def put_setting(
            _s: Any, _t: Any, cam: str, endpoint: str, body: dict[str, Any], **_k: Any
        ) -> Any:
            bodies.append((endpoint, copy.deepcopy(body)))
            await asyncio.sleep(0.01)
            return SimpleNamespace(response=None)

        sw = _entity(BoschSoftLightFadingSwitch, c)
        num = _entity(BoschDarknessThresholdNumber, c)
        with (
            patch(f"{SW}.put_setting", new=AsyncMock(side_effect=put_setting)),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await asyncio.gather(sw.async_turn_on(), num.async_set_native_value(30))
        assert len(bodies) == 2
        assert bodies[-1][1] == {"softLightFading": True, "darknessThreshold": 0.3}
        assert c.global_lighting_cache[CAM] == bodies[-1][1]

    @pytest.mark.asyncio
    async def test_threshold_first_then_fading(self) -> None:
        c = _probed(_light_coord())
        bodies: list[tuple[str, dict[str, Any]]] = []
        c.async_put_camera = _slow_put(bodies)

        async def put_setting(
            _s: Any, _t: Any, cam: str, endpoint: str, body: dict[str, Any], **_k: Any
        ) -> Any:
            bodies.append((endpoint, copy.deepcopy(body)))
            return SimpleNamespace(response=None)

        sw = _entity(BoschSoftLightFadingSwitch, c)
        num = _entity(BoschDarknessThresholdNumber, c)
        with (
            patch(f"{SW}.put_setting", new=AsyncMock(side_effect=put_setting)),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await asyncio.gather(num.async_set_native_value(30), sw.async_turn_on())
        assert bodies[-1][1] == {"darknessThreshold": 0.3, "softLightFading": True}

    @pytest.mark.asyncio
    async def test_ambient_writes_do_not_overlap(self) -> None:
        c = _probed(_light_coord())
        active = 0
        peak = 0

        async def fetch(*_a: Any, **_k: Any) -> dict[str, Any]:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            return {"ambientLightEnabled": False, "brightness": 40}

        async def put(*_a: Any, **_k: Any) -> Any:
            nonlocal active
            await asyncio.sleep(0.01)
            active -= 1
            return SimpleNamespace(response=None)

        sw1 = _entity(BoschAmbientLightSwitch, c)
        sw2 = _entity(BoschAmbientLightSwitch, c)
        with (
            patch(f"{SW}.fetch_json", new=AsyncMock(side_effect=fetch)),
            patch(f"{SW}.put_setting", new=AsyncMock(side_effect=put)),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await asyncio.gather(sw1.async_turn_on(), sw2.async_turn_off())
        assert peak == 1
        assert c.ambient_lighting_cache[CAM]["ambientLightEnabled"] is False

    @pytest.mark.asyncio
    async def test_locks_are_per_endpoint_and_camera(self) -> None:
        c = _probed(_light_coord())
        c.async_put_camera = AsyncMock(return_value=True)
        await _entity(BoschMotionLightSensitivityNumber, c).async_set_native_value(2)
        await _entity(BoschDarknessThresholdNumber, c).async_set_native_value(30)
        motion = c._lighting_motion_locks[CAM]
        config = c._lighting_config_locks[CAM]
        assert isinstance(motion, asyncio.Lock) and motion is not config
        assert not motion.locked() and not config.locked()


# ── settings read cut short by its deadline ─────────────────────────────────
def _timed_get(fast: int, first: ldi_rest.LdiRestResponse | None = None) -> Any:
    calls = 0

    async def get(*_a: Any, **_k: Any) -> ldi_rest.LdiRestResponse:
        nonlocal calls
        calls += 1
        if calls > fast:
            await asyncio.sleep(1)
        if calls == 1 and first is not None:
            return first
        return ldi_rest.LdiRestResponse(200, {"enable": True})

    return get


class TestPartialDeadline:
    @pytest.mark.asyncio
    async def test_keeps_fields_read_before_the_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ldi_rest, "LDI_SETTINGS_DEADLINE", 0.2)
        with patch.object(ldi_rest, "_get", new=_timed_get(2)):
            batch = await ldi_rest.get_settings(MagicMock(), IP, "localuser", PW)
        assert batch.result == ldi_rest.PROBE_OK and batch.partial is True
        assert batch.fields["status_led"] == ldi_rest.LdiSetting("ok", True)
        assert batch.fields["record_sound"] == ldi_rest.LdiSetting("ok", True)
        every_key = {k for _, specs in ldi_rest.SETTINGS_ENDPOINTS for k, _, _ in specs}
        assert set(batch.fields) == every_key
        for key in every_key - {"status_led", "record_sound"}:
            assert batch.fields[key].status == ldi_rest.FIELD_UNREACHABLE
        assert batch.missing_paths == frozenset()

    @pytest.mark.asyncio
    async def test_nothing_read_is_unreachable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ldi_rest, "LDI_SETTINGS_DEADLINE", 0.2)
        # a 404 answers, but carries no value
        with patch.object(
            ldi_rest, "_get", new=_timed_get(1, ldi_rest.LdiRestResponse(404))
        ):
            batch = await ldi_rest.get_settings(MagicMock(), IP, "localuser", PW)
        assert batch == ldi_rest.LdiSettings(ldi_rest.PROBE_UNREACHABLE, {})

    @pytest.mark.asyncio
    async def test_skipped_paths_stay_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ldi_rest, "LDI_SETTINGS_DEADLINE", 0.2)
        skip = frozenset({"/sh/data/lighting/motion"})
        with patch.object(ldi_rest, "_get", new=_timed_get(1)):
            batch = await ldi_rest.get_settings(
                MagicMock(), IP, "localuser", PW, skip=skip
            )
        assert batch.partial is True
        assert batch.fields["motion_light_enabled"].status == ldi_rest.FIELD_UNAVAILABLE
        assert batch.fields["alarm_delay"].status == ldi_rest.FIELD_UNREACHABLE

    @pytest.mark.asyncio
    async def test_refresh_stores_the_partial_fields(self) -> None:
        c = _probed(_light_coord())
        fields = dict(_ok_batch(status_led=True).fields)
        fields["intrusion_enabled"] = ldi_rest.LdiSetting(ldi_rest.FIELD_UNREACHABLE)
        batch = ldi_rest.LdiSettings(ldi_rest.PROBE_OK, fields, partial=True)
        with _patched_settings(batch):
            assert await refresh_ldi_settings(c, CAM) is True  # type: ignore[arg-type]
        assert ldi_setting(c, CAM, "status_led") is True  # type: ignore[arg-type]
        assert ldi_setting(c, CAM, "intrusion_enabled") is None  # type: ignore[arg-type]
