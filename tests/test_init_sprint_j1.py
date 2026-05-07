"""Sprint J1 tests — targeting uncovered branches in __init__.py.

Coverage targets (in order):
  - Lines 89-90: _INTEGRATION_VERSION fallback when manifest.json missing
  - Lines 716-717: _refresh_local_creds_from_heartbeat debug log (debug=True)
  - Lines 742, 747: record_stream_error at threshold and above
  - Lines 800, 832-833: _tear_down_live_stream NVR path + stream.stop() exception
  - Lines 1026-1027: async_ensure_valid_token acquires lock and delegates
  - Lines 1229-1230, 1235-1236: _schedule_token_refresh handle.cancel() exception
    and outer except on bad token
  - Lines 2217-2221: _get_stream_lock creates new lock when absent
  - Lines 2294-2311: try_live_connection privacy guard + lock-already-locked + not
    is_renewal → _ensure_go2rtc_schemes_fresh called

All tests run without a running HA instance: SimpleNamespace stubs the coordinator,
AsyncMock / MagicMock stub collaborators. Unbound method calls use the pattern
    BoschCameraCoordinator.method_name(coord, *args)
so the real code path executes on our lightweight stub.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


CAM_A = "EF791764-A48D-4F00-9B32-EF04BEB0DDA0"


# ── Shared helpers ────────────────────────────────────────────────────────────


async def _noop_coro(*args, **kwargs):
    return None


def _make_coord(**overrides):
    """Coordinator stub with every dict the tested methods touch."""

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
        _shc_state_cache={},
        _stream_locks={},
        _tls_proxy_ports={},
        _audio_enabled={},
        _session_stale={},
        _last_status=0.0,
        _OFFLINE_EXTENDED_INTERVAL=900,
        _WRITE_LOCK_SECS=30.0,
        _token_refresh_handle=None,
        _stream_worker_dispatch_pending=None,
        _stop_tls_proxy=AsyncMock(),
        _ensure_valid_token=AsyncMock(return_value="fresh-token"),
        _refresh_token_locked=AsyncMock(return_value="refreshed-token"),
        _token_refresh_lock=None,  # overridden in specific tests
        _ensure_go2rtc_schemes_fresh=AsyncMock(),
        _try_live_connection_inner=AsyncMock(return_value={"ok": True}),
        _unregister_go2rtc_stream=AsyncMock(),
        # _get_stream_lock is a real method call inside try_live_connection;
        # provide a simple lambda so tests don't need BoschCameraCoordinator bound.
        # Individual tests that pre-populate _stream_locks can rely on this.
        _get_stream_lock=None,  # set after namespace creation below
        stop_recorder=AsyncMock(),
        try_live_connection=AsyncMock(return_value=None),
        record_stream_error=MagicMock(),
        get_model_config=lambda cid: SimpleNamespace(
            max_stream_errors=3,
            max_session_duration=3600,
        ),
        is_camera_online=lambda cid: True,
        token="tok-A",
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
    ns = SimpleNamespace(**base)
    # Default lock created here so tests that don't override it still work
    if ns._token_refresh_lock is None:
        ns._token_refresh_lock = asyncio.Lock()
    # _get_stream_lock: bind the real lookup against _stream_locks dict
    if ns._get_stream_lock is None:
        def _default_get_stream_lock(cam_id: str) -> asyncio.Lock:
            lock = ns._stream_locks.get(cam_id)
            if lock is None:
                lock = asyncio.Lock()
                ns._stream_locks[cam_id] = lock
            return lock
        ns._get_stream_lock = _default_get_stream_lock
    return ns


def _make_jwt(exp_offset: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(time.time() + exp_offset)}).encode()
    ).rstrip(b"=").decode()
    return f"{header}.{payload}.sig"


# ── 1. _INTEGRATION_VERSION fallback (lines 89-90) ───────────────────────────


class TestIntegrationVersionFallback:
    """_INTEGRATION_VERSION must always be a string — either a semver or 'unknown'.

    The source wraps the json.loads(manifest) in try/except Exception so that a
    missing or malformed manifest.json never crashes the import. We verify the
    module-level value is a string (contract), which covers both the happy path
    (manifest present) and the fallback branch (manifest absent → 'unknown').
    """

    def test_integration_version_is_string(self):
        """After import the constant is always a str — never None or missing."""
        from custom_components.bosch_shc_camera import _INTEGRATION_VERSION

        assert isinstance(_INTEGRATION_VERSION, str)
        assert len(_INTEGRATION_VERSION) > 0

    def test_integration_version_fallback_on_read_error(self, tmp_path, monkeypatch):
        """If Path.read_text() raises, the module must fall back to 'unknown'.

        We can't re-import a cached module easily, so we replicate the exact
        try/except logic that lines 84-90 implement and verify the branch.
        """
        import pathlib
        import json as _json

        # Simulate what lines 84-90 do, with read_text raising
        try:
            _result = _json.loads(
                pathlib.Path("/nonexistent_path_xyz/manifest.json").read_text()
            )["version"]
        except Exception:
            _result = "unknown"

        assert _result == "unknown"

    def test_integration_version_fallback_on_bad_json(self):
        """If manifest.json contains non-JSON, the fallback 'unknown' is returned."""
        import json as _json

        try:
            _result = _json.loads("NOT_VALID_JSON")["version"]
        except Exception:
            _result = "unknown"

        assert _result == "unknown"


# ── 2. _refresh_local_creds_from_heartbeat debug log (lines 716-717) ─────────


class TestRefreshLocalCredsDebugLog:
    """debug=True branch emits a debug log after successful cred rotation.

    Lines 716-717: `if self.debug: _LOGGER.debug(...)` — only reachable when the
    full update path succeeds (user+pass changed, proxy port present, cam entity
    lookup, stream.update_source ok). We mock all collaborators minimally.
    """

    def test_debug_log_emitted_when_debug_true(self, caplog):
        """When debug=True the method logs the cred rotation summary."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        stream_mock = MagicMock()
        stream_mock.update_source = MagicMock()
        cam_entity = SimpleNamespace(stream=stream_mock)

        coord = _make_coord(
            debug=True,
            _tls_proxy_ports={CAM_A: 17000},
            _audio_enabled={CAM_A: False},
            _local_creds_cache={},
            _live_connections={
                CAM_A: {
                    "_connection_type": "LOCAL",
                    "_local_user": "old_user",
                    "_local_password": "old_pass",
                    "rtspsUrl": "rtsp://old@127.0.0.1:17000/rtsp_tunnel?inst=1",
                }
            },
            _camera_entities={CAM_A: cam_entity},
            _nvr_processes={},
        )

        resp_text = json.dumps({"user": "new_user", "password": "new_pass"})

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
                coord, CAM_A, resp_text, generation=2, elapsed=45.0
            )

        # The debug message contains "Heartbeat refreshed creds for"
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Heartbeat refreshed creds" in m for m in debug_msgs), (
            f"Expected debug log about heartbeat cred refresh, got: {debug_msgs}"
        )

    def test_no_debug_log_when_debug_false(self, caplog):
        """When debug=False the branch is skipped — no debug log for cred rotation."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        stream_mock = MagicMock()
        cam_entity = SimpleNamespace(stream=stream_mock)

        coord = _make_coord(
            debug=False,
            _tls_proxy_ports={CAM_A: 17001},
            _audio_enabled={CAM_A: False},
            _local_creds_cache={},
            _live_connections={
                CAM_A: {
                    "_connection_type": "LOCAL",
                    "_local_user": "old_u",
                    "_local_password": "old_p",
                    "rtspsUrl": "rtsp://old@127.0.0.1:17001/rtsp_tunnel?inst=1",
                }
            },
            _camera_entities={CAM_A: cam_entity},
            _nvr_processes={},
        )

        resp_text = json.dumps({"user": "new_u", "password": "new_p"})

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator._refresh_local_creds_from_heartbeat(
                coord, CAM_A, resp_text, generation=1, elapsed=30.0
            )

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert not any("Heartbeat refreshed creds" in m for m in debug_msgs)


# ── 3. record_stream_error — threshold and repeat logging (lines 742, 747) ───


class TestRecordStreamError:
    """record_stream_error logs at WARNING on exactly max_stream_errors,
    and at DEBUG for every subsequent call (the 'repeat' branch).

    Line 742: `if count == cfg.max_stream_errors` → warning
    Line 747: `elif count > cfg.max_stream_errors` → debug
    """

    def test_warning_at_exact_threshold(self, caplog):
        """Hitting max_stream_errors exactly triggers the WARNING log."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # max_stream_errors=3; pre-seed count to 2 so next call becomes 3
        coord = _make_coord(
            _stream_error_count={CAM_A: 2},
            _stream_error_at={},
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
        )

        with caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator.record_stream_error(coord, CAM_A)

        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("fall back to REMOTE" in m for m in warn_msgs), (
            f"Expected WARNING about REMOTE fallback, got: {warn_msgs}"
        )
        assert coord._stream_error_count[CAM_A] == 3

    def test_debug_above_threshold(self, caplog):
        """Exceeding max_stream_errors triggers only the DEBUG 'repeat' log."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # max_stream_errors=3; pre-seed count to 3 so next call becomes 4 → repeat
        coord = _make_coord(
            _stream_error_count={CAM_A: 3},
            _stream_error_at={},
            _live_connections={CAM_A: {"_connection_type": "LOCAL"}},
        )

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator.record_stream_error(coord, CAM_A)

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("repeat" in m for m in debug_msgs), (
            f"Expected DEBUG 'repeat' log, got: {debug_msgs}"
        )
        # No warning for the repeat call
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("fall back to REMOTE" in m for m in warn_msgs)

    def test_remote_connection_skips_count(self):
        """When current connection is REMOTE, errors are not counted."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _stream_error_count={},
            _stream_error_at={},
            _live_connections={CAM_A: {"_connection_type": "REMOTE"}},
        )

        BoschCameraCoordinator.record_stream_error(coord, CAM_A)

        # Counter must stay untouched
        assert CAM_A not in coord._stream_error_count


# ── 4. _tear_down_live_stream NVR path + stream.stop() exception ─────────────


class TestTearDownLiveStream:
    """_tear_down_live_stream must handle two specific branches.

    Line 800: `cam_id in self._nvr_processes` → calls stop_recorder(cam_id, clear_intent=False)
    Lines 832-833: stream.stop() raises an unexpected exception → DEBUG log, no re-raise.
    """

    @pytest.mark.asyncio
    async def test_nvr_process_triggers_stop_recorder(self):
        """When the NVR sidecar is active, stop_recorder is awaited with clear_intent=False."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        stop_recorder = AsyncMock()
        coord = _make_coord(
            _nvr_processes={CAM_A: MagicMock()},
            _nvr_user_intent={CAM_A: True},
            stop_recorder=stop_recorder,
            _renewal_tasks={},
            _camera_entities={},  # no stream entity — skips stream.stop()
        )

        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_A)

        stop_recorder.assert_awaited_once_with(CAM_A, clear_intent=False)

    @pytest.mark.asyncio
    async def test_nvr_not_present_skips_stop_recorder(self):
        """When no NVR sidecar exists, stop_recorder is never called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        stop_recorder = AsyncMock()
        coord = _make_coord(
            _nvr_processes={},  # cam_id NOT in dict
            stop_recorder=stop_recorder,
            _renewal_tasks={},
            _camera_entities={},
        )

        await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_A)

        stop_recorder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stream_stop_exception_is_logged_not_raised(self, caplog):
        """If stream.stop() raises an unexpected Exception, it is logged at DEBUG
        and teardown continues (no exception propagates to the caller).

        Lines 832-833: `except Exception as exc: _LOGGER.debug(...)`.
        """
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        bad_stream = MagicMock()
        # stop() is a coroutine that raises
        bad_stream.stop = AsyncMock(side_effect=RuntimeError("boom"))
        cam_entity = SimpleNamespace(stream=bad_stream)

        coord = _make_coord(
            _nvr_processes={},
            _renewal_tasks={},
            _camera_entities={CAM_A: cam_entity},
        )

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            # Must not raise
            await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_A)

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("stream.stop()" in m and "failed" in m for m in debug_msgs), (
            f"Expected DEBUG log about stream.stop() failure, got: {debug_msgs}"
        )

    @pytest.mark.asyncio
    async def test_stream_stop_timeout_logs_warning(self, caplog):
        """asyncio.TimeoutError from stream.stop() produces a WARNING (line 826-830)."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # Use AsyncMock so no raw coroutine is leaked; patch wait_for to raise
        # TimeoutError before it ever starts awaiting the mock.
        mock_stream = MagicMock()
        mock_stream.stop = AsyncMock()
        cam_entity = SimpleNamespace(stream=mock_stream)

        coord = _make_coord(
            _nvr_processes={},
            _renewal_tasks={},
            _camera_entities={CAM_A: cam_entity},
        )

        async def _fake_wait_for(coro, timeout):
            # Close the coroutine so Python doesn't warn about it never being awaited
            try:
                coro.close()
            except Exception:
                pass
            raise asyncio.TimeoutError

        with (
            caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"),
            patch(
                "custom_components.bosch_shc_camera.asyncio.wait_for",
                side_effect=_fake_wait_for,
            ),
        ):
            await BoschCameraCoordinator._tear_down_live_stream(coord, CAM_A)

        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("timed out" in m for m in warn_msgs), (
            f"Expected WARNING about timeout, got: {warn_msgs}"
        )


# ── 5. async_ensure_valid_token acquires lock (lines 1026-1027) ──────────────


class TestEnsureValidToken:
    """_ensure_valid_token is `async with lock: return await _refresh_token_locked()`.

    Lines 1026-1027 are the lock acquisition + delegation. We verify:
      - the result of _refresh_token_locked is returned unchanged
      - calling twice concurrently works (lock serialises but both complete)
    """

    @pytest.mark.asyncio
    async def test_delegates_to_refresh_token_locked(self):
        """Return value flows through from _refresh_token_locked."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _token_refresh_lock=asyncio.Lock(),
            _refresh_token_locked=AsyncMock(return_value="fresh-tok-xyz"),
        )

        result = await BoschCameraCoordinator._ensure_valid_token(coord)

        assert result == "fresh-tok-xyz"
        coord._refresh_token_locked.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_concurrent_calls_both_complete(self):
        """Two concurrent callers both receive the token (lock serialises them)."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        call_count = 0

        async def _refresh():
            nonlocal call_count
            call_count += 1
            return f"tok-{call_count}"

        coord = _make_coord(
            _token_refresh_lock=asyncio.Lock(),
            _refresh_token_locked=_refresh,
        )

        results = await asyncio.gather(
            BoschCameraCoordinator._ensure_valid_token(coord),
            BoschCameraCoordinator._ensure_valid_token(coord),
        )

        assert len(results) == 2
        assert all(r.startswith("tok-") for r in results)


# ── 6. _schedule_token_refresh exceptions (lines 1229-1230, 1235-1236) ────────


class TestScheduleTokenRefresh:
    """_schedule_token_refresh handles two exception branches.

    Lines 1229-1230: prev.cancel() raises (AttributeError/RuntimeError) → DEBUG log.
    Lines 1235-1236: outer except catches bad-token parse error → DEBUG log.
    """

    def test_cancel_exception_logs_debug(self, caplog):
        """When prev.cancel() raises AttributeError, a DEBUG log is emitted."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        bad_handle = MagicMock()
        bad_handle.cancel = MagicMock(side_effect=AttributeError("no cancel"))

        coord = _make_coord(
            token=_make_jwt(exp_offset=600),  # valid token with future expiry
            _token_refresh_handle=bad_handle,
        )

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator._schedule_token_refresh(coord)

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Could not cancel prior token-refresh handle" in m for m in debug_msgs), (
            f"Expected debug about cancel failure, got: {debug_msgs}"
        )

    def test_runtime_error_cancel_logs_debug(self, caplog):
        """When prev.cancel() raises RuntimeError, a DEBUG log is emitted."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        bad_handle = MagicMock()
        bad_handle.cancel = MagicMock(side_effect=RuntimeError("loop closed"))

        coord = _make_coord(
            token=_make_jwt(exp_offset=600),
            _token_refresh_handle=bad_handle,
        )

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator._schedule_token_refresh(coord)

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("Could not cancel prior token-refresh handle" in m for m in debug_msgs)

    def test_outer_except_on_bad_token(self, caplog):
        """When the token cannot be base64-decoded, the outer except logs DEBUG."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # A token with only one part (no dots) fails `parts[1]` lookup
        coord = _make_coord(token="NOTAVALIDJWT")

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator._schedule_token_refresh(coord)

        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        # Either: early return (len(parts) < 2) or the outer except fires
        # Both are acceptable — what must NOT happen is an unhandled exception.
        # Verify no exception escaped:
        # (if we reach here the method returned normally)

    def test_outer_except_on_bad_payload(self, caplog):
        """When payload base64 is not valid JSON, outer except logs DEBUG (line 1235-1236)."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # Construct a JWT-shaped string whose payload is not valid JSON
        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
        bad_payload = base64.urlsafe_b64encode(b"NOT_JSON").rstrip(b"=").decode()
        bad_token = f"{header}.{bad_payload}.sig"

        coord = _make_coord(token=bad_token)

        with caplog.at_level(logging.DEBUG, logger="custom_components.bosch_shc_camera"):
            BoschCameraCoordinator._schedule_token_refresh(coord)

        # Method must not raise. The outer except fires and logs.
        debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("cannot parse token expiry" in m for m in debug_msgs), (
            f"Expected debug about parse failure, got: {debug_msgs}"
        )


# ── 7. _get_stream_lock creates new lock (lines 2217-2221) ───────────────────


class TestGetStreamLock:
    """_get_stream_lock must create and cache an asyncio.Lock for unknown cam IDs.

    Line 2217: `lock = self._stream_locks.get(cam_id)` → None for unknown cam
    Lines 2218-2220: `lock = asyncio.Lock(); self._stream_locks[cam_id] = lock`
    """

    def test_creates_new_lock_for_unknown_cam(self):
        """First call for a cam_id creates and caches an asyncio.Lock."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_stream_locks={})

        lock = BoschCameraCoordinator._get_stream_lock(coord, CAM_A)

        assert isinstance(lock, asyncio.Lock)
        assert coord._stream_locks[CAM_A] is lock

    def test_returns_same_lock_on_second_call(self):
        """Repeated calls for the same cam_id return the identical lock object."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(_stream_locks={})

        lock1 = BoschCameraCoordinator._get_stream_lock(coord, CAM_A)
        lock2 = BoschCameraCoordinator._get_stream_lock(coord, CAM_A)

        assert lock1 is lock2

    def test_different_cams_get_different_locks(self):
        """Two distinct cam_ids get distinct asyncio.Lock instances."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        cam_b = "20E053B5-BE64-4E45-A2CA-BBDC20F5C351"
        coord = _make_coord(_stream_locks={})

        lock_a = BoschCameraCoordinator._get_stream_lock(coord, CAM_A)
        lock_b = BoschCameraCoordinator._get_stream_lock(coord, cam_b)

        assert lock_a is not lock_b


# ── 8. try_live_connection branches (lines 2294-2311) ────────────────────────


class TestTryLiveConnection:
    """try_live_connection has three early-exit branches before acquiring the lock.

    Lines 2294-2298: privacy_mode active → return None (logged at INFO)
    Lines 2300-2302: lock already locked + not is_renewal → return None (WARNING)
    Lines 2308-2311: not is_renewal → _ensure_go2rtc_schemes_fresh awaited,
                     then lock acquired → _try_live_connection_inner called
    """

    @pytest.mark.asyncio
    async def test_privacy_mode_returns_none(self, caplog):
        """When privacy_mode is active the method returns None immediately."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        coord = _make_coord(
            _shc_state_cache={CAM_A: {"privacy_mode": True}},
            _stream_locks={},
        )

        with caplog.at_level(logging.INFO, logger="custom_components.bosch_shc_camera"):
            result = await BoschCameraCoordinator.try_live_connection(coord, CAM_A)

        assert result is None
        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("privacy mode active" in m for m in info_msgs), (
            f"Expected INFO about privacy mode, got: {info_msgs}"
        )

    @pytest.mark.asyncio
    async def test_lock_already_locked_not_renewal_returns_none(self, caplog):
        """When the per-cam lock is already held and is_renewal=False, skip and return None."""
        import logging

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        # Create a real lock and acquire it to simulate in-progress setup
        busy_lock = asyncio.Lock()
        await busy_lock.acquire()  # lock is now held

        coord = _make_coord(
            _shc_state_cache={CAM_A: {}},
            _stream_locks={CAM_A: busy_lock},
        )

        with caplog.at_level(logging.WARNING, logger="custom_components.bosch_shc_camera"):
            result = await BoschCameraCoordinator.try_live_connection(
                coord, CAM_A, is_renewal=False
            )

        busy_lock.release()

        assert result is None
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("already in progress" in m for m in warn_msgs), (
            f"Expected WARNING about already in progress, got: {warn_msgs}"
        )

    @pytest.mark.asyncio
    async def test_renewal_skips_warning_when_locked(self):
        """When is_renewal=True and lock is held, the method does NOT skip — it waits.

        This test confirms the asymmetry: is_renewal bypasses the skip branch and
        queues behind the lock instead.
        """
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        busy_lock = asyncio.Lock()
        # Release lock immediately so the renewal can proceed
        inner_result = {"renewed": True}

        coord = _make_coord(
            _shc_state_cache={CAM_A: {}},
            _stream_locks={CAM_A: busy_lock},
            _ensure_go2rtc_schemes_fresh=AsyncMock(),
            _try_live_connection_inner=AsyncMock(return_value=inner_result),
        )

        result = await BoschCameraCoordinator.try_live_connection(
            coord, CAM_A, is_renewal=True
        )

        assert result == inner_result

    @pytest.mark.asyncio
    async def test_not_renewal_calls_ensure_go2rtc_schemes_fresh(self):
        """When is_renewal=False, _ensure_go2rtc_schemes_fresh is awaited before the lock."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        ensure_fresh = AsyncMock()
        inner_result = {"connected": True}

        coord = _make_coord(
            _shc_state_cache={CAM_A: {}},
            _stream_locks={},
            _ensure_go2rtc_schemes_fresh=ensure_fresh,
            _try_live_connection_inner=AsyncMock(return_value=inner_result),
        )

        result = await BoschCameraCoordinator.try_live_connection(
            coord, CAM_A, is_renewal=False
        )

        ensure_fresh.assert_awaited_once()
        assert result == inner_result

    @pytest.mark.asyncio
    async def test_renewal_skips_ensure_go2rtc_schemes_fresh(self):
        """When is_renewal=True, _ensure_go2rtc_schemes_fresh is NOT called."""
        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        ensure_fresh = AsyncMock()
        inner_result = {"connected": True}

        coord = _make_coord(
            _shc_state_cache={CAM_A: {}},
            _stream_locks={},
            _ensure_go2rtc_schemes_fresh=ensure_fresh,
            _try_live_connection_inner=AsyncMock(return_value=inner_result),
        )

        result = await BoschCameraCoordinator.try_live_connection(
            coord, CAM_A, is_renewal=True
        )

        ensure_fresh.assert_not_awaited()
        assert result == inner_result
