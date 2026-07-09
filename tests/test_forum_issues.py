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
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests.source_match import assert_in_source

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


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

        api_ref = Path(__file__).parent.parent.parent / "docs" / "api-reference.md"
        if not api_ref.exists():
            pytest.skip("docs/api-reference.md not in repo (workspace-only)")
        text = api_ref.read_text()
        assert "Motion Revert" in text or "motion revert" in text.lower(), (
            "Motion-revert limitation must stay documented in api-reference.md"
        )


# ── Issues #5-8: routed to per-module test files during the tests/ reorg ────
#
# geotie's binary-sensor / polling-bootstrap fixes (#5, #6) now live in
# tests/test_binary_sensor.py (TestIssue5_BinarySensorMissesEvents equivalent
# window/60s-lag tests) and tests/test_init.py
# (test_forum_issue5_polling_seeds_last_event_ids_on_first_tick).
# xDraGGi's mark_events_read opt-out (#7) now lives in tests/test_const.py.
# geotie's Media Browser empty-after-upgrade fix (#8) now lives in
# tests/test_media_source.py (test_download_path_creates_missing_directory)
# plus the README doc-check (test_readme_documents_auto_download_path).


# ── Meta: every forum-reported issue has a test somewhere in the suite ──


class TestMeta:
    """The CLAUDE.md TEST_EVERY_BUG rule says every forum-reported issue
    must have a regression test before the fix is committed. This file is
    the traceability index — issues #1-#8 map to test functions across
    this file and the per-module files noted above. If a future PR fixes a
    forum bug without adding a test anywhere, that's the gap to close.
    """

    def test_forum_issue_index_documents_all_code_testable_issues(self):
        """Sanity: this file's docstring table must list every forum issue
        (code-testable or not) so the traceability index doesn't silently
        go stale as fixes get routed into per-module test files."""
        from pathlib import Path

        text = Path(__file__).read_text()
        for n in range(1, 9):
            assert f"| {n} |" in text, (
                f"Forum issue #{n} missing from the traceability table — "
                "CLAUDE.md TEST_EVERY_BUG requires every forum-reported "
                "issue stay indexed even after its test moves to a "
                "per-module file."
            )
