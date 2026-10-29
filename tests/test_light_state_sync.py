"""Gen2 light state consistency (GitHub #80).

The Front/Upper/Lower light entities read `lighting_switch_cache` live; the
Camera Light / Front Light / Wallwasher switches read fields derived from it.
The derivation used to run BEFORE the per-tick lighting/switch fetch, so the
switches lagged one tick (60 s) behind the lights and Camera Light flipped off
~30 s after a manual ON.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_smart_camera.shc import (
    _refresh_lighting_switch_after_on,
    async_cloud_set_camera_light,
)
from custom_components.bosch_smart_camera.slow_tier import (
    CamContext,
    _poll_cam_control,
    _poll_cam_info_caches,
    sync_gen2_light_cache,
)

CAM = "11111111-1111-1111-1111-111111111111"
OFF = {
    "frontLightSettings": {"brightness": 0},
    "topLedLightSettings": {"brightness": 0},
    "bottomLedLightSettings": {"brightness": 0},
}
ON = {
    "frontLightSettings": {"brightness": 100},
    "topLedLightSettings": {"brightness": 40},
    "bottomLedLightSettings": {"brightness": 40},
}


def _coord(lsc: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        lighting_switch_cache={CAM: lsc} if lsc is not None else {},
        light_set_at={},
        privacy_set_at={},
        notif_set_at={},
        live_connections={},
        WRITE_LOCK_SECS=30.0,
        shc_state_cache={},
        hw_version={CAM: "HOME_Eyes_Outdoor"},
        token="t",
        hass=MagicMock(),
        async_update_listeners=MagicMock(),
        async_request_refresh=AsyncMock(),
        spawn_tracked=MagicMock(side_effect=lambda coro, name: coro.close()),
        lighting_switch_locks={},
    )


def _resp(status: int, data: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def test_sync_derives_all_four_fields() -> None:
    coord = _coord(ON)
    sync_gen2_light_cache(coord, CAM)
    c = coord.shc_state_cache[CAM]
    assert (c["front_light"], c["wallwasher"], c["camera_light"]) == (True, True, True)
    assert c["front_light_intensity"] == 1.0


def test_sync_only_front_on() -> None:
    lsc = {**OFF, "frontLightSettings": {"brightness": 30}}
    coord = _coord(lsc)
    sync_gen2_light_cache(coord, CAM)
    c = coord.shc_state_cache[CAM]
    assert (c["front_light"], c["wallwasher"], c["camera_light"]) == (True, False, True)


def test_sync_noop_without_cache_and_inside_write_lock() -> None:
    coord = _coord()
    sync_gen2_light_cache(coord, CAM)
    assert coord.shc_state_cache == {}
    coord = _coord(ON)
    coord.shc_state_cache[CAM] = {"camera_light": False}
    coord.light_set_at[CAM] = time.monotonic()  # fresh write
    sync_gen2_light_cache(coord, CAM)
    assert coord.shc_state_cache[CAM]["camera_light"] is False
    coord.light_set_at[CAM] = time.monotonic() - 31  # lock expired
    sync_gen2_light_cache(coord, CAM)
    assert coord.shc_state_cache[CAM]["camera_light"] is True


@pytest.mark.asyncio
async def test_switches_follow_lights_within_the_same_tick() -> None:
    """Lights turned on in the app: lighting/switch fetch updates the switches
    in the SAME tick (old behaviour: one tick later)."""
    coord = _coord(OFF)  # stale cache from the previous tick
    cam_raw = {
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "featureSupport": {"light": True},
        "featureStatus": {"frontIlluminatorInGeneralLightOn": False},
    }
    _poll_cam_info_caches(coord, CAM, cam_raw)  # derives from the stale cache
    assert coord.shc_state_cache[CAM]["camera_light"] is False

    session = MagicMock()
    session.get.return_value = _resp(200, ON)  # app switched the lights on
    ctx = CamContext(
        hw="HOME_Eyes_Outdoor",
        is_gen2=True,
        is_online=True,
        stream_active=False,
        local_stream_active=False,
        privacy_on=False,
        do_slow_cam=False,
        pan_limit=0,
        has_light=True,
    )
    await _poll_cam_control(coord, CAM, ctx, session, {})
    c = coord.shc_state_cache[CAM]
    assert (c["camera_light"], c["front_light"], c["wallwasher"]) == (True, True, True)


@pytest.mark.asyncio
async def test_switches_update_even_when_feature_status_has_no_light_flag() -> None:
    """Gen2 without featureStatus.frontIlluminatorInGeneralLightOn must still sync."""
    coord = _coord(OFF)
    session = MagicMock()
    session.get.return_value = _resp(200, ON)
    ctx = CamContext(
        hw="HOME_Eyes_Outdoor",
        is_gen2=True,
        is_online=True,
        stream_active=False,
        local_stream_active=False,
        privacy_on=False,
        do_slow_cam=False,
        pan_limit=0,
        has_light=True,
    )
    await _poll_cam_control(coord, CAM, ctx, session, {})
    assert coord.shc_state_cache[CAM]["camera_light"] is True


@pytest.mark.asyncio
async def test_camera_light_on_write_schedules_confirm_refresh_gen2_only() -> None:
    coord = _coord(OFF)
    session = MagicMock()
    with (
        patch(
            "custom_components.bosch_smart_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "custom_components.bosch_smart_camera.shc.cloud_set_camera_light",
            new=AsyncMock(
                return_value=SimpleNamespace(ok=True, status=204, topdown_status=204)
            ),
        ),
    ):
        with patch(
            "custom_components.bosch_smart_camera.shc._is_gen2", return_value=True
        ):
            assert await async_cloud_set_camera_light(coord, CAM, True)
        assert coord.spawn_tracked.call_count == 1
        # OFF and Gen1 do not schedule it
        with patch(
            "custom_components.bosch_smart_camera.shc._is_gen2", return_value=True
        ):
            await async_cloud_set_camera_light(coord, CAM, False)
        with patch(
            "custom_components.bosch_smart_camera.shc._is_gen2", return_value=False
        ):
            await async_cloud_set_camera_light(coord, CAM, True)
        assert coord.spawn_tracked.call_count == 1


@pytest.mark.asyncio
async def test_confirm_refresh_stops_once_lights_reported_on() -> None:
    coord = _coord(OFF)
    session = MagicMock()
    session.get.side_effect = [_resp(200, OFF), _resp(200, ON)]
    with (
        patch(
            "custom_components.bosch_smart_camera.shc._LIGHT_ON_CONFIRM_DELAYS",
            (0, 0, 0),
        ),
        patch(
            "custom_components.bosch_smart_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        await _refresh_lighting_switch_after_on(coord, CAM, None)
    assert session.get.call_count == 2  # third attempt skipped
    assert coord.lighting_switch_cache[CAM] == ON
    assert coord.async_update_listeners.call_count == 2


@pytest.mark.asyncio
async def test_confirm_refresh_survives_library_value_error() -> None:
    """An endpoint/camera-id rejected by the library is logged, not raised."""
    coord = _coord(OFF)
    with (
        patch(
            "custom_components.bosch_smart_camera.shc._LIGHT_ON_CONFIRM_DELAYS", (0,)
        ),
        patch(
            "custom_components.bosch_smart_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "custom_components.bosch_smart_camera.shc.get_setting_raw",
            new=AsyncMock(side_effect=ValueError("invalid camera id")),
        ),
    ):
        await _refresh_lighting_switch_after_on(coord, CAM, None)
    assert coord.lighting_switch_cache[CAM] == OFF
    coord.async_update_listeners.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_refresh_survives_errors_and_bad_payloads() -> None:
    coord = _coord(OFF)
    session = MagicMock()
    session.get.side_effect = [
        _resp(500),
        _resp(200, ["not", "a", "dict"]),
        TimeoutError(),
    ]
    with (
        patch(
            "custom_components.bosch_smart_camera.shc._LIGHT_ON_CONFIRM_DELAYS",
            (0, 0, 0),
        ),
        patch(
            "custom_components.bosch_smart_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        await _refresh_lighting_switch_after_on(coord, CAM, None)
    assert coord.lighting_switch_cache[CAM] == OFF  # untouched
    coord.async_update_listeners.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_discarded_when_a_newer_write_happened_before_fetch() -> None:
    """ON then OFF: the stale confirm must not bring the lights back."""
    coord = _coord(OFF)
    coord.light_set_at[CAM] = 100.0  # the ON write's stamp is 50.0 below
    session = MagicMock()
    with (
        patch(
            "custom_components.bosch_smart_camera.shc._LIGHT_ON_CONFIRM_DELAYS", (0, 0)
        ),
        patch(
            "custom_components.bosch_smart_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        await _refresh_lighting_switch_after_on(coord, CAM, 50.0)
    session.get.assert_not_called()
    assert coord.lighting_switch_cache[CAM] == OFF


@pytest.mark.asyncio
async def test_confirm_discarded_when_a_light_put_ran_during_the_fetch() -> None:
    coord = _coord(OFF)
    coord.light_set_at[CAM] = 50.0
    session = MagicMock()

    def get(*_a: Any, **_k: Any) -> MagicMock:
        coord.light_set_at[CAM] = 99.0  # a light entity PUT finished meanwhile
        return _resp(200, ON)

    session.get.side_effect = get
    with (
        patch(
            "custom_components.bosch_smart_camera.shc._LIGHT_ON_CONFIRM_DELAYS", (0, 0)
        ),
        patch(
            "custom_components.bosch_smart_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
    ):
        await _refresh_lighting_switch_after_on(coord, CAM, 50.0)
    assert coord.lighting_switch_cache[CAM] == OFF  # stale GET not applied
    coord.async_update_listeners.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_is_cancellable_and_delays_are_absolute() -> None:
    coord = _coord(OFF)
    task = asyncio.create_task(_refresh_lighting_switch_after_on(coord, CAM, None))
    await asyncio.sleep(0)
    task.cancel()  # unload cancels tracked tasks
    with pytest.raises(asyncio.CancelledError):
        await task
    from custom_components.bosch_smart_camera import shc

    assert shc._LIGHT_ON_CONFIRM_DELAYS == (4.0, 12.0)


def _gen1_ctx() -> CamContext:
    return CamContext(
        hw="CAMERA_EYES",
        is_gen2=False,
        is_online=True,
        stream_active=False,
        local_stream_active=False,
        privacy_on=False,
        do_slow_cam=False,
        pan_limit=0,
        has_light=True,
    )


@pytest.mark.asyncio
async def test_gen1_switches_follow_lighting_override_not_feature_status() -> None:
    """GitHub #78: featureStatus reports the general light, so a manually
    switched-on Gen1 light read back as OFF after the write lock. The
    lighting_override GET is the source of truth."""
    coord = _coord()
    coord.hw_version = {CAM: "CAMERA_EYES"}
    _poll_cam_info_caches(
        coord,
        CAM,
        {
            "hardwareVersion": "CAMERA_EYES",
            "featureSupport": {"light": True},
            "featureStatus": {
                "frontIlluminatorInGeneralLightOn": False,
                "wallwasherInGeneralLightOn": False,
            },
        },
    )
    assert coord.shc_state_cache[CAM]["wallwasher"] is False

    session = MagicMock()
    session.get.return_value = _resp(
        200, {"frontLightOn": True, "wallwasherOn": True, "frontLightIntensity": 0.5}
    )
    await _poll_cam_control(coord, CAM, _gen1_ctx(), session, {})
    c = coord.shc_state_cache[CAM]
    assert (c["camera_light"], c["front_light"], c["wallwasher"]) == (True, True, True)
    assert c["front_light_intensity"] == 0.5
    assert session.get.call_args.args[0].endswith("/lighting_override")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"frontLightOn": "yes", "wallwasherOn": True},
        {"frontLightOn": True},
    ],
)
async def test_gen1_malformed_lighting_override_keeps_cache(payload: Any) -> None:
    coord = _coord()
    coord.shc_state_cache[CAM] = {"front_light": False, "wallwasher": True}
    session = MagicMock()
    session.get.return_value = _resp(200, payload)
    await _poll_cam_control(coord, CAM, _gen1_ctx(), session, {})
    assert coord.shc_state_cache[CAM] == {"front_light": False, "wallwasher": True}


@pytest.mark.asyncio
async def test_gen1_lighting_override_skipped_inside_write_lock() -> None:
    coord = _coord()
    coord.light_set_at[CAM] = time.monotonic()
    session = MagicMock()
    await _poll_cam_control(coord, CAM, _gen1_ctx(), session, {})
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_gen1_feature_status_does_not_replace_confirmed_light_state() -> None:
    """GitHub #78: a failed lighting_override read must not let the general-light
    featureStatus flip a confirmed ON back to OFF."""
    coord = _coord()
    coord.shc_state_cache[CAM] = {
        "front_light": True,
        "wallwasher": True,
        "camera_light": True,
    }
    _poll_cam_info_caches(
        coord,
        CAM,
        {
            "hardwareVersion": "CAMERA_EYES",
            "featureSupport": {"light": True},
            "featureStatus": {
                "frontIlluminatorInGeneralLightOn": False,
                "wallwasherInGeneralLightOn": False,
            },
        },
    )
    c = coord.shc_state_cache[CAM]
    assert (c["camera_light"], c["front_light"], c["wallwasher"]) == (True, True, True)


@pytest.mark.asyncio
async def test_gen1_lighting_override_ignored_when_write_lands_during_request() -> None:
    coord = _coord()
    coord.shc_state_cache[CAM] = {"front_light": False, "wallwasher": False}
    resp = _resp(200, {"frontLightOn": False, "wallwasherOn": False})
    session = MagicMock()

    def _get(*_a: Any, **_k: Any) -> Any:
        coord.light_set_at[CAM] = time.monotonic()  # user write during the GET
        coord.shc_state_cache[CAM]["front_light"] = True
        return resp

    session.get.side_effect = _get
    await _poll_cam_control(coord, CAM, _gen1_ctx(), session, {})
    assert coord.shc_state_cache[CAM]["front_light"] is True


@pytest.mark.asyncio
async def test_gen1_lighting_override_fetch_error_keeps_cache() -> None:
    """A failing or non-200 lighting_override read leaves the cache untouched."""
    coord = _coord()
    coord.shc_state_cache[CAM] = {"front_light": True, "wallwasher": True}
    session = MagicMock()
    session.get.side_effect = TimeoutError("boom")
    await _poll_cam_control(coord, CAM, _gen1_ctx(), session, {})
    session.get.side_effect = None
    session.get.return_value = _resp(500, None)
    await _poll_cam_control(coord, CAM, _gen1_ctx(), session, {})
    assert coord.shc_state_cache[CAM] == {"front_light": True, "wallwasher": True}
