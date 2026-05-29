"""Regression: a data-only config-entry write must NEVER reload the entry.

Incident 2026-05-29 (card v13.3.0): user toggled privacy mode on the Terrasse
camera while the Innenbereich camera was live-streaming via WebRTC. The toggle's
coordinator refresh persisted a refreshed bearer token via
`async_update_entry(entry, data=...)` (__init__.py:1560). That fired the
`_async_options_updated` listener while `entry.runtime_data` was briefly None
(reload / startup window). The old guard read:

    coord = getattr(entry, "runtime_data", None)
    if coord:                       # <-- None → block skipped entirely
        ...compare options, maybe return...
    await hass.config_entries.async_reload(entry.entry_id)   # <-- always runs

So a data-only write with coord=None fell straight through to a FULL reload,
which tore down every camera's live stream (go2rtc unregister + TLS-proxy stop).
The Innenbereich WebRTC source vanished from go2rtc → DESCRIBE 404 → the card
fell back to a >30 s-delayed HLS stream.

These data-only writes happen constantly in the background (token refresh +
five FCM `data=` writes in fcm.py). The reload decision must depend ONLY on
whether the *options* actually changed — never on whether the coordinator
happens to be present. The previous-options snapshot therefore lives in
hass.data (keyed by entry_id) so it survives the runtime_data=None window.

Pins: data-only write (coord present OR None) → no reload; real options change
→ reload + snapshot refreshed; unknown (no snapshot, no coord) → safe reload.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import (
    OPTIONS_SNAPSHOT_KEY,
    _async_options_updated,
)

MODULE = "custom_components.bosch_shc_camera"
ENTRY_ID = "01KM38DHZ525S61HPENAT7NHC0"
OPTS = {"scan_interval": 60, "enable_fcm_push": True}


def _entry(*, runtime_data, options=None):
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.runtime_data = runtime_data
    entry.options = options if options is not None else {}
    entry.data = {}
    return entry


def _hass(snapshot=None):
    hass = MagicMock()
    hass.data = {} if snapshot is None else {OPTIONS_SNAPSHOT_KEY: dict(snapshot)}
    hass.config_entries.async_reload = AsyncMock()
    return hass


@pytest.mark.asyncio
async def test_data_only_write_with_none_coord_does_not_reload():
    """THE incident: token written, runtime_data=None, options unchanged → no reload."""
    hass = _hass(snapshot={ENTRY_ID: OPTS})
    entry = _entry(runtime_data=None)
    with patch(f"{MODULE}.get_options", return_value=dict(OPTS)):
        await _async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_data_only_write_with_coord_present_does_not_reload():
    """Normal case: coord present, options unchanged → no reload (kept behavior)."""
    hass = _hass(snapshot={ENTRY_ID: OPTS})
    coord = MagicMock()
    coord._options_snapshot = dict(OPTS)
    entry = _entry(runtime_data=coord)
    with patch(f"{MODULE}.get_options", return_value=dict(OPTS)):
        await _async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_options_change_reloads_and_refreshes_snapshot():
    """User edits an option (e.g. scan_interval) → reload + hass.data snapshot updated."""
    hass = _hass(snapshot={ENTRY_ID: OPTS})
    entry = _entry(runtime_data=None)
    new_opts = {"scan_interval": 30, "enable_fcm_push": True}
    with patch(f"{MODULE}.get_options", return_value=dict(new_opts)):
        await _async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(ENTRY_ID)
    assert hass.data[OPTIONS_SNAPSHOT_KEY][ENTRY_ID] == new_opts


@pytest.mark.asyncio
async def test_no_snapshot_and_no_coord_reloads_safely():
    """Unknown previous options (no snapshot, no coord) → safe fallback = reload."""
    hass = _hass(snapshot=None)
    entry = _entry(runtime_data=None)
    with patch(f"{MODULE}.get_options", return_value=dict(OPTS)):
        await _async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with(ENTRY_ID)


@pytest.mark.asyncio
async def test_coord_snapshot_used_when_hass_data_absent():
    """Fallback to coord._options_snapshot when hass.data has no snapshot yet."""
    hass = _hass(snapshot=None)
    coord = MagicMock()
    coord._options_snapshot = dict(OPTS)
    entry = _entry(runtime_data=coord)
    with patch(f"{MODULE}.get_options", return_value=dict(OPTS)):
        await _async_options_updated(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()
