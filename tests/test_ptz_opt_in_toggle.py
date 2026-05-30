"""Tests for `enable_ptz_controls` opt-in toggle.

PIN_EVERY_MODE: explicit tests for default, disabled, enabled, and the
panLimit gate (so enabling the toggle on a non-pan camera still creates
no entity).

Covers:
  - DEFAULT_OPTIONS contains `enable_ptz_controls: False`
  - select platform: panLimit > 0 + toggle OFF → no BoschPanPresetSelect
  - select platform: panLimit > 0 + toggle ON  → BoschPanPresetSelect created
  - select platform: panLimit = 0 + toggle ON  → still no entity
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.bosch_shc_camera.const import (
    CONF_ENABLE_PTZ_CONTROLS,
    DEFAULT_OPTIONS,
)

CAM_PAN = "22222222-AAAA-BBBB-CCCC-000000000001"  # CAMERA_360, has pan
CAM_NOPAN = "11111111-AAAA-BBBB-CCCC-000000000001"  # Gen2 outdoor, no pan


def _coord(*, with_pan: bool):
    pan_limit = 120 if with_pan else 0
    hw = "CAMERA_360" if with_pan else "HOME_Eyes_Outdoor"
    cam_id = CAM_PAN if with_pan else CAM_NOPAN
    return SimpleNamespace(
        data={
            cam_id: {
                "info": {
                    "title": "Test cam",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:00:00:00",
                    "featureSupport": {"panLimit": pan_limit},
                }
            }
        },
        _pan_cache={cam_id: 0},
        _image_rotation_180={},
        last_update_success=True,
        async_cloud_set_pan=AsyncMock(return_value=True),
    )


def _entry(*, ptz_enabled: bool):
    return SimpleNamespace(
        options={CONF_ENABLE_PTZ_CONTROLS: ptz_enabled},
        entry_id="01TEST",
        title="Bosch",
        runtime_data=None,
    )


# ── PIN_EVERY_MODE ──────────────────────────────────────────────────────────


def test_default_options_has_ptz_disabled() -> None:
    """`enable_ptz_controls` must be in DEFAULT_OPTIONS and False by default."""
    assert "enable_ptz_controls" in DEFAULT_OPTIONS
    assert DEFAULT_OPTIONS["enable_ptz_controls"] is False


@pytest.mark.asyncio
async def test_pan_select_NOT_created_when_toggle_disabled() -> None:
    """panLimit > 0 + toggle off → no BoschPanPresetSelect entity."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    entry = _entry(ptz_enabled=False)
    entry.runtime_data = coord
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == [], (
        f"BoschPanPresetSelect must NOT be created when enable_ptz_controls=False. "
        f"Got {len(pan_selects)} entities."
    )


@pytest.mark.asyncio
async def test_pan_select_created_when_toggle_enabled() -> None:
    """panLimit > 0 + toggle on → exactly one BoschPanPresetSelect entity."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    entry = _entry(ptz_enabled=True)
    entry.runtime_data = coord
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert len(pan_selects) == 1


@pytest.mark.asyncio
async def test_pan_select_NOT_created_on_non_pan_camera_even_when_toggle_enabled() -> (
    None
):
    """panLimit = 0 + toggle on → still no entity (gate is panLimit AND toggle)."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=False)
    entry = _entry(ptz_enabled=True)
    entry.runtime_data = coord
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == []


@pytest.mark.asyncio
async def test_pan_select_options_value_missing_treated_as_disabled() -> None:
    """Missing key in options dict → fallback False → entity not created."""
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    entry = SimpleNamespace(
        options={},  # key missing entirely
        entry_id="01TEST",
        title="Bosch",
        runtime_data=coord,
    )
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == [], "Missing key must be treated as disabled (default off)."


@pytest.mark.asyncio
async def test_pan_select_options_value_garbage_collapses_to_disabled() -> None:
    """Non-bool truthy garbage in options collapses to disabled via bool-coerce
    in config_flow.py. Here we test that the runtime gate works either way:
    only literal True enables. (None / 0 / "" → False.)
    """
    from custom_components.bosch_shc_camera.select import async_setup_entry

    coord = _coord(with_pan=True)
    # explicit None — config_flow bool-coerce makes this False on submit, but
    # legacy options dicts written before the field existed contain no entry.
    entry = SimpleNamespace(
        options={CONF_ENABLE_PTZ_CONTROLS: None},
        entry_id="01TEST",
        title="Bosch",
        runtime_data=coord,
    )
    added: list = []
    await async_setup_entry(MagicMock(), entry, lambda ents: added.extend(ents))
    pan_selects = [e for e in added if type(e).__name__ == "BoschPanPresetSelect"]
    assert pan_selects == []
