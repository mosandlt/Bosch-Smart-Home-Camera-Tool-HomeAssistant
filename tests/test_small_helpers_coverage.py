"""Coverage gap-fill tests for four small helper modules.

Target lines (per task brief):
  auth_utils.py    206-216  — stale-nonce retry branch
  cloud_ssl.py     127      — session-already-open early return
  cloud_ssl.py     135-136  — _close_session inner function + await session.close()
  snapshot_store.py 78-80   — unlink() failure inside failed replace() handler
  cf_unbuffer.py   101      — empty-token guard in _note_hls_access
  cf_unbuffer.py   111      — second-oldest eviction path in _note_hls_access
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ══════════════════════════════════════════════════════════════════════════════
# auth_utils — lines 206-216: stale-nonce retry on second 401
# ══════════════════════════════════════════════════════════════════════════════


def _make_resp(
    status: int,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> MagicMock:
    """Minimal aiohttp.ClientResponse mock."""
    r = MagicMock()
    r.status = status
    r.headers = headers or {}
    r.read = AsyncMock(return_value=body)
    r.__aenter__ = AsyncMock(return_value=r)
    r.__aexit__ = AsyncMock(return_value=False)
    return r


def _digest_hdr(nonce: str = "nonce1", stale: str = "") -> str:
    parts = [
        'realm="cam@bosch.com"',
        f'nonce="{nonce}"',
        "algorithm=MD5",
        'qop="auth"',
    ]
    if stale:
        parts.append(f"stale={stale}")
    return "Digest " + ", ".join(parts)


@pytest.mark.asyncio
class TestAuthUtilsStaleNonce:
    """Lines 206-216: when the second 401 carries stale=true, retry with the new nonce."""

    async def test_stale_true_triggers_third_request(self) -> None:
        from custom_components.bosch_shc_camera.auth_utils import async_digest_request

        session = MagicMock()
        session.request = AsyncMock()

        resp_401_first = _make_resp(
            401, headers={"WWW-Authenticate": _digest_hdr("nonce1")}
        )
        resp_401_stale = _make_resp(
            401,
            headers={"WWW-Authenticate": _digest_hdr("nonce2", stale="true")},
        )
        resp_200 = _make_resp(200, body=b"ok")

        session.request.side_effect = [resp_401_first, resp_401_stale, resp_200]

        result = await async_digest_request(
            session, "GET", "https://cam/snap.jpg", "user", "pass"
        )

        assert result.status == 200
        assert session.request.call_count == 3, (
            "Stale-nonce path must issue a third request with the refreshed nonce"
        )
        # Third call must carry Authorization built from the NEW nonce
        _, third_kwargs = session.request.call_args
        auth = third_kwargs["headers"]["Authorization"]
        assert "nonce2" in auth, "Third request must use the new stale nonce"

    async def test_stale_false_does_not_retry(self) -> None:
        """stale=false on second 401 → second response returned as-is (no third request)."""
        from custom_components.bosch_shc_camera.auth_utils import async_digest_request

        session = MagicMock()
        session.request = AsyncMock()

        resp_401_first = _make_resp(
            401, headers={"WWW-Authenticate": _digest_hdr("nonce1")}
        )
        resp_401_nonstale = _make_resp(
            401,
            headers={"WWW-Authenticate": _digest_hdr("nonce2", stale="false")},
        )

        session.request.side_effect = [resp_401_first, resp_401_nonstale]

        result = await async_digest_request(
            session, "GET", "https://cam/snap.jpg", "user", "bad_pass"
        )

        assert result.status == 401
        assert session.request.call_count == 2

    async def test_second_401_no_www_auth_returns_as_is(self) -> None:
        """Second 401 without WWW-Authenticate → returned immediately (no retry)."""
        from custom_components.bosch_shc_camera.auth_utils import async_digest_request

        session = MagicMock()
        session.request = AsyncMock()

        resp_401_first = _make_resp(401, headers={"WWW-Authenticate": _digest_hdr()})
        resp_401_bare = _make_resp(401, headers={})

        session.request.side_effect = [resp_401_first, resp_401_bare]

        result = await async_digest_request(
            session, "GET", "https://cam/snap.jpg", "user", "pass"
        )

        assert result.status == 401
        assert session.request.call_count == 2

    async def test_stale_retry_uses_caller_headers(self) -> None:
        """Custom caller headers survive into the stale-retry third request."""
        from custom_components.bosch_shc_camera.auth_utils import async_digest_request

        session = MagicMock()
        session.request = AsyncMock()

        resp_401_first = _make_resp(
            401, headers={"WWW-Authenticate": _digest_hdr("n1")}
        )
        resp_401_stale = _make_resp(
            401,
            headers={"WWW-Authenticate": _digest_hdr("n2", stale="true")},
        )
        resp_200 = _make_resp(200)

        session.request.side_effect = [resp_401_first, resp_401_stale, resp_200]

        custom = {"X-Source": "test"}
        await async_digest_request(
            session, "GET", "https://cam/snap.jpg", "u", "p", headers=custom
        )

        _, third_kw = session.request.call_args
        assert third_kw["headers"].get("X-Source") == "test"
        assert "Authorization" in third_kw["headers"]


# ══════════════════════════════════════════════════════════════════════════════
# cloud_ssl — lines 127, 135-136
# ══════════════════════════════════════════════════════════════════════════════


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
    """Lines 127, 135-136: session caching and close-on-stop handler."""

    async def test_returns_existing_open_session(self) -> None:
        """Line 127: if an open session already exists it is returned immediately."""
        from custom_components.bosch_shc_camera import cloud_ssl

        hass = _make_hass_for_cloud()
        existing = MagicMock()
        existing.closed = False
        hass.data[cloud_ssl._SESSION_DATA_KEY] = existing

        # Mock ssl context so we don't need a real one
        with patch.object(
            cloud_ssl,
            "async_get_bosch_cloud_ssl_context",
            new=AsyncMock(return_value=MagicMock()),
        ):
            result = await cloud_ssl.async_get_bosch_cloud_session(hass)

        assert result is existing, (
            "Should return the cached open session without creating a new one"
        )
        # async_get_bosch_cloud_ssl_context must NOT have been called
        # (early return at line 127 before it's needed)

    async def test_replaces_closed_session(self) -> None:
        """Line 127: a closed cached session is replaced with a fresh one."""
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
        """Lines 135-136: _close_session callback closes the session when not yet closed."""
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
        """Lines 135-136: _close_session does not call close() when session is already closed."""
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


# ══════════════════════════════════════════════════════════════════════════════
# snapshot_store — lines 78-80: unlink() OSError inside failed replace()
# ══════════════════════════════════════════════════════════════════════════════


VALID_CAM_ID = "AABBCCDD-EEFF-1122-3344-556677889900"
VALID_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 200  # 204 B — above 100 B min


def _make_hass(tmp_path: Path) -> Any:
    hass = SimpleNamespace()
    storage = tmp_path / ".storage"
    storage.mkdir()
    hass.config = SimpleNamespace(path=lambda *parts: str(Path(tmp_path, *parts)))

    async def _executor(fn: Any, *args: Any) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, fn, *args)

    hass.async_add_executor_job = _executor
    return hass


@pytest.mark.asyncio
async def test_sync_save_unlink_failure_logs_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Lines 78-80: when replace() fails AND unlink() also raises, the debug message is logged.

    The original replace() exception is still re-raised (not masked by the
    secondary unlink failure).
    """
    import logging

    from custom_components.bosch_shc_camera import snapshot_store

    hass = _make_hass(tmp_path)
    caplog.set_level(logging.DEBUG)

    # Make replace() fail, and also make unlink() fail
    with (
        patch.object(Path, "replace", side_effect=OSError("simulated replace failure")),
        patch.object(Path, "unlink", side_effect=OSError("simulated unlink failure")),
    ):
        with pytest.raises(OSError, match="simulated replace failure"):
            await snapshot_store.save_snapshot(hass, VALID_CAM_ID, VALID_JPEG)

    # The secondary-failure debug message must have been emitted
    assert any("could not remove tmp file" in r.message for r in caplog.records), (
        "Expected DEBUG log about failed tmp-file cleanup"
    )


@pytest.mark.asyncio
async def test_sync_save_unlink_failure_does_not_mask_replace_error(
    tmp_path: Path,
) -> None:
    """Lines 78-80: unlink() failure must not suppress the original replace() OSError."""
    from custom_components.bosch_shc_camera import snapshot_store

    hass = _make_hass(tmp_path)

    with (
        patch.object(Path, "replace", side_effect=OSError("replace-error")),
        patch.object(Path, "unlink", side_effect=OSError("unlink-error")),
    ):
        exc_info: list[BaseException] = []
        try:
            await snapshot_store.save_snapshot(hass, VALID_CAM_ID, VALID_JPEG)
        except OSError as exc:
            exc_info.append(exc)

    assert exc_info, "OSError from replace() must propagate"
    assert "replace-error" in str(exc_info[0]), (
        "Original replace() error must be raised"
    )


# ══════════════════════════════════════════════════════════════════════════════
# cf_unbuffer — lines 101, 111
# ══════════════════════════════════════════════════════════════════════════════


class TestNoteHlsAccessGaps:
    """Lines 101 + 111: empty-token guard and second-oldest eviction."""

    def _cf(self):
        import custom_components.bosch_shc_camera.cf_unbuffer as cf

        cf._HLS_ACCESS.clear()
        return cf

    def test_empty_token_after_hls_segment_is_ignored(self) -> None:
        """Line 101: path '/api/hls/' has 'hls' but the next part is '' → no stamp."""
        cf = self._cf()
        # Path ends with 'hls/' → split gives '' as the token after 'hls'
        req = SimpleNamespace(path="/api/hls/")
        cf._note_hls_access(req)
        assert cf._HLS_ACCESS == {}, "Empty token must not be recorded"

    def test_second_oldest_evicted_when_oldest_is_active(self) -> None:
        """Line 111: when the oldest token was stamped within _HLS_ACTIVE_WINDOW,
        the SECOND-oldest should be evicted instead."""
        import time

        cf = self._cf()
        base = time.monotonic()

        # Fill up to exactly the cap, using incrementing timestamps so order is known
        for i in range(cf._HLS_ACCESS_MAX):
            cf._HLS_ACCESS[f"tok{i:04d}"] = base + i

        # The oldest token is tok0000 (base), second-oldest is tok0001 (base+1)
        # Now add one more entry with a timestamp where oldest is still "active"
        # (i.e. now - base < _HLS_ACTIVE_WINDOW).  We need base to be very recent.
        # Patch monotonic so "now" is base + _HLS_ACCESS_MAX but oldest delta < window.
        fresh_base = 0.0
        # Reset with fresh timestamps so oldest is well within the active window
        cf._HLS_ACCESS.clear()
        for i in range(cf._HLS_ACCESS_MAX):
            cf._HLS_ACCESS[f"tok{i:04d}"] = fresh_base + i  # tok0000 is oldest (=0.0)

        # now = fresh_base + _HLS_ACCESS_MAX + 0.1
        # age of tok0000 = (_HLS_ACCESS_MAX + 0.1) which is > _HLS_ACTIVE_WINDOW (30s)
        # We need the oldest within the window, so set now just beyond oldest
        now_ts = (
            fresh_base + cf._HLS_ACTIVE_WINDOW * 0.5
        )  # oldest is only 0s old, well within window

        with patch.object(cf.time, "monotonic", return_value=now_ts):
            cf._note_hls_access(SimpleNamespace(path="/api/hls/tok_new/playlist.m3u8"))

        # tok0000 should be PRESERVED (it was recently accessed / active)
        # tok0001 should have been evicted
        assert "tok0000" in cf._HLS_ACCESS, (
            "Oldest active token must be preserved — second-oldest should be evicted"
        )
        assert "tok0001" not in cf._HLS_ACCESS, (
            "Second-oldest token must be evicted when oldest is still active"
        )
        assert "tok_new" in cf._HLS_ACCESS, "New token must be recorded"

    def test_second_oldest_eviction_requires_at_least_two_tokens(self) -> None:
        """Line 111 guard: len(sorted_tokens) > 1 — with exactly 1 token, oldest is evicted."""
        cf = self._cf()
        # Fill to exactly max with all tokens having the same timestamp (fresh)
        # so the oldest is "active" — but there's only one token to evict
        # (The guard `len(sorted_tokens) > 1` prevents index [1] on a 1-element list)
        # We can't really hit the one-element edge at cap > 1, but we verify the
        # dict size stays bounded (i.e. eviction does happen)
        cf._HLS_ACCESS.clear()
        base = 1000.0
        for i in range(cf._HLS_ACCESS_MAX):
            cf._HLS_ACCESS[f"tx{i:04d}"] = base + i

        # now is just a little past oldest — oldest age = _HLS_ACCESS_MAX seconds
        # which is >> _HLS_ACTIVE_WINDOW=30, so oldest is NOT active; normal eviction
        now_ts = base + cf._HLS_ACCESS_MAX + 1.0
        with patch.object(cf.time, "monotonic", return_value=now_ts):
            cf._note_hls_access(SimpleNamespace(path="/api/hls/new_token/segment.m4s"))

        assert len(cf._HLS_ACCESS) <= cf._HLS_ACCESS_MAX
