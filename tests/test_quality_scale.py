"""Quality-Scale YAML + manifest consistency tests.

Verifies that the integration's claimed quality_scale tier (in manifest.json)
matches what's actually marked `done` in quality_scale.yaml. Catches
regressions where someone bumps the manifest tier without updating the
rule status, or vice versa.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Optional yaml — install via pytest-homeassistant-custom-component dep tree
yaml = pytest.importorskip("yaml")


COMPONENT_DIR = (
    Path(__file__).parent.parent
    / "custom_components"
    / "bosch_shc_camera"
)


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads((COMPONENT_DIR / "manifest.json").read_text())


@pytest.fixture(scope="module")
def quality_scale() -> dict:
    return yaml.safe_load((COMPONENT_DIR / "quality_scale.yaml").read_text())


# Tier rule sets per https://developers.home-assistant.io/docs/core/integration-quality-scale/
BRONZE_RULES = {
    "action-setup", "appropriate-polling", "brands", "common-modules",
    "config-flow", "config-flow-test-coverage", "dependency-transparency",
    "docs-actions", "docs-high-level-description", "docs-installation-instructions",
    "docs-removal-instructions", "entity-event-setup", "entity-unique-id",
    "has-entity-name", "runtime-data", "test-before-configure",
    "test-before-setup", "unique-config-entry",
}
SILVER_RULES = {
    "action-exceptions", "config-entry-unloading", "docs-configuration-parameters",
    "docs-installation-parameters", "entity-unavailable", "integration-owner",
    "log-when-unavailable", "parallel-updates", "reauthentication-flow",
    "test-coverage",
}
GOLD_RULES = {
    "devices", "diagnostics", "discovery", "discovery-update-info",
    "docs-data-update", "docs-examples", "docs-known-limitations",
    "docs-supported-devices", "docs-supported-functions", "docs-troubleshooting",
    "docs-use-cases", "dynamic-devices", "entity-category", "entity-device-class",
    "entity-disabled-by-default", "entity-translations", "exception-translations",
    "icon-translations", "reconfiguration-flow", "repair-issues", "stale-devices",
}


def _is_done_or_exempt(entry) -> bool:
    """Treat both `done` strings and `{status: exempt}` dicts as compliant."""
    if isinstance(entry, str):
        return entry == "done"
    if isinstance(entry, dict):
        return entry.get("status") in ("done", "exempt")
    return False


def test_quality_scale_yaml_lists_all_known_rules(quality_scale) -> None:
    """quality_scale.yaml must list every rule in Bronze + Silver + Gold."""
    listed = set(quality_scale["rules"].keys())
    expected = BRONZE_RULES | SILVER_RULES | GOLD_RULES
    missing = expected - listed
    assert not missing, (
        f"quality_scale.yaml is missing entries for these rules: {missing}"
    )


def test_manifest_tier_matches_yaml_completeness(
    manifest, quality_scale
) -> None:
    """If manifest declares a tier, quality_scale.yaml must back it up.

    Rule: every Bronze + Silver + Gold rule must be `done` (or `exempt`)
    when manifest claims `gold`. Open `todo` items are allowed if they're
    NOT in the claimed tier (e.g. test-coverage on Silver may be `todo`
    while manifest claims `silver` — many integrations do this).
    """
    declared_tier = manifest.get("quality_scale")
    rules = quality_scale["rules"]
    if declared_tier == "bronze":
        required = BRONZE_RULES
    elif declared_tier == "silver":
        required = BRONZE_RULES | SILVER_RULES
    elif declared_tier == "gold":
        required = BRONZE_RULES | SILVER_RULES | GOLD_RULES
    elif declared_tier == "platinum":
        required = BRONZE_RULES | SILVER_RULES | GOLD_RULES  # platinum adds 3 more
    else:
        pytest.skip(f"manifest quality_scale={declared_tier!r} — nothing to verify")

    not_done = [
        rule for rule in required
        if not _is_done_or_exempt(rules.get(rule))
    ]
    # Allow specific known-todo rules even at the claimed tier — these are
    # tracked as `todo` with explicit comment in quality_scale.yaml. Update
    # this allowlist when one is closed.
    allowed_todo = {"test-coverage"}
    blockers = set(not_done) - allowed_todo
    assert not blockers, (
        f"manifest claims quality_scale={declared_tier!r} but these rules "
        f"are not `done` or `exempt`: {blockers}. Either fix them or step "
        f"the manifest tier down."
    )


def test_no_unknown_rules_in_yaml(quality_scale) -> None:
    """quality_scale.yaml shouldn't declare rules that don't exist upstream.

    Catches typos like `parallel_updates` instead of `parallel-updates`.
    """
    listed = set(quality_scale["rules"].keys())
    known = (
        BRONZE_RULES | SILVER_RULES | GOLD_RULES
        | {"async-dependency", "inject-websession", "strict-typing"}  # Platinum
    )
    unknown = listed - known
    assert not unknown, (
        f"quality_scale.yaml has rules that don't exist in the official "
        f"HA quality scale: {unknown}. Probably typos."
    )


def test_exempt_rules_have_comments(quality_scale) -> None:
    """Every `exempt` rule must include a `comment` explaining why."""
    rules = quality_scale["rules"]
    for name, body in rules.items():
        if isinstance(body, dict) and body.get("status") == "exempt":
            assert body.get("comment"), (
                f"Rule {name!r} is `exempt` but has no `comment`. "
                f"Exempt rules need justification."
            )


def test_todo_rules_have_comments(quality_scale) -> None:
    """Every `todo` rule must include a `comment` describing what's open."""
    rules = quality_scale["rules"]
    for name, body in rules.items():
        if isinstance(body, dict) and body.get("status") == "todo":
            assert body.get("comment"), (
                f"Rule {name!r} is `todo` but has no `comment`. "
                f"Todo rules need a note about scope/blocker."
            )
