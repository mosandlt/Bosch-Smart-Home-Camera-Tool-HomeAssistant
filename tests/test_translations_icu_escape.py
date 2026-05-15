"""Regression test for ICU MessageFormat escape in translation strings.

User-observed in production HA logs 2026-05-15 (Andrew75, HA Community forum):

    formatjs Error: MISSING_VALUE — The intl string context variable "camera"
    was not provided to the string "Subfolder pattern. Available variables:
    {camera}, {year}, ..."

Root cause: HA's frontend uses formatjs (ICU MessageFormat) to render
translations. A bare `{camera}` token in a translation string is parsed as a
runtime variable to interpolate. The integration meant `{camera}` as a
literal placeholder example, so the frontend throws ``MISSING_VALUE``.

Fix: wrap the brace in single-quote escapes — ``'{camera}'`` renders as the
literal text ``{camera}`` per ICU's quoting rules.

This test pins the escaping so a future refactor (or copy-paste from another
integration's translation file) can't reintroduce naked placeholders in any
field whose value is a literal-template description.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# Translation files relative to the integration root.
_INTEGRATION = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
_FILES = [
    _INTEGRATION / "strings.json",
    _INTEGRATION / "translations" / "en.json",
    _INTEGRATION / "translations" / "de.json",
]

# Keys whose values are *literal* pattern descriptions, not runtime templates.
# Their `{name}` tokens are showing the user which variable names are valid in
# the input field — they must NOT be parsed as ICU variables.
_PATTERN_DESC_KEYS = ("folder_pattern", "file_pattern")

# Match `{lowercase_token}` not preceded by an escaping single quote.
_NAKED_PLACEHOLDER_RE = re.compile(r"(?<!')\{[a-z_]+\}(?!')")


def _all_pattern_description_strings():
    """Yield (file, key, value) for each pattern-desc string in each file."""
    for path in _FILES:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Recursively scan for data_description blocks containing our keys.
        stack: list[dict] = [data]
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
def test_pattern_description_has_no_naked_placeholders(
    file_name: str, key: str, value: str
) -> None:
    """The string must not contain bare ``{name}`` tokens.

    formatjs parses ``{name}`` as a variable to interpolate. Our pattern
    descriptions intend ``{camera}`` etc. as literal text the user can type
    into the input field, so they must be ICU-escaped as ``'{camera}'``.
    Without the escape, the HA frontend throws ``MISSING_VALUE`` errors and
    spams the log on every options-dialog render.
    """
    naked = _NAKED_PLACEHOLDER_RE.findall(value)
    assert not naked, (
        f"{file_name}::{key} contains naked ICU placeholders {naked} — "
        f"wrap them in single-quote escapes (e.g. `'{{camera}}'`) so formatjs "
        f"renders them as literal text instead of trying to interpolate."
    )


def test_pattern_descriptions_actually_escape_each_token() -> None:
    """Positive check: the fix is in place — `'{camera}'` is present in every file.

    Catches an aggressive future refactor that strips the helper-text entirely
    (which would silently make the field harder to use).
    """
    for path in _FILES:
        text = path.read_text(encoding="utf-8")
        assert "'{camera}'" in text, (
            f"{path.name} no longer mentions the camera placeholder — "
            f"either escape is missing or the field doc was removed entirely"
        )
