"""Format-robust source-code substring assertions.

WHY THIS EXISTS
---------------
Many regression tests verify that a fix is actually wired into the code by
reading the module's own source — ``inspect.getsource(...)`` or
``Path(...).read_text()`` — and asserting that a code fragment is present::

    src = inspect.getsource(BoschCameraCoordinator.__init__)
    assert "self._stream_warming = set()" in src   # <-- BRITTLE

The problem: a naive ``"<fragment>" in source`` couples the test to the exact
*formatting* of the production code, not its *logic*. The moment ``ruff format``
(or black, or a manual reflow) rewraps a call that fits on one line today::

    foo(a, b, c)

into the magic-trailing-comma form tomorrow::

    foo(
        a,
        b,
        c,
    )

the literal substring no longer matches — even though the code is semantically
byte-for-byte equivalent. We lost ~7 such tests to exactly this when ``ruff
format`` landed on 2026-05-30 (see HARDEN_SOURCE_ASSERT_ROBUST in CLAUDE.md).

WHAT THIS DOES
--------------
``assert_in_source`` makes the match invariant under the formatter's degrees of
freedom. Before testing containment it normalizes BOTH the haystack and each
needle by:

  1. collapsing every run of whitespace (incl. newlines) to a single space,
  2. stripping spaces around structural punctuation ``( ) [ ] { } , :``,
  3. dropping a magic trailing comma immediately before a closing bracket.

So ``foo(a, b)``, ``foo(a,b)`` and the 4-line wrapped form all normalize to
``foo(a,b)`` and compare equal.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not rename identifiers, normalize string-quote style (``ruff`` rewrites
``'x'`` -> ``"x"``; pass the post-format form, or both forms via ``any_of=True``),
strip comments, or understand semantics. It is a *formatting-robust textual*
check and nothing more — keep needles specific enough that a coincidental match
is implausible.

USAGE
-----
    from tests.source_match import assert_in_source

    assert_in_source(src, "self._stream_warming = set()")          # one fragment
    assert_in_source(src, "frag a", "frag b")                       # all must match
    assert_in_source(src, 'float("-inf")', "float('-inf')",         # quote variants
                     any_of=True)
"""

from __future__ import annotations

import re

_WHITESPACE = re.compile(r"\s+")
_AROUND_PUNCT = re.compile(r"\s*([()\[\]{},:])\s*")
_TRAILING_COMMA = re.compile(r",(?=[)\]}])")


def normalize_source(code: str) -> str:
    """Return *code* with formatting-only differences flattened away.

    Idempotent: ``normalize_source(normalize_source(x)) == normalize_source(x)``.
    """
    code = _WHITESPACE.sub(" ", code)
    code = _AROUND_PUNCT.sub(r"\1", code)
    code = _TRAILING_COMMA.sub("", code)
    return code.strip()


def assert_in_source(source: str, *needles: str, any_of: bool = False) -> None:
    """Assert code *needles* appear in *source*, ignoring formatting differences.

    By default every needle must be present (logical AND). With ``any_of=True``
    at least one needle must be present (logical OR) — use this for variants the
    formatter or developer may legitimately choose between (e.g. quote style).

    Raises ``AssertionError`` with the un-normalized needle(s) in the message so
    failures stay readable.
    """
    haystack = normalize_source(source)
    if any_of:
        if not any(normalize_source(n) in haystack for n in needles):
            raise AssertionError(
                "none of the expected fragments were found in source "
                f"(whitespace/format-normalized): {list(needles)!r}"
            )
        return
    for needle in needles:
        if normalize_source(needle) not in haystack:
            raise AssertionError(
                "expected fragment not found in source "
                f"(whitespace/format-normalized): {needle!r}"
            )
