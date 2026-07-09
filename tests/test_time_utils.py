"""Tests for time_utils.py — the shared Bosch-timestamp parser.

Relocated from tests/test_event_timestamp_offset.py during the per-module
test-file consolidation (time_utils.py previously had no dedicated test
file). GitHub issue #34 (GhostRider2809, Gen1 Eyes Outdoor FW 7.91.56):
``sensor.<cam>_last_event`` showed the event time exactly +2h (CEST) because
the pre-fix code truncated Bosch's timezone designator (``ts_str[:19]``)
before parsing, then re-labelled the local wall-clock reading as UTC.

Bosch /v11/events timestamps carry an explicit timezone designator — live
format is an offset plus an RFC-9557 zone bracket, e.g.
``"2026-06-18T06:06:30.499+02:00[Europe/Berlin]"`` (historically a ``Z``
suffix). `time_utils.parse_bosch_timestamp` must honor whichever designator
Bosch sends instead of truncating it away.

Ground truth captured live 2026-06-18 from the real account:
raw "2026-06-18T06:06:30.499+02:00[Europe/Berlin]" -> true instant 04:06:30Z.

The sensor.py "today" bucket tests and the binary_sensor.py motion-window
test for this same issue live in tests/test_sensor.py and
tests/test_binary_sensor.py respectively.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

RAW_OFFSET = "2026-06-18T06:06:30.499+02:00[Europe/Berlin]"
RAW_OFFSET_INSTANT = datetime(2026, 6, 18, 4, 6, 30, 499000, tzinfo=UTC)
RAW_Z = "2026-03-22T14:30:00.000Z"
RAW_Z_INSTANT = datetime(2026, 3, 22, 14, 30, 0, tzinfo=UTC)
RAW_NAIVE = "2026-03-19T09:32:08"
RAW_NAIVE_INSTANT = datetime(2026, 3, 19, 9, 32, 8, tzinfo=UTC)


class TestParseBoschTimestamp:
    """The shared helper must honor every designator Bosch can send."""

    def test_offset_with_zone_bracket(self) -> None:
        from custom_components.bosch_shc_camera.time_utils import parse_bosch_timestamp

        dt = parse_bosch_timestamp(RAW_OFFSET)
        assert dt == RAW_OFFSET_INSTANT
        # The +2h bug returned 06:06:30Z — pin that it does NOT.
        assert dt != datetime(2026, 6, 18, 6, 6, 30, 499000, tzinfo=UTC)

    def test_z_suffix(self) -> None:
        from custom_components.bosch_shc_camera.time_utils import parse_bosch_timestamp

        assert parse_bosch_timestamp(RAW_Z) == RAW_Z_INSTANT

    def test_naive_treated_as_utc(self) -> None:
        from custom_components.bosch_shc_camera.time_utils import parse_bosch_timestamp

        assert parse_bosch_timestamp(RAW_NAIVE) == RAW_NAIVE_INSTANT

    def test_negative_offset(self) -> None:
        from custom_components.bosch_shc_camera.time_utils import parse_bosch_timestamp

        dt = parse_bosch_timestamp("2026-06-18T00:30:00-05:00[America/New_York]")
        assert dt == datetime(2026, 6, 18, 5, 30, 0, tzinfo=UTC)

    def test_result_is_utc_aware(self) -> None:
        from custom_components.bosch_shc_camera.time_utils import parse_bosch_timestamp

        dt = parse_bosch_timestamp(RAW_OFFSET)
        assert dt is not None
        assert dt.tzinfo == UTC

    @pytest.mark.parametrize("bad", ["", None, "not-a-date", "2026-13-99T99:99"])
    def test_invalid_returns_none(self, bad: object) -> None:
        from custom_components.bosch_shc_camera.time_utils import parse_bosch_timestamp

        assert parse_bosch_timestamp(bad) is None  # type: ignore[arg-type]
