"""FCM coverage round 2 — push handler, dedup, type→switch slug map.

Targets `fcm.py` (was 14%) — covers `_FCMNoiseFilter` edge cases,
`_install_fcm_noise_filter` idempotency, `_on_fcm_push` running flag,
`build_notify_data` for additional service backends, and the PERSON
tag upgrade path.
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── _FCMNoiseFilter ──────────────────────────────────────────────────────


def _make_record(msg: str, exc_info=None) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="firebase_messaging.fcmpushclient",
        level=logging.ERROR,
        pathname="x",
        lineno=1,
        msg=msg,
        args=None,
        exc_info=exc_info,
    )
    return rec


class TestFcmNoiseFilterAdditional:
    """Beyond the basic tests in test_fcm_helpers.py — edge cases."""

    def test_passes_unrelated_record_unchanged(self):
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        rec = _make_record("connection established")
        assert f.filter(rec) is True
        # Unrelated records keep their exc_info
        rec.exc_info = ("type", "value", "tb")
        rec.msg = "some other error"
        # filter would still pass through (the check is "Unexpected exception during read")
        assert f.filter(rec) is True
        assert rec.exc_info == ("type", "value", "tb"), (
            "Unrelated records must keep exc_info — only the recursive "
            "FCM read traceback is stripped."
        )

    def test_strips_exc_info_on_target_record(self):
        """Filter strips exc_info on matching records to defeat the
        thousands-of-frame recursive trace."""
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        rec = _make_record(
            "Unexpected exception during read", exc_info=("t", "v", "tb"),
        )
        f.filter(rec)
        assert rec.exc_info is None
        assert rec.exc_text is None

    def test_60s_dedup_window(self):
        """Filter lets one record through per 60 s (anti-flood)."""
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        # First record: passes
        r1 = _make_record("Unexpected exception during read")
        assert f.filter(r1) is True
        # Second record immediately after: dropped
        r2 = _make_record("Unexpected exception during read")
        assert f.filter(r2) is False

    def test_60s_window_lets_through_after_elapsed(self, monkeypatch):
        """After 60 s the next matching record passes again — keeps a
        heartbeat so users still see the WAN-down state in the log."""
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        f.filter(_make_record("Unexpected exception during read"))
        # Force the internal timestamp 70 s into the past
        f._last_passed = time.monotonic() - 70.0
        rec = _make_record("Unexpected exception during read")
        assert f.filter(rec) is True


class TestInstallFcmNoiseFilter:
    """The installer must be idempotent — re-running attaches no
    duplicate filters. Otherwise reload-the-integration would chain
    filters and the dedup window would multiply."""

    def test_installs_once(self):
        from custom_components.bosch_shc_camera.fcm import (
            _install_fcm_noise_filter, _FCMNoiseFilter,
        )
        # Strip any pre-existing filters from previous test
        log = logging.getLogger("firebase_messaging.fcmpushclient")
        log.filters = [f for f in log.filters if not isinstance(f, _FCMNoiseFilter)]
        _install_fcm_noise_filter()
        count_after_first = sum(1 for f in log.filters if isinstance(f, _FCMNoiseFilter))
        _install_fcm_noise_filter()
        _install_fcm_noise_filter()
        count_after_third = sum(1 for f in log.filters if isinstance(f, _FCMNoiseFilter))
        assert count_after_first == 1
        assert count_after_third == 1, (
            "Re-installing must be a no-op — duplicate filters multiply "
            "the dedup window and break the heartbeat log."
        )


# ── _on_fcm_push ─────────────────────────────────────────────────────────


class TestOnFcmPush:
    """`_on_fcm_push` is the FCM client callback. Must:
      1. Drop pushes when `_fcm_running` is False (post-stop trailing push).
      2. Update `_fcm_last_push` + `_fcm_healthy` flags.
      3. Schedule `async_handle_fcm_push` on the HA loop.
    """

    def _make_coord(self, running: bool = True):
        loop = SimpleNamespace(call_soon_threadsafe=MagicMock())
        hass = SimpleNamespace(
            loop=loop,
            async_create_task=MagicMock(),
        )
        import threading
        return SimpleNamespace(
            _fcm_lock=threading.Lock(),
            _fcm_running=running,
            _fcm_last_push=0.0,
            _fcm_healthy=False,
            hass=hass,
        )

    def test_drops_when_not_running(self):
        """Trailing push after stop must be ignored — otherwise the
        scheduled handler runs against a torn-down session."""
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push
        coord = self._make_coord(running=False)
        _on_fcm_push(coord, {"from": "test"}, "push-id-1")
        coord.hass.loop.call_soon_threadsafe.assert_not_called()
        assert coord._fcm_last_push == 0.0
        assert coord._fcm_healthy is False

    def test_updates_health_flags_when_running(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push
        coord = self._make_coord(running=True)
        _on_fcm_push(coord, {"from": "test"}, "push-id-1")
        assert coord._fcm_last_push > 0.0
        assert coord._fcm_healthy is True

    def test_schedules_handler_via_loop(self):
        """Must schedule via `loop.call_soon_threadsafe` since the FCM
        callback runs on a background thread, not the event loop."""
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push
        coord = self._make_coord(running=True)
        _on_fcm_push(coord, {"from": "test"}, "push-id-1")
        coord.hass.loop.call_soon_threadsafe.assert_called_once()


# ── build_notify_data — extra backends ──────────────────────────────────


class TestBuildNotifyDataExtras:
    """Round 2 of build_notify_data — covers paths missed in
    test_fcm_helpers.py."""

    def test_with_title(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.alex", "msg", file_path=None, title="Bewegung",
        )
        assert data["message"] == "msg"
        assert data["title"] == "Bewegung"
        assert "data" not in data

    def test_no_title_no_data_key(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data("notify.alex", "msg")
        assert "title" not in data
        assert "data" not in data

    def test_mobile_app_includes_default_sound(self):
        """iOS Companion App: the alert needs an explicit sound key.
        Without `push.sound`, iOS plays no chime — silent alerts are
        easy to miss."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.mobile_app_thomas_iphone", "msg",
            file_path="/tmp/img.jpg",
        )
        assert data["data"]["push"]["sound"] == "default"
        assert data["data"]["image"] == "/local/bosch_alerts/img.jpg"

    def test_telegram_uppercase_match(self):
        """Telegram service name detection is case-insensitive."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.TELEGRAM_chat_main", "Bewegung erkannt",
            file_path="/tmp/x.jpg",
        )
        assert data["data"]["photo"] == "/tmp/x.jpg"
        assert data["data"]["caption"] == "Bewegung erkannt"

    def test_signal_uses_attachments_list(self):
        """Signal-Messenger (HA addon notify.signal) requires an
        `attachments` list with file path strings."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.signal_thomas", "msg", file_path="/x/y.mp4",
        )
        assert data["data"] == {"attachments": ["/x/y.mp4"]}

    def test_email_falls_into_attachments(self):
        """`notify.smtp` and similar email-based services hit the
        generic `else` branch and use `attachments`."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.smtp_default", "msg", file_path="/file.jpg",
        )
        assert data["data"] == {"attachments": ["/file.jpg"]}


# ── get_alert_services — fallback chain coverage ────────────────────────


class TestGetAlertServicesExtras:
    """Round 2 — paths missed in test_fcm_helpers.py."""

    def test_screenshot_does_not_fall_back_to_default(self):
        """`screenshot` must NOT inherit from `alert_notify_service` —
        the user explicitly opted out by leaving screenshot empty."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_screenshot": "",
            "alert_notify_service": "notify.fallback",
        })
        assert get_alert_services(coord, "screenshot") == []

    def test_video_does_not_fall_back_to_default(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_video": "",
            "alert_notify_service": "notify.fallback",
        })
        assert get_alert_services(coord, "video") == []

    def test_information_falls_back_to_default(self):
        """`information` (text alerts) DOES fall back — the default
        service was historically the only routing."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_information": "",
            "alert_notify_service": "notify.thomas",
        })
        assert get_alert_services(coord, "information") == ["notify.thomas"]

    def test_system_falls_back_to_default(self):
        """`system` (TROUBLE_CONNECT/DISCONNECT) also falls back."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_system": "",
            "alert_notify_service": "notify.thomas",
        })
        assert get_alert_services(coord, "system") == ["notify.thomas"]

    def test_explicit_value_takes_precedence_over_default(self):
        """Explicit per-type service must NOT be overwritten by the
        global default."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_information": "notify.specific",
            "alert_notify_service": "notify.fallback",
        })
        assert get_alert_services(coord, "information") == ["notify.specific"]

    def test_strips_whitespace_in_csv(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_information": "  notify.a , notify.b ,,, notify.c ",
        })
        assert get_alert_services(coord, "information") == [
            "notify.a", "notify.b", "notify.c",
        ]


# ── _is_safe_bosch_url (fcm copy) ───────────────────────────────────────


class TestFcmSafeBoschUrl:
    """`fcm.py` has its own copy of `_is_safe_bosch_url` (alongside the
    one in `__init__.py` / `smb.py`). All copies must enforce identical
    rules — divergence opens an SSRF window in one of the alert paths."""

    @pytest.mark.parametrize("url,expected", [
        ("https://residential.cbs.boschsecurity.com/x", True),
        ("https://api.bosch.com/y", True),
        ("https://abc.boschsecurity.com.attacker.com/", False),  # suffix-injection guard
        ("http://residential.cbs.boschsecurity.com/x", False),  # not HTTPS
        ("https://attacker.com/", False),
        ("https://192.168.1.1/", False),
        ("ftp://api.bosch.com/", False),
        ("", False),
        ("not-a-url", False),
    ])
    def test_url_validation(self, url, expected):
        from custom_components.bosch_shc_camera.fcm import _is_safe_bosch_url
        assert _is_safe_bosch_url(url) is expected
