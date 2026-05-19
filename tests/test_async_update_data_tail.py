"""Cover the post-data tail of `_async_update_data` (v12.4.7+).

Hits the v12.4.7/8/11 hooks that run after a successful cloud refresh:
- per-camera availability transition notifier (L2454-2457)
- persistent LAN-IP store save when the cache changed (L2464-2468)
- periodic background maintenance refresh (L2478-2486)
- cloud-state transition alert (L2493-2498)
- the UpdateFailed + TimeoutError + ClientError except branches (L2504-2515)

Also covers the HTTP 5xx camera-list branch (L1647, L1653): reactive
maintenance refresh + outage-ping sweep are kicked off before the
UpdateFailed propagates.

Reuses the canonical `_make_coord` / `_make_session` helpers from
`test_init_sprint_ka.py` so the stub stays identical to the rest of the
suite (avoids drift).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

from .test_init_sprint_ka import (
    _make_coord,
    _make_resp,
    _make_session,
    _PATCH_SESSION,
)


CAM_A = "11111111-1111-1111-1111-111111111111"


def _coord_with_tail_hooks(**overrides):
    """Second-tick coord wired with all the v12.4.7+ announcement helpers
    so the tail block actually executes."""
    coord = _make_coord(**overrides)
    coord._first_tick_done = True
    coord._async_maybe_announce_camera_status = AsyncMock(return_value=None)
    coord._compute_status_for = MagicMock(return_value="online")
    coord._async_refresh_maintenance = AsyncMock(return_value=None)
    coord._async_maybe_announce_cloud_state = AsyncMock(return_value=None)
    coord._async_outage_ping_all = AsyncMock(return_value=None)
    coord._MAINTENANCE_INTERVAL_S = 3600.0
    coord._maintenance_last_fetch = float("-inf")  # forces refresh on first tick
    coord._maintenance_cache = None
    # LAN-IP persistence
    coord._lan_ips_store = MagicMock()
    coord._lan_ips_store.async_save = AsyncMock(return_value=None)
    coord._lan_ips_snapshot = None  # no prior snapshot → save fires
    return coord


# ── Happy-path tail ───────────────────────────────────────────────────────


class TestAsyncUpdateDataHappyTail:
    @pytest.mark.asyncio
    async def test_tail_fires_announce_and_lan_persist_and_maint_and_cloud_alert(self):
        """On a healthy second-tick refresh with cameras + LAN-IPs cached,
        the post-data tail must:
          1. compute status + schedule camera-status announce per cam
          2. persist the LAN-IP snapshot via _lan_ips_store
          3. kick a periodic maintenance refresh (last_fetch == -inf)
          4. announce cloud-up via _async_maybe_announce_cloud_state(True)
        Pins L2454-2498 (the entire success tail)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _coord_with_tail_hooks()
        coord._rcp_lan_ip_cache = {CAM_A: "192.0.2.10"}

        session = _make_session({
            "v11/video_inputs": _make_resp(200, [{"id": CAM_A, "title": "Terrasse"}]),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            "ping": _make_resp(200, {}, text_data="ONLINE"),
        })

        with patch(_PATCH_SESSION, return_value=session):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict)
        assert CAM_A in result
        coord._async_maybe_announce_camera_status.assert_called()
        coord._compute_status_for.assert_called()
        # LAN-IP store fired with the new snapshot.
        coord._lan_ips_store.async_save.assert_called_once_with({CAM_A: "192.0.2.10"})
        # Maintenance refresh kicked (last_fetch was -inf, interval elapsed).
        coord._async_refresh_maintenance.assert_called_with(reactive=False)
        # Cloud-up announce fired with True.
        coord._async_maybe_announce_cloud_state.assert_called_with(True)

    @pytest.mark.asyncio
    async def test_tail_skips_lan_persist_when_snapshot_unchanged(self):
        """`_lan_ips_snapshot == current snapshot` → `async_save` is NOT
        called (throttle protects the disk from rewriting the same dict
        every tick). Pins the L2466 negative branch."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _coord_with_tail_hooks()
        coord._rcp_lan_ip_cache = {CAM_A: "192.0.2.10"}
        # Pretend the same snapshot was already written.
        coord._lan_ips_snapshot = {CAM_A: "192.0.2.10"}

        session = _make_session({
            "v11/video_inputs": _make_resp(200, [{"id": CAM_A, "title": "Terrasse"}]),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            "ping": _make_resp(200, {}, text_data="ONLINE"),
        })
        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._lan_ips_store.async_save.assert_not_called()

    @pytest.mark.asyncio
    async def test_tail_skips_maint_refresh_within_interval(self):
        """If `_maintenance_last_fetch` is fresh (just now), the periodic
        refresh is NOT scheduled — only reactive paths fire mid-interval."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _coord_with_tail_hooks()
        coord._rcp_lan_ip_cache = {}
        coord._maintenance_last_fetch = time.monotonic()  # very fresh

        session = _make_session({
            "v11/video_inputs": _make_resp(200, [{"id": CAM_A, "title": "Terrasse"}]),
            "feature_flags": _make_resp(200, {}),
            "protocol_support": _make_resp(200, {"state": "SUPPORTED"}),
            "ping": _make_resp(200, {}, text_data="ONLINE"),
        })
        with patch(_PATCH_SESSION, return_value=session):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_refresh_maintenance.assert_not_called()


# ── 503 reactive branch ───────────────────────────────────────────────────


class TestCloudFiveHundredTriggers:
    @pytest.mark.asyncio
    async def test_503_camera_list_kicks_reactive_maint_and_outage_ping(self):
        """A 503 on `/v11/video_inputs` triggers `_async_refresh_maintenance
        (reactive=True)` + `_async_outage_ping_all()` before `UpdateFailed`
        propagates. Pins L1647, L1653."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _coord_with_tail_hooks()
        coord._rcp_lan_ip_cache = {}

        session = _make_session({
            "v11/video_inputs": _make_resp(503, []),
            "feature_flags": _make_resp(200, {}),
        })

        with patch(_PATCH_SESSION, return_value=session), \
             pytest.raises(UpdateFailed):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_refresh_maintenance.assert_called_with(reactive=True)
        coord._async_outage_ping_all.assert_called_once()


# ── Outer except branches ─────────────────────────────────────────────────


class TestOuterExceptBranches:
    @pytest.mark.asyncio
    async def test_update_failed_propagates_and_fires_cloud_down_alert(self):
        """A 5xx in the camera-list flow raises UpdateFailed; the outer
        except block fires `_async_maybe_announce_cloud_state(False)` before
        re-raising. Pins L2504."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _coord_with_tail_hooks()
        session = _make_session({
            "v11/video_inputs": _make_resp(500, []),
            "feature_flags": _make_resp(200, {}),
        })
        with patch(_PATCH_SESSION, return_value=session), \
             pytest.raises(UpdateFailed):
            await BoschCameraCoordinator._async_update_data(coord)

        # cloud_state announced with False — the cloud is down.
        coord._async_maybe_announce_cloud_state.assert_called_with(False)

    @pytest.mark.asyncio
    async def test_asyncio_timeout_announces_cloud_down_and_kicks_outage_ping(self):
        """`asyncio.TimeoutError` from the cloud branch wraps to
        `UpdateFailed("Timeout fetching ...")` AND fires reactive maint
        refresh + outage ping + cloud_state(False). Pins L2509."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _coord_with_tail_hooks()

        # Make session.get raise TimeoutError synchronously to bubble out of
        # the `async with asyncio.timeout(15)` block via `__aexit__`.
        timeout_resp = MagicMock()
        timeout_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        timeout_resp.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=timeout_resp)

        with patch(_PATCH_SESSION, return_value=session), \
             pytest.raises(UpdateFailed, match="Timeout"):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_maybe_announce_cloud_state.assert_called_with(False)
        coord._async_outage_ping_all.assert_called_once()
        coord._async_refresh_maintenance.assert_called_with(reactive=True)

    @pytest.mark.asyncio
    async def test_aiohttp_client_error_announces_cloud_down(self):
        """A generic `aiohttp.ClientError` is wrapped to
        `UpdateFailed("Network error: ...")` with cloud-down announce.
        Pins the ClientError except branch."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _coord_with_tail_hooks()

        err_resp = MagicMock()
        err_resp.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("connreset"))
        err_resp.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=err_resp)

        with patch(_PATCH_SESSION, return_value=session), \
             pytest.raises(UpdateFailed, match="Network error"):
            await BoschCameraCoordinator._async_update_data(coord)

        coord._async_maybe_announce_cloud_state.assert_called_with(False)
