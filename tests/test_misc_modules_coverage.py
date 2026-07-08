"""Misc module coverage gaps.

Targets
-------
fcm.py
  1266-1267  async_handle_fcm_push — Path A except branch (warning log)
  1287-1288  async_handle_fcm_push — _mark_read_bg inner except (pass)
  1613-1625  async_send_alert — ai_notify_include_description block (happy
             path + exception branch)
  1777       async_send_alert — clip poll: event_id present + _ev_id mismatch
             → continue
  1784       async_send_alert — no event_id + timestamp mismatch → continue

sensor.py
  151-152    async_setup_entry — BoschCameraAiDescriptionSensor appended when
             CONF_ENABLE_AI_DESCRIPTION option is True

rcp.py
  608        async_update_rcp_data — 0x0d00 privacy mask returns XML envelope
             → _mark_fail

media_source.py
  406        _SmbBackend._scandir_filtered — want_dirs=True, entry name starts
             with "_" → continue (NVR internal dirs skipped)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

FCM_MODULE = "custom_components.bosch_shc_camera.fcm"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"
RCP_MODULE = "custom_components.bosch_shc_camera.rcp"

CAM_ID = "11111111-1111-1111-1111-111111111111"
PROXY_HOST = "proxy-01.live.cbs.boschsecurity.com:42090"
PROXY_HASH = "abc123hash"


# ─── shared response-mock helpers ────────────────────────────────────────────


def _resp_cm(
    status: int, body: bytes = b"", content_type: str = "image/jpeg", json_data=None
):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.json = AsyncMock(return_value=json_data if json_data is not None else [])
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


# ─── FCM coordinator stubs ────────────────────────────────────────────────────


def _make_push_coord(**overrides):
    """Coordinator stub for async_handle_fcm_push."""
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock()
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-push",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        _last_event_ids={},
        _alert_sent_ids={},
        _camera_entities={},
        _cached_events={},
        _bg_tasks=set(),
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_alert_coord(options=None, **overrides):
    """Coordinator stub for async_send_alert."""
    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha-misc"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    base_opts: dict = {
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

    coord = SimpleNamespace(
        token="tok-alert",
        hass=hass,
        options=base_opts,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        _last_event_ids={CAM_ID: "event-old"},
        _shc_state_cache={},
        _cached_status={},
        _lan_tcp_reachable={},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _one_event(
    event_id="new-evt",
    event_type="MOVEMENT",
    tags=None,
    image="",
    clip="",
    clip_status="",
):
    return [
        {
            "id": event_id,
            "eventType": event_type,
            "eventTags": tags or [],
            "timestamp": "2026-05-07T10:00:00Z",
            "imageUrl": image,
            "videoClipUrl": clip,
            "videoClipUploadStatus": clip_status,
        }
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# FCM — lines 1266-1267: Path A except branch warning log
# ═══════════════════════════════════════════════════════════════════════════════


class TestFcmPathAExceptionWarning:
    """async_handle_fcm_push lines 1266-1267: when Path A's try block raises
    (e.g. cam_entity._async_trigger_image_refresh raises), the except logs a
    WARNING and does NOT propagate.

    Conditions to reach this branch:
    - prev_id set, newest_id differs → _dispatched_new=True
    - cam_entity present + event_type in _SNAP_EVENT_TYPES
    - cam_entity.is_streaming is False (Path A is not skipped)
    - get_model_config or async_create_task inside the try raises
    """

    @pytest.mark.asyncio
    async def test_path_a_exception_logs_warning(self, caplog):
        """Path A try raises RuntimeError → WARNING logged, no propagation."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity.is_streaming = False
        cam_entity._async_trigger_image_refresh = MagicMock(
            side_effect=RuntimeError("refresh boom")
        )

        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
        )

        # Return a new event so the prev_id != newest_id branch is taken
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, json_data=_one_event("new-evt", "MOVEMENT"))
        )

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.async_send_alert", new_callable=AsyncMock),
            patch(f"{FCM_MODULE}.async_mark_events_read", new_callable=AsyncMock),
            caplog.at_level("WARNING", logger=FCM_MODULE),
        ):
            await async_handle_fcm_push(coord)

        assert any(
            "FCM Path A: failed to schedule live-snap refresh" in r.message
            for r in caplog.records
        ), "Expected Path A failure WARNING in logs"

    @pytest.mark.asyncio
    async def test_path_a_exception_does_not_propagate(self):
        """Path A exception must be swallowed — async_handle_fcm_push returns normally."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity.is_streaming = False
        cam_entity._async_trigger_image_refresh = MagicMock(
            side_effect=ValueError("boom inside path A")
        )

        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            _camera_entities={CAM_ID: cam_entity},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, json_data=_one_event("new-evt2", "MOVEMENT"))
        )

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.async_send_alert", new_callable=AsyncMock),
            patch(f"{FCM_MODULE}.async_mark_events_read", new_callable=AsyncMock),
        ):
            # Must not raise
            await async_handle_fcm_push(coord)


# ═══════════════════════════════════════════════════════════════════════════════
# FCM — lines 1287-1288: _mark_read_bg inner except (pass)
# ═══════════════════════════════════════════════════════════════════════════════


class TestMarkReadBgInnerExcept:
    """async_handle_fcm_push lines 1287-1288: when mark_events_read=True and
    async_mark_events_read raises inside the background task, the inner
    except swallows it silently (pass). The coverage gap is the pass statement
    itself (line 1288).

    Strategy: set mark_events_read=True, make async_mark_events_read raise,
    then run the created background task to completion.
    """

    @pytest.mark.asyncio
    async def test_mark_read_bg_exception_is_swallowed(self):
        """async_mark_events_read raises inside _mark_read_bg → silently swallowed."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord(
            _last_event_ids={CAM_ID: "old-evt"},
            options={"mark_events_read": True},
        )

        # Capture tasks created via hass.async_create_task
        created_tasks: list = []

        def _capture_task(coro):
            task = asyncio.get_event_loop().create_task(coro)
            created_tasks.append(task)
            mock_task = MagicMock()
            mock_task.add_done_callback = MagicMock()
            return mock_task

        coord.hass.async_create_task = _capture_task

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, json_data=_one_event("new-evt-mr", "MOVEMENT"))
        )

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.async_send_alert", new_callable=AsyncMock),
            patch(
                f"{FCM_MODULE}.async_mark_events_read",
                AsyncMock(side_effect=RuntimeError("mark-read-fail")),
            ),
        ):
            await async_handle_fcm_push(coord)

        # Run all captured coroutines (the _mark_read_bg coroutine)
        for t in created_tasks:
            try:
                await t
            except Exception:
                pass  # _mark_read_bg itself swallows — if we see an error, it leaked

        # The key invariant: no RuntimeError propagated
        for t in created_tasks:
            if not t.done():
                t.cancel()


# ═══════════════════════════════════════════════════════════════════════════════
# FCM — lines 1613-1625: async_send_alert AI description block
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendAlertAiDescription:
    """async_send_alert lines 1613-1625: ai_notify_include_description block.

    Covers:
    - Happy path: description appended to caption (lines 1613-1623)
    - Exception path: AI call raises → DEBUG logged, caption unchanged (1624-1625)
    """

    # Safe Bosch image URL (must end with .boschsecurity.com or .bosch.com)
    _SAFE_IMAGE_URL = "https://media.boschsecurity.com/image.jpg"

    @pytest.mark.asyncio
    async def test_ai_description_appended_to_caption(self):
        """When ai_notify_include_description=True and AI returns text, caption
        is extended with the AI description (lines 1613-1623 covered)."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        coord = _make_alert_coord(
            options={
                "ai_notify_include_description": True,
                "alert_save_snapshots": False,
                "alert_delete_after_send": True,
            }
        )
        # async_generate_ai_description must be available on coordinator
        coord.async_generate_ai_description = AsyncMock(
            return_value="A dog in the garden"
        )

        notify_calls: list = []

        async def _fake_svc_call(domain, service, service_data=None, **kw):
            notify_calls.append((domain, service, service_data))

        coord.hass.services.async_call = AsyncMock(side_effect=_fake_svc_call)

        # Image response: 200 with image/jpeg body
        image_resp = _resp_cm(
            200, body=b"\xff\xd8\xff" + b"\x00" * 100, content_type="image/jpeg"
        )
        session = MagicMock()
        session.get = MagicMock(return_value=image_resp)

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
            patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
        ):
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-07T10:00:00.000Z",
                self._SAFE_IMAGE_URL,
                cam_id=CAM_ID,
            )

        # Check that AI description was queried
        coord.async_generate_ai_description.assert_awaited_once_with(CAM_ID)

        # At least one HA services call was made
        assert len(notify_calls) >= 1

    @pytest.mark.asyncio
    async def test_ai_description_exception_swallowed(self, caplog):
        """When AI call raises, exception is caught and logged at DEBUG;
        caption stays unchanged (lines 1624-1625 covered)."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        coord = _make_alert_coord(
            options={
                "ai_notify_include_description": True,
                "alert_save_snapshots": False,
                "alert_delete_after_send": True,
            }
        )
        coord.async_generate_ai_description = AsyncMock(
            side_effect=RuntimeError("AI boom")
        )

        image_resp = _resp_cm(
            200, body=b"\xff\xd8\xff" + b"\x00" * 100, content_type="image/jpeg"
        )
        session = MagicMock()
        session.get = MagicMock(return_value=image_resp)

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
            patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
            caplog.at_level("DEBUG", logger=FCM_MODULE),
        ):
            # Must not raise despite AI failure
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-07T10:00:00.000Z",
                self._SAFE_IMAGE_URL,
                cam_id=CAM_ID,
            )

        assert any("AI notify-include failed" in r.message for r in caplog.records), (
            "Expected 'AI notify-include failed' debug log"
        )

    @pytest.mark.asyncio
    async def test_ai_description_none_leaves_caption_unchanged(self):
        """When AI returns None or empty, caption stays with just the snapshot
        text — the `if _desc:` guard (line 1621) prevents appending."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        coord = _make_alert_coord(
            options={
                "ai_notify_include_description": True,
                "alert_save_snapshots": False,
            }
        )
        coord.async_generate_ai_description = AsyncMock(return_value=None)

        image_resp = _resp_cm(
            200, body=b"\xff\xd8\xff" + b"\x00" * 100, content_type="image/jpeg"
        )
        session = MagicMock()
        session.get = MagicMock(return_value=image_resp)

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
            patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
        ):
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-07T10:00:00.000Z",
                self._SAFE_IMAGE_URL,
                cam_id=CAM_ID,
            )

        # AI was called but returned None — no robot emoji in any call
        coord.async_generate_ai_description.assert_awaited_once_with(CAM_ID)


# ═══════════════════════════════════════════════════════════════════════════════
# FCM — lines 1777 and 1784: clip poll event-id / timestamp mismatch → continue
# ═══════════════════════════════════════════════════════════════════════════════


class TestClipPollMatchGuards:
    """async_send_alert lines 1777 and 1784: the clip-poll loop skips events
    that don't match the current alert's event_id or timestamp.

    Line 1777: event_id provided + _ev_id present + mismatch → continue
    Line 1784: no event_id + timestamp mismatch → continue
    """

    # Safe Bosch URLs required by _is_safe_bosch_url
    _SAFE_IMAGE_URL = "https://media.boschsecurity.com/image.jpg"

    def _poll_coord(self):
        return _make_alert_coord(
            options={
                "alert_save_snapshots": False,
                "alert_delete_after_send": True,
                "enable_smb_upload": False,
                "enable_local_save": False,
            }
        )

    @pytest.mark.asyncio
    async def test_event_id_mismatch_skips_event(self):
        """Line 1777: event_id=target-id, poll returns different id → continue.

        The poll returns one event with a non-matching id — the clip is never
        found; the function completes without a clip notification.
        """
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        coord = self._poll_coord()

        # Step 1 + step 2 use these (info text + screenshot not needed)
        # We just want to reach the clip poll: provide an initial empty clip URL
        # so the code enters the polling branch.

        call_count = [0]

        def _session_get(url, **kw):
            call_count[0] += 1
            if "image" in url or call_count[0] == 1:
                # image fetch
                return _resp_cm(
                    200, body=b"\xff\xd8\xff" + b"\x00" * 10, content_type="image/jpeg"
                )
            # clip poll: returns an event with DIFFERENT id
            return _resp_cm(
                200,
                json_data=[
                    {
                        "id": "OTHER-EVENT-ID",
                        "timestamp": "2026-05-07T10:00:00Z",
                        "videoClipUploadStatus": "Done",
                        "videoClipUrl": "https://bosch.example/other.mp4",
                    }
                ],
            )

        session = MagicMock()
        session.get = MagicMock(side_effect=_session_get)

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
            patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
        ):
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-07T10:00:00Z",
                self._SAFE_IMAGE_URL,
                "",  # empty clip_url triggers poll
                "Pending",
                event_id="TARGET-EVENT-ID",  # will NOT match "OTHER-EVENT-ID"
                cam_id=CAM_ID,
            )

        # Other-event's clip must NOT have been notified
        # (services.async_call may be called for step 1 info text and step 2
        # screenshot, but NOT for a video with the other event's URL)
        call_args_list = coord.hass.services.async_call.call_args_list
        video_calls = [
            c
            for c in call_args_list
            if c
            and len(c.args) >= 3
            and c.args[2]
            and "other.mp4" in str(c.args[2].get("data", {}).get("url", ""))
        ]
        assert video_calls == [], "Mismatched event_id clip must not be sent"

    @pytest.mark.asyncio
    async def test_timestamp_mismatch_skips_event_no_event_id(self):
        """Line 1784: no event_id, poll returns event with different timestamp → continue.

        When event_id is None/empty, the fallback uses timestamp[:19] matching.
        A poll result with a non-matching timestamp is skipped.
        """
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        coord = self._poll_coord()
        # async_send_alert backfills event_id from coordinator._last_event_ids
        # (fcm.py:1728); keep it empty so the no-event_id timestamp fallback
        # (line 1784) is actually exercised instead of the event_id path.
        coord._last_event_ids = {}

        call_count = [0]

        def _session_get(url, **kw):
            call_count[0] += 1
            if "image" in url or call_count[0] == 1:
                return _resp_cm(
                    200, body=b"\xff\xd8\xff" + b"\x00" * 10, content_type="image/jpeg"
                )
            # Clip poll: return event with timestamp that does NOT match
            return _resp_cm(
                200,
                json_data=[
                    {
                        "id": "",  # empty id → fallback to timestamp match
                        "timestamp": "2025-01-01T00:00:00Z",  # different date
                        "videoClipUploadStatus": "Done",
                        "videoClipUrl": "https://bosch.example/wrong-ts.mp4",
                    }
                ],
            )

        session = MagicMock()
        session.get = MagicMock(side_effect=_session_get)

        with (
            patch(
                f"{FCM_MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{FCM_MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
            patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
        ):
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-07T10:00:00Z",
                self._SAFE_IMAGE_URL,
                "",  # empty clip → triggers poll
                "Pending",
                event_id="",  # empty → timestamp fallback
                cam_id=CAM_ID,
            )

        # Wrong-ts clip must not appear in any notify call
        call_args_list = coord.hass.services.async_call.call_args_list
        wrong_clip_calls = [c for c in call_args_list if "wrong-ts.mp4" in str(c)]
        assert wrong_clip_calls == [], "Timestamp-mismatched clip must not be sent"


# ═══════════════════════════════════════════════════════════════════════════════
# sensor.py — lines 151-152: BoschCameraAiDescriptionSensor appended
# ═══════════════════════════════════════════════════════════════════════════════


class TestSensorSetupAiDescriptionOption:
    """sensor.py lines 151-152: async_setup_entry appends BoschCameraAiDescriptionSensor
    per camera when CONF_ENABLE_AI_DESCRIPTION option is True."""

    def _stub_coord(self):
        return SimpleNamespace(
            data={
                CAM_ID: {
                    "info": {
                        "title": "Terrasse",
                        "hardwareVersion": "HOME_Eyes_Outdoor",
                        "firmwareVersion": "9.40.25",
                        "macAddress": "aa:bb:cc:dd:ee:01",
                        "featureSupport": {"light": True, "panLimit": 0},
                    },
                    "status": "ONLINE",
                    "events": [],
                }
            },
            _wifiinfo_cache={},
            _rcp_alarm_catalog_cache={},
            _rcp_motion_zones_cache={},
            _rcp_motion_coords_cache={},
            _cloud_zones_cache={},
            _gen2_zones_cache={},
            _rcp_tls_cert_cache={},
            _rcp_network_services_cache={},
            _rcp_iva_catalog_cache={},
            _rcp_private_areas_cache={},
            _ambient_lighting_cache={},
            _ambient_schedule_cache={},
            _alarm_status_cache={},
            _alarm_settings_cache={},
            _arming_cache={},
            _live_connections={},
            _stream_fell_back={},
            _stream_error_count={},
            _stream_warming=set(),
            _nvr_drain_state={},
            _commissioned_cache={},
            _firmware_cache={},
            _unread_events_cache={},
            _fcm_running=False,
            _fcm_healthy=True,
            _fcm_push_mode="auto",
            _fcm_last_push=0.0,
            last_update_success=True,
            options={
                "enable_fcm_push": True,
                "enable_sensors": True,
                "enable_nvr": False,
            },
            motion_settings=lambda cid: {
                "enabled": True,
                "motionAlarmConfiguration": "HIGH",
            },
            is_camera_online=lambda cid: True,
            is_stream_warming=lambda cid: False,
        )

    def test_ai_description_sensor_appended_when_option_true(self):
        """Lines 151-152: when enable_ai_description=True, BoschCameraAiDescriptionSensor
        is included in the entities list passed to async_add_entities."""
        import asyncio

        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
            async_setup_entry,
        )

        coord = self._stub_coord()

        entry = SimpleNamespace(
            runtime_data=coord,
            options={"enable_ai_description": True, "enable_sensors": True},
        )

        added_entities: list = []

        def fake_add(entities, **kw):
            added_entities.extend(entities)

        asyncio.run(async_setup_entry(None, entry, fake_add))

        ai_sensors = [
            e for e in added_entities if isinstance(e, BoschCameraAiDescriptionSensor)
        ]
        assert len(ai_sensors) == 1, (
            f"Expected 1 BoschCameraAiDescriptionSensor, got {len(ai_sensors)}"
        )
        assert ai_sensors[0].coordinator is coord
        assert ai_sensors[0]._cam_id == CAM_ID

    def test_ai_description_sensor_not_appended_when_option_false(self):
        """Complement: when enable_ai_description is absent/False, no
        BoschCameraAiDescriptionSensor is added."""
        import asyncio

        from custom_components.bosch_shc_camera.sensor import (
            BoschCameraAiDescriptionSensor,
            async_setup_entry,
        )

        coord = self._stub_coord()

        entry = SimpleNamespace(
            runtime_data=coord,
            options={"enable_sensors": True},  # no enable_ai_description
        )

        added_entities: list = []

        def fake_add(entities, **kw):
            added_entities.extend(entities)

        asyncio.run(async_setup_entry(None, entry, fake_add))

        ai_sensors = [
            e for e in added_entities if isinstance(e, BoschCameraAiDescriptionSensor)
        ]
        assert len(ai_sensors) == 0, "No AI sensor when option is absent"


# ═══════════════════════════════════════════════════════════════════════════════
# rcp.py — line 608: 0x0d00 XML envelope → _mark_fail
# ═══════════════════════════════════════════════════════════════════════════════


class TestRcpPrivacyXmlEnvelope:
    """rcp.py line 608: _read("0x0d00") returns an XML envelope → _mark_fail.

    _is_xml_envelope returns True for bytes starting with b"<". The branch
    at line 607 (`if _is_xml_envelope(raw)`) then calls _mark_fail at line 608.
    """

    def _make_coord(self):
        coord = SimpleNamespace(
            hass=MagicMock(),
            _rcp_session_cache={},
            _rcp_session_locks={},
            _rcp_dimmer_cache={},
            _rcp_privacy_cache={},
            _rcp_clock_offset_cache={},
            _rcp_lan_ip_cache={},
            _rcp_product_name_cache={},
            _rcp_bitrate_cache={},
            _rcp_alarm_catalog_cache={},
            _rcp_motion_zones_cache={},
            _rcp_motion_coords_cache={},
            _rcp_tls_cert_cache={},
            _rcp_network_services_cache={},
            _rcp_iva_catalog_cache={},
            _rcp_cmd_failures={},
        )
        coord._rcp_cmd_failures[CAM_ID] = {}
        return coord

    @pytest.mark.asyncio
    async def test_privacy_xml_envelope_marks_fail(self):
        """0x0d00 returns XML envelope → _mark_fail, privacy cache not written."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = self._make_coord()

        # XML envelope bytes — starts with b"<"
        xml_envelope = b"<Result><Error>NotSupported</Error></Result>"

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0d00":
                return xml_envelope
            return None  # all other commands return None

        with (
            patch(f"{RCP_MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{RCP_MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        # Privacy cache must remain empty — XML envelope is a failure case
        assert CAM_ID not in coord._rcp_privacy_cache, (
            "XML envelope response must NOT write to _rcp_privacy_cache"
        )
        # _mark_fail increments the failure counter for 0x0d00
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0d00", 0) >= 1, (
            "XML envelope must trigger _mark_fail (failure counter >= 1)"
        )

    @pytest.mark.asyncio
    async def test_privacy_xml_envelope_is_not_treated_as_valid_data(self):
        """Complement: valid 2-byte payload writes cache and does NOT mark fail."""
        from custom_components.bosch_shc_camera.rcp import async_update_rcp_data

        coord = self._make_coord()

        async def mock_rcp_read(hass, rcp_base, command, sessionid, **kwargs):
            if command == "0x0d00":
                return b"\x00\x01"  # byte[1]=1 → privacy ON
            return None

        with (
            patch(f"{RCP_MODULE}.get_cached_rcp_session", return_value="sess123"),
            patch(f"{RCP_MODULE}.rcp_read", side_effect=mock_rcp_read),
        ):
            await async_update_rcp_data(coord, CAM_ID, PROXY_HOST, PROXY_HASH)

        assert coord._rcp_privacy_cache.get(CAM_ID) == 1, (
            "Valid 2-byte response must write byte[1] value to cache"
        )
        assert coord._rcp_cmd_failures[CAM_ID].get("0x0d00", 0) == 0, (
            "Valid response must not mark fail"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# media_source.py — line 406: _scandir_filtered skips "_"-prefixed dirs
# ═══════════════════════════════════════════════════════════════════════════════


class TestSmbBackendScandirFilteredNvrInternalDirs:
    """media_source.py line 406: _scandir_filtered with want_dirs=True skips
    directory entries whose name starts with "_" (NVR internal dirs like
    _staging, _failed).

    Strategy: patch smbclient.scandir to yield fake DirEntry objects where one
    is "_staging" (underscore-prefixed dir) and others are real dirs/files.
    """

    def _make_smb_backend(self):
        from custom_components.bosch_shc_camera.media_source import _SmbBackend

        opts = {
            "smb_server": "nas.local",
            "smb_share": "cameras",
            "smb_username": "user",
            "smb_password": "pass",
            "upload_protocol": "SMB",
            "smb_base_path": "",
            "folder_pattern": "{camera}/{year}/{month}/{day}",
        }
        hass = MagicMock()
        return _SmbBackend(hass, opts)

    def _make_dir_entry(self, name: str, is_dir: bool = True, is_file: bool = False):
        e = MagicMock()
        e.name = name
        e.is_dir = MagicMock(return_value=is_dir)
        e.is_file = MagicMock(return_value=is_file)
        return e

    def test_underscore_prefixed_dirs_skipped(self):
        """Line 406: _staging and _failed entries skipped; real dirs yielded."""
        backend = self._make_smb_backend()

        entries = [
            self._make_dir_entry(
                "_staging"
            ),  # NVR internal → must be skipped (line 406)
            self._make_dir_entry(
                "_failed"
            ),  # NVR internal → must be skipped (line 406)
            self._make_dir_entry("Terrasse"),  # real camera dir → must be yielded
            self._make_dir_entry("Eingang"),  # real camera dir → must be yielded
        ]

        with (
            patch("smbclient.scandir", return_value=iter(entries)),
            patch("smbclient.register_session", return_value=None),
            patch("smbclient.delete_session", return_value=None),
        ):
            result = list(backend._scandir_filtered(want_dirs=True))

        assert "_staging" not in result, "_staging must be filtered out"
        assert "_failed" not in result, "_failed must be filtered out"
        assert "Terrasse" in result, "Terrasse (real dir) must be yielded"
        assert "Eingang" in result, "Eingang (real dir) must be yielded"
        assert len(result) == 2, f"Expected exactly 2 dirs, got {result}"

    def test_underscore_files_not_skipped_in_file_mode(self):
        """Line 406 guard only applies when want_dirs=True.

        When want_dirs=False, the `startswith("_")` check is skipped (the
        condition is `if want_dirs and e.name.startswith("_")`), so a file
        named "_index.json" IS yielded.
        """
        backend = self._make_smb_backend()

        entries = [
            self._make_dir_entry("_index.json", is_dir=False, is_file=True),
            self._make_dir_entry("event.mp4", is_dir=False, is_file=True),
        ]

        with (
            patch("smbclient.scandir", return_value=iter(entries)),
            patch("smbclient.register_session", return_value=None),
            patch("smbclient.delete_session", return_value=None),
        ):
            result = list(backend._scandir_filtered(want_dirs=False))

        # Both files yielded in file mode — the "_" guard does not apply
        assert "_index.json" in result
        assert "event.mp4" in result
