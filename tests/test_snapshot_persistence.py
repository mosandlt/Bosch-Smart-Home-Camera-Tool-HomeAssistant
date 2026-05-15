"""Tests for snapshot_store.py — disk-persistence helpers.

Covers: round-trip, atomic write, invalid cam_id, path traversal,
missing file, size guards, and privacy-mode gate in the camera entity.

Source: user report — iOS Companion App served yesterday's snapshot for ~5s
on cold-open (WKWebView heuristic cache). Fix: disk-persist + image entity.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

VALID_CAM_ID = "11111111-1111-1111-1111-111111111111"
VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200  # 204 B — passes min-size guard


def _make_hass(tmp_path: Path) -> Any:
    """Minimal hass stub that resolves .storage → tmp_path/.storage."""
    hass = SimpleNamespace()
    storage = tmp_path / ".storage"
    storage.mkdir()
    hass.config = SimpleNamespace(path=lambda *parts: str(Path(tmp_path, *parts)))

    async def _executor(fn: Any, *args: Any) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args)

    hass.async_add_executor_job = _executor
    return hass


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_load_roundtrip(tmp_path: Path) -> None:
    """Persisted bytes survive a save → load cycle."""
    from custom_components.bosch_shc_camera.snapshot_store import (
        load_snapshot,
        save_snapshot,
    )

    hass = _make_hass(tmp_path)
    await save_snapshot(hass, VALID_CAM_ID, VALID_JPEG)
    result = await load_snapshot(hass, VALID_CAM_ID)
    assert result == VALID_JPEG


@pytest.mark.asyncio
async def test_save_creates_directory(tmp_path: Path) -> None:
    """save_snapshot creates the snapshots/ directory if absent."""
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass = _make_hass(tmp_path)
    snap_dir = Path(tmp_path) / ".storage" / "bosch_shc_camera" / "snapshots"
    assert not snap_dir.exists()
    await save_snapshot(hass, VALID_CAM_ID, VALID_JPEG)
    assert snap_dir.exists()
    assert (snap_dir / f"{VALID_CAM_ID}.jpg").exists()


@pytest.mark.asyncio
async def test_atomic_write_no_tmp_left(tmp_path: Path) -> None:
    """After a successful save the .tmp file is gone (replaced)."""
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass = _make_hass(tmp_path)
    await save_snapshot(hass, VALID_CAM_ID, VALID_JPEG)
    snap_dir = Path(tmp_path) / ".storage" / "bosch_shc_camera" / "snapshots"
    tmp_file = snap_dir / f"{VALID_CAM_ID}.jpg.tmp"
    assert not tmp_file.exists(), "Temp file must be removed after atomic replace"


@pytest.mark.asyncio
async def test_overwrite_preserves_previous_on_exception(tmp_path: Path) -> None:
    """When Path.replace raises, the original file is left intact."""
    from custom_components.bosch_shc_camera import snapshot_store

    hass = _make_hass(tmp_path)
    original = b"\xff\xd8\xff\xe0" + b"\xab" * 200

    # Write original file directly
    snap_dir = Path(tmp_path) / ".storage" / "bosch_shc_camera" / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    final = snap_dir / f"{VALID_CAM_ID}.jpg"
    final.write_bytes(original)

    # Patch Path.replace to raise so the atomic swap fails
    with patch.object(Path, "replace", side_effect=OSError("disk full")):
        # _sync_save will raise — async_add_executor_job propagates it
        with pytest.raises(OSError, match="disk full"):
            await snapshot_store.save_snapshot(hass, VALID_CAM_ID, VALID_JPEG)

    # Original must be untouched
    assert final.read_bytes() == original


# --------------------------------------------------------------------------- #
# cam_id validation (path traversal prevention)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_invalid_cam_id_raises_value_error_save(tmp_path: Path) -> None:
    """save_snapshot raises ValueError for non-UUID cam_id."""
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass = _make_hass(tmp_path)
    with pytest.raises(ValueError, match="UUID format"):
        await save_snapshot(hass, "../../etc/passwd", VALID_JPEG)


@pytest.mark.asyncio
async def test_invalid_cam_id_raises_value_error_load(tmp_path: Path) -> None:
    """load_snapshot raises ValueError for non-UUID cam_id."""
    from custom_components.bosch_shc_camera.snapshot_store import load_snapshot

    hass = _make_hass(tmp_path)
    with pytest.raises(ValueError, match="UUID format"):
        await load_snapshot(hass, "../../etc/passwd")


@pytest.mark.asyncio
async def test_path_traversal_no_file_written(tmp_path: Path) -> None:
    """Path-traversal cam_id → ValueError raised, no file created outside storage."""
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass = _make_hass(tmp_path)
    with pytest.raises(ValueError):
        await save_snapshot(hass, "../../../tmp/evil", VALID_JPEG)

    # Confirm nothing was written anywhere outside tmp_path
    evil = Path("/tmp/evil.jpg")
    assert not evil.exists()


@pytest.mark.asyncio
async def test_lowercase_uuid_rejected(tmp_path: Path) -> None:
    """Lowercase UUIDs (as stored by HA entity_id slugs) are rejected.

    Bosch cam IDs are always upper-case hex. Lower-case means a caller
    accidentally passed a slugified entity_id — reject early.
    """
    from custom_components.bosch_shc_camera.snapshot_store import save_snapshot

    hass = _make_hass(tmp_path)
    lower_id = VALID_CAM_ID.lower()
    with pytest.raises(ValueError):
        await save_snapshot(hass, lower_id, VALID_JPEG)


# --------------------------------------------------------------------------- #
# Missing file
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_missing_returns_none(tmp_path: Path) -> None:
    """load_snapshot returns None when no file exists for that cam_id."""
    from custom_components.bosch_shc_camera.snapshot_store import load_snapshot

    hass = _make_hass(tmp_path)
    result = await load_snapshot(hass, VALID_CAM_ID)
    assert result is None


@pytest.mark.asyncio
async def test_load_oserror_returns_none_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """load_snapshot on non-FileNotFoundError OSError returns None + logs WARNING."""
    from custom_components.bosch_shc_camera import snapshot_store

    hass = _make_hass(tmp_path)

    with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
        result = await snapshot_store.load_snapshot(hass, VALID_CAM_ID)

    assert result is None
    assert any("failed to read snapshot" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# Size guards
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_too_small_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bytes < 100 are silently skipped (WARNING logged, no file written)."""
    from custom_components.bosch_shc_camera.snapshot_store import (
        load_snapshot,
        save_snapshot,
    )

    hass = _make_hass(tmp_path)
    await save_snapshot(hass, VALID_CAM_ID, b"\xff\xd8\xff" + b"\x00" * 10)  # 13 B
    result = await load_snapshot(hass, VALID_CAM_ID)
    assert result is None
    assert any("too small" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_too_large_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bytes > 10 MiB are silently skipped (WARNING logged, no file written)."""
    from custom_components.bosch_shc_camera.snapshot_store import (
        load_snapshot,
        save_snapshot,
    )

    hass = _make_hass(tmp_path)
    oversized = b"\xff\xd8\xff\xe0" + b"\x00" * (10 * 1024 * 1024 + 1)
    await save_snapshot(hass, VALID_CAM_ID, oversized)
    result = await load_snapshot(hass, VALID_CAM_ID)
    assert result is None
    assert any("too large" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_exact_min_size_accepted(tmp_path: Path) -> None:
    """Exactly 100 bytes passes the minimum size guard."""
    from custom_components.bosch_shc_camera.snapshot_store import (
        load_snapshot,
        save_snapshot,
    )

    hass = _make_hass(tmp_path)
    exactly_100 = b"\xff\xd8" + b"\x00" * 98  # 100 B
    await save_snapshot(hass, VALID_CAM_ID, exactly_100)
    result = await load_snapshot(hass, VALID_CAM_ID)
    assert result == exactly_100


# --------------------------------------------------------------------------- #
# Privacy mode gate in camera entity
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_privacy_mode_skips_save(tmp_path: Path) -> None:
    """When privacy_mode is ON the background refresh exits early — no save occurs.

    The privacy gate at the top of _async_trigger_image_refresh returns before
    any fetch/save logic runs. We verify no disk file is created.

    turbojpeg is mocked so the test runs without the libturbojpeg C extension.
    """
    import sys
    import types

    # Mock turbojpeg before importing camera (which imports homeassistant.components.camera
    # which imports turbojpeg at import time in img_util.py)
    fake_turbojpeg = types.ModuleType("turbojpeg")
    fake_turbojpeg.TurboJPEG = object  # type: ignore[attr-defined]
    sys.modules.setdefault("turbojpeg", fake_turbojpeg)

    # Also stub out the img_util path so the import chain resolves cleanly
    fake_img_util = types.ModuleType("homeassistant.components.camera.img_util")
    fake_img_util.TurboJPEGSingleton = object  # type: ignore[attr-defined]
    fake_img_util.scale_jpeg_camera_image = lambda *a, **kw: None  # type: ignore[attr-defined]
    sys.modules.setdefault("homeassistant.components.camera.img_util", fake_img_util)

    from custom_components.bosch_shc_camera.camera import BoschCamera

    cam_id = VALID_CAM_ID

    coordinator = SimpleNamespace(
        data={
            cam_id: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:33:14:ae",
                },
                "events": [],
                "live": {},
            }
        },
        _live_connections={},
        _camera_entities={},
        _image_entities={},
        _stream_fell_back={},
        _stream_error_count={},
        last_update_success=True,
        motion_settings=lambda cid: {},
        is_stream_warming=lambda cid: False,
        # Privacy ON for this camera
        _shc_state_cache={cam_id: {"privacy_mode": True}},
    )

    entry = SimpleNamespace(
        entry_id="01ENTRY",
        data={"bearer_token": "fake-token"},
        options={"snapshot_interval": 1800},
    )

    cam = BoschCamera(coordinator, cam_id, entry)

    saved_calls: list[Any] = []

    async def _mock_save(h: Any, cid: str, data: bytes) -> None:
        saved_calls.append((cid, data))

    hass = _make_hass(tmp_path)
    cam.hass = hass  # type: ignore[assignment]

    with patch(
        "custom_components.bosch_shc_camera.camera.save_snapshot",
        side_effect=_mock_save,
    ):
        await cam._async_trigger_image_refresh(delay=0)

    # Privacy mode gate must prevent any save_snapshot call
    assert saved_calls == [], (
        "save_snapshot must not be called while privacy mode is ON"
    )
