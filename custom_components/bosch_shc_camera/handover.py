"""Hand the setup over to the renamed integration (``bosch_smart_camera``).

From v18 the integration lives in ``custom_components/bosch_smart_camera``.
HACS leaves this old folder behind, so this code keeps running after the
update. Once the new folder exists, start the new integration's import flow
once: it copies login, options and entity IDs, so no manual step is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

NEW_DOMAIN = "bosch_smart_camera"
_FLAG_STORE_KEY = "bosch_shc_camera_handover"
_SCHEDULED_KEY = "bosch_shc_camera_handover_scheduled"


def _new_integration_installed(hass: HomeAssistant) -> bool:
    """Whether the renamed integration is on disk."""
    manifest = Path(hass.config.path("custom_components", NEW_DOMAIN, "manifest.json"))
    return manifest.is_file()


async def _async_run_handover(hass: HomeAssistant) -> None:
    """Start the import flow once, unless there is nothing to do."""
    if hass.config_entries.async_entries(NEW_DOMAIN):
        return
    if not await hass.async_add_executor_job(_new_integration_installed, hass):
        return
    store: Store[dict[str, bool]] = Store(hass, 1, _FLAG_STORE_KEY)
    if await store.async_load():
        return
    # Saved before the flow starts: a failure must never repeat on every restart.
    await store.async_save({"started": True})
    try:
        result = await hass.config_entries.flow.async_init(
            NEW_DOMAIN, context={"source": SOURCE_IMPORT}, data={}
        )
    except Exception:
        _LOGGER.warning("Automatic hand-over to %s failed", NEW_DOMAIN, exc_info=True)
        return
    _LOGGER.info("Hand-over to %s finished: %s", NEW_DOMAIN, result.get("type"))


def async_schedule_handover(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Schedule the hand-over after startup (at most once per run)."""
    if entry.disabled_by is not None or hass.data.get(_SCHEDULED_KEY):
        return
    hass.data[_SCHEDULED_KEY] = True

    async def _started(_hass: HomeAssistant) -> None:
        hass.async_create_background_task(
            _async_run_handover(hass), f"{entry.domain} hand-over"
        )

    async_at_started(hass, _started)
