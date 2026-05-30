"""Tests for BoschWifiSignalSensor and BoschFirmwareVersionSensor.

Covers per PIN_EVERY_MODE: one test per distinct value/state + None + garbage input.
No HA runtime needed — uses SimpleNamespace stubs.

Feature: diagnostic sensors (entity_category=DIAGNOSTIC, wifi signal %, firmware string).
Source: /v11/video_inputs/{id}/wifiinfo (signalStrength 0-100%) + info.firmwareVersion.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.helpers.entity import EntityCategory

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_coord(
    firmware: str = "9.40.25",
    wifiinfo: dict[str, Any] | None = None,
    last_update_success: bool = True,
    rcp_lan_ip: str | None = None,
    rcp_bitrate_ladder: list[int] | None = None,
    rcp_product_name: str | None = None,
    up_to_date: bool | None = None,
    hardware_version: str = "HOME_Eyes_Outdoor",
) -> Any:
    """Build a minimal coordinator stub for sensor tests."""
    info: dict[str, Any] = {
        "firmwareVersion": firmware,
        "hardwareVersion": hardware_version,
        "macAddress": "aa:bb:cc:33:14:ae",
        "title": "Terrasse",
    }
    if up_to_date is not None:
        info["upToDate"] = up_to_date

    coord = SimpleNamespace(
        data={CAM_ID: {"info": info, "status": "ONLINE", "events": []}},
        last_update_success=last_update_success,
        _wifiinfo_cache={} if wifiinfo is None else {CAM_ID: wifiinfo},
        rcp_lan_ip=lambda cid: rcp_lan_ip,
        rcp_bitrate_ladder=lambda cid: rcp_bitrate_ladder,
        rcp_product_name=lambda cid: rcp_product_name,
        async_request_refresh=None,
    )
    return coord


def _make_entry() -> Any:
    return SimpleNamespace(entry_id="test_entry", options={})


# ── BoschWifiSignalSensor ─────────────────────────────────────────────────────


class TestWifiSignalSensor:
    """Tests for BoschWifiSignalSensor (entity_category=DIAGNOSTIC, unit=%, no dBm device_class)."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschWifiSignalSensor

        c = coord if coord is not None else _make_coord()
        return BoschWifiSignalSensor(c, CAM_ID, _make_entry())

    # entity metadata
    def test_entity_category_is_diagnostic(self) -> None:
        s = self._make()
        assert s.entity_category == EntityCategory.DIAGNOSTIC

    def test_unit_is_percent(self) -> None:
        s = self._make()
        assert s.native_unit_of_measurement == "%"

    def test_icon(self) -> None:
        s = self._make()
        assert s.icon == "mdi:wifi"

    def test_translation_key(self) -> None:
        s = self._make()
        assert s.translation_key == "wifi_signal"

    # native_value: None when no wifiinfo in cache
    def test_native_value_none_when_cache_empty(self) -> None:
        s = self._make(_make_coord(wifiinfo=None))
        assert s.native_value is None

    # native_value: valid typical signal (mid-range)
    def test_native_value_typical_signal(self) -> None:
        s = self._make(
            _make_coord(
                wifiinfo={
                    "signalStrength": 67,
                    "ssid": "HOME",
                    "ipAddress": "192.168.1.2",
                    "macAddress": "aa:bb",
                }
            )
        )
        assert s.native_value == 67

    # native_value: minimum boundary (0)
    def test_native_value_zero_signal(self) -> None:
        s = self._make(
            _make_coord(
                wifiinfo={
                    "signalStrength": 0,
                    "ssid": "X",
                    "ipAddress": "",
                    "macAddress": "",
                }
            )
        )
        assert s.native_value == 0

    # native_value: maximum boundary (100)
    def test_native_value_full_signal(self) -> None:
        s = self._make(
            _make_coord(
                wifiinfo={
                    "signalStrength": 100,
                    "ssid": "X",
                    "ipAddress": "",
                    "macAddress": "",
                }
            )
        )
        assert s.native_value == 100

    # native_value: signalStrength key missing (garbage/partial response)
    def test_native_value_none_when_signal_key_absent(self) -> None:
        s = self._make(_make_coord(wifiinfo={"ssid": "HOME"}))  # no signalStrength key
        assert s.native_value is None

    # native_value: signalStrength explicitly null
    def test_native_value_none_when_signal_explicit_none(self) -> None:
        s = self._make(_make_coord(wifiinfo={"signalStrength": None, "ssid": "HOME"}))
        assert s.native_value is None

    # available: False when cache is empty
    def test_available_false_when_no_wifiinfo(self) -> None:
        s = self._make(_make_coord(wifiinfo=None))
        assert s.available is False

    # available: False when coordinator update failed
    def test_available_false_when_update_failed(self) -> None:
        c = _make_coord(
            wifiinfo={
                "signalStrength": 75,
                "ssid": "X",
                "ipAddress": "",
                "macAddress": "",
            },
            last_update_success=False,
        )
        s = self._make(c)
        assert s.available is False

    # available: True when cache has data + update succeeded
    def test_available_true_when_data_present(self) -> None:
        c = _make_coord(
            wifiinfo={
                "signalStrength": 80,
                "ssid": "HOME",
                "ipAddress": "192.168.1.1",
                "macAddress": "cc:dd",
            },
        )
        s = self._make(c)
        assert s.available is True

    # extra_state_attributes: basic keys always present
    def test_extra_attrs_basic_keys(self) -> None:
        c = _make_coord(
            wifiinfo={
                "signalStrength": 70,
                "ssid": "MYNET",
                "ipAddress": "10.0.0.5",
                "macAddress": "aa:bb:cc",
            }
        )
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert attrs["ssid"] == "MYNET"
        assert attrs["ip_address"] == "10.0.0.5"
        assert attrs["mac_address"] == "aa:bb:cc"

    # extra_state_attributes: lan_ip_rcp only present when rcp returns a value
    def test_extra_attrs_rcp_lan_ip_included(self) -> None:
        c = _make_coord(
            wifiinfo={
                "signalStrength": 60,
                "ssid": "X",
                "ipAddress": "",
                "macAddress": "",
            },
            rcp_lan_ip="192.0.2.149",
        )
        s = self._make(c)
        assert s.extra_state_attributes["lan_ip_rcp"] == "192.0.2.149"

    def test_extra_attrs_rcp_lan_ip_absent_when_none(self) -> None:
        c = _make_coord(
            wifiinfo={
                "signalStrength": 60,
                "ssid": "X",
                "ipAddress": "",
                "macAddress": "",
            }
        )
        s = self._make(c)
        assert "lan_ip_rcp" not in s.extra_state_attributes

    # extra_state_attributes: bitrate ladder included when rcp returns ladder
    def test_extra_attrs_bitrate_ladder_included(self) -> None:
        c = _make_coord(
            wifiinfo={
                "signalStrength": 55,
                "ssid": "Y",
                "ipAddress": "",
                "macAddress": "",
            },
            rcp_bitrate_ladder=[500, 1000, 2000],
        )
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert attrs["bitrate_ladder_kbps"] == [500, 1000, 2000]
        assert attrs["max_bitrate_kbps"] == 2000

    def test_extra_attrs_bitrate_ladder_absent_when_none(self) -> None:
        c = _make_coord(
            wifiinfo={
                "signalStrength": 55,
                "ssid": "Y",
                "ipAddress": "",
                "macAddress": "",
            }
        )
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert "bitrate_ladder_kbps" not in attrs
        assert "max_bitrate_kbps" not in attrs

    # extra_state_attributes: empty wifiinfo cache still returns all keys (empty strings)
    def test_extra_attrs_empty_wifiinfo_cache(self) -> None:
        c = _make_coord(wifiinfo=None)
        s = self._make(c)
        attrs = s.extra_state_attributes
        assert attrs["ssid"] == ""
        assert attrs["ip_address"] == ""
        assert attrs["mac_address"] == ""


# ── BoschFirmwareVersionSensor ────────────────────────────────────────────────


class TestFirmwareVersionSensor:
    """Tests for BoschFirmwareVersionSensor (entity_category=DIAGNOSTIC, string state)."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschFirmwareVersionSensor

        c = coord if coord is not None else _make_coord()
        return BoschFirmwareVersionSensor(c, CAM_ID, _make_entry())

    # entity metadata
    def test_entity_category_is_diagnostic(self) -> None:
        s = self._make()
        assert s.entity_category == EntityCategory.DIAGNOSTIC

    def test_no_state_class(self) -> None:
        """Firmware is a string — no measurement state class."""
        s = self._make()
        assert s.state_class is None

    def test_no_unit(self) -> None:
        s = self._make()
        assert s.native_unit_of_measurement is None

    def test_icon(self) -> None:
        s = self._make()
        assert s.icon == "mdi:chip"

    def test_translation_key(self) -> None:
        s = self._make()
        assert s.translation_key == "firmware_version"

    # native_value: typical version string (Gen2)
    def test_native_value_gen2_version(self) -> None:
        s = self._make(_make_coord(firmware="9.40.25"))
        assert s.native_value == "9.40.25"

    # native_value: Gen1 version string
    def test_native_value_gen1_version(self) -> None:
        s = self._make(_make_coord(firmware="7.91.56"))
        assert s.native_value == "7.91.56"

    # native_value: empty string → None
    def test_native_value_none_when_empty_string(self) -> None:
        s = self._make(_make_coord(firmware=""))
        assert s.native_value is None

    # native_value: missing key (info dict has no firmwareVersion)
    def test_native_value_none_when_key_missing(self) -> None:
        coord = _make_coord(firmware="9.40.25")
        del coord.data[CAM_ID]["info"]["firmwareVersion"]
        s = self._make(coord)
        assert s.native_value is None

    # available: False when firmware is empty
    def test_available_false_when_empty_firmware(self) -> None:
        s = self._make(_make_coord(firmware=""))
        assert s.available is False

    # available: False when update failed
    def test_available_false_when_update_failed(self) -> None:
        s = self._make(_make_coord(firmware="9.40.25", last_update_success=False))
        assert s.available is False

    # available: True when firmware present + update succeeded
    def test_available_true_when_firmware_present(self) -> None:
        s = self._make(_make_coord(firmware="9.40.25"))
        assert s.available is True

    # extra_state_attributes: up_to_date from top-level info key
    def test_extra_attrs_up_to_date_top_level(self) -> None:
        s = self._make(_make_coord(firmware="9.40.25", up_to_date=True))
        assert s.extra_state_attributes["up_to_date"] is True

    # extra_state_attributes: up_to_date from featureSupport fallback
    def test_extra_attrs_up_to_date_feature_support_fallback(self) -> None:
        coord = _make_coord(firmware="9.40.25")
        coord.data[CAM_ID]["info"]["featureSupport"] = {"upToDate": False}
        s = self._make(coord)
        assert s.extra_state_attributes["up_to_date"] is False

    # extra_state_attributes: up_to_date None when not present in either location
    def test_extra_attrs_up_to_date_none_when_absent(self) -> None:
        s = self._make(_make_coord(firmware="9.40.25"))  # no upToDate key
        assert s.extra_state_attributes["up_to_date"] is None

    # extra_state_attributes: hardware_version always present
    def test_extra_attrs_hardware_version(self) -> None:
        s = self._make(
            _make_coord(firmware="9.40.25", hardware_version="HOME_Eyes_Outdoor")
        )
        assert s.extra_state_attributes["hardware_version"] == "HOME_Eyes_Outdoor"

    # extra_state_attributes: product_name_rcp included when rcp returns value
    def test_extra_attrs_product_name_included(self) -> None:
        c = _make_coord(
            firmware="9.40.25", rcp_product_name="FLEXIDOME IP outdoor 4000i"
        )
        s = self._make(c)
        assert (
            s.extra_state_attributes["product_name_rcp"] == "FLEXIDOME IP outdoor 4000i"
        )

    def test_extra_attrs_product_name_absent_when_none(self) -> None:
        s = self._make(_make_coord(firmware="9.40.25"))
        assert "product_name_rcp" not in s.extra_state_attributes
