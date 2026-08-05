"""Timestamp parsing helpers for Bosch /v11/events data.

Bosch event timestamps are **offset-bearing** and must be parsed with the
timezone designator the API sends — they must NOT be truncated to a naive
"YYYY-MM-DDTHH:MM:SS" string and then assumed to be UTC (or local).

Observed live formats (FW 7.91.56 Gen1 and FW 9.40.x Gen2):

    "2026-06-18T06:06:30.499+02:00[Europe/Berlin]"   ← current API: explicit
                                                        offset + RFC-9557 zone
    "2026-03-22T14:30:00.000Z"                       ← older / some accounts
    "2026-03-19T09:32:08"                            ← naive (rare/legacy)

`datetime.fromisoformat` (Python 3.11+) handles both the numeric ``+02:00``
offset and a trailing ``Z``; it cannot parse the RFC-9557 ``[Europe/Berlin]``
bracket suffix, so that is stripped first.

A previous naive ``ts_str[:19]`` truncation discarded the offset and
re-labelled the local wall-clock reading as UTC — this showed
``sensor.<cam>_last_event`` exactly +2h (CEST) off, made the motion
active-window check see events 2h in the future (motion stuck on), and
bucketed local-date events against a UTC "today" in the events-today /
movement / audio counters.
"""

from __future__ import annotations

from datetime import UTC, datetime


def parse_bosch_timestamp(ts_str: str | None) -> datetime | None:
    """Parse a Bosch event timestamp into a timezone-aware UTC datetime.

    Honors the timezone designator the API sends (``+02:00`` or ``Z``). A
    naive timestamp (no offset, no ``Z``) is assumed to be UTC — this branch
    is effectively never hit by the live API but keeps behavior defined.

    Returns ``None`` for empty or unparseable input.
    """
    if not ts_str:
        return None
    # Strip the RFC-9557 zone bracket suffix, e.g. "...+02:00[Europe/Berlin]".
    # fromisoformat understands the numeric offset / "Z" but not the bracket.
    iso = ts_str.split("[", 1)[0]
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Legacy naive form — define it as UTC (the historical Bosch "Z" tier).
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)
