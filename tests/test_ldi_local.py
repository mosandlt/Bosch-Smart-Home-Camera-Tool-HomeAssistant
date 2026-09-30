"""Local-only video source over the local data interface.

Pins: source selection per mode (active/inactive/unsupported x password
present/empty/garbage), zero cloud calls on the local path, failure paths that
never fall through to the cloud, redaction of the stored password, the options
flow step, and the two lifecycle guards (no LOCAL->REMOTE escalation, no RCP
read on the interface's port).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.diagnostics import async_redact_data

from custom_components.bosch_shc_camera import (
    BoschCameraCoordinator,
    ldi_local,
    ldi_rest,
)
from custom_components.bosch_shc_camera.config_flow import BoschCameraOptionsFlow
from custom_components.bosch_shc_camera.diagnostics import TO_REDACT
from custom_components.bosch_shc_camera.ldi_local import (
    LDI_PASSWORDS_OPTION,
    LDI_RTSP_PORT,
    LDI_USER,
    ldi_source,
    open_ldi_connection,
)
from custom_components.bosch_shc_camera.live_connection import (
    try_live_connection_inner,
)
from custom_components.bosch_shc_camera.session_state import (
    CameraSessionState,
    get_or_create_session,
)
from custom_components.bosch_shc_camera.stream_lifecycle import (
    handle_stream_worker_error,
)

CAM = "11111111-1111-1111-1111-111111111111"
IP = "10.0.0.50"
PW = "fake-sticker-pw"
RESTREAM = "rtsp://127.0.0.1:18554/ldi_11111111"
LDI = "custom_components.bosch_shc_camera.ldi_local"
_PATH_HIGH = "/rtsp_tunnel?line=1&inst=1&enableaudio=1"


@pytest.fixture(autouse=True)
def _no_probe_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ldi_local, "_PROBE_RETRY_WAIT", 0)


def _probe(status: int | None, privacy_on: bool | None = False) -> ldi_rest.LdiProbe:
    """REST probe outcome for an HTTP status (None = no answer)."""
    if status == 200:
        return ldi_rest.LdiProbe(ldi_rest.PROBE_OK, privacy_on, "9.40.0202")
    if status == 401:
        return ldi_rest.LdiProbe(ldi_rest.PROBE_AUTH)
    if status is None:
        return ldi_rest.LdiProbe(ldi_rest.PROBE_UNREACHABLE)
    return ldi_rest.LdiProbe(ldi_rest.PROBE_ERROR)


def _open_patches(
    status: int | None = 200,
    restream: str | None = RESTREAM,
    privacy_on: bool | None = False,
):
    """Camera probe answer + go2rtc registration result for one open."""
    return (
        patch(
            f"{LDI}.ldi_rest.probe_camera",
            new=AsyncMock(return_value=_probe(status, privacy_on)),
        ),
        patch(f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=restream)),
    )


def _coord(
    *,
    state: str | None = "active",
    passwords: object = None,
    ip: str | None = IP,
    privacy: bool = False,
    token: str | None = "tok",
) -> SimpleNamespace:
    if passwords is None:
        passwords = {CAM: PW}
    options = {LDI_PASSWORDS_OPTION: passwords} if passwords != "absent" else {}
    cache = {CAM: {"state": state}} if state is not None else {}
    c = SimpleNamespace(
        local_data_interface_cache=cache,
        entry=SimpleNamespace(options=options),
        get_cam_lan_ip=lambda _cid: ip,
        shc_state_cache={CAM: {"privacy_mode": privacy}},
        wifiinfo_cache={},
        ldi_open_status={},
        nvr_user_intent={},
        replace_reaper_task=MagicMock(),
        idle_session_reaper=MagicMock(return_value=None),
        live_connections={},
        live_opened_at={},
        stream_warming=set(),
        _quality_effective_inst={},
        _sessions={},
        camera_entities={},
        tls_proxy_rebuild_last={},
        tls_proxy_ports={CAM: 40000},
        token=token,
        hass=MagicMock(),
        get_quality_params=lambda _cid: (True, 1),
        get_quality=lambda _cid: "auto",
        get_model_config=lambda _cid: SimpleNamespace(
            describe_timeout=3, max_session_duration=3600
        ),
        start_tls_proxy=AsyncMock(return_value=40000),
        stop_tls_proxy=AsyncMock(),
        stop_viewing_front_door=AsyncMock(),
        stop_remote_viewing_front_door=AsyncMock(),
        start_viewing_front_door=AsyncMock(
            return_value="rtsp://127.0.0.1:41000/rtsp_tunnel?inst=1&fmtp=1"
        ),
        async_update_listeners=MagicMock(),
        check_and_recover_webrtc=MagicMock(return_value=None),
    )
    c.get_session = lambda cid: get_or_create_session(c._sessions, cid)
    return c


# ── source selection ────────────────────────────────────────────────────────
class TestLdiSource:
    def test_active_with_password(self) -> None:
        assert ldi_source(_coord(), CAM) == (IP, LDI_USER, PW)  # type: ignore[arg-type]

    def test_constants(self) -> None:
        assert LDI_RTSP_PORT == 9554
        assert LDI_USER == "localuser"

    @pytest.mark.parametrize("state", ["inactive", "unsupported", "bogus", None])
    def test_not_active_uses_normal_path(self, state: str | None) -> None:
        assert ldi_source(_coord(state=state), CAM) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "passwords",
        [
            {},
            {CAM: ""},
            {CAM: "   "},
            {CAM: None},
            {CAM: 12345},
            {"OTHER": PW},
            "absent",
            "str",
            ["x"],
        ],
    )
    def test_missing_or_garbage_password(self, passwords: object) -> None:
        assert ldi_source(_coord(passwords=passwords), CAM) is None  # type: ignore[arg-type]

    def test_no_lan_ip(self) -> None:
        assert ldi_source(_coord(ip=None), CAM) is None  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",
            "127.0.0.1",
            "169.254.169.254",
            "0.0.0.0",
            "not-an-ip",
            "10.0.0.1:1",
        ],
    )
    def test_unsafe_lan_ip_rejected(self, ip: str) -> None:
        assert ldi_source(_coord(ip=ip), CAM) is None  # type: ignore[arg-type]

    def test_stub_without_cache_attribute(self) -> None:
        assert ldi_source(SimpleNamespace(), CAM) is None  # type: ignore[arg-type]


# ── local session ───────────────────────────────────────────────────────────
class TestOpen:
    @pytest.mark.asyncio
    async def test_success_publishes_go2rtc_restream(self) -> None:
        c = _coord()
        probe, ensure = _open_patches()
        with probe as probe_mock, ensure as ensure_mock:
            res = await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        assert res is not None
        probe_mock.assert_awaited_once()
        ensure_mock.assert_awaited_once_with(
            c, CAM, f"rtsps://localuser:{PW}@{IP}:9554{_PATH_HIGH}", force=True
        )
        assert res["_connection_type"] == "LOCAL"
        assert res["_ldi"] is True
        assert res["urls"] == [f"{IP}:9554"]
        assert res["rtspsUrl"] == res["rtspUrl"] == RESTREAM
        assert "proxyUrl" not in res
        assert "_local_password" not in res and "_local_user" not in res
        assert PW not in repr(res)
        assert c.live_connections[CAM] is res
        assert c._quality_effective_inst[CAM] == 1
        assert CAM not in c.stream_warming
        assert c.get_session(CAM).stream_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_no_proxy_or_front_door_is_started(self) -> None:
        c = _coord()
        probe, ensure = _open_patches()
        with probe, ensure:
            await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        c.start_tls_proxy.assert_not_awaited()
        c.start_viewing_front_door.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_earlier_cloud_plumbing_is_stopped(self) -> None:
        c = _coord()
        probe, ensure = _open_patches()
        with probe, ensure:
            await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        c.stop_tls_proxy.assert_awaited_once_with(CAM)
        c.stop_viewing_front_door.assert_awaited_once_with(CAM)
        c.stop_remote_viewing_front_door.assert_awaited_once_with(CAM)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "password", ["p@ss", "a:b", "x/y", "100%", "sp ace", "ü?#&"]
    )
    async def test_password_with_reserved_characters_is_quoted(
        self, password: str
    ) -> None:
        from urllib.parse import quote

        c = _coord()
        probe, ensure = _open_patches()
        with probe, ensure as ensure_mock:
            await open_ldi_connection(c, CAM, (IP, LDI_USER, password))  # type: ignore[arg-type]
        src = ensure_mock.await_args.args[2]
        assert (
            src == f"rtsps://localuser:{quote(password, safe='')}@{IP}:9554{_PATH_HIGH}"
        )
        assert src.count("@") == 1

    def test_source_url_quotes_user_too(self) -> None:
        assert (
            ldi_local.ldi_source_url("10.0.0.9", "a b", "p:w")
            == "rtsps://a%20b:p%3Aw@10.0.0.9:9554/rtsp_tunnel"
            "?line=1&inst=1&enableaudio=1"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("status", "reason"),
        [(None, "unreachable"), (503, "unreachable"), (401, "auth")],
    )
    async def test_camera_failure_returns_none_without_registering(
        self, status: int | None, reason: str
    ) -> None:
        c = _coord()
        probe, ensure = _open_patches(status)
        with probe as probe_mock, ensure as ensure_mock:
            res = await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        assert res is None
        ensure_mock.assert_not_awaited()
        assert CAM not in c.live_connections
        assert CAM not in c.stream_warming
        assert c.ldi_open_status[CAM]["reason"] == reason
        # silence is retried once, a password verdict is not
        assert probe_mock.await_count == (1 if status == 401 else 2)

    @pytest.mark.asyncio
    async def test_go2rtc_missing_fails_closed(self) -> None:
        c = _coord()
        probe, ensure = _open_patches(200, None)
        with probe, ensure:
            res = await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        assert res is None
        assert CAM not in c.live_connections
        assert c.ldi_open_status[CAM]["reason"] == "no_go2rtc"
        assert c.get_session(CAM).stream_ready_event.is_set()

    @pytest.mark.asyncio
    async def test_abort_unregisters_the_stream(self) -> None:
        c = _coord()
        probe, ensure = _open_patches(200, None)
        with (
            probe,
            ensure,
            patch(f"{LDI}.ldi_go2rtc.unregister_stream", new=AsyncMock()) as gone,
        ):
            await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        gone.assert_awaited_once_with(c, CAM)

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_none_and_never_logs_the_password(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        c = _coord()
        probe, _ = _open_patches()
        boom = AsyncMock(
            side_effect=OSError(f"PUT rtsps://localuser:{PW}@{IP}:9554/x failed")
        )
        with probe, patch(f"{LDI}.ldi_go2rtc.ensure_stream", new=boom):
            res = await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        assert res is None
        assert PW not in caplog.text
        assert c.ldi_open_status[CAM]["reason"] == "unreachable"

    @pytest.mark.asyncio
    async def test_stale_stream_invalidated(self) -> None:
        c = _coord()
        stale = MagicMock()
        stale.stop = AsyncMock()
        ent = SimpleNamespace(stream=stale, async_refresh_providers=AsyncMock())
        c.camera_entities = {CAM: ent}
        probe, ensure = _open_patches()
        with probe, ensure:
            await open_ldi_connection(c, CAM, (IP, LDI_USER, PW))  # type: ignore[arg-type]
        stale.stop.assert_awaited_once()
        assert ent.stream is None
        ent.async_refresh_providers.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stale_stream_stop_error_swallowed(self) -> None:
        c = _coord()
        stale = MagicMock()
        stale.stop = AsyncMock(side_effect=RuntimeError("x"))
        ent = SimpleNamespace(
            stream=stale,
            async_refresh_providers=AsyncMock(side_effect=RuntimeError("y")),
        )
        c.camera_entities = {CAM: ent}
        probe, ensure = _open_patches()
        with probe, ensure:
            res = await open_ldi_connection(c, CAM, (IP, LDI_USER, PW), is_renewal=True)  # type: ignore[arg-type]
        assert res is not None
        assert ent.stream is None
        c.async_update_listeners.assert_not_called()


class TestEnsureLdiStream:
    @pytest.mark.asyncio
    async def test_registers_with_the_stored_password(self) -> None:
        c = _coord()
        with patch(
            f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=RESTREAM)
        ) as ensure:
            assert await ldi_local.ensure_ldi_stream(c, CAM) == RESTREAM  # type: ignore[arg-type]
        ensure.assert_awaited_once_with(
            c, CAM, f"rtsps://localuser:{PW}@{IP}:9554{_PATH_HIGH}"
        )

    @pytest.mark.asyncio
    async def test_no_local_source_means_no_stream(self) -> None:
        c = _coord(passwords={})
        with patch(f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock()) as ensure:
            assert await ldi_local.ensure_ldi_stream(c, CAM) is None  # type: ignore[arg-type]
        ensure.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_go2rtc_failure_is_tracked_and_clears_when_back(self) -> None:
        c = _coord()
        with patch(f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=None)):
            assert await ldi_local.ensure_ldi_stream(c, CAM) is None  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == "no_go2rtc"
        with patch(
            f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=RESTREAM)
        ):
            assert await ldi_local.ensure_ldi_stream(c, CAM) == RESTREAM  # type: ignore[arg-type]
        assert CAM not in c.ldi_open_status

    @pytest.mark.asyncio
    async def test_success_keeps_an_unrelated_failure(self) -> None:
        c = _coord()
        ldi_local.record_ldi_result(c, CAM, "auth")  # type: ignore[arg-type]
        with patch(
            f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=RESTREAM)
        ):
            await ldi_local.ensure_ldi_stream(c, CAM)  # type: ignore[arg-type]
        assert c.ldi_open_status[CAM]["reason"] == "auth"

    @pytest.mark.asyncio
    async def test_stub_without_status_map(self) -> None:
        c = _coord()
        del c.ldi_open_status
        with patch(
            f"{LDI}.ldi_go2rtc.ensure_stream", new=AsyncMock(return_value=RESTREAM)
        ):
            assert await ldi_local.ensure_ldi_stream(c, CAM) == RESTREAM  # type: ignore[arg-type]


# ── routing inside try_live_connection_inner: zero cloud calls ──────────────
class TestInnerRouting:
    @pytest.mark.asyncio
    async def test_active_with_password_never_touches_cloud(self) -> None:
        c = _coord()
        cloud = AsyncMock(side_effect=AssertionError("cloud session requested"))
        probe, ensure = _open_patches()
        with (
            patch(
                "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
                new=cloud,
            ),
            probe,
            ensure,
        ):
            res = await try_live_connection_inner(c, CAM)  # type: ignore[arg-type]
        assert res is not None and res["_ldi"] is True
        cloud.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failure_does_not_fall_back_to_cloud(self) -> None:
        c = _coord()
        cloud = AsyncMock(side_effect=AssertionError("cloud session requested"))
        probe, ensure = _open_patches(None)
        with (
            patch(
                "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
                new=cloud,
            ),
            probe,
            ensure,
        ):
            res = await try_live_connection_inner(c, CAM)  # type: ignore[arg-type]
        assert res is None
        cloud.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_go2rtc_does_not_fall_back_to_cloud(self) -> None:
        c = _coord()
        cloud = AsyncMock(side_effect=AssertionError("cloud session requested"))
        probe, ensure = _open_patches(200, None)
        with (
            patch(
                "custom_components.bosch_shc_camera.async_get_bosch_cloud_session",
                new=cloud,
            ),
            probe,
            ensure,
        ):
            res = await try_live_connection_inner(c, CAM)  # type: ignore[arg-type]
        assert res is None
        cloud.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "passwords"),
        [
            ("active", {}),
            ("active", {CAM: ""}),
            ("inactive", {CAM: PW}),
            (None, {CAM: PW}),
        ],
    )
    async def test_other_modes_take_normal_path(
        self, state: str | None, passwords: dict[str, str]
    ) -> None:
        c = _coord(state=state, passwords=passwords, token=None)
        with patch(
            "custom_components.bosch_shc_camera.live_connection.open_ldi_connection",
            new=AsyncMock(),
        ) as opened:
            # token=None: normal path stops at the token gate, proving it was reached
            res = await try_live_connection_inner(c, CAM)  # type: ignore[arg-type]
        assert res is None
        opened.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_interface_without_password_keeps_cloud_path(self) -> None:
        """Interface active but no password stored: normal cloud/TLS-proxy path,
        no go2rtc registration, no probe."""
        c = _coord(passwords={}, token=None)
        probe, ensure = _open_patches()
        with probe as probe_mock, ensure as ensure_mock:
            res = await try_live_connection_inner(c, CAM)  # type: ignore[arg-type]
        assert res is None  # stops at the cloud token gate
        probe_mock.assert_not_awaited()
        ensure_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_privacy_on_blocks_before_local_branch(self) -> None:
        c = _coord(privacy=True)
        c.get_stream_lock = lambda _cid: None
        with patch(
            "custom_components.bosch_shc_camera.live_connection.open_ldi_connection",
            new=AsyncMock(),
        ) as opened:
            res = await BoschCameraCoordinator.try_live_connection(c, CAM)  # type: ignore[arg-type]
        assert res is None
        opened.assert_not_awaited()


# ── guards ──────────────────────────────────────────────────────────────────
class TestGuards:
    @pytest.mark.asyncio
    async def test_rcp_read_skipped_for_ldi_session(self) -> None:
        c = SimpleNamespace(
            live_connections={
                CAM: {
                    "_connection_type": "LOCAL",
                    "_ldi": True,
                    "_local_user": LDI_USER,
                    "_local_password": PW,
                    "urls": [f"{IP}:9554"],
                }
            },
            hass=SimpleNamespace(async_add_executor_job=AsyncMock()),
        )
        assert (
            await BoschCameraCoordinator._rcp_read_active(c, CAM, "0x0001", "int")
            is None
        )  # type: ignore[arg-type]
        c.hass.async_add_executor_job.assert_not_awaited()

    def _lifecycle_coord(self, *, ldi: bool) -> SimpleNamespace:
        live = {"_connection_type": "LOCAL"}
        if ldi:
            live["_ldi"] = True
        return SimpleNamespace(
            stream_worker_dispatch_pending={CAM},
            record_stream_error=MagicMock(),
            get_model_config=MagicMock(
                return_value=SimpleNamespace(max_stream_errors=3)
            ),
            live_connections={CAM: live},
            stream_error_count={CAM: 5},
            stream_fell_back={},
            local_rescue_attempts={},
            local_rescue_at={},
            tls_proxy_rebuild_last={},
            try_live_connection=AsyncMock(return_value={"_connection_type": "LOCAL"}),
        )

    @pytest.mark.asyncio
    async def test_no_remote_escalation_for_ldi(self) -> None:
        c = self._lifecycle_coord(ldi=True)
        await handle_stream_worker_error(c, CAM, "connection refused")  # type: ignore[arg-type]
        assert c.stream_fell_back.get(CAM) is None
        c.try_live_connection.assert_awaited_once_with(CAM, force_reset=True)
        # second burst inside the backoff window does not rebuild again
        await handle_stream_worker_error(c, CAM, "connection refused")  # type: ignore[arg-type]
        c.try_live_connection.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cloud_local_session_still_escalates(self) -> None:
        c = self._lifecycle_coord(ldi=False)
        await handle_stream_worker_error(c, CAM, "connection refused")  # type: ignore[arg-type]
        assert c.stream_fell_back[CAM] is True

    @pytest.mark.asyncio
    async def test_frigate_resolve_does_not_lazy_open_for_ldi(self) -> None:
        c = _coord()
        c.live_connections = {
            CAM: {"_connection_type": "LOCAL", "_ldi": True, "urls": [f"{IP}:9554"]}
        }
        c.try_live_connection = AsyncMock()
        with patch(
            "custom_components.bosch_shc_camera.frigate_endpoint.ensure_ldi_stream",
            new=AsyncMock(return_value=RESTREAM),
        ):
            target = await BoschCameraCoordinator._frigate_resolve_inner(c, CAM)  # type: ignore[arg-type]
        assert target is not None and target.port == 18554
        c.try_live_connection.assert_not_awaited()


# ── redaction ───────────────────────────────────────────────────────────────
def test_password_redacted_in_diagnostics() -> None:
    assert {"local_passwords", "_local_password"} <= TO_REDACT
    out = async_redact_data(
        {LDI_PASSWORDS_OPTION: {CAM: PW}, "live": {"_local_password": PW}}, TO_REDACT
    )
    assert PW not in repr(out)


# ── options flow ────────────────────────────────────────────────────────────
def _flow(
    options: dict | None = None, *, cameras: bool = True
) -> BoschCameraOptionsFlow:
    data = {CAM: {"info": {"title": "Terrace"}}} if cameras else {}
    entry = SimpleNamespace(
        entry_id="01TEST",
        data={"bearer_token": "", "refresh_token": "rt"},
        options=options or {},
        runtime_data=SimpleNamespace(data=data),
    )
    flow = BoschCameraOptionsFlow(entry)  # type: ignore[arg-type]
    flow.async_create_entry = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: {"type": "create_entry", "data": kw["data"]}
    )
    flow.async_show_form = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda **kw: {"type": "form", **kw}
    )
    return flow


class TestOptionsFlow:
    @pytest.mark.asyncio
    async def test_init_flag_leads_to_password_step(self) -> None:
        flow = _flow({"scan_interval": 60})
        res = await flow.async_step_init({"auth": {"configure_local_password": True}})
        assert res["type"] == "form" and res["step_id"] == "local_passwords"
        assert "configure_local_password" not in flow._pending_options

    @pytest.mark.asyncio
    async def test_init_without_flag_saves_directly(self) -> None:
        flow = _flow()
        res = await flow.async_step_init({"auth": {"force_relogin": False}})
        assert res["type"] == "create_entry"
        assert "configure_local_password" not in res["data"]

    @pytest.mark.asyncio
    async def test_set_password(self) -> None:
        flow = _flow({"x": 1})
        flow._pending_options = {"x": 1}
        res = await flow.async_step_local_passwords({"Terrace": f"  {PW} "})
        assert res["type"] == "create_entry"
        assert res["data"][LDI_PASSWORDS_OPTION] == {CAM: PW}
        assert res["data"]["x"] == 1

    @pytest.mark.asyncio
    async def test_empty_and_whitespace_keep_stored(self) -> None:
        for value in ("", "   ", None):
            flow = _flow()
            flow._pending_options = {LDI_PASSWORDS_OPTION: {CAM: PW}}
            res = await flow.async_step_local_passwords({"Terrace": value})
            assert res["data"][LDI_PASSWORDS_OPTION] == {CAM: PW}

    @pytest.mark.asyncio
    async def test_overwrite(self) -> None:
        flow = _flow()
        flow._pending_options = {LDI_PASSWORDS_OPTION: {CAM: "old"}}
        res = await flow.async_step_local_passwords({"Terrace": PW})
        assert res["data"][LDI_PASSWORDS_OPTION] == {CAM: PW}

    @pytest.mark.asyncio
    async def test_clear_only_selected_camera(self) -> None:
        other = "22222222-2222-2222-2222-222222222222"
        flow = _flow()
        flow._pending_options = {LDI_PASSWORDS_OPTION: {CAM: PW, other: "keep"}}
        res = await flow.async_step_local_passwords({"clear_password_for": [CAM]})
        assert res["data"][LDI_PASSWORDS_OPTION] == {other: "keep"}

    @pytest.mark.asyncio
    async def test_clear_wins_over_typed_value(self) -> None:
        flow = _flow()
        flow._pending_options = {LDI_PASSWORDS_OPTION: {CAM: "old"}}
        res = await flow.async_step_local_passwords(
            {"Terrace": PW, "clear_password_for": [CAM]}
        )
        assert res["data"][LDI_PASSWORDS_OPTION] == {}

    @pytest.mark.asyncio
    async def test_unknown_ids_and_labels_ignored(self) -> None:
        flow = _flow()
        flow._pending_options = {"x": 1, LDI_PASSWORDS_OPTION: {"gone": "keep"}}
        res = await flow.async_step_local_passwords(
            {"Nope": PW, "clear_password_for": ["gone", "nope"]}
        )
        assert res["data"][LDI_PASSWORDS_OPTION] == {"gone": "keep"}
        assert res["data"]["x"] == 1

    @pytest.mark.asyncio
    async def test_no_cameras_saves_pending_unchanged(self) -> None:
        flow = _flow(cameras=False)
        flow._pending_options = {"x": 1}
        res = await flow.async_step_local_passwords(None)
        assert res["type"] == "create_entry" and res["data"] == {"x": 1}

    @pytest.mark.asyncio
    async def test_unset_runtime_data(self) -> None:
        flow = _flow()
        flow._config_entry.runtime_data = None
        flow._pending_options = {}
        res = await flow.async_step_local_passwords(None)
        assert res["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_form_renders_one_field_per_camera(self) -> None:
        other = "22222222-2222-2222-2222-222222222222"
        flow = _flow(
            {LDI_PASSWORDS_OPTION: {CAM: PW}},
        )
        flow._pending_options = {LDI_PASSWORDS_OPTION: {CAM: PW}}
        flow._config_entry.runtime_data = SimpleNamespace(
            data={
                CAM: {"info": {"title": "Terrace"}},
                other: {"info": {"title": "Terrace"}},
                "33333333-3333": {"info": {}},
            },
            local_data_interface_cache={CAM: {"state": "active"}},
        )
        res = await flow.async_step_local_passwords(None)
        keys = {str(k) for k in res["data_schema"].schema}
        assert keys == {
            "Terrace",
            "Terrace (22222222)",
            "33333333",
            "clear_password_for",
        }
        text = res["description_placeholders"]["cameras"]
        assert "- Terrace: interface active, password set" in text
        assert "- Terrace (22222222): interface not active, password not set" in text
        # stored values are never echoed back
        assert PW not in repr(res["data_schema"].schema)
        assert PW not in repr(res["description_placeholders"])

    @pytest.mark.asyncio
    async def test_passwords_never_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level("DEBUG")
        flow = _flow()
        flow._pending_options = {LDI_PASSWORDS_OPTION: {CAM: "old-pw"}}
        await flow.async_step_local_passwords(None)
        await flow.async_step_local_passwords({"Terrace": PW})
        assert PW not in caplog.text and "old-pw" not in caplog.text

    @pytest.mark.asyncio
    async def test_existing_passwords_survive_normal_save(self) -> None:
        flow = _flow({LDI_PASSWORDS_OPTION: {CAM: PW}})
        res = await flow.async_step_init({"auth": {"force_relogin": False}})
        assert res["data"][LDI_PASSWORDS_OPTION] == {CAM: PW}


class TestRedactLiveConnectionSecrets:
    """The open_live_connection log line must not leak the local password."""

    def test_local_password_and_url_userinfo_redacted(self) -> None:
        from custom_components.bosch_shc_camera import _redact_creds

        out = _redact_creds(
            {
                "_local_password": "fake-pw-1234",
                "rtspsUrl": "rtsp://localuser:fake-pw-1234@127.0.0.1:8554/x?inst=1",
                "urls": ["10.0.0.5:9554"],
            }
        )
        assert "fake-pw-1234" not in str(out)
        assert out["rtspsUrl"].endswith("@127.0.0.1:8554/x?inst=1")
        assert out["urls"] == ["10.0.0.5:9554"]


# ── unknown status after a restart must not open the cloud path ─────────────
class TestUnknownStatusStaysLocal:
    """Cache is in-memory: before the first slow-tier poll the status is unknown."""

    def _unknown(self, fw: str | None, *, source: str = "cache") -> SimpleNamespace:
        c = _coord(state=None)
        c.firmware_cache = {CAM: {"current": fw}} if source == "cache" else {}
        c.data = {CAM: {"info": {"firmwareVersion": fw}}} if source == "info" else {}
        return c

    @pytest.mark.parametrize("source", ["cache", "info"])
    def test_qualifying_firmware_is_local_only(self, source: str) -> None:
        from custom_components.bosch_shc_camera.ldi_local import ldi_wanted

        assert ldi_wanted(self._unknown("9.40.105", source=source), CAM)  # type: ignore[arg-type]

    @pytest.mark.parametrize("fw", ["9.40.104", None, "garbage"])
    def test_old_or_unknown_firmware_uses_normal_path(self, fw: str | None) -> None:
        from custom_components.bosch_shc_camera.ldi_local import ldi_wanted

        assert not ldi_wanted(self._unknown(fw), CAM)  # type: ignore[arg-type]

    def test_known_inactive_beats_firmware(self) -> None:
        from custom_components.bosch_shc_camera.ldi_local import ldi_wanted

        c = self._unknown("9.40.105")
        c.local_data_interface_cache = {CAM: {"state": "inactive"}}
        assert not ldi_wanted(c, CAM)  # type: ignore[arg-type]


# ── cloud heartbeat must not outlive a switch to the local source ───────────
class TestHeartbeatSwitchesToLocal:
    @pytest.mark.asyncio
    async def test_cloud_session_renewed_locally_when_interface_becomes_wanted(
        self,
    ) -> None:
        from custom_components.bosch_shc_camera.session_renewal import (
            auto_renew_local_session,
        )

        c = _coord()
        c.get_model_config = lambda _cid: SimpleNamespace(
            heartbeat_interval=1, renewal_interval=9999
        )
        c.live_connections = {CAM: {"_connection_type": "LOCAL"}}  # cloud LOCAL
        c.session_stale = {}
        c.renewal_tasks = {}

        async def _renew(cid: str, is_renewal: bool = False) -> dict[str, bool]:
            c.get_session(cid).generation += 1  # new session -> loop exits
            return {"ok": True}

        c.try_live_connection = AsyncMock(side_effect=_renew)
        cloud = AsyncMock(side_effect=AssertionError("cloud heartbeat sent"))
        with (
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "custom_components.bosch_shc_camera.session_renewal."
                "async_bosch_cloud_session_cm",
                new=cloud,
            ),
        ):
            await auto_renew_local_session(c, CAM, 0)  # type: ignore[arg-type]
        c.try_live_connection.assert_awaited_once_with(CAM, is_renewal=True)
        cloud.assert_not_called()
