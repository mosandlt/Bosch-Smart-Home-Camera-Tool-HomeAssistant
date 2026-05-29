"""Regression test — BoschIntrusionDistanceNumber clamps distance to max 8.

Bug: number.py L937 used min(10, value); Bosch cloud API rejects distance > 8
with HTTP 400 ("must be less than or equal to 8") on FW 9.40.102.
Result: setting 9 or 10 produced a doomed PUT and left the entity stuck.

Fix: clamp changed to min(8, value) and _attr_native_max_value = 8.

Regression: values 9 and 10 must be clamped to 8; values 1–8 pass through.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "22222222-2222-2222-2222-222222222222"  # Gen2 Indoor II


def _make_coord(distance: int = 5) -> SimpleNamespace:
    cfg = {
        "enabled": True,
        "sensitivity": 3,
        "detectionMode": "ZONES",
        "distance": distance,
    }
    return SimpleNamespace(
        data={CAM_ID: {
            "info": {
                "title": "Innenbereich",
                "hardwareVersion": "HOME_Eyes_Indoor",
                "firmwareVersion": "9.40.102",
                "macAddress": "aa:bb:cc:dd:ee:ff",
            },
        }},
        _intrusion_config_cache={CAM_ID: dict(cfg)},
        _intrusion_config_set_at={},
        _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ---------------------------------------------------------------------------
# attr declaration tests
# ---------------------------------------------------------------------------

def test_native_max_value_is_8(stub_entry) -> None:
    """_attr_native_max_value must be 8, not 10."""
    from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber

    coord = _make_coord()
    entity = BoschIntrusionDistanceNumber(coord, CAM_ID, stub_entry)
    assert entity._attr_native_max_value == 8, (
        "API rejects distance > 8; max must be 8 not 10"
    )


def test_native_min_value_is_1(stub_entry) -> None:
    """_attr_native_min_value must remain 1."""
    from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber

    coord = _make_coord()
    entity = BoschIntrusionDistanceNumber(coord, CAM_ID, stub_entry)
    assert entity._attr_native_min_value == 1


# ---------------------------------------------------------------------------
# clamp tests — the actual regression
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("input_val,expected", [
    (9,  8),   # was accepted before fix, now clamped — prevents HTTP 400
    (10, 8),   # was accepted before fix, now clamped — prevents HTTP 400
    (8,  8),   # boundary: must pass through unchanged
    (7,  7),   # normal value: must pass through unchanged
    (1,  1),   # minimum: must pass through unchanged
    (5,  5),   # mid-range: must pass through unchanged
])
async def test_set_native_value_clamp(input_val: int, expected: int, stub_entry) -> None:
    """Values > 8 must be clamped to 8; values 1–8 pass through unchanged."""
    from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber

    coord = _make_coord()
    entity = BoschIntrusionDistanceNumber(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(float(input_val))

    actual = coord._intrusion_config_cache[CAM_ID]["distance"]
    assert actual == expected, (
        f"input={input_val}: expected clamped distance={expected}, got {actual}"
    )
    coord.async_put_camera.assert_called_once()
    # PUT body must also carry the clamped value
    _, put_cfg = coord.async_put_camera.call_args[0][2], coord.async_put_camera.call_args[0]
    assert coord._intrusion_config_cache[CAM_ID]["distance"] == expected


@pytest.mark.asyncio
async def test_set_native_value_9_does_not_send_9_to_api(stub_entry) -> None:
    """Explicit regression: value=9 must never appear in the PUT payload."""
    from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber

    coord = _make_coord()
    entity = BoschIntrusionDistanceNumber(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_set_native_value(9.0)

    # The third positional arg to async_put_camera is the config dict
    call_args = coord.async_put_camera.call_args[0]
    sent_cfg = call_args[2]
    assert sent_cfg["distance"] == 8, (
        f"PUT payload must contain distance=8, got {sent_cfg['distance']}"
    )


@pytest.mark.asyncio
async def test_write_lock_set_after_successful_put(stub_entry) -> None:
    """_intrusion_config_set_at must be stamped after a successful distance set."""
    import time
    from custom_components.bosch_shc_camera.number import BoschIntrusionDistanceNumber

    coord = _make_coord()
    entity = BoschIntrusionDistanceNumber(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    before = time.monotonic()
    await entity.async_set_native_value(6.0)
    after = time.monotonic()

    assert CAM_ID in coord._intrusion_config_set_at
    ts = coord._intrusion_config_set_at[CAM_ID]
    assert before <= ts <= after
