"""Persisted last-confirmed privacy state per camera.

An offline camera never re-reports privacy after an HA restart, so the
fail-closed snapshot gate would hide its persisted JPEG forever. The last
explicitly confirmed state is kept in a private HA Store (never inside the
JPEG) and consulted only while no live value exists.

Known limit: privacy switched ON in the Bosch app while the camera is offline
and HA restarts keeps the stored False (nothing reports the change).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Self

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .session_state import CacheFieldView, CameraSessionState

_LOGGER = logging.getLogger(__name__)

STORE_KEY = f"{DOMAIN}_privacy_state"
STORE_VERSION = 1
_SAVE_DELAY_SEC = 1.0
_PRIVACY_KEY = "privacy_mode"


class PersistedPrivacy:
    """cam_id -> last confirmed privacy bool, saved via delayed Store writes."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, version=STORE_VERSION, key=STORE_KEY
        )
        self._data: dict[str, dict[str, Any]] = {}
        self._spawn: Callable[..., Any] | None = None

    def set_spawner(self, spawn: Callable[..., Any]) -> None:
        """Register the tracked-task helper used for immediate saves."""
        self._spawn = spawn

    async def async_load(self) -> None:
        """Merge valid persisted entries; anything malformed counts as unknown."""
        try:
            raw = await self._store.async_load()
        except Exception:  # newer-version/corrupt file must not abort setup
            _LOGGER.warning(
                "Persisted privacy state unreadable; treating all as unknown",
                exc_info=True,
            )
            return
        if not isinstance(raw, dict):
            return
        for cam_id, entry in raw.items():
            if (
                not isinstance(cam_id, str)
                or not isinstance(entry, dict)
                or entry.get("v") != STORE_VERSION
                or not isinstance(entry.get("privacy"), bool)
            ):
                continue
            # A value recorded while loading is newer than the disk copy.
            self._data.setdefault(cam_id.upper(), entry)

    def get(self, cam_id: str) -> bool | None:
        """Return the persisted state, or None when unknown."""
        entry = self._data.get(cam_id.upper())
        if entry is None:
            return None
        value = entry.get("privacy")
        return value if isinstance(value, bool) else None

    def record(self, cam_id: str, privacy: bool, source: str = "cache") -> None:
        """Remember a confirmed state; saves only when the value changed."""
        key = cam_id.upper()
        current = self._data.get(key)
        if current is not None and current.get("privacy") is privacy:
            return
        self._data[key] = {
            "v": STORE_VERSION,
            "privacy": privacy,
            "confirmed_at": time.time(),
            "source": source,
        }
        if privacy and self._spawn is not None:
            # A stale False on disk after a hard kill would show the offline
            # image despite privacy ON, so ON is flushed without the delay.
            self._spawn(self._save_now(), name="bosch_shc_camera_privacy_save")
        else:
            self._store.async_delay_save(self._snapshot, _SAVE_DELAY_SEC)

    async def _save_now(self) -> None:
        """Write the full current snapshot (also cancels a pending delayed save)."""
        try:
            await self._store.async_save(self._snapshot())
        except Exception:  # disk error must not break the writer
            _LOGGER.warning("Persisting privacy state failed", exc_info=True)

    def remove(self, cam_id: str) -> None:
        """Drop a camera's entry (device removal)."""
        if self._data.pop(cam_id.upper(), None) is not None:
            self._store.async_delay_save(self._snapshot, _SAVE_DELAY_SEC)

    def _snapshot(self) -> dict[str, Any]:
        return {cam_id: dict(entry) for cam_id, entry in self._data.items()}


class _PrivacyTrackingDict(dict[str, Any]):
    """Per-camera state dict reporting every explicit privacy_mode write."""

    def __init__(
        self,
        cam_id: str,
        on_privacy: Callable[[str, bool], None],
        initial: dict[str, Any],
    ) -> None:
        super().__init__()
        self._cam_id = cam_id
        self._on_privacy = on_privacy
        self.update(initial)

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        if key == _PRIVACY_KEY and isinstance(value, bool):
            self._on_privacy(self._cam_id, value)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args: Any, **kwargs: Any) -> None:
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def __ior__(self, other: Any) -> Self:  # type: ignore[override,misc]
        self.update(other)
        return self


class PrivacyTrackingCacheView(CacheFieldView[dict[str, Any]]):
    """shc_state_cache view: single choke point for every privacy writer."""

    def __init__(
        self,
        sessions: dict[str, CameraSessionState],
        field_name: str,
        on_privacy: Callable[[str, bool], None],
    ) -> None:
        super().__init__(sessions, field_name)
        self._on_privacy = on_privacy

    def setdefault(
        self, cam_id: str, default: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        # The mixin returns `default` itself; callers mutate the result, so it
        # must be the stored (tracking) object.
        if cam_id not in self:
            self[cam_id] = default if default is not None else {}
        return self[cam_id]

    def __setitem__(self, cam_id: str, value: dict[str, Any]) -> None:
        if isinstance(value, dict) and not isinstance(value, _PrivacyTrackingDict):
            value = _PrivacyTrackingDict(cam_id, self._on_privacy, value)
        super().__setitem__(cam_id, value)


def effective_privacy(coordinator: Any, cam_id: str) -> bool | None:
    """Live privacy state, else the persisted last-confirmed one, else None."""
    live = coordinator.shc_state_cache.get(cam_id, {}).get(_PRIVACY_KEY)
    if isinstance(live, bool):
        return live
    store = getattr(coordinator, "privacy_store", None)
    persisted = store.get(cam_id) if store is not None else None
    return persisted if isinstance(persisted, bool) else None
