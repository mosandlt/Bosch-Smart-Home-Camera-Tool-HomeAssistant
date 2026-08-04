"""Tests for the Bosch community RSS maintenance fetcher (maintenance.py) and
the coordinator hooks that consume it.

Background: Bosch announces maintenance windows in their community forum
(community.bosch-smarthome.com/.../Wartungsarbeiten). The 19.05.2026 camera
maintenance reported by Thomas was at 07:00–10:00 MESZ — fixture below uses
that real announcement as a regression input.

Covers, in order:
- `_parse_window` / `_parse_pub_date` / `_is_camera_relevant` / `_prefers`
  parsing and ranking helpers.
- `_parse_feed_body` (RSS + Atom) and `_parse_html_fallback`.
- `async_fetch_maintenance` end to end against a mocked aiohttp session.
- `BoschCameraCoordinator._async_maybe_announce_cloud_state`: the
  cloud-up/cloud-down transition notifier.
- `BoschCameraCoordinator._async_maybe_announce_maintenance`: the
  scheduled/active/past maintenance-window notify hook.
- `BoschCameraCoordinator._async_refresh_maintenance`: the periodic +
  reactive refresh helper (cooldown gating, cache retention on failure).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory

from custom_components.bosch_shc_camera import BoschCameraCoordinator
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
</rss>""".encode()


class TestParseWindow:
    def test_real_announcement_msz(self):
        pub = datetime(2026, 5, 18, 10, 6, 13, tzinfo=UTC)
        text = "Wartung am 19.05.2026 zwischen 07:00 und 10:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start == datetime(2026, 5, 19, 5, 0, tzinfo=UTC)
        assert end == datetime(2026, 5, 19, 8, 0, tzinfo=UTC)

    def test_winter_mez_offset(self):
        pub = datetime(2026, 1, 14, 9, 0, tzinfo=UTC)
        text = "Wartung am 15.01.2026 von 02:00 bis 04:00 Uhr (MEZ)"
        start, end = _parse_window(text, pub)
        assert start == datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
        assert end == datetime(2026, 1, 15, 3, 0, tzinfo=UTC)

    def test_falls_back_to_pub_date_when_no_date_in_text(self):
        pub = datetime(2026, 5, 19, 5, 0, tzinfo=UTC)
        text = "Wartung von 07:00 bis 10:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start is not None and end is not None
        assert start.astimezone(BERLIN).day == 19

    def test_returns_none_when_no_time_range(self):
        pub = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
        text = "Geplante Wartung — wir melden uns mit Details"
        assert _parse_window(text, pub) == (None, None)

    def test_endash_separator(self):
        pub = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
        text = "Wartung am 19.05.2026 von 07:00 – 10:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start is not None and end is not None

    def test_end_before_start_rolls_to_next_day(self):
        pub = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
        text = "Wartung am 19.05.2026 von 23:00 bis 02:00 Uhr (MESZ)"
        start, end = _parse_window(text, pub)
        assert start is not None and end is not None
        assert end > start
        assert (end - start) == timedelta(hours=3)


class TestState:
    def _mw(self, start=None, end=None, pub=None, **kw):
        defaults = {
            "title": "x",
            "link": "x",
            "summary": "x",
            "source": "rss:x",
            "camera_relevant": False,
            "pub_date": pub or datetime(2026, 5, 19, tzinfo=UTC),
            "scheduled_start": start,
            "scheduled_end": end,
        }
        defaults.update(kw)
        return MaintenanceWindow(**defaults)

    def test_active_when_now_inside_window(self):
        mw = self._mw(
            start=datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
            end=datetime(2026, 5, 19, 8, 0, tzinfo=UTC),
        )
        now = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
        assert mw.state(now) == "active"

    def test_scheduled_when_window_in_future(self):
        mw = self._mw(
            start=datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
            end=datetime(2026, 5, 19, 8, 0, tzinfo=UTC),
        )
        now = datetime(2026, 5, 19, 4, 0, tzinfo=UTC)
        assert mw.state(now) == "scheduled"

    def test_past_when_window_already_ended(self):
        mw = self._mw(
            start=datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
            end=datetime(2026, 5, 19, 8, 0, tzinfo=UTC),
        )
        now = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
        assert mw.state(now) == "past"

    def test_recent_when_no_window_but_pub_fresh(self):
        mw = self._mw(pub=datetime(2026, 5, 18, tzinfo=UTC))
        now = datetime(2026, 5, 19, tzinfo=UTC)
        assert mw.state(now) == "recent"

    def test_unknown_when_no_window_and_old(self):
        mw = self._mw(pub=datetime(2026, 1, 1, tzinfo=UTC))
        now = datetime(2026, 5, 19, tzinfo=UTC)
        assert mw.state(now) == "unknown"


class TestCameraRelevance:
    @pytest.mark.parametrize(
        "text",
        [
            "Kamera-Infrastruktur Wartung",
            "video streams unavailable",
            "Cloud-Backend Störung",
            "CBS service maintenance",
        ],
    )
    def test_relevant_keywords_hit(self, text: str):
        assert _is_camera_relevant(text, "")

    @pytest.mark.parametrize(
        "text",
        [
            "Heizung Update",
            "Thermostat-Firmware",
            "Tür-/Fenster-Kontakt rollout",
        ],
    )
    def test_unrelated_keywords_miss(self, text: str):
        assert not _is_camera_relevant(text, "")


class TestParseFeedBody:
    def test_real_rss_fixture(self):
        mw = _parse_feed_body(REAL_RSS, "https://x?board.id=Wartungsarbeiten")
        assert mw is not None
        assert mw.title.startswith("Wartung: Kamera-Infrastruktur")
        assert mw.scheduled_start == datetime(2026, 5, 19, 5, 0, tzinfo=UTC)
        assert mw.scheduled_end == datetime(2026, 5, 19, 8, 0, tzinfo=UTC)
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
        assert mw.scheduled_start == datetime(2026, 5, 20, 7, 0, tzinfo=UTC)


class TestPrefers:
    def _mw(self, **kw):
        defaults = {
            "title": "x",
            "link": "x",
            "summary": "x",
            "source": "rss:x",
            "pub_date": datetime(2026, 5, 19, tzinfo=UTC),
            "scheduled_start": None,
            "scheduled_end": None,
            "camera_relevant": False,
        }
        defaults.update(kw)
        return MaintenanceWindow(**defaults)

    def test_active_beats_scheduled(self, freezer: FrozenDateTimeFactory):
        # Freeze wall clock so _prefers's internal state() call (which uses
        # utcnow as default) lands inside the active window. Without freezing,
        # the test only passes between 05:00 and 09:00 UTC.
        freezer.move_to("2026-05-19T07:00:00+00:00")
        active = self._mw(  # type: ignore[no-untyped-call]
            scheduled_start=datetime(2026, 5, 19, 5, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 5, 19, 9, 0, tzinfo=UTC),
        )
        scheduled = self._mw(  # type: ignore[no-untyped-call]
            scheduled_start=datetime(2026, 5, 20, 5, 0, tzinfo=UTC),
            scheduled_end=datetime(2026, 5, 20, 9, 0, tzinfo=UTC),
        )
        assert active.state() == "active"
        assert scheduled.state() == "scheduled"
        assert _prefers(active, scheduled)

    def test_camera_relevant_breaks_tie(self):
        a = self._mw(camera_relevant=True)
        b = self._mw(camera_relevant=False)
        assert _prefers(a, b)

    def test_newer_pub_date_wins_on_tie(self):
        a = self._mw(pub_date=datetime(2026, 5, 19, tzinfo=UTC))
        b = self._mw(pub_date=datetime(2026, 5, 10, tzinfo=UTC))
        assert _prefers(a, b)


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
        sess = _MockSession(
            {
                "Wartungsarbeiten": (503, b""),
                "Statusmeldungen": (
                    200,
                    REAL_RSS.replace(b"Wartungsarbeiten", b"Statusmeldungen"),
                ),
            }
        )
        mw = await async_fetch_maintenance(sess)  # type: ignore[arg-type]
        assert mw is not None
        # Even when Wartungsarbeiten is dead, Statusmeldungen yields a result.

    async def test_falls_through_to_html_when_all_rss_fail(self):
        html = b"""<html>
<head><meta name="description" content="Wartung Kamera am 19.05.2026 von 07:00 bis 10:00 Uhr (MESZ)"></head>
<body><a href="/t5/wartungsarbeiten/foo/ba-p/110703">Wartung Kamera</a></body>
</html>"""
        sess = _MockSession(
            {
                "rss/board": (503, b""),
                "bg-p": (200, html),
            }
        )
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


class TestParsePubDate:
    def test_rss_format(self):
        d = _parse_pub_date("Mon, 18 May 2026 10:06:13 GMT")
        assert d.tzinfo is not None and d.year == 2026 and d.day == 18

    def test_atom_zulu(self):
        d = _parse_pub_date("2026-05-19T12:00:00Z")
        assert d.year == 2026 and d.month == 5 and d.day == 19

    def test_unparseable_falls_back_to_now(self):
        before = datetime.now(tz=UTC)
        d = _parse_pub_date("not a date")
        after = datetime.now(tz=UTC)
        assert before <= d <= after


# BoschCameraCoordinator._async_maybe_announce_cloud_state
#
# Pins:
# - First observation (healthy or failed) is silent — baseline only.
# - One-tick failure blips never fire (must persist ≥ _CLOUD_OUTAGE_NOTIFY_AFTER_S).
# - Outage announcement fires exactly once when the threshold is crossed.
# - Recovery fires immediately when the next success arrives after an outage.
# - Active RSS maintenance suppresses both outage and recovery announcements.
# - Notify-service failure is swallowed.
# - No notify service configured = silent + state still tracked.


def _make_cloud_state_coord(
    notify_service: str = "thomas", maintenance: MaintenanceWindow | None = None
) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.options = {"alert_notify_service": notify_service}
    coord._cloud_outage_started_at = None
    coord.cloud_outage_notified = False
    coord._CLOUD_OUTAGE_NOTIFY_AFTER_S = 60.0
    coord.maintenance_cache = maintenance
    coord.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    # No `_async_dispatch_cloud_alert` binding needed here (style audit,
    # 2026-08-04): the actual notify-dispatch logic moved to
    # announcements._dispatch_cloud_alert, a module-private free function
    # that announcements.maybe_announce_cloud_state calls directly — it
    # is no longer reached via a `coordinator._async_dispatch_cloud_alert`
    # attribute at all, so this stub doesn't need to provide one.
    return coord


def _active_maintenance() -> MaintenanceWindow:
    ref = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
    return MaintenanceWindow(
        title="Wartung Kamera-Infrastruktur",
        link="https://example/x",
        pub_date=ref - timedelta(hours=12),
        summary="07:00–10:00 MESZ",
        scheduled_start=ref - timedelta(hours=1),
        scheduled_end=ref + timedelta(hours=2),
        source="rss:Wartungsarbeiten",
        camera_relevant=True,
    )


@pytest.mark.asyncio
class TestCloudStateAnnounce:
    async def test_first_success_is_silent(self):
        coord = _make_cloud_state_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_not_called()

    async def test_first_failure_is_silent(self):
        """Single failed tick must never fire — could be a transient blip."""
        coord = _make_cloud_state_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_not_called()
        assert coord._cloud_outage_started_at == 1000.0
        assert coord.cloud_outage_notified is False

    async def test_failure_under_threshold_stays_silent(self):
        coord = _make_cloud_state_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1030.0
        ):  # +30s
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_not_called()
        assert coord.cloud_outage_notified is False

    async def test_failure_past_threshold_fires_once(self):
        coord = _make_cloud_state_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1070.0
        ):  # +70s
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_called_once()
        args = coord.hass.services.async_call.await_args.args
        assert args[0] == "notify"
        assert args[1] == "thomas"
        assert "nicht erreichbar" in args[2]["title"].lower()
        assert coord.cloud_outage_notified is True
        # Subsequent failed ticks don't re-fire.
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1200.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        assert coord.hass.services.async_call.await_count == 1

    async def test_blip_clears_without_announcing(self):
        """One failed tick followed by a success must not announce anything."""
        coord = _make_cloud_state_coord()
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1010.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_not_called()
        assert coord._cloud_outage_started_at is None
        assert coord.cloud_outage_notified is False

    async def test_recovery_fires_immediately(self):
        coord = _make_cloud_state_coord()
        coord._cloud_outage_started_at = 1000.0
        coord.cloud_outage_notified = True
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1500.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_called_once()
        title = coord.hass.services.async_call.await_args.args[2]["title"]
        assert "wieder erreichbar" in title.lower()
        assert coord.cloud_outage_notified is False
        assert coord._cloud_outage_started_at is None

    async def test_active_maintenance_suppresses_outage(self):
        coord = _make_cloud_state_coord(maintenance=_active_maintenance())
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
            ),
            patch("custom_components.bosch_shc_camera.maintenance.datetime") as dt_mock,
        ):
            dt_mock.now.return_value = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0
        ):
            with patch.object(
                MaintenanceWindow,
                "state",
                return_value="active",
            ):
                await BoschCameraCoordinator._async_maybe_announce_cloud_state(
                    coord, False
                )
        coord.hass.services.async_call.assert_not_called()
        # Internal state still flipped so a recovery during maintenance does
        # not later re-fire — but no notification was sent.
        assert coord.cloud_outage_notified is True

    async def test_active_maintenance_suppresses_recovery(self):
        coord = _make_cloud_state_coord(maintenance=_active_maintenance())
        coord.cloud_outage_notified = True
        coord._cloud_outage_started_at = 1000.0
        with patch.object(MaintenanceWindow, "state", return_value="active"):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, True)
        coord.hass.services.async_call.assert_not_called()
        # Tracker still reset so the next genuine outage starts fresh.
        assert coord.cloud_outage_notified is False
        assert coord._cloud_outage_started_at is None

    async def test_no_service_configured_still_tracks_state(self):
        coord = _make_cloud_state_coord(notify_service="")
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_not_called()
        # State still tracked so configuring a service mid-outage doesn't
        # surface a stale notification on the next failed tick.
        assert coord.cloud_outage_notified is True

    async def test_notify_failure_is_swallowed(self):
        coord = _make_cloud_state_coord()
        coord.hass.services.async_call = AsyncMock(side_effect=RuntimeError("svc down"))
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0
        ):
            # Must not raise.
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        # State still flipped so we do not retry-storm.
        assert coord.cloud_outage_notified is True

    async def test_multiple_services_all_called(self):
        coord = _make_cloud_state_coord(notify_service="thomas, signalhome")
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        assert coord.hass.services.async_call.await_count == 2
        services = {c.args[1] for c in coord.hass.services.async_call.await_args_list}
        assert services == {"thomas", "signalhome"}

    async def test_notify_prefix_stripped(self):
        """A configured `notify.thomas` (with the `notify.` prefix already
        present) must not be concatenated onto the hardcoded `domain="notify"`
        — that combination would call `notify.notify.thomas`, which HA
        rejects with `Action notify.notify.thomas not found`. The helper
        splits any `notify.<name>` form so the call lands as
        `domain="notify"`, `service="<name>"`."""
        coord = _make_cloud_state_coord(notify_service="notify.thomas")
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        with patch(
            "custom_components.bosch_shc_camera.time.monotonic", return_value=1080.0
        ):
            await BoschCameraCoordinator._async_maybe_announce_cloud_state(coord, False)
        coord.hass.services.async_call.assert_called_once()
        args = coord.hass.services.async_call.await_args.args
        assert args[0] == "notify"
        assert args[1] == "thomas"  # NOT "notify.thomas"


# BoschCameraCoordinator._async_maybe_announce_maintenance
#
# Pin every transition path so the same window can never spam the user, but a
# genuine state change (scheduled -> active) gets one fresh announcement.


def _mw_for_announce(
    state: str, link: str = "https://example/x", camera_relevant: bool = True
) -> MaintenanceWindow:
    """Build a MaintenanceWindow that classifies as `state` at frozen 'now'.

    Picks start/end relative to a fixed reference instant so the same
    `state()` evaluation lands in the expected bucket regardless of wall
    clock.
    """
    ref = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
    pub = ref - timedelta(hours=12)
    if state == "active":
        start, end = ref - timedelta(hours=1), ref + timedelta(hours=2)
    elif state == "scheduled":
        start, end = ref + timedelta(hours=3), ref + timedelta(hours=5)
    elif state == "past":
        start, end = ref - timedelta(hours=5), ref - timedelta(hours=3)
    elif state == "recent":
        start, end = None, None
        pub = ref - timedelta(hours=2)  # within recent window
    else:  # unknown
        start, end = None, None
        pub = ref - timedelta(days=60)
    return MaintenanceWindow(
        title="Wartung Kamera-Infrastruktur",
        link=link,
        pub_date=pub,
        summary="Window between 07:00 and 10:00 MESZ",
        scheduled_start=start,
        scheduled_end=end,
        source="rss:Wartungsarbeiten",
        camera_relevant=camera_relevant,
    )


def _make_announce_coord(notify_service: str = "thomas") -> BoschCameraCoordinator:
    """Stub coordinator carrying only what `_async_maybe_announce_maintenance` reads."""
    coord = SimpleNamespace()
    coord.options = {"alert_notify_service": notify_service}
    coord.maintenance_notified_key = None
    coord.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    return cast(BoschCameraCoordinator, coord)


@pytest.mark.asyncio
class TestMaintenanceAnnounce:
    async def _state_fixed_to(self, coord: SimpleNamespace, state: str) -> None:
        """Patch `MaintenanceWindow.state` so the test does not race wall clock."""
        # We just rely on _mw_for_announce building windows that classify
        # naturally — but state() does evaluate against utcnow(). Freeze via
        # monkeypatch in tests that care (only "recent"/"unknown" depend on
        # now; "active" uses a fixed +/-1h window around 2026-05-19 which may
        # have already passed in CI). Use freezegun via pytest-freezer
        # (already in deps).
        pass

    async def test_announces_on_scheduled(self, freezer: FrozenDateTimeFactory):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        mw = _mw_for_announce("scheduled")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_called_once()
        args = coord.hass.services.async_call.await_args
        assert args.args[0] == "notify"
        assert args.args[1] == "thomas"
        assert "geplant" in args.args[2]["title"].lower()
        assert "Wartung" in args.args[2]["message"]
        assert coord.maintenance_notified_key == (mw.link, "scheduled")

    async def test_announces_again_on_scheduled_to_active(
        self, freezer: FrozenDateTimeFactory
    ):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        sched = _mw_for_announce("scheduled")
        active = _mw_for_announce("active", link=sched.link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, sched)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, active)
        assert coord.hass.services.async_call.await_count == 2
        # Second call carries the active wording.
        second = coord.hass.services.async_call.await_args_list[1]
        assert "läuft" in second.args[2]["title"].lower()
        assert coord.maintenance_notified_key == (active.link, "active")

    async def test_dedupes_duplicate_calls(self, freezer: FrozenDateTimeFactory):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        mw = _mw_for_announce("scheduled")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_called_once()

    @pytest.mark.parametrize("silent_state", ["past", "recent", "unknown"])
    async def test_silent_for_non_actionable_states(
        self, freezer: FrozenDateTimeFactory, silent_state: str
    ):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        mw = _mw_for_announce(silent_state)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_not_called()

    async def test_silent_when_not_camera_relevant(
        self, freezer: FrozenDateTimeFactory
    ):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        mw = _mw_for_announce("active", camera_relevant=False)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_not_called()

    async def test_no_service_configured_still_dedupes(
        self, freezer: FrozenDateTimeFactory
    ):
        """Without a notify service we record the key anyway so the user is
        not pestered the moment they later configure a service mid-window."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord(notify_service="")
        mw = _mw_for_announce("scheduled")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        coord.hass.services.async_call.assert_not_called()
        assert coord.maintenance_notified_key == (mw.link, "scheduled")

    async def test_notify_failure_is_swallowed(self, freezer: FrozenDateTimeFactory):
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        coord.hass.services.async_call = AsyncMock(
            side_effect=RuntimeError("service down")
        )
        mw = _mw_for_announce("active")
        # Must not raise — the maintenance fetch loop should not be brittle
        # to a misconfigured notify service.
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        # Key still gets recorded so we don't retry-storm on every coordinator tick.
        assert coord.maintenance_notified_key == (mw.link, "active")

    async def test_multiple_services_all_called(self, freezer: FrozenDateTimeFactory):
        """alert_notify_service can be a comma-separated list — every entry is called."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord(notify_service="thomas, signalhome")
        mw = _mw_for_announce("active")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, mw)
        assert coord.hass.services.async_call.await_count == 2
        called = {c.args[1] for c in coord.hass.services.async_call.await_args_list}
        assert called == {"thomas", "signalhome"}

    async def test_new_window_link_re_announces(self, freezer: FrozenDateTimeFactory):
        """A different announcement (new Bosch RSS item, different link)
        should re-announce even if the previous one was already 'scheduled'."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        first = _mw_for_announce("scheduled", link="https://example/a")
        second = _mw_for_announce("scheduled", link="https://example/b")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, first)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, second)
        assert coord.hass.services.async_call.await_count == 2

    async def test_active_to_past_announces_ended(self, freezer: FrozenDateTimeFactory):
        """active → past transition for the same window fires one final
        'beendet' notification so users know the cloud should be back."""
        freezer.move_to("2026-05-19T07:30:00+00:00")
        coord = _make_announce_coord()
        active = _mw_for_announce("active")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, active)
        # Now jump past the window end (was ref + 2h)
        freezer.move_to("2026-05-19T10:00:00+00:00")
        past = _mw_for_announce("past", link=active.link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, past)
        assert coord.hass.services.async_call.await_count == 2
        second = coord.hass.services.async_call.await_args_list[1]
        assert "beendet" in second.args[2]["title"].lower()
        assert coord.maintenance_notified_key == (past.link, "past")

    async def test_stale_past_window_does_not_announce(
        self, freezer: FrozenDateTimeFactory
    ):
        """A 'past' announcement discovered without a prior 'active' phase
        (e.g. integration restart after the window already closed) must
        stay silent — otherwise users get spammed about historical
        maintenance every time HA reboots."""
        freezer.move_to("2026-05-19T10:00:00+00:00")
        coord = _make_announce_coord()
        past = _mw_for_announce("past")
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, past)
        coord.hass.services.async_call.assert_not_called()
        # Dedupe key is still set so a follow-up tick stays silent too.
        assert coord.maintenance_notified_key == (past.link, "past")

    async def test_full_scheduled_active_past_lifecycle(
        self, freezer: FrozenDateTimeFactory
    ):
        """End-to-end: scheduled → active → past for the same window
        triggers exactly three notifications in the right order."""
        freezer.move_to("2026-05-19T03:00:00+00:00")
        coord = _make_announce_coord()
        link = "https://example/abc"
        # Phase 1: scheduled (now is before window start)
        sched = _mw_for_announce("scheduled", link=link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, sched)
        # Phase 2: active (jump into window)
        freezer.move_to("2026-05-19T07:30:00+00:00")
        active = _mw_for_announce("active", link=link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, active)
        # Phase 3: past (jump past end)
        freezer.move_to("2026-05-19T10:00:00+00:00")
        past = _mw_for_announce("past", link=link)
        await BoschCameraCoordinator._async_maybe_announce_maintenance(coord, past)
        assert coord.hass.services.async_call.await_count == 3
        titles = [
            c.args[2]["title"].lower()
            for c in coord.hass.services.async_call.await_args_list
        ]
        assert "geplant" in titles[0]
        assert "läuft" in titles[1]
        assert "beendet" in titles[2]


# BoschCameraCoordinator._async_refresh_maintenance
#
# Periodic + reactive refresh helper that hits the Bosch community RSS feed
# in the background. The cooldown logic and the exception-swallow path are
# not exercised by the tests above because they go through the public RSS
# fetcher directly.


def _mw_for_refresh() -> MaintenanceWindow:
    ref = datetime(2026, 5, 19, 7, 30, tzinfo=UTC)
    return MaintenanceWindow(
        title="Wartung Kamera-Infrastruktur",
        link="https://example/x",
        pub_date=ref - timedelta(hours=12),
        summary="07:00–10:00 MESZ",
        scheduled_start=ref - timedelta(hours=1),
        scheduled_end=ref + timedelta(hours=2),
        source="rss:Wartungsarbeiten",
        camera_relevant=True,
    )


def _make_refresh_coord(
    *,
    last_fetch: float = float("-inf"),
    cooldown: float = 300.0,
    cache: MaintenanceWindow | None = None,
) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.maintenance_last_fetch = last_fetch
    coord._MAINTENANCE_REACTIVE_COOLDOWN_S = cooldown
    coord.maintenance_cache = cache
    coord.hass = SimpleNamespace(data={})
    # Stub out announce side-effect so the test only exercises the refresh path.
    coord._async_maybe_announce_maintenance = AsyncMock(return_value=None)
    return coord


@pytest.mark.asyncio
class TestAsyncRefreshMaintenance:
    async def test_periodic_fetch_updates_cache(self):
        coord = _make_refresh_coord()
        new_mw = _mw_for_refresh()
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(return_value=new_mw),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            await BoschCameraCoordinator._async_refresh_maintenance(
                coord, reactive=False
            )
        assert coord.maintenance_cache is new_mw
        assert coord.maintenance_last_fetch == 1000.0
        coord._async_maybe_announce_maintenance.assert_awaited_once_with(new_mw)

    async def test_reactive_within_cooldown_is_noop(self):
        coord = _make_refresh_coord(last_fetch=950.0, cooldown=300.0)
        fetch_mock = AsyncMock(return_value=_mw_for_refresh())
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=fetch_mock,
            ),
        ):
            await BoschCameraCoordinator._async_refresh_maintenance(
                coord, reactive=True
            )
        fetch_mock.assert_not_awaited()
        # Cache untouched, last_fetch untouched (we returned before stamping).
        assert coord.maintenance_cache is None
        assert coord.maintenance_last_fetch == 950.0

    async def test_reactive_outside_cooldown_runs(self):
        coord = _make_refresh_coord(last_fetch=500.0, cooldown=300.0)
        new_mw = _mw_for_refresh()
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(return_value=new_mw),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            await BoschCameraCoordinator._async_refresh_maintenance(
                coord, reactive=True
            )
        assert coord.maintenance_cache is new_mw

    async def test_periodic_ignores_cooldown(self):
        """Cooldown gate only applies to reactive calls — periodic ticks
        always fetch when scheduled."""
        coord = _make_refresh_coord(last_fetch=950.0, cooldown=300.0)
        new_mw = _mw_for_refresh()
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(return_value=new_mw),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            await BoschCameraCoordinator._async_refresh_maintenance(
                coord, reactive=False
            )
        assert coord.maintenance_cache is new_mw

    async def test_fetch_exception_keeps_previous_cache(self):
        previous = _mw_for_refresh()
        coord = _make_refresh_coord(cache=previous)
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(side_effect=RuntimeError("network broken")),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            # Must not raise.
            await BoschCameraCoordinator._async_refresh_maintenance(
                coord, reactive=False
            )
        # Cache unchanged — sensor stays stable across community-site outage.
        assert coord.maintenance_cache is previous
        coord._async_maybe_announce_maintenance.assert_not_awaited()

    async def test_fetch_returns_none_keeps_previous_cache(self):
        previous = _mw_for_refresh()
        coord = _make_refresh_coord(cache=previous)
        with (
            patch(
                "custom_components.bosch_shc_camera.time.monotonic", return_value=1000.0
            ),
            patch(
                "custom_components.bosch_shc_camera.maintenance.async_fetch_maintenance",
                new=AsyncMock(return_value=None),
            ),
            patch(
                "custom_components.bosch_shc_camera.async_get_clientsession",
                return_value=object(),
            ),
        ):
            await BoschCameraCoordinator._async_refresh_maintenance(
                coord, reactive=False
            )
        assert coord.maintenance_cache is previous
        coord._async_maybe_announce_maintenance.assert_not_awaited()


# Section: RSS parser edge cases (relocated from
# tests/test_misc_small_gaps.py)


class TestMaintenanceParserEdges:
    def test_invalid_date_returns_none_pair(self):
        """`_parse_window` swallows a ValueError from invalid date components
        (e.g. 30. Februar) and returns (None, None)."""
        from custom_components.bosch_shc_camera.maintenance import _parse_window

        # 30. Februar is unparseable — the datetime constructor raises.
        text = "Wartung am 30.02.2026 von 07:00 bis 10:00 Uhr MESZ"
        pub = datetime(2026, 2, 28, tzinfo=UTC)
        start, end = _parse_window(text, pub)
        assert start is None and end is None

    @pytest.mark.asyncio
    async def test_empty_title_entry_is_skipped(self):
        """RSS items without a title must be skipped instead of crashing on
        the empty string."""
        from custom_components.bosch_shc_camera import maintenance

        rss = b"""<?xml version="1.0"?>
        <rss><channel>
          <item>
            <title></title>
            <link>https://example/empty</link>
            <pubDate>Tue, 19 May 2026 06:00:00 +0000</pubDate>
            <description>placeholder</description>
          </item>
          <item>
            <title>Wartung Kamera-Cloud 19.05.2026 07:00-10:00 MESZ</title>
            <link>https://example/good</link>
            <pubDate>Tue, 19 May 2026 06:00:00 +0000</pubDate>
            <description>Wartung der Kamera-Cloud</description>
          </item>
        </channel></rss>"""

        class _FakeResp:
            status = 200

            async def read(self):
                return rss

            async def text(self):
                return rss.decode()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _FakeSession:
            def get(self, _url, **_kw):
                return _FakeResp()

        result = await maintenance.async_fetch_maintenance(_FakeSession())
        assert result is not None
        assert "Wartung" in result.title
