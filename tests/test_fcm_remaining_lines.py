"""Tests for fcm.py — remaining uncovered lines.

Target lines:
  213-217: _on_creds_updated / _persist closure inside async_start_fcm_push
  221:     _on_push closure inside async_start_fcm_push
  583-584: bare except around async_mark_events_read in async_handle_fcm_push
  955-958: asyncio.TimeoutError + bare Exception in local save block of async_send_alert

Strategy: invoke async_start_fcm_push with a mock FcmPushClient that captures the
callbacks passed to it, then call those callbacks directly to reach the inner closures.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "custom_components.bosch_shc_camera.fcm"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"
CAM_ID = "11111111-1111-1111-1111-111111111111"


# ── shared helpers ─────────────────────────────────────────────────────────────


def _resp_cm(status: int, body: bytes = b"", json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.json = AsyncMock(return_value=json_data if json_data is not None else [])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_fcm_modules():
    """Return a fake firebase_messaging module dict for patch.dict."""
    mock_fm = MagicMock()
    mock_fm.FcmRegisterConfig = MagicMock(return_value=MagicMock())
    mock_fm.FcmPushClientConfig = MagicMock(return_value=MagicMock())
    return {
        "firebase_messaging": mock_fm,
        "firebase_messaging.FcmPushClient": mock_fm.FcmPushClient,
    }


def _make_start_coord(push_mode="ios"):
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.config_entries = MagicMock()
    hass.async_create_task = MagicMock()
    entry = SimpleNamespace(data={})
    return SimpleNamespace(
        _fcm_running=False,
        _fcm_client=None,
        _fcm_token=None,
        _fcm_lock=threading.Lock(),
        _fcm_healthy=False,
        _fcm_push_mode="unknown",
        options={"enable_fcm_push": True, "fcm_push_mode": push_mode},
        hass=hass,
        _entry=entry,
        data={},
    )


def _make_alert_coord(options=None):
    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha-remaining"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    base_opts = {
        "alert_notify_service": "notify.test",
        "alert_notify_information": "",
        "alert_notify_screenshot": "",
        "alert_notify_video": "",
        "alert_notify_system": "",
        "alert_save_snapshots": False,
        "alert_delete_after_send": False,
        "mark_events_read": False,
        "enable_smb_upload": False,
        "enable_local_save": False,
        "download_path": "",
        "smb_server": "",
    }
    if options:
        base_opts.update(options)

    return SimpleNamespace(
        token="tok-remaining",
        hass=hass,
        options=base_opts,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        _last_event_ids={CAM_ID: "event-id-001"},
    )


# ── Lines 213-217 and 221: closures inside async_start_fcm_push ──────────────


class TestAsyncStartFcmPushClosures:
    """Capture the callbacks passed to FcmPushClient and invoke them directly.

    Lines 213-217: _on_creds_updated -> _persist() -> hass.loop.call_soon_threadsafe
    Line 221:      _on_push -> _on_fcm_push(coordinator, ...)
    """

    async def _run_start(self, push_mode="ios"):
        """Run async_start_fcm_push with a capturing mock client.

        Returns (coord, captured_callbacks) where captured_callbacks has
        keys 'credentials_updated_callback' and 'callback'.
        """
        captured = {}

        class CapturingClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.checkin_or_register = AsyncMock(
                    return_value="fake-fcm-token-abc123"
                )
                self.start = AsyncMock()

        coord = _make_start_coord(push_mode=push_mode)

        mock_fm = MagicMock()
        mock_fm.FcmRegisterConfig = MagicMock(return_value=MagicMock())
        mock_fm.FcmPushClientConfig = MagicMock(return_value=MagicMock())
        mock_fm.FcmPushClient = CapturingClient

        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        with patch.dict(sys.modules, {"firebase_messaging": mock_fm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.register_fcm_with_bosch",
                    new_callable=lambda: AsyncMock(return_value=True),
                ):
                    await async_start_fcm_push(coord)

        return coord, captured

    @pytest.mark.asyncio
    async def test_on_creds_updated_calls_call_soon_threadsafe(self):
        """Lines 213-217: _on_creds_updated invokes call_soon_threadsafe with _persist closure."""
        coord, captured = await self._run_start(push_mode="ios")

        assert "credentials_updated_callback" in captured, (
            "FcmPushClient must receive credentials_updated_callback"
        )

        creds_cb = captured["credentials_updated_callback"]
        fake_creds = {"token": "abc", "keys": {}}

        # Call the outer callback — this should call hass.loop.call_soon_threadsafe
        coord.hass.loop.call_soon_threadsafe.reset_mock()
        creds_cb(fake_creds)

        coord.hass.loop.call_soon_threadsafe.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_creds_updated_persist_closure_creates_task(self):
        """Lines 213-217: _persist() is the arg passed to call_soon_threadsafe; calling it
        creates an async task via hass.async_create_task."""
        coord, captured = await self._run_start(push_mode="ios")

        creds_cb = captured["credentials_updated_callback"]
        fake_creds = {"token": "xyz"}

        # Capture what gets passed to call_soon_threadsafe
        persist_fn = None

        def _capture_threadsafe(fn):
            nonlocal persist_fn
            persist_fn = fn

        coord.hass.loop.call_soon_threadsafe = _capture_threadsafe
        creds_cb(fake_creds)

        assert persist_fn is not None, (
            "_persist must have been passed to call_soon_threadsafe"
        )

        # Now call _persist() directly — this exercises lines 214-215
        coord.hass.async_create_task = MagicMock()
        persist_fn()

        coord.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_push_delegates_to_on_fcm_push(self):
        """Line 221: _on_push closure calls _on_fcm_push with the coordinator."""
        _coord, captured = await self._run_start(push_mode="ios")

        assert "callback" in captured, "FcmPushClient must receive callback"
        push_cb = captured["callback"]

        calls = []

        def _fake_on_fcm_push(c, notif, pid, obj=None):
            calls.append((c, notif, pid, obj))

        with patch(f"{MODULE}._on_fcm_push", side_effect=_fake_on_fcm_push):
            push_cb({"from": "bosch"}, "persistent-id-1")

        assert len(calls) == 1
        _, notif, pid, obj = calls[0]
        assert notif == {"from": "bosch"}
        assert pid == "persistent-id-1"
        assert obj is None  # default

    @pytest.mark.asyncio
    async def test_on_push_passes_obj_argument(self):
        """Line 221: _on_push passes the optional obj kwarg through to _on_fcm_push."""
        _coord, captured = await self._run_start(push_mode="ios")
        push_cb = captured["callback"]

        calls = []

        def _fake_on_fcm_push(c, notif, pid, obj=None):
            calls.append(obj)

        some_obj = object()
        with patch(f"{MODULE}._on_fcm_push", side_effect=_fake_on_fcm_push):
            push_cb({"from": "bosch"}, "pid-2", obj=some_obj)

        assert calls == [some_obj]


# ── Lines 583-584: exception in mark_events_read inside async_handle_fcm_push ─


class TestHandleFcmPushMarkEventsReadException:
    """Lines 583-584: when mark_events_read raises inside async_handle_fcm_push,
    the bare `except Exception: pass` must swallow it silently."""

    def _make_handle_coord(self):
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        hass.async_create_task = MagicMock()
        hass.bus.async_fire = MagicMock()
        return SimpleNamespace(
            token="tok-handle",
            hass=hass,
            data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
            _last_event_ids={CAM_ID: "old-event-id"},
            _alert_sent_ids={},
            _cached_events={},
            _camera_entities={},
            _bg_tasks=set(),
            options={"mark_events_read": True},
            async_update_listeners=MagicMock(),
            _fcm_last_push=float("-inf"),
            _cached_status={},
        )

    @pytest.mark.asyncio
    async def test_mark_events_read_exception_swallowed_in_handle_push(self):
        """Lines 583-584: async_mark_events_read raises inside the push handler
        but the exception must be silently swallowed."""
        coord = self._make_handle_coord()

        new_event = {
            "id": "new-event-id",
            "eventType": "MOVEMENT",
            "eventTags": [],
            "timestamp": "2026-05-12T10:00:00Z",
            "imageUrl": "",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }

        # Build a session that returns the new event
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[new_event]))

        async def _raising_mark(c, ids):
            raise RuntimeError("mark-read network error")

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_mark_events_read", side_effect=_raising_mark):
                from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

                # Must complete without raising
                await async_handle_fcm_push(coord)

        # The new event id must still have been recorded despite the mark failure
        assert coord._last_event_ids[CAM_ID] == "new-event-id"

    @pytest.mark.asyncio
    async def test_mark_events_read_not_called_when_option_off(self):
        """Ensure mark_events_read is skipped when option is False (control test)."""
        coord = self._make_handle_coord()
        coord.options = {"mark_events_read": False}

        new_event = {
            "id": "new-event-id-2",
            "eventType": "MOVEMENT",
            "eventTags": [],
            "timestamp": "2026-05-12T10:01:00Z",
            "imageUrl": "",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[new_event]))

        mark_calls = []

        async def _track_mark(c, ids):
            mark_calls.append(ids)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.async_mark_events_read", side_effect=_track_mark):
                from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

                await async_handle_fcm_push(coord)

        assert mark_calls == [], (
            "mark_events_read must not be called when option is off"
        )


# ── Lines 955-958: local save asyncio.TimeoutError + generic Exception ─────────


class TestLocalSaveExceptionBranches:
    """Lines 955-958: exceptions from async_send_alert's local save block.

    Line 955-956: asyncio.TimeoutError → logged as warning, not re-raised.
    Line 957-958: generic Exception → logged as warning, not re-raised.
    """

    def _run_alert_with_wait_for(self, wait_for_side_effect):
        """Helper: run async_send_alert with local save enabled and a controlled wait_for."""
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        async def _run():
            from custom_components.bosch_shc_camera.fcm import async_send_alert

            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            with patch(
                                f"{MODULE}.asyncio.wait_for",
                                side_effect=wait_for_side_effect,
                            ):
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-12T10:00:00.000Z",
                                    "",
                                )

        asyncio.get_event_loop().run_until_complete(_run())

    @pytest.mark.asyncio
    async def test_local_save_timeout_does_not_propagate(self):
        """Lines 955-956: asyncio.TimeoutError in local save is caught and logged."""
        raised = []

        async def _timeout_wait_for(coro, timeout=None):
            raised.append("timeout")
            raise TimeoutError()

        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        with patch(
                            f"{MODULE}.asyncio.wait_for", side_effect=_timeout_wait_for
                        ):
                            # Must not raise
                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-12T10:00:00.000Z",
                                "",
                            )

        assert raised, (
            "wait_for must have been called (proving local save path was reached)"
        )

    @pytest.mark.asyncio
    async def test_local_save_generic_exception_does_not_propagate(self):
        """Lines 957-958: generic Exception in local save is caught and logged."""
        raised = []

        async def _error_wait_for(coro, timeout=None):
            raised.append("error")
            raise RuntimeError("disk full")

        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        with patch(
                            f"{MODULE}.asyncio.wait_for", side_effect=_error_wait_for
                        ):
                            # Must not raise
                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-12T10:00:00.000Z",
                                "",
                            )

        assert raised, (
            "wait_for must have been called (proving local save path was reached)"
        )

    @pytest.mark.asyncio
    async def test_local_save_timeout_logged_as_warning(self, caplog):
        """Lines 955-956: TimeoutError path logs a warning with cam_name."""
        import logging

        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        async def _timeout_wait_for(coro, timeout=None):
            raise TimeoutError()

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with caplog.at_level(
            logging.WARNING, logger="custom_components.bosch_shc_camera.fcm"
        ):
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            with patch(
                                f"{MODULE}.asyncio.wait_for",
                                side_effect=_timeout_wait_for,
                            ):
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-12T10:00:00.000Z",
                                    "",
                                )

        timeout_msgs = [
            r
            for r in caplog.records
            if "local save timed out" in r.message and r.levelno == logging.WARNING
        ]
        assert timeout_msgs, "A WARNING about 'local save timed out' must be emitted"
        assert "Terrasse" in timeout_msgs[0].message

    @pytest.mark.asyncio
    async def test_local_save_exception_logged_as_warning(self, caplog):
        """Lines 957-958: generic Exception path logs a warning with cam_name and error."""
        import logging

        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        async def _error_wait_for(coro, timeout=None):
            raise OSError("no space left on device")

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with caplog.at_level(
            logging.WARNING, logger="custom_components.bosch_shc_camera.fcm"
        ):
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            with patch(
                                f"{MODULE}.asyncio.wait_for",
                                side_effect=_error_wait_for,
                            ):
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-12T10:00:00.000Z",
                                    "",
                                )

        err_msgs = [
            r
            for r in caplog.records
            if "local save failed" in r.message and r.levelno == logging.WARNING
        ]
        assert err_msgs, "A WARNING about 'local save failed' must be emitted"
        assert "Terrasse" in err_msgs[0].message
