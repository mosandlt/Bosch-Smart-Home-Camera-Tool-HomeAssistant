"""Regression tests for stream-freeze-on-motion-event-contention fix.

ROOT CAUSE (knowledge-base/stream-freeze-on-motion-event-contention.md):
  Gen2 cameras have ONE TLS control channel shared by:
  - RTSP OPTIONS keepalive (~every 25 s, 30 s session timeout)
  - Motion-event processing: Path A live-snap (PUT /connection + snap.jpg)
  - SMB/FTP upload: urllib _http_get() in executor thread

  During a MOVEMENT event with an active live-stream:
  1. Path A triggered _async_trigger_image_refresh → fresh camera-side pull
  2. sync_smb_upload pulled snapshot from Bosch cloud (urllib/executor)
  Both saturated the camera's single TLS channel → RTSP keepalive RTT >30 s
  → go2rtc producer EOF → 5-10 s stream freeze.

FIX (fcm.py + smb.py):
  - Path A (async_handle_fcm_push): skip _async_trigger_image_refresh when
    cam_entity.is_streaming=True; Path B (alert step-2) already updates cache.
  - SMB/FTP upload (async_send_alert → sync_smb_upload/_sync_ftp_upload):
    pass prefetched_image bytes (already in memory from step-2 download) so
    the upload uses zero extra cloud/camera requests during a live-stream.

PIN_EVERY_MODE: one test each for is_streaming=True and is_streaming=False.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"
MODULE = "custom_components.bosch_shc_camera.fcm"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"

# Minimal JPEG-like bytes (>100 B) that look like a real snapshot.
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x42" * 400  # 404 B


# ── helpers ──────────────────────────────────────────────────────────────────


def _resp_cm(
    status: int,
    json_data: Any = None,
    body: bytes = b"",
    content_type: str = "application/json",
) -> Any:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else [])
    resp.read = AsyncMock(return_value=body)
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _one_movement_event(event_id: str = "new-evt") -> list[dict[str, Any]]:
    return [
        {
            "id": event_id,
            "eventType": "MOVEMENT",
            "eventTags": [],
            "timestamp": "2026-06-12T07:07:30Z",
            "imageUrl": "https://residential.cbs.boschsecurity.com/img.jpg",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }
    ]


def _make_push_coord(is_streaming: bool = False, **overrides: Any) -> Any:
    """Build a minimal coordinator stub for async_handle_fcm_push tests."""
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    task_stub = MagicMock(add_done_callback=MagicMock())
    hass.async_create_task = MagicMock(return_value=task_stub)
    hass.bus.async_fire = MagicMock()

    cam_entity = MagicMock()
    cam_entity._async_trigger_image_refresh = AsyncMock(return_value=None)
    cam_entity.is_streaming = is_streaming

    coord = SimpleNamespace(
        token="tok-test",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Innenbereich"}, "events": []}},
        _last_event_ids={CAM_ID: "old-evt"},
        _alert_sent_ids={},
        _camera_entities={CAM_ID: cam_entity},
        _image_entities={},
        _shc_state_cache={},
        _cached_events={},
        _bg_tasks=set(),
        _hw_version={CAM_ID: "HOME_Eyes_Indoor"},  # Gen2 Indoor
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_alert_coord(cam_entity: Any | None = None, **overrides: Any) -> Any:
    """Build a minimal coordinator stub for async_send_alert tests."""
    hass = MagicMock()
    hass.config.config_dir = "/tmp/bosch-test-alert"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    coord = SimpleNamespace(
        token="tok-alert",
        hass=hass,
        options={
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
        },
        data={
            CAM_ID: {"info": {"title": "Innenbereich"}, "events": []},
        },
        _last_event_ids={CAM_ID: "prior-event-id"},
        _camera_entities={CAM_ID: cam_entity} if cam_entity else {},
        _image_entities={},
        _shc_state_cache={CAM_ID: {"privacy_mode": False}},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


# ═══════════════════════════════════════════════════════════════════════════════
# Path A — live-snap refresh gated on is_streaming
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestPathAStreamingGuard:
    """Path A must skip _async_trigger_image_refresh when is_streaming=True."""

    async def test_path_a_skipped_when_streaming(self) -> None:
        """MOVEMENT event + is_streaming=True → NO _async_trigger_image_refresh call.

        Regression: before fix, Path A always scheduled the refresh regardless
        of streaming state, causing a fresh camera-side PUT /connection that
        saturated the RTSP TLS control channel.
        """
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord(is_streaming=True)
        cam_entity = coord._camera_entities[CAM_ID]

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, json_data=_one_movement_event("new-evt"))
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ),
            patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock),
        ):
            await async_handle_fcm_push(coord)

        # Path A must NOT call _async_trigger_image_refresh when streaming.
        cam_entity._async_trigger_image_refresh.assert_not_called()

    async def test_path_a_fires_when_not_streaming(self) -> None:
        """MOVEMENT event + is_streaming=False → _async_trigger_image_refresh IS called.

        PIN_EVERY_MODE: is_streaming=False must keep the original behaviour.
        """
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord(is_streaming=False)
        cam_entity = coord._camera_entities[CAM_ID]

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, json_data=_one_movement_event("new-evt"))
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ),
            patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock),
        ):
            await async_handle_fcm_push(coord)

        # Path A MUST schedule the refresh when not streaming.
        cam_entity._async_trigger_image_refresh.assert_called_once_with(delay=0)


# ═══════════════════════════════════════════════════════════════════════════════
# SMB prefetch — sync_smb_upload uses caller-supplied bytes, no _http_get call
# ═══════════════════════════════════════════════════════════════════════════════


class TestSmbPrefetchedImage:
    """sync_smb_upload(prefetched_image=...) must use supplied bytes, skip _http_get."""

    def _make_coord(self) -> Any:
        coord = MagicMock()
        coord.options = {
            "enable_smb_upload": True,
            "smb_server": "nas.local",
            "smb_share": "Bosch",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "Cams",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            "upload_protocol": "smb",
        }
        return coord

    def _smb_data(self, img_url: str = "") -> dict[str, Any]:
        return {
            CAM_ID: {
                "info": {"title": "Innenbereich"},
                "events": [
                    {
                        "id": "aabbccdd-test",
                        "eventType": "MOVEMENT",
                        "timestamp": "2026-06-12T07:07:30Z",
                        "imageUrl": img_url,
                        "videoClipUrl": "",
                        "videoClipUploadStatus": "",
                    }
                ],
            }
        }

    def test_prefetched_image_bypasses_http_get(self) -> None:
        """When prefetched_image is provided, _http_get must NOT be called.

        Regression: before fix, sync_smb_upload always called _http_get(imageUrl)
        to download the snapshot, competing with RTSP on the camera's TLS channel.
        """
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = self._make_coord()
        data = self._smb_data(
            img_url="https://residential.cbs.boschsecurity.com/img.jpg"
        )

        mock_open_file = MagicMock()
        mock_open_file.return_value.__enter__ = MagicMock(return_value=MagicMock())
        mock_open_file.return_value.__exit__ = MagicMock(return_value=False)

        with (
            patch(f"{SMB_MODULE}.smbclient", create=True),
            patch(f"{SMB_MODULE}.register_session", create=True) as mock_register,
            patch(f"{SMB_MODULE}.open_file", mock_open_file, create=True),
            patch(
                f"{SMB_MODULE}.smb_stat", side_effect=OSError("not found"), create=True
            ),
            patch(f"{SMB_MODULE}.smb_makedirs"),
            patch(f"{SMB_MODULE}._http_get") as mock_get,
        ):
            # Patch the import inside sync_smb_upload
            import importlib
            import sys

            # Inject a mock smbclient module so the `import smbclient` inside
            # sync_smb_upload succeeds without the real library installed.
            fake_smb = MagicMock()
            fake_smb.register_session = MagicMock()
            fake_smb.open_file = mock_open_file
            fake_smb.stat = MagicMock(side_effect=OSError("not found"))
            fake_smb.mkdir = MagicMock()
            sys.modules.setdefault("smbclient", fake_smb)

            sync_smb_upload(coord, data, "tok", prefetched_image=JPEG_BYTES)

        # _http_get must NOT have been called — prefetched bytes were used.
        mock_get.assert_not_called()

    def test_no_prefetch_calls_http_get(self) -> None:
        """When prefetched_image=None, _http_get IS called (backward compat).

        PIN_EVERY_MODE: is_streaming=False / no prefetch must keep original behaviour.
        """
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = self._make_coord()
        img_url = "https://residential.cbs.boschsecurity.com/img.jpg"
        data = self._smb_data(img_url=img_url)

        import sys

        fake_smb = MagicMock()
        fake_smb.register_session = MagicMock()
        fake_smb.open_file = MagicMock(
            return_value=MagicMock(
                __enter__=MagicMock(return_value=MagicMock()),
                __exit__=MagicMock(return_value=False),
            )
        )
        fake_smb.stat = MagicMock(side_effect=OSError("not found"))
        fake_smb.mkdir = MagicMock()
        sys.modules["smbclient"] = fake_smb

        with (
            patch(f"{SMB_MODULE}.smb_makedirs"),
            patch(
                f"{SMB_MODULE}._http_get", return_value=(200, JPEG_BYTES)
            ) as mock_get,
        ):
            sync_smb_upload(coord, data, "tok", prefetched_image=None)

        # Without prefetch, _http_get must be called to download the snapshot.
        mock_get.assert_called_once_with(img_url, "tok", timeout=30)

    def test_prefetched_image_written_when_file_missing(self) -> None:
        """File-doesn't-exist + prefetched bytes → write prefetched bytes, no fetch.

        Pins smb.py L364-367: this combination (smb_stat raises OSError i.e.
        the snapshot isn't already on the SMB share, AND the caller supplied
        prefetched_image bytes) was previously untested in isolation — the
        sibling test above shares its cloud-import path with `sys.modules`
        (via ``setdefault``), which is order-dependent: once any earlier test
        in the suite has triggered a real `import smbclient`, `setdefault`
        becomes a no-op and the genuine smbclient package (not the mock) gets
        used, so `register_session` fails against a real network call and the
        function returns before ever reaching the upload branch. This test
        installs its fake `smbclient` via unconditional assignment (matching
        the robust pattern in test_smb_open_file_cleanup.py), so it reliably
        exercises L364-367 regardless of test execution order.
        """
        from custom_components.bosch_shc_camera.smb import sync_smb_upload

        coord = self._make_coord()
        data = self._smb_data(
            img_url="https://residential.cbs.boschsecurity.com/img.jpg"
        )

        import sys

        written: dict[str, Any] = {}

        def _fake_open_file(path: str, mode: str = "r") -> Any:
            handle = MagicMock()

            def _write(content: bytes) -> None:
                written["path"] = path
                written["mode"] = mode
                written["content"] = content

            handle.write = MagicMock(side_effect=_write)
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=handle)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        fake_smb = MagicMock()
        fake_smb.register_session = MagicMock()
        fake_smb.open_file = MagicMock(side_effect=_fake_open_file)
        fake_smb.stat = MagicMock(side_effect=OSError("not found"))
        fake_smb.mkdir = MagicMock()
        sys.modules["smbclient"] = fake_smb

        with (
            patch(f"{SMB_MODULE}.smb_makedirs"),
            patch(f"{SMB_MODULE}._http_get") as mock_get,
        ):
            sync_smb_upload(coord, data, "tok", prefetched_image=JPEG_BYTES)

        # L364-367: prefetched bytes written in binary mode, no cloud fetch.
        fake_smb.register_session.assert_called_once()
        mock_get.assert_not_called()
        assert written["mode"] == "wb"
        assert written["content"] == JPEG_BYTES
        assert written["path"].endswith(
            "Innenbereich_2026-06-12_07-07-30_MOVEMENT_aabbccdd.jpg"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# FTP prefetch — _sync_ftp_upload uses caller-supplied bytes, no _http_get call
# ═══════════════════════════════════════════════════════════════════════════════


class TestFtpPrefetchedImage:
    """_sync_ftp_upload(prefetched_image=...) must use supplied bytes, skip _http_get."""

    def _make_coord(self, protocol: str = "ftp") -> Any:
        coord = MagicMock()
        coord.options = {
            "enable_smb_upload": True,
            "smb_server": "fritz.box",
            "smb_username": "user",
            "smb_password": "pass",
            "smb_base_path": "Cams",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
            "file_pattern": "{camera}_{date}_{time}_{type}_{id}",
            "upload_protocol": protocol,
        }
        return coord

    def _ftp_data(self, img_url: str = "") -> dict[str, Any]:
        return {
            CAM_ID: {
                "info": {"title": "Innenbereich"},
                "events": [
                    {
                        "id": "aabbccdd-ftp",
                        "eventType": "MOVEMENT",
                        "timestamp": "2026-06-12T07:07:30Z",
                        "imageUrl": img_url,
                        "videoClipUrl": "",
                        "videoClipUploadStatus": "",
                    }
                ],
            }
        }

    def test_ftp_prefetched_bypasses_http_get(self) -> None:
        """FTP upload: prefetched_image → no _http_get call."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = self._make_coord()
        img_url = "https://residential.cbs.boschsecurity.com/img.jpg"
        data = self._ftp_data(img_url=img_url)

        fake_ftp = MagicMock()
        fake_ftp.size = MagicMock(
            side_effect=Exception("not found")
        )  # file doesn't exist
        fake_ftp.storbinary = MagicMock()
        fake_ftp.quit = MagicMock()

        with (
            patch(f"{SMB_MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{SMB_MODULE}._ftp_exists", return_value=False),
            patch(f"{SMB_MODULE}._ftp_makedirs"),
            patch(f"{SMB_MODULE}._http_get") as mock_get,
        ):
            _sync_ftp_upload(coord, data, "tok", prefetched_image=JPEG_BYTES)

        mock_get.assert_not_called()
        # storbinary must have been called with the prefetched bytes.
        fake_ftp.storbinary.assert_called_once()
        call_args = fake_ftp.storbinary.call_args
        # Second arg is a BytesIO wrapping JPEG_BYTES.
        from io import BytesIO

        stored_bytes = call_args[0][1].read()
        assert stored_bytes == JPEG_BYTES

    def test_ftp_no_prefetch_calls_http_get(self) -> None:
        """FTP upload: no prefetch → _http_get IS called (backward compat)."""
        from custom_components.bosch_shc_camera.smb import _sync_ftp_upload

        coord = self._make_coord()
        img_url = "https://residential.cbs.boschsecurity.com/img.jpg"
        data = self._ftp_data(img_url=img_url)

        fake_ftp = MagicMock()
        fake_ftp.storbinary = MagicMock()
        fake_ftp.quit = MagicMock()

        with (
            patch(f"{SMB_MODULE}._ftp_connect", return_value=fake_ftp),
            patch(f"{SMB_MODULE}._ftp_exists", return_value=False),
            patch(f"{SMB_MODULE}._ftp_makedirs"),
            patch(
                f"{SMB_MODULE}._http_get", return_value=(200, JPEG_BYTES)
            ) as mock_get,
        ):
            _sync_ftp_upload(coord, data, "tok", prefetched_image=None)

        mock_get.assert_called_once_with(img_url, "tok", timeout=30)


# ═══════════════════════════════════════════════════════════════════════════════
# async_send_alert — _prefetched_snapshot propagated to SMB upload
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestAlertPipelinePrefetch:
    """async_send_alert passes downloaded snapshot bytes to sync_smb_upload.

    async_add_executor_job(fn, *args) is what actually calls sync_smb_upload
    in a thread pool.  We intercept it by making async_add_executor_job call
    the function synchronously in the test (no real thread needed).
    The lazy import `from .smb import … sync_smb_upload` in async_send_alert
    resolves at runtime, so the correct patch target is the smb module.
    """

    def _make_coord_for_alert(
        self, tmp_path: Any, cam_entity: Any | None = None
    ) -> Any:
        coord = _make_alert_coord(
            cam_entity=cam_entity,
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "",
                "alert_notify_screenshot": "",
                "alert_notify_video": "",
                "alert_notify_system": "",
                "alert_save_snapshots": False,
                "alert_delete_after_send": True,
                "mark_events_read": False,
                "enable_smb_upload": True,
                "smb_server": "nas.local",
                "enable_local_save": False,
                "download_path": "",
            },
        )
        coord.hass.config = MagicMock(config_dir=str(tmp_path))
        return coord

    async def test_smb_receives_prefetched_bytes(self, tmp_path: Any) -> None:
        """When step-2 downloads image bytes, sync_smb_upload receives them as
        prefetched_image — no second cloud pull needed.

        async_add_executor_job calls sync_smb_upload(coord, data, token, pf).
        We capture the call by intercepting async_add_executor_job.
        """
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        cam_entity = MagicMock()
        cam_entity.is_streaming = True
        coord = self._make_coord_for_alert(tmp_path, cam_entity=cam_entity)

        img_resp = MagicMock()
        img_resp.status = 200
        img_resp.read = AsyncMock(return_value=JPEG_BYTES)
        img_resp.headers = {"Content-Type": "image/jpeg"}
        img_cm = MagicMock()
        img_cm.__aenter__ = AsyncMock(return_value=img_resp)
        img_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=img_cm)

        captured_prefetch: list[bytes | None] = []

        # async_add_executor_job(fn, *args) — call fn(*args) synchronously and
        # capture args when fn is sync_smb_upload.
        async def _fake_executor(fn: Any, *args: Any, **kwargs: Any) -> None:
            from custom_components.bosch_shc_camera import smb as _smb_mod

            if fn is _smb_mod.sync_smb_upload:
                # args = (coordinator, data, token, prefetched_image)
                prefetch = args[3] if len(args) > 3 else kwargs.get("prefetched_image")
                captured_prefetch.append(prefetch)
            # Don't actually run fn — SMB needs real smbprotocol

        coord.hass.async_add_executor_job = _fake_executor

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
        ):
            await async_send_alert(
                coord,
                "Innenbereich",
                "MOVEMENT",
                "2026-06-12T07:07:30Z",
                "https://residential.cbs.boschsecurity.com/img.jpg",
                "",
                "",
                event_id="aabbccdd-test",
            )

        assert len(captured_prefetch) == 1, (
            f"sync_smb_upload must be called once via executor; calls={captured_prefetch}"
        )
        assert captured_prefetch[0] == JPEG_BYTES, (
            "sync_smb_upload must receive the step-2 snapshot bytes as prefetched_image"
        )

    async def test_smb_receives_none_when_no_image(self, tmp_path: Any) -> None:
        """When no imageUrl / step-2 is skipped, prefetched_image=None (backward compat).

        PIN_EVERY_MODE: no-image path must leave prefetched_image=None.
        """
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        coord = self._make_coord_for_alert(tmp_path)

        # All session.get calls → 404 (no image, no clip)
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        captured_prefetch: list[bytes | None] = []

        async def _fake_executor(fn: Any, *args: Any, **kwargs: Any) -> None:
            from custom_components.bosch_shc_camera import smb as _smb_mod

            if fn is _smb_mod.sync_smb_upload:
                prefetch = args[3] if len(args) > 3 else kwargs.get("prefetched_image")
                captured_prefetch.append(prefetch)

        coord.hass.async_add_executor_job = _fake_executor

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
        ):
            await async_send_alert(
                coord,
                "Innenbereich",
                "MOVEMENT",
                "2026-06-12T07:07:30Z",
                "",  # no imageUrl → step 2 skipped → _prefetched_snapshot stays None
                "",
                "",
                event_id="aabbccdd-none",
            )

        assert len(captured_prefetch) == 1, (
            f"sync_smb_upload must be called once via executor; calls={captured_prefetch}"
        )
        assert captured_prefetch[0] is None, (
            "prefetched_image must be None when step-2 image was not downloaded"
        )
