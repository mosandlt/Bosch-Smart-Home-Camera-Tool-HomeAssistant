"""A camera whose cloud JSON carries null fields must not crash setup (GitHub #87).

Source: GitHub #87 — Gen1 Eyes Outdoor returned ``"featureSupport": null``;
``.get(key, default)`` returns None for a present-but-null key, so every
chained ``.get``/``.upper`` raised and the entry stayed in setup_retry.
These tests drive the coordinator info pass, the slow-tier context and every
entity platform's ``async_setup_entry`` + state properties with such a camera.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bosch_smart_camera.const import DOMAIN
from custom_components.bosch_smart_camera.coordinator import BoschCameraCoordinator
from custom_components.bosch_smart_camera.slow_tier import (
    _compute_cam_context,
    _poll_cam_info_caches,
)

CAM = "11111111-1111-1111-1111-111111111111"

VARIANTS: dict[str, dict[str, Any]] = {
    "gen1_outdoor_nulls": {
        "id": CAM,
        "title": "Garden",
        "hardwareVersion": "OUTDOOR",
        "firmwareVersion": "7.91.56",
        "featureSupport": None,
        "featureStatus": None,
        "privacyMode": None,
    },
    "all_null": {
        "id": CAM,
        "title": None,
        "hardwareVersion": None,
        "firmwareVersion": None,
        "macAddress": None,
        "featureSupport": None,
        "featureStatus": None,
        "privacyMode": None,
        "notificationsEnabledStatus": None,
    },
    "keys_missing": {"id": CAM},
}

PLATFORMS = (
    "binary_sensor",
    "button",
    "camera",
    "image",
    "light",
    "number",
    "select",
    "sensor",
    "switch",
    "text",
    "update",
)

# State properties HA reads right after entity registration. Translation-
# based ones (name, entity_picture) need a registered platform and are skipped.
PROPS = (
    "available",
    "device_info",
    "is_on",
    "native_value",
    "current_option",
    "options",
    "extra_state_attributes",
    "brightness",
    "installed_version",
    "latest_version",
    "icon",
    "supported_features",
    "native_min_value",
    "native_max_value",
    "color_mode",
)


def _coordinator(hass: HomeAssistant, info: dict[str, Any]) -> BoschCameraCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"bearer_token": "t", "refresh_token": "r"},
        options={},
        unique_id=DOMAIN,
        version=1,
    )
    entry.add_to_hass(hass)
    coord = BoschCameraCoordinator(hass, entry)
    coord.data = {CAM: {"info": info, "status": "ONLINE", "events": []}}
    entry.runtime_data = coord
    return coord


@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_info_pass_and_slow_tier_context(
    hass: HomeAssistant, variant: str
) -> None:
    info = dict(VARIANTS[variant])
    coord = _coordinator(hass, info)
    _poll_cam_info_caches(coord, CAM, info)
    ctx = _compute_cam_context(coord, CAM, info, coord.data, {}, True)
    assert ctx.privacy_on is False
    assert ctx.has_light is False
    assert ctx.pan_limit == 0


@pytest.mark.parametrize(
    ("privacy", "expected"),
    [("ON", True), ("on", True), ("OFF", False), (None, False), ("", False)],
)
async def test_privacy_on_values(
    hass: HomeAssistant, privacy: str | None, expected: bool
) -> None:
    """Normal string values keep their old meaning; null maps to OFF."""
    info = {"id": CAM, "hardwareVersion": "OUTDOOR", "privacyMode": privacy}
    coord = _coordinator(hass, info)
    ctx = _compute_cam_context(coord, CAM, info, coord.data, {}, True)
    assert ctx.privacy_on is expected


@pytest.mark.parametrize("variant", list(VARIANTS))
async def test_every_platform_sets_up(hass: HomeAssistant, variant: str) -> None:
    coord = _coordinator(hass, dict(VARIANTS[variant]))
    entry = coord.entry
    errors: list[str] = []
    for plat in PLATFORMS:
        mod = importlib.import_module(f"custom_components.bosch_smart_camera.{plat}")
        added: list[Any] = []

        def _add(ents: Any, *_a: Any, _added: list[Any] = added, **_k: Any) -> None:
            _added.extend(ents)

        try:
            await mod.async_setup_entry(hass, entry, _add)
        except Exception as exc:
            errors.append(f"{plat} setup: {exc!r}")
            continue
        for ent in added:
            ent.hass = hass
            for prop in PROPS:
                if not hasattr(type(ent), prop):
                    continue
                try:
                    getattr(ent, prop)
                except Exception as exc:
                    errors.append(f"{plat} {type(ent).__name__}.{prop}: {exc!r}")
    # Platforms that add coordinator listeners arm the refresh timer.
    coord._unschedule_refresh()
    assert not errors


@pytest.mark.asyncio
async def test_shc_state_update_tolerates_null_title_and_device_name():
    """GitHub #87 follow-up: a null camera title / SHC device name must not
    raise AttributeError on `.lower()`; the camera simply has no SHC match."""
    from types import SimpleNamespace

    from custom_components.bosch_smart_camera.shc import _update_one_camera_shc_state

    coord = SimpleNamespace(shc_state_cache={})
    cam = {"info": {"title": None}}
    await _update_one_camera_shc_state(
        coord, "11111111-1111-1111-1111-111111111111", cam, [{"name": None, "id": "x"}]
    )
    assert coord.shc_state_cache == {}
    await _update_one_camera_shc_state(
        coord, "11111111-1111-1111-1111-111111111111", {"info": None}, []
    )
    assert coord.shc_state_cache == {}


@pytest.mark.asyncio
async def test_shc_state_update_skips_null_device_entries() -> None:
    """A null / nameless entry in the SHC /devices list is skipped, not fatal."""
    from types import SimpleNamespace

    from custom_components.bosch_smart_camera.shc import _update_one_camera_shc_state

    coord = SimpleNamespace(shc_state_cache={})
    cam = {"info": {"title": "Terrasse"}}
    await _update_one_camera_shc_state(
        coord,
        "11111111-1111-1111-1111-111111111111",
        cam,
        [None, {"name": None, "id": "x"}],
    )
    assert coord.shc_state_cache == {}
