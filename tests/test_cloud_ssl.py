"""Tests for cloud_ssl.py — cached aiohttp ClientSession using the Bosch
cloud SSL context, plus its close-on-HA-stop handler.

Covers:
  - the open/cached-session fast path and the closed-session replacement path
  - the _close_session callback registered via hass.bus.async_listen_once
  - the double-checked-locking guard against concurrent callers each
    building their own ClientSession (bug-hunt 2026-07-03 regression)
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_hass_for_cloud() -> MagicMock:
    """Minimal hass mock for cloud_ssl tests."""
    hass = MagicMock()
    hass.data = {}
    hass.async_add_executor_job = AsyncMock()
    hass.bus = MagicMock()
    hass.bus.async_listen_once = MagicMock()
    return hass


@pytest.mark.asyncio
class TestCloudSslSession:
    """Session caching and close-on-stop handler."""

    async def test_returns_existing_open_session(self) -> None:
        """An open cached session is returned immediately, without rebuilding
        the SSL context."""
        from custom_components.bosch_shc_camera import cloud_ssl

        hass = _make_hass_for_cloud()
        existing = MagicMock()
        existing.closed = False
        hass.data[cloud_ssl._SESSION_DATA_KEY] = existing

        with patch.object(
            cloud_ssl,
            "async_get_bosch_cloud_ssl_context",
            new=AsyncMock(return_value=MagicMock()),
        ):
            result = await cloud_ssl.async_get_bosch_cloud_session(hass)

        assert result is existing, (
            "Should return the cached open session without creating a new one"
        )

    async def test_replaces_closed_session(self) -> None:
        """A closed cached session is replaced with a fresh one."""
        from custom_components.bosch_shc_camera import cloud_ssl

        hass = _make_hass_for_cloud()
        closed_session = MagicMock()
        closed_session.closed = True
        hass.data[cloud_ssl._SESSION_DATA_KEY] = closed_session

        fake_ssl_ctx = MagicMock()
        fake_connector = MagicMock()
        fake_new_session = MagicMock()
        fake_new_session.closed = False

        with (
            patch.object(
                cloud_ssl,
                "async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=fake_ssl_ctx),
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.TCPConnector",
                return_value=fake_connector,
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.ClientSession",
                return_value=fake_new_session,
            ),
        ):
            result = await cloud_ssl.async_get_bosch_cloud_session(hass)

        assert result is fake_new_session

    async def test_close_session_callback_closes_open_session(self) -> None:
        """The _close_session callback closes the session when not yet closed."""
        from custom_components.bosch_shc_camera import cloud_ssl

        hass = _make_hass_for_cloud()

        # Capture the callback registered with async_listen_once
        captured_cb: list[Any] = []

        def capture_listen_once(event_type: str, cb: Any) -> None:
            captured_cb.append(cb)

        hass.bus.async_listen_once.side_effect = capture_listen_once

        fake_ssl_ctx = MagicMock()
        fake_connector = MagicMock()
        fake_session = MagicMock()
        fake_session.closed = False
        fake_session.close = AsyncMock()

        with (
            patch.object(
                cloud_ssl,
                "async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=fake_ssl_ctx),
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.TCPConnector",
                return_value=fake_connector,
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.ClientSession",
                return_value=fake_session,
            ),
        ):
            # Ensure no cached session so a new one gets created
            hass.data.pop(cloud_ssl._SESSION_DATA_KEY, None)
            await cloud_ssl.async_get_bosch_cloud_session(hass)

        assert captured_cb, "async_listen_once must register a callback"
        cb = captured_cb[0]

        # Invoke the callback (simulating HA stop event) — must close the session
        await cb(MagicMock())
        fake_session.close.assert_awaited_once()

    async def test_concurrent_calls_create_only_one_session(self) -> None:
        """Regression (bug-hunt 2026-07-03): async_get_bosch_cloud_session had
        no lock, unlike its sibling async_get_bosch_cloud_ssl_context which
        already used double-checked locking. HA starts camera/switch/light/
        sensor platforms concurrently at integration setup, all calling this
        helper — without a lock, two concurrent callers both see no cached
        session, both build a fresh ClientSession+TCPConnector, and the
        second write silently clobbers the first (discarded session stays
        open, idle, until HA stop — log noise + wasted connector). Fix:
        mirror the existing _SSL_CONTEXT_LOCK double-checked-locking pattern.
        """
        from custom_components.bosch_shc_camera import cloud_ssl

        hass = _make_hass_for_cloud()
        hass.data.pop(cloud_ssl._SESSION_DATA_KEY, None)

        # Force a real yield point between the cache-miss check and the
        # session being stored, so two concurrent callers actually interleave
        # instead of one finishing before the other starts.
        release = asyncio.Event()

        async def _slow_ssl_context(_hass: Any) -> MagicMock:
            await release.wait()
            return MagicMock()

        created_sessions: list[MagicMock] = []

        def _make_session(*_args: Any, **_kwargs: Any) -> MagicMock:
            session = MagicMock()
            session.closed = False
            created_sessions.append(session)
            return session

        with (
            patch.object(
                cloud_ssl, "async_get_bosch_cloud_ssl_context", new=_slow_ssl_context
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.TCPConnector",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.ClientSession",
                side_effect=_make_session,
            ),
        ):
            task_a = asyncio.create_task(cloud_ssl.async_get_bosch_cloud_session(hass))
            task_b = asyncio.create_task(cloud_ssl.async_get_bosch_cloud_session(hass))
            await asyncio.sleep(0)  # let both reach the awaited ssl-context call
            release.set()
            result_a, result_b = await asyncio.gather(task_a, task_b)

        assert len(created_sessions) == 1, (
            "concurrent callers must build exactly one ClientSession, not one each"
        )
        assert result_a is result_b
        assert hass.data[cloud_ssl._SESSION_DATA_KEY] is result_a

    async def test_close_session_callback_skips_already_closed(self) -> None:
        """_close_session does not call close() when the session is already closed."""
        from custom_components.bosch_shc_camera import cloud_ssl

        hass = _make_hass_for_cloud()

        captured_cb: list[Any] = []

        def capture_listen_once(event_type: str, cb: Any) -> None:
            captured_cb.append(cb)

        hass.bus.async_listen_once.side_effect = capture_listen_once

        fake_ssl_ctx = MagicMock()
        fake_connector = MagicMock()
        fake_session = MagicMock()
        # Session already closed when the HA-stop event fires
        fake_session.closed = True
        fake_session.close = AsyncMock()

        with (
            patch.object(
                cloud_ssl,
                "async_get_bosch_cloud_ssl_context",
                new=AsyncMock(return_value=fake_ssl_ctx),
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.TCPConnector",
                return_value=fake_connector,
            ),
            patch(
                "custom_components.bosch_shc_camera.cloud_ssl.aiohttp.ClientSession",
                return_value=fake_session,
            ),
        ):
            hass.data.pop(cloud_ssl._SESSION_DATA_KEY, None)
            await cloud_ssl.async_get_bosch_cloud_session(hass)

        assert captured_cb
        await captured_cb[0](MagicMock())
        fake_session.close.assert_not_awaited()
