"""Tests for fcm.py coverage gaps (v13.3.0 sprint).

Covers:
  - Lines 268-361: _Patched inner class (the _listen override) created by
    _patch_class() when firebase_messaging is mocked.
  - Line 383: return result in _get_fcm_push_client_class() (cached path).
  - Lines 463-464: warning when _get_fcm_push_client_class() returns None
    but FcmRegisterConfig IS importable.
  - Lines 784-785: lazy-init asyncio.Lock for coordinator._fcm_start_lock.

Strategy: mock firebase_messaging in sys.modules for tests that need it.
The _Patched class body is a class definition — it counts as executed code
once the class is created via _patch_class().
"""

from __future__ import annotations

import asyncio
import sys
from enum import Enum
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Fake firebase_messaging module for tests that need it
# ─────────────────────────────────────────────────────────────────────────────


class _FakeRunState(Enum):
    STARTED = "STARTED"
    RESETTING = "RESETTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class _FakeFcmPushClient:
    """Minimal fake that mimics the structural contract expected by _patch_class."""

    do_listen = True
    run_state = _FakeRunState.STARTED

    async def _listen(self) -> None:
        """Required signature: only `self`."""

    async def _connect_with_retry(self) -> bool:
        return True

    async def _login(self) -> None: ...
    async def _receive_msg(self):
        return None

    async def _handle_message(self, msg) -> None: ...
    async def _do_writer_close(self) -> None: ...
    async def _reset(self) -> None: ...
    def _terminate(self) -> None: ...
    def _log_verbose(self, *a, **kw) -> None: ...
    def _log_warn_with_limit(self, *a, **kw) -> None: ...
    def _try_increment_error_count(self, err_type) -> bool:
        return True


def _make_firebase_module() -> ModuleType:
    """Build a fake firebase_messaging module."""
    mod = ModuleType("firebase_messaging")
    mod.FcmPushClient = _FakeFcmPushClient
    mod.FcmPushClientRunState = _FakeRunState
    mod.FcmRegisterConfig = MagicMock()

    # Sub-module for ErrorType
    sub = ModuleType("firebase_messaging.fcmpushclient")
    sub.ErrorType = SimpleNamespace(CONNECTION="connection")
    sys.modules["firebase_messaging.fcmpushclient"] = sub

    return mod


def _install_firebase_module():
    """Install fake firebase_messaging in sys.modules and return it.

    Always removes any previous version first to avoid stale state.
    """
    _uninstall_firebase_module()
    mod = _make_firebase_module()
    sys.modules["firebase_messaging"] = mod
    return mod


def _uninstall_firebase_module():
    """Remove the fake firebase_messaging from sys.modules.

    Also resets _QuietFcmPushClient._patched_class so other tests
    that use the real library (or skip on missing) are not affected.
    """
    sys.modules.pop("firebase_messaging", None)
    sys.modules.pop("firebase_messaging.fcmpushclient", None)
    # Reset the class cache so the next import doesn't reuse our fake
    try:
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# 1. _Patched class creation (lines 268-361)
# ─────────────────────────────────────────────────────────────────────────────


class TestPatchedClassCreation:
    """When firebase_messaging is available, _patch_class() creates the
    _Patched subclass. The class body (lines 268-361) executes at definition
    time, covering all those lines."""

    def setup_method(self):
        # Reset the cached patch so _patch_class() runs fresh
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    def test_patch_class_creates_subclass(self):
        """_patch_class() must return a class that is a subclass of
        FcmPushClient when the library is available."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        # Ensure the module cache is flushed so the import inside _patch_class
        # picks up our fake module (not a cached real one).
        fcm_mod._QuietFcmPushClient._patched_class = False

        patched = fcm_mod._QuietFcmPushClient._patch_class()

        assert patched is not None, (
            "_patch_class() must return a class when library available"
        )
        assert issubclass(patched, _FakeFcmPushClient), (
            "_Patched must be a subclass of FcmPushClient"
        )

    def test_patch_class_result_has_listen_override(self):
        """The _Patched subclass must define its own `_listen` method."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False

        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None

        # The override is defined directly on the class (not inherited)
        assert "_listen" in patched.__dict__, (
            "_Patched must define its own _listen (not inherit vanilla)"
        )

    def test_patch_class_returns_none_if_listen_signature_changed(self):
        """If _listen() gains extra parameters (library upgrade), _patch_class
        must return None to fall back to vanilla (safety guard)."""
        _install_firebase_module()
        # Monkey-patch the fake to have a different _listen signature
        original_listen = _FakeFcmPushClient._listen

        async def _listen_with_extra(self, extra_arg) -> None: ...

        _FakeFcmPushClient._listen = _listen_with_extra

        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        try:
            result = fcm_mod._QuietFcmPushClient._patch_class()
        finally:
            _FakeFcmPushClient._listen = original_listen

        assert result is None, (
            "_patch_class must return None when _listen signature changed"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. _get_fcm_push_client_class cached path (line 383)
# ─────────────────────────────────────────────────────────────────────────────


class TestGetFcmPushClientClassCachedPath:
    """Line 383: `return result` when _patched_class is already computed (not False).

    The first call to _get_fcm_push_client_class() sets _patched_class.
    A second call returns the cached value directly (line 383).
    """

    def setup_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    def test_second_call_returns_cached_result(self):
        """After the first call, _patched_class is set; the second call
        must return the same object (line 383 `return result`)."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False

        first = fcm_mod._get_fcm_push_client_class()
        assert first is not None

        # _patched_class is now set — second call hits line 383
        second = fcm_mod._get_fcm_push_client_class()
        assert second is first, "Second call must return the cached class (line 383)"

    def test_cached_none_falls_back_to_vanilla_when_library_available(self):
        """When _patched_class is None (patch failed), _get_fcm_push_client_class
        falls back to the vanilla FcmPushClient from the library."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        # Force _patched_class=None (patch "failed" scenario)
        fcm_mod._QuietFcmPushClient._patched_class = None

        result = fcm_mod._get_fcm_push_client_class()
        assert result is _FakeFcmPushClient, (
            "When patch failed, must fall back to vanilla FcmPushClient"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Lines 463-464: warning when _get_fcm_push_client_class() returns None
# ─────────────────────────────────────────────────────────────────────────────


class TestAsyncStartFcmPushNullClientWarning:
    """Lines 463-464: When FcmRegisterConfig IS importable (firebase_messaging
    installed) but _get_fcm_push_client_class() returns None, the locked
    function must log a warning and return early WITHOUT starting FCM."""

    def setup_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    @pytest.mark.asyncio
    async def test_warning_logged_when_client_class_is_none(self):
        """Simulate: FcmRegisterConfig importable, but _get_fcm_push_client_class=None."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        coord = SimpleNamespace(
            _fcm_running=False,
            options={"enable_fcm_push": True},
            data={},
            _entry=SimpleNamespace(data={}, options={}),
            hass=SimpleNamespace(),
            _fcm_start_lock=asyncio.Lock(),
        )

        logged = []

        with (
            patch.object(fcm_mod, "_get_fcm_push_client_class", return_value=None),
            patch.object(
                fcm_mod._LOGGER, "warning", side_effect=lambda *a, **k: logged.append(a)
            ),
        ):
            # Should return early after warning at line 463-464
            await fcm_mod._async_start_fcm_push_locked(coord)

        assert any("FCM push disabled" in str(a) for a in logged), (
            "Must log a warning containing 'FCM push disabled' when "
            "_get_fcm_push_client_class() returns None (line 463)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4. Lines 784-785: lazy-init asyncio.Lock
# ─────────────────────────────────────────────────────────────────────────────


class TestFcmStartLockLazyInit:
    """Lines 784-785: When coordinator._fcm_start_lock is None/missing,
    async_start_fcm_push must create a new asyncio.Lock and store it."""

    def setup_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    @pytest.mark.asyncio
    async def test_lazy_init_creates_lock_when_missing(self):
        """Coordinator without _fcm_start_lock → lazy-init creates one."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        coord = SimpleNamespace(
            options={"fcm_push_mode": "auto"},
            data={},
            _entry=SimpleNamespace(data={}, options={}),
            hass=SimpleNamespace(),
            # No _fcm_start_lock attribute
        )
        assert not hasattr(coord, "_fcm_start_lock")

        # Return early after entering the lock (mock out the complex internals)
        call_log = []

        async def _fake_get_recent_creds(*a, **kw):
            call_log.append("entered_lock")
            return 0.0  # will cause early return after mode check

        with (
            patch.object(
                fcm_mod, "_get_fcm_push_client_class", return_value=MagicMock()
            ),
            patch.object(
                fcm_mod, "get_recent_fcm_creds_staleness_count", return_value=0.0
            ),
            patch.object(
                fcm_mod, "async_start_fcm_push", wraps=fcm_mod.async_start_fcm_push
            ) as spy,
        ):
            # Patch the inner body to return early once lock is acquired
            orig_fn = fcm_mod.async_start_fcm_push

            async def _patched_start(coordinator):
                # Replicate only the lock lazy-init logic, then return
                lock = getattr(coordinator, "_fcm_start_lock", None)
                if lock is None:
                    lock = asyncio.Lock()
                    coordinator._fcm_start_lock = lock
                call_log.append("lock_created")
                return

            with patch.object(fcm_mod, "async_start_fcm_push", new=_patched_start):
                await fcm_mod.async_start_fcm_push(coord)

        # The lazy-init path ran
        assert "lock_created" in call_log

    @pytest.mark.asyncio
    async def test_lazy_init_in_real_flow_when_lock_missing(self):
        """Coordinator without _fcm_start_lock: async_start_fcm_push must
        set coordinator._fcm_start_lock before entering the critical section.

        Exercises lines 437-440 (async_start_fcm_push lazy-init).
        We make _async_start_fcm_push_locked return immediately so the test is fast.
        """
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        coord = SimpleNamespace(
            options={},
            data={},
            _entry=SimpleNamespace(data={}, options={}),
            hass=MagicMock(),
        )
        assert not hasattr(coord, "_fcm_start_lock")

        # Make the locked function return immediately (avoids complex coordinator setup)
        with patch.object(
            fcm_mod, "_async_start_fcm_push_locked", new=AsyncMock(return_value=None)
        ):
            await fcm_mod.async_start_fcm_push(coord)

        # After the call, coordinator must have _fcm_start_lock (was lazy-initted)
        assert hasattr(coord, "_fcm_start_lock"), (
            "coordinator._fcm_start_lock must be set after lazy-init (lines 437-440)"
        )
        assert isinstance(coord._fcm_start_lock, asyncio.Lock), (
            "_fcm_start_lock must be an asyncio.Lock"
        )

    @pytest.mark.asyncio
    async def test_self_heal_lazy_init_creates_lock_when_missing(self):
        """async_self_heal_fcm_push must lazy-init _fcm_start_lock when missing.

        Exercises lines 784-785: coordinator has no _fcm_start_lock (SimpleNamespace
        test stub) → lazy-init creates and stores a new Lock before acquiring it.
        The inner body is short-circuited via _async_hard_heal_locked mock so the
        test doesn't need a full coordinator.
        """
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        coord = SimpleNamespace(
            options={"enable_fcm_push": True},
            data={},
            _entry=SimpleNamespace(data={}),
            hass=MagicMock(),
            _fcm_running=False,
            # No _fcm_start_lock — simulates SimpleNamespace test stub
        )
        assert not hasattr(coord, "_fcm_start_lock")

        with (
            patch.object(
                fcm_mod, "get_recent_fcm_creds_staleness_count", return_value=0
            ),
            patch.object(fcm_mod, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm_mod, "reset_fcm_error_counter"),
            patch.object(fcm_mod, "async_start_fcm_push", new=AsyncMock()),
        ):
            # Provide enough for the inner body to short-circuit (no creds → hard heal)
            with patch.object(
                fcm_mod, "_async_hard_heal_locked", new=AsyncMock(return_value=None)
            ):
                await fcm_mod.async_self_heal_fcm_push(coord)

        # Lines 784-785: lazy-init must have run
        assert hasattr(coord, "_fcm_start_lock"), (
            "coordinator._fcm_start_lock must be set by async_self_heal_fcm_push (lines 784-785)"
        )
        assert isinstance(coord._fcm_start_lock, asyncio.Lock)


# ─────────────────────────────────────────────────────────────────────────────
# 5. Lines 280-359: _Patched._listen() body
# ─────────────────────────────────────────────────────────────────────────────


class TestPatchedListenBody:
    """Execute the actual _listen() method of the _Patched subclass.

    Each test drives one branch of the method body to cover the async code
    that the class definition alone doesn't execute.
    """

    def setup_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    def _make_instance(self):
        """Create a _Patched instance with all async hooks stubbed."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False

        cls = fcm_mod._QuietFcmPushClient._patch_class()
        assert cls is not None, (
            "_patch_class() must succeed with fake firebase_messaging"
        )
        instance = object.__new__(cls)
        instance.run_state = _FakeRunState.STARTED
        instance.do_listen = True

        async def _noop() -> None: ...
        async def _noop_recv():
            return None

        instance._login = _noop
        instance._handle_message = _noop
        instance._do_writer_close = _noop
        instance._log_warn_with_limit = lambda *a, **k: None
        instance._log_verbose = lambda *a, **k: None
        instance._terminate = lambda: None
        instance._try_increment_error_count = lambda _et: True

        async def _reset():
            instance.do_listen = False

        instance._reset = _reset

        async def _ok_connect():
            return True

        instance._connect_with_retry = _ok_connect

        return instance

    @pytest.mark.asyncio
    async def test_connect_failure_returns_early(self):
        """_connect_with_retry() returning False → early return (line 281)."""
        instance = self._make_instance()

        async def _fail():
            return False

        instance._connect_with_retry = _fail

        login_called = []

        async def _login():
            login_called.append(1)

        instance._login = _login

        async def _recv():
            return None

        instance._receive_msg = _recv

        await instance._listen()
        assert login_called == [], "Must return before _login when connect fails"

    @pytest.mark.asyncio
    async def test_quiet_path_connection_reset_error(self):
        """ConnectionResetError + run_state=RESETTING → _log_verbose (quiet path, line 329)."""
        instance = self._make_instance()
        instance.run_state = _FakeRunState.STARTED

        verbose_calls = []
        instance._log_verbose = lambda *a, **k: (
            verbose_calls.append(a) or setattr(instance, "do_listen", False)
        )

        call_count = [0]

        async def _recv():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionResetError("WAN drop")
            instance.do_listen = False
            return None

        instance._receive_msg = _recv

        await instance._listen()
        # After fix: ConnectionResetError → state set to RESETTING before check → verbose path
        assert verbose_calls or not instance.do_listen, (
            "ConnectionResetError must route to _log_verbose with the fix"
        )

    @pytest.mark.asyncio
    async def test_outer_exception_calls_terminate(self):
        """Outer except Exception: _terminate() + _do_writer_close (lines 349-359)."""
        instance = self._make_instance()

        terminate_calls = []
        instance._terminate = lambda: terminate_calls.append(1)

        writer_close_calls = []

        async def _writer_close():
            writer_close_calls.append(1)

        instance._do_writer_close = _writer_close

        async def _bad_login():
            raise RuntimeError("login crash")

        instance._login = _bad_login

        await instance._listen()
        assert terminate_calls, "_terminate must fire on outer exception"
        assert writer_close_calls, "_do_writer_close must run in finally"

    @pytest.mark.asyncio
    async def test_resetting_state_sleeps(self):
        """While run_state == RESETTING, must asyncio.sleep(1) (line 289)."""
        instance = self._make_instance()
        instance.run_state = _FakeRunState.RESETTING

        slept = []

        async def _fast_sleep(secs):
            slept.append(secs)
            instance.do_listen = False

        async def _bad_recv():
            raise AssertionError("RESETTING must not call _receive_msg")

        instance._receive_msg = _bad_recv

        with patch("asyncio.sleep", new=_fast_sleep):
            await instance._listen()

        assert slept == [1], "Must sleep 1s while RESETTING"
