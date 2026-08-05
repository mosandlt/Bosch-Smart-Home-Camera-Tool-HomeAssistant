"""Regression tests for rcp_diagnostics.py — LAN-sourced RCP diagnostic
sensors (ONVIF scopes, RCP version) + cache getters, extracted out of
coordinator.py (structural cleanup toward Platinum quality_scale).

Tests call the module functions directly with a lightweight stub
(SimpleNamespace) standing in for the coordinator, mirroring
test_quality_prefs.py's/test_rcp_client.py's convention.
`_async_update_lan_diagnostic_sensors` originally called `self._fetch_rcp_lan`
(a coordinator method, itself now backed by rcp_client.py) — the stub binds
an instance-level `_fetch_rcp_lan` so a test overriding it (AsyncMock) is
honored by the callee exactly like coordinator.py's real delegating stub.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.bosch_shc_camera import rcp_diagnostics

CAM_A = "cam-a"
CAM_B = "cam-b"


def _err_str(err: BaseException) -> str:
    return str(err) or type(err).__name__


def _make_coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "rcp_onvif_scopes_cache": {},
        "rcp_version_cache": {},
        "rcp_clock_offset_cache": {},
        "rcp_lan_ip_cache": {},
        "rcp_product_name_cache": {},
        "rcp_bitrate_cache": {},
        "err_str": _err_str,
    }
    base.update(overrides)
    coord = SimpleNamespace(**base)
    coord._fetch_rcp_lan = AsyncMock(return_value=None)  # type: ignore[attr-defined]
    return coord


class TestAsyncUpdateLanDiagnosticSensors:
    @pytest.mark.asyncio
    async def test_populates_onvif_scopes_on_success(self) -> None:
        coord = _make_coord()
        onvif_raw = b"onvif://www.onvif.org/hardware/HOME_Eyes_Outdoor\x00"
        version_raw = bytes([9, 40, 102, 0])

        async def _fetch(cam_id: str, opcode: str) -> bytes | None:
            return onvif_raw if opcode == "0x0a98" else version_raw

        coord._fetch_rcp_lan = _fetch
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(coord, CAM_A)
        assert coord.rcp_onvif_scopes_cache[CAM_A]["hardware"] == "HOME_Eyes_Outdoor"

    @pytest.mark.asyncio
    async def test_populates_rcp_version_on_success(self) -> None:
        coord = _make_coord()

        async def _fetch(cam_id: str, opcode: str) -> bytes | None:
            return bytes([9, 40, 102, 0]) if opcode == "0xff00" else None

        coord._fetch_rcp_lan = _fetch
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(coord, CAM_A)
        assert coord.rcp_version_cache[CAM_A] == "9.40.102.0"

    @pytest.mark.asyncio
    async def test_no_raw_onvif_leaves_cache_untouched(self) -> None:
        coord = _make_coord(rcp_onvif_scopes_cache={})
        coord._fetch_rcp_lan = AsyncMock(return_value=None)
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(coord, CAM_A)
        assert coord.rcp_onvif_scopes_cache == {}

    @pytest.mark.asyncio
    async def test_short_version_payload_not_cached(self) -> None:
        """Fewer than 4 bytes for the RCP-version response — too short to
        unpack — must not populate (and not raise)."""
        coord = _make_coord()

        async def _fetch(cam_id: str, opcode: str) -> bytes | None:
            return b"\x09\x28" if opcode == "0xff00" else None

        coord._fetch_rcp_lan = _fetch
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(coord, CAM_A)
        assert CAM_A not in coord.rcp_version_cache

    @pytest.mark.asyncio
    async def test_onvif_fetch_raising_is_swallowed(self) -> None:
        coord = _make_coord()

        async def _raise_for_onvif(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0x0a98":
                raise RuntimeError("boom")
            return None

        coord._fetch_rcp_lan = _raise_for_onvif
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(
            coord, CAM_A
        )  # must not raise
        assert CAM_A not in coord.rcp_onvif_scopes_cache

    @pytest.mark.asyncio
    async def test_version_fetch_raising_is_swallowed(self) -> None:
        coord = _make_coord()

        async def _raise_for_version(cam_id: str, opcode: str) -> bytes | None:
            if opcode == "0xff00":
                raise RuntimeError("boom")
            return None

        coord._fetch_rcp_lan = _raise_for_version
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(
            coord, CAM_A
        )  # must not raise
        assert CAM_A not in coord.rcp_version_cache

    @pytest.mark.asyncio
    async def test_calls_through_coordinator_instance_not_module_function(self) -> None:
        """Virtual-dispatch guard: an instance-level override of `_fetch_rcp_lan`
        (as coordinator.py's real delegating stub installs) must be the only
        thing called — never `rcp_client._fetch_rcp_lan` directly."""
        coord = _make_coord()
        coord._fetch_rcp_lan = AsyncMock(return_value=None)
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(coord, CAM_A)
        assert coord._fetch_rcp_lan.await_count == 2
        coord._fetch_rcp_lan.assert_any_await(CAM_A, "0x0a98")
        coord._fetch_rcp_lan.assert_any_await(CAM_A, "0xff00")

    @pytest.mark.asyncio
    async def test_does_not_affect_other_cameras_caches(self) -> None:
        coord = _make_coord(
            rcp_onvif_scopes_cache={CAM_B: {"name": "other"}},
            rcp_version_cache={CAM_B: "1.0.0.0"},
        )
        coord._fetch_rcp_lan = AsyncMock(return_value=None)
        await rcp_diagnostics._async_update_lan_diagnostic_sensors(coord, CAM_A)
        assert coord.rcp_onvif_scopes_cache == {CAM_B: {"name": "other"}}
        assert coord.rcp_version_cache == {CAM_B: "1.0.0.0"}


class TestClockOffset:
    def test_returns_cached_value(self) -> None:
        coord = _make_coord(rcp_clock_offset_cache={CAM_A: -2.5})
        assert rcp_diagnostics.clock_offset(coord, CAM_A) == -2.5

    def test_missing_camera_returns_none(self) -> None:
        coord = _make_coord(rcp_clock_offset_cache={CAM_A: -2.5})
        assert rcp_diagnostics.clock_offset(coord, "unknown-cam") is None


class TestRcpLanIp:
    def test_returns_cached_value(self) -> None:
        coord = _make_coord(rcp_lan_ip_cache={CAM_A: "192.168.1.50"})
        assert rcp_diagnostics.rcp_lan_ip(coord, CAM_A) == "192.168.1.50"

    def test_missing_camera_returns_none(self) -> None:
        coord = _make_coord(rcp_lan_ip_cache={CAM_A: "192.168.1.50"})
        assert rcp_diagnostics.rcp_lan_ip(coord, "unknown-cam") is None


class TestRcpProductName:
    def test_returns_cached_value(self) -> None:
        coord = _make_coord(rcp_product_name_cache={CAM_A: "HOME_Eyes_Outdoor"})
        assert rcp_diagnostics.rcp_product_name(coord, CAM_A) == "HOME_Eyes_Outdoor"

    def test_missing_camera_returns_none(self) -> None:
        coord = _make_coord(rcp_product_name_cache={CAM_A: "HOME_Eyes_Outdoor"})
        assert rcp_diagnostics.rcp_product_name(coord, "unknown-cam") is None


class TestRcpBitrateLadder:
    def test_returns_cached_list(self) -> None:
        coord = _make_coord(rcp_bitrate_cache={CAM_A: [512, 1024, 2048, 4096]})
        assert rcp_diagnostics.rcp_bitrate_ladder(coord, CAM_A) == [
            512,
            1024,
            2048,
            4096,
        ]

    def test_missing_camera_returns_empty_list(self) -> None:
        coord = _make_coord(rcp_bitrate_cache={CAM_A: [512]})
        assert rcp_diagnostics.rcp_bitrate_ladder(coord, "unknown-cam") == []
