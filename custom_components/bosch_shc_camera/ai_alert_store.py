"""AI Camera Analysis — alert persistence (JSONL + snapshot images).

Mirrors `snapshot_store.py`'s atomic-write discipline (temp file → rename)
and `recorder.py`'s age-based retention-purge pattern, applied to
`ai_analysis.py`'s structured alert records instead of a single rolling
snapshot / video segments.

Layout: ``.storage/bosch_shc_camera/ai_alerts/<safe_cam_name>/``
  - ``alerts.jsonl`` — one JSON record per line, newest appended last.
  - ``images/<generated_at-as-filename>.jpg`` — the snapshot for that alert
    (best-effort; a record with no image still gets an ``alerts.jsonl``
    line, `image_path` is just ``None``).

All blocking I/O runs in ``hass.async_add_executor_job`` — never called
directly from the event loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .const import CONF_AI_ANALYSIS_RETENTION_DAYS
from .smb import _safe_name

if TYPE_CHECKING:
    from .coordinator import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

_STORE_DIRNAME = "ai_alerts"
_JSONL_NAME = "alerts.jsonl"
_IMAGES_DIRNAME = "images"
# Bound the in-memory recent-alerts cache regardless of configured
# retention_days — repeat-context only ever looks back
# ai_analysis_repeat_context_minutes (max 120 per the config-flow schema),
# so a generous fixed cap avoids unbounded growth on a very chatty camera
# without needing a second, separately-tuned option.
_RECENT_CACHE_MAX_PER_CAM = 200
# Timestamp-safe filename: ISO 8601 with colons replaced (Windows-hostile
# character, and this store may sit under a mounted share on some setups).
_TS_SAFE_RE = re.compile(r"[:.]")


def _cam_dir(hass_config_path: str, cam_name: str) -> str:
    return os.path.join(
        hass_config_path,
        ".storage",
        "bosch_shc_camera",
        _STORE_DIRNAME,
        _safe_name(cam_name),
    )


def _ts_to_filename(generated_at: str) -> str:
    return _TS_SAFE_RE.sub("-", generated_at) + ".jpg"


def _sync_append_and_prune(
    cam_dir: str,
    record: dict[str, Any],
    image_bytes: bytes | None,
    retention_days: int,
) -> str | None:
    """Blocking: write the image (if any), append the JSONL record, and
    prune anything past ``retention_days``. Runs inside an executor job.
    Returns the image's relative path (for the record), or None if no
    image was written. Never raises — a storage failure must not break the
    AI-analysis pipeline; logs and returns None on any OSError.
    """
    try:
        os.makedirs(cam_dir, 0o755, exist_ok=True)
    except OSError as err:
        _LOGGER.warning("AI alert store: cannot create %s: %s", cam_dir, err)
        return None

    image_path: str | None = None
    if image_bytes:
        images_dir = os.path.join(cam_dir, _IMAGES_DIRNAME)
        try:
            os.makedirs(images_dir, 0o755, exist_ok=True)
            fname = _ts_to_filename(str(record.get("generated_at", "")))
            final = os.path.join(images_dir, fname)
            tmp = final + ".tmp"
            with open(tmp, "wb") as f:
                f.write(image_bytes)
            os.replace(tmp, final)
            image_path = os.path.join(_IMAGES_DIRNAME, fname)
        except OSError as err:
            _LOGGER.debug("AI alert store: image write failed: %s", err)

    record_with_image = {**record, "image_path": image_path}
    jsonl_path = os.path.join(cam_dir, _JSONL_NAME)
    try:
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record_with_image, ensure_ascii=False) + "\n")
    except OSError as err:
        _LOGGER.warning("AI alert store: cannot append %s: %s", jsonl_path, err)
        return image_path

    if retention_days > 0:
        _sync_prune(cam_dir, retention_days)

    return image_path


def _sync_prune(cam_dir: str, retention_days: int) -> None:
    """Blocking: rewrite ``alerts.jsonl`` keeping only records newer than
    ``retention_days``, deleting the corresponding orphaned images.
    Best-effort — any parse/IO error aborts the prune for this tick without
    touching existing data (matches `recorder.py`'s prune-is-never-fatal
    discipline)."""
    import datetime

    jsonl_path = os.path.join(cam_dir, _JSONL_NAME)
    try:
        with open(jsonl_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return

    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=retention_days
    )
    kept: list[str] = []
    dropped_images: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            gen_at = datetime.datetime.fromisoformat(rec.get("generated_at", ""))
        except (ValueError, TypeError, json.JSONDecodeError):
            kept.append(line)  # malformed record — keep, don't silently lose data
            continue
        if gen_at >= cutoff:
            kept.append(line)
        elif rec.get("image_path"):
            dropped_images.append(rec["image_path"])

    if len(kept) == len(lines):
        return  # nothing pruned — skip the rewrite

    try:
        tmp = jsonl_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))
        os.replace(tmp, jsonl_path)
    except OSError as err:
        _LOGGER.debug("AI alert store: prune rewrite failed: %s", err)
        return

    for rel in dropped_images:
        try:
            os.unlink(os.path.join(cam_dir, rel))
        except OSError:
            pass


async def async_store_alert(
    coordinator: BoschCameraCoordinator,
    cam_id: str,
    result: dict[str, Any],
    generated_at: str,
    image_bytes: bytes | None,
) -> dict[str, Any] | None:
    """Persist one AI-analysis alert for *cam_id*. Returns
    ``{"image_path": <path-or-None>}`` on success, or None if the camera
    isn't known (never raises for storage failures — those degrade to a
    logged warning + a record with no image, see `_sync_append_and_prune`).

    Also updates the in-memory recent-alerts cache
    (`coordinator.ai_analysis_recent`) that `ai_analysis.py`'s
    repeat-context prompt hint reads synchronously.
    """
    cam_name = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    cam_dir = _cam_dir(coordinator.hass.config.path(), cam_name)
    retention_days = int(
        coordinator.options.get(CONF_AI_ANALYSIS_RETENTION_DAYS, 30) or 0
    )
    record = {**result, "cam_id": cam_id, "generated_at": generated_at}

    image_path = await coordinator.hass.async_add_executor_job(
        _sync_append_and_prune, cam_dir, record, image_bytes, retention_days
    )

    recent = coordinator.ai_analysis_recent.setdefault(cam_id, [])
    recent.append((generated_at, int(result.get("score", 0))))
    if len(recent) > _RECENT_CACHE_MAX_PER_CAM:
        del recent[: len(recent) - _RECENT_CACHE_MAX_PER_CAM]

    return {"image_path": image_path}


def recent_alerts(
    coordinator: BoschCameraCoordinator, cam_id: str, *, minutes: int
) -> list[tuple[str, int]]:
    """Return this camera's (generated_at, score) alerts from the last
    *minutes* minutes — pure, synchronous, in-memory only (no disk I/O), so
    it's safe to call from `ai_analysis.py`'s prompt builder without
    blocking the event loop. Backed by `coordinator.ai_analysis_recent`,
    populated by `async_store_alert`.
    """
    import datetime

    if minutes <= 0:
        return []
    entries = coordinator.ai_analysis_recent.get(cam_id, [])
    if not entries:
        return []
    cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes)
    out: list[tuple[str, int]] = []
    for generated_at, score in entries:
        try:
            gen_dt = datetime.datetime.fromisoformat(generated_at)
        except (ValueError, TypeError):
            continue
        if gen_dt >= cutoff:
            out.append((generated_at, score))
    return out


async def async_load_recent_alerts(coordinator: BoschCameraCoordinator) -> None:
    """Rebuild the in-memory recent-alerts cache from each known camera's
    ``alerts.jsonl`` tail on integration setup (the cache is otherwise
    empty after a restart, which would silently disable repeat-context for
    up to `ai_analysis_repeat_context_minutes` after every restart).

    A per-camera failure here (unreadable dir, executor error) must not
    abort setup entirely for every OTHER camera too, or integration setup
    as a whole — this is a best-effort cache warm-up, not a critical path.
    Matches this codebase's general "startup enhancement
    must never break setup" discipline (see `async_load_ai_budget`'s own
    try/except around its store load).
    """
    for cam_id, cam_data in coordinator.data.items():
        cam_name = cam_data.get("info", {}).get("title", cam_id)
        cam_dir = _cam_dir(coordinator.hass.config.path(), cam_name)
        try:
            entries = await coordinator.hass.async_add_executor_job(
                _sync_read_recent_tail, cam_dir
            )
        except Exception as err:
            _LOGGER.debug(
                "AI alert store: recent-alerts cache warm-up failed for %s: %s",
                cam_id[:8],
                err,
            )
            continue
        if entries:
            coordinator.ai_analysis_recent[cam_id] = entries


def _sync_read_image(cam_dir: str, image_path: str) -> bytes | None:
    """Blocking: read one alert image, guarding against path traversal.

    `image_path` is normally our own trusted value (written by
    `_sync_append_and_prune`, read back off `coordinator.data[cam_id]
    ["ai_analysis"]["image_path"]`), but the `image` platform ultimately
    calls this with whatever that dict currently holds — resolve + verify
    the final path stays under `cam_dir` before opening it (same discipline
    as this repo's media_source path-traversal guard), rather than trusting
    the string blindly. Never raises — returns None on any I/O/traversal
    problem.
    """
    try:
        base = Path(cam_dir).resolve()
        full = (base / image_path).resolve()
        full.relative_to(base)  # raises ValueError if full escapes base
    except (OSError, ValueError):
        return None
    try:
        return full.read_bytes()
    except OSError:
        return None


async def async_read_alert_image(
    coordinator: BoschCameraCoordinator, cam_id: str, image_path: str | None
) -> bytes | None:
    """Read one persisted alert image off disk for the `image` platform.

    Blocking I/O runs in an executor job (module docstring). Returns None
    if `image_path` is falsy, the camera is unknown, or the read fails —
    the `image` entity treats that as "no image available yet", never as
    an error.
    """
    if not image_path:
        return None
    cam_name = coordinator.data.get(cam_id, {}).get("info", {}).get("title", cam_id)
    cam_dir = _cam_dir(coordinator.hass.config.path(), cam_name)
    image_bytes: bytes | None = await coordinator.hass.async_add_executor_job(
        _sync_read_image, cam_dir, image_path
    )
    return image_bytes


def _sync_read_recent_tail(cam_dir: str) -> list[tuple[str, int]]:
    """Blocking: read up to the last `_RECENT_CACHE_MAX_PER_CAM` lines of
    ``alerts.jsonl`` and return (generated_at, score) tuples. Runs inside an
    executor job; never raises — a missing/corrupt file just yields []."""
    jsonl_path = os.path.join(cam_dir, _JSONL_NAME)
    try:
        lines = Path(jsonl_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[tuple[str, int]] = []
    for line in lines[-_RECENT_CACHE_MAX_PER_CAM:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        gen_at = rec.get("generated_at")
        if isinstance(gen_at, str):
            try:
                out.append((gen_at, int(rec.get("score", 0))))
            except (TypeError, ValueError):
                continue
    return out
