"""Tests for BoschPanicAlarmSwitch (Gen2 siren trigger).

Discovered via mitmproxy capture 2026-05-13:
  PUT /v11/video_inputs/{id}/panic_alarm  {"status":"ON"|"OFF"} → 204

The original integration used PUT /acoustic_alarm with {"enabled":True} but
that endpoint exists only on CAMERA_360 Gen1 — which has no integrated siren
anyway (confirmed with hardware owner). Gen2 cameras (HOME_Eyes_Indoor /
HOME_Eyes_Outdoor) require the panic_alarm endpoint with status string.

The acoustic_alarm button was removed from button.py async_setup_entry,
the BoschAcousticAlarmButton class is kept only for backward registry compat.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "22222222-2222-2222-2222-222222222222"  # Innenkamera II (Gen2)


def _make_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={CAM_ID: {
            "info": {
                "title": "Innenbereich",
                "hardwareVersion": "HOME_Eyes_Indoor",
                "firmwareVersion": "9.40.25",
                "macAddress": "aa:bb:cc:30:68:29",
            },
        }},
        _panic_alarm_cache={},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


@pytest.mark.asyncio
async def test_turn_on_sends_status_on(stub_entry) -> None:
    """The mitm capture pins the exact body Bosch expects."""
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord()
    entity = BoschPanicAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    coord.async_put_camera.assert_called_once()
    args = coord.async_put_camera.call_args.args
    assert args[1] == "panic_alarm", "Endpoint must be /panic_alarm (not /acoustic_alarm)"
    assert args[2] == {"status": "ON"}, "Body MUST be {\"status\":\"ON\"} per mitm capture"
    assert coord._panic_alarm_cache[CAM_ID] is True


@pytest.mark.asyncio
async def test_turn_off_sends_status_off(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord()
    coord._panic_alarm_cache[CAM_ID] = True
    entity = BoschPanicAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_off()

    args = coord.async_put_camera.call_args.args
    assert args[1] == "panic_alarm"
    assert args[2] == {"status": "OFF"}
    assert coord._panic_alarm_cache[CAM_ID] is False


@pytest.mark.asyncio
async def test_is_on_tracks_local_cache(stub_entry) -> None:
    """No GET endpoint exists — track last-sent state locally."""
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord()
    entity = BoschPanicAlarmSwitch(coord, CAM_ID, stub_entry)
    assert entity.is_on is False

    coord._panic_alarm_cache[CAM_ID] = True
    assert entity.is_on is True


@pytest.mark.asyncio
async def test_failed_put_does_not_set_cache(stub_entry) -> None:
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord()
    coord.async_put_camera = AsyncMock(return_value=False)
    entity = BoschPanicAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coord._panic_alarm_cache.get(CAM_ID) is None or coord._panic_alarm_cache[CAM_ID] is False, (
        "Bei PUT-Fehler: Cache nicht True setzen (sonst Switch zeigt ON ohne Wirkung)"
    )


def test_disabled_by_default(stub_entry) -> None:
    """Panic-Alarm Switch ist standardmäßig deaktiviert (75 dB ist laut, nichts
    für Default-Dashboard). User muss explizit aktivieren."""
    from custom_components.bosch_shc_camera.switch import BoschPanicAlarmSwitch

    coord = _make_coord()
    entity = BoschPanicAlarmSwitch(coord, CAM_ID, stub_entry)
    assert entity._attr_entity_registry_enabled_default is False
