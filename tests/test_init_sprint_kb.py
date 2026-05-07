"""Sprint KB: targeted coverage for _async_update_data inner closures.

Targets (lines 1454-1625 of __init__.py):
  - _check_status: TCP ping hit → ONLINE, cloud call skipped (lines 1466-1510)
  - _check_status: TCP ping + REMOTE-fallback clear (lines 1482-1510)
  - _check_status: TCP miss → cloud /ping 200 ONLINE (lines 1516-1528)
  - _check_status: TCP miss → /ping non-200 → /commissioned fallback (lines 1534-1554)
  - _check_status: OFFLINE tracking + recovery clear (lines 1555-1568)
  - _check_status: exception returned by gather → method survives (lines 1569-1571)
  - _fetch_events: last-event same ID → skip full fetch (lines 1582-1600)
  - _fetch_events: last-event different ID → full /events fetch (lines 1600-1609)
  - _fetch_events: TimeoutError swallowed, empty events returned (line ~1609)
  - do_events=False → /events endpoint never called (line 1611)

Strategy: call BoschCameraCoordinator._async_update_data(coord) as an unbound
method on a SimpleNamespace stub. The session mock is routed by URL substring.
Setting _feature_flags and _protocol_checked=True skips FF + protocol fetches.
Setting _last_slow to time.monotonic() (recent) skips slow-tier.
Setting _first_tick_done avoids the fast-first-tick do_events/do_slow override.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator


CAM_A = "CAM-A"


# ── session builder ───────────────────────────────────────────────────────────


def _make_resp(status: int, json_val=None, text_val: str = ""):
    """Return a context-manager-compatible response mock."""
    r = MagicMock()
    r.status = status
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=None)
    r.json = AsyncMock(return_value=json_val if json_val is not None else {})
    r.text = AsyncMock(return_value=text_val)
    return r


def _url_session(url_map: dict):
    """Build an aiohttp session mock that routes GET by URL.

    Special key "__cam_list__" matches the camera-list endpoint
    (URL ends with "/video_inputs" — no per-cam suffix).

    All other keys are substring patterns; longer patterns win (most-specific
    first) to avoid /v11/video_inputs matching per-camera sub-paths.

    Unmatched URLs return 200 with an empty JSON list.
    """
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)

    cam_list_resp = url_map.get("__cam_list__")
    # Remove the sentinel from the pattern dict before building the match list.
    pattern_items = [
        (k, v) for k, v in url_map.items() if k != "__cam_list__"
    ]
    # Longer patterns take priority over shorter ones.
    _sorted_patterns = sorted(pattern_items, key=lambda kv: len(kv[0]), reverse=True)

    def _get(url, **kwargs):
        # Strip query string for endpoint matching.
        path = url.split("?")[0]
        # Camera-list endpoint: ends exactly on /video_inputs (no more segments).
        if cam_list_resp is not None and path.endswith("/video_inputs"):
            return cam_list_resp
        for pattern, resp in _sorted_patterns:
            if pattern in url:
                return resp
        # default: 200 empty list
        return _make_resp(200, json_val=[], text_val="")

    session.get = _get
    session.put = AsyncMock()
    return session


# ── coordinator stub ──────────────────────────────────────────────────────────


def _make_coord(**overrides):
    """Full coordinator stub for _async_update_data.

    Sets every attribute the method may touch.  Tests override only what
    they need.  _should_check_status is mocked to True so status runs every
    time; tests that want the real logic pass it explicitly.
    """
    def _create_task(coro):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        # ── entry & auth ─────────────────────────────────────────────────────
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
            entry_id="01KM38DHZ525S61HPENAT7NHC0",
        ),
        _refreshed_token=None,
        _refreshed_refresh=None,
        # token + refresh_token are @property on the real class; SimpleNamespace
        # needs them as plain attributes so self.token / self.refresh_token work.
        token="tok-A",
        refresh_token="rfr-B",
        # options is also a @property (returns get_options(self._entry)); mirror it.
        options={},

        # ── feature flags + protocol: already done, skip both ────────────────
        _feature_flags={"dummy": True},
        _protocol_checked=True,

        # ── FCM state ────────────────────────────────────────────────────────
        _fcm_lock=threading.Lock(),
        _fcm_running=False,
        _fcm_healthy=True,
        _fcm_client=None,

        # ── timing (stale → run status+events; recent _last_slow → skip slow) ─
        _last_status=float('-inf'),
        _last_events=float('-inf'),
        _last_slow=time.monotonic(),   # recent → skip slow tier
        _last_smb_cleanup=time.monotonic(),
        _last_nvr_cleanup=time.monotonic(),
        _last_smb_disk_check=time.monotonic(),

        # ── camera caches ────────────────────────────────────────────────────
        _hw_version={},
        _cached_status={},
        _cached_events={},
        _commissioned_cache={},
        _offline_since={},
        _per_cam_status_at={},
        _stream_fell_back={},
        _stream_error_count={},
        _stream_error_at={},
        _live_connections={},
        _local_promote_at={},
        _lan_tcp_reachable={},
        _rcp_lan_ip_cache={},
        _local_creds_cache={},
        _shc_state_cache={},
        _wifiinfo_cache={},
        _last_event_ids={},
        _event_dedup_cache={},
        _alert_sent_ids={},
        _pan_cache={},
        _lighting_switch_cache={},

        # ── write-lock timestamps ────────────────────────────────────────────
        _privacy_set_at={},
        _light_set_at={},
        _notif_set_at={},
        _audio_alarm_set_at={},
        _privacy_sound_set_at={},
        _timestamp_set_at={},
        _ledlights_set_at={},

        # ── misc flags ───────────────────────────────────────────────────────
        _integration_version="11.0.10",
        _OFFLINE_EXTENDED_INTERVAL=900,
        _WRITE_LOCK_SECS=30.0,
        shc_ready=False,

        # ── stream / connection ──────────────────────────────────────────────
        _camera_entities={},
        _stream_locks={},
        _tls_proxy_ports={},
        _audio_enabled={},
        _session_stale={},
        _renewal_tasks={},
        _bg_tasks=set(),
        _nvr_processes={},
        _nvr_user_intent={},
        _rcp_session_cache={},

        # ── mocked collaborators ─────────────────────────────────────────────
        _ensure_valid_token=AsyncMock(return_value="fresh-tok"),
        _async_update_shc_states=AsyncMock(),
        _async_update_rcp_data=AsyncMock(),
        _async_update_rcp_data_for_cam=AsyncMock(),
        async_mark_events_read=AsyncMock(),
        _is_write_locked=MagicMock(return_value=False),
        _cleanup_stale_devices=MagicMock(),
        _tear_down_live_stream=AsyncMock(),
        _promote_to_local=AsyncMock(),
        _async_send_alert=AsyncMock(),
        _async_local_tcp_ping=AsyncMock(return_value=False),  # override per test
        # _should_check_status mocked → always says "yes, run status now"
        _should_check_status=MagicMock(return_value=True),
        get_model_config=lambda cid: SimpleNamespace(generation=2),

        # ── hass ─────────────────────────────────────────────────────────────
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_create_background_task=MagicMock(side_effect=_create_task),
            async_add_executor_job=AsyncMock(),
            bus=SimpleNamespace(async_fire=MagicMock()),
            data={},
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp"),
            config_entries=SimpleNamespace(async_reload=AsyncMock()),
        ),
    )
    base.update(overrides)
    ns = SimpleNamespace(**base)
    # Simulate second tick so do_events / do_slow are NOT force-disabled.
    ns._first_tick_done = True
    return ns


# ── cam_list used in every test ───────────────────────────────────────────────

CAM_LIST = [{"id": CAM_A, "hardwareVersion": "HOME_Eyes_Outdoor"}]


# ── helper: build cam_list response + minimal video_inputs response ───────────

def _base_url_map(**extras):
    """Return url_map for _url_session with camera-list returning CAM_LIST.

    Uses "video_inputs_list_endpoint" as a sentinel key that never appears
    in real URLs; _url_session handles this via the special __cam_list__ slot.
    Per-cam URLs always contain a UUID segment after /video_inputs/, so we can
    distinguish the list endpoint (URL ends exactly on /video_inputs) from
    per-cam endpoints via the routing function.
    """
    m = {"__cam_list__": _make_resp(200, json_val=CAM_LIST)}
    m.update(extras)
    return m


# ── Tests: _check_status ─────────────────────────────────────────────────────


class TestCheckStatusTcpHit:
    """TCP ping returns True → camera declared ONLINE without cloud call.

    Lines 1466-1510.
    """

    @pytest.mark.asyncio
    async def test_tcp_hit_sets_per_cam_status_at(self):
        """TCP ping → _per_cam_status_at[CAM_A] is set to a recent timestamp."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=True))
        ping_resp = _make_resp(200, text_val='"ONLINE"')
        session = _url_session(_base_url_map(**{"/ping": ping_resp}))

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A in coord._per_cam_status_at, (
            "_per_cam_status_at must be populated for a TCP-reachable camera"
        )
        assert coord._per_cam_status_at[CAM_A] > 0.0, (
            "timestamp must be a positive monotonic value"
        )

    @pytest.mark.asyncio
    async def test_tcp_hit_no_offline_entry(self):
        """TCP ping hit → CAM_A must not appear in _offline_since."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=True))
        # Pre-populate to ensure it is cleared
        coord._offline_since[CAM_A] = time.monotonic() - 60
        session = _url_session(_base_url_map())

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A not in coord._offline_since, (
            "_offline_since must be cleared when TCP ping succeeds"
        )

    @pytest.mark.asyncio
    async def test_tcp_hit_cloud_ping_not_called(self):
        """TCP ping hit → cloud /ping endpoint is NOT called."""
        cloud_ping_resp = _make_resp(200, text_val='"ONLINE"')
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=True))
        session = _url_session(_base_url_map(**{"/ping": cloud_ping_resp}))

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        # cloud_ping_resp.__aenter__ called means cloud /ping was reached.
        # For the video_inputs route it is called; for the per-cam /ping it must NOT be.
        # We distinguish by checking the /ping mock context was NOT entered for CAM_A.
        # Since our session routes "/ping" to cloud_ping_resp, and TCP took the early
        # return, cloud_ping_resp.__aenter__ must not have been awaited.
        cloud_ping_resp.__aenter__.assert_not_called(), (
            "Cloud /ping endpoint must not be called when local TCP ping succeeds"
        )

    @pytest.mark.asyncio
    async def test_cached_status_is_online_after_tcp_hit(self):
        """TCP hit → _cached_status[CAM_A] == 'ONLINE'."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=True))
        session = _url_session(_base_url_map())

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_status.get(CAM_A) == "ONLINE", (
            "Cached status must be ONLINE after successful TCP ping"
        )


class TestCheckStatusRemoteFallbackClear:
    """TCP ping + camera was on REMOTE fallback → fallback cleared.

    Lines 1482-1510.
    """

    @pytest.mark.asyncio
    async def test_remote_fallback_cleared_on_lan_recovery(self):
        """_stream_fell_back[CAM_A] and error count cleared when LAN reachable again."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _stream_fell_back={CAM_A: True},
            _stream_error_count={CAM_A: 3},
            _stream_error_at={CAM_A: time.monotonic() - 10},
            _live_connections={},   # no active REMOTE stream → no promotion
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
                options={"stream_connection_type": "auto"},
                entry_id="01KM38DHZ525S61HPENAT7NHC0",
            ),
        )
        session = _url_session(_base_url_map())

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert not coord._stream_fell_back.get(CAM_A), (
            "_stream_fell_back must be cleared when LAN is reachable in auto mode"
        )
        assert CAM_A not in coord._stream_error_count, (
            "_stream_error_count must be cleared on LAN recovery"
        )

    @pytest.mark.asyncio
    async def test_active_remote_stream_schedules_promotion(self):
        """Active REMOTE stream on LAN-recovery schedules _promote_to_local task."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _stream_fell_back={CAM_A: True},
            _stream_error_count={CAM_A: 2},
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            _local_promote_at={},  # no recent promote → cooldown not active
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
                options={"stream_connection_type": "auto"},
                entry_id="01KM38DHZ525S61HPENAT7NHC0",
            ),
        )
        session = _url_session(_base_url_map())

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord.hass.async_create_task.called, (
            "async_create_task must be called to schedule LOCAL promotion"
        )

    @pytest.mark.asyncio
    async def test_non_auto_mode_no_fallback_clear(self):
        """stream_connection_type=local_only → fallback flag NOT cleared by status tick."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _stream_fell_back={CAM_A: True},
            _stream_error_count={CAM_A: 2},
            _live_connections={},
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
                options={"stream_connection_type": "local_only"},
                entry_id="01KM38DHZ525S61HPENAT7NHC0",
            ),
        )
        session = _url_session(_base_url_map())

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        # In local_only mode the inner `opts.get("stream_connection_type") == "auto"`
        # check is False, so the clear block is skipped.
        assert coord._stream_fell_back.get(CAM_A), (
            "Fallback flag must not be cleared in non-auto stream mode"
        )


class TestCheckStatusCloudPing:
    """TCP miss → cloud /ping called and result used.

    Lines 1516-1534.
    """

    @pytest.mark.asyncio
    async def test_cloud_ping_200_online(self):
        """TCP miss + /ping 200 'ONLINE' → status ONLINE, no offline entry."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        ping_resp = _make_resp(200, text_val='"ONLINE"')
        session = _url_session(_base_url_map(**{f"/{CAM_A}/ping": ping_resp}))

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_status.get(CAM_A) == "ONLINE", (
            "Cloud /ping 200 ONLINE must propagate to _cached_status"
        )
        assert CAM_A not in coord._offline_since, (
            "ONLINE camera must not appear in _offline_since"
        )

    @pytest.mark.asyncio
    async def test_cloud_ping_200_updating_status(self):
        """TCP miss + /ping 200 'UPDATING…' → status UPDATING."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        ping_resp = _make_resp(200, text_val='"UPDATING_FIRMWARE"')
        session = _url_session(_base_url_map(**{f"/{CAM_A}/ping": ping_resp}))

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_status.get(CAM_A) == "UPDATING", (
            "UPDATING firmware status must be mapped to 'UPDATING'"
        )

    @pytest.mark.asyncio
    async def test_cloud_ping_444_is_offline(self):
        """TCP miss + /ping 444 → status OFFLINE, _offline_since set."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        ping_resp = _make_resp(444)
        session = _url_session(_base_url_map(**{f"/{CAM_A}/ping": ping_resp}))

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_status.get(CAM_A) == "OFFLINE", (
            "HTTP 444 from /ping must map to OFFLINE"
        )
        assert CAM_A in coord._offline_since, (
            "OFFLINE camera must be tracked in _offline_since"
        )


class TestCheckStatusCommissionedFallback:
    """TCP miss + /ping non-200 → /commissioned fallback used.

    Lines 1534-1554.
    """

    @pytest.mark.asyncio
    async def test_commissioned_connected_true_is_online(self):
        """Non-200 /ping → /commissioned {connected:true, commissioned:true} → ONLINE."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        ping_resp = _make_resp(404)
        comm_resp = _make_resp(200, json_val={"connected": True, "commissioned": True})
        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/ping": ping_resp, f"/{CAM_A}/commissioned": comm_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_status.get(CAM_A) == "ONLINE", (
            "commissioned={connected:True} must yield ONLINE status"
        )

    @pytest.mark.asyncio
    async def test_commissioned_configured_is_offline(self):
        """/commissioned {configured:true} but not connected → OFFLINE."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        ping_resp = _make_resp(500)
        comm_resp = _make_resp(200, json_val={"configured": True, "connected": False})
        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/ping": ping_resp, f"/{CAM_A}/commissioned": comm_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_status.get(CAM_A) == "OFFLINE", (
            "configured-but-not-connected must map to OFFLINE"
        )

    @pytest.mark.asyncio
    async def test_commissioned_444_is_offline(self):
        """/commissioned 444 → OFFLINE."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        ping_resp = _make_resp(503)
        comm_resp = _make_resp(444)
        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/ping": ping_resp, f"/{CAM_A}/commissioned": comm_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_status.get(CAM_A) == "OFFLINE", (
            "HTTP 444 from /commissioned must also map to OFFLINE"
        )

    @pytest.mark.asyncio
    async def test_commissioned_populated_in_cache(self):
        """When /commissioned returns 200, _commissioned_cache is populated."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        comm_data = {"connected": True, "commissioned": True}
        ping_resp = _make_resp(404)
        comm_resp = _make_resp(200, json_val=comm_data)
        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/ping": ping_resp, f"/{CAM_A}/commissioned": comm_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._commissioned_cache.get(CAM_A) == comm_data, (
            "_commissioned_cache must store the JSON body from /commissioned"
        )


class TestCheckStatusOfflineTracking:
    """Offline tracking: set on first miss, cleared on recovery.

    Lines 1555-1568.
    """

    @pytest.mark.asyncio
    async def test_offline_since_set_when_offline(self):
        """OFFLINE status → _offline_since[CAM_A] populated."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))
        ping_resp = _make_resp(444)
        session = _url_session(_base_url_map(**{f"/{CAM_A}/ping": ping_resp}))

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A in coord._offline_since, (
            "_offline_since must be populated when status is OFFLINE"
        )

    @pytest.mark.asyncio
    async def test_offline_since_not_overwritten_when_already_set(self):
        """Second OFFLINE result must NOT reset _offline_since timestamp."""
        original_ts = time.monotonic() - 500
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=False),
            _offline_since={CAM_A: original_ts},
        )
        ping_resp = _make_resp(444)
        session = _url_session(_base_url_map(**{f"/{CAM_A}/ping": ping_resp}))

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._offline_since[CAM_A] == original_ts, (
            "Existing _offline_since timestamp must not be overwritten on repeated OFFLINE"
        )

    @pytest.mark.asyncio
    async def test_offline_since_cleared_on_recovery(self):
        """Camera recovers to ONLINE (TCP ping) → _offline_since entry removed."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _offline_since={CAM_A: time.monotonic() - 120},
        )
        session = _url_session(_base_url_map())

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert CAM_A not in coord._offline_since, (
            "_offline_since must be cleared when camera recovers to ONLINE"
        )


class TestCheckStatusExceptionHandling:
    """Exception from _check_status returned by gather → method does not crash.

    Lines 1569-1571.
    """

    @pytest.mark.asyncio
    async def test_client_error_does_not_crash_update(self):
        """aiohttp.ClientError inside _check_status → gather catches it, update returns."""
        import aiohttp

        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))

        # /ping raises ClientError; /commissioned also raises so we fall all the way through
        bad_resp = MagicMock()
        bad_resp.status = 200
        bad_resp.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("boom"))
        bad_resp.__aexit__ = AsyncMock(return_value=None)

        session = _url_session(
            _base_url_map(**{f"/{CAM_A}/ping": bad_resp, f"/{CAM_A}/commissioned": bad_resp})
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            # Must complete without raising
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "_async_update_data must return a dict even when _check_status raises"
        )

    @pytest.mark.asyncio
    async def test_timeout_in_check_status_does_not_crash(self):
        """asyncio.TimeoutError inside _check_status → swallowed, update returns."""
        coord = _make_coord(_async_local_tcp_ping=AsyncMock(return_value=False))

        timeout_resp = MagicMock()
        timeout_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        timeout_resp.__aexit__ = AsyncMock(return_value=None)

        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/ping": timeout_resp, f"/{CAM_A}/commissioned": timeout_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "_async_update_data must return a dict even when _check_status times out"
        )


# ── Tests: _fetch_events ─────────────────────────────────────────────────────


class TestFetchEventsLastEventOptimization:
    """last_event same ID → full /events fetch skipped.

    Lines 1582-1600.
    """

    @pytest.mark.asyncio
    async def test_same_event_id_skips_full_fetch(self):
        """When /last_event returns a known ID, /events must NOT be called."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _last_event_ids={CAM_A: "ev-known"},
            _cached_events={CAM_A: [{"id": "ev-known"}]},
        )
        last_ev_resp = _make_resp(200, json_val={"id": "ev-known"})
        events_resp = _make_resp(200, json_val=[{"id": "ev-new"}])
        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/last_event": last_ev_resp, "/events?videoInputId": events_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        # If the optimization fires, _cached_events stays as the old list.
        assert events_resp.__aenter__.call_count == 0, (
            "Full /events endpoint must NOT be called when last_event ID is unchanged"
        )

    @pytest.mark.asyncio
    async def test_same_event_id_keeps_cached_events(self):
        """Skipped fetch → _cached_events[CAM_A] retains the pre-existing list."""
        old_events = [{"id": "ev-known", "eventType": "MOVEMENT"}]
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _last_event_ids={CAM_A: "ev-known"},
            _cached_events={CAM_A: old_events},
        )
        last_ev_resp = _make_resp(200, json_val={"id": "ev-known"})
        session = _url_session(
            _base_url_map(**{f"/{CAM_A}/last_event": last_ev_resp})
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_events.get(CAM_A) == old_events, (
            "Cached events must be preserved when last_event ID is unchanged"
        )


class TestFetchEventsFullFetch:
    """Different last-event ID → full /events fetch runs.

    Lines 1600-1609.
    """

    @pytest.mark.asyncio
    async def test_different_event_id_triggers_full_fetch(self):
        """New event ID → full /events fetch is called and _cached_events updated."""
        new_events = [{"id": "ev-new", "eventType": "MOVEMENT"}]
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _last_event_ids={CAM_A: "ev-old"},
            _cached_events={CAM_A: []},
        )
        last_ev_resp = _make_resp(200, json_val={"id": "ev-new"})
        events_resp = _make_resp(200, json_val=new_events)
        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/last_event": last_ev_resp, "/events?videoInputId": events_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_events.get(CAM_A) == new_events, (
            "_cached_events must be replaced with the fresh /events response"
        )

    @pytest.mark.asyncio
    async def test_no_cached_event_id_triggers_full_fetch(self):
        """/last_event returns an ID we've never seen → full fetch runs."""
        new_events = [{"id": "ev-brand-new"}]
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _last_event_ids={},   # empty → no known ID
            _cached_events={},
        )
        last_ev_resp = _make_resp(200, json_val={"id": "ev-brand-new"})
        events_resp = _make_resp(200, json_val=new_events)
        session = _url_session(
            _base_url_map(
                **{f"/{CAM_A}/last_event": last_ev_resp, "/events?videoInputId": events_resp}
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert coord._cached_events.get(CAM_A) == new_events, (
            "First-seen event ID must trigger full /events fetch"
        )


class TestFetchEventsTimeoutSwallowed:
    """TimeoutError from /last_event → swallowed, empty events returned.

    Line ~1609.
    """

    @pytest.mark.asyncio
    async def test_last_event_timeout_does_not_crash(self):
        """asyncio.TimeoutError from /last_event → method returns successfully."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _last_event_ids={},
            _cached_events={},
        )
        timeout_resp = MagicMock()
        timeout_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        timeout_resp.__aexit__ = AsyncMock(return_value=None)

        # Also make the full /events endpoint time out so both paths fail gracefully.
        events_timeout = MagicMock()
        events_timeout.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        events_timeout.__aexit__ = AsyncMock(return_value=None)

        session = _url_session(
            _base_url_map(
                **{
                    f"/{CAM_A}/last_event": timeout_resp,
                    "/events?videoInputId": events_timeout,
                }
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            result = await BoschCameraCoordinator._async_update_data(coord)

        assert isinstance(result, dict), (
            "_async_update_data must return dict even when /last_event times out"
        )

    @pytest.mark.asyncio
    async def test_last_event_timeout_yields_empty_events(self):
        """TimeoutError from /last_event → _cached_events[CAM_A] is empty list."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _last_event_ids={},
            _cached_events={},
        )
        timeout_resp = MagicMock()
        timeout_resp.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        timeout_resp.__aexit__ = AsyncMock(return_value=None)

        events_timeout = MagicMock()
        events_timeout.__aenter__ = AsyncMock(side_effect=asyncio.TimeoutError())
        events_timeout.__aexit__ = AsyncMock(return_value=None)

        session = _url_session(
            _base_url_map(
                **{
                    f"/{CAM_A}/last_event": timeout_resp,
                    "/events?videoInputId": events_timeout,
                }
            )
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        # When both requests fail, _cached_events may be [] or absent — either is fine.
        events_for_cam = coord._cached_events.get(CAM_A, [])
        assert events_for_cam == [], (
            "Timeout on both /last_event and /events must leave an empty events list"
        )


class TestDoEventsFalse:
    """do_events=False → /events endpoint never called.

    Line 1611.
    """

    @pytest.mark.asyncio
    async def test_no_events_fetch_when_interval_not_elapsed(self):
        """When _last_events is recent, /events endpoint is never called."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            # Set _last_events to "now" so event interval has not elapsed.
            _last_events=time.monotonic(),
        )
        events_resp = _make_resp(200, json_val=[{"id": "should-not-appear"}])
        session = _url_session(
            _base_url_map(**{"/events?videoInputId": events_resp})
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert events_resp.__aenter__.call_count == 0, (
            "/events endpoint must not be called when event interval has not elapsed"
        )

    @pytest.mark.asyncio
    async def test_no_last_event_check_when_do_events_false(self):
        """do_events=False → /last_event endpoint also never called."""
        coord = _make_coord(
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _last_events=time.monotonic(),
        )
        last_ev_resp = _make_resp(200, json_val={"id": "ev-never"})
        session = _url_session(
            _base_url_map(**{f"/{CAM_A}/last_event": last_ev_resp})
        )

        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            await BoschCameraCoordinator._async_update_data(coord)

        assert last_ev_resp.__aenter__.call_count == 0, (
            "/last_event endpoint must not be called when do_events is False"
        )
