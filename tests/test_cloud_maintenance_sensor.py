"""Coverage tests for `BoschCloudMaintenanceSensor` (v12.4.7+).

The sensor surfaces the parsed community RSS maintenance window state
(`active`/`scheduled`/`past`/`recent`/`unknown`/`idle`) as a HA ENUM sensor.
It must remain available even while the Bosch cloud is down, since that's
exactly when users check it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.bosch_shc_camera.maintenance import MaintenanceWindow
from custom_components.bosch_shc_camera.sensor import BoschCloudMaintenanceSensor


def _window(*, active: bool = True) -> MaintenanceWindow:
    ref = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
    start = ref - timedelta(hours=1) if active else ref + timedelta(hours=1)
    end = ref + timedelta(hours=2) if active else ref + timedelta(hours=3)
    return MaintenanceWindow(
        title="Wartung Kamera-Infrastruktur",
        link="https://example/x",
        pub_date=ref - timedelta(hours=12),
        summary="07:00–10:00 MESZ",
        scheduled_start=start,
        scheduled_end=end,
        source="rss:Wartungsarbeiten",
        camera_relevant=True,
    )


def _coord(
    *, cache: MaintenanceWindow | None, last_fetch: float = float("-inf")
) -> SimpleNamespace:
    c = SimpleNamespace()
    c._maintenance_cache = cache
    c._maintenance_last_fetch = last_fetch
    # _BoschSensorBase.__init__ reads coordinator.data[cam_id]['info'] for
    # device-info fields — stub it so the constructor succeeds.
    c.data = {"CAM_ID_X": {"info": {"title": "TestCam"}}}
    return c


def _make_sensor(
    cache: MaintenanceWindow | None, last_fetch: float = float("-inf")
) -> BoschCloudMaintenanceSensor:
    return BoschCloudMaintenanceSensor(
        _coord(cache=cache, last_fetch=last_fetch),
        "CAM_ID_X",
        SimpleNamespace(),  # entry — _BoschSensorBase only stores it
    )


class TestCloudMaintenanceSensorMetadata:
    def test_identity_props(self):
        s = _make_sensor(None)
        # v14.2.2 — name resolved from translation key at runtime (not _attr_name)
        assert s._attr_translation_key == "cloud_maintenance"
        assert s.unique_id == "bosch_shc_camera_cloud_maintenance"
        # Always-on availability — sensor must stay readable during cloud outage.
        assert s.available is True


class TestCloudMaintenanceSensorValue:
    def test_native_value_idle_when_no_cache(self):
        assert _make_sensor(None).native_value == "idle"

    def test_native_value_active(self):
        # MaintenanceWindow.state() returns active/scheduled/past/recent/unknown.
        assert _make_sensor(_window(active=True)).native_value in {
            "active",
            "scheduled",
            "past",
            "recent",
            "unknown",
        }

    def test_extra_attrs_empty_when_no_cache(self):
        attrs = _make_sensor(None).extra_state_attributes
        assert "title" not in attrs
        assert "last_fetched_seconds_ago" not in attrs

    def test_extra_attrs_with_window(self, monkeypatch):
        mw = _window(active=True)
        import time as _time

        monkeypatch.setattr(_time, "monotonic", lambda: 1042.0)
        attrs = _make_sensor(mw, last_fetch=1000.0).extra_state_attributes
        assert attrs.get("title") == mw.title
        assert attrs.get("source") == mw.source
        # 1042 - 1000 = 42s ago.
        assert attrs.get("last_fetched_seconds_ago") == 42

    def test_extra_attrs_skips_last_fetched_when_never(self, monkeypatch):
        mw = _window(active=True)
        import time as _time

        monkeypatch.setattr(_time, "monotonic", lambda: 1042.0)
        attrs = _make_sensor(mw, last_fetch=float("-inf")).extra_state_attributes
        assert "last_fetched_seconds_ago" not in attrs

    def test_volatile_attr_is_unrecorded(self, monkeypatch):
        """HA#39: `last_fetched_seconds_ago` changes every tick → exclude it
        from the recorder so `state_attributes` does not bloat. Emitted live,
        recording suppressed."""
        mw = _window(active=True)
        import time as _time

        monkeypatch.setattr(_time, "monotonic", lambda: 1042.0)
        s = _make_sensor(mw, last_fetch=1000.0)
        assert "last_fetched_seconds_ago" in s.extra_state_attributes
        assert "last_fetched_seconds_ago" in s._unrecorded_attributes
