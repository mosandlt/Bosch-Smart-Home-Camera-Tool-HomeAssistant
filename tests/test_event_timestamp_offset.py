"""Regression tests for Bosch event-timestamp offset handling (GitHub issue #34).

Reporter (GhostRider2809, Gen1 Eyes Outdoor FW 7.91.56) saw
``sensor.<cam>_last_event`` show the event time exactly +2h (CEST) in v13.7.1;
it was correct in v13.5.0.

Root cause: Bosch /v11/events timestamps carry an explicit timezone
designator — live format is an offset plus an RFC-9557 zone bracket, e.g.
``"2026-06-18T06:06:30.499+02:00[Europe/Berlin]"`` (historically a ``Z``
suffix). The v13.7.0 code did ``ts_str[:19]`` which DISCARDED the ``+02:00``
offset, then ``.replace(tzinfo=UTC)`` re-labelled the local wall-clock reading
as UTC → the instant shifted +2h in CEST. The same truncation affected:
  - the motion active-window check (events appeared ~2h in the future → the
    window check stayed satisfied → motion stuck on), and
  - the events-today / movement / audio counters (local-date events bucketed
    against a UTC "today" → mis-count in the hours around local midnight).

Fix: honor the designator Bosch sends via
``time_utils.parse_bosch_timestamp`` instead of truncating it away.

Ground truth captured live 2026-06-18 from the real account:
  raw "2026-06-18T06:06:30.499+02:00[Europe/Berlin]" → true instant 04:06:30Z.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
BERLIN = ZoneInfo("Europe/Berlin")

# Live-observed raw formats.
RAW_OFFSET = "2026-06-18T06:06:30.499+02:00[Europe/Berlin]"
RAW_OFFSET_INSTANT = datetime(2026, 6, 18, 4, 6, 30, 499000, tzinfo=UTC)
RAW_Z = "2026-03-22T14:30:00.000Z"
RAW_Z_INSTANT = datetime(2026, 3, 22, 14, 30, 0, tzinfo=UTC)
RAW_NAIVE = "2026-03-19T09:32:08"
RAW_NAIVE_INSTANT = datetime(2026, 3, 19, 9, 32, 8, tzinfo=UTC)


def _coord(events: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            CAM_ID: {
                "info": {
                    "title": "Terrasse",
                    "hardwareVersion": "HOME_Eyes_Outdoor",
                    "firmwareVersion": "9.40.25",
                    "macAddress": "aa:bb:cc:dd:ee:01",
                    "featureSupport": {"light": True},
                },
                "status": "ONLINE",
                "events": events,
            }
        },
    )


def _entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="ENTRY01", data={}, options={})


# ── parse_bosch_timestamp ───────────────────────────────────────────────────


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


# ── BoschCameraLastEventSensor — the reported +2h bug ───────────────────────


class TestLastEventSensorOffset:
    def test_offset_timestamp_not_shifted_2h(self) -> None:
        """Issue #34: native_value instant must equal the true offset instant."""
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(
            _coord([{"eventType": "MOVEMENT", "timestamp": RAW_OFFSET}]),
            CAM_ID,
            _entry(),
        )
        val = s.native_value
        assert val is not None
        assert val.astimezone(UTC) == RAW_OFFSET_INSTANT
        # The bug rendered this two hours later.
        assert val.astimezone(UTC) != datetime(
            2026, 6, 18, 6, 6, 30, 499000, tzinfo=UTC
        )

    def test_z_timestamp_preserved(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(
            _coord([{"eventType": "MOVEMENT", "timestamp": RAW_Z}]),
            CAM_ID,
            _entry(),
        )
        val = s.native_value
        assert val is not None
        assert val.astimezone(UTC) == RAW_Z_INSTANT

    def test_no_events_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(_coord([]), CAM_ID, _entry())
        assert s.native_value is None

    def test_garbage_timestamp_returns_none(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraLastEventSensor,
        )

        s = BoschCameraLastEventSensor(
            _coord([{"eventType": "MOVEMENT", "timestamp": "garbage"}]),
            CAM_ID,
            _entry(),
        )
        assert s.native_value is None


# ── "today" buckets across the local/UTC midnight boundary ──────────────────


class TestTodayBucketsLocalDate:
    """Buckets must use the LOCAL date of the event instant, not a UTC prefix.

    Scenario: HA in Europe/Berlin, local now = 2026-06-18 01:00 (= 2026-06-17
    23:00 UTC). An event at 2026-06-18 00:30+02:00 is local-today but its UTC
    date is 2026-06-17. The old UTC-prefix bucketing counted 0; correct local
    bucketing counts 1.
    """

    NOW_BERLIN = datetime(2026, 6, 18, 1, 0, 0, tzinfo=BERLIN)
    EVT_LOCAL_TODAY = "2026-06-18T00:30:00.000+02:00[Europe/Berlin]"
    EVT_LOCAL_YESTERDAY = "2026-06-17T23:00:00.000+02:00[Europe/Berlin]"

    def _patch_local(self):  # type: ignore[no-untyped-def]
        # Drive both dt_util.now() and dt_util.as_local() to Europe/Berlin
        # without mutating HA's global default timezone.
        mod = "custom_components.bosch_shc_camera.sensor.dt_util"
        return (
            patch(f"{mod}.now", return_value=self.NOW_BERLIN),
            patch(f"{mod}.as_local", side_effect=lambda dt: dt.astimezone(BERLIN)),
        )

    def test_events_today_counts_local_today(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraEventsTodaySensor,
        )

        s = BoschCameraEventsTodaySensor(
            _coord(
                [
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_YESTERDAY},
                ]
            ),
            CAM_ID,
            _entry(),
        )
        p_now, p_local = self._patch_local()
        with p_now, p_local:
            assert s.native_value == 1
            attrs = s.extra_state_attributes
        assert attrs["events_in_feed"] == 2
        assert len(attrs["latest_timestamps"]) == 1

    def test_movement_today_counts_local_today(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschMovementEventsTodaySensor,
        )

        s = BoschMovementEventsTodaySensor(
            _coord(
                [
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "AUDIO_ALARM", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "MOVEMENT", "timestamp": self.EVT_LOCAL_YESTERDAY},
                ]
            ),
            CAM_ID,
            _entry(),
        )
        p_now, p_local = self._patch_local()
        with p_now, p_local:
            assert s.native_value == 1

    def test_audio_today_counts_local_today(self) -> None:
        from custom_components.bosch_shc_camera.sensor import (
            BoschAudioEventsTodaySensor,
        )

        s = BoschAudioEventsTodaySensor(
            _coord(
                [
                    {"eventType": "AUDIO_ALARM", "timestamp": self.EVT_LOCAL_TODAY},
                    {"eventType": "AUDIO_ALARM", "timestamp": self.EVT_LOCAL_YESTERDAY},
                ]
            ),
            CAM_ID,
            _entry(),
        )
        p_now, p_local = self._patch_local()
        with p_now, p_local:
            assert s.native_value == 1


# ── motion active-window: offset must not make events look future ───────────


class TestMotionWindowOffset:
    """The motion window must compute true age from the offset instant.

    With the old truncation an event whose local reading is "now" but is in
    fact hours old (or just the +2h shift) appeared in the future → the
    ``age <= window`` check stayed satisfied → motion stuck on.
    """

    def _offset_iso(self, *, minutes_ago: int) -> str:
        ts = datetime.now(BERLIN) - timedelta(minutes=minutes_ago)
        return ts.isoformat() + "[Europe/Berlin]"

    def test_stale_offset_event_is_off(self) -> None:
        """Event 5 min old (default window 90 s) must be OFF, not stuck on."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(
            _coord(
                [
                    {
                        "eventType": "MOVEMENT",
                        "id": "e1",
                        "timestamp": self._offset_iso(minutes_ago=5),
                    }
                ]
            ),
            CAM_ID,
            _entry(),
        )
        assert s.is_on is False

    def test_fresh_offset_event_is_on(self) -> None:
        """A just-now event (offset format) must be ON."""
        from custom_components.bosch_shc_camera.binary_sensor import (
            BoschMotionBinarySensor,
        )

        s = BoschMotionBinarySensor(
            _coord(
                [
                    {
                        "eventType": "MOVEMENT",
                        "id": "e1",
                        "timestamp": self._offset_iso(minutes_ago=0),
                    }
                ]
            ),
            CAM_ID,
            _entry(),
        )
        assert s.is_on is True
