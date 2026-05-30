"""Bundled coverage tests for cheap remaining gaps across modules.

Targets:
- image.py L105-106, L110 (will_remove_from_hass + device_info)
- switch.py L1804 (PanicAlarm.available), L1976 (ExternalStream.available)
- light.py L187-190 (LAN-reachable Gen2 fallback availability)
- shc.py L471 (_local_write_at timestamp on privacy success)
- maintenance.py L162-164 (invalid date parse), L231 (empty title skip)
- rcp.py L493-494 (LED dimmer _mark_fail on None read)

Each test bypasses heavy HA init via __new__ so the assertion drives
exactly one property/branch.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── image.py ──────────────────────────────────────────────────────────────
class TestImageEntityHooks:
    @pytest.mark.asyncio
    async def test_will_remove_pops_from_coordinator(self):
        from custom_components.bosch_shc_camera.image import (
            BoschCameraLastSnapshotImage,
        )

        coord = SimpleNamespace(_image_entities={"C": "self_ref"})
        ent = BoschCameraLastSnapshotImage.__new__(BoschCameraLastSnapshotImage)
        ent._coordinator = coord
        ent._cam_id = "C"
        with patch(
            "custom_components.bosch_shc_camera.image.ImageEntity.async_will_remove_from_hass",
            new=MagicMock(return_value=None),
        ) as super_mock:
            # Patch the super coroutine into an awaitable.
            async def _noop():
                return None

            super_mock.return_value = _noop()
            await BoschCameraLastSnapshotImage.async_will_remove_from_hass(ent)
        assert "C" not in coord._image_entities

    def test_device_info_full_payload(self):
        """`device_info` builds the device-registry payload from cached
        info — pins L110-117."""
        from custom_components.bosch_shc_camera.image import (
            BoschCameraLastSnapshotImage,
        )

        ent = BoschCameraLastSnapshotImage.__new__(BoschCameraLastSnapshotImage)
        ent._cam_id = "11111111-1111-1111-1111-111111111111"
        ent._display_name = "Bosch Terrasse"
        ent._model_name = "Eyes Outdoor II"
        ent._fw = "9.40.25"
        ent._mac = "64:00:00:00:00:01"
        info = ent.device_info
        assert info["name"] == "Bosch Terrasse"
        assert info["manufacturer"] == "Bosch"
        assert info["model"] == "Eyes Outdoor II"
        assert info["sw_version"] == "9.40.25"
        assert ("mac", "64:00:00:00:00:01") in info["connections"]


# ── switch.py — quick available() pins ────────────────────────────────────
class TestSwitchAvailability:
    def test_panic_alarm_available_requires_coordinator_success_and_online(self):
        """Panic-alarm switch must be unavailable when the coordinator failed
        OR the camera is offline. Pins switch.py L1804."""
        from custom_components.bosch_shc_camera.switch import (
            BoschPanicAlarmSwitch,
        )

        sw = BoschPanicAlarmSwitch.__new__(BoschPanicAlarmSwitch)
        sw._cam_id = "C"
        coord = SimpleNamespace(
            last_update_success=True,
            is_camera_online=lambda _c: True,
        )
        sw.coordinator = coord
        assert sw.available is True
        coord.last_update_success = False
        assert sw.available is False
        coord.last_update_success = True
        coord.is_camera_online = lambda _c: False
        assert sw.available is False

    def test_external_stream_available_follows_coordinator(self):
        """External-stream URL switch is unconditional once the coordinator
        is healthy — pins switch.py L1976."""
        from custom_components.bosch_shc_camera.switch import (
            BoschExternalStreamSwitch,
        )

        sw = BoschExternalStreamSwitch.__new__(BoschExternalStreamSwitch)
        sw._cam_id = "C"
        sw.coordinator = SimpleNamespace(last_update_success=True)
        assert sw.available is True
        sw.coordinator.last_update_success = False
        assert sw.available is False


# ── light.py — LAN-reachable Gen2 availability fallback ──────────────────
class TestLightLanFallback:
    def test_lan_fallback_returns_false_without_helper(self):
        """`is_lan_reachable` missing on stub coords (older builds) → False."""
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(last_update_success=False)
        assert _BoschLightBase.available.fget(light) is False

    def test_lan_fallback_returns_false_when_not_gen2(self):
        """Gen1 cams never get the LAN-RCP fallback — must stay unavailable."""
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(
            last_update_success=False,
            is_lan_reachable=lambda _c: True,
            _hw_version={"C": "CAMERA_EYES"},  # Gen1
        )
        with patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=False,
        ):
            assert _BoschLightBase.available.fget(light) is False

    def test_lan_fallback_returns_true_when_gen2_and_lan_reachable(self):
        """Gen2 + LAN-pingable → light stays controllable during cloud 503."""
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(
            last_update_success=False,
            is_lan_reachable=lambda _c: True,
            _hw_version={"C": "HOME_Eyes_Outdoor"},  # Gen2
        )
        with patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=True,
        ):
            assert _BoschLightBase.available.fget(light) is True

    def test_lan_fallback_returns_false_when_gen2_but_unreachable(self):
        from custom_components.bosch_shc_camera.light import _BoschLightBase

        light = _BoschLightBase.__new__(_BoschLightBase)
        light._cam_id = "C"
        light.coordinator = SimpleNamespace(
            last_update_success=False,
            is_lan_reachable=lambda _c: False,
            _hw_version={"C": "HOME_Eyes_Outdoor"},
        )
        with patch(
            "custom_components.bosch_shc_camera.shc._is_gen2",
            return_value=True,
        ):
            assert _BoschLightBase.available.fget(light) is False


# ── maintenance.py — parser edge cases ────────────────────────────────────
class TestMaintenanceParserEdges:
    def test_invalid_date_returns_none_pair(self):
        """`_parse_window` swallows ValueError from invalid date components
        (e.g. 30. Februar) and returns (None, None). Pins L162-164."""
        from custom_components.bosch_shc_camera.maintenance import _parse_window

        # 30. Februar is unparseable — datetime constructor raises.
        # Need the "Uhr" keyword for _TIME_RANGE_RE to match, then trigger the
        # ValueError in datetime() below.
        text = "Wartung am 30.02.2026 von 07:00 bis 10:00 Uhr MESZ"
        pub = datetime(2026, 2, 28, tzinfo=UTC)
        start, end = _parse_window(text, pub)
        assert start is None and end is None

    @pytest.mark.asyncio
    async def test_empty_title_entry_is_skipped(self):
        """RSS items without a title must be skipped instead of crashing on
        the empty string. Pins maintenance.py L231."""
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera import maintenance

        # Synthetic RSS payload with one good entry and one with empty title.
        rss = b"""<?xml version="1.0"?>
        <rss><channel>
          <item>
            <title></title>
            <link>https://example/empty</link>
            <pubDate>Tue, 19 May 2026 06:00:00 +0000</pubDate>
            <description>placeholder</description>
          </item>
          <item>
            <title>Wartung Kamera-Cloud 19.05.2026 07:00-10:00 MESZ</title>
            <link>https://example/good</link>
            <pubDate>Tue, 19 May 2026 06:00:00 +0000</pubDate>
            <description>Wartung der Kamera-Cloud</description>
          </item>
        </channel></rss>"""

        class _FakeResp:
            status = 200

            async def read(self):
                return rss

            async def text(self):
                return rss.decode()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def get(self, _url, **_kw):
                return _FakeResp()

        result = await maintenance.async_fetch_maintenance(_FakeSession())
        # The empty-title item was skipped (continue branch at L231) and the
        # good item produced the returned MaintenanceWindow.
        assert result is not None
        assert "Wartung" in result.title


# ── shc.py — _local_write_at timestamp on Gen2 LAN-RCP privacy success ───
class TestLocalWriteTimestamp:
    @pytest.mark.asyncio
    async def test_local_write_at_recorded_on_lan_fallback_success(self):
        """When the cloud privacy call fails but the LAN-RCP fallback
        succeeds, the coordinator records `monotonic()` in
        `_local_write_at[cam_id]` so the next coordinator tick gives the
        camera a 30 s grace period before re-polling state. Pins shc.py L471."""
        from unittest.mock import AsyncMock

        from custom_components.bosch_shc_camera import shc

        coord = SimpleNamespace()
        coord._cached_status = {}
        coord._hw_version = {"C": "HOME_Eyes_Outdoor"}
        coord._shc_state_cache = {}
        coord._rcp_lan_ip_cache = {"C": "192.0.2.10"}
        coord._local_creds_cache = {}
        coord._privacy_set_at = {}
        coord._local_write_at = {}
        coord.hass = MagicMock()
        coord.token = None  # bypass the cloud branch entirely
        coord.async_update_listeners = MagicMock()
        with (
            patch(
                "custom_components.bosch_shc_camera.rcp.rcp_local_write_privacy",
                new=AsyncMock(return_value=True),
            ),
            patch.object(shc, "_is_gen2", return_value=True),
            patch(
                "custom_components.bosch_shc_camera.shc.time.monotonic",
                return_value=4242.0,
            ),
        ):
            ok = await shc.async_cloud_set_privacy_mode(coord, "C", True)
        assert ok is True
        # L471 — timestamp recorded on the coordinator.
        assert coord._local_write_at["C"] == 4242.0
