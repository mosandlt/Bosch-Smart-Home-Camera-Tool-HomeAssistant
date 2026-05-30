"""Bosch Smart Home Camera — Snapshot Persistence.

Async-safe disk helpers to persist the latest JPEG snapshot per camera across
HA restarts. Stored in .storage/bosch_shc_camera/snapshots/{cam_id}.jpg.

All blocking I/O is wrapped in hass.async_add_executor_job so the event loop
is never blocked. Writes are atomic: a temp file is written first, then
renamed to the final path so a crash mid-write leaves the previous file intact.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Bosch camera IDs are UUID-formatted: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
# All hex upper-case, 8-4-4-4-12 groups separated by hyphens.
_CAM_ID_RE = re.compile(
    r"^[A-F0-9]{8}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{4}-[A-F0-9]{12}$"
)

# Sanity bounds for snapshot byte sizes.
# Bosch snapshots are 50–800 KiB typically; 100 B is the smallest valid JPEG.
_MIN_JPEG_BYTES = 100
_MAX_JPEG_BYTES = 10 * 1024 * 1024  # 10 MiB hard cap


def _validate_cam_id(cam_id: str) -> None:
    """Raise ValueError when cam_id is not a valid Bosch UUID.

    Enforced to prevent path-traversal attacks via crafted cam_id values
    (e.g. '../../etc/passwd'). Bosch UUIDs are always UUID-shaped hex+hyphen.
    """
    if not _CAM_ID_RE.match(cam_id):
        raise ValueError(
            f"cam_id must match ^[A-F0-9-]{{36}}$ (UUID format), got: {cam_id!r}"
        )


def _storage_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(".storage")) / "bosch_shc_camera" / "snapshots"


def _snap_path(hass: HomeAssistant, cam_id: str) -> Path:
    return _storage_dir(hass) / f"{cam_id}.jpg"


def _sync_save(hass: HomeAssistant, cam_id: str, jpeg: bytes) -> None:
    """Blocking: atomically write *jpeg* to the snapshot store.

    Called via async_add_executor_job — never call directly from async code.
    """
    snap_dir = _storage_dir(hass)
    snap_dir.mkdir(parents=True, exist_ok=True)
    final = snap_dir / f"{cam_id}.jpg"
    tmp = snap_dir / f"{cam_id}.jpg.tmp"
    tmp.write_bytes(jpeg)
    tmp.replace(final)


def _sync_load(hass: HomeAssistant, cam_id: str) -> bytes | None:
    """Blocking: read and return persisted snapshot bytes, or None if absent.

    Called via async_add_executor_job — never call directly from async code.
    """
    snap_path = _snap_path(hass, cam_id)
    try:
        return snap_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as err:
        _LOGGER.warning(
            "bosch_shc_camera: failed to read snapshot for %s: %s", cam_id, err
        )
        return None


async def save_snapshot(hass: HomeAssistant, cam_id: str, jpeg: bytes) -> None:
    """Async: validate and atomically persist *jpeg* for *cam_id*.

    Silently skips (with WARNING) when:
    - *jpeg* is smaller than 100 bytes (corrupt / not a real snapshot)
    - *jpeg* is larger than 10 MiB (unexpected; would waste disk I/O)

    Raises ValueError when *cam_id* is not a valid UUID — callers must ensure
    only real Bosch camera IDs are passed (prevents path traversal).
    """
    _validate_cam_id(cam_id)
    n = len(jpeg)
    if n < _MIN_JPEG_BYTES:
        _LOGGER.warning(
            "bosch_shc_camera: snapshot for %s too small (%d B) — skipping persist",
            cam_id,
            n,
        )
        return
    if n > _MAX_JPEG_BYTES:
        _LOGGER.warning(
            "bosch_shc_camera: snapshot for %s too large (%d B > %d B) — skipping persist",
            cam_id,
            n,
            _MAX_JPEG_BYTES,
        )
        return
    await hass.async_add_executor_job(_sync_save, hass, cam_id, jpeg)


async def load_snapshot(hass: HomeAssistant, cam_id: str) -> bytes | None:
    """Async: load persisted snapshot bytes for *cam_id*, or None if absent.

    Raises ValueError when *cam_id* is not a valid UUID.
    Returns None on FileNotFoundError; logs WARNING on other OSError.
    """
    _validate_cam_id(cam_id)
    return await hass.async_add_executor_job(_sync_load, hass, cam_id)  # type: ignore[no-any-return]  # value is correct at runtime; HA/external source is Any-typed
