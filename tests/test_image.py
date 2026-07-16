"""Tests for image.py — BoschCameraLastSnapshotImage entity.

Covers: creation gate, async_image disk+fallback, async_notify_refreshed,
unique_id stability, and privacy bypass.

Source: user report — iOS Companion App (WKWebView) served yesterday's
snapshot for ~5s on cold-open due to heuristic disk-caching.
Fix: image entity whose signed URL changes on each state-push.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
DISK_JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 300  # 304 B — looks like a real snapshot
RAM_JPEG = b"\xff\xd8\xff\xe0" + b"\x02" * 300  # different bytes for RAM path


def _make_hass(tmp_path: Path) -> Any:
    hass = SimpleNamespace()
    storage = tmp_path / ".storage"
    storage.mkdir()
    hass.config = SimpleNamespace(path=lambda *parts: str(Path(tmp_path, *parts)))

    async def _executor(fn: Any, *args: Any) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args)

    hass.async_add_executor_job = _executor
    return hass


def _make_coordinator(cam_id: str = CAM_ID) -> Any:
    return SimpleNamespace(
        data={
            cam_id: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                },
                "events": [],
                "live": {},
            }
        },
        camera_entities={},
        image_entities={},
    )


def _make_entry() -> Any:
    return SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"enable_snapshots": True, "snapshot_interval": 1800},
    )


def _build_image_entity(
    hass: Any,
    coordinator: Any = None,
    cam_id: str = CAM_ID,
) -> Any:
    """Construct a BoschCameraLastSnapshotImage bypassing full HA entity lifecycle."""
    from custom_components.bosch_shc_camera.image import BoschCameraLastSnapshotImage

    if coordinator is None:
        coordinator = _make_coordinator(cam_id)

    entry = _make_entry()

    # Patch ImageEntity.__init__ to skip httpx client creation (needs real event loop)
    with patch(
        "custom_components.bosch_shc_camera.image.ImageEntity.__init__",
        lambda self, h, verify_ssl=False: None,
    ):
        entity = BoschCameraLastSnapshotImage(hass, coordinator, cam_id, entry)

    # Minimal attributes ImageEntity normally sets
    entity.hass = hass
    entity.access_tokens: Any = ["dummy-token"]  # type: ignore[assignment]
    entity._attr_image_last_updated = None

    return entity


@pytest.mark.asyncio
async def test_image_entity_not_created_when_snapshots_disabled(
    tmp_path: Path,
) -> None:
    """Image entities must NOT be created when enable_snapshots=False."""
    from custom_components.bosch_shc_camera.image import async_setup_entry

    coordinator = _make_coordinator()
    entry = SimpleNamespace(
        runtime_data=coordinator,
        options={"enable_snapshots": False},
    )

    added: list[Any] = []
    hass = _make_hass(tmp_path)

    with patch(
        "custom_components.bosch_shc_camera.image.get_options",
        return_value={"enable_snapshots": False},
    ):
        await async_setup_entry(
            hass, entry, lambda entities, **kw: added.extend(entities)
        )

    assert added == [], (
        "No image entities should be created when snapshots are disabled"
    )


@pytest.mark.asyncio
async def test_image_entity_created_when_snapshots_enabled(
    tmp_path: Path,
) -> None:
    """Image entities ARE created when enable_snapshots=True (default)."""
    from custom_components.bosch_shc_camera.image import (
        BoschCameraLastSnapshotImage,
        async_setup_entry,
    )

    coordinator = _make_coordinator()
    entry = SimpleNamespace(
        runtime_data=coordinator,
        options={"enable_snapshots": True},
    )

    added: list[Any] = []
    hass = _make_hass(tmp_path)

    with patch(
        "custom_components.bosch_shc_camera.image.get_options",
        return_value={"enable_snapshots": True},
    ):
        with patch(
            "custom_components.bosch_shc_camera.image.ImageEntity.__init__",
            lambda self, h, verify_ssl=False: None,
        ):
            await async_setup_entry(
                hass,
                entry,
                lambda entities, **kw: added.extend(entities),
            )

    assert len(added) == 1
    assert isinstance(added[0], BoschCameraLastSnapshotImage)


@pytest.mark.asyncio
async def test_async_image_returns_disk_bytes(tmp_path: Path) -> None:
    """async_image returns disk-persisted bytes when the file exists."""
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass = _make_hass(tmp_path)
    await save_snapshot(hass, CAM_ID, DISK_JPEG)

    entity = _build_image_entity(hass)

    result = await entity.async_image()
    assert result == DISK_JPEG


@pytest.mark.asyncio
async def test_async_image_disk_takes_priority_over_ram(tmp_path: Path) -> None:
    """When both disk and RAM cache are populated, disk bytes are returned."""
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass = _make_hass(tmp_path)
    await save_snapshot(hass, CAM_ID, DISK_JPEG)

    coordinator = _make_coordinator()
    cam_stub = SimpleNamespace(cached_image=RAM_JPEG)
    coordinator.camera_entities[CAM_ID] = cam_stub

    entity = _build_image_entity(hass, coordinator=coordinator)

    result = await entity.async_image()
    # Disk takes priority
    assert result == DISK_JPEG


@pytest.mark.asyncio
async def test_async_image_fallback_to_ram_cache(tmp_path: Path) -> None:
    """When no disk file exists, async_image falls back to camera.cached_image."""
    hass = _make_hass(tmp_path)

    coordinator = _make_coordinator()
    cam_stub = SimpleNamespace(cached_image=RAM_JPEG)
    coordinator.camera_entities[CAM_ID] = cam_stub

    entity = _build_image_entity(hass, coordinator=coordinator)

    result = await entity.async_image()
    assert result == RAM_JPEG


@pytest.mark.asyncio
async def test_async_image_placeholder_not_returned_as_fallback(
    tmp_path: Path,
) -> None:
    """The 1×1 black placeholder JPEG (≤200 B) is NOT served as a snapshot image."""
    hass = _make_hass(tmp_path)

    # Placeholder is tiny (~130 B) — entity must filter it out
    placeholder = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 120

    coordinator = _make_coordinator()
    cam_stub = SimpleNamespace(cached_image=placeholder)
    coordinator.camera_entities[CAM_ID] = cam_stub

    entity = _build_image_entity(hass, coordinator=coordinator)

    result = await entity.async_image()
    assert result is None, "Placeholder JPEG must not be served via image entity"


@pytest.mark.asyncio
async def test_async_image_returns_none_when_no_data(tmp_path: Path) -> None:
    """async_image returns None when disk is empty and RAM cache is None."""
    hass = _make_hass(tmp_path)

    coordinator = _make_coordinator()
    cam_stub = SimpleNamespace(cached_image=None)
    coordinator.camera_entities[CAM_ID] = cam_stub

    entity = _build_image_entity(hass, coordinator=coordinator)

    result = await entity.async_image()
    assert result is None


@pytest.mark.asyncio
async def test_async_image_no_camera_entity_registered(tmp_path: Path) -> None:
    """async_image returns None gracefully when the camera entity is not yet registered."""
    hass = _make_hass(tmp_path)

    coordinator = _make_coordinator()
    # No camera entity registered for CAM_ID

    entity = _build_image_entity(hass, coordinator=coordinator)

    result = await entity.async_image()
    assert result is None


@pytest.mark.asyncio
async def test_notify_refreshed_bumps_last_updated(tmp_path: Path) -> None:
    """async_notify_refreshed must set _attr_image_last_updated to a non-None datetime."""
    hass = _make_hass(tmp_path)
    entity = _build_image_entity(hass)

    assert entity._attr_image_last_updated is None

    write_state_calls: list[None] = []
    entity.async_write_ha_state = lambda: write_state_calls.append(None)  # type: ignore[method-assign]
    entity.async_update_token = lambda: None  # type: ignore[method-assign]

    await entity.async_notify_refreshed()

    assert entity._attr_image_last_updated is not None
    assert len(write_state_calls) == 1


@pytest.mark.asyncio
async def test_notify_refreshed_calls_write_ha_state(tmp_path: Path) -> None:
    """async_notify_refreshed must call async_write_ha_state to push WS update."""
    hass = _make_hass(tmp_path)
    entity = _build_image_entity(hass)

    calls: list[str] = []
    entity.async_write_ha_state = lambda: calls.append("write_state")  # type: ignore[method-assign]
    entity.async_update_token = lambda: None  # type: ignore[method-assign]

    await entity.async_notify_refreshed()

    assert "write_state" in calls


@pytest.mark.asyncio
async def test_notify_refreshed_called_twice_advances_timestamp(
    tmp_path: Path,
) -> None:
    """Each call to async_notify_refreshed should advance image_last_updated."""
    import asyncio as _asyncio

    hass = _make_hass(tmp_path)
    entity = _build_image_entity(hass)
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    entity.async_update_token = lambda: None  # type: ignore[method-assign]

    await entity.async_notify_refreshed()
    first_ts = entity._attr_image_last_updated

    await _asyncio.sleep(0.01)  # ensure wall-clock advances
    await entity.async_notify_refreshed()
    second_ts = entity._attr_image_last_updated

    assert second_ts is not None
    assert first_ts is not None
    assert second_ts >= first_ts


def test_unique_id_stable(tmp_path: Path) -> None:
    """Unique ID must be deterministic: {cam_id}_last_snapshot."""
    hass = _make_hass(tmp_path)
    entity = _build_image_entity(hass)
    assert entity._attr_unique_id == f"{CAM_ID}_last_snapshot"


def test_unique_id_stable_across_rebuilds(tmp_path: Path) -> None:
    """Two separately constructed entities for the same cam_id share the unique_id."""
    hass = _make_hass(tmp_path)
    e1 = _build_image_entity(hass)
    e2 = _build_image_entity(hass)
    assert e1._attr_unique_id == e2._attr_unique_id


def test_entity_registers_with_coordinator(tmp_path: Path) -> None:
    """The image entity must register itself in coordinator.image_entities."""
    hass = _make_hass(tmp_path)
    coordinator = _make_coordinator()

    entity = _build_image_entity(hass, coordinator=coordinator)

    assert coordinator.image_entities.get(CAM_ID) is entity


# In-RAM cache (perf 2026-06-18) — disk read only once per refresh
@pytest.mark.asyncio
async def test_async_image_caches_bytes_no_second_disk_read(tmp_path: Path) -> None:
    """Perf pin: repeated async_image() calls between refreshes must hit disk
    only once. Every /api/image_proxy request used to re-read the file."""
    hass = _make_hass(tmp_path)
    entity = _build_image_entity(hass)

    calls: list[str] = []

    async def _counting_load(_hass: Any, _cam_id: str) -> bytes:
        calls.append(_cam_id)
        return DISK_JPEG

    with patch(
        "custom_components.bosch_shc_camera.image.load_snapshot",
        side_effect=_counting_load,
    ):
        first = await entity.async_image()
        second = await entity.async_image()
        third = await entity.async_image()

    assert first == DISK_JPEG and second == DISK_JPEG and third == DISK_JPEG
    assert len(calls) == 1, (
        f"REGRESSION: async_image read disk {len(calls)}× for 3 requests — "
        "the in-RAM cache is not serving repeated requests."
    )


@pytest.mark.asyncio
async def test_notify_refreshed_invalidates_cache(tmp_path: Path) -> None:
    """A persisted refresh must invalidate the cache so the next request
    reloads the fresh frame from disk exactly once more."""
    hass = _make_hass(tmp_path)
    entity = _build_image_entity(hass)
    entity.async_write_ha_state = lambda: None  # type: ignore[method-assign]
    entity.async_update_token = lambda: None  # type: ignore[method-assign]

    calls: list[str] = []

    async def _counting_load(_hass: Any, _cam_id: str) -> bytes:
        calls.append(_cam_id)
        return DISK_JPEG

    with patch(
        "custom_components.bosch_shc_camera.image.load_snapshot",
        side_effect=_counting_load,
    ):
        await entity.async_image()  # disk read #1 → cached
        await entity.async_image()  # served from cache
        assert len(calls) == 1
        await entity.async_notify_refreshed()  # invalidate
        await entity.async_image()  # disk read #2 (fresh frame)
        await entity.async_image()  # served from cache again

    assert len(calls) == 2, (
        "REGRESSION: async_notify_refreshed did not invalidate the cache — "
        "stale frame would be served after a refresh."
    )


@pytest.mark.asyncio
async def test_ram_fallback_not_cached(tmp_path: Path) -> None:
    """The camera RAM-cache fallback (cold start, no disk file) must NOT be
    stored as our own cache — otherwise it would shadow the disk snapshot
    once it lands."""
    hass = _make_hass(tmp_path)
    coordinator = _make_coordinator()
    cam_stub = SimpleNamespace(cached_image=RAM_JPEG)
    coordinator.camera_entities[CAM_ID] = cam_stub
    entity = _build_image_entity(hass, coordinator=coordinator)

    # No disk file → fallback to RAM, but cache stays None.
    assert await entity.async_image() == RAM_JPEG
    assert entity._cached_bytes is None, (
        "RAM fallback must not populate the disk-snapshot cache."
    )


# Entity lifecycle hooks (relocated from tests/test_misc_small_gaps.py)
class TestImageEntityHooks:
    @pytest.mark.asyncio
    async def test_will_remove_pops_from_coordinator(self):
        from custom_components.bosch_shc_camera.image import (
            BoschCameraLastSnapshotImage,
        )

        coord = SimpleNamespace(image_entities={"C": "self_ref"})
        ent = BoschCameraLastSnapshotImage.__new__(BoschCameraLastSnapshotImage)
        ent._coordinator = coord
        ent._cam_id = "C"
        with patch(
            "custom_components.bosch_shc_camera.image.ImageEntity.async_will_remove_from_hass",
            new=MagicMock(return_value=None),
        ) as super_mock:

            async def _noop():
                return None

            super_mock.return_value = _noop()
            await BoschCameraLastSnapshotImage.async_will_remove_from_hass(ent)
        assert "C" not in coord.image_entities

    def test_device_info_full_payload(self):
        """`device_info` builds the device-registry payload from cached info."""
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


# ─────────────────────────────────────────────────────────────────────────────
# BoschAiLatestAlertImage — most recent AI Camera Analysis alert snapshot.
# Deliberately a plain ImageEntity (not CoordinatorEntity), refreshed by
# listening for the `bosch_shc_camera_ai_alert` bus event fired by
# ai_analysis._finalize_alert. See image.py class docstring.

CAM_ID_2 = "22222222-2222-2222-2222-222222222222"
ALERT_JPEG = b"\xff\xd8\xff\xe0" + b"\x03" * 300


def _make_ai_alert_coordinator(
    cam_ids: list[str] | None = None,
    image_path: str | None = "20260716-120000.jpg",
) -> Any:
    cam_ids = cam_ids or [CAM_ID]
    data = {}
    for cid in cam_ids:
        data[cid] = {
            "info": {
                "title": "Terrasse",
                "hardwareVersion": "HOME_Eyes_Outdoor",
                "firmwareVersion": "9.40.25",
                "macAddress": "aa:bb:cc:dd:ee:01",
            },
            "ai_analysis": {"image_path": image_path},
        }
    return SimpleNamespace(data=data, hass=None)


def _build_ai_alert_image_entity(
    hass: Any,
    coordinator: Any = None,
    cam_id: str = CAM_ID,
) -> Any:
    """Construct a BoschAiLatestAlertImage bypassing full HA entity lifecycle."""
    from custom_components.bosch_shc_camera.image import BoschAiLatestAlertImage

    if coordinator is None:
        coordinator = _make_ai_alert_coordinator([cam_id])
    coordinator.hass = hass

    entry = _make_entry()

    with patch(
        "custom_components.bosch_shc_camera.image.ImageEntity.__init__",
        lambda self, h, verify_ssl=False: None,
    ):
        entity = BoschAiLatestAlertImage(hass, coordinator, cam_id, entry)

    entity.hass = hass
    entity.access_tokens: Any = ["dummy-token"]  # type: ignore[assignment]
    entity._attr_image_last_updated = None
    return entity


@pytest.mark.asyncio
async def test_ai_alert_image_not_created_when_ai_analysis_disabled(
    tmp_path: Path,
) -> None:
    from custom_components.bosch_shc_camera.image import async_setup_entry

    coordinator = _make_coordinator()
    entry = SimpleNamespace(
        runtime_data=coordinator,
        options={"enable_snapshots": False, "ai_analysis_enabled": False},
    )
    added: list[Any] = []
    hass = _make_hass(tmp_path)

    with patch(
        "custom_components.bosch_shc_camera.image.get_options",
        return_value={"enable_snapshots": False, "ai_analysis_enabled": False},
    ):
        await async_setup_entry(
            hass, entry, lambda entities, **kw: added.extend(entities)
        )

    assert added == []


@pytest.mark.asyncio
async def test_ai_alert_image_created_when_ai_analysis_enabled(
    tmp_path: Path,
) -> None:
    from custom_components.bosch_shc_camera.image import (
        BoschAiLatestAlertImage,
        async_setup_entry,
    )

    coordinator = _make_coordinator()
    entry = SimpleNamespace(
        runtime_data=coordinator,
        options={"enable_snapshots": False, "ai_analysis_enabled": True},
    )
    added: list[Any] = []
    hass = _make_hass(tmp_path)

    with patch(
        "custom_components.bosch_shc_camera.image.get_options",
        return_value={"enable_snapshots": False, "ai_analysis_enabled": True},
    ):
        with patch(
            "custom_components.bosch_shc_camera.image.ImageEntity.__init__",
            lambda self, h, verify_ssl=False: None,
        ):
            await async_setup_entry(
                hass, entry, lambda entities, **kw: added.extend(entities)
            )

    assert len(added) == 1
    assert isinstance(added[0], BoschAiLatestAlertImage)


class TestAiLatestAlertImageAsyncImage:
    @pytest.mark.asyncio
    async def test_returns_bytes_when_alert_image_exists(self, tmp_path: Path) -> None:
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass)

        with patch(
            "custom_components.bosch_shc_camera.image.ai_alert_store.async_read_alert_image",
            AsyncMock(return_value=ALERT_JPEG),
        ):
            result = await entity.async_image()

        assert result == ALERT_JPEG

    @pytest.mark.asyncio
    async def test_returns_none_when_no_alert_yet(self, tmp_path: Path) -> None:
        hass = _make_hass(tmp_path)
        coordinator = _make_ai_alert_coordinator([CAM_ID], image_path=None)
        entity = _build_ai_alert_image_entity(hass, coordinator=coordinator)

        result = await entity.async_image()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_disk_read_fails(self, tmp_path: Path) -> None:
        """image_path set but the persisted file failed to read → safe None,
        matching ai_alert_store.async_read_alert_image's never-raises contract."""
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass)

        with patch(
            "custom_components.bosch_shc_camera.image.ai_alert_store.async_read_alert_image",
            AsyncMock(return_value=None),
        ):
            result = await entity.async_image()

        assert result is None

    @pytest.mark.asyncio
    async def test_caches_bytes_for_same_image_path(self, tmp_path: Path) -> None:
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass)

        reader = AsyncMock(return_value=ALERT_JPEG)
        with patch(
            "custom_components.bosch_shc_camera.image.ai_alert_store.async_read_alert_image",
            reader,
        ):
            first = await entity.async_image()
            second = await entity.async_image()

        assert first == ALERT_JPEG
        assert second == ALERT_JPEG
        assert reader.await_count == 1, (
            "REGRESSION: async_image re-read disk for the same image_path — "
            "the in-RAM cache is not serving repeated requests."
        )

    @pytest.mark.asyncio
    async def test_rereads_when_image_path_changes(self, tmp_path: Path) -> None:
        """A NEW alert (different image_path) must not serve the stale cache."""
        hass = _make_hass(tmp_path)
        coordinator = _make_ai_alert_coordinator(
            [CAM_ID], image_path="20260716-120000.jpg"
        )
        entity = _build_ai_alert_image_entity(hass, coordinator=coordinator)

        second_jpeg = b"\xff\xd8\xff\xe0" + b"\x04" * 300
        reader = AsyncMock(side_effect=[ALERT_JPEG, second_jpeg])
        with patch(
            "custom_components.bosch_shc_camera.image.ai_alert_store.async_read_alert_image",
            reader,
        ):
            first = await entity.async_image()
            coordinator.data[CAM_ID]["ai_analysis"]["image_path"] = (
                "20260716-130000.jpg"
            )
            second = await entity.async_image()

        assert first == ALERT_JPEG
        assert second == second_jpeg
        assert reader.await_count == 2


class TestAiLatestAlertImageAvailable:
    def test_available_true_when_image_path_set(self, tmp_path: Path) -> None:
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass)
        assert entity.available is True

    def test_available_false_when_no_alert_yet(self, tmp_path: Path) -> None:
        hass = _make_hass(tmp_path)
        coordinator = _make_ai_alert_coordinator([CAM_ID], image_path=None)
        entity = _build_ai_alert_image_entity(hass, coordinator=coordinator)
        assert entity.available is False


class TestAiLatestAlertImageEventListener:
    @pytest.mark.asyncio
    async def test_async_added_to_hass_registers_own_bus_listener(
        self, tmp_path: Path
    ) -> None:
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass)
        entity.async_on_remove = MagicMock()

        fake_hass = MagicMock()
        fake_hass.bus.async_listen = MagicMock(return_value=lambda: None)
        entity.hass = fake_hass

        with patch(
            "custom_components.bosch_shc_camera.image.ImageEntity.async_added_to_hass",
            new=AsyncMock(return_value=None),
        ):
            await entity.async_added_to_hass()

        fake_hass.bus.async_listen.assert_called_once()
        event_type = fake_hass.bus.async_listen.call_args.args[0]
        assert event_type == "bosch_shc_camera_ai_alert"

    @pytest.mark.asyncio
    async def test_bus_event_for_own_camera_triggers_state_refresh(
        self, tmp_path: Path
    ) -> None:
        """Firing the registered handler (as the real hass.bus would on a
        genuine `async_fire`) for THIS camera's alert must bump the image
        token and push a state write."""
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass, cam_id=CAM_ID)
        entity.async_on_remove = MagicMock()

        captured_handler: list[Any] = []

        def _capture_listen(event_type: str, handler: Any) -> Any:
            captured_handler.append(handler)
            return lambda: None

        fake_hass = MagicMock()
        fake_hass.bus.async_listen = MagicMock(side_effect=_capture_listen)
        entity.hass = fake_hass

        write_calls: list[None] = []
        entity.async_write_ha_state = lambda: write_calls.append(None)  # type: ignore[method-assign]
        entity.async_update_token = lambda: None  # type: ignore[method-assign]

        with patch(
            "custom_components.bosch_shc_camera.image.ImageEntity.async_added_to_hass",
            new=AsyncMock(return_value=None),
        ):
            await entity.async_added_to_hass()

        assert entity._attr_image_last_updated is None
        handler = captured_handler[0]
        # Simulate the real bus firing the event this handler was registered for.
        fake_event = SimpleNamespace(data={"camera_id": CAM_ID})
        handler(fake_event)

        assert entity._attr_image_last_updated is not None
        assert len(write_calls) == 1
        assert entity._cached_bytes is None
        assert entity._cached_image_path is None

    @pytest.mark.asyncio
    async def test_bus_event_for_other_camera_is_ignored(self, tmp_path: Path) -> None:
        """A multi-camera setup: this entity must NOT react to another
        camera's AI-alert event."""
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass, cam_id=CAM_ID)
        entity.async_on_remove = MagicMock()
        # Pre-populate the cache to prove it survives an unrelated event.
        entity._cached_bytes = ALERT_JPEG
        entity._cached_image_path = "some-path.jpg"

        captured_handler: list[Any] = []

        def _capture_listen(event_type: str, handler: Any) -> Any:
            captured_handler.append(handler)
            return lambda: None

        fake_hass = MagicMock()
        fake_hass.bus.async_listen = MagicMock(side_effect=_capture_listen)
        entity.hass = fake_hass

        write_calls: list[None] = []
        entity.async_write_ha_state = lambda: write_calls.append(None)  # type: ignore[method-assign]
        entity.async_update_token = lambda: None  # type: ignore[method-assign]

        with patch(
            "custom_components.bosch_shc_camera.image.ImageEntity.async_added_to_hass",
            new=AsyncMock(return_value=None),
        ):
            await entity.async_added_to_hass()

        handler = captured_handler[0]
        fake_event = SimpleNamespace(data={"camera_id": CAM_ID_2})
        handler(fake_event)

        assert entity._attr_image_last_updated is None
        assert write_calls == []
        # Cache must be untouched — proves the early-return guard fired.
        assert entity._cached_bytes == ALERT_JPEG
        assert entity._cached_image_path == "some-path.jpg"


class TestAiLatestAlertImageUniqueId:
    def test_unique_id(self, tmp_path: Path) -> None:
        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass)
        assert entity._attr_unique_id == f"bosch_shc_camera_{CAM_ID}_ai_latest_alert"


class TestAiLatestAlertImageDeviceInfo:
    def test_device_info_returns_identifiers(self, tmp_path: Path) -> None:
        from custom_components.bosch_shc_camera import DOMAIN

        hass = _make_hass(tmp_path)
        entity = _build_ai_alert_image_entity(hass)
        info = entity.device_info
        assert (DOMAIN, CAM_ID) in info["identifiers"]
        assert info["manufacturer"] == "Bosch"
