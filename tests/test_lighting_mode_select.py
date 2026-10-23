"""Gen1 Eyes Outdoor ambient-light mode select (GitHub issue #78).

Switching must change ONLY ``scheduleStatus`` (GET-merge-PUT), keep every other
lighting_options field (threshold, schedule, motion light) and serialize
overlapping writes per camera.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.bosch_shc_camera.lighting_options import (
    SCHEDULE_STATUS_OPTIONS,
    async_set_schedule_status,
    lighting_write_lock,
)
from custom_components.bosch_shc_camera.select import BoschLightingModeSelect

CAM = "11111111-1111-1111-1111-111111111111"
CURRENT = {
    "scheduleStatus": "AMBIENT_LIGHT",
    "generalLightOnTime": "20:15:00",
    "generalLightOffTime": "06:15:00",
    "darknessThreshold": 0.0515872,
    "lightOnMotion": True,
}


def _resp(status: int, data: dict[str, Any] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=dict(data or {}))
    resp.text = AsyncMock(return_value="boom")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _coord(cache: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        hass=MagicMock(),
        token="t",
        lighting_options_cache={CAM: dict(cache)} if cache is not None else {},
        lighting_options_set_at={},
        shc_state_cache={},
        lighting_mode_light_snapshot={},
        async_request_refresh=AsyncMock(),
        last_update_success=True,
        is_write_locked=lambda cam, d: cam in d,
        data={CAM: {"info": {"title": "Terrasse", "hardwareVersion": "CAMERA_EYES"}}},
    )


def _patch_session(session: MagicMock):
    return patch(
        "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
        AsyncMock(return_value=session),
    )


@pytest.mark.asyncio
async def test_always_off_changes_only_schedule_status() -> None:
    coord = _coord(CURRENT)
    session = MagicMock()
    session.get.return_value = _resp(200, CURRENT)
    session.put.return_value = _resp(200)
    with _patch_session(session):
        await async_set_schedule_status(coord, CAM, "always_off")
    sent = session.put.call_args.kwargs["json"]
    assert sent == {**CURRENT, "scheduleStatus": "ALWAYS_OFF"}
    assert coord.lighting_options_cache[CAM]["scheduleStatus"] == "ALWAYS_OFF"
    assert CAM in coord.lighting_options_set_at
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("option", SCHEDULE_STATUS_OPTIONS)
async def test_every_mode_maps_to_api_value(option: str) -> None:
    coord = _coord(CURRENT)
    session = MagicMock()
    session.get.return_value = _resp(
        200, {**CURRENT, "scheduleStatus": "SOMETHING_ELSE"}
    )
    session.put.return_value = _resp(204)
    with _patch_session(session):
        await async_set_schedule_status(coord, CAM, option)
    assert session.put.call_args.kwargs["json"]["scheduleStatus"] == option.upper()


@pytest.mark.asyncio
async def test_unknown_option_rejected_without_http() -> None:
    coord = _coord(CURRENT)
    session = MagicMock()
    with _patch_session(session), pytest.raises(HomeAssistantError):
        await async_set_schedule_status(coord, CAM, "garbage")
    session.get.assert_not_called()


@pytest.mark.asyncio
async def test_get_failure_raises_and_does_not_put() -> None:
    coord = _coord(CURRENT)
    session = MagicMock()
    session.get.return_value = _resp(442)
    with _patch_session(session), pytest.raises(HomeAssistantError):
        await async_set_schedule_status(coord, CAM, "always_off")
    session.put.assert_not_called()
    assert coord.lighting_options_cache[CAM]["scheduleStatus"] == "AMBIENT_LIGHT"


@pytest.mark.asyncio
async def test_put_failure_raises_and_keeps_cache() -> None:
    coord = _coord(CURRENT)
    session = MagicMock()
    session.get.return_value = _resp(200, CURRENT)
    session.put.return_value = _resp(500)
    with _patch_session(session), pytest.raises(HomeAssistantError):
        await async_set_schedule_status(coord, CAM, "always_off")
    assert coord.lighting_options_cache[CAM]["scheduleStatus"] == "AMBIENT_LIGHT"
    assert CAM not in coord.lighting_options_set_at


@pytest.mark.asyncio
async def test_overlapping_writes_are_serialized() -> None:
    """A second write must not GET before the first write's PUT finished."""
    coord = _coord(CURRENT)
    gate = asyncio.Event()
    order: list[str] = []
    n = {"get": 0}

    def get(*_a: Any, **_k: Any) -> MagicMock:
        n["get"] += 1
        idx = n["get"]
        order.append(f"get{idx}")
        cm = _resp(200, CURRENT)
        if idx == 1:
            inner = cm.__aenter__

            async def slow(*_a: Any) -> Any:
                await gate.wait()  # first write stalls here, holding the lock
                return await inner()

            cm.__aenter__ = slow
        return cm

    def put(*_a: Any, **_k: Any) -> MagicMock:
        order.append("put")
        return _resp(200)

    session = MagicMock()
    session.get.side_effect = get
    session.put.side_effect = put
    with _patch_session(session):
        t1 = asyncio.create_task(async_set_schedule_status(coord, CAM, "always_off"))
        await asyncio.sleep(0.01)
        t2 = asyncio.create_task(async_set_schedule_status(coord, CAM, "ambient_light"))
        await asyncio.sleep(0.05)
        assert order == ["get1"], "second write must wait for the first"
        gate.set()
        await asyncio.gather(t1, t2)
    # 2nd write builds on the 1st write's payload (cache), no stale GET
    assert order == ["get1", "put", "put"]


def test_lock_is_per_camera_and_stable() -> None:
    coord = _coord()
    assert lighting_write_lock(coord, "a") is lighting_write_lock(coord, "a")
    assert lighting_write_lock(coord, "a") is not lighting_write_lock(coord, "b")


def _entity(cache: dict[str, Any] | None, ok: bool = True) -> BoschLightingModeSelect:
    coord = _coord(cache)
    coord.last_update_success = ok
    return BoschLightingModeSelect(coord, CAM, MagicMock())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("AMBIENT_LIGHT", "ambient_light"),
        ("FOLLOW_SCHEDULE", "follow_schedule"),
        ("AMBIENT_LIGHT_AND_SCHEDULE", "ambient_light_and_schedule"),
        ("ALWAYS_OFF", "always_off"),
        ("SOMETHING_NEW", None),
        (None, None),
    ],
)
def test_current_option_mapping(raw: str | None, expected: str | None) -> None:
    ent = _entity({"scheduleStatus": raw} if raw is not None else {})
    assert ent.current_option == expected
    assert ent.available is (expected is not None)


def test_unavailable_when_update_failed_or_no_data() -> None:
    assert _entity(CURRENT, ok=False).available is False
    assert _entity(None).available is False


def test_device_info_and_ids() -> None:
    ent = _entity(CURRENT)
    assert ent.unique_id == f"bosch_shc_camera_{CAM}_lighting_mode_select"
    assert ent.device_info["name"] == "Bosch Terrasse"
    assert ent.device_info["model"] == "CAMERA_EYES"


@pytest.mark.asyncio
async def test_select_option_delegates_to_helper() -> None:
    ent = _entity(CURRENT)
    ent.async_write_ha_state = MagicMock()
    with patch(
        "custom_components.bosch_shc_camera.select.async_set_schedule_status",
        AsyncMock(),
    ) as setter:
        await ent.async_select_option("always_off")
    setter.assert_awaited_once_with(ent.coordinator, CAM, "always_off")
    ent.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_same_mode_under_lock_skips_put() -> None:
    """Equality is checked against the fresh GET inside the lock (no stale skip)."""
    coord = _coord(CURRENT)
    session = MagicMock()
    session.get.return_value = _resp(200, CURRENT)  # already AMBIENT_LIGHT
    with _patch_session(session):
        await async_set_schedule_status(coord, CAM, "ambient_light")
    session.put.assert_not_called()


@pytest.mark.asyncio
async def test_queued_request_is_not_dropped_by_stale_cache() -> None:
    """Cache says ambient_light, but a write in flight changed it to always_off:
    the queued ambient_light request must still reach the camera."""
    coord = _coord(CURRENT)
    session = MagicMock()
    session.get.return_value = _resp(200, {**CURRENT, "scheduleStatus": "ALWAYS_OFF"})
    session.put.return_value = _resp(200)
    with _patch_session(session):
        await async_set_schedule_status(coord, CAM, "ambient_light")
    assert session.put.call_args.kwargs["json"]["scheduleStatus"] == "AMBIENT_LIGHT"


@pytest.mark.asyncio
async def test_uses_own_last_write_when_cloud_get_would_be_stale() -> None:
    """Inside the write-lock window the cached PUT payload is the merge base."""
    coord = _coord({**CURRENT, "darknessThreshold": 0.9})
    coord.lighting_options_set_at[CAM] = 1.0  # fresh write in flight
    session = MagicMock()
    session.get.return_value = _resp(200, CURRENT)  # stale threshold 0.05
    session.put.return_value = _resp(200)
    with _patch_session(session):
        await async_set_schedule_status(coord, CAM, "always_off")
    session.get.assert_not_called()
    assert session.put.call_args.kwargs["json"]["darknessThreshold"] == 0.9


@pytest.mark.asyncio
async def test_cancelled_write_releases_lock() -> None:
    coord = _coord(CURRENT)
    gate = asyncio.Event()
    cm = _resp(200, CURRENT)
    inner = cm.__aenter__

    async def slow(*_a: Any) -> Any:
        await gate.wait()
        return await inner()

    cm.__aenter__ = slow
    session = MagicMock()
    session.get.return_value = cm
    with _patch_session(session):
        task = asyncio.create_task(async_set_schedule_status(coord, CAM, "always_off"))
        await asyncio.sleep(0.01)
        assert lighting_write_lock(coord, CAM).locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not lighting_write_lock(coord, CAM).locked()


@pytest.mark.asyncio
async def test_service_and_select_share_the_lock() -> None:
    """set_lighting_schedule holds the same lock the select waits on."""
    coord = _coord(CURRENT)
    lock = lighting_write_lock(coord, CAM)
    session = MagicMock()
    session.get.return_value = _resp(200, CURRENT)
    session.put.return_value = _resp(200)
    await lock.acquire()  # simulate the service mid-write
    with _patch_session(session):
        task = asyncio.create_task(async_set_schedule_status(coord, CAM, "always_off"))
        await asyncio.sleep(0.02)
        session.get.assert_not_called()
        lock.release()
        await task
    session.put.assert_called_once()


@pytest.mark.asyncio
async def test_setup_creates_select_only_for_gen1_with_light() -> None:
    from custom_components.bosch_shc_camera import select as sel

    async def build(hw: str, light: bool) -> list[Any]:
        coord = _coord(CURRENT)
        coord.data = {
            CAM: {
                "info": {
                    "title": "x",
                    "hardwareVersion": hw,
                    "featureSupport": {"light": light},
                }
            }
        }
        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=lambda _x: None
        )
        added: list[Any] = []
        with patch.object(sel, "register_dynamic_camera_listener", MagicMock()):
            await sel.async_setup_entry(MagicMock(), entry, added.extend)
        return added

    def has(entities: list[Any]) -> bool:
        return any(isinstance(e, BoschLightingModeSelect) for e in entities)

    assert has(await build("CAMERA_EYES", True))
    assert not has(await build("CAMERA_EYES", False))
    assert not has(await build("HOME_Eyes_Outdoor", True))
    assert not has(await build("SOME_UNKNOWN_MODEL", True))  # default model != Gen1


def _mode_coord(lights: dict[str, bool], status: str) -> SimpleNamespace:
    coord = _coord({**CURRENT, "scheduleStatus": status})
    coord.shc_state_cache = {CAM: lights}
    coord.lighting_mode_light_snapshot = {}
    coord.async_cloud_set_light_component = AsyncMock(return_value=True)
    return coord


async def _switch(coord: SimpleNamespace, option: str) -> None:
    session = MagicMock()
    session.get.return_value = _resp(200, coord.lighting_options_cache[CAM])
    session.put.return_value = _resp(200)
    with _patch_session(session):
        await async_set_schedule_status(coord, CAM, option)


@pytest.mark.asyncio
async def test_leaving_always_off_restores_lights_enabled_before() -> None:
    """GitHub #78: off → ambient must bring back the upper/lower light."""
    coord = _mode_coord({"wallwasher": True, "front_light": False}, "AMBIENT_LIGHT")
    await _switch(coord, "always_off")
    coord.shc_state_cache[CAM] = {"wallwasher": False, "front_light": False}
    await _switch(coord, "ambient_light")
    coord.async_cloud_set_light_component.assert_awaited_once_with(
        CAM, "wallwasher", True
    )
    assert CAM not in coord.lighting_mode_light_snapshot


@pytest.mark.asyncio
async def test_no_restore_when_lights_were_off_or_mode_unrelated() -> None:
    coord = _mode_coord({"wallwasher": False, "front_light": False}, "AMBIENT_LIGHT")
    await _switch(coord, "follow_schedule")
    await _switch(coord, "always_off")
    await _switch(coord, "ambient_light")
    coord.async_cloud_set_light_component.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_restore_keeps_snapshot_and_raises() -> None:
    coord = _mode_coord({"wallwasher": True, "front_light": True}, "AMBIENT_LIGHT")
    await _switch(coord, "always_off")
    coord.async_cloud_set_light_component = AsyncMock(side_effect=[True, False])
    with pytest.raises(HomeAssistantError):
        await _switch(coord, "ambient_light")
    assert coord.lighting_mode_light_snapshot[CAM] == {"front": True}


@pytest.mark.asyncio
async def test_noop_switch_refreshes_stale_cache() -> None:
    coord = _mode_coord({}, "AMBIENT_LIGHT")
    coord.lighting_options_cache[CAM]["scheduleStatus"] = "FOLLOW_SCHEDULE"
    session = MagicMock()
    session.get.return_value = _resp(200, {**CURRENT, "scheduleStatus": "ALWAYS_OFF"})
    with _patch_session(session):
        await async_set_schedule_status(coord, CAM, "always_off")
    assert coord.lighting_options_cache[CAM]["scheduleStatus"] == "ALWAYS_OFF"
    session.put.assert_not_called()
