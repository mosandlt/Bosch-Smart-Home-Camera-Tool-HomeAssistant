"""Persisted last-confirmed privacy state (offline camera after HA restart).

An offline camera never re-reports privacy, so the fail-closed snapshot gate
served the placeholder forever. The last explicit state is persisted in a
private Store and consulted only while no live value exists.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import privacy_store as ps
from custom_components.bosch_shc_camera.privacy_store import (
    PersistedPrivacy,
    PrivacyTrackingCacheView,
    effective_privacy,
)
from custom_components.bosch_shc_camera.session_state import CameraSessionState

CAM = "11111111-1111-1111-1111-111111111111"
CAM_B = "AABBCCDD-1111-1111-1111-111111111111"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 400


class _FakeStore:
    saved: dict[str, Any] | None
    loaded: Any

    def __init__(self, hass: Any, *, version: int, key: str) -> None:
        self.key = key
        self.loaded = None
        self.saved = None
        self.delay_calls = 0

    async def async_load(self) -> Any:
        return self.loaded

    async def async_save(self, data: Any) -> None:
        self.save_calls = getattr(self, "save_calls", 0) + 1
        if getattr(self, "save_error", None):
            raise self.save_error
        self.saved = data

    def async_delay_save(self, data_func: Any, delay: float) -> None:
        self.delay_calls += 1
        self.saved = data_func()


def _make(loaded: Any = None) -> PersistedPrivacy:
    with patch.object(ps, "Store", _FakeStore):
        p = PersistedPrivacy(MagicMock())
    p._store.loaded = loaded  # type: ignore[attr-defined]
    return p


def _entry(value: Any, v: int = 1) -> dict[str, Any]:
    return {"v": v, "privacy": value, "confirmed_at": 1.0, "source": "cache"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loaded", "expected"),
    [
        ({CAM: _entry(False)}, False),
        ({CAM: _entry(True)}, True),
        ({CAM: _entry(None)}, None),
        ({CAM: _entry("off")}, None),
        ({CAM: _entry(False, v=2)}, None),
        ({CAM: "garbage"}, None),
        ({CAM: {}}, None),
        ("garbage", None),
        ([1, 2], None),
        (None, None),
        ({}, None),
    ],
)
async def test_restore_modes(loaded: Any, expected: bool | None) -> None:
    p = _make(loaded)
    await p.async_load()
    assert p.get(CAM) is expected
    assert p.get(CAM.lower()) is expected


@pytest.mark.asyncio
async def test_non_str_key_ignored() -> None:
    p = _make({5: _entry(False)})
    await p.async_load()
    assert p._data == {}


@pytest.mark.asyncio
async def test_value_recorded_during_load_beats_disk() -> None:
    p = _make({CAM: _entry(False)})
    p.record(CAM, True)
    await p.async_load()
    assert p.get(CAM) is True


def test_get_unknown_and_bad_stored_value() -> None:
    p = _make()
    assert p.get(CAM) is None
    p._data[CAM] = {"privacy": "x"}
    assert p.get(CAM) is None


def test_record_saves_only_on_change_and_remove() -> None:
    p = _make()
    p.record(CAM, False)
    p.record(CAM, False)
    assert p._store.delay_calls == 1  # type: ignore[attr-defined]
    p.record(CAM, True)
    assert p._store.delay_calls == 2  # type: ignore[attr-defined]
    saved = p._store.saved  # type: ignore[attr-defined]
    assert saved[CAM]["privacy"] is True
    assert saved[CAM]["v"] == 1
    assert isinstance(saved[CAM]["confirmed_at"], float)
    p.remove(CAM)
    assert p._store.saved == {}  # type: ignore[attr-defined]
    calls = p._store.delay_calls  # type: ignore[attr-defined]
    p.remove(CAM)  # already gone: no extra save
    assert p._store.delay_calls == calls  # type: ignore[attr-defined]


def _view() -> tuple[PrivacyTrackingCacheView, PersistedPrivacy]:
    p = _make()
    sessions: dict[str, CameraSessionState] = {}
    return PrivacyTrackingCacheView(sessions, "shc_state_cache", p.record), p


def test_every_writer_shape_is_persisted() -> None:
    view, p = _view()
    # setdefault with initial dict (slow-tier / shc.py shape): None is ignored
    cache = view.setdefault(CAM, {"privacy_mode": None, "x": 1})
    assert p.get(CAM) is None
    cache["privacy_mode"] = False
    assert p.get(CAM) is False
    # nested subscript write (cloud setter) after a stored False
    view[CAM]["privacy_mode"] = True
    assert p.get(CAM) is True
    # setdefault(...)["privacy_mode"] (shc.py shape)
    view.setdefault(CAM_B, {})["privacy_mode"] = False
    assert p.get(CAM_B) is False
    # dict.update and dict.setdefault on the entry
    view[CAM].update({"privacy_mode": False})
    assert p.get(CAM) is False
    view[CAM].setdefault("privacy_mode", True)  # present: untouched
    assert p.get(CAM) is False
    view[CAM_B].pop("privacy_mode")
    view[CAM_B].setdefault("privacy_mode", True)  # absent: recorded
    assert p.get(CAM_B) is True
    # whole-entry assignment with a privacy value
    view[CAM] = {"privacy_mode": True}
    assert p.get(CAM) is True
    # explicit None never erases the last confirmed state
    view[CAM]["privacy_mode"] = None
    assert p.get(CAM) is True
    # already-wrapped dict is not re-wrapped
    wrapped = view[CAM]
    view[CAM] = wrapped
    assert view[CAM] is wrapped


def test_non_dict_value_passthrough() -> None:
    view, _ = _view()
    view[CAM] = None  # type: ignore[assignment]
    assert view[CAM] is None


def test_live_value_wins_over_persisted() -> None:
    p = _make()
    p.record(CAM, False)
    coord = SimpleNamespace(
        shc_state_cache={CAM: {"privacy_mode": True}}, privacy_store=p
    )
    assert effective_privacy(coord, CAM) is True
    coord.shc_state_cache = {CAM: {"privacy_mode": None}}
    assert effective_privacy(coord, CAM) is False
    coord.shc_state_cache = {}
    assert effective_privacy(coord, CAM) is False
    assert effective_privacy(coord, CAM_B) is None


def test_effective_privacy_tolerates_missing_or_junk_store() -> None:
    coord = SimpleNamespace(shc_state_cache={})
    assert effective_privacy(coord, CAM) is None
    coord.privacy_store = MagicMock()
    assert effective_privacy(coord, CAM) is None


def test_coordinator_method_and_purge_cleanup() -> None:
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    p = _make()
    p.record(CAM, False)
    coord = SimpleNamespace(shc_state_cache={}, privacy_store=p)
    assert BoschCameraCoordinator.effective_privacy(coord, CAM) is False  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_concurrent_poll_vs_restore() -> None:
    import asyncio

    p = _make({CAM: _entry(False)})
    view = PrivacyTrackingCacheView({}, "shc_state_cache", p.record)

    async def poll() -> None:
        view.setdefault(CAM, {})["privacy_mode"] = True

    await asyncio.gather(poll(), p.async_load())
    assert p.get(CAM) is True


# ── image entity ──────────────────────────────────────────────────────────────


def _image_entity(tmp_path: Any, state: Any, persisted: bool | None) -> Any:
    from tests.test_image import _build_image_entity, _make_coordinator, _make_hass

    hass = _make_hass(tmp_path)
    coord = _make_coordinator(CAM)
    coord.shc_state_cache = {CAM: {"privacy_mode": state}} if state != "absent" else {}
    p = _make()
    if persisted is not None:
        p.record(CAM, persisted)
    coord.privacy_store = p
    return hass, _build_image_entity(hass, coordinator=coord, cam_id=CAM)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live", "persisted", "served"),
    [
        ("absent", False, True),
        (None, False, True),
        (False, None, True),
        (False, True, True),
        ("absent", True, False),
        ("absent", None, False),
        (True, False, False),
        (True, None, False),
    ],
)
async def test_image_entity_gate(
    tmp_path: Any, live: Any, persisted: bool | None, served: bool
) -> None:
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass, entity = _image_entity(tmp_path, live, persisted)
    await save_snapshot(hass, CAM, JPEG)
    assert await entity.async_image() == (JPEG if served else None)


@pytest.mark.asyncio
async def test_image_entity_gate_blocks_ram_cache_too(tmp_path: Any) -> None:
    _hass, entity = _image_entity(tmp_path, "absent", None)
    entity._coordinator.camera_entities[CAM] = SimpleNamespace(cached_image=JPEG)
    assert await entity.async_image() is None


# ── camera entity ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("persisted", "served"),
    [(False, True), (True, False), (None, False)],
)
async def test_camera_offline_after_restart(
    persisted: bool | None, served: bool
) -> None:
    from custom_components.bosch_shc_camera.camera import BoschCamera
    from tests.test_camera import (
        CAM_ID_GAPS,
        _make_camera_gaps,
        _make_coord_gaps,
    )

    coord = _make_coord_gaps()
    coord.live_connections = {CAM_ID_GAPS: {"rtspsUrl": "rtsps://192.168.1.1/s"}}
    coord.local_creds_cache = {}
    coord.auth_outage_count = 0
    coord.data[CAM_ID_GAPS]["events"] = []
    coord.shc_state_cache = {}  # restart: nothing live yet
    p = _make()
    if persisted is not None:
        p.record(CAM_ID_GAPS, persisted)
    coord.privacy_store = p

    cam = _make_camera_gaps(coord=coord)
    cam.cached_image = b"\xff\xd8\xff\xe0persisted_frame"
    cam._was_streaming = False
    cam.hass = SimpleNamespace(
        async_create_task=MagicMock(side_effect=lambda c: (c.close(), MagicMock())[1]),
        async_add_executor_job=AsyncMock(),
    )
    with (
        patch(
            "custom_components.bosch_shc_camera.camera.get_options",
            return_value={"use_mjpeg_snapshot": False},
        ),
        patch(
            "custom_components.bosch_shc_camera.camera.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=MagicMock()),
        ),
    ):
        result = await BoschCamera._async_camera_image_impl(cam)

    assert result == (b"\xff\xd8\xff\xe0persisted_frame" if served else None)


def test_ior_is_tracked() -> None:
    view, p = _view()
    cache = view.setdefault(CAM, {"x": 1})
    cache |= {"privacy_mode": True}
    assert p.get(CAM) is True
    assert cache["privacy_mode"] is True
    cache |= [("privacy_mode", False)]
    assert p.get(CAM) is False


@pytest.mark.asyncio
async def test_on_is_saved_immediately_off_is_delayed() -> None:
    p = _make()
    tasks: list[Any] = []

    def spawn(coro: Any, *, name: str) -> None:
        tasks.append(coro)

    p.set_spawner(spawn)
    store: Any = p._store
    p.record(CAM, False)
    assert store.delay_calls == 1
    assert not tasks
    p.record(CAM_B, True)
    assert store.delay_calls == 1
    assert len(tasks) == 1
    await tasks[0]
    assert store.save_calls == 1
    assert store.saved[CAM_B]["privacy"] is True
    assert store.saved[CAM]["privacy"] is False  # full snapshot


@pytest.mark.asyncio
async def test_immediate_save_failure_is_swallowed() -> None:
    p = _make()
    store: Any = p._store
    store.save_error = OSError("disk")
    await p._save_now()
    assert store.save_calls == 1


@pytest.mark.asyncio
async def test_load_failure_is_unknown_not_fatal() -> None:
    p = _make()

    async def boom() -> Any:
        raise NotImplementedError("newer store version")

    p._store.async_load = boom  # type: ignore[method-assign]
    await p.async_load()
    assert p.get(CAM) is None


# ── fail-closed races / local confirmation ────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("ram_fallback", [False, True])
async def test_image_privacy_flip_during_disk_read(
    tmp_path: Any, ram_fallback: bool
) -> None:
    from custom_components.bosch_shc_camera import image as image_mod
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass, entity = _image_entity(tmp_path, False, None)
    await save_snapshot(hass, CAM, JPEG)
    entity._coordinator.camera_entities[CAM] = SimpleNamespace(cached_image=JPEG)

    async def _flip(_hass: Any, _cam: str) -> bytes | None:
        entity._coordinator.shc_state_cache[CAM]["privacy_mode"] = True
        return None if ram_fallback else JPEG

    with patch.object(image_mod, "load_snapshot", _flip):
        assert await entity.async_image() is None
    assert entity._cached_bytes is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("flip", "served"), [(True, False), (False, True)])
async def test_camera_wrapper_rechecks_privacy_after_impl(
    flip: bool, served: bool
) -> None:
    from custom_components.bosch_shc_camera.camera import BoschCamera
    from tests.test_camera import _make_camera_gaps, _make_coord_gaps

    coord = _make_coord_gaps()
    coord.shc_state_cache = {CAM: {"privacy_mode": False}}
    coord.privacy_store = _make()
    cam = _make_camera_gaps(coord=coord)
    cam._cam_id = CAM
    cam.hass = SimpleNamespace(async_add_executor_job=AsyncMock())

    async def _impl(_w: Any = None, _h: Any = None) -> bytes:
        coord.shc_state_cache[CAM]["privacy_mode"] = flip
        return JPEG

    cam._async_camera_image_impl = _impl
    cam._async_cap_snapshot_width = AsyncMock(side_effect=lambda j: j)
    result = await BoschCamera.async_camera_image(cam)
    assert (result == JPEG) is served
    if not served:
        assert result is BoschCamera._PLACEHOLDER_JPEG


@pytest.mark.parametrize("value", [True, False])
def test_local_probe_value_reaches_tracked_cache_and_store(value: bool) -> None:
    from custom_components.bosch_shc_camera import ldi_local
    from tests.test_ldi_local import _coord, _probe

    c = _coord(privacy=not value)
    view, p = _view()
    c.shc_state_cache = view
    c.shc_state_cache[CAM] = {"privacy_mode": not value}
    ldi_local._apply_probe(c, CAM, _probe(200, value))  # type: ignore[arg-type]
    assert effective_privacy(c, CAM) is value
    assert p.get(CAM) is value


@pytest.mark.parametrize("status", [None, 401, 500])
def test_failed_probe_leaves_cache_untouched(status: int | None) -> None:
    from custom_components.bosch_shc_camera import ldi_local
    from tests.test_ldi_local import _coord, _probe

    c = _coord(privacy=False)
    ldi_local._apply_probe(c, CAM, _probe(status, True))  # type: ignore[arg-type]
    assert c.shc_state_cache[CAM]["privacy_mode"] is False
