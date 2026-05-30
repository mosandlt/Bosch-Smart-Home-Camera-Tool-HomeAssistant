"""Regression test for ICU MessageFormat / Hassfest compatibility in translations.

Two-validator squeeze (production-discovered 2026-05-15):

1. **HA frontend (formatjs / ICU MessageFormat)** parses any `{name}` token
   in a translation string as a runtime variable. If the backend doesn't
   provide a value for `name`, formatjs throws
   ``Error: MISSING_VALUE``. User Andrew75 reported repeated occurrences
   in his French frontend on every options-dialog render.

2. **Hassfest** (HA's official manifest validator, runs in CI on every PR)
   rejects strings containing the ICU escape syntax ``'{name}'``:

       [ERROR] [TRANSLATIONS] Invalid strings.json: the string should not
       contain placeholders inside single quotes for dictionary value @
       data['options']['step']['init']['sections']['events_storage']
       ['data_description']['file_pattern']

So **both** ``{name}`` and ``'{name}'`` are forbidden in helper-text /
``data_description`` strings. The only safe form for descriptions that
need to mention literal placeholder names is **prose** — list the
variable names plain-text and explain in words that the user should wrap
them in curly braces inside the actual pattern field.

This test pins the post-rewrite state: zero curly-brace tokens in any
``data_description.{folder_pattern,file_pattern}`` value across all
translation files. Prevents both regressions:

- A future copy-paste from another integration that drops naked
  ``{camera}`` → formatjs MISSING_VALUE returns.
- An over-eager future "fix" that re-introduces ``'{camera}'`` → Hassfest
  fails CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_INTEGRATION = Path(__file__).parent.parent / "custom_components" / "bosch_shc_camera"
_FILES = [
    _INTEGRATION / "strings.json",
    _INTEGRATION / "translations" / "en.json",
    _INTEGRATION / "translations" / "de.json",
]

# Keys whose values are literal pattern descriptions, not runtime templates.
_PATTERN_DESC_KEYS = ("folder_pattern", "file_pattern")

# Match any `{lowercase_token}` regardless of whether it's escape-wrapped
# with surrounding apostrophes. Both forms are forbidden by HA's
# validator/renderer combo.
_ANY_CURLY_TOKEN_RE = re.compile(r"\{[a-z_]+\}")


def _all_pattern_description_strings():
    """Yield (file, key, value) for each pattern-desc string in each file."""
    for path in _FILES:
        data = json.loads(path.read_text(encoding="utf-8"))
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

    for path in _FILES:
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
