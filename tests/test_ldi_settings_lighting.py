"""Light, speaker and lens settings read from the camera's local REST interface.

Pins (v18.2.0-beta-2): motion light (enable + sensitivity), ambient light,
lighting config (darkness threshold + soft fading), speaker level and lens
height are parsed per field, indoor models' 404 on the lighting endpoints is
harmless and re-asked only hourly, each entity shows the local value first
with the cloud cache as fallback, and the whole-block cloud writes take the
camera's own sibling values. Live sample bodies: Eyes Outdoor II, FW 9.40.203.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bosch_shc_camera_client.cameras import BoschCameraError

from custom_components.bosch_smart_camera import ldi_rest
from custom_components.bosch_smart_camera.ldi_local import (
    LDI_SETTINGS_MAX_AGE_SEC,
    merge_local_into_cfg,
    note_cloud_write,
    refresh_ldi_settings,
)
from custom_components.bosch_smart_camera.number import (
    BoschDarknessThresholdNumber,
    BoschLensElevationNumber,
    BoschMicrophoneLevelNumber,
    BoschMotionLightSensitivityNumber,
    BoschSpeakerLevelNumber,
)
from custom_components.bosch_smart_camera.switch import (
    BoschAmbientLightSwitch,
    BoschIntercomSwitch,
    BoschMotionLightSwitch,
    BoschSoftLightFadingSwitch,
)
from tests.test_ldi_local import CAM
from tests.test_ldi_rest import _patched, _Resp, _script, _Session
from tests.test_ldi_settings import (
    _ENTRY,
    _FULL,
    _entity_coord,
    _probed,
    _read,
    _values,
    _with_settings,
)

SW = "custom_components.bosch_smart_camera.switch"
_LIGHTING_PATHS = (
    "/sh/data/lighting/motion",
    "/sh/data/lighting/ambient",
    "/sh/data/lighting/config",
)


def _light_coord(**kwargs: Any) -> Any:
    c = _entity_coord(**kwargs)
    c.motion_light_cache = {
        CAM: {
            "lightOnMotionEnabled": True,
            "lightOnMotionFollowUpTimeSeconds": 60,
            "frontLightSettings": {"brightness": 0},
            "motionLightSensitivity": 5,
        }
    }
    c.ambient_lighting_cache = {CAM: {"ambientLightEnabled": False}}
    c.global_lighting_cache = {
        CAM: {"darknessThreshold": 0.47, "softLightFading": False}
    }
    c.audio_cache = {
        CAM: {"audioEnabled": True, "microphoneLevel": 60, "speakerLevel": 80}
    }
    c.lens_elevation_cache = {CAM: 2.0}
    return c


# ── parsing ─────────────────────────────────────────────────────────────────
class TestParsing:
    @pytest.mark.asyncio
    async def test_indoor_404_on_lighting_is_harmless(self) -> None:
        by_path = dict(_FULL)
        for path in _LIGHTING_PATHS:
            by_path[path] = _Resp(404)
        batch = await _read(_script(**by_path))
        assert batch.result == ldi_rest.PROBE_OK
        assert batch.missing_paths == frozenset(_LIGHTING_PATHS)
        for key in (
            "motion_light_enabled",
            "motion_light_sensitivity",
            "ambient_light_enabled",
            "darkness_threshold",
            "light_fading",
        ):
            assert batch.fields[key].status == ldi_rest.FIELD_UNAVAILABLE
        values = _values(batch)
        assert values["speaker_level"] == 50
        assert values["lens_elevation"] == 1.85

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("path", "body", "key"),
        [
            (
                "/sh/data/lighting/motion",
                '{"sensitivity": "3"}',
                "motion_light_sensitivity",
            ),
            (
                "/sh/data/lighting/motion",
                '{"sensitivity": 3.5}',
                "motion_light_sensitivity",
            ),
            (
                "/sh/data/lighting/motion",
                '{"sensitivity": true}',
                "motion_light_sensitivity",
            ),
            ("/sh/data/lighting/motion", '{"enable": "ON"}', "motion_light_enabled"),
            ("/sh/data/lighting/ambient", '{"enable": 1}', "ambient_light_enabled"),
            ("/sh/data/lighting/ambient", "[]", "ambient_light_enabled"),
            (
                "/sh/data/lighting/config",
                '{"environmentalBrightnessThreshold": NaN}',
                "darkness_threshold",
            ),
            (
                "/sh/data/lighting/config",
                '{"environmentalBrightnessThreshold": "0.6"}',
                "darkness_threshold",
            ),
            (
                "/sh/data/lighting/config",
                '{"environmentalBrightnessThreshold": false}',
                "darkness_threshold",
            ),
            ("/sh/data/lighting/config", '{"fadingEnabled": null}', "light_fading"),
            ("/sh/data/audio/volumeLevel", '{"volumeLevel": true}', "speaker_level"),
            ("/sh/data/audio/volumeLevel", '{"volumeLevel": 1e400}', "speaker_level"),
            (
                "/sh/data/audio/volumeLevel",
                '{"volumeLevel": 1' + "0" * 400 + "}",
                "speaker_level",
            ),
            ("/sh/data/calibration/extrinsic", '{"elevation": null}', "lens_elevation"),
            ("/sh/data/calibration/extrinsic", '{"height": 1.8}', "lens_elevation"),
        ],
    )
    async def test_garbage_is_invalid_and_stays_local_to_its_field(
        self, path: str, body: str, key: str
    ) -> None:
        by_path = dict(_FULL)
        by_path[path] = _Resp(200, body)
        batch = await _read(_script(**by_path))
        assert batch.result == ldi_rest.PROBE_OK
        assert batch.fields[key].status == ldi_rest.FIELD_INVALID
        assert batch.fields["status_led"].status == ldi_rest.FIELD_OK

    def test_request_budget(self) -> None:
        # 120 s poll; keep each batch small for the camera.
        assert len(ldi_rest.SETTINGS_ENDPOINTS) <= 14
        assert "/sh/data/lighting/manual" not in dict(ldi_rest.SETTINGS_ENDPOINTS)

    @pytest.mark.asyncio
    async def test_missing_lighting_paths_skipped_for_an_hour(self) -> None:
        c = _probed(_light_coord())
        by_path = dict(_FULL)
        for path in _LIGHTING_PATHS:
            by_path[path] = _Resp(404)
        first = _script(**by_path)
        p1, p2, _ = _patched(_Session(first))
        with p1, p2:
            assert await refresh_ldi_settings(c, CAM) is True
        assert set(_LIGHTING_PATHS) <= set(first.asked)
        c.ldi_rest_state[CAM]["settings_checked_at"] -= 200
        second = _script(**by_path)
        p1, p2, _ = _patched(_Session(second))
        with p1, p2:
            assert await refresh_ldi_settings(c, CAM) is True
        assert not set(_LIGHTING_PATHS) & set(second.asked)
        # entities on such a model keep the cloud value
        assert BoschMotionLightSwitch(c, CAM, _ENTRY).is_on is True


# ── entities: local first, cloud fallback ───────────────────────────────────
# (entity class, local key, local value, expected with it, cloud value)
_SWITCH_CASES = [
    (BoschMotionLightSwitch, "motion_light_enabled", False, False, True),
    (BoschAmbientLightSwitch, "ambient_light_enabled", True, True, False),
    (BoschSoftLightFadingSwitch, "light_fading", True, True, False),
]

_NUMBER_CASES = [
    (BoschSpeakerLevelNumber, "speaker_level", 50, 50.0, 80.0),
    (BoschSpeakerLevelNumber, "speaker_level", 0, 0.0, 80.0),
    (BoschLensElevationNumber, "lens_elevation", 1.85, 1.85, 2.0),
    (BoschMotionLightSensitivityNumber, "motion_light_sensitivity", 3, 4.0, 2.0),
    (BoschMotionLightSensitivityNumber, "motion_light_sensitivity", 1, 6.0, 2.0),
    (BoschDarknessThresholdNumber, "darkness_threshold", 0.67, 67.0, 47.0),
    (BoschDarknessThresholdNumber, "darkness_threshold", 0, 0.0, 47.0),
    (BoschDarknessThresholdNumber, "darkness_threshold", 1, 100.0, 47.0),
]


class TestEntities:
    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _SWITCH_CASES)
    def test_switch_local_first(
        self, cls: Any, key: str, local: bool, want: bool, cloud: bool
    ) -> None:
        c = _with_settings(_light_coord(), **{key: local})
        assert cls(c, CAM, _ENTRY).is_on is want

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _SWITCH_CASES)
    def test_switch_cloud_fallback(
        self, cls: Any, key: str, local: bool, want: bool, cloud: bool
    ) -> None:
        stale = _with_settings(
            _light_coord(), age=LDI_SETTINGS_MAX_AGE_SEC + 1, **{key: local}
        )
        assert cls(stale, CAM, _ENTRY).is_on is cloud
        no_pw = _with_settings(_light_coord(passwords="absent"), **{key: local})
        assert cls(no_pw, CAM, _ENTRY).is_on is cloud

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _SWITCH_CASES)
    def test_switch_held_after_cloud_write(
        self, cls: Any, key: str, local: bool, want: bool, cloud: bool
    ) -> None:
        c = _with_settings(_light_coord(), **{key: local})
        c.ldi_rest_state[CAM]["writes"] = {key: time.monotonic()}
        assert cls(c, CAM, _ENTRY).is_on is cloud

    @pytest.mark.parametrize(("cls", "key"), [(c[0], c[1]) for c in _SWITCH_CASES])
    def test_switch_ignores_non_bool_local_value(self, cls: Any, key: str) -> None:
        c = _with_settings(_light_coord(), **{key: 1})
        assert cls(c, CAM, _ENTRY).is_on == cls(_light_coord(), CAM, _ENTRY).is_on

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _NUMBER_CASES)
    def test_number_local_first(
        self, cls: Any, key: str, local: float, want: float, cloud: float
    ) -> None:
        c = _with_settings(_light_coord(), **{key: local})
        assert cls(c, CAM, _ENTRY).native_value == want

    @pytest.mark.parametrize(("cls", "key", "local", "want", "cloud"), _NUMBER_CASES)
    def test_number_cloud_fallback(
        self, cls: Any, key: str, local: float, want: float, cloud: float
    ) -> None:
        stale = _with_settings(
            _light_coord(), age=LDI_SETTINGS_MAX_AGE_SEC + 1, **{key: local}
        )
        assert cls(stale, CAM, _ENTRY).native_value == cloud
        no_pw = _with_settings(_light_coord(passwords="absent"), **{key: local})
        assert cls(no_pw, CAM, _ENTRY).native_value == cloud

    @pytest.mark.parametrize(
        ("cls", "key", "bad"),
        [
            (BoschSpeakerLevelNumber, "speaker_level", 150),
            (BoschSpeakerLevelNumber, "speaker_level", -1),
            (BoschSpeakerLevelNumber, "speaker_level", True),
            (BoschLensElevationNumber, "lens_elevation", 10.0),
            (BoschLensElevationNumber, "lens_elevation", 0.0),
            (BoschMotionLightSensitivityNumber, "motion_light_sensitivity", -1),
            (BoschMotionLightSensitivityNumber, "motion_light_sensitivity", 9),
            (BoschDarknessThresholdNumber, "darkness_threshold", 1.5),
            (BoschDarknessThresholdNumber, "darkness_threshold", -0.1),
            (BoschDarknessThresholdNumber, "darkness_threshold", 67),
            (BoschDarknessThresholdNumber, "darkness_threshold", True),
        ],
    )
    def test_number_out_of_range_local_value_falls_back(
        self, cls: Any, key: str, bad: Any
    ) -> None:
        c = _with_settings(_light_coord(), **{key: bad})
        assert (
            cls(c, CAM, _ENTRY).native_value
            == cls(_light_coord(), CAM, _ENTRY).native_value
        )

    def test_availability_still_needs_the_cloud_body(self) -> None:
        """Writes are whole-block cloud PUTs; without the cloud body none can run."""
        c = _with_settings(_light_coord(), motion_light_sensitivity=3, speaker_level=50)
        c.motion_light_cache = {}
        c.audio_cache = {}
        assert BoschMotionLightSensitivityNumber(c, CAM, _ENTRY).available is False
        assert BoschSpeakerLevelNumber(c, CAM, _ENTRY).available is False


# ── writes: cloud only, siblings from the camera ────────────────────────────
def _writable(c: Any) -> Any:
    c.async_put_camera = AsyncMock(return_value=True)
    return c


class TestWrites:
    @pytest.mark.asyncio
    async def test_motion_light_switch_keeps_local_sensitivity(self) -> None:
        c = _writable(_with_settings(_light_coord(), motion_light_sensitivity=2))
        sw = BoschMotionLightSwitch(c, CAM, _ENTRY)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        endpoint, body = c.async_put_camera.await_args.args[1:]
        assert endpoint == "lighting/motion"
        assert body["lightOnMotionEnabled"] is False
        assert body["motionLightSensitivity"] == 2
        assert body["lightOnMotionFollowUpTimeSeconds"] == 60

    @pytest.mark.asyncio
    async def test_motion_sensitivity_keeps_local_enable(self) -> None:
        c = _writable(_with_settings(_light_coord(), motion_light_enabled=False))
        n = BoschMotionLightSensitivityNumber(c, CAM, _ENTRY)
        n.async_write_ha_state = MagicMock()
        await n.async_set_native_value(4)
        body = c.async_put_camera.await_args.args[2]
        assert body["motionLightSensitivity"] == 3
        assert body["lightOnMotionEnabled"] is False

    @pytest.mark.asyncio
    async def test_darkness_threshold_keeps_local_fading(self) -> None:
        c = _writable(_with_settings(_light_coord(), light_fading=True))
        n = BoschDarknessThresholdNumber(c, CAM, _ENTRY)
        n.async_write_ha_state = MagicMock()
        await n.async_set_native_value(30)
        c.async_put_camera.assert_awaited_once_with(
            CAM, "lighting", {"softLightFading": True, "darknessThreshold": 0.3}
        )

    @pytest.mark.asyncio
    async def test_soft_fading_keeps_local_threshold_and_holds(self) -> None:
        c = _with_settings(_light_coord(), darkness_threshold=0.67, light_fading=False)
        put = AsyncMock(return_value=MagicMock(response=None))
        sw = BoschSoftLightFadingSwitch(c, CAM, _ENTRY)
        sw.hass = MagicMock()
        sw.async_write_ha_state = MagicMock()
        with (
            patch(f"{SW}.put_setting", new=put),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await sw.async_turn_on()
        assert put.await_args.args[3] == "lighting"
        assert put.await_args.args[4] == {
            "darknessThreshold": 0.67,
            "softLightFading": True,
        }
        # the stale local False is held back; the optimistic cloud value shows
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_ambient_write_holds_the_local_value(self) -> None:
        c = _with_settings(_light_coord(), ambient_light_enabled=False)
        sw = BoschAmbientLightSwitch(c, CAM, _ENTRY)
        sw.hass = MagicMock()
        sw.async_write_ha_state = MagicMock()
        put = AsyncMock()
        with (
            patch(
                f"{SW}.fetch_json",
                new=AsyncMock(return_value={"ambientLightEnabled": False}),
            ),
            patch(f"{SW}.put_setting", new=put),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await sw.async_turn_on()
        assert put.await_args.args[4] == {"ambientLightEnabled": True}
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_microphone_write_keeps_local_speaker_level(self) -> None:
        c = _writable(_with_settings(_light_coord(), speaker_level=50))
        c.hw_version = {CAM: "HOME_Eyes_Outdoor"}
        n = BoschMicrophoneLevelNumber(c, CAM, _ENTRY)
        n.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.bosch_smart_camera.number._is_gen2_indoor",
            return_value=False,
        ):
            await n.async_set_native_value(40)
        c.async_put_camera.assert_awaited_once_with(
            CAM,
            "audio",
            {"audioEnabled": True, "microphoneLevel": 40, "speakerLevel": 50},
        )
        # the cache keeps the sent block, so a later write in the hold window
        # does not fall back to a stale cloud speaker level
        assert c.audio_cache[CAM]["speakerLevel"] == 50

    @pytest.mark.asyncio
    async def test_second_audio_write_in_hold_keeps_speaker_level(self) -> None:
        """Two writes in a row: the hold hides the local value, the cache holds it."""
        c = _writable(_with_settings(_light_coord(), speaker_level=50))
        bodies: list[dict[str, Any]] = []

        async def put(cam_id: str, endpoint: str, body: dict[str, Any]) -> bool:
            bodies.append(dict(body))
            note_cloud_write(c, cam_id, endpoint)
            return True

        c.async_put_camera = AsyncMock(side_effect=put)
        n = BoschMicrophoneLevelNumber(c, CAM, _ENTRY)
        n.async_write_ha_state = MagicMock()
        with patch(
            "custom_components.bosch_smart_camera.number._is_gen2_indoor",
            return_value=False,
        ):
            await n.async_set_native_value(40)
            await n.async_set_native_value(30)
        assert [b["speakerLevel"] for b in bodies] == [50, 50]
        assert bodies[1]["microphoneLevel"] == 30

    @pytest.mark.asyncio
    async def test_failed_ambient_write_starts_no_hold(self) -> None:
        c = _with_settings(_light_coord(), ambient_light_enabled=True)
        sw = BoschAmbientLightSwitch(c, CAM, _ENTRY)
        sw.hass = MagicMock()
        sw.async_write_ha_state = MagicMock()
        with (
            patch(
                f"{SW}.fetch_json",
                new=AsyncMock(return_value={"ambientLightEnabled": True}),
            ),
            patch(
                f"{SW}.put_setting",
                new=AsyncMock(side_effect=BoschCameraError("rejected", status=443)),
            ),
            patch(f"{SW}.async_get_bosch_cloud_session", new=AsyncMock()),
        ):
            await sw.async_turn_off()
        assert not c.ldi_rest_state[CAM].get("writes")

    @pytest.mark.asyncio
    async def test_intercom_off_keeps_local_speaker_level(self) -> None:
        c = _writable(_with_settings(_light_coord(), speaker_level=35))
        sw = BoschIntercomSwitch(c, CAM, _ENTRY)
        sw.async_write_ha_state = MagicMock()
        await sw.async_turn_off()
        body = c.async_put_camera.await_args.args[2]
        assert body == {
            "audioEnabled": False,
            "microphoneLevel": 60,
            "speakerLevel": 35,
        }
        assert c.audio_cache[CAM]["speakerLevel"] == 35

    @pytest.mark.asyncio
    async def test_lens_elevation_write_unchanged(self) -> None:
        c = _writable(_with_settings(_light_coord(), lens_elevation=1.85))
        n = BoschLensElevationNumber(c, CAM, _ENTRY)
        n.async_write_ha_state = MagicMock()
        await n.async_set_native_value(2.5)
        c.async_put_camera.assert_awaited_once_with(
            CAM, "lens_elevation", {"elevation": 2.5}
        )


class TestWriteHoldsAndMerge:
    @pytest.mark.parametrize(
        ("endpoint", "keys"),
        [
            ("lighting/motion", {"motion_light_enabled", "motion_light_sensitivity"}),
            ("lighting/ambient", {"ambient_light_enabled"}),
            ("lighting", {"darkness_threshold", "light_fading"}),
            ("audio", {"speaker_level"}),
            ("lens_elevation", {"lens_elevation"}),
        ],
    )
    def test_cloud_write_holds_its_fields(self, endpoint: str, keys: set[str]) -> None:
        c = _with_settings(_light_coord())
        note_cloud_write(c, CAM, endpoint)
        assert set(c.ldi_rest_state[CAM]["writes"]) == keys

    @pytest.mark.parametrize(
        "endpoint", ["lighting/switch", "lighting/switch/front", "lighting_override"]
    )
    def test_light_switch_writes_hold_nothing(self, endpoint: str) -> None:
        c = _with_settings(_light_coord())
        note_cloud_write(c, CAM, endpoint)
        assert "writes" not in c.ldi_rest_state[CAM]

    def test_fraction_never_rounded_to_cloud_int(self) -> None:
        c = _with_settings(_light_coord(), darkness_threshold=0.67)
        out = merge_local_into_cfg(c, CAM, {"darknessThreshold": 1}, "lighting")
        assert out == {"darknessThreshold": 0.67}

    def test_out_of_range_not_merged(self) -> None:
        c = _with_settings(
            _light_coord(),
            darkness_threshold=1.5,
            speaker_level=101,
            motion_light_sensitivity=8,
        )
        assert merge_local_into_cfg(c, CAM, {"darknessThreshold": 0.4}, "lighting") == {
            "darknessThreshold": 0.4
        }
        assert merge_local_into_cfg(c, CAM, {"speakerLevel": 80}, "audio") == {
            "speakerLevel": 80
        }
        assert merge_local_into_cfg(
            c, CAM, {"motionLightSensitivity": 5}, "lighting/motion"
        ) == {"motionLightSensitivity": 5}

    def test_nested_light_groups_are_not_touched(self) -> None:
        c = _with_settings(_light_coord(), motion_light_enabled=False)
        cfg = {"lightOnMotionEnabled": True, "frontLightSettings": {"brightness": 40}}
        out = merge_local_into_cfg(c, CAM, cfg, "lighting/motion")
        assert out == {
            "lightOnMotionEnabled": False,
            "frontLightSettings": {"brightness": 40},
        }

    def test_speaker_level_int_stays_int(self) -> None:
        c = _with_settings(_light_coord(), speaker_level=49.6)
        out = merge_local_into_cfg(c, CAM, {"speakerLevel": 80}, "audio")
        assert out == {"speakerLevel": 50} and isinstance(out["speakerLevel"], int)
