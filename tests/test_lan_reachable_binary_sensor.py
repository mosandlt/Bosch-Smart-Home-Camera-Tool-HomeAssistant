"""Coverage tests for `BoschLanReachableBinarySensor` (v12.4.10).

The sensor surfaces the coordinator's LAN-ping cache to automations and the
overview-card LAN tiles. The base class is exercised heavily by
`test_binary_sensors.py`; these pins focus on the unique-to-this-class
behaviour: always-available, None-pass-through, grace-period attributes.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord():
    coord = SimpleNamespace()
    coord.data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": []}}
    coord._lan_tcp_reachable = {}
    coord._local_write_at = {}
    coord._LOCAL_WRITE_GRACE_S = 30.0
    coord.is_lan_reachable = lambda _cid: None
    return coord


@pytest.fixture
def stub_entry():
    return SimpleNamespace(entry_id="01ENTRY", data={}, options={})


def _make(stub_coord, stub_entry):
    from custom_components.bosch_shc_camera.binary_sensor import (
        BoschLanReachableBinarySensor,
    )

    return BoschLanReachableBinarySensor(stub_coord, CAM_ID, stub_entry)


class TestAvailable:
    def test_always_returns_true(self, stub_coord, stub_entry):
        """The sensor must stay readable while the Bosch cloud is down —
        that is the entire reason it exists."""
        s = _make(stub_coord, stub_entry)
        assert s.available is True

    def test_available_even_when_coordinator_last_update_success_false(
        self,
        stub_coord,
        stub_entry,
    ):
        stub_coord.last_update_success = False
        s = _make(stub_coord, stub_entry)
        assert s.available is True


class TestIsOn:
    def test_returns_none_when_coordinator_helper_missing(self, stub_coord, stub_entry):
        """Stub coordinators in older tests may lack `is_lan_reachable` —
        getattr fallback returns None instead of raising."""
        del stub_coord.is_lan_reachable
        s = _make(stub_coord, stub_entry)
        assert s.is_on is None

    def test_returns_none_when_helper_returns_none(self, stub_coord, stub_entry):
        stub_coord.is_lan_reachable = lambda _cid: None
        s = _make(stub_coord, stub_entry)
        assert s.is_on is None

    def test_returns_true_when_helper_returns_true(self, stub_coord, stub_entry):
        stub_coord.is_lan_reachable = lambda _cid: True
        s = _make(stub_coord, stub_entry)
        assert s.is_on is True

    def test_returns_false_when_helper_returns_false(self, stub_coord, stub_entry):
        stub_coord.is_lan_reachable = lambda _cid: False
        s = _make(stub_coord, stub_entry)
        assert s.is_on is False


class TestExtraStateAttributes:
    def test_minimal_attrs_when_cache_empty(self, stub_coord, stub_entry):
        s = _make(stub_coord, stub_entry)
        attrs = s.extra_state_attributes
        assert attrs == {"camera_id": CAM_ID}

    def test_adds_last_check_seconds_ago(self, stub_coord, stub_entry):
        # cache populated 10s ago (current monotonic - 10)
        with patch("time.monotonic", return_value=1010.0):
            stub_coord._lan_tcp_reachable[CAM_ID] = (True, 1000.0)
            s = _make(stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        assert attrs["camera_id"] == CAM_ID
        assert attrs["last_check_seconds_ago"] == 10

    def test_adds_write_grace_when_inside_window(self, stub_coord, stub_entry):
        with patch("time.monotonic", return_value=1010.0):
            stub_coord._local_write_at[CAM_ID] = 1000.0  # 10s ago, inside 30s grace
            s = _make(stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        # grace_left = 30 - 10 = 20s
        assert attrs.get("write_grace_seconds_left") == 20

    def test_no_write_grace_when_outside_window(self, stub_coord, stub_entry):
        with patch("time.monotonic", return_value=1100.0):
            stub_coord._local_write_at[CAM_ID] = 1000.0  # 100s ago, outside 30s grace
            s = _make(stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        assert "write_grace_seconds_left" not in attrs

    def test_no_grace_when_coordinator_lacks_local_write_at(
        self, stub_coord, stub_entry
    ):
        """Belt-and-braces guard for stub coordinators in legacy tests."""
        del stub_coord._local_write_at
        s = _make(stub_coord, stub_entry)
        attrs = s.extra_state_attributes
        assert "write_grace_seconds_left" not in attrs

    def test_combines_last_check_and_grace_when_both_set(self, stub_coord, stub_entry):
        with patch("time.monotonic", return_value=1010.0):
            stub_coord._lan_tcp_reachable[CAM_ID] = (True, 1005.0)
            stub_coord._local_write_at[CAM_ID] = 1000.0
            s = _make(stub_coord, stub_entry)
            attrs = s.extra_state_attributes
        assert attrs["last_check_seconds_ago"] == 5
        assert attrs["write_grace_seconds_left"] == 20
        assert attrs["camera_id"] == CAM_ID

    def test_volatile_attrs_are_unrecorded(self, stub_coord, stub_entry):
        """HA#39: both freshness fields change every tick → exclude them from
        the recorder so `state_attributes` does not bloat. They are still
        emitted live (asserted above); only their recording is suppressed."""
        s = _make(stub_coord, stub_entry)
        assert "last_check_seconds_ago" in s._unrecorded_attributes
        assert "write_grace_seconds_left" in s._unrecorded_attributes
