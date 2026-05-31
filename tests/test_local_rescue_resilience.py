"""Regression tests for the resilient LOCAL rescue burst.

Live incident 2026-05-31 (Indoor Gen2): Bosch rotated the LOCAL
RTSP session creds. The rescue path re-issued a fresh PUT /connection, but the
new TLS proxy hit "SSL UNEXPECTED_EOF" / "Connection reset by peer" because the
camera was still tearing the old session down. The rescue made exactly ONE
attempt, then gave up — leaving go2rtc + HA Stream pinned to the dead proxy
port. Consumers saw "connection refused" / "wrong user/pass" → frozen image
until a manual integration reload.

Fix: the rescue burst self-retries with backoff (up to 3 attempts) instead of
relying on a fresh stream-worker error to drive attempt 2 (which never comes —
the rescue tore the stream down, so nothing generates a new error). The outer
`_local_rescue_attempts < 1` guard still claims one burst per cam at a time and
decays after the TTL, so a genuine LAN-auth fault can't loop forever.

Pins: input (sequence of try_live_connection results) → output (number of
attempts + final state).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

CAM_ID = "11111111-1111-1111-1111-111111111111"


def _make_coord(try_results):
    """Coordinator stub for `_handle_stream_worker_error`.

    `try_results` is the sequence returned by successive `try_live_connection`
    calls (None = failed attempt, dict = success).
    """
    coord = SimpleNamespace(
        _stream_worker_dispatch_pending={CAM_ID},
        record_stream_error=MagicMock(),
        get_model_config=MagicMock(return_value=SimpleNamespace(max_stream_errors=5)),
        _live_connections={CAM_ID: {"_connection_type": "LOCAL"}},
        _stream_error_count={CAM_ID: 0},
        _stream_fell_back={},
        _local_rescue_attempts={},
        _local_rescue_at={},
        _stop_tls_proxy=AsyncMock(),
        try_live_connection=AsyncMock(side_effect=list(try_results)),
        hass=MagicMock(),
    )
    return coord


@pytest.mark.asyncio
async def test_rescue_retries_until_success():
    """Two transient failures then success → 3 attempts, proxy restarted each."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    coord = _make_coord([None, None, {"_connection_type": "LOCAL"}])
    with patch("custom_components.bosch_shc_camera.asyncio.sleep", new=AsyncMock()):
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_ID, "RTSP/1.0 401 Unauthorized"
        )
    assert coord.try_live_connection.await_count == 3
    # Fresh proxy on every attempt (the new-port-per-restart design forces a
    # fresh RTSP URL so go2rtc re-registration carries the new creds).
    assert coord._stop_tls_proxy.await_count == 3


@pytest.mark.asyncio
async def test_rescue_stops_at_first_success():
    """Success on attempt 1 → exactly one attempt, no wasted retries."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    coord = _make_coord([{"_connection_type": "LOCAL"}])
    with patch(
        "custom_components.bosch_shc_camera.asyncio.sleep", new=AsyncMock()
    ) as sleep_mock:
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_ID, "401 Unauthorized"
        )
    assert coord.try_live_connection.await_count == 1
    sleep_mock.assert_not_awaited()  # no backoff sleep when first attempt wins


@pytest.mark.asyncio
async def test_rescue_caps_at_three_attempts():
    """Persistent failure → bounded at 3 attempts (no infinite loop)."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    coord = _make_coord([None, None, None, None, None])
    with patch("custom_components.bosch_shc_camera.asyncio.sleep", new=AsyncMock()):
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_ID, "401 Unauthorized"
        )
    assert coord.try_live_connection.await_count == 3
    # Burst claimed exactly once; stays claimed (decays via TTL) so a genuine
    # LAN-auth fault cannot re-enter the loop until the TTL window passes.
    assert coord._local_rescue_attempts[CAM_ID] == 1


@pytest.mark.asyncio
async def test_rescue_backoff_between_failed_attempts():
    """A backoff sleep fires between failed attempts, not after the last one."""
    from custom_components.bosch_shc_camera import BoschCameraCoordinator

    coord = _make_coord([None, None, None])
    with patch(
        "custom_components.bosch_shc_camera.asyncio.sleep", new=AsyncMock()
    ) as sleep_mock:
        await BoschCameraCoordinator._handle_stream_worker_error(
            coord, CAM_ID, "401 Unauthorized"
        )
    # 3 attempts → sleeps after attempt 1 and 2 only (not after the final one).
    assert sleep_mock.await_count == 2
