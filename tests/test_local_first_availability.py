"""Entities that need no Bosch cloud stay usable while the cloud poll fails,
but only when the camera is served over its local data interface.

Cloud-only entities (camera light, privacy, notifications, ...) deliberately
keep following the cloud poll: they could not be operated anyway."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import custom_components.bosch_smart_camera.number as number_mod
import custom_components.bosch_smart_camera.select as select_mod
import custom_components.bosch_smart_camera.switch as switch_mod
from custom_components.bosch_smart_camera.local_data_interface import STATE_ACTIVE

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _coord(
    *, cloud_ok: bool, status: str = "ONLINE", reachable: bool | None = True
) -> SimpleNamespace:
    return SimpleNamespace(
        last_update_success=cloud_ok,
        data={CAM_ID: {"status": status, "info": {"title": "Terrasse"}}},
        is_camera_online=lambda cam_id: status == "ONLINE",
        is_lan_reachable=lambda cam_id: reachable,
        local_data_interface_cache={CAM_ID: {"state": STATE_ACTIVE}},
        entry=SimpleNamespace(options={"local_passwords": {CAM_ID: "pw"}}),
        get_cam_lan_ip=lambda cam_id: "10.0.0.5",
        ldi_open_status={},
        shc_state_cache={CAM_ID: {"front_light_intensity": 0.5}},
        live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
        is_updating=lambda cam_id: False,
    )


def _entity(cls: type, coord: SimpleNamespace):
    entity = cls.__new__(cls)
    entity.coordinator = coord
    entity._cam_id = CAM_ID
    return entity


SELECTS = [
    select_mod.BoschVideoQualitySelect,
    select_mod.BoschNvrModeSelect,
    select_mod.BoschStreamModeSelect,
]


@pytest.mark.parametrize("cls", SELECTS)
@pytest.mark.parametrize(
    ("cloud_ok", "reachable", "expected"),
    [
        (True, True, True),
        (False, True, True),  # cloud down, served locally
        (False, False, False),  # cloud down and camera not seen on the LAN
        (False, None, False),  # reachability unknown
    ],
)
def test_select_available(cls, cloud_ok, reachable, expected):
    entity = _entity(cls, _coord(cloud_ok=cloud_ok, reachable=reachable))
    assert entity.available is expected


@pytest.mark.parametrize(
    ("cloud_ok", "reachable", "expected"),
    [(False, True, True), (False, False, False), (True, True, True)],
)
def test_external_stream_switch_available(cloud_ok, reachable, expected):
    entity = _entity(
        switch_mod.BoschExternalStreamSwitch,
        _coord(cloud_ok=cloud_ok, reachable=reachable),
    )
    assert entity.available is expected


@pytest.mark.parametrize(
    ("cloud_ok", "status", "reachable", "expected"),
    [
        (False, "ONLINE", True, True),  # cloud down, served locally
        (False, "ONLINE", False, False),
        (True, "OFFLINE", False, False),  # cloud OFFLINE and LAN dead
        (True, "ONLINE", False, True),  # legacy path: cloud healthy
    ],
)
def test_audio_switch_available(cloud_ok, status, reachable, expected):
    entity = _entity(
        switch_mod.BoschAudioSwitch,
        _coord(cloud_ok=cloud_ok, status=status, reachable=reachable),
    )
    assert entity.available is expected


@pytest.mark.parametrize(
    ("cloud_ok", "status", "reachable", "connection", "expected"),
    [
        (False, "ONLINE", True, "LOCAL", True),  # cloud down, served locally
        (False, "ONLINE", False, "LOCAL", False),
        (True, "ONLINE", False, "LOCAL", True),  # legacy path
        (True, "ONLINE", False, "REMOTE", False),  # recording needs LOCAL
        (True, "OFFLINE", False, "LOCAL", False),
    ],
)
def test_nvr_switch_available(cloud_ok, status, reachable, connection, expected):
    coord = _coord(cloud_ok=cloud_ok, status=status, reachable=reachable)
    coord.live_connections = {CAM_ID: {"_connection_type": connection}}
    entity = _entity(switch_mod.BoschNvrRecordingSwitch, coord)
    assert entity.available is expected


@pytest.mark.parametrize(
    ("cloud_ok", "reachable", "expected"),
    [(False, True, True), (False, False, False), (True, False, False)],
)
def test_audio_volume_available(cloud_ok, reachable, expected):
    """Cloud healthy but camera OFFLINE per cloud and LAN dead stays unavailable."""
    coord = _coord(cloud_ok=cloud_ok, status="OFFLINE", reachable=reachable)
    entity = SimpleNamespace(coordinator=coord, _cam_id=CAM_ID)
    if expected:
        coord.is_camera_online = lambda cam_id: True
    assert number_mod._audio_volume_available(entity) is expected
