"""Tests for the always-reused main-viewing-path front-door (viewing_front_door.py).

Covers `viewing_resolve_inner` (the resolve callback wired into the shared
`FrontDoorRunner`), `start_viewing_front_door` (bind + sticky-port reuse +
OSError fallback + URL shape), and `stop_viewing_front_door`.

Reuses the same lightweight `SimpleNamespace` coordinator-stub pattern as
`test_init.py`'s `try_live_connection_inner` tests, since this module reads
the exact same coordinator attributes (`live_connections`,
`tls_proxy_ports`) that those fixtures already populate.
"""

from __future__ import annotations

from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.bosch_shc_camera.frigate_endpoint import (
    AUTH_NONE,
    FrontDoorRunner,
)
from custom_components.bosch_shc_camera.viewing_front_door import (
    start_viewing_front_door,
    stop_viewing_front_door,
    viewing_resolve_inner,
)

CAM_A = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _enable_loopback_sockets(socket_enabled: None) -> Generator[None, None, None]:
    """Allow 127.0.0.1 loopback for the real front-door listener sockets
    this module's tests bind (matches test_frigate_endpoint.py's pattern)."""
    yield


def _coord(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = dict(
        live_connections={},
        tls_proxy_ports={},
        viewing_front_door_runner=None,
        viewing_sticky_port={},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ─────────────────────────────────────────────────────────────────────────────
# viewing_resolve_inner
# ─────────────────────────────────────────────────────────────────────────────


class TestViewingResolveInner:
    @pytest.mark.asyncio
    async def test_no_live_connection_returns_none(self):
        coord = _coord()
        assert await viewing_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_non_local_connection_type_returns_none(self):
        coord = _coord(
            live_connections={CAM_A: {"_connection_type": "REMOTE"}},
            tls_proxy_ports={CAM_A: 12345},
        )
        assert await viewing_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_missing_port_returns_none(self):
        coord = _coord(
            live_connections={
                CAM_A: {
                    "_connection_type": "LOCAL",
                    "_local_user": "u",
                    "_local_password": "p",
                }
            },
            tls_proxy_ports={},  # no port cached yet
        )
        assert await viewing_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_missing_user_or_password_returns_none(self):
        coord = _coord(
            live_connections={CAM_A: {"_connection_type": "LOCAL", "_local_user": "u"}},
            tls_proxy_ports={CAM_A: 12345},
        )
        assert await viewing_resolve_inner(coord, CAM_A) is None

    @pytest.mark.asyncio
    async def test_all_present_returns_inner_target(self):
        coord = _coord(
            live_connections={
                CAM_A: {
                    "_connection_type": "LOCAL",
                    "_local_user": "u-fresh",
                    "_local_password": "p-fresh",
                }
            },
            tls_proxy_ports={CAM_A: 55123},
        )
        target = await viewing_resolve_inner(coord, CAM_A)
        assert target is not None
        assert target.port == 55123
        assert target.digest_user == "u-fresh"
        assert target.digest_password == "p-fresh"

    @pytest.mark.asyncio
    async def test_never_calls_try_live_connection(self):
        """Unlike frigate's resolver, this one must NOT lazily open a session
        — the main viewing path always already has one by the time the
        front-door is running. Calling try_live_connection() here would risk
        rotating creds / killing the TLS proxy port on Gen2 FW 9.40.25+."""
        coord = _coord(try_live_connection=AsyncMock())
        await viewing_resolve_inner(coord, CAM_A)
        coord.try_live_connection.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# start_viewing_front_door / stop_viewing_front_door
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def runner_coord():
    coord = _coord()
    yield coord
    runner = coord.viewing_front_door_runner
    if runner is not None:
        runner.stop_all()


class TestStartViewingFrontDoor:
    @pytest.mark.asyncio
    async def test_returns_credential_free_stable_url(self, runner_coord):
        url = await start_viewing_front_door(
            runner_coord,
            CAM_A,
            inst=1,
            audio_param="&enableaudio=1",
            max_session_duration=3600,
        )
        assert url is not None
        assert "@" not in url  # no embedded user:pass
        assert url.startswith("rtsp://127.0.0.1:")
        assert "/rtsp_tunnel?inst=1&enableaudio=1&fmtp=1&maxSessionDuration=3600" in url

    @pytest.mark.asyncio
    async def test_non_default_inst_value_threaded_through(self, runner_coord):
        """`inst` is a straight pass-through of whatever quality the caller
        selected (LOCAL default is 1, but REMOTE/quality-select callers can
        legitimately pass 2/3/4, see live_connection.py) — every prior test
        here hardcoded inst=1, which never exercised that the f-string
        actually threads through a different value rather than silently
        defaulting to 1."""
        url = await start_viewing_front_door(
            runner_coord,
            CAM_A,
            inst=4,
            audio_param="&enableaudio=1",
            max_session_duration=3600,
        )
        assert url is not None
        assert "inst=4" in url
        assert "inst=1" not in url

    @pytest.mark.asyncio
    async def test_lazily_creates_runner(self, runner_coord):
        assert runner_coord.viewing_front_door_runner is None
        await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        assert isinstance(runner_coord.viewing_front_door_runner, FrontDoorRunner)

    @pytest.mark.asyncio
    async def test_binds_auth_none_localhost_only(self, runner_coord):
        await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        server = runner_coord.viewing_front_door_runner._servers[CAM_A]
        assert server.config.bind_host == "127.0.0.1"
        assert server.config.auth_mode == AUTH_NONE
        assert server.config.ip_allowlist == frozenset()

    @pytest.mark.asyncio
    async def test_sticky_port_reused_across_calls(self, runner_coord):
        url1 = await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        port1 = runner_coord.viewing_sticky_port[CAM_A]
        # Second call reuses the already-bound listener (see
        # test_second_call_reuses_listener_does_not_rebind below) and must
        # keep publishing the SAME port either way.
        url2 = await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        port2 = runner_coord.viewing_sticky_port[CAM_A]
        assert port1 == port2
        assert url1 == url2

    @pytest.mark.asyncio
    async def test_second_call_reuses_listener_does_not_rebind(self, runner_coord):
        """Bug-hunt finding: a naive implementation always calls
        `FrontDoorRunner.start_server`, which internally does an unconditional
        stop_server()+rebind — harmless on a fresh connect, but needless
        churn (and a brief ECONNREFUSED window for any racing client) on
        every periodic LOCAL session renewal, since `viewing_resolve_inner`
        already reads fresh creds/port per client-connect and never needs
        the listener itself to move. A second call for the same cam_id with
        an already-bound listener must NOT call `start_server` again."""
        await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        server_before = runner_coord.viewing_front_door_runner._servers[CAM_A]
        with patch.object(
            FrontDoorRunner, "start_server", AsyncMock()
        ) as start_server_spy:
            await start_viewing_front_door(
                runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
            )
        start_server_spy.assert_not_called()
        server_after = runner_coord.viewing_front_door_runner._servers[CAM_A]
        assert server_before is server_after

    @pytest.mark.asyncio
    async def test_renewal_call_with_different_inst_updates_url_not_listener(
        self, runner_coord
    ):
        """A renewal call that reuses the listener still returns a URL
        reflecting THIS call's inst/audio/session-duration args (a fresh,
        non-renewal call with a genuinely different quality selection must
        still see it reflected), even though the underlying port is
        untouched."""
        url1 = await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        port1 = runner_coord.viewing_sticky_port[CAM_A]
        url2 = await start_viewing_front_door(
            runner_coord,
            CAM_A,
            inst=2,
            audio_param="&enableaudio=1",
            max_session_duration=3600,
        )
        port2 = runner_coord.viewing_sticky_port[CAM_A]
        assert port1 == port2  # same listener, same port
        assert "inst=1" in url1
        assert "inst=2" in url2
        assert "maxSessionDuration=3600" in url2

    @pytest.mark.asyncio
    async def test_oserror_on_sticky_port_falls_back_to_ephemeral(self, runner_coord):
        """A stale sticky port that's now taken by something else must not
        wedge the front-door — it should fall back to a fresh ephemeral bind
        instead of raising, mirroring frigate_endpoint.py's own fallback."""
        # Reserve a real port first so binding it again raises OSError.
        import asyncio

        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        blocked_port = blocker.sockets[0].getsockname()[1]
        try:
            runner_coord.viewing_sticky_port[CAM_A] = blocked_port
            url = await start_viewing_front_door(
                runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
            )
            assert url is not None
            new_port = runner_coord.viewing_sticky_port[CAM_A]
            assert new_port != blocked_port
        finally:
            blocker.close()
            await blocker.wait_closed()

    @pytest.mark.asyncio
    async def test_total_bind_failure_returns_none(self, runner_coord, monkeypatch):
        """If even the ephemeral-port retry can't bind (e.g. a broken
        bind_host), the function must return None so the caller can fall back
        to the raw credentialed URL rather than raising into the caller."""

        async def _always_raise_oserror(*args, **kwargs):
            raise OSError("simulated bind failure")

        monkeypatch.setattr(FrontDoorRunner, "start_server", _always_raise_oserror)
        url = await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        assert url is None


class TestStopViewingFrontDoor:
    @pytest.mark.asyncio
    async def test_noop_when_runner_is_none(self):
        coord = _coord()
        # Must not raise even though no runner/server exists yet.
        await stop_viewing_front_door(coord, CAM_A)

    @pytest.mark.asyncio
    async def test_stops_running_server(self, runner_coord):
        await start_viewing_front_door(
            runner_coord, CAM_A, inst=1, audio_param="", max_session_duration=60
        )
        assert runner_coord.viewing_front_door_runner.has_server(CAM_A)
        await stop_viewing_front_door(runner_coord, CAM_A)
        assert not runner_coord.viewing_front_door_runner.has_server(CAM_A)
