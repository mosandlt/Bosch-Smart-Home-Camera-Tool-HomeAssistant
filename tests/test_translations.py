"""Translation / strings.json content integrity tests.

Consolidates every meta-test that validates ``strings.json`` and the
``translations/*.json`` locale files as a whole (not tied to one platform
module): JSON parses, keys are consistent across locales, translation keys
used by service handlers and issues are all defined, icon keys exist for
every entity that references one, placeholder names match Hassfest's rules
and stay consistent across locales, ICU MessageFormat escaping rules for
pattern-description helper text, and that a specific option's doc text
matches its actual default value in ``const.DEFAULT_OPTIONS``.

Covers:
  - strings.json + translations/de.json + translations/en.json all parse
    as JSON and share the same top-level structure
  - all translation_keys raised in the service handlers exist in
    strings.json
  - all `exceptions.*` / `issues.*` keys defined in strings.json have
    corresponding entries in both de.json and en.json (no missing
    translations)
  - all translation keys match HA's [a-z0-9_-]+ rule (no camelCase)
  - icons.json defines an icon for every entity that renders via
    translation_key, and every state-based icon entry has a default
  - `{placeholder}` names are consistent between strings.json and every
    locale file, and never sit inside single quotes (Hassfest rule)
  - `data_description.{folder_pattern,file_pattern}` helper text contains
    no curly-brace tokens at all (forbidden by both formatjs and
    Hassfest) while still naming each variable in prose
  - the `use_mjpeg_snapshot` option's doc text agrees with its actual
    default in const.DEFAULT_OPTIONS
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest

from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
STRINGS_PATH = COMPONENT_DIR / "strings.json"
TRANSLATIONS_DIR = COMPONENT_DIR / "translations"
DE_PATH = TRANSLATIONS_DIR / "de.json"
EN_PATH = TRANSLATIONS_DIR / "en.json"
ICONS_PATH = COMPONENT_DIR / "icons.json"


@pytest.fixture(scope="module")
def strings() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(STRINGS_PATH.read_text()))


@pytest.fixture(scope="module")
def de() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(DE_PATH.read_text()))


@pytest.fixture(scope="module")
def en() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(EN_PATH.read_text()))


@pytest.fixture(scope="module")
def icons() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(ICONS_PATH.read_text()))


def test_all_translation_files_parse(
    strings: dict[str, Any],
    de: dict[str, Any],
    en: dict[str, Any],
    icons: dict[str, Any],
) -> None:
    """All translation + icon JSON files must be valid JSON."""
    assert "exceptions" in strings, "strings.json missing top-level 'exceptions' key"
    assert "exceptions" in de, "de.json missing top-level 'exceptions' key"
    assert "exceptions" in en, "en.json missing top-level 'exceptions' key"
    assert "entity" in icons, "icons.json missing top-level 'entity' key"


def test_exceptions_keys_match_across_files(
    strings: dict[str, Any], de: dict[str, Any], en: dict[str, Any]
) -> None:
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


def _flatten_leaf_paths(d: dict[str, Any], prefix: str = "") -> set[str]:
    """Return every top-two-level '<domain>.<key>' path under a dict shaped
    like strings.json's ``entity``/``config_subentries`` blocks (domain ->
    key -> content) — used to diff key coverage without caring about the
    content itself."""
    out: set[str] = set()
    for domain, keys in d.items():
        if not isinstance(keys, dict):
            continue
        for key in keys:
            out.add(f"{prefix}{domain}.{key}")
    return out


def test_entity_keys_match_across_files(
    strings: dict[str, Any], de: dict[str, Any], en: dict[str, Any]
) -> None:
    """Every `entity.<domain>.<key>` in strings.json must exist in BOTH
    de.json and en.json.

    Regression test: an entire batch of new AI Camera Analysis entity keys
    (switch/text/sensor/binary_sensor/image) was added to strings.json only
    — HA reads translations/*.json at runtime, not strings.json directly,
    so the omission produced garbled/collision-disambiguated entity_ids
    (e.g. `switch.therrasse_bosch_terrasse` instead of
    `switch.bosch_terrasse_ai_analysis`) on a real HA instance, invisible
    to every other test in this suite since none of them exercise HA's
    live translation-driven entity-naming pipeline. Caught only by live
    deploy-testing before release — this test exists so the next such gap
    is caught by `pytest` instead.
    """
    canonical = _flatten_leaf_paths(strings.get("entity", {}))
    de_keys = _flatten_leaf_paths(de.get("entity", {}))
    en_keys = _flatten_leaf_paths(en.get("entity", {}))
    assert canonical <= de_keys, f"de.json missing entity keys: {canonical - de_keys}"
    assert canonical <= en_keys, f"en.json missing entity keys: {canonical - en_keys}"


def test_service_keys_match_across_files(
    strings: dict[str, Any], de: dict[str, Any], en: dict[str, Any]
) -> None:
    """Every `services.<key>` in strings.json must exist in de.json + en.json."""
    canonical = set(strings.get("services", {}).keys())
    de_keys = set(de.get("services", {}).keys())
    en_keys = set(en.get("services", {}).keys())
    assert canonical <= de_keys, f"de.json missing service keys: {canonical - de_keys}"
    assert canonical <= en_keys, f"en.json missing service keys: {canonical - en_keys}"


def test_config_subentry_keys_match_across_files(
    strings: dict[str, Any], de: dict[str, Any], en: dict[str, Any]
) -> None:
    """Every `config_subentries.<key>` in strings.json must exist in
    de.json + en.json."""
    canonical = set(strings.get("config_subentries", {}).keys())
    de_keys = set(de.get("config_subentries", {}).keys())
    en_keys = set(en.get("config_subentries", {}).keys())
    assert canonical <= de_keys, (
        f"de.json missing config_subentries keys: {canonical - de_keys}"
    )
    assert canonical <= en_keys, (
        f"en.json missing config_subentries keys: {canonical - en_keys}"
    )


def test_translation_keys_match_hassfest_rule(
    strings: dict[str, Any],
    en: dict[str, Any],
    de: dict[str, Any],
    icons: dict[str, Any],
) -> None:
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


def test_known_translation_keys_used_by_handlers(strings: dict[str, Any]) -> None:
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


def test_issue_translation_keys_present(
    strings: dict[str, Any], de: dict[str, Any], en: dict[str, Any]
) -> None:
    """ir.async_create_issue calls must have translation_key entries."""
    must_exist = {"token_expired", "auth_server_outage"}
    for label, blob in [("strings", strings), ("de", de), ("en", en)]:
        defined = set(blob.get("issues", {}).keys())
        missing = must_exist - defined
        assert not missing, (
            f"{label}.issues is missing keys for ir.async_create_issue: {missing}"
        )


def test_icon_translation_keys_present(icons: dict[str, Any]) -> None:
    """icons.json must define icons for every entity that uses translation_key.

    Spot-check the most prominent state-based switches and sensors that
    would render with no icon if their key disappeared from icons.json.
    """
    sw = icons["entity"].get("switch", {})
    se = icons["entity"].get("sensor", {})
    must_have_switch = {
        "live_stream",
        "privacy_mode",
        "audio",
        "camera_light",
        "notifications",
        "intercom",
        "intrusion_detection",
        "alarm_system_arm",
        "notification_type_movement",
        "notification_type_person",
        "notification_type_camera_alarm",
        "notification_type_trouble_email",
    }
    must_have_sensor = {"status", "fcm_push_status", "stream_status"}
    missing_sw = must_have_switch - set(sw.keys())
    missing_se = must_have_sensor - set(se.keys())
    assert not missing_sw, f"icons.json switch missing: {missing_sw}"
    assert not missing_se, f"icons.json sensor missing: {missing_se}"


def test_state_based_icons_have_default(icons: dict[str, Any]) -> None:
    """Every icon entry with a `state` block must also define `default`."""
    for platform, entries in icons["entity"].items():
        for key, body in entries.items():
            if "state" in body:
                assert "default" in body, (
                    f"icons.json entity.{platform}.{key} has 'state' "
                    f"without 'default' — entities will render with no "
                    f"icon when their state isn't in the state map"
                )


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{([^}]+)\}", text))


def _walk_leaves(obj, path=""):
    """Yield (path, value) for every string leaf, dotted/indexed path form."""
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_leaves(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_leaves(v, f"{path}[{i}]")


def test_de_placeholders_match_strings():
    """de.json must use the same placeholder names as strings.json for every key."""
    strings = json.loads(STRINGS_PATH.read_text())
    de = json.loads(DE_PATH.read_text())

    ref = {path: _placeholders(val) for path, val in _walk_leaves(strings)}
    errors = []
    for path, val in _walk_leaves(de):
        if path not in ref:
            continue
        de_ph = _placeholders(val)
        en_ph = ref[path]
        if de_ph != en_ph:
            errors.append(f"{path}: DE={de_ph} EN={en_ph}")

    assert not errors, "Placeholder mismatch in de.json:\n" + "\n".join(errors)


def test_nvr_base_path_no_german_placeholders():
    """Regression: nvr_base_path must not use German placeholder names {basis}/{Kamera}.

    strings.json used {base}/{Camera}/{YYYY-MM-DD} while de.json used
    {basis}/{Kamera}/{YYYY-MM-DD} for the same key — HA translation
    validation raised an ERROR on every startup since the placeholder
    names didn't match across locales.
    """
    de = json.loads(DE_PATH.read_text())
    try:
        text = de["options"]["step"]["init"]["data_description"]["nvr_base_path"]
    except KeyError:
        return  # key removed — no regression possible
    ph = _placeholders(text)
    german = ph & {"basis", "Kamera"}
    assert not german, f"German placeholder names still in nvr_base_path: {german}"


def test_all_translation_files_placeholder_consistency():
    """Every translation file must use the same placeholder names as strings.json."""
    strings = json.loads(STRINGS_PATH.read_text())
    ref = {path: _placeholders(val) for path, val in _walk_leaves(strings)}

    errors = []
    for lang_file in TRANSLATIONS_DIR.glob("*.json"):
        lang = json.loads(lang_file.read_text())
        for path, val in _walk_leaves(lang):
            if path not in ref:
                continue
            lang_ph = _placeholders(val)
            en_ph = ref[path]
            if lang_ph != en_ph:
                errors.append(f"[{lang_file.name}] {path}: got={lang_ph} want={en_ph}")

    assert not errors, "Placeholder mismatches found:\n" + "\n".join(errors)


def test_placeholders_not_in_single_quotes(
    strings: dict[str, Any], de: dict[str, Any], en: dict[str, Any]
) -> None:
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


_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_VALID_PLACEHOLDER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
# Hassfest also rejects HTML-looking tokens. Catch the obvious shape
# (`<word>` or `</word>`) so we don't try to "fix" a placeholder by
# swapping `{x}` for `<x>` and trip a different validation rule.
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*\s*/?>")


def _walk_string_values(node: Any, path: list[str]) -> Iterator[tuple[str, str]]:
    """Yield (path, string_value) for every JSON string leaf, list-based path."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk_string_values(v, [*path, str(k)])
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk_string_values(v, [*path, str(i)])
    elif isinstance(node, str):
        yield ".".join(path), node


@pytest.mark.parametrize("fixture_name", ["strings", "de", "en"])
def test_no_invalid_placeholders_in_translations(
    fixture_name: str, request: pytest.FixtureRequest
):
    """Every `{...}` placeholder in translation strings must be a valid
    Python identifier (`[a-zA-Z_][a-zA-Z0-9_]*`). Hassfest enforces
    this and fails the GitHub-Action `Validate` workflow if violated.

    Reason this test exists: a docs-style description for an option once
    contained a layout hint like `{base}/{Camera}/{YYYY-MM-DD}` — looks
    like a placeholder to a human, but Hassfest interprets each `{...}`
    token as a runtime placeholder reference and rejects tokens like
    `{YYYY-MM-DD}` (hyphens are not valid in identifiers). Fix at the
    time: replaced curly braces with `<...>` angle brackets in the
    layout description. Pinned here so a future docs-style description
    with `{date}` or `{name}` etc. that's NOT actually a runtime
    placeholder gets flagged locally before the push.
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
            f"  {p}: {{{ph}}}\n    in: {snippet}" for p, ph, snippet in bad_placeholders
        )
        + "\n\nFix: rephrase in plain prose — DO NOT use `<...>` either, "
        "Hassfest also rejects HTML-looking tokens."
    )
    assert not bad_html, (
        f"\n{fixture_name}.json has {len(bad_html)} HTML-looking token(s) — "
        f"Hassfest rejects these as 'string should not contain HTML':\n"
        + "\n".join(f"  {p}: {tag}\n    in: {snippet}" for p, tag, snippet in bad_html)
        + "\n\nFix: rephrase in plain prose, avoid `<word>` / `</word>` shapes."
    )


#
# Two validators squeeze pattern-description helper text from opposite
# directions:
#
# 1. HA frontend (formatjs / ICU MessageFormat) parses any `{name}` token
#    in a translation string as a runtime variable. If the backend doesn't
#    provide a value for `name`, formatjs throws `Error: MISSING_VALUE` at
#    render time — reproduced in practice on every options-dialog render
#    in a non-English frontend.
#
# 2. Hassfest (HA's official manifest validator, runs in CI on every PR)
#    rejects strings containing the ICU escape syntax `'{name}'`:
#
#        [ERROR] [TRANSLATIONS] Invalid strings.json: the string should not
#        contain placeholders inside single quotes for dictionary value @
#        data['options']['step']['init']['sections']['events_storage']
#        ['data_description']['file_pattern']
#
# So both `{name}` and `'{name}'` are forbidden in helper-text /
# `data_description` strings. The only safe form for descriptions that
# need to mention literal placeholder names is prose — list the variable
# names plain-text and explain in words that the user should wrap them in
# curly braces inside the actual pattern field.
#
# These tests pin the post-rewrite state: zero curly-brace tokens in any
# `data_description.{folder_pattern,file_pattern}` value across all
# translation files, while still naming every variable in prose. Guards
# against both regressions:
#
# - A future copy-paste from another integration that drops naked
#   `{camera}` → formatjs MISSING_VALUE returns.
# - An over-eager future "fix" that re-introduces `'{camera}'` → Hassfest
#   fails CI.

_INTEGRATION = COMPONENT_DIR
_ICU_FILES = [STRINGS_PATH, EN_PATH, DE_PATH]

# Keys whose values are literal pattern descriptions, not runtime templates.
_PATTERN_DESC_KEYS = ("folder_pattern", "file_pattern")

# Match any `{lowercase_token}` regardless of whether it's escape-wrapped
# with surrounding apostrophes. Both forms are forbidden by HA's
# validator/renderer combo.
_ANY_CURLY_TOKEN_RE = re.compile(r"\{[a-z_]+\}")


def _all_pattern_description_strings() -> Iterator[tuple[str, str, str]]:
    """Yield (file, key, value) for each pattern-desc string in each file."""
    for path in _ICU_FILES:
        data = json.loads(path.read_text(encoding="utf-8"))
        stack: list[dict[str, Any]] = [data]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            data_desc = node.get("data_description")
            if isinstance(data_desc, dict):
                for key in _PATTERN_DESC_KEYS:
                    if key in data_desc:
                        yield path.name, key, data_desc[key]
            for v in node.values():
                if isinstance(v, dict):
                    stack.append(v)


@pytest.mark.parametrize(
    "file_name,key,value",
    list(_all_pattern_description_strings()),
)
def test_pattern_description_has_no_curly_brace_tokens(
    file_name: str, key: str, value: str
) -> None:
    """The string must not contain ``{name}`` or ``'{name}'`` tokens.

    Both formatjs (rejects naked tokens at render time) and Hassfest
    (rejects escaped tokens at validation time) reject these. Use prose to
    name the variables and explain that the user wraps them in curly
    braces when entering the actual pattern field.
    """
    tokens = _ANY_CURLY_TOKEN_RE.findall(value)
    assert not tokens, (
        f"{file_name}::{key} contains curly-brace tokens {tokens} — "
        f"formatjs and Hassfest both reject these in helper text. "
        f"Use prose (e.g. 'Variables: camera, year — wrap each in curly "
        f"braces') instead of literal `{{camera}}` examples."
    )


def test_pattern_descriptions_still_mention_each_variable_name_in_prose() -> None:
    """Positive check: the helper text still names every variable the user can
    type, just without the curly-brace wrappers.

    A future overzealous trim that drops variable names entirely (leaving
    only "Pattern.") would silently make the field much harder to use.
    Catches that regression.
    """
    expected_in_folder = ["camera", "year", "month", "day", "type"]
    expected_in_file = [
        "camera",
        "date",
        "time",
        "type",
        "id",
        "year",
        "month",
        "day",
    ]

    for path in _ICU_FILES:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        # Find the data_description block (only one in this integration)
        found_folder = False
        found_file = False
        stack: list[dict] = [data]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            data_desc = node.get("data_description")
            if isinstance(data_desc, dict):
                folder_text = data_desc.get("folder_pattern", "")
                file_text = data_desc.get("file_pattern", "")
                if folder_text:
                    found_folder = True
                    missing = [v for v in expected_in_folder if v not in folder_text]
                    assert not missing, (
                        f"{path.name}::folder_pattern missing variable "
                        f"name(s) {missing} from helper text"
                    )
                if file_text:
                    found_file = True
                    missing = [v for v in expected_in_file if v not in file_text]
                    assert not missing, (
                        f"{path.name}::file_pattern missing variable "
                        f"name(s) {missing} from helper text"
                    )
            for v in node.values():
                if isinstance(v, dict):
                    stack.append(v)

        assert found_folder, f"{path.name} no longer has folder_pattern description"
        assert found_file, f"{path.name} no longer has file_pattern description"


class TestUseMjpegSnapshotDoc:
    """All 3 translation files for the `use_mjpeg_snapshot` option must agree
    with `const.DEFAULT_OPTIONS` on whether the feature is on or off by
    default — the doc text and the actual default silently drifted apart
    once (doc claimed "On by default" while the real default was False),
    confusing users who toggled it and saw no behavior change.
    """

    def test_default_is_false(self) -> None:
        """Sanity: DEFAULT_OPTIONS still says use_mjpeg_snapshot is OFF.
        If you change this, also update the doc strings."""
        assert DEFAULT_OPTIONS["use_mjpeg_snapshot"] is False

    @pytest.mark.parametrize(
        "rel_path",
        [
            "strings.json",
            "translations/en.json",
        ],
    )
    def test_english_doc_says_off_by_default(self, rel_path: str) -> None:
        text = (COMPONENT_DIR / rel_path).read_text(encoding="utf-8")
        data = json.loads(text)
        desc = data["options"]["step"]["init"]["sections"]["stream"][
            "data_description"
        ]["use_mjpeg_snapshot"]
        # Must NOT claim "On by default" (the historical inaccuracy).
        assert "on by default" not in desc.lower(), (
            f"{rel_path}: still says 'On by default' — must match DEFAULT_OPTIONS=False"
        )
        # Must explicitly say "Off by default".
        assert "off by default" in desc.lower(), (
            f"{rel_path}: doc must say 'Off by default' to match DEFAULT_OPTIONS"
        )

    def test_german_doc_says_standardmaessig_aus(self) -> None:
        text = (COMPONENT_DIR / "translations/de.json").read_text(encoding="utf-8")
        data = json.loads(text)
        desc = data["options"]["step"]["init"]["sections"]["stream"][
            "data_description"
        ]["use_mjpeg_snapshot"]
        assert "standardmäßig aktiviert" not in desc.lower(), (
            "de.json: still says 'Standardmäßig aktiviert' — must say 'Standardmäßig aus'"
        )
        assert "standardmäßig aus" in desc.lower(), (
            "de.json: must say 'Standardmäßig aus' to match DEFAULT_OPTIONS"
        )
