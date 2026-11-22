"""Null LED groups in the cloud `lighting/switch` body (sibling of GitHub #87).

The cloud can send `"topLedLightSettings": null` for a group a camera lacks,
the same way #87 showed `"featureSupport": null`. Every cache reader chains
`.get(group, {}).get("brightness")` or spreads the group into the next PUT
body, so a null group raised AttributeError/TypeError. The cache writers now
normalise null groups to the full-shape default.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_smart_camera.shc import normalize_lighting_switch
from custom_components.bosch_smart_camera.slow_tier import (
    _poll_cam_control,
    sync_gen2_light_cache,
)
from tests.test_slow_tier import (
    CAM_A,
    HEADERS,
    _ctx,
    _make_control_coord,
    _make_resp,
    _make_session,
)

_DEFAULT = {"brightness": 0, "color": None, "whiteBalance": -1.0}


def test_normalize_replaces_null_groups() -> None:
    body: dict[str, Any] = {
        "frontLightSettings": {"brightness": 40, "color": None, "whiteBalance": 0.0},
        "topLedLightSettings": None,
        "bottomLedLightSettings": None,
    }
    out = normalize_lighting_switch(body)
    assert out["frontLightSettings"]["brightness"] == 40
    assert out["topLedLightSettings"] == _DEFAULT
    assert out["bottomLedLightSettings"] == _DEFAULT


def test_normalize_leaves_missing_groups_and_other_keys() -> None:
    body: dict[str, Any] = {"frontLightSettings": None, "other": None}
    assert normalize_lighting_switch(body) == {
        "frontLightSettings": _DEFAULT,
        "other": None,
    }


def test_normalize_returns_distinct_defaults() -> None:
    out = normalize_lighting_switch(
        {"topLedLightSettings": None, "bottomLedLightSettings": None}
    )
    assert out["topLedLightSettings"] is not out["bottomLedLightSettings"]


@pytest.mark.asyncio
async def test_poll_cam_control_null_groups_do_not_crash_light_sync() -> None:
    coord = _make_control_coord(lighting_switch_cache={})
    resp = _make_resp(200)
    resp.json = AsyncMock(
        return_value={
            "frontLightSettings": {"brightness": 60, "color": None},
            "topLedLightSettings": None,
            "bottomLedLightSettings": None,
        }
    )
    session = _make_session({"/lighting/switch": resp})
    await _poll_cam_control(
        coord, CAM_A, _ctx(is_gen2=True, is_online=True), session, HEADERS
    )
    lsc = coord.lighting_switch_cache[CAM_A]
    assert lsc["topLedLightSettings"] == _DEFAULT
    assert lsc["bottomLedLightSettings"] == _DEFAULT
    cache = coord.shc_state_cache[CAM_A]
    assert cache["front_light"] is True
    assert cache["wallwasher"] is False


def test_sync_gen2_light_cache_after_normalize() -> None:
    coord = SimpleNamespace(
        lighting_switch_cache={
            CAM_A: normalize_lighting_switch(
                {
                    "frontLightSettings": None,
                    "topLedLightSettings": {"brightness": 30},
                    "bottomLedLightSettings": None,
                }
            )
        },
        light_set_at={CAM_A: float("-inf")},
        WRITE_LOCK_SECS=10.0,
        shc_state_cache={},
    )
    assert time.monotonic() > coord.light_set_at[CAM_A]
    sync_gen2_light_cache(coord, CAM_A)  # type: ignore[arg-type]
    assert coord.shc_state_cache[CAM_A]["wallwasher"] is True
    assert coord.shc_state_cache[CAM_A]["front_light"] is False
