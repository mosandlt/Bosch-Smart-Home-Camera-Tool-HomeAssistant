"""Tests for tick_bootstrap.py — one-time feature-flags/protocol-version
checks inside _async_update_data (Phase 2 step 3 of the coordinator
rewrite). Direct unit tests in isolation; the existing integration-level
tests exercising the full _async_update_data (test_init.py) already cover
end-to-end wiring."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from custom_components.bosch_shc_camera.tick_bootstrap import (
    ensure_feature_flags,
    ensure_protocol_checked,
)

HEADERS = {"Authorization": "Bearer tok", "Accept": "application/json"}


def _make_resp(status: int, json_data=None):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session(resp):
    session = MagicMock()
    session.get = MagicMock(return_value=resp)
    return session


def _make_coord(**overrides):
    base = dict(
        _feature_flags={},
        _protocol_checked=False,
        _integration_version="14.5.9",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEnsureFeatureFlags:
    @pytest.mark.asyncio
    async def test_already_set_is_a_noop(self):
        coord = _make_coord(_feature_flags={"already": "set"})
        session = _make_session(_make_resp(200, {"new": "data"}))

        await ensure_feature_flags(coord, session, HEADERS)

        session.get.assert_not_called()
        assert coord._feature_flags == {"already": "set"}

    @pytest.mark.asyncio
    async def test_200_caches_flags(self):
        coord = _make_coord()
        session = _make_session(_make_resp(200, {"flag_a": True}))

        await ensure_feature_flags(coord, session, HEADERS)

        assert coord._feature_flags == {"flag_a": True}

    @pytest.mark.asyncio
    async def test_non_200_leaves_flags_unset(self):
        coord = _make_coord()
        session = _make_session(_make_resp(500))

        await ensure_feature_flags(coord, session, HEADERS)

        assert coord._feature_flags == {}

    @pytest.mark.asyncio
    async def test_client_error_is_swallowed(self):
        coord = _make_coord()
        resp = MagicMock()
        resp.__aenter__ = AsyncMock(side_effect=aiohttp.ClientError("x"))
        resp.__aexit__ = AsyncMock(return_value=None)
        session = _make_session(resp)

        await ensure_feature_flags(coord, session, HEADERS)  # must not raise

        assert coord._feature_flags == {}

    @pytest.mark.asyncio
    async def test_timeout_is_swallowed(self):
        coord = _make_coord()
        resp = MagicMock()
        resp.__aenter__ = AsyncMock(side_effect=TimeoutError())
        resp.__aexit__ = AsyncMock(return_value=None)
        session = _make_session(resp)

        await ensure_feature_flags(coord, session, HEADERS)  # must not raise

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self):
        """CancelledError must NOT be swallowed — re-raised so task
        cancellation (e.g. HA unload/reload) actually takes effect."""
        coord = _make_coord()
        resp = MagicMock()
        resp.__aenter__ = AsyncMock(side_effect=asyncio.CancelledError())
        resp.__aexit__ = AsyncMock(return_value=None)
        session = _make_session(resp)

        with pytest.raises(asyncio.CancelledError):
            await ensure_feature_flags(coord, session, HEADERS)


class TestEnsureProtocolChecked:
    @pytest.mark.asyncio
    async def test_already_checked_is_a_noop(self):
        coord = _make_coord(_protocol_checked=True)
        session = _make_session(_make_resp(200, {"state": "SUPPORTED"}))

        await ensure_protocol_checked(coord, session, HEADERS)

        session.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_flag_ends_up_true_even_when_fetch_fails(self):
        """The flag must be True after a failed fetch too — a failed check
        must not retry every tick. NOTE: this only proves the end-state,
        not the ORDERING (flag-set-before-fetch vs. after) — a
        THREE_PER_ISSUE_PER_CHANGE bug-hunt agent correctly pointed out
        that distinguishing those would need a genuine concurrent-call
        scenario. That's moot here: `_async_update_data` (this function's
        only caller) is serialized per-coordinator by HA's
        DataUpdateCoordinator debouncer, so two calls can never race on
        this flag — verified by the same bug-hunt pass. The source ordering
        itself (flag set immediately after the guard, before the try/fetch
        block) is pinned by reading `tick_bootstrap.py` directly, not by
        this test."""
        coord = _make_coord()
        session = _make_session(_make_resp(500))

        await ensure_protocol_checked(coord, session, HEADERS)

        assert coord._protocol_checked is True

    @pytest.mark.asyncio
    async def test_malformed_json_body_is_swallowed(self):
        """A more realistic failure than a raw session.get() error: the
        response's own .json() call raising (malformed/empty body) must
        also be caught by the broad `except Exception`."""
        coord = _make_coord()
        resp = _make_resp(200)
        resp.json = AsyncMock(side_effect=ValueError("bad json"))
        session = _make_session(resp)

        await ensure_protocol_checked(coord, session, HEADERS)  # must not raise

        assert coord._protocol_checked is True

    @pytest.mark.asyncio
    async def test_supported_logs_no_warning(self, caplog):
        import logging

        coord = _make_coord()
        session = _make_session(_make_resp(200, {"state": "SUPPORTED"}))

        with caplog.at_level(logging.WARNING):
            await ensure_protocol_checked(coord, session, HEADERS)

        assert not any(r.levelno == logging.WARNING for r in caplog.records)

    @pytest.mark.asyncio
    async def test_not_supported_logs_warning(self, caplog):
        import logging

        coord = _make_coord()
        session = _make_session(_make_resp(200, {"state": "DEPRECATED"}))

        with caplog.at_level(logging.WARNING):
            await ensure_protocol_checked(coord, session, HEADERS)

        assert any("may no longer be supported" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_non_200_logs_warning(self, caplog):
        import logging

        coord = _make_coord()
        session = _make_session(_make_resp(503))

        with caplog.at_level(logging.WARNING):
            await ensure_protocol_checked(coord, session, HEADERS)

        assert any("HTTP 503" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_any_exception_is_swallowed_broadly(self):
        """Unlike ensure_feature_flags, this uses a bare `except Exception`
        (matches the pre-extraction inline code exactly — a pre-existing
        asymmetry, not introduced by this extraction)."""
        coord = _make_coord()
        session = MagicMock()
        session.get = MagicMock(side_effect=RuntimeError("boom"))

        await ensure_protocol_checked(coord, session, HEADERS)  # must not raise

        assert coord._protocol_checked is True
