"""Tests for fcm.py async_send_alert coverage gaps.

Pins:
  Lines 727-729: step 1 `_notify_type` raises → log warning + return; the
                 function must NOT proceed to step 2 (no makedirs of the
                 alert_dir for media steps, no async_put_camera, no further
                 hass.services.async_call).
  Lines 818-819: direct clip.mp4 probe — when GET /v11/events/<id>/clip.mp4
                 returns HTTP 200 with Content-Type containing "video", set
                 `found_clip_url` to the canonical clip URL and bypass the
                 poll loop. The Content-Type guard is what stops a 200 HTML
                 error page from being treated as a video.

The first set covers the step-1 failure branch (a CancelledError thrown by
the outer `_notify_type` call). The second pins the direct-200 content-type
guard.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera.fcm"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"


def _resp_cm(
    status: int, body: bytes = b"", content_type: str = "image/jpeg", json_data=None
):
    """aiohttp-style async context manager response mock."""
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
    """Coordinator stub for async_send_alert tests."""
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
        "enable_local_save": False,
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


# ── Lines 727-729: step 1 exception → return before step 2 ───────────────────


class TestStep1Failure:
    """`_notify_type` for step 1 can raise (e.g. asyncio.CancelledError from a
    HA shutdown mid-alert). The outer try/except logs a warning and returns
    BEFORE any further work — no snapshot, no clip poll, no SMB.
    """

    @pytest.mark.asyncio
    async def test_step1_exception_logs_and_returns(self, caplog):
        """Step 1 raises → warning logged + early return; no step 2/3 work."""
        coord = _make_coord()

        # Bypass the `not info_svcs and not _is_trouble and ...` early-exit
        # (info_svcs derived from alert_notify_service="notify.test" → truthy).
        # The first `hass.services.async_call` (step 1 notify) is mocked to
        # raise.  The outer try wraps the whole `_notify_type` call, so any
        # exception inside the loop (including from svc.split) reaches the
        # `except Exception as err` at line 727 only when re-raised — but
        # _notify_type swallows per-service exceptions internally.  We force
        # the failure path by raising from within _notify_type *after* its
        # internal try (e.g. asyncio.CancelledError, which is intentionally
        # NOT caught by `except Exception` since 3.8).

        async def _raising_call(domain, service, data):
            # CancelledError propagates past _notify_type's `except Exception`
            # (which doesn't catch BaseException-derived in 3.8+).
            raise asyncio.CancelledError("HA shutting down")

        coord.hass.services.async_call = AsyncMock(side_effect=_raising_call)
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        with caplog.at_level("WARNING", logger=MODULE):
                            # CancelledError must propagate out of async_send_alert
                            # because Python 3.8+ no longer catches it via
                            # `except Exception`.  The whole function is
                            # cancelled mid-flight — step 2 never runs.
                            with pytest.raises(asyncio.CancelledError):
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-07T10:00:00.000Z",
                                    "",
                                )

        # No GET to the events endpoint must have been issued (step 2 skipped).
        assert session.get.call_count == 0, (
            "step 1 cancellation must short-circuit before step 2 issues any HTTP GET"
        )


class TestStep1FailureNonCancelled:
    """Variant: a regular Exception inside step 1 IS caught by the outer
    try/except (lines 727-729): warning logged, function returns cleanly,
    and step 2 never runs.

    Step 1 calls `_notify_type` which already wraps service calls in
    try/except; an exception only escapes if the outer machinery (e.g.
    coroutine scheduling) blows up.  We simulate this by patching
    `get_alert_services` to raise — that runs INSIDE _notify_type's loop
    before its inner try, so the exception propagates out and the outer
    try (line 724) catches it.
    """

    @pytest.mark.asyncio
    async def test_get_alert_services_raises_step1_caught(self, caplog):
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        call_count = [0]

        def _selective_raise(coord_arg, type_key):
            call_count[0] += 1
            # Initial info_svcs lookup at line 669 must return something so
            # the early-exit at 672 is NOT taken. Only raise on the inner
            # _notify_type call (line 712) which also calls get_alert_services.
            if call_count[0] == 1:
                return ["notify.test"]
            raise RuntimeError("synthetic services lookup failure")

        with patch(f"{MODULE}.get_alert_services", side_effect=_selective_raise):
            with patch(f"{MODULE}.async_get_clientsession", return_value=session):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            with caplog.at_level("WARNING", logger=MODULE):
                                # Regular Exception → caught at line 727 →
                                # warning + return, no propagation.
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-07T10:00:00.000Z",
                                    "",
                                )

        # The outer except logs a warning starting with "Alert step 1 failed"
        msgs = [r.getMessage() for r in caplog.records]
        assert any("Alert step 1 failed" in m for m in msgs), (
            f"step 1 outer except must log 'Alert step 1 failed: ...', got {msgs}"
        )
        # And step 2 must NOT issue any HTTP GET to the events endpoint.
        assert session.get.call_count == 0, (
            "step 1 regular-exception path must return before step 2"
        )


# ── Lines 818-819: direct clip.mp4 200 + video Content-Type ──────────────────


class TestDirectClipMp4ContentTypeGuard:
    """Pin the direct clip.mp4 probe content-type guard.

    Flow when `clip_url is empty` AND `event_id is set`:
      GET /v11/events/<event_id>/clip.mp4
        → status 200 AND Content-Type contains "video" → found_clip_url set
        → step 3 then downloads via the same URL.
    """

    @pytest.mark.asyncio
    async def test_200_video_content_type_sets_found_clip_url(self):
        """200 + Content-Type video/mp4 → direct clip.mp4 URL is used (no poll)."""
        coord = _make_coord(options={"alert_notify_service": "notify.test"})

        # Track which URLs were requested
        gets: list[str] = []

        def _get_side(url, headers=None, **kwargs):
            gets.append(url)
            if "/clip.mp4" in url:
                # Direct probe: 200 + video Content-Type → lines 817-819 fire
                return _resp_cm(200, body=b"", content_type="video/mp4")
            if "/events/" in url and "videoInputId=" in url:
                # Events list lookups for clip-polling fallback (must NOT
                # be hit because direct probe succeeded).
                return _resp_cm(404)
            # Step-3 download: return a payload > 1000 bytes so the write path
            # runs (so we can assert the download URL was the direct clip.mp4).
            return _resp_cm(200, body=b"x" * 2048, content_type="video/mp4")

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "MOVEMENT",
                            "2026-05-07T10:00:00.000Z",
                            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                            clip_url="",  # empty → triggers direct probe
                            clip_status="",
                            event_id="evt-direct-001",
                        )

        # The direct clip.mp4 URL must have been queried with the supplied event_id.
        direct_url = "/v11/events/evt-direct-001/clip.mp4"
        assert any(direct_url in u for u in gets), (
            f"direct clip.mp4 probe URL must be issued for the given event_id; got {gets}"
        )
        # No events-poll lookup with limit=3 must follow (we found the clip
        # in the direct probe → poll loop is skipped at line 827).
        poll_urls = [u for u in gets if "limit=3" in u]
        assert poll_urls == [], (
            f"direct clip 200/video must skip the poll fallback; saw poll calls: {poll_urls}"
        )

    @pytest.mark.asyncio
    async def test_200_non_video_content_type_does_not_set_clip_url(self):
        """200 but Content-Type=text/html → guard rejects → falls through to poll loop.

        Pins the OTHER half of the guard: status alone isn't enough.  A
        misconfigured proxy or error page returning HTTP 200 with HTML must
        NOT be treated as a video.
        """
        coord = _make_coord(options={"alert_notify_service": "notify.test"})
        gets: list[str] = []

        def _get_side(url, headers=None, **kwargs):
            gets.append(url)
            if "/clip.mp4" in url:
                # 200 but WRONG Content-Type → guard rejects
                return _resp_cm(200, body=b"<html>", content_type="text/html")
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "MOVEMENT",
                            "2026-05-07T10:00:00.000Z",
                            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                            clip_url="",
                            clip_status="",
                            event_id="evt-html-002",
                        )

        # The HTML 200 must NOT be accepted as a clip → poll fallback runs.
        # Poll loop hits the events?videoInputId=&limit=3 URL.
        poll_urls = [u for u in gets if "limit=3" in u]
        assert poll_urls, (
            f"200 with non-video Content-Type must NOT set found_clip_url; "
            f"the poll fallback must run instead, saw URLs: {gets}"
        )


# ── Path-traversal guard on the video clip filename (bug hunt 2026-06-10) ─────


class TestClipPathTraversalGuard:
    """Regression: the step-3 video clip path used the cloud-provided camera
    title (`cam_name`) verbatim in the `.mp4` filename, while the snapshot path
    one block above already neutralised it with `_safe_path_segment`. A title
    like "../../config/evil" let the `.mp4` write escape the alert dir.
    Pins that the clip path stays a direct child of alert_dir for a malicious
    title.  Bug found 2026-06-10."""

    @pytest.mark.asyncio
    async def test_malicious_cam_title_clip_path_stays_in_alert_dir(self):
        import os

        malicious = "../../config/evil"
        coord = _make_coord(options={"alert_notify_service": "notify.test"})
        # cam_id is resolved by matching the cloud title to cam_name → make the
        # malicious title the stored title so step 3 builds the clip path.
        coord.data = {CAM_ID: {"info": {"title": malicious}, "events": []}}

        clip_hits = [0]

        def _get_side(url, headers=None, **kwargs):
            if "/clip.mp4" in url:
                clip_hits[0] += 1
                # 1st hit = direct probe (empty 200 video) → sets found_clip_url;
                # 2nd hit = the actual download → >1000 bytes triggers _write_file.
                if clip_hits[0] == 1:
                    return _resp_cm(200, body=b"", content_type="video/mp4")
                return _resp_cm(200, body=b"x" * 2048, content_type="video/mp4")
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        with patch(f"{MODULE}.async_get_clientsession", return_value=session):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera import fcm
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            malicious,
                            "MOVEMENT",
                            "2026-05-07T10:00:00.000Z",
                            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                            clip_url="",
                            clip_status="",
                            event_id="evt-traversal-001",
                        )

        # Find the _write_file executor call for the .mp4 and grab its path arg.
        alert_dir = os.path.join(coord.hass.config.config_dir, "www", "bosch_alerts")
        mp4_paths = [
            c.args[1]
            for c in coord.hass.async_add_executor_job.call_args_list
            if len(c.args) >= 2
            and c.args[0] is fcm._write_file
            and str(c.args[1]).endswith(".mp4")
        ]
        assert mp4_paths, "step 3 must have written an .mp4 via _write_file"
        clip_path = mp4_paths[0]
        # The write must stay a DIRECT child of alert_dir — no traversal escape.
        assert ".." not in clip_path, f"clip path still contains '..': {clip_path}"
        assert os.path.dirname(os.path.normpath(clip_path)) == os.path.normpath(
            alert_dir
        ), f"clip path escaped alert_dir: {clip_path}"
