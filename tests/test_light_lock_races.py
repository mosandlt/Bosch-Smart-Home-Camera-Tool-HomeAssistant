"""Lighting write races: multi-step writes must hold the per-camera lock.

Each light write is a /lighting/switch brightness PUT plus an enable PUT
(`/front`, `/topdown`) plus cache updates. If the lock ends after the first
PUT, a parallel write slips between the steps and the camera, the cache and
the HA state end up contradicting each other.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bosch_shc_camera_client.cloud import CloudPutResult

from custom_components.bosch_shc_camera.light import (
    BoschFrontLight,
    BoschTopLedLight,
)
from custom_components.bosch_shc_camera.shc import (
    async_cloud_set_light_component,
    get_lighting_lock,
)

CAM_ID = "11111111-1111-1111-1111-111111111111"
SWITCH = "lighting/switch"


def _group(brightness: int, *, color: str | None = None, wb: float | None = -1.0):
    return {"brightness": brightness, "color": color, "whiteBalance": wb}


def _coord(**cache: Any) -> SimpleNamespace:
    return SimpleNamespace(
        hass=SimpleNamespace(),
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True},
                },
                "status": "ONLINE",
            }
        },
        token="tok-A",
        hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
        lighting_switch_cache={
            CAM_ID: {
                "frontLightSettings": _group(0),
                "topLedLightSettings": _group(0),
                "bottomLedLightSettings": _group(0),
                **cache,
            }
        },
        shc_state_cache={CAM_ID: {}},
        light_set_at={},
        async_update_listeners=MagicMock(),
    )


class _FakeCamera:
    """Records PUTs in arrival order; yields so concurrent writers interleave."""

    def __init__(self, fail_switch: bool = False) -> None:
        self.ops: list[tuple[str, Any]] = []
        self.fail_switch = fail_switch

    async def put(self, url: str, body: dict[str, Any]) -> bool:
        await asyncio.sleep(0)
        endpoint = url.split("/v11/video_inputs/")[1].split("/", 1)[1]
        if endpoint == SWITCH:
            self.ops.append((SWITCH, body))
            await asyncio.sleep(0)
            return not self.fail_switch
        self.ops.append((endpoint, body["enabled"]))
        await asyncio.sleep(0)
        return True

    # light.py path (aiohttp session.put context manager)
    def session(self) -> MagicMock:
        cam = self

        class _Ctx:
            def __init__(self, url: str, json: dict[str, Any]) -> None:
                self.url, self.json = url, json
                self.status = 204

            async def __aenter__(self) -> _Ctx:
                self.status = 204 if await cam.put(self.url, self.json) else 500
                return self

            async def __aexit__(self, *a: Any) -> None:
                return None

            async def json_(self) -> None:  # pragma: no cover
                return None

        sess = MagicMock()
        sess.put = lambda url, headers=None, json=None: _Ctx(url, json)
        return sess

    # shc.py path
    async def cloud_put_json(
        self, session: Any, token: str, url: str, body: dict[str, Any], **kw: Any
    ) -> CloudPutResult:
        ok = await self.put(url, body)
        return CloudPutResult(ok=ok, status=204 if ok else 500)


def _light(klass: type, coord: SimpleNamespace) -> Any:
    entity = klass(coord, CAM_ID, SimpleNamespace(entry_id="01ENTRY"))
    entity.hass = coord.hass
    entity.async_write_ha_state = MagicMock()
    return entity


def _patch_light(cam: _FakeCamera):
    return patch(
        "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
        new=AsyncMock(return_value=cam.session()),
    )


def _patch_shc(cam: _FakeCamera):
    return (
        patch(
            "custom_components.bosch_shc_camera.shc.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "custom_components.bosch_shc_camera.shc.cloud_put_json",
            new=cam.cloud_put_json,
        ),
    )


class TestRgbOffVersusOn:
    @pytest.mark.asyncio
    async def test_off_and_on_do_not_interleave_steps(self) -> None:
        """OFF (brightness 0 + topdown disable) vs ON must be two atomic units."""
        coord = _coord(topLedLightSettings=_group(80))
        cam = _FakeCamera()
        light = _light(BoschTopLedLight, coord)
        with _patch_light(cam):
            await asyncio.gather(light.async_turn_off(), light.async_turn_on())
        assert [op[0] for op in cam.ops] == [SWITCH, "lighting/switch/topdown"] * 2
        assert cam.ops[1][1] is False
        assert cam.ops[3][1] is True

    @pytest.mark.asyncio
    async def test_nudge_and_exact_write_stay_adjacent(self) -> None:
        """Colour->white nudge and exact write hold one lock; a parallel OFF waits."""
        coord = _coord(topLedLightSettings=_group(50, color="#FF0000", wb=None))
        cam = _FakeCamera()
        light = _light(BoschTopLedLight, coord)
        other = _light(BoschTopLedLight, coord)
        with _patch_light(cam):
            await asyncio.gather(
                light.async_turn_on(color_temp_kelvin=6500), other.async_turn_off()
            )
        kinds = [op[0] for op in cam.ops]
        assert kinds == [
            SWITCH,
            SWITCH,
            "lighting/switch/topdown",
            SWITCH,
            "lighting/switch/topdown",
        ]
        nudge = cam.ops[0][1]["topLedLightSettings"]["whiteBalance"]
        exact = cam.ops[1][1]["topLedLightSettings"]["whiteBalance"]
        assert nudge != exact
        assert cam.ops[3][1]["topLedLightSettings"]["brightness"] == 0


class TestFrontLightOffVersusOn:
    @pytest.mark.asyncio
    async def test_front_off_and_on_do_not_interleave(self) -> None:
        coord = _coord(frontLightSettings=_group(70))
        cam = _FakeCamera()
        light = _light(BoschFrontLight, coord)
        with _patch_light(cam):
            await asyncio.gather(light.async_turn_off(), light.async_turn_on())
        assert [op[0] for op in cam.ops] == [SWITCH, "lighting/switch/front"] * 2
        assert [cam.ops[1][1], cam.ops[3][1]] == [False, True]


class TestWallwasherStepOneFailure:
    @pytest.mark.asyncio
    async def test_failed_brightness_write_skips_topdown_and_reports_failure(
        self,
    ) -> None:
        coord = _coord()
        cam = _FakeCamera(fail_switch=True)
        p1, p2 = _patch_shc(cam)
        with p1, p2:
            ok = await async_cloud_set_light_component(
                coord, CAM_ID, "wallwasher", True
            )
        assert ok is False
        assert [op[0] for op in cam.ops] == [SWITCH]
        assert "wallwasher" not in coord.shc_state_cache[CAM_ID]


class TestIntensityRace:
    @pytest.mark.asyncio
    async def test_parallel_intensity_writes_end_consistent(self) -> None:
        """60% vs 0%: each brightness PUT is followed by its own enable PUT."""
        coord = _coord()
        cam = _FakeCamera()
        p1, p2 = _patch_shc(cam)
        with p1, p2:
            r = await asyncio.gather(
                async_cloud_set_light_component(coord, CAM_ID, "intensity", 60),
                async_cloud_set_light_component(coord, CAM_ID, "intensity", 0),
            )
        assert r == [True, True]
        assert [op[0] for op in cam.ops] == [SWITCH, "lighting/switch/front"] * 2
        assert [cam.ops[1][1], cam.ops[3][1]] == [True, False]
        assert (
            coord.lighting_switch_cache[CAM_ID]["frontLightSettings"]["brightness"] == 0
        )
        assert coord.shc_state_cache[CAM_ID]["front_light"] is False


class TestFrontSwitchHoldsLock:
    @pytest.mark.asyncio
    async def test_front_off_enable_put_waits_for_lighting_lock(self) -> None:
        """enabled=False must not reach the camera while another writer holds the lock."""
        coord = _coord(frontLightSettings=_group(70))
        cam = _FakeCamera()
        p1, p2 = _patch_shc(cam)
        lock = get_lighting_lock(coord, CAM_ID)
        with p1, p2:
            async with lock:
                task = asyncio.ensure_future(
                    async_cloud_set_light_component(coord, CAM_ID, "front", False)
                )
                for _ in range(5):
                    await asyncio.sleep(0)
                assert cam.ops == []
            assert await task is True
        assert cam.ops == [("lighting/switch/front", False)]
        assert (
            coord.lighting_switch_cache[CAM_ID]["frontLightSettings"]["brightness"] == 0
        )
        assert coord.shc_state_cache[CAM_ID]["front_light"] is False
