"""Sprint KD tests — targeting _try_live_connection_inner in __init__.py.

Coverage targets (lines 2313-2808):
  - Group 1: No-token guard (lines 2316-2319)
  - Group 2: Connection type preference / candidate selection (lines 2341-2393)
  - Group 3: TCP pre-check (lines 2401-2425)
  - Group 4: PUT 200 LOCAL success path (lines 2465-2695)
  - Group 5: PUT 200 REMOTE success path (lines 2508-2563)
  - Group 6: Error paths — 401, 404/non-success, TimeoutError, ClientError,
             session.close() finally block (lines 2790-2808)

All tests run without a live HA instance.  Coordinator is a SimpleNamespace stub;
the method is called via the unbound pattern:
    BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)
"""
from __future__ import annotations

import asyncio
import json
import time as time_mod
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _noop_coro():
    """No-op coroutine (replacement for removed asyncio.coroutine decorator)."""


def _model_cfg(**overrides):
    """Return a SimpleNamespace that mimics a ModelConfig dataclass."""
    base = dict(
        max_stream_errors=3,
        min_wifi_for_local=50,
        max_session_duration=3600,
        generation=2,
        display_name="Eyes Außenkamera II",
        pre_warm_delay=0,        # zero so tests don't sleep
        pre_warm_retries=2,
        pre_warm_retry_wait=1,
        post_warm_buffer=0,
        describe_timeout=5,
        min_total_wait=0,        # zero so tests don't sleep
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_coord(**overrides):
    """Minimal coordinator stub for _try_live_connection_inner."""
    task_mock = MagicMock()
    task_mock.done = MagicMock(return_value=True)

    def _create_task(coro, **kwargs):
        """Consume the coroutine so 'never awaited' warnings don't fire."""
        import inspect
        if inspect.iscoroutine(coro):
            coro.close()
        return task_mock

    base = dict(
        # Token property — the method reads self.token directly
        token="tok-A",
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={"stream_connection_type": "auto"},
        ),
        # Stream-type override (None = use options)
        _stream_type_override=None,
        # Stream-error tracking
        _stream_error_count={},
        _stream_error_at={},
        _stream_fell_back={},
        # Local promotion/TCP cache
        _local_promote_at={},
        _lan_tcp_reachable={},
        _rcp_lan_ip_cache={CAM_A: "192.168.1.1"},  # needed for TCP pre-check path
        # Cred/proxy caches
        _local_creds_cache={},
        _tls_proxy_ports={},
        # HW version
        _hw_version={CAM_A: "HOME_Eyes_Outdoor"},
        # WiFi info
        _wifiinfo_cache={},
        # Audio
        _audio_enabled={},
        # Live connection tracking
        _live_connections={},
        _live_opened_at={},
        # Stream warming
        _stream_warming=set(),
        _stream_warming_started={},
        # Camera entities (for stream stop/update)
        _camera_entities={},
        # Background task tracking
        _bg_tasks=set(),
        # Renewal task tracking
        _renewal_tasks={},
        _auto_renew_generation={},
        # NVR
        _nvr_user_intent={},
        _nvr_processes={},
        # Collaborator mocks
        _async_local_tcp_ping=AsyncMock(return_value=False),
        _start_tls_proxy=AsyncMock(return_value=12345),
        _stop_tls_proxy=AsyncMock(),
        _register_go2rtc_stream=AsyncMock(),
        _check_and_recover_webrtc=AsyncMock(),
        # These return coroutines passed to hass.async_create_task — use AsyncMock so
        # the coroutine is only created when called (avoids "never awaited" warnings).
        _auto_renew_local_session=AsyncMock(return_value=None),
        _remote_session_terminator=AsyncMock(return_value=None),
        _refresh_rcp_state=AsyncMock(return_value=None),
        async_request_refresh=AsyncMock(return_value=None),
        get_quality_params=MagicMock(return_value=(True, 1)),
        get_quality=MagicMock(return_value="auto"),
        get_model_config=MagicMock(return_value=_model_cfg()),
        # Used by _replace_renewal_task
        hass=SimpleNamespace(
            async_create_task=_create_task,
            async_add_executor_job=AsyncMock(),
            data={},
        ),
        debug=False,
        _get_cam_lan_ip=MagicMock(return_value="192.168.1.1"),
    )
    base.update(overrides)
    coord = SimpleNamespace(**base)
    # Wire _replace_renewal_task to use hass.async_create_task
    def _replace_renewal_task(cam_id, coro):
        t = coord.hass.async_create_task(coro)
        coord._renewal_tasks[cam_id] = t
        return t
    coord._replace_renewal_task = _replace_renewal_task
    return coord


def _put_resp(status: int, body: str):
    """Build a fake response for session.put(...)."""
    r = MagicMock()
    r.status = status
    r.text = AsyncMock(return_value=body)
    return r


def _make_session(put_response):
    """Build a minimal aiohttp session mock for _try_live_connection_inner."""
    session_mock = MagicMock()
    session_mock.close = AsyncMock()
    session_mock.put = AsyncMock(return_value=put_response)
    return session_mock


# ── Group 1: No-token guard ────────────────────────────────────────────────────


class TestNoTokenGuard:
    """Lines 2316-2319: if not token → return None immediately."""

    @pytest.mark.asyncio
    async def test_no_token_returns_none(self):
        """coord.token = None → returns None without creating a session."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(token=None)

        session_created = []
        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", side_effect=lambda **kw: session_created.append(1) or MagicMock()):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is None
        # Session must NOT be created when token is missing
        assert len(session_created) == 0

    @pytest.mark.asyncio
    async def test_empty_string_token_returns_none(self):
        """coord.token = '' → falsy → returns None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(token="")

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=MagicMock()):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is None


# ── Group 2: Connection type preference ────────────────────────────────────────


class TestConnectionTypePref:
    """Lines 2341-2393: candidate selection based on options / error counters."""

    @pytest.mark.asyncio
    async def test_local_pref_puts_local_only(self):
        """options stream_connection_type='local' → candidates=['LOCAL'] → PUT type=LOCAL."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            # No LAN IP → TCP pre-check skipped when only LOCAL candidate
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
        )
        coord._get_cam_lan_ip = MagicMock(return_value=None)

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        call_kwargs = session_mock.put.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs["json"]["type"] == "LOCAL"

    @pytest.mark.asyncio
    async def test_remote_pref_puts_remote_only(self):
        """options stream_connection_type='remote' → candidates=['REMOTE'] → PUT type=REMOTE."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        call_kwargs = session_mock.put.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs["json"]["type"] == "REMOTE"

    @pytest.mark.asyncio
    async def test_auto_high_error_count_uses_remote_only(self):
        """AUTO mode, error_count >= max_stream_errors → candidates=['REMOTE'], _stream_fell_back=True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={CAM_A: 3},  # >= max_stream_errors=3
            _stream_error_at={CAM_A: time_mod.monotonic() - 10},  # recent, not aged out
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        call_kwargs = session_mock.put.call_args
        assert call_kwargs is not None
        assert call_kwargs.kwargs["json"]["type"] == "REMOTE"
        assert coord._stream_fell_back.get(CAM_A) is True

    @pytest.mark.asyncio
    async def test_auto_no_errors_local_first(self):
        """AUTO mode, error_count=0, good WiFi → candidates=['LOCAL','REMOTE'], LOCAL tried first."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},  # good WiFi
            # Make TCP pre-check pass so LOCAL stays first
            _async_local_tcp_ping=AsyncMock(return_value=True),
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # First PUT call must be LOCAL
        first_call = session_mock.put.call_args_list[0]
        assert first_call.kwargs["json"]["type"] == "LOCAL"

    @pytest.mark.asyncio
    async def test_auto_error_aged_out_resets_counter(self):
        """AUTO mode: error_at > TTL(300s) with LAN=ok → counter cleared, LOCAL attempted."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={CAM_A: 2},
            _stream_error_at={CAM_A: time_mod.monotonic() - 400},  # 400s ago > 300s TTL
            _stream_fell_back={CAM_A: True},
            # LAN=ok so TTL=300
            _lan_tcp_reachable={CAM_A: (True, time_mod.monotonic())},
            _async_local_tcp_ping=AsyncMock(return_value=True),
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # After aged-out decay the counter must be cleared
        assert CAM_A not in coord._stream_error_count
        assert CAM_A not in coord._stream_error_at

        # And LOCAL must be the first candidate attempted
        first_call = session_mock.put.call_args_list[0]
        assert first_call.kwargs["json"]["type"] == "LOCAL"

    @pytest.mark.asyncio
    async def test_auto_weak_wifi_prefers_remote(self):
        """AUTO mode, WiFi < threshold → candidates=['REMOTE','LOCAL'], REMOTE tried first."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 30}},  # < min_wifi_for_local=50
        )

        # Make both attempts return 404 so we iterate through all candidates
        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # First PUT must be REMOTE (weak WiFi → REMOTE preferred)
        first_call = session_mock.put.call_args_list[0]
        assert first_call.kwargs["json"]["type"] == "REMOTE"

    @pytest.mark.asyncio
    async def test_stream_type_override_beats_options(self):
        """_stream_type_override='remote' overrides options stream_connection_type='local'."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_type_override="remote",
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},  # would normally → LOCAL
            ),
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        call_kwargs = session_mock.put.call_args
        assert call_kwargs.kwargs["json"]["type"] == "REMOTE"


# ── Group 3: TCP pre-check ─────────────────────────────────────────────────────


class TestTcpPreCheck:
    """Lines 2401-2425: TCP ping determines if LOCAL stays in candidates."""

    @pytest.mark.asyncio
    async def test_tcp_ping_fail_removes_local(self):
        """Both LOCAL+REMOTE, TCP ping=False → candidates=['REMOTE'], _stream_fell_back=True."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},  # good WiFi → both candidates
            _async_local_tcp_ping=AsyncMock(return_value=False),
            _lan_tcp_reachable={},  # no cache → actual ping
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # After TCP fail, only REMOTE should be tried
        assert session_mock.put.call_count == 1
        assert session_mock.put.call_args.kwargs["json"]["type"] == "REMOTE"
        assert coord._stream_fell_back.get(CAM_A) is True

    @pytest.mark.asyncio
    async def test_tcp_ping_success_keeps_local(self):
        """Both LOCAL+REMOTE, TCP ping=True → LOCAL remains first candidate."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},  # good WiFi
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _lan_tcp_reachable={},  # no cache → actual ping
        )

        # 404 for both candidates so we see what order they're tried
        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # LOCAL should be tried first (TCP ok → LOCAL stays in candidates)
        first_call = session_mock.put.call_args_list[0]
        assert first_call.kwargs["json"]["type"] == "LOCAL"

    @pytest.mark.asyncio
    async def test_tcp_cache_hit_skips_ping(self):
        """Cached TCP result (fresh) used → _async_local_tcp_ping NOT called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        ping_mock = AsyncMock(return_value=True)
        coord = _make_coord(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},
            _async_local_tcp_ping=ping_mock,
            # Cached result: reachable, fresh (< 60s ago)
            _lan_tcp_reachable={CAM_A: (True, time_mod.monotonic() - 10)},
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # Ping must NOT be called (cache hit)
        ping_mock.assert_not_called()


# ── Group 4: PUT 200 LOCAL success ─────────────────────────────────────────────


class TestPut200LocalSuccess:
    """Lines 2465-2695: LOCAL 200 response populates result, starts TLS proxy."""

    @pytest.mark.asyncio
    async def test_local_200_sets_connection_type_and_creds_cache(self):
        """PUT 200 LOCAL → result._connection_type='LOCAL', _local_creds_cache populated."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        local_body = json.dumps({
            "user": "u-local",
            "password": "p-local",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),  # skip TCP pre-check
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
        )

        resp = _put_resp(200, local_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        assert result.get("_connection_type") == "LOCAL"
        # Creds cache populated
        assert CAM_A in coord._local_creds_cache
        c = coord._local_creds_cache[CAM_A]
        assert c["user"] == "u-local"
        assert c["password"] == "p-local"
        assert c["host"] == "192.168.1.1"
        assert c["port"] == 443

    @pytest.mark.asyncio
    async def test_local_200_calls_start_tls_proxy(self):
        """PUT 200 LOCAL → _start_tls_proxy called with cam host/port."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.20.149:443"],
            "bufferingTime": 500,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
        )

        resp = _put_resp(200, local_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        coord._start_tls_proxy.assert_awaited_once()
        call_args = coord._start_tls_proxy.call_args
        # First positional arg = cam_id, second = host, third = port
        assert call_args.args[0] == CAM_A
        assert call_args.args[1] == "192.168.20.149"
        assert call_args.args[2] == 443

    @pytest.mark.asyncio
    async def test_local_200_registers_go2rtc_after_prewarm(self):
        """PUT 200 LOCAL → _register_go2rtc_stream called after pre-warm."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        reg_mock = AsyncMock()
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=12345),
            _tls_proxy_ports={CAM_A: 12345},
            _register_go2rtc_stream=reg_mock,
        )

        resp = _put_resp(200, local_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        reg_mock.assert_awaited_once()
        # First arg to _register_go2rtc_stream is cam_id
        assert reg_mock.call_args.args[0] == CAM_A

    @pytest.mark.asyncio
    async def test_local_200_sets_rtsps_url_after_prewarm(self):
        """PUT 200 LOCAL → result['rtspsUrl'] set only after pre-warm, contains proxy port."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        local_body = json.dumps({
            "user": "u",
            "password": "p",
            "urls": ["192.168.1.1:443"],
            "bufferingTime": 500,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "local"},
            ),
            _rcp_lan_ip_cache={},
            _local_creds_cache={},
            _get_cam_lan_ip=MagicMock(return_value=None),
            _start_tls_proxy=AsyncMock(return_value=54321),
            _tls_proxy_ports={CAM_A: 54321},
        )

        resp = _put_resp(200, local_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock), \
             patch("custom_components.bosch_shc_camera.pre_warm_rtsp",
                   new=AsyncMock(return_value=True)):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        assert "rtspsUrl" in result
        assert "127.0.0.1:54321" in result["rtspsUrl"]


# ── Group 5: PUT 200 REMOTE success ───────────────────────────────────────────


class TestPut200RemoteSuccess:
    """Lines 2508-2563: REMOTE 200 response builds rtspsUrl via TLS proxy."""

    @pytest.mark.asyncio
    async def test_remote_200_urls_field_sets_rtsps_url(self):
        """PUT 200 REMOTE with urls field → rtspsUrl starts with rtsp://127.0.0.1:."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
        )

        resp = _put_resp(200, remote_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        assert "rtspsUrl" in result
        assert result["rtspsUrl"].startswith("rtsp://127.0.0.1:")
        assert "54321" in result["rtspsUrl"]

    @pytest.mark.asyncio
    async def test_remote_200_hash_field_sets_proxy_url(self):
        """PUT 200 REMOTE with hash field (no urls) → result['proxyUrl'] contains hash."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        remote_body = json.dumps({
            "hash": "abc123",
            "proxyHost": "proxy-01.live.cbs.boschsecurity.com",
            "proxyPort": 42090,
            "bufferingTime": 1000,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
        )

        resp = _put_resp(200, remote_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        assert "proxyUrl" in result
        assert "abc123" in result["proxyUrl"]

    @pytest.mark.asyncio
    async def test_remote_200_proxy_start_failure_falls_back_to_direct(self):
        """If _start_tls_proxy raises → result['rtspsUrl'] falls back to direct rtsps://."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(side_effect=OSError("port unavailable")),
        )

        resp = _put_resp(200, remote_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        # Falls back to direct rtsps://
        assert "rtspsUrl" in result
        assert result["rtspsUrl"].startswith("rtsps://")

    @pytest.mark.asyncio
    async def test_remote_200_buffering_time_stored(self):
        """PUT 200 REMOTE → result['_bufferingTime'] = bufferingTime from body."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 2000,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
        )

        resp = _put_resp(200, remote_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        assert result.get("_bufferingTime") == 2000

    @pytest.mark.asyncio
    async def test_remote_200_live_connections_populated(self):
        """PUT 200 REMOTE → _live_connections[cam_id] is set."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
        )

        resp = _put_resp(200, remote_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        assert CAM_A in coord._live_connections


# ── Group 6: Error paths ───────────────────────────────────────────────────────


class TestErrorPaths:
    """Lines 2790-2808: 401, non-200, TimeoutError, ClientError, session.close()."""

    @pytest.mark.asyncio
    async def test_put_401_returns_none(self):
        """PUT 401 → WARNING logged, returns None immediately (token expired)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        resp = _put_resp(401, "Unauthorized")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is None

    @pytest.mark.asyncio
    async def test_put_404_continues_then_returns_none(self):
        """PUT 404 → loop continues; when all candidates exhausted returns None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is None

    @pytest.mark.asyncio
    async def test_put_500_returns_none_after_all_candidates(self):
        """PUT 500 (non-200/404/401) → loop continues; all fail → None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        resp = _put_resp(500, "Server Error")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_error_continues_to_next_candidate(self):
        """asyncio.TimeoutError on PUT → WARNING logged, continues to next candidate."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        import aiohttp as _aiohttp

        coord = _make_coord(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},
            # TCP pre-check must pass so we get both LOCAL and REMOTE
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _lan_tcp_reachable={},
        )

        # First call (LOCAL) → TimeoutError, second call (REMOTE) → 200 success
        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        resp_ok = _put_resp(200, remote_body)
        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(side_effect=[
            asyncio.TimeoutError(),
            resp_ok,
        ])

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        # TimeoutError on LOCAL → fell through to REMOTE → success
        assert result is not None

    @pytest.mark.asyncio
    async def test_client_error_returns_none(self):
        """aiohttp.ClientError → WARNING logged, returns None."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        import aiohttp as _aiohttp

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(side_effect=_aiohttp.ClientConnectionError("connection refused"))

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is None

    @pytest.mark.asyncio
    async def test_session_close_called_in_finally(self):
        """session.close() is always called — even on 401 early return."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        resp = _put_resp(401, "Unauthorized")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        session_mock.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_close_called_on_exception(self):
        """session.close() called even when PUT raises ClientError."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        import aiohttp as _aiohttp

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        session_mock = MagicMock()
        session_mock.close = AsyncMock()
        session_mock.put = AsyncMock(side_effect=_aiohttp.ClientConnectionError("fail"))

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        session_mock.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_close_called_on_success(self):
        """session.close() called even when PUT succeeds (REMOTE 200)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        remote_body = json.dumps({
            "urls": ["proxy-01.live.cbs.boschsecurity.com:42090/hashXXX"],
            "bufferingTime": 1000,
        })
        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
            _start_tls_proxy=AsyncMock(return_value=54321),
        )

        resp = _put_resp(200, remote_body)
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is not None
        session_mock.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_candidates_exhausted_returns_none(self):
        """When all candidates return non-success, returns None with warning."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={},
            _wifiinfo_cache={CAM_A: {"signalStrength": 80}},
            _async_local_tcp_ping=AsyncMock(return_value=True),
            _lan_tcp_reachable={},
        )

        # Both LOCAL and REMOTE → 500
        resp = _put_resp(500, "Server Error")
        session_mock = _make_session(resp)
        # Return same 500 for all calls
        session_mock.put = AsyncMock(return_value=resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            result = await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        assert result is None
        # Both LOCAL and REMOTE must have been tried
        assert session_mock.put.call_count == 2

    @pytest.mark.asyncio
    async def test_put_uses_bearer_token_in_headers(self):
        """PUT request includes Authorization: Bearer <token> header."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            token="my-secret-token",
            _entry=SimpleNamespace(
                data={"bearer_token": "my-secret-token"},
                options={"stream_connection_type": "remote"},
            ),
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        call_headers = session_mock.put.call_args.kwargs["headers"]
        assert call_headers["Authorization"] == "Bearer my-secret-token"

    @pytest.mark.asyncio
    async def test_put_url_contains_cam_id(self):
        """PUT URL contains the camera ID."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _entry=SimpleNamespace(
                data={"bearer_token": "tok-A"},
                options={"stream_connection_type": "remote"},
            ),
        )

        resp = _put_resp(404, "not found")
        session_mock = _make_session(resp)

        with patch("aiohttp.TCPConnector", return_value=MagicMock()), \
             patch("aiohttp.ClientSession", return_value=session_mock):
            await BoschCameraCoordinator._try_live_connection_inner(coord, CAM_A)

        call_url = session_mock.put.call_args.args[0]
        assert CAM_A in call_url
        assert "/v11/video_inputs/" in call_url
        assert "/connection" in call_url
