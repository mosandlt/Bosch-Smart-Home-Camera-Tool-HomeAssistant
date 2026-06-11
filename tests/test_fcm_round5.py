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
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_A = "11111111-1111-1111-1111-111111111111"


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
        _fcm_last_push=float("-inf"),
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

        coord = _stub_coord(
            options={
                "alert_notify_information": "notify.test_user, notify.signal",
            }
        )
        out = get_alert_services(coord, "information")
        assert out == ["notify.test_user", "notify.signal"]

    def test_falls_back_to_alert_notify_service_for_system(self):
        """`system` and `information` fall back to `alert_notify_service`
        when their per-type field is empty. Pin so a refactor can't drop
        the fallback (would silently disable system alerts)."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_service": "notify.fallback",
                "alert_notify_system": "",
            }
        )
        out = get_alert_services(coord, "system")
        assert out == ["notify.fallback"]

    def test_screenshot_does_not_fall_back(self):
        """`screenshot` and `video` are opt-in — empty means skip that
        step entirely. Pin so they never silently inherit the
        alert_notify_service value."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_service": "notify.fallback",
                "alert_notify_screenshot": "",
            }
        )
        out = get_alert_services(coord, "screenshot")
        assert out == []

    def test_video_does_not_fall_back(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_service": "notify.fallback",
                "alert_notify_video": "",
            }
        )
        out = get_alert_services(coord, "video")
        assert out == []

    def test_strips_whitespace_around_entries(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_information": "  notify.a  ,  notify.b , ",
            }
        )
        out = get_alert_services(coord, "information")
        assert out == ["notify.a", "notify.b"]

    def test_empty_strings_filtered(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_information": ",,notify.real,,",
            }
        )
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
            "notify.mobile_app_iphone",
            "Bewegung",
            file_path="/config/www/bosch_alerts/snap.jpg",
        )
        assert out["data"]["image"] == "/local/bosch_alerts/snap.jpg"
        # iOS sound config
        assert out["data"]["push"]["sound"] == "default"

    def test_telegram_uses_photo_field(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.telegram_bot",
            "Audio-Alarm",
            file_path="/path/to/clip.mp4",
        )
        assert out["data"]["photo"] == "/path/to/clip.mp4"
        assert out["data"]["caption"] == "Audio-Alarm"

    def test_signal_uses_attachments(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.signal_thomas",
            "Snapshot",
            file_path="/tmp/snap.jpg",
        )
        assert out["data"]["attachments"] == ["/tmp/snap.jpg"]

    def test_email_uses_attachments(self):
        """Generic notify provider (email, etc.) → attachments path."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.email_admin",
            "Alert",
            file_path="/tmp/alert.jpg",
        )
        assert out["data"]["attachments"] == ["/tmp/alert.jpg"]

    def test_mobile_app_extracts_basename(self):
        """The image URL uses /local/bosch_alerts/{basename} — the file
        path's directory is stripped. Pin so HA can find the file."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.mobile_app_xy",
            "x",
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
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True

    @pytest.mark.asyncio
    async def test_500_unknown_error_returns_false(self):
        """HTTP 500 with an unrecognised error body must return False and log a warning."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 500
            r.text = AsyncMock(return_value='{"status":500,"error":"sh:unknown.error"}')
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_500_logs_response_body(self, caplog):
        """HTTP 500 with unknown error must include the body in the WARNING log."""
        import logging

        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        bosch_body = (
            '{"status":500,"error":"sh:unknown.error","message":"Something else."}'
        )

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 500
            r.text = AsyncMock(return_value=bosch_body)
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with caplog.at_level(
                logging.WARNING, logger="custom_components.bosch_shc_camera.fcm"
            ):
                ok = await register_fcm_with_bosch(coord)

        assert ok is False
        assert "sh:unknown.error" in caplog.text, (
            "Warning log must contain Bosch error body so operators can diagnose "
            "unexpected FCM registration failures."
        )

    @pytest.mark.asyncio
    async def test_500_already_registered_treated_as_success(self):
        """HTTP 500 with sh:internal.error means the token is already registered.

        Bosch returns this on every re-registration of an existing token.
        FCM push still works — treat as success and save the token so the
        next restart skips the POST entirely (avoids persistent 500 spam).
        """
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        already_registered_body = '{"status":500,"error":"sh:internal.error","message":"Internal Server Error."}'

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 500
            r.text = AsyncMock(return_value=already_registered_body)
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(_entry=SimpleNamespace(data={}))
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)

        assert ok is True, (
            "sh:internal.error means the token is already registered — must return True "
            "so the coordinator treats FCM as functional"
        )
        update_call = coord.hass.config_entries.async_update_entry
        update_call.assert_called_once()
        saved_data = update_call.call_args.kwargs.get("data") or update_call.call_args[
            1
        ].get("data", {})
        assert saved_data.get("fcm_registered_token") == "fcm-token-xyz", (
            "Token must be saved even on 500 sh:internal.error so the next restart "
            "skips the registration POST entirely"
        )

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        session = MagicMock()
        session.post = MagicMock(side_effect=TimeoutError())
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is False

    # test_device_type_picks_ios removed in v12.4.5: deviceType is now always
    # ANDROID — the OSS-sanctioned key handles both platforms transparently.

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
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await register_fcm_with_bosch(coord)
        assert captured["json"]["deviceType"] == "ANDROID"

    @pytest.mark.asyncio
    async def test_same_token_in_entry_skips_post(self):
        """When fcm_registered_token matches AND fcm_registered_device_type==ANDROID, skip POST.

        The skip-check now requires BOTH conditions (Fix C++ deviceType-drift heal):
        token match alone is insufficient — a missing/wrong deviceType marker means
        Bosch CBS may have the stale deviceType=IOS registration and must be healed.

        Regression: every HA restart triggered a redundant POST → Bosch returned
        HTTP 500 ("sh:internal.error") because the token was already registered.
        The updated guard preserves this fast-path for the steady-state case where
        both the token and the ANDROID marker are present.
        """
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        post_called = []

        @asynccontextmanager
        async def _post(*args, **kw):
            post_called.append(True)
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(
            _entry=SimpleNamespace(
                data={
                    "fcm_registered_token": "fcm-token-xyz",
                    "fcm_registered_device_type": "ANDROID",  # both conditions must hold
                }
            ),
        )
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True, "already-registered token must return True"
        assert not post_called, (
            "POST must be skipped when token is unchanged AND deviceType=ANDROID"
        )

    @pytest.mark.asyncio
    async def test_new_token_triggers_post(self):
        """When no saved token exists (first run), the POST fires normally."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        post_called = []

        @asynccontextmanager
        async def _post(*args, **kw):
            post_called.append(True)
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(_entry=SimpleNamespace(data={}))
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True
        assert post_called, "POST must fire when no saved token exists"

    @pytest.mark.asyncio
    async def test_success_saves_registered_token(self):
        """On HTTP 204, save fcm_registered_token to the config entry for future skip."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(_entry=SimpleNamespace(data={}))
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True
        update_call = coord.hass.config_entries.async_update_entry
        update_call.assert_called_once()
        saved_data = update_call.call_args.kwargs.get("data") or update_call.call_args[
            1
        ].get("data", {})
        assert saved_data.get("fcm_registered_token") == "fcm-token-xyz", (
            "Registered token must be saved to config entry so subsequent "
            "restarts can skip the Bosch POST and avoid HTTP 500"
        )


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

    @pytest.mark.asyncio
    async def test_awaits_pending_tasks_after_stop(self):
        """Regression: ``client.stop()`` only cancels the read loop — it does
        not await the ``finally: await self._do_writer_close()`` SSL shutdown.
        If we recreate the client before that finishes, the old loop logs
        ``ERROR firebase_messaging.fcmpushclient: Unexpected exception during
        read`` once per ~63 s (state machine sees the SSL close outside of
        RESETTING). Fix awaits ``client.tasks`` here so the new instance never
        races the old one's SSL teardown.

        User/forum source: live-log session 2026-05-16 — 44 errors in 45 min
        after toggling fcm_push_mode 'polling' -> 'auto' via the select entity.
        Library upstream issue: github.com/sdb9696/firebase-messaging #33.
        """
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock()
        client.stop = AsyncMock()
        ssl_close_done = asyncio.Event()

        async def slow_ssl_close() -> None:
            await asyncio.sleep(0.05)
            ssl_close_done.set()

        client.tasks = [asyncio.create_task(slow_ssl_close())]
        coord = _stub_coord(_fcm_client=client, _fcm_running=True)
        await async_stop_fcm_push(coord)
        assert ssl_close_done.is_set(), "stop must await pending SSL-close tasks"
        assert coord._fcm_client is None

    @pytest.mark.asyncio
    async def test_pending_tasks_timeout_does_not_block_forever(self):
        """If a library task hangs (e.g. SSL shutdown deadlock), stop must
        still return within ~10 s so the user-facing UI toggle doesn't freeze.
        State is cleared even when the gather times out."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock()
        client.stop = AsyncMock()

        async def never_finishes() -> None:
            await asyncio.sleep(3600)

        hung = asyncio.create_task(never_finishes())
        client.tasks = [hung]
        coord = _stub_coord(_fcm_client=client, _fcm_running=True)

        with patch(
            "custom_components.bosch_shc_camera.fcm.asyncio.wait_for",
            AsyncMock(side_effect=TimeoutError()),
        ):
            await async_stop_fcm_push(coord)
        # State cleared regardless of timeout
        assert coord._fcm_client is None
        assert coord._fcm_running is False
        hung.cancel()

    @pytest.mark.asyncio
    async def test_no_tasks_attr_backcompat(self):
        """Older firebase-messaging versions may not expose ``client.tasks``.
        Stop must work via getattr() default and not raise AttributeError."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock(spec=["stop"])  # no `tasks` attribute
        client.stop = AsyncMock()
        coord = _stub_coord(_fcm_client=client, _fcm_running=True)
        await async_stop_fcm_push(coord)
        assert coord._fcm_client is None


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


# ── _QuietFcmPushClient / _get_fcm_push_client_class (issue #33 fix) ─────────


class TestQuietFcmPushClient:
    """Regression tests for the upstream state-machine fix (firebase-messaging#33).

    Root cause: FcmPushClient._listen() logs _logger.exception("Unexpected exception
    during read") BEFORE calling _reset(), which is where run_state is set to RESETTING.
    The existing quiet-path check (run_state == RESETTING) therefore always misses the
    very first error → one ERROR + traceback per ~63 s reconnect cycle.

    Fix: _QuietFcmPushClient._patch_class() returns a subclass whose _listen() override
    sets run_state = RESETTING immediately on catching the OSError, so the existing check
    routes to the INFO-level path instead.

    User/forum source: live-log session 2026-05-16 — issue sdb9696/firebase-messaging#33.
    """

    def test_patch_class_returns_subclass_of_fcm_push_client(self):
        """_patch_class() must return a type that is a subclass of FcmPushClient."""
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient

        from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

        cls = _QuietFcmPushClient._patch_class()
        assert cls is not None, (
            "patch must succeed when firebase_messaging is importable"
        )
        assert issubclass(cls, FcmPushClient), (
            "patched class must be a subclass of FcmPushClient"
        )

    def test_patched_class_has_listen_override(self):
        """The patched subclass must define its own _listen so it's not the vanilla one."""
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient

        from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

        cls = _QuietFcmPushClient._patch_class()
        assert cls is not None
        # _listen defined directly on cls (not inherited from FcmPushClient)
        assert "_listen" in cls.__dict__, (
            "patched class must override _listen directly — "
            "otherwise the fix is silently absent"
        )
        # And it must differ from the base
        assert cls.__dict__["_listen"] is not FcmPushClient._listen, (  # type: ignore[attr-defined]
            "override must not be the same function object as the base"
        )

    def test_get_fcm_push_client_class_returns_nonnone(self):
        """_get_fcm_push_client_class() returns a usable class when the library is present."""
        pytest.importorskip("firebase_messaging")
        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
            _QuietFcmPushClient,
        )

        # Reset cache so we get a fresh computation
        _QuietFcmPushClient._patched_class = False
        cls = _get_fcm_push_client_class()
        assert cls is not None

    def test_get_fcm_push_client_class_caches_result(self):
        """Second call must return the same object (no double-computation)."""
        pytest.importorskip("firebase_messaging")
        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
            _QuietFcmPushClient,
        )

        _QuietFcmPushClient._patched_class = False
        first = _get_fcm_push_client_class()
        second = _get_fcm_push_client_class()
        assert first is second, "class must be cached after first computation"

    def test_fallback_to_vanilla_when_patch_fails(self):
        """If _patch_class() returns None (e.g. signature changed), _get_fcm_push_client_class
        must fall back to vanilla FcmPushClient rather than returning None."""
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient

        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
            _QuietFcmPushClient,
        )

        # Force the cache to None (simulates patch_class() returning None)
        _QuietFcmPushClient._patched_class = None
        cls = _get_fcm_push_client_class()
        assert cls is FcmPushClient, (
            "when patch fails, must fall back to vanilla FcmPushClient — "
            "returning None would crash async_start_fcm_push"
        )
        # Restore so other tests are unaffected
        _QuietFcmPushClient._patched_class = False

    @pytest.mark.asyncio
    async def test_listen_override_sets_resetting_before_log_decision(self):
        """Core regression test for issue #33.

        The patched _listen() must set run_state = RESETTING in the OSError handler
        BEFORE the quiet-path check runs.  We verify this by confirming that a
        ConnectionResetError caught while run_state == STARTED is NOT logged via
        _logger.exception — it should route to _log_verbose (INFO/debug) instead.

        Without the fix: run_state is STARTED when the check fires → else-branch →
        _logger.exception → ERROR + traceback in the log.
        With the fix: run_state is set to RESETTING first → isinstance check passes →
        _log_verbose → no ERROR.

        Behaviour note: after the fix routes to _log_verbose, the except block exits
        and the while loop re-enters with run_state == RESETTING, which hits
        ``asyncio.sleep(1)``.  We stop the loop inside fake_log_verbose (by setting
        do_listen=False) so the test terminates immediately without a real sleep.
        """
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClientRunState

        from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

        cls = _QuietFcmPushClient._patch_class()
        assert cls is not None

        # Build a minimal fake instance that has only the attributes _listen needs.
        # We drive the loop through exactly one iteration that raises ConnectionResetError.
        instance = object.__new__(cls)

        # --- State we need to control ---
        instance.run_state = FcmPushClientRunState.STARTED
        instance.do_listen = True

        connect_called: list[bool] = []
        login_called: list[bool] = []
        log_exception_called: list[str] = []
        log_verbose_called: list[str] = []

        async def fake_connect_with_retry() -> bool:
            connect_called.append(True)
            return True

        async def fake_login() -> None:
            login_called.append(True)

        iteration = [0]

        async def fake_receive_msg() -> bytes:
            iteration[0] += 1
            if iteration[0] == 1:
                raise ConnectionResetError("simulated WAN drop")
            return b""

        async def fake_handle_message(msg: bytes) -> None:
            pass

        def fake_log_verbose(fmt: str, *args: object) -> None:
            log_verbose_called.append(fmt)
            # Stop the loop here so we don't enter the asyncio.sleep(1) branch
            # (run_state == RESETTING) forever.  This is the correct termination
            # point: the quiet-path log fired, test is satisfied.
            instance.do_listen = False

        def fake_log_warn_with_limit(fmt: str, *args: object) -> None:
            pass

        async def fake_reset() -> None:
            # Not called in the quiet path, but needed for the fallback else-branch.
            instance.do_listen = False

        async def fake_writer_close() -> None:
            pass

        def fake_try_increment_error_count(error_type: object) -> bool:
            return True

        def fake_terminate() -> None:
            pass

        instance._connect_with_retry = fake_connect_with_retry  # type: ignore[attr-defined]
        instance._login = fake_login  # type: ignore[attr-defined]
        instance._receive_msg = fake_receive_msg  # type: ignore[attr-defined]
        instance._handle_message = fake_handle_message  # type: ignore[attr-defined]
        instance._log_verbose = fake_log_verbose  # type: ignore[attr-defined]
        instance._log_warn_with_limit = fake_log_warn_with_limit  # type: ignore[attr-defined]
        instance._reset = fake_reset  # type: ignore[attr-defined]
        instance._do_writer_close = fake_writer_close  # type: ignore[attr-defined]
        instance._try_increment_error_count = fake_try_increment_error_count  # type: ignore[attr-defined]
        instance._terminate = fake_terminate  # type: ignore[attr-defined]

        # Capture _logger.exception calls from our fcm module
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        original_exception = fcm_mod._LOGGER.exception

        def patched_exception(fmt: str, *args: object, **kwargs: object) -> None:
            log_exception_called.append(fmt)

        fcm_mod._LOGGER.exception = patched_exception  # type: ignore[method-assign]
        try:
            await instance._listen()
        finally:
            fcm_mod._LOGGER.exception = original_exception  # type: ignore[method-assign]

        assert not log_exception_called, (
            "With the fix, ConnectionResetError must NOT trigger _logger.exception — "
            "it should be routed to _log_verbose (INFO) instead.  "
            f"Got: {log_exception_called}"
        )
        assert log_verbose_called, (
            "ConnectionResetError during STARTED→RESETTING transition must log via "
            "_log_verbose (quiet INFO path) — confirms the fix is active"
        )

    @pytest.mark.asyncio
    async def test_listen_vanilla_would_log_exception_without_fix(self):
        """Counter-test: vanilla FcmPushClient._listen() DOES log _logger.exception
        on the first ConnectionResetError (run_state == STARTED at that point).

        This test documents the upstream bug and will fail if upstream ever ships
        a fix — at which point our subclass can be retired.

        Behaviour note: vanilla _listen() calls _reset() in the else-branch, and
        our fake_reset() sets do_listen=False so the loop terminates cleanly.
        """
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient, FcmPushClientRunState

        instance = object.__new__(FcmPushClient)
        instance.run_state = FcmPushClientRunState.STARTED
        instance.do_listen = True

        log_verbose_called: list[str] = []

        async def fake_connect_with_retry() -> bool:
            return True

        async def fake_login() -> None:
            pass

        iteration2 = [0]

        async def fake_receive_msg() -> bytes:
            iteration2[0] += 1
            if iteration2[0] == 1:
                raise ConnectionResetError("simulated WAN drop")
            instance.do_listen = False
            return b""

        async def fake_handle_message(msg: bytes) -> None:
            pass

        def fake_log_verbose(fmt: str, *args: object) -> None:
            log_verbose_called.append(fmt)

        def fake_log_warn_with_limit(fmt: str, *args: object) -> None:
            pass

        async def fake_reset() -> None:
            # Called by vanilla's else-branch after _logger.exception — stop loop.
            instance.do_listen = False

        async def fake_writer_close() -> None:
            pass

        def fake_try_increment_error_count(error_type: object) -> bool:
            return True

        def fake_terminate() -> None:
            pass

        instance._connect_with_retry = fake_connect_with_retry  # type: ignore[attr-defined]
        instance._login = fake_login  # type: ignore[attr-defined]
        instance._receive_msg = fake_receive_msg  # type: ignore[attr-defined]
        instance._handle_message = fake_handle_message  # type: ignore[attr-defined]
        instance._log_verbose = fake_log_verbose  # type: ignore[attr-defined]
        instance._log_warn_with_limit = fake_log_warn_with_limit  # type: ignore[attr-defined]
        instance._reset = fake_reset  # type: ignore[attr-defined]
        instance._do_writer_close = fake_writer_close  # type: ignore[attr-defined]
        instance._try_increment_error_count = fake_try_increment_error_count  # type: ignore[attr-defined]
        instance._terminate = fake_terminate  # type: ignore[attr-defined]

        import logging

        fcm_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        exception_records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.ERROR:
                    exception_records.append(record.getMessage())

        # Earlier tests that install the `_FCMNoiseFilter` via
        # `async_start_fcm_push` leave it on this logger and that filter
        # silently drops the very "Unexpected exception during read" record
        # we're asserting on. Snapshot + remove + restore so this counter-
        # test always sees the raw library output.
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        prev_filters = list(fcm_logger.filters)
        for _f in prev_filters:
            if isinstance(_f, _FCMNoiseFilter):
                fcm_logger.removeFilter(_f)

        handler = _Capture()
        fcm_logger.addHandler(handler)
        try:
            await FcmPushClient._listen(instance)  # type: ignore[arg-type]
        finally:
            fcm_logger.removeHandler(handler)
            for _f in prev_filters:
                if isinstance(_f, _FCMNoiseFilter):
                    fcm_logger.addFilter(_f)

        assert any("Unexpected exception" in r for r in exception_records), (
            "Upstream FcmPushClient._listen() MUST log 'Unexpected exception during read' "
            "on the first ConnectionResetError (documents the bug).  "
            "If this assertion fails, upstream has shipped a fix — retire _QuietFcmPushClient."
        )


# ── _FCMNoiseFilter dedup — filter installed on BOTH loggers ─────────────


class TestFCMNoiseFilterDualLogger:
    """Regression test: _install_fcm_noise_filter() must cover BOTH loggers.

    Root cause of the observed 14-errors-in-14-min bug (2026-05-16):
    _QuietFcmPushClient._listen() logs via _LOGGER (bosch module logger) in
    its fallback else-branch, but _FCMNoiseFilter was only installed on
    'firebase_messaging.fcmpushclient'. Records emitted through _LOGGER
    bypassed the filter entirely → every retry printed an ERROR line.

    Fix: _install_fcm_noise_filter() now installs a SINGLE shared
    _FCMNoiseFilter instance on both loggers so the 300s dedup window
    applies regardless of which logger is used.
    """

    def setup_method(self) -> None:
        """Remove any existing _FCMNoiseFilter from both loggers before each test."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        for logger in (lib_logger, bosch_logger):
            logger.filters = [
                f for f in logger.filters if not isinstance(f, _FCMNoiseFilter)
            ]

    def teardown_method(self) -> None:
        """Clean up installed filters after each test."""
        self.setup_method()

    def test_filter_installed_on_both_loggers(self) -> None:
        """After _install_fcm_noise_filter(), both loggers carry the filter."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        lib_filters = [f for f in lib_logger.filters if isinstance(f, _FCMNoiseFilter)]
        bosch_filters = [
            f for f in bosch_logger.filters if isinstance(f, _FCMNoiseFilter)
        ]

        assert lib_filters, (
            "filter must be installed on firebase_messaging.fcmpushclient"
        )
        assert bosch_filters, (
            "filter must ALSO be installed on bosch_shc_camera.fcm logger — "
            "_QuietFcmPushClient._listen() logs via _LOGGER, not the library logger"
        )

    def test_shared_instance_same_object(self) -> None:
        """Both loggers must carry the SAME filter instance so _last_passed is shared."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        lib_f = next(f for f in lib_logger.filters if isinstance(f, _FCMNoiseFilter))
        bosch_f = next(
            f for f in bosch_logger.filters if isinstance(f, _FCMNoiseFilter)
        )

        assert lib_f is bosch_f, (
            "Both loggers must share ONE filter instance — if they have separate "
            "instances each gets its own _last_passed and the dedup window is broken: "
            "the bosch logger lets records through even after the lib logger blocked them."
        )

    def test_bosch_logger_deduplicates_error_within_window(self) -> None:
        """Records logged via _LOGGER must be blocked by the shared dedup window.

        This is the exact failure path observed in the wild: 14 consecutive
        ERROR lines from _QuietFcmPushClient._listen() at 1/min because the
        filter only covered the library logger.
        """
        import logging
        import time

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()

        bosch_logger = fcm_mod._LOGGER

        # Capture records that PASS the filter on the bosch logger
        passed: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                passed.append(record.getMessage())

        cap = _Capture()
        bosch_logger.addHandler(cap)
        try:
            # Simulate 5 consecutive "Unexpected exception during read" errors
            # at 1-second intervals — only the first should pass.
            for _ in range(5):
                bosch_logger.error("Unexpected exception during read\n")
                # Advance the filter's notion of time by not sleeping — the
                # _last_passed comparison uses time.monotonic() internally;
                # since we're not patching time we just fire them synchronously.
        finally:
            bosch_logger.removeHandler(cap)

        assert len(passed) == 1, (
            f"Exactly 1 of 5 rapid 'Unexpected exception' records should pass the "
            f"300s dedup filter — got {len(passed)}.  "
            "If this fails, the filter is not installed on the bosch logger."
        )

    def test_idempotent_reinstall_does_not_double_install(self) -> None:
        """Calling _install_fcm_noise_filter() twice must not attach two filter
        instances to either logger (would allow two records through per window)."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()
        _install_fcm_noise_filter()  # second call must be a no-op

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        lib_count = sum(1 for f in lib_logger.filters if isinstance(f, _FCMNoiseFilter))
        bosch_count = sum(
            1 for f in bosch_logger.filters if isinstance(f, _FCMNoiseFilter)
        )

        assert lib_count == 1, f"lib logger must have exactly 1 filter, got {lib_count}"
        assert bosch_count == 1, (
            f"bosch logger must have exactly 1 filter, got {bosch_count}"
        )
