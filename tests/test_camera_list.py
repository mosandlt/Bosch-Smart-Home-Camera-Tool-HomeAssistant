"""Tests for camera_list.py — camera-list fetch + 401 retry (Phase 2 step 2
of the coordinator rewrite). Direct unit tests in isolation; the existing
integration-level tests exercising the full _async_update_data (in
test_init.py) already cover the end-to-end wiring and are left in place."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.bosch_shc_camera.camera_list import fetch_camera_list

CAM_A = "11111111-1111-1111-1111-111111111111"


def _make_resp(status: int, json_data=None, text_data: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=json_data if json_data is not None else [])
    resp.text = AsyncMock(return_value=text_data)
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_session(*responses):
    """Session whose .get() returns each response in sequence (one per call)."""
    queue = list(responses)

    def _get(*args, **kwargs):
        return queue.pop(0)

    session = MagicMock()
    session.get = MagicMock(side_effect=_get)
    return session


def _make_coord(**overrides):
    def _create_task(coro, **kwargs):
        coro.close()
        return MagicMock()

    base = dict(
        hass=SimpleNamespace(async_create_task=MagicMock(side_effect=_create_task)),
        ensure_valid_token=AsyncMock(return_value="fresh-token"),
        _async_refresh_maintenance=AsyncMock(),
        async_outage_ping_all=AsyncMock(),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


HEADERS = {"Authorization": "Bearer old-token", "Accept": "application/json"}


class TestFetchCameraListHappyPath:
    @pytest.mark.asyncio
    async def test_200_returns_list_unchanged_token(self):
        coord = _make_coord()
        session = _make_session(_make_resp(200, [{"id": CAM_A}]))

        cams, token, headers = await fetch_camera_list(
            coord, session, HEADERS, "old-token"
        )

        assert cams == [{"id": CAM_A}]
        assert token == "old-token"
        assert headers == HEADERS
        coord.ensure_valid_token.assert_not_called()


def _make_timeout_cm():
    """A `session.get(...)` context manager whose `__aenter__` raises
    `TimeoutError` — simulates a bare connect/read timeout distinct from
    any HTTP response."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=TimeoutError("simulated timeout"))
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestFetchCameraListTimeoutRetry:
    """A bare timeout on `GET /v11/video_inputs` gets one quick retry before
    failing the whole tick over it (2026-07-23 community report: a brief
    Bosch-cloud blip failed two consecutive ticks)."""

    @pytest.mark.asyncio
    async def test_timeout_then_success_recovers_without_raising(self, monkeypatch):
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _fake_sleep)
        coord = _make_coord()
        session = _make_session(_make_timeout_cm(), _make_resp(200, [{"id": CAM_A}]))

        cams, token, _headers = await fetch_camera_list(
            coord, session, HEADERS, "old-token"
        )

        assert cams == [{"id": CAM_A}]
        assert token == "old-token"
        assert session.get.call_count == 2
        from custom_components.bosch_shc_camera.const import (
            VIDEO_INPUTS_RETRY_DELAY_SEC,
        )

        assert sleeps == [VIDEO_INPUTS_RETRY_DELAY_SEC]

    @pytest.mark.asyncio
    async def test_timeout_twice_raises_after_exactly_one_retry(self, monkeypatch):
        sleeps: list[float] = []

        async def _fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("asyncio.sleep", _fake_sleep)
        coord = _make_coord()
        session = _make_session(_make_timeout_cm(), _make_timeout_cm())

        with pytest.raises(TimeoutError):
            await fetch_camera_list(coord, session, HEADERS, "old-token")

        # Exactly 2 attempts (1 retry), not more — a persistent outage must
        # still fail promptly, not loop.
        assert session.get.call_count == 2
        assert len(sleeps) == 1

    @pytest.mark.asyncio
    async def test_non_timeout_error_status_is_not_retried(self):
        """A definitive HTTP error (not a bare timeout) must fail
        immediately — the retry is only for a connection that never
        completed at all."""
        coord = _make_coord()
        session = _make_session(_make_resp(500))

        with pytest.raises(UpdateFailed, match="HTTP 500"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")

        assert session.get.call_count == 1


class TestFetchCameraListNon200:
    @pytest.mark.asyncio
    async def test_500_raises_update_failed_and_kicks_maint_and_outage_ping(self):
        coord = _make_coord()
        session = _make_session(_make_resp(500))

        with pytest.raises(UpdateFailed, match="HTTP 500"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")

        coord._async_refresh_maintenance.assert_called_once_with(reactive=True)
        coord.async_outage_ping_all.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_missing_hooks_are_a_noop(self):
        """getattr(..., None) guards for stub coordinators without these
        methods must not crash."""
        coord = _make_coord()
        del coord._async_refresh_maintenance
        del coord.async_outage_ping_all
        session = _make_session(_make_resp(503))

        with pytest.raises(UpdateFailed, match="HTTP 503"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")


class TestFetchCameraList401Retry:
    @pytest.mark.asyncio
    async def test_401_then_200_returns_refreshed_token_and_headers(self):
        coord = _make_coord()
        session = _make_session(_make_resp(401), _make_resp(200, [{"id": CAM_A}]))

        cams, token, headers = await fetch_camera_list(
            coord, session, HEADERS, "old-token"
        )

        assert cams == [{"id": CAM_A}]
        assert token == "fresh-token"
        assert headers["Authorization"] == "Bearer fresh-token"
        coord.ensure_valid_token.assert_called_once_with("old-token")

    @pytest.mark.asyncio
    async def test_401_then_401_generic_raises_relogin_message(self):
        coord = _make_coord()
        session = _make_session(_make_resp(401), _make_resp(401, text_data="{}"))

        with pytest.raises(UpdateFailed, match="Force new browser login"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")

    @pytest.mark.asyncio
    async def test_401_then_401_auth_failed_body_raises_account_permission_message(
        self,
    ):
        coord = _make_coord()
        body = json.dumps(
            {"error": "sh:authorization.failed", "message": "missing permission"}
        )
        session = _make_session(_make_resp(401), _make_resp(401, text_data=body))

        with pytest.raises(UpdateFailed, match=r"sh:authorization\.failed"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")

    @pytest.mark.asyncio
    async def test_401_then_401_auth_failed_body_without_message_falls_back(self):
        """`body_json.get('message', 'no detail')` — the 'no detail' fallback
        string must be exercised when Bosch's error body omits `message`
        entirely (a .get() value-default, invisible to line/branch coverage
        alone — found by a bug-hunt agent as an untested path)."""
        coord = _make_coord()
        body = json.dumps({"error": "sh:authorization.failed"})
        session = _make_session(_make_resp(401), _make_resp(401, text_data=body))

        with pytest.raises(UpdateFailed, match="no detail"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")

    @pytest.mark.asyncio
    async def test_401_then_401_with_unparseable_body_falls_back_to_generic(self):
        """Bad JSON in the retry-401 body must not crash — swallowed, falls
        through to the generic relogin message."""
        coord = _make_coord()
        session = _make_session(
            _make_resp(401), _make_resp(401, text_data="not json {{{")
        )

        with pytest.raises(UpdateFailed, match="Force new browser login"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")

    @pytest.mark.asyncio
    async def test_401_then_non_200_non_401_raises_http_status_message(self):
        coord = _make_coord()
        session = _make_session(_make_resp(401), _make_resp(503))

        with pytest.raises(UpdateFailed, match="HTTP 503"):
            await fetch_camera_list(coord, session, HEADERS, "old-token")


class TestFetchCameraListCloudApiOverride:
    @pytest.mark.asyncio
    async def test_uses_diagnostic_cloud_api_override_when_set(self):
        coord = _make_coord(_cloud_api="https://diagnostic.example.invalid")
        session = _make_session(_make_resp(200, []))

        await fetch_camera_list(coord, session, HEADERS, "old-token")

        called_url = session.get.call_args[0][0]
        assert called_url.startswith("https://diagnostic.example.invalid")

    @pytest.mark.asyncio
    async def test_falls_back_to_default_cloud_api_when_unset(self):
        """A stub coordinator without _cloud_api (predates the diagnostic
        override field) must still work via the getattr(..., CLOUD_API)
        fallback."""
        coord = _make_coord()
        session = _make_session(_make_resp(200, []))

        await fetch_camera_list(coord, session, HEADERS, "old-token")

        called_url = session.get.call_args[0][0]
        assert called_url.startswith("https://residential.cbs.boschsecurity.com")
