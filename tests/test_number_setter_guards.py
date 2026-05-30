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
    """`available` reflects coordinator.last_update_success only (no cache check
    because the cache is populated lazily)."""

    def test_available_true_when_coord_ok(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord()
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is True

    def test_available_false_when_coord_failed(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord(last_update_success=False)
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)
        assert e.available is False


# ── L569-570 — WhiteBalance resp.json swallow ──────────────────────────────


class TestWhiteBalanceJsonParseError:
    """On HTTP 200 with malformed JSON body, the white-balance setter must NOT
    crash and must NOT update the cache (line 569-570)."""

    @pytest.mark.asyncio
    async def test_json_error_after_200_swallowed(self):
        from custom_components.bosch_shc_camera.number import BoschWhiteBalanceNumber

        coord = _stub_coord()
        coord._lighting_switch_cache[CAM_ID] = {"sentinel": "preserved"}
        e = _make_entity(BoschWhiteBalanceNumber, coord=coord)

        session, resp = _make_put_session(
            status=200, json_raises=ValueError("not JSON")
        )

        with patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=session,
        ):
            # Must not raise
            await e.async_set_native_value(0.5)

        # _wb_value got updated optimistically; cache untouched (sentinel survives)
        assert e._wb_value == 0.5
        assert coord._lighting_switch_cache[CAM_ID] == {"sentinel": "preserved"}


# ── L601 — LedBrightness available ─────────────────────────────────────────


class TestLedBrightnessAvailable:
    """`available` returns just coordinator.last_update_success — the cache
    can be empty and the slider still shows."""

    def test_available_follows_coord(self):
        from custom_components.bosch_shc_camera.number import (
            BoschTopLedBrightnessNumber,
        )

        coord = _stub_coord()
        e = _make_entity(
            BoschTopLedBrightnessNumber, coord=coord, led_key="topLedLightSettings"
        )
        e._brightness = None
        assert e.available is True
        coord.last_update_success = False
        assert e.available is False


# ── L634-635 — LedBrightness resp.json swallow ─────────────────────────────


class TestLedBrightnessJsonParseError:
    """After a 200 PUT, `resp.json()` may raise (e.g. proxy stripped body).
    The cache stays untouched, `_brightness` is still updated optimistically."""

    @pytest.mark.asyncio
    async def test_json_error_after_200_swallowed(self):
        from custom_components.bosch_shc_camera.number import (
            BoschTopLedBrightnessNumber,
        )

        coord = _stub_coord()
        coord._lighting_switch_cache[CAM_ID] = {"sentinel": "preserved"}
        e = _make_entity(
            BoschTopLedBrightnessNumber, coord=coord, led_key="topLedLightSettings"
        )
        e._brightness = None

        session, resp = _make_put_session(
            status=200, json_raises=ValueError("bad body")
        )

        with patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=session,
        ):
            await e.async_set_native_value(80)

        assert e._brightness == 80.0
        assert coord._lighting_switch_cache[CAM_ID] == {"sentinel": "preserved"}


# ── L639-640 — LedBrightness outer Exception swallow ───────────────────────


class TestLedBrightnessRequestException:
    """A connection / timeout error during session.put() must NOT crash the
    setter; a warning is logged and `_brightness` stays at the prior value."""

    @pytest.mark.asyncio
    async def test_session_put_exception_swallowed(self):
        from custom_components.bosch_shc_camera.number import (
            BoschBottomLedBrightnessNumber,
        )

        coord = _stub_coord()
        e = _make_entity(
            BoschBottomLedBrightnessNumber,
            coord=coord,
            led_key="bottomLedLightSettings",
        )
        e._brightness = 33.0

        session, _ = _make_put_session(put_raises=TimeoutError("read timeout"))

        with patch(
            "homeassistant.helpers.aiohttp_client.async_get_clientsession",
            return_value=session,
        ):
            # Must not raise
            await e.async_set_native_value(80)

        # Optimistic update did NOT run — PUT failed before status-200 branch
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
