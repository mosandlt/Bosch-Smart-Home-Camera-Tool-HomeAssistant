"""Tests for BoschPanPresetSelect — PTZ named-preset select entity (Gen1 360°).

Covers:
  - Construction and metadata
  - current_option: each of the 5 preset angles → matching name
  - current_option: non-preset position → None
  - current_option: cache empty → None
  - current_option: ceiling-mount (image_rotation_180) sign inversion
  - available: True / False conditions
  - async_select_option: each preset → async_cloud_set_pan called with correct angle
  - async_select_option: unknown option → no call
  - async_select_option: pan failure → cache NOT updated
  - async_select_option: ceiling-mount → angle inverted before PUT
  - PIN_EVERY_MODE: one test per preset value (home / left / right / back_left / back_right)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


CAM_ID = "22222222-AAAA-BBBB-CCCC-000000000001"


@pytest.fixture
def stub_coord():
    """Minimal coordinator stub for PTZ preset tests."""
    coord = SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Kamera",
                    "hardwareVersion": "CAMERA_360",
                    "firmwareVersion": "7.91.56",
                    "macAddress": "aa:bb:cc:08:36:27",
                    "featureSupport": {"panLimit": 120},
                }
            }
        },
        _pan_cache={CAM_ID: 0},  # parked at home position
        _image_rotation_180={},
        last_update_success=True,
        async_cloud_set_pan=AsyncMock(return_value=True),
    )
    return coord


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _make_sel(coord: SimpleNamespace, entry: SimpleNamespace) -> object:
    from custom_components.bosch_shc_camera.select import BoschPanPresetSelect
    return BoschPanPresetSelect(coord, CAM_ID, entry, pan_limit=120)


# ── Construction ──────────────────────────────────────────────────────────────


class TestPanPresetConstruction:
    def test_translation_key(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        assert sel._attr_translation_key == "pan_preset"  # type: ignore[attr-defined]

    def test_unique_id(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        assert sel._attr_unique_id == f"bosch_shc_camera_{CAM_ID}_pan_preset"  # type: ignore[attr-defined]

    def test_options_list(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        from custom_components.bosch_shc_camera.select import PAN_PRESET_OPTIONS
        assert sel._attr_options == PAN_PRESET_OPTIONS  # type: ignore[attr-defined]
        assert len(sel._attr_options) == 5  # type: ignore[attr-defined]


# ── current_option — one test per preset (PIN_EVERY_MODE) ────────────────────


class TestCurrentOptionPresetPositions:
    """Each of the 5 named angles must map back to the correct preset name."""

    def test_home_position(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache[CAM_ID] = 0
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "home"  # type: ignore[attr-defined]

    def test_left_position(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache[CAM_ID] = -60
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "left"  # type: ignore[attr-defined]

    def test_right_position(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache[CAM_ID] = 60
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "right"  # type: ignore[attr-defined]

    def test_back_left_position(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache[CAM_ID] = -120
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "back_left"  # type: ignore[attr-defined]

    def test_back_right_position(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache[CAM_ID] = 120
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "back_right"  # type: ignore[attr-defined]

    def test_between_presets_returns_none(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        """Manual slider move to a non-preset angle → current_option is None."""
        stub_coord._pan_cache[CAM_ID] = 45
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option is None  # type: ignore[attr-defined]

    def test_cache_empty_returns_none(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache = {}
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option is None  # type: ignore[attr-defined]


# ── current_option — ceiling-mount sign inversion ────────────────────────────


class TestCurrentOptionCeilingMount:
    """When _image_rotation_180 is set the pan cache value sign is inverted
    before comparing against preset angles — so the user sees the correct
    preset name even when the camera is ceiling-mounted.
    """

    def test_ceiling_home(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._image_rotation_180 = {CAM_ID: True}
        stub_coord._pan_cache[CAM_ID] = 0  # 0 inverted = 0 → home
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "home"  # type: ignore[attr-defined]

    def test_ceiling_left_shows_left(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        # Camera reports raw +60 (physical right); inversion → user sees −60 (left)
        stub_coord._image_rotation_180 = {CAM_ID: True}
        stub_coord._pan_cache[CAM_ID] = 60
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "left"  # type: ignore[attr-defined]

    def test_ceiling_right_shows_right(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._image_rotation_180 = {CAM_ID: True}
        stub_coord._pan_cache[CAM_ID] = -60
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.current_option == "right"  # type: ignore[attr-defined]


# ── available ─────────────────────────────────────────────────────────────────


class TestAvailable:
    def test_available_when_cache_populated(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache[CAM_ID] = 0
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.available is True  # type: ignore[attr-defined]

    def test_not_available_when_cache_missing(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord._pan_cache = {}
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.available is False  # type: ignore[attr-defined]

    def test_not_available_when_coordinator_failed(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        stub_coord.last_update_success = False
        sel = _make_sel(stub_coord, stub_entry)
        assert sel.available is False  # type: ignore[attr-defined]


# ── async_select_option — one test per preset (PIN_EVERY_MODE) ───────────────


class TestSelectOption:
    """Each preset must result in the correct angle being passed to async_cloud_set_pan."""

    @pytest.mark.asyncio
    async def test_select_home(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("home")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 0)
        assert stub_coord._pan_cache[CAM_ID] == 0

    @pytest.mark.asyncio
    async def test_select_left(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("left")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, -60)
        assert stub_coord._pan_cache[CAM_ID] == -60

    @pytest.mark.asyncio
    async def test_select_right(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("right")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 60)
        assert stub_coord._pan_cache[CAM_ID] == 60

    @pytest.mark.asyncio
    async def test_select_back_left(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("back_left")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, -120)
        assert stub_coord._pan_cache[CAM_ID] == -120

    @pytest.mark.asyncio
    async def test_select_back_right(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("back_right")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 120)
        assert stub_coord._pan_cache[CAM_ID] == 120

    @pytest.mark.asyncio
    async def test_unknown_option_no_call(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        """Invalid options must be silently rejected without calling the API."""
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("garbage_value")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_failure_does_not_update_cache(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        """When async_cloud_set_pan returns False the cache must not be updated."""
        stub_coord.async_cloud_set_pan = AsyncMock(return_value=False)
        stub_coord._pan_cache[CAM_ID] = 0  # parked at home
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("right")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 60)
        # Cache must remain at 0 (unchanged) — no optimistic update on failure
        assert stub_coord._pan_cache[CAM_ID] == 0


# ── async_select_option — ceiling-mount angle inversion ──────────────────────


class TestSelectOptionCeilingMount:
    """For ceiling-mounted cameras the angle sent to the API must be sign-inverted."""

    @pytest.mark.asyncio
    async def test_right_inverted_to_minus60_on_ceiling(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord._image_rotation_180 = {CAM_ID: True}
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("right")  # type: ignore[attr-defined]
        # "right" = +60 user-visible → inverted → -60 physical → API must receive -60
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, -60)

    @pytest.mark.asyncio
    async def test_left_inverted_to_plus60_on_ceiling(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord._image_rotation_180 = {CAM_ID: True}
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("left")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 60)

    @pytest.mark.asyncio
    async def test_home_not_inverted_on_ceiling(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ) -> None:
        stub_coord._image_rotation_180 = {CAM_ID: True}
        sel = _make_sel(stub_coord, stub_entry)
        sel.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await sel.async_select_option("home")  # type: ignore[attr-defined]
        stub_coord.async_cloud_set_pan.assert_called_once_with(CAM_ID, 0)


# ── async_setup_entry integration ────────────────────────────────────────────


class TestSetupEntry:
    """BoschPanPresetSelect is created when panLimit > 0, skipped when panLimit == 0."""

    def test_created_when_pan_limit_positive(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        from custom_components.bosch_shc_camera.select import BoschPanPresetSelect, PAN_PRESET_OPTIONS
        sel = BoschPanPresetSelect(stub_coord, CAM_ID, stub_entry, pan_limit=120)
        assert isinstance(sel, BoschPanPresetSelect)
        assert sel._attr_options == PAN_PRESET_OPTIONS  # type: ignore[attr-defined]

    def test_pan_preset_options_count(self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace) -> None:
        from custom_components.bosch_shc_camera.select import PAN_PRESET_OPTIONS
        assert len(PAN_PRESET_OPTIONS) == 5

    def test_pan_preset_angles_all_in_range(self) -> None:
        """All mapped angles must be within the default ±120° pan range."""
        from custom_components.bosch_shc_camera.select import PAN_PRESET_ANGLES
        for name, angle in PAN_PRESET_ANGLES.items():
            assert -120 <= angle <= 120, f"Preset {name!r} angle {angle} out of ±120° range"
