"""Tests for fcm.py — push notification helpers (Round 5).

`fcm.py` is at 16% coverage with most of the deep Firebase integration
unreachable in unit tests (would need a real firebase_messaging mock).
This file covers the pure helpers + the small wrappers that don't
touch Firebase:

  - `get_alert_services` — comma-split with per-type fallback to
    alert_notify_service (system/information fall back; screenshot/video
    do NOT — they're opt-in).
  - `build_notify_data` — service-specific attachment formatting
    (mobile_app uses /local/ URL, telegram uses photo, others use
    data.attachments).
  - `_write_file` — trivial executor-bound file write.
  - `register_fcm_with_bosch` — POST /v11/devices wrapper.
  - `async_stop_fcm_push` — client teardown wrapper.
  - `_async_persist_fcm_creds` — config entry update wrapper.
  - `_on_fcm_push` — push callback router (gating + scheduling).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from threading import RLock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


def _stub_coord(**overrides):
    base = dict(
        options={},
        token="tok-A",
        _fcm_token="fcm-token-xyz",
        _fcm_push_mode="ios",
        _fcm_lock=RLock(),
        _fcm_running=False,
        _fcm_healthy=False,
        _fcm_client=None,
        _fcm_last_push=0.0,
        _entry=SimpleNamespace(data={}),
        data={},
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=MagicMock()),
            loop=SimpleNamespace(call_soon_threadsafe=MagicMock()),
            async_create_task=MagicMock(),
        ),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── get_alert_services ───────────────────────────────────────────────────


class TestGetAlertServices:
    def test_per_type_value_returned(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = _stub_coord(options={
            "alert_notify_information": "notify.test_user, notify.signal",
        })
        out = get_alert_services(coord, "information")
        assert out == ["notify.test_user", "notify.signal"]

    def test_falls_back_to_alert_notify_service_for_system(self):
        """`system` and `information` fall back to `alert_notify_service`
        when their per-type field is empty. Pin so a refactor can't drop
        the fallback (would silently disable system alerts)."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = _stub_coord(options={
            "alert_notify_service": "notify.fallback",
            "alert_notify_system": "",
        })
        out = get_alert_services(coord, "system")
        assert out == ["notify.fallback"]

    def test_screenshot_does_not_fall_back(self):
        """`screenshot` and `video` are opt-in — empty means skip that
        step entirely. Pin so they never silently inherit the
        alert_notify_service value."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = _stub_coord(options={
            "alert_notify_service": "notify.fallback",
            "alert_notify_screenshot": "",
        })
        out = get_alert_services(coord, "screenshot")
        assert out == []

    def test_video_does_not_fall_back(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = _stub_coord(options={
            "alert_notify_service": "notify.fallback",
            "alert_notify_video": "",
        })
        out = get_alert_services(coord, "video")
        assert out == []

    def test_strips_whitespace_around_entries(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = _stub_coord(options={
            "alert_notify_information": "  notify.a  ,  notify.b , ",
        })
        out = get_alert_services(coord, "information")
        assert out == ["notify.a", "notify.b"]

    def test_empty_strings_filtered(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services
        coord = _stub_coord(options={
            "alert_notify_information": ",,notify.real,,",
        })
        out = get_alert_services(coord, "information")
        assert out == ["notify.real"]


# ── build_notify_data ────────────────────────────────────────────────────


class TestBuildNotifyData:
    def test_message_only_no_attachment(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        out = build_notify_data("notify.test_user", "Hi")
        assert out == {"message": "Hi"}

    def test_title_added_when_present(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        out = build_notify_data("notify.test_user", "Body", title="Title")
        assert out["title"] == "Title"
        assert out["message"] == "Body"

    def test_mobile_app_uses_local_url(self):
        """HA Companion App reads images from /local/ URL — files served
        from /config/www/bosch_alerts/. Must NOT use file path."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        out = build_notify_data(
            "notify.mobile_app_iphone", "Bewegung",
            file_path="/config/www/bosch_alerts/snap.jpg",
        )
        assert out["data"]["image"] == "/local/bosch_alerts/snap.jpg"
        # iOS sound config
        assert out["data"]["push"]["sound"] == "default"

    def test_telegram_uses_photo_field(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        out = build_notify_data(
            "notify.telegram_bot", "Audio-Alarm",
            file_path="/path/to/clip.mp4",
        )
        assert out["data"]["photo"] == "/path/to/clip.mp4"
        assert out["data"]["caption"] == "Audio-Alarm"

    def test_signal_uses_attachments(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        out = build_notify_data(
            "notify.signal_thomas", "Snapshot",
            file_path="/tmp/snap.jpg",
        )
        assert out["data"]["attachments"] == ["/tmp/snap.jpg"]

    def test_email_uses_attachments(self):
        """Generic notify provider (email, etc.) → attachments path."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        out = build_notify_data(
            "notify.email_admin", "Alert",
            file_path="/tmp/alert.jpg",
        )
        assert out["data"]["attachments"] == ["/tmp/alert.jpg"]

    def test_mobile_app_extracts_basename(self):
        """The image URL uses /local/bosch_alerts/{basename} — the file
        path's directory is stripped. Pin so HA can find the file."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data
        out = build_notify_data(
            "notify.mobile_app_xy", "x",
            file_path="/some/deep/dir/event_2026-05-04.jpg",
        )
        assert out["data"]["image"] == "/local/bosch_alerts/event_2026-05-04.jpg"


# ── _write_file ──────────────────────────────────────────────────────────


class TestWriteFile:
    def test_writes_bytes_to_file(self, tmp_path):
        from custom_components.bosch_shc_camera.fcm import _write_file
        target = tmp_path / "snap.jpg"
        _write_file(str(target), b"\xff\xd8DATA\xff\xd9")
        assert target.read_bytes() == b"\xff\xd8DATA\xff\xd9"

    def test_overwrites_existing(self, tmp_path):
        from custom_components.bosch_shc_camera.fcm import _write_file
        target = tmp_path / "snap.jpg"
        target.write_bytes(b"OLD")
        _write_file(str(target), b"NEW")
        assert target.read_bytes() == b"NEW"


# ── register_fcm_with_bosch ──────────────────────────────────────────────


class TestRegisterFcmWithBosch:
    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch
        coord = _stub_coord(token="")
        ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_no_fcm_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch
        coord = _stub_coord(_fcm_token="")
        ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_clientsession",
            return_value=session,
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True

    @pytest.mark.asyncio
    async def test_500_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 500
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_clientsession",
            return_value=session,
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch
        session = MagicMock()
        session.post = MagicMock(side_effect=asyncio.TimeoutError())
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_clientsession",
            return_value=session,
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_device_type_picks_ios(self):
        """`_fcm_push_mode == 'ios'` → deviceType=IOS in the body."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch
        captured = {}

        @asynccontextmanager
        async def _post(*args, **kw):
            captured["json"] = kw.get("json", {})
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(_fcm_push_mode="ios")
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_clientsession",
            return_value=session,
        ):
            await register_fcm_with_bosch(coord)
        assert captured["json"]["deviceType"] == "IOS"

    @pytest.mark.asyncio
    async def test_device_type_picks_android_for_other(self):
        """Anything other than `ios` → ANDROID."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch
        captured = {}

        @asynccontextmanager
        async def _post(*args, **kw):
            captured["json"] = kw.get("json", {})
            r = MagicMock()
            r.status = 201
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(_fcm_push_mode="android")
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_clientsession",
            return_value=session,
        ):
            await register_fcm_with_bosch(coord)
        assert captured["json"]["deviceType"] == "ANDROID"


# ── async_stop_fcm_push ──────────────────────────────────────────────────


class TestAsyncStopFcmPush:
    @pytest.mark.asyncio
    async def test_no_client_no_op(self):
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push
        coord = _stub_coord(_fcm_client=None, _fcm_running=False)
        # Must NOT raise
        await async_stop_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_stops_running_client_and_clears_state(self):
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push
        client = MagicMock()
        client.stop = AsyncMock()
        coord = _stub_coord(_fcm_client=client, _fcm_running=True, _fcm_healthy=True)
        await async_stop_fcm_push(coord)
        client.stop.assert_awaited_once()
        # All state cleared
        assert coord._fcm_running is False
        assert coord._fcm_healthy is False
        assert coord._fcm_client is None
        assert coord._fcm_push_mode == "unknown"

    @pytest.mark.asyncio
    async def test_client_stop_exception_swallowed(self):
        """Library may throw on stop (idempotency, race) — must not
        propagate. State must still be cleared."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push
        client = MagicMock()
        client.stop = AsyncMock(side_effect=RuntimeError("library bug"))
        coord = _stub_coord(_fcm_client=client, _fcm_running=True)
        await async_stop_fcm_push(coord)
        assert coord._fcm_client is None
        assert coord._fcm_running is False

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        """asyncio.CancelledError must NOT be swallowed (HA shutdown)."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push
        client = MagicMock()
        client.stop = AsyncMock(side_effect=asyncio.CancelledError())
        coord = _stub_coord(_fcm_client=client, _fcm_running=True)
        with pytest.raises(asyncio.CancelledError):
            await async_stop_fcm_push(coord)


# ── _async_persist_fcm_creds ─────────────────────────────────────────────


class TestAsyncPersistFcmCreds:
    @pytest.mark.asyncio
    async def test_writes_creds_to_entry_data(self):
        from custom_components.bosch_shc_camera.fcm import _async_persist_fcm_creds
        coord = _stub_coord()
        coord._entry = SimpleNamespace(data={"existing": "value"})
        creds = {"refresh_token": "rfr", "android_id": 12345}
        await _async_persist_fcm_creds(coord, creds)
        coord.hass.config_entries.async_update_entry.assert_called_once()
        call = coord.hass.config_entries.async_update_entry.call_args
        new_data = call.kwargs["data"]
        # Existing fields preserved + new fcm_credentials key
        assert new_data["existing"] == "value"
        assert new_data["fcm_credentials"] == creds

    @pytest.mark.asyncio
    async def test_swallows_exception(self):
        """async_update_entry might fire during HA shutdown — must not
        crash the FCM listener."""
        from custom_components.bosch_shc_camera.fcm import _async_persist_fcm_creds
        coord = _stub_coord()
        coord.hass.config_entries.async_update_entry = MagicMock(
            side_effect=RuntimeError("entry locked"),
        )
        # Must NOT raise
        await _async_persist_fcm_creds(coord, {"x": 1})


# ── _on_fcm_push callback ────────────────────────────────────────────────


class TestOnFcmPush:
    def test_running_false_drops_push(self):
        """A push that arrives after async_stop_fcm_push cleared the
        client must be dropped — otherwise it'd reschedule on a loop
        that already considers FCM down. Pin the gate."""
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push
        coord = _stub_coord(_fcm_running=False)
        _on_fcm_push(coord, {"from": "x"}, "push-id-1")
        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_running_true_schedules_handler(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push
        coord = _stub_coord(_fcm_running=True)
        _on_fcm_push(coord, {"from": "Bosch"}, "push-id-2")
        coord.hass.loop.call_soon_threadsafe.assert_called_once()

    def test_marks_fcm_healthy_and_stamps_last_push(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push
        coord = _stub_coord(_fcm_running=True, _fcm_healthy=False)
        before = coord._fcm_last_push
        _on_fcm_push(coord, {"from": "x"}, "push-id-3")
        assert coord._fcm_healthy is True
        assert coord._fcm_last_push > before
