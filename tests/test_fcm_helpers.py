"""Tests for fcm.py pure helpers (no Firebase listener / no aiohttp).

The high-leverage targets:
  - `_is_safe_bosch_url` — duplicate of the __init__ SSRF guard (different
    file, same contract — both must reject internal IPs / non-Bosch hosts)
  - `_FCMNoiseFilter` — strips recursive trace + dedupes the noisy
    firebase_messaging error record (CPU spike fix from v10.5.x)
  - `get_alert_services` — alert routing fallback rules (system/information
    fall back to default; screenshot/video do NOT)
  - `build_notify_data` — per-service attachment format (mobile_app vs
    telegram vs signal/generic)
"""

from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest


# ── _is_safe_bosch_url (fcm copy) ───────────────────────────────────────


class TestFcmSafeBoschUrl:
    @pytest.mark.parametrize("url", [
        "https://residential.cbs.boschsecurity.com/v11/devices",
        "https://api.bosch.com/x",
        "https://something.boschsecurity.com/y",
    ])
    def test_legit_urls_allowed(self, url):
        from custom_components.bosch_shc_camera.fcm import _is_safe_bosch_url
        assert _is_safe_bosch_url(url) is True

    @pytest.mark.parametrize("url", [
        "http://residential.cbs.boschsecurity.com/x",  # not HTTPS
        "https://attacker.com/x",
        "https://127.0.0.1/x",
        "https://10.0.0.1/x",
        "",
    ])
    def test_unsafe_urls_rejected(self, url):
        from custom_components.bosch_shc_camera.fcm import _is_safe_bosch_url
        assert _is_safe_bosch_url(url) is False


# ── _FCMNoiseFilter ─────────────────────────────────────────────────────


def _make_record(msg: str, *, with_exc: bool = False) -> logging.LogRecord:
    record = logging.LogRecord(
        name="firebase_messaging.fcmpushclient",
        level=logging.ERROR,
        pathname="x.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if with_exc:
        record.exc_info = (ValueError, ValueError("x"), None)
        record.exc_text = "Traceback (most recent call last):\n  File...\n" * 3000
    return record


class TestFCMNoiseFilter:
    def test_unrelated_messages_pass_through(self):
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        record = _make_record("FCM token registered successfully")
        assert f.filter(record) is True

    def test_first_offending_record_passes_with_exc_stripped(self):
        """First 'Unexpected exception during read' lets through, but
        without the recursive stack trace."""
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        record = _make_record("Unexpected exception during read", with_exc=True)
        assert f.filter(record) is True
        # The recursive trace must be stripped by now
        assert record.exc_info is None
        assert record.exc_text is None

    def test_second_record_within_60s_dropped(self):
        """De-dupe within 60 s window — second message gets filtered out."""
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        # First passes
        f.filter(_make_record("Unexpected exception during read"))
        # Second within window must be dropped
        rec2 = _make_record("Unexpected exception during read")
        assert f.filter(rec2) is False

    def test_record_after_60s_passes(self):
        """After the 60 s window, another message gets through."""
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter
        f = _FCMNoiseFilter()
        f.filter(_make_record("Unexpected exception during read"))
        # Backdate the last_passed timestamp so the next record passes
        f._last_passed = time.monotonic() - 70.0
        rec = _make_record("Unexpected exception during read")
        assert f.filter(rec) is True


# ── get_alert_services routing ──────────────────────────────────────────


class TestGetAlertServices:
    def test_specific_slot_used_when_set(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_information": "notify.foo",
            "alert_notify_service": "notify.fallback",
        })
        assert get_alert_services(coord, "information") == ["notify.foo"]

    def test_information_falls_back_to_default(self):
        """When the per-step slot is empty, fall back to alert_notify_service."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_information": "",
            "alert_notify_service": "notify.signalkamera",
        })
        assert get_alert_services(coord, "information") == ["notify.signalkamera"]

    def test_system_falls_back_to_default(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_system": "",
            "alert_notify_service": "notify.thomas",
        })
        assert get_alert_services(coord, "system") == ["notify.thomas"]

    def test_screenshot_does_not_fall_back(self):
        """Empty `alert_notify_screenshot` must NOT fall back — empty means skip step."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_screenshot": "",
            "alert_notify_service": "notify.thomas",
        })
        assert get_alert_services(coord, "screenshot") == []

    def test_video_does_not_fall_back(self):
        """Same skip-on-empty rule for video."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_video": "",
            "alert_notify_service": "notify.thomas",
        })
        assert get_alert_services(coord, "video") == []

    def test_comma_separated_services_split(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_information": "notify.a, notify.b , notify.c",
        })
        assert get_alert_services(coord, "information") == [
            "notify.a", "notify.b", "notify.c",
        ]

    def test_empty_strings_filtered_out(self):
        """Trailing comma or double comma → no empty entry in the result."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = SimpleNamespace(options={
            "alert_notify_information": "notify.a,, notify.b,",
        })
        assert get_alert_services(coord, "information") == ["notify.a", "notify.b"]


# ── build_notify_data ──────────────────────────────────────────────────


class TestBuildNotifyData:
    def test_text_only_no_attachment(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data("notify.foo", "Hello", title="Subject")
        assert data["message"] == "Hello"
        assert data["title"] == "Subject"
        assert "data" not in data

    def test_mobile_app_uses_local_image_url(self):
        """HA Companion App reads images from /local/bosch_alerts/ (auth-free)."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.mobile_app_iphone17",
            "msg",
            file_path="/config/www/bosch_alerts/snap_123.jpg",
        )
        assert data["data"]["image"] == "/local/bosch_alerts/snap_123.jpg"
        assert data["data"]["push"]["sound"] == "default"

    def test_telegram_uses_photo_field(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.telegram_chat", "msg", file_path="/config/x.jpg",
        )
        assert data["data"]["photo"] == "/config/x.jpg"
        assert data["data"]["caption"] == "msg"

    def test_signal_uses_attachments(self):
        """Signal / email / generic services use data.attachments list."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data(
            "notify.signal_messenger", "msg", file_path="/config/x.mp4",
        )
        assert data["data"]["attachments"] == ["/config/x.mp4"]

    def test_no_title_field_when_empty(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        data = build_notify_data("notify.x", "msg", title=None)
        assert "title" not in data
