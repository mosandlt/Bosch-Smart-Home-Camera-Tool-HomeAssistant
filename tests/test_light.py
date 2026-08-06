"""Tests for light.py — Gen2 RGB light entities (front, top LED, bottom LED).

Light entities are state-rich: they cache last_color, last_brightness,
last_white_balance to keep the card's color picker informed even when
the light is off (HA blanks `rgb_color` / `brightness` when state=off).

Coverage areas in this file:
  - Basic property reads (is_on, brightness, extra_state_attributes) for
    BoschTopLedLight / BoschBottomLedLight / BoschFrontLight.
  - `device_info` — identifiers/connections/model/sw_version contract.
  - `_put_lighting_switch` — defensive merge branch for unknown update
    keys, optimistic cache update when `resp.json()` raises on a 200/204
    response, happy-path success, HTTP-error and exception handling, and
    the requirement that every PUT body includes all 3 light groups.
  - `async_turn_on` / `async_turn_off` — remembered brightness/color
    restore, preconfigure-while-off behavior, RGB body assembly, and
    write-failure gating (a failed PUT must not flip `is_on`).
  - `extra_state_attributes` — last_* fields exposed even when the light
    is off (HA blanks rgb_color/brightness in that state, but the card
    needs them).
  - `async_added_to_hass` — RestoreState round-trip so last_rgb_color /
    last_brightness_pct / last_white_balance survive HA restarts.
  - `_load_state_from_cache` / `_get_current_state` — sync from the
    coordinator cache on property access, with sane defaults when empty.
  - `_sync_wallwasher_cache` — propagates light state to the wallwasher
    switch cache (and stamps the write-lock timestamp) so the switch UI
    updates without waiting for the next coordinator poll.
  - `rgb_color` — hex → tuple conversion + warm-white default.
  - Concurrent sibling writes (e.g. a scene toggling Top + Bottom LED at
    once) must not clobber each other's cache/PUT body.
  - `async_setup_entry` gating — which cameras/models get light entities.
  - `BoschFrontLight.color_temp_kelvin` — whiteBalance <-> Kelvin mapping.

Approach for the lower-level classes: bypass `__init__` via `klass.__new__()`
so tests don't need the HA framework's CoordinatorEntity setup chain. Async
calls run with `pytest-asyncio` (asyncio_mode=auto).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                }
            }
        },
        lighting_switch_cache={
            CAM_ID: {
                "frontLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "topLedLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "bottomLedLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
            }
        },
        last_update_success=True,
        token="tok",
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


class TestTopLedLight:
    def test_construction(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        assert light._led_key == "topLedLightSettings"

    def test_off_when_brightness_zero(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        # Cache has brightness=0 → is_on=False
        assert light.is_on is False

    def test_on_when_brightness_positive(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        stub_coord.lighting_switch_cache[CAM_ID]["topLedLightSettings"][
            "brightness"
        ] = 75
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        assert light.is_on is True

    def test_brightness_scales_to_255(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """API uses 0-100, HA uses 0-255 — values must be scaled."""
        stub_coord.lighting_switch_cache[CAM_ID]["topLedLightSettings"][
            "brightness"
        ] = 50
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        # 50% → 127/128 in HA's 255-scale
        bri = light.brightness
        assert bri is not None
        assert 100 <= bri <= 150  # ~127

    def test_extra_attrs_warm_white_default_when_no_color(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """When user never picked a color, card sees a warm-white default
        so the color dot isn't grey on first load."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        attrs = light.extra_state_attributes
        assert "last_rgb_color" in attrs
        # Warm-white-ish — high red, mid green, low blue
        r, g, b = attrs["last_rgb_color"]
        assert r > g > b

    def test_extra_attrs_preserves_user_color(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """If the user picked a color, that's what appears in last_rgb_color."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        light._last_color_hex = "#FF0080"
        attrs = light.extra_state_attributes
        assert attrs["last_rgb_color"] == [255, 0, 128]

    def test_extra_attrs_invalid_hex_does_not_raise(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Garbled cached color must not crash extra_state_attributes.

        Implementation choice: invalid hex falls through silently (no
        last_rgb_color attribute), rather than substituting a default.
        Either way is acceptable as long as the property doesn't raise.
        """
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        light._last_color_hex = "#ZZZZZZ"  # invalid hex
        # Must not raise. Specific contents are an implementation detail.
        attrs = light.extra_state_attributes
        assert isinstance(attrs, dict)

    def test_available_follows_coordinator(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        assert light.available is True
        stub_coord.last_update_success = False
        assert light.available is False


class TestBottomLedLight:
    def test_uses_bottom_led_key(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschBottomLedLight

        light = BoschBottomLedLight(stub_coord, CAM_ID, stub_entry)
        assert light._led_key == "bottomLedLightSettings"


class TestFrontLight:
    def test_uses_front_led_key(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        light = BoschFrontLight(stub_coord, CAM_ID, stub_entry)
        assert light._led_key == "frontLightSettings"


def _stub_coord_edge(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
            },
        },
        lighting_switch_cache={},
        shc_state_cache={},
        # SENTINEL_RULE: float('-inf') so monotonic comparisons always trigger
        light_set_at={},
        last_update_success=True,
        token="fake-tok",
        async_update_listeners=MagicMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_light(coord=None, klass=None, led_key="topLedLightSettings"):
    """Bypass __init__ so we don't need the HA framework's CoordinatorEntity setup chain."""
    if klass is None:
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        klass = BoschTopLedLight
    coord = coord or _stub_coord_edge()
    light = klass.__new__(klass)
    light.coordinator = coord
    light._cam_id = CAM_ID
    light._entry = SimpleNamespace(data={}, options={})
    light._cam_title = "Terrasse"
    light._model = "HOME_Eyes_Outdoor"
    light._model_name = "Eyes Outdoor"
    light._fw = "9.40.25"
    light._mac = "aa:bb:cc:dd:ee:01"
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


def _make_put_session(
    status: int = 200, json_payload=None, json_raises: Exception | None = None
):
    """Return an async-context-manager session.put() stub returning the given
    status; json() either returns json_payload or raises json_raises."""
    resp = MagicMock()
    resp.status = status

    if json_raises is not None:
        resp.json = AsyncMock(side_effect=json_raises)
    else:
        resp.json = AsyncMock(return_value=json_payload or {})

    @asynccontextmanager
    async def _resp_cm(*args, **kwargs):
        yield resp

    session = MagicMock()
    session.put = MagicMock(side_effect=_resp_cm)
    return session, resp


class TestDeviceInfoReturn:
    """`device_info` must return a dict containing identifiers + connections
    derived from cam_id + mac. Cheap smoke to pin the contract."""

    def test_device_info_returns_dict_with_identifiers_and_mac(self):
        light = _make_light()
        info = light.device_info
        assert isinstance(info, dict)
        assert ("bosch_shc_camera", CAM_ID) in info["identifiers"] or any(
            CAM_ID in str(i) for i in info["identifiers"]
        )
        # MAC populated → connections set
        assert info["connections"]
        assert info["model"] == "Eyes Outdoor"
        assert info["sw_version"] == "9.40.25"

    def test_device_info_empty_connections_when_no_mac(self):
        """No MAC → connections is an empty set (not None) so HA's device
        registry treats it as 'no connections', not 'unknown connections'."""
        light = _make_light()
        light._mac = ""
        info = light.device_info
        assert info["connections"] == set()


class TestPutLightingSwitchDefensiveMerge:
    """`_put_lighting_switch` merges `updates` onto the cached body. The else
    branch (`body[key] = val`) covers the defensive case where an unknown
    update key is passed in — must not crash, must include the new key."""

    @pytest.mark.asyncio
    async def test_unknown_update_key_added_to_body(self):
        """If updates contains a key not in the 3-LED default body (e.g. a
        new lighting group added by future firmware), it is assigned wholesale
        instead of dict-merged."""
        light = _make_light()
        session, resp = _make_put_session(status=200, json_payload={"ok": True})

        captured_body = {}

        def _capture(url, headers=None, json=None):
            captured_body.update(json or {})

            @asynccontextmanager
            async def _cm():
                yield resp

            return _cm()

        session.put = MagicMock(side_effect=_capture)

        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            # `someFutureGroup` is NOT in the 3-LED default → triggers else branch
            updates = {"someFutureGroup": {"brightness": 50, "color": None}}
            ok = await light._put_lighting_switch(updates)

        assert ok is True
        # The wholesale assignment branch executed: future key landed in body
        assert "someFutureGroup" in captured_body
        assert captured_body["someFutureGroup"] == {"brightness": 50, "color": None}


class TestPutLightingSwitchJsonParseError:
    """If the Bosch cloud returns 200 but with a malformed body (HTML error
    page, gzip-stripped, etc.), `resp.json()` raises. The except branch now
    falls back to updating the cache from the sent body so is_on reads True
    after a successful write even when the server returns 204 No Content or
    an unparseable body."""

    @pytest.mark.asyncio
    async def test_json_parse_error_returns_true_with_optimistic_cache_update(self):
        light = _make_light()
        session, _resp = _make_put_session(
            status=200,
            json_raises=ValueError("not JSON"),
        )

        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await light._put_lighting_switch(
                {"topLedLightSettings": {"brightness": 50}}
            )

        assert ok is True, (
            "200 status should still count as success even if body unparseable"
        )
        # Cache IS updated from the sent body (optimistic update), NOT left
        # untouched. This ensures is_on reads True after a 204 No Content response.
        cache = light.coordinator.lighting_switch_cache[CAM_ID]
        assert "topLedLightSettings" in cache, (
            "Cache must be updated from sent body when resp.json() raises (fallback path)"
        )
        assert cache["topLedLightSettings"]["brightness"] == 50, (
            "Optimistic update must apply the written brightness value"
        )


class TestAsyncTurnOnRemembersBrightness:
    """Calling `async_turn_on(brightness=N)` must store the converted value in
    `_last_brightness` so a later turn_on without brightness restores it.
    `max(1, round(brightness * 100 / 255))` guarantees sentinel brightness=1
    doesn't collapse to 0%."""

    @pytest.mark.asyncio
    async def test_brightness_kwarg_updates_last_brightness(self):
        from homeassistant.components.light import ATTR_BRIGHTNESS

        light = _make_light()
        light._is_on = False  # was_off branch
        light._last_brightness = 100  # default
        session, _resp = _make_put_session(status=200, json_payload={})

        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            # brightness=255 → round(255*100/255)=100 → _last_brightness=max(1,100)=100
            await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})

        # was_off=True + brightness kwarg → preconfigure-while-off branch returns
        # but the brightness bookkeeping still ran before the early return.
        assert light._last_brightness == 100

    @pytest.mark.asyncio
    async def test_brightness_sentinel_rounded_up_to_one(self):
        """brightness=1 must clamp to _last_brightness=1, not 0% (would
        suppress the 'remembered brightness' for the next turn_on)."""
        from homeassistant.components.light import ATTR_BRIGHTNESS

        light = _make_light()
        light._is_on = False
        light._last_brightness = 50
        session, _resp = _make_put_session(status=200, json_payload={})

        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await light.async_turn_on(**{ATTR_BRIGHTNESS: 1})

        # round(1*100/255)=0 → max(1,0)=1 (sentinel rule keeps it visible)
        assert light._last_brightness == 1


class TestAsyncTurnOffRemembersColor:
    """Before zeroing brightness, the off-handler must persist the current
    `_color_hex` into `_last_color_hex` so the next turn_on restores the
    same color rather than reverting to warm-white default."""

    @pytest.mark.asyncio
    async def test_color_hex_persisted_to_last_color_hex_on_turn_off(self):
        light = _make_light()
        light._is_on = True
        light._brightness = 80
        light._color_hex = "#FF8000"  # user-picked orange
        light._last_color_hex = None  # was never set before

        session, _resp = _make_put_session(status=200, json_payload={})

        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await light.async_turn_off()

        # Current color preserved
        assert light._last_color_hex == "#FF8000"
        assert light._is_on is False
        assert light._brightness == 0


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


class TestAsyncAddedToHassRestore:
    """RestoreState round-trip: read last_rgb_color, last_brightness_pct,
    last_white_balance from `last_state.attributes` so user choices
    survive HA restarts."""

    @pytest.mark.asyncio
    async def test_restores_color_and_brightness_and_wb(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = _make_light()
        last_state = SimpleNamespace(
            attributes={
                "last_rgb_color": [255, 100, 50],
                "last_brightness_pct": 60,
                "last_white_balance": 0.3,
            }
        )
        light.async_get_last_state = AsyncMock(return_value=last_state)
        with (
            patch(
                "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
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
        with (
            patch(
                "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
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
        last_state = SimpleNamespace(
            attributes={
                "last_rgb_color": ["not", "ints", "here"],
            }
        )
        light.async_get_last_state = AsyncMock(return_value=last_state)
        with (
            patch(
                "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
        ):
            await BoschTopLedLight.async_added_to_hass(light)
        # Field stayed at default
        assert light._last_color_hex is None

    @pytest.mark.asyncio
    async def test_brightness_out_of_range_skipped(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        light = _make_light()
        last_state = SimpleNamespace(
            attributes={
                "last_brightness_pct": 200,  # > 100
            }
        )
        light.async_get_last_state = AsyncMock(return_value=last_state)
        with (
            patch(
                "custom_components.bosch_shc_camera.light.CoordinatorEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.LightEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.light.RestoreEntity.async_added_to_hass",
                new=AsyncMock(),
            ),
        ):
            await BoschTopLedLight.async_added_to_hass(light)
        # _last_brightness stayed at default 100
        assert light._last_brightness == 100


class TestLoadStateFromCache:
    def test_off_when_brightness_zero(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {"topLedLightSettings": {"brightness": 0, "color": None}},
            }
        )
        light = _make_light(coord)
        light._load_state_from_cache()
        assert light._is_on is False
        assert light._brightness == 0

    def test_on_when_brightness_positive(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {"topLedLightSettings": {"brightness": 75, "color": "#FF00FF"}},
            }
        )
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

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {"topLedLightSettings": {"brightness": 60, "color": None}},
            }
        )
        light = _make_light(coord)
        light._load_state_from_cache()
        assert light._last_brightness == 60

    def test_white_balance_replaces_color(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "topLedLightSettings": {
                        "brightness": 50,
                        "color": None,
                        "whiteBalance": 0.6,
                    }
                },
            }
        )
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

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "topLedLightSettings": {"brightness": 40, "color": "#FF0000"},
                    "frontLightSettings": {
                        "brightness": 80,
                        "color": None,
                        "whiteBalance": 0.5,
                    },
                },
            }
        )
        light = _make_light(coord)
        state = light._get_current_state()
        assert state["topLedLightSettings"]["brightness"] == 40
        assert state["topLedLightSettings"]["color"] == "#FF0000"
        assert state["frontLightSettings"]["whiteBalance"] == 0.5
        # Bottom not in cache → default
        assert state["bottomLedLightSettings"]["brightness"] == 0


class TestPutLightingSwitch:
    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(token="")
        light = _make_light(coord)
        ok = await BoschTopLedLight._put_lighting_switch(
            light,
            {"topLedLightSettings": {"brightness": 50}},
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_success_updates_cache(self):
        """200 with a full-group body → cache adopts the server's value for the
        CHANGED group only (merge-only-changed-key — no longer a wholesale
        replace of the entry)."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        server_response = {
            "frontLightSettings": {
                "brightness": 0,
                "color": None,
                "whiteBalance": -1.0,
            },
            "topLedLightSettings": {
                "brightness": 50,
                "color": None,
                "whiteBalance": -1.0,
            },
            "bottomLedLightSettings": {
                "brightness": 0,
                "color": None,
                "whiteBalance": -1.0,
            },
        }

        @asynccontextmanager
        async def _put_resp(*args, **kw):
            r = MagicMock()
            r.status = 200
            r.json = AsyncMock(return_value=server_response)
            yield r

        session = MagicMock()
        session.put = _put_resp
        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "frontLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "topLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "bottomLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                }
            }
        )
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light,
                {"topLedLightSettings": {"brightness": 50}},
            )
        assert ok is True
        # Only the changed group is taken from the response; siblings preserved.
        assert (
            coord.lighting_switch_cache[CAM_ID]["topLedLightSettings"]["brightness"]
            == 50
        )
        assert "frontLightSettings" in coord.lighting_switch_cache[CAM_ID]
        assert "bottomLedLightSettings" in coord.lighting_switch_cache[CAM_ID]

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
        coord = _stub_coord_edge()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light,
                {"topLedLightSettings": {"brightness": 50}},
            )
        assert ok is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        session = MagicMock()
        session.put = MagicMock(side_effect=TimeoutError())
        coord = _stub_coord_edge()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light,
                {"topLedLightSettings": {"brightness": 50}},
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
        coord = _stub_coord_edge()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await BoschTopLedLight._put_lighting_switch(
                light,
                {"topLedLightSettings": {"brightness": 80}},
            )
        body = captured["json"]
        assert "frontLightSettings" in body
        assert "topLedLightSettings" in body
        assert "bottomLedLightSettings" in body
        # Only the requested key was modified
        assert body["topLedLightSettings"]["brightness"] == 80


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
        coord = _stub_coord_edge()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_switch_endpoint(light, "front", True)
        assert ok is True

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(token="")
        light = _make_light(coord)
        ok = await BoschTopLedLight._put_switch_endpoint(light, "front", True)
        assert ok is False

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        session = MagicMock()
        session.put = MagicMock(side_effect=RuntimeError("network"))
        coord = _stub_coord_edge()
        light = _make_light(coord)
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_switch_endpoint(light, "front", True)
        assert ok is False


class TestSyncWallwasherCache:
    def test_top_or_bottom_on_marks_wallwasher_on(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "topLedLightSettings": {"brightness": 50},
                    "bottomLedLightSettings": {"brightness": 0},
                    "frontLightSettings": {"brightness": 0},
                },
            }
        )
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert coord.shc_state_cache[CAM_ID]["wallwasher"] is True

    def test_only_front_on_does_not_mark_wallwasher(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "topLedLightSettings": {"brightness": 0},
                    "bottomLedLightSettings": {"brightness": 0},
                    "frontLightSettings": {"brightness": 80},
                },
            }
        )
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert coord.shc_state_cache[CAM_ID]["wallwasher"] is False
        # camera_light is True — front light counts
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is True

    def test_all_off_marks_camera_light_off(self):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "topLedLightSettings": {"brightness": 0},
                    "bottomLedLightSettings": {"brightness": 0},
                    "frontLightSettings": {"brightness": 0},
                }
            }
        )
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert coord.shc_state_cache[CAM_ID]["camera_light"] is False

    def test_stamps_light_set_at_for_write_lock(self):
        """The 30s write-lock that prevents stale poll reverts depends on
        light_set_at being stamped here. Pin so a refactor can't drop
        this and reintroduce the brightness-revert-after-toggle bug."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        coord = _stub_coord_edge()
        light = _make_light(coord)
        light._sync_wallwasher_cache()
        assert CAM_ID in coord.light_set_at
        assert coord.light_set_at[CAM_ID] > 0


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


# 204 No Content: optimistic cache update regression
#
# Root cause: /lighting/switch returns 204 No Content (empty body).
# Old code: resp.json() raised (no JSON body) → except swallowed silently
# → lighting_switch_cache[cam_id] never updated
# → _load_state_from_cache() read brightness=0 → is_on stayed False
# → HA warned "state change could not be verified".
#
# Fix: on json() failure, fall back to updating cache from the sent body.


class TestPutLightingSwitch204NoCacheUpdate:
    """Regression: PUT returns 204 No Content → cache must still be updated
    from the sent body so is_on reads True after turn_on succeeds."""

    @pytest.mark.asyncio
    async def test_204_no_content_updates_cache_from_body(self):
        """204 with empty response → cache updated from the PUT body, not empty."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        @asynccontextmanager
        async def _put_204(*args, **kw):
            r = MagicMock()
            r.status = 204
            # Simulate 204 No Content: resp.json() raises because there is no body
            r.json = AsyncMock(side_effect=Exception("No JSON body"))
            yield r

        session = MagicMock()
        session.put = _put_204
        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "frontLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "topLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "bottomLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                }
            }
        )
        light = _make_light(coord, led_key="frontLightSettings")
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light,
                {
                    "frontLightSettings": {
                        "brightness": 100,
                        "color": None,
                        "whiteBalance": -1.0,
                    }
                },
            )
        assert ok is True
        # Cache must be updated from the body we sent — brightness 100, NOT 0
        assert (
            coord.lighting_switch_cache[CAM_ID]["frontLightSettings"]["brightness"]
            == 100
        )

    @pytest.mark.asyncio
    async def test_204_no_content_is_on_reads_true_after_turn_on(self):
        """End-to-end: after a 204 response, is_on reflects the written state
        (not the stale cache), so HA stops warning about unverifiable state."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        @asynccontextmanager
        async def _put_204(*args, **kw):
            r = MagicMock()
            r.status = 204
            r.json = AsyncMock(side_effect=Exception("No JSON body"))
            yield r

        session = MagicMock()
        session.put = _put_204
        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "frontLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "topLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "bottomLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                }
            }
        )
        light = _make_light(coord, led_key="frontLightSettings")

        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light,
                {
                    "frontLightSettings": {
                        "brightness": 100,
                        "color": None,
                        "whiteBalance": -1.0,
                    }
                },
            )
        assert ok is True
        # After the PUT, is_on must read True (not False)
        assert light.is_on is True

    @pytest.mark.asyncio
    async def test_200_with_valid_json_still_uses_response_body(self):
        """200 with valid JSON body → cache updated from response (not sent body).
        Ensures the fallback doesn't regress the happy-path."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        server_response = {
            "frontLightSettings": {
                "brightness": 100,
                "color": None,
                "whiteBalance": -1.0,
            },
            "topLedLightSettings": {
                "brightness": 0,
                "color": None,
                "whiteBalance": -1.0,
            },
            "bottomLedLightSettings": {
                "brightness": 0,
                "color": None,
                "whiteBalance": -1.0,
            },
        }

        @asynccontextmanager
        async def _put_200(*args, **kw):
            r = MagicMock()
            r.status = 200
            r.json = AsyncMock(return_value=server_response)
            yield r

        session = MagicMock()
        session.put = _put_200
        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "frontLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "topLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "bottomLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                }
            }
        )
        light = _make_light(coord, led_key="frontLightSettings")
        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await BoschTopLedLight._put_lighting_switch(
                light,
                {
                    "frontLightSettings": {
                        "brightness": 100,
                        "color": None,
                        "whiteBalance": -1.0,
                    }
                },
            )
        assert ok is True
        # The changed group adopts the server's authoritative value, but the
        # whole cache entry is NOT wholesale-replaced by the response object —
        # only the changed group is merged in, so a concurrently-written sibling
        # group isn't clobbered.
        assert (
            coord.lighting_switch_cache[CAM_ID]["frontLightSettings"]
            == server_response["frontLightSettings"]
        )
        assert coord.lighting_switch_cache[CAM_ID] is not server_response


class TestPutLightingSwitchConcurrentNoClobber:
    """/lighting/switch requires the full 3-group body, so two concurrent
    sibling writes (a scene toggling Top + Bottom LED) that each build their
    body from a pre-write snapshot used to re-send the OTHER group's stale
    value — reverting it in cache AND on the camera. The per-camera lock +
    merge-only-changed-key must make the later write read the sibling's
    committed result and preserve both changes."""

    @pytest.mark.asyncio
    async def test_concurrent_sibling_writes_do_not_clobber(self):
        from custom_components.bosch_shc_camera.light import (
            BoschBottomLedLight,
            BoschTopLedLight,
        )

        put_bodies: list[dict] = []

        @asynccontextmanager
        async def _slow_put(url, *args, **kw):
            # Yield so the sibling task starts (and blocks on the lock) before we
            # commit — this is exactly the interleaving that used to clobber.
            await asyncio.sleep(0)
            put_bodies.append(dict(kw.get("json", {})))
            r = MagicMock()
            r.status = 204
            r.json = AsyncMock(side_effect=Exception("No JSON body"))
            yield r

        session = MagicMock()
        session.put = _slow_put
        coord = _stub_coord_edge(
            lighting_switch_cache={
                CAM_ID: {
                    "frontLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "topLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                    "bottomLedLightSettings": {
                        "brightness": 0,
                        "color": None,
                        "whiteBalance": -1.0,
                    },
                }
            }
        )
        top = _make_light(coord, klass=BoschTopLedLight, led_key="topLedLightSettings")
        bottom = _make_light(
            coord, klass=BoschBottomLedLight, led_key="bottomLedLightSettings"
        )

        with patch(
            "custom_components.bosch_shc_camera.light.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await asyncio.gather(
                top._put_lighting_switch({"topLedLightSettings": {"brightness": 50}}),
                bottom._put_lighting_switch(
                    {"bottomLedLightSettings": {"brightness": 70}}
                ),
            )

        cache = coord.lighting_switch_cache[CAM_ID]
        # Neither write reverted the other in the cache.
        assert cache["topLedLightSettings"]["brightness"] == 50
        assert cache["bottomLedLightSettings"]["brightness"] == 70
        # The second (serialized) PUT body carried the first write's committed
        # value, so it did not revert the sibling on the camera either.
        assert put_bodies[-1]["topLedLightSettings"]["brightness"] == 50
        assert put_bodies[-1]["bottomLedLightSettings"]["brightness"] == 70


def _stub_coord_setup(**overrides):
    base = dict(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True},
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        lighting_switch_cache={},
        shc_state_cache={CAM_ID: {}},
        light_set_at={},
        last_update_success=True,
        token="tok-A",
        async_update_listeners=MagicMock(),
        async_add_listener=MagicMock(return_value=MagicMock()),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_coord_setup() -> SimpleNamespace:
    return _stub_coord_setup()


# NOTE: `stub_entry` fixture defined earlier in this file (identical body:
# SimpleNamespace(entry_id="01ENTRY", data={}, options={})) is reused here.


class TestLightAsyncSetupEntry:
    def test_entities_added_for_gen2_with_light(self):
        """Three light entities (top/bottom/front) must be added for Gen2 Outdoor with light."""
        from custom_components.bosch_shc_camera.light import async_setup_entry

        coord = _stub_coord_setup()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = True
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=MagicMock()
        )
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert "BoschTopLedLight" in entity_classes, (
            "BoschTopLedLight must be added for Gen2 Outdoor"
        )
        assert "BoschBottomLedLight" in entity_classes, (
            "BoschBottomLedLight must be added for Gen2 Outdoor"
        )
        assert "BoschFrontLight" in entity_classes, (
            "BoschFrontLight must be added for Gen2 Outdoor"
        )

    def test_no_entities_for_gen1_cameras(self):
        """No light entities for Gen1 (non-Gen2) cameras — light.py is Gen2-only."""
        from custom_components.bosch_shc_camera.light import async_setup_entry

        coord = _stub_coord_setup()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_360"  # Gen1
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=MagicMock()
        )
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert added == [], "No light entities must be registered for Gen1 camera"

    def test_no_entities_when_has_light_false(self):
        """No light entities for Gen2 Outdoor without featureSupport.light."""
        from custom_components.bosch_shc_camera.light import async_setup_entry

        coord = _stub_coord_setup()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=MagicMock()
        )
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert added == [], "No light entities when featureSupport.light=False"

    def test_indoor_ii_gets_no_light_entities(self):
        """Eyes Indoor II has NO controllable light hardware (only fixed IR
        night-vision LEDs that the firmware manages on its own): Indoor II
        + cloud `featureSupport.light=false` must yield zero light
        entities (confirmed by the camera owner)."""
        from custom_components.bosch_shc_camera.light import async_setup_entry

        coord = _stub_coord_setup()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Indoor"
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=MagicMock()
        )
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        entity_classes = [type(e).__name__ for e in added]
        assert entity_classes == [], (
            f"REGRESSION: Indoor II got light entities even though the "
            f"camera has no controllable light hardware. "
            f"Got {entity_classes}, expected []."
        )

    def test_indoor_ii_alias_also_gets_no_light(self):
        """`CAMERA_INDOOR_GEN2` is the legacy alias for HOME_Eyes_Indoor —
        cover both hw strings."""
        from custom_components.bosch_shc_camera.light import async_setup_entry

        coord = _stub_coord_setup()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "CAMERA_INDOOR_GEN2"
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = False
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=MagicMock()
        )
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert [type(e).__name__ for e in added] == []

    def test_hw_version_fallback_from_persistent_cache(self):
        """When `cam_info.hardwareVersion` is empty (cold-start during cloud
        outage, rehydrated coordinator.data has just `info.title`), light
        setup falls back to the persistent `hw_version` store. Same fallback
        used by the rest of the integration to determine model behaviour.
        Outdoor II must still get its three light entities on cold start."""
        from custom_components.bosch_shc_camera.light import async_setup_entry

        coord = _stub_coord_setup()
        # Simulate cold-start rehydrate: info has title only, no hardwareVersion
        coord.data[CAM_ID]["info"].pop("hardwareVersion", None)
        coord.data[CAM_ID]["info"]["featureSupport"] = {"light": True}
        # Persistent store knows it's Outdoor II
        coord.hw_version = {CAM_ID: "HOME_Eyes_Outdoor"}
        added = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=MagicMock()
        )
        asyncio.run(async_setup_entry(None, entry, _fake_add))
        assert set(type(e).__name__ for e in added) == {
            "BoschTopLedLight",
            "BoschBottomLedLight",
            "BoschFrontLight",
        }, (
            "REGRESSION: hw_version fallback from persistent store no longer "
            "feeds the light entity setup. Outdoor II loses its light entities "
            "during cloud-degraded cold starts."
        )

    def test_new_camera_gets_entities_added_dynamically(self):
        """Quality-Scale Gold `dynamic-devices`: a camera added to
        `coordinator.data` AFTER the initial `async_setup_entry` pass must
        get its light entities added automatically via the registered
        coordinator-update listener, with no duplicate/no-op re-adds on a
        tick that introduces no new cameras."""
        from custom_components.bosch_shc_camera.light import async_setup_entry

        coord = _stub_coord_setup()
        coord.data[CAM_ID]["info"]["hardwareVersion"] = "HOME_Eyes_Outdoor"
        coord.data[CAM_ID]["info"]["featureSupport"]["light"] = True
        added: list[object] = []

        def _fake_add(entities, **kw):
            added.extend(entities)

        import asyncio

        entry = SimpleNamespace(
            runtime_data=coord, options={}, async_on_unload=MagicMock()
        )
        asyncio.run(async_setup_entry(None, entry, _fake_add))

        coord.async_add_listener.assert_called_once()
        entry.async_on_unload.assert_called_once()
        assert len(added) == 3  # initial camera's 3 light entities

        listener = coord.async_add_listener.call_args[0][0]

        new_cam_id = "new-cam-002"
        coord.data[new_cam_id] = {
            "info": {
                "title": "Neue Kamera",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.25",
                "macAddress": "aa:bb:cc:dd:ee:02",
                "featureSupport": {"light": True},
            },
            "status": "ONLINE",
            "events": [],
        }

        listener()

        new_entity_classes = {type(e).__name__ for e in added[3:]}
        assert new_entity_classes == {
            "BoschTopLedLight",
            "BoschBottomLedLight",
            "BoschFrontLight",
        }, (
            f"New camera's light entities not added dynamically. Got {new_entity_classes}."
        )
        assert len(added) == 6

        # A second tick with no new cameras must be a no-op (no duplicates).
        listener()
        assert len(added) == 6, (
            "Listener must not re-add entities for already-known cameras"
        )


class TestFrontLightColorTempKelvin:
    def _make_front_light(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(coord, CAM_ID, entry)
        return entity

    def test_cool_white_balance_gives_high_kelvin(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """whiteBalance=-1.0 (coolest) must map to 6500K."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._white_balance = -1.0
        entity._is_on = True
        entity._brightness = 80
        # Bypass _load_state_from_cache by clearing cache
        stub_coord_setup.lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k == 6500, "whiteBalance=-1.0 must map to 6500K (cool)"

    def test_warm_white_balance_gives_low_kelvin(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """whiteBalance=1.0 (warmest) must map to 2000K."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._white_balance = 1.0
        entity._is_on = True
        entity._brightness = 80
        stub_coord_setup.lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k == 2000, "whiteBalance=1.0 must map to 2000K (warm)"

    def test_neutral_white_balance_gives_mid_kelvin(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """whiteBalance=0.0 must map to 4250K (midpoint)."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._white_balance = 0.0
        entity._is_on = True
        entity._brightness = 80
        stub_coord_setup.lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k == 4250, "whiteBalance=0.0 must map to 4250K (neutral)"

    def test_returns_value_when_off_for_ui_slider(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Must return a non-None Kelvin value even when light is off (UI slider position)."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = False
        entity._white_balance = None
        entity._last_white_balance = -1.0
        stub_coord_setup.lighting_switch_cache = {}
        k = entity.color_temp_kelvin
        assert k is not None, "color_temp_kelvin must return a value even when off"


class TestFrontLightTurnOn:
    def _make_front_light(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        return entity

    @pytest.mark.asyncio
    async def test_turn_on_without_kwargs_uses_last_brightness(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Turn ON with no kwargs must use remembered brightness (not 0)."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = False
        entity._last_brightness = 75
        entity._white_balance = -0.5
        await entity.async_turn_on()
        call_args = entity._put_lighting_switch.call_args[0][0]
        assert call_args["frontLightSettings"]["brightness"] == 75, (
            "Turn ON must restore last brightness (75) when no explicit brightness given"
        )

    @pytest.mark.asyncio
    async def test_turn_on_with_color_temp_stores_wb(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Turn ON with ATTR_COLOR_TEMP_KELVIN must convert to whiteBalance and store it."""
        from homeassistant.components.light import ATTR_COLOR_TEMP_KELVIN

        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on(**{ATTR_COLOR_TEMP_KELVIN: 6500})
        # 6500K → wb = (4250-6500)/2250 = -1.0
        assert entity._white_balance == -1.0, "6500K must map to whiteBalance=-1.0"
        assert entity._last_white_balance == -1.0, (
            "last_white_balance must also be updated"
        )

    @pytest.mark.asyncio
    async def test_turn_on_while_off_with_brightness_only_preconfigures(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """When light is off and only brightness is given: store locally, don't PUT API."""
        from homeassistant.components.light import ATTR_BRIGHTNESS

        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = False
        await entity.async_turn_on(**{ATTR_BRIGHTNESS: 128})
        entity._put_lighting_switch.assert_not_called()
        assert entity._last_brightness == 50, (
            "128/255*100 = 50% must be stored as last_brightness in preconfigure mode"
        )

    @pytest.mark.asyncio
    async def test_turn_on_sends_put_and_enables_front_switch(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Turn ON from on-state must PUT lighting/switch and enable front switch endpoint."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._last_brightness = 60
        await entity.async_turn_on()
        entity._put_lighting_switch.assert_called_once()
        entity._put_switch_endpoint.assert_called_once_with("front", True)

    @pytest.mark.asyncio
    async def test_turn_on_sets_is_on_true(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Entity must be is_on=True after turn_on."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on()
        assert entity._is_on is True, "Entity must be on after turn_on"


class TestFrontLightTurnOff:
    def _make_front_light(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        return entity

    @pytest.mark.asyncio
    async def test_turn_off_sets_is_on_false(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._brightness = 80
        await entity.async_turn_off()
        assert entity._is_on is False, "Entity must be off after turn_off"
        assert entity._brightness == 0, "Brightness must be 0 after turn_off"

    @pytest.mark.asyncio
    async def test_turn_off_sends_brightness_zero_and_disables_endpoint(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Turn OFF must PUT brightness=0 AND disable the front switch endpoint."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._brightness = 80
        entity._white_balance = -0.5
        await entity.async_turn_off()
        put_call = entity._put_lighting_switch.call_args[0][0]
        assert put_call["frontLightSettings"]["brightness"] == 0, (
            "Must send brightness=0 to keep cache consistent with camera state"
        )
        entity._put_switch_endpoint.assert_called_once_with("front", False)

    @pytest.mark.asyncio
    async def test_turn_off_preserves_white_balance_in_put(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Turn OFF must preserve whiteBalance in PUT body so subsequent top/bottom PUTs don't accidentally re-enable front."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._brightness = 80
        entity._white_balance = 0.5
        await entity.async_turn_off()
        put_call = entity._put_lighting_switch.call_args[0][0]
        assert put_call["frontLightSettings"]["whiteBalance"] == 0.5, (
            "whiteBalance must be preserved in turn_off PUT (prevents accidental re-enable)"
        )


class TestFrontLightSyncsCameraLightCache:
    """GitHub #66 regression: BoschFrontLight must sync shc_state_cache like
    _BoschRgbLedLight already does, else switch.bosch_kamera_*_kameralicht
    (which reads shc_state_cache, not lighting_switch_cache) stays stale
    after a Front-Light-only toggle until the next coordinator poll.
    """

    def _make_front_light(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        return entity

    @pytest.mark.asyncio
    async def test_turn_on_stamps_light_set_at_write_lock(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = False
        entity._last_brightness = 80
        assert CAM_ID not in stub_coord_setup.light_set_at
        await entity.async_turn_on()
        assert CAM_ID in stub_coord_setup.light_set_at, (
            "turn_on must stamp light_set_at so a stale SHC/cloud poll can't "
            "immediately overwrite the fresh optimistic state (GitHub #66)"
        )

    @pytest.mark.asyncio
    async def test_turn_on_updates_shc_state_cache_camera_light(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """switch.*_kameralicht reads shc_state_cache — must reflect a Front-Light-only turn_on."""

        async def _fake_put(body):
            stub_coord_setup.lighting_switch_cache.setdefault(CAM_ID, {})[
                "frontLightSettings"
            ] = {"brightness": body["frontLightSettings"]["brightness"]}
            return True

        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._put_lighting_switch = AsyncMock(side_effect=_fake_put)
        entity._is_on = False
        entity._last_brightness = 80
        await entity.async_turn_on()
        assert stub_coord_setup.shc_state_cache[CAM_ID]["camera_light"] is True

    @pytest.mark.asyncio
    async def test_turn_off_updates_shc_state_cache_camera_light(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Front-Light-only turn_off, with top/bottom LEDs already off, must clear camera_light."""
        stub_coord_setup.shc_state_cache[CAM_ID]["camera_light"] = True

        async def _fake_put(body):
            stub_coord_setup.lighting_switch_cache.setdefault(CAM_ID, {})[
                "frontLightSettings"
            ] = {"brightness": 0}
            return True

        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._put_lighting_switch = AsyncMock(side_effect=_fake_put)
        entity._is_on = True
        entity._brightness = 80
        await entity.async_turn_off()
        assert stub_coord_setup.shc_state_cache[CAM_ID]["camera_light"] is False, (
            "turn_off must clear the camera_light switch's cache once every "
            "LED group is confirmed at brightness 0 (GitHub #66)"
        )

    @pytest.mark.asyncio
    async def test_turn_on_failure_does_not_stamp_write_lock(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A failed PUT must NOT stamp light_set_at — else it blocks the
        SHC/cloud poll from correcting reality with nothing actually
        written to protect (bug-hunt round-2 finding: the sync call was
        previously unconditional, even on failure)."""
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._put_lighting_switch = AsyncMock(return_value=False)
        entity._is_on = False
        entity._last_brightness = 80
        await entity.async_turn_on()
        assert CAM_ID not in stub_coord_setup.light_set_at

    @pytest.mark.asyncio
    async def test_turn_off_failure_does_not_stamp_write_lock(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._put_lighting_switch = AsyncMock(return_value=False)
        entity._is_on = True
        entity._brightness = 80
        await entity.async_turn_off()
        assert CAM_ID not in stub_coord_setup.light_set_at

    @pytest.mark.asyncio
    async def test_turn_off_clears_front_light_and_intensity(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """switch.*_front_light and number.*_front_light_intensity (unlike
        wallwasher/camera_light) were previously never written by
        _sync_wallwasher_cache — pin both fields explicitly (bug-hunt
        round-2 finding)."""
        stub_coord_setup.shc_state_cache[CAM_ID]["front_light"] = True
        stub_coord_setup.shc_state_cache[CAM_ID]["front_light_intensity"] = 0.8

        async def _fake_put(body):
            stub_coord_setup.lighting_switch_cache.setdefault(CAM_ID, {})[
                "frontLightSettings"
            ] = {"brightness": 0}
            return True

        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._put_lighting_switch = AsyncMock(side_effect=_fake_put)
        entity._is_on = True
        entity._brightness = 80
        await entity.async_turn_off()
        assert stub_coord_setup.shc_state_cache[CAM_ID]["front_light"] is False
        assert stub_coord_setup.shc_state_cache[CAM_ID]["front_light_intensity"] == 0.0

    @pytest.mark.asyncio
    async def test_turn_on_sets_front_light_intensity_from_brightness(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        async def _fake_put(body):
            stub_coord_setup.lighting_switch_cache.setdefault(CAM_ID, {})[
                "frontLightSettings"
            ] = {"brightness": body["frontLightSettings"]["brightness"]}
            return True

        entity = self._make_front_light(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._put_lighting_switch = AsyncMock(side_effect=_fake_put)
        entity._is_on = False
        entity._last_brightness = 80
        await entity.async_turn_on()
        assert stub_coord_setup.shc_state_cache[CAM_ID]["front_light"] is True
        assert stub_coord_setup.shc_state_cache[CAM_ID]["front_light_intensity"] == 0.8


class TestTopLedLightTurnOn:
    def _make_top_led(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entity = BoschTopLedLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        entity._sync_wallwasher_cache = MagicMock()
        return entity

    @pytest.mark.asyncio
    async def test_turn_on_with_rgb_sends_color_hex(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Turn ON with ATTR_RGB_COLOR must convert to #RRGGBB and include in PUT body."""
        from homeassistant.components.light import ATTR_RGB_COLOR

        entity = self._make_top_led(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on(**{ATTR_RGB_COLOR: (255, 0, 128)})
        call_args = entity._put_lighting_switch.call_args[0][0]
        assert call_args["topLedLightSettings"]["color"] == "#FF0080", (
            "RGB (255,0,128) must be sent as #FF0080"
        )
        assert call_args["topLedLightSettings"]["whiteBalance"] is None, (
            "whiteBalance must be None when color is set (API requires mutual exclusion)"
        )

    @pytest.mark.asyncio
    async def test_turn_on_enables_topdown_endpoint(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Turn ON must also enable the topdown lighting endpoint (ambient mode)."""
        entity = self._make_top_led(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._last_brightness = 80
        await entity.async_turn_on()
        entity._put_switch_endpoint.assert_called_with("topdown", True)

    @pytest.mark.asyncio
    async def test_preconfigure_while_off_with_rgb(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Color change while off must store color locally without calling API."""
        from homeassistant.components.light import ATTR_RGB_COLOR

        entity = self._make_top_led(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = False
        await entity.async_turn_on(**{ATTR_RGB_COLOR: (0, 255, 0)})
        entity._put_lighting_switch.assert_not_called()
        assert entity._last_color_hex == "#00FF00", (
            "RGB color must be stored as last_color_hex in preconfigure mode"
        )

    @pytest.mark.asyncio
    async def test_turn_off_sends_brightness_zero(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        entity = self._make_top_led(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._brightness = 70
        stub_coord_setup.lighting_switch_cache[CAM_ID] = {
            "topLedLightSettings": {"brightness": 0},
            "bottomLedLightSettings": {"brightness": 0},
        }
        await entity.async_turn_off()
        put_call = entity._put_lighting_switch.call_args[0][0]
        assert put_call["topLedLightSettings"]["brightness"] == 0, (
            "Turn off must send brightness=0 for topLedLightSettings"
        )
        assert entity._is_on is False, "Entity must be off after turn_off"

    @pytest.mark.asyncio
    async def test_turn_off_disables_topdown_when_both_leds_off(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Topdown endpoint must be disabled when both Top and Bottom brightness reach 0."""
        entity = self._make_top_led(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._brightness = 70
        # Both LEDs at 0 after PUT
        stub_coord_setup.lighting_switch_cache[CAM_ID] = {
            "topLedLightSettings": {"brightness": 0},
            "bottomLedLightSettings": {"brightness": 0},
        }
        await entity.async_turn_off()
        entity._put_switch_endpoint.assert_called_with("topdown", False)


class TestLightBaseAvailableAndBrightness:
    def test_available_requires_only_coordinator_success(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Light entities must be available when coordinator succeeded (no camera-online gate)."""
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(stub_coord_setup, CAM_ID, stub_entry)
        stub_coord_setup.last_update_success = True
        assert entity.available is True, (
            "Light entity must be available when coordinator succeeded"
        )

    def test_brightness_returns_last_brightness_when_off(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """brightness property must return last_brightness (scaled to 0-255) when light is off."""
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(stub_coord_setup, CAM_ID, stub_entry)
        entity._is_on = False
        entity._brightness = 0
        entity._last_brightness = 80
        stub_coord_setup.lighting_switch_cache = {}  # ensure _load_state_from_cache is a no-op
        b = entity.brightness
        assert b == round(80 * 255 / 100), (
            "Brightness when off must return last_brightness scaled to HA 0-255"
        )


class TestLightWriteFailureGating:
    """Regression: light turn_on/off must commit the optimistic is_on/brightness
    only when _put_lighting_switch() succeeds. is_on returns the raw instance
    var, so a failed PUT previously showed the light in the wrong state until the
    next slow poll, and stamped the wallwasher write-lock."""

    def _front(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=False)  # PUT FAILS
        entity._put_switch_endpoint = AsyncMock(return_value=False)
        return entity

    def _top(self, coord, entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entity = BoschTopLedLight(coord, CAM_ID, entry)
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=False)  # PUT FAILS
        entity._put_switch_endpoint = AsyncMock(return_value=False)
        entity._sync_wallwasher_cache = MagicMock()
        return entity

    @pytest.mark.asyncio
    async def test_front_turn_off_keeps_on_when_put_fails(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        entity = self._front(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._brightness = 60
        await entity.async_turn_off()
        assert entity._is_on is True, "is_on must stay True when the off-PUT fails"
        assert entity._brightness == 60, (
            "brightness must be unchanged on a failed off-PUT"
        )
        entity._put_switch_endpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_top_led_turn_off_keeps_on_when_put_fails(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        entity = self._top(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._is_on = True
        entity._brightness = 70
        await entity.async_turn_off()
        assert entity._is_on is True, "is_on must stay True when the off-PUT fails"
        assert entity._brightness == 70, (
            "brightness must be unchanged on a failed off-PUT"
        )
        entity._put_switch_endpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_front_turn_off_clears_on_success(
        self, stub_coord_setup: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Control: a successful off-PUT does flip is_on to False."""
        entity = self._front(stub_coord_setup, stub_entry)  # type: ignore[no-untyped-call]
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        entity._is_on = True
        entity._brightness = 60
        await entity.async_turn_off()
        assert entity._is_on is False, "is_on must be False after a successful off-PUT"


# Section: doubled entity-name-prefix regression (relocated from
# tests/test_doubled_prefix_light_binary_sensor.py — the binary_sensor.py
# half lives in tests/test_binary_sensor.py). Classes with
# `_attr_has_entity_name=True` AND `_attr_name=f"Bosch {cam_title} <Suffix>"`
# produced entity_ids like `light.bosch_est_bosch_est_oberes_licht` because
# HA already prepends the device name when has_entity_name=True.


@pytest.fixture
def stub_coord_light_prefix() -> SimpleNamespace:
    """Minimal coordinator for the doubled-prefix light tests."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                }
            }
        },
        lighting_switch_cache={
            CAM_ID: {
                "frontLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "topLedLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
                "bottomLedLightSettings": {
                    "brightness": 0,
                    "color": None,
                    "whiteBalance": -1.0,
                },
            }
        },
        last_update_success=True,
        token="tok",
    )


def _no_doubled_prefix_light(entity) -> bool:
    """Return True when _attr_name is None or does not start with 'Bosch '."""
    name = getattr(entity, "_attr_name", None)
    return name is None or not name.startswith("Bosch ")


def _has_entity_name_light(entity) -> bool:
    """Resolve _attr_has_entity_name through the MRO."""
    for cls in type(entity).__mro__:
        if "_attr_has_entity_name" in cls.__dict__:
            return bool(cls.__dict__["_attr_has_entity_name"])
    return bool(getattr(entity, "_attr_has_entity_name", False))


class TestTopLedLightPrefix:
    """light.py BoschTopLedLight (Oberes Licht)"""

    def test_name_no_doubled_prefix(
        self, stub_coord_light_prefix: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entity = BoschTopLedLight(stub_coord_light_prefix, CAM_ID, stub_entry)
        assert _no_doubled_prefix_light(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(
        self, stub_coord_light_prefix: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entity = BoschTopLedLight(stub_coord_light_prefix, CAM_ID, stub_entry)
        assert _has_entity_name_light(entity)


class TestBottomLedLightPrefix:
    """light.py BoschBottomLedLight (Unteres Licht)"""

    def test_name_no_doubled_prefix(
        self, stub_coord_light_prefix: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschBottomLedLight

        entity = BoschBottomLedLight(stub_coord_light_prefix, CAM_ID, stub_entry)
        assert _no_doubled_prefix_light(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(
        self, stub_coord_light_prefix: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschBottomLedLight

        entity = BoschBottomLedLight(stub_coord_light_prefix, CAM_ID, stub_entry)
        assert _has_entity_name_light(entity)


class TestFrontLightPrefixDoubling:
    """light.py BoschFrontLight (Frontlicht)"""

    def test_name_no_doubled_prefix(
        self, stub_coord_light_prefix: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(stub_coord_light_prefix, CAM_ID, stub_entry)
        assert _no_doubled_prefix_light(entity), (
            f"_attr_name={entity._attr_name!r} still contains 'Bosch '"
        )

    def test_has_entity_name(
        self, stub_coord_light_prefix: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entity = BoschFrontLight(stub_coord_light_prefix, CAM_ID, stub_entry)
        assert _has_entity_name_light(entity)


# Section: Gen2 LAN-reachable fallback availability (relocated from
# tests/test_misc_small_gaps.py)


class TestLightLanFallbackAvailability:
    """`_BoschLightBase.available` falls back to a LAN-RCP reachability check
    when the cloud coordinator update failed, but only for Gen2 cameras."""

    def test_lan_fallback_returns_false_without_helper(self):
        """`is_lan_reachable` missing on stub coords (older builds) → False."""
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(last_update_success=False)
        assert _BoschLightBase.available.fget(light) is False

    def test_lan_fallback_returns_false_when_not_gen2(self):
        """Gen1 cams never get the LAN-RCP fallback — must stay unavailable."""
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(
            last_update_success=False,
            is_lan_reachable=lambda _c: True,
            hw_version={"C": "CAMERA_EYES"},  # Gen1
        )
        with patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=False,
        ):
            assert _BoschLightBase.available.fget(light) is False

    def test_lan_fallback_returns_true_when_gen2_and_lan_reachable(self):
        """Gen2 + LAN-pingable → light stays controllable during a cloud 503."""
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(
            last_update_success=False,
            is_lan_reachable=lambda _c: True,
            hw_version={"C": "HOME_Eyes_Outdoor"},  # Gen2
        )
        with patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=True,
        ):
            assert _BoschLightBase.available.fget(light) is True

    def test_lan_fallback_returns_false_when_gen2_but_unreachable(self):
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(
            last_update_success=False,
            is_lan_reachable=lambda _c: False,
            hw_version={"C": "HOME_Eyes_Outdoor"},
        )
        with patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=True,
        ):
            assert _BoschLightBase.available.fget(light) is False


# Section: privacy-mode guard on light turn_on (relocated from
# tests/test_privacy_guard_branches.py — the switch.py/number.py siblings
# live in tests/test_switch.py and tests/test_number.py)


def _stub_coord_with_privacy_light(
    privacy_on: bool = False, hw: str = "HOME_Eyes_Indoor"
):
    """Coordinator stub that `_warn_if_privacy_on` can interrogate."""
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Innenbereich",
                    "hardwareVersion": hw,
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:02",
                },
            }
        },
        shc_state_cache={CAM_ID: {"privacy_mode": privacy_on}},
        lighting_switch_cache={},
        light_set_at={},
        last_update_success=True,
        token="tok-A",
        is_camera_online=lambda cid: True,
        async_put_camera=AsyncMock(return_value=True),
        async_update_listeners=MagicMock(),
    )


def _hass_stub_light():
    svc = SimpleNamespace(async_call=AsyncMock())
    return SimpleNamespace(services=svc)


class TestRgbLedLightPrivacyGuard:
    """When privacy is ON, `_BoschRgbLedLight.async_turn_on` must abort and
    NOT call the API."""

    def _make_top_led(self, coord):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight

        entry = SimpleNamespace(data={}, options={})
        entity = BoschTopLedLight.__new__(BoschTopLedLight)
        entity.coordinator = coord
        entity._cam_id = CAM_ID
        entity._entry = entry
        entity._cam_title = "Innenbereich"
        entity._model = "HOME_Eyes_Indoor"
        entity._model_name = "Eyes Indoor"
        entity._fw = "9.40.25"
        entity._mac = "aa:bb:cc:dd:ee:02"
        entity._brightness = 0
        entity._last_brightness = 80
        entity._color_hex = None
        entity._last_color_hex = None
        entity._white_balance = None
        entity._last_white_balance = -1.0
        entity._is_on = True
        entity._led_key = "topLedLightSettings"
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        entity._sync_wallwasher_cache = MagicMock()
        entity.hass = _hass_stub_light()
        return entity

    @pytest.mark.asyncio
    async def test_turn_on_blocked_when_privacy_on(self):
        coord = _stub_coord_with_privacy_light(privacy_on=True)
        entity = self._make_top_led(coord)

        with patch(
            "custom_components.bosch_shc_camera.light._warn_if_privacy_on",
            new=AsyncMock(return_value=True),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_not_called()
        entity._put_switch_endpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_on_proceeds_when_privacy_off(self):
        coord = _stub_coord_with_privacy_light(privacy_on=False)
        entity = self._make_top_led(coord)

        with patch(
            "custom_components.bosch_shc_camera.light._warn_if_privacy_on",
            new=AsyncMock(return_value=False),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_called_once()


class TestFrontLightPrivacyGuard:
    """When privacy is ON, `BoschFrontLight.async_turn_on` must abort early."""

    def _make_front_light(self, coord):
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        entry = SimpleNamespace(data={}, options={})
        entity = BoschFrontLight.__new__(BoschFrontLight)
        entity.coordinator = coord
        entity._cam_id = CAM_ID
        entity._entry = entry
        entity._cam_title = "Innenbereich"
        entity._model = "HOME_Eyes_Indoor"
        entity._model_name = "Eyes Indoor"
        entity._fw = "9.40.25"
        entity._mac = "aa:bb:cc:dd:ee:02"
        entity._brightness = 0
        entity._last_brightness = 80
        entity._color_hex = None
        entity._last_color_hex = None
        entity._white_balance = -1.0
        entity._last_white_balance = -1.0
        entity._is_on = True
        entity._led_key = "frontLightSettings"
        entity.async_write_ha_state = MagicMock()
        entity._put_lighting_switch = AsyncMock(return_value=True)
        entity._put_switch_endpoint = AsyncMock(return_value=True)
        entity.hass = _hass_stub_light()
        return entity

    @pytest.mark.asyncio
    async def test_front_light_blocked_when_privacy_on(self):
        coord = _stub_coord_with_privacy_light(privacy_on=True)
        entity = self._make_front_light(coord)

        with patch(
            "custom_components.bosch_shc_camera.light._warn_if_privacy_on",
            new=AsyncMock(return_value=True),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_not_called()
        entity._put_switch_endpoint.assert_not_called()

    @pytest.mark.asyncio
    async def test_front_light_proceeds_when_privacy_off(self):
        coord = _stub_coord_with_privacy_light(privacy_on=False)
        entity = self._make_front_light(coord)

        with patch(
            "custom_components.bosch_shc_camera.light._warn_if_privacy_on",
            new=AsyncMock(return_value=False),
        ):
            await entity.async_turn_on()

        entity._put_lighting_switch.assert_called_once()


# Section: GH#3 — Gen2 Outdoor RGB light classes (relocated from
# tests/test_github_issues.py — the switch.py wallwasher/front-light switch
# classes for the same issue are already covered in tests/test_switch.py;
# the models.py Gen2 config check lives in tests/test_models.py)


class TestGH3Gen2RgbLightClasses:
    def test_gen2_outdoor_has_rgb_light_classes(self):
        """RGB color picker classes for top + bottom LEDs + front light."""
        from custom_components.bosch_shc_camera import light as light_mod

        assert hasattr(light_mod, "BoschTopLedLight")
        assert hasattr(light_mod, "BoschBottomLedLight")
        assert hasattr(light_mod, "BoschFrontLight")


# Section: firmware-install unavailability — light.py side (relocated from
# tests/test_updating_unavailable.py — the camera.py/switch.py/init.py
# siblings live in tests/test_camera.py, tests/test_switch.py, and
# tests/test_init.py)


def _coord_light_updating(*, is_updating_value: bool) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
                "events": [],
            }
        },
        last_update_success=True,
        is_updating=lambda cam_id: is_updating_value if cam_id == CAM_ID else False,
        firmware_cache={CAM_ID: {"updating": is_updating_value}},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
        hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
        lan_tcp_reachable={CAM_ID: (True, 0.0)},
        is_lan_reachable=lambda cam_id: True,
        is_session_stale=lambda cam_id: False,
        user_intent_streams=set(),
    )


class TestLightUpdatingUnavailable:
    def _mk_light(self, coord):
        # Concrete subclass — _BoschLightBase carries the available() override.
        from custom_components.bosch_shc_camera.light import BoschFrontLight

        light = BoschFrontLight.__new__(BoschFrontLight)
        light.coordinator = coord
        light._cam_id = CAM_ID
        return light

    def test_available_when_not_updating(self):
        light = self._mk_light(_coord_light_updating(is_updating_value=False))
        assert light.available is True

    def test_unavailable_when_updating(self):
        """Light writes go via cloud or LAN RCP — both fail mid-reboot."""
        light = self._mk_light(_coord_light_updating(is_updating_value=True))
        assert light.available is False
