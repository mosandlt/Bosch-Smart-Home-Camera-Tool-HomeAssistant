"""Video-source sensor: which path a camera's video takes.

Pins every source state (local, local problem per reason, cloud because no
local password is stored, cloud for inactive/unsupported/unknown interface x
password present/empty), the attributes, and the sensor's enum options.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.bosch_shc_camera.ldi_local import (
    LDI_PASSWORDS_OPTION,
    SOURCE_CLOUD,
    SOURCE_CLOUD_UNCONFIGURED,
    SOURCE_LDI,
    SOURCE_LDI_PROBLEM,
    VIDEO_SOURCE_OPTIONS,
    ldi_video_source,
)
from custom_components.bosch_shc_camera.sensor import (
    VIDEO_SOURCE_DESCRIPTION,
    BoschVideoSourceSensor,
)

CAM = "11111111-1111-1111-1111-111111111111"


def _coord(
    state: str | None,
    password: object = "fake-pw",
    status: dict[str, Any] | None = None,
    live: dict[str, Any] | None = None,
) -> SimpleNamespace:
    cache = {CAM: {"state": state}} if state else {}
    return SimpleNamespace(
        local_data_interface_cache=cache,
        entry=SimpleNamespace(options={LDI_PASSWORDS_OPTION: {CAM: password}}),
        ldi_open_status={CAM: status} if status else {},
        live_connections={CAM: live} if live else {},
        firmware_cache={},
        data={CAM: {}},
        last_update_success=True,
    )


def test_active_with_password_is_local() -> None:
    assert ldi_video_source(_coord("active"), CAM) == (SOURCE_LDI, None)


@pytest.mark.parametrize(
    "reason", ["auth", "unreachable", "no_ip", "no_go2rtc", "something_new"]
)
def test_wanted_with_failed_open_is_problem(reason: str) -> None:
    coord = _coord("active", status={"reason": reason, "since": 0.0})
    assert ldi_video_source(coord, CAM) == (SOURCE_LDI_PROBLEM, reason)


@pytest.mark.parametrize("password", ["", "   ", None, 5])
def test_active_without_usable_password_is_cloud_no_password(password: object) -> None:
    assert ldi_video_source(_coord("active", password), CAM) == (
        SOURCE_CLOUD_UNCONFIGURED,
        None,
    )


@pytest.mark.parametrize("state", ["inactive", "unsupported", None, "garbage"])
def test_interface_not_active_is_cloud(state: str | None) -> None:
    assert ldi_video_source(_coord(state), CAM) == (SOURCE_CLOUD, None)


def test_stale_failure_without_wanted_is_ignored() -> None:
    coord = _coord("inactive", status={"reason": "auth", "since": 0.0})
    assert ldi_video_source(coord, CAM) == (SOURCE_CLOUD, None)


def test_every_source_is_a_declared_option() -> None:
    assert set(VIDEO_SOURCE_OPTIONS) == {
        SOURCE_LDI,
        SOURCE_LDI_PROBLEM,
        SOURCE_CLOUD_UNCONFIGURED,
        SOURCE_CLOUD,
    }
    assert VIDEO_SOURCE_DESCRIPTION.options == VIDEO_SOURCE_OPTIONS


def _entity(coord: SimpleNamespace) -> BoschVideoSourceSensor:
    ent = BoschVideoSourceSensor.__new__(BoschVideoSourceSensor)
    ent.coordinator = coord  # type: ignore[assignment]
    ent._cam_id = CAM
    ent.entity_description = VIDEO_SOURCE_DESCRIPTION
    return ent


def test_native_value_and_attrs_for_problem_with_live_session() -> None:
    coord = _coord(
        "active",
        status={"reason": "auth", "since": 0.0},
        live={"_connection_type": "LOCAL"},
    )
    ent = _entity(coord)
    assert ent.native_value == SOURCE_LDI_PROBLEM
    assert ent.extra_state_attributes == {"problem": "auth", "connection": "LOCAL"}


def test_attrs_empty_without_problem_or_session() -> None:
    ent = _entity(_coord("inactive"))
    assert ent.native_value == SOURCE_CLOUD
    assert ent.extra_state_attributes == {}


def test_available_survives_failed_cloud_update() -> None:
    coord = _coord("inactive")
    ent = _entity(coord)
    assert ent.available is True
    coord.last_update_success = False
    assert ent.available is True


def test_unavailable_when_camera_not_in_data() -> None:
    coord = _coord("inactive")
    coord.data = {}
    assert _entity(coord).available is False
    coord.data = None
    assert _entity(coord).available is False


def test_unknown_state_with_qualifying_firmware_is_local() -> None:
    coord = _coord(None)
    coord.firmware_cache = {CAM: {"current": "9.40.105"}}
    assert ldi_video_source(coord, CAM) == (SOURCE_LDI, None)


def test_unknown_state_with_old_firmware_is_cloud() -> None:
    coord = _coord(None)
    coord.firmware_cache = {CAM: {"current": "9.40.104"}}
    assert ldi_video_source(coord, CAM) == (SOURCE_CLOUD, None)


@pytest.mark.parametrize(
    "status", [{"since": 0.0}, {"reason": None}, {"reason": 5}, "garbage"]
)
def test_malformed_open_status_is_not_a_problem(status: object) -> None:
    coord = _coord("active")
    coord.ldi_open_status = {CAM: status}
    assert ldi_video_source(coord, CAM) == (SOURCE_LDI, None)


@pytest.mark.parametrize(
    ("live", "expected"),
    [
        ({"_connection_type": "REMOTE"}, {"connection": "REMOTE"}),
        ({"rtspsUrl": "x"}, {"connection": ""}),
    ],
)
def test_connection_attribute(live: dict[str, Any], expected: dict[str, Any]) -> None:
    ent = _entity(_coord("inactive", live=live))
    assert ent.extra_state_attributes == expected
