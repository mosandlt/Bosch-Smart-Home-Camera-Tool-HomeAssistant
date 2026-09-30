"""Local-only session: every consumer/lifecycle path stays off the cloud.

Covers the wanted-vs-resolvable split (no cloud fallback without a LAN
address), failure classification (wrong password vs. unreachable vs. privacy),
the Repairs issue lifecycle, renewal/heartbeat bypasses, the single rescue
attempt, recorder auth handling, Frigate on-demand open and the snapshot and
RCP cloud guards. Fake values only.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.bosch_shc_camera import BoschCameraCoordinator, ldi_local
from custom_components.bosch_shc_camera.ldi_local import (
    RESULT_AUTH,
    RESULT_NO_IP,
    RESULT_OK,
    RESULT_PRIVACY,
    RESULT_UNREACHABLE,
    _probe_describe_status,
    ldi_source,
    ldi_wanted,
    open_ldi_connection,
    record_ldi_result,
    resolve_ldi_ip,
)
from custom_components.bosch_shc_camera.live_connection import (
    try_live_connection_inner,
)
from custom_components.bosch_shc_camera.local_data_interface import LDI_ENDPOINT
from custom_components.bosch_shc_camera.repairs import (
    LDI_UNREACHABLE_GRACE_SEC,
    refresh_local_data_interface_auth_issue,
)
from custom_components.bosch_shc_camera.session_renewal import (
    auto_renew_local_session,
    refresh_local_creds_from_heartbeat,
)
from custom_components.bosch_shc_camera.stream_lifecycle import (
    handle_stream_worker_error,
)
from tests.test_ldi_local import (
    CAM,
    IP,
    PW,
    RESTREAM,
    _coord,
    _no_probe_wait,
)

MODULE = "custom_components.bosch_shc_camera"
LDI_LIVE: dict[str, Any] = {
    "_connection_type": "LOCAL",
    "_ldi": True,
    "_local_user": "localuser",
    "_local_password": PW,
    "urls": [f"{IP}:9554"],
    "rtspsUrl": "rtsp://127.0.0.1:41000/rtsp_tunnel?inst=1&fmtp=1",
}


# ── wanted vs. resolvable ───────────────────────────────────────────────────
class TestWanted:
    @pytest.mark.parametrize(
        ("state", "passwords", "expected"),
        [
            ("active", {CAM: PW}, True),
            ("active", {CAM: ""}, False),
            ("active", {CAM: "   "}, False),
            ("active", {CAM: 5}, False),
            ("active", {}, False),
            ("active", "absent", False),
            ("inactive", {CAM: PW}, False),
            ("unsupported", {CAM: PW}, False),
            (None, {CAM: PW}, False),
        ],
    )
    def test_modes(self, state: Any, passwords: Any, expected: bool) -> None:
        c = _coord(state=state, passwords=passwords)
        assert ldi_wanted(c, CAM) is expected  # type: ignore[arg-type]

    def test_wanted_without_any_lan_address(self) -> None:
        c = _coord(ip=None)
        assert ldi_wanted(c, CAM) is True  # type: ignore[arg-type]
        assert ldi_source(c, CAM) is None  # type: ignore[arg-type]


class TestResolveIp:
    def test_primary_cache(self) -> None:
        assert resolve_ldi_ip(_coord(ip=IP), CAM) == IP  # type: ignore[arg-type]

    def test_wifi_status_fallback(self) -> None:
        c = _coord(ip=None)
        c.wifiinfo_cache = {CAM: {"ipAddress": "10.0.0.77"}}
        assert resolve_ldi_ip(c, CAM) == "10.0.0.77"  # type: ignore[arg-type]
        assert ldi_source(c, CAM) == ("10.0.0.77", "localuser", PW)  # type: ignore[arg-type]

    def test_unsafe_primary_falls_through_to_safe_wifi(self) -> None:
        c = _coord(ip="8.8.8.8")
        c.wifiinfo_cache = {CAM: {"ipAddress": "10.0.0.77"}}
        assert resolve_ldi_ip(c, CAM) == "10.0.0.77"  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["8.8.8.8", "127.0.0.1", "0.0.0.0", "", None, 5])
    def test_nothing_safe(self, bad: Any) -> None:
        c = _coord(ip=None)
        c.wifiinfo_cache = {CAM: {"ipAddress": bad}}
        assert resolve_ldi_ip(c, CAM) is None  # type: ignore[arg-type]

    def test_stub_without_wifi_cache(self) -> None:
        c = _coord(ip=None)
        del c.wifiinfo_cache
        assert resolve_ldi_ip(c, CAM) is None  # type: ignore[arg-type]


# ── unresolved address: no cloud fallback ───────────────────────────────────
class TestUnresolvedNoCloud:
    @pytest.mark.asyncio
    async def test_wanted_but_unresolved_returns_none_without_cloud(self) -> None:
        c = _coord(ip=None)
        cloud = AsyncMock(side_effect=AssertionError("cloud session requested"))
        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=cloud):
            res = await try_live_connection_inner(c, CAM)  # type: ignore[arg-type]
        assert res is None
        cloud.assert_not_awaited()
        assert c.ldi_open_status[CAM]["reason"] == RESULT_NO_IP

    @pytest.mark.asyncio
    async def test_not_wanted_still_uses_normal_path(self) -> None:
        c = _coord(state="inactive", ip=None, token=None)
        with patch(f"{MODULE}.live_connection.open_ldi_connection") as opened:
            res = await try_live_connection_inner(c, CAM)  # type: ignore[arg-type]
        assert res is None  # stopped at the token gate = normal path reached
        opened.assert_not_called()
        assert CAM not in c.ldi_open_status


# ── result tracking ─────────────────────────────────────────────────────────
class TestRecordResult:
    def test_failure_keeps_first_timestamp_per_reason(self) -> None:
        c = _coord()
        record_ldi_result(c, CAM, RESULT_UNREACHABLE)  # type: ignore[arg-type]
        first = c.ldi_open_status[CAM]["since"]
        record_ldi_result(c, CAM, RESULT_UNREACHABLE)  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["since"] == first
        record_ldi_result(c, CAM, RESULT_AUTH)  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == RESULT_AUTH

    @pytest.mark.parametrize("clearing", [RESULT_OK, RESULT_PRIVACY])
    def test_ok_and_privacy_clear(self, clearing: str) -> None:
        c = _coord()
        record_ldi_result(c, CAM, RESULT_AUTH)  # type: ignore[arg-type]
        record_ldi_result(c, CAM, clearing)  # type: ignore[arg-type]
        assert CAM not in c.ldi_open_status

    def test_stub_without_status_map_gets_one(self) -> None:
        c = SimpleNamespace()
        record_ldi_result(c, CAM, RESULT_AUTH)  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == RESULT_AUTH


class TestOpenClassification:
    async def _open(self, c: SimpleNamespace, *, probe: int | None) -> Any:
        with (
            patch.object(
                ldi_local, "_probe_describe_status", new=AsyncMock(return_value=probe)
            ),
            patch.object(
                ldi_local.ldi_go2rtc,
                "ensure_stream",
                new=AsyncMock(return_value=RESTREAM),
            ),
        ):
            return await open_ldi_connection(c, CAM, (IP, "localuser", PW))  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("probe", "reason"),
        [
            (401, RESULT_AUTH),
            (None, RESULT_UNREACHABLE),
            (503, RESULT_UNREACHABLE),
        ],
    )
    async def test_failure_reasons(self, probe: int | None, reason: str) -> None:
        c = _coord()
        assert await self._open(c, probe=probe) is None
        assert c.ldi_open_status[CAM]["reason"] == reason

    @pytest.mark.asyncio
    async def test_privacy_is_a_state_not_a_failure(self) -> None:
        c = _coord(privacy=True)
        assert await self._open(c, probe=401) is None
        assert CAM not in c.ldi_open_status

    @pytest.mark.asyncio
    async def test_success_clears_failure(self) -> None:
        c = _coord()
        record_ldi_result(c, CAM, RESULT_AUTH)  # type: ignore[arg-type]
        assert await self._open(c, probe=200) is not None
        assert CAM not in c.ldi_open_status

    @pytest.mark.asyncio
    async def test_exception_counts_as_unreachable(self) -> None:
        c = _coord()
        c.stop_tls_proxy = AsyncMock(side_effect=OSError("bind"))
        assert await open_ldi_connection(c, CAM, (IP, "localuser", PW)) is None  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == RESULT_UNREACHABLE

    @pytest.mark.asyncio
    async def test_cancellation_clears_warming_and_reraises(self) -> None:
        c = _coord()
        c.stop_tls_proxy = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await open_ldi_connection(c, CAM, (IP, "localuser", PW))  # type: ignore[arg-type]
        assert CAM not in c.stream_warming
        assert CAM not in c.live_connections


class TestOpenConsumers:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("intent", [True, False])
    async def test_recorder_started_only_with_intent(self, intent: bool) -> None:
        c = _coord()
        c.nvr_user_intent = {CAM: intent}
        c.hass.async_create_task = MagicMock(
            side_effect=lambda coro, **_k: coro.close() if coro else None
        )
        start = AsyncMock()
        with (
            patch.object(
                ldi_local, "_probe_describe_status", new=AsyncMock(return_value=200)
            ),
            patch.object(
                ldi_local.ldi_go2rtc,
                "ensure_stream",
                new=AsyncMock(return_value=RESTREAM),
            ),
            patch(f"{MODULE}.nvr_recorder.start_recorder", new=start),
        ):
            assert await open_ldi_connection(c, CAM, (IP, "localuser", PW)) is not None  # type: ignore[arg-type]
        assert start.call_count == (1 if intent else 0)
        if intent:
            assert start.call_args.args[:2] == (c, CAM)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("green", [True, False])
    async def test_idle_reaper_only_when_enabled(self, green: bool) -> None:
        c = _coord()
        c.entry.options["enable_green_it"] = green
        with (
            patch.object(
                ldi_local, "_probe_describe_status", new=AsyncMock(return_value=200)
            ),
            patch.object(
                ldi_local.ldi_go2rtc,
                "ensure_stream",
                new=AsyncMock(return_value=RESTREAM),
            ),
        ):
            await open_ldi_connection(c, CAM, (IP, "localuser", PW))  # type: ignore[arg-type]
        assert c.replace_reaper_task.call_count == (1 if green else 0)
        if green:
            c.idle_session_reaper.assert_called_once_with(
                CAM, c.get_session(CAM).generation
            )


# ── describe probe ──────────────────────────────────────────────────────────
class _Reader:
    def __init__(self, replies: list[bytes | BaseException]) -> None:
        self._replies = replies

    async def read(self, _n: int) -> bytes:
        item = self._replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _Writer:
    def __init__(self, close_error: bool = False) -> None:
        self.sent: list[bytes] = []
        self.closed = False
        self._close_error = close_error

    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self._close_error:
            raise OSError("reset")


_CHALLENGE = b'RTSP/1.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="r", nonce="n"\r\n\r\n'


class TestProbe:
    async def _run(self, replies: list[Any], writer: _Writer | None = None) -> Any:
        writer = writer or _Writer()
        with patch(
            "asyncio.open_connection",
            new=AsyncMock(return_value=(_Reader(replies), writer)),
        ) as opened:
            result = await _probe_describe_status(IP, "localuser", PW, 1.0)
        assert writer.closed
        # straight at the camera's TLS port, certificate not verified
        assert opened.await_args.args == (IP, 9554)
        ctx = opened.await_args.kwargs["ssl"]
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE
        assert f"rtsps://{IP}:9554/live".encode() in writer.sent[0]
        return result, writer

    @pytest.mark.asyncio
    async def test_authenticated_200(self) -> None:
        result, writer = await self._run([_CHALLENGE, b"RTSP/1.0 200 OK\r\n\r\n"])
        assert result == 200
        assert b"Authorization: Digest" in writer.sent[1]
        assert PW.encode() not in writer.sent[1]

    @pytest.mark.asyncio
    async def test_rejected_password_401(self) -> None:
        result, _ = await self._run([_CHALLENGE, _CHALLENGE])
        assert result == 401

    @pytest.mark.asyncio
    async def test_no_challenge_reports_plain_status(self) -> None:
        result, _ = await self._run([b"RTSP/1.0 503 Service Unavailable\r\n\r\n"])
        assert result == 503

    @pytest.mark.asyncio
    async def test_unparsable_challenge_is_not_a_password_verdict(self) -> None:
        result, _ = await self._run([b"RTSP/1.0 401 Unauthorized\r\n"])
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "replies",
        [
            [b""],
            [b"garbage"],
            [_CHALLENGE, b""],
            [TimeoutError()],
            [_CHALLENGE, OSError()],
        ],
    )
    async def test_no_usable_answer_is_none(self, replies: list[Any]) -> None:
        result, _ = await self._run(replies)
        assert result is None

    @pytest.mark.asyncio
    async def test_connect_refused_is_none(self) -> None:
        with patch("asyncio.open_connection", new=AsyncMock(side_effect=OSError)):
            assert await _probe_describe_status(IP, "u", PW, 1.0) is None

    @pytest.mark.asyncio
    async def test_close_error_swallowed(self) -> None:
        result, _ = await self._run([b"RTSP/1.0 200 OK\r\n\r\n"], _Writer(True))
        assert result == 200


# ── Repairs lifecycle ───────────────────────────────────────────────────────
def _repairs_coord(
    *,
    status: dict[str, Any] | None = None,
    privacy: bool = False,
    offline_for: float | None = None,
    wanted: bool = True,
    live: bool = False,
) -> SimpleNamespace:
    c = _coord(state="active" if wanted else "inactive", privacy=privacy)
    c.hass = SimpleNamespace()
    c.data = {CAM: {"info": {"title": "Terrasse"}}}
    c.offline_since = (
        {} if offline_for is None else {CAM: time.monotonic() - offline_for}
    )
    c.ldi_open_status = {} if status is None else {CAM: status}
    c.live_connections = {CAM: dict(LDI_LIVE)} if live else {}
    c._ldi_auth_alerted = set()
    return c


class TestRepairsIssue:
    @patch(f"{MODULE}.ir")
    def test_wrong_password_raises_once_logged_once(
        self, ir: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        c = _repairs_coord(status={"reason": RESULT_AUTH, "since": time.monotonic()})
        with caplog.at_level(logging.INFO, logger=f"{MODULE}.repairs"):
            refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
            refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        kw = ir.async_create_issue.call_args
        assert kw.args[2] == f"local_data_interface_auth_{CAM}"
        assert kw.kwargs["translation_key"] == "local_data_interface_wrong_password"
        assert kw.kwargs["is_fixable"] is False
        assert kw.kwargs["translation_placeholders"] == {"camera": "Terrasse"}
        assert sum("stream unavailable" in r.message for r in caplog.records) == 1

    @patch(f"{MODULE}.ir")
    def test_unreachable_over_grace_via_offline_since(self, ir: MagicMock) -> None:
        c = _repairs_coord(offline_for=LDI_UNREACHABLE_GRACE_SEC + 5)
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        assert (
            ir.async_create_issue.call_args.kwargs["translation_key"]
            == "local_data_interface_unreachable"
        )

    @patch(f"{MODULE}.ir")
    def test_unreachable_over_grace_via_failed_opens(self, ir: MagicMock) -> None:
        old = time.monotonic() - LDI_UNREACHABLE_GRACE_SEC - 5
        c = _repairs_coord(status={"reason": RESULT_UNREACHABLE, "since": old})
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        assert (
            ir.async_create_issue.call_args.kwargs["translation_key"]
            == "local_data_interface_unreachable"
        )

    @patch(f"{MODULE}.ir")
    def test_short_outage_not_raised(self, ir: MagicMock) -> None:
        c = _repairs_coord(
            status={"reason": RESULT_UNREACHABLE, "since": time.monotonic()},
            offline_for=30,
        )
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()
        ir.async_delete_issue.assert_called_once()

    @patch(f"{MODULE}.ir")
    def test_stale_failure_ignored_while_session_is_up(self, ir: MagicMock) -> None:
        old = time.monotonic() - LDI_UNREACHABLE_GRACE_SEC - 5
        c = _repairs_coord(
            status={"reason": RESULT_UNREACHABLE, "since": old}, live=True
        )
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    @pytest.mark.parametrize("reason", [RESULT_AUTH, RESULT_UNREACHABLE, RESULT_NO_IP])
    def test_privacy_never_raises(self, ir: MagicMock, reason: str) -> None:
        old = time.monotonic() - 10 * LDI_UNREACHABLE_GRACE_SEC
        c = _repairs_coord(
            status={"reason": reason, "since": old}, privacy=True, offline_for=9999
        )
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_not_wanted_never_raises_and_clears(self, ir: MagicMock) -> None:
        c = _repairs_coord(
            status={"reason": RESULT_AUTH, "since": 0.0},
            wanted=False,
            offline_for=9999,
        )
        c._ldi_auth_alerted = {CAM}
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()
        ir.async_delete_issue.assert_called_once()
        assert CAM not in c._ldi_auth_alerted

    @patch(f"{MODULE}.ir")
    def test_cleared_after_successful_open(self, ir: MagicMock) -> None:
        c = _repairs_coord(status={"reason": RESULT_AUTH, "since": time.monotonic()})
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        record_ldi_result(c, CAM, RESULT_OK)  # type: ignore[arg-type]
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_delete_issue.assert_called_with(
            c.hass, "bosch_shc_camera", f"local_data_interface_auth_{CAM}"
        )
        assert CAM not in c._ldi_auth_alerted

    @patch(f"{MODULE}.ir")
    def test_no_data_no_crash(self, ir: MagicMock) -> None:
        c = _repairs_coord()
        c.data = None
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()

    @patch(f"{MODULE}.ir")
    def test_stub_without_status_map(self, ir: MagicMock) -> None:
        c = _repairs_coord()
        del c.ldi_open_status
        refresh_local_data_interface_auth_issue(c)  # type: ignore[arg-type]
        ir.async_create_issue.assert_not_called()

    def test_coordinator_delegator(self) -> None:
        c = MagicMock()
        with patch(f"{MODULE}.coordinator.repairs") as repairs:
            BoschCameraCoordinator._refresh_local_data_interface_auth_issue(c)
        repairs.refresh_local_data_interface_auth_issue.assert_called_once_with(c)


# ── renewal / heartbeat bypasses ────────────────────────────────────────────
class TestRenewalBypass:
    @pytest.mark.asyncio
    async def test_heartbeat_cred_refresh_ignores_ldi_session(self) -> None:
        live = dict(LDI_LIVE)
        c = SimpleNamespace(live_connections={CAM: live}, tls_proxy_ports={CAM: 1})
        await refresh_local_creds_from_heartbeat(
            c,  # type: ignore[arg-type]
            CAM,
            '{"user": "cbs-x", "password": "other"}',
            1,
            0.1,
        )
        assert live["_local_user"] == "localuser"
        assert live["_local_password"] == PW

    @pytest.mark.asyncio
    async def test_keepalive_loop_exits_without_cloud_call(self) -> None:
        c = SimpleNamespace(
            get_model_config=lambda _cid: SimpleNamespace(
                heartbeat_interval=1, renewal_interval=1
            ),
            get_session=lambda _cid: SimpleNamespace(generation=3),
            live_connections={CAM: dict(LDI_LIVE)},
            renewal_tasks={},
            try_live_connection=AsyncMock(),
            token="tok",
        )
        cloud = AsyncMock(side_effect=AssertionError("cloud session requested"))
        with (
            patch("asyncio.sleep", new=AsyncMock()),
            patch(f"{MODULE}.async_get_bosch_cloud_session", new=cloud),
        ):
            await auto_renew_local_session(c, CAM, 3)  # type: ignore[arg-type]
        c.try_live_connection.assert_not_awaited()
        cloud.assert_not_awaited()


# ── single rescue attempt ───────────────────────────────────────────────────
class TestSingleRescue:
    def _coord_401(self, ldi: bool) -> SimpleNamespace:
        live: dict[str, Any] = {"_connection_type": "LOCAL"}
        if ldi:
            live["_ldi"] = True
        return SimpleNamespace(
            stream_worker_dispatch_pending={CAM},
            record_stream_error=MagicMock(),
            get_model_config=MagicMock(
                return_value=SimpleNamespace(max_stream_errors=3)
            ),
            live_connections={CAM: live},
            stream_error_count={CAM: 0},
            stream_fell_back={},
            local_rescue_attempts={},
            local_rescue_at={},
            tls_proxy_rebuild_last={},
            try_live_connection=AsyncMock(return_value=None),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("ldi", "attempts"), [(True, 1), (False, 3)])
    async def test_attempts_per_session_kind(self, ldi: bool, attempts: int) -> None:
        c = self._coord_401(ldi)
        sleep = AsyncMock()
        with patch("asyncio.sleep", new=sleep):
            await handle_stream_worker_error(c, CAM, "401 Unauthorized")  # type: ignore[arg-type]
        assert c.try_live_connection.await_count == attempts
        assert sleep.await_count == attempts - 1
        assert c.stream_fell_back == {}


# ── switch watchdog ─────────────────────────────────────────────────────────
class TestWatchdog:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("ldi", [True, False])
    async def test_final_tick_saturates_counter_only_for_cloud_sessions(
        self, ldi: bool
    ) -> None:
        from custom_components.bosch_shc_camera.switch import BoschLiveStreamSwitch

        live: dict[str, Any] = {"_connection_type": "LOCAL"}
        if ldi:
            live["_ldi"] = True
        stream = SimpleNamespace(available=False)

        async def _reopen(cam_id: str) -> dict[str, Any]:
            coord.live_connections[cam_id] = dict(live)
            return coord.live_connections[cam_id]

        coord = SimpleNamespace(
            live_connections={CAM: live},
            user_intent_streams={CAM},
            camera_entities={CAM: SimpleNamespace(stream=stream)},
            stream_error_count={},
            stream_error_at={},
            stop_tls_proxy=AsyncMock(),
            try_live_connection=AsyncMock(side_effect=_reopen),
            record_stream_error=MagicMock(),
            record_stream_success=MagicMock(),
            get_model_config=lambda _cid: SimpleNamespace(max_stream_errors=3),
            go2rtc_consumer_count=AsyncMock(return_value=0),
            get_session=lambda _cid: SimpleNamespace(generation=0),
        )
        sw = BoschLiveStreamSwitch.__new__(BoschLiveStreamSwitch)
        sw.coordinator = coord  # type: ignore[assignment]
        sw._cam_id = CAM
        sw.async_write_ha_state = MagicMock()
        with patch("asyncio.sleep", new=AsyncMock()):
            await BoschLiveStreamSwitch._stream_health_watchdog(sw, CAM)
        assert coord.try_live_connection.await_count == 2
        if ldi:
            assert coord.stream_error_count == {}
        else:
            assert coord.stream_error_count == {CAM: 3}


# ── recorder ────────────────────────────────────────────────────────────────
class TestRecorder:
    def test_argv_has_no_audio_assumption(self) -> None:
        from custom_components.bosch_shc_camera import recorder

        url = "rtsp://127.0.0.1:41000/rtsp_tunnel?inst=1&fmtp=1"
        for args in (
            recorder._build_ffmpeg_args(url, "/tmp/x/%H.mp4"),
            recorder._build_preroll_ffmpeg_args(url, "/tmp/x/%H.mp4"),
        ):
            joined = " ".join(args)
            assert "-c:a" not in args and "0:a" not in joined and "-an" not in args
            assert args[args.index("-map") + 1] == "0"
            assert args[args.index("-c") + 1] == "copy"
            assert args[args.index("-analyzeduration") + 1] == "10M"
            assert args[args.index("-probesize") + 1] == "10M"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("ldi", "second_gives_up"), [(True, True), (False, False)])
    async def test_second_auth_failure(self, ldi: bool, second_gives_up: bool) -> None:
        from custom_components.bosch_shc_camera import recorder
        from tests.test_recorder import (
            CAM_ID,
            _make_lifecycle_coord,
            _mock_proc,
            _tail_for,
        )

        coord = _make_lifecycle_coord()
        live: dict[str, Any] = {"_connection_type": "LOCAL"}
        if ldi:
            live["_ldi"] = True
        coord.live_connections[CAM_ID] = live
        coord.nvr_auth_retry_count[CAM_ID] = 1
        proc = _mock_proc(returncode=8, stderr_data=b"method OPTIONS failed: 401")
        coord.nvr_processes[CAM_ID] = proc
        with (
            patch.object(recorder, "start_recorder", new=AsyncMock()) as restart,
            patch.object(asyncio, "sleep", new=AsyncMock()),
        ):
            await recorder._watch_recorder(coord, CAM_ID, proc, _tail_for(proc))
        if second_gives_up:
            restart.assert_not_awaited()
            assert "repeated auth failures" in coord.nvr_error_state[CAM_ID]
        else:
            restart.assert_awaited_once()
            assert CAM_ID not in coord.nvr_error_state


# ── frigate front door ──────────────────────────────────────────────────────
class TestFrigateOnDemand:
    @pytest.mark.asyncio
    async def test_opens_local_only_session_when_none_exists(self) -> None:
        c = _coord()

        async def _open(cam_id: str) -> dict[str, Any]:
            c.live_connections[cam_id] = dict(LDI_LIVE)
            return c.live_connections[cam_id]

        c.try_live_connection = AsyncMock(side_effect=_open)
        with patch(
            f"{MODULE}.frigate_endpoint.ensure_ldi_stream",
            new=AsyncMock(return_value=RESTREAM),
        ):
            target = await BoschCameraCoordinator._frigate_resolve_inner(c, CAM)  # type: ignore[arg-type]
        assert target is not None
        assert (target.port, target.path) == (18554, "/ldi_11111111")
        c.try_live_connection.assert_awaited_once_with(CAM)

    @pytest.mark.asyncio
    async def test_failed_open_returns_none(self) -> None:
        c = _coord()
        c.try_live_connection = AsyncMock(return_value=None)
        assert await BoschCameraCoordinator._frigate_resolve_inner(c, CAM) is None  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_no_cloud_when_wanted_but_unresolved(self) -> None:
        c = _coord(ip=None)
        c.options = {}
        cloud = AsyncMock(side_effect=AssertionError("cloud session requested"))

        async def _open(cam_id: str, **_k: Any) -> Any:
            return await try_live_connection_inner(c, cam_id)  # type: ignore[arg-type]

        c.try_live_connection = AsyncMock(side_effect=_open)
        with patch(f"{MODULE}.async_get_bosch_cloud_session", new=cloud):
            target = await BoschCameraCoordinator._frigate_resolve_inner(c, CAM)  # type: ignore[arg-type]
        assert target is None
        cloud.assert_not_awaited()


class TestCloudGuards:
    @pytest.mark.asyncio
    async def test_live_snapshots_make_no_cloud_call_for_ldi(self) -> None:
        c = _coord()
        c.token = "tok"
        cloud = MagicMock(side_effect=AssertionError("cloud session requested"))
        with (
            patch(f"{MODULE}.coordinator.async_bosch_cloud_session_cm", new=cloud),
            patch(f"{MODULE}.coordinator.JPEG_SIZE_FULL", 1),
        ):
            assert (
                await BoschCameraCoordinator._async_fetch_live_snapshot_impl(c, CAM)
                is None
            )  # type: ignore[arg-type]
            assert (
                await BoschCameraCoordinator.async_fetch_live_snapshot_local(c, CAM)
                is None
            )  # type: ignore[arg-type]
        cloud.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_snapshot_normal_path_unchanged_when_not_wanted(self) -> None:
        c = _coord(state="inactive")
        c.token = "tok"
        cm = MagicMock(side_effect=RuntimeError("normal path reached"))
        with patch(f"{MODULE}.coordinator.async_bosch_cloud_session_cm", new=cm):
            with pytest.raises(RuntimeError, match="normal path reached"):
                await BoschCameraCoordinator._async_fetch_live_snapshot_impl(c, CAM)  # type: ignore[arg-type]


# ── slow-tier RCP cloud session ─────────────────────────────────────────────
class TestSlowTierRcpGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("wanted", "puts"), [(True, 0), (False, 1)])
    async def test_cloud_rcp_session_only_when_not_local_only(
        self, wanted: bool, puts: int
    ) -> None:
        from tests.test_init import (
            _PATCH_SESSION,
            CAM_A,
            CAM_GEN2_INDOOR_PRIV_OFF,
            _build_slow_tier_routes_sprint_mc,
            _make_coord_for_update_data_sprint_mc,
            _make_resp_sprint_mc,
            _make_session_fn_sprint_mc,
            _put_resp_sprint_mc,
        )

        coord = _make_coord_for_update_data_sprint_mc(
            _last_slow=float("-inf"), cached_status={CAM_A: "ONLINE"}
        )
        coord.local_data_interface_cache = {}
        coord.firmware_cache = {CAM_A: {"current": "9.40.105"}}
        coord.entry.options["local_passwords"] = {CAM_A: PW}
        routes = _build_slow_tier_routes_sprint_mc(CAM_GEN2_INDOOR_PRIV_OFF)
        routes[f"{CAM_A}/{LDI_ENDPOINT}"] = (
            _make_resp_sprint_mc(200, {"username": "localuser"})
            if wanted
            else _make_resp_sprint_mc(404, {})
        )
        session = _make_session_fn_sprint_mc(routes)
        session.put = MagicMock(return_value=_put_resp_sprint_mc(403, ""))
        with patch(_PATCH_SESSION, new=AsyncMock(return_value=session)):
            await BoschCameraCoordinator._async_update_data(coord)
        assert session.put.call_count == puts


# ── "enabled but no password" hint ──────────────────────────────────────────
def _hint_coord(state: str | None, password: bool) -> Any:
    c = _coord(state=state, passwords={CAM: PW} if password else {})
    c.hass = SimpleNamespace()
    c.data = {CAM: {"info": {"title": "Terrasse"}}}
    c._ldi_nopw_alerted = set()
    return c


class TestPasswordHint:
    @patch(f"{MODULE}.ir")
    def test_created_on_active_without_password_logged_once(
        self, ir: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        from custom_components.bosch_shc_camera.repairs import (
            refresh_local_data_interface_password_hint,
        )

        c = _hint_coord("active", password=False)
        with caplog.at_level(logging.INFO, logger=f"{MODULE}.repairs"):
            refresh_local_data_interface_password_hint(c)
            refresh_local_data_interface_password_hint(c)
        kw = ir.async_create_issue.call_args
        assert kw.args[2] == f"local_data_interface_no_password_{CAM}"
        assert kw.kwargs["translation_key"] == "local_data_interface_no_password"
        assert kw.kwargs["is_fixable"] is False
        assert kw.kwargs["translation_placeholders"] == {"camera": "Terrasse"}
        assert sum("no password stored" in r.message for r in caplog.records) == 1

    @patch(f"{MODULE}.ir")
    @pytest.mark.parametrize(
        ("state", "password"),
        [
            ("active", True),
            ("inactive", False),
            ("inactive", True),
            ("unsupported", False),
            (None, False),  # Gen1 / old firmware: never polled, no cache entry
        ],
    )
    def test_not_created(
        self, ir: MagicMock, state: str | None, password: bool
    ) -> None:
        from custom_components.bosch_shc_camera.repairs import (
            refresh_local_data_interface_password_hint,
        )

        c = _hint_coord(state, password)
        refresh_local_data_interface_password_hint(c)
        ir.async_create_issue.assert_not_called()
        ir.async_delete_issue.assert_called_once()

    @patch(f"{MODULE}.ir")
    def test_cleared_when_password_set(self, ir: MagicMock) -> None:
        from custom_components.bosch_shc_camera.repairs import (
            refresh_local_data_interface_password_hint,
        )

        c = _hint_coord("active", password=False)
        refresh_local_data_interface_password_hint(c)
        assert CAM in c._ldi_nopw_alerted
        c.entry.options["local_passwords"] = {CAM: PW}
        refresh_local_data_interface_password_hint(c)
        ir.async_delete_issue.assert_called_with(
            c.hass, "bosch_shc_camera", f"local_data_interface_no_password_{CAM}"
        )
        assert CAM not in c._ldi_nopw_alerted

    @patch(f"{MODULE}.ir")
    def test_cleared_when_interface_disabled(self, ir: MagicMock) -> None:
        from custom_components.bosch_shc_camera.repairs import (
            refresh_local_data_interface_password_hint,
        )

        c = _hint_coord("active", password=False)
        refresh_local_data_interface_password_hint(c)
        c.local_data_interface_cache[CAM] = {"state": "inactive"}
        refresh_local_data_interface_password_hint(c)
        ir.async_delete_issue.assert_called_once()

    @patch(f"{MODULE}.ir")
    def test_no_data_no_crash(self, ir: MagicMock) -> None:
        from custom_components.bosch_shc_camera.repairs import (
            refresh_local_data_interface_password_hint,
        )

        c = _hint_coord("active", password=False)
        c.data = None
        refresh_local_data_interface_password_hint(c)
        ir.async_create_issue.assert_not_called()

    def test_coordinator_delegator(self) -> None:
        c = MagicMock()
        with patch(f"{MODULE}.coordinator.repairs") as repairs:
            BoschCameraCoordinator._refresh_local_data_interface_password_hint(c)
        repairs.refresh_local_data_interface_password_hint.assert_called_once_with(c)

    @pytest.mark.asyncio
    async def test_one_failing_refresh_does_not_block_the_others(self) -> None:
        from tests.test_init import (
            _PATCH_SESSION,
            CAM_A,
            CAM_GEN2_INDOOR_PRIV_OFF,
            _build_slow_tier_routes_sprint_mc,
            _make_coord_for_update_data_sprint_mc,
            _make_session_fn_sprint_mc,
        )

        coord = _make_coord_for_update_data_sprint_mc(
            _last_slow=time.monotonic(), cached_status={CAM_A: "ONLINE"}
        )
        coord._refresh_local_data_interface_issues = MagicMock(
            side_effect=RuntimeError("boom")
        )
        coord._refresh_local_data_interface_auth_issue = MagicMock()
        coord._refresh_local_data_interface_password_hint = MagicMock()
        session = _make_session_fn_sprint_mc(
            _build_slow_tier_routes_sprint_mc(CAM_GEN2_INDOOR_PRIV_OFF)
        )
        with patch(_PATCH_SESSION, new=AsyncMock(return_value=session)):
            await BoschCameraCoordinator._async_update_data(coord)
        coord._refresh_local_data_interface_auth_issue.assert_called_once()
        coord._refresh_local_data_interface_password_hint.assert_called_once()


# ── the interface user name is fixed; only the password is configurable ─────
class TestFixedUsername:
    @pytest.mark.asyncio
    async def test_options_step_has_no_username_field(self) -> None:
        from tests.test_ldi_local import _flow

        res = await _flow().async_step_local_passwords()
        assert {str(k) for k in res["data_schema"].schema} == {
            "Terrace",
            "clear_password_for",
        }

    def test_username_constant_and_no_username_option(self) -> None:
        from custom_components.bosch_shc_camera.const import DEFAULT_OPTIONS

        assert ldi_local.LDI_USER == "localuser"
        assert not [k for k in DEFAULT_OPTIONS if "local" in k and "user" in k]
        assert ldi_source(_coord(), CAM)[1] == "localuser"  # type: ignore[arg-type,index]
