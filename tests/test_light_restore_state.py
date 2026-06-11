"""Light restore-state + edge-branch coverage (Bucket C).

Pins the missing light.py lines none of the existing rounds touch:
  - L147     : `device_info` property — return-dict path (cheap smoke).
  - L228     : `_put_lighting_switch` — `body[key] = val` when the update key
                is NOT already in the default body (defensive merge branch).
  - L245-246 : `_put_lighting_switch` — `resp.json()` raises on a 200 response;
                cache must NOT be updated and the function still returns True.
  - L330     : `_BoschRgbLedLight.async_turn_on` with brightness kwarg →
                `_last_brightness = max(1, round(brightness * 100 / 255))`.
  - L361     : `_BoschRgbLedLight.async_turn_off` remembers `_color_hex` into
                `_last_color_hex` so the next turn_on restores the same color.

Approach: bypass __init__ via `klass.__new__()` so we don't need the HA
framework's CoordinatorEntity setup chain. Async calls run with `pytest-asyncio`.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "00000000-0000-0000-0000-000000000001"


def _stub_coord(**overrides):
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
        _lighting_switch_cache={},
        _shc_state_cache={},
        # SENTINEL_RULE: float('-inf') so monotonic comparisons always trigger
        _light_set_at={},
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
    coord = coord or _stub_coord()
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


# ── L147 — device_info return ──────────────────────────────────────────────


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


# ── L228 — defensive merge branch in _put_lighting_switch ──────────────────


class TestPutLightingSwitchDefensiveMerge:
    """`_put_lighting_switch` merges `updates` onto the cached body. The else
    branch (`body[key] = val`) covers the defensive case where an unknown
    update key is passed in — must not crash, must include the new key."""

    @pytest.mark.asyncio
    async def test_unknown_update_key_added_to_body(self):
        """If updates contains a key not in the 3-LED default body (e.g. a
        new lighting group added by future firmware), it is assigned wholesale
        instead of dict-merged (line 228)."""
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


# ── L245-246 — resp.json() raises on 200 ───────────────────────────────────


class TestPutLightingSwitchJsonParseError:
    """If the Bosch cloud returns 200 but with a malformed body (HTML error
    page, gzip-stripped, etc.), `resp.json()` raises. The except branch
    (BUG-FIX 2026-05-28) now falls back to updating the cache from the sent body
    so is_on reads True after a successful write even when the server returns
    204 No Content or an unparseable body."""

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
        # BUG-FIX 2026-05-28: cache IS updated from the sent body (optimistic update),
        # NOT left untouched. This ensures is_on reads True after a 204 No Content response.
        cache = light.coordinator._lighting_switch_cache[CAM_ID]
        assert "topLedLightSettings" in cache, (
            "Cache must be updated from sent body when resp.json() raises (fallback path)"
        )
        assert cache["topLedLightSettings"]["brightness"] == 50, (
            "Optimistic update must apply the written brightness value"
        )


# ── L330 — async_turn_on remembers brightness ──────────────────────────────


class TestAsyncTurnOnRemembersBrightness:
    """Calling `async_turn_on(brightness=N)` must store the converted value in
    `_last_brightness` so a later turn_on without brightness restores it.
    `max(1, round(brightness * 100 / 255))` guarantees sentinel brightness=1
    doesn't collapse to 0% (line 330)."""

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
        # but L330 still ran before the early return.
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


# ── L361 — async_turn_off remembers color_hex ──────────────────────────────


class TestAsyncTurnOffRemembersColor:
    """Before zeroing brightness, the off-handler must persist the current
    `_color_hex` into `_last_color_hex` so the next turn_on restores the
    same color rather than reverting to warm-white default (line 361)."""

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

        # L361 branch fired: current color preserved
        assert light._last_color_hex == "#FF8000"
        assert light._is_on is False
        assert light._brightness == 0
