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

from bosch_shc_camera_client.events import parse_event_timestamp

# Library parser: honors the offset/"Z", strips the RFC-9557 bracket, treats a
# naive value as UTC, returns None for empty or unparseable input.
parse_bosch_timestamp = parse_event_timestamp

__all__ = ["parse_bosch_timestamp"]
