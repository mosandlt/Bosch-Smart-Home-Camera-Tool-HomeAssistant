"""Number-entity guard + edge coverage (Bucket D).

Pins the missing number.py lines that no other test currently exercises:
  - L108     : BoschPanNumber.device_info — return dict.
  - L186     : BoschAudioThresholdNumber.device_info — return dict.
  - L224     : BoschAudioThresholdNumber.async_set_native_value early return when
                privacy is on (Gen2 indoor + _warn_if_privacy_on=True).
  - L282     : BoschSpeakerLevelNumber.device_info — return dict.
  - L362     : BoschFrontLightIntensityNumber.device_info — return dict.
  - L411     : _BoschGen2NumberBase.device_info — return dict.
  - L541     : BoschWhiteBalanceNumber.available — returns coordinator flag.
  - L569-570 : BoschWhiteBalanceNumber.async_set_native_value — `resp.json()`
                raises after 200 response → cache untouched, no crash.
  - L601     : _BoschLedBrightnessBase.available — returns coordinator flag.
  - L634-635 : _BoschLedBrightnessBase.async_set_native_value — `resp.json()`
                raises after 200 → cache untouched.
  - L639-640 : _BoschLedBrightnessBase.async_set_native_value — outer Exception
                (session.put raises) → warning logged, no crash.
  - L748     : BoschDarknessThresholdNumber.available — returns based on cache.
  - L943     : BoschAudioAlarmSensitivityNumber.available — returns based on
                settings.
  - L951     : BoschAudioAlarmSensitivityNumber.async_set_native_value early
                return when settings cache is empty.

Approach: bypass __init__ via `klass.__new__()`. Async aiohttp PUTs are
patched at the module level.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _stub_coord(**overrides):
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
        _shc_state_cache={
            CAM_ID: {"front_light_intensity": 0.5, "privacy_mode": False}
        },
        _pan_cache={},
        _lens_elevation_cache={},
        _audio_cache={},
        _lighting_switch_cache={},
        _motion_light_cache={},
        _global_lighting_cache={},
        _icon_led_brightness_cache={},
        _alarm_settings_cache={},
        _image_rotation_180={},
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


def _make_entity(
    klass, coord=None, *, led_key=None, field=None, mac="aa:bb:cc:dd:ee:01"
):
    """Bypass __init__ for number entities."""
    coord = coord or _stub_coord()
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


def _make_put_session(
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


# ── L108 / L186 / L282 / L362 / L411 — device_info returns ─────────────────


class TestDeviceInfoReturns:
    """Every number entity exposes `device_info` so HA renders them under the
    correct device. Pin the return-dict shape for each class hosting its own
    device_info property."""

    def test_pan_number_device_info(self):
        from custom_components.bosch_shc_camera.number import BoschPanNumber

        e = _make_entity(BoschPanNumber)
        info = e.device_info
        assert isinstance(info, dict)
        assert info["model"] == "Eyes Outdoor"
        assert info["sw_version"] == "9.40.25"

    def test_speaker_level_device_info(self):
        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

        e = _make_entity(BoschSpeakerLevelNumber)
        info = e.device_info
        assert info["manufacturer"] == "Bosch"

    def test_front_light_intensity_device_info(self):
        from custom_components.bosch_shc_camera.number import (
            BoschFrontLightIntensityNumber,
        )

        e = _make_entity(BoschFrontLightIntensityNumber)
        info = e.device_info
        assert info["model"] == "Eyes Outdoor"

    def test_gen2_base_device_info(self):
        """`_BoschGen2NumberBase.device_info` covered via any Gen2 subclass."""
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        e = _make_entity(BoschLensElevationNumber)
        info = e.device_info
        assert info["manufacturer"] == "Bosch"


# ── L541 — WhiteBalanceNumber.available ────────────────────────────────────


class TestWhiteBalanceAvailable:
    """`available` requires coordinator.last_update_success AND a populated
    lighting cache — writing before the cache is populated would PUT
    zero-defaults and clobber real settings (bug-hunt 2026-06-02)."""

    def test_available_true_when_coord_ok_and_cache_populated(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord(_lighting_switch_cache={CAM_ID: {"frontLightSettings": {}}})
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is True

    def test_available_false_when_cache_empty(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord()  # _lighting_switch_cache={} by default
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is False

    def test_available_false_when_coord_failed(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord(last_update_success=False)
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is False


# ── L569-570 — WhiteBalance resp.json swallow ──────────────────────────────


class TestWhiteBalanceWrite:
    """White-balance writes delegate to coordinator.async_put_camera (which owns
    the 401 → token-refresh + retry). Success updates the value and the local
    cache (from the body just sent); a failed write changes neither
    (bug-hunt 2026-06-02 — was a raw Bearer PUT that silently failed on 401)."""

    @pytest.mark.asyncio
    async def test_success_updates_value_and_cache(self):
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)

        await e.async_set_native_value(0.5)

        coord.async_put_camera.assert_awaited_once()
        assert e._wb_value == 0.5
        assert (
            coord._lighting_switch_cache[CAM_ID]["frontLightSettings"]["whiteBalance"]
            == 0.5
        )

    @pytest.mark.asyncio
    async def test_failed_write_leaves_value_unchanged(self):
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord()
        coord.async_put_camera = AsyncMock(return_value=False)
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)

        await e.async_set_native_value(0.5)

        assert e._wb_value is None


# ── L601 — LedBrightness available ─────────────────────────────────────────


class TestLedBrightnessAvailable:
    """`available` requires last_update_success AND a populated lighting cache
    (bug-hunt 2026-06-02 — avoids writing zero-defaults before populate)."""

    def test_available_follows_coord_and_cache(self):
        from custom_components.bosch_shc_camera.number import (
            BoschTopLedBrightnessNumber,
        )

        coord = _stub_coord(
            _lighting_switch_cache={CAM_ID: {"topLedLightSettings": {}}}
        )
        e = _make_entity(
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

        coord = _stub_coord()  # empty lighting cache
        e = _make_entity(
            BoschTopLedBrightnessNumber, coord=coord, led_key="topLedLightSettings"
        )
        assert e.available is False


# ── L634-635 — LedBrightness resp.json swallow ─────────────────────────────


class TestLedBrightnessWrite:
    """LED-brightness writes delegate to coordinator.async_put_camera (401-retry
    owned there). Success updates brightness + the local cache from the body
    sent (bug-hunt 2026-06-02 — was a raw Bearer PUT silently failing on 401)."""

    @pytest.mark.asyncio
    async def test_success_updates_brightness_and_cache(self):
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.number import (
            BoschTopLedBrightnessNumber,
        )

        coord = _stub_coord()
        coord.async_put_camera = AsyncMock(return_value=True)
        e = _make_entity(
            BoschTopLedBrightnessNumber, coord=coord, led_key="topLedLightSettings"
        )
        e._brightness = None

        await e.async_set_native_value(80)

        coord.async_put_camera.assert_awaited_once()
        assert e._brightness == 80.0
        assert (
            coord._lighting_switch_cache[CAM_ID]["topLedLightSettings"]["brightness"]
            == 80
        )


# ── L639-640 — LedBrightness outer Exception swallow ───────────────────────


class TestLedBrightnessRequestException:
    """A failed write (async_put_camera returns False — it swallows the
    connection/timeout/401 internally) must NOT crash the setter and must leave
    `_brightness` at the prior value (no optimistic update)."""

    @pytest.mark.asyncio
    async def test_failed_write_keeps_prior_brightness(self):
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.number import (
            BoschBottomLedBrightnessNumber,
        )

        coord = _stub_coord()
        coord.async_put_camera = AsyncMock(return_value=False)
        e = _make_entity(
            BoschBottomLedBrightnessNumber,
            coord=coord,
            led_key="bottomLedLightSettings",
        )
        e._brightness = 33.0

        # Must not raise
        await e.async_set_native_value(80)

        # Failed write → no optimistic update
        assert e._brightness == 33.0


# ── L748 — DarknessThreshold.available ─────────────────────────────────────


class TestDarknessThresholdAvailable:
    """`available` requires both coordinator-ok AND non-empty
    `_global_lighting_cache` for this cam_id."""

    def test_available_true_when_cache_populated(self):
        from custom_components.bosch_shc_camera.number import (
            BoschDarknessThresholdNumber,
        )

        coord = _stub_coord()
        coord._global_lighting_cache[CAM_ID] = {"darknessThreshold": 0.5}
        e = _make_entity(BoschDarknessThresholdNumber, coord=coord)
        assert e.available is True

    def test_available_false_when_cache_empty(self):
        from custom_components.bosch_shc_camera.number import (
            BoschDarknessThresholdNumber,
        )

        coord = _stub_coord()
        # Empty cache → bool({}) is False
        e = _make_entity(BoschDarknessThresholdNumber, coord=coord)
        assert e.available is False


# ── Write-path success-gating: a failed PUT must not poison the cache ─────────


class TestWritePathSuccessGating:
    """Regression: number setters must gate the optimistic cache write on the
    async_put_camera() bool result. A failed PUT previously poisoned the cache
    with the user-entered value; native_value reads that cache, so the slider
    showed the wrong value until the next slow-tier poll (~300 s)."""

    @pytest.mark.asyncio
    async def test_lens_elevation_cache_untouched_on_put_failure(self):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        coord = _stub_coord(async_put_camera=AsyncMock(return_value=False))
        e = _make_entity(BoschLensElevationNumber, coord)
        await e.async_set_native_value(12.0)
        assert CAM_ID not in coord._lens_elevation_cache, (
            "lens_elevation cache must stay empty when the PUT fails"
        )

    @pytest.mark.asyncio
    async def test_lens_elevation_cache_set_on_success(self):
        from custom_components.bosch_shc_camera.number import BoschLensElevationNumber

        coord = _stub_coord(async_put_camera=AsyncMock(return_value=True))
        e = _make_entity(BoschLensElevationNumber, coord)
        await e.async_set_native_value(12.0)
        assert coord._lens_elevation_cache[CAM_ID] == 12.0

    @pytest.mark.asyncio
    async def test_mic_level_cache_untouched_on_put_failure(self):
        from custom_components.bosch_shc_camera.number import (
            BoschMicrophoneLevelNumber,
        )

        coord = _stub_coord(
            async_put_camera=AsyncMock(return_value=False),
            _audio_cache={CAM_ID: {"microphoneLevel": 30}},
        )
        e = _make_entity(BoschMicrophoneLevelNumber, coord)
        await e.async_set_native_value(80.0)
        assert coord._audio_cache[CAM_ID]["microphoneLevel"] == 30, (
            "mic-level cache must keep its prior value when the PUT fails"
        )

    @pytest.mark.asyncio
    async def test_mic_level_cache_updated_on_success(self):
        from custom_components.bosch_shc_camera.number import (
            BoschMicrophoneLevelNumber,
        )

        coord = _stub_coord(
            async_put_camera=AsyncMock(return_value=True),
            _audio_cache={CAM_ID: {"microphoneLevel": 30}},
        )
        e = _make_entity(BoschMicrophoneLevelNumber, coord)
        await e.async_set_native_value(80.0)
        assert coord._audio_cache[CAM_ID]["microphoneLevel"] == 80


# ── bug-hunt 2026-06-02: cache-merge preserves sibling groups ──────────────────
class TestLightingCacheMerge:
    """A light write must merge ONLY its own group into the shared lighting
    cache, not overwrite the whole snapshot — otherwise a concurrent sibling
    write to another light group is clobbered."""

    @pytest.mark.asyncio
    async def test_white_balance_write_preserves_other_groups(self):
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord(
            _lighting_switch_cache={CAM_ID: {"topLedLightSettings": {"brightness": 77}}}
        )
        coord.async_put_camera = AsyncMock(return_value=True)
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)

        await e.async_set_native_value(0.5)

        cache = coord._lighting_switch_cache[CAM_ID]
        assert cache["frontLightSettings"]["whiteBalance"] == 0.5  # our write
        assert cache["topLedLightSettings"]["brightness"] == 77  # sibling kept

    @pytest.mark.asyncio
    async def test_speaker_write_preserves_microphone(self):
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera.number import BoschSpeakerLevelNumber

        coord = _stub_coord(_audio_cache={CAM_ID: {"microphoneLevel": 42}})
        coord.async_put_camera = AsyncMock(return_value=True)
        e = _make_entity(BoschSpeakerLevelNumber, coord=coord)

        await e.async_set_native_value(80.0)

        assert coord._audio_cache[CAM_ID]["speakerLevel"] == 80  # our write
        assert coord._audio_cache[CAM_ID]["microphoneLevel"] == 42  # sibling kept
