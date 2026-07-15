"""Self-tests for the format-robust source-assertion helper.

These guarantee the property the helper exists for: a fragment that matches the
single-line form still matches after ``ruff format`` rewraps the production code
(magic trailing comma, indentation, line breaks). If someone weakens
``normalize_source`` these tests fail before any regression pin silently rots.
"""

from __future__ import annotations

import pytest

from tests.source_match import assert_in_source, normalize_source


def test_matches_across_magic_trailing_comma_rewrap() -> None:
    """A one-line call still matches when the formatter expands it."""
    needle = "foo(a, b, c)"
    reformatted = "foo(\n        a,\n        b,\n        c,\n    )"
    assert_in_source(reformatted, needle)


def test_matches_regardless_of_inner_spacing() -> None:
    """Bracket / comma / colon spacing differences do not break the match."""
    # compact source (e.g. hand-written) vs spaced needle and vice versa
    assert_in_source("x = {'a':1,'b':2}", "x = {'a': 1, 'b': 2}")
    assert_in_source("foo(a , b ,c)", "foo(a, b, c)")


def test_collapses_newlines_and_indentation() -> None:
    source = (
        "    self.last_event_ids[cam_id] = newest_id\n    self.async_write_ha_state()\n"
    )
    assert_in_source(source, "self.last_event_ids[cam_id] = newest_id")


def test_missing_fragment_raises() -> None:
    with pytest.raises(AssertionError, match="not found in source"):
        assert_in_source("def foo(): pass", "def bar()")


def test_all_of_requires_every_needle() -> None:
    src = "alpha(); gamma()"
    assert_in_source(src, "alpha()", "gamma()")
    with pytest.raises(AssertionError):
        assert_in_source(src, "alpha()", "beta()")


def test_any_of_requires_one_needle() -> None:
    src = 'value = float("-inf")'
    # quote-style variants — ruff rewrites ' -> " so only one form survives
    assert_in_source(src, "float('-inf')", 'float("-inf")', any_of=True)
    with pytest.raises(AssertionError, match="none of the expected"):
        assert_in_source(src, "float('nan')", "float('+inf')", any_of=True)


def test_normalize_is_idempotent() -> None:
    once = normalize_source("foo(\n  a,\n  b,\n)")
    assert normalize_source(once) == once
    assert once == "foo(a,b)"
