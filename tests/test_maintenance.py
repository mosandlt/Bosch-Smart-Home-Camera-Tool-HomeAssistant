"""Tests for the Bosch community RSS maintenance fetcher (maintenance.py).

Background: Bosch announces maintenance windows in their community forum
(community.bosch-smarthome.com/.../Wartungsarbeiten). The 19.05.2026 camera
maintenance reported by Thomas was at 07:00–10:00 MESZ — fixture below uses
that real announcement as a regression input.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.bosch_shc_camera.maintenance import (
    MaintenanceWindow,
    _is_camera_relevant,
    _parse_feed_body,
    _parse_html_fallback,
    _parse_pub_date,
    _parse_window,
    _prefers,
    async_fetch_maintenance,
)


BERLIN = ZoneInfo("Europe/Berlin")


REAL_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Wartungsarbeiten</title>
    <item>
      <title>Wartung: Kamera-Infrastruktur (Di., 19.05.2026)</title>
      <link>https://community.bosch-smarthome.com/t5/wartungsarbeiten/wartung-kamera-infrastruktur-di-19-05-2026/ba-p/110703</link>
      <pubDate>Mon, 18 May 2026 10:06:13 GMT</pubDate>
      <description><![CDATA[<P>wir arbeiten an Kameras. Wartungsarbeiten an der Kamera-Infrastruktur eingeplant. Diese finden zwischen <STRONG>07:00 und 10:00 Uhr (MESZ)</STRONG> statt. Bei manchen von euch kann es daher in diesem Zeitraum zu Einschränkungen von bis zu 30 Minuten kommen am 19.05.2026.</P>]]></description>
    </item>
  </channel>
</rss>""".encode("utf-8")


# ── _parse_window ────────────────────────────────────────────────────────


class TestParseWindow:
    def test_real_announcement_msz(self):
        pub = datetime(2026, 5, 18, 10, 6, 13, tzinfo=timezone.utc)
        text = "Wartung am 19.05.2026 zwischen 07:00 und 10:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start == datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)

    def test_winter_mez_offset(self):
        pub = datetime(2026, 1, 14, 9, 0, tzinfo=timezone.utc)
        text = "Wartung am 15.01.2026 von 02:00 bis 04:00 Uhr (MEZ)"
        start, end = _parse_window(text, pub)
        assert start == datetime(2026, 1, 15, 1, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc)

    def test_falls_back_to_pub_date_when_no_date_in_text(self):
        pub = datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc)
        text = "Wartung von 07:00 bis 10:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start is not None and end is not None
        assert start.astimezone(BERLIN).day == 19

    def test_returns_none_when_no_time_range(self):
        pub = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
        text = "Geplante Wartung — wir melden uns mit Details"
        assert _parse_window(text, pub) == (None, None)

    def test_endash_separator(self):
        pub = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
        text = "Wartung am 19.05.2026 von 07:00 – 10:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start is not None and end is not None

    def test_end_before_start_rolls_to_next_day(self):
        pub = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
        text = "Wartung am 19.05.2026 von 23:00 bis 02:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start is not None and end is not None
        assert end > start
        assert (end - start) == timedelta(hours=3)


# ── MaintenanceWindow.state() ────────────────────────────────────────────


class TestState:
    def _mw(self, start=None, end=None, pub=None, **kw):
        defaults = {
            "title": "x", "link": "x", "summary": "x",
            "source": "rss:x", "camera_relevant": False,
            "pub_date": pub or datetime(2026, 5, 19, tzinfo=timezone.utc),
            "scheduled_start": start,
            "scheduled_end": end,
        }
        defaults.update(kw)
        return MaintenanceWindow(**defaults)

    def test_active_when_now_inside_window(self):
        mw = self._mw(
            start=datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 19, 7, 30, tzinfo=timezone.utc)
        assert mw.state(now) == "active"

    def test_scheduled_when_window_in_future(self):
        mw = self._mw(
            start=datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 19, 4, 0, tzinfo=timezone.utc)
        assert mw.state(now) == "scheduled"

    def test_past_when_window_already_ended(self):
        mw = self._mw(
            start=datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        assert mw.state(now) == "past"

    def test_recent_when_no_window_but_pub_fresh(self):
        mw = self._mw(pub=datetime(2026, 5, 18, tzinfo=timezone.utc))
        now = datetime(2026, 5, 19, tzinfo=timezone.utc)
        assert mw.state(now) == "recent"

    def test_unknown_when_no_window_and_old(self):
        mw = self._mw(pub=datetime(2026, 1, 1, tzinfo=timezone.utc))
        now = datetime(2026, 5, 19, tzinfo=timezone.utc)
        assert mw.state(now) == "unknown"


# ── _is_camera_relevant ─────────────────────────────────────────────────


class TestCameraRelevance:
    @pytest.mark.parametrize("text", [
        "Kamera-Infrastruktur Wartung",
        "video streams unavailable",
        "Cloud-Backend Störung",
        "CBS service maintenance",
    ])
    def test_relevant_keywords_hit(self, text):
        assert _is_camera_relevant(text, "")

    @pytest.mark.parametrize("text", [
        "Heizung Update", "Thermostat-Firmware", "Tür-/Fenster-Kontakt rollout",
    ])
    def test_unrelated_keywords_miss(self, text):
        assert not _is_camera_relevant(text, "")


# ── _parse_feed_body ────────────────────────────────────────────────────


class TestParseFeedBody:
    def test_real_rss_fixture(self):
        mw = _parse_feed_body(REAL_RSS, "https://x?board.id=Wartungsarbeiten")
        assert mw is not None
        assert mw.title.startswith("Wartung: Kamera-Infrastruktur")
        assert mw.scheduled_start == datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc)
        assert mw.scheduled_end == datetime(2026, 5, 19, 8, 0, tzinfo=timezone.utc)
        assert mw.camera_relevant is True
        assert mw.source == "rss:Wartungsarbeiten"

    def test_empty_xml_returns_none(self):
        assert _parse_feed_body(b"<rss><channel/></rss>", "x") is None

    def test_invalid_xml_returns_none(self):
        assert _parse_feed_body(b"not xml at all", "x") is None

    def test_atom_format(self):
        atom = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Wartung Kamera am 20.05.2026 von 09:00 bis 10:00 Uhr (MESZ)</title>
    <link href="https://example/x"/>
    <updated>2026-05-19T12:00:00Z</updated>
    <summary>Camera maintenance</summary>
  </entry>
</feed>"""
        mw = _parse_feed_body(atom, "https://x?board.id=Statusmeldungen")
        assert mw is not None
        assert mw.camera_relevant is True
        assert mw.scheduled_start == datetime(2026, 5, 20, 7, 0, tzinfo=timezone.utc)


# ── _prefers ────────────────────────────────────────────────────────────


class TestPrefers:
    def _mw(self, **kw):
        defaults = {
            "title": "x", "link": "x", "summary": "x", "source": "rss:x",
            "pub_date": datetime(2026, 5, 19, tzinfo=timezone.utc),
            "scheduled_start": None, "scheduled_end": None,
            "camera_relevant": False,
        }
        defaults.update(kw)
        return MaintenanceWindow(**defaults)

    def test_active_beats_scheduled(self, freezer):
        # Freeze wall clock so _prefers's internal state() call (which uses
        # utcnow as default) lands inside the active window. Without freezing,
        # the test only passes between 05:00 and 09:00 UTC.
        freezer.move_to("2026-05-19T07:00:00+00:00")
        active = self._mw(
            scheduled_start=datetime(2026, 5, 19, 5, 0, tzinfo=timezone.utc),
            scheduled_end=datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc),
        )
        scheduled = self._mw(
            scheduled_start=datetime(2026, 5, 20, 5, 0, tzinfo=timezone.utc),
            scheduled_end=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        )
        assert active.state() == "active"
        assert scheduled.state() == "scheduled"
        assert _prefers(active, scheduled)

    def test_camera_relevant_breaks_tie(self):
        a = self._mw(camera_relevant=True)
        b = self._mw(camera_relevant=False)
        assert _prefers(a, b)

    def test_newer_pub_date_wins_on_tie(self):
        a = self._mw(pub_date=datetime(2026, 5, 19, tzinfo=timezone.utc))
        b = self._mw(pub_date=datetime(2026, 5, 10, tzinfo=timezone.utc))
        assert _prefers(a, b)


# ── _parse_html_fallback ────────────────────────────────────────────────


class TestHtmlFallback:
    def test_extracts_first_item(self):
        html = b"""<html>
<head><meta name="description" content="Geplant: Wartung am 19.05.2026 von 07:00 bis 10:00 Uhr (MESZ) Kamera-Infrastruktur"></head>
<body><a href="/t5/wartungsarbeiten/foo/ba-p/110703">Wartung: Kamera-Infrastruktur Di. 19.05.2026</a></body>
</html>"""
        mw = _parse_html_fallback(html, "https://x/bg-p/Wartungsarbeiten")
        assert mw is not None
        assert mw.link.endswith("ba-p/110703")
        assert mw.camera_relevant is True
        assert mw.source.startswith("html:")
        assert mw.scheduled_start is not None

    def test_returns_none_without_item_anchor(self):
        assert _parse_html_fallback(b"<html><body>nope</body></html>", "x") is None


# ── async_fetch_maintenance (with mocked aiohttp) ───────────────────────


class _MockResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self._body


class _MockSession:
    def __init__(self, responses):
        # responses: dict of url-substring -> (status, body) OR Exception
        self._responses = responses
        self.calls: list[str] = []

    def get(self, url, headers=None):
        self.calls.append(url)
        for key, value in self._responses.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return _MockResp(*value)
        return _MockResp(404, b"")


@pytest.mark.asyncio
class TestFetchEndToEnd:
    async def test_primary_rss_success(self):
        sess = _MockSession({"Wartungsarbeiten": (200, REAL_RSS)})
        mw = await async_fetch_maintenance(sess)  # type: ignore[arg-type]
        assert mw is not None
        assert mw.camera_relevant is True
        assert mw.source == "rss:Wartungsarbeiten"

    async def test_falls_through_to_secondary_rss_on_503(self):
        sess = _MockSession({
            "Wartungsarbeiten": (503, b""),
            "Statusmeldungen": (200, REAL_RSS.replace(b"Wartungsarbeiten", b"Statusmeldungen")),
        })
        mw = await async_fetch_maintenance(sess)  # type: ignore[arg-type]
        assert mw is not None
        # Even when Wartungsarbeiten is dead, Statusmeldungen yields a result.

    async def test_falls_through_to_html_when_all_rss_fail(self):
        html = b"""<html>
<head><meta name="description" content="Wartung Kamera am 19.05.2026 von 07:00 bis 10:00 Uhr (MESZ)"></head>
<body><a href="/t5/wartungsarbeiten/foo/ba-p/110703">Wartung Kamera</a></body>
</html>"""
        sess = _MockSession({
            "rss/board": (503, b""),
            "bg-p": (200, html),
        })
        mw = await async_fetch_maintenance(sess)  # type: ignore[arg-type]
        assert mw is not None
        assert mw.source.startswith("html:")

    async def test_all_sources_fail_returns_none(self):
        sess = _MockSession({})  # all 404
        mw = await async_fetch_maintenance(sess)  # type: ignore[arg-type]
        assert mw is None

    async def test_network_exception_does_not_propagate(self):
        import aiohttp
        sess = _MockSession({"Wartungsarbeiten": aiohttp.ClientError("DNS down")})
        # All other URLs will be 404 → final result is None, no exception.
        mw = await async_fetch_maintenance(sess)  # type: ignore[arg-type]
        assert mw is None


# ── _parse_pub_date ──────────────────────────────────────────────────────


class TestParsePubDate:
    def test_rss_format(self):
        d = _parse_pub_date("Mon, 18 May 2026 10:06:13 GMT")
        assert d.tzinfo is not None and d.year == 2026 and d.day == 18

    def test_atom_zulu(self):
        d = _parse_pub_date("2026-05-19T12:00:00Z")
        assert d.year == 2026 and d.month == 5 and d.day == 19

    def test_unparseable_falls_back_to_now(self):
        before = datetime.now(tz=timezone.utc)
        d = _parse_pub_date("not a date")
        after = datetime.now(tz=timezone.utc)
        assert before <= d <= after
