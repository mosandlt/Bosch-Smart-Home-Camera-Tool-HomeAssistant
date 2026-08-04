"""Tests for button.py entities: snapshot-refresh, soft-reset, hard-reset.

Also covers BoschCameraCoordinator.async_soft_reset_camera() /
async_hard_reset_camera() — the coordinator methods the soft/hard reset
buttons call into on press.

Reverse-engineered from the official Bosch app (research/apk_2.12.0
decompile): BackendUrlProviderService.GetCameraSoftResetUrl /
GetCameraHardResetUrl -> bodyless PUT video_inputs/{id}/soft_reset and
.../hard_reset. Applies to all camera generations (gating in the app is
"camera online + not shared", not hardware-version-specific).

Pins:
  - both coordinator methods PUT the correct endpoint with an empty body
  - both raise HomeAssistantError when the cloud rejects the PUT
  - the hard-reset button is disabled by default (destructive — requires
    re-pairing the camera in the Bosch app afterward)
  - the soft-reset button is NOT disabled by default (non-destructive)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def stub_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "status": "ONLINE",
            }
        },
        camera_entities={},
        async_request_refresh=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
        async_add_listener=MagicMock(return_value=MagicMock()),
    )


@pytest.fixture
def stub_entry() -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="01ENTRY", data={}, options={}, async_on_unload=MagicMock()
    )


# ── BoschRefreshSnapshotButton ──────────────────────────────────────────


def test_refresh_button_construction(
    stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
):
    """Refresh button instantiates with the expected unique_id + translation_key."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    assert btn._attr_unique_id.startswith("bosch_shc_refresh_")
    assert btn._attr_translation_key == "refresh_snapshot"
    # v14.2.2 — _attr_name is None; HA resolves the entity name from
    # translations/en.json via _attr_translation_key at runtime.
    assert getattr(btn, "_attr_name", None) is None


def test_refresh_button_device_info(
    stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
):
    """device_info propagates model name + firmware + mac."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["manufacturer"] == "Bosch"
    assert info["sw_version"] == "9.40.25"
    assert "Außenkamera" in info["model"]
    assert info["connections"] == {("mac", "aa:bb:cc:dd:ee:01")}


# ── No mac → no connection entry ────────────────────────────────────────


def test_device_info_no_mac_skipped(
    stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
):
    """Empty mac → device_info connections is an empty set."""
    from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

    stub_coord.data[CAM_ID]["info"]["macAddress"] = ""
    btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
    info = btn.device_info
    assert info["connections"] == set()

    assert info["sw_version"] == "9.40.25"


# ── async_press ─────────────────────────────────────────────────────────


class TestRefreshSnapshotPress:
    @pytest.mark.asyncio
    async def test_press_schedules_coordinator_refresh(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """async_press must schedule coordinator.async_request_refresh via hass.async_create_task."""
        from unittest.mock import MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        # MagicMock.async_create_task returns a MagicMock (has .add_done_callback)
        btn.hass = fake_hass
        await btn.async_press()
        assert fake_hass.async_create_task.call_count >= 1

    @pytest.mark.asyncio
    async def test_press_also_triggers_image_refresh_when_cam_entity_present(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """When a camera entity is registered, async_press also schedules its image refresh."""
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        fake_cam = MagicMock()
        fake_cam.async_trigger_image_refresh = AsyncMock(return_value=None)
        stub_coord.camera_entities[CAM_ID] = fake_cam

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        btn.hass = fake_hass
        await btn.async_press()
        assert fake_hass.async_create_task.call_count == 2


class TestRefreshSnapshotErrorHandling:
    """async_press attaches error-logging callbacks to the created tasks."""

    @pytest.mark.asyncio
    async def test_done_callback_attached_to_refresh_task(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A done callback must be registered on the coordinator refresh task."""
        from unittest.mock import MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        btn.hass = fake_hass
        await btn.async_press()
        task_mock = fake_hass.async_create_task.return_value
        task_mock.add_done_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_done_callback_attached_to_image_refresh_task(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """A done callback must be registered on the image refresh task too."""
        from unittest.mock import AsyncMock, MagicMock

        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        fake_cam = MagicMock()
        fake_cam.async_trigger_image_refresh = AsyncMock(return_value=None)
        stub_coord.camera_entities[CAM_ID] = fake_cam

        btn = BoschRefreshSnapshotButton(stub_coord, CAM_ID, stub_entry)
        fake_hass = MagicMock()
        btn.hass = fake_hass
        await btn.async_press()
        # Two tasks created → each should have add_done_callback called once
        assert fake_hass.async_create_task.call_count == 2
        for (
            call
        ) in fake_hass.async_create_task.return_value.add_done_callback.call_args_list:
            assert call is not None


# ── async_setup_entry ────────────────────────────────────────────────────


class TestSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_one_button_per_camera(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """Default options → 3 button entities per camera: Refresh Snapshot,
        Restart (soft reset), Factory Reset (hard reset).

        BoschAcousticAlarmButton was removed entirely in v13.3 (was kept as
        an orphan since v12.0.4). Gen1 cameras have no integrated siren;
        Gen2 cameras use BoschPanicAlarmSwitch via /panic_alarm in switch.py.
        """
        from custom_components.bosch_shc_camera.button import (
            BoschHardResetButton,
            BoschRefreshSnapshotButton,
            BoschSoftResetButton,
            async_setup_entry,
        )

        stub_entry.runtime_data = stub_coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=stub_entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        types_ = {type(e) for e in captured}
        assert BoschRefreshSnapshotButton in types_
        assert BoschSoftResetButton in types_
        assert BoschHardResetButton in types_
        assert len(captured) == 3

    @pytest.mark.asyncio
    async def test_snapshot_button_disabled_but_reset_buttons_remain(
        self, stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """enable_snapshot_button=False only skips the snapshot-refresh button —
        the reset buttons aren't gated by that option and must still appear."""
        from custom_components.bosch_shc_camera.button import (
            BoschHardResetButton,
            BoschRefreshSnapshotButton,
            BoschSoftResetButton,
            async_setup_entry,
        )

        stub_entry.options = {"enable_snapshot_button": False}
        stub_entry.data = {}
        stub_entry.runtime_data = stub_coord
        captured: list = []
        await async_setup_entry(
            hass=None,
            config_entry=stub_entry,
            async_add_entities=lambda e, update_before_add=False: captured.extend(e),
        )
        types_ = {type(e) for e in captured}
        assert BoschRefreshSnapshotButton not in types_
        assert BoschSoftResetButton in types_
        assert BoschHardResetButton in types_
        assert len(captured) == 2


# ── coordinator soft/hard reset methods (called by the reset buttons) ──


def _make_coord() -> SimpleNamespace:
    return SimpleNamespace(async_put_camera=AsyncMock(return_value=True))


class TestAsyncSoftResetCamera:
    @pytest.mark.asyncio
    async def test_puts_soft_reset_endpoint_with_empty_body(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        await BoschCameraCoordinator.async_soft_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]
        coord.async_put_camera.assert_awaited_once_with(CAM_ID, "soft_reset", None)

    @pytest.mark.asyncio
    async def test_cloud_rejects_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.async_put_camera.return_value = False
        with pytest.raises(HomeAssistantError, match="soft_reset_rejected"):
            await BoschCameraCoordinator.async_soft_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]


class TestAsyncHardResetCamera:
    @pytest.mark.asyncio
    async def test_puts_hard_reset_endpoint_with_empty_body(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        await BoschCameraCoordinator.async_hard_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]
        coord.async_put_camera.assert_awaited_once_with(CAM_ID, "hard_reset", None)

    @pytest.mark.asyncio
    async def test_cloud_rejects_raises(self):
        from homeassistant.exceptions import HomeAssistantError

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord()
        coord.async_put_camera.return_value = False
        with pytest.raises(HomeAssistantError, match="hard_reset_rejected"):
            await BoschCameraCoordinator.async_hard_reset_camera(coord, CAM_ID)  # type: ignore[arg-type]


def _make_button_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {"info": {"title": "Terrasse", "hardwareVersion": "CAMERA_EYES"}}
        },
        async_soft_reset_camera=AsyncMock(return_value=None),
        async_hard_reset_camera=AsyncMock(return_value=None),
    )


class TestResetButtonEntities:
    """BoschSoftResetButton / BoschHardResetButton default-enabled state and
    press-delegates-to-coordinator behavior (separate from the
    per-camera-setup coverage in TestSetupEntry above, which uses a
    different stub_coord fixture)."""

    def test_soft_reset_button_disabled_by_default(self):
        """Live-tested 2026-07-08: Bosch's cloud rejects this with HTTP 404
        sh:entity.notfound despite the request matching the app exactly —
        disabled by default until Bosch's backend actually supports it."""
        from custom_components.bosch_shc_camera.button import BoschSoftResetButton

        btn = BoschSoftResetButton(_make_button_coord(), CAM_ID, MagicMock())
        assert btn._attr_entity_registry_enabled_default is False

    def test_hard_reset_button_disabled_by_default(self):
        """Destructive action — must not be silently enabled for every user."""
        from custom_components.bosch_shc_camera.button import BoschHardResetButton

        btn = BoschHardResetButton(_make_button_coord(), CAM_ID, MagicMock())
        assert btn._attr_entity_registry_enabled_default is False

    @pytest.mark.asyncio
    async def test_soft_reset_button_press_calls_coordinator(self):
        from custom_components.bosch_shc_camera.button import BoschSoftResetButton

        coord = _make_button_coord()
        btn = BoschSoftResetButton(coord, CAM_ID, MagicMock())
        await btn.async_press()
        coord.async_soft_reset_camera.assert_awaited_once_with(CAM_ID)

    @pytest.mark.asyncio
    async def test_hard_reset_button_press_calls_coordinator(self):
        from custom_components.bosch_shc_camera.button import BoschHardResetButton

        coord = _make_button_coord()
        btn = BoschHardResetButton(coord, CAM_ID, MagicMock())
        await btn.async_press()
        coord.async_hard_reset_camera.assert_awaited_once_with(CAM_ID)


# ── Doubled-prefix entity_id naming regression ──────────────────────────
#
# Source: Andrew75 forum post 998974/15 (2026-05-15) reported entity IDs
# like button.bosch_est_bosch_est_refresh_snapshot instead of
# button.bosch_est_refresh_snapshot.
#
# Root cause: classes with `_attr_has_entity_name = True` AND
# `_attr_name = f"Bosch {self._cam_title} <Suffix>"` caused HA to prepend
# the device name automatically AND the code re-prepended "Bosch {title}"
# manually.
#
# Fix (v14.2.2): remove all `_attr_name` assignments; use
# `_attr_translation_key` instead so HA resolves the entity name from
# translations/en.json at runtime. `_attr_name` must be None (unset) for
# translation_key-based naming to work.


@pytest.fixture
def _naming_stub_coord() -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:ff",
                },
                "live": {},
                "motion": {},
            }
        },
        camera_entities={},
        firmware_cache={},
        intrusion_config_cache={},
        stream_type_override=None,
        last_update_success=True,
        get_quality=lambda cid: "auto",
        set_quality=lambda cid, q: None,
        motion_settings=lambda cid: {},
        async_request_refresh=AsyncMock(),
        async_put_camera=AsyncMock(return_value=True),
        options={"enable_fcm_push": False},
    )


class TestRefreshSnapshotButtonNaming:
    def test_attr_name_is_none(
        self, _naming_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        """_attr_name must be None — translation_key provides the entity name."""
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(_naming_stub_coord, CAM_ID, stub_entry)
        name = getattr(btn, "_attr_name", None)
        assert name is None or not name.startswith("Bosch "), (
            f"_attr_name={name!r} still contains the 'Bosch' prefix — entity_id would be doubled"
        )

    def test_translation_key_is_refresh_snapshot(
        self, _naming_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(_naming_stub_coord, CAM_ID, stub_entry)
        assert btn._attr_translation_key == "refresh_snapshot"

    def test_has_entity_name_is_true(
        self, _naming_stub_coord: SimpleNamespace, stub_entry: SimpleNamespace
    ):
        from custom_components.bosch_shc_camera.button import BoschRefreshSnapshotButton

        btn = BoschRefreshSnapshotButton(_naming_stub_coord, CAM_ID, stub_entry)
        assert btn._attr_has_entity_name is True
