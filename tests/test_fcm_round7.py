"""Tests for fcm.py async_send_alert — 3-step alert pipeline.

Sprint F coverage target: fcm.py lines 627-930 (async_send_alert):
  Lines 633-636:  early exit — no info services + non-trouble event
  Lines 686-695:  step 1 text alert routing (information vs system key)
  Lines 694-695:  TROUBLE_CONNECT/DISCONNECT returns after step 1 (no media)
  Lines 704-728:  step 2 image_url retry loop (empty imageUrl → re-fetch)
  Lines 730-753:  step 2 snap download + screenshot notify + cleanup flag
  Lines 759-853:  step 3 video clip (direct clip.mp4, poll, download, notify)
  Lines 856-862:  mark_events_read gate in send_alert
  Lines 865-898:  SMB upload (enabled/disabled, timeout)
  Lines 901-921:  local save (enabled/disabled, timeout)
  Lines 924-930:  file cleanup (delete_after flag)

Uses SimpleNamespace stubs — no real HA runtime, no network.
Regression: duplicate text-alert bug (concurrent push, 60s dedup already
tested in test_fcm_round6.py); step-2 snapshot silent skip (2026-04-26,
observed: text sent, imageUrl populated 90s later → retry loop fix).
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

CAM_ID = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
MODULE = "custom_components.bosch_shc_camera.fcm"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"


# ── shared helpers ────────────────────────────────────────────────────────────

def _resp_cm(status: int, body: bytes = b"", content_type: str = "image/jpeg",
             json_data=None):
    """Async context-manager mock for aiohttp session responses."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.json = AsyncMock(return_value=json_data or [])
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_coord(options=None, **overrides):
    """Return a minimal coordinator stub for async_send_alert tests."""
    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    base_opts = {
        "alert_notify_service": "notify.test",
        "alert_notify_information": "",
        "alert_notify_screenshot": "",
        "alert_notify_video": "",
        "alert_notify_system": "",
        "alert_save_snapshots": False,
        "alert_delete_after_send": True,
        "mark_events_read": False,
        "enable_smb_upload": False,
        "download_path": "",
    }
    if options:
        base_opts.update(options)

    coord = SimpleNamespace(
        token="tok-A",
        hass=hass,
        options=base_opts,
        data={
            CAM_ID: {"info": {"title": "Terrasse"}, "events": []},
        },
        _last_event_ids={CAM_ID: "event-id-001"},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _run_send_alert(coord, event_type="MOVEMENT", image_url="", clip_url="",
                    clip_status="", cam_name="Terrasse",
                    timestamp="2026-05-07T10:00:00.000Z",
                    session_override=None):
    """Helper: call async_send_alert with a mocked aiohttp session."""
    from custom_components.bosch_shc_camera.fcm import async_send_alert

    session = session_override or MagicMock()
    session.get = MagicMock(return_value=_resp_cm(404))

    async def _run():
        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch("custom_components.bosch_shc_camera.smb.sync_smb_upload", MagicMock()):
                    with patch("custom_components.bosch_shc_camera.smb.sync_local_save", MagicMock()):
                        await async_send_alert(
                            coord, cam_name, event_type, timestamp,
                            image_url, clip_url, clip_status,
                        )

    return _run()


# ── 1. Early exit — no services configured ───────────────────────────────────

class TestAsyncSendAlertEarlyExit:
    """Lines 633-636: no info services + non-trouble → return immediately."""

    @pytest.mark.asyncio
    async def test_no_services_returns_before_makedirs(self):
        """No information services, no system services → makedirs never called."""
        coord = _make_coord(options={
            "alert_notify_service": "",
            "alert_notify_information": "",
            "alert_notify_system": "",
        })
        await _run_send_alert(coord, event_type="MOVEMENT")
        coord.hass.async_add_executor_job.assert_not_awaited(), \
            "must not call makedirs when no services configured for non-trouble event"

    @pytest.mark.asyncio
    async def test_trouble_event_with_no_info_services_does_not_return_early(self):
        """TROUBLE_CONNECT must proceed even when information services are empty."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_information": "",
            "alert_notify_system": "",
        })
        # get_alert_services("system") falls back to alert_notify_service="notify.signal"
        # so step 1 should be attempted
        await _run_send_alert(coord, event_type="TROUBLE_CONNECT")
        coord.hass.async_add_executor_job.assert_awaited(), \
            "TROUBLE_CONNECT must not exit early — must call makedirs"

    @pytest.mark.asyncio
    async def test_movement_with_services_proceeds(self):
        """MOVEMENT + at least one service configured → makedirs called."""
        coord = _make_coord()  # alert_notify_service = "notify.test"
        await _run_send_alert(coord, event_type="MOVEMENT")
        coord.hass.async_add_executor_job.assert_awaited(), \
            "must call makedirs when services are configured"


# ── 2. Step 1 text alert — routing ───────────────────────────────────────────

class TestStep1TextAlert:
    """Lines 686-695: step 1 routes to 'system' for trouble, 'information' otherwise."""

    @pytest.mark.asyncio
    async def test_movement_step1_calls_information_service(self):
        """MOVEMENT → _notify_type('information', ...) → hass.services.async_call."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_information": "notify.info_svc",
        })
        await _run_send_alert(coord, event_type="MOVEMENT",
                              image_url="https://residential.cbs.boschsecurity.com/img.jpg")
        # async_call(domain, service, data) → args[1] is the service name
        calls = [str(c) for c in coord.hass.services.async_call.call_args_list]
        assert any("info_svc" in s for s in calls), \
            "MOVEMENT step 1 must route through 'information' services"

    @pytest.mark.asyncio
    async def test_trouble_connect_step1_calls_system_service(self):
        """TROUBLE_CONNECT → routes to 'system' key → system service called."""
        coord = _make_coord(options={
            "alert_notify_system": "notify.system_svc",
            "alert_notify_information": "notify.info_svc",
        })
        await _run_send_alert(coord, event_type="TROUBLE_CONNECT")
        calls = coord.hass.services.async_call.call_args_list
        # system service must have been called; info service must NOT
        svc_names = [str(c) for c in calls]
        assert any("system_svc" in s for s in svc_names), \
            "TROUBLE_CONNECT step 1 must use 'system' service"
        assert not any("info_svc" in s for s in svc_names), \
            "TROUBLE_CONNECT step 1 must NOT use 'information' service"

    @pytest.mark.asyncio
    async def test_trouble_connect_returns_after_step1(self):
        """TROUBLE_CONNECT/DISCONNECT returns after step 1 — no makedirs for alert_dir."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
        })
        await _run_send_alert(coord, event_type="TROUBLE_DISCONNECT")
        # makedirs is called once (before step 1), but no screenshot/video writes
        # Check that async_add_executor_job was only called for makedirs (not _write_file)
        write_calls = [
            c for c in coord.hass.async_add_executor_job.call_args_list
            if c.args and callable(c.args[0]) and getattr(c.args[0], "__name__", "") == "_write_file"
        ]
        assert len(write_calls) == 0, \
            "TROUBLE events must not write any files (no snapshot/clip for connectivity events)"

    @pytest.mark.asyncio
    async def test_step1_text_contains_cam_name_and_type(self):
        """Step 1 message must contain camera name and event label."""
        coord = _make_coord()
        captured_calls = []
        coord.hass.services.async_call = AsyncMock(
            side_effect=lambda d, s, data: captured_calls.append(data.get("message", ""))
        )
        await _run_send_alert(coord, event_type="MOVEMENT",
                              timestamp="2026-05-07T10:00:00.000Z")
        assert any("Terrasse" in m for m in captured_calls), \
            "step 1 message must contain the camera name"
        assert any("Bewegung" in m or "MOVEMENT" in m for m in captured_calls), \
            "step 1 message must contain the event type label"


# ── 3. Step 2 — empty image_url retry loop ───────────────────────────────────

class TestStep2ImageUrlRetry:
    """Lines 704-728: empty image_url → retry loop (3 attempts with delays)."""

    @pytest.mark.asyncio
    async def test_empty_image_url_triggers_refetch(self):
        """image_url='' → must attempt session.get to re-fetch events."""
        coord = _make_coord()
        session = MagicMock()
        # Return empty events on all re-fetch attempts so the loop exhausts
        session.get.return_value = _resp_cm(200, json_data=[])

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",  # empty imageUrl
                            )

        await _run()
        assert session.get.call_count >= 3, \
            "empty image_url must trigger at least 3 re-fetch attempts"

    @pytest.mark.asyncio
    async def test_empty_image_url_found_on_second_attempt_proceeds(self):
        """image_url becomes available on 2nd re-fetch → step 2 download triggered."""
        coord = _make_coord()
        session = MagicMock()
        call_count = [0]

        @asynccontextmanager
        async def _get(url, **kw):
            call_count[0] += 1
            resp = MagicMock()
            if "events" in url and call_count[0] == 2:
                # Second event re-fetch → imageUrl populated
                resp.status = 200
                resp.json = AsyncMock(return_value=[{
                    "imageUrl": "https://residential.cbs.boschsecurity.com/img.jpg",
                    "videoClipUrl": "",
                    "videoClipUploadStatus": "",
                }])
                resp.read = AsyncMock(return_value=b"")
            elif "img.jpg" in url:
                # Snap download after finding imageUrl
                resp.status = 200
                resp.headers = {"Content-Type": "image/jpeg"}
                resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            else:
                resp.status = 200
                resp.json = AsyncMock(return_value=[])
                resp.read = AsyncMock(return_value=b"")
                resp.headers = {"Content-Type": "text/json"}
            yield resp

        session.get = _get
        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",  # empty imageUrl
                            )
        await _run()
        # _write_file must have been called for the snapshot
        write_calls = [
            c for c in coord.hass.async_add_executor_job.call_args_list
            if c.args and len(c.args) >= 2 and isinstance(c.args[1], str) and c.args[1].endswith(".jpg")
        ]
        assert len(write_calls) >= 1, \
            "when image_url found on retry, step 2 must write the snapshot file"

    @pytest.mark.asyncio
    async def test_unsafe_image_url_rejected_no_snap_download(self):
        """Unsafe imageUrl from Bosch API response must be rejected — no file written."""
        coord = _make_coord()
        session = MagicMock()
        session.get.return_value = _resp_cm(200, json_data=[{
            "imageUrl": "http://evil.example.com/steal.jpg",
        }])

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "http://evil.example.com/steal.jpg",  # unsafe from start
                            )
        await _run()
        # The unsafe URL must be cleared before download — no _write_file for .jpg
        write_jpg_calls = [
            c for c in coord.hass.async_add_executor_job.call_args_list
            if c.args and len(c.args) >= 2 and isinstance(c.args[1], str)
            and c.args[1].endswith(".jpg")
        ]
        assert len(write_jpg_calls) == 0, \
            "unsafe imageUrl must be rejected — no snapshot file written"


# ── 4. Step 2 — snapshot download and screenshot notify ──────────────────────

class TestStep2SnapshotDownload:
    """Lines 730-753: snap.jpg download → write file + screenshot service call."""

    @pytest.mark.asyncio
    async def test_snap_200_writes_file_and_notifies(self):
        """snap.jpg 200 + image/* → write to alert_dir + screenshot service called."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_screenshot": "notify.signal",
            "alert_save_snapshots": True,  # keep file so it's not cleaned up
        })

        @asynccontextmanager
        async def _get(url, **kw):
            resp = MagicMock()
            resp.status = 200
            resp.headers = {"Content-Type": "image/jpeg"}
            resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img,
                            )
        await _run()

        # screenshot service must be called
        svc_calls = [c.args[:2] for c in coord.hass.services.async_call.call_args_list]
        assert ("notify", "signal") in svc_calls, \
            "screenshot notify service must be called after snap.jpg 200"

        # _write_file must have been called with a .jpg path
        write_jpg_calls = [
            c for c in coord.hass.async_add_executor_job.call_args_list
            if c.args and len(c.args) >= 2 and isinstance(c.args[1], str)
            and c.args[1].endswith(".jpg")
        ]
        assert len(write_jpg_calls) >= 1, \
            "must write snapshot file after 200 + image/jpeg response"

    @pytest.mark.asyncio
    async def test_snap_200_empty_body_skips_write(self):
        """snap.jpg 200 + empty body → guard `if data:` prevents file write."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord()

        @asynccontextmanager
        async def _get(url, **kw):
            resp = MagicMock()
            resp.status = 200
            resp.headers = {"Content-Type": "image/jpeg"}
            resp.read = AsyncMock(return_value=b"")  # empty body
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img,
                            )
        await _run()

        write_jpg_calls = [
            c for c in coord.hass.async_add_executor_job.call_args_list
            if c.args and len(c.args) >= 2 and isinstance(c.args[1], str)
            and c.args[1].endswith(".jpg")
        ]
        assert len(write_jpg_calls) == 0, "empty snap body must not result in file write"

    @pytest.mark.asyncio
    async def test_delete_after_adds_to_cleanup(self):
        """alert_delete_after_send=True (default) → snap path added to files_to_cleanup."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_screenshot": "notify.signal",
            "alert_save_snapshots": False,  # delete_after=True is default
        })
        removed_files = []

        async def _exec_job(fn, *args, **kw):
            if callable(fn) and getattr(fn, "__name__", "") == "remove":
                removed_files.append(args[0] if args else "")
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec_job)

        @asynccontextmanager
        async def _get(url, **kw):
            resp = MagicMock()
            resp.status = 200
            resp.headers = {"Content-Type": "image/jpeg"}
            resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img,
                            )
        await _run()
        assert any(f.endswith(".jpg") for f in removed_files), \
            "snapshot file must be cleaned up when delete_after_send=True"


# ── 5. Step 3 — video clip direct download ────────────────────────────────────

class TestStep3VideoClipDirect:
    """Lines 770-784: direct clip.mp4 download check before polling."""

    @pytest.mark.asyncio
    async def test_clip_status_done_with_url_skips_direct_and_poll(self):
        """clip_url given + clip_status=Done → use directly, skip direct check and poll."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        clip_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/clip.mp4"
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_video": "notify.signal",
        })

        get_calls = []

        @asynccontextmanager
        async def _get(url, **kw):
            get_calls.append(url)
            resp = MagicMock()
            if url.endswith(".jpg"):
                resp.status = 200
                resp.headers = {"Content-Type": "image/jpeg"}
                resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            elif url.endswith(".mp4"):
                resp.status = 200
                resp.headers = {"Content-Type": "video/mp4"}
                resp.read = AsyncMock(return_value=b"\x00" * 2000)  # >1000 bytes
            else:
                resp.status = 200
                resp.headers = {"Content-Type": "application/json"}
                resp.json = AsyncMock(return_value=[])
                resp.read = AsyncMock(return_value=b"")
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img, clip_url, "Done",
                            )
        await _run()
        # Clip should be downloaded and video service called
        svc_calls = [c.args[:2] for c in coord.hass.services.async_call.call_args_list]
        assert ("notify", "signal") in svc_calls, \
            "video service must be called when clip_status=Done and url is provided"

    @pytest.mark.asyncio
    async def test_clip_status_unavailable_skips_poll(self):
        """clip_status=Unavailable from start → polling loop skipped."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord()

        poll_count = [0]

        @asynccontextmanager
        async def _get(url, **kw):
            resp = MagicMock()
            if "events?videoInputId" in url and "limit=3" in url:
                poll_count[0] += 1
            resp.status = 200
            resp.headers = {"Content-Type": "image/jpeg"}
            resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            resp.json = AsyncMock(return_value=[])
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img, "", "Unavailable",
                            )
        await _run()
        assert poll_count[0] == 0, \
            "clip_status=Unavailable must skip polling loop entirely"


# ── 6. Step 3 — clip download and video notify ───────────────────────────────

class TestStep3VideoDownload:
    """Lines 828-851: clip URL found → download → write .mp4 → video service."""

    @pytest.mark.asyncio
    async def test_found_clip_url_small_body_skips_write(self):
        """found_clip_url but body <= 1000 bytes → guard prevents file write."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        clip_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/clip.mp4"
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_video": "notify.signal",
        })

        @asynccontextmanager
        async def _get(url, **kw):
            resp = MagicMock()
            if url.endswith(".jpg"):
                resp.status = 200
                resp.headers = {"Content-Type": "image/jpeg"}
                resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            elif url.endswith(".mp4") and "clip.mp4" in url:
                resp.status = 200
                resp.headers = {"Content-Type": "video/mp4"}
                resp.read = AsyncMock(return_value=b"\x00" * 500)  # <= 1000 bytes
            else:
                resp.status = 200
                resp.headers = {"Content-Type": "application/json"}
                resp.json = AsyncMock(return_value=[])
                resp.read = AsyncMock(return_value=b"")
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img, clip_url, "Done",
                            )
        await _run()

        write_mp4_calls = [
            c for c in coord.hass.async_add_executor_job.call_args_list
            if c.args and len(c.args) >= 2 and isinstance(c.args[1], str)
            and c.args[1].endswith(".mp4")
        ]
        assert len(write_mp4_calls) == 0, \
            "video body <= 1000 bytes must be rejected — not written as clip"

    @pytest.mark.asyncio
    async def test_unsafe_clip_url_rejected(self):
        """Clip URL not on Bosch domain → _is_safe_bosch_url rejects it."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_video": "notify.signal",
        })
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        unsafe_clip = "https://evil.example.com/clip.mp4"
        session_get_urls = []

        @asynccontextmanager
        async def _get(url, **kw):
            session_get_urls.append(url)
            resp = MagicMock()
            resp.status = 200
            resp.headers = {"Content-Type": "image/jpeg"}
            resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img, unsafe_clip, "Done",
                            )
        await _run()
        assert not any("evil.example.com" in u for u in session_get_urls), \
            "unsafe clip URL must never be fetched via session.get"


# ── 7. mark_events_read gate in send_alert ───────────────────────────────────

class TestMarkEventsReadInSendAlert:
    """Lines 856-862: mark_events_read=True → async_mark_events_read called at end."""

    @pytest.mark.asyncio
    async def test_mark_events_read_true_calls_mark(self):
        """mark_events_read=True + cam found → async_mark_events_read awaited."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "mark_events_read": True,
        })
        mock_mark = AsyncMock(return_value=True)

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{MODULE}.async_mark_events_read", mock_mark):
                        with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                            with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                                from custom_components.bosch_shc_camera.fcm import async_send_alert
                                await async_send_alert(
                                    coord, "Terrasse", "MOVEMENT",
                                    "2026-05-07T10:00:00.000Z", "",
                                )
        await _run()
        mock_mark.assert_awaited(), \
            "mark_events_read=True must call async_mark_events_read in send_alert"

    @pytest.mark.asyncio
    async def test_mark_events_read_false_skips_mark(self):
        """mark_events_read=False → async_mark_events_read NOT called in send_alert."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "mark_events_read": False,
        })
        mock_mark = AsyncMock(return_value=True)

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{MODULE}.async_mark_events_read", mock_mark):
                        with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                            with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                                from custom_components.bosch_shc_camera.fcm import async_send_alert
                                await async_send_alert(
                                    coord, "Terrasse", "MOVEMENT",
                                    "2026-05-07T10:00:00.000Z", "",
                                )
        await _run()
        mock_mark.assert_not_awaited(), \
            "mark_events_read=False must not call async_mark_events_read in send_alert"


# ── 8. SMB upload gate ────────────────────────────────────────────────────────

class TestSmbUploadGate:
    """Lines 865-898: enable_smb_upload + smb_server → executor job for SMB upload."""

    @pytest.mark.asyncio
    async def test_smb_disabled_no_executor_smb_call(self):
        """enable_smb_upload=False → sync_smb_upload never called."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "enable_smb_upload": False,
        })
        mock_smb = MagicMock()

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", mock_smb):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z", "",
                            )
        await _run()
        mock_smb.assert_not_called(), \
            "sync_smb_upload must not be called when enable_smb_upload=False"

    @pytest.mark.asyncio
    async def test_smb_enabled_calls_executor_smb(self):
        """enable_smb_upload=True + smb_server set → sync_smb_upload called via executor."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "enable_smb_upload": True,
            "smb_server": "//nas/share",
        })
        executor_fns = []

        async def _exec(fn, *args, **kw):
            executor_fns.append(fn)
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)
        mock_smb = MagicMock(__name__="sync_smb_upload")

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", mock_smb):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z", "",
                            )
        await _run()
        # The patched mock_smb should have been passed to async_add_executor_job
        assert mock_smb in executor_fns, \
            "sync_smb_upload must be submitted to executor when smb is enabled"

    @pytest.mark.asyncio
    async def test_smb_timeout_does_not_raise(self):
        """SMB upload timeout after 30s must be caught — not propagated to caller."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "enable_smb_upload": True,
            "smb_server": "//nas/share",
        })

        async def _exec(fn, *args, **kw):
            if getattr(fn, "__name__", "") == "sync_smb_upload":
                raise asyncio.TimeoutError()
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            # Must not raise
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z", "",
                            )
        await _run()  # passes if no exception


# ── 9. Local save gate ────────────────────────────────────────────────────────

class TestLocalSaveGate:
    """Lines 901-921: download_path set → sync_local_save called via executor."""

    @pytest.mark.asyncio
    async def test_download_path_empty_skips_local_save(self):
        """download_path='' → sync_local_save never called."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "download_path": "",
        })
        mock_save = MagicMock()

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", mock_save):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z", "",
                            )
        await _run()
        mock_save.assert_not_called(), \
            "sync_local_save must not be called when download_path is empty"

    @pytest.mark.asyncio
    async def test_download_path_set_calls_local_save(self):
        """download_path set → sync_local_save submitted via async_add_executor_job."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "download_path": "/mnt/nvr",
        })
        executor_fns = []

        async def _exec(fn, *args, **kw):
            executor_fns.append(fn)
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)
        mock_save = MagicMock(__name__="sync_local_save")

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", mock_save):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z", "",
                            )
        await _run()
        assert mock_save in executor_fns, \
            "sync_local_save must be submitted to executor when download_path is set"

    @pytest.mark.asyncio
    async def test_local_save_timeout_does_not_raise(self):
        """local save timeout must be caught — not propagated."""
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "download_path": "/mnt/nvr",
        })

        async def _exec(fn, *args, **kw):
            if getattr(fn, "__name__", "") == "sync_local_save":
                raise asyncio.TimeoutError()
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=MagicMock()):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z", "",
                            )
        await _run()  # passes if no exception


# ── 10. File cleanup ──────────────────────────────────────────────────────────

class TestFileCleanup:
    """Lines 924-930: delete_after_send=True → os.remove called for temp files."""

    @pytest.mark.asyncio
    async def test_save_snapshots_true_no_cleanup(self):
        """alert_save_snapshots=True → files NOT added to cleanup list."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_coord(options={
            "alert_notify_service": "notify.signal",
            "alert_notify_screenshot": "notify.signal",
            "alert_save_snapshots": True,
            "alert_delete_after_send": True,  # delete_after only applies when save=False
        })
        removed = []

        async def _exec(fn, *args, **kw):
            name = getattr(fn, "__name__", "")
            if name == "remove":
                removed.append(args[0] if args else "")
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)

        @asynccontextmanager
        async def _get(url, **kw):
            resp = MagicMock()
            resp.status = 200
            resp.headers = {"Content-Type": "image/jpeg"}
            resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            yield resp

        session = MagicMock()
        session.get = _get

        async def _run():
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import async_send_alert
                            await async_send_alert(
                                coord, "Terrasse", "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                safe_img,
                            )
        await _run()
        jpg_removed = [p for p in removed if p.endswith(".jpg")]
        assert len(jpg_removed) == 0, \
            "alert_save_snapshots=True must not remove the snapshot file"
