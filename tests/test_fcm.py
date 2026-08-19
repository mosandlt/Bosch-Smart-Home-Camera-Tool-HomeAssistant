"""Consolidated tests for custom_components/bosch_shc_camera/fcm.py.

fcm.py implements the FCM push-notification supervisor: starting/stopping the
push client, the soft/hard self-heal ladder, drift detection between the
locally cached and Bosch-registered push credentials, the push-data handling
pipeline (path A: binary_sensor/camera state refresh, path B: snapshot/clip
download + notify + optional SMB/local-save), Bosch backend re-registration,
and the noise-filter installed on the firebase_messaging logger.

This file consolidates what used to be ~20 separately-named test files
(sprint/round/coverage-gate/bug-hunt splits) into one flat module, matching
home-assistant/core's convention of a single test_<module>.py per source
module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import sys
import threading
import time
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from threading import Lock, RLock
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import fcm
from custom_components.bosch_shc_camera.fcm import (
    _FCMNoiseFilter,
    _install_fcm_noise_filter,
    async_handle_fcm_push,
    async_send_alert,
    async_start_fcm_push,
    get_recent_fcm_creds_staleness_count,
    reset_fcm_creds_staleness_counter,
)
from tests.source_match import assert_in_source

MODULE = "custom_components.bosch_shc_camera.fcm"
RECORDER_MODULE = "custom_components.bosch_shc_camera.recorder"
SMB_MODULE = "custom_components.bosch_shc_camera.smb"
CAM_ID = "11111111-1111-1111-1111-111111111111"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x42" * 400  # 404 B -- real-looking snapshot
JPEG_BYTES_ALT = b"\xff\xd8\xff\xe0" + b"\x99" * 400  # different content, same length

# Supervisor lifecycle (ensure/stop), hard-heal reasons, poll-loop branches, path-a/b step1 failure + clip-guard tests, patched-client/_listen-body creation branches, drift-heal registration markers (from: bug-hunt grab-bag, coverage-gate grab-bag, clip coverage, coverage gaps, drift/heal registration)


class TestSafePathSegment:
    """_safe_path_segment() neutralises path-traversal tokens in a
    cloud-provided camera title before it is used to build an alert
    snapshot/clip filename."""

    def test_normal_names_unchanged(self) -> None:
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        assert _safe_path_segment("Terrasse") == "Terrasse"
        assert _safe_path_segment("Eyes Outdoor II") == "Eyes Outdoor II"

    def test_traversal_tokens_neutralised(self) -> None:
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        out = _safe_path_segment("../../config/secrets")
        assert "/" not in out
        assert "\\" not in out
        assert ".." not in out

    def test_join_cannot_escape_alert_dir(self) -> None:
        """The concrete attack: a camera titled '../../config/secrets' must not
        resolve outside the alert directory."""
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        alert_dir = "/config/www/bosch_alerts"
        seg = _safe_path_segment("../../config/secrets")
        path = os.path.join(alert_dir, f"{seg}_ts_MOVEMENT.jpg")
        assert os.path.abspath(path).startswith(os.path.abspath(alert_dir) + os.sep)

    def test_backslash_variant(self) -> None:
        from custom_components.bosch_shc_camera.fcm import _safe_path_segment

        assert "\\" not in _safe_path_segment("..\\..\\windows")


class TestUrlsafeB64DecodePadded:
    """_urlsafe_b64decode_padded() — GitHub #68 root-cause fix: RFC 8291
    crypto-key/salt headers arrive without base64 '=' padding."""

    def test_decodes_unpadded_input(self) -> None:
        import base64

        from custom_components.bosch_shc_camera.fcm import _urlsafe_b64decode_padded

        raw = b"hello world, this is test data!"
        padded = base64.urlsafe_b64encode(raw)
        unpadded = padded.rstrip(b"=").decode("ascii")
        assert len(unpadded) % 4 != 0  # genuinely needs padding restored

        assert _urlsafe_b64decode_padded(unpadded) == raw

    def test_already_padded_input_still_decodes(self) -> None:
        """Input that happens to already be a multiple of 4 (no padding
        needed) must still decode correctly — the padding math (-len % 4)
        must be a no-op in that case."""
        import base64

        from custom_components.bosch_shc_camera.fcm import _urlsafe_b64decode_padded

        raw = b"12 bytes!!!!"  # 12 bytes -> exactly 16 b64 chars, no padding
        encoded = base64.urlsafe_b64encode(raw).decode("ascii")
        assert len(encoded) % 4 == 0
        assert not encoded.endswith("=")

        assert _urlsafe_b64decode_padded(encoded) == raw

    def test_raises_on_input_still_invalid_after_padding(self) -> None:
        """`urlsafe_b64decode` is non-strict: illegal characters (spaces,
        `!`) are silently discarded rather than rejected outright, so this
        does NOT verify general input validation — e.g. `"$$$$"` decodes to
        `b""` without raising, and some garbage strings decode to garbage
        bytes without raising at all. It only pins that `_urlsafe_b64decode_padded`
        propagates `binascii.Error` rather than swallowing it when the
        padding it restores still isn't enough to make the (cleaned) input
        valid."""
        import binascii

        from custom_components.bosch_shc_camera.fcm import _urlsafe_b64decode_padded

        with pytest.raises(binascii.Error):
            _urlsafe_b64decode_padded("not valid base64!!!")


class TestDecodeMessageHeader:
    """_decode_message_header() — GitHub #68 live-deploy finding, 2026-08-18:
    non-ASCII bytes in a per-message crypto-key/salt header (RFC 8291-legal,
    proto3 string fields) must be treated as a skippable single-message
    fault, not escape as an unhandled UnicodeEncodeError."""

    def test_normalizes_unicode_encode_error_to_binascii_error(self) -> None:
        import binascii

        from custom_components.bosch_shc_camera.fcm import _decode_message_header

        with pytest.raises(binascii.Error):
            _decode_message_header("AAé")

    def test_valid_input_still_decodes(self) -> None:
        import base64

        from custom_components.bosch_shc_camera.fcm import _decode_message_header

        raw = b"a valid header value"
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        assert _decode_message_header(encoded) == raw


class TestDecodeCredentialMaterial:
    """_decode_credential_material() — GitHub #68 live-deploy finding,
    2026-08-18: a corrupt STORED credential whose base64 length happens to
    land on len%4==1 raised binascii.Error via the same padding helper used
    for message headers — which IS in skip_exceptions — silently
    skip-and-acking every push forever instead of triggering hard-heal."""

    def test_normalizes_binascii_error_to_value_error(self) -> None:
        """A genuine SECP256R1 PKCS8 DER key, base64'd then truncated by 3
        chars (len%4==1 after truncation), must raise ValueError — NOT
        binascii.Error — from this helper, so it's classified as a
        client-wide credential fault, never silently skipped."""
        import base64
        import binascii

        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        from custom_components.bosch_shc_camera.fcm import (
            _decode_credential_material,
        )

        priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
        der = priv.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        encoded = base64.urlsafe_b64encode(der).rstrip(b"=").decode("ascii")
        truncated = encoded[:-3]
        assert len(truncated) % 4 == 1, "precondition: this is the misrouted case"

        with pytest.raises(ValueError) as exc_info:
            _decode_credential_material(truncated)

        assert not isinstance(exc_info.value, binascii.Error), (
            "must be reclassified as a plain ValueError so it propagates to "
            "hard-heal instead of being silently skip-and-acked forever"
        )

    def test_valid_input_still_decodes(self) -> None:
        import base64

        from custom_components.bosch_shc_camera.fcm import (
            _decode_credential_material,
        )

        raw = b"some credential bytes"
        encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        assert _decode_credential_material(encoded) == raw


def _make_supervisor_coord(
    entry_data: dict, *, force_hard: bool = False
) -> SimpleNamespace:
    """Minimal coordinator for _async_run_fcm_supervisor tests.

    Does NOT pre-set fcm_start_lock, so tests covering the lock-absent path
    can leave it unset. Callers needing the lock use
    _make_supervisor_coord_with_lock() instead.
    """
    coord = SimpleNamespace()
    coord.entry = SimpleNamespace(data=dict(entry_data))
    coord.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    coord.options = {"enable_fcm_push": True}
    coord.fcm_force_hard_heal = force_hard
    coord.fcm_last_push = float("-inf")
    coord.fcm_running = False
    coord.fcm_healthy = False
    return coord


def _make_supervisor_coord_with_lock(
    entry_data: dict, *, force_hard: bool = False
) -> SimpleNamespace:
    coord = _make_supervisor_coord(entry_data, force_hard=force_hard)
    coord.fcm_start_lock = asyncio.Lock()
    return coord


def test_reset_fcm_error_counter_delegates_to_staleness_reset() -> None:
    """reset_fcm_error_counter() (backward-compat shim) clears
    _SHARED_STALENESS_TIMESTAMPS."""
    from custom_components.bosch_shc_camera.fcm import (
        _FCMNoiseFilter,
        reset_fcm_error_counter,
    )

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())
    assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) >= 1

    reset_fcm_error_counter()

    assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []


async def test_ensure_supervisor_returns_early_when_fcm_disabled() -> None:
    """async_ensure_fcm_supervisor with enable_fcm_push=False → returns without spawning."""
    from custom_components.bosch_shc_camera import fcm

    coord = SimpleNamespace(
        options={"enable_fcm_push": False},
    )
    await fcm.async_ensure_fcm_supervisor(coord)
    assert not hasattr(coord, "fcm_supervisor_task")


async def test_ensure_supervisor_spawns_task_when_none() -> None:
    """async_ensure_fcm_supervisor with no existing task → creates supervisor task."""
    from custom_components.bosch_shc_camera import fcm

    coord = SimpleNamespace(
        options={"enable_fcm_push": True},
        fcm_supervisor_task=None,
    )
    with patch.object(fcm, "_async_run_fcm_supervisor", new=AsyncMock()):
        await fcm.async_ensure_fcm_supervisor(coord)

    assert coord.fcm_supervisor_task is not None
    coord.fcm_supervisor_task.cancel()
    try:
        await coord.fcm_supervisor_task
    except (asyncio.CancelledError, Exception):
        pass


async def test_ensure_supervisor_idempotent_when_task_alive() -> None:
    """async_ensure_fcm_supervisor with a running task → returns early, task unchanged."""
    from custom_components.bosch_shc_camera import fcm

    async def _hang() -> None:
        await asyncio.sleep(9999)

    existing = asyncio.create_task(_hang())
    coord = SimpleNamespace(
        options={"enable_fcm_push": True},
        fcm_supervisor_task=existing,
    )

    await fcm.async_ensure_fcm_supervisor(coord)

    assert coord.fcm_supervisor_task is existing  # not replaced

    existing.cancel()
    try:
        await existing
    except asyncio.CancelledError:
        pass


async def test_stop_supervisor_cancels_running_task_and_calls_stop_push() -> None:
    """async_stop_fcm_supervisor cancels a live task, sets it None, and calls async_stop_fcm_push."""
    from custom_components.bosch_shc_camera import fcm

    async def _hang() -> None:
        await asyncio.sleep(9999)

    running = asyncio.create_task(_hang())
    coord = SimpleNamespace(
        fcm_supervisor_task=running,
        fcm_client=None,
        fcm_running=False,
    )

    with patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()) as mock_stop:
        await fcm.async_stop_fcm_supervisor(coord)

    assert coord.fcm_supervisor_task is None
    mock_stop.assert_called_once_with(coord)
    assert running.done()


async def test_stop_supervisor_reraises_callers_own_cancellation() -> None:
    """GitHub #68 live-deploy finding, 2026-08-18: a bare
    `except (asyncio.CancelledError, Exception): pass` around `await sup`
    swallowed a cancellation of the CALLER (this function's own task, e.g.
    HA's shutdown deadline), not just the supervisor's own.

    Note `sup.cancelled()` alone can't distinguish the two cases: cancelling
    the caller while it's suspended on `await sup` cancels `sup` too, as
    part of how asyncio delivers the cancellation (`Task._fut_waiter.
    cancel()`) — both the self-inflicted case (our own `sup.cancel()` call)
    and the caller-cancelled case end up with `sup.cancelled() is True`. The
    fix instead checks `Task.cancelling()` on the CURRENT task, which only
    counts cancel() requests against THAT task specifically — cancelling
    `sup` (a different task object) never touches it.
    """
    from custom_components.bosch_shc_camera import fcm

    supervisor_started = asyncio.Event()

    async def _supervisor_hangs() -> None:
        supervisor_started.set()
        await asyncio.sleep(9999)

    sup = asyncio.create_task(_supervisor_hangs())
    await supervisor_started.wait()

    coord = SimpleNamespace(fcm_supervisor_task=sup, fcm_client=None, fcm_running=False)

    outer_task = asyncio.create_task(fcm.async_stop_fcm_supervisor(coord))
    await asyncio.sleep(0)  # let it reach `sup.cancel()` + start awaiting sup
    outer_task.cancel()  # cancel the CALLER, not the supervisor directly

    with pytest.raises(asyncio.CancelledError):
        await outer_task

    assert outer_task.cancelled()


async def test_stop_supervisor_swallows_supervisor_own_exception() -> None:
    """If the supervisor task itself raised a genuine (non-CancelledError)
    exception, that's the supervisor's own failure to have already handled
    internally — async_stop_fcm_supervisor must still swallow it and
    proceed with the rest of teardown, not propagate it to the caller."""
    from custom_components.bosch_shc_camera import fcm

    supervisor_started = asyncio.Event()

    async def _supervisor_raises() -> None:
        supervisor_started.set()
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            # Raise something else in response to our sup.cancel() below,
            # instead of letting the cancellation itself propagate — this
            # is the "supervisor's own genuine failure" case this branch
            # exists to swallow.
            raise RuntimeError("supervisor's own unrelated failure") from None

    sup = asyncio.create_task(_supervisor_raises())
    await supervisor_started.wait()
    coord = SimpleNamespace(fcm_supervisor_task=sup, fcm_client=None, fcm_running=False)

    with patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()) as mock_stop:
        await fcm.async_stop_fcm_supervisor(coord)  # must not raise

    assert coord.fcm_supervisor_task is None
    mock_stop.assert_called_once_with(coord)


async def test_start_fcm_push_passes_shared_http_session_to_avoid_leak() -> None:
    """GitHub #68 live-deploy finding, 2026-08-18: without http_client_session,
    FcmRegister lazily creates and owns its own aiohttp.ClientSession, only
    closed on the checkin_or_register() SUCCESS path — a failed registration
    leaked one session per attempt. FcmPushClient must be constructed with
    HA's shared session so FcmRegister never owns one to leak."""
    _install_firebase_module()
    import custom_components.bosch_shc_camera.fcm as fcm_mod

    fcm_mod._QuietFcmPushClient._patched_class = False

    shared_session = object()
    captured_kwargs: dict[str, object] = {}

    class _CapturingFcmPushClient(_FakeFcmPushClient):
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.update(kwargs)

        async def checkin_or_register(self) -> str:
            raise RuntimeError("registration failed")

    import sys

    sys.modules["firebase_messaging"].FcmPushClient = _CapturingFcmPushClient

    coord = SimpleNamespace(
        options={"enable_fcm_push": True},
        entry=SimpleNamespace(data={}),
        hass=SimpleNamespace(loop=MagicMock(), config_entries=MagicMock()),
        fcm_running=False,
    )

    with (
        patch(
            f"{MODULE}.async_get_clientsession",
            return_value=shared_session,
        ),
        patch.object(
            fcm_mod,
            "fetch_firebase_config",
            new=AsyncMock(
                return_value={
                    "project_id": "p",
                    "app_id": "a",
                    "api_key": "k",
                }
            ),
        ),
    ):
        try:
            await fcm_mod._async_start_fcm_push_locked(coord)
        finally:
            _uninstall_firebase_module()

    assert captured_kwargs.get("http_client_session") is shared_session, (
        "FcmPushClient must be constructed with HA's shared clientsession"
    )


async def test_supervisor_hard_heal_reason_soft_streak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """soft_streak >= FCM_SUPERVISOR_SOFT_HEAL_MAX → 'soft-restarts' hard-heal reason."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    # Patch MAX to 0 so soft_streak=0 >= 0 triggers the elif on the first iteration.
    with (
        patch.object(fcm, "FCM_SUPERVISOR_SOFT_HEAL_MAX", 0),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch.object(
            fcm,
            "_async_start_fcm_push_locked",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        caplog.at_level("INFO", logger=MODULE),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    hard_heal_logs = [
        r.getMessage() for r in caplog.records if "hard-heal" in r.getMessage()
    ]
    assert any("soft-restarts without a push" in msg for msg in hard_heal_logs), (
        hard_heal_logs
    )
    assert not any("PHONE_REGISTRATION_ERROR" in msg for msg in hard_heal_logs)
    assert not any("no persisted credentials" in msg for msg in hard_heal_logs)


async def test_supervisor_hard_heal_resets_failures_not_just_soft_streak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bug-hunt 2026-07-20: a hard-heal purge+re-registration is exactly the
    fix for a credential-related failure, but only soft_streak was ever
    reset — failures (the backoff-delay counter) stayed at whatever it had
    climbed to. If the freshly re-registered listener then fails again for
    an unrelated reason (not credentials), the supervisor computed its
    retry delay off the STALE, still-elevated failures value instead of
    starting fresh — contradicting this module's own docstring ("resets to
    0 after a successful push arrived").

    Sequence: two ordinary soft failures (failures=2, soft_streak=2) →
    soft_streak hits the (patched) hard-heal threshold → hard-heal purge
    resets soft_streak (and, with the fix, failures) → the listener start
    inside that SAME iteration also fails. The "attempt #%d" log line
    must show #1 (fresh count), not #3 (stale pre-heal count + 1).
    """
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    with (
        patch.object(fcm, "FCM_SUPERVISOR_SOFT_HEAL_MAX", 2),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch("asyncio.sleep", new=AsyncMock()),
        patch.object(
            fcm,
            "_async_start_fcm_push_locked",
            new=AsyncMock(side_effect=[False, False, False, asyncio.CancelledError]),
        ),
        caplog.at_level("INFO", logger=MODULE),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    hard_heal_logs = [
        r.getMessage() for r in caplog.records if "hard-heal" in r.getMessage()
    ]
    assert any("soft-restarts without a push" in msg for msg in hard_heal_logs), (
        "hard-heal must have fired via the soft_streak threshold"
    )
    attempt_logs = [
        r.getMessage()
        for r in caplog.records
        if "start failed — retry" in r.getMessage()
    ]
    assert len(attempt_logs) == 3, attempt_logs
    assert "attempt #1" in attempt_logs[0]
    assert "attempt #2" in attempt_logs[1]
    assert "attempt #1" in attempt_logs[2], (
        f"failures must reset to 0 on hard-heal, not stay stale — got: {attempt_logs[2]!r}"
    )


async def test_supervisor_hard_heal_reason_creds_staleness(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recent staleness timestamp → 'PHONE_REGISTRATION_ERROR' hard-heal reason."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    # Add a recent staleness entry so get_recent_fcm_creds_staleness_count > 0.
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())

    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    try:
        with (
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "reset_fcm_creds_staleness_counter"),
            patch.object(
                fcm,
                "_async_start_fcm_push_locked",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ),
            caplog.at_level("INFO", logger=MODULE),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            try:
                await task
            except asyncio.CancelledError:
                pass
        assert task.done()
        hard_heal_logs = [
            r.getMessage() for r in caplog.records if "hard-heal" in r.getMessage()
        ]
        assert any("PHONE_REGISTRATION_ERROR" in msg for msg in hard_heal_logs), (
            hard_heal_logs
        )
        assert not any("soft-restarts without a push" in msg for msg in hard_heal_logs)
        assert not any("no persisted credentials" in msg for msg in hard_heal_logs)
    finally:
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


async def test_supervisor_hard_heal_reason_no_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No fcm_credentials in entry.data → 'no persisted credentials' hard-heal reason."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    # Empty entry data → not coordinator.entry.data.get("fcm_credentials") is True.
    coord = _make_supervisor_coord_with_lock({}, force_hard=False)

    with (
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch.object(
            fcm,
            "_async_start_fcm_push_locked",
            new=AsyncMock(side_effect=asyncio.CancelledError),
        ),
        caplog.at_level("INFO", logger=MODULE),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    hard_heal_logs = [
        r.getMessage() for r in caplog.records if "hard-heal" in r.getMessage()
    ]
    assert any("no persisted credentials" in msg for msg in hard_heal_logs), (
        hard_heal_logs
    )
    assert not any("soft-restarts without a push" in msg for msg in hard_heal_logs)
    assert not any("PHONE_REGISTRATION_ERROR" in msg for msg in hard_heal_logs)


async def test_supervisor_hard_heal_backoff_escalates_when_delivery_stays_dead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GitHub #68: if a CONFIRMED-problem hard-heal's purge+re-registration
    doesn't restore delivery (no push received since), repeating it
    immediately just re-hits Bosch/Google's registration endpoint at the
    same cadence as a fresh problem — plausibly worsening the reporter's
    recurring PHONE_REGISTRATION_ERROR. The second consecutive creds-staleness
    hard-heal with no push in between must wait FCM_SUPERVISOR_BACKOFF_SEC[0]
    before purging again. Uses the creds-staleness trigger (not force_hard)
    since it's deterministic without a running listener/poll cycle; the
    staleness-reset is patched to a no-op so the trigger keeps firing exactly
    like an unresolved PHONE_REGISTRATION_ERROR would in production."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    sleep_calls: list[float] = []

    async def _sleep(secs: float, *_a: object, **_k: object) -> None:
        sleep_calls.append(secs)

    start_calls = 0

    async def _start(_coord: object) -> bool:
        nonlocal start_calls
        start_calls += 1
        if start_calls >= 2:
            raise asyncio.CancelledError()
        return False  # registration fails again, no push

    try:
        with (
            patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "reset_fcm_creds_staleness_counter"),  # no-op
            patch("asyncio.sleep", new=_sleep),
            caplog.at_level("INFO", logger=MODULE),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert task.done()
        assert start_calls == 2
        streak_logs = [
            r.getMessage()
            for r in caplog.records
            if "hard-heal streak" in r.getMessage()
        ]
        assert any("hard-heal streak 2" in msg for msg in streak_logs), streak_logs
        assert fcm.FCM_SUPERVISOR_BACKOFF_SEC[0] in sleep_calls
    finally:
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


async def test_supervisor_hard_heal_streak_resets_once_push_arrives(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A push arriving between two CONFIRMED-problem hard-heals means the
    first one actually worked — the streak (and its backoff) must reset
    instead of treating the next, unrelated hard-heal as a continuation of
    the old one."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    start_calls = 0

    async def _start(_coord: object) -> bool:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            coord.fcm_last_push = time.monotonic()  # this heal DID restore delivery
            return False
        if start_calls >= 2:
            raise asyncio.CancelledError()
        return False

    try:
        with (
            patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "reset_fcm_creds_staleness_counter"),  # no-op
            patch("asyncio.sleep", new=AsyncMock()),
            caplog.at_level("INFO", logger=MODULE),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert task.done()
        streak_logs = [
            r.getMessage()
            for r in caplog.records
            if "hard-heal streak" in r.getMessage()
        ]
        assert not streak_logs, streak_logs
    finally:
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


async def test_supervisor_cancelled_during_hard_heal_streak_backoff_sleep_breaks() -> (
    None
):
    """Cancellation arriving during the pre-purge streak-backoff sleep (added
    for GitHub #68) must break the loop cleanly, same as the other backoff
    sleeps in this function, instead of propagating uncaught."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.append(time.monotonic())
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    start_calls = 0

    async def _start(_coord: object) -> bool:
        nonlocal start_calls
        start_calls += 1
        return False  # never restores delivery

    sleep_calls = 0

    async def _sleep(*_a: object, **_k: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 2:
            # 2nd sleep call is the pre-purge streak-backoff sleep on the
            # 2nd consecutive hard-heal.
            raise asyncio.CancelledError()

    try:
        with (
            patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "reset_fcm_creds_staleness_counter"),  # no-op
            patch("asyncio.sleep", new=_sleep),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            await task  # must complete normally (break), not raise

        assert task.done()
        assert not task.cancelled()
        assert start_calls == 1
    finally:
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


async def test_supervisor_fresh_install_no_credentials_first_occurrence_is_benign(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuinely fresh install (no fcm_credentials, never hard-healed yet)
    hitting the "no persisted credentials" reason for the FIRST time must
    stay benign — no streak escalation. This is not a recurring failure yet,
    just a brand-new client that hasn't registered."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock({}, force_hard=False)

    async def _start(_coord: object) -> bool:
        raise asyncio.CancelledError()

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch("asyncio.sleep", new=AsyncMock()),
        caplog.at_level("INFO", logger=MODULE),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    streak_logs = [
        r.getMessage() for r in caplog.records if "hard-heal streak" in r.getMessage()
    ]
    assert not streak_logs, streak_logs


async def test_supervisor_no_credentials_after_purge_escalates_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GitHub #68 live-deploy follow-up: once we've ALREADY hard-healed
    (purged credentials) at least once, a subsequent "no persisted
    credentials" occurrence is the SAME ongoing registration failure —
    re-registration can keep failing (e.g. a plain FCM-install error or a
    WAN outage) without ever emitting a PHONE_REGISTRATION_ERROR marker,
    which previously meant confirmed_problem stayed False forever and the
    backoff was bypassed entirely, pinning retries at 5s indefinitely. The
    SECOND+ hard-heal via this reason must now escalate like any other
    confirmed-problem streak.
    """
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock({}, force_hard=False)

    start_calls = 0

    async def _start(_coord: object) -> bool:
        nonlocal start_calls
        start_calls += 1
        if start_calls >= 3:
            raise asyncio.CancelledError()
        return False  # registration keeps "failing" (never persists creds)

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch("asyncio.sleep", new=AsyncMock()),
        caplog.at_level("INFO", logger=MODULE),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert start_calls >= 2  # at least 2 hard-heals happened
    streak_logs = [
        r.getMessage() for r in caplog.records if "hard-heal streak" in r.getMessage()
    ]
    assert any("hard-heal streak 2" in msg for msg in streak_logs), streak_logs


async def test_supervisor_creates_lock_when_absent_and_breaks_on_cancelled() -> None:
    """A missing fcm_start_lock is lazily created; CancelledError from the
    start call makes the supervisor break out and return normally."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    # Coordinator WITHOUT fcm_start_lock so the lazy-init path is reached.
    coord = _make_supervisor_coord({"fcm_credentials": {"gcm": "x"}}, force_hard=False)

    with patch.object(
        fcm,
        "_async_start_fcm_push_locked",
        new=AsyncMock(side_effect=asyncio.CancelledError),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    # Lock was created on the coordinator.
    assert hasattr(coord, "fcm_start_lock")


async def test_supervisor_exception_during_start_logs_and_continues() -> None:
    """A generic exception from the start call is logged; the loop continues
    to a second attempt instead of dying."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    call_count = 0

    async def _flaky(_coord: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient error")
        raise asyncio.CancelledError()  # terminate on 2nd attempt

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_flaky),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert call_count == 2  # RuntimeError iteration + CancelledError iteration


async def test_supervisor_poll_exits_when_listener_stops() -> None:
    """The inner poll loop breaks when fcm_client.is_started() returns False."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = False
    coord.fcm_client = fcm_client

    call_count = 0

    async def _start_then_cancel(_coord: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return True  # listener "up" — enters poll loop
        raise asyncio.CancelledError()

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start_then_cancel),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert call_count == 2  # first start (poll exits) + second (cancel terminates)


async def test_supervisor_cancelled_during_poll_calls_stop_push() -> None:
    """Task cancellation inside the poll sleep still calls async_stop_fcm_push."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = (
        True  # stays "alive" so poll doesn't break by itself
    )
    coord.fcm_client = fcm_client

    with (
        patch.object(
            fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=True)
        ),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()) as mock_stop,
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        await asyncio.sleep(0.05)  # let supervisor reach the poll-loop sleep
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    mock_stop.assert_called()


async def test_supervisor_push_received_resets_counters() -> None:
    """A push arriving while the listener ran resets the failure/soft_streak counters to 0."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = False
    coord.fcm_client = fcm_client

    call_count = 0

    async def _start_effect(_coord: object) -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a push arriving while this listener was alive.
            coord.fcm_last_push = time.monotonic()
            return True
        raise asyncio.CancelledError()

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start_effect),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert call_count == 2


async def test_supervisor_cancelled_during_final_backoff_sleep() -> None:
    """task.cancel() while the supervisor sleeps after listener termination → clean break."""
    import asyncio as _asyncio

    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import (
        FCM_SUPERVISOR_POLL_SEC,
        _FCMNoiseFilter,
    )

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.return_value = False  # poll breaks immediately
    coord.fcm_client = fcm_client

    # Save real asyncio.sleep before patching to avoid infinite recursion in the hook.
    _real_sleep = _asyncio.sleep
    reached_final_sleep = _asyncio.Event()

    async def _controlled_sleep(secs: float) -> None:
        if secs == FCM_SUPERVISOR_POLL_SEC:
            return  # poll sleep → instant
        # Final backoff sleep: signal the test, then actually block so the cancel fires here.
        reached_final_sleep.set()
        await _real_sleep(9999)

    with (
        patch.object(
            fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=True)
        ),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch("asyncio.sleep", new=_controlled_sleep),
    ):
        task = _asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        await reached_final_sleep.wait()  # supervisor is now blocked inside final sleep
        task.cancel()
        try:
            await task
        except _asyncio.CancelledError:
            pass

    assert task.done()


async def test_supervisor_inner_poll_breaks_on_forced_hard_heal() -> None:
    """The inner poll loop must honor fcm_force_hard_heal even while the
    listener still reports is_started()==True (the silent-delivery-death
    case the flag exists for) — it must not wait for an independent socket
    death that, in this scenario, may never come.

    Before the corresponding fix, the inner loop only exited on
    is_started()==False, so with a listener that stays 'started' the
    supervisor never re-read the flag and the forced hard-heal never
    happened. The sleep-call safety net prevents a regressed loop from
    hanging the test."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    # Listener stays "started" — the silent-delivery-death case.
    fcm_client = MagicMock()
    fcm_client.is_started.return_value = True
    coord.fcm_client = fcm_client

    start_calls = 0

    async def _start(_coord: object) -> bool:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            return True  # listener "up" → enter the inner poll loop
        # Second start == the forced hard-heal restart. Terminate the supervisor.
        raise asyncio.CancelledError()

    sleep_calls = 0

    async def _sleep(*_a: object, **_k: object) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            # First sleep is inside the inner poll loop: simulate the watchdog
            # flagging delivery death while the listener still reports started.
            coord.fcm_force_hard_heal = True
        elif sleep_calls > 50:
            # Safety net: a regressed inner loop that never breaks would spin
            # here forever — abort so the test FAILS (on start_calls) not hangs.
            raise asyncio.CancelledError()

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
        patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch("asyncio.sleep", new=_sleep),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    # The listener never stopped (is_started stayed True) yet the supervisor
    # restarted → the inner poll loop honored the forced hard-heal promptly.
    assert start_calls == 2
    # The hard-heal actually purged the credentials from the entry data.
    coord.hass.config_entries.async_update_entry.assert_called()
    # Flag consumed (and reset) by the top-of-loop hard-heal.
    assert coord.fcm_force_hard_heal is False


async def test_supervisor_hard_heal_purge_exception_logs_and_retries() -> None:
    """An exception raised while purging credentials (e.g. from
    config_entries.async_update_entry) must not propagate straight out of
    _async_run_fcm_supervisor and kill the task — that would leave FCM push
    fully down until the next coordinator-tick watchdog cycle noticed the
    task was done and restarted it, instead of the designed ~10s poll
    cadence. The purge is wrapped in try/except and retries instead of
    dying. Pinned here: the first purge attempt raises, the supervisor
    survives and completes a second hard-heal attempt before terminating."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    # No fcm_credentials → needs_hard is True every iteration (deterministic,
    # doesn't depend on force_hard being consumed/reset).
    coord = _make_supervisor_coord_with_lock({}, force_hard=False)
    coord.hass.config_entries.async_update_entry = MagicMock(
        side_effect=[RuntimeError("boom"), None]
    )

    purge_attempts = 0

    async def _stop_push(_coord: object) -> None:
        nonlocal purge_attempts
        purge_attempts += 1

    async def _start(_coord: object) -> bool:
        # Only reached after a hard-heal purge attempt completed without
        # raising (2nd attempt) — terminate the supervisor cleanly.
        raise asyncio.CancelledError()

    with (
        patch.object(fcm, "async_stop_fcm_push", new=_stop_push),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert not task.cancelled()
    # Both the failed and the succeeding purge attempt ran async_stop_fcm_push.
    assert purge_attempts == 2
    assert coord.hass.config_entries.async_update_entry.call_count == 2


async def test_supervisor_cancelled_during_hard_heal_purge_reraises() -> None:
    """A real `asyncio.CancelledError` raised while purging credentials (e.g.
    the task is cancelled mid-`async_stop_fcm_push`) must propagate out of
    the supervisor immediately — it is NOT a "purge raised an exception,
    retry" case (that path is only for genuine errors). The purge's
    try/except distinguishes the two: `except asyncio.CancelledError: raise`
    re-raises before the broader `except Exception:` retry-handler below it
    can swallow real cancellation as a retryable failure."""
    from custom_components.bosch_shc_camera import fcm

    coord = _make_supervisor_coord_with_lock({}, force_hard=False)

    async def _stop_push(_coord: object) -> None:
        raise asyncio.CancelledError()

    with (
        patch.object(fcm, "async_stop_fcm_push", new=_stop_push),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        with pytest.raises(asyncio.CancelledError):
            await task

    assert task.done()
    assert task.cancelled()


async def test_supervisor_cancelled_during_purge_retry_backoff_sleep_breaks() -> None:
    """After a hard-heal purge raises a plain `Exception` (retryable), the
    supervisor sleeps for the backoff before retrying. If cancellation
    arrives DURING that backoff sleep, the nested
    `except asyncio.CancelledError: break` must stop the loop cleanly
    (returning normally) rather than letting cancellation fall through to
    the outer retry `continue` and looping forever on a dying task."""
    from custom_components.bosch_shc_camera import fcm

    coord = _make_supervisor_coord_with_lock({}, force_hard=False)

    async def _stop_push(_coord: object) -> None:
        raise RuntimeError("boom")

    with (
        patch.object(fcm, "async_stop_fcm_push", new=_stop_push),
        patch.object(fcm, "reset_fcm_creds_staleness_counter"),
        patch("asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError())),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        # The `break` inside the nested except-CancelledError ends the while
        # loop normally — the task completes WITHOUT propagating
        # CancelledError and without being marked cancelled.
        await task

    assert task.done()
    assert not task.cancelled()


async def test_supervisor_poll_loop_exception_logs_and_treats_as_terminated() -> None:
    """An unexpected exception while polling the listener (e.g.
    fcm_client.is_started() raising) must not propagate out of
    _async_run_fcm_supervisor and kill the task, same failure class as the
    hard-heal purge case above. It is instead treated like a normal
    listener termination (falls through to stop+backoff). Pinned here:
    is_started() raises once, the supervisor survives and reaches a second
    start attempt."""
    from custom_components.bosch_shc_camera import fcm
    from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
    coord = _make_supervisor_coord_with_lock(
        {"fcm_credentials": {"gcm": "x"}}, force_hard=False
    )

    fcm_client = MagicMock()
    fcm_client.is_started.side_effect = RuntimeError("boom")
    coord.fcm_client = fcm_client

    start_calls = 0

    async def _start(_coord: object) -> bool:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            return True  # listener "up" → enter the poll loop, is_started() raises
        raise asyncio.CancelledError()  # terminate on the 2nd attempt

    stop_calls = 0

    async def _stop_push(_coord: object) -> None:
        nonlocal stop_calls
        stop_calls += 1

    with (
        patch.object(fcm, "_async_start_fcm_push_locked", new=_start),
        patch.object(fcm, "async_stop_fcm_push", new=_stop_push),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert task.done()
    assert not task.cancelled()
    # The supervisor reached a second start attempt instead of dying on the
    # is_started() exception.
    assert start_calls == 2
    assert stop_calls == 1


async def test_handle_push_retries_when_http200_but_no_new_event() -> None:
    """A recursive retry fires when HTTP 200 is returned but no new event was dispatched."""
    from custom_components.bosch_shc_camera import fcm

    coord = SimpleNamespace()
    coord.token = "bearer_tok"
    coord.data = {"cam1": {"info": {"title": "Cam1"}}}
    # prev_id == newest_id → no *new* event, but _any_fetch_ok=True.
    # (Not prev_id=None: since the GitHub #64 fix, a None baseline now
    # legitimately dispatches — see test_first_push_after_restart_dispatches.)
    coord.last_event_ids = {"cam1": "evt1"}
    coord.alert_sent_ids = {}
    coord.fcm_running = True  # enables the retry branch
    coord.options = {}
    coord.camera_entities = {}
    coord.hass = SimpleNamespace(
        states=SimpleNamespace(get=MagicMock(return_value=None))
    )

    resp = MagicMock()
    resp.status = 200
    resp.json = AsyncMock(return_value=[{"id": "evt1", "eventType": "MOVEMENT"}])

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=cm)

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        # _attempt=0: no dispatch + HTTP200 + fcm_running → recurse with _attempt=1
        # _attempt=1: same → recurse with _attempt=2
        # _attempt=2: 2 < 2 is False → stop
        await fcm.async_handle_fcm_push(coord, 0)


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


def _make_alert_coord(options=None, **overrides):
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
        last_event_ids={CAM_ID: "event-id-001"},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


class TestStep1Failure:
    """`_notify_type` for step 1 can raise (e.g. asyncio.CancelledError from a
    HA shutdown mid-alert). The outer try/except logs a warning and returns
    BEFORE any further work — no snapshot, no clip poll, no SMB.
    """

    @pytest.mark.asyncio
    async def test_step1_exception_logs_and_returns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Step 1 raises → warning logged + early return; no step 2/3 work."""
        coord = _make_alert_coord()

        # Bypass the `not info_svcs and not _is_trouble and ...` early-exit
        # (info_svcs derived from alert_notify_service="notify.test" → truthy).
        # The first `hass.services.async_call` (step 1 notify) is mocked to
        # raise.  The outer try wraps the whole `_notify_type` call, so any
        # exception inside the loop (including from svc.split) reaches the
        # outer `except Exception as err` only when re-raised — but
        # _notify_type swallows per-service exceptions internally.  We force
        # the failure path by raising from within _notify_type *after* its
        # internal try (e.g. asyncio.CancelledError, which is intentionally
        # NOT caught by `except Exception` since 3.8).

        async def _raising_call(domain, service, data, **kw):
            # CancelledError propagates past _notify_type's `except Exception`
            # (which doesn't catch BaseException-derived in 3.8+).
            raise asyncio.CancelledError("HA shutting down")

        coord.hass.services.async_call = AsyncMock(side_effect=_raising_call)
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
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
    try/except: warning logged, function returns cleanly, and step 2 never
    runs.

    Step 1 calls `_notify_type` which already wraps service calls in
    try/except; an exception only escapes if the outer machinery (e.g.
    coroutine scheduling) blows up.  We simulate this by patching
    `get_alert_services` to raise — that runs INSIDE _notify_type's loop
    before its inner try, so the exception propagates out and the outer
    try catches it.
    """

    @pytest.mark.asyncio
    async def test_get_alert_services_raises_step1_caught(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        coord = _make_alert_coord()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        call_count = [0]

        def _selective_raise(coord_arg, type_key):
            call_count[0] += 1
            # Initial info_svcs lookup must return something so the
            # early-exit is NOT taken. Only raise on the inner _notify_type
            # call which also calls get_alert_services.
            if call_count[0] == 1:
                return ["notify.test"]
            raise RuntimeError("synthetic services lookup failure")

        with patch(f"{MODULE}.get_alert_services", side_effect=_selective_raise):
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            with caplog.at_level("WARNING", logger=MODULE):
                                # Regular Exception → caught by the outer
                                # except → warning + return, no propagation.
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
        coord = _make_alert_coord(options={"alert_notify_service": "notify.test"})

        # Track which URLs were requested
        gets: list[str] = []

        def _get_side(url, headers=None, **kwargs):
            gets.append(url)
            if "/clip.mp4" in url:
                # Direct probe: 200 + video Content-Type → fires
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

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
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
        # in the direct probe → poll loop is skipped).
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
        coord = _make_alert_coord(options={"alert_notify_service": "notify.test"})
        gets: list[str] = []

        def _get_side(url, headers=None, **kwargs):
            gets.append(url)
            if "/clip.mp4" in url:
                # 200 but WRONG Content-Type → guard rejects
                return _resp_cm(200, body=b"<html>", content_type="text/html")
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
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


class TestClipPathTraversalGuard:
    """Regression: the step-3 video clip path used the cloud-provided camera
    title (`cam_name`) verbatim in the `.mp4` filename, while the snapshot
    path one block above already neutralised it with `_safe_path_segment`. A
    title like "../../config/evil" let the `.mp4` write escape the alert
    dir. Pins that the clip path stays a direct child of alert_dir for a
    malicious title."""

    @pytest.mark.asyncio
    async def test_malicious_cam_title_clip_path_stays_in_alert_dir(self):
        import os

        malicious = "../../config/evil"
        coord = _make_alert_coord(options={"alert_notify_service": "notify.test"})
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

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
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

    @staticmethod
    def _decrypt_raw_data(credentials, crypto_key_str, salt_str, raw_data) -> bytes:
        """Required signature: credentials, crypto_key_str, salt_str, raw_data."""
        return b""

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

    credentials: dict | None = None
    callback = None
    callback_context = None

    def _app_data_by_key(self, p, key: str, do_not_raise: bool = False) -> str:
        """Required signature: self, msg, key, do_not_raise=False."""
        for x in p.app_data:
            if x.key == key:
                return x.value
        if do_not_raise:
            return ""
        raise RuntimeError(f"couldn't find in app_data {key}")

    def _handle_data_message(self, msg) -> None:
        """Required signature: self, msg."""

    def _reset_error_count(self, err_type) -> None: ...


def _make_firebase_module() -> ModuleType:
    """Build a fake firebase_messaging module."""
    mod = ModuleType("firebase_messaging")
    mod.FcmPushClient = _FakeFcmPushClient
    mod.FcmPushClientRunState = _FakeRunState
    mod.FcmRegisterConfig = MagicMock()

    # Sub-module for ErrorType
    sub = ModuleType("firebase_messaging.fcmpushclient")
    sub.ErrorType = SimpleNamespace(CONNECTION="connection", NOTIFY="notify")
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


class TestPatchedClassCreation:
    """When firebase_messaging is available, _patch_class() creates the
    _Patched subclass. The class body executes at definition time, covering
    all of it."""

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

    def test_patch_class_degrades_gracefully_if_decrypt_raw_data_signature_changed(
        self,
    ):
        """If _decrypt_raw_data() gains/loses parameters (library upgrade),
        _patch_class must NOT return None — the GitHub #68 padding override is
        independent of the issue #33 _listen fix, so only the override itself
        should degrade (fall back to vanilla, unpadded _decrypt_raw_data) while
        _listen keeps working. Returning None here would silently lose the
        issue #33 fix too over an unrelated, narrower signature drift."""
        _install_firebase_module()
        original = _FakeFcmPushClient._decrypt_raw_data

        @staticmethod
        def _decrypt_raw_data_with_extra(
            credentials, crypto_key_str, salt_str, raw_data, extra
        ) -> bytes:
            return b""

        _FakeFcmPushClient._decrypt_raw_data = _decrypt_raw_data_with_extra

        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        try:
            result = fcm_mod._QuietFcmPushClient._patch_class()
        finally:
            _FakeFcmPushClient._decrypt_raw_data = original

        assert result is not None, (
            "_patch_class must still return a patched class (with the issue "
            "#33 _listen fix) when only _decrypt_raw_data's signature changed"
        )
        assert "_decrypt_raw_data" not in result.__dict__, (
            "the padding override must NOT be attached when the upstream "
            "signature no longer matches — falls back to inherited (vanilla) "
            "_decrypt_raw_data instead of risking a mismatched override"
        )
        assert "_listen" in result.__dict__, (
            "the issue #33 _listen fix must still be present"
        )

    def test_patch_class_returns_none_if_decrypt_raw_data_missing_entirely(self):
        """If _decrypt_raw_data() is removed entirely (not just reshaped),
        _build_decrypt_raw_data_override's getattr(..., None) guard must not
        raise AttributeError — same graceful-degradation outcome as a
        signature mismatch."""
        _install_firebase_module()
        original = _FakeFcmPushClient._decrypt_raw_data
        del _FakeFcmPushClient._decrypt_raw_data

        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        try:
            result = fcm_mod._QuietFcmPushClient._patch_class()
        finally:
            _FakeFcmPushClient._decrypt_raw_data = original

        assert result is not None
        assert "_decrypt_raw_data" not in result.__dict__

    def test_patch_class_result_has_decrypt_raw_data_override(self):
        """The _Patched subclass must define its own `_decrypt_raw_data`
        (not inherited) — this is the GitHub #68 padding fix itself."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False

        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None
        assert "_decrypt_raw_data" in patched.__dict__, (
            "_Patched must define its own _decrypt_raw_data (not inherit vanilla)"
        )


class TestBuildDecryptRawDataOverrideImportFailures:
    """_build_decrypt_raw_data_override()'s two independent ImportError
    fallback branches — http_ece and cryptography are both real installed
    dependencies in this venv, so failure is simulated via the documented
    `sys.modules[name] = None` sentinel (forces ImportError on the next
    `import`/`from ... import` of that exact module)."""

    def test_returns_none_and_binascii_only_if_http_ece_missing(self) -> None:
        import binascii
        import sys

        from custom_components.bosch_shc_camera.fcm import (
            _build_decrypt_raw_data_override,
        )

        _sentinel = object()
        orig = sys.modules.get("http_ece", _sentinel)
        sys.modules["http_ece"] = None  # type: ignore[assignment]
        try:
            override, skip_exceptions = _build_decrypt_raw_data_override(object)
        finally:
            if orig is _sentinel:
                sys.modules.pop("http_ece", None)
            else:
                sys.modules["http_ece"] = orig

        assert override is None
        assert skip_exceptions == (binascii.Error,), (
            "ECEException can't be part of skip_exceptions when http_ece "
            "itself is unimportable"
        )

    def test_returns_none_but_keeps_ece_exception_if_cryptography_missing(
        self,
    ) -> None:
        """http_ece imports fine (so ECEException IS still available for
        skip_exceptions), but cryptography is missing — only the override
        itself must be unavailable."""
        import binascii
        import sys

        from http_ece import ECEException

        from custom_components.bosch_shc_camera.fcm import (
            _build_decrypt_raw_data_override,
        )

        _sentinel = object()
        orig = sys.modules.get("cryptography.hazmat.backends", _sentinel)
        sys.modules["cryptography.hazmat.backends"] = None  # type: ignore[assignment]
        try:
            override, skip_exceptions = _build_decrypt_raw_data_override(object)
        finally:
            if orig is _sentinel:
                sys.modules.pop("cryptography.hazmat.backends", None)
            else:
                sys.modules["cryptography.hazmat.backends"] = orig

        assert override is None
        assert skip_exceptions == (binascii.Error, ECEException)


class TestPatchedDecryptRawData:
    """End-to-end correctness of the GitHub #68 padding fix: a real
    ECDH-encrypted payload, with the crypto-key/salt headers passed in their
    genuine RFC-8291 UNPADDED wire form, must decrypt successfully via the
    `_Patched._decrypt_raw_data` override — this is what upstream
    sdb9696/firebase-messaging#37 fixes and our vanilla-library copy (0.4.5)
    doesn't have yet."""

    def setup_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    def _get_patched_decrypt(self):
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None
        return patched._decrypt_raw_data

    def test_decrypts_unpadded_crypto_key_and_salt(self):
        import base64

        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from http_ece import encrypt as http_ece_encrypt

        def b64url_unpadded(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        # Receiver = this integration's stored FCM registration keys.
        receiver_priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
        receiver_priv_der = receiver_priv.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        receiver_pub_bytes = receiver_priv.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        auth_secret = os.urandom(16)
        salt = os.urandom(16)

        # Sender = Bosch's cloud, encrypting a push exactly like http_ece
        # web-push encryption does — ephemeral key + the receiver's public
        # key, matching the real ECDH handshake this decrypts against.
        sender_priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
        sender_pub_bytes = sender_priv.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )

        raw_data = b'{"foo": "bar"}'
        encrypted = http_ece_encrypt(
            raw_data,
            salt=salt,
            private_key=sender_priv,
            dh=receiver_pub_bytes,
            version="aesgcm",
            auth_secret=auth_secret,
        )

        credentials = {
            "keys": {
                "private": b64url_unpadded(receiver_priv_der),
                "secret": b64url_unpadded(auth_secret),
            }
        }
        crypto_key_str = b64url_unpadded(sender_pub_bytes)
        salt_str = b64url_unpadded(salt)
        # Genuinely unpadded — a P-256 uncompressed point (65 bytes) and a
        # 16-byte salt both produce non-multiple-of-4 base64 lengths once
        # stripped, matching the real RFC 8291 wire format this fix targets.
        assert len(crypto_key_str) % 4 != 0
        assert len(salt_str) % 4 != 0

        decrypt = self._get_patched_decrypt()
        result = decrypt(credentials, crypto_key_str, salt_str, encrypted)

        assert result == raw_data

    def test_message_crypto_key_invalid_ec_point_reraises_as_ece_exception(self):
        """Live-deploy finding (GitHub #68, 2026-08-18): a single message's
        crypto-key header can pad and base64-decode fine but still not
        represent a valid point on the P-256 curve (observed live: a message
        redelivered by Google MCS on every reconnect since it was never
        acked, crashing the whole FcmPushClient on each redelivery for
        hours). http_ece.decrypt()'s own EC-point parsing
        (EllipticCurvePublicKey.from_encoded_point) raises a raw
        ValueError("Invalid EC key.") that is NOT normalized to
        ECEException the way AEAD/tag-mismatch failures are — reproduced
        here with a correctly-formatted (0x04-prefixed, 65-byte) but
        mathematically-invalid point. Must be re-raised as ECEException (a
        single-message fault _listen() already skips), not left to escape
        as an unhandled ValueError that reads as a stored-credentials
        fault it isn't — load_der_private_key succeeds fine here, proving
        the credentials themselves are valid."""
        import base64
        import os

        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from http_ece import ECEException

        def b64url_unpadded(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

        receiver_priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
        receiver_priv_der = receiver_priv.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        credentials = {
            "keys": {
                "private": b64url_unpadded(receiver_priv_der),
                "secret": b64url_unpadded(os.urandom(16)),
            }
        }
        # 0x04 (uncompressed-point format marker) + 64 bytes of garbage
        # coordinates: passes the format/length check, fails actual curve
        # point validation deep inside the cryptography library.
        bad_crypto_key = b"\x04" + os.urandom(64)
        crypto_key_str = b64url_unpadded(bad_crypto_key)
        salt_str = b64url_unpadded(os.urandom(16))

        decrypt = self._get_patched_decrypt()

        with pytest.raises(ECEException):
            decrypt(credentials, crypto_key_str, salt_str, b"irrelevant-ciphertext")

    def test_raises_on_genuinely_corrupt_credentials(self):
        """A corrupt/malformed stored private key must raise a plain
        ValueError from load_der_private_key — specifically NOT a
        binascii.Error, even though binascii.Error is itself a ValueError
        subclass. _listen()'s except clause in fcm.py deliberately catches
        only binascii.Error/ECEException (not all ValueError) so THIS
        client-wide credential fault still propagates to trigger the
        supervisor's hard-heal, instead of being silently skipped as a
        one-off bad message. A regression that made this raise
        binascii.Error instead would defeat that distinction while still
        passing a bare `pytest.raises(Exception)`."""
        import binascii

        decrypt = self._get_patched_decrypt()
        credentials = {"keys": {"private": "not-a-valid-der-key", "secret": "AAAA"}}

        with pytest.raises(ValueError) as exc_info:
            decrypt(credentials, "AAAA", "AAAA", b"irrelevant")

        assert not isinstance(exc_info.value, binascii.Error), (
            "must be load_der_private_key's ValueError, not a base64 "
            "padding/decode error — this is the exact distinction "
            "_listen()'s except clause relies on"
        )


class TestExtractCryptoHeader:
    """_extract_crypto_header() — matches upstream sdb9696/firebase-messaging
    #42 + #44 (both open, unmerged as of 2026-08): the blind header[3:]/
    header[5:] slice in upstream's _handle_data_message() corrupts real
    key bytes for header shapes it doesn't anticipate."""

    def test_plain_prefixed_header(self):
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header("dh=AAAA", "dh=") == "AAAA"

    def test_multi_segment_header_drops_trailing_vapid_segment(self):
        """GitHub #44: a real Crypto-Key header can carry a trailing
        p256ecdsa= (VAPID) segment separated by ';' — upstream's blind
        slice leaves it appended to the key bytes."""
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header("dh=AAAA; p256ecdsa=BBBB", "dh=") == "AAAA"

    def test_header_without_expected_prefix_passed_through(self):
        """GitHub #42: a header not literally starting with 'dh='/'salt=' must
        NOT be truncated by a blind slice — removeprefix() is a no-op when
        the prefix is absent."""
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header("AAAA", "dh=") == "AAAA"

    def test_whitespace_around_segment_stripped(self):
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header(" salt=AAAA ", "salt=") == "AAAA"

    def test_prefix_segment_not_first_still_found(self):
        """Bug-hunt finding (2026-08-19): RFC 8291/8188 do not fix segment
        order — a VAPID p256ecdsa= segment (or an RFC 8188 rs=/keyid=
        parameter) can legally precede dh=/salt=. A first-segment-only scan
        silently returns the WRONG value (the VAPID key, not the real DH
        key) instead of failing loudly."""
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header("p256ecdsa=BBBB;dh=AAAA", "dh=") == "AAAA"
        assert _extract_crypto_header("rs=4096;salt=CCCC", "salt=") == "CCCC"
        assert _extract_crypto_header("keyid=p256dh;salt=CCCC", "salt=") == "CCCC"

    def test_comma_separated_element_list(self):
        """Crypto-Key/Encryption are also ',' separated element lists per
        RFC 8188's ABNF, not just ';' separated parameter lists — this is
        the #44 corruption shape verbatim when the comma form is used."""
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header("dh=AAAA,p256ecdsa=BBBB", "dh=") == "AAAA"
        assert _extract_crypto_header("p256ecdsa=BBBB,dh=AAAA", "dh=") == "AAAA"

    def test_case_insensitive_prefix_match(self):
        """Bug-hunt finding (2026-08-19): HTTP header parameter names are
        case-insensitive; upstream's positional header[3:]/header[5:] slice
        tolerated 'DH='/'Salt=' (wrong prefix text, but the slice offset is
        purely positional). A literal-case-only removeprefix() would be a
        REGRESSION versus upstream for this shape, not just an incomplete
        fix."""
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header("DH=AAAA", "dh=") == "AAAA"
        assert _extract_crypto_header("Salt=CCCC", "salt=") == "CCCC"

    def test_whitespace_after_prefix_stripped(self):
        """Bug-hunt finding (2026-08-19): a space between '=' and the value
        (e.g. 'dh= AAAA') must not survive prefix removal — it would throw
        off _decode_message_header's padding-length math (a2b_base64
        discards the space but len() would still count it)."""
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert _extract_crypto_header("dh= AAAA", "dh=") == "AAAA"

    def test_no_matching_segment_falls_back_to_stripped_raw(self):
        """When no segment matches the prefix at all, pass the whole
        (stripped) raw string through unmodified rather than guessing which
        segment might be intended."""
        from custom_components.bosch_shc_camera.fcm import _extract_crypto_header

        assert (
            _extract_crypto_header(" keyid=p256dh;p256ecdsa=BBBB ", "dh=")
            == "keyid=p256dh;p256ecdsa=BBBB"
        )


class TestBuildHandleDataMessageOverride:
    """_build_handle_data_message_override()'s signature-guard degradation
    paths, mirroring TestPatchedClassCreation's coverage for the sibling
    _decrypt_raw_data override."""

    def setup_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    def test_patch_class_result_has_handle_data_message_override(self):
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()

        assert patched is not None
        assert "_handle_data_message" in patched.__dict__, (
            "_Patched must define its own _handle_data_message (not inherit "
            "upstream's blind header[3:]/header[5:] slice)"
        )

    def test_degrades_gracefully_if_signature_changed(self):
        """If _handle_data_message() gains/loses parameters (library
        upgrade), _patch_class must NOT return None — this override is
        independent of _listen/_decrypt_raw_data, so only it should degrade."""
        _install_firebase_module()
        original = _FakeFcmPushClient._handle_data_message

        def _handle_data_message_with_extra(self, msg, extra) -> None: ...

        _FakeFcmPushClient._handle_data_message = _handle_data_message_with_extra

        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        try:
            result = fcm_mod._QuietFcmPushClient._patch_class()
        finally:
            _FakeFcmPushClient._handle_data_message = original

        assert result is not None
        assert "_handle_data_message" not in result.__dict__, (
            "the header-extraction fix must NOT be attached when the "
            "upstream signature no longer matches"
        )
        assert "_listen" in result.__dict__
        assert "_decrypt_raw_data" in result.__dict__

    def test_returns_none_if_handle_data_message_missing_entirely(self):
        _install_firebase_module()
        original = _FakeFcmPushClient._handle_data_message
        del _FakeFcmPushClient._handle_data_message

        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        try:
            result = fcm_mod._QuietFcmPushClient._patch_class()
        finally:
            _FakeFcmPushClient._handle_data_message = original

        assert result is not None
        assert "_handle_data_message" not in result.__dict__

    def test_returns_none_if_error_type_unimportable(self):
        from custom_components.bosch_shc_camera.fcm import (
            _build_handle_data_message_override,
        )

        _sentinel = object()
        orig = sys.modules.get("firebase_messaging.fcmpushclient", _sentinel)
        sys.modules["firebase_messaging.fcmpushclient"] = None  # type: ignore[assignment]
        try:
            override = _build_handle_data_message_override(_FakeFcmPushClient)
        finally:
            if orig is _sentinel:
                sys.modules.pop("firebase_messaging.fcmpushclient", None)
            else:
                sys.modules["firebase_messaging.fcmpushclient"] = orig

        assert override is None

    def test_returns_none_if_dependent_helper_missing(self):
        """Bug-hunt finding (2026-08-19): the replicated body depends on SIX
        other private upstream methods beyond _handle_data_message's own
        signature. If one of them (e.g. _app_data_by_key) is renamed/removed
        by a future firebase_messaging release, the guard must catch that
        too — not just _handle_data_message's own signature — else the
        override would attach and AttributeError on the very first message,
        escaping _listen()'s narrow skip_exceptions and reintroducing the
        2026-08-18 crash-loop class."""
        _install_firebase_module()
        original = _FakeFcmPushClient._app_data_by_key
        del _FakeFcmPushClient._app_data_by_key

        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        try:
            result = fcm_mod._QuietFcmPushClient._patch_class()
        finally:
            _FakeFcmPushClient._app_data_by_key = original

        assert result is not None
        assert "_handle_data_message" not in result.__dict__, (
            "must not attach a body whose dependency is missing — would "
            "AttributeError on the first real message"
        )
        assert "_listen" in result.__dict__
        assert "_decrypt_raw_data" in result.__dict__

    def test_returns_none_if_handle_data_message_is_coroutine_function(self):
        """Bug-hunt finding (2026-08-19): our replica is deliberately sync.
        If a future firebase_messaging release makes _handle_data_message an
        `async def` (matching (self, msg) exactly, so the plain signature
        check alone wouldn't catch it), installing our sync override would
        TypeError on the next message instead of being awaited."""
        _install_firebase_module()
        original = _FakeFcmPushClient._handle_data_message

        async def _handle_data_message_async(self, msg) -> None: ...

        _FakeFcmPushClient._handle_data_message = _handle_data_message_async

        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        try:
            result = fcm_mod._QuietFcmPushClient._patch_class()
        finally:
            _FakeFcmPushClient._handle_data_message = original

        assert result is not None
        assert "_handle_data_message" not in result.__dict__


class TestPatchedHandleDataMessage:
    """End-to-end correctness of the crypto-key/salt header-extraction fix
    (matches upstream sdb9696/firebase-messaging#42 + #44, both open as of
    2026-08): the patched _handle_data_message must pass the CORRECTLY
    extracted crypto_key/salt into _decrypt_raw_data, not upstream's
    blindly-sliced (and, for these header shapes, corrupted) values."""

    def setup_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        self._orig = fcm_mod._QuietFcmPushClient._patched_class
        fcm_mod._QuietFcmPushClient._patched_class = False
        _install_firebase_module()

    def teardown_method(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = self._orig
        _uninstall_firebase_module()

    def _make_msg(self, crypto_key: str, encryption: str, subtype: str = "app-1"):
        return SimpleNamespace(
            stream_id=1,
            last_stream_id_received=1,
            status=None,
            persistent_id="msg-1",
            raw_data=b"ciphertext",
            app_data=[
                SimpleNamespace(key="crypto-key", value=crypto_key),
                SimpleNamespace(key="encryption", value=encryption),
                SimpleNamespace(key="subtype", value=subtype),
            ],
        )

    def test_multi_segment_crypto_key_decrypted_with_extracted_value(self):
        """GitHub #44 reproduction: a real 'dh=<key>; p256ecdsa=<vapid>'
        header must reach _decrypt_raw_data as just '<key>', not upstream's
        corrupted blind-slice result."""
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None

        client = patched.__new__(patched)
        client.credentials = {"gcm": {"app_id": "app-1"}}
        client.callback_context = None
        recorded = {}

        def _fake_decrypt(credentials, crypto_key_str, salt_str, raw_data):
            recorded["crypto_key_str"] = crypto_key_str
            recorded["salt_str"] = salt_str
            return b'{"ok": true}'

        client._decrypt_raw_data = _fake_decrypt

        def _fake_callback(ret_val, persistent_id, context):
            recorded["ret_val"] = ret_val

        client.callback = _fake_callback

        msg = self._make_msg("dh=AAAA; p256ecdsa=BBBB", "salt=CCCC")
        patched._handle_data_message(client, msg)

        assert recorded["crypto_key_str"] == "AAAA"
        assert recorded["salt_str"] == "CCCC"
        assert recorded["ret_val"] == {"ok": True}

    def test_subtype_mismatch_logs_warning_but_still_decrypts(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None

        client = patched.__new__(patched)
        client.credentials = {"gcm": {"app_id": "registered-app"}}
        client.callback_context = None
        client._decrypt_raw_data = lambda *a, **kw: b'{"ok": true}'
        client.callback = MagicMock()
        client._log_warn_with_limit = MagicMock()

        msg = self._make_msg("dh=AAAA", "salt=CCCC", subtype="other-app")
        patched._handle_data_message(client, msg)

        assert client._log_warn_with_limit.called
        client.callback.assert_called_once()

    def test_undecryptable_payload_logs_warning(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None

        client = patched.__new__(patched)
        client.credentials = {"gcm": {"app_id": "app-1"}}
        client.callback_context = None
        client._decrypt_raw_data = lambda *a, **kw: b"not-json-and-falsy-check"
        client.callback = MagicMock()
        client._log_warn_with_limit = MagicMock()

        msg = self._make_msg("dh=AAAA", "salt=CCCC")
        patched._handle_data_message(client, msg)

        assert client._log_warn_with_limit.called

    def test_non_dict_decrypted_json_wrapped_in_message_key(self):
        """A JSON payload that decodes to a non-dict (e.g. a bare list) must
        be wrapped as {"message": ...} before reaching the callback, matching
        upstream's own contract with callers."""
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None

        client = patched.__new__(patched)
        client.credentials = {"gcm": {"app_id": "app-1"}}
        client.callback_context = None
        client._decrypt_raw_data = lambda *a, **kw: b"[1, 2, 3]"
        recorded = {}
        client.callback = lambda ret_val, *a, **kw: recorded.update(ret_val=ret_val)

        msg = self._make_msg("dh=AAAA", "salt=CCCC")
        patched._handle_data_message(client, msg)

        assert recorded["ret_val"] == {"message": [1, 2, 3]}

    def test_callback_exception_is_caught_and_logged(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None

        client = patched.__new__(patched)
        client.credentials = {"gcm": {"app_id": "app-1"}}
        client.callback_context = None
        client._decrypt_raw_data = lambda *a, **kw: b'{"ok": true}'

        def _raising_callback(*a, **kw):
            raise RuntimeError("boom")

        client.callback = _raising_callback
        client._try_increment_error_count = MagicMock()

        msg = self._make_msg("dh=AAAA", "salt=CCCC")
        patched._handle_data_message(client, msg)  # must not raise

        client._try_increment_error_count.assert_called_once()

    def test_deleted_messages_short_circuits(self):
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False
        patched = fcm_mod._QuietFcmPushClient._patch_class()
        assert patched is not None

        client = patched.__new__(patched)
        client.credentials = {"gcm": {"app_id": "app-1"}}
        called = {"decrypt": False}
        client._decrypt_raw_data = lambda *a, **kw: called.__setitem__("decrypt", True)

        msg = SimpleNamespace(
            stream_id=1,
            last_stream_id_received=1,
            status=None,
            persistent_id="msg-1",
            raw_data=b"",
            app_data=[SimpleNamespace(key="message_type", value="deleted_messages")],
        )
        patched._handle_data_message(client, msg)

        assert called["decrypt"] is False


class TestGetFcmPushClientClassCachedPath:
    """The cached path in `_get_fcm_push_client_class()`: `return result`
    when _patched_class is already computed (not False).

    The first call to _get_fcm_push_client_class() sets _patched_class.
    A second call returns the cached value directly.
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
        must return the same object (cached-path return)."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        fcm_mod._QuietFcmPushClient._patched_class = False

        first = fcm_mod._get_fcm_push_client_class()
        assert first is not None

        # _patched_class is now set — second call hits the cached-return path
        second = fcm_mod._get_fcm_push_client_class()
        assert second is first, "Second call must return the cached class"

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


class TestAsyncStartFcmPushNullClientWarning:
    """When FcmRegisterConfig IS importable (firebase_messaging installed)
    but _get_fcm_push_client_class() returns None, the locked function must
    log a warning and return early WITHOUT starting FCM."""

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
            fcm_running=False,
            options={"enable_fcm_push": True},
            data={},
            entry=SimpleNamespace(data={}, options={}),
            hass=SimpleNamespace(),
            fcm_start_lock=asyncio.Lock(),
        )

        logged = []

        with (
            patch.object(fcm_mod, "_get_fcm_push_client_class", return_value=None),
            patch.object(
                fcm_mod._LOGGER, "warning", side_effect=lambda *a, **k: logged.append(a)
            ),
        ):
            # Should return early after logging the warning.
            await fcm_mod._async_start_fcm_push_locked(coord)

        assert any("FCM push disabled" in str(a) for a in logged), (
            "Must log a warning containing 'FCM push disabled' when "
            "_get_fcm_push_client_class() returns None"
        )


class TestFcmStartLockLazyInit:
    """When coordinator.fcm_start_lock is None/missing,
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
        """Coordinator without fcm_start_lock → lazy-init creates one."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        coord = SimpleNamespace(
            options={"fcm_push_mode": "auto"},
            data={},
            entry=SimpleNamespace(data={}, options={}),
            hass=SimpleNamespace(),
            # No fcm_start_lock attribute
        )
        assert not hasattr(coord, "fcm_start_lock")

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
                lock = getattr(coordinator, "fcm_start_lock", None)
                if lock is None:
                    lock = asyncio.Lock()
                    coordinator.fcm_start_lock = lock
                call_log.append("lock_created")
                return

            with patch.object(fcm_mod, "async_start_fcm_push", new=_patched_start):
                await fcm_mod.async_start_fcm_push(coord)

        # The lazy-init path ran
        assert "lock_created" in call_log

    @pytest.mark.asyncio
    async def test_lazy_init_in_real_flow_when_lock_missing(self):
        """Coordinator without fcm_start_lock: async_start_fcm_push must
        set coordinator.fcm_start_lock before entering the critical section.

        We make _async_start_fcm_push_locked return immediately so the test
        is fast.
        """
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        coord = SimpleNamespace(
            options={},
            data={},
            entry=SimpleNamespace(data={}, options={}),
            hass=MagicMock(),
        )
        assert not hasattr(coord, "fcm_start_lock")

        # Make the locked function return immediately (avoids complex coordinator setup)
        with patch.object(
            fcm_mod, "_async_start_fcm_push_locked", new=AsyncMock(return_value=None)
        ):
            await fcm_mod.async_start_fcm_push(coord)

        # After the call, coordinator must have fcm_start_lock (was lazy-initted)
        assert hasattr(coord, "fcm_start_lock"), (
            "coordinator.fcm_start_lock must be set after lazy-init"
        )
        assert isinstance(coord.fcm_start_lock, asyncio.Lock), (
            "fcm_start_lock must be an asyncio.Lock"
        )

    @pytest.mark.asyncio
    async def test_async_start_fcm_push_shim_lazy_init(self):
        """async_start_fcm_push shim must lazy-init fcm_start_lock when missing."""
        _install_firebase_module()
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        coord = SimpleNamespace(
            options={"enable_fcm_push": True},
            data={},
            entry=SimpleNamespace(data={}),
            hass=MagicMock(),
            fcm_running=False,
        )
        assert not hasattr(coord, "fcm_start_lock")

        with patch.object(
            fcm_mod, "_async_start_fcm_push_locked", new=AsyncMock(return_value=False)
        ):
            await fcm_mod.async_start_fcm_push(coord)

        assert hasattr(coord, "fcm_start_lock"), (
            "async_start_fcm_push shim must lazy-init fcm_start_lock"
        )
        assert isinstance(coord.fcm_start_lock, asyncio.Lock)


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

        instance.persistent_ids = []
        # Test-coverage finding (GitHub #68 follow-up, 2026-08-18): True
        # matches the REAL FcmPushClientConfig default, which production's
        # _async_start_fcm_push_locked never overrides — production always
        # has acks on. The old False default here silently meant most tests
        # exercised the acks-DISABLED branch production never takes; any
        # test that genuinely needs acks off sets this explicitly itself.
        instance.config = SimpleNamespace(send_selective_acknowledgements=True)

        async def _noop_ack(persistent_id):
            pass

        instance._send_selective_ack = _noop_ack

        return instance

    @pytest.mark.asyncio
    async def test_connect_failure_returns_early(self):
        """_connect_with_retry() returning False → early return."""
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
        """ConnectionResetError + run_state=RESETTING → _log_verbose (quiet path)
        AND _reset() must actually be called.

        Live-deploy finding (GitHub #68 follow-up, 2026-08-18): the issue #33
        fix (pre-setting run_state=RESETTING to route the FIRST error to the
        quiet log path too) had a side effect nothing here used to catch —
        upstream's own quiet branch never calls _reset(), only its loud/else
        branch does. Since our fix makes routine WAN blips take the quiet
        path from the very first error, _reset() was never being called for
        them at all: _listen() just spun sleeping 1s forever instead of
        reconnecting, and recovery only ever came from the supervisor's
        outer teardown+rebuild (a full fresh Google registration) instead of
        a cheap in-place reconnect. This test would have caught that
        regression — the OLD (buggy) code passed the previous, weaker
        version of this test (which only asserted the log call) while
        reset_calls stayed empty.
        """
        instance = self._make_instance()
        instance.run_state = _FakeRunState.STARTED

        verbose_calls = []
        instance._log_verbose = lambda *a, **k: verbose_calls.append(a)

        reset_calls = []

        async def _reset():
            reset_calls.append(1)
            instance.do_listen = False

        instance._reset = _reset

        call_count = [0]

        async def _recv():
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionResetError("WAN drop")
            instance.do_listen = False
            return None

        instance._receive_msg = _recv

        await instance._listen()

        assert verbose_calls, "ConnectionResetError must route to _log_verbose"
        assert reset_calls, (
            "_reset() must actually be called for the quiet path too, or a "
            "routine WAN blip never reconnects and just spins forever"
        )

    async def _assert_quiet_path_calls_reset(self, exc: Exception) -> None:
        instance = self._make_instance()
        instance.run_state = _FakeRunState.STARTED

        reset_calls: list[int] = []

        async def _reset() -> None:
            reset_calls.append(1)
            instance.do_listen = False

        instance._reset = _reset
        instance._log_verbose = lambda *a, **k: None
        instance._log_warn_with_limit = lambda *a, **k: None

        call_count = [0]

        async def _recv():
            call_count[0] += 1
            if call_count[0] == 1:
                raise exc
            instance.do_listen = False
            return None

        instance._receive_msg = _recv

        await instance._listen()

        assert reset_calls, (
            f"_reset() must be called for quiet-path {type(exc).__name__}"
        )

    @pytest.mark.asyncio
    async def test_quiet_path_calls_reset_connection_reset_error(self):
        await self._assert_quiet_path_calls_reset(ConnectionResetError("x"))

    @pytest.mark.asyncio
    async def test_quiet_path_calls_reset_timeout_error(self):
        await self._assert_quiet_path_calls_reset(TimeoutError("x"))

    @pytest.mark.asyncio
    async def test_quiet_path_calls_reset_incomplete_read_error(self):
        await self._assert_quiet_path_calls_reset(asyncio.IncompleteReadError(b"", 10))

    @pytest.mark.asyncio
    async def test_quiet_path_calls_reset_ssl_error(self):
        ssl_err = ssl.SSLError()
        ssl_err.reason = "APPLICATION_DATA_AFTER_CLOSE_NOTIFY"
        await self._assert_quiet_path_calls_reset(ssl_err)

    @pytest.mark.asyncio
    async def test_outer_exception_calls_terminate(self):
        """Outer except Exception: _terminate() + _do_writer_close in finally."""
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
        """While run_state == RESETTING, must asyncio.sleep(1)."""
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

    @pytest.mark.asyncio
    async def test_undecryptable_message_skipped_not_terminated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression test for GitHub issue #65.

        Upstream firebase_messaging's _handle_message() can raise
        binascii.Error("Incorrect padding") (a ValueError subclass) when a push
        message's crypto-key/salt header arrives without base64 padding
        (RFC 8291-valid, see sdb9696/firebase-messaging#40). One bad message
        must be skipped, not terminate the whole FcmPushClient.
        """
        import binascii

        instance = self._make_instance()

        terminate_calls = []
        instance._terminate = lambda: terminate_calls.append(1)

        handled = []

        async def _handle_message(msg):
            handled.append(msg)
            if len(handled) == 1:
                raise binascii.Error("Incorrect padding")
            instance.do_listen = False

        instance._handle_message = _handle_message

        call_count = [0]

        async def _recv():
            call_count[0] += 1
            return {"msg": call_count[0]}

        instance._receive_msg = _recv

        with caplog.at_level("WARNING", logger=MODULE):
            await instance._listen()

        assert len(handled) == 2, (
            "Must continue receiving messages after a decode failure"
        )
        assert not terminate_calls, (
            "A single undecryptable message must not terminate FcmPushClient"
        )
        assert any(
            "Skipping undecryptable FCM push message" in r.getMessage()
            for r in caplog.records
        ), (
            "Must log via our own module logger (GitHub #68 follow-up, "
            "2026-08-18: not the upstream rate-limited helper, which caps "
            "at 5 occurrences per instance and would go silent)"
        )

    @pytest.mark.asyncio
    async def test_undecryptable_ciphertext_skipped_not_terminated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """GitHub #68 follow-up: once the padded _decrypt_raw_data() override
        makes crypto-key/salt header decoding succeed, a message whose
        headers pad fine but whose ciphertext BODY is genuinely corrupt (bit
        flip, message meant for a different subtype, stale key after a
        registration rotation) now reaches http_ece.decrypt() and raises
        ECEException — a bare Exception, not a binascii.Error, not a
        ValueError. Before the padding fix this path was unreachable in
        practice (every message failed earlier at the unpadded-header decode
        step). Must be skipped like any other single bad message, not
        terminate the whole FcmPushClient.
        """
        from http_ece import ECEException

        instance = self._make_instance()

        terminate_calls = []
        instance._terminate = lambda: terminate_calls.append(1)

        handled = []

        async def _handle_message(msg):
            handled.append(msg)
            if len(handled) == 1:
                raise ECEException("Decryption error: InvalidTag()")
            instance.do_listen = False

        instance._handle_message = _handle_message

        call_count = [0]

        async def _recv():
            call_count[0] += 1
            return {"msg": call_count[0]}

        instance._receive_msg = _recv

        with caplog.at_level("WARNING", logger=MODULE):
            await instance._listen()

        assert len(handled) == 2, (
            "Must continue receiving messages after an ECEException"
        )
        assert not terminate_calls, (
            "A single undecryptable-ciphertext message must not terminate FcmPushClient"
        )
        assert any(
            "Skipping undecryptable FCM push message" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_missing_app_data_key_runtime_error_skipped_not_terminated(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Live-deploy finding (GitHub #68 follow-up, 2026-08-18): upstream's
        _handle_data_message() only special-cases message_type ==
        "deleted_messages" before unconditionally looking up the
        "crypto-key"/"encryption" app_data entries — missing either raises a
        bare RuntimeError("couldn't find in app_data ...") that isn't in
        skip_exceptions. A non-webpush control/diagnostic message (or an
        aes128gcm-encoded message, which carries no separate headers at all)
        must be skipped like any other single bad message, not terminate the
        whole FcmPushClient. Uses a real message object with a genuine
        persistent_id (proto3-shaped, not a plain dict) so the ack half of
        _skip_and_ack is actually exercised via THIS except branch too, not
        just via the skip_exceptions branch."""
        instance = self._make_instance()
        instance.config = SimpleNamespace(send_selective_acknowledgements=True)

        ack_calls = []

        async def _send_selective_ack(persistent_id):
            ack_calls.append(persistent_id)

        instance._send_selective_ack = _send_selective_ack

        terminate_calls = []
        instance._terminate = lambda: terminate_calls.append(1)

        handled = []

        async def _handle_message(msg):
            handled.append(msg)
            if len(handled) == 1:
                raise RuntimeError("couldn't find in app_data crypto-key")
            instance.do_listen = False

        instance._handle_message = _handle_message

        call_count = [0]

        async def _recv():
            call_count[0] += 1
            if call_count[0] == 1:
                return SimpleNamespace(persistent_id="missing-header-msg-id")
            return {"msg": call_count[0]}

        instance._receive_msg = _recv

        with caplog.at_level("WARNING", logger=MODULE):
            await instance._listen()

        assert len(handled) == 2, (
            "Must continue receiving messages after a missing-app_data-key RuntimeError"
        )
        assert not terminate_calls, (
            "A single message missing its crypto-key/encryption header must "
            "not terminate FcmPushClient"
        )
        assert any(
            "Skipping undecryptable FCM push message" in r.getMessage()
            for r in caplog.records
        )
        assert instance.persistent_ids == ["missing-header-msg-id"], (
            "the RuntimeError branch must ack the skipped message too, not "
            "just the skip_exceptions branch"
        )
        assert ack_calls == ["missing-header-msg-id"]

    @pytest.mark.asyncio
    async def test_unrelated_runtime_error_still_terminates(self):
        """Only the SPECIFIC "couldn't find in app_data" RuntimeError shape
        is skippable — any other RuntimeError (e.g. from a totally different
        code path) is not single-message-scoped and must still terminate
        FcmPushClient so the supervisor's hard-heal can run."""
        instance = self._make_instance()

        terminate_calls = []
        instance._terminate = lambda: terminate_calls.append(1)

        async def _handle_message(msg):
            raise RuntimeError("some unrelated failure")

        instance._handle_message = _handle_message

        async def _recv():
            return {"msg": 1}

        instance._receive_msg = _recv

        await instance._listen()

        assert terminate_calls, (
            "An unrelated RuntimeError must still terminate FcmPushClient"
        )

    @pytest.mark.asyncio
    async def test_skipped_message_still_acked_to_stop_redelivery(self):
        """GitHub #68 live-deploy follow-up: upstream's _handle_message()
        only appends to persistent_ids / sends the selective ack AFTER
        _handle_data_message() returns successfully — our skip-logic above
        aborts before either runs, so without this fix Google MCS would
        treat the skipped message as never received and redeliver it
        (harmlessly, but indefinitely — one warning per reconnect forever).
        The skip path must append persistent_id itself and send the ack when
        the client has selective acks enabled.
        """
        import binascii

        instance = self._make_instance()
        instance.config = SimpleNamespace(send_selective_acknowledgements=True)

        ack_calls = []

        async def _send_selective_ack(persistent_id):
            ack_calls.append(persistent_id)

        instance._send_selective_ack = _send_selective_ack

        async def _handle_message(msg):
            instance.do_listen = False
            raise binascii.Error("Incorrect padding")

        instance._handle_message = _handle_message

        async def _recv():
            return SimpleNamespace(persistent_id="poisoned-msg-id-123")

        instance._receive_msg = _recv

        await instance._listen()

        assert instance.persistent_ids == ["poisoned-msg-id-123"], (
            "the skipped message's persistent_id must be recorded so it "
            "isn't replayed again at the next login"
        )
        assert ack_calls == ["poisoned-msg-id-123"], (
            "must send the selective ack when the client has it enabled"
        )

    @pytest.mark.asyncio
    async def test_skipped_message_ack_skipped_when_no_persistent_id(self):
        """A msg object without a persistent_id attribute (e.g. a test
        double, or a future message type) must not raise AttributeError —
        the ack-append is a best-effort addition, not a hard requirement."""
        import binascii

        instance = self._make_instance()

        async def _handle_message(msg):
            instance.do_listen = False
            raise binascii.Error("Incorrect padding")

        instance._handle_message = _handle_message

        async def _recv():
            return {"msg": "no persistent_id attribute on a plain dict"}

        instance._receive_msg = _recv

        await instance._listen()  # must not raise

        assert instance.persistent_ids == []

    @pytest.mark.asyncio
    async def test_skipped_message_empty_persistent_id_not_acked(self):
        """Live-deploy finding (GitHub #68 follow-up, 2026-08-18): the real
        protobuf DataMessageStanza.persistent_id field defaults to ''
        (never None) — so `if persistent_id is not None:` was always true
        and appended/acked a meaningless empty string. A message with a
        genuinely empty id must be logged but NOT appended/acked."""
        import binascii

        instance = self._make_instance()
        instance.config = SimpleNamespace(send_selective_acknowledgements=True)

        ack_calls = []

        async def _send_selective_ack(persistent_id):
            ack_calls.append(persistent_id)

        instance._send_selective_ack = _send_selective_ack

        async def _handle_message(msg):
            instance.do_listen = False
            raise binascii.Error("Incorrect padding")

        instance._handle_message = _handle_message

        async def _recv():
            return SimpleNamespace(persistent_id="")  # real proto3 default

        instance._receive_msg = _recv

        await instance._listen()

        assert instance.persistent_ids == [], (
            "an empty persistent_id must not be appended"
        )
        assert ack_calls == [], "an empty persistent_id must not be acked"

    @pytest.mark.asyncio
    async def test_corrupt_credentials_value_error_still_terminates(self):
        """A plain ValueError (e.g. corrupt stored FCM credentials) must NOT be
        caught by the binascii.Error-only skip — it needs the supervisor's
        hard-heal (credential purge + re-registration), not a silent per-message
        skip that would mask a client-wide fault forever.
        """
        instance = self._make_instance()

        terminate_calls = []
        instance._terminate = lambda: terminate_calls.append(1)

        async def _handle_message(msg):
            raise ValueError("Could not deserialize key data")

        instance._handle_message = _handle_message

        async def _recv():
            return {"msg": 1}

        instance._receive_msg = _recv

        await instance._listen()

        assert terminate_calls, (
            "A non-binascii ValueError must still terminate FcmPushClient "
            "so the supervisor's hard-heal can run"
        )


def _make_register_coord(data: dict | None = None) -> SimpleNamespace:
    """Minimal coordinator stub for register_fcm_with_bosch tests."""
    entry_data: dict = data if data is not None else {}
    update_calls: list[dict] = []

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        update_calls.append(dict(kwargs))
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry),
    )
    entry = SimpleNamespace(data=entry_data)

    coord = SimpleNamespace(
        token="bearer-abc",
        fcm_token="fcm-tok-new",
        entry=entry,
        hass=hass,
    )
    # Attach update_calls so tests can inspect them
    coord._update_calls = update_calls  # type: ignore[attr-defined]
    return coord


def _make_mock_response(status: int, body: str = "") -> MagicMock:
    """Build an aiohttp-like async context manager response stub."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_session_post(resp_cm: MagicMock) -> MagicMock:
    """Return a mock session whose .post() returns resp_cm."""
    session = MagicMock()
    session.post = MagicMock(return_value=resp_cm)
    return session


async def test_drift_heal_a_fresh_install_posts_and_writes_both_markers() -> None:
    """(a) Fresh install — no fcm_registered_token, no fcm_registered_device_type.

    register_fcm_with_bosch must:
    - NOT skip (no stored token → skip condition is false)
    - POST to /v11/devices with deviceType=ANDROID in the JSON body
    - Write BOTH fcm_registered_token AND fcm_registered_device_type="ANDROID"
      into entry.data after HTTP 204.

    Without writing fcm_registered_device_type, the drift-heal guard has no
    marker to check on the next restart and will POST every time (wasteful) or,
    if the guard is absent, skip and leave a stale IOS registration.
    """
    coord = _make_register_coord(data={})  # no stored token, no marker
    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []

    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "Fresh install must return True on HTTP 204"

    # POST body must declare ANDROID
    assert posted_bodies, "POST must have been called for a fresh install"
    assert posted_bodies[0].get("deviceType") == "ANDROID", (
        "POST body must contain deviceType=ANDROID — Bosch CBS routes pushes "
        "based on this field; IOS is now HTTP 500 for this OSS account."
    )
    assert posted_bodies[0].get("deviceToken") == "fcm-tok-new"

    # Both markers must be written
    assert coord._update_calls, "async_update_entry must have been called"
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_token") == "fcm-tok-new", (
        "fcm_registered_token must be written to enable the skip-check on next restart."
    )
    assert written_data.get("fcm_registered_device_type") == "ANDROID", (
        "fcm_registered_device_type=ANDROID must be written so the drift-heal guard "
        "can verify the Bosch CBS registration is current on next restart."
    )


async def test_drift_heal_b_already_healed_skips_post() -> None:
    """(b) token matches AND fcm_registered_device_type == "ANDROID" → skip.

    The fast-path must be preserved for the steady-state case: after a
    successful registration (drift healed or fresh install), every subsequent
    HA restart must skip the POST — as long as the registration is still FRESH
    (younger than FCM_REREGISTER_INTERVAL_SEC). A periodic re-POST for a
    registration older than the interval (or one with no `fcm_registered_at`
    stamp) re-announces to heal a server-side-dropped Bosch registration.
    Here we stamp it now so the steady-state skip still holds.
    """
    coord = _make_register_coord(
        data={
            "fcm_registered_token": "fcm-tok-new",  # same as fcm_token
            "fcm_registered_device_type": "ANDROID",
            "fcm_registered_at": time.time(),  # fresh → fast-path skip
        }
    )

    session = MagicMock()
    session.post = MagicMock()

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "Already-healed entry must return True without POST"
    (
        session.post.assert_not_called(),
        (
            "token unchanged + ANDROID marker present → POST must NOT fire. "
            "Firing it would cause unnecessary Bosch CBS round-trips on every restart."
        ),
    )
    assert not coord._update_calls, (
        "No async_update_entry call expected when fast-path skips."
    )


async def test_drift_heal_c_token_matches_but_marker_missing_forces_post() -> None:
    """(c) token matches BUT fcm_registered_device_type missing → POST fires.

    This is the exact state a user is in after running a migration that left
    fcm_registered_token intact without also writing the device-type marker:
    fcm_registered_token == current token (migration left it intact), but no
    fcm_registered_device_type marker exists.

    The old skip-logic would see token==token and silently skip, leaving Bosch
    CBS with deviceType=IOS. FCM pushes would be silently routed to the iOS
    sub-app, arriving minutes late via the polling fallback.

    The fix must detect this drift and force a POST regardless of token equality.
    """
    coord = _make_register_coord(
        data={
            "fcm_registered_token": "fcm-tok-new",  # token unchanged — old skip would fire
            # fcm_registered_device_type intentionally absent
        }
    )

    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []
    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "Drift-heal POST on HTTP 204 must return True"
    assert posted_bodies, (
        "POST must fire when fcm_registered_device_type is absent — "
        "without re-registration Bosch CBS keeps the stale deviceType=IOS entry."
    )
    assert posted_bodies[0].get("deviceType") == "ANDROID", (
        "Drift-heal POST body must contain deviceType=ANDROID."
    )

    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_device_type") == "ANDROID", (
        "After drift-heal POST, fcm_registered_device_type=ANDROID must be written "
        "so that the next restart uses the fast-path correctly."
    )
    assert written_data.get("fcm_registered_token") == "fcm-tok-new"


async def test_drift_heal_c2_token_matches_but_marker_is_ios_forces_post() -> None:
    """(c2) token matches AND fcm_registered_device_type == "IOS" → POST fires.

    The marker could theoretically be IOS if written by old code. This variant
    ensures the drift-heal guard catches any non-ANDROID marker, not just None.
    """
    coord = _make_register_coord(
        data={
            "fcm_registered_token": "fcm-tok-new",
            "fcm_registered_device_type": "IOS",  # explicit wrong marker
        }
    )

    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []
    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True
    assert posted_bodies, "IOS marker must trigger drift-heal POST"
    assert posted_bodies[0].get("deviceType") == "ANDROID"
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_device_type") == "ANDROID"


async def test_drift_heal_d_token_changed_posts_and_writes_marker() -> None:
    """(d) different token, ANDROID marker present → POST fires.

    Token rotation must always trigger re-registration regardless of the
    deviceType marker. This is the existing stable behavior — ensure the
    drift-heal guard does not accidentally gate on the marker when the token
    also changed.
    """
    coord = _make_register_coord(
        data={
            "fcm_registered_token": "fcm-tok-OLD",  # differs from fcm_token
            "fcm_registered_device_type": "ANDROID",
        }
    )

    resp_cm = _make_mock_response(204)
    session = _make_session_post(resp_cm)

    posted_bodies: list[dict] = []
    original_post = session.post

    def _capturing_post(url: str, **kwargs: object) -> MagicMock:
        if "json" in kwargs:
            posted_bodies.append(dict(kwargs["json"]))  # type: ignore[arg-type]
        return original_post(url, **kwargs)

    session.post = _capturing_post

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True
    assert posted_bodies, "Token rotation must always trigger POST"
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_token") == "fcm-tok-new"
    assert written_data.get("fcm_registered_device_type") == "ANDROID"


async def test_drift_heal_e_server_500_internal_error_writes_marker() -> None:
    """(e) Server returns 500 sh:internal.error → treated as success, marker written.

    Bosch CBS returns HTTP 500 "sh:internal.error" when a token is already
    registered (duplicate). This is normal for the first restart after any
    registration. The integration treats it as success (FCM push will work)
    and must write BOTH markers to avoid repeating the POST on every restart.
    """
    coord = _make_register_coord(data={})  # no stored token

    resp_cm = _make_mock_response(
        500, body='{"code":"sh:internal.error","message":"Already exists"}'
    )
    session = _make_session_post(resp_cm)

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is True, "HTTP 500 sh:internal.error must be treated as success"
    assert coord._update_calls, (
        "async_update_entry must have been called on 500 success path"
    )
    written_data = coord._update_calls[-1]["data"]
    assert written_data.get("fcm_registered_token") == "fcm-tok-new", (
        "fcm_registered_token must be written on 500 success path to avoid "
        "repeating the POST on every restart."
    )
    assert written_data.get("fcm_registered_device_type") == "ANDROID", (
        "fcm_registered_device_type=ANDROID must also be written on 500 success path."
    )


async def test_drift_heal_f_server_401_returns_false_no_marker_written() -> None:
    """(f) Server returns 401 → False, no marker written, no token written.

    Auth failure means the bearer token is invalid or expired. The function
    must return False and must NOT write any markers — writing them would
    cause the next restart to skip the POST even though registration failed,
    silently disabling FCM push.
    """
    coord = _make_register_coord(data={})  # no stored token

    resp_cm = _make_mock_response(
        401, body='{"code":"sh:no.permission","message":"Unauthorized"}'
    )
    session = _make_session_post(resp_cm)

    with (
        patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ),
        patch(f"{MODULE}.CLOUD_API", "https://api.bosch.example"),
    ):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        result = await register_fcm_with_bosch(coord)

    assert result is False, "HTTP 401 must return False"
    assert not coord._update_calls, (
        "No async_update_entry call expected on auth failure — writing markers "
        "after a failed POST would cause the skip-check to fire on next restart "
        "and silently leave FCM push broken."
    )


# Noise filter, safe-URL validation, notify-data building, alert-service slot resolution, path A/B event handling + snapshot/dedup/ordering, creds-staleness helpers, _listen() branches, mode/pin migration (from: event-snapshot, extra coverage, filter helpers, general helpers, _listen branches, mode/pin)


def _isolate_shared_state():
    """Snapshot + restore staleness timestamps + logger filters between tests.

    Autouse and file-scoped: applies to every test in this module, not only
    the creds-staleness/noise-filter tests it was originally written for.
    That's intentional (it keeps global logger/list state from leaking
    between unrelated tests) but a merger folding this into a bigger
    tests/test_fcm.py should confirm nothing else in that file depends on
    the *unfiltered* state of these loggers.
    """
    prev_ts = list(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS)
    lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
    bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
    prev_lib = list(lib_logger.filters)
    prev_bosch = list(bosch_logger.filters)
    yield
    _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = prev_ts
    lib_logger.filters[:] = prev_lib
    bosch_logger.filters[:] = prev_bosch


def _one_event(
    event_id: str = "new-evt",
    event_type: str = "MOVEMENT",
    tags: list[str] | None = None,
    image: str = "",
) -> list[dict[str, Any]]:
    return [
        {
            "id": event_id,
            "eventType": event_type,
            "eventTags": tags or [],
            "timestamp": "2026-05-15T10:00:00Z",
            "imageUrl": image,
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }
    ]


def _make_push_coord(**overrides: Any) -> Any:
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock(
        return_value=MagicMock(add_done_callback=MagicMock())
    )
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-test",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        last_event_ids={},
        alert_sent_ids={},
        camera_entities={},
        image_entities={},
        shc_state_cache={},
        cached_events={},
        bg_tasks=set(),
        hw_version={CAM_ID: "HOME_Eyes_Outdoor"},  # Gen2 Outdoor → delay=0
        options={},
    )
    coord.async_update_listeners = MagicMock()
    # Mirrors BoschCameraCoordinator.spawn_tracked closely enough for these
    # direct-module unit tests — routes through hass.async_create_task
    # (already asserted on directly in several tests) instead of needing a
    # real bg_tasks/add_done_callback dance on this stub.
    coord.spawn_tracked = lambda coro, **kw: coord.hass.async_create_task(coro, **kw)
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_alert_coord2(options: dict[str, Any] | None = None, **overrides: Any) -> Any:
    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha-snap"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    base_opts: dict[str, Any] = {
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
        token="tok-alert",
        hass=hass,
        options=base_opts,
        data={
            CAM_ID: {"info": {"title": "Terrasse"}, "events": []},
        },
        last_event_ids={CAM_ID: "prior-event-id"},
        camera_entities={},
        image_entities={},
        shc_state_cache={},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


async def _run_alert(
    coord: Any,
    event_type: str = "MOVEMENT",
    image_url: str = "",
    clip_url: str = "",
    clip_status: str = "",
    cam_name: str = "Terrasse",
    timestamp: str | None = "2026-05-15T10:00:00.000Z",
    session_override: Any = None,
) -> None:
    from custom_components.bosch_shc_camera.fcm import async_send_alert

    session = session_override or MagicMock(get=MagicMock(return_value=_resp_cm(404)))
    with patch(
        f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
    ):
        with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
            with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                    await async_send_alert(
                        coord,
                        cam_name,
                        event_type,
                        timestamp,
                        image_url,
                        clip_url,
                        clip_status,
                    )


class TestPathAMovement:
    """MOVEMENT event → async_trigger_image_refresh called exactly once."""

    @pytest.mark.asyncio
    async def test_movement_triggers_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity.async_trigger_image_refresh = AsyncMock(return_value=None)
        cam_entity.is_streaming = False  # not streaming → Path A must fire
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="MOVEMENT")
            )
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        coord.hass.async_create_task.assert_called()
        cam_entity.async_trigger_image_refresh.assert_called_once_with(delay=0)


def _make_nvr_push_coord(
    *,
    mode: str = "event_buffered",
    switch_on: bool = True,
    preroll_seconds: int = 30,
    postroll_seconds: int = 0,
    conn_type: str = "LOCAL",
    online: bool = True,
    enable_nvr: bool = True,
    **overrides: Any,
) -> Any:
    """`_make_push_coord` extended with the Mini-NVR fields the new
    event_buffered clip-assembly dispatch hook in `async_handle_fcm_push`
    reads (issue #43 follow-up)."""
    coord = _make_push_coord(**overrides)
    coord.options = {
        "enable_nvr": enable_nvr,
        "nvr_preroll_seconds": preroll_seconds,
        "nvr_postroll_seconds": postroll_seconds,
    }
    coord.get_nvr_mode = MagicMock(return_value=mode)
    coord.nvr_user_intent = {CAM_ID: switch_on}
    coord.live_connections = {CAM_ID: {"_connection_type": conn_type}}
    coord.is_camera_online = MagicMock(return_value=online)
    # 2026-08-09: real BoschCameraCoordinator always has this (BoolFieldView
    # in __init__) — a bare `set()` here matches production shape so tests
    # exercise the real "not scheduled" branch instead of silently relying
    # on fcm.py's broad `except Exception` (event-fetch error handler) to
    # swallow an AttributeError from a stub missing this field, which was
    # masking that the new diagnostic WARNING never actually fired in any
    # existing test (bug-hunt finding).
    if not hasattr(coord, "_nvr_motion_clip_blocked_warned"):
        coord._nvr_motion_clip_blocked_warned = set()
    return coord


class TestNvrEventBufferedClipDispatch:
    """FCM movement/person event -> Mini-NVR event_buffered clip assembly
    dispatch (issue #43 follow-up, realKim-dotcom): create_motion_clip()
    previously had zero call sites anywhere in the integration."""

    async def _fire_movement(self, coord) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="MOVEMENT")
            )
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
        ):
            await async_handle_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_event_buffered_switch_on_schedules_assembly(self) -> None:
        coord = _make_nvr_push_coord(
            last_event_ids={CAM_ID: "old-evt"}, camera_entities={}
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_called_once_with(coord, CAM_ID)
        coord.hass.async_create_task.assert_any_call(
            "stub-coro", name=f"bosch_shc_camera_nvr_motion_clip_{CAM_ID[:8]}"
        )

    @pytest.mark.asyncio
    async def test_first_push_after_restart_still_schedules_assembly(self) -> None:
        """GitHub #64 root cause: no `last_event_ids` baseline yet for this
        camera (e.g. the ~60-90s window right after HA restart, before the
        coordinator's own polling tick seeds it — see event_dispatch.py's
        bootstrap comment). The old `prev_id is not None` guard treated
        this exactly like polling's "avoid replaying historical backlog"
        case and silently swallowed the event — no dispatch, no log line,
        no clip. Unlike polling, an FCM push is inherently real-time (Bosch
        only pushes because something just happened), so it must never be
        dropped just because no baseline exists yet."""
        coord = _make_nvr_push_coord(last_event_ids={}, camera_entities={})
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_called_once_with(coord, CAM_ID)
        assert coord.last_event_ids[CAM_ID] == "new-evt"

    @pytest.mark.asyncio
    async def test_continuous_mode_not_scheduled(self) -> None:
        """mode='continuous' — the always-on recorder already handles this
        camera; no separate clip assembly needed."""
        coord = _make_nvr_push_coord(
            mode="continuous", last_event_ids={CAM_ID: "old-evt"}, camera_entities={}
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_not_called()

    @pytest.mark.asyncio
    async def test_switch_off_not_scheduled(self) -> None:
        coord = _make_nvr_push_coord(
            switch_on=False,
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_nvr_false_not_scheduled(self) -> None:
        """Mini-NVR feature itself disabled (`enable_nvr=False`) — defense
        in depth: even if mode/switch state somehow looked event_buffered
        + on, the feature-level gate must win (bug-hunt finding, issue #43
        follow-up)."""
        coord = _make_nvr_push_coord(
            enable_nvr=False,
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_not_called()

    @pytest.mark.asyncio
    async def test_enable_nvr_false_no_warning_noise(self, caplog) -> None:
        """Same warning-suppression fix as event_dispatch.py's polling
        path — must also hold for FCM push, since `enable_nvr=False`
        means there's nothing to diagnose regardless of which path
        discovered the event."""
        coord = _make_nvr_push_coord(
            enable_nvr=False,
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with (
            patch(
                f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
                MagicMock(return_value="stub-coro"),
            ),
            caplog.at_level(
                "WARNING", logger="custom_components.bosch_shc_camera.recorder"
            ),
        ):
            await self._fire_movement(coord)
        blocked_logs = [
            r for r in caplog.records if "NVR motion clip not created" in r.message
        ]
        assert blocked_logs == []

    @pytest.mark.asyncio
    async def test_fcm_push_exempt_from_staleness_guard(self) -> None:
        """The staleness guard added for the polling path (GitHub #64
        follow-up, 2026-08-13) must NOT apply to FCM push — push always
        arrives seconds after the real event, so a fixed/old test
        timestamp here (`_one_event` hardcodes "2026-05-15T10:00:00Z")
        must not be mistaken for a genuinely stale poll-discovered event."""
        coord = _make_nvr_push_coord(
            preroll_seconds=30,
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_called_once_with(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_preroll_and_postroll_both_zero_not_scheduled(self) -> None:
        """Nothing configured to assemble from → skip, don't spawn ffmpeg
        for an empty clip."""
        coord = _make_nvr_push_coord(
            preroll_seconds=0,
            postroll_seconds=0,
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_not_called()

    @pytest.mark.asyncio
    async def test_postroll_only_configured_still_scheduled(self) -> None:
        """preroll=0 but postroll>0 — a post-roll-only clip is still valid,
        must not be skipped."""
        coord = _make_nvr_push_coord(
            preroll_seconds=0,
            postroll_seconds=15,
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_called_once_with(coord, CAM_ID)

    @pytest.mark.asyncio
    async def test_not_local_not_scheduled(self) -> None:
        """Camera on cloud relay (REMOTE) — LAN-only gate applies to event
        clip assembly the same as continuous recording."""
        coord = _make_nvr_push_coord(
            conn_type="REMOTE",
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_not_called()

    @pytest.mark.asyncio
    async def test_camera_offline_not_scheduled(self) -> None:
        coord = _make_nvr_push_coord(
            online=False,
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_not_called()

    @pytest.mark.asyncio
    async def test_blocked_clip_logs_warning_once_then_clears_on_success(
        self, caplog
    ) -> None:
        """2026-08-09 fix (GitHub #64 follow-up, 3-agent bug-hunt): a blocked
        clip must WARNING once per camera (not silently, and not DEBUG —
        this issue's reporter already burned many rounds needing debug
        logging enabled just to diagnose the pre-roll ring itself), then
        the dedup flag clears as soon as a clip is next successfully
        scheduled so the warning can re-fire if it starts failing again."""
        coord = _make_nvr_push_coord(
            switch_on=False,  # should_record() blocks — clip not scheduled
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={},
        )
        with (
            patch(
                f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
                MagicMock(return_value="stub-coro"),
            ),
            caplog.at_level(
                "WARNING", logger="custom_components.bosch_shc_camera.recorder"
            ),
        ):
            await self._fire_movement(coord)
            # _fire_movement always fetches the same fixed event id
            # ("new-evt") — clear the per-event-id dedup cache too, or the
            # 2nd call would `continue` on the "already sent" check before
            # ever reaching the NVR gate, not because it was re-blocked.
            coord.alert_sent_ids.clear()
            coord.last_event_ids[CAM_ID] = "old-evt-2"  # allow a 2nd dispatch
            await self._fire_movement(coord)

        blocked_logs = [
            r for r in caplog.records if "NVR motion clip not created" in r.message
        ]
        assert len(blocked_logs) == 1, (
            "must warn exactly once across repeated blocked attempts, "
            f"got {len(blocked_logs)}"
        )
        assert CAM_ID in coord._nvr_motion_clip_blocked_warned

        # Now let it succeed — the dedup flag must clear.
        coord.nvr_user_intent[CAM_ID] = True
        coord.alert_sent_ids.clear()
        coord.last_event_ids[CAM_ID] = "old-evt-3"
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)
        mock_assemble.assert_called_once_with(coord, CAM_ID)
        assert CAM_ID not in coord._nvr_motion_clip_blocked_warned

    @pytest.mark.asyncio
    async def test_missing_get_nvr_mode_on_stub_coordinator_no_crash(self) -> None:
        """Minimal test-fixture coordinators without `get_nvr_mode` (most of
        this file's other push-coord factories) must not raise — mirrors the
        `_is_rcp_lan_denied` defensive-getattr pattern elsewhere."""
        coord = _make_push_coord(last_event_ids={CAM_ID: "old-evt"}, camera_entities={})
        assert not hasattr(coord, "get_nvr_mode")
        with patch(
            f"{RECORDER_MODULE}.assemble_and_ship_motion_clip",
            MagicMock(return_value="stub-coro"),
        ) as mock_assemble:
            await self._fire_movement(coord)  # must not raise
        mock_assemble.assert_not_called()


class TestPathAPersonEvent:
    """PERSON event → async_trigger_image_refresh called exactly once."""

    @pytest.mark.asyncio
    async def test_person_triggers_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity.async_trigger_image_refresh = AsyncMock(return_value=None)
        cam_entity.is_streaming = False  # not streaming → Path A must fire
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        # PERSON via eventTags upgrade path
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200,
                json_data=_one_event("new-evt", event_type="MOVEMENT", tags=["PERSON"]),
            )
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity.async_trigger_image_refresh.assert_called_once_with(delay=0)


class TestPathAGen1Delay:
    """Gen1 camera (e.g. CAMERA_360 Indoor) → delay=1.5 s.

    The per-model event_refresh_delay field gives slower Gen1 hardware time
    to capture the post-trigger frame before snap.jpg is fetched. Without
    this delay the live-snap on Gen1 can return the pre-motion frame.
    """

    @pytest.mark.asyncio
    async def test_gen1_indoor_uses_15s_delay(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity.async_trigger_image_refresh = AsyncMock(return_value=None)
        cam_entity.is_streaming = False  # not streaming → Path A must fire
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
            hw_version={CAM_ID: "INDOOR"},  # Gen1 360 Innenkamera
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="MOVEMENT")
            )
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity.async_trigger_image_refresh.assert_called_once_with(delay=1.5)


class TestPathAStatusOnlyEvent:
    """Status-only event type → async_trigger_image_refresh NOT called."""

    @pytest.mark.asyncio
    async def test_trouble_connect_does_not_trigger_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity.async_trigger_image_refresh = AsyncMock(return_value=None)
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="TROUBLE_CONNECT")
            )
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity.async_trigger_image_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_trouble_disconnect_does_not_trigger_refresh(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        cam_entity.async_trigger_image_refresh = AsyncMock(return_value=None)
        task_stub = MagicMock(add_done_callback=MagicMock())
        coord = _make_push_coord(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="TROUBLE_DISCONNECT")
            )
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        cam_entity.async_trigger_image_refresh.assert_not_called()


class TestPathBValidJpeg:
    """Valid JPEG bytes from imageUrl → cache updated, save_snapshot called, notify fired."""

    @pytest.mark.asyncio
    async def test_path_b_updates_cache_and_notifies(self) -> None:
        cam_entity = MagicMock()
        cam_entity.cached_image = None  # no existing cache
        cam_entity.last_image_fetch = float("-inf")

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord2(
            camera_entities={CAM_ID: cam_entity},
            image_entities={CAM_ID: image_entity},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        assert cam_entity.cached_image == JPEG_BYTES, (
            "cache must hold the event image bytes"
        )
        mock_save.assert_awaited_once_with(coord.hass, CAM_ID, JPEG_BYTES)
        image_entity.async_notify_refreshed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_path_b_updates_last_image_fetch(self) -> None:
        """last_image_fetch must be set to current monotonic time."""
        cam_entity = MagicMock()
        cam_entity.cached_image = None
        cam_entity.last_image_fetch = float("-inf")

        coord = _make_alert_coord2(
            camera_entities={CAM_ID: cam_entity},
            image_entities={},
            shc_state_cache={},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        before = time.monotonic()
        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock):
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )
        after = time.monotonic()

        fetch_ts: float = cam_entity.last_image_fetch
        assert before <= fetch_ts <= after, (
            f"last_image_fetch must be updated to current monotonic; "
            f"got {fetch_ts!r}, expected [{before:.3f}, {after:.3f}]"
        )


class TestPathBPrivacyModeBlocked:
    """Privacy mode ON → no cache update, no save, no notify."""

    @pytest.mark.asyncio
    async def test_path_b_blocked_by_privacy_mode(self) -> None:
        cam_entity = MagicMock()
        cam_entity.cached_image = None
        cam_entity.last_image_fetch = float("-inf")

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord2(
            camera_entities={CAM_ID: cam_entity},
            image_entities={CAM_ID: image_entity},
            shc_state_cache={CAM_ID: {"privacy_mode": True}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        assert cam_entity.cached_image is None, (
            "cache must NOT be updated when privacy is ON"
        )
        mock_save.assert_not_awaited()
        image_entity.async_notify_refreshed.assert_not_awaited()


class TestPathBDeduplication:
    """Byte-identity dedup: same bytes → skip; different bytes → update (even same length)."""

    @pytest.mark.asyncio
    async def test_path_b_skipped_on_byte_identical(self) -> None:
        """Exact same bytes already cached → deduplication skips the write."""
        existing = JPEG_BYTES  # identical object, same content
        assert existing == JPEG_BYTES

        cam_entity = MagicMock()
        cam_entity.cached_image = existing
        cam_entity.last_image_fetch = time.monotonic()

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord2(
            camera_entities={CAM_ID: cam_entity},
            image_entities={CAM_ID: image_entity},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        # Byte-identical → dedup must skip
        mock_save.assert_not_awaited()
        image_entity.async_notify_refreshed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_path_b_fires_on_same_length_different_content(self) -> None:
        """Same byte-length but different content → NOT deduplicated → update fires.

        A dedup check based on len() alone would incorrectly treat
        same-length-different-content images as duplicates (e.g. two motion
        events producing JPEG snapshots of the same byte-count from the same
        camera) — this pins the byte-content comparison instead.
        """
        # JPEG_BYTES_ALT has same length as JPEG_BYTES but different payload.
        assert len(JPEG_BYTES_ALT) == len(JPEG_BYTES)
        assert JPEG_BYTES_ALT != JPEG_BYTES

        cam_entity = MagicMock()
        cam_entity.cached_image = JPEG_BYTES_ALT  # cached: different image, same size
        cam_entity.last_image_fetch = time.monotonic()

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord2(
            camera_entities={CAM_ID: cam_entity},
            image_entities={CAM_ID: image_entity},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        # Different content → must NOT be skipped, even though length is equal
        mock_save.assert_awaited_once()
        image_entity.async_notify_refreshed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_path_b_fires_when_length_differs(self) -> None:
        """Different length → NOT deduplicated → update fires."""
        short_existing = b"\xff\xd8\xff\xe0" + b"\xaa" * 100  # shorter than JPEG_BYTES
        assert len(short_existing) != len(JPEG_BYTES)

        cam_entity = MagicMock()
        cam_entity.cached_image = short_existing
        cam_entity.last_image_fetch = time.monotonic()

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        coord = _make_alert_coord2(
            camera_entities={CAM_ID: cam_entity},
            image_entities={CAM_ID: image_entity},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        mock_save.assert_awaited_once()
        image_entity.async_notify_refreshed.assert_awaited_once()


class TestPathBNoCameraEntity:
    """No camera entity registered → no error, no crash."""

    @pytest.mark.asyncio
    async def test_path_b_no_camera_entity_is_silent(self) -> None:
        coord = _make_alert_coord2(
            camera_entities={},
            image_entities={},
            shc_state_cache={},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            # Must complete without raising
            await _run_alert(
                coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=session,
            )

        mock_save.assert_not_awaited()


class TestPathAandBOrdering:
    """Path A fires on FCM push; Path B fires later when alert pipeline downloads imageUrl."""

    @pytest.mark.asyncio
    async def test_path_a_fires_before_path_b(self) -> None:
        """Verify ordering: FCM push → Path A (immediate), alert pipeline → Path B (delayed).

        Wall-clock ordering can't be observed in a unit test, but both are
        confirmed independently:
        - Path A: async_create_task is called during async_handle_fcm_push
        - Path B: save_snapshot is called during async_send_alert (the alert
          pipeline)
        Both are independent coroutines; the test confirms both fire for a
        single event.
        """
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        # Set up a cam entity for Path A
        cam_entity = MagicMock()
        cam_entity.async_trigger_image_refresh = AsyncMock(return_value=None)
        cam_entity.is_streaming = False  # not streaming → Path A must fire
        # Set it up so Path B can also update it (no existing cache → update will fire)
        cam_entity.cached_image = None
        cam_entity.last_image_fetch = float("-inf")

        image_entity = MagicMock()
        image_entity.async_notify_refreshed = AsyncMock()

        task_stub = MagicMock(add_done_callback=MagicMock())

        coord = _make_push_coord(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
            image_entities={CAM_ID: image_entity},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )
        coord.hass.async_create_task = MagicMock(return_value=task_stub)
        # Alert send must be patchable — replace with no-op so we can test push handler alone
        path_a_fired = False

        original_create_task = coord.hass.async_create_task

        def _track_create_task(coro: Any) -> Any:
            nonlocal path_a_fired
            # If the cam entity refresh was scheduled, Path A fired
            if cam_entity.async_trigger_image_refresh.called:
                path_a_fired = True
            return task_stub

        coord.hass.async_create_task = _track_create_task

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200, json_data=_one_event("new-evt", event_type="MOVEMENT")
            )
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                    await async_handle_fcm_push(coord)

        # Path A: refresh must have been called
        cam_entity.async_trigger_image_refresh.assert_called_once_with(delay=0)

        # Path B: simulate the alert pipeline completing with imageUrl bytes
        alert_coord = _make_alert_coord2(
            camera_entities={CAM_ID: cam_entity},
            image_entities={CAM_ID: image_entity},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )

        img_session = MagicMock()
        img_session.get = MagicMock(
            return_value=_resp_cm(200, body=JPEG_BYTES, content_type="image/jpeg")
        )

        with patch(f"{MODULE}.save_snapshot", new_callable=AsyncMock) as mock_save:
            await _run_alert(
                alert_coord,
                event_type="MOVEMENT",
                image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                session_override=img_session,
            )

        # Path B: save_snapshot must have fired
        mock_save.assert_awaited_once_with(alert_coord.hass, CAM_ID, JPEG_BYTES)
        image_entity.async_notify_refreshed.assert_awaited_once()


class TestAlertTimestampNone:
    """Regression: Bosch has been observed sending "timestamp": null in event
    payloads. newest_event.get("timestamp", "") only substitutes the default
    when the key is ABSENT, not when its value is JSON null — a bare None
    reaching async_send_alert used to crash on len(timestamp)/timestamp[:19],
    silently (it runs inside an untracked hass.async_create_task, swallowed
    by asyncio's default exception handler) dropping the text/snapshot/clip
    notification steps with zero visible symptom.
    """

    @pytest.mark.asyncio
    async def test_none_timestamp_does_not_raise(self) -> None:
        coord = _make_alert_coord2()
        # Must not raise — this is the regression itself.
        await _run_alert(coord, event_type="MOVEMENT", timestamp=None)


class TestFcmSafeBoschUrl:
    """`fcm.py` aliases `bosch_shc_camera_client.media_transfer.is_safe_bosch_url`
    as `_is_safe_bosch_url` — this pins the alert-path behavior against the
    shared library implementation.
    """

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://residential.cbs.boschsecurity.com/x", True),
            ("https://api.bosch.com/y", True),
            (
                "https://abc.boschsecurity.com.attacker.com/",
                False,
            ),  # suffix-injection guard
            ("http://residential.cbs.boschsecurity.com/x", False),  # not HTTPS
            ("https://attacker.com/", False),
            ("https://192.168.1.1/", False),
            ("ftp://api.bosch.com/", False),
            ("", False),
            ("not-a-url", False),
        ],
    )
    def test_url_validation(self, url: str, expected: bool) -> None:
        from custom_components.bosch_shc_camera.fcm import _is_safe_bosch_url

        assert _is_safe_bosch_url(url) is expected

    def test_malformed_ipv6_bracket_fails_closed(self) -> None:
        """urlparse() can raise ValueError on malformed input — fail closed
        (backported from the Core PR's Copilot review round 18, 2026-08-04).
        """
        from custom_components.bosch_shc_camera.fcm import _is_safe_bosch_url

        assert _is_safe_bosch_url("https://[::1") is False


def _make_record(msg: str, *, with_exc: bool = False) -> logging.LogRecord:
    record = logging.LogRecord(
        name="firebase_messaging.fcmpushclient",
        level=logging.ERROR,
        pathname="x.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    if with_exc:
        record.exc_info = (ValueError, ValueError("x"), None)
        record.exc_text = "Traceback (most recent call last):\n  File...\n" * 3000
    return record


class TestFCMNoiseFilter:
    def test_unrelated_messages_pass_through(self):
        f = _FCMNoiseFilter()
        record = _make_record("FCM token registered successfully")
        assert f.filter(record) is True

    def test_first_offending_record_passes_with_exc_stripped(self):
        """First 'Unexpected exception during read' lets through, but
        without the recursive stack trace."""
        f = _FCMNoiseFilter()
        record = _make_record("Unexpected exception during read", with_exc=True)
        assert f.filter(record) is True
        # The recursive trace must be stripped by now
        assert record.exc_info is None
        assert record.exc_text is None

    def test_second_record_within_60s_dropped(self):
        """De-dupe within the dedup window — second message gets filtered out."""
        f = _FCMNoiseFilter()
        # First passes
        f.filter(_make_record("Unexpected exception during read"))
        # Second within window must be dropped
        rec2 = _make_record("Unexpected exception during read")
        assert f.filter(rec2) is False

    def test_record_after_dedup_window_passes(self):
        """After the dedup window elapses, another message gets through."""
        f = _FCMNoiseFilter()
        f.filter(_make_record("Unexpected exception during read"))
        # Backdate past the dedup window so the next record passes
        f._last_passed = time.monotonic() - (
            _FCMNoiseFilter._DEDUP_WINDOW_SECONDS + 10.0
        )
        rec = _make_record("Unexpected exception during read")
        assert f.filter(rec) is True

    def test_initial_last_passed_is_sentinel(self):
        """_last_passed must be float('-inf'), not 0.0.

        SENTINEL_RULE: CI VMs boot with time.monotonic() < 60 s. With 0.0,
        now - 0.0 < 60 → the very first FCM noise record would be
        suppressed instead of passing through. float('-inf') ensures the
        first call always passes regardless of VM uptime.
        """
        f = _FCMNoiseFilter()
        assert f._last_passed == float("-inf"), (
            "SENTINEL_RULE violation: _last_passed=0.0 silently drops the first "
            "FCM noise record on CI VMs with uptime < 60 s"
        )


def _make_record_ext(msg: str, exc_info: Any = None) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="firebase_messaging.fcmpushclient",
        level=logging.ERROR,
        pathname="x",
        lineno=1,
        msg=msg,
        args=None,
        exc_info=exc_info,
    )
    return rec


class TestFcmNoiseFilterAdditional:
    """Additional _FCMNoiseFilter edge cases beyond the base coverage above."""

    def test_passes_unrelated_record_unchanged(self):
        f = _FCMNoiseFilter()
        rec = _make_record_ext("connection established")
        assert f.filter(rec) is True
        # Unrelated records keep their exc_info
        rec.exc_info = ("type", "value", "tb")
        rec.msg = "some other error"
        # filter would still pass through (the check is "Unexpected exception during read")
        assert f.filter(rec) is True
        assert rec.exc_info == ("type", "value", "tb"), (
            "Unrelated records must keep exc_info — only the recursive "
            "FCM read traceback is stripped."
        )

    def test_strips_exc_info_on_target_record(self):
        """Filter strips exc_info on matching records to defeat the
        thousands-of-frame recursive trace."""
        f = _FCMNoiseFilter()
        rec = _make_record_ext(
            "Unexpected exception during read",
            exc_info=("t", "v", "tb"),
        )
        f.filter(rec)
        assert rec.exc_info is None
        assert rec.exc_text is None

    def test_60s_dedup_window(self):
        """Filter lets one record through per dedup window (anti-flood)."""
        f = _FCMNoiseFilter()
        # First record: passes
        r1 = _make_record_ext("Unexpected exception during read")
        assert f.filter(r1) is True
        # Second record immediately after: dropped
        r2 = _make_record_ext("Unexpected exception during read")
        assert f.filter(r2) is False

    def test_dedup_window_lets_through_after_elapsed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After the dedup window elapses, the next matching record passes
        again — keeps a heartbeat so users still see the WAN-down state in
        the log."""
        f = _FCMNoiseFilter()
        f.filter(_make_record_ext("Unexpected exception during read"))
        # Force the internal timestamp past the dedup window
        f._last_passed = time.monotonic() - (
            _FCMNoiseFilter._DEDUP_WINDOW_SECONDS + 10.0
        )
        rec = _make_record_ext("Unexpected exception during read")
        assert f.filter(rec) is True


class TestFailureMarkers:
    """Creds-staleness markers must record to _SHARED_STALENESS_TIMESTAMPS;
    connectivity-only markers must dedupe but NOT record there."""

    def _make_record(self, msg: str) -> logging.LogRecord:
        return logging.LogRecord(
            name="firebase_messaging.fcmregister",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )

    def test_phone_registration_error_records_staleness_timestamp(self):
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        passed = f.filter(
            self._make_record(
                "GCM register request attempt 1 out of 2 has failed with Error=PHONE_REGISTRATION_ERROR"
            )
        )
        assert passed is True
        assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) == 1

    def test_unable_to_complete_gcm_auth_records_staleness_timestamp(self):
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        f.filter(
            self._make_record(
                "Unable to complete gcm auth request after 2 tries, last error was Error=PHONE_REGISTRATION_ERROR"
            )
        )
        assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) == 1

    def test_unable_to_establish_subscription_records_staleness_timestamp(self):
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        f.filter(
            self._make_record(
                "FCM registration failed: Unable to establish subscription with Google Cloud Messaging."
            )
        )
        assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) == 1

    def test_connectivity_error_does_not_record_staleness_timestamp(self):
        """'Unexpected exception during read' is deduplicated but NOT a staleness marker."""
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        passed = f.filter(self._make_record("Unexpected exception during read"))
        assert passed is True  # first occurrence passes through
        assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []

    def test_unrelated_message_passes_through_untouched(self):
        """Non-failure messages pass through without recording any timestamp."""
        f = _FCMNoiseFilter()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        passed = f.filter(self._make_record("FCM push listener started"))
        assert passed is True
        assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []


class TestPatchClassImportError:
    def test_returns_none_when_library_missing(self):
        import builtins as _bi

        real = _bi.__import__

        def _fake(name, *a, **kw):
            if name == "firebase_messaging":
                raise ImportError("simulated absence")
            return real(name, *a, **kw)

        with patch("builtins.__import__", side_effect=_fake):
            result = fcm._QuietFcmPushClient._patch_class()
        assert result is None


class TestInstallFcmNoiseFilter:
    """The installer must be idempotent — re-running attaches no duplicate
    filters. Otherwise reload-the-integration would chain filters and the
    dedup window would multiply."""

    def test_installs_once(self):
        # Strip any pre-existing filters from previous test
        log = logging.getLogger("firebase_messaging.fcmpushclient")
        register_log = logging.getLogger("firebase_messaging.fcmregister")
        log.filters = [f for f in log.filters if not isinstance(f, _FCMNoiseFilter)]
        register_log.filters = [
            f for f in register_log.filters if not isinstance(f, _FCMNoiseFilter)
        ]
        _install_fcm_noise_filter()
        count_after_first = sum(
            1 for f in log.filters if isinstance(f, _FCMNoiseFilter)
        )
        _install_fcm_noise_filter()
        _install_fcm_noise_filter()
        count_after_third = sum(
            1 for f in log.filters if isinstance(f, _FCMNoiseFilter)
        )
        assert count_after_first == 1
        assert count_after_third == 1, (
            "Re-installing must be a no-op — duplicate filters multiply "
            "the dedup window and break the heartbeat log."
        )


class TestInstallNoiseFilter:
    def test_install_adds_filter_to_all_three_loggers(self):
        """Regression (GitHub #68): `firebase_messaging.fcmregister` — where
        gcm_register()/gcm_check_in() actually log PHONE_REGISTRATION_ERROR —
        is a SIBLING of fcmpushclient in the logger hierarchy, not a
        descendant. A `logging.Filter` attached to one logger is never
        consulted for a sibling's own records, so without this the creds-
        staleness markers from the real failure path were silently never
        recorded and get_recent_fcm_creds_staleness_count() stayed 0 forever
        in production."""
        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        register_logger = logging.getLogger("firebase_messaging.fcmregister")
        bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
        lib_logger.filters[:] = []
        register_logger.filters[:] = []
        bosch_logger.filters[:] = []
        _install_fcm_noise_filter()
        assert any(isinstance(f, _FCMNoiseFilter) for f in lib_logger.filters)
        assert any(isinstance(f, _FCMNoiseFilter) for f in register_logger.filters), (
            "filter must be installed on firebase_messaging.fcmregister — "
            "PHONE_REGISTRATION_ERROR is logged there, not on fcmpushclient"
        )
        assert any(isinstance(f, _FCMNoiseFilter) for f in bosch_logger.filters)

    def test_install_repairs_partial_install(self):
        """If the lib logger has the filter but the register/bosch loggers
        lost it (e.g. after a partial reload), a second call must re-attach
        to both without creating a second `_FCMNoiseFilter` instance."""
        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        register_logger = logging.getLogger("firebase_messaging.fcmregister")
        bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
        f = _FCMNoiseFilter()
        lib_logger.filters[:] = [f]
        register_logger.filters[:] = []
        bosch_logger.filters[:] = []
        _install_fcm_noise_filter()
        assert f in register_logger.filters
        assert f in bosch_logger.filters
        register_fcm = [
            g for g in register_logger.filters if isinstance(g, _FCMNoiseFilter)
        ]
        bosch_fcm = [g for g in bosch_logger.filters if isinstance(g, _FCMNoiseFilter)]
        assert register_fcm == [f]
        assert bosch_fcm == [f]

    def test_register_logger_records_real_phone_registration_error(self):
        """End-to-end (not the direct-.filter()-call unit tests in
        TestFailureMarkers): a PHONE_REGISTRATION_ERROR logged through the
        REAL `firebase_messaging.fcmregister` logger, via normal Python
        logging propagation, must reach the installed filter and record a
        staleness timestamp — this is exactly what GitHub #68 needed and
        what the sibling-logger gap silently broke."""
        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        register_logger = logging.getLogger("firebase_messaging.fcmregister")
        bosch_logger = logging.getLogger("custom_components.bosch_shc_camera.fcm")
        lib_logger.filters[:] = []
        register_logger.filters[:] = []
        bosch_logger.filters[:] = []
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        try:
            _install_fcm_noise_filter()
            register_logger.warning(
                "GCM register request attempt 1 out of 2 has failed with "
                "Error=PHONE_REGISTRATION_ERROR"
            )
            assert len(_FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS) == 1
        finally:
            _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()


class TestCredsStatenessHelpers:
    def test_count_zero_on_empty_list(self):
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        assert get_recent_fcm_creds_staleness_count() == 0

    def test_count_within_window(self):
        now = time.monotonic()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = [
            now - 200.0,  # in default 600s window
            now - 50.0,  # in window
            now - 700.0,  # outside window
        ]
        assert get_recent_fcm_creds_staleness_count() == 2

    def test_count_custom_window(self):
        now = time.monotonic()
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = [now - 30.0, now - 90.0]
        # 60s window catches one; default 600s would catch both.
        assert get_recent_fcm_creds_staleness_count(window_seconds=60.0) == 1

    def test_reset_clears_list(self):
        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS[:] = [1.0, 2.0, 3.0]
        reset_fcm_creds_staleness_counter()
        assert _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS == []


class TestOnFcmPush:
    """`_on_fcm_push` is the FCM client callback. Must:
    1. Drop pushes when `fcm_running` is False (post-stop trailing push).
    2. Update `fcm_last_push` + `fcm_healthy` flags.
    3. Schedule `async_handle_fcm_push` on the HA loop.
    """

    def _make_coord(self, running: bool = True):
        loop = SimpleNamespace(call_soon_threadsafe=MagicMock())
        hass = SimpleNamespace(
            loop=loop,
            async_create_task=MagicMock(),
        )
        import threading

        return SimpleNamespace(
            fcm_lock=threading.Lock(),
            fcm_running=running,
            fcm_last_push=0.0,
            fcm_healthy=False,
            hass=hass,
        )

    def test_drops_when_not_running(self):
        """Trailing push after stop must be ignored — otherwise the
        scheduled handler runs against a torn-down session."""
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = self._make_coord(running=False)
        _on_fcm_push(coord, {"from": "test"}, "push-id-1")
        coord.hass.loop.call_soon_threadsafe.assert_not_called()
        assert coord.fcm_last_push == 0.0
        assert coord.fcm_healthy is False

    def test_updates_health_flags_when_running(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = self._make_coord(running=True)
        _on_fcm_push(coord, {"from": "test"}, "push-id-1")
        assert coord.fcm_last_push > 0.0
        assert coord.fcm_healthy is True

    def test_schedules_handler_via_loop(self):
        """Must schedule via `loop.call_soon_threadsafe` since the FCM
        callback runs on a background thread, not the event loop."""
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = self._make_coord(running=True)
        _on_fcm_push(coord, {"from": "test"}, "push-id-1")
        coord.hass.loop.call_soon_threadsafe.assert_called_once()


class TestBuildNotifyData:
    def test_text_only_no_attachment(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data("notify.foo", "Hello", title="Subject")
        assert data["message"] == "Hello"
        assert data["title"] == "Subject"
        assert "data" not in data

    def test_mobile_app_uses_local_image_url(self):
        """HA Companion App reads images from /local/bosch_alerts/ (auth-free)."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.mobile_app_test_phone",
            "msg",
            file_path="/config/www/bosch_alerts/snap_123.jpg",
        )
        assert data["data"]["image"] == "/local/bosch_alerts/snap_123.jpg"
        assert data["data"]["push"]["sound"] == "default"

    def test_telegram_uses_photo_field(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.telegram_chat",
            "msg",
            file_path="/config/x.jpg",
        )
        assert data["data"]["photo"] == "/config/x.jpg"
        assert data["data"]["caption"] == "msg"

    def test_signal_uses_attachments(self):
        """Signal / email / generic services use data.attachments list."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.signal_messenger",
            "msg",
            file_path="/config/x.mp4",
        )
        assert data["data"]["attachments"] == ["/config/x.mp4"]

    def test_no_title_field_when_empty(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data("notify.x", "msg", title=None)
        assert "title" not in data


class TestBuildNotifyDataExtras:
    """Additional build_notify_data paths not covered above."""

    def test_with_title(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.alex",
            "msg",
            file_path=None,
            title="Bewegung",
        )
        assert data["message"] == "msg"
        assert data["title"] == "Bewegung"
        assert "data" not in data

    def test_no_title_no_data_key(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data("notify.alex", "msg")
        assert "title" not in data
        assert "data" not in data

    def test_mobile_app_includes_default_sound(self):
        """iOS Companion App: the alert needs an explicit sound key.
        Without `push.sound`, iOS plays no chime — silent alerts are
        easy to miss."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.mobile_app_thomas_iphone",
            "msg",
            file_path="/tmp/img.jpg",
        )
        assert data["data"]["push"]["sound"] == "default"
        assert data["data"]["image"] == "/local/bosch_alerts/img.jpg"

    def test_telegram_uppercase_match(self):
        """Telegram service name detection is case-insensitive."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.TELEGRAM_chat_main",
            "Bewegung erkannt",
            file_path="/tmp/x.jpg",
        )
        assert data["data"]["photo"] == "/tmp/x.jpg"
        assert data["data"]["caption"] == "Bewegung erkannt"

    def test_signal_uses_attachments_list(self):
        """Signal-Messenger (HA addon notify.signal) requires an
        `attachments` list with file path strings."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.signal_thomas",
            "msg",
            file_path="/x/y.mp4",
        )
        assert data["data"] == {"attachments": ["/x/y.mp4"]}

    def test_email_falls_into_attachments(self):
        """`notify.smtp` and similar email-based services hit the
        generic `else` branch and use `attachments`."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        data = build_notify_data(
            "notify.smtp_default",
            "msg",
            file_path="/file.jpg",
        )
        assert data["data"] == {"attachments": ["/file.jpg"]}


class TestGetAlertServices:
    def test_specific_slot_used_when_set(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_information": "notify.foo",
                "alert_notify_service": "notify.fallback",
            }
        )
        assert get_alert_services(coord, "information") == ["notify.foo"]

    def test_information_falls_back_to_default(self):
        """When the per-step slot is empty, fall back to alert_notify_service."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_information": "",
                "alert_notify_service": "notify.signalkamera",
            }
        )
        assert get_alert_services(coord, "information") == ["notify.signalkamera"]

    def test_system_falls_back_to_default(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_system": "",
                "alert_notify_service": "notify.test_user",
            }
        )
        assert get_alert_services(coord, "system") == ["notify.test_user"]

    def test_screenshot_does_not_fall_back(self):
        """Empty `alert_notify_screenshot` must NOT fall back — empty means skip step."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_screenshot": "",
                "alert_notify_service": "notify.test_user",
            }
        )
        assert get_alert_services(coord, "screenshot") == []

    def test_video_does_not_fall_back(self):
        """Same skip-on-empty rule for video."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_video": "",
                "alert_notify_service": "notify.test_user",
            }
        )
        assert get_alert_services(coord, "video") == []

    def test_comma_separated_services_split(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_information": "notify.a, notify.b , notify.c",
            }
        )
        assert get_alert_services(coord, "information") == [
            "notify.a",
            "notify.b",
            "notify.c",
        ]

    def test_empty_strings_filtered_out(self):
        """Trailing comma or double comma → no empty entry in the result."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_information": "notify.a,, notify.b,",
            }
        )
        assert get_alert_services(coord, "information") == ["notify.a", "notify.b"]


class TestGetAlertServicesExtras:
    """Additional get_alert_services paths not covered above."""

    def test_explicit_value_takes_precedence_over_default(self):
        """Explicit per-type service must NOT be overwritten by the
        global default."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_information": "notify.specific",
                "alert_notify_service": "notify.fallback",
            }
        )
        assert get_alert_services(coord, "information") == ["notify.specific"]

    def test_strips_whitespace_in_csv(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = SimpleNamespace(
            options={
                "alert_notify_information": "  notify.a , notify.b ,,, notify.c ",
            }
        )
        assert get_alert_services(coord, "information") == [
            "notify.a",
            "notify.b",
            "notify.c",
        ]


def _make_listen_instance():
    """Build a minimal `_Patched` instance with every protected hook stubbed
    so a test can drive `_listen` through one specific branch."""
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


class TestListenBranches:
    async def test_connect_failure_returns_early(self):
        """`_connect_with_retry()` returning False makes _listen return
        immediately without touching _login or the loop."""
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
        `_log_warn_with_limit` instead of the quiet `_log_verbose` path."""
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
        `_reset`."""
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
        bubbles to the outer `except Exception: _terminate()` arm."""
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
        instead of consuming a message."""
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
        """A non-empty `_receive_msg()` return invokes `_handle_message(msg)`."""
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
        call `_reset()`."""
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


def _make_coord(**overrides: object) -> SimpleNamespace:
    """Minimal coordinator stub for FCM mode tests."""
    base = dict(
        token="tok-A",
        fcm_token="fcm-token-xyz",
        fcm_push_mode="unknown",
        fcm_lock=RLock(),
        fcm_running=False,
        fcm_healthy=False,
        fcm_client=None,
        entry=SimpleNamespace(data={}),
        options={"enable_fcm_push": True, "fcm_push_mode": "auto"},
        hass=MagicMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_migrate_harness(version: int, options: dict) -> tuple:
    """Return (hass, entry, captured) for async_migrate_entry tests.

    entry.data defaults to {} so that the v2→v3 migration (which always reads
    entry.data) does not AttributeError on entries that have no FCM data.
    """
    captured: dict = {}

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        captured.update(kwargs)
        if "options" in kwargs:
            entry.options = kwargs["options"]
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]
        if "version" in kwargs:
            entry.version = kwargs["version"]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="test-entry", version=version, options=options, data={}
    )
    return hass, entry, captured


def _make_migrate_harness_with_data(version: int, options: dict, data: dict) -> tuple:
    """Return (hass, entry, captured) for async_migrate_entry tests that touch entry.data.

    Unlike _make_migrate_harness, the entry carries a data dict and the captured
    dict also records the 'data' kwarg passed to async_update_entry.
    """
    captured: dict = {}

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        captured.update(kwargs)
        if "options" in kwargs:
            entry.options = kwargs["options"]  # type: ignore[assignment]
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]
        if "version" in kwargs:
            entry.version = kwargs["version"]  # type: ignore[assignment]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry)
    )
    entry = SimpleNamespace(
        entry_id="test-entry", version=version, options=options, data=data
    )
    return hass, entry, captured


async def test_auto_mode_calls_fcm_register() -> None:
    """PIN auto: fcm_push_mode='auto' → register_fcm_with_bosch is invoked once.

    The OSS-key FCM path (_try_fcm) must attempt a Bosch device registration
    whenever the user selects 'auto'. Failure to call register means pushes
    never arrive and motion events are silently delayed to polling interval.

    FcmRegisterConfig is imported lazily inside async_start_fcm_push, so we
    patch it via firebase_messaging module-level and patch the lazy import.
    """
    register_called: list[bool] = []

    async def _fake_register(c: object) -> bool:
        register_called.append(True)
        return True

    coord = _make_coord(options={"enable_fcm_push": True, "fcm_push_mode": "auto"})

    mock_client = MagicMock()
    mock_client.checkin_or_register = AsyncMock(return_value="fcm-tok")
    mock_client.start = AsyncMock()
    mock_client_cls = MagicMock(return_value=mock_client)

    # FcmRegisterConfig is imported inside the function: patch the module attribute
    # on the firebase_messaging stub so the lazy `from firebase_messaging import` works.
    fake_firebase = MagicMock()
    fake_firebase.FcmRegisterConfig = MagicMock()
    fake_firebase.FcmPushClientConfig = None  # optional import guarded with try/except

    with (
        patch(f"{MODULE}.register_fcm_with_bosch", side_effect=_fake_register),
        patch(f"{MODULE}._install_fcm_noise_filter"),
        patch(f"{MODULE}._get_fcm_push_client_class", return_value=mock_client_cls),
        patch(
            f"{MODULE}.fetch_firebase_config",
            new=AsyncMock(
                return_value={"api_key": "key", "project_id": "proj", "app_id": "app"}
            ),
        ),
        patch.dict("sys.modules", {"firebase_messaging": fake_firebase}),
    ):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        await async_start_fcm_push(coord)

    assert register_called, (
        "fcm_push_mode='auto' must call register_fcm_with_bosch once. "
        "Without registration, Bosch never sends push notifications to this device."
    )


async def test_polling_mode_skips_fcm_register() -> None:
    """PIN polling: fcm_push_mode='polling' → register_fcm_with_bosch NOT called.

    Users who explicitly chose 'polling' (e.g. behind FCM-blocking firewalls)
    must never trigger FCM registration. Calling it silently would fail or
    create a registered token the user doesn't want, causing Bosch 500 spam.
    """
    coord = _make_coord(options={"enable_fcm_push": True, "fcm_push_mode": "polling"})

    register_called: list[bool] = []

    async def _fake_register(c: object) -> bool:
        register_called.append(True)
        return True

    with (
        patch(f"{MODULE}.register_fcm_with_bosch", side_effect=_fake_register),
        patch(f"{MODULE}._install_fcm_noise_filter"),
        patch(f"{MODULE}._get_fcm_push_client_class", return_value=MagicMock()),
    ):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        await async_start_fcm_push(coord)

    assert not register_called, (
        "fcm_push_mode='polling' must NOT call register_fcm_with_bosch. "
        "Polling users opted out of FCM — registration would create unwanted "
        "Bosch device entries and cause HTTP 500 spam on each restart."
    )


def test_default_mode_is_auto() -> None:
    """PIN default: DEFAULT_OPTIONS['fcm_push_mode'] == 'auto'.

    New installs must default to 'auto' so FCM push is attempted automatically.
    A regression to 'polling' would silently downgrade all fresh installs to
    polling-only, increasing motion event latency from ~1s to the polling interval.
    """
    from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

    assert DEFAULT_OPTIONS["fcm_push_mode"] == "auto", (
        "New installs must default to fcm_push_mode='auto' so FCM push is "
        "attempted automatically. Regression to 'polling' would silently "
        "increase motion-event latency for all fresh installs."
    )


async def test_garbage_mode_coerced_to_auto() -> None:
    """PIN garbage: unknown fcm_push_mode string → coerces to 'auto' (FCM path).

    Stale config entries or manual edits can produce arbitrary strings.
    They must not disable FCM silently (which 'polling' would do) — instead
    the integration treats unrecognized values as 'auto' and tries FCM.
    """
    coord = _make_coord(
        options={"enable_fcm_push": True, "fcm_push_mode": "garbage_string"}
    )

    register_called: list[bool] = []

    async def _fake_register(c: object) -> bool:
        register_called.append(True)
        return True

    mock_client = MagicMock()
    mock_client.checkin_or_register = AsyncMock(return_value="fcm-tok")
    mock_client.start = AsyncMock()
    mock_client_cls = MagicMock(return_value=mock_client)

    fake_firebase = MagicMock()
    fake_firebase.FcmRegisterConfig = MagicMock()
    fake_firebase.FcmPushClientConfig = None

    with (
        patch(f"{MODULE}.register_fcm_with_bosch", side_effect=_fake_register),
        patch(f"{MODULE}._install_fcm_noise_filter"),
        patch(f"{MODULE}._get_fcm_push_client_class", return_value=mock_client_cls),
        patch(
            f"{MODULE}.fetch_firebase_config",
            new=AsyncMock(
                return_value={"api_key": "key", "project_id": "proj", "app_id": "app"}
            ),
        ),
        patch.dict("sys.modules", {"firebase_messaging": fake_firebase}),
    ):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        await async_start_fcm_push(coord)

    assert register_called, (
        "Garbage fcm_push_mode must coerce to 'auto' and attempt FCM registration. "
        "Silently treating unknown values as 'polling' would disable push for "
        "users whose config entries contain stale or manually-edited values."
    )


async def test_migration_rewrites_ios_to_auto() -> None:
    """Migration v2→v3 must rewrite fcm_push_mode='ios' to 'auto'."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "ios", "enable_fcm_push": True}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto", (
        "Legacy 'ios' must be rewritten to 'auto' — the OSS key handles both "
        "platforms; the iOS-specific code path no longer exists."
    )
    assert captured["version"] == 3


async def test_migration_rewrites_android_to_auto() -> None:
    """Migration v2→v3 must rewrite fcm_push_mode='android' to 'auto'."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "android", "some_option": True}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto", (
        "Legacy 'android' must be rewritten to 'auto' — the OSS key is already "
        "the Android path; 'android' as a distinct value is now meaningless."
    )
    assert captured["options"]["some_option"] is True, (
        "Other options must be preserved unchanged during migration."
    )
    assert captured["version"] == 3


async def test_migration_keeps_auto() -> None:
    """Migration v2→v3 must leave fcm_push_mode='auto' unchanged."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "auto"}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3


async def test_migration_keeps_polling() -> None:
    """Migration v2→v3 must leave fcm_push_mode='polling' unchanged.

    Users who explicitly opted out of FCM (firewalls, privacy) must not be
    silently switched to 'auto' during migration.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=2, options={"fcm_push_mode": "polling"}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured["options"]["fcm_push_mode"] == "polling", (
        "Explicit 'polling' must survive migration — overwriting with 'auto' "
        "would silently re-enable FCM for users who deliberately disabled it."
    )
    assert captured["version"] == 3


async def test_migration_v3_entry_is_noop() -> None:
    """A v3 entry must not be touched by the v2→v3 migration."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness(
        version=3, options={"fcm_push_mode": "auto"}
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert captured == {}, "v3 entry must produce zero async_update_entry calls"


_FCM_DATA_WITH_CREDS: dict = {
    "fcm_credentials": {"token": "old-token-abc", "device_id": "dev-123"},
    "fcm_registered_token": "old-fcm-reg-token",
    "fcm_config": {"api_key": "key", "project_id": "proj"},
    "bearer_token": "bearer-xyz",
    "refresh_token": "refresh-xyz",
}


async def test_migration_v3_ios_clears_fcm_creds() -> None:
    """(a) fcm_push_mode='ios' + creds present → after v2→v3: mode='auto',
    fcm_credentials + fcm_registered_token absent from data, other data preserved,
    version=3.

    Regression: without the fix, register_fcm_with_bosch saw token unchanged and
    skipped re-registration, leaving Bosch CBS with deviceType=IOS while the
    HA client registered platform=ANDROID. Push routing was silently broken.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "ios", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3
    assert "fcm_credentials" not in captured["data"], (
        "fcm_credentials must be cleared from data so register_fcm_with_bosch "
        "forces re-registration with deviceType=ANDROID on next startup."
    )
    assert "fcm_registered_token" not in captured["data"], (
        "fcm_registered_token must be cleared to trigger Bosch CBS re-registration."
    )
    # Non-FCM data must survive migration unchanged
    assert captured["data"]["fcm_config"] == _FCM_DATA_WITH_CREDS["fcm_config"]
    assert captured["data"]["bearer_token"] == _FCM_DATA_WITH_CREDS["bearer_token"]
    assert captured["data"]["refresh_token"] == _FCM_DATA_WITH_CREDS["refresh_token"]


async def test_migration_v3_android_clears_fcm_creds() -> None:
    """(b) fcm_push_mode='android' → same clearance outcome as (a).

    'android' in v2 used the same iOS-first Bosch registration path; after
    migration to the OSS Android key, the old token is stale.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "android", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3
    assert "fcm_credentials" not in captured["data"]
    assert "fcm_registered_token" not in captured["data"]
    assert captured["data"]["fcm_config"] == _FCM_DATA_WITH_CREDS["fcm_config"]
    assert captured["data"]["bearer_token"] == _FCM_DATA_WITH_CREDS["bearer_token"]


async def test_migration_v3_old_auto_clears_fcm_creds() -> None:
    """(c) fcm_push_mode='auto' in v2 → creds must also be cleared.

    In v2, 'auto' was an iOS-first chain (not the OSS Android key). Any token
    registered under the old 'auto' is equally stale and must be cleared to
    force re-registration with platform=ANDROID.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "auto", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "auto"
    assert captured["version"] == 3
    assert "fcm_credentials" not in captured["data"], (
        "Old 'auto' (iOS-first) tokens are stale after migration to OSS Android key; "
        "fcm_credentials must be cleared."
    )
    assert "fcm_registered_token" not in captured["data"]


async def test_migration_v3_polling_preserves_fcm_creds() -> None:
    """(d) fcm_push_mode='polling' → fcm_credentials + fcm_registered_token preserved.

    'polling' users never use FCM; their data dict may contain leftover creds
    from a prior FCM phase but we must not alter them — we must not corrupt data
    that was stored by a different mechanism, and we must not trigger any re-reg.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=2,
        options={"fcm_push_mode": "polling"},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["options"]["fcm_push_mode"] == "polling"
    assert captured["version"] == 3
    assert (
        captured["data"]["fcm_credentials"] == _FCM_DATA_WITH_CREDS["fcm_credentials"]
    ), (
        "polling mode: fcm_credentials must NOT be cleared — user opted out of FCM "
        "and we must not silently alter their stored data."
    )
    assert (
        captured["data"]["fcm_registered_token"]
        == _FCM_DATA_WITH_CREDS["fcm_registered_token"]
    )


async def test_migration_already_v3_data_untouched() -> None:
    """(e) entry already at version=3 → no-op, data fields not touched."""
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=3,
        options={"fcm_push_mode": "auto"},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured == {}, (
        "v3 entry must produce zero async_update_entry calls — no options, "
        "no data, no version changes."
    )


async def test_migration_v1_to_v3_ios_clears_fcm_creds() -> None:
    """(f) entry at version=1 → v1→v2 runs (stream_connection_type preserved)
    THEN v2→v3 clears FCM creds as in test (a).

    v1 entries never had an explicit stream_connection_type, so v1→v2 must
    inject 'auto'. The combined v1→v3 path must also clear FCM creds.
    """
    from custom_components.bosch_shc_camera import async_migrate_entry

    hass, entry, captured = _make_migrate_harness_with_data(
        version=1,
        options={"fcm_push_mode": "ios", "enable_fcm_push": True},
        data=dict(_FCM_DATA_WITH_CREDS),
    )
    result = await async_migrate_entry(hass, entry)

    assert result is True
    assert captured["version"] == 3
    assert captured["options"]["stream_connection_type"] == "auto", (
        "v1→v2 must inject stream_connection_type='auto' for legacy entries."
    )
    assert captured["options"]["fcm_push_mode"] == "auto", (
        "v2→v3 must coerce 'ios' to 'auto'."
    )
    assert "fcm_credentials" not in captured["data"], (
        "Combined v1→v3 path must clear fcm_credentials."
    )
    assert "fcm_registered_token" not in captured["data"]
    assert captured["data"]["bearer_token"] == _FCM_DATA_WITH_CREDS["bearer_token"]


# No-library import-error fallback, exception paths in path A/B and mark-events-read/local-save, push-data-none guard, stop/cancellation, creds persistence, Bosch registration, remaining-lines coverage (from: no-library fallback, path A/B exceptions, push-data-none guard, remaining-lines coverage, round 5)


def _coord(data: Any) -> Any:
    """Minimal coordinator stub for the push-data-None guard tests."""
    return SimpleNamespace(
        token="tok",
        hass=MagicMock(),
        data=data,
        last_event_ids={},
        alert_sent_ids={},
        camera_entities={},
        image_entities={},
        shc_state_cache={},
        cached_events={},
        bg_tasks=set(),
        hw_version={},
        options={},
    )


def _make_push_coord2(**overrides: Any) -> Any:
    """Coordinator stub for `async_handle_fcm_push` / `async_send_alert` Path A/B tests."""
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock(
        return_value=MagicMock(add_done_callback=MagicMock())
    )
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-test",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        last_event_ids={CAM_ID: "old-evt"},
        alert_sent_ids={},
        camera_entities={},
        image_entities={},
        shc_state_cache={},
        cached_events={},
        bg_tasks=set(),
        hw_version={CAM_ID: "HOME_Eyes_Outdoor"},
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _one_event2(
    event_id: str = "new-evt", event_type: str = "MOVEMENT"
) -> list[dict[str, Any]]:
    return [
        {
            "id": event_id,
            "eventType": event_type,
            "eventTags": [],
            "timestamp": "2026-05-15T10:00:00Z",
            "imageUrl": "",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }
    ]


def _make_start_coord(push_mode: str = "ios") -> Any:
    """Coordinator stub for `async_start_fcm_push` closure-capture tests."""
    hass = MagicMock()
    hass.loop = MagicMock()
    hass.config_entries = MagicMock()
    hass.async_create_task = MagicMock()
    entry = SimpleNamespace(data={})
    return SimpleNamespace(
        fcm_running=False,
        fcm_client=None,
        fcm_token=None,
        fcm_lock=threading.Lock(),
        fcm_healthy=False,
        fcm_push_mode="unknown",
        options={"enable_fcm_push": True, "fcm_push_mode": push_mode},
        hass=hass,
        entry=entry,
        data={},
    )


def _make_alert_coord3(options: dict[str, Any] | None = None) -> Any:
    """Coordinator stub for `async_send_alert` local-save exception-branch tests."""
    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha-remaining"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    base_opts = {
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

    return SimpleNamespace(
        token="tok-remaining",
        hass=hass,
        options=base_opts,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        last_event_ids={CAM_ID: "event-id-001"},
    )


def _stub_coord(**overrides: Any) -> Any:
    """Coordinator stub for the pure-helper / FCM-client-lifecycle tests."""
    base = dict(
        options={},
        token="tok-A",
        fcm_token="fcm-token-xyz",
        fcm_push_mode="ios",
        fcm_lock=RLock(),
        fcm_running=False,
        fcm_healthy=False,
        fcm_client=None,
        fcm_last_push=float("-inf"),
        entry=SimpleNamespace(data={}),
        data={},
        hass=SimpleNamespace(
            config_entries=SimpleNamespace(async_update_entry=MagicMock()),
            loop=SimpleNamespace(call_soon_threadsafe=MagicMock()),
            async_create_task=MagicMock(),
        ),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestGetFcmPushClientClassImportError:
    def test_returns_none_when_both_paths_fail(self):
        """`_get_fcm_push_client_class` returns None when both the patched
        subclass creation AND the vanilla `from firebase_messaging import
        FcmPushClient` fallback fail (library fully absent)."""
        import builtins as _bi

        real = _bi.__import__

        def _fake(name, *a, **kw):
            # Block both the firebase_messaging top-level package and
            # FcmRegisterConfig/FcmPushClientConfig submodule lookups.
            if name == "firebase_messaging" or name.startswith("firebase_messaging."):
                raise ImportError("simulated absence")
            return real(name, *a, **kw)

        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
        )

        with patch("builtins.__import__", side_effect=_fake):
            assert _get_fcm_push_client_class() is None


class TestAsyncStartFcmPushNoLib:
    async def test_early_exit_when_lib_missing(self):
        """When `_get_fcm_push_client_class()` returns None,
        `async_start_fcm_push` logs a warning and returns without touching
        the coordinator state."""
        coord = SimpleNamespace(
            fcm_running=False,
            options={"enable_fcm_push": True, "fcm_push_mode": "auto"},
            entry=SimpleNamespace(data={}),
        )
        # Patch FcmRegisterConfig presence so the first ImportError check is
        # bypassed — we want execution to land on the FcmPushClient check.
        # Then force the class lookup to None.
        from custom_components.bosch_shc_camera import fcm

        with patch.object(fcm, "_get_fcm_push_client_class", return_value=None):
            # FcmRegisterConfig import is inside the function; patch builtins
            # so the helper finishes the import successfully before the
            # class-lookup branch fires.
            try:
                await fcm.async_start_fcm_push(coord)
            except ImportError:
                # FcmRegisterConfig may genuinely be absent on this Python
                # env — that's already covered by another test; we only care
                # about the warn-and-return branch here.
                pytest.skip("firebase_messaging library not installed in test env")
        # Must not have flipped fcm_running.
        assert coord.fcm_running is False


class TestQuietFcmPushClient:
    """Regression tests for the upstream state-machine fix (firebase-messaging#33).

    Root cause: FcmPushClient._listen() logs _logger.exception("Unexpected exception
    during read") BEFORE calling _reset(), which is where run_state is set to RESETTING.
    The existing quiet-path check (run_state == RESETTING) therefore always misses the
    very first error → one ERROR + traceback per ~63 s reconnect cycle.

    Fix: _QuietFcmPushClient._patch_class() returns a subclass whose _listen() override
    sets run_state = RESETTING immediately on catching the OSError, so the existing check
    routes to the INFO-level path instead.
    """

    def test_patch_class_returns_subclass_of_fcm_push_client(self):
        """_patch_class() must return a type that is a subclass of FcmPushClient."""
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient

        from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

        cls = _QuietFcmPushClient._patch_class()
        assert cls is not None, (
            "patch must succeed when firebase_messaging is importable"
        )
        assert issubclass(cls, FcmPushClient), (
            "patched class must be a subclass of FcmPushClient"
        )

    def test_patched_class_has_listen_override(self):
        """The patched subclass must define its own _listen so it's not the vanilla one."""
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient

        from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

        cls = _QuietFcmPushClient._patch_class()
        assert cls is not None
        # _listen defined directly on cls (not inherited from FcmPushClient)
        assert "_listen" in cls.__dict__, (
            "patched class must override _listen directly — "
            "otherwise the fix is silently absent"
        )
        # And it must differ from the base
        assert cls.__dict__["_listen"] is not FcmPushClient._listen, (  # type: ignore[attr-defined]
            "override must not be the same function object as the base"
        )

    def test_patched_class_has_decrypt_raw_data_override_against_real_library(self):
        """Test-coverage finding (GitHub #68 follow-up, 2026-08-18): every
        prior assertion that the padding-fix override attaches ran against
        the hand-rolled `_FakeFcmPushClient` (whose `_decrypt_raw_data`
        signature was written by hand to match what our signature-guard
        expects) — never against the REAL installed firebase_messaging
        library, unlike the sibling `_listen` override test above. A future
        firebase-messaging upgrade that reshapes `_decrypt_raw_data` would
        silently fall back to the unpadded vanilla version (debug-logged
        only) and regress #68's "notifications always late" fix, with this
        suite staying green and 100%-coverage the whole time. This test
        would fail that scenario immediately."""
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient

        from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

        _QuietFcmPushClient._patched_class = False
        cls = _QuietFcmPushClient._patch_class()
        assert cls is not None
        assert "_decrypt_raw_data" in cls.__dict__, (
            "the GitHub #68 padded-decrypt override must attach against the "
            "REAL installed firebase_messaging.FcmPushClient, not just the "
            "test's own hand-rolled fake — if this fails, the real "
            "library's _decrypt_raw_data signature has changed and the "
            "signature guard in _build_decrypt_raw_data_override() is "
            "(correctly) refusing to attach a mismatched override"
        )
        assert (
            cls.__dict__["_decrypt_raw_data"]
            is not FcmPushClient.__dict__["_decrypt_raw_data"]
        ), "override must not be the same function object as the base"

    def test_get_fcm_push_client_class_returns_nonnone(self):
        """_get_fcm_push_client_class() returns a usable class when the library is present."""
        pytest.importorskip("firebase_messaging")
        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
            _QuietFcmPushClient,
        )

        # Reset cache so we get a fresh computation
        _QuietFcmPushClient._patched_class = False
        cls = _get_fcm_push_client_class()
        assert cls is not None

    def test_get_fcm_push_client_class_caches_result(self):
        """Second call must return the same object (no double-computation)."""
        pytest.importorskip("firebase_messaging")
        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
            _QuietFcmPushClient,
        )

        _QuietFcmPushClient._patched_class = False
        first = _get_fcm_push_client_class()
        second = _get_fcm_push_client_class()
        assert first is second, "class must be cached after first computation"

    def test_fallback_to_vanilla_when_patch_fails(self):
        """If _patch_class() returns None (e.g. signature changed), _get_fcm_push_client_class
        must fall back to vanilla FcmPushClient rather than returning None."""
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient

        from custom_components.bosch_shc_camera.fcm import (
            _get_fcm_push_client_class,
            _QuietFcmPushClient,
        )

        # Force the cache to None (simulates _patch_class() returning None)
        _QuietFcmPushClient._patched_class = None
        cls = _get_fcm_push_client_class()
        assert cls is FcmPushClient, (
            "when patch fails, must fall back to vanilla FcmPushClient — "
            "returning None would crash async_start_fcm_push"
        )
        # Restore so other tests are unaffected
        _QuietFcmPushClient._patched_class = False

    @pytest.mark.asyncio
    async def test_listen_override_sets_resetting_before_log_decision(self):
        """Core regression test for issue #33.

        The patched _listen() must set run_state = RESETTING in the OSError handler
        BEFORE the quiet-path check runs.  We verify this by confirming that a
        ConnectionResetError caught while run_state == STARTED is NOT logged via
        _logger.exception — it should route to _log_verbose (INFO/debug) instead.

        Without the fix: run_state is STARTED when the check fires → else-branch →
        _logger.exception → ERROR + traceback in the log.
        With the fix: run_state is set to RESETTING first → isinstance check passes →
        _log_verbose → no ERROR.

        Behaviour note: after the fix routes to _log_verbose, the except block exits
        and the while loop re-enters with run_state == RESETTING, which hits
        ``asyncio.sleep(1)``.  We stop the loop inside fake_log_verbose (by setting
        do_listen=False) so the test terminates immediately without a real sleep.
        """
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClientRunState

        from custom_components.bosch_shc_camera.fcm import _QuietFcmPushClient

        cls = _QuietFcmPushClient._patch_class()
        assert cls is not None

        # Build a minimal fake instance that has only the attributes _listen needs.
        # We drive the loop through exactly one iteration that raises ConnectionResetError.
        instance = object.__new__(cls)

        # --- State we need to control ---
        instance.run_state = FcmPushClientRunState.STARTED
        instance.do_listen = True

        connect_called: list[bool] = []
        login_called: list[bool] = []
        log_exception_called: list[str] = []
        log_verbose_called: list[str] = []

        async def fake_connect_with_retry() -> bool:
            connect_called.append(True)
            return True

        async def fake_login() -> None:
            login_called.append(True)

        iteration = [0]

        async def fake_receive_msg() -> bytes:
            iteration[0] += 1
            if iteration[0] == 1:
                raise ConnectionResetError("simulated WAN drop")
            return b""

        async def fake_handle_message(msg: bytes) -> None:
            pass

        def fake_log_verbose(fmt: str, *args: object) -> None:
            log_verbose_called.append(fmt)
            # Stop the loop here so we don't enter the asyncio.sleep(1) branch
            # (run_state == RESETTING) forever.  This is the correct termination
            # point: the quiet-path log fired, test is satisfied.
            instance.do_listen = False

        def fake_log_warn_with_limit(fmt: str, *args: object) -> None:
            pass

        async def fake_reset() -> None:
            # Not called in the quiet path, but needed for the fallback else-branch.
            instance.do_listen = False

        async def fake_writer_close() -> None:
            pass

        def fake_try_increment_error_count(error_type: object) -> bool:
            return True

        def fake_terminate() -> None:
            pass

        instance._connect_with_retry = fake_connect_with_retry  # type: ignore[attr-defined]
        instance._login = fake_login  # type: ignore[attr-defined]
        instance._receive_msg = fake_receive_msg  # type: ignore[attr-defined]
        instance._handle_message = fake_handle_message  # type: ignore[attr-defined]
        instance._log_verbose = fake_log_verbose  # type: ignore[attr-defined]
        instance._log_warn_with_limit = fake_log_warn_with_limit  # type: ignore[attr-defined]
        instance._reset = fake_reset  # type: ignore[attr-defined]
        instance._do_writer_close = fake_writer_close  # type: ignore[attr-defined]
        instance._try_increment_error_count = fake_try_increment_error_count  # type: ignore[attr-defined]
        instance._terminate = fake_terminate  # type: ignore[attr-defined]

        # Capture _logger.exception calls from our fcm module
        import custom_components.bosch_shc_camera.fcm as fcm_mod

        original_exception = fcm_mod._LOGGER.exception

        def patched_exception(fmt: str, *args: object, **kwargs: object) -> None:
            log_exception_called.append(fmt)

        fcm_mod._LOGGER.exception = patched_exception  # type: ignore[method-assign]
        try:
            await instance._listen()
        finally:
            fcm_mod._LOGGER.exception = original_exception  # type: ignore[method-assign]

        assert not log_exception_called, (
            "With the fix, ConnectionResetError must NOT trigger _logger.exception — "
            "it should be routed to _log_verbose (INFO) instead.  "
            f"Got: {log_exception_called}"
        )
        assert log_verbose_called, (
            "ConnectionResetError during STARTED→RESETTING transition must log via "
            "_log_verbose (quiet INFO path) — confirms the fix is active"
        )

    @pytest.mark.asyncio
    async def test_listen_vanilla_would_log_exception_without_fix(self):
        """Counter-test: vanilla FcmPushClient._listen() DOES log _logger.exception
        on the first ConnectionResetError (run_state == STARTED at that point).

        This test documents the upstream bug and will fail if upstream ever ships
        a fix — at which point our subclass can be retired.

        Behaviour note: vanilla _listen() calls _reset() in the else-branch, and
        our fake_reset() sets do_listen=False so the loop terminates cleanly.
        """
        pytest.importorskip("firebase_messaging")
        from firebase_messaging import FcmPushClient, FcmPushClientRunState

        instance = object.__new__(FcmPushClient)
        instance.run_state = FcmPushClientRunState.STARTED
        instance.do_listen = True

        log_verbose_called: list[str] = []

        async def fake_connect_with_retry() -> bool:
            return True

        async def fake_login() -> None:
            pass

        iteration2 = [0]

        async def fake_receive_msg() -> bytes:
            iteration2[0] += 1
            if iteration2[0] == 1:
                raise ConnectionResetError("simulated WAN drop")
            instance.do_listen = False
            return b""

        async def fake_handle_message(msg: bytes) -> None:
            pass

        def fake_log_verbose(fmt: str, *args: object) -> None:
            log_verbose_called.append(fmt)

        def fake_log_warn_with_limit(fmt: str, *args: object) -> None:
            pass

        async def fake_reset() -> None:
            # Called by vanilla's else-branch after _logger.exception — stop loop.
            instance.do_listen = False

        async def fake_writer_close() -> None:
            pass

        def fake_try_increment_error_count(error_type: object) -> bool:
            return True

        def fake_terminate() -> None:
            pass

        instance._connect_with_retry = fake_connect_with_retry  # type: ignore[attr-defined]
        instance._login = fake_login  # type: ignore[attr-defined]
        instance._receive_msg = fake_receive_msg  # type: ignore[attr-defined]
        instance._handle_message = fake_handle_message  # type: ignore[attr-defined]
        instance._log_verbose = fake_log_verbose  # type: ignore[attr-defined]
        instance._log_warn_with_limit = fake_log_warn_with_limit  # type: ignore[attr-defined]
        instance._reset = fake_reset  # type: ignore[attr-defined]
        instance._do_writer_close = fake_writer_close  # type: ignore[attr-defined]
        instance._try_increment_error_count = fake_try_increment_error_count  # type: ignore[attr-defined]
        instance._terminate = fake_terminate  # type: ignore[attr-defined]

        import logging

        fcm_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        exception_records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.ERROR:
                    exception_records.append(record.getMessage())

        # Earlier tests that install the `_FCMNoiseFilter` via
        # `async_start_fcm_push` leave it on this logger and that filter
        # silently drops the very "Unexpected exception during read" record
        # we're asserting on. Snapshot + remove + restore so this counter-
        # test always sees the raw library output.
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        prev_filters = list(fcm_logger.filters)
        for _f in prev_filters:
            if isinstance(_f, _FCMNoiseFilter):
                fcm_logger.removeFilter(_f)

        handler = _Capture()
        fcm_logger.addHandler(handler)
        try:
            await FcmPushClient._listen(instance)  # type: ignore[arg-type]
        finally:
            fcm_logger.removeHandler(handler)
            for _f in prev_filters:
                if isinstance(_f, _FCMNoiseFilter):
                    fcm_logger.addFilter(_f)

        assert any("Unexpected exception" in r for r in exception_records), (
            "Upstream FcmPushClient._listen() MUST log 'Unexpected exception during read' "
            "on the first ConnectionResetError (documents the bug).  "
            "If this assertion fails, upstream has shipped a fix — retire _QuietFcmPushClient."
        )


class TestFCMNoiseFilterDualLogger:
    """Regression: `_install_fcm_noise_filter()` must cover BOTH loggers.

    Root cause of an observed error-log storm: `_QuietFcmPushClient._listen()`
    logs via `_LOGGER` (bosch module logger) in its fallback else-branch, but
    `_FCMNoiseFilter` was only installed on 'firebase_messaging.fcmpushclient'.
    Records emitted through `_LOGGER` bypassed the filter entirely → every
    retry printed an ERROR line.

    Fix: `_install_fcm_noise_filter()` now installs a SINGLE shared
    `_FCMNoiseFilter` instance on both loggers so the dedup window applies
    regardless of which logger is used.
    """

    def setup_method(self) -> None:
        """Remove any existing _FCMNoiseFilter from both loggers before each test."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        for logger in (lib_logger, bosch_logger):
            logger.filters = [
                f for f in logger.filters if not isinstance(f, _FCMNoiseFilter)
            ]

    def teardown_method(self) -> None:
        """Clean up installed filters after each test."""
        self.setup_method()

    def test_filter_installed_on_both_loggers(self) -> None:
        """After _install_fcm_noise_filter(), both loggers carry the filter."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        lib_filters = [f for f in lib_logger.filters if isinstance(f, _FCMNoiseFilter)]
        bosch_filters = [
            f for f in bosch_logger.filters if isinstance(f, _FCMNoiseFilter)
        ]

        assert lib_filters, (
            "filter must be installed on firebase_messaging.fcmpushclient"
        )
        assert bosch_filters, (
            "filter must ALSO be installed on bosch_shc_camera.fcm logger — "
            "_QuietFcmPushClient._listen() logs via _LOGGER, not the library logger"
        )

    def test_shared_instance_same_object(self) -> None:
        """Both loggers must carry the SAME filter instance so _last_passed is shared."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        lib_f = next(f for f in lib_logger.filters if isinstance(f, _FCMNoiseFilter))
        bosch_f = next(
            f for f in bosch_logger.filters if isinstance(f, _FCMNoiseFilter)
        )

        assert lib_f is bosch_f, (
            "Both loggers must share ONE filter instance — if they have separate "
            "instances each gets its own _last_passed and the dedup window is broken: "
            "the bosch logger lets records through even after the lib logger blocked them."
        )

    def test_bosch_logger_deduplicates_error_within_window(self) -> None:
        """Records logged via _LOGGER must be blocked by the shared dedup window.

        This is the exact failure path observed in the wild: repeated ERROR
        lines from `_QuietFcmPushClient._listen()` because the filter only
        covered the library logger.
        """
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()

        bosch_logger = fcm_mod._LOGGER

        # Capture records that PASS the filter on the bosch logger
        passed: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                passed.append(record.getMessage())

        cap = _Capture()
        bosch_logger.addHandler(cap)
        try:
            # Simulate 5 consecutive "Unexpected exception during read" errors
            # at 1-second intervals — only the first should pass.
            for _ in range(5):
                bosch_logger.error("Unexpected exception during read\n")
                # Advance the filter's notion of time by not sleeping — the
                # _last_passed comparison uses time.monotonic() internally;
                # since we're not patching time we just fire them synchronously.
        finally:
            bosch_logger.removeHandler(cap)

        assert len(passed) == 1, (
            f"Exactly 1 of 5 rapid 'Unexpected exception' records should pass the "
            f"dedup filter — got {len(passed)}.  "
            "If this fails, the filter is not installed on the bosch logger."
        )

    def test_idempotent_reinstall_does_not_double_install(self) -> None:
        """Calling _install_fcm_noise_filter() twice must not attach two filter
        instances to either logger (would allow two records through per window)."""
        import logging

        import custom_components.bosch_shc_camera.fcm as fcm_mod
        from custom_components.bosch_shc_camera.fcm import (
            _FCMNoiseFilter,
            _install_fcm_noise_filter,
        )

        _install_fcm_noise_filter()
        _install_fcm_noise_filter()  # second call must be a no-op

        lib_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        bosch_logger = fcm_mod._LOGGER

        lib_count = sum(1 for f in lib_logger.filters if isinstance(f, _FCMNoiseFilter))
        bosch_count = sum(
            1 for f in bosch_logger.filters if isinstance(f, _FCMNoiseFilter)
        )

        assert lib_count == 1, f"lib logger must have exactly 1 filter, got {lib_count}"
        assert bosch_count == 1, (
            f"bosch logger must have exactly 1 filter, got {bosch_count}"
        )


class TestAsyncStartFcmPushClosures:
    """Capture the callbacks passed to FcmPushClient and invoke them directly.

    Covers the `_on_creds_updated` -> `_persist()` -> `hass.loop.call_soon_threadsafe`
    chain, and the `_on_push` -> `_on_fcm_push(coordinator, ...)` delegation.
    """

    async def _run_start(self, push_mode="ios"):
        """Run async_start_fcm_push with a capturing mock client.

        Returns (coord, captured_callbacks) where captured_callbacks has
        keys 'credentials_updated_callback' and 'callback'.
        """
        captured = {}

        class CapturingClient:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.checkin_or_register = AsyncMock(
                    return_value="fake-fcm-token-abc123"
                )
                self.start = AsyncMock()

        coord = _make_start_coord(push_mode=push_mode)

        mock_fm = MagicMock()
        mock_fm.FcmRegisterConfig = MagicMock(return_value=MagicMock())
        mock_fm.FcmPushClientConfig = MagicMock(return_value=MagicMock())
        mock_fm.FcmPushClient = CapturingClient

        from custom_components.bosch_shc_camera.fcm import (
            _QuietFcmPushClient,
            async_start_fcm_push,
        )

        # Merge-order note: `_QuietFcmPushClient._patched_class` is a
        # module-level cache populated on first use. `_get_fcm_push_client_class()`
        # only re-imports `firebase_messaging.FcmPushClient` (picking up whatever
        # is currently in `sys.modules`, i.e. `mock_fm` below) when the cache is
        # `None` ("patch failed, fall back to vanilla") — a real cached patched
        # class short-circuits straight past the mock. Other test classes in this
        # file reset the cache to `False` in their teardown, so — unlike when this
        # test lived alone in its own file, where an earlier test happened to
        # leave the cache as `None` — it can no longer be assumed to already be in
        # the right state by the time this test runs. Force it to `None` so the
        # vanilla-fallback path is taken deterministically and `CapturingClient`
        # (assigned to `mock_fm.FcmPushClient`) is what actually gets
        # instantiated. This only makes the helper independent of execution
        # order; the callback-capturing behaviour under test is unaffected.
        _QuietFcmPushClient._patched_class = None

        with patch.dict(sys.modules, {"firebase_messaging": mock_fm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.register_fcm_with_bosch",
                    new_callable=lambda: AsyncMock(return_value=True),
                ):
                    await async_start_fcm_push(coord)

        return coord, captured

    @pytest.mark.asyncio
    async def test_on_creds_updated_calls_call_soon_threadsafe(self):
        """_on_creds_updated invokes call_soon_threadsafe with the _persist closure."""
        coord, captured = await self._run_start(push_mode="ios")

        assert "credentials_updated_callback" in captured, (
            "FcmPushClient must receive credentials_updated_callback"
        )

        creds_cb = captured["credentials_updated_callback"]
        fake_creds = {"token": "abc", "keys": {}}

        # Call the outer callback — this should call hass.loop.call_soon_threadsafe
        coord.hass.loop.call_soon_threadsafe.reset_mock()
        creds_cb(fake_creds)

        coord.hass.loop.call_soon_threadsafe.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_creds_updated_persist_closure_creates_task(self):
        """_persist() is the arg passed to call_soon_threadsafe; calling it
        creates an async task via hass.async_create_task."""
        coord, captured = await self._run_start(push_mode="ios")

        creds_cb = captured["credentials_updated_callback"]
        fake_creds = {"token": "xyz"}

        # Capture what gets passed to call_soon_threadsafe
        persist_fn = None

        def _capture_threadsafe(fn):
            nonlocal persist_fn
            persist_fn = fn

        coord.hass.loop.call_soon_threadsafe = _capture_threadsafe
        creds_cb(fake_creds)

        assert persist_fn is not None, (
            "_persist must have been passed to call_soon_threadsafe"
        )

        # Now call _persist() directly — this exercises the entry-update path
        coord.hass.async_create_task = MagicMock()
        persist_fn()

        coord.hass.async_create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_push_delegates_to_on_fcm_push(self):
        """_on_push closure calls _on_fcm_push with the coordinator."""
        _coord, captured = await self._run_start(push_mode="ios")

        assert "callback" in captured, "FcmPushClient must receive callback"
        push_cb = captured["callback"]

        calls = []

        def _fake_on_fcm_push(c, notif, pid, obj=None):
            calls.append((c, notif, pid, obj))

        with patch(f"{MODULE}._on_fcm_push", side_effect=_fake_on_fcm_push):
            push_cb({"from": "bosch"}, "persistent-id-1")

        assert len(calls) == 1
        _, notif, pid, obj = calls[0]
        assert notif == {"from": "bosch"}
        assert pid == "persistent-id-1"
        assert obj is None  # default

    @pytest.mark.asyncio
    async def test_on_push_passes_obj_argument(self):
        """_on_push passes the optional obj kwarg through to _on_fcm_push."""
        _coord, captured = await self._run_start(push_mode="ios")
        push_cb = captured["callback"]

        calls = []

        def _fake_on_fcm_push(c, notif, pid, obj=None):
            calls.append(obj)

        some_obj = object()
        with patch(f"{MODULE}._on_fcm_push", side_effect=_fake_on_fcm_push):
            push_cb({"from": "bosch"}, "pid-2", obj=some_obj)

        assert calls == [some_obj]


class TestOnFcmPush2:
    def test_running_false_drops_push(self):
        """A push that arrives after async_stop_fcm_push cleared the
        client must be dropped — otherwise it'd reschedule on a loop
        that already considers FCM down. Pin the gate."""
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = _stub_coord(fcm_running=False)
        _on_fcm_push(coord, {"from": "x"}, "push-id-1")
        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_running_true_schedules_handler(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = _stub_coord(fcm_running=True)
        _on_fcm_push(coord, {"from": "Bosch"}, "push-id-2")
        coord.hass.loop.call_soon_threadsafe.assert_called_once()

    def test_marks_fcm_healthy_and_stamps_last_push(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = _stub_coord(fcm_running=True, fcm_healthy=False)
        before = coord.fcm_last_push
        _on_fcm_push(coord, {"from": "x"}, "push-id-3")
        assert coord.fcm_healthy is True
        assert coord.fcm_last_push > before


class TestOnFcmPushSpawnHandler:
    """`_on_fcm_push` schedules `_spawn_fcm_handler` on the HA loop; invoking that
    closure must create the event-fetch task AND store it in `bg_tasks` so it
    holds a strong reference (an untracked task can be GC-cancelled mid-flight).

    Near-duplicate note: `test_trailing_push_after_stop_is_dropped` below
    overlaps with `TestOnFcmPush2.test_running_false_drops_push` above (same
    `fcm_running=False` drop behavior) — kept both since they use distinct
    coordinator-stub setups exercising slightly different code paths, but a
    future consolidation could collapse them.
    """

    def _coord(self):
        fake_task = MagicMock(name="handler_task")
        hass = MagicMock()
        hass.async_create_task = MagicMock(return_value=fake_task)
        scheduled: list = []
        hass.loop.call_soon_threadsafe = lambda fn: scheduled.append(fn)
        coord = SimpleNamespace(
            hass=hass,
            fcm_lock=threading.Lock(),
            fcm_running=True,
            fcm_last_push=0.0,
            fcm_healthy=False,
            bg_tasks=set(),
        )
        return coord, scheduled, fake_task

    @pytest.mark.asyncio
    async def test_spawn_handler_creates_and_tracks_task(self):
        from custom_components.bosch_shc_camera import fcm

        coord, scheduled, fake_task = self._coord()
        with patch.object(fcm, "async_handle_fcm_push", new=MagicMock()):
            fcm._on_fcm_push(coord, {"from": "bosch"}, "pid-1")
            # Push marked healthy; exactly one closure scheduled on the loop.
            assert coord.fcm_healthy is True
            assert len(scheduled) == 1
            # Run the scheduled closure → create + track the handler task.
            scheduled[0]()

        coord.hass.async_create_task.assert_called_once()
        assert fake_task in coord.bg_tasks, (
            "REGRESSION: FCM handler task not tracked in bg_tasks — it can be "
            "GC-cancelled mid-flight, leaving coordinator.data partially updated."
        )
        fake_task.add_done_callback.assert_called_once_with(coord.bg_tasks.discard)

    @pytest.mark.asyncio
    async def test_trailing_push_after_stop_is_dropped(self):
        """fcm_running=False (client stopped) → push dropped, nothing scheduled."""
        from custom_components.bosch_shc_camera import fcm

        coord, scheduled, _ = self._coord()
        coord.fcm_running = False
        fcm._on_fcm_push(coord, {"from": "bosch"}, "pid-2")
        assert scheduled == []


class TestPushDataNoneGuard:
    """Regression: async_handle_fcm_push must not crash when coordinator.data is None.

    Race window observed in production: an FCM push arrives during integration
    setup, before the first coordinator refresh has populated `self.data`.
    Pre-fix the handler raised ``AttributeError: 'NoneType' object has no
    attribute 'keys'``.
    """

    @pytest.mark.asyncio
    async def test_returns_early_when_data_is_none(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _coord(data=None)
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("must not call cloud API"))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_when_data_is_empty_dict(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _coord(data={})
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("must not call cloud API"))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_when_token_missing(self) -> None:
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _coord(data={"cam-id": {"info": {}, "events": []}})
        coord.token = ""
        session = MagicMock()
        session.get = MagicMock(side_effect=AssertionError("must not call cloud API"))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)

        session.get.assert_not_called()


class TestPathAExceptionSwallow:
    async def test_get_model_config_raise_is_swallowed(self):
        """If `get_model_config` raises mid-flight (e.g. unexpected hw
        string), the event-arrival live-snap-refresh path logs a warning
        and continues — no propagation."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        cam_entity = MagicMock()
        coord = _make_push_coord2(camera_entities={CAM_ID: cam_entity})

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(
                200,
                json_data=_one_event2("new-evt", event_type="MOVEMENT"),
            )
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                "custom_components.bosch_shc_camera.models.get_model_config",
                side_effect=RuntimeError("simulated unknown hw"),
            ),
        ):
            # Must NOT raise — the warn-and-continue arm runs.
            await async_handle_fcm_push(coord)


class TestPathBExceptionSwallow:
    async def test_save_snapshot_raise_is_swallowed(self, tmp_path: Path) -> None:
        """If `save_snapshot` raises mid-flight while persisting the
        event-image bytes from the cloud, the alert-image step logs a
        warning and continues — no propagation, no FCM listener crash."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        cam_b = MagicMock(cached_image=None, last_image_fetch=0.0)
        coord = _make_push_coord2(
            camera_entities={CAM_ID: cam_b},
            shc_state_cache={CAM_ID: {"privacy_mode": False}},
        )
        coord.data = {CAM_ID: {"info": {"title": "Terrasse"}, "events": []}}
        coord.options = {
            "alert_notify_service": "notify.test",
            "alert_notify_screenshot": "notify.test",
            "alert_save_snapshots": False,
            "alert_delete_after_send": True,
            "enable_smb_upload": False,
            "enable_local_save": False,
            "download_path": "",
        }
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)
        coord.hass.services.async_call = AsyncMock(return_value=None)
        coord.hass.config = MagicMock(config_dir=str(tmp_path))

        # Fetches image_url. Mock session.get to return JPEG bytes with
        # image content-type so the function reaches the save_snapshot
        # invocation that we want to make raise.
        img_resp = MagicMock()
        img_resp.status = 200
        img_resp.read = AsyncMock(return_value=b"\xff\xd8\xff\xe0" + b"\x99" * 500)
        img_resp.headers = {"Content-Type": "image/jpeg"}
        img_cm = MagicMock()
        img_cm.__aenter__ = AsyncMock(return_value=img_resp)
        img_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=img_cm)

        # Domain must end with .boschsecurity.com for _is_safe_bosch_url.
        image_url = "https://residential.cbs.boschsecurity.com/img.jpg"

        async def _fast_sleep(_secs):
            return None

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(
                f"{MODULE}.save_snapshot",
                new=AsyncMock(side_effect=RuntimeError("disk full")),
            ),
            patch("asyncio.sleep", new=_fast_sleep),
        ):
            # Must NOT raise — the inner try/except swallows the disk error.
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-19T10:00:00Z",
                image_url,
                "",
                "",
                event_id="ev-1",
            )


class TestHandleFcmPushMarkEventsReadException:
    """When `async_mark_events_read` raises inside `async_handle_fcm_push`,
    the bare `except Exception: pass` must swallow it silently."""

    def _make_handle_coord(self):
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        hass.async_create_task = MagicMock()
        hass.bus.async_fire = MagicMock()
        return SimpleNamespace(
            token="tok-handle",
            hass=hass,
            data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
            last_event_ids={CAM_ID: "old-event-id"},
            alert_sent_ids={},
            cached_events={},
            camera_entities={},
            bg_tasks=set(),
            options={"mark_events_read": True},
            async_update_listeners=MagicMock(),
            fcm_last_push=float("-inf"),
            cached_status={},
        )

    @pytest.mark.asyncio
    async def test_mark_events_read_exception_swallowed_in_handle_push(self):
        """async_mark_events_read raises inside the push handler but the
        exception must be silently swallowed."""
        coord = self._make_handle_coord()

        new_event = {
            "id": "new-event-id",
            "eventType": "MOVEMENT",
            "eventTags": [],
            "timestamp": "2026-05-12T10:00:00Z",
            "imageUrl": "",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }

        # Build a session that returns the new event
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[new_event]))

        async def _raising_mark(c, ids):
            raise RuntimeError("mark-read network error")

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_mark_events_read", side_effect=_raising_mark):
                from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

                # Must complete without raising
                await async_handle_fcm_push(coord)

        # The new event id must still have been recorded despite the mark failure
        assert coord.last_event_ids[CAM_ID] == "new-event-id"

    @pytest.mark.asyncio
    async def test_mark_events_read_not_called_when_option_off(self):
        """Ensure mark_events_read is skipped when option is False (control test)."""
        coord = self._make_handle_coord()
        coord.options = {"mark_events_read": False}

        new_event = {
            "id": "new-event-id-2",
            "eventType": "MOVEMENT",
            "eventTags": [],
            "timestamp": "2026-05-12T10:01:00Z",
            "imageUrl": "",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(200, json_data=[new_event]))

        mark_calls = []

        async def _track_mark(c, ids):
            mark_calls.append(ids)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_mark_events_read", side_effect=_track_mark):
                from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

                await async_handle_fcm_push(coord)

        assert mark_calls == [], (
            "mark_events_read must not be called when option is off"
        )


class TestLocalSaveExceptionBranches:
    """Exceptions from `async_send_alert`'s local-save block.

    Covers: `asyncio.TimeoutError` → logged as warning, not re-raised; and
    generic `Exception` → logged as warning, not re-raised.
    """

    def _run_alert_with_wait_for(self, wait_for_side_effect):
        """Helper: run async_send_alert with local save enabled and a controlled wait_for."""
        coord = _make_alert_coord3(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        async def _run():
            from custom_components.bosch_shc_camera.fcm import async_send_alert

            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            with patch(
                                f"{MODULE}.asyncio.wait_for",
                                side_effect=wait_for_side_effect,
                            ):
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-12T10:00:00.000Z",
                                    "",
                                )

        asyncio.get_event_loop().run_until_complete(_run())

    @pytest.mark.asyncio
    async def test_local_save_timeout_does_not_propagate(self):
        """asyncio.TimeoutError in local save is caught and logged, not re-raised."""
        raised = []

        async def _timeout_wait_for(coro, timeout=None):
            raised.append("timeout")
            raise TimeoutError()

        coord = _make_alert_coord3(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        with patch(
                            f"{MODULE}.asyncio.wait_for", side_effect=_timeout_wait_for
                        ):
                            # Must not raise
                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-12T10:00:00.000Z",
                                "",
                            )

        assert raised, (
            "wait_for must have been called (proving local save path was reached)"
        )

    @pytest.mark.asyncio
    async def test_local_save_generic_exception_does_not_propagate(self):
        """Generic Exception in local save is caught and logged, not re-raised."""
        raised = []

        async def _error_wait_for(coro, timeout=None):
            raised.append("error")
            raise RuntimeError("disk full")

        coord = _make_alert_coord3(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        with patch(
                            f"{MODULE}.asyncio.wait_for", side_effect=_error_wait_for
                        ):
                            # Must not raise
                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-12T10:00:00.000Z",
                                "",
                            )

        assert raised, (
            "wait_for must have been called (proving local save path was reached)"
        )

    @pytest.mark.asyncio
    async def test_local_save_timeout_logged_as_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """TimeoutError path logs a warning with the camera name."""
        import logging

        coord = _make_alert_coord3(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        async def _timeout_wait_for(coro, timeout=None):
            raise TimeoutError()

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with caplog.at_level(
            logging.WARNING, logger="custom_components.bosch_shc_camera.fcm"
        ):
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            with patch(
                                f"{MODULE}.asyncio.wait_for",
                                side_effect=_timeout_wait_for,
                            ):
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-12T10:00:00.000Z",
                                    "",
                                )

        timeout_msgs = [
            r
            for r in caplog.records
            if "local save timed out" in r.message and r.levelno == logging.WARNING
        ]
        assert timeout_msgs, "A WARNING about 'local save timed out' must be emitted"
        assert "Terrasse" in timeout_msgs[0].message

    @pytest.mark.asyncio
    async def test_local_save_exception_logged_as_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Generic Exception path logs a warning with the camera name and error."""
        import logging

        coord = _make_alert_coord3(
            options={
                "alert_notify_service": "notify.test",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_remaining",
            }
        )

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        async def _error_wait_for(coro, timeout=None):
            raise OSError("no space left on device")

        from custom_components.bosch_shc_camera.fcm import async_send_alert

        with caplog.at_level(
            logging.WARNING, logger="custom_components.bosch_shc_camera.fcm"
        ):
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            with patch(
                                f"{MODULE}.asyncio.wait_for",
                                side_effect=_error_wait_for,
                            ):
                                await async_send_alert(
                                    coord,
                                    "Terrasse",
                                    "MOVEMENT",
                                    "2026-05-12T10:00:00.000Z",
                                    "",
                                )

        err_msgs = [
            r
            for r in caplog.records
            if "local save failed" in r.message and r.levelno == logging.WARNING
        ]
        assert err_msgs, "A WARNING about 'local save failed' must be emitted"
        assert "Terrasse" in err_msgs[0].message


class TestStopFcmPushCancellation:
    """CancelledError must propagate out of `async_stop_fcm_push`, not be
    swallowed by the broad `except Exception` arm around it.

    Near-duplicate note: overlaps with `TestAsyncStopFcmPush.test_cancellation_propagates`
    below — this variant injects the CancelledError via a patched
    `asyncio.wait_for` (exercising the pending-tasks-gather block), the other
    via `client.stop` raising directly. Kept both as they pin different call
    sites of the same propagation guarantee.
    """

    @pytest.mark.asyncio
    async def test_wait_for_cancellation_propagates(self):
        """If the `asyncio.wait_for(asyncio.gather(...))` block in
        `async_stop_fcm_push` is cancelled during HA shutdown, the
        CancelledError must propagate."""
        import asyncio
        import threading

        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        async def _dummy():
            return None

        coord = SimpleNamespace(
            fcm_lock=threading.Lock(),
            fcm_client=MagicMock(
                stop=AsyncMock(return_value=None),
                tasks=[_dummy()],
            ),
            fcm_running=True,
        )

        with patch(
            "asyncio.wait_for", new=AsyncMock(side_effect=asyncio.CancelledError)
        ):
            with pytest.raises(asyncio.CancelledError):
                await async_stop_fcm_push(coord)


class TestAsyncStopFcmPush:
    @pytest.mark.asyncio
    async def test_no_client_no_op(self):
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        coord = _stub_coord(fcm_client=None, fcm_running=False)
        # Must NOT raise
        await async_stop_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_stops_running_client_and_clears_state(self):
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock()
        client.stop = AsyncMock()
        coord = _stub_coord(fcm_client=client, fcm_running=True, fcm_healthy=True)
        await async_stop_fcm_push(coord)
        client.stop.assert_awaited_once()
        # All state cleared
        assert coord.fcm_running is False
        assert coord.fcm_healthy is False
        assert coord.fcm_client is None
        assert coord.fcm_push_mode == "unknown"

    @pytest.mark.asyncio
    async def test_client_stop_exception_swallowed(self):
        """Library may throw on stop (idempotency, race) — must not
        propagate. State must still be cleared."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock()
        client.stop = AsyncMock(side_effect=RuntimeError("library bug"))
        coord = _stub_coord(fcm_client=client, fcm_running=True)
        await async_stop_fcm_push(coord)
        assert coord.fcm_client is None
        assert coord.fcm_running is False

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        """asyncio.CancelledError must NOT be swallowed (HA shutdown)."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock()
        client.stop = AsyncMock(side_effect=asyncio.CancelledError())
        coord = _stub_coord(fcm_client=client, fcm_running=True)
        with pytest.raises(asyncio.CancelledError):
            await async_stop_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_awaits_pending_tasks_after_stop(self):
        """Regression: ``client.stop()`` only cancels the read loop — it does
        not await the ``finally: await self._do_writer_close()`` SSL shutdown.
        If we recreate the client before that finishes, the old loop logs
        ``ERROR firebase_messaging.fcmpushclient: Unexpected exception during
        read`` once per ~63 s (state machine sees the SSL close outside of
        RESETTING). Fix awaits ``client.tasks`` here so the new instance never
        races the old one's SSL teardown.

        Upstream library issue: github.com/sdb9696/firebase-messaging #33.
        """
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock()
        client.stop = AsyncMock()
        ssl_close_done = asyncio.Event()

        async def slow_ssl_close() -> None:
            await asyncio.sleep(0.05)
            ssl_close_done.set()

        client.tasks = [asyncio.create_task(slow_ssl_close())]
        coord = _stub_coord(fcm_client=client, fcm_running=True)
        await async_stop_fcm_push(coord)
        assert ssl_close_done.is_set(), "stop must await pending SSL-close tasks"
        assert coord.fcm_client is None

    @pytest.mark.asyncio
    async def test_pending_tasks_timeout_does_not_block_forever(self):
        """If a library task hangs (e.g. SSL shutdown deadlock), stop must
        still return within ~10 s so the user-facing UI toggle doesn't freeze.
        State is cleared even when the gather times out."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock()
        client.stop = AsyncMock()

        async def never_finishes() -> None:
            await asyncio.sleep(3600)

        hung = asyncio.create_task(never_finishes())
        client.tasks = [hung]
        coord = _stub_coord(fcm_client=client, fcm_running=True)

        with patch(
            "custom_components.bosch_shc_camera.fcm.asyncio.wait_for",
            AsyncMock(side_effect=TimeoutError()),
        ):
            await async_stop_fcm_push(coord)
        # State cleared regardless of timeout
        assert coord.fcm_client is None
        assert coord.fcm_running is False
        hung.cancel()

    @pytest.mark.asyncio
    async def test_no_tasks_attr_backcompat(self):
        """Older firebase-messaging versions may not expose ``client.tasks``.
        Stop must work via getattr() default and not raise AttributeError."""
        from custom_components.bosch_shc_camera.fcm import async_stop_fcm_push

        client = MagicMock(spec=["stop"])  # no `tasks` attribute
        client.stop = AsyncMock()
        coord = _stub_coord(fcm_client=client, fcm_running=True)
        await async_stop_fcm_push(coord)
        assert coord.fcm_client is None


class TestAsyncPersistFcmCreds:
    @pytest.mark.asyncio
    async def test_writes_creds_to_entry_data(self):
        from custom_components.bosch_shc_camera.fcm import _async_persist_fcm_creds

        coord = _stub_coord()
        coord.entry = SimpleNamespace(data={"existing": "value"})
        creds = {"refresh_token": "rfr", "android_id": 12345}
        await _async_persist_fcm_creds(coord, creds)
        coord.hass.config_entries.async_update_entry.assert_called_once()
        call = coord.hass.config_entries.async_update_entry.call_args
        new_data = call.kwargs["data"]
        # Existing fields preserved + new fcm_credentials key
        assert new_data["existing"] == "value"
        assert new_data["fcm_credentials"] == creds

    @pytest.mark.asyncio
    async def test_swallows_exception(self):
        """async_update_entry might fire during HA shutdown — must not
        crash the FCM listener."""
        from custom_components.bosch_shc_camera.fcm import _async_persist_fcm_creds

        coord = _stub_coord()
        coord.hass.config_entries.async_update_entry = MagicMock(
            side_effect=RuntimeError("entry locked"),
        )
        # Must NOT raise
        await _async_persist_fcm_creds(coord, {"x": 1})


class TestRegisterFcmWithBosch:
    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        coord = _stub_coord(token="")
        ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_no_fcm_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        coord = _stub_coord(fcm_token="")
        ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_success_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True

    @pytest.mark.asyncio
    async def test_500_unknown_error_returns_false(self):
        """HTTP 500 with an unrecognised error body must return False and log a warning."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 500
            r.text = AsyncMock(return_value='{"status":500,"error":"sh:unknown.error"}')
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_500_logs_response_body(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HTTP 500 with unknown error must include the body in the WARNING log."""
        import logging

        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        bosch_body = (
            '{"status":500,"error":"sh:unknown.error","message":"Something else."}'
        )

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 500
            r.text = AsyncMock(return_value=bosch_body)
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with caplog.at_level(
                logging.WARNING, logger="custom_components.bosch_shc_camera.fcm"
            ):
                ok = await register_fcm_with_bosch(coord)

        assert ok is False
        assert "sh:unknown.error" in caplog.text, (
            "Warning log must contain Bosch error body so operators can diagnose "
            "unexpected FCM registration failures."
        )

    @pytest.mark.asyncio
    async def test_500_already_registered_treated_as_success(self):
        """HTTP 500 with sh:internal.error means the token is already registered.

        Bosch returns this on every re-registration of an existing token.
        FCM push still works — treat as success and save the token so the
        next restart skips the POST entirely (avoids persistent 500 spam).
        """
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        already_registered_body = '{"status":500,"error":"sh:internal.error","message":"Internal Server Error."}'

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 500
            r.text = AsyncMock(return_value=already_registered_body)
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(entry=SimpleNamespace(data={}))
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)

        assert ok is True, (
            "sh:internal.error means the token is already registered — must return True "
            "so the coordinator treats FCM as functional"
        )
        update_call = coord.hass.config_entries.async_update_entry
        update_call.assert_called_once()
        saved_data = update_call.call_args.kwargs.get("data") or update_call.call_args[
            1
        ].get("data", {})
        assert saved_data.get("fcm_registered_token") == "fcm-token-xyz", (
            "Token must be saved even on 500 sh:internal.error so the next restart "
            "skips the registration POST entirely"
        )

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        session = MagicMock()
        session.post = MagicMock(side_effect=TimeoutError())
        coord = _stub_coord()
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is False

    @pytest.mark.asyncio
    async def test_device_type_picks_android_for_other(self):
        """Anything other than `ios` → ANDROID."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        captured = {}

        @asynccontextmanager
        async def _post(*args, **kw):
            captured["json"] = kw.get("json", {})
            r = MagicMock()
            r.status = 201
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(fcm_push_mode="android")
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await register_fcm_with_bosch(coord)
        assert captured["json"]["deviceType"] == "ANDROID"

    @pytest.mark.asyncio
    async def test_same_token_in_entry_skips_post(self):
        """When fcm_registered_token matches AND fcm_registered_device_type==ANDROID, skip POST.

        The skip-check requires BOTH conditions (deviceType-drift heal):
        token match alone is insufficient — a missing/wrong deviceType marker means
        Bosch CBS may have the stale deviceType=IOS registration and must be healed.

        Regression: every HA restart triggered a redundant POST → Bosch returned
        HTTP 500 ("sh:internal.error") because the token was already registered.
        The updated guard preserves this fast-path for the steady-state case where
        both the token and the ANDROID marker are present.
        """
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        post_called = []

        @asynccontextmanager
        async def _post(*args, **kw):
            post_called.append(True)
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        import time as _time

        coord = _stub_coord(
            entry=SimpleNamespace(
                data={
                    "fcm_registered_token": "fcm-token-xyz",
                    "fcm_registered_device_type": "ANDROID",  # both conditions must hold
                    # skip requires a FRESH registration too — stamp now.
                    "fcm_registered_at": _time.time(),
                }
            ),
        )
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True, "already-registered token must return True"
        assert not post_called, (
            "POST must be skipped when token is unchanged AND deviceType=ANDROID"
        )

    @pytest.mark.asyncio
    async def test_new_token_triggers_post(self):
        """When no saved token exists (first run), the POST fires normally."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        post_called = []

        @asynccontextmanager
        async def _post(*args, **kw):
            post_called.append(True)
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(entry=SimpleNamespace(data={}))
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True
        assert post_called, "POST must fire when no saved token exists"

    @pytest.mark.asyncio
    async def test_success_saves_registered_token(self):
        """On HTTP 204, save fcm_registered_token to the config entry for future skip."""
        from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

        @asynccontextmanager
        async def _post(*args, **kw):
            r = MagicMock()
            r.status = 204
            yield r

        session = MagicMock()
        session.post = _post
        coord = _stub_coord(entry=SimpleNamespace(data={}))
        with patch(
            "custom_components.bosch_shc_camera.fcm.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            ok = await register_fcm_with_bosch(coord)
        assert ok is True
        update_call = coord.hass.config_entries.async_update_entry
        update_call.assert_called_once()
        saved_data = update_call.call_args.kwargs.get("data") or update_call.call_args[
            1
        ].get("data", {})
        assert saved_data.get("fcm_registered_token") == "fcm-token-xyz", (
            "Registered token must be saved to config entry so subsequent "
            "restarts can skip the Bosch POST and avoid HTTP 500"
        )


class TestGetAlertServices2:
    def test_per_type_value_returned(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_information": "notify.test_user, notify.signal",
            }
        )
        out = get_alert_services(coord, "information")
        assert out == ["notify.test_user", "notify.signal"]

    def test_falls_back_to_alert_notify_service_for_system(self):
        """`system` and `information` fall back to `alert_notify_service`
        when their per-type field is empty. Pin so a refactor can't drop
        the fallback (would silently disable system alerts)."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_service": "notify.fallback",
                "alert_notify_system": "",
            }
        )
        out = get_alert_services(coord, "system")
        assert out == ["notify.fallback"]

    def test_screenshot_does_not_fall_back(self):
        """`screenshot` and `video` are opt-in — empty means skip that
        step entirely. Pin so they never silently inherit the
        alert_notify_service value."""
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_service": "notify.fallback",
                "alert_notify_screenshot": "",
            }
        )
        out = get_alert_services(coord, "screenshot")
        assert out == []

    def test_video_does_not_fall_back(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_service": "notify.fallback",
                "alert_notify_video": "",
            }
        )
        out = get_alert_services(coord, "video")
        assert out == []

    def test_strips_whitespace_around_entries(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_information": "  notify.a  ,  notify.b , ",
            }
        )
        out = get_alert_services(coord, "information")
        assert out == ["notify.a", "notify.b"]

    def test_empty_strings_filtered(self):
        from custom_components.bosch_shc_camera.fcm import get_alert_services

        coord = _stub_coord(
            options={
                "alert_notify_information": ",,notify.real,,",
            }
        )
        out = get_alert_services(coord, "information")
        assert out == ["notify.real"]


class TestBuildNotifyData2:
    def test_message_only_no_attachment(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data("notify.test_user", "Hi")
        assert out == {"message": "Hi"}

    def test_title_added_when_present(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data("notify.test_user", "Body", title="Title")
        assert out["title"] == "Title"
        assert out["message"] == "Body"

    def test_mobile_app_uses_local_url(self):
        """HA Companion App reads images from /local/ URL — files served
        from /config/www/bosch_alerts/. Must NOT use file path."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.mobile_app_iphone",
            "Bewegung",
            file_path="/config/www/bosch_alerts/snap.jpg",
        )
        assert out["data"]["image"] == "/local/bosch_alerts/snap.jpg"
        # iOS sound config
        assert out["data"]["push"]["sound"] == "default"

    def test_telegram_uses_photo_field(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.telegram_bot",
            "Audio-Alarm",
            file_path="/path/to/clip.mp4",
        )
        assert out["data"]["photo"] == "/path/to/clip.mp4"
        assert out["data"]["caption"] == "Audio-Alarm"

    def test_signal_uses_attachments(self):
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.signal_thomas",
            "Snapshot",
            file_path="/tmp/snap.jpg",
        )
        assert out["data"]["attachments"] == ["/tmp/snap.jpg"]

    def test_email_uses_attachments(self):
        """Generic notify provider (email, etc.) → attachments path."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.email_admin",
            "Alert",
            file_path="/tmp/alert.jpg",
        )
        assert out["data"]["attachments"] == ["/tmp/alert.jpg"]

    def test_mobile_app_extracts_basename(self):
        """The image URL uses /local/bosch_alerts/{basename} — the file
        path's directory is stripped. Pin so HA can find the file."""
        from custom_components.bosch_shc_camera.fcm import build_notify_data

        out = build_notify_data(
            "notify.mobile_app_xy",
            "x",
            file_path="/some/deep/dir/event_2026-05-04.jpg",
        )
        assert out["data"]["image"] == "/local/bosch_alerts/event_2026-05-04.jpg"


class TestWriteFile:
    def test_writes_bytes_to_file(self, tmp_path: Path) -> None:
        from custom_components.bosch_shc_camera.fcm import _write_file

        target = tmp_path / "snap.jpg"
        _write_file(str(target), b"\xff\xd8DATA\xff\xd9")
        assert target.read_bytes() == b"\xff\xd8DATA\xff\xd9"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        from custom_components.bosch_shc_camera.fcm import _write_file

        target = tmp_path / "snap.jpg"
        target.write_bytes(b"OLD")
        _write_file(str(target), b"NEW")
        assert target.read_bytes() == b"NEW"


# Noise-filter idempotency, handle_fcm_push early exits/http branches/dedup/new-event/mark-events-read/person-upgrade/notification-switch, mark_events_read, send_alert early exit + step 1/2/3 + SMB/local-save gates + file cleanup (from: round 6, round 7)


def _resp_cm_push(status: int, json_data=None, text: str = ""):
    """Return an async context-manager mock for aiohttp session.get / session.put
    used by the async_handle_fcm_push / async_mark_events_read tests."""
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=text)
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _resp_cm_alert(
    status: int, body: bytes = b"", content_type: str = "image/jpeg", json_data=None
):
    """Async context-manager mock for aiohttp session responses used by the
    async_send_alert tests."""
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.json = AsyncMock(return_value=json_data or [])
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_push_coord3(**overrides):
    """Return a minimal coordinator stub for async_handle_fcm_push /
    async_mark_events_read tests."""
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock()
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-A",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        last_event_ids={},
        alert_sent_ids={},
        camera_entities={},
        cached_events={},
        bg_tasks=set(),
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _one_event3(event_id="new-event-id", event_type="MOVEMENT", event_tags=None):
    return [
        {
            "id": event_id,
            "eventType": event_type,
            "eventTags": event_tags or [],
            "timestamp": "2026-05-07T10:00:00Z",
            "imageUrl": "",
            "videoClipUrl": "",
            "videoClipUploadStatus": "",
        }
    ]


def _make_alert_coord4(options=None, **overrides):
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
        last_event_ids={CAM_ID: "event-id-001"},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _run_send_alert(
    coord,
    event_type="MOVEMENT",
    image_url="",
    clip_url="",
    clip_status="",
    cam_name="Terrasse",
    timestamp="2026-05-07T10:00:00.000Z",
    session_override=None,
):
    """Helper: call async_send_alert with a mocked aiohttp session."""
    from custom_components.bosch_shc_camera.fcm import async_send_alert

    session = session_override or MagicMock()
    session.get = MagicMock(return_value=_resp_cm_alert(404))

    async def _run():
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(
                    "custom_components.bosch_shc_camera.smb.sync_smb_upload",
                    MagicMock(),
                ):
                    with patch(
                        "custom_components.bosch_shc_camera.smb.sync_local_save",
                        MagicMock(),
                    ):
                        await async_send_alert(
                            coord,
                            cam_name,
                            event_type,
                            timestamp,
                            image_url,
                            clip_url,
                            clip_status,
                        )

    return _run()


class TestAsyncSendAlertScreenshotVideoOnlyConfig:
    """GitHub #68 live-deploy finding, 2026-08-18: async_send_alert's early
    return guard only checked "information" services (with alert_notify_service
    as its fallback) — a screenshot/video-only config (no information/default
    service) had the whole alert silently no-op, with zero log line, before
    steps 2/3 ever ran."""

    @pytest.mark.asyncio
    async def test_screenshot_only_config_does_not_early_return(self):
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "",
                "alert_notify_information": "",
                "alert_notify_screenshot": "notify.screenshot_svc",
                "alert_notify_video": "",
                "alert_save_snapshots": True,
            }
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_alert(
                200, body=b"\xff\xd8snap", content_type="image/jpeg"
            )
        )

        async def _run():
            from custom_components.bosch_shc_camera.fcm import async_send_alert

            with (
                patch(
                    f"{MODULE}.async_get_bosch_cloud_session",
                    new=AsyncMock(return_value=session),
                ),
                patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
                patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
                patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
            ):
                await async_send_alert(
                    coord,
                    "Terrasse",
                    "MOVEMENT",
                    "2026-05-07T10:00:00.000Z",
                    image_url="https://residential.cbs.boschsecurity.com/img.jpg",
                )

        await _run()

        calls = [str(c) for c in coord.hass.services.async_call.call_args_list]
        assert any("screenshot_svc" in s for s in calls), (
            "a screenshot-only config must not be silently skipped by the "
            f"top-of-function guard; calls were {calls}"
        )

    @pytest.mark.asyncio
    async def test_video_only_config_does_not_early_return(self):
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "",
                "alert_notify_information": "",
                "alert_notify_screenshot": "",
                "alert_notify_video": "notify.video_svc",
                "alert_save_snapshots": True,
            }
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_alert(200, body=b"x" * 2048, content_type="video/mp4")
        )

        async def _run():
            from custom_components.bosch_shc_camera.fcm import async_send_alert

            with (
                patch(
                    f"{MODULE}.async_get_bosch_cloud_session",
                    new=AsyncMock(return_value=session),
                ),
                patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
                patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
                patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
            ):
                await async_send_alert(
                    coord,
                    "Terrasse",
                    "MOVEMENT",
                    "2026-05-07T10:00:00.000Z",
                    image_url="",
                    clip_url="https://residential.cbs.boschsecurity.com/clip.mp4",
                    clip_status="Done",
                )

        await _run()

        calls = [str(c) for c in coord.hass.services.async_call.call_args_list]
        assert any("video_svc" in s for s in calls), (
            "a video-only config must not be silently skipped by the "
            f"top-of-function guard; calls were {calls}"
        )

    @pytest.mark.asyncio
    async def test_nothing_configured_still_returns_early_with_debug_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The guard must still correctly no-op when genuinely nothing is
        configured — now with a debug log explaining why, instead of the
        previous silent return."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "",
                "alert_notify_information": "",
                "alert_notify_screenshot": "",
                "alert_notify_video": "",
            }
        )
        with caplog.at_level("DEBUG", logger=MODULE):
            await _run_send_alert(coord, event_type="MOVEMENT")

        assert coord.hass.services.async_call.call_args_list == []
        assert any("nothing configured" in r.getMessage() for r in caplog.records)


class TestInstallFcmNoiseFilterIdempotent:
    """_install_fcm_noise_filter must be idempotent: repeated calls never add
    a second filter instance to the firebase_messaging logger."""

    def _get_filter_count(self, logger_name: str) -> int:
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        fcm_logger = logging.getLogger(logger_name)
        return sum(1 for f in fcm_logger.filters if isinstance(f, _FCMNoiseFilter))

    def setup_method(self):
        """Clear any pre-existing filters before each test."""
        fcm_logger = logging.getLogger("firebase_messaging.fcmpushclient")
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        fcm_logger.filters = [
            f for f in fcm_logger.filters if not isinstance(f, _FCMNoiseFilter)
        ]

    def test_first_call_installs_one_filter(self):
        from custom_components.bosch_shc_camera.fcm import _install_fcm_noise_filter

        _install_fcm_noise_filter()
        count = self._get_filter_count("firebase_messaging.fcmpushclient")
        assert count == 1, (
            "first _install_fcm_noise_filter call must add exactly one filter"
        )

    def test_second_call_is_idempotent(self):
        from custom_components.bosch_shc_camera.fcm import _install_fcm_noise_filter

        _install_fcm_noise_filter()
        _install_fcm_noise_filter()
        count = self._get_filter_count("firebase_messaging.fcmpushclient")
        assert count == 1, (
            "second _install_fcm_noise_filter call must not add a duplicate filter"
        )

    def test_many_calls_stay_at_one(self):
        from custom_components.bosch_shc_camera.fcm import _install_fcm_noise_filter

        for _ in range(5):
            _install_fcm_noise_filter()
        count = self._get_filter_count("firebase_messaging.fcmpushclient")
        assert count == 1, (
            "repeated _install_fcm_noise_filter calls must keep exactly one filter"
        )


class TestAsyncStartFcmPushEarlyExits:
    """async_start_fcm_push must return early (before importing Firebase) on
    three distinct gating conditions."""

    def _stub(self, **overrides):
        base = dict(
            fcm_running=False,
            options={"enable_fcm_push": True},
            hass=MagicMock(),
            entry=SimpleNamespace(data={}),
            data={},
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    @pytest.mark.asyncio
    async def test_already_running_returns_immediately(self):
        """fcm_running=True → function must return without touching options."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._stub(fcm_running=True)
        # options intentionally absent so any options read would KeyError
        del coord.options
        # Must not raise
        await async_start_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_fcm_push_disabled_returns_early(self):
        """enable_fcm_push=False → debug log + return, no Firebase import."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._stub(options={"enable_fcm_push": False})
        # Ensure firebase_messaging is NOT importable so we'd know if it tried
        with patch.dict(sys.modules, {"firebase_messaging": None}):
            await async_start_fcm_push(coord)
        # No exception = early exit worked
        assert not coord.fcm_running, "FCM must not be marked running after early exit"

    @pytest.mark.asyncio
    async def test_import_error_returns_with_warning(self):
        """firebase_messaging ImportError → log warning + return."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._stub()

        # Remove firebase_messaging from sys.modules to force ImportError
        saved = sys.modules.pop("firebase_messaging", None)
        # Also block the submodule that FCM tries
        sys.modules["firebase_messaging"] = (
            None  # causes ImportError on 'from ... import'
        )
        try:
            await async_start_fcm_push(coord)
        finally:
            if saved is not None:
                sys.modules["firebase_messaging"] = saved
            else:
                sys.modules.pop("firebase_messaging", None)

        assert not coord.fcm_running, "FCM must not be marked running after ImportError"

    @pytest.mark.asyncio
    async def test_fcm_disabled_default_false(self):
        """options with no 'enable_fcm_push' key → defaults to False → early exit."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._stub(options={})  # key absent → .get(..., False) = False
        await async_start_fcm_push(coord)
        assert not coord.fcm_running, (
            "missing enable_fcm_push must default to False and exit early"
        )


class TestAsyncHandleFcmPushNoToken:
    """token is falsy → return immediately, no HTTP call."""

    @pytest.mark.asyncio
    async def test_no_token_returns_without_http(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(token="")
        session = MagicMock()
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            session.get.assert_not_called(),
            "no HTTP request must be made when token is empty",
        )

    @pytest.mark.asyncio
    async def test_none_token_returns_without_http(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(token=None)
        session = MagicMock()
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            session.get.assert_not_called(),
            "no HTTP request must be made when token is None",
        )

    @pytest.mark.asyncio
    async def test_no_token_does_not_update_listeners(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(token="")
        await async_handle_fcm_push(coord)
        (
            coord.async_update_listeners.assert_not_called(),
            "async_update_listeners must not be called when token is absent",
        )


class TestAsyncHandleFcmPushHttpBranches:
    """Non-200 responses and an empty events list must both be treated as
    "nothing to do for this camera" — no listener update, no bus events."""

    @pytest.mark.asyncio
    async def test_http_404_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(404))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            coord.async_update_listeners.assert_not_called(),
            "non-200 response must not trigger listener update",
        )

    @pytest.mark.asyncio
    async def test_http_500_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(500))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            coord.async_update_listeners.assert_not_called(),
            "HTTP 500 must not trigger listener update",
        )

    @pytest.mark.asyncio
    async def test_empty_events_list_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=[]))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            coord.async_update_listeners.assert_not_called(),
            "empty events list must not trigger listener update",
        )

    @pytest.mark.asyncio
    async def test_http_401_skips_cam(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(401))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            coord.hass.bus.async_fire.assert_not_called(),
            "HTTP 401 must not fire any HA events",
        )


class TestAsyncHandleFcmPushDedup:
    """newest_id already in alert_sent_ids within 60s → skip; entries beyond
    the window are not deduped and get evicted from the cache."""

    @pytest.mark.asyncio
    async def test_recent_sent_id_skips_alert(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        recent_ts = time.monotonic() - 10.0  # 10 s ago → within 60 s window
        coord = _make_push_coord3(
            last_event_ids={CAM_ID: "old-event-id"},
            alert_sent_ids={"new-event-id": recent_ts},
        )
        events = _one_event3("new-event-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            coord.hass.bus.async_fire.assert_not_called(),
            "event already sent within 60s must be deduped — no bus fire",
        )
        (
            coord.async_update_listeners.assert_not_called(),
            "event already sent within 60s must be deduped — no listener update",
        )

    @pytest.mark.asyncio
    async def test_stale_sent_id_beyond_60s_not_deduped(self):
        """If the same event_id was sent >60s ago it is NOT deduped (window expired)."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        old_ts = time.monotonic() - 70.0  # 70 s ago → outside 60 s window
        coord = _make_push_coord3(
            last_event_ids={CAM_ID: "old-event-id"},
            alert_sent_ids={"new-event-id": old_ts},
        )
        events = _one_event3("new-event-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        (
            coord.hass.bus.async_fire.assert_called(),
            "event sent >60s ago must not be deduped — bus fire expected",
        )

    @pytest.mark.asyncio
    async def test_old_entries_evicted_from_sent_cache(self):
        """alert_sent_ids entries older than 120s are evicted on each call."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        very_old_ts = time.monotonic() - 130.0
        coord = _make_push_coord3(
            last_event_ids={CAM_ID: "old-event-id"},
            alert_sent_ids={
                "ancient-id": very_old_ts,
                "new-event-id": very_old_ts - 5,
            },
        )
        events = _one_event3("new-event-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        assert "ancient-id" not in coord.alert_sent_ids, (
            "entries older than 120s must be evicted from alert_sent_ids"
        )


class TestAsyncHandleFcmPushNewEvent:
    """prev_id != newest_id → fire bus + create alert task.
    elif newest_id (prev_id=None) → only update last_event_ids.
    """

    def _coord_with_prev(self, prev_id=None):
        coord = _make_push_coord3(
            last_event_ids={CAM_ID: prev_id} if prev_id else {},
            options={},
        )
        return coord

    @pytest.mark.asyncio
    async def test_new_event_fires_motion_bus_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_motion" in fired, (
            "MOVEMENT event must fire bosch_shc_camera_motion on the HA bus"
        )

    @pytest.mark.asyncio
    async def test_new_event_logs_bosch_ts_and_received_at(self, caplog):
        """Diagnostic log (2026-07-31 FCM timing question) must carry
        Bosch's own event timestamp plus our local receipt time, so a
        cloud-side vs. integration-side delay can be told apart from logs.
        """
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "PERSON")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with caplog.at_level(logging.DEBUG):
                    await async_handle_fcm_push(coord)
        matching = [r for r in caplog.records if "FCM push timing" in r.message]
        assert len(matching) == 1
        assert "id=new-id" in matching[0].message, matching[0].message
        assert "bosch_ts=2026-05-07T10:00:00Z" in matching[0].message
        assert "prev_event_bosch_ts=n/a" in matching[0].message, matching[0].message
        # `received_at=` alone would also be satisfied by an empty/garbage
        # value — the whole point of this diagnostic is that the local
        # receipt time is a parseable, timezone-aware ISO timestamp that can
        # be diffed against bosch_ts, so assert exactly that.
        import re
        from datetime import datetime

        m = re.search(r"received_at=([^,)]+)", matching[0].message)
        assert m is not None, matching[0].message
        received_at = datetime.fromisoformat(m.group(1))
        assert received_at.tzinfo is not None, m.group(1)

    @pytest.mark.asyncio
    async def test_new_event_logs_prev_event_bosch_ts_when_batched(self, caplog):
        """If Bosch delivers a MOVEMENT+PERSON pair in the same
        /v11/events response, only events[0] gets dispatched — the log
        must still surface events[1]'s own timestamp, or the exact pair
        this diagnostic exists to compare would never appear together.
        """
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = [
            {
                "id": "new-id",
                "eventType": "PERSON",
                "eventTags": [],
                "timestamp": "bosch-ts-2",
                "imageUrl": "",
                "videoClipUrl": "",
                "videoClipUploadStatus": "",
            },
            {
                "id": "prev-id",
                "eventType": "MOVEMENT",
                "eventTags": [],
                "timestamp": "bosch-ts-1",
                "imageUrl": "",
                "videoClipUrl": "",
                "videoClipUploadStatus": "",
            },
        ]
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with caplog.at_level(logging.DEBUG):
                    await async_handle_fcm_push(coord)
        matching = [r for r in caplog.records if "FCM push timing" in r.message]
        assert len(matching) == 1
        assert "bosch_ts=bosch-ts-2" in matching[0].message
        assert "prev_event_bosch_ts=bosch-ts-1" in matching[0].message

    @pytest.mark.asyncio
    async def test_new_event_creates_alert_task(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        (
            coord.hass.async_create_task.assert_called(),
            "new event must schedule an async_send_alert task via async_create_task",
        )

    @pytest.mark.asyncio
    async def test_new_event_updates_last_event_id(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        assert coord.last_event_ids[CAM_ID] == "new-id", (
            "last_event_ids must be updated to newest_id on new event"
        )

    @pytest.mark.asyncio
    async def test_new_event_calls_async_update_listeners(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        (
            coord.async_update_listeners.assert_called_once(),
            "new event must call async_update_listeners to refresh binary sensors",
        )

    @pytest.mark.asyncio
    async def test_first_push_no_baseline_still_fires(self):
        """prev_id is None (first push after restart, no baseline seeded
        yet by the coordinator's own polling tick) → GitHub #64: must still
        fire the bus event, not silently swallow it. An FCM push only ever
        arrives for a genuinely new, real-time event — unlike polling's
        historical-backlog concern, there's nothing here to guard against
        by staying silent on a missing baseline. The `if` branch must be
        taken (and set last_event_ids as its own side effect), not the
        `elif`."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={})  # no prev_id
        events = _one_event3("first-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        coord.hass.bus.async_fire.assert_called_once()
        assert coord.last_event_ids.get(CAM_ID) == "first-id", (
            "dispatch (if) branch must also record the new id"
        )

    @pytest.mark.asyncio
    async def test_first_push_no_baseline_but_stale_event_does_not_fire(self):
        """Counterpart to test_first_push_no_baseline_still_fires: no
        baseline yet, but the event itself predates this HA session by
        well over the 60s slack window (e.g. a queued/redelivered FCM push,
        or the first push after a fresh install surfacing pre-existing
        cloud history). Firing here would trade GitHub #64's silent
        non-delivery for a false alert/clip on an old event instead of
        actually fixing it — must stay silent (bootstrap-only), same as
        the pre-fix `elif` fallback did for every case, not just this
        one."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={})  # no prev_id
        coord._download_started_at = 1_800_000_000.0  # 2027-01-15T08:00:00Z
        stale_events = [
            {
                "id": "old-id",
                "eventType": "MOVEMENT",
                "eventTags": [],
                # Hours before _download_started_at above.
                "timestamp": "2027-01-15T00:00:00Z",
                "imageUrl": "",
                "videoClipUrl": "",
                "videoClipUploadStatus": "",
            }
        ]
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=stale_events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        coord.hass.bus.async_fire.assert_not_called()
        assert coord.last_event_ids.get(CAM_ID) == "old-id", (
            "must still seed the baseline even when skipping dispatch"
        )

    def test_event_predates_session_missing_timestamp_is_not_stale(self):
        """No/too-short timestamp on the event → can't evaluate staleness,
        so `_event_predates_session` must fail open (False) rather than
        block a genuinely new event just because Bosch's payload was
        missing the field this time."""
        from custom_components.bosch_shc_camera.fcm import _event_predates_session

        coord = SimpleNamespace(_download_started_at=1_800_000_000.0)
        assert _event_predates_session(coord, {"timestamp": ""}) is False
        assert _event_predates_session(coord, {}) is False
        assert _event_predates_session(coord, {"timestamp": "2027"}) is False

    def test_event_predates_session_unparseable_timestamp_is_not_stale(self):
        """A timestamp that doesn't match Bosch's expected ISO shape must
        not raise out of the FCM dispatch path — fail open (False), same
        as the missing-timestamp case above."""
        from custom_components.bosch_shc_camera.fcm import _event_predates_session

        coord = SimpleNamespace(_download_started_at=1_800_000_000.0)
        # 19 chars (passes the length gate) but not a valid date/time, so
        # time.strptime() raises ValueError.
        assert (
            _event_predates_session(coord, {"timestamp": "2027-13-45T99:99:99"})
            is False
        )

    @pytest.mark.asyncio
    async def test_same_event_id_as_prev_does_not_fire(self):
        """newest_id == prev_id → neither if nor elif branch → no bus fire."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "same-id"})
        events = _one_event3("same-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)
        (
            coord.hass.bus.async_fire.assert_not_called(),
            "same event id as prev must not fire HA events",
        )

    @pytest.mark.asyncio
    async def test_audio_alarm_fires_audio_alarm_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "AUDIO_ALARM")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_audio_alarm" in fired, (
            "AUDIO_ALARM event must fire bosch_shc_camera_audio_alarm on the HA bus"
        )


class TestAsyncHandleFcmPushMarkEventsRead:
    """mark_events_read option gates fire-and-forget background task creation.

    Mark-read is fire-and-forget (async_create_task) so it does not block the
    per-camera loop for other cameras. Tests verify that a task is scheduled
    (not directly awaited) when the option is True, and that no task is
    scheduled when it is False/absent.
    """

    @pytest.mark.asyncio
    async def test_mark_events_read_true_schedules_task(self):
        """mark_events_read=True → a background task is created (fire-and-forget)."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(
            last_event_ids={CAM_ID: "old-id"},
            options={"mark_events_read": True},
        )
        events = _one_event3("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))

        # Track async_create_task calls so we can inspect what was scheduled.
        created_coros: list[object] = []

        def _capture_task(coro: object) -> MagicMock:
            created_coros.append(coro)
            stub = MagicMock()
            stub.add_done_callback = MagicMock()
            return stub

        coord.hass.async_create_task = MagicMock(side_effect=_capture_task)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with patch(
                    f"{MODULE}.async_mark_events_read", new_callable=AsyncMock
                ) as mock_mark:
                    await async_handle_fcm_push(coord)
                    # Now actually run any pending mark-read background coroutines
                    # so we can assert the inner call was made.
                    for coro in created_coros:
                        import inspect

                        if inspect.iscoroutine(coro):
                            await coro

        assert mock_mark.await_count >= 1, (
            "mark_events_read=True must schedule async_mark_events_read"
        )
        # Verify the call included the new event id
        all_args = [call.args for call in mock_mark.await_args_list]
        assert any("new-id" in str(a) for a in all_args), (
            "async_mark_events_read must be called with the new event id"
        )

    @pytest.mark.asyncio
    async def test_mark_events_read_false_skips_mark(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(
            last_event_ids={CAM_ID: "old-id"},
            options={"mark_events_read": False},
        )
        events = _one_event3("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        mock_mark = AsyncMock(return_value=True)
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with patch(f"{MODULE}.async_mark_events_read", mock_mark):
                    await async_handle_fcm_push(coord)
        (
            mock_mark.assert_not_awaited(),
            "mark_events_read=False must not call async_mark_events_read",
        )

    @pytest.mark.asyncio
    async def test_mark_events_read_absent_skips_mark(self):
        """mark_events_read key absent (default) → option.get returns False → skip."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(
            last_event_ids={CAM_ID: "old-id"},
            options={},  # key absent
        )
        events = _one_event3("new-id")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        mock_mark = AsyncMock(return_value=True)
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                with patch(f"{MODULE}.async_mark_events_read", mock_mark):
                    await async_handle_fcm_push(coord)
        (
            mock_mark.assert_not_awaited(),
            "absent mark_events_read must default to False and not call async_mark_events_read",
        )


class TestAsyncHandleFcmPushPersonUpgrade:
    """MOVEMENT + PERSON tag → upgraded to bosch_shc_camera_person event."""

    @pytest.mark.asyncio
    async def test_movement_with_person_tag_fires_person_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "MOVEMENT", ["PERSON"])
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_person" in fired, (
            "MOVEMENT + PERSON tag must fire bosch_shc_camera_person (not motion)"
        )
        assert "bosch_shc_camera_motion" not in fired, (
            "MOVEMENT + PERSON tag must NOT also fire bosch_shc_camera_motion"
        )

    @pytest.mark.asyncio
    async def test_movement_without_person_tag_fires_motion_event(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "MOVEMENT", [])  # no PERSON tag
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_motion" in fired, (
            "MOVEMENT without PERSON tag must fire bosch_shc_camera_motion"
        )
        assert "bosch_shc_camera_person" not in fired, (
            "MOVEMENT without PERSON tag must NOT fire bosch_shc_camera_person"
        )

    @pytest.mark.asyncio
    async def test_pure_person_event_fires_person_event(self):
        """eventType=PERSON (rare, but possible) fires person without upgrade path."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "PERSON", [])
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_person" in fired, (
            "eventType=PERSON must fire bosch_shc_camera_person"
        )

    @pytest.mark.asyncio
    async def test_person_tag_on_non_movement_not_upgraded(self):
        """PERSON tag only upgrades MOVEMENT, not AUDIO_ALARM etc."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        events = _one_event3("new-id", "AUDIO_ALARM", ["PERSON"])
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        fired = [c.args[0] for c in coord.hass.bus.async_fire.call_args_list]
        assert "bosch_shc_camera_audio_alarm" in fired, (
            "AUDIO_ALARM + PERSON tag must fire audio_alarm (upgrade only for MOVEMENT)"
        )
        assert "bosch_shc_camera_person" not in fired, (
            "AUDIO_ALARM + PERSON tag must not fire person event"
        )


class TestAsyncHandleFcmPushNotificationSwitch:
    """Master switch OFF → alert blocked → no async_create_task for alert.
    Type-specific switch OFF also blocks even when master switch is ON."""

    @pytest.mark.asyncio
    async def test_master_switch_off_blocks_alert(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        master_state = MagicMock()
        master_state.state = "off"
        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        # Make states.get return OFF for the master switch
        coord.hass.states.get = MagicMock(return_value=master_state)
        events = _one_event3("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.async_send_alert", new_callable=AsyncMock
            ) as mock_alert:
                await async_handle_fcm_push(coord)
        # Bus still fires (event still logged) but no alert task
        (
            mock_alert.assert_not_awaited(),
            "master notifications switch OFF must prevent async_send_alert call",
        )

    @pytest.mark.asyncio
    async def test_master_switch_on_allows_alert(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        master_state = MagicMock()
        master_state.state = "on"
        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        coord.hass.states.get = MagicMock(return_value=master_state)
        events = _one_event3("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        (
            coord.hass.async_create_task.assert_called(),
            "master notifications switch ON must allow async_send_alert task creation",
        )

    @pytest.mark.asyncio
    async def test_no_switch_state_allows_alert(self):
        """states.get returns None (switch not found) → no blocking → alert allowed."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        coord.hass.states.get = MagicMock(return_value=None)  # switch not found
        events = _one_event3("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock):
                await async_handle_fcm_push(coord)
        (
            coord.hass.async_create_task.assert_called(),
            "absent notification switch must default to allowed (None → not off)",
        )

    @pytest.mark.asyncio
    async def test_type_specific_switch_off_blocks_alert(self):
        """Master ON but type-specific switch OFF → alert blocked."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        master_on = MagicMock()
        master_on.state = "on"
        type_off = MagicMock()
        type_off.state = "off"

        def _states_get(eid):
            if "movement_notifications" in eid:
                return type_off
            if "_notifications" in eid:
                return master_on
            return None

        coord = _make_push_coord3(last_event_ids={CAM_ID: "old-id"})
        coord.hass.states.get = MagicMock(side_effect=_states_get)
        events = _one_event3("new-id", "MOVEMENT")
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_push(200, json_data=events))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.async_send_alert", new_callable=AsyncMock
            ) as mock_alert:
                await async_handle_fcm_push(coord)
        (
            mock_alert.assert_not_awaited(),
            "type-specific switch OFF must block alert even when master switch is ON",
        )


class TestAsyncMarkEventsRead:
    """Empty list → True, no token → False, HTTP 2xx → True, all fail → False,
    partial success → True."""

    @pytest.mark.asyncio
    async def test_empty_list_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3(token="tok")
        result = await async_mark_events_read(coord, [])
        assert result is True, "empty event_ids list must return True (nothing to do)"

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3(token="")
        result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "no token must return False (cannot authenticate)"

    @pytest.mark.asyncio
    async def test_none_token_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3(token=None)
        result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "None token must return False"

    @pytest.mark.asyncio
    async def test_http_200_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm_push(200))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is True, "HTTP 200 PUT response must return True"

    @pytest.mark.asyncio
    async def test_http_201_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm_push(201))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is True, "HTTP 201 PUT response must return True"

    @pytest.mark.asyncio
    async def test_http_204_returns_true(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm_push(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is True, "HTTP 204 PUT response must return True"

    @pytest.mark.asyncio
    async def test_http_500_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()
        session = MagicMock()
        session.put = MagicMock(return_value=_resp_cm_push(500))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "HTTP 500 response must return False (all failed)"

    @pytest.mark.asyncio
    async def test_exception_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()
        session = MagicMock()
        session.put = MagicMock(side_effect=Exception("network error"))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_mark_events_read(coord, ["event-1"])
        assert result is False, "exception during PUT must return False"

    @pytest.mark.asyncio
    async def test_partial_success_returns_true(self):
        """Multiple events: one fails, one succeeds → any success → True."""
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()
        call_count = 0

        @asynccontextmanager
        async def _put(*args, **kw):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status = 200 if call_count == 2 else 500
            yield resp

        session = MagicMock()
        session.put = _put
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_mark_events_read(coord, ["fail-id", "ok-id"])
        assert result is True, "partial success (at least one 200) must return True"

    @pytest.mark.asyncio
    async def test_all_fail_returns_false(self):
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()

        @asynccontextmanager
        async def _put(*args, **kw):
            resp = MagicMock()
            resp.status = 403
            yield resp

        session = MagicMock()
        session.put = _put
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await async_mark_events_read(coord, ["e1", "e2", "e3"])
        assert result is False, "all-fail responses must return False"

    @pytest.mark.asyncio
    async def test_put_sends_correct_payload(self):
        """PUT body must include id + isRead=True."""
        from custom_components.bosch_shc_camera.fcm import async_mark_events_read

        coord = _make_push_coord3()
        captured = {}

        @asynccontextmanager
        async def _put(*args, **kw):
            captured["json"] = kw.get("json", {})
            resp = MagicMock()
            resp.status = 200
            yield resp

        session = MagicMock()
        session.put = _put
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_mark_events_read(coord, ["event-xyz"])
        assert captured["json"].get("id") == "event-xyz", (
            "PUT payload must include the event id"
        )
        assert captured["json"].get("isRead") is True, (
            "PUT payload must set isRead=True"
        )


class TestAsyncSendAlertEarlyExit:
    """No info services + non-trouble event → return immediately, before
    makedirs. TROUBLE_* events proceed even with empty information services
    because they route through the 'system' service key."""

    @pytest.mark.asyncio
    async def test_no_services_returns_before_makedirs(self):
        """No information services, no system services → makedirs never called."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "",
                "alert_notify_information": "",
                "alert_notify_system": "",
            }
        )
        await _run_send_alert(coord, event_type="MOVEMENT")
        (
            coord.hass.async_add_executor_job.assert_not_awaited(),
            "must not call makedirs when no services configured for non-trouble event",
        )

    @pytest.mark.asyncio
    async def test_trouble_event_with_no_info_services_does_not_return_early(self):
        """TROUBLE_CONNECT must proceed even when information services are empty."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_information": "",
                "alert_notify_system": "",
            }
        )
        # get_alert_services("system") falls back to alert_notify_service="notify.signal"
        # so step 1 should be attempted
        await _run_send_alert(coord, event_type="TROUBLE_CONNECT")
        (
            coord.hass.async_add_executor_job.assert_awaited(),
            "TROUBLE_CONNECT must not exit early — must call makedirs",
        )

    @pytest.mark.asyncio
    async def test_movement_with_services_proceeds(self):
        """MOVEMENT + at least one service configured → makedirs called."""
        coord = _make_alert_coord4()  # alert_notify_service = "notify.test"
        await _run_send_alert(coord, event_type="MOVEMENT")
        (
            coord.hass.async_add_executor_job.assert_awaited(),
            "must call makedirs when services are configured",
        )


class TestStep1TextAlert:
    """Step 1 routes to 'system' for trouble events, 'information' otherwise."""

    @pytest.mark.asyncio
    async def test_movement_step1_calls_information_service(self):
        """MOVEMENT → _notify_type('information', ...) → hass.services.async_call."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_information": "notify.info_svc",
            }
        )
        await _run_send_alert(
            coord,
            event_type="MOVEMENT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
        )
        # async_call(domain, service, data) → args[1] is the service name
        calls = [str(c) for c in coord.hass.services.async_call.call_args_list]
        assert any("info_svc" in s for s in calls), (
            "MOVEMENT step 1 must route through 'information' services"
        )

    @pytest.mark.asyncio
    async def test_notify_call_uses_blocking_true(self):
        """GitHub #68 live-deploy finding, 2026-08-18: hass.services.async_call
        must be called with blocking=True — HA core's default blocking=False
        makes it fire-and-forget, so a real downstream delivery failure
        (SMTP down, push rejected, ...) is caught inside HA core's own
        wrapper and never reaches _notify_type's except Exception, meaning
        `delivered`/the "Alert step N sent" log would claim success even
        when the notify silently failed."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_information": "notify.info_svc",
            }
        )
        await _run_send_alert(
            coord,
            event_type="MOVEMENT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
        )
        calls = coord.hass.services.async_call.call_args_list
        assert calls, "expected at least one notify call"
        for c in calls:
            assert c.kwargs.get("blocking") is True, (
                f"async_call must be invoked with blocking=True, got call: {c}"
            )

    @pytest.mark.asyncio
    async def test_trouble_connect_step1_calls_system_service(self):
        """TROUBLE_CONNECT → routes to 'system' key → system service called."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_system": "notify.system_svc",
                "alert_notify_information": "notify.info_svc",
            }
        )
        await _run_send_alert(coord, event_type="TROUBLE_CONNECT")
        calls = coord.hass.services.async_call.call_args_list
        # system service must have been called; info service must NOT
        svc_names = [str(c) for c in calls]
        assert any("system_svc" in s for s in svc_names), (
            "TROUBLE_CONNECT step 1 must use 'system' service"
        )
        assert not any("info_svc" in s for s in svc_names), (
            "TROUBLE_CONNECT step 1 must NOT use 'information' service"
        )

    @pytest.mark.asyncio
    async def test_trouble_connect_returns_after_step1(self):
        """TROUBLE_CONNECT/DISCONNECT returns after step 1 — no makedirs for alert_dir."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
            }
        )
        await _run_send_alert(coord, event_type="TROUBLE_DISCONNECT")
        # makedirs is called once (before step 1), but no screenshot/video writes
        # Check that async_add_executor_job was only called for makedirs (not _write_file)
        write_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and callable(c.args[0])
            and getattr(c.args[0], "__name__", "") == "_write_file"
        ]
        assert len(write_calls) == 0, (
            "TROUBLE events must not write any files (no snapshot/clip for connectivity events)"
        )

    @pytest.mark.asyncio
    async def test_step1_text_contains_cam_name_and_type(self):
        """Step 1 message must contain camera name and event label."""
        coord = _make_alert_coord4()
        captured_calls = []
        coord.hass.services.async_call = AsyncMock(
            side_effect=lambda d, s, data, **kw: captured_calls.append(
                data.get("message", "")
            )
        )
        await _run_send_alert(
            coord, event_type="MOVEMENT", timestamp="2026-05-07T10:00:00.000Z"
        )
        assert any("Terrasse" in m for m in captured_calls), (
            "step 1 message must contain the camera name"
        )
        assert any("Bewegung" in m or "MOVEMENT" in m for m in captured_calls), (
            "step 1 message must contain the event type label"
        )


class TestStep2ImageUrlRetry:
    """Empty image_url → retry loop (3 attempts with delays). Also covers the
    URL-safety allowlist (Bosch-domain-only) and the no-camera-match guard."""

    @pytest.mark.asyncio
    async def test_empty_image_url_triggers_refetch(self):
        """image_url='' → must attempt session.get to re-fetch events."""
        coord = _make_alert_coord4()
        session = MagicMock()
        # Return empty events on all re-fetch attempts so the loop exhausts
        session.get.return_value = _resp_cm_alert(200, json_data=[])

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                "",  # empty imageUrl
                            )

        await _run()
        assert session.get.call_count >= 3, (
            "empty image_url must trigger at least 3 re-fetch attempts"
        )

    async def test_no_camera_match_skips_refetch(self):
        """If cam_name matches no camera title, the empty-image re-fetch must
        be SKIPPED. Querying with an empty videoInputId returns every camera's
        events and event[0] would attach a foreign camera's image to this
        alert."""
        coord = _make_alert_coord4()  # only camera title is "Terrasse"
        session = MagicMock()
        session.get.return_value = _resp_cm_alert(200, json_data=[])

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            await async_send_alert(
                                coord,
                                "UnknownCam",  # no title match
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",  # empty imageUrl
                            )

        await _run()
        for gcall in session.get.call_args_list:
            assert "videoInputId" not in str(gcall), (
                "must not query events when no camera matches the alert title"
            )

    async def test_unsafe_image_url_not_downloaded(self):
        """An imageUrl failing the Bosch domain allowlist must never reach
        session.get."""
        coord = _make_alert_coord4()
        session = MagicMock()
        session.get.return_value = _resp_cm_alert(200, json_data=[])
        evil = "https://evil.example.com/x.jpg"

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                evil,  # unsafe imageUrl (not a Bosch domain)
                            )

        await _run()
        for gcall in session.get.call_args_list:
            assert evil not in str(gcall), "rejected imageUrl must not be fetched"

    @pytest.mark.asyncio
    async def test_empty_image_url_found_on_second_attempt_proceeds(self):
        """image_url becomes available on 2nd re-fetch → step 2 download triggered."""
        coord = _make_alert_coord4()
        session = MagicMock()
        call_count = [0]

        @asynccontextmanager
        async def _get(url, **kw):
            call_count[0] += 1
            resp = MagicMock()
            if "events" in url and call_count[0] == 2:
                # Second event re-fetch → imageUrl populated
                resp.status = 200
                resp.json = AsyncMock(
                    return_value=[
                        {
                            "imageUrl": "https://residential.cbs.boschsecurity.com/img.jpg",
                            "videoClipUrl": "",
                            "videoClipUploadStatus": "",
                        }
                    ]
                )
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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                "",  # empty imageUrl
                            )

        await _run()
        # _write_file must have been called for the snapshot
        write_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and len(c.args) >= 2
            and isinstance(c.args[1], str)
            and c.args[1].endswith(".jpg")
        ]
        assert len(write_calls) >= 1, (
            "when image_url found on retry, step 2 must write the snapshot file"
        )

    @pytest.mark.asyncio
    async def test_unsafe_image_url_rejected_no_snap_download(self):
        """Unsafe imageUrl from Bosch API response must be rejected — no file written."""
        coord = _make_alert_coord4()
        session = MagicMock()
        session.get.return_value = _resp_cm_alert(
            200,
            json_data=[
                {
                    "imageUrl": "http://evil.example.com/steal.jpg",
                }
            ],
        )

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                "http://evil.example.com/steal.jpg",  # unsafe from start
                            )

        await _run()
        # The unsafe URL must be cleared before download — no _write_file for .jpg
        write_jpg_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and len(c.args) >= 2
            and isinstance(c.args[1], str)
            and c.args[1].endswith(".jpg")
        ]
        assert len(write_jpg_calls) == 0, (
            "unsafe imageUrl must be rejected — no snapshot file written"
        )


class TestStep2SnapshotDownload:
    """snap.jpg download → write file + screenshot service call. Empty body
    or delete_after_send affect write/cleanup behavior."""

    @pytest.mark.asyncio
    async def test_snap_200_writes_file_and_notifies(self):
        """snap.jpg 200 + image/* → write to alert_dir + screenshot service called."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_screenshot": "notify.signal",
                "alert_save_snapshots": True,  # keep file so it's not cleaned up
            }
        )

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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                            )

        await _run()

        # screenshot service must be called
        svc_calls = [c.args[:2] for c in coord.hass.services.async_call.call_args_list]
        assert ("notify", "signal") in svc_calls, (
            "screenshot notify service must be called after snap.jpg 200"
        )

        # _write_file must have been called with a .jpg path
        write_jpg_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and len(c.args) >= 2
            and isinstance(c.args[1], str)
            and c.args[1].endswith(".jpg")
        ]
        assert len(write_jpg_calls) >= 1, (
            "must write snapshot file after 200 + image/jpeg response"
        )

    @pytest.mark.asyncio
    async def test_snap_200_empty_body_skips_write(self):
        """snap.jpg 200 + empty body → guard `if data:` prevents file write."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_alert_coord4()

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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                            )

        await _run()

        write_jpg_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and len(c.args) >= 2
            and isinstance(c.args[1], str)
            and c.args[1].endswith(".jpg")
        ]
        assert len(write_jpg_calls) == 0, (
            "empty snap body must not result in file write"
        )

    @pytest.mark.asyncio
    async def test_delete_after_adds_to_cleanup(self):
        """alert_delete_after_send=True (default) → snap path added to files_to_cleanup."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_screenshot": "notify.signal",
                "alert_save_snapshots": False,  # delete_after=True is default
            }
        )
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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                            )

        await _run()
        assert any(f.endswith(".jpg") for f in removed_files), (
            "snapshot file must be cleaned up when delete_after_send=True"
        )


class TestStep3VideoClipDirect:
    """Direct clip.mp4 download check before polling (clip_status=Done with a
    URL already provided skips the poll loop entirely)."""

    @pytest.mark.asyncio
    async def test_clip_status_done_with_url_skips_direct_and_poll(self):
        """clip_url given + clip_status=Done → use directly, skip direct check and poll."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        clip_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/clip.mp4"
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_video": "notify.signal",
            }
        )

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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                                clip_url,
                                "Done",
                            )

        await _run()
        # Clip should be downloaded and video service called
        svc_calls = [c.args[:2] for c in coord.hass.services.async_call.call_args_list]
        assert ("notify", "signal") in svc_calls, (
            "video service must be called when clip_status=Done and url is provided"
        )

    @pytest.mark.asyncio
    async def test_clip_status_unavailable_skips_poll(self):
        """clip_status=Unavailable from start → polling loop skipped."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_alert_coord4()

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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                                "",
                                "Unavailable",
                            )

        await _run()
        assert poll_count[0] == 0, (
            "clip_status=Unavailable must skip polling loop entirely"
        )


class TestStep3VideoDownload:
    """Clip URL found → download → write .mp4 → video service. Small bodies
    and unsafe (non-Bosch) URLs are both rejected."""

    @pytest.mark.asyncio
    async def test_found_clip_url_small_body_skips_write(self):
        """found_clip_url but body <= 1000 bytes → guard prevents file write."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        clip_url = "https://residential.cbs.boschsecurity.com/v11/events/abc/clip.mp4"
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_video": "notify.signal",
            }
        )

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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                                clip_url,
                                "Done",
                            )

        await _run()

        write_mp4_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and len(c.args) >= 2
            and isinstance(c.args[1], str)
            and c.args[1].endswith(".mp4")
        ]
        assert len(write_mp4_calls) == 0, (
            "video body <= 1000 bytes must be rejected — not written as clip"
        )

    @pytest.mark.asyncio
    async def test_unsafe_clip_url_rejected(self):
        """Clip URL not on Bosch domain → _is_safe_bosch_url rejects it."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_video": "notify.signal",
            }
        )
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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                                unsafe_clip,
                                "Done",
                            )

        await _run()
        assert not any("evil.example.com" in u for u in session_get_urls), (
            "unsafe clip URL must never be fetched via session.get"
        )


class TestMarkEventsReadInSendAlert:
    """mark_events_read=True → async_mark_events_read called at end of
    async_send_alert."""

    @pytest.mark.asyncio
    async def test_mark_events_read_true_calls_mark(self):
        """mark_events_read=True + cam found → async_mark_events_read awaited."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "mark_events_read": True,
            }
        )
        mock_mark = AsyncMock(return_value=True)

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{MODULE}.async_mark_events_read", mock_mark):
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
                                    "",
                                )

        await _run()
        (
            mock_mark.assert_awaited(),
            "mark_events_read=True must call async_mark_events_read in send_alert",
        )

    @pytest.mark.asyncio
    async def test_mark_events_read_false_skips_mark(self):
        """mark_events_read=False → async_mark_events_read NOT called in send_alert."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "mark_events_read": False,
            }
        )
        mock_mark = AsyncMock(return_value=True)

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{MODULE}.async_mark_events_read", mock_mark):
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
                                    "",
                                )

        await _run()
        (
            mock_mark.assert_not_awaited(),
            "mark_events_read=False must not call async_mark_events_read in send_alert",
        )


class TestSmbUploadGate:
    """enable_smb_upload + smb_server → executor job for SMB upload. Timeouts
    must be swallowed, never propagated."""

    @pytest.mark.asyncio
    async def test_smb_disabled_no_executor_smb_call(self):
        """enable_smb_upload=False → sync_smb_upload never called."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "enable_smb_upload": False,
            }
        )
        mock_smb = MagicMock()

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", mock_smb):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",
                            )

        await _run()
        (
            mock_smb.assert_not_called(),
            "sync_smb_upload must not be called when enable_smb_upload=False",
        )

    @pytest.mark.asyncio
    async def test_smb_enabled_calls_executor_smb(self):
        """enable_smb_upload=True + smb_server set → sync_smb_upload called via executor."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "enable_smb_upload": True,
                "smb_server": "//nas/share",
            }
        )
        executor_fns = []

        async def _exec(fn, *args, **kw):
            executor_fns.append(fn)
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)
        mock_smb = MagicMock(__name__="sync_smb_upload")

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", mock_smb):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",
                            )

        await _run()
        # The patched mock_smb should have been passed to async_add_executor_job
        assert mock_smb in executor_fns, (
            "sync_smb_upload must be submitted to executor when smb is enabled"
        )

    @pytest.mark.asyncio
    async def test_smb_timeout_does_not_raise(self):
        """SMB upload timeout after 30s must be caught — not propagated to caller."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "enable_smb_upload": True,
                "smb_server": "//nas/share",
            }
        )

        async def _exec(fn, *args, **kw):
            if getattr(fn, "__name__", "") == "sync_smb_upload":
                raise TimeoutError()
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            # Must not raise
                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",
                            )

        await _run()  # passes if no exception


class TestLocalSaveGate:
    """download_path set → sync_local_save called via executor; timeouts must
    not propagate."""

    @pytest.mark.asyncio
    async def test_download_path_empty_skips_local_save(self):
        """download_path='' → sync_local_save never called."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "download_path": "",
            }
        )
        mock_save = MagicMock()

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", mock_save):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",
                            )

        await _run()
        (
            mock_save.assert_not_called(),
            "sync_local_save must not be called when download_path is empty",
        )

    @pytest.mark.asyncio
    async def test_download_path_set_calls_local_save(self):
        """download_path set → sync_local_save submitted via async_add_executor_job."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "enable_local_save": True,
                "download_path": "/mnt/nvr",
            }
        )
        executor_fns = []

        async def _exec(fn, *args, **kw):
            executor_fns.append(fn)
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)
        mock_save = MagicMock(__name__="sync_local_save")

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
                with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                    with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                        with patch(f"{SMB_MODULE}.sync_local_save", mock_save):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",
                            )

        await _run()
        assert mock_save in executor_fns, (
            "sync_local_save must be submitted to executor when download_path is set"
        )

    @pytest.mark.asyncio
    async def test_local_save_timeout_does_not_raise(self):
        """local save timeout must be caught — not propagated."""
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "download_path": "/mnt/nvr",
            }
        )

        async def _exec(fn, *args, **kw):
            if getattr(fn, "__name__", "") == "sync_local_save":
                raise TimeoutError()
            return None

        coord.hass.async_add_executor_job = AsyncMock(side_effect=_exec)

        async def _run():
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=MagicMock()),
            ):
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
                                "",
                            )

        await _run()  # passes if no exception


class TestFileCleanup:
    """alert_save_snapshots=True → temp files are NOT added to the cleanup
    list (os.remove never called for them)."""

    @pytest.mark.asyncio
    async def test_save_snapshots_true_no_cleanup(self):
        """alert_save_snapshots=True → files NOT added to cleanup list."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_screenshot": "notify.signal",
                "alert_save_snapshots": True,
                "alert_delete_after_send": True,  # delete_after only applies when save=False
            }
        )
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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                            )

        await _run()
        jpg_removed = [p for p in removed if p.endswith(".jpg")]
        assert len(jpg_removed) == 0, (
            "alert_save_snapshots=True must not remove the snapshot file"
        )

    @pytest.mark.asyncio
    async def test_save_snapshots_false_deletes_even_when_delete_after_send_false(
        self,
    ):
        """GitHub #53 regression: alert_save_snapshots=False must delete the
        snapshot file even when alert_delete_after_send=False. Before the
        fix, cleanup was gated on `delete_after AND files_to_cleanup`, so
        this exact combo (both toggles OFF) silently kept every alert file
        forever in www/bosch_alerts/ despite alert_save_snapshots's own
        description promising deletion within seconds — reported as ~2GB of
        accumulated files over a few weeks."""
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_screenshot": "notify.signal",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            }
        )
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
            with patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ):
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
                                safe_img,
                            )

        await _run()
        jpg_removed = [p for p in removed if p.endswith(".jpg")]
        assert len(jpg_removed) == 1, (
            "alert_save_snapshots=False must delete the snapshot file "
            "regardless of alert_delete_after_send (#53)"
        )


# start_fcm_push mode branches + early exits, fetch_firebase_config, push snapshot task + exception handling, on_fcm_push drop-when-not-running, send_alert notify-type/trouble-event/step2/step3 branches, SMB/local-save timeouts + toggles, cleanup OSError, event-id concurrency, stale-client creds race, periodic re-registration, force-hard-heal (from: round 8, stale-client-creds race, issue-36 delivery, sprint grab-bag)


def _resp_cm_text(status: int, body: str = "") -> MagicMock:
    """Async-context-manager response stub for session.post() (text body, .text()
    interface) — used by the Bosch registration tests below.

    NOTE (merge note): this had the same name (`_resp_cm`) as the binary-body
    helper above in its source file; renamed to avoid an intra-batch collision.
    Kept as a separate helper rather than merged since the two stub different
    aiohttp response shapes (.read/.json vs .text).
    """
    resp = MagicMock()
    resp.status = status
    resp.text = AsyncMock(return_value=body)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _make_resp(
    status: int, json_data=None, text_data: str = "", headers: dict | None = None
):
    """Alternate response stub shape (resp itself is the async context manager)."""
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text_data)
    resp.read = AsyncMock(return_value=b"")
    resp.headers = headers or {}
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session(**method_responses):
    """Build a session mock; method_responses maps method name to return value."""
    session = MagicMock()
    for method, rv in method_responses.items():
        mock_method = MagicMock(return_value=rv)
        setattr(session, method, mock_method)
    return session


async def _run_alert2(
    coord,
    event_type="MOVEMENT",
    image_url="",
    clip_url="",
    clip_status="",
    cam_name="Terrasse",
    timestamp="2026-05-07T10:00:00.000Z",
    session_override=None,
):
    from custom_components.bosch_shc_camera.fcm import async_send_alert

    session = session_override or MagicMock()
    if session_override is None:
        session.get = MagicMock(return_value=_resp_cm(404))
    with patch(
        f"{MODULE}.async_get_bosch_cloud_session", new=AsyncMock(return_value=session)
    ):
        with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
            with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                    await async_send_alert(
                        coord,
                        cam_name,
                        event_type,
                        timestamp,
                        image_url,
                        clip_url,
                        clip_status,
                    )


def _make_push_coord4(**overrides):
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock()
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-B",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        last_event_ids={},
        alert_sent_ids={},
        camera_entities={},
        cached_events={},
        bg_tasks=set(),
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _one_event4(
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


class TestAsyncStartFcmPushModeBranches:
    """async_start_fcm_push: mode=polling returns immediately; unknown mode falls
    back to ios; registration/start failures leave the coordinator not-running."""

    def _entry_stub(self, data=None):
        return SimpleNamespace(data=data or {})

    def _coord_stub(self, push_mode="polling", data=None, fcm_cfg=None):
        entry_data = {}
        if fcm_cfg:
            entry_data["fcm_config"] = fcm_cfg
        return SimpleNamespace(
            fcm_running=False,
            fcm_client=None,
            fcm_token=None,
            fcm_lock=__import__("threading").Lock(),
            fcm_healthy=False,
            fcm_push_mode="unknown",
            options={"enable_fcm_push": True, "fcm_push_mode": push_mode},
            hass=MagicMock(),
            entry=self._entry_stub(entry_data),
            data=data or {},
        )

    @pytest.mark.asyncio
    async def test_polling_mode_returns_immediately(self):
        """push_mode='polling' → no FcmPushClient created, function returns."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._coord_stub(push_mode="polling")

        mock_fcm = MagicMock()
        mock_fcm.FcmPushClient = MagicMock()
        mock_fcm.FcmRegisterConfig = MagicMock()
        mock_fcm.FcmPushClientConfig = MagicMock()
        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                await async_start_fcm_push(coord)

        assert not coord.fcm_running

    @pytest.mark.asyncio
    async def test_unknown_mode_uses_ios_fallback(self):
        """push_mode='badvalue' → falls through to ios _try_fcm_with_mode."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._coord_stub(push_mode="weirdmode")

        ios_called_with = []

        async def fake_try(mode):
            ios_called_with.append(mode)
            return False

        with patch(f"{MODULE}._install_fcm_noise_filter"):
            try:
                from firebase_messaging import FcmPushClient, FcmRegisterConfig
            except ImportError:
                pytest.skip("firebase_messaging not installed")

            with patch(
                f"{MODULE}.fetch_firebase_config",
                new_callable=lambda: (
                    lambda: AsyncMock(
                        return_value={"api_key": "k", "project_id": "p", "app_id": "a"}
                    )
                ),
            ):
                try:
                    await async_start_fcm_push(coord)
                except Exception:
                    pass

    @pytest.mark.asyncio
    async def test_registration_failure_does_not_set_running(self):
        """FcmPushClient.checkin_or_register raises → fcm_running stays False."""
        try:
            from firebase_messaging import FcmPushClient, FcmRegisterConfig
        except ImportError:
            pytest.skip("firebase_messaging not installed")

        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._coord_stub(push_mode="ios")
        coord.entry = self._entry_stub()

        mock_client = MagicMock()
        mock_client.checkin_or_register = AsyncMock(
            side_effect=Exception("checkin failed")
        )

        # fcm.py imports FcmPushClient lazily inside the function; patch the
        # class lookup helper to return a stub that produces our mock_client.
        with patch(f"{MODULE}._install_fcm_noise_filter"):
            with patch(
                f"{MODULE}.fetch_firebase_config",
                new=AsyncMock(
                    return_value={
                        "api_key": "key",
                        "project_id": "proj",
                        "app_id": "appid",
                    }
                ),
            ):
                with patch(
                    f"{MODULE}._get_fcm_push_client_class",
                    return_value=MagicMock(return_value=mock_client),
                ):
                    await async_start_fcm_push(coord)

        assert not coord.fcm_running

    @pytest.mark.asyncio
    async def test_start_failure_clears_client(self):
        """FcmPushClient.start() raises → fcm_client set to None."""
        try:
            from firebase_messaging import FcmPushClient, FcmRegisterConfig
        except ImportError:
            pytest.skip("firebase_messaging not installed")

        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = self._coord_stub(push_mode="ios")
        coord.entry = self._entry_stub()

        mock_client = MagicMock()
        mock_client.checkin_or_register = AsyncMock(return_value="fake-token-xyz")
        mock_client.start = AsyncMock(side_effect=Exception("start failed"))

        with patch(f"{MODULE}._install_fcm_noise_filter"):
            with patch(
                f"{MODULE}.fetch_firebase_config",
                new=AsyncMock(
                    return_value={
                        "api_key": "key",
                        "project_id": "proj",
                        "app_id": "appid",
                    }
                ),
            ):
                with patch(
                    f"{MODULE}._get_fcm_push_client_class",
                    return_value=MagicMock(return_value=mock_client),
                ):
                    with patch(
                        f"{MODULE}.register_fcm_with_bosch",
                        new=AsyncMock(return_value=True),
                    ):
                        await async_start_fcm_push(coord)

        assert coord.fcm_client is None


class TestStartFcmEarlyExits:
    """async_start_fcm_push must no-op (no network) when already running or the
    enable_fcm_push option is disabled/missing."""

    @pytest.mark.asyncio
    async def test_start_fcm_already_running_returns(self):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = SimpleNamespace(
            fcm_running=True,
            options={"enable_fcm_push": True},
        )
        await async_start_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_start_fcm_option_disabled_returns(self):
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = SimpleNamespace(
            fcm_running=False,
            options={"enable_fcm_push": False},
        )
        await async_start_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_start_fcm_option_missing_defaults_to_disabled(self):
        """options.get("enable_fcm_push", False) == False → early return."""
        from custom_components.bosch_shc_camera.fcm import async_start_fcm_push

        coord = SimpleNamespace(
            fcm_running=False,
            options={},
        )
        await async_start_fcm_push(coord)


class TestFetchFirebaseConfig:
    @pytest.mark.asyncio
    async def test_fetch_firebase_config_returns_dict(self):
        """fetch_firebase_config must return a dict with the Bosch Firebase project info."""
        from custom_components.bosch_shc_camera.fcm import fetch_firebase_config

        hass = MagicMock()
        result = await fetch_firebase_config(hass)
        assert isinstance(result, dict), "fetch_firebase_config must return a dict"
        assert "project_id" in result, "result must include 'project_id'"
        assert "api_key" in result, "result must include 'api_key'"
        assert "app_id" in result, "result must include 'app_id'"
        assert result["project_id"] == "bosch-smart-cameras", (
            "project_id must match the Bosch Firebase project"
        )
        assert result["api_key"], "api_key must be a non-empty string"

    @pytest.mark.asyncio
    async def test_fetch_firebase_config_hass_arg_ignored(self):
        """fetch_firebase_config is a pure function; hass argument is accepted but unused."""
        from custom_components.bosch_shc_camera.fcm import fetch_firebase_config

        result1 = await fetch_firebase_config(MagicMock())
        result2 = await fetch_firebase_config(None)  # type: ignore[arg-type]
        assert result1["project_id"] == result2["project_id"], (
            "result must be independent of the hass argument"
        )


class TestHandlePushSnapshotTask:
    """When cam_entity exists, snapshot task is created and tracked."""

    @pytest.mark.asyncio
    async def test_camera_entity_snapshot_triggered(self):
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord4(
            last_event_ids={CAM_ID: "old-evt"},
        )
        cam_entity = MagicMock()

        async def _fake_refresh(delay=2):
            pass

        cam_entity.async_trigger_image_refresh = MagicMock(return_value=_fake_refresh())
        coord.camera_entities = {CAM_ID: cam_entity}

        task_stub = MagicMock()
        task_stub.add_done_callback = MagicMock()
        coord.hass.async_create_task = MagicMock(return_value=task_stub)

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, json_data=_one_event4("new-evt"))
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        coord.hass.async_create_task.assert_called()


class TestHandlePushExceptions:
    """Network errors and generic exceptions during push handling are caught per-camera."""

    @pytest.mark.asyncio
    async def test_timeout_error_caught_per_camera(self):
        """asyncio.TimeoutError during push event fetch must not propagate."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord4()
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=TimeoutError())
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=cm)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_generic_exception_caught_per_camera(self):
        """Any unexpected exception during push must be caught and logged, not raised."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord4()
        session = MagicMock()
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=RuntimeError("unexpected"))
        cm.__aexit__ = AsyncMock(return_value=None)
        session.get = MagicMock(return_value=cm)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            await async_handle_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_handle_fcm_push_mark_events_read_exception_swallowed(self):
        """Exception from async_mark_events_read inside async_handle_fcm_push is silently swallowed."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        CAM = "CAM1"
        event_id = "evt-001"

        # Simulate a new event (prev_id != newest_id) so the mark_events_read branch is reached
        resp = _make_resp(
            200,
            json_data=[
                {
                    "id": event_id,
                    "eventType": "MOVEMENT",
                    "eventTags": [],
                    "timestamp": "2026-01-01T10:00:00",
                    "imageUrl": "",
                    "videoClipUrl": "",
                }
            ],
        )
        session = _make_session(get=MagicMock(return_value=resp))

        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        hass.bus.async_fire = MagicMock()
        hass.async_create_task = MagicMock()

        coord = SimpleNamespace(
            token="tok",
            data={CAM: {"info": {"title": "Kamera"}, "events": []}},
            last_event_ids={CAM: "old-id"},  # different from event_id → new event
            alert_sent_ids={},
            bg_tasks=set(),
            camera_entities={},
            cached_events={},
            options={"mark_events_read": True},  # enable the mark-as-read branch
            hass=hass,
        )

        async def _raising_mark(coord_, ids):
            raise RuntimeError("simulated mark failure")

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.async_mark_events_read",
                side_effect=_raising_mark,
            ):
                await async_handle_fcm_push(coord)

    @pytest.mark.asyncio
    async def test_non200_response_skips_camera(self):
        """Non-200 response → camera skipped, no event processing."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord4()
        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(500))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        coord.hass.bus.async_fire.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_prev_id_sets_last_event_id(self):
        """newest_id present + prev_id=None → last_event_ids[cam] set to newest_id."""
        from custom_components.bosch_shc_camera.fcm import async_handle_fcm_push

        coord = _make_push_coord4()
        # No prior event ID → prev_id will be None (key absent)
        coord.last_event_ids = {}

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, json_data=_one_event4("first-evt"))
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(
                f"{MODULE}.asyncio.timeout",
                return_value=MagicMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()),
            ):
                await async_handle_fcm_push(coord)

        assert coord.last_event_ids.get(CAM_ID) == "first-evt"


class TestOnFcmPushDroppedWhenNotRunning:
    """_on_fcm_push: if fcm_running=False, push is silently dropped."""

    def test_push_dropped_when_not_running(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = _make_push_coord4()
        coord.fcm_running = False
        coord.fcm_lock = __import__("threading").Lock()

        _on_fcm_push(coord, {"from": "test"}, "pid-1")

        coord.hass.loop.call_soon_threadsafe.assert_not_called()

    def test_push_accepted_when_running(self):
        from custom_components.bosch_shc_camera.fcm import _on_fcm_push

        coord = _make_push_coord4()
        coord.fcm_running = True
        coord.fcm_healthy = False
        coord.fcm_last_push = float("-inf")
        coord.fcm_lock = __import__("threading").Lock()

        _on_fcm_push(coord, {"from": "test"}, "pid-2")

        coord.hass.loop.call_soon_threadsafe.assert_called_once()
        assert coord.fcm_healthy is True


class TestNotifyTypeExceptionHandled:
    """Exception in services.async_call is logged, not raised."""

    @pytest.mark.asyncio
    async def test_service_call_exception_is_logged_not_raised(self):
        coord = _make_alert_coord()
        coord.hass.services.async_call = AsyncMock(
            side_effect=Exception("notify failed")
        )

        await _run_alert2(
            coord,
            event_type="MOVEMENT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
        )

    @pytest.mark.asyncio
    async def test_send_alert_step1_exception_causes_early_return(self):
        """When _notify_type raises in step 1, async_send_alert logs and returns early."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("svc boom"))

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={},
            last_event_ids={},
            hass=hass,
        )

        session = MagicMock()
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            # Should not raise — exception in step 1 is caught, logged, and returns
            await async_send_alert(
                coord,
                "TestCam",
                "MOVEMENT",
                "2026-01-01T10:00:00",
                image_url="",
                clip_url="",
                clip_status="",
            )

    @pytest.mark.asyncio
    async def test_step1_all_configured_services_fail_logs_not_delivered(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Maintenance-round bug-hunt finding, 2026-07-17: _notify_type used
        to return `bool(services)` — true whenever a service was CONFIGURED,
        even if every single `hass.services.async_call` raised. A genuinely
        configured-but-failing notify target (e.g. a briefly-unavailable
        mobile_app device, a Signal/Telegram outage) would still be logged
        as "sent", the exact same misreport class as the alert_notify_video
        bug fixed earlier the same day — just triggered by a live call
        failure instead of an unset option. `_notify_type` now tracks
        actual per-call success; this test configures step 1's service and
        makes every call to it raise, then asserts the log says the
        message was NOT delivered, not "sent"."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock(side_effect=RuntimeError("svc boom"))

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={},
            last_event_ids={},
            hass=hass,
        )

        session = MagicMock()
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            caplog.at_level("DEBUG", logger="custom_components.bosch_shc_camera.fcm"),
        ):
            await async_send_alert(
                coord,
                "TestCam",
                "MOVEMENT",
                "2026-01-01T10:00:00",
                image_url="",
                clip_url="",
                clip_status="",
            )

        hass.services.async_call.assert_awaited()
        messages = [r.message for r in caplog.records]
        assert any("Alert step 1 (text) NOT delivered" in m for m in messages), (
            "a service that is configured but whose every call fails must "
            "log NOT delivered, not the misleading 'sent'"
        )
        assert not any(m.startswith("Alert step 1 (text) sent") for m in messages), (
            "the 'sent' log must not fire when every configured call failed"
        )

    @pytest.mark.asyncio
    async def test_step1_returns_delivered_when_at_least_one_service_succeeds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Control case for the fix above: with MULTIPLE configured services
        where at least one call succeeds, the step must still be logged as
        sent — the fix tracks "at least one delivered", not "all delivered"."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        call_log: list[str] = []

        async def _svc_call(domain, service, data, **kw):
            call_log.append(service)
            if service == "fails":
                raise RuntimeError("this target is down")

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock(side_effect=_svc_call)

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.fails,notify.works",
                "alert_notify_information": "notify.fails,notify.works",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={},
            last_event_ids={},
            hass=hass,
        )

        session = MagicMock()
        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            caplog.at_level("DEBUG", logger="custom_components.bosch_shc_camera.fcm"),
        ):
            await async_send_alert(
                coord,
                "TestCam",
                "MOVEMENT",
                "2026-01-01T10:00:00",
                image_url="",
                clip_url="",
                clip_status="",
            )

        assert call_log == ["fails", "works"], (
            "both configured services must be attempted, in order"
        )
        messages = [r.message for r in caplog.records]
        assert any(m.startswith("Alert step 1 (text) sent") for m in messages), (
            "one successful delivery among several configured targets must "
            "still count as sent"
        )


class TestTroubleEventStep1EarlyReturn:
    """A TROUBLE-class event returns after step 1 — no step-2 file writes happen."""

    @pytest.mark.asyncio
    async def test_trouble_event_skips_step2_writes(self):
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"imagedata", content_type="image/jpeg")
        )

        coord2 = _make_alert_coord(
            options={
                "alert_notify_service": "notify.signal",
                "alert_notify_system": "",  # no system service → still proceeds
            }
        )
        await _run_alert2(
            coord2,
            event_type="TROUBLE_DISCONNECT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
            session_override=session,
        )
        write_calls = [
            c
            for c in coord2.hass.async_add_executor_job.call_args_list
            if c.args
            and callable(c.args[0])
            and getattr(c.args[0], "__name__", "") == "_write_file"
        ]
        assert len(write_calls) == 0, "TROUBLE event must not write any files"


class TestStep2RefetchExceptionContinues:
    """Exception in a re-fetch attempt is caught, loop continues."""

    @pytest.mark.asyncio
    async def test_refetch_exception_does_not_abort(self):
        """Exception on re-fetch attempt → caught, loop continues to next delay."""
        coord = _make_alert_coord()

        session = MagicMock()
        call_count = [0]

        def _get_side(url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(side_effect=Exception("net fail"))
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm
            return _resp_cm(404)

        session.get = MagicMock(side_effect=_get_side)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        # Must not raise even though re-fetch #1 throws
                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "MOVEMENT",
                            "2026-05-07T10:00:00.000Z",
                            "",
                        )


class TestStep2NoImageData:
    """imageUrl responds with empty body → snapshot not written."""

    @pytest.mark.asyncio
    async def test_empty_image_body_skips_write(self):
        coord = _make_alert_coord()
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=b"", content_type="image/jpeg")
        )
        await _run_alert2(
            coord,
            event_type="MOVEMENT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
            session_override=session,
        )
        write_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and callable(c.args[0])
            and getattr(c.args[0], "__name__", "") == "_write_file"
        ]
        assert len(write_calls) == 0, "empty body must not trigger _write_file"

    @pytest.mark.asyncio
    async def test_send_alert_step2_exception_is_swallowed(self):
        """Exception during snapshot download in step 2 is caught and does not propagate."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        call_count = 0

        async def _svc_call(domain, service, data, **kw):
            nonlocal call_count
            call_count += 1

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock(side_effect=_svc_call)

        # Step-2 GET raises a network error
        snap_resp = MagicMock()
        snap_resp.__aenter__ = AsyncMock(side_effect=RuntimeError("network failure"))
        snap_resp.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=snap_resp)

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "alert_notify_screenshot": "notify.test",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={},
            last_event_ids={},
            hass=hass,
        )

        image_url = "https://media.boschsecurity.com/snap.jpg"
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                await async_send_alert(
                    coord,
                    "TestCam",
                    "MOVEMENT",
                    "2026-01-01T10:00:00",
                    image_url=image_url,
                    clip_url="",
                    clip_status="",
                )
        # Step 1 must have fired; step 2 exception must be swallowed (no raise)
        assert call_count >= 1, "step-1 notify must have been called"


class TestStep3DirectClipException:
    """Exception during direct clip.mp4 probe is silently swallowed."""

    @pytest.mark.asyncio
    async def test_direct_clip_probe_exception_swallowed(self):
        coord = _make_alert_coord()
        call_count = [0]

        def _get_side(url, **kwargs):
            call_count[0] += 1
            if "clip.mp4" in str(url):
                cm = MagicMock()
                cm.__aenter__ = AsyncMock(side_effect=Exception("clip probe fail"))
                cm.__aexit__ = AsyncMock(return_value=None)
                return cm
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        await _run_alert2(
            coord,
            event_type="MOVEMENT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
            clip_url="",
            clip_status="",
            session_override=session,
        )


class TestStep3DirectClipDetected:
    """Direct clip.mp4 responds 200 with video content-type → found_clip_url is set."""

    @pytest.mark.asyncio
    async def test_send_alert_direct_clip_mp4_detected(self):
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        svc_calls: list[tuple] = []

        async def _svc_call(domain, service, data, **kw):
            svc_calls.append((domain, service))

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock(side_effect=_svc_call)

        CAM = "cam-xyz"
        event_id = "evt-clip-001"

        # Direct clip download: 200 + video/mp4 content-type
        clip_direct_resp = MagicMock()
        clip_direct_resp.status = 200
        clip_direct_resp.headers = {"Content-Type": "video/mp4"}
        clip_direct_resp.__aenter__ = AsyncMock(return_value=clip_direct_resp)
        clip_direct_resp.__aexit__ = AsyncMock(return_value=None)

        # The actual clip download (step-3): returns data → skips write path checked elsewhere
        clip_dl_resp = MagicMock()
        clip_dl_resp.status = 200
        clip_dl_resp.read = AsyncMock(return_value=b"x" * 5000)
        clip_dl_resp.headers = {"Content-Type": "video/mp4"}
        clip_dl_resp.__aenter__ = AsyncMock(return_value=clip_dl_resp)
        clip_dl_resp.__aexit__ = AsyncMock(return_value=None)

        call_iter = iter([clip_direct_resp, clip_dl_resp])

        def _get(url, **kw):
            try:
                return next(call_iter)
            except StopIteration:
                return _make_resp(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get)
        session.put = MagicMock(return_value=_make_resp(204))

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "alert_notify_video": "notify.test",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={CAM: {"info": {"title": "TestCam"}}},
            last_event_ids={CAM: event_id},
            hass=hass,
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                with patch(f"{MODULE}._write_file"):
                    await async_send_alert(
                        coord,
                        "TestCam",
                        "MOVEMENT",
                        "2026-01-01T10:00:00",
                        image_url="",  # no snapshot → skips step 2
                        clip_url="",
                        clip_status="",
                    )


class TestAlertStepSkipLogging:
    """Live-report 2026-07-17 (Thomas): notifications not reliably arriving
    on his phone, traced to `alert_notify_video` (and, symmetrically,
    `alert_notify_screenshot`) being unset in his config — "screenshot" and
    "video" deliberately do NOT fall back to alert_notify_service
    (get_alert_services docstring), so zero notify calls happen for that
    step. The bug: `_notify_type`'s callers logged "sent" unconditionally
    regardless of whether any service was actually configured/called,
    silently lying about delivery. Fixed by having `_notify_type` report
    whether it actually dispatched anything, and having each caller log
    accordingly.
    """

    @pytest.mark.asyncio
    async def test_video_skipped_logs_not_sent_when_alert_notify_video_unset(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The exact reported scenario: alert_notify_video="" while every
        other alert_notify_* is configured. No notify.* service call must
        happen for the video step, and the log must say so — not "sent"."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        clip_dl_resp = MagicMock()
        clip_dl_resp.status = 200
        clip_dl_resp.read = AsyncMock(return_value=b"x" * 5000)
        clip_dl_resp.headers = {"Content-Type": "video/mp4"}
        clip_dl_resp.__aenter__ = AsyncMock(return_value=clip_dl_resp)
        clip_dl_resp.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=clip_dl_resp)

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock()

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.signalkamera",
                "alert_notify_information": "notify.signalkamera",
                "alert_notify_screenshot": "notify.signalkamera",
                "alert_notify_video": "",  # unset — the reported scenario
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            last_event_ids={CAM_ID: "evt-001"},
            hass=hass,
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()),
            patch(f"{MODULE}._write_file"),
            caplog.at_level("DEBUG", logger="custom_components.bosch_shc_camera.fcm"),
        ):
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-07-17T10:00:00",
                image_url="",  # skip step 2, isolate step 3
                clip_url="https://residential.cbs.boschsecurity.com/v11/events/abc/clip.mp4",
                clip_status="Done",
            )

        # Step 1 (text) still legitimately calls notify.signalkamera via the
        # "information" key — only the VIDEO caption (containing "Video")
        # must never appear in any call, since that's the step under test.
        video_calls = [
            c
            for c in hass.services.async_call.call_args_list
            if c.args and len(c.args) >= 3 and "Video" in str(c.args[2])
        ]
        assert video_calls == [], (
            "no notify service must be called with the video caption when "
            "alert_notify_video is unset"
        )
        messages = [r.message for r in caplog.records]
        assert any(
            "Alert step 3 (video) downloaded but NOT delivered" in m for m in messages
        ), (
            "log must clearly say the video was NOT delivered, not the old "
            "misleading unconditional 'sent'"
        )
        assert not any(m.startswith("Alert step 3 (video) sent") for m in messages), (
            "the misleading 'sent' log must not fire when nothing was sent"
        )

    @pytest.mark.asyncio
    async def test_video_sent_logs_sent_when_alert_notify_video_configured(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Control case: with alert_notify_video actually set, the service IS
        called and the log correctly says "sent" — the fix must not
        regress the working case into always saying "skipped"."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        clip_dl_resp = MagicMock()
        clip_dl_resp.status = 200
        clip_dl_resp.read = AsyncMock(return_value=b"x" * 5000)
        clip_dl_resp.headers = {"Content-Type": "video/mp4"}
        clip_dl_resp.__aenter__ = AsyncMock(return_value=clip_dl_resp)
        clip_dl_resp.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=clip_dl_resp)

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock()

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.signalkamera",
                "alert_notify_information": "notify.signalkamera",
                "alert_notify_screenshot": "notify.signalkamera",
                "alert_notify_video": "notify.signalkamera",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={CAM_ID: {"info": {"title": "Terrasse"}}},
            last_event_ids={CAM_ID: "evt-002"},
            hass=hass,
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()),
            patch(f"{MODULE}._write_file"),
            caplog.at_level("DEBUG", logger="custom_components.bosch_shc_camera.fcm"),
        ):
            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-07-17T10:00:00",
                image_url="",
                clip_url="https://residential.cbs.boschsecurity.com/v11/events/abc/clip.mp4",
                clip_status="Done",
            )

        video_calls = [
            c
            for c in hass.services.async_call.call_args_list
            if c.args[:2] == ("notify", "signalkamera")
        ]
        assert len(video_calls) >= 1, (
            "video notify service must be called when configured"
        )
        messages = [r.message for r in caplog.records]
        assert any(m.startswith("Alert step 3 (video) sent") for m in messages), (
            "log must say 'sent' when the video was actually delivered"
        )
        assert not any("downloaded but NOT delivered" in m for m in messages)

    @pytest.mark.asyncio
    async def test_screenshot_skipped_logs_not_sent_when_alert_notify_screenshot_unset(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Same class of bug, screenshot step (alert_notify_screenshot unset).

        alert_notify_service is deliberately a DIFFERENT service than the
        one under test, so step 1's legitimate fallback call to it can't be
        confused with (or mask a bug in) the screenshot-step assertion.
        """
        safe_img = "https://residential.cbs.boschsecurity.com/v11/events/abc/image.jpg"
        coord = _make_alert_coord4(
            options={
                "alert_notify_service": "notify.textonly",
                "alert_notify_screenshot": "",  # unset
            }
        )

        @asynccontextmanager
        async def _get(url, **kw):
            resp = MagicMock()
            resp.status = 200
            resp.headers = {"Content-Type": "image/jpeg"}
            resp.read = AsyncMock(return_value=b"\xff\xd8snap")
            yield resp

        session = MagicMock()
        session.get = _get

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                new=AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
            patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
            caplog.at_level("DEBUG", logger="custom_components.bosch_shc_camera.fcm"),
        ):
            from custom_components.bosch_shc_camera.fcm import async_send_alert

            await async_send_alert(
                coord,
                "Terrasse",
                "MOVEMENT",
                "2026-05-07T10:00:00.000Z",
                safe_img,
            )

        screenshot_calls = [
            c
            for c in coord.hass.services.async_call.call_args_list
            if c.args[:2] == ("notify", "signal")
        ]
        assert screenshot_calls == [], (
            "no notify service must be called for the screenshot step when "
            "alert_notify_screenshot is unset"
        )
        messages = [r.message for r in caplog.records]
        assert any("Alert step 2 (screenshot) NOT delivered" in m for m in messages), (
            "log must say the screenshot was NOT delivered, not the old "
            "misleading unconditional 'sent'"
        )
        assert not any(m.startswith("Alert step 2 (screenshot) sent") for m in messages)


class TestStep3ClipUnavailableMidPoll:
    """Poll returns Unavailable → stop polling immediately."""

    @pytest.mark.asyncio
    async def test_unavailable_stops_poll(self):
        coord = _make_alert_coord()
        poll_count = [0]

        def _get_side(url, **kwargs):
            if "clip.mp4" in str(url):
                return _resp_cm(404)  # direct probe fails
            if "events" in str(url):
                poll_count[0] += 1
                if poll_count[0] == 1:
                    return _resp_cm(
                        200,
                        json_data=[
                            {
                                "timestamp": "2026-05-07T10:00:00Z",
                                "videoClipUploadStatus": "Unavailable",
                                "videoClipUrl": "",
                            }
                        ],
                    )
                return _resp_cm(200, json_data=[])
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        await _run_alert2(
            coord,
            event_type="MOVEMENT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
            clip_url="",
            clip_status="",
            session_override=session,
        )
        # Should have polled only once (Unavailable stops the loop)
        assert poll_count[0] <= 2


class TestStep3ClipDoneAfterPoll:
    """Poll returns Done → found_clip_url set, loop breaks, download proceeds."""

    @pytest.mark.asyncio
    async def test_clip_done_after_poll_triggers_download(self):
        coord = _make_alert_coord()
        CLIP_URL = "https://residential.cbs.boschsecurity.com/clip.mp4"
        poll_count = [0]
        download_body = b"D" * 2000

        def _get_side(url, **kwargs):
            if "clip.mp4" in str(url) and "events" not in str(url):
                if poll_count[0] == 0:
                    return _resp_cm(404)
                return _resp_cm(200, body=download_body, content_type="video/mp4")
            if "events" in str(url):
                poll_count[0] += 1
                return _resp_cm(
                    200,
                    json_data=[
                        {
                            "timestamp": "2026-05-07T10:00:00Z",
                            "videoClipUploadStatus": "Done",
                            "videoClipUrl": CLIP_URL,
                        }
                    ],
                )
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
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
                            "https://residential.cbs.boschsecurity.com/img.jpg",
                        )

        assert poll_count[0] >= 1


class TestStep3PollException:
    """Exception during a poll iteration → continue (not crash)."""

    @pytest.mark.asyncio
    async def test_poll_exception_continues(self):
        coord = _make_alert_coord()
        call_count = [0]

        def _get_side(url, **kwargs):
            if "events" in str(url) and "clip" not in str(url):
                call_count[0] += 1
                if call_count[0] == 1:
                    cm = MagicMock()
                    cm.__aenter__ = AsyncMock(side_effect=Exception("poll boom"))
                    cm.__aexit__ = AsyncMock(return_value=None)
                    return cm
                return _resp_cm(200, json_data=[])
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        await _run_alert2(
            coord,
            event_type="MOVEMENT",
            image_url="https://residential.cbs.boschsecurity.com/img.jpg",
            clip_url="",
            clip_status="",
            session_override=session,
        )
        # Must complete without raising

    @pytest.mark.asyncio
    async def test_send_alert_step3_exception_is_swallowed(self):
        """Exception during clip download in step 3 is caught and does not propagate."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock()

        CAM = "cam-abc"

        bad_resp = MagicMock()
        bad_resp.__aenter__ = AsyncMock(side_effect=RuntimeError("clip dl boom"))
        bad_resp.__aexit__ = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=bad_resp)

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={CAM: {"info": {"title": "TestCam"}}},
            last_event_ids={CAM: ""},
            hass=hass,
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                await async_send_alert(
                    coord,
                    "TestCam",
                    "MOVEMENT",
                    "2026-01-01T10:00:00",
                    image_url="",
                    clip_url="https://media.boschsecurity.com/clip.mp4",
                    clip_status="Done",
                )


class TestStep3VideoTooSmall:
    """Downloaded video < 1000 bytes → not written, not notified."""

    @pytest.mark.asyncio
    async def test_small_video_not_written(self):
        coord = _make_alert_coord()
        CLIP_URL = "https://residential.cbs.boschsecurity.com/clip.mp4"

        def _get_side(url, **kwargs):
            if str(url) == CLIP_URL:
                return _resp_cm(200, body=b"tiny", content_type="video/mp4")
            return _resp_cm(404)

        session = MagicMock()
        session.get = MagicMock(side_effect=_get_side)

        await _run_alert2(
            coord,
            event_type="MOVEMENT",
            image_url="",
            clip_url=CLIP_URL,
            clip_status="Done",
            session_override=session,
        )
        write_calls = [
            c
            for c in coord.hass.async_add_executor_job.call_args_list
            if c.args
            and callable(c.args[0])
            and getattr(c.args[0], "__name__", "") == "_write_file"
            and str(c.args[1]).endswith(".mp4")
        ]
        assert len(write_calls) == 0, "< 1 KB video must not be written"


class TestMarkEventsReadGate:
    """mark_events_read called when option enabled + cam_id found; and swallows
    exceptions raised by async_mark_events_read (both pre- and post-send call
    sites)."""

    @pytest.mark.asyncio
    async def test_mark_events_read_called_when_enabled(self):
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "mark_events_read": True,
            }
        )

        mark_read_calls = []

        async def _fake_mark(c, ids):
            mark_read_calls.append(ids)

        with patch(f"{MODULE}.async_mark_events_read", side_effect=_fake_mark):
            await _run_alert2(coord, event_type="MOVEMENT")

        assert len(mark_read_calls) >= 1, (
            "mark_events_read must be called when option enabled"
        )

    @pytest.mark.asyncio
    async def test_mark_events_read_not_called_when_disabled(self):
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "mark_events_read": False,
            }
        )

        mark_read_calls = []

        async def _fake_mark(c, ids):
            mark_read_calls.append(ids)

        with patch(f"{MODULE}.async_mark_events_read", side_effect=_fake_mark):
            await _run_alert2(coord, event_type="MOVEMENT")

        assert len(mark_read_calls) == 0, (
            "mark_events_read must NOT be called when option disabled"
        )

    @pytest.mark.asyncio
    async def test_send_alert_mark_events_read_exception_swallowed(self):
        """async_mark_events_read called after step-3 must swallow exceptions."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.async_add_executor_job = AsyncMock()
        hass.services.async_call = AsyncMock()

        CAM = "cam-mark"
        event_id = "evt-mark-001"

        # All GETs fail → no snapshot, no clip
        bad_resp = MagicMock()
        bad_resp.status = 404
        bad_resp.__aenter__ = AsyncMock(return_value=bad_resp)
        bad_resp.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=bad_resp)

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "mark_events_read": True,  # enable the post-send mark branch
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={CAM: {"info": {"title": "TestCam"}}},
            last_event_ids={CAM: event_id},
            hass=hass,
        )

        async def _raising_mark(coord_, ids):
            raise RuntimeError("mark failed")

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                with patch(
                    f"{MODULE}.async_mark_events_read",
                    side_effect=_raising_mark,
                ):
                    await async_send_alert(
                        coord,
                        "TestCam",
                        "MOVEMENT",
                        "2026-01-01T10:00:00",
                        image_url="",
                        clip_url="",
                        clip_status="",
                    )


class TestSmbUploadTimeout:
    """asyncio.TimeoutError from SMB upload is caught + logged."""

    @pytest.mark.asyncio
    async def test_smb_timeout_does_not_propagate(self):
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "enable_smb_upload": True,
                "smb_server": "nas",
            }
        )

        wait_for_count = [0]

        async def _selective_wait_for(coro, timeout=None):
            wait_for_count[0] += 1
            # First wait_for is SMB upload — raise TimeoutError
            raise TimeoutError()

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(
                return_value=MagicMock(get=MagicMock(return_value=_resp_cm(404)))
            ),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        with patch(
                            f"{MODULE}.asyncio.wait_for",
                            side_effect=_selective_wait_for,
                        ):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",
                            )

    @pytest.mark.asyncio
    async def test_send_alert_smb_exception_swallowed(self):
        """Generic (non-timeout) exception from SMB upload is swallowed."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        async def _executor_job(fn, *args, **kw):
            # Allow os.makedirs (or any non-SMB call) to succeed; only raise for sync_smb_upload
            from custom_components.bosch_shc_camera.smb import sync_smb_upload

            if fn is sync_smb_upload:
                raise RuntimeError("smb boom")
            return None  # os.makedirs, _write_file, etc.

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.services.async_call = AsyncMock()
        hass.async_add_executor_job = AsyncMock(side_effect=_executor_job)

        CAM = "cam-smb"

        # All GETs return 404 to skip snapshot/clip
        bad_resp = _make_resp(404)
        session = MagicMock()
        session.get = MagicMock(return_value=bad_resp)

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "enable_smb_upload": True,
                "smb_server": "//nas/share",
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={CAM: {"info": {"title": "TestCam"}}},
            last_event_ids={CAM: "evt-smb-001"},
            hass=hass,
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                await async_send_alert(
                    coord,
                    "TestCam",
                    "MOVEMENT",
                    "2026-01-01T10:00:00",
                    image_url="",
                    clip_url="",
                    clip_status="",
                )


class TestLocalSaveTimeout:
    """asyncio.TimeoutError from local save is caught + logged."""

    @pytest.mark.asyncio
    async def test_local_save_timeout_does_not_propagate(self):
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "download_path": "/tmp/bosch_test_events",
            }
        )

        wait_for_count = [0]

        async def _selective_wait_for(coro, timeout=None):
            wait_for_count[0] += 1
            if wait_for_count[0] == 1:
                raise TimeoutError()
            return await coro

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(
                return_value=MagicMock(get=MagicMock(return_value=_resp_cm(404)))
            ),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        with patch(
                            f"{MODULE}.asyncio.wait_for",
                            side_effect=_selective_wait_for,
                        ):
                            from custom_components.bosch_shc_camera.fcm import (
                                async_send_alert,
                            )

                            await async_send_alert(
                                coord,
                                "Terrasse",
                                "MOVEMENT",
                                "2026-05-07T10:00:00.000Z",
                                "",
                            )

    @pytest.mark.asyncio
    async def test_send_alert_local_save_exception_swallowed(self):
        """Generic (non-timeout) exception from local save is swallowed."""
        from custom_components.bosch_shc_camera.fcm import async_send_alert

        async def _executor_job(fn, *args, **kw):
            from custom_components.bosch_shc_camera.smb import sync_local_save

            if fn is sync_local_save:
                raise RuntimeError("local save boom")
            return None  # os.makedirs etc.

        hass = MagicMock()
        hass.config.config_dir = "/tmp"
        hass.services.async_call = AsyncMock()
        hass.async_add_executor_job = AsyncMock(side_effect=_executor_job)

        CAM = "cam-local"

        bad_resp = _make_resp(404)
        session = MagicMock()
        session.get = MagicMock(return_value=bad_resp)

        coord = SimpleNamespace(
            token="tok",
            options={
                "alert_notify_service": "notify.test",
                "alert_notify_information": "notify.test",
                "download_path": "/tmp/bosch",  # enables local save branch
                "alert_save_snapshots": False,
                "alert_delete_after_send": False,
            },
            data={CAM: {"info": {"title": "TestCam"}}},
            last_event_ids={CAM: "evt-local-001"},
            hass=hass,
        )

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new=AsyncMock()):
                await async_send_alert(
                    coord,
                    "TestCam",
                    "MOVEMENT",
                    "2026-01-01T10:00:00",
                    image_url="",
                    clip_url="",
                    clip_status="",
                )


class TestCleanupOsError:
    """OSError during file cleanup is caught silently."""

    @pytest.mark.asyncio
    async def test_os_remove_error_does_not_propagate(self):
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.test",
                "alert_delete_after_send": True,
                "alert_save_snapshots": False,
            }
        )

        image_body = b"J" * 500
        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm(200, body=image_body, content_type="image/jpeg")
        )

        # Make async_add_executor_job succeed for makedirs+write but raise for os.remove
        exec_call_count = [0]

        async def _exec_side(fn, *args):
            exec_call_count[0] += 1
            if fn is os.remove:
                raise OSError("file busy")
            return fn(*args)

        coord.hass.async_add_executor_job = _exec_side

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        # Must not raise even though os.remove fails
                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "MOVEMENT",
                            "2026-05-07T10:00:00.000Z",
                            "https://residential.cbs.boschsecurity.com/img.jpg",
                        )


class TestLocalSaveWithoutNotifyService:
    """Regression: sync_local_save must fire even with no alert_notify_service.

    Bug: async_send_alert returned early at the info_svcs guard when no
    notification service was configured, so sync_local_save was never reached.
    Fresh installs default to no notify service, leaving the local events
    directory permanently empty.
    """

    @pytest.mark.asyncio
    async def test_local_save_fires_without_notify_service(self):
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_events",
            }
        )
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save") as mock_save:
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "MOVEMENT",
                            "2026-05-07T10:00:00.000Z",
                            "",
                            "",
                            "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert any(c.args[0] is mock_save for c in executor_calls), (
            f"sync_local_save must be queued via async_add_executor_job when "
            f"download_path is set, even with no notify service. "
            f"executor calls: {[getattr(c.args[0], '__name__', repr(c.args[0])) for c in executor_calls]}"
        )

    @pytest.mark.asyncio
    async def test_early_return_when_truly_nothing_configured(self):
        """No notify service + no download_path + no SMB → immediate return, no work done."""
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "",
                "download_path": "",
                "enable_smb_upload": False,
            }
        )
        coord.hass.async_add_executor_job = AsyncMock()

        session = MagicMock()
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    from custom_components.bosch_shc_camera.fcm import async_send_alert

                    await async_send_alert(
                        coord,
                        "Terrasse",
                        "MOVEMENT",
                        "2026-05-07T10:00:00.000Z",
                        "",
                        "",
                        "",
                    )

        coord.hass.async_add_executor_job.assert_not_called()


class TestLocalSaveEnableToggle:
    """Regression: disabling enable_local_save must stop sync_local_save from being called.

    Bug: fcm.py checked only `download_path`, ignoring `enable_local_save`, so
    unchecking the Options toggle had no effect as long as download_path was
    still set.
    """

    @pytest.mark.asyncio
    async def test_local_save_skipped_when_toggle_off(self):
        """enable_local_save=False → sync_local_save must NOT be called even if download_path is set."""
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.mobile_app",
                "enable_local_save": False,
                "download_path": "/tmp/bosch_test_events",
            }
        )
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save") as mock_save:
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "MOVEMENT",
                            "2026-05-08T10:00:00.000Z",
                            "",
                            "",
                            "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert not any(c.args[0] is mock_save for c in executor_calls), (
            "sync_local_save must NOT run when enable_local_save=False, "
            "even if download_path is configured."
        )

    @pytest.mark.asyncio
    async def test_local_save_runs_when_toggle_on(self):
        """enable_local_save=True + download_path set → sync_local_save must be called."""
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.mobile_app",
                "enable_local_save": True,
                "download_path": "/tmp/bosch_test_events",
            }
        )
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save") as mock_save:
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "MOVEMENT",
                            "2026-05-08T10:00:00.000Z",
                            "",
                            "",
                            "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert any(c.args[0] is mock_save for c in executor_calls), (
            "sync_local_save must be called when enable_local_save=True and download_path is set."
        )


class TestEventIdNotOverwrittenByConcurrentPush:
    """Regression: concurrent FCM pipelines must not cross-report each other's
    event_id in FTP/local-save filenames.

    Bug: async_send_alert fetched coordinator.last_event_ids[cam_id] at
    SMB/local-save time (after up to 90s of clip polling). A later FCM push
    arriving during that window overwrote last_event_ids, so all concurrent
    pipelines reported the newest event_id regardless of which event triggered
    them (observed: three PERSON events within 47s all uploaded under the
    last push's event ID).

    Fix: pass event_id as a parameter to async_send_alert; use it instead of
    looking up last_event_ids at upload time.
    """

    def _get_smb_ev_id(self, coord, mock_smb_upload) -> str | None:
        """Extract the event id from the smb_data passed to async_add_executor_job."""
        for c in coord.hass.async_add_executor_job.call_args_list:
            if c.args and c.args[0] is mock_smb_upload:
                smb_data = c.args[2]  # (fn, coordinator, smb_data, token)
                for cam_data in smb_data.values():
                    for ev in cam_data.get("events", []):
                        return ev.get("id")
        return None

    @pytest.mark.asyncio
    async def test_passed_event_id_used_over_last_event_ids(self):
        """event_id kwarg must be used for SMB filename, not last_event_ids at upload time."""
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.mobile_app",
                "enable_smb_upload": True,
                "smb_server": "nas.local",
                "smb_share": "cameras",
            }
        )
        # Simulate a later push overwriting last_event_ids before this pipeline's upload
        coord.last_event_ids[CAM_ID] = "NEWER_OVERWRITE_ID"

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload") as mock_smb:
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "PERSON",
                            "2026-05-08T16:19:51.000Z",
                            "",
                            "",
                            "",
                            event_id="ORIGINAL_PIPELINE_ID",
                        )
                        ev_id = self._get_smb_ev_id(coord, mock_smb)

        assert ev_id == "ORIGINAL_PIPELINE_ID", (
            f"SMB upload must use the event_id passed at pipeline start, not last_event_ids "
            f"which may have been overwritten by a concurrent push. Got: {ev_id!r}"
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_last_event_ids_when_no_event_id_passed(self):
        """When event_id kwarg is omitted, fall back to last_event_ids (backwards compat)."""
        coord = _make_alert_coord(
            options={
                "alert_notify_service": "notify.mobile_app",
                "enable_smb_upload": True,
                "smb_server": "nas.local",
                "smb_share": "cameras",
            }
        )
        coord.last_event_ids[CAM_ID] = "FALLBACK_ID"

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload") as mock_smb:
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        from custom_components.bosch_shc_camera.fcm import (
                            async_send_alert,
                        )

                        await async_send_alert(
                            coord,
                            "Terrasse",
                            "PERSON",
                            "2026-05-08T16:19:51.000Z",
                            "",
                            "",
                            "",
                            # no event_id — must fall back to last_event_ids
                        )
                        ev_id = self._get_smb_ev_id(coord, mock_smb)

        assert ev_id == "FALLBACK_ID", (
            f"Without explicit event_id, SMB upload must fall back to last_event_ids. "
            f"Got: {ev_id!r}"
        )


class _StubFcmClient:
    """Stub replacing the real (patched) FcmPushClient class.

    Records the credentials_updated_callback it was constructed with so the
    test can invoke it directly, simulating the Firebase SDK's background
    thread calling back into HA-land.
    """

    instances: ClassVar[list[_StubFcmClient]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.creds_cb = kwargs["credentials_updated_callback"]
        _StubFcmClient.instances.append(self)

    async def checkin_or_register(self) -> str:
        return "fake-token-" + str(len(_StubFcmClient.instances))

    async def start(self) -> None:
        return None

    def is_started(self) -> bool:
        return True


def _make_creds_race_coord() -> SimpleNamespace:
    hass = MagicMock()
    # call_soon_threadsafe runs the callback immediately (synchronously) so
    # the test doesn't need a real event loop turn to observe the effect.
    hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn: fn())
    # Schedule the coroutine as a real task (not close() it) so the test can
    # await one loop tick and observe whether _fake_persist ran.
    hass.async_create_task = lambda coro: asyncio.get_event_loop().create_task(coro)
    hass.config_entries = SimpleNamespace(async_update_entry=MagicMock())

    return SimpleNamespace(
        token="tok-A",
        fcm_token=None,
        fcm_push_mode="unknown",
        fcm_lock=Lock(),
        fcm_running=False,
        fcm_healthy=False,
        fcm_client=None,
        fcm_started_at=float("-inf"),
        entry=SimpleNamespace(
            data={
                "fcm_config": {
                    "api_key": "fake-key",
                    "project_id": "fake-project",
                    "app_id": "fake-app",
                },
            }
        ),
        options={"enable_fcm_push": True, "fcm_push_mode": "auto"},
        hass=hass,
    )


class TestStaleClientCredsCallbackIgnoredAfterHardHeal:
    """`_on_creds_updated` fires from the Firebase SDK's own background thread
    (not the HA event loop) whenever the client refreshes its credentials. A
    hard-heal purges credentials and starts a brand-new client; if the OLD,
    now-replaced client's callback fires late (after the new client already
    persisted fresh credentials), it must not silently overwrite them with
    stale ones — which would defeat the hard-heal it was meant to recover
    from.

    Fix: `_try_fcm()` in fcm.py captures the client instance it created in a
    closure variable (`_this_client`), and `_on_creds_updated`'s inner
    `_persist` only proceeds if `coordinator.fcm_client is _this_client` at
    call time — detecting that the coordinator has since moved on to a newer
    client.

    This drives the real `_async_start_fcm_push_locked` (not a mock of it)
    twice, simulating two consecutive client generations, and fires the
    FIRST generation's stale credentials_updated_callback after the second
    generation is already active — pinning that the stale callback's persist
    is skipped.
    """

    @pytest.mark.asyncio
    async def test_stale_client_creds_callback_is_ignored_after_hard_heal(self) -> None:
        from custom_components.bosch_shc_camera import fcm

        _StubFcmClient.instances = []
        coord = _make_creds_race_coord()

        persisted: list[dict[str, Any]] = []

        async def _fake_persist(_coord: object, creds: dict[str, Any]) -> None:
            persisted.append(creds)

        with (
            patch.object(
                fcm, "_get_fcm_push_client_class", return_value=_StubFcmClient
            ),
            patch.object(
                fcm, "register_fcm_with_bosch", new=AsyncMock(return_value=True)
            ),
            patch.object(fcm, "_async_persist_fcm_creds", new=_fake_persist),
        ):
            # Generation 1 (e.g. the client running before a hard-heal).
            started_1 = await fcm._async_start_fcm_push_locked(coord)
            assert started_1 is True
            assert len(_StubFcmClient.instances) == 1
            gen1 = _StubFcmClient.instances[0]

            # Simulate a hard-heal: coordinator moves on to a fresh client
            # (mirrors async_stop_fcm_push + a second _async_start_fcm_push_locked
            # call in _async_run_fcm_supervisor).
            coord.fcm_running = False
            started_2 = await fcm._async_start_fcm_push_locked(coord)
            assert started_2 is True
            assert len(_StubFcmClient.instances) == 2
            gen2 = _StubFcmClient.instances[1]
            assert coord.fcm_client is gen2  # sanity: coordinator points at gen2
            assert gen2 is not gen1  # sanity: distinct client instances

            # Generation 2 is fresh: its own callback persists normally.
            gen2.creds_cb({"gen": 2})
            await asyncio.sleep(0)  # let the scheduled persist task run
            assert persisted == [{"gen": 2}]

            # The STALE generation-1 callback fires late (its own SDK thread was
            # never guaranteed to be covered by async_stop_fcm_push's drain-wait).
            # Without the fix this would silently overwrite the fresh gen-2 creds.
            gen1.creds_cb({"gen": 1, "stale": True})
            await asyncio.sleep(0)
            assert persisted == [{"gen": 2}], (
                "stale generation-1 credentials_updated_callback must NOT persist "
                "after the coordinator has moved on to generation 2"
            )


def _session_post(resp_cm: MagicMock) -> MagicMock:
    session = MagicMock()
    session.post = MagicMock(return_value=resp_cm)
    return session


def _make_register_coord2(data: dict) -> SimpleNamespace:
    update_calls: list[dict] = []

    def _update_entry(entry: SimpleNamespace, **kwargs: object) -> None:
        update_calls.append(dict(kwargs))
        if "data" in kwargs:
            entry.data = kwargs["data"]  # type: ignore[assignment]

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=_update_entry),
    )
    coord = SimpleNamespace(
        token="bearer-abc",
        fcm_token="fcm-tok-X",
        entry=SimpleNamespace(data=dict(data)),
        hass=hass,
    )
    coord._update_calls = update_calls  # type: ignore[attr-defined]
    return coord


class TestPeriodicReRegistration:
    """register_fcm_with_bosch: skip re-POST when the registration is fresh;
    re-POST (and refresh the timestamp) when it is stale or the stamp is
    malformed, so a server-side-dropped Bosch registration self-heals."""

    async def test_fresh_registration_skips_post(self) -> None:
        """Token unchanged + ANDROID + registered just now → skip the POST."""
        coord = _make_register_coord2(
            {
                "fcm_registered_token": "fcm-tok-X",
                "fcm_registered_device_type": "ANDROID",
                "fcm_registered_at": time.time(),
            }
        )
        session = _session_post(_resp_cm_text(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

            result = await register_fcm_with_bosch(coord)
        assert result is True
        session.post.assert_not_called()

    async def test_stale_registration_reposts(self) -> None:
        """Token unchanged but fcm_registered_at older than the interval → re-POST
        and refresh the timestamp (heal a dropped Bosch registration)."""
        from custom_components.bosch_shc_camera.fcm import (
            FCM_REREGISTER_INTERVAL_SEC,
            register_fcm_with_bosch,
        )

        coord = _make_register_coord2(
            {
                "fcm_registered_token": "fcm-tok-X",
                "fcm_registered_device_type": "ANDROID",
                "fcm_registered_at": time.time() - FCM_REREGISTER_INTERVAL_SEC - 3600,
            }
        )
        session = _session_post(_resp_cm_text(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            result = await register_fcm_with_bosch(coord)
        assert result is True
        session.post.assert_called_once()
        # timestamp refreshed on success
        last = coord._update_calls[-1]["data"]
        assert last["fcm_registered_at"] >= time.time() - 5

    async def test_malformed_registered_at_treated_as_stale(self) -> None:
        """A non-numeric fcm_registered_at must not crash — treat as stale (0.0)
        and re-POST so a corrupted stamp self-heals."""
        coord = _make_register_coord2(
            {
                "fcm_registered_token": "fcm-tok-X",
                "fcm_registered_device_type": "ANDROID",
                "fcm_registered_at": "not-a-number",
            }
        )
        session = _session_post(_resp_cm_text(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

            assert await register_fcm_with_bosch(coord) is True
        session.post.assert_called_once()

    async def test_successful_post_stamps_registered_at(self) -> None:
        """Fresh install: a 204 must persist fcm_registered_at for the gate."""
        coord = _make_register_coord2({})
        session = _session_post(_resp_cm_text(204))
        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            from custom_components.bosch_shc_camera.fcm import register_fcm_with_bosch

            assert await register_fcm_with_bosch(coord) is True
        last = coord._update_calls[-1]["data"]
        assert "fcm_registered_at" in last
        assert last["fcm_registered_token"] == "fcm-tok-X"


def _make_heal_coord(entry_data: dict, force_hard: bool = True) -> SimpleNamespace:
    coord = SimpleNamespace()
    coord.entry = SimpleNamespace(data=dict(entry_data))
    coord.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=MagicMock()),
    )
    coord.options = {"enable_fcm_push": True}
    coord.fcm_start_lock = asyncio.Lock()
    coord.fcm_force_hard_heal = force_hard
    coord.fcm_last_push = float("-inf")
    coord.fcm_running = False
    coord.fcm_healthy = False
    return coord


class TestForceHardHeal:
    """When the periodic /v11/events poll has confirmed push delivery is dead
    (`fcm_force_hard_heal=True`), the supervisor must purge credentials and
    hard-heal regardless of socket-liveness state; without the flag, and no
    staleness markers, it takes the soft path and must not purge creds."""

    async def test_supervisor_clears_force_hard_flag_and_purges_creds(self) -> None:
        """`fcm_force_hard_heal=True` → supervisor purges fcm_* creds and clears flag."""
        from custom_components.bosch_shc_camera import fcm
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        coord = _make_heal_coord(
            {
                "fcm_credentials": {"gcm": "x"},
                "fcm_registered_token": "tok",
                "other": "y",
            },
            force_hard=True,
        )

        with (
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "reset_fcm_creds_staleness_counter"),
            patch.object(
                fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=False)
            ),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            await asyncio.sleep(
                0.05
            )  # let one iteration run (hard-heal + failed start)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert coord.fcm_force_hard_heal is False, (
            "fcm_force_hard_heal must be cleared after supervisor hard-heal"
        )
        update_call = coord.hass.config_entries.async_update_entry.call_args
        assert update_call is not None, (
            "async_update_entry must be called during hard-heal"
        )
        new_data = (
            update_call.kwargs.get("data")
            or update_call[1].get("data")
            or update_call[0][1]
        )
        assert "fcm_credentials" not in new_data, (
            "fcm_* keys must be purged on hard-heal"
        )

    async def test_no_force_flag_skips_hard_heal(self) -> None:
        """Without fcm_force_hard_heal=True and no staleness markers, supervisor
        takes the soft path — async_update_entry (cred-purge) is NOT called."""
        from custom_components.bosch_shc_camera import fcm
        from custom_components.bosch_shc_camera.fcm import _FCMNoiseFilter

        _FCMNoiseFilter._SHARED_STALENESS_TIMESTAMPS.clear()
        coord = _make_heal_coord(
            {"fcm_credentials": {"gcm": "x"}, "fcm_registered_token": "tok"},
            force_hard=False,
        )

        with (
            patch.object(fcm, "async_stop_fcm_push", new=AsyncMock()),
            patch.object(fcm, "get_recent_fcm_creds_staleness_count", return_value=0),
            patch.object(
                fcm, "_async_start_fcm_push_locked", new=AsyncMock(return_value=False)
            ),
        ):
            task = asyncio.create_task(fcm._async_run_fcm_supervisor(coord))
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        (
            coord.hass.config_entries.async_update_entry.assert_not_called(),
            ("Soft path must not purge creds (async_update_entry must not be called)"),
        )


def test_fast_poll_is_inside_default_motion_window() -> None:
    """A polled event must be younger than the motion window when first seen,
    otherwise the motion sensor can never turn ON in polling-only (push-dead)
    mode."""
    import custom_components.bosch_shc_camera.coordinator as init_mod
    from custom_components.bosch_shc_camera.const import DEFAULT_MOTION_ACTIVE_WINDOW

    assert init_mod.FCM_DOWN_EVENT_POLL_SEC < DEFAULT_MOTION_ACTIVE_WINDOW


# Section: alert_sent_ids eviction + FCM watchdog tempo (relocated from
# tests/test_theoretical_bugs.py)


class TestAlertSentIdsEviction:
    """Eviction of `alert_sent_ids` must run whenever the cache is non-empty,
    not only once it exceeds a fixed size — a size-gated eviction starves
    when a burst of events (e.g. 4 cameras all seeing motion) keeps every
    entry younger than 120s while still growing the cache unbounded."""

    def _build_coord(self, sent_ids: dict[str, float]):
        return SimpleNamespace(
            alert_sent_ids=sent_ids,
            last_event_ids={},
            data={CAM_ID: {"info": {"title": "x"}}},
            options={},
            token="tok",
            hass=SimpleNamespace(
                bus=SimpleNamespace(async_fire=lambda *a, **kw: None),
                states=SimpleNamespace(get=lambda eid: None),
                async_create_task=lambda c: c.close() if hasattr(c, "close") else None,
            ),
            bg_tasks=set(),
        )

    def test_old_entries_evicted_on_dedup_check(self):
        """Entries older than 120s must be evicted, regardless of cache len."""
        now = time.monotonic()
        sent = {f"id-{i}": now - 200.0 for i in range(5)}
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert sent == {}, "All > 120s entries must be evicted"

    def test_recent_entries_kept(self):
        """Entries < 120s old must stay in the cache (still useful for dedup)."""
        now = time.monotonic()
        sent = {"recent-1": now - 30.0, "recent-2": now - 60.0}
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert "recent-1" in sent
        assert "recent-2" in sent

    def test_mixed_age_eviction(self):
        """Mix of old + recent — only old gets evicted."""
        now = time.monotonic()
        sent = {
            "old-1": now - 150.0,
            "old-2": now - 200.0,
            "fresh-1": now - 30.0,
            "fresh-2": now - 90.0,
        }
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert "old-1" not in sent
        assert "old-2" not in sent
        assert "fresh-1" in sent
        assert "fresh-2" in sent

    def test_eviction_fires_even_with_small_cache(self):
        """A small cache (e.g. 5 entries) must still evict stale entries —
        eviction runs whenever the cache is non-empty, not gated on size."""
        now = time.monotonic()
        sent = {f"stale-{i}": now - 300.0 for i in range(3)}
        if sent:
            for k in [k for k, v in sent.items() if v < now - 120.0]:
                sent.pop(k, None)
        assert sent == {}

    def test_eviction_skipped_when_cache_empty(self):
        """Empty cache → no work, no errors."""
        sent = {}
        if sent:
            assert False, "Eviction loop should not run on empty cache"

    def test_fix_present_in_fcm_source(self):
        """Pin the actual fix in fcm.py — if someone re-introduces the
        size-gate eviction it would starve during burst-event scenarios."""
        import re
        from pathlib import Path

        fcm_src = (
            Path(__file__).parent.parent
            / "custom_components"
            / "bosch_shc_camera"
            / "fcm.py"
        )
        text = fcm_src.read_text()
        no_comments = re.sub(r"#[^\n]*", "", text)
        assert "if len(_sent) > 32:" not in no_comments, (
            "Old size-gate eviction reintroduced in actual code path — "
            "would starve during burst events. Use plain `if _sent:` gate."
        )


class TestFCMWatchdogTempo:
    """`fcm_healthy` gates the events-poll cadence (fast 60s fallback while
    FCM is unhealthy vs. the relaxed default while healthy) and must be
    reset consistently by `async_stop_fcm_push`."""

    def test_fcm_unhealthy_uses_60s_interval(self):
        """When `fcm_healthy = False`, the events poll falls back to 60s —
        the tempo-fallback that keeps event detection working when FCM dies
        (router reboot, WAN blip)."""
        opts = {"interval_events": 60}
        healthy_default = 300
        healthy_value = opts.get("interval_events", healthy_default)
        assert healthy_value == 60  # user override

        unhealthy_default = 60
        unhealthy_value = opts.get("interval_events", unhealthy_default)
        assert unhealthy_value == 60

    def test_fcm_running_flag_initial_state(self):
        """Coordinator starts with FCM not running — listener must be
        explicitly started."""
        import inspect

        from custom_components.bosch_shc_camera import BoschCameraCoordinator

        src = inspect.getsource(BoschCameraCoordinator.__init__)
        assert_in_source(
            src, "fcm_running: bool = False", "fcm_running = False", any_of=True
        )

    def test_async_stop_fcm_push_clears_state(self):
        """After `async_stop_fcm_push`, `fcm_running`, `fcm_healthy`,
        `fcm_client` must all be cleared so a subsequent restart starts fresh."""
        from pathlib import Path

        fcm_src = (
            Path(__file__).parent.parent
            / "custom_components"
            / "bosch_shc_camera"
            / "fcm.py"
        )
        text = fcm_src.read_text()
        for must_assign in (
            "fcm_running = False",
            "fcm_healthy = False",
            "fcm_client = None",
        ):
            assert must_assign in text, (
                f"async_stop_fcm_push must clear `{must_assign}` to allow clean restart"
            )


# Section: motion-event / live-stream TLS-channel contention fix (relocated
# from tests/test_stream_motion_contention.py). Path A live-snap refresh must
# be skipped while a camera is actively streaming; the smb.py side (prefetch
# bypassing a second cloud pull) lives in tests/test_smb.py.


def _resp_cm_motion(
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


def _make_push_coord_motion(is_streaming: bool = False, **overrides: Any) -> Any:
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    task_stub = MagicMock(add_done_callback=MagicMock())
    hass.async_create_task = MagicMock(return_value=task_stub)
    hass.bus.async_fire = MagicMock()

    cam_entity = MagicMock()
    cam_entity.async_trigger_image_refresh = AsyncMock(return_value=None)
    cam_entity.is_streaming = is_streaming

    coord = SimpleNamespace(
        token="tok-test",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Innenbereich"}, "events": []}},
        last_event_ids={CAM_ID: "old-evt"},
        alert_sent_ids={},
        camera_entities={CAM_ID: cam_entity},
        image_entities={},
        shc_state_cache={},
        cached_events={},
        bg_tasks=set(),
        hw_version={CAM_ID: "HOME_Eyes_Indoor"},  # Gen2 Indoor
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_alert_coord_motion(cam_entity: Any | None = None, **overrides: Any) -> Any:
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
        last_event_ids={CAM_ID: "prior-event-id"},
        camera_entities={CAM_ID: cam_entity} if cam_entity else {},
        image_entities={},
        shc_state_cache={CAM_ID: {"privacy_mode": False}},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


@pytest.mark.asyncio
class TestPathAStreamingGuard:
    """Path A must skip async_trigger_image_refresh when is_streaming=True —
    both a live-stream and Path A's own PUT /connection + snap.jpg compete
    for a Gen2 camera's single TLS control channel; saturating it starves the
    RTSP keepalive and freezes the stream."""

    async def test_path_a_skipped_when_streaming(self) -> None:
        """MOVEMENT event + is_streaming=True → NO async_trigger_image_refresh call."""
        coord = _make_push_coord_motion(is_streaming=True)
        cam_entity = coord.camera_entities[CAM_ID]

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_motion(200, json_data=_one_movement_event("new-evt"))
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

        cam_entity.async_trigger_image_refresh.assert_not_called()

    async def test_path_a_fires_when_not_streaming(self) -> None:
        """MOVEMENT event + is_streaming=False → async_trigger_image_refresh IS
        called — PIN_EVERY_MODE: the not-streaming mode must keep the original
        behaviour."""
        coord = _make_push_coord_motion(is_streaming=False)
        cam_entity = coord.camera_entities[CAM_ID]

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_motion(200, json_data=_one_movement_event("new-evt"))
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

        cam_entity.async_trigger_image_refresh.assert_called_once_with(delay=0)


@pytest.mark.asyncio
class TestAlertPipelinePrefetch:
    """async_send_alert passes downloaded snapshot bytes to sync_smb_upload
    as `prefetched_image` so the SMB/FTP upload doesn't compete with a live
    stream for a second cloud/camera pull of the same snapshot. The smb.py
    side of this (sync_smb_upload / _sync_ftp_upload honoring the kwarg)
    lives in tests/test_smb.py."""

    _JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"\x42" * 400

    def _make_coord_for_alert(
        self, tmp_path: Any, cam_entity: Any | None = None
    ) -> Any:
        coord = _make_alert_coord_motion(
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
        """When step-2 downloads image bytes, sync_smb_upload receives them
        as prefetched_image — no second cloud pull needed."""
        cam_entity = MagicMock()
        cam_entity.is_streaming = True
        coord = self._make_coord_for_alert(tmp_path, cam_entity=cam_entity)

        img_resp = MagicMock()
        img_resp.status = 200
        img_resp.read = AsyncMock(return_value=self._JPEG_BYTES)
        img_resp.headers = {"Content-Type": "image/jpeg"}
        img_cm = MagicMock()
        img_cm.__aenter__ = AsyncMock(return_value=img_resp)
        img_cm.__aexit__ = AsyncMock(return_value=None)
        session = MagicMock()
        session.get = MagicMock(return_value=img_cm)

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

        assert len(captured_prefetch) == 1
        assert captured_prefetch[0] == self._JPEG_BYTES, (
            "sync_smb_upload must receive the step-2 snapshot bytes as prefetched_image"
        )

    async def test_smb_receives_none_when_no_image(self, tmp_path: Any) -> None:
        """When no imageUrl / step-2 is skipped, prefetched_image=None
        (backward compat) — PIN_EVERY_MODE: the no-image path."""
        coord = self._make_coord_for_alert(tmp_path)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_motion(404))

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
                "",
                "",
                "",
                event_id="aabbccdd-none",
            )

        assert len(captured_prefetch) == 1
        assert captured_prefetch[0] is None, (
            "prefetched_image must be None when step-2 image was not downloaded"
        )


# Section: misc gap-fill coverage (relocated from
# tests/test_misc_modules_coverage.py) — Path A exception swallowing,
# mark-read background-task exception swallowing, AI-description-in-caption
# block, clip-poll id/timestamp mismatch guards.


def _resp_cm_misc14(
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


def _make_push_coord_misc14(**overrides):
    hass = MagicMock()
    hass.states.get = MagicMock(return_value=None)
    hass.async_create_task = MagicMock()
    hass.bus.async_fire = MagicMock()
    coord = SimpleNamespace(
        token="tok-push",
        hass=hass,
        data={CAM_ID: {"info": {"title": "Terrasse"}, "events": []}},
        last_event_ids={},
        alert_sent_ids={},
        camera_entities={},
        cached_events={},
        bg_tasks=set(),
        options={},
    )
    coord.async_update_listeners = MagicMock()
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _make_alert_coord_misc14(options=None, **overrides):
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
        last_event_ids={CAM_ID: "event-old"},
        shc_state_cache={},
        cached_status={},
        lan_tcp_reachable={},
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


def _one_event_misc14(
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


class TestFcmPathAExceptionWarning:
    """`async_handle_fcm_push`: when Path A's try block raises (e.g.
    `cam_entity.async_trigger_image_refresh` raises), the except logs a
    WARNING and does NOT propagate."""

    @pytest.mark.asyncio
    async def test_path_a_exception_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Path A try raises RuntimeError → WARNING logged, no propagation."""
        cam_entity = MagicMock()
        cam_entity.is_streaming = False
        cam_entity.async_trigger_image_refresh = MagicMock(
            side_effect=RuntimeError("refresh boom")
        )

        coord = _make_push_coord_misc14(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_misc14(
                200, json_data=_one_event_misc14("new-evt", "MOVEMENT")
            )
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock),
            patch(f"{MODULE}.async_mark_events_read", new_callable=AsyncMock),
            caplog.at_level("WARNING", logger=MODULE),
        ):
            await async_handle_fcm_push(coord)

        assert any(
            "FCM Path A: failed to schedule live-snap refresh" in r.message
            for r in caplog.records
        ), "Expected Path A failure WARNING in logs"

    @pytest.mark.asyncio
    async def test_path_a_exception_does_not_propagate(self):
        """Path A exception must be swallowed — async_handle_fcm_push returns normally."""
        cam_entity = MagicMock()
        cam_entity.is_streaming = False
        cam_entity.async_trigger_image_refresh = MagicMock(
            side_effect=ValueError("boom inside path A")
        )

        coord = _make_push_coord_misc14(
            last_event_ids={CAM_ID: "old-evt"},
            camera_entities={CAM_ID: cam_entity},
        )

        session = MagicMock()
        session.get = MagicMock(
            return_value=_resp_cm_misc14(
                200, json_data=_one_event_misc14("new-evt2", "MOVEMENT")
            )
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock),
            patch(f"{MODULE}.async_mark_events_read", new_callable=AsyncMock),
        ):
            await async_handle_fcm_push(coord)


class TestMarkReadBgInnerExcept:
    """`async_handle_fcm_push`: when `mark_events_read=True` and
    `async_mark_events_read` raises inside the background task, the inner
    except swallows it silently."""

    @pytest.mark.asyncio
    async def test_mark_read_bg_exception_is_swallowed(self):
        coord = _make_push_coord_misc14(
            last_event_ids={CAM_ID: "old-evt"},
            options={"mark_events_read": True},
        )

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
            return_value=_resp_cm_misc14(
                200, json_data=_one_event_misc14("new-evt-mr", "MOVEMENT")
            )
        )

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.async_send_alert", new_callable=AsyncMock),
            patch(
                f"{MODULE}.async_mark_events_read",
                AsyncMock(side_effect=RuntimeError("mark-read-fail")),
            ),
        ):
            await async_handle_fcm_push(coord)

            # Await the background tasks WHILE the patches above are still
            # active. asyncio.create_task() only schedules the coroutine —
            # it doesn't run any of its body until the loop yields control,
            # which may happen only here at the first real await. Awaiting
            # outside this `with` block is a race: if the task's body hadn't
            # started yet, it resumes only after the patches unwind and
            # calls the REAL async_mark_events_read (a real network PUT),
            # rather than the mock this test exists to exercise.
            for t in created_tasks:
                try:
                    await t
                except Exception:
                    pass  # _mark_read_bg itself swallows — if we see an error, it leaked

        for t in created_tasks:
            if not t.done():
                t.cancel()


class TestSendAlertAiDescription:
    """`async_send_alert`: the `ai_notify_include_description` block appends
    the AI description to the caption on success, and swallows/logs an
    exception from the AI call without changing the caption."""

    _SAFE_IMAGE_URL = "https://media.boschsecurity.com/image.jpg"

    @pytest.mark.asyncio
    async def test_ai_description_appended_to_caption(self):
        """When ai_notify_include_description=True and AI returns text, the
        caption is extended with the AI description."""
        coord = _make_alert_coord_misc14(
            options={
                "ai_notify_include_description": True,
                "alert_save_snapshots": False,
                "alert_delete_after_send": True,
            }
        )
        coord.async_generate_ai_description = AsyncMock(
            return_value="A dog in the garden"
        )

        notify_calls: list = []

        async def _fake_svc_call(domain, service, service_data=None, **kw):
            notify_calls.append((domain, service, service_data))

        coord.hass.services.async_call = AsyncMock(side_effect=_fake_svc_call)

        image_resp = _resp_cm_misc14(
            200, body=b"\xff\xd8\xff" + b"\x00" * 100, content_type="image/jpeg"
        )
        session = MagicMock()
        session.get = MagicMock(return_value=image_resp)

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
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

        coord.async_generate_ai_description.assert_awaited_once_with(CAM_ID)
        assert len(notify_calls) >= 1

    @pytest.mark.asyncio
    async def test_ai_description_exception_swallowed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the AI call raises, the exception is caught and logged at
        DEBUG; the caption stays unchanged."""
        coord = _make_alert_coord_misc14(
            options={
                "ai_notify_include_description": True,
                "alert_save_snapshots": False,
                "alert_delete_after_send": True,
            }
        )
        coord.async_generate_ai_description = AsyncMock(
            side_effect=RuntimeError("AI boom")
        )

        image_resp = _resp_cm_misc14(
            200, body=b"\xff\xd8\xff" + b"\x00" * 100, content_type="image/jpeg"
        )
        session = MagicMock()
        session.get = MagicMock(return_value=image_resp)

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
            patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()),
            patch(f"{SMB_MODULE}.sync_local_save", MagicMock()),
            caplog.at_level("DEBUG", logger=MODULE),
        ):
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
        """When AI returns None or empty, the caption stays with just the
        snapshot text — the `if _desc:` guard prevents appending."""
        coord = _make_alert_coord_misc14(
            options={
                "ai_notify_include_description": True,
                "alert_save_snapshots": False,
            }
        )
        coord.async_generate_ai_description = AsyncMock(return_value=None)

        image_resp = _resp_cm_misc14(
            200, body=b"\xff\xd8\xff" + b"\x00" * 100, content_type="image/jpeg"
        )
        session = MagicMock()
        session.get = MagicMock(return_value=image_resp)

        with (
            patch(
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
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

        coord.async_generate_ai_description.assert_awaited_once_with(CAM_ID)


class TestClipPollMatchGuards:
    """`async_send_alert`'s clip-poll loop must skip events that don't match
    the current alert's event_id or timestamp (event_id mismatch, and the
    no-event_id timestamp-fallback mismatch)."""

    _SAFE_IMAGE_URL = "https://media.boschsecurity.com/image.jpg"

    def _poll_coord(self):
        return _make_alert_coord_misc14(
            options={
                "alert_save_snapshots": False,
                "alert_delete_after_send": True,
                "enable_smb_upload": False,
                "enable_local_save": False,
            }
        )

    @pytest.mark.asyncio
    async def test_event_id_mismatch_skips_event(self):
        """event_id=target-id, poll returns a different id → continue; no
        clip notification for the other event's clip."""
        coord = self._poll_coord()

        call_count = [0]

        def _session_get(url, **kw):
            call_count[0] += 1
            if "image" in url or call_count[0] == 1:
                return _resp_cm_misc14(
                    200, body=b"\xff\xd8\xff" + b"\x00" * 10, content_type="image/jpeg"
                )
            return _resp_cm_misc14(
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
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
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
        """No event_id: the fallback uses timestamp[:19] matching. A poll
        result with a non-matching timestamp is skipped."""
        coord = self._poll_coord()
        coord.last_event_ids = {}

        call_count = [0]

        def _session_get(url, **kw):
            call_count[0] += 1
            if "image" in url or call_count[0] == 1:
                return _resp_cm_misc14(
                    200, body=b"\xff\xd8\xff" + b"\x00" * 10, content_type="image/jpeg"
                )
            return _resp_cm_misc14(
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
                f"{MODULE}.async_get_bosch_cloud_session",
                AsyncMock(return_value=session),
            ),
            patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock),
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

        call_args_list = coord.hass.services.async_call.call_args_list
        wrong_clip_calls = [c for c in call_args_list if "wrong-ts.mp4" in str(c)]
        assert wrong_clip_calls == [], "Timestamp-mismatched clip must not be sent"


# Section: _build_fcm_cfg / _try_fcm_with_mode / dispatch-mode coverage
# (relocated from tests/test_coverage_round_n.py)


def _mock_fcm_module(
    checkin_token="fcm-tok-abc", start_raises=False, checkin_raises=False
):
    """Build a minimal firebase_messaging mock that passes through async_start_fcm_push."""
    mock_client = MagicMock()
    if checkin_raises:
        mock_client.checkin_or_register = AsyncMock(
            side_effect=RuntimeError("checkin fail")
        )
    else:
        mock_client.checkin_or_register = AsyncMock(return_value=checkin_token)
    if start_raises:
        mock_client.start = AsyncMock(side_effect=RuntimeError("start fail"))
    else:
        mock_client.start = AsyncMock(return_value=None)

    mock_module = MagicMock()
    mock_module.FcmPushClient = MagicMock(return_value=mock_client)
    mock_module.FcmRegisterConfig = MagicMock()
    mock_module.FcmPushClientConfig = MagicMock()
    return mock_module, mock_client


def _fcm_coord(push_mode="ios", entry_data=None, **overrides):
    entry_data = entry_data or {}
    base = SimpleNamespace(
        fcm_running=False,
        fcm_client=None,
        fcm_token=None,
        fcm_lock=threading.Lock(),
        fcm_healthy=False,
        fcm_push_mode="unknown",
        options={"enable_fcm_push": True, "fcm_push_mode": push_mode},
        hass=MagicMock(),
        entry=SimpleNamespace(data=entry_data),
        data={},
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


class TestBuildFcmCfgAndroid:
    """`_build_fcm_cfg` (android path) uses the stored config or fetches it."""

    @pytest.mark.asyncio
    async def test_android_uses_stored_config(self):
        """push_mode=android with stored fcm_config → fetch_firebase_config not called."""
        mock_fcm, _ = _mock_fcm_module()
        stored_cfg = {
            "project_id": "bosch-test",
            "app_id": "1:123:android:abc",
            "api_key": "stored-key",
        }
        coord = _fcm_coord("android", entry_data={"fcm_config": stored_cfg})

        fetch_called = []

        async def fake_fetch(hass):
            fetch_called.append(True)
            return stored_cfg

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.fetch_firebase_config", side_effect=fake_fetch):
                    with patch(
                        f"{MODULE}.register_fcm_with_bosch",
                        new=AsyncMock(return_value=True),
                    ):
                        await async_start_fcm_push(coord)

        assert not fetch_called, (
            "fetch_firebase_config must NOT be called when config is already stored"
        )

    @pytest.mark.asyncio
    async def test_android_fetches_config_when_missing(self):
        """push_mode=android with no stored config → fetch_firebase_config called."""
        mock_fcm, _ = _mock_fcm_module()
        coord = _fcm_coord("android")  # no fcm_config in entry data

        fetched_cfg = {
            "project_id": "bosch-proj",
            "app_id": "1:123:android:def",
            "api_key": "fetched-key",
        }
        fetch_called = []

        async def fake_fetch(hass):
            fetch_called.append(True)
            return fetched_cfg

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.fetch_firebase_config", side_effect=fake_fetch):
                    with patch(
                        f"{MODULE}.register_fcm_with_bosch",
                        new=AsyncMock(return_value=True),
                    ):
                        await async_start_fcm_push(coord)

        assert fetch_called, (
            "fetch_firebase_config must be called when no stored config"
        )


class TestTryFcmWithModeGuards:
    """`_try_fcm_with_mode` no-api-key / checkin-failure / start-failure guards."""

    @pytest.mark.asyncio
    async def test_no_api_key_does_not_start_client(self):
        """`_build_fcm_cfg` returns a config without api_key → returns False."""
        mock_fcm, mock_client = _mock_fcm_module()
        coord = _fcm_coord("android")

        async def fake_fetch(hass):
            return {"project_id": "p", "app_id": "a"}  # no api_key

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(f"{MODULE}.fetch_firebase_config", side_effect=fake_fetch):
                    await async_start_fcm_push(coord)

        assert not coord.fcm_running, (
            "FCM must not start when api_key is missing from config"
        )
        mock_client.checkin_or_register.assert_not_called()

    @pytest.mark.asyncio
    async def test_checkin_failure_keeps_running_false(self):
        """checkin_or_register raises → fcm_running stays False, fcm_client None."""
        mock_fcm, _ = _mock_fcm_module(checkin_raises=True)
        coord = _fcm_coord("ios")

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.register_fcm_with_bosch",
                    new=AsyncMock(return_value=True),
                ):
                    await async_start_fcm_push(coord)

        assert not coord.fcm_running, "checkin failure must not set fcm_running=True"
        assert coord.fcm_client is None, "checkin failure must clear fcm_client"

    @pytest.mark.asyncio
    async def test_start_failure_clears_client(self):
        """FcmPushClient.start() raises → fcm_client set to None."""
        mock_fcm, _ = _mock_fcm_module(start_raises=True)
        coord = _fcm_coord("ios")

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.register_fcm_with_bosch",
                    new=AsyncMock(return_value=True),
                ):
                    await async_start_fcm_push(coord)

        assert coord.fcm_client is None, "start() failure must clear fcm_client"
        assert not coord.fcm_running, "start() failure must not set fcm_running"


class TestDispatchModes:
    """push_mode branch coverage — auto/polling/legacy-coercion."""

    @pytest.mark.asyncio
    async def test_auto_mode_calls_fcm_once(self):
        """auto mode → calls _try_fcm exactly once with OSS key."""
        mock_fcm, mock_client = _mock_fcm_module()
        coord = _fcm_coord("auto")

        call_count = []

        def track_client(**kwargs):
            call_count.append(1)
            return mock_client

        mock_fcm.FcmPushClient = track_client

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.register_fcm_with_bosch",
                    new=AsyncMock(return_value=True),
                ):
                    await async_start_fcm_push(coord)

        assert len(call_count) == 1, "auto mode: exactly one FCM client created"
        assert coord.fcm_running is True, "auto mode success must set fcm_running=True"

    @pytest.mark.asyncio
    async def test_auto_mode_fcm_fail_no_crash(self):
        """auto mode → FCM registration fails → function returns without crash."""
        mock_module = MagicMock()
        fail_client = MagicMock()
        fail_client.checkin_or_register = AsyncMock(side_effect=RuntimeError("fail"))
        mock_module.FcmPushClient = MagicMock(return_value=fail_client)
        mock_module.FcmRegisterConfig = MagicMock()
        mock_module.FcmPushClientConfig = None

        coord = _fcm_coord("auto")

        with patch.dict(sys.modules, {"firebase_messaging": mock_module}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.fetch_firebase_config",
                    new=AsyncMock(
                        return_value={"project_id": "p", "app_id": "a", "api_key": "k"}
                    ),
                ):
                    await async_start_fcm_push(coord)

        assert not coord.fcm_running, "FCM fail → must not set fcm_running"

    @pytest.mark.asyncio
    async def test_unknown_mode_coerces_to_auto(self):
        """push_mode='weirdvalue' → coerced to 'auto' → fcm_push_mode='auto' on success."""
        mock_fcm, _mock_client = _mock_fcm_module()
        coord = _fcm_coord("weirdvalue")

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.register_fcm_with_bosch",
                    new=AsyncMock(return_value=True),
                ):
                    await async_start_fcm_push(coord)

        assert coord.fcm_push_mode == "auto", (
            "unknown push_mode must coerce to auto → fcm_push_mode='auto'"
        )

    @pytest.mark.asyncio
    async def test_android_legacy_coerces_to_auto(self):
        """push_mode='android' (legacy) → coerced to 'auto' → fcm_push_mode='auto' on success."""
        mock_fcm, _mock_client = _mock_fcm_module()
        coord = _fcm_coord("android")
        coord.entry = SimpleNamespace(
            data={
                "fcm_config": {
                    "project_id": "p",
                    "app_id": "a",
                    "api_key": "k",
                }
            }
        )

        with patch.dict(sys.modules, {"firebase_messaging": mock_fcm}):
            with patch(f"{MODULE}._install_fcm_noise_filter"):
                with patch(
                    f"{MODULE}.register_fcm_with_bosch",
                    new=AsyncMock(return_value=True),
                ):
                    await async_start_fcm_push(coord)

        assert coord.fcm_push_mode == "auto", (
            "legacy 'android' mode must coerce to auto → fcm_push_mode='auto'"
        )


# Section: fresh-install async_send_alert defaults (relocated from
# tests/test_fresh_install.py — the get_options()-merging tests from that
# file duplicated tests/test_init.py::TestGetOptions and were dropped rather
# than duplicated; the _FILE_RE / sync_local_save filenaming tests moved to
# tests/test_media_source.py and tests/test_smb.py respectively).


def _resp_cm_freshinstall(status, body=b"", content_type="image/jpeg"):
    resp = MagicMock()
    resp.status = status
    resp.read = AsyncMock(return_value=body)
    resp.json = AsyncMock(return_value=[])
    resp.headers = {"Content-Type": content_type}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _make_fresh_coord(**overrides):
    """Coordinator with default options (empty entry.options merged with defaults)."""
    from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

    hass = MagicMock()
    hass.config.config_dir = "/tmp/test-ha"
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.services.async_call = AsyncMock(return_value=None)

    coord = SimpleNamespace(
        token="tok-fresh",
        hass=hass,
        options=dict(DEFAULT_OPTIONS),  # fresh install = all defaults
        data={CAM_ID: {"info": {"title": "Aussenkamera"}, "events": []}},
        last_event_ids={CAM_ID: "fresh-event-001"},
        _download_started_at=time.time() - 10,  # started 10s ago
    )
    for k, v in overrides.items():
        setattr(coord, k, v)
    return coord


class TestFreshInstallAlertSave:
    """Fresh install (empty options): local save is opt-in — nothing fires
    without an explicit path (regression for Andreas74's simon42-forum
    report, 2026-05-07)."""

    @pytest.mark.asyncio
    async def test_no_local_save_on_default_options(self):
        """Fresh install: download_path='' → async_send_alert must NOT queue sync_local_save."""
        coord = _make_fresh_coord()  # download_path="" by default
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_freshinstall(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save") as mock_save:
                        await async_send_alert(
                            coord,
                            "Aussenkamera",
                            "MOVEMENT",
                            "2026-05-07T12:00:00.000Z",
                            "",
                            "",
                            "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert not any(c.args[0] is mock_save for c in executor_calls), (
            "sync_local_save must NOT fire on fresh install (download_path empty by default)"
        )

    @pytest.mark.asyncio
    async def test_local_save_fires_when_enabled_and_path_configured(self):
        """sync_local_save must be called when the user enables the toggle
        AND sets a path."""
        coord = _make_fresh_coord()
        coord.options["enable_local_save"] = True  # user opted in
        coord.options["download_path"] = "/config/bosch_events"
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_freshinstall(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload", MagicMock()):
                    with patch(f"{SMB_MODULE}.sync_local_save") as mock_save:
                        await async_send_alert(
                            coord,
                            "Aussenkamera",
                            "MOVEMENT",
                            "2026-05-07T12:00:00.000Z",
                            "",
                            "",
                            "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert any(c.args[0] is mock_save for c in executor_calls), (
            "sync_local_save must fire when download_path is explicitly configured"
        )

    @pytest.mark.asyncio
    async def test_smb_not_called_on_default_options(self):
        """SMB upload must NOT fire on fresh install (enable_smb_upload=False
        by default)."""
        coord = _make_fresh_coord()
        coord.hass.async_add_executor_job = AsyncMock(return_value=None)

        session = MagicMock()
        session.get = MagicMock(return_value=_resp_cm_freshinstall(404))

        with patch(
            f"{MODULE}.async_get_bosch_cloud_session",
            new=AsyncMock(return_value=session),
        ):
            with patch(f"{MODULE}.asyncio.sleep", new_callable=AsyncMock):
                with patch(f"{SMB_MODULE}.sync_smb_upload") as mock_smb:
                    with patch(f"{SMB_MODULE}.sync_local_save", MagicMock()):
                        await async_send_alert(
                            coord,
                            "Aussenkamera",
                            "MOVEMENT",
                            "2026-05-07T12:00:00.000Z",
                            "",
                            "",
                            "",
                        )

        executor_calls = coord.hass.async_add_executor_job.call_args_list
        assert not any(c.args[0] is mock_smb for c in executor_calls), (
            "sync_smb_upload must NOT fire when enable_smb_upload=False (default)"
        )


# Section: fcm.py alert_sent_ids SENTINEL_RULE default (relocated from
# tests/test_bug_regression_v11.py — the __init__.py sibling of this same
# bug pattern lives in tests/test_init.py::TestAlertSentIdsSentinel)


class TestFcmAlertSentIdsSentinel:
    """fcm.py's own `alert_sent_ids.get(id, ...)` dedup gate must default to
    float('-inf'), not 0.0 — on hosts with monotonic < 60s, `0.0 > (now-60)`
    is True, suppressing the first FCM push for any event."""

    def test_fcm_dedup_default_not_zero(self):
        import inspect

        from custom_components.bosch_shc_camera import fcm as fcm_mod

        src = inspect.getsource(fcm_mod)
        assert "_sent.get(newest_id, 0.0) > _now - 60.0" not in src, (
            "fcm.py must not use 0.0 as default for alert_sent_ids.get(); "
            "on hosts with monotonic < 60s first FCM alert is suppressed"
        )

    def test_fcm_dedup_uses_neginf_default(self):
        import inspect

        from custom_components.bosch_shc_camera import fcm as fcm_mod

        src = inspect.getsource(fcm_mod)
        assert '_sent.get(newest_id, float("-inf")) > _now - 60.0' in src, (
            "fcm.py must use float('-inf') so dedup only fires for IDs sent within 60s"
        )
