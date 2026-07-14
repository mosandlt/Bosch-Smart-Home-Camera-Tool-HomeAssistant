"""Tests for rcp.py — the coordinator-cache orchestration wrapper.

rcp.py used to contain the full RCP session-management/protocol/parser
implementation; that's all in `bosch_shc_camera_client.rcp` now (Core-
submission client-library extraction, see
knowledge-base/ha-core-submission-plan.md tasks #10/#11), with its own
212+ tests in that separate repo. This file now only tests
`async_update_rcp_data`, the thin wrapper that:
  1. builds ssl_context/session via cloud_ssl
  2. calls the library's `fetch_rcp_camera_data`
  3. merges the returned `RcpCameraData` fields into the coordinator's
     own 11 cache dicts (only non-None fields; None means "not read this
     round", not "clear the cache")

Mocks `bosch_shc_camera_client.rcp.fetch_rcp_camera_data` directly rather
than the deeper get_cached_rcp_session/rcp_read layer — the per-command
protocol behavior (XML-envelope guards, out-of-range values, 3-strikes
skip logic) is the library's own responsibility and is tested there.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bosch_shc_camera_client.rcp import RcpCameraData

MODULE = "custom_components.bosch_shc_camera.rcp"
CAM_ID = "11111111-1111-1111-1111-111111111111"
PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"


@pytest.fixture(autouse=True)
def _mock_cloud_ssl_helpers():
    """Every test mocks fetch_rcp_camera_data wholesale, so the real
    ssl_context/session values async_update_rcp_data builds via cloud_ssl
    are never used for a real request -- harmless doubles, same rationale
    as the source repo's own autouse fixture (see git history)."""
    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_ssl_context",
            AsyncMock(return_value=MagicMock()),
        ),
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            AsyncMock(return_value=MagicMock()),
        ),
    ):
        yield


def _make_coord(cam_id: str = CAM_ID) -> SimpleNamespace:
    """Minimal coordinator stub required by async_update_rcp_data."""
    coord = SimpleNamespace(
        hass=MagicMock(),
        _rcp_session_cache={},
        _rcp_session_locks={},
        _rcp_dimmer_cache={},
        _rcp_privacy_cache={},
        _rcp_clock_offset_cache={},
        _rcp_lan_ip_cache={},
        _rcp_product_name_cache={},
        _rcp_bitrate_cache={},
        _rcp_alarm_catalog_cache={},
        _rcp_motion_zones_cache={},
        _rcp_motion_coords_cache={},
        _rcp_tls_cert_cache={},
        _rcp_network_services_cache={},
        _rcp_iva_catalog_cache={},
        _rcp_cmd_failures={},
    )
    coord._rcp_cmd_failures[cam_id] = {}
    return coord


class TestAsyncUpdateRcpDataNoSession:
    """fetch_rcp_camera_data returning None (no RCP session) -> early return,
    zero cache writes, no exception."""

    @pytest.mark.asyncio
    async def test_none_result_touches_no_caches(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()

        with patch(
            f"{MODULE}.fetch_rcp_camera_data",
            AsyncMock(return_value=None),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_dimmer_cache == {}
        assert coord._rcp_privacy_cache == {}
        assert coord._rcp_clock_offset_cache == {}
        assert coord._rcp_lan_ip_cache == {}
        assert coord._rcp_product_name_cache == {}
        assert coord._rcp_bitrate_cache == {}
        assert coord._rcp_alarm_catalog_cache == {}
        assert coord._rcp_motion_zones_cache == {}
        assert coord._rcp_motion_coords_cache == {}
        assert coord._rcp_tls_cert_cache == {}
        assert coord._rcp_network_services_cache == {}
        assert coord._rcp_iva_catalog_cache == {}


class TestAsyncUpdateRcpDataFullyPopulated:
    """Every RcpCameraData field set -> every corresponding coordinator
    cache dict gets written under cam_id."""

    @pytest.mark.asyncio
    async def test_all_fields_merged_into_caches(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        data = RcpCameraData(
            dimmer=42,
            privacy=1,
            clock_offset=-1.5,
            lan_ip="192.0.2.149",
            product_name="HOME_Eyes_Outdoor_II",
            bitrate=[1000, 2000, 3000],
            alarm_catalog=[{"id": 0, "name": "Virtual Alarm 0", "type": "virtual"}],
            motion_zones=[{"zone_id": 0, "raw_hex": "00", "size": 28}],
            motion_coords=[{"x1": 0.0, "y1": 0.0, "x2": 50.0, "y2": 50.0}],
            tls_cert={"raw_size": 455},
            network_services=["rtsp", "rcp"],
            iva_catalog=[{"module_id": 1, "version": 2, "flags": 1, "active": True}],
        )

        with patch(
            f"{MODULE}.fetch_rcp_camera_data",
            AsyncMock(return_value=data),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_dimmer_cache[CAM_ID] == 42
        assert coord._rcp_privacy_cache[CAM_ID] == 1
        assert coord._rcp_clock_offset_cache[CAM_ID] == -1.5
        assert coord._rcp_lan_ip_cache[CAM_ID] == "192.0.2.149"
        assert coord._rcp_product_name_cache[CAM_ID] == "HOME_Eyes_Outdoor_II"
        assert coord._rcp_bitrate_cache[CAM_ID] == [1000, 2000, 3000]
        assert coord._rcp_alarm_catalog_cache[CAM_ID] == data.alarm_catalog
        assert coord._rcp_motion_zones_cache[CAM_ID] == data.motion_zones
        assert coord._rcp_motion_coords_cache[CAM_ID] == data.motion_coords
        assert coord._rcp_tls_cert_cache[CAM_ID] == data.tls_cert
        assert coord._rcp_network_services_cache[CAM_ID] == data.network_services
        assert coord._rcp_iva_catalog_cache[CAM_ID] == data.iva_catalog


class TestAsyncUpdateRcpDataPartiallyPopulated:
    """Only some RcpCameraData fields set -> only those cache dicts are
    written; unset (None) fields leave the corresponding cache untouched
    (None means 'not read this round', not 'clear the value')."""

    @pytest.mark.asyncio
    async def test_only_non_none_fields_written(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        # Pre-existing cached value for a field this round did NOT read.
        coord._rcp_lan_ip_cache[CAM_ID] = "10.0.0.5"

        data = RcpCameraData(dimmer=50, privacy=0)  # everything else None

        with patch(
            f"{MODULE}.fetch_rcp_camera_data",
            AsyncMock(return_value=data),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_dimmer_cache[CAM_ID] == 50
        assert coord._rcp_privacy_cache[CAM_ID] == 0
        # Untouched fields: no new entry, and the pre-existing one survives.
        assert coord._rcp_clock_offset_cache == {}
        assert coord._rcp_lan_ip_cache[CAM_ID] == "10.0.0.5"
        assert coord._rcp_product_name_cache == {}
        assert coord._rcp_bitrate_cache == {}
        assert coord._rcp_alarm_catalog_cache == {}
        assert coord._rcp_motion_zones_cache == {}
        assert coord._rcp_motion_coords_cache == {}
        assert coord._rcp_tls_cert_cache == {}
        assert coord._rcp_network_services_cache == {}
        assert coord._rcp_iva_catalog_cache == {}

    @pytest.mark.asyncio
    async def test_privacy_zero_is_not_treated_as_falsy_none(self):
        """0 is a valid privacy/dimmer value -- must not be skipped by an
        `if data.field:` truthiness check instead of `is not None`."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        data = RcpCameraData(dimmer=0, privacy=0)

        with patch(
            f"{MODULE}.fetch_rcp_camera_data",
            AsyncMock(return_value=data),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_dimmer_cache[CAM_ID] == 0
        assert coord._rcp_privacy_cache[CAM_ID] == 0


class TestAsyncUpdateRcpDataCallArguments:
    """async_update_rcp_data passes the right objects to fetch_rcp_camera_data:
    a real session/ssl_context (from cloud_ssl), the coordinator's own
    session_cache/session_locks dicts (shared by reference, not copied), and
    a per-cam_id slice of the failure-counter dict."""

    @pytest.mark.asyncio
    async def test_arguments_passed_through_correctly(self):
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        mock_fetch = AsyncMock(return_value=None)

        with patch(f"{MODULE}.fetch_rcp_camera_data", mock_fetch):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        mock_fetch.assert_awaited_once()
        args = mock_fetch.await_args.args
        # (session, ssl_context, session_cache, session_locks, cmd_failures,
        #  cam_id, proxy_host, proxy_hash)
        assert args[2] is coord._rcp_session_cache
        assert args[3] is coord._rcp_session_locks
        assert args[4] is coord._rcp_cmd_failures[CAM_ID]
        assert args[5] == CAM_ID
        assert args[6] == PROXY_HOST
        assert args[7] == PROXY_HASH

    @pytest.mark.asyncio
    async def test_missing_cmd_failures_entry_is_created(self):
        """First-ever call for a cam_id with no pre-existing failure-counter
        entry -- getattr/.setdefault must create it, not raise KeyError."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = _make_coord()
        coord._rcp_cmd_failures = {}  # no entry for CAM_ID at all yet

        with patch(
            f"{MODULE}.fetch_rcp_camera_data",
            AsyncMock(return_value=None),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_cmd_failures[CAM_ID] == {}
