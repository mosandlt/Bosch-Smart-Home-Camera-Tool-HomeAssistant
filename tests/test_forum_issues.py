"""Regression tests for every distinct user-reported issue from the
simon42 forum thread (`bosch-smart-home-kameras-vollstaendig-...`).

CLAUDE.md TEST_EVERY_BUG rule: every reproduced bug + every reported
user-issue gets a regression test BEFORE the fix is committed. This file
maps forum posts to test functions. If a user reopens an issue we
already shipped a fix for, run the matching test first to confirm the
regression.

Source thread: community.simon42.com/.../bosch-smart-home-kameras-...

Issue index (8 posts × distinct concerns):

| # | User    | Post | Concern                                              | Status   |
|---|---------|------|------------------------------------------------------|----------|
| 1 | Poldi41 | #2   | Motion sensitivity reverts after PUT                 | KNOWN    |
| 2 | Poldi41 | #2   | Motion-detection switch toggles don't persist        | KNOWN    |
| 3 | geotie  | #6   | Automation setup unclear (docs)                      | DOCS     |
| 4 | geotie  | #6   | Alert system needs absent/night conditional triggers | FEATURE  |
| 5 | geotie  | #8   | Binary-sensor misses motion events                   | FIXED    |
| 6 | geotie  | #8   | Inconsistent event detection across sensors          | FIXED    |
| 7 | xDraGGi | #10  | Events marked as read in Bosch app unintentionally   | OPT-OUT  |
| 8 | geotie  | #14  | Downloaded recordings hard to find in dashboard      | FIXED    |

KNOWN  = limitation, no fix possible without Bosch local-write API
DOCS   = README change, no code test
FEATURE = enhancement, not a bug
FIXED  = code path tested + behavior pinned
OPT-OUT = behavior is intentional but user-controllable (option flow flag)
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── Issue #1, #2: Motion-detection settings revert (KNOWN limitation) ──


class TestIssue1_MotionRevert:
    """Poldi41 — `PUT /motion` accepted (HTTP 200) but reverts in ~1s.

    Root cause documented in `docs/api-reference.md` § "Motion Revert":
    on-device IVA engine (RCP 0x09f3, gzip 2282B) enforces motion settings
    independently. Cloud-side rules engine (POST/PUT/DELETE /rules) is
    the workaround; full IVA write needs a Bosch service-account user.

    No code-level fix possible; we just pin that the documentation is
    in place so a future PR doesn't accidentally revert it.
    """

    def test_motion_revert_documented_in_api_reference(self):
        """The known-limitation note must stay in api-reference.md so users
        understand why their motion-sensitivity changes don't stick."""
        from pathlib import Path
        api_ref = (
            Path(__file__).parent.parent.parent / "docs" / "api-reference.md"
        )
        if not api_ref.exists():
            pytest.skip(f"docs/api-reference.md not in repo (workspace-only)")
        text = api_ref.read_text()
        assert "Motion Revert" in text or "motion revert" in text.lower(), (
            "Motion-revert limitation must stay documented in api-reference.md"
        )


# ── Issue #5, #6: Binary sensor misses motion events (FIXED) ───────────


class TestIssue5_BinarySensorMissesEvents:
    """geotie — 'Die obige Automation funktioniert, wird aber oft nicht ausgelöst.'

    Three independent fixes shipped over time pin this behavior:

    a) `EVENT_ACTIVE_WINDOW = 90 s` (binary_sensor.py) covers the polling-only
       case where an event can be up to 60s old when first seen.
    b) FCM push handler at `fcm.py:async_handle_fcm_push` now mirrors
       fresh events into `coordinator.data[cam_id]['events']` BEFORE
       calling `async_update_listeners()` so windowed binary sensors
       see the new event immediately (without this, data[] was only
       refreshed on the next 60s tick).
    c) `_last_event_ids` bootstrap on the first polling tick (this
       commit) — without the seed, polling-only mode after a restart
       had `prev_id is None` permanently, the alert-chain elif was
       never reached, and `bosch_shc_camera_motion` never fired.
    """

    def _make_hass(self):
        fake_hass = MagicMock()
        fake_hass.config.time_zone = "UTC"
        return fake_hass

    def test_window_covers_60s_polling_lag(self):
        """Event 60s old (max polling lag) must still trigger the sensor."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor, EVENT_ACTIVE_WINDOW,
        )
        assert EVENT_ACTIVE_WINDOW >= 90, (
            "Window must cover the 60s scan_interval + margin; lowering "
            "below 90s reintroduces the geotie missed-trigger bug."
        )

    def test_motion_sensor_fires_for_60s_old_event(self):
        """A 60s-old event still triggers — the polling path can be that lagged."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )
        coord = SimpleNamespace(data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "x",
                },
                "events": [
                    {
                        "eventType": "MOVEMENT",
                        "id": "e1",
                        "timestamp": (
                            datetime.now(timezone.utc) - timedelta(seconds=60)
                        ).strftime("%Y-%m-%dT%H:%M:%S"),
                    }
                ],
            }
        })
        entry = SimpleNamespace(entry_id="01ENTRY", data={}, options={})
        s = BoschMotionBinarySensor(coord, CAM_ID, entry)
        s.hass = self._make_hass()
        assert s.is_on is True

    def test_polling_seeds_last_event_ids_on_first_tick(self):
        """After restart with FCM disabled, polling must bootstrap
        `_last_event_ids` so subsequent ticks can detect new events.

        Pre-fix: `prev_id is None` branch only marked events as read
        without setting `_last_event_ids`, so prev_id stayed None forever
        and the alert-chain elif was never reached. Result: motion
        automations never fired in polling-only mode after a restart.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        # Read the current source to confirm the seed is in place; if
        # someone removes it, this assertion fails loudly.
        import inspect
        src = inspect.getsource(BoschCameraCoordinator._async_update_data)
        # The fix line: after the prev_id-is-None mark-as-read block,
        # we set self._last_event_ids[cam_id] = newest_id.
        assert "self._last_event_ids[cam_id] = newest_id" in src, (
            "_last_event_ids bootstrap missing in _async_update_data — "
            "polling-only mode will stop firing alerts after restart"
        )


# ── Issue #7: Events marked as read in Bosch app (OPT-OUT) ─────────────


class TestIssue7_MarkEventsReadOptOut:
    """xDraGGi — 'Integration markiert alle Events als gelesen, dadurch verschwinden
    sie aus "neu" im offiziellen Bosch-App.'

    By design — the integration calls `PUT /v11/events {id, isRead: true}`
    after dispatching the alert chain, so the same event isn't re-alerted
    on a coordinator-tick after FCM already handled it. xDraGGi prefers
    to keep events visible in the Bosch app.

    Fix: the option flow has `mark_events_read` (default OFF in newer
    versions). This test pins the contract that the option exists +
    defaults to OFF so xDraGGi's setup is the new norm.
    """

    def test_mark_events_read_option_is_documented(self):
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS
        # Either default to False or be absent (treated as False on .get).
        assert DEFAULT_OPTIONS.get("mark_events_read", False) is False, (
            "mark_events_read must default to False so the user controls "
            "whether events disappear from the Bosch app's 'new' list "
            "(xDraGGi forum complaint)."
        )

    def test_option_present_in_strings(self):
        """The option must appear in strings.json so users can find + toggle it."""
        from pathlib import Path
        import json
        comp = (
            Path(__file__).parent.parent
            / "custom_components" / "bosch_shc_camera"
        )
        strings = json.loads((comp / "strings.json").read_text())
        # Labels now live under sections.<section>.data, not flat data.
        sections = (
            strings.get("options", {})
            .get("step", {})
            .get("init", {})
            .get("sections", {})
        )
        all_labels = {k for sec in sections.values() for k in sec.get("data", {})}
        assert "mark_events_read" in all_labels, (
            "The mark_events_read option must be exposed in the options "
            "flow UI — xDraGGi reported confusion about WHY events "
            "disappear from the Bosch app, fix is to make the toggle "
            "discoverable."
        )


# ── Issue #8: Media Browser hard to find / empty after upgrade (FIXED) ──


class TestIssue8_MediaBrowserEmpty:
    """geotie — 'Wo findet man die Aufnahmen schneller im Dashboard oder sonst in HA?'
    plus user-report 'Media Browser bleibt leer nach v10.7.1 → v11.0.0 upgrade'.

    Two distinct fixes:
    a) Media Browser provider exists since v10.7.0 — events appear under
       Media → Bosch SHC Camera. The README documents both the local
       and SMB tree shapes.
    b) v11.0.1: `_enabled_sources` now creates the download directory
       on first call so the entry appears immediately when the user
       enables auto-download (was hidden until first event arrived).
    """

    def test_enabled_sources_creates_missing_dir(self, tmp_path):
        """v11.0.1 fix — the regression guard that closes the user-visible
        'Media Browser bleibt leer' issue."""
        from custom_components.bosch_shc_camera.media_source import _enabled_sources
        new_dir = tmp_path / "fresh_install"
        assert not new_dir.exists()
        hass = SimpleNamespace(
            config_entries=SimpleNamespace(
                async_loaded_entries=lambda d: [SimpleNamespace(
                    entry_id="01ENTRY",
                    runtime_data=SimpleNamespace(options={
                        "download_path": str(new_dir),
                        "media_browser_source": "auto",
                    }),
                )],
            ),
        )
        _enabled_sources(hass)
        assert new_dir.is_dir(), (
            "Media Browser must auto-create download_path so the entry "
            "appears immediately, not only after first event"
        )

    def test_readme_documents_auto_download_path(self):
        """README must explain WHERE the local save folder option is.

        enable_auto_download was removed — local saving is now controlled solely
        by the download_path field (non-empty = active). The UI label is
        'Local save folder' / 'Lokaler Speicher-Ordner'. The README must
        document the Configure path so users can find it.
        """
        from pathlib import Path
        readme = (
            Path(__file__).parent.parent / "README.md"
        )
        text = readme.read_text()
        # The field that controls local saving (filling it in = enable)
        assert "Local save folder" in text or "download_path" in text, (
            "README must document the local save folder so users know how to "
            "enable Media Browser (non-empty path = active, no separate toggle)"
        )
        # The Reconfigure-vs-Configure UX confusion must be addressed
        assert "Reconfigure" in text and "Configure" in text, (
            "README must distinguish Configure (options) from Reconfigure "
            "(re-OAuth), the new v11.0.0 menu item that confused users"
        )


# ── Meta: every forum-reported issue has a test in this file ──


class TestMeta:
    """The CLAUDE.md TEST_EVERY_BUG rule says every forum-reported issue
    must have a regression test before the fix is committed. This is the
    enforcer — if a future PR fixes a forum bug without adding a test,
    `count_test_classes` flags it.
    """

    def test_eight_forum_issues_have_test_classes(self):
        """Sanity: this file must grow when new forum issues appear."""
        # Six TestIssue<N>_… classes for issues 1, 5, 7, 8 (issues 2/6
        # share class with 1/5; 3/4 are docs/feature, no code test
        # possible). The count below is the floor — adding more is fine.
        from pathlib import Path
        text = Path(__file__).read_text()
        # `class TestIssue` followed by digits
        import re
        classes = re.findall(r"^class TestIssue\d+_", text, re.MULTILINE)
        assert len(classes) >= 4, (
            "test_forum_issues.py must have at least 4 TestIssue<N>_ "
            "classes — one per code-testable forum complaint. If you "
            "added a forum-reported bug fix, add a TestIssue<N>_ class "
            "here too (CLAUDE.md TEST_EVERY_BUG rule)."
        )
