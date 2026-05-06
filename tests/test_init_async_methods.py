"""Tests for `__init__.py` async methods + thin delegation wrappers.

Round-1B follow-up to `test_init_helpers.py`. Targets the async I/O
methods on `BoschCameraCoordinator` that delegate to other modules
(fcm, shc, nvr_recorder), wrap aiohttp HTTP calls, or run coordinator-
specific lifecycle (token refresh, RCP session cache, stream worker
error rescue, recorder gating).

Each section pins a different contract:

  - FCM wrappers (start_fcm_push, stop_fcm_push, mark_events_read, ...)
    are thin one-liners over `fcm.async_*` — patch the imported symbol
    and assert the call signature.
  - SHC setter wrappers (async_shc_set_camera_light, async_cloud_set_*)
    are thin one-liners over `shc_mod.*` — same approach.
  - `async_put_camera` runs the universal Bosch v11 PUT path with
    401-rescue. Mock the aiohttp session.
  - `_async_local_tcp_ping` wraps asyncio.open_connection. Mock it.
  - `_handle_stream_worker_error` is the v10.4.10 LOCAL-rescue path —
    when HA's stream worker reports auth failure, refresh creds without
    falling back to REMOTE. Test all branches.
  - `start_recorder` / `stop_recorder` / `_restart_recorder_if_active`
    are the NVR gating wrappers from v11.0.4-5.
  - RCP session cache + token refresh schedule round out the suite.

These all run without HA runtime — `SimpleNamespace` for `self`,
`MagicMock` / `AsyncMock` for collaborators.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"
CAM_B = "20E053B5-BE64-4E45-A2CA-BBDC20F5C351"


# ── Shared stubs ─────────────────────────────────────────────────────────


async def _noop_coro(*args, **kwargs):
    return None


def _make_coord(**overrides):
    """Coordinator stub with the dicts most async methods touch."""
    # async_create_task gets called with a coroutine — close it to avoid
    # the "coroutine never awaited" warning, and return a MagicMock task.
    def _create_task(coro):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock(spec=asyncio.Task)

    base = dict(
        _entry=SimpleNamespace(
            data={"bearer_token": "tok-A", "refresh_token": "rfr-B"},
            options={},
        ),
        _refreshed_token=None,
        _refreshed_refresh=None,
        _camera_entities={},
        _live_connections={},
        _live_opened_at={},
        _stream_error_count={},
        _stream_error_at={},
        _stream_fell_back={},
        _local_rescue_attempts={},
        _local_rescue_at={},
        _renewal_tasks={},
        _bg_tasks=set(),
        _nvr_processes={},
        _nvr_user_intent={},
        _rcp_lan_ip_cache={},
        _local_creds_cache={},
        _rcp_session_cache={},
        _lan_tcp_reachable={},
        _hw_version={},
        _privacy_set_at={},
        _light_set_at={},
        _offline_since={},
        _per_cam_status_at={},
        _last_status=0.0,
        _OFFLINE_EXTENDED_INTERVAL=900,
        _WRITE_LOCK_SECS=30.0,
        _token_refresh_handle=None,
        _stream_worker_dispatch_pending=None,
        _stop_tls_proxy=AsyncMock(),
        _ensure_valid_token=AsyncMock(return_value="fresh-token"),
        try_live_connection=AsyncMock(return_value=None),
        record_stream_error=MagicMock(),
        # _handle_stream_worker_error is invoked inside _schedule_stream_worker_error
        # as `self._handle_stream_worker_error(cam_id, msg)` — must return a
        # coroutine that async_create_task can wrap. Lambda + helper keeps it simple.
        _handle_stream_worker_error=lambda *a, **kw: _noop_coro(),
        get_model_config=lambda cid: SimpleNamespace(max_stream_errors=3, max_session_duration=3600),
        is_camera_online=lambda cid: True,
        hass=SimpleNamespace(
            async_create_task=MagicMock(side_effect=_create_task),
            async_add_executor_job=AsyncMock(),
            loop=SimpleNamespace(
                call_later=MagicMock(return_value=MagicMock()),
                call_soon_threadsafe=MagicMock(),
            ),
            services=SimpleNamespace(async_call=AsyncMock()),
            config=SimpleNamespace(path=lambda *a: "/tmp/x"),
        ),
        debug=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _bind_method(coord, method_name: str) -> None:
    """Make a real BoschCameraCoordinator method callable on the stub.

    Useful when one method-under-test calls another bound method on
    self — bind the helper to the same SimpleNamespace stub so the
    call dispatches through real code, not a mock.
    """
    from custom_components.bosch_shc_camera import BoschCameraCoordinator
    func = getattr(BoschCameraCoordinator, method_name)
    setattr(coord, method_name, lambda *a, **kw: func(coord, *a, **kw))


def _make_jwt(exp_offset_seconds: int) -> str:
    """Build a fake JWT whose payload's `exp` is now + offset."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload_dict = {"exp": int(time.time() + exp_offset_seconds)}
    payload = base64.urlsafe_b64encode(
        json.dumps(payload_dict).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.signature"


@asynccontextmanager
async def _aiohttp_resp(status: int = 200, body: str = ""):
    """Fake `async with` aiohttp response context manager."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json.loads(body) if body else {})
    resp.text = AsyncMock(return_value=body)
    yield resp


def _fake_session(responses: list):
    """Build a MagicMock aiohttp session whose `.put`/`.get`/`.delete`/`.post`
    return the next response from `responses` (in order, cycling on each
    call). Each response is a `(status, body)` tuple."""
    iterator = iter(responses)

    def _make_cm(*args, **kwargs):
        try:
            status, body = next(iterator)
        except StopIteration:
            status, body = 500, ""
        return _aiohttp_resp(status, body)

    session = MagicMock()
    session.put = _make_cm
    session.get = _make_cm
    session.delete = _make_cm
    session.post = _make_cm
    return session


# ── FCM delegation wrappers (3872-3915) ──────────────────────────────────


class TestFcmWrappers:
    """All `_fcm_*` symbol delegations — each is a single-line wrapper.
    Patching the imported symbol lets us assert the call shape without
    touching firebase_messaging."""

    @pytest.mark.asyncio
    async def test_async_start_fcm_push_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera._fcm_async_start_fcm_push",
            new=AsyncMock(return_value=None),
        ) as m:
            await BoschCameraCoordinator.async_start_fcm_push(coord)
            m.assert_awaited_once_with(coord)

    @pytest.mark.asyncio
    async def test_async_stop_fcm_push_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera._fcm_async_stop_fcm_push",
            new=AsyncMock(return_value=None),
        ) as m:
            await BoschCameraCoordinator.async_stop_fcm_push(coord)
            m.assert_awaited_once_with(coord)

    @pytest.mark.asyncio
    async def test_register_fcm_with_bosch_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera._fcm_register_fcm_with_bosch",
            new=AsyncMock(return_value=True),
        ) as m:
            ok = await BoschCameraCoordinator._register_fcm_with_bosch(coord)
            assert ok is True
            m.assert_awaited_once_with(coord)

    @pytest.mark.asyncio
    async def test_async_handle_fcm_push_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera._fcm_async_handle_fcm_push",
            new=AsyncMock(return_value=None),
        ) as m:
            await BoschCameraCoordinator._async_handle_fcm_push(coord)
            m.assert_awaited_once_with(coord)

    @pytest.mark.asyncio
    async def test_async_send_alert_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera._fcm_async_send_alert",
            new=AsyncMock(return_value=None),
        ) as m:
            await BoschCameraCoordinator._async_send_alert(
                coord, "Terrasse", "motion", "2026-05-06T10:00",
                "https://x/img.jpg", clip_url="https://x/clip.mp4",
                clip_status="ok",
            )
            m.assert_awaited_once_with(
                coord, "Terrasse", "motion", "2026-05-06T10:00",
                "https://x/img.jpg", "https://x/clip.mp4", "ok",
            )

    @pytest.mark.asyncio
    async def test_async_mark_events_read_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera._fcm_async_mark_events_read",
            new=AsyncMock(return_value=True),
        ) as m:
            ok = await BoschCameraCoordinator.async_mark_events_read(coord, ["id1", "id2"])
            assert ok is True
            m.assert_awaited_once_with(coord, ["id1", "id2"])

    def test_get_alert_services_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera._fcm_get_alert_services",
            return_value=["notify.test_user"],
        ) as m:
            out = BoschCameraCoordinator._get_alert_services(coord, "movement")
            assert out == ["notify.test_user"]
            m.assert_called_once_with(coord, "movement")

    def test_build_notify_data_delegates(self):
        """Static method — no coordinator self."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        with patch(
            "custom_components.bosch_shc_camera._fcm_build_notify_data",
            return_value={"message": "x"},
        ) as m:
            out = BoschCameraCoordinator._build_notify_data(
                "notify.test_user", "Hi", file_path="/tmp/img.jpg", title="T",
            )
            assert out == {"message": "x"}
            m.assert_called_once_with("notify.test_user", "Hi", "/tmp/img.jpg", "T")

    def test_write_file_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        with patch(
            "custom_components.bosch_shc_camera._fcm_write_file",
        ) as m:
            BoschCameraCoordinator._write_file("/tmp/x.jpg", b"\xff\xd8")
            m.assert_called_once_with("/tmp/x.jpg", b"\xff\xd8")


# ── SHC delegation wrappers (4179-4229) ──────────────────────────────────


class TestShcWrappers:
    """All `shc_mod.*` delegations. Same one-line wrapper pattern."""

    def test_shc_configured_property(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.shc_configured",
            return_value=True,
        ) as m:
            assert BoschCameraCoordinator.shc_configured.fget(coord) is True
            m.assert_called_once_with(coord)

    def test_shc_ready_property(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.shc_ready",
            return_value=False,
        ) as m:
            assert BoschCameraCoordinator.shc_ready.fget(coord) is False
            m.assert_called_once_with(coord)

    def test_shc_mark_success_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod._shc_mark_success",
        ) as m:
            BoschCameraCoordinator._shc_mark_success(coord)
            m.assert_called_once_with(coord)

    def test_shc_mark_failure_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod._shc_mark_failure",
        ) as m:
            BoschCameraCoordinator._shc_mark_failure(coord)
            m.assert_called_once_with(coord)

    @pytest.mark.asyncio
    async def test_async_shc_request_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_shc_request",
            new=AsyncMock(return_value={"ok": 1}),
        ) as m:
            out = await BoschCameraCoordinator._async_shc_request(
                coord, "GET", "/devices",
            )
            assert out == {"ok": 1}
            m.assert_awaited_once_with(coord, "GET", "/devices", None)

    @pytest.mark.asyncio
    async def test_async_update_shc_states_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_update_shc_states",
            new=AsyncMock(return_value=None),
        ) as m:
            await BoschCameraCoordinator._async_update_shc_states(coord, {"data": 1})
            m.assert_awaited_once_with(coord, {"data": 1})

    @pytest.mark.asyncio
    async def test_shc_set_camera_light_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_shc_set_camera_light",
            new=AsyncMock(return_value=True),
        ) as m:
            ok = await BoschCameraCoordinator.async_shc_set_camera_light(coord, CAM_A, True)
            assert ok is True
            m.assert_awaited_once_with(coord, CAM_A, True)

    @pytest.mark.asyncio
    async def test_cloud_set_light_component_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_cloud_set_light_component",
            new=AsyncMock(return_value=True),
        ) as m:
            ok = await BoschCameraCoordinator.async_cloud_set_light_component(
                coord, CAM_A, "frontLight", 80,
            )
            assert ok is True
            m.assert_awaited_once_with(coord, CAM_A, "frontLight", 80)

    @pytest.mark.asyncio
    async def test_shc_set_privacy_mode_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_shc_set_privacy_mode",
            new=AsyncMock(return_value=True),
        ) as m:
            ok = await BoschCameraCoordinator.async_shc_set_privacy_mode(coord, CAM_A, False)
            assert ok is True
            m.assert_awaited_once_with(coord, CAM_A, False)

    @pytest.mark.asyncio
    async def test_cloud_set_privacy_mode_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_cloud_set_privacy_mode",
            new=AsyncMock(return_value=True),
        ) as m:
            ok = await BoschCameraCoordinator.async_cloud_set_privacy_mode(coord, CAM_A, True)
            assert ok is True
            m.assert_awaited_once_with(coord, CAM_A, True)

    @pytest.mark.asyncio
    async def test_cloud_set_camera_light_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_cloud_set_camera_light",
            new=AsyncMock(return_value=True),
        ) as m:
            await BoschCameraCoordinator.async_cloud_set_camera_light(coord, CAM_A, True)
            m.assert_awaited_once_with(coord, CAM_A, True)

    @pytest.mark.asyncio
    async def test_cloud_set_notifications_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_cloud_set_notifications",
            new=AsyncMock(return_value=True),
        ) as m:
            await BoschCameraCoordinator.async_cloud_set_notifications(coord, CAM_A, False)
            m.assert_awaited_once_with(coord, CAM_A, False)

    @pytest.mark.asyncio
    async def test_cloud_set_pan_delegates(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.shc_mod.async_cloud_set_pan",
            new=AsyncMock(return_value=True),
        ) as m:
            await BoschCameraCoordinator.async_cloud_set_pan(coord, CAM_A, 30)
            m.assert_awaited_once_with(coord, CAM_A, 30)


# ── async_put_camera (4151-4177) ─────────────────────────────────────────


class TestAsyncPutCamera:
    """Universal Bosch v11 PUT path with 401-rescue. Used by every
    cloud-side setter (pan, color temp, audio, motion, etc.). A
    regression here breaks ~30 different switches/numbers."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_true(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = "valid"
        session = _fake_session([(204, "")])
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschCameraCoordinator.async_put_camera(
                coord, CAM_A, "audio", {"enabled": True},
            )
            assert ok is True

    @pytest.mark.asyncio
    async def test_201_counted_as_success(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = "valid"
        session = _fake_session([(201, "")])
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschCameraCoordinator.async_put_camera(
                coord, CAM_A, "x", {},
            )
            assert ok is True

    @pytest.mark.asyncio
    async def test_500_returns_false(self):
        """Any non-2xx other than 401 → False, no retry, no exception."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = "valid"
        session = _fake_session([(500, "")])
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschCameraCoordinator.async_put_camera(
                coord, CAM_A, "x", {},
            )
            assert ok is False

    @pytest.mark.asyncio
    async def test_401_triggers_token_refresh_and_retries(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = "expired"
        session = _fake_session([(401, ""), (204, "")])  # 401 then success
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschCameraCoordinator.async_put_camera(
                coord, CAM_A, "x", {},
            )
            assert ok is True
            # _ensure_valid_token must have been awaited exactly once
            coord._ensure_valid_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_401_then_token_refresh_fails_returns_false(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = "expired"
        coord._ensure_valid_token = AsyncMock(side_effect=RuntimeError("auth down"))
        session = _fake_session([(401, "")])
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschCameraCoordinator.async_put_camera(
                coord, CAM_A, "x", {},
            )
            assert ok is False

    @pytest.mark.asyncio
    async def test_network_error_returns_false_no_raise(self):
        """aiohttp errors / timeouts must not propagate — switches that
        call us would surface a red toast otherwise on a transient blip."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = "valid"
        session = MagicMock()

        def boom(*a, **kw):
            raise asyncio.TimeoutError("network lost")

        session.put = boom
        with patch(
            "custom_components.bosch_shc_camera.async_get_clientsession",
            return_value=session,
        ):
            ok = await BoschCameraCoordinator.async_put_camera(
                coord, CAM_A, "x", {},
            )
            assert ok is False


# ── _async_local_tcp_ping (1250-1271) ────────────────────────────────────


class TestLocalTcpPing:
    """Quick LAN reachability probe — much faster than the cloud
    /commissioned check (~5ms vs ~200ms). Used by AUTO-mode to choose
    LOCAL vs REMOTE without paying the full PUT /connection cost."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_lan_ip_known(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()  # no LAN IP cached
        _bind_method(coord, "_get_cam_lan_ip")
        ok = await BoschCameraCoordinator._async_local_tcp_ping(coord, CAM_A)
        assert ok is False
        # Note: when no LAN IP, we return early without writing _lan_tcp_reachable.
        assert CAM_A not in coord._lan_tcp_reachable

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_connect(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_rcp_lan_ip_cache={CAM_A: "192.0.2.149"})
        _bind_method(coord, "_get_cam_lan_ip")

        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch(
            "asyncio.open_connection",
            new=AsyncMock(return_value=(MagicMock(), writer)),
        ):
            ok = await BoschCameraCoordinator._async_local_tcp_ping(coord, CAM_A)
            assert ok is True
            writer.close.assert_called_once()
        # Result cached for re-use
        assert coord._lan_tcp_reachable[CAM_A][0] is True

    @pytest.mark.asyncio
    async def test_returns_false_on_connection_refused(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_rcp_lan_ip_cache={CAM_A: "192.0.2.149"})
        _bind_method(coord, "_get_cam_lan_ip")
        with patch(
            "asyncio.open_connection",
            new=AsyncMock(side_effect=OSError("refused")),
        ):
            ok = await BoschCameraCoordinator._async_local_tcp_ping(coord, CAM_A)
            assert ok is False
        assert coord._lan_tcp_reachable[CAM_A][0] is False

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_rcp_lan_ip_cache={CAM_A: "192.0.2.149"})
        _bind_method(coord, "_get_cam_lan_ip")
        with patch(
            "asyncio.open_connection",
            new=AsyncMock(side_effect=asyncio.TimeoutError()),
        ):
            ok = await BoschCameraCoordinator._async_local_tcp_ping(coord, CAM_A)
            assert ok is False

    @pytest.mark.asyncio
    async def test_falls_back_to_local_creds_host(self):
        """When _rcp_lan_ip_cache is empty but _local_creds_cache has a
        host field, use that. Pin so a refactor of the LAN-IP discovery
        order can't silently lose the fallback."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _local_creds_cache={CAM_A: {"host": "192.0.2.21"}},
        )
        _bind_method(coord, "_get_cam_lan_ip")
        writer = MagicMock()
        writer.close = MagicMock()
        writer.wait_closed = AsyncMock()
        with patch(
            "asyncio.open_connection",
            new=AsyncMock(return_value=(MagicMock(), writer)),
        ) as conn:
            await BoschCameraCoordinator._async_local_tcp_ping(coord, CAM_A)
            conn.assert_awaited_once()
            assert conn.await_args[0][0] == "192.0.2.21"


# ── _proactive_refresh + _schedule_token_refresh (1192-1248) ─────────────


class TestTokenRefreshSchedule:
    """The proactive token refresh — fires 5 minutes before JWT exp.
    Without this, automations firing in the gap between expiry and the
    next coordinator tick would 401 and trigger reactive refresh, with
    a visible delay to the user."""

    def test_schedule_token_refresh_with_valid_token(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = _make_jwt(exp_offset_seconds=600)  # 10 min from now
        BoschCameraCoordinator._schedule_token_refresh(coord)
        # Should have called hass.loop.call_later once
        coord.hass.loop.call_later.assert_called_once()
        # First arg is delay — should be roughly remaining (600) - 300 = 300
        delay = coord.hass.loop.call_later.call_args[0][0]
        assert 290 <= delay <= 310, f"Expected ~300s delay, got {delay}"

    def test_schedule_token_refresh_clamps_minimum_10s(self):
        """Token already expired (negative remaining) — must clamp to
        10 s so we don't enter a tight refresh loop."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = _make_jwt(exp_offset_seconds=-100)  # already expired
        BoschCameraCoordinator._schedule_token_refresh(coord)
        coord.hass.loop.call_later.assert_called_once()
        delay = coord.hass.loop.call_later.call_args[0][0]
        assert delay == 10, f"Expected 10s clamp, got {delay}"

    def test_schedule_token_refresh_cancels_prior_handle(self):
        """A new schedule must cancel the previous timer to avoid
        stacking refresh callbacks."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        prior_handle = MagicMock()
        coord = _make_coord(_token_refresh_handle=prior_handle)
        coord.token = _make_jwt(exp_offset_seconds=600)
        BoschCameraCoordinator._schedule_token_refresh(coord)
        prior_handle.cancel.assert_called_once()

    def test_schedule_token_refresh_no_token_returns(self):
        """Empty token → silent no-op, no exception."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = ""
        BoschCameraCoordinator._schedule_token_refresh(coord)
        coord.hass.loop.call_later.assert_not_called()

    def test_schedule_token_refresh_garbage_token_returns(self):
        """Malformed JWT → swallow, don't crash."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord.token = "not-a-jwt"
        BoschCameraCoordinator._schedule_token_refresh(coord)
        coord.hass.loop.call_later.assert_not_called()

    @pytest.mark.asyncio
    async def test_proactive_refresh_calls_ensure_valid_token(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        await BoschCameraCoordinator._proactive_refresh(coord)
        coord._ensure_valid_token.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_proactive_refresh_swallows_failure(self):
        """A failed proactive refresh must not crash the timer chain —
        the next reactive 401 will retry."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._ensure_valid_token = AsyncMock(side_effect=RuntimeError("oauth down"))
        # Must not raise
        await BoschCameraCoordinator._proactive_refresh(coord)


# ── _handle_stream_worker_error (850-953) ────────────────────────────────


class TestHandleStreamWorkerError:
    """v10.4.10 LOCAL-rescue path — when HA's stream worker reports a
    401, refresh the LOCAL session creds rather than falling back to
    REMOTE. The LAN is fine, the camera just rotated its per-session
    Digest creds out from under FFmpeg.

    Branches to cover:
      - below threshold: return without action
      - LOCAL + 401 + first attempt: rescue path triggers
      - LOCAL + 401 + already attempted: REMOTE escalation
      - LOCAL + non-401: REMOTE escalation
      - REMOTE/no-session: no further fallback, log only
      - Time-decay: a rescue >5 min ago doesn't block a fresh attempt
    """

    @pytest.mark.asyncio
    async def test_below_threshold_returns_silently(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_stream_error_count={CAM_A: 1})  # threshold is 3
        await BoschCameraCoordinator._handle_stream_worker_error(coord, CAM_A, "Error from stream worker: timeout")
        # try_live_connection NOT called below threshold
        coord.try_live_connection.assert_not_awaited()
        # Stop TLS proxy NOT called below threshold
        coord._stop_tls_proxy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_local_401_first_attempt_triggers_rescue(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _stream_error_count={CAM_A: 3},  # at threshold
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
        )
        coord.try_live_connection = AsyncMock(return_value={"_connection_type": "LOCAL"})
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_A, "Error from stream worker: HTTP 401 Unauthorized",
        )
        # Rescue counter incremented
        assert coord._local_rescue_attempts.get(CAM_A) == 1
        # Error counter reset so try_live_connection picks LOCAL again
        assert coord._stream_error_count[CAM_A] == 0
        # Live conn cleared, TLS proxy stopped, new try_live_connection called
        assert CAM_A not in coord._live_connections
        coord._stop_tls_proxy.assert_awaited_once_with(CAM_A)
        coord.try_live_connection.assert_awaited_once_with(CAM_A)

    @pytest.mark.asyncio
    async def test_local_401_second_attempt_falls_back_to_remote(self):
        """After the first rescue attempt was used, a repeat 401 must
        NOT rescue again — it falls through to the REMOTE escalation."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _stream_error_count={CAM_A: 3},
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
            _local_rescue_attempts={CAM_A: 1},  # already used the rescue
            _local_rescue_at={CAM_A: time.monotonic()},  # recent
        )
        coord.try_live_connection = AsyncMock(return_value={"_connection_type": "REMOTE"})
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_A, "Error from stream worker: 401 Unauthorized",
        )
        # _stream_fell_back marked so next start prefers REMOTE
        assert coord._stream_fell_back.get(CAM_A) is True

    @pytest.mark.asyncio
    async def test_local_non_401_falls_back_to_remote(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _stream_error_count={CAM_A: 3},
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
        )
        coord.try_live_connection = AsyncMock(return_value={"_connection_type": "REMOTE"})
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_A, "Error from stream worker: connection reset",
        )
        # No rescue attempt
        assert CAM_A not in coord._local_rescue_attempts
        # Fell-back flag set
        assert coord._stream_fell_back.get(CAM_A) is True

    @pytest.mark.asyncio
    async def test_already_remote_no_escalation(self):
        """If we're already on REMOTE there's nowhere to fall back to."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _stream_error_count={CAM_A: 3},
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_A, "Error from stream worker: timeout",
        )
        coord.try_live_connection.assert_not_awaited()
        coord._stop_tls_proxy.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_session_no_escalation(self):
        """Worker errors on a torn-down stream — log only, no rebuild
        attempt (the user toggle is OFF, leave it OFF)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_stream_error_count={CAM_A: 3})
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_A, "Error from stream worker: stale",
        )
        coord.try_live_connection.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rescue_counter_decays_after_5_minutes(self):
        """A rescue attempt older than 5 min belongs to a previous burst —
        must be reset so the next legitimate 401 burst gets its own
        rescue. Pinned because the symptom of a regression is silent
        REMOTE-pinning after the 8-14min Bosch cred-rotation window."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _stream_error_count={CAM_A: 3},
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
            _local_rescue_attempts={CAM_A: 1},
            _local_rescue_at={CAM_A: time.monotonic() - 600},  # 10 min ago
        )
        coord.try_live_connection = AsyncMock(return_value={"_connection_type": "LOCAL"})
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_A, "Error from stream worker: HTTP 401",
        )
        # Decay reset → fresh rescue attempt allowed
        assert coord._local_rescue_attempts.get(CAM_A) == 1  # incremented from 0
        # And we DID call try_live_connection (rescue path)
        coord.try_live_connection.assert_awaited_once_with(CAM_A)

    @pytest.mark.asyncio
    async def test_dispatch_pending_set_cleared_on_completion(self):
        """The dispatch dedup set must lose the cam_id even when an
        exception fires inside — pinned via finally clause."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        pending = {CAM_A}
        coord = _make_coord(
            _stream_error_count={CAM_A: 1},  # below threshold → returns early
            _stream_worker_dispatch_pending=pending,
        )
        await BoschCameraCoordinator._handle_stream_worker_error(coord, CAM_A, "ignore me")
        assert CAM_A not in pending


# ── start_recorder / stop_recorder / _restart_recorder_if_active ─────────


class TestRecorderWrappers:
    """NVR Phase 1+2 wrappers (v11.0.4-5). Thin gating on
    `nvr_recorder.should_record`. Pin so the gate doesn't accidentally
    recursively spin the recorder when the LAN drops."""

    @pytest.mark.asyncio
    async def test_start_recorder_sets_user_intent(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.should_record",
            return_value=False,
        ):
            await BoschCameraCoordinator.start_recorder(coord, CAM_A)
        # Even when gate is closed, user-intent flag is set so the
        # watcher restarts the recorder when LAN comes back.
        assert coord._nvr_user_intent.get(CAM_A) is True

    @pytest.mark.asyncio
    async def test_start_recorder_skips_when_gate_closed(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        start = AsyncMock()
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.should_record",
            return_value=False,
        ), patch(
            "custom_components.bosch_shc_camera.nvr_recorder.start_recorder",
            new=start,
        ):
            await BoschCameraCoordinator.start_recorder(coord, CAM_A)
        start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_recorder_calls_through_when_gate_open(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        start = AsyncMock()
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.should_record",
            return_value=True,
        ), patch(
            "custom_components.bosch_shc_camera.nvr_recorder.start_recorder",
            new=start,
        ):
            await BoschCameraCoordinator.start_recorder(coord, CAM_A)
        start.assert_awaited_once_with(coord, CAM_A)

    @pytest.mark.asyncio
    async def test_stop_recorder_clears_intent_by_default(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_nvr_user_intent={CAM_A: True})
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.stop_recorder",
            new=AsyncMock(),
        ):
            await BoschCameraCoordinator.stop_recorder(coord, CAM_A)
        assert CAM_A not in coord._nvr_user_intent

    @pytest.mark.asyncio
    async def test_stop_recorder_keeps_intent_when_clear_intent_false(self):
        """When LAN drops, we stop ffmpeg but preserve intent so the
        recorder restarts when LAN comes back. v11.0.5 behavior."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_nvr_user_intent={CAM_A: True})
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.stop_recorder",
            new=AsyncMock(),
        ):
            await BoschCameraCoordinator.stop_recorder(coord, CAM_A, clear_intent=False)
        assert coord._nvr_user_intent.get(CAM_A) is True

    @pytest.mark.asyncio
    async def test_restart_recorder_skips_when_no_active_process(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()  # _nvr_processes empty
        start = AsyncMock()
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.start_recorder",
            new=start,
        ):
            await BoschCameraCoordinator._restart_recorder_if_active(coord, CAM_A)
        start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restart_recorder_skips_when_no_user_intent(self):
        """ffmpeg running but the user already toggled the switch off —
        don't restart, the next teardown will clean it up."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _nvr_processes={CAM_A: object()},
            _nvr_user_intent={},  # no intent
        )
        start = AsyncMock()
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.start_recorder",
            new=start,
        ):
            await BoschCameraCoordinator._restart_recorder_if_active(coord, CAM_A)
        start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restart_recorder_when_active_and_intent_set(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(
            _nvr_processes={CAM_A: object()},
            _nvr_user_intent={CAM_A: True},
        )
        start = AsyncMock()
        with patch(
            "custom_components.bosch_shc_camera.nvr_recorder.start_recorder",
            new=start,
        ):
            await BoschCameraCoordinator._restart_recorder_if_active(coord, CAM_A)
        start.assert_awaited_once_with(coord, CAM_A)


# ── _invalidate_rcp_session + _get_cached_rcp_session ────────────────────


class TestRcpSessionCache:
    """5-min TTL session cache for RCP+ over the cloud proxy. Saves the
    2-step handshake (0xff0c + 0xff0d) on every thumbnail/data fetch.
    Misuse here → either stale-session 0x0c0d errors or unnecessary
    re-handshake latency."""

    def test_invalidate_pops_existing_entry(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_rcp_session_cache={"hashA": ("sess1", 9e9)})
        BoschCameraCoordinator._invalidate_rcp_session(coord, "hashA")
        assert "hashA" not in coord._rcp_session_cache

    def test_invalidate_no_entry_no_crash(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        # Must NOT raise on missing key
        BoschCameraCoordinator._invalidate_rcp_session(coord, "nonexistent")

    @pytest.mark.asyncio
    async def test_get_cached_returns_cached_session_when_valid(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        future = time.monotonic() + 100  # 100s into the future
        coord = _make_coord(_rcp_session_cache={"hashB": ("sess-X", future)})
        coord._rcp_session = AsyncMock(return_value="should-not-be-called")
        out = await BoschCameraCoordinator._get_cached_rcp_session(
            coord, "host", "hashB",
        )
        assert out == "sess-X"
        coord._rcp_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_get_cached_renews_when_expired(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        past = time.monotonic() - 10
        coord = _make_coord(_rcp_session_cache={"hashC": ("old", past)})
        coord._rcp_session = AsyncMock(return_value="new-sess")
        out = await BoschCameraCoordinator._get_cached_rcp_session(
            coord, "host", "hashC",
        )
        assert out == "new-sess"
        # Old entry replaced
        sess, exp = coord._rcp_session_cache["hashC"]
        assert sess == "new-sess"
        assert exp > time.monotonic() + 290  # ~5min TTL

    @pytest.mark.asyncio
    async def test_get_cached_no_entry_opens_new(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_session = AsyncMock(return_value="brand-new")
        out = await BoschCameraCoordinator._get_cached_rcp_session(
            coord, "host", "hashD",
        )
        assert out == "brand-new"
        assert "hashD" in coord._rcp_session_cache

    @pytest.mark.asyncio
    async def test_get_cached_handshake_failure_not_cached(self):
        """If `_rcp_session` returns None (handshake failed), the cache
        must NOT pin None — next caller must retry the handshake."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._rcp_session = AsyncMock(return_value=None)
        out = await BoschCameraCoordinator._get_cached_rcp_session(
            coord, "host", "hashE",
        )
        assert out is None
        assert "hashE" not in coord._rcp_session_cache


# ── _replace_renewal_task (955) ──────────────────────────────────────────


class TestReplaceRenewalTask:
    """Cancel any existing renewal task before starting a new one. A
    leaked renewal loop fights with the new one over the same cred
    refresh; this is the bug shape behind the 'random LOCAL drops every
    few minutes' regression in v10.3.10."""

    def test_replace_with_no_existing_task(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        task = BoschCameraCoordinator._replace_renewal_task(coord, CAM_A, _noop_coro())
        coord.hass.async_create_task.assert_called_once()
        assert coord._renewal_tasks[CAM_A] is task
        assert task in coord._bg_tasks

    def test_replace_cancels_old_task(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        old = MagicMock()
        old.done = MagicMock(return_value=False)
        old.cancel = MagicMock()
        coord = _make_coord(_renewal_tasks={CAM_A: old})
        BoschCameraCoordinator._replace_renewal_task(coord, CAM_A, _noop_coro())
        old.cancel.assert_called_once()

    def test_replace_does_not_cancel_done_task(self):
        """A task that's already finished must not be cancelled (no-op
        protect — `task.cancel()` on a done future raises in some
        Python versions)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        old = MagicMock()
        old.done = MagicMock(return_value=True)
        old.cancel = MagicMock()
        coord = _make_coord(_renewal_tasks={CAM_A: old})
        BoschCameraCoordinator._replace_renewal_task(coord, CAM_A, _noop_coro())
        old.cancel.assert_not_called()

    def test_replace_registers_done_callback(self):
        """Returned task gets `add_done_callback(self._bg_tasks.discard)` so
        the bg-task set self-cleans when the task finishes."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        task = BoschCameraCoordinator._replace_renewal_task(coord, CAM_A, _noop_coro())
        task.add_done_callback.assert_called_once_with(coord._bg_tasks.discard)


# ── Properties (token, refresh_token, options, debug) ────────────────────


class TestCoordinatorProperties:
    """Trivial property reads but each is a hot path in the coordinator
    — `token` is read on every HTTP call. Pin so refactors of the
    `_refreshed_*` shadow-cache don't break them."""

    def test_token_returns_refreshed_when_set(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_refreshed_token="fresh")
        assert BoschCameraCoordinator.token.fget(coord) == "fresh"

    def test_token_falls_back_to_entry_data(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        # _refreshed_token is None in default stub
        assert BoschCameraCoordinator.token.fget(coord) == "tok-A"

    def test_refresh_token_returns_refreshed_when_set(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord(_refreshed_refresh="fresh-rfr")
        assert BoschCameraCoordinator.refresh_token.fget(coord) == "fresh-rfr"

    def test_refresh_token_falls_back_to_entry_data(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        assert BoschCameraCoordinator.refresh_token.fget(coord) == "rfr-B"

    def test_options_property_uses_get_options(self):
        from custom_components.bosch_shc_camera import (
            BoschCameraCoordinator, get_options,
        )
        coord = _make_coord()
        coord._entry = SimpleNamespace(
            data={}, options={"scan_interval": 42},
        )
        assert BoschCameraCoordinator.options.fget(coord) == get_options(coord._entry)

    def test_debug_property_default_false(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._entry = SimpleNamespace(data={}, options={})
        assert BoschCameraCoordinator.debug.fget(coord) is False

    def test_debug_property_true_when_option_set(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        coord._entry = SimpleNamespace(data={}, options={"debug_logging": True})
        assert BoschCameraCoordinator.debug.fget(coord) is True


# ── _token_still_valid pure check ────────────────────────────────────────


class TestTokenStillValid:
    """Pure JWT exp parsing. Already partially covered in
    test_coordinator_pure_helpers.py — these add edge cases that
    weren't pinned: malformed payload, missing exp, default
    min_remaining."""

    def test_valid_token_with_lots_of_runway(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(
            _entry=SimpleNamespace(
                data={"bearer_token": _make_jwt(exp_offset_seconds=3600)},
            ),
            _refreshed_token=None,
        )
        coord.token = BoschCameraCoordinator.token.fget(coord)
        assert BoschCameraCoordinator._token_still_valid(coord) is True

    def test_token_within_min_remaining_window_invalid(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        # Token expires in 30s; default min_remaining=60 → invalid
        coord = SimpleNamespace(
            _entry=SimpleNamespace(
                data={"bearer_token": _make_jwt(exp_offset_seconds=30)},
            ),
            _refreshed_token=None,
        )
        coord.token = BoschCameraCoordinator.token.fget(coord)
        assert BoschCameraCoordinator._token_still_valid(coord) is False

    def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(
            _entry=SimpleNamespace(data={}),
            _refreshed_token=None,
        )
        coord.token = ""
        assert BoschCameraCoordinator._token_still_valid(coord) is False

    def test_garbage_token_returns_false(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(
            _entry=SimpleNamespace(data={"bearer_token": "not-a-jwt"}),
            _refreshed_token=None,
        )
        coord.token = "not-a-jwt"
        assert BoschCameraCoordinator._token_still_valid(coord) is False


# ── _schedule_stream_worker_error (834) ──────────────────────────────────


class TestScheduleStreamWorkerError:
    """The dedup wrapper — emit() in the listener calls
    call_soon_threadsafe with this; the coordinator dispatches once per
    cam_id even if multiple errors land in the same loop tick. Pin the
    deduplication so a future refactor can't accidentally turn it into
    a per-error fire-and-forget (which would spawn dozens of rescue
    attempts on a single 401 burst)."""

    def test_first_call_creates_pending_set_and_schedules_task(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        BoschCameraCoordinator._schedule_stream_worker_error(coord, CAM_A, "Error from stream worker")
        # A task was created
        coord.hass.async_create_task.assert_called_once()
        # And cam_id is in the pending set
        assert CAM_A in getattr(coord, "_stream_worker_dispatch_pending", set())

    def test_second_call_for_same_cam_dedups(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        BoschCameraCoordinator._schedule_stream_worker_error(coord, CAM_A, "msg1")
        BoschCameraCoordinator._schedule_stream_worker_error(coord, CAM_A, "msg2")
        # Only one task scheduled despite two calls
        assert coord.hass.async_create_task.call_count == 1

    def test_different_cams_each_get_a_task(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = _make_coord()
        BoschCameraCoordinator._schedule_stream_worker_error(coord, CAM_A, "msg")
        BoschCameraCoordinator._schedule_stream_worker_error(coord, CAM_B, "msg")
        assert coord.hass.async_create_task.call_count == 2


# ── clear_stream_warming / is_stream_warming (2247-2305) ─────────────────


class TestStreamWarming:
    """Already well-covered in test_coordinator_pure_helpers.py.
    These add idempotency + lazy-init pins."""

    def test_clear_when_not_warming_no_crash(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(_stream_warming=set(), _stream_warming_started={})
        BoschCameraCoordinator.clear_stream_warming(coord, CAM_A)
        # No assertion — just must not crash

    def test_is_warming_returns_false_for_unknown(self):
        from custom_components.bosch_shc_camera import BoschCameraCoordinator
        coord = SimpleNamespace(_stream_warming=set(), _stream_warming_started={})
        assert BoschCameraCoordinator.is_stream_warming(coord, CAM_A) is False
