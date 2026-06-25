"""Tests for the Gen2 Audio-Plus sound-detection switches (2026-06-22).

Covers BoschGlassBreakDetectionSwitch + BoschFireAlarmDetectionSwitch which
toggle /v11/video_inputs/{id}/audioDetectionConfig {detectGlassBreak,
detectFireAlarm}. Pins: state mapping per field, availability, and the critical
"PUT sends BOTH fields, preserving the other toggle" behaviour, plus the
privacy-mode write guard.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _coord(cfg: dict | None, privacy_on: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        data={CAM_ID: {"info": {"title": "Terrasse"}}},
        _audio_detection_cache={CAM_ID: cfg} if cfg is not None else {},
        _audio_detection_set_at={},
        _shc_state_cache={CAM_ID: {"privacy_mode": privacy_on}},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )


def _entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={"bearer_token": "x"}, options={})


def _make(cls_name: str, coord: SimpleNamespace):
    import custom_components.bosch_shc_camera.switch as sw_mod

    sw = getattr(sw_mod, cls_name)(coord, CAM_ID, _entry())
    sw.async_write_ha_state = MagicMock()  # not added to hass in unit test
    sw.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    return sw


GLASS = "BoschGlassBreakDetectionSwitch"
FIRE = "BoschFireAlarmDetectionSwitch"


# ── state mapping (PIN_EVERY_MODE: on / off / unknown) ────────────────────
@pytest.mark.parametrize(
    ("cls", "cfg", "expected"),
    [
        (GLASS, {"detectGlassBreak": True, "detectFireAlarm": False}, True),
        (GLASS, {"detectGlassBreak": False, "detectFireAlarm": True}, False),
        (GLASS, {"detectFireAlarm": True}, None),  # field missing → unknown
        (FIRE, {"detectGlassBreak": False, "detectFireAlarm": True}, True),
        (FIRE, {"detectGlassBreak": True, "detectFireAlarm": False}, False),
        (FIRE, {"detectGlassBreak": True}, None),
    ],
)
def test_is_on_maps_its_own_field(cls, cfg, expected):
    sw = _make(cls, _coord(cfg))
    assert sw.is_on is expected


def test_unavailable_without_cache():
    sw = _make(GLASS, _coord(None))
    assert sw.available is False


def test_available_with_cache():
    sw = _make(GLASS, _coord({"detectGlassBreak": False, "detectFireAlarm": False}))
    assert sw.available is True


# ── write preserves the OTHER field (both always sent) ────────────────────
async def test_glass_on_preserves_fire():
    coord = _coord({"detectGlassBreak": False, "detectFireAlarm": True})
    sw = _make(GLASS, coord)
    await sw.async_turn_on()
    coord.async_put_camera.assert_awaited_once_with(
        CAM_ID,
        "audioDetectionConfig",
        {"detectGlassBreak": True, "detectFireAlarm": True},
    )


async def test_fire_off_preserves_glass():
    coord = _coord({"detectGlassBreak": True, "detectFireAlarm": True})
    sw = _make(FIRE, coord)
    await sw.async_turn_off()
    coord.async_put_camera.assert_awaited_once_with(
        CAM_ID,
        "audioDetectionConfig",
        {"detectGlassBreak": True, "detectFireAlarm": False},
    )


async def test_successful_write_updates_cache_and_lock():
    coord = _coord({"detectGlassBreak": False, "detectFireAlarm": False})
    sw = _make(GLASS, coord)
    await sw.async_turn_on()
    assert coord._audio_detection_cache[CAM_ID]["detectGlassBreak"] is True
    assert CAM_ID in coord._audio_detection_set_at  # write-lock stamped


# ── privacy guard: write blocked, no PUT, no cache change ─────────────────
async def test_privacy_on_blocks_write():
    coord = _coord(
        {"detectGlassBreak": False, "detectFireAlarm": False}, privacy_on=True
    )
    sw = _make(GLASS, coord)
    await sw.async_turn_on()
    coord.async_put_camera.assert_not_awaited()
    assert coord._audio_detection_cache[CAM_ID]["detectGlassBreak"] is False


# ── empty cache: write is a no-op (can't preserve unknown fields) ─────────
async def test_no_write_when_cache_empty():
    coord = _coord(None)
    sw = _make(FIRE, coord)
    await sw.async_turn_on()
    coord.async_put_camera.assert_not_awaited()
