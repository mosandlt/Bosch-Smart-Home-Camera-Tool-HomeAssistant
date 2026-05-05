"""Service-handler input validation tests.

Verifies that every service handler that accepts user input rejects bad
input with `ServiceValidationError` and routes the message through the
translation layer (`translation_domain`, `translation_key`, `translation_placeholders`).

These tests do NOT hit the cloud API — they only exercise the input
validation gate before the network call. High-value coverage of the
new Silver `action-exceptions` rule with no aiohttp mocking required.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_shc_camera.const import DOMAIN


# Each test case is: (service_name, bad_data, expected_translation_key)
INVALID_INPUT_CASES = [
    # camera_id required
    ("open_live_connection", {}, "argument_required"),
    ("set_motion_zones", {"zones": []}, "argument_required"),
    ("get_motion_zones", {}, "argument_required"),
    ("get_privacy_masks", {}, "argument_required"),
    ("set_privacy_masks", {"masks": []}, "argument_required"),
    ("get_lighting_schedule", {}, "argument_required"),
    # camera_id + something else
    ("update_rule", {"camera_id": ""}, "argument_required"),
    ("delete_motion_zone", {"camera_id": "", "zone_index": 0}, "argument_required"),
    ("rename_camera", {"camera_id": "abc"}, "argument_required"),
    # share_camera: friend_id + camera_ids
    ("share_camera", {}, "argument_required"),
    ("share_camera", {"friend_id": "fid"}, "argument_required"),
    # invite_friend: email
    ("invite_friend", {}, "argument_required"),
    # remove_friend: friend_id
    ("remove_friend", {}, "argument_required"),
    # set_motion_zones: zones must be list
    ("set_motion_zones", {"camera_id": "abc", "zones": "not-a-list"}, "argument_must_be_list"),
    ("set_privacy_masks", {"camera_id": "abc", "masks": "not-a-list"}, "argument_must_be_list"),
]


@pytest.fixture
async def setup_services(hass: HomeAssistant):
    """Register the services without a coordinator (no cloud calls happen).

    The service handlers raise ServiceValidationError BEFORE iterating
    config entries, so we don't need a real coordinator for input-validation
    tests.
    """
    from custom_components.bosch_shc_camera import _register_services
    _register_services(hass)
    yield
    # Cleanup — services are domain-level, leave them registered for the
    # next test in the session (idempotent registration via has_service guard)


@pytest.mark.parametrize("service_name,bad_data,expected_key", INVALID_INPUT_CASES)
async def test_service_rejects_missing_argument(
    hass: HomeAssistant,
    setup_services,
    service_name: str,
    bad_data: dict,
    expected_key: str,
) -> None:
    """Bad input must raise ServiceValidationError with the expected translation_key."""
    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN, service_name, bad_data, blocking=True
        )
    err = exc_info.value
    assert err.translation_domain == DOMAIN, (
        f"{service_name}: expected translation_domain={DOMAIN!r}, "
        f"got {err.translation_domain!r}"
    )
    assert err.translation_key == expected_key, (
        f"{service_name}: expected translation_key={expected_key!r}, "
        f"got {err.translation_key!r}"
    )


async def test_set_motion_zones_rejects_missing_field(
    hass: HomeAssistant, setup_services
) -> None:
    """Zone missing 'x' must raise the missing_field translation key."""
    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN, "set_motion_zones",
            {"camera_id": "abc", "zones": [{"y": 0.5, "w": 0.1, "h": 0.1}]},
            blocking=True,
        )
    assert exc_info.value.translation_key == "missing_field"
    placeholders = exc_info.value.translation_placeholders or {}
    assert placeholders.get("kind") == "zone"
    assert placeholders.get("field") == "x"


async def test_set_motion_zones_rejects_out_of_range(
    hass: HomeAssistant, setup_services
) -> None:
    """Zone coord >1.0 must raise the value_out_of_range translation key."""
    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN, "set_motion_zones",
            {"camera_id": "abc", "zones": [{"x": 1.5, "y": 0.5, "w": 0.1, "h": 0.1}]},
            blocking=True,
        )
    assert exc_info.value.translation_key == "value_out_of_range"
    placeholders = exc_info.value.translation_placeholders or {}
    assert placeholders.get("kind") == "zone"
    assert placeholders.get("field") == "x"


async def test_set_privacy_masks_rejects_missing_field(
    hass: HomeAssistant, setup_services
) -> None:
    """Mask missing 'h' must raise missing_field with kind=mask."""
    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN, "set_privacy_masks",
            {"camera_id": "abc", "masks": [{"x": 0.1, "y": 0.1, "w": 0.1}]},
            blocking=True,
        )
    assert exc_info.value.translation_key == "missing_field"
    placeholders = exc_info.value.translation_placeholders or {}
    assert placeholders.get("kind") == "mask"
    assert placeholders.get("field") == "h"


async def test_delete_motion_zone_rejects_negative_index(
    hass: HomeAssistant, setup_services
) -> None:
    """Negative zone_index is treated as missing — argument_required."""
    with pytest.raises(ServiceValidationError) as exc_info:
        await hass.services.async_call(
            DOMAIN, "delete_motion_zone",
            {"camera_id": "abc", "zone_index": -1},
            blocking=True,
        )
    assert exc_info.value.translation_key == "argument_required"
