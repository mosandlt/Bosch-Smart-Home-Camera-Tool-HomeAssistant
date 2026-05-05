"""Tests for synchronous coordinator helper methods.

These methods are pure-state queries (no I/O, no async) — they read from
internal dicts and return bool/values. Highest test ROI on the
coordinator since they're called dozens of times per coordinator tick
from every entity's `available` and `is_on` properties.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest


# Tested helpers (all sync, all read-only):
#   is_camera_online(cam_id)
#   is_session_stale(cam_id)
#   is_stream_warming(cam_id)
#   debug  (property)
#   token  (property)
#   refresh_token  (property)
#   options  (property)
#
# Approach: build a tiny stub coordinator that exposes the fields each
# helper reads. Avoids the cost + complexity of a full coordinator init
# (would need OAuth + cloud + SHC + FCM mocks).


class _StubCoord(SimpleNamespace):
    """Provides just the attributes each helper touches."""


def _make_coord(**overrides) -> _StubCoord:
    base = dict(
        data={},
        _session_stale={},
        _stream_warming=set(),
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-AAA", "refresh_token": "rfr-BBB"},
            options={"debug": False, "scan_interval": 60},
        ),
        _refreshed_token=None,
        _refreshed_refresh=None,
    )
    base.update(overrides)
    return _StubCoord(**base)


# ── Bind real methods from the module to the stub ────────────────────────


@pytest.fixture
def bind_helpers():
    """Return functions that emulate the bound methods on a stub coord."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    return {
        "is_camera_online":  BoschCameraCoordinator.is_camera_online,
        "is_session_stale":  BoschCameraCoordinator.is_session_stale,
    }


# ── is_camera_online ──────────────────────────────────────────────────────


def test_is_camera_online_returns_true_for_ONLINE(bind_helpers) -> None:
    coord = _make_coord(data={"cam-A": {"status": "ONLINE"}})
    assert bind_helpers["is_camera_online"](coord, "cam-A") is True


def test_is_camera_online_returns_false_for_OFFLINE(bind_helpers) -> None:
    coord = _make_coord(data={"cam-A": {"status": "OFFLINE"}})
    assert bind_helpers["is_camera_online"](coord, "cam-A") is False


def test_is_camera_online_false_for_unknown_status(bind_helpers) -> None:
    """UPDATING_REGULAR, UNKNOWN, missing — all return False."""
    coord = _make_coord(data={"cam-A": {"status": "UPDATING_REGULAR"}})
    assert bind_helpers["is_camera_online"](coord, "cam-A") is False


def test_is_camera_online_missing_camera_returns_false(bind_helpers) -> None:
    """A cam_id not in coordinator.data must return False, not raise."""
    coord = _make_coord(data={})
    assert bind_helpers["is_camera_online"](coord, "cam-MISSING") is False


def test_is_camera_online_missing_status_field(bind_helpers) -> None:
    """Camera entry without 'status' field returns False (defaults to UNKNOWN)."""
    coord = _make_coord(data={"cam-A": {"info": {"title": "x"}}})
    assert bind_helpers["is_camera_online"](coord, "cam-A") is False


# ── is_session_stale ──────────────────────────────────────────────────────


def test_is_session_stale_default_false(bind_helpers) -> None:
    """No entry in `_session_stale` → not stale."""
    coord = _make_coord()
    assert bind_helpers["is_session_stale"](coord, "cam-A") is False


def test_is_session_stale_true_when_marked(bind_helpers) -> None:
    coord = _make_coord(_session_stale={"cam-A": True})
    assert bind_helpers["is_session_stale"](coord, "cam-A") is True


def test_is_session_stale_false_when_explicit_false(bind_helpers) -> None:
    coord = _make_coord(_session_stale={"cam-A": False})
    assert bind_helpers["is_session_stale"](coord, "cam-A") is False


# ── token / refresh_token property contracts ─────────────────────────────


def test_token_property_returns_entry_data() -> None:
    """coord.token reads bearer_token from entry.data when no in-memory override."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = _make_coord()
    assert BoschCameraCoordinator.token.fget(coord) == "tok-AAA"


def test_token_property_prefers_in_memory_refresh() -> None:
    """If a fresh token was just minted, use it instead of stale entry.data."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = _make_coord(_refreshed_token="tok-FRESH")
    assert BoschCameraCoordinator.token.fget(coord) == "tok-FRESH"


def test_token_property_falls_back_when_in_memory_empty() -> None:
    """Empty/None _refreshed_token must NOT shadow entry.data."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = _make_coord(_refreshed_token=None)
    assert BoschCameraCoordinator.token.fget(coord) == "tok-AAA"
    coord2 = _make_coord(_refreshed_token="")
    assert BoschCameraCoordinator.token.fget(coord2) == "tok-AAA"


def test_refresh_token_property_returns_entry_data() -> None:
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = _make_coord()
    assert BoschCameraCoordinator.refresh_token.fget(coord) == "rfr-BBB"


def test_refresh_token_in_memory_override() -> None:
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = _make_coord(_refreshed_refresh="rfr-FRESH")
    assert BoschCameraCoordinator.refresh_token.fget(coord) == "rfr-FRESH"


# ── options property ─────────────────────────────────────────────────────


def test_options_property_merges_defaults() -> None:
    """coord.options returns DEFAULT_OPTIONS overlaid by entry.options."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
    coord = _make_coord()
    coord._entry.options = {"interval_status": 999}
    opts = BoschCameraCoordinator.options.fget(coord)
    assert opts["interval_status"] == 999
    # Default keys must still be present
    for key in DEFAULT_OPTIONS:
        assert key in opts


def test_debug_default_off() -> None:
    """debug follows the entry option; default is False."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = _make_coord()
    assert BoschCameraCoordinator.debug.fget(coord) is False


def test_debug_on_when_option_true() -> None:
    """The option key is `debug_logging` even though the property is `debug`."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    coord = _make_coord()
    coord._entry.options = {"debug_logging": True}
    assert BoschCameraCoordinator.debug.fget(coord) is True
