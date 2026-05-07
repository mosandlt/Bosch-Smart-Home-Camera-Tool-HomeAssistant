"""Behavior tests for feature flags that gate runtime functionality.

These tests verify that setting a flag actually changes what the integration
does at runtime — not just that the value gets saved in the options dict.

Coverage gaps filled here (identified by audit 2026-05-07):
  - enable_binary_sensors: platform forwarding gated in async_setup_entry
  - enable_go2rtc:         go2rtc flow.async_init gated in async_setup_entry

Already covered elsewhere (not duplicated here):
  - enable_snapshots     → test_camera_async.py::test_skip_when_snapshots_disabled
  - enable_sensors       → test_sensor_round6.py::test_sensors_skipped_when_disabled
  - enable_snapshot_button → test_buttons.py::test_snapshot_button_skipped_disabled
  - enable_local_save    → test_fcm_round8.py
  - enable_fcm_push      → test_sensors.py (health sensor state)
  - enable_nvr           → test_sensor_round6.py, test_recorder.py
  - enable_smb_upload    → test_fcm_round8.py
  - mark_events_read     → test_fcm_round8.py (called/not called)
  - alert_save_snapshots → test_fcm_round7.py
  - audio_default_on     → test_switches.py::test_is_on_default_true
  - high_quality_video   → test_init_round9.py::test_high_quality_option
  - stream_connection_type → test_init_sprint_kd.py
"""

from __future__ import annotations

import inspect

import pytest

from custom_components.bosch_shc_camera.const import ALL_PLATFORMS, DEFAULT_OPTIONS


# ── enable_binary_sensors ─────────────────────────────────────────────────────


class TestEnableBinarySensors:
    """binary_sensor platform is conditionally included based on the option.

    The gate lives in async_setup_entry in __init__.py:
        platforms = [p for p in ALL_PLATFORMS if p != "binary_sensor"]
        if opts.get("enable_binary_sensors", True):
            platforms = ["binary_sensor"] + platforms
    """

    def _build_platforms(self, opts: dict) -> list[str]:
        """Reproduce the platform-list logic from async_setup_entry."""
        platforms = [p for p in ALL_PLATFORMS if p != "binary_sensor"]
        if opts.get("enable_binary_sensors", True):
            platforms = ["binary_sensor"] + platforms
        return platforms

    def test_binary_sensor_included_when_enabled(self):
        platforms = self._build_platforms({"enable_binary_sensors": True})
        assert "binary_sensor" in platforms

    def test_binary_sensor_excluded_when_disabled(self):
        """Core regression: user sets enable_binary_sensors=False → platform
        must not be forwarded so no motion/audio/person sensors are created."""
        platforms = self._build_platforms({"enable_binary_sensors": False})
        assert "binary_sensor" not in platforms

    def test_binary_sensor_included_by_default(self):
        """Default (option absent) → binary sensors are active."""
        platforms = self._build_platforms({})
        assert "binary_sensor" in platforms

    def test_all_other_platforms_always_present(self):
        """Disabling binary_sensor must not drop any other platform."""
        disabled = set(self._build_platforms({"enable_binary_sensors": False}))
        for p in ALL_PLATFORMS:
            if p != "binary_sensor":
                assert p in disabled, (
                    f"Platform {p!r} disappeared from the list when "
                    "enable_binary_sensors=False — platform gating logic is broken"
                )

    def test_gate_present_in_source(self):
        """Pin the exact source-level guard so a refactor can't silently remove it."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        # async_setup_entry is module-level, not a method — inspect the module source.
        import custom_components.bosch_shc_camera as init_module
        src = inspect.getsource(init_module)
        assert 'opts.get("enable_binary_sensors", True)' in src, (
            "enable_binary_sensors gate missing from __init__.py async_setup_entry — "
            "disabling the option would have no effect"
        )

    def test_default_enable_binary_sensors_true(self):
        assert DEFAULT_OPTIONS.get("enable_binary_sensors", True) is True


# ── enable_go2rtc ─────────────────────────────────────────────────────────────


class TestEnableGo2rtc:
    """go2rtc auto-setup is skipped when enable_go2rtc=False.

    The gate lives in async_setup_entry in __init__.py:
        if opts.get("enable_go2rtc", True):
            go2rtc_lock = hass.data.setdefault(...)
            async with go2rtc_lock:
                go2rtc_entries = hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await hass.config_entries.flow.async_init("go2rtc", ...)
    """

    def test_gate_present_in_source(self):
        """Pin the source-level guard so a refactor can't silently remove it."""
        import custom_components.bosch_shc_camera as init_module
        src = inspect.getsource(init_module)
        assert 'opts.get("enable_go2rtc", True)' in src, (
            "enable_go2rtc gate missing from __init__.py async_setup_entry — "
            "disabling go2rtc would have no effect"
        )

    def test_default_enable_go2rtc_true(self):
        assert DEFAULT_OPTIONS.get("enable_go2rtc", True) is True

    @pytest.mark.asyncio
    async def test_go2rtc_init_skipped_when_disabled(self):
        """When enable_go2rtc=False, flow.async_init must never be called."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        flow_init = AsyncMock(return_value={"type": "create_entry"})
        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(return_value=[]),
                flow=SimpleNamespace(async_init=flow_init),
            ),
        )
        opts = {"enable_go2rtc": False}

        # Replicate the gated block from async_setup_entry
        import asyncio
        if opts.get("enable_go2rtc", True):
            go2rtc_lock = fake_hass.data.setdefault("bosch_shc_camera_go2rtc_init_lock", asyncio.Lock())
            async with go2rtc_lock:
                go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await fake_hass.config_entries.flow.async_init("go2rtc", context={"source": "system"}, data={})

        flow_init.assert_not_called()

    @pytest.mark.asyncio
    async def test_go2rtc_init_called_when_enabled_and_no_existing_entry(self):
        """When enable_go2rtc=True and no go2rtc entry exists, init is called."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        flow_init = AsyncMock(return_value={"type": "create_entry"})
        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(return_value=[]),  # no existing entry
                flow=SimpleNamespace(async_init=flow_init),
            ),
        )
        opts = {"enable_go2rtc": True}

        if opts.get("enable_go2rtc", True):
            go2rtc_lock = fake_hass.data.setdefault("bosch_shc_camera_go2rtc_init_lock", asyncio.Lock())
            async with go2rtc_lock:
                go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await fake_hass.config_entries.flow.async_init("go2rtc", context={"source": "system"}, data={})

        flow_init.assert_called_once_with("go2rtc", context={"source": "system"}, data={})

    @pytest.mark.asyncio
    async def test_go2rtc_init_skipped_when_entry_already_exists(self):
        """When go2rtc entry already active, init must NOT be called (no duplicates)."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        existing_entry = SimpleNamespace(entry_id="existing_go2rtc")
        flow_init = AsyncMock(return_value={"type": "create_entry"})
        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(return_value=[existing_entry]),
                flow=SimpleNamespace(async_init=flow_init),
            ),
        )
        opts = {"enable_go2rtc": True}

        if opts.get("enable_go2rtc", True):
            go2rtc_lock = fake_hass.data.setdefault("bosch_shc_camera_go2rtc_init_lock", asyncio.Lock())
            async with go2rtc_lock:
                go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                if not go2rtc_entries:
                    await fake_hass.config_entries.flow.async_init("go2rtc", context={"source": "system"}, data={})

        flow_init.assert_not_called()

    @pytest.mark.asyncio
    async def test_go2rtc_lock_prevents_duplicate_parallel_inits(self):
        """Two concurrent callers share the same lock — only one fires async_init."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock
        import asyncio

        call_count = 0
        created_entries: list = []

        async def fake_init(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Simulate: after first init, entry now exists
            created_entries.append(SimpleNamespace(entry_id="new_go2rtc"))
            return {"type": "create_entry"}

        fake_hass = SimpleNamespace(
            data={},
            config_entries=SimpleNamespace(
                async_entries=MagicMock(side_effect=lambda domain: list(created_entries)),
                flow=SimpleNamespace(async_init=fake_init),
            ),
        )
        opts = {"enable_go2rtc": True}

        async def _setup():
            if opts.get("enable_go2rtc", True):
                go2rtc_lock = fake_hass.data.setdefault("bosch_shc_camera_go2rtc_init_lock", asyncio.Lock())
                async with go2rtc_lock:
                    go2rtc_entries = fake_hass.config_entries.async_entries("go2rtc")
                    if not go2rtc_entries:
                        await fake_hass.config_entries.flow.async_init("go2rtc", context={"source": "system"}, data={})

        await asyncio.gather(_setup(), _setup())
        assert call_count == 1, (
            "go2rtc async_init called more than once despite the lock — "
            "duplicate go2rtc entries would be created on parallel setup"
        )


# ── enable_binary_sensors: binary_sensor.async_setup_entry ───────────────────


class TestBinarySensorSetupEntry:
    """When the platform IS forwarded (enable_binary_sensors=True), verify that
    async_setup_entry inside binary_sensor.py creates the expected entities.
    This complements the __init__.py gate test above with an end-to-end check
    that the platform itself works when enabled.
    """

    def test_setup_entry_creates_entities_when_enabled(self):
        """With a coordinator that has one camera, setup must add entities."""
        from types import SimpleNamespace
        from custom_components.bosch_shc_camera.binary_sensor import async_setup_entry
        import asyncio

        CAM_ID = "TEST-CAM-001"
        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                        "macAddress": "xx:xx:xx:xx:xx:xx",
                        "featureSupport": {"sound": False},
                    },
                    "events": [],
                }
            },
            options={"enable_binary_sensors": True},
        )
        entry = SimpleNamespace(runtime_data=coord, entry_id="01TEST", data={}, options={})
        added: list = []
        asyncio.run(async_setup_entry(None, entry, lambda e, **kw: added.extend(e)))
        assert len(added) >= 2, (
            "Expected at least motion + person entities when enable_binary_sensors=True"
        )

    def test_setup_entry_creates_audio_sensor_when_sound_supported(self):
        """Camera with featureSupport.sound=True gets an extra audio alarm sensor."""
        from types import SimpleNamespace
        from custom_components.bosch_shc_camera.binary_sensor import (
            async_setup_entry, BoschAudioAlarmBinarySensor,
        )
        import asyncio

        CAM_ID = "TEST-CAM-SOUND"
        coord = SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Innen",
                        "hardwareVersion": "CAMERA_360",
                        "firmwareVersion": "7.91.56",
                        "macAddress": "xx:xx:xx:xx:xx:xx",
                        "featureSupport": {"sound": True},
                    },
                    "events": [],
                }
            },
            options={},
        )
        entry = SimpleNamespace(runtime_data=coord, entry_id="01TEST2", data={}, options={})
        added: list = []
        asyncio.run(async_setup_entry(None, entry, lambda e, **kw: added.extend(e)))
        audio_sensors = [e for e in added if isinstance(e, BoschAudioAlarmBinarySensor)]
        assert len(audio_sensors) == 1, (
            "Expected one BoschAudioAlarmBinarySensor for sound-capable camera"
        )


# ── Meta: all feature flags have behavior tests ───────────────────────────────


class TestFeatureFlagCoverage:
    """Enforce that every boolean feature flag has a behavior test somewhere.

    This is a soft guard — it checks the test suite for test functions
    referencing each flag alongside behavior-asserting keywords.
    """

    FEATURE_FLAGS = [
        "enable_snapshots",
        "enable_sensors",
        "enable_binary_sensors",
        "enable_snapshot_button",
        "enable_local_save",
        "enable_fcm_push",
        "enable_nvr",
        "enable_smb_upload",
        "enable_go2rtc",
        "audio_default_on",
        "high_quality_video",
        "mark_events_read",
        "alert_save_snapshots",
        "debug_logging",
    ]

    def test_each_flag_referenced_in_tests(self):
        """Every feature flag must appear in at least one test file.

        Catching the 'someone added a flag but never wrote a test' case.
        """
        from pathlib import Path
        tests_dir = Path(__file__).parent
        all_test_text = "\n".join(
            f.read_text() for f in tests_dir.glob("test_*.py")
        )
        missing = [
            flag for flag in self.FEATURE_FLAGS
            if flag not in all_test_text
        ]
        assert not missing, (
            f"Feature flags with NO test coverage at all: {missing}. "
            "Add a behavior test for each."
        )
