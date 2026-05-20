"""Regression tests for the v12.4.13 LAN-fallback hardening.

Surfaced during the Bosch cloud maintenance window 2026-05-20: the
Innenbereich + Terrasse Privacy switches were unavailable for the full
outage. Root-cause inventory:

  1. `rcp_local_write` opened plain HTTP on port 80; cameras only listen
     on HTTPS port 443. Every LAN-fallback write failed with connection-
     refused (HTTP 000).
  2. The switch / light `available` property required `_is_gen2()`, which
     defaults to False when `_hw_version[cam_id]` is empty. After a cold
     restart during a cloud outage the cache is empty, so even Gen2 cams
     showed `unavailable`.
  3. `_hw_version` was not persisted, so a cold-restart-during-outage
     could never repopulate it.
  4. LOCAL Digest creds (`_local_creds_cache`) were not persisted, so
     even with everything else fixed the camera responded `<err>` to
     anonymous writes.

These tests pin the contracts that survived the fix.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── 1. rcp_local_write uses HTTPS + Digest when creds passed ─────────────


class TestRcpLocalWriteTransport:
    """Pin that `rcp_local_write` issues HTTPS (not HTTP) and uses
    `async_digest_request` when user+password are supplied."""

    @pytest.mark.asyncio
    async def test_url_is_https_when_creds_supplied(self):
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        observed_url: list[str] = []

        class _FakeResp:
            status = 200
            async def read(self):
                return b"<rcp><payload>00</payload></rcp>"
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False

        async def _fake_digest_request(session, method, url, user, password, **_):
            observed_url.append(url)
            return _FakeResp()

        with patch("custom_components.bosch_shc_camera.auth_utils.async_digest_request",
                   side_effect=_fake_digest_request):
            with patch("custom_components.bosch_shc_camera.rcp.async_get_clientsession",
                       return_value=MagicMock()):
                ok = await rcp_local_write(
                    MagicMock(), "192.0.2.149", "0x0d00",
                    "00010000", "P_OCTET",
                    user="cbs-xxx", password="secret",
                )

        assert ok is True
        assert observed_url, "async_digest_request was not invoked"
        assert observed_url[0].startswith("https://"), (
            f"REGRESSION: rcp_local_write opened {observed_url[0]} — must be "
            "HTTPS so the camera (port 443, no port 80 listener) accepts it."
        )
        assert "192.0.2.149/rcp.xml" in observed_url[0]

    @pytest.mark.asyncio
    async def test_no_digest_when_creds_missing(self):
        """Anonymous fallback path still issues HTTPS, just no auth."""
        from custom_components.bosch_shc_camera.rcp import rcp_local_write

        observed_url: list[str] = []

        class _FakeResp:
            status = 200
            async def read(self):
                return b"<rcp><payload>00</payload></rcp>"
            async def __aenter__(self): return self
            async def __aexit__(self, *_): return False

        class _FakeSession:
            def get(self, url, **kwargs):
                observed_url.append(url)
                return _FakeResp()

        with patch("custom_components.bosch_shc_camera.rcp.async_get_clientsession",
                   return_value=_FakeSession()):
            await rcp_local_write(
                MagicMock(), "192.0.2.149", "0x0d00",
                "00010000", "P_OCTET",
            )

        assert observed_url
        assert observed_url[0].startswith("https://"), (
            "REGRESSION: anonymous path still emitted HTTP — should be HTTPS."
        )


# ── 2. Privacy switch availability accepts hw_unknown ────────────────────


class TestPrivacyAvailableWhenHwUnknown:
    """`BoschPrivacyModeSwitch.available` must return True when the camera
    is LAN-reachable + hw_version is unknown (cold-start cloud-outage)."""

    def test_available_when_hw_unknown_and_lan_reachable(self):
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        coord = SimpleNamespace(
            last_update_success=False,
            _shc_state_cache={},
            _hw_version={},  # empty — cold start
            is_lan_reachable=lambda cid: True,
            is_camera_online=lambda cid: True,
        )
        sw = SimpleNamespace(coordinator=coord, _cam_id=CAM_ID)
        # Wire the super().available chain — for the test we treat super
        # as always-true (entity not in registry-suspended state).
        with patch.object(
            BoschPrivacyModeSwitch.__bases__[0], "available",
            new_callable=lambda: property(lambda _self: True),
        ):
            result = BoschPrivacyModeSwitch.available.fget(sw)
        assert result is True

    def test_unavailable_when_gen1_known_and_cloud_down(self):
        """If we KNOW it's Gen1, deny — no LAN RCP endpoint."""
        from custom_components.bosch_shc_camera.switch import BoschPrivacyModeSwitch

        coord = SimpleNamespace(
            last_update_success=False,
            _shc_state_cache={},
            _hw_version={CAM_ID: "CAMERA_EYES"},  # Gen1
            is_lan_reachable=lambda cid: True,
            is_camera_online=lambda cid: True,
        )
        sw = SimpleNamespace(coordinator=coord, _cam_id=CAM_ID)
        with patch.object(
            BoschPrivacyModeSwitch.__bases__[0], "available",
            new_callable=lambda: property(lambda _self: True),
        ):
            result = BoschPrivacyModeSwitch.available.fget(sw)
        assert result is False, (
            "REGRESSION: Gen1 cam shows available during cloud outage. "
            "LAN RCP is Gen2-only — Gen1 has no rcp.xml endpoint."
        )


# ── 3. shc.py LAN-fallback fires for hw_unknown ──────────────────────────


class TestShcLanFallbackFiresForUnknownHw:
    """`async_cloud_set_privacy_mode` must attempt the LAN-fallback even
    when `_is_gen2()` returns False due to empty `_hw_version` cache
    (cold-start during cloud outage)."""

    @pytest.mark.asyncio
    async def test_lan_fallback_fires_with_unknown_hw(self):
        """Source-grep: the privacy fallback gate must reference both
        `_is_gen2` and the hw-unknown sentinel set."""
        import inspect
        from custom_components.bosch_shc_camera import shc

        src = inspect.getsource(shc.async_cloud_set_privacy_mode)
        # The fix uses `_hw in (None, "", "CAMERA")` as the "unknown" hint.
        assert "CAMERA" in src and "_hw" in src, (
            "REGRESSION: async_cloud_set_privacy_mode no longer references "
            "the hw-unknown sentinel — cold-start LAN fallback will fail."
        )

    @pytest.mark.asyncio
    async def test_light_lan_fallback_includes_unknown_hw(self):
        import inspect
        from custom_components.bosch_shc_camera import shc

        src = inspect.getsource(shc.async_cloud_set_light_component)
        assert "_hw_light" in src and "CAMERA" in src, (
            "REGRESSION: async_cloud_set_light_component no longer relaxes "
            "the Gen2 gate for unknown hw — light writes will fail during "
            "cold-start cloud outages."
        )
