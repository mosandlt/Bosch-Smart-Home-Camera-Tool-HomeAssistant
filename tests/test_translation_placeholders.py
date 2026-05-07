"""Regression test: translation placeholder names must match strings.json.

Reported 2026-05-07: de.json had {basis}/{Kamera}/{YYYY-MM-DD} in
options.step.init.data_description.nvr_base_path but strings.json used
{base}/{Camera}/{YYYY-MM-DD} — HA translation validation raised ERROR on
every startup. Fix: deploy the up-to-date local de.json to HA.
"""

import json
import re
from pathlib import Path

COMPONENT = Path(__file__).parents[1] / "custom_components" / "bosch_shc_camera"
STRINGS = COMPONENT / "strings.json"
TRANSLATIONS = COMPONENT / "translations"


def _placeholders(text: str) -> set[str]:
    return set(re.findall(r"\{([^}]+)\}", text))


def _walk_leaves(obj, path=""):
    """Yield (path, value) for every string leaf."""
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
    strings = json.loads(STRINGS.read_text())
    de = json.loads((TRANSLATIONS / "de.json").read_text())

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
    """Regression: nvr_base_path must not use German placeholder names {basis}/{Kamera}."""
    de = json.loads((TRANSLATIONS / "de.json").read_text())
    try:
        text = de["options"]["step"]["init"]["data_description"]["nvr_base_path"]
    except KeyError:
        return  # key removed — no regression possible
    ph = _placeholders(text)
    german = ph & {"basis", "Kamera"}
    assert not german, f"German placeholder names still in nvr_base_path: {german}"


def test_all_translation_files_placeholder_consistency():
    """Every translation file must use the same placeholder names as strings.json."""
    strings = json.loads(STRINGS.read_text())
    ref = {path: _placeholders(val) for path, val in _walk_leaves(strings)}

    errors = []
    for lang_file in TRANSLATIONS.glob("*.json"):
        lang = json.loads(lang_file.read_text())
        for path, val in _walk_leaves(lang):
            if path not in ref:
                continue
            lang_ph = _placeholders(val)
            en_ph = ref[path]
            if lang_ph != en_ph:
                errors.append(f"[{lang_file.name}] {path}: got={lang_ph} want={en_ph}")

    assert not errors, "Placeholder mismatches found:\n" + "\n".join(errors)
