"""Tests for light.py — Gen2 RGB light entities (front, top LED, bottom LED).

Light entities are state-rich: they cache last_color, last_brightness,
last_white_balance to keep the card's color picker informed even when
the light is off (HA blanks `rgb_color` / `brightness` when state=off).
These tests verify the cache mechanics + extra_state_attributes contract.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


@pytest.fixture
def stub_coord():
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "64:da:a0:33:14:ae",
                }
            }
        },
        _lighting_switch_cache={
            CAM_ID: {
                "frontLightSettings": {"brightness": 0, "color": None, "whiteBalance": -1.0},
                "topLedLightSettings": {"brightness": 0, "color": None, "whiteBalance": -1.0},
                "bottomLedLightSettings": {"brightness": 0, "color": None, "whiteBalance": -1.0},
            }
        },
        last_update_success=True,
        token="tok",
    )


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


# ── BoschTopLedLight ────────────────────────────────────────────────────


class TestTopLedLight:
    def test_construction(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        assert light._led_key == "topLedLightSettings"

    def test_off_when_brightness_zero(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        # Cache has brightness=0 → is_on=False
        assert light.is_on is False

    def test_on_when_brightness_positive(self, stub_coord, stub_entry):
        stub_coord._lighting_switch_cache[CAM_ID]["topLedLightSettings"]["brightness"] = 75
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        assert light.is_on is True

    def test_brightness_scales_to_255(self, stub_coord, stub_entry):
        """API uses 0-100, HA uses 0-255 — values must be scaled."""
        stub_coord._lighting_switch_cache[CAM_ID]["topLedLightSettings"]["brightness"] = 50
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        # 50% → 127/128 in HA's 255-scale
        bri = light.brightness
        assert bri is not None
        assert 100 <= bri <= 150  # ~127

    def test_extra_attrs_warm_white_default_when_no_color(self, stub_coord, stub_entry):
        """When user never picked a color, card sees a warm-white default
        so the color dot isn't grey on first load."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        attrs = light.extra_state_attributes
        assert "last_rgb_color" in attrs
        # Warm-white-ish — high red, mid green, low blue
        r, g, b = attrs["last_rgb_color"]
        assert r > g > b

    def test_extra_attrs_preserves_user_color(self, stub_coord, stub_entry):
        """If the user picked a color, that's what appears in last_rgb_color."""
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        light._last_color_hex = "#FF0080"
        attrs = light.extra_state_attributes
        assert attrs["last_rgb_color"] == [255, 0, 128]

    def test_extra_attrs_invalid_hex_does_not_raise(self, stub_coord, stub_entry):
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

    def test_available_follows_coordinator(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschTopLedLight
        light = BoschTopLedLight(stub_coord, CAM_ID, stub_entry)
        assert light.available is True
        stub_coord.last_update_success = False
        assert light.available is False


# ── BoschBottomLedLight ─────────────────────────────────────────────────


class TestBottomLedLight:
    def test_uses_bottom_led_key(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschBottomLedLight
        light = BoschBottomLedLight(stub_coord, CAM_ID, stub_entry)
        assert light._led_key == "bottomLedLightSettings"


# ── BoschFrontLight ─────────────────────────────────────────────────────


class TestFrontLight:
    def test_uses_front_led_key(self, stub_coord, stub_entry):
        from custom_components.bosch_shc_camera.light import BoschFrontLight
        light = BoschFrontLight(stub_coord, CAM_ID, stub_entry)
        assert light._led_key == "frontLightSettings"
