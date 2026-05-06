"""Translation file integrity tests.

Verifies that:
  - strings.json + translations/de.json + translations/en.json all parse as JSON
  - all translation_keys raised in the service handlers exist in strings.json
  - all `exceptions.*` keys defined in strings.json have corresponding entries
    in both de.json and en.json (no missing translations)
  - all translation keys match HA's [a-z0-9_-]+ rule (no camelCase)
  - placeholders in translation messages don't sit inside single quotes
    (Hassfest rule that bit us in v11.0.0 → patched in v11.0.0 squash)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


COMPONENT_DIR = (
    Path(__file__).parent.parent
    / "custom_components"
    / "bosch_shc_camera"
)
STRINGS_PATH = COMPONENT_DIR / "strings.json"
DE_PATH = COMPONENT_DIR / "translations" / "de.json"
EN_PATH = COMPONENT_DIR / "translations" / "en.json"
ICONS_PATH = COMPONENT_DIR / "icons.json"


@pytest.fixture(scope="module")
def strings() -> dict:
    return json.loads(STRINGS_PATH.read_text())


@pytest.fixture(scope="module")
def de() -> dict:
    return json.loads(DE_PATH.read_text())


@pytest.fixture(scope="module")
def en() -> dict:
    return json.loads(EN_PATH.read_text())


@pytest.fixture(scope="module")
def icons() -> dict:
    return json.loads(ICONS_PATH.read_text())


def test_all_translation_files_parse(strings, de, en, icons) -> None:
    """All translation + icon JSON files must be valid JSON."""
    assert "exceptions" in strings, "strings.json missing top-level 'exceptions' key"
    assert "exceptions" in de, "de.json missing top-level 'exceptions' key"
    assert "exceptions" in en, "en.json missing top-level 'exceptions' key"
    assert "entity" in icons, "icons.json missing top-level 'entity' key"


def test_exceptions_keys_match_across_files(strings, de, en) -> None:
    """Every key in strings.json/exceptions must exist in de.json + en.json."""
    canonical = set(strings["exceptions"].keys())
    de_keys = set(de.get("exceptions", {}).keys())
    en_keys = set(en.get("exceptions", {}).keys())
    assert canonical == de_keys, (
        f"de.json missing keys: {canonical - de_keys}; extra: {de_keys - canonical}"
    )
    assert canonical == en_keys, (
        f"en.json missing keys: {canonical - en_keys}; extra: {en_keys - canonical}"
    )


def test_translation_keys_match_hassfest_rule(strings, en, de, icons) -> None:
    """Every key must match [a-z0-9_-]+ — Hassfest enforces this."""
    pattern = re.compile(r"^[a-z0-9_-]+$")
    samples: list[tuple[str, str]] = []
    for key in strings.get("exceptions", {}):
        samples.append(("strings.exceptions", key))
    for key in strings.get("issues", {}):
        samples.append(("strings.issues", key))
    for platform, ents in icons.get("entity", {}).items():
        for key in ents:
            samples.append((f"icons.entity.{platform}", key))
    for src, key in samples:
        assert pattern.fullmatch(key), (
            f"{src}.{key!r}: violates [a-z0-9_-]+ — camelCase or punctuation forbidden"
        )


def test_placeholders_not_in_single_quotes(strings, de, en) -> None:
    """Hassfest rejects messages where a {placeholder} sits inside single quotes.

    Pattern: '{anything}' — the single quotes around braces are forbidden.
    """
    bad = re.compile(r"'\{[^}]+\}'")
    for label, blob in [("strings", strings), ("de", de), ("en", en)]:
        for key, entry in blob.get("exceptions", {}).items():
            msg = entry.get("message", "")
            assert not bad.search(msg), (
                f"{label}.exceptions.{key}: message contains a "
                f"'{{placeholder}}' sequence (forbidden by Hassfest): {msg!r}"
            )


def test_known_translation_keys_used_by_handlers(strings) -> None:
    """The translation keys raised in __init__.py must all be defined.

    This is an explicit allowlist — if a new HomeAssistantError /
    ServiceValidationError gets raised with a new translation_key, this
    test fails until the key is added to strings.json (and en/de).
    """
    must_exist = {
        "argument_required",
        "argument_must_be_list",
        "missing_field",
        "value_out_of_range",
        "index_out_of_range",
        "not_found",
        "live_connection_failed",
        "http_error",
        "http_error_with_body",
        "privacy_blocked",
        "unexpected_error",
    }
    defined = set(strings.get("exceptions", {}).keys())
    missing = must_exist - defined
    assert not missing, (
        f"strings.json/exceptions is missing translation keys raised by "
        f"the service handlers: {missing}"
    )


def test_issue_translation_keys_present(strings, de, en) -> None:
    """ir.async_create_issue calls must have translation_key entries."""
    must_exist = {"token_expired", "auth_server_outage"}
    for label, blob in [("strings", strings), ("de", de), ("en", en)]:
        defined = set(blob.get("issues", {}).keys())
        missing = must_exist - defined
        assert not missing, (
            f"{label}.issues is missing keys for ir.async_create_issue: {missing}"
        )


def test_icon_translation_keys_present(icons) -> None:
    """icons.json must define icons for every entity that uses translation_key.

    Spot-check the most prominent state-based switches and sensors that
    would render with no icon if their key disappeared from icons.json.
    """
    sw = icons["entity"].get("switch", {})
    se = icons["entity"].get("sensor", {})
    must_have_switch = {
        "live_stream", "privacy_mode", "audio", "camera_light",
        "notifications", "intercom", "intrusion_detection",
        "alarm_system_arm",
        "notification_type_movement", "notification_type_person",
        "notification_type_camera_alarm", "notification_type_trouble_email",
    }
    must_have_sensor = {"status", "fcm_push_status", "stream_status"}
    missing_sw = must_have_switch - set(sw.keys())
    missing_se = must_have_sensor - set(se.keys())
    assert not missing_sw, f"icons.json switch missing: {missing_sw}"
    assert not missing_se, f"icons.json sensor missing: {missing_se}"


def test_state_based_icons_have_default(icons) -> None:
    """Every icon entry with a `state` block must also define `default`."""
    for platform, entries in icons["entity"].items():
        for key, body in entries.items():
            if "state" in body:
                assert "default" in body, (
                    f"icons.json entity.{platform}.{key} has 'state' "
                    f"without 'default' — entities will render with no "
                    f"icon when their state isn't in the state map"
                )


# ── Hassfest placeholder rule (CI fail-safe) ─────────────────────────────────


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_VALID_PLACEHOLDER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# Hassfest also rejects HTML-looking tokens. Catch the obvious shape
# (`<word>` or `</word>`) so we don't try to "fix" a placeholder by
# swapping `{x}` for `<x>` and trip a different validation rule.
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\s*/?>")


def _walk_string_values(node, path):
    """Yield (path, string_value) for every JSON string leaf."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_string_values(v, path + [str(k)])
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_string_values(v, path + [str(i)])
    elif isinstance(node, str):
        yield ".".join(path), node


@pytest.mark.parametrize(
    "fixture_name", ["strings", "de", "en"]
)
def test_no_invalid_placeholders_in_translations(fixture_name, request):
    """Every `{...}` placeholder in translation strings must be a valid
    Python identifier (`[a-zA-Z_][a-zA-Z0-9_]*`). Hassfest enforces
    this and fails the GitHub-Action `Validate` workflow if violated.

    Reason this test exists: in v11.0.5 the description for the new
    `nvr_base_path` option contained `{base}/{Camera}/{YYYY-MM-DD}` —
    looks like a layout hint to a human, but Hassfest interpreted each
    `{...}` token as a placeholder reference and rejected `{YYYY-MM-DD}`
    (hyphens are not valid in identifiers). Fix: replaced curly braces
    with `<...>` angle brackets in the layout description. Pinned here
    so a future docs-style description with `{date}` or `{name}` etc.
    that's NOT actually a runtime placeholder gets flagged locally
    BEFORE the push. Saves a CI round-trip + a hot-fix release.
    """
    data = request.getfixturevalue(fixture_name)
    bad_placeholders: list[tuple[str, str, str]] = []
    bad_html: list[tuple[str, str, str]] = []
    for path, value in _walk_string_values(data, []):
        for ph in _PLACEHOLDER_RE.findall(value):
            if not _VALID_PLACEHOLDER_RE.match(ph):
                bad_placeholders.append((path, ph, value[:120]))
        for tag in _HTML_TAG_RE.findall(value):
            bad_html.append((path, tag, value[:120]))
    assert not bad_placeholders, (
        f"\n{fixture_name}.json has {len(bad_placeholders)} invalid Hassfest placeholder(s):\n"
        + "\n".join(
            f"  {p}: {{{ph}}}\n    in: {snippet}"
            for p, ph, snippet in bad_placeholders
        )
        + "\n\nFix: rephrase in plain prose — DO NOT use `<...>` either, "
        "Hassfest also rejects HTML-looking tokens."
    )
    assert not bad_html, (
        f"\n{fixture_name}.json has {len(bad_html)} HTML-looking token(s) — "
        f"Hassfest rejects these as 'string should not contain HTML':\n"
        + "\n".join(
            f"  {p}: {tag}\n    in: {snippet}"
            for p, tag, snippet in bad_html
        )
        + "\n\nFix: rephrase in plain prose, avoid `<word>` / `</word>` shapes."
    )
