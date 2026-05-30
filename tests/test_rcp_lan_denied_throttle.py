"""Regression test for Bug #2 (2026-05-26): RCP 0x0a98 returned HTTP 401 on
every slow-tier cycle (~5 min) for CBS users that lack permission for that
opcode. Production logs (7.5 h window) showed 124 unique 401s — pure noise,
no recovery, no backoff.

Fix: per-(cam_id, opcode_hex) denied cache. On 401, mark as denied for
`_RCP_LAN_DENIED_TTL` seconds (24 h); subsequent calls return None
immediately without a network request. On 200, clear the entry so a
permission change recovers automatically.

Pin-tests for every transition (PIN_EVERY_MODE).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator

CAM_A = "DEAD-BEEF-AAAA"
CAM_B = "DEAD-BEEF-BBBB"
OPCODE_DENIED = "0x0a98"
OPCODE_OTHER = "0xff00"


def _make_coordinator() -> BoschCameraCoordinator:
    coord = BoschCameraCoordinator.__new__(BoschCameraCoordinator)
    coord._rcp_lan_denied_until = {}
    return coord


class TestRcpLanDeniedHelpers:
    def test_not_denied_by_default(self) -> None:
        coord = _make_coordinator()
        assert coord._is_rcp_lan_denied(CAM_A, OPCODE_DENIED) is False

    def test_mark_then_is_denied(self) -> None:
        coord = _make_coordinator()
        coord._mark_rcp_lan_denied(CAM_A, OPCODE_DENIED)
        assert coord._is_rcp_lan_denied(CAM_A, OPCODE_DENIED) is True

    def test_denial_expires_after_ttl(self) -> None:
        coord = _make_coordinator()
        ttl = BoschCameraCoordinator._RCP_LAN_DENIED_TTL
        # Set an expired entry manually (in the past beyond TTL)
        coord._rcp_lan_denied_until[(CAM_A, OPCODE_DENIED)] = (
            time.monotonic() - ttl - 1.0
        )
        assert coord._is_rcp_lan_denied(CAM_A, OPCODE_DENIED) is False

    def test_denial_independent_per_opcode(self) -> None:
        coord = _make_coordinator()
        coord._mark_rcp_lan_denied(CAM_A, OPCODE_DENIED)
        assert coord._is_rcp_lan_denied(CAM_A, OPCODE_OTHER) is False

    def test_denial_independent_per_cam(self) -> None:
        coord = _make_coordinator()
        coord._mark_rcp_lan_denied(CAM_A, OPCODE_DENIED)
        assert coord._is_rcp_lan_denied(CAM_B, OPCODE_DENIED) is False

    def test_clear_removes_entry(self) -> None:
        coord = _make_coordinator()
        coord._mark_rcp_lan_denied(CAM_A, OPCODE_DENIED)
        coord._clear_rcp_lan_denied(CAM_A, OPCODE_DENIED)
        assert coord._is_rcp_lan_denied(CAM_A, OPCODE_DENIED) is False

    def test_clear_when_not_set_does_not_raise(self) -> None:
        coord = _make_coordinator()
        # Must not raise KeyError.
        coord._clear_rcp_lan_denied(CAM_A, OPCODE_DENIED)
        assert coord._is_rcp_lan_denied(CAM_A, OPCODE_DENIED) is False

    def test_ttl_constant_is_24h(self) -> None:
        """Pin: cache TTL is 24 h. If you change this, also update the
        commentary in `_fetch_rcp_lan` and the user docs."""
        assert BoschCameraCoordinator._RCP_LAN_DENIED_TTL == 86400.0
