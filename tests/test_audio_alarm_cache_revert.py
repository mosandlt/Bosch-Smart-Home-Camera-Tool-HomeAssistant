"""Regression test — Geraeusch-Erkennung Switch revertet sofort zurück auf OFF.

User-Report Thomas (2026-05-13): `switch.bosch_innenbereich_audio_plus`
("Bosch <cam> Geraeusch-Erkennung") flickte beim Einschalten kurz auf ON und
sprang dann selbsttätig zurück auf OFF — kein Persist über den nächsten
Coordinator-Slow-Tick hinaus.

Root cause: `BoschAudioAlarmSwitch._set` schickte zwar erfolgreich
`PUT /v11/video_inputs/{id}/audioAlarm` mit `enabled=True`, aber
- update nur in `coordinator.data[cam_id]["audioAlarm"]` (transient, 60 s)
- KEIN update in `coordinator._audio_alarm_cache` (persistent, source of truth
  für `is_on` via `audio_alarm_settings()`)
- KEIN write-lock `coordinator._audio_alarm_set_at[cam_id]`

Effekt: nächster Slow-Tier-Poll (alle 300 s) überschrieb `_audio_alarm_cache`
mit dem stale Cloud-Wert `enabled=False`, weil der Write-Lock nicht aktiv
war. Switch zeigte wieder OFF.

Vergleich: `BoschAlarmModeSwitch` setzt korrekt `_alarm_settings_cache` —
selbes Pattern fehlte hier seit jeher.

Fix: Switch updated nun beide Caches + Write-Lock-Timestamp.
"""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "22222222-2222-2222-2222-222222222222"  # Innenbereich Gen2 (Reporter)


def _make_coord_with_caches() -> SimpleNamespace:
    """Stub coordinator including the audio-alarm cache + write-lock dicts."""
    settings = {
        "enabled": False,
        "threshold": 50,
        "sensitivity": "MEDIUM",
        "audioAlarmConfiguration": "CUSTOM",
    }
    coord = SimpleNamespace(
        data={CAM_ID: {
            "info": {
                "title": "Innenbereich",
                "hardwareVersion": "HOME_Eyes_Indoor",
                "firmwareVersion": "9.40.25",
                "macAddress": "aa:bb:cc:30:68:29",
                "featureSupport": {"sound": True},
            },
            "audioAlarm": dict(settings),
        }},
        _audio_alarm_cache={CAM_ID: dict(settings)},
        _audio_alarm_set_at={},
        _shc_state_cache={CAM_ID: {"privacy_mode": False}},
        last_update_success=True,
        is_camera_online=lambda cid: True,
        # audio_alarm_settings reads from cache like in production
        audio_alarm_settings=lambda cid: None,  # set per-test
        async_put_camera=AsyncMock(return_value=True),
    )
    coord.audio_alarm_settings = lambda cid: coord._audio_alarm_cache.get(cid, {})
    return coord


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


@pytest.mark.asyncio
async def test_turn_on_updates_audio_alarm_cache(stub_entry) -> None:
    """Nach erfolgreichem PUT muss _audio_alarm_cache aktualisiert sein —
    sonst flickt is_on direkt zurück auf False sobald das nächste Tick
    den Switch-Status liest (data[]["audioAlarm"] ist nicht source of truth)."""
    from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch

    coord = _make_coord_with_caches()
    entity = BoschAudioAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coord._audio_alarm_cache[CAM_ID]["enabled"] is True, (
        "_audio_alarm_cache muss nach turn_on True sein — sonst kehrt is_on "
        "sofort wieder auf False zurück"
    )
    # Preserved other fields (sensitivity/threshold/config) — must not be wiped
    assert coord._audio_alarm_cache[CAM_ID]["threshold"] == 50
    assert coord._audio_alarm_cache[CAM_ID]["audioAlarmConfiguration"] == "CUSTOM"


@pytest.mark.asyncio
async def test_turn_on_sets_write_lock(stub_entry) -> None:
    """Write-Lock-Timestamp muss gesetzt sein — sonst überschreibt der
    nächste Slow-Tier-Poll (alle 300 s) den Cache mit dem stale Cloud-Wert."""
    from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch

    coord = _make_coord_with_caches()
    entity = BoschAudioAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    before = time.monotonic()
    await entity.async_turn_on()
    after = time.monotonic()

    assert CAM_ID in coord._audio_alarm_set_at, (
        "_audio_alarm_set_at[cam_id] muss gesetzt sein als Write-Lock — "
        "sonst revertet der nächste Slow-Tier-Poll den Cache"
    )
    ts = coord._audio_alarm_set_at[CAM_ID]
    assert before <= ts <= after, "Write-Lock-Timestamp muss aus dem turn_on stammen"


@pytest.mark.asyncio
async def test_turn_off_also_updates_cache_and_lock(stub_entry) -> None:
    """Gleicher Vertrag für turn_off."""
    from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch

    coord = _make_coord_with_caches()
    # Start in ON state to test the OFF transition
    coord._audio_alarm_cache[CAM_ID]["enabled"] = True
    entity = BoschAudioAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_off()

    assert coord._audio_alarm_cache[CAM_ID]["enabled"] is False
    assert CAM_ID in coord._audio_alarm_set_at


@pytest.mark.asyncio
async def test_put_body_pairs_audioAlarmConfiguration_with_enabled(stub_entry) -> None:
    """Bosch-Cloud-Bug aus mitmproxy capture 2026-05-13: PUT mit
    enabled=true UND audioAlarmConfiguration="OFF" wird 204'd, aber
    NICHT angewandt. Der PUT-Body MUSS audioAlarmConfiguration entlang
    von enabled mit-flippen — "CUSTOM" für ON, "OFF" für OFF. Die iOS-App
    macht genau das."""
    from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch

    coord = _make_coord_with_caches()
    # GET response carries audioAlarmConfiguration="OFF" while disabled —
    # genau der Initialzustand aus dem Capture
    coord._audio_alarm_cache[CAM_ID]["audioAlarmConfiguration"] = "OFF"
    entity = BoschAudioAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    # Inspect what was sent to the cloud
    coord.async_put_camera.assert_called_once()
    args, kwargs = coord.async_put_camera.call_args
    sent_body = args[2] if len(args) >= 3 else kwargs.get("payload")
    assert sent_body["enabled"] is True
    assert sent_body["audioAlarmConfiguration"] == "CUSTOM", (
        "audioAlarmConfiguration MUSS auf 'CUSTOM' geflippt werden — "
        "sonst 204't Bosch silent ohne Anwendung"
    )


@pytest.mark.asyncio
async def test_put_body_resets_audioAlarmConfiguration_off(stub_entry) -> None:
    """turn_off muss audioAlarmConfiguration auf 'OFF' setzen."""
    from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch

    coord = _make_coord_with_caches()
    coord._audio_alarm_cache[CAM_ID]["enabled"] = True
    coord._audio_alarm_cache[CAM_ID]["audioAlarmConfiguration"] = "CUSTOM"
    entity = BoschAudioAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_off()

    args, kwargs = coord.async_put_camera.call_args
    sent_body = args[2] if len(args) >= 3 else kwargs.get("payload")
    assert sent_body["enabled"] is False
    assert sent_body["audioAlarmConfiguration"] == "OFF"


@pytest.mark.asyncio
async def test_failed_put_does_not_corrupt_cache(stub_entry) -> None:
    """Wenn die PUT-API fehlschlägt, dürfen Cache + Write-Lock NICHT
    gesetzt werden — sonst zeigt der Switch ON obwohl die Cloud OFF hat."""
    from custom_components.bosch_shc_camera.switch import BoschAudioAlarmSwitch

    coord = _make_coord_with_caches()
    coord.async_put_camera = AsyncMock(return_value=False)  # simulate API failure
    entity = BoschAudioAlarmSwitch(coord, CAM_ID, stub_entry)
    entity.async_write_ha_state = MagicMock()

    await entity.async_turn_on()

    assert coord._audio_alarm_cache[CAM_ID]["enabled"] is False, (
        "Bei PUT-Fehlschlag muss der Cache OFF bleiben"
    )
    assert CAM_ID not in coord._audio_alarm_set_at, (
        "Bei PUT-Fehlschlag darf kein Write-Lock gesetzt werden"
    )
