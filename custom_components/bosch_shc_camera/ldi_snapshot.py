"""Snapshots of a local-data-interface camera without the Bosch cloud.

The camera's preview stream (`inst=3`, a JPEG stream refreshed once a second,
no audio) is registered as its own short-lived go2rtc producer
(`ldi_<id8>_snap`) and one frame is read from go2rtc's `/api/frame.jpeg`.
go2rtc was chosen over a one-shot ffmpeg read because it is already the only
component holding the camera password and the authenticated session (see
ldi_go2rtc.py), needs no extra process, and opens the camera session only
while a frame is being served.

The camera serves few concurrent sessions and Bosch asks for low request
rates, so a grabbed frame is shared for a few seconds between every entity
asking, and a failed grab is not retried for a little while. Without go2rtc,
while the privacy mode is on, or without a usable local source there is no
frame (None): the caller keeps its cached image and the cloud is never used.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from . import ldi_go2rtc
from .ldi_local import (
    LDI_INST_PREVIEW,
    VARIANT_SNAP,
    ldi_privacy_on,
    ldi_source,
    ldi_source_url,
)

if TYPE_CHECKING:  # pragma: no cover — only for type hints
    from . import BoschCameraCoordinator

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_SHARE_SEC = 5.0
SNAPSHOT_RETRY_SEC = 15.0


def _entry(coordinator: BoschCameraCoordinator, cam_id: str) -> dict[str, Any]:
    store: dict[str, dict[str, Any]] | None = getattr(
        coordinator, "ldi_snapshot_state", None
    )
    if store is None:
        store = {}
        coordinator.ldi_snapshot_state = store
    return store.setdefault(
        cam_id,
        {
            "data": None,
            "at": float("-inf"),
            "failed_at": float("-inf"),
            "lock": asyncio.Lock(),
        },
    )


async def fetch_snapshot(
    coordinator: BoschCameraCoordinator, cam_id: str
) -> bytes | None:
    """One JPEG frame from the camera's preview stream, shared and throttled."""
    if ldi_privacy_on(coordinator, cam_id) is True:
        return None  # the camera returns no video while privacy mode is on
    source = ldi_source(coordinator, cam_id)
    if source is None:
        return None
    ip, user, password = source
    entry = _entry(coordinator, cam_id)
    async with entry["lock"]:
        now = time.monotonic()
        if entry["data"] is not None and now - entry["at"] < SNAPSHOT_SHARE_SEC:
            return bytes(entry["data"])
        if now - entry["failed_at"] < SNAPSHOT_RETRY_SEC:
            return None
        url = await ldi_go2rtc.ensure_stream(
            coordinator,
            cam_id,
            ldi_source_url(ip, user, password, inst=LDI_INST_PREVIEW, audio=False),
            variant=VARIANT_SNAP,
        )
        frame = (
            await ldi_go2rtc.fetch_frame(coordinator, cam_id, VARIANT_SNAP)
            if url is not None
            else None
        )
        if frame is None:
            entry["failed_at"] = time.monotonic()
            _LOGGER.debug("local snapshot for %s not available", cam_id[:8])
            return None
        entry["data"] = frame
        entry["at"] = time.monotonic()
        return frame
