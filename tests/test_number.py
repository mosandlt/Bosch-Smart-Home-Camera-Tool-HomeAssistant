"""Tests for number.py entity classes.

number.py hosts every NumberEntity the integration exposes: pan position,
speaker/microphone level, intrusion detection sensitivity/distance, front
light intensity, lens elevation, white balance, LED brightness (top/bottom/
power), motion-light sensitivity, darkness threshold, alarm delays, and the
virtual card-playback audio-volume entity.

Most entities follow the same pattern:
  - read coordinator cache for native_value
  - write via a coordinator method (or coordinator.async_put_camera) on
    async_set_native_value
  - available iff cache populated AND coordinator success

Also covered:
  - the rotation-180 sign-inversion for the pan slider (ceiling-mounted
    cameras): the user-visible direction must match what they expect.
  - the doubled-"Bosch "-prefix entity_id regression (forum post 998974/15,
    Andrew75): `_attr_has_entity_name = True` combined with a manually
    prefixed `_attr_name` produced entity_ids like
    `number.bosch_est_bosch_est_pan_position`.
  - the intrusion-distance clamp regression: the Bosch cloud API rejects
    distance > 8 with HTTP 400 on FW 9.40.102, so values above 8 must be
    clamped before the PUT is sent.
  - async_setup_entry entity gating per camera generation/feature-support.
  - PIN_EVERY_MODE boundary/default/garbage coverage for intrusion
    sensitivity + distance.
  - shared per-camera locking for the audio endpoint (speaker + microphone
    level both read-modify-write the same cache/PUT body).

No HA runtime needed — SimpleNamespace + AsyncMock pattern throughout.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
CAM_ID_GEN2_OUTDOOR = CAM_ID
CAM_ID_GEN2_INDOOR = "22222222-2222-2222-2222-222222222222"
CAM_ID_GEN1 = "44444444-0000-0000-0000-000000000001"

_INTRUSION_CFG = {
    "enabled": True,
    "detectionMode": "ALL_MOTIONS",
    "sensitivity": 3,
    "distance": 8,
}

_AUDIO_CFG = {
    "audioEnabled": True,
    "microphoneLevel": 60,
    "speakerLevel": 75,
}


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    """Shared minimal ConfigEntry stub used across most sections below."""
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


@pytest.fixture
def stub_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
            }
        },
        pan_cache={},
        image_rotation_180={},
        _front_light_intensity_cache={CAM_ID: 0.5},
        _front_light_color_temp_cache={CAM_ID: 4000},
        _top_led_brightness_cache={CAM_ID: 0.7},
        _bottom_led_brightness_cache={CAM_ID: 0.3},
        _ledlight_brightness_cache={CAM_ID: 80},
        _mounting_height_cache={CAM_ID: 2.5},
        _mic_level_cache={CAM_ID: 50},
        _speaker_level_cache={CAM_ID: 75},
        _white_balance_cache={CAM_ID: 5000},
        _motion_light_sensitivity_cache={CAM_ID: 0.6},
        _darkness_threshold_cache={CAM_ID: 0.3},
        _power_led_brightness_cache={CAM_ID: 0.5},
        _alarm_delay_cache={CAM_ID: {"alarmDelay": 30}},
        last_update_success=True,
        async_cloud_set_pan=AsyncMock(),
    )


class TestPanNumber:
    def test_construction(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n._attr_translation_key == "pan_position"
        assert n._attr_native_min_value == -120
        assert n._attr_native_max_value == 120

    def test_native_value_none_when_cache_empty(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value is None

    def test_native_value_reads_cache(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.pan_cache[CAM_ID] = 30
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value == 30

    def test_unavailable_when_cache_empty(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.available is False

    def test_available_when_cache_populated(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.pan_cache[CAM_ID] = 0
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.available is True

    def test_rotation_180_inverts_sign_on_read(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Ceiling-mounted: cam-physical +30° → user-visible -30°."""
        stub_coord.pan_cache[CAM_ID] = 30
        stub_coord.image_rotation_180[CAM_ID] = True
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value == -30

    def test_rotation_180_off_no_inversion(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.pan_cache[CAM_ID] = 30
        stub_coord.image_rotation_180[CAM_ID] = False
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert n.native_value == 30

    @pytest.mark.asyncio
    async def test_set_value_inverts_when_rotated(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """User drags slider to +50 (right) on ceiling-mounted cam → send -50 to camera."""
        stub_coord.image_rotation_180[CAM_ID] = True
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        await n.async_set_native_value(50)
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, -50)

    @pytest.mark.asyncio
    async def test_set_value_no_invert_when_not_rotated(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        n = BoschPanNumber(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        await n.async_set_native_value(50)
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 50)

    def test_device_info(self):
        """`device_info` returns a dict wired to model/firmware (bypass __init__)."""
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        e = _make_entity_guards(BoschPanNumber)
        info = e.device_info
        assert isinstance(info, dict)
        assert info["model"] == "Eyes Outdoor"
        assert info["sw_version"] == "9.40.25"


# Doubled-"Bosch "-prefix entity_id regression
#
# Bug source: forum post 998974/15, Andrew75, 2026-05-15.
# Classes with `_attr_has_entity_name = True` AND `_attr_name = f"Bosch {title} Suffix"`
# produced entity_ids like `number.bosch_est_bosch_est_pan_position` because HA
# prepends the device name automatically when has_entity_name=True, and the code
# re-prepended "Bosch {title}" manually.
#
# Fix: _attr_name must be the bare suffix literal (e.g. "Pan Position"), NOT
# prefixed with "Bosch {title}".
#
# Covers all 16 classes that had the bug:
#   BoschPanNumber, BoschAudioThresholdNumber, BoschSpeakerLevelNumber,
#   BoschFrontLightIntensityNumber, BoschLensElevationNumber,
#   BoschMicrophoneLevelNumber, BoschWhiteBalanceNumber,
#   BoschTopLedBrightnessNumber, BoschBottomLedBrightnessNumber,
#   BoschMotionLightSensitivityNumber, BoschDarknessThresholdNumber,
#   BoschPowerLedBrightnessNumber, BoschAlarmDelayNumber,
#   BoschAlarmActivationDelayNumber, BoschPreAlarmDelayNumber,
#   BoschAudioAlarmSensitivityNumber.


@pytest.fixture
def coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
            }
        },
        last_update_success=True,
        options={},
        token="test-token",
        pan_cache={},
        image_rotation_180={},
        lens_elevation_cache={},
        audio_cache={},
        lighting_switch_cache={},
        motion_light_cache={},
        global_lighting_cache={},
        icon_led_brightness_cache={},
        alarm_settings_cache={},
        shc_state_cache={CAM_ID: {}},
        async_put_camera=AsyncMock(return_value=True),
        async_cloud_set_pan=AsyncMock(),
        async_cloud_set_light_component=AsyncMock(),
    )


@pytest.fixture
def entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


@pytest.fixture
def coord_indoor(coord: SimpleNamespace) -> SimpleNamespace:
    """Indoor II coord (for alarm-delay / power-LED classes)."""
    coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
    return coord


# ── parametrized instantiation helpers ───────────────────────────────────────


def _make_pan(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschPanNumber

    return BoschPanNumber(coord, CAM_ID, entry, pan_limit=120)


def _make_speaker_level(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

    return BoschSpeakerLevelNumber(coord, CAM_ID, entry)


def _make_front_light_intensity(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber

    return BoschFrontLightIntensityNumber(coord, CAM_ID, entry)


def _make_lens_elevation(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

    return BoschLensElevationNumber(coord, CAM_ID, entry)


def _make_microphone_level(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

    return BoschMicrophoneLevelNumber(coord, CAM_ID, entry)


def _make_white_balance(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

    return BoschWhiteBalanceNumber(coord, CAM_ID, entry)


def _make_top_led_brightness(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschTopLedBrightnessNumber

    return BoschTopLedBrightnessNumber(coord, CAM_ID, entry)


def _make_bottom_led_brightness(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschBottomLedBrightnessNumber

    return BoschBottomLedBrightnessNumber(coord, CAM_ID, entry)


def _make_motion_light_sensitivity(coord, entry):
    from custom_components.bosch_shc_camera.number import (
        BoschMotionLightSensitivityNumber,
    )

    return BoschMotionLightSensitivityNumber(coord, CAM_ID, entry)


def _make_darkness_threshold(coord, entry):
    from custom_components.bosch_shc_camera.number import BoschDarknessThresholdNumber

    return BoschDarknessThresholdNumber(coord, CAM_ID, entry)


def _make_power_led_brightness(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import BoschPowerLedBrightnessNumber

    return BoschPowerLedBrightnessNumber(coord_indoor, CAM_ID, entry)


def _make_alarm_delay(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import BoschAlarmDelayNumber

    return BoschAlarmDelayNumber(coord_indoor, CAM_ID, entry)


def _make_alarm_activation_delay(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import (
        BoschAlarmActivationDelayNumber,
    )

    return BoschAlarmActivationDelayNumber(coord_indoor, CAM_ID, entry)


def _make_pre_alarm_delay(coord_indoor, entry):
    from custom_components.bosch_shc_camera.number import BoschPreAlarmDelayNumber

    return BoschPreAlarmDelayNumber(coord_indoor, CAM_ID, entry)


# ── outdoor-coord classes ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "factory",
    [
        _make_pan,
        _make_speaker_level,
        _make_front_light_intensity,
        _make_lens_elevation,
        _make_microphone_level,
        _make_white_balance,
        _make_top_led_brightness,
        _make_bottom_led_brightness,
        _make_motion_light_sensitivity,
        _make_darkness_threshold,
    ],
)
def test_no_doubled_bosch_prefix_outdoor(
    factory: Callable[[SimpleNamespace, SimpleNamespace], object],
    coord: SimpleNamespace,
    entry: SimpleNamespace,
):
    """_attr_name must not start with 'Bosch ' for outdoor-coord classes."""
    entity = factory(coord, entry)
    name = getattr(entity, "_attr_name", None)
    assert name is None or not name.startswith("Bosch "), (
        f"{type(entity).__name__}._attr_name={name!r} still has 'Bosch ' prefix"
    )


@pytest.mark.parametrize(
    "factory",
    [
        _make_pan,
        _make_speaker_level,
        _make_front_light_intensity,
        _make_lens_elevation,
        _make_microphone_level,
        _make_white_balance,
        _make_top_led_brightness,
        _make_bottom_led_brightness,
        _make_motion_light_sensitivity,
        _make_darkness_threshold,
    ],
)
def test_has_entity_name_true_outdoor(
    factory: Callable[[SimpleNamespace, SimpleNamespace], object],
    coord: SimpleNamespace,
    entry: SimpleNamespace,
):
    """_attr_has_entity_name must be True (own or inherited) for all outdoor-coord classes."""
    entity = factory(coord, entry)
    # Use getattr so we resolve both plain attributes and properties correctly.
    has_entity_name = getattr(entity, "_attr_has_entity_name", False)
    # HA's NumberEntity / RestoreEntity may expose this as a property that returns
    # the class-level bool set in our classes, so evaluate truthiness.
    assert bool(has_entity_name) is True, (
        f"{type(entity).__name__}._attr_has_entity_name is not True (got {has_entity_name!r})"
    )


# ── indoor-coord classes ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "factory",
    [
        _make_power_led_brightness,
        _make_alarm_delay,
        _make_alarm_activation_delay,
        _make_pre_alarm_delay,
    ],
)
def test_no_doubled_bosch_prefix_indoor(
    factory: Callable[[SimpleNamespace, SimpleNamespace], object],
    coord_indoor: SimpleNamespace,
    entry: SimpleNamespace,
):
    """_attr_name must not start with 'Bosch ' for indoor-coord classes."""
    entity = factory(coord_indoor, entry)
    name = getattr(entity, "_attr_name", None)
    assert name is None or not name.startswith("Bosch "), (
        f"{type(entity).__name__}._attr_name={name!r} still has 'Bosch ' prefix"
    )


@pytest.mark.parametrize(
    "factory",
    [
        _make_power_led_brightness,
        _make_alarm_delay,
        _make_alarm_activation_delay,
        _make_pre_alarm_delay,
    ],
)
def test_has_entity_name_true_indoor(
    factory: Callable[[SimpleNamespace, SimpleNamespace], object],
    coord_indoor: SimpleNamespace,
    entry: SimpleNamespace,
):
    """_attr_has_entity_name must be True (own or inherited) for all indoor-coord classes."""
    entity = factory(coord_indoor, entry)
    has_entity_name = getattr(entity, "_attr_has_entity_name", False)
    assert bool(has_entity_name) is True, (
        f"{type(entity).__name__}._attr_has_entity_name is not True (got {has_entity_name!r})"
    )


# Shared helpers for the SimpleNamespace + AsyncMock coordinator-bypass sections
# below (speaker/mic/intrusion, lens elevation, white balance, LED brightness,
# motion-light, darkness threshold, power LED, alarm delay, audio volume).
def _coord(
    cam_id: str = CAM_ID_GEN2_OUTDOOR,
    hw: str = "HOME_Eyes_Outdoor",
    audio_cache: dict | None = None,
    intrusion_cache: dict | None = None,
    intrusion_set_at: dict | None = None,
    shc_state_cache: dict | None = None,
    put_return: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            cam_id: {
                "info": {
                    "title": "Testkamera",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
            }
        },
        last_update_success=True,
        token="test-token",
        options={},
        audio_cache=audio_cache if audio_cache is not None else {},
        intrusion_config_cache=intrusion_cache if intrusion_cache is not None else {},
        intrusion_config_set_at=intrusion_set_at
        if intrusion_set_at is not None
        else {},
        shc_state_cache=shc_state_cache
        if shc_state_cache is not None
        else {cam_id: {}},
        async_put_camera=AsyncMock(return_value=put_return),
        is_camera_online=lambda cid: True,
    )


def _entry() -> SimpleNamespace:
    return SimpleNamespace(data={"bearer_token": "tok"}, options={}, runtime_data=None)


def _make_entity_intrusion(cls, coord, cam_id=CAM_ID_GEN2_OUTDOOR):
    """Bypass __init__ safely for entities that call CoordinatorEntity.__init__."""
    ent = cls.__new__(cls)
    ent.coordinator = coord
    ent._cam_id = cam_id
    ent._entry = _entry()
    info = coord.data[cam_id]["info"]
    ent._cam_title = info["title"]
    ent._model = info["hardwareVersion"]
    ent._model_name = ent._model
    ent._fw = info["firmwareVersion"]
    ent._mac = info.get("macAddress", "")
    ent.async_write_ha_state = MagicMock()
    return ent


def _stub_coord_guards(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 90},
                },
            },
        },
        shc_state_cache={CAM_ID: {"front_light_intensity": 0.5, "privacy_mode": False}},
        pan_cache={},
        lens_elevation_cache={},
        audio_cache={},
        lighting_switch_cache={},
        motion_light_cache={},
        global_lighting_cache={},
        icon_led_brightness_cache={},
        alarm_settings_cache={},
        image_rotation_180={},
        last_update_success=True,
        token="tok-A",
        options={},
        motion_settings=lambda cid: {},
        async_put_camera=AsyncMock(return_value=True),
        async_cloud_set_light_component=AsyncMock(),
        is_camera_online=lambda cid: True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_entity_guards(
    klass, coord=None, *, led_key=None, field=None, mac="aa:bb:cc:dd:ee:01"
):
    """Bypass __init__ for number entities (device_info / available / write-guard tests)."""
    coord = coord or _stub_coord_guards()
    e = klass.__new__(klass)
    e.coordinator = coord
    e._cam_id = CAM_ID
    e._entry = SimpleNamespace(data={}, options={})
    e._cam_title = "Terrasse"
    e._model = "HOME_Eyes_Outdoor"
    e._model_name = "Eyes Outdoor"
    e._fw = "9.40.25"
    e._mac = mac
    e._brightness = None
    e._wb_value = None
    e._last_written = 0
    e._current_level = 50
    if led_key is not None:
        e._led_key = led_key
    if field is not None:
        e._field = field
    e.async_write_ha_state = MagicMock()
    e.hass = SimpleNamespace()
    return e


def _make_put_session_guards(
    status: int = 200,
    json_payload=None,
    json_raises: Exception | None = None,
    put_raises: Exception | None = None,
):
    """Stub async-context session.put()."""
    resp = MagicMock()
    resp.status = status
    if json_raises is not None:
        resp.json = AsyncMock(side_effect=json_raises)
    else:
        resp.json = AsyncMock(return_value=json_payload or {})

    @asynccontextmanager
    async def _resp_cm(*args, **kwargs):
        yield resp

    session = MagicMock()
    if put_raises is not None:
        session.put = MagicMock(side_effect=put_raises)
    else:
        session.put = MagicMock(side_effect=_resp_cm)
    return session, resp


class TestBoschSpeakerLevelNumber:
    """Verify that speaker level reads from audio_cache, not a static default."""

    def _make(self, audio_cache=None, cam_id=CAM_ID_GEN2_OUTDOOR, put_return=True):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

        coord = _coord(cam_id=cam_id, audio_cache=audio_cache, put_return=put_return)
        ent = _make_entity_intrusion(BoschSpeakerLevelNumber, coord, cam_id)
        return ent, coord

    # --- native_value ---

    def test_native_value_reads_audio_cache(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        assert ent.native_value == 75.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(audio_cache={})
        assert ent.native_value is None

    def test_native_value_none_when_speaker_level_missing(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"audioEnabled": True}})
        assert ent.native_value is None

    def test_native_value_min_boundary(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"speakerLevel": 0}})
        assert ent.native_value == 0.0

    def test_native_value_max_boundary(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"speakerLevel": 100}})
        assert ent.native_value == 100.0

    # --- available ---

    def test_available_true_when_cache_populated(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        assert ent.available is True

    def test_available_false_when_cache_empty(self):
        ent, _ = self._make(audio_cache={})
        assert ent.available is False

    # --- async_set_native_value ---

    @pytest.mark.asyncio
    async def test_set_value_sends_full_body(self):
        """PUT must send complete audio body — not just speakerLevel field."""
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(50.0)

        coord.async_put_camera.assert_called_once()
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "audio"
        assert body["speakerLevel"] == 50
        # audioEnabled and microphoneLevel must be preserved
        assert "audioEnabled" in body
        assert "microphoneLevel" in body

    @pytest.mark.asyncio
    async def test_set_value_updates_cache_on_success(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(30.0)
        assert coord.audio_cache[CAM_ID_GEN2_OUTDOOR]["speakerLevel"] == 30

    @pytest.mark.asyncio
    async def test_set_value_no_cache_update_on_failure(self):
        audio = {"speakerLevel": 75, "audioEnabled": True, "microphoneLevel": 60}
        ent, coord = self._make(
            audio_cache={CAM_ID_GEN2_OUTDOOR: dict(audio)},
            put_return=False,
        )
        await ent.async_set_native_value(20.0)
        # Cache must stay at original value
        assert coord.audio_cache[CAM_ID_GEN2_OUTDOOR]["speakerLevel"] == 75

    @pytest.mark.asyncio
    async def test_set_value_rounds_float(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(67.6)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["speakerLevel"] == 68

    @pytest.mark.asyncio
    async def test_set_min_value(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(0.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["speakerLevel"] == 0

    @pytest.mark.asyncio
    async def test_set_max_value(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(100.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["speakerLevel"] == 100

    @pytest.mark.asyncio
    async def test_set_value_exception_propagates_cache_unchanged(self):
        """async_put_camera exceptions propagate — HA platform layer catches
        them. Cache must not be corrupted on exception."""
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        coord.async_put_camera = AsyncMock(side_effect=Exception("net error"))
        try:
            await ent.async_set_native_value(90.0)
        except Exception:
            pass
        assert coord.audio_cache[CAM_ID_GEN2_OUTDOOR]["speakerLevel"] == 75

    @pytest.mark.asyncio
    async def test_set_value_preserves_microphone_sibling(self):
        """A write must merge only speakerLevel into the shared audio cache —
        the sibling microphoneLevel field must survive untouched."""
        ent, coord = self._make(
            audio_cache={CAM_ID_GEN2_OUTDOOR: {"microphoneLevel": 42}}
        )
        await ent.async_set_native_value(80.0)
        assert coord.audio_cache[CAM_ID_GEN2_OUTDOOR]["speakerLevel"] == 80
        assert coord.audio_cache[CAM_ID_GEN2_OUTDOOR]["microphoneLevel"] == 42

    # --- metadata ---

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

        coord = _coord(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        ent = BoschSpeakerLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "speaker_level"

    def test_unique_id(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

        coord = _coord(audio_cache={})
        ent = BoschSpeakerLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert (
            ent._attr_unique_id
            == f"bosch_shc_camera_{CAM_ID_GEN2_OUTDOOR}_speaker_level"
        )

    def test_disabled_by_default(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

        coord = _coord(audio_cache={})
        ent = BoschSpeakerLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.entity_registry_enabled_default is False

    def test_device_info(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

        e = _make_entity_guards(BoschSpeakerLevelNumber)
        info = e.device_info
        assert info["manufacturer"] == "Bosch"


class TestBoschMicrophoneLevelNumber:
    """Smoke + regression tests for mic level — privacy guard and body shape."""

    def _make(
        self,
        hw="HOME_Eyes_Outdoor",
        audio_cache=None,
        cam_id=CAM_ID_GEN2_OUTDOOR,
        put_return=True,
    ):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

        coord = _coord(
            cam_id=cam_id, hw=hw, audio_cache=audio_cache, put_return=put_return
        )
        ent = _make_entity_intrusion(BoschMicrophoneLevelNumber, coord, cam_id)
        return ent, coord

    def test_native_value_reads_microphone_level(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)})
        assert ent.native_value == 60.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(audio_cache={})
        assert ent.native_value is None

    def test_native_value_none_when_field_missing(self):
        """Distinct from an empty cache: the cam has an audio entry, just no
        microphoneLevel field in it yet."""
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"audioEnabled": True}})
        assert ent.native_value is None

    def test_native_value_min(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"microphoneLevel": 0}})
        assert ent.native_value == 0.0

    def test_native_value_max(self):
        ent, _ = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: {"microphoneLevel": 100}})
        assert ent.native_value == 100.0

    def test_available_false_when_no_cache(self):
        ent, _ = self._make(audio_cache={})
        assert ent.available is False

    def test_range_is_0_to_100(self):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

        coord = _coord(audio_cache={})
        ent = BoschMicrophoneLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_native_min_value == 0
        assert ent._attr_native_max_value == 100

    @pytest.mark.asyncio
    async def test_set_value_sends_full_audio_body(self):
        audio = dict(_AUDIO_CFG)
        ent, coord = self._make(audio_cache={CAM_ID_GEN2_OUTDOOR: audio})
        await ent.async_set_native_value(80.0)
        coord.async_put_camera.assert_called_once()
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "audio"
        assert body["microphoneLevel"] == 80
        assert "speakerLevel" in body

    @pytest.mark.asyncio
    async def test_privacy_guard_indoor_blocks_write_via_internal_mocks(self):
        """Indoor II with privacy ON must not call async_put_camera — exercised
        by patching the internal `_is_gen2_indoor`/`_warn_if_privacy_on` helpers
        directly (unit-isolated variant)."""
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            audio_cache={CAM_ID_GEN2_INDOOR: dict(_AUDIO_CFG)},
            shc_state_cache={CAM_ID_GEN2_INDOOR: {"privacy_mode": True}},
        )
        ent = _make_entity_intrusion(
            BoschMicrophoneLevelNumber, coord, CAM_ID_GEN2_INDOOR
        )
        import unittest.mock as mock

        with (
            mock.patch(
                "custom_components.bosch_shc_camera.number._is_gen2_indoor",
                return_value=True,
            ),
            mock.patch(
                "custom_components.bosch_shc_camera.number._warn_if_privacy_on",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            await ent.async_set_native_value(40.0)
        coord.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_privacy_guard_indoor_blocks_write_via_real_state(self):
        """Same guard exercised end-to-end via the real shc_state_cache
        privacy_mode flag, without mocking internal helpers."""
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

        coord = _coord(
            cam_id=CAM_ID,
            hw="HOME_Eyes_Indoor",
            audio_cache={CAM_ID: {"microphoneLevel": 60}},
            shc_state_cache={CAM_ID: {"privacy_mode": True}},
        )
        ent = _make_entity_intrusion(BoschMicrophoneLevelNumber, coord, CAM_ID)
        await ent.async_set_native_value(80.0)
        coord.async_put_camera.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_untouched_on_put_failure(self):
        """Regression: number setters must gate the optimistic cache write on
        the async_put_camera() bool result — a failed PUT previously poisoned
        the cache with the user-entered value."""
        from custom_components.bosch_shc_camera.number import (
            BoschMicrophoneLevelNumber,
        )

        coord = _stub_coord_guards(
            async_put_camera=AsyncMock(return_value=False),
            audio_cache={CAM_ID: {"microphoneLevel": 30}},
        )
        e = _make_entity_guards(BoschMicrophoneLevelNumber, coord)
        await e.async_set_native_value(80.0)
        assert coord.audio_cache[CAM_ID]["microphoneLevel"] == 30, (
            "mic-level cache must keep its prior value when the PUT fails"
        )

    @pytest.mark.asyncio
    async def test_cache_updated_on_put_success(self):
        from custom_components.bosch_shc_camera.number import (
            BoschMicrophoneLevelNumber,
        )

        coord = _stub_coord_guards(
            async_put_camera=AsyncMock(return_value=True),
            audio_cache={CAM_ID: {"microphoneLevel": 30}},
        )
        e = _make_entity_guards(BoschMicrophoneLevelNumber, coord)
        await e.async_set_native_value(80.0)
        assert coord.audio_cache[CAM_ID]["microphoneLevel"] == 80

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import BoschMicrophoneLevelNumber

        coord = _coord(audio_cache={})
        ent = BoschMicrophoneLevelNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "microphone_level"


# Shared per-camera lock — BoschSpeakerLevelNumber / BoschMicrophoneLevelNumber /
# BoschIntercomSwitch (switch.py) all read-modify-write the same /audio endpoint
# and audio_cache.
class TestAudioConfigLockConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_speaker_and_mic_writes_serialize(self):
        """Regression (bug-hunt 2026-07-03): BoschSpeakerLevelNumber and
        BoschMicrophoneLevelNumber already merge only their own field back
        into audio_cache after a successful PUT (bug-hunt 2026-06-02), but
        without a lock the READ before that merge could still race: two
        concurrent writes for different fields could both snapshot the
        cache before either finishes writing, so whichever completes last
        would overwrite the other's just-written field with its own stale
        snapshot. Pinned via a controlled interleaving: the microphone write
        must NOT even start its PUT until the speaker write (which acquired
        the shared lock first) has fully finished — proving the lock forces
        real serialization, not just accidental non-overlap."""
        from custom_components.bosch_shc_camera.number import (
            BoschMicrophoneLevelNumber,
            BoschSpeakerLevelNumber,
        )

        cam_id = CAM_ID_GEN2_OUTDOOR
        coord = _coord(cam_id=cam_id, audio_cache={cam_id: dict(_AUDIO_CFG)})

        release = asyncio.Event()
        call_log: list[str] = []

        async def _slow_put(_cam_id: str, _endpoint: str, _body: dict) -> bool:
            call_log.append("put_start")
            await release.wait()
            return True

        coord.async_put_camera = _slow_put

        speaker_ent = _make_entity_intrusion(BoschSpeakerLevelNumber, coord, cam_id)
        mic_ent = _make_entity_intrusion(BoschMicrophoneLevelNumber, coord, cam_id)

        task_a = asyncio.create_task(speaker_ent.async_set_native_value(90.0))
        await asyncio.sleep(0)  # let task_a acquire the lock and reach the PUT await
        task_b = asyncio.create_task(mic_ent.async_set_native_value(10.0))
        await asyncio.sleep(0)  # task_b must block on the lock, not reach the PUT

        assert call_log == ["put_start"], (
            "microphone write must not start its PUT while the speaker "
            "write still holds the shared lock"
        )

        release.set()
        await task_a
        await task_b

        assert coord.audio_cache[cam_id]["speakerLevel"] == 90
        assert coord.audio_cache[cam_id]["microphoneLevel"] == 10


class TestBoschIntrusionSensitivityNumber:
    """PIN_EVERY_MODE: min(0) / max(7) / default(3) / garbage-clamp / PUT body / write-lock."""

    def _make(self, cam_id=CAM_ID_GEN2_OUTDOOR, intrusion_cache=None, put_return=True):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
        )

        ic = (
            {cam_id: dict(_INTRUSION_CFG)}
            if intrusion_cache is None
            else intrusion_cache
        )
        coord = _coord(cam_id=cam_id, intrusion_cache=ic, put_return=put_return)
        ent = _make_entity_intrusion(BoschIntrusionSensitivityNumber, coord, cam_id)
        return ent, coord

    # --- native_value ---

    def test_native_value_default(self):
        ent, _ = self._make()
        assert ent.native_value == 3.0

    def test_native_value_min(self):
        ent, _ = self._make(
            intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"sensitivity": 0, "distance": 5}}
        )
        assert ent.native_value == 0.0

    def test_native_value_max(self):
        ent, _ = self._make(
            intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"sensitivity": 7, "distance": 5}}
        )
        assert ent.native_value == 7.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.native_value is None

    # --- available ---

    def test_available_true_when_cache_populated(self):
        ent, _ = self._make()
        assert ent.available is True

    def test_available_false_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.available is False

    # --- async_set_native_value: PIN_EVERY_MODE ---

    @pytest.mark.asyncio
    async def test_set_min_sensitivity(self):
        ent, coord = self._make()
        await ent.async_set_native_value(0.0)
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "intrusionDetectionConfig"
        assert body["sensitivity"] == 0

    @pytest.mark.asyncio
    async def test_set_max_sensitivity(self):
        ent, coord = self._make()
        await ent.async_set_native_value(7.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 7

    @pytest.mark.asyncio
    async def test_set_default_sensitivity(self):
        ent, coord = self._make()
        await ent.async_set_native_value(5.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 5

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_to_max(self):
        """Values above 7 must be clamped to 7."""
        ent, coord = self._make()
        await ent.async_set_native_value(99.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 7

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_to_min(self):
        """Negative values must be clamped to 0."""
        ent, coord = self._make()
        await ent.async_set_native_value(-5.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["sensitivity"] == 0

    @pytest.mark.asyncio
    async def test_put_body_preserves_other_fields(self):
        """Full intrusion body must be sent — detectionMode + distance must not be dropped."""
        ent, coord = self._make()
        await ent.async_set_native_value(4.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert "detectionMode" in body
        assert "distance" in body
        assert "enabled" in body

    @pytest.mark.asyncio
    async def test_cache_updated_on_success(self):
        ent, coord = self._make()
        await ent.async_set_native_value(6.0)
        assert coord.intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["sensitivity"] == 6

    @pytest.mark.asyncio
    async def test_write_lock_set_on_success(self):
        """intrusion_config_set_at must be set after successful PUT."""
        ent, coord = self._make()
        before = time.monotonic()
        await ent.async_set_native_value(4.0)
        after = time.monotonic()
        assert CAM_ID_GEN2_OUTDOOR in coord.intrusion_config_set_at
        ts = coord.intrusion_config_set_at[CAM_ID_GEN2_OUTDOOR]
        assert before <= ts <= after

    @pytest.mark.asyncio
    async def test_no_cache_update_on_failure(self):
        ent, coord = self._make(put_return=False)
        original_sensitivity = coord.intrusion_config_cache[CAM_ID_GEN2_OUTDOOR][
            "sensitivity"
        ]
        await ent.async_set_native_value(7.0)
        assert (
            coord.intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["sensitivity"]
            == original_sensitivity
        )

    @pytest.mark.asyncio
    async def test_no_write_lock_on_failure(self):
        ent, coord = self._make(put_return=False)
        await ent.async_set_native_value(7.0)
        assert CAM_ID_GEN2_OUTDOOR not in coord.intrusion_config_set_at

    @pytest.mark.asyncio
    async def test_empty_cache_is_noop(self):
        ent, coord = self._make(intrusion_cache={})
        await ent.async_set_native_value(5.0)
        coord.async_put_camera.assert_not_called()

    # --- Gen1 skip (no intrusion cache → noop) ---

    @pytest.mark.asyncio
    async def test_gen1_not_wired_no_cache(self):
        """Gen1 has no intrusionDetectionConfig endpoint — cache stays empty, entity is unavailable."""
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
        )

        coord = _coord(cam_id=CAM_ID_GEN1, hw="CAMERA_360", intrusion_cache={})
        ent = _make_entity_intrusion(
            BoschIntrusionSensitivityNumber, coord, CAM_ID_GEN1
        )
        assert ent.available is False
        assert ent.native_value is None

    # --- metadata ---

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "intrusion_sensitivity"

    def test_unique_id(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert (
            ent._attr_unique_id
            == f"bosch_shc_camera_{CAM_ID_GEN2_OUTDOOR}_intrusion_sensitivity"
        )

    def test_range_is_0_to_7(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.native_min_value == 0
        assert ent.native_max_value == 7

    def test_enabled_by_default(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionSensitivityNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.entity_registry_enabled_default is True

    # --- Gen2 Indoor II also gets the entity ---

    def test_available_gen2_indoor(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionSensitivityNumber,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            intrusion_cache={CAM_ID_GEN2_INDOOR: dict(_INTRUSION_CFG)},
        )
        ent = _make_entity_intrusion(
            BoschIntrusionSensitivityNumber, coord, CAM_ID_GEN2_INDOOR
        )
        assert ent.available is True
        assert ent.native_value == 3.0


# BoschIntrusionDistanceNumber
#
# Includes the distance-clamp regression: number.py used to clamp with
# min(10, value), but the Bosch cloud API rejects distance > 8 with HTTP 400
# ("must be less than or equal to 8") on FW 9.40.102 — setting 9 or 10
# produced a doomed PUT and left the entity stuck. Fix: clamp to min(8, value)
# and _attr_native_max_value = 8.


class TestBoschIntrusionDistanceNumber:
    """PIN_EVERY_MODE: min(1) / max(8, was 10) / default(8 per capture) / garbage-clamp / write-lock."""

    def _make(self, cam_id=CAM_ID_GEN2_OUTDOOR, intrusion_cache=None, put_return=True):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        ic = (
            {cam_id: dict(_INTRUSION_CFG)}
            if intrusion_cache is None
            else intrusion_cache
        )
        coord = _coord(cam_id=cam_id, intrusion_cache=ic, put_return=put_return)
        ent = _make_entity_intrusion(BoschIntrusionDistanceNumber, coord, cam_id)
        return ent, coord

    # --- native_value ---

    def test_native_value_default(self):
        ent, _ = self._make()
        assert (
            ent.native_value == 8.0
        )  # from _INTRUSION_CFG distance=8 (capture 2026-04-28)

    def test_native_value_min(self):
        ent, _ = self._make(
            intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"distance": 1, "sensitivity": 3}}
        )
        assert ent.native_value == 1.0

    def test_native_value_max(self):
        ent, _ = self._make(
            intrusion_cache={CAM_ID_GEN2_OUTDOOR: {"distance": 10, "sensitivity": 3}}
        )
        assert ent.native_value == 10.0

    def test_native_value_none_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.native_value is None

    # --- available ---

    def test_available_true_when_cache_populated(self):
        ent, _ = self._make()
        assert ent.available is True

    def test_available_false_when_cache_empty(self):
        ent, _ = self._make(intrusion_cache={})
        assert ent.available is False

    # --- async_set_native_value: PIN_EVERY_MODE ---

    @pytest.mark.asyncio
    async def test_set_min_distance(self):
        ent, coord = self._make()
        await ent.async_set_native_value(1.0)
        _, endpoint, body = coord.async_put_camera.call_args[0]
        assert endpoint == "intrusionDetectionConfig"
        assert body["distance"] == 1

    @pytest.mark.asyncio
    async def test_set_max_distance(self):
        ent, coord = self._make()
        await ent.async_set_native_value(8.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 8  # max 8 — API rejects > 8 (HTTP 400, FW 9.40.102)

    @pytest.mark.asyncio
    async def test_set_default_distance(self):
        ent, coord = self._make()
        await ent.async_set_native_value(4.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 4

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_above_max(self):
        ent, coord = self._make()
        await ent.async_set_native_value(999.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 8  # clamp to 8 — API rejects > 8 (HTTP 400)

    @pytest.mark.asyncio
    async def test_set_garbage_clamps_below_min(self):
        ent, coord = self._make()
        await ent.async_set_native_value(-3.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert body["distance"] == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "input_val,expected",
        [
            (9, 8),  # was accepted before fix, now clamped — prevents HTTP 400
            (10, 8),  # was accepted before fix, now clamped — prevents HTTP 400
            (8, 8),  # boundary: must pass through unchanged
            (7, 7),  # normal value: must pass through unchanged
            (1, 1),  # minimum: must pass through unchanged
            (5, 5),  # mid-range: must pass through unchanged
        ],
    )
    async def test_set_native_value_clamp_regression(
        self, input_val: int, expected: int
    ):
        """Explicit regression matrix for the min(10, value) → min(8, value)
        fix: values 9 and 10 (accepted by the old buggy clamp) must now be
        clamped to 8; values 1-8 must pass through unchanged."""
        ent, coord = self._make()
        await ent.async_set_native_value(float(input_val))
        actual = coord.intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["distance"]
        assert actual == expected, (
            f"input={input_val}: expected clamped distance={expected}, got {actual}"
        )
        coord.async_put_camera.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_native_value_9_does_not_send_9_to_api(self):
        """Explicit regression: value=9 (the exact historical bug input) must
        never appear in the PUT payload."""
        ent, coord = self._make()
        await ent.async_set_native_value(9.0)
        call_args = coord.async_put_camera.call_args[0]
        sent_cfg = call_args[2]
        assert sent_cfg["distance"] == 8, (
            f"PUT payload must contain distance=8, got {sent_cfg['distance']}"
        )

    @pytest.mark.asyncio
    async def test_put_body_preserves_other_fields(self):
        """detectionMode and sensitivity must survive the PUT body."""
        ent, coord = self._make()
        await ent.async_set_native_value(5.0)
        _, _, body = coord.async_put_camera.call_args[0]
        assert "sensitivity" in body
        assert "detectionMode" in body
        assert "enabled" in body

    @pytest.mark.asyncio
    async def test_cache_updated_on_success(self):
        ent, coord = self._make()
        await ent.async_set_native_value(7.0)
        assert coord.intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["distance"] == 7

    @pytest.mark.asyncio
    async def test_write_lock_set_on_success(self):
        ent, coord = self._make()
        before = time.monotonic()
        await ent.async_set_native_value(5.0)
        after = time.monotonic()
        assert CAM_ID_GEN2_OUTDOOR in coord.intrusion_config_set_at
        ts = coord.intrusion_config_set_at[CAM_ID_GEN2_OUTDOOR]
        assert before <= ts <= after

    @pytest.mark.asyncio
    async def test_no_cache_update_on_failure(self):
        ent, coord = self._make(put_return=False)
        original_distance = coord.intrusion_config_cache[CAM_ID_GEN2_OUTDOOR][
            "distance"
        ]
        await ent.async_set_native_value(2.0)
        assert (
            coord.intrusion_config_cache[CAM_ID_GEN2_OUTDOOR]["distance"]
            == original_distance
        )

    @pytest.mark.asyncio
    async def test_no_write_lock_on_failure(self):
        ent, coord = self._make(put_return=False)
        await ent.async_set_native_value(2.0)
        assert CAM_ID_GEN2_OUTDOOR not in coord.intrusion_config_set_at

    @pytest.mark.asyncio
    async def test_empty_cache_is_noop(self):
        ent, coord = self._make(intrusion_cache={})
        await ent.async_set_native_value(5.0)
        coord.async_put_camera.assert_not_called()

    # --- metadata ---

    def test_translation_key(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent._attr_translation_key == "intrusion_distance"

    def test_unique_id(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert (
            ent._attr_unique_id
            == f"bosch_shc_camera_{CAM_ID_GEN2_OUTDOOR}_intrusion_distance"
        )

    def test_range_is_1_to_8(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.native_min_value == 1
        assert ent.native_max_value == 8  # API rejects > 8 (HTTP 400, FW 9.40.102)

    def test_unit_is_meters(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        # Use private attr — public unit_of_measurement property requires entity platform context
        assert ent._attr_native_unit_of_measurement == "m"

    def test_enabled_by_default(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        coord = _coord(intrusion_cache={})
        ent = BoschIntrusionDistanceNumber(coord, CAM_ID_GEN2_OUTDOOR, _entry())
        assert ent.entity_registry_enabled_default is True

    # --- Gen2 Indoor II also gets the entity ---

    def test_available_gen2_indoor(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            intrusion_cache={CAM_ID_GEN2_INDOOR: dict(_INTRUSION_CFG)},
        )
        ent = _make_entity_intrusion(
            BoschIntrusionDistanceNumber, coord, CAM_ID_GEN2_INDOOR
        )
        assert ent.available is True
        assert ent.native_value == 8.0

    # --- Gen1 not wired ---

    def test_gen1_no_cache_unavailable(self):
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
        )

        coord = _coord(cam_id=CAM_ID_GEN1, hw="CAMERA_EYES", intrusion_cache={})
        ent = _make_entity_intrusion(BoschIntrusionDistanceNumber, coord, CAM_ID_GEN1)
        assert ent.available is False
        assert ent.native_value is None


class TestAsyncSetupEntryWiring:
    """Verify that setup_entry adds the correct entities for Gen1 and Gen2 cameras."""

    @pytest.mark.asyncio
    async def test_gen2_outdoor_gets_intrusion_entities(self):
        """Gen2 Outdoor II must produce BoschIntrusionSensitivityNumber + BoschIntrusionDistanceNumber."""
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
            BoschIntrusionSensitivityNumber,
            async_setup_entry,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN2_OUTDOOR,
            hw="HOME_Eyes_Outdoor",
            audio_cache={CAM_ID_GEN2_OUTDOOR: dict(_AUDIO_CFG)},
            intrusion_cache={CAM_ID_GEN2_OUTDOOR: dict(_INTRUSION_CFG)},
        )
        entry = SimpleNamespace(
            runtime_data=coord, data={}, options={}, entry_id="01ENTRY"
        )
        added: list = []
        await async_setup_entry(None, entry, lambda ents, **_: added.extend(ents))  # type: ignore[arg-type]

        types = [type(e) for e in added]
        assert BoschIntrusionSensitivityNumber in types
        assert BoschIntrusionDistanceNumber in types

    @pytest.mark.asyncio
    async def test_gen2_indoor_gets_intrusion_entities(self):
        """Gen2 Indoor II must also produce the intrusion number entities."""
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
            BoschIntrusionSensitivityNumber,
            async_setup_entry,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN2_INDOOR,
            hw="HOME_Eyes_Indoor",
            audio_cache={CAM_ID_GEN2_INDOOR: dict(_AUDIO_CFG)},
            intrusion_cache={CAM_ID_GEN2_INDOOR: dict(_INTRUSION_CFG)},
        )
        entry = SimpleNamespace(
            runtime_data=coord, data={}, options={}, entry_id="01ENTRY"
        )
        added: list = []
        await async_setup_entry(None, entry, lambda ents, **_: added.extend(ents))  # type: ignore[arg-type]

        types = [type(e) for e in added]
        assert BoschIntrusionSensitivityNumber in types
        assert BoschIntrusionDistanceNumber in types

    @pytest.mark.asyncio
    async def test_gen1_does_not_get_intrusion_entities(self):
        """Gen1 must NOT get intrusion number entities (no endpoint on Gen1)."""
        from custom_components.bosch_shc_camera.number import (
            BoschIntrusionDistanceNumber,
            BoschIntrusionSensitivityNumber,
            async_setup_entry,
        )

        coord = _coord(
            cam_id=CAM_ID_GEN1,
            hw="CAMERA_360",
            audio_cache={},
            intrusion_cache={},
        )
        entry = SimpleNamespace(
            runtime_data=coord, data={}, options={}, entry_id="01ENTRY"
        )
        added: list = []
        await async_setup_entry(None, entry, lambda ents, **_: added.extend(ents))  # type: ignore[arg-type]

        types = [type(e) for e in added]
        assert BoschIntrusionSensitivityNumber not in types
        assert BoschIntrusionDistanceNumber not in types

    def test_pan_number_added_only_for_pan_cameras(self):
        """BoschPanNumber must only appear for cameras with panLimit > 0."""
        from custom_components.bosch_shc_camera.number import async_setup_entry

        coord = _stub_coord_guards()
        coord.data[CAM_ID]["info"]["featureSupport"]["panLimit"] = 120
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschPanNumber" in entity_classes, (
            "BoschPanNumber must be added when panLimit > 0"
        )

    def test_pan_number_skipped_for_no_pan(self):
        """BoschPanNumber must be absent when panLimit=0."""
        from custom_components.bosch_shc_camera.number import async_setup_entry

        coord = _stub_coord_guards()
        coord.data[CAM_ID]["info"]["featureSupport"]["panLimit"] = 0
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschPanNumber" not in entity_classes, (
            "BoschPanNumber must be skipped when panLimit=0"
        )

    def test_front_light_intensity_added_when_has_light(self):
        """BoschFrontLightIntensityNumber must appear when featureSupport.light=True."""
        from custom_components.bosch_shc_camera.number import async_setup_entry

        coord = _stub_coord_guards()
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = True
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschFrontLightIntensityNumber" in entity_classes, (
            "FrontLightIntensityNumber must be added when has_light=True"
        )

    def test_gen2_entities_added_for_gen2_outdoor(self):
        """LensElevation + MicrophoneLevel must appear for Gen2 cameras."""
        from custom_components.bosch_shc_camera.number import async_setup_entry

        coord = _stub_coord_guards()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschLensElevationNumber" in entity_classes, (
            "LensElevationNumber must be added for Gen2 camera"
        )
        assert "BoschMicrophoneLevelNumber" in entity_classes, (
            "MicrophoneLevelNumber must be added for Gen2 camera"
        )

    def test_gen2_outdoor_lights_present(self):
        """WhiteBalance + TopLed + BottomLed brightness must appear for Gen2 Outdoor."""
        from custom_components.bosch_shc_camera.number import async_setup_entry

        coord = _stub_coord_guards()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschWhiteBalanceNumber" in entity_classes, (
            "WhiteBalanceNumber must be added for Gen2 Outdoor"
        )
        assert "BoschTopLedBrightnessNumber" in entity_classes, (
            "TopLedBrightnessNumber must be added for Gen2 Outdoor"
        )

    def test_indoor_ii_alarm_entities_added(self):
        """AlarmDelay + PreAlarmDelay must appear for HOME_Eyes_Indoor."""
        from custom_components.bosch_shc_camera.number import async_setup_entry

        coord = _stub_coord_guards()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschAlarmDelayNumber" in entity_classes, (
            "AlarmDelayNumber must be added for Gen2 Indoor II"
        )
        assert "BoschPreAlarmDelayNumber" in entity_classes, (
            "PreAlarmDelayNumber must be added for Gen2 Indoor II"
        )

    def test_white_balance_not_added_for_indoor_ii(self):
        """WhiteBalanceNumber must NOT appear for HOME_Eyes_Indoor (no RGB lights)."""
        from custom_components.bosch_shc_camera.number import async_setup_entry

        coord = _stub_coord_guards()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        entry = SimpleNamespace(runtime_data=coord, options={})
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschWhiteBalanceNumber" not in entity_classes, (
            "WhiteBalanceNumber must NOT be added for HOME_Eyes_Indoor (Indoor II has no RGB lights)"
        )


def _make_hass():
    return SimpleNamespace(
        async_create_task=MagicMock(),
        services=SimpleNamespace(async_call=AsyncMock()),
        config=SimpleNamespace(time_zone="Europe/Berlin"),
    )


def _coord2(
    pan_cache=None,
    lens_elevation_cache=None,
    audio_cache=None,
    lighting_switch_cache=None,
    motion_light_cache=None,
    global_lighting_cache=None,
    icon_led_brightness_cache=None,
    alarm_settings_cache=None,
    shc_state_cache=None,
    hw="HOME_Eyes_Outdoor",
    **overrides,
):
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
            }
        },
        last_update_success=True,
        options={},
        token="test-token",
        pan_cache=pan_cache if pan_cache is not None else {},
        image_rotation_180={},
        lens_elevation_cache=lens_elevation_cache
        if lens_elevation_cache is not None
        else {},
        audio_cache=audio_cache if audio_cache is not None else {},
        lighting_switch_cache=lighting_switch_cache
        if lighting_switch_cache is not None
        else {},
        motion_light_cache=motion_light_cache if motion_light_cache is not None else {},
        global_lighting_cache=global_lighting_cache
        if global_lighting_cache is not None
        else {},
        icon_led_brightness_cache=icon_led_brightness_cache
        if icon_led_brightness_cache is not None
        else {},
        alarm_settings_cache=alarm_settings_cache
        if alarm_settings_cache is not None
        else {},
        alarm_settings_set_at={},
        shc_state_cache=shc_state_cache
        if shc_state_cache is not None
        else {CAM_ID: {}},
        async_put_camera=AsyncMock(return_value=True),
        is_camera_online=lambda cid: True,
        **overrides,
    )
    return coord


def _make_front_light_intensity_r2(shc_state_cache=None):
    from custom_components.bosch_shc_camera.number import BoschFrontLightIntensityNumber

    coord = _coord2(shc_state_cache=shc_state_cache or {CAM_ID: {}})
    coord.async_cloud_set_light_component = AsyncMock()
    sw = BoschFrontLightIntensityNumber.__new__(BoschFrontLightIntensityNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


@pytest.fixture
def stub_coord_gen2() -> SimpleNamespace:
    return _stub_coord_gen2_factory()


def _stub_coord_gen2_factory(**overrides: object) -> SimpleNamespace:
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True, "panLimit": 0},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        shc_state_cache={CAM_ID: {"front_light_intensity": 0.5}},
        pan_cache={},
        lens_elevation_cache={},
        audio_cache={},
        audio_volume={},
        lighting_switch_cache={},
        image_rotation_180={},
        last_update_success=True,
        token="tok-A",
        options={},
        motion_settings=lambda cid: {},
        async_put_camera=AsyncMock(return_value=True),
        async_cloud_set_light_component=AsyncMock(),
        is_camera_online=lambda cid: True,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestFrontLightIntensityNumber:
    def test_native_value_scaled_from_cache(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        stub_coord_gen2.shc_state_cache[CAM_ID]["front_light_intensity"] = 0.75
        entity = BoschFrontLightIntensityNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity.native_value == 75, "Must scale 0.75 API value to 75 percent"

    def test_native_value_none_when_missing(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        stub_coord_gen2.shc_state_cache[CAM_ID] = {}
        entity = BoschFrontLightIntensityNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity.native_value is None, (
            "Must return None when intensity not in cache"
        )

    def test_unavailable_without_cache(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Regression: with no front-light data cached the entity must report
        unavailable, not 'unknown' (available=True + native_value=None). Matches
        the cache-gating of the sibling number entities."""
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        stub_coord_gen2.shc_state_cache[CAM_ID] = {}
        entity = BoschFrontLightIntensityNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity.available is False

    def test_available_with_cache(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        stub_coord_gen2.shc_state_cache[CAM_ID] = {"front_light_intensity": 0.5}
        entity = BoschFrontLightIntensityNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_set_calls_cloud_light_component(self):
        """async_set_native_value must scale the percent value back to 0-1 and
        delegate to async_cloud_set_light_component."""
        sw = _make_front_light_intensity_r2()
        await sw.async_set_native_value(60.0)
        sw.coordinator.async_cloud_set_light_component.assert_awaited_once_with(
            CAM_ID, "intensity", 0.6
        )

    def test_device_info(self):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        e = _make_entity_guards(BoschFrontLightIntensityNumber)
        info = e.device_info
        assert info["model"] == "Eyes Outdoor"


def _make_lens_elevation_r2(elevation_cache=None):
    from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

    coord = _coord2(lens_elevation_cache=elevation_cache or {})
    sw = BoschLensElevationNumber.__new__(BoschLensElevationNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


class TestLensElevationNumber:
    def test_native_value(self):
        sw = _make_lens_elevation_r2(elevation_cache={CAM_ID: 2.5})
        assert sw.native_value == 2.5

    def test_native_value_none_when_missing(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        entity = BoschLensElevationNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when not yet fetched"

    def test_available_true(self):
        sw = _make_lens_elevation_r2(elevation_cache={CAM_ID: 2.0})
        assert sw.available is True

    def test_available_false(self):
        sw = _make_lens_elevation_r2(elevation_cache={})
        assert sw.available is False

    def test_range_constants(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        entity = BoschLensElevationNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity._attr_native_min_value == 0.5, "Min elevation must be 0.5 m"
        assert entity._attr_native_max_value == 5.0, "Max elevation must be 5.0 m"

    @pytest.mark.asyncio
    async def test_set_updates_cache_and_calls_put(self):
        """Pins the exact PUT call signature (endpoint literal "lens_elevation")."""
        sw = _make_lens_elevation_r2(elevation_cache={CAM_ID: 2.0})
        await sw.async_set_native_value(3.0)
        sw.coordinator.async_put_camera.assert_awaited_once_with(
            CAM_ID, "lens_elevation", {"elevation": 3.0}
        )
        assert sw.coordinator.lens_elevation_cache[CAM_ID] == 3.0

    @pytest.mark.asyncio
    async def test_set_value_puts_rounded_value(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Pins float-rounding of the elevation value sent in the PUT body."""
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        entity = BoschLensElevationNumber(stub_coord_gen2, CAM_ID, stub_entry)
        entity.async_write_ha_state = MagicMock()
        await entity.async_set_native_value(2.123)
        call_args = stub_coord_gen2.async_put_camera.call_args
        assert call_args[0][2]["elevation"] == round(2.123, 2), (
            "Must PUT rounded elevation value"
        )
        assert stub_coord_gen2.lens_elevation_cache[CAM_ID] == 2.123, (
            "Cache must be updated immediately"
        )

    @pytest.mark.asyncio
    async def test_cache_untouched_on_put_failure(self):
        """Regression: a failed PUT must not poison the cache with the
        user-entered value (native_value reads that cache directly)."""
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        coord = _stub_coord_guards(async_put_camera=AsyncMock(return_value=False))
        e = _make_entity_guards(BoschLensElevationNumber, coord)
        await e.async_set_native_value(12.0)
        assert CAM_ID not in coord.lens_elevation_cache, (
            "lens_elevation cache must stay empty when the PUT fails"
        )

    @pytest.mark.asyncio
    async def test_cache_set_on_put_success(self):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        coord = _stub_coord_guards(async_put_camera=AsyncMock(return_value=True))
        e = _make_entity_guards(BoschLensElevationNumber, coord)
        await e.async_set_native_value(12.0)
        assert coord.lens_elevation_cache[CAM_ID] == 12.0

    def test_gen2_base_device_info(self):
        """`_BoschGen2NumberBase.device_info` covered via any Gen2 subclass."""
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        e = _make_entity_guards(BoschLensElevationNumber)
        info = e.device_info
        assert info["manufacturer"] == "Bosch"


def _mock_aiohttp_session(status=200):
    @asynccontextmanager
    async def _put(*args, **kwargs):
        yield SimpleNamespace(status=status)

    session = MagicMock()
    session.put = _put
    return session


def _make_white_balance_r2(lighting_switch_cache=None):
    from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

    coord = _coord2(lighting_switch_cache=lighting_switch_cache or {})
    sw = BoschWhiteBalanceNumber.__new__(BoschWhiteBalanceNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._wb_value = None
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


class TestWhiteBalanceNumber:
    def test_native_value_from_cache(self):
        cache = {CAM_ID: {"frontLightSettings": {"whiteBalance": 0.5}}}
        sw = _make_white_balance_r2(lighting_switch_cache=cache)
        assert sw.native_value == 0.5

    def test_native_value_fallback_to_wb_value(self):
        sw = _make_white_balance_r2()
        sw._wb_value = 0.3
        assert sw.native_value == 0.3

    def test_native_value_none_when_cache_empty(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Via the real constructor: freshly built entity with no lighting
        cache and no remembered _wb_value must read as None."""
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        entity = BoschWhiteBalanceNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity.native_value is None, "Must return None when lighting cache empty"

    def test_caches_last_wb_value(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        stub_coord_gen2.lighting_switch_cache[CAM_ID] = {
            "frontLightSettings": {"brightness": 80, "whiteBalance": 0.3}
        }
        entity = BoschWhiteBalanceNumber(stub_coord_gen2, CAM_ID, stub_entry)
        val1 = entity.native_value
        stub_coord_gen2.lighting_switch_cache = {}  # clear cache
        val2 = entity.native_value  # must return remembered value
        assert val2 == 0.3, (
            "Must remember last read whiteBalance even after cache cleared"
        )

    def test_range_minus_one_to_one(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        entity = BoschWhiteBalanceNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert entity._attr_native_min_value == -1.0, "Min white balance must be -1.0"
        assert entity._attr_native_max_value == 1.0, "Max white balance must be 1.0"

    def test_available_true_when_coord_ok_and_cache_populated(self):
        """`available` requires coordinator.last_update_success AND a populated
        lighting cache — writing before the cache is populated would PUT
        zero-defaults and clobber real settings (bug-hunt 2026-06-02)."""
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord_guards(
            lighting_switch_cache={CAM_ID: {"frontLightSettings": {}}}
        )
        e = _make_entity_guards(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is True

    def test_available_false_when_cache_empty(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord_guards()  # lighting_switch_cache={} by default
        e = _make_entity_guards(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is False

    def test_available_false_when_coord_failed(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord_guards(last_update_success=False)
        e = _make_entity_guards(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is False

    @pytest.mark.asyncio
    async def test_set_success_via_aiohttp_mock(self):
        sw = _make_white_balance_r2()
        session = _mock_aiohttp_session(200)
        resp_json = {
            "frontLightSettings": {"brightness": 0, "whiteBalance": 0.2, "color": None}
        }

        @asynccontextmanager
        async def _put(*args, **kwargs):
            yield SimpleNamespace(status=200, json=AsyncMock(return_value=resp_json))

        session.put = _put
        with patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=session,
        ):
            await sw.async_set_native_value(0.2)
        assert sw._wb_value == 0.2

    @pytest.mark.asyncio
    async def test_set_non_200_leaves_value_unchanged(self):
        """Write delegates to coordinator.async_put_camera (which owns the
        401-retry); a failed write must not optimistically update the value."""
        sw = _make_white_balance_r2()
        sw.coordinator.async_put_camera = AsyncMock(return_value=False)
        await sw.async_set_native_value(0.5)
        assert sw._wb_value is None
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_set_exception_does_not_crash(self):
        import aiohttp

        @asynccontextmanager
        async def _bad_put(*args, **kwargs):
            raise aiohttp.ClientError("net err")
            yield

        session = MagicMock()
        session.put = _bad_put
        sw = _make_white_balance_r2()
        with patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=session,
        ):
            await sw.async_set_native_value(0.3)
        sw.async_write_ha_state.assert_called()

    @pytest.mark.asyncio
    async def test_success_updates_value_and_cache_via_coordinator(self):
        """Success updates the value and the local cache (from the body just
        sent); a failed write changes neither (bug-hunt 2026-06-02 — was a raw
        Bearer PUT that silently failed on 401)."""
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord_guards()
        coord.async_put_camera = AsyncMock(return_value=True)
        e = _make_entity_guards(BoschWhiteBalanceNumber, coord=coord)

        await e.async_set_native_value(0.5)

        coord.async_put_camera.assert_awaited_once()
        assert e._wb_value == 0.5
        assert (
            coord.lighting_switch_cache[CAM_ID]["frontLightSettings"]["whiteBalance"]
            == 0.5
        )

    @pytest.mark.asyncio
    async def test_write_preserves_other_lighting_groups(self):
        """A light write must merge ONLY its own group into the shared lighting
        cache, not overwrite the whole snapshot — otherwise a concurrent
        sibling write to another light group is clobbered (bug-hunt 2026-06-02)."""
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord_guards(
            lighting_switch_cache={CAM_ID: {"topLedLightSettings": {"brightness": 77}}}
        )
        coord.async_put_camera = AsyncMock(return_value=True)
        e = _make_entity_guards(BoschWhiteBalanceNumber, coord=coord)

        await e.async_set_native_value(0.5)

        cache = coord.lighting_switch_cache[CAM_ID]
        assert cache["frontLightSettings"]["whiteBalance"] == 0.5  # our write
        assert cache["topLedLightSettings"]["brightness"] == 77  # sibling kept


def _make_top_led_r2(lighting_switch_cache=None):
    from custom_components.bosch_shc_camera.number import BoschTopLedBrightnessNumber

    coord = _coord2(lighting_switch_cache=lighting_switch_cache or {})
    sw = BoschTopLedBrightnessNumber.__new__(BoschTopLedBrightnessNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._brightness = None
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


class TestTopLedBrightnessNumber:
    def test_native_value_from_cache(self):
        cache = {CAM_ID: {"topLedLightSettings": {"brightness": 80}}}
        sw = _make_top_led_r2(lighting_switch_cache=cache)
        assert sw.native_value == 80.0

    def test_native_value_fallback(self):
        sw = _make_top_led_r2()
        sw._brightness = 50.0
        assert sw.native_value == 50.0

    @pytest.mark.asyncio
    async def test_set_success_via_aiohttp_mock(self):
        sw = _make_top_led_r2()
        resp_json = {}

        @asynccontextmanager
        async def _put(*args, **kwargs):
            yield SimpleNamespace(status=204, json=AsyncMock(return_value=resp_json))

        session = MagicMock()
        session.put = _put
        with patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=session,
        ):
            await sw.async_set_native_value(60.0)
        assert sw._brightness == 60.0

    @pytest.mark.asyncio
    async def test_set_non_200_leaves_brightness_unchanged(self):
        sw = _make_top_led_r2()
        sw.coordinator.async_put_camera = AsyncMock(return_value=False)
        await sw.async_set_native_value(70.0)
        assert sw._brightness is None  # not updated on failed write

    def test_available_follows_coord_and_cache(self):
        """`available` requires last_update_success AND a populated lighting
        cache (bug-hunt 2026-06-02 — avoids writing zero-defaults before
        populate)."""
        from custom_components.bosch_shc_camera.number import (
            BoschTopLedBrightnessNumber,
        )

        coord = _stub_coord_guards(
            lighting_switch_cache={CAM_ID: {"topLedLightSettings": {}}}
        )
        e = _make_entity_guards(
            BoschTopLedBrightnessNumber, coord=coord, led_key="topLedLightSettings"
        )
        e._brightness = None
        assert e.available is True
        coord.last_update_success = False
        assert e.available is False

    def test_available_false_when_cache_empty(self):
        from custom_components.bosch_shc_camera.number import (
            BoschTopLedBrightnessNumber,
        )

        coord = _stub_coord_guards()  # empty lighting cache
        e = _make_entity_guards(
            BoschTopLedBrightnessNumber, coord=coord, led_key="topLedLightSettings"
        )
        assert e.available is False

    @pytest.mark.asyncio
    async def test_success_updates_brightness_and_cache_via_coordinator(self):
        """Writes delegate to coordinator.async_put_camera (401-retry owned
        there). Success updates brightness + the local cache from the body
        sent (bug-hunt 2026-06-02 — was a raw Bearer PUT silently failing on
        401)."""
        from custom_components.bosch_shc_camera.number import (
            BoschTopLedBrightnessNumber,
        )

        coord = _stub_coord_guards()
        coord.async_put_camera = AsyncMock(return_value=True)
        e = _make_entity_guards(
            BoschTopLedBrightnessNumber, coord=coord, led_key="topLedLightSettings"
        )
        e._brightness = None

        await e.async_set_native_value(80)

        coord.async_put_camera.assert_awaited_once()
        assert e._brightness == 80.0
        assert (
            coord.lighting_switch_cache[CAM_ID]["topLedLightSettings"]["brightness"]
            == 80
        )


class TestBottomLedBrightnessNumber:
    @pytest.mark.asyncio
    async def test_failed_write_keeps_prior_brightness(self):
        """A failed write (async_put_camera returns False — it swallows the
        connection/timeout/401 internally) must NOT crash the setter and must
        leave `_brightness` at the prior value (no optimistic update)."""
        from custom_components.bosch_shc_camera.number import (
            BoschBottomLedBrightnessNumber,
        )

        coord = _stub_coord_guards()
        coord.async_put_camera = AsyncMock(return_value=False)
        e = _make_entity_guards(
            BoschBottomLedBrightnessNumber,
            coord=coord,
            led_key="bottomLedLightSettings",
        )
        e._brightness = 33.0

        # Must not raise
        await e.async_set_native_value(80)

        # Failed write → no optimistic update
        assert e._brightness == 33.0


def _make_motion_light_sens_r2(motion_light_cache=None):
    from custom_components.bosch_shc_camera.number import (
        BoschMotionLightSensitivityNumber,
    )

    coord = _coord2(motion_light_cache=motion_light_cache or {})
    sw = BoschMotionLightSensitivityNumber.__new__(BoschMotionLightSensitivityNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


class TestMotionLightSensitivityNumber:
    def test_native_value(self):
        sw = _make_motion_light_sens_r2({CAM_ID: {"motionLightSensitivity": 3}})
        assert sw.native_value == 3.0

    def test_available_false_when_cache_empty(self):
        sw = _make_motion_light_sens_r2({})
        assert sw.available is False

    @pytest.mark.asyncio
    async def test_set_empty_cache_noop(self):
        sw = _make_motion_light_sens_r2({})
        await sw.async_set_native_value(3.0)
        sw.coordinator.async_put_camera.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_set_updates_cache(self):
        sw = _make_motion_light_sens_r2(
            {CAM_ID: {"motionLightSensitivity": 2, "duration": 30}}
        )
        await sw.async_set_native_value(4.0)
        sw.coordinator.async_put_camera.assert_awaited_once()
        body = sw.coordinator.async_put_camera.call_args[0][2]
        assert body["motionLightSensitivity"] == 4


def _make_darkness_threshold_r2(global_lighting_cache=None):
    from custom_components.bosch_shc_camera.number import BoschDarknessThresholdNumber

    coord = _coord2(global_lighting_cache=global_lighting_cache or {})
    sw = BoschDarknessThresholdNumber.__new__(BoschDarknessThresholdNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


class TestDarknessThresholdNumber:
    def test_native_value(self):
        sw = _make_darkness_threshold_r2(
            {CAM_ID: {"darknessThreshold": 0.47, "softLightFading": True}}
        )
        assert sw.native_value == 47.0

    def test_native_value_none(self):
        sw = _make_darkness_threshold_r2({})
        assert sw.native_value is None

    @pytest.mark.asyncio
    async def test_set_preserves_soft_fading(self):
        cache = {CAM_ID: {"darknessThreshold": 0.5, "softLightFading": True}}
        sw = _make_darkness_threshold_r2(cache)
        await sw.async_set_native_value(60.0)
        sw.coordinator.async_put_camera.assert_awaited_once()
        body = sw.coordinator.async_put_camera.call_args[0][2]
        assert body["darknessThreshold"] == pytest.approx(0.6, abs=0.0001)
        assert body["softLightFading"] is True

    def test_available_true_when_cache_populated(self):
        """`available` requires both coordinator-ok AND non-empty
        `global_lighting_cache` for this cam_id."""
        from custom_components.bosch_shc_camera.number import (
            BoschDarknessThresholdNumber,
        )

        coord = _stub_coord_guards()
        coord.global_lighting_cache[CAM_ID] = {"darknessThreshold": 0.5}
        e = _make_entity_guards(BoschDarknessThresholdNumber, coord=coord)
        assert e.available is True

    def test_available_false_when_cache_empty(self):
        from custom_components.bosch_shc_camera.number import (
            BoschDarknessThresholdNumber,
        )

        coord = _stub_coord_guards()
        # Empty cache → bool({}) is False
        e = _make_entity_guards(BoschDarknessThresholdNumber, coord=coord)
        assert e.available is False


def _make_power_led_r2(icon_led_cache=None):
    from custom_components.bosch_shc_camera.number import BoschPowerLedBrightnessNumber

    coord = _coord2(icon_led_brightness_cache=icon_led_cache or {})
    sw = BoschPowerLedBrightnessNumber.__new__(BoschPowerLedBrightnessNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


class TestPowerLedBrightnessNumber:
    def test_native_value(self):
        sw = _make_power_led_r2({CAM_ID: 3})
        assert sw.native_value == 3

    def test_available_true(self):
        sw = _make_power_led_r2({CAM_ID: 2})
        assert sw.available is True

    def test_available_false(self):
        sw = _make_power_led_r2({})
        assert sw.available is False

    @pytest.mark.asyncio
    async def test_set_value(self):
        sw = _make_power_led_r2({CAM_ID: 2})
        await sw.async_set_native_value(3.0)
        sw.coordinator.async_put_camera.assert_awaited_once_with(
            CAM_ID, "iconLedBrightness", {"value": 3}
        )

    @pytest.mark.asyncio
    async def test_set_clamps_max(self):
        sw = _make_power_led_r2({CAM_ID: 2})
        await sw.async_set_native_value(10.0)  # clamp to 4
        body = sw.coordinator.async_put_camera.call_args[0][2]
        assert body["value"] == 4

    @pytest.mark.asyncio
    async def test_set_clamps_min(self):
        sw = _make_power_led_r2({CAM_ID: 2})
        await sw.async_set_native_value(-5.0)  # clamp to 0
        body = sw.coordinator.async_put_camera.call_args[0][2]
        assert body["value"] == 0


def _make_alarm_delay_r2(alarm_settings=None):
    from custom_components.bosch_shc_camera.number import BoschAlarmDelayNumber

    coord = _coord2(
        alarm_settings_cache={CAM_ID: alarm_settings}
        if alarm_settings is not None
        else {}
    )
    sw = BoschAlarmDelayNumber.__new__(BoschAlarmDelayNumber)
    sw.coordinator = coord
    sw._cam_id = CAM_ID
    sw._cam_title = "Terrasse"
    sw._model_name = "Outdoor"
    sw._fw = "9.40.25"
    sw._mac = ""
    sw._field = "alarmDelayInSeconds"
    sw.hass = _make_hass()
    sw.async_write_ha_state = MagicMock()
    return sw


class TestAlarmDelayNumber:
    def test_native_value(self):
        sw = _make_alarm_delay_r2({"alarmDelayInSeconds": 60})
        assert sw.native_value == 60.0

    def test_native_value_none(self):
        sw = _make_alarm_delay_r2({})
        assert sw.native_value is None

    def test_available_false_no_settings(self):
        sw = _make_alarm_delay_r2({})
        assert sw.available is False

    @pytest.mark.asyncio
    async def test_set_updates_cache(self):
        sw = _make_alarm_delay_r2({"alarmDelayInSeconds": 60, "alarmMode": "ON"})
        await sw.async_set_native_value(90.0)
        sw.coordinator.async_put_camera.assert_awaited_once()
        body = sw.coordinator.async_put_camera.call_args[0][2]
        assert body["alarmDelayInSeconds"] == 90
        assert body["alarmMode"] == "ON"
        # bug-hunt 2026-06-02: write-lock stamped so the slow-tier poll won't revert.
        assert CAM_ID in sw.coordinator.alarm_settings_set_at

    @pytest.mark.asyncio
    async def test_set_empty_noop(self):
        sw = _make_alarm_delay_r2({})
        await sw.async_set_native_value(30.0)
        sw.coordinator.async_put_camera.assert_not_awaited()


# BoschAudioVolumeNumber — virtual card-playback-volume entity: seeds a
# default, stores the set value in the coordinator, and never calls the
# Bosch API (browser-side level). It is the automatable source of truth the
# card reads + writes.
class TestAudioVolumeNumber:
    def test_default_volume_when_unset(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschAudioVolumeNumber

        stub_coord_gen2.audio_volume = {}
        e = BoschAudioVolumeNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert e.native_value == 50.0  # default returned without pre-seeding

    @pytest.mark.asyncio
    async def test_set_value_stores_no_api_call(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschAudioVolumeNumber

        stub_coord_gen2.audio_volume = {}
        e = BoschAudioVolumeNumber(stub_coord_gen2, CAM_ID, stub_entry)
        e.async_write_ha_state = MagicMock()
        await e.async_set_native_value(75)
        assert stub_coord_gen2.audio_volume[CAM_ID] == 75
        assert e.native_value == 75.0
        # Virtual preference — no Bosch write must happen.
        stub_coord_gen2.async_put_camera.assert_not_called()

    def test_slider_metadata(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from homeassistant.components.number import NumberMode

        from custom_components.bosch_shc_camera.number import BoschAudioVolumeNumber

        e = BoschAudioVolumeNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert e._attr_native_min_value == 0
        assert e._attr_native_max_value == 100
        assert e._attr_mode == NumberMode.SLIDER

    def test_device_info_groups_under_camera_device(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.const import DOMAIN
        from custom_components.bosch_shc_camera.number import BoschAudioVolumeNumber

        e = BoschAudioVolumeNumber(stub_coord_gen2, CAM_ID, stub_entry)
        di = e.device_info
        assert (DOMAIN, CAM_ID) in di["identifiers"]
        assert di["manufacturer"] == "Bosch"
        assert di["name"].startswith("Bosch ")

    def test_available_when_camera_online(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschAudioVolumeNumber

        e = BoschAudioVolumeNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert e.available is True

    def test_unavailable_when_camera_offline(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Greys out together with the camera's other controls when offline."""
        from custom_components.bosch_shc_camera.number import BoschAudioVolumeNumber

        stub_coord_gen2.is_camera_online = lambda cid: False
        e = BoschAudioVolumeNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert e.available is False

    def test_unavailable_when_coordinator_update_failed(
        self, stub_coord_gen2: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.number import BoschAudioVolumeNumber

        stub_coord_gen2.last_update_success = False
        e = BoschAudioVolumeNumber(stub_coord_gen2, CAM_ID, stub_entry)
        assert e.available is False


# Alarm-delay privacy guard (relocated from tests/test_privacy_guard_branches.py
# — the light.py/switch.py siblings live in tests/test_light.py and
# tests/test_switch.py)
def _stub_coord_with_privacy_number(
    privacy_on: bool = False, hw: str = "HOME_Eyes_Indoor"
):
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                },
            }
        },
        shc_state_cache={CAM_ID: {"privacy_mode": privacy_on}},
        panic_alarm_cache={},
        alarm_settings_cache={},
        alarm_settings_set_at={},
        lighting_switch_cache={},
        light_set_at={},
        last_update_success=True,
        token="tok-A",
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
        async_update_listeners=MagicMock(),
    )


def _hass_stub_number():
    svc = SimpleNamespace(async_call=AsyncMock())
    return SimpleNamespace(services=svc)


class TestAlarmDelayPrivacyGuard:
    """Gen2 Indoor + privacy ON must abort `async_set_native_value` before
    any API PUT; Gen2 Outdoor is not in `_GEN2_INDOOR_HW` so the guard must
    not fire there regardless of the privacy cache."""

    def _make_entity(self, coord, klass_name="BoschAlarmDelayNumber"):
        import importlib

        mod = importlib.import_module("custom_components.bosch_shc_camera.number")
        klass = getattr(mod, klass_name)
        entry = SimpleNamespace(data={}, options={})
        entity = klass.__new__(klass)
        entity.coordinator = coord
        entity._cam_id = CAM_ID
        entity._entry = entry
        entity._cam_title = "Innenbereich"
        entity._model = "HOME_Eyes_Indoor"
        entity._model_name = "Eyes Indoor"
        entity._fw = "9.40.25"
        entity._mac = "aa:bb:cc:dd:ee:02"
        entity._field = "alarmDelayInSeconds"
        coord.alarm_settings_cache[CAM_ID] = {
            "alarmDelayInSeconds": 10,
            "sirenDurationInSeconds": 30,
        }
        entity.async_write_ha_state = MagicMock()
        entity.hass = _hass_stub_number()
        return entity

    @pytest.mark.asyncio
    async def test_set_value_blocked_for_gen2_indoor_with_privacy_on(self):
        """Gen2 Indoor + privacy ON → async_put_camera must NOT be called."""
        coord = _stub_coord_with_privacy_number(privacy_on=True, hw="HOME_Eyes_Indoor")
        entity = self._make_entity(coord)
        original_delay = coord.alarm_settings_cache[CAM_ID]["alarmDelayInSeconds"]

        await entity.async_set_native_value(15.0)

        coord.async_put_camera.assert_not_called()
        assert (
            coord.alarm_settings_cache[CAM_ID]["alarmDelayInSeconds"] == original_delay
        )

    @pytest.mark.asyncio
    async def test_set_value_proceeds_for_gen2_indoor_with_privacy_off(self):
        """Gen2 Indoor + privacy OFF → async_put_camera IS called."""
        coord = _stub_coord_with_privacy_number(privacy_on=False, hw="HOME_Eyes_Indoor")
        entity = self._make_entity(coord)

        await entity.async_set_native_value(15.0)

        coord.async_put_camera.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_value_proceeds_for_gen2_outdoor_regardless_of_privacy(self):
        """Gen2 Outdoor is NOT in `_GEN2_INDOOR_HW` → guard does NOT fire
        even when `shc_state_cache` says privacy_mode=True."""
        coord = _stub_coord_with_privacy_number(privacy_on=True, hw="HOME_Eyes_Outdoor")
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        entity = self._make_entity(coord)

        await entity.async_set_native_value(15.0)

        coord.async_put_camera.assert_called_once()


# Front-light-intensity notify-on-total-failure coverage (relocated from
# tests/test_switch_write_failure_warnings.py — the switch.py call sites for
# the same 2026-07-07 fix live in tests/test_switch.py)
def _coord_light_write_failure(**overrides: object) -> SimpleNamespace:
    coord = SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        async_cloud_set_camera_light=AsyncMock(return_value=False),
        async_cloud_set_light_component=AsyncMock(return_value=False),
        async_cloud_set_notifications=AsyncMock(return_value=False),
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


@pytest.mark.asyncio
class TestFrontLightIntensityNumberWarnsOnFailure:
    async def test_set_value_failure_warns(self, caplog: pytest.LogCaptureFixture):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        num = BoschFrontLightIntensityNumber(
            _coord_light_write_failure(), CAM_ID, entry
        )
        with caplog.at_level("WARNING"):
            await num.async_set_native_value(75.0)
        assert any("failed on all paths" in r.message for r in caplog.records)

    async def test_set_value_success_is_silent(self, caplog: pytest.LogCaptureFixture):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        num = BoschFrontLightIntensityNumber(
            _coord_light_write_failure(
                async_cloud_set_light_component=AsyncMock(return_value=True)
            ),
            CAM_ID,
            entry,
        )
        with caplog.at_level("WARNING"):
            await num.async_set_native_value(75.0)
        assert not any("failed on all paths" in r.message for r in caplog.records)
