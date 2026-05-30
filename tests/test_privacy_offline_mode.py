"""End-to-end pins for the privacy switch offline-mode contract (v12.4.10+).

After today's bug — switch unavailable on a cold start during cloud-503
because the `_shc_state_cache` was empty even though LAN was reachable —
these tests pin the new availability semantics:

  - cloud healthy + cached state → available (primary)
  - cloud down + cached state + Gen2 + LAN reachable → available (fallback)
  - cloud down + NO cached state + Gen2 + LAN reachable → available
    (cold-start case the user hit; pre-v12.4.10 fix returned False)
  - cloud down + Gen1 → unavailable (no RCP-write path on Gen1)
  - cloud down + LAN unreachable → unavailable
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(
    *,
    last_update_success: bool = True,
    has_cached_state: bool = True,
    is_lan_reachable_value=None,
    gen2: bool = True,
) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.last_update_success = last_update_success
    coord._shc_state_cache = (
        {CAM_ID: {"privacy_mode": False}} if has_cached_state else {CAM_ID: {}}
    )
    coord._rcp_privacy_cache = {}
    coord.data = {
        CAM_ID: {
            "info": {
                "title": "Terrasse",
                "hardwareVersion": "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR",
            }
        }
    }
    coord._hw_version = {CAM_ID: "HOME_Eyes_Outdoor" if gen2 else "OUTDOOR"}
    if is_lan_reachable_value is None:
        # No helper attached at all — simulates pre-v12.4.10 stub.
        pass
    else:
        coord.is_lan_reachable = lambda _cid, _v=is_lan_reachable_value: _v
    return coord


def _make_switch(coord) -> object:
    from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

    entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
    return BoschPrivacyModeSwitch(coord, CAM_ID, entry)


class TestPrivacySwitchOfflineMode:
    def test_cloud_healthy_with_state_is_available(self):
        coord = _make_coord(last_update_success=True, has_cached_state=True)
        s = _make_switch(coord)
        assert s.available is True

    def test_cloud_healthy_without_state_is_unavailable(self):
        """Before a single coordinator tick succeeded, we have no signal at all."""
        coord = _make_coord(last_update_success=True, has_cached_state=False)
        s = _make_switch(coord)
        assert s.available is False

    def test_cloud_down_cached_state_gen2_lan_reachable_is_available(self):
        """The pre-v12.4.10 fallback path — still must keep working."""
        coord = _make_coord(
            last_update_success=False,
            has_cached_state=True,
            is_lan_reachable_value=True,
            gen2=True,
        )
        s = _make_switch(coord)
        assert s.available is True

    def test_cloud_down_no_cached_state_gen2_lan_reachable_is_available(self):
        """v12.4.10 fix — cold-start during cloud-503 with no cached state
        but a reachable LAN must keep the switch toggleable. is_on returns
        None (HA renders 'unknown'), but the user can still flip it."""
        coord = _make_coord(
            last_update_success=False,
            has_cached_state=False,
            is_lan_reachable_value=True,
            gen2=True,
        )
        s = _make_switch(coord)
        assert s.available is True
        # is_on returns None in this state (no cached value, no live data).
        assert s.is_on is None

    def test_cloud_down_gen1_is_unavailable(self):
        """Gen1 cameras have no RCP-write path → fallback is N/A."""
        coord = _make_coord(
            last_update_success=False,
            has_cached_state=False,
            is_lan_reachable_value=True,
            gen2=False,
        )
        s = _make_switch(coord)
        assert s.available is False

    def test_cloud_down_lan_unreachable_is_unavailable(self):
        coord = _make_coord(
            last_update_success=False,
            has_cached_state=True,
            is_lan_reachable_value=False,
            gen2=True,
        )
        s = _make_switch(coord)
        assert s.available is False

    def test_cloud_down_lan_unknown_is_unavailable(self):
        """is_lan_reachable returning None (no ping yet) — treat as unavailable."""
        coord = _make_coord(
            last_update_success=False,
            has_cached_state=True,
            is_lan_reachable_value=None,
            gen2=True,
        )
        # Force the helper to be present but return None.
        coord.is_lan_reachable = lambda _cid: None
        s = _make_switch(coord)
        assert s.available is False

    def test_no_is_lan_reachable_helper_returns_false(self):
        """Stub coordinators in older tests have no is_lan_reachable method.
        Must not crash — return False instead."""
        coord = _make_coord(
            last_update_success=False,
            has_cached_state=True,
            gen2=True,
        )
        # No is_lan_reachable attached at all.
        s = _make_switch(coord)
        assert s.available is False
