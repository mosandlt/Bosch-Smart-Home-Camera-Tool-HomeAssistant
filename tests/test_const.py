"""Tests for const.py's own constants/values.

Extracted from tests/test_fresh_install.py (2026-07-09 test-file
reorganization to match HA-core's one-flat-test-file-per-module convention).
These specific tests assert directly on DEFAULT_OPTIONS values defined in
const.py and do not exercise any other module's behaviour — unlike the
get_options()-merging tests that stayed in test_fresh_install.py (those test
__init__.py's get_options() function, not const.py itself).

Source: https://github.com/mosandlt/Bosch-Smart-Home-Camera-Tool-HomeAssistant
"""

from __future__ import annotations


class TestDefaultOptions:
    """DEFAULT_OPTIONS (const.py) must hold sensible, opt-in-safe defaults."""

    def test_default_enable_local_save_is_false(self):
        """Fresh install: enable_local_save must default to False (opt-in, not auto-enabled)."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        assert DEFAULT_OPTIONS.get("enable_local_save") is False

    def test_default_download_path_is_set(self):
        """download_path has a default path but is inactive until enable_local_save=True."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        assert DEFAULT_OPTIONS.get("download_path") == "/config/bosch_events"

    def test_default_fcm_push_disabled(self):
        """Fresh install: FCM push is disabled by default — polling drives events."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        assert DEFAULT_OPTIONS.get("enable_fcm_push") is False

    def test_default_notify_service_empty(self):
        """Fresh install: no notification service configured by default."""
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        assert DEFAULT_OPTIONS.get("alert_notify_service") == ""


# Section: GH#4 — CARD_VERSION constant (relocated from
# tests/test_github_issues.py — the bundled card JS file-existence check
# for the same issue is a non-single-module meta check and stays in
# tests/test_github_issues.py)


class TestCardVersionConstant:
    def test_card_version_constant_exists(self):
        """`CARD_VERSION` must exist and be a non-empty string. Must stay in
        sync with `src/bosch-camera-card.js` — Lovelace resource cache
        invalidation depends on the version match."""
        from custom_components.bosch_shc_camera.const import CARD_VERSION

        assert isinstance(CARD_VERSION, str)
        assert len(CARD_VERSION) > 0


# Section: forum issue #7 (xDraGGi) — mark_events_read opt-out default
# (relocated from tests/test_forum_issues.py)


class TestMarkEventsReadOptOutDefault:
    """xDraGGi (simon42 forum) — the integration marking events as read in
    the Bosch app made them disappear from the app's own 'new' list. Fix:
    `mark_events_read` is an option the user controls, defaulting to Off."""

    def test_mark_events_read_option_is_documented(self):
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        assert DEFAULT_OPTIONS.get("mark_events_read", False) is False, (
            "mark_events_read must default to False so the user controls "
            "whether events disappear from the Bosch app's 'new' list."
        )

    def test_option_present_in_strings(self):
        """The option must appear in strings.json so users can find + toggle it."""
        import json
        from pathlib import Path

        comp = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
        strings = json.loads((comp / "strings.json").read_text())
        sections = (
            strings.get("options", {})
            .get("step", {})
            .get("init", {})
            .get("sections", {})
        )
        all_labels = {k for sec in sections.values() for k in sec.get("data", {})}
        assert "mark_events_read" in all_labels, (
            "The mark_events_read option must be exposed in the options "
            "flow UI so users can discover why events disappear from the "
            "Bosch app's 'new' list."
        )


# Section: STREAM_START_SKIPPED sentinel (relocated from
# tests/test_stream_start_in_progress.py — the coordinator/switch-side
# consumers of the sentinel stayed in tests/test_init.py / tests/test_switch.py)


class TestStreamStartSkippedSentinel:
    """A coalesced (de-duped) concurrent stream-start must be distinguishable
    from a real failure by every `if result:` consumer without special-casing —
    the sentinel must stay falsy, unique, and distinct from `None`."""

    def test_sentinel_is_falsy_singleton(self):
        from custom_components.bosch_shc_camera.const import STREAM_START_SKIPPED

        assert bool(STREAM_START_SKIPPED) is False
        assert STREAM_START_SKIPPED is STREAM_START_SKIPPED
        # It must NOT be None — that is the whole point (None == real failure).
        assert STREAM_START_SKIPPED is not None
