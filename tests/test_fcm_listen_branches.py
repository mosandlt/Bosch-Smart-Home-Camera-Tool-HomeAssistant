"""Cover the various branches inside `_QuietFcmPushClient._listen`.

The existing `test_listen_override_sets_resetting_before_log_decision`
in test_fcm_round5.py covers the happy quiet-path
(`ConnectionResetError → _log_verbose`). This file pins the remaining
branches:

- SSLError with non-CLOSE_NOTIFY reason → `_log_warn_with_limit`
- Generic OSError (not in the quiet-set) → `_logger.exception` + error
  counter increment + `_reset`
- ImportError on `ErrorType` lookup → fallback bare `_reset`
- Outer `except Exception` → `_terminate` + writer close
- `_connect_with_retry` returning False → early return
"""

from __future__ import annotations

import ssl

import pytest


def _make_listen_instance():
    """Build a minimal `_Patched` instance with every protected hook
    stubbed so we can drive `_listen` through one specific branch."""
    pytest.importorskip("firebase_messaging")
    from firebase_messaging import FcmPushClientRunState

    from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

    cls = _QuietFcmPushClient._patch_class()
    assert cls is not None
    instance = object.__new__(cls)
    instance.run_state = FcmPushClientRunState.STARTED
    instance.do_listen = True

    async def _noop_async():
        return None

    instance._login = _noop_async
    instance._handle_message = _noop_async
    instance._do_writer_close = _noop_async
    instance._log_warn_with_limit = lambda *a, **k: None
    instance._log_verbose = lambda *a, **k: None
    instance._terminate = lambda: None
    instance._try_increment_error_count = lambda _et: True

    async def _ok_connect():
        return True

    instance._connect_with_retry = _ok_connect

    async def _reset():
        instance.do_listen = False

    instance._reset = _reset

    return instance, FcmPushClientRunState


@pytest.mark.asyncio
class TestListenBranches:
    async def test_connect_failure_returns_early(self):
        """`_connect_with_retry()` returning False makes _listen return
        immediately without touching _login or the loop. Pins L230-231."""
        instance, _ = _make_listen_instance()

        async def _bad_connect():
            return False

        instance._connect_with_retry = _bad_connect

        login_calls = []

        async def _login():
            login_calls.append(1)

        instance._login = _login

        async def _receive():
            return None

        instance._receive_msg = _receive

        await instance._listen()
        assert login_calls == [], "must return before calling _login"

    async def test_ssl_error_with_non_close_notify_reason_logs_warn(self):
        """SSLError with a reason that's NOT
        'APPLICATION_DATA_AFTER_CLOSE_NOTIFY' goes through
        `_log_warn_with_limit` instead of the quiet `_log_verbose` path.
        Pins L274."""
        instance, _ = _make_listen_instance()

        warn_calls = []
        instance._log_warn_with_limit = lambda *a, **k: (
            warn_calls.append(a) or instance.__setattr__("do_listen", False)
        )

        called = [0]

        async def _receive():
            called[0] += 1
            if called[0] == 1:
                err = ssl.SSLError("simulated")
                err.reason = "DECRYPT_ERROR"  # not CLOSE_NOTIFY
                raise err
            return None

        instance._receive_msg = _receive

        await instance._listen()
        assert warn_calls, (
            "SSLError with non-CLOSE_NOTIFY reason must route through "
            "_log_warn_with_limit"
        )

    async def test_generic_oserror_takes_else_branch(self):
        """A generic `OSError` that is NOT in (ConnectionResetError,
        TimeoutError, IncompleteReadError, SSLError) falls through to the
        else branch: `_logger.exception` + error-counter increment +
        `_reset`. Pins L284 + L289-290 + L293."""
        instance, _ = _make_listen_instance()

        reset_calls = []

        async def _reset():
            reset_calls.append(1)
            instance.do_listen = False

        instance._reset = _reset

        called = [0]

        async def _receive():
            called[0] += 1
            if called[0] == 1:
                raise OSError("ENOSYS — not the quiet quartet")
            return None

        instance._receive_msg = _receive

        # Stub the upstream ErrorType import successfully so the
        # try_increment_error_count branch runs.
        import sys
        from types import SimpleNamespace

        sys.modules.setdefault(
            "firebase_messaging.fcmpushclient",
            SimpleNamespace(ErrorType=SimpleNamespace(CONNECTION="conn")),
        )

        # Capture exception log so it can be asserted on.
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        original_exc = fcm_mod._LOGGER.exception
        captured = []
        fcm_mod._LOGGER.exception = lambda *a, **k: captured.append(a)
        try:
            await instance._listen()
        finally:
            fcm_mod._LOGGER.exception = original_exc

        assert captured, "_logger.exception must fire on the else branch"
        assert reset_calls, "_reset must run after the error counter increments"

    async def test_outer_exception_calls_terminate(self):
        """An unhandled exception in `_login` (or the message loop)
        bubbles to the outer `except Exception: _terminate()` arm.
        Pins L302+ + L311 (the writer-close finally)."""
        instance, _ = _make_listen_instance()

        terminate_calls = []
        instance._terminate = lambda: terminate_calls.append(1)

        writer_close_calls = []

        async def _writer_close():
            writer_close_calls.append(1)

        instance._do_writer_close = _writer_close

        async def _bad_login():
            raise RuntimeError("upstream login bug")

        instance._login = _bad_login

        await instance._listen()
        assert terminate_calls, "_terminate must fire on outer except"
        assert writer_close_calls, "_do_writer_close must run in finally"

    async def test_resetting_state_takes_sleep_branch(self):
        """While `run_state == RESETTING`, the loop must `asyncio.sleep(1)`
        instead of consuming a message. Pins fcm.py L239."""
        from unittest.mock import patch

        instance, RunState = _make_listen_instance()
        instance.run_state = RunState.RESETTING

        slept = []

        async def _fast_sleep(secs):
            slept.append(secs)
            instance.do_listen = False

        async def _bad_receive():
            raise AssertionError("RESETTING path must not call _receive_msg")

        instance._receive_msg = _bad_receive

        with patch("asyncio.sleep", new=_fast_sleep):
            await instance._listen()
        assert slept == [1], "must sleep 1 s while RESETTING"

    async def test_message_received_dispatches_to_handle_message(self):
        """A non-empty `_receive_msg()` return invokes
        `_handle_message(msg)`. Pins fcm.py L241."""
        instance, _ = _make_listen_instance()

        handled = []

        async def _handle(msg):
            handled.append(msg)
            instance.do_listen = False

        instance._handle_message = _handle

        async def _receive():
            return b"FCM_PAYLOAD"

        instance._receive_msg = _receive

        await instance._listen()
        assert handled == [b"FCM_PAYLOAD"]

    async def test_error_type_import_failure_falls_back_to_reset(self):
        """If `from firebase_messaging.fcmpushclient import ErrorType`
        fails (future library refactor), the ImportError arm must still
        call `_reset()`. Pins fcm.py L297-298."""
        from unittest.mock import patch

        instance, _ = _make_listen_instance()

        reset_calls = []

        async def _reset():
            reset_calls.append(1)
            instance.do_listen = False

        instance._reset = _reset

        called = [0]

        async def _receive():
            called[0] += 1
            if called[0] == 1:
                raise OSError("ENOSYS — not in the quiet quartet")
            return None

        instance._receive_msg = _receive

        # Force the inner import to raise.
        import builtins as _bi

        real = _bi.__import__

        def _fake(name, globs=None, locs=None, fromlist=(), level=0):
            if name == "firebase_messaging.fcmpushclient":
                raise ImportError("simulated library refactor")
            return real(name, globs, locs, fromlist, level)

        with patch("builtins.__import__", side_effect=_fake):
            await instance._listen()

        assert reset_calls, "_reset must still fire even when ErrorType import fails"
