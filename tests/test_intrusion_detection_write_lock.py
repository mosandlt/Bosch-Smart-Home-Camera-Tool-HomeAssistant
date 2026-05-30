"""Regression test — Einbrucherkennung Switch revertet nach Slow-Tier-Poll.

Spotted alongside the Geraeusch-Erkennung audio-alarm bug (Thomas, 2026-05-13):
`BoschIntrusionDetectionSwitch._set_intrusion` updated zwar
`_intrusion_config_cache` nach erfolgreichem PUT, setzte aber KEINEN
Write-Lock-Timestamp — und der Slow-Tier-Endpoint-Handler in __init__.py
schrieb den Cache zurück auf den (noch nicht propagierten) Cloud-Wert.

Symptom: User toggelt "Einbrucherkennung" auf ON, switch zeigt ON, dann nach
maximal 300 s (Slow-Tier-Interval) springt er auf OFF zurück bis Bosch-Cloud
endgültig konsistent ist.

Fix: switch setzt `_intrusion_config_set_at[cam_id]` und der Endpoint-Handler
checked `_is_write_locked()` bevor er den Cache überschreibt.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "22222222-2222-2222-2222-222222222222"  # Innenkamera II (Gen2)


def _make_coord() -> SimpleNamespace:
    cfg = {
        "enabled": False,
        "sensitivity": 3,
        "detectionMode": "ZONES",
        "distance": 5,
    }
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": "HOME_Eyes_Indoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                },
            }
        },
        _intrusion_config_cache={CAM_ID: dict(cfg)},
        _intrusion_config_set_at={},
        _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
    )
    return coord


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


@pytest.mark.asyncio
async def test_turn_on_sets_intrusion_write_lock(stub_entry) -> None:
    """Nach turn_on muss _intrusion_config_set_at[cam_id] gesetzt sein
    — sonst überschreibt der nächste Slow-Tier-Poll (300 s) den Cache mit
    dem stale Cloud-Wert."""
    from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch

    coord = _make_coord()
    entity = BoschIntrusionDetectionSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    before = time.monotonic()
    await entity.async_turn_on()
    after = time.monotonic()

    assert coord._intrusion_config_cache[CAM_ID]["enabled"] is True
    assert CAM_ID in coord._intrusion_config_set_at, (
        "Write-Lock-Timestamp muss gesetzt sein nach erfolgreichem PUT"
    )
    ts = coord._intrusion_config_set_at[CAM_ID]
    assert before <= ts <= after


@pytest.mark.asyncio
async def test_failed_put_does_not_set_write_lock(stub_entry) -> None:
    """Wenn PUT fehlschlägt: weder Cache noch Write-Lock anfassen."""
    from custom_components.bosch_shc_camera.switch import BoschIntrusionDetectionSwitch

    coord = _make_coord()
    coord.async_put_camera = AsyncMock(return_value=False)
    entity = BoschIntrusionDetectionSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coord._intrusion_config_cache[CAM_ID]["enabled"] is False, (
        "Bei PUT-Fehler bleibt Cache unverändert"
    )
    assert CAM_ID not in coord._intrusion_config_set_at, (
        "Bei PUT-Fehler kein Write-Lock"
    )


def test_coordinator_has_intrusion_set_at_attribute() -> None:
    """Smoke: das neue Write-Lock-Dict `_intrusion_config_set_at` existiert
    am Coordinator-Init. Wenn dieses Attribut wegfällt, fliegt der Switch
    beim ersten turn_on mit AttributeError raus."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    # Constructor-Smoke ohne Mocking via class-attribute introspection
    init_src = BoschCameraCoordinator.__init__.__code__
    # Walk co_consts looking for the attribute name string literal.
    # If the attribute is removed, this test fails loudly.
    co_names = set(init_src.co_names)
    assert "_intrusion_config_set_at" in co_names, (
        "BoschCameraCoordinator.__init__ darf `_intrusion_config_set_at` "
        "nicht entfernen — sonst bricht der Intrusion-Switch beim turn_on"
    )
