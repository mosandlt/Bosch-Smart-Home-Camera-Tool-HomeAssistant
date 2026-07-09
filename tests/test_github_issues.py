"""Regression tests for closed GitHub issues.

CLAUDE.md `TEST_EVERY_BUG` rule: every reported bug gets a regression
test before/with the fix. This file covers issues that were closed
before the rule was in place.

Issue index (status as of 2026-05-05):

| # | Title                                                  | Author       | Status | Test class             |
|---|--------------------------------------------------------|--------------|--------|------------------------|
| 1 | Motion Sensitivity (and other states) not permanent    | DrNiKa       | CLOSED | TestGH1_MotionRevert   |
| 2 | Token refresh fails - 6.4.2 (Solved after re-install)  | —            | CLOSED | TestGH2_TokenRefresh   |
| 3 | Light controls for Eyes outdoor camera II              | DrNiKa       | CLOSED | TestGH3_Gen2Light      |
| 4 | bosch-camera-card is not working                       | Michael…     | CLOSED | TestGH4_CardFrontend   |
| 5 | Refresh-Token abgelaufen, Link zur Neuanmeldung        | dziko83      | CLOSED | TestGH5_ReauthFlow     |
| 6 | Streaming broken since 10.x (cloud & LAN)              | WoodenDuke   | CLOSED | TestGH6_StreamPipeline |
| 7 | Bosch Cam ein Traum wird wahr (positive feedback)      | —            | CLOSED | (no test — non-bug)    |
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── GH#1: Motion Sensitivity not permanent (DrNiKa) ────────────────────


class TestGH1_MotionRevert:
    """Same root cause as forum issue #1 (Poldi41) — see test_forum_issues.py.

    The on-device IVA engine reverts cloud-side motion config in ~1 s.
    Documented in `docs/api-reference.md` § 'Motion Revert'. Workaround
    is the cloud rules engine, fully implemented as service actions in
    v8+.
    """

    def test_create_rule_service_registered(self):
        """Workaround for the limitation: cloud rules engine.

        `create_rule`, `update_rule`, `delete_rule` services must be
        present so users can manage schedule-based motion rules instead
        of relying on the auto-reverted /motion endpoint.
        """
        comp = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
        services = (comp / "services.yaml").read_text()
        for svc in ("create_rule", "update_rule", "delete_rule"):
            assert svc in services, (
                f"Service '{svc}' missing from services.yaml — needed as "
                f"the documented workaround for motion-revert (GH#1)"
            )


# ── GH#2, GH#3, GH#5, GH#6: routed to per-module test files during the
# tests/ reorg — see tests/test_init.py (token-refresh methods + repair
# issue string), tests/test_switch.py + tests/test_light.py + tests/
# test_models.py (Gen2 outdoor light/switch/model-config), tests/
# test_config_flow.py (reauth/reconfigure), tests/test_camera.py
# (supported_features / live_connections). Only the doc/file-existence
# checks below don't fit a single custom_components module and stay here.


# ── GH#4: bosch-camera-card is not working (Michael8885443) ────────────


class TestGH4_CardFrontend:
    """Card auto-registration since v10.3.19 — manual resource entry no
    longer needed. The integration serves the card from its own bundled
    `www/` folder via HA static-path handler.
    """

    def test_card_javascript_exists(self):
        """The bundled card must be present in custom_components/.../www/."""
        comp = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
        card = comp / "www" / "bosch-camera-card.js"
        assert card.exists(), (
            "Card auto-registration relies on the bundled JS at "
            "custom_components/bosch_shc_camera/www/bosch-camera-card.js"
        )


# Note: CARD_VERSION (GH#4), reauth/reconfigure flow (GH#5), and the
# live_connections/supported_features stream pin (GH#6) now live in
# tests/test_const.py, tests/test_config_flow.py, and tests/test_camera.py
# respectively — see the module-routing note above TestGH4_CardFrontend.
# The live_stream-switch-specific regression tests that used to live here
# (class existence + unavailable-on-stale-session) also moved, to
# tests/test_switch.py.


# ── Meta enforcer ─────────────────────────────────────────────────────


class TestMeta:
    """Traceability index for closed GitHub issues (CLAUDE.md
    TEST_EVERY_BUG rule). Most fixes' regression tests now live in their
    per-module test_<module>.py files after the tests/ reorg — this file
    keeps only the checks with no single owning module (card JS bundle
    presence, services.yaml content) plus this issue-index docstring."""

    def test_github_issue_index_documents_all_closed_issues(self):
        """Sanity: the docstring table must list every closed issue (code-
        testable or not) so the traceability index doesn't go stale as
        fixes get routed into per-module test files."""
        text = Path(__file__).read_text()
        for n in range(1, 8):
            assert f"| {n} |" in text, (
                f"GitHub issue #{n} missing from the traceability table — "
                "CLAUDE.md TEST_EVERY_BUG requires every closed issue stay "
                "indexed even after its test moves to a per-module file."
            )
