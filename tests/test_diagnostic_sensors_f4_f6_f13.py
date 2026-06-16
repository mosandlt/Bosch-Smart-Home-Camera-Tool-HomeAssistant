"""Tests for diagnostic sensors F4 (ONVIF Scopes), F6 (RCP Version), F13 (Cloud Feature Flags).

Per PLATINUM_DISCIPLINE: 100% coverage on new code paths.
Per PIN_EVERY_MODE: one test per distinct state + unavailable + edge-case.

Covers:
- BoschOnvifScopesSensor (F4): happy-path, unavailable, extra_state_attributes
- BoschRcpVersionSensor (F6): happy-path, unavailable, extra_state_attributes, version format
- BoschCloudFeatureFlagsSensor (F13): happy-path, unavailable, no-flags, extra_state_attributes
- _parse_onvif_scopes helper: full TLV, empty, partial, non-ONVIF scopes
- _fetch_rcp_lan helper: no-IP, no-creds, HTTP 401, RCP error, success
- _async_update_lan_diagnostic_sensors: F4/F6 paths, error-swallowing
"""

from __future__ import annotations

import asyncio
import struct
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.entity import EntityCategory

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── Stub builders ──────────────────────────────────────────────────────────────


def _make_coord(
    *,
    onvif_scopes: dict[str, Any] | None = None,
    rcp_version: str | None = None,
    feature_flags: dict[str, bool] | None = None,
    last_update_success: bool = True,
    lan_ip: str | None = "192.0.2.149",
    local_creds: dict[str, Any] | None = None,
) -> Any:
    """Build a minimal coordinator stub for F4/F6/F13 sensor tests."""
    info: dict[str, Any] = {
        "firmwareVersion": "9.40.102",
        "hardwareVersion": "HOME_Eyes_Outdoor",
        "macAddress": "aa:bb:cc:33:14:ae",
        "title": "Terrasse",
    }
    _local_creds: dict[str, Any] = (
        local_creds
        if local_creds is not None
        else {
            "user": "cbs-A1B2C3D4",
            "password": "secret123",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 1000.0,
        }
    )
    coord = SimpleNamespace(
        data={CAM_ID: {"info": info, "status": "ONLINE", "events": []}},
        last_update_success=last_update_success,
        _rcp_onvif_scopes_cache=(
            {CAM_ID: onvif_scopes} if onvif_scopes is not None else {}
        ),
        _rcp_version_cache=({CAM_ID: rcp_version} if rcp_version is not None else {}),
        _feature_flags=feature_flags if feature_flags is not None else {},
        _local_creds_cache={CAM_ID: _local_creds} if _local_creds else {},
        _rcp_lan_ip_cache={CAM_ID: lan_ip} if lan_ip else {},
        async_request_refresh=None,
    )

    def _get_cam_lan_ip(cam_id: str) -> str | None:
        ip = coord._rcp_lan_ip_cache.get(cam_id)
        if ip:
            return ip
        creds = coord._local_creds_cache.get(cam_id)
        return creds.get("host") if creds else None

    coord._get_cam_lan_ip = _get_cam_lan_ip
    return coord


def _make_entry() -> Any:
    return SimpleNamespace(entry_id="test_entry", options={})


# ── F4: BoschOnvifScopesSensor ────────────────────────────────────────────────


class TestBoschOnvifScopesSensor:
    """Tests for BoschOnvifScopesSensor (F4)."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschOnvifScopesSensor

        c = coord if coord is not None else _make_coord()
        return BoschOnvifScopesSensor(c, CAM_ID, _make_entry())

    def test_entity_category_is_diagnostic(self) -> None:
        assert self._make().entity_category == EntityCategory.DIAGNOSTIC

    def test_disabled_by_default(self) -> None:
        assert self._make().entity_registry_enabled_default is False

    def test_translation_key(self) -> None:
        assert self._make().translation_key == "onvif_scopes"

    def test_unique_id(self) -> None:
        s = self._make()
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_onvif_scopes"

    def test_native_value_returns_supported_when_scopes_present(self) -> None:
        scopes = {
            "supported": True,
            "name": "Terrasse",
            "hardware": "HOME_Eyes_Outdoor",
            "profiles": ["Streaming"],
            "raw_scopes": [],
        }
        s = self._make(_make_coord(onvif_scopes=scopes))
        # BoschOnvifScopesSensor.native_value returns "supported" (the enum
        # value used in _attr_options); the HA translation layer maps this to
        # the localised string "ONVIF supported" at render time.
        assert s.native_value == "supported"

    def test_native_value_none_when_no_scopes(self) -> None:
        s = self._make(_make_coord(onvif_scopes=None))
        assert s.native_value is None

    def test_native_value_none_when_empty_dict(self) -> None:
        # empty dict is falsy
        coord = _make_coord(onvif_scopes=None)
        coord._rcp_onvif_scopes_cache = {CAM_ID: {}}
        s = self._make(coord)
        assert s.native_value is None

    def test_available_true_when_scopes_present(self) -> None:
        scopes = {
            "supported": True,
            "name": "Terrasse",
            "hardware": "",
            "profiles": [],
            "raw_scopes": [],
        }
        s = self._make(_make_coord(onvif_scopes=scopes))
        assert s.available is True

    def test_available_false_when_no_scopes(self) -> None:
        assert self._make(_make_coord(onvif_scopes=None)).available is False

    def test_available_false_when_update_failed(self) -> None:
        scopes = {
            "supported": True,
            "name": "X",
            "hardware": "",
            "profiles": [],
            "raw_scopes": [],
        }
        s = self._make(_make_coord(onvif_scopes=scopes, last_update_success=False))
        assert s.available is False

    def test_extra_attrs_keys_present(self) -> None:
        scopes = {
            "supported": True,
            "name": "Terrasse",
            "hardware": "HOME_Eyes_Outdoor",
            "profiles": ["Streaming"],
            "raw_scopes": ["onvif://x"],
        }
        s = self._make(_make_coord(onvif_scopes=scopes))
        attrs = s.extra_state_attributes
        assert attrs["name"] == "Terrasse"
        assert attrs["hardware"] == "HOME_Eyes_Outdoor"
        assert attrs["profiles"] == ["Streaming"]
        assert attrs["raw_scopes"] == ["onvif://x"]

    def test_extra_attrs_empty_when_no_scopes(self) -> None:
        s = self._make(_make_coord(onvif_scopes=None))
        attrs = s.extra_state_attributes
        assert attrs["name"] == ""
        assert attrs["hardware"] == ""
        assert attrs["profiles"] == []
        assert attrs["raw_scopes"] == []


# ── F6: BoschRcpVersionSensor ─────────────────────────────────────────────────


class TestBoschRcpVersionSensor:
    """Tests for BoschRcpVersionSensor (F6)."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import BoschRcpVersionSensor

        c = coord if coord is not None else _make_coord()
        return BoschRcpVersionSensor(c, CAM_ID, _make_entry())

    def test_entity_category_is_diagnostic(self) -> None:
        assert self._make().entity_category == EntityCategory.DIAGNOSTIC

    def test_disabled_by_default(self) -> None:
        assert self._make().entity_registry_enabled_default is False

    def test_translation_key(self) -> None:
        assert self._make().translation_key == "rcp_version"

    def test_unique_id(self) -> None:
        s = self._make()
        assert s.unique_id == f"bosch_shc_camera_{CAM_ID}_rcp_version"

    def test_native_value_gen2(self) -> None:
        s = self._make(_make_coord(rcp_version="1.2.38.150"))
        assert s.native_value == "1.2.38.150"

    def test_native_value_gen1(self) -> None:
        s = self._make(_make_coord(rcp_version="1.2.9.225"))
        assert s.native_value == "1.2.9.225"

    def test_native_value_none_when_no_version(self) -> None:
        s = self._make(_make_coord(rcp_version=None))
        assert s.native_value is None

    def test_available_true_when_version_present(self) -> None:
        assert self._make(_make_coord(rcp_version="1.2.38.150")).available is True

    def test_available_false_when_no_version(self) -> None:
        assert self._make(_make_coord(rcp_version=None)).available is False

    def test_available_false_when_update_failed(self) -> None:
        s = self._make(_make_coord(rcp_version="1.2.38.150", last_update_success=False))
        assert s.available is False

    def test_extra_attrs_version_parts(self) -> None:
        s = self._make(_make_coord(rcp_version="1.2.38.150"))
        attrs = s.extra_state_attributes
        assert attrs["major"] == "1"
        assert attrs["minor"] == "2"
        assert attrs["patch"] == "38"
        assert attrs["build"] == "150"

    def test_extra_attrs_empty_when_no_version(self) -> None:
        s = self._make(_make_coord(rcp_version=None))
        assert s.extra_state_attributes == {}

    def test_extra_attrs_partial_version(self) -> None:
        # Short version string with fewer than 4 components
        coord = _make_coord(rcp_version=None)
        coord._rcp_version_cache = {CAM_ID: "1.2"}
        s = self._make(coord)
        attrs = s.extra_state_attributes
        assert attrs["major"] == "1"
        assert attrs["minor"] == "2"
        assert attrs.get("patch", "") == ""
        assert attrs.get("build", "") == ""


# ── F13: BoschCloudFeatureFlagsSensor ─────────────────────────────────────────


class TestBoschCloudFeatureFlagsSensor:
    """Tests for BoschCloudFeatureFlagsSensor (F13)."""

    def _make(self, coord: Any | None = None) -> Any:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCloudFeatureFlagsSensor,
        )

        c = coord if coord is not None else _make_coord()
        return BoschCloudFeatureFlagsSensor(c, CAM_ID, _make_entry())

    def test_entity_category_is_diagnostic(self) -> None:
        assert self._make().entity_category == EntityCategory.DIAGNOSTIC

    def test_disabled_by_default(self) -> None:
        assert self._make().entity_registry_enabled_default is False

    def test_translation_key(self) -> None:
        assert self._make().translation_key == "cloud_feature_flags"

    def test_unique_id_is_account_level(self) -> None:
        # Account-level — not cam-specific
        assert self._make().unique_id == "bosch_shc_camera_cloud_feature_flags"

    def test_native_value_enabled_flags_sorted(self) -> None:
        flags = {"APP_RATING": True, "IOT_THINGS": True, "BETA_FEATURE": False}
        s = self._make(_make_coord(feature_flags=flags))
        assert s.native_value == "APP_RATING, IOT_THINGS"

    def test_native_value_none_when_no_flags(self) -> None:
        s = self._make(_make_coord(feature_flags={}))
        assert s.native_value is None

    def test_native_value_all_disabled_returns_none(self) -> None:
        # All flags False → no enabled flags → native_value = None (flags dict is empty-ish)
        # Actually empty dict evaluates as falsy, so returns None.
        # If dict non-empty but all False: returns "none"
        flags: dict[str, bool] = {"APP_RATING": False}
        s = self._make(_make_coord(feature_flags=flags))
        assert s.native_value == "none"

    def test_native_value_single_enabled_flag(self) -> None:
        flags = {"APP_RATING": True}
        s = self._make(_make_coord(feature_flags=flags))
        assert s.native_value == "APP_RATING"

    def test_available_true_when_flags_present(self) -> None:
        flags = {"APP_RATING": True}
        assert self._make(_make_coord(feature_flags=flags)).available is True

    def test_available_false_when_no_flags(self) -> None:
        assert self._make(_make_coord(feature_flags={})).available is False

    def test_available_false_when_update_failed(self) -> None:
        flags = {"APP_RATING": True}
        s = self._make(_make_coord(feature_flags=flags, last_update_success=False))
        assert s.available is False

    def test_extra_attrs_full_flags_dict(self) -> None:
        flags = {"APP_RATING": True, "IOT_THINGS": False}
        s = self._make(_make_coord(feature_flags=flags))
        attrs = s.extra_state_attributes
        assert attrs == {"APP_RATING": True, "IOT_THINGS": False}

    def test_extra_attrs_empty_when_no_flags(self) -> None:
        s = self._make(_make_coord(feature_flags={}))
        assert s.extra_state_attributes == {}


# ── _parse_onvif_scopes helper ────────────────────────────────────────────────


class TestParseOnvifScopes:
    """Tests for _parse_onvif_scopes (module-level helper in __init__.py)."""

    def _parse(self, raw: bytes) -> dict[str, Any]:
        from custom_components.bosch_shc_camera import _parse_onvif_scopes

        return _parse_onvif_scopes(raw)

    def test_supported_true(self) -> None:
        raw = b"onvif://www.onvif.org/name/MyCamera\x00"
        result = self._parse(raw)
        assert result["supported"] is True

    def test_parses_name(self) -> None:
        raw = b"onvif://www.onvif.org/name/Bosch%20Camera\x00"
        result = self._parse(raw)
        assert result["name"] == "Bosch Camera"

    def test_parses_hardware(self) -> None:
        raw = b"onvif://www.onvif.org/hardware/HOME_Eyes_Outdoor\x00"
        result = self._parse(raw)
        assert result["hardware"] == "HOME_Eyes_Outdoor"

    def test_parses_profiles(self) -> None:
        raw = b"onvif://www.onvif.org/Profile/Streaming\x00onvif://www.onvif.org/Profile/G\x00"
        result = self._parse(raw)
        assert "Streaming" in result["profiles"]
        assert "G" in result["profiles"]

    def test_parses_multiple_scopes(self) -> None:
        raw = (
            b"onvif://www.onvif.org/name/TestCam\x00"
            b"onvif://www.onvif.org/hardware/CAMERA_360\x00"
            b"onvif://www.onvif.org/Profile/Streaming\x00"
        )
        result = self._parse(raw)
        assert result["name"] == "TestCam"
        assert result["hardware"] == "CAMERA_360"
        assert result["profiles"] == ["Streaming"]

    def test_raw_scopes_included(self) -> None:
        raw = b"onvif://www.onvif.org/name/X\x00"
        result = self._parse(raw)
        assert "onvif://www.onvif.org/name/X" in result["raw_scopes"]

    def test_non_onvif_scopes_ignored(self) -> None:
        raw = b"http://something.else.com/foo\x00"
        result = self._parse(raw)
        assert result["name"] == ""
        assert result["hardware"] == ""
        assert result["profiles"] == []

    def test_empty_raw_returns_defaults(self) -> None:
        result = self._parse(b"")
        assert result["supported"] is True
        assert result["name"] == ""
        assert result["hardware"] == ""
        assert result["profiles"] == []
        assert result["raw_scopes"] == []

    def test_scope_without_slash_skipped(self) -> None:
        raw = b"onvif://www.onvif.org/name\x00"  # no value after /
        result = self._parse(raw)
        assert result["name"] == ""

    def test_url_encoded_plus_space(self) -> None:
        raw = b"onvif://www.onvif.org/name/Bosch+Camera\x00"
        result = self._parse(raw)
        # + is not treated as space by unquote (only %XX); "+" stays literal
        assert "Camera" in result["name"]


# ── _fetch_rcp_lan helper (async) ─────────────────────────────────────────────


class TestFetchRcpLan:
    """Tests for BoschCameraCoordinator._fetch_rcp_lan (async helper)."""

    def _make_coordinator(
        self,
        *,
        lan_ip: str | None = "192.0.2.149",
        local_creds: dict[str, Any] | None = None,
    ) -> Any:
        """Return a minimal coordinator instance for _fetch_rcp_lan."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # Build with the minimum required attributes
        coord = object.__new__(BoschCameraCoordinator)
        coord._rcp_lan_ip_cache = {CAM_ID: lan_ip} if lan_ip else {}
        coord._local_creds_cache = {CAM_ID: local_creds} if local_creds else {}
        coord.hass = MagicMock()

        def _get_cam_lan_ip(cam_id: str) -> str | None:
            ip = coord._rcp_lan_ip_cache.get(cam_id)
            if ip:
                return ip
            creds = coord._local_creds_cache.get(cam_id)
            return creds.get("host") if creds else None

        coord._get_cam_lan_ip = _get_cam_lan_ip  # type: ignore[method-assign]
        return coord

    @pytest.mark.asyncio
    async def test_returns_none_when_no_lan_ip(self) -> None:
        coord = self._make_coordinator(lan_ip=None, local_creds=None)
        result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_creds(self) -> None:
        coord = self._make_coordinator(lan_ip="192.0.2.149", local_creds=None)
        result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_creds_missing_user(self) -> None:
        creds: dict[str, Any] = {
            "user": "",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)
        result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_http_401(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_rcp_error_in_body(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=b"<err>0x0090</err>")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_parses_str_hex_payload(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        # Version bytes 1.2.38.150 = 01 02 26 96
        payload_hex = "01022696"
        rcp_xml = f"<rcp><str>{payload_hex}</str></rcp>".encode()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=rcp_xml)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result == bytes.fromhex(payload_hex)

    @pytest.mark.asyncio
    async def test_returns_none_on_timeout(self) -> None:
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                side_effect=TimeoutError(),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result is None

    @pytest.mark.asyncio
    async def test_fallback_raw_bytes_when_no_str_tag(self) -> None:
        """Non-XML binary payload falls through to raw bytes return."""
        creds: dict[str, Any] = {
            "user": "cbs-XYZ",
            "password": "pw",
            "host": "192.0.2.149",
            "port": 443,
            "ts": 0.0,
        }
        coord = self._make_coordinator(local_creds=creds)

        raw_bytes = b"\x01\x02\x26\x96"  # pure binary, no XML envelope

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.read = AsyncMock(return_value=raw_bytes)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(
                "custom_components.bosch_shc_camera.async_digest_request",
                return_value=mock_resp,
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=MagicMock(),
            ),
        ):
            result = await coord._fetch_rcp_lan(CAM_ID, "0xff00")
        assert result == raw_bytes


# ── _async_update_lan_diagnostic_sensors ─────────────────────────────────────


class TestAsyncUpdateLanDiagnosticSensors:
    """Tests for coordinator._async_update_lan_diagnostic_sensors."""

    def _make_coordinator_with_caches(self) -> Any:
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = object.__new__(BoschCameraCoordinator)
        coord._rcp_onvif_scopes_cache = {}
        coord._rcp_version_cache = {}
        coord._rcp_lan_ip_cache = {CAM_ID: "192.0.2.149"}
        coord._local_creds_cache = {
            CAM_ID: {
                "user": "cbs-XYZ",
                "password": "pw",
                "host": "192.0.2.149",
                "port": 443,
                "ts": 0.0,
            }
        }
        coord.hass = MagicMock()

        def _get_cam_lan_ip(cam_id: str) -> str | None:
            return coord._rcp_lan_ip_cache.get(cam_id)

        coord._get_cam_lan_ip = _get_cam_lan_ip  # type: ignore[method-assign]
        return coord

    @pytest.mark.asyncio
    async def test_f4_onvif_scopes_populated_on_success(self) -> None:
        coord = self._make_coordinator_with_caches()
        onvif_raw = b"onvif://www.onvif.org/name/TestCam\x00"

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0x0a98":
                return onvif_raw
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert CAM_ID in coord._rcp_onvif_scopes_cache
        assert coord._rcp_onvif_scopes_cache[CAM_ID]["name"] == "TestCam"

    @pytest.mark.asyncio
    async def test_f6_rcp_version_populated_on_success(self) -> None:
        coord = self._make_coordinator_with_caches()
        # Version 1.2.38.150 → bytes 0x01 0x02 0x26 0x96
        ver_raw = bytes([1, 2, 38, 150])

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0xff00":
                return ver_raw
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert coord._rcp_version_cache.get(CAM_ID) == "1.2.38.150"

    @pytest.mark.asyncio
    async def test_version_bytes_too_short_no_update(self) -> None:
        coord = self._make_coordinator_with_caches()

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0xff00":
                return b"\x01\x02"  # only 2 bytes — not 4
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert coord._rcp_version_cache.get(CAM_ID) is None

    @pytest.mark.asyncio
    async def test_onvif_none_does_not_update_cache(self) -> None:
        coord = self._make_coordinator_with_caches()

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            return None

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        assert CAM_ID not in coord._rcp_onvif_scopes_cache
        assert CAM_ID not in coord._rcp_version_cache

    @pytest.mark.asyncio
    async def test_exception_in_onvif_does_not_prevent_version_fetch(self) -> None:
        coord = self._make_coordinator_with_caches()
        ver_raw = bytes([1, 2, 9, 225])

        call_count = 0

        async def _fetch_mock(cam_id: str, opcode: str) -> bytes | None:
            nonlocal call_count
            call_count += 1
            if opcode == "0x0a98":
                raise RuntimeError("ONVIF fetch failed")
            return ver_raw

        coord._fetch_rcp_lan = _fetch_mock  # type: ignore[method-assign]
        # Should NOT raise — exception is swallowed per spec
        await coord._async_update_lan_diagnostic_sensors(CAM_ID)
        # Version should still be updated
        assert coord._rcp_version_cache.get(CAM_ID) == "1.2.9.225"
