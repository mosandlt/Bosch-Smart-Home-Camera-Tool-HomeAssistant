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

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        async def _fake_digest_request(session, method, url, user, password, **_):
            observed_url.append(url)
            return _FakeResp()

        with patch(
            "custom_components.bosch_shc_camera.auth_utils.async_digest_request",
            side_effect=_fake_digest_request,
        ):
            with patch(
                "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
                return_value=MagicMock(),
            ):
                ok = await rcp_local_write(
                    MagicMock(),
                    "192.0.2.149",
                    "0x0d00",
                    "00010000",
                    "P_OCTET",
                    user="cbs-xxx",
                    password="secret",
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

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

        class _FakeSession:
            def get(self, url, **kwargs):
                observed_url.append(url)
                return _FakeResp()

        with patch(
            "custom_components.bosch_shc_camera.rcp.async_get_clientsession",
            return_value=_FakeSession(),
        ):
            await rcp_local_write(
                MagicMock(),
                "192.0.2.149",
                "0x0d00",
                "00010000",
                "P_OCTET",
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
            BoschPrivacyModeSwitch.__bases__[0],
            "available",
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
            BoschPrivacyModeSwitch.__bases__[0],
            "available",
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


# ── 4. Cloud 444 → skip-cloud cooldown ───────────────────────────────────


class TestCloud444Cooldown:
    """A cloud HTTP 444 (session quota / freshly re-paired camera that is
    'online' for status but rejects writes) must:
      1. stamp `coordinator._cloud_444_at[cam_id]`, then
      2. make the *next* privacy write within the cooldown skip the cloud
         entirely and go straight to the LAN/SHC fallback.

    Surfaced 2026-06-01: a re-paired Gen1 indoor returned 444 to every cloud
    privacy write while status still read 'online'. Without the cooldown we
    re-hit the cloud for another 444 on every toggle.
    """

    def _coord(self):
        return SimpleNamespace(
            token="token-AAA",
            hass=SimpleNamespace(
                async_create_task=lambda coro: coro.close(),
                services=SimpleNamespace(async_call=AsyncMock()),
            ),
            _shc_state_cache={CAM_ID: {}},
            _privacy_set_at={},
            _light_set_at={},
            _notif_set_at={},
            _local_creds_cache={},
            _rcp_lan_ip_cache={},
            _pan_cache={},
            _camera_entities={},
            _hw_version={CAM_ID: "OUTDOOR"},
            _cached_status={},  # NOT "OFFLINE" — status reads online
            _cloud_444_at={},
            _auth_outage_count=0,
            async_update_listeners=lambda: None,
            async_request_refresh=AsyncMock(),
            _ensure_valid_token=AsyncMock(return_value="token-FRESH"),
        )

    def _resp(self, status: int):
        resp = MagicMock()
        resp.status = status
        resp.json = AsyncMock(return_value={})
        resp.text = AsyncMock(return_value="")
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    @pytest.mark.asyncio
    async def test_444_stamps_and_next_write_skips_cloud(self):
        from custom_components.bosch_shc_camera import shc

        coord = self._coord()

        # First write: cloud returns 444 → must stamp _cloud_444_at and fall
        # through to the (unconfigured) SHC fallback → overall False.
        with (
            patch.object(
                shc, "async_get_bosch_cloud_session", new_callable=AsyncMock
            ) as session_factory,
            patch.object(shc, "shc_ready", return_value=False),
        ):
            session = MagicMock()
            session.put = MagicMock(return_value=self._resp(444))
            session_factory.return_value = session
            ok1 = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

        assert ok1 is False
        assert CAM_ID in coord._cloud_444_at, (
            "REGRESSION: a cloud 444 no longer stamps _cloud_444_at — the next "
            "write will re-hit the cloud for another 444."
        )

        # Second write within the cooldown: cloud must NOT be called at all.
        with (
            patch.object(
                shc, "async_get_bosch_cloud_session", new_callable=AsyncMock
            ) as session_factory,
            patch.object(shc, "shc_ready", return_value=False),
        ):
            session = MagicMock()
            session.put = MagicMock(return_value=self._resp(204))
            session_factory.return_value = session
            ok2 = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

            assert session.put.call_count == 0, (
                "REGRESSION: privacy write hit the cloud despite a recent 444 — "
                "the LAN/SHC fallback should be used directly during cooldown."
            )
        assert ok2 is False  # SHC unconfigured → fallback also fails

    @pytest.mark.asyncio
    async def test_stale_444_outside_cooldown_uses_cloud_again(self):
        import time

        from custom_components.bosch_shc_camera import shc

        coord = self._coord()
        # Stamp a 444 well outside the 120s cooldown.
        coord._cloud_444_at[CAM_ID] = time.monotonic() - 600

        with patch.object(
            shc, "async_get_bosch_cloud_session", new_callable=AsyncMock
        ) as session_factory:
            session = MagicMock()
            session.put = MagicMock(return_value=self._resp(204))
            session_factory.return_value = session
            ok = await shc.async_cloud_set_privacy_mode(coord, CAM_ID, True)

            assert session.put.call_count == 1, (
                "REGRESSION: a stale (expired) 444 still suppressed the cloud — "
                "cooldown must lapse after _CLOUD_444_COOLDOWN seconds."
            )
        assert ok is True
