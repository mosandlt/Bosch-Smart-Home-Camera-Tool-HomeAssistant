"""Tests for light.py — RGB LED + FrontLight entities (Round 5).

`test_light.py` covers basic property reads. This file goes after the
heavier units that the existing tests skipped:

  - `_BoschLightBase.async_added_to_hass` — RestoreState round-trip
    (last_rgb_color, last_brightness_pct, last_white_balance survive
    HA restarts so the card's color circle isn't grey on reboot).
  - `_BoschLightBase.extra_state_attributes` — last_* fields exposed
    even when the light is off (HA blanks rgb_color/brightness when
    state==off, but the card needs them).
  - `_BoschLightBase._load_state_from_cache` — sync from coordinator
    on every property access; remember last non-zero brightness +
    last color for restore-on-turn-on.
  - `_BoschLightBase._get_current_state` — default fallback when
    cache is empty.
  - `_BoschLightBase._put_lighting_switch` — always sends full body
    with all 3 light groups (Bosch API requirement).
  - `_BoschLightBase._put_switch_endpoint` — simple wrapper for /front
    + /topdown convenience endpoints.
  - `_BoschRgbLedLight.rgb_color` — hex → tuple conversion + warm-
    white default.
  - `_BoschRgbLedLight._sync_wallwasher_cache` — propagates light state
    to the wallwasher switch cache so the switch UI updates without
    waiting for the next coordinator poll.
  - `_BoschRgbLedLight.async_turn_on` — preconfigure-while-off + RGB
    body assembly + last-color restore.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _stub_coord(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:00:00:00:00:01",
                },
            },
        },
        _lighting_switch_cache={},
        _shc_state_cache={},
        _light_set_at={},
        last_update_success=True,
        token="fake-tok",
        async_update_listeners=MagicMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_light(coord=None, klass=None, led_key="topLedLightSettings"):
    """Bypass __init__ for the BoschLightBase subclasses so we don't need
    the HA framework's CoordinatorEntity setup chain."""
    if klass is None:
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        klass = BoschTopLedLight
    coord = coord or _stub_coord()
    light = klass.__new__(klass)
    light.coordinator = coord
    light._cam_id = CAM_ID
    light._entry = SimpleNamespace(data={}, options={})
    light._cam_title = "Terrasse"
    light._model = "X"
    light._model_name = "X"
    light._fw = ""
    light._mac = ""
    light._brightness = 0
    light._last_brightness = 100
    light._color_hex = None
    light._last_color_hex = None
    light._white_balance = None
    light._last_white_balance = -1.0
    light._is_on = False
    if led_key:
        light._led_key = led_key
    light.async_write_ha_state = MagicMock()
    light.hass = SimpleNamespace()
    return light


# ── extra_state_attributes ───────────────────────────────────────────────


class TestExtraStateAttributes:
    """The card reads `last_rgb_color` to render the color circle even
    when the light is off. HA blanks `rgb_color` in that state, so we
    expose `last_rgb_color` as an extra attr."""

    def test_warm_white_default_when_no_color(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        attrs = light.extra_state_attributes
        # Warm-white display default
        assert attrs["last_rgb_color"] == [255, 180, 100]

    def test_returns_decoded_color_when_set(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light._last_color_hex = "#FF8800"
        attrs = light.extra_state_attributes
        assert attrs["last_rgb_color"] == [255, 136, 0]

    def test_invalid_hex_skipped_silently(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light._last_color_hex = "#NOTAHEX"
        attrs = light.extra_state_attributes
        # Bad hex → key absent (not a crash)
        assert "last_rgb_color" not in attrs

    def test_includes_last_brightness_pct(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light._last_brightness = 75
        assert light.extra_state_attributes["last_brightness_pct"] == 75

    def test_omits_last_brightness_when_zero(self):
        """`if self._last_brightness:` gates the field — zero is excluded
        so the card doesn't restore to 0."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light._last_brightness = 0
        attrs = light.extra_state_attributes
        assert "last_brightness_pct" not in attrs

    def test_includes_last_white_balance_when_set(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light._last_white_balance = 0.42
        assert light.extra_state_attributes["last_white_balance"] == 0.42


# ── async_added_to_hass restore ─────────────────────────────────────────


class TestAsyncAddedToHassRestore:
    """RestoreState round-trip: read last_rgb_color, last_brightness_pct,
    last_white_balance from `last_state.attributes` so user choices
    survive HA restarts."""

    @pytest.mark.asyncio
    async def test_restores_color_and_brightness_and_wb(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        last_state = SimpleNamespace(attributes={
            "last_rgb_color": [255, 100, 50],
            "last_brightness_pct": 60,
            "last_white_balance": 0.3,
        })
        light.async_get_last_state = AsyncMock(return_value=last_state)
        with patch(
            "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await BoschTopLedLight.async_added_to_hass(light)
        assert light._last_color_hex == "#FF6432"
        assert light._last_brightness == 60
        assert light._last_white_balance == 0.3

    @pytest.mark.asyncio
    async def test_no_last_state_returns_silently(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light.async_get_last_state = AsyncMock(return_value=None)
        with patch(
            "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            # Must NOT raise
            await BoschTopLedLight.async_added_to_hass(light)
        assert light._last_color_hex is None

    @pytest.mark.asyncio
    async def test_invalid_color_tuple_swallowed(self):
        """Corrupt RestoreState (e.g. user mucked with .storage) must
        not crash entity setup."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        last_state = SimpleNamespace(attributes={
            "last_rgb_color": ["not", "ints", "here"],
        })
        light.async_get_last_state = AsyncMock(return_value=last_state)
        with patch(
            "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await BoschTopLedLight.async_added_to_hass(light)
        # Field stayed at default
        assert light._last_color_hex is None

    @pytest.mark.asyncio
    async def test_brightness_out_of_range_skipped(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        last_state = SimpleNamespace(attributes={
            "last_brightness_pct": 200,  # > 100
        })
        light.async_get_last_state = AsyncMock(return_value=last_state)
        with patch(
            "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
            new=AsyncMock(),
        ), patch(
            "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
            new=AsyncMock(),
        ):
            await BoschTopLedLight.async_added_to_hass(light)
        # _last_brightness stayed at default 100
        assert light._last_brightness == 100


# ── _load_state_from_cache ──────────────────────────────────────────────


class TestLoadStateFromCache:
    def test_off_when_brightness_zero(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={
            CAM_ID: {"topLedLightSettings": {"brightness": 0, "color": None}},
        })
        light = _make_light(coord)
        light._load_state_from_cache()
        assert light._is_on is False
        assert light._brightness == 0

    def test_on_when_brightness_positive(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={
            CAM_ID: {"topLedLightSettings": {"brightness": 75, "color": "#FF00FF"}},
        })
        light = _make_light(coord)
        light._load_state_from_cache()
        assert light._is_on is True
        assert light._brightness == 75
        assert light._color_hex == "#FF00FF"
        assert light._last_color_hex == "#FF00FF"
        # Color set → wb cleared
        assert light._white_balance is None

    def test_remembers_last_brightness(self):
        """Last non-zero brightness saved for restore-on-turn-on so the
        slider position survives an off cycle."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={
            CAM_ID: {"topLedLightSettings": {"brightness": 60, "color": None}},
        })
        light = _make_light(coord)
        light._load_state_from_cache()
        assert light._last_brightness == 60

    def test_white_balance_replaces_color(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={
            CAM_ID: {"topLedLightSettings": {
                "brightness": 50, "color": None, "whiteBalance": 0.6,
            }},
        })
        light = _make_light(coord)
        light._color_hex = "#stale"
        light._load_state_from_cache()
        assert light._white_balance == 0.6
        assert light._color_hex is None  # color cleared when wb wins

    def test_empty_cache_returns_silently(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()  # default cache empty
        # Must NOT raise; state untouched
        light._load_state_from_cache()
        assert light._is_on is False


# ── _get_current_state ──────────────────────────────────────────────────


class TestGetCurrentState:
    def test_returns_defaults_when_cache_empty(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        state = light._get_current_state()
        assert "frontLightSettings" in state
        assert "topLedLightSettings" in state
        assert "bottomLedLightSettings" in state
        # Defaults: brightness=0, color=None, whiteBalance=-1.0
        assert state["topLedLightSettings"]["brightness"] == 0
        assert state["topLedLightSettings"]["color"] is None

    def test_uses_cached_values_when_present(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={
            CAM_ID: {
                "topLedLightSettings": {"brightness": 40, "color": "#FF0000"},
                "frontLightSettings": {"brightness": 80, "color": None, "whiteBalance": 0.5},
            },
        })
        light = _make_light(coord)
        state = light._get_current_state()
        assert state["topLedLightSettings"]["brightness"] == 40
        assert state["topLedLightSettings"]["color"] == "#FF0000"
        assert state["frontLightSettings"]["whiteBalance"] == 0.5
        # Bottom not in cache → default
        assert state["bottomLedLightSettings"]["brightness"] == 0


# ── _put_lighting_switch ────────────────────────────────────────────────


class TestPutLightingSwitch:
    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(token="")
        light = _make_light(coord)
        ok = await BoschTopLedLight._put_lighting_switch(
            light, {"topLedLightSettings": {"brightness": 50}},
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_success_updates_cache(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        @asynccontextmanager
        async def _put_resp(*args, **kw):
            r = MagicMock()
            r.status = 200
            r.json = AsyncMock(return_value={"newCacheState": True})
            yield r

        session = MagicMock()
        session.put = _put_resp
        coord = _stub_coord()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light, {"topLedLightSettings": {"brightness": 50}},
            )
        assert ok is True
        # Cache replaced with response body
        assert coord._lighting_switch_cache[CAM_ID] == {"newCacheState": True}

    @pytest.mark.asyncio
    async def test_500_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        @asynccontextmanager
        async def _put_500(*args, **kw):
            r = MagicMock()
            r.status = 500
            r.text = AsyncMock(return_value="Internal")
            yield r

        session = MagicMock()
        session.put = _put_500
        coord = _stub_coord()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light, {"topLedLightSettings": {"brightness": 50}},
            )
        assert ok is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        session = MagicMock()
        session.put = MagicMock(side_effect=asyncio.TimeoutError())
        coord = _stub_coord()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light, {"topLedLightSettings": {"brightness": 50}},
            )
        assert ok is False

    @pytest.mark.asyncio
    async def test_body_includes_all_three_light_groups(self):
        """Bosch API requires all 3 groups in every PUT — pin so a
        refactor doesn't accidentally send a partial body that the
        camera would reject as 400."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        captured = {}

        @asynccontextmanager
        async def _put(*args, **kw):
            captured["json"] = kw.get("json", {})
            r = MagicMock()
            r.status = 204
            r.json = AsyncMock(return_value={})
            yield r

        session = MagicMock()
        session.put = _put
        coord = _stub_coord()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_clientsession",
            return_value=session,
        ):
            await BoschTopLedLight._put_lighting_switch(
                light, {"topLedLightSettings": {"brightness": 80}},
            )
        body = captured["json"]
        assert "frontLightSettings" in body
        assert "topLedLightSettings" in body
        assert "bottomLedLightSettings" in body
        # Only the requested key was modified
        assert body["topLedLightSettings"]["brightness"] == 80


# ── _put_switch_endpoint ────────────────────────────────────────────────


class TestPutSwitchEndpoint:
    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        @asynccontextmanager
        async def _put(*args, **kw):
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.put = _put
        coord = _stub_coord()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschTopLedLight._put_switch_endpoint(light, "front", True)
        assert ok is True

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(token="")
        light = _make_light(coord)
        ok = await BoschTopLedLight._put_switch_endpoint(light, "front", True)
        assert ok is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        session = MagicMock()
        session.put = MagicMock(side_effect=RuntimeError("network"))
        coord = _stub_coord()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschTopLedLight._put_switch_endpoint(light, "front", True)
        assert ok is False


# ── _BoschRgbLedLight._sync_wallwasher_cache ────────────────────────────


class TestSyncWallwasherCache:
    def test_top_or_bottom_on_marks_wallwasher_on(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={
            CAM_ID: {
                "topLedLightSettings": {"brightness": 50},
                "bottomLedLightSettings": {"brightness": 0},
                "frontLightSettings": {"brightness": 0},
            },
        })
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert coord._shc_state_cache[CAM_ID]["wallwasher"] is True

    def test_only_front_on_does_not_mark_wallwasher(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={
            CAM_ID: {
                "topLedLightSettings": {"brightness": 0},
                "bottomLedLightSettings": {"brightness": 0},
                "frontLightSettings": {"brightness": 80},
            },
        })
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert coord._shc_state_cache[CAM_ID]["wallwasher"] is False
        # camera_light is True — front light counts
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is True

    def test_all_off_marks_camera_light_off(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord(_lighting_switch_cache={CAM_ID: {
            "topLedLightSettings": {"brightness": 0},
            "bottomLedLightSettings": {"brightness": 0},
            "frontLightSettings": {"brightness": 0},
        }})
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert coord._shc_state_cache[CAM_ID]["camera_light"] is False

    def test_stamps_light_set_at_for_write_lock(self):
        """The 30s write-lock that prevents stale poll reverts depends on
        _light_set_at being stamped here. Pin so a refactor can't drop
        this and reintroduce the brightness-revert-after-toggle bug."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        coord = _stub_coord()
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert CAM_ID in coord._light_set_at
        assert coord._light_set_at[CAM_ID] > 0


# ── _BoschRgbLedLight.rgb_color ─────────────────────────────────────────


class TestRgbColor:
    def test_returns_tuple_when_color_set(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light._color_hex = "#10ABFF"
        rgb = light.rgb_color
        assert rgb == (0x10, 0xAB, 0xFF)

    def test_returns_warm_white_default_when_no_color(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        # Default warm white display value
        assert light.rgb_color == (255, 180, 100)

    def test_uses_last_color_when_current_is_none(self):
        """After turn_off the cache may have color=None, but the saved
        last_color_hex should still surface for the card."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = _make_light()
        light._color_hex = None
        light._last_color_hex = "#22DD44"
        assert light.rgb_color == (0x22, 0xDD, 0x44)
